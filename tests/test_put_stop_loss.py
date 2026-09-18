"""Put-phase stop loss: a short put whose underlying closes `put_stop_loss_pct` below the spot on the day the
put was sold is bought back at that day's close, ending the cycle before any delivery can happen.

Threshold chosen as 0.15 on 2026-09-18 from scripts/wheel/stop_loss_probe.py; see docs/STOP_LOSS.md for why
tighter stops destroy value. The rule is off when `put_stop_loss_pct` is null, so pre-rule runs reproduce.
"""
import pandas as pd
import pytest

from nse.wheel.engine import WheelEngine
from tests import synth
from tests.test_stage2_engine import FLAT, pnl_reconciles

# Put is decided day 0 and filled day 1, so the reference spot is day 1's close (1000) and the stop is 850.
BREACH = [1000.0] * 6 + [840.0] * 54          # closes 16% down on day 6, well before the day-19 expiry
NEAR_MISS = [1000.0] * 6 + [851.0] * 54       # 14.9% down: never breaches
RECOVER = [1000.0] * 6 + [840.0] * 4 + [1000.0] * 50   # breaches day 6, recovers by day 10


def run(md, costs, params, sel=("AAA",), end_day=None, **over):
    eng = WheelEngine(md, costs, synth.cfg(params, **over), lambda t: list(sel))
    end = md.trading_days[end_day] if end_day is not None else md.trading_days[-1]
    for d in [d for d in md.trading_days if d <= end]:
        eng.step(d)
    return eng


def trades(eng):
    return pd.DataFrame(eng.trades + eng.open_positions_rows(eng.daily[-1]["date"]))


def puts(eng):
    t = trades(eng)
    return t[t.instrument == "PE"]


# ---------------------------------------------------------------- the trigger
def test_put_is_bought_back_on_the_close_that_breaches_the_stop(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": BREACH})
    eng = run(md, costs, params, end_day=18, put_stop_loss_pct=0.15)
    p = puts(eng)
    assert len(p) == 1
    row = p.iloc[0]
    assert row.status == "stop_loss"
    assert row.exit_date == md.trading_days[6]          # same day as the breaching close, not T+1
    assert row.exit_reason == "STOP_LOSS"


def test_stop_does_not_fire_when_the_close_stays_above_the_level(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": NEAR_MISS})
    eng = run(md, costs, params, end_day=18, put_stop_loss_pct=0.15)
    row = puts(eng).iloc[0]
    assert row.status != "stop_loss"
    assert row.exit_date == md.trading_days[19 - 1] or row.status == "open_mtm"


def test_null_threshold_leaves_the_engine_unchanged(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": BREACH})
    off = run(md, costs, params, end_day=18, put_stop_loss_pct=None)
    assert "stop_loss" not in set(puts(off).status)


def test_a_put_that_would_have_recovered_is_still_stopped(tmp_path, costs, params):
    """The rule is mechanical: it does not know the path recovers. This is the cost the probe measured."""
    md = synth.build(tmp_path, {"AAA": RECOVER})
    eng = run(md, costs, params, end_day=18, put_stop_loss_pct=0.15)
    assert puts(eng).iloc[0].status == "stop_loss"


# ---------------------------------------------------------------- cycle bookkeeping
def test_stopped_cycle_ends_with_no_shares_and_is_marked(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": BREACH})
    eng = run(md, costs, params, end_day=18, put_stop_loss_pct=0.15)
    c = pd.DataFrame([vars(x) for x in eng.cycles.values()]).iloc[0]
    assert c.status == "stop_loss"
    assert c.end == md.trading_days[6]
    assert bool(c.stopped_out)
    assert c.stop_level == pytest.approx(850.0)
    assert c.shares_assigned == 0 and not c.assigned


def test_no_stock_is_ever_delivered_on_a_stopped_cycle(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": BREACH})
    eng = run(md, costs, params, end_day=18, put_stop_loss_pct=0.15)
    assert trades(eng).instrument.eq("STOCK").sum() == 0


def test_symbol_is_eligible_again_at_the_next_expiry(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": BREACH})
    eng = run(md, costs, params, end_day=25, put_stop_loss_pct=0.15)
    p = puts(eng)
    assert len(p) == 2                                          # stopped day 6, re-sold after the day-19 expiry
    assert p.iloc[0].status == "stop_loss"
    assert pd.Timestamp(p.iloc[1].date) > md.trading_days[19]


def test_pnl_reconciles_after_a_stop(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": BREACH})
    eng = run(md, costs, params, end_day=25, put_stop_loss_pct=0.15)
    nav_change, from_trades = pnl_reconciles(eng)
    assert nav_change == pytest.approx(from_trades, abs=1.0)


def test_stop_cancels_a_pending_order_for_the_same_symbol(tmp_path, costs, params):
    """A put decided yesterday must not fill into a name that stopped out today."""
    md = synth.build(tmp_path, {"AAA": BREACH})
    eng = run(md, costs, params, end_day=18, put_stop_loss_pct=0.15)
    assert not [o for o in eng.orders if o.symbol == "AAA" and o.kind == "PUT"
                and o.decided_on <= md.trading_days[6]]


def test_engine_counts_the_stop(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": BREACH})
    eng = run(md, costs, params, end_day=18, put_stop_loss_pct=0.15)
    assert eng.diag["put_stop_losses"] == 1
    ev = pd.DataFrame(eng.events)
    assert (ev.event == "PUT_STOP_LOSS").sum() == 1
