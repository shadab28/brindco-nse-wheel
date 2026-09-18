"""Dividends in the holding leg: parsing NSE announcements, merging with F&O-detected actions, engine treatment."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nse.wheel import dividends as DIV
from tests import synth
from tests.test_stage2_engine import pnl_reconciles, run

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize("subject,amount,status", [
    ("Annual General Meeting/Special Dividend - Rs 8 Per Share /Dividend - Rs 20 Per Share", 28.0, "OK"),
    ("Dividend - Rs 3 Per Share & Special Dividend - Rs 3 Per Share", 6.0, "OK"),
    ("Interim Dividend - Rs 5.25 Per Share And Special Dividend - Rs 2 Per Share", 7.25, "OK"),
    ("Interim Dividend-Rs.1.75 Per Share", 1.75, "OK"),
    ("Dividend - Re 0.50 Per Share", 0.5, "OK"),
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", 0.0, "NONE"),
    ("Bonus 1:1", 0.0, "NONE"),
    ("Interim Dividend - Rs 8 Per Share Special Dividend - Rs 67 Per Share", 75.0, "OK"),   # no separator
    ("Annual General Meeting/Dividend - Rs 15 Per Share Special Dividend 15 Per Share", 30.0, "OK"),
    ("Dividend - Rs 12.50 Per Share Pursuant To Scheme", 12.5, "OK"),
    ("Dividend - Rs 10/- Per Share", 10.0, "OK"),
    ("Interim Dividend - Rs 25 Per Sh/Dividend - Rs 65 Per Share", 90.0, "OK"),     # truncated by NSE
    ("Interim Dividned - Rs 4 Per Share", 4.0, "OK"),                               # misspelled by NSE
    ("Interim Dividend - Rs 0.50 Per Hsare", 0.5, "OK"),
    ("Interim Dividend", 0.0, "UNPARSED"),
    ("Final Dividend - 150%", 0.0, "UNPARSED"),            # never guessed
])
def test_dividend_amount(subject, amount, status):
    assert DIV.dividend_amount(subject) == (amount, status)


# ---------------------------------------------------------------- merge
def _close(sym, days, px):
    return pd.DataFrame({sym: px}, index=pd.DatetimeIndex(days))


def _div(sym, day, amt):
    return pd.DataFrame([{"symbol": sym, "ex_date": pd.Timestamp(day), "dividend_per_share": amt,
                          "status": "OK", "traded_on_ex_date": True, "subject": "Dividend"}])


def test_dividend_on_contract_adjusted_day_is_paid_once():
    days = ["2023-01-13", "2023-01-16"]
    ca = pd.DataFrame([{"symbol": "TCS", "ex_date": pd.Timestamp(days[1]), "action_type": "value_adjustment",
                        "model": "shift", "a": 1.0, "b": -75.0, "share_multiplier": 1.0, "qty_multiplier": 1.0,
                        "distribution_per_share": np.nan}])
    out = DIV.merge(ca, _div("TCS", days[1], 75.0), _close("TCS", days, [3374.55, 3300.0]))
    assert len(out) == 1 and out.distribution_per_share.iloc[0] == pytest.approx(75.0)
    assert bool(out.contract_adjusted.iloc[0])


def test_sourced_dividend_floors_a_smaller_implied_distribution():
    days = ["2021-09-15", "2021-09-16"]
    ca = pd.DataFrame([{"symbol": "X", "ex_date": pd.Timestamp(days[1]), "action_type": "value_adjustment",
                        "model": "shift", "a": 1.0, "b": -58.0, "share_multiplier": 1.0, "qty_multiplier": 1.0,
                        "distribution_per_share": np.nan}])
    out = DIV.merge(ca, _div("X", days[1], 60.0), _close("X", days, [500.0, 440.0]))
    assert len(out) == 1 and out.distribution_per_share.iloc[0] == pytest.approx(60.0)


def test_manual_override_value_is_kept():
    days = ["2023-07-19", "2023-07-20"]
    ca = pd.DataFrame([{"symbol": "R", "ex_date": pd.Timestamp(days[1]), "action_type": "value_adjustment",
                        "model": "manual", "a": 1.0, "b": 0.0, "share_multiplier": 1.0, "qty_multiplier": 1.0,
                        "distribution_per_share": 261.85}])
    out = DIV.merge(ca, _div("R", days[1], 9.0), _close("R", days, [2800.0, 2500.0]))
    assert len(out) == 1 and out.distribution_per_share.iloc[0] == pytest.approx(261.85)


def test_ordinary_dividend_becomes_cash_only_row():
    days = ["2024-05-30", "2024-05-31"]
    out = DIV.merge(pd.DataFrame(columns=["symbol", "ex_date", "a", "b", "share_multiplier", "qty_multiplier"]),
                    _div("INFY", days[1], 28.0), _close("INFY", days, [1400.0, 1380.0]))
    assert len(out) == 1
    r = out.iloc[0]
    assert r.action_type == "dividend" and not r.contract_adjusted and r.distribution_per_share == 28.0


# ---------------------------------------------------------------- engine
PATH = [1000.0] * 19 + [900.0] * 5 + [880.0] * 36        # put assigned day 19; ex-dividend Rs 20 on day 24


def _div_row(md, day, amt):
    return pd.DataFrame([{"symbol": "AAA", "ex_date": md.trading_days[day], "action_type": "dividend",
                          "model": "sourced", "a": 1.0, "b": 0.0, "share_multiplier": 1.0, "qty_multiplier": 1.0,
                          "distribution_per_share": amt, "contract_adjusted": False}])


def test_dividend_credited_once_to_held_shares_and_nothing_else_changes(tmp_path, costs, params):
    md = synth.build(tmp_path / "a", {"AAA": PATH})
    base = run(md, costs, params, end_day=45)
    eng = run(md, costs, params, end_day=45, ca=_div_row(md, 24, 20.0))
    held = next(r for r in eng.events if r["event"] == "dividend")["shares"]
    divs = [x for x in eng.ledger if x[2] == "dividend"]
    assert held > 0 and len(divs) == 1 and divs[0][3] == pytest.approx(20.0 * held)
    # same decisions: identical option trades, NAV higher by exactly the dividend
    cols = ["date", "instrument", "strike", "quantity", "status"]
    assert pd.DataFrame(base.trades)[cols].equals(pd.DataFrame(eng.trades)[cols])
    assert eng.nav() - base.nav() == pytest.approx(20.0 * held, abs=1.0)
    a, b = pnl_reconciles(eng)
    assert a == pytest.approx(b, abs=1.0)                 # trade P&L carries the dividend via the lowered entry price


def test_no_dividend_without_shares(tmp_path, costs, params):
    md = synth.build(tmp_path, {"AAA": [1000.0] * 60})    # put never assigned
    eng = run(md, costs, params, end_day=30, ca=_div_row(md, 10, 20.0))
    assert not [x for x in eng.ledger if x[2] == "dividend"]


# ---------------------------------------------------------------- real data
DIV_FILE = ROOT / "data/reference/dividends.csv"


@pytest.mark.skipif(not DIV_FILE.exists(), reason="dividends.csv not built")
@pytest.mark.parametrize("syms,date,amount", [
    (("INFY",), "2024-05-31", 28.0),                  # Rs 20 final + Rs 8 special
    (("TMPV", "TATAMOTORS"), "2024-06-11", 6.0),      # Rs 3 + Rs 3 special
    (("TCS",), "2023-01-16", 75.0),                   # Rs 8 interim + Rs 67 special (F&O adjusted by 75)
])
def test_known_dividends(syms, date, amount):
    d = pd.read_csv(DIV_FILE, parse_dates=["ex_date"])
    hit = d[d.symbol.isin(syms) & (d.ex_date == date) & d.traded_on_ex_date]
    assert len(hit) >= 1 and (hit.dividend_per_share == amount).all()


@pytest.mark.skipif(not DIV_FILE.exists(), reason="dividends.csv not built")
def test_detected_extraordinary_dividends_agree_with_announcements(detected_ca):
    """Every F&O strike shift should be backed by an announced dividend of that size (rights issues excluded:
    NSE can apply those as a shift too, e.g. GRASIM 2024-01-10)."""
    d = pd.read_csv(DIV_FILE, parse_dates=["ex_date"])
    ann = pd.read_csv(ROOT / "data/reference/nse_corporate_announcements.csv", parse_dates=["ex_date"])
    rights = set(zip(ann.loc[ann.subject.str.contains("rights", case=False), "folder_symbol"],
                     ann.loc[ann.subject.str.contains("rights", case=False), "ex_date"]))
    shifts = detected_ca[detected_ca.accepted & (detected_ca.model == "shift")]
    shifts = shifts[[k not in rights for k in zip(shifts.symbol, shifts.ex_date)]]
    m = shifts.merge(d, on=["symbol", "ex_date"], how="left")
    missing = m[m.dividend_per_share.isna()]
    assert missing.empty, missing[["symbol", "ex_date", "b"]].to_string()
    off = m[(m.dividend_per_share + m.b).abs() > 0.02 * m.dividend_per_share.clip(lower=1)]
    assert off.empty, off[["symbol", "ex_date", "b", "dividend_per_share"]].to_string()
