"""Per-contract-series metadata: lot size and instrument token.

Lot size is not a column in the legacy bhavcopy, and the assignment requires
handling lot-size revisions. Two facts make it recoverable exactly:

  * open interest is denominated in SHARES, so it is always an integer multiple
    of the series' lot size -- the GCD of OI across a series' strikes recovers
    the lot;
  * a lot revision applies only to newly introduced series, so lots must be
    derived per (symbol, expiry), not per symbol. Grouping per symbol yields a
    divisor of the two lots and is wrong: TCS in June 2020 shows 250 for the
    June series and 300 for July/August.

Validated against `NewBrdLotQty`, which the UDiFF format supplies directly from
2024-07-08 (RELIANCE 500, TCS 175 both agree).
"""

from __future__ import annotations

from datetime import date
from functools import reduce
from math import gcd

import pandas as pd

STOCK_FO = {"OPTSTK", "FUTSTK", "STO", "STF"}


def _gcd_all(values) -> int:
    vals = [int(v) for v in values if v and v > 0]
    return reduce(gcd, vals) if vals else 0


def extract(df: pd.DataFrame, d: date) -> pd.DataFrame:
    """Per (symbol, expiry) lot size and token for one bhavcopy."""
    if "TckrSymb" in df.columns:                                    # UDiFF
        instr = df["FinInstrmTp"].astype(str).str.strip().str.upper()
        sub = df[instr.isin(STOCK_FO)]
        if sub.empty:
            return pd.DataFrame()
        # FininstrmActlXpryDt is the exchange's authoritative expiry; it differs
        # from XpryDt when an expiry is advanced for a holiday.
        exp_col = ("FininstrmActlXpryDt" if "FininstrmActlXpryDt" in sub.columns
                   else "XpryDt")
        base = pd.DataFrame({
            "symbol": sub["TckrSymb"].astype(str).str.strip(),
            "expiry": pd.to_datetime(sub[exp_col], errors="coerce").dt.date,
            "oi": pd.to_numeric(sub["OpnIntrst"], errors="coerce"),
            "declared_lot": pd.to_numeric(sub.get("NewBrdLotQty"), errors="coerce"),
            "token": sub["FinInstrmId"].astype(str).str.strip(),
        })
    else:                                                           # legacy
        instr = df["INSTRUMENT"].astype(str).str.strip().str.upper()
        sub = df[instr.isin(STOCK_FO)]
        if sub.empty:
            return pd.DataFrame()
        base = pd.DataFrame({
            "symbol": sub["SYMBOL"].astype(str).str.strip(),
            "expiry": pd.to_datetime(sub["EXPIRY_DT"], format="%d-%b-%Y",
                                     errors="coerce").dt.date,
            "oi": pd.to_numeric(sub["OPEN_INT"], errors="coerce"),
            "declared_lot": pd.NA,
            "token": pd.NA,
        })

    base = base[base["expiry"].notna()]
    if base.empty:
        return pd.DataFrame()

    g = base.groupby(["symbol", "expiry"], dropna=False)
    out = g.agg(
        oi_gcd=("oi", _gcd_all),
        n_contracts=("oi", "size"),
        declared_lot=("declared_lot", lambda s: s.dropna().mode().iloc[0]
                      if not s.dropna().empty else pd.NA),
        sample_token=("token", lambda s: s.dropna().iloc[0]
                      if not s.dropna().empty else pd.NA),
    ).reset_index()
    out.insert(0, "trade_date", d)
    return out


def consolidate(daily: pd.DataFrame) -> pd.DataFrame:
    """Collapse per-day observations into one row per (symbol, expiry).

    GCD across every day of a series' life converges on the true lot; a single
    thin day can over-estimate (one contract quoting OI 4800 makes the GCD
    4800), so n_obs is carried to let callers distrust thin series.
    """
    if daily.empty:
        return pd.DataFrame()
    g = daily.groupby(["symbol", "expiry"], dropna=False)
    out = g.agg(
        lot_from_oi=("oi_gcd", _gcd_all),
        declared_lot=("declared_lot", lambda s: s.dropna().mode().iloc[0]
                      if not s.dropna().empty else pd.NA),
        instrument_token=("sample_token", lambda s: s.dropna().iloc[0]
                          if not s.dropna().empty else pd.NA),
        n_obs=("n_contracts", "sum"),
        first_seen=("trade_date", "min"),
        last_seen=("trade_date", "max"),
    ).reset_index()

    # Prefer the exchange's declared lot where the format supplies it.
    out["lot_size"] = out["declared_lot"].fillna(out["lot_from_oi"])
    out["lot_source"] = out["declared_lot"].notna().map(
        {True: "NewBrdLotQty", False: "oi_gcd"})
    out["contract_month"] = out["expiry"].map(lambda e: e.replace(day=1))
    return out
