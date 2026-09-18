"""Universe removal: a stock that leaves the strategy universe gets no new put, call or cycle, and any existing
wheel position is explicitly wound down (UNIVERSE_REMOVAL), never silently dropped."""
import pandas as pd
import pytest

from nse.wheel.engine import WheelEngine
from tests import synth
from tests.test_stage2_engine import FLAT, pnl_reconciles

ASSIGN = [1000.0] * 19 + [900.0] * 41


class Universe:
    """AAA is a member except on [removed_from, rejoin) trading-day indices; records every date asked."""

    def __init__(self, md, removed_from, rejoin=None):
        self.days = md.trading_days
        self.a = md.trading_days[removed_from]
        self.b = md.trading_days[rejoin] if rejoin is not None else None
        self.asked = []

    def __call__(self, sym, t):
        self.asked.append(pd.Timestamp(t))
        if sym != "AAA":
            return True
        return not (t >= self.a and (self.b is None or t < self.b))


def run(md, costs, params, universe, sel=("AAA",), end_day=None, **over):
    eng = WheelEngine(md, costs, synth.cfg(params, **over), lambda t: list(sel), universe=universe)
    end = md.trading_days[end_day] if end_day is not None else md.trading_days[-1]
    steps = []
    for d in [d for d in md.trading_days if d <= end]:
        n = len(universe.asked)
        eng.step(d)
        steps.append((d, max(universe.asked[n:], default=d)))
    eng._steps = steps
    return eng


def trades(eng):
    return pd.DataFrame(eng.trades + eng.open_positions_rows(eng.daily[-1]["date"]))


def exits(eng):
    return pd.DataFrame(eng.universe_exits)


def events(eng, name):
    ev = pd.DataFrame(eng.events)
    return ev[ev.event == name] if len(ev) else ev


def no_aaa_entries_after(eng, day):
    t = trades(eng)
    new = t[(t.symbol == "AAA") & t.instrument.isin(["PE", "CE"]) & (pd.to_datetime(t.date) > day)]
    assert new.empty, new
    assert not [o for o in eng.orders if o.symbol == "AAA" and o.kind in ("PUT", "CALL")]


# ---------------------------------------------------------------- short put open at removal
def test_short_put_is_bought_back_after_removal_and_never_reopened(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT, "BBB": FLAT})
    u = Universe(md, removed_from=10)
    eng = run(md, costs, params, u, sel=("AAA", "BBB"), n_positions=2, leverage=5.0)
    d10, d11 = md.trading_days[10:12]
    t = trades(eng)
    put = t[(t.symbol == "AAA") & (t.instrument == "PE")]
    assert len(put) == 1                                             # no re-entry at the day-19 or day-39 expiries
    p = put.iloc[0]
    assert p.status == "universe_removal_buyback" and p.exit_reason == "UNIVERSE_REMOVAL"
    assert p.exit_date == d11                                        # decided on the removal close, filled next day
    ex = exits(eng)
    assert len(ex) == 1
    r = ex.iloc[0]
    assert r.removal_date == d10 and r.fill_date == d11 and r.exit_reason == "UNIVERSE_REMOVAL"
    assert r.position_before_exit.startswith("short PE") and r.position_after_exit == "flat" and not r.residual
    assert r.realized_pnl == pytest.approx(p.option_pnl) and r.transaction_cost == pytest.approx(p.transaction_costs)
    # cash released: buy-back price and costs leave cash; the K·q/L reserve returns to liquidity
    buy = [x for x in eng.ledger if x[0] == d11 and x[1] == "AAA"]
    assert r.cash_after - r.cash_before == pytest.approx(sum(x[3] for x in buy))
    entry_costs = next(x.transaction_costs for x in pd.DataFrame(eng.audit).itertuples()
                       if x.event == "option_sale" and x.symbol == "AAA")
    buyback_costs = -sum(x[3] for x in buy if x[2] == "option_buyback_costs")
    assert [x[2] for x in buy] == ["option_buyback", "option_buyback_costs"]
    assert sum(x[3] for x in buy) == pytest.approx(-p.exit_price * p.quantity - buyback_costs)
    assert p.transaction_costs == pytest.approx(entry_costs + buyback_costs) and buyback_costs > 0
    before = eng.daily[10]
    after = eng.daily[11]
    assert after["cash_liquidity"] > before["cash_liquidity"]                       # liquidity recalculated after exit
    assert r.available_liquidity_after == pytest.approx(after["available_liquidity"], rel=1e-6, abs=1.0)
    assert eng.wheels["AAA"].option is None and eng.wheels["AAA"].shares == 0 and eng.wheels["AAA"].cycle_id is None
    no_aaa_entries_after(eng, d11)
    t_b = t[(t.symbol == "BBB") & (t.instrument == "PE")]
    assert len(t_b) >= 2                                              # other names keep wheeling
    a, b = pnl_reconciles(eng)
    assert a == pytest.approx(b, abs=1.0)                             # exit P&L and costs are in NAV


# ---------------------------------------------------------------- assigned shares with a covered call
def test_assigned_shares_and_covered_call_are_wound_down(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": ASSIGN})
    u = Universe(md, removed_from=25)
    eng = run(md, costs, params, u)
    d25, d26 = md.trading_days[25:27]
    t = trades(eng)
    call = t[(t.symbol == "AAA") & (t.instrument == "CE")]
    stock = t[(t.symbol == "AAA") & (t.instrument == "STOCK")]
    assert len(call) == 1 and call.iloc[0].exit_reason == "UNIVERSE_REMOVAL" and call.iloc[0].exit_date == d26
    assert len(stock) == 1 and stock.iloc[0].status == "universe_removal_sale" and stock.iloc[0].exit_date == d26
    assert stock.iloc[0].quantity == call.iloc[0].quantity
    r = exits(eng).iloc[0]
    assert "shares" in r.position_before_exit and "short CE" in r.position_before_exit
    assert r.exit_action == "buy back CE + sell shares" and r.position_after_exit == "flat"
    assert r.realized_pnl == pytest.approx(call.iloc[0].option_pnl + stock.iloc[0].stock_pnl)
    assert r.transaction_cost == pytest.approx(call.iloc[0].transaction_costs + stock.iloc[0].transaction_costs)
    assert eng.wheels["AAA"].shares == 0
    no_aaa_entries_after(eng, d26)
    cyc = [c for c in eng.cycles.values() if c.symbol == "AAA"]
    assert len(cyc) == 1 and cyc[0].end == d26
    a, b = pnl_reconciles(eng)
    assert a == pytest.approx(b, abs=1.0)


def test_shares_assigned_on_removal_day_get_no_call_and_are_sold(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": ASSIGN})
    u = Universe(md, removed_from=19)                                 # removal effective on the assignment expiry
    eng = run(md, costs, params, u, end_day=40)
    d19, d20 = md.trading_days[19:21]
    t = trades(eng)
    assert t[(t.symbol == "AAA") & (t.instrument == "PE")].iloc[0].status == "assigned"      # assignment unaffected
    assert t[(t.symbol == "AAA") & (t.instrument == "CE")].empty                           # no call after removal
    s = t[(t.symbol == "AAA") & (t.instrument == "STOCK")].iloc[0]
    assert s.exit_reason == "UNIVERSE_REMOVAL" and s.exit_date == d20
    assert exits(eng).iloc[0].position_before_exit.endswith("shares")


# ---------------------------------------------------------------- historical date, no look-ahead
def test_nothing_happens_before_the_effective_removal_date(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": ASSIGN})
    u = Universe(md, removed_from=30)
    eng = run(md, costs, params, u)
    d30 = md.trading_days[30]
    assert all(o["date"] >= d30 for o in eng.events if o["event"].startswith("UNIVERSE_EXIT"))
    t = trades(eng)
    early = t[(t.exit_reason == "UNIVERSE_REMOVAL") & (pd.to_datetime(t.exit_date) <= d30)]
    assert early.empty
    # the call written before removal (day 20) is legitimate; it is closed only after the effective date
    assert t[(t.symbol == "AAA") & (t.instrument == "CE")].iloc[0].date < d30
    # the universe was only ever asked about the day being processed
    assert all(asked <= day for day, asked in eng._steps)


# ---------------------------------------------------------------- data unavailable: flagged, kept, retried
def test_missing_quote_keeps_position_and_flags_it(tmp_path, costs, params):
    crash = [1000.0] * 5 + [600.0] * 55                              # 950 put leaves the listed strike range (<= 1.3·S)
    md = synth.build(tmp_path, {"AAA": crash})
    u = Universe(md, removed_from=8)
    eng = run(md, costs, params, u, end_day=30, max_margin_utilization=10)
    ex = exits(eng)
    unavailable = ex[ex.data_unavailable != ""]
    assert len(unavailable) >= 1 and unavailable.iloc[0].residual           # not silently removed
    assert len(events(eng, "UNIVERSE_EXIT_DATA_UNAVAILABLE")) >= 1
    t = trades(eng)
    put = t[(t.symbol == "AAA") & (t.instrument == "PE")].iloc[0]
    assert put.status == "assigned" and put.exit_reason == "CONTRACT_EXPIRY"   # held to expiry, settled by the exchange
    stock = t[(t.symbol == "AAA") & (t.instrument == "STOCK")].iloc[0]
    assert stock.exit_reason == "UNIVERSE_REMOVAL"                            # then the shares were cleared
    assert not ex.iloc[-1].residual and eng.wheels["AAA"].shares == 0
    assert t[(t.symbol == "AAA") & (t.instrument == "CE")].empty
    a, b = pnl_reconciles(eng)
    assert a == pytest.approx(b, abs=1.0)


# ---------------------------------------------------------------- re-inclusion
def test_reentry_only_after_historical_reinclusion(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT})
    u = Universe(md, removed_from=10, rejoin=30)
    eng = run(md, costs, params, u)
    days = md.trading_days
    t = trades(eng)
    puts = t[(t.symbol == "AAA") & (t.instrument == "PE")].sort_values("date")
    assert list(pd.to_datetime(puts.date)) == [days[1], days[40]]     # original, then the first expiry after rejoining
    assert not ((pd.to_datetime(puts.date) > days[10]) & (pd.to_datetime(puts.date) < days[30])).any()


# ---------------------------------------------------------------- distinct exit reasons
def test_exit_reasons_distinguish_expiry_corporate_action_margin_and_universe(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT})
    eng = run(md, costs, params, Universe(md, removed_from=45))
    reasons = set(trades(eng).exit_reason.dropna())
    assert "CONTRACT_EXPIRY" in reasons and "UNIVERSE_REMOVAL" in reasons

    p = [1000.0] * 5 + [930.0, 880.0, 850.0] + [850.0] * 52
    md2 = synth.build(tmp_path / "m", {"AAA": p})
    eng2 = run(md2, costs, params, Universe(md2, removed_from=59), end_day=15, leverage=5.0)
    assert trades(eng2).iloc[0].exit_reason == "MARGIN_CALL"

    from nse.wheel.engine import exit_reason
    assert exit_reason("terminated_assigned") == "CORPORATE_ACTION"
    assert exit_reason("expired_otm") == "CONTRACT_EXPIRY"


def test_no_universe_means_no_forced_exits(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": FLAT})
    eng = WheelEngine(md, costs, synth.cfg(params), lambda t: ["AAA"])
    for d in md.trading_days:
        eng.step(d)
    assert eng.universe_exits == [] and "UNIVERSE_REMOVAL" not in set(trades(eng).exit_reason.dropna())
