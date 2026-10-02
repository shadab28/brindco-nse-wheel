#!/usr/bin/env python3
"""Monte Carlo of the chosen run (params.yaml) and of every variant seen before it was chosen.

    python scripts/wheel/strike_grid.py            # writes strike_grid_L3/navs.csv
    python scripts/wheel/stop_loss_probe.py rerun  # writes stop_loss/rerun_navs.csv
    python scripts/wheel/monte_carlo.py            # 10,000 paths, seed fixed
    python scripts/wheel/monte_carlo.py --paths 2000

Each part has a "match" check that must pass before its numbers are written:

1. Metric engine. Paths are scored with a vectorised copy of nse.wheel.metrics.nav_stats. On the actual chosen
   path it must reproduce metrics.json (CAGR, vol, Sharpe, max DD, Calmar) to 1e-9.
2. Return bootstrap of the chosen run. Stationary block bootstrap (Politis & Romano 1994, mean block 21 trading
   days) of its daily returns, each day drawn together with that day's risk-free rate. Blocks keep volatility
   clustering, which is what drives drawdowns; an iid bootstrap (block 1) runs alongside for comparison. Check:
   the median simulated CAGR, vol and Sharpe land within tolerance of the backtest.
3. Every variant. All runs seen before the headline was fixed (leverage L1–L5, the 36-cell strike grid, the
   stop-loss sweep, the L5 threshold runs), deduplicated, are pushed through the SAME resampled day sequences.
   Because the days are shared, the correlation between variants survives, and on each path one can ask which
   variant would have won. That gives
     - per-variant distributions (variants.csv),
     - a Reality Check (White 2000): each variant's excess return is recentred to zero, and the best null Sharpe
       across all variants on each path is the bar a no-skill search reaches. Its mean also gives the effective
       number of independent trials, which the Deflated Sharpe Ratio otherwise has to guess,
     - selection optimism: pick the winner on each path, then compare its in-sample value with its average
       over all paths. The mean gap is how much picking the best inflates the headline.
   Check: the closed-form expected-maximum formula agrees with a direct simulation of N null trials.
4. Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014) as a cross-check, with the skew and kurtosis of the
   chosen run's returns.

Writes data/backtest/report/monte_carlo/{summary.csv, variants.csv, selection.csv, deflated_sharpe.csv,
MONTE_CARLO.md, *.png}. scripts/final_results.py copies them into final_results/.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import NormalDist

import matplotlib
import numpy as np
import pandas as pd
import yaml

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from nse.wheel.metrics import ANN, daily_rf, nav_stats  # noqa: E402
from nse.wheel.runner import load_risk_free  # noqa: E402

REP = ROOT / "data/backtest/report"
OUT = REP / "monte_carlo"
SEED = 20260930
BLOCK = 21                  # mean block length, trading days (about one monthly expiry cycle)
EULER = 0.5772156649015329
N = NormalDist()
KEYS = ["cagr", "annualized_vol", "sharpe", "max_drawdown", "calmar"]


def path_stats(R: np.ndarray, RF: np.ndarray, years: float) -> dict[str, np.ndarray]:
    """nav_stats for many paths at once. R, RF: (paths, days) daily returns and daily risk-free."""
    growth = np.cumprod(1.0 + R, axis=1)
    nav = np.concatenate([np.ones((len(R), 1)), growth], axis=1)
    mdd = (nav / np.maximum.accumulate(nav, axis=1) - 1.0).min(axis=1)
    cagr = growth[:, -1] ** (1.0 / years) - 1.0
    ex = R - RF
    return {"cagr": cagr, "annualized_vol": R.std(axis=1, ddof=1) * np.sqrt(ANN),
            "sharpe": ex.mean(axis=1) * ANN / (ex.std(axis=1, ddof=1) * np.sqrt(ANN)),
            "max_drawdown": mdd, "calmar": cagr / np.abs(mdd)}


def stationary_bootstrap(n: int, paths: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """Index matrix (paths, n). Each step continues the current block, or with prob 1/block jumps to a random day."""
    if block == 1:
        return rng.integers(n, size=(paths, n))
    idx = np.empty((paths, n), dtype=np.int64)
    idx[:, 0] = rng.integers(n, size=paths)
    for t in range(1, n):
        jump = rng.random(paths) < 1.0 / block
        idx[:, t] = np.where(jump, rng.integers(n, size=paths), (idx[:, t - 1] + 1) % n)
    return idx


def expected_max_z(n: float) -> float:
    """Expected maximum of n iid standard normals (Bailey & Lopez de Prado approximation)."""
    if n <= 1:
        return 0.0
    return (1 - EULER) * N.inv_cdf(1 - 1 / n) + EULER * N.inv_cdf(1 - 1 / (n * np.e))


def effective_trials(z: float, n_max: int) -> float:
    """The n whose expected maximum equals z (bisection on a log scale), capped at n_max."""
    if z <= 0:
        return 1.0
    lo, hi = 1.0, float(n_max)
    if expected_max_z(hi) <= z:
        return hi
    for _ in range(60):
        mid = np.sqrt(lo * hi)
        lo, hi = (mid, hi) if expected_max_z(mid) < z else (lo, mid)
    return lo


def load_variants(chosen: str, index: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict[str, str]]:
    """Daily NAV of every run seen before the headline was fixed, one column each, exact duplicates dropped.
    Returns (navs, family of each column)."""
    runs = ROOT / "data/backtest/runs"
    cols, family = {}, {}

    def add(name, nav, fam):
        cols[name], family[name] = nav, fam

    for L in [1, 2, 3, 4, 5]:
        rid = f"ranked_L{L}"
        add(rid, pd.read_csv(runs / rid / "daily.csv", parse_dates=["date"]).set_index("date").nav, "leverage")
    for name, s in pd.read_csv(REP / "strike_grid_L3/navs.csv", parse_dates=["date"]).set_index("date").items():
        add(f"L3 {name}", s, "strike grid (L3)")
    for name, s in pd.read_csv(REP / "stop_loss/rerun_navs.csv", parse_dates=["date"]).set_index("date").items():
        add(f"{name} (put5_call5)", s, "stop sweep")
    for name, s in pd.read_csv(REP / "threshold_L5/nav_comparison.csv", parse_dates=["date"]).set_index("date").items():
        add(f"L5 {name}", s, "threshold (L5, older engine)")
    navs = pd.DataFrame(cols)
    assert navs.index.equals(index) and not navs.isna().any().any(), "variant NAVs must share the chosen run's dates"
    keep: list[str] = []
    for c in navs.columns:
        if not any(np.allclose(navs[c].to_numpy(), navs[k].to_numpy(), rtol=1e-12, atol=1e-6) for k in keep):
            keep.append(c)
    assert chosen in keep
    return navs[keep], {c: family[c] for c in keep}


def pct(x: float) -> str:
    return f"{x:.2%}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", type=int, default=10_000)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(SEED)

    params = yaml.safe_load((ROOT / "params.yaml").read_text())
    run_id = f"ranked_L{params['backtest']['leverage']:g}"
    run = ROOT / "data/backtest/runs" / run_id
    rf = load_risk_free(ROOT / params["backtest"]["risk_free_file"])
    nav = pd.read_csv(run / "daily.csv", parse_dates=["date"]).set_index("date").nav
    reported = json.loads((run / "metrics.json").read_text())
    bench = pd.read_csv(REP / "analysis/portfolio_metrics.csv", index_col=0)
    nifty_cagr = bench.loc[bench.index.str.startswith("NIFTY"), "cagr"].iloc[0]
    nifty_sharpe = bench.loc[bench.index.str.startswith("NIFTY"), "sharpe"].iloc[0]

    r = nav.pct_change().dropna()
    rfd = daily_rf(r.index, rf)
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    R, RF = r.to_numpy(), rfd.to_numpy()
    T = len(R)

    # ---- 1. metric engine must reproduce the backtest's own numbers -------------------------------------
    mine = {k: float(v[0]) for k, v in path_stats(R[None, :], RF[None, :], years).items()}
    ref = nav_stats(nav, rf)
    for k in KEYS:
        assert abs(mine[k] - ref[k]) < 1e-9 and abs(mine[k] - reported[k]) < 1e-9, (k, mine[k], ref[k], reported[k])
    print(f"[1] metric engine matches metrics.json for {run_id} on {', '.join(KEYS)}")

    # ---- 2. bootstrap of the chosen run -----------------------------------------------------------------
    rows, sims = [], {}
    for label, block in [(f"block{BLOCK}", BLOCK), ("iid", 1)]:
        idx = stationary_bootstrap(T, args.paths, block, rng)
        st = path_stats(R[idx], RF[idx], years)
        sims[label] = (idx, st)
        for k in KEYS:
            v = st[k]
            rows.append({"bootstrap": label, "metric": k, "backtest": mine[k], "mc_median": np.median(v),
                         "mc_mean": v.mean(), "p05": np.quantile(v, .05), "p25": np.quantile(v, .25),
                         "p75": np.quantile(v, .75), "p95": np.quantile(v, .95),
                         "backtest_percentile": (v < mine[k]).mean()})
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT / "summary.csv", index=False)

    idx, main_st = sims[f"block{BLOCK}"]
    tol = {"cagr": 0.01, "annualized_vol": 0.01, "sharpe": 0.05}
    for k, t in tol.items():
        med = np.median(main_st[k])
        assert abs(med - mine[k]) < t, f"bootstrap median {k} {med:.4f} vs backtest {mine[k]:.4f} (tol {t})"
    print(f"[2] block bootstrap medians match the backtest within tolerance: "
          + ", ".join(f"{k} {np.median(main_st[k]):.4f} vs {mine[k]:.4f}" for k in tol))

    probs = {
        "P(CAGR < NIFTY 50 TRI CAGR)": (main_st["cagr"] < nifty_cagr).mean(),
        "P(CAGR < average T-bill)": (main_st["cagr"] < reported["avg_risk_free"]).mean(),
        "P(CAGR < 0)": (main_st["cagr"] < 0).mean(),
        "P(max DD worse than -30%)": (main_st["max_drawdown"] < -0.30).mean(),
        "P(max DD worse than -40%)": (main_st["max_drawdown"] < -0.40).mean(),
        "P(Sharpe < NIFTY 50 TRI Sharpe)": (main_st["sharpe"] < nifty_sharpe).mean(),
    }

    # ---- 3. every variant through the same resampled days -----------------------------------------------
    navs, family = load_variants(run_id, nav.index)
    names = list(navs.columns)
    V = len(names)
    sim = {k: np.empty((args.paths, V)) for k in ["cagr", "sharpe", "max_drawdown", "calmar"]}
    null_sharpe = np.empty((args.paths, V))
    var_rows = []
    for j, c in enumerate(names):
        Rv = navs[c].pct_change().dropna().to_numpy()
        actual = {k: float(v[0]) for k, v in path_stats(Rv[None, :], RF[None, :], years).items()}
        st = path_stats(Rv[idx], RF[idx], years)
        for k in sim:
            sim[k][:, j] = st[k]
        ex = Rv[idx] - RF[idx]
        mu = (Rv - RF).mean()                       # recentre: this variant's true excess return set to zero
        null_sharpe[:, j] = (ex.mean(axis=1) - mu) * ANN / (ex.std(axis=1, ddof=1) * np.sqrt(ANN))
        var_rows.append({"variant": c, "family": family[c], "chosen": c == run_id,
                         **{f"backtest_{k}": actual[k] for k in ["cagr", "sharpe", "max_drawdown", "calmar"]},
                         "mc_median_cagr": np.median(st["cagr"]), "mc_p05_cagr": np.quantile(st["cagr"], .05),
                         "mc_p95_cagr": np.quantile(st["cagr"], .95), "mc_median_sharpe": np.median(st["sharpe"]),
                         "mc_p05_sharpe": np.quantile(st["sharpe"], .05),
                         "mc_median_max_drawdown": np.median(st["max_drawdown"]),
                         "mc_p05_max_drawdown": np.quantile(st["max_drawdown"], .05),
                         "p_cagr_below_nifty": (st["cagr"] < nifty_cagr).mean(),
                         "p_sharpe_below_zero": (st["sharpe"] < 0).mean()})
    variants = pd.DataFrame(var_rows)
    variants.to_csv(OUT / "variants.csv", index=False)
    ci = names.index(run_id)
    assert np.array_equal(sim["cagr"][:, ci], main_st["cagr"])   # chosen column is exactly part 2's simulation

    # Reality Check: best recentred (no-skill) Sharpe across all variants, path by path
    null_max = null_sharpe.max(axis=1)
    obs_sharpe = variants.backtest_sharpe.to_numpy()
    se_annual = np.median(null_sharpe.std(axis=0, ddof=1))       # sampling s.e. of one variant's Sharpe
    n_eff = effective_trials(null_max.mean() / se_annual, V)
    # Selection optimism: in-sample winner vs that same variant's average over all paths. The headline pool is
    # the L3 strike grid picked by CAGR, which is how put 4% / call 3% was chosen; the all-variant pool mixes in
    # L5 runs whose CAGR is far noisier than L3's, so it overstates the CAGR haircut (shown for reference).
    rows_ = np.arange(args.paths)
    grid_cols = [j for j, c in enumerate(names) if family[c] == "strike grid (L3)" or c == run_id]
    pools = [("L3 strike grid (how 4%/3% was picked)", grid_cols, ["cagr", "sharpe"]),
             (f"all {V} variants", list(range(V)), ["cagr", "sharpe", "calmar"])]
    sel_rows = []
    for pool, cols, crits in pools:
        for crit in crits:
            win = np.array(cols)[sim[crit][:, cols].argmax(axis=1)]
            optimism = {k: float(np.mean(sim[k][rows_, win] - sim[k].mean(axis=0)[win]))
                        for k in ["cagr", "sharpe", "max_drawdown", "calmar"]}
            share = np.bincount(win, minlength=V) / args.paths
            sel_rows.append({"pool": pool, "picked_by": crit, **{f"optimism_{k}": v for k, v in optimism.items()},
                             "chosen_win_share": share[ci], "most_frequent_winner": names[int(share.argmax())],
                             "its_win_share": share.max(), "n_variants_ever_winning": int((share > 0).sum())})
    selection = pd.DataFrame(sel_rows)
    rc = {"n_variants": V, "observed_chosen_sharpe": mine["sharpe"], "observed_best_sharpe": obs_sharpe.max(),
          "observed_best_variant": names[int(obs_sharpe.argmax())],
          "null_best_sharpe_mean": null_max.mean(), "null_best_sharpe_p95": np.quantile(null_max, .95),
          "p_value_chosen": (null_max >= mine["sharpe"]).mean(), "p_value_best": (null_max >= obs_sharpe.max()).mean(),
          "sharpe_se_annual": se_annual, "effective_independent_trials": n_eff}
    pd.DataFrame([rc]).to_csv(OUT / "reality_check.csv", index=False)
    selection.to_csv(OUT / "selection.csv", index=False)

    formula = expected_max_z(V)
    direct = rng.standard_normal((20_000, V)).max(axis=1).mean()
    assert abs(direct - formula) / formula < 0.03, (direct, formula)
    print(f"[3] {V} variants (after dedup). E[max of {V} null z]: formula {formula:.3f} vs simulation {direct:.3f}. "
          f"Reality Check p = {rc['p_value_chosen']:.3f}, effective trials {n_eff:.1f}")

    # ---- 4. Deflated Sharpe Ratio (cross-check) ----------------------------------------------------------
    ex = R - RF
    sr = ex.mean() / ex.std(ddof=1)                      # per-day Sharpe
    z = (ex - ex.mean()) / ex.std(ddof=0)
    skew, kurt = (z ** 3).mean(), (z ** 4).mean()        # kurt is raw (normal = 3)
    sr_sd = variants.backtest_sharpe.std(ddof=1) / np.sqrt(ANN)
    se = np.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr ** 2) / np.sqrt(T - 1)   # s.e. of per-day Sharpe (Mertens)
    dsr_rows = []
    for n in sorted({1, 5, 10, 20, round(n_eff, 1), V}):
        sr0 = sr_sd * expected_max_z(n)                  # hurdle from the observed spread of the trials (BLdP)
        sr0_noise = se * expected_max_z(n)               # hurdle if every trial were pure noise
        dsr_rows.append({"n_trials": n, "sharpe_hurdle_annual": sr0 * np.sqrt(ANN),
                         "deflated_sharpe_prob": N.cdf((sr - sr0) / se),
                         "noise_hurdle_annual": sr0_noise * np.sqrt(ANN),
                         "noise_deflated_prob": N.cdf((sr - sr0_noise) / se)})
    dsr = pd.DataFrame(dsr_rows)
    dsr.to_csv(OUT / "deflated_sharpe.csv", index=False)

    # ---- final number ----------------------------------------------------------------------------------
    head = selection.iloc[0]                             # L3 strike grid, picked by CAGR
    final = {"backtest_cagr": mine["cagr"], "selection_optimism_cagr": head.optimism_cagr,
             "adjusted_cagr": mine["cagr"] - head.optimism_cagr,
             "mc_p05_cagr": np.quantile(main_st["cagr"], .05) - head.optimism_cagr,
             "mc_p95_cagr": np.quantile(main_st["cagr"], .95) - head.optimism_cagr,
             "backtest_sharpe": mine["sharpe"], "selection_optimism_sharpe": head.optimism_sharpe,
             "adjusted_sharpe": mine["sharpe"] - head.optimism_sharpe,
             "haircut_range_cagr_low": mine["cagr"] - selection.optimism_cagr.max(),
             "haircut_range_cagr_high": mine["cagr"] - selection.optimism_cagr.min(),
             "backtest_max_drawdown": mine["max_drawdown"], "mc_p05_max_drawdown": np.quantile(main_st["max_drawdown"], .05),
             "p_cagr_below_nifty": probs["P(CAGR < NIFTY 50 TRI CAGR)"], "reality_check_p": rc["p_value_chosen"]}
    pd.DataFrame([final]).to_csv(OUT / "final_number.csv", index=False)

    # ---- charts --------------------------------------------------------------------------------------
    paths = nav.iloc[0] * np.cumprod(1.0 + R[idx], axis=1)
    q = np.quantile(paths, [.05, .25, .5, .75, .95], axis=0)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.fill_between(r.index, q[0] / 1e7, q[4] / 1e7, alpha=.15, color="C0", label="5–95%")
    ax.fill_between(r.index, q[1] / 1e7, q[3] / 1e7, alpha=.3, color="C0", label="25–75%")
    ax.plot(r.index, q[2] / 1e7, color="C0", lw=1, label="MC median")
    ax.plot(nav.index, nav / 1e7, color="black", lw=1.2, label=f"backtest {run_id}")
    ax.set_ylabel("NAV (₹ crore)")
    ax.set_title(f"{run_id}: {args.paths:,} block-bootstrap paths (mean block {BLOCK} days)")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(OUT / "nav_fan.png", dpi=130)
    plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(14, 4))
    for ax, k, f in zip(axes, ["cagr", "max_drawdown", "sharpe"], [100, 100, 1]):
        ax.hist(main_st[k] * f, bins=60, color="C0", alpha=.7)
        ax.axvline(mine[k] * f, color="black", lw=1.5, label="backtest")
        ax.set_title(k + (" (%)" if f == 100 else ""))
    axes[0].axvline(nifty_cagr * 100, color="C3", ls="--", label="NIFTY 50 TRI")
    axes[0].legend()
    fig.tight_layout()
    fig.savefig(OUT / "distributions.png", dpi=130)
    plt.close(fig)

    vs = variants.sort_values("mc_median_cagr").reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(10, 0.18 * V + 1.5))
    colors = ["C3" if c else "C0" for c in vs.chosen]
    ax.hlines(range(V), vs.mc_p05_cagr * 100, vs.mc_p95_cagr * 100, color=colors, alpha=.5)
    ax.scatter(vs.mc_median_cagr * 100, range(V), color=colors, s=12, zorder=3)
    ax.axvline(nifty_cagr * 100, color="grey", ls="--", lw=1)
    ax.set_yticks(range(V), vs.variant, fontsize=6)
    ax.set_xlabel("CAGR (%) — dot: MC median, bar: 5–95%; red = chosen; dashed = NIFTY 50 TRI")
    fig.tight_layout()
    fig.savefig(OUT / "variants_cagr.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.hist(null_max, bins=60, color="C7", alpha=.8, label=f"best of {V} no-skill variants")
    ax.axvline(mine["sharpe"], color="C3", lw=1.5, label=f"chosen {mine['sharpe']:.2f}")
    ax.axvline(obs_sharpe.max(), color="black", ls="--", lw=1, label=f"best observed {obs_sharpe.max():.2f}")
    ax.set_xlabel("annualised Sharpe")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "reality_check.png", dpi=130)
    plt.close(fig)

    # ---- write-up ------------------------------------------------------------------------------------
    s = summary[summary.bootstrap == f"block{BLOCK}"].set_index("metric")
    iid = summary[summary.bootstrap == "iid"].set_index("metric")
    fmt = {"sharpe": "{:.2f}".format, "calmar": "{:.2f}".format}
    f = lambda k, v: fmt.get(k, pct)(v)  # noqa: E731
    fams = variants.groupby("family", sort=False).size()
    L = [f"# Monte Carlo: {run_id}", "",
         f"Generated by `scripts/wheel/monte_carlo.py`. Do not edit by hand. {args.paths:,} paths, seed {SEED}, "
         f"{T} trading days ({nav.index[0].date()} → {nav.index[-1].date()}).", "",
         "## The final number", "",
         "| | backtest | after selection haircut |", "|---|---|---|",
         f"| CAGR | {pct(final['backtest_cagr'])} | **{pct(final['adjusted_cagr'])}** "
         f"(90% range {pct(final['mc_p05_cagr'])} to {pct(final['mc_p95_cagr'])}) |",
         f"| Sharpe | {final['backtest_sharpe']:.2f} | **{final['adjusted_sharpe']:.2f}** |",
         f"| Max drawdown | {pct(final['backtest_max_drawdown'])} | 1-in-20 path: **{pct(final['mc_p05_max_drawdown'])}** |",
         f"| P(CAGR below NIFTY 50 TRI) | | {final['p_cagr_below_nifty']:.0%} |",
         f"| Reality Check p-value (search of {V} variants) | | {final['reality_check_p']:.3f} |", "",
         f"- The haircut is the selection optimism from part 3. Put 4% / call 3% was chosen as the best-CAGR cell "
         f"of the L3 strike grid. On each simulated history, the best-CAGR cell of that grid beats its own "
         f"average by {pct(final['selection_optimism_cagr'])} CAGR and {final['selection_optimism_sharpe']:.2f} "
         "Sharpe, so that gap is taken off.",
         f"- Other pools and criteria in part 3 put the adjusted CAGR between "
         f"{pct(final['haircut_range_cagr_low'])} and {pct(final['haircut_range_cagr_high'])}. The low end picks by "
         "CAGR across all variants, where the noisy L5 runs dominate the winners.",
         "- The 90% range is the bootstrap 5–95% of the chosen run, shifted by the haircut.", "",
         "## 1. Match checks", "",
         f"- **Metric engine:** the vectorised scorer reproduces `runs/{run_id}/metrics.json` to 1e-9 on the "
         "actual path (CAGR, vol, Sharpe, max DD, Calmar).",
         "- **Bootstrap centre:** the median simulated CAGR / vol / Sharpe are within 1 pp / 1 pp / 0.05 of the "
         "backtest (they must be: the simulation draws from the same daily returns).",
         f"- **Shared paths:** the chosen run's column in the variant simulation is identical to part 2.",
         f"- **Expected-maximum formula:** E[max of {V} null trials] = {formula:.3f}σ by formula, {direct:.3f}σ by "
         "direct simulation.",
         "- **Variant NAVs:** `strike_grid.py` and `stop_loss_probe.py rerun` reproduced their published "
         "tables exactly when they were rerun to save daily NAVs.", "",
         f"## 2. Chosen run: return bootstrap (stationary, mean block {BLOCK} days)", "",
         "| metric | backtest | MC median | 5% | 95% | backtest percentile | iid median | iid 5% |",
         "|---|---|---|---|---|---|---|---|"]
    for k in KEYS:
        L.append(f"| {k} | {f(k, s.backtest[k])} | {f(k, s.mc_median[k])} | {f(k, s.p05[k])} | "
                 f"{f(k, s.p95[k])} | {s.backtest_percentile[k]:.0%} | {f(k, iid.mc_median[k])} | {f(k, iid.p05[k])} |")
    L += ["", "| event (block bootstrap) | probability |", "|---|---|"]
    L += [f"| {k} | {v:.1%} |" for k, v in probs.items()]
    L += ["", "- *Backtest percentile* = share of simulated paths below the backtest value. For max drawdown, "
          "a low percentile means most reshuffled histories had a *worse* drawdown than the one realised.",
          "- iid resampling destroys volatility clustering and understates drawdowns; read the block columns.",
          "", "![fan](nav_fan.png)", "", "![distributions](distributions.png)", "",
          f"## 3. All {V} variants on the same resampled days", "",
          "Variants by family (exact duplicates removed): "
          + ", ".join(f"{fam} {n}" for fam, n in fams.items()) + ". The L5 threshold runs predate the stop-loss "
          "and later engine fixes; they are included because they were seen when the threshold was chosen.", "",
          "| variant | backtest CAGR | MC median CAGR | 5–95% CAGR | backtest Sharpe | MC 5% Sharpe | "
          "1-in-20 max DD | P(CAGR < NIFTY) |", "|---|---|---|---|---|---|---|---|"]
    show = variants[(variants.family == "leverage") | variants.chosen
                    | variants.variant.isin(variants.nlargest(5, "backtest_calmar").variant)
                    | variants.variant.isin(variants.nlargest(3, "backtest_cagr").variant)]
    for v in show.sort_values("backtest_cagr", ascending=False).itertuples():
        tag = " **(chosen)**" if v.chosen else ""
        L.append(f"| {v.variant}{tag} | {pct(v.backtest_cagr)} | {pct(v.mc_median_cagr)} | {pct(v.mc_p05_cagr)} – "
                 f"{pct(v.mc_p95_cagr)} | {v.backtest_sharpe:.2f} | {v.mc_p05_sharpe:.2f} | "
                 f"{pct(v.mc_p05_max_drawdown)} | {v.p_cagr_below_nifty:.0%} |")
    L += ["", f"Leverage runs, the chosen run, the top 3 by CAGR and top 5 by Calmar shown; all {V} are in "
          "`variants.csv`.", "", "![variants](variants_cagr.png)", "",
          "**Reality Check (White 2000).** Each variant's excess return is recentred to zero, so every variant "
          "has no skill but keeps its own volatility and its correlation with the others. On each path the best "
          "Sharpe across all variants is what a pure search would have found.", "",
          "| | value |", "|---|---|",
          f"| best no-skill Sharpe, mean | {rc['null_best_sharpe_mean']:.2f} |",
          f"| best no-skill Sharpe, 95th pct | {rc['null_best_sharpe_p95']:.2f} |",
          f"| chosen Sharpe | {rc['observed_chosen_sharpe']:.2f} → p = {rc['p_value_chosen']:.3f} |",
          f"| best observed Sharpe ({rc['observed_best_variant']}) | {rc['observed_best_sharpe']:.2f} → "
          f"p = {rc['p_value_best']:.3f} |",
          f"| effective independent trials | {n_eff:.1f} of {V} |", "",
          "![reality check](reality_check.png)", "",
          "**Selection optimism.** On each path pick the winner by the criterion, then compare its in-sample value "
          "with its average over all paths.", "",
          "| pool | picked by | CAGR optimism | Sharpe optimism | chosen run wins | most frequent winner | "
          "variants that ever win |", "|---|---|---|---|---|---|---|"]
    for v in selection.itertuples():
        L.append(f"| {v.pool} | {v.picked_by} | {pct(v.optimism_cagr)} | {v.optimism_sharpe:.2f} | {v.chosen_win_share:.1%} | "
                 f"{v.most_frequent_winner} ({v.its_win_share:.0%}) | {v.n_variants_ever_winning} |")
    L += ["", "## 4. Deflated Sharpe Ratio (cross-check)", "",
          f"Chosen-run daily excess returns: skew {skew:.2f}, kurtosis {kurt:.1f} (normal = 3). Variant Sharpes: "
          f"mean {variants.backtest_sharpe.mean():.2f}, s.d. {variants.backtest_sharpe.std(ddof=1):.2f}.", "",
          "| trials | hurdle: variant spread (BLdP) | DSR | hurdle: pure noise | DSR vs noise |", "|---|---|---|---|---|"]
    L += [f"| {d.n_trials:g}{' (effective, part 3)' if abs(d.n_trials - round(n_eff, 1)) < 1e-9 else ''} | "
          f"{d.sharpe_hurdle_annual:.2f} | {d.deflated_sharpe_prob:.1%} | {d.noise_hurdle_annual:.2f} | "
          f"{d.noise_deflated_prob:.1%} |" for d in dsr.itertuples()]
    L += ["", "- The formula has to guess the number of independent trials. Part 3 measures it from the variants' "
          "correlation, so read the *effective* row.", "",
          "## 5. What this does not cover", "",
          "- The bootstrap only reshuffles days that happened in 2020–2026. A crash worse than March 2020 cannot "
          "appear, so the left tail is a floor on risk, not a ceiling.",
          "- It resamples portfolio returns, not trades. Path effects inside the engine (assignment chains, margin "
          "limits, the stop-loss reacting to a new path) are held at their historical behaviour.",
          "- The variants are resampled on the same calendar, so a variant that happened to dodge one specific "
          "bad week keeps that edge in every path. That is why the parameter choice is tested in time "
          "separately: `scripts/wheel/walk_forward.py` → WALK_FORWARD.md (fit 2019–2023, test 2024–2026)."]
    (OUT / "MONTE_CARLO.md").write_text("\n".join(L) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}/")
    print(pd.Series(final).to_string())
    print(pd.Series(rc).to_string())
    print(selection.to_string(index=False))


if __name__ == "__main__":
    main()
