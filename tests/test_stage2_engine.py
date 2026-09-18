"""Stage 2 — engine mechanics on synthetic markets (real MarketData / CostModel code paths)."""
import math

import numpy as np
import pandas as pd
import pytest

from nse.wheel import corporate_actions as CA
from nse.wheel.engine import WheelEngine
from tests import synth


def run(md, costs, params, sel=("AAA",), end_day=None, ca=None, risk_free=None, **over):
    cfg = synth.cfg(params, **over)
    eng = WheelEngine(md, costs, cfg, lambda t: list(sel), corporate_actions=ca, risk_free=risk_free)
    end = md.trading_days[end_day] if end_day is not None else md.trading_days[-1]
    days = [d for d in md.trading_days if d <= end]
    eng._last_day = days[-1]
    for d in days:
        eng.step(d)
    return eng


def trades(eng):
    return pd.DataFrame(eng.trades)


def pnl_reconciles(eng):
    """NAV change == closed trade P&L + open MTM P&L − financing (independent of the cash ledger)."""
    t = eng.daily[-1]["date"]
    closed = sum(x["total_pnl"] for x in eng.trades)
    open_ = sum(x["total_pnl"] for x in eng.open_positions_rows(t))
    fin = -sum(x[3] for x in eng.ledger if x[2] == "financing")
    interest = sum(x[3] for x in eng.ledger if x[2] == "cash_interest")
    return eng.nav() - eng.initial, closed + open_ - fin + interest


FLAT = [1000.0] * 60


# ---------------------------------------------------------------- the cycle
def test_put_expires_otm_keeps_premium(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT})
    eng = run(md, costs, params, end_day=25)
    tr = trades(eng)
    put = tr.iloc[0]
    assert put.instrument == "PE" and put.strike == 950 and put.status == "expired_otm"
    assert put.lots == 10 and put.quantity == 1000 and put.lot_size == 100      # floor(1e6 / 95,000)
    assert put.date == md.trading_days[1]                                        # decided day 0, filled day 1
    assert put.option_pnl == pytest.approx(put.entry_premium * 1000)
    assert put.transaction_costs == pytest.approx(costs.option_sale(put.date, put.entry_premium * 1000).total)
    assert eng.cycles[1].status == "put_expired_otm" and not eng.cycles[1].assigned
    assert eng.diag["assignments"] == 0
    a, b = pnl_reconciles(eng)
    assert a == pytest.approx(b, abs=1.0)


def test_idle_cash_earns_risk_free_daily(tmp_path, costs, params):
    """No names selected: NAV grows only by the month's rate on cash, credited to cash every trading day."""
    md = synth.build(tmp_path, {"AAA": FLAT})
    rf = pd.Series({"2022-01": 0.06, "2022-02": 0.12})
    eng = run(md, costs, params, sel=(), risk_free=rf)
    days = [d for d in md.trading_days]
    credits = [x for x in eng.ledger if x[2] == "cash_interest"]
    assert [c[0] for c in credits] == days[1:]                      # every trading day after the first
    rate = lambda d: rf.get(d.strftime("%Y-%m"), 0.12)              # months past the series reuse the latest rate
    cash = eng.initial
    navs = {r["date"]: r["nav"] for r in eng.daily}
    for prev, d in zip(days, days[1:]):
        cash += cash * rate(prev) * (d - prev).days / 365.0         # previous close's cash, ACT/365
        assert navs[d] == pytest.approx(cash, rel=1e-12)            # NAV includes the day's interest the same day
    assert eng.nav() == pytest.approx(cash, rel=1e-12)
    a, b = pnl_reconciles(eng)
    assert a == pytest.approx(b, abs=1.0)


def test_negative_cash_earns_no_interest(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT})
    eng = WheelEngine(md, costs, synth.cfg(params), lambda t: [], risk_free=pd.Series({"2022-01": 0.06}))
    eng.cash = -1000.0
    eng._cash_interest(md.trading_days[0])
    eng._prev_day = md.trading_days[0]
    eng._cash_interest(md.trading_days[1])
    assert eng.cash == -1000.0 and not [x for x in eng.ledger if x[2] == "cash_interest"]


def full_wheel_path():
    p = [1000.0] * 19 + [900.0] * 6 + list(np.linspace(900, 1100, 14)) + [1100.0] * 21
    return p


def test_full_wheel_put_assign_call_callaway_restart(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": full_wheel_path()})
    eng = run(md, costs, params, end_day=45)
    tr = trades(eng)
    d = md.trading_days
    put, call = tr[tr.instrument == "PE"].iloc[0], tr[tr.instrument == "CE"].iloc[0]
    stock = tr[tr.instrument == "STOCK"].iloc[0]
    # put assigned at expiry (FSP 900 < 950)
    assert put.status == "assigned" and put.exit_price == pytest.approx(50.0) and put.underlying_exit == 900
    assert put.option_pnl == pytest.approx((put.entry_premium - 50.0) * 1000)
    # assigned on expiry day 19 -> call decided at that close, sold next day (day 20), strike >= max(spot*1.05, basis)
    assert call.date == d[20]
    basis = 950 - put.entry_premium + (costs.option_sale(put.date, put.entry_premium * 1000).total
                                       + costs.put_assignment(d[19], 950, 900, 1000).total) / 1000
    assert call.strike >= max(900 * 1.05, basis) and call.strike == 950
    assert call.quantity <= 1000
    # called away at FSP 1100 > 950
    assert call.status == "exercised" and stock.status == "called_away"
    assert stock.stock_pnl == pytest.approx((1100 - 900) * 1000)
    c1 = eng.cycles[1]
    assert c1.status == "completed" and c1.assigned and c1.called_away and c1.n_calls == 1
    # restart: a new cash-secured put is decided on the call-away expiry and filled next day
    assert eng.cycles[2].start == d[40] and eng.cycles[2].n_puts == 1
    # economic identity for the completed cycle: premiums + (K_call − K_put) × qty − costs
    cyc = tr[tr.cycle_id == 1]
    expect = (put.entry_premium + call.entry_premium) * 1000 + (950 - 950) * 1000 - cyc.transaction_costs.sum()
    assert cyc.total_pnl.sum() == pytest.approx(expect, abs=0.01)
    a, b = pnl_reconciles(eng)
    assert a == pytest.approx(b, abs=1.0)


def test_covered_call_sold_day_after_assignment(tmp_path, costs, params):
    p = [1000.0] * 19 + [900.0] * 41
    md = synth.build(tmp_path, {"AAA": p})
    eng = run(md, costs, params, end_day=21)
    d = md.trading_days
    put = trades(eng).iloc[0]
    assert put.status == "assigned" and put.exit_date == d[19]
    call = eng.wheels["AAA"].option
    assert call is not None and call.option_type == "CE" and call.entry_date == d[20]
    # a configurable lag still delays it: 3 trading days after assignment
    eng3 = run(md, costs, params, end_day=25, call_fill_lag_days=3)
    assert eng3.wheels["AAA"].option.entry_date == d[22]


def test_call_otm_keeps_shares_and_writes_next_call(tmp_path, costs, params):
    p = [1000.0] * 19 + [900.0] * 41
    md = synth.build(tmp_path, {"AAA": p})
    eng = run(md, costs, params, end_day=50)
    tr = trades(eng)
    calls = tr[tr.instrument == "CE"]
    assert (calls.status == "expired_otm").any()
    assert eng.wheels["AAA"].shares == 1000
    assert eng.cycles[1].n_calls >= 2                              # rolled into the next monthly call
    assert (tr.instrument == "PE").sum() == 1                      # never a put while holding stock


# ---------------------------------------------------------------- no naked calls
def test_calls_never_exceed_shares_when_lot_grows(tmp_path, costs, params):
    p = [1000.0] * 19 + [900.0] * 41
    md = synth.build(tmp_path, {"AAA": p}, lot_overrides={("AAA", "2022-02"): 300, ("AAA", "2022-03"): 300})
    eng = run(md, costs, params, end_day=30)
    call = trades(eng).query("instrument == 'CE'").iloc[0] if (trades(eng).instrument == "CE").any() else None
    if call is None:
        call = eng.wheels["AAA"].option
        assert call is not None and call.qty == 900 and call.lots == 3
    else:
        assert call.quantity == 900
    assert eng.wheels["AAA"].shares == 1000


def test_uncoverable_shares_are_sold_not_left_naked(tmp_path, costs, params):
    p = [1000.0] * 19 + [900.0] * 41
    md = synth.build(tmp_path, {"AAA": p}, lot_overrides={("AAA", "2022-02"): 5000, ("AAA", "2022-03"): 5000})
    eng = run(md, costs, params, end_day=30)
    tr = trades(eng)
    assert (tr.instrument == "CE").sum() == 0 and eng.wheels["AAA"].option is None
    assert eng.diag["uncoverable_odd_lot_sales"] >= 1
    assert tr[tr.instrument == "STOCK"].status.iloc[0] == "uncoverable_odd_lot"
    assert eng.wheels["AAA"].shares == 0


def test_residual_shares_after_call_away_are_sold_and_cycle_ends_flat(tmp_path, costs, params):
    # the call contract month has a bigger lot, so the call covers only part of the assigned shares
    md = synth.build(tmp_path, {"AAA": full_wheel_path()},
                     lot_overrides={("AAA", "2022-02"): 300, ("AAA", "2022-03"): 300})
    eng = run(md, costs, params, end_day=45)
    tr = trades(eng)
    call = tr[tr.instrument == "CE"].iloc[0]
    assert call.status == "exercised" and call.quantity == 900
    stock = tr[(tr.instrument == "STOCK") & (tr.cycle_id == 1)]
    assert stock.quantity.sum() == 1000                                    # every assigned share left the book
    residual = stock[stock.status == "residual_after_call_away"]
    assert len(residual) == 1 and residual.quantity.iloc[0] == 100 and residual.exit_date.iloc[0] == md.trading_days[40]
    c1 = eng.cycles[1]
    assert c1.end is not None and c1.shares_open == 0 and c1.residual_after_call_away == 100
    assert c1.max_uncovered_at_call == 100 and c1.shares_called_away == 900 and c1.shares_sold == 100
    audit = eng.share_audit(eng.daily[-1]["date"])
    assert (audit.status != "ORPHAN").all() and audit.query("cycle_id == 1").status.iloc[0] == "FLAT"
    a, b = pnl_reconciles(eng)
    assert a == pytest.approx(b, abs=1.0)


def test_call_sized_on_fill_day_lot_after_overnight_revision(tmp_path, costs, params):
    # lot halves between the call decision (assignment close, day 19) and its fill (day 20): cover every share
    md = synth.build(tmp_path, {"AAA": full_wheel_path()})
    d = md.trading_days
    real = md.lot_size
    md.lot_size = lambda t, sym, cm: real(t, sym, cm) // 2 if t >= d[20] else real(t, sym, cm)
    eng = run(md, costs, params, end_day=20)
    call = eng.wheels["AAA"].option
    assert call is not None and call.option_type == "CE"
    assert call.lot == 50 and call.lots == 20 and call.qty == eng.wheels["AAA"].shares == 1000


def test_topup_call_covers_lots_left_by_participation_cap(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": full_wheel_path()})
    d = md.trading_days
    cfg_cap = {}
    eng = WheelEngine(md, costs, synth.cfg(params), lambda t: ["AAA"])
    eng._last_day = d[25]
    for day in d[:26]:
        # only the first call fill (day 20) is capped at 4 lots; the top-up fills the rest the next day
        eng.cfg["participation_cap"] = 4 / 10_000 if day == d[20] else params["participation_cap"]
        eng.step(day)
        ww = eng.wheels.get("AAA")
        cfg_cap[day] = ww.option.qty if ww is not None and ww.option is not None and ww.option.option_type == "CE" else 0
    w = eng.wheels["AAA"]
    assert cfg_cap[d[20]] == 400                              # capped first fill
    assert cfg_cap[d[21]] == 1000 and w.option.qty == w.shares  # top-up covers every whole lot
    c = eng.cycles[w.cycle_id]
    assert c.n_call_topups == 1 and c.n_calls == 1
    ev = pd.DataFrame(eng.events)
    assert (ev.event == "covered_call_topup").sum() == 1
    prem = sum(x[3] for x in eng.ledger if x[2] == "premium" and x[0] >= d[20])
    assert prem == pytest.approx(w.option.entry_premium * 1000, rel=1e-9)
    a, b = pnl_reconciles(eng)
    assert a == pytest.approx(b, abs=1.0)


def test_cycle_cannot_end_with_shares(tmp_path, costs, params):
    from nse.wheel.engine import InvariantError
    md = synth.build(tmp_path, {"AAA": FLAT})
    eng = run(md, costs, params, end_day=2)
    w = eng.wheel("AAA")
    w.shares, w.cycle_id = 100, None
    with pytest.raises(InvariantError):
        eng._end_cycle(md.trading_days[2], w, "completed")


# ---------------------------------------------------------------- leverage
def test_leverage_changes_size_not_prices(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT})
    e1 = run(md, costs, params, end_day=25, leverage=1.0)
    e5 = run(md, costs, params, end_day=25, leverage=5.0)
    p1, p5 = trades(e1).iloc[0], trades(e5).iloc[0]
    assert p1.lots == 10 and p5.lots == 52                       # floor(1e6/95k), floor(5e6/95k)
    assert p1.entry_premium == p5.entry_premium                  # same fill price
    assert p1.margin_requirement == 950 * 1000 and p5.margin_requirement == 950 * 5200
    assert p1.leveraged_capital_requirement == 950 * 1000
    assert p5.leveraged_capital_requirement == pytest.approx(950 * 5200 / 5)
    # per-share option P&L identical; total scales with quantity only
    assert p5.option_pnl / 5200 == pytest.approx(p1.option_pnl / 1000)
    assert max(r["margin_utilization"] for r in e5.daily) <= 1.0 + 1e-9
    assert all(r["margin_utilization"] <= 1.0 + 1e-9 for r in e1.daily)


def test_same_quantity_same_pnl_regardless_of_leverage(tmp_path, costs, params):
    """5x on 1e6 and 1x on 5e6 sell the same 52 lots, so the PUT leg is identical — but Step 9a breaks the
    equivalence at assignment. Delivery costs the full notional in cash: the 1x book holds 5e6 and can pay
    for it, the 5x book holds 1e6 and cannot, so it realises the loss instead. Leverage therefore no longer
    scales a strategy; past 1x it changes which strategy runs. This is the reason the pre-declared L grid is
    not a like-for-like comparison."""
    md = synth.build(tmp_path, {"AAA": full_wheel_path()})
    # margin rules are tested separately; lift the limit so both books hold the same position throughout
    e5 = run(md, costs, params, end_day=39, leverage=5.0, initial_capital=1_000_000, max_margin_utilization=10)
    e1 = run(md, costs, params, end_day=39, leverage=1.0, initial_capital=5_000_000, max_margin_utilization=10)
    t5, t1 = trades(e5), trades(e1)
    # the put that opens each book is the same trade, sized identically
    p5, p1 = t5[t5.instrument == "PE"].iloc[0], t1[t1.instrument == "PE"].iloc[0]
    for f in ("date", "strike", "quantity", "entry_premium"):
        assert p5[f] == p1[f]
    # but only the unlevered book can pay for delivery
    assert e1.diag["assignments_physical"] == 1 and e1.diag["assignments_cash_rejected"] == 0
    assert e5.diag["assignments_physical"] == 0 and e5.diag["assignments_cash_rejected"] == 1
    assert p1.status == "assigned" and p5.status == "cash_settled_insufficient_cash"
    assert (t1.instrument == "STOCK").any() and not (t5.instrument == "STOCK").any()
    assert not (t5.instrument == "CE").any()               # no shares, so the wheel never reaches a call
    # neither book borrows
    assert min(r["cash"] for r in e1.daily) >= 0.0 and min(r["cash"] for r in e5.daily) >= 0.0


def test_margin_call_forces_liquidation(tmp_path, costs, params):
    p = [1000.0] * 5 + [930.0, 880.0, 850.0] + [850.0] * 52
    md = synth.build(tmp_path, {"AAA": p})
    eng = run(md, costs, params, end_day=15, leverage=5.0)
    ev = pd.DataFrame(eng.events)
    assert (ev.event == "margin_call").any() and (ev.event == "margin_call_liquidation").any()
    tr = trades(eng)
    assert tr.iloc[0].status == "margin_call_buyback"
    call_day = ev[ev.event == "margin_call"].date.iloc[0]
    assert tr.iloc[0].exit_date == md.next_day(call_day)
    first_breach = next(r for r in eng.daily if r["margin_utilization"] > 1)
    assert first_breach["date"] == call_day
    a, b = pnl_reconciles(eng)
    assert a == pytest.approx(b, abs=1.0)


def test_financing_is_never_charged_under_the_cash_only_rule(tmp_path, costs, params):
    """This scenario used to borrow ~5x NAV to take delivery and pay financing on it every day. Step 9a
    forbids borrowing, so the ITM put is realised instead and the financing leg can never fire — whatever
    `margin_financing_rate` is set to. The rate survives in params.yaml only for the pre-Step-9a comparison."""
    p = [1000.0] * 19 + [900.0] * 41
    md = synth.build(tmp_path, {"AAA": p})
    eng = run(md, costs, params, end_day=30, leverage=5.0, margin_financing_rate=0.10, max_margin_utilization=10)
    assert eng.wheels["AAA"].shares == 0                   # no delivery: it was not affordable
    assert eng.diag["assignments_cash_rejected"] == 1
    assert not [x for x in eng.ledger if x[2] == "financing"]
    assert eng.diag["financing_inr"] == 0.0
    assert min(r["cash"] for r in eng.daily) >= 0.0


def test_cash_secured_book_is_never_margin_called(tmp_path, costs, params):
    p = [1000.0] * 5 + [700.0] * 14 + [650.0] * 41            # 35% crash through a short put, then held stock
    md = synth.build(tmp_path, {"AAA": p})
    eng = run(md, costs, params, end_day=50, leverage=1.0)
    assert eng.diag["margin_breach_days"] == 0 and eng.wheels["AAA"].shares == 1000
    assert max(r["margin_utilization"] for r in eng.daily) <= 1.0 + 1e-9


def test_leverage_1_never_borrows(tmp_path, costs, params):
    p = [1000.0] * 19 + [900.0] * 41
    md = synth.build(tmp_path, {"AAA": p})
    eng = run(md, costs, params, end_day=40, leverage=1.0, margin_financing_rate=0.10)
    assert min(r["cash"] for r in eng.daily) > -1_000             # only costs could dip below zero
    assert eng.diag["financing_inr"] < 10


# ---------------------------------------------------------------- portfolio rules
def test_max_names_and_no_duplicates(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT, "BBB": FLAT, "CCC": FLAT})
    eng = run(md, costs, params, sel=("AAA", "BBB", "CCC"), end_day=45, n_positions=2)
    tr = trades(eng)
    for d in md.trading_days[:45]:
        open_ = tr[(tr.date <= d) & (tr.exit_date > d)]
        assert open_.symbol.nunique() <= 2 and not open_.symbol.duplicated().any()
    assert set(tr[tr.date == md.trading_days[1]].symbol) == {"AAA", "BBB"}       # selection priority order


def test_failed_fill_is_retried_next_day(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT}, zero_contracts={("2022-01-04", "AAA")})
    eng = run(md, costs, params, end_day=10)
    tr = trades(eng) if eng.trades else pd.DataFrame(eng.open_positions_rows(md.trading_days[10]))
    # day 1: fill fails (no trades). Re-signal on day 1's close finds no liquid strike (same empty day) and
    # still counts; re-signal on day 2 succeeds and fills on day 3 with 2 retries used.
    assert tr.iloc[0].date == md.trading_days[3]
    assert eng.diag["fill_fail_no_trades_t1"] == 1 and eng.diag["retries_used"] == 2


def test_participation_cap_limits_lots(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT}, contracts=40)
    eng = run(md, costs, params, end_day=10)
    pos = eng.wheels["AAA"].option or None
    lots = pos.lots if pos else trades(eng).iloc[0].lots
    assert lots == 4                                                 # 10% of 40 contracts


def test_termination_settles_at_cm_close(tmp_path, costs, params):
    p = [1000.0] * 10 + [900.0] * 50
    md = synth.build(tmp_path, {"AAA": p})
    md.terminations = {(md.trading_days[12], "AAA"): "test merger"}
    md.termination_by_symbol = {"AAA": (md.trading_days[11], md.trading_days[12])}   # announced day 11
    eng = run(md, costs, params, end_day=18)
    tr = trades(eng)
    assert tr.iloc[0].exit_date == md.trading_days[12] and tr.iloc[0].status == "terminated_assigned"
    stock = tr[tr.instrument == "STOCK"].iloc[0]
    assert stock.status == "terminated" and eng.wheels["AAA"].shares == 0
    assert eng.diag["skip_termination_announced"] == 0            # put opened before the announcement
    a, b = pnl_reconciles(eng)
    assert a == pytest.approx(b, abs=1.0)


# ---------------------------------------------------------------- corporate actions
def test_no_new_contract_after_termination_announced(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT})
    md.terminations = {(md.trading_days[12], "AAA"): "test merger"}
    md.termination_by_symbol = {"AAA": (md.trading_days[0], md.trading_days[12])}
    eng = run(md, costs, params, end_day=10)
    assert eng.trades == [] and eng.wheels.get("AAA") is None or eng.wheels["AAA"].option is None
    assert eng.diag["skip_termination_announced"] == 1


def test_split_detected_and_applied_with_nav_continuity(tmp_path, costs, params):
    p = [1000.0] * 19 + [900.0] * 5 + [450.0] * 36          # 2:1 split on day 24 while holding stock
    md = synth.build(tmp_path, {"AAA": p}, splits={"AAA": (24, 2)})
    ca = CA.detect(md)
    acc = ca[ca.accepted]
    assert len(acc) == 1 and acc.share_multiplier.iloc[0] == 2.0 and acc.ex_date.iloc[0] == md.trading_days[24]
    eng = run(md, costs, params, end_day=30, ca=CA.load(ca, tmp_path / "none.csv") if False else acc.assign(
        distribution_per_share=np.nan))
    nav = {r["date"]: r["nav"] for r in eng.daily}
    d = md.trading_days
    assert eng.wheels["AAA"].shares == 2000
    assert abs(nav[d[24]] / nav[d[23]] - 1) < 0.01               # only option theta / slippage moves NAV
    o = eng.wheels["AAA"].option
    if o is not None:
        assert o.qty <= 2000 and o.strike < 600


# ---------------------------------------------------------------- look-ahead
def test_decisions_identical_when_future_data_deleted(tmp_path, costs, params):
    path = full_wheel_path()
    full = synth.build(tmp_path / "full", {"AAA": path})
    cut_day = full.trading_days[30]
    trunc = synth.build(tmp_path / "trunc", {"AAA": path}, truncate_after=cut_day)
    e_full = run(full, costs, params, end_day=30)
    e_trunc = run(trunc, costs, params, end_day=30)
    assert e_full.trades == e_trunc.trades
    assert pd.DataFrame(e_full.daily).equals(pd.DataFrame(e_trunc.daily))
    assert [vars(o) for o in e_full.orders] == [vars(o) for o in e_trunc.orders]
