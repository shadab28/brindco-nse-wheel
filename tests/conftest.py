import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nse.wheel.costs import default_cost_model  # noqa: E402
from nse.wheel.data import load_market  # noqa: E402


@pytest.fixture(scope="session")
def params():
    p = yaml.safe_load(open(ROOT / "params.yaml"))
    return {**{k: v for k, v in p.items() if k != "backtest"}, **p["backtest"]}


@pytest.fixture(scope="session")
def md(params):
    cache = ROOT / params["base_dir"] / "cache"
    if not (cache / "options.parquet").exists():
        pytest.skip("market cache not built (scripts/wheel/backtest.py cache)")
    return load_market(cache, ROOT / params["expiry_file"], ROOT / params["terminations_file"])


@pytest.fixture(scope="session")
def costs(params):
    return default_cost_model(params)


@pytest.fixture(scope="session")
def detected_ca():
    f = ROOT / "data/backtest/corporate_actions_detected.csv"
    if not f.exists():
        pytest.skip("corporate actions not detected yet")
    return pd.read_csv(f, parse_dates=["ex_date"])
