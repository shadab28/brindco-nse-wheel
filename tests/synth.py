"""Synthetic market for engine tests: controllable spot paths, Black-Scholes option chains, written in the
same parquet layout as the real cache so the real MarketData class is exercised."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from nse.wheel.data import MarketData


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, vol, typ):
    if T <= 0:
        return max(K - S, 0.0) if typ == "PE" else max(S - K, 0.0)
    d1 = (math.log(S / K) + 0.5 * vol * vol * T) / (vol * math.sqrt(T))
    d2 = d1 - vol * math.sqrt(T)
    c = S * _ncdf(d1) - K * _ncdf(d2)
    return c if typ == "CE" else c - S + K


def build(tmp: Path, paths: dict[str, list[float]], start="2022-01-03", n_days=None, lot=100, step=10.0,
          vol=0.30, contracts=10_000, oi_lots=500, lot_overrides=None, zero_contracts=None,
          expiry_every=20, truncate_after=None, splits=None) -> MarketData:
    """paths: symbol -> daily closes. Expiry every `expiry_every` trading days (the last day of each block).
    lot_overrides: {(symbol, cmonth): lot}; zero_contracts: {(date_str, symbol)} days with no option trades.
    splits: {symbol: (day_index, multiplier)} — strikes/prices divide by multiplier from that day."""
    n = n_days or len(next(iter(paths.values())))
    days = pd.bdate_range(start, periods=n)
    exp_idx = list(range(expiry_every - 1, n + 3 * expiry_every, expiry_every))
    all_days = pd.bdate_range(start, periods=exp_idx[-1] + 1)
    expiries = [all_days[i] for i in exp_idx]
    cal = pd.DataFrame({"contract_month": [e.strftime("%Y-%m") for e in expiries], "expiry_date": expiries})
    assert cal.contract_month.is_unique, "synthetic expiries must fall in distinct months"
    cm_of = dict(zip(cal.expiry_date, cal.contract_month))
    cash, fut, opt, lots = [], [], [], []
    lot_overrides = lot_overrides or {}
    zero_contracts = zero_contracts or set()
    splits = splits or {}
    for sym, path in paths.items():
        for i, d in enumerate(days):
            if truncate_after is not None and d > pd.Timestamp(truncate_after):
                break
            S = float(path[i])
            cash.append((d, sym, S))
            live = [e for e in expiries if e >= d][:2]
            for e in live:
                cm = cm_of[e]
                T = max((e - d).days, 0) / 365.0
                fut.append((d, sym, e, S, S, 100))
                lt = lot_overrides.get((sym, cm), lot)
                if sym in splits and i >= splits[sym][0]:
                    lt = int(lt * splits[sym][1])
                lots.append((d.strftime("%Y-%m-%d"), sym, e.strftime("%Y-%m-%d"), lt))
                nc = 0 if (d.strftime("%Y-%m-%d"), sym) in zero_contracts else contracts
                m = splits[sym][1] if sym in splits and i >= splits[sym][0] else 1
                # NSE keeps adjusted strikes after a split: the pre-split grid divided by the multiplier
                lo, hi = math.floor(S * m * 0.7 / step) * step, math.ceil(S * m * 1.3 / step) * step
                for k in np.arange(lo, hi + step / 2, step) / m:
                    k = round(float(k), 4)
                    for typ in ("PE", "CE"):
                        p = round(bs(S, k, T, vol, typ), 2)
                        opt.append((d, sym, e, k, typ, p, p, nc, oi_lots * lt, ))
    tmp.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(cash, columns=["trade_date", "symbol", "close"]).to_parquet(tmp / "cash.parquet")
    pd.DataFrame(fut, columns=["trade_date", "symbol", "expiry", "close", "settle", "contracts"]).to_parquet(
        tmp / "futures.parquet")
    pd.DataFrame(opt, columns=["trade_date", "symbol", "expiry", "strike", "option_type", "close", "settle",
                               "contracts", "open_interest"]).to_parquet(tmp / "options.parquet")
    pd.DataFrame(lots, columns=["trade_date", "symbol", "expiry", "lot"]).to_parquet(tmp / "lots.parquet")
    cal.to_csv(tmp / "calendar.csv", index=False)
    return MarketData(tmp, tmp / "calendar.csv", None)


def cfg(params: dict, **over) -> dict:
    c = dict(params)
    c.update({"initial_capital": 1_000_000, "n_positions": 1, "leverage": 1.0, "margin_financing_rate": 0.0,
              "first_signal_date": "2022-01-03",
              # the synthetic scenarios are built around 5% strikes (put 950 / call 1050 on a 1000 stock);
              # pinned so a strike change in params.yaml doesn't silently move every expected price
              "put_strike_filter_pct": 0.05, "call_strike_filter_pct": 0.05})
    c.update(over)
    return c
