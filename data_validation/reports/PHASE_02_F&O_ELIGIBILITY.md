# Phase 2 — F&O Eligibility

**Generated:** 2026-09-17T18:02:36  
**Gate:** FAIL  
**Checks:** 13 run, 9 passed, 4 failed (2 critical)

## Data sources

Raw NSE F&O bhavcopy zips `data/raw/fo/*.csv.zip` (1,718 files: legacy fo*bhav.csv to 2024-07-05, UDiFF BhavCopy_NSE_FO from 2024-07-08), read directly by `data_validation/raw_fo.py` (independent of the warehouse and cache). 2021-03-30 has no bhavcopy on disk; `data/raw/fo_mkt/2021-03-30_reconstructed_bhav.csv` was rebuilt from the NSE F&O market-activity report `fo30032021.zip`. Checked: `data/backtest/cache/{futures,options}.parquet`, backtest trades at 1/2/3/5× (`data_validation/output/backtest/`).

## Methodology

A stock is F&O-eligible on a trading day when that day's NSE file lists at least one stock future (FUTSTK/STF) and one stock option (OPTSTK/STO) on it. Eligibility windows are maximal runs of consecutive exchange trading days (CM bhavcopy calendar). Every option the backtest traded is checked on its entry day and exit day against the exact listed contract (symbol, expiry, strike, CE/PE). The cache is anti-joined against the raw files row by row. Warehouse ticker renames (ZOMATO→ETERNAL, TATAGLOBAL→TATACONSUM) are applied to raw tickers before comparing.

## Tests run

F-01..F-10 in data_validation/phase02_fo_eligibility.py; tests/test_dv_fo_eligibility.py (pytest).

## Results

| ID | Check | Critical | Checked | Failed | Status | Detail |
|---|---|---|---:|---:|---|---|
| F-01 | Every CM trading day has an NSE F&O file (original or logged reconstruction) | yes | 1,687 | 0 | PASS | 1718 original F&O files; CM days in window 1687; days without an original file: 2021-03-30 |
| F-01b | No F&O file on a day without a CM session | yes | 1,718 | 0 | PASS |  |
| F-01c | Each F&O file's embedded trade date equals its file date | yes | 1,718 | 0 | PASS |  |
| F-01d | 2021-03-30 reconstruction is complete (all listed contracts, settlement prices) | no | 1 | 1 | FAIL | Logged in REPAIR_LOG.csv. Non-critical here; its effect on marks and trades is checked in F-04 and phase 6. |
| F-02 | Stock futures and options are listed together (no one-sided underlying-days) | no | 307,655 | 0 | PASS | 304 stocks eligible at some point; 317 eligibility windows |
| F-03 | Engine F&O-listed flag (cache futures) equals raw NSE eligibility for backtest symbols | yes | 99,938 | 10 | FAIL (CRITICAL) |  |
| F-04 | Underlying was F&O-eligible on every backtest option entry day | yes | 2,912 | 0 | PASS |  |
| F-05 | Exact option contract was listed on every backtest entry day | yes | 2,912 | 58 | FAIL (CRITICAL) |  |
| F-06 | Exact contract was listed on every bought-back exit day (margin / universe exits) | yes | 2,912 | 0 | PASS |  |
| F-07 | Contract listed on every day a position was held (marks) | no |  | 581 | FAIL | days: 2020-05-04, 2020-05-05, 2020-05-06, 2020-05-07, 2020-05-08, 2020-05-11, 2020-05-12, 2020-05-13, 2020-05-14, 2020-05-15, 2020-05-18, 2020-05-19, 2020-05-20, 2020-05-21, 2020-05-22, 2020-05-26, 2020-05-27, 2020-05-28, 2020-06-26, 2020-06-29. A missing day carries the last mark (engine stale-mark rule). |
| F-08 | Every cached option row exists in the raw NSE file for that day (no options outside eligibility) | yes | 21,092,893 | 0 | PASS |  |
| F-09 | No cached option row on a day its underlying was not F&O-eligible | yes | 21,092,893 | 0 | PASS |  |
| F-10 | F&O start/end events for traded symbols inside the window (informational) | no | 58 | 8 | INFO |  |

## Failures

### F-01d — 2021-03-30 reconstruction is complete (all listed contracts, settlement prices) (FAIL)

Logged in REPAIR_LOG.csv. Non-critical here; its effect on marks and trades is checked in F-04 and phase 6.

1 failing record(s); first 1:

```
[
 {
  "stock_option_rows": 4838,
  "stock_future_rows": 383,
  "settle_missing_rows": 5221,
  "note": "market-activity report lists traded contracts only; untraded strikes and all settlement prices absent"
 }
]
```

### F-03 — Engine F&O-listed flag (cache futures) equals raw NSE eligibility for backtest symbols (FAIL (CRITICAL))



10 failing record(s); first 10:

```
[
 {
  "date": "2021-03-30",
  "symbol": "BPCL",
  "cache": false,
  "raw": true
 },
 {
  "date": "2021-03-30",
  "symbol": "BRITANNIA",
  "cache": false,
  "raw": true
 },
 {
  "date": "2021-03-30",
  "symbol": "DIVISLAB",
  "cache": false,
  "raw": true
 },
 {
  "date": "2021-03-30",
  "symbol": "GAIL",
  "cache": false,
  "raw": true
 },
 {
  "date": "2021-03-30",
  "symbol": "IBULHSGFIN",
  "cache": false,
  "raw": true
 },
 {
  "date": "2021-03-30",
  "symbol": "IOC",
  "cache": false,
  "raw": true
 },
 {
  "date": "2021-03-30",
  "symbol": "SHREECEM",
  "cache": false,
  "raw": true
 },
 {
  "date": "2021-03-30",
  "symbol": "UPL",
  "cache": false,
  "raw": true
 },
 {
  "date": "2021-03-30",
  "symbol": "VEDL",
  "cache": false,
  "raw": true
 },
 {
  "date": "2021-03-30",
  "symbol": "ZEEL",
  "cache": false,
  "raw": true
 }
]
```

### F-05 — Exact option contract was listed on every backtest entry day (FAIL (CRITICAL))



58 failing record(s); first 25:

```
[
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "entry": "2020-05-04",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "ITC",
  "entry": "2020-06-26",
  "contract": "2020-07 179.85 PE"
 },
 {
  "lev": "L1",
  "symbol": "EICHERMOT",
  "entry": "2020-07-31",
  "contract": "2020-08 2000.0 PE"
 },
 {
  "lev": "L1",
  "symbol": "BPCL",
  "entry": "2021-08-30",
  "contract": "2021-09 387.0 PE"
 },
 {
  "lev": "L1",
  "symbol": "COALINDIA",
  "entry": "2021-11-26",
  "contract": "2021-12 166.0 CE"
 },
 {
  "lev": "L1",
  "symbol": "BAJAJFINSV",
  "entry": "2022-08-26",
  "contract": "2022-09 1725.0 CE"
 },
 {
  "lev": "L1",
  "symbol": "COALINDIA",
  "entry": "2023-01-27",
  "contract": "2023-02 234.75 CE"
 },
 {
  "lev": "L1",
  "symbol": "ITC",
  "entry": "2023-05-29",
  "contract": "2023-06 410.5 PE"
 },
 {
  "lev": "L1",
  "symbol": "BPCL",
  "entry": "2023-12-04",
  "contract": "2023-12 394.0 PE"
 },
 {
  "lev": "L1",
  "symbol": "TECHM",
  "entry": "2024-07-03",
  "contract": "2024-07 1652.0 CE"
 },
 {
  "lev": "L1",
  "symbol": "BPCL",
  "entry": "2024-07-29",
  "contract": "2024-08 299.5 PE"
 },
 {
  "lev": "L1",
  "symbol": "WIPRO",
  "entry": "2024-11-29",
  "contract": "2024-12 335.0 CE"
 },
 {
  "lev": "L1",
  "symbol": "WIPRO",
  "entry": "2024-12-27",
  "contract": "2025-01 329.0 CE"
 },
 {
  "lev": "L1",
  "symbol": "SHRIRAMFIN",
  "entry": "2024-12-27",
  "contract": "2025-01 680.0 CE"
 },
 {
  "lev": "L1",
  "symbol": "TATASTEEL",
  "entry": "2025-06-02",
  "contract": "2025-06 148.9 PE"
 },
 {
  "lev": "L1",
  "symbol": "NESTLEIND",
  "entry": "2025-08-01",
  "contract": "2025-08 1300.0 CE"
 },
 {
  "lev": "L2",
  "symbol": "RELIANCE",
  "entry": "2020-05-04",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L2",
  "symbol": "ITC",
  "entry": "2020-06-26",
  "contract": "2020-07 179.85 PE"
 },
 {
  "lev": "L2",
  "symbol": "EICHERMOT",
  "entry": "2020-07-31",
  "contract": "2020-08 2000.0 PE"
 },
 {
  "lev": "L2",
  "symbol": "BPCL",
  "entry": "2021-08-30",
  "contract": "2021-09 387.0 PE"
 },
 {
  "lev": "L2",
  "symbol": "COALINDIA",
  "entry": "2021-11-26",
  "contract": "2021-12 166.0 CE"
 },
 {
  "lev": "L2",
  "symbol": "BAJAJFINSV",
  "entry": "2022-08-26",
  "contract": "2022-09 1725.0 CE"
 },
 {
  "lev": "L2",
  "symbol": "COALINDIA",
  "entry": "2023-01-27",
  "contract": "2023-02 234.75 CE"
 },
 {
  "lev": "L2",
  "symbol": "ITC",
  "entry": "2023-05-29",
  "contract": "2023-06 410.5 PE"
 },
 {
  "lev": "L2",
  "symbol": "BPCL",
  "entry": "2023-12-04",
  "contract": "2023-12 394.0 PE"
 }
]
```

### F-07 — Contract listed on every day a position was held (marks) (FAIL)

days: 2020-05-04, 2020-05-05, 2020-05-06, 2020-05-07, 2020-05-08, 2020-05-11, 2020-05-12, 2020-05-13, 2020-05-14, 2020-05-15, 2020-05-18, 2020-05-19, 2020-05-20, 2020-05-21, 2020-05-22, 2020-05-26, 2020-05-27, 2020-05-28, 2020-06-26, 2020-06-29. A missing day carries the last mark (engine stale-mark rule).

581 failing record(s); first 25:

```
[
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-04",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-05",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-06",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-07",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-08",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-11",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-12",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-13",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-14",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-15",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-18",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-19",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-20",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-21",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-22",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-26",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-27",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "RELIANCE",
  "date": "2020-05-28",
  "contract": "2020-05 1367.0424 PE"
 },
 {
  "lev": "L1",
  "symbol": "ITC",
  "date": "2020-06-26",
  "contract": "2020-07 179.85 PE"
 },
 {
  "lev": "L1",
  "symbol": "ITC",
  "date": "2020-06-29",
  "contract": "2020-07 179.85 PE"
 },
 {
  "lev": "L1",
  "symbol": "ITC",
  "date": "2020-06-30",
  "contract": "2020-07 179.85 PE"
 },
 {
  "lev": "L1",
  "symbol": "ITC",
  "date": "2020-07-01",
  "contract": "2020-07 179.85 PE"
 },
 {
  "lev": "L1",
  "symbol": "ITC",
  "date": "2020-07-02",
  "contract": "2020-07 179.85 PE"
 },
 {
  "lev": "L1",
  "symbol": "ITC",
  "date": "2020-07-03",
  "contract": "2020-07 179.85 PE"
 },
 {
  "lev": "L1",
  "symbol": "EICHERMOT",
  "date": "2020-07-31",
  "contract": "2020-08 2000.0 PE"
 }
]
```

### F-10 — F&O start/end events for traded symbols inside the window (informational) (INFO)



8 failing record(s); first 8:

```
[
 {
  "symbol": "ETERNAL",
  "fo_start": "2024-11-29T00:00:00.000",
  "fo_end": "2026-07-31T00:00:00.000",
  "trading_days": 412,
  "event": "start"
 },
 {
  "symbol": "HDFCLIFE",
  "fo_start": "2020-02-28T00:00:00.000",
  "fo_end": "2026-07-31T00:00:00.000",
  "trading_days": 1586,
  "event": "start"
 },
 {
  "symbol": "JIOFIN",
  "fo_start": "2024-11-29T00:00:00.000",
  "fo_end": "2026-07-31T00:00:00.000",
  "trading_days": 412,
  "event": "start"
 },
 {
  "symbol": "SBILIFE",
  "fo_start": "2020-05-04T00:00:00.000",
  "fo_end": "2026-07-31T00:00:00.000",
  "trading_days": 1546,
  "event": "start"
 },
 {
  "symbol": "SHRIRAMFIN",
  "fo_start": "2022-12-20T00:00:00.000",
  "fo_end": "2026-07-31T00:00:00.000",
  "trading_days": 890,
  "event": "start"
 },
 {
  "symbol": "TMPV",
  "fo_start": "2025-10-24T00:00:00.000",
  "fo_end": "2026-07-31T00:00:00.000",
  "trading_days": 189,
  "event": "start"
 },
 {
  "symbol": "TRENT",
  "fo_start": "2021-02-26T00:00:00.000",
  "fo_end": "2026-07-31T00:00:00.000",
  "trading_days": 1338,
  "event": "start"
 },
 {
  "symbol": "TATAMOTORS",
  "fo_start": "2019-10-01T00:00:00.000",
  "fo_end": "2025-10-23T00:00:00.000",
  "trading_days": 1498,
  "event": "end"
 }
]
```

## Notes

- Eligibility table: data_validation/output/fo_eligibility_daily.parquet; windows: fo_eligibility_windows.csv.
- Repair logged: 2021-03-30 F&O reconstruction (pre-existing, previously unlogged).
