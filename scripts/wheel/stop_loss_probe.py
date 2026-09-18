#!/usr/bin/env python3
"""Evidence behind `put_stop_loss_pct` in params.yaml. Two independent measurements of the same rule.

    python scripts/wheel/stop_loss_probe.py counterfactual   # per-trade, rest of the book held fixed
    python scripts/wheel/stop_loss_probe.py rerun            # full engine re-runs at each leverage

`counterfactual` replays every short put in an existing run: if the underlying closed at or below
(1 − pct) x the close on the day the put was sold, it prices the buyback from the real option chain and
compares that to what the cycle actually earned from that put onward. It is cheap and it is WRONG in a
knowable direction — it holds the rest of the portfolio fixed, so it cannot see that stopping out early
frees the cash that would otherwise have forced an ITM put to be cash-settled under Step 9a.

`rerun` is the real measurement: the engine is re-run end to end at each threshold. It is the number that
decided the parameter. Both are reported in docs/STOP_LOSS.md because the gap between them is the point.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nse.wheel.metrics import nav_stats  # noqa: E402
from nse.wheel.runner import Context, load_params, run  # noqa: E402

LEVELS = [0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25]
RERUN_LEVELS = [None, 0.10, 0.12, 0.15, 0.20]


def counterfactual(ctx, run_id: str, pct: float) -> pd.DataFrame:
    """Per-put replay against an already-written run. Returns one row per put that would have stopped."""
    md, costs = ctx.md, ctx.costs
    t = pd.read_csv(ROOT / f"data/backtest/runs/{run_id}/trades.csv", parse_dates=["date", "expiry", "exit_date"])
    puts = t[(t.instrument == "PE") & t.expiry.notna()]
    days = pd.DatetimeIndex(md.trading_days)
    rows = []
    for r in puts.itertuples():
        stop = r.underlying_entry * (1 - pct)
        # strictly inside the contract's life: the expiry day itself is settlement, not a stop opportunity
        hit = next((d for d in days[(days > r.date) & (days < r.expiry)]
                    if (s := md.spot(d, r.symbol)) is not None and s <= stop), None)
        if hit is None:
            continue
        cm = pd.Timestamp(r.expiry).strftime("%Y-%m")
        q = md.quote(hit, r.symbol, cm, "PE", r.strike)
        if q is None or q.mark <= 0:
            rows.append({"symbol": r.symbol, "trade_id": r.trade_id, "no_quote": True})
            continue
        px = costs.option_fill(hit, q.mark, "buy", md.month_end_spot(hit, r.symbol) or 0.0)
        if px is None:
            rows.append({"symbol": r.symbol, "trade_id": r.trade_id, "no_quote": True})
            continue
        # everything the cycle earned from this put onward is what the stop forgoes
        legs = t[(t.cycle_id == r.cycle_id) & (t.trade_id.fillna(1e9) >= r.trade_id)]
        actual = legs.option_pnl.sum() + legs.stock_pnl.sum() - legs.transaction_costs.sum()
        counter = (r.entry_premium - px) * r.quantity - costs.option_buy(hit, px * r.quantity).total
        rows.append({"symbol": r.symbol, "trade_id": r.trade_id, "no_quote": False, "sold": r.date.date(),
                     "expiry": pd.Timestamp(r.expiry).date(), "spot_at_sale": r.underlying_entry,
                     "stop_level": stop, "hit_date": hit.date(), "status": r.status,
                     "actual_from_here": actual, "counterfactual": counter, "saving": counter - actual})
    return pd.DataFrame(rows)


def cmd_counterfactual(ctx, run_id="ranked_L3"):
    out, detail = [], None
    for pct in LEVELS:
        df = counterfactual(ctx, run_id, pct)
        s = df[~df.no_quote] if len(df) else df
        out.append({"stop": f"-{pct:.0%}", "stopped": len(df), "no_quote": int(df.no_quote.sum()) if len(df) else 0,
                    "helped": int((s.saving > 0).sum()) if len(s) else 0,
                    "hurt": int((s.saving < 0).sum()) if len(s) else 0,
                    "gross_gain": s.saving[s.saving > 0].sum() if len(s) else 0.0,
                    "gross_loss": s.saving[s.saving < 0].sum() if len(s) else 0.0,
                    "net_saving": s.saving.sum() if len(s) else 0.0})
        if pct == 0.15:
            detail = s
        print(f"  {pct:.0%} done", flush=True)
    df = pd.DataFrame(out)
    print(f"\n=== per-trade counterfactual, {run_id} (rest of the book held fixed) ===")
    print(df.to_string(index=False, float_format=lambda x: f"{x:,.0f}"))
    if detail is not None:
        print("\n--- at -15%, by what the put actually did ---")
        print(detail.groupby("status").agg(n=("saving", "size"), saving=("saving", "sum")).sort_values("saving"))
    return df


def cmd_rerun(ctx, leverages=(1.0, 3.0, 5.0)):
    rows = []
    for L in leverages:
        for sl in RERUN_LEVELS:
            r = run(ctx, run_id=None, write=False, leverage=L, put_stop_loss_pct=sl)
            nav = r["daily"].set_index("date").nav
            st, m = nav_stats(nav, ctx.risk_free), r["metrics"]
            rows.append({"leverage": L, "stop": "off" if sl is None else f"-{sl:.0%}",
                         "total_pnl": nav.iloc[-1] - nav.iloc[0], "cagr": st["cagr"],
                         "max_drawdown": st["max_drawdown"], "sharpe": st["sharpe"], "calmar": st["calmar"],
                         "stops": m["n_put_stop_losses"], "no_quote": m["n_put_stop_loss_no_quote"],
                         "assignments": m["n_assignments"],
                         "cash_settled": m["n_itm_puts_not_delivered_insufficient_cash"],
                         "recon": m["reconciliation_nav_vs_trades_inr"]})
            print(f"  L{L:g} {rows[-1]['stop']} done", flush=True)
    df = pd.DataFrame(rows)
    base = df[df.stop == "off"].set_index("leverage").total_pnl
    df["vs_off"] = df.total_pnl - df.leverage.map(base)
    print("\n=== full engine re-runs ===")
    print(df.to_string(index=False, float_format=lambda x: f"{x:,.4g}"))
    return df


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "rerun"
    ctx = Context(load_params())
    out = ROOT / "data/backtest/report/stop_loss"
    out.mkdir(parents=True, exist_ok=True)
    if cmd == "counterfactual":
        cmd_counterfactual(ctx).to_csv(out / "counterfactual.csv", index=False)
    elif cmd == "rerun":
        cmd_rerun(ctx).to_csv(out / "rerun.csv", index=False)
    else:
        raise SystemExit(__doc__)
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
