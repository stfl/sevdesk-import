"""The import window, which exists because sevDesk does not de-duplicate CSV imports.

Its failure mode is silent omission rather than a visible error, so the boundary
behaviour is asserted exactly: both bounds inclusive, filtering on settlement, and
two adjacent windows partitioning a statement with nothing lost between them.

The upper bound is derived rather than chosen, so the rule that derives it is asserted
here too — a row that could still settle holds the window back, one that never can does
not, and a row that could settle but cannot be dated stops the run outright.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from sevdesk_importer.model import Statement, Unsettled
from sevdesk_importer.providers import parse_statement
from sevdesk_importer.window import (
    UndatableSettlement,
    Window,
    apply_window,
    settlement_ceiling,
)


@pytest.fixture
def statement(revolut_statement: Path) -> Statement:
    return parse_statement(revolut_statement.read_text())


class TestBoundsAreInclusive:
    def test_a_row_on_the_lower_bound_is_kept(self, statement: Statement) -> None:
        kept, _ = apply_window(statement.movements, Window(date(2026, 5, 28), date(2026, 6, 30)))
        assert date(2026, 5, 28) in {m.booking_date for m in kept}

    def test_a_row_on_the_upper_bound_is_kept(self, statement: Statement) -> None:
        kept, _ = apply_window(statement.movements, Window(date(2026, 5, 1), date(2026, 5, 28)))
        assert date(2026, 5, 28) in {m.booking_date for m in kept}

    def test_a_row_one_day_below_the_lower_bound_is_excluded(self, statement: Statement) -> None:
        kept, _ = apply_window(statement.movements, Window(date(2026, 5, 29), date(2026, 6, 30)))
        assert date(2026, 5, 28) not in {m.booking_date for m in kept}

    def test_a_row_one_day_above_the_upper_bound_is_excluded(self, statement: Statement) -> None:
        kept, _ = apply_window(statement.movements, Window(date(2026, 5, 1), date(2026, 5, 27)))
        assert date(2026, 5, 28) not in {m.booking_date for m in kept}


class TestTheWindowFiltersOnSettlement:
    def test_a_row_that_settled_a_week_after_it_started_is_caught_by_its_settlement(
        self, statement: Statement
    ) -> None:
        """Started 2026-05-21, settled 2026-05-28; a window opening on the 22nd keeps it."""
        kept, _ = apply_window(statement.movements, Window(date(2026, 5, 22), date(2026, 5, 31)))
        assert any(m.lead == "Merchant payment" for m in kept)

    def test_the_window_covering_its_start_but_not_its_settlement_excludes_it(
        self, statement: Statement
    ) -> None:
        kept, _ = apply_window(statement.movements, Window(date(2026, 5, 21), date(2026, 5, 27)))
        assert not any(m.lead == "Merchant payment" for m in kept)


class TestOmittedBounds:
    def test_no_lower_bound_converts_everything_up_to_the_upper(self, statement: Statement) -> None:
        kept, dropped = apply_window(statement.movements, Window(None, date(2026, 12, 31)))
        assert len(kept) == len(statement.movements)
        assert dropped == ()

    def test_no_upper_bound_converts_everything_from_the_lower(self, statement: Statement) -> None:
        """Nothing outstanding means the statement bounds itself."""
        kept, dropped = apply_window(statement.movements, Window(None, None))
        assert len(kept) == len(statement.movements)
        assert dropped == ()


class TestAdjacentWindowsPartitionExactly:
    def test_every_row_falls_in_exactly_one_of_two_adjacent_windows(
        self, statement: Statement
    ) -> None:
        first = Window(None, date(2026, 5, 31))
        second = Window(first.next_since, date(2026, 12, 31))

        early, _ = apply_window(statement.movements, first)
        late, _ = apply_window(statement.movements, second)

        assert len(early) + len(late) == len(statement.movements), "rows lost or double-counted"
        assert set(early).isdisjoint(late)
        assert set(early) | set(late) == set(statement.movements)

    def test_the_recommended_next_lower_bound_is_the_day_after_the_upper(self) -> None:
        """This is exactly the value that makes the partition hold."""
        assert Window(None, date(2026, 5, 31)).next_since == date(2026, 6, 1)

    def test_the_next_lower_bound_crosses_a_month_end(self) -> None:
        assert Window(None, date(2026, 12, 31)).next_since == date(2027, 1, 1)

    def test_covering_nothing_leaves_the_lower_bound_where_it_was(self) -> None:
        """A run that booked nothing must not advance the resume point past unseen days."""
        assert Window(date(2026, 6, 1), None).next_since == date(2026, 6, 1)


class TestExclusionsAreCountedSeparately:
    def test_a_windowed_out_row_is_reported_as_outside_the_period(
        self, statement: Statement
    ) -> None:
        """ "Outside this period" must never be confused with "not bookable"."""
        _, dropped = apply_window(statement.movements, Window(date(2026, 6, 1), date(2026, 6, 30)))
        assert dropped
        assert {d.reason for d in dropped} == {"outside_window"}

    def test_the_drop_detail_names_the_settlement_date(self, statement: Statement) -> None:
        _, dropped = apply_window(statement.movements, Window(date(2026, 6, 1), date(2026, 6, 30)))
        assert any("2026-05-28" in d.detail for d in dropped)


class TestTheCeilingStopsShortOfWhatCouldStillSettle:
    def test_nothing_outstanding_leaves_the_window_unbounded(self) -> None:
        assert settlement_ceiling(()) is None

    def test_a_pending_row_pulls_the_ceiling_to_the_day_before_it_started(self) -> None:
        """A row that settles later must never be stepped over, so the window stops before it."""
        pending = Unsettled("REV-1", "PENDING", date(2026, 6, 14))
        assert settlement_ceiling([pending]) == date(2026, 6, 13)

    def test_the_earliest_outstanding_row_sets_the_ceiling(self) -> None:
        rows = [
            Unsettled("REV-2", "PENDING", date(2026, 6, 20)),
            Unsettled("REV-1", "PENDING", date(2026, 6, 14)),
            Unsettled("REV-3", "PENDING", date(2026, 6, 30)),
        ]
        assert settlement_ceiling(rows) == date(2026, 6, 13)

    def test_the_ceiling_crosses_a_month_boundary(self) -> None:
        assert settlement_ceiling([Unsettled("REV-1", "PENDING", date(2026, 7, 1))]) == date(
            2026, 6, 30
        )


class TestStatesThatCanNeverSettleDoNotHoldTheWindowBack:
    """Otherwise one cancelled transfer stalls every later import, permanently."""

    @pytest.mark.parametrize(
        "state", ["REVERTED", "DECLINED", "FAILED", "CANCELLED", "REFUNDED", "cancelled"]
    )
    def test_a_terminal_state_is_ignored(self, state: str) -> None:
        assert settlement_ceiling([Unsettled("W-1", state, date(2026, 6, 14))]) is None

    def test_a_terminal_row_does_not_mask_a_live_one(self) -> None:
        rows = [
            Unsettled("W-1", "CANCELLED", date(2026, 6, 1)),
            Unsettled("REV-1", "PENDING", date(2026, 6, 14)),
        ]
        assert settlement_ceiling(rows) == date(2026, 6, 13)

    def test_an_unrecognised_state_is_treated_as_still_live(self) -> None:
        """Unknown means unknown: stalling is recoverable, skipping money is not."""
        assert settlement_ceiling([Unsettled("REV-9", "AWAITING_REVIEW", date(2026, 6, 14))]) == (
            date(2026, 6, 13)
        )


class TestAnUndatableOutstandingRowRefuses:
    def test_a_live_row_without_a_start_date_is_refused(self) -> None:
        """There is no safe day to stop at, and guessing one would skip it silently."""
        with pytest.raises(UndatableSettlement):
            settlement_ceiling([Unsettled("REV-1", "PENDING", None)])

    def test_the_refusal_names_the_row_that_caused_it(self) -> None:
        with pytest.raises(UndatableSettlement, match="REV-1"):
            settlement_ceiling([Unsettled("REV-1", "PENDING", None)])

    def test_a_terminal_row_without_a_start_date_is_harmless(self) -> None:
        assert settlement_ceiling([Unsettled("W-1", "CANCELLED", None)]) is None
