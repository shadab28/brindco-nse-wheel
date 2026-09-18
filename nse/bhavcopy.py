"""Fetch, cache and normalise NSE end-of-day bhavcopy files.

The bhavcopy is a whole-market file, so one download serves every symbol we
care about. Raw zips are cached on disk under data/raw/<segment>/ and are the
only thing that ever touches the network.

NSE moved both segments to the UDiFF layout on 2024-07-08; both layouts are
read here and normalised to a single schema.
"""

from __future__ import annotations

import io
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

from .universe import canonical

BASE = "https://nsearchives.nseindia.com"
UDIFF_CUTOVER = date(2024, 7, 8)
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
}

FUT_INSTRUMENTS = {"FUTSTK", "FUTIDX", "STF", "IDF"}
OPT_INSTRUMENTS = {"OPTSTK", "OPTIDX", "STO", "IDO"}


class NoFileForDate(Exception):
    """NSE has no bhavcopy for this date (market holiday, or not yet published)."""


# ------------------------------------------------------------------ networking

def url_for(segment: str, d: date) -> str:
    """`segment` is 'cm' (cash) or 'fo' (derivatives)."""
    if d >= UDIFF_CUTOVER:
        tag = "CM" if segment == "cm" else "FO"
        return f"{BASE}/content/{segment}/BhavCopy_NSE_{tag}_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    stamp = f"{d.day:02d}{MONTHS[d.month - 1]}{d.year}"
    folder = "EQUITIES" if segment == "cm" else "DERIVATIVES"
    return (f"{BASE}/content/historical/{folder}/{d.year}/{MONTHS[d.month - 1]}/"
            f"{segment}{stamp}bhav.csv.zip")


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    try:  # warm up cookies; the archive host rejects cold clients
        s.get("https://www.nseindia.com/all-reports", timeout=20)
    except requests.RequestException:
        pass
    return s


def fetch(session: requests.Session, segment: str, d: date,
          cache_dir: Path, retries: int = 3) -> bytes:
    """Return zip bytes for one date, from cache when present.

    Raises NoFileForDate on a 404, RuntimeError on a persistent failure.
    """
    cached = cache_dir / segment / f"{d:%Y-%m-%d}.csv.zip"
    if cached.exists():
        return cached.read_bytes()
    cached.parent.mkdir(parents=True, exist_ok=True)

    url = url_for(segment, d)
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=45)
            if r.status_code == 404:
                raise NoFileForDate(f"{segment} {d}")
            r.raise_for_status()
            if not r.content.startswith(b"PK"):
                raise ValueError("response is not a zip (NSE served an error page)")
            cached.write_bytes(r.content)
            return r.content
        except NoFileForDate:
            raise
        except Exception as exc:  # noqa: BLE001 - transient network / throttling
            if attempt == retries - 1:
                raise RuntimeError(f"{segment} {d}: {exc}") from exc
            time.sleep(1.5 * (attempt + 1))
            session.headers.update(HEADERS)
    raise RuntimeError(f"{segment} {d}: exhausted retries")


def read_zip(blob: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        df = pd.read_csv(io.BytesIO(z.read(name)), low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    return df


# ----------------------------------------------------------------- normalising

def _finish(out: pd.DataFrame, d: date) -> pd.DataFrame:
    out.insert(0, "trade_date", d)
    out["symbol"] = out["source_symbol"].map(canonical)
    return out


def normalise_cash(df: pd.DataFrame, d: date, symbols: set[str]) -> pd.DataFrame:
    """EQ-series rows for the requested symbols, one row per symbol."""
    if "TckrSymb" in df.columns:                                    # UDiFF
        tick, series = df["TckrSymb"], df["SctySrs"]
        ren = {"OpnPric": "open", "HghPric": "high", "LwPric": "low",
               "ClsPric": "close", "LastPric": "last", "PrvsClsgPric": "prev_close",
               "TtlTradgVol": "volume", "TtlTrfVal": "turnover",
               "TtlNbOfTxsExctd": "trades", "ISIN": "isin"}
    else:                                                           # legacy
        tick, series = df["SYMBOL"], df["SERIES"]
        ren = {"OPEN": "open", "HIGH": "high", "LOW": "low", "CLOSE": "close",
               "LAST": "last", "PREVCLOSE": "prev_close",
               "TOTTRDQTY": "volume", "TOTTRDVAL": "turnover",
               "TOTALTRADES": "trades", "ISIN": "isin"}

    tick = tick.astype(str).str.strip()
    mask = tick.isin(symbols) & (series.astype(str).str.strip() == "EQ")
    sub = df.loc[mask].rename(columns=ren)
    if sub.empty:
        return pd.DataFrame()

    out = sub.reindex(columns=list(ren.values())).copy()
    out.insert(0, "source_symbol", tick.loc[mask].values)
    out.insert(1, "series", "EQ")
    for c in ("open", "high", "low", "close", "last", "prev_close",
              "volume", "turnover", "trades"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return _finish(out, d)


def normalise_fo(df: pd.DataFrame, d: date, symbols: set[str]) -> pd.DataFrame:
    """Futures and option rows for the requested symbols."""
    if "TckrSymb" in df.columns:                                    # UDiFF
        tick = df["TckrSymb"].astype(str).str.strip()
        instr = df["FinInstrmTp"].astype(str).str.strip().str.upper()
        mask = tick.isin(symbols) & instr.isin(FUT_INSTRUMENTS | OPT_INSTRUMENTS)
        sub = df.loc[mask]
        if sub.empty:
            return pd.DataFrame()
        out = pd.DataFrame({
            "source_symbol": tick.loc[mask].values,
            "instrument": instr.loc[mask].values,
            "expiry": pd.to_datetime(sub["XpryDt"], errors="coerce").dt.date,
            "strike": pd.to_numeric(sub["StrkPric"], errors="coerce"),
            "option_type": sub["OptnTp"].astype(str).str.strip().str.upper(),
            "open": pd.to_numeric(sub["OpnPric"], errors="coerce"),
            "high": pd.to_numeric(sub["HghPric"], errors="coerce"),
            "low": pd.to_numeric(sub["LwPric"], errors="coerce"),
            "close": pd.to_numeric(sub["ClsPric"], errors="coerce"),
            "settle": pd.to_numeric(sub["SttlmPric"], errors="coerce"),
            "contracts": pd.to_numeric(sub["TtlTradgVol"], errors="coerce"),
            # UDiFF turnover is in rupees; the legacy file was in lakh. Match it.
            "value_lakh": pd.to_numeric(sub["TtlTrfVal"], errors="coerce") / 1e5,
            "open_interest": pd.to_numeric(sub["OpnIntrst"], errors="coerce"),
            "change_in_oi": pd.to_numeric(sub["ChngInOpnIntrst"], errors="coerce"),
        })
    else:                                                           # legacy
        tick = df["SYMBOL"].astype(str).str.strip()
        instr = df["INSTRUMENT"].astype(str).str.strip().str.upper()
        mask = tick.isin(symbols) & instr.isin(FUT_INSTRUMENTS | OPT_INSTRUMENTS)
        sub = df.loc[mask]
        if sub.empty:
            return pd.DataFrame()
        out = pd.DataFrame({
            "source_symbol": tick.loc[mask].values,
            "instrument": instr.loc[mask].values,
            "expiry": pd.to_datetime(sub["EXPIRY_DT"], format="%d-%b-%Y",
                                     errors="coerce").dt.date,
            "strike": pd.to_numeric(sub["STRIKE_PR"], errors="coerce"),
            "option_type": sub["OPTION_TYP"].astype(str).str.strip().str.upper(),
            "open": pd.to_numeric(sub["OPEN"], errors="coerce"),
            "high": pd.to_numeric(sub["HIGH"], errors="coerce"),
            "low": pd.to_numeric(sub["LOW"], errors="coerce"),
            "close": pd.to_numeric(sub["CLOSE"], errors="coerce"),
            "settle": pd.to_numeric(sub["SETTLE_PR"], errors="coerce"),
            "contracts": pd.to_numeric(sub["CONTRACTS"], errors="coerce"),
            "value_lakh": pd.to_numeric(sub["VAL_INLAKH"], errors="coerce"),
            "open_interest": pd.to_numeric(sub["OPEN_INT"], errors="coerce"),
            "change_in_oi": pd.to_numeric(sub["CHG_IN_OI"], errors="coerce"),
        })

    out["option_type"] = out["option_type"].replace({"XX": "", "NAN": "", "": ""})
    out["dte"] = [(e - d).days if pd.notna(e) else None for e in out["expiry"]]
    out["is_option"] = out["instrument"].isin(OPT_INSTRUMENTS)
    return _finish(out.reset_index(drop=True), d)


def split_fo(fo: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (futures, options). Options with no CE/PE tag are dropped as bad rows."""
    if fo.empty:
        return pd.DataFrame(), pd.DataFrame()
    futs = fo[~fo["is_option"]].drop(columns=["is_option", "strike", "option_type"])
    opts = fo[fo["is_option"] & fo["option_type"].isin(["CE", "PE"])
              & fo["strike"].notna()].drop(columns=["is_option"])
    return futs.reset_index(drop=True), opts.reset_index(drop=True)


def weekday_range(start: date, end: date) -> list[date]:
    """Candidate trading days. NSE holidays surface as 404s and get logged."""
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out
