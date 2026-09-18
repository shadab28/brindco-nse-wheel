#!/usr/bin/env python3
"""Scatter of rank_final (at expiry E) against the stock's return to the next monthly expiry, with an OLS fit.

Return = 15:15 bar close at the next expiry / ltp at E - 1, both from data/market/nifty50_15m.db (split/bonus
adjusted, the same source rank_final uses). Output: data/signals/analysis/rank_vs_next_expiry_return.{csv,png}

Second chart, ITM exercise rate vs rank: a put struck X% below ltp at E is taken as exercised when the next-expiry
return < −X% (15:15 close stands in for the FSP). Rate of the 'rank_final > cutoff' subset, one curve per X in ITM_THRESHOLDS.
Output: data/signals/analysis/itm_exercise_rate_vs_rank.{csv,png}
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data/signals/analysis"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
DOT, FIT = "#2a78d6", "#eb6834"
ITM_THRESHOLDS = [2, 3, 4, 5, 6, 7]
LINES = ["#9ec5f4", "#5b9ce6", "#2a78d6", "#1b5bb0", "#eb6834", "#b8431a"]
CUTOFFS = np.arange(-6, 10.01, 0.25)
MIN_N = 50
CURRENT_THRESHOLD = 1.5  # params.yaml rank_threshold
DIP_THRESHOLD = 1.5      # local trough shared by every curve; chosen as rank_threshold


def build() -> pd.DataFrame:
    rk = pd.read_csv(ROOT / "data/signals/expiry_rankings.csv", parse_dates=["expiry_date"])
    exps = sorted(rk.expiry_date.unique())
    rk["next_expiry"] = rk.expiry_date.map(dict(zip(exps[:-1], exps[1:])))
    with sqlite3.connect(ROOT / "data/market/nifty50_15m.db") as con:
        bars = pd.read_sql("select symbol, trade_date, close from ohlcv_15m where substr(ts, 12, 5) = '15:15'", con,
                           parse_dates=["trade_date"])
    px = bars.set_index(["symbol", "trade_date"]).close
    rk["ltp_next"] = [px.get((s, d), np.nan) if pd.notna(d) else np.nan for s, d in zip(rk.symbol, rk.next_expiry)]
    rk["next_expiry_return_pct"] = (rk.ltp_next / rk.ltp - 1) * 100
    return rk.dropna(subset=["next_expiry_return_pct"])


def fit(x, y):
    n = len(x)
    b, a = np.polyfit(x, y, 1)
    r = np.corrcoef(x, y)[0, 1]
    se = np.sqrt((1 - r * r) / (n - 2)) * y.std(ddof=1) / x.std(ddof=1)
    return b, a, r, se


def exercise_by_cutoff(d: pd.DataFrame) -> pd.DataFrame:
    """Exercise rate of the 'rank_final > cutoff' subset, per cutoff on a 0.25 grid and per ITM threshold."""
    rows = []
    for c in CUTOFFS:
        g = d.next_expiry_return_pct[d.rank_final > c]
        if len(g) < MIN_N:
            break
        rows.append({"rank_final_gt": c, "n": len(g), **{f"ex_{x}pct": (g < -x).mean() * 100 for x in ITM_THRESHOLDS}})
    return pd.DataFrame(rows)


def plot_exercise(d: pd.DataFrame):
    t = exercise_by_cutoff(d)
    t.to_csv(OUT / "itm_exercise_rate_vs_rank.csv", index=False)

    fig, (ax, axn) = plt.subplots(2, 1, figsize=(10, 7.4), dpi=160, facecolor=SURFACE, sharex=True,
                                  gridspec_kw={"height_ratios": [4, 1], "hspace": 0.08})
    for a in (ax, axn):
        a.set_facecolor(SURFACE); a.grid(True, color=GRID, linewidth=0.8); a.set_axisbelow(True)
        for sp in ("top", "right"):
            a.spines[sp].set_visible(False)
        a.axvline(CURRENT_THRESHOLD, color=INK2, linewidth=1, linestyle="--", zorder=1)
    ax.annotate(f"rank_threshold = {CURRENT_THRESHOLD:g} (local low)", xy=(CURRENT_THRESHOLD, 1), xycoords=("data", "axes fraction"),
                xytext=(4, -4), textcoords="offset points", va="top", fontsize=8.5, color=INK2)
    label_y = []  # end-label positions, pushed down so none sit closer than 1.3 pp
    for x in ITM_THRESHOLDS:
        v = t[f"ex_{x}pct"].iloc[-1]
        label_y.append(min(v, label_y[-1] - 1.3) if label_y else v)
    for x, c, ly in zip(ITM_THRESHOLDS, LINES, label_y):
        ax.plot(t.rank_final_gt, t[f"ex_{x}pct"], color=c, linewidth=2.2)
        ax.annotate(f"{x}% ITM", xy=(t.rank_final_gt.iloc[-1], ly), xytext=(6, 0), textcoords="offset points",
                    va="center", fontsize=8.5, color=c, fontweight="bold")
        dip = t.loc[t.rank_final_gt == DIP_THRESHOLD, f"ex_{x}pct"].iloc[0]
        ax.scatter([DIP_THRESHOLD], [dip], s=36, color=SURFACE, edgecolors=c, linewidths=2, zorder=4)
        ax.annotate(f"{dip:.1f}%", xy=(DIP_THRESHOLD, dip), xytext=(-6, -9), textcoords="offset points",
                    ha="right", fontsize=8, color=c, fontweight="bold")
    if DIP_THRESHOLD != CURRENT_THRESHOLD:
        ax.axvline(DIP_THRESHOLD, color=FIT, linewidth=1, linestyle=":", zorder=1)
        ax.annotate(f"local low at rank_final > {DIP_THRESHOLD:g}", xy=(DIP_THRESHOLD, 1),
                    xycoords=("data", "axes fraction"), xytext=(-4, -4), textcoords="offset points", ha="right",
                    va="top", fontsize=8.5, color=FIT)
    ax.set_ylabel("Puts exercised at next expiry (%)")
    axn.fill_between(t.rank_final_gt, t.n, color="#c8c7c2", step="post")
    axn.set_ylabel("n traded")
    axn.set_xlabel("Rank threshold: only stocks with rank_final > x")
    fig.suptitle("Put exercise rate by rank threshold — NIFTY 50 stocks, Oct 2019 – Jul 2026", x=0.07, ha="left",
                 fontsize=13, fontweight="bold", color=INK)
    ax.set_title("Each curve: a put struck X% below spot at expiry E, exercised when the stock closes below it at the next expiry",
                 loc="left", fontsize=9, color=INK2)
    fig.text(0.07, 0.01, f"15:15 bar close used in place of the final settlement price. Curves stop once fewer than "
             f"{MIN_N} stock-expiries remain.", fontsize=8, color=INK2)
    fig.subplots_adjust(left=0.1, right=0.9, top=0.9, bottom=0.12)
    fig.savefig(OUT / "itm_exercise_rate_vs_rank.png", facecolor=SURFACE)
    plt.close(fig)
    with pd.option_context("display.width", 200, "display.float_format", "{:.1f}".format):
        print(t[t.rank_final_gt % 1 == 0].to_string(index=False))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    d = build()
    d.to_csv(OUT / "rank_vs_next_expiry_return.csv", index=False)
    x, y = d.rank_final.to_numpy(), d.next_expiry_return_pct.to_numpy()
    b, a, r, se = fit(x, y)
    rho = pd.Series(x).rank().corr(pd.Series(y).rank())
    # average of per-expiry cross-sectional slopes (removes the common market move)
    per = d.groupby("expiry_date").apply(lambda g: np.polyfit(g.rank_final, g.next_expiry_return_pct, 1)[0]
                                         if len(g) > 5 else np.nan, include_groups=False).dropna()
    fm_t = per.mean() / (per.std(ddof=1) / np.sqrt(len(per)))

    plt.rcParams.update({"font.family": "sans-serif", "font.size": 10, "axes.edgecolor": GRID, "text.color": INK,
                         "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2})
    fig, ax = plt.subplots(figsize=(10, 6.2), dpi=160, facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.axhline(0, color="#c8c7c2", linewidth=1, zorder=1)
    ax.axvline(0, color="#c8c7c2", linewidth=1, zorder=1)
    ax.axvline(CURRENT_THRESHOLD, color=INK2, linewidth=1, linestyle="--", zorder=1)
    ax.annotate(f"rank_threshold = {CURRENT_THRESHOLD:g}", xy=(CURRENT_THRESHOLD, 1), xycoords=("data", "axes fraction"),
                xytext=(4, -4), textcoords="offset points", va="top", fontsize=8.5, color=INK2)
    ax.scatter(x, y, s=10, color=DOT, alpha=0.35, linewidths=0, zorder=2)
    xs = np.linspace(x.min(), x.max(), 100)
    ax.plot(xs, a + b * xs, color=FIT, linewidth=2.2, zorder=3)
    ax.annotate(f"Best fit (OLS): return = {a:.2f} {'−' if b < 0 else '+'} {abs(b):.2f} × rank_final",
                xy=(xs[-1], a + b * xs[-1]), xytext=(0, 60), textcoords="offset points", ha="right", color=INK,
                fontsize=10, fontweight="bold", bbox=dict(boxstyle="round,pad=0.35", fc=SURFACE, ec=GRID),
                arrowprops=dict(arrowstyle="-", color=INK2, linewidth=0.8))
    ax.set_xlabel("rank_final at expiry (%)")
    ax.set_ylabel("Return to next monthly expiry (%)")
    fig.suptitle("rank_final vs next-expiry return — NIFTY 50 stocks, Oct 2019 – Jul 2026", x=0.07, ha="left",
                 fontsize=13, fontweight="bold", color=INK)
    ax.set_title(f"{len(d):,} stock-expiries · slope {b:.3f} pp per rank point (SE {se:.3f}) · r = {r:.3f}, R² = {r*r:.3f}\n"
                 f"Spearman ρ = {rho:.3f} · mean per-expiry slope {per.mean():.3f}, t = {fm_t:.2f} ({len(per)} expiries)",
                 loc="left", fontsize=9, color=INK2)
    fig.text(0.07, 0.01, "Prices: Kite 15-minute bars (split/bonus adjusted), 15:15 bar close. Return excludes dividends.",
             fontsize=8, color=INK2)
    fig.tight_layout(rect=(0.02, 0.03, 1, 0.97))
    fig.savefig(OUT / "rank_vs_next_expiry_return.png", facecolor=SURFACE)
    plt.close(fig)
    print(f"n={len(d)} slope={b:.4f} se={se:.4f} intercept={a:.4f} r={r:.4f} r2={r*r:.4f} spearman={rho:.4f} "
          f"per_expiry_slope_mean={per.mean():.4f} t={fm_t:.2f} n_expiries={len(per)}")
    plot_exercise(d)


if __name__ == "__main__":
    main()
