#!/usr/bin/env python3
"""Monthly expiry cycles and the point-in-time universe for the wheel backtest.

Stages (run in order):

    calendar   monthly expiry dates + cycle start dates  -> nse.expiry_calendar
    lots       per-series lot size and instrument token  -> nse.contract_lots
    universe   eligible stocks per cycle, with contracts -> nse.cycle_universe

Everything is derived from ingested data rather than from calendar rules. NSE
stock options carry only monthly expiries, so the distinct expiries observed in
the stock-option chain ARE the monthly calendar -- including the 2025 move of
expiry day from Thursday to Tuesday and every holiday shift, which a hardcoded
"last Thursday" rule gets wrong 17 times out of 83 in this window.

    python scripts/wheel/build_cycles.py calendar
    python scripts/wheel/build_cycles.py lots
    python scripts/wheel/build_cycles.py universe --source liquidity:turnover_top12
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
from nse import contracts as ct
from nse import db

RAW = ROOT / "data" / "raw" / "fo"

CAL_COLS = ["contract_month", "expiry_date", "quoted_expiry_stamps",
            "prev_expiry_date", "cycle_start_date", "cycle_trading_days",
            "n_symbols"]
CAL_PK = ["contract_month"]

LOT_COLS = ["symbol", "expiry", "contract_month", "lot_size", "lot_source",
            "lot_from_oi", "declared_lot", "instrument_token", "n_obs",
            "first_seen", "last_seen"]
LOT_PK = ["symbol", "expiry"]

CU_COLS = ["contract_month", "cycle_start_date", "expiry_date", "symbol",
           "instrument_token", "constituent_source", "has_option_chain",
           "n_strikes", "lot_size"]
CU_PK = ["contract_month", "symbol", "constituent_source"]

# A series' true expiry is the last date it actually traded. Grouping by
# contract month (not by the quoted stamp) reunites a series whose expiry was
# advanced for a holiday and restamped only on its final day.
SERIES_SQL = """
SELECT date_trunc('month', expiry)::date          AS contract_month,
       max(trade_date)                            AS expiry_date,
       array_agg(DISTINCT expiry ORDER BY expiry) AS quoted_expiry_stamps,
       count(DISTINCT symbol)                     AS n_symbols
FROM nse.options_eod
WHERE instrument IN ('OPTSTK', 'STO')
  AND expiry BETWEEN %(start)s AND %(end)s
GROUP BY 1
ORDER BY 1
"""

TRADING_DAYS_SQL = "SELECT DISTINCT trade_date FROM nse.cash_eod ORDER BY 1"


def file_date(f: Path) -> date:
    return date.fromisoformat(f.name.split(".")[0])  # name is YYYY-MM-DD.csv.zip


# -------------------------------------------------------------------- calendar

def cmd_calendar(a, conn) -> int:
    cal = pd.read_sql(SERIES_SQL, conn, params={"start": a.start, "end": a.end})
    days = pd.read_sql(TRADING_DAYS_SQL, conn)["trade_date"].tolist()
    if cal.empty:
        print("no stock-option expiries found; ingest first", file=sys.stderr)
        return 1

    # Drop a trailing series that has not expired yet: its max(trade_date) is
    # just the last date we hold data for, not a real expiry.
    last_data_day = max(days)
    unexpired = cal["expiry_date"] >= last_data_day
    if unexpired.any():
        print(f"dropped {int(unexpired.sum())} unexpired series: "
              f"{[str(x) for x in cal.loc[unexpired, 'contract_month']]}")
        cal = cal[~unexpired].reset_index(drop=True)

    cal["prev_expiry_date"] = cal["expiry_date"].shift(1)

    # The cycle begins on the first trading day AFTER the previous monthly
    # expiry -- that is when the wheel rolls into the new front month.
    idx = {d: i for i, d in enumerate(days)}

    def next_trading_day(d):
        if d is None or pd.isna(d):
            return None
        i = idx.get(d)
        return days[i + 1] if i is not None and i + 1 < len(days) else None

    cal["cycle_start_date"] = cal["prev_expiry_date"].map(next_trading_day)
    cal.loc[cal["cycle_start_date"].isna(), "cycle_start_date"] = days[0]

    cal["cycle_trading_days"] = cal.apply(
        lambda r: sum(1 for d in days
                      if r["cycle_start_date"] <= d <= r["expiry_date"]), axis=1)

    stamps = cal["quoted_expiry_stamps"].map(list)
    # COPY needs a Postgres array literal, not a Python list repr.
    cal["quoted_expiry_stamps"] = stamps.map(
        lambda xs: "{" + ",".join(str(x) for x in xs) + "}")

    db.upsert(conn, "nse.expiry_calendar", cal, CAL_COLS, CAL_PK)
    conn.commit()

    print(f"{len(cal)} monthly cycles, "
          f"{cal['contract_month'].min()} .. {cal['contract_month'].max()}")
    multi = cal[stamps.map(len) > 1]
    for _, r in multi.iterrows():
        print(f"  restamped (holiday shift) {r['contract_month']}: "
              f"settled {r['expiry_date']}, quoted {r['quoted_expiry_stamps']}")
    print(f"cycle length: median {cal['cycle_trading_days'].median():.0f} "
          f"trading days, range {cal['cycle_trading_days'].min()}"
          f"-{cal['cycle_trading_days'].max()}")
    return 0


# ------------------------------------------------------------------------ lots

def cmd_lots(a, conn) -> int:
    """Recover per-series lot size and instrument token from the zip cache."""
    files = [f for f in sorted(RAW.glob("*.csv.zip"))
             if date.fromisoformat(a.start) <= file_date(f) <= date.fromisoformat(a.end)]
    if not files:
        print("no cached F&O files; run ingest.py first", file=sys.stderr)
        return 1

    print(f"scanning {len(files)} cached files for lot sizes")
    daily = []
    for n, f in enumerate(files, 1):
        try:
            got = ct.extract(bc.read_zip(f.read_bytes()), file_date(f))
            if not got.empty:
                daily.append(got)
        except Exception as exc:  # noqa: BLE001 - one bad cache entry must not stop the scan
            print(f"  {file_date(f)}: SKIPPED ({exc})", file=sys.stderr)
        if n % 250 == 0:
            print(f"  {n}/{len(files)}", flush=True)

    lots = ct.consolidate(pd.concat(daily, ignore_index=True))
    db.upsert(conn, "nse.contract_lots", lots, LOT_COLS, LOT_PK)
    conn.commit()

    declared = lots[lots["lot_source"] == "NewBrdLotQty"]
    print(f"\n{len(lots):,} contract series; {len(declared):,} carry an "
          f"exchange-declared lot (UDiFF era)")
    if len(declared):
        agree = (declared["lot_from_oi"].astype("Int64")
                 == declared["declared_lot"].astype("Int64")).mean()
        print(f"OI-GCD matches the declared lot on {agree:.1%} of those -- "
              f"this is the validation for the pre-2024 derived values")
    rev = lots.groupby("symbol")["lot_size"].nunique()
    print(f"{int((rev > 1).sum())} symbols show at least one lot-size revision")
    return 0


# -------------------------------------------------------------------- universe

def cmd_universe(a, conn) -> int:
    cal = pd.read_sql("SELECT * FROM nse.expiry_calendar ORDER BY contract_month", conn)
    if cal.empty:
        print("run 'calendar' first", file=sys.stderr)
        return 1

    src = a.source
    if src.startswith("liquidity:"):
        ruleset = src.split(":", 1)[1]
        mem = pd.read_sql(
            "SELECT symbol, effective_from, effective_to "
            "FROM nse.universe_membership WHERE ruleset = %(r)s",
            conn, params={"r": ruleset})
        if mem.empty:
            print(f"no membership for ruleset '{ruleset}'", file=sys.stderr)
            return 1

        def members_on(d):
            m = mem[(mem["effective_from"] <= d) & (d <= mem["effective_to"])]
            return sorted(m["symbol"].unique())
    elif src == "nifty50_current":
        # Deliberately survivorship-biased; kept only as a comparison baseline.
        from nse.universe import NIFTY_50
        fixed = sorted(NIFTY_50)

        def members_on(d):
            return fixed
    else:
        print(f"unknown --source '{src}'", file=sys.stderr)
        return 1

    chains = pd.read_sql("""
        SELECT symbol, date_trunc('month', expiry)::date AS contract_month,
               count(DISTINCT strike) AS n_strikes
        FROM nse.options_eod WHERE instrument IN ('OPTSTK','STO')
        GROUP BY 1, 2
    """, conn).set_index(["symbol", "contract_month"])
    # A restamped expiry (e.g. Jun 2023) yields two contract_lots rows in one
    # contract month; keep the best-observed one so the index stays unique.
    lots = pd.read_sql(
        "SELECT DISTINCT ON (symbol, contract_month) symbol, contract_month, "
        "       lot_size, instrument_token "
        "FROM nse.contract_lots ORDER BY symbol, contract_month, n_obs DESC",
        conn).set_index(["symbol", "contract_month"])

    rows = []
    for _, c in cal.iterrows():
        for sym in members_on(c["cycle_start_date"]):
            key = (sym, c["contract_month"])
            ch = chains.loc[key] if key in chains.index else None
            lt = lots.loc[key] if key in lots.index else None
            tok = None if lt is None or pd.isna(lt["instrument_token"]) else str(lt["instrument_token"])
            rows.append(dict(
                contract_month=c["contract_month"],
                cycle_start_date=c["cycle_start_date"],
                expiry_date=c["expiry_date"],
                symbol=sym,
                instrument_token=tok,
                constituent_source=src,
                has_option_chain=ch is not None,
                n_strikes=None if ch is None else int(ch["n_strikes"]),
                lot_size=None if lt is None or pd.isna(lt["lot_size"]) else int(lt["lot_size"]),
            ))

    cu = pd.DataFrame(rows)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM nse.cycle_universe WHERE constituent_source = %s", (src,))
    db.upsert(conn, "nse.cycle_universe", cu, CU_COLS, CU_PK)
    conn.commit()

    print(f"source '{src}': {len(cu):,} cycle-symbol rows over "
          f"{cu['contract_month'].nunique()} cycles, "
          f"{cu['symbol'].nunique()} distinct symbols")
    print(f"option chain present for {cu['has_option_chain'].mean():.1%}; "
          f"lot size known for {cu['lot_size'].notna().mean():.1%}")
    missing = cu[~cu["has_option_chain"]]
    if len(missing):
        print(f"{len(missing)} cycle-symbol rows have no chain that month "
              f"(not tradeable): {sorted(missing['symbol'].unique())[:12]}")
    return 0


# ----------------------------------------------------------------------- chain

CHAIN_SQL = """
INSERT INTO nse.wheel_chain (
    constituent_source, contract_month, cycle_start_date, expiry_date, symbol,
    trade_date, dte, strike, option_type, close, settle, contracts,
    open_interest, lot_size, spot_close, moneyness)
SELECT u.constituent_source, u.contract_month, u.cycle_start_date, u.expiry_date,
       o.symbol, o.trade_date,
       (SELECT count(*) FROM trading_days t
         WHERE t.d > o.trade_date AND t.d <= u.expiry_date) AS dte,
       o.strike, o.option_type, o.close, o.settle, o.contracts, o.open_interest,
       u.lot_size, c.close AS spot_close,
       CASE WHEN c.close > 0 THEN o.strike / c.close END AS moneyness
FROM nse.cycle_universe u
JOIN nse.options_eod o
  ON o.symbol = u.symbol
 AND date_trunc('month', o.expiry)::date = u.contract_month
 AND o.instrument IN ('OPTSTK','STO')
 AND o.trade_date >= u.cycle_start_date
 AND o.trade_date <= u.expiry_date
LEFT JOIN nse.cash_eod c
  ON c.symbol = o.symbol AND c.trade_date = o.trade_date
WHERE u.constituent_source = %(src)s
  AND u.has_option_chain
ON CONFLICT DO NOTHING
"""


def cmd_chain(a, conn) -> int:
    """Materialise the front-month chain the backtest consumes."""
    with conn.cursor() as cur:
        # dte counts trading days, not calendar days: a wheel decision cares
        # about how many sessions remain, not weekends.
        cur.execute("CREATE TEMP TABLE trading_days ON COMMIT DROP AS "
                    "SELECT DISTINCT trade_date AS d FROM nse.cash_eod")
        cur.execute("CREATE INDEX ON trading_days (d)")
        cur.execute("DELETE FROM nse.wheel_chain WHERE constituent_source = %s",
                    (a.source,))
        print(f"building chain for '{a.source}' ...", flush=True)
        cur.execute(CHAIN_SQL, {"src": a.source})
        inserted = cur.rowcount
    conn.commit()

    stats = pd.read_sql("""
        SELECT count(*) rows, count(DISTINCT symbol) syms,
               count(DISTINCT contract_month) cycles,
               min(trade_date) first_date, max(trade_date) last_date,
               round(100.0*avg((contracts > 0)::int), 1) pct_traded,
               round(100.0*avg((spot_close IS NOT NULL)::int), 1) pct_with_spot
        FROM nse.wheel_chain WHERE constituent_source = %(src)s
    """, conn, params={"src": a.source}).iloc[0]
    print(f"{inserted:,} rows inserted")
    print(f"  {stats['rows']:,} rows | {stats['syms']} symbols | "
          f"{stats['cycles']} cycles | {stats['first_date']} .. {stats['last_date']}")
    print(f"  {stats['pct_traded']}% of contract-days actually traded; "
          f"spot present on {stats['pct_with_spot']}%")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command",
                   choices=["calendar", "lots", "universe", "chain"])
    p.add_argument("--start", default="2019-10-01")
    p.add_argument("--end", default="2026-12-31")
    p.add_argument("--source", default="liquidity:turnover_top12",
                   help="'liquidity:<ruleset>' or 'nifty50_current'")
    p.add_argument("--dsn")
    a = p.parse_args()

    conn = db.connect(a.dsn)
    try:
        db.bootstrap(conn)
        return {"calendar": cmd_calendar, "lots": cmd_lots,
                "universe": cmd_universe, "chain": cmd_chain}[a.command](a, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
