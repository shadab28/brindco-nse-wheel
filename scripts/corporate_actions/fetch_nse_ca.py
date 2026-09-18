"""Download NSE corporate-action announcements (JSON) per symbol into data/raw/corporate_actions/nse_api/.

    python scripts/corporate_actions/fetch_nse_ca.py                       # every symbol in the backtest cache
    python scripts/corporate_actions/fetch_nse_ca.py --symbols INFY TCS

Source: https://www.nseindia.com/api/corporates-corporateActions (needs cookies from the corporate-filings
page). One file per (symbol, 365-day window); existing files are kept unless --refresh. Every request is
logged to data/logs/corporate_actions_fetch.csv with the SHA-256 of what was saved.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data/raw/corporate_actions/nse_api"
LOG = ROOT / "data/logs/corporate_actions_fetch.csv"
REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-actions"
URL = ("https://www.nseindia.com/api/corporates-corporateActions?index=equities"
       "&from_date={a}&to_date={b}&symbol={s}")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def _ssl() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs() and Path("/etc/ssl/cert.pem").exists():   # python.org builds can ship an empty store
        ctx = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ctx


class Client:
    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()),
                                              urllib.request.HTTPSHandler(context=_ssl()))
        self.op.addheaders = [("User-Agent", UA), ("Accept-Language", "en-US,en;q=0.9")]

    def warm(self):
        self.op.open(REFERER, timeout=30).read()

    def get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"Referer": REFERER, "Accept": "application/json"})
        return self.op.open(req, timeout=30).read()


def windows(start: dt.date, end: dt.date, days: int = 365):
    a = start
    while a <= end:
        b = min(a + dt.timedelta(days=days - 1), end)
        yield a, b
        a = b + dt.timedelta(days=1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--start", default="2019-10-01")
    ap.add_argument("--end", default="2026-09-16")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--pause", type=float, default=0.7)
    args = ap.parse_args()
    syms = args.symbols or (ROOT / "data/backtest/cache/symbols.txt").read_text().split()
    c = Client()
    c.warm()
    new = not LOG.exists()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    fails = 0
    with open(LOG, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["fetched_at", "symbol", "from", "to", "status", "rows", "sha256", "path", "url"])
        for s in syms:
            for a, b in windows(dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)):
                out = RAW / s / f"{a}_{b}.json"
                if out.exists() and not args.refresh:
                    continue
                url = URL.format(a=a.strftime("%d-%m-%Y"), b=b.strftime("%d-%m-%Y"), s=urllib.parse.quote(s, safe=""))
                now = dt.datetime.now().isoformat(timespec="seconds")
                for attempt in range(3):
                    try:
                        body = c.get(url)
                        data = json.loads(body)
                        break
                    except Exception as e:  # blocked / expired cookie: re-warm and retry
                        err = f"{type(e).__name__}: {e}"
                        time.sleep(2 + 2 * attempt)
                        try:
                            c.warm()
                        except Exception:
                            pass
                else:
                    fails += 1
                    w.writerow([now, s, a, b, "error", "", "", "", url])
                    print(f"{s:12s} {a}..{b} FAILED {err}")
                    continue
                rows = data if isinstance(data, list) else data.get("data", [])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(body)
                w.writerow([now, s, a, b, "ok", len(rows), hashlib.sha256(body).hexdigest(),
                            out.relative_to(ROOT).as_posix(), url])
                fh.flush()
                print(f"{s:12s} {a}..{b} ok rows={len(rows)}")
                time.sleep(args.pause)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
