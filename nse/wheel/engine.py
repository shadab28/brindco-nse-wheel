"""Leveraged wheel engine: short put -> assignment -> covered call -> call-away -> short put.

Daily loop for trading day t (rulebook Step 4, with leverage-aware margin):

    1  accrue financing on borrowed cash (cash < 0) since the previous trading day; credit the month's risk-free
       rate on positive cash to cash daily (so NAV includes it)
    2  apply corporate actions with ex-date t (share count / strike adjustments)
    3  contract terminations on t (merger / demerger): settle at CM close, sell any stock
    4  mark stock at CM close, short options at close (settle if untraded; carry if missing)
    5  expiry processing: put ITM iff FSP < K (assignment), call ITM iff FSP > K (call-away)
    6  fill orders decided on t-1 at t's close less slippage (participation cap, margin re-check)
    7  NAV, margin, utilisation; utilisation > max ⇒ schedule liquidation for t+1
    8  decisions for t+1 using data dated <= t only (entries on expiry days, calls when shares usable)
    9  reconcile: cash rebuilt from the ledger, invariants (no naked calls, <= N names)

Leverage L: a short put needs K × qty / L of margin; assigned stock needs S × shares / L (the shortfall
in cash is borrowed and charged `margin_financing_rate`); a covered call needs no extra margin.
Sizing targets L × NAV / N notional per name, capped by portfolio liquidity
    max_util × NAV − Σ leveraged requirements − reserve for unfilled put orders,
where a new put consumes (K − mark)·q/L less the premium cash it brings in (see `max_affordable_lots`);
every sale, rejection, assignment, call-away, interest credit and financing charge is logged to `audit`.

Covered-call priority (`covered_call_first`): after an assignment the portfolio is re-measured and flagged
(ASSIGNMENT_LIQUIDITY_WARNING); calls on usable shares are decided and filled before any new put; while a call
order is still pending (and has not exhausted `max_retries` fills) no put is decided (SKIP_NEW_PUT), and the
entry is re-evaluated after the calls fill. On a margin breach with uncovered usable shares, liquidation waits one
decision cycle so those calls are attempted first; the existing liquidation then runs if the breach persists. Leverage never touches a price or a premium: P&L comes
from fills, settlements and marks only.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from nse.wheel.corporate_actions import distribution_per_old_share

PUT, CALL = "PE", "CE"


# ------------------------------------------------------------------------------ state
@dataclass
class OptionPos:
    trade_id: int
    cycle_id: int
    symbol: str
    option_type: str
    cmonth: str
    expiry: pd.Timestamp
    strike: float
    lots: int
    lot: int
    qty: int
    entry_date: pd.Timestamp
    entry_premium: float          # per share, after slippage
    entry_quote: float            # per share, before slippage
    entry_costs: float
    underlying_entry: float
    margin_unlev: float
    margin_lev: float
    last_mark: float
    stale_days: int = 0


@dataclass
class Wheel:
    symbol: str
    cycle_id: int | None = None
    state: str = "CASH"            # CASH | SHORT_PUT | STOCK | STOCK_CALL
    shares: int = 0
    stock_price: float = 0.0       # accounting entry price per share (FSP at assignment)
    stock_date: pd.Timestamp | None = None
    econ_basis: float = 0.0
    usable_from: pd.Timestamp | None = None
    option: OptionPos | None = None
    stock_mark: float = 0.0


@dataclass
class Order:
    symbol: str
    kind: str                      # PUT | CALL | LIQUIDATE | SELL_STOCK | UNIVERSE_EXIT
    decided_on: pd.Timestamp
    cmonth: str | None = None
    strike: float | None = None
    lots: int = 0
    lot: int = 0
    retries: int = 0
    reason: str = ""
    est_premium: float = 0.0       # per share, decision-day close: reserved liquidity is net of it


@dataclass
class Cycle:
    cycle_id: int
    symbol: str
    start: pd.Timestamp
    end: pd.Timestamp | None = None
    status: str = "open"
    n_puts: int = 0
    n_calls: int = 0
    assigned: bool = False
    called_away: bool = False
    premium: float = 0.0
    option_pnl: float = 0.0
    stock_pnl: float = 0.0
    costs: float = 0.0
    max_margin_lev: float = 0.0
    max_margin_unlev: float = 0.0
    put_strike: float = 0.0
    assign_fsp: float = 0.0
    assign_date: pd.Timestamp | None = None
    first_call_spot: float | None = None
    min_spot_while_held: float | None = None
    # share ledger: held = assigned + corporate-action delta - called away - sold; must be 0 when the cycle ends
    shares_assigned: int = 0
    shares_ca_delta: int = 0
    shares_called_away: int = 0
    shares_sold: int = 0
    cash_settled: bool = False              # ITM put closed out for want of cash (Step 9a), no delivery taken
    stopped_out: bool = False               # short put closed early by the price stop (no delivery taken)
    stop_level: float = 0.0                 # underlying close at or below which the put is stopped; 0 = no stop
    max_uncovered_at_call: int = 0          # shares left outside the call when it was sold (lot size / participation)
    residual_after_call_away: int = 0       # shares still held right after a call-away; sold next day
    n_call_topups: int = 0                  # extra calls sold into the open call to cover whole lots left uncovered

    @property
    def shares_open(self) -> int:
        return self.shares_assigned + self.shares_ca_delta - self.shares_called_away - self.shares_sold


class InvariantError(AssertionError):
    pass


def exit_reason(status: str) -> str:
    """Which event closed a position; universe removal, contract expiry and corporate actions stay separate."""
    if status.startswith("universe_removal"):
        return "UNIVERSE_REMOVAL"
    if status.startswith("terminated"):
        return "CORPORATE_ACTION"
    if status.startswith("margin_call"):
        return "MARGIN_CALL"
    if status in ("assigned", "expired_otm", "exercised", "called_away", "cash_settled_insufficient_cash"):
        return "CONTRACT_EXPIRY"
    if status == "fo_exit":
        return "FO_ELIGIBILITY_EXIT"
    if status in ("uncoverable_odd_lot", "residual_after_call_away"):
        return "ODD_LOT_SALE"
    return status.upper()


def max_affordable_lots(free, strike, lot, leverage, premium, mark, cost_per_lot, max_util, cap,
                        cash_free=None) -> int:
    """Largest lot count (<= cap) whose short put keeps portfolio liquidity >= 0.

    Selling n lots (q = n × lot) at `premium`, marked at `mark`, changes liquidity by
        + max_util × (premium − mark) × q      premium cash in, less the liability booked at the mark
        − max_util × costs                     costs leave NAV
        − (K − mark) × q / L                   leveraged requirement, already net of the premium
    so each lot consumes (K − mark)·lot/L − max_util·((premium − mark)·lot − cost_per_lot).
    The cash leg (`cash_free`, see `cash_liquidity`) locks the full K·lot/L and receives premium·lot − costs: cash
    carries no option liability, so the mark cannot offset the cash reserve.
    """
    def fit(avail, per_lot):
        if avail <= 0:
            return 0
        return int(cap) if per_lot <= 0 else int(max(0, min(cap, math.floor(avail / per_lot))))

    if cap < 1:
        return 0
    lots = fit(free, (strike - mark) * lot / leverage - max_util * ((premium - mark) * lot - cost_per_lot))
    if cash_free is not None:
        lots = min(lots, fit(cash_free, strike * lot / leverage - (premium * lot - cost_per_lot)))
    return lots


# ------------------------------------------------------------------------------ engine
class WheelEngine:
    def __init__(self, md, costs, cfg: dict, selections, corporate_actions: pd.DataFrame | None = None,
                 sectors: dict | None = None, risk_free: pd.Series | None = None, universe=None):
        """
        md          MarketData
        costs       CostModel
        cfg         merged params (top level + backtest section), see params.yaml
        selections  callable(date) -> ordered list of symbols eligible for new puts on that decision day,
                    built only from information dated <= date
        risk_free   annual risk-free rate (decimal) indexed by 'YYYY-MM'; positive cash earns it. None = 0%.
        universe    callable(symbol, date) -> bool: historical strategy-universe membership on that date (no later
                    information). A non-member gets no new put, call or cycle; an existing position is wound down
                    (UNIVERSE_EXIT). None = every symbol stays eligible.
                    A month missing from the series uses the latest earlier month.
        """
        self.md, self.costs, self.cfg, self.selections = md, costs, cfg, selections
        self.L = float(cfg["leverage"])
        self.N = int(cfg["n_positions"])
        self.max_util = float(cfg["max_margin_utilization"])
        self.fin_rate = float(cfg["margin_financing_rate"])
        self.sectors = sectors or {}
        self.cash = float(cfg["initial_capital"])
        self.initial = float(cfg["initial_capital"])
        self.wheels: dict[str, Wheel] = {}
        self.orders: list[Order] = []
        self.ledger: list[tuple] = []
        self.trades: list[dict] = []
        self.cycles: dict[int, Cycle] = {}
        self.daily: list[dict] = []
        self.symbol_marks: list[tuple] = []   # (date, symbol, shares × stock mark − short option qty × mark)
        self.events: list[dict] = []
        self.diag = Counter()
        self._tid = 0
        self._cid = 0
        self._prev_day = None
        self.ca = {}
        if corporate_actions is not None and len(corporate_actions):
            for r in corporate_actions.itertuples():
                self.ca.setdefault(pd.Timestamp(r.ex_date), []).append(r)
        self.ruined = False
        self.rf = risk_free.sort_index() if risk_free is not None and len(risk_free) else None
        self._last_day = None
        self._retry_queue: list[tuple] = []
        self.audit: list[dict] = []
        self._day = Counter()          # per-day interest accrued / credited, financing charged
        self._day_reasons: set = set()
        self.cc_first = bool(cfg.get("covered_call_first", False))
        self.universe = universe
        self.universe_exits: list[dict] = []
        self._exit_pending: dict[str, dict] = {}
        self.conc_warn = float(cfg.get("assignment_concentration_warn_x_nav", 1.0))
        sl = cfg.get("put_stop_loss_pct")
        self.stop_pct = None if sl is None else float(sl)   # None: no price stop on the short-put phase
        self.decision_log: list[dict] = []
        self._assigned_today: list[dict] = []
        self._call_fails = Counter()   # consecutive failed call fills per symbol (>= max_retries: not eligible)
        self._deferred_entry = False   # put entry day postponed until pending covered calls are processed
        self._breach_today = False
        self._breach_deferred = False  # liquidation already postponed once in the current breach episode
        self._cash_after_calls = None

    # ---------------------------------------------------------------- helpers
    def wheel(self, sym) -> Wheel:
        return self.wheels.setdefault(sym, Wheel(sym))

    def _cash(self, t, sym, kind, amount):
        self.cash += amount
        self.ledger.append((t, sym, kind, amount))

    def _event(self, t, sym, kind, **kw):
        self.events.append({"date": t, "symbol": sym, "event": kind, **kw})

    def margin_of(self, w: Wheel) -> tuple[float, float]:
        """(unleveraged requirement, leveraged requirement) for one wheel, on current marks.

        The unleveraged requirement is the capital that fully secures the position:
            short put      K·q − put mark·q    (cash to take delivery, net of the loss already in NAV;
                                                ≈ S·q once deep ITM, so it is continuous across assignment)
            stock          S·shares
            covered call   − call mark·q       (the stock's value is capped at the strike)
        The leveraged requirement is that divided by L. At L = 1 utilisation can exceed 100% only if the
        account has borrowed, so a cash-secured wheel is never margin-called.
        """
        unlev = w.stock_mark * w.shares
        if w.option is not None:
            o = w.option
            unlev += (o.strike * o.qty if o.option_type == PUT else 0.0) - o.last_mark * o.qty
        unlev = max(unlev, 0.0)
        return unlev, unlev / self.L

    def nav(self) -> float:
        v = self.cash
        for w in self.wheels.values():
            v += w.shares * w.stock_mark
            if w.option is not None:
                v -= w.option.qty * w.option.last_mark
        return v

    def required(self) -> float:
        return sum(self.margin_of(w)[1] for w in self.wheels.values())

    def pending_reserved(self, cash: bool = False) -> float:
        """Reserve for put orders not yet filled: net of the estimated premium on the margin leg; the full K·q/L on
        the cash leg, where that premium has not arrived yet."""
        return sum((o.strike if cash else max(o.strike - o.est_premium, 0.0)) * o.lots * o.lot / self.L
                   for o in self.orders if o.kind == "PUT" and o.strike is not None)

    def margin_liquidity(self) -> float:
        """max_util × NAV − leveraged requirements of open positions − reserve for put orders not yet filled."""
        return self.max_util * self.nav() - self.required() - self.pending_reserved()

    def cash_liquidity(self) -> float:
        """Cash (incl. premiums, less stock bought on assignment) − Σ K·q/L over open puts − pending reserve.
        Cash carries no option liability, so the reserve is the full leveraged strike, not the margin (K − mark)·q/L.
        Assigned stock is paid in full, so borrowing to take delivery leaves nothing for new puts."""
        puts = sum(w.option.strike * w.option.qty / self.L for w in self.wheels.values()
                   if w.option is not None and w.option.option_type == PUT)
        return self.cash - puts - self.pending_reserved(cash=True)

    def available_liquidity(self) -> float:
        """Portfolio-level liquidity for new entries: the tighter of the margin and cash legs. Only entries are
        gated by it; the margin check (step 7) alone decides forced liquidation."""
        return min(self.margin_liquidity(), self.cash_liquidity())

    def _audit(self, t, sym, event, opening_cash, **kw):
        row = {"date": t, "symbol": sym, "event": event, "opening_cash": opening_cash, "premium_received": 0.0,
               "margin_requirement": 0.0, "leveraged_requirement": 0.0, "stock_cash_flow": 0.0,
               "transaction_costs": 0.0, "interest_income": 0.0, "borrowing_cost": 0.0, "closing_cash": self.cash,
               "available_liquidity": None, "required_liquidity": 0.0, "remaining_liquidity": None,
               "lots_considered": 0, "lots_executed": 0, "lots_rejected": 0, "rejection_reason": ""}
        row.update(kw)
        if row["available_liquidity"] is None:
            row["available_liquidity"] = self.available_liquidity()
        if row["remaining_liquidity"] is None:
            row["remaining_liquidity"] = row["available_liquidity"] - row["required_liquidity"]
        self.audit.append(row)

    def active_names(self) -> set:
        act = {s for s, w in self.wheels.items() if w.state != "CASH" or w.shares > 0}
        act |= {o.symbol for o in self.orders if o.kind in ("PUT", "CALL")}
        act |= {sym for sym, kind, _ in self._retry_queue if kind == "PUT"}
        return act

    def exposure(self) -> dict:
        """Stock at market, open-put notional K·q, covered-call notional K·q; gross = stock + put notional."""
        stock = put = call = 0.0
        by_name, by_sector = {}, {}
        for w in self.wheels.values():
            x = w.shares * w.stock_mark
            stock += x
            if w.option is not None:
                n = w.option.strike * w.option.qty
                if w.option.option_type == PUT:
                    put += n
                    x += n
                else:
                    call += n
            if x > 0:
                by_name[w.symbol] = x
                sec = self.sectors.get(w.symbol, "Unknown")
                by_sector[sec] = by_sector.get(sec, 0.0) + x
        return {"stock_value": stock, "put_exposure": put, "call_exposure": call, "gross": stock + put,
                "by_name": by_name, "by_sector": by_sector}

    def uncovered_usable_shares(self, t) -> dict:
        out = {}
        for s, w in self.wheels.items():
            if w.shares and w.usable_from is not None and t >= w.usable_from:
                covered = w.option.qty if w.option is not None and w.option.option_type == CALL else 0
                if w.shares - covered > 0:
                    out[s] = w.shares - covered
        return out

    def pending_call_orders(self) -> list:
        """Call orders still eligible to block new puts: a name whose call fills keep failing stops blocking."""
        cap = int(self.cfg["max_retries"])
        return [o for o in self.orders if o.kind == "CALL" and o.reason != "topup" and self._call_fails[o.symbol] <= cap]

    def rf_rate(self, t) -> float:
        if self.rf is None:
            return 0.0
        prior = self.rf[self.rf.index <= t.strftime("%Y-%m")]
        return float(prior.iloc[-1]) if len(prior) else float(self.rf.iloc[0])

    # ---------------------------------------------------------------- 1-3 pre-market events
    def _financing(self, t):
        if self._prev_day is not None and self.cash < 0 and self.fin_rate > 0:
            days = (t - self._prev_day).days
            interest = -self.cash * self.fin_rate * days / 365.0
            opening = self.cash
            self._cash(t, None, "financing", -interest)
            self.diag["financing_inr"] += interest
            self._day["financing_cost"] += interest
            self._audit(t, None, "financing", opening, borrowing_cost=interest)

    def _cash_interest(self, t):
        """Positive cash earns the risk-free rate of the month it is held in (ACT/365 on the previous close's cash),
        credited to cash every trading day so NAV carries the day's interest; it compounds daily."""
        if self.rf is None or self._prev_day is None or self.cash <= 0:
            return
        prev = self._prev_day
        interest = self.cash * self.rf_rate(prev) * (t - prev).days / 365.0
        if not interest:
            return
        opening = self.cash
        self._cash(t, None, "cash_interest", interest)
        self.diag["cash_interest_inr"] += interest
        self._day["cash_interest_accrued"] += interest
        self._day["cash_interest"] += interest
        self._audit(t, None, "cash_interest", opening, interest_income=interest)

    def _cover_ca_shortfall(self, t, w: Wheel, extra: int):
        """A lot that grows more than the shares (rights issue: NSE scales the lot, the renounced entitlement is
        paid in cash) leaves the covered call short `extra` shares. Buy them at the adjusted close so the call
        stays covered; price, slippage and charges go into the blended stock basis, so trade P&L still reconciles."""
        px = w.stock_mark * (1 + self.costs.equity_slippage_bps(t) / 1e4)
        cost = px * extra + self.costs.equity_buy(t, px * extra).total
        self._cash(t, w.symbol, "ca_cover_purchase", -cost)
        w.stock_price = (w.stock_price * w.shares + cost) / (w.shares + extra)
        w.shares += extra
        c = self.cycles.get(w.cycle_id)
        if c is not None:
            c.shares_ca_delta += extra
        self._event(t, w.symbol, "ca_cover_purchase", shares=extra, per_share=px)
        self.diag["ca_cover_purchases"] += 1

    def _corporate_actions(self, t):
        """NSE contract adjustment: strike -> a·K + b, option qty × lot ratio; held shares × share_multiplier,
        plus a cash distribution for the value that left the share (dividend / rights / demerger).

        A sourced dividend NSE did not adjust contracts for (contract_adjusted False) only pays cash on the shares
        held at the previous close: strikes, lots, pending orders and the economic basis are left alone."""
        for ev in self.ca.get(t, []):
            if not getattr(ev, "contract_adjusted", True):
                self._dividend(t, ev)
                continue
            w = self.wheels.get(ev.symbol)
            if w is None or (w.shares == 0 and w.option is None):
                for o in self.orders:
                    if o.symbol == ev.symbol and o.kind in ("PUT", "CALL"):
                        o.strike = None      # decided on the pre-adjustment chain: re-signal
                continue
            m = float(ev.share_multiplier)
            if w.shares:
                prev = w.stock_mark
                dist = distribution_per_old_share(ev, prev)
                if dist > 0:
                    self._cash(t, ev.symbol, "ca_distribution", dist * w.shares)
                w.stock_price = (w.stock_price - dist) / m     # distribution lands in the stock leg's P&L
                w.econ_basis = (w.econ_basis - dist) / m
                w.stock_mark = (prev - dist) / m
                old = w.shares
                w.shares = int(round(w.shares * m))
                c = self.cycles.get(w.cycle_id)
                if c is not None:
                    c.shares_ca_delta += w.shares - old
                if c is not None and m != 1.0:          # keep per-share references in post-event units
                    c.assign_fsp /= m
                    c.put_strike /= m
                    c.min_spot_while_held = None if c.min_spot_while_held is None else c.min_spot_while_held / m
                    c.first_call_spot = None if c.first_call_spot is None else c.first_call_spot / m
            if w.option is not None:
                o = w.option
                qm = float(ev.qty_multiplier)
                o.strike = round(ev.a * o.strike + ev.b, 4)
                o.qty = int(round(o.qty * qm))
                o.lot = int(round(o.lot * qm))
                o.entry_premium /= qm
                o.entry_quote /= qm
                o.last_mark /= qm
                if o.option_type == CALL and o.qty > w.shares:
                    self._cover_ca_shortfall(t, w, o.qty - w.shares)
            for o in self.orders:
                if o.symbol == ev.symbol and o.kind in ("PUT", "CALL"):
                    o.strike = None
            self._event(t, ev.symbol, f"corporate_action_{ev.action_type}", a=ev.a, b=ev.b, share_multiplier=m)
            self.diag["corporate_actions_applied"] += 1

    def _dividend(self, t, ev):
        w = self.wheels.get(ev.symbol)
        d = float(ev.distribution_per_share)
        if w is None or not w.shares or not d > 0:
            return
        self._cash(t, ev.symbol, "dividend", d * w.shares)
        w.stock_price -= d            # trade P&L includes the dividend, as for F&O-adjusted distributions
        w.stock_mark -= d             # ex-date drop is offset by the cash, so NAV does not jump
        self._event(t, ev.symbol, "dividend", per_share=d, shares=w.shares)
        self.diag["dividends_credited_inr"] += d * w.shares

    def _terminations(self, t):
        for w in list(self.wheels.values()):
            if (t, w.symbol) not in self.md.terminations:
                continue
            if w.option is None and w.shares == 0:
                continue
            spot = self.md.spot(t, w.symbol) or w.stock_mark
            if w.option is not None:
                self._settle_option(t, w, spot, reason="terminated")
            if w.shares:
                self._sell_stock(t, w, spot, status="terminated")
            self._event(t, w.symbol, "contract_termination", reason=self.md.terminations[(t, w.symbol)])
            self.diag["early_termination_events"] += 1
        self.orders = [o for o in self.orders if (t, o.symbol) not in self.md.terminations]

    # ---------------------------------------------------------------- 4 marks
    def _mark(self, t):
        for w in self.wheels.values():
            if w.shares:
                s = self.md.spot(t, w.symbol)
                if s is not None:
                    w.stock_mark = s
                c = self.cycles.get(w.cycle_id)
                if c is not None and s is not None:
                    c.min_spot_while_held = s if c.min_spot_while_held is None else min(c.min_spot_while_held, s)
            o = w.option
            if o is not None:
                q = self.md.quote(t, o.symbol, o.cmonth, o.option_type, o.strike)
                if q is not None and q.mark > 0:
                    o.last_mark, o.stale_days = q.mark, 0
                else:
                    o.stale_days += 1
                    self.diag["stale_marked_days"] += 1
                    if o.stale_days == 6:
                        self.diag["stale_mark_p1_flags"] += 1

    # ---------------------------------------------------------------- 5 expiry
    def _expiries(self, t):
        """Step 9a makes cash a binding, order-dependent resource at settlement, so the order is fixed and
        never left to dict insertion: calls first (a call-away releases K·q that can fund a later delivery),
        then puts by ascending delivery cost (which maximises the number of deliveries the cash supports),
        with the symbol as the deterministic tie-breaker."""
        due = []
        for w in list(self.wheels.values()):
            o = w.option
            if o is None or o.expiry != t:
                continue
            fsp = self.md.fsp.get((t, o.symbol))            # CM close: NSE's final settlement rule
            if fsp is None:
                fsp = self.md.fut_fsp.get((t, o.symbol)) or w.stock_mark
                self.diag["fsp_fallback_to_future_settle"] += 1
            due.append((w, o, fsp))
        calls = sorted((x for x in due if x[1].option_type == CALL), key=lambda x: x[0].symbol)
        # an OTM put costs nothing to settle: sorted ahead of the deliveries, which then see the full balance
        puts = sorted((x for x in due if x[1].option_type == PUT),
                      key=lambda x: (self.delivery_cost(t, x[1]) if x[2] < x[1].strike else -1.0, x[0].symbol))
        for w, _, fsp in calls + puts:
            self._settle_option(t, w, fsp, reason="expiry")

    def _assignment_warnings(self, t):
        """Re-measure the portfolio after the day's assignments and flag excessive stock accumulation."""
        if not self._assigned_today:
            return
        nav, ex = self.nav(), self.exposure()
        util = self.required() / nav if nav > 0 else float("inf")
        put_reserve = sum(w.option.strike * w.option.qty for w in self.wheels.values()
                          if w.option is not None and w.option.option_type == PUT) / self.L
        cl, ml = self.cash_liquidity(), self.margin_liquidity()
        top_sector = max(ex["by_sector"].items(), key=lambda kv: kv[1], default=(None, 0.0))
        reasons = []
        if ex["stock_value"] > max(nav - put_reserve, 0.0):
            reasons.append("stock_exceeds_cash_backed_capacity")
        if nav <= 0 or util >= self.max_util - 1e-9:
            reasons.append("margin_utilization_at_limit")
        # a 'negative_cash' reason used to live here; Step 9a makes that state unreachable
        if min(cl, ml) < 0:
            reasons.append("liquidity_constraint_breached")
        if len(self._assigned_today) >= 2 and nav > 0 and top_sector[1] / nav > self.conc_warn:
            reasons.append("simultaneous_assignments_over_concentrated")
        if not reasons:
            return
        for a in self._assigned_today:
            w = self.wheels[a["symbol"]]
            sec = self.sectors.get(a["symbol"], "Unknown")
            self._event(t, a["symbol"], "ASSIGNMENT_LIQUIDITY_WARNING", reasons="|".join(reasons),
                        assigned_lots=a["lots"], assigned_shares=a["shares"],
                        assigned_market_value=w.shares * w.stock_mark, cash_before=a["cash_before"],
                        cash_after=a["cash_after"], nav=nav, margin_utilization=util,
                        total_assigned_stock_exposure=ex["stock_value"], sector=sec,
                        sector_exposure=ex["by_sector"].get(sec, 0.0), top_sector=top_sector[0],
                        top_sector_exposure=top_sector[1], cash_liquidity=cl, margin_liquidity=ml,
                        remaining_liquidity=min(cl, ml))
            self.diag["assignment_liquidity_warnings"] += 1

    def _stop_losses(self, t):
        """Close any short put whose underlying has closed at or below its stop level, at that same close.

        Evaluated after expiries, so a put that reached its expiry day is settled normally rather than stopped.
        The fill is same-day by design (params.yaml `put_stop_loss_pct`): the engine's other exits decide at t
        and fill at t+1, but a stop that waits a day is not a stop. Cost: the breach is only observable once
        the close prints, so this assumes execution on the bell.
        """
        if self.stop_pct is None:
            return
        for sym, w in sorted(self.wheels.items()):
            o = w.option
            if w.state != "SHORT_PUT" or o is None or o.option_type != PUT:
                continue
            c = self.cycles[w.cycle_id]
            spot = self.md.spot(t, sym)
            if spot is None or c.stop_level <= 0 or spot > c.stop_level:
                continue
            q = self.md.quote(t, sym, o.cmonth, o.option_type, o.strike)
            if q is None or q.mark <= 0:
                self.diag["put_stop_loss_no_quote"] += 1
                self._event(t, sym, "PUT_STOP_LOSS_NO_QUOTE", strike=o.strike, spot=spot, stop_level=c.stop_level)
                continue                                  # not closed at an invented price; retried next close
            need = self.costs.option_fill(t, q.mark, "buy", self.md.month_end_spot(t, sym) or 0.0) or 0.0
            self._fund_same_day(t, need * o.qty * 1.05, exclude=sym)   # Step 9a: cash may not go negative
            cancelled = [x.kind for x in self.orders if x.symbol == sym]
            self.orders = [x for x in self.orders if x.symbol != sym]
            self._retry_queue = [x for x in self._retry_queue if x[0] != sym]
            self._buy_back(t, w, "stop_loss")
            c.stopped_out = True
            self._end_cycle(t, w, "stop_loss")
            self.diag["put_stop_losses"] += 1
            self._event(t, sym, "PUT_STOP_LOSS", strike=o.strike, spot=spot, stop_level=c.stop_level,
                        entry_spot=o.underlying_entry, cancelled_orders="|".join(cancelled))

    def _close_trade_row(self, o: OptionPos, exit_date, exit_price, underlying_exit, status, assigned,
                         option_pnl, costs):
        self.trades.append({
            "trade_id": o.trade_id, "cycle_id": o.cycle_id, "date": o.entry_date, "symbol": o.symbol,
            "sector": self.sectors.get(o.symbol, "Unknown"), "instrument": o.option_type,
            "expiry": o.expiry, "exit_date": exit_date, "strike": o.strike, "lots": o.lots, "lot_size": o.lot,
            "quantity": o.qty, "entry_quote": o.entry_quote, "entry_premium": o.entry_premium,
            "exit_price": exit_price, "status": status, "assigned_or_exercised": assigned,
            "underlying_entry": o.underlying_entry, "underlying_exit": underlying_exit,
            "premium_inr": o.entry_premium * o.qty, "option_pnl": option_pnl, "stock_pnl": 0.0,
            "transaction_costs": costs, "margin_requirement": o.margin_unlev,
            "leveraged_capital_requirement": o.margin_lev, "total_pnl": option_pnl - costs,
            "exit_reason": exit_reason(status),
        })

    def delivery_cost(self, t, o: OptionPos) -> float:
        """Step 9a: cash needed to take delivery on an assigned put — the strike value plus assignment costs."""
        return o.strike * o.qty + self.costs.put_assignment(t, o.strike, o.strike, o.qty).total

    def _can_take_delivery(self, t, o: OptionPos) -> bool:
        """Free cash is the raw balance: delivery is allowed whenever it leaves cash >= 0 on the day."""
        return self.cash >= self.delivery_cost(t, o)

    def _fund_same_day(self, t, need: float, exclude: str) -> None:
        """Raise `need` in cash by force-selling held stock on the settlement day itself, so cash never goes
        negative even intra-day. Deterministic order: wheels with no open call first (descending market
        value, ties by symbol), then wheels whose call must be bought back before their shares can be sold —
        selling covered shares without that would leave a naked call."""
        if self.cash >= need:
            return
        sold = 0
        for called in (False, True):       # uncalled stock first; only then disturb a written call
            pool = sorted((w for w in self.wheels.values()
                           if w.shares > 0 and w.symbol != exclude and (w.option is not None) == called),
                          key=lambda w: (-w.stock_mark * w.shares, w.symbol))
            for w in pool:
                if self.cash >= need:
                    break
                if w.option is not None:
                    self._buy_back(t, w, "cash_settlement_forced_call_buyback")
                    self.diag["cash_settlement_forced_call_buybacks"] += 1
                px = self.md.spot(t, w.symbol) or w.stock_mark
                self.diag["cash_settlement_forced_sale_value_inr"] += px * w.shares
                self._sell_stock(t, w, px, "cash_settlement_forced_sale")
                self.diag["cash_settlement_forced_sales"] += 1
                sold += 1
            if self.cash >= need:
                break
        if self.cash < need:
            raise InvariantError(f"{t.date()} {exclude}: cash settlement shortfall — need {need:.2f} to close "
                                 f"out the put, hold {self.cash:.2f} and nothing left to sell")
        self.diag["cash_settlement_shortfall"] += 1
        self._event(t, exclude, "cash_settlement_shortfall", need=need, forced_sales=sold)

    def _settle_option(self, t, w: Wheel, fsp: float, reason: str):
        """Expiry or termination settlement at `fsp` (physical delivery)."""
        o, c = w.option, self.cycles[w.option.cycle_id]
        itm = (fsp < o.strike) if o.option_type == PUT else (fsp > o.strike)
        intrinsic = max(o.strike - fsp, 0.0) if o.option_type == PUT else max(fsp - o.strike, 0.0)
        option_pnl = (o.entry_premium - intrinsic) * o.qty
        costs = o.entry_costs
        w.option = None
        if o.option_type == PUT:
            if itm and not self._can_take_delivery(t, o):
                # Step 9a: no free cash to pay for delivery, and borrowing is never permitted. The put is
                # closed out for its intrinsic value and the net loss is realised; no shares are received.
                opening = self.cash
                required = self.delivery_cost(t, o)
                closeout = intrinsic * o.qty
                sc = self.costs.option_buy(t, closeout).total
                self._fund_same_day(t, closeout + sc, exclude=o.symbol)
                self._cash(t, o.symbol, "put_cash_settled_intrinsic", -closeout)
                self._cash(t, o.symbol, "put_cash_settled_costs", -sc)
                costs += sc
                w.state = "CASH"
                c.cash_settled = True
                self.diag["assignments_cash_rejected"] += 1
                self.diag["forced_realisation_loss_inr"] += closeout + sc
                self.diag["cash_rejected_inr"] += required
                self._audit(t, o.symbol, "put_forced_realisation", opening,
                            stock_cash_flow=-closeout, transaction_costs=sc)
                self._event(t, o.symbol, "put_forced_realisation", strike=o.strike, fsp=fsp, qty=o.qty,
                            required_cash=required, free_cash=opening, shortfall=required - opening,
                            realised_loss=closeout + sc)
                status = "cash_settled_insufficient_cash"
            elif itm:
                opening = self.cash
                ac = self.costs.put_assignment(t, o.strike, fsp, o.qty).total
                self._cash(t, o.symbol, "put_assignment_pay_strike", -o.strike * o.qty)
                self._cash(t, o.symbol, "assignment_costs", -ac)
                costs += ac
                w.shares, w.stock_price, w.stock_date = o.qty, fsp, t
                w.stock_mark = self.md.spot(t, o.symbol) or fsp
                w.econ_basis = o.strike - o.entry_premium + (o.entry_costs + ac) / o.qty
                # the call is decided on usable_from's close and fills the next day: lag 1 = sold T+1
                lag = max(int(self.cfg["call_fill_lag_days"]) - 1, 0)
                w.usable_from = self.md.offset_day(t, lag) or t
                w.state = "STOCK"
                c.assigned, c.assign_fsp, c.assign_date = True, fsp, t
                c.shares_assigned += o.qty
                self.diag["assignments"] += 1          # kept: metrics.py reads it as n_assignments
                self.diag["assignments_physical"] += 1
                self.diag["assignment_costs_inr"] += ac
                self._audit(t, o.symbol, "put_assignment", opening, stock_cash_flow=-o.strike * o.qty,
                            transaction_costs=ac, margin_requirement=fsp * o.qty,
                            leveraged_requirement=self.margin_of(w)[1])
                self._assigned_today.append({"symbol": o.symbol, "lots": o.lots, "shares": o.qty,
                                             "cash_before": opening, "cash_after": self.cash})
                status = "assigned"
            else:
                w.state = "CASH"
                status = "expired_otm"
        else:
            if itm:
                opening = self.cash
                cc = self.costs.call_away(t, o.strike, fsp, o.qty).total
                self._cash(t, o.symbol, "call_away_receive_strike", o.strike * o.qty)
                self._cash(t, o.symbol, "call_away_costs", -cc)
                costs += cc
                self._stock_row(t, w, o.qty, fsp, 0.0, "called_away")
                w.shares -= o.qty
                c.shares_called_away += o.qty
                self.diag["calls_exercised"] += 1
                c.called_away = True
                self._audit(t, o.symbol, "call_away", opening, stock_cash_flow=o.strike * o.qty, transaction_costs=cc)
                status = "exercised"
            else:
                status = "expired_otm"
            w.state = "STOCK" if w.shares > 0 else "CASH"
        if reason == "terminated":
            status = f"terminated_{status}"
        c.option_pnl += option_pnl
        c.costs += costs
        # a cash-settled put is ITM but nothing was assigned or exercised: no shares changed hands
        self._close_trade_row(o, t, intrinsic, fsp, status,
                              itm and status != "cash_settled_insufficient_cash", option_pnl, costs)
        if w.state == "CASH":
            self._end_cycle(t, w, "put_cash_settled" if c.cash_settled else
                            "completed" if c.called_away else
                            ("put_expired_otm" if not c.assigned else "closed"))
        elif w.shares > 0 and o.option_type == CALL and itm:
            # shares the call did not cover (lot size / participation cap): the cycle is over, sell them next day
            c.residual_after_call_away = max(c.residual_after_call_away, w.shares)
            self.diag["residual_shares_after_call_away"] += w.shares
            self._event(t, w.symbol, "residual_shares_after_call_away", shares=w.shares, reason="sell_next_day")
            self.orders = [x for x in self.orders if x.symbol != w.symbol or x.kind in ("LIQUIDATE", "UNIVERSE_EXIT")]
            if not any(x.symbol == w.symbol for x in self.orders):
                self.orders.append(Order(w.symbol, "SELL_STOCK", t, reason="residual_after_call_away"))

    def _stock_row(self, t, w: Wheel, qty, exit_price, costs, status):
        pnl = (exit_price - w.stock_price) * qty
        c = self.cycles[w.cycle_id]
        c.stock_pnl += pnl
        c.costs += costs
        self.trades.append({
            "trade_id": self._next_tid(), "cycle_id": w.cycle_id, "date": w.stock_date, "symbol": w.symbol,
            "sector": self.sectors.get(w.symbol, "Unknown"), "instrument": "STOCK", "expiry": None,
            "exit_date": t, "strike": None, "lots": None, "lot_size": None, "quantity": qty,
            "entry_quote": None, "entry_premium": None, "exit_price": exit_price, "status": status,
            "assigned_or_exercised": status == "called_away", "underlying_entry": w.stock_price,
            "underlying_exit": exit_price, "premium_inr": 0.0, "option_pnl": 0.0, "stock_pnl": pnl,
            "transaction_costs": costs, "margin_requirement": w.stock_price * qty,
            "leveraged_capital_requirement": w.stock_price * qty / self.L, "total_pnl": pnl - costs,
            "exit_reason": exit_reason(status),
        })

    def _sell_stock(self, t, w: Wheel, price_ref: float, status: str):
        bps = self.costs.equity_slippage_bps(t) / 1e4
        px = price_ref * (1 - bps)
        qty = w.shares
        sc = self.costs.equity_sale(t, px * qty).total
        self._cash(t, w.symbol, "equity_sale", px * qty)
        self._cash(t, w.symbol, "equity_sale_costs", -sc)
        self._stock_row(t, w, qty, px, sc, status)
        self.cycles[w.cycle_id].shares_sold += qty
        w.shares = 0
        w.state = "CASH" if w.option is None else w.state
        if w.option is None:
            self._end_cycle(t, w, status)

    def _buy_back(self, t, w: Wheel, status: str):
        o = w.option
        c = self.cycles[o.cycle_id]
        q = self.md.quote(t, o.symbol, o.cmonth, o.option_type, o.strike)
        ref = q.mark if (q is not None and q.mark > 0) else o.last_mark
        px = self.costs.option_fill(t, ref, "buy", self.md.month_end_spot(t, o.symbol) or 0.0)
        bc = self.costs.option_buy(t, px * o.qty).total
        self._cash(t, o.symbol, "option_buyback", -px * o.qty)
        self._cash(t, o.symbol, "option_buyback_costs", -bc)
        option_pnl = (o.entry_premium - px) * o.qty
        c.option_pnl += option_pnl
        c.costs += o.entry_costs + bc
        self._close_trade_row(o, t, px, self.md.spot(t, o.symbol) or w.stock_mark, status, False,
                              option_pnl, o.entry_costs + bc)
        w.option = None
        w.state = "STOCK" if w.shares else "CASH"

    def _end_cycle(self, t, w: Wheel, status: str):
        c = self.cycles.get(w.cycle_id)
        if w.shares or w.option is not None or (c is not None and c.shares_open):
            raise InvariantError(f"{t.date()} {w.symbol}: cycle {w.cycle_id} ends '{status}' with {w.shares} shares held, "
                                 f"ledger {c.shares_open if c else 'n/a'}, option {w.option is not None}")
        if c is not None and c.end is None:
            c.end, c.status = t, status
        w.cycle_id, w.state, w.usable_from, w.econ_basis = None, "CASH", None, 0.0
        self._call_fails.pop(w.symbol, None)

    def _next_tid(self):
        self._tid += 1
        return self._tid

    # ---------------------------------------------------------------- 6 fills
    def _fill_orders(self, t) -> list[Order]:
        todo = [o for o in self.orders if o.decided_on < t]
        self.orders = [o for o in self.orders if o.decided_on >= t]
        if self.cc_first:              # liquidations and stock sales, then every covered call, then puts
            rank = {"LIQUIDATE": 0, "UNIVERSE_EXIT": 0, "SELL_STOCK": 1, "CALL": 2, "PUT": 3}
            todo.sort(key=lambda o: rank[o.kind])
        failed = []
        self._cash_after_calls = None
        for od in todo:
            if od.kind == "PUT" and self._cash_after_calls is None:
                self._cash_after_calls = self.cash
            w = self.wheel(od.symbol)
            if od.kind == "LIQUIDATE":
                if w.option is not None:
                    self._buy_back(t, w, "margin_call_buyback")
                if w.shares:
                    self._sell_stock(t, w, self.md.spot(t, w.symbol) or w.stock_mark, "margin_call_liquidation")
                self.diag["margin_call_liquidations"] += 1
                self._event(t, od.symbol, "margin_call_liquidation", reason=od.reason)
                continue
            if od.kind == "UNIVERSE_EXIT":
                self._fill_universe_exit(t, od, w)
                continue
            if od.kind == "SELL_STOCK":
                if w.shares and w.option is None:
                    self._sell_stock(t, w, self.md.spot(t, w.symbol) or w.stock_mark, od.reason)
                elif w.shares:                   # never drop a stock sale silently: keep it until it can fill
                    self._event(t, od.symbol, "stock_sale_deferred", reason=od.reason, shares=w.shares)
                    self.orders.append(Order(od.symbol, "SELL_STOCK", t, reason=od.reason))
                continue
            ok, why = self._fill_option(t, od, w)
            if od.kind == "CALL":
                if ok:
                    self._call_fails.pop(od.symbol, None)
                else:
                    self._call_fails[od.symbol] += 1
                    self._event(t, od.symbol, "covered_call_fill_failed", reason=why, strike=od.strike, lots=od.lots)
            if not ok:
                self.diag[f"fill_fail_{why}"] += 1
                failed.append(od)
        if self._cash_after_calls is None:
            self._cash_after_calls = self.cash
        return failed

    def in_universe(self, sym, t) -> bool:
        return True if self.universe is None else bool(self.universe(sym, t))

    @staticmethod
    def _position(w: Wheel) -> str:
        parts = [f"{w.shares} shares"] if w.shares else []
        if w.option is not None:
            parts.append(f"short {w.option.option_type} K={w.option.strike:g} x{w.option.qty} exp {w.option.expiry.date()}")
        return "; ".join(parts) or "flat"

    def _schedule_universe_exits(self, t):
        """Decided at t's close for every position whose symbol is not in the universe on t; filled t+1."""
        if self.universe is None:
            return
        for sym, w in sorted(self.wheels.items()):
            if (w.option is None and not w.shares) or self.in_universe(sym, t):
                continue
            if any(o.symbol == sym and o.kind in ("UNIVERSE_EXIT", "LIQUIDATE") for o in self.orders):
                continue
            cancelled = [o.kind for o in self.orders if o.symbol == sym]
            self.orders = [o for o in self.orders if o.symbol != sym]
            self._retry_queue = [x for x in self._retry_queue if x[0] != sym]
            self.orders.append(Order(sym, "UNIVERSE_EXIT", t, reason="UNIVERSE_REMOVAL"))
            self._exit_pending[sym] = {"symbol": sym, "exit_reason": "UNIVERSE_REMOVAL", "removal_date": t,
                                       "decision_date": t, "position_before_exit": self._position(w),
                                       "cancelled_orders": "|".join(cancelled)}
            self.diag["universe_exits_scheduled"] += 1
            self._event(t, sym, "UNIVERSE_EXIT_SCHEDULED", position=self._position(w), cancelled_orders="|".join(cancelled))

    def _fill_universe_exit(self, t, od: Order, w: Wheel):
        """Buy back the open option at the day's quote and sell held shares at the CM close. A leg whose market data is
        missing is not closed at an invented price: the order stays open, flagged, and is retried next day."""
        rec = self._exit_pending.get(od.symbol) or {"symbol": od.symbol, "exit_reason": "UNIVERSE_REMOVAL",
                                                    "removal_date": od.decided_on, "decision_date": od.decided_on,
                                                    "position_before_exit": self._position(w), "cancelled_orders": ""}
        cash0, n_trades = self.cash, len(self.trades)
        actions, prices, missing = [], [], []
        if w.option is not None:
            o = w.option
            q = self.md.quote(t, o.symbol, o.cmonth, o.option_type, o.strike)
            if q is None or q.mark <= 0:
                missing.append(f"no quote for {o.option_type} K={o.strike:g}")
            else:
                self._buy_back(t, w, "universe_removal_buyback")
                actions.append(f"buy back {o.option_type}")
                prices.append(self.trades[-1]["exit_price"])
        if w.shares:
            spot = self.md.spot(t, w.symbol)
            if spot is None:
                missing.append("no CM close for shares")
            elif w.option is not None:
                missing.append("shares kept: covering call could not be bought back")
            else:
                self._sell_stock(t, w, spot, "universe_removal_sale")
                actions.append("sell shares")
                prices.append(self.trades[-1]["exit_price"])
        if w.option is None and not w.shares and w.cycle_id is not None:
            self._end_cycle(t, w, "universe_removal")
        new = self.trades[n_trades:]
        rec.update({"fill_date": t, "exit_action": " + ".join(actions) or "none",
                    "exit_price": "|".join(f"{p:.4f}" for p in prices),
                    "realized_pnl": sum(x["option_pnl"] + x["stock_pnl"] for x in new),
                    "transaction_cost": sum(x["transaction_costs"] for x in new),   # option rows include entry costs
                    "cash_before": cash0, "cash_after": self.cash,
                    "position_after_exit": self._position(w), "nav_after": self.nav(),
                    "available_liquidity_after": self.available_liquidity(),
                    "margin_utilization_after": self.required() / self.nav() if self.nav() > 0 else float("inf"),
                    "residual": bool(w.option is not None or w.shares), "data_unavailable": "|".join(missing)})
        self.universe_exits.append(dict(rec))
        if missing:
            self.diag["universe_exit_data_unavailable"] += 1
            self._event(t, od.symbol, "UNIVERSE_EXIT_DATA_UNAVAILABLE", reason="|".join(missing),
                        position=self._position(w))
            self.orders.append(Order(od.symbol, "UNIVERSE_EXIT", t, reason="UNIVERSE_REMOVAL_RETRY"))
        else:
            self._exit_pending.pop(od.symbol, None)
            self.diag["universe_exits_cleared"] += 1
            self._event(t, od.symbol, "UNIVERSE_EXIT_CLEARED", action=rec["exit_action"], pnl=rec["realized_pnl"])

    def _fill_option(self, t, od: Order, w: Wheel) -> tuple[bool, str]:
        if od.strike is None:
            return False, "adjusted_by_corporate_action"
        topup = od.kind == "CALL" and od.reason == "topup"
        if w.option is not None and not (topup and w.option.option_type == CALL and w.option.cmonth == od.cmonth
                                         and w.option.strike == od.strike):
            return False, "already_has_option"
        if topup and w.option is None:
            return False, "topup_call_gone"
        q = self.md.quote(t, od.symbol, od.cmonth, PUT if od.kind == "PUT" else CALL, od.strike)
        if q is None or q.close <= 0:
            return False, "no_close"
        if q.contracts <= 0:
            return False, "no_trades_t1"
        lot = self.md.lot_size(t, od.symbol, od.cmonth) or od.lot
        lots = min(od.lots, math.floor(self.cfg["participation_cap"] * q.contracts))
        spot = self.md.spot(t, od.symbol) or 0.0
        px = self.costs.option_fill(t, q.close, "sell", self.md.month_end_spot(t, od.symbol) or spot)
        if px is None:
            return False, "slippage_below_tick"
        opening = self.cash
        free = self.available_liquidity()
        considered, per_lot_req = lots, 0.0
        if od.kind == "CALL":
            if w.usable_from is None or t < w.usable_from:
                return False, "shares_not_usable"
            # size on the fill-day lot (an NSE lot revision can take effect overnight) and on shares not yet covered;
            # never more calls than shares held, so no extra margin
            covered = w.option.qty if topup else 0
            if topup and w.option.lot != lot:
                return False, "topup_lot_mismatch"
            lots = min(math.floor(self.cfg["participation_cap"] * q.contracts), (w.shares - covered) // lot)
        elif lots >= 1:
            # size on real-time portfolio liquidity, crediting the premium this sale brings in; then confirm
            # with exact (non-linear) costs that both liquidity legs stay >= 0 after the trade
            mfree, cfree = self.margin_liquidity(), self.cash_liquidity()

            def after(n):
                cost = self.costs.option_sale(t, px * n * lot).total
                lock = (od.strike - q.mark) * n * lot / self.L
                return min(mfree + self.max_util * ((px - q.mark) * n * lot - cost) - lock,
                           cfree + px * n * lot - cost - od.strike * n * lot / self.L)
            est = self.costs.option_sale(t, px * lots * lot).total / lots
            lots = max_affordable_lots(mfree, od.strike, lot, self.L, px, q.mark, est, self.max_util, lots,
                                       cash_free=cfree)
            while lots > 0 and after(lots) < 0:
                lots -= 1
            per_lot_req = (free - after(1)) if considered >= 1 else 0.0
        if lots < 1:
            reason = "insufficient_liquidity" if od.kind == "PUT" and considered >= 1 else "participation_or_shares"
            if od.kind == "PUT":
                self._day["put_lots_attempted"] += considered
                self._day["put_lots_rejected"] += considered
                self._day_reasons.add(f"put_fill_{reason}")
            self._audit(t, od.symbol, f"{od.kind.lower()}_fill_rejected", opening, available_liquidity=free,
                        required_liquidity=per_lot_req, lots_considered=considered, lots_rejected=considered,
                        rejection_reason=reason)
            return False, "participation_or_margin"
        qty = lots * lot
        oc = self.costs.option_sale(t, px * qty).total
        if px * qty - oc <= 0:
            return False, "net_premium_nonpositive"
        if od.kind == "PUT":
            self._cid += 1
            w.cycle_id = self._cid
            self.cycles[self._cid] = Cycle(self._cid, od.symbol, t, put_strike=od.strike)
        c = self.cycles[w.cycle_id]
        if topup:                          # add to the open call: same contract and strike, one position
            o = w.option
            o.entry_premium = (o.entry_premium * o.qty + px * qty) / (o.qty + qty)
            o.entry_quote = (o.entry_quote * o.qty + q.close * qty) / (o.qty + qty)
            o.entry_costs += oc
            o.lots += lots
            o.qty += qty
            self._cash(t, od.symbol, "premium", px * qty)
            self._cash(t, od.symbol, "option_sale_costs", -oc)
            c.premium += px * qty
            c.n_call_topups += 1
            self.diag["call_topups"] += 1
            self._day["call_lots_sold"] += lots
            self._day["call_premium"] += px * qty
            self._event(t, od.symbol, "covered_call_topup", shares=w.shares, lots=lots, strike=od.strike,
                        reason=f"added {qty}, covered {o.qty}, uncovered {w.shares - o.qty}")
            self._audit(t, od.symbol, "option_sale", opening, premium_received=px * qty, margin_requirement=0.0,
                        leveraged_requirement=self.margin_of(w)[1], transaction_costs=oc, available_liquidity=free,
                        required_liquidity=0.0, lots_considered=considered, lots_executed=lots,
                        lots_rejected=considered - lots, rejection_reason="")
            return True, ""
        unlev = od.strike * qty if od.kind == "PUT" else 0.0
        o = OptionPos(self._next_tid(), w.cycle_id, od.symbol, PUT if od.kind == "PUT" else CALL, od.cmonth,
                      self.md.expiry_of[od.cmonth], od.strike, lots, lot, qty, t, px, q.close, oc, spot,
                      unlev, unlev / self.L, q.mark)
        self._cash(t, od.symbol, "premium", px * qty)
        self._cash(t, od.symbol, "option_sale_costs", -oc)
        w.option = o
        c.premium += px * qty
        if od.kind == "PUT":
            c.n_puts += 1
            w.state = "SHORT_PUT"
            if self.stop_pct is not None:
                c.stop_level = spot * (1 - self.stop_pct)    # reference: the underlying close on the sale day
        else:
            c.n_calls += 1
            if w.shares - qty > 0:
                c.max_uncovered_at_call = max(c.max_uncovered_at_call, w.shares - qty)
                self._event(t, od.symbol, "covered_call_leftover_shares", shares=w.shares, lots=lots, strike=od.strike,
                            reason=f"covered {qty}, uncovered {w.shares - qty}")
            if c.first_call_spot is None:
                c.first_call_spot = spot
            w.state = "STOCK_CALL"
        self.diag["retries_used"] += od.retries
        if od.kind == "PUT":
            self._day["put_lots_attempted"] += considered
            self._day["put_lots_executed"] += lots
            self._day["put_lots_rejected"] += considered - lots
        else:
            self._day["call_lots_sold"] += lots
            self._day["call_premium"] += px * qty
        required = free - self.available_liquidity() if od.kind == "PUT" else 0.0
        self._audit(t, od.symbol, "option_sale", opening, premium_received=px * qty, margin_requirement=unlev,
                    leveraged_requirement=self.margin_of(w)[1], transaction_costs=oc, available_liquidity=free,
                    required_liquidity=required, lots_considered=considered, lots_executed=lots,
                    lots_rejected=considered - lots,
                    rejection_reason="insufficient_liquidity" if lots < considered and od.kind == "PUT" else "")
        return True, ""

    # ---------------------------------------------------------------- 7 margin
    def _margin_check(self, t):
        nav = self.nav()
        if nav <= 0:
            if not self.ruined:
                self._event(t, None, "ruin_nav_nonpositive", nav=nav)
            self.ruined = True
        req = self.required()
        if nav > 0 and req <= self.max_util * nav + 1e-6:
            self._breach_deferred = False
            return
        if not any(w.option or w.shares for w in self.wheels.values()):
            return                                   # nothing left to liquidate (account wiped out)
        self.diag["margin_breach_days"] += 1
        self._breach_today = True
        # drop pending entries first, then liquidate the largest requirements until back under the limit
        self.orders = [o for o in self.orders if o.kind != "PUT"]
        if self.cc_first and nav > 0 and not self._breach_deferred:
            liquidating = {o.symbol for o in self.orders if o.kind == "LIQUIDATE"}
            uncovered = {s: q for s, q in self.uncovered_usable_shares(t).items()
                         if s not in liquidating and self.wheels[s].option is None}
            if uncovered:
                # covered calls on those shares are decided tonight and sold tomorrow before liquidation is decided
                self._breach_deferred = True
                self.diag["margin_breach_calls_first"] += 1
                self._event(t, None, "margin_breach_calls_first", nav=nav, required=req,
                            uncovered_names="|".join(sorted(uncovered)), uncovered_shares=sum(uncovered.values()))
                return
        self._schedule_liquidation(t, nav, req)

    def _schedule_liquidation(self, t, nav, req):
        already = {o.symbol for o in self.orders if o.kind == "LIQUIDATE"}
        ranked = sorted((w for w in self.wheels.values() if (w.option or w.shares) and w.symbol not in already),
                        key=lambda w: -self.margin_of(w)[1])
        for w in ranked:
            if nav > 0 and req <= self.max_util * nav:
                break
            self.orders = [o for o in self.orders if o.symbol != w.symbol]
            self.orders.append(Order(w.symbol, "LIQUIDATE", t,
                                     reason=f"utilisation {req / nav if nav > 0 else float('inf'):.2f}"))
            req -= self.margin_of(w)[1]
        self._event(t, None, "margin_call", nav=nav, required=self.required())

    # ---------------------------------------------------------------- 8 decisions
    def _liquid_chain(self, t, sym, cm, typ, lot):
        ch = self.md.chain(t, sym, cm, typ)
        full = ch.strike.to_numpy()
        ok = ch[(ch.close > 0) & (ch.contracts >= self.cfg["min_contracts_t"])
                & (ch.oi_shares / lot >= self.cfg["min_oi_contracts_t"])]
        return ok, full

    def _distance_ok(self, full, target, strike, spot) -> bool:
        below, above = full[full <= target], full[full >= target]
        interval = (above.min() - below.max()) if len(below) and len(above) else 0.0
        return abs(strike - target) <= max(self.cfg["max_distance_min_pct"] * spot, interval) + 1e-9

    def _contract_for(self, t, sym):
        nxt = self.md.next_day(t)
        if nxt is None:
            return None, None
        cm = self.md.contract_after(nxt)
        if cm is None:
            return None, None
        term = self.md.termination_by_symbol.get(sym)
        if term is not None and term[0] <= t < term[1] <= self.md.expiry_of[cm]:    # only once announced
            self.diag["skip_termination_announced"] += 1
            return None, None
        return nxt, cm

    def decide_put(self, t, sym, retries=0) -> Order | None:
        if not self.in_universe(sym, t):
            self.diag["skip_not_in_universe"] += 1
            return None
        spot = self.md.spot(t, sym)
        if spot is None or not self.md.is_fo_listed(t, sym):
            self.diag["skip_no_spot_or_not_fo"] += 1
            return None
        nxt, cm = self._contract_for(t, sym)
        if cm is None:
            return None
        lot = self.md.lot_size(t, sym, cm)
        if lot is None:
            self.diag["lot_unresolved_skips"] += 1
            return None
        ok, full = self._liquid_chain(t, sym, cm, PUT, lot)
        target = spot * (1 - self.cfg["put_strike_filter_pct"])
        cands = ok[ok.strike <= target + 1e-9]
        if cands.empty:
            self.diag["cycles_skipped_no_eligible_strike"] += 1
            return None
        k = float(cands.strike.max())
        prem = float(cands.loc[cands.strike.idxmax(), "close"])
        if not self._distance_ok(full, target, k, spot):
            self.diag["cycles_skipped_distance_cap"] += 1
            return None
        nav = self.nav()
        tgt_notional = self.L * nav / self.N
        lots = math.floor(tgt_notional / (k * lot))
        if lots == 0:
            if k * lot <= self.cfg["max_reserve_multiple"] * tgt_notional:
                lots = 1
            else:
                self.diag["cycles_skipped_lot_too_large"] += 1
                return None
        free = self.available_liquidity()
        considered = lots
        est_cost = self.costs.option_sale(t, prem * lots * lot).total / lots
        lots = max_affordable_lots(self.margin_liquidity(), k, lot, self.L, prem, prem, est_cost, self.max_util,
                                   lots, cash_free=self.cash_liquidity())
        per_lot = (k - prem) * lot / self.L + self.max_util * est_cost
        self._audit(t, sym, "put_decision", self.cash, premium_received=prem * lots * lot,
                    margin_requirement=k * lots * lot, leveraged_requirement=(k - prem) * lots * lot / self.L,
                    available_liquidity=free, required_liquidity=per_lot * max(lots, 1), lots_considered=considered,
                    lots_executed=lots, lots_rejected=considered - lots,
                    rejection_reason="insufficient_liquidity" if lots < considered else "")
        if lots < 1:
            self.diag["cycles_skipped_insufficient_margin"] += 1
            return None
        return Order(sym, "PUT", t, cm, k, lots, lot, retries, est_premium=prem)

    def decide_call_topup(self, t, sym, retries=0) -> Order | None:
        """A call is open but covers fewer whole lots than the shares allow (participation cap, lot revision): sell more
        of the same contract and strike, provided that strike still meets today's call target (5% OTM and the
        economic-basis floor) and is liquid. Shares below one lot cannot be covered and stay with the exit rules."""
        w = self.wheels[sym]
        o = w.option
        if o is None or o.option_type != CALL or not self.in_universe(sym, t) or not self.md.is_fo_listed(t, sym):
            return None
        nxt = self.md.offset_day(t, 1)
        if nxt is None or nxt > o.expiry:
            return None
        lot = self.md.lot_size(t, sym, o.cmonth)
        spot = self.md.spot(t, sym)
        if lot is None or spot is None or lot != o.lot or (w.shares - o.qty) // lot < 1:
            return None
        target = spot * (1 + self.cfg["call_strike_filter_pct"])
        if self.cfg["call_floor_economic_basis"]:
            target = max(target, w.econ_basis)
        if o.strike < target - 1e-9:
            self.diag["call_topups_skipped_below_target"] += 1
            return None
        ok, _ = self._liquid_chain(t, sym, o.cmonth, CALL, lot)
        if not (ok.strike == o.strike).any():
            self.diag["call_topups_skipped_illiquid"] += 1
            return None
        return Order(sym, "CALL", t, o.cmonth, o.strike, (w.shares - o.qty) // lot, lot, retries, reason="topup")

    def decide_call(self, t, sym, retries=0) -> Order | None:
        w = self.wheels[sym]
        if w.option is not None:
            return self.decide_call_topup(t, sym, retries)
        if not self.in_universe(sym, t):
            self.diag["calls_skipped_not_in_universe"] += 1       # the position is being wound down instead
            return None
        spot = self.md.spot(t, sym)
        if spot is None:
            return None
        if not self.md.is_fo_listed(t, sym):
            self.diag["fo_exit_stock_sales"] += 1
            return Order(sym, "SELL_STOCK", t, reason="fo_exit")
        nxt, cm = self._contract_for(t, sym)
        if cm is None:
            return None
        lot = self.md.lot_size(t, sym, cm)
        if lot is None:
            self.diag["lot_unresolved_skips"] += 1
            return None
        lots = w.shares // lot
        if lots < 1:
            self.diag["uncoverable_odd_lot_sales"] += 1
            return Order(sym, "SELL_STOCK", t, reason="uncoverable_odd_lot")
        ok, full = self._liquid_chain(t, sym, cm, CALL, lot)
        target = spot * (1 + self.cfg["call_strike_filter_pct"])
        if self.cfg["call_floor_economic_basis"]:
            target = max(target, w.econ_basis)
        cands = ok[ok.strike >= target - 1e-9]
        if cands.empty:
            self.diag["calls_skipped_no_eligible_strike"] += 1
            return None
        k = float(cands.strike.min())
        if not self._distance_ok(full, target, k, spot):
            self.diag["calls_skipped_distance_cap"] += 1
            return None
        return Order(sym, "CALL", t, cm, k, lots, lot, retries)

    def _decisions(self, t, failed: list[Order]):
        self._schedule_universe_exits(t)
        failed = [o for o in failed if self.in_universe(o.symbol, t)]
        if self.ruined:
            return
        if self.cc_first:
            return self._decisions_calls_first(t, failed)
        busy = {o.symbol for o in self.orders}
        # retries: fills that failed today, plus re-signals that found no strike yesterday. Each re-signal uses
        # today's close; a re-signal that finds nothing still counts (rulebook Step 8.5), max `max_retries`.
        pending, self._retry_queue = [(o.symbol, o.kind, o.retries) for o in failed] + self._retry_queue, []
        for sym, kind, retries in pending:
            if retries >= self.cfg["max_retries"]:
                self.diag["entries_missed"] += 1
                continue
            if kind == "PUT" and (self.wheels.get(sym) is None or self.wheels[sym].state != "CASH"):
                continue
            new = self.decide_put(t, sym, retries + 1) if kind == "PUT" else self.decide_call(t, sym, retries + 1)
            if new is not None:
                self.orders.append(new)
                busy.add(sym)
            else:
                self.diag["retry_found_no_strike"] += 1
                if kind == "PUT":
                    self._retry_queue.append((sym, kind, retries + 1))
        # covered calls on usable shares
        for sym, w in sorted(self.wheels.items()):
            if w.shares and w.option is not None and sym not in busy:
                od = self.decide_call_topup(t, sym)
                if od is not None:
                    self.orders.append(od)
                    busy.add(sym)
                continue
            if w.shares and w.option is None and sym not in busy and w.usable_from is not None and t >= w.usable_from:
                od = self.decide_call(t, sym)
                if od is not None:
                    self.orders.append(od)
                    busy.add(sym)
        # new cash-secured puts on entry days
        if self.md.is_expiry(t) or t == pd.Timestamp(self.cfg["first_signal_date"]):
            ranked = self.selections(t)
            active = self.active_names()
            slots = self.N - len(active)
            for sym in ranked:
                if slots <= 0:
                    break
                if sym in active:
                    continue
                od = self.decide_put(t, sym)
                if od is not None:
                    self.orders.append(od)
                    slots -= 1

    def _put_gate_reason(self, call_orders) -> str | None:
        if call_orders:
            return "uncovered_assigned_shares_calls_pending"
        if self._breach_today:
            return "margin_breach"
        nav = self.nav()
        if nav <= 0:
            return "nav_nonpositive"
        if self.required() >= self.max_util * nav - 1e-6:
            return "margin_utilization_at_limit"          # also the leverage constraint: requirement = notional / L
        if self.cash_liquidity() <= 0:
            return "cash_liquidity"
        if self.margin_liquidity() <= 0:
            return "margin_liquidity"
        return None                                        # no sector limit is configured

    def _decisions_calls_first(self, t, failed: list[Order]):
        """Covered calls on every usable assigned share first; new puts only once no eligible call is pending and
        the portfolio constraints pass."""
        busy = {o.symbol for o in self.orders}
        pending, self._retry_queue = [(o.symbol, o.kind, o.retries) for o in failed] + self._retry_queue, []
        put_retries = [x for x in pending if x[1] == "PUT"]
        for sym, kind, retries in pending:
            if kind != "CALL":
                continue
            if retries >= self.cfg["max_retries"]:
                self.diag["entries_missed"] += 1
                continue
            new = self.decide_call(t, sym, retries + 1)
            if new is not None:
                self.orders.append(new)
                busy.add(sym)
            else:
                self.diag["retry_found_no_strike"] += 1
        for sym, w in sorted(self.wheels.items()):
            if w.shares and w.option is not None and sym not in busy:
                od = self.decide_call_topup(t, sym)
                if od is not None:
                    self.orders.append(od)
                    busy.add(sym)
                continue
            if w.shares and w.option is None and sym not in busy and w.usable_from is not None and t >= w.usable_from:
                before = Counter(self.diag)
                od = self.decide_call(t, sym)
                if od is not None:
                    self.orders.append(od)
                    busy.add(sym)
                else:
                    why = [k for k in self.diag if self.diag[k] != before.get(k, 0)]
                    self._event(t, sym, "covered_call_not_available", reason="|".join(why) or "no_quote",
                                shares=w.shares)
        call_orders = self.pending_call_orders()
        if self._breach_today and self._breach_deferred and not call_orders and \
                not any(o.kind == "LIQUIDATE" for o in self.orders):
            # liquidation was held back for covered calls, but none can be written: run it now
            self._event(t, None, "margin_breach_no_call_available")
            self._schedule_liquidation(t, self.nav(), self.required())
        self._day["call_opportunities"] = len(call_orders)
        self._day["call_lots_ordered"] = sum(o.lots for o in call_orders)
        self._day["uncovered_usable_shares"] = sum(self.uncovered_usable_shares(t).values())
        self._day["new_put_capacity"] = self.available_liquidity()

        entry_day = self.md.is_expiry(t) or t == pd.Timestamp(self.cfg["first_signal_date"]) or self._deferred_entry
        reason = self._put_gate_reason(call_orders)
        if reason is not None:
            if entry_day or put_retries:
                self._day_reasons.add(f"SKIP_NEW_PUT:{reason}")
                self._event(t, None, "SKIP_NEW_PUT", reason=reason, entry_day=bool(entry_day),
                            put_retries=len(put_retries), pending_calls=len(call_orders))
            if reason == "uncovered_assigned_shares_calls_pending":
                if entry_day:
                    self._deferred_entry = True
                    self.diag["put_entries_deferred_for_calls"] += 1
                self._retry_queue.extend(put_retries)        # waiting for calls does not use up a retry
            else:
                for sym, kind, retries in put_retries:
                    if retries >= self.cfg["max_retries"]:
                        self.diag["entries_missed"] += 1
                    else:
                        self._retry_queue.append((sym, kind, retries + 1))
                if entry_day:
                    self._deferred_entry = False
                    n = len([s for s in self.selections(t) if s not in self.active_names()][:max(self.N - len(
                        self.active_names()), 0)])
                    self.diag[f"skip_new_put_{reason}"] += n
            return
        for sym, kind, retries in put_retries:
            if retries >= self.cfg["max_retries"]:
                self.diag["entries_missed"] += 1
                continue
            if self.wheels.get(sym) is None or self.wheels[sym].state != "CASH":
                continue
            new = self.decide_put(t, sym, retries + 1)
            if new is not None:
                self.orders.append(new)
                busy.add(sym)
            else:
                self.diag["retry_found_no_strike"] += 1
                self._retry_queue.append((sym, kind, retries + 1))
        if entry_day:
            self._deferred_entry = False
            ranked = self.selections(t)
            active = self.active_names()
            slots = self.N - len(active)
            if slots <= 0:
                self._day_reasons.add("SKIP_NEW_PUT:position_count")
                self._event(t, None, "SKIP_NEW_PUT", reason="position_count", entry_day=True, put_retries=0,
                            pending_calls=0)
            for sym in ranked:
                if slots <= 0:
                    break
                if sym in active:
                    continue
                od = self.decide_put(t, sym)
                if od is not None:
                    self.orders.append(od)
                    slots -= 1
        self._day["put_lots_decided"] = sum(o.lots for o in self.orders if o.kind == "PUT" and o.decided_on == t)

    # ---------------------------------------------------------------- 9 reconcile / record
    def _record(self, t):
        nav = self.nav()
        unlev = lev = stock_val = liab = put_notional = 0.0
        n_put = n_call = n_stock = 0
        for w in self.wheels.values():
            u, l = self.margin_of(w)
            unlev += u
            lev += l
            stock_val += w.shares * w.stock_mark
            if w.shares:
                n_stock += 1
            if w.option is not None:
                liab += w.option.qty * w.option.last_mark
                if w.option.option_type == PUT:
                    n_put += 1
                    put_notional += w.option.strike * w.option.qty
                else:
                    n_call += 1
            if w.shares or w.option is not None:
                self.symbol_marks.append((t, w.symbol, w.shares * w.stock_mark
                                          - (w.option.qty * w.option.last_mark if w.option is not None else 0.0)))
            c = self.cycles.get(w.cycle_id)
            if c is not None:
                c.max_margin_lev = max(c.max_margin_lev, l)
                c.max_margin_unlev = max(c.max_margin_unlev, u)
        self.daily.append({
            "date": t, "nav": nav, "cash": self.cash, "stock_value": stock_val, "option_liability": liab,
            "borrowed": max(-self.cash, 0.0), "notional_exposure": put_notional + stock_val,
            "unleveraged_requirement": unlev, "leveraged_requirement": lev,
            "available_capital": nav - lev, "margin_utilization": lev / nav if nav > 0 else float("inf"),
            "n_short_puts": n_put, "n_covered_calls": n_call, "n_stock_names": n_stock,
            "n_active_names": len({s for s, w in self.wheels.items() if w.option or w.shares}),
            "cash_interest_accrued": self._day["cash_interest_accrued"], "cash_interest": self._day["cash_interest"],
            "financing_cost": self._day["financing_cost"], "available_liquidity": self.available_liquidity(),
            "margin_liquidity": self.margin_liquidity(), "cash_liquidity": self.cash_liquidity(),
        })

    def _reconcile(self, t):
        rebuilt = self.initial + sum(x[3] for x in self.ledger)
        if abs(rebuilt - self.cash) > 1.0:
            raise InvariantError(f"{t.date()}: cash {self.cash:,.2f} != ledger rebuild {rebuilt:,.2f}")
        if self.cash < -1e-6:
            # Step 9a: borrowing is never permitted, and a negative balance is never clamped away
            raise InvariantError(f"{t.date()}: negative cash {self.cash:,.2f} — Step 9a forbids borrowing")
        names = 0
        for sym, w in self.wheels.items():
            if w.shares < 0:
                raise InvariantError(f"{t.date()} {sym}: negative shares")
            o = w.option
            c = self.cycles.get(w.cycle_id)
            if c is not None and c.end is None and c.shares_open != w.shares:
                raise InvariantError(f"{t.date()} {sym}: share ledger {c.shares_open} != shares held {w.shares}")
            if w.shares and o is None and c is not None and c.called_away and not any(
                    x.symbol == sym and x.kind in ("SELL_STOCK", "LIQUIDATE", "UNIVERSE_EXIT") for x in self.orders):
                raise InvariantError(f"{t.date()} {sym}: {w.shares} shares orphaned after call-away (no exit order)")
            if o is not None and o.option_type == CALL and o.qty > w.shares:
                raise InvariantError(f"{t.date()} {sym}: naked call {o.qty} > shares {w.shares}")
            if o is not None and o.option_type == PUT and w.shares:
                raise InvariantError(f"{t.date()} {sym}: put written while holding stock")
            if o is not None or w.shares:
                names += 1
                if w.cycle_id is None:
                    raise InvariantError(f"{t.date()} {sym}: open position without a wheel cycle")
        if names > self.N:
            raise InvariantError(f"{t.date()}: {names} names open > {self.N}")
        if len({o.symbol for o in self.orders}) != len(self.orders):
            raise InvariantError(f"{t.date()}: duplicate orders for one symbol")

    # ---------------------------------------------------------------- run
    def step(self, t):
        t = pd.Timestamp(t)
        self._day = Counter()
        self._day_reasons = set()
        self._assigned_today = []
        self._breach_today = False
        self._financing(t)
        self._cash_interest(t)
        self._corporate_actions(t)
        self._terminations(t)
        self._mark(t)
        self._expiries(t)
        self._stop_losses(t)
        self._assignment_warnings(t)
        start_cash, start_nav, ex = self.cash, self.nav(), self.exposure()
        failed = self._fill_orders(t)
        nav_calls = self.nav()
        util_calls = self.required() / nav_calls if nav_calls > 0 else float("inf")
        self._margin_check(t)
        self._decisions(t, failed)
        self._record(t)
        self._reconcile(t)
        self.decision_log.append({
            "date": t, "starting_cash": start_cash, "starting_nav": start_nav,
            "assigned_stock_value": ex["stock_value"], "n_assigned_stocks": sum(1 for w in self.wheels.values() if w.shares),
            "assignments_today": len(self._assigned_today), "open_put_exposure": ex["put_exposure"],
            "covered_call_exposure": ex["call_exposure"], "gross_exposure": ex["gross"],
            "borrowed": max(-self.cash, 0.0), "uncovered_usable_shares": self._day["uncovered_usable_shares"],
            "covered_call_opportunities": self._day["call_opportunities"],
            "covered_call_lots_ordered": self._day["call_lots_ordered"],
            "covered_call_lots_sold": self._day["call_lots_sold"], "covered_call_premium": self._day["call_premium"],
            "cash_after_calls": self._cash_after_calls, "margin_utilization_after_calls": util_calls,
            "breach": self._breach_today, "new_put_capacity": self._day["new_put_capacity"],
            "new_put_lots_decided": self._day["put_lots_decided"],
            "new_put_lots_attempted": self._day["put_lots_attempted"],
            "new_put_lots_executed": self._day["put_lots_executed"],
            "new_put_lots_rejected": self._day["put_lots_rejected"],
            "rejection_reason": "|".join(sorted(self._day_reasons)),
        })
        self._prev_day = t

    def run(self, start, end):
        days = [d for d in self.md.trading_days if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
        first = pd.Timestamp(self.cfg["first_signal_date"])
        self._last_day = days[-1] if days else None
        if first not in self.md.day_index:
            raise ValueError(f"first_signal_date {first.date()} is not a trading day")
        for t in [first] + [d for d in days if d > first]:
            self.step(t)
        return self

    # ---------------------------------------------------------------- outputs
    def open_positions_rows(self, t):
        """Mark-to-market rows for positions still open at the end of the window."""
        rows = []
        for w in self.wheels.values():
            if w.option is not None:
                o = w.option
                pnl = (o.entry_premium - o.last_mark) * o.qty
                rows.append({"trade_id": o.trade_id, "cycle_id": o.cycle_id, "date": o.entry_date,
                             "symbol": o.symbol, "sector": self.sectors.get(o.symbol, "Unknown"),
                             "instrument": o.option_type, "expiry": o.expiry, "exit_date": None,
                             "strike": o.strike, "lots": o.lots, "lot_size": o.lot, "quantity": o.qty,
                             "entry_quote": o.entry_quote, "entry_premium": o.entry_premium,
                             "exit_price": o.last_mark, "status": "open_mtm", "assigned_or_exercised": False,
                             "underlying_entry": o.underlying_entry, "underlying_exit": w.stock_mark or None,
                             "premium_inr": o.entry_premium * o.qty, "option_pnl": pnl, "stock_pnl": 0.0,
                             "transaction_costs": o.entry_costs, "margin_requirement": o.margin_unlev,
                             "leveraged_capital_requirement": o.margin_lev, "total_pnl": pnl - o.entry_costs})
            if w.shares:
                pnl = (w.stock_mark - w.stock_price) * w.shares
                rows.append({"trade_id": None, "cycle_id": w.cycle_id, "date": w.stock_date, "symbol": w.symbol,
                             "sector": self.sectors.get(w.symbol, "Unknown"), "instrument": "STOCK",
                             "expiry": None, "exit_date": None, "strike": None, "lots": None, "lot_size": None,
                             "quantity": w.shares, "entry_quote": None, "entry_premium": None,
                             "exit_price": w.stock_mark, "status": "open_mtm", "assigned_or_exercised": False,
                             "underlying_entry": w.stock_price, "underlying_exit": w.stock_mark,
                             "premium_inr": 0.0, "option_pnl": 0.0, "stock_pnl": pnl, "transaction_costs": 0.0,
                             "margin_requirement": w.stock_mark * w.shares,
                             "leveraged_capital_requirement": w.stock_mark * w.shares / self.L, "total_pnl": pnl})
        return rows

    def share_audit(self, t) -> pd.DataFrame:
        """One row per wheel cycle that ever held stock, plus any symbol holding shares outside a cycle. Status ORPHAN
        marks shares the strategy no longer manages: a closed cycle with a non-zero share ledger, or shares with no
        open cycle. Positions still open at the window end are OPEN_AT_END (marked to market, still managed)."""
        rows = []
        for c in self.cycles.values():
            if not c.assigned:
                continue
            w = self.wheels.get(c.symbol)
            live = c.end is None and w is not None and w.cycle_id == c.cycle_id
            held = w.shares if live else 0
            covered = w.option.qty if live and w.option is not None and w.option.option_type == CALL else 0
            ok = (c.shares_open == held) if live else (c.shares_open == 0)
            rows.append({"symbol": c.symbol, "cycle_id": c.cycle_id, "cycle_status": c.status, "end": c.end,
                         "shares_assigned": c.shares_assigned, "shares_ca_delta": c.shares_ca_delta,
                         "shares_called_away": c.shares_called_away, "shares_sold": c.shares_sold,
                         "shares_held_at_end": held, "ledger_shares_open": c.shares_open,
                         "call_covered_at_end": covered, "uncovered_at_end": held - covered,
                         "max_uncovered_at_call": c.max_uncovered_at_call,
                         "residual_after_call_away": c.residual_after_call_away,
                         "status": ("OPEN_AT_END" if live else "FLAT") if ok else "ORPHAN"})
        for sym, w in self.wheels.items():
            if w.shares and (w.cycle_id is None or self.cycles[w.cycle_id].end is not None):
                rows.append({"symbol": sym, "cycle_id": w.cycle_id, "shares_held_at_end": w.shares, "status": "ORPHAN"})
        return pd.DataFrame(rows)

    def liquidation_cost_estimate(self, t) -> float:
        est = 0.0
        for w in self.wheels.values():
            if w.option is not None:
                o = w.option
                px = self.costs.option_fill(t, o.last_mark, "buy", self.md.month_end_spot(t, o.symbol) or 0.0)
                est += (px - o.last_mark) * o.qty + self.costs.option_buy(t, px * o.qty).total
            if w.shares:
                v = w.stock_mark * w.shares
                est += v * self.costs.equity_slippage_bps(t) / 1e4 + self.costs.equity_sale(t, v).total
        return est
