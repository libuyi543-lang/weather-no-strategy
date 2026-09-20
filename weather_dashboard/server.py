#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT / "data/weather_market_monitor.sqlite3"
DAILY_ACCURACY_PATH = ROOT / "data/weather_monitor/daily_accuracy_latest.json"
DATA_QUALITY_PATH = ROOT / "data/weather_monitor/data_quality_latest.json"
LADDER_HISTORY_PATH = ROOT / "research/output/weather_ladder_microstructure_report.json"
MODEL = "mblue"
EXTERNAL_MODELS = ("ecmwf_ifs025",)
AI_STRATEGY = "weather_no_three_thesis_paper_v1"
NO_PAPER_TYPES = (
    "NO_OVERSHOOT",
    "NO_CEILING",
    "NO_MARKET_TAIL_REJECTION",
)
REPORT_TZ = ZoneInfo("Asia/Shanghai")
LADDER_V4_PORTFOLIO = "ladder_portfolio_v4_1_1100_5_15_5_all_eligible"
LADDER_V4_HISTORY_KEY = "5/15/5"
LADDER_FORWARD_START = "2026-08-07"


def as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def rounded_temperature(value: float | None) -> int | None:
    if value is None:
        return None
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def in_bucket(value: float | None, low: float | None, high: float | None) -> bool:
    if value is None:
        return False
    return (low is None or value >= low) and (high is None or value <= high)


class WeatherDashboardData:
    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db

    @staticmethod
    def latest_events(db: sqlite3.Connection) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
        run = db.execute("SELECT * FROM runs ORDER BY slot_utc DESC LIMIT 1").fetchone()
        if not run:
            return None, []
        ranked_rows = db.execute(
            """
            SELECT er.rank, er.volume_24h, er.liquidity, er.selection_score,
                   e.event_id, e.title, e.city, e.target_date, e.station_id,
                   e.station_name, e.resolution_source, s.timezone
            FROM event_rankings er
            JOIN events e ON e.event_id=er.event_id
            LEFT JOIN stations s ON s.station_id=e.station_id
            WHERE er.run_id=?
              AND lower(e.city) NOT IN ('shenzhen', 'hong kong')
              AND e.city NOT IN ('深圳', '香港')
            ORDER BY CASE WHEN er.rank > 0 THEN 0 ELSE 1 END,
                     er.rank, e.target_date, e.event_id
            """,
            (run["run_id"],),
        ).fetchall()
        rows: list[sqlite3.Row] = []
        seen_cities: set[str] = set()
        for row in ranked_rows:
            city_key = str(row["city"] or "").strip().casefold()
            if not city_key or city_key in seen_cities:
                continue
            seen_cities.add(city_key)
            rows.append(row)
        return run, rows

    @staticmethod
    def forecast_rows(db: sqlite3.Connection, station_id: str, target_date: str) -> list[sqlite3.Row]:
        return db.execute(
            """
            SELECT slot_utc, fetched_at_utc, model, model_ref_time_utc, model_updated_at_utc,
                   forecast_step_hours, forecast_max_c, forecast_max_f, forecast_peak_local,
                   point_count, points_json, sample_local_date, sample_local_hour,
                   sample_local_offset, status, error
            FROM windy_forecasts
            WHERE station_id=? AND target_date=? AND model=? AND status='ok'
            ORDER BY slot_utc DESC LIMIT 48
            """,
            (station_id, target_date, MODEL),
        ).fetchall()

    @staticmethod
    def latest_market_rows(db: sqlite3.Connection, event_id: str) -> list[sqlite3.Row]:
        return db.execute(
            """
            WITH recent_slots AS (
                SELECT slot_utc FROM market_snapshots
                WHERE event_id=? GROUP BY slot_utc
                ORDER BY slot_utc DESC LIMIT 2
            ), ranked AS (
                SELECT ms.*,DENSE_RANK() OVER (ORDER BY ms.slot_utc DESC) AS slot_rank
                FROM market_snapshots ms
                WHERE ms.event_id=? AND ms.slot_utc IN (SELECT slot_utc FROM recent_slots)
            )
            SELECT m.market_id,m.outcome_range,m.bucket_low,m.bucket_high,m.bucket_unit,
                   MAX(CASE WHEN ranked.slot_rank=1 THEN ranked.slot_utc END) AS slot_utc,
                   MAX(CASE WHEN ranked.slot_rank=1 THEN ranked.gamma_yes_price END) AS gamma_yes_price,
                   MAX(CASE WHEN ranked.slot_rank=1 THEN ranked.gamma_no_price END) AS gamma_no_price,
                   MAX(CASE WHEN ranked.slot_rank=1 THEN ranked.yes_best_bid END) AS yes_best_bid,
                   MAX(CASE WHEN ranked.slot_rank=1 THEN ranked.yes_best_ask END) AS yes_best_ask,
                   MAX(CASE WHEN ranked.slot_rank=1 THEN ranked.no_best_bid END) AS no_best_bid,
                   MAX(CASE WHEN ranked.slot_rank=1 THEN ranked.no_best_ask END) AS no_best_ask,
                   MAX(CASE WHEN ranked.slot_rank=2 THEN ranked.gamma_yes_price END) AS prior_yes_price
            FROM markets m
            JOIN ranked ON ranked.market_id=m.market_id
            WHERE m.event_id=?
            GROUP BY m.market_id,m.outcome_range,m.bucket_low,m.bucket_high,m.bucket_unit
            ORDER BY COALESCE(m.bucket_low, -999), COALESCE(m.bucket_high, 999)
            """,
            (event_id, event_id, event_id),
        ).fetchall()

    def city_summary(self, db: sqlite3.Connection, event: sqlite3.Row) -> dict[str, Any]:
        forecasts = self.forecast_rows(db, event["station_id"], event["target_date"])
        current_forecast = forecasts[0] if forecasts else None
        previous_forecast = forecasts[1] if len(forecasts) > 1 else None
        max_c = as_float(current_forecast["forecast_max_c"]) if current_forecast else None
        previous_c = as_float(previous_forecast["forecast_max_c"]) if previous_forecast else None
        delta_c = max_c - previous_c if max_c is not None and previous_c is not None else None
        display_temp = rounded_temperature(max_c)

        markets = self.latest_market_rows(db, event["event_id"])
        predicted_market = next(
            (
                row
                for row in markets
                if in_bucket(display_temp, as_float(row["bucket_low"]), as_float(row["bucket_high"]))
            ),
            None,
        )
        mode_market = max(markets, key=lambda row: as_float(row["gamma_yes_price"]) or -1) if markets else None
        predicted_price = as_float(predicted_market["gamma_yes_price"]) if predicted_market else None
        prior = as_float(predicted_market["prior_yes_price"]) if predicted_market else None
        price_delta = predicted_price - prior if predicted_price is not None and prior is not None else None
        forecast_spark = [
            {"time": row["slot_utc"], "value": as_float(row["forecast_max_c"])}
            for row in reversed(forecasts[:16])
        ]
        baseline = None
        try:
            local_tz = ZoneInfo(event["timezone"] or "UTC")
            target = datetime.fromisoformat(event["target_date"]).date()
            canonical_local = datetime(target.year, target.month, target.day, 7, 30, tzinfo=local_tz)
            exact_rows = [
                row for row in forecasts
                if row["sample_local_date"] == event["target_date"]
                and row["sample_local_hour"] == 7
                and datetime.fromisoformat(row["slot_utc"]).astimezone(local_tz).minute == 30
            ]
            if exact_rows:
                chosen = min(
                    exact_rows,
                    key=lambda row: abs(
                        (datetime.fromisoformat(row["slot_utc"]) - canonical_local.astimezone(timezone.utc)).total_seconds()
                    ),
                )
                baseline = {
                    "raw_c": as_float(chosen["forecast_max_c"]),
                    "display_c": rounded_temperature(as_float(chosen["forecast_max_c"])),
                    "slot_utc": chosen["slot_utc"],
                    "local_time": datetime.fromisoformat(chosen["slot_utc"]).astimezone(local_tz).isoformat(timespec="minutes"),
                    "updated_at": chosen["model_updated_at_utc"],
                    "status": chosen["status"],
                }
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            baseline = None

        return {
            "rank": event["rank"],
            "event_id": event["event_id"],
            "title": event["title"],
            "city": event["city"],
            "target_date": event["target_date"],
            "station_id": event["station_id"],
            "station_name": event["station_name"],
            "timezone": event["timezone"],
            "volume_24h": as_float(event["volume_24h"]),
            "liquidity": as_float(event["liquidity"]),
            "forecast": {
                "raw_c": max_c,
                "display_c": display_temp,
                "delta_c": delta_c,
                "peak_local": current_forecast["forecast_peak_local"] if current_forecast else None,
                "slot_utc": current_forecast["slot_utc"] if current_forecast else None,
                "updated_at": current_forecast["model_updated_at_utc"] if current_forecast else None,
                "step_hours": as_float(current_forecast["forecast_step_hours"]) if current_forecast else None,
                "point_count": current_forecast["point_count"] if current_forecast else 0,
                "status": current_forecast["status"] if current_forecast else "missing",
                "spark": forecast_spark,
                "baseline_0730": baseline,
            },
            "market": {
                "predicted_bucket": predicted_market["outcome_range"] if predicted_market else None,
                "predicted_yes": predicted_price,
                "price_delta": price_delta,
                "mode_bucket": mode_market["outcome_range"] if mode_market else None,
                "mode_yes": as_float(mode_market["gamma_yes_price"]) if mode_market else None,
                "slot_utc": mode_market["slot_utc"] if mode_market else None,
            },
        }

    def overview(self) -> dict[str, Any]:
        with closing(self.connect()) as db:
            run, events = self.latest_events(db)
            cities = [self.city_summary(db, event) for event in events]
        forecasted = [city for city in cities if city["forecast"]["raw_c"] is not None]
        changed = [city for city in cities if abs(city["forecast"]["delta_c"] or 0) >= 0.005]
        price_changed = [city for city in cities if abs(city["market"]["price_delta"] or 0) >= 0.0005]
        latest_update = max(
            (city["forecast"]["updated_at"] for city in forecasted if city["forecast"]["updated_at"]),
            default=None,
        )
        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": MODEL,
            "latest_run": dict(run) if run else None,
            "summary": {
                "city_count": len(cities),
                "forecasted_count": len(forecasted),
                "changed_count": len(changed),
                "price_changed_count": len(price_changed),
                "latest_model_update": latest_update,
                "average_max_c": (
                    sum(city["forecast"]["raw_c"] for city in forecasted) / len(forecasted)
                    if forecasted
                    else None
                ),
            },
            "cities": cities,
        }

    def city_detail(self, event_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            event = db.execute(
                """
                SELECT e.event_id, e.title, e.city, e.target_date, e.station_id, e.station_name,
                       e.resolution_source, s.timezone
                FROM events e LEFT JOIN stations s ON s.station_id=e.station_id
                WHERE e.event_id=?
                """,
                (event_id,),
            ).fetchone()
            if not event:
                return None
            forecasts = self.forecast_rows(db, event["station_id"], event["target_date"])
            forecast_history = [
                {
                    "time": row["slot_utc"],
                    "max_c": as_float(row["forecast_max_c"]),
                    "peak_local": row["forecast_peak_local"],
                    "updated_at": row["model_updated_at_utc"],
                }
                for row in reversed(forecasts)
            ]
            hourly_profile: list[dict[str, Any]] = []
            if forecasts:
                try:
                    hourly_profile = json.loads(forecasts[0]["points_json"] or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    hourly_profile = []

            markets = self.latest_market_rows(db, event_id)
            market_rows: list[dict[str, Any]] = []
            price_history: list[dict[str, Any]] = []
            for market in markets:
                history = db.execute(
                    """
                    SELECT slot_utc, gamma_yes_price, yes_best_bid, yes_best_ask
                    FROM market_snapshots WHERE market_id=?
                    ORDER BY slot_utc DESC LIMIT 72
                    """,
                    (market["market_id"],),
                ).fetchall()
                series = [
                    {"time": row["slot_utc"], "value": as_float(row["gamma_yes_price"])}
                    for row in reversed(history)
                    if as_float(row["gamma_yes_price"]) is not None
                ]
                latest_price = as_float(market["gamma_yes_price"])
                prior_price = series[-2]["value"] if len(series) > 1 else None
                market_rows.append(
                    {
                        "market_id": market["market_id"],
                        "bucket": market["outcome_range"],
                        "low": as_float(market["bucket_low"]),
                        "high": as_float(market["bucket_high"]),
                        "unit": market["bucket_unit"],
                        "yes": latest_price,
                        "yes_bid": as_float(market["yes_best_bid"]),
                        "yes_ask": as_float(market["yes_best_ask"]),
                        "delta": latest_price - prior_price if latest_price is not None and prior_price is not None else None,
                    }
                )
                price_history.append(
                    {
                        "market_id": market["market_id"],
                        "bucket": market["outcome_range"],
                        "latest": latest_price,
                        "points": series,
                    }
                )

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": MODEL,
            "event": dict(event),
            "forecast_history": forecast_history,
            "hourly_profile": hourly_profile,
            "markets": market_rows,
            "price_history": price_history,
        }

    @staticmethod
    def daily_accuracy() -> dict[str, Any]:
        if not DAILY_ACCURACY_PATH.exists():
            return {
                "generated_at_utc": None,
                "summary": {"records": 0, "resolved": 0, "pending": 0, "accurate": 0, "inaccurate": 0},
                "records": [],
            }
        payload = json.loads(DAILY_ACCURACY_PATH.read_text(encoding="utf-8"))
        records = [
            row
            for row in payload.get("records", [])
            if str(row.get("city", "")).casefold() not in {"shenzhen", "hong kong", "深圳", "香港"}
        ]
        payload["records"] = records
        return payload

    def data_quality(self) -> dict[str, Any]:
        """Return the latest collection run with model/observation coverage details."""
        if not DATA_QUALITY_PATH.exists():
            return {
                "generated_at_utc": None,
                "run_id": None,
                "summary": {"cities": 0, "complete_cities": 0, "incomplete_cities": 0},
                "cities": [],
            }
        try:
            payload = json.loads(DATA_QUALITY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {"generated_at_utc": None, "summary": {}, "cities": []}

        report_cities = payload.get("cities") or []
        run_id = payload.get("run_id")
        with closing(self.connect()) as db:
            if run_id is None:
                row = db.execute("SELECT run_id FROM runs ORDER BY slot_utc DESC LIMIT 1").fetchone()
                run_id = row[0] if row else None
            external: dict[tuple[str, str], dict[str, Any]] = {}
            observations: dict[tuple[str, str], dict[str, Any]] = {}
            raw_counts: dict[str, int] = {}
            raw_by_station: dict[str, set[int]] = {}
            slot = None
            if run_id is not None:
                ext_rows = db.execute(
                    """
                    SELECT station_id,target_date,model,forecast_max_c,forecast_peak_local,
                           point_count,status,error,slot_utc,raw_payload_id
                    FROM external_forecasts WHERE run_id=? ORDER BY slot_utc DESC
                    """, (run_id,)
                ).fetchall()
                for row in ext_rows:
                    key = (row["station_id"], row["target_date"], row["model"])
                    external[key] = dict(row)
                    slot = max(slot or row["slot_utc"], row["slot_utc"])
                obs_rows = db.execute(
                    """
                    SELECT station_id,sample_local_date,source,temperature_c,observed_daily_max_c,
                           observation_time_utc,status,error,slot_utc,raw_payload_id
                    FROM weather_observations WHERE run_id=? ORDER BY slot_utc DESC
                    """, (run_id,)
                ).fetchall()
                for row in obs_rows:
                    key = (row["station_id"], row["sample_local_date"], row["source"])
                    observations[key] = dict(row)
                    slot = max(slot or row["slot_utc"], row["slot_utc"])
                raw_rows = db.execute(
                    "SELECT payload_id,station_id,source,status FROM raw_weather_payloads WHERE run_id=?",
                    (run_id,),
                ).fetchall()
                for row in raw_rows:
                    key = f"{row['source']}:{row['status']}"
                    raw_counts[key] = raw_counts.get(key, 0) + 1
                    if row["status"] == "ok":
                        raw_by_station.setdefault(row["station_id"], set()).add(int(row["payload_id"]))

        enriched: list[dict[str, Any]] = []
        for city in report_cities:
            station = city.get("station_id")
            target_date = city.get("target_date")
            models = []
            for model in EXTERNAL_MODELS:
                row = external.get((station, target_date, model), {})
                models.append({
                    "model": model,
                    "max_c": as_float(row.get("forecast_max_c")),
                    "peak_local": row.get("forecast_peak_local"),
                    "status": row.get("status") or ("ok" if row else "missing"),
                    "error": row.get("error"),
                    "slot_utc": row.get("slot_utc"),
                    "raw_payload_id": row.get("raw_payload_id"),
                })
            metar = observations.get((station, target_date, "metar"), {})
            current = observations.get((station, target_date, "open_meteo_current"), {})
            ext_values = [item["max_c"] for item in models if item["max_c"] is not None]
            enriched.append({
                **city,
                "external_models": models,
                "external_range_c": [min(ext_values), max(ext_values)] if ext_values else [None, None],
                "observations": {
                    "metar": {
                        "temperature_c": as_float(metar.get("temperature_c")),
                        "daily_max_c": as_float(metar.get("observed_daily_max_c")),
                        "status": metar.get("status") or "missing",
                        "observation_time_utc": metar.get("observation_time_utc"),
                        "raw_payload_id": metar.get("raw_payload_id"),
                    },
                    "open_meteo_current": {
                        "temperature_c": as_float(current.get("temperature_c")),
                        "daily_max_c": as_float(current.get("observed_daily_max_c")),
                        "status": current.get("status") or "missing",
                        "observation_time_utc": current.get("observation_time_utc"),
                        "raw_payload_id": current.get("raw_payload_id"),
                    },
                },
                "raw_payload_count": len(raw_by_station.get(station, set())),
            })

        summary = dict(payload.get("summary") or {})
        summary.update({
            "cities": len(enriched),
            "complete_cities": sum(1 for city in enriched if city.get("complete")),
            "incomplete_cities": sum(1 for city in enriched if not city.get("complete")),
            "meteoblue_ok": sum(1 for city in enriched if city.get("meteoblue_max_c") is not None),
            "external_models_expected": len(enriched) * len(EXTERNAL_MODELS),
            "external_models_ok": sum(1 for city in enriched for model in city["external_models"] if model["status"] == "ok"),
            "metar_ok": sum(1 for city in enriched if city["observations"]["metar"]["status"] == "ok"),
            "open_meteo_current_ok": sum(1 for city in enriched if city["observations"]["open_meteo_current"]["status"] == "ok"),
            "raw_payloads_ok": sum(raw_counts.get(f"{source}:ok", 0) for source in ("windy_mblue", "open_meteo_multi", "aviationweather_metar")),
        })
        return {
            "generated_at_utc": payload.get("generated_at_utc"),
            "run_id": run_id,
            "slot_utc": slot or payload.get("slot_utc"),
            "summary": summary,
            "cities": enriched,
        }

    def ai_system(self) -> dict[str, Any]:
        """Return recent AI decisions, requested actions, fills, and account state."""
        with closing(self.connect()) as db:
            cycle_rows = db.execute(
                """
                SELECT cycle_id,event_id,city,target_date,metar_observation_time_utc,
                       trigger_slot_utc,
                       primary_metar_observation_time_utc,decision_trigger_type,
                       decision_trigger_time_utc,analyzed_at_utc,status,state_assessment,
                       model_reality_gap,remaining_heating_assessment,
                       market_consensus_assessment,weather_process_assessment,
                       model_correction_assessment,process_confidence,
                       market_decision_mode,ridge_v2_state_json,market_alignment_json,
                       temperature_thesis,uncertainty_assessment,next_review_reason
                FROM weather_ai_agent_cycles
                WHERE strategy_name=?
                ORDER BY analyzed_at_utc DESC,cycle_id DESC LIMIT 60
                """,
                (AI_STRATEGY,),
            ).fetchall()
            cycle_ids = [int(row["cycle_id"]) for row in cycle_rows]
            action_rows: list[sqlite3.Row] = []
            fill_rows: list[sqlite3.Row] = []
            if cycle_ids:
                placeholders = ",".join("?" for _ in cycle_ids)
                action_rows = db.execute(
                    f"""
                    SELECT action_id,cycle_id,action_index,requested_action,executed_action,
                           market_id,outcome_range,outcome_side,requested_shares,executed_shares,
                           execution_price,notional_usdc,fee_usdc,market_implied_probability,
                           consensus_position,evidence_strength,entry_type,heating_process_status,
                           outcome_assessment,probability_band,price_assessment,
                           thesis,evidence_json,key_risk,invalidation_condition,rejection_reason,
                           analysis_market_snapshot_at_utc,execution_market_snapshot_at_utc,
                           execution_market_age_minutes,execution_checked_at_utc,
                           why_market_may_be_right,why_market_may_be_wrong,
                           new_evidence_since_prior_json
                    FROM weather_ai_agent_actions
                    WHERE cycle_id IN ({placeholders})
                    ORDER BY cycle_id DESC,action_index
                    """,
                    cycle_ids,
                ).fetchall()
                action_ids = [int(row["action_id"]) for row in action_rows]
                if action_ids:
                    fill_placeholders = ",".join("?" for _ in action_ids)
                    fill_rows = db.execute(
                        f"""
                        SELECT fill_id,action_id,side,outcome_side,shares,price,notional_usdc,
                               fee_usdc,realized_pnl_usdc,filled_at_utc
                        FROM weather_ai_agent_fills
                        WHERE action_id IN ({fill_placeholders}) ORDER BY filled_at_utc
                        """,
                        action_ids,
                    ).fetchall()
            position_rows = db.execute(
                """
                SELECT event_id,market_id,city,outcome_range,outcome_side,shares,cost_basis_usdc,
                       realized_pnl_usdc,status,opened_at_utc,updated_at_utc,closed_at_utc,final_outcome
                FROM weather_ai_agent_positions WHERE strategy_name=? ORDER BY city,outcome_range
                """,
                (AI_STRATEGY,),
            ).fetchall()
            recent_fills = db.execute(
                """
                SELECT f.fill_id,f.action_id,f.event_id,f.market_id,f.city,f.fill_type,
                       f.side,f.outcome_side,f.shares,f.price,f.notional_usdc,f.fee_usdc,
                       f.realized_pnl_usdc,f.filled_at_utc,a.outcome_range,
                       COALESCE(a.entry_type,(
                           SELECT a2.entry_type
                           FROM weather_ai_agent_fills f2
                           JOIN weather_ai_agent_actions a2 ON a2.action_id=f2.action_id
                           WHERE f2.strategy_name=f.strategy_name AND f2.market_id=f.market_id
                             AND f2.side='BUY'
                           ORDER BY f2.filled_at_utc LIMIT 1
                       )) AS entry_type
                FROM weather_ai_agent_fills f
                LEFT JOIN weather_ai_agent_actions a ON a.action_id=f.action_id
                WHERE f.strategy_name=? ORDER BY f.filled_at_utc DESC LIMIT 120
                """,
                (AI_STRATEGY,),
            ).fetchall()
            paper_entry_rows = db.execute(
                """
                SELECT a.entry_type,f.market_id,f.shares,f.notional_usdc,f.fee_usdc,
                       r.is_resolved,r.winning_outcome,r.no_final_price
                FROM weather_ai_agent_fills f
                JOIN weather_ai_agent_actions a ON a.action_id=f.action_id
                LEFT JOIN market_resolutions r ON r.market_id=f.market_id
                WHERE f.strategy_name=? AND f.fill_type='paper' AND f.side='BUY'
                  AND a.entry_type IN (?,?,?)
                ORDER BY f.filled_at_utc
                """,
                (AI_STRATEGY, *NO_PAPER_TYPES),
            ).fetchall()
            schedule_rows = db.execute(
                """
                SELECT r.event_id,e.city,r.review_slot_utc,r.review_type,r.status,
                       r.attempts,r.retry_after_utc,r.last_error
                FROM weather_ai_agent_scheduled_reviews r JOIN events e ON e.event_id=r.event_id
                WHERE r.strategy_name=? ORDER BY r.review_slot_utc DESC,e.city LIMIT 60
                """,
                (AI_STRATEGY,),
            ).fetchall()
            run = db.execute(
                """
                SELECT run_id,started_at_utc,completed_at_utc,status,due_cycles,cycles_written,
                       actions_written,fills_written,error
                FROM weather_ai_agent_runs ORDER BY run_id DESC LIMIT 1
                """
            ).fetchone()
            initial_row = db.execute(
                "SELECT value FROM weather_ai_agent_meta WHERE key=?",
                (f"initial_cash_usdc:{AI_STRATEGY}",),
            ).fetchone()

        fills_by_action: dict[int, list[dict[str, Any]]] = {}
        for row in fill_rows:
            fills_by_action.setdefault(int(row["action_id"]), []).append(dict(row))
        actions_by_cycle: dict[int, list[dict[str, Any]]] = {}
        for row in action_rows:
            item = dict(row)
            item["evidence"] = self._json_value(item.pop("evidence_json"), [])
            item["new_evidence_since_prior"] = self._json_value(
                item.pop("new_evidence_since_prior_json"), []
            )
            item["fills"] = fills_by_action.get(int(item["action_id"]), [])
            actions_by_cycle.setdefault(int(item["cycle_id"]), []).append(item)

        cycles = []
        for row in cycle_rows:
            item = dict(row)
            item["ridge_v2"] = self._json_value(item.pop("ridge_v2_state_json"), {})
            item["market_alignment"] = self._json_value(item.pop("market_alignment_json"), {})
            item["actions"] = actions_by_cycle.get(int(item["cycle_id"]), [])
            cycles.append(item)

        local_today = datetime.now(REPORT_TZ).date()

        def is_today(value: Any) -> bool:
            if not value:
                return False
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(REPORT_TZ).date() == local_today
            except (TypeError, ValueError):
                return False

        positions = [dict(row) for row in position_rows]
        open_positions = [row for row in positions if as_float(row.get("shares")) and as_float(row.get("shares")) > 0]
        realized = sum(as_float(row.get("realized_pnl_usdc")) or 0 for row in positions)
        open_cost = sum(as_float(row.get("cost_basis_usdc")) or 0 for row in open_positions)
        initial_cash = as_float(initial_row[0]) if initial_row else 20.0
        latest_cycle = cycles[0] if cycles else None
        mode_counts: dict[str, int] = {}
        for cycle in cycles:
            if is_today(cycle.get("analyzed_at_utc")):
                mode = str(cycle.get("market_decision_mode") or "UNKNOWN")
                mode_counts[mode] = mode_counts.get(mode, 0) + 1
        today_fills = [dict(row) for row in recent_fills if is_today(row["filled_at_utc"])]
        paper_breakdown = {
            entry_type: {
                "entry_type": entry_type,
                "signals": 0,
                "settled": 0,
                "wins": 0,
                "open_cost_usdc": 0.0,
                "realized_pnl_usdc": 0.0,
            }
            for entry_type in NO_PAPER_TYPES
        }
        for row in paper_entry_rows:
            item = paper_breakdown[str(row["entry_type"])]
            item["signals"] += 1
            shares = as_float(row["shares"]) or 0.0
            notional = (as_float(row["notional_usdc"]) or 0.0) + (as_float(row["fee_usdc"]) or 0.0)
            if int(row["is_resolved"] or 0) != 1:
                item["open_cost_usdc"] += notional
                continue
            winning_side = str(row["winning_outcome"] or "").upper()
            if winning_side not in {"YES", "NO"}:
                winning_side = "NO" if (as_float(row["no_final_price"]) or 0.0) >= 0.99 else "YES"
            won = winning_side == "NO"
            item["settled"] += 1
            item["wins"] += int(won)
            item["realized_pnl_usdc"] += (shares if won else 0.0) - notional
        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "strategy": AI_STRATEGY,
            "paper_only": True,
            "latest_run": dict(run) if run else None,
            "summary": {
                "latest_mode": latest_cycle.get("market_decision_mode") if latest_cycle else None,
                "latest_city": latest_cycle.get("city") if latest_cycle else None,
                "latest_decision_at_utc": latest_cycle.get("analyzed_at_utc") if latest_cycle else None,
                "cycles_today": sum(1 for cycle in cycles if is_today(cycle.get("analyzed_at_utc"))),
                "fills_today": len(today_fills),
                "open_positions": len(open_positions),
                "initial_cash_usdc": initial_cash,
                "available_cash_usdc": (initial_cash or 0) + realized - open_cost,
                "open_cost_basis_usdc": open_cost,
                "realized_pnl_usdc": realized,
                "mode_counts": mode_counts,
            },
            "cycles": cycles,
            "positions": open_positions,
            "recent_fills": [dict(row) for row in recent_fills[:40]],
            "paper_breakdown": list(paper_breakdown.values()),
            "scheduled_reviews": [dict(row) for row in schedule_rows],
        }

    def dual_strategy(self) -> dict[str, Any]:
        """Return the Hermes paper-only dual-strategy audit stream."""
        strategy_name = "weather_dual_strategy_paper_v1"
        generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
        empty = {
            "generated_at_utc": generated,
            "enabled": True,
            "paper_only": True,
            "model": "gpt-5.6-sol",
            "strategy_name": strategy_name,
            "latest_run": {"status": "not_started", "last_review_at_utc": None, "last_error": None},
            "summary": {"reviews_today": 0, "three_bucket_reviews_today": 0, "single_no_reviews_today": 0,
                        "fills_today": 0, "open_positions": 0, "open_cost_usdc": 0.0, "realized_pnl_usdc": 0.0},
            "strategies": [
                {"strategy_type": "THREE_BUCKET", "label": "三桶 YES", "reviews": 0, "fills": 0, "open_cost_usdc": 0.0, "realized_pnl_usdc": 0.0},
                {"strategy_type": "SINGLE_NO", "label": "单桶 NO", "reviews": 0, "fills": 0, "open_cost_usdc": 0.0, "realized_pnl_usdc": 0.0},
            ],
            "reviews": [], "actions": [], "fills": [], "positions": [], "outcome_reviews": [],
        }
        with closing(self.connect()) as db:
            tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "weather_dual_reviews" not in tables:
                return empty
            reviews = [dict(row) for row in db.execute(
                """SELECT review_id,event_id,city,target_date,trigger_type,trigger_time_utc,
                          reviewed_at_utc,status,error FROM weather_dual_reviews
                   WHERE strategy_name=? ORDER BY reviewed_at_utc DESC,review_id DESC LIMIT 40""",
                (strategy_name,),
            ).fetchall()]
            actions = [dict(row) for row in db.execute(
                """SELECT action_id,review_id,event_id,city,strategy_type,market_id,outcome_range,
                          outcome_side,thesis,skew,sizing_tier,requested_shares,executed_shares,
                          execution_price,notional_usdc,fee_usdc,executed_action,rejection_reason,created_at_utc
                   FROM weather_dual_actions WHERE strategy_name=?
                   ORDER BY created_at_utc DESC,action_id DESC LIMIT 80""", (strategy_name,),
            ).fetchall()]
            fills = [dict(row) for row in db.execute(
                """SELECT f.fill_id,f.action_id,f.event_id,f.city,f.market_id,f.outcome_side,
                          f.strategy_type,f.shares,f.price,f.notional_usdc,f.fill_type,
                          f.fee_usdc,f.realized_pnl_usdc,f.filled_at_utc,a.outcome_range,a.thesis,a.sizing_tier
                   FROM weather_dual_fills f
                   LEFT JOIN weather_dual_actions a ON a.action_id=f.action_id
                   WHERE f.strategy_name=?
                   ORDER BY f.filled_at_utc DESC,f.fill_id DESC LIMIT 80""", (strategy_name,),
            ).fetchall()]
            positions = [dict(row) for row in db.execute(
                """SELECT event_id,city,market_id,outcome_range,outcome_side,strategy_type,
                          thesis,shares,cost_basis_usdc,entry_count,opened_at_utc,updated_at_utc,
                          settled_at_utc,realized_pnl_usdc
                   FROM weather_dual_positions WHERE strategy_name=? AND shares>0
                   ORDER BY updated_at_utc DESC""", (strategy_name,),
            ).fetchall()]
            outcome_reviews = []
            if "weather_dual_outcome_reviews" in tables:
                outcome_reviews = [dict(row) for row in db.execute(
                    """SELECT outcome_review_id,event_id,city,target_date,net_pnl_usdc,
                              review_json,created_at_utc
                       FROM weather_dual_outcome_reviews WHERE strategy_name=?
                       ORDER BY created_at_utc DESC LIMIT 20""",
                    (strategy_name,),
                ).fetchall()]
                for row in outcome_reviews:
                    try:
                        row["review"] = json.loads(row.pop("review_json"))
                    except (TypeError, json.JSONDecodeError):
                        row["review"] = {}
        today = datetime.now(REPORT_TZ).date()
        def is_today(value: Any) -> bool:
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(REPORT_TZ).date() == today
            except (TypeError, ValueError):
                return False
        today_reviews = [row for row in reviews if is_today(row.get("reviewed_at_utc"))]
        today_fills = [row for row in fills if is_today(row.get("filled_at_utc"))]
        strategies = []
        for kind, label in (("THREE_BUCKET", "三桶 YES"), ("SINGLE_NO", "单桶 NO")):
            kind_fills = [row for row in fills if row.get("strategy_type") == kind]
            kind_positions = [row for row in positions if row.get("strategy_type") == kind]
            strategies.append({"strategy_type": kind, "label": label,
                               "reviews": sum(1 for row in today_reviews if any(
                                   action.get("strategy_type") == kind and action.get("review_id") == row.get("review_id") for action in actions)),
                               "fills": sum(1 for row in today_fills if row.get("strategy_type") == kind),
                               "open_cost_usdc": sum(as_float(row.get("cost_basis_usdc")) or 0 for row in kind_positions),
                               "realized_pnl_usdc": sum(as_float(row.get("realized_pnl_usdc")) or 0 for row in kind_fills)})
        last = reviews[0] if reviews else None
        return {"generated_at_utc": generated, "enabled": True, "paper_only": True,
                "model": "gpt-5.6-sol", "strategy_name": strategy_name,
                "latest_run": {"status": "running" if last else "waiting", "last_review_at_utc": last.get("reviewed_at_utc") if last else None,
                                "last_error": last.get("error") if last and last.get("status") == "error" else None},
                "summary": {"reviews_today": len(today_reviews),
                            "three_bucket_reviews_today": strategies[0]["reviews"], "single_no_reviews_today": strategies[1]["reviews"],
                            "fills_today": len(today_fills), "open_positions": len(positions),
                            "open_cost_usdc": sum(as_float(row.get("cost_basis_usdc")) or 0 for row in positions),
                            "realized_pnl_usdc": sum(as_float(row.get("realized_pnl_usdc")) or 0 for row in fills)},
                "strategies": strategies, "reviews": reviews, "actions": actions, "fills": fills,
                "positions": positions, "outcome_reviews": outcome_reviews}

    def ladder_v4(self) -> dict[str, Any]:
        """Return the frozen V4 forward ledger and its historical research benchmark."""
        with closing(self.connect()) as db:
            rows = db.execute(
                """
                SELECT s.target_date,s.frozen_slot_utc,s.selected_at_utc,
                       s.eligible_candidates,s.selection_status,s.rejection_reasons_json,
                       s.event_id,s.city,s.selected_cost_usdc,s.selected_notional_usdc,
                       s.selected_fee_usdc,s.resolved_at_utc,s.winning_range,s.payout_usdc,
                       s.hypothetical_pnl_usdc,v.legs_json,v.lower_weight,v.center_weight,
                       v.upper_weight,c.lower_bucket_c,c.center_bucket_c,c.upper_bucket_c,
                       c.lower_spread,c.center_spread,c.upper_spread
                FROM weather_ladder_frozen_portfolio_selections s
                LEFT JOIN weather_ladder_frozen_variants v ON v.variant_id=s.variant_id
                LEFT JOIN weather_ladder_frozen_candidates c ON c.candidate_id=s.candidate_id
                WHERE s.portfolio_version=? AND s.target_date>=?
                ORDER BY s.target_date DESC
                """,
                (LADDER_V4_PORTFOLIO, LADDER_FORWARD_START),
            ).fetchall()
            candidate_rows = db.execute(
                """
                SELECT target_date,COUNT(*) candidate_count,
                       SUM(CASE WHEN eligibility_status='ELIGIBLE_SHADOW' THEN 1 ELSE 0 END) eligible_count
                FROM weather_ladder_frozen_candidates
                WHERE target_date>=? GROUP BY target_date
                """,
                (LADDER_FORWARD_START,),
            ).fetchall()

        candidates_by_date = {
            str(row["target_date"]): {
                "candidate_count": int(row["candidate_count"] or 0),
                "eligible_count": int(row["eligible_count"] or 0),
            }
            for row in candidate_rows
        }
        records = []
        records_by_date: dict[str, list[dict[str, Any]]] = {}
        completed_pnls: list[float] = []
        total_cost = 0.0
        selected_dates = no_eligible_dates = unresolved_dates = profitable_dates = 0
        outcome_counts = {"lower": 0, "center": 0, "upper": 0, "outside": 0}
        for row in rows:
            item = dict(row)
            legs = self._json_value(item.pop("legs_json"), [])
            reasons = self._json_value(item.pop("rejection_reasons_json"), [])
            status = str(item.get("selection_status") or "")
            completed = status == "NO_ELIGIBLE_SHADOW" or (
                item.get("resolved_at_utc") is not None
                and as_float(item.get("hypothetical_pnl_usdc")) is not None
            )
            outcome_category = None
            if status == "SELECTED_SHADOW" and completed:
                outcome_category = "outside"
                for index, leg in enumerate(legs):
                    if str(leg.get("outcomeRange")) == str(item.get("winning_range")):
                        outcome_category = ("lower", "center", "upper")[index]
                        break
                outcome_counts[outcome_category] += 1
            item.update({
                "legs": legs,
                "rejection_reasons": reasons,
                "completed": completed,
                "outcome_category": outcome_category,
                **candidates_by_date.get(str(item.get("target_date")), {
                    "candidate_count": 0, "eligible_count": 0,
                }),
            })
            records.append(item)
            records_by_date.setdefault(str(item.get("target_date")), []).append(item)

        for daily_records in records_by_date.values():
            selected_records = [
                item for item in daily_records
                if item.get("selection_status") == "SELECTED_SHADOW"
            ]
            if not selected_records:
                no_eligible_dates += 1
                completed_pnls.append(0.0)
                continue
            selected_dates += 1
            if any(not item.get("completed") for item in selected_records):
                unresolved_dates += 1
                continue
            daily_cost = sum(as_float(item.get("selected_cost_usdc")) or 0.0 for item in selected_records)
            daily_pnl = sum(as_float(item.get("hypothetical_pnl_usdc")) or 0.0 for item in selected_records)
            total_cost += daily_cost
            completed_pnls.append(daily_pnl)
            profitable_dates += int(daily_pnl > 0)

        total_pnl = sum(completed_pnls)
        completed_dates = len(completed_pnls)
        if completed_dates < 10:
            phase, checkpoint = "DATA_QUALITY_ONLY", 10
        elif completed_dates < 15:
            phase, checkpoint = "WAIT_FOR_INTERIM", 15
        elif completed_dates < 30:
            phase, checkpoint = "INTERIM_ONLY_NO_RULE_CHANGES", 30
        else:
            phase, checkpoint = "FORMAL_REVIEW_DUE", completed_dates

        local_now = datetime.now(REPORT_TZ)
        local_date = local_now.date().isoformat()
        current_records = records_by_date.get(local_date, [])
        current = None
        if current_records:
            selected_current = [
                item for item in current_records
                if item.get("selection_status") == "SELECTED_SHADOW"
            ]
            if selected_current:
                current = {
                    "selection_status": "SELECTED_SHADOW",
                    "cities": [str(item.get("city")) for item in selected_current if item.get("city")],
                    "selection_count": len(selected_current),
                    "eligible_candidates": max(int(item.get("eligible_candidates") or 0) for item in selected_current),
                    "selected_cost_usdc": sum(as_float(item.get("selected_cost_usdc")) or 0.0 for item in selected_current),
                    "completed": all(bool(item.get("completed")) for item in selected_current),
                }
            else:
                current = current_records[0]
        if local_date < LADDER_FORWARD_START:
            current_status = "NOT_STARTED"
        elif local_now.hour * 60 + local_now.minute < 11 * 60 + 12:
            current_status = "NOT_DUE"
        elif current is None:
            current_status = "MISSING_CAPTURE"
        else:
            current_status = str(current.get("selection_status") or "UNKNOWN")

        historical = {}
        try:
            payload = json.loads(LADDER_HISTORY_PATH.read_text(encoding="utf-8"))
            focus = (payload.get("executable_min_5_share_structures") or {}).get(LADDER_V4_HISTORY_KEY) or {}
            historical = {
                "metrics": focus,
                "first_half": {},
                "second_half": {},
                "outcome_legs": {},
                "selection_pipeline_leave_one_date_out": {},
                "selection_pipeline_rolling_origin": {},
            }
        except (OSError, ValueError, json.JSONDecodeError):
            historical = {"metrics": {}, "outcome_legs": {}}

        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "portfolio_version": LADDER_V4_PORTFOLIO,
            "shadow_only": True,
            "rule": {
                "cutoff_local": "11:00",
                "weights": {"lower": 5, "center": 15, "upper": 5},
                "selector": "all_eligible_by_max_spread_then_city",
                "daily_city_limit": None,
                "max_net_cost_usdc": 15.0,
                "forward_start_date": LADDER_FORWARD_START,
            },
            "status": {
                "phase": phase,
                "current_date": local_date,
                "current_status": current_status,
                "next_checkpoint_dates": checkpoint,
                "dates_remaining": max(0, checkpoint - completed_dates),
            },
            "summary": {
                "recorded_dates": len(records_by_date),
                "recorded_selections": len(rows),
                "completed_dates": completed_dates,
                "selected_dates": selected_dates,
                "no_eligible_dates": no_eligible_dates,
                "unresolved_dates": unresolved_dates,
                "profitable_dates": profitable_dates,
                "total_net_cost_usdc": total_cost,
                "total_net_pnl_usdc": total_pnl,
                "net_roi": total_pnl / total_cost if total_cost else None,
                "mean_net_pnl_per_completed_date": total_pnl / completed_dates if completed_dates else None,
                "bootstrap_p05_net_pnl_per_date": self._bootstrap_p05(completed_pnls),
                "outcome_counts": outcome_counts,
            },
            "current": current,
            "records": records[:40],
            "historical": historical,
        }

    @staticmethod
    def _bootstrap_p05(values: list[float], samples: int = 5000) -> float | None:
        if not values:
            return None
        rng = random.Random(20260805)
        estimates = sorted(
            sum(rng.choice(values) for _ in values) / len(values)
            for _ in range(samples)
        )
        return estimates[int(0.05 * len(estimates))]

    @staticmethod
    def _json_value(value: Any, fallback: Any) -> Any:
        try:
            return json.loads(value) if value else fallback
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback

DATA = WeatherDashboardData()


class WeatherDashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """launchd already supervises the service; avoid unbounded access logs."""
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/overview":
                self.send_json(DATA.overview())
                return
            if parsed.path == "/api/city":
                event_id = (parse_qs(parsed.query).get("event_id") or [""])[0]
                payload = DATA.city_detail(event_id)
                if payload is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "unknown event")
                else:
                    self.send_json(payload)
                return
            if parsed.path == "/api/accuracy":
                self.send_json(DATA.daily_accuracy())
                return
            if parsed.path == "/api/data-quality":
                self.send_json(DATA.data_quality())
                return
            if parsed.path == "/api/ai-system":
                self.send_json(DATA.ai_system())
                return
            if parsed.path == "/api/dual-strategy":
                self.send_json(DATA.dual_strategy())
                return
            if parsed.path == "/api/ladder-v4":
                self.send_json(DATA.ladder_v4())
                return
            super().do_GET()
        except (BrokenPipeError, ConnectionResetError):
            return
        except (sqlite3.Error, OSError, ValueError) as exc:
            try:
                self.send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            except (BrokenPipeError, ConnectionResetError):
                return

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone Meteoblue weather market dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), WeatherDashboardHandler)
    print(f"Weather dashboard listening on http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
