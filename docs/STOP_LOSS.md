# Put-phase stop loss (`put_stop_loss_pct`)

**Status:** live at `0.15`. Added 2026-09-18. `null` disables it and reproduces every pre-rule run.

## The rule

A short put is bought back on the first close where the underlying trades at or below

```
stop_level = (1 − put_stop_loss_pct) × underlying close on the day the put was SOLD
```

The buyback happens at that same day's close, the cycle ends with status `stop_loss`, and **no stock is
ever delivered**. The symbol is free to be selected again at the next expiry; there is no cooldown.

The reference is the spot when the put was written, not the strike and not an assignment price. This is
a stop on the *short-put phase only*. Stock already delivered from an earlier assignment is untouched —
that would be a different rule and was not scoped.

Implementation: `WheelEngine._stop_losses()` in [`nse/wheel/engine.py`](../nse/wheel/engine.py), called from
`step()` immediately after `_expiries()`. Tests: [`tests/test_put_stop_loss.py`](../tests/test_put_stop_loss.py).

## Why 15% and not 10%

The rule was originally specified at 10%. **It loses money at 10% on a per-trade basis**, and the reason
generalises: a 10% dip in a NIFTY 50 name inside a one-month contract is usually noise, and the wheel is
designed to absorb exactly that by taking delivery and selling calls against the shares. A tight stop
converts absorbed volatility into realised losses.

Per-trade counterfactual over the 517 puts in `ranked_L3`
(`python scripts/wheel/stop_loss_probe.py counterfactual`):

| Stop | Puts stopped | Helped | Hurt | Net |
|---|---|---|---|---|
| −5% | 170 (32.9%) | 31 | 138 | −₹1.26 Cr |
| −8% | 96 (18.6%) | 25 | 71 | −₹1.08 Cr |
| −10% | 61 (11.8%) | 20 | 41 | −₹40.9 L |
| −12% | 41 (7.9%) | 16 | 25 | −₹45.3 L |
| **−15%** | **20 (3.9%)** | **11** | **9** | **+₹21.1 L** |
| −20% | 5 (1.0%) | 4 | 1 | +₹18.6 L |
| −25% | 1 (0.2%) | 0 | 1 | −₹0.1 L |

At −10%, 37 of the 61 stops fired on puts that were assigned and then **recovered and made money**. M&M in
October 2024 dipped to ₹2,720 against a ₹2,785 stop; exiting there costs ₹5.8 L, and the cycle went on to
earn +₹13.4 L. Seven more stops fired on puts that expired worthless — paying to close a position that was
about to pay in full. 15% is the first threshold where the rescues outweigh that damage.

## Why the real number is much larger than the counterfactual

The table above holds the rest of the book fixed, and that is knowably wrong in one direction. Under Step 9a
the book never borrows, so an ITM put is delivered only if free cash covers `strike × qty`; otherwise it is
cash-settled at intrinsic with **no shares received** — the worst outcome in the strategy. Stopping a losing
put early releases the margin and cash that were trapped behind it, which lets *other* wheels take delivery
properly instead of being cash-settled.

The counterfactual cannot see this. The full re-run can
(`python scripts/wheel/stop_loss_probe.py rerun`):

| L | Stop | Total P&L | CAGR | Max DD | Sharpe | Stops | Assignments | Cash-settled | vs. off |
|---|---|---|---|---|---|---|---|---|---|
| 3 | off | ₹2.71 Cr | 14.10% | −20.5% | 0.69 | 0 | 75 | 27 | — |
| 3 | −10% | ₹3.52 Cr | 16.92% | −15.5% | 1.03 | 64 | 58 | 2 | +₹81.0 L |
| 3 | −12% | ₹3.07 Cr | 15.38% | −27.2% | 0.81 | 44 | 68 | 5 | +₹35.2 L |
| **3** | **−15%** | **₹4.05 Cr** | **18.58%** | **−20.2%** | **1.03** | **20** | **74** | **16** | **+₹1.34 Cr** |
| 3 | −20% | ₹2.96 Cr | 15.01% | −22.1% | 0.72 | 4 | 77 | 25 | +₹24.8 L |

Watch the `cash_settled` column: 27 → 16 at −15%, and 27 → 2 at −10%. That is the mechanism. At −10% the
rule is *net positive in the re-run* (+₹81 L) despite being net negative per-trade (−₹40.9 L) — the entire
difference is cash it freed for other positions.

At L3 the −15% stop lifts CAGR from 14.1% to 18.6% and Sharpe from 0.69 to 1.03, with drawdown essentially
unchanged (−20.5% → −20.2%).

## Robustness

Positive at every leverage tested, and −15% is the best or near-best at each:

| L | off | −10% | −12% | −15% | −20% |
|---|---|---|---|---|---|
| 1 | — | −₹2.5 L | +₹5.6 L | **+₹16.2 L** | +₹8.5 L |
| 3 | — | +₹81.0 L | +₹35.2 L | **+₹1.34 Cr** | +₹24.8 L |
| 5 | — | +₹2.17 Cr | +₹1.27 Cr | +₹1.55 Cr | +₹74.6 L |

L1 matters most for believing the result. At 1× the cash gate never binds — `cash_settled` is 0 in every
row — so the freed-cash mechanism cannot be operating. The stop still adds ₹16.2 L and cuts max drawdown
from −7.7% to −6.3%. Some of the benefit is genuine risk reduction, not a Step 9a artifact.

## What is weak about this, stated plainly

1. **Not fixed ex ante.** Every other parameter in `params.yaml` was set before any result existed. This one
   was chosen after seeing the L1–L5 grid and after seeing which cycles lost money. It is fitted to this
   window and should be discounted accordingly.
2. **Non-monotonic in the threshold.** −12% is worse than both −10% and −15% at L1 and L3, and −10% beats
   −15% at L5. If the rule captured a stable effect the curve would be smooth. It is not. That is evidence
   of path dependence, and it means the specific value 0.15 is not well identified — anywhere in 10–20%
   is defensible, and the ranking between them is probably noise.
3. **Small samples.** 20 stops at −15% across 6 years and 5 concurrent names. Four names produce most of
   the gain (INFY, TATAMOTORS, HCLTECH, HINDUNILVR, all 2024–2026).
4. **Close-only.** The cache holds daily closes, no intraday. A stock that trades through the level and
   closes above it never triggers. A real intraday stop would fire more often, and firing more often is
   what hurts at tight thresholds — so live results would likely be worse than this.
5. **Same-day fill is optimistic.** The breach is only observable once the close prints, so filling at that
   same close assumes execution on the bell. Every other exit in the engine decides at `t` and fills at
   `t+1`. This one does not, by explicit choice — a stop that waits a day is not a stop — but it is the one
   place in the engine with this lookahead, and it is worth perhaps a few tenths of a percent.
6. **The gains concentrate in one regime.** Most of the benefit comes from the 2026 IT drawdown. A window
   without a sharp sector selloff would show much less.

## Honest summary

The direction is real and survives at three leverages and through a mechanism visible at L1 where the cash
explanation does not apply. The magnitude is not trustworthy, and the specific threshold is not well
identified. Treat `+₹1.34 Cr at L3` as the optimistic end of a wide range, not as an expectation.
