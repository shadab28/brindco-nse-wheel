# NSE Stock-Option Wheel — Step-by-Step Rulebook

Derived from the frozen baseline specification (a fixed 10-name, cash-secured wheel). The steps below are
self-contained; nothing outside this project is needed to read or run them.

**The Brindco backtest** (`nse/wheel/`, `scripts/wheel/backtest.py`) follows these steps but changes some
of them. The **Brindco implementation** table below lists every change, and `params.yaml` is the source of
truth for Brindco runs. Steps without a note apply to both.

Notation: **t** = signal day (decide on its close), **t+1** = fill day, **K** = strike,
**S** = spot (CM EQ close), **FSP** = final exercise settlement price (underlying CM close on expiry day), **lot** = lot size in shares.

Hard rules that apply to every step:

- Decide on data dated **≤ t** only. Fill on **t+1**. Never read t+1 to choose anything.
- Never fabricate a price, lot size or expiry. Missing ⇒ skip (fills) or carry-and-flag (marks).
- Never tune a parameter after seeing a backtest result. Changes need explicit approval.
- Every parameter comes from config; no numeric literals in strategy code.

## Brindco implementation — what differs from the frozen baseline

| Topic | Frozen baseline | Brindco (`params.yaml`, `nse/wheel/`) |
|---|---|---|
| Universe | Fixed 10 names from Q4-2019 liquidity (Step 1) | At each expiry, the top `n_positions` (**20**) names in `data/signals/expiry_rankings.csv` with `rank_final > rank_threshold` (**1.5**). Rankings only cover NIFTY 50 members on that date (`data/universe/nifty50_membership_validated.csv`). A name that leaves the index is wound down. |
| Allocation order | Alphabetical | By rank (`allocation_priority: rank`) |
| Capital | ₹2 crore, fully cash-secured puts | ₹2 crore with **leverage L** (`leverage: 5.0`; grid 1, 2, 3, 5). Put margin = `K × qty / L`. Target notional per name = `L × NAV / N`. |
| Margin | Tracked, never used for sizing | **Used for sizing.** Entries may not push margin above `max_margin_utilization` (1.0) × NAV. A close above that liquidates the next day. |
| Idle cash | 0% | Positive cash earns the monthly India 3M T-bill rate (`data/market/india_rf_3m_tbill_monthly.csv`). |
| Borrowed cash | 0% | **Not permitted.** Cash may never go negative; an ITM put with insufficient free cash is realised as a loss instead of delivered (Step 9a). `margin_financing_rate` is now dead for live runs — the financing leg cannot fire and is asserted to be zero — and is retained in `params.yaml` only to reproduce the archived pre-Step-9a runs. |
| Sharpe / Sortino hurdle | 0% | The same 3M T-bill rate |
| First covered call | Shares usable 2 trading days after expiry | Decided at the assignment close and **sold T+1** (`call_fill_lag_days: 1`) |
| Call vs put priority | — | `covered_call_first: true`: calls on usable shares fill before any new put, and puts wait while a call order is pending |
| Contract terminations | Lifecycle rules (Step 13) | `data/calendar/contract_terminations.csv`: HDFC and TATAMOTORS settle at the CM close, and held stock is sold there |
| Corporate actions | Scheme-sourced | Detected from F&O strike adjustments (`data/backtest/corporate_actions_detected.csv`) plus manual demerger values (`data/calendar/corporate_action_overrides.csv`) plus cash dividends from NSE announcements (`data/reference/dividends.csv`). Full method: `CORPORATE_ACTIONS.md` |
| Costs | Frozen baseline cost table | `data/costs/wheel_charges_schedule.csv` + `data/costs/slippage_schedule.csv` (see Step 11) |
| Slippage `low` | s = 1.0%, equity 2 bps | s = 1.25%, equity 5 bps at every level |
| Benchmarks | — | NIFTY 50 buy-and-hold (NIFTY 50 Total Return Index, niftyindices.com) and an equal-weight portfolio of the same top-N names |
| Sensitivity | 11-run OTM × slippage grid | Leverage 1/2/3/5, frozen 10 names at 1× and 5×, `rank_threshold` ∈ {0.5, 1, 1.5, 2} at 5×. The 5×-with-0%-financing run is **retired** — Step 9a makes borrowing impossible, so it was bit-identical to `ranked_L5` |
| Stress windows | — | Covid 2020-02-19→03-23, rate hikes 2022-01-17→06-17, FPI outflows 2024-09-26→2025-03-04, tariffs 2025-04-01→04-07 |

Unchanged from the baseline: the 5% OTM put and call filters, liquidity (≥ 10 contracts, OI ≥ 50 contracts),
the 10% participation cap, 3 retries, the 1.5% distance cap, the economic-basis call floor, the 1.5× one-lot
exception, strict FSP moneyness, and the daily ledger reconciliation.

---

## Step 0 — Fixed setup (once)

| Item | Value |
|---|---|
| Window | 2020-01-01 → 2026-06-30 |
| First signal | Close of 2019-12-31; first fill on the next trading day |
| Capital | ₹2,00,00,000, **one pooled cash account** |
| Names | 10 |
| Idle cash yield | 0% |
| Risk-free (Sharpe) / Sortino MAR | 0% / 0% |
| Annualisation | 252 |
| Slippage level | `base` |
| End of window | Mark to market, no forced liquidation; report liquidation cost separately; settle an expiry falling on the end date normally |

## Step 1 — Build the universe (ex-ante, once)

Use **only data dated 2019-10-01 → 2019-12-31**.

1. **Eligibility** — a name must:
   - have stock options on *every* Q4-2019 trading day,
   - be listed on 2019-12-31,
   - have a resolved lot size (Step 3) for its first contract.
2. **Score** each eligible name:
   `z(log option premium turnover) + z(log(OI value + 1)) + z(coverage) − z(staleness)`
   - premium turnover = mean daily Σ(close × contracts × lot) over stock-option rows
   - OI value = mean daily Σ(OI shares × spot)
   - coverage / staleness measured on strikes within ±10% of spot, near + next monthly expiry
   - z-scores cross-sectional, population std (ddof 0)
3. Take the **top 10**; ties broken by symbol ascending.

Frozen result: `IBULHSGFIN, ICICIBANK, INFY, MARUTI, RELIANCE, SBIN, TATAMOTORS, TATASTEEL, YESBANK, ZEEL`.
No replacement names are ever added.

## Step 2 — Expiry calendar

- Monthly stock-option expiries only, **read from contract data**, never computed ("last Thursday" is wrong).
- Explicit aliases (`data/reference/expiry_aliases.csv`) map a label to the actual last trading day.
- Early termination: settle on the actual last trading day at the exchange settlement price.
  If termination is announced before expiry, open no new contract from the announcement date.

## Step 3 — Lot size for (name, contract, day t)

1. **2024-01-01 onward (UDiFF):** `NewBrdLotQty` on the row.
2. **Before (legacy):** `lot = VAL_INLAKH × 1e5 / (CONTRACTS × CLOSE)` on the same-expiry stock future, that day.
3. Snap to the nearest integer **only if within ±1%**; otherwise unresolved.
4. Unresolved ⇒ use the most recent resolved day of the **same contract** on or before t, only if no
   corporate action lies between (flag `lot_fallback_days`). None ⇒ **skip**.
5. A resolved day-t lot that differs from the contract's previous lot is accepted only if another contract
   of the same name shows it on t, or t is a lot-revision circular date or corporate-action ex-date.
   Otherwise fall back as in 4.
6. No averaging, no borrowing from other contracts.

---

## Step 4 — Daily loop (run every trading day, in exactly this order)

```
1  load the day's data
2  apply corporate actions (splits, bonuses, dividends, rights, mergers, demergers, locked quantity)
3  release assigned shares for a covered call decided at the assignment close (sold T+1)
4  mark to market (stock on CM close, short options on option close)
5  process expiries (Step 9)
6  snapshot positions
7  generate decisions for tomorrow (Steps 5–8)   ← only data ≤ today
8  execute fills of orders decided yesterday      (Step 8)
9  compute NAV
10 reconcile (Step 12) — fail hard if it breaks
```

## Step 5 — Is today a decision day for this name?

Loop names **alphabetically**. For each name, skip it if it already has an open short option, has no spot,
is not F&O-listed, or has a termination announced.

Otherwise make a decision if **any** of these holds:

- today is a **monthly expiry day** for this name (or the first signal date), or
- the name holds **coverable shares** with no call written (post-assignment / re-check), or
- the name is **retrying** a failed fill (Step 8).

Then:

- **Contract** = nearest monthly expiry **strictly after** the execution date.
- **Lot** = Step 3 for that contract. Unresolved ⇒ skip (`lot_unresolved`).
- Holding coverable shares ⇒ **Step 7 (call)**. Holding no shares ⇒ **Step 6 (put)**.

## Step 6 — Cash-secured put (state: CASH → SHORT_PUT)

1. **Liquidity filter on t** — keep strikes of the chosen expiry (puts) with:
   - close present and > 0,
   - contracts traded on t **≥ 10**,
   - OI on t **≥ 50 contracts** (source OI is in shares ⇒ `OI / lot ≥ 50`).
2. **Target** `= S_t × (1 − 0.05)`.
3. **Strike** = highest eligible strike **≤ target** (tie → lower). None ⇒ skip, stay in cash.
4. **Distance cap**: reject if `|K − target| > max(1.5% × S_t, strike interval bracketing target)`.
5. **Sizing (at t)**, in this order:
   1. `target_per_name = NAV_t / 10`
   2. `lots = floor(target_per_name / (K × lot))`
   3. if `lots = 0`: allow **1 lot** iff `K × lot ≤ 1.5 × target_per_name`, else skip (`lot_too_large`)
   4. cap by cash: `lots = min(lots, floor(free_cash / (K × lot)))`; 0 ⇒ skip (`insufficient_cash`)
6. Emit a **sell PUT** order for t+1. Reserve **100% of K × lots × lot** while the put is open.

## Step 7 — Covered call (state: LONG_STOCK → STOCK + SHORT_CALL)

1. After assignment, the first call is decided at the **assignment close** and **sold T+1**
   (`call_fill_lag_days: 1`), in the **current** monthly contract, subject to all normal filters.
   (The frozen baseline instead waits until the shares are usable, 2 trading days after expiry.)
2. **Liquidity filter on t** — same as Step 6.1, for calls.
3. **Target** `= max(S_t × 1.05, economic_basis)` (Step 10).
4. **Strike** = lowest eligible strike **≥ target** (tie → higher).
5. **Distance cap** — same rule as the put.
6. None eligible ⇒ hold the stock uncovered, re-check at the next decision day.
7. **Lots** = `floor(coverable_shares / lot)`; < 1 ⇒ skip.
8. Emit a **sell CALL** order for t+1.
9. **Sized on the fill-day lot**: at the t+1 fill, `lots = min(10% × contracts traded, floor(shares_held / lot_{t+1}))`,
   so an NSE lot revision that takes effect overnight still covers every whole lot.
10. **Top-up calls**: while a call is open and `floor((shares_held − call_qty) / lot) ≥ 1` (participation cap or lot
    revision left whole lots uncovered), sell more of the **same contract and strike** the next day, provided that
    strike still meets today's target (`≥ max(S_t × 1.05, economic_basis)`), is liquid on t, and expires after t+1.
    The added lots join the open call (one position, blended premium). Top-ups do not hold back new puts.
11. **Residual shares**: shares below one lot can never be covered without writing a naked call. After a call-away
    they are sold at the next day's close (`residual_after_call_away`); before any call, an uncoverable holding is
    sold (`uncoverable_odd_lot`). A cycle ends only with 0 shares (checked; `share_audit.csv` per run).

## Step 8 — Fill on t+1

0. **Lot size is re-resolved on the fill day**: the order is sized on `lot(t+1)` (Step 3 evaluated at t+1),
   not on the lot used to size the decision at t — an NSE lot revision or a corporate action can take effect
   overnight. Unresolved on t+1 ⇒ fall back to the decision-day lot. A **top-up** into an open call is
   refused outright if the fill-day lot differs from the open option's lot (`topup_lot_mismatch`), because
   the two legs would no longer be the same contract.
1. **Participation cap**: `lots = min(lots, floor(10% × contracts traded on t+1))`.
2. **Fill fails** if any of:
   - contracts traded on t+1 = 0 (stale close),
   - option close missing (never substitute the settlement price),
   - < 1 lot after the participation cap,
   - net premium ≤ 0 after slippage.
3. **Fill price** (sell) `= P − slip`, `slip = max(k × tick, s × P)`, floored at 1 tick.
   `base`: k = 2 ticks, s = 2.5%.
   Tick: ₹0.05 until 2025-11-02. From 2025-11-03: ₹0.01 if underlying < ₹250, else ₹0.05
   (band from the underlying's close at the previous month-end).
4. Credit premium, charge option-sale costs (Step 11).
5. **On failure**: do not reselect on t+1. Re-signal at the t+1 close (data ≤ t+1), fill at t+2.
   Max **3 retries** (a retry that finds no strike still counts). Exhausted ⇒ wait for the next expiry
   (`entries_missed`).

## Step 9 — Expiry processing

- **Moneyness** is decided on the **FSP** = the final exercise settlement price applicable to the stock
  option under the NSE settlement-price specification in force on that expiry date: the **closing price of
  the underlying security in the capital-market segment** on the last trading day (`nse.cash_eod.close`).
  The expiring stock future's settle (`FUTSTK.SETTLE_PR` / `STF.SttlmPric`) is **not** the exercise price;
  it is carried as `fut_fsp` for cross-checking only (Step 15) and used as a fallback solely when the CM
  close is missing, with a P1 flag (`fsp_fallback_to_future_settle`).
- Strict: put ITM iff `FSP < K`; call ITM iff `FSP > K`; `FSP = K` is **OTM** (exact equality expires
  worthless, never exercised — tested explicitly, not left to floating-point luck).
- All ITM contracts are exercised (do-not-exercise is not modelled; disclosed).

| Outcome | Action | Next state |
|---|---|---|
| Put OTM | premium kept, reserve released | CASH → Step 6 at this expiry close |
| **Put ITM, cash sufficient (assignment)** | pay `K × qty`, shares held, covered call sold T+1, assignment costs, compute economic basis | PENDING_STOCK → Step 7 |
| **Put ITM, cash insufficient (forced realisation)** | no delivery; pay intrinsic `(K − FSP) × qty` + settlement costs, book the net loss (Step 9a) | CASH → Step 6 |
| Call OTM | premium kept, shares still held | LONG_STOCK → Step 7 |
| **Call ITM (call-away)** | deliver shares, receive `K × qty`, call-away costs | CASH → Step 6 |

## Step 9a — Cash-only assignment rule

**Brindco must never borrow and must never run a negative cash balance.** Leverage raises the notional of
short puts; it does **not** entitle the strategy to take delivery beyond free cash. This constraint binds at
every leverage setting — a 5× book can write puts it cannot afford to be assigned on, and that is precisely
what this step resolves.

### The test, at each ITM put expiry

```
required_cash = K × qty + put_assignment_costs(K, FSP, qty)
free_cash     = the portfolio cash balance at the moment of settlement
```

`free_cash` is the **raw cash balance**, not cash net of the reserve held against other open puts. Delivery
is allowed whenever it leaves cash ≥ 0 on the day. The consequence is deliberate and must be visible in the
report: an early delivery can consume the cash a later put on the same day (or a later expiry) needed, and
that put then fails the test in turn. The cascade is a real portfolio outcome, not an accounting artefact.

### Case 1 — `free_cash ≥ required_cash` → physical assignment

- Take delivery of `qty` shares.
- Deduct `K × qty` and the assignment costs from cash.
- Add the shares to the wheel; record the economic basis (Step 10).
- The covered-call lifecycle begins at the next eligible trading day (`call_fill_lag_days`).

### Case 2 — `free_cash < required_cash` → forced realisation

**Do not borrow. Do not take partial delivery** — the test is all-or-nothing on the full assigned quantity,
never a part of the lots.

- The put is closed out for its intrinsic value: cash pays `(K − FSP) × qty` plus option settlement costs
  (brokerage, exchange, SEBI, GST, stamp as applicable — the `option_buy` leg of Step 11).
- No shares enter the portfolio. No covered call follows. The cycle ends here and the name returns to CASH.
- The loss is booked immediately as a **forced realisation loss**, net of all applicable costs.
- Option P&L is unchanged by the path — it is `(premium − intrinsic) × qty` either way. What differs is that
  no stock leg is created and the costs are option-settlement costs rather than delivery costs.

### Settlement order within a day

Because cash is now a binding, order-dependent resource, expiry settlement must be **deterministic**:

1. Settle all expiring **calls** first — a call-away releases cash and can fund a later put.
2. Then settle expiring **puts** in ascending `required_cash`, ties broken by symbol.

Ascending order maximises the number of deliveries the available cash supports. Any other order is a
different strategy; this one is fixed so results are reproducible.

### Residual shortfall — same-day forced sale

Intrinsic `(K − FSP) × qty` is always less than `K × qty`, so the close-out is cheaper than delivery — but it
is not free, and cash can be short of the close-out itself. The loss is contractually owed and cannot be
skipped, so it is funded by **force-selling held stock on the settlement day itself**: cash never goes
negative, not even intra-day. The order is fixed:

1. Wheels holding stock with **no open call**, descending market value (fewest positions disturbed), ties by
   symbol.
2. Only if those are exhausted: wheels whose shares are covered by a written call. The **call is bought back
   first**, then the shares are sold — selling covered shares without that would leave a naked call.
3. Never the symbol being settled; never a wheel holding no shares.
4. Sold at the CM close through the ordinary stock-sale path, with equity-sale costs and slippage. Each sale
   ends that wheel's cycle normally.
5. If step 2 is exhausted and cash is *still* short, the engine raises `InvariantError` — that is genuine
   insolvency, not a modelling choice, and is never papered over by borrowing.

`self.cash >= 0` is additionally asserted in the **daily reconciliation** (`_reconcile`), so a negative
balance fails the run rather than being clamped to zero.

### Consequence — leverage costs deliveries, but does not abolish them

Delivery costs the **full notional in cash**, while the target notional per name is `L × NAV / N`. What
matters is therefore the ratio `L / N`, not `L` alone: at the Brindco settings (`L = 5`, `N = 20`) a name's
notional is `0.25 × NAV`, which free cash can usually fund. Measured on the **actual** 2016–2025 backtest:

| Run | ITM puts delivered | cash-rejected | delivery rate | forced realisation loss | delivery prevented |
|---|---|---|---|---|---|
| `ranked_L1` | 108 | 0 | **100%** | ₹0 | ₹0 |
| `ranked_L2` | 100 | 9 | 92% | ₹21.7 lakh | ₹3.17 crore |
| `ranked_L3` | 75 | 27 | 74% | ₹1.11 crore | ₹15.0 crore |
| `ranked_L5` | 50 | 41 | **55%** | ₹2.96 crore | ₹40.6 crore |
| `frozen_L5` | 44 | 24 | 65% | ₹1.29 crore | ₹17.1 crore |

At 1× the gate never binds: `ranked_L1` and `frozen_L1` are **bit-identical** before and after the change,
which is the cleanest available evidence that the physical path was left untouched. Leverage then buys put
notional at the cost of deliveries, and the wheel keeps turning throughout — 181 calls were still sold at 5×.

What this does mean:

- **The pre-declared L ∈ {1, 2, 3, 5} grid is no longer like-for-like.** Leverage now also changes how often
  the stock leg is reached (100% → 55% of assignments), so the grid varies two things at once. Every
  leverage comparison must say so.
- **`ranked_L5_fin0` is now redundant, not merely questionable**: it varies a financing rate for a state that
  cannot occur, and it produces output **bit-identical** to `ranked_L5`. Retire it.
- A run at small `N` (or any setting where `L / N` approaches 1) *does* approach zero deliveries, and the
  wheel then degenerates into naked put selling. `L / N` is the quantity to watch, and it is a property of
  the configuration, not of this rule.
- If a higher delivery rate is wanted at leverage, the constraint belongs at **entry** (Step 6 sizing against
  deliverable cash) rather than only at settlement. That is a different rule and is not what is implemented.

### Why the test can fail at all

The frozen baseline reserves **100% of `K × qty`** while a put is open (Step 6.6), so delivery is always
funded and this step would never bind. Brindco reserves only `K × qty / L`. At `L = 5` a put reserves a fifth
of what assignment costs, so a book that is fully deployed by the margin rule can be structurally unable to
pay for its own assignments. Step 9a is the constraint that makes that solvent instead of borrowed.

### What this replaces

The engine currently takes delivery unconditionally and lets cash go negative, financed at
`margin_financing_rate` (`nse/wheel/engine.py::_settle_option`, and the `cash_liquidity()` docstring which
explicitly assumes borrowing to take delivery). Both must change with this step. Any run comparison across
this change is not like-for-like.

## Step 10 — Economic basis (per share, at assignment)

```
basis = K − put_premium_fill + put_sale_costs/share + assignment_costs/share
```

Call premiums and dividends are **not** subtracted. Accounting purchase price stays at K.

## Step 11 — Costs (date-effective, from `data/costs/wheel_charges_schedule.csv`)

For each trade, use the rate whose `effective_from` is the latest date on or before the trade date.

### 11a. Rates

| Component | Effective from | Rate | Base | Status |
|---|---|---|---|---|
| STT, option sale (seller) | 2019-10-01 | 0.05% | premium | VERIFIED |
| | 2023-04-01 | 0.0625% | premium | VERIFIED |
| | 2024-10-01 | 0.10% | premium | VERIFIED |
| | 2026-04-01 | 0.15% | premium | VERIFIED |
| STT, delivery (buy and sell) | 2019-10-01 | 0.10% | delivery value | VERIFIED |
| **Exchange txn charge, equity options** (NSE charge + IPFT) | 2019-10-01 | **0.0500%** (₹50.00/lakh) | premium turnover | VERIFIED [E1] |
| | 2021-01-01 | **0.0530%** (₹53.00/lakh) | premium turnover | VERIFIED [E1] |
| | 2023-04-01 | **0.0505%** (₹50.00 + ₹0.50 IPFT) | premium turnover | VERIFIED [E2] |
| | 2024-04-01 | **0.0500%** (₹49.50 + ₹0.50 IPFT) | premium turnover | VERIFIED [E3] |
| | 2024-10-01 | **0.03553%** (₹35.03 + ₹0.50 IPFT) | premium turnover | VERIFIED [E4] |
| | 2026-03-01 | **0.03553%** (IPFT folded in, total unchanged) | premium turnover | VERIFIED [E5] |
| **Exchange txn charge, equity cash** (NSE charge + IPFT) | 2019-10-01 | **0.00325%** (₹3.25/lakh) | traded value | VERIFIED [E1] |
| | 2021-01-01 | **0.00345%** (₹3.45/lakh) | traded value | VERIFIED [E1] |
| | 2023-04-01 | **0.00335%** (₹3.25 + ₹0.10 IPFT) | traded value | VERIFIED [E2] |
| | 2024-04-01 | **0.00332%** (₹3.22 + ₹0.10 IPFT) | traded value | VERIFIED [E3] |
| | 2024-10-01 | **0.00307%** (₹2.97 + ₹0.10 IPFT) | traded value | VERIFIED [E4] |
| | 2026-03-01 | **0.00307%** (IPFT folded in, total unchanged) | traded value | VERIFIED [E5] |
| **SEBI turnover fee** | 2019-10-01 | **0.0001%** (₹10/crore) | turnover or delivery value | VERIFIED [S1] |
| | 2020-06-01 | **0.00005%** (₹5/crore, COVID relief) | turnover or delivery value | VERIFIED [S1, S2] |
| | 2021-04-01 | **0.0001%** (₹10/crore, relief ends) | turnover or delivery value | VERIFIED end date [S2]; current rate matches Zerodha |
| Brokerage, option order | 2019-10-01 | ₹20 flat per order | — | ASSUMED: Zerodha schedule as published 2026-09-16, applied back to 2019-10-01 |
| Brokerage, equity delivery | 2019-10-01 | 0% | — | ASSUMED: Zerodha schedule as published 2026-09-16, applied back to 2019-10-01 |
| Brokerage, physical delivery | 2019-10-01 | 0.25% | delivery value | ASSUMED: Zerodha schedule as published 2026-09-16, applied back to 2019-10-01 |
| Stamp duty, options (buy side) | 2019-10-01 | 0.003% | premium; paid by the buyer, so **not charged on the wheel's option sales**, only on forced buy-backs | ASSUMED before 2020-07-01 (state-wise rates, uniform rate used as proxy) |
| | 2020-07-01 | 0.003% | premium | VERIFIED [PIB 1635399] |
| Stamp duty, equity delivery (buy side) | 2019-10-01 | 0.015% | delivery value | ASSUMED before 2020-07-01 (state-wise rates, uniform rate used as proxy) |
| | 2020-07-01 | 0.015% | delivery value | VERIFIED [PIB 1635399] |
| DP charge (delivery sale) | 2019-10-01 | ₹15.34 per scrip, GST included | — | ASSUMED: Zerodha schedule as published 2026-09-16, applied back to 2019-10-01 |
| GST | 2019-10-01 | 18% | brokerage + exchange txn + SEBI fee (never on STT or stamp duty) | ASSUMED |

**Delivery value** = `max(K, FSP) × qty` (ASSUMED, the conservative choice).

#### Sources for the exchange and SEBI rates (primary documents only)

| Ref | Document | What it fixes |
|---|---|---|
| E1 | NSE/FA/46730, 18 Dec 2020 — [FA46730.pdf](https://archives.nseindia.com/content/circulars/FA46730.pdf) | "Existing" rates (options ₹50.00, cash ₹3.25 per lakh) and the increase to ₹53.00 / ₹3.45 from **1 Jan 2021** |
| E2 | NSE/FA/56129, 24 Mar 2023 — [FA56129.pdf](https://archives.nseindia.com/content/circulars/FA56129.pdf) | Roll-back to ₹50.00 / ₹3.25 from **1 Apr 2023**; IPFT raised from ₹0.01/crore to ₹50/crore (options) and ₹10/crore (cash) |
| E3 | NSE/FA/61137, 14 Mar 2024 — [FA61137.pdf](https://nsearchives.nseindia.com/content/circulars/FA61137.pdf) | Cut to ₹49.50 / ₹3.22 from **1 Apr 2024** |
| E4 | NSE/FA/64232, 27 Sep 2024 — [FA64232.pdf](https://nsearchives.nseindia.com/content/circulars/FA64232.pdf) | Flat true-to-label ₹35.03 / ₹2.97 from **1 Oct 2024** (SEBI/HO/MRD/TPD-1/P/CIR/2024/92) |
| E5 | NSE/FA/73061, 27 Feb 2026 — [FA73061.pdf](https://nsearchives.nseindia.com/content/circulars/FA73061.pdf) | Confirms total member outflow ₹3,553/crore (options) and ₹307/crore (cash), IPFT folded in from **1 Mar 2026** |
| S1 | SEBI Board memorandum, *Relaxation in Applicable Fees on account of COVID-19* — [PDF](https://www.sebi.gov.in/sebi_data/meetingfiles/jul-2020/1594293677017_1.pdf) | Existing broker turnover fee ₹10/crore (securities and equity derivatives); 50% cut June 2020 – March 2021 |
| S2 | SEBI press release, 27 Apr 2020 — [link](https://www.sebi.gov.in/media/press-releases/apr-2020/sebi-reduces-broker-turnover-fees-and-filing-fees-for-issuers_46571.html) | "reduced to 50% of the existing fee structure for the period June 2020 to March 2021" |

How to read these:

1. **Client rate = NSE transaction charge + NSE IPFT contribution.** Since 1 Oct 2024 SEBI's true-to-label
   rule makes the client charge equal the exchange charge, which is why Zerodha's 0.03553% / 0.00307%
   match E4 + IPFT exactly.
2. **Before 1 Oct 2024 NSE billed brokers on monthly slabs** (options: flat ₹2,500 up to ₹3 crore premium,
   then the per-lakh rates above). The rulebook uses the **first incremental slab** (the highest rate), which
   is what retail brokers passed on to clients. Pre-2023 IPFT (₹0.01/crore) is negligible and ignored.
3. **The 2019-10-01 row** is E1's "existing" rate as of Dec 2020. No NSE revision between Oct 2019 and
   Dec 2020 was found; treat that start date as *in force by Dec 2020, assumed from Oct 2019*.
4. **Cash-market concession:** NSE/FA/46225 gives lower CM charges on EQ stocks outside NIFTY 50 /
   NIFTY Next 50. This only touches the equity-sale path (F&O exits) and is not modelled — conservative.

### 11b. What each path charges (as in `nse/wheel/costs.py`)

| Path | STT | Brokerage | Exchange txn | SEBI fee | Stamp | DP | GST |
|---|---|---|---|---|---|---|---|
| **Option sale** (put or call) | option-sale rate × premium | ₹20 | options rate × premium | × premium | — (seller) | — | ✓ |
| **Put assignment** (we receive shares) | 0.10% × delivery value | 0.25% × delivery value | **not charged** (settled by NSE Clearing, not an exchange trade; explicit 0% row) | × delivery value | 0.015% × delivery value | — | ✓ |
| **Call-away** (we deliver shares) | 0.10% × delivery value | 0.25% × delivery value | **not charged** (explicit 0% row) | × delivery value | — | ₹15.34 | ✓ |
| **Equity sale** (F&O exit, lifecycle) | 0.10% × traded value | 0% | cash rate × traded value | × traded value | — | ₹15.34 | ✓ |
| Exercise STT on intrinsic value | paid by the option buyer; **the wheel never pays it** (assert = 0) | | | | | | |

Worked example: selling a put with premium turnover ₹1,00,000 on 2025-01-15:
exchange txn = 0.03553% × 1,00,000 = ₹35.53; SEBI = 0.0001% × 1,00,000 = ₹0.10;
GST = 18% × (20 + 35.53 + 0.10) = ₹10.01; STT = 0.10% × 1,00,000 = ₹100.

Equity slippage (`base`) = 5 bps on stock sales. Every non-STT cost (brokerage, exchange txn, SEBI fee,
GST, stamp duty, DP charge) scales with `non_stt_cost_multiplier` (1.0 baseline; 0.5 and 1.5 reported).

## Step 12 — Mark, NAV, reconcile (daily)

- Short options marked at close; stale/missing ⇒ carry last mark and flag; P1 if > 5 consecutive days.
- Stock marked at CM EQ close.
- `NAV = cash + stock value − short option liability`.
- Reconcile **two ways** every day, tolerance ₹1:
  1. component P&L (premium, costs, stock MTM, option MTM, settlements) = ΔNAV
  2. independent rebuild of NAV from the trade ledger
- Frozen baseline: margin (SPAN + exposure + expiry-week proxy) is **tracked and reported only, never used for sizing**.
- Brindco: margin = `notional / L` (a proxy, not SPAN) **is** used for sizing and for next-day liquidation above
  `max_margin_utilization`. Cash is also rebuilt from the ledger every day (±₹1, hard fail).

## Step 13 — Lifecycle events

| Event | Rule |
|---|---|
| Name leaves F&O | No new options. Hold stock to the last expiry, then sell at next close with costs. Cash stays idle; no replacement. Never re-enter. |
| Merger | Follow share ratio; continue only if successor is F&O, else exit as above |
| Demerger | F&O successor continues (TATAMOTORS → TMPV); non-F&O shares carried at apportioned cost, sold at first listing close |
| Locked quantity | Cannot sell or cover; still marked at market |
| Delisting / suspension | Hold at last traded price, flag P1; never invent an exit price |

## Step 14 — Record diagnostics (per name and portfolio)

`cycles_skipped_insufficient_cash`, `cycles_skipped_lot_too_large`, `cycles_skipped_no_eligible_strike`,
`cycles_skipped_participation`, `retries_used`, `entries_missed`, `stale_marked_days`,
`stale_mark_p1_flags`, `fsp_fallback_to_future_settle`, `pre_apr2023_nearest_itm_assignments`,
`lot_unresolved_skips`, `lot_fallback_days`, `lot_mid_contract_change_flags`, `fo_missing_day_exposure`,
`early_termination_events`, `deployed_vs_idle_capital`, `locked_quantity_days`.

Cash-only assignment (Step 9a): `assignments_physical`, `assignments_cash_rejected`,
`forced_realisation_loss_inr`, `assignment_costs_inr`, `cash_rejected_inr` (the `required_cash` refused),
`max_cash_utilisation`, `min_free_cash`, `cash_settlement_shortfall`.

Data gap: a trading day with no F&O file ⇒ no signals, no fills (scheduled fills fail → retry), option
marks carried; stock still marked if the CM close exists.

---

## Step 15 — Validate before trusting results

1. Synthetic backtest → 1 name → 3 names → 10 names over Oct 2019–Jun 2020 (includes March 2020) → full window.
   Reconcile at every rung; stop on any failure.
2. Look-ahead tests: decisions must be identical if all data after t is deleted.
3. Manual audit: trace one put-assignment and one call-away cycle
   `bhavcopy row → signal → strike → fill → premium → costs → assignment → shares → MTM → call → settlement → NAV`,
   quoting raw rows by file and line.

## Step 16 — Report

- **Metrics:** CAGR, vol, Sharpe, Sortino, max drawdown, Calmar, monthly returns, turnover.
- **Wheel stats:** cycles, assignment rate, call-away rate, days in stock, premium yield, P&L split
  (premium vs stock vs costs).
- **Cash-only assignment (Step 9a), reported separately:** ITM puts physically assigned; ITM puts not
  delivered for want of cash; realised assignment losses; assignment costs; cash rejected / delivery
  prevented; maximum cash utilisation; minimum free cash; number of forced realisations.
- **Two auditable consequences of the no-borrow constraint, documented explicitly in the report.** Neither is
  a bug; both follow from the rule and must be visible rather than buried:
  1. **Forced-sale cascade** — settling one put can force an unrelated wheel to terminate early, because its
     stock was sold to fund the close-out. Counted as `cash_settlement_forced_sales`, with the sold value.
  2. **Forced call buyback** — funding a settlement can require buying back an otherwise profitable covered
     call before its shares can be sold. Counted as `cash_settlement_forced_call_buybacks`.
- **The leverage caveat** from Step 9a: the L grid is not like-for-like, and at 5× the wheel does not turn.
- **Benchmarks** on the same window and capital (e.g. buy-and-hold of the 10 names, NIFTY 50).
- **Sensitivity (11 runs, all reported, no winner picked):**
  OTM ∈ {3%, 5%, 7.5%} × slippage ∈ {low, base, high} (9) + `calls_from_spot` (no basis floor)
  + `tbill_idle_cash` (idle cash at the 91-day T-bill rate).
- Cost drag at non-STT multiplier 0.5 and 1.5.

---

## The Brindco expiry lifecycle

This is the authoritative order. Where it differs from the frozen baseline, Brindco wins.

```
EXPIRY DAY t
│
├── Load CM / option / F&O settlement data
│
├── Apply corporate actions (splits, bonuses, dividends, rights, mergers, demergers, locked qty)
│
├── Release shares assigned earlier whose call was decided at the assignment close
│
├── Mark existing positions (stock on CM close, short options on option close)
│
├── Determine moneyness on the applicable stock-option final exercise settlement
│   price — the CM close of the underlying (Step 9). FSP = K is OTM.
│
├── OTM put
│      └── keep premium, release reserve → CASH
│
├── ITM put ──► CASH TEST (Step 9a): required = K × qty + assignment costs
│      │        vs free cash. Never borrow; never partial.
│      │
│      ├── cash sufficient → physical assignment at K
│      │     ├── pay K × qty + applicable costs
│      │     ├── receive shares
│      │     ├── calculate economic basis (Step 10)
│      │     └── schedule the call decision
│      │
│      └── cash insufficient → forced realisation
│            ├── pay intrinsic (K − FSP) × qty + option settlement costs
│            ├── no shares received
│            └── book the net loss → CASH
│
├── OTM call
│      └── keep premium, retain shares
│
└── ITM call ──► physical call-away at K
       ├── deliver shares
       ├── receive K × qty
       ├── call-away costs
       └── → CASH

   Settlement order within the day is fixed: calls first, then puts by ascending
   required cash (Step 9a) — cash is a binding, order-dependent resource.

T+1
│
├── Use the actual T+1 lot size (Step 8.0), not the lot that sized the decision
│
├── Contract = nearest monthly expiry strictly after T+1
│
├── The initial covered call has priority (`covered_call_first`): it fills before
│   any new put, and puts wait while a call order is pending
│
├── Call fill subject to liquidity and the 10% participation cap
│
├── Top-ups into an open call are handled separately (and refused on lot mismatch)
│
└── New puts only once the initial-call priority rule is satisfied
```
