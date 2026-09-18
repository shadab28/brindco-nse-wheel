#!/usr/bin/env python3
"""Write the NIFTY 50 constituent list for every trading day, plus a change log.

Outputs
-------
  data/universe/nifty50_constituents_daily.csv   one row per (trade_date, symbol): 50 per day
  data/universe/nifty50_changes.csv              one row per index change (added / removed)
  data/universe/nifty50_master_list.csv          every stock ever in the index in the window

Trading days come from the NSE cash bhavcopy (cached under data/raw/cm/, missing
days are downloaded). Each day's 50 names are checked against that day's
bhavcopy, so a wrong date in the change log shows up as a missing stock.

Examples
--------
    python scripts/universe/nifty50_constituents.py
    python scripts/universe/nifty50_constituents.py --start 2024-01-01 --end 2026-09-16
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parents[2]   # project root (scripts/<group>/<file>.py)
sys.path.insert(0, str(ROOT))                # so `from nse import ...` works from anywhere

from nse import bhavcopy as bc
from nse import nifty50_history as nh

CACHE = ROOT / "data" / "raw"


def write_changes(path: Path, today: date) -> None:
    # One row per index change: what came in and what went out on that date.
    df = pd.DataFrame([dict(effective_date=ch.effective,
                            added=" ".join(ch.added),
                            removed=" ".join(ch.removed),
                            reason=ch.reason,
                            status="effective" if ch.effective <= today else "announced")
                       for ch in nh.CHANGES])
    df.to_csv(path, index=False)
    print(f"{len(df)} change rows -> {path}")


# Names while in the index, for stocks since delisted or renamed.
FALLBACK_NAMES = {
    "HDFC": "Housing Development Finance Corporation Ltd.",
    "INFRATEL": "Bharti Infratel Ltd.",
    "LTIM": "LTIMindtree Ltd.",
}


def write_master(daily: pd.DataFrame, path: Path, names: dict[str, str],
                 last_eq: dict[str, str]) -> None:
    """One row per stock that was ever in the NIFTY 50 inside the window."""
    daily = daily.sort_values("trade_date")
    days = list(dict.fromkeys(daily["trade_date"]))
    last_day = days[-1]
    rows = []
    for sym, g in daily.groupby("symbol"):
        # Split the stock's days into continuous stretches in the index.
        idx = [days.index(d) for d in dict.fromkeys(g["trade_date"])]
        periods, begin = [], idx[0]
        for prev, cur in zip(idx, idx[1:] + [None]):
            if cur != prev + 1:
                periods.append(f"{days[begin]}..{days[prev]}")
                begin = cur
        isin = g["isin"].iloc[-1]
        rows.append(dict(
            symbol=sym,
            company=names.get(sym) or FALLBACK_NAMES.get(sym) or last_eq.get(isin, ""),
            isin=isin,
            bhavcopy_symbols=" ".join(dict.fromkeys(g["bhavcopy_symbol"])),
            first_date_in_index=g["trade_date"].iloc[0],
            last_date_in_index=g["trade_date"].iloc[-1],
            trading_days_in_index=len(idx),
            in_index_periods="; ".join(periods),
            currently_in_nifty50=g["trade_date"].iloc[-1] == last_day,
        ))
    df = pd.DataFrame(rows).sort_values(["currently_in_nifty50", "symbol"],
                                        ascending=[False, True])
    df.to_csv(path, index=False)
    print(f"{len(df)} stocks ever in NIFTY 50 "
          f"({int(df['currently_in_nifty50'].sum())} current) -> {path}")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2019-10-01")
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--out", default="data/universe/nifty50_constituents_daily.csv")
    p.add_argument("--changes-out", default="data/universe/nifty50_changes.csv")
    p.add_argument("--master-out", default="data/universe/nifty50_master_list.csv")
    a = p.parse_args()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    today = date.today()

    live = nh.fetch_live_list()
    names = dict(zip(live["Symbol"], live["Company Name"]))
    timeline = nh.membership_timeline(set(live["Symbol"]), as_of=today)

    write_changes(ROOT / a.changes_out, today)

    session = bc.new_session()
    rows, problems, days = [], [], 0
    for d in bc.weekday_range(start, end):
        try:
            raw = bc.read_zip(bc.fetch(session, "cm", d, CACHE))
        except bc.NoFileForDate:
            continue                                   # holiday / not yet published
        if "TckrSymb" in raw.columns:
            tick, series, isin = raw["TckrSymb"], raw["SctySrs"], raw["ISIN"]
        else:
            tick, series, isin = raw["SYMBOL"], raw["SERIES"], raw["ISIN"]
        eq = dict(zip(tick.astype(str).str.strip()[series.astype(str).str.strip() == "EQ"],
                      isin[series.astype(str).str.strip() == "EQ"]))
        days += 1

        for sym in sorted(nh.members_on(timeline, d)):
            printed = next((t for t in [sym, *nh.RENAMES.get(sym, [])] if t in eq), None)
            if printed is None:
                problems.append((d, sym))
            rows.append(dict(trade_date=d, symbol=sym, bhavcopy_symbol=printed or "",
                             isin=eq.get(printed, ""), company=names.get(sym, "")))
        if days % 250 == 0:
            print(f"  {d}: {days} trading days", flush=True)

    if not rows:
        print("no trading days in range", file=sys.stderr)
        return 1

    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    daily = pd.DataFrame(rows)
    daily.to_csv(out, index=False)
    print(f"{len(rows):,} rows, {days} trading days -> {out}")

    # Company names for stocks that have left the index: latest file NSE
    # published with names (from 2024-07-08, the newer file format), matched by ISIN.
    last_eq = {}
    if "FinInstrmNm" in raw.columns:
        last_eq = dict(zip(raw["ISIN"], raw["FinInstrmNm"].astype(str).str.title()))
    write_master(daily, ROOT / a.master_out, names, last_eq)

    if problems:
        print(f"\nWARNING: {len(problems)} member-days with no EQ row in the bhavcopy:")
        for (sym), grp in pd.DataFrame(problems, columns=["d", "sym"]).groupby("sym"):
            print(f"  {sym}: {grp['d'].min()} .. {grp['d'].max()} ({len(grp)} days)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
