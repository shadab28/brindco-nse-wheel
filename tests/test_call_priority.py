"""Covered-call priority after assignment (`covered_call_first`): calls on usable assigned shares are decided and
filled before any new put; puts wait while those calls are pending; margin breaches still liquidate."""
import pandas as pd
import pytest

from tests import synth
from tests.test_stage2_engine import FLAT, pnl_reconciles, run

ASSIGN = [1000.0] * 19 + [900.0] * 41            # AAA's 950 put is assigned at the day-19 expiry


def events(eng, name=None):
    ev = pd.DataFrame(eng.events)
    return ev if name is None or ev.empty else ev[ev.event == name]


def trades(eng):
    return pd.DataFrame(eng.trades + eng.open_positions_rows(eng.daily[-1]["date"]))


def audit(eng, **match):
    df = pd.DataFrame(eng.audit)
    for k, v in match.items():
        df = df[df[k] == v]
    return df


def no_naked_calls(eng):
    t = trades(eng)
    for c in t[t.instrument == "CE"].itertuples():
        held = t[(t.instrument == "STOCK") & (t.cycle_id == c.cycle_id)]
        assert held.quantity.sum() >= c.quantity


def two_names(tmp_path, **build):
    return synth.build(tmp_path, {"AAA": ASSIGN, "BBB": FLAT}, **build)


# 1 ---------------------------------------------------------------- assignment IS gated, by cash (Step 9a)
def test_itm_put_is_realised_not_assigned_when_cash_is_insufficient(tmp_path, costs, params):
    """Step 9a replaced the old 'assignment is never gated' rule. At 5x the delivery cost is ~L x NAV, far
    above free cash, so the put is closed out for its intrinsic value and the loss booked: no shares, no
    borrowing, cash never negative. The engine used to finance this at margin_financing_rate."""
    md = synth.build(tmp_path, {"AAA": ASSIGN})
    eng = run(md, costs, params, end_day=21, leverage=5.0, max_margin_utilization=10)
    d19 = md.trading_days[19]
    put = trades(eng)
    put = put[put.instrument == "PE"].iloc[0]
    assert put.status == "cash_settled_insufficient_cash"
    assert not put.assigned_or_exercised
    assert eng.wheels["AAA"].shares == 0                       # no delivery taken
    assert eng.daily[19]["cash"] >= 0                          # and nothing borrowed to take it
    assert eng.diag["assignments_cash_rejected"] == 1
    assert eng.diag["assignments_physical"] == 0
    ev = events(eng, "put_forced_realisation")
    assert len(ev) == 1
    row = ev.iloc[0]
    assert row.date == d19 and row.symbol == "AAA"
    assert row.required_cash > row.free_cash and row.shortfall > 0
    assert row.realised_loss > 0
    # the loss is the intrinsic value plus settlement costs, never the full strike
    assert row.realised_loss < put.strike * put.quantity
    assert not events(eng, "ASSIGNMENT_LIQUIDITY_WARNING").shape[0]   # nothing was assigned to warn about


def test_no_warning_when_assignment_is_cash_backed(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": ASSIGN})
    eng = run(md, costs, params, end_day=21, leverage=1.0)
    assert eng.wheels["AAA"].shares > 0 and events(eng, "ASSIGNMENT_LIQUIDITY_WARNING").empty


# 2, 5, 6 ------------------------------------------------------- calls first, liquidity re-measured, puts resume
def test_calls_fill_before_new_puts_are_decided(tmp_path, costs, params):
    md = two_names(tmp_path)
    eng = run(md, costs, params, sel=("AAA", "BBB"), end_day=24, n_positions=2)
    d19, d20, d21 = md.trading_days[19:22]
    t = trades(eng)
    call = t[(t.symbol == "AAA") & (t.instrument == "CE")].iloc[0]
    reentry = t[(t.symbol == "BBB") & (t.instrument == "PE") & (t.date > d19)].iloc[0]
    assert call.date == d20                                  # call decided at the assignment close, sold T+1
    assert reentry.date == d21                                # put only after the call has been processed
    skip = events(eng, "SKIP_NEW_PUT")
    assert (skip[skip.date == d19].reason == "uncovered_assigned_shares_calls_pending").all() and len(skip)
    assert audit(eng, symbol="BBB", event="put_decision", date=d19).empty
    # the put decision on d20 is sized on cash that already contains the call premium
    dec = audit(eng, symbol="BBB", event="put_decision", date=d20).iloc[0]
    sale = audit(eng, symbol="AAA", event="option_sale", date=d20).iloc[0]
    assert dec.opening_cash == pytest.approx(sale.closing_cash)
    assert dec.opening_cash > sale.opening_cash
    log = pd.DataFrame(eng.decision_log).set_index("date")
    assert log.loc[d19, "covered_call_opportunities"] == 1 and "calls_pending" in log.loc[d19, "rejection_reason"]
    assert log.loc[d20, "covered_call_lots_sold"] == call.lots
    assert log.loc[d20, "covered_call_premium"] == pytest.approx(call.entry_premium * call.quantity)
    assert log.loc[d20, "new_put_lots_decided"] > 0 and log.loc[d21, "new_put_lots_executed"] == reentry.lots
    no_naked_calls(eng)


def test_baseline_ordering_decides_puts_on_assignment_day(tmp_path, costs, params):
    md = two_names(tmp_path)
    eng = run(md, costs, params, sel=("AAA", "BBB"), end_day=24, n_positions=2, covered_call_first=False)
    d19, d20 = md.trading_days[19:21]
    t = trades(eng)
    assert t[(t.symbol == "BBB") & (t.instrument == "PE") & (t.date > d19)].iloc[0].date == d20
    assert events(eng, "SKIP_NEW_PUT").empty


def test_fills_process_calls_before_puts_whatever_the_order_list(tmp_path, costs, params):
    md = two_names(tmp_path)
    eng = run(md, costs, params, sel=("AAA", "BBB"), end_day=19, n_positions=2)
    from nse.wheel.engine import Order
    d19, d20 = md.trading_days[19:21]
    eng.orders.insert(0, Order("BBB", "PUT", d19, eng.orders[0].cmonth, 950.0, 1, 100, est_premium=10.0))
    eng.step(d20)
    kinds = [x[2] for x in eng.ledger if x[0] == d20 and x[2] == "premium"]
    syms = [x[1] for x in eng.ledger if x[0] == d20 and x[2] == "premium"]
    assert kinds and syms[0] == "AAA"                           # AAA's call premium booked before BBB's put


# 3 ---------------------------------------------------------------- puts blocked while eligible calls pend
def test_puts_blocked_while_call_fills_fail_then_resume(tmp_path, costs, params):
    md = two_names(tmp_path)
    days = md.trading_days
    # call fills fail on days 20-22 (no option trades for AAA); calls are re-decided on the prior close each time
    zero = {(d.strftime("%Y-%m-%d"), "AAA") for d in days[20:23]}
    md = synth.build(tmp_path / "z", {"AAA": ASSIGN, "BBB": FLAT}, zero_contracts=zero)
    eng = run(md, costs, params, sel=("AAA", "BBB"), end_day=30, n_positions=2)
    t = trades(eng)
    assert not ((t.instrument == "CE") & t.date.isin(days[20:23])).any()        # no manufactured call fill
    assert len(events(eng, "covered_call_fill_failed")) >= 1
    reentry = t[(t.symbol == "BBB") & (t.instrument == "PE") & (t.date > days[19])]
    assert len(reentry)
    blocked = events(eng, "SKIP_NEW_PUT")
    blocked = blocked[blocked.reason == "uncovered_assigned_shares_calls_pending"]
    put_decisions = audit(eng, symbol="BBB", event="put_decision")
    first_dec = put_decisions[put_decisions.date >= days[19]].date.min()
    assert (blocked.date < first_dec).all() and len(blocked) >= 1
    # when the put was decided, no eligible call order was pending
    log = pd.DataFrame(eng.decision_log).set_index("date")
    assert log.loc[first_dec, "new_put_lots_decided"] > 0
    no_naked_calls(eng)


def test_no_valid_call_does_not_block_puts(tmp_path, costs, params):
    days = synth.build(tmp_path, {"AAA": ASSIGN}).trading_days
    zero = {(days[19].strftime("%Y-%m-%d"), "AAA")}                 # no liquid call strike on the assignment close
    md = synth.build(tmp_path / "z", {"AAA": ASSIGN, "BBB": FLAT}, zero_contracts=zero)
    eng = run(md, costs, params, sel=("AAA", "BBB"), end_day=22, n_positions=2)
    na = events(eng, "covered_call_not_available")
    assert (na.date == days[19]).any() and (na.symbol == "AAA").all()
    assert len(audit(eng, symbol="BBB", event="put_decision", date=days[19]))
    skip = events(eng, "SKIP_NEW_PUT")
    assert skip.empty or not (skip.date == days[19]).any()


# 4 ---------------------------------------------------------------- calls limited to usable shares
def test_unusable_shares_neither_get_calls_nor_block_puts(tmp_path, costs, params):
    md = two_names(tmp_path)
    eng = run(md, costs, params, sel=("AAA", "BBB"), end_day=26, n_positions=2, call_fill_lag_days=3)
    days = md.trading_days
    w = eng.wheels["AAA"]
    t = trades(eng)
    call = t[(t.symbol == "AAA") & (t.instrument == "CE")].iloc[0]
    assert w.usable_from == days[21] and call.date == days[22]
    assert call.quantity <= w.shares
    assert len(audit(eng, symbol="BBB", event="put_decision", date=days[19]))       # not blocked by unusable shares


def test_calls_never_exceed_usable_shares_when_lot_grows(tmp_path, costs, params):
    md0 = synth.build(tmp_path, {"AAA": ASSIGN})
    nxt = md0.contract_after(md0.trading_days[20])
    md = synth.build(tmp_path / "g", {"AAA": ASSIGN}, lot_overrides={("AAA", nxt): 700})
    eng = run(md, costs, params, end_day=30)
    t = trades(eng)
    for c in t[t.instrument == "CE"].itertuples():
        assert c.quantity <= t[(t.instrument == "STOCK") & (t.cycle_id == c.cycle_id)].quantity.sum()


# 7 ---------------------------------------------------------------- margin breach still liquidates
def test_short_put_breach_liquidates_next_day_unchanged(tmp_path, costs, params):
    p = [1000.0] * 5 + [930.0, 880.0, 850.0] + [850.0] * 52
    md = synth.build(tmp_path, {"AAA": p})
    eng = run(md, costs, params, end_day=15, leverage=5.0)
    ev = events(eng)
    call_day = ev[ev.event == "margin_call"].date.iloc[0]
    liq = ev[ev.event == "margin_call_liquidation"]
    assert liq.date.iloc[0] == md.next_day(call_day) and events(eng, "margin_breach_calls_first").empty


def test_cash_only_book_cannot_breach_margin_from_assigned_stock(tmp_path, costs, params):
    """This scenario used to produce a stock-driven margin breach: at 5x the book took delivery with borrowed
    cash, so stock exposure reached ~L x NAV and utilisation blew through the limit, and the engine then tried
    covered calls before liquidating.

    Step 9a makes that state unreachable. Stock can only ever be bought with cash in hand, so stock value <=
    NAV and its leveraged requirement <= NAV / L, which cannot breach max_margin_utilization on its own. The
    calls-first deferral path still exists for mark-driven breaches (see
    test_stage2_engine.test_margin_call_forces_liquidation); it simply cannot be reached through assignment."""
    p = [1000.0] * 19 + [900.0, 860.0, 820.0] + [780.0] * 38
    md = synth.build(tmp_path, {"AAA": p})
    eng = run(md, costs, params, end_day=30, leverage=5.0)
    assert eng.diag["assignments_cash_rejected"] > 0        # the old delivery is now a forced realisation
    assert eng.diag["assignments_physical"] == 0
    assert eng.wheels["AAA"].shares == 0
    assert not len(events(eng, "margin_breach_calls_first"))
    assert min(r["cash"] for r in eng.daily) >= 0.0
    a, bb = pnl_reconciles(eng)
    assert a == pytest.approx(bb, abs=1.0)
    no_naked_calls(eng)


def test_no_new_puts_on_breach_day(tmp_path, costs, params):
    # the breach must now come from the short put's mark before expiry: under Step 9a no stock is ever bought
    # with borrowed cash, so a post-assignment stock breach is unreachable (see the test above).
    p = [1000.0] * 5 + [930.0, 880.0, 850.0] + [850.0] * 52
    md = synth.build(tmp_path, {"AAA": p, "BBB": FLAT})
    eng = run(md, costs, params, sel=("AAA", "BBB"), end_day=30, n_positions=2, leverage=5.0)
    breach_days = {r["date"] for r in eng.decision_log if r["breach"]}
    assert breach_days
    dec = audit(eng, event="put_decision")
    assert not dec.date.isin(breach_days).any()
    assert not [o for o in eng.orders if o.kind == "PUT" and o.decided_on in breach_days]
    # the breach is mark-driven and pre-expiry, so no shares are held: the calls-first deferral has nothing to
    # try and liquidation is not held back. (`margin_breach_no_call_available` needs held stock to fire, which
    # this scenario no longer reaches — the deferral path itself is covered by the test above.)
    b = min(breach_days)
    assert not len(events(eng, "margin_breach_calls_first"))
    assert events(eng, "margin_call_liquidation").date.iloc[0] == md.next_day(b)


# 8, 9 ------------------------------------------------------------ invariants and unchanged assignment mechanics
def test_assignment_mechanics_identical_with_and_without_priority(tmp_path, costs, params):
    md = two_names(tmp_path)
    on = run(md, costs, params, sel=("AAA", "BBB"), end_day=19, n_positions=2, covered_call_first=True)
    off = run(md, costs, params, sel=("AAA", "BBB"), end_day=19, n_positions=2, covered_call_first=False)
    d19 = md.trading_days[19]
    assert [x for x in on.ledger if x[1] == "AAA"] == [x for x in off.ledger if x[1] == "AAA"]
    wa, wb = on.wheels["AAA"], off.wheels["AAA"]
    assert (wa.shares, wa.stock_price, wa.econ_basis, wa.usable_from) == (wb.shares, wb.stock_price, wb.econ_basis,
                                                                          wb.usable_from)
    pa = [x for x in on.trades if x["symbol"] == "AAA"]
    pb = [x for x in off.trades if x["symbol"] == "AAA"]
    assert pa == pb and pa[0]["status"] == "assigned" and pa[0]["exit_date"] == d19
    assert audit(on, event="put_assignment").drop(columns=["available_liquidity", "remaining_liquidity"]).equals(
        audit(off, event="put_assignment").drop(columns=["available_liquidity", "remaining_liquidity"]))


@pytest.mark.parametrize("L", [1.0, 5.0])
def test_full_wheel_under_priority_never_writes_naked_calls(tmp_path, costs, params, L):
    p = [1000.0] * 19 + [900.0] * 20 + [1000.0] * 21
    md = synth.build(tmp_path, {"AAA": p, "BBB": FLAT, "CCC": ASSIGN})
    eng = run(md, costs, params, sel=("AAA", "BBB", "CCC"), n_positions=3, leverage=L, max_margin_utilization=10)
    no_naked_calls(eng)                                            # _reconcile also asserts it every day
    a, b = pnl_reconciles(eng)
    assert a == pytest.approx(b, abs=1.0)
