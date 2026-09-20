# Independent AI NO Backtest

This backtest is separate from the active trading AI. It reads the existing
point-in-time SQLite history but does not use Ridge, deterministic candidates,
the live AI prompt, its SOUL, its memory, or project AGENTS instructions.

## Decision flow

1. Select the latest three fully resolved dates by default.
2. Reconstruct data that had actually been fetched by each historical review time.
3. Give each city to Hermes `deepseek-v4-flash` in a separate call.
4. Give only the short city conclusions and candidate prices to a final portfolio call.
5. Permit only NO purchases, with 20 USDC initial cash and 3 USDC total cash per order.
6. Execute against the historical NO ask book, include the weather taker fee, and settle from the official winning market.

The portfolio AI chooses whether to trade, the city, exact bucket, number of
orders that fit available cash, and the next review interval. Empty actions are
valid. A 3 USDC research fill that does not reach the exchange's five-share
minimum is retained but marked `liveOrderMinSatisfied=false`.

## Run

Prepare and inspect the first leak-free input without calling AI:

```bash
python3 research/weather_ai_no_backtest.py --prepare-only
```

Run the latest three resolved dates:

```bash
python3 research/weather_ai_no_backtest.py
```

Run explicit dates:

```bash
python3 research/weather_ai_no_backtest.py --dates 2026-08-12 2026-08-13 2026-08-14
```

Each run is written under `data/ai_no_backtests/<run-id>/`. `report.md` is the
human summary, `result.json` is the machine-readable ledger, and per-step input,
raw response, validated response, and execution files provide the audit trail.

The number of reviews per day and the initial review window are intentionally
configuration values in `weather_ai_no_backtest_config.json`. DeepSeek calls are
currently slow, so use one date or reduce `maxReviewsPerDay` for quick experiments.
