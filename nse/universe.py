"""Symbol universe definitions and historical symbol aliasing."""

from __future__ import annotations

from pathlib import Path

# NIFTY 50 constituents as of 2026-09-16, taken from the NSE index factsheet.
#
# SURVIVORSHIP WARNING: this is a *point-in-time* list of today's index members.
# Ingesting history for it means the panel is biased toward names that survived
# and stayed in the index. For an unbiased backtest, rebuild the universe from
# the historical index-constituent files, or ingest --all-fo-stocks and apply a
# rule-based liquidity filter per rebalance date.
NIFTY_50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY",
    "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]

# Cash-settled index underlyings, useful for Part B of the assignment.
INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]

# Ticker changes inside the 2019-2026 window. The bhavcopy carries whatever
# ticker was live on that trading day, so we fold the old ticker's rows into
# the current one and keep the raw value in `source_symbol` for auditing.
#
# Note these are RENAMES only. Events that changed the economics of the share
# itself -- the HDFC/HDFCBANK merger (Jul 2023) and the TATAMOTORS demerger
# into CV and TMPV (Oct 2025) -- are NOT aliased here, because splicing those
# series would silently fabricate a continuous price history. Handle them
# explicitly in the corporate-action layer.
SYMBOL_ALIASES = {
    "ZOMATO": "ETERNAL",        # renamed 2025 (listed Jul 2021)
    "TATAGLOBAL": "TATACONSUM",  # renamed Feb 2020, same entity
}

# Deliberately NOT aliased -- these changed the economics of the share, so
# splicing the series would fabricate a continuous price history:
#   HDFC       -> HDFCBANK    merger, Jul 2023
#   SRTRANSFIN -> SHRIRAMFIN  Shriram Transport + Shriram City Union, Dec 2022
#   TATAMOTORS -> TATAMOTORS (CV) + TMPV   demerger, Oct 2025
# Each needs an explicit decision in the corporate-action layer.


def resolve(symbols: list[str]) -> list[str]:
    """Expand aliases so we also capture a name's pre-rename bhavcopy rows."""
    wanted = set(s.upper() for s in symbols)
    for old, new in SYMBOL_ALIASES.items():
        if new in wanted:
            wanted.add(old)
    return sorted(wanted)


def canonical(symbol: str) -> str:
    return SYMBOL_ALIASES.get(symbol, symbol)


def load_universe_file(path: Path) -> list[str]:
    """One ticker per line; blank lines and #-comments ignored."""
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.upper())
    if not out:
        raise ValueError(f"no symbols found in {path}")
    return out
