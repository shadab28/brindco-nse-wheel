#!/usr/bin/env python3
"""Final settlement price check: CM close (used) vs expiring-future settle (cross-check), on every expiry day.

    python scripts/wheel/fsp_check.py

NSE settles stock options on the underlying's CM closing price, so the engine uses that. This script
reports how often the expiring future's settle differs, and whether the difference changes the ITM/OTM
status -- i.e. whether shares are allotted (put assignment) or called away -- for
  (a) every expiring strike that carried open interest (market-wide), and
  (b) every expired option in the saved backtest runs.

Outputs in data/backtest/report/:
    fsp_check_by_expiry.csv    one row per (expiry, symbol)
    fsp_check_trade_flips.csv  backtest trades whose assignment/call-away would differ
    FSP_CHECK.md               summary
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nse.wheel.data import load_market  # noqa: E402
from nse.wheel.runner import load_params  # noqa: E402


def main():
    p = load_params()
    b = p
    base = ROOT / b["base_dir"]
    md = load_market(base / "cache", ROOT / b["expiry_file"], ROOT / b["terminations_file"])
    out = base / "report"
    out.mkdir(parents=True, exist_ok=True)

    start, end = pd.Timestamp(p["start_date"]), pd.Timestamp(p["end_date"])
    chk = md.fsp_check()
    chk = chk[(chk.expiry >= start) & (chk.expiry <= end)]
    chk.to_csv(out / "fsp_check_by_expiry.csv", index=False)

    both = chk.dropna(subset=["cm_close", "future_settle"])
    differ = both[both["diff"].abs() > 1e-9]
    abs_bps = differ.diff_bps.abs()

    # (b) backtest trades settled at expiry
    flips, n_trades = [], 0
    for tf in sorted((base / "runs").glob("*/trades.csv")):
        t = pd.read_csv(tf, parse_dates=["exit_date", "expiry"])
        t = t[t.status.isin(["expired_otm", "assigned", "exercised"])]
        t = t[(t.exit_date >= start) & (t.exit_date <= end)]
        n_trades += len(t)
        for x in t.itertuples():
            cm, fut = md.fsp.get((x.exit_date, x.symbol)), md.fut_fsp.get((x.exit_date, x.symbol))
            if cm is None or fut is None:
                continue
            itm = (lambda f: f < x.strike) if x.instrument == "PE" else (lambda f: f > x.strike)
            if itm(cm) != itm(fut):
                flips.append({"run": tf.parent.name, "symbol": x.symbol, "expiry": x.exit_date.date(),
                              "instrument": x.instrument, "strike": x.strike, "quantity": x.quantity,
                              "cm_close": cm, "future_settle": fut,
                              "allotted_with_cm_close": itm(cm), "allotted_with_future_settle": itm(fut)})
    flips = pd.DataFrame(flips)
    flips.to_csv(out / "fsp_check_trade_flips.csv", index=False)

    lines = [
        "# Final settlement price check",
        "",
        f"Window {start.date()} → {end.date()}. FSP used = CM close on expiry day (NSE rule). "
        "Cross-check = expiring stock future's settle.",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| (expiry, symbol) pairs | {len(chk):,} |",
        f"| CM close missing (engine falls back to future settle) | {chk.cm_close.isna().sum():,} |",
        f"| Future settle missing | {chk.future_settle.isna().sum():,} |",
        f"| Both present | {len(both):,} |",
        f"| Prices differ | {len(differ):,} ({len(differ) / max(len(both), 1):.1%}) |",
        f"| Median / max abs difference where they differ | "
        f"{abs_bps.median() if len(differ) else 0:.2f} / {abs_bps.max() if len(differ) else 0:.2f} bps |",
        f"| Pairs with an OI-carrying strike between the two prices | "
        f"{(both.oi_strikes_between > 0).sum():,} ({both.oi_strikes_between.sum():,} strikes) |",
        f"| Backtest expiries checked (all runs) | {n_trades:,} |",
        f"| Backtest assignments / call-aways that flip | {len(flips):,} |",
        "",
    ]
    if len(differ):
        lines += ["Largest differences:", "", "| Expiry | Symbol | CM close | Future settle | bps | OI strikes between |",
                  "|---|---|---|---|---|---|"]
        top = differ.reindex(abs_bps.sort_values(ascending=False).index).head(15)
        lines += [f"| {r.expiry:%Y-%m-%d} | {r.symbol} | {r.cm_close:.2f} | {r.future_settle:.2f} | "
                  f"{r.diff_bps:+.1f} | {r.oi_strikes_between} |" for r in top.itertuples()]
        lines.append("")
    if len(flips):
        lines += ["Backtest trades whose allotment depends on the price used:", "",
                  flips.to_markdown(index=False), ""]
    (out / "FSP_CHECK.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
