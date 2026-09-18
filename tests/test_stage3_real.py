"""Stage 3 — real data: look-ahead guard, reconciliation ladder, settlement, lots, costs, margin, portfolio rules.

Runs the ranked wheel on the full window at 1x and 5x and checks every trade against independent recomputation.
"""
from __future__ import annotations

import pandas as pd
import pytest

from nse.wheel.engine import WheelEngine
from nse.wheel.runner import Context, load_params, run
from nse.wheel.selection import RankedSelection

LEVERAGES = [1.0, 5.0]


@pytest.fixture(scope="module")
def ctx():
    try:
        return Context(load_params())
    except FileNotFoundError:
        pytest.skip("market cache / corporate actions not built")


@pytest.fixture(scope="module")
def runs(ctx):
    return {L: run(ctx, write=False, leverage=L) for L in LEVERAGES}


# ---------------------------------------------------------------- look-ahead
class LookaheadError(AssertionError):
    pass


class GuardedMarket:
    """Proxy that fails if the engine reads any dated market fact later than the day being processed.
    Calendar facts (expiry dates, trading-day list) are published in advance and are allowed."""

    DATED = {"spot", "chain", "quote", "lot_size", "is_fo_listed", "month_end_spot"}

    def __init__(self, md):
        self._md, self.now, self.reads = md, None, 0

    def __getattr__(self, name):
        attr = getattr(self._md, name)
        if name in self.DATED:
            def guarded(date, *a, **k):
                if date is not None and self.now is not None and pd.Timestamp(date) > self.now:
                    raise LookaheadError(f"{name}({pd.Timestamp(date).date()}) read on {self.now.date()}")
                self.reads += 1
                return attr(date, *a, **k)
            return guarded
        if name == "fsp":
            return _GuardedDict(attr, self)
        return attr


class _GuardedDict(dict):
    def __init__(self, d, guard):
        super().__init__()
        self._d, self._g = d, guard

    def get(self, key, default=None):
        if pd.Timestamp(key[0]) > self._g.now:
            raise LookaheadError(f"fsp{key} read on {self._g.now.date()}")
        return self._d.get(key, default)


@pytest.mark.parametrize("L", LEVERAGES)
def test_engine_never_reads_future_data(ctx, L):
    guard = GuardedMarket(ctx.md)
    cfg = {**ctx.params, "leverage": L}
    sel = RankedSelection(ctx.rankings, cfg["n_positions"], cfg["rank_threshold"])

    def guarded_sel(t):
        names = sel(t)
        assert sel.log[-1]["ranking_date"] <= pd.Timestamp(t)
        return names

    eng = WheelEngine(guard, ctx.costs, cfg, guarded_sel, ctx.ca, ctx.sectors)
    days = [d for d in ctx.md.trading_days if pd.Timestamp(cfg["first_signal_date"]) <= d <= pd.Timestamp(cfg["end_date"])]
    for d in days:
        guard.now = d
        eng.step(d)
    assert guard.reads > 500 and len(eng.trades) > (100 if L == 1.0 else 5)


def test_truncated_history_gives_identical_decisions(ctx, runs):
    """Run to a cut date, then compare with the full run: every trade opened by the cut is identical."""
    cut = "2022-06-30"
    part = run(ctx, write=False, leverage=5.0, end=cut)
    full = runs[5.0]
    cols = ["symbol", "instrument", "date", "strike", "quantity", "entry_premium"]
    a = part["trades"][cols].sort_values(cols).reset_index(drop=True)
    b = full["trades"][full["trades"].exit_date <= pd.Timestamp(cut)][cols].sort_values(cols).reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)
    pd.testing.assert_frame_equal(part["daily"], full["daily"][full["daily"].date <= pd.Timestamp(cut)].reset_index(drop=True))


# ---------------------------------------------------------------- reconciliation ladder
@pytest.mark.parametrize("kw", [
    dict(universe="frozen", symbols=["RELIANCE"], n_positions=1, end="2020-06-30"),
    dict(universe="frozen", symbols=["RELIANCE", "SBIN", "INFY"], n_positions=3, end="2020-06-30"),
    dict(end="2020-06-30"),
    dict(),
])
@pytest.mark.parametrize("L", LEVERAGES)
def test_reconciliation_ladder(ctx, kw, L):
    r = run(ctx, write=False, leverage=L, **kw)
    assert abs(r["metrics"]["reconciliation_nav_vs_trades_inr"]) < 1.0
    led = r["ledger"]
    assert abs(r["engine"].initial + led.amount.sum() - r["daily"].cash.iloc[-1]) < 1.0


# ---------------------------------------------------------------- trade-level checks
def option_rows(r):
    t = pd.concat([r["trades"], r["open"]], ignore_index=True)
    return t[t.instrument.isin(["PE", "CE"])]


@pytest.mark.parametrize("L", LEVERAGES)
def test_expiries_contracts_and_lots(ctx, runs, L):
    md = ctx.md
    for x in option_rows(runs[L]).itertuples():
        cm = x.expiry.strftime("%Y-%m")
        assert md.expiry_of[cm] == x.expiry
        assert md.contract_after(x.date) == cm or md.contract_after(md.trading_days[md.day_index[x.date] - 1]) == cm
        assert x.quantity == x.lots * x.lot_size or x.status.startswith("margin") or x.quantity % x.lot_size == 0
        assert md.lot_size(x.date, x.symbol, cm) in (x.lot_size, None) or x.exit_date is not pd.NaT


@pytest.mark.parametrize("L", LEVERAGES)
def test_settlement_mechanics(ctx, runs, L):
    md = ctx.md
    t = runs[L]["trades"]
    exp = t[t.status.isin(["expired_otm", "assigned", "exercised"])]
    assert len(exp) > (100 if L == 1.0 else 5)
    for x in exp.itertuples():
        fsp = md.fsp.get((x.exit_date, x.symbol), md.spot(x.exit_date, x.symbol))
        assert x.exit_date == x.expiry
        assert x.underlying_exit == pytest.approx(fsp)
        intrinsic = max(x.strike - fsp, 0) if x.instrument == "PE" else max(fsp - x.strike, 0)
        assert x.exit_price == pytest.approx(intrinsic)
        itm = fsp < x.strike if x.instrument == "PE" else fsp > x.strike
        assert x.assigned_or_exercised == itm
        assert x.status == ("expired_otm" if not itm else ("assigned" if x.instrument == "PE" else "exercised"))
        assert x.option_pnl == pytest.approx((x.entry_premium - intrinsic) * x.quantity, abs=0.01)


@pytest.mark.parametrize("L", LEVERAGES)
def test_entry_fills_and_costs(ctx, runs, L):
    md, costs = ctx.md, ctx.costs
    ev = runs[L]["events"]
    topups = ev[ev.event == "covered_call_topup"] if len(ev) else ev
    topped = {(r.symbol, r.strike) for r in topups.itertuples()}
    for x in option_rows(runs[L]).itertuples():
        if not isinstance(x.entry_quote, float) or x.status.startswith("terminated"):
            continue
        if x.instrument == "CE" and x.symbol in {sym for sym, _ in topped} and runs[L]["cycles"].set_index("cycle_id").loc[
                x.cycle_id, "n_call_topups"] > 0:
            continue                   # blended entry over several fills: each fill is checked below
        q = md.quote(x.date, x.symbol, x.expiry.strftime("%Y-%m"), x.instrument, x.strike)
        if q is None:          # strike later adjusted by a corporate action
            continue
        assert q.close == pytest.approx(x.entry_quote)
        ref = md.month_end_spot(x.date, x.symbol)
        assert costs.option_fill(x.date, q.close, "sell", ref) == pytest.approx(x.entry_premium, abs=1e-4)
        assert q.contracts > 0 and x.lots <= int(ctx.params["participation_cap"] * q.contracts)


@pytest.mark.parametrize("L", LEVERAGES)
def test_topup_call_fills_use_that_days_quote(ctx, runs, L):
    """Every top-up call fill: same contract and strike as the open call, fill-day quote, participation cap, and the
    open call never covers more shares than are held."""
    md, costs, r = ctx.md, ctx.costs, runs[L]
    ev, led = r["events"], r["ledger"]
    tops = ev[ev.event == "covered_call_topup"] if len(ev) else ev
    calls = option_rows(r).query("instrument == 'CE'")
    for x in tops.itertuples():
        t = pd.Timestamp(x.date)
        call = calls[(calls.symbol == x.symbol) & (calls.date <= t) & (calls.exit_date.isna() | (calls.exit_date >= t))]
        assert len(call) == 1, f"{x.date} {x.symbol}: top-up without its open call"
        c = call.iloc[0]
        q = md.quote(t, x.symbol, pd.Timestamp(c.expiry).strftime("%Y-%m"), "CE", x.strike)
        assert q is not None and q.contracts > 0 and x.lots <= int(ctx.params["participation_cap"] * q.contracts)
        px = costs.option_fill(t, q.close, "sell", md.month_end_spot(t, x.symbol))
        lot = md.lot_size(t, x.symbol, pd.Timestamp(c.expiry).strftime("%Y-%m"))
        prem = led[(led.date == t) & (led.symbol == x.symbol) & (led.kind == "premium")].amount.sum()
        assert prem == pytest.approx(px * x.lots * lot, rel=1e-9)
        assert c.quantity <= c.lot_size * c.lots and c.lots * c.lot_size == c.quantity


@pytest.mark.parametrize("L", LEVERAGES)
def test_margin_fields(runs, L):
    ev = runs[L]["events"]
    ca = ev[ev.event.str.startswith("corporate_action")] if len(ev) else ev
    for x in option_rows(runs[L]).itertuples():
        if x.instrument == "PE" and x.status != "open_mtm":
            # recorded at entry; a corporate action during the trade adjusts strike/qty (e.g. BPCL's ₹58 special
            # dividend cut K by 13%), so only unadjusted trades must match exactly
            adjusted = len(ca) and ((ca.symbol == x.symbol) & (ca.date > x.date) & (ca.date <= x.exit_date)).any()
            if not adjusted:
                assert x.margin_requirement == pytest.approx(x.strike * x.quantity, rel=1e-9)
        assert x.leveraged_capital_requirement == pytest.approx(x.margin_requirement / L, rel=1e-9)
    d = runs[L]["daily"]
    assert (d.leveraged_requirement - d.unleveraged_requirement / L).abs().max() < 1e-3
    assert (d.available_capital - (d.nav - d.leveraged_requirement)).abs().max() < 1e-3


@pytest.mark.parametrize("L", LEVERAGES)
def test_breaches_are_liquidated_next_day(ctx, runs, L):
    d, ev = runs[L]["daily"], runs[L]["events"]
    breaches = d[(d.margin_utilization > ctx.params["max_margin_utilization"] + 1e-9)
                 & (d.leveraged_requirement > 0)].date          # after a wipe-out nothing is left to sell
    liq = set(ev[ev.event == "margin_call_liquidation"].date) if len(ev) else set()
    # covered_call_first holds liquidation back one cycle, once per breach episode, for calls on uncovered shares
    calls_first = set(ev[ev.event == "margin_breach_calls_first"].date) if len(ev) else set()
    for b in breaches:
        if b in calls_first:
            continue
        assert ctx.md.next_day(b) in liq, f"breach on {b.date()} not liquidated next day"
    if L == 1.0:
        assert len(breaches) == 0


# ---------------------------------------------------------------- portfolio rules
@pytest.mark.parametrize("L", LEVERAGES)
def test_no_overlapping_positions_and_no_naked_calls(runs, L):
    t = pd.concat([runs[L]["trades"], runs[L]["open"]], ignore_index=True)
    end = runs[L]["daily"].date.iloc[-1]
    t["exit"] = t.exit_date.fillna(end)
    for sym, g in t.groupby("symbol"):
        opts = g[g.instrument.isin(["PE", "CE"])].sort_values("date")
        assert (opts.date.iloc[1:].values >= opts.exit.iloc[:-1].values).all(), f"{sym}: overlapping options"
        stock = g[g.instrument == "STOCK"]
        for c in opts[opts.instrument == "CE"].itertuples():
            held = stock[(stock.cycle_id == c.cycle_id)]
            assert held.quantity.sum() >= c.quantity, f"{sym}: call {c.quantity} > shares {held.quantity.sum()}"
            assert (held.date <= c.date).all() and (held.exit >= c.exit).all(), f"{sym}: call outside stock holding"
        for p in opts[opts.instrument == "PE"].itertuples():
            overlap = stock[(stock.date < p.exit) & (stock.exit > p.date)]
            assert overlap.empty, f"{sym}: put written while holding stock"


@pytest.mark.parametrize("L", LEVERAGES)
def test_at_most_n_names_and_puts_only_on_selected(ctx, runs, L):
    r = runs[L]
    d = r["daily"]
    assert d.n_active_names.max() <= ctx.params["n_positions"]
    sel = r["selection_log"]
    sel = sel.assign(names=sel.selected.str.split(","))
    decided = {row.decision_date: set(row.names) for row in sel.itertuples()}
    md = ctx.md
    for p in r["cycles"].itertuples():
        prior = [dd for dd in decided if dd < p.start]
        assert prior and p.symbol in decided[max(prior)], f"put on {p.symbol} {p.start.date()} was not selected"


@pytest.mark.parametrize("L", LEVERAGES)
def test_wheel_state_machine(runs, L):
    t = pd.concat([runs[L]["trades"], runs[L]["open"]], ignore_index=True)
    for cid, g in t.groupby("cycle_id"):
        g = g.sort_values(["date", "instrument"])
        puts = g[g.instrument == "PE"]
        assert len(puts) == 1, f"cycle {cid}: {len(puts)} puts"
        calls = g[g.instrument == "CE"]
        if len(calls):
            assert puts.status.iloc[0] in ("assigned", "terminated_assigned")
            assert (calls.date > puts.exit_date.iloc[0]).all()
    cyc = runs[L]["cycles"]
    done = cyc[cyc.status == "completed"]
    assert (done.assigned & done.called_away).all()
