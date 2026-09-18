"""Date-effective transaction costs and slippage for the wheel.

Every rate comes from data/costs/wheel_charges_schedule.csv and data/costs/slippage_schedule.csv
(rulebook Step 11); nothing numeric is hard-coded here. A rate applies on a date when
from <= date <= to ("current" = open ended).

Paths (rulebook 11b):
    option_sale      STT(sale) + ₹20 brokerage + exchange txn + SEBI fee, GST on the non-STT fees
    option_buy       used only for forced buy-backs: stamp duty + ₹20 + exchange txn + SEBI, GST
    put_assignment   delivery STT + 0.25% brokerage + SEBI + stamp duty, GST (exchange txn row = 0%)
    call_away        delivery STT + 0.25% brokerage + SEBI + DP charge, GST (exchange txn row = 0%)
    equity_sale      STT + cash exchange txn + SEBI + DP charge, GST
    equity_buy       equal-weight benchmark only: STT + cash exchange txn + SEBI + stamp duty, GST
Delivery value for assignment paths = max(K, FSP) × qty (conservative).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class CostBreakdown:
    stt: float = 0.0
    brokerage: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    dp: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return self.stt + self.brokerage + self.exchange + self.sebi + self.stamp + self.dp + self.gst


class CostModel:
    def __init__(self, charges_file: Path, slippage_file: Path, slippage_level: str = "base"):
        ch = pd.read_csv(charges_file)
        ch["from"] = pd.to_datetime(ch["from"])
        ch["to"] = pd.to_datetime(ch["to"].replace("current", "2100-12-31"))
        self.ch = ch
        sl = pd.read_csv(slippage_file)
        sl["from"] = pd.to_datetime(sl["from"])
        sl["to"] = pd.to_datetime(sl["to"].replace("current", "2100-12-31"))
        self.sl = sl[sl.level == slippage_level].reset_index(drop=True)
        if self.sl.empty:
            raise ValueError(f"unknown slippage level {slippage_level!r}")
        self._cache: dict = {}

    # ------------------------------------------------------------------ lookups
    def _row(self, trade_type: str, charge: str, date) -> pd.Series:
        key = (trade_type, charge, pd.Timestamp(date))
        if key not in self._cache:
            d = pd.Timestamp(date)
            hit = self.ch[(self.ch.trade_type == trade_type) & (self.ch.charge == charge)
                          & (self.ch["from"] <= d) & (self.ch["to"] >= d)]
            if len(hit) != 1:
                raise KeyError(f"expected one {trade_type}/{charge} rate on {d.date()}, found {len(hit)}")
            self._cache[key] = hit.iloc[0]
        return self._cache[key]

    def rate(self, trade_type: str, charge: str, date) -> float:
        return float(self._row(trade_type, charge, date).rate_fraction)

    def fixed(self, trade_type: str, charge: str, date) -> float:
        return float(self._row(trade_type, charge, date).fixed_inr)

    def _gst(self, c: CostBreakdown, date, trade_type: str) -> float:
        return self.rate(trade_type, "GST", date) * (c.brokerage + c.exchange + c.sebi)

    # ------------------------------------------------------------------ paths
    def option_sale(self, date, turnover: float) -> CostBreakdown:
        t = "Option sale"
        c = CostBreakdown(stt=self.rate(t, "STT", date) * turnover,
                          brokerage=self.fixed(t, "Brokerage", date),
                          exchange=self.rate(t, "Exchange txn charge", date) * turnover,
                          sebi=self.rate(t, "SEBI turnover fee", date) * turnover)
        c.gst = self._gst(c, date, t)
        return c

    def option_buy(self, date, turnover: float) -> CostBreakdown:
        t = "Option sale"   # exchange, SEBI, brokerage and GST are charged on both sides
        c = CostBreakdown(brokerage=self.fixed(t, "Brokerage", date),
                          exchange=self.rate(t, "Exchange txn charge", date) * turnover,
                          sebi=self.rate(t, "SEBI turnover fee", date) * turnover,
                          stamp=self.rate("Option buy (not used)", "Stamp duty", date) * turnover)
        c.gst = self._gst(c, date, t)
        return c

    def put_assignment(self, date, strike: float, fsp: float, qty: int) -> CostBreakdown:
        t, v = "Put assignment", max(strike, fsp) * qty
        c = CostBreakdown(stt=self.rate(t, "STT (delivery)", date) * v,
                          brokerage=self.rate(t, "Brokerage (physical delivery)", date) * v,
                          exchange=self.rate(t, "Exchange txn charge", date) * v,
                          sebi=self.rate(t, "SEBI turnover fee", date) * v,
                          stamp=self.rate(t, "Stamp duty", date) * v)
        c.gst = self._gst(c, date, t)
        return c

    def call_away(self, date, strike: float, fsp: float, qty: int) -> CostBreakdown:
        t, v = "Call-away", max(strike, fsp) * qty
        c = CostBreakdown(stt=self.rate(t, "STT (delivery)", date) * v,
                          brokerage=self.rate(t, "Brokerage (physical delivery)", date) * v,
                          exchange=self.rate(t, "Exchange txn charge", date) * v,
                          sebi=self.rate(t, "SEBI turnover fee", date) * v,
                          dp=self.fixed(t, "DP charge", date))
        c.gst = self._gst(c, date, t)
        return c

    def equity_sale(self, date, value: float) -> CostBreakdown:
        t = "Equity sale"
        c = CostBreakdown(stt=self.rate(t, "STT (delivery)", date) * value,
                          brokerage=self.rate(t, "Brokerage", date) * value,
                          exchange=self.rate(t, "Exchange txn charge", date) * value,
                          sebi=self.rate(t, "SEBI turnover fee", date) * value,
                          dp=self.fixed(t, "DP charge", date))
        c.gst = self._gst(c, date, t)
        return c

    def equity_buy(self, date, value: float) -> CostBreakdown:
        t = "Equity sale"
        c = CostBreakdown(stt=self.rate(t, "STT (delivery)", date) * value,
                          exchange=self.rate(t, "Exchange txn charge", date) * value,
                          sebi=self.rate(t, "SEBI turnover fee", date) * value,
                          stamp=self.rate("Put assignment", "Stamp duty", date) * value)
        c.gst = self._gst(c, date, t)
        return c

    # ------------------------------------------------------------------ slippage
    def _slip_row(self, date) -> pd.Series:
        d = pd.Timestamp(date)
        hit = self.sl[(self.sl["from"] <= d) & (self.sl["to"] >= d)]
        if len(hit) != 1:
            raise KeyError(f"no slippage row on {d.date()}")
        return hit.iloc[0]

    def tick(self, date, underlying_ref_price: float) -> float:
        r = self._slip_row(date)
        if pd.isna(r.tick_price_threshold):
            return float(r.tick_at_or_above)
        return float(r.tick_below if underlying_ref_price < r.tick_price_threshold else r.tick_at_or_above)

    def option_fill(self, date, price: float, side: str, underlying_ref_price: float) -> float | None:
        """Fill price after slippage: sell = P − slip, buy = P + slip, slip = max(k·tick, s·P).
        A sell that would fill below min_ticks·tick returns None (fill fails)."""
        r = self._slip_row(date)
        tk = self.tick(date, underlying_ref_price)
        slip = max(r.ticks * tk, r.pct * price)
        if side == "sell":
            px = price - slip
            return None if px < r.min_ticks * tk - 1e-9 else round(px, 4)
        return round(price + slip, 4)

    def equity_slippage_bps(self, date) -> float:
        return float(self._slip_row(date).equity_sale_bps)


def default_cost_model(params: dict) -> CostModel:
    return CostModel(ROOT / params["costs_file"], ROOT / params["slippage_file"], params["slippage_level"])
