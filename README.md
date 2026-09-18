# Brindco — NSE wheel strategy: data warehouse and leveraged backtest

**Start here: [`final_results/README.md`](final_results/README.md).** It has the chosen parameters, the best
alternative the grids found, the headline results against benchmarks, and how to rebuild every number
(`python scripts/final_results.py`).

```
final_results/     the deliverable: chosen vs best params, headline tables, charts, full analysis (generated)
docs/              methodology: rulebook, data, corporate actions, stock selection, stop-loss, data audit
params.yaml        every parameter in force (the chosen strategy)
nse/               library: warehouse loaders, wheel engine (nse/wheel/), costs, metrics
scripts/           entry points: ingest/, universe/, corporate_actions/, wheel/, final_results.py
tests/             pytest suite (python -m pytest -q tests)
data/              inputs and generated outputs; data/backtest/runs/ and data/backtest/report/ are rebuilt by scripts
data_validation/   universe and data validation phases
sql/               warehouse schema
archive/           superseded files kept for reference; nothing imports from here
```

Two parts:

1. **Warehouse.** Loads NSE end-of-day **cash closing prices** and the **full option chain**
   (every strike and expiry) plus stock/index futures into PostgreSQL, for the NIFTY 50
   universe over 2019-10-01 → 2026-07-31.
2. **Wheel backtest.** A leveraged short put → assignment → covered call → call-away engine
   (`nse/wheel/`, `scripts/wheel/backtest.py`) that trades the top-ranked NIFTY 50 names at each
   monthly expiry. Rules: `docs/WHEEL_STRATEGY_RULEBOOK.md`. Parameters: `params.yaml`.

The window starts October 2019 because that is when NSE single-stock options
became **physically settled** — before that date the delivery mechanics the
wheel depends on did not exist.

## Reproducing the results

There are three levels, and they cost very different amounts. **Level 1 needs nothing but a clone and
pandas** — it rebuilds every number in the report from committed run outputs.

| Level | What it rebuilds | Needs | Time |
|---|---|---|---|
| **1. Report from run outputs** | `final_results/` — every table, chart and number in the report | clone + `pandas`, `pyyaml` | seconds |
| **2. Backtest from the parquet cache** | `data/backtest/runs/` and `report/` from market data | Level 1 + the parquet cache (~1 GB, rebuilt from the warehouse) | ~minutes per run |
| **3. Everything from raw NSE files** | the warehouse, the cache, then 2 and 1 | Postgres + ~1.9 GB downloaded from the NSE public archives | hours, mostly download |

### Level 1 — from a fresh clone

```bash
git clone <repo> && cd brindco-nse-wheel
pip install pandas pyyaml
python scripts/final_results.py
```

`data/backtest/runs/` (all nine runs: five leverages plus the four `rank_threshold` variants) and
`data/backtest/report/` are **committed**, so this regenerates `final_results/` — including
`results_pack/` and every figure quoted in the report — with no database and no downloads. This is the
level a reviewer should use to check that the reported numbers follow from the trade logs.

### Level 2 — re-run the backtest

```bash
python scripts/wheel/backtest.py cache    # Postgres -> data/backtest/cache/*.parquet
python scripts/wheel/backtest.py run      # all pre-declared runs + benchmarks + report
python scripts/wheel/required_analysis.py
python scripts/final_results.py
```

The parquet cache is ~1 GB and is **not** committed, so `cache` needs a loaded warehouse (Level 3).
Everything downstream of the cache is deterministic: the same cache and the same `params.yaml` reproduce
the committed runs exactly.

### Level 3 — raw NSE files to results

```bash
scripts/pg.sh start
python scripts/ingest/ingest.py --summary     # downloads ~1.9 GB of bhavcopy zips
python scripts/wheel/build_cycles.py calendar && ... lots && ... universe && ... chain
# then Level 2
```

Every bhavcopy source is a public NSE archive with no login and no paid feed (see **Schema** below for
the exact URLs). The ingest is incremental and idempotent, so an interrupted run resumes.

#### One exception: the ranking signal

`scripts/wheel/expiry_rankings.py` is the **only** step that is not reproducible from public data. It
scores each NIFTY 50 member on how far its close sits above a 15-minute EMA50 and a daily EMA20, and NSE
does not publish 15-minute bars — they come from the **Zerodha Kite Connect historical API**, which needs
an account. The bar store is 962 MB and is not committed, so:

- its **output is committed** (`data/signals/expiry_rankings.csv`, one row per stock per expiry, with
  `rank_final` and both component gaps), and every downstream step reads that file;
- Levels 1 and 2 are therefore fully reproducible, and Level 3 reproduces everything **except** this file;
- the scoring rule is fully specified in the module docstring, so the signal can be re-derived by anyone
  with any 15-minute source.

This is a documented dependency, not a hidden one, but it is a real limitation of the submission and it
is restated in the report's limitations section.

### What is deliberately not in the repo

Large regenerable artefacts are excluded (`.gitignore`) to keep the clone at ~200 MB rather than 17 GB.
Nothing here is an input you cannot rebuild or re-download:

| Excluded | Size | Rebuilt by |
|---|---|---|
| `data/pgdata/` | 11 GB | `scripts/pg.sh start` + ingest — a live Postgres cluster, not data |
| `data/raw/` | 1.9 GB | `scripts/ingest/ingest.py` (public NSE archives) |
| `data/backtest/cache/` | 988 MB | `backtest.py cache` |
| `data/backtest/sensitivity_u11/` | 987 MB | `scripts/wheel/u11_sensitivity.py` |
| `data/market/*.db` | 962 MB | 15-minute bars behind the ranking signal — **cannot be rebuilt from public files**, see below |
| `data_validation/output/raw_fo/` | 327 MB | `data_validation/raw_fo.py` from `data/raw/fo/` |
| `data/backtest/_archive_pre_step9a/` | 24 MB | superseded runs, kept locally only; not comparable with current results |

Everything the results actually depend on — the cost and slippage schedules, the lot-size history, the
expiry calendar, the validated NIFTY 50 membership, the rankings, the dividend and corporate-action
tables, and all nine run outputs — **is committed**.

## Why this is cheap to extend

The bhavcopy is a **whole-market** file: one download per day contains every
symbol. Adding the other 49 NIFTY 50 names to a RELIANCE-only pull costs **zero
extra requests** — it only changes which rows get parsed out. Raw zips are
cached under `data/raw/` (~1.4 GB for the full window), so after the first pass
every re-parse is local.

**Report sections:** data sources, deficiencies, window and survivorship in `docs/DATA.md`; corporate-action
methodology in `docs/CORPORATE_ACTIONS.md`; stock filters, the score threshold and monthly capacity in `docs/STOCK_SELECTION.md`.

## Setup

```bash
pip install pandas requests psycopg2-binary
createdb nse
cp .env.example .env              # fill in credentials
set -a; source .env; set +a
```

### This machine

The data is loaded into a **project-local Postgres 18 cluster** on port **5440**
(trust auth, database `nse`, schema `nse`). The cluster's data directory lives inside
the project at `data/pgdata` (gitignored), so the warehouse travels with the folder --
the two EDB clusters on 5432/5433 are password protected and were left untouched.

```
DATABASE_URL=postgresql://shadab@127.0.0.1:5440/nse
```

The server is started with `pg_ctl`, so it does **not** survive a reboot. Use the helper:

```bash
scripts/pg.sh start     # also: stop | restart | status
```

It runs the Homebrew `postgresql@18` binaries (override with `PGBIN=...`) against
`data/pgdata` and logs to `data/logs/postgres-5440.log`.

Do **not** run `brew services start postgresql@18`: that would point at
`/opt/homebrew/var/postgresql@18`, which no longer holds this cluster (see the
`MOVED.txt` marker left there). Because the cluster is a live 15 GB binary tree inside
the project, don't copy, zip, or cloud-sync the Brindco folder while the server is
running -- stop it first.

## Run

```bash
python scripts/ingest/ingest.py --summary                    # NIFTY 50, full window
python scripts/ingest/ingest.py --symbols RELIANCE           # one name
python scripts/ingest/ingest.py --indices                    # + NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY
python scripts/ingest/ingest.py --universe-file my_names.txt # your own list
python scripts/ingest/ingest.py --start 2026-08-01 --end 2026-09-16   # top up to date
```

`scripts/ingest/ingest.py` is **incremental and idempotent**. Every (date, segment) outcome is
recorded in `nse.ingest_log`, so a re-run skips what is already loaded, retries
only what errored, and never re-requests a known holiday. Re-ingesting a day
overwrites its rows, so a republished bhavcopy supersedes stale data. Use
`--force` to redo days regardless.

## Schema (`nse` schema, see `sql/schema.sql`)

| Table | Grain | Notes |
|---|---|---|
| `nse.cash_eod` | (date, symbol) | EQ series; `close` is the official NSE close |
| `nse.options_eod` | (date, symbol, expiry, strike, CE/PE) | partitioned by year |
| `nse.futures_eod` | (date, symbol, expiry) | stock and index futures |
| `nse.ingest_log` | (date, segment) | `ok` / `no_file` / `error` per day |
| `nse.coverage` | view | first/last date and day count per symbol |

Sources, both public NSE archives, no login and no paid feed:

- legacy (before 2024-07-08) `content/historical/{EQUITIES,DERIVATIVES}/…bhav.csv.zip`
- UDiFF (2024-07-08 onward) `content/{cm,fo}/BhavCopy_NSE_{CM,FO}_…csv.zip`

Both layouts are normalised to one schema in `nse/bhavcopy.py`.

## Data caveats that affect a backtest

1. **Use `settle`, not `close`, for option marking.** For untraded strikes
   (`contracts = 0`) the bhavcopy `close` is a stale carry-forward. This is not
   an edge case: **58% of the 506k loaded RELIANCE option rows never traded**,
   and in 99.9% of those `close <> settle`. Example: RELIANCE 1140 CE on
   2025-09-01 printed `close = 379.10` against `settle = 219.50`. Marking a
   wheel backtest on `close` invents P&L.
2. **Prices are unadjusted.** Corporate actions inside the window — the
   RELIANCE 1:1 bonus (Oct 2024), splits, and F&O lot-size revisions — are
   stored raw and must be adjusted downstream. Options carry the strike as
   traded on the day.
3. **Survivorship — solved by `scripts/universe/build_universe.py`, not by the NIFTY 50 list.**
   The built-in `NIFTY_50` list is today's membership, so backfilling it is
   biased toward survivors: only **27-37 of the 50 most option-liquid F&O
   stocks** in any given year are in it. Use the point-in-time universe below
   for anything that feeds a backtest; the NIFTY 50 list is for convenience
   exports only.
4. **Renames are aliased; structural events are not.** `nse/universe.py` folds
   pure renames into the current ticker (`ZOMATO`->`ETERNAL`,
   `TATAGLOBAL`->`TATACONSUM`) and keeps the as-printed ticker in
   `source_symbol` for auditing. It deliberately does **not** splice events that
   changed the share itself -- the HDFC/HDFCBANK merger (Jul 2023),
   SRTRANSFIN->SHRIRAMFIN (Dec 2022), and the TATAMOTORS demerger (Oct 2025).
   Consequences visible in the data: `TATAMOTORS` ends 2025-10-23, `SHRIRAMFIN`
   starts 2022-12-20, and `JIOFIN` starts at its Sep 2023 listing.
5. **No bid/ask.** EOD bhavcopy has no spread, so slippage must be an explicit
   modelling assumption.
6. **Dividends come from a separate source.** The cash table has price returns only. The holding leg
   receives cash dividends from NSE corporate-action announcements (`data/reference/dividends.csv`).
   Methodology for every corporate action: `docs/CORPORATE_ACTIONS.md`.
7. **Settlement prices at illiquid strikes are not arbitrage-free.** In the
   2020-02-28 RELIANCE chain the 1220 PE settles at 6.70 while the 1240 sits at
   22.50 and the 1200 at 14.05 -- the 1220 had open interest of 1,000. Strike
   selection should filter on OI/traded volume, not just moneyness, or the
   backtest will "sell" premium at prices nobody would have paid.

## Point-in-time universe (`scripts/universe/build_universe.py`)

Selecting on today's index membership is survivorship bias. Instead, rank the
whole F&O pool at each rebalance from the data itself:

```bash
python scripts/universe/build_universe.py scan                       # -> nse.fo_liquidity_daily
python scripts/universe/build_universe.py select --top 12 --rebalance 6M --start 2020-01-01
```

`scan` aggregates every F&O stock's daily option/futures turnover, OI and chain
breadth: **307,499 rows over 306 distinct stocks**. It is deliberately an
aggregate -- ranking needs turnover and OI, not every strike, so the pool costs
~300k rows instead of the ~88M (~22 GB) its full chains would take. Full chains
are then ingested only for names actually selected.

`select` ranks that pool and writes time-varying membership to
`nse.universe_membership`. Design points:

- **No look-ahead.** The trailing window ends *strictly before* the rebalance
  date, so selection never sees the period it is about to trade. This is why
  rebalances start 2020-01-01 rather than 2019-10-01: the first universe needs
  90 days of prior data, and starting in January keeps the March 2020 crash
  in-window.
- **Bid-ask proxy.** EOD data has no spread, so eligibility requires a minimum
  fraction of quoted strikes to actually trade (`--min-traded-frac`). A name
  quoting 80 strikes and trading 3 is illiquid exactly where you would be
  selling.
- **Rulesets are atomic.** `select` replaces its `--ruleset` wholesale; merging
  two runs on different rebalance grids would leave overlapping effective
  periods and double-count names. Variants coexist under different
  `--ruleset` names and feed the sensitivity analysis directly.

Result for `turnover_top12` (top 12 by option turnover, 6-monthly, 14
rebalances): **27 distinct names, average churn 2.3 per rebalance**. Seven are
not in today's NIFTY 50 -- BSE, DIXON, HAL, MCX, PFC, INDIGO and **HDFC**,
which merged out of existence in Jul 2023. A today's-list panel cannot see any
of them.

Query the universe as of a date:

```sql
SELECT symbol, rank FROM nse.universe_membership
WHERE ruleset = 'turnover_top12' AND %(d)s BETWEEN effective_from AND effective_to
ORDER BY rank;
```

## Wheel cycle pipeline (`scripts/wheel/build_cycles.py`)

```bash
python scripts/wheel/build_cycles.py calendar                                  # 1-2
python scripts/wheel/build_cycles.py lots                                      # 5 (lot + token)
python scripts/wheel/build_cycles.py universe --source liquidity:turnover_top12  # 3-4
python scripts/wheel/build_cycles.py chain    --source liquidity:turnover_top12  # 6-7
```

| Stage | Table | Result |
|---|---|---|
| `calendar` | `nse.expiry_calendar` | 82 monthly cycles, Oct 2019 - Jul 2026 |
| `lots` | `nse.contract_lots` | 15,818 series, lot size + instrument token |
| `universe` | `nse.cycle_universe` | eligible stocks per cycle, per source |
| `chain` | `nse.wheel_chain` | 1,840,551 backtest-ready contract-days |

**Expiries are derived, not assumed.** Stock options are monthly-only, so the
observed expiries *are* the calendar. They fall on 69 Thursdays, 13 Tuesdays
(NSE moved expiry day in 2025), 3 Wednesdays and 1 Monday -- a hardcoded "last
Thursday" rule is wrong 17 times in 83. The Jun 2023 series was quoted as
expiring 2023-06-29 for its whole life and settled 2023-06-28 (Bakri Id), so
contracts are joined on **contract month**, never on the raw expiry stamp.

**Lot sizes are recovered exactly, including revisions.** No bhavcopy carries a
lot size before Jul 2024, but open interest is denominated in shares, so the
GCD of OI across a series' strikes recovers the lot. It must be computed per
**(symbol, expiry)**: a revision applies only to newly introduced series, so
TCS in Jun 2020 carries lot 250 on the June series and 300 on July/August, and
grouping per symbol returns a meaningless common divisor. Validated against
`NewBrdLotQty`, which UDiFF publishes from 2024-07: **97.3% exact agreement**
overall, 98% on well-observed series. The residual splits into thin series
(too few strikes to pin the GCD) and series whose lot was revised *mid-life*,
where no single value is correct for the whole series -- check `lot_source`
and `n_obs` before trusting a value.

**Instrument tokens exist only from 2024-07** (`FinInstrmId`, UDiFF). They are
NULL for the earlier two thirds of the window; no source can backfill them.

**Survivorship is enforced, not documented.** `nse.wheel_chain` contains a row
only if the symbol was a member at that cycle's start, verified by constraint.
The contrast between sources makes the bias concrete:

| `constituent_source` | cycle-symbol rows | with an option chain |
|---|---|---|
| `liquidity:turnover_top12` | 936 | 99.9% |
| `nifty50_current` | 4,100 | 95.2% |

The 198 chain-less rows under `nifty50_current` are ETERNAL, JIOFIN, TRENT,
SBILIFE and others in months *before they were listed or F&O-enabled*: a
backtest on today's index list asks the strategy to sell puts on contracts that
did not exist.

**`constituent_source` is pluggable.** Neither source above is actual
historical NIFTY 50 membership. No machine-readable history exists (NSE and
niftyindices serve today's list only), so the backtest uses a history rebuilt
by hand instead: `data/universe/nifty50_membership_validated.csv`. It rolls
today's official list back through the dated NSE Indices press releases
(saved in `data_validation/sources/niftyindices/`) and is checked in
`data_validation` Phase 1.

## Wheel backtest (`scripts/wheel/backtest.py`)

```bash
python scripts/wheel/expiry_rankings.py        # rank NIFTY 50 members on each expiry -> data/signals/
python scripts/wheel/backtest.py cache         # Postgres -> data/backtest/cache/*.parquet
python scripts/wheel/backtest.py detect-ca     # corporate actions from F&O strike adjustments
python scripts/corporate_actions/fetch_nse_ca.py   # NSE corporate-action announcements (raw JSON)
python scripts/wheel/backtest.py dividends     # cash dividends for the holding leg
python scripts/wheel/backtest.py run           # pre-declared runs + benchmarks + report
python scripts/wheel/threshold_compare.py      # rank_threshold sensitivity at 5x
python scripts/wheel/selection_funnel.py       # month-by-month selection funnel at 1x and 5x
```

- **Selection:** at each monthly expiry, the top `n_positions` names in `data/signals/expiry_rankings.csv`
  with `rank_final > rank_threshold`. Rankings only cover names in the NIFTY 50 on that date.
- **Leverage:** a short put locks `strike × qty / L` of margin. Idle cash earns the monthly India 3M T-bill
  rate.
- **Cash-only assignment:** the book never borrows and cash never goes negative. An ITM put is physically
  assigned only if free cash covers `strike × qty` plus assignment costs; otherwise it is closed out for its
  intrinsic value and booked as a forced realisation loss, with no shares received. If even the close-out is
  unaffordable, held stock is force-sold the same day (uncalled positions first; a written call is bought
  back before its shares are sold). `cash >= 0` is a hard daily invariant. See Step 9a of
  `docs/WHEEL_STRATEGY_RULEBOOK.md` and section 8 of the report.
- **Runs:** `ranked_L{1,2,3,4,5}`, the ranked universe at each leverage in `leverage_grid`. Slippage sensitivity:
  `python scripts/wheel/slippage_sensitivity.py` reruns every leverage at levels `zero|low|base|high|stress`
  (`data/costs/slippage_schedule.csv`) into `data/backtest/report/slippage_sensitivity/`.
  Brief §7 (per-underlying metrics, wheel diagnostics, P&L split, benchmarks, stress walk-through, strike
  sensitivity, biases): `python scripts/wheel/required_analysis.py` → `data/backtest/report/analysis/ANALYSIS.md`.
  `ranked_L5_fin0` (0% financing) was retired with the cash-only
  rule: borrowing can no longer occur, so it produced output bit-identical to `ranked_L5`. The pre-rule runs
  are archived in `data/backtest/_archive_pre_step9a/` and are **not** comparable with current results.
- **Outputs:** `data/backtest/runs/<run_id>/` (trades, daily NAV, cash ledger, events, metrics) and
  `data/backtest/report/` (`REPORT.md`, `VALIDATION.md`, CSVs, charts).

Every parameter comes from `params.yaml`. Costs come from `data/costs/`.

## Tests and data validation

```bash
python -m pytest tests/                           # engine, data, liquidity, call priority, universe exit
python -m data_validation.run --phase 0           # input inventory  -> data_validation/reports/
python -m data_validation.run --phase 1           # historical NIFTY 50 universe
```

A phase refuses to start unless every earlier phase passed. The reports are generated, so don't edit
them by hand.

## Layout

```
final_results/       generated deliverable (python scripts/final_results.py)
docs/                methodology docs (rulebook, DATA, CORPORATE_ACTIONS, STOCK_SELECTION, STOP_LOSS, data audit)
params.yaml          every backtest parameter (dates, capital, N, threshold, leverage, file paths)
sql/schema.sql       tables, partitions, indexes, ingest log
scripts/             run everything from the project root: python scripts/<group>/<file>.py
  ingest/
    ingest.py              CLI: download → normalise → upsert
    export_daily.py        single-CSV export of daily cash prices
  universe/
    build_universe.py      point-in-time liquidity scan + universe selection
    nifty50_constituents.py  daily NIFTY 50 list + addition/deletion log
  wheel/
    build_cycles.py        expiry calendar, lot sizes, cycle universe, wheel chain
    lot_sizes.py           lot-size history and wheel lot impact
    expiry_rankings.py     rank NIFTY 50 stocks on each monthly expiry
    backtest.py            cache / detect-ca / run: leveraged wheel backtest + report
    threshold_compare.py   rank_threshold sensitivity at 5x
nse/                 shared library imported by the scripts
  bhavcopy.py          fetch, cache, and normalise both bhavcopy layouts
  db.py                COPY-based bulk upsert
  universe.py          NIFTY 50 list, symbol aliases, survivorship notes
  nifty50_history.py   dated NIFTY 50 index changes (edit on each review)
  liquidity.py         per-symbol daily F&O activity aggregates
  contracts.py         lot-size recovery and instrument tokens
  wheel/               backtest engine
    data.py              market data from Postgres, cached as parquet
    selection.py         ranked (top-N over threshold) and frozen universes
    engine.py            daily loop: margin, fills, expiry, assignment, calls, liquidation, reconcile
    costs.py             date-effective charges and slippage from data/costs/
    corporate_actions.py detect splits, bonuses, dividends, demergers from F&O adjustments
    runner.py            wire one run together and write its tables
    metrics.py           performance, capital efficiency, risk
    benchmarks.py        NIFTY 50 buy-and-hold, equal-weight top-N
tests/               pytest suite (synthetic + real-data stages)
data_validation/     gated data checks: phase code, reports/, sources/ (NSE press releases), output/
data/                inputs and outputs; the large regenerable parts are gitignored (see Reproducing the results)
  raw/               cached bhavcopy zips (cm, fo)
  market/            15m bars DB, daily EOD exports
  universe/          NIFTY 50 membership, changes, master list
  calendar/          monthly expiry calendar
  lots/              lot-size history
  costs/             date-effective charges, slippage schedule, sources
  signals/           expiry rankings
  backtest/          parquet cache, detected corporate actions, runs/, report/
  logs/              run and download logs
```
