"""Phase 2 — historical F&O eligibility, from the raw NSE F&O bhavcopy files."""
from __future__ import annotations

import pandas as pd

from data_validation import raw_fo
from data_validation.backtest_snapshot import OUT as BT
from data_validation.common import REPAIR_LOG, ROOT, PhaseResult, log_repair, query

START, END = pd.Timestamp("2019-10-01"), pd.Timestamp("2026-07-31")      # warehouse / cache window
RAW_TO_DB = {"ZOMATO": "ETERNAL", "TATAGLOBAL": "TATACONSUM"}              # nse/universe.SYMBOL_ALIASES
RECON = ROOT / "data/raw/fo_mkt/2021-03-30_reconstructed_bhav.csv"
LEVERAGES = ["L1", "L2", "L3", "L5"]


def reconstructed_day() -> pd.DataFrame:
    d = pd.read_csv(RECON)
    d = d[d.INSTRUMENT.isin(["FUTSTK", "OPTSTK"])].copy()
    d["symbol"] = d.SYMBOL.str.strip().replace(RAW_TO_DB)
    d["kind"] = d.INSTRUMENT.map({"FUTSTK": "FUT", "OPTSTK": "OPT"})
    return d


def eligibility() -> pd.DataFrame:
    """(trade_date, symbol) with stock futures and options listed in that day's NSE file."""
    sd = pd.read_parquet(raw_fo.OUT / "symbol_day.parquet")
    sd["symbol"] = sd.symbol.replace(RAW_TO_DB)
    sd = sd.groupby(["trade_date", "symbol"], as_index=False)[["n_fut", "n_opt"]].sum(min_count=1)
    rec = reconstructed_day().groupby(["symbol", "kind"]).size().unstack(fill_value=0).reset_index()
    rec = rec.rename(columns={"FUT": "n_fut", "OPT": "n_opt"}).assign(trade_date=pd.Timestamp("2021-03-30"), reconstructed=True)
    sd["reconstructed"] = False
    el = pd.concat([sd, rec[["trade_date", "symbol", "n_fut", "n_opt", "reconstructed"]]], ignore_index=True)
    el["n_fut"] = el.n_fut.fillna(0).astype(int)
    el["n_opt"] = el.n_opt.fillna(0).astype(int)
    el["eligible"] = (el.n_fut > 0) & (el.n_opt > 0)
    return el


def windows(el: pd.DataFrame, days: list) -> pd.DataFrame:
    """F&O start/end per symbol: maximal runs of consecutive exchange trading days with futures and options."""
    idx = {d: i for i, d in enumerate(days)}
    rows = []
    for s, g in el[el.eligible].groupby("symbol"):
        pos = sorted(idx[d] for d in g.trade_date if d in idx)
        start = prev = pos[0]
        for p in pos[1:] + [None]:
            if p is None or p != prev + 1:
                rows.append({"symbol": s, "fo_start": days[start], "fo_end": days[prev], "trading_days": prev - start + 1})
                start = p
            prev = p if p is not None else prev
    return pd.DataFrame(rows)


def run() -> PhaseResult:
    res = PhaseResult(
        2, "F&O Eligibility", "F&O eligibility",
        sources="Raw NSE F&O bhavcopy zips `data/raw/fo/*.csv.zip` (1,718 files: legacy fo*bhav.csv to 2024-07-05, UDiFF "
                "BhavCopy_NSE_FO from 2024-07-08), read directly by `data_validation/raw_fo.py` (independent of the warehouse "
                "and cache). 2021-03-30 has no bhavcopy on disk; `data/raw/fo_mkt/2021-03-30_reconstructed_bhav.csv` was "
                "rebuilt from the NSE F&O market-activity report `fo30032021.zip`. Checked: `data/backtest/cache/{futures,options}.parquet`, "
                "backtest trades at 1/2/3/5× (`data_validation/output/backtest/`).",
        methodology="A stock is F&O-eligible on a trading day when that day's NSE file lists at least one stock future "
                    "(FUTSTK/STF) and one stock option (OPTSTK/STO) on it. Eligibility windows are maximal runs of consecutive "
                    "exchange trading days (CM bhavcopy calendar). Every option the backtest traded is checked on its entry "
                    "day and exit day against the exact listed contract (symbol, expiry, strike, CE/PE). The cache is anti-joined "
                    "against the raw files row by row. Warehouse ticker renames (ZOMATO→ETERNAL, TATAGLOBAL→TATACONSUM) are applied "
                    "to raw tickers before comparing.")
    files = pd.read_csv(raw_fo.OUT / "files.csv", parse_dates=["file_date"])
    cm_days = sorted(pd.to_datetime(query("select distinct trade_date from nse.cash_eod where trade_date between %s and %s",
                                          (START.date(), END.date())).trade_date))
    fo_days = set(files.file_date)
    missing = [d for d in cm_days if d not in fo_days]
    no_file = [str(d.date()) for d in missing if d != pd.Timestamp("2021-03-30")]
    res.add("F-01", "Every CM trading day has an NSE F&O file (original or logged reconstruction)", True, no_file, len(cm_days),
            f"{len(fo_days)} original F&O files; CM days in window {len(cm_days)}; days without an original file: "
            + ", ".join(str(d.date()) for d in missing))
    extra = sorted(d for d in fo_days if START <= d <= END and d not in set(cm_days))
    res.add("F-01b", "No F&O file on a day without a CM session", True, [str(d.date()) for d in extra], len(fo_days))
    embedded = files[files.trade_dates_in_file != files.file_date.dt.strftime("%Y-%m-%d")]
    res.add("F-01c", "Each F&O file's embedded trade date equals its file date", True, embedded, len(files))

    rec = reconstructed_day()
    if not REPAIR_LOG.exists() or "2021-03-30_reconstructed_bhav" not in REPAIR_LOG.read_text():
        log_repair(2, "data/raw/fo (warehouse nse.options_eod / futures_eod)", "2021-03-30", "whole F&O bhavcopy",
                   "missing (no NSE bhavcopy on disk)", "data/raw/fo_mkt/2021-03-30_reconstructed_bhav.csv",
                   "pre-existing manual recovery discovered in validation; rows are NSE market-activity report op/fut files: "
                   "traded contracts only, SETTLE_PR empty", "NSE F&O market activity report fo30032021.zip")
    res.add("F-01d", "2021-03-30 reconstruction is complete (all listed contracts, settlement prices)", False,
            [{"stock_option_rows": int((rec.kind == "OPT").sum()), "stock_future_rows": int((rec.kind == "FUT").sum()),
              "settle_missing_rows": int(rec.SETTLE_PR.isna().sum()),
              "note": "market-activity report lists traded contracts only; untraded strikes and all settlement prices absent"}],
            1, "Logged in REPAIR_LOG.csv. Non-critical here; its effect on marks and trades is checked in F-04 and phase 6.")

    el = eligibility()
    el_w = el[(el.trade_date >= START) & (el.trade_date <= END)]
    all_days = sorted(set(cm_days))
    win = windows(el_w, all_days)
    win.to_csv(ROOT / "data_validation/output/fo_eligibility_windows.csv", index=False)
    el_w.to_parquet(ROOT / "data_validation/output/fo_eligibility_daily.parquet")
    one_sided = el_w[(el_w.n_fut > 0) != (el_w.n_opt > 0)]
    res.add("F-02", "Stock futures and options are listed together (no one-sided underlying-days)", False, one_sided,
            len(el_w), f"{win.symbol.nunique()} stocks eligible at some point; {len(win)} eligibility windows")

    cache_syms = set((ROOT / "data/backtest/cache/symbols.txt").read_text().split())
    elig_set = set(zip(el_w[el_w.eligible].trade_date, el_w[el_w.eligible].symbol))
    fut = pd.read_parquet(ROOT / "data/backtest/cache/futures.parquet", columns=["trade_date", "symbol"])
    fut["trade_date"] = pd.to_datetime(fut.trade_date)
    cache_fut_days = set(zip(fut.trade_date, fut.symbol))
    raw_fut = el_w[(el_w.n_fut > 0) & el_w.symbol.isin(cache_syms)]
    raw_fut_days = set(zip(raw_fut.trade_date, raw_fut.symbol))
    phantom = sorted(cache_fut_days - raw_fut_days)
    dropped = sorted(raw_fut_days - cache_fut_days)
    res.add("F-03", "Engine F&O-listed flag (cache futures) equals raw NSE eligibility for backtest symbols", True,
            [{"date": str(d.date()), "symbol": s, "cache": True, "raw": False} for d, s in phantom] +
            [{"date": str(d.date()), "symbol": s, "cache": False, "raw": True} for d, s in dropped],
            len(cache_fut_days | raw_fut_days))

    # every option the backtest traded: symbol eligible and exact contract listed on entry and exit day
    keys = ["trade_date", "symbol", "expiry", "strike", "option_type"]
    raw_contracts = []
    for y in range(2019, 2027):
        c = raw_fo.contracts([y], columns=["trade_date", "symbol", "expiry", "strike", "option_type", "kind", "contracts",
                                           "volume_raw", "close", "settle", "open_interest"])
        c = c[c.kind == "OPT"].drop(columns="kind")
        c["symbol"] = c.symbol.replace(RAW_TO_DB)
        raw_contracts.append(c)
    rc = pd.concat(raw_contracts, ignore_index=True)
    ro = rec[rec.kind == "OPT"].assign(trade_date=pd.Timestamp("2021-03-30"), expiry=pd.to_datetime(rec.EXPIRY_DT),
                                       strike=rec.STRIKE_PR.astype(float), option_type=rec.OPTION_TYP.str.strip())
    rc = pd.concat([rc, ro[keys]], ignore_index=True)
    rc["cmonth"] = rc.expiry.dt.strftime("%Y-%m")
    listed = set(zip(rc.trade_date, rc.symbol, rc.cmonth, rc.strike.round(4), rc.option_type))

    bad_entry, bad_exit, not_elig, held_missing, n_opt = [], [], [], [], 0
    for tag in LEVERAGES:
        t = pd.read_parquet(BT / f"trades_{tag}.parquet")
        o = t[t.instrument.isin(["PE", "CE"])].copy()
        n_opt += len(o)
        for x in o.itertuples():
            cm = pd.Timestamp(x.expiry).strftime("%Y-%m")
            entry = pd.Timestamp(x.date)
            k = (entry, x.symbol, cm, round(float(x.strike), 4), x.instrument)
            if (entry, x.symbol) not in elig_set:
                not_elig.append({"lev": tag, "symbol": x.symbol, "date": str(entry.date()), "contract": f"{cm} {x.strike} {x.instrument}"})
            if k not in listed:
                bad_entry.append({"lev": tag, "symbol": x.symbol, "entry": str(entry.date()), "contract": f"{cm} {x.strike} {x.instrument}"})
            if not x.open and pd.notna(x.exit_date):
                ex = pd.Timestamp(x.exit_date)
                if str(x.status) in ("universe_removal_buyback", "margin_call_buyback") and \
                        (ex, x.symbol, cm, round(float(x.strike), 4), x.instrument) not in listed:
                    bad_exit.append({"lev": tag, "symbol": x.symbol, "exit": str(ex.date()), "status": x.status,
                                     "contract": f"{cm} {x.strike} {x.instrument}"})
            end = pd.Timestamp(x.exit_date) if pd.notna(x.exit_date) else END
            for d in [d for d in all_days if entry <= d <= end]:
                if (d, x.symbol, cm, round(float(x.strike), 4), x.instrument) not in listed:
                    held_missing.append({"lev": tag, "symbol": x.symbol, "date": str(d.date()), "contract": f"{cm} {x.strike} {x.instrument}"})
    res.add("F-04", "Underlying was F&O-eligible on every backtest option entry day", True, not_elig, n_opt)
    res.add("F-05", "Exact option contract was listed on every backtest entry day", True, bad_entry, n_opt)
    res.add("F-06", "Exact contract was listed on every bought-back exit day (margin / universe exits)", True, bad_exit, n_opt)
    hm = pd.DataFrame(held_missing)
    res.add("F-07", "Contract listed on every day a position was held (marks)", False, hm, None,
            "" if hm.empty else "days: " + ", ".join(sorted(hm.date.unique())[:20]) +
            ". A missing day carries the last mark (engine stale-mark rule).")

    # cache option rows vs raw, row by row
    cache = pd.read_parquet(ROOT / "data/backtest/cache/options.parquet", columns=keys)
    cache["trade_date"] = pd.to_datetime(cache.trade_date)
    cache["expiry"] = pd.to_datetime(cache.expiry)
    cache["strike"] = cache.strike.astype(float).round(4)
    rc["strike"] = rc.strike.astype(float).round(4)
    m = cache.merge(rc[keys].drop_duplicates(), on=keys, how="left", indicator=True)
    phantom_rows = m[m._merge == "left_only"].drop(columns="_merge")
    res.add("F-08", "Every cached option row exists in the raw NSE file for that day (no options outside eligibility)", True,
            phantom_rows, len(cache), "" if phantom_rows.empty else phantom_rows.groupby("trade_date").size().head(20).to_string())
    outside = cache.merge(el_w[["trade_date", "symbol", "eligible"]], on=["trade_date", "symbol"], how="left")
    outside = outside[outside.eligible != True]                                     # noqa: E712
    res.add("F-09", "No cached option row on a day its underlying was not F&O-eligible", True,
            outside.drop_duplicates(["trade_date", "symbol"]), len(cache))

    bt_syms = set()
    for tag in LEVERAGES:
        bt_syms |= set(pd.read_parquet(BT / f"trades_{tag}.parquet").symbol)
    wb = win[win.symbol.isin(bt_syms)]
    starts = wb[wb.fo_start > START]
    ends = wb[wb.fo_end < END]
    res.add("F-10", "F&O start/end events for traded symbols inside the window (informational)", False,
            pd.concat([starts.assign(event="start"), ends.assign(event="end")]), len(bt_syms), passed=True)
    res.tests_run = "F-01..F-10 in data_validation/phase02_fo_eligibility.py; tests/test_dv_fo_eligibility.py (pytest)."
    res.notes += ["Eligibility table: data_validation/output/fo_eligibility_daily.parquet; windows: fo_eligibility_windows.csv.",
                  "Repair logged: 2021-03-30 F&O reconstruction (pre-existing, previously unlogged)."]
    return res
