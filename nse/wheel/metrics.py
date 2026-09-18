"""Performance, capital-efficiency and risk metrics for a wheel run (252 days).

Risk-free: idle cash earns the month's India 3M T-bill rate inside the engine, so NAV and CAGR already include it.
Sharpe and Sortino use returns in excess of that same rate (daily rf = annual rate / 252); rf=None means 0%.

Capital definitions (all INR, daily averages over the window):
    NAV                          account equity
    leveraged requirement        margin actually locked = economic exposure / L   ("deployed capital")
    unleveraged requirement      capital that would fully secure the book = economic exposure
                                 (short puts at strike net of their mark, stock at market, covered calls capped)
    notional exposure            Σ strike × qty of short puts + Σ stock market value
Return on deployed capital   = annual P&L / average leveraged requirement
Return on leveraged capital  = annual P&L / average unleveraged requirement (the full capital the levered book controls)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ANN = 252


def daily_rf(index: pd.DatetimeIndex, risk_free: pd.Series | None) -> pd.Series:
    """Per-trading-day risk-free return for each date, from the monthly annual rate (latest earlier month if missing)."""
    if risk_free is None or not len(risk_free):
        return pd.Series(0.0, index=index)
    rf = risk_free.copy()
    rf.index = pd.PeriodIndex(rf.index, freq="M")
    months = pd.PeriodIndex(index, freq="M")
    full = rf.reindex(pd.period_range(min(rf.index.min(), months.min()), max(rf.index.max(), months.max()), freq="M"))
    full = full.ffill().bfill()
    return pd.Series(full.reindex(months).to_numpy() / ANN, index=index)


def nav_stats(nav: pd.Series, risk_free: pd.Series | None = None) -> dict:
    """Return statistics. Equity is floored at zero: once an account is wiped out, percentage returns on a
    negative balance are meaningless (the shortfall owed to the broker is reported in total P&L instead)."""
    nav = nav.dropna().clip(lower=0.0)
    if (nav <= 0).any():
        nav = nav.loc[: nav[nav <= 0].index[0]]
    r = nav.pct_change().dropna()
    ex = r - daily_rf(r.index, risk_free)
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    total = nav.iloc[-1] / nav.iloc[0] - 1
    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1 if nav.iloc[-1] > 0 else -1.0
    dd = nav / nav.cummax() - 1
    downside = np.sqrt(np.mean(np.minimum(ex, 0) ** 2)) * np.sqrt(ANN)
    vol = r.std(ddof=1) * np.sqrt(ANN)
    mdd = dd.min()
    trough = dd.idxmin()
    peak = nav.loc[:trough].idxmax()
    rec = nav.loc[trough:][nav.loc[trough:] >= nav.loc[peak]]
    return {
        "start": nav.index[0].date().isoformat(), "end": nav.index[-1].date().isoformat(), "years": years,
        "total_return": total, "cagr": cagr, "annualized_return": r.mean() * ANN, "annualized_vol": vol,
        "sharpe": (ex.mean() * ANN) / (ex.std(ddof=1) * np.sqrt(ANN)) if vol > 0 else np.nan,
        "sortino": (ex.mean() * ANN) / downside if downside > 0 else np.nan,
        "avg_risk_free": (r - ex).mean() * ANN if len(r) else 0.0,
        "max_drawdown": mdd, "max_dd_peak": peak.date().isoformat(), "max_dd_trough": trough.date().isoformat(),
        "max_dd_recovered": rec.index[0].date().isoformat() if len(rec) else None,
        "calmar": cagr / abs(mdd) if mdd < 0 else np.nan,
        "worst_day": r.min(), "best_day": r.max(),
    }


def monthly_returns(nav: pd.Series) -> pd.Series:
    nav = nav.clip(lower=0.0)
    m = nav.resample("ME").last()
    first = pd.Series([nav.iloc[0]], index=[nav.index[0] - pd.offsets.MonthEnd(1)])
    return pd.concat([first, m]).pct_change().dropna()


def drawdown(nav: pd.Series) -> pd.Series:
    nav = nav.clip(lower=0.0)
    return nav / nav.cummax() - 1


def strategy_metrics(daily: pd.DataFrame, trades: pd.DataFrame, cycles: pd.DataFrame, open_rows: pd.DataFrame,
                     ledger: pd.DataFrame, diag: dict, initial: float, leverage: float,
                     risk_free: pd.Series | None = None) -> dict:
    nav = daily.set_index("date").nav
    out = nav_stats(nav, risk_free)
    mr = monthly_returns(nav)
    years = out["years"]
    total_pnl = nav.iloc[-1] - initial
    closed = trades
    opt = closed[closed.instrument.isin(["PE", "CE"])]
    gp, gl = closed.total_pnl[closed.total_pnl > 0].sum(), closed.total_pnl[closed.total_pnl < 0].sum()
    cyc_closed = cycles[cycles.status != "open"]
    completed = cycles[cycles.status == "completed"]
    fin = -ledger.loc[ledger.kind == "financing", "amount"].sum() if len(ledger) else 0.0
    interest = ledger.loc[ledger.kind == "cash_interest", "amount"].sum() if len(ledger) else 0.0
    avg_nav = nav.mean()
    lev_req = daily.leveraged_requirement.mean()
    unlev_req = daily.unleveraged_requirement.mean()
    annual_pnl = total_pnl / years
    all_opt_rows = pd.concat([opt, open_rows[open_rows.instrument.isin(["PE", "CE"])]]) if len(open_rows) else opt
    notional_sold = (all_opt_rows.strike * all_opt_rows.quantity).sum()
    out.update({
        "leverage": leverage, "initial_capital": initial, "final_nav": nav.iloc[-1], "total_pnl": total_pnl,
        "avg_monthly_return": mr.mean(), "median_monthly_return": mr.median(),
        "best_month": mr.max(), "best_month_date": mr.idxmax().strftime("%Y-%m"),
        "worst_month": mr.min(), "worst_month_date": mr.idxmin().strftime("%Y-%m"),
        "pct_positive_months": (mr > 0).mean(),
        "n_option_trades": len(opt), "option_trade_win_rate": (opt.total_pnl > 0).mean() if len(opt) else np.nan,
        "avg_trade_pnl": closed.total_pnl.mean() if len(closed) else np.nan,
        "avg_option_trade_pnl": opt.total_pnl.mean() if len(opt) else np.nan,
        "profit_factor": gp / abs(gl) if gl < 0 else np.nan,
        "n_cycles_closed": len(cyc_closed),
        "cycle_win_rate": (cyc_closed.total_pnl > 0).mean() if len(cyc_closed) else np.nan,
        "premium_collected": all_opt_rows.premium_inr.sum(),
        "option_pnl": closed.option_pnl.sum() + (open_rows.option_pnl.sum() if len(open_rows) else 0.0),
        "stock_pnl": closed.stock_pnl.sum() + (open_rows.stock_pnl.sum() if len(open_rows) else 0.0),
        "transaction_costs": closed.transaction_costs.sum() + (open_rows.transaction_costs.sum() if len(open_rows) else 0.0),
        "financing_cost": fin, "cash_interest_income": interest,
        "n_puts_sold": int((all_opt_rows.instrument == "PE").sum()),
        "n_calls_sold": int((all_opt_rows.instrument == "CE").sum()),
        "n_assignments": int(diag.get("assignments", 0)),
        "assignment_rate": diag.get("assignments", 0) / max((closed.instrument == "PE").sum(), 1),
        # put-phase price stop (params.yaml put_stop_loss_pct)
        "n_put_stop_losses": int(diag.get("put_stop_losses", 0)),
        "n_put_stop_loss_no_quote": int(diag.get("put_stop_loss_no_quote", 0)),
        "put_stop_loss_pnl": closed[closed.status == "stop_loss"].total_pnl.sum() if len(closed) else 0.0,
        # Step 9a — cash-only assignment: an ITM put is delivered only if free cash covers it
        "n_itm_puts_physically_assigned": int(diag.get("assignments_physical", 0)),
        "n_itm_puts_not_delivered_insufficient_cash": int(diag.get("assignments_cash_rejected", 0)),
        "n_forced_realisations": int(diag.get("assignments_cash_rejected", 0)),
        "forced_realisation_loss": diag.get("forced_realisation_loss_inr", 0.0),
        "assignment_costs": diag.get("assignment_costs_inr", 0.0),
        "cash_rejected_delivery_prevented": diag.get("cash_rejected_inr", 0.0),
        "n_cash_settlement_forced_stock_sales": int(diag.get("cash_settlement_forced_sales", 0)),
        "forced_stock_sale_value": diag.get("cash_settlement_forced_sale_value_inr", 0.0),
        "n_cash_settlement_forced_call_buybacks": int(diag.get("cash_settlement_forced_call_buybacks", 0)),
        "min_free_cash": daily.cash.min(),
        "min_free_cash_date": daily.loc[daily.cash.idxmin(), "date"].date().isoformat() if len(daily) else None,
        "max_cash_utilization": (1 - daily.cash / daily.nav.replace(0, np.nan)).max(),
        "n_calls_exercised": int(diag.get("calls_exercised", 0)),
        "n_completed_wheel_cycles": len(completed),
        "avg_completed_cycle_days": (completed.end - completed.start).dt.days.mean() if len(completed) else np.nan,
        "avg_closed_cycle_days": (cyc_closed.end - cyc_closed.start).dt.days.mean() if len(cyc_closed) else np.nan,
        "n_margin_call_liquidations": int(diag.get("margin_call_liquidations", 0)),
        "margin_breach_days": int(diag.get("margin_breach_days", 0)),
        "avg_margin_utilization": daily.margin_utilization.replace(np.inf, np.nan).mean(),
        "max_margin_utilization": daily.margin_utilization.replace(np.inf, np.nan).max(),
        "max_margin_utilization_date": daily.loc[daily.margin_utilization.replace(np.inf, np.nan).idxmax(), "date"].date().isoformat(),
        "avg_capital_utilization": lev_req / avg_nav,
        "avg_leveraged_requirement": lev_req, "avg_unleveraged_requirement": unlev_req,
        "avg_notional_exposure": daily.notional_exposure.mean(),
        "avg_gross_exposure_x_nav": (daily.notional_exposure / daily.nav).mean(),
        "max_gross_exposure_x_nav": (daily.notional_exposure / daily.nav).replace(np.inf, np.nan).max(),
        "max_borrowed": daily.borrowed.max(), "days_borrowing": int((daily.borrowed > 0).sum()),
        "return_on_deployed_capital": annual_pnl / lev_req if lev_req > 0 else np.nan,
        "return_on_leveraged_capital": annual_pnl / unlev_req if unlev_req > 0 else np.nan,
        "return_on_avg_nav": annual_pnl / avg_nav,
        "annual_premium_turnover_x_nav": all_opt_rows.premium_inr.sum() / years / avg_nav,
        "annual_notional_sold_x_nav": notional_sold / years / avg_nav,
        "pct_days_invested": (daily.n_active_names > 0).mean(),
    })
    return out


def regimes(index_close: pd.Series) -> pd.Series:
    """Daily market regime from trailing NIFTY data: bull = above 200-day SMA and 6-month return > +5%,
    bear = below the SMA and 6-month return < −5%, sideways otherwise."""
    sma = index_close.rolling(200, min_periods=120).mean()
    r6 = index_close.pct_change(126)
    reg = pd.Series("sideways", index=index_close.index)
    reg[(index_close > sma) & (r6 > 0.05)] = "bull"
    reg[(index_close < sma) & (r6 < -0.05)] = "bear"
    return reg
