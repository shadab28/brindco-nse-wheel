"""Corporate-action detection from the F&O data itself.

The cash prices are unadjusted, so a split, bonus, rights issue, demerger or extraordinary dividend
shows up as a price gap. NSE handles these by adjusting every open F&O contract on the ex-date:
    strike' = a × strike + b        lot' = lot / a  (a = 1 for dividends, where b = −dividend)
That leaves an unmistakable signature: the strikes carrying open interest yesterday are absent today
and today's strikes are an affine map of yesterday's.

detect() finds that signature for each (symbol, ex-date) and fits (a, b) from three candidate models:
    lot-ratio   a = lot_before / lot_after, b fitted   (splits, bonuses, rights, split + dividend)
    scale       a fitted (< 1), b = 0                  (rights / demergers without a lot change)
    shift       a = 1, b fitted (< 0)                  (extraordinary dividends)
The model mapping the most OI strikes wins. Accepted events must map >= 80% of >= 8 OI strikes and be
consistent with the observed price gap.

Classification and engine treatment:
    share_multiplier  lot_after / lot_before when it is a small-integer ratio >= 15% away from 1
                      (split/bonus): held shares × multiplier; else 1 (rights/dividend/demerger keep
                      the share count)
    option contracts  strike -> a·K + b, quantity × lot_after / lot_before
    held stock        receives the value that left the share: per old share
                      prev_close − share_multiplier × (a·prev_close + b)   (ASSUMED realised in cash)

Look-ahead: the strikes seen on the ex-date are published that morning, and the adjustment itself is
announced by circular days in advance; using the ex-date chain is information available on the day.
"""
from __future__ import annotations

from fractions import Fraction

import numpy as np
import pandas as pd

MIN_OI_STRIKES = 8
MAX_UNCHANGED = 0.5
MATCH_TOL = 0.002
MIN_MATCH = 0.8
MAX_PRICE_GAP_ERR = 0.08


def _match_index(pre: np.ndarray, post: np.ndarray, a: float, b: float) -> tuple[np.ndarray, np.ndarray]:
    """For each pre strike: index of the nearest post strike to a·k + b, and whether it is within tolerance."""
    target = a * pre + b
    j = np.argmin(np.abs(target[:, None] - post[None, :]), axis=1)
    ok = np.abs(post[j] - target) <= MATCH_TOL * np.maximum(np.abs(target), 1e-9)
    return j, ok


def _candidates(pre, post, lot_before, lot_after):
    """(model, a, b) mappings worth scoring: every b implied by a strike pair for the fixed-scale models,
    every a implied by a strike pair for the pure-scale model."""
    ref = float(np.median(pre))
    out = []
    scales = [("shift", 1.0)]
    if lot_before and lot_after and lot_before != lot_after:
        scales.insert(0, ("lot_ratio", lot_before / lot_after))
    for name, a in scales:
        bs = np.unique(np.round((post[None, :] - a * pre[:, None]).ravel(), 2))
        bs = bs[np.abs(bs) <= 0.5 * a * ref]
        if name == "shift":
            bs = bs[bs < 0]
        out += [(name, a, float(x)) for x in bs]
    As = np.unique(np.round((post[None, :] / pre[:, None]).ravel(), 5))
    out += [("scale", float(x), 0.0) for x in As[(As > 0.05) & (As < 0.995)]]
    return out


def fit_adjustment(pre: np.ndarray, pre_oi: np.ndarray, post: np.ndarray, post_oi: np.ndarray,
                   lot_before, lot_after) -> dict:
    """Mapping strike -> a·K + b that explains today's chain.

    Strike matching alone is ambiguous: shifting by one strike step maps a regular grid onto itself.
    Open interest is carried across the adjustment (scaled by the lot ratio), so among mappings that
    match >= MIN_MATCH of the OI strikes the one whose OI profile agrees best wins.
    """
    qm = (lot_after / lot_before) if (lot_before and lot_after) else 1.0
    scored = []
    for name, a, b in _candidates(pre, post, lot_before, lot_after):
        j, ok = _match_index(pre, post, a, b)
        hit = float(ok.mean())
        if hit < MIN_MATCH:
            continue
        want = pre_oi * (qm if name == "lot_ratio" else 1.0)
        got = np.where(ok, post_oi[j], 0.0)
        mismatch = float(np.abs(want - got).sum() / max(want.sum(), 1.0))
        scored.append((mismatch, -hit, abs(b), name, a, b, hit))
    if not scored:
        return {"model": "none", "a": 1.0, "b": 0.0, "strike_match": 0.0, "oi_mismatch": np.nan}
    mismatch, _, _, name, a, b, hit = min(scored)
    j, ok = _match_index(pre, post, a, b)       # refine to the exact residual of matched strikes
    if name == "scale":
        a = float(np.median(post[j][ok] / pre[ok]))
    else:
        b = float(np.median(post[j][ok] - a * pre[ok]))
    return {"model": name, "a": a, "b": b, "strike_match": hit, "oi_mismatch": mismatch}


def share_multiplier(lot_before, lot_after) -> float:
    """New shares per old share for a split/bonus (lot ratio is a small-integer ratio >= 15% from 1)."""
    if not lot_before or not lot_after:
        return 1.0
    r = lot_after / lot_before
    frac = Fraction(r).limit_denominator(10)
    if abs(r - 1) >= 0.15 and frac.numerator <= 20 and abs(float(frac) / r - 1) <= 0.005:
        return float(frac)
    return 1.0


def detect(md) -> pd.DataFrame:
    rows = []
    days = md.trading_days
    for sym in sorted(md.close.columns):
        for d0, d1 in zip(days[:-1], days[1:]):
            best = None
            for cm in (md.contract_after(d1 - pd.Timedelta(days=1)), md.contract_after(d1 + pd.Timedelta(days=25))):
                if cm is None:
                    continue
                c0, c1 = md.chain(d0, sym, cm, "CE"), md.chain(d1, sym, cm, "CE")
                if len(c0) == 0 or len(c1) == 0:
                    continue
                post = c1.strike.to_numpy()
                c0 = c0[c0.oi_shares > 0]
                if len(c0) < MIN_OI_STRIKES:
                    continue
                # "unchanged" is judged only on strikes today's listed range could contain: a crash moves the
                # cached strike band and would otherwise look like an adjustment. A split leaves none in range.
                pre = c0.strike.to_numpy()
                in_range = pre[(pre >= post.min()) & (pre <= post.max())]
                if len(in_range) >= MIN_OI_STRIKES:
                    unchanged = float(np.mean(np.isin(np.round(in_range, 4), np.round(post, 4))))
                elif md.lot_size(d0, sym, cm) != md.lot_size(d1, sym, cm):
                    unchanged = 0.0          # strikes moved out of range together with the lot: split/bonus
                else:
                    continue                 # too little overlap to judge (e.g. crash day) and no lot change
                if unchanged >= MAX_UNCHANGED:
                    best = None
                    break
                if best is None or len(pre) > best[4]:
                    best = (cm, c0, c1, unchanged, len(pre))
            if best is None:
                continue
            cm, c0, c1, unchanged, n = best
            l0, l1 = md.lot_size(d0, sym, cm), md.lot_size(d1, sym, cm)
            fit = fit_adjustment(c0.strike.to_numpy(), c0.oi_shares.to_numpy(float), c1.strike.to_numpy(),
                                 c1.oi_shares.to_numpy(float), l0, l1)
            s0, s1 = md.spot(d0, sym), md.spot(d1, sym)
            price_ratio = s1 / s0 if s0 and s1 else np.nan
            implied_ratio = (fit["a"] * s0 + fit["b"]) / s0 if s0 else np.nan
            mult = share_multiplier(l0, l1)
            rows.append({"symbol": sym, "ex_date": d1, "contract_month": cm,
                         "action_type": "split_bonus" if mult != 1.0 else "value_adjustment",
                         "model": fit["model"], "a": round(fit["a"], 8), "b": round(fit["b"], 4),
                         "share_multiplier": mult, "qty_multiplier": (l1 / l0) if (l0 and l1) else 1.0,
                         "strike_match": round(fit["strike_match"], 3), "oi_mismatch": round(fit["oi_mismatch"], 3),
                         "oi_strikes": n, "unchanged_frac": round(unchanged, 3), "prev_close": s0,
                         "price_ratio": price_ratio, "implied_ratio": implied_ratio, "lot_before": l0,
                         "lot_after": l1, "source": "detected"})
    return validate(pd.DataFrame(rows))


def validate(ca: pd.DataFrame) -> pd.DataFrame:
    """Accept an event only if the fit is strong and the implied price gap matches the observed one."""
    out = ca.copy()
    if out.empty:
        out["accepted"] = []
        return out
    out["price_consistent"] = (out.price_ratio / out.implied_ratio - 1).abs() <= MAX_PRICE_GAP_ERR
    out["accepted"] = (out.strike_match >= MIN_MATCH) & out.price_consistent
    return out


def distribution_per_old_share(ev, prev_close: float) -> float:
    """Value that left one pre-event share (0 for a pure split/bonus)."""
    fixed = getattr(ev, "distribution_per_share", float("nan"))
    if fixed == fixed:          # explicit value from the overrides file
        return float(fixed)
    return max(prev_close - ev.share_multiplier * (ev.a * prev_close + ev.b), 0.0)


def load(detected: pd.DataFrame, overrides_file) -> pd.DataFrame:
    """Accepted detected events plus manual overrides (overrides win on the same symbol and date)."""
    det = detected[detected.accepted].copy()
    det["distribution_per_share"] = np.nan
    det["ex_date"] = pd.to_datetime(det.ex_date)
    ov = pd.read_csv(overrides_file, parse_dates=["ex_date"])
    ov["source"] = "override"
    key = set(zip(ov.symbol, ov.ex_date))
    det = det[[k not in key for k in zip(det.symbol, det.ex_date)]]
    return pd.concat([det, ov], ignore_index=True).sort_values(["ex_date", "symbol"]).reset_index(drop=True)
