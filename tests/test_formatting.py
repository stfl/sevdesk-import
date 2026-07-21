"""Number, date and rate formatting — what the sevDesk wizard keys on."""

from datetime import date
from decimal import Decimal

import pytest

from sevdesk_importer.formatting import GERMAN, US, round_eur


class TestHalfUpRounding:
    """Python's default is banker's rounding, which under-states exact halves."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0.005", "0.01"),
            ("0.015", "0.02"),
            ("0.025", "0.03"),
            ("2.345", "2.35"),
            ("-0.005", "-0.01"),
            ("-2.345", "-2.35"),
        ],
    )
    def test_exact_halves_round_away_from_zero(self, raw: str, expected: str) -> None:
        assert round_eur(Decimal(raw)) == Decimal(expected)

    def test_below_half_rounds_down(self) -> None:
        assert round_eur(Decimal("0.004")) == Decimal("0.00")

    def test_result_always_carries_two_decimal_places(self) -> None:
        assert str(round_eur(Decimal("7"))) == "7.00"


class TestGermanConvention:
    def test_amount_uses_dot_thousands_and_comma_decimal(self) -> None:
        assert GERMAN.amount(Decimal("9876.54")) == "9.876,54"

    def test_amount_pads_to_two_decimals(self) -> None:
        assert GERMAN.amount(Decimal("6")) == "6,00"

    def test_amount_below_one_thousand_has_no_separator(self) -> None:
        assert GERMAN.amount(Decimal("321.09")) == "321,09"

    def test_amount_separates_every_thousands_group(self) -> None:
        assert GERMAN.amount(Decimal("1234567.89")) == "1.234.567,89"

    def test_date_omits_leading_zeros(self) -> None:
        assert GERMAN.date(date(2026, 3, 9)) == "9.3.2026"

    def test_delimiter_is_semicolon_so_decimal_commas_need_no_quoting(self) -> None:
        assert GERMAN.delimiter == ";"

    def test_rate_uses_six_decimals_with_a_comma(self) -> None:
        assert GERMAN.rate(Decimal("0.8765430000000000")) == "0,876543"


class TestUsConvention:
    def test_amount_uses_dot_decimal_and_no_thousands_separator(self) -> None:
        """A thousands comma would collide with the comma delimiter."""
        assert US.amount(Decimal("9876.54")) == "9876.54"

    def test_date_is_iso(self) -> None:
        assert US.date(date(2026, 3, 9)) == "2026-03-09"

    def test_delimiter_is_comma(self) -> None:
        assert US.delimiter == ","

    def test_rate_uses_six_decimals_with_a_dot(self) -> None:
        assert US.rate(Decimal("0.8765430000000000")) == "0.876543"


class TestRateDisplayRounding:
    def test_rate_display_rounds_half_up(self) -> None:
        assert US.rate(Decimal("0.8768095")) == "0.876810"

    def test_rate_display_pads_short_rates(self) -> None:
        assert US.rate(Decimal("1")) == "1.000000"
