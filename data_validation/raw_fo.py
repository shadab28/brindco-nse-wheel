"""Independent extract of the raw NSE F&O bhavcopy zips (data/raw/fo/), used as ground truth by phases 2-9.

This deliberately does not reuse nse/bhavcopy.py, the warehouse or the backtest cache: it reads the exchange files
directly so those derived layers can be checked against them.

Outputs (data_validation/output/raw_fo/):
    symbol_day.parquet        every stock underlying per trading day: futures / option contract counts and expiries
    contracts_<year>.parquet  every stock future and option row for the backtest symbols, all published fields
    files.csv                 one row per source file: date, layout, rows, sha256
"""
from __future__ import annotations

import hashlib
import io
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "fo"
OUT = ROOT / "data_validation" / "output" / "raw_fo"

LEGACY = {"INSTRUMENT": "instrument", "SYMBOL": "symbol", "EXPIRY_DT": "expiry", "STRIKE_PR": "strike",
          "OPTION_TYP": "option_type", "OPEN": "open", "HIGH": "high", "LOW": "low", "CLOSE": "close",
          "SETTLE_PR": "settle", "CONTRACTS": "contracts", "VAL_INLAKH": "value_lakh", "OPEN_INT": "open_interest",
          "CHG_IN_OI": "change_in_oi", "TIMESTAMP": "trade_date"}
UDIFF = {"FinInstrmTp": "instrument", "TckrSymb": "symbol", "XpryDt": "expiry", "StrkPric": "strike", "OptnTp": "option_type",
         "OpnPric": "open", "HghPric": "high", "LwPric": "low", "ClsPric": "close", "SttlmPric": "settle",
         "TtlTradgVol": "volume_raw", "TtlTrfVal": "value_inr", "OpnIntrst": "open_interest",
         "ChngInOpnIntrst": "change_in_oi", "TradDt": "trade_date", "NewBrdLotQty": "lot_size", "FinInstrmId": "token",
         "FininstrmActlXpryDt": "actual_expiry", "UndrlygPric": "underlying_price", "LastPric": "last"}
STOCK = {"FUTSTK": "FUT", "OPTSTK": "OPT", "STF": "FUT", "STO": "OPT"}


def read_one(path: Path, symbols: frozenset) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    raw = path.read_bytes()
    z = zipfile.ZipFile(io.BytesIO(raw))
    name = z.namelist()[0]
    df = pd.read_csv(z.open(name), low_memory=False)
    layout = "udiff" if "TckrSymb" in df.columns else "legacy"
    df = df.rename(columns=UDIFF if layout == "udiff" else LEGACY)
    df = df[[c for c in df.columns if c in set((UDIFF if layout == "udiff" else LEGACY).values())]]
    df["instrument"] = df.instrument.astype(str).str.strip()
    df = df[df.instrument.isin(STOCK)].copy()
    df["kind"] = df.instrument.map(STOCK)
    df["symbol"] = df.symbol.astype(str).str.strip()
    file_date = pd.Timestamp(path.name[:10])
    df["trade_date"] = pd.to_datetime(df.trade_date, format="mixed", dayfirst=layout == "legacy")
    df["expiry"] = pd.to_datetime(df.expiry, format="mixed", dayfirst=layout == "legacy")
    df["option_type"] = df.option_type.astype(str).str.strip().where(df.kind == "OPT", None)
    df["layout"] = layout
    df["file_date"] = file_date
    if layout == "legacy":
        df["volume_raw"] = df.contracts           # legacy CONTRACTS = number of contracts traded
    else:
        df["contracts"] = pd.NA                    # UDiFF TtlTradgVol unit is established in phase 7
    meta = {"file_date": file_date, "file": str(path.relative_to(ROOT)), "inner_name": name, "layout": layout,
            "rows_all": int(len(z.open(name).read().splitlines()) - 1), "stock_rows": len(df),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "trade_dates_in_file": ",".join(sorted({str(d.date()) for d in df.trade_date.dropna()}))}
    agg = df.groupby(["symbol", "kind"]).agg(n=("expiry", "size"), expiries=("expiry", lambda s: ",".join(
        sorted({str(x.date()) for x in s.dropna()})))).reset_index()
    day = agg.pivot(index="symbol", columns="kind", values=["n", "expiries"])
    day.columns = [f"{a}_{b.lower()}" for a, b in day.columns]
    day = day.reset_index()
    day.insert(0, "trade_date", file_date)
    sub = df[df.symbol.isin(symbols)].copy()
    return meta, day, sub


def build(workers: int = 8) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    symbols = set((ROOT / "data/backtest/cache/symbols.txt").read_text().split())
    symbols |= {"ZOMATO", "TATAGLOBAL", "LTI", "LTIM", "INFRATEL", "INDUSTOWER", "ZEEL", "HDFC", "TMPV", "TMCV"}
    files = sorted(RAW.glob("*.csv.zip"))
    metas, days, subs = [], [], {}
    with ProcessPoolExecutor(workers) as ex:
        for i, (meta, day, sub) in enumerate(ex.map(read_one, files, [frozenset(symbols)] * len(files), chunksize=8)):
            metas.append(meta)
            days.append(day)
            subs.setdefault(meta["file_date"].year, []).append(sub)
            if i % 200 == 0:
                print(f"  {i}/{len(files)} {meta['file_date'].date()}", flush=True)
    pd.DataFrame(metas).to_csv(OUT / "files.csv", index=False)
    pd.concat(days, ignore_index=True).to_parquet(OUT / "symbol_day.parquet")
    for y, parts in subs.items():
        d = pd.concat(parts, ignore_index=True)
        for c in ("strike", "open", "high", "low", "close", "settle", "last", "underlying_price", "value_lakh", "value_inr"):
            if c in d:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        for c in ("contracts", "volume_raw", "open_interest", "change_in_oi", "lot_size", "token"):
            if c in d:
                d[c] = pd.to_numeric(d[c], errors="coerce").astype("Int64")
        d.to_parquet(OUT / f"contracts_{y}.parquet")
        print(f"  {y}: {len(d):,} rows", flush=True)


def contracts(years=None, columns=None) -> pd.DataFrame:
    files = sorted(OUT.glob("contracts_*.parquet"))
    if years:
        files = [f for f in files if int(f.stem[-4:]) in set(years)]
    return pd.concat([pd.read_parquet(f, columns=columns) for f in files], ignore_index=True)


if __name__ == "__main__":
    build(int(sys.argv[1]) if len(sys.argv) > 1 else 8)
