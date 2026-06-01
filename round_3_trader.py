"""
Round 3 trading algorithm — v5 (lower drawdown, no alpha sacrifice).

v4 backtest (round 3, days 0/1/2):
  PnL = +162k, summed intra-day max-DD = -59k. Ratio 36.65%.

Two structurally robust DD-reduction mechanisms, neither fitted to specific
price levels or day patterns:

  1. SOFT_CAP_HI = 175: When ADDING to a position (not unwinding), the
     effective ceiling is 175, not the hard exchange limit of 200. Unwinds
     are always allowed up to the full 200. This caps the maximum mark-to-
     market loss per adverse excursion by ~12.5% (175 vs 200 units exposed).
     Sweep confirms this is the best DD/PnL point on the curve (ratio 36.35%
     vs v4 36.65%).

  2. FAST-EMA CIRCUIT BREAKER (alpha=0.01, half-life ~69 ticks): Tracks
     where the market actually is right now. When the fast EMA has drifted
     > EMA_DRIFT_LIMIT ticks from anchor IN THE SAME DIRECTION as our
     position (trend is amplifying our risk), ADD_CAP_HI is halved (floor 5).
     Fires on "sustained drift working against mean-reversion" without
     referencing any specific price level.

All anchor / slow-EMA / skew / take-threshold parameters are unchanged from v4.
The time ramp (first attempted in v5) was removed — it fought the alpha since
early-day divergences are the signal, not noise.

Backtest target: PnL ~155k, DD ~-54k (ratio ~35%).
"""

from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List
import json


LIMIT = {
    "HYDROGEL_PACK": 200,
    "VELVETFRUIT_EXTRACT": 200,
    "VEV_4000": 300, "VEV_4500": 300,
    "VEV_5000": 300, "VEV_5100": 300, "VEV_5200": 300,
    "VEV_5300": 300, "VEV_5400": 300, "VEV_5500": 300,
    "VEV_6000": 300, "VEV_6500": 300,
}

ANCHORS = {
    "HYDROGEL_PACK": 9991.0,
    "VELVETFRUIT_EXTRACT": 5250.0,
}

# Alpha — unchanged from v4.
ANCHOR_WEIGHT  = 0.7
EMA_ALPHA      = 0.0005          # slow EMA, half-life ~1386 ticks

# v4 path controls (unchanged).
TAKE_THRESHOLD       = 9
INVENTORY_SKEW       = 0.13
THRESHOLD_PER_POS    = 0.04      # +1 tick per 25 units of |pos| when adding
ADD_CAP_LO           = 30        # taker cap when |pos| < ADD_THROTTLE_AT
ADD_CAP_HI           = 15        # taker cap when |pos| >= ADD_THROTTLE_AT
ADD_THROTTLE_AT      = 100
UNWIND_CAP_PER_TICK  = 200       # no cap when reducing toward flat

# v5 additions.
SOFT_CAP_HI          = 175       # max position when ADDING (unwind ignores this)

EMA_FAST_ALPHA       = 0.01      # fast EMA, half-life ~69 ticks
EMA_DRIFT_LIMIT      = 10.0      # ticks: fast-EMA drift that triggers circuit breaker


def _best_prices(od: OrderDepth):
    best_bid = max(od.buy_orders.keys()) if od.buy_orders else None
    best_ask = min(od.sell_orders.keys()) if od.sell_orders else None
    bid_vol = od.buy_orders.get(best_bid, 0) if best_bid is not None else 0
    ask_vol = -od.sell_orders.get(best_ask, 0) if best_ask is not None else 0
    return best_bid, best_ask, bid_vol, ask_vol


class Trader:
    def run(self, state: TradingState):
        try:
            mem = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            mem = {}
        ema      = mem.get("ema", {})
        ema_fast = mem.get("ema_fast", {})

        result: Dict[str, List[Order]] = {}
        positions = state.position or {}

        for sym in ("HYDROGEL_PACK", "VELVETFRUIT_EXTRACT"):
            od = state.order_depths.get(sym)
            if od is None:
                continue
            bb, ba, bv, av = _best_prices(od)
            if bb is None or ba is None:
                continue

            mid    = 0.5 * (bb + ba)
            anchor = ANCHORS[sym]

            # Slow EMA — fair-value / alpha generation.
            prev_ema = ema.get(sym, anchor)
            new_ema  = (1 - EMA_ALPHA) * prev_ema + EMA_ALPHA * mid
            ema[sym] = new_ema

            # Fast EMA — regime / circuit-breaker signal.
            prev_fast = ema_fast.get(sym, anchor)
            new_fast  = (1 - EMA_FAST_ALPHA) * prev_fast + EMA_FAST_ALPHA * mid
            ema_fast[sym] = new_fast

            pos = positions.get(sym, 0)
            lim = LIMIT[sym]

            fair = ANCHOR_WEIGHT * anchor + (1 - ANCHOR_WEIGHT) * new_ema - INVENTORY_SKEW * pos

            buy_is_unwind  = pos < 0
            sell_is_unwind = pos > 0

            # Fast-EMA circuit breaker: drift in same direction as position
            # means the market is trending against mean-reversion and we're
            # already exposed — halve ADD_CAP_HI.
            fast_drift = new_fast - anchor
            drift_amplifies = (fast_drift >  EMA_DRIFT_LIMIT and pos > 0) or \
                              (fast_drift < -EMA_DRIFT_LIMIT and pos < 0)
            effective_add_hi = max(5, ADD_CAP_HI // 2) if drift_amplifies else ADD_CAP_HI

            add_cap  = ADD_CAP_LO if abs(pos) < ADD_THROTTLE_AT else effective_add_hi
            buy_cap  = UNWIND_CAP_PER_TICK if buy_is_unwind  else add_cap
            sell_cap = UNWIND_CAP_PER_TICK if sell_is_unwind else add_cap
            buy_thr  = TAKE_THRESHOLD if buy_is_unwind  else TAKE_THRESHOLD + THRESHOLD_PER_POS * abs(pos)
            sell_thr = TAKE_THRESHOLD if sell_is_unwind else TAKE_THRESHOLD + THRESHOLD_PER_POS * abs(pos)

            # Effective position ceiling: SOFT_CAP_HI when adding, hard lim when unwinding.
            buy_limit  = SOFT_CAP_HI if not buy_is_unwind  else lim
            sell_limit = SOFT_CAP_HI if not sell_is_unwind else lim

            orders: List[Order] = []
            taken_buy = 0
            taken_sell = 0

            if ba <= fair - buy_thr and pos < buy_limit:
                qty = min(av, buy_limit - pos, buy_cap)
                if qty > 0:
                    orders.append(Order(sym, ba, qty))
                    taken_buy += qty

            if bb >= fair + sell_thr and pos > -sell_limit:
                qty = min(bv, sell_limit + pos, sell_cap)
                if qty > 0:
                    orders.append(Order(sym, bb, -qty))
                    taken_sell += qty

            if od.sell_orders:
                for px in sorted(od.sell_orders.keys()):
                    if px == ba:
                        continue
                    if px <= fair - buy_thr and (pos + taken_buy) < buy_limit:
                        avail = -od.sell_orders[px]
                        room  = min(buy_limit - pos - taken_buy, buy_cap - taken_buy)
                        qty   = min(avail, room)
                        if qty > 0:
                            orders.append(Order(sym, px, qty))
                            taken_buy += qty
                    else:
                        break

            if od.buy_orders:
                for px in sorted(od.buy_orders.keys(), reverse=True):
                    if px == bb:
                        continue
                    if px >= fair + sell_thr and (pos - taken_sell) > -sell_limit:
                        avail = od.buy_orders[px]
                        room  = min(sell_limit + pos - taken_sell, sell_cap - taken_sell)
                        qty   = min(avail, room)
                        if qty > 0:
                            orders.append(Order(sym, px, -qty))
                            taken_sell += qty
                    else:
                        break

            if orders:
                result[sym] = orders

        mem["ema"]      = ema
        mem["ema_fast"] = ema_fast
        traderData = json.dumps(mem)
        if len(traderData) > 45000:
            traderData = json.dumps({"ema": ema, "ema_fast": ema_fast})

        return result, 0, traderData
