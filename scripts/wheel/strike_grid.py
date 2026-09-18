#!/usr/bin/env python3
"""Put × call strike-offset grid at the headline leverage (params.yaml `leverage`, L3).

    python scripts/wheel/strike_grid.py

Every combination of put offset p and call offset c in OFFSETS is one full backtest:
    put strike  = highest liquid listed strike <= spot × (1 − p)   (params `put_strike_filter_pct`)
    call strike = lowest liquid listed strike >= spot × (1 + c)    (params `call_strike_filter_pct`)
Everything else is the headline configuration (ranking, threshold, N, costs, base slippage, cash-only assignment).
Outputs: data/backtest/report/strike_grid_L<L>/ (grid.csv plus one pivot per metric, heatmaps.png).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nse.wheel.metrics import nav_stats  # noqa: E402
from nse.wheel.runner import Context, load_params, run  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts" / "wheel"))
from required_analysis import decomposition, diagnostics  # noqa: E402

OFFSETS = [0.02, 0.03, 0.04, 0.05, 0.06, 0.07]


def main():
    p = load_params()
    L = p["leverage"]
    ctx = Context(p)
    out = ROOT / p["base_dir"] / "report" / f"strike_grid_L{L:g}"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for pp in OFFSETS:
        for cc in OFFSETS:
            r = run(ctx, run_id=None, write=False, leverage=L, put_strike_filter_pct=pp, call_strike_filter_pct=cc)
            nav = r["daily"].set_index("date").nav
            st = nav_stats(nav, ctx.risk_free)
            tr = pd.concat([r["trades"], r["open"]], ignore_index=True)
            dec = decomposition(tr, r["ledger"]).iloc[0]
            dg = diagnostics(r["cycles"], r["daily"].date.iloc[-1]).iloc[0]
            m = r["metrics"]
            rows.append({"put_otm": pp, "call_otm": cc, "final_nav": nav.iloc[-1], "total_pnl": nav.iloc[-1] - nav.iloc[0],
                         "cagr": st["cagr"], "ann_vol": st["annualized_vol"], "sharpe": st["sharpe"],
                         "sortino": st["sortino"], "max_drawdown": st["max_drawdown"], "calmar": st["calmar"],
                         "max_dd_peak": st["max_dd_peak"], "max_dd_trough": st["max_dd_trough"],
                         "max_dd_recovered": st["max_dd_recovered"],
                         "premium_sold": dec.premium_sold, "premium_income": dec.premium_income,
                         "delivery_leg_price_pnl": dec.delivery_leg_price_pnl, "dividends": dec.dividends,
                         "costs": dec.costs, "cash_interest": m["cash_interest_income"],
                         "puts_sold": dg.puts_sold, "calls_sold": dg.calls_sold, "assignment_rate": dg.assignment_rate,
                         "itm_put_rate": dg.itm_put_rate, "itm_cash_settled": dg.itm_cash_settled,
                         "call_away_rate": dg.call_away_rate,
                         "avg_days_held_post_delivery": dg.avg_days_held_post_delivery,
                         "margin_call_liquidations": m["n_margin_call_liquidations"]})
            print(f"put {pp:.0%} call {cc:.0%}  CAGR {st['cagr']:7.2%}  MDD {st['max_drawdown']:7.2%}  "
                  f"Sharpe {st['sharpe']:.2f}  Calmar {st['calmar']:.2f}  P&L {nav.iloc[-1] - nav.iloc[0]:>13,.0f}", flush=True)
    g = pd.DataFrame(rows)
    g.to_csv(out / "grid.csv", index=False)
    for col in ["cagr", "max_drawdown", "sharpe", "calmar", "total_pnl"]:
        g.pivot(index="put_otm", columns="call_otm", values=col).to_csv(out / f"pivot_{col}.csv")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.8))
    for ax, (col, title, fmt, cmap) in zip(axes, [("cagr", "CAGR", "{:.1%}", "viridis"),
                                                  ("max_drawdown", "Max drawdown", "{:.1%}", "viridis"),
                                                  ("calmar", "Calmar (CAGR / |MDD|)", "{:.2f}", "viridis")]):
        pv = g.pivot(index="put_otm", columns="call_otm", values=col)
        im = ax.imshow(pv.values, cmap=cmap, origin="lower")
        ax.set_xticks(range(len(pv.columns)), [f"{c:.0%}" for c in pv.columns])
        ax.set_yticks(range(len(pv.index)), [f"{c:.0%}" for c in pv.index])
        ax.set_xlabel("call OTM"); ax.set_ylabel("put OTM"); ax.set_title(f"L{L:g} — {title}")
        for i in range(pv.shape[0]):
            for j in range(pv.shape[1]):
                ax.text(j, i, fmt.format(pv.values[i, j]), ha="center", va="center", fontsize=7, color="w")
        fig.colorbar(im, ax=ax, shrink=.8)
    fig.tight_layout(); fig.savefig(out / "heatmaps.png", dpi=130); plt.close(fig)
    print(f"-> {out}")


if __name__ == "__main__":
    main()
