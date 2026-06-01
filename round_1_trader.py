from dataclasses import dataclass
from typing import Dict, List, Any, Optional
import json, math
from collections import Counter

# ─────────────────────────────────────────────────────────────────────────────
# IMC Prosperity boilerplate types  (local back-testing; the competition SDK
# provides these same classes via `from datamodel import ...`)
# ─────────────────────────────────────────────────────────────────────────────
Symbol = str

@dataclass
class Order:
    symbol: Symbol
    price: int
    quantity: int       # positive = buy, negative = sell

@dataclass
class Trade:
    symbol: Symbol
    price: float
    quantity: int
    buyer: str = ""
    seller: str = ""
    timestamp: int = 0

@dataclass
class OrderDepth:
    buy_orders: Dict[int, int]    # price → volume (positive)
    sell_orders: Dict[int, int]   # price → volume (negative)

@dataclass
class TradingState:
    timestamp: int
    listings: Dict[Symbol, Any]
    order_depths: Dict[Symbol, OrderDepth]
    own_trades: Dict[Symbol, List[Trade]]
    market_trades: Dict[Symbol, List[Trade]]
    position: Dict[Symbol, int]
    observations: Any
    traderData: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# ASH_COATED_OSMIUM configuration  (Avellaneda-Stoikov market making)
# ─────────────────────────────────────────────────────────────────────────────
ACO_CONFIG: dict = {
    # FV: fixed anchor at 10000 with light EMA correction
    "fv_mode":              "fixed_anchor",
    "fv_anchor":            10_000.0,
    "fv_anchor_weight":     0.95,         # 95% anchor, 5% EMA
    "fv_ema_alpha":         0.02,         # slow EMA for the correction term
    "fv_seed":              10_000.0,

    # Position limits
    "position_limit":       50,
    "soft_limit":           30,
    "max_order_size":       10,

    # Avellaneda-Stoikov spread parameters
    "gamma":                0.08,
    "kappa_init":           0.45,
    "kappa_alpha":          0.0,
    "kappa_min":            0.45,
    "kappa_max":            0.45,
    "inventory_skew":       0.4,          # lighter skew — range-bound, less drift risk

    # Taking
    "take_threshold":       4,            # ACO is range-bound, rarely mispriced by 4+

    # End-of-day urgency
    "eod_threshold":        0.95,
    "eod_pos_min":          3,

    # Spread overrides
    "fixed_half_spread":    8,            # target half-spread 8 → total spread 16 (matches market)
    "use_fixed_spread":     True,

    # Order flow
    "flow_alpha":           0.2,
    "flow_skew_scale":      0.0,
    "flow_take_threshold":  999.0,
    "flow_take_boost":      0,
    "flow_vol_scale":       0.02,

    # Momentum / mean-reversion fade
    "momentum_alpha":       0.3,
    "momentum_scale":       0.5,          # stronger momentum signal for ACO

    # Adverse selection filter
    "adv_sel_threshold":    999.0,        # disabled — bot is not directional
    "adv_sel_min_frac":     1.0,

    # Hard inventory skew
    "hard_skew_ticks":      3,

    # Queue / bot
    "use_queue_shift":      True,
    "queue_thick_vol":      4,
    "bot_min_count":        4,

    # Mean-reversion fade aggressor: if |mid - FV| >= 8, fade the jump
    "fade_threshold":       8,
    "fade_size":            5,
}


# ─────────────────────────────────────────────────────────────────────────────
# INTARIAN_PEPPER_ROOT  —  MOMENTUM / TREND-FOLLOWING STRATEGY
# ─────────────────────────────────────────────────────────────────────────────
#
# This product trends strongly (~+1000/day).  Avellaneda-Stoikov is wrong here.
# We use a pure momentum signal (EMA of tick-to-tick returns) to decide
# direction, then aggressively take liquidity in that direction.
#
# NO fair value anchor.  NO symmetric quoting.  NO signal_flip.
#
class MomentumTrader:
    """
    Trend-following trader for INTARIAN_PEPPER_ROOT.

    Signal: EMA of tick-by-tick mid-price returns.
      - mom_ema > +THRESHOLD  →  buy aggressively (take asks) + passive bid
      - mom_ema < -THRESHOLD  →  sell aggressively (take bids) + passive ask
      - |mom_ema| < THRESHOLD →  do nothing (avoid overtrading on noise)

    Inventory control: order size scales down as position approaches the limit
    in the momentum direction, preventing runaway one-sided exposure.
    """

    POSITION_LIMIT = 50

    # ── tunable parameters ────────────────────────────────────────────────────
    MOM_ALPHA      = 0.10   # EMA alpha for return signal  (~10-tick window)
    MOM_THRESHOLD  = 0.10   # |signal| floor to place any order
    AGG_SIZE       = 8      # aggressive (taker) order size
    PASS_SIZE      = 10     # passive (maker) order size behind the aggressive

    def __init__(self):
        self.last_mid:   float = 0.0
        self.mom_ema:    float = 0.0
        self.initialized: bool = False

    # ── public API ────────────────────────────────────────────────────────────
    def orders(self, product: str, depth: OrderDepth,
               position: int, own_fills: List[Trade],
               market_trades: List[Trade],
               timestamp: int) -> List[Order]:

        mid = self._mid(depth)
        if mid <= 0:
            return []

        if not self.initialized:
            self.last_mid    = mid
            self.initialized = True
            return []

        # ── update momentum signal ────────────────────────────────────────────
        ret          = mid - self.last_mid
        self.mom_ema = (1.0 - self.MOM_ALPHA) * self.mom_ema + self.MOM_ALPHA * ret
        self.last_mid = mid

        lim      = self.POSITION_LIMIT
        buy_cap  = lim - position   # max additional long
        sell_cap = lim + position   # max additional short
        mom      = self.mom_ema
        orders: List[Order] = []

        if mom > self.MOM_THRESHOLD and buy_cap > 0:
            # ── bullish: build long ───────────────────────────────────────────
            # Scale down order size when already carrying a long inventory.
            # inv_factor → 1.0 when flat, → 0.1 when at the limit.
            inv_factor = max(0.1, (lim - max(position, 0)) / lim)

            # Aggressive: lift the ask to enter immediately
            if depth.sell_orders:
                best_ask = min(depth.sell_orders)
                sz = max(1, min(self.AGG_SIZE,
                                buy_cap,
                                round(self.AGG_SIZE * inv_factor)))
                orders.append(Order(product, best_ask, sz))
                buy_cap -= sz

            # Passive: bid one tick above current best bid (but below best ask)
            if buy_cap > 0 and depth.buy_orders and depth.sell_orders:
                best_bid = max(depth.buy_orders)
                best_ask = min(depth.sell_orders)
                bid_price = best_bid + 1
                if bid_price < best_ask:   # only if there is room (spread > 1)
                    sz = max(1, min(self.PASS_SIZE,
                                    buy_cap,
                                    round(self.PASS_SIZE * inv_factor)))
                    orders.append(Order(product, bid_price, sz))

        elif mom < -self.MOM_THRESHOLD and sell_cap > 0:
            # ── bearish: build short ──────────────────────────────────────────
            inv_factor = max(0.1, (lim - max(-position, 0)) / lim)

            # Aggressive: hit the bid to enter immediately
            if depth.buy_orders:
                best_bid = max(depth.buy_orders)
                sz = max(1, min(self.AGG_SIZE,
                                sell_cap,
                                round(self.AGG_SIZE * inv_factor)))
                orders.append(Order(product, best_bid, -sz))
                sell_cap -= sz

            # Passive: ask one tick below current best ask (but above best bid)
            if sell_cap > 0 and depth.buy_orders and depth.sell_orders:
                best_bid = max(depth.buy_orders)
                best_ask = min(depth.sell_orders)
                ask_price = best_ask - 1
                if ask_price > best_bid:   # only if there is room
                    sz = max(1, min(self.PASS_SIZE,
                                    sell_cap,
                                    round(self.PASS_SIZE * inv_factor)))
                    orders.append(Order(product, ask_price, -sz))

        return orders

    # ── helpers ───────────────────────────────────────────────────────────────
    def _mid(self, depth: OrderDepth) -> float:
        if depth.buy_orders and depth.sell_orders:
            return (max(depth.buy_orders) + min(depth.sell_orders)) / 2.0
        return self.last_mid

    # ── serialisation ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "last_mid":    round(self.last_mid, 4),
            "mom_ema":     round(self.mom_ema, 6),
            "initialized": self.initialized,
        }

    def from_dict(self, d: dict):
        self.last_mid    = d.get("last_mid",    0.0)
        self.mom_ema     = d.get("mom_ema",     0.0)
        self.initialized = d.get("initialized", False)


# ─────────────────────────────────────────────────────────────────────────────
# ASH_COATED_OSMIUM  —  Avellaneda-Stoikov market making (unchanged)
# ─────────────────────────────────────────────────────────────────────────────
class ProductTrader:
    """
    Avellaneda-Stoikov market-making for ASH_COATED_OSMIUM.

    Key features:
    - 'fixed_anchor' FV mode: 95 % anchor at 10 000, 5 % slow EMA correction
    - Zero-price tick filter (carry forward last valid FV)
    - One-sided book guard (only quote sides with resting reference)
    - Mean-reversion fade aggressor (fade large jumps vs FV)
    """

    DAY_LENGTH = 1_000_000

    def __init__(self, cfg: dict):
        self.cfg              = cfg
        self.fv: float        = cfg.get("fv_seed", cfg.get("fv_anchor", 10_000))
        self.fv_initialized   = not cfg.get("fv_lazy_init", False)
        self.kappa            = cfg["kappa_init"]
        self.mid_history:     List[float] = []
        self.last_valid_mid:  float = self.fv

        self.flow_ema: float  = 0.0
        self.flow_history:    List[float] = []
        self.price_mom: float = 0.0

        self.recent_trade_prices: List[int] = []

    # ── public API ────────────────────────────────────────────────────────────
    def orders(self, product: str, depth: OrderDepth,
               position: int, own_fills: List[Trade],
               market_trades: List[Trade],
               timestamp: int) -> List[Order]:
        cfg = self.cfg
        mid = self._mid(depth)

        if mid <= 0:
            mid = self.last_valid_mid
        else:
            self.last_valid_mid = mid

        # ── update fair value ─────────────────────────────────────────────────
        if not self.fv_initialized and mid > 0:
            self.fv = mid
            self.fv_initialized = True
        fv_mode = cfg.get("fv_mode", "fixed")
        if fv_mode == "ema":
            self.fv = (1 - cfg["fv_ema_alpha"]) * self.fv + cfg["fv_ema_alpha"] * mid
        elif fv_mode == "fixed_anchor":
            anchor  = cfg["fv_anchor"]
            w       = cfg.get("fv_anchor_weight", 0.95)
            ema_val = (1 - cfg["fv_ema_alpha"]) * self.fv + cfg["fv_ema_alpha"] * mid
            self.fv = w * anchor + (1 - w) * ema_val
        else:
            self.fv = float(cfg.get("fv_anchor", cfg.get("fv_seed", 10_000)))

        if self.mid_history:
            mom_alpha   = cfg.get("momentum_alpha", 0.3)
            self.price_mom = ((1 - mom_alpha) * self.price_mom
                              + mom_alpha * (mid - self.mid_history[-1]))

        self.mid_history.append(mid)
        if len(self.mid_history) > 100:
            self.mid_history.pop(0)

        self._update_flow(market_trades, depth)

        if cfg["kappa_alpha"] > 0:
            for t in own_fills:
                delta = abs(t.price - mid)
                if delta > 0.5:
                    obs = 1.0 / delta
                    self.kappa = ((1 - cfg["kappa_alpha"]) * self.kappa
                                  + cfg["kappa_alpha"] * obs)
            self.kappa = max(cfg["kappa_min"], min(cfg["kappa_max"], self.kappa))

        time_remaining = max(1.0 - timestamp / self.DAY_LENGTH, 1e-4)

        result  = self._take(product, depth, position, mid)
        result += self._fade(product, depth, position, mid)
        result += self._make(product, depth, position, time_remaining, mid)
        return result

    # ── order flow signal ─────────────────────────────────────────────────────
    def _update_flow(self, market_trades: List[Trade], depth: OrderDepth):
        best_ask = min(depth.sell_orders) if depth.sell_orders else None
        best_bid = max(depth.buy_orders)  if depth.buy_orders  else None

        net_volume = 0.0
        for t in market_trades:
            if best_ask is not None and t.price >= best_ask:
                net_volume += t.quantity
            elif best_bid is not None and t.price <= best_bid:
                net_volume -= t.quantity

        alpha = self.cfg.get("flow_alpha", 0.2)
        self.flow_ema = (1 - alpha) * self.flow_ema + alpha * net_volume

        self.flow_history.append(net_volume)
        if len(self.flow_history) > 20:
            self.flow_history.pop(0)

        for t in market_trades:
            self.recent_trade_prices.append(int(t.price))
        if len(self.recent_trade_prices) > 50:
            self.recent_trade_prices = self.recent_trade_prices[-50:]

    def _flow_volatility(self) -> float:
        if len(self.flow_history) < 3:
            return 0.0
        mean = sum(self.flow_history) / len(self.flow_history)
        var  = sum((f - mean) ** 2 for f in self.flow_history) / len(self.flow_history)
        return math.sqrt(var)

    def _composite_signal(self) -> float:
        mom_scale = self.cfg.get("momentum_scale", 0.0)
        return self.flow_ema + self.price_mom * mom_scale

    # ── take (aggressor) ──────────────────────────────────────────────────────
    def _take(self, product: str, depth: OrderDepth,
              position: int, mid: float) -> List[Order]:
        cfg    = self.cfg
        fv_int = round(self.fv)
        thresh = cfg["take_threshold"]
        lim    = cfg["position_limit"]
        orders = []
        buy_cap = lim - position
        sel_cap = lim + position

        flow_thresh = cfg.get("flow_take_threshold", float("inf"))
        flow_boost  = cfg.get("flow_take_boost", 0)
        sig = self._composite_signal()
        buy_boost  = flow_boost if sig >  flow_thresh else 0
        sell_boost = flow_boost if sig < -flow_thresh else 0

        for ask in sorted(depth.sell_orders):
            if ask > fv_int - thresh + buy_boost or buy_cap <= 0:
                break
            vol = min(-depth.sell_orders[ask], buy_cap)
            orders.append(Order(product, ask, vol))
            buy_cap -= vol

        for bid in sorted(depth.buy_orders, reverse=True):
            if bid < fv_int + thresh - sell_boost or sel_cap <= 0:
                break
            vol = min(depth.buy_orders[bid], sel_cap)
            orders.append(Order(product, bid, -vol))
            sel_cap -= vol

        return orders

    # ── mean-reversion fade ───────────────────────────────────────────────────
    def _fade(self, product: str, depth: OrderDepth,
              position: int, mid: float) -> List[Order]:
        cfg         = self.cfg
        fade_thresh = cfg.get("fade_threshold", 0)
        if fade_thresh <= 0:
            return []

        fv_int    = round(self.fv)
        deviation = mid - fv_int
        lim       = cfg["position_limit"]
        fade_sz   = min(cfg.get("fade_size", 5), cfg["max_order_size"])
        orders    = []

        if deviation >= fade_thresh and depth.sell_orders:
            best_ask = min(depth.sell_orders)
            vol = min(-depth.sell_orders[best_ask], fade_sz, lim + position)
            if vol > 0:
                orders.append(Order(product, best_ask, -vol))
        elif deviation <= -fade_thresh and depth.buy_orders:
            best_bid = max(depth.buy_orders)
            vol = min(depth.buy_orders[best_bid], fade_sz, lim - position)
            if vol > 0:
                orders.append(Order(product, best_bid, vol))

        return orders

    # ── make (passive quotes) ─────────────────────────────────────────────────
    def _make(self, product: str, depth: OrderDepth,
              position: int, time_remaining: float, mid: float) -> List[Order]:
        cfg    = self.cfg
        orders = []
        lim    = cfg["position_limit"]
        soft   = cfg["soft_limit"]
        fv_int = round(self.fv)

        has_bid = bool(depth.buy_orders)
        has_ask = bool(depth.sell_orders)

        # end-of-day urgency
        eod_pos_min = cfg.get("eod_pos_min", 3)
        if time_remaining < (1.0 - cfg["eod_threshold"]) and abs(position) > eod_pos_min:
            buy_cap = lim - position
            sel_cap = lim + position
            if position > 0 and has_bid and sel_cap > 0:
                orders.append(Order(product, max(depth.buy_orders),
                                    -min(sel_cap, cfg["max_order_size"])))
            elif position < 0 and has_ask and buy_cap > 0:
                orders.append(Order(product, min(depth.sell_orders),
                                    min(buy_cap, cfg["max_order_size"])))
            return orders

        r = self._reservation_price(position, time_remaining, self.fv)

        if cfg.get("use_fixed_spread", False):
            delta = cfg["fixed_half_spread"]
        else:
            delta = self._optimal_half_spread(time_remaining)

        our_bid = round(r - delta)
        our_ask = round(r + delta)

        our_bid = min(our_bid, fv_int - 1)
        our_ask = max(our_ask, fv_int + 1)
        if our_bid >= our_ask:
            our_bid, our_ask = fv_int - 1, fv_int + 1

        if abs(position) > soft:
            excess     = abs(position) - soft
            skew_extra = int(cfg.get("hard_skew_ticks", 3) * excess / (lim - soft))
            if position > 0:
                our_ask = max(fv_int + 1, our_ask - skew_extra)
                our_bid -= skew_extra
            else:
                our_bid = min(fv_int - 1, our_bid + skew_extra)
                our_ask += skew_extra

        if abs(position) < soft:
            if has_bid:
                our_bid = min(max(our_bid, max(depth.buy_orders)), fv_int - 1)
            if has_ask:
                our_ask = max(min(our_ask, min(depth.sell_orders)), fv_int + 1)

        bot_level = self._detect_bot_level()
        if bot_level is not None and has_bid and has_ask:
            best_bid = max(depth.buy_orders)
            best_ask = min(depth.sell_orders)
            if bot_level == best_bid and our_bid <= best_bid and our_bid < fv_int - 1:
                our_bid = best_bid + 1
            if bot_level == best_ask and our_ask >= best_ask and our_ask > fv_int + 1:
                our_ask = best_ask - 1

        if cfg.get("use_queue_shift", False):
            thick = cfg.get("queue_thick_vol", 4)
            if has_bid and self._queue_vol(depth, our_bid, "bid") > thick and our_bid < fv_int - 1:
                our_bid += 1
            if has_ask and self._queue_vol(depth, our_ask, "ask") > thick and our_ask > fv_int + 1:
                our_ask -= 1

        buy_cap = lim - position
        sel_cap = lim + position
        bid_sz  = min(self._taper(position,  1), buy_cap)
        ask_sz  = min(self._taper(position, -1), sel_cap)

        adv_thresh = cfg.get("adv_sel_threshold", 999.0)
        adv_min    = cfg.get("adv_sel_min_frac",  1.0)
        sig        = self._composite_signal()
        if sig < -adv_thresh:
            bid_sz = max(1, int(bid_sz * adv_min))
        if sig > adv_thresh:
            ask_sz = max(1, int(ask_sz * adv_min))

        if not has_ask:
            bid_sz = 0
        if not has_bid:
            ask_sz = 0

        if bid_sz > 0:
            orders.append(Order(product, our_bid,  bid_sz))
        if ask_sz > 0:
            orders.append(Order(product, our_ask, -ask_sz))

        return orders

    # ── A-S formulas ──────────────────────────────────────────────────────────
    def _sigma_sq(self) -> float:
        if len(self.mid_history) < 2:
            return 2.0
        ch   = [self.mid_history[i] - self.mid_history[i-1]
                for i in range(1, len(self.mid_history))]
        mean = sum(ch) / len(ch)
        return sum((c - mean) ** 2 for c in ch) / len(ch)

    def _reservation_price(self, position: int, time_remaining: float,
                           fv_used: float = None) -> float:
        cfg      = self.cfg
        base_fv  = self.fv if fv_used is None else fv_used
        vol_skew = position * cfg["gamma"] * self._sigma_sq() * time_remaining
        inv_skew = position * cfg["inventory_skew"]
        flow_skew = self.flow_ema * cfg.get("flow_skew_scale", 0.0)
        return base_fv - vol_skew - inv_skew + flow_skew

    def _optimal_half_spread(self, time_remaining: float) -> float:
        cfg      = self.cfg
        vol_term = cfg["gamma"] * self._sigma_sq() * time_remaining / 2.0
        fill_term = (1.0 / cfg["gamma"]) * math.log(
            1.0 + cfg["gamma"] / max(self.kappa, 0.05)
        )
        base = max(vol_term + fill_term, 1.0)
        flow_factor = 1.0 + cfg.get("flow_vol_scale", 0.0) * self._flow_volatility()
        return base * flow_factor

    # ── helpers ───────────────────────────────────────────────────────────────
    def _queue_vol(self, depth: OrderDepth, price: int, side: str) -> int:
        if side == "bid":
            return depth.buy_orders.get(price, 0)
        return abs(depth.sell_orders.get(price, 0))

    def _detect_bot_level(self) -> Optional[int]:
        if len(self.recent_trade_prices) < 10:
            return None
        counts   = Counter(self.recent_trade_prices[-30:])
        bot_min  = self.cfg.get("bot_min_count", 4)
        top_price, top_count = counts.most_common(1)[0]
        return top_price if top_count >= bot_min else None

    def _taper(self, position: int, direction: int) -> int:
        soft = self.cfg["soft_limit"]
        lim  = self.cfg["position_limit"]
        exp  = max(position, 0) if direction == 1 else max(-position, 0)
        if exp < soft:
            return self.cfg["max_order_size"]
        ratio = (exp - soft) / (lim - soft)
        return max(self.cfg["max_order_size"] - round(ratio * (self.cfg["max_order_size"] - 1)), 0)

    def _mid(self, depth: OrderDepth) -> float:
        if depth.buy_orders and depth.sell_orders:
            return (max(depth.buy_orders) + min(depth.sell_orders)) / 2.0
        return self.fv

    # ── serialisation ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "fv":                   round(self.fv, 4),
            "kappa":                round(self.kappa, 6),
            "mid_history":          self.mid_history[-100:],
            "flow_ema":             round(self.flow_ema, 4),
            "flow_history":         self.flow_history[-20:],
            "price_mom":            round(self.price_mom, 4),
            "recent_trade_prices":  self.recent_trade_prices[-50:],
            "last_valid_mid":       round(self.last_valid_mid, 4),
            "fv_initialized":       self.fv_initialized,
        }

    def from_dict(self, d: dict):
        self.fv                  = d.get("fv",    self.fv)
        self.kappa               = d.get("kappa", self.cfg["kappa_init"])
        self.mid_history         = d.get("mid_history", [])
        self.flow_ema            = d.get("flow_ema", 0.0)
        self.flow_history        = d.get("flow_history", [])
        self.price_mom           = d.get("price_mom", 0.0)
        self.recent_trade_prices = d.get("recent_trade_prices", [])
        self.last_valid_mid      = d.get("last_valid_mid", self.fv)
        self.fv_initialized      = d.get("fv_initialized", self.fv_initialized)


# ─────────────────────────────────────────────────────────────────────────────
# Trader  —  the ONE class Prosperity calls
# ─────────────────────────────────────────────────────────────────────────────
class Trader:
    """
    ROUND 1 bot:
      INTARIAN_PEPPER_ROOT  → MomentumTrader  (trend-following, no FV anchor)
      ASH_COATED_OSMIUM     → ProductTrader   (Avellaneda-Stoikov market making)
    """

    PRODUCTS = {
        "INTARIAN_PEPPER_ROOT": None,   # populated in __init__
        "ASH_COATED_OSMIUM":    None,
    }

    def __init__(self):
        self._traders: Dict[str, Any] = {
            "INTARIAN_PEPPER_ROOT": MomentumTrader(),
            "ASH_COATED_OSMIUM":    ProductTrader(ACO_CONFIG),
        }

    def run(self, state: TradingState):
        # ── restore persisted state ───────────────────────────────────────────
        saved = {}
        if state.traderData:
            try:
                saved = json.loads(state.traderData)
            except Exception:
                pass
        for product, pt in self._traders.items():
            if product in saved:
                pt.from_dict(saved[product])

        # ── generate orders for every active product ──────────────────────────
        all_orders: Dict[Symbol, List[Order]] = {}

        for product, pt in self._traders.items():
            if product not in state.order_depths:
                continue

            depth         = state.order_depths[product]
            position      = state.position.get(product, 0)
            own_fills     = state.own_trades.get(product, [])
            market_trades = state.market_trades.get(product, [])

            product_orders = pt.orders(
                product, depth, position, own_fills, market_trades, state.timestamp
            )
            all_orders[product] = product_orders

            best_bid = max(depth.buy_orders)  if depth.buy_orders  else "—"
            best_ask = min(depth.sell_orders) if depth.sell_orders else "—"

            # Different log line per strategy type
            if isinstance(pt, MomentumTrader):
                print(
                    f"[{product}] t={state.timestamp} pos={position:+d} "
                    f"mom={pt.mom_ema:+.4f} book={best_bid}/{best_ask} "
                    f"orders={[(o.price, o.quantity) for o in product_orders]}"
                )
            else:
                print(
                    f"[{product}] t={state.timestamp} pos={position:+d} "
                    f"fv={pt.fv:.1f} κ={pt.kappa:.3f} flow={pt.flow_ema:+.2f} "
                    f"book={best_bid}/{best_ask} "
                    f"orders={[(o.price, o.quantity) for o in product_orders]}"
                )

        # ── persist state ─────────────────────────────────────────────────────
        trader_data = json.dumps({p: pt.to_dict() for p, pt in self._traders.items()})
        return all_orders, 0, trader_data


# ─────────────────────────────────────────────────────────────────────────────
# Local back-test harness  (python trader2.py)
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import csv, os
    from collections import defaultdict

    PRODUCTS = list(Trader().PRODUCTS.keys())
    ROUND    = "ROUND_1"

    def load_prices(path, day_tag):
        rows = defaultdict(list)
        with open(path) as f:
            for r in csv.DictReader(f, delimiter=";"):
                if r["product"] in PRODUCTS:
                    rows[(day_tag, int(r["timestamp"]))].append(r)
        return rows

    def load_trades(path, day_tag):
        trades = defaultdict(list)
        if not os.path.exists(path):
            return trades
        with open(path) as f:
            for r in csv.DictReader(f, delimiter=";"):
                sym = r.get("symbol", r.get("product", ""))
                if sym in PRODUCTS:
                    trades[(day_tag, int(r["timestamp"]))].append(
                        Trade(sym, float(r["price"]), int(r["quantity"]),
                              r.get("buyer",""), r.get("seller",""),
                              int(r["timestamp"]))
                    )
        return trades

    def make_depth(row):
        buys, sells = {}, {}
        for i in range(1, 4):
            bp, bv = row.get(f"bid_price_{i}",""), row.get(f"bid_volume_{i}","")
            ap, av = row.get(f"ask_price_{i}",""), row.get(f"ask_volume_{i}","")
            if bp and bv:
                try: buys[int(float(bp))] = int(float(bv))
                except ValueError: pass
            if ap and av:
                try: sells[int(float(ap))] = -int(float(av))
                except ValueError: pass
        return OrderDepth(buys, sells)

    def simulate_fills(orders, depth, mkt_trades, position, pnl, product):
        pos, cash, fills = position, pnl, []
        for o in orders:
            if o.quantity > 0:
                best_ask = min(depth.sell_orders) if depth.sell_orders else None
                if best_ask is not None and o.price >= best_ask:
                    fq = min(o.quantity, -depth.sell_orders[best_ask])
                    pos += fq; cash -= fq * o.price
                    fills.append(Trade(product, o.price, fq))
                else:
                    rem = o.quantity
                    for t in sorted(mkt_trades, key=lambda x: x.price):
                        if rem <= 0: break
                        if o.price >= t.price:
                            fq = min(rem, t.quantity)
                            pos += fq; cash -= fq * o.price; rem -= fq
                            fills.append(Trade(product, o.price, fq))
            else:
                best_bid = max(depth.buy_orders) if depth.buy_orders else None
                if best_bid is not None and o.price <= best_bid:
                    fq = min(-o.quantity, depth.buy_orders[best_bid])
                    pos -= fq; cash += fq * o.price
                    fills.append(Trade(product, o.price, -fq))
                else:
                    rem = -o.quantity
                    for t in sorted(mkt_trades, key=lambda x: -x.price):
                        if rem <= 0: break
                        if o.price <= t.price:
                            fq = min(rem, t.quantity)
                            pos -= fq; cash += fq * o.price; rem -= fq
                            fills.append(Trade(product, o.price, -fq))
        return pos, cash, fills

    # ── load all data ─────────────────────────────────────────────────────────
    base     = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base, ROUND)
    all_price_rows: Dict[int, list] = defaultdict(list)
    all_trade_rows: Dict[int, list] = defaultdict(list)

    for day in ["-2", "-1", "0"]:
        pf = os.path.join(data_dir, f"prices_round_1_day_{day}.csv")
        tf = os.path.join(data_dir, f"trades_round_1_day_{day}.csv")
        if os.path.exists(pf):
            for key, rows in load_prices(pf, day).items():
                all_price_rows[key].extend(rows)
        for key, trades in load_trades(tf, day).items():
            all_trade_rows[key].extend(trades)

    sorted_ticks = sorted(all_price_rows.keys())

    # ── simulation state ──────────────────────────────────────────────────────
    trader      = Trader()
    positions   = {p: 0   for p in PRODUCTS}
    pnls        = {p: 0.0 for p in PRODUCTS}
    prev_own    = {p: []  for p in PRODUCTS}
    trader_data = ""
    total_fills = {p: 0   for p in PRODUCTS}

    print(f"\n{'Tick':>8}  " + "  ".join(f"{p:>10} {'Pos':>4} {'MTM':>9}" for p in PRODUCTS))
    print("─" * (10 + 26 * len(PRODUCTS)))

    import io, sys
    for tick_key in sorted_ticks:
        _, ts = tick_key
        rows_at_ts   = {r["product"]: r for r in all_price_rows[tick_key]}
        trades_at_ts = list(all_trade_rows.get(tick_key, []))

        depths = {p: make_depth(rows_at_ts[p]) for p in PRODUCTS if p in rows_at_ts}
        if not depths:
            continue

        mkt_by_product = defaultdict(list)
        for t in trades_at_ts:
            mkt_by_product[t.symbol].append(t)

        old_stdout = sys.stdout; sys.stdout = io.StringIO()

        state = TradingState(
            timestamp     = ts,
            listings      = {},
            order_depths  = depths,
            own_trades    = {p: prev_own[p] for p in PRODUCTS},
            market_trades = dict(mkt_by_product),
            position      = dict(positions),
            observations  = None,
            traderData    = trader_data,
        )
        all_orders, _, trader_data = trader.run(state)

        sys.stdout = old_stdout

        new_own = {p: [] for p in PRODUCTS}
        for p in PRODUCTS:
            if p not in depths: continue
            prev_pos = positions[p]
            positions[p], pnls[p], new_own[p] = simulate_fills(
                all_orders.get(p, []), depths[p],
                mkt_by_product.get(p, []),
                positions[p], pnls[p], p
            )
            total_fills[p] += abs(positions[p] - prev_pos)
        prev_own = new_own

        if ts % 5000 == 0:
            mid_vals = {p: float(rows_at_ts[p]["mid_price"])
                        for p in PRODUCTS if p in rows_at_ts}
            row = f"{ts:>8}  "
            for p in PRODUCTS:
                mtm = pnls[p] + positions[p] * mid_vals.get(p, 0)
                row += f"  {p:>10} {positions[p]:>+4} {mtm:>9.0f}"
            print(row)

    # ── final summary ─────────────────────────────────────────────────────────
    print("─" * (10 + 26 * len(PRODUCTS)))
    saved_state = json.loads(trader_data) if trader_data else {}
    total_mtm   = 0.0
    for p in PRODUCTS:
        ps  = saved_state.get(p, {})
        # Use last_mid for MomentumTrader (IPR), fv for ProductTrader (ACO)
        ref = ps.get("last_mid") or ps.get("fv") or 0
        mtm = pnls[p] + positions[p] * ref
        total_mtm += mtm
        print(f"{p:>25}  fills={total_fills[p]:>5}  pos={positions[p]:>+3}  MTM={mtm:>10,.1f}")

    print(f"\n{'TOTAL':>25}  {'':>12}       MTM={total_mtm:>10,.1f} seashells")
