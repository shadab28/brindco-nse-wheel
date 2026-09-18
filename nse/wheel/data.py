"""Market data for the wheel backtest, loaded once from Postgres and cached as parquet.

What is loaded (all unadjusted, as traded):
    cash      nse.cash_eod close                          -> spot, stock marks, equity exits; FSP on expiry day
    futures   nse.futures_eod settle of the expiring month -> FSP cross-check on expiry day; F&O-listed flag

Final settlement price (FSP): NSE settles stock options on the closing price of the underlying in the CM
segment on expiry day, so `fsp` is the CM close. The expiring future's settle is kept as `fut_fsp`, used by
`fsp_check` and as a fallback only when the CM close is missing.
    options   nse.options_eod, the two nearest monthly contracts, strikes within 30%-300% of spot (deep ITM
              puts after a crash must stay markable)
    lots      data/lots/lot_daily.csv.gz (per trade_date, symbol, contract)
    calendar  data/calendar/nifty50_monthly_expiries.csv (expiries read from contract data)

Contracts are keyed by contract month ("YYYY-MM"), never by the raw expiry stamp: the Jun 2023 series
was quoted as 2023-06-29 and settled 2023-06-28 (README).

Look-ahead: this module only stores data. Every accessor takes the date being asked about and returns
that day's rows; the engine never asks for a date later than the one it is processing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]

# Ranking symbols that trade under a different warehouse ticker for part of the window.
# TMPV (Tata Motors Passenger Vehicles) is the renamed TATAMOTORS listing from 2025-10-24; the 15m
# ranking data carries the whole history under TMPV.
RANK_TO_DB = [("TMPV", "TATAMOTORS", "2025-10-24")]   # (rank symbol, db symbol, db symbol valid before)


def db_symbol(rank_symbol: str, date) -> str:
    for rs, dbs, until in RANK_TO_DB:
        if rank_symbol == rs and pd.Timestamp(date) < pd.Timestamp(until):
            return dbs
    return rank_symbol


def _dsn() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env = ROOT / ".env"
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("DATABASE_URL not set")


def _query(sql: str, params=None) -> pd.DataFrame:
    import warnings

    import psycopg2
    with psycopg2.connect(_dsn()) as con, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.read_sql(sql, con, params=params)


def build_cache(symbols: list[str], start: str, end: str, cache_dir: Path, lot_daily_file: Path) -> None:
    """Pull everything the engine needs for `symbols` into parquet files under cache_dir."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    syms = sorted(set(symbols))
    cash = _query("select trade_date, symbol, close from nse.cash_eod "
                  "where symbol = any(%s) and trade_date between %s and %s", (syms, start, end))
    cash.to_parquet(cache_dir / "cash.parquet")
    fut = _query("select trade_date, symbol, expiry, close, settle, contracts from nse.futures_eod "
                 "where symbol = any(%s) and trade_date between %s and %s", (syms, start, end))
    fut.to_parquet(cache_dir / "futures.parquet")
    frames = []
    for s in syms:
        o = _query("""
            select o.trade_date, o.symbol, o.expiry, o.strike, o.option_type, o.close, o.settle,
                   o.contracts, o.open_interest
            from nse.options_eod o join nse.cash_eod c on c.trade_date = o.trade_date and c.symbol = o.symbol
            where o.symbol = %s and o.trade_date between %s and %s
              and o.expiry <= o.trade_date + 75
              and o.strike between 0.3 * c.close and 3.0 * c.close""", (s, start, end))
        frames.append(o)
        print(f"  options {s}: {len(o):,}", flush=True)
    opt = pd.concat(frames, ignore_index=True)
    opt.to_parquet(cache_dir / "options.parquet")
    lots = pd.read_csv(lot_daily_file, usecols=["trade_date", "symbol", "expiry", "lot"])
    lots = lots[lots.symbol.isin(syms) & (lots.trade_date >= start) & (lots.trade_date <= end)]
    lots.to_parquet(cache_dir / "lots.parquet")
    (cache_dir / "symbols.txt").write_text("\n".join(syms))


@dataclass
class Quote:
    close: float
    settle: float
    contracts: int
    oi_shares: int

    @property
    def mark(self) -> float:
        """Close if it traded today, else the exchange settlement price (README caveat 1)."""
        return self.close if self.contracts > 0 and self.close > 0 else self.settle


class MarketData:
    def __init__(self, cache_dir: Path, expiry_file: Path, terminations_file: Path | None = None):
        cal = pd.read_csv(expiry_file, parse_dates=["expiry_date"])
        self.calendar = cal[["contract_month", "expiry_date"]].sort_values("expiry_date").reset_index(drop=True)
        self.expiry_of = dict(zip(self.calendar.contract_month, self.calendar.expiry_date))
        self.expiry_dates = set(self.calendar.expiry_date)
        self.month_of_expiry = dict(zip(self.calendar.expiry_date, self.calendar.contract_month))

        cash = pd.read_parquet(cache_dir / "cash.parquet")
        cash["trade_date"] = pd.to_datetime(cash.trade_date)
        self.close = cash.pivot(index="trade_date", columns="symbol", values="close").astype(float).sort_index()

        fut = pd.read_parquet(cache_dir / "futures.parquet")
        fut["trade_date"] = pd.to_datetime(fut.trade_date)
        fut["cmonth"] = pd.to_datetime(fut.expiry).dt.strftime("%Y-%m")
        self.fo_listed = set(zip(fut.trade_date, fut.symbol))
        on_exp = fut[fut.trade_date.isin(self.expiry_dates)].copy()
        on_exp = on_exp[on_exp.cmonth == on_exp.trade_date.map(self.month_of_expiry)]
        self.fut_fsp = {(r.trade_date, r.symbol): float(r.settle) for r in on_exp.itertuples()}
        exp_close = self.close[self.close.index.isin(self.expiry_dates)].stack().dropna()
        self.fsp = {(d, s): float(v) for (d, s), v in exp_close.items() if (d, s) in self.fo_listed}
        self.fut_settle = {(r.trade_date, r.symbol, r.cmonth): float(r.settle) for r in fut.itertuples()}

        opt = pd.read_parquet(cache_dir / "options.parquet")
        opt["trade_date"] = pd.to_datetime(opt.trade_date)
        opt["cmonth"] = pd.to_datetime(opt.expiry).dt.strftime("%Y-%m")
        for c in ("strike", "close", "settle"):
            opt[c] = opt[c].astype(float)
        opt["contracts"] = opt.contracts.fillna(0).astype(np.int64)
        opt["open_interest"] = opt.open_interest.fillna(0).astype(np.int64)
        opt["close"] = opt.close.fillna(0.0)
        opt["settle"] = opt.settle.fillna(0.0)
        opt = opt.sort_values(["symbol", "cmonth", "option_type", "trade_date", "strike"]).reset_index(drop=True)
        key = opt.symbol + "|" + opt.cmonth + "|" + opt.option_type + "|" + opt.trade_date.dt.strftime("%Y-%m-%d")
        codes, uniq = pd.factorize(key, sort=False)
        starts = np.r_[0, np.flatnonzero(np.diff(codes)) + 1]
        ends = np.r_[starts[1:], len(codes)]
        self._slices = {uniq[codes[s]]: (s, e) for s, e in zip(starts, ends)}
        self._strike = opt.strike.to_numpy()
        self._close = opt.close.to_numpy()
        self._settle = opt.settle.to_numpy()
        self._contracts = opt.contracts.to_numpy()
        self._oi = opt.open_interest.to_numpy()
        self.opt_dates = set(opt.trade_date.unique())

        lots = pd.read_parquet(cache_dir / "lots.parquet")
        lots["cmonth"] = pd.to_datetime(lots.expiry).dt.strftime("%Y-%m")
        self.lot = {(pd.Timestamp(r.trade_date), r.symbol, r.cmonth): int(r.lot) for r in lots.itertuples()}

        self.trading_days = sorted(set(self.close.index) | self.opt_dates)
        self.day_index = {d: i for i, d in enumerate(self.trading_days)}

        self.terminations: dict[tuple, str] = {}
        self.termination_by_symbol: dict[str, tuple] = {}     # symbol -> (announcement date, last trading day)
        if terminations_file is not None and Path(terminations_file).exists():
            t = pd.read_csv(terminations_file, parse_dates=["announcement_date", "last_trading_day"])
            self.terminations = {(r.last_trading_day, r.symbol): r.reason for r in t.itertuples()}
            self.termination_by_symbol = {r.symbol: (r.announcement_date, r.last_trading_day) for r in t.itertuples()}

    # ---------------------------------------------------------------- calendar
    def contract_after(self, date) -> str | None:
        """Nearest monthly contract whose expiry is strictly after `date`."""
        after = self.calendar[self.calendar.expiry_date > pd.Timestamp(date)]
        return None if after.empty else after.contract_month.iloc[0]

    def is_expiry(self, date) -> bool:
        return pd.Timestamp(date) in self.expiry_dates

    def next_day(self, date):
        i = self.day_index[pd.Timestamp(date)]
        return self.trading_days[i + 1] if i + 1 < len(self.trading_days) else None

    def offset_day(self, date, n: int):
        i = self.day_index[pd.Timestamp(date)] + n
        return self.trading_days[i] if 0 <= i < len(self.trading_days) else None

    # ---------------------------------------------------------------- prices
    def spot(self, date, symbol) -> float | None:
        try:
            v = self.close.at[pd.Timestamp(date), symbol]
        except KeyError:
            return None
        return None if pd.isna(v) else float(v)

    def month_end_spot(self, date, symbol) -> float | None:
        """Close on the last trading day of the previous calendar month (tick-band reference)."""
        d = pd.Timestamp(date)
        prev = self.close.loc[: d.replace(day=1) - pd.Timedelta(days=1), symbol].dropna() \
            if symbol in self.close else pd.Series(dtype=float)
        return float(prev.iloc[-1]) if len(prev) else self.spot(date, symbol)

    def is_fo_listed(self, date, symbol) -> bool:
        return (pd.Timestamp(date), symbol) in self.fo_listed

    def chain(self, date, symbol, cmonth, option_type) -> pd.DataFrame:
        k = f"{symbol}|{cmonth}|{option_type}|{pd.Timestamp(date):%Y-%m-%d}"
        if k not in self._slices:
            return pd.DataFrame(columns=["strike", "close", "settle", "contracts", "oi_shares"])
        s, e = self._slices[k]
        return pd.DataFrame({"strike": self._strike[s:e], "close": self._close[s:e], "settle": self._settle[s:e],
                             "contracts": self._contracts[s:e], "oi_shares": self._oi[s:e]})

    def quote(self, date, symbol, cmonth, option_type, strike) -> Quote | None:
        k = f"{symbol}|{cmonth}|{option_type}|{pd.Timestamp(date):%Y-%m-%d}"
        if k not in self._slices:
            return None
        s, e = self._slices[k]
        i = s + int(np.searchsorted(self._strike[s:e], strike - 1e-6))
        if i >= e or abs(self._strike[i] - strike) > 1e-6:
            return None
        return Quote(float(self._close[i]), float(self._settle[i]), int(self._contracts[i]), int(self._oi[i]))

    def fsp_check(self) -> pd.DataFrame:
        """CM close vs expiring-future settle on every expiry day where either exists.

        One row per (expiry, symbol): both prices, the difference, and `oi_strikes_between` = expiring option
        strikes with open interest on the day before expiry that lie strictly between the two prices, i.e.
        whose ITM/OTM status (assignment / call-away) depends on which price is used.
        """
        rows = []
        for d, s in sorted(set(self.fsp) | set(self.fut_fsp)):
            cm, fut = self.fsp.get((d, s)), self.fut_fsp.get((d, s))
            both = cm is not None and fut is not None
            flips = 0
            prev = self.offset_day(d, -1)
            if both and cm != fut and prev is not None:
                lo, hi = min(cm, fut), max(cm, fut)
                for ot in ("CE", "PE"):
                    k = f"{s}|{self.month_of_expiry[d]}|{ot}|{prev:%Y-%m-%d}"
                    if k in self._slices:
                        a, b = self._slices[k]
                        ks = self._strike[a:b]
                        flips += int(((ks > lo) & (ks < hi) & (self._oi[a:b] > 0)).sum())
            rows.append({"expiry": d, "symbol": s, "cm_close": cm, "future_settle": fut,
                         "diff": fut - cm if both else None,
                         "diff_bps": (fut - cm) / cm * 1e4 if both else None,
                         "oi_strikes_between": flips})
        return pd.DataFrame(rows)

    def lot_size(self, date, symbol, cmonth) -> int | None:
        return self.lot.get((pd.Timestamp(date), symbol, cmonth))


def load_market(cache_dir: Path, expiry_file: Path, terminations_file: Path | None) -> MarketData:
    """MarketData, pickled next to the parquet cache (rebuilt when any input is newer than the pickle)."""
    import pickle
    pkl = cache_dir / "market.pkl"
    inputs = [*cache_dir.glob("*.parquet"), Path(expiry_file)] + ([Path(terminations_file)] if terminations_file else [])
    if pkl.exists() and all(p.stat().st_mtime < pkl.stat().st_mtime for p in inputs):
        with open(pkl, "rb") as fh:
            return pickle.load(fh)
    md = MarketData(cache_dir, expiry_file, terminations_file)
    with open(pkl, "wb") as fh:
        pickle.dump(md, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return md
