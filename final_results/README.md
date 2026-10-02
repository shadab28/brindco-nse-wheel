# Brindco NIFTY 50 wheel — final results

Built 2026-10-02 by `scripts/final_results.py` from the current run outputs. Window 2019-12-31 → 2026-06-30, ₹20,000,000 start, identical for every series.

## 1. Chosen parameters and the best alternative found

| Parameter | **Chosen (params.yaml)** | Best alternative in the L3 strike grid |
|---|---|---|
| Leverage | **3×** | 3× |
| Put strike | **≤ spot × (1 − 4%)** | ≤ spot × (1 − 7%) |
| Call strike | **≥ spot × (1 + 3%)**, never below cost basis | ≥ spot × (1 + 2%) |
| Put stop-loss | **15% below spot at sale** | same |
| Names / rank threshold | **top 20, rank_final > 1.5** | same |
| Slippage assumption | **base** | same |
| CAGR | **18.05%** | 16.83% |
| Max drawdown | **-22.25%** | -12.29% |
| Sharpe | **0.91** | 1.13 |
| Calmar | **0.81** | 1.37 |
| NAV P&L | **₹3.88 cr** | ₹3.49 cr |

- **Chosen** is what `params.yaml` runs and what every other table here reports.
- **Best alternative** is the highest-Calmar cell of the 36-run put × call grid at 3×. The put 7% row is also the most stable row (highest average Calmar). Across its six call offsets it has CAGR 16.27%–17.08%, max drawdown -17.22% to -12.29%, and Calmar 0.94–1.37.
- **The trade-off:** the chosen setting earns 1.22 more CAGR points for 9.96 more points of drawdown. The chosen strikes were picked from the grid *before* the stop-loss existed, when they were the best cell. With the stop on, the put 7% row dominates on risk.
- **The case against switching:** the stop-loss (15%) and this grid were both fitted on the same window, so re-optimising again compounds the in-sample selection.
- **What the out-of-sample test says (section 8):** fitted on 2020–2023 and run from flat on 2024-01 → 2026-06, the chosen setting made 4.83% CAGR (Sharpe -0.01, max DD -25.32%); the put 7% / call 2% alternative made 9.83% (Sharpe 0.41, max DD -14.60%).

## 2. Headline performance vs benchmarks

|                                                                                             | final_nav   | cagr   | annualized_vol   |   sharpe |   sortino | max_drawdown   | max_dd_peak   | max_dd_trough   | max_dd_recovered   |   calmar |
|:--------------------------------------------------------------------------------------------|:------------|:-------|:-----------------|---------:|----------:|:---------------|:--------------|:----------------|:-------------------|---------:|
| ranked_L1                                                                                   | ₹36,499,660 | 9.70%  | 4.89%            |     0.88 |      1.22 | -6.54%         | 2024-09-27    | 2025-02-28      | 2025-05-12         |     1.48 |
| ranked_L2                                                                                   | ₹51,183,169 | 15.56% | 10.67%           |     0.94 |      1.31 | -15.67%        | 2024-09-27    | 2025-04-07      | 2025-09-19         |     0.99 |
| **ranked_L3 (chosen)**                                                                      | ₹58,774,324 | 18.05% | 13.82%           |     0.91 |      1.26 | -22.25%        | 2024-09-27    | 2025-02-28      | not recovered      |     0.81 |
| ranked_L4                                                                                   | ₹57,353,566 | 17.60% | 16.35%           |     0.77 |      1.05 | -29.99%        | 2024-09-27    | 2025-03-03      | not recovered      |     0.59 |
| ranked_L5                                                                                   | ₹52,042,769 | 15.86% | 19.29%           |     0.6  |      0.82 | -35.50%        | 2024-09-27    | 2025-03-03      | not recovered      |     0.45 |
| NIFTY 50 TRI buy-and-hold                                                                   | ₹42,332,673 | 12.23% | 17.99%           |     0.45 |      0.61 | -38.27%        | 2020-01-14    | 2020-03-23      | 2020-11-06         |     0.32 |
| Selected universe buy-and-hold (20 names picked 2019-12-31, equal weight, never rebalanced) | ₹42,402,299 | 12.26% | 13.20%           |     0.56 |      0.72 | -18.12%        | 2026-04-29    | 2026-06-25      | not recovered      |     0.68 |
| Selected universe, equal weight, rebalanced to the top-20 each expiry                       | ₹36,883,386 | 9.88%  | 10.46%           |     0.47 |      0.65 | -17.00%        | 2021-10-18    | 2022-06-17      | 2023-07-31         |     0.58 |

Sharpe and Sortino are in excess of the India 3-month T-bill. All series use the same window and capital.

## 3. Why 3× — premium sold against what it costs

|           | premium kept   | delivery-leg P&L   | share of premium given back   |   ITM puts cash-settled | NAV P&L   | CAGR   | max DD   |
|:----------|:---------------|:-------------------|:------------------------------|------------------------:|:----------|:-------|:---------|
| ranked_L1 | ₹118.1 L       | ₹-22.2 L           | 19%                           |                       0 | ₹165.0 L  | 9.70%  | -6.54%   |
| ranked_L2 | ₹319.5 L       | ₹-54.5 L           | 17%                           |                       4 | ₹311.8 L  | 15.56% | -15.67%  |
| ranked_L3 | ₹444.7 L       | ₹-92.4 L           | 21%                           |                      17 | ₹387.7 L  | 18.05% | -22.25%  |
| ranked_L4 | ₹518.4 L       | ₹-187.8 L          | 36%                           |                      32 | ₹373.5 L  | 17.60% | -29.99%  |
| ranked_L5 | ₹565.0 L       | ₹-283.8 L          | 50%                           |                      36 | ₹320.4 L  | 15.86% | -35.50%  |

|       | extra premium kept   | extra delivery-leg loss   | loss per ₹1 of extra premium   | change in NAV P&L   |
|:------|:---------------------|:--------------------------|:-------------------------------|:--------------------|
| 1 → 2 | ₹201.4 L             | ₹-32.3 L                  | ₹0.16                          | ₹146.8 L            |
| 2 → 3 | ₹125.2 L             | ₹-37.9 L                  | ₹0.30                          | ₹75.9 L             |
| 3 → 4 | ₹73.7 L              | ₹-95.4 L                  | ₹1.29                          | ₹-14.2 L            |
| 4 → 5 | ₹46.6 L              | ₹-96.0 L                  | ₹2.06                          | ₹-53.1 L            |

- **For 3×:** 2 → 3 is the last step where the extra premium more than pays for the extra stock losses. Beyond it, each extra rupee of premium costs more than a rupee on the delivery leg, and the cash-only rule leaves more ITM puts unfunded.
- **Against:** 2× has the better Sharpe (0.94 vs 0.91) and Calmar (0.99 vs 0.81), and its drawdown recovered (2025-09-19). 3×'s drawdown had not recovered by the end of the window. 3× was chosen after the L1–L5 grid was seen.

## 4. Strike selection at 3× (put × call offsets 2–7%, stop-loss on)

**CAGR**

|        | call 2%   | call 3%   | call 4%   | call 5%   | call 6%   | call 7%   |
|:-------|:----------|:----------|:----------|:----------|:----------|:----------|
| put 2% | 19.23%    | 20.42%    | 20.15%    | 17.89%    | 16.84%    | 19.19%    |
| put 3% | 18.74%    | 18.62%    | 19.19%    | 19.08%    | 19.47%    | 17.32%    |
| put 4% | 17.57%    | 18.05%    | 17.09%    | 17.92%    | 15.09%    | 17.64%    |
| put 5% | 15.26%    | 14.92%    | 17.14%    | 18.58%    | 15.42%    | 17.52%    |
| put 6% | 16.80%    | 15.88%    | 16.37%    | 16.94%    | 17.24%    | 16.95%    |
| put 7% | 16.83%    | 16.86%    | 16.86%    | 16.27%    | 16.58%    | 17.08%    |

**Max drawdown**

|        | call 2%   | call 3%   | call 4%   | call 5%   | call 6%   | call 7%   |
|:-------|:----------|:----------|:----------|:----------|:----------|:----------|
| put 2% | -30.09%   | -30.30%   | -30.28%   | -30.35%   | -30.62%   | -31.16%   |
| put 3% | -26.51%   | -26.72%   | -26.99%   | -27.10%   | -27.35%   | -28.67%   |
| put 4% | -22.48%   | -22.25%   | -20.81%   | -22.97%   | -21.66%   | -23.34%   |
| put 5% | -20.32%   | -22.08%   | -21.00%   | -20.23%   | -25.47%   | -23.09%   |
| put 6% | -16.29%   | -18.97%   | -18.75%   | -17.08%   | -19.58%   | -17.76%   |
| put 7% | -12.29%   | -15.00%   | -15.97%   | -17.22%   | -15.70%   | -15.68%   |

**Calmar**

|        |   call 2% |   call 3% |   call 4% |   call 5% |   call 6% |   call 7% |
|:-------|----------:|----------:|----------:|----------:|----------:|----------:|
| put 2% |      0.64 |      0.67 |      0.67 |      0.59 |      0.55 |      0.62 |
| put 3% |      0.71 |      0.7  |      0.71 |      0.7  |      0.71 |      0.6  |
| put 4% |      0.78 |      0.81 |      0.82 |      0.78 |      0.7  |      0.76 |
| put 5% |      0.75 |      0.68 |      0.82 |      0.92 |      0.61 |      0.76 |
| put 6% |      1.03 |      0.84 |      0.87 |      0.99 |      0.88 |      0.95 |
| put 7% |      1.37 |      1.12 |      1.06 |      0.94 |      1.06 |      1.09 |

**Average over call offsets, by put offset**

|        | CAGR   | max DD   |   Calmar |
|:-------|:-------|:---------|---------:|
| put 2% | 18.95% | -30.46%  |     0.62 |
| put 3% | 18.74% | -27.22%  |     0.69 |
| put 4% | 17.23% | -22.25%  |     0.77 |
| put 5% | 16.47% | -22.03%  |     0.75 |
| put 6% | 16.70% | -18.07%  |     0.93 |
| put 7% | 16.75% | -15.31%  |     1.11 |

- **The put offset is what controls risk.** The highest CAGR is put 2% / call 3% (20.42%), but it comes with a -30.30% drawdown.
- **The call offset matters much less.** Within a row the drawdown moves by a few points, with no consistent direction.

## 5. Slippage and stop-loss sensitivity

**CAGR by slippage level** (per option leg: zero / 1 tick or 1.25% / 2 ticks or 2.5% (base) / 4 ticks or 5% / 8 ticks or 10% of premium; stock sales 0 / 2.5 / 5 / 10 / 20 bps)

| slippage_level   | L1    | L2     | L3     | L4     | L5     |
|:-----------------|:------|:-------|:-------|:-------|:-------|
| zero             | 9.92% | 15.54% | 17.93% | 19.65% | 16.89% |
| low              | 9.82% | 15.21% | 18.02% | 19.15% | 14.31% |
| base             | 9.70% | 15.56% | 18.05% | 17.60% | 15.86% |
| high             | 9.43% | 14.46% | 16.71% | 16.96% | 14.04% |
| stress           | 8.59% | 13.07% | 15.44% | 14.16% | 11.74% |

**Put stop-loss sweep at 3×** (run at 5% / 5% strikes, before the strike change; see `STOP_LOSS.md`)

| stop   | cagr   | max_drawdown   |   sharpe |   stops |   assignments |   cash_settled | total_pnl   |
|:-------|:-------|:---------------|---------:|--------:|--------------:|---------------:|:------------|
| off    | 14.10% | -20.48%        |     0.69 |       0 |            75 |             27 | ₹2.71 cr    |
| -10%   | 16.92% | -15.52%        |     1.03 |      64 |            58 |              2 | ₹3.52 cr    |
| -12%   | 15.38% | -27.18%        |     0.81 |      44 |            68 |              5 | ₹3.06 cr    |
| -15%   | 18.58% | -20.23%        |     1.03 |      20 |            74 |             16 | ₹4.05 cr    |
| -20%   | 15.01% | -22.13%        |     0.72 |       4 |            77 |             25 | ₹2.96 cr    |

## 6. P&L decomposition and wheel diagnostics — chosen run

| Premium sold | Premium kept | Delivery-leg P&L | Dividends | Costs | Cash interest | **NAV P&L** |
|---|---|---|---|---|---|---|
| ₹577.8 L | ₹444.7 L | ₹-92.4 L | ₹20.2 L | −₹38.3 L | ₹53.5 L | **₹387.7 L** |

- **Puts:** 505 sold. 18.8% were delivered, and 17 ITM puts were cash-settled because there wasn't enough cash to take delivery.
- **After delivery:** 87.4% of delivered cycles were called away. Stock was held 129 days on average after delivery.
- **Premium capture:** 77.0% of premium sold was kept (wheel convention). Counting the intrinsic value paid on ITM options against the premium, the strict figure is -5.6%.
- **Reconciliation:** the parts add up to the NAV change to within ₹1. Per-stock detail, the worst-drawdown walk-through and the full bias discussion are in `ANALYSIS.md`.

## 7. Monte Carlo: the number after the selection haircut

10,000 stationary block-bootstrap histories (mean block 21 trading days) of the chosen run, and the same histories for all 58 distinct variants seen before it was chosen (leverage, strike grid, stop sweep, threshold). Full method and checks: `MONTE_CARLO.md`.

| | Backtest | **Monte Carlo, after selection haircut** |
|---|---|---|
| CAGR | 18.05% | **14.04%** (90% range 2.32% to 26.58%) |
| Sharpe | 0.91 | **0.72** |
| Max drawdown | -22.25% | **-35.66%** on a 1-in-20 path |
| P(CAGR below NIFTY 50 TRI) | | 20% |
| Reality Check p-value | | 0.076 |

- **Match:** the simulation's scorer reproduces `metrics.json` exactly, and its median CAGR / Sharpe sit on the backtest, which itself lands near the 50th percentile on every metric. The backtest was a typical path, not a lucky one.
- **Haircut:** put 4% / call 3% was the best-CAGR cell of the strike grid. On each simulated history the best-CAGR cell of that grid beats its own average by 4.01% CAGR, so that is taken off. Other pools and criteria give 12.27%–17.89%.
- **For the strategy:** with every variant's edge set to zero, the best of all 58 reaches a Sharpe of 0.33 on average (1.01 at the 95th percentile), far below the variants' own. They trade the same names on the same days, so they behave like only 2.7 independent bets. Before the haircut the chosen run beats the NIFTY 50 TRI in 80% of histories.
- **Against:** at p = 0.076 the chosen run does not clear the 5% bar once the search is counted. The best observed variant (L3 put0.07_call0.02, Sharpe 1.13) does (p = 0.029). The bootstrap cannot produce a crash worse than March 2020, so the drawdown tail is a floor, not a ceiling.

## 8. Out of sample: walk-forward, overfitting probability, Reality Check re-checked

Every parameter that was tuned on the full window (leverage 1–5 × put 2–7% × call 2–7% × stop off/10/12/15/20% = 900 configs) re-run end to end, then tested on years it was not fitted on. Full method and checks: `WALK_FORWARD.md`.

| Fit 2020–2023, test 2024-01 → 2026-06 (from flat) | In sample | **Out of sample** |
|---|---|---|
| Chosen params CAGR | 25.37% | **4.83%** (rank 681 of 900) |
| Chosen params Sharpe | 1.42 | **-0.01** |
| Chosen params max drawdown | -20.45% | -25.32% |
| NIFTY 50 TRI CAGR | 16.96% | 5.08% |
| Average 3M T-bill | | 6.08% |

**If the pick had been made on 2020–2023 only**

| pool | picked by | winner | in-sample CAGR | OOS CAGR | OOS Sharpe | OOS max DD |
|---|---|---|---|---|---|---|
| all 900 | cagr | `L4_p0.02_c0.05_s0.15` | 33.96% | 3.67% | -0.04 | -34.93% |
| all 900 | sharpe | `L2_p0.07_c0.03_s0.1` | 13.53% | 8.28% | 0.55 | -3.60% |
| all 900 | calmar | `L1_p0.07_c0.03_s0.1` | 8.31% | 6.92% | 0.44 | -1.79% |
| L3 only (180) | cagr | `L3_p0.02_c0.03_s0.12` | 29.94% | 2.90% | -0.14 | -27.64% |
| L3 only (180) | sharpe | `L3_p0.07_c0.03_s0.1` | 18.98% | 9.11% | 0.49 | -7.88% |
| L3 only (180) | calmar | `L3_p0.07_c0.03_s0.1` | 18.98% | 9.11% | 0.49 | -7.88% |

**Anchored walk-forward, re-fit every January, test years 2022 → 2026 H1 stitched**

| stream                          | CAGR   |   Sharpe | max DD   |   Calmar |
|:--------------------------------|:-------|---------:|:---------|---------:|
| shipped params (fixed)          | 10.21% |     0.39 | -28.69%  |     0.36 |
| NIFTY 50 TRI                    | 8.64%  |     0.25 | -15.84%  |     0.55 |
| walk-forward, pick by cagr      | 2.21%  |    -0.1  | -35.62%  |     0.06 |
| walk-forward, pick by sharpe    | 8.41%  |     0.59 | -5.63%   |     1.49 |
| walk-forward, pick by calmar    | 7.19%  |     0.62 | -2.21%   |     3.25 |
| L3 walk-forward, pick by cagr   | 3.18%  |    -0.12 | -31.60%  |     0.1  |
| L3 walk-forward, pick by sharpe | 9.82%  |     0.61 | -9.17%   |     1.07 |
| L3 walk-forward, pick by calmar | 9.82%  |     0.61 | -9.17%   |     1.07 |

| Overfitting and data-snooping tests | value |
|---|---|
| Rank correlation, in-sample vs OOS CAGR (all 900 / L3 only) | -0.26 / -0.15 |
| Rank correlation, in-sample vs OOS Sharpe (all 900 / L3 only) | 0.50 / 0.30 |
| Probability of backtest overfitting, CSCV (all 900 / L3 only) | 0.42 / 0.41 |
| Reality Check p, chosen run (published → reproduced) | 0.076 → 0.076 |
| … over 5 seeds × block lengths 5–63 days | 0.053 – 0.083 |
| Hansen SPA p, best of 58 variants vs T-bill | 0.010 |
| Hansen SPA p, best of 58 variants vs NIFTY 50 TRI | 0.316 |
| Chosen params alone, OOS: p(Sharpe ≤ 0) / p(no edge over NIFTY) | 0.52 / 0.52 |

- **The Reality Check figure stands.** p = 0.076 reproduces exactly from the same seed and stays between 0.053 and 0.083 across seeds and block lengths, never below 0.05. Its null is *no variant beats the T-bill*. Against that bar the best of the search does clear it under Hansen's SPA (p = 0.010). Against the NIFTY 50 TRI nothing does: SPA p = 0.32.
- **The chosen params did not validate.** On 2020–2023 they ranked 189 of 900 by CAGR; from 2024 they made 4.83%, below the T-bill, and rank 681 of 900.
- **Selecting by CAGR is the failure.** CAGR ranks reverse out of sample. The CAGR-picked walk-forward made 2.21% a year with a -35.62% drawdown. Picking by Sharpe at 3× made 9.82%, Sharpe 0.61, max DD -9.17%: the far-OTM put row the strike grid already flagged as the risk-efficient alternative.
- **For the chosen params:** run fixed through the same test years they made 10.21% a year vs NIFTY 8.64%. PBO is 0.42, below the 0.5 coin-flip line, so the search is not pure noise. And a bull-then-sell-off sequence reverses CAGR ranks on its own: leverage and tight puts pay in rallies and lose in sell-offs.
- **Against:** that fixed stream used hindsight on those very years, and still has the worse Sharpe (0.39 vs 0.61) and drawdown (-28.69% vs -9.17%). The test window holds one sell-off, so it is short evidence either way. Only data after 2026-06-30 is truly unseen.

## 9. Verdict and what it rests on

- **Deployability:** the 3× / put 4% / call 3% setting is not validated out of sample. Plan on the Monte Carlo figure (14.04% CAGR, Sharpe 0.72) only as an upper bound; the one clean out-of-sample stretch earned 4.83%. The out-of-sample evidence favours the far-OTM put row (put 7%): in the full-window grid it gave up 1.2 CAGR points for a drawdown 10.0 points smaller, and out of sample it made 9.83% vs 4.83%. That alternative was also found on this data. It should run on paper or at small size before any capital is committed.
- **Pre-tax.** Almost all of the return is short-term premium income, which is the most heavily taxed kind.
- **Unfunded ITM puts are closed at intrinsic value on expiry day.** A broker would square them off earlier, at worse prices, so this flatters the result, and more so at higher leverage.
- **Cash interest** assumes idle cash earns the T-bill rate. A broker pays nothing on margin cash.
- **The margin model is simplified:** strike ÷ leverage, not NSE's SPAN plus exposure margin.
- **The worst drawdown is essentially one expiry (October 2024).** Staggering puts across expiries is the highest-value next change.

## Reproduce

```
python scripts/wheel/backtest.py run
python scripts/wheel/slippage_sensitivity.py
python scripts/wheel/required_analysis.py
python scripts/wheel/strike_grid.py
python scripts/wheel/stop_loss_probe.py rerun     # the stop sweep (pinned to 5% / 5% strikes)
python scripts/wheel/monte_carlo.py               # bootstrap of the chosen run and every variant
python scripts/wheel/walk_forward.py              # 900-config out-of-sample test, PBO, Reality Check re-check
python scripts/final_results.py
```

## Files

- `chosen_params.yaml`: the parameters in force for every number here.
- `ANALYSIS.md`: brief section 7 in full (per-stock results, worst-drawdown walk-through, biases).
- `STOP_LOSS.md`: the put stop-loss rule and why it is set at 15%.
- `MONTE_CARLO.md`: bootstrap of the chosen run and every variant, Reality Check, selection haircut.
- `WALK_FORWARD.md`: 900-config out-of-sample split, anchored walk-forward, PBO, Reality Check / SPA re-check.
- `tables/`: `portfolio_metrics.csv`, `pnl_decomposition_by_leverage.csv`, `wheel_diagnostics_by_leverage.csv`, `per_underlying_L3.csv`, `strike_grid_L3.csv`, `slippage_sensitivity.csv`, `stop_loss_sweep.csv`, `stress_pnl_by_symbol_L3.csv`, `stress_deliveries_L3.csv`, `monthly_returns.csv`, `stress_windows.csv`, `regime_performance.csv`, `monte_carlo_final_number.csv`, `monte_carlo_summary.csv`, `monte_carlo_variants.csv`, `monte_carlo_selection.csv`, `monte_carlo_reality_check.csv`, `monte_carlo_deflated_sharpe.csv`, `walk_forward_split_shipped.csv`, `walk_forward_split_winners.csv`, `walk_forward_split_rank_correlation.csv`, `walk_forward_split_by_config.csv`, `walk_forward_walk_forward_folds.csv`, `walk_forward_walk_forward_stitched.csv`, `walk_forward_pbo.csv`, `walk_forward_reality_check_recheck.csv`, `walk_forward_reality_check_sensitivity.csv`, `walk_forward_spa.csv`
- `charts/`: `equity_curve.png`, `drawdown_curve.png`, `margin_utilization.png`, `strike_grid_heatmaps_L3.png`, `cagr_vs_slippage.png`, `monte_carlo_nav_fan.png`, `monte_carlo_distributions.png`, `monte_carlo_variants_cagr.png`, `monte_carlo_reality_check.png`, `walk_forward_is_vs_oos.png`, `walk_forward_walk_forward_equity.png`, `walk_forward_pbo.png`
- `results_pack/`: the submission bundle — equity and drawdown charts, the per-name summary, and the cycle and fill trade logs for `ranked_L3`. See `results_pack/README.md`.
