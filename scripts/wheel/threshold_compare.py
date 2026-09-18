#!/usr/bin/env python3
"""Rank-threshold sensitivity at 5x leverage: one run per rank_final threshold, NAVs compared.

    python scripts/wheel/threshold_compare.py

Outputs: data/backtest/runs/ranked_L5_th<threshold>/ per run, and data/backtest/report/threshold_L5/
(nav_comparison.csv, summary.csv, monthly_returns.csv, nav_comparison.png, drawdown_comparison.png).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nse.wheel.metrics import drawdown, monthly_returns, nav_stats  # noqa: E402
from nse.wheel.runner import Context, load_params, run  # noqa: E402

THRESHOLDS = [2.0, 1.5, 1.0, 0.5]
LEVERAGE = 5.0


def main():
    p = load_params()
    ctx = Context(p)
    out = ROOT / p["base_dir"] / "report" / "threshold_L5"
    out.mkdir(parents=True, exist_ok=True)

    navs, rows = {}, []
    for th in THRESHOLDS:
        rid = f"ranked_L5_th{th:g}"
        r = run(ctx, run_id=rid, leverage=LEVERAGE, rank_threshold=th)
        nav = r["daily"].set_index("date").nav
        navs[f"rank>{th:g}"] = nav
        m, st = r["metrics"], nav_stats(nav, ctx.risk_free)
        sel = r["selection_log"]
        rows.append({"threshold": th, "final_nav": nav.iloc[-1], "total_pnl": nav.iloc[-1] - p["initial_capital"],
                     "cagr": st["cagr"], "ann_vol": st["annualized_vol"], "sharpe": st["sharpe"],
                     "sortino": st["sortino"], "max_drawdown": st["max_drawdown"], "calmar": st["calmar"],
                     "avg_names_selected": sel.n_selected.mean(), "n_puts_sold": m["n_puts_sold"],
                     "assignment_rate": m["assignment_rate"], "margin_call_liquidations": m["n_margin_call_liquidations"],
                     "financing_cost": m["financing_cost"], "cash_interest_income": m["cash_interest_income"], "avg_margin_util": m["avg_margin_utilization"]})
        print(f"{rid:18s} final {nav.iloc[-1]:>14,.0f}  CAGR {st['cagr']:7.2%}  MDD {st['max_drawdown']:7.2%}  "
              f"Sharpe {st['sharpe']:5.2f}  avg names {sel.n_selected.mean():4.1f}  "
              f"margin calls {m['n_margin_call_liquidations']}", flush=True)

    eq = pd.DataFrame(navs); eq.index.name = "date"
    eq.to_csv(out / "nav_comparison.csv")
    pd.DataFrame(rows).to_csv(out / "summary.csv", index=False)
    mr = pd.DataFrame({k: monthly_returns(s) for k, s in navs.items()})
    mr.index = mr.index.strftime("%Y-%m")
    mr.to_csv(out / "monthly_returns.csv")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(12, 6))
    for k, s in navs.items():
        ax.plot(s.index, s / 1e7, label=k, lw=1.3)
    ax.set_yscale("log"); ax.set_ylabel("NAV, ₹ crore (log)")
    ax.set_title("5× leverage wheel — NAV by rank_final threshold"); ax.grid(alpha=.3); ax.legend()
    fig.tight_layout(); fig.savefig(out / "nav_comparison.png", dpi=130); plt.close(fig)
    fig, ax = plt.subplots(figsize=(12, 5))
    for k, s in navs.items():
        ax.plot(s.index, drawdown(s) * 100, label=k, lw=1.1)
    ax.set_ylabel("drawdown, %"); ax.set_title("5× leverage wheel — drawdown by threshold"); ax.grid(alpha=.3); ax.legend()
    fig.tight_layout(); fig.savefig(out / "drawdown_comparison.png", dpi=130); plt.close(fig)
    print(f"report -> {out}")


if __name__ == "__main__":
    main()
