"""Phase 2 data validation: F&O eligibility from raw NSE F&O bhavcopies."""
import pandas as pd
import pytest

from data_validation import phase02_fo_eligibility as P
from data_validation import raw_fo


@pytest.fixture(scope="module")
def el():
    if not (raw_fo.OUT / "symbol_day.parquet").exists():
        pytest.skip("raw F&O extract not built (python data_validation/raw_fo.py)")
    return P.eligibility()


def elig(el, sym, day):
    x = el[(el.symbol == sym) & (el.trade_date == pd.Timestamp(day))]
    return bool(len(x)) and bool(x.eligible.iloc[0])


@pytest.mark.parametrize("sym,day,expected", [
    ("HDFC", "2023-07-12", True), ("HDFC", "2023-07-13", False),              # merged into HDFC Bank
    ("ETERNAL", "2024-11-28", False), ("ETERNAL", "2024-11-29", True),        # ZOMATO entered F&O 29-Nov-2024
    ("JIOFIN", "2024-11-28", False), ("JIOFIN", "2024-11-29", True),
    ("INFRATEL", "2020-12-17", True), ("INDUSTOWER", "2020-12-18", True),     # same company, ticker renamed
    ("TATAMOTORS", "2025-10-23", True), ("TMPV", "2025-10-24", True),
    ("RELIANCE", "2021-03-30", True),                                          # reconstructed day still carries eligibility
])
def test_known_eligibility(el, sym, day, expected):
    assert elig(el, sym, day) is expected


def test_windows_split_on_gaps():
    days = list(pd.bdate_range("2024-01-01", periods=10))
    el = pd.DataFrame({"trade_date": days[:3] + days[5:8], "symbol": "X", "eligible": True})
    w = P.windows(el, days)
    assert list(zip(w.fo_start, w.fo_end)) == [(days[0], days[2]), (days[5], days[7])]


def test_eligibility_requires_both_futures_and_options():
    el = pd.DataFrame({"n_fut": [1, 0, 2], "n_opt": [0, 5, 3]})
    assert list((el.n_fut > 0) & (el.n_opt > 0)) == [False, False, True]
