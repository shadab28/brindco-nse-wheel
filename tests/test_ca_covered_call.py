"""A contract adjustment that grows the option lot but not the shares (rights issue, value adjustment) must not
leave a covered call partly naked. Real case: ADANIENT rights, ex 2025-11-17, lot 300 -> 309, shares unchanged."""
from types import SimpleNamespace

import pandas as pd
import pytest

from nse.wheel.engine import CALL, OptionPos
from tests.test_cash_only_assignment import SYM, T, engine, hold_stock, market  # noqa: F401

EV = SimpleNamespace(symbol=SYM, contract_adjusted=True, action_type="value_adjustment", a=0.96948473, b=0.0,
                     share_multiplier=1.0, qty_multiplier=1.03, distribution_per_share=float("nan"))


def covered(eng, shares=1200, strike=1100.0):
    w = hold_stock(eng, SYM, shares, 1000.0)
    w.option = OptionPos(eng._next_tid(), w.cycle_id, SYM, CALL, "2022-02", pd.Timestamp("2022-02-24"), strike,
                         shares // 300, 300, shares, pd.Timestamp("2022-01-03"), 10.0, 10.0, 0.0, strike,
                         strike * shares, strike * shares / eng.L, 10.0)
    return w


def test_lot_growth_keeps_the_call_fully_covered(params, costs, market):
    eng = engine(params, costs, market, cash=1_000_000.0)
    w = covered(eng)
    eng.ca = {T: [EV]}
    nav_before = eng.nav()
    eng._corporate_actions(T)
    assert w.option.qty == 1236
    assert w.shares >= w.option.qty                         # no naked call
    assert eng.cycles[w.cycle_id].shares_open == w.shares   # share ledger follows
    assert eng.cash >= 0.0
    # buying the shortfall costs only charges and slippage, not value
    assert eng.nav() == pytest.approx(nav_before, rel=1e-3)
