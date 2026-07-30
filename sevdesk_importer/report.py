"""The machine-readable run report.

Every row that was dropped and why, every rate that was used and where it came
from, and every warning — so a run can be audited, and so an agent can act on the
outcome without parsing prose. It carries no timestamps, so identical input
produces an identical report.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from sevdesk_importer import __version__
from sevdesk_importer.conversion import Conversion
from sevdesk_importer.model import Booking, Drop, Statement
from sevdesk_importer.window import Window

EXIT_CLEAN = 0
EXIT_REFUSED = 1
EXIT_WARNINGS = 2


def build_report(
    *,
    source: str,
    output: str | None,
    statement: Statement,
    conversion: Conversion,
    excluded: Sequence[Drop],
    window: Window,
    convention_name: str,
    warnings: Sequence[str],
    exit_code: int,
) -> dict[str, Any]:
    drops = list(statement.drops) + list(conversion.drops)
    return {
        "tool": "sevdesk-importer",
        "version": __version__,
        "source": {"path": source, "provider": statement.provider},
        "output": output,
        "format": convention_name,
        "window": {
            "since": window.since.isoformat() if window.since else None,
            "until": window.until.isoformat() if window.until else None,
            "next_since": window.next_since.isoformat() if window.next_since else None,
        },
        "counts": {
            "rows_read": statement.rows_read,
            "bookings_emitted": len(conversion.bookings),
            "rows_dropped": len(drops),
            "rows_outside_window": len(excluded),
        },
        "emitted": [_booking(booking) for booking in conversion.bookings],
        "dropped": [_drop(drop) for drop in drops],
        "outside_window": [_drop(drop) for drop in excluded],
        "rates": [
            {
                "booking_date": rate.booking_date.isoformat(),
                "usd_to_eur": str(rate.usd_to_eur),
                "provenance": rate.provenance,
                "ecb_quote_usd_per_eur": str(rate.ecb_quote)
                if rate.ecb_quote is not None
                else None,
                "quote_date": rate.quote_date.isoformat() if rate.quote_date else None,
            }
            for rate in conversion.rates
        ],
        "totals": _totals(conversion.bookings),
        "warnings": list(warnings),
        "exit_code": exit_code,
    }


def render_report(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False) + "\n"


def render_summary(report: dict[str, Any]) -> str:
    """The same run, told briefly enough to read in a terminal."""
    counts = report["counts"]
    window = report["window"]
    totals = report["totals"]
    opens_at = window["since"] or "the start of the statement"
    lines = [
        f"Read {counts['rows_read']} rows from {report['source']['path']} "
        f"({report['source']['provider']})",
    ]
    if window["until"]:
        lines.append(f"Window {opens_at} to {window['until']}, both inclusive")
        emitted = _plural(counts["bookings_emitted"], "booking")
        lines.append(f"Emitted {emitted} to {report['output']}")
    else:
        lines.append(f"Window opens at {opens_at}; nothing has settled in it yet")
        lines.append("Emitted nothing, so no import was filed and the export was left in place")
    if report["dropped"]:
        reasons = Counter(drop["reason"] for drop in report["dropped"])
        detail = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
        lines.append(f"Dropped {_rows(counts['rows_dropped'])}: {detail}")
        lines += [f"  {_dropped_line(drop)}" for drop in report["dropped"]]
    if counts["rows_outside_window"]:
        lines.append(f"Outside the window: {_rows(counts['rows_outside_window'])}")
    lines.append(
        f"Moved {totals['usd_credited']} USD in, {totals['usd_debited']} USD out, "
        f"{totals['usd_net']} USD net"
    )
    lines.append(f"Fees {totals['usd_fees']} USD")
    lines.append(
        f"Booked {totals['eur_credited']} EUR in, {totals['eur_debited']} EUR out, "
        f"{totals['eur_net']} EUR net"
    )
    if window["next_since"]:
        lines.append(f"Next run resumes at --since {window['next_since']}")
    lines += [f"Warning: {warning}" for warning in report["warnings"]]
    return "\n".join(lines)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _rows(count: int) -> str:
    return _plural(count, "row")


def _dropped_line(drop: dict[str, str]) -> str:
    """One dropped row, named and valued, so nothing vanishes without a figure."""
    described = drop["description"] or drop["source_ref"]
    return f"{described} — {drop['amount']} {drop['currency']} — {drop['detail']}"


def _drop(drop: Drop) -> dict[str, str]:
    return {
        "source_ref": drop.source_ref,
        "reason": drop.reason,
        "detail": drop.detail,
        "description": drop.description,
        "amount": str(drop.amount),
        "currency": drop.currency,
    }


def _booking(booking: Booking) -> dict[str, Any]:
    """One emitted row, traceable to the source row and the dated rate that priced it."""
    return {
        "source_ref": booking.source_ref,
        "booking_date": booking.booking_date.isoformat(),
        "amount_usd": str(booking.amount_usd),
        "fee_usd": str(booking.fee_usd),
        "amount_eur": str(booking.amount_eur),
        "usd_to_eur": str(booking.rate.usd_to_eur),
        "rate_provenance": booking.rate.provenance,
        "rate_date": booking.rate.quote_date.isoformat() if booking.rate.quote_date else None,
    }


def _totals(bookings: Iterable[Booking]) -> dict[str, str]:
    """Booked sums in both currencies.

    When usd_net returns to zero but eur_net does not, the difference is realised FX
    gain or loss and belongs in Kursdifferenzen. This states the figures; posting the
    entry is a decision made in sevDesk.
    """
    rows = list(bookings)
    usd_in = sum((b.amount_usd for b in rows if b.amount_usd > 0), Decimal(0))
    usd_out = sum((-b.amount_usd for b in rows if b.amount_usd < 0), Decimal(0))
    eur_in = sum((b.amount_eur for b in rows if b.amount_eur > 0), Decimal(0))
    eur_out = sum((-b.amount_eur for b in rows if b.amount_eur < 0), Decimal(0))
    return {
        "usd_credited": str(usd_in),
        "usd_debited": str(usd_out),
        "usd_net": str(usd_in - usd_out),
        "usd_fees": str(sum((b.fee_usd for b in rows), Decimal(0))),
        "eur_credited": str(eur_in),
        "eur_debited": str(eur_out),
        "eur_net": str(eur_in - eur_out),
    }
