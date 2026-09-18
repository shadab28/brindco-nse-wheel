"""Stock selection for each wheel entry day.

ranked_selection: the top `n` names of data/signals/expiry_rankings.csv with rank_final > threshold, from the
latest ranking dated on or before the decision day. Rankings are computed at an expiry's close from data up to
that close (scripts/wheel/expiry_rankings.py) and cover only names in the NIFTY 50 on that date, so the pool is
point-in-time (no survivorship) and the decision never reads a later ranking (no look-ahead).

frozen_selection: the rulebook's frozen baseline universe, alphabetical, no replacements.
"""
from __future__ import annotations

import pandas as pd

from nse.wheel.data import RANK_TO_DB, db_symbol

# warehouse ticker -> index ticker on a date (same-ISIN renames); TATAMOTORS is TMPV only before the demerger rename
DB_TO_INDEX = {"ZOMATO": "ETERNAL", "TATAGLOBAL": "TATACONSUM", "LTI": "LTIM"}


class MembershipUniverse:
    """Historical NIFTY 50 membership as of each date, from dated effective_from / effective_to periods
    (data/universe/nifty50_membership_validated.csv). Only the period covering the asked date is read, so a
    removal is known from its effective date and never earlier."""

    def __init__(self, periods: pd.DataFrame):
        p = periods.copy()
        p["effective_from"] = pd.to_datetime(p.effective_from)
        p["effective_to"] = pd.to_datetime(p.effective_to)
        self.p = {s: g[["effective_from", "effective_to"]].to_numpy() for s, g in p.groupby("symbol")}
        self.queries: list = []             # dates asked, for look-ahead tests

    def index_symbol(self, sym: str, t) -> str:
        for rs, dbs, until in RANK_TO_DB:
            if sym == dbs and pd.Timestamp(t) < pd.Timestamp(until):
                return rs
        return DB_TO_INDEX.get(sym, sym)

    def __call__(self, sym: str, t) -> bool:
        t = pd.Timestamp(t)
        self.queries.append(t)
        for a, b in self.p.get(self.index_symbol(sym, t), []):
            if (pd.isna(a) or a <= t) and (pd.isna(b) or t <= b):
                return True
        return False


class RankedSelection:
    def __init__(self, rankings: pd.DataFrame, n: int, threshold: float, priority: str = "rank"):
        r = rankings.copy()
        r["expiry_date"] = pd.to_datetime(r.expiry_date)
        self.r = r.sort_values(["expiry_date", "rank"])
        self.dates = sorted(self.r.expiry_date.unique())
        self.n, self.threshold, self.priority = n, threshold, priority
        self.log: list[dict] = []

    def ranking_date(self, t) -> pd.Timestamp | None:
        t = pd.Timestamp(t)
        eligible = [d for d in self.dates if d <= t]
        return eligible[-1] if eligible else None

    def __call__(self, t) -> list[str]:
        d = self.ranking_date(t)
        if d is None:
            return []
        day = self.r[(self.r.expiry_date == d) & (self.r.rank_final > self.threshold)].head(self.n)
        syms = [db_symbol(s, t) for s in day.symbol]
        if self.priority == "alphabetical":
            syms = sorted(syms)
        self.log.append({"decision_date": pd.Timestamp(t), "ranking_date": d, "selected": ",".join(syms),
                         "n_selected": len(syms)})
        return syms


class FrozenSelection:
    def __init__(self, names: list[str]):
        self.names = sorted(names)
        self.log: list[dict] = []

    def __call__(self, t) -> list[str]:
        self.log.append({"decision_date": pd.Timestamp(t), "ranking_date": None,
                         "selected": ",".join(self.names), "n_selected": len(self.names)})
        return list(self.names)
