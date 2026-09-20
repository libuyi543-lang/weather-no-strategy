# Weather research repository

The current entry point is `weather_edge_agent.py`. It produces a full-bucket probability distribution and delegates execution, fees, sizing, freshness and cash limits to Python. Paper only; live execution is not implemented.

Run `python3 -m unittest discover -v` after Python changes and `npm ci && npm run build` in `hermes_gui` after frontend changes. Do not run collectors, notification senders or service installers as part of tests.

Keep credentials, SQLite databases, local account state and generated reports out of Git. Public GUI data must be clearly marked as synthetic. The older dual-strategy architecture is documented in `docs/legacy-agent-rules.md`; do not treat it as the current strategy. Never claim benchmark superiority from unit-test results or example data.
