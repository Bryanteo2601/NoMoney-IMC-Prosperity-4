"""
Round 4 v29: robust-plus anchor gate + rebound lock.

This is deliberately less timestamp-fitted than v19/v20-style hard locks:
  1. The short-call overlay needs an anchor gate: VEX must be elevated above
     a blended static+session anchor before we open/refresh forced shorts.
  2. Targets are dynamic: size increases when VEX is clearly expensive,
     but scales down when Mark-flow is bullish or VEX starts rebounding.
  3. Rebound lock is armed by regime, not a single magic timestamp. Once VEX
     has made a meaningful low and rebounds from it, the strategy stops adding
     VEX/options risk and flattens VEX + option positions.
  4. A late fallback lock remains as a safety net.
"""

from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Optional, Tuple
import json, math


STRIKES   = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
VEV_PRODS = [f"VEV_{k}" for k in STRIKES]
UNDERLYING = "VELVETFRUIT_EXTRACT"
HP = "HYDROGEL_PACK"

LIMIT: Dict[str, int] = {
    HP:              200,
    UNDERLYING:      200,
    **{p: 300 for p in VEV_PRODS},
}

# Aliases for local backtest.py compatibility (not used by official grader)
POSITION_LIMIT = LIMIT
DEFAULT_LIMIT  = 300

ANCHORS = {
    HP:              10010.0,  # v13: 9991 → 10010 (+37k); live market trades at 10008-10056
    UNDERLYING:      5250.0,
}

# ── Core anchor/EMA MM ────────────────────────────────────────────────────────
ANCHOR_WEIGHT     = 0.85   # v11: 0.90 → 0.85  (+947)
EMA_ALPHA         = 0.0005
TAKE_THRESHOLD    = 12     # v10: 9    → 12    (+6,294)
INVENTORY_SKEW    = 0.005  # v19 sweep: 0.05 → 0.005
THRESHOLD_PER_POS = 0.04
ADD_CAP_LO        = 20     # reverted: 15 hurt HP passive fill volume in live
ADD_CAP_HI        = 18
ADD_THROTTLE_AT   = 100
UNWIND_CAP        = 200

# Maker params
HP_MAKE  = 3
VEX_MAKE = 3               # v10: 2    → 3     (+2,889)
MAKE_SZ  = 35

# ── Signal memory / flow leaning ──────────────────────────────────────────────
HP_SIG_DECAY      = 0.62
HP_SIG_TO_TICKS   = 0.10   # sweep: 0.40 → 0.10  (+27k backtest)
HP_SIG_CLIP_TICKS = 10
HP_FLOW_PASSIVE   = 18
HP_FLOW_TAKER     = 7

VEX_SIG_DECAY      = 0.50   # v19 sweep: 0.65 → 0.50
VEX_SIG_TO_TICKS   = 0.08   # v19 sweep: 0.16 → 0.08
VEX_SIG_CLIP_TICKS = 4      # v19 sweep: 8 → 4
VEX_FLOW_PASSIVE   = 23
VEX_FLOW_TAKER     = 8

HP_MARK38_EXTRA = 12

FLOW_WARMUP_TS = 9500
OPT_WARMUP_TS  = 5000

# ── Options MM / BS blend ─────────────────────────────────────────────────────
OPT_TAKE    = 1
OPT_MAKE    = 1
OPT_SZ      = 15
OPT_SKEW    = 1.0
OPT_MIN_MID = 10.0

USE_BS_OPTIONS = True
BS_WEIGHT      = 1.00   # sweep: 0.85 → 1.00  (+6k backtest)
# TTE starts at 4.0 at the beginning of Round 4, decays ~1/day.
# Historical CSVs had TTE=7 at start — sigma calibrated from those at that TTE.
ROUND4_TTE_START = 4.0
SIGMA_DAILY      = 0.01255

# ── V9 controlled smile / relative-value overlay ──────────────────────────────
# The v8 directional-pulse test only matched v4, so this file keeps the v4 core
# and adds ONE different edge: cross-sectional option IV mispricing.
USE_SMILE_OVERLAY = True
USE_SOFT_DELTA_HEDGE = False

SMILE_MIN_OBS = 4
SMILE_MAX_SPREAD = 18
SMILE_IV_LOW  = 0.0030
SMILE_IV_HIGH = 0.0450
SMILE_SIGMA_MIN = 0.0060
SMILE_SIGMA_MAX = 0.0260
SMILE_FAIR_CLIP = 20.0

SMILE_TAKE_EDGE    = 2.4
SMILE_PASSIVE_EDGE = 1.35
SMILE_TARGET_PER_TICK = 18
SMILE_TARGET_MAX_ATM  = 170
SMILE_TARGET_MAX_WING = 90
SMILE_TAKE_SZ_ATM     = 28
SMILE_TAKE_SZ_WING    = 16
SMILE_PASSIVE_SZ_ATM  = 22
SMILE_PASSIVE_SZ_WING = 12

# If the underlying informed-flow signal strongly disagrees with the option RV
# signal, only act when the option mispricing is large enough.
SMILE_CONFLICT_SIGNAL = 5.5
SMILE_CONFLICT_EDGE   = 5.0

# Soft hedge only used in v9b. It is deliberately weak so it does not erase the
# Mark67/Mark49 directional edge that made v4 good.
HEDGE_SKIP_SIGNAL = 5.0
HEDGE_DEAD_BAND   = 95.0
HEDGE_MAX_TRADE   = 35

# ── Rebound-triggered profit-lock overlay ─────────────────────────────────────
# Robust lock: it is not just a timestamp.  We arm the lock only after enough
# market has been observed or after VEX has made a meaningful low vs anchor.
# Once armed, a rebound from the intraday low means the bearish short-call
# regime may be ending, so we stop opening options/VEX risk and flatten.
USE_LATE_PROFIT_LOCK  = True
REBOUND_ARM_TS        = 34000
REBOUND_ARM_DROP      = 18     # arm early if VEX trades this far below anchor
SOFT_REBOUND_TICKS    = 20     # before hard lock: reduce forced-short target
REBOUND_LOCK_TICKS    = 34     # hard lock trigger above intraday VEX low
FALLBACK_LOCK_TS      = 86000  # hard late-day backstop regardless of rebound
SOFT_LOCK_VEX_UNWIND_CAP = 35
SOFT_LOCK_OPT_UNWIND_CAP = 55
LOCK_VEX_UNWIND_CAP      = 85
LOCK_OPT_UNWIND_CAP      = 110

# ── Price-conditional bearish call overlay ────────────────────────────────────
# Base targets are intentionally below the degen 300/300/300.  Runtime logic
# scales them up/down using the anchor gate, flow signal and rebound state.
FORCE_SHORT_CALLS = {
    "VEV_4500": 300,
    "VEV_5000": 300,
    "VEV_5500": 300,
}
FORCE_SHORT_MAX_TARGET = {
    "VEV_4500": 285,
    "VEV_5000": 285,
    "VEV_5500": 220,
}
FORCE_SHORT_PER_TICK = {
    "VEV_4500": 35,
    "VEV_5000": 35,
    "VEV_5500": 24,
}
FORCE_SHORT_UND_ABOVE_ANCHOR = 26
FORCE_SHORT_EXTREME          = 78
FLOW_BULLISH_CUT             = 2.2
FLOW_BEARISH_BOOST           = -1.2
SESSION_ANCHOR_WEIGHT        = 0.25  # blend first observed VEX mid into anchor

# Keep only a controlled OTM 5500 decayer through the lock.  The rebound lock
# still flattens VEX and the ITM 4500/5000 legs, which are the large tail risks.
LOCK_SKIP_SYMBOLS = {"VEV_5500"}




def _best(od: OrderDepth) -> Tuple[Optional[int], Optional[int], int, int]:
    bb = max(od.buy_orders)  if od and od.buy_orders  else None
    ba = min(od.sell_orders) if od and od.sell_orders else None
    bv = od.buy_orders[bb]   if bb is not None else 0
    av = -od.sell_orders[ba] if ba is not None else 0
    return bb, ba, bv, av


def _mid(od: Optional[OrderDepth]) -> Optional[float]:
    if not od or not od.buy_orders or not od.sell_orders:
        return None
    return (max(od.buy_orders) + min(od.sell_orders)) / 2.0


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_call(S: float, K: float, t_days: float, sigma_daily: float = SIGMA_DAILY) -> float:
    """Black-Scholes call, r=0. t_days is remaining TTE in days."""
    if S <= 0 or K <= 0:
        return 0.0
    if t_days <= 0 or sigma_daily <= 0:
        return max(S - K, 0.0)
    T = max(t_days, 1e-9) / 252.0
    sigma = sigma_daily * math.sqrt(252.0)
    vol_sqrt_t = sigma * math.sqrt(T)
    if vol_sqrt_t <= 1e-12:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    return S * _norm_cdf(d1) - K * _norm_cdf(d2)


def _bs_delta_call(S: float, K: float, t_days: float, sigma_daily: float = SIGMA_DAILY) -> float:
    """Black-Scholes call delta, clipped to [0, 1]."""
    if S <= 0 or K <= 0:
        return 0.0
    if t_days <= 0 or sigma_daily <= 0:
        return 1.0 if S > K else 0.0
    T = max(t_days, 1e-9) / 252.0
    sigma = sigma_daily * math.sqrt(252.0)
    vol_sqrt_t = sigma * math.sqrt(T)
    if vol_sqrt_t <= 1e-12:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / vol_sqrt_t
    return _clip(_norm_cdf(d1), 0.0, 1.0)


def _implied_sigma_daily_call(price: float, S: float, K: float, t_days: float) -> Optional[float]:
    """Bisection implied daily sigma for a call. Returns None for invalid quotes."""
    if S <= 0 or K <= 0 or t_days <= 0 or price <= 0:
        return None
    intrinsic = max(S - K, 0.0)
    if price < intrinsic + 0.05 or price > S:
        return None

    lo, hi = SMILE_IV_LOW, SMILE_IV_HIGH
    lo_val = _bs_call(S, K, t_days, lo)
    hi_val = _bs_call(S, K, t_days, hi)
    if price <= lo_val:
        return lo
    if price >= hi_val:
        return hi

    for _ in range(28):
        mid = 0.5 * (lo + hi)
        val = _bs_call(S, K, t_days, mid)
        if val < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _weighted_quad_fit(xs: List[float], ys: List[float], ws: List[float]) -> Optional[Tuple[float, float, float]]:
    """Weighted fit y = a*x^2 + b*x + c without numpy."""
    if len(xs) < 3:
        return None

    s0 = sum(ws)
    s1 = sum(w * x for x, w in zip(xs, ws))
    s2 = sum(w * x * x for x, w in zip(xs, ws))
    s3 = sum(w * x * x * x for x, w in zip(xs, ws))
    s4 = sum(w * x * x * x * x for x, w in zip(xs, ws))
    t0 = sum(w * y for y, w in zip(ys, ws))
    t1 = sum(w * x * y for x, y, w in zip(xs, ys, ws))
    t2 = sum(w * x * x * y for x, y, w in zip(xs, ys, ws))

    A = [[s4, s3, s2, t2],
         [s3, s2, s1, t1],
         [s2, s1, s0, t0]]

    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(A[r][col]))
        if abs(A[piv][col]) < 1e-12:
            return None
        if piv != col:
            A[col], A[piv] = A[piv], A[col]
        div = A[col][col]
        for j in range(col, 4):
            A[col][j] /= div
        for r in range(3):
            if r == col:
                continue
            factor = A[r][col]
            for j in range(col, 4):
                A[r][j] -= factor * A[col][j]

    a, b, c = A[0][3], A[1][3], A[2][3]
    return a, b, c


def _pending(result: Dict[str, List[Order]], prod: str) -> Tuple[int, int]:
    buys = sum(o.quantity for o in result.get(prod, []) if o.quantity > 0)
    sells = -sum(o.quantity for o in result.get(prod, []) if o.quantity < 0)
    return buys, sells


class Trader:
    def run(self, state: TradingState):
        try:
            mem = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            mem = {}

        ema: Dict[str, float] = mem.get("ema", {})
        sig: Dict[str, float] = mem.get("sig", {})

        result: Dict[str, List[Order]] = {}
        positions = state.position or {}
        self.cur_ts = getattr(state, "timestamp", 0)

        # ── TTE decay: track day resets (timestamp rolls back when new day starts) ─
        last_ts = mem.get("last_ts", -1)
        day = mem.get("day", 0)
        if last_ts >= 0 and self.cur_ts < last_ts:
            day += 1
            mem["und_low"] = None  # reset intraday low on new day
            mem["und_high"] = None
            mem["session_anchor"] = None
            mem["rebound_armed"] = False
            mem["profit_locked"] = False
        mem["last_ts"] = self.cur_ts
        mem["day"] = day
        current_tte = max(0.25, ROUND4_TTE_START - day - self.cur_ts / 1_000_000.0)

        # ── Update signal memory from visible market trades ───────────────────
        hp_raw = 0.0
        for t in state.market_trades.get(HP, []):
            q = getattr(t, "quantity", 0) or 0
            if getattr(t, "buyer", None) == "Mark 14":
                hp_raw += 1.00 * q
            if getattr(t, "seller", None) == "Mark 14":
                hp_raw -= 1.00 * q
            if getattr(t, "buyer", None) == "Mark 38":
                hp_raw -= 0.85 * q
            if getattr(t, "seller", None) == "Mark 38":
                hp_raw += 0.85 * q
        sig[HP] = HP_SIG_DECAY * sig.get(HP, 0.0) + hp_raw

        vex_raw = 0.0
        for t in state.market_trades.get(UNDERLYING, []):
            q = getattr(t, "quantity", 0) or 0
            buyer  = getattr(t, "buyer", None)
            seller = getattr(t, "seller", None)
            # Mark 67: pure buyer, strong bullish signal (96% accuracy, +1.97/tick).
            if buyer  == "Mark 67": vex_raw += 1.00 * q
            if seller == "Mark 67": vex_raw -= 1.00 * q
            # Mark 49: sells to Mark 67 — bullish when selling (same signal, 94% accuracy).
            # Increased weight 0.35 -> 0.60: data shows near-identical signal quality to M67.
            if seller == "Mark 49": vex_raw += 0.60 * q
            if buyer  == "Mark 49": vex_raw -= 0.60 * q
            # Mark 55 REMOVED: h=1 signal = +0.01 (zero), but 12x volume = pure noise.
        sig[UNDERLYING] = VEX_SIG_DECAY * sig.get(UNDERLYING, 0.0) + vex_raw

        # ── HP + VEX: anchor-EMA MM with signal-adjusted fair ─────────────────
        for sym in (HP, UNDERLYING):
            od = state.order_depths.get(sym)
            if od is None:
                continue
            bb, ba, bv, av = _best(od)
            if bb is None or ba is None:
                continue

            mid = 0.5 * (bb + ba)
            anchor = ANCHORS[sym]
            new_ema = (1 - EMA_ALPHA) * ema.get(sym, anchor) + EMA_ALPHA * mid
            ema[sym] = new_ema

            pos = positions.get(sym, 0)
            lim = LIMIT[sym]
            make_edge = HP_MAKE if sym == HP else VEX_MAKE

            if sym == HP:
                sig_ticks = _clip(sig.get(sym, 0.0) * HP_SIG_TO_TICKS,
                                  -HP_SIG_CLIP_TICKS, HP_SIG_CLIP_TICKS)
            else:
                sig_ticks = _clip(sig.get(sym, 0.0) * VEX_SIG_TO_TICKS,
                                  -VEX_SIG_CLIP_TICKS, VEX_SIG_CLIP_TICKS)

            if self.cur_ts < FLOW_WARMUP_TS:
                sig_ticks *= 0.20
            fair = (ANCHOR_WEIGHT * anchor + (1 - ANCHOR_WEIGHT) * new_ema
                    + sig_ticks - INVENTORY_SKEW * pos)

            buy_is_unwind  = pos < 0
            sell_is_unwind = pos > 0
            add_cap  = ADD_CAP_LO if abs(pos) < ADD_THROTTLE_AT else ADD_CAP_HI
            buy_cap  = UNWIND_CAP if buy_is_unwind  else add_cap
            sell_cap = UNWIND_CAP if sell_is_unwind else add_cap
            buy_thr  = TAKE_THRESHOLD if buy_is_unwind  else TAKE_THRESHOLD + THRESHOLD_PER_POS * abs(pos)
            sell_thr = TAKE_THRESHOLD if sell_is_unwind else TAKE_THRESHOLD + THRESHOLD_PER_POS * abs(pos)

            orders: List[Order] = []
            taken_buy = 0
            taken_sell = 0

            if ba <= fair - buy_thr and pos < lim:
                qty = min(av, lim - pos, buy_cap)
                if qty > 0:
                    orders.append(Order(sym, ba, qty))
                    taken_buy += qty

            if bb >= fair + sell_thr and pos > -lim:
                qty = min(bv, lim + pos, sell_cap)
                if qty > 0:
                    orders.append(Order(sym, bb, -qty))
                    taken_sell += qty

            for px in sorted(od.sell_orders):
                if px == ba:
                    continue
                if px > fair - buy_thr or pos + taken_buy >= lim:
                    break
                avail = -od.sell_orders[px]
                room  = min(lim - pos - taken_buy, buy_cap - taken_buy)
                qty   = min(avail, room)
                if qty > 0:
                    orders.append(Order(sym, px, qty))
                    taken_buy += qty

            for px in sorted(od.buy_orders, reverse=True):
                if px == bb:
                    continue
                if px < fair + sell_thr or pos - taken_sell <= -lim:
                    break
                avail = od.buy_orders[px]
                room  = min(lim + pos - taken_sell, sell_cap - taken_sell)
                qty   = min(avail, room)
                if qty > 0:
                    orders.append(Order(sym, px, -qty))
                    taken_sell += qty

            rem_buy  = lim - pos - taken_buy
            rem_sell = lim + pos - taken_sell
            my_bid = min(bb + 1, math.floor(fair - make_edge))
            my_ask = max(ba - 1, math.ceil (fair + make_edge))
            if my_bid >= ba:     my_bid = ba - 1
            if my_ask <= bb:     my_ask = bb + 1
            if my_ask <= my_bid: my_ask = my_bid + 1
            if my_bid > 0 and rem_buy > 0:
                q = min(rem_buy, MAKE_SZ)
                orders.append(Order(sym, my_bid, q))
                rem_buy -= q
            if rem_sell > 0:
                q = min(rem_sell, MAKE_SZ)
                orders.append(Order(sym, my_ask, -q))
                rem_sell -= q

            cur_sig = sig.get(sym, 0.0)
            if self.cur_ts < FLOW_WARMUP_TS:
                cur_sig = 0.0
            if sym == HP:
                passive_sz, taker_sz, passive_thr, taker_thr = HP_FLOW_PASSIVE, HP_FLOW_TAKER, 3.0, 10.0
            else:
                passive_sz, taker_sz, passive_thr, taker_thr = VEX_FLOW_PASSIVE, VEX_FLOW_TAKER, 4.0, 14.0

            if cur_sig > passive_thr and rem_buy > 0:
                if cur_sig > taker_thr and ba <= fair + 1 and rem_buy > 0:
                    q = min(av, rem_buy, taker_sz)
                    if q > 0:
                        orders.append(Order(sym, ba, q))
                        rem_buy -= q
                if rem_buy > 0:
                    px = min(ba - 1, bb + 1)
                    if px > 0 and px < ba:
                        orders.append(Order(sym, px, min(rem_buy, passive_sz)))
            elif cur_sig < -passive_thr and rem_sell > 0:
                if cur_sig < -taker_thr and bb >= fair - 1 and rem_sell > 0:
                    q = min(bv, rem_sell, taker_sz)
                    if q > 0:
                        orders.append(Order(sym, bb, -q))
                        rem_sell -= q
                if rem_sell > 0:
                    px = max(bb + 1, ba - 1)
                    if px > bb:
                        orders.append(Order(sym, px, -min(rem_sell, passive_sz)))

            if orders:
                result[sym] = orders

        # ── Direct Mark 38 HP fade from current tick ──────────────────────────
        hp_od  = state.order_depths.get(HP)
        hp_pos = positions.get(HP, 0)
        if hp_od:
            hp_mt       = state.market_trades.get(HP, [])
            m38_buying  = any(getattr(t, "buyer", None)  == "Mark 38" for t in hp_mt)
            m38_selling = any(getattr(t, "seller", None) == "Mark 38" for t in hp_mt)
            already_buy  = sum(o.quantity for o in result.get(HP, []) if o.quantity > 0)
            already_sell = -sum(o.quantity for o in result.get(HP, []) if o.quantity < 0)
            if self.cur_ts < FLOW_WARMUP_TS:
                m38_buying = False
                m38_selling = False
            if m38_buying and hp_od.sell_orders:
                best_ask = min(hp_od.sell_orders)
                cap = LIMIT[HP] + hp_pos - already_sell
                if cap > 0:
                    result.setdefault(HP, []).append(
                        Order(HP, best_ask - 1, -min(cap, HP_MARK38_EXTRA))
                    )
            elif m38_selling and hp_od.buy_orders:
                best_bid = max(hp_od.buy_orders)
                cap = LIMIT[HP] - hp_pos - already_buy
                if cap > 0:
                    result.setdefault(HP, []).append(
                        Order(HP, best_bid + 1, min(cap, HP_MARK38_EXTRA))
                    )

        # ── VEV call options MM with BS correction using decaying TTE ─────────
        self.cur_vex_ticks = _clip(sig.get(UNDERLYING, 0.0) * VEX_SIG_TO_TICKS,
                                   -VEX_SIG_CLIP_TICKS, VEX_SIG_CLIP_TICKS)
        if self.cur_ts < FLOW_WARMUP_TS:
            self.cur_vex_ticks *= 0.20

        und_mid = _mid(state.order_depths.get(UNDERLYING))

        # ── Track VEX regime for anchor-gate + rebound lock ─────────────────
        session_anchor = mem.get("session_anchor", None)
        und_low = mem.get("und_low", None)
        und_high = mem.get("und_high", None)
        if und_mid is not None:
            if session_anchor is None:
                session_anchor = und_mid
            if und_low is None or und_mid < und_low:
                und_low = und_mid
            if und_high is None or und_mid > und_high:
                und_high = und_mid
        mem["session_anchor"] = session_anchor
        mem["und_low"] = und_low
        mem["und_high"] = und_high

        # Anchor gate = mostly the stable historical anchor, with a small
        # adjustment for the current session's opening regime.
        if session_anchor is not None:
            anchor_ref = ((1.0 - SESSION_ANCHOR_WEIGHT) * ANCHORS[UNDERLYING]
                          + SESSION_ANCHOR_WEIGHT * session_anchor)
        else:
            anchor_ref = ANCHORS[UNDERLYING]
        self.anchor_ref = anchor_ref

        rebound_armed = bool(mem.get("rebound_armed", False))
        if und_mid is not None and und_low is not None:
            if (self.cur_ts >= REBOUND_ARM_TS or
                    und_low <= anchor_ref - REBOUND_ARM_DROP):
                rebound_armed = True
        mem["rebound_armed"] = rebound_armed

        soft_rebound = (rebound_armed and und_mid is not None and und_low is not None
                        and und_mid > und_low + SOFT_REBOUND_TICKS)
        vex_rebounded = (rebound_armed and und_mid is not None and und_low is not None
                         and und_mid > und_low + REBOUND_LOCK_TICKS)

        profit_lock_active = bool(mem.get("profit_locked", False))
        if USE_LATE_PROFIT_LOCK and (vex_rebounded or self.cur_ts >= FALLBACK_LOCK_TS):
            profit_lock_active = True
        mem["profit_locked"] = profit_lock_active
        self.soft_rebound = soft_rebound

        smile_fairs, smile_deltas = self._compute_smile_fairs(state, und_mid, current_tte)
        for prod in VEV_PRODS:
            depth = state.order_depths.get(prod)
            if not depth:
                continue
            mid = _mid(depth)
            if mid is None or mid < OPT_MIN_MID:
                continue

            strike = int(prod.split("_")[1])
            intrinsic = max((und_mid or 0.0) - strike, 0.0) if und_mid is not None else 0.0
            book_fair = max(mid, intrinsic + 0.5)
            fair_opt = book_fair
            if USE_BS_OPTIONS and und_mid is not None:
                # Use current_tte (decays across round days) not the fixed 4.0
                bs_fair = max(_bs_call(und_mid, strike, current_tte), intrinsic + 0.5)
                blended = (1.0 - BS_WEIGHT) * book_fair + BS_WEIGHT * bs_fair
                fair_opt = _clip(blended, book_fair - 6.0, book_fair + 6.0)

            pos  = positions.get(prod, 0)
            lim  = LIMIT[prod]
            opts = self._opt_mm(prod, depth, fair_opt, pos, lim)
            if opts:
                result[prod] = opts

            if prod in smile_fairs:
                self._add_smile_overlay(prod, depth, smile_fairs[prod],
                                        smile_deltas.get(prod, 0.5), positions, result)

        if USE_SOFT_DELTA_HEDGE and smile_deltas and und_mid is not None:
            self._maybe_soft_delta_hedge(state, positions, result, smile_deltas)



        # ── Anchor-gated dynamic force-short overlay ─────────────────────────
        und_above_anchor = (und_mid is not None
                            and und_mid > self.anchor_ref + FORCE_SHORT_UND_ABOVE_ANCHOR)
        self.force_short_targets = {}
        if FORCE_SHORT_CALLS and not profit_lock_active and und_above_anchor:
            ext = und_mid - self.anchor_ref
            vex_ticks_now = getattr(self, "cur_vex_ticks", 0.0)

            # Start conservative near the gate, scale up only if VEX is clearly
            # stretched.  Cut hard if visible Mark-flow is bullish or a rebound
            # is already beginning.
            mult = 0.72
            if ext >= FORCE_SHORT_EXTREME:
                mult = 1.12
            elif ext >= FORCE_SHORT_UND_ABOVE_ANCHOR + 28:
                mult = 0.92
            if vex_ticks_now > FLOW_BULLISH_CUT:
                mult *= 0.50
            elif vex_ticks_now < FLOW_BEARISH_BOOST:
                mult *= 1.12
            if soft_rebound:
                mult *= 0.65

            for prod, base_target in FORCE_SHORT_CALLS.items():
                cap_target = FORCE_SHORT_MAX_TARGET.get(prod, base_target)
                target = int(_clip(round(base_target * mult), 0, cap_target))
                if target >= 40:
                    self.force_short_targets[prod] = target
            if self.force_short_targets:
                self._add_forced_call_shorts(state, positions, result)

        # ── Rebound-triggered profit lock: keep HP, flatten VEX + options ────
        if profit_lock_active:
            for sym in [UNDERLYING] + VEV_PRODS:
                if sym in LOCK_SKIP_SYMBOLS:
                    continue
                depth = state.order_depths.get(sym)
                pos = positions.get(sym, 0)
                cap = LOCK_VEX_UNWIND_CAP if sym == UNDERLYING else LOCK_OPT_UNWIND_CAP
                lock_orders = self._late_flatten_symbol(sym, depth, pos, cap)
                if lock_orders:
                    result[sym] = lock_orders
                else:
                    result.pop(sym, None)

        mem["ema"] = ema
        mem["sig"] = sig
        # und_low already written into mem above
        td = json.dumps(mem)
        if len(td) > 45000:
            td = json.dumps({"ema": ema, "sig": sig, "und_low": und_low,
                             "und_high": und_high, "session_anchor": session_anchor,
                             "rebound_armed": rebound_armed,
                             "profit_locked": profit_lock_active,
                             "day": day, "last_ts": self.cur_ts})
        return result, 0, td


    def _compute_smile_fairs(self, state: TradingState, und_mid: Optional[float],
                             tte: float) -> Tuple[Dict[str, float], Dict[str, float]]:
        """Fit a guarded quadratic IV smile and return per-option fair/delta."""
        fairs: Dict[str, float] = {}
        deltas: Dict[str, float] = {}
        if (not USE_SMILE_OVERLAY) or und_mid is None or und_mid <= 0:
            return fairs, deltas

        obs: List[Tuple[float, float, float]] = []
        for prod in VEV_PRODS:
            depth = state.order_depths.get(prod)
            if not depth or not depth.buy_orders or not depth.sell_orders:
                continue
            bb, ba, _, _ = _best(depth)
            if bb is None or ba is None:
                continue
            spread = ba - bb
            if spread <= 0 or spread > SMILE_MAX_SPREAD:
                continue
            mid = 0.5 * (bb + ba)
            if mid < OPT_MIN_MID:
                continue
            try:
                k = int(prod.split("_")[1])
            except Exception:
                continue
            iv = _implied_sigma_daily_call(mid, und_mid, k, tte)
            if iv is None:
                continue
            delta = _bs_delta_call(und_mid, k, tte, iv)
            if delta < 0.08 or delta > 0.94:
                continue
            x = math.log(k / und_mid) / math.sqrt(max(tte / 252.0, 1e-9))
            atm_weight = 0.65 + 0.35 * min(1.0, 4.0 * min(delta, 1.0 - delta))
            w = atm_weight / max(1.0, spread)
            obs.append((x, iv, w))

        if len(obs) < SMILE_MIN_OBS:
            return fairs, deltas

        fit = _weighted_quad_fit([o[0] for o in obs], [o[1] for o in obs], [o[2] for o in obs])
        if fit is None:
            return fairs, deltas
        a, b, c = fit

        for prod in VEV_PRODS:
            depth = state.order_depths.get(prod)
            if not depth or not depth.buy_orders or not depth.sell_orders:
                continue
            bb, ba, _, _ = _best(depth)
            if bb is None or ba is None:
                continue
            spread = ba - bb
            if spread <= 0 or spread > SMILE_MAX_SPREAD:
                continue
            mid = 0.5 * (bb + ba)
            if mid < OPT_MIN_MID:
                continue
            try:
                k = int(prod.split("_")[1])
            except Exception:
                continue
            x = math.log(k / und_mid) / math.sqrt(max(tte / 252.0, 1e-9))
            pred_iv = _clip(a * x * x + b * x + c, SMILE_SIGMA_MIN, SMILE_SIGMA_MAX)
            delta = _bs_delta_call(und_mid, k, tte, pred_iv)
            if delta < 0.04 or delta > 0.97:
                continue
            raw_fair = _bs_call(und_mid, k, tte, pred_iv)
            intrinsic = max(und_mid - k, 0.0)
            raw_fair = max(raw_fair, intrinsic + 0.5)
            fairs[prod] = _clip(raw_fair, mid - SMILE_FAIR_CLIP, mid + SMILE_FAIR_CLIP)
            deltas[prod] = delta

        return fairs, deltas

    def _add_smile_overlay(self, prod: str, depth: OrderDepth, fair: float,
                           delta: float, positions: Dict[str, int],
                           result: Dict[str, List[Order]]) -> None:
        """Add controlled RV orders if fitted-smile fair says the option is mispriced."""
        if not depth.buy_orders or not depth.sell_orders:
            return
        bb, ba, bv, av = _best(depth)
        if bb is None or ba is None:
            return
        mid = 0.5 * (bb + ba)
        edge = fair - mid
        if abs(edge) < SMILE_PASSIVE_EDGE:
            return

        vex_ticks = getattr(self, "cur_vex_ticks", 0.0)
        if edge > 0 and vex_ticks < -SMILE_CONFLICT_SIGNAL and edge < SMILE_CONFLICT_EDGE:
            return
        if edge < 0 and vex_ticks > SMILE_CONFLICT_SIGNAL and -edge < SMILE_CONFLICT_EDGE:
            return

        pos = positions.get(prod, 0)
        lim = LIMIT[prod]
        buy_pending, sell_pending = _pending(result, prod)
        effective_pos = pos + buy_pending - sell_pending

        try:
            k = int(prod.split("_")[1])
        except Exception:
            k = 5250
        is_atm = 5000 <= k <= 5500
        max_target = SMILE_TARGET_MAX_ATM if is_atm else SMILE_TARGET_MAX_WING
        take_sz = SMILE_TAKE_SZ_ATM if is_atm else SMILE_TAKE_SZ_WING
        pass_sz = SMILE_PASSIVE_SZ_ATM if is_atm else SMILE_PASSIVE_SZ_WING
        target_abs = min(max_target, int(SMILE_TARGET_PER_TICK * abs(edge)))
        target = target_abs if edge > 0 else -target_abs

        if edge > 0:
            need = target - effective_pos
            cap = lim - pos - buy_pending
            if need <= 0 or cap <= 0:
                return
            if ba <= fair - SMILE_TAKE_EDGE:
                q = min(av, cap, need, take_sz)
                if q > 0:
                    result.setdefault(prod, []).append(Order(prod, ba, q))
                    return
            px = min(ba - 1, math.floor(fair - SMILE_PASSIVE_EDGE))
            if px > 0 and px < ba and px > bb:
                q = min(cap, need, pass_sz)
                if q > 0:
                    result.setdefault(prod, []).append(Order(prod, px, q))
        else:
            need = effective_pos - target
            cap = lim + pos - sell_pending
            if need <= 0 or cap <= 0:
                return
            if bb >= fair + SMILE_TAKE_EDGE:
                q = min(bv, cap, need, take_sz)
                if q > 0:
                    result.setdefault(prod, []).append(Order(prod, bb, -q))
                    return
            px = max(bb + 1, math.ceil(fair + SMILE_PASSIVE_EDGE))
            if px > bb and px < ba:
                q = min(cap, need, pass_sz)
                if q > 0:
                    result.setdefault(prod, []).append(Order(prod, px, -q))



    def _add_forced_call_shorts(self, state: TradingState, positions: Dict[str, int],
                                result: Dict[str, List[Order]]) -> None:
        """Short selected call strikes into visible bid liquidity, without opening longs."""
        targets = getattr(self, "force_short_targets", FORCE_SHORT_CALLS)
        for prod, target_abs in targets.items():
            depth = state.order_depths.get(prod)
            if not depth or not depth.buy_orders:
                continue
            bb = max(depth.buy_orders)
            if bb <= 0:
                continue
            bv = depth.buy_orders[bb]
            pos = positions.get(prod, 0)

            # Replace normal MM orders for this target. Passive asks can block the
            # forced fill accounting, so this overlay uses only visible bid liquidity.
            result[prod] = []
            buy_pending, sell_pending = _pending(result, prod)
            effective_pos = pos + buy_pending - sell_pending
            target_pos = -min(target_abs, LIMIT[prod])
            need_to_sell = effective_pos - target_pos
            if need_to_sell <= 0:
                if not result[prod]:
                    result.pop(prod, None)
                continue

            cap = LIMIT[prod] + pos - sell_pending
            q = min(bv, cap, int(need_to_sell), FORCE_SHORT_PER_TICK.get(prod, 30))
            if q > 0:
                result.setdefault(prod, []).append(Order(prod, bb, -q))
            elif not result[prod]:
                result.pop(prod, None)

    def _late_flatten_symbol(self, sym: str, depth: Optional[OrderDepth],
                             pos: int, per_tick_cap: int) -> List[Order]:
        """Aggressively reduce an existing position; never opens fresh risk."""
        orders: List[Order] = []
        if pos == 0 or depth is None:
            return orders

        remaining = min(abs(pos), per_tick_cap)

        # Short position: buy asks to reduce |pos|.
        if pos < 0 and depth.sell_orders:
            for px in sorted(depth.sell_orders):
                if remaining <= 0:
                    break
                avail = -depth.sell_orders[px]
                q = min(avail, remaining)
                if q > 0:
                    orders.append(Order(sym, px, q))
                    remaining -= q

        # Long position: sell bids to reduce |pos|.
        elif pos > 0 and depth.buy_orders:
            for px in sorted(depth.buy_orders, reverse=True):
                if remaining <= 0:
                    break
                avail = depth.buy_orders[px]
                q = min(avail, remaining)
                if q > 0:
                    orders.append(Order(sym, px, -q))
                    remaining -= q

        return orders

    def _maybe_soft_delta_hedge(self, state: TradingState, positions: Dict[str, int],
                                result: Dict[str, List[Order]],
                                opt_deltas: Dict[str, float]) -> None:
        """Weakly hedge option delta only when VEX flow is not giving a strong edge."""
        if not USE_SOFT_DELTA_HEDGE:
            return
        if abs(getattr(self, "cur_vex_ticks", 0.0)) >= HEDGE_SKIP_SIGNAL:
            return
        depth = state.order_depths.get(UNDERLYING)
        if not depth or not depth.buy_orders or not depth.sell_orders:
            return
        bb, ba, bv, av = _best(depth)
        if bb is None or ba is None:
            return

        opt_delta = 0.0
        for prod, d in opt_deltas.items():
            opt_delta += positions.get(prod, 0) * d

        under_pos = positions.get(UNDERLYING, 0)
        buy_pending, sell_pending = _pending(result, UNDERLYING)
        effective_under = under_pos + buy_pending - sell_pending
        target_under = int(_clip(-0.55 * opt_delta, -LIMIT[UNDERLYING], LIMIT[UNDERLYING]))
        need = target_under - effective_under
        if abs(need) < HEDGE_DEAD_BAND:
            return

        if need > 0:
            cap = LIMIT[UNDERLYING] - under_pos - buy_pending
            q = min(int(need), cap, av, HEDGE_MAX_TRADE)
            if q > 0:
                result.setdefault(UNDERLYING, []).append(Order(UNDERLYING, ba, q))
        else:
            cap = LIMIT[UNDERLYING] + under_pos - sell_pending
            q = min(int(-need), cap, bv, HEDGE_MAX_TRADE)
            if q > 0:
                result.setdefault(UNDERLYING, []).append(Order(UNDERLYING, bb, -q))

    def _opt_mm(self, prod: str, depth: OrderDepth, fair: float,
                pos: int, lim: int) -> List[Order]:
        orders: List[Order] = []
        buy_cap  = lim - pos
        sell_cap = lim + pos
        try:
            k = int(prod.split("_")[1])
        except Exception:
            k = 5250
        opt_sz = 22 if 5000 <= k <= 5500 else 12
        if self.cur_ts < OPT_WARMUP_TS:
            opt_sz = max(8, opt_sz // 2)

        asks = sorted(depth.sell_orders)              if depth.sell_orders else []
        bids = sorted(depth.buy_orders, reverse=True) if depth.buy_orders  else []
        best_ask = asks[0] if asks else None
        best_bid = bids[0] if bids else None

        for px in asks:
            if px >= fair - OPT_TAKE or buy_cap <= 0:
                break
            v = min(-depth.sell_orders[px], buy_cap, opt_sz)
            if v > 0:
                orders.append(Order(prod, px, v))
                buy_cap -= v

        for px in bids:
            if px <= fair + OPT_TAKE or sell_cap <= 0:
                break
            v = min(depth.buy_orders[px], sell_cap, opt_sz)
            if v > 0:
                orders.append(Order(prod, px, -v))
                sell_cap -= v

        if best_ask is None or best_bid is None:
            return orders

        skew   = -pos / max(lim, 1) * OPT_MAKE * OPT_SKEW
        my_bid = min(best_bid + 1, math.floor(fair - OPT_MAKE + skew))
        my_ask = max(best_ask - 1, math.ceil (fair + OPT_MAKE + skew))
        if my_bid >= best_ask:  my_bid = best_ask - 1
        if my_ask <= best_bid:  my_ask = best_bid + 1
        if my_ask <= my_bid:    my_ask = my_bid + 1
        if my_bid > 0 and buy_cap > 0:
            orders.append(Order(prod, my_bid,  min(buy_cap,  opt_sz)))
        if sell_cap > 0:
            orders.append(Order(prod, my_ask, -min(sell_cap, opt_sz)))

        return orders