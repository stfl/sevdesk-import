"""Wise rows to balance movements: scope, direction, fees, names, dates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from sevdesk_importer.model import Movement, Statement
from sevdesk_importer.providers import UnknownTransactionType, parse_statement

WISE_HEADER = (
    'ID,Status,Direction,"Created on","Finished on","Source fee amount",'
    '"Source fee currency","Target fee amount","Target fee currency","Source name",'
    '"Source amount (after fees)","Source currency","Target name",'
    '"Target amount (after fees)","Target currency","Exchange rate",Reference,Batch,'
    '"Created by",Category,Note'
)


def wise_csv(*rows: str) -> str:
    return "\n".join([WISE_HEADER, *rows]) + "\n"


@pytest.fixture
def statement(wise_statement: Path) -> Statement:
    return parse_statement(wise_statement.read_text())


def movement_by(statement: Statement, fragment: str) -> Movement:
    matches = [m for m in statement.movements if fragment in m.source_ref]
    assert len(matches) == 1, f"expected exactly one movement for {fragment!r}, got {len(matches)}"
    return matches[0]


class TestProviderDetection:
    def test_the_wise_schema_is_recognised(self, statement: Statement) -> None:
        assert statement.provider == "wise"


class TestRowScope:
    def test_only_rows_that_moved_the_usd_balance_are_emitted(self, statement: Statement) -> None:
        assert len(statement.movements) == 6
        assert statement.rows_read == 8

    def test_the_eur_funded_leg_of_a_split_payment_is_excluded(self, statement: Statement) -> None:
        """sevDesk already holds that leg from the Wise EUR auto-import."""
        drops = [d for d in statement.drops if d.reason == "funded_from_other_currency"]
        assert len(drops) == 1
        assert "9000000001" in drops[0].source_ref

    def test_an_unsettled_row_is_excluded(self, statement: Statement) -> None:
        drops = [d for d in statement.drops if d.reason == "not_settled"]
        assert len(drops) == 1
        assert "9000000007" in drops[0].source_ref

    def test_a_transaction_identifier_is_not_treated_as_a_key(self, statement: Statement) -> None:
        """One card payment appears twice under one ID, split across two currencies."""
        assert len([m for m in statement.movements if "9000000001" in m.source_ref]) == 1


class TestSettlementDate:
    def test_the_settlement_date_is_used_never_the_initiation_date(
        self, statement: Statement
    ) -> None:
        movement = movement_by(statement, "9000000003")
        assert movement.booking_date == date(2026, 5, 11)

    def test_a_late_utc_timestamp_books_on_the_next_vienna_day(self, statement: Statement) -> None:
        """Finished 2026-05-20 22:30 UTC, which is 00:30 on the 21st in Vienna."""
        assert movement_by(statement, "9000000001").booking_date == date(2026, 5, 21)


class TestDirectionAndFees:
    def test_an_outgoing_row_debits_the_amount_that_reached_the_counterparty(
        self, statement: Statement
    ) -> None:
        movement = movement_by(statement, "9000000001")
        assert movement.amount_usd == Decimal("-32.00")
        assert movement.fee_usd == Decimal("0.12")

    def test_an_incoming_transfer_is_booked_gross_with_the_fee_split_out(
        self, statement: Statement
    ) -> None:
        """15.50 was deducted before 3450.00 arrived; revenue is stated in full."""
        movement = movement_by(statement, "9000000003")
        assert movement.amount_usd == Decimal("3465.50")
        assert movement.fee_usd == Decimal("15.50")

    def test_the_emitted_amounts_sum_to_the_real_balance_movement(
        self, statement: Statement
    ) -> None:
        movement = movement_by(statement, "9000000003")
        assert movement.amount_usd - movement.fee_usd == Decimal("3450.00")

    def test_an_outgoing_fee_adds_to_the_debit(self, statement: Statement) -> None:
        movement = movement_by(statement, "9000000001")
        assert movement.amount_usd - movement.fee_usd == Decimal("-32.12")

    def test_empty_fee_columns_mean_no_fee(self, statement: Statement) -> None:
        assert movement_by(statement, "9000000005").fee_usd == Decimal(0)

    def test_an_export_without_fee_columns_at_all_still_reads(self) -> None:
        """Wise omits the fee columns entirely on some exports."""
        payload = (
            'ID,Status,Direction,"Finished on","Source name",'
            '"Source amount (after fees)","Source currency","Target name",'
            '"Target amount (after fees)","Target currency","Exchange rate",Reference\n'
            'TRANSFER-1,COMPLETED,OUT,"2026-05-11 10:00:00","Test Holder",'
            '10.00,USD,"Somebody",10.00,USD,1.0,\n'
        )
        statement = parse_statement(payload)
        assert statement.movements[0].fee_usd == Decimal(0)
        assert statement.movements[0].amount_usd == Decimal("-10.00")

    def test_a_fee_in_another_currency_does_not_touch_this_balance(
        self, statement: Statement
    ) -> None:
        assert movement_by(statement, "9000000008").fee_usd == Decimal(0)

    def test_an_amount_with_one_decimal_place_parses(self, statement: Statement) -> None:
        assert movement_by(statement, "9000000003").amount_usd == Decimal("3465.50")


class TestName:
    def test_incoming_rows_take_the_sender(self, statement: Statement) -> None:
        assert movement_by(statement, "9000000003").name == "Acme Client Inc"

    def test_outgoing_rows_take_the_recipient(self, statement: Statement) -> None:
        assert movement_by(statement, "9000000004").name == "Neobank Top-up"

    def test_an_empty_target_name_never_reaches_an_incoming_row(self, statement: Statement) -> None:
        """The cashback row has no target name; the sender still names it."""
        assert movement_by(statement, "9000000005").name == "TransferWise"

    def test_an_empty_source_name_never_reaches_an_outgoing_row(self, statement: Statement) -> None:
        assert movement_by(statement, "9000000006").name == "TransferWise"

    def test_no_wise_row_lacks_a_name(self, statement: Statement) -> None:
        assert all(m.name for m in statement.movements)


class TestRecordedRate:
    def test_a_usd_to_eur_conversion_uses_the_recorded_rate(self, statement: Statement) -> None:
        assert movement_by(statement, "9000000001").recorded_rate == Decimal("0.8750000000000000")

    def test_a_eur_to_usd_conversion_inverts_the_recorded_rate(self, statement: Statement) -> None:
        """The export quotes USD per EUR on that leg; the factor is its reciprocal."""
        assert movement_by(statement, "9000000008").recorded_rate == Decimal(1) / Decimal("1.15")

    def test_a_same_currency_row_records_no_rate(self, statement: Statement) -> None:
        """An exchange rate of 1.0 between two USD legs prices nothing."""
        assert movement_by(statement, "9000000004").recorded_rate is None
        assert movement_by(statement, "9000000003").recorded_rate is None


class TestVerwendungszweckLead:
    def test_a_converted_card_payment_uses_the_sevdesk_phrasing(self, statement: Statement) -> None:
        assert (
            movement_by(statement, "9000000001").lead
            == "Card transaction of 32.50 EUR issued by Example SaaS Ltd"
        )

    def test_the_invoice_total_sums_both_legs_of_a_split_payment(
        self, statement: Statement
    ) -> None:
        """28.00 EUR from the USD balance plus 4.50 EUR from the EUR balance."""
        assert "32.50" in movement_by(statement, "9000000001").lead

    def test_the_invoice_total_keeps_a_decimal_point_so_one_search_finds_both_legs(
        self, statement: Statement
    ) -> None:
        assert "32,50" not in movement_by(statement, "9000000001").lead

    def test_a_plain_transfer_is_described_simply(self, statement: Statement) -> None:
        assert movement_by(statement, "9000000004").lead == "Kartenzahlung an Neobank Top-up"

    def test_a_reference_is_carried_through(self, statement: Statement) -> None:
        assert (
            movement_by(statement, "9000000003").lead
            == "Überweisung von Acme Client Inc (SampleACH)"
        )


class TestUnknownType:
    UNKNOWN_ROW = (
        'NEW_WISE_THING-1,COMPLETED,OUT,"2026-05-11 10:00:00","2026-05-11 10:00:00",'
        '0.00,USD,,,"Test Holder",10.00,USD,"Somebody",10.00,USD,1.0,,,"Test Holder",Bills,'
    )

    def test_an_unknown_transaction_type_fails_loudly(self) -> None:
        with pytest.raises(UnknownTransactionType) as excinfo:
            parse_statement(wise_csv(self.UNKNOWN_ROW))
        assert "NEW_WISE_THING" in str(excinfo.value)

    def test_an_unknown_type_is_named_with_its_row(self) -> None:
        with pytest.raises(UnknownTransactionType) as excinfo:
            parse_statement(wise_csv(self.UNKNOWN_ROW))
        assert "NEW_WISE_THING-1" in str(excinfo.value)
