"""Pricing balance movements into EUR bookings.

Each movement becomes exactly one booking, priced at the rate of its own
Buchungstag, with any bank fee folded into the amount. That is the line the bank
statement itself shows -- a card payment of 32.00 plus 0.12 of fee left the
account as a single movement of 32.12 -- so the booking reconciles against the
provider's own balance column without arithmetic.

The fee does not disappear: the Verwendungszweck names it and the run report
records it per booking.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sevdesk_importer.formatting import Convention, round_eur
from sevdesk_importer.model import Booking, Drop, Movement
from sevdesk_importer.rates import EcbSeries, Rate, resolve


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
    """Price every movement.

    Output order is settlement date, then the row's position in the export. That
    ordering is what makes re-running and diffing a valid correctness check.
    """
    bookings: list[Booking] = []
    drops: list[Drop] = []
    used: dict[tuple[object, ...], Rate] = {}

    for movement in sorted(movements, key=lambda m: (m.booking_date, m.order)):
        rate = resolve(movement.booking_date, movement.recorded_rate, series)

        booking = _price(movement, rate, convention)
        if booking is None:
            drops.append(
                Drop(
                    movement.source_ref,
                    "rounds_to_zero",
                    "worth less than half a cent at this rate",
                    movement.lead,
                    movement.amount_usd - movement.fee_usd,
                    "USD",
                )
            )
            continue

        bookings.append(booking)
        used.setdefault(rate.identity, rate)

    rates = tuple(sorted(used.values(), key=lambda r: r.identity))
    warnings = tuple(rate.warning for rate in rates if rate.is_substituted)
    return Conversion(tuple(bookings), rates, tuple(drops), warnings)


def _price(movement: Movement, rate: Rate, convention: Convention) -> Booking | None:
    """One priced row, or nothing when it is worth less than half a cent."""
    # A fee is always a deduction: on an outgoing row it adds to what left the
    # account, on an incoming one it reduces what arrived. Either way the account
    # moved by the difference.
    amount_usd = movement.amount_usd - movement.fee_usd
    amount_eur = round_eur(amount_usd * rate.usd_to_eur)
    if amount_eur == 0:
        return None

    return Booking(
        name=movement.name,
        purpose=_purpose(movement, amount_usd, rate, convention),
        booking_date=movement.booking_date,
        amount_usd=amount_usd,
        fee_usd=movement.fee_usd,
        amount_eur=amount_eur,
        rate=rate,
        source_ref=movement.source_ref,
    )


def _purpose(movement: Movement, amount_usd: Decimal, rate: Rate, convention: Convention) -> str:
    """The description, the FX detail needed to verify the EUR figure, and the fee."""
    parts = [
        movement.lead,
        f"{convention.amount(abs(amount_usd))} USD zu Kurs {convention.rate(rate.usd_to_eur)}",
    ]
    if movement.fee_usd:
        # On a debit the fee is part of what left the account; on a credit it was
        # taken before the money arrived, so it is not part of what landed.
        preposition = "davon" if amount_usd < 0 else "abzgl."
        parts.append(f"{preposition} {convention.amount(movement.fee_usd)} USD Gebühr")
    return " | ".join(parts)
