"""Phase 0 — inventory every input the wheel backtest reads, with source, provenance class and date range."""
from __future__ import annotations

import re
import sqlite3

import pandas as pd
import yaml

from data_validation.common import REPORTS, ROOT, PhaseResult, query

BACKTEST_CODE = ["nse/wheel/data.py", "nse/wheel/runner.py", "nse/wheel/engine.py", "nse/wheel/selection.py",
                 "nse/wheel/costs.py", "nse/wheel/corporate_actions.py", "nse/wheel/benchmarks.py",
                 "nse/wheel/metrics.py", "scripts/wheel/backtest.py"]

# Every input, what it holds, where it comes from. `klass`: NSE-OFFICIAL (exchange file as published),
# NSE-DERIVED (computed only from NSE official files), NSE-MANUAL (hand-transcribed from NSE documents),
# VENDOR (third-party data), ASSUMPTION (modelling choice, no external data).
INVENTORY = [
    # path/table, categories, klass, source, used_by, date_col
    ("nse.cash_eod", "underlying equity (EOD close)", "NSE-OFFICIAL",
     "NSE CM bhavcopy: legacy cmDDMMMYYYYbhav.csv.zip (<2024-07-08), UDiFF BhavCopy_NSE_CM (>=2024-07-08); cached zips data/raw/cm/",
     "build_cache -> cache/cash.parquet", "trade_date"),
    ("nse.futures_eod", "F&O eligibility (futures listed), settlement (futures settle on expiry = FSP)", "NSE-OFFICIAL",
     "NSE F&O bhavcopy (FUTSTK / STF rows); cached zips data/raw/fo/", "build_cache -> cache/futures.parquet", "trade_date"),
    ("nse.options_eod", "option OHLC, settle, volume (contracts), OI, expiry, listed strikes", "NSE-OFFICIAL",
     "NSE F&O bhavcopy (OPTSTK / STO rows); cached zips data/raw/fo/; 2021-03-30 reconstructed (data/raw/fo_mkt/)",
     "build_cache -> cache/options.parquet (2 nearest months, strikes 30%-300% of spot)", "trade_date"),
    ("data/raw/cm", "underlying equity (raw)", "NSE-OFFICIAL", "nsearchives.nseindia.com CM bhavcopy zips", "ingest -> nse.cash_eod", None),
    ("data/raw/fo", "options / futures (raw)", "NSE-OFFICIAL", "nsearchives.nseindia.com F&O bhavcopy zips", "ingest -> nse.options_eod, futures_eod; lot_sizes.py", None),
    ("data/raw/fo_mkt", "2021-03-30 F&O gap", "NSE-DERIVED", "neighbouring NSE zips + reconstructed bhavcopy (manual recovery)", "ingest", None),
    ("data/backtest/cache/cash.parquet", "underlying equity", "NSE-OFFICIAL", "extract of nse.cash_eod", "MarketData.spot / marks / FSP fallback", "trade_date"),
    ("data/backtest/cache/futures.parquet", "F&O eligibility, settlement", "NSE-OFFICIAL", "extract of nse.futures_eod", "MarketData.is_fo_listed, fsp", "trade_date"),
    ("data/backtest/cache/options.parquet", "option close/settle/contracts/OI, strikes, expiry", "NSE-OFFICIAL", "extract of nse.options_eod", "MarketData.chain / quote", "trade_date"),
    ("data/backtest/cache/lots.parquet", "lot size (per trade date, contract)", "NSE-OFFICIAL + NSE-DERIVED", "extract of data/lots/lot_daily.csv.gz", "MarketData.lot_size", "trade_date"),
    ("data/backtest/cache/market.pkl", "pickled MarketData of the parquet files above", "NSE-DERIVED", "load_market()", "every run", None),
    ("data/lots/lot_daily.csv.gz", "lot size per (trade_date, symbol, expiry)", "NSE-OFFICIAL + NSE-DERIVED",
     "UDiFF NewBrdLotQty (>=2024-07-08, exchange-published); before: futures value/(contracts x close) snapped to a divisor of the "
     "series' open-interest GCD (scripts/wheel/lot_sizes.py); cross-checked vs NSE circulars FAOP47856, FAOP53920 and fo_mktlots.csv",
     "build_cache -> cache/lots.parquet", "trade_date"),
    ("data/calendar/nifty50_monthly_expiries.csv", "expiry calendar", "NSE-DERIVED", "expiries observed in F&O bhavcopy contracts", "MarketData calendar, rankings", "expiry_date"),
    ("data/calendar/contract_terminations.csv", "corporate actions: merger / demerger contract terminations", "NSE-MANUAL",
     "NSE/FAOP/70615 (TATAMOTORS); HDFC from futures_eod settlement; announcement dates partly ASSUMED", "MarketData.terminations", "last_trading_day"),
    ("data/backtest/corporate_actions_detected.csv", "corporate actions: F&O strike/lot adjustments", "NSE-DERIVED",
     "detected from strike-grid and OI jumps between consecutive F&O bhavcopies (nse/wheel/corporate_actions.detect)", "engine corporate actions", "ex_date"),
    ("data/calendar/corporate_action_overrides.csv", "corporate actions: demerger value adjustments", "NSE-MANUAL + ASSUMPTION",
     "manual; distribution values from special pre-open price discovery (ASSUMED realised)", "engine corporate actions", "ex_date"),
    ("data/signals/expiry_rankings.csv", "ranking", "VENDOR-DERIVED",
     "scripts/wheel/expiry_rankings.py on Zerodha Kite 15m bars (data/market/nifty50_15m.db) + NIFTY 50 master list", "RankedSelection", "expiry_date"),
    ("data/market/nifty50_15m.db", "underlying equity (15m, split/bonus adjusted) for ranking", "VENDOR",
     "Zerodha Kite Connect historical API (not rebuildable without an account)", "expiry_rankings.py", None),
    ("data/universe/nifty50_master_list.csv", "NIFTY 50 constituents (periods per symbol)", "NSE-MANUAL",
     "nse/nifty50_history.py: live ind_nifty50list.csv + hand-transcribed NSE Indices change announcements, rolled back", "expiry_rankings.py (members on each expiry)", None),
    ("data/universe/nifty50_constituents_daily.csv", "NIFTY 50 constituents (daily)", "NSE-MANUAL", "same as master list, checked against each day's CM bhavcopy", "reference only", "trade_date"),
    ("data/universe/nifty50_changes.csv", "NIFTY 50 constituent changes", "NSE-MANUAL", "nse/nifty50_history.CHANGES", "reference only", "effective_date"),
    ("data/universe/sector_map.csv", "sector per symbol", "ASSUMPTION", "hand-mapped NSE industry classification, status ASSUMED", "reporting / concentration", None),
    ("data/costs/wheel_charges_schedule.csv", "transaction charges", "NSE-MANUAL", "NSE/SEBI circulars listed in data/costs/charge_sources.csv", "CostModel", "from"),
    ("data/costs/slippage_schedule.csv", "slippage", "ASSUMPTION", "modelling assumption (ticks / pct)", "CostModel", "from"),
    ("data/market/india_rf_3m_tbill_monthly.csv", "risk-free rate", "VENDOR", "OECD IR3TIB (India 3M T-bill)", "engine cash interest, Sharpe", None),
    ("data/market/nifty50_tri_daily.csv", "NIFTY 50 Total Return Index (benchmark)", "NSE-OFFICIAL", "niftyindices.com Historical Data - Total Return Index (scripts/ingest/fetch_nifty_tri.py)", "benchmarks", "trade_date"),
    ("data/market/nifty50_index_daily.csv", "NIFTY 50 price index (regimes)", "VENDOR", "Yahoo Finance ^NSEI", "regimes / FSP check", "trade_date"),
    ("params.yaml", "strategy parameters", "ASSUMPTION", "project configuration", "runner", None),
]

REQUESTED = {
    "underlying equity data": ["nse.cash_eod", "data/backtest/cache/cash.parquet"],
    "NIFTY 50 constituent data": ["data/universe/nifty50_master_list.csv"],
    "F&O eligibility data": ["nse.futures_eod", "data/backtest/cache/futures.parquet"],
    "option OHLC data": ["nse.options_eod"],
    "option volume": ["nse.options_eod", "data/backtest/cache/options.parquet"],
    "option OI": ["nse.options_eod", "data/backtest/cache/options.parquet"],
    "expiry data": ["data/calendar/nifty50_monthly_expiries.csv"],
    "strike data": ["nse.options_eod", "data/backtest/cache/options.parquet"],
    "lot-size data": ["data/lots/lot_daily.csv.gz", "data/backtest/cache/lots.parquet"],
    "corporate-action data": ["data/backtest/corporate_actions_detected.csv", "data/calendar/corporate_action_overrides.csv",
                              "data/calendar/contract_terminations.csv"],
    "settlement data": ["nse.futures_eod", "data/backtest/cache/futures.parquet"],
    "ranking data": ["data/signals/expiry_rankings.csv"],
}


def referenced_inputs() -> set[str]:
    """Files the backtest code reads: params *_file keys used in code, literal data/ paths, and the cache."""
    p = yaml.safe_load(open(ROOT / "params.yaml"))
    flat = {**{k: v for k, v in p.items() if k != "backtest"}, **p["backtest"]}
    code = "\n".join((ROOT / f).read_text() for f in BACKTEST_CODE)
    out = {"params.yaml"}
    for k, v in flat.items():
        if isinstance(v, str) and (k.endswith("_file") or k == "base_dir") and re.search(rf"\[\"{k}\"\]", code):
            out.add(v if k != "base_dir" else f"{v}/corporate_actions_detected.csv")
    out |= set(re.findall(r"\"(data/[\w/.\-]+)\"", code))
    out |= {f"data/backtest/cache/{f}" for f in ("cash.parquet", "futures.parquet", "options.parquet", "lots.parquet",
                                                  "market.pkl")}
    return out, {k: v for k, v in flat.items() if isinstance(v, str) and k.endswith("_file")
                 and not re.search(rf"\[\"{k}\"\]", code)}


def date_range(name, col):
    if name.startswith("nse."):
        r = query(f"select min(trade_date) a, max(trade_date) b, count(*) n, count(distinct symbol) s from {name}")
        return str(r.a[0]), str(r.b[0]), int(r.n[0]), int(r.s[0])
    path = ROOT / name
    if path.is_dir():
        days = sorted(x.name[:10] for x in path.iterdir() if x.name[:4].isdigit())
        return (days[0] if days else None), (days[-1] if days else None), len(list(path.iterdir())), None
    if name.endswith(".parquet"):
        d = pd.read_parquet(path, columns=[col, "symbol"])
        c = pd.to_datetime(d[col])
        return str(c.min().date()), str(c.max().date()), len(d), d.symbol.nunique()
    if name.endswith(".db"):
        con = sqlite3.connect(path)
        a, b, n, s = con.execute("select min(trade_date), max(trade_date), count(*), count(distinct symbol) from ohlcv_15m").fetchone()
        return a, b, n, s
    if name.endswith(".csv") or name.endswith(".csv.gz"):
        d = pd.read_csv(path)
        n, s = len(d), (d.symbol.nunique() if "symbol" in d else None)
        if col:
            c = pd.to_datetime(d[col], errors="coerce")
            return str(c.min().date()), str(c.max().date()), n, s
        if "month" in d:
            return d.month.min(), d.month.max(), n, s
        if "in_index_periods" in d:
            parts = d.in_index_periods.str.split(r"[;|]").explode().str.split(r"\.\.")
            return min(x[0] for x in parts), max(x[1] for x in parts), n, s
        return None, None, n, s
    return None, None, None, None


def run() -> PhaseResult:
    res = PhaseResult(0, "Inventory", "Inventory",
                      sources="Static scan of the backtest code (" + ", ".join(BACKTEST_CODE) + "), params.yaml, the "
                              "files on disk and the Postgres warehouse `nse` (127.0.0.1:5440).",
                      methodology="Every params `*_file` key the code reads, every literal `data/...` path in the code and the "
                                  "parquet cache are collected and matched against the inventory. Date ranges and row counts "
                                  "are computed from the data itself. Provenance class is assigned from the producing script "
                                  "and its docstring.")
    used, unused_params = referenced_inputs()
    inv = {name: dict(categories=c, klass=k, source=s, used_by=u, date_col=dc) for name, c, k, s, u, dc in INVENTORY}

    missing_inv = sorted(u for u in used if u not in inv)
    missing_disk = sorted(u for u in used if not u.startswith("nse.") and not (ROOT / u).exists())
    res.add("INV-01", "Every input read by the backtest is in the inventory", True, missing_inv, len(used),
            f"{len(used)} inputs referenced by the backtest code")
    res.add("INV-02", "Every referenced input exists", True, missing_disk, len(used))

    rows, bad = [], []
    for name, meta in inv.items():
        try:
            a, b, n, s = date_range(name, meta["date_col"])
        except Exception as e:                                   # noqa: BLE001
            a = b = n = s = None
            bad.append({"input": name, "error": str(e)})
        if not meta["source"] or not meta["klass"] or (a is None and name not in ("params.yaml", "data/backtest/cache/market.pkl",
                                                                                    "data/universe/sector_map.csv",
                                                                                    "data/costs/slippage_schedule.csv")):
            bad.append({"input": name, "missing": "source/class/date range"})
        rows.append(dict(input=name, **{k: meta[k] for k in ("categories", "klass", "source", "used_by")},
                         first=a, last=b, rows=n, symbols=s))
    res.add("INV-03", "Every input has a source, provenance class and date range", True, bad, len(inv))

    gaps = [cat for cat, names in REQUESTED.items() if not all(x in inv for x in names)]
    res.add("INV-04", "Every requested data category is mapped to an input", True, gaps, len(REQUESTED))

    # cache is a faithful extract of the warehouse (one liquid symbol, full window)
    db = query("select count(*) n from nse.cash_eod where symbol='RELIANCE' and trade_date between '2019-10-01' and '2026-07-31'")
    cache = pd.read_parquet(ROOT / "data/backtest/cache/cash.parquet")
    n_cache = int((cache.symbol == "RELIANCE").sum())
    res.add("INV-05", "Cache cash rows equal warehouse rows (RELIANCE)", False,
            [] if n_cache == int(db.n[0]) else [{"cache": n_cache, "db": int(db.n[0])}], 1)

    log = query("select segment, count(*) n from nse.ingest_log where status='ok' group by segment")
    ok = dict(zip(log.segment, log.n))
    zips = {seg: len([x for x in (ROOT / f"data/raw/{seg}").iterdir() if x.name.endswith(".zip")]) for seg in ("cm", "fo")}
    off = [{"segment": s, "zips": zips[s], "ingest_ok_days_to_2026-07-31": ok.get(s)} for s in zips if zips[s] < ok.get(s, 0)]
    res.add("INV-06", "Raw NSE zips cover every successfully ingested day", False, off, 2,
            f"zips cm={zips['cm']}, fo={zips['fo']} (to 2026-09-16); ingest_log ok cm={ok.get('cm')}, fo={ok.get('fo')} (to 2026-07-31)")
    res.add("INV-07", "params *_file keys not read by the backtest (informational)", False,
            [{"key": k, "path": v} for k, v in unused_params.items()], len(unused_params),
            "listed so a reader does not assume they affect results", passed=True)

    df = pd.DataFrame(rows)
    order = {"NSE-OFFICIAL": 0, "NSE-OFFICIAL + NSE-DERIVED": 1, "NSE-DERIVED": 2, "NSE-MANUAL": 3,
             "NSE-MANUAL + ASSUMPTION": 4, "VENDOR-DERIVED": 5, "VENDOR": 6, "ASSUMPTION": 7}
    df = df.sort_values("klass", key=lambda s: s.map(order))
    md = ["# Data Inventory — NSE Stock Options Wheel Backtest", "",
          "Generated by `python -m data_validation.run --phase 0`. Every input the backtest reads, where it comes from, "
          "and the period it covers.", "",
          "## Provenance classes", "",
          "| Class | Meaning |", "|---|---|",
          "| NSE-OFFICIAL | Exchange file as published (bhavcopy), or a straight extract of it |",
          "| NSE-DERIVED | Computed only from NSE official files (e.g. expiry calendar, detected contract adjustments) |",
          "| NSE-MANUAL | Hand-transcribed from NSE / SEBI documents (circulars, index announcements) |",
          "| VENDOR / VENDOR-DERIVED | Third-party data (Zerodha Kite, Yahoo Finance, OECD) or computed from it |",
          "| ASSUMPTION | Modelling choice with no external data |", "",
          "## Inputs", "",
          "| Input | Holds | Class | Source | Used by | First | Last | Rows | Symbols |",
          "|---|---|---|---|---|---|---|---:|---:|"]
    for r in df.itertuples():
        md.append(f"| `{r.input}` | {r.categories} | {r.klass} | {r.source} | {r.used_by} | {r.first or ''} | {r.last or ''} | "
                  f"{'' if pd.isna(r.rows) else f'{int(r.rows):,}'} | {'' if pd.isna(r.symbols) else int(r.symbols)} |")
    md += ["", "## Requested categories", "", "| Category | Input(s) | Class |", "|---|---|---|"]
    for cat, names in REQUESTED.items():
        md.append(f"| {cat} | " + ", ".join(f"`{n}`" for n in names) + " | " + ", ".join(sorted({inv[n]['klass'] for n in names})) + " |")
    md += ["", "## Gaps and caveats found during inventory", "",
           "- **No NSE contract master file** (with lot size, freeze quantity, tick size) exists in the repository for any date. "
           "Lot size is exchange-published only from 2024-07-08 (UDiFF `NewBrdLotQty`); earlier lots are derived. "
           "Freeze quantity and tick size are not stored anywhere. Only today's `fo_mktlots.csv` (2026-09-17) is on disk.",
           "- **No NSE F&O eligibility list.** Eligibility is inferred from the presence of stock futures in that day's bhavcopy.",
           "- **No NSE strike-scheme table.** Strikes are the ones listed in the bhavcopy; there is no independent strike-interval source.",
           "- **Final settlement price is not stored as such.** The engine uses the expiring future's `settle` on expiry day; "
           "see FSP_CHECK.md; CM close is primary, future settle is the fallback (`fsp_fallback_to_future_settle`).",
           "- **Option OHLC is in the warehouse only.** The backtest cache keeps close, settle, contracts and OI, not open/high/low.",
           "- **NIFTY 50 history is hand-transcribed**, rolled back from today's live list; no point-in-time NSE file exists.",
           "- **Ranking depends on vendor data** (Kite 15m bars, split/bonus-adjusted) that cannot be rebuilt without an account.",
           "- **Raw zips run to 2026-09-16; the warehouse and cache stop at 2026-07-31; the backtest ends 2026-06-30.**",
           "- **params keys not read by the backtest:** " + (", ".join(f"`{k}` ({v})" for k, v in unused_params.items()) or "none"),
           ""]
    (REPORTS / "DATA_INVENTORY.md").write_text("\n".join(md))
    res.notes.append("Inventory written to data_validation/reports/DATA_INVENTORY.md")
    res.tests_run = "INV-01..07 in this script (static scan + data queries)."
    return res
