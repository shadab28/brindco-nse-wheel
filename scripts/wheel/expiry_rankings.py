"""
Rank the NIFTY 50 stocks at the close of every monthly expiry day.

For each monthly expiry date E, take the stocks that were in the index on E and
score each one by how far its closing price sits above its short-term (15m EMA50)
and medium-term (daily EMA20) trend lines. Higher score = stronger uptrend.
The ranked list feeds the wheel strategy's stock selection.

Inputs (all under data/):
    universe/nifty50_master_list.csv       one row per symbol; in_index_periods lists the
                                  date ranges it was a member ("start..end", ';' or '|' separated)
    calendar/nifty50_monthly_expiries.csv  expiry calendar (contract_month, expiry_date)
    market/nifty50_15m.db                SQLite table ohlcv_15m; bars are labelled by their
                                  start time, 09:15 .. 15:15

Score:
    ltp        = close of E's 15:15 bar (the day's last bar, ends 15:30)
    pct_15m    = (ltp - EMA50 of 15m closes) / EMA50 * 100
    pct_daily  = (ltp - EMA20 of daily closes) / EMA20 * 100
    rank_final = (sqrt((1 + pct_15m/100) * (1 + pct_daily/100)) - 1) * 100

    rank_final is the geometric mean of the two percentage gaps, so a stock has to
    be above both trend lines to score well. Every member is written; there is no cut-off.

Data notes:
    - The 15m bars come from the Zerodha Kite Connect historical data API, not from
      public NSE files (NSE does not publish 15m bars). No script in this folder
      downloads them, so rebuilding nifty50_15m.db needs a Kite Connect account;
      without one, only this copy of the database can be checked.
    - Daily closes are the last 15m close of each day, not the exchange EOD file.
      The 15m data is split/bonus-adjusted and the EOD file is not, so taking both
      EMAs from the same source keeps them comparable.
    - Only data up to and including E's close is read, so there is no look-ahead.
    - A stock is skipped for E if it has no 15:15 bar on E, fewer than 50 15m bars,
      or fewer than 20 daily closes.
    - EMAs use adjust=False (seeded from the first close in the window). The 15m
      window is 60 calendar days (~1000 bars), long enough for the seed to wash out.

Output (data/signals/expiry_rankings.csv by default), one row per stock per expiry:
    expiry_date, contract_month, rank, symbol, ltp,
    pct_vs_15m_ema50, pct_vs_daily_ema20, rank_final, next_expiry_return_pct

    next_expiry_return_pct = (close on the next monthly expiry / ltp - 1) * 100, both from
    the same split-adjusted 15m source. It is forward-looking (an outcome, not an input
    to the score) and is blank for the latest expiry or if the stock has no close that day.

Usage:
    python scripts/wheel/expiry_rankings.py                                      # all expiries
    python scripts/wheel/expiry_rankings.py --start 2024-01-01 --end 2024-12-31  # a date range
    python scripts/wheel/expiry_rankings.py --out some/other.csv
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]   # project root

DATA = ROOT / "data"

EMA15_LEN = 50     # span of the EMA on 15m closes
MA_DAILY_LEN = 20  # span of the EMA on daily closes


# --- Scoring ---

def trend_score(pct_vs_15m_ema50: float, pct_vs_daily_ema20: float) -> float:
    """Geometric mean of the two % gaps, in %. Returns 0.0 if either price ratio is non-positive."""
    if pct_vs_15m_ema50 <= -100 or pct_vs_daily_ema20 <= -100:
        return 0.0
    product = (1 + pct_vs_15m_ema50 / 100) * (1 + pct_vs_daily_ema20 / 100)
    if product < 0:
        return 0.0
    return round((math.sqrt(product) - 1) * 100, 2)


def _pct(px: float, ma: float) -> float:
    """Percentage distance of price px from moving average ma."""
    return round((px - ma) / ma * 100, 2)


# --- Inputs ---

def load_members(expiry: pd.Timestamp, master: pd.DataFrame) -> list[str]:
    """Symbols whose in_index_periods cover the expiry date, sorted."""
    out = []
    for r in master.itertuples(index=False):
        for period in str(r.in_index_periods).replace("|", ";").split(";"):
            # An open-ended period ("start..") counts as a single day.
            start, _, end = period.strip().partition("..")
            if start and pd.Timestamp(start) <= expiry <= pd.Timestamp(end or start):
                out.append(r.symbol)
                break
    return sorted(out)


def load_15m(con: sqlite3.Connection, expiry: pd.Timestamp, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """15m closes for each symbol over the 60 days ending on the expiry (inclusive)."""
    q = (f"select symbol, ts, close from ohlcv_15m where symbol in ({','.join('?' * len(symbols))}) "
         "and trade_date > ? and trade_date <= ? order by symbol, ts")
    lo = (expiry - pd.Timedelta(days=60)).strftime("%Y-%m-%d")  # ~1000 bars: EMA50 warm-up
    df = pd.read_sql_query(q, con, params=[*symbols, lo, expiry.strftime("%Y-%m-%d")])
    df["ts"] = pd.to_datetime(df.ts)
    return {s: g.reset_index(drop=True) for s, g in df.groupby("symbol")}


# --- Ranking ---

def rank_stock(bars: pd.DataFrame, closes: pd.Series, expiry: pd.Timestamp) -> dict | None:
    """Score one stock at the expiry close, or None if its data is missing or too short.

    bars: the stock's 15m bars from load_15m. closes: its daily closes indexed by date.
    """
    end = expiry + pd.Timedelta(hours=15, minutes=15)
    closes = closes[closes.index <= expiry]
    # Require E's own 15:15 bar and E's own daily close, else the rank would be stale.
    if len(bars) < EMA15_LEN or bars.ts.iloc[-1] != end:
        return None
    if len(closes) < MA_DAILY_LEN or closes.index[-1] != expiry:
        return None

    c = bars.close.to_numpy(dtype=float)
    ema50 = bars.close.ewm(span=EMA15_LEN, adjust=False).mean().to_numpy(dtype=float)
    ema20 = closes.ewm(span=MA_DAILY_LEN, adjust=False).mean().iloc[-1]

    p15, p20 = _pct(c[-1], ema50[-1]), _pct(c[-1], ema20)
    return {
        "ltp": round(float(c[-1]), 2),
        "pct_vs_15m_ema50": p15,
        "pct_vs_daily_ema20": p20,
        "rank_final": trend_score(p15, p20),
    }


def build(start: str | None, end: str | None, out: Path) -> pd.DataFrame:
    """Rank every expiry between start and end (inclusive), write the CSV to out and return it."""
    master = pd.read_csv(DATA / "universe" / "nifty50_master_list.csv")
    expiries = pd.read_csv(DATA / "calendar" / "nifty50_monthly_expiries.csv", parse_dates=["expiry_date"])
    expiries = expiries.sort_values("expiry_date").reset_index(drop=True)
    # Next expiry from the full calendar, so a --end filter doesn't blank the last row.
    expiries["next_expiry"] = expiries.expiry_date.shift(-1)
    if start:
        expiries = expiries[expiries.expiry_date >= start]
    if end:
        expiries = expiries[expiries.expiry_date <= end]

    # Daily close series per symbol = last 15m close of each trading day.
    with sqlite3.connect(DATA / "market" / "nifty50_15m.db") as con:
        last15 = pd.read_sql_query(
            "select symbol, trade_date, close, max(ts) from ohlcv_15m group by symbol, trade_date", con,
            parse_dates=["trade_date"])  # SQLite returns close from the max(ts) row
    daily = {s: g.set_index("trade_date").close.sort_index() for s, g in last15.groupby("symbol")}

    frames = []
    with sqlite3.connect(DATA / "market" / "nifty50_15m.db") as con:
        for exp in expiries.itertuples(index=False):
            e = exp.expiry_date
            members = load_members(e, master)
            bars = load_15m(con, e, members)
            rows, skipped = [], []
            for s in members:
                r = rank_stock(bars[s], daily[s], e) if s in bars and s in daily else None
                if r is None:
                    skipped.append(s)
                else:
                    nxt = exp.next_expiry
                    nxt_close = daily[s].get(nxt) if pd.notna(nxt) else None
                    r["next_expiry_return_pct"] = (round((nxt_close / r["ltp"] - 1) * 100, 2)
                                                   if nxt_close is not None else None)
                    rows.append({"symbol": s, **r})
            if not rows:
                print(f"{e.date()}: no data for any of {len(members)} members, skipped")
                continue
            # Highest score first; rank 1 = strongest trend.
            df = pd.DataFrame(rows).sort_values("rank_final", ascending=False, kind="stable").reset_index(drop=True)
            df.insert(0, "rank", df.index + 1)
            df.insert(0, "contract_month", exp.contract_month)
            df.insert(0, "expiry_date", e.date())
            frames.append(df)
            print(f"{e.date()}: ranked {len(df)}/{len(members)}, "
                  f"top {df.symbol.iloc[0]} {df.rank_final.iloc[0]:.2f}"
                  + (f"  | no data: {skipped}" if skipped else ""))

    res = pd.concat(frames, ignore_index=True)
    res.to_csv(out, index=False)
    print(f"\n{res.expiry_date.nunique()} expiries, {len(res)} rows -> {out}")
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", help="first expiry date, YYYY-MM-DD")
    ap.add_argument("--end", help="last expiry date, YYYY-MM-DD")
    ap.add_argument("--out", type=Path, default=DATA / "signals" / "expiry_rankings.csv")
    a = ap.parse_args(argv)
    build(a.start, a.end, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
