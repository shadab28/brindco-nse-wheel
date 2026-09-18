#!/usr/bin/env python3
"""Ingest NSE end-of-day cash + F&O bhavcopy data into PostgreSQL.

Downloads are whole-market files, so the NIFTY 50 (or any other symbol list)
costs exactly the same network traffic as a single name. Raw zips are cached
under data/raw/, and an ingest log in the database makes re-runs incremental.

Examples
--------
    # one-off: create the schema and load the full NIFTY 50 history
    python scripts/ingest/ingest.py --start 2019-10-01 --end 2026-07-31

    # just RELIANCE
    python scripts/ingest/ingest.py --symbols RELIANCE

    # NIFTY 50 plus the index underlyings (Part B)
    python scripts/ingest/ingest.py --indices

    # keep the warehouse current (skips days already ingested)
    python scripts/ingest/ingest.py --start 2026-07-01 --end 2026-09-16

Connection comes from DATABASE_URL, or the standard PGHOST/PGPORT/PGDATABASE/
PGUSER/PGPASSWORD variables, or --dsn.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

import sys
ROOT = Path(__file__).resolve().parents[2]   # project root (scripts/<group>/<file>.py)
sys.path.insert(0, str(ROOT))                # so `from nse import ...` works from anywhere

from nse import bhavcopy as bc
from nse import db
from nse.universe import INDICES, NIFTY_50, load_universe_file, resolve

RAW = ROOT / "data" / "raw"


def parse_day(session, d: date, symbols: set[str], uhash: str):
    """Fetch + normalise one date. Never raises; failures come back as logs."""
    cash = futs = opts = None
    logs = []
    for segment in ("cm", "fo"):
        try:
            blob = bc.fetch(session, segment, d, RAW)
            frame = bc.read_zip(blob)
            if segment == "cm":
                cash = bc.normalise_cash(frame, d, symbols)
                n = len(cash)
            else:
                fo = bc.normalise_fo(frame, d, symbols)
                futs, opts = bc.split_fo(fo)
                n = len(futs) + len(opts)
            logs.append(dict(trade_date=d, segment=segment, status="ok",
                             row_count=n, message=None, universe_hash=uhash))
        except bc.NoFileForDate:
            logs.append(dict(trade_date=d, segment=segment, status="no_file",
                             row_count=0, message="holiday or not published",
                             universe_hash=None))
        except Exception as exc:  # noqa: BLE001 - log and continue the run
            logs.append(dict(trade_date=d, segment=segment, status="error",
                             row_count=None, message=str(exc)[:500],
                             universe_hash=None))
    return cash, futs, opts, logs


def flush(conn, buckets: dict, logs: list, totals: dict) -> None:
    """Merge one batch into Postgres as a single transaction."""
    def cat(key):
        frames = [f for f in buckets[key] if f is not None and not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else None

    totals["cash"] += db.upsert_cash(conn, cat("cash"))
    totals["futures"] += db.upsert_futures(conn, cat("futures"))
    totals["options"] += db.upsert_options(conn, cat("options"))
    db.log_days(conn, logs)
    conn.commit()
    for k in buckets:
        buckets[k].clear()
    logs.clear()


def run(args) -> int:
    if args.universe_file:
        base = load_universe_file(Path(args.universe_file))
    elif args.symbols:
        base = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        base = list(NIFTY_50)
    if args.indices:
        base += INDICES

    symbols = resolve(base)
    uhash = db.universe_hash(symbols)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    days = bc.weekday_range(start, end)

    conn = db.connect(args.dsn)
    try:
        db.bootstrap(conn)
        print(f"schema ready; universe {len(symbols)} symbols (hash {uhash})")

        done = set() if args.force else db.completed_days(conn, uhash)
        todo = [d for d in days
                if (d, "cm") not in done or (d, "fo") not in done]
        skipped = len(days) - len(todo)
        print(f"{len(days)} candidate trading days {start} .. {end}; "
              f"{skipped} already ingested, {len(todo)} to process")
        if not todo:
            print("nothing to do")
            return 0

        symbol_set = set(symbols)
        sessions = [bc.new_session() for _ in range(args.workers)]
        buckets = {"cash": [], "futures": [], "options": []}
        pending_logs: list[dict] = []
        totals = {"cash": 0, "futures": 0, "options": 0}

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            tasks = ((sessions[i % args.workers], d, symbol_set, uhash)
                     for i, d in enumerate(todo))
            for n, (cash, futs, opts, logs) in enumerate(
                    pool.map(lambda t: parse_day(*t), tasks), 1):
                buckets["cash"].append(cash)
                buckets["futures"].append(futs)
                buckets["options"].append(opts)
                pending_logs.extend(logs)
                if n % args.batch_days == 0:
                    flush(conn, buckets, pending_logs, totals)
                    print(f"  {n}/{len(todo)} days | "
                          f"cash {totals['cash']:,} fut {totals['futures']:,} "
                          f"opt {totals['options']:,}", flush=True)
        flush(conn, buckets, pending_logs, totals)

        print(f"\nmerged: {totals['cash']:,} cash / {totals['futures']:,} futures "
              f"/ {totals['options']:,} option rows")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM nse.ingest_log WHERE status='error'")
            errs = cur.fetchone()[0]
        if errs:
            print(f"{errs} day/segment(s) errored. Inspect with:\n"
                  "  SELECT * FROM nse.ingest_log WHERE status='error';\n"
                  "then re-run this command to retry them.")

        if args.summary:
            print("\ncoverage:")
            print(db.summarise(conn).to_string(index=False))
        return 1 if errs else 0
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2019-10-01", help="YYYY-MM-DD")
    p.add_argument("--end", default="2026-07-31", help="YYYY-MM-DD")
    p.add_argument("--symbols", help="comma-separated tickers (default: NIFTY 50)")
    p.add_argument("--universe-file", help="file with one ticker per line")
    p.add_argument("--indices", action="store_true",
                   help="also ingest NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY")
    p.add_argument("--dsn", help="libpq DSN or URL; overrides env")
    p.add_argument("--workers", type=int, default=4,
                   help="parallel downloads; keep low, NSE throttles")
    p.add_argument("--batch-days", type=int, default=20,
                   help="days per database transaction")
    p.add_argument("--force", action="store_true",
                   help="re-ingest days already marked done")
    p.add_argument("--summary", action="store_true",
                   help="print per-symbol coverage when finished")
    return run(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
