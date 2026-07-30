"""The import window.

sevDesk does not check for duplicates when it reads a CSV, and the only remedy on
its side is deleting each duplicated row by hand. Bounding the conversion by
settlement date is what keeps a re-exported overlapping period from double-booking.

Both bounds are inclusive, and the next run's lower bound is this run's upper bound
plus one day — the value that makes two adjacent windows partition a statement with
nothing lost between them.

The upper bound is not a date anyone picks. It is derived from the statement, and it
stops short of the first row that has not settled yet: such a row can settle later on
a day the window has already passed, and a window that stepped over it would skip that
money forever. Everything behind it defers to a later run instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from sevdesk_importer.model import Drop, Movement, Unsettled

#: States from which a row can never settle, so it never holds the window back. Every
#: other state — including one no export has shown us yet — counts as still live,
#: because stalling on a row that turns out to be dead is visible and recoverable,
#: while advancing past one that was merely slow loses a booking silently.
TERMINAL_STATES = frozenset({"REVERTED", "DECLINED", "FAILED", "CANCELLED", "REFUNDED"})


class UndatableSettlement(Exception):
    """A row could still settle, but carries no date to place the window edge behind."""


@dataclass(frozen=True)
class Window:
    """An inclusive range of Buchungstage. An absent bound means no bound on that side."""

    since: date | None
    until: date | None

    def contains(self, day: date) -> bool:
        if self.since is not None and day < self.since:
            return False
        return self.until is None or day <= self.until

    @property
    def next_since(self) -> date | None:
        """The lower bound to use next time, so no day falls between two runs.

        With no upper bound reached nothing was covered, so the current lower bound
        still stands.
        """
        if self.until is None:
            return self.since
        return self.until + timedelta(days=1)


def settlement_ceiling(unsettled: Iterable[Unsettled]) -> date | None:
    """The last day a window may cover without stepping over a row that could still settle.

    That is the day before the earliest such row. `None` means nothing is outstanding
    and the statement bounds itself.
    """
    live = [row for row in unsettled if row.state.strip().upper() not in TERMINAL_STATES]
    if not live:
        return None

    undatable = [row for row in live if row.initiated_on is None]
    if undatable:
        blocker = undatable[0]
        raise UndatableSettlement(
            f"{blocker.source_ref}: state {blocker.state or 'missing'} may still settle, but "
            "the row carries no initiation date, so there is no safe day to stop the window. "
            "Refusing rather than risk skipping it."
        )

    return min(row.initiated_on for row in live if row.initiated_on is not None) - timedelta(days=1)


def apply_window(
    movements: Iterable[Movement], window: Window
) -> tuple[tuple[Movement, ...], tuple[Drop, ...]]:
    """Split movements into those the window covers and those it excludes.

    Excluded rows are returned separately from rows that are not bookable at all, so
    "outside this period" is never read as "not importable".
    """
    kept: list[Movement] = []
    excluded: list[Drop] = []
    for movement in movements:
        if window.contains(movement.booking_date):
            kept.append(movement)
        else:
            excluded.append(
                Drop(
                    movement.source_ref,
                    "outside_window",
                    f"settled {movement.booking_date.isoformat()}, outside the requested window",
                    movement.lead,
                    movement.amount_usd - movement.fee_usd,
                    "USD",
                )
            )
    return tuple(kept), tuple(excluded)
