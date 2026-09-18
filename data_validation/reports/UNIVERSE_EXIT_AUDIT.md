# Universe Removal / Position Exit Audit

Ranked NIFTY 50 wheel, full backtest window, every leverage in `leverage_grid`. Universe = validated historical NIFTY 50 membership (`data/universe/nifty50_membership_validated.csv`). A removal is known only from its effective date; the exit is decided on that close and filled the next trading day.

| Leverage | Removals in window | With existing positions | Exit records | Cleared | Residual positions | New option trades after removal | Data-unavailable flags | Closed by own expiry on removal date | Exit P&L (₹) | Exit costs (₹) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1× | 19 | 3 | 3 | 3 | 0 | 0 | 0 | 1 | -238,509 | 2,505 |
| 2× | 19 | 2 | 2 | 2 | 0 | 0 | 0 | 1 | 460,509 | 6,248 |
| 3× | 19 | 1 | 1 | 1 | 0 | 0 | 0 | 1 | 245,828 | 4,836 |
| 5× | 19 | 1 | 1 | 1 | 0 | 0 | 0 | 0 | 540,821 | 10,563 |

Positions held on a removal date: 1×: GAIL 2021-03-31; IOC 2022-03-31; UPL 2024-03-28; 2×: GAIL 2021-03-31; IOC 2022-03-31; 3×: GAIL 2021-03-31; 5×: GAIL 2021-03-31

Expected: residual positions after required universe exit = 0; new positions after effective removal = 0.

**Result: PASS**

Per-exit detail: `data_validation/reports/universe_exit_audit_detail.csv`.
