#!/usr/bin/env python3
"""Brief section 7: portfolio and per-underlying metrics, wheel diagnostics, P&L decomposition, benchmarks,
worst-drawdown walk-through data and strike/slippage sensitivity.

    python scripts/wheel/backtest.py run                 # first: writes data/backtest/runs/ranked_L*/
    python scripts/wheel/slippage_sensitivity.py         # first: slippage grid read here
    python scripts/wheel/required_analysis.py

Outputs: data/backtest/report/analysis/*.csv and ANALYSIS.md. ANALYSIS.md is regenerated above the
HAND_WRITTEN marker; the stress narrative and bias discussion below the marker are kept verbatim.

Conventions (also printed in ANALYSIS.md):
  premium income    premium sold − cost of option buy-backs
  delivery-leg P&L  everything the underlying's price did to the book once a put finished ITM: intrinsic paid on
                    ITM puts/calls at expiry (delivered or cash-settled), plus the stock's mark-to-market while held,
                    excluding dividends
  dividends         cash dividends + cash corporate-action distributions on held shares
  premium capture   premium income / premium sold (a delivered put keeps its premium; the strike paid is a stock entry)
  per-underlying    a sleeve of NAV(t−1) / n_positions: daily return = that name's P&L change / sleeve capital
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nse.wheel import benchmarks as B  # noqa: E402
from nse.wheel.metrics import nav_stats  # noqa: E402
from nse.wheel.runner import Context, load_params, run  # noqa: E402
from nse.wheel.selection import RankedSelection  # noqa: E402

PRIMARY = "ranked_L3"            # params.yaml strategy (leverage 3.0)
# (put offset, call offset): put strike <= spot × (1 − p), call strike >= spot × (1 + c).
# BASE is params.yaml; the other two are the pre-declared alternatives (one tighter, one wider).
STRIKE_BASE = (0.04, 0.03)
STRIKE_GRID = [(0.02, 0.02), STRIKE_BASE, (0.06, 0.05)]
MARKER = "<!-- HAND_WRITTEN: everything below this line is kept when the script reruns -->"
STAT_COLS = ["cagr", "annualized_vol", "sharpe", "sortino", "max_drawdown", "max_dd_peak", "max_dd_trough",
             "max_dd_recovered", "calmar"]


def load_run(base: Path, rid: str) -> dict:
    d = base / "runs" / rid
    return {"daily": pd.read_csv(d / "daily.csv", parse_dates=["date"]),
            "trades": pd.read_csv(d / "trades.csv", parse_dates=["date", "exit_date"]),
            "cycles": pd.read_csv(d / "wheel_cycles.csv", parse_dates=["start", "end", "assign_date"]),
            "ledger": pd.read_csv(d / "cash_ledger.csv", parse_dates=["date"]),
            "events": pd.read_csv(d / "events.csv", parse_dates=["date"]),
            "marks": pd.read_csv(d / "symbol_marks.csv", parse_dates=["date"]),
            "metrics": json.load(open(d / "metrics.json"))}


def stats_row(nav: pd.Series, rf) -> dict:
    st = nav_stats(nav, rf)
    return {k: st[k] for k in STAT_COLS}


# ------------------------------------------------------------------------------ wheel diagnostics
def decomposition(trades: pd.DataFrame, ledger: pd.DataFrame, by: str | None = None) -> pd.DataFrame:
    """P&L split per the module conventions; `by` = None for the portfolio, "symbol" per underlying."""
    key = [by] if by else []
    opt = trades[trades.instrument.isin(["PE", "CE"])]
    g = lambda df, col: (df.groupby(key)[col].sum() if key else pd.Series({"portfolio": df[col].sum()}))
    lg = lambda kinds: (ledger[ledger.kind.isin(kinds)].groupby(key).amount.sum() if key
                        else pd.Series({"portfolio": ledger[ledger.kind.isin(kinds)].amount.sum()}))
    out = pd.DataFrame({"premium_sold": g(opt, "premium_inr"), "option_pnl": g(opt, "option_pnl"),
                        "stock_pnl": g(trades, "stock_pnl"), "costs": g(trades, "transaction_costs"),
                        "buybacks": -lg(["option_buyback"]), "dividends": lg(["dividend", "ca_distribution"])}).fillna(0.0)
    out["premium_income"] = out.premium_sold - out.buybacks
    out["delivery_leg_price_pnl"] = out.stock_pnl + (out.option_pnl - out.premium_income) - out.dividends
    out["total_pnl"] = out.premium_income + out.delivery_leg_price_pnl + out.dividends - out.costs
    out["premium_capture"] = out.premium_income / out.premium_sold
    out["premium_capture_strict"] = out.option_pnl / out.premium_sold   # also charges ITM intrinsic to the option
    return out


def diagnostics(cycles: pd.DataFrame, last: pd.Timestamp, by: str | None = None) -> pd.DataFrame:
    c = cycles[cycles.n_puts > 0].copy()
    c["cash_settled"] = c.cash_settled.astype(bool)
    c["assigned"] = c.assigned.astype(bool)
    c["called_away"] = c.called_away.astype(bool)
    c["days_held"] = np.where(c.assigned, (c.end.fillna(last) - c.assign_date).dt.days, np.nan)
    grp = c.groupby(by) if by else c.assign(_all="portfolio").groupby("_all")
    d = grp.agg(csp_cycles=("cycle_id", "count"), puts_sold=("n_puts", "sum"), calls_sold=("n_calls", "sum"),
                delivered=("assigned", "sum"), itm_cash_settled=("cash_settled", "sum"),
                called_away=("called_away", "sum"), avg_days_held_post_delivery=("days_held", "mean"))
    d["assignment_rate"] = d.delivered / d.puts_sold
    d["itm_put_rate"] = (d.delivered + d.itm_cash_settled) / d.puts_sold
    d["call_away_rate"] = d.called_away / d.delivered.replace(0, np.nan)
    return d


def per_underlying_stats(r: dict, n: int, rf) -> pd.DataFrame:
    """Each name as a sleeve of NAV(t−1)/n: cumulative P&L = its cash flows + its end-of-day mark."""
    nav = r["daily"].set_index("date").nav
    led = r["ledger"][r["ledger"].symbol.notna() & (r["ledger"].kind != "cash_interest")]
    flows = led.pivot_table(index="date", columns="symbol", values="amount", aggfunc="sum")
    marks = r["marks"].pivot_table(index="date", columns="symbol", values="mark_value", aggfunc="sum")
    cols = sorted(set(flows.columns) | set(marks.columns))
    flows = flows.reindex(index=nav.index, columns=cols).fillna(0.0).cumsum()
    marks = marks.reindex(index=nav.index, columns=cols).fillna(0.0)
    pnl = flows + marks
    sleeve = (nav.shift(1) / n).reindex(pnl.index)
    rows = []
    for s in cols:
        ret = (pnl[s].diff() / sleeve).fillna(0.0)
        idx = (1 + ret).cumprod()
        active = int(((marks[s] != 0) | (pnl[s].diff().fillna(0) != 0)).sum())
        rows.append({"symbol": s, "days_active": active, "total_pnl": pnl[s].iloc[-1], **stats_row(idx, rf)})
    return pd.DataFrame(rows).set_index("symbol"), pnl


# ------------------------------------------------------------------------------ stress episode
def stress_episode(r: dict, pnl: pd.DataFrame, bench: dict, rf) -> dict:
    nav = r["daily"].set_index("date").nav
    st = nav_stats(nav, rf)
    peak, trough = pd.Timestamp(st["max_dd_peak"]), pd.Timestamp(st["max_dd_trough"])
    win = lambda df: df[(df.date > peak) & (df.date <= trough)]
    led, ev = win(r["ledger"]), win(r["events"])
    by_sym = (pnl.loc[trough] - pnl.loc[peak]).sort_values()
    marks = r["marks"]
    held = lambda d: marks[marks.date == d].set_index("symbol").mark_value
    cyc = r["cycles"]
    in_win = cyc[(cyc.assign_date > peak) & (cyc.assign_date <= trough)]
    dly = r["daily"].set_index("date")
    return {
        "peak": peak, "trough": trough, "recovered": st["max_dd_recovered"], "depth": st["max_drawdown"],
        "nav_peak": nav[peak], "nav_trough": nav[trough],
        "bench_moves": {k: s.asof(trough) / s.asof(peak) - 1 for k, s in bench.items()},
        "flows_by_kind": led.groupby("kind").amount.sum().sort_values(),
        "events_by_kind": ev.groupby(ev.columns[2] if "kind" not in ev.columns else "kind").size().sort_values(ascending=False),
        "pnl_by_symbol": by_sym, "held_at_peak": held(peak), "held_at_trough": held(trough),
        "deliveries": in_win[["symbol", "assign_date", "put_strike", "assign_fsp", "assignment_gap_pct",
                              "min_spot_while_held", "called_away", "cash_settled", "end"]].sort_values("assign_date"),
        "state": dly.loc[[peak, trough], ["nav", "cash", "stock_value", "option_liability", "margin_utilization",
                                           "n_short_puts", "n_covered_calls", "n_stock_names"]],
        "interest": led[led.kind == "cash_interest"].amount.sum(),
    }


# ------------------------------------------------------------------------------ markdown helpers
def pct(x):
    return "" if pd.isna(x) else f"{x:.2%}"


def inr(x):
    return "" if pd.isna(x) else f"₹{x:,.0f}"


def md_table(df: pd.DataFrame, fmt: dict, index=True) -> str:
    d = df.copy()
    for c, f in fmt.items():
        if c in d.columns:
            d[c] = d[c].map(lambda v, f=f: f(v) if not (isinstance(v, float) and pd.isna(v)) else "")
    return d.to_markdown(index=index)


STAT_FMT = {"cagr": pct, "annualized_vol": pct, "sharpe": lambda v: f"{v:.2f}", "sortino": lambda v: f"{v:.2f}",
            "max_drawdown": pct, "calmar": lambda v: f"{v:.2f}", "max_dd_recovered": lambda v: v or "not recovered",
            "total_pnl": inr, "final_nav": inr}


def main():
    p = load_params()
    base = ROOT / p["base_dir"]
    out = base / "report" / "analysis"
    out.mkdir(parents=True, exist_ok=True)
    rids = [f"ranked_L{L:g}" for L in p["leverage_grid"]]
    runs = {rid: load_run(base, rid) for rid in rids}
    ctx = Context(p)
    rf, cap, n = ctx.risk_free, p["initial_capital"], p["n_positions"]

    # ---- benchmarks: identical window (first NAV date → end) and capital
    d0 = runs[PRIMARY]["daily"].date
    start = d0.iloc[0]
    sel = RankedSelection(ctx.rankings, n, p["rank_threshold"])
    entry = [start] + [d for d in sorted(ctx.md.expiry_dates) if start < d <= pd.Timestamp(p["end_date"])]
    ew = B.equal_weight(ctx.md, ctx.costs, ctx.ca, sel, entry, cap, n, p["end_date"], rf)
    bh = B.equal_weight(ctx.md, ctx.costs, ctx.ca, sel, [start], cap, n, p["end_date"], rf)   # bought once, never rebalanced
    fix = lambda s: pd.concat([pd.Series({start: cap}), s]) if start not in s.index else s
    bench = {"NIFTY 50 TRI buy-and-hold": B.nifty_buy_and_hold(ROOT / p["nifty_index_file"], start, p["end_date"], cap, dates=d0),
             f"Selected universe buy-and-hold ({n} names picked {start.date()}, equal weight, never rebalanced)": fix(bh),
             f"Selected universe, equal weight, rebalanced to the top-{n} each expiry": fix(ew)}
    initial_names = [s for s in sel(start) if s in ctx.md.close.columns][:n]

    # ---- portfolio metrics
    port = pd.DataFrame({k: {"final_nav": s.iloc[-1], **stats_row(s, rf)} for k, s in
                         ({rid: r["daily"].set_index("date").nav for rid, r in runs.items()} | bench).items()}).T
    port.to_csv(out / "portfolio_metrics.csv")

    # ---- wheel diagnostics + decomposition, all runs
    diag = pd.concat({rid: diagnostics(r["cycles"], r["daily"].date.iloc[-1]).iloc[0] for rid, r in runs.items()}, axis=1).T
    dec = pd.concat({rid: decomposition(r["trades"], r["ledger"]).iloc[0] for rid, r in runs.items()}, axis=1).T
    for rid, r in runs.items():
        nav = r["daily"].nav
        check = dec.at[rid, "total_pnl"] + r["metrics"]["cash_interest_income"] - (nav.iloc[-1] - nav.iloc[0])
        assert abs(check) < 1.0, f"{rid}: decomposition misses NAV by {check:,.2f}"
    dec["cash_interest"] = [runs[rid]["metrics"]["cash_interest_income"] for rid in dec.index]
    diag.to_csv(out / "wheel_diagnostics.csv")
    dec.to_csv(out / "pnl_decomposition.csv")

    # ---- per underlying (primary run)
    r5 = runs[PRIMARY]
    pu, pnl = per_underlying_stats(r5, n, rf)
    last = r5["daily"].date.iloc[-1]
    per = (pu.join(diagnostics(r5["cycles"], last, "symbol"))
             .join(decomposition(r5["trades"], r5["ledger"], "symbol")
                   [["premium_sold", "premium_income", "delivery_leg_price_pnl", "dividends", "costs", "premium_capture"]]))
    per = per.sort_values("total_pnl")
    per.to_csv(out / f"per_underlying_{PRIMARY}.csv")

    # ---- stress episode (primary run)
    se = stress_episode(r5, pnl, bench, rf)
    se["pnl_by_symbol"].rename("pnl_peak_to_trough").to_csv(out / "stress_pnl_by_symbol.csv")
    se["flows_by_kind"].rename("amount").to_csv(out / "stress_cash_flows_by_kind.csv")
    se["deliveries"].to_csv(out / "stress_deliveries.csv", index=False)

    # ---- strike sensitivity (base slippage) + slippage grid from slippage_sensitivity.py
    rows = []
    for pp, cc in STRIKE_GRID:
        for L in p["leverage_grid"]:
            if (pp, cc) == STRIKE_BASE:
                nav = runs[f"ranked_L{L:g}"]["daily"].set_index("date").nav
            else:
                rr = run(ctx, run_id=None, write=False, leverage=L, put_strike_filter_pct=pp, call_strike_filter_pct=cc)
                nav = rr["daily"].set_index("date").nav
            st = stats_row(nav, rf)
            rows.append({"put_otm": pp, "call_otm": cc, "leverage": L, "final_nav": nav.iloc[-1], **st})
            print(f"put {pp:.0%} call {cc:.0%}  L{L:g}  CAGR {st['cagr']:7.2%}  MDD {st['max_drawdown']:7.2%}  Sharpe {st['sharpe']:.2f}",
                  flush=True)
    strike = pd.DataFrame(rows)
    strike["offsets"] = [f"put {a:.0%} / call {b:.0%}" for a, b in zip(strike.put_otm, strike.call_otm)]
    strike.to_csv(out / "strike_sensitivity.csv", index=False)
    sgrid = lambda df, col, val: df.pivot(index=col, columns="leverage", values=val).rename(columns=lambda c: f"L{c:g}")
    slip_file = base / "report" / "slippage_sensitivity" / "summary.csv"
    slip = pd.read_csv(slip_file) if slip_file.exists() else None

    # ---- ANALYSIS.md
    L = []
    w = L.append
    w("# Required analysis (brief section 7)\n")
    w(f"Generated by `scripts/wheel/required_analysis.py`. Window {start.date()} → {last.date()}, capital ₹{cap:,.0f} "
      f"for every series. Primary run `{PRIMARY}` (params.yaml). Sharpe and Sortino are in excess of the 3-month "
      "T-bill. Drawdown dates: peak → trough → first close back at the peak.\n")
    w("## 1. Portfolio metrics and benchmarks\n")
    w(md_table(port, STAT_FMT) + "\n")
    w(f"The buy-and-hold universe is the {len(initial_names)} names the ranking selected on {start.date()}: "
      f"{', '.join(initial_names)}. It is bought at the next close with the same costs and slippage as the wheel's stock "
      "sales and is never rebalanced. A name that stops trading is sold at its last close and the cash earns the T-bill.\n")
    w("## 2. Wheel diagnostics (all leverages)\n")
    dfmt = {"assignment_rate": pct, "itm_put_rate": pct, "call_away_rate": pct,
            "avg_days_held_post_delivery": lambda v: f"{v:.0f}"}
    w(md_table(diag, dfmt) + "\n")
    w("- `csp_cycles`: cycles that began with a cash-secured put. `delivered`: ITM puts that took delivery. "
      "`itm_cash_settled`: ITM puts closed at intrinsic because Step 9a's cash-only rule left too little cash to take "
      "delivery. `assignment_rate` = delivered / puts sold. `itm_put_rate` counts both.\n"
      "- `call_away_rate` = cycles whose shares were called away / cycles that took delivery. The rest were sold under "
      "a rule (universe exit, liquidity) or are still held at the end.\n")
    w("## 3. P&L decomposition\n")
    w(md_table(dec[["premium_sold", "buybacks", "premium_income", "delivery_leg_price_pnl", "dividends", "costs",
                    "cash_interest", "total_pnl", "premium_capture", "premium_capture_strict"]],
               {c: inr for c in ["premium_sold", "buybacks", "premium_income", "delivery_leg_price_pnl", "dividends",
                                 "costs", "cash_interest", "total_pnl"]} | {"premium_capture": pct, "premium_capture_strict": pct}) + "\n")
    w("`total_pnl` = premium income + delivery-leg price P&L + dividends − costs. Adding cash interest gives the NAV "
      "change exactly (the script asserts this to ₹1). `premium_capture` = premium income / premium sold. A delivered put "
      "keeps its premium, and the strike it pays is a stock entry. `premium_capture_strict` also charges the ITM "
      "intrinsic at expiry to the option leg, so the delivery-leg loss shows up there instead.\n")
    w(f"## 4. Per underlying — `{PRIMARY}`\n")
    w("Each name is a sleeve of NAV(t−1)/N: its daily P&L change (cash flows plus end-of-day mark) over that capital. "
      "Days a name is not held return 0 and still count toward CAGR and volatility. For names held only a few months, "
      "read `total_pnl` rather than the ratios. Sorted worst to best.\n")
    pcols = ["days_active", "total_pnl", "cagr", "annualized_vol", "sharpe", "sortino", "max_drawdown", "max_dd_peak",
             "max_dd_trough", "max_dd_recovered", "calmar", "csp_cycles", "assignment_rate", "avg_days_held_post_delivery",
             "call_away_rate", "premium_sold", "premium_income", "delivery_leg_price_pnl", "dividends", "costs",
             "premium_capture"]
    w(md_table(per[pcols], STAT_FMT | dfmt | {c: inr for c in ["premium_sold", "premium_income", "delivery_leg_price_pnl",
                                                               "dividends", "costs"]} | {"premium_capture": pct}) + "\n")
    w(f"## 5. Worst drawdown — `{PRIMARY}` facts\n")
    w(f"Peak {se['peak'].date()} ({inr(se['nav_peak'])}) → trough {se['trough'].date()} ({inr(se['nav_trough'])}), "
      f"depth {pct(se['depth'])}, recovered: {se['recovered'] or 'not by the end of the window'}. Over the same dates: "
      + "; ".join(f"{k.split(' (')[0]} {pct(v)}" for k, v in se["bench_moves"].items()) + ".\n")
    w("Book state at the peak and at the trough:\n")
    w(md_table(se["state"], {"nav": inr, "cash": inr, "stock_value": inr, "option_liability": inr,
                             "margin_utilization": pct}) + "\n")
    w("Cash flows inside the episode, by kind:\n")
    w(md_table(se["flows_by_kind"].rename("amount").to_frame(), {"amount": inr}) + "\n")
    w("Rule-driven events inside the episode:\n")
    w(se["events_by_kind"].rename("count").to_frame().to_markdown() + "\n")
    w("P&L peak → trough by underlying (worst 10, best 5):\n")
    s = se["pnl_by_symbol"]
    w(pd.concat([s.head(10), s.tail(5)]).rename("pnl").to_frame().pipe(md_table, {"pnl": inr}) + "\n")
    w("Deliveries inside the episode:\n")
    w(md_table(se["deliveries"].assign(assign_date=lambda d: d.assign_date.dt.date, end=lambda d: d.end.dt.date),
               {"assignment_gap_pct": pct}, index=False) + "\n")
    w("## 6. Sensitivity\n")
    w("### Strike selection (put ≤ spot × (1 − p), call ≥ spot × (1 + c); base = put 4% / call 3%), base slippage — CAGR\n")
    w(md_table(sgrid(strike, "offsets", "cagr"), {c: pct for c in [f"L{l:g}" for l in p["leverage_grid"]]}) + "\n")
    w("Max drawdown:\n")
    w(md_table(sgrid(strike, "offsets", "max_drawdown"), {c: pct for c in [f"L{l:g}" for l in p["leverage_grid"]]}) + "\n")
    w("Sharpe:\n")
    w(md_table(sgrid(strike, "offsets", "sharpe"), {c: (lambda v: f"{v:.2f}") for c in [f"L{l:g}" for l in p["leverage_grid"]]}) + "\n")
    if slip is not None:
        order = list(dict.fromkeys(slip.slippage_level))
        w("### Slippage per leg (`data/costs/slippage_schedule.csv`), base strikes — CAGR\n")
        g = sgrid(slip, "slippage_level", "cagr").loc[order]
        w(md_table(g, {c: pct for c in g.columns}) + "\n")
        g = sgrid(slip, "slippage_level", "max_drawdown").loc[order]
        w("Max drawdown:\n")
        w(md_table(g, {c: pct for c in g.columns}) + "\n")

    body = "\n".join(L)
    f = out / "ANALYSIS.md"
    tail = f.read_text().split(MARKER, 1)[1] if f.exists() and MARKER in f.read_text() else "\n"
    f.write_text(body + "\n" + MARKER + tail)
    print(f"-> {f}")


if __name__ == "__main__":
    main()
