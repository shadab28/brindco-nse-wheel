"""Run one validation phase. A phase refuses to start unless every earlier phase's gate passed."""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_validation.common import gate_passed  # noqa: E402

PHASES = {0: "phase00_inventory", 1: "phase01_universe", 2: "phase02_fo_eligibility"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", type=int, required=True)
    ap.add_argument("--force", action="store_true", help="run even if an earlier gate failed (report only)")
    a = ap.parse_args()
    blocked = [p for p in range(a.phase) if not gate_passed(p)]
    if blocked and not a.force:
        sys.exit(f"Phase {a.phase} blocked: gate not passed for phase(s) {blocked}")
    mod = importlib.import_module(f"data_validation.{PHASES[a.phase]}")
    res = mod.run()
    path = res.write()
    print(f"Phase {a.phase}: gate {res.gate_status} -> {path}")
    for c in res.checks:
        print(f"  {c.id:10s} {c.status():16s} failed={c.n_failed:<8} {c.name}")
    sys.exit(0 if res.gate_passed else 1)


if __name__ == "__main__":
    main()
