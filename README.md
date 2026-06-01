# NoMoney - IMC Prosperity 4

This repository contains NoMoney's final submission files for IMC Prosperity 4, plus the custom market dashboard we used while developing and testing our strategies.

Our workflow was built around one idea: every round should be researched like a small market structure problem. Instead of only trying random parameter changes and submitting whatever backtested highest, we built tools to understand the data, replay the market, visualize our trades, and run sensitivity analysis before deciding what to trust.

## What's in This Repo

| File | Description |
| --- | --- |
| `Round1_EMA_Microprice_MarketMaking.py` | Round 1 strategy using EMA/microprice market-making logic. |
| `Round2_EMA_Microprice_MarketMaking_MAF.py` | Round 2 strategy with Market Access Fee bidding. |
| `Round3_Anchor_EMA_MarketMaking_Optimized.py` | Round 3 anchor/EMA strategy with tuned risk controls. |
| `Round4_Options_OrderFlow_RelativeValue.py` | Round 4 strategy combining options pricing, order-flow signals, and relative value logic. |
| `Round5_Directional_TrendSelection.py` | Round 5 strategy built around directional product selection. |
| `market_dashboard_v3.html` | Our custom dashboard for visualizing market data, trades, positions, and strategy behavior. |

The five Python files are the final renamed submissions from `Final_Submissions_File_IMC_Renamed_v9`. The backtester files from `NoMoney_Backtester-master` are not included here; that repo was used as part of our local research workflow, not as part of this final upload.

## Our Research Workflow

During the competition, we spent a lot of time building the infrastructure around the strategy, because the raw leaderboard result was never enough to explain what was going on.

The workflow usually looked like this:

1. Inspect the new round data and identify the products that actually had structure.
2. Build a first simple strategy around the most obvious behavior.
3. Replay the data locally with our backtester.
4. Break down PnL by product and by day.
5. Use the dashboard to inspect price paths, order-book behavior, fills, and inventory.
6. Run sweeps over the important parameters.
7. Look for stable parameter regions rather than single lucky peaks.
8. Add sizing and risk controls only after the alpha made sense.

This was especially important because Prosperity strategies can look very good for the wrong reason. A parameter might work because of one historical path, one lucky fill sequence, or one product hiding losses in another. Our process was designed to catch that before submitting.

## Backtester

We used a local backtesting workflow to replay historical data tick by tick. This allowed us to test each submission before sending it to the official platform.

The backtester was useful for:

- Testing how market-taking and passive quoting behaved under the recorded order books.
- Comparing total PnL against worst-day PnL.
- Checking whether inventory was getting stuck near limits.
- Seeing whether a product's PnL was stable or came from one isolated moment.
- Running repeated parameter sweeps without manually submitting every variation.

For many changes, we did not only ask "does this increase total PnL?" We also asked whether it made the strategy less fragile.

## Sensitivity Analysis and Sizing

A major part of the work was writing scripts to sweep parameters and search for better sizing.

Across the rounds, we tested things like:

- EMA speeds.
- Microprice/fair-value edge thresholds.
- Passive quote width.
- Taker size and maker size.
- Inventory skew.
- Position-scaled thresholds.
- Product inclusion and exclusion.
- Market Access Fee bid size.
- Option fair-value weights.
- Order-flow signal decay.
- Short-call sizing and risk locks.

The important part was not blindly picking the best number. We tried to find parameters where nearby values also performed reasonably well. If a setting only worked at one exact value, we treated it as overfit risk.

This also affected sizing. For example, when a strategy had an edge but became unstable at large inventory, we added caps, throttles, or position-scaled thresholds so that the bot would not keep adding risk just because the current book looked attractive.

## Dashboard

`market_dashboard_v3.html` was our custom visualization tool.

We used it to understand the market rather than just stare at CSV files. It helped us inspect:

- Product price paths.
- Order-book levels.
- Trades and fills.
- Position over time.
- PnL movement.
- Strategy behavior during drawdowns.
- Whether the strategy was making money repeatedly or only in one regime.

The dashboard was especially useful when backtest results were ambiguous. Sometimes a parameter change improved PnL but made inventory behavior worse. Sometimes a product looked profitable overall but only because of one large move. Visualizing the actual path made those issues much easier to spot.

## Round 1 - EMA and Microprice Market Making

Round 1 started with `ASH_COATED_OSMIUM` and `INTARIAN_PEPPER_ROOT`.

Our first approach was based on estimating fair value from the order book using microprice and EMAs. The idea was to use the current book imbalance and recent price movement to decide whether to take liquidity or post passive quotes.

`ASH_COATED_OSMIUM` was treated more like a stable market-making product. It had a fixed fair-value assumption around 10,000, so the focus was on quoting around that value and taking only when the edge was clear.

`INTARIAN_PEPPER_ROOT` behaved more directionally. For that product, we moved away from symmetric market making and leaned into directional accumulation. The strategy became more comfortable holding inventory when the observed behavior supported it.

The main lesson from Round 1 was that a single market-making template was not enough. Even with only two products, the better approach was product-specific: stable fair-value quoting for one, directional inventory for the other.

Final file: `Round1_EMA_Microprice_MarketMaking.py`

## Round 2 - Market Access Fee and More Controlled Quoting

Round 2 kept the Round 1 structure but added the Market Access Fee component.

The strategy had to decide not only how to trade, but also how much access was worth. We estimated the value of the strategy from backtests and set the `bid()` value based on the expected benefit of getting access. This became part of the final Round 2 file.

The trading logic also became more controlled:

- `ASH_COATED_OSMIUM` used more deliberate quote improvement and arbitrage-style logic when the book gave clear opportunities.
- `INTARIAN_PEPPER_ROOT` became long-only, because the data supported directional exposure much more than shorting.
- Dip-buying logic was added using a slower moving average, so the bot could accumulate when prices were attractive relative to the trend.

This round was where sizing and participation decisions became more important. A good strategy still needed a sensible access bid and risk-aware order sizes.

Final file: `Round2_EMA_Microprice_MarketMaking_MAF.py`

## Round 3 - Anchor/EMA Market Making

Round 3 introduced `HYDROGEL_PACK`, `VELVETFRUIT_EXTRACT`, and the VEV voucher products.

The cleanest edge came from the two underlying products. We used a fair value built from:

- A long-run anchor.
- A slow EMA.
- Inventory skew.

This gave the bot a stable estimate of fair value while still letting it adapt slightly when the market moved.

The first versions were too eager to trade. They crossed the spread on apparent edge that was not always real, and they could build large positions while the market moved against us. The optimized version fixed this with:

- A larger take threshold.
- Stronger inventory skew.
- Position-scaled thresholds.
- Two-stage taker caps.
- Fast unwinding when trades reduced inventory risk.

The Round 3 file includes the reasoning from our sweep results: increasing the threshold and improving inventory controls raised PnL while reducing drawdown. That was a good example of sensitivity analysis improving both return and path quality.

Final file: `Round3_Anchor_EMA_MarketMaking_Optimized.py`

## Round 4 - Options, Order Flow, and Relative Value

Round 4 was the most complex strategy file.

We kept the anchor/EMA engine from Round 3, then added option logic around the VEV products. The strategy included:

- Black-Scholes pricing for call options.
- Relative-value checks across strikes.
- Counterparty/order-flow memory signals.
- Signal clipping so flow could adjust fair value without completely overriding it.
- Short-call overlays when the underlying looked expensive.
- Rebound-based risk locks to stop adding risk when the regime changed.

The hardest part was avoiding overfitting. Options gave us many more parameters to tune, but more parameters also meant more ways to fit one historical path too closely.

To reduce that risk, the final strategy used gates and dynamic sizing. It did not blindly force the same short-call exposure at all times. It checked whether `VELVETFRUIT_EXTRACT` was elevated relative to the anchor, whether order-flow was supportive, and whether the market had started rebounding from its low.

This round was where our dashboard was most helpful. Looking only at final PnL did not show when the short-call book was becoming dangerous. Visualizing the path helped us add the rebound lock and late fallback behavior.

Final file: `Round4_Options_OrderFlow_RelativeValue.py`

## Round 5 - Directional Trend Selection

Round 5 had many products, so the main problem became selection.

Instead of trying to trade everything, we looked for products with repeated directional evidence across the available days and live feedback. The final strategy maintained directional positions in products where the historical behavior was most consistent and ignored products where the edge looked weak.

The strategy separated products into groups:

- Strong directional candidates.
- Moderate candidates that still had enough net evidence.
- Products removed after live feedback or unstable behavior.
- A small mean-reversion sleeve for `ROBOT_DISHES`.

This was also where "do nothing" became a real strategy decision. Adding a product just because it existed often made the bot worse. The final version was built around being selective and keeping the logic simple.

Final file: `Round5_Directional_TrendSelection.py`

## What We Learned

The biggest improvements came from process, not from making the code more complicated.

What worked best:

- Building our own backtesting workflow.
- Using the dashboard to understand why PnL moved.
- Running sensitivity analysis instead of relying on one backtest.
- Searching for robust sizing rather than max-size trading everything.
- Treating each product based on its own behavior.
- Cutting products or overlays when the evidence was weak.

What we would improve next time:

- Keep the research scripts cleaner from the start.
- Save sweep outputs in a more reproducible format.
- Separate final submissions, experiments, dashboards, and backtests into clearer folders.
- Add more automatic reporting for per-product PnL, drawdown, and parameter stability.

Even though the final repo is compact, it reflects the full path we took: market-making, MAF bidding, anchor-based fair values, options relative value, order-flow signals, dashboard-driven debugging, and final-round product selection.

