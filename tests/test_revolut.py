"""Revolut rows to balance movements.

Revolut's Amount column is signed and its Fee is always a deduction, so the real
balance movement of a row is Amount minus Fee.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from sevdesk_importer.model import Movement, Statement
from sevdesk_importer.providers import UnknownTransactionType, parse_statement

REVOLUT_HEADER = (
    "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance"
)


def revolut_csv(*rows: str) -> str:
    return "\n".join([REVOLUT_HEADER, *rows]) + "\n"


@pytest.fixture
def statement(revolut_statement: Path) -> Statement:
    return parse_statement(revolut_statement.read_text())


def movements_by(statement: Statement, description: str) -> list[Movement]:
    return [m for m in statement.movements if m.lead == description]


def movement_by(statement: Statement, description: str) -> Movement:
    matches = movements_by(statement, description)
    assert len(matches) == 1, f"expected exactly one movement for {description!r}"
    return matches[0]


class TestProviderDetection:
    def test_the_revolut_schema_is_recognised(self, statement: Statement) -> None:
        assert statement.provider == "revolut"


class TestRowScope:
    def test_every_settled_row_moves_the_balance_and_is_emitted(self, statement: Statement) -> None:
        assert len(statement.movements) == 6
        assert statement.rows_read == 7

    def test_an_unsettled_row_is_dropped(self, statement: Statement) -> None:
        """A pending row has neither a completed date nor a balance."""
        drops = [d for d in statement.drops if d.reason == "not_settled"]
        assert len(drops) == 1

    def test_an_unsettled_row_neither_crashes_nor_emits_a_zero(self, statement: Statement) -> None:
        assert all(m.amount_usd != 0 or m.fee_usd != 0 for m in statement.movements)


class TestSettlementDate:
    def test_the_completed_date_is_the_buchungstag(self, statement: Statement) -> None:
        """This row started on 2026-05-21 and settled seven days later."""
        assert movement_by(statement, "Merchant payment").booking_date == date(2026, 5, 28)

    def test_revolut_timestamps_are_already_vienna_local(self, statement: Statement) -> None:
        assert movement_by(statement, "Google Pay top-up by *1234").booking_date == date(2026, 6, 1)


class TestAmountsAndFees:
    def test_a_credit_keeps_its_sign(self, statement: Statement) -> None:
        assert movement_by(statement, "Google Pay top-up by *1234").amount_usd == Decimal("800.00")

    def test_a_debit_keeps_its_sign(self, statement: Statement) -> None:
        assert movement_by(statement, "To USD Savings Pocket").amount_usd == Decimal("-6.55")

    def test_an_incoming_row_carries_its_fee_separately(self, statement: Statement) -> None:
        movement = movement_by(statement, "Merchant payment")
        assert movement.amount_usd == Decimal("9.00")
        assert movement.fee_usd == Decimal("0.45")

    def test_the_emitted_amounts_sum_to_the_real_balance_movement(
        self, statement: Statement
    ) -> None:
        """Amount minus Fee is exactly what the Balance column moved by."""
        movement = movement_by(statement, "Merchant payment")
        assert movement.amount_usd - movement.fee_usd == Decimal("8.55")

    def test_a_zero_amount_with_a_nonzero_fee_survives(self, statement: Statement) -> None:
        movement = movement_by(statement, "Card delivery fee")
        assert movement.amount_usd == Decimal("0.00")
        assert movement.fee_usd == Decimal("3.20")

    def test_exchanges_and_pocket_transfers_both_move_the_balance(
        self, statement: Statement
    ) -> None:
        exchanges = movements_by(statement, "Exchanged to EUR")
        assert sorted(m.amount_usd for m in exchanges) == [Decimal("-796.80"), Decimal("-2.00")]
        assert movement_by(statement, "To USD Savings Pocket").amount_usd == Decimal("-6.55")


class TestName:
    def test_the_name_column_is_always_empty(self, statement: Statement) -> None:
        """The export has no counterparty column; generic descriptions would be junk."""
        assert all(m.name == "" for m in statement.movements)

    def test_the_full_description_is_preserved_instead(self, statement: Statement) -> None:
        assert movement_by(statement, "Merchant payment").lead == "Merchant payment"
        assert movement_by(statement, "To USD Savings Pocket").lead == "To USD Savings Pocket"


class TestRecordedRate:
    def test_revolut_records_no_rate_so_ecb_prices_every_row(self, statement: Statement) -> None:
        assert all(m.recorded_rate is None for m in statement.movements)


class TestUnknownType:
    def test_an_unknown_transaction_type_fails_loudly(self) -> None:
        payload = revolut_csv(
            "Cryptostuff,Pro,2026-06-01 10:00:00,2026-06-01 10:00:00,Something new,-5.00,0.00,USD,COMPLETED,0.00"
        )
        with pytest.raises(UnknownTransactionType) as excinfo:
            parse_statement(payload)
        assert "Cryptostuff" in str(excinfo.value)


class TestOtherCurrencies:
    def test_a_row_in_another_currency_is_dropped(self) -> None:
        payload = revolut_csv(
            "Topup,Pro,2026-06-01 10:00:00,2026-06-01 10:00:00,Top-up,50.00,0.00,GBP,COMPLETED,50.00"
        )
        statement = parse_statement(payload)
        assert statement.movements == ()
        assert statement.drops[0].reason == "other_currency"
