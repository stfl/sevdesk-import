# sevdesk-importer

Converts Wise and Revolut **USD** statement exports into CSVs the sevDesk bank-import
wizard accepts, with every amount already expressed in EUR at the reference rate of its
settlement date. sevDesk only supports EUR-denominated bank accounts; this bridges that gap.

```bash
nix run . -- wise-usd.csv -o wise.sevdesk.csv --since 2026-05-01
```

Each USD account is its own sevDesk bank account and gets its own output file. Alongside the
CSV the tool writes a JSON report — every row it dropped and why, every rate it used and
where that rate came from, and the exact `--since` to use next time.

## Options

| Option | Meaning |
| --- | --- |
| `-o PATH` | where to write the sevDesk CSV (required) |
| `--report PATH` | write the JSON report here instead of stdout |
| `--format german\|us` | number and date convention; the delimiter follows from it (default German) |
| `--since YYYY-MM-DD` | earliest settlement date to convert, inclusive (default: no lower bound) |
| `--until YYYY-MM-DD` | latest settlement date to convert, inclusive (default: today in Vienna) |

Exit `0` for a clean run, `2` when output was written but something needs attention — a rate
substituted from an earlier business day, say — and `1` when the tool refused to produce
output rather than guess a number.

## How amounts are decided

**Buchungstag is the settlement date**, never the initiation date, normalised to
Europe/Vienna. Wise stamps UTC and Revolut stamps Vienna local time, so a 22:02 UTC
transaction belongs to the *next* Vienna day — which is how a transaction near a month
boundary lands in the right VAT period.

**Rates resolve in one order**: the rate recorded in the export, then the ECB daily
reference rate for the Buchungstag, then the most recent published business day before it,
which is always reported. If none resolves the run refuses.

**Fees are separate rows** on the same date at the same rate, so they stay deductible as
Bankspesen. An incoming transfer is booked gross with its fee as a separate debit. Emitted
rows still sum to the real balance movement.

**Only rows that moved this account's USD balance are emitted.** For Wise that is `OUT` with
source currency USD, or `IN` with target currency USD — which excludes the EUR-funded leg of
a split-currency card payment, since sevDesk already holds that leg from the Wise EUR
auto-import. The Verwendungszweck of such a payment carries the full invoice total in the
form sevDesk's own importer writes, so one search reunites both legs onto a single Beleg.

## Before importing

Enable **"Gutschrift & Belastung einblenden"** in the sevDesk import wizard — direction is
expressed by which of the two amount columns is populated — and match the wizard's number
and date format to `--format`.

sevDesk does not check for duplicates when it reads a CSV, and the only remedy on its side is
deleting each duplicated row by hand. Use `--since` with the `next_since` value from the
previous run's report.

## Development

```bash
nix develop --command pytest              # tests, offline
nix develop --command mypy                # types
nix develop --command ruff format .       # formatting
```

Entering the dev shell installs a pre-commit hook that runs `ruff format`; a commit whose
files it reformats is rejected, so re-stage and commit again. CI runs the same three checks
plus a package build on every push and pull request.

Python 3, standard library only. Tests never touch the network: the ECB call is patched at
the HTTP boundary against a captured real response. Fixtures are synthetic — generated to
reproduce the structural quirks of real exports without containing real data.

> **Never commit real account data.** `.gitignore` denies `*.csv` everywhere and re-admits
> only `tests/fixtures/**/*.csv`. Never weaken that rule and never `git add -f` a statement.
