"""The import window.

sevDesk does not check for duplicates when it reads a CSV, and the only remedy on
its side is deleting each duplicated row by hand. Bounding the conversion by
settlement date is what keeps a re-exported overlapping period from double-booking.

Both bounds are inclusive, and the next run's lower bound is this run's upper bound
plus one day — the value that makes two adjacent windows partition a statement with
nothing lost between them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from sevdesk_importer.model import Drop, Movement


@dataclass(frozen=True)
class Window:
    """An inclusive range of Buchungstage. An absent lower bound means no lower bound."""

    since: date | None
    until: date

    def contains(self, day: date) -> bool:
        if self.since is not None and day < self.since:
            return False
        return day <= self.until

    @property
    def next_since(self) -> date:
        """The lower bound to use next time, so no day falls between two runs."""
        return self.until + timedelta(days=1)


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
                )
            )
    return tuple(kept), tuple(excluded)
