# Final settlement price check

Window 2020-01-01 → 2026-06-30. FSP used = CM close on expiry day (NSE rule). Cross-check = expiring stock future's settle.

| Measure | Value |
|---|---|
| (expiry, symbol) pairs | 4,626 |
| CM close missing (engine falls back to future settle) | 0 |
| Future settle missing | 1 |
| Both present | 4,625 |
| Prices differ | 897 (19.4%) |
| Median / max abs difference where they differ | 0.23 / 20.87 bps |
| Pairs with an OI-carrying strike between the two prices | 4 (8 strikes) |
| Backtest expiries checked (all runs) | 7,067 |
| Backtest assignments / call-aways that flip | 0 |

Largest differences:

| Expiry | Symbol | CM close | Future settle | bps | OI strikes between |
|---|---|---|---|---|---|
| 2023-02-23 | MARUTI | 8696.25 | 8714.40 | +20.9 | 2 |
| 2025-05-29 | MAXHEALTH | 1135.20 | 1133.80 | -12.3 | 0 |
| 2022-02-24 | IBULHSGFIN | 148.55 | 148.45 | -6.7 | 0 |
| 2020-06-25 | TCS | 2016.10 | 2014.75 | -6.7 | 0 |
| 2020-03-26 | HDFCBANK | 901.10 | 901.70 | +6.7 | 0 |
| 2025-03-27 | YESBANK | 17.26 | 17.25 | -5.8 | 0 |
| 2020-03-26 | KOTAKBANK | 1372.95 | 1373.60 | +4.7 | 0 |
| 2025-02-27 | ULTRACEMCO | 10447.65 | 10442.90 | -4.5 | 0 |
| 2020-03-26 | HINDUNILVR | 2194.90 | 2195.85 | +4.3 | 0 |
| 2020-03-26 | RELIANCE | 1066.20 | 1066.60 | +3.8 | 0 |
| 2021-02-25 | GRASIM | 1270.45 | 1270.90 | +3.5 | 0 |
| 2020-03-26 | M&M | 285.50 | 285.60 | +3.5 | 0 |
| 2021-12-30 | TATACONSUM | 728.00 | 728.25 | +3.4 | 0 |
| 2020-03-26 | HCLTECH | 447.70 | 447.85 | +3.4 | 0 |
| 2020-03-26 | TCS | 1790.95 | 1791.55 | +3.4 | 0 |
