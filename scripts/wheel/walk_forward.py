#!/usr/bin/env python3
"""Out-of-sample test of the parameter choice: walk-forward, overfitting probability, and the Reality Check re-examined.

    python scripts/wheel/walk_forward.py grid      # 900 configs × (full period, fresh start at the test split), ~25 min
    python scripts/wheel/walk_forward.py analyse   # everything below, from the saved NAVs + a few fresh runs
    python scripts/wheel/walk_forward.py           # both

Why this exists. Leverage (L3), strikes (put 4% / call 3%) and the 15% put stop were all chosen after seeing the
2020–2026 backtest. The Monte Carlo (monte_carlo.py) can only reshuffle those same days, so it measures how lucky the
chosen run was, not whether the choice carries forward in time. This script asks the time question directly.

The search space is every dimension that was tuned on the full window:
    leverage {1..5} × put offset {2..7%} × call offset {2..7%} × put stop {off, 10, 12, 15, 20%}  = 900 configs.
Rank threshold (1.5) and N (20) are held at params.yaml; they were also fixed in-sample, which this cannot undo.

Parts (each has a match check that must pass before numbers are written):

0. Match. The grid's (L3, 4%, 3%, 15%) column must equal runs/ranked_L3/daily.csv, its L3/15%-stop cells must equal
   strike_grid_L3/navs.csv and its L3/5%/5% stop cells must equal stop_loss/rerun_navs.csv.
1. The split pre-registered in MONTE_CARLO.md §5: fit 2019-12-31 → 2023-12-29, test 2024-01-01 → 2026-06-30.
   The engine is re-run FROM FLAT at the split for every config (no positions carried in from the fit window), so the
   test numbers are what a fresh account would have earned. Reported: the in-sample winner by CAGR / Sharpe / Calmar,
   what it did out of sample, what the shipped params did, where both rank among all 900, and the rank correlation
   between in-sample and out-of-sample performance.
2. Anchored walk-forward, annual steps. Fit on 2020 → end of year Y, trade year Y+1 with the winner, roll. Test years
   2022, 2023, 2024, 2025, 2026 H1. Each test year is a fresh-from-flat engine run of that year's winner; the stitched
   out-of-sample curve is compared with the shipped params over the same years (also fresh each year) and NIFTY 50 TRI.
3. Probability of backtest overfitting (Bailey, Borwein, López de Prado & Zhu 2015), CSCV with 16 blocks: split the
   days into 16 blocks, take every half as in-sample (12,870 splits), pick the best Sharpe in-sample and record its
   out-of-sample rank. PBO = share of splits where the in-sample best lands in the bottom half out of sample.
4. Reality Check re-examined. (a) reproduce monte_carlo.py's p = 0.076 exactly (same seed, same paths); (b) its spread
   over seeds and block lengths; (c) Hansen's SPA (2005), which studentises and stops clearly-bad variants from
   inflating the null; (d) the same tests with NIFTY 50 TRI instead of T-bills as the benchmark; (e) one test with no
   search at all: the shipped params' out-of-sample Sharpe from part 1.

Writes data/backtest/report/walk_forward/. scripts/final_results.py copies it into final_results/.
"""
from __future__ import annotations

import argparse
import itertools
import json
import multiprocessing as mp
import sys
import warnings
from math import comb
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
import yaml

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "wheel"))
from nse.wheel.metrics import ANN, daily_rf, nav_stats  # noqa: E402
from nse.wheel.runner import load_risk_free  # noqa: E402
import monte_carlo as mc  # noqa: E402

REP = ROOT / "data/backtest/report"
OUT = REP / "walk_forward"
LEVERAGES = [1.0, 2.0, 3.0, 4.0, 5.0]
OFFSETS = [0.02, 0.03, 0.04, 0.05, 0.06, 0.07]
STOPS = [None, 0.10, 0.12, 0.15, 0.20]
SPLIT_FIT_END = "2023-12-29"          # last close of the fit window = first signal date of the test window
TEST_YEARS = [2022, 2023, 2024, 2025, 2026]
CRITERIA = ["cagr", "sharpe", "calmar"]
CSCV_BLOCKS = 16
WORKERS = 3                           # each engine process holds ~1.5 GB of market data
_CTX = None                           # set in the parent before forking workers


# ------------------------------------------------------------------------------------------------ configs
def config_id(L: float, p: float, c: float, s: float | None) -> str:
    return f"L{L:g}_p{p:g}_c{c:g}_s{'off' if s is None else f'{s:g}'}"


def all_configs() -> list[dict]:
    return [{"id": config_id(L, p, c, s), "leverage": L, "put": p, "call": c, "stop": s}
            for L, p, c, s in itertools.product(LEVERAGES, OFFSETS, OFFSETS, STOPS)]


def parse_id(cid: str) -> dict:
    L, p, c, s = cid.split("_")
    return {"leverage": float(L[1:]), "put": float(p[1:]), "call": float(c[1:]),
            "stop": None if s == "soff" else float(s[1:])}


def shipped_id(params: dict) -> str:
    b = params["backtest"]
    return config_id(b["leverage"], params["put_strike_filter_pct"], params["call_strike_filter_pct"],
                     b["put_stop_loss_pct"])


def overrides(cfg: dict) -> dict:
    return {"leverage": cfg["leverage"], "put_strike_filter_pct": cfg["put"], "call_strike_filter_pct": cfg["call"],
            "put_stop_loss_pct": cfg["stop"]}


# ------------------------------------------------------------------------------------------------ engine runs
def _run_one(job):
    from nse.wheel.runner import run
    cfg, first, end = job
    kw = overrides(cfg)
    if first is not None:
        kw["first_signal_date"] = pd.Timestamp(first)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        r = run(_CTX, run_id=None, write=False, end=end, **kw)
    return cfg["id"], first, r["daily"].set_index("date").nav


def run_many(jobs: list) -> list:
    """Run (cfg, first_signal_date | None, end | None) jobs on forked workers that share the parent's Context."""
    global _CTX
    if _CTX is None:
        from nse.wheel.runner import Context, load_params
        _CTX = Context(load_params())
    if len(jobs) <= 2:
        return [_run_one(j) for j in jobs]
    with mp.get_context("fork").Pool(WORKERS) as pool:
        out = []
        for i, res in enumerate(pool.imap_unordered(_run_one, jobs, chunksize=4), 1):
            out.append(res)
            if i % 50 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)}", flush=True)
        return out


def cmd_grid():
    OUT.mkdir(parents=True, exist_ok=True)
    cfgs = all_configs()
    print(f"full period: {len(cfgs)} configs", flush=True)
    full = {cid: nav for cid, _, nav in run_many([(c, None, None) for c in cfgs])}
    pd.DataFrame(full)[[c["id"] for c in cfgs]].to_parquet(OUT / "navs_full.parquet")
    print(f"fresh start at {SPLIT_FIT_END}: {len(cfgs)} configs", flush=True)
    test = {cid: nav for cid, _, nav in run_many([(c, SPLIT_FIT_END, None) for c in cfgs])}
    pd.DataFrame(test)[[c["id"] for c in cfgs]].to_parquet(OUT / "navs_test_fresh.parquet")
    pd.DataFrame(cfgs).to_csv(OUT / "configs.csv", index=False)


# ------------------------------------------------------------------------------------------------ statistics
def window_stats(nav: pd.DataFrame, rf: pd.Series) -> pd.DataFrame:
    """nav_stats for every column of a NAV frame (one row per column)."""
    r = nav.pct_change().iloc[1:]
    R, RF = r.to_numpy().T, daily_rf(r.index, rf).to_numpy()[None, :]
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    st = mc.path_stats(R, np.broadcast_to(RF, R.shape), years)
    return pd.DataFrame(st, index=nav.columns)


def pick(stats: pd.DataFrame, crit: str) -> str:
    """In-sample winner; ties broken by config id so the pick is deterministic."""
    best = stats[crit].max()
    return sorted(stats.index[stats[crit] >= best - 1e-15])[0]


def cscv_pbo(X: np.ndarray, blocks: int = CSCV_BLOCKS) -> dict:
    """Probability of backtest overfitting by combinatorially symmetric cross-validation.

    X: (days, configs) per-day excess returns. Rows are cut into `blocks` contiguous blocks; every choice of half the
    blocks is an in-sample set, the rest out-of-sample. Per split: best in-sample Sharpe, its out-of-sample relative
    rank w in (0, 1), logit λ = ln(w / (1 − w)). PBO = P(λ <= 0)."""
    T, N = X.shape
    T -= T % blocks
    B = X[:T].reshape(blocks, T // blocks, N)
    s1, s2, n = B.sum(axis=1), (B ** 2).sum(axis=1), T // blocks       # per-block sums: (blocks, N)
    lam, is_best, oos_of_best = [], [], []
    for ins in itertools.combinations(range(blocks), blocks // 2):
        m = np.zeros(blocks, bool)
        m[list(ins)] = True
        sr = []
        for sel in (m, ~m):
            k = sel.sum() * n
            mu = s1[sel].sum(0) / k
            sd = np.sqrt(np.maximum(s2[sel].sum(0) / k - mu ** 2, 1e-30) * k / (k - 1))
            sr.append(mu / sd)
        j = int(np.argmax(sr[0]))
        rank = (sr[1] < sr[1][j]).sum() + 0.5 * ((sr[1] == sr[1][j]).sum() - 1) + 1     # 1..N, average ties
        w = rank / (N + 1)
        lam.append(np.log(w / (1 - w)))
        is_best.append(sr[0][j] * np.sqrt(ANN))
        oos_of_best.append(sr[1][j] * np.sqrt(ANN))
    lam, is_best, oos_of_best = map(np.asarray, (lam, is_best, oos_of_best))
    slope = np.polyfit(is_best, oos_of_best, 1)[0]
    return {"splits": len(lam), "pbo": float((lam <= 0).mean()), "lambda_median": float(np.median(lam)),
            "is_best_sharpe_mean": float(is_best.mean()), "oos_sharpe_of_is_best_mean": float(oos_of_best.mean()),
            "oos_loss_prob": float((oos_of_best < 0).mean()), "degradation_slope": float(slope),
            "_lambda": lam, "_is": is_best, "_oos": oos_of_best}


def spa_test(D: np.ndarray, idx: np.ndarray) -> dict:
    """White's Reality Check and Hansen's SPA on mean loss differentials, sharing one set of bootstrap paths.

    D: (days, models) per-day performance over the benchmark (positive = beats it). idx: (paths, days) resampled day
    indices. RC uses the raw mean and recentres every model to zero; SPA_c studentises and recentres only models that
    are not clearly worse than the benchmark (threshold sqrt(2 log log n)), so bad models cannot inflate the null."""
    n = D.shape[0]
    dbar = D.mean(0)
    boot = np.stack([D[i].mean(0) for i in idx])                     # (paths, models)
    omega = np.sqrt(n) * boot.std(axis=0, ddof=1)
    rc_obs = np.sqrt(n) * dbar.max()
    rc_null = np.sqrt(n) * (boot - dbar).max(axis=1)
    t = np.sqrt(n) * dbar / omega
    spa_obs = max(t.max(), 0.0)
    keep = t >= -np.sqrt(2 * np.log(np.log(n)))
    g_c = np.where(keep, dbar, 0.0)
    spa_null_c = np.maximum((np.sqrt(n) * (boot - dbar + (dbar - g_c)) / omega).max(axis=1), 0)
    spa_null_u = np.maximum((np.sqrt(n) * (boot - dbar) / omega).max(axis=1), 0)
    return {"models": D.shape[1], "best_model": int(dbar.argmax()), "rc_p": float((rc_null >= rc_obs).mean()),
            "spa_c_p": float((spa_null_c >= spa_obs).mean()), "spa_u_p": float((spa_null_u >= spa_obs).mean()),
            "models_clearly_worse": int((~keep).sum())}


def bootstrap_sharpe_p(ex: np.ndarray, paths: int, seed: int) -> float:
    """One-sided p for annualised Sharpe <= 0 (block bootstrap, recentred)."""
    rng = np.random.default_rng(seed)
    idx = mc.stationary_bootstrap(len(ex), paths, mc.BLOCK, rng)
    obs = ex.mean() / ex.std(ddof=1)
    b = ex[idx]
    null = (b.mean(1) - ex.mean()) / b.std(1, ddof=1)
    return float((null >= obs).mean())


def pct(x: float) -> str:
    return "" if pd.isna(x) else f"{x:.2%}"


def label(cid: str) -> str:
    c = parse_id(cid)
    s = "off" if c["stop"] is None else f"{c['stop']:.0%}"
    return f"L{c['leverage']:g} put {c['put']:.0%} / call {c['call']:.0%} / stop {s}"


# ------------------------------------------------------------------------------------------------ analysis
def cmd_analyse(paths: int):
    OUT.mkdir(parents=True, exist_ok=True)
    params = yaml.safe_load((ROOT / "params.yaml").read_text())
    b = params["backtest"]
    rf = load_risk_free(ROOT / b["risk_free_file"])
    ship = shipped_id(params)
    full = pd.read_parquet(OUT / "navs_full.parquet")
    fresh = pd.read_parquet(OUT / "navs_test_fresh.parquet")
    ids = list(full.columns)
    tri = pd.read_csv(ROOT / b["nifty_index_file"], parse_dates=["trade_date"]).set_index("trade_date").close
    tri = tri.reindex(full.index).ffill()
    assert not tri.isna().any(), "NIFTY 50 TRI must cover every backtest day"

    # ---- 0. match checks ------------------------------------------------------------------------------
    ref = pd.read_csv(ROOT / "data/backtest/runs/ranked_L3/daily.csv", parse_dates=["date"]).set_index("date").nav
    assert ship == "L3_p0.04_c0.03_s0.15" and np.allclose(full[ship], ref, rtol=0, atol=1e-4), "shipped column drifted"
    sg = pd.read_csv(REP / "strike_grid_L3/navs.csv", parse_dates=["date"]).set_index("date")
    for name in sg.columns:
        p, c = (float(x[4:]) for x in name.split("_"))
        assert np.allclose(full[config_id(3.0, p, c, 0.15)], sg[name], atol=1e-4), name
    sl = pd.read_csv(REP / "stop_loss/rerun_navs.csv", parse_dates=["date"]).set_index("date")
    for name in sl.columns:
        L, _, s = name.split("_")
        assert np.allclose(full[config_id(float(L[1:]), .05, .05, None if s == "off" else float(s))], sl[name],
                           atol=1e-4), name
    print(f"[0] grid reproduces ranked_L3, {len(sg.columns)} strike-grid and {len(sl.columns)} stop-sweep NAVs")

    # ---- 1. pre-registered split ----------------------------------------------------------------------
    fit = full.loc[:SPLIT_FIT_END]
    is_st = window_stats(fit, rf)
    oos_st = window_stats(fresh, rf)                                   # fresh from flat at the split
    cont_st = window_stats(full.loc[SPLIT_FIT_END:], rf)               # same window, positions carried in
    bench_oos = window_stats(pd.DataFrame({"NIFTY 50 TRI": tri.loc[SPLIT_FIT_END:]}), rf).iloc[0]
    bench_is = window_stats(pd.DataFrame({"NIFTY 50 TRI": tri.loc[:SPLIT_FIT_END]}), rf).iloc[0]
    l3 = [c for c in ids if c.startswith("L3_")]
    pools = {"all 900": ids, "L3 only (180)": l3}
    split_rows, rankcorr = [], []
    for pool, cols in pools.items():
        for crit in CRITERIA:
            w = pick(is_st.loc[cols], crit)
            o = oos_st.loc[cols]
            split_rows.append({"pool": pool, "picked_by": crit, "winner": w, "is_cagr": is_st.cagr[w],
                               "is_sharpe": is_st.sharpe[w], "is_max_drawdown": is_st.max_drawdown[w],
                               "oos_cagr": o.cagr[w], "oos_sharpe": o.sharpe[w], "oos_max_drawdown": o.max_drawdown[w],
                               "oos_calmar": o.calmar[w], "oos_cagr_continuous": cont_st.cagr[w],
                               f"oos_{crit}_percentile": (o[crit] < o[crit][w]).mean(),
                               "pool_oos_median_cagr": o.cagr.median(), "pool_oos_median_sharpe": o.sharpe.median()})
            rankcorr.append({"pool": pool, "metric": crit,
                             "spearman_is_vs_oos": is_st.loc[cols, crit].rank().corr(o[crit].rank())})
    split = pd.DataFrame(split_rows)
    rankcorr = pd.DataFrame(rankcorr)
    ship_row = {"is_cagr": is_st.cagr[ship], "is_sharpe": is_st.sharpe[ship], "is_max_drawdown": is_st.max_drawdown[ship],
                "is_calmar": is_st.calmar[ship], "oos_cagr": oos_st.cagr[ship], "oos_sharpe": oos_st.sharpe[ship],
                "oos_max_drawdown": oos_st.max_drawdown[ship], "oos_calmar": oos_st.calmar[ship],
                "oos_cagr_continuous": cont_st.cagr[ship],
                "is_cagr_rank_of_900": int((is_st.cagr > is_st.cagr[ship]).sum() + 1),
                "oos_cagr_rank_of_900": int((oos_st.cagr > oos_st.cagr[ship]).sum() + 1),
                "oos_cagr_percentile_all": (oos_st.cagr < oos_st.cagr[ship]).mean(),
                "oos_sharpe_percentile_all": (oos_st.sharpe < oos_st.sharpe[ship]).mean(),
                "oos_cagr_percentile_L3": (oos_st.cagr[l3] < oos_st.cagr[ship]).mean(),
                "nifty_is_cagr": bench_is.cagr, "nifty_oos_cagr": bench_oos.cagr, "nifty_oos_sharpe": bench_oos.sharpe,
                "nifty_oos_max_drawdown": bench_oos.max_drawdown}
    rf_oos = daily_rf(fresh.index[1:], rf).mean() * ANN
    ship_row["avg_tbill_oos"] = rf_oos
    print(f"[1] split {SPLIT_FIT_END}: shipped IS CAGR {pct(ship_row['is_cagr'])} → OOS {pct(ship_row['oos_cagr'])} "
          f"(rank {ship_row['oos_cagr_rank_of_900']}/900); NIFTY OOS {pct(bench_oos.cagr)}")
    print(split.to_string(index=False))
    print(rankcorr.to_string(index=False))

    # ---- 2. anchored walk-forward, annual ------------------------------------------------------------------
    days = full.index
    jobs, folds = [], []
    for y in TEST_YEARS:
        fit_end = days[days < pd.Timestamp(f"{y}-01-01")][-1]
        test_end = days[days <= pd.Timestamp(f"{y}-12-31")][-1]
        st = window_stats(full.loc[:fit_end], rf)
        winners = {crit: pick(st, crit) for crit in CRITERIA}
        winners_l3 = {crit: pick(st.loc[l3], crit) for crit in CRITERIA}
        folds.append({"year": y, "fit_end": fit_end, "test_end": test_end, "winners": winners, "winners_l3": winners_l3})
        for cid in {ship, *winners.values(), *winners_l3.values()}:
            jobs.append((parse_id(cid) | {"id": cid}, fit_end.strftime("%Y-%m-%d"), test_end.strftime("%Y-%m-%d")))
    jobs = list({(j[0]["id"], j[1]): j for j in jobs}.values())
    print(f"[2] {len(jobs)} fresh fold runs", flush=True)
    fold_nav = {(cid, first): nav for cid, first, nav in run_many(jobs)}
    wf_rows, stitched = [], {}
    for f in folds:
        first = f["fit_end"].strftime("%Y-%m-%d")
        seg_tri = tri.loc[f["fit_end"]:f["test_end"]]
        row = {"test_year": f["year"], "fit": f"2019-12-31 → {f['fit_end'].date()}",
               "test": f"{f['fit_end'].date()} → {f['test_end'].date()}",
               "shipped_return": fold_nav[(ship, first)].iloc[-1] / fold_nav[(ship, first)].iloc[0] - 1,
               "nifty_tri_return": seg_tri.iloc[-1] / seg_tri.iloc[0] - 1,
               "grid_median_return_continuous": (full.loc[f["test_end"]] / full.loc[f["fit_end"]] - 1).median()}
        streams = {"shipped params (fixed)": (ship, first), "NIFTY 50 TRI": None}
        for scope, wins in [("", f["winners"]), ("L3 ", f["winners_l3"])]:
            for crit, cid in wins.items():
                nav = fold_nav[(cid, first)]
                row[f"{scope}winner_by_{crit}"] = cid
                row[f"{scope}winner_by_{crit}_return"] = nav.iloc[-1] / nav.iloc[0] - 1
                streams[f"{scope}walk-forward, pick by {crit}"] = (cid, first)
        wf_rows.append(row)
        for name, key in streams.items():
            seg = seg_tri if key is None else fold_nav[key]
            stitched.setdefault(name, []).append(seg.pct_change().iloc[1:])
    wf = pd.DataFrame(wf_rows)
    curves = pd.DataFrame({k: pd.concat(v) for k, v in stitched.items()})
    start = folds[0]["fit_end"]
    curves = pd.concat([pd.DataFrame(0.0, index=[start], columns=curves.columns), curves])
    curves = (1 + curves).cumprod()
    wf_st = window_stats(curves, rf)
    print(wf.to_string(index=False))
    print(wf_st[["cagr", "sharpe", "max_drawdown", "calmar"]].to_string())

    # ---- 3. PBO (CSCV) --------------------------------------------------------------------------------
    r = full.pct_change().iloc[1:]
    rfd = daily_rf(r.index, rf).to_numpy()
    X = r.to_numpy() - rfd[:, None]
    pbo = {pool: cscv_pbo(X[:, [ids.index(c) for c in cols]]) for pool, cols in pools.items()}
    for k, v in pbo.items():
        print(f"[3] PBO {k}: {v['pbo']:.2f} (median logit {v['lambda_median']:.2f}, slope {v['degradation_slope']:.2f})")

    # ---- 4. Reality Check re-examined ------------------------------------------------------------------
    nav_ship = full[ship]
    R = nav_ship.pct_change().dropna().to_numpy()
    T = len(R)
    rng = np.random.default_rng(mc.SEED)
    idx = mc.stationary_bootstrap(T, paths, mc.BLOCK, rng)          # monte_carlo.py's first draw: same paths
    navs58, _ = mc.load_variants(f"ranked_L{b['leverage']:g}", nav_ship.index)
    names58 = list(navs58.columns)
    E58 = navs58.pct_change().iloc[1:].to_numpy() - rfd[:, None]
    obs = E58.mean(0) / E58.std(0, ddof=1) * np.sqrt(ANN)

    def rc_sharpe(E, idx_, chosen_j):
        null = np.full(len(idx_), -np.inf)
        for j in range(E.shape[1]):                                   # one variant at a time: (paths, days) fits in RAM
            ex = E[:, j][idx_]
            null = np.maximum(null, (ex.mean(1) - E[:, j].mean()) / ex.std(1, ddof=1) * np.sqrt(ANN))
        o = E.mean(0) / E.std(0, ddof=1) * np.sqrt(ANN)
        return float((null >= o[chosen_j]).mean()), float((null >= o.max()).mean())

    cj = names58.index(f"ranked_L{b['leverage']:g}")
    p_rep, p_rep_best = rc_sharpe(E58, idx, cj)
    published = pd.read_csv(REP / "monte_carlo/reality_check.csv").iloc[0]
    assert paths != 10_000 or abs(p_rep - published.p_value_chosen) < 1e-12, (p_rep, published.p_value_chosen)
    print(f"[4a] Reality Check reproduced: p = {p_rep:.4f} (published {published.p_value_chosen:.4f})")
    sens = []
    for blk in [5, 10, 21, 42, 63]:
        for seed in range(5):
            i_ = mc.stationary_bootstrap(T, min(paths, 4000), blk, np.random.default_rng(1000 + seed))
            pc, pb = rc_sharpe(E58, i_, cj)
            sens.append({"block": blk, "seed": 1000 + seed, "p_chosen": pc, "p_best": pb})
    sens = pd.DataFrame(sens)
    # SPA and RC on means: vs T-bill and vs NIFTY 50 TRI, for the 58 seen variants and the 900 grid
    tri_r = tri.pct_change().iloc[1:].to_numpy()
    spa_rows = []
    for bench, base in [("T-bill", rfd), ("NIFTY 50 TRI", tri_r)]:
        for pool, Rm in [("58 variants seen before the choice", navs58.pct_change().iloc[1:].to_numpy()),
                         ("900-config walk-forward grid", r.to_numpy())]:
            res = spa_test(Rm - base[:, None], idx[: min(paths, 4000)])
            names = names58 if pool.startswith("58") else ids
            spa_rows.append({"benchmark": bench, "pool": pool, **res, "best_model": names[res["best_model"]]})
    spa = pd.DataFrame(spa_rows)
    print(spa.to_string(index=False))
    # no-search test: the shipped params, out of sample only
    ex_oos = fresh[ship].pct_change().iloc[1:].to_numpy() - daily_rf(fresh.index[1:], rf).to_numpy()
    act_oos = fresh[ship].pct_change().iloc[1:].to_numpy() - tri.loc[SPLIT_FIT_END:].pct_change().iloc[1:].to_numpy()
    oos_tests = {"p_oos_sharpe_le_0_vs_tbill": bootstrap_sharpe_p(ex_oos, paths, 7),
                 "p_oos_active_le_0_vs_nifty": bootstrap_sharpe_p(act_oos, paths, 8)}
    print(oos_tests)

    # ---- tables ---------------------------------------------------------------------------------------
    cfg = pd.DataFrame([parse_id(c) | {"id": c} for c in ids]).set_index("id")
    per_cfg = cfg.join(is_st.add_prefix("is_")).join(oos_st.add_prefix("oos_")).join(
        cont_st[["cagr"]].add_prefix("oos_continuous_"))
    per_cfg.to_csv(OUT / "split_by_config.csv", index_label="config")
    split.to_csv(OUT / "split_winners.csv", index=False)
    pd.DataFrame([ship_row]).to_csv(OUT / "split_shipped.csv", index=False)
    rankcorr.to_csv(OUT / "split_rank_correlation.csv", index=False)
    wf.to_csv(OUT / "walk_forward_folds.csv", index=False)
    wf_st.to_csv(OUT / "walk_forward_stitched.csv", index_label="stream")
    pd.DataFrame([{"pool": k, **{kk: vv for kk, vv in v.items() if not kk.startswith("_")}} for k, v in pbo.items()]
                 ).to_csv(OUT / "pbo.csv", index=False)
    rc = {"published_p_chosen": published.p_value_chosen, "reproduced_p_chosen": p_rep,
          "reproduced_p_best": p_rep_best, "sens_p_chosen_min": sens.p_chosen.min(),
          "sens_p_chosen_max": sens.p_chosen.max(), "sens_p_chosen_block21_mean": sens[sens.block == 21].p_chosen.mean(),
          **oos_tests}
    pd.DataFrame([rc]).to_csv(OUT / "reality_check_recheck.csv", index=False)
    sens.to_csv(OUT / "reality_check_sensitivity.csv", index=False)
    spa.to_csv(OUT / "spa.csv", index=False)

    # ---- charts --------------------------------------------------------------------------------------
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, crit in zip(axes, ["cagr", "sharpe"]):
        f = 100 if crit == "cagr" else 1
        for L in LEVERAGES:
            m = cfg.leverage == L
            ax.scatter(is_st[crit][m] * f, oos_st[crit][m] * f, s=8, alpha=.5, label=f"L{L:g}")
        ax.scatter(is_st[crit][ship] * f, oos_st[crit][ship] * f, s=90, marker="*", color="black", label="shipped")
        ax.axhline((bench_oos[crit]) * f, color="C3", ls="--", lw=1, label="NIFTY 50 TRI (test)")
        rc_ = rankcorr[(rankcorr.pool == "all 900") & (rankcorr.metric == crit)].spearman_is_vs_oos.iloc[0]
        ax.set_xlabel(f"in-sample {crit} (2020–2023)" + (" %" if f == 100 else ""))
        ax.set_ylabel(f"out-of-sample {crit} (2024–2026 H1, fresh start)" + (" %" if f == 100 else ""))
        ax.set_title(f"{crit}: Spearman IS vs OOS = {rc_:.2f}")
    axes[0].legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(OUT / "is_vs_oos.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 5))
    for col in curves.columns:
        if col.startswith("L3 "):
            continue
        ax.plot(curves.index, curves[col], lw=1.6 if "shipped" in col or "NIFTY" in col else 1,
                ls="--" if "NIFTY" in col else "-", label=f"{col} ({pct(wf_st.cagr[col])} CAGR)")
    for f in folds:
        ax.axvline(f["fit_end"], color="grey", lw=.5)
    ax.set_ylabel("growth of 1 (each test year re-started from flat)")
    ax.set_title("Anchored walk-forward: out-of-sample years only")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "walk_forward_equity.png", dpi=130)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
    v = pbo["all 900"]
    axes[0].hist(v["_lambda"], bins=50, color="C0", alpha=.8)
    axes[0].axvline(0, color="C3")
    axes[0].set_title(f"CSCV logit of OOS rank of IS-best (all 900): PBO = {v['pbo']:.2f}")
    axes[1].scatter(v["_is"], v["_oos"], s=3, alpha=.3)
    axes[1].set_xlabel("in-sample Sharpe of IS-best")
    axes[1].set_ylabel("its out-of-sample Sharpe")
    axes[1].set_title(f"degradation slope {v['degradation_slope']:.2f}")
    fig.tight_layout()
    fig.savefig(OUT / "pbo.png", dpi=130)
    plt.close(fig)

    json.dump({"paths": paths, "split_fit_end": SPLIT_FIT_END, "test_years": TEST_YEARS, "configs": len(ids),
               "cscv_blocks": CSCV_BLOCKS, "shipped": ship}, open(OUT / "run_info.json", "w"), indent=2)
    write_md(params, ship, split, ship_row, rankcorr, wf, wf_st, pbo, rc, sens, spa, bench_oos, paths, len(jobs))
    print(f"wrote {OUT.relative_to(ROOT)}/")


# ------------------------------------------------------------------------------------------------ write-up
def write_md(params, ship, split, s, rankcorr, wf, wf_st, pbo, rc, sens, spa, bench_oos, paths, n_fold_runs):
    rk = rankcorr.set_index(["pool", "metric"]).spearman_is_vs_oos
    p900 = pbo["all 900"]
    pl3 = pbo["L3 only (180)"]
    ws = wf_st
    wf_ship, wf_nifty = ws.loc["shipped params (fixed)"], ws.loc["NIFTY 50 TRI"]
    l3s = [parse_id(c) for c in wf["L3 winner_by_sharpe"]]
    stop_txt = lambda c: "off" if c["stop"] is None else f"{c['stop']:.0%}"  # noqa: E731
    picks = pd.Series([f"put {c['put']:.0%} / stop {stop_txt(c)}" for c in l3s]).value_counts()
    wf_streams = ws.loc[[i for i in ws.index if "walk-forward" in i]]
    ship_vs_wf = "ahead of" if wf_ship.cagr > wf_streams.cagr.max() else "not ahead of"
    L = ["# Out-of-sample test of the parameter choice", "",
         f"Generated by `scripts/wheel/walk_forward.py`. Do not edit by hand. Search space: leverage 1–5 × put 2–7% × "
         f"call 2–7% × put stop off/10/12/15/20% = 900 configs, every one a full engine run. Shipped = "
         f"`{label(ship)}`. Rank threshold and N stay at params.yaml in every run.", "",
         "## The answer", "",
         "| | in sample (2020–2023) | out of sample (2024 → 2026-06, fresh start) |", "|---|---|---|",
         f"| Shipped params CAGR | {pct(s['is_cagr'])} (rank {s['is_cagr_rank_of_900']} of 900) | "
         f"**{pct(s['oos_cagr'])}** (rank {s['oos_cagr_rank_of_900']} of 900) |",
         f"| Shipped params Sharpe | {s['is_sharpe']:.2f} | **{s['oos_sharpe']:.2f}** |",
         f"| Shipped params max drawdown | {pct(s['is_max_drawdown'])} | {pct(s['oos_max_drawdown'])} |",
         f"| NIFTY 50 TRI CAGR | {pct(s['nifty_is_cagr'])} | {pct(s['nifty_oos_cagr'])} |",
         f"| Average 3M T-bill | | {pct(s['avg_tbill_oos'])} |",
         f"| Rank correlation, in-sample vs out-of-sample CAGR (900 configs) | | {rk[('all 900', 'cagr')]:.2f} |",
         f"| Probability of backtest overfitting (CSCV, Sharpe, 900 configs) | | {p900['pbo']:.2f} |",
         f"| Anchored walk-forward 2022–2026 H1, pick by CAGR each year | | {pct(ws.cagr['walk-forward, pick by cagr'])} "
         f"CAGR vs shipped {pct(wf_ship.cagr)} vs NIFTY {pct(wf_nifty.cagr)} |",
         f"| Reality Check p (58 variants, published) | {rc['published_p_chosen']:.3f} → reproduced "
         f"{rc['reproduced_p_chosen']:.3f}; range over seeds × block lengths {rc['sens_p_chosen_min']:.3f}–"
         f"{rc['sens_p_chosen_max']:.3f} | |",
         f"| No-search test: shipped OOS Sharpe ≤ 0 | | p = {rc['p_oos_sharpe_le_0_vs_tbill']:.3f} |",
         f"| No-search test: shipped OOS beats NIFTY 50 TRI? (p for active Sharpe ≤ 0) | | "
         f"p = {rc['p_oos_active_le_0_vs_nifty']:.3f} |", "",
         "**Reading.**", "",
         f"- **The shipped params did not hold up out of sample.** Fitted on 2020–2023 they would have made "
         f"{pct(s['is_cagr'])}; from flat on 2024-01 they made {pct(s['oos_cagr'])}, below the T-bill "
         f"({pct(s['avg_tbill_oos'])}) and level with NIFTY ({pct(s['nifty_oos_cagr'])}), with a "
         f"{pct(s['oos_max_drawdown'])} drawdown. Out of sample they rank {s['oos_cagr_rank_of_900']} of 900.",
         f"- **Picking by CAGR is what fails.** The in-sample CAGR winner lands at the "
         f"{split.iloc[0].oos_cagr_percentile:.0%} percentile out of sample. Walk-forward by CAGR earns "
         f"{pct(ws.cagr['walk-forward, pick by cagr'])} a year. Picking by Sharpe at 3× earns "
         f"{pct(ws.cagr['L3 walk-forward, pick by sharpe'])}, Sharpe {ws.sharpe['L3 walk-forward, pick by sharpe']:.2f}, "
         f"max DD {pct(ws.max_drawdown['L3 walk-forward, pick by sharpe'])}. Its yearly picks: "
         + ", ".join(f"{k} ×{v}" for k, v in picks.items()) + ".",
         f"- **The case for the shipped params:** run fixed through the same test years, they made "
         f"{pct(wf_ship.cagr)} a year vs NIFTY {pct(wf_nifty.cagr)}, {ship_vs_wf} every walk-forward stream on CAGR. "
         f"PBO is {p900['pbo']:.2f}, below the 0.5 coin-flip line. And one regime break (a strong 2020–2023 bull "
         "market, then the Sep 2024 → Feb 2025 sell-off) can reverse CAGR ranks without any curve-fitting, because "
         "leverage and tight puts simply pay in rallies and lose in sell-offs.",
         f"- **Why that case is weaker:** the fixed-params stream was chosen with those years in view, so it is not "
         f"evidence. Its Sharpe ({wf_ship.sharpe:.2f}) and drawdown ({pct(wf_ship.max_drawdown)}) are worse than the "
         "Sharpe-picked walk-forward's. A regime explanation also means the CAGR-optimal setting depends on a regime "
         "nobody can call in advance, which is the practical meaning of \"not validated\".", "",
         "## 1. The pre-registered split (fit 2019-12-31 → 2023-12-29, test 2024-01-01 → 2026-06-30)", "",
         "Every config is re-run from flat at the split, so no position opened in the fit window leaks into the test. "
         f"The *continuous* column is the same window cut out of the full-period run (positions carried in) for "
         f"comparison: shipped {pct(s['oos_cagr_continuous'])}.", "",
         "**Who would have been picked on 2020–2023, and what they then did**", "",
         "| pool | picked by | winner | IS CAGR | IS Sharpe | OOS CAGR | OOS Sharpe | OOS max DD | OOS percentile on "
         "that criterion | pool OOS median CAGR |", "|---|---|---|---|---|---|---|---|---|---|"]
    for v in split.itertuples():
        L.append(f"| {v.pool} | {v.picked_by} | {label(v.winner)} | {pct(v.is_cagr)} | {v.is_sharpe:.2f} | "
                 f"{pct(v.oos_cagr)} | {v.oos_sharpe:.2f} | {pct(v.oos_max_drawdown)} | "
                 f"{getattr(v, f'oos_{v.picked_by}_percentile'):.0%} | {pct(v.pool_oos_median_cagr)} |")
    L += ["", f"Shipped params out of sample: CAGR percentile {s['oos_cagr_percentile_all']:.0%} of all 900, "
          f"{s['oos_cagr_percentile_L3']:.0%} of the L3 configs; Sharpe percentile {s['oos_sharpe_percentile_all']:.0%}.", "",
          "**Does in-sample rank predict out-of-sample rank?** (Spearman across configs)", "",
          "| pool | CAGR | Sharpe | Calmar |", "|---|---|---|---|"]
    for pool in ["all 900", "L3 only (180)"]:
        L.append(f"| {pool} | {rk[(pool, 'cagr')]:.2f} | {rk[(pool, 'sharpe')]:.2f} | {rk[(pool, 'calmar')]:.2f} |")
    L += ["", f"- **CAGR ranks {'reverse' if rk[('all 900', 'cagr')] < 0 else 'persist'}** "
          f"({rk[('all 900', 'cagr')]:.2f} across all 900, {rk[('L3 only (180)', 'cagr')]:.2f} within L3). CAGR is how "
          "put 4% / call 3% was picked from the strike grid, and how 3× was preferred to 2×.",
          "- **Sharpe and Calmar ranks persist**, but across all 900 that is largely leverage: low-leverage, far-OTM "
          "configs have the best risk ratios in both windows. Within L3 (same leverage, so only strikes and stop "
          f"differ) the persistence is weaker: {rk[('L3 only (180)', 'sharpe')]:.2f} for Sharpe, "
          f"{rk[('L3 only (180)', 'calmar')]:.2f} for Calmar.", "",
          "![is vs oos](is_vs_oos.png)", "",
          "## 2. Anchored walk-forward (annual re-fit, each test year started from flat)", "",
          "| test year | fit window | winner by CAGR | its return | winner by Calmar | its return | shipped return | "
          "NIFTY 50 TRI | grid median (continuous) |", "|---|---|---|---|---|---|---|---|---|"]
    for v in wf.itertuples():
        L.append(f"| {v.test_year} | {v.fit} | {label(v.winner_by_cagr)} | {pct(v.winner_by_cagr_return)} | "
                 f"{label(v.winner_by_calmar)} | {pct(v.winner_by_calmar_return)} | {pct(v.shipped_return)} | "
                 f"{pct(v.nifty_tri_return)} | {pct(v.grid_median_return_continuous)} |")
    L += ["", "**Stitched out-of-sample years**", "",
          "| stream | CAGR | Sharpe | max DD | Calmar |", "|---|---|---|---|---|"]
    for name, v in ws.iterrows():
        L.append(f"| {name} | {pct(v.cagr)} | {v.sharpe:.2f} | {pct(v.max_drawdown)} | {v.calmar:.2f} |")
    L += ["", f"- {n_fold_runs} fresh engine runs. Each test year starts from cash; positions still open at year-end "
          "are marked to market. Every stream (shipped included) is restarted the same way.",
          "- The *shipped* stream is not a walk-forward: its params were chosen with the 2022–2026 data in view. It is the "
          "benchmark the walk-forward has to be compared with, not a validation of itself.", "",
          "![walk-forward](walk_forward_equity.png)", "",
          f"## 3. Probability of backtest overfitting (CSCV, {CSCV_BLOCKS} blocks, {p900['splits']:,} splits, Sharpe)", "",
          "| pool | PBO | median logit | IS-best Sharpe (mean) | its OOS Sharpe (mean) | P(OOS Sharpe < 0) | "
          "degradation slope |", "|---|---|---|---|---|---|---|"]
    for pool, v in pbo.items():
        L.append(f"| {pool} | **{v['pbo']:.2f}** | {v['lambda_median']:.2f} | {v['is_best_sharpe_mean']:.2f} | "
                 f"{v['oos_sharpe_of_is_best_mean']:.2f} | {v['oos_loss_prob']:.0%} | {v['degradation_slope']:.2f} |")
    L += ["", "- PBO is the share of splits where the in-sample best config lands in the *bottom half* out of sample. "
          "0.5 means picking the best is no better than picking at random; below 0.5 the search carries information.",
          "- CSCV shuffles calendar blocks, so unlike part 2 it lets the test half sit before the fit half. It answers "
          "\"is the ranking stable across sub-periods\", not \"does it hold in the future\".", "",
          "![pbo](pbo.png)", "",
          "## 4. The Reality Check p = 0.076, re-examined", "",
          f"- **Reproduced exactly.** Re-drawing monte_carlo.py's first {paths:,} block-21 paths with seed {mc.SEED} "
          f"gives p = {rc['reproduced_p_chosen']:.4f} for the shipped run (published {rc['published_p_chosen']:.4f}); "
          f"{rc['reproduced_p_best']:.4f} for the best observed variant.",
          f"- **It is not a fixed number.** Over 5 seeds × block lengths 5–63 days the shipped p ranges "
          f"{rc['sens_p_chosen_min']:.3f}–{rc['sens_p_chosen_max']:.3f}:", "",
          "| mean block (days) | p, mean of 5 seeds | min | max |", "|---|---|---|---|"]
    for blk, g in sens.groupby("block"):
        L.append(f"| {blk} | {g.p_chosen.mean():.3f} | {g.p_chosen.min():.3f} | {g.p_chosen.max():.3f} |")
    L += ["", "- **What it tests.** The null is *no variant beats the T-bill*. That is a low bar for a 3×-levered "
          "short-volatility book; the question an allocator asks is whether it beats the index. The table below runs "
          "White's Reality Check and Hansen's SPA on mean daily excess return against both benchmarks, on the 58 "
          "variants actually seen and on the 900-config grid.", "",
          "| benchmark | pool | RC p | SPA_c p | SPA_u p | variants clearly worse than benchmark | best variant |",
          "|---|---|---|---|---|---|---|"]
    for v in spa.itertuples():
        L.append(f"| {v.benchmark} | {v.pool} | {v.rc_p:.3f} | {v.spa_c_p:.3f} | {v.spa_u_p:.3f} | "
                 f"{v.models_clearly_worse} of {v.models} | {v.best_model} |")
    L += ["", "- SPA_c is Hansen's consistent version: it studentises each variant and leaves out of the null those "
          "clearly worse than the benchmark, so it is less conservative than RC. SPA_u (no exclusion) is the upper bound.",
          f"- **No search, out of sample.** Testing only the shipped config on 2024–2026 (fresh start) is one "
          f"hypothesis, so no multiple-testing penalty applies: p(Sharpe ≤ 0 vs "
          f"T-bill) = {rc['p_oos_sharpe_le_0_vs_tbill']:.3f}, p(no outperformance of NIFTY 50 TRI) = "
          f"{rc['p_oos_active_le_0_vs_nifty']:.3f}. Caveat: the 2024–2026 data *was* in the full-window backtest used "
          "to choose them, so this is out-of-sample for this test's split, not a genuinely unseen period.", "",
          "## 5. Limits", "",
          "- The only data never used to choose anything is data after 2026-06-30. Everything here is a re-analysis "
          "of a window the parameters were already fitted on; it can show the choice is fragile, it cannot fully "
          "clear it. A paper or small-size live period is the only clean out-of-sample test left.",
          "- Rank threshold (1.5) and N (20) were also chosen in-sample and are held fixed here.",
          "- Two and a half test years contain one major drawdown (Sep 2024 → Feb 2025). One episode dominates "
          "the out-of-sample number; a different split would weigh it differently, which is why part 2 and part 3 exist."]
    (OUT / "WALK_FORWARD.md").write_text("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="all", choices=["grid", "analyse", "all"])
    ap.add_argument("--paths", type=int, default=10_000)
    a = ap.parse_args()
    if a.cmd in ("grid", "all"):
        cmd_grid()
    if a.cmd in ("analyse", "all"):
        cmd_analyse(a.paths)


if __name__ == "__main__":
    main()
