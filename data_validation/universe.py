"""Point-in-time NIFTY 50 membership rebuilt from NSE Indices press releases (primary source).

Sources (saved under data_validation/sources/niftyindices/):
    ind_prs*.pdf / .txt                  every replacement, exclusion and corporate-adjustment release, 2016-01..2026-09
    press_release_index.csv              release date, file, title, URL
    ind_nifty50list_2026-09-17.csv       official constituent list downloaded on 2026-09-17 (anchor)

Method: parse every NIFTY 50 change from the releases, then start from the anchor list and undo each change
effective on or before the anchor date, newest first. Spin-off entities that NSE Indices added at zero price
(JIOFIN 2023, ITC Hotels 2025, TML CV 2025, Kwality Wall's 2025) are included for the days they were members.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache

import pandas as pd

from data_validation.common import ROOT

SRC = ROOT / "data_validation" / "sources" / "niftyindices"
ANCHOR_FILE = SRC / "ind_nifty50list_2026-09-17.csv"
ANCHOR_DATE = date(2026, 9, 17)
OUT = ROOT / "data_validation" / "output"

# ticker printed in a release -> today's canonical ticker (same ISIN renames only)
CANONICAL = {"ZOMATO": "ETERNAL", "TATAGLOBAL": "TATACONSUM", "LTI": "LTIM", "TATAMOTORS": "TMPV"}
# spin-offs NSE Indices added before listing: exclusion release ticker for the entity
SPINOFF_TICKER = {"RELIANCE": "JIOFIN", "ITC": "ITCHOTELS", "TATAMOTORS": "TMCV", "HINDUNILVR": "KWIL"}

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE = rf"({MONTHS})\s+(\d{{1,2}})\s*,\s*(\d{{4}})"


def _d(m) -> date:
    return datetime.strptime(f"{m[0]} {int(m[1])} {m[2]}", "%B %d %Y").date()


def _clean(txt: str) -> str:
    # pypdf splits digits in dates ("202 5", "0 5"); join them back
    txt = re.sub(r"(?<=\d) (?=\d)", "", txt)
    return re.sub(r"[ \t]+", " ", txt)


@dataclass
class Event:
    effective: date
    symbol: str
    action: str           # add | remove
    kind: str             # review | event | spinoff_in | spinoff_out
    release_date: date
    release: str
    evidence: str


def parse_releases() -> list[Event]:
    idx = pd.read_csv(SRC / "press_release_index.csv", parse_dates=["release_date"])
    events: list[Event] = []
    for r in idx.itertuples():
        p = SRC / (r.file[:-4] + ".txt")
        if not p.exists():
            continue
        txt = _clean(p.read_text())
        lines = txt.splitlines()
        rel = r.release_date.date()
        # ---- replacement sections "1) Nifty 50" / "a) NIFTY 50"
        for i, ln in enumerate(lines):
            if not re.match(r"^\s*(\d+|[a-z])\)\s*nifty\s*50(\s+index)?\s*$", ln, re.I):
                continue
            before = "\n".join(lines[:i])
            eff = re.findall(rf"effective\s+from\s+(?:\w+,\s*)?{DATE_RE}", before, re.I) or \
                re.findall(rf"w\.?e\.?f\.?\s+(?:\w+,\s*)?{DATE_RE}", before + " " + r.title, re.I)
            if not eff:
                raise ValueError(f"{r.file}: NIFTY 50 section without an effective date")
            effective = _d(eff[-1])
            mode = None
            for ln2 in lines[i + 1:]:
                if re.match(r"^\s*(\d+|[a-z])\)\s", ln2) or re.match(r"^\s*[A-Z]\.\s", ln2):
                    break
                if re.search(r"being\s+excluded", ln2, re.I):
                    mode = "remove"
                elif re.search(r"being\s+included", ln2, re.I):
                    mode = "add"
                elif mode and (m := re.match(r"^\s*\d+\s+.+\s([A-Z0-9&\-]{2,20})\s*$", ln2)):
                    sym = CANONICAL.get(m.group(1), m.group(1))
                    events.append(Event(effective, sym, mode, "review" if "review" in txt.lower() else "event",
                                        rel, r.file, ln2.strip()))
        # ---- spin-off inclusion / exclusion lists ("Sr. No. Index Name" ... "1 Nifty 50")
        in_list = re.search(r"Index Name\s*\n\s*\d+\s+Nifty 50\s*\n", txt, re.I)
        if not in_list:
            continue
        eff = re.findall(rf"effective\s+from\s+{DATE_RE}", txt, re.I)
        if re.search(r"^\s*Corporate (Action )?Adjustment for", r.title, re.I) and re.search(r"spun.off|demerged entity", txt, re.I):
            parent = re.search(r"\(([A-Z0-9&\-]+)\)", txt).group(1)
            events.append(Event(_d(eff[-1]), SPINOFF_TICKER[parent], "add", "spinoff_in", rel, r.file,
                                f"spun-off entity of {parent} included at zero price"))
        elif re.search(r"^\s*Exclusion of", r.title, re.I):
            # every exclusion release names the listed entity: "... Ltd. (TMCV) was listed on" / "JIOFIN was listed on"
            m = re.search(r"\(\s*([A-Z0-9&\-]{2,20})\s*\)\s*was\s+listed", txt) or re.search(r"\b([A-Z0-9&\-]{2,20})\s+was\s+listed", txt)
            if not m:
                raise ValueError(f"{r.file}: no excluded ticker found")
            tick = m.group(1)
            events.append(Event(_d(eff[-1]), tick, "remove", "spinoff_out", rel, r.file,
                                f"{tick} excluded after listing"))
    # a later release supersedes an earlier one for the same symbol/action (e.g. Yes Bank brought forward)
    events.sort(key=lambda e: (e.symbol, e.action, e.effective, e.release_date))
    kept: list[Event] = []
    for e in events:
        prev = kept[-1] if kept else None
        if prev and prev.symbol == e.symbol and prev.action == e.action and abs((prev.effective - e.effective).days) <= 120:
            if e.release_date >= prev.release_date:
                kept[-1] = e
            continue
        kept.append(e)
    return sorted(kept, key=lambda e: (e.effective, e.action, e.symbol))


@lru_cache(maxsize=1)
def build() -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """(membership periods, change events, rollback errors). Periods: symbol, effective_from, effective_to (inclusive,
    None = still a member on the anchor date), source releases."""
    events = [e for e in parse_releases() if e.effective <= ANCHOR_DATE]
    anchor = pd.read_csv(ANCHOR_FILE)
    members = set(CANONICAL.get(s, s) for s in anchor.Symbol.str.strip())
    errors = []
    open_since: dict[str, tuple[date | None, str]] = {s: (None, "anchor list 2026-09-17") for s in members}
    periods = []
    by_date: dict[date, list[Event]] = {}
    for e in events:
        by_date.setdefault(e.effective, []).append(e)
    for eff in sorted(by_date, reverse=True):
        day = by_date[eff]
        for e in day:
            if e.action == "add":
                if e.symbol not in members:
                    errors.append(f"{eff}: {e.symbol} added but not a member afterwards ({e.release})")
                    continue
                start_src = open_since.pop(e.symbol)
                periods.append((e.symbol, eff, start_src[0], e.release, start_src[1]))
                members.discard(e.symbol)
        for e in day:
            if e.action == "remove":
                if e.symbol in members:
                    errors.append(f"{eff}: {e.symbol} removed but still a member afterwards ({e.release})")
                    continue
                members.add(e.symbol)
                open_since[e.symbol] = (eff - pd.Timedelta(days=1), e.release)
    for s, (end, src) in open_since.items():
        periods.append((s, None, end, "before window", src))
    per = pd.DataFrame(periods, columns=["symbol", "effective_from", "effective_to", "from_source", "to_source"])
    per["effective_from"] = pd.to_datetime(per.effective_from)
    per["effective_to"] = pd.to_datetime(per.effective_to)
    per = per.sort_values(["symbol", "effective_from"], na_position="first").reset_index(drop=True)
    ev = pd.DataFrame([e.__dict__ for e in events])
    return per, ev, errors


def is_nifty50(symbol: str, d) -> bool:
    per, _, _ = build()
    d = pd.Timestamp(d)
    s = CANONICAL.get(symbol, symbol)
    x = per[per.symbol == s]
    return bool(((x.effective_from.isna() | (x.effective_from <= d)) & (x.effective_to.isna() | (d <= x.effective_to))).any())


def members_on(d) -> set[str]:
    per, _, _ = build()
    d = pd.Timestamp(d)
    x = per[(per.effective_from.isna() | (per.effective_from <= d)) & (per.effective_to.isna() | (d <= per.effective_to))]
    return set(x.symbol)
