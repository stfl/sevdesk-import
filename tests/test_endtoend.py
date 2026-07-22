"""The written file, and the command line contract around it.

Exactly one test here asserts on the *shape* of the written bytes, and it covers
serialization only — header names, column order, delimiter, quoting and decimal
separator, which are what the sevDesk wizard keys on and what no assertion over
records would catch. One further test reads the file only to check that two runs
agree, pinning no part of the format. Everything else asserts on what the command
prints, or on the JSON report.
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

import pytest

from sevdesk_importer.cli import main

GERMAN_AMOUNT = re.compile(r"^-?\d{1,3}(\.\d{3})*,\d{2}$")
GERMAN_DATE = re.compile(r"^[1-9]\d?\.[1-9]\d?\.\d{4}$")
US_AMOUNT = re.compile(r"^-?\d+\.\d{2}$")
US_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def run(statement: Path, tmp_path: Path, *extra: str) -> tuple[int, Path, dict[str, Any]]:
    output = tmp_path / "sevdesk.csv"
    report = tmp_path / "report.json"
    code = main([str(statement), "-o", str(output), "--report", str(report), *extra])
    return code, output, json.loads(report.read_text())


class TestSerialization:
    """The one test that looks at bytes."""

    def test_the_written_file_meets_the_wizards_structural_contract(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        _, german_path, _ = run(revolut_statement, tmp_path, "--until", "2026-06-30")
        german = german_path.read_bytes().decode("utf-8")

        header, *rows = german.split("\r\n")[:-1]
        assert header == "Name;Verwendungszweck;Buchungstag;Betrag"
        assert german.endswith("\r\n"), "rows are CRLF terminated"
        assert '"' not in german, "a semicolon delimiter never collides with a decimal comma"

        amounts = []
        for row in rows:
            name, purpose, day, amount = row.split(";")
            assert GERMAN_DATE.match(day), f"{day!r} is not D.M.YYYY"
            assert GERMAN_AMOUNT.match(amount), f"{amount!r} is not a German amount"
            assert purpose
            amounts.append(amount)

        # Direction rides on the sign of the one amount column, so both must appear.
        assert any(a.startswith("-") for a in amounts), "no debit written"
        assert any(not a.startswith("-") for a in amounts), "no credit written"

        us_output = tmp_path / "us.csv"
        main(
            [
                str(revolut_statement),
                "-o",
                str(us_output),
                "--report",
                str(tmp_path / "us.json"),
                "--until",
                "2026-06-30",
                "--format",
                "us",
            ]
        )
        american = us_output.read_bytes().decode("utf-8")

        us_header, *us_rows = american.split("\r\n")[:-1]
        assert us_header == "Name,Verwendungszweck,Buchungstag,Betrag"
        for row in us_rows:
            _, _, day, amount = next(iter(csv.reader(io.StringIO(row))))
            assert US_DATE.match(day), f"{day!r} is not ISO"
            assert US_AMOUNT.match(amount), f"{amount!r} is not a US amount"
            assert "," not in amount, "US amounts never group thousands"


class TestWhatItPrints:
    """stdout is the run's result: readable by default, JSON on request."""

    def test_the_default_is_a_readable_summary_not_json(
        self,
        revolut_statement: Path,
        tmp_path: Path,
        offline_ecb: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([str(revolut_statement), "-o", str(tmp_path / "out.csv"), "--until", "2026-06-30"])
        printed = capsys.readouterr().out

        assert not printed.lstrip().startswith("{")
        assert "Read 7 rows" in printed
        assert "Emitted 7 bookings" in printed

    def test_the_summary_states_the_window_and_the_next_lower_bound(
        self,
        revolut_statement: Path,
        tmp_path: Path,
        offline_ecb: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([str(revolut_statement), "-o", str(tmp_path / "out.csv"), "--until", "2026-06-30"])
        printed = capsys.readouterr().out

        assert "2026-06-30" in printed
        assert "--since 2026-07-01" in printed

    def test_the_summary_surfaces_warnings(
        self,
        revolut_statement: Path,
        tmp_path: Path,
        offline_ecb: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([str(revolut_statement), "-o", str(tmp_path / "out.csv"), "--until", "2026-06-30"])
        assert "Warning:" in capsys.readouterr().out

    def test_output_json_prints_the_report_and_nothing_else(
        self,
        revolut_statement: Path,
        tmp_path: Path,
        offline_ecb: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An agent parses stdout whole, so nothing may share it."""
        main(
            [
                str(revolut_statement),
                "-o",
                str(tmp_path / "out.csv"),
                "--until",
                "2026-06-30",
                "--output",
                "json",
            ]
        )
        report = json.loads(capsys.readouterr().out)

        assert report["counts"]["bookings_emitted"] == 7
        assert report["exit_code"] == 2

    def test_a_report_file_can_be_written_alongside_the_summary(
        self,
        revolut_statement: Path,
        tmp_path: Path,
        offline_ecb: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        report_path = tmp_path / "report.json"
        main(
            [
                str(revolut_statement),
                "-o",
                str(tmp_path / "out.csv"),
                "--until",
                "2026-06-30",
                "--report",
                str(report_path),
            ]
        )

        assert "Emitted 7 bookings" in capsys.readouterr().out
        assert json.loads(report_path.read_text())["counts"]["bookings_emitted"] == 7

    def test_an_unknown_output_kind_is_refused(
        self, revolut_statement: Path, tmp_path: Path
    ) -> None:
        with pytest.raises(SystemExit):
            main([str(revolut_statement), "-o", str(tmp_path / "out.csv"), "--output", "yaml"])


class TestExitCodes:
    def test_a_clean_run_exits_zero(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        code, _, report = run(
            revolut_statement, tmp_path, "--since", "2026-06-01", "--until", "2026-06-30"
        )
        assert code == 0
        assert report["warnings"] == []

    def test_a_substituted_rate_exits_two_with_output_still_written(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        code, output, report = run(revolut_statement, tmp_path, "--until", "2026-06-30")
        assert code == 2
        assert output.read_text()
        assert any("2026-05-31" in warning for warning in report["warnings"])

    def test_an_unpriceable_row_refuses_rather_than_guessing(
        self, tmp_path: Path, offline_ecb: None
    ) -> None:
        """The published series stops well before this row, so no rate resolves."""
        statement = tmp_path / "later.csv"
        statement.write_text(
            "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
            "Topup,Pro,2026-07-15 09:00:00,2026-07-15 09:00:01,Top-up,100.00,0.00,USD,COMPLETED,100.00\n"
        )
        code = main([str(statement), "-o", str(tmp_path / "out.csv"), "--until", "2026-07-31"])
        assert code == 1

    def test_an_empty_window_warns_rather_than_failing_silently(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        code, _, report = run(
            revolut_statement, tmp_path, "--since", "2026-01-01", "--until", "2026-01-31"
        )
        assert code == 2
        assert report["counts"]["bookings_emitted"] == 0
        assert report["warnings"]

    def test_an_inverted_window_is_refused(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        output = tmp_path / "out.csv"
        code = main(
            [
                str(revolut_statement),
                "-o",
                str(output),
                "--since",
                "2026-06-30",
                "--until",
                "2026-06-01",
            ]
        )
        assert code == 1

    def test_a_usage_error_does_not_masquerade_as_a_warning(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["nonexistent.csv"])
        assert excinfo.value.code == 1


class TestReport:
    @pytest.fixture
    def report(self, wise_statement: Path, tmp_path: Path, offline_ecb: None) -> dict[str, Any]:
        _, _, report = run(wise_statement, tmp_path, "--until", "2026-06-14")
        return report

    def test_it_states_the_effective_window_and_the_row_count(self, report: dict[str, Any]) -> None:
        assert report["window"]["until"] == "2026-06-14"
        assert report["counts"]["rows_read"] == 8

    def test_it_states_the_lower_bound_to_use_next_time(self, report: dict[str, Any]) -> None:
        assert report["window"]["next_since"] == "2026-06-15"

    def test_it_names_every_dropped_row_and_the_reason(self, report: dict[str, Any]) -> None:
        reasons = {drop["reason"] for drop in report["dropped"]}
        assert reasons == {"funded_from_other_currency", "not_settled"}
        assert all(drop["source_ref"] for drop in report["dropped"])

    def test_rows_outside_the_window_are_counted_separately(
        self, wise_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        _, _, report = run(
            wise_statement, tmp_path, "--since", "2026-06-01", "--until", "2026-06-14"
        )
        assert report["counts"]["rows_outside_window"] > 0
        assert {d["reason"] for d in report["outside_window"]} == {"outside_window"}
        assert "outside_window" not in {d["reason"] for d in report["dropped"]}

    def test_it_names_every_rate_and_where_it_came_from(self, report: dict[str, Any]) -> None:
        provenances = {rate["provenance"] for rate in report["rates"]}
        assert provenances <= {"export", "ecb", "ecb_previous_business_day"}
        assert "export" in provenances

    def test_an_ecb_rate_cites_the_published_quote_it_inverted(
        self, report: dict[str, Any]
    ) -> None:
        ecb = [rate for rate in report["rates"] if rate["provenance"] != "export"]
        assert ecb
        for rate in ecb:
            assert rate["ecb_quote_usd_per_eur"]
            assert rate["quote_date"]

    def test_every_emitted_booking_is_traceable_to_a_source_row_and_a_dated_rate(
        self, report: dict[str, Any]
    ) -> None:
        assert len(report["emitted"]) == report["counts"]["bookings_emitted"]
        for booking in report["emitted"]:
            assert booking["source_ref"]
            assert booking["booking_date"]
            assert booking["usd_to_eur"]
            assert booking["rate_provenance"]

    def test_it_states_the_totals_needed_for_kursdifferenzen(self, report: dict[str, Any]) -> None:
        assert set(report["totals"]) == {
            "usd_credited",
            "usd_debited",
            "usd_net",
            "eur_credited",
            "eur_debited",
            "eur_net",
        }

    def test_it_is_machine_readable_without_parsing_prose(self, report: dict[str, Any]) -> None:
        assert report["exit_code"] in {0, 2}
        assert report["source"]["provider"] == "wise"


class TestDeterminism:
    def test_rerunning_over_the_same_input_changes_nothing(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        """Re-running and diffing is a valid correctness check.

        This asserts only that two runs agree; it pins no part of the file's format.
        """
        _, output, first_report = run(revolut_statement, tmp_path, "--until", "2026-06-30")
        first_csv = output.read_bytes()

        _, output, second_report = run(revolut_statement, tmp_path, "--until", "2026-06-30")

        assert output.read_bytes() == first_csv
        assert second_report == first_report
