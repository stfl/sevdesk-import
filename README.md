# sevdesk-importer

> **This repository is archived.** The converter lives on as the `sevdesk-usd-import`
> skill inside [stfl/sevdesk-probe](https://github.com/stfl/sevdesk-probe)
> (`.claude/skills/sevdesk-usd-import/`), where it gained per-account import state
> that guards against double-booking. Development continues there.

sevDesk only supports bank accounts denominated in EUR. If you hold USD — a Wise USD balance,
a Revolut Pro USD balance — that money is invisible to your bookkeeping: you cannot import
either statement, so client payments, card spend and bank fees on those accounts never reach
sevDesk.

This converts either statement into a CSV the sevDesk bank-import wizard accepts, with every
amount already expressed in EUR at the exchange rate of the day the money actually moved.

```bash
nix run github:stfl/sevdesk-import -- wise-usd.csv
```

That is the whole tool. No build step, no account, no API keys — it reads a CSV, fetches the
ECB reference rates it needs, and writes a CSV. It files each import under `run/`, so the next
run knows where the last one stopped and you never type a date to avoid double-booking.

---

## 1. Get your statement out of the bank

You need the **USD** statement, as CSV. Each USD account becomes its own sevDesk bank account
and gets its own output file — do not merge them.

The converter detects which bank a file came from by its columns, so you do not tell it:

| Bank | The right export is the one whose columns include |
| --- | --- |
| Wise | `Finished on`, `Source currency`, `Target currency`, `Exchange rate` |
| Revolut | `Completed Date`, `State`, `Amount`, `Fee`, `Balance` |

In Wise this is the transaction-history CSV for the USD balance; in Revolut Business it is the
USD account statement exported as CSV rather than PDF. If you grab the wrong file the tool
tells you so immediately and names the columns it actually found.

> Export a **wide date range** — wider than you intend to import. The converter bounds what it
> books by itself, so extra rows cost nothing, and a settlement that lands late is caught
> rather than lost. See §4.

## 2. Convert it

```bash
nix run github:stfl/sevdesk-import -- revolut-usd.csv
```

You do not say where the output goes. The run is filed under `run/<bank>/<last day booked>/`,
holding the export it read as `in.csv` and the sevDesk CSV as `out.csv`:

```
run/revolut/2026-06-11/in.csv     the statement you handed it, moved here
run/revolut/2026-06-11/out.csv    the CSV to feed the wizard
run/revolut/latest -> 2026-06-11  where the next run picks up
```

The export is **moved**, not copied, so the same download cannot be fed in twice.

### From a clone

`nix run . -- …` does the same thing from inside a checkout. If you have [direnv][] the
directory sets itself up on entry, and there is a shorter form:

```bash
just run wise-usd.csv
```

`just` on its own lists every recipe. Without direnv, put one command in front:
`nix develop --command just run …`.

[direnv]: https://direnv.net

It prints a short summary:

```
Read 8 rows from /home/you/run/wise/2026-06-11/in.csv (wise)
Window the start of the statement to 2026-06-11, both inclusive
Emitted 6 bookings to /home/you/run/wise/2026-06-11/out.csv
Dropped 2 rows: 1 funded_from_other_currency, 1 not_settled
  Example SaaS Ltd — 4.50 EUR — funded from the EUR balance, which sevDesk imports separately
  Reverted Recipient — 500.00 USD — status CANCELLED
Moved 3565.40 USD in, 782.42 USD out, 2782.98 USD net
Fees 15.62 USD
Booked 3032.77 EUR in, 672.48 EUR out, 2360.29 EUR net
Next run resumes at --since 2026-06-12
Warning: No ECB reference rate published for 2026-05-31; used the previous published business day 2026-05-29 (1.1644 USD/EUR).
```

Read three things here: **the full path of the file** to hand the wizard, **how many bookings**
it holds, and **what got dropped** — each named with its own value. Totals are stated in USD as
the bank moved them, and again in EUR as they were booked. The last line is where the next run
will pick up on its own (§4).

Want the machine-readable version instead? `--output json` replaces the summary with the full
report on stdout, and nothing else shares it, so you can pipe it straight into `jq`:

```bash
nix run . -- wise-usd.csv --output json | jq .counts
```

`--report PATH` writes that same JSON to a file while leaving the summary on screen.

### What comes out

```
Name;Verwendungszweck;Buchungstag;Betrag
Acme Client Inc;Überweisung von Acme Client Inc (SampleACH) | 3.465,50 USD zu Kurs 0,849979;11.5.2026;2.945,60
Wise;Gebühr zu: Überweisung von Acme Client Inc (SampleACH) | 15,50 USD zu Kurs 0,849979;11.5.2026;-13,17
Example SaaS Ltd;Card transaction of 32.50 EUR issued by Example SaaS Ltd | 32,00 USD zu Kurs 0,875000;21.5.2026;-28,00
Wise;Gebühr zu: Card transaction of 32.50 EUR issued by Example SaaS Ltd | 0,12 USD zu Kurs 0,875000;21.5.2026;-0,11
Neobank Top-up;Kartenzahlung an Neobank Top-up | 750,00 USD zu Kurs 0,858811;29.5.2026;-644,11
```

Direction is the sign of `Betrag`: positive credits the account, negative debits it.

Every row states the original USD amount and the rate used, so you can check any figure
without opening the source export, and names any bank fee that was charged.

## 3. Import it into sevDesk

1. Set the number and date format to **German** (`1.234,56`, `31.12.2026`). That is what the
   converter writes by default. If your wizard is set to US conventions, convert with
   `--format us` instead and the delimiter follows automatically.
2. Map the four columns straight across: `Name`, `Verwendungszweck`, `Buchungstag`, `Betrag`.
   Direction rides on the sign of `Betrag`, so no wizard-side column toggle is needed.

### A payment split across two balances

A card payment can draw partly on your USD balance and partly on your EUR one. sevDesk already
holds the EUR leg from the Wise EUR auto-import, so this tool deliberately does not emit it —
booking it again would double-count.

To put both legs on a single Beleg, search sevDesk for the **invoice total** exactly as it
appears in the Verwendungszweck (`32.50` in the sample above, with a decimal point). That total
is the one token both legs share, so one search finds them in both bank accounts and you can
attach them to the same Beleg.

## 4. Importing again later, without double-booking

**sevDesk does not check for duplicates when it reads a CSV.** Import the same period twice and
every transaction lands twice; the only remedy on its side is deleting each duplicated row by
hand.

You do not manage that yourself. Each run files itself under `run/<bank>/<last day booked>/`
and the next one resumes from the day after — export a fresh statement whenever you like and
run the same command:

```bash
nix run . -- wise-usd.csv     # books through 2026-06-11, files it, points latest at it
nix run . -- wise-usd.csv     # a later export: starts at 2026-06-12 on its own
```

Both bounds are **inclusive**, and both filter on the **settlement** date — the day the money
actually landed, not the day it was initiated. A Revolut transfer can settle seven days after
it starts; using the settlement date is what stops it falling into the gap between two runs.

### The window stops at what settled, not at today

The upper bound is the last day the run actually **booked**, and it stops short of anything
that could still settle. A transfer started on the 8th and still pending holds the window at
the 7th, even if later rows have already settled — those rows are not dropped, they are
**deferred**, and the next run reads them once the pending one resolves.

That is why the bound is never today's date: a day the tool has merely *seen* is not a day it
has *booked*, and advancing past a transaction that lands later would skip it for good. States
a row can never leave — `CANCELLED`, `DECLINED`, `FAILED`, `REVERTED`, `REFUNDED` — hold
nothing back. Any other state does, including one no export has shown before.

### Filing the same period twice is refused

The bound is derived from your data, so re-importing a period produces the same directory name
— and an existing directory is refused rather than overwritten:

```
run/wise/2026-06-11 already exists, so bookings up to 2026-06-11 have been
imported already. Refusing to write over them.
```

Nothing is written and your export stays where it is. Pass `--since` to deliberately re-cover
an earlier period, after moving the old directory aside.

## 5. Options

| Option | Meaning |
| --- | --- |
| `--run-dir PATH` | where imports are filed and resumed from (default: `run`) |
| `--output text\|json` | what to print: a readable summary, or the full JSON report (default: text) |
| `--report PATH` | also write the JSON report to this file |
| `--format german\|us` | number and date convention; the delimiter follows from it (default German) |
| `--since YYYY-MM-DD` | earliest settlement date to convert, inclusive (default: the day after the last import of this bank) |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | clean run |
| `2` | the CSV was written, but read the warnings first — a substituted rate, or nothing new to book |
| `1` | refused to write anything rather than guess a number |

## 6. The report

The JSON report is the audit trail: it names every row that was dropped and why, every rate it
used and where that rate came from, and the totals.

```json
"dropped": [
  { "source_ref": "CARD_TRANSACTION-9000000001",
    "reason": "funded_from_other_currency",
    "detail": "funded from the EUR balance, which sevDesk imports separately" },
  { "source_ref": "TRANSFER-9000000007",
    "reason": "not_settled",
    "detail": "status CANCELLED" }
]
```

Rows excluded by the date window are counted separately, under `outside_window`, so "outside
this period" is never confused with "not bookable". `window.next_since` states where the next
run will resume, which is the day after the last one booked.

Each rate records the published ECB quote it came from, so you can cite any figure to its
publisher:

```json
{ "booking_date": "2026-05-11", "usd_to_eur": "0.84997875…",
  "provenance": "ecb", "ecb_quote_usd_per_eur": "1.1765", "quote_date": "2026-05-11" }
```

`totals` gives the run in both currencies. When `usd_net` returns to zero but `eur_net` does
not, that difference is realised FX gain or loss and belongs in **Kursdifferenzen** — the tool
states the figure but does not make the entry.

## 7. When something looks wrong

| Symptom | What it means |
| --- | --- |
| `Unrecognised statement: expected either a Wise export…` | wrong file — see the column table in §1 |
| `transaction type 'X' has no booking rule` | your bank used a type this tool has never seen. It refuses rather than mis-book it; open an issue with the type name |
| `No ECB reference rate for …` | the statement reaches into days the ECB has not published (usually the future). Export again once they are |
| Exit `2`, "Nothing new has settled" | everything in this export was booked by an earlier run. Nothing was filed and your export was left in place |
| `already exists, so bookings up to … have been imported already` | this period is already filed under `run/`. See §4 |
| Fewer bookings than you expected | a pending row is holding the window back; the rest are deferred to the next run, not lost. Check the summary's upper bound |
| `may still settle, but the row carries no initiation date` | an unsettled row has no date to stop before, so the run refuses rather than risk skipping it. Re-export once it settles |
| The wizard shows no amounts | its number format does not match `--format` — German by default |
| Every amount imports as a credit | the wizard is ignoring the sign of `Betrag`; map that column as a signed amount rather than as a credit-only one |
| A row you expected is missing | check `dropped` in the report; the EUR-funded leg of a split card payment is excluded on purpose (§3) |

## How the numbers are decided

**Buchungstag is the settlement date**, never the initiation date, normalised to
Europe/Vienna. Wise stamps UTC and Revolut stamps Vienna local time, so a 22:02 UTC
transaction belongs to the *next* Vienna day — which is how a transaction near a month
boundary lands in the right VAT period. Daylight saving is handled for you.

**Rates resolve in one order**: the rate recorded in the export — so a Wise card payment is
booked at what Wise actually charged you, spread included — then the ECB daily reference rate
for the Buchungstag, then the most recent published business day before it, which is always
reported and never silent. If none resolves, the run refuses rather than guessing. ECB rates
are cached between runs under `$XDG_CACHE_HOME/sevdesk-importer/`; deleting that is always
safe.

**A bank fee is folded into the booking it belongs to.** One row of your statement becomes one
booking, for the amount the balance actually moved by — a 32.00 card payment charged 0.12 of
fee is a single booking of 32.12, exactly the line the bank shows. The Verwendungszweck names
the fee (`davon 0,12 USD Gebühr` on a debit, `abzgl.` on a credit, where the fee was taken
before the money arrived), and the report records it per booking under `fee_usd`.

**Only rows that moved this account's USD balance are emitted**, so spending drawn from a
different currency balance is never booked against this account.

Amounts round half-up, and identical input produces byte-identical output — so re-running and
diffing is a valid way to check nothing changed.

## Development

```bash
just            # list every recipe
just check      # tests, types and the format check — what CI runs
just test       # unit tests, offline; extra arguments pass through
just types      # mypy, strict
just fmt        # reformat in place
just build      # build the package and run its entry point
```

Recipes expect their tools on `PATH`, so run them inside the dev shell — direnv, or
`nix develop --command just check`.

Entering the dev shell installs a pre-commit hook that runs `ruff format`; a commit whose files
it reformats is rejected, so re-stage and commit again. CI invokes the same recipes on pushes
to `main` and on every pull request, so what passes locally is what passes there.

Python 3, standard library only — the reason `nix run` starts in seconds on a cold machine.
Tests never touch the network: the ECB call is patched at the HTTP boundary against a captured
real response. Fixtures are synthetic, written to reproduce the structural quirks of real
exports without containing real data.

> **Never commit real account data.** `.gitignore` denies `*.csv` everywhere and re-admits only
> `tests/fixtures/**/*.csv`, and ignores `run/` whole — it holds real statements by definition.
> Never weaken those rules and never `git add -f` a statement.
