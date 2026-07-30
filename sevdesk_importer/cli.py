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
from typing import Any, NoReturn

from sevdesk_importer import __version__
from sevdesk_importer.conversion import Conversion, convert
from sevdesk_importer.formatting import CONVENTIONS, GERMAN
from sevdesk_importer.model import Drop, Statement
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
from sevdesk_importer.runs import IN_NAME, RunError, commit, provider_root, resume_since
from sevdesk_importer.window import UndatableSettlement, Window, apply_window, settlement_ceiling
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
    except (StatementError, RateUnavailable, UndatableSettlement, RunError) as error:
        print(f"sevdesk-importer: {error}", file=sys.stderr)
        return EXIT_REFUSED
    except OSError as error:
        print(f"sevdesk-importer: {error}", file=sys.stderr)
        return EXIT_REFUSED


def _run(args: argparse.Namespace) -> int:
    convention = CONVENTIONS[args.format]
    # Exports occasionally carry a byte-order mark; utf-8-sig reads them either way.
    statement = parse_statement(args.statement.read_text(encoding="utf-8-sig"))

    root: Path = args.run_dir
    provider_root(root, statement.provider).mkdir(parents=True, exist_ok=True)

    since = args.since if args.since is not None else resume_since(root, statement.provider)
    # Stop short of anything that could still settle, so a slow transaction is never
    # stepped over. Rows behind it wait for the run that can see them settled.
    scanned = Window(since, settlement_ceiling(statement.unsettled))

    kept, excluded = apply_window(statement.movements, scanned)
    # One request covers the whole span; nothing to price asks the ECB nothing.
    series = load_series([movement.booking_date for movement in kept])
    conversion = convert(kept, series, convention)

    warnings = list(conversion.warnings)
    warnings += [f"{drop.source_ref}: {drop.detail}" for drop in conversion.drops]

    if not conversion.bookings:
        return _nothing_new(
            args, statement, conversion, excluded, scanned, convention.name, warnings
        )

    # The upper bound is what was actually booked, never what was merely asked for, so
    # the next run resumes from a day this one can prove it covered.
    until = max(booking.booking_date for booking in conversion.bookings)
    written = commit(
        root=root,
        provider=statement.provider,
        until=until,
        statement=args.statement,
        csv_text=render_csv(conversion.bookings, convention),
    )

    exit_code = EXIT_WARNINGS if warnings else EXIT_CLEAN
    report = build_report(
        source=str((written.parent / IN_NAME).resolve()),
        output=str(written.resolve()),
        statement=statement,
        conversion=conversion,
        excluded=excluded,
        window=Window(since, until),
        convention_name=convention.name,
        warnings=warnings,
        exit_code=exit_code,
    )
    _emit(args, report)
    return exit_code


def _nothing_new(
    args: argparse.Namespace,
    statement: Statement,
    conversion: Conversion,
    excluded: Sequence[Drop],
    scanned: Window,
    convention_name: str,
    warnings: list[str],
) -> int:
    """Nothing settled that an earlier run had not already covered.

    No directory is claimed, the export stays where it was, and the resume pointer is
    left alone — so running again once more has settled costs nothing and skips nothing.
    """
    reached = scanned.since.isoformat() if scanned.since else "the start of the statement"
    warnings.append(f"No rows were emitted. Nothing new has settled since {reached}.")
    report = build_report(
        source=str(args.statement),
        output=None,
        statement=statement,
        conversion=conversion,
        excluded=excluded,
        window=Window(scanned.since, None),
        convention_name=convention_name,
        warnings=warnings,
        exit_code=EXIT_WARNINGS,
    )
    _emit(args, report)
    return EXIT_WARNINGS


def _emit(args: argparse.Namespace, report: dict[str, Any]) -> None:
    if args.report is not None:
        args.report.write_text(render_report(report), encoding="utf-8")
    # Under --output json stdout carries the report and nothing else, so an agent
    # can parse it whole.
    if args.output == "json":
        sys.stdout.write(render_report(report))
    else:
        print(render_summary(report))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _Parser(
        prog="sevdesk-importer",
        description=(
            "Convert a Wise or Revolut USD statement into a sevDesk-importable EUR CSV. "
            "One statement produces one output file for one sevDesk bank account, filed "
            "under <run-dir>/<provider>/<last day booked>/ and resumed from there next time."
        ),
    )
    parser.add_argument("statement", type=Path, help="Wise or Revolut USD statement export")
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path("run"),
        metavar="PATH",
        help="where imports are filed and resumed from (default: run)",
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
        help=(
            "earliest settlement date to convert, inclusive "
            "(default: the day after the last import of this provider)"
        ),
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
