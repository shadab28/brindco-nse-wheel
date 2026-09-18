#!/usr/bin/env python3
"""U-11 sensitivity: how much does the missing 15-minute data for HDFC, INFRATEL, LTIM and ZEEL move the results?

The main results are NOT changed. The ranking method stays as accepted (U-11); this script only measures the
bias. Everything it writes goes to data/backtest/sensitivity_u11/.

Why a proxy is needed: the entry score is rank_final = geometric mean of the close's gap to a 15m EMA50 and to a
daily EMA20. NSE publishes no free historical intraday bars, and the vendor 15m file has no bars for these four
names on 84 member-expiries. So for those member-expiries only:

    daily_gap  = (close / EMA20 of daily closes − 1) × 100     from NSE CM bhavcopy, split/bonus adjusted
    rank_proxy = β × daily_gap

β is fitted by least squares through the origin on every (symbol, expiry) that has both a real rank_final and an
NSE daily gap. The fit uses no returns, so it has no look-ahead into performance. It does use the whole window's
cross-section to set one scale factor; the per-year β range is printed so that choice can be judged.

Steps:
    1. cache   main cache + INFRATEL and LTIM option chains (HDFC and ZEEL are already in it)
    2. ranks   data/backtest/sensitivity_u11/expiry_rankings_u11.csv: real ranks + proxy rows, re-ranked per expiry
    3. divs    dividends for the extended cache (LTM -> LTIM, INDUSTOWER -> INFRATEL via ticker_aliases.csv)
    4. run     ranked runs at every leverage in leverage_grid: main rankings vs rankings with the proxy rows

    python scripts/wheel/u11_sensitivity.py            # all steps
"""
from __future__ import annotations

import json
import shutil
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
warnings.simplefilter("ignore", FutureWarning)

from nse.wheel import dividends as DIV  # noqa: E402
from nse.wheel.data import _query, load_market  # noqa: E402
from nse.wheel.runner import Context, load_params, run  # noqa: E402

OUT = ROOT / "data/backtest/sensitivity_u11"
MISSING = ["HDFC", "INFRATEL", "LTIM", "ZEEL"]
EXTRA_CHAINS = ["INFRATEL", "LTIM"]            # not in the main cache


def build_cache(p) -> Path:
    src, dst = ROOT / p["base_dir"] / "cache", OUT / "cache"
    dst.mkdir(parents=True, exist_ok=True)
    syms = sorted(set((src / "symbols.txt").read_text().split()) | set(EXTRA_CHAINS))
    start, end = "2019-10-01", "2026-07-31"
    cash = _query("select trade_date, symbol, close from nse.cash_eod where symbol = any(%s) and trade_date between %s and %s",
                  (EXTRA_CHAINS, start, end))
    fut = _query("select trade_date, symbol, expiry, close, settle, contracts from nse.futures_eod "
                 "where symbol = any(%s) and trade_date between %s and %s", (EXTRA_CHAINS, start, end))
    opt = pd.concat([_query("""
        select o.trade_date, o.symbol, o.expiry, o.strike, o.option_type, o.close, o.settle, o.contracts, o.open_interest
        from nse.options_eod o join nse.cash_eod c on c.trade_date = o.trade_date and c.symbol = o.symbol
        where o.symbol = %s and o.trade_date between %s and %s
          and o.expiry <= o.trade_date + 75 and o.strike between 0.3 * c.close and 3.0 * c.close""", (s, start, end))
        for s in EXTRA_CHAINS], ignore_index=True)
    lots = pd.read_csv(ROOT / p["lot_daily_file"], usecols=["trade_date", "symbol", "expiry", "lot"])
    lots = lots[lots.symbol.isin(EXTRA_CHAINS) & (lots.trade_date >= start) & (lots.trade_date <= end)]
    for name, extra in (("cash", cash), ("futures", fut), ("options", opt), ("lots", lots)):
        base = pd.read_parquet(src / f"{name}.parquet")
        base = base[~base.symbol.isin(EXTRA_CHAINS)]
        extra = extra.astype({c: base[c].dtype for c in base.columns if c in extra and c != "trade_date" and c != "expiry"},
                             errors="ignore")
        pd.concat([base, extra], ignore_index=True).to_parquet(dst / f"{name}.parquet")
        print(f"  {name}: +{len(extra):,} rows for {EXTRA_CHAINS}")
    (dst / "symbols.txt").write_text("\n".join(syms))
    (dst / "market.pkl").unlink(missing_ok=True)
    return dst


def daily_gap(md, ca) -> pd.DataFrame:
    """% gap of the close to its EMA20, on closes adjusted backwards for splits/bonuses."""
    px = md.close.copy()
    for r in ca[ca.share_multiplier != 1].itertuples():
        if r.symbol in px:
            px.loc[px.index < r.ex_date, r.symbol] /= r.share_multiplier
    return (px / px.ewm(span=20, adjust=False).mean() - 1) * 100


def build_ranks(p, md, ca) -> tuple[pd.DataFrame, dict]:
    rk = pd.read_csv(ROOT / p["rankings_file"], parse_dates=["expiry_date"])
    gap = daily_gap(md, ca)
    look = lambda e, s: gap.at[e, s] if (e in gap.index and s in gap.columns) else np.nan
    rk["daily_gap_nse"] = [look(e, s) for e, s in zip(rk.expiry_date, rk.symbol)]
    fit = rk.dropna(subset=["daily_gap_nse"])
    fit = fit[fit.expiry_date >= "2019-12-01"]          # EMA20 needs ~2 months of NSE closes (data starts 2019-10-01)
    beta = float((fit.daily_gap_nse * fit.rank_final).sum() / (fit.daily_gap_nse ** 2).sum())
    resid = fit.rank_final - beta * fit.daily_gap_nse
    by_year = {int(y): round(float((g.daily_gap_nse * g.rank_final).sum() / (g.daily_gap_nse ** 2).sum()), 3)
               for y, g in fit.groupby(fit.expiry_date.dt.year)}
    thr = p["rank_threshold"]
    stats = {"beta": round(beta, 4), "r2": round(float(1 - resid.var() / fit.rank_final.var()), 4),
             "resid_sd": round(float(resid.std()), 3), "beta_by_year": by_year, "fit_rows": int(len(fit)),
             "threshold_agreement": round(float(((fit.rank_final > thr) == (beta * fit.daily_gap_nse > thr)).mean()), 4)}

    mem = pd.read_csv(ROOT / p["universe_membership_file"], parse_dates=["effective_from", "effective_to"])
    exp = sorted(rk.expiry_date.unique())
    have = set(zip(rk.symbol, rk.expiry_date))
    add = []
    for r in mem[mem.symbol.isin(MISSING)].itertuples():
        lo = r.effective_from if pd.notna(r.effective_from) else pd.Timestamp.min
        hi = r.effective_to if pd.notna(r.effective_to) else pd.Timestamp.max
        for e in exp:
            if lo <= e <= hi and (r.symbol, e) not in have:
                g = look(e, r.symbol)
                add.append({"expiry_date": e, "symbol": r.symbol, "daily_gap_nse": g,
                            "rank_final": round(beta * g, 2) if pd.notna(g) else np.nan,
                            "ltp": md.close.at[e, r.symbol] if e in md.close.index and r.symbol in md.close else np.nan,
                            "score_source": "daily_proxy"})
    add = pd.DataFrame(add)
    stats["proxy_rows"] = int(len(add))
    stats["proxy_rows_unscored"] = int(add.rank_final.isna().sum()) if len(add) else 0
    stats["proxy_rows_by_symbol"] = add.groupby("symbol").size().to_dict() if len(add) else {}
    add = add.dropna(subset=["rank_final"])
    rk["score_source"] = "vendor_15m"
    cm = dict(zip(rk.expiry_date, rk.contract_month))
    add["contract_month"] = add.expiry_date.map(cm)
    out = pd.concat([rk, add], ignore_index=True).sort_values(["expiry_date", "rank_final"], ascending=[True, False])
    out["rank"] = out.groupby("expiry_date").cumcount() + 1
    stats["proxy_rows_above_threshold"] = int((add.rank_final > thr).sum())
    stats["proxy_rows_in_top_n"] = int(((out.score_source == "daily_proxy") & (out["rank"] <= p["n_positions"])
                                        & (out.rank_final > thr)).sum())
    return out, stats


def main():
    p = load_params()
    OUT.mkdir(parents=True, exist_ok=True)
    print("1. cache"); cache = build_cache(p)
    md = load_market(cache, ROOT / p["expiry_file"], ROOT / p["terminations_file"])
    det = pd.read_csv(ROOT / p["base_dir"] / "corporate_actions_detected.csv", parse_dates=["ex_date"])
    det = det[det.accepted]

    print("2. ranks"); ranks, stats = build_ranks(p, md, det)
    ranks.to_csv(OUT / "expiry_rankings_u11.csv", index=False)
    print(json.dumps(stats, indent=1))

    print("3. dividends")
    div = DIV.build_dividends(DIV.parse_raw(), md.close)
    div.to_csv(OUT / "dividends_u11.csv", index=False)
    for s in EXTRA_CHAINS:
        print(f"  {s}: {int((div.symbol.eq(s) & div.traded_on_ex_date).sum())} payable dividends")

    print("4. runs")
    shutil.copy(ROOT / p["base_dir"] / "corporate_actions_detected.csv", OUT / "corporate_actions_detected.csv")
    rows = []
    for tag, rank_file, base_dir in (("main", p["rankings_file"], p["base_dir"]),
                                     ("u11_proxy", str((OUT / "expiry_rankings_u11.csv").relative_to(ROOT)),
                                      str(OUT.relative_to(ROOT)))):
        q = {**p, "rankings_file": rank_file, "base_dir": base_dir,
             "dividends_file": p["dividends_file"] if tag == "main" else str((OUT / "dividends_u11.csv").relative_to(ROOT))}
        ctx = Context(q)
        for L in p["leverage_grid"]:
            r = run(ctx, run_id=None, write=False, leverage=L)
            m, tr = r["metrics"], r["trades"]
            picked = tr[tr.symbol.isin(MISSING)] if len(tr) else tr
            rows.append({"rankings": tag, "leverage": L, "final_nav": m["final_nav"], "cagr": m["cagr"],
                         "sharpe": m["sharpe"], "max_drawdown": m["max_drawdown"],
                         "trades_in_missing_names": int(len(picked)),
                         "pnl_in_missing_names": float(picked.total_pnl.sum()) if len(picked) else 0.0,
                         "recon": m["reconciliation_nav_vs_trades_inr"]})
            print(f"  {tag:10s} L{L:g}  CAGR {m['cagr']:.2%}  Sharpe {m['sharpe']:.2f}  MDD {m['max_drawdown']:.2%}  "
                  f"trades in the 4 names {rows[-1]['trades_in_missing_names']}", flush=True)
    res = pd.DataFrame(rows)
    res.to_csv(OUT / "u11_sensitivity_results.csv", index=False)
    (OUT / "u11_proxy_fit.json").write_text(json.dumps(stats, indent=1, default=str))


if __name__ == "__main__":
    main()
