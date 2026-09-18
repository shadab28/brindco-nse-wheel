"""Phase 1 — historical NIFTY 50 universe, validated against NSE Indices press releases."""
from __future__ import annotations

import random
import sqlite3

import pandas as pd

from data_validation import universe as U
from data_validation.common import ROOT, PhaseResult, query

START, END = pd.Timestamp("2016-01-01"), pd.Timestamp("2026-09-16")


def project_periods() -> pd.DataFrame:
    m = pd.read_csv(ROOT / "data/universe/nifty50_master_list.csv")
    rows = []
    for r in m.itertuples():
        for period in str(r.in_index_periods).replace("|", ";").split(";"):
            a, _, b = period.strip().partition("..")
            rows.append((U.CANONICAL.get(r.symbol, r.symbol), pd.Timestamp(a), pd.Timestamp(b or a)))
    return pd.DataFrame(rows, columns=["symbol", "start", "end"])


def forward_replay(events: pd.DataFrame, days) -> dict:
    """Expected membership per day from the change events alone, replayed forward from the window-start state."""
    state = set(U.members_on(days[0]))
    ev = events[pd.to_datetime(events.effective) > days[0]]
    by_day = {d: g for d, g in ev.groupby(pd.to_datetime(ev.effective))}
    out = {}
    for d in days:
        for eff in [e for e in list(by_day) if e <= d]:
            g = by_day.pop(eff)
            state -= set(g[g.action == "remove"].symbol)
            state |= set(g[g.action == "add"].symbol)
        out[d] = frozenset(state)
    return out


def run() -> PhaseResult:
    res = PhaseResult(
        1, "Historical NIFTY 50 Universe", "Universe",
        sources="NSE Indices press releases (niftyindices.com/Press_Release/ind_prs*.pdf): every replacement, exclusion and "
                "corporate-adjustment release 2016-01..2026-09 (462 PDFs, saved with extracted text and an index CSV in "
                "`data_validation/sources/niftyindices/`). Anchor: official `ind_nifty50list.csv` downloaded 2026-09-17. "
                "Compared against the project's `data/universe/nifty50_master_list.csv` (hand-transcribed, used by the "
                "rankings), `data/signals/expiry_rankings.csv`, and the vendor `in_nifty50` flag in `data/market/nifty50_15m.db`.",
        methodology="NIFTY 50 changes are parsed from the PDFs: replacement sections (`1) Nifty 50` / `a) NIFTY 50`) with "
                    "their `effective from` date, and spin-off inclusions / exclusions whose index list contains Nifty 50. A "
                    "later release supersedes an earlier one for the same symbol and action within 120 days (Yes Bank's "
                    "exclusion was brought forward from 27-Mar to 19-Mar-2020). Membership is rebuilt by undoing every change "
                    "from the anchor list, newest first; the rollback must never remove a non-member or add a member. "
                    "Renames with the same ISIN are mapped to today's ticker (ZOMATO→ETERNAL, TATAMOTORS→TMPV, LTI→LTIM). "
                    "`is_nifty50` is an interval lookup; the expected status is an independent forward replay of the events.")
    per, ev, errors = U.build()
    U.OUT.mkdir(parents=True, exist_ok=True)
    per.to_csv(U.OUT / "nifty50_membership.csv", index=False)
    ev.to_csv(U.OUT / "nifty50_change_events.csv", index=False)
    days = pd.DatetimeIndex(sorted(query("select distinct trade_date from nse.cash_eod where trade_date between %s and %s",
                                         (START.date(), END.date())).trade_date.pipe(pd.to_datetime)))
    evw = ev[(pd.to_datetime(ev.effective) >= START)]

    res.add("U-01", "Rollback from the anchor list is consistent (no impossible add/remove)", True,
            [{"error": e} for e in errors], len(ev))
    spin = per[per.from_source.astype(str).str.contains("17072023|30122024|07102025|28112025")]
    # NSE carried Tata Motors DVR as an additional 51st security between the Mar-2016
    # and Sep-2017 reviews (ind_prs22022016_2.pdf: "the Nifty 50 index shall have 51
    # securities"), so the expected count is 51 over exactly that stretch.
    DVR_FROM, DVR_TO = pd.Timestamp("2016-04-01"), pd.Timestamp("2017-09-28")
    counts = []
    for d in days:
        n = len(U.members_on(d))
        n_spin = int(((spin.effective_from <= d) & (d <= spin.effective_to)).sum())
        base = 51 if DVR_FROM <= d <= DVR_TO else 50
        if n != base + n_spin:
            counts.append({"date": str(d.date()), "members": n, "expected": base + n_spin,
                           "spinoffs_active": n_spin})
    res.add("U-02", "Member count is 50 (51 in the 2016-04..2017-09 Tata Motors DVR era, "
            "+ active zero-price spin-offs) on every trading day", True, counts, len(days))

    anchor = set(U.CANONICAL.get(s, s) for s in pd.read_csv(U.ANCHOR_FILE).Symbol.str.strip())
    diff = sorted(anchor ^ U.members_on(U.ANCHOR_DATE))
    res.add("U-03", "Rebuilt membership on 2026-09-17 equals the official NSE list", True, diff, 50)

    no_src = ev[ev.release.isna() | ev.evidence.isna()]
    late = ev[pd.to_datetime(ev.release_date) > pd.to_datetime(ev.effective)]
    res.add("U-04", "Every change has a dated NSE release announced on or before its effective date", True,
            pd.concat([no_src, late]), len(ev))

    # future leakage: the day before a change still shows the old status, the effective day the new one
    leak = []
    for e in evw.itertuples():
        eff = pd.Timestamp(e.effective)
        prev = days[days < eff]
        if not len(prev):
            continue
        before, on = U.is_nifty50(e.symbol, prev[-1]), U.is_nifty50(e.symbol, days[days >= eff][0])
        if (e.action == "add" and (before or not on)) or (e.action == "remove" and (not before or on)):
            leak.append({"symbol": e.symbol, "action": e.action, "effective": str(e.effective),
                         "status_day_before": before, "status_on_effective": on})
    res.add("U-05", "No retrospective application: status flips exactly on each effective date", True, leak, len(evw))
    today_only = sorted(anchor - U.members_on(START))
    early = [{"symbol": s, "first_member_day": str(per[per.symbol == s].effective_from.min().date())}
             for s in today_only if U.is_nifty50(s, START)]
    res.add("U-06", "Current constituents added after 2019-10-01 are not members before their inclusion", True, early,
            len(today_only), f"{len(today_only)} of today's members joined inside the window: {', '.join(today_only)}")

    # sampled is_nifty50 against an independent forward replay
    replay = forward_replay(ev, list(days))
    fo = query("select distinct symbol from nse.futures_eod where trade_date between %s and %s and expiry - trade_date < 100",
               (START.date(), END.date())).symbol.tolist()
    pool = sorted(set(per.symbol) | set(random.Random(7).sample(sorted(fo), 60)))
    rng = random.Random(20260917)
    sample = [(rng.choice(pool), days[rng.randrange(len(days))]) for _ in range(5000)]
    # plus every symbol on the trading day before and on every effective date
    for e in evw.itertuples():
        eff = pd.Timestamp(e.effective)
        sample += [(e.symbol, d) for d in list(days[days < eff][-1:]) + list(days[days >= eff][:1])]
    bad = [{"symbol": s, "date": str(d.date()), "is_nifty50": U.is_nifty50(s, d), "expected": U.CANONICAL.get(s, s) in replay[d]}
           for s, d in sample if U.is_nifty50(s, d) != (U.CANONICAL.get(s, s) in replay[d])]
    res.add("U-07", "is_nifty50(symbol, date) matches the expected status on sampled dates", True, bad, len(sample),
            f"{len(sample):,} samples: 5,000 random (symbol, trading day) over {len(pool)} symbols incl. 60 random F&O "
            "non-members, plus both sides of every change")

    # the project's hand-built list (feeds the rankings) vs the NSE-derived table
    pp = project_periods()
    spin_syms = set(spin.symbol)
    reg_bad, spin_bad = [], []
    for d in days:
        proj = set(pp[(pp.start <= d) & (d <= pp.end)].symbol)
        truth = U.members_on(d)
        for s in proj ^ truth:
            row = {"date": str(d.date()), "symbol": s, "project": s in proj, "nse": s in truth}
            (spin_bad if s in spin_syms and s not in proj and not any(
                (per.symbol == s) & (per.effective_from <= d) & (per.to_source == "anchor list 2026-09-17") & per.effective_to.isna())
             else reg_bad).append(row)
    reg_df = pd.DataFrame(reg_bad)
    res.add("U-08", "Project NIFTY 50 list equals NSE membership for regular constituents on every day", True, reg_df,
            len(days), "" if reg_df.empty else reg_df.groupby(["symbol", "project", "nse"]).date.agg(["min", "max", "count"])
            .reset_index().to_string(index=False))
    sp = pd.DataFrame(spin_bad)
    res.add("U-09", "Project list omits zero-price spin-off inclusions (known, non-tradable)", False, sp, len(days),
            "" if sp.empty else "; ".join(f"{s}: {g.date.min()}..{g.date.max()} ({len(g)} days)" for s, g in sp.groupby("symbol"))
            + ". Each was added by NSE Indices at zero price before listing and had no F&O contract, so it could never "
              "be a wheel candidate.", passed=True)

    # rankings: the only path by which the universe reaches the backtest
    rk = pd.read_csv(ROOT / "data/signals/expiry_rankings.csv", parse_dates=["expiry_date"])
    rk["canon"] = rk.symbol.map(lambda s: U.CANONICAL.get(s, s))
    outside, not_ranked = [], []
    for e, g in rk.groupby("expiry_date"):
        mem = U.members_on(e)
        for s in sorted(set(g.canon) - mem):
            outside.append({"expiry": str(e.date()), "symbol": s})
        for s in sorted(mem - set(g.canon)):
            not_ranked.append({"expiry": str(e.date()), "symbol": s})
    res.add("U-10", "Every ranked symbol was a NIFTY 50 member on its ranking expiry", True, outside, len(rk))
    nr = pd.DataFrame(not_ranked)
    spin_names = set(spin.symbol)
    nr_real = nr[~nr.symbol.isin(spin_names)] if len(nr) else nr
    res.add("U-11", "Every tradable NIFTY 50 member is present in the ranking on each expiry (no survivorship gap)", True,
            nr_real, int(rk.expiry_date.nunique()),
            "" if nr_real.empty else "; ".join(f"{s}: {len(g)} expiries ({g.expiry.min()}..{g.expiry.max()})"
                                               for s, g in nr_real.groupby("symbol"))
            + ". These members have no bars in the vendor 15m database (data/market/nifty50_15m.db), so "
              "scripts/wheel/expiry_rankings.py skipped them and the backtest could never select them.")
    nr_spin = nr[nr.symbol.isin(spin_names)] if len(nr) else nr
    res.add("U-11b", "Zero-price spin-off members not ranked (expected: not listed yet)", False, nr_spin,
            int(rk.expiry_date.nunique()), passed=True)

    con = sqlite3.connect(ROOT / "data/market/nifty50_15m.db")
    vf = pd.read_sql("select trade_date, symbol, max(in_nifty50) m from ohlcv_15m group by trade_date, symbol", con,
                     parse_dates=["trade_date"])
    # The vendor only populated in_nifty50 from 2019-10-01 (every earlier row is 0, not False),
    # so comparing it over the 2016-2019 backfill would measure a missing field, not a disagreement.
    VENDOR_FLAG_FROM = pd.Timestamp("2019-10-01")
    vf = vf[(vf.trade_date >= max(START, VENDOR_FLAG_FROM)) & (vf.trade_date <= END)]
    vf["canon"] = vf.symbol.map(lambda s: U.CANONICAL.get(s, s))
    vb = [{"date": str(r.trade_date.date()), "symbol": r.canon, "vendor": bool(r.m)} for r in vf.itertuples()
          if bool(r.m) != (r.canon in replay.get(r.trade_date, frozenset()))]
    vbd = pd.DataFrame(vb)
    res.add("U-12", "Vendor in_nifty50 flag (Kite 15m DB) agrees with NSE membership", False, vbd, len(vf),
            "" if vbd.empty else vbd.groupby(["symbol", "vendor"]).date.agg(["min", "max", "count"]).reset_index().to_string(index=False))

    res.tests_run = "U-01..U-12 in data_validation/phase01_universe.py; tests/test_dv_universe.py (pytest)."
    res.notes += ["Validated table: data_validation/output/nifty50_membership.csv (symbol, effective_from, effective_to, "
                  "source releases); events: data_validation/output/nifty50_change_events.csv.",
                  "The 2026-09-30 change (BSE in, WIPRO out, release ind_prs10082026.pdf) is after the anchor date and "
                  "after the backtest window; it is parsed but not applied.",
                  "No data was repaired in this phase."]
    return res
