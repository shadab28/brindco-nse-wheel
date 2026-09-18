#!/usr/bin/env python3
"""Month-by-month selection funnel: from NIFTY 50 members to puts actually sold, for each monthly entry.

For every ranking expiry E (decision at E's close, fills from E+1) the funnel counts:

    members          NIFTY 50 members on E (validated membership)
    ranked           members with a score on E (members without 15m data drop out here, U-11)
    above_threshold  rank_final > rank_threshold
    selected         the top n_positions of those
    held_at_decision        names in the book at E's close (put, stock or call)
    selected_already_held   selected names already in the book, so no new entry for them
    open_slots       n_positions − held_at_decision
    entries_blocked  names were selected but no put was even evaluated: entries deferred all month (covered
                     calls pending on assigned shares, or no cash/margin headroom)
    evaluated        names that reached the put decision
    skipped_*        why an evaluated name got no order (no liquid strike ≤ 95% of spot, distance cap, lot too
                     large for the per-name target, margin/cash, not F&O-listed, lot unresolved, termination)
    put_orders       put orders created
    new_puts_filled  new wheel cycles started before the next expiry
    names_held_before_next_expiry   names in the book on the last day before the next expiry

The engine is not changed. Its decide_put is wrapped to log each decision and the diagnostic counter it moved,
and SKIP_NEW_PUT events (entries deferred while covered calls are pending) are read from the event log.
Outputs: data/backtest/report/selection_funnel_L<leverage>.csv and selection_funnel_summary.csv.

    python scripts/wheel/selection_funnel.py
"""
from __future__ import annotations

import sys
import warnings
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
warnings.simplefilter("ignore", FutureWarning)

from nse.wheel.engine import WheelEngine  # noqa: E402
from nse.wheel.runner import Context, load_params, run  # noqa: E402

SKIP_KEYS = {
    "cycles_skipped_no_eligible_strike": "skipped_no_liquid_strike",
    "cycles_skipped_distance_cap": "skipped_distance_cap",
    "cycles_skipped_lot_too_large": "skipped_lot_too_large",
    "cycles_skipped_insufficient_margin": "skipped_margin_or_cash",
    "skip_no_spot_or_not_fo": "skipped_not_fo_listed",
    "lot_unresolved_skips": "skipped_lot_unresolved",
    "skip_termination_announced": "skipped_termination",
    "skip_not_in_universe": "skipped_left_index",
}
LOG: list[dict] = []
_orig_decide_put = WheelEngine.decide_put


def _logged_decide_put(self, t, sym, retries=0):
    before = Counter(self.diag)
    od = _orig_decide_put(self, t, sym, retries)
    moved = [k for k in self.diag if self.diag[k] != before.get(k, 0)]
    LOG.append({"t": pd.Timestamp(t), "symbol": sym, "retry": retries > 0, "order": od is not None,
                "reason": "order" if od is not None else next((SKIP_KEYS[k] for k in moved if k in SKIP_KEYS),
                                                               "skipped_other"),
                "active_at_decision": len(self.active_names())})
    return od


WheelEngine.decide_put = _logged_decide_put


def funnel(ctx, p, leverage: float) -> pd.DataFrame:
    LOG.clear()
    r = run(ctx, run_id=None, write=False, leverage=leverage)
    rk = ctx.rankings.copy()
    rk["expiry_date"] = pd.to_datetime(rk.expiry_date)
    thr, n = p["rank_threshold"], p["n_positions"]
    mem = pd.read_csv(ROOT / p["universe_membership_file"], parse_dates=["effective_from", "effective_to"])
    exps = sorted(rk.expiry_date.unique())
    exps = [e for e in exps if pd.Timestamp(p["first_signal_date"]) - pd.Timedelta(days=10) <= e <= pd.Timestamp(p["end_date"])]
    dec = pd.DataFrame(LOG)
    ev = r["events"] if len(r["events"]) else pd.DataFrame(columns=["date", "event", "reason", "entry_day"])
    ev["date"] = pd.to_datetime(ev.date)
    cyc = r["cycles"] if "cycles" in r else pd.DataFrame()
    daily = r["daily"].copy()
    daily["date"] = pd.to_datetime(daily.date)
    rows = []
    for i, e in enumerate(exps):
        nxt = exps[i + 1] if i + 1 < len(exps) else pd.Timestamp(p["end_date"]) + pd.Timedelta(days=1)
        day = rk[rk.expiry_date == e]
        lo = mem.effective_from.fillna(pd.Timestamp.min)
        hi = mem.effective_to.fillna(pd.Timestamp.max)
        members = int(((lo <= e) & (hi >= e)).sum())
        above = day[day.rank_final > thr]
        sel = above.sort_values("rank").head(n)
        d = dec[(dec.t >= e) & (dec.t < nxt) & ~dec.retry] if len(dec) else dec
        first = d.drop_duplicates("symbol") if len(d) else d
        on_e = daily[daily.date <= e].tail(1)
        held = int(on_e.n_active_names.iloc[0]) if len(on_e) else 0
        if len(cyc):
            st, en = pd.to_datetime(cyc.start), pd.to_datetime(cyc.end)
            active = set(cyc.symbol[(st <= e) & (en.isna() | (en > e))])
        else:
            active = set()
        skips = first.reason.value_counts().to_dict() if len(first) else {}
        deferred = ev[(ev.date >= e) & (ev.date < nxt) & (ev.event == "SKIP_NEW_PUT")]
        new_cycles = int(((pd.to_datetime(cyc.start) > e) & (pd.to_datetime(cyc.start) <= nxt)).sum()) if len(cyc) else 0
        last = daily[daily.date < nxt].tail(1)
        rows.append({
            "expiry": e.date(), "members": members, "ranked": len(day), "above_threshold": len(above),
            "selected": len(sel), "held_at_decision": held, "selected_already_held": len(set(sel.symbol) & active),
            "open_slots": n - held,
            "evaluated": len(first), **{k: int(skips.get(k, 0)) for k in ["order", *SKIP_KEYS.values(), "skipped_other"]},
            "entry_deferred_days": int(deferred.date.nunique()),
            "deferred_reason": "|".join(sorted(set(deferred.reason.dropna()))) if len(deferred) else "",
            "entries_blocked": bool(len(sel) and not len(first) and len(deferred)),
            "new_puts_filled": new_cycles,
            "names_held_before_next_expiry": int(last.n_active_names.iloc[0]) if len(last) else None,
        })
    out = pd.DataFrame(rows).rename(columns={"order": "put_orders"})
    return out


def main():
    p = load_params()
    ctx = Context(p)
    out = ROOT / p["base_dir"] / "report"
    summ = []
    for L in (1.0, 5.0):
        f = funnel(ctx, p, L)
        f.to_csv(out / f"selection_funnel_L{L:g}.csv", index=False)
        num = f.select_dtypes("number")
        summ.append({"leverage": L, "months": len(f), **{f"avg_{c}": round(num[c].mean(), 2) for c in num.columns}})
        print(f"L{L:g}\n" + f.to_string(index=False), flush=True)
    pd.DataFrame(summ).to_csv(out / "selection_funnel_summary.csv", index=False)


if __name__ == "__main__":
    main()
