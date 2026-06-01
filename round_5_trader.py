"""
Round 5: Cherry Picking Winners  (v3 — updated from live submission 570570)

5-day evidence (D2/D3/D4 training + live Day 4 + live Day 5):

  5/5 DAYS CONSISTENT (highest confidence):
    OXYGEN_SHAKE_GARLIC +1 (+1828, +111, +1958, +278, +2708)  all up
    UV_VISOR_RED        +1 (+842, +182, +698, +593, +5856)     all up

  4/5 DAYS CONSISTENT:
    MICROCHIP_OVAL   -1  (-744, -1824, -1898, -452, +4480)   4 down, D5 reversed
    PEBBLES_XS       -1  (-1952, -1204, -824, -190, +1858)   4 down, D5 reversed
    PEBBLES_S        -1  (-840, -177, -937, -559, +5534)     4 down, D5 reversed
    UV_VISOR_AMBER   -1  (-1500, -1109, -255, -422, +4164)   4 down, D5 reversed
    PEBBLES_XL       +1  (+3674, -1552, +4014, +964, +9561)  4 up, D3 exception
    SLEEP_POD_COTTON +1  (+1123, +1076, -784, +721, +7158)   4 up, D4 exception

  3/5 DAYS CONSISTENT:
    GALAXY_SOUNDS_BLACK_HOLES +1 (+1446, +688, +1320, -65, -732)  ⚠ last 2 live = down
    ROBOT_LAUNDRY             -1 (+252, -752, -219, -524, +5205)   ⚠ mixed, live D5 up

  NEW 3/3 TRAINING CONSISTENT (added v3):
    SNACKPACK_PISTACHIO  -1  (-489, -124, -282)  all down
    SNACKPACK_STRAWBERRY +1  (+436, +358, +98)   all up
    SLEEP_POD_LAMB_WOOL  +1  (+404, +396, +16)   all up (D4 barely)
    SNACKPACK_CHOCOLATE  -1  (-84, -75, -182)    all down (small drift)

  REMOVED FROM v1:
    MICROCHIP_SQUARE — 2/4 (D3 +3438, D4 -2278, live -478 = two consecutive losses)
    PANEL_2X4        — 3/4 but +738/+738 in training was a data artifact; live reversed

  Training backtest: v2=310,024 → v3=338,556 (+28,532) → v4=318,724 (+8,700 vs v2)
  Live performance: Day 4=+25,010 (v1), Day 5=+45,696 (v2/v3-same-day), v3=-6,721 vs v2
  Live attribution: LAMB_WOOL=-5954, CHOC=-842, STRAW=-199, PISTACHIO=+273 → dropped 3
"""

from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List, Optional
import json
import math

LIMIT = 10  # position limit for all products

EMA_ALPHA = 0.02   # fast EMA for tracking current price

# Directional: maintain ±LIMIT position aggressively
TREND_CONFIG: Dict[str, int] = {
    # ALL 4 DAYS CONSISTENT (6.25% chance by luck alone)
    "MICROCHIP_OVAL":              -1,   # -744, -1824, -1898, -452/day
    "PEBBLES_XS":                  -1,   # -1952, -1204, -824, -190/day
    "PEBBLES_S":                   -1,   # -840, -177, -937, -559/day
    "UV_VISOR_AMBER":              -1,   # -1500, -1109, -255, -422/day
    "OXYGEN_SHAKE_GARLIC":         +1,   # +1828, +111, +1958, +278/day
    "UV_VISOR_RED":                +1,   # +842, +182, +698, +593/day
    # 3/5 DAYS CONSISTENT (kept — training strong, live mixed)
    # GALAXY_SOUNDS_BLACK_HOLES removed: MM sim consistently negative (trending product,
    # adverse inventory accumulation), directional +1 failed 2/2 live days, -1 would be
    # 2-point overfit. No edge available without more live data.
    "PEBBLES_XL":                  +1,   # +3674,-1552,+4014,+964,+9561 (large net positive)
    "SLEEP_POD_COTTON":            +1,   # +1123,+1076,-784,+721,+7158 (live confirms)
    "ROBOT_LAUNDRY":               -1,   # +252,-752,-219,-524,+5205 (net down)
    # NEW v3: 3/3 TRAINING CONSISTENT
    "SNACKPACK_PISTACHIO":         -1,   # -489,-124,-282/day  live D5=+273 ✓
    # REMOVED after v3 live test (same day as v2):
    #   SLEEP_POD_LAMB_WOOL  +1  — D4 training drift only +16, live D5=-5,954
    #   SNACKPACK_CHOCOLATE  -1  — live D5=-842
    #   SNACKPACK_STRAWBERRY +1  — live D5=-199, declining training trend
}

# Mean-reversion MM: small passive quotes, tight inventory cap (±3)
# Only ROBOT_DISHES has reliably positive MM PnL across all 3 days (net +5.7K).
# ROBOT_IRONING lost -5K net; OXYGEN_SHAKE_EVENING_BREATH marginally positive.
MM_CONFIG: Dict[str, int] = {
    # AC1=-0.232 combined (Day4=-0.29). inv_cap=7 is 3.6x better than cap=3 in
    # training sim — improvement is monotonic across all 3 days (not day-specific),
    # driven by fewer inventory-cap truncations. D4 degrades past cap=8 so 7 is sweet spot.
    "ROBOT_DISHES":  7,
}
MM_SLOW_ALPHA  = 0.001  # very slow EMA for MM fair value
MM_MAKE_EDGE   = 4      # ticks outside current best to place passive quotes
MM_MAKE_SIZE   = 2      # small size per order


class Trader:
    def run(self, state: TradingState):
        try:
            mem = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            mem = {}

        ema: Dict[str, float] = mem.get("ema", {})
        mm_ema: Dict[str, float] = mem.get("mm_ema", {})
        result: Dict[str, List[Order]] = {}
        positions = state.position or {}

        for sym, od in state.order_depths.items():
            bb = max(od.buy_orders)  if od.buy_orders  else None
            ba = min(od.sell_orders) if od.sell_orders else None
            if bb is None and ba is None:
                continue

            if bb is not None and ba is not None:
                mid = (bb + ba) / 2.0
            elif bb is not None:
                mid = float(bb)
            else:
                mid = float(ba)  # type: ignore[arg-type]

            pos = positions.get(sym, 0)
            direction = TREND_CONFIG.get(sym, 0)

            if direction != 0:
                # Fast EMA for directional products (tracks current level)
                prev = ema.get(sym, mid)
                ema[sym] = (1 - EMA_ALPHA) * prev + EMA_ALPHA * mid
                orders = _directional(sym, od, pos, direction, bb, ba)
                if orders:
                    result[sym] = orders

            elif sym in MM_CONFIG:
                # Slow EMA as fair value anchor for mean-reversion MM
                prev = mm_ema.get(sym, mid)
                slow_fair = (1 - MM_SLOW_ALPHA) * prev + MM_SLOW_ALPHA * mid
                mm_ema[sym] = slow_fair
                orders = _mm_reversion(sym, od, slow_fair, pos, bb, ba,
                                       MM_CONFIG[sym])
                if orders:
                    result[sym] = orders
            # All other products: skip (no orders)

        mem["ema"] = ema
        mem["mm_ema"] = mm_ema
        td = json.dumps(mem)
        return result, 0, td


def _directional(
    sym: str,
    od: OrderDepth,
    pos: int,
    direction: int,
    bb: Optional[int],
    ba: Optional[int],
) -> List[Order]:
    """
    Get to ±LIMIT as fast as possible.
    Once at target: hold, no orders.
    Passive leg placed inside spread (ba-1 or bb+1) for max fill rate.
    """
    target = LIMIT * direction
    need = target - pos  # >0 = need buys, <0 = need sells
    orders: List[Order] = []

    if need > 0 and od.sell_orders:
        remaining = need
        for px in sorted(od.sell_orders):
            if remaining <= 0:
                break
            q = min(-od.sell_orders[px], remaining)
            if q > 0:
                orders.append(Order(sym, px, q))
                remaining -= q
        if remaining > 0:
            px = _passive_bid(bb, ba)
            if px is not None:
                orders.append(Order(sym, px, remaining))

    elif need < 0 and od.buy_orders:
        remaining = -need
        for px in sorted(od.buy_orders, reverse=True):
            if remaining <= 0:
                break
            q = min(od.buy_orders[px], remaining)
            if q > 0:
                orders.append(Order(sym, px, -q))
                remaining -= q
        if remaining > 0:
            px = _passive_ask(bb, ba)
            if px is not None:
                orders.append(Order(sym, px, -remaining))

    return orders


def _mm_reversion(
    sym: str,
    od: OrderDepth,
    fair: float,
    pos: int,
    bb: Optional[int],
    ba: Optional[int],
    inv_cap: int,
) -> List[Order]:
    """
    Tight mean-reversion MM for strongly mean-reverting products.
    Uses a SLOW EMA (set by caller) as fair value so it doesn't chase price.
    Inventory capped at ±inv_cap to limit exposure to adverse trends.
    Only places passive orders; never crosses the spread.
    """
    orders: List[Order] = []
    if bb is None or ba is None:
        return orders

    # Lean against inventory
    adj = fair - pos * 0.3

    buy_room  = inv_cap - pos
    sell_room = inv_cap + pos

    my_bid = math.floor(adj - MM_MAKE_EDGE)
    my_ask = math.ceil(adj + MM_MAKE_EDGE)

    # Clamp to not cross spread
    if my_bid >= ba:  my_bid = ba - 1
    if my_ask <= bb:  my_ask = bb + 1
    if my_ask <= my_bid: my_ask = my_bid + 1

    if my_bid > 0 and buy_room > 0:
        orders.append(Order(sym, my_bid, min(buy_room, MM_MAKE_SIZE)))
    if sell_room > 0:
        orders.append(Order(sym, my_ask, -min(sell_room, MM_MAKE_SIZE)))

    return orders


def _passive_bid(bb: Optional[int], ba: Optional[int]) -> Optional[int]:
    """Best passive bid price: inside spread if room, else best bid."""
    if ba is not None and bb is not None and ba > bb + 1:
        return ba - 1
    if bb is not None:
        return bb
    if ba is not None:
        return ba - 1
    return None


def _passive_ask(bb: Optional[int], ba: Optional[int]) -> Optional[int]:
    """Best passive ask price: inside spread if room, else best ask."""
    if bb is not None and ba is not None and ba > bb + 1:
        return bb + 1
    if ba is not None:
        return ba
    if bb is not None:
        return bb + 1
    return None
