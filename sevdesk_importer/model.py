"""The records this converter passes between its stages.

A `Movement` is one provider row that moved this account's USD balance, already
normalised across providers. A `Booking` is one row of the sevDesk CSV, priced in
EUR. Fees become their own `Booking`, which is why one `Movement` can produce two.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sevdesk_importer.rates import Rate


@dataclass(frozen=True)
class Movement:
    """One provider row that moved this account's USD balance.

    `amount_usd` is signed and gross: positive credits the account, negative debits
    it, and it excludes `fee_usd`. `fee_usd` is never negative — a fee is always a
    debit — so the real balance movement of the row is `amount_usd - fee_usd`.
    """

    source_ref: str
    booking_date: date
    name: str
    lead: str
    amount_usd: Decimal
    fee_usd: Decimal
    fee_name: str
    recorded_rate: Decimal | None
    order: int

    @property
    def balance_movement_usd(self) -> Decimal:
        """What the provider's own balance column moved by."""
        return self.amount_usd - self.fee_usd


@dataclass(frozen=True)
class Drop:
    """A row that was read but not emitted, and why. Nothing disappears silently."""

    source_ref: str
    reason: str
    detail: str


@dataclass(frozen=True)
class Statement:
    """One provider export, parsed."""

    provider: str
    rows_read: int
    movements: tuple[Movement, ...]
    drops: tuple[Drop, ...]


@dataclass(frozen=True)
class Booking:
    """One row of the sevDesk CSV.

    `amount_eur` is signed and already rounded — rounding happens once, here, so
    serialization only ever formats digits.
    """

    name: str
    purpose: str
    booking_date: date
    amount_usd: Decimal
    amount_eur: Decimal
    rate: Rate
    source_ref: str
    is_fee: bool

    @property
    def gutschrift(self) -> Decimal | None:
        return self.amount_eur if self.amount_eur > 0 else None

    @property
    def belastung(self) -> Decimal | None:
        return -self.amount_eur if self.amount_eur < 0 else None
