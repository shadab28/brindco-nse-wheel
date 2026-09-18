"""Cash dividends from NSE corporate-action announcements, merged into the F&O-detected corporate actions.

Why a second source: `corporate_actions.detect()` only sees events on which NSE adjusted the F&O contracts
(splits, bonuses, rights, demergers, and dividends NSE treats as extraordinary). Ordinary dividends leave the
contracts untouched, so the holding leg would otherwise never receive them.

Source: data/raw/corporate_actions/nse_api/<SYMBOL>/*.json (scripts/corporate_actions/fetch_nse_ca.py).
Every rupee amount attached to the word "dividend" in an announcement is summed, so a combined
"Special Dividend - Rs 8 /Dividend - Rs 20" pays Rs 28. Announcements that mention a dividend but give no
parseable rupee amount are kept with status UNPARSED and are not paid (never invented).

Merge rule (`merge`), per (symbol, ex-date):
    detected event exists  -> the event keeps its strike/lot mapping; its cash distribution is
                              max(sourced dividend, value implied by the fitted mapping), recorded with both
                              numbers (`_merge_distribution`); a manual override value is never replaced
    no detected event      -> new row, action_type "dividend", contract_adjusted False:
                              cash only; strikes, lots, open orders and economic basis are untouched
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data/raw/corporate_actions/nse_api"

# "Dividend - Rs 20 Per Share", "Interim Dividend-Rs.1.75 Per Share", "Special Dividend - Re 1 Per Share"
# each "dividend" takes the first amount after Rs/Re, or before "per", ahead of the next "dividend"
# (NSE truncates and misspells: "Per Sh", "Per Hsare", "Dividend 15 Per Share", no separator between parts)
_DIV_WORD = re.compile(r"divid\w*", re.I)         # NSE subjects contain typos: "Dividned"
_DIV_AMT = re.compile(r"divid\w*(?:(?!divid)[^\d%])*?(?:\br[se]\.?\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:/-)?\s*per\b)", re.I)


def dividend_amount(subject: str) -> tuple[float, str]:
    """(rupees per share, status) for one announcement subject. status: OK | UNPARSED | NONE."""
    n = len(_DIV_WORD.findall(subject))
    if not n:
        return 0.0, "NONE"
    amounts = [float(m.group(1) or m.group(2)) for m in _DIV_AMT.finditer(subject)]
    if not amounts:
        return 0.0, "UNPARSED"
    return round(sum(amounts), 4), "OK" if len(amounts) == n else "PARTIAL"


def parse_raw(raw: Path = RAW) -> pd.DataFrame:
    """Every announcement under raw/<folder symbol>/*.json, one row each, deduplicated."""
    rows = []
    for f in sorted(raw.glob("*/*.json")):
        data = json.loads(f.read_bytes())
        data = data if isinstance(data, list) else data.get("data", [])
        for r in data:
            amt, status = dividend_amount(r["subject"])
            rows.append({"folder_symbol": f.parent.name, "nse_symbol": r.get("symbol", ""),
                         "isin": r.get("isin", ""), "series": r.get("series", ""),
                         "ex_date": pd.to_datetime(r["exDate"], format="%d-%b-%Y", errors="coerce"),
                         "record_date": r.get("recDate", ""), "subject": " ".join(r["subject"].split()),
                         "dividend_per_share": amt, "dividend_status": status,
                         "raw_file": f.relative_to(ROOT).as_posix()})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return (df.drop_duplicates(["folder_symbol", "isin", "ex_date", "subject"])
              .sort_values(["folder_symbol", "ex_date", "subject"], kind="mergesort").reset_index(drop=True))


def build_dividends(ann: pd.DataFrame, close: pd.DataFrame,
                    aliases_file: Path = ROOT / "data/reference/ticker_aliases.csv") -> pd.DataFrame:
    """Dividends per (warehouse symbol, ex-date), attached only where that symbol traded on the ex-date.

    NSE files a renamed company's whole history under its current ticker. ticker_aliases.csv maps those
    announcements back to the warehouse ticker in use on the ex-date (TMPV -> TATAMOTORS before 2025-10-24).
    """
    d = ann[(ann.dividend_status != "NONE") & ann.ex_date.notna()].copy()
    if aliases_file.exists():
        al = pd.read_csv(aliases_file, parse_dates=["valid_until"])
        for r in al.itertuples():
            m = (d.folder_symbol == r.nse_symbol) & (d.ex_date <= r.valid_until)
            d.loc[m, "folder_symbol"] = r.warehouse_symbol
        d = d.drop_duplicates(["folder_symbol", "ex_date", "subject"])
    d = d[d.series.isin(["EQ", ""])] if "series" in d else d
    out = []
    for (sym, ex), g in d.groupby(["folder_symbol", "ex_date"]):
        traded = sym in close.columns and ex in close.index and pd.notna(close.at[ex, sym])
        statuses = sorted(set(g["dividend_status"]))
        out.append({"symbol": sym, "ex_date": ex, "dividend_per_share": float(g["dividend_per_share"].sum()),
                    "status": "OK" if statuses == ["OK"] else "|".join(statuses),
                    "traded_on_ex_date": traded, "isin": "|".join(sorted(set(g["isin"]))),
                    "subject": " || ".join(g["subject"]), "raw_file": "|".join(sorted(set(g["raw_file"])))})
    res = pd.DataFrame(out)
    return res if res.empty else res.sort_values(["ex_date", "symbol"]).reset_index(drop=True)


def _merge_distribution(ev: pd.Series, prev_close: float, div: float) -> tuple[float, float]:
    """(distribution per old share, implied-by-mapping) for a detected event that shares its ex-date with a dividend.

    The fitted mapping already prices the whole value that left the share (prev − m·(a·prev + b)).
    NSE only adjusts contracts for the extraordinary part, so the sourced dividend is the floor:
    distribution = max(implied, dividend). A split + dividend or rights + dividend day therefore pays the
    dividend once, never twice.
    """
    implied = max(prev_close - ev.share_multiplier * (ev.a * prev_close + ev.b), 0.0) if prev_close == prev_close else 0.0
    return max(implied, div), implied


def merge(ca: pd.DataFrame, div: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """Corporate actions (accepted detections + overrides) with sourced cash dividends folded in."""
    ca = ca.copy()
    if "distribution_per_share" not in ca:
        ca["distribution_per_share"] = np.nan
    ca["contract_adjusted"] = True
    ca["sourced_dividend"] = np.nan
    ca["implied_distribution"] = np.nan
    prev = close.shift(1)
    new = []
    usable = div[(div.traded_on_ex_date) & (div.dividend_per_share > 0)] if len(div) else div
    for r in usable.itertuples():
        hit = ca.index[(ca.symbol == r.symbol) & (ca.ex_date == r.ex_date)]
        if len(hit):
            i = hit[0]
            p0 = prev.at[r.ex_date, r.symbol] if r.ex_date in prev.index else np.nan
            if pd.notna(ca.at[i, "distribution_per_share"]):       # manual override already fixes the value
                ca.at[i, "sourced_dividend"] = r.dividend_per_share
                continue
            dist, implied = _merge_distribution(ca.loc[i], p0, r.dividend_per_share)
            ca.at[i, "distribution_per_share"] = dist
            ca.at[i, "sourced_dividend"] = r.dividend_per_share
            ca.at[i, "implied_distribution"] = implied
        else:
            new.append({"symbol": r.symbol, "ex_date": r.ex_date, "action_type": "dividend", "model": "sourced",
                        "a": 1.0, "b": 0.0, "share_multiplier": 1.0, "qty_multiplier": 1.0,
                        "distribution_per_share": r.dividend_per_share, "sourced_dividend": r.dividend_per_share,
                        "contract_adjusted": False, "source": "nse_corporate_actions", "note": r.subject})
    parts = [x for x in (ca, pd.DataFrame(new)) if len(x)]
    out = pd.concat(parts, ignore_index=True) if parts else ca
    return out.sort_values(["ex_date", "symbol"]).reset_index(drop=True)
