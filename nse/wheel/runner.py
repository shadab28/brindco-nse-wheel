"""Wire data, corporate actions, selection and the engine into one run, and write its tables."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from nse.wheel import corporate_actions as CA
from nse.wheel import dividends as DIV
from nse.wheel.costs import default_cost_model
from nse.wheel.data import load_market
from nse.wheel.engine import InvariantError, WheelEngine
from nse.wheel.metrics import strategy_metrics
from nse.wheel.selection import FrozenSelection, MembershipUniverse, RankedSelection

ROOT = Path(__file__).resolve().parents[2]


def load_params(path: Path = ROOT / "params.yaml") -> dict:
    p = yaml.safe_load(open(path))
    return {**{k: v for k, v in p.items() if k != "backtest"}, **p["backtest"]}


class Context:
    """Everything shared by all runs: market data, costs, corporate actions, rankings, sectors."""

    def __init__(self, params: dict):
        self.params = params
        base = ROOT / params["base_dir"]
        self.md = load_market(base / "cache", ROOT / params["expiry_file"], ROOT / params["terminations_file"])
        self.costs = default_cost_model(params)
        det = pd.read_csv(base / "corporate_actions_detected.csv", parse_dates=["ex_date"])
        ca = CA.load(det, ROOT / "data/calendar/corporate_action_overrides.csv")
        div_file = ROOT / params["dividends_file"]
        if not div_file.exists():
            raise FileNotFoundError(f"{div_file} missing: run scripts/wheel/backtest.py dividends (holding leg needs dividends)")
        div = pd.read_csv(div_file, parse_dates=["ex_date"])
        self.ca = DIV.merge(ca, div, self.md.close)
        self.rankings = pd.read_csv(ROOT / params["rankings_file"])
        sec = pd.read_csv(ROOT / params["sector_file"])
        self.sectors = dict(zip(sec.symbol, sec.sector))
        mf = params.get("universe_membership_file")
        self.membership = pd.read_csv(ROOT / mf) if mf else None
        self.risk_free = load_risk_free(ROOT / params["risk_free_file"]) if params.get("risk_free_file") else None


def load_risk_free(path: Path) -> pd.Series:
    """Annual risk-free rate as a decimal, indexed by 'YYYY-MM'."""
    rf = pd.read_csv(path, dtype={"month": str})
    return rf.set_index("month").rate_pct / 100.0


def run(ctx: Context, run_id: str | None = None, universe: str = "ranked", symbols: list[str] | None = None,
        end: str | None = None, write: bool = True, **overrides) -> dict:
    cfg = {**ctx.params, **overrides}
    if universe == "ranked":
        sel = RankedSelection(ctx.rankings, cfg["n_positions"], cfg["rank_threshold"], cfg["allocation_priority"])
        if symbols is not None:
            inner = sel
            sel = type("Restricted", (), {"log": inner.log, "__call__": lambda self, t: [s for s in inner(t) if s in symbols]})()
    else:
        sel = FrozenSelection(symbols or cfg["frozen_baseline_names"])
    # the ranked universe is historical NIFTY 50 membership; the frozen baseline list has no index rule
    universe = MembershipUniverse(ctx.membership) if universe == "ranked" and ctx.membership is not None else None
    eng = WheelEngine(ctx.md, ctx.costs, cfg, sel, ctx.ca, ctx.sectors, ctx.risk_free, universe=universe)
    end = pd.Timestamp(end or cfg["end_date"])
    eng.run(cfg["first_signal_date"], end)
    last = eng.daily[-1]["date"]

    daily = pd.DataFrame(eng.daily)
    trades = pd.DataFrame(eng.trades)
    open_rows = pd.DataFrame(eng.open_positions_rows(last))
    cycles = pd.DataFrame([asdict(c) for c in eng.cycles.values()])
    if len(cycles):
        cyc_pnl = pd.concat([trades, open_rows]).groupby("cycle_id").agg(
            option_pnl_t=("option_pnl", "sum"), stock_pnl_t=("stock_pnl", "sum"),
            costs_t=("transaction_costs", "sum"), total_pnl=("total_pnl", "sum"))
        cycles = cycles.merge(cyc_pnl, left_on="cycle_id", right_index=True, how="left")
        cycles["sector"] = cycles.symbol.map(ctx.sectors)
        cycles["duration_days"] = (cycles.end.fillna(last) - cycles.start).dt.days
        cycles["assignment_gap_pct"] = np.where(cycles.assigned, cycles.assign_fsp / cycles.put_strike - 1, np.nan)
        cycles["drawdown_while_held_pct"] = np.where(cycles.assigned, cycles.min_spot_while_held / cycles.assign_fsp - 1,
                                                     np.nan)
    ledger = pd.DataFrame(eng.ledger, columns=["date", "symbol", "kind", "amount"])
    events = pd.DataFrame(eng.events)
    audit = pd.DataFrame(eng.audit)
    metrics = strategy_metrics(daily, trades, cycles, open_rows, ledger, dict(eng.diag), eng.initial, eng.L,
                               ctx.risk_free)
    metrics["liquidation_cost_estimate_at_end"] = eng.liquidation_cost_estimate(last)
    metrics["n_open_positions_at_end"] = len(open_rows)
    metrics["diagnostics"] = {k: (float(v) if isinstance(v, float) else int(v)) for k, v in eng.diag.items()}

    # final share audit: every completed cycle must be flat; a run with orphaned shares does not pass silently
    share_audit = eng.share_audit(last)
    orphans = share_audit[share_audit.status == "ORPHAN"] if len(share_audit) else share_audit
    metrics["leftover_shares"] = {
        "orphan_rows": int(len(orphans)), "orphan_shares": int(orphans.shares_held_at_end.sum()) if len(orphans) else 0,
        "orphan_symbols": sorted(orphans.symbol.unique().tolist()) if len(orphans) else [],
        "completed_cycles_checked": int((share_audit.status == "FLAT").sum()) if len(share_audit) else 0,
        "open_at_end_positions": int((share_audit.status == "OPEN_AT_END").sum()) if len(share_audit) else 0}
    if len(orphans):
        raise InvariantError(f"{run_id}: leftover shares outside the strategy:\n{orphans.to_string(index=False)}")

    # reconciliation 2: NAV change == trade-level P&L − financing + cash interest (independent of the cash ledger)
    trade_pnl = trades.total_pnl.sum() + (open_rows.total_pnl.sum() if len(open_rows) else 0.0)
    metrics["reconciliation_nav_vs_trades_inr"] = (daily.nav.iloc[-1] - eng.initial) - (
        trade_pnl - metrics["financing_cost"] + metrics["cash_interest_income"])

    res = {"share_audit": share_audit, "universe_exits": pd.DataFrame(eng.universe_exits), "cfg": cfg, "engine": eng, "daily": daily, "trades": trades, "open": open_rows, "cycles": cycles,
           "ledger": ledger, "events": events, "audit": audit, "metrics": metrics, "selection_log": pd.DataFrame(sel.log)}
    if write and run_id:
        out = ROOT / cfg["base_dir"] / "runs" / run_id
        out.mkdir(parents=True, exist_ok=True)
        pd.concat([trades, open_rows], ignore_index=True).to_csv(out / "trades.csv", index=False)
        cycles.to_csv(out / "wheel_cycles.csv", index=False)
        share_audit.to_csv(out / "share_audit.csv", index=False)
        daily.to_csv(out / "daily.csv", index=False)
        pd.DataFrame(eng.symbol_marks, columns=["date", "symbol", "mark_value"]).to_csv(out / "symbol_marks.csv", index=False)
        ledger.to_csv(out / "cash_ledger.csv", index=False)
        events.to_csv(out / "events.csv", index=False)
        audit.to_csv(out / "liquidity_audit.csv", index=False)
        pd.DataFrame(eng.universe_exits).to_csv(out / "universe_exit_audit.csv", index=False)
        pd.DataFrame(eng.decision_log).to_csv(out / "decision_cycle_log.csv", index=False)
        res["selection_log"].to_csv(out / "selection_log.csv", index=False)
        json.dump({k: v for k, v in metrics.items()}, open(out / "metrics.json", "w"), indent=2, default=str)
        json.dump({k: v for k, v in cfg.items() if k != "stress_windows"} | {"stress_windows": cfg["stress_windows"]},
                  open(out / "config.json", "w"), indent=2, default=str)
    return res
