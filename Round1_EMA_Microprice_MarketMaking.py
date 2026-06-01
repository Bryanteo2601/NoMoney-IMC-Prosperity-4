from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import jsonpickle


POSITION_LIMIT: Dict[str, int] = {
    "ASH_COATED_OSMIUM": 80,
    "INTARIAN_PEPPER_ROOT": 80,
}

# Per-product config.
#   ema_fast / ema_slow:  smoothing factors. drift = fast - slow.
#   edge:         min distance from fair to take an order
#   make_edge:    distance from fair at which we post passive quotes
#   drift_scale:  0 disables drift bias. Otherwise, target_pos = drift * scale (clamped).
#   drift_boost:  how much the take-edge widens/narrows when we want inventory
PRODUCT_CFG: Dict[str, dict] = {
    "ASH_COATED_OSMIUM":    {"ema_fast": 0.15, "ema_slow": 0.05, "edge": 1, "make_edge": 1, "drift_scale": 0, "drift_boost": 0,
                             "quote_frac": 0.5, "fixed_fair": 10000},
    "INTARIAN_PEPPER_ROOT": {"ema_fast": 0.3, "ema_slow": 0.03, "edge": 1, "make_edge": 1, "drift_scale": 0,  "drift_boost": 0,
                             "buy_only": True},
}

DEFAULT_CFG = {"ema_fast": 0.3, "ema_slow": 0.3, "edge": 1, "make_edge": 1, "drift_scale": 0, "drift_boost": 0}
DEFAULT_LIMIT = 80


class Trader:

    def bid(self):
        return 15

    def run(self, state: TradingState):
        memory = self._load_memory(state.traderData)
        result: Dict[str, List[Order]] = {}

        for product, depth in state.order_depths.items():
            cfg = PRODUCT_CFG.get(product, DEFAULT_CFG)
            limit = POSITION_LIMIT.get(product, DEFAULT_LIMIT)
            pos = state.position.get(product, 0)

            fair, drift = self._fair_and_drift(product, depth, cfg, memory)
            if fair is None:
                result[product] = []
                continue

            # Drift → desired inventory target, clamped to the position limit.
            target = 0
            if cfg.get("buy_only"):
                target = limit  # always max long
            elif cfg["drift_scale"]:
                target = max(-limit, min(limit, int(round(drift * cfg["drift_scale"]))))

            orders = self._make_orders(product, depth, fair, pos, target, limit, cfg)
            result[product] = orders

        trader_data = jsonpickle.encode(memory)
        conversions = 0
        return result, conversions, trader_data

    # ── fair value + drift ──────────────────────────────────────
    def _fair_and_drift(self, product, depth: OrderDepth, cfg, memory):
        if not depth.sell_orders or not depth.buy_orders:
            return None, 0.0

        # Fixed fair bypasses EMA entirely — use hardcoded true price.
        if "fixed_fair" in cfg:
            return cfg["fixed_fair"], 0.0

        best_ask = min(depth.sell_orders)
        best_bid = max(depth.buy_orders)
        ask_vol = -depth.sell_orders[best_ask]
        bid_vol =  depth.buy_orders[best_bid]
        min_bvol = cfg.get("min_book_vol", 0)
        if min_bvol and (ask_vol < min_bvol or bid_vol < min_bvol):
            return None, 0.0
        denom = ask_vol + bid_vol
        mid = (best_ask + best_bid) / 2
        # Microprice: volume-weighted toward the heavier side.
        micro = (best_bid * ask_vol + best_ask * bid_vol) / denom if denom else mid

        kf, ks = f"fast_{product}", f"slow_{product}"
        prev_fast = memory.get(kf)
        prev_slow = memory.get(ks)

        af = cfg["ema_fast"]
        asw = cfg["ema_slow"]
        fast = micro if prev_fast is None else af  * micro + (1 - af)  * prev_fast
        slow = micro if prev_slow is None else asw * micro + (1 - asw) * prev_slow

        memory[kf] = fast
        memory[ks] = slow

        # Fast EMA is the short-term fair value; (fast - slow) is the drift signal.
        return fast, fast - slow

    # ── order generation ────────────────────────────────────────
    def _make_orders(self, product, depth: OrderDepth, fair: float,
                     pos: int, target: int, limit: int, cfg) -> List[Order]:
        orders: List[Order] = []
        edge = cfg["edge"]
        make_edge = cfg["make_edge"]
        boost = cfg["drift_boost"]
        buy_only = cfg.get("buy_only", False)

        buy_capacity  = limit - pos
        sell_capacity = limit + pos

        # ── BUY-ONLY mode (e.g. buy-and-hold + dip accumulation) ──
        if buy_only:
            if buy_capacity <= 0:
                return orders

            best_ask = min(depth.sell_orders) if depth.sell_orders else None
            best_bid = max(depth.buy_orders)  if depth.buy_orders  else None
            if best_ask is None or best_bid is None:
                return orders

            # Always sweep all available asks aggressively (trending market)
            for ask_px in sorted(depth.sell_orders):
                if buy_capacity <= 0:
                    break
                vol = min(-depth.sell_orders[ask_px], buy_capacity)
                if vol > 0:
                    orders.append(Order(product, ask_px, vol))
                    buy_capacity -= vol

            return orders

        # ── NORMAL market-making mode ──────────────────────────────
        # Inventory gap: how far current pos is from where drift says we should be.
        # Positive gap → we want to buy more (we're under target).
        gap = target - pos
        urgency = gap / limit  # roughly in [-1, 1]

        # Widen/narrow take-edge based on urgency. Chasing drift → pay up for inventory.
        buy_edge  = edge - urgency * boost  # more negative = willing to buy higher
        sell_edge = edge + urgency * boost  # more positive = reluctant to sell low

        # 1) TAKE: cross mispriced liquidity.
        for ask_px in sorted(depth.sell_orders):
            if ask_px > fair - buy_edge or buy_capacity <= 0:
                break
            vol = min(-depth.sell_orders[ask_px], buy_capacity)
            if vol > 0:
                orders.append(Order(product, ask_px, vol))
                buy_capacity -= vol

        for bid_px in sorted(depth.buy_orders, reverse=True):
            if bid_px < fair + sell_edge or sell_capacity <= 0:
                break
            vol = min(depth.buy_orders[bid_px], sell_capacity)
            if vol > 0:
                orders.append(Order(product, bid_px, -vol))
                sell_capacity -= vol

        # 2) MAKE: post passive quotes around fair, shifted by urgency.
        best_ask = min(depth.sell_orders) if depth.sell_orders else None
        best_bid = max(depth.buy_orders)  if depth.buy_orders  else None
        if best_bid is None or best_ask is None:
            return orders

        # Thin/unstable book: skip passive quotes if spread is too wide.
        spread = best_ask - best_bid
        max_spread = cfg.get("max_spread", 0)
        if max_spread and spread > max_spread:
            return orders

        # Baseline inventory skew (1 tick) keeps us mean-reverting to target even
        # when drift_boost=0. Adds the drift-urgency shift on top.
        base_skew = 0
        if   pos - target >  limit * 0.5: base_skew = -1
        elif pos - target < -limit * 0.5: base_skew =  1
        shift = base_skew + int(round(urgency * boost))

        quote_frac = cfg.get("quote_frac", 1.0)

        if buy_capacity > 0:
            my_bid = min(best_bid + 1, int(fair - make_edge)) + shift
            qty = max(1, int(buy_capacity * quote_frac))
            orders.append(Order(product, my_bid, qty))

        if sell_capacity > 0:
            my_ask = max(best_ask - 1, int(fair + make_edge) + 1) + shift
            qty = max(1, int(sell_capacity * quote_frac))
            orders.append(Order(product, my_ask, -qty))

        return orders

    # ── persistence ─────────────────────────────────────────────
    def _load_memory(self, raw: str) -> dict:
        if not raw:
            return {}
        try:
            return jsonpickle.decode(raw)
        except Exception:
            return {}