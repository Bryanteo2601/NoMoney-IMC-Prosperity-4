"""
IDK-aware sweep: each product can be set to 'idk_mode' (do nothing) as a
third option alongside the normal parameter grid.

Objective = raw_pnl - IDK_PENALTY * num_idk_products * num_days

The penalty forces the optimizer to justify ignoring a product — if ignoring
ACO costs 5000/day you only choose it when the active strategy would lose more.
"""

import io
import itertools
from contextlib import redirect_stdout
import unittest.mock as mock

import trader as trader_mod
import backtest as backtest_mod
from backtest import run_backtest

DAYS = (-1, 0, 1)    # Round 2 data days
ROUND = 2
IDK_PENALTY = 5_000   # seashells per day per IDK'd product — tune as needed

BEST_IPR = {
    "ema_fast": 0.3, "ema_slow": 0.03,
    "edge": 2, "make_edge": 3,
    "drift_scale": 75, "drift_boost": 6,
}
BEST_ACO = {
    "ema_fast": 0.15, "ema_slow": 0.05,
    "edge": 1, "make_edge": 2,
    "drift_scale": 0, "drift_boost": 4,
    "quote_frac": 1.0, "arb_mode": True,
    "max_take_size": 80, "max_quote_size": 80, "max_spread": 30,
}

IDK_CFG = {"idk_mode": True}   # sentinel — trader returns [] for this product

ACO_OPTIONS = [BEST_ACO, IDK_CFG]
IPR_OPTIONS = [BEST_IPR, IDK_CFG]


def _r2_price_path(day): return f"prices_round_{ROUND}_day_{day}.csv"
def _r2_trade_path(day): return f"trades_round_{ROUND}_day_{day}.csv"


def run_days(aco_cfg, ipr_cfg):
    trader_mod.PRODUCT_CFG["ASH_COATED_OSMIUM"]    = aco_cfg
    trader_mod.PRODUCT_CFG["INTARIAN_PEPPER_ROOT"] = ipr_cfg

    per_day = []
    for day in DAYS:
        buf = io.StringIO()
        # Patch backtest to use round-2 file paths
        with redirect_stdout(buf), \
             mock.patch.object(backtest_mod, "load_prices",
                               lambda path, d: backtest_mod.load_prices.__wrapped__(
                                   _r2_price_path(d), d)):
            # Simpler approach: just rename inline via os.path override
            pass

        # Use direct patching of the path strings inside run_backtest
        buf = io.StringIO()
        orig_exists = backtest_mod.os.path.exists

        def patched_exists(p):
            # redirect round-1 requests to round-2 files
            import re
            m = re.match(r"(prices|trades)_round_1_day_(-?\d+)\.csv", p)
            if m:
                kind, d = m.group(1), m.group(2)
                return orig_exists(f"{kind}_round_{ROUND}_day_{d}.csv")
            return orig_exists(p)

        def patched_open(p, *a, **kw):
            import re
            m = re.match(r"(prices|trades)_round_1_day_(-?\d+)\.csv", p)
            if m:
                kind, d = m.group(1), m.group(2)
                p = f"{kind}_round_{ROUND}_day_{d}.csv"
            return _orig_open(p, *a, **kw)

        _orig_open = open

        import builtins
        with redirect_stdout(buf), \
             mock.patch.object(backtest_mod.os.path, "exists", patched_exists), \
             mock.patch("builtins.open", patched_open):
            run_backtest(days=(day,))

        line = next((l for l in buf.getvalue().splitlines() if l.startswith("TOTAL")), None)
        if line is None:
            per_day.append(0.0)
        else:
            per_day.append(float(line.split()[-2]))
    return per_day


def penalised_score(per_day, n_idk):
    """Total PnL minus the IDK penalty across all days."""
    raw   = sum(per_day)
    penalty = IDK_PENALTY * n_idk * len(DAYS)
    return raw - penalty, raw, penalty


def label(cfg):
    return "IDK" if cfg.get("idk_mode") else "ACTIVE"


def main():
    results = []
    combos = list(itertools.product(ACO_OPTIONS, IPR_OPTIONS))

    print(f"IDK_PENALTY = {IDK_PENALTY:,} per product per day\n")
    print(f"{'#':>3}  {'ACO':>7}  {'IPR':>7}  {'Raw PnL':>10}  {'Penalty':>9}  {'Score':>10}  {'Worst day':>10}")
    print("─" * 70)

    for i, (aco, ipr) in enumerate(combos, 1):
        per_day  = run_days(aco, ipr)
        n_idk    = sum(1 for c in (aco, ipr) if c.get("idk_mode"))
        score, raw, penalty = penalised_score(per_day, n_idk)
        worst    = min(per_day)
        lbl      = f"{label(aco):>7}  {label(ipr):>7}"
        print(f"{i:>3}  {lbl}  {raw:>10,.0f}  {penalty:>9,.0f}  {score:>10,.0f}  {worst:>10,.0f}")
        results.append({
            "aco": label(aco), "ipr": label(ipr),
            "per_day": per_day, "raw": raw,
            "penalty": penalty, "score": score, "worst": worst,
            "n_idk": n_idk,
        })

    results.sort(key=lambda r: -r["score"])
    print("\n── RANKING BY PENALISED SCORE ──")
    for rank, r in enumerate(results, 1):
        print(f"  #{rank}  ACO={r['aco']}  IPR={r['ipr']}  "
              f"score={r['score']:>10,.0f}  raw={r['raw']:>10,.0f}  "
              f"penalty={r['penalty']:>8,.0f}")

    return results


if __name__ == "__main__":
    results = main()

    try:
        from visualize_idk import plot_results
        plot_results(results, idk_penalty=IDK_PENALTY)
    except ImportError:
        pass
