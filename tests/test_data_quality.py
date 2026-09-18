"""Data quality over the warehouse and the backtest cache (guide §6.3).

    pytest tests/test_data_quality.py

Needs the Postgres warehouse (DATABASE_URL or .env); skipped if it can't be reached. Each check runs over the
full window on disk, not a sample.

Checks:
    1. no duplicate (contract, date) keys: warehouse primary keys exist and hold; the parquet cache has no duplicates
    2. no contract with expiry earlier than the trade date
    3. option_type is only CE / PE
    4. the underlying close exists on every signal day (ranked names) and every expiry day (open contracts)
    5. every weekday without files is a market holiday, not a missing download
"""
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
WINDOW = ("2019-10-01", "2026-07-31")
# F&O bhavcopy not published; rebuilt from NSE's market-activity report (data_validation/reports/REPAIR_LOG.csv)
REPAIRED_FO_DAYS = {pd.Timestamp("2021-03-30")}


@pytest.fixture(scope="module")
def db():
    import psycopg2
    from nse.wheel.data import _dsn
    try:
        con = psycopg2.connect(_dsn(), connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"warehouse not reachable: {e}")
    yield con
    con.close()


def q(con, sql, params=None) -> pd.DataFrame:
    with con.cursor() as cur:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


# ---------------------------------------------------------------- 1. duplicate keys
PKS = {
    "cash_eod": ["trade_date", "symbol"],
    "futures_eod": ["trade_date", "symbol", "expiry"],
    "options_eod": ["trade_date", "symbol", "expiry", "strike", "option_type"],
}


@pytest.mark.parametrize("table,key", PKS.items())
def test_primary_key_declared(db, table, key):
    # the database itself then rejects a duplicate (contract, date) row
    got = q(db, """
        select a.attname from pg_index i
        join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)
        where i.indrelid = %s::regclass and i.indisprimary""", (f"nse.{table}",)).attname.tolist()
    assert sorted(got) == sorted(key)


@pytest.mark.parametrize("table,key", PKS.items())
def test_no_duplicate_keys_in_warehouse(db, table, key):
    k = ", ".join(key)
    dup = q(db, f"select {k}, count(*) n from nse.{table} group by {k} having count(*) > 1 limit 20")
    assert dup.empty, dup


def test_no_duplicate_keys_in_backtest_cache(params):
    cache = ROOT / params["base_dir"] / "cache"
    if not (cache / "options.parquet").exists():
        pytest.skip("market cache not built")
    for name, key in [("cash", PKS["cash_eod"]), ("futures", PKS["futures_eod"]), ("options", PKS["options_eod"])]:
        df = pd.read_parquet(cache / f"{name}.parquet", columns=key)
        assert not df.duplicated().any(), f"{name}.parquet: {df[df.duplicated(keep=False)].head()}"


def test_no_same_contract_under_two_expiry_stamps(db):
    # stock options are monthly-only, so the same (date, symbol, strike, type) quoted for two expiries in one
    # contract month would double a chain. Index options (weeklies) are excluded.
    dup = q(db, """
        select trade_date, symbol, date_trunc('month', expiry) cm, strike, option_type, count(*) n
        from nse.options_eod where instrument in ('OPTSTK', 'STO')
        group by 1, 2, 3, 4, 5 having count(*) > 1 limit 20""")
    assert dup.empty, dup


# ---------------------------------------------------------------- 2. expiry before trade date
@pytest.mark.parametrize("table", ["options_eod", "futures_eod"])
def test_no_expiry_before_trade_date(db, table):
    bad = q(db, f"select trade_date, symbol, expiry from nse.{table} where expiry < trade_date limit 20")
    assert bad.empty, bad


def test_cache_has_no_expiry_before_trade_date(params):
    cache = ROOT / params["base_dir"] / "cache"
    if not (cache / "options.parquet").exists():
        pytest.skip("market cache not built")
    for name in ("options", "futures"):
        df = pd.read_parquet(cache / f"{name}.parquet", columns=["trade_date", "expiry"])
        assert not (pd.to_datetime(df.expiry) < pd.to_datetime(df.trade_date)).any(), name


# ---------------------------------------------------------------- 3. option type
def test_option_type_is_ce_or_pe(db):
    types = set(q(db, "select distinct option_type from nse.options_eod").option_type)
    assert types == {"CE", "PE"}


def test_cache_option_type_is_ce_or_pe(params):
    f = ROOT / params["base_dir"] / "cache" / "options.parquet"
    if not f.exists():
        pytest.skip("market cache not built")
    assert set(pd.read_parquet(f, columns=["option_type"]).option_type.unique()) == {"CE", "PE"}


# ---------------------------------------------------------------- 4. underlying price on signal / expiry days
def _warehouse_symbol(symbols: pd.Series, dates: pd.Series) -> pd.Series:
    """Map current NSE tickers to the warehouse ticker in use on each date (ticker_aliases.csv, up to valid_until)."""
    a = pd.read_csv(ROOT / "data/reference/ticker_aliases.csv", parse_dates=["valid_until"])
    out = symbols.copy()
    for r in a.itertuples():
        out[(symbols == r.nse_symbol) & (dates <= r.valid_until)] = r.warehouse_symbol
    return out


def test_underlying_close_on_every_signal_day(db, params):
    """Every ranked (signal date, name) the strategy could trade has a CM close that day."""
    r = pd.read_csv(ROOT / params["rankings_file"], parse_dates=["expiry_date"])
    r = r[(r.expiry_date >= pd.Timestamp(params["first_signal_date"])) & (r.expiry_date <= pd.Timestamp(params["end_date"]))]
    r["symbol"] = _warehouse_symbol(r.symbol, r.expiry_date)
    cash = q(db, "select trade_date, symbol from nse.cash_eod where close is not null and close > 0 "
                 "and trade_date = any(%s)", (sorted({d.date() for d in r.expiry_date}),))
    have = set(zip(pd.to_datetime(cash.trade_date), cash.symbol))
    missing = r[[(d, s) not in have for d, s in zip(r.expiry_date, r.symbol)]]
    assert len(r) > 1000
    assert missing.empty, missing[["expiry_date", "symbol"]].head(20)


def test_underlying_close_on_every_expiry_day(db):
    """Every stock with open option interest going into expiry has a CM close on expiry day (its settlement price).
    Contracts NSE terminated early (data/calendar/contract_terminations.csv) never reach expiry and are excluded."""
    exp = pd.read_csv(ROOT / "data/calendar/nifty50_monthly_expiries.csv", parse_dates=["expiry_date"])
    term = pd.read_csv(ROOT / "data/calendar/contract_terminations.csv", parse_dates=["last_trading_day"])
    missing = q(db, """
        with e as (select unnest(%s::date[]) expiry_date),
        live as (   -- open interest on the last trading day before expiry, in the expiring month
            select distinct e.expiry_date, o.symbol
            from e
            join lateral (select max(trade_date) d from nse.cash_eod where trade_date < e.expiry_date) p on true
            join nse.options_eod o on o.trade_date = p.d
                 and date_trunc('month', o.expiry) = date_trunc('month', e.expiry_date)
                 and o.instrument in ('OPTSTK', 'STO')      -- index options have no CM close
                 and o.open_interest > 0)
        select l.expiry_date, l.symbol from live l
        left join nse.cash_eod c on c.trade_date = l.expiry_date and c.symbol = l.symbol
        where c.close is null or c.close <= 0
        order by 1, 2""", ([d.date() for d in exp.expiry_date],))
    missing["expiry_date"] = pd.to_datetime(missing.expiry_date)
    ended = {(r.symbol, d) for r in term.itertuples() for d in exp.expiry_date if d > r.last_trading_day}
    missing = missing[[(s, d) not in ended for s, d in zip(missing.symbol, missing.expiry_date)]]
    assert missing.empty, missing.head(20)


# ---------------------------------------------------------------- 5. holidays vs missing files
@pytest.fixture(scope="module")
def ingest(db):
    lg = q(db, "select trade_date, segment, status from nse.ingest_log where trade_date between %s and %s", WINDOW)
    lg["trade_date"] = pd.to_datetime(lg.trade_date)
    return lg.pivot(index="trade_date", columns="segment", values="status")


def test_every_weekday_was_attempted_and_none_errored(ingest):
    weekdays = pd.bdate_range(*WINDOW)
    assert weekdays.difference(ingest.index).empty, weekdays.difference(ingest.index)[:20]
    assert not (ingest == "error").any().any(), ingest[(ingest == "error").any(axis=1)]


def test_missing_files_are_missing_in_both_segments(ingest):
    """A holiday closes the cash and F&O markets together. A file missing in only one segment is a gap."""
    one_sided = ingest[(ingest.cm == "no_file") != (ingest.fo == "no_file")]
    assert set(one_sided.index) == REPAIRED_FO_DAYS, one_sided


def test_repaired_fo_day_has_data(db):
    for d in REPAIRED_FO_DAYS:
        n = q(db, "select count(*) n from nse.options_eod where trade_date = %s", (d.date(),)).n.iloc[0]
        assert n > 0, d


def test_no_file_days_agree_with_independent_index_calendar(ingest, params):
    """Yahoo's NIFTY 50 series (a separate source) must have no close on file-less weekdays, and must not have a
    close on a weekday the exchange files skip. Both ways, so a missed download can't pass as a holiday."""
    idx = pd.read_csv(ROOT / params["nifty_price_index_file"], parse_dates=["trade_date"])
    idx_days = set(idx.trade_date[idx.close.notna()])
    lo, hi = max(ingest.index.min(), idx.trade_date.min()), min(ingest.index.max(), idx.trade_date.max())
    both_missing = {d for d in ingest.index[(ingest.cm == "no_file") & (ingest.fo == "no_file")] if lo <= d <= hi}
    open_days = {d for d in ingest.index[(ingest.cm == "ok")] if lo <= d <= hi}
    assert not (both_missing & idx_days), sorted(both_missing & idx_days)
    weekday_idx = {d for d in idx_days if lo <= d <= hi and d.dayofweek < 5}
    assert not (weekday_idx - open_days), sorted(weekday_idx - open_days)


def test_holiday_count_per_year_is_plausible(ingest):
    """NSE has roughly 10-17 weekday trading holidays a year; far more means downloads were lost."""
    h = ingest[(ingest.cm == "no_file") & (ingest.fo == "no_file")]
    per_year = h.groupby(h.index.year).size()
    full = per_year.loc[2020:2025]
    assert full.between(8, 18).all(), per_year
