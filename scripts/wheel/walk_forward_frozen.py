#!/usr/bin/env python3
"""Walk-forward test of one frozen configuration: L2, put 7%, call 2%, 15% put stop.

    python scripts/wheel/walk_forward_frozen.py            # ~12 engine runs, a few minutes

Nothing is fitted. The configuration is frozen, so the "fit" window 2019-12-31 → 2023-12-29 is only where its numbers
were seen, and the test window 2023-12-29 → 2026-06-30 is where they are checked. The test is run two ways:
    fresh       the engine restarts FROM FLAT at the split (a new account opened on 2023-12-29)
    continuous  the full-period run, measured from the split (positions carried in from 2023)

Caveat that this script cannot remove: put 7% / call 2% was picked after the full 2020–2026 strike grid was seen
(STRIKE_SELECTION / STOP_LOSS, best Calmar cell). The test years were therefore not unseen when the choice was made.
What the split does show is whether the configuration's edge is spread over both windows or carried by one of them.

For context, the same split is run for the shipped params (params.yaml) and for the frozen config's neighbours
(L2 × put 6–7% × call 2–3%), so an isolated peak shows up as a neighbour that collapses out of sample.

Match check: the shipped config's full run must equal runs/ranked_L3/daily.csv before anything is written.
Writes data/backtest/report/walk_forward_frozen/.
"""
from __future__ import annotations

import json
import sys
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
from nse.wheel.metrics import daily_rf  # noqa: E402
from nse.wheel.runner import load_risk_free  # noqa: E402
import walk_forward as wfm  # noqa: E402

OUT = ROOT / "data/backtest/report/walk_forward_frozen"
SPLIT = wfm.SPLIT_FIT_END
FROZEN = {"leverage": 2.0, "put": 0.07, "call": 0.02, "stop": 0.15}
NEIGHBOURS = [(0.06, 0.02), (0.06, 0.03), (0.07, 0.03)]
PATHS = 10_000


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    params = yaml.safe_load((ROOT / "params.yaml").read_text())
    b = params["backtest"]
    rf = load_risk_free(ROOT / b["risk_free_file"])
    frozen = wfm.config_id(*FROZEN.values())
    ship = wfm.shipped_id(params)
    cids = [frozen, ship] + [wfm.config_id(FROZEN["leverage"], p, c, FROZEN["stop"]) for p, c in NEIGHBOURS]
    jobs = [(wfm.parse_id(c) | {"id": c}, first, None) for c in cids for first in (None, SPLIT)]
    res = wfm.run_many(jobs)
    full = pd.DataFrame({c: nav for c, first, nav in res if first is None})[cids]
    fresh = pd.DataFrame({c: nav for c, first, nav in res if first is not None})[cids]

    ref = pd.read_csv(ROOT / "data/backtest/runs/ranked_L3/daily.csv", parse_dates=["date"]).set_index("date").nav
    assert np.allclose(full[ship], ref, rtol=0, atol=1e-4), "shipped full run no longer reproduces ranked_L3"
    print(f"[0] {ship} full run reproduces runs/ranked_L3/daily.csv")

    tri = pd.read_csv(ROOT / b["nifty_index_file"], parse_dates=["trade_date"]).set_index("trade_date").close
    tri = tri.reindex(full.index).ffill()
    full["NIFTY 50 TRI"] = tri
    fresh["NIFTY 50 TRI"] = tri.reindex(fresh.index)

    windows = {"fit 2020–2023": full.loc[:SPLIT], "test 2024–2026 (fresh)": fresh,
               "test 2024–2026 (continuous)": full.loc[SPLIT:], "full 2020–2026": full}
    rows = []
    for w, nav in windows.items():
        st = wfm.window_stats(nav, rf)
        for col in nav.columns:
            rows.append({"window": w, "config": col, "start": nav.index[0].date(), "end": nav.index[-1].date(),
                         **st.loc[col, ["cagr", "annualized_vol", "sharpe", "max_drawdown", "calmar"]].to_dict()})
    stats = pd.DataFrame(rows)

    # year by year, frozen config: fit-window years from the full run, test-window years from the fresh run
    yr = []
    for y in range(2020, 2027):
        src = full if y <= 2023 else fresh
        seg = src.loc[(src.index.year == y) | (src.index == src.index[src.index < f"{y}-01-01"].max())]
        seg = seg.loc[seg.index >= src.index[0]]
        yr.append({"year": y, "window": "fit" if y <= 2023 else "test",
                   **{c: seg[c].iloc[-1] / seg[c].iloc[0] - 1 for c in (frozen, ship, "NIFTY 50 TRI")}})
    yearly = pd.DataFrame(yr)

    # no-search tests on the test window only: Sharpe <= 0 over T-bills, and active return <= 0 over NIFTY 50 TRI
    r = fresh[frozen].pct_change().iloc[1:].to_numpy()
    ex = r - daily_rf(fresh.index[1:], rf).to_numpy()
    act = r - fresh["NIFTY 50 TRI"].pct_change().iloc[1:].to_numpy()
    tests = {"p_test_sharpe_le_0_vs_tbill": wfm.bootstrap_sharpe_p(ex, PATHS, 7),
             "p_test_active_le_0_vs_nifty": wfm.bootstrap_sharpe_p(act, PATHS, 8)}

    stats.to_csv(OUT / "split_stats.csv", index=False)
    yearly.to_csv(OUT / "yearly_returns.csv", index=False)
    full.to_csv(OUT / "navs_full.csv", index_label="date")
    fresh.to_csv(OUT / "navs_test_fresh.csv", index_label="date")
    json.dump({"frozen": frozen, "shipped": ship, "split": SPLIT, "paths": PATHS, **tests},
              open(OUT / "run_info.json", "w"), indent=2)

    fig, ax = plt.subplots(1, 2, figsize=(13, 4.5))
    for c, lab in [(frozen, "frozen L2 7%/2%"), (ship, "shipped L3 4%/3%"), ("NIFTY 50 TRI", "NIFTY 50 TRI")]:
        ax[0].plot(full.index, full[c] / full[c].iloc[0], label=lab)
        ax[1].plot(fresh.index, fresh[c] / fresh[c].iloc[0], label=lab)
    ax[0].axvline(pd.Timestamp(SPLIT), color="grey", ls="--", lw=1)
    ax[0].set_title("Full period (split dashed)")
    ax[1].set_title(f"Test window, fresh from flat at {SPLIT}")
    for a in ax:
        a.set_yscale("log")
        a.legend()
        a.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(OUT / "equity.png", dpi=130)

    write_md(stats, yearly, tests, frozen, ship)
    print(stats[stats.config.isin([frozen, ship, "NIFTY 50 TRI"])].to_string(index=False))
    print(yearly.to_string(index=False))
    print(tests)
    print(f"wrote {OUT.relative_to(ROOT)}/")


def write_md(stats, yearly, tests, frozen, ship):
    p = wfm.pct

    def name(c):
        return c if c == "NIFTY 50 TRI" else wfm.label(c)

    L = ["# Walk-forward: frozen L2 / put 7% / call 2% / stop 15%", "",
         "Generated by `scripts/wheel/walk_forward_frozen.py`. Do not edit by hand.", "",
         f"Fit window 2019-12-31 → {SPLIT}, test window {SPLIT} → 2026-06-30. The configuration is frozen; nothing is "
         "re-fitted at the split. **It was chosen after the full 2020–2026 grid was seen, so the test years are not "
         "truly unseen.** The split shows whether the result holds in both halves, not that it was predicted.", "",
         "## Fit vs test", "",
         "| window | config | CAGR | vol | Sharpe | max DD | Calmar |", "|---|---|---:|---:|---:|---:|---:|"]
    main = stats[stats.config.isin([frozen, ship, "NIFTY 50 TRI"])]
    for _, r in main.iterrows():
        L.append(f"| {r.window} | {name(r.config)} | {p(r.cagr)} | {p(r.annualized_vol)} | {r.sharpe:.2f} | "
                 f"{p(r.max_drawdown)} | {r.calmar:.2f} |")
    L += ["", "## Neighbours (L2, stop 15%)", "",
          "| config | fit CAGR | fit max DD | test CAGR (fresh) | test max DD | test Sharpe |", "|---|---:|---:|---:|---:|---:|"]
    fit, te = (stats[stats.window == w].set_index("config") for w in ("fit 2020–2023", "test 2024–2026 (fresh)"))
    for c in fit.index:
        if c.startswith("L2_"):
            L.append(f"| {name(c)} | {p(fit.cagr[c])} | {p(fit.max_drawdown[c])} | {p(te.cagr[c])} | "
                     f"{p(te.max_drawdown[c])} | {te.sharpe[c]:.2f} |")
    L += ["", "## Year by year", "", "2020–2023 from the full run, 2024–2026 H1 from the fresh test run.", "",
          "| year | window | frozen L2 7%/2% | shipped L3 4%/3% | NIFTY 50 TRI |", "|---|---|---:|---:|---:|"]
    for _, r in yearly.iterrows():
        L.append(f"| {r.year} | {r.window} | {p(r[frozen])} | {p(r[ship])} | {p(r['NIFTY 50 TRI'])} |")
    L += ["", "## Test-window significance (no search)", "",
          f"- P(Sharpe over T-bills ≤ 0), block bootstrap: **{tests['p_test_sharpe_le_0_vs_tbill']:.3f}**",
          f"- P(active return over NIFTY 50 TRI ≤ 0): **{tests['p_test_active_le_0_vs_nifty']:.3f}**", "",
          "![equity](equity.png)", ""]
    (OUT / "WALK_FORWARD_FROZEN.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
