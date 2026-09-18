"""Step 9a — cash-only assignment. Brindco never borrows: an ITM put is physically assigned only if free
cash covers K x qty plus assignment costs; otherwise it is closed out for its intrinsic value and booked as a
forced realisation loss. `self.cash >= 0` is an invariant after every transaction, never a clamp."""
import pandas as pd
import pytest

from nse.wheel.engine import CALL, PUT, Cycle, OptionPos, Wheel, WheelEngine
from tests import synth

SYM = "AAA"
T = pd.Timestamp("2022-01-28")          # an expiry in the synthetic calendar (20 bdays from 2022-01-03)


@pytest.fixture
def market(tmp_path):
    """Flat-ish path for one name; the expiry lands on day index 19."""
    return synth.build(tmp_path / "cache", {SYM: [1000.0] * 40, "BBB": [500.0] * 40, "CCC": [300.0] * 40})


def engine(params, costs, market, cash, leverage=1.0):
    cfg = synth.cfg(params, initial_capital=cash, leverage=leverage)
    return WheelEngine(market, costs, cfg, lambda t: [])


def open_put(eng, sym, strike, qty, premium=20.0, entry_costs=0.0, cmonth="2022-01", expiry=T):
    """Put the wheel in the state _settle_option expects: an open short put inside an open cycle."""
    eng._cid += 1
    cid = eng._cid
    eng.cycles[cid] = Cycle(cid, sym, pd.Timestamp("2022-01-03"), put_strike=strike)
    o = OptionPos(eng._next_tid(), cid, sym, PUT, cmonth, expiry, strike, qty // 100, 100, qty,
                  pd.Timestamp("2022-01-03"), premium, premium, entry_costs, strike, strike * qty,
                  strike * qty / eng.L, premium)
    eng.wheels[sym] = Wheel(sym, cid, "SHORT_PUT", option=o)
    return o


def hold_stock(eng, sym, shares, price, mark=None):
    """A wheel holding assigned shares with no call written, inside its own open cycle."""
    eng._cid += 1
    cid = eng._cid
    eng.cycles[cid] = Cycle(cid, sym, pd.Timestamp("2022-01-03"), assigned=True, shares_assigned=shares)
    eng.wheels[sym] = Wheel(sym, cid, "STOCK", shares=shares, stock_price=price, stock_date=T,
                            stock_mark=mark if mark is not None else price, econ_basis=price)
    return eng.wheels[sym]


def required_cash(eng, strike, fsp, qty):
    return strike * qty + eng.costs.put_assignment(T, strike, fsp, qty).total


# ---------------------------------------------------------------- the gate
def test_exact_cash_takes_physical_delivery(params, costs, market):
    """free_cash == required_cash is sufficient: shares are received and the existing accounting is unchanged."""
    eng = engine(params, costs, market, cash=0.0)
    o = open_put(eng, SYM, strike=1000, qty=100)
    eng.cash = required_cash(eng, 1000, 950.0, 100)
    eng._settle_option(T, eng.wheels[SYM], 950.0, reason="expiry")
    w = eng.wheels[SYM]
    assert w.shares == 100
    assert w.state == "STOCK"
    assert eng.cycles[o.cycle_id].assigned is True
    assert eng.diag["assignments_physical"] == 1
    assert eng.diag["assignments_cash_rejected"] == 0
    assert eng.cash == pytest.approx(0.0, abs=1e-6)
    assert eng.cash >= -1e-9


def test_one_rupee_short_forces_realisation(params, costs, market):
    """One rupee below required: no delivery, no shares, loss booked, cash still non-negative."""
    eng = engine(params, costs, market, cash=0.0)
    o = open_put(eng, SYM, strike=1000, qty=100)
    eng.cash = required_cash(eng, 1000, 950.0, 100) - 1.0
    cash_before = eng.cash
    eng._settle_option(T, eng.wheels[SYM], 950.0, reason="expiry")
    w = eng.wheels[SYM]
    assert w.shares == 0
    assert w.state == "CASH"
    assert eng.cycles[o.cycle_id].assigned is False
    assert eng.diag["assignments_physical"] == 0
    assert eng.diag["assignments_cash_rejected"] == 1
    # intrinsic (1000 - 950) x 100 = 5,000 plus option settlement costs
    assert eng.cash < cash_before - 5_000
    assert eng.cash >= 0.0


def test_forced_realisation_never_schedules_a_covered_call(params, costs, market):
    eng = engine(params, costs, market, cash=0.0)
    open_put(eng, SYM, strike=1000, qty=100)
    eng.cash = required_cash(eng, 1000, 950.0, 100) - 1.0
    eng._settle_option(T, eng.wheels[SYM], 950.0, reason="expiry")
    w = eng.wheels[SYM]
    assert w.usable_from is None
    assert w.econ_basis == 0.0
    assert w.cycle_id is None                       # cycle ended; nothing left to write a call against
    assert not [o for o in eng.orders if o.kind == "CALL"]


def test_option_pnl_is_identical_on_both_paths(params, costs, market):
    """option_pnl = (premium - intrinsic) x qty regardless of which path settlement takes."""
    pnls = {}
    for label, cash in (("physical", 0.0), ("forced", -1.0)):
        eng = engine(params, costs, market, cash=0.0)
        o = open_put(eng, SYM, strike=1000, qty=100, premium=20.0)
        eng.cash = required_cash(eng, 1000, 950.0, 100) + cash
        eng._settle_option(T, eng.wheels[SYM], 950.0, reason="expiry")
        pnls[label] = eng.cycles[o.cycle_id].option_pnl
    assert pnls["physical"] == pytest.approx((20.0 - 50.0) * 100)
    assert pnls["forced"] == pytest.approx(pnls["physical"])


def test_forced_realisation_separates_closeout_cost_in_the_ledger(params, costs, market):
    eng = engine(params, costs, market, cash=0.0)
    open_put(eng, SYM, strike=1000, qty=100)
    eng.cash = required_cash(eng, 1000, 950.0, 100) - 1.0
    eng._settle_option(T, eng.wheels[SYM], 950.0, reason="expiry")
    kinds = [k for _, _, k, _ in eng.ledger]
    assert "put_cash_settled_intrinsic" in kinds
    assert "put_cash_settled_costs" in kinds
    assert "put_assignment_pay_strike" not in kinds
    intrinsic = next(a for _, _, k, a in eng.ledger if k == "put_cash_settled_intrinsic")
    assert intrinsic == pytest.approx(-(1000 - 950) * 100)


def test_otm_put_is_untouched_by_the_gate(params, costs, market):
    """FSP >= K never reaches the cash test, even with no cash at all."""
    eng = engine(params, costs, market, cash=0.0)
    o = open_put(eng, SYM, strike=1000, qty=100)
    eng._settle_option(T, eng.wheels[SYM], 1000.0, reason="expiry")   # FSP == K is OTM
    assert eng.wheels[SYM].shares == 0
    assert eng.cycles[o.cycle_id].status == "put_expired_otm"
    assert eng.diag["assignments_cash_rejected"] == 0
    assert eng.cash == pytest.approx(0.0)


# ---------------------------------------------------------------- same-day funding of the close-out
def add_call(eng, w, strike, qty, premium=8.0, cmonth="2022-01", expiry=T):
    """Write a covered call against an existing stock wheel, inside the same cycle."""
    o = OptionPos(eng._next_tid(), w.cycle_id, w.symbol, CALL, cmonth, expiry, strike, qty // 100, 100, qty,
                  pd.Timestamp("2022-01-03"), premium, premium, 0.0, w.stock_price, 0.0, 0.0, premium)
    w.option = o
    w.state = "STOCK_CALL"
    return o


def test_unaffordable_closeout_forces_a_stock_sale(params, costs, market):
    """Cash below the close-out itself: held stock is sold the same day rather than cash going negative."""
    eng = engine(params, costs, market, cash=0.0)
    open_put(eng, SYM, strike=1000, qty=100)
    hold_stock(eng, "BBB", shares=200, price=500.0)
    eng.cash = 1_000.0                                  # close-out needs ~5,000 + costs
    eng._settle_option(T, eng.wheels[SYM], 950.0, reason="expiry")
    assert eng.cash >= 0.0
    assert eng.wheels["BBB"].shares == 0                # sold to fund the realisation
    assert eng.diag["cash_settlement_forced_sales"] == 1
    assert any(k == "equity_sale" for _, _, k, _ in eng.ledger)
    assert eng.diag["assignments_cash_rejected"] == 1


def test_forced_sale_never_sells_the_symbol_being_settled(params, costs, market):
    eng = engine(params, costs, market, cash=0.0)
    open_put(eng, SYM, strike=1000, qty=100)
    hold_stock(eng, SYM + "_other", shares=200, price=500.0)
    eng.wheels[SYM].shares = 0
    eng.cash = 1_000.0
    eng._settle_option(T, eng.wheels[SYM], 950.0, reason="expiry")
    assert eng.wheels[SYM].shares == 0
    assert eng.wheels[SYM + "_other"].shares == 0
    assert eng.cash >= 0.0


def test_forced_sale_prefers_uncalled_stock_and_largest_position_first(params, costs, market):
    """No-call wheels go first, descending market value, so the fewest positions are disturbed."""
    eng = engine(params, costs, market, cash=0.0)
    open_put(eng, SYM, strike=1000, qty=100)
    hold_stock(eng, "BBB", shares=200, price=500.0)     # 100,000 - the largest, uncalled
    hold_stock(eng, "CCC", shares=100, price=300.0)     #  30,000 - uncalled
    eng.cash = 1_000.0
    eng._settle_option(T, eng.wheels[SYM], 950.0, reason="expiry")
    assert eng.wheels["BBB"].shares == 0                # largest uncalled sold
    assert eng.wheels["CCC"].shares == 100              # one sale sufficed; smaller left alone
    assert eng.cash >= 0.0
    assert eng.diag["cash_settlement_forced_sales"] == 1


def test_forced_sale_buys_back_the_call_before_selling_covered_shares(params, costs, market):
    """A called wheel is only touched after uncalled stock is exhausted, and never left naked."""
    eng = engine(params, costs, market, cash=0.0)
    open_put(eng, SYM, strike=1000, qty=100)
    w = hold_stock(eng, "BBB", shares=200, price=500.0)
    add_call(eng, w, strike=520, qty=200)
    eng.cash = 1_000.0
    eng._settle_option(T, eng.wheels[SYM], 950.0, reason="expiry")
    assert eng.wheels["BBB"].shares == 0
    assert eng.wheels["BBB"].option is None             # never naked: the call went first
    kinds = [k for _, _, k, _ in eng.ledger]
    assert kinds.index("option_buyback") < kinds.index("equity_sale")
    assert eng.diag["cash_settlement_forced_call_buybacks"] == 1
    assert eng.cash >= 0.0


def test_insolvency_raises_rather_than_borrowing(params, costs, market):
    """Nothing left to sell and still short: a genuine insolvency, not a modelling choice."""
    from nse.wheel.engine import InvariantError
    eng = engine(params, costs, market, cash=0.0)
    open_put(eng, SYM, strike=1000, qty=100)
    eng.cash = 1.0
    with pytest.raises(InvariantError, match="cash settlement shortfall"):
        eng._settle_option(T, eng.wheels[SYM], 950.0, reason="expiry")


# ---------------------------------------------------------------- deterministic expiry ordering
def test_same_day_puts_settle_ascending_by_required_cash(params, costs, market):
    """Only the cheaper delivery fits: it must be the one that gets it, whatever the dict order."""
    eng = engine(params, costs, market, cash=0.0)
    # inserted dearest-first on purpose: plain dict order would deliver BBB and reject CCC
    open_put(eng, "BBB", strike=500, qty=100)           # ~50,000 to deliver
    open_put(eng, "CCC", strike=300, qty=100)           # ~30,000 to deliver
    eng.cash = 55_000.0                                 # funds either one alone, never both
    eng.md.fsp[(T, "CCC")] = 280.0
    eng.md.fsp[(T, "BBB")] = 480.0
    eng._expiries(T)
    assert eng.wheels["CCC"].shares == 100              # cheaper delivered
    assert eng.wheels["BBB"].shares == 0                # dearer realised
    assert eng.diag["assignments_physical"] == 1
    assert eng.diag["assignments_cash_rejected"] == 1
    assert eng.cash >= 0.0


def test_calls_settle_before_puts_so_call_away_cash_can_fund_delivery(params, costs, market):
    """A call-away releases K x qty; that cash must be available to a put expiring the same day."""
    eng = engine(params, costs, market, cash=0.0)
    # the put is inserted first on purpose: plain dict order would settle it before the call-away arrives
    open_put(eng, "CCC", strike=300, qty=100)           # needs ~30,000, cash alone is 0
    w = hold_stock(eng, "BBB", shares=200, price=500.0)
    add_call(eng, w, strike=500, qty=200)               # ITM at FSP 520 -> releases 100,000
    eng.cash = 0.0
    eng.md.fsp[(T, "BBB")] = 520.0
    eng.md.fsp[(T, "CCC")] = 280.0
    eng._expiries(T)
    assert eng.wheels["CCC"].shares == 100              # funded by the call-away that settled first
    assert eng.diag["assignments_physical"] == 1
    assert eng.diag["assignments_cash_rejected"] == 0
    assert eng.cash >= 0.0


# ---------------------------------------------------------------- full-run invariant
def full_run(md, costs, params, sel, **over):
    cfg = synth.cfg(params, **over)
    eng = WheelEngine(md, costs, cfg, lambda t: list(sel))
    eng._last_day = md.trading_days[-1]
    for d in md.trading_days:
        eng.step(d)
    return eng


def test_cash_never_negative_across_a_full_leveraged_run(tmp_path, costs, params):
    """A falling market at 5x leverage is the case that used to borrow to take delivery. Under Step 9a the
    daily cash balance must never go below zero, on any day, for any name."""
    n = 120
    crash = [1000.0 * (1 - 0.006) ** i for i in range(n)]          # ~ -51% drift: assignment after assignment
    md = synth.build(tmp_path / "cache", {"AAA": crash,
                                          "BBB": [x * 0.5 for x in crash],
                                          "CCC": [x * 0.3 for x in crash]}, n_days=n)
    eng = full_run(md, costs, params, ("AAA", "BBB", "CCC"),
                   initial_capital=2_000_000, leverage=5.0, n_positions=3)
    assert eng.daily, "the run produced no days"
    worst = min(r["cash"] for r in eng.daily)
    assert worst >= 0.0, f"cash went negative: {worst:.2f}"
    # the rule must actually bite somewhere in this run, or the test proves nothing

    assert eng.diag["assignments_cash_rejected"] > 0, "no put was ever cash-rejected; scenario too easy"


def test_no_financing_is_ever_charged_under_the_cash_only_rule(tmp_path, costs, params):
    """Borrowing is the thing being removed: the financing charge must never fire."""
    n = 120
    crash = [1000.0 * (1 - 0.006) ** i for i in range(n)]
    md = synth.build(tmp_path / "cache", {"AAA": crash, "BBB": [x * 0.5 for x in crash]}, n_days=n)
    eng = full_run(md, costs, params, ("AAA", "BBB"),
                   initial_capital=2_000_000, leverage=5.0, n_positions=2,
                   margin_financing_rate=0.10)
    assert eng.diag["financing_inr"] == 0.0


def test_daily_reconcile_rejects_negative_cash(params, costs, market):
    """The no-borrow rule is a hard invariant, not a convention: a negative balance must fail the run."""
    from nse.wheel.engine import InvariantError
    eng = engine(params, costs, market, cash=1_000.0)
    eng._cash(T, SYM, "synthetic_overdraft", -2_000.0)
    with pytest.raises(InvariantError, match="negative cash"):
        eng._reconcile(T)
