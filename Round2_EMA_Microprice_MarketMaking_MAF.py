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
    "ASH_COATED_OSMIUM": {
        "ema_fast": 0.15,
        "ema_slow": 0.05,
        "edge": 1,           # take: bid > fair+1 (sell) or ask < fair-1 (buy)
        "make_edge": 2,      # quote: bid at fair-2, ask at fair+3
        "drift_scale": 0,
        "drift_boost": 0,
        "quote_frac": 1.0,
        "center_quote": False,  # penny: bid at best_bid+1, ask at best_ask-1
        "arb_mode": True,      # exploit any crossed-book opportunities
        "max_take_size": 80,
        "max_quote_size": 80,
        "max_spread": 30,
    },
    "INTARIAN_PEPPER_ROOT": {
        # Slower EMAs: fast tracks ~30-tick half-life, slow ~115-tick half-life.
        # Slow EMA is the trend MA used for dip detection.
        "ema_fast": 0.08,
        "ema_slow": 0.02,
        "edge": 2,
        "make_edge": 2,
        "drift_scale": 0,
        "drift_boost": 0,
        "no_short": True,

        "long_only": True,
        "aggressive_accumulate": True,  # always buy at best ask until at position limit
        "dip_buy_ma": True,             # also buy at lowest ask when ask < slow MA
        "exit_threshold": -0.05,        # trim only if trend clearly reverses
        "exit_edge": 2,
        "max_take_size": 20,            # fill as much as possible on a dip
        "max_quote_size": 10,
        "max_spread": 20,
    },
}

DEFAULT_CFG = {"ema_fast": 0.3, "ema_slow": 0.3, "edge": 1, "make_edge": 1, "drift_scale": 0, "drift_boost": 0}
DEFAULT_LIMIT = 80


class Trader:

    # Market Access Fee (MAF) bid in seashells.
    # Full round = 3 days × 10k ticks. ACO base ~43k → +25% extra ≈ +10k.
    # Bid high enough to guarantee top-50% win; breakeven is ~10k.
    MAF = 8_000

    def bid(self):
        return self.MAF

    def run(self, state: TradingState):
        memory = self._load_memory(state.traderData)
        result: Dict[str, List[Order]] = {}

        for product, depth in state.order_depths.items():
            cfg = PRODUCT_CFG.get(product, DEFAULT_CFG)
            limit = POSITION_LIMIT.get(product, DEFAULT_LIMIT)
            pos = state.position.get(product, 0)

            fair, drift, slow_ema = self._fair_and_drift(product, depth, cfg, memory)
            if fair is None:
                result[product] = []
                continue

            # Drift → desired inventory target, clamped to the position limit.
            target = 0

            if cfg.get("long_only"):
                # Buy-and-hold: always target max long.
                target = limit

            elif cfg["drift_scale"]:
                target = max(-limit, min(limit, int(round(drift * cfg["drift_scale"]))))
                if cfg.get("no_short"):
                    target = max(0, target)

            # Expose current drift and slow MA to _make_orders via a mutable copy of cfg.
            cfg = dict(cfg)
            cfg["_current_drift"] = drift
            cfg["_slow_ema"] = slow_ema

            orders = self._make_orders(product, depth, fair, pos, target, limit, cfg)
            result[product] = orders

        trader_data = jsonpickle.encode(memory)
        conversions = 0
        return result, conversions, trader_data

    # ── fair value + drift ──────────────────────────────────────
    def _fair_and_drift(self, product, depth: OrderDepth, cfg, memory):
        if not depth.sell_orders or not depth.buy_orders:
            return None, 0.0, 0.0

        # Fixed fair bypasses EMA entirely — use hardcoded true price.
        if "fixed_fair" in cfg:
            return cfg["fixed_fair"], 0.0, cfg["fixed_fair"]

        best_ask = min(depth.sell_orders)
        best_bid = max(depth.buy_orders)
        ask_vol = -depth.sell_orders[best_ask]
        bid_vol =  depth.buy_orders[best_bid]
        min_bvol = cfg.get("min_book_vol", 0)
        if min_bvol and (ask_vol < min_bvol or bid_vol < min_bvol):
            return None, 0.0, 0.0
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

        drift = fast - slow
        memory[f"last_drift_{product}"] = drift

        # Fast EMA is the short-term fair value; slow EMA is the trend MA.
        return fast, drift, slow

    # ── order generation ────────────────────────────────────────
    def _make_orders(self, product, depth: OrderDepth, fair: float,
                     pos: int, target: int, limit: int, cfg) -> List[Order]:
        orders: List[Order] = []
        edge = cfg["edge"]
        make_edge = cfg["make_edge"]
        boost = cfg["drift_boost"]
        long_only = cfg.get("long_only", False)

        buy_capacity  = limit - pos
        sell_capacity = limit + pos

        best_ask = min(depth.sell_orders) if depth.sell_orders else None
        best_bid = max(depth.buy_orders)  if depth.buy_orders  else None
        if best_ask is None or best_bid is None:
            return orders

        spread = best_ask - best_bid
        max_spread = cfg.get("max_spread", 0)
        if max_spread and spread > max_spread:
            return orders

        # ── CROSSED BOOK ARB: buy cheap ask / sell rich bid simultaneously ──
        if cfg.get("arb_mode"):
            asks_sorted = sorted(depth.sell_orders)
            bids_sorted = sorted(depth.buy_orders, reverse=True)
            max_arb = cfg.get("max_take_size", limit)
            ai = bi = 0
            while (ai < len(asks_sorted) and bi < len(bids_sorted)
                   and buy_capacity > 0 and sell_capacity > 0):
                apx = asks_sorted[ai]
                bpx = bids_sorted[bi]
                if bpx < apx:
                    break
                avol = -depth.sell_orders[apx]
                bvol =  depth.buy_orders[bpx]
                vol = min(avol, bvol, buy_capacity, sell_capacity, max_arb)
                if vol > 0:
                    orders.append(Order(product, apx,  vol))   # buy cheap
                    orders.append(Order(product, bpx, -vol))   # sell rich
                    buy_capacity  -= vol
                    sell_capacity -= vol
                ai += 1
                bi += 1

        # ── LONG-ONLY BUY-AND-HOLD / DIP-BUY MODE ───────────────────
        if long_only:
            exit_edge      = cfg.get("exit_edge", edge)
            max_take_size  = cfg.get("max_take_size", 10)
            max_quote_size = cfg.get("max_quote_size", 10)
            exit_threshold = cfg.get("exit_threshold", 0.0)
            dip_buy_ma     = cfg.get("dip_buy_ma", False)
            slow_ema       = cfg.get("_slow_ema", fair)

            gap = target - pos

            # 1a) Aggressive accumulate: take at best ask whenever below position limit.
            if cfg.get("aggressive_accumulate") and buy_capacity > 0:
                for ask_px in sorted(depth.sell_orders):
                    if buy_capacity <= 0:
                        break
                    vol = min(-depth.sell_orders[ask_px], buy_capacity, max_take_size)
                    if vol > 0:
                        orders.append(Order(product, ask_px, vol))
                        buy_capacity -= vol

            # 1b) Dip-buy: ask < slow MA → take at the lowest ask price (belt-and-suspenders).
            elif dip_buy_ma and buy_capacity > 0 and best_ask < slow_ema:
                lowest_ask = min(depth.sell_orders)
                vol = min(-depth.sell_orders[lowest_ask], buy_capacity, max_take_size)
                if vol > 0:
                    orders.append(Order(product, lowest_ask, vol))
                    buy_capacity -= vol

            # 2) Passive bid to accumulate if still below target.
            if gap > 0 and buy_capacity > 0:
                post_qty = min(buy_capacity, gap, max_quote_size)
                post_bid = min(best_bid + 1, int(round(fair - make_edge)))
                if post_qty > 0:
                    orders.append(Order(product, post_bid, post_qty))

            # 3) Only trim if drift clearly reverses; never short.
            current_drift = cfg.get("_current_drift", 0.0)
            if pos > 0 and current_drift < exit_threshold:
                remaining_to_sell = min(pos, max_quote_size)

                for bid_px in sorted(depth.buy_orders, reverse=True):
                    if remaining_to_sell <= 0:
                        break
                    if bid_px < fair + exit_edge:
                        break
                    vol = min(depth.buy_orders[bid_px], remaining_to_sell)
                    if vol > 0:
                        orders.append(Order(product, bid_px, -vol))
                        remaining_to_sell -= vol
                        sell_capacity -= vol

                if remaining_to_sell > 0:
                    post_ask = max(best_ask - 1, int(round(fair + make_edge)))
                    orders.append(Order(product, post_ask, -remaining_to_sell))

            return orders

        # ── NORMAL market-making mode ──────────────────────────────
        # Inventory gap: how far current pos is from where drift says we should be.
        # Positive gap → we want to buy more (we're under target).
        gap = target - pos
        urgency = gap / limit  # roughly in [-1, 1]

        # Widen/narrow take-edge based on urgency. Chasing drift → pay up for inventory.
        buy_edge  = edge - urgency * boost  # more negative = willing to buy higher
        sell_edge = edge + urgency * boost  # more positive = reluctant to sell low

        max_take = cfg.get("max_take_size", limit)

        # 1) TAKE: cross mispriced liquidity.
        for ask_px in sorted(depth.sell_orders):
            if ask_px > fair - buy_edge or buy_capacity <= 0:
                break
            vol = min(-depth.sell_orders[ask_px], buy_capacity, max_take)
            if vol > 0:
                orders.append(Order(product, ask_px, vol))
                buy_capacity -= vol

        for bid_px in sorted(depth.buy_orders, reverse=True):
            if bid_px < fair + sell_edge or sell_capacity <= 0:
                break
            vol = min(depth.buy_orders[bid_px], sell_capacity, max_take)
            if vol > 0:
                orders.append(Order(product, bid_px, -vol))
                sell_capacity -= vol

        # 2) MAKE: post passive quotes around fair, shifted by urgency.
        if best_bid is None or best_ask is None:
            return orders

        # Thin/unstable book: skip passive quotes if spread is too wide.
        max_spread_mm = cfg.get("max_spread", 0)
        if max_spread_mm and spread > max_spread_mm:
            return orders

        # Baseline inventory skew (1 tick) keeps us mean-reverting to target even
        # when drift_boost=0. Adds the drift-urgency shift on top.
        base_skew = 0
        if   pos - target >  limit * 0.5: base_skew = -1
        elif pos - target < -limit * 0.5: base_skew =  1
        shift = base_skew + int(round(urgency * boost))

        quote_frac = cfg.get("quote_frac", 1.0)
        max_quote_size_mm = cfg.get("max_quote_size", limit)
        layers = cfg.get("quote_layers")

        # Flatten-at-fair: when inventory is lopsided, offer to rotate at exact fair.
        flatten_threshold = cfg.get("flatten_threshold", 0)
        if flatten_threshold and abs(pos - target) > flatten_threshold:
            fair_px = int(round(fair))
            if pos > target and sell_capacity > 0:
                flat_qty = min(sell_capacity, pos - target)
                orders.append(Order(product, fair_px, -flat_qty))
                sell_capacity -= flat_qty
            elif pos < target and buy_capacity > 0:
                flat_qty = min(buy_capacity, target - pos)
                orders.append(Order(product, fair_px, flat_qty))
                buy_capacity -= flat_qty

        if layers:
            sorted_layers = sorted(layers)
            if buy_capacity > 0:
                per_level = max(1, min(max_quote_size_mm, buy_capacity // len(sorted_layers)))
                for i, lvl in enumerate(sorted_layers):
                    if buy_capacity <= 0:
                        break
                    px = int(fair - lvl) + shift
                    if i == 0:
                        px = min(best_bid + 1, px)
                    qty = min(per_level, buy_capacity) if i < len(sorted_layers) - 1 else min(buy_capacity, max_quote_size_mm)
                    if qty > 0:
                        orders.append(Order(product, px, qty))
                        buy_capacity -= qty
            if sell_capacity > 0:
                per_level = max(1, min(max_quote_size_mm, sell_capacity // len(sorted_layers)))
                for i, lvl in enumerate(sorted_layers):
                    if sell_capacity <= 0:
                        break
                    px = int(fair + lvl) + 1 + shift
                    if i == 0:
                        px = max(best_ask - 1, px)
                    qty = min(per_level, sell_capacity) if i < len(sorted_layers) - 1 else min(sell_capacity, max_quote_size_mm)
                    if qty > 0:
                        orders.append(Order(product, px, -qty))
                        sell_capacity -= qty
        else:
            if cfg.get("center_quote"):
                # Quote at fair ± make_edge regardless of current best bid/ask.
                # Captures wide spreads by posting aggressively inside.
                if buy_capacity > 0:
                    my_bid = int(fair - make_edge) + shift
                    qty = min(max_quote_size_mm, max(1, int(buy_capacity * quote_frac)))
                    orders.append(Order(product, my_bid, qty))
                if sell_capacity > 0:
                    my_ask = int(fair + make_edge) + 1 + shift
                    qty = min(max_quote_size_mm, max(1, int(sell_capacity * quote_frac)))
                    orders.append(Order(product, my_ask, -qty))
            else:
                if buy_capacity > 0:
                    my_bid = min(best_bid + 1, int(fair - make_edge)) + shift
                    qty = min(max_quote_size_mm, max(1, int(buy_capacity * quote_frac)))
                    orders.append(Order(product, my_bid, qty))

                if sell_capacity > 0:
                    my_ask = max(best_ask - 1, int(fair + make_edge) + 1) + shift
                    qty = min(max_quote_size_mm, max(1, int(sell_capacity * quote_frac)))
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