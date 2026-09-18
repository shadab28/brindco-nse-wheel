# data/

Local inputs, outputs and caches. Gitignored.

- **Can't be rebuilt:** `raw/`, `market/nifty50_15m.db` (needs a Kite account) and the hand-made files marked *manual*.
- **Everything else** can be rebuilt with the scripts listed.

Provenance for every backtest input is in `data_validation/reports/DATA_INVENTORY.md`.

| Folder | File | What it holds | Produced by |
|---|---|---|---|
| `raw/` | `cm/`, `fo/` | NSE cash and F&O bhavcopy zips, one per trading day (`YYYY-MM-DD.csv.zip`) | `scripts/ingest/ingest.py` (`nse/bhavcopy.py`) |
| | `fo_mkt/` | 2021-03-30 F&O gap: neighbouring zips + a reconstructed bhavcopy | manual recovery |
| `market/` | `nifty50_15m.db` | 15-minute bars, SQLite table `ohlcv_15m` | Zerodha Kite download |
| | `nifty50_daily_eod.csv`, `nifty50_daily_eod_close_matrix.csv` | daily cash closes (long format / date × symbol) | `scripts/ingest/export_daily.py` |
| | `nifty50_index_daily.csv` | NIFTY 50 price index, the benchmark | Yahoo Finance ^NSEI |
| | `india_rf_3m_tbill_monthly.csv` | India 3M T-bill rate by month: interest on idle cash, Sharpe/Sortino hurdle | OECD IR3TIB |
| `universe/` | `nifty50_membership_validated.csv` | **historical NIFTY 50 membership used by the backtest** | `data_validation` Phase 1 (NSE Indices press releases) |
| | `nifty50_constituents_daily.csv`, `nifty50_changes.csv`, `nifty50_master_list.csv` | daily list, changes and per-symbol periods | `scripts/universe/nifty50_constituents.py` |
| | `sector_map.csv` | symbol → sector, used for concentration reports | manual (ASSUMED) |
| `calendar/` | `nifty50_monthly_expiries.csv` | monthly expiries read from contract data | derived from `raw/fo` |
| | `contract_terminations.csv` | merger/demerger contract terminations (HDFC, TATAMOTORS) | manual |
| | `corporate_action_overrides.csv` | demerger value adjustments F&O didn't reflect (RELIANCE→JIOFIN, ITC→ITCHOTELS, HINDUNILVR→KWIL) | manual (ASSUMED values) |
| `reference/` | `nse_corporate_announcements.csv`, `dividends.csv` | every NSE corporate-action announcement parsed; cash dividends per (symbol, ex-date) | `fetch_nse_ca.py` + `backtest.py dividends` |
| | `ticker_aliases.csv` | NSE ticker -> warehouse ticker for renamed companies (same ISIN) | manual |
| | `verified_corporate_actions.csv`, `verified_lot_sizes.csv` | hand-verified splits/bonuses and lot sizes (test references) | manual |
| `lots/` | `lot_daily.csv.gz`, `contract_lots.csv`, `lot_changes.csv`, `lot_at_monthly_expiry*.csv`, `wheel_lot_impact.csv` | lot-size history | `scripts/wheel/lot_sizes.py` |
| | `lot_size_test_report.md`, `validation/` | lot-size check against NSE circulars FAOP47856 and FAOP53920 and `fo_mktlots` (PASS) | lot-size validation |
| `costs/` | `wheel_charges_schedule.csv`, `charge_sources.csv`, `wheel_charges_final.md` | date-effective charges, their sources, and the formulas | manual, from NSE/SEBI circulars |
| | `slippage_schedule.csv` | slippage ticks / % / equity bps by level, with the 2025-11-03 tick change | manual |
| `signals/` | `expiry_rankings.csv` | every NIFTY 50 member ranked (`rank_final`) on each monthly expiry | `scripts/wheel/expiry_rankings.py` |
| `backtest/` | `cache/` | parquet extracts of Postgres (cash, futures, options, lots) + `market.pkl` | `backtest.py cache` |
| | `corporate_actions_detected.csv` | splits, bonuses, dividends and demergers found in F&O strike adjustments | `backtest.py detect-ca` |
| | `runs/<run_id>/` | `trades`, `daily`, `cash_ledger`, `events`, `wheel_cycles`, `selection_log`, `liquidity_audit`, `metrics.json`, `config.json` | `backtest.py run`, `threshold_compare.py` |
| | `report/` | `REPORT.md`, `VALIDATION.md`, comparison CSVs, charts; `threshold_L5/` | `backtest.py run`, `threshold_compare.py` |
| `logs/` | `*.log`, `download_log.csv` | run logs and per-day download status | the scripts above |

`report/` and `runs/` are only as current as their last run. Compare each run's `config.json` with
`params.yaml` before quoting any result.
