"""Per-symbol daily F&O activity aggregates, for point-in-time universe ranking.

Ranking candidates needs turnover and open interest per name, not every strike,
so we aggregate the whole F&O stock pool (~220 names) into one row per
(date, symbol) rather than ingesting ~88M option rows we would never query.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

STOCK_OPT = {"OPTSTK", "STO"}
STOCK_FUT = {"FUTSTK", "STF"}


def aggregate(df: pd.DataFrame, d: date) -> pd.DataFrame:
    """Collapse one F&O bhavcopy into per-stock activity. Indices are excluded:
    the wheel's delivery mechanics only exist on physically settled stocks."""
    if "TckrSymb" in df.columns:                                    # UDiFF
        sym = df["TckrSymb"].astype(str).str.strip()
        instr = df["FinInstrmTp"].astype(str).str.strip().str.upper()
        turnover = pd.to_numeric(df["TtlTrfVal"], errors="coerce") / 1e7   # INR -> crore
        contracts = pd.to_numeric(df["TtlTradgVol"], errors="coerce")
        oi = pd.to_numeric(df["OpnIntrst"], errors="coerce")
        strike = pd.to_numeric(df["StrkPric"], errors="coerce")
    else:                                                           # legacy
        sym = df["SYMBOL"].astype(str).str.strip()
        instr = df["INSTRUMENT"].astype(str).str.strip().str.upper()
        turnover = pd.to_numeric(df["VAL_INLAKH"], errors="coerce") / 100  # lakh -> crore
        contracts = pd.to_numeric(df["CONTRACTS"], errors="coerce")
        oi = pd.to_numeric(df["OPEN_INT"], errors="coerce")
        strike = pd.to_numeric(df["STRIKE_PR"], errors="coerce")

    base = pd.DataFrame({"symbol": sym, "instr": instr, "turnover": turnover,
                         "contracts": contracts, "oi": oi, "strike": strike})

    opts = base[base["instr"].isin(STOCK_OPT)]
    futs = base[base["instr"].isin(STOCK_FUT)]
    if opts.empty:
        return pd.DataFrame()

    g = opts.groupby("symbol")
    out = pd.DataFrame({
        "opt_turnover_cr": g["turnover"].sum(),
        "opt_contracts": g["contracts"].sum(),
        "opt_oi": g["oi"].sum(),
        "n_strikes": g["strike"].nunique(),
        # Breadth of *tradeable* chain: a name can quote 80 strikes and trade 3.
        "n_traded_strikes": opts[opts["contracts"] > 0].groupby("symbol")["strike"].nunique(),
    })
    if not futs.empty:
        fg = futs.groupby("symbol")
        out["fut_turnover_cr"] = fg["turnover"].sum()
        out["fut_oi"] = fg["oi"].sum()

    out = out.reset_index()
    out.insert(0, "trade_date", d)
    out["n_traded_strikes"] = out["n_traded_strikes"].fillna(0)
    return out
