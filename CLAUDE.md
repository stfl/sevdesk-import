# sevdesk-importer

Converts Wise and Revolut **USD** statement exports into CSVs that the sevDesk bank-import
wizard accepts, with every amount already expressed in EUR at the reference rate of its
settlement date. sevDesk only supports EUR-denominated bank accounts; this bridges that gap.

> ## Never commit real account data
>
> **This repository is public and the working tree contains real bank statements.**
>
> `.gitignore` denies `*.csv` everywhere and re-admits only `tests/fixtures/**/*.csv`, and
> ignores `run/` whole — imports filed there are real statements by definition.
> Never weaken those rules, never `git add -f` a statement, and check `git status --ignored`
> before any bulk `git add`. Test fixtures are synthetic — generated to reproduce the
> structural quirks of real exports without containing real data.

## Issue Tracking

Issues live in **GitHub Issues** on [stfl/sevdesk-import](https://github.com/stfl/sevdesk-import/issues),
reached with the `gh` CLI:

```bash
gh issue list                          # what is open
gh issue view <number>                 # read one
gh issue create --title T --body B     # file follow-up work
gh issue close <number>                # complete it
```

File an issue for anything that outlives the session; a TODO list that dies with the
conversation does not count as tracking. Steps within a session can be tracked however you
like.

Commit and push only when asked. Branch rather than committing to `main`, and open a pull
request — CI runs the tests, types, the format check and a package build on every one.

## Current State

`sevdesk_importer/` holds the converter. Reading an export is `providers.py`, which turns
both schemas into one `Movement` record; pricing is `conversion.py`, which folds a fee into
the booking it belongs to; `rates.py` owns the ECB series and the resolution order.
`window.py` derives the import window and owns the rule that stops it short of anything
unsettled; `runs.py` files each import under `run/` and reads back where the last one stopped.
`cli.py` wires them together and chooses the exit code.

## Stack

Python 3, **standard library only** — `csv`, `decimal`, `zoneinfo`, `datetime`,
`urllib.request`, `json`, `argparse`. Do not add a third-party dependency without a
compelling reason: a zero-dependency closure is why `nix run` starts in seconds on a cold
machine instead of building anything. Packaged as a flake.

```bash
just run wise-usd.csv            # convert a statement; it files itself under run/
just check                       # tests, types, format check — what CI runs
just                             # list every recipe
```

Recipes assume their tools are on `PATH`. Inside the dev shell they just work; from a bare
shell, prefix one command: `nix develop --command just check`. Never make a recipe or script
invoke `nix develop` itself — the environment is the caller's responsibility.

## Domain Rules

These are settled decisions. Changing one changes what gets booked, so treat them as the
contract rather than as preferences.

- **Buchungstag is the settlement date**, never the initiation date — Wise `Finished on`,
  Revolut `Completed Date`. Revolut can settle seven days after initiation.
- **Wise exports UTC; Revolut exports Europe/Vienna local time.** Normalise everything to
  Vienna. A `22:02` UTC transaction belongs to the *next* Vienna day, which is how a
  transaction near a month boundary ends up in the correct VAT period.
- **Rate order:** the rate recorded in the export wins; otherwise the ECB daily reference
  rate for the Buchungstag; otherwise the most recent published business day, reported and
  never silent. If none resolves, refuse — do not guess.
- **A row is emitted only if it moved this account's USD balance.** Wise: `OUT` with source
  currency USD, or `IN` with target currency USD. This excludes the EUR-funded leg of a
  split-currency card payment, which sevDesk already holds from the Wise EUR auto-import.
- **A bank fee is folded into the booking it belongs to**, never split into a second row. One
  provider row is one booking, whose amount is the net movement of the balance, because that
  is the single line the bank statement itself shows. The fee is named in the Verwendungszweck
  and recorded per booking in the report, so it stays visible without being booked apart.
- **One statement, one output file, one sevDesk bank account.** No cross-file matching.
- **The import window's upper bound is derived, never chosen.** It is the last day actually
  booked, pulled back behind the earliest row that could still settle — so a slow transaction
  is deferred to a later run, never stepped over. States a row can never leave (`CANCELLED`,
  `DECLINED`, `FAILED`, `REVERTED`, `REFUNDED`) hold nothing back; every other state does,
  including an unfamiliar one. An unsettled row with no initiation date refuses the run.
- **Each import is filed under `run/<provider>/<until>/`** as `in.csv` and `out.csv`, with a
  `latest` symlink naming the newest. The next run resumes from the day after it. An existing
  directory is refused, never overwritten, and the export is moved rather than copied so one
  download cannot be booked twice. A run that books nothing touches none of it.

## Traps

Each of these produces plausible-looking wrong numbers rather than an error.

- **The ECB series `D.USD.EUR.SP00.A` is quoted USD per EUR.** The USD→EUR factor is its
  *reciprocal* (`1.1405` → `0.87681`). Inverting it is wrong by ~30% and looks fine.
- **`Decimal` defaults to banker's rounding**, which turns `0.005` into `0.00`. Accounting
  wants `ROUND_HALF_UP`, set explicitly, or every exact half under-rounds systematically.
- **The Wise transaction identifier is not unique.** One card payment appears twice under a
  single ID, split across two source currencies. Never treat it as a key.
- **German numbers collide with a comma delimiter** — `9.876,54` forces quoting. The field
  delimiter follows the number format: `;` for German, `,` for US.
- **A Revolut row can have an empty `Completed Date` and empty `Balance`** when unsettled,
  and can carry `Amount=0.00` with a nonzero `Fee`. Neither may crash or emit a zero row.
- **An unsettled row is not just a dropped row.** It also bounds the window, so parsing must
  keep its state and initiation date rather than flattening it into a `Drop`. Treating a
  pending row as merely "not bookable" re-opens the silent skip the window exists to prevent.
- **Only unsettled rows that would move *USD* may hold the window back.** A pending EUR row
  belongs to a balance sevDesk imports separately, and stalling this account on it would defer
  bookings for no reason.

## Testing

Assert on externally observable behaviour, never on internal structure — the implementation
should be freely restructurable as long as bookings stay correct.

Business rules live in unit tests over pure functions, asserted on structured records rather
than CSV text, so a failure names the broken rule rather than a byte offset. Exactly one
end-to-end test asserts on the shape of the written bytes, covering serialization only: header
names, column order, delimiter, quoting, decimal separator. Those are what the sevDesk wizard
keys on. One further test reads the file only to confirm two runs agree, pinning no format.

**Tests never touch the network.** The ECB call is patched at the HTTP boundary, so URL
construction and response parsing stay under test, using a captured real ECB response as a
fixture. No injection point exists in production code for the benefit of tests.

## Non-Interactive Shell Commands

Shell commands may be aliased to interactive mode, causing an agent to hang waiting for
confirmation. Always use non-interactive forms:

```bash
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file
rm -rf directory            # NOT: rm -r directory
```

Also: `scp`/`ssh` with `-o BatchMode=yes`, `apt-get -y`, `HOMEBREW_NO_AUTO_UPDATE=1` for
`brew`.
