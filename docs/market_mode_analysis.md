# Market Mode Analysis

Analysis date: 2026-09-05. Source: `data/weather_market_monitor.sqlite3`, resolved events in the seven configured trading cities from 2026-07-19 through 2026-09-01. The current 2026-09-02 events are unresolved and were excluded.

The sample contains 273 resolved city-events with usable market snapshots near each measured decision horizon. For each event, the winning market is compared with the latest snapshot closest to the horizon before the event end. YES and NO quotes are treated as executable asks when available; this is a descriptive audit, not a backtest with an assumed fill size.

| Hours before close | Market Brier | Winner probability median |
| ---: | ---: | ---: |
| 12.5 | 0.6625 | 0.313 |
| 10 | 0.6481 | 0.331 |
| 8 | 0.5758 | 0.408 |
| 6 | 0.3992 | 0.580 |
| 4 | 0.1449 | 0.950 |
| 2 | 0.0218 | 0.994 |

At roughly 10 hours before close, unconditional YES/NO buying was close to fair after the spread and generally slightly negative. Across the seven cities, the average gross difference between realized outcome and YES ask was about -0.006 to -0.012; NO was about -0.008 to -0.016. Small positive cells existed, but they were narrow price bins with few observations and did not form a stable city-independent rule.

The market therefore has three regimes:

1. `EARLY_DISCOVERY`: broad uncertainty and weak evidence for a repeatable directional trade.
2. `LATE_DISCOVERY`: probabilities begin to concentrate; stale weather information can still create an opportunity, but the evidence threshold must rise.
3. `SETTLEMENT_CONVERGENCE`: the market is usually already right; chasing the leader has poor payoff after fees.

The supported trading mode is selective mispricing. WAIT is the default. A trade needs fresh, independent weather evidence that explains why the market has not incorporated a specific bucket change, a positive fee-adjusted edge, and executable depth. The system now exposes the regime to Hermes, raises the minimum edge to 10 percentage points before local noon and 8 points from noon to 15:00, and allows later review only for exceptional evidence. It never treats a single profitable settlement as proof of a reusable rule.

The execution ledger now follows the same accounting assumptions used by the recommended paper-trading reference: it keeps the requested and filled size, VWAP, book midpoint, estimated slippage in basis points, number of consumed ask levels, and net edge after the executable price and fee. A decision is also blocked when the supplied market probabilities fail the event sum invariant, and a decision may contain only one order.

The active decision path is deliberately separated into three measurements: the AI's raw bucket distribution, its evidence confidence, and the execution probability used for sizing. The execution probability shrinks the AI/market difference toward the market prior, with greater shrinkage near settlement. Python then computes fractional-Kelly paper size from this execution probability and executable cost. The shrink factors are conservative initialization parameters, not validated alpha; they should only be relaxed after walk-forward paper evidence shows a statistically credible improvement over the market.

This sample is large enough to reject unconditional fixed-side rules, but not large enough to establish a city-specific edge or a calibrated AI edge threshold. Continue paper collection and evaluate every candidate by counterfactual fill price, fee, depth, Brier improvement, and settlement PnL before lowering the gates.
