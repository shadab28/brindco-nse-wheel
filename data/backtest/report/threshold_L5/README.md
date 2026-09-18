# rank_threshold sensitivity at 5× — n_positions 20

Rerun on 2026-09-17 with `python scripts/wheel/threshold_compare.py`, after dividends were added (see `CORPORATE_ACTIONS.md`). `params.yaml` uses 1.5. Each run's settings are saved in
`data/backtest/runs/ranked_L5_th<threshold>/config.json`.

Settings: `n_positions: 20`, leverage 5×, thresholds 2.0 / 1.5 / 1.0 / 0.5. All other parameters come from
`params.yaml`. That includes `universe_membership_file` (a name leaving the NIFTY 50 is wound down) and
`covered_call_first: true`.

| rank_final > | Final NAV | CAGR | Max DD | Sharpe | Avg names | Puts sold | Margin calls |
|---|---|---|---|---|---|---|---|
| 2 | ₹749.1 L | 22.54% | -42.68% | 0.72 | 8.3 | 295 | 0 |
| 1.5 (current) | ₹673.8 L | 20.56% | -41.99% | 0.65 | 10.6 | 286 | 0 |
| 1 | ₹463.4 L | 13.81% | -42.59% | 0.43 | 12.9 | 248 | 0 |
| 0.5 | ₹494.3 L | 14.94% | -42.53% | 0.46 | 15.2 | 279 | 0 |

Files: `summary.csv`, `nav_comparison.csv`, `monthly_returns.csv`, `nav_comparison.png`, `drawdown_comparison.png`.

## Earlier run, now superseded

`../threshold_L5_n10_superseded/` holds the 12:03 run. It used `n_positions: 10` and an older engine without
the universe wind-down and without call-first priority:

| rank_final > | CAGR (n=10, old engine) | CAGR (n=20, current) |
|---|---|---|
| 2.0 | 33.78% | 22.54% |
| 1.5 | 29.22% | 20.56% |
| 1.0 | 21.62% | 13.81% |
| 0.5 | −39.54% (NAV ₹7.6 L) | 14.94% |

The selection rationale and the evidence for and against 1.5 are in `STOCK_SELECTION.md`.
