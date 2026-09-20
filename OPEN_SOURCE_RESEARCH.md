# Open-source integration research

Research date: 2026-07-24

## Decision summary

| Project | Evidence checked | Fit for this system | Decision |
|---|---|---|---|
| [python-metar](https://github.com/python-metar/python-metar) | Active BSD package, version 2.0.1, Python >=3.10, zero runtime dependencies; parses international METAR/SPECI and remark groups | Directly fills the collector's missing structured fields: gust, visibility, pressure, sky layers and a parser status for the raw report | **Integrated** |
| [WeatherBench-X](https://github.com/google-research/weatherbenchX) | Active Apache-2.0 framework, but its base install requires Apache Beam, xarray, NumPy, pandas, SciPy, scikit-learn, Zarr, GCSFS, PyArrow, JAX and more; currently declares Python <3.12 while this machine runs 3.14 | The design is valuable for explicit forecast/target pairing and metrics, but the dependency/runtime contract is not suitable for the live macOS collector | **Patterns integrated, package not installed** |
| [MetPy](https://github.com/Unidata/MetPy) | Active BSD package; requires NumPy, pandas, SciPy, xarray, Pint, pyproj and matplotlib | Useful for future derived meteorology, but current inputs already contain temperature/dew point/wind and the extra scientific stack would increase failure surface without improving today's decisions | **Keep as research option** |
| [Evidently](https://github.com/evidentlyai/evidently) | Active Apache-2.0 observability framework; current package pulls pandas/scikit-learn/SciPy/Plotly and a broad service stack | Valuable after enough settled samples for drift/calibration monitoring, but the current sample is only a few event-days per city and the dashboard already consumes SQLite reports | **Defer until sample threshold** |
| [Polymarket py-sdk](https://github.com/Polymarket/py-sdk) | Official active MIT SDK, but current 0.x package pulls httpx/http2, eth-account, eth-abi, websockets, pydantic and Web3 transaction support | The active system is paper-only and already has a narrow read-only Gamma/CLOB adapter. SDK would add wallet/trading surface that is explicitly out of scope | **Do not install; revisit for a read-only adapter later** |
| [py-clob-client](https://github.com/Polymarket/py-clob-client) | Repository is archived | Not an acceptable new dependency | **Rejected** |

## What was integrated

### 1. METAR enrichment

`weather_market_monitor.py` now parses the canonical `rawOb` with `python-metar` and stores parser status plus:

- gust speed;
- visibility in metres;
- pressure in hPa;
- flight category;
- structured cloud layers;
- original raw report and parser error.

The AviationWeather JSON remains the primary source for existing fields. The parser is an enrichment and validation layer, so a parser failure does not silently replace the upstream observation.

### 2. Leakage-aware forecast evaluation

`weather_forecast_evaluator.py` adds a small SQLite evaluation layer inspired by WeatherBench-X. It pairs Meteoblue/ECMWF forecasts with exact resolved temperature buckets and computes:

- signed error / bias;
- absolute error (MAE);
- RMSE;
- whole-degree hit rate;
- +/-1 C hit rate.

The evaluator retains every half-hour snapshot for auditability, but calibration queries select only one latest snapshot per event-day, model and local time. Current-day or later settlements are excluded, so the Agent cannot learn from future information.

Reports are written to `data/weather_monitor/forecast_evaluations_latest.csv` and `forecast_calibration_latest.json`. The Agent receives the prior-event calibration summary in `forecastCalibration` and is told to treat small samples as descriptive only.

## Verification against this database

At integration time the database contained 5,507 forecast rows across 84 event-days with exact settlement buckets. The calibration output correctly marked current city/model samples as insufficient when fewer than 10 prior event-days existed. This is expected: the system must accumulate more settled days before any automatic bias correction or drift alarm is trusted.

## Deferred integration gates

- Add Evidently only after at least 30 settled event-days per city/model, with a stable reference window and an explicit report export contract.
- Consider the official Polymarket SDK only for a read-only adapter after its public API stabilizes; do not enable authenticated or live order methods without a separate authorization decision.
- Add MetPy only when a concrete derived feature (for example, wet-bulb or lapse-rate calculation) is specified and tested against station data.
- Keep the Hermes runtime and the Python risk/execution kernel unchanged. None of these projects should become a second Agent framework.
