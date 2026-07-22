"""Movements priced into EUR bookings: fee rows, Verwendungszweck, ordering.

These assert on structured records rather than CSV text, so a failure names the
rule that broke.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from sevdesk_importer.conversion import Conversion, convert
from sevdesk_importer.formatting import GERMAN, US
from sevdesk_importer.model import Booking
from sevdesk_importer.providers import parse_statement
from sevdesk_importer.rates import EcbSeries, parse_ecb_csv


@pytest.fixture
def series(ecb_response: bytes) -> EcbSeries:
    return EcbSeries(parse_ecb_csv(ecb_response.decode()))


@pytest.fixture
def wise(wise_statement: Path, series: EcbSeries) -> Conversion:
    statement = parse_statement(wise_statement.read_text())
    return convert(statement.movements, series, GERMAN)


@pytest.fixture
def revolut(revolut_statement: Path, series: EcbSeries) -> Conversion:
    statement = parse_statement(revolut_statement.read_text())
    return convert(statement.movements, series, GERMAN)


def booking_by(conversion: Conversion, fragment: str) -> Booking:
    matches = [b for b in conversion.bookings if fragment in b.source_ref]
    assert len(matches) == 1, f"expected exactly one booking for {fragment!r}"
    return matches[0]


class TestPricing:
    def test_an_ecb_priced_row_uses_the_reciprocal_of_the_published_quote(
        self, revolut: Conversion
    ) -> None:
        """796.80 USD on 2026-06-11, quoted 1.1537 USD per EUR, is 690.65 EUR."""
        booking = booking_by(revolut, "2026-06-11 11:55:29")
        assert booking.amount_eur == Decimal("-690.65")

    def test_a_row_with_a_recorded_rate_uses_it(self, wise: Conversion) -> None:
        """32.12 USD at the rate Wise itself charged, 0.875, is 28.11 EUR."""
        booking = booking_by(wise, "9000000001")
        assert booking.amount_eur == Decimal("-28.11")
        assert booking.rate.provenance == "export"

    def test_an_inverted_recorded_rate_prices_an_incoming_conversion(
        self, wise: Conversion
    ) -> None:
        """115.00 USD credited against 100.00 EUR sent."""
        assert booking_by(wise, "9000000008").amount_eur == Decimal("100.00")

    def test_money_arriving_is_signed_positive(self, wise: Conversion) -> None:
        assert booking_by(wise, "9000000003").amount_eur > 0

    def test_money_leaving_is_signed_negative(self, wise: Conversion) -> None:
        assert booking_by(wise, "9000000004").amount_eur < 0

    def test_amounts_are_rounded_to_cents(self, revolut: Conversion) -> None:
        assert all(b.amount_eur == b.amount_eur.quantize(Decimal("0.01")) for b in revolut.bookings)


class TestFees:
    """A fee rides inside the booking it belongs to, as the bank statement shows it."""

    def test_a_fee_is_folded_into_the_amount_rather_than_split_out(self, wise: Conversion) -> None:
        """32.00 reached the merchant and 0.12 was charged; 32.12 left the account."""
        booking = booking_by(wise, "9000000001")
        assert booking.amount_usd == Decimal("-32.12")
        assert booking.fee_usd == Decimal("0.12")

    def test_a_fee_on_an_incoming_transfer_reduces_what_arrived(self, wise: Conversion) -> None:
        """3465.50 was sent, 15.50 was taken, 3450.00 landed."""
        booking = booking_by(wise, "9000000003")
        assert booking.amount_usd == Decimal("3450.00")
        assert booking.fee_usd == Decimal("15.50")

    def test_one_movement_produces_exactly_one_booking(self, wise: Conversion) -> None:
        assert len({b.source_ref for b in wise.bookings}) == len(wise.bookings)

    def test_the_verwendungszweck_still_names_the_fee(self, wise: Conversion) -> None:
        """Folding the fee in must not hide it."""
        assert "davon 0,12 USD Gebühr" in booking_by(wise, "9000000001").purpose

    def test_a_fee_taken_before_the_money_arrived_is_not_called_part_of_it(
        self, wise: Conversion
    ) -> None:
        """3450.00 landed; the 15.50 was deducted from what was sent, not from this."""
        assert "abzgl. 15,50 USD Gebühr" in booking_by(wise, "9000000003").purpose

    def test_a_row_without_a_fee_says_nothing_about_one(self, wise: Conversion) -> None:
        assert "Gebühr" not in booking_by(wise, "9000000004").purpose

    def test_the_fee_is_recorded_for_every_booking(self, wise: Conversion) -> None:
        assert booking_by(wise, "9000000004").fee_usd == Decimal(0)

    def test_a_row_that_is_only_a_fee_books_the_fee_alone(self, revolut: Conversion) -> None:
        """A zero amount with a nonzero fee still moved the balance."""
        booking = booking_by(revolut, "Card delivery fee")
        assert booking.amount_usd == Decimal("-3.20")
        assert booking.amount_eur == Decimal("-2.75")

    def test_a_movement_too_small_to_book_is_reported_rather_than_dropped_silently(
        self, series: EcbSeries
    ) -> None:
        """Suppressing a sub-half-cent row would otherwise break reconciliation unseen."""
        payload = (
            "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
            "Topup,Pro,2026-06-11 10:00:00,2026-06-11 10:00:00,Top-up,0.004,0.000,USD,COMPLETED,0.004\n"
        )
        conversion = convert(parse_statement(payload).movements, series, GERMAN)
        assert conversion.bookings == ()
        assert [drop.reason for drop in conversion.drops] == ["rounds_to_zero"]


class TestReconciliation:
    def test_splitting_a_fee_out_preserves_an_incoming_balance_movement(
        self, wise: Conversion
    ) -> None:
        """3465.50 credited less 15.50 charged is the 3450.00 that actually arrived."""
        rows = [b for b in wise.bookings if "9000000003" in b.source_ref]
        assert sum(b.amount_usd for b in rows) == Decimal("3450.00")

    def test_splitting_a_fee_out_preserves_an_outgoing_balance_movement(
        self, wise: Conversion
    ) -> None:
        rows = [b for b in wise.bookings if "9000000001" in b.source_ref]
        assert sum(b.amount_usd for b in rows) == Decimal("-32.12")

    def test_every_movement_is_fully_accounted_for(
        self, revolut: Conversion, revolut_statement: Path
    ) -> None:
        """Across the whole statement, emitted rows sum to what the balance did."""
        statement = parse_statement(revolut_statement.read_text())
        assert sum(b.amount_usd for b in revolut.bookings) == sum(
            m.amount_usd - m.fee_usd for m in statement.movements
        )


class TestVerwendungszweck:
    def test_it_states_the_original_usd_amount_and_the_rate(self, wise: Conversion) -> None:
        purpose = booking_by(wise, "9000000003").purpose
        assert "3.450,00 USD zu Kurs " in purpose

    def test_it_opens_with_the_sevdesk_phrasing_on_a_purchase(self, wise: Conversion) -> None:
        assert booking_by(wise, "9000000001").purpose.startswith(
            "Card transaction of 32.50 EUR issued by Example SaaS Ltd"
        )

    def test_the_invoice_total_survives_into_the_output(self, wise: Conversion) -> None:
        """One search on the total reunites both legs across two sevDesk accounts."""
        assert "32.50" in booking_by(wise, "9000000001").purpose

    def test_a_revolut_description_is_preserved_in_full(self, revolut: Conversion) -> None:
        assert booking_by(revolut, "To USD Savings Pocket").purpose.startswith(
            "To USD Savings Pocket"
        )

    def test_the_fx_detail_follows_the_selected_number_format(
        self, wise_statement: Path, series: EcbSeries
    ) -> None:
        statement = parse_statement(wise_statement.read_text())
        american = convert(statement.movements, series, US)
        purpose = [b for b in american.bookings if "9000000003" in b.source_ref][0].purpose
        assert "3450.00 USD zu Kurs " in purpose


class TestRateReporting:
    def test_every_rate_used_is_reported_once_with_its_provenance(
        self, revolut: Conversion
    ) -> None:
        assert {r.booking_date for r in revolut.rates} == {b.booking_date for b in revolut.bookings}
        assert len(revolut.rates) == len({r.booking_date for r in revolut.rates})

    def test_a_substituted_rate_produces_a_warning(self, revolut: Conversion) -> None:
        """2026-05-31 is a Sunday; the rate comes from Friday the 29th."""
        assert any("2026-05-31" in w and "2026-05-29" in w for w in revolut.warnings)

    def test_an_exact_rate_produces_no_warning(self, wise: Conversion) -> None:
        assert not any("2026-06-11" in w for w in wise.warnings)


class TestDeterministicOrdering:
    def test_bookings_are_ordered_by_settlement_date(self, revolut: Conversion) -> None:
        dates = [b.booking_date for b in revolut.bookings]
        assert dates == sorted(dates)

    def test_rows_settling_on_one_day_keep_their_order_in_the_export(
        self, revolut: Conversion
    ) -> None:
        same_day = [b for b in revolut.bookings if b.booking_date == date(2026, 5, 28)]
        assert [b.purpose.split(" |")[0] for b in same_day] == [
            "Merchant payment",
            "Exchanged to EUR",
        ]

    def test_converting_twice_produces_identical_bookings(
        self, revolut_statement: Path, series: EcbSeries
    ) -> None:
        statement = parse_statement(revolut_statement.read_text())
        assert convert(statement.movements, series, GERMAN) == convert(
            statement.movements, series, GERMAN
        )
