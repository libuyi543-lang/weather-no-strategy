#!/usr/bin/env python3
"""Minimal Hermes-backed paper engine for three-bucket YES and single-bucket NO."""

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
    configure_logging,
    executable_vwap,
    json_value,
    parse_ts,
)
from weather_data_store import WeatherDataStore, as_float, iso_utc, utc_now
from weather_market_alignment import RidgeV2Adapter
from weather_market_monitor import parsed_metar_fields
from weather_context_markdown import timeline_context_markdown, write_daily_context_markdown
from weather_decision_gate import WeatherDecisionGate
from weather_evidence_timeline import WeatherEvidenceTimeline
from weather_problem_solver import WeatherNoProblemSolver


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "weather_dual_strategy_config.json"
SCHEMA_PATH = ROOT / "weather_dual_strategy.schema.json"
OUTCOME_REVIEW_SCHEMA_PATH = ROOT / "weather_outcome_review.schema.json"
UTC = timezone.utc
NO_THESES = {"NO_OVERSHOOT", "NO_CEILING"}
SKEWS = {"COLD", "NEUTRAL", "HOT"}


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DualStrategyEngine(WeatherAIAgent):
    """Small paper engine that reuses read helpers without legacy startup work."""

    def __init__(self, config: dict[str, Any]):
        if not config.get("paperOnly", True):
            raise RuntimeError("live execution is not implemented; paperOnly must remain true")
        WeatherDataStore.__init__(self, config)
        self.ridge_v2 = RidgeV2Adapter(ROOT, self.db_path, config)
        self.problem_solver = WeatherNoProblemSolver(config)
        self.decision_gate = WeatherDecisionGate()
        self._review_retry_after: dict[str, datetime] = {}
        self._init_runtime_schema()
        self._init_dual_schema()

    def _init_runtime_schema(self) -> None:
        """Create only shared state required by scheduling and the AI circuit."""
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS weather_ai_agent_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS weather_ai_agent_scheduled_reviews (
                strategy_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                review_slot_utc TEXT NOT NULL,
                review_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_after_utc TEXT,
                last_error TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                PRIMARY KEY(strategy_name,event_id,review_slot_utc)
            );
            """
        )
        self.db.commit()

    def _init_dual_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS weather_dual_reviews (
                review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                target_date TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_time_utc TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                reviewed_at_utc TEXT NOT NULL,
                status TEXT NOT NULL,
                input_json TEXT NOT NULL,
                ai_response_json TEXT,
                error TEXT,
                UNIQUE(strategy_name,event_id,state_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_weather_dual_reviews_event
                ON weather_dual_reviews(strategy_name,event_id,reviewed_at_utc);

            CREATE TABLE IF NOT EXISTS weather_dual_actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id INTEGER NOT NULL,
                strategy_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                strategy_type TEXT NOT NULL,
                market_id TEXT,
                outcome_range TEXT,
                outcome_side TEXT,
                thesis TEXT,
                skew TEXT,
                sizing_tier TEXT,
                requested_shares REAL,
                executed_shares REAL,
                execution_price REAL,
                notional_usdc REAL,
                fee_usdc REAL NOT NULL DEFAULT 0,
                executed_action TEXT NOT NULL,
                rejection_reason TEXT,
                state_hash TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                FOREIGN KEY(review_id) REFERENCES weather_dual_reviews(review_id)
            );

            CREATE TABLE IF NOT EXISTS weather_dual_positions (
                strategy_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_range TEXT NOT NULL,
                outcome_side TEXT NOT NULL,
                strategy_type TEXT NOT NULL,
                thesis TEXT,
                shares REAL NOT NULL,
                cost_basis_usdc REAL NOT NULL,
                entry_count INTEGER NOT NULL,
                last_state_hash TEXT NOT NULL,
                opened_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                settled_at_utc TEXT,
                realized_pnl_usdc REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(strategy_name,event_id,market_id,outcome_side)
            );

            CREATE TABLE IF NOT EXISTS weather_dual_fills (
                fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id INTEGER NOT NULL,
                strategy_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_side TEXT NOT NULL,
                strategy_type TEXT NOT NULL,
                shares REAL NOT NULL,
                price REAL NOT NULL,
                notional_usdc REAL NOT NULL,
                fee_usdc REAL NOT NULL DEFAULT 0,
                fill_type TEXT NOT NULL,
                realized_pnl_usdc REAL NOT NULL DEFAULT 0,
                filled_at_utc TEXT NOT NULL,
                FOREIGN KEY(action_id) REFERENCES weather_dual_actions(action_id)
            );
            CREATE TABLE IF NOT EXISTS weather_dual_candidate_audits (
                candidate_audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id INTEGER,
                review_id INTEGER NOT NULL,
                strategy_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                target_date TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_range TEXT,
                outcome_side TEXT NOT NULL,
                thesis TEXT,
                sizing_tier TEXT,
                decision_time_utc TEXT,
                executable_price REAL,
                executable_shares REAL,
                requested_shares REAL,
                probability_low REAL,
                probability_high REAL,
                all_in_cost_per_share REAL,
                raw_edge REAL,
                required_edge REAL,
                evidence_support_count INTEGER,
                evidence_type_count INTEGER,
                candidate_status TEXT NOT NULL,
                rejection_reason TEXT,
                final_outcome TEXT,
                signal_correct INTEGER,
                hypothetical_pnl_usdc REAL,
                resolved_at_utc TEXT,
                created_at_utc TEXT NOT NULL,
                FOREIGN KEY(action_id) REFERENCES weather_dual_actions(action_id),
                FOREIGN KEY(review_id) REFERENCES weather_dual_reviews(review_id)
            );
            CREATE INDEX IF NOT EXISTS idx_weather_dual_candidate_audits_event
                ON weather_dual_candidate_audits(strategy_name,event_id,created_at_utc);
            CREATE TABLE IF NOT EXISTS weather_dual_outcome_reviews (
                outcome_review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                target_date TEXT NOT NULL,
                net_pnl_usdc REAL NOT NULL,
                review_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                UNIQUE(strategy_name,event_id)
            );
            """
        )
        self._ensure_column(
            "weather_dual_actions", "fee_usdc", "REAL NOT NULL DEFAULT 0",
        )
        self._ensure_column(
            "weather_dual_fills", "fee_usdc", "REAL NOT NULL DEFAULT 0",
        )
        self.db.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _record_candidate_audit(
        self, *, action_id: int, review_id: int, context: dict[str, Any], market: dict[str, Any],
        side: str, review: dict[str, Any], decision_time_utc: str | None,
        price: float | None, available: float | None, requested: float,
        all_in_cost: float | None, raw_edge: float | None, required_edge: float | None,
        status: str, rejection: str | None,
    ) -> None:
        """Persist every AI-selected candidate, including candidates rejected by Python."""
        self.db.execute(
            """INSERT INTO weather_dual_candidate_audits(
                action_id,review_id,strategy_name,event_id,city,target_date,market_id,outcome_range,
                outcome_side,thesis,sizing_tier,decision_time_utc,executable_price,executable_shares,
                requested_shares,probability_low,probability_high,all_in_cost_per_share,raw_edge,
                required_edge,evidence_support_count,evidence_type_count,candidate_status,rejection_reason,
                created_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                action_id, review_id, self.strategy_name, context["event"]["event_id"],
                context["event"]["city"], context["event"]["target_date"], market["marketId"],
                market.get("outcomeRange"), side, review.get("thesis"), review.get("sizingTier"),
                decision_time_utc, price, available, requested,
                as_float(review.get("conservativePathProbabilityLow")),
                as_float(review.get("conservativePathProbabilityHigh")), all_in_cost, raw_edge,
                required_edge, len(review.get("supportingEvidence") or []),
                len(set(review.get("newInformationTypes") or [])), status, rejection, iso_utc(),
            ),
        )

    def _backfill_candidate_audits(
        self, event_id: str, market_id: str, winning_outcome: str, resolved_at_utc: str,
    ) -> None:
        """Attach settlement truth to all candidate attempts for one market."""
        self.db.execute(
            """UPDATE weather_dual_candidate_audits
               SET final_outcome=?, signal_correct=(CASE WHEN UPPER(?) = UPPER(outcome_side) THEN 1 ELSE 0 END),
                   hypothetical_pnl_usdc=(
                       CASE WHEN UPPER(?) = UPPER(outcome_side)
                            THEN executable_shares * (1.0 - executable_price) -
                                 (executable_shares * ? * executable_price * (1.0 - executable_price))
                            ELSE -executable_shares * executable_price -
                                 (executable_shares * ? * executable_price * (1.0 - executable_price))
                       END
                   ), resolved_at_utc=?
               WHERE strategy_name=? AND event_id=? AND market_id=? AND final_outcome IS NULL""",
            (
                winning_outcome, winning_outcome, winning_outcome,
                float(self.config.get("feeRate", 0)), float(self.config.get("feeRate", 0)),
                resolved_at_utc, self.strategy_name, event_id, market_id,
            ),
        )

    def taker_fee_usdc(self, shares: float, price: float) -> float:
        rate = max(0.0, float(self.config.get("feeRate", 0)))
        bounded_price = min(1.0, max(0.0, float(price)))
        return max(0.0, float(shares)) * rate * bounded_price * (1.0 - bounded_price)

    def all_in_cost_per_share(self, price: float) -> float:
        return float(price) + self.taker_fee_usdc(1.0, price)

    def account_state(self) -> dict[str, float]:
        initial = float(self.config.get("initialCashUsdc", 20))
        spent = float(self.db.execute(
            "SELECT COALESCE(SUM(notional_usdc+fee_usdc),0) FROM weather_dual_fills "
            "WHERE strategy_name=? AND fill_type='paper_buy'", (self.strategy_name,),
        ).fetchone()[0])
        returned = float(self.db.execute(
            "SELECT COALESCE(SUM(notional_usdc),0) FROM weather_dual_fills "
            "WHERE strategy_name=? AND fill_type='settlement'", (self.strategy_name,),
        ).fetchone()[0])
        open_cost = float(self.db.execute(
            "SELECT COALESCE(SUM(cost_basis_usdc),0) FROM weather_dual_positions "
            "WHERE strategy_name=? AND shares>0", (self.strategy_name,),
        ).fetchone()[0])
        cash = initial - spent + returned
        reserve = float(self.config.get("minCashReserveUsdc", 5))
        return {
            "initialCashUsdc": initial,
            "availableCashUsdc": cash,
            "spendableCashUsdc": max(0.0, cash - reserve),
            "openCostBasisUsdc": open_cost,
            "realizedPnlUsdc": returned - (spent - open_cost),
        }

    def positions_for_event(self, event_id: str, markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM weather_dual_positions WHERE strategy_name=? AND event_id=? AND shares>0",
            (self.strategy_name, event_id),
        ).fetchall()]

    def market_states(self, event: dict[str, Any], as_of_utc: datetime) -> list[dict[str, Any]]:
        """Load one current and one prior quote per market, without history arrays."""
        as_of = iso_utc(as_of_utc)
        current = self.db.execute(
            "SELECT MAX(slot_utc) FROM market_snapshots WHERE event_id=? AND slot_utc<=?",
            (event["event_id"], as_of),
        ).fetchone()
        slot = current[0] if current else None
        if not slot:
            return []
        prior = self.db.execute(
            "SELECT MAX(slot_utc) FROM market_snapshots WHERE event_id=? AND slot_utc<?",
            (event["event_id"], slot),
        ).fetchone()
        prior_slot = prior[0] if prior else None
        rows = self.db.execute(
            """
            SELECT m.market_id,m.outcome_range,m.bucket_low,m.bucket_high,m.bucket_unit,
                   ms.slot_utc,ms.yes_best_bid,ms.yes_best_ask,ms.no_best_bid,ms.no_best_ask,
                   ms.yes_book_json,ms.no_book_json,ms.market_volume_24h,ms.market_liquidity,
                   old.yes_best_ask AS previous_yes_best_ask,
                   old.no_best_ask AS previous_no_best_ask
            FROM markets m
            JOIN market_snapshots ms ON ms.market_id=m.market_id AND ms.slot_utc=?
            LEFT JOIN market_snapshots old ON old.market_id=m.market_id AND old.slot_utc=?
            WHERE m.event_id=?
            ORDER BY COALESCE(m.bucket_low,-999),COALESCE(m.bucket_high,999)
            """,
            (slot, prior_slot, event["event_id"]),
        ).fetchall()
        base_shares = float(self.config.get("singleNoBaseShares", 5))
        output: list[dict[str, Any]] = []
        for row in rows:
            yes_book = json_value(row["yes_book_json"], {})
            no_book = json_value(row["no_book_json"], {})
            yes_price, yes_available = executable_vwap(yes_book, base_shares, "asks")
            no_price, no_available = executable_vwap(no_book, base_shares, "asks")
            output.append({
                "marketId": row["market_id"], "outcomeRange": row["outcome_range"],
                "bucketLow": row["bucket_low"], "bucketHigh": row["bucket_high"],
                "bucketUnit": row["bucket_unit"], "snapshotUtc": row["slot_utc"],
                "yesBestBid": row["yes_best_bid"], "yesBestAsk": row["yes_best_ask"],
                "noBestBid": row["no_best_bid"], "noBestAsk": row["no_best_ask"],
                "yesExecutableBuyPrice5": yes_price, "yesBuyAvailableShares": yes_available,
                "noExecutableBuyPrice5": no_price, "noBuyAvailableShares": no_available,
                "previousYesBestAsk": row["previous_yes_best_ask"],
                "previousNoBestAsk": row["previous_no_best_ask"],
                "volume24h": row["market_volume_24h"], "liquidity": row["market_liquidity"],
                "yesBook": yes_book, "noBook": no_book,
            })
        return output

    def _due_metar_events(self, now: datetime) -> list[dict[str, Any]]:
        """Return today's latest METAR per city without legacy cycle tables."""
        allowed = self.allowed_cities()
        if not allowed:
            return []
        rows = self.db.execute(
            """
            SELECT e.event_id,e.city,e.target_date,e.station_id,e.station_name,
                   e.resolution_source,e.rules,e.end_date_utc,
                   s.latitude,s.longitude,s.timezone
            FROM events e JOIN stations s ON s.station_id=e.station_id
            WHERE e.resolved_at_utc IS NULL AND s.timezone IS NOT NULL
              AND e.station_id IS NOT NULL
            ORDER BY e.target_date,e.city,e.last_seen_utc DESC
            """
        ).fetchall()
        output: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        start = int(self.config.get("activeLocalStartMinutes", 7 * 60))
        end = int(self.config.get("activeLocalEndHour", 20)) * 60
        for row in rows:
            city_key = str(row["city"] or "").strip().casefold()
            key = (city_key, str(row["target_date"]))
            if city_key not in allowed or key in seen:
                continue
            try:
                local_now = now.astimezone(ZoneInfo(row["timezone"]))
            except (ValueError, KeyError):
                continue
            local_minutes = local_now.hour * 60 + local_now.minute
            if local_now.date().isoformat() != row["target_date"] or not start <= local_minutes < end:
                continue
            fast_timeline = self._fast_metar_timeline(dict(row), now)
            metar = fast_timeline[-1] if fast_timeline else self._latest_metar_for_event(dict(row), now)
            if not metar:
                continue
            observed_at = parse_ts(metar.get("observation_time_utc"))
            if observed_at is None or observed_at.astimezone(ZoneInfo(row["timezone"])).date().isoformat() != row["target_date"]:
                continue
            output.append({**dict(row), "metar_trigger": metar})
            seen.add(key)
        return output

    @staticmethod
    def _trim_hourly(payload: dict[str, Any] | None, limit: int = 6) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return payload
        return {
            key: (value[:limit] if key in {"hourly", "futureHourlyProcess"} and isinstance(value, list) else value)
            for key, value in payload.items()
        }

    def _fast_metar_timeline(
        self, event: dict[str, Any], before_utc: datetime,
    ) -> list[dict[str, Any]]:
        """Return reports known by the decision time, including rolling backfill rows."""
        if not self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fast_metar_reports'"
        ).fetchone():
            return []
        cutoff = iso_utc(before_utc - timedelta(hours=30))
        rows = self.db.execute(
            """SELECT observation_time_utc,first_fetched_at_utc,payload_json
               FROM fast_metar_reports
               WHERE station_id=? AND observation_time_utc>=? AND observation_time_utc<=?
                 AND first_fetched_at_utc<=?
               ORDER BY observation_time_utc""",
            (event["station_id"], cutoff, iso_utc(before_utc), iso_utc(before_utc)),
        ).fetchall()
        reports: list[dict[str, Any]] = []
        daily_max: float | None = None
        timezone_name = event.get("timezone") or "UTC"
        target_date = str(event.get("target_date") or "")
        for row in rows:
            payload = json_value(row["payload_json"], {})
            observed = parse_ts(row["observation_time_utc"])
            if not isinstance(payload, dict) or observed is None:
                continue
            try:
                is_target_day = observed.astimezone(ZoneInfo(timezone_name)).date().isoformat() == target_date
            except (KeyError, ValueError):
                is_target_day = True
            temperature = as_float(payload.get("temp"))
            if not is_target_day:
                continue
            if temperature is not None:
                daily_max = temperature if daily_max is None else max(daily_max, temperature)
            parsed = parsed_metar_fields(payload.get("rawOb"))
            reports.append({
                "slot_utc": row["first_fetched_at_utc"],
                "observation_time_utc": row["observation_time_utc"],
                "fetched_at_utc": row["first_fetched_at_utc"],
                "temperature_c": temperature, "dewpoint_c": as_float(payload.get("dewp")),
                "wind_direction_deg": as_float(payload.get("wdir")),
                "wind_speed": as_float(payload.get("wspd")), "wind_speed_unit": "kt",
                "wind_gust": as_float(payload.get("wgst", parsed.get("wind_gust"))),
                "visibility_m": as_float(payload.get("visib")) * 1609.344 if as_float(payload.get("visib")) is not None else parsed.get("visibility_m"),
                "pressure_hpa": as_float(payload.get("altim", parsed.get("pressure_hpa"))),
                "flight_category": payload.get("fltCat"),
                "sky_conditions_json": json.dumps(payload.get("clouds") or [], ensure_ascii=False),
                "raw_metar": payload.get("rawOb"), "metar_type": payload.get("metarType"),
                "metar_parser_status": parsed.get("parser_status"),
                "weather_code": payload.get("wxString") or payload.get("rawOb"),
                "observed_daily_max_c": daily_max,
            })
        return reports

    def build_context(self, event: dict[str, Any]) -> dict[str, Any]:
        """Build only the state used by the two active strategies."""
        decision_trigger = event.get("decision_trigger") or {}
        scheduled = decision_trigger.get("type") in {"scheduled_review", "position_review"}
        trigger = event["metar_trigger"]
        if scheduled:
            as_of = parse_ts(decision_trigger.get("analysisAsOfUtc")) or utc_now()
            trigger = self._latest_metar_for_event(event, as_of) or trigger
        else:
            as_of = parse_ts(
                decision_trigger.get("analysisAsOfUtc")
                or decision_trigger.get("sourceSlotUtc")
                or trigger["slot_utc"]
            ) or utc_now()
        markets = self.market_states(event, as_of)
        fast_timeline = self._fast_metar_timeline(event, as_of)
        if fast_timeline and parse_ts(fast_timeline[-1]["observation_time_utc"]) >= parse_ts(trigger["observation_time_utc"]):
            trigger = fast_timeline[-1]
        previous = fast_timeline[-2] if len(fast_timeline) > 1 else self.previous_metar(event["station_id"], trigger["observation_time_utc"])
        coverage_times = [parse_ts(row["observation_time_utc"]) for row in fast_timeline]
        coverage_times = [value for value in coverage_times if value is not None]
        gaps = [(right - left).total_seconds() / 60 for left, right in zip(coverage_times, coverage_times[1:])]
        metar_coverage = {
            "reportCount": len(coverage_times),
            "firstObservationTimeUtc": iso_utc(coverage_times[0]) if coverage_times else None,
            "lastObservationTimeUtc": iso_utc(coverage_times[-1]) if coverage_times else None,
            "maxGapMinutes": max(gaps) if gaps else None,
            "missingDataRisk": not coverage_times or bool(gaps and max(gaps) > float(self.config.get("singleNoMaxMetarGapMinutes", 75))),
        }
        weather = self.local_weather_payload(event, as_of)
        weather["meteoblue"] = self._trim_hourly(weather.get("meteoblue"))
        models = self.model_update_state(event, as_of)
        for name in ("meteoblue", "ecmwf"):
            models[name] = self._trim_hourly(models.get(name))
        process = self.weather_process_state(event, as_of)
        ridge = self.ridge_v2.snapshot(
            event, as_of,
            str((decision_trigger.get("weatherState") or {}).get("stateVersion") or as_of),
            process,
        )
        market_slot = parse_ts(markets[0]["snapshotUtc"]) if markets else None
        model_slot = parse_ts((models.get("meteoblue") or {}).get("sampleSlotUtc"))
        return {
            "event": {key: event.get(key) for key in (
                "event_id", "city", "target_date", "station_id", "station_name", "timezone",
                "resolution_source", "rules", "end_date_utc",
            )},
            "trigger": {
                "type": decision_trigger.get("type") or "new_metar",
                "slotUtc": iso_utc(as_of),
                "observationTimeUtc": trigger["observation_time_utc"],
                "decisionTriggerTimeUtc": decision_trigger.get("sourceSlotUtc") or trigger["slot_utc"],
                "analysisAsOfUtc": iso_utc(as_of),
                "triggerTypes": decision_trigger.get("triggerTypes") or ["primary_metar"],
            },
            "metar": {
                "current": trigger,
                "previous": previous,
            },
            "metarCoverage": metar_coverage,
            "weather": weather,
            "modelUpdates": models,
            "weatherProcess": process,
            "marketConsensus": self.market_consensus(markets),
            "ridgeV2": ridge,
            "markets": markets,
            "positions": self.positions_for_event(event["event_id"], markets),
            "account": self.account_state(),
            "dataFreshness": {
                "marketAgeMinutes": (as_of - market_slot).total_seconds() / 60 if market_slot else None,
                "meteoblueAgeMinutes": (as_of - model_slot).total_seconds() / 60 if model_slot else None,
            },
        }

    @staticmethod
    def three_bucket_weights(skew: str, config: dict[str, Any]) -> tuple[float, float, float]:
        if skew not in SKEWS:
            raise ValueError(f"unsupported three-bucket skew: {skew}")
        values = (config.get("threeBucketShares") or {}).get(skew)
        if not isinstance(values, list) or len(values) != 3:
            raise ValueError(f"threeBucketShares.{skew} must contain exactly three values")
        weights = tuple(float(value) for value in values)
        if any(value <= 0 for value in weights) or abs(sum(weights) - 30) > 1e-9:
            raise ValueError("three-bucket shares must be positive and total 30")
        if weights[1] <= max(weights[0], weights[2]):
            raise ValueError("three-bucket center must have the largest allocation")
        return weights

    @staticmethod
    def _bucket_value(market: dict[str, Any]) -> int | None:
        low, high = as_float(market.get("bucketLow")), as_float(market.get("bucketHigh"))
        if low is None or high is None or abs(low - high) > 1e-9:
            return None
        return int(round(low))

    @staticmethod
    def _ridge_bucket_probabilities(context: dict[str, Any]) -> dict[int, float]:
        return {
            int(row["bucketC"]): float(row["probability"])
            for row in (context.get("ridgeV2") or {}).get("bucketProbabilities") or []
            if as_float(row.get("bucketC")) is not None and as_float(row.get("probability")) is not None
        }

    def _ridge_center_bucket(self, context: dict[str, Any]) -> int | None:
        probabilities = self._ridge_bucket_probabilities(context)
        if not probabilities:
            return None
        return max(probabilities, key=lambda bucket: (probabilities[bucket], bucket))

    def _three_bucket_candidate(self, context: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        if not self.config.get("threeBucketEnabled", True):
            return None
        local = now.astimezone(ZoneInfo(context["event"]["timezone"]))
        minute = local.hour * 60 + local.minute
        review_start = int(self.config.get("threeBucketReviewStartLocalMinutes", 630))
        hard_stop = int(self.config.get("threeBucketHardStopLocalMinutes", 720))
        if minute < review_start or minute >= hard_stop:
            return None
        markets_by_bucket = {
            bucket: market for market in context.get("markets") or []
            if (bucket := self._bucket_value(market)) is not None
        }
        ridge_probs = self._ridge_bucket_probabilities(context)
        source = "ridge_v3_research"
        probabilities = ridge_probs
        if not probabilities:
            probabilities = {
                int(row["bucketLow"]): float(row["normalizedProbability"])
                for row in (context.get("marketConsensus") or {}).get("distribution") or []
                if as_float(row.get("bucketLow")) is not None and as_float(row.get("normalizedProbability")) is not None
            }
            source = "market_consensus_fallback"
        eligible = [
            (probability, bucket) for bucket, probability in probabilities.items()
            if all(value in markets_by_bucket for value in (bucket - 1, bucket, bucket + 1))
        ]
        if not eligible:
            return None
        eligible.sort(reverse=True)
        center_probability, center = eligible[0]
        second_probability = eligible[1][0] if len(eligible) > 1 else 0.0
        lead = center_probability - second_probability
        legs = [markets_by_bucket[value] for value in (center - 1, center, center + 1)]
        return {
            "centerBucketC": center,
            "centerSource": source,
            "centerProbability": center_probability,
            "centerLead": lead,
            "centerStable": lead >= float(self.config.get("threeBucketMinimumCenterLead", 0.03)),
            "entryAllowed": minute <= int(self.config.get("threeBucketEntryEndLocalMinutes", 675)),
            "localMinute": minute,
            "legs": [{
                "marketId": market["marketId"],
                "outcomeRange": market["outcomeRange"],
                "bucketC": self._bucket_value(market),
                "yesExecutableBuyPrice5": market.get("yesExecutableBuyPrice5"),
                "yesBuyAvailableShares": market.get("yesBuyAvailableShares"),
            } for market in legs],
        }

    def _trade_window_open(self, context: dict[str, Any], now: datetime) -> bool:
        event = context.get("event") or {}
        try:
            local = now.astimezone(ZoneInfo(str(event["timezone"])))
        except (KeyError, TypeError, ValueError):
            return False
        if local.date().isoformat() != str(event.get("target_date") or ""):
            return False
        minute = local.hour * 60 + local.minute
        start = int(self.config.get("tradeLocalStartMinutes", 7 * 60))
        end = int(self.config.get("tradeLocalEndMinutes", 19 * 60))
        return start <= minute < end

    def _single_no_portfolio_cap(self, context: dict[str, Any], now: datetime | None) -> float:
        """Keep a configurable early-session reserve for later candidates."""
        cap = float(self.config.get("maxOpenNotionalTotal", 20))
        reserve = max(0.0, float(self.config.get("singleNoEarlyPortfolioReserveUsdc", 0)))
        if reserve <= 0 or now is None:
            return cap
        try:
            local = now.astimezone(ZoneInfo(str((context.get("event") or {})["timezone"])))
        except (KeyError, TypeError, ValueError):
            return cap
        end_minute = int(self.config.get("singleNoEarlyPortfolioReserveEndLocalMinutes", 600))
        minute = local.hour * 60 + local.minute
        if minute < end_minute:
            return max(0.0, cap - reserve)
        return cap

    def _decision_phase(self, context: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        """Expose a small, auditable time phase to the AI and execution guard."""
        event = context.get("event") or {}
        candidate = now or parse_ts(((context.get("trigger") or {}).get("analysisAsOfUtc")))
        if candidate is None:
            candidate = parse_ts(((context.get("metar") or {}).get("current") or {}).get("observation_time_utc"))
        if candidate is None:
            return {"name": "UNKNOWN", "localMinute": None, "ceilingAllowed": True}
        try:
            local = candidate.astimezone(ZoneInfo(str(event["timezone"])))
        except (KeyError, TypeError, ValueError):
            return {"name": "UNKNOWN", "localMinute": None, "ceilingAllowed": True}
        minute = local.hour * 60 + local.minute
        if minute < 10 * 60:
            name, ceiling_allowed = "EARLY_WARMING", False
        elif minute < 12 * 60:
            name, ceiling_allowed = "TRANSITION", True
        else:
            name, ceiling_allowed = "LATE_SESSION", True
        return {
            "name": name,
            "localMinute": minute,
            "localTime": local.strftime("%H:%M"),
            "ceilingAllowed": ceiling_allowed,
        }

    def _single_no_universe(
        self, context: dict[str, Any], now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if not self.config.get("singleNoEnabled", True):
            return []
        if now is not None and not self._trade_window_open(context, now):
            return []
        probabilities = self._ridge_bucket_probabilities(context)
        ridge_center = self._ridge_center_bucket(context)
        ridge = context.get("ridgeV2") or {}
        ridge_path_buckets = {
            "RIDGE_PRIMARY": as_float(ridge.get("primaryBucketC")),
            "RIDGE_CAPPING": as_float(ridge.get("cappingBucketC")),
            "RIDGE_WARM_TAIL": as_float(ridge.get("warmTailBucketC")),
        }
        coverage = context.get("metarCoverage") or {}
        phase = self._decision_phase(context, now)
        current_metar = (context.get("metar") or {}).get("current") or {}
        current_temp = as_float(current_metar.get("temperature_c"))
        observed_daily_max = as_float(current_metar.get("observed_daily_max_c"))
        observed_values = [value for value in (current_temp, observed_daily_max) if value is not None]
        observed_max = max(observed_values) if observed_values else None
        universe = []
        for market in context.get("markets") or []:
            bucket = self._bucket_value(market)
            price = as_float(market.get("noExecutableBuyPrice5"))
            if bucket is None or price is None:
                continue
            ridge_bucket_probability = probabilities.get(bucket)
            ridge_roles = [
                role for role, value in ridge_path_buckets.items()
                if value is not None and int(round(value)) == bucket
            ]
            if bucket == ridge_center and "RIDGE_PRIMARY" not in ridge_roles:
                ridge_roles.append("RIDGE_CENTER")
            universe.append({
                "marketId": market["marketId"], "outcomeRange": market["outcomeRange"],
                "bucketC": bucket, "noExecutableBuyPrice5": price,
                "estimatedTakerFeePerShare5": self.taker_fee_usdc(1.0, price),
                "noAllInCostPerShare5": self.all_in_cost_per_share(price),
                "noBuyAvailableShares": market.get("noBuyAvailableShares"),
                "ridgeBucketProbability": ridge_bucket_probability,
                "ridgeRoles": ridge_roles,
                "decisionPhase": phase["name"],
                "decisionLocalMinute": phase["localMinute"],
                "currentObservedTempC": current_temp,
                "observedDailyMaxC": observed_daily_max,
                "targetAlreadyReached": observed_max is not None and observed_max >= bucket,
                "targetDistanceFromObservedMax": (
                    bucket - observed_max if observed_max is not None else None
                ),
                "metarGapMinutes": coverage.get("maxGapMinutes"),
                "metarGapRisk": bool(coverage.get("missingDataRisk")),
                "strategyQuestion": (
                    f"只判断最终最高温是否会高于{bucket}°C，或是否会低于{bucket}°C；"
                    "不需要预测最终落入哪个其他桶"
                ),
            })
        universe.sort(key=lambda row: row["bucketC"])
        return universe

    @staticmethod
    def _state_payload(context: dict[str, Any], three: dict[str, Any] | None, no_universe: list[dict[str, Any]]) -> dict[str, Any]:
        metar = (context.get("metar") or {}).get("current") or {}
        models = context.get("modelUpdates") or {}
        process = context.get("weatherProcess") or {}
        markets = context.get("markets") or []
        return {
            "metar": [metar.get("observation_time_utc"), metar.get("temperature_c"), metar.get("observed_daily_max_c")],
            "models": [
                (models.get("meteoblue") or {}).get("modelVersion"),
                (models.get("ecmwf") or {}).get("modelVersion"),
            ],
            "process": [process.get("snapshotSlotUtc"), process.get("detectedProcesses")],
            "markets": [[row.get("marketId"), row.get("snapshotUtc"), row.get("yesBestAsk"), row.get("noBestAsk")] for row in markets],
            "three": three,
            "noUniverse": no_universe,
        }

    def build_review_input(self, context: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        analysis_at = (
            parse_ts((context.get("trigger") or {}).get("analysisAsOfUtc"))
            or parse_ts((context.get("trigger") or {}).get("slotUtc"))
            or now or utc_now()
        )
        at = now or analysis_at
        decision_phase = self._decision_phase(context, at)
        three = self._three_bucket_candidate(context, at)
        no_universe = self._single_no_universe(context, at)
        problem_solver = getattr(self, "problem_solver", None) or WeatherNoProblemSolver(self.config)
        problem_definition = problem_solver.define(
            context, no_universe, at, decision_phase,
        )
        learning_state = self.recent_outcome_learning_state(context["event"]["city"])
        selected_market_ids = {
            row["marketId"] for row in (three or {}).get("legs") or []
        } | {row["marketId"] for row in no_universe}
        compact_markets = []
        for market in context.get("markets") or []:
            if market.get("marketId") not in selected_market_ids:
                continue
            compact_markets.append({key: market.get(key) for key in (
                "marketId", "outcomeRange", "bucketLow", "bucketHigh", "snapshotUtc",
                "yesBestBid", "yesBestAsk", "yesExecutableBuyPrice5", "yesBuyAvailableShares",
                "noBestBid", "noBestAsk", "noExecutableBuyPrice5", "noBuyAvailableShares",
                "previousYesBestAsk", "previousNoBestAsk", "volume24h", "liquidity",
            )})
        compact = {
            "event": {key: (context.get("event") or {}).get(key) for key in (
                "event_id", "city", "target_date", "station_id", "timezone", "resolution_source",
            )},
            "trigger": context.get("trigger") or {},
            "metar": context.get("metar") or {},
            "metarCoverage": context.get("metarCoverage") or {},
            "weather": context.get("weather") or {},
            "modelUpdates": context.get("modelUpdates") or {},
            "weatherProcess": context.get("weatherProcess") or {},
            "marketConsensus": context.get("marketConsensus") or {},
            "ridgeV2": context.get("ridgeV2") or {},
            "markets": compact_markets,
            "positions": context.get("positions") or [],
            "account": context.get("account") or {},
            "dataFreshness": context.get("dataFreshness") or {},
            "decisionPhase": decision_phase,
        }
        context_markdown = None
        context_document_hash = None
        context_path = None
        if self.config.get("contextDocumentEnabled", False):
            document_context = {
                **context,
                "singleNoUniverse": no_universe,
                "threeBucketCandidate": three,
            }
            context_path, full_context_markdown = write_daily_context_markdown(
                ROOT, self.config, self.db, context["event"], document_context, analysis_at,
            )
            context_document_hash = stable_hash(full_context_markdown)
            context_markdown = timeline_context_markdown(full_context_markdown)
            context["contextDocumentPath"] = str(context_path)
        evidence_timeline = WeatherEvidenceTimeline.build(
            context, no_universe, at,
            document_path=str(context_path) if context_path else None,
            document_hash=context_document_hash,
        )
        payload = {
            "eventId": context["event"]["event_id"],
            "city": context["event"]["city"],
            "asOfUtc": iso_utc(at),
            "trigger": context.get("trigger") or {},
            "threeBucketCandidate": three,
            "singleNoUniverse": no_universe,
            "decisionPhase": decision_phase,
            "problemDefinition": problem_definition,
            "learningState": learning_state,
            "evidenceTimeline": evidence_timeline,
            "weatherState": compact,
            "contextDocumentPath": str(context_path) if context_path else None,
            "contextDocumentHash": context_document_hash,
            "contextMarkdown": context_markdown,
        }
        state_payload = self._state_payload(context, three, no_universe)
        state_payload["problemVersion"] = problem_definition["version"]
        state_payload["learningState"] = learning_state
        state_payload["contextDocumentHash"] = context_document_hash
        payload["stateHash"] = stable_hash(state_payload)
        return payload

    def recent_outcome_learning_state(self, city: str, limit: int = 5) -> dict[str, Any]:
        """Return compact, non-binding AI review observations for future problems."""
        try:
            rows = self.db.execute(
                """SELECT event_id,target_date,review_json FROM weather_dual_outcome_reviews
                   WHERE strategy_name=? AND city=? ORDER BY target_date DESC LIMIT ?""",
                (self.strategy_name, city, max(1, int(limit))),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        lessons = []
        for row in rows:
            review = json_value(row["review_json"], {})
            for lesson in review.get("lessonCandidates") or []:
                if lesson.get("level") not in {"OBSERVATION", "LESSON_CANDIDATE"}:
                    continue
                lessons.append({
                    "eventId": row["event_id"],
                    "targetDate": row["target_date"],
                    "level": lesson.get("level"),
                    "statement": lesson.get("statement"),
                    "validationNeeded": lesson.get("validationNeeded"),
                })
        return {
            "binding": False,
            "warning": "这些只是复盘观察或待验证经验，不能作为硬规则或替代当前证据",
            "lessons": lessons[-10:],
        }

    def _prompt(self, review_input: dict[str, Any]) -> str:
        context_markdown = str(review_input.get("contextMarkdown") or "")
        structured_input = {key: value for key, value in review_input.items() if key != "contextMarkdown"}
        return (
            "你是天气市场问题求解器。结构化输入中的problemDefinition是本轮不可改写的问题契约："
            "先理解目标、信息边界、盈利判据和WAIT条件，再分析数据；不要把任务重新定义成预测最终温度。"
            "系统是paper-only，当前只启用单桶NO；你不决定份额、不执行订单。"
            "threeBucketReview必须为null。\n"
            "ONE_CALL_PROBLEM_SOLVING：同一次响应必须完整填写problemSolution。先在hypotheses提出1至3个可证伪的单侧天气"
            "假设，再在hypothesisTests逐个检验；只有SUPPORTED假设才可进入mispricingChecks。随后判断对应市场是尚未计价、"
            "部分计价、已经计价或证据不足，最后selection只能BUY一个目标或WAIT。BUY时selection、所选hypothesis、"
            "mispricingCheck和singleNoReviews唯一候选必须互相一致；WAIT时singleNoReviews必须为空。不要在四个步骤之间"
            "重复叙述同一句话，每一步只完成自己的判断。\n"
            "SINGLE_NO_GENERATION：singleNoUniverse是该城市全部当前有可执行NO报价的精确温度档，Python没有按Ridge、"
            "固定edge或价格上限替你筛选。你必须结合完整天气与价格时间线主动比较所有档位，并在singleNoReviews中只输出你选择的"
            "BUY候选；没有足够机会就返回空数组，不要输出OBSERVE项，也不要为凑数生成候选。你不预测最终赢家桶，也不需要构造完整温度"
            "分布；只判断一个目标桶能否被单侧天气路径排除。候选只允许以下两种且必须选出唯一主论点："
            "NO_CEILING=根据当前点时信息，最终最高温低于目标整数桶的单侧路径本身已足以支撑买NO；"
            "NO_OVERSHOOT=根据当前点时信息，最终最高温高于目标整数桶的单侧路径本身已足以支撑买NO。"
            "conservativePathProbabilityLow/High只能填写所选单侧路径的概率区间：CEILING填写P(最终最高温<目标桶)，OVERSHOOT填写"
            "P(最终最高温>目标桶)。不得把另一侧尾部加进来抬高概率，也不得声称知道最终会落入哪个其他桶。\n"
            "市场反应慢或错误定价不是第三种NO，而是每个候选都必须满足的价格条件。必须填写marketInefficiencyType、"
            "marketInefficiencyAssessment和unpricedInformation，明确指出哪些带时间戳的新天气信息尚未被价格充分消化，或者市场具体"
            "低估了哪条单侧路径。仅说市场错了、价格便宜、与模型不同都不够。每个候选还必须给出支持证据、最强反证、市场可能正确/错误"
            "的原因和可证伪条件。BASE/STRONG只是证据等级建议；Python会用实际NO卖价加天气类taker fee复核净edge、深度、现金、已有"
            "仓位和新信息。\n"
            "Ridge只是研究先验，不是中心桶禁买规则，也不授权交易。ridgeRoles和ridgeBucketProbability用于提醒AI检查现有路径；"
            "ridgeAssessment必须说明Ridge与当前单侧判断一致还是冲突，以及新实况是否真的改变了判断。不得仅因目标桶是或不是Ridge中心"
            "就买卖。结算按全天最高记录；不能把当前METAR未报到某温度当成该温度全天没有发生。必须结合实时升温率、剩余加热时间、"
            "太阳辐射、云雨、风场、模型修订与历史误差评估所选单侧路径。metarGapRisk属于数据不完整，必须下调置信度且禁止建议STRONG。"
            "优先使用METAR/SPECI轨迹、已观测日高、雷达/云/辐射/风场过程，再参考Meteoblue、ECMWF、集合和Ridge。"
            "市场是信息先验，不是事实；不得仅凭模型中值差异或价格便宜交易。不要声称成交。\n"
            "先判断decisionPhase，再选择目标桶。EARLY_WARMING（当地10:00前）优先寻找NO_OVERSHOOT；"
            "此阶段不要把短暂降温、单次云量变化或模型下调当作已确认的NO_CEILING。10:00后才可考虑NO_CEILING，"
            "但仍须有持续、多来源的封顶证据，不能因为时间到了就自动成立。每个复核最多选择一个目标桶，"
            "把相邻桶视为同一天气假设的竞争目标；reason必须说明为什么选这个桶、为什么相邻桶不如它。"
            "若无法选出唯一优势桶，返回空数组。\n"
            "下面的Markdown是完整城市文档中的历史时间线；当前模型、Ridge和可交易全集已在结构化输入中提供，"
            "不要重复索取。必须优先按时间顺序分析变化，不得使用asOfUtc之后的信息。\n\n"
            "结构化候选输入：\n"
            + json.dumps(structured_input, ensure_ascii=False, separators=(",", ":"), default=str)
            + "\n\n=== DAILY CITY CONTEXT MARKDOWN BEGIN ===\n"
            + context_markdown
            + "\n=== DAILY CITY CONTEXT MARKDOWN END ==="
        )

    def call_review_ai(self, review_input: dict[str, Any]) -> dict[str, Any]:
        scope = f"{self.strategy_name}:{review_input['eventId']}"
        global_key = self._ai_circuit_key("ai_circuit_retry_after_utc")
        global_row = self.db.execute(
            "SELECT value FROM weather_ai_agent_meta WHERE key=?", (global_key,),
        ).fetchone()
        if not self.ai_calls_allowed(utc_now()):
            raise RuntimeError(
                f"global AI circuit open until {global_row[0] if global_row else 'later'}"
            )
        prompt = self._prompt(review_input)
        started = time.monotonic()
        logging.info(
            "dual strategy AI review started event=%s city=%s prompt_chars=%d",
            review_input["eventId"], review_input.get("city", "unknown"), len(prompt),
        )
        try:
            response = self._run_ai(SCHEMA_PATH, prompt, circuit_scope=scope)
        except Exception as exc:
            message = str(exc).casefold()
            systemic_markers = (
                "insufficient balance", "credits exhausted", "billing", "http 402",
                "quota exceeded", "rate limit", "http 429",
            )
            if any(marker in message for marker in systemic_markers):
                self.open_ai_circuit(utc_now(), str(exc))
            raise
        if global_row:
            self.clear_ai_circuit()
        logging.info(
            "dual strategy AI review finished event=%s city=%s duration_s=%.1f candidates=%d",
            review_input["eventId"], review_input.get("city", "unknown"),
            time.monotonic() - started, len(response.get("singleNoReviews") or []),
        )
        return response

    def validate_ai_response(self, review_input: dict[str, Any], response: dict[str, Any]) -> None:
        if str(response.get("eventId")) != str(review_input["eventId"]):
            raise RuntimeError("AI response eventId does not match review input")
        three_candidate = review_input.get("threeBucketCandidate")
        three_review = response.get("threeBucketReview")
        if (three_candidate is None) != (three_review is None):
            raise RuntimeError("threeBucketReview must be null exactly when no candidate exists")
        if three_review and three_review["decision"] == "ENTER":
            if not three_candidate["centerStable"] or not three_candidate["entryAllowed"]:
                raise RuntimeError("AI cannot enter an unstable or closed-window three-bucket candidate")
        universe = {row["marketId"]: row for row in review_input.get("singleNoUniverse") or []}
        universe_ids = set(universe)
        reviews = response.get("singleNoReviews") or []
        review_ids = [row["marketId"] for row in reviews]
        if len(review_ids) != len(set(review_ids)):
            raise RuntimeError("AI-generated single-NO candidates must have unique market IDs")
        unknown_ids = set(review_ids) - universe_ids
        if unknown_ids:
            raise RuntimeError(f"AI-generated single-NO candidates are outside the supplied universe: {sorted(unknown_ids)}")
        for row in reviews:
            if row.get("decision") != "BUY":
                raise RuntimeError("singleNoReviews may contain only AI-generated BUY candidates")
            low = float(row["conservativePathProbabilityLow"])
            high = float(row["conservativePathProbabilityHigh"])
            if low > high:
                raise RuntimeError("single-NO conservative path probability low exceeds high")
            if row["thesis"] not in NO_THESES or row["sizingTier"] not in {"BASE", "STRONG"}:
                raise RuntimeError("single-NO candidate requires a valid thesis and sizing tier")
            required_text = (
                "pathAssessment", "ridgeAssessment", "marketInefficiencyAssessment",
                "whyMarketMayBeRight", "whyMarketMayBeWrong", "invalidationCondition",
            )
            if any(not str(row.get(field) or "").strip() for field in required_text):
                raise RuntimeError("single-NO candidate requires complete path, Ridge, market, and invalidation assessments")
            if not row.get("supportingEvidence") or not row.get("contradictingEvidence"):
                raise RuntimeError("single-NO candidate requires supporting and contradicting evidence")
            if not row.get("unpricedInformation"):
                raise RuntimeError("single-NO candidate requires specific unpriced information")
            information_types = set(row.get("newInformationTypes") or [])
            weather_types = {
                "metar_change", "daily_high_change", "model_update",
                "radar_cloud_radiation_change", "wind_process_change",
            }
            if not information_types.intersection(weather_types):
                raise RuntimeError("single-NO candidate requires fresh weather evidence")
            inefficiency = row.get("marketInefficiencyType")
            if inefficiency not in {"MARKET_LAG", "WRONG_PRICING", "BOTH"}:
                raise RuntimeError("single-NO candidate requires a valid market inefficiency type")
            if inefficiency in {"MARKET_LAG", "BOTH"} and not information_types.intersection(
                {"market_lag", "orderbook_change"}
            ):
                raise RuntimeError("market-lag candidate requires market-lag or order-book evidence")
        decision_gate = getattr(self, "decision_gate", None) or WeatherDecisionGate()
        decision_gate.validate(review_input, response)

    def _position(self, event_id: str, market_id: str, side: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM weather_dual_positions WHERE strategy_name=? AND event_id=? AND market_id=? AND outcome_side=?",
            (self.strategy_name, event_id, market_id, side),
        ).fetchone()

    def _record_action(
        self, review_id: int, context: dict[str, Any], strategy_type: str, state_hash: str,
        *, market: dict[str, Any] | None = None, side: str | None = None,
        thesis: str | None = None, skew: str | None = None, tier: str | None = None,
        requested: float | None = None, executed: float | None = None, price: float | None = None,
        fee: float = 0.0, action: str, rejection: str | None = None,
    ) -> int:
        cursor = self.db.execute(
            """INSERT INTO weather_dual_actions(
                review_id,strategy_name,event_id,city,strategy_type,market_id,outcome_range,outcome_side,
                thesis,skew,sizing_tier,requested_shares,executed_shares,execution_price,notional_usdc,
                fee_usdc,executed_action,rejection_reason,state_hash,created_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (review_id, self.strategy_name, context["event"]["event_id"], context["event"]["city"],
             strategy_type, market.get("marketId") if market else None, market.get("outcomeRange") if market else None,
             side, thesis, skew, tier, requested, executed, price,
             executed * price if executed is not None and price is not None else None,
             fee, action, rejection, state_hash, iso_utc()),
        )
        return int(cursor.lastrowid)

    def _apply_buy(
        self, action_id: int, context: dict[str, Any], market: dict[str, Any], side: str,
        strategy_type: str, thesis: str | None, shares: float, price: float, state_hash: str,
    ) -> None:
        event_id, city, now = context["event"]["event_id"], context["event"]["city"], iso_utc()
        notional = shares * price
        fee = self.taker_fee_usdc(shares, price)
        cost = notional + fee
        self.db.execute(
            """INSERT INTO weather_dual_positions(
                strategy_name,event_id,city,market_id,outcome_range,outcome_side,strategy_type,thesis,
                shares,cost_basis_usdc,entry_count,last_state_hash,opened_at_utc,updated_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(strategy_name,event_id,market_id,outcome_side) DO UPDATE SET
                shares=shares+excluded.shares,cost_basis_usdc=cost_basis_usdc+excluded.cost_basis_usdc,
                entry_count=entry_count+1,last_state_hash=excluded.last_state_hash,
                thesis=excluded.thesis,updated_at_utc=excluded.updated_at_utc""",
            (self.strategy_name, event_id, city, market["marketId"], market["outcomeRange"], side,
             strategy_type, thesis, shares, cost, 1, state_hash, now, now),
        )
        self.db.execute(
            """INSERT INTO weather_dual_fills(
                action_id,strategy_name,event_id,city,market_id,outcome_side,strategy_type,
                shares,price,notional_usdc,fee_usdc,fill_type,filled_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (action_id, self.strategy_name, event_id, city, market["marketId"], side,
             strategy_type, shares, price, notional, fee, "paper_buy", now),
        )

    def _execute_three_bucket(
        self, review_id: int, context: dict[str, Any], candidate: dict[str, Any] | None,
        review: dict[str, Any] | None, state_hash: str,
    ) -> int:
        if candidate is None or review is None or review["decision"] != "ENTER":
            return 0
        markets = {row["marketId"]: row for row in context.get("markets") or []}
        legs = [markets[row["marketId"]] for row in candidate["legs"]]
        if self.db.execute(
            "SELECT 1 FROM weather_dual_positions WHERE strategy_name=? AND event_id=? AND strategy_type='THREE_BUCKET' LIMIT 1",
            (self.strategy_name, context["event"]["event_id"]),
        ).fetchone():
            self._record_action(review_id, context, "THREE_BUCKET", state_hash, skew=review["skew"], action="OBSERVE", rejection="three-bucket position already exists")
            return 0
        weights = self.three_bucket_weights(review["skew"], self.config)
        prices: list[float] = []
        for market, shares in zip(legs, weights):
            price, available = executable_vwap(market.get("yesBook"), shares, "asks")
            if price is None or available < shares - 1e-9:
                self._record_action(review_id, context, "THREE_BUCKET", state_hash, market=market, side="YES", skew=review["skew"], requested=shares, action="REJECTED", rejection=f"only {available:g} YES shares executable")
                return 0
            prices.append(float(price))
        total = sum(
            shares * price + self.taker_fee_usdc(shares, price)
            for shares, price in zip(weights, prices)
        )
        account = self.account_state()
        city_open = float(self.db.execute(
            "SELECT COALESCE(SUM(cost_basis_usdc),0) FROM weather_dual_positions "
            "WHERE strategy_name=? AND city=? AND shares>0",
            (self.strategy_name, context["event"]["city"]),
        ).fetchone()[0])
        total_open = float(self.db.execute(
            "SELECT COALESCE(SUM(cost_basis_usdc),0) FROM weather_dual_positions "
            "WHERE strategy_name=? AND shares>0", (self.strategy_name,),
        ).fetchone()[0])
        reason = None
        if total > float(self.config.get("threeBucketMaxCombinedCostUsdc", 15)) + 1e-9:
            reason = "three-bucket combined cost exceeds configured cap"
        elif city_open + total > float(self.config.get("maxOpenNotionalPerCity", 20)) + 1e-9:
            reason = "three-bucket city open-notional limit exceeded"
        elif total_open + total > float(self.config.get("maxOpenNotionalTotal", 20)) + 1e-9:
            reason = "three-bucket portfolio open-notional limit exceeded"
        elif total > account["spendableCashUsdc"] + 1e-9:
            reason = "three-bucket buy would breach the cash reserve"
        if reason:
            self._record_action(review_id, context, "THREE_BUCKET", state_hash, skew=review["skew"], action="REJECTED", rejection=reason)
            return 0
        for market, shares, price in zip(legs, weights, prices):
            fee = self.taker_fee_usdc(shares, price)
            action_id = self._record_action(review_id, context, "THREE_BUCKET", state_hash, market=market, side="YES", skew=review["skew"], requested=shares, executed=shares, price=price, fee=fee, action="BUY")
            self._apply_buy(action_id, context, market, "YES", "THREE_BUCKET", None, shares, price, state_hash)
        return 3

    def _execute_single_no(
        self, review_id: int, context: dict[str, Any], universe: list[dict[str, Any]],
        reviews: list[dict[str, Any]], state_hash: str,
    ) -> int:
        markets = {row["marketId"]: row for row in context.get("markets") or []}
        universe_by_id = {row["marketId"]: row for row in universe}
        positions = {
            row["market_id"]: row for row in self.db.execute(
                "SELECT * FROM weather_dual_positions "
                "WHERE strategy_name=? AND event_id=? AND outcome_side='NO' AND shares>0",
                (self.strategy_name, context["event"]["event_id"]),
            ).fetchall()
        }
        account = self.account_state()
        total_open = account["openCostBasisUsdc"]
        spendable_cash = account["spendableCashUsdc"]
        city_open = float(self.db.execute(
            "SELECT COALESCE(SUM(cost_basis_usdc),0) FROM weather_dual_positions "
            "WHERE strategy_name=? AND city=? AND shares>0",
            (self.strategy_name, context["event"]["city"]),
        ).fetchone()[0])
        execution_at = parse_ts(context.get("executionCheckedAtUtc"))

        def priority(review: dict[str, Any]) -> float:
            market = markets.get(review.get("marketId"))
            if market is None:
                return float("-inf")
            position = positions.get(review["marketId"])
            current = float(position["shares"]) if position else 0.0
            target = float(
                self.config["singleNoStrongShares"]
                if review.get("sizingTier") == "STRONG"
                else self.config["singleNoBaseShares"]
            )
            price, _ = executable_vwap(
                market.get("noBook"), max(0.0, target - current), "asks",
            )
            if price is None:
                return float("-inf")
            return float(review["conservativePathProbabilityLow"]) - self.all_in_cost_per_share(price)

        def edge_policy(review: dict[str, Any], price: float | None) -> tuple[float, str]:
            """Return the minimum edge and the auditable policy used for this candidate.

            BASE candidates with several independent, fresh evidence types may use a
            smaller edge only when the executable price is still reasonable. STRONG
            keeps its original 25-point requirement and never uses the relaxed path.
            """
            tier = str(review.get("sizingTier") or "BASE")
            if tier == "STRONG":
                return float(self.config.get("singleNoStrongEdge", 0.25)), "STRONG_FIXED"
            support_count = len(review.get("supportingEvidence") or [])
            info_count = len(set(review.get("newInformationTypes") or []))
            max_price = float(self.config.get("singleNoRelaxedEdgeMaxPrice", 0.80))
            high_support = int(self.config.get("singleNoRelaxedEdgeMinSupporting", 5))
            high_info = int(self.config.get("singleNoRelaxedEdgeMinInformationTypes", 5))
            if (
                price is not None
                and float(price) <= max_price + 1e-9
                and support_count >= high_support
                and info_count >= high_info
            ):
                return float(self.config.get("singleNoRelaxedEdge", 0.02)), "BASE_HIGH_EVIDENCE"
            medium_support = int(self.config.get("singleNoMediumEdgeMinSupporting", 4))
            medium_info = int(self.config.get("singleNoMediumEdgeMinInformationTypes", 3))
            medium_price = float(self.config.get("singleNoMediumEdgeMaxPrice", 0.80))
            if (
                price is not None
                and float(price) <= medium_price + 1e-9
                and support_count >= medium_support
                and info_count >= medium_info
            ):
                return float(self.config.get("singleNoMediumEdge", 0.05)), "BASE_MEDIUM_EVIDENCE"
            return float(self.config.get("singleNoBaseEdge", 0.10)), "BASE_STANDARD"

        fills = 0
        ordered_reviews = sorted(reviews, key=priority, reverse=True)
        primary_market_id = next(
            (row.get("marketId") for row in ordered_reviews if row.get("marketId") in universe_by_id),
            None,
        )
        decision_phase = self._decision_phase(context, execution_at)
        current_metar = (context.get("metar") or {}).get("current") or {}
        observed_values = [
            as_float(current_metar.get("temperature_c")),
            as_float(current_metar.get("observed_daily_max_c")),
        ]
        observed_values = [value for value in observed_values if value is not None]
        observed_max = max(observed_values) if observed_values else None
        for review in ordered_reviews:
            market_id, tier = review["marketId"], review["sizingTier"]
            if market_id not in universe_by_id:
                continue
            market = markets[market_id]
            position = positions.get(market_id)
            current = float(position["shares"]) if position else 0.0
            target = float(self.config["singleNoStrongShares"] if tier == "STRONG" else self.config["singleNoBaseShares"])
            shares = target - current
            reason = None
            universe_market = universe_by_id[market_id]
            if primary_market_id is not None and market_id != primary_market_id:
                reason = "related weather hypothesis: one target bucket per review"
            elif execution_at is not None and not self._trade_window_open(context, execution_at):
                reason = "single-NO trading window is closed"
            elif (
                execution_at is not None
                and review.get("thesis") == "NO_CEILING"
                and not decision_phase.get("ceilingAllowed", True)
            ):
                reason = "early phase prioritizes NO_OVERSHOOT; NO_CEILING requires a confirmed cap"
            elif (
                review.get("thesis") == "NO_CEILING"
                and observed_max is not None
                and as_float(universe_market.get("bucketC")) is not None
                and observed_max >= float(universe_market["bucketC"])
            ):
                reason = "observed temperature has already reached the target; NO_CEILING is impossible"
            elif shares <= 1e-9:
                reason = f"{tier} target already reached"
            elif position and int(position["entry_count"]) >= 2:
                reason = "single-NO permits only one BASE entry and one upgrade"
            elif position and tier != "STRONG":
                reason = "an existing BASE position may only be upgraded to STRONG"
            elif position and position["last_state_hash"] == state_hash:
                reason = "single-NO upgrade requires new observable state"
            elif tier == "STRONG" and (
                len(review.get("supportingEvidence") or []) < 4
                or len(set(review.get("newInformationTypes") or [])) < 3
            ):
                reason = "STRONG requires four supporting facts and three new-information types"
            price, available = executable_vwap(market.get("noBook"), max(shares, 0), "asks")
            if reason is None and (price is None or available < shares - 1e-9):
                reason = f"only {available:g} NO shares executable"
            gap_risk = bool(universe_market.get("metarGapRisk"))
            if reason is None and tier == "STRONG" and gap_risk:
                reason = "STRONG is blocked by METAR-gap risk"
            edge_required, edge_policy_name = edge_policy(review, price)
            if reason is None and float(price) >= float(self.config["singleNoMaxBuyPriceExclusive"]):
                reason = "NO executable price is at or above the configured cap"
            all_in_cost = self.all_in_cost_per_share(float(price or 0))
            if reason is None and float(review["conservativePathProbabilityLow"]) - all_in_cost < edge_required - 1e-9:
                reason = f"conservative one-sided path edge after taker fee is below the {edge_required:.2f} requirement"
            fee = self.taker_fee_usdc(max(shares, 0), float(price or 0))
            cost = shares * float(price or 0) + fee
            if reason is None and city_open + cost > float(self.config.get("maxOpenNotionalPerCity", 20)) + 1e-9:
                reason = "single-NO city open-notional limit exceeded"
            portfolio_cap = self._single_no_portfolio_cap(context, execution_at)
            if reason is None and total_open + cost > portfolio_cap + 1e-9:
                if portfolio_cap < float(self.config.get("maxOpenNotionalTotal", 20)):
                    reason = (
                        "single-NO early portfolio budget reserved for later candidates "
                        f"(cap={portfolio_cap:g})"
                    )
                else:
                    reason = "single-NO portfolio open-notional limit exceeded"
            if reason is None and cost > spendable_cash + 1e-9:
                reason = "single-NO buy would breach the cash reserve"
            requested_shares = max(shares, 0)
            raw_edge = None
            if price is not None:
                raw_edge = float(review["conservativePathProbabilityLow"]) - float(price)
            if reason:
                action_id = self._record_action(
                    review_id, context, "SINGLE_NO", state_hash, market=market, side="NO",
                    thesis=review["thesis"], tier=tier, requested=requested_shares,
                    action="REJECTED", rejection=reason,
                )
                self._record_candidate_audit(
                    action_id=action_id, review_id=review_id, context=context, market=market,
                    side="NO", review=review,
                    decision_time_utc=context.get("executionCheckedAtUtc") or context.get("asOfUtc"),
                    price=float(price) if price is not None else None,
                    available=float(available) if available is not None else None,
                    requested=requested_shares, all_in_cost=all_in_cost if price is not None else None,
                    raw_edge=raw_edge, required_edge=edge_required, status="REJECTED",
                    rejection=f"{reason} [{edge_policy_name}]",
                )
                continue
            action_id = self._record_action(review_id, context, "SINGLE_NO", state_hash, market=market, side="NO", thesis=review["thesis"], tier=tier, requested=shares, executed=shares, price=float(price), fee=fee, action="BUY")
            self._record_candidate_audit(
                action_id=action_id, review_id=review_id, context=context, market=market,
                side="NO", review=review,
                decision_time_utc=context.get("executionCheckedAtUtc") or context.get("asOfUtc"),
                price=float(price), available=float(available), requested=shares,
                all_in_cost=all_in_cost, raw_edge=raw_edge, required_edge=edge_required,
                status="EXECUTED", rejection=None,
            )
            self._apply_buy(action_id, context, market, "NO", "SINGLE_NO", review["thesis"], shares, float(price), state_hash)
            city_open += cost
            total_open += cost
            spendable_cash -= cost
            fills += 1
        return fills

    def persist_review(self, context: dict[str, Any], review_input: dict[str, Any], response: dict[str, Any]) -> dict[str, int]:
        self.validate_ai_response(review_input, response)
        event = context["event"]
        existing = self.db.execute(
            "SELECT review_id FROM weather_dual_reviews WHERE strategy_name=? AND event_id=? AND state_hash=?",
            (self.strategy_name, event["event_id"], review_input["stateHash"]),
        ).fetchone()
        if existing:
            self.mark_scheduled_review_completed([context])
            self.db.commit()
            return {"review_id": int(existing[0]), "three_bucket_fills": 0, "single_no_fills": 0}
        cursor = self.db.execute(
            """INSERT INTO weather_dual_reviews(
                strategy_name,event_id,city,target_date,trigger_type,trigger_time_utc,state_hash,
                reviewed_at_utc,status,input_json,ai_response_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (self.strategy_name, event["event_id"], event["city"], event["target_date"],
             (context.get("trigger") or {}).get("type", "unknown"),
             (context.get("trigger") or {}).get("decisionTriggerTimeUtc") or review_input["asOfUtc"],
             review_input["stateHash"], iso_utc(), "completed",
             json.dumps(review_input, ensure_ascii=False, default=str),
             json.dumps(response, ensure_ascii=False, default=str)),
        )
        review_id = int(cursor.lastrowid)
        three_fills = self._execute_three_bucket(review_id, context, review_input.get("threeBucketCandidate"), response.get("threeBucketReview"), review_input["stateHash"])
        no_fills = self._execute_single_no(review_id, context, review_input.get("singleNoUniverse") or [], response.get("singleNoReviews") or [], review_input["stateHash"])
        getattr(self, "_review_retry_after", {}).pop(str(event["event_id"]), None)
        self.mark_scheduled_review_completed([context])
        self.db.commit()
        return {"review_id": review_id, "three_bucket_fills": three_fills, "single_no_fills": no_fills}

    def _already_reviewed(self, event_id: str, state_hash: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM weather_dual_reviews WHERE strategy_name=? AND event_id=? AND state_hash=? AND status='completed'",
            (self.strategy_name, event_id, state_hash),
        ).fetchone() is not None

    def _trigger_already_consumed(self, event: dict[str, Any]) -> bool:
        trigger = event.get("decision_trigger") or {}
        if trigger.get("type") in {"scheduled_review", "position_review"}:
            return False
        metar = event.get("metar_trigger") or {}
        trigger_time = metar.get("slot_utc")
        if not trigger_time:
            return False
        return self.db.execute(
            "SELECT 1 FROM weather_dual_reviews WHERE strategy_name=? AND event_id=? "
            "AND trigger_time_utc=? AND status IN ('completed','skipped')",
            (self.strategy_name, event["event_id"], trigger_time),
        ).fetchone() is not None

    def _record_skipped_review(
        self, context: dict[str, Any], review_input: dict[str, Any], reason: str,
    ) -> None:
        event, trigger = context["event"], context.get("trigger") or {}
        self.db.execute(
            """INSERT OR IGNORE INTO weather_dual_reviews(
                strategy_name,event_id,city,target_date,trigger_type,trigger_time_utc,state_hash,
                reviewed_at_utc,status,input_json,error
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (self.strategy_name, event["event_id"], event["city"], event["target_date"],
             trigger.get("type", "unknown"), trigger.get("decisionTriggerTimeUtc") or review_input["asOfUtc"],
             review_input["stateHash"], iso_utc(), "skipped",
             json.dumps({
                 "eventId": review_input["eventId"],
                 "city": review_input["city"],
                 "asOfUtc": review_input["asOfUtc"],
                 "stateHash": review_input["stateHash"],
                "inputCounts": {
                    "threeBucket": int(review_input.get("threeBucketCandidate") is not None),
                    "singleNoUniverse": len(review_input.get("singleNoUniverse") or []),
                },
             }, ensure_ascii=False), reason),
        )
        getattr(self, "_review_retry_after", {}).pop(str(event["event_id"]), None)
        self.mark_scheduled_review_completed([context])

    def compact_audit_history(self, now: datetime | None = None) -> int:
        """Drop stale skipped-review payloads while retaining their audit rows."""
        days = max(1, int(self.config.get("skippedReviewPayloadRetentionDays", 14)))
        cutoff = (now or utc_now()).timestamp() - days * 86400
        cursor = self.db.execute(
            """
            UPDATE weather_dual_reviews
            SET input_json='{}',ai_response_json=NULL
            WHERE strategy_name=? AND status='skipped' AND input_json!='{}'
              AND unixepoch(reviewed_at_utc)<?
            """,
            (self.strategy_name, int(cutoff)),
        )
        self.db.commit()
        return int(cursor.rowcount)

    @staticmethod
    def _due_event_time(event: dict[str, Any]) -> datetime:
        trigger = event.get("decision_trigger") or {}
        metar = event.get("metar_trigger") or {}
        return (
            parse_ts(trigger.get("analysisAsOfUtc"))
            or parse_ts(trigger.get("sourceSlotUtc"))
            or parse_ts(metar.get("slot_utc"))
            or datetime.min.replace(tzinfo=UTC)
        )

    def _select_due_events(
        self, events: list[dict[str, Any]], limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Keep one freshest trigger per event and prioritize pending checkpoints."""
        newest: dict[str, dict[str, Any]] = {}
        for event in events:
            event_id = str(event.get("event_id") or "")
            current = newest.get(event_id)
            is_checkpoint = (event.get("decision_trigger") or {}).get("type") in {
                "scheduled_review", "position_review",
            }
            current_is_checkpoint = (
                (current or {}).get("decision_trigger") or {}
            ).get("type") in {"scheduled_review", "position_review"}
            if (
                current is None
                or (is_checkpoint and not current_is_checkpoint)
                or (
                    is_checkpoint == current_is_checkpoint
                    and self._due_event_time(event) > self._due_event_time(current)
                )
            ):
                newest[event_id] = event
        ordered = sorted(
            newest.values(),
            key=lambda event: (
                (event.get("decision_trigger") or {}).get("type")
                not in {"scheduled_review", "position_review"},
                self._due_event_time(event),
                str(event.get("city") or ""),
            ),
        )
        selected_limit = (
            int(self.config.get("maxReviewEventsPerRun", len(ordered) or 1))
            if limit is None else int(limit)
        )
        return ordered[:max(1, selected_limit)]

    @staticmethod
    def _response_requests_entry(response: dict[str, Any]) -> bool:
        three = response.get("threeBucketReview") or {}
        if three.get("decision") == "ENTER":
            return True
        return bool(response.get("singleNoReviews"))

    def _weather_context_change_reason(
        self, context: dict[str, Any], checked_at: datetime,
    ) -> str | None:
        """Detect new weather evidence that arrived while the AI was reasoning."""
        event = context["event"]
        reviewed_metar = (context.get("metar") or {}).get("current") or {}
        fast_timeline = self._fast_metar_timeline(event, checked_at)
        latest_metar = (
            fast_timeline[-1] if fast_timeline
            else self._latest_metar_for_event(event, checked_at)
        ) or {}
        reviewed_observed = parse_ts(reviewed_metar.get("observation_time_utc"))
        latest_observed = parse_ts(latest_metar.get("observation_time_utc"))
        if latest_observed and (
            reviewed_observed is None or latest_observed > reviewed_observed
        ):
            return (
                "new METAR arrived during AI review "
                f"({iso_utc(reviewed_observed) if reviewed_observed else 'missing'} -> "
                f"{iso_utc(latest_observed)})"
            )

        reviewed_process = context.get("weatherProcess") or {}
        latest_process = self.weather_process_state(event, checked_at)
        reviewed_process_slot = parse_ts(reviewed_process.get("snapshotSlotUtc"))
        latest_process_slot = parse_ts(latest_process.get("snapshotSlotUtc"))
        if latest_process_slot and (
            reviewed_process_slot is None or latest_process_slot > reviewed_process_slot
        ):
            reviewed_payload = {
                key: value for key, value in reviewed_process.items()
                if key != "snapshotSlotUtc"
            }
            latest_payload = {
                key: value for key, value in latest_process.items()
                if key != "snapshotSlotUtc"
            }
            if stable_hash(reviewed_payload) != stable_hash(latest_payload):
                return "weather-process evidence changed during AI review"

        reviewed_models = context.get("modelUpdates") or {}
        latest_models = self.model_update_state(event, checked_at)
        for model_name in ("meteoblue", "ecmwf"):
            latest_models[model_name] = self._trim_hourly(latest_models.get(model_name))
        for model_name in ("meteoblue", "ecmwf", "ecmwfEnsemble"):
            if stable_hash(reviewed_models.get(model_name)) != stable_hash(
                latest_models.get(model_name)
            ):
                return f"{model_name} evidence changed during AI review"
        return None

    def _refresh_execution_markets(
        self, context: dict[str, Any], response: dict[str, Any], checked_at: datetime,
    ) -> str | None:
        """Refresh only when AI requests an entry; Python will recheck price and depth."""
        if not self._response_requests_entry(response):
            return None
        markets = self.market_states(context["event"], checked_at)
        if not markets:
            return "latest executable markets are unavailable"
        required_ids = {
            str(row.get("marketId"))
            for row in context.get("markets") or []
            if row.get("marketId")
        }
        latest_ids = {str(row.get("marketId")) for row in markets if row.get("marketId")}
        if not required_ids.issubset(latest_ids):
            return "latest executable markets do not cover every reviewed candidate"
        market_slots = [
            slot for row in markets
            if (slot := parse_ts(row.get("snapshotUtc"))) is not None
        ]
        latest_slot = max(market_slots, default=None)
        max_age = float(self.config.get("maxMarketDataAgeMinutes", 10))
        if latest_slot is None:
            return "latest executable market timestamp is unavailable"
        age = max(0.0, (checked_at - latest_slot).total_seconds() / 60.0)
        if age > max_age:
            return f"latest executable market data is stale (age={age:.1f}m limit={max_age:g}m)"
        context["markets"] = markets
        return None

    def _defer_review(
        self, context: dict[str, Any], message: str, retry_minutes: float,
    ) -> None:
        retry_at = utc_now() + timedelta(minutes=max(0.5, retry_minutes))
        if not hasattr(self, "_review_retry_after"):
            self._review_retry_after = {}
        self._review_retry_after[str(context["event"]["event_id"])] = retry_at
        retry_after = iso_utc(retry_at)
        self.mark_scheduled_review_failed([context], message, retry_after)
        self.db.commit()

    def _cross_city_candidate_priority(
        self, context: dict[str, Any], response: dict[str, Any],
    ) -> tuple[float, ...]:
        """Rank this review's best NO candidate before portfolio allocation.

        The score is deliberately deterministic and auditable.  It is only an
        execution order, never a replacement for the edge/depth/cash checks in
        ``_execute_single_no``.
        """
        markets = {row.get("marketId"): row for row in context.get("markets") or []}
        candidates = [
            row for row in response.get("singleNoReviews") or []
            if row.get("decision") == "BUY" and row.get("marketId") in markets
        ]
        if not candidates:
            # Keep empty/three-bucket reviews after single-NO opportunities.
            return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        positions = {
            row["market_id"]: row for row in self.db.execute(
                "SELECT market_id,shares FROM weather_dual_positions "
                "WHERE strategy_name=? AND event_id=? AND outcome_side='NO' AND shares>0",
                (self.strategy_name, context["event"]["event_id"]),
            ).fetchall()
        }
        scored: list[tuple[float, ...]] = []
        for review in candidates:
            market = markets[review["marketId"]]
            tier = str(review.get("sizingTier") or "BASE")
            target = float(
                self.config.get("singleNoStrongShares", 10)
                if tier == "STRONG" else self.config.get("singleNoBaseShares", 5)
            )
            position = positions.get(review["marketId"])
            current = float(position["shares"]) if position else 0.0
            price, available = executable_vwap(
                market.get("noBook"), max(0.0, target - current), "asks",
            )
            if price is None or available < max(0.0, target - current) - 1e-9:
                continue
            all_in = self.all_in_cost_per_share(float(price))
            path_probability = float(review.get("conservativePathProbabilityLow") or 0.0)
            edge = path_probability - all_in
            support = len(review.get("supportingEvidence") or [])
            info_types = len(set(review.get("newInformationTypes") or []))
            gap_penalty = 1.0 if bool((context.get("metarCoverage") or {}).get("missingDataRisk")) else 0.0
            # New positions are preferred over upgrades when quality is otherwise equal.
            new_position = 1.0 if review["marketId"] not in positions else 0.0
            scored.append((
                # Evidence quality is the primary ordering signal.  Edge and
                # price buffer then break ties before risk and position state.
                support + info_types * 0.5,
                edge,
                0.92 - float(price),
                info_types,
                -gap_penalty,
                new_position,
                1.0 if tier == "STRONG" else 0.0,
            ))
        return max(scored, default=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

    def due_review_contexts(self, now: datetime) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Build selected review contexts for callers that need a batch snapshot."""
        output = []
        for event in self.due_review_events(now):
            self.ridge_v2.begin_review_batch()
            try:
                context = self.build_context(event)
                self.db.commit()
                review_input = self.build_review_input(context, now)
            finally:
                self.ridge_v2.end_review_batch()
            if not review_input.get("threeBucketCandidate") and not review_input.get("singleNoUniverse"):
                self._record_skipped_review(context, review_input, "no tradable single-NO market universe")
                continue
            if self._already_reviewed(event["event_id"], review_input["stateHash"]):
                self.mark_scheduled_review_completed([context])
                continue
            output.append((context, review_input))
        return output

    def due_review_events(self, now: datetime) -> list[dict[str, Any]]:
        """Select events only; build each context immediately before its AI review."""
        pending_events = [
            event for event in self.due_events(now)
            if not self._trigger_already_consumed(event)
            and now >= getattr(self, "_review_retry_after", {}).get(
                str(event.get("event_id") or ""), datetime.min.replace(tzinfo=UTC)
            )
        ]
        events = self._select_due_events(pending_events)
        self.db.commit()
        return events

    def pending_outcome_review_inputs(self, limit: int = 1) -> list[dict[str, Any]]:
        """Build one settled event at a time for the independent AI reviewer."""
        rows = self.db.execute(
            """
            SELECT e.event_id,e.city,e.target_date,e.winning_range,e.resolved_at_utc
            FROM events e
            WHERE e.resolved_at_utc IS NOT NULL
              AND EXISTS(
                  SELECT 1 FROM weather_dual_reviews r
                  WHERE r.strategy_name=? AND r.event_id=e.event_id
                    AND json_extract(r.input_json,'$.problemDefinition.version') IS NOT NULL
                    AND json_extract(r.ai_response_json,'$.problemSolution.selection.decision') IS NOT NULL
              )
              AND NOT EXISTS(SELECT 1 FROM weather_dual_outcome_reviews o WHERE o.strategy_name=? AND o.event_id=e.event_id)
            ORDER BY e.resolved_at_utc LIMIT ?
            """,
            (self.strategy_name, self.strategy_name, max(1, int(limit))),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for event in rows:
            review_rows = self.db.execute(
                """SELECT review_id,reviewed_at_utc,status,input_json,ai_response_json,error
                   FROM weather_dual_reviews WHERE strategy_name=? AND event_id=?
                   ORDER BY reviewed_at_utc""",
                (self.strategy_name, event["event_id"]),
            ).fetchall()
            action_rows = self.db.execute(
                """SELECT action_id,review_id,city,market_id,outcome_range,outcome_side,thesis,
                          sizing_tier,requested_shares,executed_shares,execution_price,notional_usdc,
                          fee_usdc,executed_action,rejection_reason,created_at_utc
                   FROM weather_dual_actions WHERE strategy_name=? AND event_id=? ORDER BY created_at_utc""",
                (self.strategy_name, event["event_id"]),
            ).fetchall()
            fill_rows = self.db.execute(
                """SELECT action_id,market_id,outcome_side,shares,price,notional_usdc,fee_usdc,
                          fill_type,realized_pnl_usdc,filled_at_utc
                   FROM weather_dual_fills WHERE strategy_name=? AND event_id=? ORDER BY filled_at_utc""",
                (self.strategy_name, event["event_id"]),
            ).fetchall()
            event_market_ids = {
                str(row[0]) for row in self.db.execute(
                    "SELECT market_id FROM markets WHERE event_id=?", (event["event_id"],)
                ).fetchall()
            }
            net_pnl = sum(float(row["realized_pnl_usdc"] or 0) for row in fill_rows)
            decision_summaries: list[dict[str, Any]] = []
            for row in review_rows:
                payload = json_value(row["input_json"], {})
                ai_response = json_value(row["ai_response_json"], {})
                decision_summaries.append({
                    "reviewId": row["review_id"],
                    "reviewedAtUtc": row["reviewed_at_utc"],
                    "status": row["status"],
                    "hasAiResponse": bool(ai_response),
                    "asOfUtc": payload.get("asOfUtc"),
                    "problemDefinition": payload.get("problemDefinition"),
                    "decisionPhase": payload.get("decisionPhase"),
                    "evidenceTimeline": payload.get("evidenceTimeline"),
                    "singleNoUniverse": payload.get("singleNoUniverse") or [],
                    "problemSolution": ai_response.get("problemSolution"),
                    "singleNoReviews": ai_response.get("singleNoReviews") or [],
                })
            timeline_path = ROOT / "data" / "ai_context" / str(event["target_date"]) / f"{event['city']}.md"
            try:
                timeline_markdown = timeline_path.read_text(encoding="utf-8")
            except OSError:
                timeline_markdown = ""
            max_chars = max(10_000, int(self.config.get("outcomeReviewerMaxTimelineChars", 80_000)))
            if len(timeline_markdown) > max_chars:
                timeline_markdown = timeline_markdown[:max_chars]
            output.append({
                "event": dict(event),
                "eventMarketIds": sorted(event_market_ids),
                "netPnlUsdc": net_pnl,
                "dailyEvidenceTimelineMarkdown": timeline_markdown,
                "reviews": decision_summaries,
                "actions": [dict(row) for row in action_rows],
                "fills": [dict(row) for row in fill_rows],
            })
        return output

    def call_outcome_reviewer(self, item: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            "你是天气交易系统的独立OutcomeReviewer，只复盘一个已经结算的城市事件。"
            "必须严格区分：做了且亏损、做了且盈利、没做但事后看似盈利、以及正确等待。"
            "盈利不自动代表判断正确，亏损也不自动代表判断错误。请分别评价TRADE_PNL、"
            "THESIS_CORRECTNESS、EVIDENCE_CORRECTNESS、MISPRICING_CORRECTNESS和TIMING_CORRECTNESS。"
            "反事实机会只有在当时信息已经可知、当时有可执行报价和深度、扣费后有edge、且不违反资金约束时才算VALID_MISS；"
            "依赖结算后信息的机会必须标记NOT_A_REAL_OPPORTUNITY。lessonCandidates只能输出OBSERVATION或LESSON_CANDIDATE，"
            "不能根据单个案例发明阈值，也不能把幸运盈利写成可复用规则。不要修改历史决策。\n\n"
            "事件复盘输入：\n" + json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str)
        )
        return self._run_ai(
            OUTCOME_REVIEW_SCHEMA_PATH,
            prompt,
            timeout_seconds=int(self.config.get("outcomeReviewerTimeoutSeconds", 180)),
            circuit_scope=f"{self.strategy_name}:outcome",
        )

    @staticmethod
    def validate_outcome_review(item: dict[str, Any], response: dict[str, Any]) -> None:
        event = item["event"]
        if str(response.get("eventId")) != str(event["event_id"]):
            raise RuntimeError("OutcomeReviewer eventId does not match settled event")
        actual_pnl: dict[str, float] = {}
        executed_markets = {
            str(row.get("market_id")) for row in item.get("fills") or []
            if row.get("fill_type") == "paper_buy"
        }
        for row in item.get("fills") or []:
            if row.get("fill_type") == "settlement":
                market_id = str(row.get("market_id"))
                actual_pnl[market_id] = actual_pnl.get(market_id, 0.0) + float(
                    row.get("realized_pnl_usdc") or 0
                )
        trade_reviews = response.get("executedTradeReviews") or []
        review_markets = [str(row.get("marketId")) for row in trade_reviews]
        if len(review_markets) != len(set(review_markets)) or set(review_markets) != executed_markets:
            raise RuntimeError("OutcomeReviewer must review every executed market exactly once")
        for row in trade_reviews:
            market_id = str(row["marketId"])
            if abs(float(row["tradePnlUsdc"]) - actual_pnl.get(market_id, 0.0)) > 0.01:
                raise RuntimeError("OutcomeReviewer trade PnL does not match the paper ledger")
            if row["classification"] == "LUCKY_PROFIT" and float(row["tradePnlUsdc"]) <= 0:
                raise RuntimeError("LUCKY_PROFIT requires positive PnL")
            if row["classification"] == "REASONABLE_LOSS" and float(row["tradePnlUsdc"]) >= 0:
                raise RuntimeError("REASONABLE_LOSS requires negative PnL")

        review_times: set[str] = set()
        universe_by_time: dict[str, set[str]] = {}
        wait_times: set[str] = set()
        all_universe_ids: set[str] = set()
        all_universe_ids.update(str(value) for value in item.get("eventMarketIds") or [])
        for review in item.get("reviews") or []:
            decision_time = str(review.get("asOfUtc") or review.get("reviewedAtUtc") or "")
            review_times.add(decision_time)
            universe_by_time[decision_time] = {
                str(row.get("marketId")) for row in review.get("singleNoUniverse") or []
            }
            all_universe_ids.update(universe_by_time[decision_time])
            if ((review.get("problemSolution") or {}).get("selection") or {}).get("decision") == "WAIT":
                wait_times.add(decision_time)
            elif review.get("hasAiResponse") and not review.get("singleNoReviews"):
                # Historical reviews before problemSolution used an empty candidate
                # list as the explicit WAIT outcome.
                wait_times.add(decision_time)
        def matching_review_time(value: str) -> str | None:
            if value in review_times:
                return value
            parsed = parse_ts(value)
            if parsed is None:
                return None
            candidates = []
            for candidate in review_times:
                candidate_ts = parse_ts(candidate)
                if candidate_ts is not None:
                    candidates.append((abs((parsed - candidate_ts).total_seconds()), candidate))
            if not candidates:
                return None
            distance, candidate = min(candidates)
            return candidate if distance <= 15 * 60 else None
        def is_event_day(value: str) -> bool:
            parsed = parse_ts(value)
            if parsed is None:
                return False
            try:
                return parsed.astimezone(ZoneInfo(str(event.get("timezone") or "Asia/Shanghai"))).date().isoformat() == str(event.get("target_date"))
            except (TypeError, ValueError):
                return parsed.date().isoformat() == str(event.get("target_date"))
        for row in response.get("missedOpportunities") or []:
            decision_time = matching_review_time(str(row["decisionTimeUtc"]))
            market_id = str(row["marketId"])
            if decision_time is None and not is_event_day(str(row["decisionTimeUtc"])):
                raise RuntimeError("missed opportunity must map to the event day")
            if market_id not in (universe_by_time.get(decision_time, set()) if decision_time else all_universe_ids):
                raise RuntimeError("missed opportunity must map to an actual reviewed market and decision time")
            valid = row["classification"] == "VALID_MISS"
            if valid and not (row["knowableThen"] and row["executableThen"]):
                raise RuntimeError("VALID_MISS must have been knowable and executable at the time")
        for row in response.get("correctWaits") or []:
            matched = matching_review_time(str(row["decisionTimeUtc"]))
            if matched is None and is_event_day(str(row["decisionTimeUtc"])) and wait_times:
                matched = next(iter(wait_times))
            if matched is None or matched not in wait_times:
                raise RuntimeError("correct WAIT must map to an actual WAIT decision")

    def persist_outcome_review(self, item: dict[str, Any], response: dict[str, Any]) -> int:
        event = item["event"]
        self.validate_outcome_review(item, response)
        cursor = self.db.execute(
            """INSERT OR IGNORE INTO weather_dual_outcome_reviews(
                strategy_name,event_id,city,target_date,net_pnl_usdc,review_json,created_at_utc
            ) VALUES(?,?,?,?,?,?,?)""",
            (self.strategy_name, event["event_id"], event["city"], event["target_date"],
             item["netPnlUsdc"], json.dumps(response, ensure_ascii=False), iso_utc()),
        )
        self.db.commit()
        return int(cursor.rowcount > 0)

    def run_outcome_reviewer_once(self) -> dict[str, Any]:
        """Review at most one event; never blocks the live decision loop."""
        local = utc_now().astimezone(ZoneInfo(str(self.config.get("outcomeReviewerTimezone", "Asia/Shanghai"))))
        minute = local.hour * 60 + local.minute
        start = int(self.config.get("outcomeReviewerStartLocalMinutes", 19 * 60 + 15))
        end = int(self.config.get("outcomeReviewerEndLocalMinutes", 23 * 60 + 30))
        if not start <= minute < end:
            return {"reviewed": 0, "skipped": "outside OutcomeReviewer window"}
        if not self.ai_calls_allowed(utc_now(), f"{self.strategy_name}:outcome"):
            return {"reviewed": 0, "skipped": "outcome AI circuit open"}
        items = self.pending_outcome_review_inputs(1)
        if not items:
            return {"reviewed": 0, "pending": 0}
        try:
            response = self.call_outcome_reviewer(items[0])
            return {"reviewed": self.persist_outcome_review(items[0], response), "eventId": items[0]["event"]["event_id"]}
        except Exception as exc:
            logging.exception("dual strategy OutcomeReviewer deferred")
            return {"reviewed": 0, "error": str(exc)[:1000]}

    def settle_positions(self) -> int:
        rows = self.db.execute(
            """SELECT p.*,r.winning_outcome FROM weather_dual_positions p
               JOIN market_resolutions r ON r.market_id=p.market_id
               WHERE p.strategy_name=? AND p.shares>0 AND r.is_resolved=1""",
            (self.strategy_name,),
        ).fetchall()
        count = 0
        for row in rows:
            win = str(row["winning_outcome"] or "").upper() == str(row["outcome_side"]).upper()
            payout = float(row["shares"]) if win else 0.0
            realized = payout - float(row["cost_basis_usdc"])
            settlement_hash = f"settlement:{row['event_id']}:{row['market_id']}:{row['outcome_side']}"
            review_cursor = self.db.execute(
                """INSERT OR IGNORE INTO weather_dual_reviews(
                    strategy_name,event_id,city,target_date,trigger_type,trigger_time_utc,state_hash,
                    reviewed_at_utc,status,input_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (self.strategy_name, row["event_id"], row["city"], "settled", "settlement",
                 iso_utc(), settlement_hash, iso_utc(), "completed", "{}"),
            )
            review_id = int(review_cursor.lastrowid) if review_cursor.lastrowid else int(self.db.execute(
                "SELECT review_id FROM weather_dual_reviews WHERE strategy_name=? AND event_id=? AND state_hash=?",
                (self.strategy_name, row["event_id"], settlement_hash),
            ).fetchone()[0])
            cursor = self.db.execute(
                """INSERT INTO weather_dual_actions(
                    review_id,strategy_name,event_id,city,strategy_type,market_id,outcome_range,outcome_side,
                    executed_shares,execution_price,notional_usdc,executed_action,state_hash,created_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (review_id, self.strategy_name, row["event_id"], row["city"], row["strategy_type"], row["market_id"],
                 row["outcome_range"], row["outcome_side"], row["shares"], 1.0 if win else 0.0,
                 payout, "SETTLE", row["last_state_hash"], iso_utc()),
            )
            self.db.execute(
                """INSERT INTO weather_dual_fills(
                    action_id,strategy_name,event_id,city,market_id,outcome_side,strategy_type,
                    shares,price,notional_usdc,fill_type,realized_pnl_usdc,filled_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(cursor.lastrowid), self.strategy_name, row["event_id"], row["city"], row["market_id"],
                 row["outcome_side"], row["strategy_type"], row["shares"], 1.0 if win else 0.0,
                 payout, "settlement", realized, iso_utc()),
            )
            self.db.execute(
                "UPDATE weather_dual_positions SET shares=0,cost_basis_usdc=0,settled_at_utc=?,realized_pnl_usdc=? "
                "WHERE strategy_name=? AND event_id=? AND market_id=? AND outcome_side=?",
                (iso_utc(), realized, self.strategy_name, row["event_id"], row["market_id"], row["outcome_side"]),
            )
            # Backfill every rejected/executed candidate for this market so the
            # daily review can measure false negatives without reconstructing books.
            self._backfill_candidate_audits(
                row["event_id"], row["market_id"], str(row["winning_outcome"]), iso_utc(),
            )
            count += 1
        self.db.commit()
        return count

    def run_once(self) -> dict[str, Any]:
        now = utc_now()
        compacted = self.compact_audit_history(now)
        settled = self.settle_positions()
        if not self.config.get("enabled", False):
            return {"enabled": False, "paperOnly": True, "settlements": settled, "auditPayloadsCompacted": compacted, "message": "dual strategy is configured but disabled"}
        results, errors = [], []
        if not self.ai_calls_allowed(now):
            return {
                "enabled": True, "paperOnly": True,
                "model": self.config.get("hermesModel"),
                "reviews": results, "errors": errors, "settlements": settled,
                "auditPayloadsCompacted": compacted, "aiCircuitOpen": True,
            }
        pending: list[dict[str, Any]] = []
        for sequence, event in enumerate(self.due_review_events(now)):
            # Build immediately before the AI call so a slow prior city cannot age this context out.
            self.ridge_v2.begin_review_batch()
            try:
                context = self.build_context(event)
                self.db.commit()
                review_input = self.build_review_input(context, utc_now())
            finally:
                self.ridge_v2.end_review_batch()
            if not review_input.get("threeBucketCandidate") and not review_input.get("singleNoUniverse"):
                self._record_skipped_review(context, review_input, "no tradable single-NO market universe")
                continue
            if self._already_reviewed(event["event_id"], review_input["stateHash"]):
                self.mark_scheduled_review_completed([context])
                continue
            preflight_reason = self._decision_context_expiry_reason([context], utc_now())
            if preflight_reason:
                errors.append({"eventId": review_input["eventId"], "error": preflight_reason})
                self._defer_review(
                    context, preflight_reason,
                    float(self.config.get("decisionRetryMinutes", 2)),
                )
                logging.warning(
                    "dual strategy review postponed event=%s: %s",
                    review_input["eventId"], preflight_reason,
                )
                continue
            try:
                response = self.call_review_ai(review_input)
                pending.append({
                    "sequence": sequence,
                    "context": context,
                    "review_input": review_input,
                    "response": response,
                    "priority": self._cross_city_candidate_priority(context, response),
                })
            except Exception as exc:
                self.db.rollback()
                message = str(exc)[:1000]
                errors.append({"eventId": review_input["eventId"], "error": message})
                self._defer_review(
                    context, message,
                    float(self.config.get("aiCircuitBreakMinutes", 15)),
                )
                logging.exception("dual strategy review failed event=%s", review_input["eventId"])
        # Allocate the shared portfolio in quality order, rather than in the
        # order cities happened to finish their AI calls.  Stable sequence
        # ordering keeps empty/three-bucket reviews deterministic.
        pending.sort(
            key=lambda item: (item["priority"], -int(item["sequence"])),
            reverse=True,
        )
        for item in pending:
            context = item["context"]
            review_input = item["review_input"]
            response = item["response"]
            try:
                checked_at = utc_now()
                context["executionCheckedAtUtc"] = iso_utc(checked_at)
                reason = None
                candidate_requested = self._response_requests_entry(response)
                if candidate_requested and self._trade_window_open(context, checked_at):
                    reason = self._weather_context_change_reason(context, checked_at)
                if reason is None and candidate_requested and self._trade_window_open(context, checked_at):
                    reason = self._refresh_execution_markets(context, response, checked_at)
                if reason:
                    errors.append({"eventId": review_input["eventId"], "error": reason})
                    self._defer_review(
                        context, reason,
                        float(self.config.get("decisionRetryMinutes", 2)),
                    )
                    logging.warning(
                        "dual strategy review deferred event=%s: %s",
                        review_input["eventId"], reason,
                    )
                    continue
                results.append(self.persist_review(context, review_input, response))
            except Exception as exc:
                self.db.rollback()
                message = str(exc)[:1000]
                errors.append({"eventId": review_input["eventId"], "error": message})
                self._defer_review(
                    context, message,
                    float(self.config.get("aiCircuitBreakMinutes", 15)),
                )
                logging.exception("dual strategy execution failed event=%s", review_input["eventId"])
        return {"enabled": True, "paperOnly": True, "model": self.config.get("hermesModel"), "reviews": results, "errors": errors, "settlements": settled, "auditPayloadsCompacted": compacted}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hermes dual-strategy weather paper engine")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--init-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    configure_logging(config)
    lock_path = ROOT / "data/weather_dual_strategy.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("w")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.info("another dual strategy process is running")
        return 0
    engine = DualStrategyEngine(config)
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        if args.init_only:
            return 0
        if not args.loop:
            print(json.dumps(engine.run_once(), ensure_ascii=False, indent=2))
            return 0
        while not stop:
            result = engine.run_once()
            if (
                result.get("reviews") or result.get("errors")
                or result.get("settlements") or result.get("auditPayloadsCompacted")
            ):
                print(json.dumps(result, ensure_ascii=False), flush=True)
            for _ in range(30):
                if stop:
                    break
                time.sleep(1)
        return 0
    finally:
        engine.close()
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
