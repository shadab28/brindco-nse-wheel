"""Phase 1 data validation: point-in-time NIFTY 50 membership from NSE Indices press releases."""
import random

import pandas as pd
import pytest

from data_validation import universe as U


@pytest.fixture(scope="module")
def built():
    return U.build()


def test_rollback_has_no_errors_and_matches_anchor(built):
    per, ev, errors = built
    assert errors == []
    anchor = {U.CANONICAL.get(s, s) for s in pd.read_csv(U.ANCHOR_FILE).Symbol.str.strip()}
    assert U.members_on(U.ANCHOR_DATE) == anchor and len(anchor) == 50


@pytest.mark.parametrize("symbol,day,expected", [
    ("YESBANK", "2020-03-18", True), ("YESBANK", "2020-03-19", False), ("SHREECEM", "2020-03-19", True),
    ("HDFC", "2023-07-12", True), ("HDFC", "2023-07-13", False), ("LTIM", "2023-07-13", True),
    ("LTIM", "2024-09-27", True), ("LTIM", "2024-09-30", False), ("TRENT", "2024-09-27", False),
    ("JIOFIN", "2023-07-20", True), ("JIOFIN", "2023-09-07", False), ("JIOFIN", "2025-03-28", True),
    ("ZOMATO", "2025-03-28", True), ("ETERNAL", "2025-03-27", False),
    ("INDIGO", "2025-09-29", False), ("INDIGO", "2025-09-30", True), ("HEROMOTOCO", "2025-09-30", False),
    ("BSE", "2026-09-16", False), ("WIPRO", "2026-09-16", True),
])
def test_is_nifty50_known_changes(symbol, day, expected):
    assert U.is_nifty50(symbol, day) is expected


def test_current_constituents_not_applied_retrospectively():
    joined = {"INDIGO": "2025-09-30", "MAXHEALTH": "2025-09-30", "BEL": "2024-09-30", "TRENT": "2024-09-30",
              "SHRIRAMFIN": "2024-03-28", "ADANIENT": "2022-09-30", "APOLLOHOSP": "2022-03-31"}
    rng = random.Random(1)
    for s, d in joined.items():
        for _ in range(20):
            before = pd.Timestamp("2019-10-01") + pd.Timedelta(days=rng.randrange((pd.Timestamp(d) - pd.Timestamp("2019-10-01")).days))
            assert not U.is_nifty50(s, before), (s, before)


def test_member_count_on_random_dates():
    rng = random.Random(2)
    for _ in range(200):
        d = pd.Timestamp("2019-10-01") + pd.Timedelta(days=rng.randrange(2500))
        assert len(U.members_on(d)) in (50, 51)
