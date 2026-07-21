"""Number and date conventions for the sevDesk import wizard.

The field delimiter follows the number format. German decimals are commas, so the
delimiter is a semicolon; US decimals are dots, so the delimiter is a comma and
thousands are not grouped. Either way no amount ever needs quoting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
RATE_PLACES = Decimal("0.000001")


def round_eur(value: Decimal) -> Decimal:
    """Round to cents, ties away from zero.

    Decimal's default is banker's rounding, which turns 0.005 into 0.00 and would
    systematically under-state every exact half.
    """
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _round_rate(value: Decimal) -> Decimal:
    return value.quantize(RATE_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Convention:
    """One set of number, date and delimiter conventions."""

    name: str
    delimiter: str
    decimal_point: str
    thousands: str
    iso_dates: bool

    def amount(self, value: Decimal) -> str:
        """A EUR amount with exactly two decimal places."""
        return self._render(round_eur(value), group=True)

    def rate(self, value: Decimal) -> str:
        """A USD-to-EUR factor at six decimal places, never grouped."""
        return self._render(_round_rate(value), group=False)

    def date(self, value: date) -> str:
        if self.iso_dates:
            return value.isoformat()
        return f"{value.day}.{value.month}.{value.year}"

    def _render(self, value: Decimal, *, group: bool) -> str:
        sign = "-" if value < 0 else ""
        whole, _, fraction = format(abs(value), "f").partition(".")
        if group and self.thousands:
            whole = self._group(whole)
        return f"{sign}{whole}{self.decimal_point}{fraction}"

    def _group(self, digits: str) -> str:
        groups = []
        while len(digits) > 3:
            groups.append(digits[-3:])
            digits = digits[:-3]
        groups.append(digits)
        return self.thousands.join(reversed(groups))


GERMAN = Convention(
    name="german",
    delimiter=";",
    decimal_point=",",
    thousands=".",
    iso_dates=False,
)

US = Convention(
    name="us",
    delimiter=",",
    decimal_point=".",
    thousands="",
    iso_dates=True,
)

CONVENTIONS = {convention.name: convention for convention in (GERMAN, US)}
