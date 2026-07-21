"""Settlement timestamps to Buchungstag.

Providers export on different clocks: Wise stamps UTC, Revolut stamps Europe/Vienna
local time. Both are normalised to the Vienna calendar day, because that is the day
the booking belongs to — and a transaction near midnight at a month boundary would
otherwise land in the wrong VAT period.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")
VIENNA = ZoneInfo("Europe/Vienna")


def vienna_date_from_utc(stamp: datetime) -> date:
    """Vienna calendar day of a naive UTC timestamp.

    zoneinfo applies the correct offset for the date, so the March and October
    daylight-saving changes need no special handling.
    """
    return stamp.replace(tzinfo=UTC).astimezone(VIENNA).date()


def vienna_date_from_local(stamp: datetime) -> date:
    """Vienna calendar day of a timestamp already written in Vienna local time."""
    return stamp.date()


def today_in_vienna() -> date:
    """Default upper bound of the import window."""
    return datetime.now(tz=VIENNA).date()
