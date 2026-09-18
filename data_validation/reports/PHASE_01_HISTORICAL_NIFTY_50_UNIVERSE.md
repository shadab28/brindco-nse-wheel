# Phase 1 — Historical NIFTY 50 Universe

**Generated:** 2026-09-17T23:46:09  
**Gate:** CONDITIONAL PASS / PROCEED WITH KNOWN LIMITATION  
**Checks:** 13 run, 11 passed, 2 failed (1 critical)

## Data sources

NSE Indices press releases (niftyindices.com/Press_Release/ind_prs*.pdf): every replacement, exclusion and corporate-adjustment release 2016-01..2026-09 (462 PDFs, saved with extracted text and an index CSV in `data_validation/sources/niftyindices/`). Anchor: official `ind_nifty50list.csv` downloaded 2026-09-17. Compared against the project's `data/universe/nifty50_master_list.csv` (hand-transcribed, used by the rankings), `data/signals/expiry_rankings.csv`, and the vendor `in_nifty50` flag in `data/market/nifty50_15m.db`.

## Methodology

NIFTY 50 changes are parsed from the PDFs: replacement sections (`1) Nifty 50` / `a) NIFTY 50`) with their `effective from` date, and spin-off inclusions / exclusions whose index list contains Nifty 50. A later release supersedes an earlier one for the same symbol and action within 120 days (Yes Bank's exclusion was brought forward from 27-Mar to 19-Mar-2020). Membership is rebuilt by undoing every change from the anchor list, newest first; the rollback must never remove a non-member or add a member. Renames with the same ISIN are mapped to today's ticker (ZOMATO→ETERNAL, TATAMOTORS→TMPV, LTI→LTIM). `is_nifty50` is an interval lookup; the expected status is an independent forward replay of the events.

## Tests run

U-01..U-12 in data_validation/phase01_universe.py; tests/test_dv_universe.py (pytest).

## Results

| ID | Check | Critical | Checked | Failed | Status | Detail |
|---|---|---|---:|---:|---|---|
| U-01 | Rollback from the anchor list is consistent (no impossible add/remove) | yes | 70 | 0 | PASS |  |
| U-02 | Member count is 50 (51 in the 2016-04..2017-09 Tata Motors DVR era, + active zero-price spin-offs) on every trading day | yes | 2,611 | 0 | PASS |  |
| U-03 | Rebuilt membership on 2026-09-17 equals the official NSE list | yes | 50 | 0 | PASS |  |
| U-04 | Every change has a dated NSE release announced on or before its effective date | yes | 70 | 0 | PASS |  |
| U-05 | No retrospective application: status flips exactly on each effective date | yes | 70 | 0 | PASS |  |
| U-06 | Current constituents added after 2019-10-01 are not members before their inclusion | yes | 18 | 0 | PASS | 18 of today's members joined inside the window: ADANIENT, APOLLOHOSP, BAJAJFINSV, BAJFINANCE, BEL, EICHERMOT, ETERNAL, HDFCLIFE, INDIGO, JIOFIN, JSWSTEEL, MAXHEALTH, NESTLEIND, SBILIFE, SHRIRAMFIN, TATACONSUM, TITAN, TRENT |
| U-07 | is_nifty50(symbol, date) matches the expected status on sampled dates | yes | 5,140 | 0 | PASS | 5,140 samples: 5,000 random (symbol, trading day) over 90 symbols incl. 60 random F&O non-members, plus both sides of every change |
| U-08 | Project NIFTY 50 list equals NSE membership for regular constituents on every day | yes | 2,611 | 0 | PASS |  |
| U-09 | Project list omits zero-price spin-off inclusions (known, non-tradable) | no | 2,611 | 135 | INFO | ITCHOTELS: 2025-01-06..2025-02-07 (25 days); JIOFIN: 2023-07-20..2023-09-06 (34 days); KWIL: 2025-12-05..2026-02-23 (54 days); TMCV: 2025-10-14..2025-11-14 (22 days). Each was added by NSE Indices at zero price before listing and had no F&O contract, so it could never be a wheel candidate. |
| U-10 | Every ranked symbol was a NIFTY 50 member on its ranking expiry | yes | 4,016 | 0 | PASS |  |
| U-11 | Every tradable NIFTY 50 member is present in the ranking on each expiry (no survivorship gap) | yes | 82 | 84 | FAIL (CRITICAL) | HDFC: 45 expiries (2019-10-31..2023-06-28); INFRATEL: 12 expiries (2019-10-31..2020-09-24); LTIM: 15 expiries (2023-07-27..2024-09-26); ZEEL: 12 expiries (2019-10-31..2020-09-24). These members have no bars in the vendor 15m database (data/market/nifty50_15m.db), so scripts/wheel/expiry_rankings.py skipped them and the backtest could never select them. |
| U-11b | Zero-price spin-off members not ranked (expected: not listed yet) | no | 82 | 6 | INFO |  |
| U-12 | Vendor in_nifty50 flag (Kite 15m DB) agrees with NSE membership | no | 101,461 | 13 | FAIL | symbol  vendor        min        max  count JIOFIN   False 2023-08-21 2023-09-06     13 |
## Accepted limitations

**U-11** — CONDITIONAL PASS / PROCEED WITH KNOWN LIMITATION (accepted 2026-09-17 by project owner)

> Four historically valid NIFTY 50/F&O names — HDFC, LTIM, INFRATEL/INDUSTOWER, and ZEEL — are missing from the vendor 15-minute underlying database for certain historical periods. As a result, those member-expiries were unavailable to the ranking engine and may introduce survivorship bias into historical rankings. No data has been fabricated or repaired at this stage.

Do not modify the ranking methodology to compensate for the missing data.


## Failures

### U-09 — Project list omits zero-price spin-off inclusions (known, non-tradable) (INFO)

ITCHOTELS: 2025-01-06..2025-02-07 (25 days); JIOFIN: 2023-07-20..2023-09-06 (34 days); KWIL: 2025-12-05..2026-02-23 (54 days); TMCV: 2025-10-14..2025-11-14 (22 days). Each was added by NSE Indices at zero price before listing and had no F&O contract, so it could never be a wheel candidate.

135 failing record(s); first 25:

```
[
 {
  "date": "2023-07-20",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-07-21",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-07-24",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-07-25",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-07-26",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-07-27",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-07-28",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-07-31",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-01",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-02",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-03",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-04",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-07",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-08",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-09",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-10",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-11",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-14",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-16",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-17",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-18",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-21",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-22",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-23",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 },
 {
  "date": "2023-08-24",
  "symbol": "JIOFIN",
  "project": false,
  "nse": true
 }
]
```

### U-11 — Every tradable NIFTY 50 member is present in the ranking on each expiry (no survivorship gap) (FAIL (CRITICAL))

HDFC: 45 expiries (2019-10-31..2023-06-28); INFRATEL: 12 expiries (2019-10-31..2020-09-24); LTIM: 15 expiries (2023-07-27..2024-09-26); ZEEL: 12 expiries (2019-10-31..2020-09-24). These members have no bars in the vendor 15m database (data/market/nifty50_15m.db), so scripts/wheel/expiry_rankings.py skipped them and the backtest could never select them.

84 failing record(s); first 25:

```
[
 {
  "expiry": "2019-10-31",
  "symbol": "HDFC"
 },
 {
  "expiry": "2019-10-31",
  "symbol": "INFRATEL"
 },
 {
  "expiry": "2019-10-31",
  "symbol": "ZEEL"
 },
 {
  "expiry": "2019-11-28",
  "symbol": "HDFC"
 },
 {
  "expiry": "2019-11-28",
  "symbol": "INFRATEL"
 },
 {
  "expiry": "2019-11-28",
  "symbol": "ZEEL"
 },
 {
  "expiry": "2019-12-26",
  "symbol": "HDFC"
 },
 {
  "expiry": "2019-12-26",
  "symbol": "INFRATEL"
 },
 {
  "expiry": "2019-12-26",
  "symbol": "ZEEL"
 },
 {
  "expiry": "2020-01-30",
  "symbol": "HDFC"
 },
 {
  "expiry": "2020-01-30",
  "symbol": "INFRATEL"
 },
 {
  "expiry": "2020-01-30",
  "symbol": "ZEEL"
 },
 {
  "expiry": "2020-02-27",
  "symbol": "HDFC"
 },
 {
  "expiry": "2020-02-27",
  "symbol": "INFRATEL"
 },
 {
  "expiry": "2020-02-27",
  "symbol": "ZEEL"
 },
 {
  "expiry": "2020-03-26",
  "symbol": "HDFC"
 },
 {
  "expiry": "2020-03-26",
  "symbol": "INFRATEL"
 },
 {
  "expiry": "2020-03-26",
  "symbol": "ZEEL"
 },
 {
  "expiry": "2020-04-30",
  "symbol": "HDFC"
 },
 {
  "expiry": "2020-04-30",
  "symbol": "INFRATEL"
 },
 {
  "expiry": "2020-04-30",
  "symbol": "ZEEL"
 },
 {
  "expiry": "2020-05-28",
  "symbol": "HDFC"
 },
 {
  "expiry": "2020-05-28",
  "symbol": "INFRATEL"
 },
 {
  "expiry": "2020-05-28",
  "symbol": "ZEEL"
 },
 {
  "expiry": "2020-06-25",
  "symbol": "HDFC"
 }
]
```

### U-11b — Zero-price spin-off members not ranked (expected: not listed yet) (INFO)



6 failing record(s); first 6:

```
[
 {
  "expiry": "2023-07-27",
  "symbol": "JIOFIN"
 },
 {
  "expiry": "2023-08-31",
  "symbol": "JIOFIN"
 },
 {
  "expiry": "2025-01-30",
  "symbol": "ITCHOTELS"
 },
 {
  "expiry": "2025-10-28",
  "symbol": "TMCV"
 },
 {
  "expiry": "2025-12-30",
  "symbol": "KWIL"
 },
 {
  "expiry": "2026-01-27",
  "symbol": "KWIL"
 }
]
```

### U-12 — Vendor in_nifty50 flag (Kite 15m DB) agrees with NSE membership (FAIL)

symbol  vendor        min        max  count
JIOFIN   False 2023-08-21 2023-09-06     13

13 failing record(s); first 13:

```
[
 {
  "date": "2023-08-21",
  "symbol": "JIOFIN",
  "vendor": false
 },
 {
  "date": "2023-08-22",
  "symbol": "JIOFIN",
  "vendor": false
 },
 {
  "date": "2023-08-23",
  "symbol": "JIOFIN",
  "vendor": false
 },
 {
  "date": "2023-08-24",
  "symbol": "JIOFIN",
  "vendor": false
 },
 {
  "date": "2023-08-25",
  "symbol": "JIOFIN",
  "vendor": false
 },
 {
  "date": "2023-08-28",
  "symbol": "JIOFIN",
  "vendor": false
 },
 {
  "date": "2023-08-29",
  "symbol": "JIOFIN",
  "vendor": false
 },
 {
  "date": "2023-08-30",
  "symbol": "JIOFIN",
  "vendor": false
 },
 {
  "date": "2023-08-31",
  "symbol": "JIOFIN",
  "vendor": false
 },
 {
  "date": "2023-09-01",
  "symbol": "JIOFIN",
  "vendor": false
 },
 {
  "date": "2023-09-04",
  "symbol": "JIOFIN",
  "vendor": false
 },
 {
  "date": "2023-09-05",
  "symbol": "JIOFIN",
  "vendor": false
 },
 {
  "date": "2023-09-06",
  "symbol": "JIOFIN",
  "vendor": false
 }
]
```

## Notes

- Validated table: data_validation/output/nifty50_membership.csv (symbol, effective_from, effective_to, source releases); events: data_validation/output/nifty50_change_events.csv.
- The 2026-09-30 change (BSE in, WIPRO out, release ind_prs10082026.pdf) is after the anchor date and after the backtest window; it is parsed but not applied.
- No data was repaired in this phase.
