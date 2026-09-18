#!/usr/bin/env python3
"""Export daily EOD cash prices for a symbol universe to a single CSV.

Reads from the PostgreSQL warehouse built by ingest.py, so the database must be
populated first. Defaults to the NIFTY 50 over 2016-01-01 .. 2026-07-31.

Examples
--------
    python scripts/ingest/export_daily.py
    python scripts/ingest/export_daily.py --out data/market/nifty50.csv --start 2020-01-01
    python scripts/ingest/export_daily.py --symbols RELIANCE,TCS --wide
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parents[2]   # project root (scripts/<group>/<file>.py)
sys.path.insert(0, str(ROOT))                # so `from nse import ...` works from anywhere

from nse import db
from nse.universe import NIFTY_50, load_universe_file

QUERY = """
SELECT trade_date, symbol, source_symbol, open, high, low, close, last,
       prev_close, volume, turnover, trades, isin
FROM nse.cash_eod
WHERE symbol = ANY(%(symbols)s)
  AND trade_date BETWEEN %(start)s AND %(end)s
ORDER BY trade_date, symbol
"""


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2016-01-01")
    p.add_argument("--end", default="2026-07-31")
    p.add_argument("--symbols", help="comma-separated; default is the NIFTY 50")
    p.add_argument("--universe-file")
    p.add_argument("--out", default="data/market/nifty50_daily_eod.csv")
    p.add_argument("--wide", action="store_true",
                   help="also write a date x symbol matrix of closing prices")
    p.add_argument("--dsn")
    a = p.parse_args()

    if a.universe_file:
        symbols = load_universe_file(Path(a.universe_file))
    elif a.symbols:
        symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    else:
        symbols = list(NIFTY_50)

    conn = db.connect(a.dsn)
    try:
        df = pd.read_sql(QUERY, conn,
                         params={"symbols": symbols, "start": a.start, "end": a.end})
    finally:
        conn.close()

    if df.empty:
        print("no rows matched - is the database populated?", file=sys.stderr)
        return 1

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)

    got = set(df["symbol"])
    missing = [s for s in symbols if s not in got]
    print(f"{len(df):,} rows x {len(got)} symbols -> {out}")
    print(f"dates {df['trade_date'].min()} .. {df['trade_date'].max()} "
          f"({df['trade_date'].nunique()} trading days)")
    if missing:
        print(f"no data for: {', '.join(missing)}")

    if a.wide:
        wide = df.pivot(index="trade_date", columns="symbol", values="close")
        wide_path = out.with_name(out.stem + "_close_matrix.csv")
        wide.to_csv(wide_path)
        print(f"{wide.shape[0]:,} x {wide.shape[1]} close matrix -> {wide_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
