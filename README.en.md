# Hermes Weather

> **"Let AI reason about probabilities; let deterministic code enforce execution boundaries."**  
> A multi-source weather evidence ingestion, Agent-driven probabilistic inference, deterministic risk gating, and automated settlement review system tailored for prediction markets (e.g., Polymarket).

[🚀 Live Interactive Demo](https://libuyi543-lang.github.io/weather-no-strategy/) · [📖 中文说明 (Chinese)](README.md) · [📊 Verification & Metrics](docs/verification.md) · [📐 Architecture](docs/architecture.md)

[![CI](https://github.com/libuyi543-lang/weather-no-strategy/actions/workflows/ci.yml/badge.svg)](https://github.com/libuyi543-lang/weather-no-strategy/actions/workflows/ci.yml)
[![Tests: 270 Passing](https://img.shields.io/badge/Unit%20Tests-270%20Passing-success)](docs/verification.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)](docs/quickstart.md)

![Weather research console using synthetic data](docs/images/weather-demo.png)

> ⚠️ **Research Prototype Disclaimer**: This project is an open-source engineering research harness designed strictly for **paper trading and decision auditing**. It contains no live trading execution and does not promise real-world financial alpha. Public Web demos render synthetic mock scenarios.

---

## 🎯 Key Highlights

1. **Anti-Leak Evidence Timeline**: Ingests airport METAR/SPECI, ECMWF ensemble runs, Windy models, and order books into a SQLite ledger. Historical queries enforce time-cutoffs to prevent lookahead bias.
2. **Probability vs. Execution Decoupling**: LLM Agent produces probability distributions (sum = 1.0) and falsification conditions. A Python deterministic layer enforces slippage checks, shrinkage towards market priors, and fractional Kelly sizing.
3. **Automated Verification**: Over **270 unit tests** validating edge cases, malformed schemas, stale data timeouts, and margin limits.
4. **Calibration Scoring**: Evaluates predictions using Brier scores and log-loss against a market baseline once true temperature tags settle.

## ⚡ Quick Verification (Local)

Run the complete test suite without external dependencies or API keys:

```bash
git clone https://github.com/libuyi543-lang/weather-no-strategy.git
cd weather-no-strategy
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# Run all 270 unit tests
python3 -m unittest discover -v
```

See [Architecture Guide](docs/architecture.md) and [Quickstart](docs/quickstart.md) for full system specifications. Licensed under [MIT](LICENSE).
