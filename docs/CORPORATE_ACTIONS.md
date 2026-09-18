# Corporate-action adjustment methodology

How the Brindco wheel backtest handles splits, bonuses, F&O lot-size revisions, dividends, rights issues,
demergers and mergers. Everything here is in this project; no outside dataset is used.

## 1. Principle: raw prices, events applied on the ex-date

- Cash closes, option chains and futures are stored **unadjusted, as traded** (`nse/wheel/data.py`).
  Nothing is back-adjusted.
- Each corporate action is applied to the open book **once, on its ex-date**, in engine step 2
  (`WheelEngine._corporate_actions`), before marks, expiries and new orders.
- Test for every event type: NAV must not jump on the ex-date beyond the market move. The fall in the share
  price is offset by more shares (split/bonus) or by cash (dividend, rights value, demerger value).

## 2. Sources

| Table | What it holds | Built by | Status |
|---|---|---|---|
| `data/raw/corporate_actions/nse_api/<SYMBOL>/*.json` | Raw NSE corporate-action announcements, 2019-10-01 → 2026-09-16, 66 symbols | `scripts/corporate_actions/fetch_nse_ca.py` | Exchange source; each request logged with SHA-256 in `data/logs/corporate_actions_fetch.csv` |
| `data/reference/nse_corporate_announcements.csv` | Every announcement parsed (704 rows) | `backtest.py dividends` | Parsed |
| `data/reference/dividends.csv` | Cash dividend per (symbol, ex-date) | `backtest.py dividends` | 600 payable in the price data window |
| `data/backtest/corporate_actions_detected.csv` | Contract adjustments NSE made (strike map `K → a·K + b`, lot ratio) | `backtest.py detect-ca` | 77 accepted, 2 rejected |
| `data/calendar/corporate_action_overrides.csv` | Demerger values NSE did not put into the contracts | manual | ASSUMED values, each with a source note |
| `data/calendar/contract_terminations.csv` | Mergers and demergers where NSE closed the contracts early | manual | NSE circulars cited |
| `data/reference/ticker_aliases.csv` | Old warehouse ticker ↔ current NSE ticker (same ISIN) | manual | ISIN continuity |
| `data/reference/verified_corporate_actions.csv`, `verified_lot_sizes.csv` | Hand-checked splits/bonuses and lot sizes, used as test references | copied in, kept here | VERIFIED rows |

Build order: `backtest.py cache` → `detect-ca` → `fetch_nse_ca.py` → `dividends` → `run`.
`runner.Context` refuses to start without `dividends.csv`.

## 3. Splits and bonuses

**Detection.** On the ex-date NSE rewrites every open contract. The detector (`corporate_actions.detect`)
looks for yesterday's open-interest strikes vanishing and today's strikes being an affine map of them. It
fits `a` from the lot ratio (`lot_before / lot_after`). The share multiplier is that ratio when it is a small
integer fraction at least 15% away from 1. An event is accepted only if at least 80% of 8 or more
open-interest strikes map, and the implied price ratio matches the observed close-to-close ratio within 8%.

**Cross-check.** Every NSE split and bonus announcement in the window matches an accepted detection with the
right multiplier. Examples: TATASTEEL 10:1 (2022-07-28), RELIANCE 1:1 (2024-10-28), BAJAJFINSV split plus
bonus = 10 (2022-09-13), POWERGRID 1:3 = 4/3 (2021 and 2023). Tests: `test_known_splits_and_bonuses`,
`test_baseline_verified_splits_are_detected`.

**Applied to the book** (multiplier `m`):

| Item | Adjustment |
|---|---|
| Shares held | × m |
| Entry price, economic basis, stock mark | ÷ m |
| Cycle references (put strike, assignment FSP, spot trackers) | ÷ m |
| Open short option | strike → a·K + b; quantity and lot × lot ratio; entry premium and mark ÷ lot ratio |
| Pending orders for the name | strike cleared, re-decided on the adjusted chain |

Test: `test_split_detected_and_applied_with_nav_continuity`.

## 4. F&O lot-size revisions

- **Lot per contract per day** comes from the data, not a static table. UDiFF files carry `NewBrdLotQty`
  from July 2024. Earlier, the lot is the GCD of open interest (in shares) across a series' strikes,
  cross-checked against the futures turnover estimate (`scripts/wheel/lot_sizes.py`, `data/lots/`).
- **Corporate-action lot changes** (split, bonus, rights) change the lot of live contracts. The engine scales
  open option quantity by the same ratio (§3), so the covered shares and the contract stay matched.
- **Periodic revisions** (NSE's contract-value reviews) only apply to new contract months. An open position
  keeps its lot to expiry; new orders use the lot of the contract being traded on day t.
- If a lot can't be resolved for a day, the last resolved lot of the same contract is used, but only when no
  corporate action falls in between. Otherwise the trade is skipped.
- Covered calls never exceed shares held when a lot grows (`test_calls_never_exceed_shares_when_lot_grows`).
- Validation: agreement with NSE circulars FAOP47856 and FAOP53920 and `fo_mktlots`
  (`data/lots/lot_size_test_report.md`, `test_lots_match_baseline_reference`).

## 5. Dividends (holding leg)

**Why two sources.** NSE adjusts F&O contracts only for dividends it treats as extraordinary, so the
detector sees those as a strike shift (`b < 0`). Ordinary dividends leave the contracts untouched and can
only come from the announcements.

**Parsing** (`nse/wheel/dividends.py`). Every amount tied to the word "dividend" in an announcement is
added up:
- "Special Dividend - Rs 8 /Dividend - Rs 20" → ₹28 (INFY 2024-05-31)
- "Interim Dividend - Rs 8 Per Share Special Dividend - Rs 67 Per Share" → ₹75 (TCS 2023-01-16)

NSE subjects are often truncated or misspelled ("Per Sh", "Per Hsare", "Dividned"), and the parser handles
these. An announcement with no amount is marked UNPARSED and **not paid**. The only case in the window is
VEDL 2022-03-09 "Interim Dividend", when VEDL is outside the index.

**Ticker mapping.** A dividend is attached to the warehouse ticker that traded on the ex-date. NSE files
renamed companies under their current ticker, so `ticker_aliases.csv` maps TMPV → TATAMOTORS (until
2025-10-23) and SAMMAANCAP → IBULHSGFIN (until 2024-07-25). Pre-merger Shriram Transport dividends
(filed under SHRIRAMFIN, before its 2022-12-20 start) are not payable, because no such position can exist.

**Merge with detected events** (`dividends.merge`):

| Case | Treatment |
|---|---|
| Dividend and a detected contract adjustment on the same date | One event. Cash = max(announced dividend, value implied by the fitted strike map). Paid once, never twice. |
| Manual override on that date | Override value kept |
| No contract adjustment (ordinary dividend) | New `dividend` event, `contract_adjusted = False` |

**Engine** (`WheelEngine._dividend`, for ordinary dividends):
- Cash = dividend × shares held at the previous close, booked to the `dividend` ledger line.
- Stock entry price and stock mark are lowered by the dividend, so NAV doesn't jump and trade P&L includes
  the dividend.
- **No change** to strikes, lots, open options, pending orders or the economic basis. The rulebook's call
  floor excludes dividends, and the contracts weren't adjusted.

**Entitlement timing.** Shares held at the close before the ex-date receive the dividend. Put assignment on
expiry day t counts as a purchase on t. Under T+1 (and T+2 before 2023) such a purchase is on the register
by the record date for an ex-date after t.

**Checks.**
- Every accepted F&O dividend shift is matched by an announced dividend within 2%, rights dates excluded
  (`test_detected_extraordinary_dividends_agree_with_announcements`).
- Known amounts are tested (`test_known_dividends`).
- The engine test shows identical option trades with and without the dividend, NAV higher by exactly
  dividend × shares, and trade P&L that still reconciles with NAV.

**Benchmark.** The equal-weight benchmark reads the same event table (`benchmarks.adjusted_returns`), so it is
now total-return as well. The NIFTY 50 buy-and-hold series is still the **price** index.

**Tax.** Dividends are credited gross. Dividend tax isn't modelled, which matches how option and stock P&L
are treated (pre-tax).

## 6. Rights issues

NSE adjusts contracts for rights, and the detector picks that up as a scale or shift map (RELIANCE 2020,
BHARTIARTL 2021, GRASIM 2024, TATACONSUM 2024, UPL 2024, ADANIENT 2025). The engine credits the value
implied by the map, `prev − (a·prev + b)`, as cash per share.

**ASSUMED:** the entitlement is sold (renounced) at its theoretical value. No subscription and no new shares.

Not covered: SAMMAANCAP 2024 (out of F&O by then) and SHRIRAMFIN 2020 (Shriram Transport, before the
warehouse ticker exists).

## 7. Demergers and mergers

| Event | Treatment | Status |
|---|---|---|
| RELIANCE → JIOFIN, 2023-07-20 | Contracts not adjusted. Override pays ₹261.85 per share (JFSL special pre-open price discovery) | ASSUMED realised |
| ITC → ITCHOTELS, 2025-01-06 | Override pays ₹27 per ITC share (₹270 discovery ÷ 10) | ASSUMED realised |
| HINDUNILVR → Kwality Wall's, 2025-12-05 (1:1) | Override pays ₹40.20 per share. The exact pre-open discovery price wasn't found, so this is NSE's adjusted reference price at KWIL's 2026-02-16 listing | ASSUMED; value UNVERIFIED |
| TATAMOTORS CV demerger, 2025-10-14 | Contracts terminated 2025-10-13 at the CM close (NSE/FAOP/70615). Held stock sold at that close. TMPV is a new name from 2025-10-24 | NSE circular |
| HDFC → HDFCBANK merger, 2023-07-13 | Contracts terminated 2023-07-12. Held stock sold at that close, not spliced into HDFCBANK | ASSUMED exit |

## 8. Known gaps (not modelled)

| Event | Why not | Impact |
|---|---|---|
| BRITANNIA 2021-05-25 bonus debenture (1 NCD per share, under a scheme) | Debenture value not sourced. The ₹12.50 dividend on that date **is** paid | Understates a BRITANNIA holder's return by the NCD value. No price gap on the ex-date (close ratio 1.018) |
| VEDL 2026-04-30 demerger (detected, rejected: 65% price gap, no strike map) | VEDL left the NIFTY 50 on 2020-07-30, so the ranked strategy can't hold it | None for ranked runs |
| Buybacks (TCS, WIPRO, INFY, LT, BAJAJ-AUTO, GAIL, NTPC) | Tender is optional and the price doesn't adjust | None: modelled as not tendering |
| Dividend tax, rights subscription | Out of scope (§5, §6) | Returns are pre-tax |
