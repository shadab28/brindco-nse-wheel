# Phase 0 — Inventory

**Generated:** 2026-09-17T15:07:53  
**Gate:** PASS  
**Checks:** 7 run, 7 passed, 0 failed (0 critical)

## Data sources

Static scan of the backtest code (nse/wheel/data.py, nse/wheel/runner.py, nse/wheel/engine.py, nse/wheel/selection.py, nse/wheel/costs.py, nse/wheel/corporate_actions.py, nse/wheel/benchmarks.py, nse/wheel/metrics.py, scripts/wheel/backtest.py), params.yaml, the files on disk and the Postgres warehouse `nse` (127.0.0.1:5440).

## Methodology

Every params `*_file` key the code reads, every literal `data/...` path in the code and the parquet cache are collected and matched against the inventory. Date ranges and row counts are computed from the data itself. Provenance class is assigned from the producing script and its docstring.

## Tests run

INV-01..07 in this script (static scan + data queries).

## Results

| ID | Check | Critical | Checked | Failed | Status | Detail |
|---|---|---|---:|---:|---|---|
| INV-01 | Every input read by the backtest is in the inventory | yes | 17 | 0 | PASS | 17 inputs referenced by the backtest code |
| INV-02 | Every referenced input exists | yes | 17 | 0 | PASS |  |
| INV-03 | Every input has a source, provenance class and date range | yes | 27 | 0 | PASS |  |
| INV-04 | Every requested data category is mapped to an input | yes | 12 | 0 | PASS |  |
| INV-05 | Cache cash rows equal warehouse rows (RELIANCE) | no | 1 | 0 | PASS |  |
| INV-06 | Raw NSE zips cover every successfully ingested day | no | 2 | 0 | PASS | zips cm=1719, fo=1718 (to 2026-09-16); ingest_log ok cm=1687, fo=1686 (to 2026-07-31) |
| INV-07 | params *_file keys not read by the backtest (informational) | no | 1 | 0 | PASS | listed so a reader does not assume they affect results |

## Failures

None.
## Notes

- Inventory written to data_validation/reports/DATA_INVENTORY.md
