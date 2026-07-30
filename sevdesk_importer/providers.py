"""Reading Wise and Revolut exports into provider-neutral balance movements.

The two exports share nothing: different columns, different clocks, different sign
conventions, and a different idea of what a fee is. Everything provider-specific
lives here, so the rest of the program sees only `Movement`.

A row is kept only if it moved *this* account's USD balance. For Wise that means
OUT with source currency USD, or IN with target currency USD, which is exactly what
excludes the EUR-funded leg of a split-currency card payment — sevDesk already holds
that leg from the Wise EUR auto-import. For Revolut it means any settled USD row.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sevdesk_importer.dates import vienna_date_from_local, vienna_date_from_utc
from sevdesk_importer.formatting import round_eur
from sevdesk_importer.model import Drop, Movement, Statement, Unsettled

#: Wise encodes the transaction type as the prefix of its identifier. The mapping is
#: also the whitelist: a type that is not here has no known booking phrasing, and is
#: refused rather than guessed at.
WISE_NOUNS = {
    "CARD_TRANSACTION": "Kartenzahlung",
    "TRANSFER": "Überweisung",
    "BALANCE_CASHBACK": "Cashback",
    "ACCRUAL_CHARGE": "Kontoführungsgebühr",
}

#: Revolut types observed in real exports. An unfamiliar one is refused.
REVOLUT_TYPES = frozenset({"TOPUP", "EXCHANGE", "TRANSFER", "MERCHANT_PAYMENT"})

ACCOUNT_CURRENCY = "USD"

#: Wise leaves its own side of an internal row blank. Falling back to the provider
#: keeps the Name column filled without ever naming the account holder.
WISE_PROVIDER_NAME = "Wise"


class StatementError(Exception):
    """The export could not be read as written."""


class UnknownTransactionType(StatementError):
    """A transaction type with no known booking rule. Refused, never guessed."""


class MalformedStatement(StatementError):
    """A column is missing, or a value is not what the schema promises."""


def parse_statement(payload: str) -> Statement:
    """Read an export, detecting the provider from its columns."""
    reader = csv.DictReader(io.StringIO(payload))
    fieldnames = reader.fieldnames or []
    provider = detect_provider(fieldnames)
    rows = list(reader)
    if provider == "wise":
        return _parse_wise(rows)
    return _parse_revolut(rows)


def detect_provider(fieldnames: Sequence[str]) -> str:
    fields = {name.strip() for name in fieldnames}
    if {"Finished on", "Source currency"} <= fields:
        return "wise"
    if {"Completed Date", "State"} <= fields:
        return "revolut"
    raise MalformedStatement(
        "Unrecognised statement: expected either a Wise export (with 'Finished on') "
        f"or a Revolut export (with 'Completed Date'), got columns {sorted(fields)}."
    )


# --------------------------------------------------------------------------- Wise


def _parse_wise(rows: list[dict[str, str]]) -> Statement:
    invoice_totals = _invoice_totals(rows)
    movements: list[Movement] = []
    drops: list[Drop] = []
    unsettled: list[Unsettled] = []

    for order, row in enumerate(rows):
        identifier = _text(row, "ID")
        wise_type = identifier.split("-", 1)[0].upper()
        if wise_type not in WISE_NOUNS:
            raise UnknownTransactionType(
                f"Wise transaction type {wise_type!r} has no booking rule (row {identifier!r}). "
                "Add it to WISE_NOUNS once its direction and fee behaviour are known."
            )

        direction = _text(row, "Direction").upper()
        if direction not in {"IN", "OUT"}:
            raise MalformedStatement(
                f"Row {identifier!r} has direction {direction!r}, expected IN or OUT."
            )

        settled_at = _text(row, "Finished on")
        status = _text(row, "Status").upper()
        incoming = direction == "IN"
        if status != "COMPLETED" or not settled_at:
            drops.append(
                Drop(
                    identifier,
                    "not_settled",
                    f"status {status or 'missing'}",
                    *_wise_drop_value(row, incoming=incoming),
                )
            )
            if _moves_usd_when_settled(row, incoming=incoming):
                unsettled.append(
                    Unsettled(identifier, status, _initiated_on(row, "Created on", identifier))
                )
            continue

        source_currency = _text(row, "Source currency").upper()
        target_currency = _text(row, "Target currency").upper()
        if direction == "OUT" and source_currency != ACCOUNT_CURRENCY:
            drops.append(
                Drop(
                    identifier,
                    "funded_from_other_currency",
                    f"funded from the {source_currency} balance, which sevDesk imports separately",
                    *_wise_drop_value(row, incoming=incoming),
                )
            )
            continue
        if direction == "IN" and target_currency != ACCOUNT_CURRENCY:
            drops.append(
                Drop(
                    identifier,
                    "credited_to_other_currency",
                    f"credited to the {target_currency} balance",
                    *_wise_drop_value(row, incoming=incoming),
                )
            )
            continue

        fee_usd = _wise_fee(row)
        counterparty = _wise_counterparty(row, incoming=incoming)

        if incoming:
            # The fee was deducted before the money landed; book revenue gross.
            amount_usd = _amount(row, "Target amount (after fees)") + fee_usd
        else:
            # The fee sits on top of what reached the counterparty.
            amount_usd = -_amount(row, "Source amount (after fees)")

        movements.append(
            Movement(
                source_ref=identifier,
                booking_date=vienna_date_from_utc(_timestamp(settled_at, identifier)),
                name=counterparty,
                lead=_wise_lead(
                    row,
                    wise_type=wise_type,
                    incoming=incoming,
                    counterparty=counterparty,
                    invoice_total=invoice_totals.get((identifier, target_currency)),
                ),
                amount_usd=amount_usd,
                fee_usd=fee_usd,
                recorded_rate=_wise_recorded_rate(row, source_currency, target_currency),
                order=order,
            )
        )

    return Statement("wise", len(rows), tuple(movements), tuple(drops), tuple(unsettled))


def _moves_usd_when_settled(row: dict[str, str], *, incoming: bool) -> bool:
    """Whether an unsettled Wise row would touch this balance once it lands.

    The same side-of-the-trade test that decides whether a settled row is kept, asked
    of a row that has not settled: a pending EUR transfer is no reason to hold the USD
    window back.
    """
    side = "Target" if incoming else "Source"
    return _text(row, f"{side} currency").upper() == ACCOUNT_CURRENCY


def _wise_drop_value(row: dict[str, str], *, incoming: bool) -> tuple[str, Decimal, str]:
    """What a dropped row was worth, described and in its own currency."""
    side = "Target" if incoming else "Source"
    return (
        _wise_counterparty(row, incoming=incoming),
        _amount(row, f"{side} amount (after fees)"),
        _text(row, f"{side} currency").upper(),
    )


def _invoice_totals(rows: list[dict[str, str]]) -> dict[tuple[str, str], Decimal]:
    """What the merchant charged, summed across every leg of one card payment.

    A split-currency card payment appears as two rows under one identifier, each
    funded from a different balance. Their target amounts sum to the invoice total,
    which is the only token the two legs share once they reach sevDesk.
    """
    totals: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        key = (_text(row, "ID"), _text(row, "Target currency").upper())
        totals[key] = totals.get(key, Decimal(0)) + _amount(row, "Target amount (after fees)")
    return totals


def _wise_fee(row: dict[str, str]) -> Decimal:
    """Only fees denominated in USD moved this balance.

    Wise omits the fee columns entirely on some exports, so they are read leniently:
    a missing column means the row carried no fee.
    """
    fee = Decimal(0)
    if _optional_text(row, "Source fee currency").upper() == ACCOUNT_CURRENCY:
        fee += _amount(row, "Source fee amount")
    if _optional_text(row, "Target fee currency").upper() == ACCOUNT_CURRENCY:
        fee += _amount(row, "Target fee amount")
    return fee


def _wise_counterparty(row: dict[str, str], *, incoming: bool) -> str:
    """The other party: the sender on incoming rows, the recipient on outgoing ones.

    Wise leaves the account holder's own side blank on internal rows, so reading the
    correct side is what keeps the Name column from ever being empty.
    """
    name = _text(row, "Source name") if incoming else _text(row, "Target name")
    return name or WISE_PROVIDER_NAME


def _wise_recorded_rate(row: dict[str, str], source: str, target: str) -> Decimal | None:
    """The USD-to-EUR factor the export recorded, when it recorded a real conversion.

    A rate between two USD legs is 1.0 and prices nothing; a rate against a third
    currency prices the wrong pair. Only a genuine USD/EUR conversion is used.
    """
    raw = _optional_amount(row, "Exchange rate")
    if raw is None or raw <= 0:
        return None
    if source == ACCOUNT_CURRENCY and target == "EUR":
        return raw
    if source == "EUR" and target == ACCOUNT_CURRENCY:
        return Decimal(1) / raw
    return None


def _wise_lead(
    row: dict[str, str],
    *,
    wise_type: str,
    incoming: bool,
    counterparty: str,
    invoice_total: Decimal | None,
) -> str:
    """The Verwendungszweck before its FX detail is appended."""
    source_currency = _text(row, "Source currency").upper()
    target_currency = _text(row, "Target currency").upper()
    converted_purchase = (
        wise_type == "CARD_TRANSACTION"
        and target_currency != ACCOUNT_CURRENCY
        and source_currency != target_currency
        and invoice_total is not None
    )

    if converted_purchase:
        assert invoice_total is not None
        # sevDesk's own Wise auto-import writes the total with a decimal point; the
        # same string on both legs is what lets one search reunite them on one Beleg.
        total = format(round_eur(invoice_total), "f")
        lead = f"Card transaction of {total} {target_currency} issued by {counterparty}"
    else:
        preposition = "von" if incoming else "an"
        lead = f"{WISE_NOUNS[wise_type]} {preposition} {counterparty}"

    reference = _text(row, "Reference")
    return f"{lead} ({reference})" if reference else lead


# ------------------------------------------------------------------------ Revolut


def _parse_revolut(rows: list[dict[str, str]]) -> Statement:
    movements: list[Movement] = []
    drops: list[Drop] = []
    unsettled: list[Unsettled] = []

    for order, row in enumerate(rows):
        written_type = _text(row, "Type")
        revolut_type = written_type.upper()
        completed_at = _text(row, "Completed Date")
        description = _text(row, "Description")
        reference = (
            f"{completed_at or _text(row, 'Started Date')} {written_type} {description}".strip()
        )

        if revolut_type not in REVOLUT_TYPES:
            raise UnknownTransactionType(
                f"Revolut transaction type {written_type!r} has no booking rule (row {reference!r}). "
                "Add it to REVOLUT_TYPES once its sign and fee behaviour are known."
            )

        state = _text(row, "State").upper()
        if state != "COMPLETED" or not completed_at:
            drops.append(
                Drop(
                    reference,
                    "not_settled",
                    f"state {state or 'missing'}",
                    description,
                    _amount(row, "Amount"),
                    _text(row, "Currency").upper(),
                )
            )
            if _text(row, "Currency").upper() == ACCOUNT_CURRENCY:
                unsettled.append(
                    Unsettled(reference, state, _initiated_on(row, "Started Date", reference))
                )
            continue

        currency = _text(row, "Currency").upper()
        if currency != ACCOUNT_CURRENCY:
            drops.append(
                Drop(
                    reference,
                    "other_currency",
                    f"denominated in {currency}",
                    description,
                    _amount(row, "Amount"),
                    currency,
                )
            )
            continue

        # Revolut signs Amount and always deducts Fee, so the balance moved by
        # Amount minus Fee.
        amount_usd = _amount(row, "Amount")
        fee_usd = _amount(row, "Fee")
        if amount_usd == 0 and fee_usd == 0:
            drops.append(
                Drop(
                    reference,
                    "no_balance_movement",
                    "amount and fee are both zero",
                    description,
                    Decimal(0),
                    currency,
                )
            )
            continue

        movements.append(
            Movement(
                source_ref=reference,
                booking_date=vienna_date_from_local(_timestamp(completed_at, reference)),
                name="",
                lead=description or revolut_type.title(),
                amount_usd=amount_usd,
                fee_usd=fee_usd,
                recorded_rate=None,
                order=order,
            )
        )

    return Statement("revolut", len(rows), tuple(movements), tuple(drops), tuple(unsettled))


# ------------------------------------------------------------------------- Shared


def _initiated_on(row: dict[str, str], column: str, reference: str) -> date | None:
    """The day the provider first saw a row, as the Vienna calendar day.

    Read leniently: an unsettled row with no initiation date is a case the window
    decides about, not one parsing can refuse on its own, because a row in a state
    that can never settle needs no date at all.
    """
    raw = _optional_text(row, column)
    if not raw:
        return None
    stamp = _timestamp(raw, reference)
    # Wise stamps UTC and Revolut Vienna local, the same split as the settlement
    # columns these sit beside.
    if column == "Created on":
        return vienna_date_from_utc(stamp)
    return vienna_date_from_local(stamp)


def _text(row: dict[str, str], column: str) -> str:
    """A column the schema must carry."""
    if column not in row:
        raise MalformedStatement(f"Statement has no {column!r} column.")
    return (row[column] or "").strip()


def _optional_text(row: dict[str, str], column: str) -> str:
    """A column the export may omit altogether."""
    return (row.get(column) or "").strip()


def _amount(row: dict[str, str], column: str) -> Decimal:
    """A monetary column. An absent or empty column means zero."""
    return _optional_amount(row, column) or Decimal(0)


def _optional_amount(row: dict[str, str], column: str) -> Decimal | None:
    raw = (row.get(column) or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation as error:
        raise MalformedStatement(
            f"Column {column!r} holds {raw!r}, which is not a number."
        ) from error


def _timestamp(raw: str, reference: str) -> datetime:
    try:
        return datetime.fromisoformat(raw)
    except ValueError as error:
        raise MalformedStatement(
            f"Row {reference!r} has timestamp {raw!r}, which is not a date-time."
        ) from error
