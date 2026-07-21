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
    output: str,
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
            "until": window.until.isoformat(),
            "next_since": window.next_since.isoformat(),
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
    lines = [
        f"Read {counts['rows_read']} rows from {report['source']['path']} "
        f"({report['source']['provider']})",
        f"Window {window['since'] or 'start of statement'} to {window['until']}, both inclusive",
        f"Emitted {counts['bookings_emitted']} bookings to {report['output']}",
    ]
    if report["dropped"]:
        reasons = Counter(drop["reason"] for drop in report["dropped"])
        detail = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
        lines.append(f"Dropped {counts['rows_dropped']} rows: {detail}")
    if counts["rows_outside_window"]:
        lines.append(f"Outside the window: {counts['rows_outside_window']} rows")
    lines.append(
        f"Booked {totals['eur_credited']} EUR credited, {totals['eur_debited']} EUR debited "
        f"({totals['usd_net']} USD net)"
    )
    lines.append(f"Next run: --since {window['next_since']}")
    lines += [f"Warning: {warning}" for warning in report["warnings"]]
    return "\n".join(lines)


def _drop(drop: Drop) -> dict[str, str]:
    return {"source_ref": drop.source_ref, "reason": drop.reason, "detail": drop.detail}


def _booking(booking: Booking) -> dict[str, Any]:
    """One emitted row, traceable to the source row and the dated rate that priced it."""
    return {
        "source_ref": booking.source_ref,
        "booking_date": booking.booking_date.isoformat(),
        "is_fee": booking.is_fee,
        "amount_usd": str(booking.amount_usd),
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
        "eur_credited": str(eur_in),
        "eur_debited": str(eur_out),
        "eur_net": str(eur_in - eur_out),
    }
