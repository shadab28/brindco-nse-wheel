"""Stage 1 — data: costs, slippage, expiry calendar, lot sizes, corporate actions, universe, index."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nse.wheel.selection import RankedSelection

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "data" / "reference"     # verified reference tables, kept in this project


# ---------------------------------------------------------------- costs
def test_option_sale_worked_example(costs):
    # rulebook 11b: premium turnover 1,00,000 on 2025-01-15
    c = costs.option_sale("2025-01-15", 100_000)
    assert c.exchange == pytest.approx(35.53, abs=0.005)
    assert c.sebi == pytest.approx(0.10, abs=0.005)
    assert c.gst == pytest.approx(0.18 * (20 + 35.53 + 0.10), abs=0.01)
    assert c.stt == pytest.approx(100.0)
    assert c.brokerage == 20.0 and c.stamp == 0.0


@pytest.mark.parametrize("date,rate", [("2020-01-02", 0.0005), ("2023-04-03", 0.000625),
                                       ("2024-10-01", 0.001), ("2026-04-01", 0.0015)])
def test_option_stt_is_date_effective(costs, date, rate):
    assert costs.rate("Option sale", "STT", date) == rate


def test_sebi_fee_covid_relief(costs):
    assert costs.rate("Option sale", "SEBI turnover fee", "2020-05-29") == 1e-6
    assert costs.rate("Option sale", "SEBI turnover fee", "2020-06-01") == 5e-7
    assert costs.rate("Option sale", "SEBI turnover fee", "2021-04-01") == 1e-6


def test_assignment_paths_use_max_strike_fsp_and_no_exchange_charge(costs):
    a = costs.put_assignment("2022-06-30", 100.0, 90.0, 1000)       # delivery value = 1,00,000
    assert a.stt == pytest.approx(100.0) and a.brokerage == pytest.approx(250.0)
    assert a.stamp == pytest.approx(15.0) and a.exchange == 0.0 and a.dp == 0.0
    c = costs.call_away("2022-06-30", 100.0, 110.0, 1000)           # delivery value = 1,10,000
    assert c.stt == pytest.approx(110.0) and c.dp == pytest.approx(15.34) and c.stamp == 0.0


def test_slippage_rules(costs):
    assert costs.option_fill("2022-01-03", 10.0, "sell", 500) == pytest.approx(9.75)   # 2.5% > 2 ticks
    assert costs.option_fill("2022-01-03", 1.0, "sell", 500) == pytest.approx(0.90)    # 2 ticks > 2.5%
    assert costs.option_fill("2022-01-03", 0.08, "sell", 500) is None                 # below one tick
    assert costs.option_fill("2022-01-03", 10.0, "buy", 500) == pytest.approx(10.25)
    assert costs.tick("2025-11-03", 200) == 0.01 and costs.tick("2025-11-03", 300) == 0.05
    assert costs.tick("2025-10-31", 200) == 0.05


# ---------------------------------------------------------------- expiry calendar
def test_expiries_come_from_contract_data(md):
    cal = md.calendar
    assert len(cal) == 82 and cal.expiry_date.is_monotonic_increasing and cal.contract_month.is_unique
    assert md.expiry_of["2023-06"] == pd.Timestamp("2023-06-28")        # quoted 06-29, settled 06-28
    assert (cal.expiry_date.dt.dayofweek < 5).all()
    assert set(cal.expiry_date) <= set(md.trading_days)
    # the last day any monthly option of that contract trades is the calendar expiry (RELIANCE, full chain)
    for cm, exp in list(md.expiry_of.items())[2:-1]:
        days = [d for d in md.trading_days if md.chain(d, "RELIANCE", cm, "PE").shape[0] > 0]
        assert days and days[-1] == exp, cm


def test_fsp_is_cm_close_for_every_fo_listed_expiry(md):
    # NSE settles stock options on the underlying's CM closing price; the expiring future's settle is a check only
    missing = [(d, s) for d in md.expiry_dates for s in md.close.columns
               if (d, s) in md.fo_listed and (d, s) not in md.fsp and d >= pd.Timestamp("2019-12-01")]
    assert missing == []
    for (d, s), v in list(md.fsp.items())[:500]:
        assert v == md.spot(d, s)


def test_contract_after_is_strictly_after(md):
    assert md.contract_after("2020-01-30") == "2020-02"      # Jan 2020 expiry day itself
    assert md.contract_after("2020-01-29") == "2020-01"


# ---------------------------------------------------------------- lot sizes
def test_lot_revision_applies_per_series(md):
    # README: TCS in Jun 2020 carries 250 on the June series and 300 on July
    assert md.lot_size("2020-06-01", "TCS", "2020-06") == 250
    assert md.lot_size("2020-06-01", "TCS", "2020-07") == 300


def test_lot_after_bonus(md):
    assert md.lot_size("2024-10-25", "RELIANCE", "2024-11") == 250
    assert md.lot_size("2024-10-28", "RELIANCE", "2024-11") == 500


@pytest.mark.skipif(not (BASELINE / "verified_lot_sizes.csv").exists(), reason="baseline reference absent")
def test_lots_match_baseline_reference(md):
    ref = pd.read_csv(BASELINE / "verified_lot_sizes.csv", parse_dates=["effective_from_expiry"])
    checked = bad = 0
    for r in ref.itertuples():
        cm = r.effective_from_expiry.strftime("%Y-%m")
        if cm not in md.expiry_of:
            continue
        exp = md.expiry_of[cm]
        prior = [d for d in md.trading_days if d <= exp][-5]
        got = md.lot_size(prior, r.symbol, cm)
        if got is None:
            continue
        checked += 1
        bad += int(got != r.lot_size)
    assert checked >= 20 and bad == 0, f"{bad}/{checked} lot mismatches vs baseline reference"


# ---------------------------------------------------------------- corporate actions
KNOWN = [("HCLTECH", "2019-12-05", 2.0), ("EICHERMOT", "2020-08-24", 10.0), ("TATASTEEL", "2022-07-28", 10.0),
         ("RELIANCE", "2024-10-28", 2.0), ("BAJFINANCE", "2025-06-16", 10.0), ("NESTLEIND", "2024-01-05", 10.0),
         ("POWERGRID", "2023-09-12", 4 / 3), ("HDFCBANK", "2025-08-26", 2.0), ("KOTAKBANK", "2026-01-14", 5.0)]


@pytest.mark.parametrize("sym,date,mult", KNOWN)
def test_known_splits_and_bonuses(detected_ca, sym, date, mult):
    ev = detected_ca[(detected_ca.symbol == sym) & (detected_ca.ex_date == date) & detected_ca.accepted]
    assert len(ev) == 1 and ev.share_multiplier.iloc[0] == pytest.approx(mult, rel=1e-3)
    assert ev.a.iloc[0] == pytest.approx(1 / mult, rel=1e-3)


@pytest.mark.parametrize("sym,date,div", [("COALINDIA", "2021-12-06", 9.0), ("TCS", "2023-01-16", 75.0),
                                          ("BPCL", "2021-09-16", 58.0), ("BAJAJ-AUTO", "2025-06-20", 210.0)])
def test_extraordinary_dividends_shift_strikes(detected_ca, sym, date, div):
    ev = detected_ca[(detected_ca.symbol == sym) & (detected_ca.ex_date == date) & detected_ca.accepted]
    assert len(ev) == 1 and ev.model.iloc[0] == "shift" and ev.b.iloc[0] == pytest.approx(-div)


def test_crash_days_are_not_corporate_actions(detected_ca):
    acc = detected_ca[detected_ca.accepted]
    assert acc[acc.ex_date.isin(pd.to_datetime(["2020-03-23", "2023-02-02", "2024-01-23"]))].empty


@pytest.mark.skipif(not (BASELINE / "verified_corporate_actions.csv").exists(), reason="baseline reference absent")
def test_baseline_verified_splits_are_detected(detected_ca):
    ref = pd.read_csv(BASELINE / "verified_corporate_actions.csv", parse_dates=["ex_date"])
    ref = ref[ref.action_type.isin(["split", "bonus"])]
    acc = detected_ca[detected_ca.accepted]
    for r in ref.itertuples():
        hit = acc[(acc.symbol == r.symbol) & (acc.ex_date == r.ex_date)]
        assert len(hit) == 1 and hit.share_multiplier.iloc[0] == pytest.approx(r.adjustment_factor), r.symbol


# ---------------------------------------------------------------- universe and selection
@pytest.fixture(scope="module")
def rankings():
    return pd.read_csv(ROOT / "data/signals/expiry_rankings.csv", parse_dates=["expiry_date"])


def test_rankings_are_point_in_time_index_members(rankings):
    cons = pd.read_csv(ROOT / "data/universe/nifty50_constituents_daily.csv", parse_dates=["trade_date"])
    members = set(zip(cons.trade_date, cons.symbol))
    alias = {"TMPV": "TATAMOTORS"}
    miss = [(d, s) for d, s in zip(rankings.expiry_date, rankings.symbol)
            if (d, s) not in members and (d, alias.get(s, s)) not in members]
    assert len(miss) == 0, miss[:10]


def test_rankings_dates_are_expiries(rankings, md):
    assert set(rankings.expiry_date) <= set(md.expiry_dates)


def test_selection_has_no_lookahead_and_respects_threshold(rankings, params):
    sel = RankedSelection(rankings, params["n_positions"], params["rank_threshold"])
    for t in pd.to_datetime(["2019-12-31", "2020-03-26", "2020-03-27", "2023-07-12", "2025-10-28"]):
        names = sel(t)
        d = sel.log[-1]["ranking_date"]
        assert d <= t
        assert d == max(x for x in rankings.expiry_date.unique() if x <= t)
        day = rankings[rankings.expiry_date == d]
        assert len(names) <= params["n_positions"]
        assert (day.set_index("symbol").loc[[s if s != "TATAMOTORS" else "TMPV" for s in names]].rank_final
                > params["rank_threshold"]).all()


def test_selection_is_not_survivor_biased(rankings, params):
    sel = RankedSelection(rankings, params["n_positions"], params["rank_threshold"])
    ever = set()
    for d in sorted(rankings.expiry_date.unique()):
        ever |= set(sel(d))
    today = set(pd.read_csv(ROOT / "data/universe/nifty50_master_list.csv").query("currently_in_nifty50").symbol)
    assert ever - today - {"TATAMOTORS"}, "no removed index members were ever selected"


def test_tmpv_maps_to_tatamotors_before_rename(rankings, params):
    sel = RankedSelection(rankings, 50, -1e9)
    assert "TATAMOTORS" in sel("2024-03-28") and "TMPV" not in sel("2024-03-28")


# ---------------------------------------------------------------- index benchmark
def test_nifty_index_matches_futures_fsp(params):
    import psycopg2
    from nse.wheel.data import _dsn
    idx = pd.read_csv(ROOT / params["nifty_price_index_file"], parse_dates=["trade_date"]).set_index("trade_date").close
    with psycopg2.connect(_dsn()) as con:
        f = pd.read_sql("select trade_date, settle from nse.futures_eod where symbol='NIFTY' and trade_date = expiry",
                        con, parse_dates=["trade_date"])
    m = f.set_index("trade_date").settle.astype(float).to_frame().join(idx, how="inner")
    assert len(m) >= 80
    assert (m.close / m.settle - 1).abs().max() < 1e-4


def test_nifty_tri_benchmark_file(params):
    tri = pd.read_csv(ROOT / params["nifty_index_file"], parse_dates=["trade_date"]).set_index("trade_date")
    px = pd.read_csv(ROOT / params["nifty_price_index_file"], parse_dates=["trade_date"]).set_index("trade_date").close
    assert tri.source.str.contains("Total Return Index").all()
    assert tri.index.is_unique and tri.index.is_monotonic_increasing
    assert px.index.difference(tri.index).empty
    j = px.to_frame("px").join(tri.close.rename("tri"), how="inner")
    r = j.pct_change().dropna()
    assert (r.tri - r.px).abs().max() < 0.005               # dividends only ever add a few bps a day
    assert (r.tri - r.px).mean() > 0                        # TRI grows faster than the price index
    yrs = (j.index[-1] - j.index[0]).days / 365.25
    carry = (j.tri.iloc[-1] / j.tri.iloc[0]) ** (1 / yrs) - (j.px.iloc[-1] / j.px.iloc[0]) ** (1 / yrs)
    assert 0.008 < carry < 0.02                             # NIFTY 50 dividend yield is roughly 1-1.5%
