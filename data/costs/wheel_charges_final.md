# Wheel Strategy — Final Charges to Calculate

Files in this folder:

- `wheel_charges_schedule.csv` — one row per **trade type × charge × date range**, sorted in that order.
- `charge_sources.csv` — every `source_id` with issuer, reference, date and URL.
- `slippage_schedule.csv` — slippage per level (`low`/`base`/`high`): `max(ticks × tick, pct × price)` on options,
  `equity_sale_bps` on stock sales. The tick is ₹0.05 until 2025-11-02. From 2025-11-03 it is ₹0.01 below a
  ₹250 underlying and ₹0.05 at or above.

Read by `nse/wheel/costs.py`, which looks up rows by the exact `trade_type` and `charge` text. **Renaming a
label breaks the lookup.** For example, the row labelled "Option buy (not used)" is in fact used for forced
buy-backs.

For every trade: filter by `trade_type`, then take the row where `from ≤ trade date ≤ to` (`to = current` means still in force).

| Column | Meaning |
|---|---|
| `trade_type` | Option sale, Put assignment, Call-away, Equity sale (plus two reference rows the wheel never pays) |
| `charge` | STT, Exchange txn charge, SEBI turnover fee, Brokerage, Stamp duty, DP charge, GST |
| `from`, `to` | Date range the rate applies to |
| `rate` | Human-readable: `0.03553%` or `₹20.00 per order` |
| `rs_per_lakh` | Same rate as rupees per ₹1 lakh of value — easiest to sanity-check |
| `rate_fraction` | The number code should multiply by (0.0003553) |
| `fixed_inr` | Flat rupee amount, for per-order / per-scrip charges |
| `charged_on` | The value the rate is applied to |
| `paid_by` | Which side pays |
| `status` | VERIFIED (primary source) or ASSUMED |
| `source_id` | Key into `charge_sources.csv` |

## Definitions

- `premium_turnover = option fill price × qty`
- `delivery_value = higher of strike or FSP × qty` (ASSUMED, conservative)
- `traded_value = stock price × qty`
- Rates are fractions (0.0005 = 0.05%).

## 1. Option sale (every put and call written)

```
stt        = stt_option_sale(date)       × premium_turnover
brokerage  = ₹20 per order
exch_txn   = exchange_txn_options(date)  × premium_turnover
sebi_fee   = sebi_turnover_fee(date)     × premium_turnover
gst        = 18% × (brokerage + exch_txn + sebi_fee)
stamp      = 0                            (buyer pays)
TOTAL      = stt + brokerage + exch_txn + sebi_fee + gst
```

## 2. Put assignment (put ITM, we receive shares)

```
stt        = 0.10% × delivery_value
brokerage  = 0.25% × delivery_value
exch_txn   = 0                            (not charged: settled by NSE Clearing, not an exchange trade; explicit 0% row)
sebi_fee   = sebi_turnover_fee(date) × delivery_value
stamp      = 0.015% × delivery_value      (we are the buyer)
gst        = 18% × (brokerage + sebi_fee)
TOTAL      = stt + brokerage + sebi_fee + stamp + gst
```

## 3. Call-away (call ITM, we deliver shares)

```
stt        = 0.10% × delivery_value
brokerage  = 0.25% × delivery_value
exch_txn   = 0
sebi_fee   = sebi_turnover_fee(date) × delivery_value
dp_charge  = ₹15.34 (GST included)
stamp      = 0                            (seller)
gst        = 18% × (brokerage + sebi_fee)
TOTAL      = stt + brokerage + sebi_fee + dp_charge + gst
```

## 4. Equity sale (F&O exit / lifecycle)

```
stt        = 0.10% × traded_value
brokerage  = 0
exch_txn   = exchange_txn_cash(date) × traded_value
sebi_fee   = sebi_turnover_fee(date) × traded_value
dp_charge  = ₹15.34
gst        = 18% × (exch_txn + sebi_fee)
slippage   = 5 bps × traded_value (base)
TOTAL      = stt + exch_txn + sebi_fee + dp_charge + gst (+ slippage in fill price)
```

## 5. Option buy-back (forced liquidation only)

```
stt        = 0                            (buyer pays no option STT)
brokerage  = ₹20 per order
exch_txn   = exchange_txn_options(date) × premium_turnover
sebi_fee   = sebi_turnover_fee(date)    × premium_turnover
stamp      = 0.003% × premium_turnover    (we are the buyer)
gst        = 18% × (brokerage + exch_txn + sebi_fee)
```

Exercise STT on intrinsic value is paid by the option **buyer** — always 0 for the wheel.

## Date-effective rates at a glance

| Period | STT option sale | Exch txn options | Exch txn cash | SEBI fee |
|---|---|---|---|---|
| 2019-10-01 → 2020-05-31 | 0.05% | 0.0500% | 0.00325% | 0.0001% |
| 2020-06-01 → 2020-12-31 | 0.05% | 0.0500% | 0.00325% | **0.00005%** |
| 2021-01-01 → 2021-03-31 | 0.05% | 0.0530% | 0.00345% | 0.00005% |
| 2021-04-01 → 2023-03-31 | 0.05% | 0.0530% | 0.00345% | 0.0001% |
| 2023-04-01 → 2024-03-31 | 0.0625% | 0.0505% | 0.00335% | 0.0001% |
| 2024-04-01 → 2024-09-30 | 0.0625% | 0.0500% | 0.00332% | 0.0001% |
| 2024-10-01 → 2026-03-31 | 0.10% | 0.03553% | 0.00307% | 0.0001% |
| 2026-04-01 → | 0.15% | 0.03553% | 0.00307% | 0.0001% |

Constant over the whole window: delivery STT 0.10%, physical-delivery brokerage 0.25%, option brokerage ₹20/order,
stamp duty on delivery purchase 0.015%, DP ₹15.34, GST 18%.

## Worked examples

**Put sale, 2025-01-15**, premium turnover ₹1,00,000:
STT ₹100.00 + brokerage ₹20.00 + exch ₹35.53 + SEBI ₹0.10 + GST ₹10.01 = **₹165.64**

**Put sale, 2021-06-15**, premium turnover ₹1,00,000:
STT ₹50.00 + brokerage ₹20.00 + exch ₹53.00 + SEBI ₹0.10 + GST ₹13.16 = **₹136.26**

**Put assignment, 2025-01-30**, K = ₹1,000, FSP = ₹980, qty 500 → delivery value ₹5,00,000:
STT ₹500.00 + brokerage ₹1,250.00 + SEBI ₹0.50 + stamp ₹75.00 + GST ₹225.09 = **₹2,050.59**

**Call-away, 2025-02-27**, K = ₹1,050, FSP = ₹1,070, qty 500 → delivery value ₹5,35,000:
STT ₹535.00 + brokerage ₹1,337.50 + SEBI ₹0.54 + DP ₹15.34 + GST ₹240.85 = **₹2,129.22**

## Status

- **VERIFIED from primary sources:** all STT, all exchange transaction charges (NSE/FA/46730, 56129, 61137, 64232, 73061),
  SEBI turnover fee including the Jun 2020 – Mar 2021 halving (SEBI board memo + press release 27-Apr-2020).
- **ASSUMED (Zerodha schedule as published 2026-09-16, zerodha.com/charges, applied to the whole window; historical
  broker rates not reconstructed):** brokerage, physical-delivery fee, DP charge, GST.
- **Stamp duty:** VERIFIED from 2020-07-01 (uniform rates, PIB 1635399). Before that it was levied state by state; the
  uniform rate is used as an ASSUMED proxy for 2019-10-01 → 2020-06-30.
- **Option-premium stamp duty (0.003%)** is paid by the buyer. The wheel sells options, so it doesn't pay it on entries;
  it's charged only on forced buy-backs (`CostModel.option_buy`).
- Pre-Oct-2024 exchange charges are NSE's highest (first incremental) member slab, as passed to retail clients.
- Not modelled (conservative): NSE cash-market concession for stocks outside NIFTY 50 / Next 50 (NSE/FA/46225).
