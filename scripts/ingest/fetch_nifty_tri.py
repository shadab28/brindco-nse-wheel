"""Download NIFTY 50 Total Return Index history from niftyindices.com.

Source: niftyindices.com → Reports → Historical Data → Total Return Index
(the page calls POST /BackPage/getTotalReturnIndexString, max one year per request).

Writes data/market/nifty50_tri_daily.csv with columns
trade_date, close (gross TRI), ntr_close (net TRI), source.
Raw JSON responses are kept in data/market/raw/nifty50_tri/.
"""
import json
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
URL = "https://www.niftyindices.com/BackPage/getTotalReturnIndexString"
PAGE = "https://www.niftyindices.com/reports/historical-data"
START, END = date(2016, 1, 1), date(2026, 7, 31)
SOURCE = "niftyindices.com Historical Data - Total Return Index (NIFTY 50, gross TRI)"


def main():
    raw_dir = ROOT / "data/market/raw/nifty50_tri"
    raw_dir.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/128", "Referer": PAGE})
    s.get(PAGE, timeout=60)

    rows, a = [], START
    while a <= END:
        b = min(date(a.year, 12, 31), END)
        info = f"{{'name':'NIFTY 50','startDate':'{a:%d-%b-%Y}','endDate':'{b:%d-%b-%Y}','indexName':'NIFTY 50'}}"
        r = s.post(URL, json={"cinfo": info}, timeout=120)
        r.raise_for_status()
        data = r.json()
        (raw_dir / f"tri_{a:%Y%m%d}_{b:%Y%m%d}.json").write_text(json.dumps(data, indent=1))
        rows += data
        print(a, b, len(data))
        a = b + timedelta(days=1)
        time.sleep(1)

    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "trade_date": pd.to_datetime(df["Date"], format="%d %b %Y"),
        "close": df["TotalReturnsIndex"].astype(float),
        "ntr_close": df["NTR_Value"].astype(float),
    }).drop_duplicates("trade_date").sort_values("trade_date")
    out["source"] = SOURCE
    out.to_csv(ROOT / "data/market/nifty50_tri_daily.csv", index=False, date_format="%Y-%m-%d")
    print(len(out), out.trade_date.min().date(), out.trade_date.max().date())


if __name__ == "__main__":
    main()
