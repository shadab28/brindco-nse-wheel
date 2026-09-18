#!/usr/bin/env python3
"""Build a point-in-time, rule-based F&O universe from cached bhavcopy data.

Two stages:

  scan    aggregate every F&O stock's daily option/futures activity into
          nse.fo_liquidity_daily (reads the local zip cache, no network)

  select  rank the pool at each rebalance date on a trailing window and write
          the chosen names to nse.universe_membership

Membership changes through time, so names that were liquid in 2020 and have
since been delisted, merged or dropped from the index (HDFC, ZEEL, SRTRANSFIN,
CADILAHC) are selectable in the periods when they actually traded. That is the
point: selecting on today's index membership is survivorship bias.

Examples
--------
    python scripts/universe/build_universe.py scan
    python scripts/universe/build_universe.py select --top 12 --rebalance 6M
    python scripts/universe/build_universe.py select --top 12 --ruleset oi_weighted --rank-by opt_oi
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parents[2]   # project root (scripts/<group>/<file>.py)
sys.path.insert(0, str(ROOT))                # so `from nse import ...` works from anywhere

from nse import bhavcopy as bc
from nse import db
from nse import liquidity as lq

RAW = ROOT / "data" / "raw" / "fo"

LIQ_COLS = ["trade_date", "symbol", "opt_turnover_cr", "opt_contracts", "opt_oi",
            "n_strikes", "n_traded_strikes", "fut_turnover_cr", "fut_oi"]
LIQ_PK = ["trade_date", "symbol"]

MEM_COLS = ["rebalance_date", "effective_from", "effective_to", "symbol", "rank",
            "opt_turnover_cr", "opt_oi", "traded_strike_frac", "ruleset"]
MEM_PK = ["rebalance_date", "symbol", "ruleset"]


# ------------------------------------------------------------------------ scan

def cmd_scan(args, conn) -> int:
    files = sorted(RAW.glob("*.csv.zip"))
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    def file_date(f: Path) -> date:
        return date.fromisoformat(f.name.split(".")[0])  # name is YYYY-MM-DD.csv.zip

    files = [f for f in files if start <= file_date(f) <= end]
    if not files:
        print(f"no cached F&O files in {RAW} for that range; run ingest.py first",
              file=sys.stderr)
        return 1

    print(f"scanning {len(files)} cached F&O bhavcopies (local, no downloads)")
    batch, total = [], 0
    for n, f in enumerate(files, 1):
        d = file_date(f)
        try:
            agg = lq.aggregate(bc.read_zip(f.read_bytes()), d)
        except Exception as exc:  # noqa: BLE001 - a corrupt cache entry must not stop the scan
            print(f"  {d}: SKIPPED ({exc})", file=sys.stderr)
            continue
        if not agg.empty:
            batch.append(agg)
        if n % 100 == 0 or n == len(files):
            if batch:
                total += db.upsert(conn, "nse.fo_liquidity_daily",
                                   pd.concat(batch, ignore_index=True),
                                   LIQ_COLS, LIQ_PK)
                conn.commit()
                batch.clear()
            print(f"  {n}/{len(files)} days | {total:,} rows", flush=True)

    with conn.cursor() as cur:
        cur.execute("SELECT count(DISTINCT symbol) FROM nse.fo_liquidity_daily")
        print(f"\npool: {cur.fetchone()[0]} distinct F&O stocks")
    return 0


# ---------------------------------------------------------------------- select

def rebalance_dates(start: date, end: date, freq: str) -> list[date]:
    months = {"1M": 1, "3M": 3, "6M": 6, "12M": 12}[freq]
    out, d = [], start
    while d <= end:
        out.append(d)
        y, m = divmod((d.month - 1) + months, 12)
        d = date(d.year + y, m + 1, 1)
    return out


def cmd_select(args, conn) -> int:
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    lookback = timedelta(days=args.lookback)

    liq = pd.read_sql("""
        SELECT trade_date, symbol, opt_turnover_cr, opt_oi,
               n_strikes, n_traded_strikes
        FROM nse.fo_liquidity_daily
        WHERE trade_date BETWEEN %(lo)s AND %(hi)s
    """, conn, params={"lo": start - lookback, "hi": end})
    if liq.empty:
        print("nse.fo_liquidity_daily is empty; run 'scan' first", file=sys.stderr)
        return 1
    liq["trade_date"] = pd.to_datetime(liq["trade_date"])

    rebals = rebalance_dates(start, end, args.rebalance)
    rows = []
    for i, rd in enumerate(rebals):
        # Trailing window strictly BEFORE the rebalance date: selecting on data
        # from the period being traded would be look-ahead bias.
        hi = pd.Timestamp(rd)
        lo = hi - pd.Timedelta(days=args.lookback)
        w = liq[(liq["trade_date"] >= lo) & (liq["trade_date"] < hi)]
        if w.empty:
            continue

        days = w["trade_date"].nunique()
        g = w.groupby("symbol").agg(
            opt_turnover_cr=("opt_turnover_cr", "mean"),
            opt_oi=("opt_oi", "mean"),
            n_strikes=("n_strikes", "mean"),
            n_traded_strikes=("n_traded_strikes", "mean"),
            active_days=("trade_date", "nunique"),
        )
        g["traded_strike_frac"] = g["n_traded_strikes"] / g["n_strikes"].replace(0, pd.NA)

        # Liquidity gates, applied before ranking:
        #  - quoted on most days in the window (weeds out newly listed names)
        #  - a real chain, not two strikes
        #  - a usable fraction of that chain actually trades: our bid-ask proxy,
        #    since EOD data carries no spread
        elig = g[(g["active_days"] >= args.min_active_frac * days)
                 & (g["n_strikes"] >= args.min_strikes)
                 & (g["traded_strike_frac"] >= args.min_traded_frac)]
        if elig.empty:
            continue

        top = elig.sort_values(args.rank_by, ascending=False).head(args.top)
        eff_to = (rebals[i + 1] - timedelta(days=1)) if i + 1 < len(rebals) else end
        for rank, (sym, r) in enumerate(top.iterrows(), 1):
            rows.append(dict(
                rebalance_date=rd, effective_from=rd, effective_to=eff_to,
                symbol=sym, rank=rank,
                opt_turnover_cr=round(float(r["opt_turnover_cr"]), 4),
                opt_oi=int(r["opt_oi"]),
                traded_strike_frac=round(float(r["traded_strike_frac"]), 4),
                ruleset=args.ruleset))

    if not rows:
        print("no periods produced a universe; loosen the filters", file=sys.stderr)
        return 1

    mem = pd.DataFrame(rows)
    # Replace the ruleset wholesale. Merging would leave rows from a previous
    # run on a different rebalance grid, producing overlapping effective
    # periods and silently double-counting names in the backtest.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM nse.universe_membership WHERE ruleset = %s",
                    (args.ruleset,))
    db.upsert(conn, "nse.universe_membership", mem, MEM_COLS, MEM_PK)
    conn.commit()

    names = sorted(mem["symbol"].unique())
    print(f"ruleset '{args.ruleset}': {len(mem)} memberships over "
          f"{mem['rebalance_date'].nunique()} rebalances, top {args.top} by {args.rank_by}")
    print(f"{len(names)} distinct names ever selected:\n  {', '.join(names)}")

    turn = mem.groupby("rebalance_date")["symbol"].apply(set)
    churn = [len(b - a) for a, b in zip(turn, turn[1:])]
    if churn:
        print(f"average churn per rebalance: {sum(churn)/len(churn):.1f} of {args.top}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["scan", "select"])
    p.add_argument("--start", default="2019-10-01")
    p.add_argument("--end", default="2026-07-31")
    p.add_argument("--dsn")
    # select-only
    p.add_argument("--top", type=int, default=12, help="names per period (assignment: 8-15)")
    p.add_argument("--rebalance", default="6M", choices=["1M", "3M", "6M", "12M"])
    p.add_argument("--lookback", type=int, default=90, help="trailing window, days")
    p.add_argument("--rank-by", default="opt_turnover_cr",
                   choices=["opt_turnover_cr", "opt_oi"])
    p.add_argument("--min-strikes", type=float, default=20)
    p.add_argument("--min-traded-frac", type=float, default=0.25)
    p.add_argument("--min-active-frac", type=float, default=0.8)
    p.add_argument("--ruleset", default="turnover_top12")
    a = p.parse_args()

    conn = db.connect(a.dsn)
    try:
        db.bootstrap(conn)
        return cmd_scan(a, conn) if a.command == "scan" else cmd_select(a, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
