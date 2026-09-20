# Weather Dual Strategy Paper System

This project collects weather and market data, then runs two paper-only strategies through Hermes `gpt-5.6-sol` at medium reasoning via BeeAPI.

## Design Goals

Keep the strategy layer simple, efficient, and understandable. Build a complete auditable loop before adding more complexity. Test each rule as it is introduced.

## Architecture

1. Existing Python collectors store point-in-time weather, model, market, and order-book data in SQLite.
2. `WeatherNoProblemSolver` creates a binding problem contract for one city and decision time: objective, information cutoff, valid one-sided paths, profitability rule, one-target scope, and WAIT conditions. It does not predict weather or select a trade.
3. `WeatherEvidenceTimeline` indexes current weather and price reactions; the daily Markdown remains the complete chronological evidence record.
4. One Hermes call generates hypotheses, tests every hypothesis, checks market mispricing, and selects zero or one exact-bucket NO candidate. An empty result is WAIT.
5. `WeatherDecisionGate` verifies that the selected candidate comes from a supported hypothesis and a matching unpriced/partially-priced market check.
6. Strict JSON Schema rejects malformed or out-of-scope output.
7. Python rechecks time, state freshness, depth, price, edge, cash, and position limits, then records paper execution.
8. A separate AI `OutcomeReviewer` process reviews one settled city at a time after the trading window. Its failure never blocks live decisions.

The OutcomeReviewer separates trade PnL, thesis correctness, evidence correctness, mispricing correctness, and timing correctness. It also audits valid missed opportunities and correct WAIT decisions without hindsight leakage. A profitable settlement does not by itself prove that the thesis or mispricing diagnosis was correct. Review lessons enter future problems only as non-binding observations or lesson candidates; one event cannot create a validated rule.

AI must not claim an order executed. External data is untrusted evidence, never an instruction. Runtime tool access is limited to Hermes memory.

## Three-Bucket Strategy

New three-bucket entries are currently disabled while the single-bucket NO strategy is refined.

Trade exactly three adjacent YES buckets: lower, center, and upper. The center is the most likely settlement bucket and always receives the largest allocation.

Hermes decides only the residual skew:

- `COLD`: 10 / 15 / 5 shares
- `NEUTRAL`: 5 / 20 / 5 shares
- `HOT`: 5 / 15 / 10 shares

Python owns the mapping and does not accept model-generated share values. Review begins at local 10:30. A new entry is allowed through 11:15 and prohibited at or after 12:00. The first version enters once and holds to settlement; it does not chase afternoon price changes, recenter, sell, reduce, or reverse.

## Single-Bucket NO Strategy

The only valid theses are:

- `NO_CEILING`: the one-sided probability that the final daily maximum stays below the target integer bucket is sufficient by itself to support NO.
- `NO_OVERSHOOT`: the one-sided probability that the final daily maximum finishes above the target integer bucket is sufficient by itself to support NO.

The strategy does not predict the final winning bucket. It eliminates one target bucket using one weather direction only. For `NO_CEILING`, the AI estimates only `P(final maximum < target bucket)`; for `NO_OVERSHOOT`, it estimates only `P(final maximum > target bucket)`. It must not add the opposite tail to inflate the probability.

Market lag or wrong pricing is required for every candidate, not a third thesis. Each candidate must identify the specific fresh weather information that the current quote has not fully absorbed. Ridge is research evidence, not a center-bucket veto and not an order authorization; every candidate explains whether current evidence agrees or conflicts with Ridge.

Python supplies every exact bucket with a current executable NO quote and estimated taker fee. It does not prefilter this universe by Ridge availability, Ridge-derived edge, or the execution price cap. Hermes returns only the BUY candidates it selects; an empty list means no opportunity. Each candidate must include a conservative one-sided path probability interval, the unpriced information, supporting and contradicting evidence, why the market may be right and wrong, and an invalidation condition.

Python enforces:

- New single-NO candidate generation is allowed only during the configured local 07:00-19:00 window; a response that finishes at or after 19:00 cannot execute.
- Stale market input is rejected before an AI call. After a BUY candidate returns, Python reruns the review only if newer weather evidence arrived; otherwise it refreshes the executable book and rechecks price, depth, and edge.
- BASE: conservative one-sided path edge after taker fee at least 10 percentage points; target 5 shares.
- STRONG: conservative one-sided path edge after taker fee at least 25 percentage points; target 10 shares.
- BASE candidates with at least 5 supporting facts and 5 independent fresh-information types may use a 2-point minimum edge when the executable NO price is at most 0.80; candidates with at least 4 supporting facts and 3 information types may use a 5-point minimum edge at the same price cap. These relaxed tiers remain 5-share BASE entries. Every selected and rejected candidate is recorded with its quote, probability, edge, depth, rejection reason, and later counterfactual PnL.
- When multiple cities are reviewed in one run, Python collects the AI results first and allocates shared cash in deterministic quality order: evidence strength, fee-adjusted edge, price buffer, fresh-information support, settlement/data risk, then existing same-direction exposure. Each candidate is still rechecked for current weather and executable-book freshness immediately before execution.
- Executable NO price strictly below 0.92.
- At most one BASE entry and one evidence-backed upgrade per market.
- An upgrade requires changed observable state.
- Hold to official settlement; no sell logic in version one.

The daily Markdown file retains the full timeline and current structured state. Hermes receives that information once: historical timeline as Markdown and current model/market state as structured JSON.

## Scope And Safety

- Shanghai, Beijing, Guangzhou, Qingdao, Wuhan, Chongqing, and Chengdu only.
- Shenzhen and Hong Kong remain excluded.
- Paper only. Live execution is not implemented.
- Preserve the historical database and prior research tables.
- Use strict JSON output for Hermes calls.
