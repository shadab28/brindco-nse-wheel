#!/usr/bin/env python3
"""Daily NAV table for one backtest run (default: ranked_L3, the params.yaml strategy).

    python scripts/wheel/daily_nav.py [run_id]

Reads data/backtest/runs/<run_id>/ (daily.csv, cash_ledger.csv, config.json) and the benchmark curves in
data/backtest/report/equity_curves.csv. Writes data/backtest/report/daily_nav_<run_id>.csv.

Checks before writing: NAV == cash + stock value − option liability every day, and the day-on-day NAV change equals
the day's cash flows plus the change in stock value and option liability.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]

# cash-ledger kinds grouped into the daily P&L columns
FLOW_GROUPS = {
    "option_premium": ["premium"],
    "option_buyback": ["option_buyback"],
    "stock_bought_on_assignment": ["put_assignment_pay_strike"],
    "stock_sold_on_call_away": ["call_away_receive_strike"],
    "stock_sold_other": ["equity_sale"],
    "dividends_and_distributions": ["dividend", "ca_distribution"],
    "transaction_costs": ["option_sale_costs", "assignment_costs", "option_buyback_costs", "call_away_costs",
                          "equity_sale_costs"],
    "cash_interest": ["cash_interest"],
    "financing_cost": ["financing"],
}


def build(run_id: str) -> pd.DataFrame:
    run = ROOT / "data/backtest/runs" / run_id
    cfg = json.load(open(run / "config.json"))
    d = pd.read_csv(run / "daily.csv", parse_dates=["date"]).set_index("date")
    led = pd.read_csv(run / "cash_ledger.csv", parse_dates=["date"])

    kinds = {k for ks in FLOW_GROUPS.values() for k in ks}
    unknown = set(led.kind) - kinds
    if unknown:
        raise ValueError(f"unmapped cash-ledger kinds: {sorted(unknown)}")
    flows = led.pivot_table(index="date", columns="kind", values="amount", aggfunc="sum").reindex(d.index).fillna(0.0)
    out = pd.DataFrame(index=d.index)
    out["nav"] = d.nav
    out["nav_index"] = 100 * d.nav / cfg["initial_capital"]
    out["daily_return"] = d.nav.pct_change().fillna(0.0)
    out["cumulative_return"] = d.nav / cfg["initial_capital"] - 1
    out["drawdown"] = d.nav / d.nav.cummax() - 1
    out["daily_pnl"] = d.nav.diff().fillna(d.nav.iloc[0] - cfg["initial_capital"])
    out["cash"] = d.cash
    out["stock_value"] = d.stock_value
    out["option_liability"] = d.option_liability
    out["borrowed"] = d.borrowed
    for col, ks in FLOW_GROUPS.items():
        out[col] = flows.reindex(columns=ks, fill_value=0.0).sum(axis=1)
    out["n_short_puts"] = d.n_short_puts
    out["n_covered_calls"] = d.n_covered_calls
    out["n_stock_names"] = d.n_stock_names
    out["n_active_names"] = d.n_active_names
    out["notional_exposure"] = d.notional_exposure
    out["gross_exposure_x_nav"] = d.notional_exposure / d.nav
    out["margin_utilization"] = d.margin_utilization

    bench = pd.read_csv(ROOT / "data/backtest/report/equity_curves.csv", parse_dates=["date"]).set_index("date")
    for col in [c for c in bench.columns if not c.startswith(("ranked_", "frozen_"))]:
        s = bench[col].reindex(d.index).ffill()
        key = col.lower().replace(" ", "_").replace("&", "").replace("-", "_").replace("__", "_")
        out[f"bench_{key}_nav"] = s
        out[f"bench_{key}_index"] = 100 * s / cfg["initial_capital"]

    # reconciliation 1: balance sheet
    bs = (out.cash + out.stock_value - out.option_liability - out.nav).abs().max()
    if bs > 1.0:
        raise AssertionError(f"NAV != cash + stock − option liability (max gap {bs:,.2f})")
    # reconciliation 2: cash moves exactly by the ledger flows
    cash_prev = out.cash.shift(1).fillna(cfg["initial_capital"])
    gap = (out.cash - cash_prev - flows.sum(axis=1)).abs().max()
    if gap > 1.0:
        raise AssertionError(f"cash change != ledger flows (max gap {gap:,.2f})")
    out.index.name = "date"
    return out


def main():
    run_id = sys.argv[1] if len(sys.argv) > 1 else "ranked_L3"
    out = build(run_id)
    path = ROOT / "data/backtest/report" / f"daily_nav_{run_id}.csv"
    out.to_csv(path, float_format="%.6f")
    yrs = (out.index[-1] - out.index[0]).days / 365.25
    cagr = (out.nav.iloc[-1] / out.nav.iloc[0]) ** (1 / yrs) - 1
    vol = out.daily_return.std() * np.sqrt(252)
    print(f"{run_id}: {len(out)} days {out.index[0].date()} -> {out.index[-1].date()}  "
          f"NAV {out.nav.iloc[0]:,.0f} -> {out.nav.iloc[-1]:,.0f}  CAGR {cagr:.2%}  vol {vol:.2%}  "
          f"max DD {out.drawdown.min():.2%}")
    print(f"-> {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
