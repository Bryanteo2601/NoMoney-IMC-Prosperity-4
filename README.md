# IMC Prosperity 4 Round Files

This folder is the clean upload set. It intentionally does not include any Python files from `NoMoney_Backtester-master`; those were used only as reference for backtesting, sweep mechanics, and visualization workflow.

## Included Python Files

- `Round1_EMA_Microprice_MarketMaking.py` - Round 1 EMA/microprice market-making submission.
- `Round2_EMA_Microprice_MarketMaking_MAF.py` - Round 2 EMA/microprice market-making submission with MAF logic.
- `Round3_Anchor_EMA_MarketMaking_Optimized.py` - Round 3 optimized anchor/EMA market-making submission.
- `Round4_Options_OrderFlow_RelativeValue.py` - Round 4 options, order-flow, and relative-value submission.
- `Round5_Directional_TrendSelection.py` - Round 5 directional trend-selection submission.
- `market_dashboard_v3.html` - Custom market dashboard used for visualizing prices, order-book behavior, PnL paths, and strategy diagnostics during research.

These Python files come from `Final_Submissions_File_IMC_Renamed_v9`.

## What I Built

I built a local research workflow around the provided market data rather than relying only on live submissions. The existing backtester package was used as the execution engine reference, while my own scripts and strategy files were kept separate.

Key work:

- Built and adapted a local backtester to replay historical round data tick by tick.
- Added order-book simulation for market-taking and passive quoting so strategies could be tested before submission.
- Built sensitivity-analysis scripts to sweep strategy parameters across all available days for each round.
- Compared total PnL, worst-day PnL, and drawdown-style behavior instead of optimizing one day only.
- Built a custom HTML market dashboard for visualization, using it to inspect product paths, compare behavior across days, and diagnose where strategy PnL came from.
- Wrote scripts to search for best sizing, thresholds, inventory skew, passive quote widths, taker caps, and product inclusion/exclusion choices.

## Round Approach

Round 1 focused on two products. `INTARIAN_PEPPER_ROOT` showed strong trend behavior, so I used momentum and aggressive liquidity taking with passive support orders. `ASH_COATED_OSMIUM` behaved more range-bound, so I used a fixed-anchor market-making approach with inventory skew, queue-aware quoting, and guarded mean-reversion taking.

Round 2 used the newer data to test whether the Round 1 product strategies still justified trading. I added an IDK-aware sweep where each product could either remain active or be ignored, with a penalty for ignoring a product. This let me avoid forcing trades into weak edges and made the product-selection decision more systematic.

Round 3 introduced `HYDROGEL_PACK`, `VELVETFRUIT_EXTRACT`, and VEV vouchers. The main profitable behavior came from anchor/EMA fair values on the delta-1 products, with inventory skew and position-scaled thresholds to avoid adding too aggressively into adverse moves. The voucher surface was tested, but persistent mispricing and spread/adverse-selection costs made simple voucher trading less attractive.

Round 4 built on the Round 3 engine and added options logic. I layered counterparty memory signals, Black-Scholes valuation for VEV calls, implied-volatility smile checks, and late-session risk locks. Sensitivity sweeps tuned the anchor weight, take threshold, skew, maker width, signal decay, signal-to-tick conversion, and Black-Scholes blend.

Round 5 had a much larger product universe. I used historical consistency to identify products with repeated directional behavior, then maintained small max positions in those directions. For products with mean-reversion evidence, I used small passive market-making with tight inventory caps. I also tested sizing and product-selection changes to avoid overfitting to one live day.

## Workflow Summary

For each round, the process was:

1. Inspect the round data and product behavior.
2. Build a simple first strategy around the strongest observed edge.
3. Backtest across all available days, not just one file.
4. Sweep key parameters and inspect heatmaps or ranked results.
5. Prefer settings that improved total PnL while keeping worst-day behavior reasonable.
6. Add sizing controls and risk caps once the core signal worked.
7. Submit the cleanest version and iterate using live feedback.
