# 4. Data

All data is free. Market data comes from public NSE archives; the other sources are listed below. Every
number in this section was measured on the backtest window (2020-01-01 → 2026-06-30) from the project's
own files.

## 4.1 Sources

| # | Source | What we use | Granularity | Window on disk | Class |
|---|---|---|---|---|---|
| S1 | **NSE F&O bhavcopy** (nsearchives.nseindia.com). Legacy `…fo DDMMMYYYY bhav.csv.zip` before 2024-07-08; UDiFF `BhavCopy_NSE_FO_…` from 2024-07-08 | Stock options: every listed strike and expiry, OHLC, **settlement price**, contracts traded, open interest (in shares). Stock futures: settle, contracts, turnover | EOD, one row per (date, symbol, expiry, strike, CE/PE) | 2016-01-01 → 2026-09-16, 2,610 days | Exchange official |
| S2 | **NSE CM (cash) bhavcopy**, legacy and UDiFF as above | Official closing price of the underlying (EQ series) | EOD per (date, symbol) | 2016-01-01 → 2026-09-16, 2,611 days of NIFTY 50 cash EOD on disk (F&O 2016-01-01 → 2026-09-16) | Exchange official |
| S3 | **NSE corporate-action announcements** (`nseindia.com/api/corporates-corporateActions`) | Dividends, splits, bonuses, rights, demergers, buybacks: ex-date, record date, subject text, ISIN | Per event | 2019-10-01 → 2026-09-16, 704 announcements, 66 symbols | Exchange official |
| S4 | **NSE circulars** (NSE/FAOP, NCL) | Lot-size revisions (FAOP47856, FAOP53920), contract terminations (TATAMOTORS FAOP/70615), `fo_mktlots.csv` | Per circular | as cited | Exchange official, hand-transcribed |
| S5 | **NSE Indices press releases** (niftyindices.com `ind_prs*.pdf`), plus today's `ind_nifty50list.csv` | Point-in-time NIFTY 50 membership | Per change, effective date | 462 PDFs, 2016-01 → 2026-09 | Exchange official, parsed |
| S6 | **NSE / SEBI charge circulars** (`data/costs/charge_sources.csv`) | STT, exchange fees, SEBI fee, stamp duty, GST, date-effective | Per rate change | 2019-10 → current | Official, hand-transcribed |
| S7 | Yahoo Finance `^NSEI` | NIFTY 50 price index (regime labels; checked against NIFTY futures FSP) | Daily close | 2016-01-04 → 2026-07-31 (899 sessions before 2019-09-03; Yahoo has a few gaps against the NSE calendar) | Open dataset |
| S7a | **niftyindices.com** Historical Data → Total Return Index (`/BackPage/getTotalReturnIndexString`, fetched by `scripts/ingest/fetch_nifty_tri.py`; raw JSON in `data/market/raw/nifty50_tri/`) | NIFTY 50 Total Return Index (gross; net TRI kept as `ntr_close`), the buy-and-hold benchmark | Daily close | 2016-01-01 → 2026-07-31, 2,621 days | Index provider official |
| S8 | OECD `IR3TIB` (India 3-month T-bill) | Interest on idle cash; Sharpe/Sortino hurdle | Monthly | 2019-10 → 2026-05 | Open dataset |
| S9 | Zerodha Kite historical API | 15-minute bars used **only** to compute the entry ranking signal | 15-minute, split/bonus adjusted | 2016-01-04 → 2026-07-31, 61 symbols (bars before 2018-10-01 copied from the Pure Alpha Kite store `research_15m.sqlite`, 09:15–15:15 only; identical to this file on the overlap) | Broker data (free with an account, not a paid feed); cannot be rebuilt without an account |

Derived tables, all built by scripts in this project from S1–S5:
- the expiry calendar (82 monthly expiries, read from contracts rather than assumed)
- daily lot size per contract (`data/lots/`)
- detected contract adjustments (`data/backtest/corporate_actions_detected.csv`)
- the dividend table (`data/reference/dividends.csv`)
- the validated membership (`data/universe/nifty50_membership_validated.csv`)

Full provenance for each input: `data_validation/reports/DATA_INVENTORY.md`.

**Granularity used.** End of day. Options are marked at the **close when the strike traded that day, else the
exchange settlement price** (`Quote.mark` in `nse/wheel/data.py`). Expiry settles at the final settlement price = the underlying's CM close on expiry day (NSE rule); the expiring stock future's settle is a cross-check and a fallback only if the close is missing (`fsp_fallback_to_future_settle`). Over 4,625 expiry pairs the two differ 19.4% of the time (median 0.23 bps, max 20.9 bps) and no backtest assignment or call-away changes: `data/backtest/report/FSP_CHECK.md` (`scripts/wheel/fsp_check.py`). No intraday option data is used.

## 4.2 Known deficiencies

| Deficiency | Size in our window | How the backtest deals with it |
|---|---|---|
| **Stale quotes at untraded strikes.** Bhavcopy `close` is carried forward when nothing trades | **68.6%** of 20.3 M option rows have zero contracts; in **99.7%** of those `close ≠ settle` | Never mark or fill on a stale close: marks use settle when untraded. Entry needs ≥ 10 contracts and ≥ 50 contracts of OI on signal day t, and ≥ 1 contract on fill day t+1. Fill size is capped at 10% of t+1 contracts traded, with up to 3 retries |
| **Settlement prices at illiquid strikes aren't arbitrage-free** | Put settle *falls* as strike rises in **3.9%** of adjacent strike pairs | Same liquidity filters; slippage is added on every fill (`data/costs/slippage_schedule.csv`: low / base / high) |
| **No bid/ask.** EOD files carry no spread | whole window | Spread is an explicit assumption (ticks or % of price, with NSE's 2025-11-03 tick change), and results are run at three slippage levels |
| **Missing strikes** in the listed grid | **8.1%** of chain-days have a gap within ±10% of spot (40.7% anywhere in the chain, mostly wider spacing far from the money); median 41 strikes per chain | The strategy picks from listed strikes only. The target strike must be within max(1.5% × spot, the strike interval around the target), otherwise the name is skipped for that cycle (`no_eligible_strike`). Nothing is interpolated |
| **Zero or missing settle** | 1.67% of rows (1.28% also untraded) | Row is unusable: never filled on; for an open option the last valid mark is carried (`stale_marked_days`), flagged P1 after 6 days |
| **Missing F&O file 2021-03-30** (not published in the archive) | 1 day | Rebuilt from NSE's market-activity report for that day (`data/raw/fo_mkt/`, logged in `REPAIR_LOG.csv`). That report lists **traded contracts only**, so untraded strikes are absent that day |
| **No lot size in legacy files** (before 2024-07-08) | ≈ ⅔ of the window | Lot = GCD of open interest across a series' strikes, per (symbol, expiry), checked against futures turnover. 97.3% exact agreement with the exchange-published `NewBrdLotQty` where both exist; checked against NSE circulars. If a lot can't be resolved, the trade is skipped |
| **Two file layouts** (legacy → UDiFF on 2024-07-08) | — | Normalised in `nse/bhavcopy.py`; 2024-07-05 exists in both and matches |
| **Expiry stamp ≠ actual settlement** (e.g. the Jun 2023 series quoted 06-29, settled 06-28 for a holiday; expiry day moved to Tuesday in 2025) | 17 of 83 expiries aren't "last Thursday" | Contracts are joined on contract month, and the expiry date is read from the data |
| **No F&O eligibility file, no contract master** (freeze quantity, tick size) | — | F&O listing is taken as "stock future present in that day's file". Freeze limits aren't modelled; the 10% participation cap keeps orders small |
| **Prices are unadjusted** for corporate actions | 34 symbols with contract adjustments; 600 payable dividends | See §4.4 |
| **Survivorship in the signal data** (S9) | HDFC, INFRATEL, LTIM, ZEEL have no 15-minute bars for 84 member-expiries | Accepted limitation (check U-11). Main results unchanged; the bias is measured with a daily-data proxy (§4.5): CAGR moves by −1.05 to +0.23 pp. No data was fabricated |
| **Benchmark needs dividends** (S7 is a price index) | — | Resolved: NIFTY 50 buy-and-hold uses the Total Return Index (S7a). The price index is kept only for regime labels. The TRI file has 8 special sessions (Muhurat, Saturday budget/DR-drill days) that aren't in the stock calendar; the benchmark drops them. Over 2019-09-03 → 2026-07-31 the TRI returns +144.6% against +125.8% for the price index |

## 4.3 Window: 1 January 2020 → 30 June 2026 (the full requested window)

- **All of it is clean.** Every trading day (1,604) has CM and F&O data. The single missing F&O file is
  rebuilt from an exchange report, and nothing had to be dropped.
- **It includes the March 2020 crash.** NIFTY 50 fell **−38.4%** from 2020-01-14 to 2020-03-23. The window
  also has two further drawdowns:
  - **−17.2%**, 2021-10-18 → 2022-06-17 (rate-hike sell-off)
  - **−15.8%**, 2024-09-26 → 2025-03-04 (FPI-outflow correction)
  
  These, plus the April 2025 tariff shock, are the pre-declared stress windows in `params.yaml`.
- **Why not start earlier.** NSE made all stock derivatives **physically settled** only from October 2019.
  Before that, assignment didn't deliver shares, so the wheel's mechanics didn't exist. Data from
  2019-10-01 to 2019-12-31 is used only as warm-up: the first universe and signal (2019-12-31) need prior
  data, and the first fill is 2020-01-01. Starting in January keeps March 2020 in the tested period without
  any look-ahead.
- **End date.** 2026-06-30 as requested. Data on disk runs to 2026-07-31, so positions open at the end are
  marked on real prices.

## 4.4 Corporate actions

Full methodology: **`CORPORATE_ACTIONS.md`**. Summary:

- **Splits and bonuses:** detected from NSE's own contract adjustment (strikes remapped, lot changed). Every
  split and bonus NSE announced in the window matches a detection. On the ex-date: shares × m; entry price,
  economic basis and cycle references ÷ m; open option strike → a·K + b; quantity and lot × lot ratio.
- **F&O lot revisions:** lot per contract per day from the data. Revisions apply to new series; an open
  position keeps its contract's lot, and covered calls never exceed shares held.
- **Dividends in the holding leg:**
  - Cash dividends come from S3; 600 are payable.
  - Combined announcements are summed (INFY 2024-05-31: ₹20 + ₹8).
  - Each is credited on the ex-date to the shares held at the previous close.
  - On dates where NSE also adjusted the contracts, it is paid once.
  - Measured effect (same code, `rank_threshold` 1.5): ranked 1× CAGR 8.81% → 9.17%; ranked 5× 19.18% → 20.56%;
    frozen 1× 6.92% → 7.07%.
- **Rights issues:** value implied by the exchange's contract adjustment, credited as cash (entitlement
  ASSUMED renounced).
- **Demergers and mergers:**
  - RELIANCE → JIOFIN, ITC → ITCHOTELS, HINDUNILVR → KWIL: price-discovery or reference value credited
    (ASSUMED realised).
  - TATAMOTORS 2025 and HDFC 2023: NSE terminated the contracts early; positions close at that close.
- **Not modelled:** BRITANNIA 2021 bonus debenture (value unsourced), buybacks (not tendered), dividend tax.

## 4.5 Survivorship

**The universe is not selected as of today.**

- **Ranked runs.** At each monthly expiry the candidates are the NIFTY 50 members **on that date**. Membership
  is rebuilt from the dated NSE Indices press releases (S5) and validated (`data_validation` phase 1):
  - rolling back from today's official list matches it exactly;
  - no change is applied before its announcement;
  - 5,140 sampled (symbol, date) checks pass;
  - the 18 current members who joined inside the window aren't eligible before they joined.
  
  Names that later left or ceased to exist stay in the universe while they were members: YESBANK (excluded
  March 2020), ZEEL, VEDL, IBULHSGFIN, HDFC (merged 2023), INFRATEL.
- **The frozen 10-name baseline runs were retired on 2026-09-18.** Only the ranked universe is reported now.

**Why this matters:** a panel built on today's NIFTY 50 list would, for most of the window, sell puts only on
companies known to have survived and grown into the index. That lifts the put win rate and removes
assignment on collapsing names, which is the wheel's main risk. In our data such a panel would also ask
for trades on contracts that didn't exist yet: 198 of 4,100 cycle-symbol rows (ETERNAL, JIOFIN, TRENT,
SBILIFE and others) have no option chain before those names were listed or F&O-enabled.

**Remaining bias (accepted limitation U-11).** The entry ranking uses 15-minute bars (S9), which are missing
for four genuine members over part of the window:

| Name | Member-expiries missing |
|---|---|
| HDFC | 45 (2019-10 → 2023-06) |
| LTIM | 15 |
| INFRATEL | 12 |
| ZEEL | 12 |

Those names could not be selected at those expiries. The main results keep the accepted method; the ranking
was **not** changed, and no data was filled in.

**Measuring the bias** (`scripts/wheel/u11_sensitivity.py`, outputs in `data/backtest/sensitivity_u11/`).
Free historical 15-minute bars don't exist for these names: NSE publishes none, and HDFC is delisted. We
therefore did not use a stand-in stock. HDFCBANK, for example, was itself ranked on the same expiries, so
copying its bars would duplicate a signal rather than measure one. Instead:

1. **A proxy score from NSE daily data.** `rank_proxy = β × daily gap`, where the daily gap is the close's
   distance from its EMA20, computed from split-adjusted NSE CM closes. β = 0.577 is fitted on the 3,852
   (name, expiry) pairs that have both scores:
   - R² 0.948;
   - per-year β between 0.56 and 0.61;
   - same side of the entry threshold 94.5% of the time.
   
   The fit uses scores only, never returns.
2. **Scores for the 84 missing member-expiries.** 21 clear the threshold, and 17 would have entered the
   top 20.
3. **Loading the extra option chains.** INFRATEL and LTIM chains are loaded from the NSE bhavcopies already
   on disk; HDFC and ZEEL were already present. Their dividends come from NSE announcements, filed under the
   current tickers INDUSTOWER and LTM.
4. **A same-code re-run** with `rank_threshold` = 1.5.

| Leverage | CAGR, main → with proxy | Sharpe | Max drawdown | Trades in the 4 names | Their direct P&L |
|---|---|---|---|---|---|
| 1× | 9.17% → 9.07% | 0.70 → 0.66 | −7.65% → −7.70% | 12 | −₹0.24 lakh |
| 2× | 13.67% → 13.90% | 0.71 → 0.71 | −17.79% → −18.12% | 12 | −₹0.07 lakh |
| 3× | 17.98% → 17.60% | 0.76 → 0.72 | −26.18% → −26.11% | 12 | +₹0.52 lakh |
| 5× | 20.56% → 19.51% | 0.65 → 0.60 | −41.99% → −42.43% | 11 | +₹1.25 lakh |

**Reading.**
- **The bias is small and slightly flatters the reported results.** With the four names selectable, CAGR is
  0.1–1.05 percentage points lower, except at 2×, where it is 0.23 points higher.
- **ZEEL explains the direction.** It is picked on 2020-01-01, just before its collapse, and loses ₹1.5 lakh
  (1×) to ₹7.6 lakh (5×). HDFC, INFRATEL and LTIM make money.
- **Most of the CAGR change is knock-on**, not the names' own P&L: capital and margin used by these trades
  displaces other entries.
- **Caveat:** the proxy lacks the 15-minute term (about 5% of the score variance), so it measures the bias
  approximately, not exactly.
