# Lot Size — Validation Test Report

**Tested:** `data/lots/lot_at_monthly_expiry.csv` (built by `lot_sizes.py`)
**Test date:** 2026-09-17 **Data through:** 2026-09-16
**Result:** **PASS**. All 989 external comparisons (T2–T5) match, differ for a documented reason, or could not be verified (1). No errors found.

Source files used are saved in `data/lots/validation/`.

## What was tested

Lot size per F&O stock per monthly expiry, 2019-10 → 2026-08 (306 stocks, 83 expiries, 15,210 contracts).

> **Scope note (2026-09-18).** `data/lots/` was rebuilt from 2016-01-01 (366 stocks, 24,674 contract
> series, 1,463,142 daily rows). The external comparisons T2–T5 below were run against the 2019-10
> onward period and have **not** been repeated for 2016-01 → 2019-09; that stretch rests on the derived
> method plus the internal checks. Evidence for it: every lot from 2019-10 on is unchanged by the rebuild
> (0 differences in 936,509 rows), T1's derived-vs-exchange agreement is unchanged at 99.85%, the
> resolved lot divides the contract's open-interest GCD on 99.9963% of the 458,113 new rows where both
> exist, and only 25 series-days in the whole file are unresolved (BHARATFIN, GRANULES, HCC, PTC, RDEL —
> no NIFTY 50 name). Every NIFTY 50 member-day in 2016-01 → 2019-09 has a lot.

How the lots were derived:

| Period | Method |
|---|---|
| 2024-07-08 onward | `NewBrdLotQty` column in the UDiFF bhavcopy (exchange-published) |
| Before 2024-07-08 | Futures traded value ÷ (contracts × close), snapped to the divisor of the contract's open-interest GCD nearest that estimate |

## Summary

| # | Test | Independent source | Scope | Result |
|---|---|---|---|---|
| T1 | Internal: derived method vs exchange lot | `NewBrdLotQty` on UDiFF days | 328,330 daily rows | **99.85 %** match (naive rounding: 21.69 %) |
| T2 | Current lots | NSE `fo_mktlots.csv` | 210 stocks × Sep/Oct/Nov-2026 = 629 contracts | **629 / 629** match |
| T3 | April-2021 revision | NSE circular FAOP47856 + annexure | 156 stocks | **154** match, 2 explained |
| T4 | October-2022 revision | NSE circular FAOP53920 + annexure | 194 stocks | **191** match, 3 explained |
| T5 | 10 random historical contracts | NSE circulars + Zerodha bulletins | 10 contracts | **9** consistent, 1 not verifiable |

T3 and T4 test the **derived** (pre-2024) period, the part most at risk.

## T2 — Current lots vs NSE `fo_mktlots.csv`

Our lot on 2026-09-16 for every live contract, compared with the official file.
Result: 629 match, 0 mismatch, 0 missing.

## T3 — NSE circular FAOP47856 (Apr 2021 periodic revision)

Checks: *present lot* = our Apr-2021 expiry lot. *Revised lot* = our lot at May-2021 expiry for Annexure-1 halvings (applied to contracts already trading), and at Jul-2021 expiry for the other annexures.

| Stock | NSE present | Ours | NSE revised | Ours (Jul-2021) | Reason |
|---|---|---|---|---|---|
| AARTIIND | 425 | 425 | 425 (unchanged) | 850 | 1:1 bonus, ex 2021-06-22, after the circular. Price ratio 0.492 on the day |
| POWERGRID | 4000 | 4000 | 4000 (unchanged) | 5333 | 1:3 bonus, ex 2021-07-29, after the circular. Price ratio 0.732 |

The other 154 match, including all 40 Annexure-1 halvings (e.g. SBIN 3000→1500, TATASTEEL 1700→850, CUMMINSIND 1200→600, NAUKRI 250→125) and the Annexure-4 revisions (BAJAJFINSV 125→75, COFORGE 375→200).

## T4 — NSE circular FAOP53920 (Oct 2022 periodic revision)

Same checks. The present lot is the Oct-2022 expiry lot; revised lots are checked at Nov-2022 expiry (halvings) and Jan-2023 expiry (others).

| Stock | NSE present | Ours | NSE revised | Ours | Reason |
|---|---|---|---|---|---|
| MOTHERSON | 4500 | 6750 | 4500 | 6750 | 1:2 bonus, ex 2022-10-03, after the circular. Price ratio 0.668 |
| AMARAJABAT | 1000 | 1000 | 1000 | — | Exited F&O; last expiry 2022-12-29 |
| GSPL | 2500 | 2500 | 2500 | — | Exited F&O; last expiry 2022-11-24 |

The other 191 match, e.g. TATACHEM 1000→500, VOLTAS 500→600, DELTACORP 2300→2800, MPHASIS 175→275, VEDL 1550→2000.

## T5 — 10 random historical contracts

Drawn with `random_state=20260917` from expiries before 2024-07-01.

| Stock | Expiry | Our lot | External evidence | Verdict |
|---|---|---|---|---|
| AMARAJABAT | 2020-01-30 | 800 | 1000 in Apr-2021 list; our history shows 700→800 (Jan-2020 series), 800→1000 (Jul-2020) | Consistent |
| MCDOWELL-N | 2019-11-28 | 1250 | 1250 in Apr-2021 list; 1250→625 from May 2022 (Zerodha) | Consistent |
| DELTACORP | 2022-11-24 | 2300 | FAOP53920: 2300 → 2800 from Jan 2023 | Match |
| VEDL | 2020-03-26 | 3500 | No source found for early 2020. Later values match: 6200→3100 (2021), 3100→1550 (2022), 1550→2000 (2023) | Not verified |
| MPHASIS | 2023-01-25 | 275 | FAOP53920: 175 → 275 from Jan 2023 | Match |
| TATACHEM | 2023-01-25 | 500 | FAOP53920: 1000 → 500 from Nov 2022 | Match |
| VOLTAS | 2022-09-29 | 500 | FAOP53920: present 500 | Match |
| NAUKRI | 2021-03-25 | 250 | FAOP47856: present 250 | Match |
| CUMMINSIND | 2020-08-27 | 1200 | FAOP47856: present 1200; no change in between | Consistent |
| SUNTV | 2021-11-25 | 1500 | FAOP47856 and FAOP53920: 1500, unchanged | Match |

## Findings

1. **No lot-size errors found.** Every difference comes from a corporate action after the circular date or from a stock leaving F&O. Both are handled correctly in the data.
2. **Rulebook Step 3 is wrong for the legacy period.** "Snap to the nearest integer if within ±1 %" of `VAL_INLAKH × 1e5 / (CONTRACTS × CLOSE)` agrees with the exchange lot only 21.69 % of the time, because traded value is priced at traded prices, not the close. The OI-divisor method gives 99.85 %.
3. **Rulebook Step 3 has the wrong UDiFF start date.** `NewBrdLotQty` is available from **2024-07-08**, not 2024-01-01.
4. **The existing `nse.contract_lots` Postgres table is unreliable** before 2024: its OI-GCD lots under-state the lot (e.g. ADANIENT 3, ANGELONE 250). Use `data/lots/` instead.

## Coverage gaps

- The Oct-2019 and Jul-2020 periodic revisions were not checked; their NSE circulars were not located. Lots from those revisions are supported by T1 and by continuity with the verified 2021 values.
- One random sample (VEDL, Mar 2020) could not be verified independently.
- Small non-round mid-contract changes classified as `corporate_action` (67 events) were spot-checked (RELIANCE rights 500→505), not exhaustively.

## Sources

| File in `validation/` | Origin |
|---|---|
| `fo_mktlots_2026-09-17.csv` | https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv |
| `FAOP47856.pdf`, `FAOP47856_annexure.xlsx` | https://nsearchives.nseindia.com/content/circulars/FAOP47856.zip |
| `FAOP53920.pdf`, `FAOP53920_annexure.xlsx` | https://archives.nseindia.com/content/circulars/FAOP53920.zip |
| — | https://zerodha.com/marketintel/bulletin/321812/revision-in-lot-size-of-stock-fo-contracts-from-may-2022-expiry |
| — | https://zerodha.com/marketintel/bulletin/333314/revision-in-lot-size-of-stock-fo-contracts-from-november-2022-expiry |
| — | https://zerodha.com/marketintel/bulletin/291849/revision-in-lot-size-of-nifty-and-stock-fo-contracts |
