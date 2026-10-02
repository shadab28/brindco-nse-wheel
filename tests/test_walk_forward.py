"""scripts/wheel/walk_forward.py: config ids round-trip, and PBO / SPA behave as their papers say on synthetic data."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "wheel"))
import monte_carlo as mc  # noqa: E402
import walk_forward as wf  # noqa: E402


def test_config_grid_and_ids():
    cfgs = wf.all_configs()
    assert len(cfgs) == 900 and len({c["id"] for c in cfgs}) == 900
    for c in cfgs[::37]:
        assert wf.parse_id(c["id"]) == {k: c[k] for k in ["leverage", "put", "call", "stop"]}
    params = {"put_strike_filter_pct": 0.04, "call_strike_filter_pct": 0.03,
              "backtest": {"leverage": 3.0, "put_stop_loss_pct": 0.15}}
    assert wf.shipped_id(params) == "L3_p0.04_c0.03_s0.15"
    assert wf.parse_id("L1_p0.05_c0.05_soff")["stop"] is None


def test_pick_is_deterministic_on_ties():
    st = pd.DataFrame({"cagr": [0.1, 0.2, 0.2]}, index=["b", "z", "a"])
    assert wf.pick(st, "cagr") == "a"


def test_pbo_noise_vs_skill():
    rng = np.random.default_rng(5)
    noise = rng.normal(0, 0.01, (1600, 40))
    assert 0.3 < wf.cscv_pbo(noise, blocks=10)["pbo"] < 0.7        # no skill: the IS winner is a coin flip OOS
    skill = noise.copy()
    skill[:, 7] += 0.004                                           # one config has a real, persistent edge
    v = wf.cscv_pbo(skill, blocks=10)
    assert v["pbo"] < 0.05 and v["oos_sharpe_of_is_best_mean"] > 2


def test_spa_rejects_real_edge_not_noise():
    rng = np.random.default_rng(6)
    idx = mc.stationary_bootstrap(1500, 1500, 10, rng)
    null = rng.normal(0, 0.01, (1500, 20))
    null -= null.mean(0)                                           # every model exactly as good as the benchmark
    r0 = wf.spa_test(null, idx)
    assert r0["rc_p"] > 0.2 and r0["spa_c_p"] > 0.2
    edge = null.copy()
    edge[:, 3] += 0.0015
    r1 = wf.spa_test(edge, idx)
    assert r1["best_model"] == 3 and r1["spa_c_p"] < 0.01 and r1["rc_p"] < 0.01
    bad = np.hstack([edge[:, :5] * 0 + rng.normal(0, 0.03, (1500, 5)) - 0.003, null[:, :5] + 0.0002])
    r2 = wf.spa_test(bad, idx)                                     # clearly-bad models are dropped from SPA_c's null
    assert r2["models_clearly_worse"] == 5 and r2["spa_c_p"] <= r2["spa_u_p"] + 1e-12
