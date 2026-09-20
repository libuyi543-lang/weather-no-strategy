#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import re
import signal
import sqlite3
import statistics
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from weather_data_store import WeatherDataStore, as_float, iso_utc, utc_now
from weather_forecast_evaluator import WeatherForecastEvaluator
from weather_market_alignment import RidgeV2Adapter, market_alignment


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "weather_ai_agent_config.json"
DECISION_SCHEMA_PATH = ROOT / "weather_ai_agent.schema.json"
LESSON_SCHEMA_PATH = ROOT / "weather_ai_lesson.schema.json"
UTC = timezone.utc
NO_PAPER_ENTRY_TYPES = {
    "NO_OVERSHOOT",
    "NO_CEILING",
    "NO_MARKET_TAIL_REJECTION",
}
FROZEN_LADDER_RULE_VERSION = "ladder_v1_1100_market_center_1_3_1"
FROZEN_LADDER_WEIGHTS = (1.0, 3.0, 1.0)
FROZEN_LADDER_VARIANTS = {
    FROZEN_LADDER_RULE_VERSION: FROZEN_LADDER_WEIGHTS,
    "ladder_v2_1100_market_center_0.5_4_0.5": (0.5, 4.0, 0.5),
    "ladder_v3_1100_market_center_5_20_5": (5.0, 20.0, 5.0),
    "ladder_v4_1100_market_center_5_15_5": (5.0, 15.0, 5.0),
}
FROZEN_LADDER_PORTFOLIO_VERSION = "ladder_portfolio_v3_1100_5_20_5_lowest_center_lead"
FROZEN_LADDER_V4_UNLIMITED_PORTFOLIO_VERSION = "ladder_portfolio_v4_1_1100_5_15_5_all_eligible"
FROZEN_LADDER_V4_UNLIMITED_START_DATE = "2026-08-07"
FROZEN_LADDER_PORTFOLIOS = (
    {
        "portfolio_version": FROZEN_LADDER_PORTFOLIO_VERSION,
        "variant_version": "ladder_v3_1100_market_center_5_20_5",
        "selector": "lowest_center_lead_then_city",
        "max_events_per_date": 1,
    },
    {
        "portfolio_version": FROZEN_LADDER_V4_UNLIMITED_PORTFOLIO_VERSION,
        "variant_version": "ladder_v4_1100_market_center_5_15_5",
        "selector": "all_eligible_by_max_spread_then_city",
        "max_events_per_date": 0,
        "forward_start_date": FROZEN_LADDER_V4_UNLIMITED_START_DATE,
    },
)
FROZEN_LADDER_LOCAL_MINUTES = 11 * 60
FROZEN_LADDER_CAPTURE_DEADLINE_MINUTES = 12
FROZEN_LADDER_MIN_CENTER_LEAD = 0.03
FROZEN_LADDER_MIN_COST_EXCLUSIVE = 1.50
FROZEN_LADDER_MAX_COST_INCLUSIVE = 2.00
FROZEN_LADDER_MAX_SPREAD = 0.20
FROZEN_LADDER_MAX_MARKET_AGE_MINUTES = 10.0
WEATHER_TAKER_FEE_RATE = 0.05


def parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def json_value(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return fallback


def validate_json_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    expected = schema.get("type")
    allowed = expected if isinstance(expected, list) else [expected] if expected else []

    def matches(kind: str) -> bool:
        return {
            "null": value is None,
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }.get(kind, False)

    if allowed and not any(matches(kind) for kind in allowed):
        raise RuntimeError(f"Hermes response schema violation at {path}: expected {allowed}")
    if "enum" in schema and value not in schema["enum"]:
        raise RuntimeError(f"Hermes response schema violation at {path}: unsupported value {value!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise RuntimeError(f"Hermes response schema violation at {path}: non-finite number")
        if "minimum" in schema and value < schema["minimum"]:
            raise RuntimeError(f"Hermes response schema violation at {path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise RuntimeError(f"Hermes response schema violation at {path}: above maximum")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        missing = [name for name in schema.get("required") or [] if name not in value]
        if missing:
            raise RuntimeError(f"Hermes response schema violation at {path}: missing {missing}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise RuntimeError(f"Hermes response schema violation at {path}: extra {extra}")
        for key, child in value.items():
            if key in properties:
                validate_json_schema(child, properties[key], f"{path}.{key}")
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            raise RuntimeError(f"Hermes response schema violation at {path}: too few items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                validate_json_schema(child, item_schema, f"{path}[{index}]")


def book_levels(book_json: Any, side: str) -> list[dict[str, float]]:
    payload = json_value(book_json, {})
    rows = payload.get(side, []) if isinstance(payload, dict) else []
    output: list[dict[str, float]] = []
    for row in rows:
        price, size = as_float(row.get("price")), as_float(row.get("size"))
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            output.append({"price": price, "size": size})
    return sorted(output, key=lambda row: row["price"], reverse=side == "bids")


def executable_vwap(book_json: Any, shares: float, side: str) -> tuple[float | None, float]:
    if shares <= 0:
        return None, 0.0
    remaining, total, filled = float(shares), 0.0, 0.0
    for level in book_levels(book_json, side):
        take = min(remaining, level["size"])
        total += take * level["price"]
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            return total / shares, filled
    return None, filled


def estimated_weather_taker_fee(shares: float, price: float | None) -> float | None:
    if price is None or shares <= 0:
        return None
    return float(shares) * WEATHER_TAKER_FEE_RATE * float(price) * (1.0 - float(price))


def conservative_liquidation_value(book_json: Any, shares: float) -> tuple[float | None, float]:
    """Value executable bids and conservatively mark any unmatched shares at zero."""
    if book_json is None or shares <= 0:
        return None, 0.0
    remaining, total, filled = float(shares), 0.0, 0.0
    for level in book_levels(book_json, "bids"):
        take = min(remaining, level["size"])
        total += take * level["price"]
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    return total, filled


class WeatherAIAgent(WeatherDataStore):
    """Half-hour, paper-only weather research and execution engine."""

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.ridge_v2 = RidgeV2Adapter(ROOT, self.db_path, config)
        self._init_agent_schema()
        self._recover_interrupted_runs()
        self._backfill_opportunity_evaluations()
        self._backfill_ladder_shadow_snapshots()
        self.forecast_evaluator = WeatherForecastEvaluator(self.db)

    def _init_agent_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS weather_ai_agent_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS weather_ai_agent_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at_utc TEXT NOT NULL,
                completed_at_utc TEXT,
                status TEXT NOT NULL,
                due_cycles INTEGER NOT NULL DEFAULT 0,
                cycles_written INTEGER NOT NULL DEFAULT 0,
                actions_written INTEGER NOT NULL DEFAULT 0,
                fills_written INTEGER NOT NULL DEFAULT 0,
                settlements_updated INTEGER NOT NULL DEFAULT 0,
                lessons_written INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS weather_ai_agent_cycles (
                cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                run_id INTEGER,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                timezone TEXT NOT NULL,
                metar_observation_time_utc TEXT NOT NULL,
                trigger_slot_utc TEXT NOT NULL,
                analyzed_at_utc TEXT NOT NULL,
                status TEXT NOT NULL,
                state_assessment TEXT,
                model_reality_gap TEXT,
                remaining_heating_assessment TEXT,
                market_consensus_assessment TEXT,
                weather_process_assessment TEXT,
                model_correction_assessment TEXT,
                process_confidence TEXT,
                market_decision_mode TEXT,
                ridge_v2_state_json TEXT,
                market_alignment_json TEXT,
                future_scenarios_json TEXT,
                settlement_distribution_json TEXT,
                temperature_thesis TEXT,
                uncertainty_assessment TEXT,
                next_review_reason TEXT,
                input_json TEXT NOT NULL,
                ai_response_json TEXT,
                error TEXT,
                UNIQUE(strategy_name,event_id,metar_observation_time_utc)
            );
            CREATE TABLE IF NOT EXISTS weather_ai_agent_actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER NOT NULL,
                action_index INTEGER NOT NULL,
                requested_action TEXT NOT NULL,
                executed_action TEXT NOT NULL,
                market_id TEXT,
                outcome_range TEXT,
                outcome_side TEXT,
                requested_shares REAL,
                executed_shares REAL,
                execution_price REAL,
                notional_usdc REAL,
                fee_usdc REAL NOT NULL DEFAULT 0,
                no_win_probability REAL,
                win_probability REAL,
                confidence_low REAL,
                confidence_high REAL,
                market_implied_probability REAL,
                consensus_position TEXT,
                evidence_strength TEXT,
                why_market_may_be_right TEXT,
                why_market_may_be_wrong TEXT,
                contrarian_evidence_json TEXT,
                new_information_types_json TEXT,
                exact_bucket_risk_assessment TEXT,
                upper_bucket_touch_probability REAL,
                outcome_assessment TEXT,
                probability_band TEXT,
                price_assessment TEXT,
                price_risk_assessment TEXT,
                upper_bucket_risk TEXT,
                entry_type TEXT,
                sizing_tier TEXT,
                heating_process_status TEXT,
                new_evidence_since_prior_json TEXT,
                thesis TEXT,
                evidence_json TEXT,
                key_risk TEXT,
                invalidation_condition TEXT,
                rejection_reason TEXT,
                created_at_utc TEXT NOT NULL,
                UNIQUE(cycle_id,action_index),
                FOREIGN KEY(cycle_id) REFERENCES weather_ai_agent_cycles(cycle_id)
            );
            CREATE TABLE IF NOT EXISTS weather_ai_agent_fills (
                fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT,
                action_id INTEGER,
                event_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                city TEXT NOT NULL,
                fill_type TEXT NOT NULL,
                side TEXT NOT NULL,
                outcome_side TEXT NOT NULL DEFAULT 'NO',
                shares REAL NOT NULL,
                price REAL NOT NULL,
                notional_usdc REAL NOT NULL,
                fee_usdc REAL NOT NULL DEFAULT 0,
                realized_pnl_usdc REAL NOT NULL DEFAULT 0,
                filled_at_utc TEXT NOT NULL,
                FOREIGN KEY(action_id) REFERENCES weather_ai_agent_actions(action_id)
            );
            CREATE TABLE IF NOT EXISTS weather_ai_agent_positions (
                strategy_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                city TEXT NOT NULL,
                outcome_range TEXT,
                outcome_side TEXT NOT NULL DEFAULT 'NO',
                shares REAL NOT NULL DEFAULT 0,
                cost_basis_usdc REAL NOT NULL DEFAULT 0,
                realized_pnl_usdc REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                opened_at_utc TEXT,
                updated_at_utc TEXT NOT NULL,
                closed_at_utc TEXT,
                final_outcome TEXT,
                PRIMARY KEY(strategy_name,event_id,market_id)
            );
            CREATE TABLE IF NOT EXISTS weather_ai_agent_lessons (
                lesson_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                target_date TEXT NOT NULL,
                net_pnl_usdc REAL NOT NULL,
                lesson_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                UNIQUE(strategy_name,event_id)
            );
            CREATE TABLE IF NOT EXISTS weather_ai_signal_evaluations (
                signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                observer_event_id INTEGER,
                cycle_id INTEGER NOT NULL,
                action_id INTEGER NOT NULL UNIQUE,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_range TEXT,
                outcome_side TEXT NOT NULL,
                entry_type TEXT NOT NULL,
                source_observed_at_utc TEXT,
                source_fetched_at_utc TEXT,
                weather_state_changed_at_utc TEXT,
                ai_started_at_utc TEXT,
                ai_completed_at_utc TEXT NOT NULL,
                market_snapshot_at_utc TEXT,
                executable_price REAL,
                market_side_first_90_at_utc TEXT,
                resolved_at_utc TEXT,
                winning_range TEXT,
                signal_correct INTEGER,
                lead_minutes REAL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS weather_ai_opportunity_evaluations (
                opportunity_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                cycle_id INTEGER NOT NULL,
                observer_event_id INTEGER,
                city TEXT NOT NULL,
                target_date TEXT NOT NULL,
                decision_time_utc TEXT NOT NULL,
                opportunity_key TEXT NOT NULL,
                opportunity_type TEXT NOT NULL,
                market_id TEXT,
                outcome_range TEXT,
                outcome_side TEXT NOT NULL,
                rank INTEGER,
                classification TEXT,
                probability_band TEXT,
                executable_price REAL,
                executable_shares REAL,
                combined_price REAL,
                market_snapshot_at_utc TEXT,
                weather_state_hash TEXT,
                candidate_status TEXT NOT NULL,
                action_id INTEGER,
                legs_json TEXT,
                created_at_utc TEXT NOT NULL,
                market_side_first_90_at_utc TEXT,
                resolved_at_utc TEXT,
                winning_range TEXT,
                signal_correct INTEGER,
                hypothetical_pnl_usdc REAL,
                lead_minutes REAL,
                UNIQUE(strategy_name,cycle_id,opportunity_key)
            );
            CREATE TABLE IF NOT EXISTS weather_ai_research_snapshots (
                research_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                target_date TEXT NOT NULL,
                sample_slot_utc TEXT NOT NULL,
                market_snapshot_at_utc TEXT,
                source_fetched_at_utc TEXT NOT NULL,
                observed_at_utc TEXT,
                observed_temperature_c REAL,
                observed_daily_max_c REAL,
                meteoblue_max_c REAL,
                ecmwf_max_c REAL,
                process_status TEXT,
                weather_state_hash TEXT,
                market_state_json TEXT NOT NULL,
                weather_state_json TEXT NOT NULL,
                forecast_state_json TEXT NOT NULL,
                resolved_at_utc TEXT,
                winning_range TEXT,
                created_at_utc TEXT NOT NULL,
                UNIQUE(strategy_name,event_id,sample_slot_utc)
            );
            CREATE TABLE IF NOT EXISTS weather_ladder_shadow_snapshots (
                ladder_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                target_date TEXT NOT NULL,
                sample_slot_utc TEXT NOT NULL,
                market_snapshot_at_utc TEXT,
                ridge_snapshot_id INTEGER,
                ridge_feature_as_of_utc TEXT,
                ridge_observation_time_utc TEXT,
                ridge_model_version TEXT,
                distribution_version TEXT,
                calibration_status TEXT,
                calibration_dates INTEGER,
                stability_constraint_applied INTEGER,
                triple_key TEXT NOT NULL,
                lower_bucket_c INTEGER NOT NULL,
                center_bucket_c INTEGER NOT NULL,
                upper_bucket_c INTEGER NOT NULL,
                package_probability REAL,
                combined_yes_price_5 REAL,
                package_edge REAL,
                executable_shares REAL NOT NULL,
                candidate_status TEXT NOT NULL,
                legs_json TEXT NOT NULL,
                ridge_payload_json TEXT,
                created_at_utc TEXT NOT NULL,
                resolved_at_utc TEXT,
                winning_range TEXT,
                official_temperature_c REAL,
                package_hit INTEGER,
                hypothetical_pnl_usdc REAL,
                UNIQUE(strategy_name,event_id,sample_slot_utc,triple_key)
            );
            CREATE TABLE IF NOT EXISTS weather_ladder_frozen_candidates (
                candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                rule_version TEXT NOT NULL,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                target_date TEXT NOT NULL,
                timezone TEXT NOT NULL,
                frozen_slot_utc TEXT NOT NULL,
                frozen_at_utc TEXT NOT NULL,
                market_snapshot_at_utc TEXT,
                market_age_minutes REAL,
                center_market_id TEXT,
                center_bucket_c INTEGER,
                center_midpoint REAL,
                second_midpoint REAL,
                center_lead REAL,
                lower_market_id TEXT,
                upper_market_id TEXT,
                lower_bucket_c INTEGER,
                upper_bucket_c INTEGER,
                lower_vwap_1 REAL,
                center_vwap_3 REAL,
                upper_vwap_1 REAL,
                lower_available REAL,
                center_available REAL,
                upper_available REAL,
                lower_spread REAL,
                center_spread REAL,
                upper_spread REAL,
                combined_cost_usdc REAL,
                estimated_fee_usdc REAL,
                net_cost_usdc REAL,
                total_shares REAL NOT NULL DEFAULT 5,
                eligibility_status TEXT NOT NULL,
                rejection_reasons_json TEXT NOT NULL,
                legs_json TEXT NOT NULL,
                process_state_json TEXT,
                ridge_payload_json TEXT,
                weather_gate_state_json TEXT,
                observed_daily_max_c REAL,
                center_minus_observed_max_c REAL,
                temperature_trend_c_per_hour REAL,
                ensemble_std_max_c REAL,
                ensemble_mean_minus_center_c REAL,
                resolved_at_utc TEXT,
                winning_range TEXT,
                official_temperature_c REAL,
                payout_usdc REAL,
                package_hit INTEGER,
                hypothetical_pnl_usdc REAL,
                UNIQUE(strategy_name,rule_version,event_id,target_date)
            );
            CREATE TABLE IF NOT EXISTS weather_ladder_frozen_variants (
                variant_id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL,
                strategy_name TEXT NOT NULL,
                rule_version TEXT NOT NULL,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                target_date TEXT NOT NULL,
                lower_weight REAL NOT NULL,
                center_weight REAL NOT NULL,
                upper_weight REAL NOT NULL,
                total_weight REAL NOT NULL,
                weight_interpretation TEXT NOT NULL,
                execution_feasibility_status TEXT NOT NULL,
                combined_cost_usdc REAL,
                taker_fee_rate REAL NOT NULL,
                estimated_fee_usdc REAL,
                net_cost_usdc REAL,
                eligibility_status TEXT NOT NULL,
                rejection_reasons_json TEXT NOT NULL,
                legs_json TEXT NOT NULL,
                resolved_at_utc TEXT,
                winning_range TEXT,
                payout_usdc REAL,
                package_hit INTEGER,
                hypothetical_pnl_usdc REAL,
                UNIQUE(strategy_name,rule_version,event_id,target_date),
                FOREIGN KEY(candidate_id) REFERENCES weather_ladder_frozen_candidates(candidate_id)
            );
            CREATE TABLE IF NOT EXISTS weather_ladder_frozen_portfolio_selections (
                selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                portfolio_version TEXT NOT NULL,
                target_date TEXT NOT NULL,
                selection_key TEXT NOT NULL,
                frozen_slot_utc TEXT NOT NULL,
                selected_at_utc TEXT NOT NULL,
                selector TEXT NOT NULL,
                max_events_per_date INTEGER NOT NULL,
                max_cost_usdc REAL NOT NULL,
                eligible_candidates INTEGER NOT NULL,
                selection_status TEXT NOT NULL,
                rejection_reasons_json TEXT NOT NULL,
                candidate_id INTEGER,
                variant_id INTEGER,
                event_id TEXT,
                city TEXT,
                center_lead REAL,
                selected_cost_usdc REAL,
                selected_notional_usdc REAL,
                selected_fee_usdc REAL,
                temperature_trend_c_per_hour REAL,
                center_minus_observed_max_c REAL,
                diagnostic_positive_trend INTEGER,
                diagnostic_center_at_least_two_above_observed INTEGER,
                resolved_at_utc TEXT,
                winning_range TEXT,
                payout_usdc REAL,
                hypothetical_pnl_usdc REAL,
                UNIQUE(strategy_name,portfolio_version,target_date,selection_key),
                FOREIGN KEY(candidate_id) REFERENCES weather_ladder_frozen_candidates(candidate_id),
                FOREIGN KEY(variant_id) REFERENCES weather_ladder_frozen_variants(variant_id)
            );
            CREATE INDEX IF NOT EXISTS idx_weather_ladder_frozen_status
            ON weather_ladder_frozen_candidates(eligibility_status,target_date);
            CREATE INDEX IF NOT EXISTS idx_weather_ladder_frozen_unresolved
            ON weather_ladder_frozen_candidates(resolved_at_utc,event_id);
            CREATE INDEX IF NOT EXISTS idx_weather_ladder_variant_unresolved
            ON weather_ladder_frozen_variants(resolved_at_utc,event_id);
            CREATE INDEX IF NOT EXISTS idx_weather_ladder_portfolio_unresolved
            ON weather_ladder_frozen_portfolio_selections(resolved_at_utc,target_date);
            CREATE TABLE IF NOT EXISTS weather_ai_agent_station_cadence (
                station_id TEXT PRIMARY KEY,
                city TEXT NOT NULL,
                sample_reports INTEGER NOT NULL,
                median_interval_minutes REAL,
                report_minutes_json TEXT NOT NULL,
                median_publication_lag_minutes REAL,
                latest_observation_time_utc TEXT,
                expected_next_observation_utc TEXT,
                expected_available_utc TEXT,
                updated_at_utc TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS weather_ridge_v2_snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                target_date TEXT NOT NULL,
                cutoff_local TEXT NOT NULL,
                feature_as_of_utc TEXT NOT NULL,
                generated_at_utc TEXT NOT NULL,
                state_version TEXT,
                status TEXT NOT NULL,
                artifact TEXT,
                trained_through TEXT,
                latest_observation_time_utc TEXT,
                latest_observation_fetched_at_utc TEXT,
                observation_age_minutes REAL,
                primary_path_c REAL,
                primary_bucket_c INTEGER,
                capping_path_c REAL,
                warm_tail_path_c REAL,
                payload_json TEXT NOT NULL,
                UNIQUE(event_id,cutoff_local,feature_as_of_utc)
            );
            CREATE INDEX IF NOT EXISTS idx_weather_ai_agent_cycles_event
            ON weather_ai_agent_cycles(event_id,metar_observation_time_utc);
            CREATE INDEX IF NOT EXISTS idx_weather_ai_agent_positions_status
            ON weather_ai_agent_positions(status,city);
            CREATE INDEX IF NOT EXISTS idx_weather_ai_agent_fills_event
            ON weather_ai_agent_fills(event_id,filled_at_utc);
            CREATE INDEX IF NOT EXISTS idx_weather_ai_opportunities_event
            ON weather_ai_opportunity_evaluations(event_id,created_at_utc);
            CREATE INDEX IF NOT EXISTS idx_weather_ai_opportunities_unresolved
            ON weather_ai_opportunity_evaluations(resolved_at_utc,opportunity_type);
            CREATE INDEX IF NOT EXISTS idx_weather_ai_research_event_time
            ON weather_ai_research_snapshots(event_id,sample_slot_utc);
            CREATE INDEX IF NOT EXISTS idx_weather_ai_research_unresolved
            ON weather_ai_research_snapshots(resolved_at_utc,event_id);
            CREATE INDEX IF NOT EXISTS idx_weather_ladder_shadow_event_time
            ON weather_ladder_shadow_snapshots(event_id,sample_slot_utc);
            CREATE INDEX IF NOT EXISTS idx_weather_ladder_shadow_unresolved
            ON weather_ladder_shadow_snapshots(resolved_at_utc,event_id);
            CREATE INDEX IF NOT EXISTS idx_weather_ridge_v2_event_time
            ON weather_ridge_v2_snapshots(event_id,feature_as_of_utc);
        """
        )
        self._migrate_frozen_ladder_portfolio_selection_schema()
        migrations = (
            ("weather_ai_agent_actions", "outcome_side", "TEXT"),
            ("weather_ai_agent_actions", "win_probability", "REAL"),
            ("weather_ai_agent_fills", "strategy_name", "TEXT"),
            ("weather_ai_agent_fills", "outcome_side", "TEXT NOT NULL DEFAULT 'NO'"),
            ("weather_ai_agent_positions", "outcome_side", "TEXT NOT NULL DEFAULT 'NO'"),
            ("weather_ai_agent_cycles", "model_reality_gap", "TEXT"),
            ("weather_ai_agent_cycles", "remaining_heating_assessment", "TEXT"),
            ("weather_ai_agent_cycles", "market_consensus_assessment", "TEXT"),
            ("weather_ai_agent_cycles", "weather_process_assessment", "TEXT"),
            ("weather_ai_agent_cycles", "model_correction_assessment", "TEXT"),
            ("weather_ai_agent_cycles", "process_confidence", "TEXT"),
            ("weather_ai_agent_cycles", "market_decision_mode", "TEXT"),
            ("weather_ai_agent_cycles", "ridge_v2_state_json", "TEXT"),
            ("weather_ai_agent_cycles", "market_alignment_json", "TEXT"),
            ("weather_ai_agent_cycles", "future_scenarios_json", "TEXT"),
            ("weather_ai_agent_cycles", "settlement_distribution_json", "TEXT"),
            ("weather_ai_agent_actions", "market_implied_probability", "REAL"),
            ("weather_ai_agent_actions", "consensus_position", "TEXT"),
            ("weather_ai_agent_actions", "evidence_strength", "TEXT"),
            ("weather_ai_agent_actions", "why_market_may_be_right", "TEXT"),
            ("weather_ai_agent_actions", "why_market_may_be_wrong", "TEXT"),
            ("weather_ai_agent_actions", "contrarian_evidence_json", "TEXT"),
            ("weather_ai_agent_actions", "new_information_types_json", "TEXT"),
            ("weather_ai_agent_actions", "exact_bucket_risk_assessment", "TEXT"),
            ("weather_ai_agent_actions", "upper_bucket_touch_probability", "REAL"),
            ("weather_ai_agent_actions", "outcome_assessment", "TEXT"),
            ("weather_ai_agent_actions", "probability_band", "TEXT"),
            ("weather_ai_agent_actions", "price_assessment", "TEXT"),
            ("weather_ai_agent_actions", "price_risk_assessment", "TEXT"),
            ("weather_ai_agent_actions", "upper_bucket_risk", "TEXT"),
            ("weather_ai_agent_actions", "entry_type", "TEXT"),
            ("weather_ai_agent_actions", "sizing_tier", "TEXT"),
            ("weather_ai_agent_actions", "heating_process_status", "TEXT"),
            ("weather_ai_agent_actions", "new_evidence_since_prior_json", "TEXT"),
            ("weather_ai_agent_cycles", "decision_trigger_id", "TEXT"),
            ("weather_ai_agent_cycles", "decision_trigger_type", "TEXT"),
            ("weather_ai_agent_cycles", "decision_trigger_time_utc", "TEXT"),
            ("weather_ai_agent_cycles", "primary_metar_observation_time_utc", "TEXT"),
            ("weather_ai_agent_actions", "analysis_market_snapshot_at_utc", "TEXT"),
            ("weather_ai_agent_actions", "execution_market_snapshot_at_utc", "TEXT"),
            ("weather_ai_agent_actions", "execution_market_fetched_at_utc", "TEXT"),
            ("weather_ai_agent_actions", "execution_market_age_minutes", "REAL"),
            ("weather_ai_agent_actions", "execution_checked_at_utc", "TEXT"),
            ("weather_ai_agent_actions", "execution_metar_observation_time_utc", "TEXT"),
            ("weather_ai_agent_actions", "execution_weather_process_slot_utc", "TEXT"),
            ("weather_ai_agent_actions", "execution_weather_alignment_json", "TEXT"),
            ("weather_ai_agent_actions", "weather_revalidation_status", "TEXT"),
        )
        for table, column, definition in migrations:
            columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        frozen_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(weather_ladder_frozen_candidates)")
        }
        for column, definition in (
            ("weather_gate_state_json", "TEXT"),
            ("observed_daily_max_c", "REAL"),
            ("center_minus_observed_max_c", "REAL"),
            ("temperature_trend_c_per_hour", "REAL"),
            ("ensemble_std_max_c", "REAL"),
            ("ensemble_mean_minus_center_c", "REAL"),
            ("estimated_fee_usdc", "REAL"),
            ("net_cost_usdc", "REAL"),
        ):
            if column not in frozen_columns:
                self.db.execute(
                    f"ALTER TABLE weather_ladder_frozen_candidates ADD COLUMN {column} {definition}"
                )
        variant_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(weather_ladder_frozen_variants)")
        }
        for column, definition in (
            ("taker_fee_rate", "REAL NOT NULL DEFAULT 0.05"),
            ("estimated_fee_usdc", "REAL"),
            ("net_cost_usdc", "REAL"),
        ):
            if column not in variant_columns:
                self.db.execute(
                    f"ALTER TABLE weather_ladder_frozen_variants ADD COLUMN {column} {definition}"
                )
        portfolio_columns = {
            row[1] for row in self.db.execute(
                "PRAGMA table_info(weather_ladder_frozen_portfolio_selections)"
            )
        }
        for column, definition in (
            ("temperature_trend_c_per_hour", "REAL"),
            ("center_minus_observed_max_c", "REAL"),
            ("diagnostic_positive_trend", "INTEGER"),
            ("diagnostic_center_at_least_two_above_observed", "INTEGER"),
            ("selected_notional_usdc", "REAL"),
            ("selected_fee_usdc", "REAL"),
        ):
            if column not in portfolio_columns:
                self.db.execute(
                    f"ALTER TABLE weather_ladder_frozen_portfolio_selections "
                    f"ADD COLUMN {column} {definition}"
                )
        observer_tables = {
            row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "weather_observer_events" in observer_tables:
            observer_columns = {
                row[1] for row in self.db.execute("PRAGMA table_info(weather_observer_events)")
            }
            for column, definition in (
                ("trade_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("trade_last_error", "TEXT"),
                ("trade_retry_after_utc", "TEXT"),
            ):
                if column not in observer_columns:
                    self.db.execute(f"ALTER TABLE weather_observer_events ADD COLUMN {column} {definition}")
        self.db.execute(
            "INSERT OR IGNORE INTO weather_ai_agent_meta(key,value) VALUES('agent_started_at_utc',?)",
            (iso_utc(),),
        )
        self.db.execute(
            "INSERT OR IGNORE INTO weather_ai_agent_meta(key,value) VALUES(?,?)",
            (
                f"initial_cash_usdc:{self.strategy_name}",
                str(float(self.config.get("initialCashUsdc", 20))),
            ),
        )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_weather_ai_cycle_trigger ON weather_ai_agent_cycles(strategy_name,decision_trigger_id) WHERE decision_trigger_id IS NOT NULL"
        )
        self.db.commit()

    def _migrate_frozen_ladder_portfolio_selection_schema(self) -> None:
        """Allow unlimited-city portfolios without rewriting prior one-city rows."""
        columns = {
            row["name"]
            for row in self.db.execute(
                "PRAGMA table_info(weather_ladder_frozen_portfolio_selections)"
            )
        }
        if "selection_key" in columns:
            return
        self.db.executescript(
            """
            DROP INDEX IF EXISTS idx_weather_ladder_portfolio_unresolved;
            ALTER TABLE weather_ladder_frozen_portfolio_selections
                RENAME TO weather_ladder_frozen_portfolio_selections_legacy;
            CREATE TABLE weather_ladder_frozen_portfolio_selections (
                selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                portfolio_version TEXT NOT NULL,
                target_date TEXT NOT NULL,
                selection_key TEXT NOT NULL,
                frozen_slot_utc TEXT NOT NULL,
                selected_at_utc TEXT NOT NULL,
                selector TEXT NOT NULL,
                max_events_per_date INTEGER NOT NULL,
                max_cost_usdc REAL NOT NULL,
                eligible_candidates INTEGER NOT NULL,
                selection_status TEXT NOT NULL,
                rejection_reasons_json TEXT NOT NULL,
                candidate_id INTEGER,
                variant_id INTEGER,
                event_id TEXT,
                city TEXT,
                center_lead REAL,
                selected_cost_usdc REAL,
                selected_notional_usdc REAL,
                selected_fee_usdc REAL,
                temperature_trend_c_per_hour REAL,
                center_minus_observed_max_c REAL,
                diagnostic_positive_trend INTEGER,
                diagnostic_center_at_least_two_above_observed INTEGER,
                resolved_at_utc TEXT,
                winning_range TEXT,
                payout_usdc REAL,
                hypothetical_pnl_usdc REAL,
                UNIQUE(strategy_name,portfolio_version,target_date,selection_key),
                FOREIGN KEY(candidate_id) REFERENCES weather_ladder_frozen_candidates(candidate_id),
                FOREIGN KEY(variant_id) REFERENCES weather_ladder_frozen_variants(variant_id)
            );
            INSERT INTO weather_ladder_frozen_portfolio_selections(
                selection_id,strategy_name,portfolio_version,target_date,selection_key,
                frozen_slot_utc,selected_at_utc,selector,max_events_per_date,max_cost_usdc,
                eligible_candidates,selection_status,rejection_reasons_json,candidate_id,
                variant_id,event_id,city,center_lead,selected_cost_usdc,selected_notional_usdc,
                selected_fee_usdc,temperature_trend_c_per_hour,center_minus_observed_max_c,
                diagnostic_positive_trend,diagnostic_center_at_least_two_above_observed,
                resolved_at_utc,winning_range,payout_usdc,hypothetical_pnl_usdc
            )
            SELECT selection_id,strategy_name,portfolio_version,target_date,
                   COALESCE(event_id,'__no_eligible__'),
                   frozen_slot_utc,selected_at_utc,selector,max_events_per_date,max_cost_usdc,
                   eligible_candidates,selection_status,rejection_reasons_json,candidate_id,
                   variant_id,event_id,city,center_lead,selected_cost_usdc,selected_notional_usdc,
                   selected_fee_usdc,temperature_trend_c_per_hour,center_minus_observed_max_c,
                   diagnostic_positive_trend,diagnostic_center_at_least_two_above_observed,
                   resolved_at_utc,winning_range,payout_usdc,hypothetical_pnl_usdc
            FROM weather_ladder_frozen_portfolio_selections_legacy;
            DROP TABLE weather_ladder_frozen_portfolio_selections_legacy;
            CREATE INDEX idx_weather_ladder_portfolio_unresolved
                ON weather_ladder_frozen_portfolio_selections(resolved_at_utc,target_date);
            """
        )

    def _recover_interrupted_runs(self) -> int:
        """Close run rows left behind when launchd terminated a worker mid-call."""
        completed_at = iso_utc()
        cursor = self.db.execute(
            """
            UPDATE weather_ai_agent_runs
            SET completed_at_utc=?,status='failed',
                error=COALESCE(error || ' | ', '') || 'interrupted before completion; recovered on startup'
            WHERE status='running'
            """,
            (completed_at,),
        )
        self.db.commit()
        if cursor.rowcount:
            logging.warning("recovered %s interrupted weather AI run(s)", cursor.rowcount)
        return int(cursor.rowcount)

    @property
    def strategy_name(self) -> str:
        return str(self.config["strategyName"])

    def allowed_cities(self) -> set[str]:
        return {str(city).strip().casefold() for city in self.config.get("allowedCities", [])}

    def decision_window(
        self, context: dict[str, Any], at_utc: datetime | None = None
    ) -> dict[str, Any]:
        timezone_name = str((context.get("event") or {}).get("timezone") or "UTC")
        decision_time = at_utc or parse_ts((context.get("trigger") or {}).get("slotUtc")) or utc_now()
        try:
            local_time = decision_time.astimezone(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            local_time = decision_time.astimezone(UTC)
            timezone_name = "UTC"
        analysis_start_minutes = int(self.config.get(
            "activeLocalStartMinutes", int(self.config.get("activeLocalStartHour", 7)) * 60
        ))
        trade_start_minutes = int(self.config.get(
            "tradeLocalStartMinutes", int(self.config.get("tradeLocalStartHour", 10)) * 60
        ))
        trade_end_minutes = int(self.config.get(
            "tradeLocalEndMinutes",
            int(self.config.get("tradeLocalEndHour", self.config.get("activeLocalEndHour", 19))) * 60,
        ))
        local_minutes = local_time.hour * 60 + local_time.minute
        trading_allowed = trade_start_minutes <= local_minutes <= trade_end_minutes
        position_only = bool((context.get("trigger") or {}).get("positionOnly"))
        format_minutes = lambda value: f"{value // 60:02d}:{value % 60:02d}"
        return {
            "phase": "trading_allowed" if trading_allowed else "observation_only",
            "decisionLocalTime": local_time.isoformat(timespec="minutes"),
            "timezone": timezone_name,
            "analysisWindowLocal": f"{format_minutes(analysis_start_minutes)}-{format_minutes(trade_end_minutes)}",
            "tradingWindowLocal": f"{format_minutes(trade_start_minutes)}-{format_minutes(trade_end_minutes)}",
            "ordersAllowed": trading_allowed,
            "newEntriesAllowed": trading_allowed and not position_only,
            "positionReviewOnly": position_only,
            "instruction": (
                "AI may buy or sell subject to all weather, probability, liquidity, and risk controls."
                if trading_allowed and not position_only else
                "Position review only: existing positions may be held or reduced, but new entries are disabled."
                if trading_allowed and position_only else
                "Observation only: analyze new data but do not request buy or sell."
            ),
        }

    def _metar_columns(self) -> list[str]:
        """Return enrichment columns present in both new and fixture databases."""
        available = {row["name"] for row in self.db.execute("PRAGMA table_info(weather_observations)")}
        return [
            column for column in (
                "wind_gust", "visibility_m", "pressure_hpa", "flight_category",
                "solar_radiation_wm2", "direct_radiation_wm2", "diffuse_radiation_wm2",
                "sky_conditions_json", "raw_metar", "metar_type", "metar_parser_status",
            ) if column in available
        ]

    def metar_cadence(self, station_id: str, city: str) -> dict[str, Any]:
        days = int(self.config.get("metarCadenceLookbackDays", 14))
        cutoff = iso_utc(utc_now() - timedelta(days=days))
        rows = self.db.execute(
            """
            SELECT observation_time_utc,MIN(fetched_at_utc) AS first_fetched_at_utc
            FROM weather_observations
            WHERE station_id=? AND source='metar' AND status='ok'
              AND observation_time_utc IS NOT NULL AND observation_time_utc>=?
            GROUP BY observation_time_utc ORDER BY observation_time_utc
            """,
            (station_id, cutoff),
        ).fetchall()
        observations = [parse_ts(row["observation_time_utc"]) for row in rows]
        observations = [value for value in observations if value is not None]
        intervals = [
            (current - previous).total_seconds() / 60
            for previous, current in zip(observations, observations[1:])
            if 5 <= (current - previous).total_seconds() / 60 <= 180
        ]
        lags = []
        for row in rows:
            observed, fetched = parse_ts(row["observation_time_utc"]), parse_ts(row["first_fetched_at_utc"])
            if observed and fetched and 0 <= (fetched - observed).total_seconds() <= 10800:
                lags.append((fetched - observed).total_seconds() / 60)
        median_interval = statistics.median(intervals) if intervals else None
        median_lag = statistics.median(lags) if lags else None
        minute_counts: dict[int, int] = {}
        for value in observations:
            minute_counts[value.minute] = minute_counts.get(value.minute, 0) + 1
        report_minutes = [minute for minute, count in sorted(minute_counts.items()) if count >= max(2, len(observations) // 10)]
        latest = observations[-1] if observations else None
        expected_observation = latest + timedelta(minutes=median_interval) if latest and median_interval else None
        expected_available = (
            expected_observation + timedelta(minutes=median_lag)
            if expected_observation and median_lag is not None
            else None
        )
        result = {
            "stationId": station_id,
            "sampleReports": len(observations),
            "medianIntervalMinutes": median_interval,
            "usualReportMinutesUtcHour": report_minutes,
            "medianPublicationLagMinutes": median_lag,
            "latestObservationTimeUtc": iso_utc(latest) if latest else None,
            "expectedNextObservationUtc": iso_utc(expected_observation) if expected_observation else None,
            "expectedAvailableUtc": iso_utc(expected_available) if expected_available else None,
        }
        self.db.execute(
            """
            INSERT INTO weather_ai_agent_station_cadence(
                station_id,city,sample_reports,median_interval_minutes,report_minutes_json,
                median_publication_lag_minutes,latest_observation_time_utc,
                expected_next_observation_utc,expected_available_utc,updated_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(station_id) DO UPDATE SET
                city=excluded.city,sample_reports=excluded.sample_reports,
                median_interval_minutes=excluded.median_interval_minutes,
                report_minutes_json=excluded.report_minutes_json,
                median_publication_lag_minutes=excluded.median_publication_lag_minutes,
                latest_observation_time_utc=excluded.latest_observation_time_utc,
                expected_next_observation_utc=excluded.expected_next_observation_utc,
                expected_available_utc=excluded.expected_available_utc,updated_at_utc=excluded.updated_at_utc
            """,
            (
                station_id, city, len(observations), median_interval,
                json.dumps(report_minutes), median_lag,
                result["latestObservationTimeUtc"], result["expectedNextObservationUtc"],
                result["expectedAvailableUtc"], iso_utc(),
            ),
        )
        self.db.commit()
        return result

    def due_events(self, now: datetime) -> list[dict[str, Any]]:
        """Merge scheduled checkpoints with observer/weather interrupts."""
        if self.config.get("observerTriggersEnabled", False):
            base = self.due_observer_events(now)
        elif self.config.get("metarTriggersEnabled", True):
            base = self._due_metar_events(now)
        else:
            base = []
        scheduled = self.scheduled_review_events(now) if self.config.get("scheduledReviewsEnabled", False) else []
        output: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for event in [*base, *scheduled]:
            trigger = event.get("decision_trigger") or {}
            key = (
                str(event.get("event_id")),
                str(trigger.get("sourceSlotUtc") or (event.get("metar_trigger") or {}).get("observation_time_utc")),
            )
            if key in seen:
                continue
            seen.add(key)
            output.append(event)
        return output

    def _due_metar_events(self, now: datetime) -> list[dict[str, Any]]:
        if self.config.get("observerTriggersEnabled", False):
            return self.due_observer_events(now)
        allowed = self.allowed_cities()
        if not allowed:
            return []
        rows = self.db.execute(
            """
            SELECT e.event_id,e.city,e.target_date,e.station_id,e.station_name,
                   e.resolution_source,e.rules,e.end_date_utc,s.latitude,s.longitude,s.timezone
            FROM events e JOIN stations s ON s.station_id=e.station_id
            WHERE e.resolved_at_utc IS NULL AND s.timezone IS NOT NULL
              AND e.station_id IS NOT NULL
            ORDER BY e.target_date,e.city,e.last_seen_utc DESC
            """
        ).fetchall()
        output: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        start_hour = int(self.config.get("activeLocalStartHour", 0))
        end_hour = int(self.config.get("activeLocalEndHour", 24))
        for row in rows:
            city_key = str(row["city"] or "").strip().casefold()
            if city_key not in allowed:
                continue
            key = (city_key, str(row["target_date"]))
            if key in seen:
                continue
            try:
                local_now = now.astimezone(ZoneInfo(row["timezone"]))
                target = date.fromisoformat(row["target_date"])
            except (ValueError, ZoneInfoNotFoundError):
                continue
            if local_now.date() != target or not start_hour <= local_now.hour < end_hour:
                continue
            extra_columns = self._metar_columns()
            metar = self.db.execute(
                f"""
                SELECT slot_utc,observation_time_utc,fetched_at_utc,temperature_c,dewpoint_c,
                       relative_humidity,wind_direction_deg,wind_speed,wind_speed_unit,
                       {', '.join(extra_columns) + ',' if extra_columns else ''}
                       weather_code,observed_daily_max_c
                FROM weather_observations
                WHERE station_id=? AND source='metar' AND status='ok'
                  AND observation_time_utc IS NOT NULL
                ORDER BY observation_time_utc DESC,slot_utc DESC LIMIT 1
                """,
                (row["station_id"],),
            ).fetchone()
            if not metar:
                continue
            observed_at = parse_ts(metar["observation_time_utc"])
            if observed_at is None:
                continue
            observed_local = observed_at.astimezone(ZoneInfo(row["timezone"]))
            if observed_local.date() != target or not start_hour <= observed_local.hour < end_hour:
                continue
            completed = self.db.execute(
                """
                SELECT 1 FROM weather_ai_agent_cycles
                WHERE strategy_name=? AND event_id=? AND metar_observation_time_utc=?
                  AND status='completed' LIMIT 1
                """,
                (self.strategy_name, row["event_id"], metar["observation_time_utc"]),
            ).fetchone()
            if completed:
                continue
            output.append({**dict(row), "metar_trigger": dict(metar)})
            seen.add(key)
        return output

    def _active_event_rows(self, now: datetime) -> list[dict[str, Any]]:
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
            ORDER BY e.target_date,e.city,e.last_seen_utc DESC
            """
        ).fetchall()
        output: list[dict[str, Any]] = []
        start_hour = int(self.config.get("activeLocalStartHour", 7))
        end_hour = int(self.config.get("activeLocalEndHour", 19))
        seen: set[tuple[str, str]] = set()
        for row in rows:
            city_key = str(row["city"] or "").strip().casefold()
            if city_key not in allowed:
                continue
            try:
                local_now = now.astimezone(ZoneInfo(row["timezone"]))
                target = date.fromisoformat(row["target_date"])
            except (ValueError, ZoneInfoNotFoundError):
                continue
            if local_now.date() != target or not start_hour <= local_now.hour < end_hour:
                continue
            key = (city_key, str(row["target_date"]))
            if key in seen:
                continue
            seen.add(key)
            output.append(dict(row))
        return output

    def _latest_metar_for_event(
        self, event: dict[str, Any], before_utc: datetime,
    ) -> dict[str, Any] | None:
        extra_columns = self._metar_columns()
        row = self.db.execute(
            f"""
            SELECT slot_utc,observation_time_utc,fetched_at_utc,temperature_c,dewpoint_c,
                   relative_humidity,wind_direction_deg,wind_speed,wind_speed_unit,
                   {', '.join(extra_columns) + ',' if extra_columns else ''}
                   weather_code,observed_daily_max_c
            FROM weather_observations
            WHERE station_id=? AND source='metar' AND status='ok'
              AND observation_time_utc IS NOT NULL AND slot_utc<=?
            ORDER BY observation_time_utc DESC,slot_utc DESC LIMIT 1
            """,
            (event["station_id"], iso_utc(before_utc)),
        ).fetchone()
        return dict(row) if row else None

    def scheduled_review_events(self, now: datetime) -> list[dict[str, Any]]:
        """Return the latest unprocessed local checkpoint per active event.

        Older missed checkpoints are marked covered by the newest checkpoint so
        a restarted service does not replay an entire day's AI calls.
        """
        if "scheduledReviewIntervalMinutes" in self.config:
            interval = max(1, int(self.config["scheduledReviewIntervalMinutes"]))
            start_minutes = int(self.config.get("scheduledReviewStartLocalMinutes", 10 * 60))
            end_minutes = int(self.config.get("scheduledReviewEndLocalMinutes", 19 * 60))
            slots_by_minute = [
                (minute, "scheduled_review")
                for minute in range(start_minutes, end_minutes + 1, interval)
            ]
        else:
            review_hours = [int(hour) for hour in self.config.get("scheduledReviewHours", [10, 12, 14, 16])]
            slots_by_minute = [(hour * 60, "scheduled_review") for hour in sorted(set(review_hours))]
            position_hour = self.config.get("scheduledPositionReviewHour", 18)
            if position_hour is not None:
                slots_by_minute.append((int(position_hour) * 60, "position_review"))
        output: list[dict[str, Any]] = []
        changed = False
        for event in self._active_event_rows(now):
            try:
                local_now = now.astimezone(ZoneInfo(event["timezone"]))
                target = date.fromisoformat(event["target_date"])
            except (ValueError, ZoneInfoNotFoundError):
                continue
            eligible: list[tuple[datetime, str]] = []
            for slot_minutes, review_type in slots_by_minute:
                hour, minute = divmod(slot_minutes, 60)
                slot_local = datetime.combine(target, datetime.min.time(), tzinfo=local_now.tzinfo).replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                if slot_local <= local_now:
                    eligible.append((slot_local.astimezone(UTC), review_type))
            if not eligible:
                continue
            for slot_utc, review_type in eligible:
                cursor = self.db.execute(
                    """
                    INSERT OR IGNORE INTO weather_ai_agent_scheduled_reviews(
                        strategy_name,event_id,review_slot_utc,review_type,status,created_at_utc,updated_at_utc
                    ) VALUES(?,?,?,?,'pending',?,?)
                    """,
                    (self.strategy_name, event["event_id"], iso_utc(slot_utc), review_type, iso_utc(now), iso_utc(now)),
                )
                changed = changed or bool(cursor.rowcount)
            rows = self.db.execute(
                """
                SELECT * FROM weather_ai_agent_scheduled_reviews
                WHERE strategy_name=? AND event_id=? AND review_slot_utc<=?
                  AND status NOT IN ('completed','covered')
                ORDER BY review_slot_utc DESC
                """,
                (self.strategy_name, event["event_id"], iso_utc(now)),
            ).fetchall()
            if not rows:
                continue
            selected = rows[0]
            # A later checkpoint supersedes older missed checkpoints.
            for older in rows[1:]:
                self.db.execute(
                    """
                    UPDATE weather_ai_agent_scheduled_reviews
                    SET status='covered',last_error='covered by a later checkpoint',updated_at_utc=?
                    WHERE strategy_name=? AND event_id=? AND review_slot_utc=?
                    """,
                    (iso_utc(now), self.strategy_name, event["event_id"], older["review_slot_utc"]),
                )
                changed = True
            retry_after = parse_ts(selected["retry_after_utc"])
            if retry_after is not None and retry_after > now:
                continue
            review_slot = parse_ts(selected["review_slot_utc"])
            if review_slot is None:
                continue
            if selected["review_type"] == "position_review":
                position = self.db.execute(
                    """
                    SELECT 1 FROM weather_ai_agent_positions
                    WHERE strategy_name=? AND event_id=? AND shares>0 LIMIT 1
                    """,
                    (self.strategy_name, event["event_id"]),
                ).fetchone()
                if not position:
                    self.db.execute(
                        """
                        UPDATE weather_ai_agent_scheduled_reviews
                        SET status='covered',last_error='no open position at position checkpoint',updated_at_utc=?
                        WHERE strategy_name=? AND event_id=? AND review_slot_utc=?
                        """,
                        (iso_utc(now), self.strategy_name, event["event_id"], selected["review_slot_utc"]),
                    )
                    changed = True
                    continue
            # The checkpoint is an audit anchor, never a reason to replay stale
            # executable data. Operational analysis always starts from "now".
            analysis_as_of = now
            metar = self._latest_metar_for_event(event, analysis_as_of)
            if not metar:
                continue
            output.append({
                **event,
                "metar_trigger": metar,
                "decision_trigger": {
                    "id": f"schedule:{event['event_id']}:{selected['review_slot_utc']}",
                    "type": selected["review_type"],
                    "triggerTypes": [selected["review_type"]],
                    "sourceSlotUtc": selected["review_slot_utc"],
                    "analysisAsOfUtc": iso_utc(analysis_as_of),
                    "positionOnly": selected["review_type"] == "position_review",
                },
            })
        if changed:
            self.db.commit()
        return output

    def mark_scheduled_review_completed(self, contexts: list[dict[str, Any]]) -> None:
        now = iso_utc()
        for context in contexts:
            trigger = context.get("trigger") or {}
            if trigger.get("type") not in {"scheduled_review", "position_review"}:
                continue
            self.db.execute(
                """
                UPDATE weather_ai_agent_scheduled_reviews
                SET status='completed',attempts=attempts+1,retry_after_utc=NULL,last_error=NULL,updated_at_utc=?
                WHERE strategy_name=? AND event_id=? AND review_slot_utc=?
                """,
                (now, self.strategy_name, context["event"]["event_id"], trigger.get("decisionTriggerTimeUtc")),
            )

    def mark_scheduled_reviews_covered_by_interrupts(self, contexts: list[dict[str, Any]]) -> None:
        """Avoid a duplicate checkpoint when an interrupt just reviewed newer data."""
        now = iso_utc()
        for context in contexts:
            trigger = context.get("trigger") or {}
            if trigger.get("type") in {"scheduled_review", "position_review"}:
                continue
            source_slot = trigger.get("decisionTriggerTimeUtc") or trigger.get("slotUtc")
            if not source_slot:
                continue
            self.db.execute(
                """
                UPDATE weather_ai_agent_scheduled_reviews
                SET status='covered',last_error='covered by a newer weather or market interrupt',updated_at_utc=?
                WHERE strategy_name=? AND event_id=? AND review_slot_utc<=?
                  AND status IN ('pending','retry_wait')
                """,
                (now, self.strategy_name, context["event"]["event_id"], source_slot),
            )

    def mark_scheduled_review_failed(
        self, contexts: list[dict[str, Any]], error: str, retry_after: str,
    ) -> None:
        now = iso_utc()
        for context in contexts:
            trigger = context.get("trigger") or {}
            if trigger.get("type") not in {"scheduled_review", "position_review"}:
                continue
            self.db.execute(
                """
                UPDATE weather_ai_agent_scheduled_reviews
                SET status='retry_wait',attempts=attempts+1,retry_after_utc=?,last_error=?,updated_at_utc=?
                WHERE strategy_name=? AND event_id=? AND review_slot_utc=?
                """,
                (
                    retry_after, str(error)[:1000], now, self.strategy_name,
                    context["event"]["event_id"], trigger.get("decisionTriggerTimeUtc"),
                ),
            )

    def due_observer_events(self, now: datetime) -> list[dict[str, Any]]:
        allowed = self.allowed_cities()
        if not allowed:
            return []
        rows = self.db.execute(
            """
            WITH pending AS (
                SELECT o.*,ROW_NUMBER() OVER(PARTITION BY o.event_id ORDER BY o.source_slot_utc DESC,o.observer_event_id DESC) AS rn
                FROM weather_observer_events o
                WHERE o.status='completed' AND o.escalation_status='pending'
                  AND o.materiality IN ('REANALYZE','POSITION_ALERT')
                  AND (o.trade_retry_after_utc IS NULL OR o.trade_retry_after_utc<=?)
            )
            SELECT o.observer_event_id,o.event_id,o.city,o.station_id,o.target_date,
                   o.source_slot_utc,o.materiality,o.trigger_types_json,o.state_json,o.response_json,
                   e.station_name,e.resolution_source,e.rules,e.end_date_utc,
                   s.latitude,s.longitude,s.timezone
            FROM pending o
            JOIN events e ON e.event_id=o.event_id
            JOIN stations s ON s.station_id=o.station_id
            WHERE o.rn=1
            ORDER BY CASE o.materiality WHEN 'POSITION_ALERT' THEN 0 ELSE 1 END,
                     o.source_slot_utc,o.observer_event_id
            """
        , (iso_utc(now),)).fetchall()
        output: list[dict[str, Any]] = []
        start_hour = int(self.config.get("activeLocalStartHour", 7))
        end_hour = int(self.config.get("activeLocalEndHour", 19))
        extra_columns = self._metar_columns()
        for row in rows:
            if str(row["city"] or "").strip().casefold() not in allowed:
                continue
            source_time = parse_ts(row["source_slot_utc"])
            try:
                local_time = source_time.astimezone(ZoneInfo(row["timezone"])) if source_time else None
                target = date.fromisoformat(row["target_date"])
            except (ValueError, ZoneInfoNotFoundError):
                continue
            if local_time is None or local_time.date() != target or not start_hour <= local_time.hour < end_hour:
                continue
            metar = self.db.execute(
                f"""
                SELECT slot_utc,observation_time_utc,fetched_at_utc,temperature_c,dewpoint_c,
                       relative_humidity,wind_direction_deg,wind_speed,wind_speed_unit,
                       {', '.join(extra_columns) + ',' if extra_columns else ''}
                       weather_code,observed_daily_max_c
                FROM weather_observations
                WHERE station_id=? AND source='metar' AND status='ok'
                  AND observation_time_utc IS NOT NULL AND slot_utc<=?
                ORDER BY observation_time_utc DESC,slot_utc DESC LIMIT 1
                """,
                (row["station_id"], row["source_slot_utc"]),
            ).fetchone()
            if not metar:
                continue
            trigger_types = json_value(row["trigger_types_json"], [])
            output.append(
                {
                    **dict(row),
                    "metar_trigger": dict(metar),
                    "decision_trigger": {
                        "id": f"observer:{row['observer_event_id']}",
                        "observerEventId": int(row["observer_event_id"]),
                        "type": str(row["materiality"]).lower(),
                        "triggerTypes": trigger_types,
                        "sourceSlotUtc": row["source_slot_utc"],
                        "weatherState": json_value(row["state_json"], {}),
                        "observerAssessment": json_value(row["response_json"], {}),
                    },
                }
            )
        return output

    def market_states(self, event: dict[str, Any], as_of_utc: datetime) -> list[dict[str, Any]]:
        as_of = iso_utc(as_of_utc)
        current_slot_row = self.db.execute(
            "SELECT MAX(slot_utc) AS slot FROM market_snapshots WHERE event_id=? AND slot_utc<=?",
            (event["event_id"], as_of),
        ).fetchone()
        slot = current_slot_row["slot"] if current_slot_row else None
        if not slot:
            return []
        prior_row = self.db.execute(
            "SELECT MAX(slot_utc) AS slot FROM market_snapshots WHERE event_id=? AND slot_utc<?",
            (event["event_id"], slot),
        ).fetchone()
        prior_slot = prior_row["slot"] if prior_row else None
        rows = self.db.execute(
            """
            SELECT m.market_id,m.outcome_range,m.bucket_low,m.bucket_high,m.bucket_unit,
                   ms.slot_utc,ms.yes_best_bid,ms.yes_best_ask,ms.no_best_bid,ms.no_best_ask,
                   ms.yes_book_json,ms.no_book_json,ms.market_volume_24h,ms.market_liquidity,
                   prior.yes_best_bid AS previous_yes_best_bid,
                   prior.yes_best_ask AS previous_yes_best_ask,
                   prior.no_best_bid AS previous_no_best_bid,
                   prior.no_best_ask AS previous_no_best_ask
            FROM markets m JOIN market_snapshots ms ON ms.market_id=m.market_id AND ms.slot_utc=?
            LEFT JOIN market_snapshots prior ON prior.market_id=m.market_id AND prior.slot_utc=?
            WHERE m.event_id=?
            ORDER BY COALESCE(m.bucket_low,-999),COALESCE(m.bucket_high,999)
            """,
            (slot, prior_slot, event["event_id"]),
        ).fetchall()
        default_shares = float(self.config.get("shares", 5))
        strong_no_shares = float(self.config.get("strongNoShares", 10))
        output = []
        for row in rows:
            yes_ask, yes_ask_available = executable_vwap(row["yes_book_json"], default_shares, "asks")
            yes_bid, yes_bid_available = executable_vwap(row["yes_book_json"], default_shares, "bids")
            no_ask, no_ask_available = executable_vwap(row["no_book_json"], default_shares, "asks")
            no_bid, no_bid_available = executable_vwap(row["no_book_json"], default_shares, "bids")
            strong_no_ask, strong_no_ask_available = executable_vwap(
                row["no_book_json"], strong_no_shares, "asks"
            )
            history = [
                dict(item) for item in reversed(self.db.execute(
                    """
                    SELECT slot_utc,yes_best_bid,yes_best_ask,no_best_bid,no_best_ask,
                           market_volume_24h,market_liquidity
                    FROM market_snapshots WHERE market_id=? AND slot_utc<=?
                    ORDER BY slot_utc DESC LIMIT 6
                    """,
                    (row["market_id"], slot),
                ).fetchall())
            ]
            output.append(
                {
                    "marketId": row["market_id"], "outcomeRange": row["outcome_range"],
                    "bucketLow": row["bucket_low"], "bucketHigh": row["bucket_high"],
                    "bucketUnit": row["bucket_unit"], "snapshotUtc": row["slot_utc"],
                    "yesBestBid": row["yes_best_bid"], "yesBestAsk": row["yes_best_ask"],
                    "yesExecutableBuyPrice5": yes_ask, "yesExecutableSellPrice5": yes_bid,
                    "yesBuyAvailableShares": yes_ask_available,
                    "yesSellAvailableShares": yes_bid_available,
                    "previousYesBestBid": row["previous_yes_best_bid"],
                    "previousYesBestAsk": row["previous_yes_best_ask"],
                    "yesBidChange": (
                        as_float(row["yes_best_bid"]) - as_float(row["previous_yes_best_bid"])
                        if as_float(row["yes_best_bid"]) is not None and as_float(row["previous_yes_best_bid"]) is not None
                        else None
                    ),
                    "yesAskChange": (
                        as_float(row["yes_best_ask"]) - as_float(row["previous_yes_best_ask"])
                        if as_float(row["yes_best_ask"]) is not None and as_float(row["previous_yes_best_ask"]) is not None
                        else None
                    ),
                    "noBestBid": row["no_best_bid"], "noBestAsk": row["no_best_ask"],
                    "noExecutableBuyPrice5": no_ask, "noExecutableSellPrice5": no_bid,
                    "noBuyAvailableShares": no_ask_available,
                    "noSellAvailableShares": no_bid_available,
                    "strongNoTargetShares": strong_no_shares,
                    "noExecutableBuyPriceStrong": strong_no_ask,
                    "noStrongBuyAvailableShares": strong_no_ask_available,
                    "previousNoBestBid": row["previous_no_best_bid"],
                    "previousNoBestAsk": row["previous_no_best_ask"],
                    "noBidChange": (
                        as_float(row["no_best_bid"]) - as_float(row["previous_no_best_bid"])
                        if as_float(row["no_best_bid"]) is not None and as_float(row["previous_no_best_bid"]) is not None
                        else None
                    ),
                    "noAskChange": (
                        as_float(row["no_best_ask"]) - as_float(row["previous_no_best_ask"])
                        if as_float(row["no_best_ask"]) is not None and as_float(row["previous_no_best_ask"]) is not None
                        else None
                    ),
                    "volume24h": row["market_volume_24h"], "liquidity": row["market_liquidity"],
                    "recentPriceHistory": history,
                    "yesBook": json_value(row["yes_book_json"], {}),
                    "noBook": json_value(row["no_book_json"], {}),
                }
            )
        return output

    def previous_metar(self, station_id: str, before_observation: str) -> dict[str, Any] | None:
        row = self.db.execute(
            f"""
            SELECT observation_time_utc,temperature_c,dewpoint_c,relative_humidity,
                   wind_direction_deg,wind_speed,wind_speed_unit,
                   {', '.join(self._metar_columns()) + ',' if self._metar_columns() else ''}
                   weather_code,observed_daily_max_c
            FROM weather_observations
            WHERE station_id=? AND source='metar' AND status='ok'
              AND observation_time_utc<?
            GROUP BY observation_time_utc ORDER BY observation_time_utc DESC LIMIT 1
            """,
            (station_id, before_observation),
        ).fetchone()
        return dict(row) if row else None

    def model_update_state(self, event: dict[str, Any], as_of_utc: datetime) -> dict[str, Any]:
        as_of = iso_utc(as_of_utc)
        mblue = self.db.execute(
            """
            SELECT slot_utc,model_ref_time_utc,model_updated_at_utc,forecast_max_c,forecast_peak_local,points_json
            FROM windy_forecasts WHERE station_id=? AND target_date=? AND model='mblue'
              AND status='ok' AND slot_utc<=? ORDER BY slot_utc DESC LIMIT 2
            """,
            (event["station_id"], event["target_date"], as_of),
        ).fetchall()
        ecmwf = self.db.execute(
            """
            SELECT f.slot_utc,f.forecast_max_c,f.forecast_peak_local,f.points_json,
                   v.version_hash
            FROM external_forecasts f
            LEFT JOIN source_collection_versions v
              ON v.source='open_meteo' AND v.station_id=f.station_id
             AND v.target_date=f.target_date AND v.product=f.model
             AND v.raw_payload_id=f.raw_payload_id
            WHERE f.station_id=? AND f.target_date=? AND f.model='ecmwf_ifs025'
              AND f.status='ok' AND f.slot_utc<=? ORDER BY f.slot_utc DESC LIMIT 2
            """,
            (event["station_id"], event["target_date"], as_of),
        ).fetchall()

        def pair(rows: list[sqlite3.Row], primary: bool) -> dict[str, Any] | None:
            if not rows:
                return None
            current, previous = rows[0], rows[1] if len(rows) > 1 else None
            current_max, previous_max = as_float(current["forecast_max_c"]), as_float(previous["forecast_max_c"]) if previous else None
            current_ref = (
                current["model_updated_at_utc"] or current["model_ref_time_utc"]
                if primary else current["version_hash"]
            )
            previous_ref = (
                (previous["model_updated_at_utc"] or previous["model_ref_time_utc"])
                if primary and previous else (previous["version_hash"] if previous else None)
            )
            points = json_value(current["points_json"], [])
            future = []
            for point in points:
                point_time = parse_ts(point.get("time_utc")) if isinstance(point, dict) else None
                if point_time is not None and point_time >= as_of_utc - timedelta(minutes=15):
                    future.append(point)
            future = future[:12]
            return {
                "sampleSlotUtc": current["slot_utc"], "maxC": current_max,
                "previousMaxC": previous_max,
                "revisionC": current_max - previous_max if current_max is not None and previous_max is not None else None,
                "peakLocal": current["forecast_peak_local"],
                "modelVersion": current_ref, "modelUpdatedSincePreviousSample": bool(previous and current_ref != previous_ref),
                "modelRunTimeUtc": None,
                "modelRunTimeSource": "provider_not_exposed" if not primary else "provider_claim_unverified",
                "modelRunTimeConfidence": "unavailable" if not primary else "low",
                "claimedProviderRefTimeUtc": current["model_ref_time_utc"] if primary else None,
                "claimedProviderUpdatedAtUtc": current["model_updated_at_utc"] if primary else None,
                "futureHourlyProcess": future,
            }

        ensemble = None
        has_ensemble = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ensemble_forecasts'"
        ).fetchone()
        if has_ensemble:
            row = self.db.execute(
                """
                SELECT slot_utc,version_hash,model_run_time_utc,model_run_time_source,
                       model_run_confidence,member_count,mean_max_c,std_max_c,min_max_c,
                       max_max_c,q10_max_c,q50_max_c,q90_max_c
                FROM ensemble_forecasts
                WHERE station_id=? AND target_date=? AND model='ecmwf_ifs025'
                  AND status='ok' AND slot_utc<=?
                ORDER BY slot_utc DESC LIMIT 1
                """,
                (event["station_id"], event["target_date"], as_of),
            ).fetchone()
            if row:
                ensemble = {
                    "role": "research_evidence_only",
                    "sampleSlotUtc": row["slot_utc"],
                    "dataVersionHash": row["version_hash"],
                    "modelRunTimeUtc": row["model_run_time_utc"],
                    "modelRunTimeSource": row["model_run_time_source"],
                    "modelRunTimeConfidence": row["model_run_confidence"],
                    "memberCount": row["member_count"],
                    "meanMaxC": row["mean_max_c"],
                    "stdMaxC": row["std_max_c"],
                    "minMaxC": row["min_max_c"],
                    "maxMaxC": row["max_max_c"],
                    "q10MaxC": row["q10_max_c"],
                    "q50MaxC": row["q50_max_c"],
                    "q90MaxC": row["q90_max_c"],
                    "warning": "Ensemble spread is evidence only and cannot bypass the NO safety kernel.",
                }

        return {
            "meteoblue": pair(mblue, True),
            "ecmwf": pair(ecmwf, False),
            "ecmwfEnsemble": ensemble,
        }

    def weather_process_state(self, event: dict[str, Any], as_of_utc: datetime) -> dict[str, Any]:
        """Return the latest auditable observation/process diagnosis from the collector."""
        try:
            row = self.db.execute(
                """
                SELECT slot_utc,primary_observation_time_utc,status,detected_processes_json,state_json
                FROM weather_process_states
                WHERE station_id=? AND target_date=? AND slot_utc<=?
                ORDER BY slot_utc DESC LIMIT 1
                """,
                (event["station_id"], event["target_date"], iso_utc(as_of_utc)),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if not row:
            return {
                "status": "missing",
                "warning": "No process state was captured for this METAR trigger; do not infer satellite/radar conditions.",
            }
        state = json_value(row["state_json"], {})
        if not isinstance(state, dict):
            state = {}
        state["snapshotSlotUtc"] = row["slot_utc"]
        state["status"] = row["status"]
        state["detectedProcesses"] = json_value(row["detected_processes_json"], [])
        return state

    def positions_for_event(self, event_id: str, markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT * FROM weather_ai_agent_positions
            WHERE strategy_name=? AND event_id=? AND shares>0 ORDER BY market_id
            """,
            (self.strategy_name, event_id),
        ).fetchall()
        market_map = {row["marketId"]: row for row in markets}
        output = []
        for row in rows:
            market = market_map.get(row["market_id"])
            outcome_side = str(row["outcome_side"] or "NO").upper()
            exit_price = market.get(f"{outcome_side.lower()}ExecutableSellPrice5") if market else None
            unrealized = (
                float(row["shares"]) * float(exit_price) - float(row["cost_basis_usdc"])
                if exit_price is not None else None
            )
            output.append({**dict(row), "currentExitPrice": exit_price, "unrealizedPnlUsdc": unrealized})
        return output

    def recent_lessons(self, city: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT target_date,net_pnl_usdc,lesson_json FROM weather_ai_agent_lessons
            WHERE strategy_name=? AND city=? ORDER BY target_date DESC LIMIT ?
            """,
            (self.strategy_name, city, int(self.config.get("recentLessonsPerCity", 12))),
        ).fetchall()
        return [
            {"targetDate": row["target_date"], "netPnlUsdc": row["net_pnl_usdc"], "lesson": json_value(row["lesson_json"], {})}
            for row in rows
        ]

    def recent_portfolio_lessons(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT city,target_date,net_pnl_usdc,lesson_json
            FROM weather_ai_agent_lessons WHERE strategy_name=?
            ORDER BY created_at_utc DESC LIMIT ?
            """,
            (self.strategy_name, int(self.config.get("recentPortfolioLessons", 30))),
        ).fetchall()
        return [
            {
                "city": row["city"], "targetDate": row["target_date"],
                "netPnlUsdc": row["net_pnl_usdc"],
                "lesson": json_value(row["lesson_json"], {}),
            }
            for row in rows
        ]

    @staticmethod
    def market_consensus(markets: list[dict[str, Any]]) -> dict[str, Any]:
        rows = []
        for market in markets:
            bid, ask = as_float(market.get("yesBestBid")), as_float(market.get("yesBestAsk"))
            midpoint = (bid + ask) / 2 if bid is not None and ask is not None else ask if ask is not None else bid
            if midpoint is not None:
                rows.append((market, midpoint))
        total = sum(value for _market, value in rows)
        distribution = []
        for rank, (market, midpoint) in enumerate(sorted(rows, key=lambda item: item[1], reverse=True), 1):
            distribution.append({
                "marketId": market["marketId"], "outcomeRange": market["outcomeRange"],
                "bucketLow": market.get("bucketLow"), "bucketHigh": market.get("bucketHigh"),
                "yesMidpoint": midpoint,
                "normalizedProbability": midpoint / total if total > 0 else None,
                "rank": rank,
                "yesAskChange": market.get("yesAskChange"),
                "volume24h": market.get("volume24h"), "liquidity": market.get("liquidity"),
            })
        return {
            "method": "normalized YES bid/ask midpoints; descriptive crowd consensus, not ground truth",
            "rawProbabilitySum": total,
            "favorite": distribution[0] if distribution else None,
            "distribution": distribution,
        }

    def build_context(self, event: dict[str, Any]) -> dict[str, Any]:
        trigger = event["metar_trigger"]
        decision_trigger = event.get("decision_trigger") or {}
        scheduled = decision_trigger.get("type") in {"scheduled_review", "position_review"}
        if scheduled:
            as_of = utc_now()
            trigger = self._latest_metar_for_event(event, as_of) or trigger
        else:
            as_of = parse_ts(
                decision_trigger.get("analysisAsOfUtc")
                or decision_trigger.get("sourceSlotUtc")
                or trigger["slot_utc"]
            ) or utc_now()
        weather = self.local_weather_payload(event, as_of)
        markets = self.market_states(event, as_of)
        previous = self.previous_metar(event["station_id"], trigger["observation_time_utc"])
        current_temp, previous_temp = as_float(trigger["temperature_c"]), as_float(previous.get("temperature_c")) if previous else None
        market_slot = parse_ts(markets[0]["snapshotUtc"]) if markets else None
        models = self.model_update_state(event, as_of)
        model_slot = parse_ts((models.get("meteoblue") or {}).get("sampleSlotUtc"))
        observed_at = parse_ts(trigger["observation_time_utc"]) or as_of
        local_observed = observed_at.astimezone(ZoneInfo(event["timezone"]))
        calibration = self.forecast_evaluator.calibration(
            event["city"], event["target_date"], local_observed.hour * 60 + local_observed.minute
        )
        process_state = self.weather_process_state(event, as_of)
        ridge_state = self.ridge_v2.snapshot(
            event, as_of,
            str((decision_trigger.get("weatherState") or {}).get("stateVersion") or as_of),
            process_state,
        )
        alignment = market_alignment(
            markets, ridge_state, process_state, self.config
        )
        context = {
            "event": {key: event.get(key) for key in (
                "event_id", "city", "target_date", "station_id", "station_name", "timezone",
                "resolution_source", "rules", "end_date_utc"
            )},
            "trigger": {
                "type": decision_trigger.get("type") or "new_metar", "slotUtc": iso_utc(as_of),
                "observationTimeUtc": trigger["observation_time_utc"], "fetchedAtUtc": trigger["fetched_at_utc"],
                "decisionTriggerId": decision_trigger.get("id"),
                "decisionTriggerTimeUtc": decision_trigger.get("sourceSlotUtc") or trigger["slot_utc"],
                "analysisAsOfUtc": iso_utc(as_of),
                "triggerTypes": decision_trigger.get("triggerTypes") or ["primary_metar"],
                "observerEventId": decision_trigger.get("observerEventId"),
            },
            "observerGate": {
                "weatherState": decision_trigger.get("weatherState") or {},
                "assessment": decision_trigger.get("observerAssessment") or {},
                "warning": "This weather-only assessment may wake the trader but never authorizes an order.",
            },
            "metarCadence": self.metar_cadence(event["station_id"], event["city"]),
            "metar": {
                "current": trigger, "previous": previous,
                "temperatureChangeC": current_temp - previous_temp if current_temp is not None and previous_temp is not None else None,
            },
            "weather": weather,
            "modelUpdates": models,
            "weatherProcess": process_state,
            "marketConsensus": self.market_consensus(markets),
            "ridgeV2": ridge_state,
            "marketAlignment": alignment,
            "forecastCalibration": {
                "method": "prior resolved event-days only; one latest same-day snapshot per model/event at or before this local time",
                "localMinute": local_observed.hour * 60 + local_observed.minute,
                "models": calibration,
                "warning": "Treat sampleSufficient=false as descriptive only; do not apply an automatic bias correction.",
            },
            "markets": markets,
            "positions": self.positions_for_event(event["event_id"], markets),
            "account": self.account_state(),
            "recentLessons": self.recent_lessons(event["city"]),
            "recentPortfolioLessons": self.recent_portfolio_lessons(),
            "dataFreshness": {
                "marketAgeMinutes": (as_of - market_slot).total_seconds() / 60 if market_slot else None,
                "meteoblueAgeMinutes": (as_of - model_slot).total_seconds() / 60 if model_slot else None,
            },
        }
        context["decisionWindow"] = self.decision_window(context)
        return context

    def _run_ai(
        self, schema: Path, prompt: str, timeout_seconds: int | None = None,
        circuit_scope: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        if not self.ai_calls_allowed(now, circuit_scope):
            row = self.db.execute(
                "SELECT value FROM weather_ai_agent_meta WHERE key=?",
                (self._ai_circuit_key("ai_circuit_retry_after_utc", circuit_scope),),
            ).fetchone()
            raise RuntimeError(f"AI circuit open until {row[0] if row else 'later'}")
        if str(self.config.get("aiRuntime", "hermes")).casefold() != "hermes":
            raise RuntimeError("weather AI agent only supports the Hermes runtime")
        hermes_binary = Path(str(self.config.get("hermesBinary", "~/.local/bin/hermes"))).expanduser()
        try:
            launcher = hermes_binary.resolve(strict=True)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Hermes executable not found: {hermes_binary}") from exc
        hermes_python = Path(str(self.config.get("hermesPython") or launcher.parent / "python3")).expanduser()
        if not hermes_python.exists():
            raise RuntimeError(f"Hermes Python runtime not found: {hermes_python}")

        profile = str(self.config.get("hermesProfile") or "").strip()
        if not profile:
            raise RuntimeError("hermesProfile must be configured")
        profile_home = Path(
            str(self.config.get("hermesHome") or Path.home() / ".hermes" / "profiles" / profile)
        ).expanduser()
        if not profile_home.is_dir():
            raise RuntimeError(f"Hermes profile not found: {profile_home}")

        schema_payload = json.loads(schema.read_text(encoding="utf-8"))
        hermes_prompt = (
            prompt
            + "\n\n严格输出规则：只返回一个 JSON 对象，不要 Markdown 代码块、前言或结语。"
            + "输出必须符合以下 JSON Schema；字段不可增删：\n"
            + json.dumps(schema_payload, ensure_ascii=False, separators=(",", ":"))
        )
        env = os.environ.copy()
        env["HERMES_HOME"] = str(profile_home)
        env["WEATHER_HERMES_TOOLSETS"] = "memory"
        provider = str(self.config.get("hermesProvider") or "").strip()
        model = str(self.config.get("hermesModel") or "").strip()
        base_url = str(self.config.get("hermesBaseUrl") or "").strip()
        api_key_file = str(self.config.get("hermesApiKeyFile") or "").strip()
        if bool(provider) != bool(model):
            raise RuntimeError("hermesProvider and hermesModel must be configured together")
        if provider:
            env["WEATHER_HERMES_PROVIDER"] = provider
        reasoning_effort = str(self.config.get("hermesReasoningEffort") or "").strip().lower()
        if reasoning_effort:
            env["WEATHER_HERMES_REASONING_EFFORT"] = reasoning_effort
        base_url_env_var = str(self.config.get("hermesBaseUrlEnvVar") or "CUSTOM_BASE_URL").strip()
        api_key_env_var = str(self.config.get("hermesApiKeyEnvVar") or "BEEAPI_API_KEY").strip()
        if base_url:
            env[base_url_env_var] = base_url
        api_key = ""
        if api_key_file:
            key_path = Path(api_key_file).expanduser()
            try:
                api_key = key_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise RuntimeError(f"Hermes API key file is unavailable: {key_path}") from exc
            if not api_key:
                raise RuntimeError(f"Hermes API key file is empty: {key_path}")
            env[api_key_env_var] = api_key
        configured_fallbacks = self.config.get("hermesFallbackModels") or []
        if not isinstance(configured_fallbacks, list):
            raise RuntimeError("hermesFallbackModels must be a list")
        candidate_models = [model] if model else [""]
        if provider:
            candidate_models.extend(str(item).strip() for item in configured_fallbacks)
        candidate_models = list(dict.fromkeys(item for item in candidate_models if item or not provider))
        failures: list[str] = []
        for candidate_model in candidate_models:
            attempt_env = env.copy()
            if provider:
                attempt_env["WEATHER_HERMES_MODEL"] = candidate_model
            label = candidate_model or "profile-default"
            format_retries = max(0, int(self.config.get("aiMalformedResponseRetries", 0)))
            for format_attempt in range(format_retries + 1):
                retry_note = ""
                if format_attempt:
                    retry_note = (
                        "\n\n上一次响应不是符合 Schema 的单一 JSON 对象。请重新计算并只输出有效 JSON；"
                        "字符串中的换行必须转义为\\n，不要截断，不要添加说明。"
                    )
                try:
                    completed = subprocess.run(
                        [str(hermes_python), str(ROOT / "hermes_weather_bridge.py")],
                        input=hermes_prompt + retry_note, text=True, capture_output=True,
                        env=attempt_env, cwd=ROOT,
                        timeout=int(timeout_seconds or self.config["aiTimeoutSeconds"]), check=False,
                    )
                    if completed.returncode != 0:
                        detail = (completed.stderr or completed.stdout)[-1200:]
                        raise RuntimeError(f"Hermes exited {completed.returncode}: {detail}")
                    try:
                        response = self._parse_hermes_json(completed.stdout)
                        validate_json_schema(response, schema_payload)
                    except RuntimeError as exc:
                        failures.append(f"{label} invalid response: {str(exc)[:500]}")
                        if format_attempt < format_retries:
                            continue
                        break
                    self.clear_ai_circuit(circuit_scope)
                    return response
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    failures.append(f"{label}: {str(exc)[:500]}")
                    break
            if provider == "custom" and base_url and api_key and candidate_model:
                try:
                    direct_text = self._run_direct_responses(
                        base_url=base_url,
                        api_key=api_key,
                        model=candidate_model,
                        prompt=hermes_prompt,
                        reasoning_effort=reasoning_effort or "medium",
                        timeout_seconds=int(timeout_seconds or self.config["aiTimeoutSeconds"]),
                    )
                    response = self._parse_hermes_json(direct_text)
                    validate_json_schema(response, schema_payload)
                    self.clear_ai_circuit(circuit_scope)
                    return response
                except Exception as direct_exc:
                    failures.append(f"{label} direct: {str(direct_exc)[:500]}")
        message = f"All {provider or 'configured AI'} routes failed: " + " | ".join(failures)
        self.open_ai_circuit(utc_now(), message, circuit_scope)
        raise RuntimeError(message)

    @staticmethod
    def _run_direct_responses(
        *, base_url: str, api_key: str, model: str, prompt: str,
        reasoning_effort: str, timeout_seconds: int,
    ) -> str:
        """Fallback for Responses providers whose SDK streaming path is unreliable."""
        payload = {
            "model": model,
            "instructions": "Return only the requested final JSON object. Do not use tools or add prose.",
            "input": prompt,
            "store": False,
            "reasoning": {"effort": reasoning_effort},
            "stream": False,
        }
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/responses",
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "weather-ai-agent/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
        if str(body.get("status") or "completed").lower() != "completed":
            raise RuntimeError(f"Responses API status={body.get('status')}: {body.get('error')}")
        parts = [
            str(content.get("text") or "")
            for item in body.get("output") or [] if isinstance(item, dict)
            for content in item.get("content") or [] if isinstance(content, dict)
            if content.get("type") == "output_text"
        ]
        text = "".join(parts).strip()
        if not text:
            raise RuntimeError("Responses API returned no output_text")
        return text

    @staticmethod
    def _parse_hermes_json(raw: str) -> dict[str, Any]:
        text = raw.strip()
        if not text:
            raise RuntimeError("Hermes returned an empty response")
        candidates = [text]
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            candidates.insert(0, fenced.group(1))
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                try:
                    value = json.loads(candidate, strict=False)
                except json.JSONDecodeError:
                    value = None
                if isinstance(value, dict):
                    return value
                start = candidate.find("{")
                if start < 0 or candidate[:start].strip():
                    continue
                try:
                    value, end = json.JSONDecoder(strict=False).raw_decode(candidate[start:])
                except json.JSONDecodeError:
                    continue
                if candidate[start + end:].strip():
                    continue
            if isinstance(value, dict):
                return value
        raise RuntimeError(f"Hermes did not return one valid JSON object: {text[:300]}")

    def call_decision_ai(self, contexts: list[dict[str, Any]]) -> dict[str, Any]:
        # Keep full books and raw hourly payloads in SQLite for audit/replay. Hermes
        # only needs executable depth, recent prices, and the compact process path.
        prompt_contexts = [self._decision_prompt_context(context) for context in contexts]
        if self.config.get("noOnlyPaperMode", False):
            shares = float(self.config.get("shares", 5))
            strong_shares = float(self.config.get("strongNoShares", 10))
            base_edge = float(self.config.get("noBaseEdgeMargin", 0.10))
            strong_edge = float(self.config.get("noStrongEdgeMargin", 0.25))
            strong_evidence = int(self.config.get("strongNoMinEvidenceItems", 4))
            strong_information = int(self.config.get("strongNoMinInformationTypes", 3))
            max_no_price = float(self.config.get("maxNoBuyPriceExclusive", 0.92))
            prompt = (
                "你是天气最高温市场的NO-only Paper研究员。每个城市从当地07:00到19:00每半小时复核一次完整状态。"
                "你的任务不是预测唯一最高温，也不是为了增加交易次数，而是寻找可由天气机制解释、市场尚未完全定价的NO购买机会。"
                "你只能输出observe或buy；禁止sell、hold、YES、换向、无新证据的重复加仓和任何未定义策略。"
                "唯一允许的加仓是把已有BASE仓位按后述规则升级到STRONG。没有合格机会时必须observe。\n\n"
                "每个buy必须且只能选择以下一种entryType：\n"
                "1. NO_OVERSHOOT：目标精确温度档最终大概率会被继续升温穿过。必须说明当前/已观测最高温、剩余有效日照、"
                "升温速度、云层/雷达/风场/周边站如何支持更高档路径，以及什么变化会使升温提前停止。\n"
                "2. NO_CEILING：天气过程已把最高温物理封在目标档以下。必须说明封顶机制，例如持续云雨、冷池、海风、"
                "辐射不足、湿度与边界层限制或剩余日照不足，并解释为何合理暖尾路径仍到不了目标档。\n"
                "3. NO_MARKET_TAIL_REJECTION：盘口仍给某个上方或下方尾部明显权重，但新鲜实时天气机制已经否定该尾部。"
                "必须先解释市场为何可能仍这样定价，再列出至少两类独立实时证据，证明这不是只因为模型中值不同。\n\n"
                "Meteoblue、ECMWF和Ridge V2只是路径参考。优先分析METAR/SPECI轨迹、已观测日最高、周边/上风向站、"
                "RainViewer雷达、Himawari卫星、辐射、风向和剩余日照。市场价格是重要先验：天气判断决定目标档是否应被排除，"
                "真实可执行NO价格决定是否值得买。不得把高胜率等同于正收益；必须让保守NO胜率覆盖买价、尾部误差和滑点。"
                "对目标精确档的粗概率带，保守NO胜率下界固定按：lt_5=>0.95、5_15=>0.85、15_30=>0.70、30_50=>0.50；"
                "gt_50没有可用NO下界。方向尚未确定不等于不能交易，但必须由价格安全边际补偿。"
                f"BASE档目标仓位为 {shares:g} shares，保守NO下界减真实可执行价格必须至少为 {base_edge:.2f}；"
                f"STRONG档目标仓位为 {strong_shares:g} shares，差值必须至少为 {strong_edge:.2f}，并至少给出"
                f" {strong_evidence} 条反共识证据和 {strong_information} 种独立新信息。"
                f"NO可执行买价始终必须低于 {max_no_price:.2f}。已有BASE仓位时，只有出现新的可观测证据才可用STRONG补足到"
                f" {strong_shares:g} shares；不得重复BASE、不得超过STRONG目标。\n\n"
                "marketDecisionMode固定填NO_RESEARCH。futureScenarios必须恰好包含一个primary，其余只能是plausible或tail。"
                "settlementDistribution必须覆盖输入中的所有温度档，按最终结算概率排序；"
                "NO_CEILING和NO_MARKET_TAIL_REJECTION只能购买classification为excluded或highly_unlikely的档位。"
                "NO_OVERSHOOT允许购买excluded、highly_unlikely，或风险带不高于30_50的clear_leader/contender/plausible档，"
                "但必须有仍在升温且上方路径成立的强证据；30_50只能依靠价格差进入，不能伪装成确定性排除。"
                "必须读取marketAlignment：当mode=FOLLOW、weatherAlignment=aligned且marketPrematureConvergence=false时，"
                "禁止对marketLeader直接买NO；即使价格很低也必须observe。只有确定性层确认市场过早收敛并给出上方受支持路径时，"
                "才可研究主档被继续升温穿过。"
                "每个action都要如实填写marketImpliedProbability、priceAssessment、priceRiskAssessment、evidence、keyRisk、"
                "invalidationCondition和newEvidenceSincePrior。每个动作还必须填写sizingTier：买入为BASE或STRONG，observe为NOT_APPLICABLE。"
                "买入必须priceAssessment=favorable；observe时marketId、outcomeSide、shares为null，entryType填NONE。"
                "STRONG首次建仓请求完整目标股数；已有BASE仓位时只请求补足差额。不要声称成交，Python Paper执行内核会用最新"
                "订单簿、现金、仓位和数据时效复核。\n\n"
                "输入：\n" + json.dumps(prompt_contexts, ensure_ascii=False, separators=(",", ":"), default=str)
            )
            return self._run_ai(DECISION_SCHEMA_PATH, prompt)
        if self.config.get("autonomousMode", False):
            reserve = float(self.config.get("minCashReserveUsdc", 5.0))
            prompt = (
                "你是这个20 USDC天气市场Paper账户的自主交易负责人。用户只负责决定你能看到哪些数据；"
                "天气判断、市场判断、是否交易、YES或NO、温度档、仓位、持有、减仓、退出和换向全部由你决定。"
                "你的唯一目标是在可接受风险下长期最大化账户净值，而不是最大化交易次数、命中率或单日PnL。"
                f"永远留在牌桌上：安全内核要求成交后至少保留 {reserve:.2f} USDC现金；不得请求绕过这一底线。"
                "除此之外，不存在预设交易模式、固定edge、指定入场类型、确定性候选列表或YES/NO偏好。"
                "你可以跟随市场，也可以在证据充分时反对市场；可以不交易，也可以管理多个仓位。"
                "Ridge V2.1中心和Ridge V3每桶概率都只是研究输入，不是裁判；V3在独立样本日期不足时尤其不得视为校准真值。"
                "你应综合Meteoblue、ECMWF、METAR、"
                "天气过程、雷达、卫星、周边站、剩余日照、盘口结构、流动性、已有持仓、账户状态和历史复盘。"
                "市场价格是重要信息但不是答案；外部数据可能有错误或延迟，发现内部矛盾时自行降低仓位或等待。"
                "recentLessons与recentPortfolioLessons来自过去真实结算和PnL；你可以据此更新未来做法，但必须区分"
                "可复用规律、样本不足和随机结果。每个周期都独立判断最有利的账户动作。"
                "输出仍使用结构化字段方便审计：marketDecisionMode固定填AUTONOMOUS；自主新开仓entryType填"
                "AI_DISCRETION；hold/sell填POSITION_MANAGEMENT；observe填NONE。probabilityBand、情景、档位排序和"
                "理由是你的当前判断记录，不构成额外交易许可。buy/sell/hold必须引用输入中的marketId和YES/NO；"
                "换向时先卖出旧方向，再买入新方向。不要声称成交，Python安全内核会按最新订单簿决定实际执行。\n\n"
                "输入：\n" + json.dumps(prompt_contexts, ensure_ascii=False, separators=(",", ":"), default=str)
            )
            return self._run_ai(DECISION_SCHEMA_PATH, prompt)
        prompt = (
            "你是一个负责复核候选并执行 paper 组合管理的天气AI，不是自由发挥的交易员。输入可能来自定时复核、天气重大变化、盘口候选价格或持仓告警；"
            "METAR/SPECI只是证据源之一，不再是唯一触发器。observerGate 是不看盘口的低延迟天气判断，它只有叫醒权，"
            "你必须独立复核其机制、档位变化和数据新鲜度，不能因为它请求升级就强行交易。"
            "市场是默认先验：先解释市场可能知道什么，再判断天气证据是否足以改变它。Python确定性层已经根据市场、Ridge V2和天气过程生成"
            "marketAlignment、marketDecisionMode和候选列表。你只能在候选列表内选择新开仓，不能自行发明新的市场、档位或入场类型；"
            "你可以否决候选、等待、延后，或管理已有仓位。把Ridge V2.1当作路径候选生成器，把V3桶概率仅当研究证据；"
            "它们都不能绕过粗概率档、价格、仓位或安全层。"
            "你必须像持续管理账户的人一样，把新报文、Meteoblue 主模型、ECMWF 辅助模型、模型是否真正更新、"
            "YES/NO 两侧盘口变化、已有持仓、结算规则、同城历史经验和气象过程综合起来。新入场默认只允许两种："
            "YES_CONVERGENCE（市场主档与天气路径收敛到唯一最终档）和 NO_EXCLUSION（目标档被主要与合理备选路径共同排除）；"
            "FADE模式下额外允许NO_LEADER_OVERSHOOT：只有市场主档被过早定价、仍接近/触及且上方天气路径仍物理可行时，才可考虑买该主档NO；"
            "另有一个明确标记的 paper 实验 YES_LADDER_EXPERIMENT，用于覆盖2-3个相邻候选档，不能当作普通YES信号。"
            "marketDecisionMode 必须原样复述确定性层的 FOLLOW、WATCH、FADE 或 NO_EDGE。FOLLOW通常跟随市场；WATCH和NO_EDGE禁止新开仓；"
            "FADE也不是自动交易，必须用多源强证据解释反共识。其他情况一律 WAIT/observe。已有仓位可以持有、减仓或退出。"
            "scheduled_review 是定时状态复核，不代表必须交易；position_review 只允许管理已有仓位，禁止新建仓位。"
            "必须先读取 decisionWindow：当地07:00至10:00是 observation_only，只分析和积累证据，不得请求 buy 或 sell；"
            "可以对已有仓位输出 hold，没有仓位则输出 observe。当地10:00至19:00才允许请求交易。这个时间门只规定最早"
            "下单时间，不代表10点后应该交易；条件不足仍然observe。精确档YES没有固定下午时间，必须由剩余加热和天气过程决定。"
            "不要为了有动作而交易，也不要机械依赖单一模型或固定 edge。模型只是先验，不是答案：必须把当前实况与模型"
            "同一时刻的小时路径比较，判断温度偏差、升温速度偏差、云量/湿度/风场是否符合模型，再根据剩余有效日照、"
            "未来逐小时云层、辐射、降雨、风和湿度建立至少两个互斥的天气情景。不要制造看似精确的单点概率："
            "futureScenarios 用 primary/plausible/tail 表示相对可信度；settlementDistribution 必须对输入 markets 中每一个"
            "outcomeRange 各输出一行，用 rank、classification 和粗粒度 probabilityBand 排序，包括低概率尾部。"
            "如果第一档不能明显领先第二档，不得标记 clear_leader，而应把相近档标成 contender。不得把模型最高温直接"
            "当作最终结算档。\n\n"
            "weatherProcess 是观测层的过程诊断，必须先判断正在发生的过程：升温、放晴、云带接近、海风、冷池或无明确变化。"
            "它包含雷达、Himawari、上风向站点和辐射数据的质量警告；不可用的数据必须明确承认，不能用模型云量冒充实况。"
            "weatherProcessAssessment 必须说明哪些观测事实支持过程判断；modelCorrectionAssessment 必须说明相对"
            "Meteoblue/ECMWF 的方向性修正、幅度不确定性和何时会失效。不要把 deterministic layer 的方向信号当成确定结论。\n\n"
            "精确温度档 YES 是双边障碍仓位：温度未达到会输，达到后再高一档同样会输。普通YES_CONVERGENCE只能选择 rank=1 且"
            "classification=clear_leader 的最终结算主档，而不是只会触及的温度。YES_LADDER_EXPERIMENT是唯一例外："
            "它可以覆盖rank 1到2或1到3的相邻候选档，但必须满足组合可执行价格总和低于实验上限，且不能把触及当成结算确定。"
            "当前温度已经触及目标档、模型最高温接近该档或价格便宜，"
            "都不能单独支持 YES 买入或加仓。只要有效加热仍在继续，就必须保留上方短触风险；海风、云层或降雨可以降低"
            "持续升温概率，但不能自动消除再升 1C 的短触尾部。每个动作必须填写 exactBucketRiskAssessment、"
            "upperBucketRisk、heatingProcessStatus 和 newEvidenceSincePrior。NO 只能选择 classification=excluded 或"
            "highly_unlikely 的档位；plausible 或 contender 不能仅因 NO 价格看起来便宜而购买。已有仓位只有在新的可观测"
            "天气证据确实降低上方风险或提高所持方向后才能加仓。\n\n"
            "持仓管理必须使用三状态：PROTECTED（目标档为excluded/highly_unlikely，默认hold）；"
            "WATCH（目标档为contender/plausible，继续观察，不因原入场条件变弱就自动sell）；"
            "INVALID（目标档成为clear_leader，或实况已经触及目标档且仍在active heating，才允许sell）。"
            "不要把WATCH状态误判为INVALID；卖出前必须说明状态为何已经失效。\n\n"
            "每个 action 必须填写 entryType：observe=NONE，YES新买入=YES_CONVERGENCE，NO新买入=NO_EXCLUSION，"
            "FADE中的主档过早收敛反转=NO_LEADER_OVERSHOOT，阶梯实验买YES=YES_LADDER_EXPERIMENT，"
            "hold/sell=POSITION_MANAGEMENT。禁止用 YES_CONVERGENCE 买NO，禁止用 NO_EXCLUSION 买YES。"
            "NO_LEADER_OVERSHOOT只能引用marketAlignment中当前marketLeader的市场。"
            "阶梯实验必须在同一个event内输出2或3个买YES action：市场不同、档位相邻、排序为rank 1到2或1到3，"
            "每腿5 shares，输入中的5-share可执行YES买价之和必须严格低于"
            f" {float(self.config.get('yesLadderMaxCombinedPrice', 0.99)):.2f}；不能只买一个腿，不能覆盖明显尾部档。"
            "阶梯不是确定盈利：只有当这些相邻档覆盖primary/plausible路径、每个档都有天气证据且组合价格有容错时才可试验；"
            "否则统一observe。实验仍受现金、盘口深度、单市场仓位和时间窗口限制。\n\n"
            "盘口价格不是噪声。它反映做市商、交易者、信息差、相邻温度档、流动性和风险偏好。marketConsensusAssessment 必须先"
            "解释市场最可能在定价什么，以及市场可能比你知道什么。档位判断决定买什么，价格只决定是否值得买：所有 buy 的"
            "priceAssessment 必须为 favorable，并用 priceRiskAssessment 解释赔率、判断误差和最坏情景。买入价格低于 0.50 的一侧"
            "视为与市场定价明显分歧：只有当你拥有强证据时才能请求 buy。强证据必须包含至少三条可观测事实、至少两种"
            "newInformationTypes，并同时说明 whyMarketMayBeRight"
            "和 whyMarketMayBeWrong。单纯的模型中值差异、价格便宜或主观直觉不构成市场分歧证据。加仓还必须说明相对上次决策"
            "出现了什么新的信息优势；若没有新证据，只能 hold、observe 或 sell。你不能声称知道某个做市商的真实心理或动机；"
            "只能根据价格分布、价差、历史变化、成交量和流动性提出并明确标记可检验的市场定价假设。\n\n"
            "action.marketImpliedProbability 必须填写所选 outcomeSide 当前5-share可执行买价对应的概率，而不是归一化后的全盘口概率；"
            "本地安全内核会用真实订单簿复核。\n\n"
            "buy/sell/hold 必须引用输入中的 marketId，并明确 outcomeSide=YES 或 NO；observe 的 marketId、outcomeSide 和 shares"
            "应为 null。action.probabilityBand 必须与所选温度档的粗区间一致；不要另行输出精确胜率。shares 由你在安全上限内决定。已有仓位未被显式卖出"
            "时会继续持有。同一 market 同时只能持有一个方向；换向必须在 actions 中先 sell 后 buy。每个 eventId 必须输出一个 cycle。"
            "重点解释：新 METAR 改变了什么、当天升温过程是否仍有能力触及某个结算档、模型更新是否包含新信息、"
            "未来天气状态如何演变、盘口是否已经反映这些信息、市场为何可能是对的、什么新证据会推翻当前判断。\n\n"
            "历史 lessons 只是经验，不是可直接复制的规则。样本不足时保持克制，禁止根据一两次结果继续微调阈值。"
            "系统安全内核会复核档位资格、粗区间价格上限、超仓位、流动性、方向一致性和盘口时效，但不会替你做天气判断。\n\n"
            f"卖出属于降低风险，可以一次卖出完整现有仓位；但5-share可执行卖价小于或等于"
            f" {float(self.config.get('minSellPriceExclusive', 0.01)):.3f} 时不要卖，残值太小，应持有等待结算。\n\n"
            f"安全边界：paperOnly={self.config.get('paperOnly', True)}；初始现金 {self.initial_cash_usdc():.2f} USDC；"
            f"每次 buy 至少 {self.config['minSharesPerBuy']} shares、最多 {self.config['maxSharesPerAction']} shares；"
            f"单市场最多 {self.config['maxSharesPerMarket']} shares；不能使用超过可用现金的资金。\n\n"
            "输入：\n" + json.dumps(prompt_contexts, ensure_ascii=False, separators=(",", ":"), default=str)
        )
        return self._run_ai(DECISION_SCHEMA_PATH, prompt)

    @staticmethod
    def _decision_prompt_context(context: dict[str, Any]) -> dict[str, Any]:
        compact = json.loads(json.dumps(context, ensure_ascii=False, default=str))
        for market in compact.get("markets") or []:
            market.pop("yesBook", None)
            market.pop("noBook", None)
            history = market.get("recentPriceHistory")
            if isinstance(history, list):
                market["recentPriceHistory"] = history[-3:]
        meteoblue = (compact.get("weather") or {}).get("meteoblue")
        if isinstance(meteoblue, dict):
            # modelUpdates.meteoblue.futureHourlyProcess is the normalized subset
            # used for scenario analysis, so the larger duplicate is unnecessary.
            meteoblue.pop("hourly", None)
        return compact

    def _market_from_context(self, context: dict[str, Any], market_id: str) -> dict[str, Any] | None:
        return next((row for row in context["markets"] if str(row["marketId"]) == str(market_id)), None)

    def _latest_market_for_execution(
        self, context: dict[str, Any], market_id: str, execution_now: datetime
    ) -> dict[str, Any] | None:
        analysis_market = self._market_from_context(context, market_id)
        if not self.config.get("refreshMarketBeforeExecution", True):
            return analysis_market
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(market_snapshots)")}
        fetched_column = "ms.fetched_at_utc" if "fetched_at_utc" in columns else "NULL"
        row = self.db.execute(
            f"""
            SELECT m.market_id,m.outcome_range,m.bucket_low,m.bucket_high,m.bucket_unit,
                   ms.slot_utc,{fetched_column} AS fetched_at_utc,
                   ms.yes_best_bid,ms.yes_best_ask,ms.no_best_bid,ms.no_best_ask,
                   ms.yes_book_json,ms.no_book_json,ms.market_volume_24h,ms.market_liquidity
            FROM market_snapshots ms JOIN markets m ON m.market_id=ms.market_id
            WHERE ms.event_id=? AND ms.market_id=? AND ms.slot_utc<=?
            ORDER BY ms.slot_utc DESC LIMIT 1
            """,
            (context["event"]["event_id"], market_id, iso_utc(execution_now)),
        ).fetchone()
        if not row:
            return None
        return {
            "marketId": row["market_id"], "outcomeRange": row["outcome_range"],
            "bucketLow": row["bucket_low"], "bucketHigh": row["bucket_high"],
            "bucketUnit": row["bucket_unit"], "snapshotUtc": row["slot_utc"],
            "fetchedAtUtc": row["fetched_at_utc"],
            "yesBestBid": row["yes_best_bid"], "yesBestAsk": row["yes_best_ask"],
            "noBestBid": row["no_best_bid"], "noBestAsk": row["no_best_ask"],
            "volume24h": row["market_volume_24h"], "liquidity": row["market_liquidity"],
            "yesBook": json_value(row["yes_book_json"], {}),
            "noBook": json_value(row["no_book_json"], {}),
        }

    @staticmethod
    def _alignment_signature(alignment: dict[str, Any]) -> tuple[Any, ...]:
        candidates = tuple(sorted(
            (
                str(item.get("marketId") or ""),
                str(item.get("outcomeSide") or "").upper(),
                str(item.get("entryType") or ""),
            )
            for item in alignment.get("candidates") or []
            if isinstance(item, dict)
        ))
        leader = alignment.get("marketLeader") or {}
        return (
            str(alignment.get("mode") or ""),
            str(alignment.get("weatherAlignment") or ""),
            bool(alignment.get("marketPrematureConvergence")),
            bool(alignment.get("activeHeating")),
            str(leader.get("marketId") or ""),
            alignment.get("ridgePrimaryBucketC"),
            alignment.get("ridgeWarmTailBucketC"),
            candidates,
        )

    def _follow_aligned_leader_no_rejection(
        self, action: dict[str, Any], context: dict[str, Any]
    ) -> str | None:
        if not self.config.get("blockFollowAlignedLeaderNo", True):
            return None
        if (
            str(action.get("action") or "") != "buy"
            or str(action.get("outcomeSide") or "").upper() != "NO"
        ):
            return None
        alignment = context.get("marketAlignment") or {}
        leader = alignment.get("marketLeader") or {}
        if (
            str(alignment.get("mode") or "") == "FOLLOW"
            and str(alignment.get("weatherAlignment") or "") == "aligned"
            and not bool(alignment.get("marketPrematureConvergence"))
            and str(leader.get("marketId") or "") == str(action.get("marketId") or "")
        ):
            return (
                "NO buy on the market-leading bucket is forbidden while alignment is "
                "FOLLOW+aligned without premature convergence"
            )
        return None

    def _execution_weather_context(
        self, context: dict[str, Any], execution_now: datetime
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        event = context["event"]
        latest_metar = self._latest_metar_for_event(event, execution_now)
        if not latest_metar:
            raise RuntimeError("no current METAR is available for execution revalidation")
        process = self.weather_process_state(event, execution_now)
        state_version = str(
            process.get("snapshotSlotUtc")
            or latest_metar.get("observation_time_utc")
            or iso_utc(execution_now)
        )
        ridge = self.ridge_v2.snapshot(event, execution_now, state_version, process)
        markets = self.market_states(event, execution_now)
        if not markets:
            raise RuntimeError("no current market set is available for weather revalidation")
        alignment = market_alignment(markets, ridge, process, self.config)

        refreshed = dict(context)
        refreshed["metar"] = {
            **(context.get("metar") or {}),
            "current": latest_metar,
        }
        refreshed["weatherProcess"] = process
        refreshed["ridgeV2"] = ridge
        refreshed["markets"] = markets
        refreshed["marketAlignment"] = alignment

        analysis_metar = (
            ((context.get("metar") or {}).get("current") or {}).get("observation_time_utc")
            or (context.get("trigger") or {}).get("observationTimeUtc")
        )
        latest_metar_time = latest_metar.get("observation_time_utc")
        analysis_process_slot = (context.get("weatherProcess") or {}).get("snapshotSlotUtc")
        process_slot = process.get("snapshotSlotUtc")
        metadata = {
            "analysisMetarObservationTimeUtc": analysis_metar,
            "executionMetarObservationTimeUtc": latest_metar_time,
            "metarChanged": bool(
                parse_ts(latest_metar_time)
                and (
                    not parse_ts(analysis_metar)
                    or parse_ts(latest_metar_time) > parse_ts(analysis_metar)
                )
            ),
            "analysisWeatherProcessSlotUtc": analysis_process_slot,
            "executionWeatherProcessSlotUtc": process_slot,
            "processAdvanced": bool(
                parse_ts(process_slot)
                and (
                    not parse_ts(analysis_process_slot)
                    or parse_ts(process_slot) > parse_ts(analysis_process_slot)
                )
            ),
            "alignmentChanged": self._alignment_signature(alignment)
            != self._alignment_signature(context.get("marketAlignment") or {}),
            "alignment": alignment,
        }
        return refreshed, metadata

    def _position(self, event_id: str, market_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            """
            SELECT * FROM weather_ai_agent_positions
            WHERE strategy_name=? AND event_id=? AND market_id=?
            """,
            (self.strategy_name, event_id, market_id),
        ).fetchone()

    def _open_notional(self, city: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(cost_basis_usdc),0) FROM weather_ai_agent_positions WHERE strategy_name=? AND shares>0"
        args: list[Any] = [self.strategy_name]
        if city is not None:
            sql += " AND city=?"
            args.append(city)
        return float(self.db.execute(sql, args).fetchone()[0] or 0)

    def initial_cash_usdc(self) -> float:
        row = self.db.execute(
            "SELECT value FROM weather_ai_agent_meta WHERE key=?",
            (f"initial_cash_usdc:{self.strategy_name}",),
        ).fetchone()
        return float(row[0]) if row else float(self.config.get("initialCashUsdc", 20))

    def account_state(self) -> dict[str, float]:
        row = self.db.execute(
            """
            SELECT COALESCE(SUM(cost_basis_usdc),0) AS open_cost,
                   COALESCE(SUM(realized_pnl_usdc),0) AS realized_pnl
            FROM weather_ai_agent_positions WHERE strategy_name=?
            """,
            (self.strategy_name,),
        ).fetchone()
        initial = self.initial_cash_usdc()
        open_cost = float(row["open_cost"] or 0)
        realized = float(row["realized_pnl"] or 0)
        available = initial + realized - open_cost
        reserve = float(self.config.get("minCashReserveUsdc", 0))
        return {
            "initialCashUsdc": initial,
            "availableCashUsdc": available,
            "minimumCashReserveUsdc": reserve,
            "spendableCashUsdc": max(0.0, available - reserve),
            "openCostBasisUsdc": open_cost,
            "realizedPnlUsdc": realized,
            "maxOpenNotionalPerCityUsdc": float(self.config["maxOpenNotionalPerCity"]),
            "maxOpenNotionalTotalUsdc": float(self.config["maxOpenNotionalTotal"]),
        }

    def daily_pnl_snapshot(self, report_date: date, report_tz: ZoneInfo) -> dict[str, Any]:
        fills = self.db.execute(
            """
            SELECT side,fill_type,realized_pnl_usdc,filled_at_utc
            FROM weather_ai_agent_fills WHERE strategy_name=?
            """,
            (self.strategy_name,),
        ).fetchall()
        daily_fills = [
            row for row in fills
            if parse_ts(row["filled_at_utc"])
            and parse_ts(row["filled_at_utc"]).astimezone(report_tz).date() == report_date
        ]
        open_positions = self.db.execute(
            """
            SELECT * FROM weather_ai_agent_positions
            WHERE strategy_name=? AND shares>0 ORDER BY city,market_id
            """,
            (self.strategy_name,),
        ).fetchall()
        unrealized = 0.0
        unpriced = 0
        zero_bid = 0
        partial_depth = 0
        market_value = 0.0
        for position in open_positions:
            snapshot = self.db.execute(
                """
                SELECT yes_book_json,no_book_json FROM market_snapshots
                WHERE market_id=? ORDER BY slot_utc DESC LIMIT 1
                """,
                (position["market_id"],),
            ).fetchone()
            outcome_side = str(position["outcome_side"] or "NO").lower()
            book = snapshot[f"{outcome_side}_book_json"] if snapshot else None
            value, available = conservative_liquidation_value(book, float(position["shares"]))
            if value is None:
                unpriced += 1
                continue
            if available <= 1e-9:
                zero_bid += 1
            elif available < float(position["shares"]) - 1e-9:
                partial_depth += 1
            market_value += value
            unrealized += value - float(position["cost_basis_usdc"])
        account = self.account_state()
        cumulative_realized = account["realizedPnlUsdc"]
        total_pnl = cumulative_realized + unrealized if not unpriced else None
        equity = account["availableCashUsdc"] + market_value if not unpriced else None
        return {
            "reportDate": report_date.isoformat(),
            "initialCashUsdc": account["initialCashUsdc"],
            "availableCashUsdc": account["availableCashUsdc"],
            "dailyRealizedPnlUsdc": sum(float(row["realized_pnl_usdc"] or 0) for row in daily_fills),
            "cumulativeRealizedPnlUsdc": cumulative_realized,
            "unrealizedPnlUsdc": unrealized if not unpriced else None,
            "totalPnlUsdc": total_pnl,
            "equityUsdc": equity,
            "openCostBasisUsdc": account["openCostBasisUsdc"],
            "openPositions": len(open_positions),
            "unpricedPositions": unpriced,
            "zeroBidPositions": zero_bid,
            "partialDepthPositions": partial_depth,
            "buysToday": sum(1 for row in daily_fills if row["side"] == "BUY"),
            "sellsToday": sum(1 for row in daily_fills if row["side"] == "SELL"),
            "settlementsToday": sum(1 for row in daily_fills if row["fill_type"] == "settlement"),
        }

    @staticmethod
    def _money(value: float | None) -> str:
        return "N/A" if value is None else f"{value:+.2f} USDC"

    def daily_report_text(self, snapshot: dict[str, Any]) -> str:
        return "\n".join(
            (
                f"天气 AI Paper 日报 · {snapshot['reportDate']}",
                f"当日已实现 PnL：{self._money(snapshot['dailyRealizedPnlUsdc'])}",
                f"累计已实现 PnL：{self._money(snapshot['cumulativeRealizedPnlUsdc'])}",
                f"未实现 PnL：{self._money(snapshot['unrealizedPnlUsdc'])}",
                f"账户总 PnL：{self._money(snapshot['totalPnlUsdc'])}",
                f"账户权益：{self._money(snapshot['equityUsdc'])}",
                f"可用现金：{snapshot['availableCashUsdc']:.2f} / 初始 {snapshot['initialCashUsdc']:.2f} USDC",
                f"持仓：{snapshot['openPositions']} 笔 · 成本 {snapshot['openCostBasisUsdc']:.2f} USDC",
                f"今日动作：买入 {snapshot['buysToday']} · 卖出 {snapshot['sellsToday']} · 结算 {snapshot['settlementsToday']}",
                (
                    f"估值提示：{snapshot['unpricedPositions']} 笔持仓缺少盘口快照，未实现和权益暂不计算"
                    if snapshot["unpricedPositions"] else (
                        f"保守估值：{snapshot['zeroBidPositions']} 笔无买盘按 0 计，"
                        f"{snapshot['partialDepthPositions']} 笔深度不足部分按 0 计"
                        if snapshot["zeroBidPositions"] or snapshot["partialDepthPositions"]
                        else "估值状态：全部持仓均有完整可成交深度"
                    )
                ),
                "模式：Hermes 自主组合管理 · Paper-only · 最低现金储备 "
                f"{float(self.config.get('minCashReserveUsdc', 0)):.2f} USDC",
            )
        )

    def _send_feishu_text(self, text: str) -> None:
        webhook_path = Path(str(self.config["feishuWebhookFile"])).expanduser()
        try:
            webhook = webhook_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Feishu webhook file unavailable: {webhook_path}") from exc
        if not webhook.startswith("https://open.feishu.cn/open-apis/bot/v2/hook/"):
            raise RuntimeError("Feishu webhook URL is invalid")
        payload = json.dumps(
            {"msg_type": "text", "content": {"text": text}}, ensure_ascii=False
        ).encode("utf-8")
        request = urllib.request.Request(
            webhook, data=payload, headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        if int(result.get("code", result.get("StatusCode", -1))) != 0:
            raise RuntimeError(f"Feishu webhook rejected report: {str(result)[:500]}")

    def maybe_send_daily_report(self, now: datetime, force: bool = False) -> dict[str, Any]:
        report_tz = ZoneInfo(str(self.config.get("dailyReportTimezone", "Asia/Shanghai")))
        local_now = now.astimezone(report_tz)
        scheduled = local_now.replace(
            hour=int(self.config.get("dailyReportHour", 19)),
            minute=int(self.config.get("dailyReportMinute", 0)),
            second=0,
            microsecond=0,
        )
        report_date = local_now.date()
        sent_key = f"daily_report_sent:{self.strategy_name}:{report_date.isoformat()}"
        if not force and local_now < scheduled:
            return {"status": "not_due", "report_date": report_date.isoformat()}
        if not force and self.db.execute(
            "SELECT 1 FROM weather_ai_agent_meta WHERE key=?", (sent_key,)
        ).fetchone():
            return {"status": "already_sent", "report_date": report_date.isoformat()}

        attempt_key = f"daily_report_attempt:{self.strategy_name}"
        last_attempt_row = self.db.execute(
            "SELECT value FROM weather_ai_agent_meta WHERE key=?", (attempt_key,)
        ).fetchone()
        last_attempt = parse_ts(last_attempt_row[0]) if last_attempt_row else None
        retry_seconds = int(self.config.get("dailyReportRetryMinutes", 10)) * 60
        if not force and last_attempt and (now - last_attempt).total_seconds() < retry_seconds:
            return {"status": "retry_wait", "report_date": report_date.isoformat()}
        self.db.execute(
            "INSERT OR REPLACE INTO weather_ai_agent_meta(key,value) VALUES(?,?)",
            (attempt_key, iso_utc(now)),
        )
        self.db.commit()
        snapshot = self.daily_pnl_snapshot(report_date, report_tz)
        self._send_feishu_text(self.daily_report_text(snapshot))
        self.db.execute(
            "INSERT OR REPLACE INTO weather_ai_agent_meta(key,value) VALUES(?,?)",
            (sent_key, iso_utc(now)),
        )
        self.db.commit()
        return {"status": "sent", "report_date": report_date.isoformat(), "snapshot": snapshot}

    def _apply_fill(
        self, action_id: int, context: dict[str, Any], market: dict[str, Any], trade_side: str,
        outcome_side: str, shares: float, price: float, fee: float,
    ) -> float:
        event = context["event"]
        position = self._position(event["event_id"], market["marketId"])
        old_shares = float(position["shares"] or 0) if position else 0.0
        old_cost = float(position["cost_basis_usdc"] or 0) if position else 0.0
        old_realized = float(position["realized_pnl_usdc"] or 0) if position else 0.0
        old_outcome_side = str(position["outcome_side"] or "NO").upper() if position else outcome_side
        if old_shares > 1e-9 and old_outcome_side != outcome_side:
            raise ValueError("cannot trade the opposite outcome while a position is open")
        realized = 0.0
        if trade_side == "BUY":
            new_shares = old_shares + shares
            new_cost = old_cost + shares * price + fee
        else:
            if old_shares <= 0 or shares > old_shares + 1e-9:
                raise ValueError("sell exceeds open shares")
            allocated_cost = old_cost * (shares / old_shares)
            realized = shares * price - fee - allocated_cost
            new_shares = max(0.0, old_shares - shares)
            new_cost = max(0.0, old_cost - allocated_cost)
        now = iso_utc()
        self.db.execute(
            """
            INSERT INTO weather_ai_agent_positions(
                strategy_name,event_id,market_id,city,outcome_range,outcome_side,shares,cost_basis_usdc,
                realized_pnl_usdc,status,opened_at_utc,updated_at_utc,closed_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(strategy_name,event_id,market_id) DO UPDATE SET
                outcome_side=excluded.outcome_side,shares=excluded.shares,cost_basis_usdc=excluded.cost_basis_usdc,
                realized_pnl_usdc=excluded.realized_pnl_usdc,status=excluded.status,
                updated_at_utc=excluded.updated_at_utc,closed_at_utc=excluded.closed_at_utc
            """,
            (
                self.strategy_name, event["event_id"], market["marketId"], event["city"],
                market["outcomeRange"], outcome_side, new_shares, new_cost, old_realized + realized,
                "open" if new_shares > 1e-9 else "closed",
                position["opened_at_utc"] if position and old_shares > 1e-9 else now,
                now, now if new_shares <= 1e-9 else None,
            ),
        )
        self.db.execute(
            """
            INSERT INTO weather_ai_agent_fills(
                strategy_name,action_id,event_id,market_id,city,fill_type,side,outcome_side,shares,price,notional_usdc,
                fee_usdc,realized_pnl_usdc,filled_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                self.strategy_name, action_id, event["event_id"], market["marketId"], event["city"], "paper",
                trade_side, outcome_side, shares, price, shares * price, fee, realized, now,
            ),
        )
        return realized

    def _validate_cycle_reasoning(self, decision: dict[str, Any], context: dict[str, Any] | None = None) -> None:
        mode = str(decision.get("marketDecisionMode") or "")
        if self.config.get("autonomousMode", False):
            if mode != "AUTONOMOUS":
                raise RuntimeError("autonomous mode requires marketDecisionMode=AUTONOMOUS")
            return
        no_only = self.config.get("noOnlyPaperMode", False)
        if no_only and mode != "NO_RESEARCH":
            raise RuntimeError("NO-only paper mode requires marketDecisionMode=NO_RESEARCH")
        if not no_only and mode not in {"FOLLOW", "WATCH", "FADE", "NO_EDGE"}:
            raise RuntimeError("marketDecisionMode must be FOLLOW, WATCH, FADE, or NO_EDGE")
        if context is not None and not no_only:
            expected_mode = str((context.get("marketAlignment") or {}).get("mode") or "")
            if expected_mode and mode != expected_mode:
                raise RuntimeError(
                    f"marketDecisionMode {mode} does not match deterministic market alignment {expected_mode}"
                )
        scenarios = decision.get("futureScenarios") or []
        distribution = decision.get("settlementDistribution") or []
        primary_count = sum(
            1 for row in scenarios if isinstance(row, dict) and row.get("plausibility") == "primary"
        )
        if primary_count != 1:
            raise RuntimeError(f"futureScenarios must contain exactly one primary scenario, got {primary_count}")
        ranges = [str(row.get("outcomeRange")) for row in distribution if isinstance(row, dict)]
        if len(ranges) != len(set(ranges)):
            raise RuntimeError("settlementDistribution outcomeRange values must be unique")
        if context is not None:
            expected_ranges = {str(row.get("outcomeRange")) for row in context.get("markets") or []}
            if set(ranges) != expected_ranges:
                missing = sorted(expected_ranges - set(ranges))
                extra = sorted(set(ranges) - expected_ranges)
                raise RuntimeError(
                    f"settlementDistribution must cover every market outcome; missing={missing}, extra={extra}"
                )
        ranks = [int(row.get("rank")) for row in distribution if isinstance(row, dict)]
        if sorted(ranks) != list(range(1, len(distribution) + 1)):
            raise RuntimeError("settlementDistribution ranks must be unique and contiguous from 1")
        leaders = [row for row in distribution if row.get("classification") == "clear_leader"]
        if len(leaders) > 1 or (leaders and int(leaders[0].get("rank")) != 1):
            raise RuntimeError("settlementDistribution may have only one clear_leader and it must rank first")
        if not leaders:
            contenders = [row for row in distribution if row.get("classification") == "contender"]
            if len(contenders) < 2 or min(int(row.get("rank")) for row in contenders) != 1:
                raise RuntimeError("a distribution without a clear leader must identify at least two contenders including rank 1")
        for row in distribution:
            classification, band = row.get("classification"), row.get("probabilityBand")
            if classification == "excluded" and band != "lt_5":
                raise RuntimeError("excluded settlement buckets must use probabilityBand=lt_5")
            if classification == "highly_unlikely" and band not in {"lt_5", "5_15"}:
                raise RuntimeError("highly_unlikely settlement buckets must use probabilityBand lt_5 or 5_15")
        for scenario in scenarios:
            low, high = as_float(scenario.get("peakLowC")), as_float(scenario.get("peakHighC"))
            if low is None or high is None or low > high:
                raise RuntimeError("futureScenarios peakLowC must not exceed peakHighC")
        if no_only:
            markets_by_id = {
                str(row.get("marketId")): row
                for row in (context or {}).get("markets") or []
                if isinstance(row, dict)
            }
            for action in decision.get("actions") or []:
                requested = str(action.get("action") or "")
                if requested == "observe":
                    if action.get("entryType") != "NONE" or any(
                        action.get(key) is not None for key in ("marketId", "outcomeSide", "shares")
                    ):
                        raise RuntimeError("NO-only observe actions must use NONE with null market, side, and shares")
                    if action.get("sizingTier") != "NOT_APPLICABLE":
                        raise RuntimeError("NO-only observe actions require sizingTier=NOT_APPLICABLE")
                    continue
                if requested != "buy":
                    raise RuntimeError("NO-only paper mode permits only observe or buy actions")
                if str(action.get("outcomeSide") or "").upper() != "NO":
                    raise RuntimeError("NO-only paper mode permits only NO purchases")
                if str(action.get("entryType") or "") not in NO_PAPER_ENTRY_TYPES:
                    raise RuntimeError("NO-only buy must use one of the three research entry types")
                market = markets_by_id.get(str(action.get("marketId") or ""))
                if market is None:
                    raise RuntimeError("NO-only buy marketId is not present in the analysis snapshot")
                analysis_price = as_float(market.get("noExecutableBuyPrice5"))
                if analysis_price is None:
                    analysis_price, _available = executable_vwap(
                        market.get("noBook"), float(self.config.get("shares", 5)), "asks"
                    )
                if analysis_price is None:
                    raise RuntimeError("NO-only buy has no executable 5-share NO analysis price")
                if reason := self._market_disagreement_rejection(action, float(analysis_price)):
                    raise RuntimeError(reason)
                position = None
                if context is not None and action.get("marketId"):
                    position = self._position(
                        context["event"]["event_id"], str(action.get("marketId"))
                    )
                if reason := self._no_sizing_rejection(action, position):
                    raise RuntimeError(reason)
                if context is not None and (
                    reason := self._follow_aligned_leader_no_rejection(action, context)
                ):
                    raise RuntimeError(reason)

    def _market_disagreement_rejection(self, action: dict[str, Any], price: float) -> str | None:
        if self.config.get("autonomousMode", False):
            return None
        implied = as_float(action.get("marketImpliedProbability"))
        if implied is None or abs(implied - price) > 0.05:
            return "marketImpliedProbability must match the executable side price within 5c"
        if price >= 0.5:
            return None
        if action.get("consensusPosition") != "against_consensus":
            return "buying a sub-50c side must be labeled against_consensus"
        if action.get("evidenceStrength") != "strong":
            return "market-disagreement buy requires strong evidence"
        evidence = [str(item).strip() for item in action.get("disagreementEvidence") or [] if str(item).strip()]
        minimum_evidence = int(self.config.get("minMarketDisagreementEvidenceItems", 3))
        if len(evidence) < minimum_evidence:
            return f"market-disagreement buy requires at least {minimum_evidence} evidence items"
        information = {str(item) for item in action.get("newInformationTypes") or []}
        minimum_types = int(self.config.get("minMarketDisagreementInformationTypes", 2))
        if len(information) < minimum_types:
            return f"market-disagreement buy requires at least {minimum_types} new-information types"
        weather_types = {"metar_model_divergence", "weather_regime_change", "forecast_revision", "station_mechanism"}
        if not information.intersection(weather_types):
            return "market-disagreement buy requires weather-process evidence, not price alone"
        if not str(action.get("whyMarketMayBeRight") or "").strip() or not str(action.get("whyMarketMayBeWrong") or "").strip():
            return "market-disagreement buy must explain why the market may be right and wrong"
        return None

    def _decision_context_expiry_reason(
        self, contexts: list[dict[str, Any]], completed_at: datetime | None = None,
    ) -> str | None:
        """Reject AI output whose executable market snapshot aged out while reasoning."""
        checked_at = completed_at or utc_now()
        max_age = float(self.config.get(
            "decisionContextMaxAgeMinutes",
            self.config.get("maxMarketDataAgeMinutes", 10),
        ))
        expired: list[str] = []
        for context in contexts:
            snapshots = [
                parse_ts(row.get("snapshotUtc"))
                for row in context.get("markets") or []
                if isinstance(row, dict) and row.get("snapshotUtc")
            ]
            snapshots = [value for value in snapshots if value is not None]
            if not snapshots:
                continue
            data_time = max(snapshots)
            age = max(0.0, (checked_at - data_time).total_seconds() / 60.0)
            if age > max_age:
                expired.append(
                    f"{context['event']['city']} data={iso_utc(data_time)} age={age:.1f}m"
                )
        if not expired:
            return None
        return (
            f"decision context expired (limit={max_age:g}m); latest data reanalysis required: "
            + "; ".join(expired)
        )

    def _no_sizing_rejection(
        self, action: dict[str, Any], position: sqlite3.Row | None
    ) -> str | None:
        tier = str(action.get("sizingTier") or "")
        if tier not in {"BASE", "STRONG"}:
            return "NO-only buy requires sizingTier=BASE or STRONG"
        base_target = float(self.config.get("shares", 5))
        strong_target = float(self.config.get("strongNoShares", 10))
        current_shares = float(position["shares"] or 0) if position else 0.0
        target_shares = base_target if tier == "BASE" else strong_target
        if tier == "BASE" and current_shares > 1e-9:
            return "BASE sizing cannot add to an existing position"
        if current_shares >= target_shares - 1e-9:
            return f"{tier} target position is already filled"
        requested_shares = float(action.get("shares") or 0)
        required_shares = target_shares - current_shares
        if abs(requested_shares - required_shares) > 1e-9:
            return (
                f"{tier} sizing must request {required_shares:g} shares to reach the "
                f"{target_shares:g}-share target"
            )
        if current_shares > 1e-9 and not [
            str(item).strip()
            for item in action.get("newEvidenceSincePrior") or []
            if str(item).strip()
        ]:
            return "STRONG sizing upgrade requires new observable evidence since the BASE entry"
        return None

    @staticmethod
    def _no_win_probability_lower_bound(action: dict[str, Any]) -> float | None:
        risk_band = str(action.get("probabilityBand") or "")
        return {
            "lt_5": 0.95,
            "5_15": 0.85,
            "15_30": 0.70,
            "30_50": 0.50,
        }.get(risk_band)

    def _buy_reasoning_rejection(
        self,
        action: dict[str, Any],
        decision: dict[str, Any],
        context: dict[str, Any],
        market: dict[str, Any],
        position: sqlite3.Row | None,
    ) -> str | None:
        if self.config.get("autonomousMode", False):
            return None
        assessment = str(action.get("exactBucketRiskAssessment") or "").strip()
        if not assessment:
            return "buy requires an exact-bucket final-settlement risk assessment"
        distribution = {
            str(row.get("outcomeRange")): row
            for row in decision.get("settlementDistribution") or []
            if isinstance(row, dict)
        }
        bucket = distribution.get(str(market.get("outcomeRange")))
        if bucket is None:
            return "buy must map to the selected settlement-distribution bucket"
        classification = str(bucket.get("classification") or "")
        band = str(bucket.get("probabilityBand") or "")
        if action.get("outcomeAssessment") != classification:
            return "outcomeAssessment is inconsistent with the selected settlement bucket"
        if action.get("probabilityBand") != band:
            return "probabilityBand is inconsistent with the selected settlement bucket"
        if action.get("priceAssessment") != "favorable":
            return "buy requires priceAssessment=favorable"
        if not str(action.get("priceRiskAssessment") or "").strip():
            return "buy requires a price-risk assessment"

        outcome_side = str(action.get("outcomeSide") or "").upper()
        entry_type = str(action.get("entryType") or "")
        if reason := self._follow_aligned_leader_no_rejection(action, context):
            return reason
        if self.config.get("noOnlyPaperMode", False):
            if outcome_side != "NO":
                return "NO-only paper mode permits only NO purchases"
            if entry_type not in NO_PAPER_ENTRY_TYPES:
                return "NO-only buy requires NO_OVERSHOOT, NO_CEILING, or NO_MARKET_TAIL_REJECTION"
            if reason := self._no_sizing_rejection(action, position):
                return reason
            if entry_type == "NO_OVERSHOOT":
                valid_bucket = classification in {"excluded", "highly_unlikely"} or (
                    classification in {"clear_leader", "contender", "plausible"}
                    and band in {"lt_5", "5_15", "15_30", "30_50"}
                )
                if not valid_bucket:
                    return "NO_OVERSHOOT requires a target bucket with a usable risk band no higher than 30_50"
                if str(action.get("heatingProcessStatus") or "") != "active":
                    return "NO_OVERSHOOT requires active heating"
                if action.get("evidenceStrength") != "strong":
                    return "NO_OVERSHOOT requires strong weather-process evidence"
            elif classification not in {"excluded", "highly_unlikely"}:
                return f"{entry_type} requires an excluded or highly_unlikely settlement bucket"
            if entry_type == "NO_MARKET_TAIL_REJECTION":
                information = {str(item) for item in action.get("newInformationTypes") or []}
                if len(information) < 2:
                    return "NO_MARKET_TAIL_REJECTION requires at least two independent information types"
            if str(action.get("sizingTier") or "") == "STRONG":
                evidence = [
                    str(item).strip() for item in action.get("disagreementEvidence") or []
                    if str(item).strip()
                ]
                information = {str(item) for item in action.get("newInformationTypes") or []}
                min_evidence = int(self.config.get("strongNoMinEvidenceItems", 4))
                min_information = int(self.config.get("strongNoMinInformationTypes", 3))
                if action.get("evidenceStrength") != "strong":
                    return "STRONG sizing requires strong evidence"
                if len(evidence) < min_evidence:
                    return f"STRONG sizing requires at least {min_evidence} disagreement evidence items"
                if len(information) < min_information:
                    return f"STRONG sizing requires at least {min_information} independent information types"
            return None
        if outcome_side == "YES" and entry_type not in {"YES_CONVERGENCE", "YES_LADDER_EXPERIMENT"}:
            return "YES buy requires entryType=YES_CONVERGENCE or YES_LADDER_EXPERIMENT"
        if outcome_side == "NO" and entry_type not in {"NO_EXCLUSION", "NO_LEADER_OVERSHOOT"}:
            return "NO buy requires entryType=NO_EXCLUSION or NO_LEADER_OVERSHOOT"
        alignment = context.get("marketAlignment") or {}
        if self.config.get("marketAlignmentEnforced", True) and alignment:
            mode = str(alignment.get("mode") or "NO_EDGE")
            if mode in {"WATCH", "NO_EDGE"}:
                return f"new buy rejected while deterministic market alignment is {mode}"
            candidates = alignment.get("candidates") or []
            candidate_match = any(
                str(item.get("marketId")) == str(action.get("marketId"))
                and str(item.get("outcomeSide") or "").upper() == outcome_side
                and str(item.get("entryType") or "") == entry_type
                for item in candidates if isinstance(item, dict)
            )
            if not candidate_match:
                return "buy is not one of the deterministic Ridge V2 market candidates"
        if outcome_side == "YES":
            if entry_type == "YES_CONVERGENCE" and (
                classification != "clear_leader" or int(bucket.get("rank") or 0) != 1
            ):
                return "YES buy requires the selected bucket to be the clear rank-1 settlement leader"
            if entry_type == "YES_LADDER_EXPERIMENT" and (
                classification not in {"clear_leader", "contender", "plausible"}
                or int(bucket.get("rank") or 0) < 1
            ):
                return "YES ladder leg must be a ranked plausible bucket"
            heating = str(action.get("heatingProcessStatus") or "unclear")
            weather_types = {"metar_model_divergence", "weather_regime_change", "forecast_revision", "station_mechanism"}
            information = {str(item) for item in action.get("newInformationTypes") or []}
            if heating == "active" and (
                action.get("evidenceStrength") != "strong" or not information.intersection(weather_types)
            ):
                return "active-heating YES buy requires strong weather-process evidence"
        elif entry_type == "NO_EXCLUSION" and classification not in {"excluded", "highly_unlikely"}:
            return "NO_EXCLUSION buy requires an excluded or highly_unlikely settlement bucket"
        elif entry_type == "NO_LEADER_OVERSHOOT":
            if str(alignment.get("mode") or "") != "FADE":
                return "NO_LEADER_OVERSHOOT requires deterministic FADE alignment"
            leader = alignment.get("marketLeader") or {}
            if str(leader.get("marketId")) != str(action.get("marketId")):
                return "NO_LEADER_OVERSHOOT must select the current market leader"
            if not alignment.get("marketPrematureConvergence"):
                return "NO_LEADER_OVERSHOOT requires market premature-convergence evidence"
            bucket_low = as_float(market.get("bucketLow"))
            observed_values = [
                as_float(((context.get("metar") or {}).get("current") or {}).get("temperature_c")),
                as_float(((context.get("metar") or {}).get("current") or {}).get("observed_daily_max_c")),
            ]
            observed_max = max((value for value in observed_values if value is not None), default=None)
            if bucket_low is None or observed_max is None or observed_max < bucket_low - 1.0:
                return "NO_LEADER_OVERSHOOT requires the market leader bucket to be touched or within 1C"
            ridge = context.get("ridgeV2") or {}
            if ridge.get("status") != "ok" or int(ridge.get("warmTailBucketC") or -999) <= int(leader.get("bucketC") or 999):
                return "NO_LEADER_OVERSHOOT requires a supported upper Ridge V2 path"
            if not alignment.get("activeHeating"):
                return "NO_LEADER_OVERSHOOT requires active heating"
            if classification == "clear_leader":
                return "NO_LEADER_OVERSHOOT cannot be used while AI still calls the target a clear leader"
            if str(action.get("probabilityBand") or "") not in {"lt_5", "5_15", "15_30"}:
                return "NO_LEADER_OVERSHOOT requires a non-leader risk band below 30%"

        current_shares = float(position["shares"] or 0) if position else 0.0
        new_evidence = [str(item).strip() for item in action.get("newEvidenceSincePrior") or [] if str(item).strip()]
        if current_shares > 1e-9 and not new_evidence:
            return "adding requires new observable evidence since the prior decision"
        return None

    def _price_band_rejection(self, action: dict[str, Any], price: float) -> str | None:
        """Use only coarse probability bands to preserve a margin for estimation error."""
        if self.config.get("autonomousMode", False):
            return None
        band = str(action.get("probabilityBand") or "")
        side = str(action.get("outcomeSide") or "").upper()
        uncertainty_buffer = float(self.config.get("coarseProbabilityPriceBuffer", 0.03))
        if side == "YES":
            lower_bounds = {"lt_5": 0.0, "5_15": 0.05, "15_30": 0.15, "30_50": 0.30, "gt_50": 0.50}
            cap = max(0.0, lower_bounds.get(band, 0.0) - uncertainty_buffer)
        elif side == "NO":
            allowed_bands = {"lt_5", "5_15"}
            if str(action.get("entryType") or "") in {"NO_LEADER_OVERSHOOT", "NO_OVERSHOOT"}:
                allowed_bands.update({"15_30", "30_50"})
            lower_bound = self._no_win_probability_lower_bound(action)
            if band not in allowed_bands or lower_bound is None:
                return "NO buy requires a usable conservative NO probability lower bound"
            if self.config.get("noOnlyPaperMode", False):
                tier = str(action.get("sizingTier") or "")
                required_edge = (
                    float(self.config.get("noStrongEdgeMargin", 0.25))
                    if tier == "STRONG"
                    else float(self.config.get("noBaseEdgeMargin", 0.10))
                )
                cap = lower_bound - required_edge
            else:
                cap = lower_bound - uncertainty_buffer
            cap = min(cap, float(self.config.get("maxNoBuyPriceExclusive", 0.92)))
        else:
            return "buy outcomeSide must be YES or NO"
        if price >= cap - 1e-9:
            return f"executable price {price:.3f} is not below coarse-band safety cap {cap:.3f}"
        return None

    def _target_bucket_touched(self, context: dict[str, Any], market: dict[str, Any]) -> bool:
        bucket_low = as_float(market.get("bucketLow"))
        if bucket_low is None:
            return False
        metar = (context.get("metar") or {}).get("current") or {}
        temperatures = [
            as_float(metar.get("temperature_c")),
            as_float(metar.get("observed_daily_max_c")),
            as_float((context.get("weather") or {}).get("metar", {}).get("temperatureC")),
        ]
        observed_max = max((value for value in temperatures if value is not None), default=None)
        return observed_max is not None and observed_max >= bucket_low - 1e-9

    def _sell_state_rejection(
        self,
        action: dict[str, Any],
        decision: dict[str, Any] | None,
        context: dict[str, Any],
        market: dict[str, Any],
    ) -> str | None:
        if not self.config.get("enforcePositionStateExit", True) or not decision:
            return None
        distribution = {
            str(row.get("outcomeRange")): row
            for row in decision.get("settlementDistribution") or []
            if isinstance(row, dict)
        }
        bucket = distribution.get(str(market.get("outcomeRange")))
        if bucket is None:
            return None
        classification = str(bucket.get("classification") or "")
        touched = self._target_bucket_touched(context, market)
        heating_active = str(action.get("heatingProcessStatus") or "") == "active"
        if classification == "clear_leader" or (touched and heating_active):
            return None
        state = (
            "PROTECTED" if classification in {"excluded", "highly_unlikely"}
            else "WATCH" if classification in {"contender", "plausible"}
            else classification or "UNKNOWN"
        )
        return (
            f"position state is {state}; sell requires INVALID state "
            "(target is clear_leader or observed target bucket has been touched while heating is active)"
        )

    def _ladder_preflight(
        self, context: dict[str, Any], decision: dict[str, Any], actions: list[dict[str, Any]]
    ) -> str | None:
        ladder = [
            action for action in actions
            if str(action.get("action") or "") == "buy"
            and str(action.get("entryType") or "") == "YES_LADDER_EXPERIMENT"
        ]
        if not ladder:
            return None
        if not self.config.get("experimentalYesLadderEnabled", False):
            return "YES ladder experiment is disabled"
        min_legs = int(self.config.get("yesLadderMinLegs", 2))
        max_legs = int(self.config.get("yesLadderMaxLegs", 3))
        if not min_legs <= len(ladder) <= max_legs:
            return f"YES ladder requires {min_legs}-{max_legs} legs in one event"
        if any(
            str(action.get("action") or "") == "buy"
            and str(action.get("entryType") or "") != "YES_LADDER_EXPERIMENT"
            for action in actions
        ):
            return "YES ladder must be the only buy group in the event cycle"
        market_ids = [str(action.get("marketId") or "") for action in ladder]
        if not all(market_ids) or len(set(market_ids)) != len(market_ids):
            return "YES ladder legs must reference distinct markets"
        if any(str(action.get("outcomeSide") or "").upper() != "YES" for action in ladder):
            return "YES ladder legs must all be YES"
        expected_shares = float(self.config.get("shares", 5))
        if any(abs(float(action.get("shares") or 0) - expected_shares) > 1e-9 for action in ladder):
            return f"YES ladder requires exactly {expected_shares:g} shares per leg"
        distribution = {
            str(row.get("outcomeRange")): row
            for row in decision.get("settlementDistribution") or []
            if isinstance(row, dict)
        }
        selected: list[tuple[int, float, dict[str, Any], dict[str, Any]]] = []
        for action in ladder:
            market = self._market_from_context(context, str(action.get("marketId")))
            if market is None:
                return "YES ladder market is missing from the event snapshot"
            if self.config.get("realtimeExecutionGuards", True):
                market = self._latest_market_for_execution(context, str(action.get("marketId")), utc_now())
            if market is None:
                return "YES ladder has no current execution snapshot"
            position = self._position(context["event"]["event_id"], str(action.get("marketId")))
            if position and float(position["shares"] or 0) > 1e-9:
                return "YES ladder cannot add to an existing market position"
            bucket = distribution.get(str(market.get("outcomeRange")))
            if bucket is None:
                return "YES ladder leg is missing from settlementDistribution"
            rank = int(bucket.get("rank") or 0)
            if rank < 1 or rank > len(ladder) or str(bucket.get("classification")) not in {
                "clear_leader", "contender", "plausible"
            }:
                return "YES ladder legs must be the top adjacent plausible buckets"
            price, available = executable_vwap(
                market.get("yesBook"), float(action.get("shares") or self.config.get("shares", 5)), "asks"
            )
            if price is None or available < float(action.get("shares") or self.config.get("shares", 5)) - 1e-9:
                return "YES ladder requires a complete 5-share YES ask on every leg"
            if (reason := self._buy_reasoning_rejection(action, decision, context, market, position)):
                return reason
            if (reason := self._market_disagreement_rejection(action, float(price))):
                return reason
            selected.append((rank, float(price), market, bucket))
        ranks = sorted(item[0] for item in selected)
        if ranks != list(range(1, len(ladder) + 1)):
            return "YES ladder must cover consecutive ranks starting at rank 1"
        lows = sorted(
            as_float(item[2].get("bucketLow"))
            for item in selected
            if as_float(item[2].get("bucketLow")) is not None
        )
        if len(lows) != len(selected) or any(abs(lows[index + 1] - lows[index] - 1.0) > 0.01 for index in range(len(lows) - 1)):
            return "YES ladder legs must be adjacent temperature buckets"
        total_price = sum(item[1] for item in selected)
        cap = float(self.config.get("yesLadderMaxCombinedPrice", 0.99))
        if total_price >= cap - 1e-9:
            return f"YES ladder combined executable price {total_price:.3f} must be below {cap:.3f}"
        total_notional = total_price * expected_shares
        fee_rate = float(self.config.get("feeRate", 0.0))
        total_proposed = total_notional * (1.0 + fee_rate)
        city = context["event"]["city"]
        if self._open_notional(city) + total_proposed > float(self.config["maxOpenNotionalPerCity"]) + 1e-9:
            return "YES ladder city open-notional limit exceeded"
        if self._open_notional() + total_proposed > float(self.config["maxOpenNotionalTotal"]) + 1e-9:
            return "YES ladder portfolio open-notional limit exceeded"
        account = self.account_state()
        if total_proposed > account["spendableCashUsdc"] + 1e-9:
            return "YES ladder insufficient available cash"
        return None

    def _opportunity_weather_hash(self, context: dict[str, Any], decision: dict[str, Any]) -> str:
        payload = {
            "trigger": context.get("trigger") or {},
            "observerGate": context.get("observerGate") or {},
            "metar": context.get("metar") or {},
            "distribution": decision.get("settlementDistribution") or [],
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()[:20]

    def persist_opportunity_evaluations(
        self,
        cycle_id: int,
        context: dict[str, Any],
        decision: dict[str, Any],
        action_records: list[tuple[dict[str, Any], int]],
    ) -> int:
        """Record both selected trades and skipped counterfactual opportunities."""
        event = context["event"]
        trigger = context.get("trigger") or {}
        decision_time = trigger.get("decisionTriggerTimeUtc") or trigger.get("slotUtc") or iso_utc()
        observer_event_id = trigger.get("observerEventId")
        distribution = {
            str(row.get("outcomeRange")): row
            for row in decision.get("settlementDistribution") or []
            if isinstance(row, dict)
        }
        action_lookup: dict[tuple[str, str], tuple[int, str, str]] = {}
        for action, action_id in action_records:
            key = (str(action.get("marketId") or ""), str(action.get("outcomeSide") or "").upper())
            row = self.db.execute(
                "SELECT requested_action,executed_action FROM weather_ai_agent_actions WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if row and key[0] and key[1]:
                action_lookup[key] = (action_id, str(row["requested_action"]), str(row["executed_action"]))
        weather_hash = self._opportunity_weather_hash(context, decision)
        created = iso_utc()
        rows_to_insert: list[dict[str, Any]] = []
        markets = context.get("markets") or []
        for market in markets:
            market_id = str(market.get("marketId") or "")
            outcome_range = str(market.get("outcomeRange") or "")
            bucket = distribution.get(outcome_range) or {}
            classification = bucket.get("classification")
            rank = bucket.get("rank")
            band = bucket.get("probabilityBand")
            for side in ("YES", "NO"):
                price = as_float(market.get(f"{side.lower()}ExecutableBuyPrice5"))
                action_info = action_lookup.get((market_id, side))
                deterministic_candidate = next(
                    (
                        item for item in (context.get("marketAlignment") or {}).get("candidates") or []
                        if str(item.get("marketId")) == market_id
                        and str(item.get("outcomeSide") or "").upper() == side
                    ), None,
                )
                if action_info:
                    action_id, _requested, executed = action_info
                    status = "EXECUTED" if executed == "buy" else "REQUESTED_REJECTED"
                    if str(action_records[0][0].get("entryType") or "") == "YES_LADDER_EXPERIMENT" and side == "YES":
                        status = "LADDER_LEG_EXECUTED" if executed == "buy" else "LADDER_LEG_REJECTED"
                elif side == "YES" and classification == "clear_leader" and int(rank or 0) == 1:
                    action_id, status = None, "YES_ELIGIBLE_SKIPPED"
                elif side == "NO" and classification in {"excluded", "highly_unlikely"}:
                    action_id, status = None, "NO_ELIGIBLE_SKIPPED"
                elif deterministic_candidate:
                    action_id, status = None, "CANDIDATE_AI_SKIPPED"
                else:
                    action_id, status = None, "NOT_ELIGIBLE"
                rows_to_insert.append({
                    "key": f"single:{market_id}:{side}",
                    "type": str((deterministic_candidate or {}).get("entryType") or f"{side}_SINGLE"),
                    "market_id": market_id, "outcome_range": outcome_range, "side": side,
                    "rank": rank, "classification": classification, "band": band,
                    "price": price, "shares": float(self.config.get("shares", 5)),
                    "combined": price, "snapshot": market.get("snapshotUtc"),
                    "status": status, "action_id": action_id,
                    "legs": [{"marketId": market_id, "outcomeRange": outcome_range, "side": side, "price": price}],
                })

        ranked = sorted(
            [market for market in markets if str(market.get("outcomeRange")) in distribution],
            key=lambda market: int(distribution[str(market.get("outcomeRange"))].get("rank") or 999),
        )
        for legs_count in (2, 3):
            if len(ranked) < legs_count:
                continue
            legs = ranked[:legs_count]
            leg_data = []
            for market in legs:
                bucket = distribution.get(str(market.get("outcomeRange"))) or {}
                low = as_float(market.get("bucketLow"))
                price = as_float(market.get("yesExecutableBuyPrice5"))
                if low is None or str(bucket.get("classification")) not in {"clear_leader", "contender", "plausible"}:
                    leg_data = []
                    break
                leg_data.append({
                    "marketId": str(market.get("marketId")), "outcomeRange": str(market.get("outcomeRange")),
                    "rank": int(bucket.get("rank") or 0), "price": price, "bucketLow": low,
                })
            if len(leg_data) != legs_count:
                continue
            lows = sorted(item["bucketLow"] for item in leg_data)
            if any(abs(lows[index + 1] - lows[index] - 1.0) > 0.01 for index in range(len(lows) - 1)):
                continue
            combined = sum(item["price"] for item in leg_data) if all(item["price"] is not None for item in leg_data) else None
            cap = float(self.config.get("yesLadderMaxCombinedPrice", 0.99))
            status = "LADDER_ELIGIBLE_SKIPPED" if combined is not None and combined < cap else "LADDER_PRICE_TOO_HIGH"
            action_ids = [action_lookup.get((item["marketId"], "YES")) for item in leg_data]
            if all(item and item[2] == "buy" for item in action_ids):
                status = "LADDER_EXECUTED"
            elif any(item for item in action_ids):
                status = "LADDER_PARTIAL_OR_REJECTED"
            rows_to_insert.append({
                "key": f"ladder:{legs_count}:" + ",".join(item["marketId"] for item in leg_data),
                "type": f"YES_LADDER_{legs_count}", "market_id": None, "outcome_range": None, "side": "YES",
                "rank": 1, "classification": "ladder", "band": None, "price": None,
                "shares": float(self.config.get("shares", 5)), "combined": combined,
                "snapshot": max((str(market.get("snapshotUtc") or "") for market in legs), default=None),
                "status": status, "action_id": action_ids[0][0] if action_ids and action_ids[0] else None,
                "legs": leg_data,
            })

        for item in rows_to_insert:
            self.db.execute(
                """
                INSERT INTO weather_ai_opportunity_evaluations(
                    strategy_name,event_id,cycle_id,observer_event_id,city,target_date,decision_time_utc,
                    opportunity_key,opportunity_type,market_id,outcome_range,outcome_side,rank,classification,
                    probability_band,executable_price,executable_shares,combined_price,market_snapshot_at_utc,
                    weather_state_hash,candidate_status,action_id,legs_json,created_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(strategy_name,cycle_id,opportunity_key) DO UPDATE SET
                    candidate_status=excluded.candidate_status,action_id=COALESCE(excluded.action_id,weather_ai_opportunity_evaluations.action_id),
                    executable_price=excluded.executable_price,combined_price=excluded.combined_price
                """,
                (
                    self.strategy_name,event["event_id"],cycle_id,observer_event_id,event["city"],event["target_date"],decision_time,
                    item["key"],item["type"],item["market_id"],item["outcome_range"],item["side"],item["rank"],item["classification"],
                    item["band"],item["price"],item["shares"],item["combined"],item["snapshot"],weather_hash,item["status"],item["action_id"],
                    json.dumps(item["legs"], ensure_ascii=False, separators=(",", ":")),created,
                ),
            )
        return len(rows_to_insert)

    def _backfill_opportunity_evaluations(self) -> int:
        rows = self.db.execute(
            """
            SELECT c.cycle_id,c.input_json,c.ai_response_json
            FROM weather_ai_agent_cycles c
            WHERE c.strategy_name=? AND NOT EXISTS(
                SELECT 1 FROM weather_ai_opportunity_evaluations o WHERE o.strategy_name=? AND o.cycle_id=c.cycle_id
            )
            ORDER BY c.cycle_id
            """,
            (self.strategy_name, self.strategy_name),
        ).fetchall()
        count = 0
        for row in rows:
            context = json_value(row["input_json"], {})
            decision = json_value(row["ai_response_json"], {})
            if not isinstance(context, dict) or not isinstance(decision, dict) or not decision.get("settlementDistribution"):
                continue
            action_records = []
            actions = decision.get("actions") or []
            action_rows = self.db.execute(
                "SELECT action_id,action_index FROM weather_ai_agent_actions WHERE cycle_id=?",
                (row["cycle_id"],),
            ).fetchall()
            by_index = {int(item["action_index"]): int(item["action_id"]) for item in action_rows}
            action_records = [(action, by_index[index]) for index, action in enumerate(actions) if index in by_index]
            try:
                count += self.persist_opportunity_evaluations(int(row["cycle_id"]), context, decision, action_records)
            except Exception:
                logging.exception("opportunity backfill failed cycle=%s", row["cycle_id"])
        self.db.commit()
        if count:
            logging.info("backfilled %s weather opportunity evaluation rows", count)
        return count

    def refresh_opportunity_evaluations(self) -> int:
        rows = self.db.execute(
            """
            SELECT o.*,e.resolved_at_utc AS event_resolved_at_utc,
                   e.winning_range AS event_winning_range
            FROM weather_ai_opportunity_evaluations o JOIN events e ON e.event_id=o.event_id
            WHERE o.resolved_at_utc IS NULL AND e.resolved_at_utc IS NOT NULL AND e.winning_range IS NOT NULL
            """
        ).fetchall()
        updated = 0
        for row in rows:
            winning = str(row["event_winning_range"])
            legs = json_value(row["legs_json"], [])
            if row["opportunity_type"].startswith("YES_LADDER"):
                selected_ranges = {str(item.get("outcomeRange")) for item in legs if isinstance(item, dict)}
                won = winning in selected_ranges
                cost = (float(row["combined_price"]) * float(row["executable_shares"])) if row["combined_price"] is not None else None
            else:
                selected = str(row["outcome_range"] or "")
                won = selected == winning if row["outcome_side"] == "YES" else selected != winning
                cost = (float(row["executable_price"]) * float(row["executable_shares"])) if row["executable_price"] is not None else None
            payout = float(row["executable_shares"]) if won else 0.0
            pnl = payout - cost if cost is not None else None
            self.db.execute(
                """
                UPDATE weather_ai_opportunity_evaluations SET resolved_at_utc=?,winning_range=?,signal_correct=?,
                    hypothetical_pnl_usdc=? WHERE opportunity_id=?
                """,
                (row["event_resolved_at_utc"], winning, int(won), pnl, row["opportunity_id"]),
            )
            updated += 1
        return updated

    def _research_snapshot_slot(self, now: datetime) -> datetime:
        interval = max(1, int(self.config.get("researchSnapshotMinutes", 5))) * 60
        # Let the collector finish the canonical slot before this secondary
        # research writer aligns and persists its cross-source view.
        delay = max(0, int(self.config.get("researchSnapshotDelaySeconds", 90)))
        epoch = int(now.timestamp()) - delay
        return datetime.fromtimestamp(epoch - (epoch % interval), UTC)

    def _due_frozen_ladder_cutoff_slot(self, now: datetime) -> datetime | None:
        """Return today's 11:00 cutoff once its canonical collector run is complete."""
        local_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
        cutoff_local = local_now.replace(hour=11, minute=0, second=0, microsecond=0)
        cutoff = cutoff_local.astimezone(UTC)
        deadline = cutoff + timedelta(minutes=FROZEN_LADDER_CAPTURE_DEADLINE_MINUTES)
        if now < cutoff or now > deadline:
            return None
        try:
            row = self.db.execute(
                """
                SELECT 1 FROM runs
                WHERE slot_utc=? AND status='completed'
                  AND completed_at_utc IS NOT NULL AND completed_at_utc<=?
                LIMIT 1
                """,
                (iso_utc(cutoff), iso_utc(now)),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return cutoff if row else None

    def record_due_frozen_ladder_cutoff(self, now: datetime) -> tuple[int, int]:
        """Catch the canonical 11:00 shadow cutoff without using post-cutoff evidence."""
        cutoff = self._due_frozen_ladder_cutoff_slot(now)
        if cutoff is None:
            return 0, 0
        cutoff_text = iso_utc(cutoff)
        candidate_writes = 0
        for event in self.research_events(cutoff):
            exists = self.db.execute(
                """
                SELECT 1 FROM weather_ladder_frozen_candidates
                WHERE strategy_name=? AND event_id=? AND target_date=?
                LIMIT 1
                """,
                (self.strategy_name, event["event_id"], event["target_date"]),
            ).fetchone()
            if exists:
                continue
            markets = self.market_states(event, cutoff)
            process = self.weather_process_state(event, cutoff)
            weather = self.local_weather_payload(event, cutoff)
            models = self.model_update_state(event, cutoff)
            metar = weather.get("metar") or {}
            ensemble = models.get("ecmwfEnsemble") or {}
            process_trend = process.get("primaryStationTrend") or {}
            _ridge_snapshot_id, ridge = self._latest_ridge_for_shadow(
                str(event["event_id"]), cutoff
            )
            weather_gate_state = {
                "role": "diagnostic_only_not_an_entry_gate",
                "observedAtUtc": metar.get("obsTime"),
                "observedTemperatureC": metar.get("temp"),
                "observedDailyMaxC": metar.get("dailyMaxC"),
                "temperatureTrendCPerHour": process_trend.get("temperatureTrendCPerHour"),
                "ensembleMeanMaxC": ensemble.get("meanMaxC"),
                "ensembleStdMaxC": ensemble.get("stdMaxC"),
                "ensembleSampleSlotUtc": ensemble.get("sampleSlotUtc"),
            }
            candidate_writes += self.record_frozen_ladder_candidate(
                event, cutoff_text, now, markets, process, ridge, weather_gate_state
            )
            self.db.commit()
        portfolio_writes = self.record_frozen_ladder_portfolio_selection(cutoff_text)
        self.db.commit()
        return candidate_writes, portfolio_writes

    def research_events(self, now: datetime) -> list[dict[str, Any]]:
        """Return active, unresolved events eligible for passive research sampling."""
        allowed = self.allowed_cities()
        if not allowed:
            return []
        rows = self.db.execute(
            """
            SELECT e.event_id,e.city,e.target_date,e.station_id,e.station_name,
                   e.resolution_source,e.rules,e.end_date_utc,s.latitude,s.longitude,s.timezone
            FROM events e JOIN stations s ON s.station_id=e.station_id
            WHERE e.resolved_at_utc IS NULL AND s.timezone IS NOT NULL
            ORDER BY e.target_date,e.city,e.last_seen_utc DESC
            """
        ).fetchall()
        output: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        start_hour = int(self.config.get("activeLocalStartHour", 7))
        end_hour = int(self.config.get("activeLocalEndHour", 19))
        for row in rows:
            city_key = str(row["city"] or "").strip().casefold()
            if city_key not in allowed:
                continue
            try:
                local_now = now.astimezone(ZoneInfo(row["timezone"]))
                target = date.fromisoformat(row["target_date"])
            except (ValueError, ZoneInfoNotFoundError):
                continue
            if local_now.date() != target or not start_hour <= local_now.hour < end_hour:
                continue
            key = (city_key, str(row["target_date"]))
            if key in seen:
                continue
            seen.add(key)
            output.append(dict(row))
        return output

    def _latest_ridge_for_shadow(
        self, event_id: str, now: datetime,
    ) -> tuple[int | None, dict[str, Any] | None]:
        row = self.db.execute(
            """
            SELECT snapshot_id,payload_json FROM weather_ridge_v2_snapshots
            WHERE event_id=? AND status='ok' AND julianday(feature_as_of_utc)<=julianday(?)
            ORDER BY julianday(feature_as_of_utc) DESC,snapshot_id DESC LIMIT 1
            """,
            (event_id, iso_utc(now)),
        ).fetchone()
        if not row:
            return None, None
        payload = json_value(row["payload_json"], None)
        return int(row["snapshot_id"]), payload if isinstance(payload, dict) else None

    def record_ladder_shadow_snapshots(
        self, event: dict[str, Any], sample_slot_utc: str,
        markets: list[dict[str, Any]], ridge_snapshot_id: int | None,
        ridge: dict[str, Any] | None,
    ) -> int:
        """Record every adjacent exact-C triple independently of AI selection."""
        exact: list[tuple[int, dict[str, Any]]] = []
        for market in markets:
            low, high = as_float(market.get("bucketLow")), as_float(market.get("bucketHigh"))
            if (
                low is None or high is None or abs(low - high) > 1e-9
                or str(market.get("bucketUnit") or "C").upper() != "C"
                or abs(low - round(low)) > 1e-9
            ):
                continue
            exact.append((int(round(low)), market))
        exact.sort(key=lambda item: item[0])
        ridge_probabilities = {
            int(row["bucketC"]): as_float(row.get("probability")) or 0.0
            for row in (ridge or {}).get("bucketProbabilities") or []
            if isinstance(row, dict) and row.get("bucketC") is not None
        }
        has_distribution = bool(
            ridge and ridge.get("status") == "ok"
            and ridge.get("distributionVersion") and ridge.get("bucketProbabilities")
        )
        calibration_status = str((ridge or {}).get("calibrationStatus") or "unavailable")
        calibration_dates = int((ridge or {}).get("calibrationDates") or 0)
        constrained = bool(((ridge or {}).get("stability") or {}).get("constraintApplied"))
        shares = float(self.config.get("shares", 5))
        min_probability = float(self.config.get("ladderShadowMinPackageProbability", 0.70))
        min_edge = float(self.config.get("ladderShadowMinEdge", 0.10))
        market_snapshot = max(
            (str(market.get("snapshotUtc") or "") for _, market in exact), default=None
        )
        written = 0
        for index in range(len(exact) - 2):
            legs = exact[index:index + 3]
            buckets = [item[0] for item in legs]
            if buckets[1] - buckets[0] != 1 or buckets[2] - buckets[1] != 1:
                continue
            leg_payload = [{
                "marketId": str(market.get("marketId")),
                "outcomeRange": str(market.get("outcomeRange")),
                "bucketC": bucket,
                "yesExecutableBuyPrice5": as_float(market.get("yesExecutableBuyPrice5")),
                "yesBuyAvailableShares": as_float(market.get("yesBuyAvailableShares")),
                "modelProbability": ridge_probabilities.get(bucket) if has_distribution else None,
            } for bucket, market in legs]
            prices = [item["yesExecutableBuyPrice5"] for item in leg_payload]
            combined = sum(prices) if all(value is not None for value in prices) else None
            probability = (
                sum(ridge_probabilities.get(bucket, 0.0) for bucket in buckets)
                if has_distribution else None
            )
            edge = probability - combined if probability is not None and combined is not None else None
            if not has_distribution:
                status = "MISSING_RIDGE_DISTRIBUTION"
            elif combined is None:
                status = "INCOMPLETE_EXECUTABLE_DEPTH"
            elif constrained:
                status = "STABILITY_CONSTRAINED_SHADOW"
            elif calibration_status != "calibrated_research_only":
                status = "UNCALIBRATED_SHADOW"
            elif probability >= min_probability and edge >= min_edge:
                status = "EDGE_QUALIFIED_SHADOW"
            else:
                status = "NO_EDGE_SHADOW"
            triple_key = f"{buckets[0]}:{buckets[1]}:{buckets[2]}"
            cursor = self.db.execute(
                """
                INSERT INTO weather_ladder_shadow_snapshots(
                    strategy_name,event_id,city,target_date,sample_slot_utc,
                    market_snapshot_at_utc,ridge_snapshot_id,ridge_feature_as_of_utc,
                    ridge_observation_time_utc,ridge_model_version,distribution_version,
                    calibration_status,calibration_dates,stability_constraint_applied,
                    triple_key,lower_bucket_c,center_bucket_c,upper_bucket_c,
                    package_probability,combined_yes_price_5,package_edge,executable_shares,
                    candidate_status,legs_json,ridge_payload_json,created_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(strategy_name,event_id,sample_slot_utc,triple_key) DO NOTHING
                """,
                (
                    self.strategy_name, event["event_id"], event["city"], event["target_date"],
                    sample_slot_utc, market_snapshot, ridge_snapshot_id,
                    (ridge or {}).get("featureAsOfUtc"),
                    (ridge or {}).get("latestObservationTimeUtc"),
                    (ridge or {}).get("modelVersion"),
                    (ridge or {}).get("distributionVersion"), calibration_status,
                    calibration_dates, int(constrained), triple_key,
                    buckets[0], buckets[1], buckets[2], probability, combined, edge, shares,
                    status, json.dumps(leg_payload, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(ridge, ensure_ascii=False, separators=(",", ":"), default=str) if ridge else None,
                    iso_utc(),
                ),
            )
            written += int(cursor.rowcount > 0)
        return written

    def refresh_ladder_shadow_snapshots(self) -> int:
        has_labels = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='weather_resolution_labels'"
        ).fetchone() is not None
        label_select = "l.official_temperature_c" if has_labels else "NULL AS official_temperature_c"
        label_join = "LEFT JOIN weather_resolution_labels l ON l.event_id=s.event_id" if has_labels else ""
        rows = self.db.execute(
            f"""
            SELECT s.ladder_snapshot_id,s.legs_json,s.combined_yes_price_5,
                   s.executable_shares,e.resolved_at_utc,e.winning_range,
                   {label_select}
            FROM weather_ladder_shadow_snapshots s JOIN events e ON e.event_id=s.event_id
            {label_join}
            WHERE s.resolved_at_utc IS NULL AND e.resolved_at_utc IS NOT NULL
              AND e.winning_range IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            legs = json_value(row["legs_json"], [])
            selected = {str(item.get("outcomeRange")) for item in legs if isinstance(item, dict)}
            hit = str(row["winning_range"]) in selected
            cost = (
                float(row["combined_yes_price_5"]) * float(row["executable_shares"])
                if row["combined_yes_price_5"] is not None else None
            )
            payout = float(row["executable_shares"]) if hit else 0.0
            pnl = payout - cost if cost is not None else None
            self.db.execute(
                """
                UPDATE weather_ladder_shadow_snapshots SET
                    resolved_at_utc=?,winning_range=?,official_temperature_c=?,
                    package_hit=?,hypothetical_pnl_usdc=? WHERE ladder_snapshot_id=?
                """,
                (
                    row["resolved_at_utc"], row["winning_range"],
                    row["official_temperature_c"], int(hit), pnl,
                    row["ladder_snapshot_id"],
                ),
            )
        if rows:
            self.db.commit()
        return len(rows)

    def record_frozen_ladder_candidate(
        self, event: dict[str, Any], sample_slot_utc: str,
        frozen_at_utc: datetime, markets: list[dict[str, Any]],
        process: dict[str, Any] | None, ridge: dict[str, Any] | None,
        weather_gate_state: dict[str, Any] | None = None,
    ) -> int:
        """Freeze the prospective 11:00 market-centered 1/3/1 shadow rule."""
        if not self.config.get("frozenLadderShadowEnabled", True):
            return 0
        timezone_name = str(event.get("timezone") or "UTC")
        slot = parse_ts(sample_slot_utc)
        if slot is None:
            return 0
        try:
            local_slot = slot.astimezone(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            return 0
        if (
            local_slot.hour * 60 + local_slot.minute != FROZEN_LADDER_LOCAL_MINUTES
            or local_slot.date().isoformat() != str(event.get("target_date") or "")
        ):
            return 0

        exact: list[tuple[int, dict[str, Any], float]] = []
        for market in markets:
            low, high = as_float(market.get("bucketLow")), as_float(market.get("bucketHigh"))
            bid, ask = as_float(market.get("yesBestBid")), as_float(market.get("yesBestAsk"))
            if (
                low is None or high is None or abs(low - high) > 1e-9
                or str(market.get("bucketUnit") or "C").upper() != "C"
                or abs(low - round(low)) > 1e-9 or bid is None or ask is None
            ):
                continue
            exact.append((int(round(low)), market, (bid + ask) / 2.0))

        reasons: list[str] = []
        center: tuple[int, dict[str, Any], float] | None = None
        second_midpoint = None
        center_lead = None
        legs: list[tuple[int, dict[str, Any], float]] = []
        if len(exact) < 2:
            reasons.append("INSUFFICIENT_EXACT_BUCKET_MIDPOINTS")
        else:
            ranked = sorted(exact, key=lambda item: (-item[2], item[0]))
            center = ranked[0]
            second_midpoint = ranked[1][2]
            center_lead = center[2] - second_midpoint
            by_bucket = {item[0]: item for item in exact}
            legs = [by_bucket[bucket] for bucket in range(center[0] - 1, center[0] + 2) if bucket in by_bucket]
            if len(legs) != 3:
                reasons.append("MISSING_ADJACENT_EXACT_BUCKET")
            if center_lead + 1e-12 < FROZEN_LADDER_MIN_CENTER_LEAD:
                reasons.append("CENTER_LEAD_BELOW_0.03")

        weights = FROZEN_LADDER_WEIGHTS
        leg_rows: list[dict[str, Any]] = []
        combined_cost = None
        spreads: list[float | None] = []
        fees: list[float] = []
        market_snapshot = max(
            (str(market.get("snapshotUtc") or "") for market in markets), default=None
        ) or None
        snapshot_time = parse_ts(market_snapshot)
        market_age = (
            max(0.0, (frozen_at_utc.astimezone(UTC) - snapshot_time).total_seconds() / 60.0)
            if snapshot_time is not None else None
        )
        if market_age is None:
            reasons.append("MISSING_MARKET_SNAPSHOT_TIME")
        elif market_age > FROZEN_LADDER_MAX_MARKET_AGE_MINUTES:
            reasons.append("MARKET_SNAPSHOT_STALE")

        if len(legs) == 3:
            costs: list[float] = []
            depth_complete = True
            for (bucket, market, midpoint), weight in zip(legs, weights):
                vwap, available = executable_vwap(market.get("yesBook"), weight, "asks")
                bid, ask = as_float(market.get("yesBestBid")), as_float(market.get("yesBestAsk"))
                spread = ask - bid if bid is not None and ask is not None else None
                spreads.append(spread)
                if vwap is None or available + 1e-9 < weight:
                    depth_complete = False
                else:
                    costs.append(vwap * weight)
                    fees.append(estimated_weather_taker_fee(weight, vwap) or 0.0)
                leg_rows.append({
                    "marketId": str(market.get("marketId") or ""),
                    "outcomeRange": str(market.get("outcomeRange") or ""),
                    "bucketC": bucket, "weight": weight, "midpoint": midpoint,
                    "vwap": vwap, "availableShares": available, "spread": spread,
                    "estimatedFeeUsdc": estimated_weather_taker_fee(weight, vwap),
                })
            if not depth_complete:
                reasons.append("INCOMPLETE_WEIGHTED_VWAP_DEPTH")
            else:
                combined_cost = sum(costs)
                if combined_cost <= FROZEN_LADDER_MIN_COST_EXCLUSIVE + 1e-12:
                    reasons.append("COMBINED_COST_NOT_ABOVE_1.50")
                if combined_cost > FROZEN_LADDER_MAX_COST_INCLUSIVE + 1e-12:
                    reasons.append("COMBINED_COST_ABOVE_2.00")
            if any(spread is None for spread in spreads):
                reasons.append("MISSING_BID_ASK_SPREAD")
            elif max(float(spread) for spread in spreads) > FROZEN_LADDER_MAX_SPREAD + 1e-12:
                reasons.append("MAX_SPREAD_ABOVE_0.20")

        estimated_fee = sum(fees) if len(legs) == 3 and len(fees) == 3 else None
        net_cost = (
            combined_cost + estimated_fee
            if combined_cost is not None and estimated_fee is not None else None
        )

        rejection_reasons = list(dict.fromkeys(reasons))
        status = "ELIGIBLE_SHADOW" if not rejection_reasons else "REJECTED_SHADOW"
        center_bucket = center[0] if center else None
        center_market = center[1] if center else {}
        gate_state = weather_gate_state or {}
        observed_max = as_float(gate_state.get("observedDailyMaxC"))
        trend = as_float(gate_state.get("temperatureTrendCPerHour"))
        ensemble_std = as_float(gate_state.get("ensembleStdMaxC"))
        ensemble_mean = as_float(gate_state.get("ensembleMeanMaxC"))
        center_minus_observed = (
            center_bucket - observed_max
            if center_bucket is not None and observed_max is not None else None
        )
        ensemble_mean_minus_center = (
            ensemble_mean - center_bucket
            if center_bucket is not None and ensemble_mean is not None else None
        )
        # Both outer legs have weight 1; address them by list position.
        lower = leg_rows[0] if len(leg_rows) == 3 else None
        center_leg = leg_rows[1] if len(leg_rows) == 3 else None
        upper = leg_rows[2] if len(leg_rows) == 3 else None
        cursor = self.db.execute(
            """
            INSERT INTO weather_ladder_frozen_candidates(
                strategy_name,rule_version,event_id,city,target_date,timezone,
                frozen_slot_utc,frozen_at_utc,market_snapshot_at_utc,market_age_minutes,
                center_market_id,center_bucket_c,center_midpoint,second_midpoint,center_lead,
                lower_market_id,upper_market_id,lower_bucket_c,upper_bucket_c,
                lower_vwap_1,center_vwap_3,upper_vwap_1,
                lower_available,center_available,upper_available,
                lower_spread,center_spread,upper_spread,combined_cost_usdc,total_shares,
                estimated_fee_usdc,net_cost_usdc,
                eligibility_status,rejection_reasons_json,legs_json,process_state_json,
                ridge_payload_json,weather_gate_state_json,observed_daily_max_c,
                center_minus_observed_max_c,temperature_trend_c_per_hour,
                ensemble_std_max_c,ensemble_mean_minus_center_c
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(strategy_name,rule_version,event_id,target_date) DO NOTHING
            """,
            (
                self.strategy_name, FROZEN_LADDER_RULE_VERSION, event["event_id"],
                event["city"], event["target_date"], timezone_name, sample_slot_utc,
                iso_utc(frozen_at_utc), market_snapshot, market_age,
                str(center_market.get("marketId") or "") or None, center_bucket,
                center[2] if center else None, second_midpoint, center_lead,
                lower["marketId"] if lower else None, upper["marketId"] if upper else None,
                lower["bucketC"] if lower else None, upper["bucketC"] if upper else None,
                lower["vwap"] if lower else None, center_leg["vwap"] if center_leg else None,
                upper["vwap"] if upper else None,
                lower["availableShares"] if lower else None,
                center_leg["availableShares"] if center_leg else None,
                upper["availableShares"] if upper else None,
                lower["spread"] if lower else None, center_leg["spread"] if center_leg else None,
                upper["spread"] if upper else None, combined_cost, sum(weights),
                estimated_fee, net_cost, status,
                json.dumps(rejection_reasons, separators=(",", ":")),
                json.dumps(leg_rows, ensure_ascii=False, separators=(",", ":")),
                json.dumps(process, ensure_ascii=False, separators=(",", ":"), default=str) if process else None,
                json.dumps(ridge, ensure_ascii=False, separators=(",", ":"), default=str) if ridge else None,
                json.dumps(gate_state, ensure_ascii=False, separators=(",", ":"), default=str) if gate_state else None,
                observed_max, center_minus_observed, trend, ensemble_std,
                ensemble_mean_minus_center,
            ),
        )
        candidate_row = self.db.execute(
            """
            SELECT candidate_id FROM weather_ladder_frozen_candidates
            WHERE strategy_name=? AND rule_version=? AND event_id=? AND target_date=?
            """,
            (
                self.strategy_name, FROZEN_LADDER_RULE_VERSION,
                event["event_id"], event["target_date"],
            ),
        ).fetchone()
        if candidate_row:
            for variant_version, variant_weights in FROZEN_LADDER_VARIANTS.items():
                variant_legs: list[dict[str, Any]] = []
                variant_costs: list[float] = []
                variant_fees: list[float] = []
                variant_reasons = list(rejection_reasons)
                if len(legs) == 3:
                    for (bucket, market, midpoint), weight in zip(legs, variant_weights):
                        price, available = executable_vwap(market.get("yesBook"), weight, "asks")
                        if price is None or available + 1e-9 < weight:
                            if "VARIANT_INCOMPLETE_WEIGHTED_VWAP_DEPTH" not in variant_reasons:
                                variant_reasons.append("VARIANT_INCOMPLETE_WEIGHTED_VWAP_DEPTH")
                        else:
                            variant_costs.append(price * weight)
                            variant_fees.append(estimated_weather_taker_fee(weight, price) or 0.0)
                        variant_legs.append({
                            "marketId": str(market.get("marketId") or ""),
                            "outcomeRange": str(market.get("outcomeRange") or ""),
                            "bucketC": bucket, "weight": weight, "midpoint": midpoint,
                            "vwap": price, "availableShares": available,
                            "estimatedFeeUsdc": estimated_weather_taker_fee(weight, price),
                        })
                variant_cost = sum(variant_costs) if len(variant_costs) == 3 else None
                variant_fee = sum(variant_fees) if len(variant_fees) == 3 else None
                variant_net_cost = (
                    variant_cost + variant_fee
                    if variant_cost is not None and variant_fee is not None else None
                )
                variant_status = (
                    "ELIGIBLE_SHADOW"
                    if status == "ELIGIBLE_SHADOW" and variant_cost is not None
                    else "REJECTED_SHADOW"
                )
                clob_minimum_satisfied = min(variant_weights) >= 5.0
                self.db.execute(
                    """
                    INSERT INTO weather_ladder_frozen_variants(
                        candidate_id,strategy_name,rule_version,event_id,city,target_date,
                        lower_weight,center_weight,upper_weight,total_weight,
                        weight_interpretation,execution_feasibility_status,
                        combined_cost_usdc,taker_fee_rate,estimated_fee_usdc,net_cost_usdc,
                        eligibility_status,rejection_reasons_json,legs_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(strategy_name,rule_version,event_id,target_date) DO NOTHING
                    """,
                    (
                        candidate_row["candidate_id"], self.strategy_name, variant_version,
                        event["event_id"], event["city"], event["target_date"],
                        variant_weights[0], variant_weights[1], variant_weights[2],
                        sum(variant_weights),
                        "actual_share_counts" if clob_minimum_satisfied else "normalized_shadow_units",
                        (
                            "CLOB_MINIMUM_5_SHARES_SATISFIED_SHADOW_ONLY"
                            if clob_minimum_satisfied
                            else "NORMALIZED_ONLY_CLOB_MINIMUM_NOT_VALIDATED"
                        ),
                        variant_cost, WEATHER_TAKER_FEE_RATE, variant_fee, variant_net_cost,
                        variant_status,
                        json.dumps(variant_reasons, separators=(",", ":")),
                        json.dumps(variant_legs, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
        return int(cursor.rowcount > 0)

    def record_frozen_ladder_portfolio_selection(self, sample_slot_utc: str) -> int:
        """Record frozen shadow packages; a zero limit means every eligible city."""
        slot = parse_ts(sample_slot_utc)
        if slot is None:
            return 0
        local_slot = slot.astimezone(ZoneInfo("Asia/Shanghai"))
        if local_slot.hour * 60 + local_slot.minute != FROZEN_LADDER_LOCAL_MINUTES:
            return 0
        target_date = local_slot.date().isoformat()
        written = 0
        for portfolio in FROZEN_LADDER_PORTFOLIOS:
            if target_date < str(portfolio.get("forward_start_date") or "0000-01-01"):
                continue
            rows = self.db.execute(
                """
                SELECT c.candidate_id,c.event_id,c.city,c.center_lead,
                       c.temperature_trend_c_per_hour,c.center_minus_observed_max_c,
                       MAX(c.lower_spread,c.center_spread,c.upper_spread) AS max_leg_spread,
                       v.variant_id,v.combined_cost_usdc,v.estimated_fee_usdc,v.net_cost_usdc
                FROM weather_ladder_frozen_candidates c
                JOIN weather_ladder_frozen_variants v ON v.candidate_id=c.candidate_id
                WHERE c.strategy_name=? AND c.target_date=?
                  AND c.eligibility_status='ELIGIBLE_SHADOW'
                  AND v.rule_version=? AND v.eligibility_status='ELIGIBLE_SHADOW'
                  AND v.net_cost_usdc<=15.0
                """,
                (self.strategy_name, target_date, portfolio["variant_version"]),
            ).fetchall()
            if portfolio["selector"] in {
                "lowest_max_spread_then_city", "all_eligible_by_max_spread_then_city",
            }:
                rows = sorted(
                    rows,
                    key=lambda row: (row["max_leg_spread"], row["city"], row["event_id"]),
                )
            else:
                rows = sorted(
                    rows,
                    key=lambda row: (row["center_lead"], row["city"], row["event_id"]),
                )
            limit = int(portfolio.get("max_events_per_date", 1))
            selected_rows = rows if limit <= 0 else rows[:limit]
            for selected in selected_rows or [None]:
                selected_at = iso_utc()
                status = "SELECTED_SHADOW" if selected else "NO_ELIGIBLE_SHADOW"
                reasons = [] if selected else ["NO_CANDIDATE_WITH_NET_COST_AT_OR_BELOW_15"]
                selection_key = str(selected["event_id"]) if selected else "__no_eligible__"
                cursor = self.db.execute(
                    """
                    INSERT INTO weather_ladder_frozen_portfolio_selections(
                        strategy_name,portfolio_version,target_date,selection_key,frozen_slot_utc,
                        selected_at_utc,selector,max_events_per_date,max_cost_usdc,
                        eligible_candidates,selection_status,rejection_reasons_json,
                        candidate_id,variant_id,event_id,city,center_lead,selected_cost_usdc,
                        selected_notional_usdc,selected_fee_usdc,
                        temperature_trend_c_per_hour,center_minus_observed_max_c,
                        diagnostic_positive_trend,diagnostic_center_at_least_two_above_observed,
                        resolved_at_utc,payout_usdc,hypothetical_pnl_usdc
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(strategy_name,portfolio_version,target_date,selection_key) DO NOTHING
                    """,
                    (
                        self.strategy_name, portfolio["portfolio_version"], target_date,
                        selection_key, sample_slot_utc, selected_at, portfolio["selector"],
                        limit, 15.0, len(rows), status,
                        json.dumps(reasons, separators=(",", ":")),
                        selected["candidate_id"] if selected else None,
                        selected["variant_id"] if selected else None,
                        selected["event_id"] if selected else None,
                        selected["city"] if selected else None,
                        selected["center_lead"] if selected else None,
                        selected["net_cost_usdc"] if selected else None,
                        selected["combined_cost_usdc"] if selected else None,
                        selected["estimated_fee_usdc"] if selected else None,
                        selected["temperature_trend_c_per_hour"] if selected else None,
                        selected["center_minus_observed_max_c"] if selected else None,
                        (
                            int(as_float(selected["temperature_trend_c_per_hour"]) > 0)
                            if selected and as_float(selected["temperature_trend_c_per_hour"]) is not None
                            else None
                        ),
                        (
                            int(as_float(selected["center_minus_observed_max_c"]) >= 2)
                            if selected and as_float(selected["center_minus_observed_max_c"]) is not None
                            else None
                        ),
                        None if selected else selected_at,
                        None if selected else 0.0,
                        None if selected else 0.0,
                    ),
                )
                written += int(cursor.rowcount > 0)
        return written

    def refresh_frozen_ladder_candidates(self) -> int:
        has_labels = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='weather_resolution_labels'"
        ).fetchone() is not None
        label_select = "l.official_temperature_c" if has_labels else "NULL AS official_temperature_c"
        label_join = "LEFT JOIN weather_resolution_labels l ON l.event_id=c.event_id" if has_labels else ""
        rows = self.db.execute(
            f"""
            SELECT c.candidate_id,c.eligibility_status,c.legs_json,c.net_cost_usdc,
                   e.resolved_at_utc,e.winning_range,{label_select}
            FROM weather_ladder_frozen_candidates c
            JOIN events e ON e.event_id=c.event_id
            {label_join}
            WHERE c.resolved_at_utc IS NULL AND e.resolved_at_utc IS NOT NULL
              AND e.winning_range IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            legs = json_value(row["legs_json"], [])
            winning = str(row["winning_range"])
            payout = sum(
                float(item.get("weight") or 0.0)
                for item in legs if isinstance(item, dict) and str(item.get("outcomeRange")) == winning
            )
            cost = as_float(row["net_cost_usdc"])
            pnl = (
                payout - cost
                if row["eligibility_status"] == "ELIGIBLE_SHADOW" and cost is not None else None
            )
            self.db.execute(
                """
                UPDATE weather_ladder_frozen_candidates SET
                    resolved_at_utc=?,winning_range=?,official_temperature_c=?,
                    payout_usdc=?,package_hit=?,hypothetical_pnl_usdc=?
                WHERE candidate_id=?
                """,
                (
                    row["resolved_at_utc"], winning, row["official_temperature_c"],
                    payout, int(payout > 0), pnl, row["candidate_id"],
                ),
            )
        variant_rows = self.db.execute(
            """
            SELECT v.variant_id,v.eligibility_status,v.legs_json,v.net_cost_usdc,
                   e.resolved_at_utc,e.winning_range
            FROM weather_ladder_frozen_variants v
            JOIN events e ON e.event_id=v.event_id
            WHERE v.resolved_at_utc IS NULL AND e.resolved_at_utc IS NOT NULL
              AND e.winning_range IS NOT NULL
            """
        ).fetchall()
        for row in variant_rows:
            legs = json_value(row["legs_json"], [])
            winning = str(row["winning_range"])
            payout = sum(
                float(item.get("weight") or 0.0)
                for item in legs if isinstance(item, dict) and str(item.get("outcomeRange")) == winning
            )
            cost = as_float(row["net_cost_usdc"])
            pnl = (
                payout - cost
                if row["eligibility_status"] == "ELIGIBLE_SHADOW" and cost is not None else None
            )
            self.db.execute(
                """
                UPDATE weather_ladder_frozen_variants SET
                    resolved_at_utc=?,winning_range=?,payout_usdc=?,package_hit=?,
                    hypothetical_pnl_usdc=? WHERE variant_id=?
                """,
                (
                    row["resolved_at_utc"], winning, payout, int(payout > 0),
                    pnl, row["variant_id"],
                ),
            )
        selection_rows = self.db.execute(
            """
            SELECT s.selection_id,v.resolved_at_utc,v.winning_range,
                   v.payout_usdc,v.hypothetical_pnl_usdc
            FROM weather_ladder_frozen_portfolio_selections s
            JOIN weather_ladder_frozen_variants v ON v.variant_id=s.variant_id
            WHERE s.resolved_at_utc IS NULL AND v.resolved_at_utc IS NOT NULL
            """
        ).fetchall()
        for row in selection_rows:
            self.db.execute(
                """
                UPDATE weather_ladder_frozen_portfolio_selections SET
                    resolved_at_utc=?,winning_range=?,payout_usdc=?,hypothetical_pnl_usdc=?
                WHERE selection_id=?
                """,
                (
                    row["resolved_at_utc"], row["winning_range"], row["payout_usdc"],
                    row["hypothetical_pnl_usdc"], row["selection_id"],
                ),
            )
        if rows or variant_rows or selection_rows:
            self.db.commit()
        return len(rows)

    def _backfill_ladder_shadow_snapshots(self) -> int:
        """Recover historical package prices without inventing unavailable V3 probabilities."""
        rows = self.db.execute(
            """
            SELECT r.* FROM weather_ai_research_snapshots r
            WHERE r.strategy_name=? AND NOT EXISTS(
                SELECT 1 FROM weather_ladder_shadow_snapshots s
                WHERE s.strategy_name=r.strategy_name AND s.event_id=r.event_id
                  AND s.sample_slot_utc=r.sample_slot_utc
            )
            ORDER BY r.research_snapshot_id
            """,
            (self.strategy_name,),
        ).fetchall()
        written = 0
        for row in rows:
            when = parse_ts(row["sample_slot_utc"])
            market_rows = json_value(row["market_state_json"], [])
            if when is None or not isinstance(market_rows, list):
                continue
            markets = []
            for market in market_rows:
                if not isinstance(market, dict):
                    continue
                metadata = self.db.execute(
                    "SELECT bucket_low,bucket_high,bucket_unit FROM markets WHERE market_id=?",
                    (str(market.get("marketId") or ""),),
                ).fetchone()
                if not metadata:
                    continue
                markets.append({
                    **market, "bucketLow": metadata["bucket_low"],
                    "bucketHigh": metadata["bucket_high"], "bucketUnit": metadata["bucket_unit"],
                    "snapshotUtc": row["market_snapshot_at_utc"],
                })
            ridge_snapshot_id, ridge = self._latest_ridge_for_shadow(str(row["event_id"]), when)
            written += self.record_ladder_shadow_snapshots(
                {
                    "event_id": row["event_id"], "city": row["city"],
                    "target_date": row["target_date"],
                },
                str(row["sample_slot_utc"]), markets, ridge_snapshot_id, ridge,
            )
        self.db.commit()
        if written:
            logging.info("backfilled %s ladder shadow package rows", written)
        return written

    def record_research_snapshots(self, now: datetime) -> int:
        """Persist aligned weather/forecast/market state without invoking the AI."""
        slot = self._research_snapshot_slot(now)
        slot_text = iso_utc(slot)
        written = 0
        self._last_ladder_shadow_written = 0
        self._last_frozen_ladder_written = 0
        self._last_frozen_portfolio_written = 0
        # Ridge and ladder research must not depend on a successful Grok
        # response. Close any prior read/write transaction before Ridge opens
        # its own point-in-time SQLite connection.
        self.db.commit()
        for event in self.research_events(now):
            markets = self.market_states(event, now)
            if not markets:
                self._last_frozen_ladder_written += self.record_frozen_ladder_candidate(
                    event, slot_text, now, [], None, None
                )
                self.db.commit()
                continue
            weather = self.local_weather_payload(event, now)
            models = self.model_update_state(event, now)
            process = self.weather_process_state(event, now)
            metar = weather.get("metar") or {}
            ridge_snapshot_id = None
            ridge = None
            ridge_adapter = getattr(self, "ridge_v2", None)
            if ridge_adapter is not None:
                try:
                    ridge = ridge_adapter.snapshot(
                        event, now, f"research:{slot_text}", process
                    )
                    if not isinstance(ridge, dict):
                        ridge = None
                    # Keep the exact Ridge payload returned for this research
                    # point. A later AI call must not replace its probabilities.
                    if ridge and ridge.get("status") in {"ok", "stale"}:
                        row = self.db.execute(
                            """
                            SELECT snapshot_id FROM weather_ridge_v2_snapshots
                            WHERE event_id=? AND feature_as_of_utc=?
                            ORDER BY snapshot_id DESC LIMIT 1
                            """,
                            (str(event["event_id"]), str(ridge.get("featureAsOfUtc") or "")),
                        ).fetchone()
                        ridge_snapshot_id = int(row["snapshot_id"]) if row else None
                except Exception as exc:
                    # Research collection remains useful when Ridge is
                    # unavailable; the ladder row will explicitly retain a
                    # missing distribution instead of inventing one.
                    logging.warning(
                        "Ridge research snapshot failed for %s: %s",
                        event.get("event_id"), str(exc)[:500],
                    )
                    ridge = {
                        "status": "unavailable",
                        "role": "candidate_generator_only",
                        "reason": f"research snapshot failed: {exc}",
                    }
            summary = [
                {
                    "marketId": item.get("marketId"),
                    "outcomeRange": item.get("outcomeRange"),
                    "yesBestBid": item.get("yesBestBid"),
                    "yesBestAsk": item.get("yesBestAsk"),
                    "noBestBid": item.get("noBestBid"),
                    "noBestAsk": item.get("noBestAsk"),
                    "yesExecutableBuyPrice5": item.get("yesExecutableBuyPrice5"),
                    "noExecutableBuyPrice5": item.get("noExecutableBuyPrice5"),
                    "yesBuyAvailableShares": item.get("yesBuyAvailableShares"),
                    "noBuyAvailableShares": item.get("noBuyAvailableShares"),
                    "volume24h": item.get("volume24h"),
                    "liquidity": item.get("liquidity"),
                }
                for item in markets
            ]
            weather_state = {
                "metar": metar,
                "process": process,
                "observedDailyMaxC": metar.get("dailyMaxC"),
            }
            forecast_state = {
                "meteoblue": models.get("meteoblue"),
                "ecmwf": models.get("ecmwf"),
                "ecmwfEnsemble": models.get("ecmwfEnsemble"),
                "ridgeV2": ridge,
            }
            ensemble_state = models.get("ecmwfEnsemble") or {}
            process_trend = (process.get("primaryStationTrend") or {})
            weather_gate_state = {
                "role": "diagnostic_only_not_an_entry_gate",
                "observedAtUtc": metar.get("obsTime"),
                "observedTemperatureC": metar.get("temp"),
                "observedDailyMaxC": metar.get("dailyMaxC"),
                "temperatureTrendCPerHour": process_trend.get("temperatureTrendCPerHour"),
                "ensembleMeanMaxC": ensemble_state.get("meanMaxC"),
                "ensembleStdMaxC": ensemble_state.get("stdMaxC"),
                "ensembleSampleSlotUtc": ensemble_state.get("sampleSlotUtc"),
            }
            state_hash = hashlib.sha256(
                json.dumps(
                    {"weather": weather_state, "forecast": forecast_state},
                    ensure_ascii=False, sort_keys=True, default=str,
                ).encode()
            ).hexdigest()[:20]
            market_snapshot = max(
                (str(item.get("snapshotUtc") or "") for item in markets),
                default=None,
            )
            mblue_max = as_float((models.get("meteoblue") or {}).get("maxC"))
            ecmwf_max = as_float((models.get("ecmwf") or {}).get("maxC"))
            cursor = self.db.execute(
                """
                INSERT INTO weather_ai_research_snapshots(
                    strategy_name,event_id,city,target_date,sample_slot_utc,
                    market_snapshot_at_utc,source_fetched_at_utc,observed_at_utc,
                    observed_temperature_c,observed_daily_max_c,meteoblue_max_c,ecmwf_max_c,
                    process_status,weather_state_hash,market_state_json,weather_state_json,
                    forecast_state_json,created_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(strategy_name,event_id,sample_slot_utc) DO NOTHING
                """,
                (
                    self.strategy_name, event["event_id"], event["city"], event["target_date"], slot_text,
                    market_snapshot, iso_utc(now), metar.get("obsTime"), metar.get("temp"),
                    metar.get("dailyMaxC"), mblue_max, ecmwf_max,
                    process.get("status"), state_hash,
                    json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(weather_state, ensure_ascii=False, separators=(",", ":"), default=str),
                    json.dumps(forecast_state, ensure_ascii=False, separators=(",", ":"), default=str),
                    iso_utc(now),
                ),
            )
            written += int(cursor.rowcount > 0)
            self._last_ladder_shadow_written += self.record_ladder_shadow_snapshots(
                event, slot_text, markets, ridge_snapshot_id, ridge
            )
            self._last_frozen_ladder_written += self.record_frozen_ladder_candidate(
                event, slot_text, now, self.market_states(event, slot), process, ridge,
                weather_gate_state,
            )
            # Ridge uses a separate SQLite connection. Release this agent's
            # writer lock after each event so the next event can persist its
            # own Ridge snapshot without waiting on an uncommitted ladder row.
            self.db.commit()
        catchup_candidates, catchup_portfolios = self.record_due_frozen_ladder_cutoff(now)
        self._last_frozen_ladder_written += catchup_candidates
        self._last_frozen_portfolio_written += catchup_portfolios
        self._last_frozen_portfolio_written += self.record_frozen_ladder_portfolio_selection(
            slot_text
        )
        # INSERT ... DO NOTHING still opens a write transaction when every row
        # already exists. Always close it so the collector is never starved.
        self.db.commit()
        return written

    def refresh_research_snapshots(self) -> int:
        rows = self.db.execute(
            """
            SELECT r.research_snapshot_id,e.resolved_at_utc,e.winning_range
            FROM weather_ai_research_snapshots r JOIN events e ON e.event_id=r.event_id
            WHERE r.resolved_at_utc IS NULL AND e.resolved_at_utc IS NOT NULL
              AND e.winning_range IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            self.db.execute(
                "UPDATE weather_ai_research_snapshots SET resolved_at_utc=?,winning_range=? WHERE research_snapshot_id=?",
                (row["resolved_at_utc"], row["winning_range"], row["research_snapshot_id"]),
            )
        if rows:
            self.db.commit()
        return len(rows)

    def execute_action(
        self,
        cycle_id: int,
        index: int,
        context: dict[str, Any],
        action: dict[str, Any],
        decision: dict[str, Any] | None = None,
        forced_rejection: str | None = None,
    ) -> tuple[int, bool]:
        requested = str(action.get("action") or "")
        market_id = str(action.get("marketId") or "")
        outcome_side = str(action.get("outcomeSide") or "").upper()
        requested_shares = as_float(action.get("shares"))
        execution_now = utc_now()
        realtime_guards = self.config.get("realtimeExecutionGuards", True)
        paper_market_order = self.config.get("paperMarketOrderExecution", False)
        analysis_market = self._market_from_context(context, market_id) if market_id else None
        market = analysis_market
        if requested in {"buy", "sell"} and analysis_market is not None and realtime_guards:
            market = self._latest_market_for_execution(context, market_id, execution_now)
        execution_context = context
        weather_metadata: dict[str, Any] = {}
        weather_rejection = None
        weather_status = "not_required"
        if (
            requested == "buy"
            and analysis_market is not None
            and realtime_guards
            and self.config.get("refreshWeatherBeforeExecution", True)
            and not forced_rejection
        ):
            try:
                execution_context, weather_metadata = self._execution_weather_context(
                    context, execution_now
                )
                weather_status = "fresh"
                if weather_metadata.get("metarChanged"):
                    weather_status = (
                        "new_metar_recorded_paper_market_order"
                        if paper_market_order else "new_metar_requires_reanalysis"
                    )
                    if not paper_market_order:
                        weather_rejection = (
                            "new METAR arrived after analysis; reanalysis against the stored "
                            "invalidation condition is required before execution"
                        )
                elif weather_metadata.get("alignmentChanged"):
                    weather_status = (
                        "alignment_changed_recorded_paper_market_order"
                        if paper_market_order else "alignment_changed_requires_reanalysis"
                    )
                    if not paper_market_order:
                        weather_rejection = (
                            "weather process/Ridge alignment changed after analysis; reanalysis is "
                            "required before execution"
                        )
                if reason := self._follow_aligned_leader_no_rejection(
                    action, execution_context
                ):
                    weather_status = "blocked_follow_aligned_leader_no"
                    weather_rejection = reason
            except Exception as exc:
                weather_status = (
                    "refresh_failed_paper_market_order"
                    if paper_market_order else "refresh_failed"
                )
                if not paper_market_order:
                    weather_rejection = f"execution weather refresh failed: {exc}"
        executed, rejection = requested, None
        executed_shares = price = notional = None
        market_age = None
        fee = 0.0
        if forced_rejection:
            executed, rejection = "rejected", forced_rejection
        elif weather_rejection:
            executed, rejection = "rejected", weather_rejection
        elif self.config.get("noOnlyPaperMode", False) and requested not in {"observe", "buy"}:
            executed, rejection = "rejected", "NO-only paper mode permits only observe or buy actions"
        elif self.config.get("noOnlyPaperMode", False) and requested == "buy" and outcome_side != "NO":
            executed, rejection = "rejected", "NO-only paper mode permits only NO purchases"
        elif self.config.get("noOnlyPaperMode", False) and requested == "buy" and (
            str(action.get("entryType") or "") not in NO_PAPER_ENTRY_TYPES
        ):
            executed, rejection = "rejected", "unsupported NO research entry type"
        elif requested == "observe":
            market_id = ""
            outcome_side = ""
        elif requested in {"buy", "sell", "hold"} and outcome_side not in {"YES", "NO"}:
            executed, rejection = "rejected", "outcomeSide must be YES or NO"
        elif requested in {"buy", "sell", "hold"} and analysis_market is None:
            executed, rejection = "rejected", "marketId is not present in the event snapshot"
        elif requested in {"buy", "sell"} and market is None:
            executed, rejection = "rejected", "no current execution snapshot is available for the selected market"
        elif requested == "hold":
            position = self._position(context["event"]["event_id"], market_id)
            if not position or float(position["shares"] or 0) <= 0:
                executed, rejection = "rejected", "hold requires an open position"
            elif str(position["outcome_side"] or "NO").upper() != outcome_side:
                executed, rejection = "rejected", "hold outcomeSide does not match the open position"
        elif requested in {"buy", "sell"}:
            shares = requested_shares or 0.0
            min_buy = float(self.config.get("minSharesPerBuy", 5))
            max_action = float(self.config["maxSharesPerAction"])
            freshness_time = parse_ts(market.get("fetchedAtUtc") or market.get("snapshotUtc"))
            market_age = (
                max(0.0, (execution_now - freshness_time).total_seconds() / 60.0)
                if realtime_guards and freshness_time else
                as_float((context.get("dataFreshness") or {}).get("marketAgeMinutes"))
            )
            max_market_age = float(self.config["maxMarketDataAgeMinutes"])
            position = self._position(context["event"]["event_id"], market_id)
            window = (
                self.decision_window(context, execution_now)
                if realtime_guards else
                context.get("decisionWindow") or self.decision_window(context)
            )
            if not bool(window.get("ordersAllowed")):
                executed = "hold" if position and float(position["shares"] or 0) > 0 else "observe"
                rejection = f"observation-only: orders are disabled outside {window.get('tradingWindowLocal', 'the local trading window')}"
            elif requested == "buy" and not bool(window.get("newEntriesAllowed", True)):
                executed = "hold" if position and float(position["shares"] or 0) > 0 else "observe"
                rejection = "position-review checkpoint: new entries are disabled"
            elif market_age is None or market_age > max_market_age:
                executed, rejection = "rejected", f"market snapshot is stale age={market_age}m"
            elif shares <= 0 or (requested == "buy" and shares > max_action + 1e-9):
                executed, rejection = "rejected", f"shares must be in (0,{max_action}]"
            elif requested == "buy" and shares < min_buy - 1e-9:
                executed, rejection = "rejected", f"buy requires at least {min_buy:g} shares"
            else:
                current_shares = float(position["shares"] or 0) if position else 0.0
                current_side = str(position["outcome_side"] or "NO").upper() if position else outcome_side
                if current_shares > 1e-9 and current_side != outcome_side:
                    executed, rejection = "rejected", "close the opposite outcome before switching direction"
                elif requested == "buy" and current_shares + shares > float(self.config["maxSharesPerMarket"]) + 1e-9:
                    executed, rejection = "rejected", "market position limit exceeded"
                elif requested == "sell" and shares > current_shares + 1e-9:
                    executed, rejection = "rejected", "sell exceeds open position"
                else:
                    book = market.get(f"{outcome_side.lower()}Book")
                    book_side = "asks" if requested == "buy" else "bids"
                    price, available = executable_vwap(book, shares, book_side)
                    validation_market = analysis_market if paper_market_order else market
                    validation_context = context if paper_market_order else execution_context
                    validation_price = None
                    if requested == "buy" and validation_market is not None:
                        validation_price = as_float(
                            validation_market.get(f"{outcome_side.lower()}ExecutableBuyPrice5")
                        )
                        if validation_price is None:
                            validation_price, _validation_available = executable_vwap(
                                validation_market.get(f"{outcome_side.lower()}Book"),
                                float(self.config.get("shares", 5)), "asks",
                            )
                    if price is None and requested == "sell" and available >= min_buy - 1e-9:
                        shares = min(shares, available)
                        price, available = executable_vwap(book, shares, book_side)
                    if price is None:
                        executed, rejection = "rejected", f"only {available:.4f} shares executable"
                    elif requested == "sell" and float(price) <= float(self.config.get("minSellPriceExclusive", 0.01)) + 1e-9:
                        executed = "hold"
                        rejection = "sell price is at or below the residual-value floor; retained for settlement"
                    elif requested == "sell" and (
                        reason := self._sell_state_rejection(action, decision, context, market)
                    ):
                        executed, rejection = "hold", reason
                    elif requested == "buy" and (
                        reason := self._buy_reasoning_rejection(
                            action, decision or {}, validation_context,
                            validation_market or market, position
                        )
                    ):
                        executed, rejection = "rejected", reason
                    elif requested == "buy" and validation_price is None:
                        executed, rejection = "rejected", "analysis snapshot has no executable side price"
                    elif requested == "buy" and str(action.get("entryType") or "") != "YES_LADDER_EXPERIMENT" and (reason := self._price_band_rejection(action, float(validation_price))):
                        executed, rejection = "rejected", reason
                    elif requested == "buy" and (reason := self._market_disagreement_rejection(action, float(validation_price))):
                        executed, rejection = "rejected", reason
                    else:
                        fee = shares * price * float(self.config.get("feeRate", 0.0))
                        proposed = shares * price + fee
                        city = context["event"]["city"]
                        if requested == "buy" and self._open_notional(city) + proposed > float(self.config["maxOpenNotionalPerCity"]) + 1e-9:
                            executed, rejection = "rejected", "city open-notional limit exceeded"
                        elif requested == "buy" and self._open_notional() + proposed > float(self.config["maxOpenNotionalTotal"]) + 1e-9:
                            executed, rejection = "rejected", "portfolio open-notional limit exceeded"
                        elif requested == "buy" and proposed > self.account_state()["spendableCashUsdc"] + 1e-9:
                            executed, rejection = "rejected", "insufficient available cash: minimum cash reserve would be breached"
                        else:
                            executed_shares, notional = shares, shares * price
        else:
            executed, rejection = "rejected", "unsupported action"

        cursor = self.db.execute(
            """
            INSERT INTO weather_ai_agent_actions(
                cycle_id,action_index,requested_action,executed_action,market_id,outcome_range,
                outcome_side,
                requested_shares,executed_shares,execution_price,notional_usdc,fee_usdc,
                no_win_probability,win_probability,confidence_low,confidence_high,thesis,evidence_json,key_risk,
                market_implied_probability,consensus_position,evidence_strength,
                why_market_may_be_right,why_market_may_be_wrong,contrarian_evidence_json,new_information_types_json,
                exact_bucket_risk_assessment,upper_bucket_touch_probability,heating_process_status,
                outcome_assessment,probability_band,price_assessment,price_risk_assessment,upper_bucket_risk,
                entry_type,sizing_tier,
                new_evidence_since_prior_json,
                invalidation_condition,analysis_market_snapshot_at_utc,
                execution_market_snapshot_at_utc,execution_market_fetched_at_utc,
                execution_market_age_minutes,execution_checked_at_utc,
                execution_metar_observation_time_utc,execution_weather_process_slot_utc,
                execution_weather_alignment_json,weather_revalidation_status,
                rejection_reason,created_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                cycle_id, index, requested, executed, market_id or None,
                market.get("outcomeRange") if market else None, outcome_side or None,
                requested_shares, executed_shares, price, notional, fee,
                None, None, None, None,
                action.get("thesis"), json.dumps(action.get("evidence") or [], ensure_ascii=False),
                action.get("keyRisk"), as_float(action.get("marketImpliedProbability")),
                action.get("consensusPosition"), action.get("evidenceStrength"),
                action.get("whyMarketMayBeRight"), action.get("whyMarketMayBeWrong"),
                json.dumps(action.get("disagreementEvidence") or [], ensure_ascii=False),
                json.dumps(action.get("newInformationTypes") or [], ensure_ascii=False),
                action.get("exactBucketRiskAssessment"), None,
                action.get("heatingProcessStatus"), action.get("outcomeAssessment"),
                action.get("probabilityBand"), action.get("priceAssessment"),
                action.get("priceRiskAssessment"), action.get("upperBucketRisk"),
                action.get("entryType"), action.get("sizingTier"),
                json.dumps(action.get("newEvidenceSincePrior") or [], ensure_ascii=False),
                action.get("invalidationCondition"),
                analysis_market.get("snapshotUtc") if analysis_market else None,
                market.get("snapshotUtc") if market else None,
                market.get("fetchedAtUtc") if market else None,
                market_age, iso_utc(execution_now),
                weather_metadata.get("executionMetarObservationTimeUtc"),
                weather_metadata.get("executionWeatherProcessSlotUtc"),
                json.dumps(weather_metadata.get("alignment") or {}, ensure_ascii=False, default=str),
                weather_status, rejection, iso_utc(),
            ),
        )
        action_id = int(cursor.lastrowid)
        if requested == "buy" and market is not None and action.get("entryType") in {
            "NO_OVERSHOOT", "NO_CEILING", "NO_MARKET_TAIL_REJECTION",
            "AI_DISCRETION", "YES_CONVERGENCE", "NO_EXCLUSION", "NO_LEADER_OVERSHOOT", "YES_LADDER_EXPERIMENT"
        }:
            trigger = context.get("trigger") or {}
            observer_event_id = trigger.get("observerEventId")
            observer_row = self.db.execute(
                "SELECT observer_started_at_utc,observer_completed_at_utc,source_observed_at_utc,source_fetched_at_utc FROM weather_observer_events WHERE observer_event_id=?",
                (observer_event_id,),
            ).fetchone() if observer_event_id is not None else None
            completed_at = iso_utc()
            self.db.execute(
                """
                INSERT OR IGNORE INTO weather_ai_signal_evaluations(
                    strategy_name,observer_event_id,cycle_id,action_id,event_id,city,market_id,
                    outcome_range,outcome_side,entry_type,source_observed_at_utc,source_fetched_at_utc,
                    weather_state_changed_at_utc,ai_started_at_utc,ai_completed_at_utc,
                    market_snapshot_at_utc,executable_price,created_at_utc,updated_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    self.strategy_name, observer_event_id, cycle_id, action_id,
                    context["event"]["event_id"], context["event"]["city"], market["marketId"],
                    market.get("outcomeRange"), outcome_side, action.get("entryType"),
                    observer_row["source_observed_at_utc"] if observer_row else trigger.get("observationTimeUtc"),
                    observer_row["source_fetched_at_utc"] if observer_row else trigger.get("fetchedAtUtc"),
                    trigger.get("decisionTriggerTimeUtc"),
                    observer_row["observer_completed_at_utc"] if observer_row else None,
                    completed_at, market.get("snapshotUtc"), price, completed_at, completed_at,
                ),
            )
        filled = bool(executed_shares and price is not None and requested in {"buy", "sell"})
        if filled and market is not None:
            self._apply_fill(
                action_id, context, market, "BUY" if requested == "buy" else "SELL",
                outcome_side, float(executed_shares), float(price), fee,
            )
        return action_id, filled

    def persist_response(self, run_id: int, contexts: list[dict[str, Any]], response: dict[str, Any]) -> tuple[int, int, int]:
        cycles = response.get("cycles") or []
        by_event = {str(row.get("eventId")): row for row in cycles if isinstance(row, dict)}
        expected = {str(row["event"]["event_id"]) for row in contexts}
        if set(by_event) != expected or len(cycles) != len(expected):
            raise RuntimeError("AI response event IDs do not match METAR-triggered events")
        cycle_count = action_count = fill_count = 0
        for context in contexts:
            event, trigger = context["event"], context["trigger"]
            cycle_key_time = trigger.get("decisionTriggerTimeUtc") or trigger["observationTimeUtc"]
            decision_trigger_id = trigger.get("decisionTriggerId")
            decision = by_event[event["event_id"]]
            self._validate_cycle_reasoning(decision, context)
            if str(decision.get("city")) != str(event["city"]):
                raise RuntimeError(f"AI city mismatch for {event['event_id']}")
            if parse_ts(decision.get("metarObservationTimeUtc")) != parse_ts(trigger["observationTimeUtc"]):
                raise RuntimeError(f"AI METAR timestamp mismatch for {event['event_id']}")
            self.db.execute(
                """
                INSERT INTO weather_ai_agent_cycles(
                    strategy_name,run_id,event_id,city,station_id,target_date,timezone,
                    metar_observation_time_utc,trigger_slot_utc,analyzed_at_utc,status,
                    decision_trigger_id,decision_trigger_type,decision_trigger_time_utc,
                    primary_metar_observation_time_utc,
                    state_assessment,model_reality_gap,remaining_heating_assessment,
                    market_consensus_assessment,weather_process_assessment,model_correction_assessment,
                    process_confidence,market_decision_mode,ridge_v2_state_json,market_alignment_json,
                    future_scenarios_json,settlement_distribution_json,
                    temperature_thesis,uncertainty_assessment,next_review_reason,
                    input_json,ai_response_json,error
                ) VALUES(?,?,?,?,?,?,?,?,?,?,'completed',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                ON CONFLICT(strategy_name,event_id,metar_observation_time_utc) DO UPDATE SET
                    run_id=excluded.run_id,analyzed_at_utc=excluded.analyzed_at_utc,status='completed',
                    decision_trigger_id=excluded.decision_trigger_id,
                    decision_trigger_type=excluded.decision_trigger_type,
                    decision_trigger_time_utc=excluded.decision_trigger_time_utc,
                    primary_metar_observation_time_utc=excluded.primary_metar_observation_time_utc,
                    state_assessment=excluded.state_assessment,model_reality_gap=excluded.model_reality_gap,
                    remaining_heating_assessment=excluded.remaining_heating_assessment,
                    market_consensus_assessment=excluded.market_consensus_assessment,
                    weather_process_assessment=excluded.weather_process_assessment,
                    model_correction_assessment=excluded.model_correction_assessment,
                    process_confidence=excluded.process_confidence,
                    market_decision_mode=excluded.market_decision_mode,
                    ridge_v2_state_json=excluded.ridge_v2_state_json,
                    market_alignment_json=excluded.market_alignment_json,
                    future_scenarios_json=excluded.future_scenarios_json,
                    settlement_distribution_json=excluded.settlement_distribution_json,
                    temperature_thesis=excluded.temperature_thesis,
                    uncertainty_assessment=excluded.uncertainty_assessment,
                    next_review_reason=excluded.next_review_reason,input_json=excluded.input_json,
                    ai_response_json=excluded.ai_response_json,error=NULL
                """,
                (
                    self.strategy_name, run_id, event["event_id"], event["city"], event["station_id"],
                    event["target_date"], event["timezone"], cycle_key_time,
                    trigger["slotUtc"], iso_utc(), decision_trigger_id, trigger.get("type"),
                    trigger.get("decisionTriggerTimeUtc"), trigger["observationTimeUtc"],
                    decision.get("stateAssessment"),
                    decision.get("modelRealityGap"), decision.get("remainingHeatingAssessment"),
                    decision.get("marketConsensusAssessment"),
                    decision.get("weatherProcessAssessment"), decision.get("modelCorrectionAssessment"),
                    decision.get("processConfidence"),
                    decision.get("marketDecisionMode"),
                    json.dumps(context.get("ridgeV2") or {}, ensure_ascii=False, default=str),
                    json.dumps(context.get("marketAlignment") or {}, ensure_ascii=False, default=str),
                    json.dumps(decision.get("futureScenarios") or [], ensure_ascii=False),
                    json.dumps(decision.get("settlementDistribution") or [], ensure_ascii=False),
                    decision.get("temperatureThesis"), decision.get("uncertaintyAssessment"),
                    decision.get("nextReviewReason"), json.dumps(context, ensure_ascii=False, default=str),
                    json.dumps(decision, ensure_ascii=False, default=str),
                ),
            )
            cycle = self.db.execute(
                """
                SELECT cycle_id FROM weather_ai_agent_cycles
                WHERE strategy_name=? AND event_id=? AND metar_observation_time_utc=?
                """,
                (self.strategy_name, event["event_id"], cycle_key_time),
            ).fetchone()
            cycle_id = int(cycle["cycle_id"])
            self.db.execute("DELETE FROM weather_ai_agent_actions WHERE cycle_id=?", (cycle_id,))
            actions = decision.get("actions") or []
            if not actions:
                raise RuntimeError(f"AI returned no actions for {event['event_id']}")
            ladder_rejection = self._ladder_preflight(context, decision, actions)
            action_records: list[tuple[dict[str, Any], int]] = []
            for index, action in enumerate(actions):
                forced_rejection = (
                    ladder_rejection
                    if str(action.get("entryType") or "") == "YES_LADDER_EXPERIMENT"
                    else None
                )
                _action_id, filled = self.execute_action(
                    cycle_id, index, context, action, decision, forced_rejection
                )
                action_records.append((action, _action_id))
                action_count += 1
                fill_count += int(filled)
            try:
                self.persist_opportunity_evaluations(cycle_id, context, decision, action_records)
            except Exception:
                logging.exception("opportunity evaluation persistence failed cycle=%s", cycle_id)
            observer_event_id = trigger.get("observerEventId")
            if observer_event_id is not None:
                self.db.execute(
                    """
                    UPDATE weather_observer_events SET escalation_status='processed',trade_cycle_id=?
                    WHERE observer_event_id=? AND escalation_status='pending'
                    """,
                    (cycle_id, int(observer_event_id)),
                )
            cycle_count += 1
        return cycle_count, action_count, fill_count

    def settle_positions(self) -> int:
        rows = self.db.execute(
            """
            SELECT p.*,r.resolved_at_utc,r.winning_outcome,r.no_final_price
            FROM weather_ai_agent_positions p JOIN market_resolutions r ON r.market_id=p.market_id
            WHERE p.strategy_name=? AND p.shares>0 AND r.is_resolved=1
            """,
            (self.strategy_name,),
        ).fetchall()
        count = 0
        for row in rows:
            winning_side = str(row["winning_outcome"] or "").upper()
            if winning_side not in {"YES", "NO"}:
                winning_side = "NO" if float(row["no_final_price"] or 0) >= 0.99 else "YES"
            position_side = str(row["outcome_side"] or "NO").upper()
            position_won = position_side == winning_side
            shares, cost = float(row["shares"]), float(row["cost_basis_usdc"])
            payout = shares if position_won else 0.0
            realized = payout - cost
            settled_at = row["resolved_at_utc"] or iso_utc()
            self.db.execute(
                """
                UPDATE weather_ai_agent_positions
                SET shares=0,cost_basis_usdc=0,realized_pnl_usdc=realized_pnl_usdc+?,
                    status='settled',updated_at_utc=?,closed_at_utc=?,final_outcome=?
                WHERE strategy_name=? AND event_id=? AND market_id=?
                """,
                (
                    realized, settled_at, settled_at, winning_side,
                    self.strategy_name, row["event_id"], row["market_id"],
                ),
            )
            self.db.execute(
                """
                INSERT INTO weather_ai_agent_fills(
                    strategy_name,action_id,event_id,market_id,city,fill_type,side,outcome_side,shares,price,notional_usdc,
                    fee_usdc,realized_pnl_usdc,filled_at_utc
                ) VALUES(?,NULL,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    self.strategy_name, row["event_id"], row["market_id"], row["city"], "settlement", "SETTLE",
                    position_side, shares, 1.0 if position_won else 0.0, payout, 0.0, realized, settled_at,
                ),
            )
            count += 1
        return count

    def refresh_signal_evaluations(self) -> int:
        rows = self.db.execute(
            """
            SELECT s.*,e.resolved_at_utc,e.winning_range
            FROM weather_ai_signal_evaluations s JOIN events e ON e.event_id=s.event_id
            WHERE s.resolved_at_utc IS NULL AND e.resolved_at_utc IS NOT NULL
            """
        ).fetchall()
        updated = 0
        for row in rows:
            snapshots = self.db.execute(
                """
                SELECT slot_utc,yes_book_json,no_book_json FROM market_snapshots
                WHERE market_id=? ORDER BY slot_utc
                """,
                (row["market_id"],),
            ).fetchall()
            first_90 = None
            side = str(row["outcome_side"] or "").lower()
            for snapshot in snapshots:
                price, available = executable_vwap(snapshot[f"{side}_book_json"], 5, "asks")
                if price is not None and available >= 5 - 1e-9 and price >= 0.90:
                    first_90 = parse_ts(snapshot["slot_utc"])
                    break
            signal_time = parse_ts(row["ai_completed_at_utc"])
            lead = (first_90 - signal_time).total_seconds() / 60 if first_90 and signal_time else None
            winning_range = str(row["winning_range"] or "")
            selected_won = str(row["outcome_range"] or "") == winning_range
            correct = selected_won if str(row["outcome_side"]).upper() == "YES" else not selected_won
            self.db.execute(
                """
                UPDATE weather_ai_signal_evaluations SET market_side_first_90_at_utc=?,
                    resolved_at_utc=?,winning_range=?,signal_correct=?,lead_minutes=?,updated_at_utc=?
                WHERE signal_id=?
                """,
                (
                    iso_utc(first_90) if first_90 else None, row["resolved_at_utc"], winning_range,
                    int(correct), lead, iso_utc(), row["signal_id"],
                ),
            )
            updated += 1
        return updated

    def pending_lesson_inputs(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT e.event_id,e.city,e.target_date,e.winning_range
            FROM events e
            WHERE e.resolved_at_utc IS NOT NULL
              AND EXISTS(SELECT 1 FROM weather_ai_agent_cycles c WHERE c.event_id=e.event_id AND c.strategy_name=?)
              AND NOT EXISTS(SELECT 1 FROM weather_ai_agent_lessons l WHERE l.event_id=e.event_id AND l.strategy_name=?)
            ORDER BY e.resolved_at_utc LIMIT ?
            """,
            (self.strategy_name, self.strategy_name, int(self.config.get("maxReviewEventsPerRun", 5))),
        ).fetchall()
        output = []
        for event in rows:
            cycles = self.db.execute(
                """
                SELECT cycle_id,city,target_date,metar_observation_time_utc,trigger_slot_utc,
                       analyzed_at_utc,status,state_assessment,temperature_thesis,
                       uncertainty_assessment,next_review_reason,error,model_reality_gap,
                       remaining_heating_assessment,market_consensus_assessment,
                       weather_process_assessment,model_correction_assessment,process_confidence,
                       market_decision_mode
                FROM weather_ai_agent_cycles
                WHERE strategy_name=? AND event_id=?
                ORDER BY metar_observation_time_utc
                """,
                (self.strategy_name, event["event_id"]),
            ).fetchall()
            cycle_rows = [dict(row) for row in cycles]
            max_cycles = max(1, int(self.config.get("lessonMaxCyclesPerEvent", 8)))
            if len(cycle_rows) > max_cycles:
                cycle_rows = cycle_rows[-max_cycles:]
            actions = self.db.execute(
                """
                SELECT a.action_id,a.cycle_id,a.action_index,a.requested_action,a.executed_action,
                       a.market_id,a.outcome_range,a.requested_shares,a.executed_shares,
                       a.execution_price,a.notional_usdc,a.fee_usdc,a.no_win_probability,
                       a.confidence_low,a.confidence_high,a.thesis,a.key_risk,
                       a.invalidation_condition,a.rejection_reason,a.created_at_utc,
                       a.outcome_side,a.win_probability,a.market_implied_probability,
                       a.consensus_position,a.evidence_strength,a.outcome_assessment,
                       a.probability_band,a.price_assessment,a.sizing_tier
                FROM weather_ai_agent_actions a JOIN weather_ai_agent_cycles c USING(cycle_id)
                WHERE c.strategy_name=? AND c.event_id=? ORDER BY c.metar_observation_time_utc,a.action_index
                """,
                (self.strategy_name, event["event_id"]),
            ).fetchall()
            action_rows = [dict(row) for row in actions]
            max_actions = max(1, int(self.config.get("lessonMaxActionsPerEvent", 20)))
            if len(action_rows) > max_actions:
                action_rows = action_rows[-max_actions:]
            positions = self.db.execute(
                """
                SELECT market_id,city,outcome_range,outcome_side,shares,cost_basis_usdc,
                       realized_pnl_usdc,status,opened_at_utc,updated_at_utc,closed_at_utc,final_outcome
                FROM weather_ai_agent_positions WHERE strategy_name=? AND event_id=?
                """,
                (self.strategy_name, event["event_id"]),
            ).fetchall()
            net = sum(float(row["realized_pnl_usdc"] or 0) for row in positions)
            output.append(
                {
                    "event": dict(event), "netPnlUsdc": net,
                    "cycles": cycle_rows,
                    "actions": action_rows,
                    "positions": [dict(row) for row in positions],
                }
            )
        return output

    def call_lesson_ai(self, inputs: list[dict[str, Any]]) -> dict[str, Any]:
        if self.config.get("autonomousMode", False):
            prompt = (
                "你是自主天气交易Agent的每日结算复盘模块。唯一目标是帮助同一Agent在可接受风险和永不all-in的前提下，"
                "长期提高20 USDC Paper账户净值。根据当时实际可见数据、每半小时Ridge V2、连续决策、请求与真实成交、"
                "持仓变化、最终结算和PnL，判断哪些收益来自可复用判断，哪些来自运气；哪些损失来自天气推理、市场理解、"
                "仓位、时点、执行或不可约尾部。不要维护预设交易模式，也不要根据单例创造阈值。将可复用经验写入"
                "reusableLessons，将样本不足和相互矛盾之处写入warnings。输出会自动进入以后每天的决策上下文。\n\n"
                "输入：\n" + json.dumps(inputs, ensure_ascii=False, separators=(",", ":"), default=str)
            )
            return self._run_ai(
                LESSON_SCHEMA_PATH, prompt, int(self.config.get("lessonAiTimeoutSeconds", 180))
            )
        prompt = (
            "你是天气交易 Agent 的结算复盘模块。根据每个事件当时实际看到的快照、连续决策、成交、最终结算和 PnL，"
            "提炼可在未来复用的气象机制与决策经验。必须分别评估温度档选择、YES/NO 方向选择、进出场时点、模型误差、"
            "METAR 轨迹误判、盘口已经计价、执行问题和纯随机尾部。"
            "不能根据单个案例发明新阈值，不能把盈利自动解释为判断正确，也不能删除失败记录。输出会成为以后决策的长期记忆。\n\n"
            "输入：\n" + json.dumps(inputs, ensure_ascii=False, separators=(",", ":"), default=str)
        )
        max_bytes = max(50_000, int(self.config.get("lessonMaxPromptBytes", 500_000)))
        if len(prompt.encode("utf-8")) > max_bytes:
            # Keep the most recent compact evidence for each event.  The full
            # audit trail remains in the project DB, not in the AI transcript.
            compact_inputs = []
            for item in inputs:
                compact = dict(item)
                compact["cycles"] = compact.get("cycles", [])[-4:]
                compact["actions"] = compact.get("actions", [])[-10:]
                compact_inputs.append(compact)
            inputs = compact_inputs
            prompt = (
                "你是天气交易 Agent 的结算复盘模块。只根据每个事件最近的紧凑证据提炼可复用经验；"
                "不要发明阈值，不要删除失败记录。输出会进入未来决策记忆。\n\n输入："
                + json.dumps(inputs, ensure_ascii=False, separators=(",", ":"), default=str)
            )
        if len(prompt.encode("utf-8")) > max_bytes:
            raise RuntimeError(f"lesson prompt exceeds {max_bytes} bytes after compaction")
        return self._run_ai(
            LESSON_SCHEMA_PATH,
            prompt,
            int(self.config.get("lessonAiTimeoutSeconds", 180)),
        )

    def lesson_retry_allowed(self, now: datetime, inputs: list[dict[str, Any]] | None = None) -> bool:
        if inputs is not None:
            signature = ",".join(str(item.get("event", {}).get("event_id")) for item in inputs)
            row = self.db.execute(
                "SELECT value FROM weather_ai_agent_meta WHERE key='lesson_retry_signature'"
            ).fetchone()
            if row and str(row[0]) != signature:
                self.db.execute(
                    "DELETE FROM weather_ai_agent_meta WHERE key IN ('lesson_retry_attempts','lesson_retry_after_utc')"
                )
                self.db.execute(
                    "INSERT INTO weather_ai_agent_meta(key,value) VALUES('lesson_retry_signature',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (signature,),
                )
                self.db.commit()
        attempts_row = self.db.execute(
            "SELECT value FROM weather_ai_agent_meta WHERE key='lesson_retry_attempts'"
        ).fetchone()
        if attempts_row and int(attempts_row[0]) >= int(self.config.get("lessonMaxRetryAttempts", 3)):
            return False
        row = self.db.execute(
            "SELECT value FROM weather_ai_agent_meta WHERE key='lesson_retry_after_utc'"
        ).fetchone()
        retry_after = parse_ts(row[0]) if row else None
        return retry_after is None or now >= retry_after

    def defer_lesson_retry(self, now: datetime, inputs: list[dict[str, Any]] | None = None) -> None:
        signature = ",".join(str(item.get("event", {}).get("event_id")) for item in (inputs or []))
        attempts_row = self.db.execute(
            "SELECT value FROM weather_ai_agent_meta WHERE key='lesson_retry_attempts'"
        ).fetchone()
        attempts = int(attempts_row[0]) + 1 if attempts_row else 1
        base_minutes = float(self.config.get("lessonRetryMinutes", 15))
        max_minutes = float(self.config.get("lessonRetryMaxMinutes", 240))
        retry_after = now + timedelta(minutes=min(max_minutes, base_minutes * (2 ** (attempts - 1))))
        self.db.execute(
            """
            INSERT INTO weather_ai_agent_meta(key,value) VALUES('lesson_retry_after_utc',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (iso_utc(retry_after),),
        )
        self.db.execute(
            """
            INSERT INTO weather_ai_agent_meta(key,value) VALUES('lesson_retry_attempts',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(attempts),),
        )
        self.db.execute(
            """
            INSERT INTO weather_ai_agent_meta(key,value) VALUES('lesson_retry_signature',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (signature,),
        )

    def lesson_processing_allowed(self, now: datetime) -> bool:
        if "lessonLocalStartHour" not in self.config:
            return True
        timezone_name = str(self.config.get("lessonTimezone") or "Asia/Shanghai")
        try:
            local_hour = now.astimezone(ZoneInfo(timezone_name)).hour
        except ZoneInfoNotFoundError:
            local_hour = now.astimezone(UTC).hour
        start = int(self.config.get("lessonLocalStartHour", 19))
        end = int(self.config.get("lessonLocalEndHour", 7))
        if start == end:
            return True
        if start < end:
            return start <= local_hour < end
        return local_hour >= start or local_hour < end

    @staticmethod
    def _ai_circuit_key(base: str, scope: str | None = None) -> str:
        return f"{base}:{scope}" if scope else base

    def ai_calls_allowed(self, now: datetime, scope: str | None = None) -> bool:
        row = self.db.execute(
            "SELECT value FROM weather_ai_agent_meta WHERE key=?",
            (self._ai_circuit_key("ai_circuit_retry_after_utc", scope),),
        ).fetchone()
        retry_after = parse_ts(row[0]) if row else None
        return retry_after is None or now >= retry_after

    def open_ai_circuit(self, now: datetime, error: str, scope: str | None = None) -> None:
        retry_after = now + timedelta(minutes=float(self.config.get("aiCircuitBreakMinutes", 30)))
        for base, value in (
            ("ai_circuit_retry_after_utc", iso_utc(retry_after)),
            ("ai_circuit_last_error", str(error)[:1000]),
        ):
            key = self._ai_circuit_key(base, scope)
            self.db.execute(
                "INSERT INTO weather_ai_agent_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        self.db.commit()

    def clear_ai_circuit(self, scope: str | None = None) -> None:
        self.db.execute(
            "DELETE FROM weather_ai_agent_meta WHERE key IN (?,?)",
            (
                self._ai_circuit_key("ai_circuit_retry_after_utc", scope),
                self._ai_circuit_key("ai_circuit_last_error", scope),
            ),
        )
        self.db.commit()

    def persist_lessons(self, inputs: list[dict[str, Any]], response: dict[str, Any]) -> int:
        reviews = {str(row.get("eventId")): row for row in response.get("reviews") or [] if isinstance(row, dict)}
        expected = {str(row["event"]["event_id"]) for row in inputs}
        if set(reviews) != expected:
            raise RuntimeError("AI lesson response event IDs do not match settled events")
        count = 0
        for item in inputs:
            event_id = str(item["event"]["event_id"])
            cursor = self.db.execute(
                """
                INSERT OR IGNORE INTO weather_ai_agent_lessons(
                    strategy_name,event_id,city,target_date,net_pnl_usdc,lesson_json,created_at_utc
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    self.strategy_name, event_id, item["event"]["city"], item["event"]["target_date"],
                    item["netPnlUsdc"], json.dumps(reviews[event_id], ensure_ascii=False), iso_utc(),
                ),
            )
            count += int(cursor.rowcount > 0)
        return count

    def run_once(self) -> dict[str, Any]:
        now = utc_now()
        if not self.config.get("enabled", True):
            return {
                "run_id": None,
                "due_cycles": 0,
                "message": "AI disabled; data collection active",
            }
        try:
            daily_report = self.maybe_send_daily_report(now)
        except Exception as exc:
            logging.exception("daily Feishu PnL report failed")
            daily_report = {"status": "error", "error": str(exc)[:500]}
        try:
            research_snapshots_written = self.record_research_snapshots(now)
            research_snapshots_resolved = self.refresh_research_snapshots()
            ladder_shadows_written = int(getattr(self, "_last_ladder_shadow_written", 0))
            ladder_shadows_resolved = self.refresh_ladder_shadow_snapshots()
            frozen_ladders_written = int(getattr(self, "_last_frozen_ladder_written", 0))
            frozen_ladders_resolved = self.refresh_frozen_ladder_candidates()
            frozen_portfolios_written = int(getattr(self, "_last_frozen_portfolio_written", 0))
        except Exception:
            logging.exception("research snapshot persistence failed")
            research_snapshots_written = 0
            research_snapshots_resolved = 0
            ladder_shadows_written = 0
            ladder_shadows_resolved = 0
            frozen_ladders_written = 0
            frozen_ladders_resolved = 0
            frozen_portfolios_written = 0
        ai_allowed = self.ai_calls_allowed(now)
        due = self.due_events(now) if ai_allowed else []
        lesson_allowed = ai_allowed and self.lesson_processing_allowed(now)
        lesson_inputs = (
            self.pending_lesson_inputs()
            if lesson_allowed else []
        )
        if lesson_inputs and not self.lesson_retry_allowed(now, lesson_inputs):
            lesson_inputs = []
        settlement_ready = self.db.execute(
            """
            SELECT 1 FROM weather_ai_agent_positions p JOIN market_resolutions r ON r.market_id=p.market_id
            WHERE p.strategy_name=? AND p.shares>0 AND r.is_resolved=1 LIMIT 1
            """,
            (self.strategy_name,),
        ).fetchone()
        opportunity_ready = self.db.execute(
            """
            SELECT 1 FROM weather_ai_opportunity_evaluations o JOIN events e ON e.event_id=o.event_id
            WHERE o.strategy_name=? AND o.resolved_at_utc IS NULL AND e.resolved_at_utc IS NOT NULL LIMIT 1
            """,
            (self.strategy_name,),
        ).fetchone()
        if not due and not lesson_inputs and not settlement_ready and not opportunity_ready:
            return {
                "run_id": None, "due_cycles": 0, "message": "no actionable work",
                "research_snapshots_written": research_snapshots_written,
                "research_snapshots_resolved": research_snapshots_resolved,
                "ladder_shadows_written": ladder_shadows_written,
                "ladder_shadows_resolved": ladder_shadows_resolved,
                "frozen_ladders_written": frozen_ladders_written,
                "frozen_ladders_resolved": frozen_ladders_resolved,
                "frozen_portfolios_written": frozen_portfolios_written,
                "daily_report": daily_report,
            }
        cursor = self.db.execute(
            "INSERT INTO weather_ai_agent_runs(started_at_utc,status,due_cycles) VALUES(?,'running',?)",
            (iso_utc(now), len(due)),
        )
        run_id = int(cursor.lastrowid)
        # Never hold SQLite's writer lock while waiting for Hermes. The collector
        # shares this database and must remain able to persist half-hour samples.
        self.db.commit()
        cycles = actions = fills = lessons = 0
        lesson_error = None
        decision_errors: list[str] = []
        try:
            settlements = self.settle_positions()
            signal_evaluations = self.refresh_signal_evaluations()
            opportunity_evaluations = self.refresh_opportunity_evaluations()
            self.db.commit()
            if due:
                batch_size = max(1, int(self.config.get("maxDecisionEventsPerCall", 1)))
                event_groups = [
                    due[offset:offset + batch_size]
                    for offset in range(0, len(due), batch_size)
                ]

                def persist_group(contexts: list[dict[str, Any]], response: dict[str, Any]) -> None:
                    nonlocal cycles, actions, fills
                    if reason := self._decision_context_expiry_reason(contexts):
                        raise RuntimeError(reason)
                    written_cycles, written_actions, written_fills = self.persist_response(
                        run_id, contexts, response
                    )
                    cycles += written_cycles
                    actions += written_actions
                    fills += written_fills
                    self.mark_scheduled_reviews_covered_by_interrupts(contexts)
                    self.mark_scheduled_review_completed(contexts)
                    self.db.execute(
                        """
                        UPDATE weather_ai_agent_runs SET cycles_written=?,actions_written=?,fills_written=?
                        WHERE run_id=?
                        """,
                        (cycles, actions, fills, run_id),
                    )
                    self.db.commit()

                def defer_group(contexts: list[dict[str, Any]], exc: Exception) -> None:
                    self.db.rollback()
                    message = str(exc)[:1000]
                    decision_errors.append(message)
                    retry_after = iso_utc(utc_now() + timedelta(
                        minutes=float(self.config.get("decisionRetryMinutes", 5))
                    ))
                    max_attempts = int(self.config.get("maxDecisionAttempts", 3))
                    for context in contexts:
                        observer_event_id = (context.get("trigger") or {}).get("observerEventId")
                        if observer_event_id is not None:
                            self.db.execute(
                                """
                                UPDATE weather_observer_events SET
                                    trade_attempts=trade_attempts+1,trade_last_error=?,
                                    trade_retry_after_utc=?,
                                    escalation_status=CASE WHEN trade_attempts+1>=? THEN 'deferred' ELSE 'pending' END
                                WHERE observer_event_id=?
                                """,
                                (message, retry_after, max_attempts, int(observer_event_id)),
                            )
                    self.mark_scheduled_review_failed(contexts, message, retry_after)
                    self.db.commit()
                    logging.error("weather trader decision deferred: %s", message)

                concurrency = max(1, int(self.config.get("decisionConcurrency", 1)))
                if concurrency == 1 or len(event_groups) == 1:
                    for events in event_groups:
                        contexts: list[dict[str, Any]] = []
                        try:
                            contexts = [self.build_context(event) for event in events]
                            persist_group(contexts, self.call_decision_ai(contexts))
                        except Exception as exc:
                            defer_group(contexts, exc)
                            if not self.ai_calls_allowed(utc_now()):
                                break
                else:
                    context_groups = [
                        [self.build_context(event) for event in events]
                        for events in event_groups
                    ]
                    with ThreadPoolExecutor(max_workers=min(concurrency, len(context_groups))) as pool:
                        futures = {
                            pool.submit(self.call_decision_ai, contexts): contexts
                            for contexts in context_groups
                        }
                        for future in as_completed(futures):
                            contexts = futures[future]
                            try:
                                persist_group(contexts, future.result())
                            except Exception as exc:
                                defer_group(contexts, exc)
            lesson_inputs = (
                self.pending_lesson_inputs()
                if lesson_allowed and self.ai_calls_allowed(utc_now())
                and self.lesson_retry_allowed(now, lesson_inputs) else []
            )
            if lesson_inputs:
                try:
                    lessons = self.persist_lessons(lesson_inputs, self.call_lesson_ai(lesson_inputs))
                    self.db.execute(
                        "DELETE FROM weather_ai_agent_meta WHERE key IN "
                        "('lesson_retry_after_utc','lesson_retry_attempts','lesson_retry_signature')"
                    )
                    self.db.commit()
                except Exception as exc:
                    self.db.rollback()
                    lesson_error = str(exc)[:2000]
                    logging.exception("weather AI lesson generation deferred")
                    self.defer_lesson_retry(utc_now(), lesson_inputs)
                    self.db.commit()
            self.db.execute(
                """
                UPDATE weather_ai_agent_runs SET completed_at_utc=?,status='completed',cycles_written=?,
                    actions_written=?,fills_written=?,settlements_updated=?,lessons_written=?,error=? WHERE run_id=?
                """,
                (
                    iso_utc(), cycles, actions, fills, settlements, lessons,
                    " | ".join([item for item in [*decision_errors, lesson_error] if item])[:2000] or None,
                    run_id,
                ),
            )
            self.db.commit()
            return {
                "run_id": run_id, "due_cycles": len(due), "cycles_written": cycles,
                "actions_written": actions, "fills_written": fills,
                "settlements_updated": settlements, "lessons_written": lessons,
                "signal_evaluations_updated": signal_evaluations,
                "opportunity_evaluations_updated": opportunity_evaluations,
                "research_snapshots_written": research_snapshots_written,
                "research_snapshots_resolved": research_snapshots_resolved,
                "ladder_shadows_written": ladder_shadows_written,
                "ladder_shadows_resolved": ladder_shadows_resolved,
                "frozen_ladders_written": frozen_ladders_written,
                "frozen_ladders_resolved": frozen_ladders_resolved,
                "frozen_portfolios_written": frozen_portfolios_written,
                "decision_errors": decision_errors,
                "lesson_error": lesson_error,
                "daily_report": daily_report,
            }
        except Exception as exc:
            self.db.rollback()
            self.db.execute(
                "UPDATE weather_ai_agent_runs SET completed_at_utc=?,status='failed',error=? WHERE run_id=?",
                (iso_utc(), str(exc)[:2000], run_id),
            )
            self.db.commit()
            raise


def configure_logging(config: dict[str, Any]) -> None:
    path = ROOT / str(config["logPath"])
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            RotatingFileHandler(
                path,
                maxBytes=int(config.get("logMaxBytes", 5 * 1024 * 1024)),
                backupCount=int(config.get("logBackupCount", 2)),
                encoding="utf-8",
            ),
        ],
        force=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="METAR-event-driven, direction-neutral AI weather paper agent")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--init-only", action="store_true")
    mode.add_argument("--send-daily-report", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not config.get("paperOnly", True):
        raise SystemExit("live execution is not implemented; paperOnly must remain true")
    configure_logging(config)
    lock_path = ROOT / "data/weather_ai_agent.lock"
    lock_handle = lock_path.open("w")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.info("another weather AI agent process is running")
        return 0
    engine = WeatherAIAgent(config)
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        if args.init_only:
            return 0
        if args.send_daily_report:
            print(json.dumps(engine.maybe_send_daily_report(utc_now(), force=True), ensure_ascii=False, indent=2))
            return 0
        if not args.loop:
            print(json.dumps(engine.run_once(), ensure_ascii=False, indent=2))
            return 0
        while not stop:
            try:
                result = engine.run_once()
                if result.get("run_id") or result.get("research_snapshots_written") or result.get("research_snapshots_resolved"):
                    logging.info("agent run: %s", json.dumps(result, ensure_ascii=False))
            except Exception:
                logging.exception("weather AI agent run failed")
            for _ in range(int(config.get("pollSeconds", 30))):
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
