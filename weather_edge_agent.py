#!/usr/bin/env python3
"""Paper engine that scores the AI's bucket distribution against the market.

Design intent differs from the earlier dual-strategy engine in one way that
matters: the AI is asked for a full probability distribution over every
tradable bucket, not for a pre-shaped one-sided candidate. Mispricing is then
a derived quantity (its probability minus the market's), and the same
distribution is scored against the market after settlement. That score, not
the trade PnL, is the iteration target.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import signal
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from weather_ai_agent import (
    WeatherAIAgent,
    book_levels,
    configure_logging,
    executable_vwap,
    parse_ts,
)
from weather_data_store import WeatherDataStore, as_float, iso_utc, json_value, utc_now

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "weather_edge_agent_config.json"
SCHEMA_PATH = ROOT / "weather_edge_agent.schema.json"
FEEDBACK_SCHEMA_PATH = ROOT / "weather_edge_feedback.schema.json"
UTC = timezone.utc
PROB_SUM_TOLERANCE = 0.02


def parse_utc(value: Any) -> datetime | None:
    """Events carry a 'Z' suffix from the Polymarket API; snapshots use '+00:00'."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def load_config() -> dict[str, Any]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not config.get("paperOnly", True):
        raise RuntimeError("live execution is not implemented; paperOnly must remain true")
    return config


class EdgeAgent(WeatherAIAgent):
    """Small engine: read market, ask for a distribution, size it, score it."""

    def __init__(self, config: dict[str, Any]):
        WeatherDataStore.__init__(self, config)
        self.tz = ZoneInfo(str(config.get("timezone", "Asia/Shanghai")))
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS weather_ai_agent_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS weather_edge_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                target_date TEXT NOT NULL,
                decided_at_utc TEXT NOT NULL,
                local_minutes INTEGER NOT NULL,
                distribution_json TEXT NOT NULL,
                market_json TEXT NOT NULL,
                reasoning TEXT NOT NULL,
                lesson TEXT,
                state_hash TEXT,
                weather_state_json TEXT,
                trigger_reason TEXT,
                confidence REAL,
                market_error TEXT,
                invalidation TEXT,
                ai_brier REAL,
                market_brier REAL,
                ai_logloss REAL,
                market_logloss REAL,
                winning_range TEXT,
                scored_at_utc TEXT
            );
            CREATE TABLE IF NOT EXISTS weather_edge_orders (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL DEFAULT 'weather_edge_paper_v1',
                decision_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_range TEXT NOT NULL,
                side TEXT NOT NULL,
                requested_shares REAL NOT NULL,
                filled_shares REAL NOT NULL,
                fill_price REAL,
                fee_usdc REAL NOT NULL DEFAULT 0,
                notional_usdc REAL NOT NULL DEFAULT 0,
                ai_probability REAL,
                execution_probability REAL,
                market_probability REAL,
                net_edge REAL,
                book_midpoint REAL,
                slippage_bps REAL,
                levels_filled INTEGER,
                execution_mode TEXT NOT NULL DEFAULT 'FOK_PAPER',
                edge_reason TEXT NOT NULL,
                status TEXT NOT NULL,
                reject_reason TEXT,
                settled_payout_usdc REAL,
                realized_pnl_usdc REAL,
                created_at_utc TEXT NOT NULL,
                settled_at_utc TEXT,
                FOREIGN KEY(decision_id) REFERENCES weather_edge_decisions(decision_id)
            );
            CREATE TABLE IF NOT EXISTS weather_edge_lessons (
                lesson_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                city TEXT NOT NULL,
                lesson TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS weather_edge_feedback (
                feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL DEFAULT 'weather_edge_paper_v1',
                decision_id INTEGER NOT NULL UNIQUE,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                target_date TEXT NOT NULL,
                generated_at_utc TEXT NOT NULL,
                ai_brier REAL,
                market_brier REAL,
                realized_pnl_usdc REAL NOT NULL DEFAULT 0,
                assessment TEXT,
                mistakes_json TEXT NOT NULL DEFAULT '[]',
                lesson TEXT,
                status TEXT NOT NULL,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_edge_decisions_event
                ON weather_edge_decisions(strategy_name, event_id);
            CREATE INDEX IF NOT EXISTS idx_edge_orders_decision
                ON weather_edge_orders(decision_id);
            CREATE INDEX IF NOT EXISTS idx_edge_orders_open
                ON weather_edge_orders(status, event_id);
            """
        )
        order_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(weather_edge_orders)")}
        if "strategy_name" not in order_columns:
            self.db.execute(
                "ALTER TABLE weather_edge_orders ADD COLUMN strategy_name TEXT NOT NULL DEFAULT 'weather_edge_paper_v1'"
            )
        decision_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(weather_edge_decisions)")}
        for column, definition in (
            ("state_hash", "TEXT"),
            ("weather_state_json", "TEXT"),
            ("trigger_reason", "TEXT"),
            ("confidence", "REAL"),
            ("market_error", "TEXT"),
            ("invalidation", "TEXT"),
        ):
            if column not in decision_columns:
                self.db.execute(f"ALTER TABLE weather_edge_decisions ADD COLUMN {column} {definition}")
        for column, definition in (
            ("execution_probability", "REAL"),
            ("net_edge", "REAL"),
            ("book_midpoint", "REAL"),
            ("slippage_bps", "REAL"),
            ("levels_filled", "INTEGER"),
            ("execution_mode", "TEXT NOT NULL DEFAULT 'FOK_PAPER'"),
        ):
            if column not in order_columns:
                self.db.execute(f"ALTER TABLE weather_edge_orders ADD COLUMN {column} {definition}")
        feedback_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(weather_edge_feedback)")}
        if feedback_columns and "strategy_name" not in feedback_columns:
            self.db.execute(
                "ALTER TABLE weather_edge_feedback ADD COLUMN strategy_name TEXT NOT NULL DEFAULT 'weather_edge_paper_v1'"
            )
        self.db.commit()

    # ---------- market state ----------

    def open_events(self, now: datetime) -> list[sqlite3.Row]:
        cities = list(self.config["allowedCities"])
        placeholders = ",".join("?" for _ in cities)
        rows = self.db.execute(
            f"""
            SELECT e.event_id, e.city, e.target_date, e.end_date_utc, e.winning_range,
                   e.station_id, e.station_name, e.rules, s.timezone,
                   s.latitude, s.longitude
            FROM events e LEFT JOIN stations s ON s.station_id = e.station_id
            WHERE e.city IN ({placeholders})
              AND winning_range IS NULL
            ORDER BY end_date_utc
            """,
            tuple(cities),
        ).fetchall()
        # end_date_utc is stored with a "Z" suffix while iso_utc() emits "+00:00",
        # so this must be an instant comparison rather than a string one.
        return [r for r in rows if (parse_utc(r["end_date_utc"]) or now) > now]

    def evidence_packet(self, event: sqlite3.Row, now: datetime) -> dict[str, Any]:
        """Compact, point-in-time weather evidence for one decision.

        The packet is bounded so Hermes sees the information needed for a
        temperature path judgment without receiving raw multi-megabyte payloads.
        All rows are truncated at the decision timestamp to prevent leakage.
        """
        event_id = str(event["event_id"])
        as_of = iso_utc(now)
        observations = self.db.execute(
            """SELECT observation_time_utc, temperature_c, dewpoint_c,
                      cloud_cover_pct, wind_direction_deg, wind_speed, precipitation_mm,
                      weather_code, observed_daily_max_c, source
               FROM weather_observations WHERE station_id = ?
                 AND sample_local_date = ? AND observation_time_utc <= ?
               ORDER BY observation_time_utc DESC LIMIT 8""",
            (event["station_id"], event["target_date"], as_of),
        ).fetchall()
        process = self.db.execute(
            """SELECT slot_utc, primary_observation_time_utc, detected_processes_json, state_json
               FROM weather_process_states WHERE station_id = ? AND target_date = ?
                 AND slot_utc <= ? ORDER BY slot_utc DESC LIMIT 1""",
            (event["station_id"], event["target_date"], as_of),
        ).fetchone()
        models = self.db.execute(
            """SELECT model, slot_utc, version_hash, member_count, mean_max_c, std_max_c,
                      q10_max_c, q50_max_c, q90_max_c, model_run_time_utc
               FROM ensemble_forecasts WHERE station_id = ? AND target_date = ?
                 AND slot_utc <= ? ORDER BY slot_utc DESC LIMIT 12""",
            (event["station_id"], event["target_date"], as_of),
        ).fetchall()
        forecast = self.db.execute(
            """SELECT model, slot_utc, forecast_max_c, forecast_peak_local, status
               FROM external_forecasts WHERE station_id = ? AND target_date = ?
                 AND slot_utc <= ? ORDER BY slot_utc DESC LIMIT 12""",
            (event["station_id"], event["target_date"], as_of),
        ).fetchall()
        return {
            "asOfUtc": as_of,
            "station": {"id": event["station_id"], "name": event["station_name"],
                         "timezone": event["timezone"]},
            "observations": [dict(row) for row in observations],
            "process": {
                "slotUtc": process["slot_utc"],
                "primaryObservationUtc": process["primary_observation_time_utc"],
                "detected": json_value(process["detected_processes_json"], []),
                "state": json_value(process["state_json"], {}),
            } if process else None,
            "ensemble": [dict(row) for row in models],
            "forecasts": [dict(row) for row in forecast],
        }

    def market_buckets(self, event_id: str, now: datetime) -> list[dict[str, Any]]:
        """Latest snapshot per bucket, with real book depth retained."""
        max_age = int(self.config.get("maxMarketDataAgeMinutes", 20))
        floor = iso_utc(now - timedelta(minutes=max_age))
        rows = self.db.execute(
            """
            SELECT s.market_id, s.outcome_range, s.slot_utc, s.fetched_at_utc,
                   s.yes_best_bid, s.yes_best_ask, s.no_best_bid, s.no_best_ask,
                   s.gamma_yes_price, s.yes_book_json, s.no_book_json,
                   m.bucket_low, m.bucket_high
            FROM market_snapshots s
            JOIN markets m ON m.market_id = s.market_id
            WHERE s.event_id = ?
              AND s.slot_utc = (
                  SELECT MAX(slot_utc) FROM market_snapshots
                  WHERE event_id = s.event_id AND fetched_at_utc >= ?
              )
            ORDER BY COALESCE(m.bucket_low, m.bucket_high), m.bucket_high
            """,
            (event_id, floor),
        ).fetchall()
        buckets = []
        for row in rows:
            mid = self._market_probability(row)
            if mid is None:
                continue
            buckets.append(
                {
                    "marketId": row["market_id"],
                    "outcomeRange": row["outcome_range"],
                    "marketProbability": round(mid, 4),
                    "yesBid": as_float(row["yes_best_bid"]),
                    "yesAsk": as_float(row["yes_best_ask"]),
                    "noBid": as_float(row["no_best_bid"]),
                    "noAsk": as_float(row["no_best_ask"]),
                    "yesBookJson": row["yes_book_json"],
                    "noBookJson": row["no_book_json"],
                    "slotUtc": row["slot_utc"],
                }
            )
        return buckets

    @staticmethod
    def _market_probability(row: sqlite3.Row) -> float | None:
        """Market-implied YES probability, preferring a two-sided book.

        Only 39% of historical snapshots carry a non-null best bid, so the
        Gamma midpoint is the necessary fallback rather than an optional one.
        """
        bid = as_float(row["yes_best_bid"])
        ask = as_float(row["yes_best_ask"])
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        gamma = as_float(row["gamma_yes_price"])
        if gamma is not None:
            return gamma
        if ask is not None:
            return ask
        return bid

    def weather_anchor(self, city: str, target_date: str, now: datetime) -> dict[str, Any]:
        """Two numbers only: observed high so far, and heating hours left.

        Deliberately minimal. A prior audit of nine richer process features
        found zero tradable increment at 09:00/11:00 and six that were
        significantly harmful, so forecast and process detail are left out.
        """
        row = self.db.execute(
            """
            SELECT MAX(MAX(COALESCE(o.temperature_c, -99), COALESCE(o.observed_daily_max_c, -99))) AS observed_high,
                   MAX(o.observation_time_utc) AS last_obs
            FROM weather_observations o
            JOIN events e ON e.station_id = o.station_id
            WHERE e.city = ? AND e.target_date = ?
              AND o.sample_local_date = ?
              AND o.observation_time_utc <= ?
            """,
            (city, target_date, target_date, iso_utc(now)),
        ).fetchone()
        local = now.astimezone(self.tz)
        heating_hours_left = max(0.0, 15.0 - (local.hour + local.minute / 60.0))
        observed_high = as_float(row["observed_high"]) if row else None
        if observed_high is not None and observed_high <= -98.0:
            observed_high = None
        return {
            "observedHighC": observed_high,
            "lastObservationUtc": row["last_obs"] if row else None,
            "heatingHoursLeft": round(heating_hours_left, 1),
            "localTime": local.strftime("%H:%M"),
        }

    # ---------- accounting ----------

    def account_state(self) -> dict[str, float]:
        row = self.db.execute(
            """
            SELECT COALESCE(SUM(notional_usdc + fee_usdc), 0) AS spent,
                   COALESCE(SUM(COALESCE(settled_payout_usdc, 0)), 0) AS returned
            FROM weather_edge_orders
            WHERE status = 'FILLED'
            """
        ).fetchone()
        initial = float(self.config.get("initialCashUsdc", 100))
        spent = float(row["spent"] or 0.0)
        returned = float(row["returned"] or 0.0)
        return {"cash": initial - spent + returned, "spent": spent, "returned": returned}

    def open_notional(self, city: str | None = None) -> float:
        if city:
            row = self.db.execute(
                """
                SELECT COALESCE(SUM(notional_usdc + fee_usdc), 0) AS n
                FROM weather_edge_orders
                WHERE status = 'FILLED' AND settled_at_utc IS NULL AND city = ?
                """,
                (city,),
            ).fetchone()
        else:
            row = self.db.execute(
                """
                SELECT COALESCE(SUM(notional_usdc + fee_usdc), 0) AS n
                FROM weather_edge_orders
                WHERE status = 'FILLED' AND settled_at_utc IS NULL
                """
            ).fetchone()
        return float(row["n"] or 0.0)

    def taker_fee_usdc(self, shares: float, price: float) -> float:
        rate = float(self.config.get("feeRate", 0.05))
        return float(shares) * rate * float(price) * (1.0 - float(price))

    def recent_lessons(self, city: str) -> list[str]:
        limit = int(self.config.get("lessonsPerCity", 8))
        rows = self.db.execute(
            """
            SELECT lesson FROM weather_edge_lessons
            WHERE strategy_name = ? AND city = ?
            ORDER BY lesson_id DESC LIMIT ?
            """,
            (self.strategy_name, city, limit),
        ).fetchall()
        return [row["lesson"] for row in rows]

    def recent_scores(self, city: str, limit: int = 5) -> list[dict[str, Any]]:
        """Past distribution scores, so the AI sees whether it is beating the market."""
        rows = self.db.execute(
            """
            SELECT target_date, ai_brier, market_brier, winning_range
            FROM weather_edge_decisions
            WHERE strategy_name = ? AND city = ? AND ai_brier IS NOT NULL
            ORDER BY decision_id DESC LIMIT ?
            """,
            (self.strategy_name, city, limit),
        ).fetchall()
        return [
            {
                "date": row["target_date"],
                "aiBrier": round(float(row["ai_brier"]), 4),
                "marketBrier": round(float(row["market_brier"]), 4),
                "beatMarket": float(row["ai_brier"]) < float(row["market_brier"]),
                "winner": row["winning_range"],
            }
            for row in rows
        ]

    def recent_feedback(self, city: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT target_date, assessment, mistakes_json, lesson, status
               FROM weather_edge_feedback WHERE strategy_name = ? AND city = ?
               ORDER BY feedback_id DESC LIMIT ?""",
            (self.strategy_name, city, limit),
        ).fetchall()
        return [
            {"date": row["target_date"], "assessment": row["assessment"],
             "mistakes": json_value(row["mistakes_json"], []), "lesson": row["lesson"],
             "status": row["status"]}
            for row in rows
        ]

    def market_calibration(self, city: str, before_target_date: str | None = None, limit: int = 200) -> dict[str, Any]:
        """Reliability summary of the market probabilities seen by this agent.

        Only resolved decisions strictly before the current event are eligible,
        so this cannot leak settlement information into a live prompt.
        """
        rows = self.db.execute(
            """SELECT market_json, winning_range, target_date, ai_brier, market_brier
               FROM weather_edge_decisions
               WHERE strategy_name = ? AND city = ? AND winning_range IS NOT NULL
                 AND ai_brier IS NOT NULL
                 AND (? IS NULL OR target_date < ?)
               ORDER BY decision_id DESC LIMIT ?""",
            (self.strategy_name, city, before_target_date, before_target_date, limit),
        ).fetchall()
        bins: dict[int, dict[str, float]] = {}
        for row in rows:
            for item in json_value(row["market_json"], []):
                probability = as_float(item.get("marketProbability"))
                if probability is None or not 0 <= probability <= 1:
                    continue
                bucket = min(9, int(probability * 10))
                state = bins.setdefault(bucket, {"count": 0, "wins": 0, "probability": 0.0})
                state["count"] += 1
                state["wins"] += int(str(item.get("outcomeRange")) == str(row["winning_range"]))
                state["probability"] += probability
        reliability = []
        for bucket, state in sorted(bins.items()):
            count = int(state["count"])
            reliability.append({
                "range": f"{bucket / 10:.1f}-{(bucket + 1) / 10:.1f}",
                "count": count,
                "meanProbability": round(state["probability"] / count, 4),
                "observedRate": round(state["wins"] / count, 4),
            })
        return {
            "sampleDecisions": len(rows),
            "sampleMarkets": sum(int(item["count"]) for item in reliability),
            "reliability": reliability,
            "use": "descriptive_prior_only",
        }

    def _trigger_state(self, event: sqlite3.Row, buckets: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
        end = parse_utc(event["end_date_utc"])
        hours_to_close = ((end - now).total_seconds() / 3600.0) if end else None
        anchor = self.weather_anchor(event["city"], event["target_date"], now)
        model = self.latest_meteoblue(event, now)
        return {
            "market": {
                b["marketId"]: {
                    "probability": round(float(b["marketProbability"]), 4),
                    "yesAsk": b["yesAsk"], "noAsk": b["noAsk"],
                } for b in buckets
            },
            "weather": {
                "observedHighC": anchor.get("observedHighC"),
                "lastObservationUtc": anchor.get("lastObservationUtc"),
                "modelMaxC": model.get("maxC") if model else None,
                "modelRevisionC": model.get("revisionC") if model else None,
                "modelSlotUtc": model.get("sampleSlotUtc") if model else None,
            },
            "hoursToClose": round(hours_to_close, 2) if hours_to_close is not None else None,
        }

    def review_due(
        self, event: sqlite3.Row, buckets: list[dict[str, Any]], now: datetime
    ) -> tuple[bool, dict[str, Any], str]:
        state = self._trigger_state(event, buckets, now)
        previous = self.db.execute(
            """SELECT decided_at_utc, state_hash, weather_state_json
               FROM weather_edge_decisions WHERE strategy_name = ? AND event_id = ?
               ORDER BY decision_id DESC LIMIT 1""",
            (self.strategy_name, str(event["event_id"])),
        ).fetchone()
        if previous is None:
            return True, state, "initial"
        decided = parse_utc(previous["decided_at_utc"])
        elapsed = (now - decided).total_seconds() / 60.0 if decided else float("inf")
        min_interval = float(self.config.get("minDecisionIntervalMinutes", 30))
        if elapsed < min_interval:
            return False, state, "cooldown"
        try:
            old = json.loads(previous["weather_state_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            old = {}
        reasons: list[str] = []
        old_market, new_market = old.get("market") or {}, state["market"]
        market_move = 0.0
        if set(old_market) != set(new_market):
            reasons.append("market_universe_change")
        for market_id, current in new_market.items():
            prior = old_market.get(market_id) or {}
            for key in ("probability", "yesAsk", "noAsk"):
                before, after = as_float(prior.get(key)), as_float(current.get(key))
                if before is not None and after is not None:
                    market_move = max(market_move, abs(after - before))
        if market_move >= float(self.config.get("marketMoveTrigger", 0.04)):
            reasons.append(f"market_move_{market_move:.3f}")
        old_weather, new_weather = old.get("weather") or {}, state["weather"]
        weather_move = 0.0
        for key in ("observedHighC", "modelMaxC"):
            before, after = as_float(old_weather.get(key)), as_float(new_weather.get(key))
            if before is not None and after is not None:
                weather_move = max(weather_move, abs(after - before))
        if weather_move >= float(self.config.get("weatherMoveTriggerC", 0.5)):
            reasons.append(f"weather_move_{weather_move:.2f}C")
        if old_weather.get("modelSlotUtc") != new_weather.get("modelSlotUtc"):
            reasons.append("model_revision")
        old_hours, new_hours = as_float(old.get("hoursToClose")), as_float(state.get("hoursToClose"))
        if old_hours is not None and new_hours is not None:
            if any(old_hours > mark >= new_hours for mark in (12, 8, 4)):
                reasons.append("phase_change")
        heartbeat = float(self.config.get("heartbeatReviewMinutes", 120))
        if elapsed >= heartbeat:
            reasons.append("heartbeat")
        if not reasons:
            return False, state, "no_material_change"
        return True, state, "+".join(dict.fromkeys(reasons))

    # ---------- the ask ----------

    def build_input(
        self, event: sqlite3.Row, buckets: list[dict[str, Any]], now: datetime,
        trigger_reason: str | None = None,
    ) -> dict[str, Any]:
        city = event["city"]
        account = self.account_state()
        local = now.astimezone(self.tz)
        end = parse_utc(event["end_date_utc"])
        hours_to_close = ((end - now).total_seconds() / 3600.0) if end else None
        valid_spreads = [
            b["yesAsk"] - b["yesBid"] for b in buckets
            if b["yesAsk"] is not None and b["yesBid"] is not None
        ]
        market_total = sum(float(b["marketProbability"]) for b in buckets)
        invariant_status = "VALID" if abs(market_total - 1.0) <= 0.08 else "INCOMPLETE_OR_CROSSED"
        if hours_to_close is not None and hours_to_close <= 4:
            mode = "SETTLEMENT_CONVERGENCE"
        elif hours_to_close is not None and hours_to_close <= 8:
            mode = "LATE_DISCOVERY"
        else:
            mode = "EARLY_DISCOVERY"
        return {
            "eventId": str(event["event_id"]),
            "city": city,
            "targetDate": event["target_date"],
            "asOfUtc": iso_utc(now),
            "weatherAnchor": self.weather_anchor(city, event["target_date"], now),
            "evidence": self.evidence_packet(event, now),
            "marketRegime": {
                "mode": mode,
                "triggerReason": trigger_reason or "manual",
                "hoursToClose": round(hours_to_close, 2) if hours_to_close is not None else None,
                "medianYesSpread": round(sorted(valid_spreads)[len(valid_spreads) // 2], 4)
                if valid_spreads else None,
                "marketProbabilitySum": round(market_total, 4),
                "probabilityInvariant": invariant_status,
                "tradeMode": "SELECTIVE_MISPRICING_ONLY",
                "defaultAction": "WAIT",
            },
            "buckets": [
                {
                    "outcomeRange": b["outcomeRange"],
                    "marketProbability": b["marketProbability"],
                    "yesBid": b["yesBid"],
                    "yesAsk": b["yesAsk"],
                    "noBid": b["noBid"],
                    "noAsk": b["noAsk"],
                }
                for b in buckets
            ],
            "cashAvailableUsdc": round(account["cash"], 2),
            "yourPastScores": self.recent_scores(city),
            "yourPastFeedback": self.recent_feedback(city),
            "marketCalibration": self.market_calibration(city, event["target_date"]),
            "yourLessons": self.recent_lessons(city),
        }

    def _prompt(self, payload: dict[str, Any]) -> str:
        baseline = self.config.get("marketBaselineBrier", 0.239)
        cutoff = int(self.config.get("entryCutoffLocalMinutes", 780))
        return (
            "你在 Polymarket 的\"当日最高温\"市场上做纸面交易。你的唯一目标：比市场更准确地判断"
            "每个温度桶的真实概率，找出市场定价不合理的地方，并把它变成收益。\n\n"
            "任务：对输入 buckets 里的每一个桶给出你的概率，构成一个完整分布（和为 1）。"
            "然后对比市场概率，决定是否下单。\n\n"
            "评分方式（这是你要优化的目标）：结算后系统会用 Brier score 同时给你的分布和市场的分布打分，"
            f"并记录你是否赢过市场。市场的历史基准约为 {baseline}，这就是你要打败的靶子。"
            "你过去的成绩在 yourPastScores 里，赢过市场才说明你真的有 edge。\n\n"
            "yourPastFeedback 是已经结算的复盘摘要。把它当作待验证经验，不能因为一次输赢就改变规则。\n\n"
            "重要事实（来自本系统的历史回测，不是猜测）：在这些市场上，市场价格比所有公开气象源都更准确，"
            "没有任何气象源在任何时点赢过市场隐含期望温。所以市场是很强的先验，默认它是对的。"
            "只在你能具体说出市场漏掉了什么的时候，才偏离它。仅仅是\"便宜\"或\"和某个模型不同\"不构成理由。\n\n"
            "当前市场模式是 SELECTIVE_MISPRICING_ONLY：默认 WAIT。越接近结算，市场越快收敛，"
            "因此只有独立、最新、可证伪的天气证据足以覆盖交易成本时才下单，不追逐已经收敛的价格。\n\n"
            "先检查 marketRegime.probabilityInvariant。若市场全桶概率和明显偏离 1，视为盘口不完整或交叉，必须 WAIT。"
            "AI 输出的分布必须严格覆盖每个桶且和为 1；Python 会再次检查，不能用不变量异常制造 edge。\n\n"
            "evidence 是严格截断到 asOfUtc 的证据包：只包含最近观测、最新模型统计和天气过程状态。"
            "先用这些证据形成基线，再决定是否有足够理由偏离市场；不要把单一模型当作真值。"
            "结算按全天最高记录，已观测到的高温不会消失，这是一个硬约束。\n\n"
            "先给出完整分布，再给出 confidence（0 到 1）。confidence 表示证据能否重复验证，不是主观兴奋程度。"
            "证据不足时 confidence 应低，即使你认为某个桶便宜也必须 WAIT。\n\n"
            "下单规则：orders 可以为空，空是正常且常见的结果，不要为了有动作而下单。"
            f"当地时间 {cutoff // 60}:00 之后不接受新开仓。买入后持有到结算，没有中途平仓。"
            "股数由你决定，Python 会按真实盘口深度、手续费和资金上限复核，可能减量或直接拒绝——"
            "盘口通常很薄，报大单只会被削掉。\n\n"
            "如果你发现了一条值得在未来同类判断中复用的机制性经验，写进 lesson；"
            "一次性的结果或行情复述不要写，没有就填 null。yourLessons 是你自己过去留下的。\n\n"
            "输入：\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        )

    def ask_ai(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = self._prompt(payload)
        logging.info(
            "edge review start event=%s city=%s prompt_chars=%d buckets=%d",
            payload["eventId"], payload["city"], len(prompt), len(payload["buckets"]),
        )
        response = self._run_ai(SCHEMA_PATH, prompt, circuit_scope=f"{self.strategy_name}:{payload['eventId']}")
        logging.info(
            "edge review done event=%s orders=%d",
            payload["eventId"], len(response.get("orders") or []),
        )
        return response

    def validate(self, payload: dict[str, Any], response: dict[str, Any]) -> None:
        if str(response.get("eventId")) != payload["eventId"]:
            raise RuntimeError("AI response eventId does not match the request")
        expected = {b["outcomeRange"] for b in payload["buckets"]}
        distribution = response.get("distribution") or []
        ranges = [str(row.get("outcomeRange")) for row in distribution]
        if len(ranges) != len(set(ranges)):
            raise RuntimeError("distribution must contain each bucket exactly once")
        got = {row["outcomeRange"] for row in distribution}
        if got != expected:
            missing = sorted(expected - got)
            extra = sorted(got - expected)
            raise RuntimeError(f"distribution must cover every bucket exactly; missing={missing} extra={extra}")
        total = sum(float(row["probability"]) for row in distribution)
        if abs(total - 1.0) > PROB_SUM_TOLERANCE:
            raise RuntimeError(f"distribution must sum to 1.0 within {PROB_SUM_TOLERANCE}; got {total:.4f}")
        confidence = response.get("confidence", 0.5)
        if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            raise RuntimeError("confidence must be between 0 and 1")
        if response.get("marketError") == "NONE" and response.get("orders"):
            raise RuntimeError("marketError=NONE cannot contain orders")
        order_keys: set[tuple[str, str]] = set()
        orders = response.get("orders") or []
        max_orders = int(self.config.get("maxOrdersPerDecision", 1))
        if len(orders) > max_orders:
            raise RuntimeError(f"a decision may contain at most {max_orders} order")
        for order in orders:
            if order["outcomeRange"] not in expected:
                raise RuntimeError(f"order references an unknown bucket: {order['outcomeRange']}")
            key = (str(order["outcomeRange"]), str(order["side"]))
            if key in order_keys:
                raise RuntimeError("orders may contain at most one order per bucket and side")
            order_keys.add(key)

    def _deterministic_shares(self, probability: float, price: float, confidence: float) -> float:
        """Fractional-Kelly size with confidence shrinkage; AI cannot set size."""
        if not 0 < price < 1 or probability <= price:
            return 0.0
        odds = (1.0 - price) / price
        kelly = max(0.0, (probability * odds - (1.0 - probability)) / odds)
        fraction = float(self.config.get("fractionalKelly", 0.15))
        confidence_factor = max(float(self.config.get("confidenceFloor", 0.25)), min(1.0, confidence))
        budget = max(0.0, self.account_state()["cash"] - float(self.config.get("minCashReserveUsdc", 10)))
        shares = budget * kelly * fraction * confidence_factor / price
        cap = float(self.config.get("maxSharesPerOrder", 100))
        return round(min(cap, max(0.0, shares)), 4)

    def _execution_probability(
        self, ai_probability: float, market_probability: float,
        confidence: float, hours_to_close: float | None,
    ) -> float:
        """Shrink model deviations toward the market prior before acting."""
        if hours_to_close is not None and hours_to_close <= 4:
            shrink = float(self.config.get("executionShrinkSettlement", 0.35))
        elif hours_to_close is not None and hours_to_close <= 8:
            shrink = float(self.config.get("executionShrinkLate", 0.50))
        else:
            shrink = float(self.config.get("executionShrinkEarly", 0.65))
        shrink = max(0.0, min(1.0, shrink))
        confidence = max(0.0, min(1.0, confidence))
        return max(0.0, min(1.0, market_probability + (ai_probability - market_probability) * shrink * confidence))

    # ---------- execution ----------

    def _entry_open(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        minutes = local.hour * 60 + local.minute
        start = int(self.config.get("reviewStartLocalMinutes", 480))
        cutoff = int(self.config.get("entryCutoffLocalMinutes", 780))
        return start <= minutes < cutoff

    def _analysis_open(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        minutes = local.hour * 60 + local.minute
        start = int(self.config.get("reviewStartLocalMinutes", 420))
        end = int(self.config.get("analysisEndLocalMinutes", 1200))
        return start <= minutes < end

    def execute_orders(
        self,
        decision_id: int,
        event: sqlite3.Row,
        buckets: list[dict[str, Any]],
        response: dict[str, Any],
        now: datetime,
    ) -> list[dict[str, Any]]:
        by_range = {b["outcomeRange"]: b for b in buckets}
        probabilities = {row["outcomeRange"]: float(row["probability"]) for row in response["distribution"]}
        city = event["city"]
        end = parse_utc(event["end_date_utc"])
        hours_to_close = ((end - now).total_seconds() / 3600.0) if end else None
        results = []
        for order in response.get("orders") or []:
            bucket = by_range[order["outcomeRange"]]
            side = order["side"]
            outcome = "no" if side == "NO" else "yes"
            ai_prob = probabilities[order["outcomeRange"]]
            if side == "NO":
                ai_prob = 1.0 - ai_prob
            market_prob = bucket["marketProbability"]
            if side == "NO":
                market_prob = 1.0 - market_prob
            edge = ai_prob - market_prob
            ask_price, _ = executable_vwap(
                bucket["noBookJson"] if outcome == "no" else bucket["yesBookJson"], 1.0, "asks"
            )
            confidence = float(response.get("confidence", 0.5))
            execution_prob = self._execution_probability(ai_prob, market_prob, confidence, hours_to_close)
            deterministic_shares = self._deterministic_shares(
                execution_prob, ask_price or (bucket["noAsk"] if outcome == "no" else bucket["yesAsk"]), confidence
            )
            record = {
                "decision_id": decision_id,
                "event_id": str(event["event_id"]),
                "city": city,
                "market_id": bucket["marketId"],
                "outcome_range": order["outcomeRange"],
                "side": side,
                "requested_shares": deterministic_shares,
                "ai_probability": round(ai_prob, 4),
                "execution_probability": round(execution_prob, 4),
                "market_probability": round(market_prob, 4),
                "edge": round(edge, 4),
                "net_edge": None,
                "book_midpoint": None,
                "slippage_bps": None,
                "levels_filled": 0,
                "execution_mode": "FOK_PAPER",
                "edge_reason": str(order["edgeReason"]),
            }
            reject = self._reject_reason(record, bucket, outcome, now)
            if reject:
                record.update(
                    {"filled_shares": 0.0, "fill_price": None, "fee_usdc": 0.0,
                     "notional_usdc": 0.0, "status": "REJECTED", "reject_reason": reject}
                )
            else:
                record.update(self._fill(record, bucket, outcome))
            self._persist_order(record, now)
            results.append(record)
        return results

    def _reject_reason(
        self, record: dict[str, Any], bucket: dict[str, Any], outcome: str, now: datetime,
    ) -> str | None:
        if not self._entry_open(now):
            cutoff = int(self.config.get("entryCutoffLocalMinutes", 780))
            return f"entry window closed (local cutoff {cutoff // 60}:00)"
        shares = min(record["requested_shares"], float(self.config.get("maxSharesPerOrder", 100)))
        local_hour = now.astimezone(self.tz).hour
        if local_hour < 12:
            default_edge = 0.10
        elif local_hour < 15:
            default_edge = 0.08
        else:
            default_edge = float(self.config.get("minEdge", 0.05))
        min_edge = max(float(self.config.get("minEdge", 0.05)), default_edge)
        existing = self.db.execute(
            """
            SELECT 1 FROM weather_edge_orders
            WHERE strategy_name = ? AND event_id = ? AND market_id = ?
              AND status = 'FILLED' AND settled_at_utc IS NULL
            LIMIT 1
            """,
            (self.strategy_name, record["event_id"], record["market_id"]),
        ).fetchone()
        if existing:
            return "an open position already exists for this market"
        book = bucket["noBookJson"] if outcome == "no" else bucket["yesBookJson"]
        price, _ = executable_vwap(book, shares, "asks")
        if price is None:
            return "book cannot fill the requested size at any price"
        bid_key, ask_key = (("noBid", "noAsk") if outcome == "no" else ("yesBid", "yesAsk"))
        midpoint = (
            (as_float(bucket[bid_key]) + as_float(bucket[ask_key])) / 2.0
            if as_float(bucket[bid_key]) is not None and as_float(bucket[ask_key]) is not None
            else None
        )
        record["book_midpoint"] = midpoint
        record["slippage_bps"] = (
            max(0.0, (price - midpoint) / midpoint * 10000.0)
            if midpoint and midpoint > 0 else None
        )
        all_in = price + self.taker_fee_usdc(1.0, price)
        net_edge = record.get("execution_probability", record["ai_probability"]) - all_in
        record["net_edge"] = net_edge
        if net_edge < min_edge:
            return f"net edge {net_edge:.4f} is below the {min_edge:.4f} minimum after execution cost"
        cap = float(self.config.get("maxAllInCostPerShare", 0.92))
        if all_in >= cap:
            return f"all-in cost per share {all_in:.4f} is at or above the {cap} cap"
        cost = shares * price + self.taker_fee_usdc(shares, price)
        account = self.account_state()
        reserve = float(self.config.get("minCashReserveUsdc", 10))
        if account["cash"] - cost < reserve:
            return "order would breach the minimum cash reserve"
        if self.open_notional(record["city"]) + cost > float(self.config.get("maxOpenNotionalPerCity", 40)):
            return "per-city open notional limit exceeded"
        if self.open_notional() + cost > float(self.config.get("maxOpenNotionalTotal", 90)):
            return "total open notional limit exceeded"
        return None

    def _fill(self, record: dict[str, Any], bucket: dict[str, Any], outcome: str) -> dict[str, Any]:
        """Size down to whatever the real book, cash, and caps actually allow."""
        book = bucket["noBookJson"] if outcome == "no" else bucket["yesBookJson"]
        shares = min(record["requested_shares"], float(self.config.get("maxSharesPerOrder", 100)))
        account = self.account_state()
        reserve = float(self.config.get("minCashReserveUsdc", 10))
        city_room = float(self.config.get("maxOpenNotionalPerCity", 40)) - self.open_notional(record["city"])
        total_room = float(self.config.get("maxOpenNotionalTotal", 90)) - self.open_notional()
        budget = min(account["cash"] - reserve, city_room, total_room)
        while shares > 0:
            price, _ = executable_vwap(book, shares, "asks")
            if price is not None:
                cost = shares * price + self.taker_fee_usdc(shares, price)
                if cost <= budget:
                    levels = sum(1 for level in book_levels(book, "asks") if level["price"] <= price + 1e-9)
                    return {
                        "filled_shares": round(shares, 4),
                        "fill_price": round(price, 6),
                        "fee_usdc": round(self.taker_fee_usdc(shares, price), 6),
                        "notional_usdc": round(shares * price, 6),
                        "status": "FILLED",
                        "reject_reason": None,
                        "levels_filled": levels,
                    }
            shares = round(shares - 1.0, 4)
        return {
            "filled_shares": 0.0, "fill_price": None, "fee_usdc": 0.0, "notional_usdc": 0.0,
            "status": "REJECTED", "reject_reason": "no affordable size clears the book and caps",
            "levels_filled": 0,
        }

    def _persist_order(self, record: dict[str, Any], now: datetime) -> None:
        self.db.execute(
            """
            INSERT INTO weather_edge_orders (
                strategy_name, decision_id, event_id, city, market_id, outcome_range, side,
                requested_shares, filled_shares, fill_price, fee_usdc, notional_usdc,
                ai_probability, execution_probability, market_probability, net_edge, book_midpoint, slippage_bps,
                levels_filled, execution_mode, edge_reason, status, reject_reason,
                created_at_utc
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                self.strategy_name, record["decision_id"], record["event_id"], record["city"], record["market_id"],
                record["outcome_range"], record["side"], record["requested_shares"],
                record["filled_shares"], record["fill_price"], record["fee_usdc"],
                record["notional_usdc"], record["ai_probability"], record.get("execution_probability"), record["market_probability"],
                record.get("net_edge"), record.get("book_midpoint"), record.get("slippage_bps"),
                record.get("levels_filled", 0), record.get("execution_mode", "FOK_PAPER"),
                record["edge_reason"], record["status"], record["reject_reason"], iso_utc(now),
            ),
        )
        self.db.commit()

    # ---------- one decision ----------

    def review_event(
        self, event: sqlite3.Row, now: datetime, buckets: list[dict[str, Any]] | None = None,
        trigger_state: dict[str, Any] | None = None, trigger_reason: str | None = None,
    ) -> dict[str, Any]:
        event_id = str(event["event_id"])
        buckets = buckets or self.market_buckets(event_id, now)
        if len(buckets) < 2:
            return {"eventId": event_id, "status": "skipped", "reason": "no fresh market snapshot"}
        trigger_state = trigger_state or self._trigger_state(event, buckets, now)
        trigger_reason = trigger_reason or "manual"
        payload = self.build_input(event, buckets, now, trigger_reason)
        response = self.ask_ai(payload)
        self.validate(payload, response)
        # A slow AI response must never trade against a book that changed while
        # it was thinking. Persist the decision for audit, but reject execution.
        latest_buckets = self.market_buckets(event_id, utc_now())
        initial_slots = {b["marketId"]: b["slotUtc"] for b in buckets}
        latest_slots = {b["marketId"]: b["slotUtc"] for b in latest_buckets}
        market_changed = initial_slots != latest_slots
        local = now.astimezone(self.tz)
        cursor = self.db.execute(
            """
            INSERT INTO weather_edge_decisions (
                strategy_name, event_id, city, target_date, decided_at_utc, local_minutes,
                distribution_json, market_json, reasoning, lesson, state_hash,
                weather_state_json, trigger_reason, confidence, market_error, invalidation
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                self.strategy_name, event_id, event["city"], event["target_date"], iso_utc(now),
                local.hour * 60 + local.minute,
                json.dumps(response["distribution"], ensure_ascii=False),
                json.dumps(
                    [{"outcomeRange": b["outcomeRange"], "marketProbability": b["marketProbability"]}
                     for b in buckets],
                    ensure_ascii=False,
                ),
                str(response["reasoning"]), response.get("lesson"),
                hashlib.sha256(
                    json.dumps(trigger_state, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                json.dumps(trigger_state, ensure_ascii=False, separators=(",", ":")), trigger_reason,
                float(response.get("confidence", 0.5)), response.get("marketError"), response.get("invalidation"),
            ),
        )
        decision_id = int(cursor.lastrowid)
        lesson = (response.get("lesson") or "").strip()
        if lesson:
            self.db.execute(
                "INSERT INTO weather_edge_lessons (strategy_name, city, lesson, created_at_utc) VALUES (?,?,?,?)",
                (self.strategy_name, event["city"], lesson, iso_utc(now)),
            )
        self.db.commit()
        invariant_ok = payload["marketRegime"]["probabilityInvariant"] == "VALID"
        orders = (
            [] if market_changed or not invariant_ok
            else self.execute_orders(decision_id, event, buckets, response, utc_now())
        )
        if market_changed:
            logging.info("edge decision id=%s skipped execution because market changed during AI call", decision_id)
        elif not invariant_ok:
            logging.info("edge decision id=%s skipped execution because market probability invariant failed", decision_id)
        filled = [o for o in orders if o["status"] == "FILLED"]
        logging.info(
            "edge decision persisted id=%s city=%s orders=%d filled=%d notional=%.2f",
            decision_id, event["city"], len(orders), len(filled),
            sum(o["notional_usdc"] for o in filled),
        )
        return {
            "eventId": event_id, "status": "completed", "decisionId": decision_id,
            "orders": len(orders), "filled": len(filled),
        }

    # ---------- scoring: the iteration target ----------

    @staticmethod
    def _brier_and_logloss(
        distribution: list[dict[str, Any]], winner: str,
    ) -> tuple[float, float] | None:
        """Multi-class Brier plus log-loss for the winning bucket."""
        ranges = [row["outcomeRange"] for row in distribution]
        if winner not in ranges:
            return None
        total = sum(max(0.0, float(row["probability"])) for row in distribution)
        if total <= 0:
            return None
        brier = 0.0
        winner_p = 0.0
        for row in distribution:
            p = max(0.0, float(row["probability"])) / total
            actual = 1.0 if row["outcomeRange"] == winner else 0.0
            brier += (p - actual) ** 2
            if row["outcomeRange"] == winner:
                winner_p = p
        import math

        logloss = -math.log(max(winner_p, 1e-9))
        return brier, logloss

    def score_settled(self, now: datetime) -> list[dict[str, Any]]:
        """Score every unscored decision whose event has resolved, and settle fills."""
        rows = self.db.execute(
            """
            SELECT d.decision_id, d.event_id, d.city, d.target_date,
                   d.distribution_json, d.market_json, e.winning_range
            FROM weather_edge_decisions d
            JOIN events e ON e.event_id = d.event_id
            WHERE d.strategy_name = ? AND d.scored_at_utc IS NULL
              AND e.winning_range IS NOT NULL
            ORDER BY d.decision_id
            """,
            (self.strategy_name,),
        ).fetchall()
        scored = []
        for row in rows:
            winner = row["winning_range"]
            ai = self._brier_and_logloss(json.loads(row["distribution_json"]), winner)
            market_dist = [
                {"outcomeRange": b["outcomeRange"], "probability": b["marketProbability"]}
                for b in json.loads(row["market_json"])
            ]
            market = self._brier_and_logloss(market_dist, winner)
            if ai is None or market is None:
                logging.warning(
                    "decision %s: winner %r absent from the bucket set; marking scored without a score",
                    row["decision_id"], winner,
                )
                self.db.execute(
                    "UPDATE weather_edge_decisions SET scored_at_utc=?, winning_range=? WHERE decision_id=?",
                    (iso_utc(now), winner, row["decision_id"]),
                )
                self.db.commit()
                continue
            self.db.execute(
                """
                UPDATE weather_edge_decisions
                SET ai_brier=?, market_brier=?, ai_logloss=?, market_logloss=?,
                    winning_range=?, scored_at_utc=?
                WHERE decision_id=?
                """,
                (ai[0], market[0], ai[1], market[1], winner, iso_utc(now), row["decision_id"]),
            )
            self._settle_orders(row["decision_id"], winner, now)
            self.db.commit()
            try:
                self.review_feedback(row, winner, now)
            except Exception:
                logging.exception("feedback persistence failed decision=%s", row["decision_id"])
            scored.append(
                {
                    "decisionId": row["decision_id"], "city": row["city"], "date": row["target_date"],
                    "winner": winner, "aiBrier": round(ai[0], 4), "marketBrier": round(market[0], 4),
                    "beatMarket": ai[0] < market[0],
                }
            )
        return scored

    def _settle_orders(self, decision_id: int, winner: str, now: datetime) -> None:
        orders = self.db.execute(
            """
            SELECT order_id, outcome_range, side, filled_shares, notional_usdc, fee_usdc
            FROM weather_edge_orders
            WHERE decision_id = ? AND status = 'FILLED' AND settled_at_utc IS NULL
            """,
            (decision_id,),
        ).fetchall()
        for order in orders:
            won = (order["outcome_range"] == winner) if order["side"] == "YES" else (order["outcome_range"] != winner)
            payout = float(order["filled_shares"]) if won else 0.0
            pnl = payout - float(order["notional_usdc"]) - float(order["fee_usdc"])
            self.db.execute(
                """
                UPDATE weather_edge_orders
                SET settled_payout_usdc=?, realized_pnl_usdc=?, settled_at_utc=?
                WHERE order_id=?
                """,
                (round(payout, 6), round(pnl, 6), iso_utc(now), order["order_id"]),
            )

    def _feedback_prompt(self, decision: sqlite3.Row, winner: str, orders: list[dict[str, Any]]) -> str:
        payload = {
            "eventId": str(decision["event_id"]), "city": decision["city"],
            "targetDate": decision["target_date"], "winner": winner,
            "distribution": json.loads(decision["distribution_json"]),
            "market": json.loads(decision["market_json"]), "orders": orders,
            "aiBrier": decision["ai_brier"], "marketBrier": decision["market_brier"],
        }
        return (
            "你是天气市场交易系统的独立复盘员。只根据决策时已经记录的分布、市场分布、订单和最终赢家复盘，"
            "不要补充决策时不可知的信息。区分预测质量、市场错价判断和运气；指出一个最小可验证改进。"
            "如果没有稳定机制，不要硬写 lesson。只输出 JSON。\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )

    def review_feedback(self, decision: sqlite3.Row, winner: str, now: datetime) -> dict[str, Any]:
        orders = [dict(row) for row in self.db.execute(
            """SELECT market_id, outcome_range, side, filled_shares, fill_price,
                      realized_pnl_usdc, status, reject_reason
               FROM weather_edge_orders WHERE decision_id = ? ORDER BY order_id""",
            (decision["decision_id"],),
        ).fetchall()]
        pnl = sum(float(row.get("realized_pnl_usdc") or 0.0) for row in orders)
        response: dict[str, Any] | None = None
        error = None
        if bool(self.config.get("feedbackAiEnabled", True)):
            try:
                response = self._run_ai(
                    FEEDBACK_SCHEMA_PATH, self._feedback_prompt(decision, winner, orders),
                    timeout_seconds=int(self.config.get("feedbackAiTimeoutSeconds", 180)),
                    circuit_scope=f"feedback:{decision['event_id']}",
                )
            except Exception as exc:
                error = str(exc)
        assessment = (response or {}).get("assessment")
        mistakes = (response or {}).get("mistakes") or []
        lesson = (response or {}).get("lesson")
        status = "completed" if response else "pending"
        self.db.execute(
            """INSERT OR REPLACE INTO weather_edge_feedback(
                 strategy_name, decision_id, event_id, city, target_date, generated_at_utc,
                 ai_brier, market_brier, realized_pnl_usdc, assessment, mistakes_json, lesson, status, error
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.strategy_name, decision["decision_id"], decision["event_id"], decision["city"],
             decision["target_date"], iso_utc(now), decision["ai_brier"], decision["market_brier"],
             round(pnl, 6), assessment, json.dumps(mistakes, ensure_ascii=False), lesson, status, error),
        )
        if lesson:
            self.db.execute(
                "INSERT INTO weather_edge_lessons(strategy_name, city, lesson, created_at_utc) VALUES(?,?,?,?)",
                (self.strategy_name, decision["city"], str(lesson), iso_utc(now)),
            )
        self.db.commit()
        return {"decisionId": decision["decision_id"], "status": status, "error": error}

    def retry_pending_feedback(self, now: datetime) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT d.* FROM weather_edge_decisions d
               JOIN events e ON e.event_id = d.event_id
               LEFT JOIN weather_edge_feedback f ON f.decision_id = d.decision_id
               WHERE d.strategy_name = ? AND d.scored_at_utc IS NOT NULL
                 AND f.feedback_id IS NULL AND e.winning_range IS NOT NULL
               ORDER BY d.decision_id LIMIT ?""",
            (self.strategy_name, int(self.config.get("maxFeedbackPerRun", 1))),
        ).fetchall()
        return [self.review_feedback(row, row["winning_range"], now) for row in rows]

    # ---------- reporting ----------

    def summary(self) -> dict[str, Any]:
        account = self.account_state()
        pnl = self.db.execute(
            """
            SELECT COUNT(*) n, COALESCE(SUM(realized_pnl_usdc), 0) pnl,
                   COALESCE(SUM(notional_usdc + fee_usdc), 0) staked
            FROM weather_edge_orders
            WHERE status = 'FILLED' AND settled_at_utc IS NOT NULL
            """
        ).fetchone()
        scores = self.db.execute(
            """
            SELECT COUNT(*) n, AVG(ai_brier) ai, AVG(market_brier) mkt,
                   SUM(CASE WHEN ai_brier < market_brier THEN 1 ELSE 0 END) wins
            FROM weather_edge_decisions
            WHERE strategy_name = ? AND ai_brier IS NOT NULL
            """,
            (self.strategy_name,),
        ).fetchone()
        return {
            "cashUsdc": round(account["cash"], 2),
            "openNotionalUsdc": round(self.open_notional(), 2),
            "settledOrders": int(pnl["n"] or 0),
            "realizedPnlUsdc": round(float(pnl["pnl"] or 0.0), 2),
            "stakedUsdc": round(float(pnl["staked"] or 0.0), 2),
            "scoredDecisions": int(scores["n"] or 0),
            "avgAiBrier": round(float(scores["ai"]), 4) if scores["ai"] is not None else None,
            "avgMarketBrier": round(float(scores["mkt"]), 4) if scores["mkt"] is not None else None,
            "decisionsBeatingMarket": int(scores["wins"] or 0),
        }

    def run_once(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        scored = self.score_settled(now)
        feedback = self.retry_pending_feedback(now)
        reviewed = []
        if self._analysis_open(now):
            for event in self.open_events(now):
                try:
                    buckets = self.market_buckets(str(event["event_id"]), now)
                    if len(buckets) < 2:
                        reviewed.append({"eventId": str(event["event_id"]), "status": "skipped", "reason": "no fresh market snapshot"})
                        continue
                    due, state, reason = self.review_due(event, buckets, now)
                    if not due:
                        reviewed.append({"eventId": str(event["event_id"]), "status": "not_due", "reason": reason})
                        continue
                    reviewed.append(self.review_event(event, now, buckets, state, reason))
                except Exception as exc:
                    logging.error("edge review failed event=%s: %s", event["event_id"], exc)
                    reviewed.append({"eventId": str(event["event_id"]), "status": "error", "error": str(exc)})
        else:
            logging.info("outside the analysis window; scoring only")
        return {"scored": scored, "feedback": feedback, "reviewed": reviewed, "summary": self.summary()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one review pass and exit")
    parser.add_argument("--loop", action="store_true", help="run review and settlement passes continuously")
    parser.add_argument("--score-only", action="store_true", help="score settled events without trading")
    parser.add_argument("--summary", action="store_true", help="print paper performance and exit")
    parser.add_argument("--calibration", metavar="CITY", help="print resolved market reliability by price bucket")
    parser.add_argument("--dry-run", action="store_true", help="build the prompt and print it without calling the AI")
    args = parser.parse_args()

    config = load_config()
    configure_logging(config)
    lock_path = ROOT / "data" / "weather_edge_agent.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another weather_edge_agent run holds the lock")
            return 1
        agent = EdgeAgent(config)
        now = utc_now()
        if args.summary:
            print(json.dumps(agent.summary(), ensure_ascii=False, indent=2))
            agent.close()
            return 0
        if args.calibration:
            print(json.dumps(agent.market_calibration(args.calibration), ensure_ascii=False, indent=2))
            agent.close()
            return 0
        if args.score_only:
            print(json.dumps(agent.score_settled(now), ensure_ascii=False, indent=2))
            agent.close()
            return 0
        if args.dry_run:
            events = agent.open_events(now)
            if not events:
                print("no open events for the configured cities")
                return 0
            event = events[0]
            buckets = agent.market_buckets(str(event["event_id"]), now)
            if len(buckets) < 2:
                print(f"no fresh market snapshot for {event['city']} {event['target_date']}")
                return 1
            payload = agent.build_input(event, buckets, now)
            prompt = agent._prompt(payload)
            print(prompt)
            print(f"\n--- prompt_chars={len(prompt)} buckets={len(buckets)} ---")
            return 0
        if not args.loop:
            print(json.dumps(agent.run_once(now), ensure_ascii=False, indent=2, default=str))
            agent.close()
            return 0
        stop = False
        def request_stop(_signum: int, _frame: Any) -> None:
            nonlocal stop
            stop = True
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        interval = max(60, int(agent.config.get("reviewIntervalMinutes", 30)) * 60)
        while not stop:
            try:
                result = agent.run_once(utc_now())
                logging.info("edge run: %s", json.dumps(result, ensure_ascii=False, default=str))
            except Exception:
                logging.exception("edge run failed")
            for _ in range(interval):
                if stop:
                    break
                time.sleep(1)
        agent.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
