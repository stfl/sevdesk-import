# sevdesk-importer

Converts Wise and Revolut **USD** statement exports into CSVs that the sevDesk bank-import
wizard accepts, with every amount already expressed in EUR at the reference rate of its
settlement date. sevDesk only supports EUR-denominated bank accounts; this bridges that gap.

> ## Never commit real account data
>
> **This repository is public and the working tree contains real bank statements.**
>
> `.gitignore` denies `*.csv` everywhere and re-admits only `tests/fixtures/**/*.csv`.
> Never weaken that rule, never `git add -f` a statement, and check `git status --ignored`
> before any bulk `git add`. Test fixtures are synthetic — generated to reproduce the
> structural quirks of real exports without containing real data.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->


## Current State

The specification is `bd show sevdesk-importer-5op`.

`sevdesk_importer/` holds the converter. Reading an export is `providers.py`, which turns
both schemas into one `Movement` record; pricing is `conversion.py`, which splits fees into
their own rows; `rates.py` owns the ECB series and the resolution order. `cli.py` wires them
together and chooses the exit code.

## Stack

Python 3, **standard library only** — `csv`, `decimal`, `zoneinfo`, `datetime`,
`urllib.request`, `json`, `argparse`. Do not add a third-party dependency without a
compelling reason: a zero-dependency closure is why `nix run` starts in seconds on a cold
machine instead of building anything. Packaged as a flake.

```bash
nix run . -- wise-usd.csv -o out.csv    # run
pytest                                   # tests, offline
mypy --strict .                          # types
```

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
- **Fees are separate rows**, same date and rate as their parent, so they stay deductible as
  Bankspesen. Emitted rows must still sum to the real balance movement.
- **One statement, one output file, one sevDesk bank account.** No cross-file matching.

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

## Testing

Assert on externally observable behaviour, never on internal structure — the implementation
should be freely restructurable as long as bookings stay correct.

Business rules live in unit tests over pure functions, asserted on structured records rather
than CSV text, so a failure names the broken rule rather than a byte offset. Exactly one
end-to-end test inspects file bytes, covering serialization only: header names, column order,
delimiter, quoting, decimal separator. Those are what the sevDesk wizard keys on.

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
