# Validation report

Command: `python3 -m pytest tests/` → **80 passed in 33.84s**. Stages were run in order; each passed before the next.

| Requirement | Evidence |
|---|---|
| No look-ahead | `test_engine_never_reads_future_data`: guard proxy fails on any dated read after the processing day, full window at 1× and 5×; `test_truncated_history_gives_identical_decisions` (real data, cut 2022-06-30); `test_decisions_identical_when_future_data_deleted` (synthetic); termination skips only after announcement date |
| Historical constituents / survivorship | rankings are point-in-time NIFTY 50 members on each expiry (`test_rankings_are_point_in_time_index_members`); removed names (YESBANK, VEDL, HDFC, …) were loaded and are selected (`test_selection_is_not_survivor_biased`); 13 missing names ingested from cached bhavcopies |
| Expiry dates | from contract data, 82 cycles, Jun-2023 settled 06-28; last trading day of each series = calendar expiry |
| Lot sizes | per series (TCS Jun/Jul 2020 250/300), bonus changes, match baseline verified reference with 0 mismatches |
| Settlement | every expired option: ITM decided on futures FSP (strict), exit = intrinsic, P&L recomputed; one FSP gap (IOC 2022-06-30) falls back to CM close and is flagged |
| Transaction costs | rulebook worked example reproduced; date-effective STT/SEBI; every fill recomputed from raw close + slippage |
| Margin / leverage | trade leveraged requirement = requirement / L; daily identities; breaches liquidated next day; 1× never margin-called or borrows; same lots at 1×/5× give identical INR P&L |
| Overlaps / naked calls | per-symbol option intervals never overlap; every call ≤ shares held within the holding window; no put while holding stock; ≤ 10 names daily (engine also asserts daily) |
| Capital accounting | daily cash rebuilt from ledger (±₹1, hard fail); NAV change = Σ trade P&L − financing: residual ₹0.0000 in all 7 runs |
| Corporate actions | 77 accepted events detected from F&O strike adjustments, cross-checked with lot change and price gap; known splits/dividends/baseline verified events reproduced; crash days rejected |
| Manual audit | RELIANCE Feb–May 2020 cycle traced to raw bhavcopy rows (strike, fill 41.75→40.7062, 31 lots, FSP 1066.6, call 1280 capped at 30 lots by participation, call-away 1466.05); cycle P&L ₹6,88,061.67 reconciled by hand |

## Known limitations (disclosed, not fixed)
- Ordinary dividends not modelled (stock leg, EW benchmark and NIFTY price index alike). Extraordinary dividends/demergers are.
- Forced liquidations fill at close ± slippage without a participation cap (optimistic in thin March-2020 markets).
- HDFC termination announcement date ASSUMED; TMPV/TATAMOTORS and HDFC holdings sold at CM close on termination.
- VEDL 2026-04-30 demerger has no adjustment (no run held VEDL then — verify before extending the window).
- Leverage margin is a notional/L proxy, not SPAN; real SPAN rises for ITM shorts, so real margin calls would come earlier.
- The ~/nse-wheel engine itself was not executed (its gated protocol forbids results without approval); benchmark 4 is its frozen universe through this engine.
