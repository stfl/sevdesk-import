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


def booking_by(conversion: Conversion, fragment: str, *, fee: bool = False) -> Booking:
    matches = [b for b in conversion.bookings if fragment in b.source_ref and b.is_fee == fee]
    assert len(matches) == 1, f"expected one {'fee' if fee else 'main'} booking for {fragment!r}"
    return matches[0]


class TestPricing:
    def test_an_ecb_priced_row_uses_the_reciprocal_of_the_published_quote(
        self, revolut: Conversion
    ) -> None:
        """796.80 USD on 2026-06-11, quoted 1.1537 USD per EUR, is 690.65 EUR."""
        booking = booking_by(revolut, "2026-06-11 11:55:29")
        assert booking.belastung == Decimal("690.65")

    def test_a_row_with_a_recorded_rate_uses_it(self, wise: Conversion) -> None:
        """32.00 USD at the rate Wise itself charged, 0.875, is 28.00 EUR."""
        booking = booking_by(wise, "9000000001")
        assert booking.belastung == Decimal("28.00")
        assert booking.rate.provenance == "export"

    def test_an_inverted_recorded_rate_prices_an_incoming_conversion(
        self, wise: Conversion
    ) -> None:
        """115.00 USD credited against 100.00 EUR sent."""
        assert booking_by(wise, "9000000008").gutschrift == Decimal("100.00")

    def test_a_credit_populates_gutschrift_only(self, wise: Conversion) -> None:
        booking = booking_by(wise, "9000000003")
        assert booking.gutschrift is not None
        assert booking.belastung is None

    def test_a_debit_populates_belastung_only(self, wise: Conversion) -> None:
        booking = booking_by(wise, "9000000004")
        assert booking.belastung is not None
        assert booking.gutschrift is None

    def test_amounts_are_rounded_to_cents(self, revolut: Conversion) -> None:
        assert all(b.amount_eur == b.amount_eur.quantize(Decimal("0.01")) for b in revolut.bookings)


class TestFeeRows:
    def test_a_fee_becomes_its_own_row(self, wise: Conversion) -> None:
        assert booking_by(wise, "9000000003", fee=True).belastung is not None

    def test_a_fee_row_is_always_a_debit_in_both_directions(self, wise: Conversion) -> None:
        """The fee on an incoming transfer offsets the credit; on an outgoing one it adds."""
        assert booking_by(wise, "9000000003", fee=True).gutschrift is None
        assert booking_by(wise, "9000000001", fee=True).gutschrift is None

    def test_a_fee_row_carries_the_same_buchungstag_as_its_parent(self, wise: Conversion) -> None:
        parent = booking_by(wise, "9000000003")
        assert booking_by(wise, "9000000003", fee=True).booking_date == parent.booking_date

    def test_a_fee_row_carries_the_same_rate_as_its_parent(self, wise: Conversion) -> None:
        parent = booking_by(wise, "9000000001")
        assert booking_by(wise, "9000000001", fee=True).rate == parent.rate

    def test_a_fee_row_names_the_transaction_it_came_from(self, wise: Conversion) -> None:
        fee = booking_by(wise, "9000000003", fee=True)
        assert "Überweisung von Acme Client Inc" in fee.purpose
        assert fee.purpose.startswith("Gebühr zu:")

    def test_a_wise_fee_is_booked_against_the_bank_that_charged_it(self, wise: Conversion) -> None:
        assert booking_by(wise, "9000000003", fee=True).name == "Wise"

    def test_a_revolut_fee_row_stays_nameless(self, revolut: Conversion) -> None:
        assert booking_by(revolut, "MERCHANT_PAYMENT", fee=True).name == ""

    def test_a_row_that_is_only_a_fee_emits_the_fee_row_alone(self, revolut: Conversion) -> None:
        """No zero-value main row is ever written."""
        matching = [b for b in revolut.bookings if "Card delivery fee" in b.source_ref]
        assert len(matching) == 1
        assert matching[0].is_fee

    def test_a_row_without_a_fee_emits_no_fee_row(self, wise: Conversion) -> None:
        assert not any(b.is_fee for b in wise.bookings if "9000000004" in b.source_ref)

    def test_a_fee_too_small_to_book_is_reported_rather_than_dropped_silently(
        self, series: EcbSeries
    ) -> None:
        """Suppressing a sub-half-cent row would otherwise break reconciliation unseen."""
        payload = (
            "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
            "Topup,Pro,2026-06-11 10:00:00,2026-06-11 10:00:00,Top-up,100.00,0.001,USD,COMPLETED,99.999\n"
        )
        conversion = convert(parse_statement(payload).movements, series, GERMAN)
        assert not any(b.is_fee for b in conversion.bookings)
        assert [drop.reason for drop in conversion.drops] == ["rounds_to_zero"]
        assert "fee" in conversion.drops[0].detail


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
            m.balance_movement_usd for m in statement.movements
        )


class TestVerwendungszweck:
    def test_it_states_the_original_usd_amount_and_the_rate(self, wise: Conversion) -> None:
        purpose = booking_by(wise, "9000000003").purpose
        assert "3.465,50 USD zu Kurs " in purpose

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
        purpose = [b for b in american.bookings if "9000000003" in b.source_ref and not b.is_fee][
            0
        ].purpose
        assert "3465.50 USD zu Kurs " in purpose


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

    def test_a_fee_row_follows_its_parent_immediately(self, revolut: Conversion) -> None:
        refs = [b.source_ref for b in revolut.bookings]
        parent = refs.index("2026-05-28 10:30:31 MERCHANT_PAYMENT Merchant payment")
        assert revolut.bookings[parent + 1].is_fee
        assert revolut.bookings[parent + 1].source_ref == refs[parent]

    def test_rows_settling_on_one_day_keep_their_order_in_the_export(
        self, revolut: Conversion
    ) -> None:
        same_day = [b for b in revolut.bookings if b.booking_date == date(2026, 5, 28)]
        assert [b.purpose.split(" |")[0] for b in same_day] == [
            "Merchant payment",
            "Gebühr zu: Merchant payment",
            "Exchanged to EUR",
        ]

    def test_converting_twice_produces_identical_bookings(
        self, revolut_statement: Path, series: EcbSeries
    ) -> None:
        statement = parse_statement(revolut_statement.read_text())
        assert convert(statement.movements, series, GERMAN) == convert(
            statement.movements, series, GERMAN
        )
