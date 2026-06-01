"""
Round 3 trading algorithm — v4 (lower-drawdown v3).

v3 baseline backtest (round 3, days 0/1/2):
  PnL = +151k, summed intra-day max-DD = -65k. Ratio 43%.

The path damage in v3 came from two places: (a) the 5-tick TAKE_THRESHOLD was
too thin — many "edges" we lifted were just noise, and we paid spread crossing
both ways; (b) at large inventory the 0.10 skew shifted fair only ~20 ticks,
which was not enough to deter adding when the local trend was against us, so
the limit pinned hard against drift. Sweep (sweep_v4.py) confirms both.

v4 changes (anchor / EMA / weighting unchanged — that IS the alpha):

  1. TAKE_THRESHOLD 5 -> 9.  The wider edge filters noise; we trade fewer
     ticks but each one survives spread costs. Backtest PnL goes UP, not
     down — the 5-tick fills were net-negative-EV.
  2. INVENTORY_SKEW 0.10 -> 0.13.  Fair shifts ~26 ticks at full position
     (was ~20), making it materially harder to grow inventory once it's
     already large.
  3. Two-stage taker cap when ADDING: ADD_CAP_LO=30 below |pos|=100,
     ADD_CAP_HI=15 above. Builds the position fast while we still have
     headroom, then throttles into the high-risk zone where each new fill
     contributes most to potential mark-to-market loss.

Sweep result (sweep_v4_refine.py):
  PnL  +162k   (v3: +151k,  +7%)
  DD   -59k    (v3: -65k,   -9%)
  ratio 36.6%  (v3: 43.1%)
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

# Alpha — unchanged from v3.
ANCHOR_WEIGHT  = 0.7
EMA_ALPHA      = 0.0005

# v4 path controls.
TAKE_THRESHOLD       = 9
INVENTORY_SKEW       = 0.13
THRESHOLD_PER_POS    = 0.04   # +1 tick per 25 units of |pos| when adding
ADD_CAP_LO           = 30     # taker cap when |pos| < ADD_THROTTLE_AT
ADD_CAP_HI           = 15     # taker cap when |pos| >= ADD_THROTTLE_AT
ADD_THROTTLE_AT      = 100
UNWIND_CAP_PER_TICK  = 200    # no cap when reducing toward flat


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
        ema = mem.get("ema", {})

        result: Dict[str, List[Order]] = {}
        positions = state.position or {}

        for sym in ("HYDROGEL_PACK", "VELVETFRUIT_EXTRACT"):
            od = state.order_depths.get(sym)
            if od is None:
                continue
            bb, ba, bv, av = _best_prices(od)
            if bb is None or ba is None:
                continue

            mid = 0.5 * (bb + ba)
            anchor = ANCHORS[sym]
            prev_ema = ema.get(sym, anchor)
            new_ema = (1 - EMA_ALPHA) * prev_ema + EMA_ALPHA * mid
            ema[sym] = new_ema

            pos = positions.get(sym, 0)
            lim = LIMIT[sym]

            fair = ANCHOR_WEIGHT * anchor + (1 - ANCHOR_WEIGHT) * new_ema - INVENTORY_SKEW * pos

            buy_is_unwind  = pos < 0
            sell_is_unwind = pos > 0
            add_cap = ADD_CAP_LO if abs(pos) < ADD_THROTTLE_AT else ADD_CAP_HI
            buy_cap  = UNWIND_CAP_PER_TICK if buy_is_unwind  else add_cap
            sell_cap = UNWIND_CAP_PER_TICK if sell_is_unwind else add_cap
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

            if od.sell_orders:
                for px in sorted(od.sell_orders.keys()):
                    if px == ba:
                        continue
                    if px <= fair - buy_thr and (pos + taken_buy) < lim:
                        avail = -od.sell_orders[px]
                        room = min(lim - pos - taken_buy, buy_cap - taken_buy)
                        qty = min(avail, room)
                        if qty > 0:
                            orders.append(Order(sym, px, qty))
                            taken_buy += qty
                    else:
                        break

            if od.buy_orders:
                for px in sorted(od.buy_orders.keys(), reverse=True):
                    if px == bb:
                        continue
                    if px >= fair + sell_thr and (pos - taken_sell) > -lim:
                        avail = od.buy_orders[px]
                        room = min(lim + pos - taken_sell, sell_cap - taken_sell)
                        qty = min(avail, room)
                        if qty > 0:
                            orders.append(Order(sym, px, -qty))
                            taken_sell += qty
                    else:
                        break

            if orders:
                result[sym] = orders

        mem["ema"] = ema
        traderData = json.dumps(mem)
        if len(traderData) > 45000:
            traderData = json.dumps({"ema": ema})

        conversions = 0
        return result, conversions, traderData