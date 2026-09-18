# Part B — A synthetic wheel on NIFTY and BANKNIFTY

Companion note to the Part A report (`final_results/ANALYSIS.md`). Design only: no backtest of this
design was run, and nothing below is fitted to a result.

## 1. Why the textbook wheel does not run on index options

The stock wheel earns its return from two distinct sources: option premium, and the fact that an
assigned short put leaves you holding a real asset that pays dividends and can be re-sold through a
covered call. NIFTY and BANKNIFTY options are European-style and **cash settled against the final
settlement price (FSP)**. An ITM short put at expiry pays `(K − FSP) × lot` in cash and then ceases to
exist. There is no share, so there is nothing to write a call on, no dividend accrual, and no
"hold until it recovers" leg. Steps 8–10 of the rulebook (delivery, usable shares, covered call,
call-away) have no counterpart. What survives unchanged is Step 4–7: sell an OTM put, collect premium,
hold to expiry.

The wheel's economic shape must therefore be rebuilt out of instruments that can hold a position.

## 2. The replicating cycle

The replacement for "own 1 lot of the stock" is **long 1 lot of the index future** on the same expiry
cycle. Futures are the only exchange-traded index instrument that carries a linear long exposure at
low cost and in the same lot-size units as the options.

| Stock wheel | Synthetic index wheel |
|---|---|
| Sell OTM cash-secured put, strike `K_p ≤ S × (1 − 4%)` | Same: sell OTM index put, same OTM filter, monthly expiry |
| Put expires OTM → keep premium, repeat | Same |
| Put expires ITM → take delivery of `lot` shares at `K_p` | Put cash-settles for `(K_p − FSP) × lot`; **simultaneously buy 1 lot of the next-month future** at the expiry-day close. Net position ≈ long index entered at `K_p`, exactly as delivery would have been |
| Hold shares, collect dividends | Hold the future, roll it each expiry. No dividends — they are already embedded in the futures basis |
| Sell OTM covered call, `K_c ≥ S × (1 + 3%)` | Sell OTM index call against the long future (a synthetic covered call). Filters, `call_fill_lag_days`, and the economic-basis call floor `K_c ≥ effective entry` carry over unchanged |
| Called away → sell shares at `K_c`, cycle ends | Call settles ITM → close the future at the same expiry-day close. The call's `(FSP − K_c)` outflow plus the future's `(FSP − entry)` gain leaves `K_c − entry`, the called-away P&L |
| 15% stop-loss on the short put | Same rule, on the index close |

Two implementation notes. (a) The long-future leg must be opened on the **expiry close itself**, not
the next day, because that is the price the cash settlement is struck at; any lag is uncompensated
gap risk. (b) The future and the short call must sit on the same expiry so the pair closes cleanly;
the leg that is rolled is the future, not the call.

An alternative to the future is a **synthetic long** (long ATM call + short ATM put, same strike and
expiry), which removes the need for a separate futures margin line but doubles the option legs and
their bid–ask. We prefer the future: one leg, tighter spread, and a clean roll.

## 3. Basis risk

The replication is not exact, and the gaps are worth naming.

- **Futures basis.** The future trades at `S × e^{(r − q)τ}` plus a supply/demand term. Entering the
  long leg at the expiry close means entering at near-zero basis for the *expiring* contract but at a
  full month of carry for the *next* one, so the synthetic holder pays roughly `(r − q) × τ` per month
  that the stock holder does not. On NIFTY the dividend yield (~1.2%) is well below the repo rate
  (~6.5%), so carry is a persistent cost of roughly 40–45 bps a month, not a wash. This is the single
  largest structural drag versus the stock version, and it is the reason the dividend line in the
  Part A P&L decomposition (₹2.0m at L3) has no analogue here — it is netted into the basis and it
  comes out negative.
- **Basis volatility.** The basis is not constant. It compresses in stress (Feb–Mar 2020 saw NIFTY
  futures trade at a discount) and widens into events. Marking the synthetic position daily gives a
  noisier NAV than marking shares, even with identical index exposure.
- **Roll gap.** The monthly roll happens at one price stamp. A stock position has no roll at all.
- **Settlement mismatch.** FSP for index options and futures is the same value, so the put/future pair
  is exact on the assignment day; the residual risk is entirely in the subsequent roll.
- **BANKNIFTY specifically** carries an additional sector-concentration risk that the Part A portfolio
  diversified away across 20 names: it is a 12-stock, single-sector index.

## 4. Roll costs

Each held cycle now pays an extra round trip per month that the stock wheel does not:

| Leg | Stock wheel | Synthetic |
|---|---|---|
| Option entry/exit | Yes | Yes (same schedule) |
| Equity delivery brokerage + STT on delivery | Yes | No |
| Futures roll (sell near, buy far) | — | Every expiry held |

At an index-future bid–ask of ~0.5–1 bp of notional plus exchange charges and STT on the sell leg
(0.02% on futures sales), a roll costs on the order of 3–5 bps of notional. A position held for the
Part A average of ~129 days post-delivery is ~4 rolls, or 12–20 bps, on top of the carry in §3. Use
`data/costs/wheel_charges_schedule.csv` extended with a futures row rather than reusing the equity
row — the tax treatment differs.

## 5. Lot-size granularity

This is the constraint that most changes the strategy's character. A NIFTY lot (75 units near
26,000 ≈ ₹19.5 lakh notional) and a BANKNIFTY lot (35 units near 58,000 ≈ ₹20 lakh) are far coarser
than a typical NSE stock lot. Consequences:

1. **Position sizing becomes lumpy.** At ₹2 crore of capital, a single index is 1 lot at 1× leverage
   and only 2–3 at L3. The `max_reserve_multiple: 1.5` one-lot exception, a minor rule in Part A,
   becomes the dominant sizing rule.
2. **No diversification.** Two underlyings replace twenty names. The per-name sleeve analysis in the
   Part A report has no counterpart, and the strategy's variance is entirely index-level.
3. **Round-trip integrity.** The put, the future and the call must all be in the same integer lot
   count, so a rule is needed for what happens when the option lot size is revised mid-cycle (NSE has
   changed NIFTY and BANKNIFTY lot sizes several times over the 2020–2026 window). The clean rule:
   size the future to the *contracted* put lot, and treat a lot-size revision as a contract-level
   event, never a resize of an open cycle. Any implementation must resolve lot sizes from the
   exchange's dated circulars, as `data/lots/lot_at_monthly_expiry.csv` already does for stocks.
4. **Capital floor.** Below roughly ₹40–50 lakh the strategy cannot run a diversified two-index book
   at all, versus a stock wheel that can spread ₹2 crore across twenty sleeves.

## 6. Margin differences

Short index options are SPAN + exposure margined, as stock options are, but the numbers differ
materially in the strategy's favour:

- **Lower volatility → lower SPAN.** Index SPAN on an OTM short put typically runs well under the
  stock-option requirement at comparable moneyness, because index vol is ~60–70% of single-stock vol.
- **Cross-margin offsets.** The long future and the short call are recognised as a covered position,
  so the pair is margined at far less than the sum of the legs. The stock wheel gets a similar benefit
  only after physical delivery, and only via early-pay-in.
- **No delivery margin.** The stock wheel's sharpest margin spike is the ITM-put delivery margin ramp
  in expiry week (Step 9). Cash settlement removes it entirely — and with it the Step 9a cash-only
  constraint that forced 17 ITM puts at L3 to be realised as losses rather than delivered.
- **Daily MTM on futures.** Unlike shares, the long leg settles in cash every day, so the strategy
  needs a working cash buffer it did not need before. `max_margin_utilization: 1.0` should be
  tightened (0.7–0.8) to absorb MTM calls without forced liquidation.

Net: the same `leverage` parameter buys materially more notional on the index book than on the stock
book, so the L1–L5 grid is **not** comparable across Parts A and B. Any comparison must be run on
matched notional or matched margin utilisation, not matched leverage.

## 7. What was not done

The design above was not backtested. Doing so needs three data sets this project does not currently
hold: NIFTY/BANKNIFTY option chains at monthly expiries over 2020-01→2026-06, the corresponding
near- and next-month futures closes, and the dated lot-size circulars for both indices. With those,
`scripts/wheel/backtest.py` would need one structural change — replacing the share inventory with a
futures position that carries an entry price and a roll schedule — after which Steps 4–7 and 11–12
apply unchanged. The honest expectation is a lower CAGR than the Part A `ranked_L3` result (18.05%):
index implied vol is lower, so premium per unit of notional is smaller, and §3's carry plus §4's roll
costs are a drag with no dividend line to offset them. The likely compensation is a better risk
profile — the −22.25% L3 drawdown is driven by single-name delivery losses that a cash-settled index
book cannot incur in the same form.
