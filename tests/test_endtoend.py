"""The written file, and the command line contract around it.

Exactly one test here asserts on the *shape* of the written bytes, and it covers
serialization only — header names, column order, delimiter, quoting and decimal
separator, which are what the sevDesk wizard keys on and what no assertion over
records would catch. One further test reads the file only to check that two runs
agree, pinning no part of the format. Everything else asserts on what the command
prints, on the JSON report, or on the run directory left behind.

The run directory is asserted through the command rather than under it: filing an
import, refusing to overwrite one, and resuming from the last are all things a caller
can only observe by running the command and looking at the tree it produced.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from sevdesk_importer.cli import main

GERMAN_AMOUNT = re.compile(r"^-?\d{1,3}(\.\d{3})*,\d{2}$")
GERMAN_DATE = re.compile(r"^[1-9]\d?\.[1-9]\d?\.\d{4}$")
US_AMOUNT = re.compile(r"^-?\d+\.\d{2}$")
US_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Both fixtures book through this day. Revolut stops here because a top-up started on
#: the 14th has not settled; Wise stops here because it is simply the last row.
LAST_BOOKED = "2026-06-11"


def run(statement: Path, tmp_path: Path, *extra: str) -> tuple[int, Path | None, dict[str, Any]]:
    """Run the command and hand back what it wrote, found the way a caller would."""
    report = tmp_path / "report.json"
    code = main(
        [str(statement), "--run-dir", str(tmp_path / "run"), "--report", str(report), *extra]
    )
    data = json.loads(report.read_text())
    written = Path(data["output"]) if data["output"] else None
    return code, written, data


class TestSerialization:
    """The one test that looks at bytes."""

    def test_the_written_file_meets_the_wizards_structural_contract(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        _, german_path, _ = run(revolut_statement, tmp_path)
        assert german_path is not None
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

        us_source = tmp_path / "us-input.csv"
        us_source.write_bytes(german_path.parent.joinpath("in.csv").read_bytes())
        main([str(us_source), "--run-dir", str(tmp_path / "us-run"), "--format", "us"])
        american = (tmp_path / "us-run" / "revolut" / LAST_BOOKED / "out.csv").read_bytes().decode()

        us_header, *us_rows = american.split("\r\n")[:-1]
        assert us_header == "Name,Verwendungszweck,Buchungstag,Betrag"
        for row in us_rows:
            _, _, day, amount = next(iter(csv.reader(io.StringIO(row))))
            assert US_DATE.match(day), f"{day!r} is not ISO"
            assert US_AMOUNT.match(amount), f"{amount!r} is not a US amount"
            assert "," not in amount, "US amounts never group thousands"


class TestTheRunDirectoryRecordsTheImport:
    def test_the_import_is_filed_under_the_provider_and_the_last_day_booked(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        _, written, _ = run(revolut_statement, tmp_path)
        assert written == tmp_path / "run" / "revolut" / LAST_BOOKED / "out.csv"

    def test_the_export_is_filed_beside_the_csv_it_produced(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        original = revolut_statement.read_bytes()
        _, written, _ = run(revolut_statement, tmp_path)
        assert written is not None

        assert (written.parent / "in.csv").read_bytes() == original
        assert not revolut_statement.exists(), "the export is moved, so it cannot be fed in twice"

    def test_the_summary_names_the_full_path_of_what_it_wrote(
        self,
        revolut_statement: Path,
        tmp_path: Path,
        offline_ecb: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([str(revolut_statement), "--run-dir", str(tmp_path / "run")])
        expected = tmp_path / "run" / "revolut" / LAST_BOOKED / "out.csv"
        assert str(expected.resolve()) in capsys.readouterr().out

    def test_a_second_import_of_the_same_period_is_refused(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        """The bound is derived, so an existing directory means this period is already in.

        Resuming normally makes this unreachable; overriding the lower bound by hand is
        what can aim a second run at a period already filed.
        """
        run(revolut_statement, tmp_path)

        again = tmp_path / "again.csv"
        again.write_bytes((tmp_path / "run" / "revolut" / LAST_BOOKED / "in.csv").read_bytes())
        code = main([str(again), "--run-dir", str(tmp_path / "run"), "--since", "2026-06-01"])

        assert code == 1
        assert again.exists(), "a refused run leaves the export where it was"

    def test_a_refused_run_leaves_the_earlier_import_untouched(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        _, written, _ = run(revolut_statement, tmp_path)
        assert written is not None
        first = written.read_bytes()

        again = tmp_path / "again.csv"
        again.write_bytes((written.parent / "in.csv").read_bytes())
        main([str(again), "--run-dir", str(tmp_path / "run"), "--since", "2026-06-01"])

        assert written.read_bytes() == first

    def test_the_two_providers_are_filed_separately(
        self, revolut_statement: Path, wise_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        """Both fixtures end on the same day, and neither may collide with the other."""
        _, from_revolut, _ = run(revolut_statement, tmp_path)
        _, from_wise, _ = run(wise_statement, tmp_path)

        assert from_revolut == tmp_path / "run" / "revolut" / LAST_BOOKED / "out.csv"
        assert from_wise == tmp_path / "run" / "wise" / LAST_BOOKED / "out.csv"


class TestResuming:
    def test_the_first_run_has_no_lower_bound(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        _, _, report = run(revolut_statement, tmp_path)
        assert report["window"]["since"] is None
        assert report["counts"]["bookings_emitted"] == 6

    def test_the_run_leaves_a_pointer_at_what_it_reached(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        run(revolut_statement, tmp_path)
        latest = tmp_path / "run" / "revolut" / "latest"

        assert latest.is_symlink()
        assert latest.resolve() == (tmp_path / "run" / "revolut" / LAST_BOOKED).resolve()

    def test_the_next_run_starts_the_day_after_the_last_one_finished(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        """No date is typed in; the tree remembers where the last import stopped."""
        run(revolut_statement, tmp_path)

        again = tmp_path / "again.csv"
        again.write_bytes((tmp_path / "run" / "revolut" / LAST_BOOKED / "in.csv").read_bytes())
        report_path = tmp_path / "second.json"
        main([str(again), "--run-dir", str(tmp_path / "run"), "--report", str(report_path)])

        assert json.loads(report_path.read_text())["window"]["since"] == "2026-06-12"

    def test_re_reading_the_same_export_books_nothing_twice(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        """Resuming is what makes an overlapping re-export safe to feed in."""
        run(revolut_statement, tmp_path)

        again = tmp_path / "again.csv"
        again.write_bytes((tmp_path / "run" / "revolut" / LAST_BOOKED / "in.csv").read_bytes())
        report_path = tmp_path / "second.json"
        code = main([str(again), "--run-dir", str(tmp_path / "run"), "--report", str(report_path)])

        assert code == 2
        assert json.loads(report_path.read_text())["counts"]["bookings_emitted"] == 0

    def test_an_explicit_since_overrides_the_pointer(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        _, _, report = run(revolut_statement, tmp_path, "--since", "2026-06-01")
        assert report["window"]["since"] == "2026-06-01"

    def test_an_unreadable_pointer_refuses_rather_than_importing_from_the_start(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        """Silently restarting from nothing would re-book every past period."""
        provider = tmp_path / "run" / "revolut"
        provider.mkdir(parents=True)
        (provider / "latest").symlink_to("not-a-day")

        code = main([str(revolut_statement), "--run-dir", str(tmp_path / "run")])

        assert code == 1
        assert revolut_statement.exists()


class TestNothingNewToBook:
    """A run with nothing settled must not disturb the ledger it read."""

    @pytest.fixture
    def spent(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> tuple[int, Path, dict[str, Any]]:
        run(revolut_statement, tmp_path)
        again = tmp_path / "again.csv"
        again.write_bytes((tmp_path / "run" / "revolut" / LAST_BOOKED / "in.csv").read_bytes())
        report_path = tmp_path / "second.json"
        code = main([str(again), "--run-dir", str(tmp_path / "run"), "--report", str(report_path)])
        return code, again, json.loads(report_path.read_text())

    def test_it_warns_rather_than_failing(self, spent: tuple[int, Path, dict[str, Any]]) -> None:
        code, _, report = spent
        assert code == 2
        assert report["warnings"]

    def test_no_import_is_filed(
        self, spent: tuple[int, Path, dict[str, Any]], tmp_path: Path
    ) -> None:
        _, _, report = spent
        assert report["output"] is None
        assert sorted(p.name for p in (tmp_path / "run" / "revolut").iterdir()) == [
            LAST_BOOKED,
            "latest",
        ]

    def test_the_export_is_left_where_it_was(self, spent: tuple[int, Path, dict[str, Any]]) -> None:
        _, statement, _ = spent
        assert statement.exists(), "nothing was booked, so nothing was consumed"

    def test_the_resume_point_does_not_move(
        self, spent: tuple[int, Path, dict[str, Any]], tmp_path: Path
    ) -> None:
        assert (tmp_path / "run" / "revolut" / "latest").resolve().name == LAST_BOOKED

    def test_the_next_lower_bound_still_stands(
        self, spent: tuple[int, Path, dict[str, Any]]
    ) -> None:
        _, _, report = spent
        assert report["window"]["next_since"] == "2026-06-12"


class TestTheWindowStopsShortOfWhatCouldStillSettle:
    def test_a_pending_row_holds_the_window_behind_the_day_it_started(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        """The top-up started 2026-06-14 has not settled, so nothing on or after it is booked."""
        _, _, report = run(revolut_statement, tmp_path)

        assert report["window"]["until"] == LAST_BOOKED
        assert all(b["booking_date"] < "2026-06-14" for b in report["emitted"])

    def test_a_cancelled_row_does_not_hold_the_window_back(
        self, wise_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        """A transfer cancelled on 2026-06-03 can never settle, so later rows still book."""
        _, _, report = run(wise_statement, tmp_path)

        assert "not_settled" in {drop["reason"] for drop in report["dropped"]}
        assert report["window"]["until"] == LAST_BOOKED

    def test_a_row_behind_a_pending_one_is_deferred_not_dropped(
        self, tmp_path: Path, offline_ecb: None
    ) -> None:
        """It is not skipped: the next run's lower bound is set so it is read again."""
        statement = tmp_path / "revolut.csv"
        statement.write_text(
            "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
            "Topup,Pro,2026-06-01 09:00:00,2026-06-01 09:00:01,Early,100.00,0.00,USD,COMPLETED,100.00\n"
            "Transfer,Pro,2026-06-08 09:00:00,,Slow,-10.00,0.00,USD,PENDING,\n"
            "Topup,Pro,2026-06-10 09:00:00,2026-06-10 09:00:01,Behind,50.00,0.00,USD,COMPLETED,140.00\n"
        )
        report_path = tmp_path / "report.json"
        main([str(statement), "--run-dir", str(tmp_path / "run"), "--report", str(report_path)])
        report = json.loads(report_path.read_text())

        assert report["window"]["until"] == "2026-06-01", "the window stops before the pending row"
        assert [b["booking_date"] for b in report["emitted"]] == ["2026-06-01"]
        assert report["window"]["next_since"] == "2026-06-02", "the deferred row is read again"

    def test_an_undatable_pending_row_refuses_the_whole_run(
        self, tmp_path: Path, offline_ecb: None
    ) -> None:
        """No start date means no safe place to stop, and guessing would skip it."""
        statement = tmp_path / "revolut.csv"
        statement.write_text(
            "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
            "Topup,Pro,2026-06-01 09:00:00,2026-06-01 09:00:01,Early,100.00,0.00,USD,COMPLETED,100.00\n"
            "Transfer,Pro,,,Undatable,-10.00,0.00,USD,PENDING,\n"
        )
        code = main([str(statement), "--run-dir", str(tmp_path / "run")])

        assert code == 1
        assert statement.exists()
        assert not (tmp_path / "run" / "revolut" / "latest").exists()

    def test_a_pending_row_in_another_currency_does_not_hold_this_balance_back(
        self, tmp_path: Path, offline_ecb: None
    ) -> None:
        """sevDesk imports the EUR balance separately; it is no reason to stall USD."""
        statement = tmp_path / "revolut.csv"
        statement.write_text(
            "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
            "Topup,Pro,2026-06-01 09:00:00,2026-06-01 09:00:01,Early,100.00,0.00,USD,COMPLETED,100.00\n"
            "Transfer,Pro,2026-06-08 09:00:00,,Pending euro,-10.00,0.00,EUR,PENDING,\n"
            "Topup,Pro,2026-06-10 09:00:00,2026-06-10 09:00:01,Behind,50.00,0.00,USD,COMPLETED,150.00\n"
        )
        report_path = tmp_path / "report.json"
        main([str(statement), "--run-dir", str(tmp_path / "run"), "--report", str(report_path)])

        assert json.loads(report_path.read_text())["window"]["until"] == "2026-06-10"


class TestWhatItPrints:
    """stdout is the run's result: readable by default, JSON on request."""

    def test_the_default_is_a_readable_summary_not_json(
        self,
        revolut_statement: Path,
        tmp_path: Path,
        offline_ecb: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([str(revolut_statement), "--run-dir", str(tmp_path / "run")])
        printed = capsys.readouterr().out

        assert not printed.lstrip().startswith("{")
        assert "Read 7 rows" in printed
        assert "Emitted 6 bookings" in printed

    def test_the_summary_states_the_window_and_the_next_lower_bound(
        self,
        revolut_statement: Path,
        tmp_path: Path,
        offline_ecb: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([str(revolut_statement), "--run-dir", str(tmp_path / "run")])
        printed = capsys.readouterr().out

        assert LAST_BOOKED in printed
        assert "--since 2026-06-12" in printed

    def test_the_summary_surfaces_warnings(
        self,
        revolut_statement: Path,
        tmp_path: Path,
        offline_ecb: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([str(revolut_statement), "--run-dir", str(tmp_path / "run")])
        assert "Warning:" in capsys.readouterr().out

    def test_output_json_prints_the_report_and_nothing_else(
        self,
        revolut_statement: Path,
        tmp_path: Path,
        offline_ecb: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An agent parses stdout whole, so nothing may share it."""
        main([str(revolut_statement), "--run-dir", str(tmp_path / "run"), "--output", "json"])
        report = json.loads(capsys.readouterr().out)

        assert report["counts"]["bookings_emitted"] == 6
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
                "--run-dir",
                str(tmp_path / "run"),
                "--report",
                str(report_path),
            ]
        )

        assert "Emitted 6 bookings" in capsys.readouterr().out
        assert json.loads(report_path.read_text())["counts"]["bookings_emitted"] == 6

    def test_a_dropped_row_is_named_and_valued_not_just_counted(
        self,
        wise_statement: Path,
        tmp_path: Path,
        offline_ecb: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A count alone does not say what went missing."""
        main([str(wise_statement), "--run-dir", str(tmp_path / "run")])
        printed = capsys.readouterr().out

        assert "Example SaaS Ltd" in printed
        assert "4.50 EUR" in printed, "a dropped row states its own value, in its own currency"

    def test_the_summary_states_the_fees_charged(
        self,
        wise_statement: Path,
        tmp_path: Path,
        offline_ecb: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """0.12 on the card payment plus 15.50 on the transfer."""
        main([str(wise_statement), "--run-dir", str(tmp_path / "run")])
        assert "Fees 15.62 USD" in capsys.readouterr().out

    def test_no_line_mixes_two_currencies(
        self,
        wise_statement: Path,
        tmp_path: Path,
        offline_ecb: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([str(wise_statement), "--run-dir", str(tmp_path / "run")])

        totals = [
            line
            for line in capsys.readouterr().out.splitlines()
            if line.startswith(("Moved", "Fees", "Booked"))
        ]
        assert totals
        for line in totals:
            assert not ("USD" in line and "EUR" in line), f"mixed currencies: {line!r}"

    def test_an_unknown_output_kind_is_refused(
        self, revolut_statement: Path, tmp_path: Path
    ) -> None:
        with pytest.raises(SystemExit):
            main([str(revolut_statement), "--run-dir", str(tmp_path / "run"), "--output", "yaml"])


class TestExitCodes:
    def test_a_clean_run_exits_zero(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        code, _, report = run(revolut_statement, tmp_path, "--since", "2026-06-01")
        assert code == 0
        assert report["warnings"] == []

    def test_a_substituted_rate_exits_two_with_output_still_written(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        code, output, report = run(revolut_statement, tmp_path)
        assert code == 2
        assert output is not None
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
        code = main([str(statement), "--run-dir", str(tmp_path / "run")])
        assert code == 1
        assert statement.exists(), "a refused run consumes nothing"

    def test_a_window_that_starts_after_the_data_warns_rather_than_failing_silently(
        self, revolut_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        code, output, report = run(revolut_statement, tmp_path, "--since", "2027-01-01")
        assert code == 2
        assert output is None
        assert report["counts"]["bookings_emitted"] == 0
        assert report["warnings"]

    def test_a_usage_error_does_not_masquerade_as_a_warning(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["nonexistent.csv", "--since", "the-first-of-may"])
        assert excinfo.value.code == 1

    def test_an_unreadable_statement_refuses_rather_than_warning(self, tmp_path: Path) -> None:
        assert main(["nonexistent.csv", "--run-dir", str(tmp_path / "run")]) == 1


class TestReport:
    @pytest.fixture
    def report(self, wise_statement: Path, tmp_path: Path, offline_ecb: None) -> dict[str, Any]:
        _, _, report = run(wise_statement, tmp_path)
        return report

    def test_it_states_the_effective_window_and_the_row_count(self, report: dict[str, Any]) -> None:
        assert report["window"]["until"] == LAST_BOOKED
        assert report["counts"]["rows_read"] == 8

    def test_it_states_the_lower_bound_to_use_next_time(self, report: dict[str, Any]) -> None:
        assert report["window"]["next_since"] == "2026-06-12"

    def test_the_next_lower_bound_is_the_day_after_the_last_booking(
        self, report: dict[str, Any]
    ) -> None:
        """Derived from what was booked, never from the day the tool happened to run."""
        last = max(date.fromisoformat(b["booking_date"]) for b in report["emitted"])
        assert report["window"]["until"] == last.isoformat()

    def test_it_names_the_export_it_filed_rather_than_where_it_came_from(
        self, report: dict[str, Any]
    ) -> None:
        assert report["source"]["path"].endswith("in.csv")

    def test_it_names_every_dropped_row_and_the_reason(self, report: dict[str, Any]) -> None:
        reasons = {drop["reason"] for drop in report["dropped"]}
        assert reasons == {"funded_from_other_currency", "not_settled"}
        assert all(drop["source_ref"] for drop in report["dropped"])

    def test_rows_outside_the_window_are_counted_separately(
        self, wise_statement: Path, tmp_path: Path, offline_ecb: None
    ) -> None:
        _, _, report = run(wise_statement, tmp_path, "--since", "2026-06-01")
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
            "usd_fees",
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
        _, output, first_report = run(revolut_statement, tmp_path)
        assert output is not None
        first_csv = output.read_bytes()

        again = tmp_path / "again.csv"
        again.write_bytes((output.parent / "in.csv").read_bytes())
        second_report_path = tmp_path / "second.json"
        main(
            [
                str(again),
                "--run-dir",
                str(tmp_path / "second-run"),
                "--report",
                str(second_report_path),
            ]
        )
        second = tmp_path / "second-run" / "revolut" / LAST_BOOKED / "out.csv"
        second_report = json.loads(second_report_path.read_text())

        assert second.read_bytes() == first_csv
        for key in ("counts", "emitted", "dropped", "rates", "totals", "warnings", "window"):
            assert second_report[key] == first_report[key]
