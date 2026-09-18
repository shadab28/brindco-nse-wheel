"""Check records, phase reports, gate state and the repair log shared by every validation phase."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "data_validation" / "reports"
REPAIR_LOG = REPORTS / "REPAIR_LOG.csv"
ACCEPTED = ROOT / "data_validation" / "accepted_limitations.json"


def accepted_limitations() -> dict:
    """Critical failures the project owner explicitly accepted as documented limitations (never silently)."""
    return json.loads(ACCEPTED.read_text()) if ACCEPTED.exists() else {}
DSN = "postgresql://shadab@127.0.0.1:5440/nse"


@dataclass
class Check:
    id: str
    name: str
    critical: bool
    passed: bool
    detail: str = ""
    n_checked: int | None = None
    n_failed: int = 0
    failures: list = field(default_factory=list)          # sample of failing records (<= 25)
    category: str = ""

    def status(self) -> str:
        if self.passed:
            return "PASS" if not self.n_failed else "INFO"          # informational: records listed, not a failure
        return "FAIL (CRITICAL)" if self.critical else "FAIL"


@dataclass
class PhaseResult:
    phase: int
    title: str
    category: str
    sources: str
    methodology: str
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tests_run: str = ""

    def add(self, cid, name, critical, failed_records=None, n_checked=None, detail="", passed=None):
        failed_records = [] if failed_records is None else failed_records
        if isinstance(failed_records, pd.DataFrame):
            n_failed = len(failed_records)
            sample = json.loads(failed_records.head(25).to_json(orient="records", date_format="iso"))
        else:
            n_failed = len(failed_records)
            sample = list(failed_records)[:25]
        ok = (n_failed == 0) if passed is None else passed
        c = Check(cid, name, critical, bool(ok), detail, n_checked, n_failed, sample, self.category)
        self.checks.append(c)
        return c

    @property
    def strict_passed(self) -> bool:
        return all(c.passed for c in self.checks if c.critical)

    @property
    def accepted_failures(self) -> list[str]:
        acc = accepted_limitations()
        return [c.id for c in self.checks if c.critical and not c.passed and c.id in acc]

    @property
    def gate_passed(self) -> bool:
        acc = set(self.accepted_failures)
        return all(c.passed or c.id in acc for c in self.checks if c.critical)

    @property
    def gate_status(self) -> str:
        if self.strict_passed:
            return "PASS"
        return "CONDITIONAL PASS / PROCEED WITH KNOWN LIMITATION" if self.gate_passed else "FAIL"

    def write(self) -> Path:
        REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        js = {"phase": self.phase, "title": self.title, "category": self.category, "generated": stamp,
              "gate_passed": self.gate_passed, "gate_status": self.gate_status,
              "accepted_limitations": {k: accepted_limitations()[k] for k in self.accepted_failures}, "sources": self.sources, "methodology": self.methodology,
              "tests_run": self.tests_run, "notes": self.notes, "checks": [asdict(c) for c in self.checks]}
        (REPORTS / f"phase_{self.phase:02d}.json").write_text(json.dumps(js, indent=1, default=str))
        n_pass = sum(c.passed for c in self.checks)
        lines = [f"# Phase {self.phase} — {self.title}", "",
                 f"**Generated:** {stamp}  ", f"**Gate:** {self.gate_status}  ",
                 f"**Checks:** {len(self.checks)} run, {n_pass} passed, {len(self.checks) - n_pass} failed "
                 f"({sum(1 for c in self.checks if c.critical and not c.passed)} critical)", "",
                 "## Data sources", "", self.sources, "", "## Methodology", "", self.methodology, ""]
        if self.tests_run:
            lines += ["## Tests run", "", self.tests_run, ""]
        lines += ["## Results", "", "| ID | Check | Critical | Checked | Failed | Status | Detail |",
                  "|---|---|---|---:|---:|---|---|"]
        for c in self.checks:
            det = c.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {c.id} | {c.name} | {'yes' if c.critical else 'no'} | "
                         f"{'' if c.n_checked is None else f'{c.n_checked:,}'} | {c.n_failed:,} | {c.status()} | {det} |")
        acc = accepted_limitations()
        if self.accepted_failures:
            lines += ["## Accepted limitations", ""]
            for cid in self.accepted_failures:
                lines += [f"**{cid}** — {acc[cid]['status']} (accepted {acc[cid]['accepted_on']} by {acc[cid]['accepted_by']})", "",
                          f"> {acc[cid]['limitation']}", "", acc[cid].get("instruction", ""), ""]
        fails = [c for c in self.checks if not c.passed or c.n_failed]
        lines += ["", "## Failures", ""]
        if not fails:
            lines.append("None.")
        for c in fails:
            lines += [f"### {c.id} — {c.name} ({c.status()})", "", c.detail, "",
                      f"{c.n_failed:,} failing record(s); first {len(c.failures)}:", "", "```",
                      json.dumps(c.failures, indent=1, default=str)[:6000], "```", ""]
        if self.notes:
            lines += ["## Notes", ""] + [f"- {n}" for n in self.notes]
        path = REPORTS / f"PHASE_{self.phase:02d}_{self.title.upper().replace(' ', '_').replace('/', '_').replace('-', '_')}.md"
        path.write_text("\n".join(lines) + "\n")
        return path


def gate_passed(phase: int) -> bool:
    p = REPORTS / f"phase_{phase:02d}.json"
    return p.exists() and json.loads(p.read_text())["gate_passed"]


def log_repair(phase: int, dataset: str, key: str, field_: str, old, new, reason: str, source: str):
    """Every data repair is appended here; validation never changes data without a row in this log."""
    REPORTS.mkdir(parents=True, exist_ok=True)
    row = pd.DataFrame([{"logged": datetime.now().isoformat(timespec="seconds"), "phase": phase, "dataset": dataset,
                         "key": key, "field": field_, "old": old, "new": new, "reason": reason, "source": source}])
    row.to_csv(REPAIR_LOG, mode="a", header=not REPAIR_LOG.exists(), index=False)


def query(sql: str, params=None) -> pd.DataFrame:
    import warnings

    import psycopg2
    with psycopg2.connect(DSN) as con, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.read_sql(sql, con, params=params)
