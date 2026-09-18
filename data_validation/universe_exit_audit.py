"""Universe Removal / Position Exit Audit on the real ranked backtest (every leverage in params.leverage_grid)."""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.simplefilter("ignore")

import pandas as pd  # noqa: E402

from data_validation.common import REPORTS  # noqa: E402
from nse.wheel.runner import Context, load_params, run  # noqa: E402
from nse.wheel.selection import MembershipUniverse  # noqa: E402


def audit(r, universe: MembershipUniverse, removals: pd.DataFrame) -> dict:
    t = pd.concat([r["trades"], r["open"]], ignore_index=True)
    ex = r["universe_exits"]
    last = r["daily"].date.iloc[-1]
    first = r["daily"].date.iloc[0]
    rem = removals[(removals.effective >= first) & (removals.effective <= last)]
    # positions open on the removal day (entered before, exited on/after the effective date)
    held, expired_same_day = [], []
    for e in rem.itertuples():
        syms = {e.symbol} | {s for s in t.symbol.unique() if universe.index_symbol(s, e.effective) == e.symbol}
        g = t[t.symbol.isin(syms)]
        ed = pd.to_datetime(g.exit_date).fillna(last + pd.Timedelta(days=1))
        open_at = (pd.to_datetime(g.date) < e.effective) & (ed >= e.effective)
        # a position settled by its own contract expiry on the removal date needs no universe exit
        survives = open_at & ~((ed == e.effective) & (g.exit_reason == "CONTRACT_EXPIRY") &
                               ~g.status.isin(["assigned"]))
        if survives.any():
            held.append(e)
        elif open_at.any():
            expired_same_day.append(e)
    opts = t[t.instrument.isin(["PE", "CE"])]
    after = [x for x in opts.itertuples() if not universe(x.symbol, pd.Timestamp(x.date))]
    wheels = r["engine"].wheels
    residual = [s for s, w in wheels.items() if (w.option is not None or w.shares) and not universe(s, last)]
    by_symbol = ex.groupby("symbol") if len(ex) else []
    cleared = sum(1 for _, g in by_symbol if not g.residual.iloc[-1]) if len(ex) else 0
    return {"removals_in_window": len(rem), "removals_with_positions": len(held),
            "exit_records": len(ex), "positions_cleared": cleared,
            "residual_positions": len(residual) + (int(ex.groupby("symbol").residual.last().sum()) if len(ex) else 0),
            "new_option_trades_after_removal": len(after),
            "data_unavailable_flags": int((ex.data_unavailable != "").sum()) if len(ex) else 0,
            "exit_pnl": float(ex.realized_pnl.sum()) if len(ex) else 0.0,
            "exit_costs": float(ex.transaction_cost.sum()) if len(ex) else 0.0,
            "closed_by_expiry_on_removal_date": len(expired_same_day),
            "held": [f"{e.symbol} {e.effective.date()}" for e in held], "exits": ex}


def main():
    p = load_params()
    ctx = Context(p)
    ev = pd.read_csv(Path(__file__).resolve().parent / "output/nifty50_change_events.csv", parse_dates=["effective"])
    removals = ev[ev.action == "remove"][["effective", "symbol", "kind", "release"]]
    rows, detail = [], []
    for L in p["leverage_grid"]:
        r = run(ctx, leverage=L, write=False)
        u = MembershipUniverse(ctx.membership)
        a = audit(r, u, removals)
        rows.append({"leverage": L, **{k: v for k, v in a.items() if k not in ("exits", "held")},
                     "held_on_removal": "; ".join(a["held"])})
        if len(a["exits"]):
            detail.append(a["exits"].assign(leverage=L))
        print(L, rows[-1], flush=True)
    s = pd.DataFrame(rows)
    d = pd.concat(detail, ignore_index=True) if detail else pd.DataFrame()
    d.to_csv(REPORTS / "universe_exit_audit_detail.csv", index=False)
    md = ["# Universe Removal / Position Exit Audit", "",
          "Ranked NIFTY 50 wheel, full backtest window, every leverage in `leverage_grid`. Universe = validated historical "
          "NIFTY 50 membership (`data/universe/nifty50_membership_validated.csv`). A removal is known only from its "
          "effective date; the exit is decided on that close and filled the next trading day.", "",
          "| Leverage | Removals in window | With existing positions | Exit records | Cleared | Residual positions | "
          "New option trades after removal | Data-unavailable flags | Closed by own expiry on removal date | Exit P&L (₹) | Exit costs (₹) |",
          "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for x in s.itertuples():
        md.append(f"| {x.leverage:g}× | {x.removals_in_window} | {x.removals_with_positions} | {x.exit_records} | "
                  f"{x.positions_cleared} | {x.residual_positions} | {x.new_option_trades_after_removal} | "
                  f"{x.data_unavailable_flags} | {x.closed_by_expiry_on_removal_date} | {x.exit_pnl:,.0f} | {x.exit_costs:,.0f} |")
    md += ["", "Positions held on a removal date: " + ("; ".join(f"{x.leverage:g}×: {x.held_on_removal or 'none'}" for x in s.itertuples())),
           "", "Expected: residual positions after required universe exit = 0; new positions after effective removal = 0.",
           "", f"**Result: {'PASS' if (s.residual_positions == 0).all() and (s.new_option_trades_after_removal == 0).all() and (s.removals_with_positions == s.positions_cleared).all() else 'FAIL'}**",
           "", "Per-exit detail: `data_validation/reports/universe_exit_audit_detail.csv`."]
    (REPORTS / "UNIVERSE_EXIT_AUDIT.md").write_text("\n".join(md) + "\n")


if __name__ == "__main__":
    main()
