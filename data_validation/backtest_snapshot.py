"""Run the current backtest configuration (params.yaml) at every leverage and store what the data phases check:
trades (closed + open), daily state, engine events, liquidity audit, universe exits, selection log."""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.simplefilter("ignore")

import pandas as pd  # noqa: E402

from nse.wheel.runner import Context, load_params, run  # noqa: E402

OUT = Path(__file__).resolve().parent / "output" / "backtest"


def snapshot():
    OUT.mkdir(parents=True, exist_ok=True)
    p = load_params()
    ctx = Context(p)
    for L in p["leverage_grid"]:
        r = run(ctx, leverage=L, write=False)
        tag = f"L{L:g}"
        t = pd.concat([r["trades"].assign(open=False), r["open"].assign(open=True)], ignore_index=True)
        t.to_parquet(OUT / f"trades_{tag}.parquet")
        r["daily"].to_parquet(OUT / f"daily_{tag}.parquet")
        r["events"].astype(str).to_parquet(OUT / f"events_{tag}.parquet")
        r["audit"].to_parquet(OUT / f"audit_{tag}.parquet")
        r["ledger"].to_parquet(OUT / f"ledger_{tag}.parquet")
        r["selection_log"].to_parquet(OUT / f"selection_{tag}.parquet")
        r["universe_exits"].astype(str).to_parquet(OUT / f"universe_exits_{tag}.parquet")
        json.dump(r["metrics"], open(OUT / f"metrics_{tag}.json", "w"), indent=1, default=str)
        print(tag, len(t), "trades", flush=True)
    json.dump({k: v for k, v in p.items() if k != "stress_windows"}, open(OUT / "params.json", "w"), indent=1, default=str)


def load(tag: str, what: str = "trades") -> pd.DataFrame:
    return pd.read_parquet(OUT / f"{what}_{tag}.parquet")


if __name__ == "__main__":
    snapshot()
