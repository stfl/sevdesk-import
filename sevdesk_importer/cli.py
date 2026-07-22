"""Command line entry point.

Exit 0 for a clean run, 2 when output was produced but something needs attention,
1 when the tool refused to produce output rather than guess a number.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import NoReturn

from sevdesk_importer import __version__
from sevdesk_importer.conversion import convert
from sevdesk_importer.dates import today_in_vienna
from sevdesk_importer.formatting import CONVENTIONS, GERMAN
from sevdesk_importer.providers import StatementError, parse_statement
from sevdesk_importer.rates import RateUnavailable, load_series
from sevdesk_importer.report import (
    EXIT_CLEAN,
    EXIT_REFUSED,
    EXIT_WARNINGS,
    build_report,
    render_report,
    render_summary,
)
from sevdesk_importer.window import Window, apply_window
from sevdesk_importer.writer import render_csv

USAGE_ERROR = 1


class _Parser(argparse.ArgumentParser):
    """Usage errors exit 1, so exit 2 always means "converted, but check the warnings"."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise SystemExit(USAGE_ERROR)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return _run(args)
    except (StatementError, RateUnavailable) as error:
        print(f"sevdesk-importer: {error}", file=sys.stderr)
        return EXIT_REFUSED
    except OSError as error:
        print(f"sevdesk-importer: {error}", file=sys.stderr)
        return EXIT_REFUSED


def _run(args: argparse.Namespace) -> int:
    convention = CONVENTIONS[args.format]
    # Exports occasionally carry a byte-order mark; utf-8-sig reads them either way.
    statement = parse_statement(args.statement.read_text(encoding="utf-8-sig"))

    window = Window(args.since, args.until or today_in_vienna())
    if window.since is not None and window.since > window.until:
        print(
            f"sevdesk-importer: --since {window.since.isoformat()} is after "
            f"--until {window.until.isoformat()}.",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    kept, excluded = apply_window(statement.movements, window)
    # One request covers the whole span; nothing to price asks the ECB nothing.
    series = load_series([movement.booking_date for movement in kept])
    conversion = convert(kept, series, convention)

    warnings = list(conversion.warnings)
    warnings += [f"{drop.source_ref}: {drop.detail}" for drop in conversion.drops]
    if not conversion.bookings:
        warnings.append(
            "No rows were emitted. Check the import window against the statement's dates."
        )

    exit_code = EXIT_WARNINGS if warnings else EXIT_CLEAN
    args.out.write_text(render_csv(conversion.bookings, convention), encoding="utf-8", newline="")

    report = build_report(
        source=str(args.statement),
        output=str(args.out),
        statement=statement,
        conversion=conversion,
        excluded=excluded,
        window=window,
        convention_name=convention.name,
        warnings=warnings,
        exit_code=exit_code,
    )
    if args.report is not None:
        args.report.write_text(render_report(report), encoding="utf-8")

    # Under --output json stdout carries the report and nothing else, so an agent
    # can parse it whole.
    if args.output == "json":
        sys.stdout.write(render_report(report))
    else:
        print(render_summary(report))
    return exit_code


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _Parser(
        prog="sevdesk-importer",
        description=(
            "Convert a Wise or Revolut USD statement into a sevDesk-importable EUR CSV. "
            "One statement produces one output file for one sevDesk bank account."
        ),
    )
    parser.add_argument("statement", type=Path, help="Wise or Revolut USD statement export")
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        required=True,
        metavar="PATH",
        help="where to write the sevDesk CSV",
    )
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="what to print: a readable summary, or the full JSON report (default: text)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        metavar="PATH",
        help="also write the JSON run report to this file",
    )
    parser.add_argument(
        "--format",
        choices=sorted(CONVENTIONS),
        default=GERMAN.name,
        help="number and date convention, and the delimiter that follows from it",
    )
    parser.add_argument(
        "--since",
        type=_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="earliest settlement date to convert, inclusive (default: no lower bound)",
    )
    parser.add_argument(
        "--until",
        type=_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="latest settlement date to convert, inclusive (default: today in Vienna)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def _date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a date in YYYY-MM-DD form") from None


if __name__ == "__main__":
    raise SystemExit(main())
