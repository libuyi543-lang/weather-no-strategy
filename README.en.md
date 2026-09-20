# Hermes Weather

AI probability estimates with deterministic execution boundaries: weather evidence, market snapshots, paper trading, and settlement evaluation.

[Interactive demo](https://libuyi543-lang.github.io/weather-no-strategy/) · [中文](README.md) · [Verification](docs/verification.md)

![Weather research console using synthetic data](docs/images/weather-demo.png)

The current Edge agent covers seven Chinese cities. Python owns freshness checks, executable quotes, fees, sizing and cash limits. Evaluation records Brier score, log-loss and a market baseline.

Paper trading only. The public UI uses synthetic data, not live quotes or verified performance. No out-of-sample profitability claim is made.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python3 -m unittest discover -v
```

270 unit tests passed locally on 2026-09-20, including 8 current Edge tests. Test counts do not measure features or trading performance. See [architecture](docs/architecture.md), [setup](docs/quickstart.md), and [contribution guide](CONTRIBUTING.md). MIT-licensed code; third-party data requires its own permissions.
