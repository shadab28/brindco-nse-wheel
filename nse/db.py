"""PostgreSQL loading: schema bootstrap and idempotent bulk upsert."""

from __future__ import annotations

import csv
import hashlib
import io
import os
from datetime import date
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = ROOT / "sql" / "schema.sql"

CASH_COLS = ["trade_date", "symbol", "source_symbol", "series", "open", "high",
             "low", "close", "last", "prev_close", "volume", "turnover",
             "trades", "isin"]
CASH_PK = ["trade_date", "symbol"]

FUT_COLS = ["trade_date", "symbol", "source_symbol", "expiry", "instrument",
            "open", "high", "low", "close", "settle", "contracts", "value_lakh",
            "open_interest", "change_in_oi", "dte"]
FUT_PK = ["trade_date", "symbol", "expiry"]

OPT_COLS = ["trade_date", "symbol", "source_symbol", "expiry", "strike",
            "option_type", "instrument", "open", "high", "low", "close",
            "settle", "contracts", "value_lakh", "open_interest",
            "change_in_oi", "dte"]
OPT_PK = ["trade_date", "symbol", "expiry", "strike", "option_type"]

INT_COLS = {"volume", "trades", "contracts", "open_interest", "change_in_oi", "dte",
            "opt_contracts", "opt_oi", "n_strikes", "n_traded_strikes", "fut_oi",
            "rank", "lot_size", "lot_from_oi", "declared_lot", "n_obs",
            "cycle_trading_days", "n_symbols"}


def dsn_from_env() -> str:
    """DATABASE_URL wins; otherwise assemble from standard PG* env vars."""
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    return (f"host={os.environ.get('PGHOST', 'localhost')} "
            f"port={os.environ.get('PGPORT', '5432')} "
            f"dbname={os.environ.get('PGDATABASE', 'nse')} "
            f"user={os.environ.get('PGUSER', os.environ.get('USER', 'postgres'))} "
            f"password={os.environ.get('PGPASSWORD', '')}")


def connect(dsn: str | None = None):
    conn = psycopg2.connect(dsn or dsn_from_env())
    conn.autocommit = False
    return conn


def bootstrap(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL.read_text())
    conn.commit()


def universe_hash(symbols: list[str]) -> str:
    return hashlib.sha1(",".join(sorted(symbols)).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ bulk upsert

def _to_csv_buffer(df: pd.DataFrame, cols: list[str]) -> io.StringIO:
    """Serialise for COPY, with NaN -> empty (NULL) and ints kept integral."""
    out = df.reindex(columns=cols).copy()
    for c in cols:
        if c in INT_COLS:
            out[c] = pd.to_numeric(out[c], errors="coerce").round().astype("Int64")
    buf = io.StringIO()
    out.to_csv(buf, index=False, header=False, na_rep="",
               quoting=csv.QUOTE_MINIMAL)
    buf.seek(0)
    return buf


def upsert(conn, table: str, df: pd.DataFrame,
           cols: list[str], pk: list[str]) -> int:
    """COPY into a staging table, then merge into `table`. Returns rows merged.

    Re-running a date is safe: conflicting rows are overwritten, so a corrected
    bhavcopy (NSE does occasionally republish) supersedes what we already have.
    """
    if df is None or df.empty:
        return 0

    collist = ", ".join(f'"{c}"' for c in cols)
    pklist = ", ".join(f'"{c}"' for c in pk)
    updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols if c not in pk)

    with conn.cursor() as cur:
        cur.execute(f"CREATE TEMP TABLE stage (LIKE {table}) ON COMMIT DROP")
        cur.copy_expert(f"COPY stage ({collist}) FROM STDIN WITH (FORMAT csv)",
                        _to_csv_buffer(df, cols))
        # DISTINCT ON guards against duplicate keys inside one batch, which
        # would otherwise abort the statement with a cardinality error.
        cur.execute(f"""
            INSERT INTO {table} ({collist})
            SELECT DISTINCT ON ({pklist}) {collist}
            FROM stage
            ORDER BY {pklist}
            ON CONFLICT ({pklist}) DO UPDATE SET {updates}
        """)
        merged = cur.rowcount
        cur.execute("DROP TABLE stage")
    return merged


def upsert_cash(conn, df):
    return upsert(conn, "nse.cash_eod", df, CASH_COLS, CASH_PK)


def upsert_futures(conn, df):
    return upsert(conn, "nse.futures_eod", df, FUT_COLS, FUT_PK)


def upsert_options(conn, df):
    return upsert(conn, "nse.options_eod", df, OPT_COLS, OPT_PK)


# -------------------------------------------------------------------- ingest log

def log_days(conn, records: list[dict]) -> None:
    """records: {trade_date, segment, status, row_count, message, universe_hash}"""
    if not records:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO nse.ingest_log
                (trade_date, segment, status, row_count, message, universe_hash)
            VALUES %s
            ON CONFLICT (trade_date, segment) DO UPDATE SET
                status = EXCLUDED.status,
                row_count = EXCLUDED.row_count,
                message = EXCLUDED.message,
                universe_hash = EXCLUDED.universe_hash,
                ingested_at = now()
        """, [(r["trade_date"], r["segment"], r["status"], r.get("row_count"),
               r.get("message"), r.get("universe_hash")) for r in records])


def completed_days(conn, uhash: str) -> set[tuple[date, str]]:
    """(date, segment) pairs we can skip: already loaded for THIS universe, or
    confirmed to have no file at all (holidays, universe-independent)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT trade_date, segment FROM nse.ingest_log
            WHERE status = 'no_file'
               OR (status = 'ok' AND universe_hash = %s)
        """, (uhash,))
        return {(r[0], r[1]) for r in cur.fetchall()}


def summarise(conn) -> pd.DataFrame:
    return pd.read_sql("""
        SELECT c.symbol, c.first_date, c.last_date, c.trading_days,
               o.option_rows, o.first_expiry, o.last_expiry
        FROM nse.coverage c
        LEFT JOIN (
            SELECT symbol, count(*) AS option_rows,
                   min(expiry) AS first_expiry, max(expiry) AS last_expiry
            FROM nse.options_eod GROUP BY symbol
        ) o USING (symbol)
        ORDER BY c.symbol
    """, conn)
