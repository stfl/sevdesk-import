"""Settlement timestamps normalise to the Europe/Vienna calendar day.

Wise exports UTC, Revolut exports Vienna local time. A booking that lands on the
wrong calendar day lands in the wrong VAT period.
"""

from datetime import date, datetime

from sevdesk_importer.dates import vienna_date_from_local, vienna_date_from_utc


class TestUtcToVienna:
    def test_midday_stays_on_the_same_day(self) -> None:
        assert vienna_date_from_utc(datetime(2026, 7, 14, 11, 20, 53)) == date(2026, 7, 14)

    def test_late_evening_utc_belongs_to_the_next_vienna_day(self) -> None:
        """22:02 UTC in July is 00:02 the following day in Vienna."""
        assert vienna_date_from_utc(datetime(2026, 7, 14, 22, 2, 52)) == date(2026, 7, 15)

    def test_summer_offset_is_two_hours(self) -> None:
        assert vienna_date_from_utc(datetime(2026, 7, 31, 22, 30, 0)) == date(2026, 8, 1)

    def test_winter_offset_is_one_hour(self) -> None:
        """At CET the same wall time does not cross midnight."""
        assert vienna_date_from_utc(datetime(2026, 1, 31, 22, 30, 0)) == date(2026, 1, 31)
        assert vienna_date_from_utc(datetime(2026, 1, 31, 23, 30, 0)) == date(2026, 2, 1)

    def test_month_boundary_moves_the_vat_period(self) -> None:
        assert vienna_date_from_utc(datetime(2026, 6, 30, 23, 15, 0)) == date(2026, 7, 1)

    def test_spring_forward_transition(self) -> None:
        """2026-03-29 01:00 UTC is when Vienna jumps from 02:00 to 03:00."""
        assert vienna_date_from_utc(datetime(2026, 3, 28, 23, 30, 0)) == date(2026, 3, 29)
        assert vienna_date_from_utc(datetime(2026, 3, 29, 22, 30, 0)) == date(2026, 3, 30)

    def test_autumn_fall_back_transition(self) -> None:
        """2026-10-25 01:00 UTC is when Vienna drops from 03:00 back to 02:00."""
        assert vienna_date_from_utc(datetime(2026, 10, 24, 22, 30, 0)) == date(2026, 10, 25)
        assert vienna_date_from_utc(datetime(2026, 10, 25, 22, 30, 0)) == date(2026, 10, 25)
        assert vienna_date_from_utc(datetime(2026, 10, 25, 23, 30, 0)) == date(2026, 10, 26)


class TestLocalVienna:
    def test_local_timestamps_keep_their_calendar_day(self) -> None:
        assert vienna_date_from_local(datetime(2026, 5, 28, 10, 30, 31)) == date(2026, 5, 28)

    def test_late_local_evening_does_not_shift(self) -> None:
        assert vienna_date_from_local(datetime(2026, 5, 28, 23, 59, 59)) == date(2026, 5, 28)
