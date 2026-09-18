"""Cash-leg reserve for short puts: the cash ledger carries no option liability, so every open or pending put
reserves the full leveraged strike K·q/L against cash. The margin leg keeps (K − mark)·q/L, which is offset by
the liability already booked in NAV. (params.yaml defines no separate configured cash reserve.)"""
import math

import pandas as pd
import pytest

from nse.wheel.engine import PUT, Order, OptionPos, Wheel, WheelEngine, max_affordable_lots
from tests import synth


# ---------------------------------------------------------------- state-level helpers (no market needed)
def engine(params, cash=20_000_000.0, leverage=5.0, util=1.0):
    cfg = synth.cfg(params, initial_capital=cash, leverage=leverage, max_margin_utilization=util)
    return WheelEngine(None, None, cfg, lambda t: [])


def add_put(eng, sym, strike, qty, mark, premium=None):
    t = pd.Timestamp("2022-01-04")
    o = OptionPos(0, 0, sym, PUT, "2022-01", t, strike, qty // 100, 100, qty, t,
                  mark if premium is None else premium, mark, 0.0, strike, strike * qty, strike * qty / eng.L, mark)
    eng.wheels[sym] = Wheel(sym, 0, "SHORT_PUT", option=o)
    return o


def add_stock(eng, sym, shares, mark):
    eng.wheels[sym] = Wheel(sym, 0, "STOCK", shares=shares, stock_price=mark, stock_mark=mark)


def old_cash_liquidity(eng):
    """The pre-fix formula, kept only to prove the tests distinguish it."""
    puts = sum((w.option.strike - w.option.last_mark) * w.option.qty / eng.L for w in eng.wheels.values()
               if w.option is not None and w.option.option_type == PUT)
    return eng.cash - puts - eng.pending_reserved()


LEVERAGES = [1.0, 2.0, 3.0, 5.0]


@pytest.mark.parametrize("L", LEVERAGES)
def test_one_open_put_reserves_full_strike(params, L):
    eng = engine(params, cash=1_000_000, leverage=L)
    add_put(eng, "AAA", 950, 1000, mark=40)
    assert eng.cash_liquidity() == pytest.approx(1_000_000 - 950 * 1000 / L)
    assert eng.cash_liquidity() < old_cash_liquidity(eng)
    assert old_cash_liquidity(eng) - eng.cash_liquidity() == pytest.approx(40 * 1000 / L)


@pytest.mark.parametrize("L", LEVERAGES)
def test_multiple_puts_with_different_strikes(params, L):
    eng = engine(params, cash=20_000_000, leverage=L)
    legs = [("AAA", 950, 1000, 12.0), ("BBB", 2400, 500, 55.0), ("CCC", 180, 12_000, 3.5)]
    for sym, k, q, m in legs:
        add_put(eng, sym, k, q, m)
    assert eng.cash_liquidity() == pytest.approx(20_000_000 - sum(k * q / L for _, k, q, _ in legs))


@pytest.mark.parametrize("L", LEVERAGES)
@pytest.mark.parametrize("new_mark", [0.0, 1e-6, 0.05, 5.0, 150.0, 400.0])
def test_cash_reserve_ignores_mark_margin_leg_unchanged(params, L, new_mark):
    """Marks rising (deep ITM) or falling (to ~0) never move the cash reserve; the margin leg still does."""
    eng = engine(params, cash=5_000_000, leverage=L)
    o = add_put(eng, "AAA", 950, 1000, mark=40)
    cash_before = eng.cash_liquidity()
    o.last_mark = new_mark
    assert eng.cash_liquidity() == pytest.approx(cash_before)
    assert eng.margin_liquidity() == pytest.approx(eng.max_util * eng.nav() - (950 - new_mark) * 1000 / L)
    assert eng.margin_of(eng.wheels["AAA"])[1] == pytest.approx((950 - new_mark) * 1000 / L)


@pytest.mark.parametrize("L", LEVERAGES)
def test_negative_cash_after_assignments(params, L):
    """Borrowed stock purchases leave cash < 0; open puts still reserve K·q/L on top, and no put fits."""
    eng = engine(params, cash=-3_000_000, leverage=L)
    add_stock(eng, "AAA", 5000, 880)
    add_stock(eng, "BBB", 2000, 1700)
    add_put(eng, "CCC", 950, 1000, mark=120)
    assert eng.cash_liquidity() == pytest.approx(-3_000_000 - 950 * 1000 / L)
    assert max_affordable_lots(1e12, 950, 100, L, 20, 20, 50, 1.0, 100, cash_free=eng.cash_liquidity()) == 0


@pytest.mark.parametrize("L", LEVERAGES)
def test_pending_put_orders_reserve_full_strike_on_cash_leg(params, L):
    eng = engine(params, cash=10_000_000, leverage=L)
    add_put(eng, "AAA", 950, 1000, mark=30)
    eng.orders.append(Order("BBB", "PUT", pd.Timestamp("2022-01-03"), "2022-01", 2000.0, 5, 100, est_premium=45.0))
    eng.orders.append(Order("CCC", "CALL", pd.Timestamp("2022-01-03"), "2022-01", 500.0, 5, 100))   # no reserve
    assert eng.pending_reserved(cash=True) == pytest.approx(2000 * 500 / L)
    assert eng.pending_reserved() == pytest.approx((2000 - 45) * 500 / L)            # margin leg unchanged
    assert eng.cash_liquidity() == pytest.approx(10_000_000 - 950 * 1000 / L - 2000 * 500 / L)
    assert eng.margin_liquidity() == pytest.approx(
        eng.nav() - (950 - 30) * 1000 / L - (2000 - 45) * 500 / L)


@pytest.mark.parametrize("util", [1.0, 0.6, 0.3])
def test_utilisation_cap_touches_margin_leg_only(params, util):
    eng = engine(params, cash=4_000_000, leverage=5.0, util=util)
    add_put(eng, "AAA", 950, 1000, mark=60)
    assert eng.cash_liquidity() == pytest.approx(4_000_000 - 950 * 1000 / 5)
    assert eng.margin_liquidity() == pytest.approx(util * eng.nav() - (950 - 60) * 1000 / 5)
    assert eng.available_liquidity() == pytest.approx(min(eng.cash_liquidity(), eng.margin_liquidity()))


@pytest.mark.parametrize("L", LEVERAGES)
@pytest.mark.parametrize("mark", [0.0, 0.05, 63.0, 300.0])
def test_max_affordable_lots_cash_leg_uses_full_strike(L, mark):
    cash_free, k, lot, prem, cost = 2_000_000, 950, 100, 63.0, 50.0
    lots = max_affordable_lots(1e12, k, lot, L, prem, mark, cost, 1.0, 10_000, cash_free=cash_free)
    per_lot = k * lot / L - (prem * lot - cost)
    assert lots == math.floor(cash_free / per_lot)
    assert lots * per_lot <= cash_free < (lots + 1) * per_lot           # cash after trade >= reserve, and maximal


@pytest.mark.parametrize("L", LEVERAGES)
def test_premium_received_credits_cash_leg(L):
    kw = dict(free=1e12, strike=950, lot=100, leverage=L, mark=63, cost_per_lot=50, max_util=1.0, cap=10_000,
              cash_free=500_000)
    with_prem = max_affordable_lots(premium=63, **kw)
    no_prem = max_affordable_lots(premium=0.0, **kw)
    assert with_prem >= no_prem
    assert no_prem == math.floor(500_000 / (950 * 100 / L + 50))


# ---------------------------------------------------------------- engine runs on a synthetic market
def run_hooked(md, costs, params, sel, end_day, **over):
    """Step the engine and snapshot the portfolio around every successful put fill.

    The price stop is off by default here: this suite exercises Step 9a's cash-only assignment, which needs the
    put to survive to expiry, and CRASH falls exactly 15% — the live `put_stop_loss_pct` — so the stop would
    close every one of these puts before assignment. A test that wants the stop passes it explicitly.
    """
    over = {"put_stop_loss_pct": None, **over}
    eng = WheelEngine(md, costs, synth.cfg(params, **over), lambda t: list(sel))
    fills, decisions, orig, orig_decide = [], [], eng._fill_option, eng.decide_put

    def hooked_decide(t, sym, retries=0):
        pre = dict(t=t, sym=sym, cash_liq=eng.cash_liquidity(), old_cash_liq=old_cash_liquidity(eng),
                   margin_liq=eng.margin_liquidity(), n_audit=len(eng.audit))
        od = orig_decide(t, sym, retries)
        rows = [r for r in eng.audit[pre["n_audit"]:] if r["event"] == "put_decision"]
        if od is not None and rows:
            decisions.append(dict(pre, K=od.strike, lot=od.lot, lots=od.lots, prem=od.est_premium,
                                  considered=rows[-1]["lots_considered"]))
        return od

    def hooked(t, od, w):
        pre = dict(cash=eng.cash, cash_liq=eng.cash_liquidity(), old_cash_liq=old_cash_liquidity(eng),
                   margin_liq=eng.margin_liquidity(), cap=od.lots)
        ok, why = orig(t, od, w)
        if ok and od.kind == "PUT":
            o = w.option
            q = md.quote(t, od.symbol, od.cmonth, PUT, od.strike)
            fills.append(dict(t=t, sym=od.symbol, K=o.strike, lot=o.lot, lots=o.lots, px=o.entry_premium,
                              mark=q.mark, participation=math.floor(params["participation_cap"] * q.contracts),
                              post_cash=eng.cash, post_cash_liq=eng.cash_liquidity(),
                              open_puts=[(x.option.strike, x.option.qty) for x in eng.wheels.values()
                                         if x.option is not None and x.option.option_type == PUT],
                              pending=[(p.strike, p.lots * p.lot) for p in eng.orders
                                       if p.kind == "PUT" and p.strike is not None], **pre))
        return ok, why

    eng._fill_option = hooked
    eng.decide_put = hooked_decide
    for d in md.trading_days[:end_day + 1]:
        eng.step(d)
    run_hooked.decisions = decisions
    return eng, fills


def check_cash_invariant(eng, fills):
    """cash_after_trade >= Σ open K·q/L + Σ pending K·q/L (proposed put now open), recomputed from raw state."""
    for f in fills:
        reserve = sum(k * q for k, q in f["open_puts"]) / eng.L + sum(k * q for k, q in f["pending"]) / eng.L
        assert f["post_cash"] - reserve >= -1e-6, f
        assert f["post_cash_liq"] == pytest.approx(f["post_cash"] - reserve)


def cash_leg_room(eng, f, n):
    """Cash leg after selling n lots at this fill, under the fixed and the old formula."""
    cost = eng.costs.option_sale(f["t"], f["px"] * n * f["lot"]).total
    inflow = f["px"] * n * f["lot"] - cost
    new = f["cash_liq"] + inflow - f["K"] * n * f["lot"] / eng.L
    old = f["old_cash_liq"] + inflow - (f["K"] - f["mark"]) * n * f["lot"] / eng.L
    return new, old


CRASH = [1000.0] * 5 + [850.0] * 55             # below the 950 put strike through the day-19 expiry
FLAT = [1000.0] * 60


@pytest.fixture(scope="module")
def crash_market(tmp_path_factory):
    return synth.build(tmp_path_factory.mktemp("crash"), {s: CRASH if s in CRASHED else FLAT for s in SEL})


CRASHED = ("AAA", "BBB", "CCC")
SEL = CRASHED + tuple(f"F{i:02d}" for i in range(10))


def test_regression_assignments_then_new_puts_sized_on_full_strike(crash_market, costs, params):
    """₹2 Cr at 5×: three names are assigned, cash falls below the margin leg, and the re-entry puts on the next
    expiry must be sized on cash − Σ K·q/L (open + pending + proposed), not cash − Σ (K − mark)·q/L."""
    md = crash_market
    eng, fills = run_hooked(md, costs, params, SEL, end_day=22, initial_capital=20_000_000, leverage=5.0,
                            n_positions=20)
    decisions = run_hooked.decisions
    d19 = md.trading_days[19]
    assert eng.diag["assignments"] == 3
    assert all(eng.wheels[s].shares > 0 for s in CRASHED)
    assert eng.daily[19]["cash_liquidity"] < eng.daily[19]["margin_liquidity"]        # cash leg binds
    check_cash_invariant(eng, fills)

    # every re-entry fill keeps cash >= reserve and, when cut back, could not take one more lot
    reentry = [f for f in fills if f["t"] > d19]
    assert len(reentry) >= 2                                               # several fills in the same event
    for f in reentry:
        assert f["cash_liq"] < f["margin_liq"]
        assert cash_leg_room(eng, f, f["lots"])[0] >= -1e-6
        if f["lots"] < min(f["cap"], f["participation"]):
            assert cash_leg_room(eng, f, f["lots"] + 1)[0] < 0

    # re-entry decisions (after the assignment day's covered calls, when covered_call_first defers them): sized
    # exactly on the full-strike cash reserve; the old reserve would have allowed more
    d_entry = min(d["t"] for d in decisions if d["t"] >= d19)
    reent = [d for d in decisions if d["t"] == d_entry]
    assert reent
    old_allows_more = False
    for d in reent:
        k, lot, prem, n0 = d["K"], d["lot"], d["prem"], d["considered"]
        est = eng.costs.option_sale(d_entry, prem * n0 * lot).total / n0
        new = max_affordable_lots(d["margin_liq"], k, lot, 5.0, prem, prem, est, 1.0, n0, cash_free=d["cash_liq"])
        assert d["lots"] == new
        assert d["cash_liq"] + (prem * lot - est) * new - k * new * lot / 5.0 >= -1e-6
        if new < n0:
            assert d["cash_liq"] + (prem * lot - est) * (new + 1) - k * (new + 1) * lot / 5.0 < 0
            old_room = d["old_cash_liq"] + (prem * lot - est) * (new + 1) - (k - prem) * (new + 1) * lot / 5.0
            old_allows_more |= old_room >= 0
    assert old_allows_more, "scenario does not distinguish the old (K − mark) cash reserve"
    dec = pd.DataFrame(eng.audit)
    assert dec[(dec.event == "put_decision") & (dec.date == d_entry)].rejection_reason.eq("insufficient_liquidity").any()


@pytest.mark.parametrize("L", LEVERAGES)
@pytest.mark.parametrize("util", [1.0, 0.6])
def test_cash_invariant_holds_on_every_put_fill(crash_market, costs, params, L, util):
    eng, fills = run_hooked(crash_market, costs, params, SEL, end_day=45, initial_capital=20_000_000, leverage=L,
                            n_positions=10, max_margin_utilization=util)
    assert fills
    check_cash_invariant(eng, fills)
    for f in fills:
        assert f["cash_liq"] + f["px"] * f["lots"] * f["lot"] - f["K"] * f["lots"] * f["lot"] / L >= -1e-6


def test_cash_leg_still_gates_new_puts_at_5x_without_going_negative(crash_market, costs, params):
    """The cash leg used to be driven below zero by assignments paid for with borrowed cash, and that
    overdraft blocked new puts. Step 9a removes the overdraft: the ITM puts are realised instead, cash stays
    non-negative, and the cash leg still reserves the full leveraged strike so it remains the binding
    constraint (cash_liquidity <= cash on every day)."""
    eng, fills = run_hooked(crash_market, costs, params, SEL, end_day=25, initial_capital=20_000_000, leverage=5.0,
                            n_positions=4)
    assert eng.diag["assignments_cash_rejected"] >= 2
    assert eng.diag["assignments_physical"] == 0
    assert all(r["cash"] >= 0 for r in eng.daily)
    assert all(r["cash_liquidity"] <= r["cash"] + 1e-6 for r in eng.daily)
