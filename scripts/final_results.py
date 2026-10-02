#!/usr/bin/env python3
"""Build final_results/: the chosen strategy parameters, the best alternative the grids found, and every
headline number, copied or computed from the current run outputs. Nothing here is typed by hand.

    python scripts/wheel/backtest.py run
    python scripts/wheel/slippage_sensitivity.py
    python scripts/wheel/required_analysis.py
    python scripts/wheel/strike_grid.py
    python scripts/wheel/stop_loss_probe.py rerun     # the stop sweep (pinned to 5% / 5% strikes)
    python scripts/wheel/monte_carlo.py               # bootstrap of the chosen run and every variant
    python scripts/wheel/walk_forward.py              # 900-config out-of-sample test, PBO, Reality Check re-check
    python scripts/final_results.py

Rerun this after any of the above; final_results/ is rebuilt from scratch.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import textwrap
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "final_results"
REP = ROOT / "data/backtest/report"

STRATEGY_KEYS = ["n_positions", "rank_threshold", "put_strike_filter_pct", "call_strike_filter_pct",
                 "slippage_level", "initial_capital"]
BACKTEST_KEYS = ["leverage", "put_stop_loss_pct", "first_signal_date", "max_margin_utilization",
                 "call_floor_economic_basis", "covered_call_first", "call_fill_lag_days", "min_contracts_t",
                 "min_oi_contracts_t", "participation_cap", "max_distance_min_pct", "allocation_priority"]

TABLES = {"portfolio_metrics.csv": REP / "analysis/portfolio_metrics.csv",
          "pnl_decomposition_by_leverage.csv": REP / "analysis/pnl_decomposition.csv",
          "wheel_diagnostics_by_leverage.csv": REP / "analysis/wheel_diagnostics.csv",
          "per_underlying_L3.csv": REP / "analysis/per_underlying_ranked_L3.csv",
          "strike_grid_L3.csv": REP / "strike_grid_L3/grid.csv",
          "slippage_sensitivity.csv": REP / "slippage_sensitivity/summary.csv",
          "stop_loss_sweep.csv": REP / "stop_loss/rerun.csv",
          "stress_pnl_by_symbol_L3.csv": REP / "analysis/stress_pnl_by_symbol.csv",
          "stress_deliveries_L3.csv": REP / "analysis/stress_deliveries.csv",
          "monthly_returns.csv": REP / "monthly_returns.csv",
          "stress_windows.csv": REP / "stress_windows.csv",
          "regime_performance.csv": REP / "regime_performance.csv",
          **{f"monte_carlo_{n}.csv": REP / f"monte_carlo/{n}.csv"
             for n in ["final_number", "summary", "variants", "selection", "reality_check", "deflated_sharpe"]},
          **{f"walk_forward_{n}.csv": REP / f"walk_forward/{n}.csv"
             for n in ["split_shipped", "split_winners", "split_rank_correlation", "split_by_config",
                       "walk_forward_folds", "walk_forward_stitched", "pbo", "reality_check_recheck",
                       "reality_check_sensitivity", "spa"]}}
CHARTS = {"equity_curve.png": REP / "equity_curve.png", "drawdown_curve.png": REP / "drawdown_curve.png",
          "margin_utilization.png": REP / "margin_utilization.png",
          "strike_grid_heatmaps_L3.png": REP / "strike_grid_L3/heatmaps.png",
          "cagr_vs_slippage.png": REP / "slippage_sensitivity/cagr_vs_slippage.png",
          **{f"monte_carlo_{n}.png": REP / f"monte_carlo/{n}.png"
             for n in ["nav_fan", "distributions", "variants_cagr", "reality_check"]},
          **{f"walk_forward_{n}.png": REP / f"walk_forward/{n}.png" for n in ["is_vs_oos", "walk_forward_equity", "pbo"]}}
DOCS = {"ANALYSIS.md": REP / "analysis/ANALYSIS.md", "STOP_LOSS.md": ROOT / "docs/STOP_LOSS.md",
        "MONTE_CARLO.md": REP / "monte_carlo/MONTE_CARLO.md", "WALK_FORWARD.md": REP / "walk_forward/WALK_FORWARD.md"}

# The submission's "results pack": the four items the brief names, in one folder, for the chosen run.
# Sources are keyed by run directory (RUNS / primary) because the primary run follows params.yaml.
RUNS = ROOT / "data/backtest/runs"
PACK_CHARTS = ["equity_curve.png", "drawdown_curve.png"]
PACK_TABLES = ["per_underlying_L3.csv"]
PACK_LOGS = {"trade_log_cycles.csv": "wheel_cycles.csv", "trade_log_fills.csv": "trades.csv"}


def pct(x, d=2):
    return "" if pd.isna(x) else f"{x:.{d}%}"


def lakh(x):
    return "" if pd.isna(x) else f"₹{x / 1e5:,.1f} L"


def crore(x):
    return f"₹{x / 1e7:,.2f} cr"


def main():
    raw = yaml.safe_load(open(ROOT / "params.yaml"))
    p = {**{k: v for k, v in raw.items() if k != "backtest"}, **raw["backtest"]}
    L = p["leverage"]
    primary = f"ranked_L{L:g}"
    pack_logs = [RUNS / primary / src for src in PACK_LOGS.values()]
    missing = [str(v) for v in [*TABLES.values(), *CHARTS.values(), *DOCS.values(), *pack_logs] if not v.exists()]
    if missing:
        sys.exit("missing inputs (run the pipeline first):\n  " + "\n  ".join(missing))

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "tables").mkdir(parents=True)
    (OUT / "charts").mkdir()
    for name, src in TABLES.items():
        shutil.copy2(src, OUT / "tables" / name)
    for name, src in CHARTS.items():
        shutil.copy2(src, OUT / "charts" / name)
    for name, src in DOCS.items():
        shutil.copy2(src, OUT / name)
    for doc, prefix in [("MONTE_CARLO.md", "monte_carlo_"), ("WALK_FORWARD.md", "walk_forward_")]:
        path = OUT / doc                   # its images sit next to it in the report folder; here they are in charts/
        text = path.read_text()
        for name in CHARTS:
            if name.startswith(prefix):
                text = text.replace(f"]({name.removeprefix(prefix)})", f"](charts/{name})")
        path.write_text(text)

    # ------------------------------------------------------------ results pack
    (OUT / "results_pack").mkdir()
    for name in PACK_CHARTS:
        shutil.copy2(OUT / "charts" / name, OUT / "results_pack" / name)
    for name in PACK_TABLES:
        shutil.copy2(OUT / "tables" / name, OUT / "results_pack" / name)
    for name, src in PACK_LOGS.items():
        shutil.copy2(RUNS / primary / src, OUT / "results_pack" / name)
    (OUT / "results_pack" / "README.md").write_text(textwrap.dedent(f"""\
        # Results pack — {primary}

        Generated by `scripts/final_results.py`; every file is a copy of a pipeline output, nothing is
        typed by hand. Parameters: `../chosen_params.yaml`. Full commentary: `../ANALYSIS.md`.

        | File | What it is |
        |---|---|
        | `equity_curve.png` | NAV of every leverage against the NIFTY 50 TRI and the equal-weight benchmarks |
        | `drawdown_curve.png` | Drawdown from running peak, same series |
        | `per_underlying_L3.csv` | Per-name summary for the chosen run: P&L, CAGR, vol, Sharpe, drawdown, cycles, assignment and call-away rates, premium capture |
        | `trade_log_cycles.csv` | **The trade log.** One row per wheel cycle: entry, puts and calls sold, expiry outcome, assignment (`assigned`, `assign_date`, `assign_fsp`), delivery and call-away share counts, stop-outs, costs, P&L split into option and stock legs |
        | `trade_log_fills.csv` | One row per fill underneath those cycles: instrument, expiry, strike, lots, entry and exit quotes, premium, costs, margin, exit reason |

        Cycle and fill rows join on `cycle_id`.
        """))

    chosen = {"strategy": {k: p[k] for k in STRATEGY_KEYS}, "backtest": {k: p[k] for k in BACKTEST_KEYS}}
    with open(OUT / "chosen_params.yaml", "w") as f:
        f.write(f"# Snapshot of params.yaml on {dt.date.today()} — the parameters behind every number in this folder.\n")
        yaml.safe_dump(chosen, f, sort_keys=False, allow_unicode=True)

    # ---------------------------------------------------------------- inputs
    pm = pd.read_csv(TABLES["portfolio_metrics.csv"], index_col=0)
    dec = pd.read_csv(TABLES["pnl_decomposition_by_leverage.csv"], index_col=0)
    diag = pd.read_csv(TABLES["wheel_diagnostics_by_leverage.csv"], index_col=0)
    grid = pd.read_csv(TABLES["strike_grid_L3.csv"])
    slip = pd.read_csv(TABLES["slippage_sensitivity.csv"])
    stop = pd.read_csv(TABLES["stop_loss_sweep.csv"])
    m = json.load(open(ROOT / "data/backtest/runs" / primary / "metrics.json"))
    mc = pd.read_csv(TABLES["monte_carlo_final_number.csv"]).iloc[0]
    mrc = pd.read_csv(TABLES["monte_carlo_reality_check.csv"]).iloc[0]
    msel = pd.read_csv(TABLES["monte_carlo_selection.csv"])
    assert abs(mc.backtest_cagr - m["cagr"]) < 1e-9, "monte_carlo outputs are stale: rerun scripts/wheel/monte_carlo.py"
    wfs = pd.read_csv(TABLES["walk_forward_split_shipped.csv"]).iloc[0]
    wfw = pd.read_csv(TABLES["walk_forward_split_winners.csv"])
    wfr = pd.read_csv(TABLES["walk_forward_split_rank_correlation.csv"]).set_index(["pool", "metric"]).spearman_is_vs_oos
    wff = pd.read_csv(TABLES["walk_forward_walk_forward_folds.csv"])
    wst = pd.read_csv(TABLES["walk_forward_walk_forward_stitched.csv"], index_col=0)
    wpbo = pd.read_csv(TABLES["walk_forward_pbo.csv"]).set_index("pool")
    wrc = pd.read_csv(TABLES["walk_forward_reality_check_recheck.csv"]).iloc[0]
    wspa = pd.read_csv(TABLES["walk_forward_spa.csv"])
    wbc = pd.read_csv(TABLES["walk_forward_split_by_config.csv"], index_col=0)
    wf_ship_id = f"L{L:g}_p{p['put_strike_filter_pct']:g}_c{p['call_strike_filter_pct']:g}_s{p['put_stop_loss_pct']:g}"
    assert abs(wbc.at[wf_ship_id, "is_cagr"] - wfs.is_cagr) < 1e-12, "walk_forward outputs are for other params: rerun it"
    assert abs(wrc.reproduced_p_chosen - mrc.p_value_chosen) < 1e-12, "walk_forward Reality Check differs from monte_carlo"
    dec["nav_pnl"] = dec.total_pnl + dec.cash_interest
    for c in ["put_otm", "call_otm"]:
        grid[c] = grid[c].round(4)

    pp, cc = round(p["put_strike_filter_pct"], 4), round(p["call_strike_filter_pct"], 4)
    ch = grid[(grid.put_otm == pp) & (grid.call_otm == cc)].iloc[0]
    assert abs(ch.cagr - pm.at[primary, "cagr"]) < 1e-9, "strike grid is stale: its base cell differs from the headline run"
    best = grid.loc[grid.calmar.idxmax()]
    row_calmar = grid.groupby("put_otm").calmar.mean()
    best_row = row_calmar.idxmax()
    rowg = grid[grid.put_otm == best_row]
    top_cagr = grid.loc[grid.cagr.idxmax()]
    bench = [i for i in pm.index if not i.startswith("ranked_")]

    # ---------------------------------------------------------------- README
    W = []
    w = W.append
    w("# Brindco NIFTY 50 wheel — final results\n")
    w(f"Built {dt.date.today()} by `scripts/final_results.py` from the current run outputs. Window "
      f"{m['start']} → {m['end']}, ₹{p['initial_capital']:,.0f} start, identical for every series.\n")
    w("## 1. Chosen parameters and the best alternative found\n")
    w("| Parameter | **Chosen (params.yaml)** | Best alternative in the L3 strike grid |\n|---|---|---|")
    w(f"| Leverage | **{L:g}×** | {L:g}× |")
    w(f"| Put strike | **≤ spot × (1 − {pp:.0%})** | ≤ spot × (1 − {best.put_otm:.0%}) |")
    w(f"| Call strike | **≥ spot × (1 + {cc:.0%})**, never below cost basis | ≥ spot × (1 + {best.call_otm:.0%}) |")
    w(f"| Put stop-loss | **{p['put_stop_loss_pct']:.0%} below spot at sale** | same |")
    w(f"| Names / rank threshold | **top {p['n_positions']}, rank_final > {p['rank_threshold']}** | same |")
    w(f"| Slippage assumption | **{p['slippage_level']}** | same |")
    w(f"| CAGR | **{pct(ch.cagr)}** | {pct(best.cagr)} |")
    w(f"| Max drawdown | **{pct(ch.max_drawdown)}** | {pct(best.max_drawdown)} |")
    w(f"| Sharpe | **{ch.sharpe:.2f}** | {best.sharpe:.2f} |")
    w(f"| Calmar | **{ch.calmar:.2f}** | {best.calmar:.2f} |")
    w(f"| NAV P&L | **{crore(ch.total_pnl)}** | {crore(best.total_pnl)} |\n")
    w(f"- **Chosen** is what `params.yaml` runs and what every other table here reports.")
    w(f"- **Best alternative** is the highest-Calmar cell of the 36-run put × call grid at {L:g}×. "
      f"The put {best_row:.0%} row is also the most stable row (highest average Calmar). Across its six call offsets it has "
      f"CAGR {pct(rowg.cagr.min())}–{pct(rowg.cagr.max())}, max drawdown "
      f"{pct(rowg.max_drawdown.min())} to {pct(rowg.max_drawdown.max())}, and Calmar "
      f"{rowg.calmar.min():.2f}–{rowg.calmar.max():.2f}.")
    w(f"- **The trade-off:** the chosen setting earns {(ch.cagr - best.cagr) * 100:.2f} more CAGR points for "
      f"{(best.max_drawdown - ch.max_drawdown) * 100:.2f} more points of drawdown. "
      f"The chosen strikes were picked from the grid *before* the stop-loss existed, when they were the best cell. "
      f"With the stop on, the put {best_row:.0%} row dominates on risk.")
    alt = wbc.loc[f"L{L:g}_p{best.put_otm:g}_c{best.call_otm:g}_s{p['put_stop_loss_pct']:g}"]
    w(f"- **The case against switching:** the stop-loss (15%) and this grid were both fitted on the same window, so "
      "re-optimising again compounds the in-sample selection.")
    w(f"- **What the out-of-sample test says (section 8):** fitted on 2020–2023 and run from flat on 2024-01 → "
      f"2026-06, the chosen setting made {pct(wfs.oos_cagr)} CAGR (Sharpe {wfs.oos_sharpe:.2f}, max DD "
      f"{pct(wfs.oos_max_drawdown)}); the put {best.put_otm:.0%} / call {best.call_otm:.0%} alternative made "
      f"{pct(alt.oos_cagr)} (Sharpe {alt.oos_sharpe:.2f}, max DD {pct(alt.oos_max_drawdown)}).\n")

    w("## 2. Headline performance vs benchmarks\n")
    t = pm.loc[[f"ranked_L{x:g}" for x in p["leverage_grid"]] + bench,
               ["final_nav", "cagr", "annualized_vol", "sharpe", "sortino", "max_drawdown", "max_dd_peak",
                "max_dd_trough", "max_dd_recovered", "calmar"]].copy()
    t.index = [f"**{i} (chosen)**" if i == primary else i for i in t.index]
    for c in ["cagr", "annualized_vol", "max_drawdown"]:
        t[c] = t[c].map(pct)
    t["final_nav"] = t.final_nav.map(lambda v: f"₹{v:,.0f}")
    for c in ["sharpe", "sortino", "calmar"]:
        t[c] = t[c].map(lambda v: f"{v:.2f}")
    t["max_dd_recovered"] = t.max_dd_recovered.fillna("not recovered")
    w(t.to_markdown() + "\n")
    w("Sharpe and Sortino are in excess of the India 3-month T-bill. All series use the same window and capital.\n")

    w(f"## 3. Why {L:g}× — premium sold against what it costs\n")
    lv = dec.loc[[f"ranked_L{x:g}" for x in p["leverage_grid"]]]
    tab = pd.DataFrame({"premium kept": lv.premium_income.map(lakh),
                        "delivery-leg P&L": lv.delivery_leg_price_pnl.map(lakh),
                        "share of premium given back": (-lv.delivery_leg_price_pnl / lv.premium_income).map(lambda v: pct(v, 0)),
                        "ITM puts cash-settled": diag.loc[lv.index, "itm_cash_settled"].astype(int),
                        "NAV P&L": lv.nav_pnl.map(lakh), "CAGR": pm.loc[lv.index, "cagr"].map(pct),
                        "max DD": pm.loc[lv.index, "max_drawdown"].map(pct)})
    w(tab.to_markdown() + "\n")
    mg = lv[["premium_income", "delivery_leg_price_pnl", "nav_pnl"]].diff().iloc[1:]
    mg.index = [f"{a[8:]} → {b[8:]}" for a, b in zip(lv.index[:-1], lv.index[1:])]
    mt = pd.DataFrame({"extra premium kept": mg.premium_income.map(lakh),
                       "extra delivery-leg loss": mg.delivery_leg_price_pnl.map(lakh),
                       "loss per ₹1 of extra premium": (-mg.delivery_leg_price_pnl / mg.premium_income).map(lambda v: f"₹{v:.2f}"),
                       "change in NAV P&L": mg.nav_pnl.map(lakh)})
    w(mt.to_markdown() + "\n")
    last_pos = [i for i, v in mg.nav_pnl.items() if v > 0][-1]
    w(f"- **For {L:g}×:** {last_pos} is the last step where the extra premium more than pays for the extra "
      "stock losses. Beyond it, each extra rupee of premium costs more than a rupee on the delivery leg, and the "
      "cash-only rule leaves more ITM puts unfunded.")
    l2 = "ranked_L2"
    w(f"- **Against:** 2× has the better Sharpe ({pm.at[l2, 'sharpe']:.2f} vs {pm.at[primary, 'sharpe']:.2f}) and "
      f"Calmar ({pm.at[l2, 'calmar']:.2f} vs {pm.at[primary, 'calmar']:.2f}), and its drawdown recovered "
      f"({pm.at[l2, 'max_dd_recovered']}). {L:g}×'s drawdown had not recovered by the end of the window. "
      f"{L:g}× was chosen after the L1–L5 grid was seen.\n")

    w(f"## 4. Strike selection at {L:g}× (put × call offsets 2–7%, stop-loss on)\n")
    for col, title, f in [("cagr", "CAGR", pct), ("max_drawdown", "Max drawdown", pct),
                          ("calmar", "Calmar", lambda v: f"{v:.2f}")]:
        pv = grid.pivot(index="put_otm", columns="call_otm", values=col)
        pv.index = [f"put {x:.0%}" for x in pv.index]
        pv.columns = [f"call {x:.0%}" for x in pv.columns]
        w(f"**{title}**\n\n" + pv.map(f).to_markdown() + "\n")
    rm = grid.groupby("put_otm")[["cagr", "max_drawdown", "calmar"]].mean()
    rm.index = [f"put {x:.0%}" for x in rm.index]
    w("**Average over call offsets, by put offset**\n\n" + pd.DataFrame(
        {"CAGR": rm.cagr.map(pct), "max DD": rm.max_drawdown.map(pct),
         "Calmar": rm.calmar.map(lambda v: f"{v:.2f}")}).to_markdown() + "\n")
    w(f"- **The put offset is what controls risk.** The highest CAGR is put {top_cagr.put_otm:.0%} / call "
      f"{top_cagr.call_otm:.0%} ({pct(top_cagr.cagr)}), but it comes with a {pct(top_cagr.max_drawdown)} drawdown.")
    w(f"- **The call offset matters much less.** Within a row the drawdown moves by a few points, with no consistent direction.\n")

    w(f"## 5. Slippage and stop-loss sensitivity\n")
    sg = slip.pivot(index="slippage_level", columns="leverage", values="cagr").loc[list(dict.fromkeys(slip.slippage_level))]
    sg.columns = [f"L{c:g}" for c in sg.columns]
    w("**CAGR by slippage level** (per option leg: zero / 1 tick or 1.25% / 2 ticks or 2.5% (base) / 4 ticks or 5% / "
      "8 ticks or 10% of premium; stock sales 0 / 2.5 / 5 / 10 / 20 bps)\n\n" + sg.map(pct).to_markdown() + "\n")
    st = stop[stop.leverage == L][["stop", "cagr", "max_drawdown", "sharpe", "stops", "assignments", "cash_settled", "total_pnl"]].copy()
    st["cagr"], st["max_drawdown"] = st.cagr.map(pct), st.max_drawdown.map(pct)
    st["sharpe"], st["total_pnl"] = st.sharpe.map(lambda v: f"{v:.2f}"), st.total_pnl.map(crore)
    w(f"**Put stop-loss sweep at {L:g}×** (run at 5% / 5% strikes, before the strike change; see `STOP_LOSS.md`)\n\n"
      + st.to_markdown(index=False) + "\n")

    w(f"## 6. P&L decomposition and wheel diagnostics — chosen run\n")
    r = dec.loc[primary]
    w("| Premium sold | Premium kept | Delivery-leg P&L | Dividends | Costs | Cash interest | **NAV P&L** |\n|---|---|---|---|---|---|---|")
    w(f"| {lakh(r.premium_sold)} | {lakh(r.premium_income)} | {lakh(r.delivery_leg_price_pnl)} | {lakh(r.dividends)} | "
      f"−{lakh(r.costs)} | {lakh(r.cash_interest)} | **{lakh(r.nav_pnl)}** |\n")
    g = diag.loc[primary]
    w(f"- **Puts:** {int(g.puts_sold)} sold. {pct(g.assignment_rate, 1)} were delivered, and {int(g.itm_cash_settled)} "
      f"ITM puts were cash-settled because there wasn't enough cash to take delivery.")
    w(f"- **After delivery:** {pct(g.call_away_rate, 1)} of delivered cycles were called away. Stock was held "
      f"{g.avg_days_held_post_delivery:.0f} days on average after delivery.")
    w(f"- **Premium capture:** {pct(r.premium_capture, 1)} of premium sold was kept (wheel convention). Counting the "
      f"intrinsic value paid on ITM options against the premium, the strict figure is {pct(r.premium_capture_strict, 1)}.")
    w(f"- **Reconciliation:** the parts add up to the NAV change to within ₹1. Per-stock detail, the worst-drawdown "
      "walk-through and the full bias discussion are in `ANALYSIS.md`.\n")

    w("## 7. Monte Carlo: the number after the selection haircut\n")
    w(f"10,000 stationary block-bootstrap histories (mean block 21 trading days) of the chosen run, and the same "
      f"histories for all {int(mrc.n_variants)} distinct variants seen before it was chosen (leverage, strike grid, "
      f"stop sweep, threshold). Full method and checks: `MONTE_CARLO.md`.\n")
    w("| | Backtest | **Monte Carlo, after selection haircut** |\n|---|---|---|")
    w(f"| CAGR | {pct(mc.backtest_cagr)} | **{pct(mc.adjusted_cagr)}** (90% range {pct(mc.mc_p05_cagr)} to "
      f"{pct(mc.mc_p95_cagr)}) |")
    w(f"| Sharpe | {mc.backtest_sharpe:.2f} | **{mc.adjusted_sharpe:.2f}** |")
    w(f"| Max drawdown | {pct(mc.backtest_max_drawdown)} | **{pct(mc.mc_p05_max_drawdown)}** on a 1-in-20 path |")
    w(f"| P(CAGR below NIFTY 50 TRI) | | {mc.p_cagr_below_nifty:.0%} |")
    w(f"| Reality Check p-value | | {mc.reality_check_p:.3f} |\n")
    w(f"- **Match:** the simulation's scorer reproduces `metrics.json` exactly, and its median CAGR / Sharpe sit on "
      f"the backtest, which itself lands near the 50th percentile on every metric. The backtest was a typical path, "
      f"not a lucky one.")
    w(f"- **Haircut:** put {pp:.0%} / call {cc:.0%} was the best-CAGR cell of the strike grid. On each simulated "
      f"history the best-CAGR cell of that grid beats its own average by {pct(mc.selection_optimism_cagr)} CAGR, "
      f"so that is taken off. Other pools and criteria give {pct(mc.haircut_range_cagr_low)}–"
      f"{pct(mc.haircut_range_cagr_high)}.")
    w(f"- **For the strategy:** with every variant's edge set to zero, the best of all {int(mrc.n_variants)} reaches "
      f"a Sharpe of {mrc.null_best_sharpe_mean:.2f} on average ({mrc.null_best_sharpe_p95:.2f} at the 95th "
      f"percentile), far below the variants' own. They trade the same names on the same days, so they behave like "
      f"only {mrc.effective_independent_trials:.1f} independent bets. Before the haircut the chosen run beats the "
      f"NIFTY 50 TRI in {1 - mc.p_cagr_below_nifty:.0%} of histories.")
    w(f"- **Against:** at p = {mc.reality_check_p:.3f} the chosen run does not clear the 5% bar once the search is "
      f"counted. The best observed variant ({mrc.observed_best_variant}, Sharpe {mrc.observed_best_sharpe:.2f}) does "
      f"(p = {mrc.p_value_best:.3f}). The bootstrap cannot produce a crash worse than March 2020, so the drawdown "
      "tail is a floor, not a ceiling.\n")

    w("## 8. Out of sample: walk-forward, overfitting probability, Reality Check re-checked\n")
    w("Every parameter that was tuned on the full window (leverage 1–5 × put 2–7% × call 2–7% × stop off/10/12/15/20% "
      "= 900 configs) re-run end to end, then tested on years it was not fitted on. Full method and checks: "
      "`WALK_FORWARD.md`.\n")
    w(f"| Fit 2020–2023, test 2024-01 → 2026-06 (from flat) | In sample | **Out of sample** |\n|---|---|---|")
    w(f"| Chosen params CAGR | {pct(wfs.is_cagr)} | **{pct(wfs.oos_cagr)}** (rank {int(wfs.oos_cagr_rank_of_900)} of 900) |")
    w(f"| Chosen params Sharpe | {wfs.is_sharpe:.2f} | **{wfs.oos_sharpe:.2f}** |")
    w(f"| Chosen params max drawdown | {pct(wfs.is_max_drawdown)} | {pct(wfs.oos_max_drawdown)} |")
    w(f"| NIFTY 50 TRI CAGR | {pct(wfs.nifty_is_cagr)} | {pct(wfs.nifty_oos_cagr)} |")
    w(f"| Average 3M T-bill | | {pct(wfs.avg_tbill_oos)} |\n")
    w("**If the pick had been made on 2020–2023 only**\n")
    w("| pool | picked by | winner | in-sample CAGR | OOS CAGR | OOS Sharpe | OOS max DD |\n|---|---|---|---|---|---|---|")
    for v in wfw.itertuples():
        w(f"| {v.pool} | {v.picked_by} | `{v.winner}` | {pct(v.is_cagr)} | {pct(v.oos_cagr)} | {v.oos_sharpe:.2f} | "
          f"{pct(v.oos_max_drawdown)} |")
    w("")
    w("**Anchored walk-forward, re-fit every January, test years 2022 → 2026 H1 stitched**\n")
    ws_ = wst.copy()
    w(pd.DataFrame({"CAGR": ws_.cagr.map(pct), "Sharpe": ws_.sharpe.map(lambda v: f"{v:.2f}"),
                    "max DD": ws_.max_drawdown.map(pct), "Calmar": ws_.calmar.map(lambda v: f"{v:.2f}")}
                   ).to_markdown() + "\n")
    w("| Overfitting and data-snooping tests | value |\n|---|---|")
    w(f"| Rank correlation, in-sample vs OOS CAGR (all 900 / L{L:g} only) | {wfr[('all 900', 'cagr')]:.2f} / "
      f"{wfr[('L3 only (180)', 'cagr')]:.2f} |")
    w(f"| Rank correlation, in-sample vs OOS Sharpe (all 900 / L{L:g} only) | {wfr[('all 900', 'sharpe')]:.2f} / "
      f"{wfr[('L3 only (180)', 'sharpe')]:.2f} |")
    w(f"| Probability of backtest overfitting, CSCV (all 900 / L{L:g} only) | {wpbo.at['all 900', 'pbo']:.2f} / "
      f"{wpbo.at['L3 only (180)', 'pbo']:.2f} |")
    w(f"| Reality Check p, chosen run (published → reproduced) | {mrc.p_value_chosen:.3f} → {wrc.reproduced_p_chosen:.3f} |")
    w(f"| … over 5 seeds × block lengths 5–63 days | {wrc.sens_p_chosen_min:.3f} – {wrc.sens_p_chosen_max:.3f} |")
    for v in wspa[wspa.pool.str.startswith("58")].itertuples():
        w(f"| Hansen SPA p, best of 58 variants vs {v.benchmark} | {v.spa_c_p:.3f} |")
    w(f"| Chosen params alone, OOS: p(Sharpe ≤ 0) / p(no edge over NIFTY) | {wrc.p_oos_sharpe_le_0_vs_tbill:.2f} / "
      f"{wrc.p_oos_active_le_0_vs_nifty:.2f} |\n")
    wcagr, wl3s = wst.loc["walk-forward, pick by cagr"], wst.loc["L3 walk-forward, pick by sharpe"]
    wship, wnif = wst.loc["shipped params (fixed)"], wst.loc["NIFTY 50 TRI"]
    spa_nifty = wspa[wspa.pool.str.startswith("58") & (wspa.benchmark == "NIFTY 50 TRI")].iloc[0]
    spa_tb = wspa[wspa.pool.str.startswith("58") & (wspa.benchmark == "T-bill")].iloc[0]
    w(f"- **The Reality Check figure stands.** p = {wrc.reproduced_p_chosen:.3f} reproduces exactly from the same "
      f"seed and stays between {wrc.sens_p_chosen_min:.3f} and {wrc.sens_p_chosen_max:.3f} across seeds and block "
      "lengths, never below 0.05. Its null is *no variant beats the T-bill*. Against that bar the best of the "
      f"search does clear it under Hansen's SPA (p = {spa_tb.spa_c_p:.3f}). Against the NIFTY 50 TRI nothing "
      f"does: SPA p = {spa_nifty.spa_c_p:.2f}.")
    w(f"- **The chosen params did not validate.** On 2020–2023 they ranked {int(wfs.is_cagr_rank_of_900)} of 900 "
      f"by CAGR; from 2024 they made {pct(wfs.oos_cagr)}, below the T-bill, and rank {int(wfs.oos_cagr_rank_of_900)} of 900.")
    w(f"- **Selecting by CAGR is the failure.** CAGR ranks reverse out of sample. The CAGR-picked walk-forward made "
      f"{pct(wcagr.cagr)} a year with a {pct(wcagr.max_drawdown)} drawdown. Picking by Sharpe at {L:g}× made "
      f"{pct(wl3s.cagr)}, Sharpe {wl3s.sharpe:.2f}, max DD {pct(wl3s.max_drawdown)}: the far-OTM put row the "
      "strike grid already flagged as the risk-efficient alternative.")
    w(f"- **For the chosen params:** run fixed through the same test years they made {pct(wship.cagr)} a year vs "
      f"NIFTY {pct(wnif.cagr)}. PBO is {wpbo.at['all 900', 'pbo']:.2f}, below the 0.5 coin-flip line, so the "
      "search is not pure noise. And a bull-then-sell-off sequence reverses CAGR ranks on its own: leverage and "
      "tight puts pay in rallies and lose in sell-offs.")
    w(f"- **Against:** that fixed stream used hindsight on those very years, and still has the worse Sharpe "
      f"({wship.sharpe:.2f} vs {wl3s.sharpe:.2f}) and drawdown ({pct(wship.max_drawdown)} vs "
      f"{pct(wl3s.max_drawdown)}). The test window holds one sell-off, so it is short evidence either way. Only data "
      "after 2026-06-30 is truly unseen.\n")

    w("## 9. Verdict and what it rests on\n")
    w(f"- **Deployability:** the {L:g}× / put {pp:.0%} / call {cc:.0%} setting is not validated out of sample. Plan "
      f"on the Monte Carlo figure ({pct(mc.adjusted_cagr)} CAGR, Sharpe {mc.adjusted_sharpe:.2f}) only as an upper "
      f"bound; the one clean out-of-sample stretch earned {pct(wfs.oos_cagr)}. The out-of-sample evidence favours "
      f"the far-OTM put row (put {best.put_otm:.0%}): in the full-window grid it gave up "
      f"{(ch.cagr - best.cagr) * 100:.1f} CAGR points for a drawdown {(best.max_drawdown - ch.max_drawdown) * 100:.1f} "
      f"points smaller, and out of sample it made {pct(alt.oos_cagr)} vs {pct(wfs.oos_cagr)}. That alternative was also found on this data. It should run on paper or at "
      "small size before any capital is committed.")
    w("- **Pre-tax.** Almost all of the return is short-term premium income, which is the most heavily taxed kind.")
    w("- **Unfunded ITM puts are closed at intrinsic value on expiry day.** A broker would square them off earlier, "
      "at worse prices, so this flatters the result, and more so at higher leverage.")
    w("- **Cash interest** assumes idle cash earns the T-bill rate. A broker pays nothing on margin cash.")
    w("- **The margin model is simplified:** strike ÷ leverage, not NSE's SPAN plus exposure margin.")
    w("- **The worst drawdown is essentially one expiry (October 2024).** Staggering puts across expiries is the "
      "highest-value next change.\n")

    w("## Reproduce\n")
    w("```\n" + textwrap.dedent(__doc__.split("\n\n")[1]).strip() + "\n```\n")
    w("## Files\n")
    w("- `chosen_params.yaml`: the parameters in force for every number here.")
    w("- `ANALYSIS.md`: brief section 7 in full (per-stock results, worst-drawdown walk-through, biases).")
    w("- `STOP_LOSS.md`: the put stop-loss rule and why it is set at 15%.")
    w("- `MONTE_CARLO.md`: bootstrap of the chosen run and every variant, Reality Check, selection haircut.")
    w("- `WALK_FORWARD.md`: 900-config out-of-sample split, anchored walk-forward, PBO, Reality Check / SPA re-check.")
    w("- `tables/`: " + ", ".join(f"`{k}`" for k in TABLES))
    w("- `charts/`: " + ", ".join(f"`{k}`" for k in CHARTS))
    w("- `results_pack/`: the submission bundle — equity and drawdown charts, the per-name summary, and the "
      f"cycle and fill trade logs for `{primary}`. See `results_pack/README.md`.")
    (OUT / "README.md").write_text("\n".join(W) + "\n")
    print(f"-> {OUT}  chosen {pp:.0%}/{cc:.0%} CAGR {pct(ch.cagr)} MDD {pct(ch.max_drawdown)} | "
          f"best {best.put_otm:.0%}/{best.call_otm:.0%} CAGR {pct(best.cagr)} MDD {pct(best.max_drawdown)}")


if __name__ == "__main__":
    main()
