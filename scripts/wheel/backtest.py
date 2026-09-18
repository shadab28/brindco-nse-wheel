#!/usr/bin/env python3
"""Leveraged wheel backtest: top-N ranked NIFTY 50 names, short put -> assignment -> covered call -> call-away.

    python scripts/wheel/backtest.py cache        # pull prices, chains, futures, lots from Postgres into parquet
    python scripts/wheel/backtest.py detect-ca    # corporate actions from F&O strike adjustments
    python scripts/wheel/backtest.py dividends    # cash dividends from NSE announcements (after fetch_nse_ca.py)
    python scripts/wheel/backtest.py run          # all pre-declared runs + benchmarks + analysis + report

Every parameter lives in params.yaml (`backtest:` section). The run grid below was fixed before any result existed:
    ranked_L{1,2,3,4,5}    ranked universe at each leverage in `leverage_grid`
    (ranked_L5_fin0 retired: Step 9a forbids borrowing, so a 0%-financing variant is bit-identical to ranked_L5)
Outputs: data/backtest/runs/<run_id>/ and data/backtest/report/.
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
warnings.simplefilter("ignore", FutureWarning)

from nse.wheel import benchmarks as B  # noqa: E402
from nse.wheel import corporate_actions as CA  # noqa: E402
from nse.wheel import dividends as DIV  # noqa: E402
from nse.wheel.data import build_cache, load_market  # noqa: E402
from nse.wheel.metrics import daily_rf, drawdown, monthly_returns, nav_stats, regimes  # noqa: E402
from nse.wheel.runner import Context, load_params, run  # noqa: E402
from nse.wheel.selection import RankedSelection  # noqa: E402


def cmd_cache(p):
    ranks = pd.read_csv(ROOT / p["rankings_file"])
    syms = set(ranks.symbol) | {"TATAMOTORS", "SAMMAANCAP", "HDFC"} | set(p["frozen_baseline_names"])
    build_cache(sorted(syms), p.get("cache_start", "2019-10-01"), p.get("cache_end", "2026-07-31"),
                ROOT / p["base_dir"] / "cache", ROOT / p["lot_daily_file"])


def cmd_detect(p):
    md = load_market(ROOT / p["base_dir"] / "cache", ROOT / p["expiry_file"], ROOT / p["terminations_file"])
    ca = CA.detect(md)
    ca.to_csv(ROOT / p["base_dir"] / "corporate_actions_detected.csv", index=False)
    print(f"{len(ca)} candidate events, {int(ca.accepted.sum())} accepted")


def cmd_dividends(p):
    md = load_market(ROOT / p["base_dir"] / "cache", ROOT / p["expiry_file"], ROOT / p["terminations_file"])
    ann = DIV.parse_raw()
    ann.to_csv(ROOT / p["ca_announcements_file"], index=False)
    div = DIV.build_dividends(ann, md.close)
    div.to_csv(ROOT / p["dividends_file"], index=False)
    ok = div[div.traded_on_ex_date & (div.dividend_per_share > 0)]
    print(f"{len(ann)} announcements, {len(div)} dividend ex-dates, {len(ok)} payable "
          f"(status not OK: {int((div.status != 'OK').sum())})")


def run_grid(ctx):
    p = ctx.params
    grid = {f"ranked_L{L:g}": dict(leverage=L) for L in p["leverage_grid"]}
    # ranked_L5_fin0 retired with Step 9a: negative cash can no longer occur, so varying the financing rate
    # produced output bit-identical to ranked_L5. The pre-rule run is kept in data/backtest/_archive_pre_step9a/.
    out = {}
    for rid, kw in grid.items():
        r = run(ctx, run_id=rid, **kw)
        m = r["metrics"]
        print(f"{rid:16s} final {m['final_nav']:>14,.0f}  CAGR {m['cagr']:7.2%}  MDD {m['max_drawdown']:7.2%}  "
              f"Sharpe {m['sharpe']:5.2f}  margin calls {m['n_margin_call_liquidations']:3d}  "
              f"recon {m['reconciliation_nav_vs_trades_inr']:.4f}", flush=True)
        out[rid] = r
    return out


# ------------------------------------------------------------------------------ analysis helpers
def ew_label(p) -> str:
    """Equal-weight benchmark name, from params.yaml n_positions."""
    return f"Equal-weight top-{p['n_positions']}"


def pct(x, d=2):
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.{d}f}%"


def inr(x):
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"₹{x / 1e5:,.2f} L"


def md_table(df: pd.DataFrame, index=True) -> str:
    return df.to_markdown(index=index)


def exposure_by(trades: pd.DataFrame, key: str, end) -> pd.Series:
    """Capital-days of unleveraged requirement per group (strike × qty for puts, entry value for stock)."""
    t = trades[trades.instrument.isin(["PE", "STOCK"])].copy()
    t["days"] = (t.exit_date.fillna(end) - t.date).dt.days.clip(lower=1)
    return (t.margin_requirement * t.days).groupby(t[key]).sum()


def stress_table(runs: dict, bench: dict, windows: dict) -> pd.DataFrame:
    rows = []
    for w, (a, b) in windows.items():
        a, b = pd.Timestamp(a), pd.Timestamp(b)
        for name, nav in [*((k, r["daily"].set_index("date").nav) for k, r in runs.items()), *bench.items()]:
            s = nav[(nav.index >= a - pd.Timedelta(days=7)) & (nav.index <= b)]
            s = s[s.index >= s.index[s.index <= a].max()] if (s.index <= a).any() else s
            row = {"window": w, "series": name, "return": s.iloc[-1] / s.iloc[0] - 1,
                   "max_dd_in_window": (s / s.cummax() - 1).min()}
            if name in runs:
                d = runs[name]["daily"].set_index("date").loc[a:b]
                ev = runs[name]["events"]
                row["max_margin_util"] = d.margin_utilization.replace(np.inf, np.nan).max()
                row["peak_gross_exposure_x_nav"] = (d.notional_exposure / d.nav).replace(np.inf, np.nan).max()
                row["forced_liquidations"] = int(((ev.event == "margin_call_liquidation") & ev.date.between(a, b)).sum()) if len(ev) else 0
                t = runs[name]["trades"]
                liq = t[t.status.str.startswith("margin") & t.exit_date.between(a, b)]
                row["liquidation_pnl"] = liq.total_pnl.sum()
            rows.append(row)
    return pd.DataFrame(rows)


def regime_table(navs: dict, reg: pd.Series, risk_free=None) -> pd.DataFrame:
    rows = []
    for name, nav in navs.items():
        r = nav.pct_change().dropna()
        ex = r - daily_rf(r.index, risk_free)
        g = reg.reindex(r.index).fillna("sideways")
        for k in ["bull", "sideways", "bear"]:
            x, xe = r[g == k], ex[g == k]
            rows.append({"series": name, "regime": k, "days": len(x), "ann_return": x.mean() * 252,
                         "ann_vol": x.std() * np.sqrt(252), "sharpe": xe.mean() / xe.std() * np.sqrt(252) if xe.std() > 0 else np.nan,
                         "cumulative": (1 + x).prod() - 1, "worst_day": x.min()})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------ charts
def charts(out: Path, navs: dict, runs: dict, p: dict):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"ranked_L1": "#1f77b4", "ranked_L2": "#17becf", "ranked_L3": "#9467bd", "ranked_L4": "#8c564b",
              "ranked_L5": "#d62728",
              "NIFTY 50 TRI B&H": "#2ca02c", ew_label(p): "#ff7f0e"}
    fig, ax = plt.subplots(figsize=(12, 6))
    for k, s in navs.items():
        ax.plot(s.index, s / 1e7, label=k, color=colors.get(k), lw=1.6 if k in ("ranked_L1", f"ranked_L{p['leverage']:g}") else 1.0)
    ax.set_yscale("log"); ax.set_ylabel("NAV, ₹ crore (log)"); ax.set_title("Equity curves — ₹2 crore start")
    ax.grid(alpha=.3); ax.legend(fontsize=8, ncol=3)
    fig.tight_layout(); fig.savefig(out / "equity_curve.png", dpi=130); plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 5))
    for k, s in navs.items():
        ax.plot(s.index, drawdown(s) * 100, label=k, color=colors.get(k), lw=1.2)
    ax.set_ylabel("drawdown, %"); ax.set_title("Drawdown"); ax.grid(alpha=.3); ax.legend(fontsize=8, ncol=3)
    fig.tight_layout(); fig.savefig(out / "drawdown_curve.png", dpi=130); plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    for k in ("ranked_L1", "ranked_L3", "ranked_L5"):
        if k in runs:
            d = runs[k]["daily"].set_index("date")
            axes[0].plot(d.index, d.margin_utilization.clip(upper=2) * 100, label=k, color=colors[k], lw=1)
            axes[1].plot(d.index, d.notional_exposure / d.nav.where(d.nav > 0), label=k, color=colors[k], lw=1)
    axes[0].axhline(100, color="k", ls="--", lw=.8); axes[0].set_ylabel("margin utilisation, % (capped 200)")
    axes[1].set_ylabel("gross notional / NAV (×)"); axes[0].legend(fontsize=8); axes[0].grid(alpha=.3); axes[1].grid(alpha=.3)
    fig.tight_layout(); fig.savefig(out / "margin_utilization.png", dpi=130); plt.close(fig)


# ------------------------------------------------------------------------------ run + report
def cmd_run(p):
    ctx = Context(p)
    base = ROOT / p["base_dir"]
    rep = base / "report"
    rep.mkdir(parents=True, exist_ok=True)
    runs = run_grid(ctx)

    first_fill = runs["ranked_L1"]["daily"].date.iloc[1]
    start_day = runs["ranked_L1"]["daily"].date.iloc[0]
    cap = p["initial_capital"]
    sel = RankedSelection(ctx.rankings, p["n_positions"], p["rank_threshold"])
    entry_days = [start_day] + [d for d in sorted(ctx.md.expiry_dates) if start_day < d <= pd.Timestamp(p["end_date"])]
    nifty = B.nifty_buy_and_hold(ROOT / p["nifty_index_file"], start_day, p["end_date"], cap,
                                  dates=runs["ranked_L1"]["daily"].date)
    ew = B.equal_weight(ctx.md, ctx.costs, ctx.ca, sel, entry_days, cap, p["n_positions"], p["end_date"], ctx.risk_free)
    if start_day not in ew.index:
        ew = pd.concat([pd.Series({start_day: cap}), ew])
    bench = {"NIFTY 50 TRI B&H": nifty, ew_label(p): ew}
    navs = {k: r["daily"].set_index("date").nav for k, r in runs.items()} | bench

    # ---- comparison table
    comp = []
    for k, s in navs.items():
        st = nav_stats(s, ctx.risk_free)
        row = {"series": k, "final_nav": s.iloc[-1], "total_pnl": s.iloc[-1] - cap, "cagr": st["cagr"],
               "ann_vol": st["annualized_vol"], "sharpe": st["sharpe"], "sortino": st["sortino"],
               "max_drawdown": st["max_drawdown"], "calmar": st["calmar"]}
        if k in runs:
            m = runs[k]["metrics"]
            row.update({"avg_margin_util": m["avg_margin_utilization"], "max_margin_util": m["max_margin_utilization"],
                        "avg_gross_exposure_x_nav": m["avg_gross_exposure_x_nav"],
                        "return_on_deployed_capital": m["return_on_deployed_capital"],
                        "return_on_leveraged_capital": m["return_on_leveraged_capital"],
                        "margin_call_liquidations": m["n_margin_call_liquidations"], "financing_cost": m["financing_cost"],
                        "cash_interest_income": m["cash_interest_income"]})
        comp.append(row)
    comp = pd.DataFrame(comp)
    comp.to_csv(rep / "performance_comparison.csv", index=False)

    # ---- monthly returns
    mr = pd.DataFrame({k: monthly_returns(s) for k, s in navs.items()})
    mr.index = mr.index.strftime("%Y-%m")
    mr.to_csv(rep / "monthly_returns.csv")
    eq = pd.DataFrame(navs); eq.index.name = "date"; eq.to_csv(rep / "equity_curves.csv")
    dd = pd.DataFrame({k: drawdown(s) for k, s in navs.items()}); dd.index.name = "date"; dd.to_csv(rep / "drawdown_curves.csv")

    # ---- risk analysis (primary run = params.yaml leverage, contrast ranked_L1)
    primary = f"ranked_L{p['leverage']:g}"
    idx = pd.read_csv(ROOT / p["nifty_price_index_file"], parse_dates=["trade_date"]).set_index("trade_date").close
    reg = regimes(idx)
    regime = regime_table(navs, reg, ctx.risk_free)
    regime.to_csv(rep / "regime_performance.csv", index=False)
    stress = stress_table(runs, bench, p["stress_windows"])
    stress.to_csv(rep / "stress_windows.csv", index=False)
    risk = {}
    for k in ("ranked_L1", primary):
        r = runs[k]
        end = r["daily"].date.iloc[-1]
        t = pd.concat([r["trades"], r["open"]], ignore_index=True)
        cyc = r["cycles"]
        by_stock = t.groupby("symbol").agg(total_pnl=("total_pnl", "sum"), premium=("premium_inr", "sum"),
                                           option_pnl=("option_pnl", "sum"), stock_pnl=("stock_pnl", "sum"),
                                           costs=("transaction_costs", "sum"))
        by_stock["cycles"] = cyc.groupby("symbol").size()
        by_stock["assignments"] = cyc.groupby("symbol").assigned.sum()
        by_stock["assignment_rate"] = by_stock.assignments / by_stock.cycles
        exp = exposure_by(t, "symbol", end)
        by_stock["capital_days_share"] = exp / exp.sum()
        by_stock["pnl_share_of_total"] = by_stock.total_pnl / by_stock.total_pnl.sum()
        by_stock = by_stock.sort_values("total_pnl")
        by_stock.to_csv(rep / f"concentration_by_stock_{k}.csv")
        by_sector = t.groupby("sector").agg(total_pnl=("total_pnl", "sum"), premium=("premium_inr", "sum"))
        exp_s = exposure_by(t, "sector", end)
        by_sector["capital_days_share"] = exp_s / exp_s.sum()
        by_sector["cycles"] = cyc.groupby("sector").size()
        by_sector["assignment_rate"] = cyc.groupby("sector").assigned.mean()
        by_sector = by_sector.sort_values("capital_days_share", ascending=False)
        by_sector.to_csv(rep / f"concentration_by_sector_{k}.csv")
        assigned = cyc[cyc.assigned]
        yearly = cyc.assign(year=cyc.start.dt.year).groupby("year").agg(
            cycles=("cycle_id", "size"), assignment_rate=("assigned", "mean"), called_away=("called_away", "sum"),
            pnl=("total_pnl", "sum"))
        yearly.to_csv(rep / f"assignment_frequency_by_year_{k}.csv")
        gap = assigned[["cycle_id", "symbol", "assign_date", "put_strike", "assign_fsp", "assignment_gap_pct",
                        "drawdown_while_held_pct", "status", "duration_days", "total_pnl"]].sort_values("assignment_gap_pct")
        gap.to_csv(rep / f"assignment_gap_risk_{k}.csv", index=False)
        nav_at = r["daily"].set_index("date").nav
        cyc = cyc.assign(nav_at_start=cyc.start.map(nav_at), pnl_pct_nav=lambda c: c.total_pnl / c.start.map(nav_at))
        large = cyc.sort_values("total_pnl").head(15)[["cycle_id", "symbol", "sector", "start", "end", "status", "assigned",
                                                       "n_calls", "premium", "option_pnl_t", "stock_pnl_t", "costs_t",
                                                       "total_pnl", "pnl_pct_nav", "assignment_gap_pct",
                                                       "drawdown_while_held_pct"]]
        large.to_csv(rep / f"large_loss_cycles_{k}.csv", index=False)
        risk[k] = {"by_stock": by_stock, "by_sector": by_sector, "yearly": yearly, "gap": gap, "large": large, "cycles": cyc}

    charts(rep, navs, runs, p)
    write_report(rep, p, runs, bench, comp, mr, regime, stress, risk)
    print(f"report -> {rep}")


def write_report(rep, p, runs, bench, comp, mr, regime, stress, risk):
    primary = f"ranked_L{p['leverage']:g}"
    keys = [("total_pnl", inr), ("final_nav", inr), ("total_return", pct), ("cagr", pct), ("annualized_return", pct),
            ("annualized_vol", pct), ("sharpe", lambda x: f"{x:.2f}"), ("sortino", lambda x: f"{x:.2f}"),
            ("max_drawdown", pct), ("max_dd_peak", str), ("max_dd_trough", str), ("max_dd_recovered", str),
            ("calmar", lambda x: f"{x:.2f}"),
            ("option_trade_win_rate", pct), ("cycle_win_rate", pct), ("avg_trade_pnl", inr), ("avg_option_trade_pnl", inr),
            ("avg_monthly_return", pct), ("best_month", pct), ("best_month_date", str), ("worst_month", pct),
            ("worst_month_date", str), ("pct_positive_months", pct), ("profit_factor", lambda x: f"{x:.2f}"),
            ("premium_collected", inr), ("option_pnl", inr), ("stock_pnl", inr), ("transaction_costs", inr),
            ("financing_cost", inr), ("cash_interest_income", inr), ("n_puts_sold", str), ("n_calls_sold", str), ("n_assignments", str),
            ("assignment_rate", pct), ("n_calls_exercised", str), ("n_completed_wheel_cycles", str),
            ("n_cycles_closed", str), ("avg_completed_cycle_days", lambda x: f"{x:.0f}"),
            ("avg_closed_cycle_days", lambda x: f"{x:.0f}"),
            ("avg_capital_utilization", pct), ("avg_margin_utilization", pct), ("max_margin_utilization", pct),
            ("max_margin_utilization_date", str), ("n_margin_call_liquidations", str), ("margin_breach_days", str),
            ("avg_leveraged_requirement", inr), ("avg_unleveraged_requirement", inr), ("avg_notional_exposure", inr),
            ("avg_gross_exposure_x_nav", lambda x: f"{x:.2f}×"), ("max_gross_exposure_x_nav", lambda x: f"{x:.2f}×"),
            ("max_borrowed", inr), ("days_borrowing", str),
            # Step 9a — cash-only assignment
            ("n_itm_puts_physically_assigned", str), ("n_itm_puts_not_delivered_insufficient_cash", str),
            ("n_forced_realisations", str), ("forced_realisation_loss", inr), ("assignment_costs", inr),
            ("cash_rejected_delivery_prevented", inr), ("n_cash_settlement_forced_stock_sales", str),
            ("forced_stock_sale_value", inr), ("n_cash_settlement_forced_call_buybacks", str),
            ("min_free_cash", inr), ("min_free_cash_date", str), ("max_cash_utilization", pct),
            ("return_on_deployed_capital", pct), ("return_on_leveraged_capital", pct), ("return_on_avg_nav", pct),
            ("annual_premium_turnover_x_nav", pct), ("annual_notional_sold_x_nav", lambda x: f"{x:.2f}×"),
            ("liquidation_cost_estimate_at_end", inr), ("n_open_positions_at_end", str)]
    order = ["ranked_L1", "ranked_L2", "ranked_L3", "ranked_L4", "ranked_L5"]
    mt = pd.DataFrame({rid: {k: f(runs[rid]["metrics"][k]) if runs[rid]["metrics"][k] is not None else "n/a"
                             for k, f in keys} for rid in order})
    mt.to_csv(rep / "portfolio_metrics.csv")

    def fmt_comp(df):
        d = df.copy()
        for c in ["final_nav", "total_pnl", "financing_cost", "cash_interest_income"]:
            d[c] = d[c].map(lambda x: inr(x) if pd.notna(x) else "")
        for c in ["cagr", "ann_vol", "max_drawdown", "avg_margin_util", "max_margin_util", "return_on_deployed_capital",
                  "return_on_leveraged_capital"]:
            d[c] = d[c].map(lambda x: pct(x) if pd.notna(x) else "")
        for c in ["sharpe", "sortino", "calmar", "avg_gross_exposure_x_nav"]:
            d[c] = d[c].map(lambda x: f"{x:.2f}" if pd.notna(x) else "")
        d["margin_call_liquidations"] = d["margin_call_liquidations"].map(lambda x: f"{x:.0f}" if pd.notna(x) else "")
        return d

    c = fmt_comp(comp).set_index("series")
    lines = [
        f"# Leveraged Wheel Backtest — Top-{p['n_positions']} Ranked NIFTY 50", "",
        f"Window {runs['ranked_L1']['metrics']['start']} → {runs['ranked_L1']['metrics']['end']} · capital ₹2 crore · "
        f"first signal {p['first_signal_date']} · slippage `{p['slippage_level']}` · margin financing "
        f"{p['margin_financing_rate']:.0%} p.a. · idle cash earns the monthly India 3M T-bill rate "
        f"(Sharpe/Sortino in excess of it) · all parameters in `params.yaml` and fixed before results.", "",
        "## 6. Performance comparison", "",
        "### Absolute P&L and drawdown", md_table(c[["final_nav", "total_pnl", "cagr", "max_drawdown"]]), "",
        "### Risk-adjusted", md_table(c[["ann_vol", "sharpe", "sortino", "calmar"]]), "",
        "### Capital efficiency and leverage impact",
        md_table(c.loc[[i for i in c.index if i in runs], ["avg_gross_exposure_x_nav", "avg_margin_util", "max_margin_util",
                  "return_on_deployed_capital", "return_on_leveraged_capital", "margin_call_liquidations", "financing_cost",
                  "cash_interest_income"]]), "",
        "![equity](equity_curve.png)", "", "![drawdown](drawdown_curve.png)", "", "![margin](margin_utilization.png)", "",
        "## Portfolio-level metrics (all runs)", "", md_table(mt), "",
        "## 5. Monthly returns", "", md_table(mr.map(lambda x: pct(x, 1) if pd.notna(x) else "")), "",
        "## 7. Risk analysis", "",
        "### Stress windows (named ex ante in params.yaml)", "",
        md_table(stress.assign(**{c_: stress[c_].map(lambda x: pct(x) if pd.notna(x) else "") for c_ in ["return", "max_dd_in_window", "max_margin_util"]},
                               liquidation_pnl=stress.liquidation_pnl.map(lambda x: inr(x) if pd.notna(x) else ""),
                               peak_gross_exposure_x_nav=stress.peak_gross_exposure_x_nav.map(lambda x: f"{x:.2f}" if pd.notna(x) else ""),
                               forced_liquidations=stress.forced_liquidations.map(lambda x: f"{x:.0f}" if pd.notna(x) else "")), index=False), "",
        "### Regimes (NIFTY above/below 200-day SMA with 6-month return beyond ±5%)", "",
        md_table(regime.assign(**{c_: regime[c_].map(pct) for c_ in ["ann_return", "ann_vol", "cumulative", "worst_day"]},
                               sharpe=regime.sharpe.map(lambda x: f"{x:.2f}")), index=False), "",
    ]
    # ---------------------------------------------------------------- Step 9a: cash-only assignment
    s9 = pd.DataFrame({rid: {
        "ITM puts physically assigned": runs[rid]["metrics"]["n_itm_puts_physically_assigned"],
        "ITM puts not delivered (cash)": runs[rid]["metrics"]["n_itm_puts_not_delivered_insufficient_cash"],
        "delivery rate": pct(runs[rid]["metrics"]["n_itm_puts_physically_assigned"]
                             / max(runs[rid]["metrics"]["n_itm_puts_physically_assigned"]
                                   + runs[rid]["metrics"]["n_itm_puts_not_delivered_insufficient_cash"], 1)),
        "realised assignment losses": inr(runs[rid]["metrics"]["forced_realisation_loss"]),
        "assignment costs": inr(runs[rid]["metrics"]["assignment_costs"]),
        "cash rejected / delivery prevented": inr(runs[rid]["metrics"]["cash_rejected_delivery_prevented"]),
        "max cash utilisation": pct(runs[rid]["metrics"]["max_cash_utilization"]),
        "min free cash": inr(runs[rid]["metrics"]["min_free_cash"]),
        "forced stock sales": runs[rid]["metrics"]["n_cash_settlement_forced_stock_sales"],
        "forced call buybacks": runs[rid]["metrics"]["n_cash_settlement_forced_call_buybacks"],
    } for rid in order})
    tot_sales = sum(runs[r]["metrics"]["n_cash_settlement_forced_stock_sales"] for r in order)
    tot_buybacks = sum(runs[r]["metrics"]["n_cash_settlement_forced_call_buybacks"] for r in order)
    lines += [
        "## 8. Cash-only assignment (Step 9a)", "",
        "The book never borrows and cash never goes negative. An ITM put is physically assigned only if free "
        "cash covers `strike × qty` plus assignment costs; otherwise it is closed out for its intrinsic value "
        "and the net loss is realised immediately, with no shares received. `cash >= 0` is asserted in the "
        "daily reconciliation, so a negative balance fails the run rather than being clamped.", "",
        md_table(s9), "",
        "### Two auditable consequences of the no-borrow constraint", "",
        "Neither is a defect: both follow from the rule, and both are counted above rather than hidden.", "",
        f"1. **Forced-sale cascade** — funding a close-out can force an *unrelated* wheel to sell its stock "
        f"and terminate its cycle early. Observed {tot_sales} time(s) across all runs"
        f"{' — rare in practice' if tot_sales <= 5 else ''}.",
        f"2. **Forced call buyback** — if the only stock available to sell is covered by a written call, that "
        f"call must be bought back first (never leaving a naked call), which can mean closing an otherwise "
        f"profitable position. Observed {tot_buybacks} time(s) across all runs"
        f"{' — the path is implemented and unit-tested but was never exercised by this data' if tot_buybacks == 0 else ''}.",
        "",
        "### Leverage caveat — the L grid is not like-for-like", "",
        "Delivery costs the full notional in cash while target notional per name is `L × NAV / N`, so the "
        "binding ratio is `L / N`. Leverage therefore buys put notional at the cost of deliveries, and the "
        "grid varies two things at once: leverage *and* how often the stock leg is reached. At 1× the gate "
        "never binds (`ranked_L1` is bit-identical to the pre-rule engine). Any run whose `L / N` approaches "
        "1 tends toward zero deliveries, at which point the strategy is naked put selling rather than a "
        "wheel. Comparisons across leverage must state this.", "",
    ]

    for k in (primary, "ranked_L1"):
        rk = risk[k]
        lines += [f"### {k}: concentration by sector", "",
                  md_table(rk["by_sector"].assign(total_pnl=rk["by_sector"].total_pnl.map(inr), premium=rk["by_sector"].premium.map(inr),
                                                  capital_days_share=rk["by_sector"].capital_days_share.map(pct),
                                                  assignment_rate=rk["by_sector"].assignment_rate.map(pct))), "",
                  f"### {k}: concentration by stock (worst and best 8 by P&L)", "",
                  md_table(pd.concat([rk["by_stock"].head(8), rk["by_stock"].tail(8)])[["total_pnl", "premium", "cycles", "assignment_rate", "capital_days_share", "pnl_share_of_total"]]
                           .assign(total_pnl=lambda d: d.total_pnl.map(inr), premium=lambda d: d.premium.map(inr),
                                   assignment_rate=lambda d: d.assignment_rate.map(pct), capital_days_share=lambda d: d.capital_days_share.map(pct),
                                   pnl_share_of_total=lambda d: d.pnl_share_of_total.map(pct))), "",
                  f"### {k}: assignment frequency by year", "",
                  md_table(rk["yearly"].assign(assignment_rate=rk["yearly"].assignment_rate.map(pct), pnl=rk["yearly"].pnl.map(inr))), "",
                  f"### {k}: gap risk after assignment", "",
                  f"Assigned cycles: {len(rk['gap'])}. Gap at assignment (FSP / strike − 1): median {pct(rk['gap'].assignment_gap_pct.median())}, "
                  f"5th pct {pct(rk['gap'].assignment_gap_pct.quantile(.05))}, worst {pct(rk['gap'].assignment_gap_pct.min())}. "
                  f"Further fall while holding (min close / FSP − 1): median {pct(rk['gap'].drawdown_while_held_pct.median())}, "
                  f"worst {pct(rk['gap'].drawdown_while_held_pct.min())}.", "",
                  f"### {k}: 15 largest-loss wheel cycles", "",
                  md_table(rk["large"].assign(start=rk["large"].start.dt.date, end=rk["large"].end.dt.date,
                                              **{c_: rk["large"][c_].map(inr) for c_ in ["premium", "option_pnl_t", "stock_pnl_t", "costs_t", "total_pnl"]},
                                              pnl_pct_nav=rk["large"].pnl_pct_nav.map(pct),
                                              assignment_gap_pct=rk["large"].assignment_gap_pct.map(lambda x: pct(x) if pd.notna(x) else ""),
                                              drawdown_while_held_pct=rk["large"].drawdown_while_held_pct.map(lambda x: pct(x) if pd.notna(x) else "")), index=False), ""]
    (rep / "REPORT.md").write_text("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["cache", "detect-ca", "dividends", "run"])
    a = ap.parse_args()
    p = load_params()
    {"cache": cmd_cache, "detect-ca": cmd_detect, "dividends": cmd_dividends, "run": cmd_run}[a.command](p)


if __name__ == "__main__":
    main()
