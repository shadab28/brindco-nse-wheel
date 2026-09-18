"""Cash balance, premium credits, leverage, dynamic lot sizing, assignment cash flows, borrowing costs and the
"no trade when liquidity is insufficient" rule (portfolio-level liquidity, synthetic markets)."""
import math

import pandas as pd
import pytest

from tests import synth
from tests.test_stage2_engine import FLAT, pnl_reconciles, run


def audit(eng, **match):
    df = pd.DataFrame(eng.audit)
    for k, v in match.items():
        df = df[df[k] == v]
    return df


# ---------------------------------------------------------------- premium credits
def test_premium_credited_to_cash_on_sale(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT})
    eng = run(md, costs, params, end_day=1)
    o = eng.wheels["AAA"].option
    day1 = [x for x in eng.ledger if x[0] == md.trading_days[1]]
    assert [x[3] for x in day1 if x[2] == "premium"] == [pytest.approx(o.entry_premium * o.qty)]
    assert eng.cash == pytest.approx(eng.initial + o.entry_premium * o.qty - o.entry_costs)
    row = audit(eng, event="option_sale", symbol="AAA").iloc[0]
    assert row.premium_received == pytest.approx(o.entry_premium * o.qty)
    assert row.transaction_costs == pytest.approx(o.entry_costs)
    assert row.closing_cash == pytest.approx(row.opening_cash + row.premium_received - row.transaction_costs)


# ---------------------------------------------------------------- leverage / liquidity definition
def test_available_liquidity_is_portfolio_level(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT, "BBB": FLAT})
    eng = run(md, costs, params, sel=("AAA", "BBB"), end_day=3, n_positions=2, leverage=5.0,
              max_margin_utilization=0.6)
    expect = eng.max_util * eng.nav() - eng.required() - eng.pending_reserved()
    assert eng.available_liquidity() == pytest.approx(expect)
    # leveraged requirement of a short put is (K − mark) × q / L: the premium already offsets it
    o = eng.wheels["AAA"].option
    assert eng.margin_of(eng.wheels["AAA"])[1] == pytest.approx((o.strike - o.last_mark) * o.qty / 5.0)


def test_max_lots_counts_premium_inflow():
    from nse.wheel.engine import max_affordable_lots
    # free 300,000; K 950, lot 100, L 5, premium 63 at a mark of 63, costs 50/lot, utilisation cap 0.3
    lots = max_affordable_lots(free=300_000, strike=950, lot=100, leverage=5, premium=63, mark=63,
                               cost_per_lot=50, max_util=0.3, cap=100)
    per_lot = (950 - 63) * 100 / 5 + 0.3 * 50
    assert lots == math.floor(300_000 / per_lot) == 16
    assert lots > math.floor(300_000 / (950 * 100 / 5))            # gross sizing would only allow 15
    assert max_affordable_lots(free=300_000, strike=950, lot=100, leverage=5, premium=63, mark=63,
                               cost_per_lot=50, max_util=0.3, cap=4) == 4
    assert max_affordable_lots(free=-1, strike=950, lot=100, leverage=5, premium=63, mark=63,
                               cost_per_lot=50, max_util=0.3, cap=100) == 0


def test_dynamic_lot_sizing_uses_premium_and_stays_within_limit(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT}, vol=0.9)
    eng = run(md, costs, params, end_day=1, leverage=5.0, max_margin_utilization=0.3)
    o = eng.wheels["AAA"].option
    gross_lots = math.floor(0.3 * eng.initial / (o.strike * o.lot / 5.0))
    assert o.lots > gross_lots                                       # premium inflow bought extra lots
    rec = eng.daily[-1]
    assert rec["margin_utilization"] <= 0.3 + 1e-9                   # ...without breaching the limit
    row = audit(eng, event="option_sale", symbol="AAA").iloc[0]
    assert row.lots_executed == o.lots
    assert row.remaining_liquidity >= 0
    assert row.remaining_liquidity < row.required_liquidity / row.lots_executed   # one more lot would not fit
    assert row.lots_rejected == row.lots_considered - row.lots_executed


# ---------------------------------------------------------------- insufficient liquidity
def test_no_trade_when_liquidity_insufficient(tmp_path, costs, params):
    """Two names, liquidity for roughly one position: the second must be skipped, not over-traded."""
    md = synth.build(tmp_path, {"AAA": FLAT, "BBB": FLAT})
    eng = run(md, costs, params, sel=("AAA", "BBB"), end_day=3, n_positions=2, leverage=1.0,
              max_margin_utilization=1.0, initial_capital=130_000)            # 1 lot needs ~95,000 − premium
    assert eng.wheels["AAA"].option is not None and eng.wheels["AAA"].option.lots == 1
    assert eng.wheels.get("BBB") is None or eng.wheels["BBB"].option is None
    rej = audit(eng, symbol="BBB")
    assert len(rej) and (rej.lots_executed == 0).all()
    assert rej.rejection_reason.str.contains("insufficient_liquidity").all()
    assert (rej.required_liquidity > rej.available_liquidity).all()
    assert all(r["margin_utilization"] <= 1.0 + 1e-9 for r in eng.daily)


def test_leverage_does_not_mean_unlimited_capital(tmp_path, costs, params):
    md = synth.build(tmp_path, {n: FLAT for n in ("AAA", "BBB", "CCC")})
    eng = run(md, costs, params, sel=("AAA", "BBB", "CCC"), end_day=3, n_positions=3, leverage=5.0,
              initial_capital=50_000)                        # 1 lot at 5× needs ~18,000 net: two fit, three do not
    held = [w for w in eng.wheels.values() if w.option is not None]
    assert eng.required() <= eng.max_util * eng.nav() + 1e-6
    assert sum(w.option.lots for w in held) == 2
    assert (audit(eng, symbol="CCC").rejection_reason == "insufficient_liquidity").all()


# ---------------------------------------------------------------- assignment / call-away
def test_put_assignment_debits_strike_and_keeps_premium(tmp_path, costs, params):
    p = [1000.0] * 19 + [900.0] * 41
    md = synth.build(tmp_path, {"AAA": p})
    eng = run(md, costs, params, end_day=19)
    d = md.trading_days[19]
    w = eng.wheels["AAA"]
    put = pd.DataFrame(eng.trades).iloc[0]
    flows = {x[2]: x[3] for x in eng.ledger if x[0] == d}
    assert flows["put_assignment_pay_strike"] == pytest.approx(-950 * 1000)
    assert w.shares == 1000 and w.state == "STOCK"
    assert sum(x[3] for x in eng.ledger if x[2] == "premium") == pytest.approx(put.entry_premium * 1000)
    row = audit(eng, event="put_assignment").iloc[0]
    assert row.stock_cash_flow == pytest.approx(-950 * 1000)
    assert row.closing_cash == pytest.approx(row.opening_cash - 950 * 1000 - row.transaction_costs)
    assert row.available_liquidity == pytest.approx(
        eng.max_util * eng.daily[-1]["nav"] - eng.daily[-1]["leveraged_requirement"], abs=1e-6)


def test_call_away_credits_strike_and_releases_capital(tmp_path, costs, params):
    from tests.test_stage2_engine import full_wheel_path
    md = synth.build(tmp_path, {"AAA": full_wheel_path()})
    eng = run(md, costs, params, end_day=39)
    row = audit(eng, event="call_away").iloc[0]
    assert row.stock_cash_flow == pytest.approx(950 * 1000)
    assert row.closing_cash == pytest.approx(row.opening_cash + 950 * 1000 - row.transaction_costs)
    assert eng.wheels["AAA"].shares == 0
    assert eng.required() == 0.0
    # released capital funds the next cycle's put on the same expiry
    assert eng.orders and eng.orders[0].kind == "PUT"
    a, b = pnl_reconciles(eng)
    assert a == pytest.approx(b, abs=1.0)


# ---------------------------------------------------------------- interest vs borrowing
def test_cash_only_book_earns_interest_and_never_pays_borrowing(tmp_path, costs, params):
    """Cash was driven negative here by taking delivery at 5x, and the book then paid financing. Step 9a
    forbids that, so every day carries a non-negative balance: interest accrues, the financing leg never
    fires, and the two can still never coincide on one day."""
    p = [1000.0] * 19 + [900.0] * 41
    md = synth.build(tmp_path, {"AAA": p})
    rf = pd.Series({"2022-01": 0.06, "2022-02": 0.06})
    eng = run(md, costs, params, end_day=30, leverage=5.0, margin_financing_rate=0.10, max_margin_utilization=10,
              risk_free=rf)
    daily = pd.DataFrame(eng.daily)
    assert {"cash_interest", "financing_cost"} <= set(daily.columns)
    assert not ((daily.cash_interest_accrued > 0) & (daily.financing_cost > 0)).any()   # never both on one day
    assert (daily.cash >= 0).all()
    assert (daily.financing_cost == 0).all()
    assert not len(audit(eng, event="financing"))
    assert daily.cash_interest.sum() == pytest.approx(sum(x[3] for x in eng.ledger if x[2] == "cash_interest"))
    assert daily.cash_interest.sum() > 0


def test_daily_interest_on_positive_cash_only(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT})
    rf = pd.Series({"2022-01": 0.06, "2022-02": 0.12})
    eng = run(md, costs, params, sel=(), risk_free=rf)
    credits = audit(eng, event="cash_interest")
    daily = pd.DataFrame(eng.daily)
    assert len(credits) == len(md.trading_days) - 1
    assert (daily.cash_interest == daily.cash_interest_accrued).all()          # nothing held back for month end and (credits.interest_income > 0).all() and (credits.borrowing_cost == 0).all()
    assert (credits.closing_cash - credits.opening_cash).values == pytest.approx(credits.interest_income.values)


# ---------------------------------------------------------------- assignment consumes cash liquidity
def test_cash_liquidity_limits_lots():
    from nse.wheel.engine import max_affordable_lots
    kw = dict(free=10_000_000, strike=950, lot=100, leverage=5, premium=63, mark=63, cost_per_lot=50, max_util=1.0,
              cap=100)
    # cash leg: each lot locks the full K·lot/L (cash carries no option liability) and brings in premium·lot − costs
    per_lot_cash = 950 * 100 / 5 - (63 * 100 - 50)
    assert max_affordable_lots(**kw, cash_free=100_000) == math.floor(100_000 / per_lot_cash)
    assert max_affordable_lots(**kw, cash_free=-1) == 0
    assert max_affordable_lots(**kw) == 100                          # no cash leg given: margin leg only


def test_assigned_stock_consumes_liquidity_and_blocks_new_puts(tmp_path, costs, params):
    """AAA's stock used to be paid for with borrowed cash at 5x, and that overdraft was what blocked BBB.
    Under Step 9a AAA is never delivered, so the cash that would have been borrowed is never spent: the
    realised loss is far smaller than the strike value, and BBB is no longer starved of cash. The liquidity
    gate itself is unchanged and still binds on the margin leg — it is the borrowing that is gone."""
    p_aaa = [1000.0] * 19 + [900.0] * 41
    md = synth.build(tmp_path, {"AAA": p_aaa, "BBB": FLAT})
    eng = run(md, costs, params, sel=("AAA", "BBB"), end_day=21, n_positions=2, leverage=5.0,
              covered_call_first=False)      # same-day put decision; the calls-first variant is in test_call_priority
    assert eng.wheels["AAA"].shares == 0
    assert eng.diag["assignments_cash_rejected"] == 1
    cash_after = eng.daily[19]["cash"]
    assert cash_after >= 0
    assert eng.daily[19]["available_liquidity"] <= cash_after + 1e-6
    assert eng.diag["margin_call_liquidations"] == 0                  # entries gated, holdings not force-sold
    assert min(r["cash"] for r in eng.daily) >= 0.0
