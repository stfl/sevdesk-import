"""The import window, which exists because sevDesk does not de-duplicate CSV imports.

Its failure mode is silent omission rather than a visible error, so the boundary
behaviour is asserted exactly: both bounds inclusive, filtering on settlement, and
two adjacent windows partitioning a statement with nothing lost between them.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from sevdesk_importer.model import Statement
from sevdesk_importer.providers import parse_statement
from sevdesk_importer.window import Window, apply_window


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
