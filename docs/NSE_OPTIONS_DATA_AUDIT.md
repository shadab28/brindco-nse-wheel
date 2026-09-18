# NSE Options Data Audit — 2016 to Present

**Audited:** 2026-09-18. **Data on disk:** raw F&O bhavcopy 2016-01-01 → 2026-09-16 (2,642 files);
warehouse `nse.options_eod` 2016-01-01 → 2026-07-31 (2,611 days, 42.4 M rows).

**Question this answers:** from 2016 onward, which parts of our NSE options data can we trust, which
parts need fixing, and what should we focus on first?

**Short answer.** The **2020-01 → 2026-06 backtest window is sound** — ingestion is faithful to the raw
files, lot sizes are right, and the option-price methodology is correct. The **2016-01 → 2019-09
stretch is not yet usable**: the symbol set was never backfilled, so a quarter of each early index is
missing, and neither corporate actions nor lot sizes were loaded for that period. Nothing here
invalidates the existing published results; it constrains how far back the window can be extended.

---

## Overall Status

```text
Data coverage:             FAIL     (2020+ PASS; 2016-01 → 2019-09 incomplete)
Bhavcopy processing:       PASS
Lot sizes:                 WARNING  (method sound; 2016-2019 lots never loaded downstream)
Corporate actions:         FAIL     (no detection before 2019-12; one live event unhandled)
Symbol changes:            FAIL     (three disagreeing alias maps; one current name losing data)
Option price adjustments:  PASS
```

---

## 1. Data coverage

### Year by year

| Year | Days | Option rows | Symbols | Members rankable per expiry | Verdict |
|---|---:|---:|---:|---:|---|
| 2016 | 246 | 2.06 M | 42 | 38.0 / ~50 | FAIL |
| 2017 | 248 | 2.17 M | 44 | 39.2 / 50 | FAIL |
| 2018 | 246 | 2.12 M | 44 | 43.5 / 50 | FAIL |
| 2019 | 244 | 2.63 M | 61 | 46.0 / 50 | WARNING |
| 2020 | 250 | 4.69 M | 63 | 47.5 / 50 | PASS |
| 2021 | 248 | 6.24 M | 66 | 49.0 / 50 | PASS |
| 2022 | 248 | 6.80 M | 68 | 49.0 / 50 | PASS |
| 2023 | 245 | 5.45 M | 69 | 49.0 / 50 | PASS |
| 2024 | 246 | 4.32 M | 72 | 49.2 / 50 | PASS |
| 2025 | 248 | 3.46 M | 72 | 50.0 / 50 | PASS |
| 2026 | 142 | 2.44 M | 71 | 50.0 / 50 | PASS (to 07-31) |

### No calendar gaps

Every raw F&O file from 2016-01-01 to 2026-07-31 is ingested. Comparing the 2,642 raw filenames against
the 2,611 distinct warehouse dates leaves exactly two differences, both already known and documented:

- **2021-03-30** is in the warehouse but has no raw zip — NSE never published it; rebuilt from the
  market-activity report (`data/raw/fo_mkt/`, `REPAIR_LOG.csv`).
- **2026-08-03 → 2026-09-16 (32 days)** are downloaded but **not ingested**. The warehouse ends
  2026-07-31 while the raw cache runs to 2026-09-16. This is the "are we lagging" answer: yes, by
  about six weeks, in the warehouse only.

### The real coverage problem: the symbol set, not the dates

Before 2019-10-01 the warehouse holds only names that are in **today's** NIFTY 50. Every other symbol
starts exactly on 2019-10-01, the original ingest start date. The raw files are not the constraint —
they carry 166 option underlyings on 2016-02-25 and 206 on 2018-06-28. Probing the raw zips directly:

| Probe date | Symbols checked | In raw NSE file | In our warehouse |
|---|---|---|---|
| 2016-02-25 | ACC, AMBUJACEM, BANKBARODA, BHEL, BOSCHLTD, CAIRN, HDFC, IDEA, LUPIN, PNB, TATAPOWER, ZEEL, AUROPHARMA, INFRATEL, TATAMTRDVR | **all 15 present** | **none** |
| 2018-06-28 | ACC, HDFC, ZEEL, LUPIN, IDEA, BANKBARODA, YESBANK, VEDL, IBULHSGFIN | **all 9 present** | **none** |

All of these were genuine NIFTY 50 members during those years. Their absence is a pure
ingestion-scope gap, fully recoverable from files already on disk — no re-download needed.

The same hole exists in the ranking signal: of the names in the index change log, **19 have no
15-minute bars at all** (ACC, AMBUJACEM, AUROPHARMA, BANKBARODA, BHEL, BOSCHLTD, BSE, CAIRN, HDFC,
HINDPETRO, IBULHSGFIN, IDEA, INFRATEL, LTIM, LUPIN, PNB, TATAMTRDVR, TATAPOWER, ZEEL). The documented
U-11 limitation covers only 4 of them (HDFC, LTIM, INFRATEL, ZEEL) and was measured on the 2019-10+
window. For 2016-2019 the hole is roughly five times larger and has never been measured.

### Chain quality degrades in the early years

| Year | Untraded rows | Zero / missing settle | Zero / missing OI |
|---|---:|---:|---:|
| 2016 | 86.0% | **9.4%** | 77.3% |
| 2017 | 85.4% | **10.6%** | 77.3% |
| 2018 | 82.2% | **8.0%** | 72.5% |
| 2019 | 79.2% | **7.8%** | 68.4% |
| 2020 | 79.3% | 0.7% | 70.6% |
| 2024 | 60.5% | 0.8% | 50.5% |
| 2026 | 55.3% | 1.2% | 41.1% |

A row with no settle is unusable, because untraded strikes are marked on settle. `DATA.md` quotes 1.67%
for the 2020-2026 window; the 2016-2017 rate is **six times worse**. Combined with 86% untraded, the
early chains are genuinely thinner, not just less ingested — the existing liquidity filters will reject
far more candidates there, which is a strategy-capacity fact rather than a bug.

---

## 2. Bhavcopy processing — PASS

### Layout changes since 2016

Scanning the header of all 2,642 raw files found exactly **three** layouts, all handled:

| Layout | Files | Window | Note |
|---|---:|---|---|
| Legacy, 16 cols | 2,096 | 2016-01-01 → 2024-07-05 | trailing comma in header |
| Legacy, 15 cols | 3 | 2019-08-28 → 2023-02-13 | same columns, no trailing comma — parsed identically |
| UDiFF, 34 cols | 543 | 2024-07-08 → 2026-09-16 | `nse/bhavcopy.py` normalises to one schema |

No unhandled format change exists in the window.

### Field-by-field spot checks (§7 of the brief)

For six eras, the most-traded put on a chosen day was traced **raw zip → warehouse → backtest cache**:

| Date | Contract | close | settle | volume | OI | Raw → DB → cache |
|---|---|---:|---:|---:|---:|---|
| 2016-06-15 | RELIANCE 960 PE, exp 2016-06-30 | 7.15 | 7.15 | 1,415 | 691,500 | exact / exact / n-a |
| 2018-09-26 | INFY 720 PE, exp 2018-09-27 | 6.05 | 6.05 | 1,843 | 411,600 | exact / exact / n-a |
| 2020-03-19 | SBIN 200 PE, exp 2020-03-26 | 15.75 | 15.75 | 7,200 | 4,056,000 | exact / exact / **exact** |
| 2022-06-22 | TATASTEEL 840 PE, exp 2022-06-30 | 27.75 | 27.75 | 18,435 | 724,625 | exact / exact / **exact** |
| 2024-10-15 | ITC 495 PE, exp 2024-10-31 | 5.60 | 5.60 | 2,134 | 1,544,000 | exact / exact / **exact** |
| 2026-05-20 | HDFCBANK 760 PE, exp 2026-05-26 | 9.85 | 9.85 | 16,348 | 2,414,500 | exact / exact / **exact** |

Symbol, expiry, strike, CE/PE, OHLC, settle, volume, OI and trading date all survive the pipeline
unchanged. ("n-a" for 2016/2018 only because the cache is built for the 2019-10+ window.)

Two further checks passed:

- **Open-interest units are consistent across the UDiFF cutover.** For 7,982 contracts alive on both
  2024-07-05 (legacy) and 2024-07-08 (UDiFF), the median OI ratio is **1.0000**. OI is in shares on both
  sides; there is no unit break for the lot-from-OI derivation to trip over.
- **`FininstrmActlXpryDt` is correctly ignored.** The UDiFF file carries an "actual expiry" column that
  disagrees with `XpryDt` on 0.03% of rows, clustered on 7 demerger dates (ITC 2025-01-03, HINDUNILVR
  2025-12-04, TATAMOTORS 2025-10-13, VEDL 2026-04-29, plus ABFRL, IDFC, SIEMENS). It looks like an early
  termination, but it is a transient NSE artefact: ITC's 2025-02-27 and 2025-03-27 series went on trading
  to their stamped expiry (63 days each, 1.44 M and 0.97 M contracts), as did HINDUNILVR's and VEDL's.
  Only TATAMOTORS genuinely stopped early (last seen 2025-10-23), and that one **is** in
  `contract_terminations.csv`. The parser's use of `XpryDt` is right, and no change is needed.

---

## 3. Lot sizes

**The derivation method is sound. The problem is that the 2016-2019 output was never loaded downstream.**

### Where each store starts

| Store | Lot coverage | Used by |
|---|---|---|
| `data/lots/contract_lots.csv` | **2016-01-28** → 2026-11-23, 24,674 series, 366 symbols | source of truth |
| `nse.contract_lots` (warehouse) | **2019-10-31** → 2026-10-27, 15,818 series | SQL queries |
| `data/backtest/cache/lots.parquet` | **2019-10-01** → 2026-07-31, 66 symbols | **the backtest** |

The 2016-2019 rebuild documented in `data/lots/lot_size_test_report.md` exists only in the CSV. The
backtest cache and the warehouse still start in October 2019. Because the engine **skips any trade whose
lot cannot be resolved**, a 2016-start run today would silently skip 2016-2019 entirely rather than fail
loudly.

### Independent validation of the 2016-2019 lots

The stored lot was compared against the lot implied by the exchange's own published futures turnover,
`turnover ÷ (contracts × close)`, over the final 7 days of every series (1,903 contract-series, 2016-01 →
2019-09) — a quantity the derivation does not read directly:

| Year | Series | Within 1% | Within 2% | Worst |
|---|---:|---:|---:|---:|
| 2016 | 492 | 99.59% | **100.00%** | 1.1% |
| 2017 | 508 | 99.41% | 99.80% | 8.2% |
| 2018 | 516 | 98.06% | 99.61% | 49.9% |
| 2019 | 387 | 98.97% | **100.00%** | 1.3% |

**1,900 of 1,903 series agree within 2%.** All three outliers are explained:

| Symbol | Expiry | Our lot | Implied | Explanation |
|---|---|---:|---:|---|
| TCS | 2018-05-31 | 500 | 250 | 1:1 bonus effective **on expiry day itself** (lot 250 → 500). Both values are right, on different days of the same series |
| BEL | 2017-09-28 | 4,950 | 4,543 | 1.1× lot revision mid-series (4,500 → 4,950) |
| ADANIENT | 2018-07-26 | 4,000 | 3,891 | 2.7%, no lot change in the series — **the only unexplained series in four years** |

### Lot table (§3 of the brief)

"NSE lot" is the exchange-published `NewBrdLotQty` where it exists (2024-07-08 onward), otherwise the
turnover-implied lot above. Circular-based checks for 2019-10+ are in `data/lots/lot_size_test_report.md`
(T2: 629/629 against `fo_mktlots.csv`; T3/T4: 345/349 against FAOP47856 and FAOP53920, 4 explained).

| Symbol | Period | Our lot size | NSE lot size | Status |
|---|---|---:|---:|---|
| RELIANCE | 2016-07 | 500 | 501 | PASS |
| RELIANCE | 2018-07 | 1000 | 1000 | PASS |
| TCS | 2016-07 | 250 | 249 | PASS |
| TCS | 2018-07 | 500 | 500 | PASS |
| INFY | 2016-07 | 500 | 500 | PASS |
| INFY | 2018-07 | 600 | 600 | PASS |
| HDFCBANK | 2016-07 | 500 | 500 | PASS |
| HDFCBANK | 2018-07 | 500 | 499 | PASS |
| ITC | 2016-07 | 2400 | 2402 | PASS |
| ITC | 2018-07 | 2400 | 2396 | PASS |
| SBIN | 2016-07 | 3000 | 3004 | PASS |
| SBIN | 2018-07 | 3000 | 2987 | PASS |
| M&M | 2016-07 | 500 | 501 | PASS |
| M&M | 2018-07 | 1000 | 999 | PASS |
| WIPRO | 2016-07 | 1000 | 999 | PASS |
| WIPRO | 2018-07 | 2400 | 2404 | PASS |
| ONGC | 2016-07 | 2500 | 2515 | PASS |
| ONGC | 2018-07 | 3750 | 3745 | PASS |
| LT | 2016-07 | 500 | 500 | PASS |
| LT | 2018-07 | 750 | 752 | PASS |
| BAJFINANCE | 2016-07 | 125 | 124 | PASS |
| BAJFINANCE | 2018-07 | 500 | 500 | PASS |
| JSWSTEEL | 2016-07 | 600 | 598 | PASS |
| JSWSTEEL | 2018-07 | 3000 | 2994 | PASS |
| ITC | 2024-10 | 1600 | 1600 (`NewBrdLotQty`) | PASS |
| HDFCBANK | 2026-05 | 550 | 550 (`NewBrdLotQty`) | PASS |
| ADANIENT | 2018-07 | 4000 | 3891 | **REVIEW** (2.7%, unexplained) |
| TATACONSUM | 2016-01 → 2019-09 | *missing* | ~1400-2000 | **FAIL** (filed under `TATAGLOBAL`, see §5) |

### Today's lot is not applied to old contracts

Confirmed three ways. Lots are stored **per (symbol, expiry)**, not per symbol, and 24,674 series carry
lot changes over time; `data/lots/lot_changes.csv` records **67 corporate-action lot changes before
2019-10** alone. The engine re-reads the lot on the fill day (`engine.py:814`) and refuses a call top-up
whose lot has changed (`topup_lot_mismatch`, `engine.py:829`). Spot values match the era, not today:
RELIANCE is 500 in 2016 and 1,000 in 2018 (1:1 bonus 2017-09-07); INFY is 500 in 2016 and 600 in 2018.

### Lot size is not multiplied twice

Checked in code and in output. In `engine.py`, `qty = lots × lot` is computed once (line 858) and every
downstream cash and P&L line uses `price × qty` — `premium` (874), `option_sale_costs` (875),
`put_assignment_pay_strike` (558), `call_away_receive_strike` (584), `option_buyback` (652). Nothing
multiplies by `lot` a second time. Verified against the `ranked_L1` trade log:

- `quantity == lots × lot_size` on **1,024 / 1,024** trades
- `premium_inr == entry_premium × quantity` on **1,024 / 1,024** trades
- per-lot notional (`strike × lot_size`) has a median of **₹7.5 lakh**, 1st percentile ₹4.1 lakh, 99th
  ₹12.5 lakh — squarely in SEBI's minimum-contract-size band. A factor-of-two error either way would put
  the median at ₹3.7 lakh or ₹15 lakh.

---

## 4. Corporate actions

### Nothing is detected before December 2019

`data/backtest/corporate_actions_detected.csv` holds 79 events spanning **2019-12-05 → 2026-07-03**.
There are **zero** events before that, and the dividend table starts **2019-10-17**.

But the events exist. `data/lots/lot_changes.csv`, which was rebuilt from 2016, flags **33
contract-adjusting corporate actions on NIFTY-50 names in 2016-01 → 2019-09**, each with a lot ratio and
a matching price gap:

| Ex-date | Symbol | Lot | Price ratio | Ex-date | Symbol | Lot | Price ratio |
|---|---|---|---:|---|---|---|---:|
| 2016-07-01 | ITC | 1600 → 2400 | 0.690 | 2017-09-28 | BHEL | 5000 → 7500 | 0.663 |
| 2016-07-13 | BPCL | 600 → 1200 | 0.490 | 2017-12-21 | M&M | 500 → 1000 | 0.483 |
| 2016-09-08 | BAJFINANCE | 125 → 1250 | 0.102 | 2018-01-31 | TATASTEEL | 1000 → 1061 | 0.920 |
| 2016-09-14 | HINDPETRO | 700 → 2100 | 0.334 | 2018-03-15 | IOC | 1500 → 3000 | 0.492 |
| 2016-10-06 | GRASIM | 150 → 750 | 0.204 | 2018-03-27 | GAIL | 2000 → 2667 | 0.760 |
| 2016-10-18 | IOC | 1500 → 3000 | 0.502 | 2018-05-31 | TCS | 250 → 500 | 0.500 |
| 2016-12-15 | ONGC | 2500 → 3750 | 0.678 | 2018-09-04 | INFY | 600 → 1200 | 0.513 |
| 2017-01-04 | JSWSTEEL | 300 → 3000 | 0.099 | 2018-11-29 | BRITANNIA | 100 → 200 | 0.509 |
| 2017-03-09 | GAIL | 1500 → 2000 | 0.751 | 2019-03-06 | WIPRO | 2400 → 3200 | 0.769 |
| 2017-03-16 | BEL | 450 → 4500 | 0.105 | 2019-03-19 | NTPC | 4000 → 4800 | 0.851 |
| 2017-06-13 | WIPRO | 1200 → 2400 | 0.493 | 2019-03-29 | IDEA | 12000 → 19868 | 0.559 |
| 2017-06-20 | ICICIBANK | 2500 → 2750 | 0.918 | 2019-04-23 | BHARTIARTL | 1700 → 1851 | 0.906 |
| 2017-07-11 | HINDPETRO | 1050 → 1575 | 0.667 | 2019-07-02 | UPL | 600 → 900 | 0.691 |
| 2017-07-13 | BPCL | 1200 → 1800 | 0.670 | 2019-07-09 | GAIL | 2667 → 5334 | 0.489 |
| 2017-07-13 | LT | 500 → 750 | 0.675 | 2019-09-19 | HDFCBANK | 250 → 500 | 0.503 |
| 2017-09-07 | RELIANCE | 500 → 1000 | 0.499 | | | | |
| 2017-09-21 | YESBANK | 350 → 1750 | 0.200 | | | | |

Since prices are deliberately unadjusted, a 2016-start backtest run today would read RELIANCE halving on
2017-09-07, TCS halving on 2018-05-31, INFY halving on 2018-09-04 and HDFCBANK halving on 2019-09-19 as
**real 50% losses**. This is the single largest correctness risk in extending the window.

### One unhandled event inside the current window

**VEDL, ex-date 2026-04-30.** Detection fires but is **rejected** (`accepted=False`): `strike_match 0.0`,
`unchanged_frac 0.0`, `price_ratio 0.351` — a **−65% price gap** where the entire old strike grid vanishes
and no affine map fits. It is not in `corporate_action_overrides.csv` and not in
`contract_terminations.csv`, so **no compensating value is credited**.

Its signature matches TATAMOTORS 2025-10-14, the only other rejected event, which was handled by early
contract termination. VEDL's contracts, however, kept trading to their stamped expiries (2026-05-26 and
2026-06-30 both ran their full course), so termination is the wrong remedy — it needs a
`value_adjustment` override like RELIANCE/JIOFIN and ITC/ITCHOTELS.

**Impact today: none.** No run holds VEDL across 2026-04-30 (checked all 7 run directories: 0 overlapping
trades). It is a latent trap — a change to `rank_threshold`, leverage or universe could select VEDL and
book a phantom 65% loss.

### What is handled correctly

Splits, bonuses, rights, dividends (600 payable), the RELIANCE/JIOFIN, ITC/ITCHOTELS and
HINDUNILVR/KWIL demergers, and the HDFC and TATAMOTORS terminations are all modelled, sourced and
documented in `CORPORATE_ACTIONS.md`. The detection is driven by **NSE's own contract adjustment**
(strikes remapped, lot changed), which is the right signal, and every split and bonus NSE announced in
the 2019-10+ window matches a detection.

---

## 5. Symbol changes — FAIL

There are **three alias maps that disagree with each other**, and only the smallest one is wired into
ingestion:

| Map | Contents | Used by |
|---|---|---|
| `nse/universe.py: SYMBOL_ALIASES` | ZOMATO→ETERNAL, TATAGLOBAL→TATACONSUM | **`canonical()` — the ingest path** |
| `nse/nifty50_history.py: RENAMES` | + TMPV, **LTI→LTIM** | universe rebuild |
| `data_validation/universe.py: CANONICAL` | + LTI→LTIM, TATAMOTORS→TMPV | validation only |
| `data/reference/ticker_aliases.csv` | TMPV, SAMMAANCAP, **LTM→LTIM**, **INDUSTOWER→INFRATEL** | dividends, tests |

Because `bhavcopy.normalise_fo` filters on the requested symbol set and folds tickers with
`canonical()`, any rename absent from `SYMBOL_ALIASES` drops the rows outright. The result, from the
warehouse's own `source_symbol` column:

| Family | In warehouse | In raw NSE files | Verdict |
|---|---|---|---|
| **LTI / LTIM / LTM** | `LTIM` only, 2023-05-02 → 2024-10-31 (372 days) | `LTI` present 2022-01-27; `LTM` present 2026-06-30 | **FAIL** — and `LTM` is the **current live ticker**, so this name is losing data *now* |
| ZOMATO / ETERNAL | 2024-11-29 → present (alias works) | ZOMATO present from Jul 2021 | WARNING — 3 years never ingested |
| TATAGLOBAL / TATACONSUM | continuous 2016-01-01 → present | — | **PASS** |
| INFRATEL / INDUSTOWER | `INFRATEL` only, to 2020-10-30 | `INDUSTOWER` present 2022-01-27 | WARNING (U-11 name) |
| IBULHSGFIN / SAMMAANCAP | to 2023-12-28, then 2025-08-29+ | absent from raw in the gap | PASS — genuine F&O de-listing, not a mapping error |
| TATAMOTORS / TMPV | clean handover at 2025-10-23 / 10-24 | — | **PASS** |

The `LTM` case is the one to fix first: `LTIM` data stops on 2024-10-31 because NSE renamed the ticker
to `LTM`, and `canonical()` has never been told. `data/lots/contract_lots.csv` already knows (`LTM`, 9
series, 2026-03-30 → 2026-11-23).

**Second defect: the lot table and the warehouse are keyed differently.** `contract_lots.csv` uses the
raw ticker as printed that day (`TATAGLOBAL` 2016-01 → 2020-04, `TATACONSUM` 2020-02 onward), while
`options_eod` uses the canonical ticker. Joining lots to options by symbol therefore loses the lot
across every rename boundary. This already reaches the live backtest cache:

| Symbol | Option data from | Lot data from | Gap |
|---|---|---|---|
| TATACONSUM | 2019-10-01 | 2020-02-27 | **~5 months inside the backtest window** |
| ETERNAL | 2024-11-29 | 2025-04-09 | ~4.5 months |

All 45 TATACONSUM series before 2019-10 have no lot under the canonical name at all.

---

## 6. Option price adjustment methodology — PASS

> **We use raw, unadjusted NSE option prices, and we mirror NSE's own contract adjustment on the
> position rather than touching any price.**

This is the correct treatment, and it is applied option-specifically, not with equity logic.

On an ex-date, `engine.py:_corporate_actions` does the following to the **position**:

- open option: `strike → a·K + b`, `qty → qty × lot_ratio`, `lot → lot × lot_ratio`
- the per-share entry basis is rescaled to post-event units: `entry_premium /= qm`, `entry_quote /= qm`,
  `last_mark /= qm`
- held shares: `shares × share_multiplier`, basis `(price − distribution) / m`
- pending orders are invalidated (`o.strike = None`) because they were decided on the pre-adjustment chain

The market data itself is never rescaled. Grepping `data.py` and `engine.py` finds no multiplication of
`close`, `settle` or `mark` by any adjustment factor — after the event the engine simply re-reads the
post-adjustment chain at the new strike, which is exactly what NSE publishes.

Against the four flags in the brief:

- **Missing adjustment** — only VEDL 2026-04-30 (§4), plus the whole of 2016-01 → 2019-11.
- **Incorrect adjustment** — none found. `(a, b)` is fitted from three candidate models and must map ≥80%
  of ≥8 open-interest strikes *and* agree with the observed price gap; OI is carried across the
  adjustment and used to break the grid-shift ambiguity.
- **Double adjustment** — none. The strike is remapped once, the price never. Dividends NSE already
  adjusted contracts for are explicitly paid once (`contract_adjusted` branch).
- **Equity logic on option prices** — **not present**, and this is the thing most often got wrong. We do
  not divide option prices by a split factor; we move the strike, which is what the exchange does.

One residual risk, not a defect: the remapped strike is `round(a·K + b, 4)`, and the engine then looks
that strike up on the new chain. If NSE's published strike differs from the fitted value by more than
rounding, the lookup would miss and the position would mark stale. The fit tolerance is 0.2% and every
accepted event maps ≥80% of OI strikes, so this has not bitten — but it is worth an assertion.

---

## Most Important Findings

### 1. 2016-2019 option data covers only today's index members — HIGH

```text
Issue:             Pre-2019-10-01 the warehouse holds only current NIFTY 50 names; every other
                   symbol starts exactly on 2019-10-01
Period:            2016-01-01 → 2019-09-30
Affected symbols:  ACC, AMBUJACEM, AUROPHARMA, BANKBARODA, BHEL, BOSCHLTD, CAIRN, HDFC, HINDPETRO,
                   IBULHSGFIN, IDEA, INFRATEL, LUPIN, PNB, TATAMTRDVR, TATAPOWER, VEDL, YESBANK, ZEEL
What is wrong:     All present in the raw zips on disk (166-206 underlyings/day), never ingested
Potential impact:  Severe survivorship bias — only 38 of ~50 members rankable per 2016 expiry (76%).
                   The one thing the project's own §4.5 is most careful about is broken before 2019-10
How hard to fix:   Easy, no downloads: re-run ingest.py with the historical universe over the
                   cached raw files
Priority:          HIGH
```

### 2. No corporate-action detection before December 2019 — HIGH

```text
Issue:             corporate_actions_detected.csv starts 2019-12-05; dividends start 2019-10-17
Period:            2016-01-01 → 2019-11-30
Affected symbols:  33 contract-adjusting events, incl. RELIANCE (2017-09-07, 1:1),
                   TCS (2018-05-31, 1:1), INFY (2018-09-04, 1:1), HDFCBANK (2019-09-19, 1:2),
                   M&M, WIPRO, ITC, BAJFINANCE (1:10), JSWSTEEL (1:10), BEL (1:10), IOC, GAIL, BPCL
What is wrong:     Prices are deliberately unadjusted, so with no detection a 1:1 bonus reads as a
                   real 50% loss. The events are already listed in data/lots/lot_changes.csv
Potential impact:  Catastrophic and one-directional for a 2016-start run. Also no dividend income
                   credited for ~3.75 years
How hard to fix:   Moderate — run the existing detect() over a 2016-backfilled cache; re-pull the
                   NSE corporate-actions API from 2016
Priority:          HIGH
```

### 3. 2016-2019 lot sizes never reached the warehouse or the backtest cache — HIGH

```text
Issue:             contract_lots.csv covers 2016-01-28+, but nse.contract_lots and
                   data/backtest/cache/lots.parquet both still start 2019-10
Period:            2016-01-28 → 2019-09-30
Affected symbols:  all
What is wrong:     The documented 2016 rebuild exists only in the CSV
Potential impact:  The engine skips any trade with an unresolved lot, so a 2016-start run would
                   silently produce almost no trades for 3.75 years rather than failing loudly
How hard to fix:   Easy — reload nse.contract_lots and rebuild the cache from lot_daily.csv.gz
Priority:          HIGH
```

### 4. LTM → LTIM alias missing: a current name is losing data now — HIGH

```text
Issue:             canonical() knows only ZOMATO and TATAGLOBAL. NSE renamed LTIM to LTM, so those
                   rows fall outside the symbol filter and are dropped
Period:            LTIM ends 2024-10-31 in the warehouse; LTM is present in raw files through 2026-09-16
Affected symbols:  LTIM/LTM (ongoing); LTI and MINDTREE pre-Nov-2022 never ingested
What is wrong:     Four alias maps disagree; only the smallest is wired into ingestion
Potential impact:  ~18 months of a live F&O name missing, and growing daily. Distorts the U-11
                   sensitivity, which is built on this exact name
How hard to fix:   Easy — one entry, then re-ingest. Better: make ticker_aliases.csv the single source
Priority:          HIGH
```

### 5. VEDL 2026-04-30: a −65% price gap with no compensating adjustment — MEDIUM

```text
Issue:             Detection fires and is rejected (strike_match 0.0, price_ratio 0.351); no entry in
                   corporate_action_overrides.csv or contract_terminations.csv
Period:            ex-date 2026-04-30 (inside the live window)
Affected symbols:  VEDL
What is wrong:     Same signature as TATAMOTORS 2025-10-14, but VEDL's contracts kept trading to their
                   stamped expiries, so it needs a value_adjustment, not a termination
Potential impact:  None today — no run holds VEDL across the date (0 overlapping trades in all 7 runs).
                   A latent phantom 65% loss if a threshold, leverage or universe change selects it
How hard to fix:   Easy once the distributed value is sourced (one override row)
Priority:          MEDIUM
```

### 6. Lot table keyed on the raw ticker, warehouse on the canonical ticker — MEDIUM

```text
Issue:             contract_lots.csv files lots under the ticker printed that day (TATAGLOBAL), while
                   options_eod uses the canonical ticker (TATACONSUM)
Period:            every rename boundary; two land inside the live window
Affected symbols:  TATACONSUM (options from 2019-10-01, lots only from 2020-02-27),
                   ETERNAL (options from 2024-11-29, lots from 2025-04-09), plus all 45 pre-2019
                   TATACONSUM series
What is wrong:     The lot join silently misses, and the engine skips rather than errors
Potential impact:  ~5 months of TATACONSUM inside the backtest window cannot be traded for the wrong
                   reason. Small now, systematic
How hard to fix:   Easy — apply canonical() when building the lot table
Priority:          MEDIUM
```

### 7. Warehouse is six weeks behind the raw cache — MEDIUM

```text
Issue:             32 trading days downloaded but not ingested
Period:            2026-08-03 → 2026-09-16
Affected symbols:  all
What is wrong:     nse.options_eod ends 2026-07-31; data/raw/fo runs to 2026-09-16
Potential impact:  None on the declared 2026-06-30 window; blocks any extension and makes "current"
                   queries quietly stale
How hard to fix:   Trivial — ingest.py --start 2026-08-01 --end 2026-09-16 (files already cached)
Priority:          MEDIUM
```

### 8. Early-year chain quality is materially worse than the documented figure — MEDIUM

```text
Issue:             Zero/missing settle is 7.8-10.6% in 2016-2019 against 1.67% documented for
                   2020-2026; 86% of 2016 rows never trade
Period:            2016-01 → 2019-09
Affected symbols:  all
What is wrong:     Not a bug — thinner early chains. But the marking rule depends on settle, so
                   unusable rows are ~6x more common than DATA.md's headline number
Potential impact:  Far fewer eligible entries and more stale marks in a 2016-start run. Affects
                   capacity and comparability across the window, not correctness
How hard to fix:   Not fixable (it is the data). Needs measuring and disclosing per year
Priority:          MEDIUM
```

### 9. The ranking signal has a much larger hole before 2019 than U-11 admits — MEDIUM

```text
Issue:             19 index-change-log names have no 15-minute bars; documented U-11 covers 4
Period:            worst in 2016-2019
Affected symbols:  ACC, AMBUJACEM, AUROPHARMA, BANKBARODA, BHEL, BOSCHLTD, CAIRN, HDFC, HINDPETRO,
                   IBULHSGFIN, IDEA, INFRATEL, LUPIN, PNB, TATAMTRDVR, TATAPOWER, ZEEL (+ BSE, LTIM)
What is wrong:     U-11 was measured on 2019-10+ (84 member-expiries, 4 names). For 2016-2019 the hole
                   is ~5x larger and unmeasured
Potential impact:  Only 38/50 members rankable in 2016. Free 15-minute history for these names does
                   not exist, so the daily-EMA proxy (R² 0.948) would have to carry far more weight
How hard to fix:   Hard — the data is not obtainable. The proxy can be extended and disclosed
Priority:          MEDIUM
```

### 10. ADANIENT 2018-07 lot disagrees by 2.7% with no explanation — LOW

```text
Issue:             Our 4,000 against a turnover-implied 3,891, with no lot change in the series
Period:            2018-07-26 expiry
Affected symbols:  ADANIENT
What is wrong:     The only one of 1,903 pre-2019 series whose disagreement has no cause
Potential impact:  Negligible — one contract-month, 2.7%
How hard to fix:   Easy to investigate, needs an external source for the 2018 lot
Priority:          LOW
```

---

## Where To Focus

### 🔴 Fix before trusting a 2016-start backtest

These four make a 2016-start run wrong rather than merely incomplete. The current 2020-2026 results do
**not** depend on any of them.

1. **Backfill the symbol universe for 2016-01 → 2019-09** (finding 1). Everything needed is already in
   `data/raw/fo/`. Without it the early years are survivorship-biased in exactly the way the project
   otherwise goes to great lengths to avoid.
2. **Extend corporate-action detection and dividends back to 2016** (finding 2). Until then, RELIANCE,
   TCS, INFY and HDFCBANK bonuses read as 50% losses. `data/lots/lot_changes.csv` already holds the
   33-event list to validate against.
3. **Load the 2016-2019 lots into the warehouse and backtest cache** (finding 3). Otherwise the engine
   silently skips the period instead of failing.
4. **Add `LTM → LTIM`, and collapse the four alias maps into one** (finding 4). This one is losing live
   data today, independent of any window change.

### 🟡 Should fix

5. **VEDL 2026-04-30 override** (finding 5) — harmless right now, a phantom 65% loss the moment the
   selection changes.
6. **Key the lot table on the canonical ticker** (finding 6) — recovers ~5 months of TATACONSUM inside
   the live window.
7. **Ingest 2026-08-01 → 2026-09-16** (finding 7) — one command, files already on disk.
8. **Publish the per-year settle/liquidity table** (finding 8) and **extend the U-11 proxy to the
   pre-2019 names** (finding 9) before quoting any 2016-start result.
9. **Assert that a post-adjustment strike is found on the new chain** (§6) rather than relying on the
   fit tolerance.
10. **Resolve ADANIENT 2018-07** (finding 10).

### 🟢 Already reliable — no further work

- **Bhavcopy ingestion fidelity.** Three layouts in 2,642 files, all handled; 6/6 end-to-end spot checks
  across 2016, 2018, 2020, 2022, 2024 and 2026 match the raw file exactly on symbol, expiry, strike,
  CE/PE, OHLC, settle, volume and OI.
- **Trading-day coverage.** No interior gap 2016-01-01 → 2026-07-31. The one missing NSE file
  (2021-03-30) is rebuilt and logged.
- **Open-interest units across the 2024-07-08 UDiFF cutover.** Median ratio 1.0000 over 7,982 contracts.
- **The `FininstrmActlXpryDt` trap.** Investigated and correctly ignored — ITC, HINDUNILVR and VEDL
  series all traded to their stamped expiry; only TATAMOTORS truly terminated, and that is modelled.
- **Lot-size derivation method.** 1,900/1,903 pre-2019 series within 2% of the exchange's own
  turnover-implied lot, all three outliers explained; 629/629 against `fo_mktlots.csv` and 345/349
  against the FAOP circulars for 2019-10+.
- **Historical lot application.** Lots are per (symbol, expiry), re-read on the fill day, with 67
  pre-2019 corporate-action lot changes recorded. Today's lot is not applied to old contracts.
- **No double lot multiplication.** `quantity == lots × lot_size` and
  `premium_inr == entry_premium × quantity` on 1,024/1,024 trades; per-lot notional median ₹7.5 lakh.
- **Option price adjustment methodology.** Raw prices with NSE's own strike remap mirrored on the
  position — option-specific, applied once, no equity logic anywhere.
- **Corporate actions and symbol handling for 2019-10 onward**, except the two named gaps.

---

*No strategy parameter, selection rule or engine behaviour was changed by this audit. Method: raw-zip
header scan over all 2,642 files; warehouse-vs-raw day reconciliation; raw-zip symbol probes;
turnover-implied lot validation over 1,903 pre-2019 contract-series; six-era raw→warehouse→cache field
traces; code review of `nse/bhavcopy.py`, `nse/universe.py`, `nse/wheel/data.py`,
`nse/wheel/corporate_actions.py` and `nse/wheel/engine.py`; and arithmetic checks against the
`ranked_L1` trade log.*
