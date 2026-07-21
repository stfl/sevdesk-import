"""Pricing balance movements into EUR bookings.

Each movement is priced at the rate of its own Buchungstag, and any fee it carries
becomes a second booking at that same date and rate — so the fee stays visible as
deductible Bankspesen while the two rows still sum to the balance movement the
provider's own statement shows.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sevdesk_importer.formatting import Convention, round_eur
from sevdesk_importer.model import Booking, Drop, Movement
from sevdesk_importer.rates import EcbSeries, Rate, resolve

FEE_PREFIX = "Gebühr zu:"


@dataclass(frozen=True)
class Conversion:
    """Everything one pricing pass produced, including what it refused to price."""

    bookings: tuple[Booking, ...]
    rates: tuple[Rate, ...]
    drops: tuple[Drop, ...]
    warnings: tuple[str, ...]


def convert(
    movements: tuple[Movement, ...], series: EcbSeries, convention: Convention
) -> Conversion:
    """Price every movement, splitting fees into their own bookings.

    Output order is settlement date, then the row's position in the export, with a
    fee row immediately after the row it belongs to. That ordering is what makes
    re-running and diffing a valid correctness check.
    """
    bookings: list[Booking] = []
    drops: list[Drop] = []
    used: dict[tuple[object, ...], Rate] = {}

    for movement in sorted(movements, key=lambda m: (m.booking_date, m.order)):
        rate = resolve(movement.booking_date, movement.recorded_rate, series)

        for row in _rows_of(movement):
            booking = _price(row, movement, rate, convention)
            if booking is None:
                drops.append(
                    Drop(
                        movement.source_ref,
                        "rounds_to_zero",
                        f"{'the fee of' if row.is_fee else ''} {abs(row.amount_usd)} USD is "
                        "worth less than half a cent at this rate".strip(),
                    )
                )
                continue
            bookings.append(booking)
            used.setdefault(rate.identity, rate)

    rates = tuple(sorted(used.values(), key=lambda r: r.identity))
    warnings = tuple(rate.warning for rate in rates if rate.is_substituted)
    return Conversion(tuple(bookings), rates, tuple(drops), warnings)


@dataclass(frozen=True)
class _Row:
    """One row a movement becomes, before it is priced."""

    name: str
    lead: str
    amount_usd: Decimal
    is_fee: bool


def _rows_of(movement: Movement) -> list[_Row]:
    """The one or two rows a movement becomes.

    A movement consisting only of a fee yields the fee row alone; no zero-value main
    row is ever written.
    """
    rows: list[_Row] = []
    if movement.amount_usd != 0:
        rows.append(_Row(movement.name, movement.lead, movement.amount_usd, is_fee=False))
    if movement.fee_usd != 0:
        # A fee is always a debit: on an outgoing row it adds to what left the
        # account, on an incoming one it offsets what arrived.
        rows.append(
            _Row(
                movement.fee_name,
                f"{FEE_PREFIX} {movement.lead}",
                -movement.fee_usd,
                is_fee=True,
            )
        )
    return rows


def _price(row: _Row, movement: Movement, rate: Rate, convention: Convention) -> Booking | None:
    """One priced row, or nothing when it is worth less than half a cent."""
    amount_eur = round_eur(row.amount_usd * rate.usd_to_eur)
    if amount_eur == 0:
        return None
    return Booking(
        name=row.name,
        purpose=_purpose(row.lead, row.amount_usd, rate, convention),
        booking_date=movement.booking_date,
        amount_usd=row.amount_usd,
        amount_eur=amount_eur,
        rate=rate,
        source_ref=movement.source_ref,
        is_fee=row.is_fee,
    )


def _purpose(lead: str, amount_usd: Decimal, rate: Rate, convention: Convention) -> str:
    """The description, then the FX detail needed to verify the EUR figure."""
    original = convention.amount(abs(amount_usd))
    return f"{lead} | {original} USD zu Kurs {convention.rate(rate.usd_to_eur)}"
