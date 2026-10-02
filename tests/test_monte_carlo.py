"""scripts/wheel/monte_carlo.py: the vectorised scorer must equal nav_stats, and the bootstrap/selection helpers
must do what their docstrings say."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "wheel"))
import monte_carlo as mc  # noqa: E402
from nse.wheel.metrics import daily_rf, nav_stats  # noqa: E402


def test_path_stats_equals_nav_stats():
    rng = np.random.default_rng(1)
    days = pd.bdate_range("2020-01-01", periods=800)
    nav = pd.Series(1e7 * np.cumprod(1 + rng.normal(4e-4, 0.01, len(days))), index=days)
    rf = pd.Series(0.06, index=[f"{y}-{m:02d}" for y in range(2019, 2024) for m in range(1, 13)])
    r = nav.pct_change().dropna()
    RF = daily_rf(r.index, rf).to_numpy()
    years = (days[-1] - days[0]).days / 365.25
    got = mc.path_stats(r.to_numpy()[None, :], RF[None, :], years)
    ref = nav_stats(nav, rf)
    for k in mc.KEYS:
        assert abs(got[k][0] - ref[k]) < 1e-12, k


def test_stationary_bootstrap_blocks():
    rng = np.random.default_rng(2)
    idx = mc.stationary_bootstrap(500, 400, 21, rng)
    assert idx.shape == (400, 500) and idx.min() >= 0 and idx.max() < 500
    cont = (np.diff(idx, axis=1) % 500 == 1).mean()          # share of steps that continue the block
    assert abs(cont - (1 - 1 / 21)) < 0.01
    iid = mc.stationary_bootstrap(500, 400, 1, rng)
    assert (np.diff(iid, axis=1) % 500 == 1).mean() < 0.01


def test_expected_max_and_effective_trials():
    rng = np.random.default_rng(3)
    for n in [5, 20, 60]:
        direct = rng.standard_normal((40_000, n)).max(axis=1).mean()
        assert abs(direct - mc.expected_max_z(n)) / direct < 0.03
    assert mc.expected_max_z(1) == 0.0
    assert abs(mc.effective_trials(mc.expected_max_z(12), 100) - 12) < 0.01
    assert mc.effective_trials(10.0, 50) == 50
