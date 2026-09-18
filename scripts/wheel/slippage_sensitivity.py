#!/usr/bin/env python3
"""Slippage sensitivity: every leverage in `leverage_grid` rerun at every level in the slippage schedule.

    python scripts/wheel/slippage_sensitivity.py

EOD data cannot see the bid/ask, so option fills are close ∓ max(ticks × tick, pct × close) per leg and stock sales
lose `equity_sale_bps`; levels are in data/costs/slippage_schedule.csv. Statutory charges are identical across levels.
Assignment and call-away settle at the strike through NSE Clearing, so they carry no slippage at any level.
Outputs: data/backtest/report/slippage_sensitivity/ (summary.csv, cagr_grid.csv, nav_comparison_L5.csv, cagr_vs_slippage.png).
Runs are not written to data/backtest/runs/ (the `base` level is the headline run from backtest.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nse.wheel.costs import CostModel  # noqa: E402
from nse.wheel.metrics import nav_stats  # noqa: E402
from nse.wheel.runner import Context, load_params, run  # noqa: E402

LEVELS = ["zero", "low", "base", "high", "stress"]


def main():
    p = load_params()
    ctx = Context(p)
    out = ROOT / p["base_dir"] / "report" / "slippage_sensitivity"
    out.mkdir(parents=True, exist_ok=True)
    sched = pd.read_csv(ROOT / p["slippage_file"]).drop_duplicates("level").set_index("level")

    rows, navs_l5 = [], {}
    for lvl in LEVELS:
        ctx.costs = CostModel(ROOT / p["costs_file"], ROOT / p["slippage_file"], lvl)
        for L in p["leverage_grid"]:
            r = run(ctx, run_id=None, write=False, leverage=L)
            nav = r["daily"].set_index("date").nav
            m, st = r["metrics"], nav_stats(nav, ctx.risk_free)
            tr = pd.concat([r["trades"], r["open"]])
            s = sched.loc[lvl]
            rows.append({"slippage_level": lvl, "ticks": s.ticks, "pct_of_premium": s.pct,
                         "equity_sale_bps": s.equity_sale_bps, "leverage": L, "final_nav": nav.iloc[-1],
                         "cagr": st["cagr"], "ann_vol": st["annualized_vol"], "sharpe": st["sharpe"],
                         "max_drawdown": st["max_drawdown"], "n_puts_sold": m["n_puts_sold"],
                         "transaction_costs": tr.transaction_costs.sum()})
            if L == 5.0:
                navs_l5[lvl] = nav
            print(f"{lvl:7s} L{L:g}  final {nav.iloc[-1]:>14,.0f}  CAGR {st['cagr']:7.2%}  "
                  f"MDD {st['max_drawdown']:7.2%}  Sharpe {st['sharpe']:5.2f}  puts {m['n_puts_sold']}", flush=True)

    df = pd.DataFrame(rows)
    base = df[df.slippage_level == "base"].set_index("leverage").cagr
    df["cagr_vs_base"] = df.cagr - df.leverage.map(base)
    df.to_csv(out / "summary.csv", index=False)
    grid = df.pivot(index="slippage_level", columns="leverage", values="cagr").loc[LEVELS]
    grid.columns = [f"L{c:g}" for c in grid.columns]
    grid.to_csv(out / "cagr_grid.csv")
    pd.DataFrame(navs_l5).rename_axis("date").to_csv(out / "nav_comparison_L5.csv")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 5))
    for c in grid.columns:
        ax.plot(LEVELS, grid[c] * 100, marker="o", label=c)
    ax.set_ylabel("CAGR, %"); ax.set_xlabel("slippage level"); ax.set_title("CAGR vs per-leg slippage assumption")
    ax.grid(alpha=.3); ax.legend()
    fig.tight_layout(); fig.savefig(out / "cagr_vs_slippage.png", dpi=130); plt.close(fig)
    print((grid * 100).round(2).to_string())


if __name__ == "__main__":
    main()
