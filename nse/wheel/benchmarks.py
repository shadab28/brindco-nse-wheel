"""Benchmarks on the same window and capital as the wheel.

nifty_buy_and_hold   NIFTY 50 Total Return Index (niftyindices.com, gross dividends reinvested), bought at the
                     first fill day's close. Kept on the backtest's trading days only, so special sessions in the
                     TRI file (Muhurat, Saturday budget/DR-drill days) are dropped.
equal_weight         the same top-N names the wheel selects at each entry day, equal-weighted at NAV / N, bought at
                     the next day's close (the wheel's fill day) and rebalanced at every entry. Returns are adjusted
                     for splits/bonuses and the value of dividend/rights/demerger adjustments (the same corporate-
                     action table the engine uses). Buys and sells pay delivery costs and 5 bps slippage. Names with no
                     close are exited at their last close (merger/delisting). Idle cash earns the month's
                     risk-free rate, credited monthly, like the wheel.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from nse.wheel.corporate_actions import distribution_per_old_share


def nifty_buy_and_hold(index_file, start, end, capital: float, dates=None) -> pd.Series:
    idx = pd.read_csv(index_file, parse_dates=["trade_date"]).set_index("trade_date").close
    idx = idx[(idx.index >= pd.Timestamp(start)) & (idx.index <= pd.Timestamp(end))]
    if dates is not None:
        idx = idx[idx.index.isin(pd.DatetimeIndex(dates))]
    return capital * idx / idx.iloc[0]


def adjusted_returns(md, ca: pd.DataFrame) -> pd.DataFrame:
    close = md.close
    prev = close.shift(1)
    value_per_old = close.copy()
    for ev in ca.itertuples():
        d = pd.Timestamp(ev.ex_date)
        if d not in close.index or ev.symbol not in close.columns:
            continue
        p0 = prev.at[d, ev.symbol]
        if pd.isna(p0):
            continue
        value_per_old.at[d, ev.symbol] = close.at[d, ev.symbol] * ev.share_multiplier + distribution_per_old_share(ev, p0)
    return value_per_old / prev - 1


def equal_weight(md, costs, ca, selection, entry_days, capital: float, n: int, end,
                 risk_free: pd.Series | None = None) -> pd.Series:
    from nse.wheel.metrics import ANN, daily_rf
    rets = adjusted_returns(md, ca)
    days = [d for d in md.trading_days if entry_days[0] <= d <= pd.Timestamp(end)]
    fills = {md.next_day(d): d for d in entry_days if md.next_day(d) is not None}
    hold: dict[str, float] = {}
    cash = capital
    out = {}
    rf_day = daily_rf(pd.DatetimeIndex(days), risk_free) * ANN if days else None
    accrued, prev = 0.0, None
    bps = lambda d: costs.equity_slippage_bps(d) / 1e4
    for d in days:
        if prev is not None:
            accrued += max(cash, 0.0) * rf_day[prev] * (d - prev).days / 365.0
            if (d.year, d.month) != (prev.year, prev.month) or d == days[-1]:
                cash, accrued = cash + accrued, 0.0
        prev = d
        for s in list(hold):
            r = rets.at[d, s] if s in rets.columns else np.nan
            if pd.isna(r):                      # no price today: exit at the last close
                v = hold.pop(s)
                cash += v * (1 - bps(d)) - costs.equity_sale(d, v).total
            else:
                hold[s] *= 1 + r
        if d in fills:
            names = [s for s in selection(fills[d]) if s in md.close.columns and not pd.isna(md.spot(d, s))][:n]
            nav = cash + sum(hold.values())
            target = {s: nav / n for s in names}
            for s in list(hold):
                delta = target.get(s, 0.0) - hold[s]
                if delta < 0:
                    v = -delta
                    cash += v * (1 - bps(d)) - costs.equity_sale(d, v).total
                    hold[s] -= v
                    if hold[s] <= 1e-6:
                        hold.pop(s)
            for s, tgt in target.items():
                delta = tgt - hold.get(s, 0.0)
                if delta > 0:
                    spend = min(delta, cash)
                    if spend <= 0:
                        continue
                    c = costs.equity_buy(d, spend).total + spend * bps(d)
                    hold[s] = hold.get(s, 0.0) + spend - c
                    cash -= spend
        out[d] = cash + sum(hold.values())
    return pd.Series(out)
