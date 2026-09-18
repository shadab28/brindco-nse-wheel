# Stock selection: filters, score and monthly capacity

This note explains how the wheel chooses which stocks to sell puts on each month, why each filter is there,
how strong the evidence for the score threshold (`rank_threshold: 1.5`) is, and how many stocks could
actually be positioned in every month of the backtest.

All figures use the current `params.yaml`: threshold 1.5, `n_positions` 20, entries at each monthly expiry,
2020-01-01 → 2026-06-30, dividends included. Sources:
- `scripts/wheel/selection_funnel.py`, which writes `data/backtest/report/selection_funnel_L1.csv` and
  `selection_funnel_L5.csv`;
- `data/signals/analysis/`;
- `data/backtest/report/threshold_L5/summary.csv`.

## 1. The selection pipeline

At the close of each monthly expiry day E, puts are sold from E+1 onward.

| Step | Filter | Rule | Why |
|---|---|---|---|
| 1 | Universe | NIFTY 50 member **on E** (`nifty50_membership_validated.csv`, rebuilt from NSE Indices releases) | Large, liquid, F&O-eligible names with physical settlement. Point-in-time, so no survivorship bias (see `DATA.md` §4.5) |
| 2 | Scored | Has a trend score on E | Needs 15-minute and daily bars up to E's close. Members without bars drop out (U-11, about 1 name a month) |
| 3 | Trend score | `rank_final > 1.5` | Only sell puts on names trading above both their short-term (15-minute EMA50) and medium-term (daily EMA20) trend. See §2–3 |
| 4 | Cap | Top 20 by score (`n_positions`) | Caps names and sets the per-name size (`L × NAV / 20`), so one name is at most ~5% of target notional |
| 5 | Not already held | Names already in the book (open put, stock or covered call) get no new entry | One wheel per name; no stacking |
| 6 | Portfolio gate | No new puts while assigned shares still await covered calls, or while cash/margin headroom is used up | Covered calls first; margin never exceeds `max_margin_utilization` × NAV |
| 7 | Contract | Lot size known; stock future listed; no announced contract termination | Never trade an unknown or terminating contract |
| 8 | Liquid strike | Highest strike ≤ 95% of spot with ≥ 10 contracts traded and ≥ 50 contracts OI on E, within max(1.5% × spot, strike interval) of the target | Avoid stale quotes and strikes far from the intended 5% cushion |
| 9 | Size | `floor(target / (K × lot))` lots; one lot allowed if ≤ 1.5× target; then capped by margin and cash | Keeps names equal-weighted without breaking lot sizes |
| 10 | Fill | E+1, ≥ 1 contract traded, ≤ 10% of the day's volume, up to 3 retries | Realistic execution on EOD data |

## 2. The score

```
pct_15m    = (close at 15:15 on E − EMA50 of 15-minute closes) / EMA50 × 100
pct_daily  = (close − EMA20 of daily closes) / EMA20 × 100
rank_final = (sqrt((1 + pct_15m/100) × (1 + pct_daily/100)) − 1) × 100
```

`rank_final` is the geometric mean of the two gaps. A stock scores well only if it sits above **both** trend
lines, so a stock stretched far above its daily trend but already falling intraday is held back. The score
uses data up to E's close only; the put is sold the next day.

**What 1.5 means in price terms.** Across all scored pairs, `rank_final ≈ 0.58 × pct_daily`. A score above
1.5 roughly means the stock closes **about 2.6% or more above its 20-day trend** and is also above its
intraday trend.

**The economic case for a trend filter on a short put.** A 5%-below-spot put loses when the stock falls
through the strike within a month. Selling only on names already in an uptrend is meant to avoid stocks in
active decline ("catching a falling knife"). An uptrend also usually means dealers aren't pricing extra
downside, so the premium is fair rather than high because of trouble.

## 3. Evidence on the threshold, both sides

### 3.1 What the backtest shows (5×, same code, dividends included)

| rank_final > | Final NAV | CAGR | Max DD | Sharpe | Avg names selected | Puts sold | Margin calls |
|---|---|---|---|---|---|---|---|
| 2 | ₹749.1 L | 22.54% | -42.68% | 0.72 | 8.3 | 295 | 0 |
| 1.5 (current) | ₹673.8 L | 20.56% | -41.99% | 0.65 | 10.6 | 286 | 0 |
| 1 | ₹463.4 L | 13.81% | -42.59% | 0.43 | 12.9 | 248 | 0 |
| 0.5 | ₹494.3 L | 14.94% | -42.53% | 0.46 | 15.2 | 279 | 0 |

**The case for 1.5:**
- **It clearly beats looser filters.** Going from 1.0 to 1.5 raises CAGR from 13.8% to 20.6% and Sharpe
  from 0.43 to 0.65, at the same drawdown.
- **It keeps diversification.** It selects 10.6 names a month on average, against 8.3 at 2.0. At 2.0,
  31 of 79 months have fewer than 5 names above the threshold, against 23 at 1.5.
- **It wasn't picked for the best backtest.** 2.0 scores higher on CAGR and Sharpe here, so 1.5 is the
  diversification choice, not the return-maximising one.

### 3.2 What the score predicts, month by month

Each scored (stock, expiry) pair against its return to the next expiry (15:15 closes; 3,966 pairs):

| Score band | Pairs | Mean next-month return | P(return < −5%) ≈ put exercised | P(return < −10%) |
|---|---|---|---|---|
| ≤ −2 | 633 | +3.17% | 19.3% | 8.2% |
| −2 to 0 | 1,222 | +1.21% | 19.9% | 7.3% |
| 0 to 1 | 804 | +1.24% | 19.9% | 7.0% |
| **1 to 1.5** | 301 | +0.34% | **24.6%** | 9.0% |
| **1.5 to 2** | 272 | +1.51% | 21.0% | 5.5% |
| 2 to 3 | 392 | +1.24% | 17.9% | 5.4% |
| 3 to 5 | 254 | +0.53% | 23.6% | 11.4% |
| > 5 | 88 | +0.26% | 28.4% | 12.5% |

Exercise rate (return < −5%) of everything **above** each cutoff, split into two halves of the window:

| Keep score > | Full window | 2019-10 → 2022-12 | 2023-01 → 2026-06 |
|---|---|---|---|
| any | 20.5% | 22.7% | 18.4% |
| 0 | 21.1% | 23.5% | 19.1% |
| 1.0 | 21.9% | 23.5% | 20.4% |
| 1.25 | 22.4% | 23.8% | 21.0% |
| **1.5** | **21.1%** | **23.1%** | **19.0%** |
| 2.0 | 21.1% | 23.4% | 18.6% |
| 3.0 | 24.9% | 26.3% | 22.5% |

**Supporting points:**
- The 1.0–1.5 band is the worst in the middle of the range: 24.6% of its puts would have been exercised,
  and it has the weakest mean return. A threshold of 1.5 excludes exactly that band.
- The dip at 1.5 is visible in **both** halves. It is small in 2019–22 (23.8% → 23.1%) and clearer in
  2023–26 (21.0% → 19.0%).
- Very high scores (> 3) are worse again (25–28% exercised). Capping at the top 20 and selling puts 5%
  below spot limits exposure to over-extended names.

### 3.3 The counter-case (read this before defending the number)

1. **The score barely predicts outcomes.**
   - The correlation between score and next-month return is **−0.10**, slightly negative.
   - Exercise rates stay within about 17–28% in every band, and within 20.5–22.4% for every cutoff
     between "any" and 2.5.
   
   The filter doesn't select safer stocks in any strong sense.
2. **The 1.5 dip is small and could be noise.** With 301 pairs in the 1.0–1.5 band, one standard error on
   a 20% rate is about 2.3 points, so 24.6% vs ~20% is about 2 standard errors. That's one band out of
   eight, found by looking at the whole sample.
3. **The threshold was chosen in-sample.** `rank_vs_return_plot.py` chose 1.5 as the "local low" of the
   exercise-rate curve, using next-expiry returns across the whole 2019–2026 window. The backtest then runs
   on that same window. That is a look-ahead in parameter choice, even though no trade uses future data.
4. **The backtest reacts sharply to the threshold.** Going from 1.5 to 1.0 cuts CAGR by 6.8 points. A
   robust rule shouldn't swing that much for half a point of score. It suggests the return difference comes
   from **which specific months and names** get included, not from a smooth edge.
5. **Much of the threshold's effect is how many names it lets in, not the score's quality.** Fewer names
   means less capital at risk in bad months (see §4). A random filter letting in the same number of names
   would need to be tested before crediting the score.

### 3.4 How to present it honestly

- **State it as a trend-confirmation rule with a diversification floor**, not as a predictive signal. The
  defensible claims are "don't sell puts on names below their trend" and "1.5 keeps about 10–12 names a
  month where 2.0 often leaves fewer than 5".
- **Disclose that 1.5 was chosen after looking at 2019–2026 returns.** Show the 0.5 / 1.0 / 1.5 / 2.0 table
  as sensitivity, not as proof.
- **What would strengthen it before relying on it** (not run yet):
  - (a) Choose the threshold on 2020–2022 only and test on 2023–2026.
  - (b) Compare against a random selection with the same number of names each month.
  - (c) Report results for thresholds 1.25 and 1.75 to show the result isn't a knife-edge.

## 4. How many stocks could be positioned each month

**Row definitions** (from `selection_funnel.py`, which logs every put decision; the engine is unchanged):

| Column | Meaning |
|---|---|
| Members | NIFTY 50 members on the expiry |
| Scored | Members with a trend score |
| Score > 1.5, Selected (≤ 20) | Steps 3–4 |
| Held | Names already in the book at the decision (put, stock or call) |
| Open slots | 20 − held |
| Skipped | Selected, free names that reached the put decision but got no order (no liquid strike, distance cap, lot too large, margin/cash) |
| New puts | New wheel cycles started before the next expiry |
| Entries blocked | Names were selected but no put was even evaluated that month, because covered calls on assigned shares were pending or cash/margin headroom was used up |

### 4.1 Summary

| Average per month (79 months) | 1× | 5× |
|---|---|---|
| NIFTY 50 members | 50.1 | 50.1 |
| Scored | 49.0 | 49.0 |
| Score > 1.5 | 12.2 | 12.2 |
| Selected (top 20) | 10.6 | 10.6 |
| Already held at decision | 8.5 | 5.1 |
| Selected names already held | 0.9 | 0.6 |
| Open slots | 11.5 | 14.9 |
| Put decisions | 8.4 | 6.4 |
| Skipped: no liquid strike / distance cap | 0.2 / 1.1 | 0.2 / 0.9 |
| Skipped: margin or cash | 0.0 | 1.7 |
| **New puts sold** | **7.1** | **3.6** |
| Names in the book before next expiry | 15.4 | 8.6 |
| Months with at least one new put | 73 | 39 |
| Months with entries blocked | 1 | 34 |
| Months with no name above 1.5 | 4 | 4 |

**Reading.**
- **The score filter is the first real bottleneck.** It cuts about 49 scored members to about 12. The
  top-20 cap rarely binds: only 17 of 79 months have 20 or more names above 1.5.
- **Liquidity and strike rules cost little:** about 1.3 names a month at 1×, mostly the 1.5% distance cap
  in volatile months (2020).
- **At 1× the binding limit is the book.** The strategy runs about 15 names on average and adds about 7 new
  puts a month as older positions expire or get called away.
- **At 5× the binding limit is capital, not stock selection.** In 34 of 79 months (median month: **0** new
  puts) no put was even considered: assigned shares were waiting for covered calls, or cash/margin headroom
  was used up. 2022 and 2025 have almost no new entries. The 5× book holds 8.6 names on average, fewer than
  the 10.6 the filter selects.
- **So at 5× the threshold matters less than it appears.** Many months can't use the selection at all, and
  the leverage result depends as much on when capital frees up as on which names score above 1.5.

### 4.2 Yearly averages (new puts sold per month)

| Year | Score > 1.5 | Selected | 1×: held | 1×: new puts | 5×: held | 5×: new puts |
|---|---|---|---|---|---|---|
| 2019 (Dec entry) | 5.0 | 5.0 | 0.0 | 4.0 | 0.0 | 4.0 |
| 2020 | 16.0 | 12.1 | 3.3 | 6.9 | 3.8 | 6.6 |
| 2021 | 11.2 | 10.5 | 3.9 | 8.3 | 4.0 | 5.4 |
| 2022 | 11.4 | 10.4 | 11.5 | 6.8 | 5.4 | 0.0 |
| 2023 | 12.3 | 9.9 | 9.1 | 6.9 | 4.1 | 4.1 |
| 2024 | 12.2 | 11.8 | 8.2 | 9.9 | 6.2 | 5.6 |
| 2025 | 11.8 | 10.0 | 13.4 | 4.7 | 7.8 | 0.6 |
| 2026 (to June) | 10.0 | 10.0 | 13.3 | 5.3 | 4.7 | 2.5 |

### 4.3 Every month

Decision at each expiry's close. The first row is the 2019-12-31 signal date, filled on 2020-01-01.

| Expiry | Members | Scored | Score > 1.5 | Selected (≤ 20) | 1×: held | 1×: open slots | 1×: skipped | 1×: new puts | 5×: held | 5×: new puts | 5×: entries blocked |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2019-12-26 | 50 | 47 | 5 | 5 | 0 | 20 | 1 | 4 | 0 | 4 |  |
| 2020-01-30 | 50 | 47 | 2 | 2 | 2 | 18 | 1 | 1 | 2 | 1 |  |
| 2020-02-27 | 50 | 47 | 0 | 0 | 2 | 18 | 0 | 0 | 2 | 0 |  |
| 2020-03-26 | 50 | 47 | 4 | 4 | 2 | 18 | 4 | 0 | 2 | 0 |  |
| 2020-04-30 | 50 | 47 | 36 | 20 | 2 | 18 | 10 | 8 | 2 | 8 |  |
| 2020-05-28 | 50 | 47 | 33 | 20 | 3 | 17 | 9 | 10 | 3 | 10 |  |
| 2020-06-25 | 50 | 47 | 15 | 15 | 2 | 18 | 6 | 9 | 3 | 9 |  |
| 2020-07-30 | 50 | 47 | 16 | 16 | 2 | 18 | 5 | 11 | 3 | 11 |  |
| 2020-08-27 | 50 | 47 | 22 | 20 | 2 | 18 | 5 | 15 | 3 | 15 |  |
| 2020-09-24 | 50 | 47 | 1 | 1 | 14 | 6 | 0 | 1 | 15 | 0 | yes |
| 2020-10-29 | 50 | 49 | 7 | 7 | 7 | 13 | 4 | 3 | 8 | 0 | yes |
| 2020-11-26 | 50 | 49 | 32 | 20 | 1 | 19 | 9 | 10 | 2 | 10 |  |
| 2020-12-31 | 50 | 49 | 24 | 20 | 1 | 19 | 5 | 15 | 1 | 15 |  |
| 2021-01-28 | 50 | 49 | 6 | 6 | 4 | 16 | 1 | 5 | 4 | 5 |  |
| 2021-02-25 | 50 | 49 | 22 | 20 | 3 | 17 | 2 | 17 | 3 | 13 |  |
| 2021-03-25 | 50 | 49 | 0 | 0 | 9 | 11 | 0 | 0 | 10 | 0 |  |
| 2021-04-29 | 50 | 49 | 12 | 12 | 5 | 15 | 3 | 8 | 6 | 0 | yes |
| 2021-05-27 | 50 | 49 | 26 | 20 | 1 | 19 | 5 | 15 | 2 | 15 |  |
| 2021-06-24 | 50 | 49 | 12 | 12 | 2 | 18 | 3 | 9 | 3 | 9 |  |
| 2021-07-29 | 50 | 49 | 13 | 13 | 4 | 16 | 1 | 12 | 5 | 0 | yes |
| 2021-08-26 | 50 | 49 | 13 | 13 | 4 | 16 | 1 | 11 | 4 | 4 |  |
| 2021-09-30 | 50 | 49 | 14 | 14 | 3 | 17 | 4 | 10 | 2 | 10 |  |
| 2021-10-28 | 50 | 49 | 2 | 2 | 3 | 17 | 1 | 1 | 2 | 1 |  |
| 2021-11-25 | 50 | 49 | 3 | 3 | 4 | 16 | 0 | 3 | 3 | 3 |  |
| 2021-12-30 | 50 | 49 | 11 | 11 | 5 | 15 | 2 | 9 | 4 | 5 |  |
| 2022-01-27 | 50 | 49 | 7 | 7 | 11 | 9 | 1 | 6 | 7 | 0 | yes |
| 2022-02-24 | 50 | 49 | 0 | 0 | 15 | 5 | 0 | 0 | 7 | 0 |  |
| 2022-03-31 | 50 | 49 | 13 | 13 | 12 | 8 | 0 | 10 | 6 | 0 | yes |
| 2022-04-28 | 50 | 49 | 12 | 12 | 12 | 8 | 1 | 8 | 5 | 0 | yes |
| 2022-05-26 | 50 | 49 | 14 | 14 | 14 | 6 | 2 | 6 | 5 | 0 | yes |
| 2022-06-30 | 50 | 49 | 3 | 3 | 15 | 5 | 0 | 3 | 5 | 0 | yes |
| 2022-07-28 | 50 | 49 | 31 | 20 | 11 | 9 | 0 | 10 | 5 | 0 | yes |
| 2022-08-25 | 50 | 49 | 7 | 7 | 9 | 11 | 1 | 6 | 5 | 0 | yes |
| 2022-09-29 | 50 | 49 | 5 | 5 | 11 | 9 | 1 | 3 | 5 | 0 | yes |
| 2022-10-27 | 50 | 49 | 21 | 20 | 7 | 13 | 0 | 13 | 5 | 0 | yes |
| 2022-11-24 | 50 | 49 | 18 | 18 | 10 | 10 | 0 | 10 | 5 | 0 | yes |
| 2022-12-29 | 50 | 49 | 6 | 6 | 11 | 9 | 0 | 6 | 5 | 0 | yes |
| 2023-01-25 | 50 | 49 | 4 | 4 | 14 | 6 | 0 | 2 | 5 | 0 | yes |
| 2023-02-23 | 50 | 49 | 3 | 3 | 14 | 6 | 0 | 1 | 5 | 0 | yes |
| 2023-03-29 | 50 | 49 | 4 | 4 | 14 | 6 | 1 | 2 | 5 | 0 | yes |
| 2023-04-27 | 50 | 49 | 23 | 20 | 10 | 10 | 0 | 10 | 4 | 5 |  |
| 2023-05-25 | 50 | 49 | 13 | 13 | 8 | 12 | 1 | 10 | 4 | 4 |  |
| 2023-06-28 | 50 | 49 | 18 | 18 | 8 | 12 | 1 | 12 | 4 | 5 |  |
| 2023-07-27 | 51 | 49 | 11 | 11 | 6 | 14 | 0 | 11 | 4 | 5 |  |
| 2023-08-31 | 51 | 49 | 3 | 3 | 8 | 12 | 0 | 3 | 4 | 3 |  |
| 2023-09-28 | 50 | 49 | 3 | 3 | 8 | 12 | 0 | 3 | 4 | 3 |  |
| 2023-10-26 | 50 | 49 | 0 | 0 | 8 | 12 | 0 | 0 | 4 | 0 |  |
| 2023-11-30 | 50 | 49 | 29 | 20 | 6 | 14 | 1 | 14 | 3 | 13 |  |
| 2023-12-28 | 50 | 49 | 37 | 20 | 5 | 15 | 2 | 15 | 3 | 11 |  |
| 2024-01-25 | 50 | 49 | 11 | 11 | 7 | 13 | 0 | 11 | 6 | 0 | yes |
| 2024-02-29 | 50 | 49 | 11 | 11 | 6 | 14 | 2 | 9 | 5 | 1 |  |
| 2024-03-28 | 50 | 49 | 19 | 19 | 7 | 13 | 2 | 14 | 5 | 1 |  |
| 2024-04-25 | 50 | 49 | 18 | 18 | 5 | 15 | 1 | 15 | 2 | 15 |  |
| 2024-05-30 | 50 | 49 | 5 | 5 | 6 | 14 | 1 | 4 | 3 | 4 |  |
| 2024-06-27 | 50 | 49 | 21 | 20 | 5 | 15 | 1 | 15 | 2 | 15 |  |
| 2024-07-25 | 50 | 49 | 14 | 14 | 5 | 15 | 0 | 13 | 5 | 4 |  |
| 2024-08-29 | 50 | 49 | 16 | 16 | 3 | 17 | 0 | 15 | 2 | 14 |  |
| 2024-09-26 | 50 | 49 | 25 | 20 | 2 | 18 | 0 | 18 | 2 | 13 |  |
| 2024-10-31 | 50 | 50 | 3 | 3 | 17 | 3 | 1 | 2 | 14 | 0 | yes |
| 2024-11-28 | 50 | 50 | 1 | 1 | 18 | 2 | 0 | 1 | 14 | 0 | yes |
| 2024-12-26 | 50 | 50 | 3 | 3 | 18 | 2 | 0 | 2 | 14 | 0 | yes |
| 2025-01-30 | 51 | 50 | 9 | 9 | 19 | 1 | 0 | 1 | 13 | 0 | yes |
| 2025-02-27 | 50 | 50 | 6 | 6 | 18 | 2 | 0 | 2 | 12 | 0 | yes |
| 2025-03-27 | 50 | 50 | 26 | 20 | 16 | 4 | 0 | 4 | 11 | 0 | yes |
| 2025-04-24 | 50 | 50 | 34 | 20 | 14 | 6 | 0 | 6 | 10 | 0 | yes |
| 2025-05-29 | 50 | 50 | 14 | 14 | 13 | 7 | 0 | 7 | 9 | 0 | yes |
| 2025-06-26 | 50 | 50 | 21 | 20 | 10 | 10 | 0 | 10 | 7 | 0 | yes |
| 2025-07-31 | 50 | 50 | 3 | 3 | 15 | 5 | 0 | 2 | 7 | 0 | yes |
| 2025-08-28 | 50 | 50 | 3 | 3 | 13 | 7 | 1 | 2 | 6 | 0 | yes |
| 2025-09-30 | 50 | 50 | 3 | 3 | 12 | 8 | 0 | 3 | 5 | 0 |  |
| 2025-10-28 | 51 | 50 | 13 | 13 | 9 | 11 | 0 | 11 | 4 | 4 |  |
| 2025-11-25 | 50 | 50 | 5 | 5 | 12 | 8 | 0 | 5 | 6 | 0 | yes |
| 2025-12-30 | 51 | 50 | 4 | 4 | 10 | 10 | 0 | 3 | 4 | 3 |  |
| 2026-01-27 | 51 | 50 | 12 | 12 | 9 | 11 | 0 | 11 | 4 | 4 |  |
| 2026-02-24 | 50 | 50 | 10 | 10 | 13 | 7 | 0 | 7 | 5 | 0 | yes |
| 2026-03-30 | 50 | 50 | 1 | 1 | 17 | 3 | 0 | 1 | 5 | 0 | yes |
| 2026-04-28 | 50 | 50 | 16 | 16 | 14 | 6 | 0 | 7 | 4 | 5 |  |
| 2026-05-26 | 50 | 50 | 9 | 9 | 13 | 7 | 0 | 6 | 4 | 6 |  |
| 2026-06-30 | 50 | 50 | 12 | 12 | 14 | 6 | 0 | 0 | 6 | 0 | yes |
| **Mean** | 50.1 | 49.0 | 12.2 | 10.6 | 8.5 | 11.5 | 1.3 | 7.1 | 5.1 | 3.6 | 34 months |

Months with no name above 1.5: 2020-02-27, 2021-03-25, 2022-02-24, 2023-10-26. Each came after a market
pullback: NIFTY 50 was down 2.6–6.0% over the prior month and 5.9–11.3% below its 3-month high. Few names sat above
their trend, and the strategy stayed in cash that month.

## 5. Reproduce

```bash
python scripts/wheel/expiry_rankings.py         # scores per expiry -> data/signals/expiry_rankings.csv
python scripts/wheel/rank_vs_return_plot.py     # score vs next-expiry return, exercise rate by cutoff
python scripts/wheel/threshold_compare.py       # 5x backtest at thresholds 2.0 / 1.5 / 1.0 / 0.5
python scripts/wheel/selection_funnel.py        # month-by-month funnel at 1x and 5x
```
