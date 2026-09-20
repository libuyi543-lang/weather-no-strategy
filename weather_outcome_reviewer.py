#!/usr/bin/env python3
"""Independent AI outcome-review worker for the active dual strategy."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import signal
import time
from pathlib import Path
from typing import Any

from weather_ai_agent import configure_logging
from weather_dual_strategy import CONFIG_PATH, ROOT, DualStrategyEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Weather dual-strategy AI OutcomeReviewer")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config["logPath"] = "logs/weather_outcome_reviewer.log"
    configure_logging(config)
    lock_path = ROOT / "data" / "weather_outcome_reviewer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("w")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.info("another OutcomeReviewer process is running")
        return 0
    engine = DualStrategyEngine(config)
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        if args.once or not args.loop:
            print(json.dumps(engine.run_outcome_reviewer_once(), ensure_ascii=False))
            return 0
        interval = max(60, int(config.get("outcomeReviewerIntervalSeconds", 900)))
        while not stop:
            result = engine.run_outcome_reviewer_once()
            if result.get("reviewed") or result.get("error"):
                logging.info("OutcomeReviewer result=%s", result)
            for _ in range(interval):
                if stop:
                    break
                time.sleep(1)
        return 0
    finally:
        engine.close()
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
