#!/usr/bin/env python3
"""Lot size per (day, symbol, expiry) from the F&O bhavcopy cache, lot-change
events, and what each change does to an open wheel position.

Outputs (data/lots/):
    lot_daily.csv.gz        one row per (trade_date, symbol, expiry)
    contract_lots.csv       one row per (symbol, expiry): lot, first/last day
    lot_changes.csv         every change: corporate action / revision (live or new series)
    wheel_lot_impact.csv    position impact for the wheel universe

Lot resolution (per day, per series):
  * UDiFF (2024-07-08 on): NewBrdLotQty, the exchange's own figure.
  * legacy: estimate = VAL_INLAKH * 1e5 / (CONTRACTS * CLOSE) on the stock
    future. VAL_INLAKH is priced at traded prices, not the close, so the
    estimate is typically 0.1-1% off (ACC 2020-06-01: 403 for a 400 lot) and
    "round to the nearest integer" is wrong. Open interest is denominated in
    shares, so the true lot divides the GCD of the series' OI that day; we take
    the divisor of that GCD nearest the estimate, within LOT_BAND.
  * no futures trade that day: carry the same series' previous lot (flagged).
The legacy method is validated on UDiFF days against NewBrdLotQty.

Usage:
    python scripts/wheel/lot_sizes.py                  # scan cache
    python scripts/wheel/lot_sizes.py --fetch          # top up the cache to today first
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta
from functools import reduce
from math import gcd
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

import sys
ROOT = Path(__file__).resolve().parents[2]   # project root (scripts/<group>/<file>.py)
sys.path.insert(0, str(ROOT))                # so `from nse import ...` works from anywhere

from nse import bhavcopy as bc

RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "lots"
LOT_BAND = 0.05          # divisor must lie within ±5% of the value estimate
STOCK_FUT = {"FUTSTK", "STF"}
STOCK_FO = {"FUTSTK", "OPTSTK", "STF", "STO"}


# ------------------------------------------------------------------- scanning

def _gcd(vals) -> int:
    v = [int(x) for x in vals if x == x and x > 0]
    return reduce(gcd, v) if v else 0


def scan_file(path: Path) -> pd.DataFrame:
    d = date.fromisoformat(path.name.split(".")[0])
    df = bc.read_zip(path.read_bytes())
    if "TckrSymb" in df.columns:
        instr = df["FinInstrmTp"].astype(str).str.strip()
        exp_col = "FininstrmActlXpryDt" if "FininstrmActlXpryDt" in df else "XpryDt"
        base = pd.DataFrame({
            "symbol": df["TckrSymb"].astype(str).str.strip(),
            "expiry": pd.to_datetime(df[exp_col], errors="coerce"),
            "is_fut": instr.isin(STOCK_FUT),
            "oi": pd.to_numeric(df["OpnIntrst"], errors="coerce"),
            "contracts": pd.to_numeric(df["TtlTradgVol"], errors="coerce"),
            "value": pd.to_numeric(df["TtlTrfVal"], errors="coerce"),
            "close": pd.to_numeric(df["ClsPric"], errors="coerce"),
            "declared": pd.to_numeric(df["NewBrdLotQty"], errors="coerce"),
        })[instr.isin(STOCK_FO)]
    else:
        instr = df["INSTRUMENT"].astype(str).str.strip()
        base = pd.DataFrame({
            "symbol": df["SYMBOL"].astype(str).str.strip(),
            "expiry": pd.to_datetime(df["EXPIRY_DT"], format="%d-%b-%Y", errors="coerce"),
            "is_fut": instr.isin(STOCK_FUT),
            "oi": pd.to_numeric(df["OPEN_INT"], errors="coerce"),
            "contracts": pd.to_numeric(df["CONTRACTS"], errors="coerce"),
            "value": pd.to_numeric(df["VAL_INLAKH"], errors="coerce") * 1e5,
            "close": pd.to_numeric(df["CLOSE"], errors="coerce"),
            "declared": np.nan,
        })[instr.isin(STOCK_FO)]
    base = base[base["expiry"].notna()]
    if base.empty:
        return pd.DataFrame()

    fut = base[base["is_fut"]].groupby(["symbol", "expiry"]).agg(
        fut_contracts=("contracts", "sum"), fut_value=("value", "sum"),
        fut_close=("close", "first"))
    allr = base.groupby(["symbol", "expiry"]).agg(
        oi_gcd=("oi", _gcd), n_rows=("oi", "size"),
        declared_lot=("declared", lambda s: s.mode().iloc[0] if s.notna().any() else np.nan))
    out = allr.join(fut, how="left").reset_index()
    out.insert(0, "trade_date", pd.Timestamp(d))
    return out


def fetch_missing(start: date, end: date) -> None:
    have = {f.name.split(".")[0] for f in (RAW / "fo").glob("*.csv.zip")}
    todo = [d for d in bc.weekday_range(start, end) if f"{d}" not in have]
    if not todo:
        return
    print(f"fetching {len(todo)} F&O bhavcopies {todo[0]} -> {todo[-1]}")
    s = bc.new_session()
    for d in todo:
        try:
            bc.fetch(s, "fo", d, RAW)
        except bc.NoFileForDate:
            pass
        except RuntimeError as exc:
            print(f"  {exc}")


# ----------------------------------------------------------------- resolution

def _divisors(n: int) -> list[int]:
    small = [i for i in range(1, int(n ** 0.5) + 1) if n % i == 0]
    return small + [n // i for i in reversed(small)]


def value_lot(est: float, oi_gcd: int) -> float:
    """Divisor of oi_gcd nearest the value estimate, if within LOT_BAND."""
    if not est or est != est or est <= 0:
        return np.nan
    if oi_gcd <= 0:
        return np.nan
    best = min(_divisors(oi_gcd), key=lambda x: abs(x - est))
    return float(best) if abs(best - est) / est <= LOT_BAND else np.nan


def _confirm_changes(d: pd.DataFrame) -> pd.DataFrame:
    """Reject a derived mid-series lot change unless there is hard evidence:
    the old lot no longer divides the series' OI, or another series of the
    same symbol shows the new lot that day. Declared lots are always trusted.
    Rejected days keep the series' previous lot (source 'carried')."""
    # (date, symbol, lot) -> number of series showing that lot that day
    shown = d.dropna(subset=["lot"]).groupby(["trade_date", "symbol", "lot"]).size().to_dict()
    lots, srcs = d["lot"].to_numpy(copy=True), d["lot_source"].to_numpy(copy=True)
    days, syms, ois = d["trade_date"].to_numpy(), d["symbol"].to_numpy(), d["oi_gcd"].to_numpy()
    for _, idx in d.groupby(["symbol", "expiry"]).indices.items():
        prev = np.nan
        for i in idx:
            cur = lots[i]
            if cur != cur:
                continue
            if prev == prev and cur != prev and srcs[i] != "NewBrdLotQty":
                oi = int(ois[i])
                old_dead = oi > 0 and oi % int(prev) != 0
                others = shown.get((days[i], syms[i], cur), 0) > 1
                if not (old_dead or others):
                    lots[i], srcs[i] = prev, "carried"
                    continue
            prev = lots[i]
    d["lot"], d["lot_source"] = lots, srcs
    return d


def _drop_isolated_runs(d: pd.DataFrame) -> pd.DataFrame:
    """A derived lot run inside one series that is later replaced, and that no
    other series of the symbol showed while it lasted, is estimation noise on a
    thin series (INFY Jan-2022 listing at 306 for a 300 lot). Overwrite it with
    the lot that replaced it."""
    shown = d.dropna(subset=["lot"]).groupby(["trade_date", "symbol", "lot"]).size().to_dict()
    lots, srcs = d["lot"].to_numpy(copy=True), d["lot_source"].to_numpy(copy=True)
    days, syms = d["trade_date"].to_numpy(), d["symbol"].to_numpy()
    for _, idx in d.groupby(["symbol", "expiry"]).indices.items():
        runs, start = [], 0
        for j in range(1, len(idx) + 1):
            if j == len(idx) or lots[idx[j]] != lots[idx[start]]:
                runs.append(idx[start:j]); start = j
        for r_i in range(len(runs) - 2, -1, -1):
            run, nxt = runs[r_i], runs[r_i + 1]
            lot = lots[run[0]]
            if lot != lot or any(srcs[i] == "NewBrdLotQty" for i in run):
                continue
            if all(shown.get((days[i], syms[i], lot), 0) <= 1 for i in run):
                lots[run] = lots[nxt[0]]
                srcs[run] = "corrected"
    d["lot"], d["lot_source"] = lots, srcs
    return d


def resolve(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.sort_values(["symbol", "expiry", "trade_date"]).reset_index(drop=True)
    has = (d["fut_contracts"] > 0) & (d["fut_close"] > 0)
    d["value_estimate"] = np.where(has, d["fut_value"] / (d["fut_contracts"] * d["fut_close"]), np.nan)
    d["lot_from_value"] = [value_lot(e, int(g)) for e, g in zip(d["value_estimate"], d["oi_gcd"])]
    d["lot"] = d["declared_lot"].fillna(d["lot_from_value"])
    d["lot_source"] = np.select(
        [d["declared_lot"].notna(), d["lot_from_value"].notna()],
        ["NewBrdLotQty", "futures_value+oi_gcd"], "carried")
    d = _confirm_changes(d)
    d["lot"] = d.groupby(["symbol", "expiry"])["lot"].ffill()
    # a series with no futures trade before its first resolved day: back-fill,
    # but only when OI on those days is consistent with that lot
    bf = d.groupby(["symbol", "expiry"])["lot"].bfill()
    ok = d["lot"].isna() & bf.notna() & ((d["oi_gcd"] == 0) | (d["oi_gcd"] % bf.fillna(1) == 0))
    d.loc[ok, "lot"] = bf[ok]
    d.loc[ok, "lot_source"] = "backfilled"
    d.loc[d["lot"].isna(), "lot_source"] = "unresolved"
    d = _drop_isolated_runs(d)
    d["lot"] = d["lot"].astype("Int64")
    return d


# ------------------------------------------------------------------- analysis

def series_table(d: pd.DataFrame) -> pd.DataFrame:
    g = d.groupby(["symbol", "expiry"])
    out = g.agg(first_seen=("trade_date", "min"), last_seen=("trade_date", "max"),
                lot_at_listing=("lot", "first"), lot_at_expiry=("lot", "last"),
                distinct_lots=("lot", lambda s: s.dropna().nunique()),
                days=("trade_date", "size"),
                unresolved_days=("lot_source", lambda s: (s == "unresolved").sum())).reset_index()
    out["mid_contract_change"] = out["distinct_lots"] > 1
    return out


def change_events(d: pd.DataFrame) -> pd.DataFrame:
    """One row per (symbol, day) on which any series' lot differs from the
    symbol's prevailing lot.

    kind:
      corporate_action      live series adjusted and the price moved by the
                            inverse ratio (split/bonus), or a small odd ratio
                            off a revision day (rights, special dividend):
                            strike / ratio, lot x ratio, contracts unchanged
      revision_live_series  live series re-lotted on a post-expiry revision
                            day with no price jump: strike unchanged,
                            quantity preserved, contracts x (old/new)
      revision_new_series   only newly listed series carry the new lot;
                            existing contracts keep the old lot to expiry
    """
    r = d.dropna(subset=["lot"]).sort_values(["symbol", "trade_date", "expiry"])
    # first trading day after each stock monthly expiry = NSE revision day
    all_days = np.sort(d["trade_date"].unique())
    expiries = np.sort(d["expiry"].unique())
    rev_days = {all_days[i] for i in np.searchsorted(all_days, expiries, side="right")
                if i < len(all_days)}

    ev = []
    for sym, g in r.groupby("symbol", sort=False):
        by_day = {day: dict(zip(x["expiry"], x["lot"])) for day, x in g.groupby("trade_date")}
        close = g.sort_values("expiry").groupby("trade_date")["fut_close"].first()
        prev_day, prev = None, {}
        for day in sorted(by_day):
            cur = by_day[day]
            live = {e: l for e, l in cur.items() if e in prev}
            # a series listed beyond every live expiry; earlier-dated "new"
            # series are restamped expiries (holiday shift), not listings
            new = {e: l for e, l in cur.items() if e not in prev and (not prev or e > max(prev))}
            changed_live = {e: (prev[e], l) for e, l in live.items() if prev[e] != l}
            prevailing = prev[max(prev)] if prev else None
            changed_new = {e: l for e, l in new.items() if prevailing is not None and l != prevailing}
            if changed_live:
                old, nl = next(iter(changed_live.values()))
                ratio = nl / old
                px = (close.get(day) / close.get(prev_day)) if prev_day is not None else np.nan
                split_like = px == px and abs(px * ratio - 1) < 0.10 and abs(ratio - 1) > 0.10
                small_odd = abs(ratio - 1) <= 0.10 and day not in rev_days
                kind = "corporate_action" if (split_like or small_odd) else "revision_live_series"
                ev.append(dict(symbol=sym, effective_date=day, kind=kind, old_lot=int(old),
                               new_lot=int(nl), ratio=round(ratio, 6),
                               price_ratio=round(px, 4) if px == px else np.nan,
                               series_affected=", ".join(pd.Timestamp(e).strftime("%Y-%m-%d")
                                                         for e in sorted(changed_live)),
                               series_unchanged=", ".join(pd.Timestamp(e).strftime("%Y-%m-%d")
                                                          for e in sorted(live) if e not in changed_live),
                               on_revision_day=day in rev_days))
            elif changed_new:
                e, nl = sorted(changed_new.items())[0]
                ev.append(dict(symbol=sym, effective_date=day, kind="revision_new_series",
                               old_lot=int(prevailing), new_lot=int(nl),
                               ratio=round(nl / prevailing, 6), price_ratio=np.nan,
                               series_affected=pd.Timestamp(e).strftime("%Y-%m-%d"),
                               series_unchanged=", ".join(pd.Timestamp(x).strftime("%Y-%m-%d")
                                                          for x in sorted(live)),
                               on_revision_day=day in rev_days))
            prev_day, prev = day, cur
    return pd.DataFrame(ev)


def wheel_impact(events: pd.DataFrame, d: pd.DataFrame, names: list[str],
                 p: dict) -> pd.DataFrame:
    """What each change does to a wheel book carried through it.

    Illustrative sizing: capital / n_names per name, put strike = spot*(1-otm),
    spot proxied by the near-month future close the day before the change.
    Three positions are evaluated: an open cash-secured put, assigned stock
    held in the cash segment, and an open covered call on that stock.
    """
    per_name = p["initial_capital"] / p["n_positions"]
    otm = p["put_strike_filter_pct"]
    one_lot_mult = 1.5  # allow 1 lot iff K × lot <= 1.5 × target
    rows = []
    for e in events[events["symbol"].isin(names)].itertuples():
        px = d[(d["symbol"] == e.symbol) & (d["trade_date"] < e.effective_date)
               & d["fut_close"].notna()].sort_values(["trade_date", "expiry"])
        if px.empty:
            continue
        spot = float(px.groupby("trade_date")["fut_close"].first().iloc[-1])
        k = spot * (1 - otm)

        def size(lot, strike):
            n = int(per_name // (strike * lot))
            return 1 if n == 0 and strike * lot <= one_lot_mult * per_name else n

        n0 = size(e.old_lot, k)
        qty0 = n0 * e.old_lot
        row = dict(symbol=e.symbol, effective_date=pd.Timestamp(e.effective_date).date(),
                   kind=e.kind, old_lot=e.old_lot, new_lot=e.new_lot, ratio=e.ratio,
                   spot_before=round(spot, 2), put_strike_before=round(k, 2),
                   put_contracts_before=n0, put_qty_before=qty0,
                   cash_reserve_before=round(k * qty0))
        if e.kind == "corporate_action":
            k1 = k / e.ratio
            row.update(put_strike_after=round(k1, 2), put_contracts_after=n0,
                       put_qty_after=round(qty0 * e.ratio), cash_reserve_after=round(k1 * qty0 * e.ratio),
                       stock_qty_after=round(qty0 * e.ratio), call_contracts_after=n0,
                       uncovered_shares_after=0,
                       next_cycle_contracts=size(e.new_lot, k1),
                       effect=("Exchange adjusts open contracts: strike / ratio, lot x ratio, same number of "
                               "contracts. Cash shares scale by the same ratio, so the put reserve, stock "
                               "value and call cover are unchanged. Rebase economic basis / ratio."))
        elif e.kind == "revision_live_series":
            n1 = qty0 / e.new_lot
            row.update(put_strike_after=round(k, 2), put_contracts_after=round(n1, 4),
                       put_qty_after=qty0, cash_reserve_after=round(k * qty0),
                       stock_qty_after=qty0, call_contracts_after=round(n1, 4),
                       uncovered_shares_after=int(qty0 - (qty0 // e.new_lot) * e.new_lot),
                       next_cycle_contracts=size(e.new_lot, k),
                       effect=("Live contracts re-lotted: strike and quantity unchanged, contract count x "
                               f"{e.old_lot / e.new_lot:g}. Reserve and stock untouched. "
                               + ("Fractional contracts: exchange handles the odd lot; "
                                  "flag for audit." if qty0 % e.new_lot else "")))
        else:
            cover = qty0 // e.new_lot
            nxt = size(e.new_lot, k)
            row.update(put_strike_after=round(k, 2), put_contracts_after=n0, put_qty_after=qty0,
                       cash_reserve_after=round(k * qty0), stock_qty_after=qty0,
                       call_contracts_after=int(cover),
                       uncovered_shares_after=int(qty0 - cover * e.new_lot),
                       next_cycle_contracts=nxt, next_cycle_qty=nxt * e.new_lot,
                       next_cycle_reserve=round(k * nxt * e.new_lot),
                       effect=("Open contracts keep the old lot to expiry; nothing changes in the book. "
                               "If the put is assigned, the shares (old-lot multiple) are covered in the "
                               f"new series with floor(qty/{e.new_lot}) calls; the remainder stays "
                               "uncovered. The next put is sized on the new lot."))
        rows.append(row)
    return pd.DataFrame(rows)



# ---------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true", help="download missing F&O days first")
    ap.add_argument("--start", default="2019-10-01")
    ap.add_argument("--end", default=str(date.today()))
    ap.add_argument("--params", default=str(ROOT / "params.yaml"))
    a = ap.parse_args()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    params = yaml.safe_load(Path(a.params).read_text())

    if a.fetch:
        last = max(date.fromisoformat(f.name.split(".")[0]) for f in (RAW / "fo").glob("*.csv.zip"))
        fetch_missing(max(start, last + timedelta(days=1)), end)

    files = sorted(f for f in (RAW / "fo").glob("*.csv.zip")
                   if start <= date.fromisoformat(f.name.split(".")[0]) <= end)
    print(f"scanning {len(files)} files")
    with ProcessPoolExecutor() as ex:
        parts = [p for p in ex.map(scan_file, files, chunksize=16) if not p.empty]
    d = resolve(pd.concat(parts, ignore_index=True))

    OUT.mkdir(parents=True, exist_ok=True)
    cols = ["trade_date", "symbol", "expiry", "lot", "lot_source", "declared_lot",
            "lot_from_value", "value_estimate", "oi_gcd", "fut_contracts", "fut_close", "n_rows"]
    d[cols].to_csv(OUT / "lot_daily.csv.gz", index=False, date_format="%Y-%m-%d")

    series = series_table(d)
    series.to_csv(OUT / "contract_lots.csv", index=False, date_format="%Y-%m-%d")
    events = change_events(d)
    events.to_csv(OUT / "lot_changes.csv", index=False, date_format="%Y-%m-%d")

    names = sorted(events["symbol"].unique())
    impact = wheel_impact(events, d, names, params)
    impact.to_csv(OUT / "wheel_lot_impact.csv", index=False)

    # ---- report
    v = d[d["declared_lot"].notna() & d["lot_from_value"].notna()]
    print(f"\n{len(d):,} daily series rows, {d['symbol'].nunique()} symbols, "
          f"{d['trade_date'].min():%Y-%m-%d} -> {d['trade_date'].max():%Y-%m-%d}")
    print(d["lot_source"].value_counts().to_string())
    if len(v):
        print(f"validation on UDiFF days: legacy method == NewBrdLotQty on "
              f"{(v['lot_from_value'] == v['declared_lot']).mean():.2%} of {len(v):,} rows")
        naive = v["value_estimate"].round()
        print(f"  (naive round of the value estimate agrees on only {(naive == v['declared_lot']).mean():.2%})")
    print(f"{len(series):,} contract series; {int(series['mid_contract_change'].sum())} changed lot mid-life")
    print(f"{len(events):,} lot-change events:\n{events['kind'].value_counts().to_string()}")
    print(f"\nwheel universe events -> {OUT / 'wheel_lot_impact.csv'}")


if __name__ == "__main__":
    main()
