"""Point-in-time NIFTY 50 membership, rebuilt from dated index changes.

Changes are transcribed from the NSE Indices press releases saved under
data_validation/sources/niftyindices/ and cover 2016-04-01 onward.

NSE publishes only today's constituent list (ind_nifty50list.csv), so history is
reconstructed by starting from that live list and undoing each change below in
reverse date order. Every entry is taken from the NSE Indices announcement as
reported at the time; `effective` is the first trading day the change applies.

Tickers are today's canonical tickers. RENAMES maps them to the ticker actually
printed in the bhavcopy before a rename, so a daily list can be checked against
that day's file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
import requests

LIVE_LIST_URL = "https://archives.nseindia.com/content/indices/ind_nifty50list.csv"


@dataclass(frozen=True)
class Change:
    effective: date
    added: tuple[str, ...]
    removed: tuple[str, ...]
    reason: str


# Oldest first. Extend this list when NSE announces a new review.
CHANGES = [
    Change(date(2016, 4, 1), ("AUROPHARMA", "EICHERMOT", "INFRATEL", "TATAMTRDVR"),
           ("CAIRN", "PNB", "VEDL"),
           "Semi-annual review, Mar 2016 (ind_prs22022016_2.pdf). Tata Motors DVR joined as an "
           "additional security, so the index carried 51 names until 2017-09-29"),
    Change(date(2017, 3, 31), ("IBULHSGFIN", "IOC"), ("BHEL", "IDEA"),
           "Semi-annual review, Mar 2017 (ind_prs16022017.pdf)"),
    Change(date(2017, 5, 26), ("VEDL",), ("GRASIM",),
           "Aditya Birla Nuvo / Grasim scheme of arrangement (ind_prs27042017.pdf)"),
    Change(date(2017, 9, 29), ("BAJFINANCE", "HINDPETRO", "UPL"),
           ("ACC", "BANKBARODA", "TATAMTRDVR", "TATAPOWER"),
           "Semi-annual review, Sep 2017 (ind_prs28082017.pdf); back to 50 names"),
    Change(date(2018, 4, 2), ("BAJAJFINSV", "GRASIM", "TITAN"),
           ("AMBUJACEM", "AUROPHARMA", "BOSCHLTD"),
           "Semi-annual review, Mar 2018 (ind_prs21022018.pdf)"),
    Change(date(2018, 9, 28), ("JSWSTEEL",), ("LUPIN",),
           "Semi-annual review, Sep 2018 (ind_prs28082018.pdf)"),
    Change(date(2019, 3, 29), ("BRITANNIA",), ("HINDPETRO",),
           "Semi-annual review, Mar 2019 (ind_prs25022019.pdf)"),
    Change(date(2019, 9, 27), ("NESTLEIND",), ("IBULHSGFIN",),
           "Semi-annual review, Sep 2019 (ind_prs28082019.pdf)"),
    Change(date(2020, 3, 19), ("SHREECEM",), ("YESBANK",),
           "Yes Bank moratorium; exclusion brought forward from 27-Mar-2020"),
    Change(date(2020, 7, 31), ("HDFCLIFE",), ("VEDL",),
           "Vedanta proposed voluntary delisting"),
    Change(date(2020, 9, 25), ("DIVISLAB", "SBILIFE"), ("INFRATEL", "ZEEL"),
           "Semi-annual review, Sep 2020"),
    Change(date(2021, 3, 31), ("TATACONSUM",), ("GAIL",),
           "Semi-annual review, Mar 2021"),
    Change(date(2022, 3, 31), ("APOLLOHOSP",), ("IOC",),
           "Semi-annual review, Mar 2022"),
    Change(date(2022, 9, 30), ("ADANIENT",), ("SHREECEM",),
           "Semi-annual review, Sep 2022"),
    Change(date(2023, 7, 13), ("LTIM",), ("HDFC",),
           "HDFC merged into HDFC Bank"),
    Change(date(2024, 3, 28), ("SHRIRAMFIN",), ("UPL",),
           "Semi-annual review, Mar 2024"),
    Change(date(2024, 9, 30), ("BEL", "TRENT"), ("DIVISLAB", "LTIM"),
           "Semi-annual review, Sep 2024"),
    Change(date(2025, 3, 28), ("ETERNAL", "JIOFIN"), ("BPCL", "BRITANNIA"),
           "Semi-annual review, Mar 2025"),
    Change(date(2025, 9, 30), ("INDIGO", "MAXHEALTH"), ("HEROMOTOCO", "INDUSINDBK"),
           "Semi-annual review, Sep 2025"),
    # Mar 2026 semi-annual review: no NIFTY 50 changes.
    Change(date(2026, 9, 30), ("BSE",), ("WIPRO",),
           "Semi-annual review, Sep 2026"),
]

# canonical ticker -> older tickers printed in the bhavcopy, newest first
RENAMES = {
    "ETERNAL": ["ZOMATO"],
    "TATACONSUM": ["TATAGLOBAL"],
    "TMPV": ["TATAMOTORS"],       # same ISIN; renamed at the Oct 2025 demerger
    "LTIM": ["LTI"],              # L&T Infotech -> LTIMindtree, Nov 2022
}


def fetch_live_list() -> pd.DataFrame:
    r = requests.get(LIVE_LIST_URL, timeout=30,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    from io import StringIO
    df = pd.read_csv(StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]
    if len(df) != 50:
        raise ValueError(f"live NIFTY 50 list has {len(df)} rows, expected 50")
    return df


def membership_timeline(live: set[str], as_of: date) -> list[tuple[date, frozenset[str]]]:
    """[(from_date, members), ...] oldest first, anchored on `live` at `as_of`.

    Changes effective after `as_of` are announced but not yet applied, so they
    are not undone.
    """
    members = set(live)
    periods = []
    for ch in sorted((c for c in CHANGES if c.effective <= as_of),
                     key=lambda c: c.effective, reverse=True):
        periods.append((ch.effective, frozenset(members)))
        missing = set(ch.added) - members
        if missing:
            raise ValueError(f"{ch.effective}: added {missing} not in the index "
                             "at that point; change log and live list disagree")
        members = (members - set(ch.added)) | set(ch.removed)
        # The index normally holds 50 names. Between the Mar-2016 review and the
        # Sep-2017 one it held 51: NSE added Tata Motors DVR as an additional
        # security (ind_prs22022016_2.pdf says so in as many words).
        expected = {51} if date(2016, 4, 1) < ch.effective <= date(2017, 9, 29) else {50}
        if len(members) not in expected:
            raise ValueError(f"before {ch.effective}: {len(members)} members")
    periods.append((date.min, frozenset(members)))
    return sorted(periods)


def members_on(timeline: list[tuple[date, frozenset[str]]], d: date) -> frozenset[str]:
    current = timeline[0][1]
    for start, members in timeline:
        if start <= d:
            current = members
    return current
