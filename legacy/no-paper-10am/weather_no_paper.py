#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import math
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "paper_config.json"
SCHEMA_PATH = ROOT / "paper_analysis.schema.json"
UTC = timezone.utc
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
METAR_URL = "https://aviationweather.gov/api/data/metar"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(UTC).isoformat(timespec="seconds")


def as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_book(value: Any) -> list[dict[str, float]]:
    if not value:
        return []
    try:
        rows = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return []
    parsed = []
    for row in rows.get("asks", []) if isinstance(rows, dict) else []:
        price, size = as_float(row.get("price")), as_float(row.get("size"))
        if price is not None and size is not None and size > 0:
            parsed.append({"price": price, "size": size})
    return sorted(parsed, key=lambda item: item["price"])


def executable_ask_vwap(book_json: Any, shares: float) -> tuple[float | None, float]:
    remaining = shares
    cost = 0.0
    available = 0.0
    for level in parse_book(book_json):
        take = min(remaining, level["size"])
        cost += take * level["price"]
        available += take
        remaining -= take
        if remaining <= 1e-9:
            return cost / shares, available
    return None, available


def load_export_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip("'\"")
    return result


class WeatherNoPaper:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.db_path = ROOT / str(config["databasePath"])
        self.db = sqlite3.connect(self.db_path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "weather-no-paper/1.0"})
        self._init_schema()

    def close(self) -> None:
        self.db.close()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS weather_no_paper_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS weather_no_paper_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at_utc TEXT NOT NULL,
                completed_at_utc TEXT,
                status TEXT NOT NULL,
                due_events INTEGER NOT NULL DEFAULT 0,
                decisions_written INTEGER NOT NULL DEFAULT 0,
                settlements_updated INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS weather_no_paper_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                run_id INTEGER,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                station_id TEXT,
                timezone TEXT NOT NULL,
                target_date TEXT NOT NULL,
                scheduled_local_time TEXT NOT NULL,
                analyzed_at_utc TEXT NOT NULL,
                action TEXT NOT NULL,
                decision_key TEXT NOT NULL,
                selected_market_id TEXT,
                outcome_range TEXT,
                no_entry_price REAL,
                available_shares REAL,
                shares REAL NOT NULL,
                notional_usdc REAL,
                entry_fee_usdc REAL NOT NULL DEFAULT 0,
                no_win_probability REAL,
                confidence_low REAL,
                confidence_high REAL,
                raw_edge REAL,
                edge_after_buffer REAL,
                weather_thesis TEXT,
                model_evidence_json TEXT,
                key_risk TEXT,
                resolution_risk TEXT,
                rejection_reason TEXT,
                weather_payload_json TEXT,
                candidates_json TEXT,
                ai_response_json TEXT,
                settlement_status TEXT NOT NULL DEFAULT 'not_applicable',
                settled_at_utc TEXT,
                final_outcome TEXT,
                gross_pnl_usdc REAL,
                net_pnl_usdc REAL,
                UNIQUE(strategy_name, event_id, decision_key),
                FOREIGN KEY(event_id) REFERENCES events(event_id),
                FOREIGN KEY(selected_market_id) REFERENCES markets(market_id)
            );

            CREATE INDEX IF NOT EXISTS idx_weather_no_paper_status
            ON weather_no_paper_decisions(settlement_status, analyzed_at_utc);
            """
        )
        self._migrate_decisions_for_multiple_no()
        self.db.execute(
            "INSERT OR IGNORE INTO weather_no_paper_meta(key,value) VALUES('strategy_started_at_utc',?)",
            (iso_utc(),),
        )
        self.db.commit()

    def _migrate_decisions_for_multiple_no(self) -> None:
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(weather_no_paper_decisions)")}
        if "decision_key" in columns:
            return
        self.db.execute("DROP INDEX IF EXISTS idx_weather_no_paper_status")
        self.db.execute("ALTER TABLE weather_no_paper_decisions RENAME TO weather_no_paper_decisions_legacy")
        self.db.executescript(
            """
            CREATE TABLE weather_no_paper_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                run_id INTEGER,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                station_id TEXT,
                timezone TEXT NOT NULL,
                target_date TEXT NOT NULL,
                scheduled_local_time TEXT NOT NULL,
                analyzed_at_utc TEXT NOT NULL,
                action TEXT NOT NULL,
                decision_key TEXT NOT NULL,
                selected_market_id TEXT,
                outcome_range TEXT,
                no_entry_price REAL,
                available_shares REAL,
                shares REAL NOT NULL,
                notional_usdc REAL,
                entry_fee_usdc REAL NOT NULL DEFAULT 0,
                no_win_probability REAL,
                confidence_low REAL,
                confidence_high REAL,
                raw_edge REAL,
                edge_after_buffer REAL,
                weather_thesis TEXT,
                model_evidence_json TEXT,
                key_risk TEXT,
                resolution_risk TEXT,
                rejection_reason TEXT,
                weather_payload_json TEXT,
                candidates_json TEXT,
                ai_response_json TEXT,
                settlement_status TEXT NOT NULL DEFAULT 'not_applicable',
                settled_at_utc TEXT,
                final_outcome TEXT,
                gross_pnl_usdc REAL,
                net_pnl_usdc REAL,
                UNIQUE(strategy_name, event_id, decision_key),
                FOREIGN KEY(event_id) REFERENCES events(event_id),
                FOREIGN KEY(selected_market_id) REFERENCES markets(market_id)
            );
            INSERT INTO weather_no_paper_decisions(
                decision_id,strategy_name,run_id,event_id,city,station_id,timezone,target_date,
                scheduled_local_time,analyzed_at_utc,action,decision_key,selected_market_id,
                outcome_range,no_entry_price,available_shares,shares,notional_usdc,entry_fee_usdc,
                no_win_probability,confidence_low,confidence_high,raw_edge,edge_after_buffer,
                weather_thesis,model_evidence_json,key_risk,resolution_risk,rejection_reason,
                weather_payload_json,candidates_json,ai_response_json,settlement_status,
                settled_at_utc,final_outcome,gross_pnl_usdc,net_pnl_usdc
            )
            SELECT
                decision_id,strategy_name,run_id,event_id,city,station_id,timezone,target_date,
                scheduled_local_time,analyzed_at_utc,action,
                CASE WHEN selected_market_id IS NOT NULL THEN 'market:' || selected_market_id ELSE action END,
                selected_market_id,outcome_range,no_entry_price,available_shares,shares,notional_usdc,
                entry_fee_usdc,no_win_probability,confidence_low,confidence_high,raw_edge,
                edge_after_buffer,weather_thesis,model_evidence_json,key_risk,resolution_risk,
                rejection_reason,weather_payload_json,candidates_json,ai_response_json,
                settlement_status,settled_at_utc,final_outcome,gross_pnl_usdc,net_pnl_usdc
            FROM weather_no_paper_decisions_legacy;
            DROP TABLE weather_no_paper_decisions_legacy;
            CREATE INDEX idx_weather_no_paper_status
            ON weather_no_paper_decisions(settlement_status, analyzed_at_utc);
            """
        )

    def strategy_started_at(self) -> datetime:
        row = self.db.execute(
            "SELECT value FROM weather_no_paper_meta WHERE key='strategy_started_at_utc'"
        ).fetchone()
        return datetime.fromisoformat(row[0]).astimezone(UTC)

    def events_for_today(self, now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows = self.db.execute(
            """
            SELECT e.event_id, e.city, e.target_date, e.station_id, e.station_name,
                   e.resolution_source, e.rules, s.latitude, s.longitude, s.timezone
            FROM events e JOIN stations s ON s.station_id=e.station_id
            WHERE e.resolved_at_utc IS NULL
              AND s.timezone IS NOT NULL AND s.latitude IS NOT NULL AND s.longitude IS NOT NULL
              AND lower(e.city) NOT IN ('shenzhen','hong kong')
              AND e.city NOT IN ('深圳','香港')
            ORDER BY e.target_date, e.city
            """
        ).fetchall()
        due: list[dict[str, Any]] = []
        missed: list[dict[str, Any]] = []
        start = self.strategy_started_at()
        hour = int(self.config["decisionLocalHour"])
        minute = int(self.config["decisionLocalMinute"])
        window = timedelta(minutes=int(self.config["decisionWindowMinutes"]))
        excluded = {str(city).strip().casefold() for city in self.config.get("excludedCities", [])}
        for row in rows:
            if str(row["city"] or "").strip().casefold() in excluded:
                continue
            try:
                tz = ZoneInfo(row["timezone"])
                target = date.fromisoformat(row["target_date"])
            except (ValueError, ZoneInfoNotFoundError):
                continue
            local_now = now.astimezone(tz)
            if local_now.date() != target:
                continue
            scheduled = datetime(target.year, target.month, target.day, hour, minute, tzinfo=tz)
            scheduled_utc = scheduled.astimezone(UTC)
            if scheduled_utc < start:
                continue
            exists = self.db.execute(
                """
                SELECT action FROM weather_no_paper_decisions
                WHERE strategy_name=? AND event_id=?
                ORDER BY CASE WHEN action='error' THEN 1 ELSE 0 END LIMIT 1
                """,
                (self.config["strategyName"], row["event_id"]),
            ).fetchone()
            # A transient AI/runtime error is retryable while the decision
            # window is open. Final decisions must remain idempotent.
            if exists and str(exists["action"] or "") != "error":
                continue
            item = {**dict(row), "scheduled_local_time": scheduled.isoformat(timespec="minutes")}
            if scheduled <= local_now < scheduled + window:
                due.append(item)
            elif local_now >= scheduled + window:
                missed.append(item)
        return due, missed

    def latest_meteoblue(
        self, event: dict[str, Any], as_of_utc: datetime | None = None
    ) -> dict[str, Any] | None:
        as_of_text = iso_utc(as_of_utc) if as_of_utc is not None else None
        rows = self.db.execute(
            """
            SELECT slot_utc, model_ref_time_utc, model_updated_at_utc, forecast_max_c,
                   forecast_max_f, forecast_peak_local, points_json
            FROM windy_forecasts
            WHERE station_id=? AND target_date=? AND model='mblue' AND status='ok'
              AND (? IS NULL OR slot_utc<=?)
            ORDER BY slot_utc DESC LIMIT 2
            """,
            (event["station_id"], event["target_date"], as_of_text, as_of_text),
        ).fetchall()
        if not rows:
            return None
        current = rows[0]
        previous_max = as_float(rows[1]["forecast_max_c"]) if len(rows) > 1 else None
        current_max = as_float(current["forecast_max_c"])
        try:
            points = json.loads(current["points_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            points = []
        return {
            "model": "mblue",
            "sampleSlotUtc": current["slot_utc"],
            "modelRefTimeUtc": current["model_ref_time_utc"],
            "modelUpdatedAtUtc": current["model_updated_at_utc"],
            "maxC": current_max,
            "previousMaxC": previous_max,
            "revisionC": current_max - previous_max if current_max is not None and previous_max is not None else None,
            "peakLocal": current["forecast_peak_local"],
            "hourly": points,
        }

    def candidates(
        self, event: dict[str, Any], as_of_utc: datetime | None = None
    ) -> list[dict[str, Any]]:
        as_of_text = iso_utc(as_of_utc) if as_of_utc is not None else None
        rows = self.db.execute(
            """
            SELECT m.market_id, m.outcome_range, m.bucket_low, m.bucket_high, m.bucket_unit,
                   ms.slot_utc, ms.no_best_bid, ms.no_best_ask, ms.no_bid_size,
                   ms.no_ask_size, ms.no_book_json
            FROM markets m JOIN market_snapshots ms ON ms.market_id=m.market_id
            WHERE m.event_id=? AND ms.slot_utc=(
                SELECT MAX(ms2.slot_utc) FROM market_snapshots ms2
                WHERE ms2.event_id=? AND (? IS NULL OR ms2.slot_utc<=?)
            )
            ORDER BY COALESCE(m.bucket_low,-999), COALESCE(m.bucket_high,999)
            """,
            (event["event_id"], event["event_id"], as_of_text, as_of_text),
        ).fetchall()
        shares = float(self.config["shares"])
        minimum, maximum = float(self.config.get("minNoAsk", 0.0)), float(self.config["maxNoAsk"])
        maximum_exclusive = bool(self.config.get("maxNoAskExclusive", False))
        output = []
        for row in rows:
            vwap, available = executable_ask_vwap(row["no_book_json"], shares)
            if (
                vwap is None
                or vwap < minimum - 1e-9
                or vwap > maximum + 1e-9
                or (maximum_exclusive and vwap >= maximum - 1e-9)
            ):
                continue
            output.append(
                {
                    "marketId": row["market_id"],
                    "outcomeRange": row["outcome_range"],
                    "bucketLow": row["bucket_low"],
                    "bucketHigh": row["bucket_high"],
                    "bucketUnit": row["bucket_unit"],
                    "snapshotUtc": row["slot_utc"],
                    "noBestBid": row["no_best_bid"],
                    "noBestAsk": row["no_best_ask"],
                    "noExecutablePrice5": round(vwap, 6),
                    "availableShares": round(available, 6),
                }
            )
        return output

    def fetch_open_meteo(self, event: dict[str, Any]) -> dict[str, Any]:
        models = ",".join(self.config["openMeteoModels"])
        response = self.session.get(
            OPEN_METEO_URL,
            params={
                "latitude": event["latitude"],
                "longitude": event["longitude"],
                "timezone": event["timezone"],
                "forecast_days": 3,
                "past_days": 1,
                "models": models,
                "current": "temperature_2m,relative_humidity_2m,precipitation,cloud_cover,wind_speed_10m",
                "hourly": "temperature_2m",
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        model_maxima = []
        requested_models = [str(model) for model in self.config.get("openMeteoModels", []) if str(model)]
        for key, values in hourly.items():
            if key == "time" or not isinstance(values, list):
                continue
            if key == "temperature_2m" and len(requested_models) == 1:
                model_name = requested_models[0]
            elif key.startswith("temperature_2m_"):
                model_name = key.removeprefix("temperature_2m_")
            else:
                continue
            points = [
                (times[index], as_float(value))
                for index, value in enumerate(values[: len(times)])
                if str(times[index]).startswith(event["target_date"]) and as_float(value) is not None
            ]
            if not points:
                continue
            peak = max(points, key=lambda item: item[1])
            model_maxima.append({"model": model_name, "maxC": peak[1], "peakLocal": peak[0]})
        values = [item["maxC"] for item in model_maxima]
        return {
            "source": "open-meteo",
            "fetchedAtUtc": iso_utc(),
            "current": payload.get("current"),
            "currentUnits": payload.get("current_units"),
            "modelMaxima": model_maxima,
            "modelRangeC": [min(values), max(values)] if values else None,
        }

    def fetch_metar(self, station_id: str | None) -> dict[str, Any] | None:
        if not station_id or len(station_id) != 4 or not station_id.isascii() or not station_id.isalnum():
            return None
        response = self.session.get(METAR_URL, params={"ids": station_id, "format": "json"}, timeout=20)
        response.raise_for_status()
        payload = response.json()
        return payload[0] if isinstance(payload, list) and payload else None

    def recent_calibration(self, city: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT e.event_id,e.target_date,e.winning_range,w.forecast_max_c,w.slot_utc,s.timezone
            FROM events e JOIN windy_forecasts w
              ON w.station_id=e.station_id AND w.target_date=e.target_date
            JOIN stations s ON s.station_id=e.station_id
            WHERE e.city=? AND e.resolved_at_utc IS NOT NULL AND e.winning_range IS NOT NULL
              AND w.model='mblue' AND w.status='ok'
            ORDER BY e.target_date DESC,w.slot_utc
            LIMIT 500
            """,
            (city,),
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["event_id"], []).append(row)
        output = []
        for event_rows in grouped.values():
            try:
                local_tz = ZoneInfo(event_rows[0]["timezone"])
                target = date.fromisoformat(event_rows[0]["target_date"])
                scheduled = datetime(target.year, target.month, target.day, 10, tzinfo=local_tz)
                chosen = min(
                    event_rows,
                    key=lambda row: abs(
                        (datetime.fromisoformat(row["slot_utc"]).astimezone(local_tz) - scheduled).total_seconds()
                    ),
                )
            except (ValueError, ZoneInfoNotFoundError):
                continue
            output.append(
                {
                    "targetDate": chosen["target_date"],
                    "winningRange": chosen["winning_range"],
                    "meteoblueMaxCAtLocal10": chosen["forecast_max_c"],
                    "sampleSlotUtc": chosen["slot_utc"],
                }
            )
        return sorted(output, key=lambda item: item["targetDate"], reverse=True)[:5]

    def recent_decision_lessons(self, city: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT target_date,outcome_range,no_entry_price,no_win_probability,
                   confidence_low,confidence_high,weather_thesis,key_risk,
                   settlement_status,final_outcome,net_pnl_usdc
            FROM weather_no_paper_decisions
            WHERE city=? AND action='trade' AND settlement_status IN ('won','lost')
            ORDER BY target_date DESC,decision_id DESC LIMIT 10
            """,
            (city,),
        ).fetchall()
        return [
            {
                "targetDate": row["target_date"],
                "outcomeRange": row["outcome_range"],
                "entryNoPrice": row["no_entry_price"],
                "estimatedNoProbability": row["no_win_probability"],
                "confidence": [row["confidence_low"], row["confidence_high"]],
                "weatherThesis": row["weather_thesis"],
                "keyRisk": row["key_risk"],
                "result": row["settlement_status"],
                "finalOutcome": row["final_outcome"],
                "netPnlUsdc": row["net_pnl_usdc"],
            }
            for row in rows
        ]

    def local_weather_payload(
        self, event: dict[str, Any], as_of_utc: datetime | None = None
    ) -> dict[str, Any]:
        persisted = self.persisted_external_weather(event, as_of_utc)
        return {
            "city": event["city"],
            "targetDate": event["target_date"],
            "analysisAsOfUtc": iso_utc(as_of_utc) if as_of_utc is not None else None,
            "station": {
                "id": event["station_id"], "name": event["station_name"],
                "latitude": event["latitude"], "longitude": event["longitude"],
                "timezone": event["timezone"], "resolutionSource": event["resolution_source"],
            },
            "resolutionRules": event["rules"],
            "meteoblue": self.latest_meteoblue(event, as_of_utc),
            "recentMeteoblueCalibration": self.recent_calibration(event["city"]),
            "recentDecisionLessons": self.recent_decision_lessons(event["city"]),
            "otherModelsAndCurrent": persisted["otherModelsAndCurrent"],
            "metar": persisted["metar"],
        }

    def persisted_external_weather(
        self, event: dict[str, Any], as_of_utc: datetime | None = None
    ) -> dict[str, Any]:
        as_of_text = iso_utc(as_of_utc) if as_of_utc is not None else None
        try:
            forecast_rows = self.db.execute(
                """
                SELECT model,forecast_max_c,forecast_peak_local,points_json,slot_utc,status
                FROM external_forecasts
                WHERE station_id=? AND target_date=? AND slot_utc=(
                    SELECT MAX(slot_utc) FROM external_forecasts
                    WHERE station_id=? AND target_date=? AND (? IS NULL OR slot_utc<=?)
                )
                ORDER BY model
                """,
                (
                    event["station_id"], event["target_date"], event["station_id"],
                    event["target_date"], as_of_text, as_of_text,
                ),
            ).fetchall()
            observation_rows = self.db.execute(
                """
                SELECT source,slot_utc,observation_time_utc,temperature_c,dewpoint_c,
                       relative_humidity,precipitation_mm,cloud_cover_pct,wind_direction_deg,
                       wind_speed,wind_speed_unit,weather_code,observed_daily_max_c,status
                FROM weather_observations WHERE station_id=? AND slot_utc=(
                    SELECT MAX(slot_utc) FROM weather_observations
                    WHERE station_id=? AND (? IS NULL OR slot_utc<=?)
                )
                """,
                (event["station_id"], event["station_id"], as_of_text, as_of_text),
            ).fetchall()
        except sqlite3.OperationalError:
            return {"otherModelsAndCurrent": {"source": "database", "error": "external tables not initialized"}, "metar": None}
        maxima = []
        allowed_models = {
            str(model) for model in self.config.get("openMeteoModels", ["ecmwf_ifs025"]) if str(model)
        }
        for row in forecast_rows:
            if (
                row["status"] != "ok"
                or row["forecast_max_c"] is None
                or row["model"] not in allowed_models
            ):
                continue
            maxima.append({"model": row["model"], "maxC": row["forecast_max_c"], "peakLocal": row["forecast_peak_local"]})
        current_row = next((row for row in observation_rows if row["source"] == "open_meteo_current"), None)
        metar_row = next((row for row in observation_rows if row["source"] == "metar"), None)
        current = None
        if current_row:
            current = {
                "time": current_row["observation_time_utc"], "temperature_2m": current_row["temperature_c"],
                "relative_humidity_2m": current_row["relative_humidity"],
                "precipitation": current_row["precipitation_mm"], "cloud_cover": current_row["cloud_cover_pct"],
                "wind_direction_10m": current_row["wind_direction_deg"],
                "wind_speed_10m": current_row["wind_speed"], "wind_speed_unit": current_row["wind_speed_unit"],
            }
        metar = None
        if metar_row:
            metar = {
                "obsTime": metar_row["observation_time_utc"], "temp": metar_row["temperature_c"],
                "dewp": metar_row["dewpoint_c"], "wdir": metar_row["wind_direction_deg"],
                "wspd": metar_row["wind_speed"], "wspdUnit": metar_row["wind_speed_unit"],
                "wxString": metar_row["weather_code"], "dailyMaxC": metar_row["observed_daily_max_c"],
            }
        values = [row["maxC"] for row in maxima if row["maxC"] is not None]
        return {
            "otherModelsAndCurrent": {
                "source": "open-meteo-persisted", "sampleSlotUtc": forecast_rows[0]["slot_utc"] if forecast_rows else None,
                "current": current, "modelMaxima": maxima,
                "modelRangeC": [min(values), max(values)] if values else None,
            },
            "metar": metar,
        }

    def external_weather_payload(self, event: dict[str, Any]) -> dict[str, Any]:
        external: dict[str, Any]
        try:
            external = self.fetch_open_meteo(event)
        except Exception as exc:
            external = {"source": "open-meteo", "error": str(exc)[:500]}
        try:
            metar = self.fetch_metar(event.get("station_id"))
        except Exception as exc:
            metar = {"error": str(exc)[:500]}
        return {"otherModelsAndCurrent": external, "metar": metar}

    def weather_payload(self, event: dict[str, Any]) -> dict[str, Any]:
        return {**self.local_weather_payload(event), **self.external_weather_payload(event)}

    def build_inputs(
        self,
        events: list[dict[str, Any]],
        as_of_by_event: dict[str, datetime] | None = None,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for event in events:
            as_of_utc = (as_of_by_event or {}).get(event["event_id"])
            candidates = self.candidates(event, as_of_utc)
            output.append(
                {
                    "event": event,
                    "weather": self.local_weather_payload(event, as_of_utc),
                    "candidates": candidates,
                }
            )
        return output

    def replay_decided_events_for_date(self, target_date: str) -> dict[str, Any]:
        """Re-run today's existing decisions using only snapshots available at local 10:00.

        The AI call happens before any existing decision is removed.  Once the
        complete response is valid, the old rows are replaced in one SQLite
        transaction so a partial replay cannot leave the dashboard inconsistent.
        """
        try:
            date.fromisoformat(target_date)
        except ValueError as exc:
            raise ValueError(f"invalid replay date: {target_date}") from exc

        rows = self.db.execute(
            """
            SELECT DISTINCT e.event_id, e.city, e.target_date, e.station_id, e.station_name,
                   e.resolution_source, e.rules, s.latitude, s.longitude, s.timezone
            FROM weather_no_paper_decisions d
            JOIN events e ON e.event_id=d.event_id
            JOIN stations s ON s.station_id=e.station_id
            WHERE d.strategy_name=? AND d.target_date=? AND d.action!='error'
              AND lower(e.city) NOT IN ('shenzhen','hong kong')
              AND e.city NOT IN ('深圳','香港')
            ORDER BY e.city, e.event_id
            """,
            (self.config["strategyName"], target_date),
        ).fetchall()
        events: list[dict[str, Any]] = []
        as_of_by_event: dict[str, datetime] = {}
        for row in rows:
            try:
                local_tz = ZoneInfo(row["timezone"])
                target = date.fromisoformat(row["target_date"])
            except (ValueError, ZoneInfoNotFoundError):
                logging.warning("skip replay event with invalid timezone/date: %s", row["event_id"])
                continue
            scheduled = datetime(target.year, target.month, target.day, 10, 0, tzinfo=local_tz)
            event = {**dict(row), "scheduled_local_time": scheduled.isoformat(timespec="minutes")}
            events.append(event)
            as_of_by_event[event["event_id"]] = scheduled.astimezone(UTC)

        started = utc_now()
        cursor = self.db.execute(
            "INSERT INTO weather_no_paper_runs(started_at_utc,status,due_events) VALUES(?,'running',?)",
            (iso_utc(started), len(events)),
        )
        run_id = int(cursor.lastrowid)
        self.db.commit()
        if not events:
            self.db.execute(
                "UPDATE weather_no_paper_runs SET completed_at_utc=?,status='completed',decisions_written=0 WHERE run_id=?",
                (iso_utc(), run_id),
            )
            self.db.commit()
            return {"run_id": run_id, "target_date": target_date, "events": 0, "decisions_written": 0}

        try:
            inputs = self.build_inputs(events, as_of_by_event)
            response = self.call_ai(inputs)

            # No old rows are touched until the single AI response validates.
            self.db.execute("BEGIN")
            self.db.executemany(
                "DELETE FROM weather_no_paper_decisions WHERE strategy_name=? AND event_id=?",
                [(self.config["strategyName"], event["event_id"]) for event in events],
            )
            decisions_written = self.persist_decisions(run_id, inputs, response)
            self.db.execute(
                "UPDATE weather_no_paper_runs SET completed_at_utc=?,status='completed',decisions_written=? WHERE run_id=?",
                (iso_utc(), decisions_written, run_id),
            )
            self.db.commit()
            return {
                "run_id": run_id,
                "target_date": target_date,
                "events": len(events),
                "decisions_written": decisions_written,
                "analysis_as_of_utc": {
                    event_id: iso_utc(as_of) for event_id, as_of in as_of_by_event.items()
                },
            }
        except Exception as exc:
            self.db.rollback()
            self.db.execute(
                "UPDATE weather_no_paper_runs SET completed_at_utc=?,status='failed',error=? WHERE run_id=?",
                (iso_utc(), str(exc)[:2000], run_id),
            )
            self.db.commit()
            raise

    def reanalyze_missed_events_for_date(self, target_date: str) -> dict[str, Any]:
        """Replace missed rows using the latest available snapshots, explicitly timestamped as late analysis."""
        try:
            date.fromisoformat(target_date)
        except ValueError as exc:
            raise ValueError(f"invalid reanalysis date: {target_date}") from exc
        rows = self.db.execute(
            """
            SELECT DISTINCT e.event_id, e.city, e.target_date, e.station_id, e.station_name,
                   e.resolution_source, e.rules, s.latitude, s.longitude, s.timezone
            FROM weather_no_paper_decisions d
            JOIN events e ON e.event_id=d.event_id
            JOIN stations s ON s.station_id=e.station_id
            WHERE d.strategy_name=? AND d.target_date=? AND d.action='missed'
            ORDER BY e.city, e.event_id
            """,
            (self.config["strategyName"], target_date),
        ).fetchall()
        analyzed_as_of = utc_now()
        events: list[dict[str, Any]] = []
        as_of_by_event: dict[str, datetime] = {}
        for row in rows:
            try:
                local_tz = ZoneInfo(row["timezone"])
                target = date.fromisoformat(row["target_date"])
            except (ValueError, ZoneInfoNotFoundError):
                continue
            scheduled = datetime(target.year, target.month, target.day, 10, 0, tzinfo=local_tz)
            events.append({**dict(row), "scheduled_local_time": scheduled.isoformat(timespec="minutes")})
            as_of_by_event[row["event_id"]] = analyzed_as_of

        cursor = self.db.execute(
            "INSERT INTO weather_no_paper_runs(started_at_utc,status,due_events) VALUES(?,'running',?)",
            (iso_utc(analyzed_as_of), len(events)),
        )
        run_id = int(cursor.lastrowid)
        self.db.commit()
        if not events:
            self.db.execute(
                "UPDATE weather_no_paper_runs SET completed_at_utc=?,status='completed' WHERE run_id=?",
                (iso_utc(), run_id),
            )
            self.db.commit()
            return {"run_id": run_id, "target_date": target_date, "events": 0, "decisions_written": 0}
        try:
            inputs = self.build_inputs(events, as_of_by_event)
            ready = [item for item in inputs if item["weather"].get("meteoblue") and item["candidates"]]
            if len(ready) != len(inputs):
                unavailable = [item["event"]["city"] for item in inputs if item not in ready]
                raise RuntimeError(f"late reanalysis data unavailable for: {', '.join(unavailable)}")
            response = self.call_ai(inputs)
            self.db.execute("BEGIN")
            self.db.executemany(
                "DELETE FROM weather_no_paper_decisions WHERE strategy_name=? AND event_id=? AND action='missed'",
                [(self.config["strategyName"], event["event_id"]) for event in events],
            )
            decisions_written = self.persist_decisions(run_id, inputs, response)
            self.db.execute(
                "UPDATE weather_no_paper_runs SET completed_at_utc=?,status='completed',decisions_written=? WHERE run_id=?",
                (iso_utc(), decisions_written, run_id),
            )
            self.db.commit()
            return {
                "run_id": run_id,
                "target_date": target_date,
                "events": len(events),
                "decisions_written": decisions_written,
                "analysis_as_of_utc": iso_utc(analyzed_as_of),
                "cities": [event["city"] for event in events],
            }
        except Exception as exc:
            self.db.rollback()
            self.db.execute(
                "UPDATE weather_no_paper_runs SET completed_at_utc=?,status='failed',error=? WHERE run_id=?",
                (iso_utc(), str(exc)[:2000], run_id),
            )
            self.db.commit()
            raise

    def has_fresh_local_data(self, event: dict[str, Any], now: datetime) -> bool:
        market_row = self.db.execute(
            "SELECT MAX(slot_utc) FROM market_snapshots WHERE event_id=?",
            (event["event_id"],),
        ).fetchone()
        forecast_row = self.db.execute(
            """
            SELECT MAX(slot_utc) FROM windy_forecasts
            WHERE station_id=? AND target_date=? AND model='mblue' AND status='ok'
            """,
            (event["station_id"], event["target_date"]),
        ).fetchone()
        external_row = self.db.execute(
            """
            SELECT COUNT(DISTINCT model),MAX(slot_utc) FROM external_forecasts
            WHERE station_id=? AND target_date=? AND status='ok'
              AND slot_utc=(SELECT MAX(slot_utc) FROM external_forecasts WHERE station_id=? AND target_date=?)
            """,
            (event["station_id"], event["target_date"], event["station_id"], event["target_date"]),
        ).fetchone()
        observation_rows = self.db.execute(
            """
            SELECT source,MAX(slot_utc) FROM weather_observations
            WHERE station_id=? GROUP BY source
            """,
            (event["station_id"],),
        ).fetchall()
        maximum_age = timedelta(minutes=int(self.config.get("maxDataAgeMinutes", 20)))
        for raw in (
            market_row[0] if market_row else None,
            forecast_row[0] if forecast_row else None,
            external_row[1] if external_row else None,
        ):
            if not raw:
                return False
            try:
                sampled = datetime.fromisoformat(raw).astimezone(UTC)
            except ValueError:
                return False
            if now - sampled > maximum_age or sampled - now > timedelta(minutes=2):
                return False
        required_external_models = int(
            self.config.get("minExternalModels", len(self.config.get("openMeteoModels", ["ecmwf_ifs025"])))
        )
        if not external_row or int(external_row[0] or 0) < required_external_models:
            return False
        if {row[0] for row in observation_rows} != {"open_meteo_current", "metar"}:
            return False
        return True

    def has_settlements_ready(self) -> bool:
        return bool(
            self.db.execute(
                """
                SELECT 1 FROM weather_no_paper_decisions d
                JOIN market_resolutions r ON r.market_id=d.selected_market_id
                WHERE d.action='trade' AND d.settlement_status='pending' AND r.is_resolved=1 LIMIT 1
                """
            ).fetchone()
        )

    def call_ai(self, inputs: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = (
            "你是天气二元市场的审慎 paper 交易决策器。只使用下方结构化数据，不调用工具、不访问网页。"
            "每个 eventId 必须有输出；同一城市可以选择多个不同温度档的 NO，每个选中的档位输出一条 trade decision。"
            "若整座城市没有任何值得交易的档位，只输出一条 no_trade。不得重复选择同一 selectedMarketId。\n\n"
            "判断重点：结算是指定 Wunderground 站点记录到的当日最高温，短暂峰值也算；严格考虑结算精度与四舍五入。"
            "Meteoblue 是主要模型，ECMWF 是唯一辅助预测模型；实时 METAR/当前天气用于判断当天升温轨迹、云雨、风和模型偏差。"
            "盘口价格不能作为天气概率证据。先独立估计每个温度档发生概率，再得到 P(NO)。"
            "recentDecisionLessons 是同城市已结算 NO 的经验回放，只用于识别重复偏差和风险机制；样本少时不得机械套用或继续调阈值。"
            "仅可从 candidates 选择，selectedMarketId 必须完全一致。即使价格合格，数据缺失、模型冲突、尾部概率估计不稳或优势不足时也应 no_trade。"
            "weatherThesis 要简洁说明预测峰值区间、模型分歧和实时轨迹；modelEvidence 列出关键数值；keyRisk 写最可能导致 NO 亏损的机制。\n\n"
            f"固定风控：每笔 {self.config['shares']} shares；NO 没有最低价格，5-share 可成交价必须严格低于 "
            f"{self.config['maxNoAsk']:.2f}；AI 的核心任务是判断该温度档是否大概率不会成为结算结果，"
            f"要求 P(NO) 至少 {float(self.config.get('minNoWinProbability', 0.0)):.0%}。"
            "Edge 只作为辅助记录，不是主要决策依据。\n\n"
            "选择多个 NO 时，要分别判断每个档位不会结算的概率，并考虑这些仓位共享同一个最终最高温结果，"
            "不能因为档位多就降低单档判断标准。\n\n"
            "输入：\n" + json.dumps(inputs, ensure_ascii=False, separators=(",", ":"), default=str)
        )
        env = os.environ.copy()
        env.update(load_export_env(ROOT / ".beeapi.env"))
        # launchd provides a minimal PATH.  The Codex launcher uses
        # `#!/usr/bin/env node`, so keep the standard Homebrew/local binary
        # locations available even when the process was not started by a
        # login shell.
        env["PATH"] = ":".join(
            part
            for part in ("/usr/local/bin", "/opt/homebrew/bin", env.get("PATH", ""))
            if part
        )
        with tempfile.TemporaryDirectory(prefix="weather-no-paper-") as directory:
            output_path = Path(directory) / "analysis.json"
            command = [
                "/usr/local/bin/codex", "exec", "--profile", str(self.config["aiProfile"]),
                "-c", f"model_reasoning_effort=\"{self.config['aiReasoningEffort']}\"",
                "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only",
                "-C", str(ROOT), "--output-schema", str(SCHEMA_PATH),
                "--output-last-message", str(output_path), "-",
            ]
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                env=env,
                timeout=int(self.config["aiTimeoutSeconds"]),
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(f"AI exited {completed.returncode}: {completed.stderr[-1000:]}")
            return json.loads(output_path.read_text(encoding="utf-8"))

    def record_missed(self, run_id: int, events: list[dict[str, Any]]) -> int:
        count = 0
        for event in events:
            cursor = self.db.execute(
                """
                INSERT OR IGNORE INTO weather_no_paper_decisions(
                    strategy_name,run_id,event_id,city,station_id,timezone,target_date,
                    scheduled_local_time,analyzed_at_utc,action,decision_key,shares,rejection_reason
                ) VALUES(?,?,?,?,?,?,?,?,?,'missed','missed',?,?)
                """,
                (
                    self.config["strategyName"], run_id, event["event_id"], event["city"],
                    event["station_id"], event["timezone"], event["target_date"],
                    event["scheduled_local_time"], iso_utc(), float(self.config["shares"]),
                    "10:00 decision window elapsed before fresh analysis was available",
                ),
            )
            count += int(cursor.rowcount > 0)
        return count

    def persist_decisions(self, run_id: int, inputs: list[dict[str, Any]], response: dict[str, Any]) -> int:
        decisions = response.get("decisions") or []
        by_event: dict[str, list[dict[str, Any]]] = {}
        for decision in decisions:
            if isinstance(decision, dict):
                by_event.setdefault(str(decision.get("eventId")), []).append(decision)
        if set(by_event) != {item["event"]["event_id"] for item in inputs}:
            raise RuntimeError("AI response event IDs do not match due events")
        count = 0
        for item in inputs:
            event, weather, candidates = item["event"], item["weather"], item["candidates"]
            self.db.execute(
                "DELETE FROM weather_no_paper_decisions WHERE strategy_name=? AND event_id=? AND action='error'",
                (self.config["strategyName"], event["event_id"]),
            )
            candidate_by_id = {row["marketId"]: row for row in candidates}
            event_decisions = by_event[event["event_id"]]
            trade_decisions = [decision for decision in event_decisions if decision.get("action") == "trade"]
            if trade_decisions and len(trade_decisions) != len(event_decisions):
                raise RuntimeError(f"AI mixed trade and no_trade rows for event {event['event_id']}")
            if not trade_decisions and (len(event_decisions) != 1 or event_decisions[0].get("action") != "no_trade"):
                raise RuntimeError(f"AI must return one no_trade row when event {event['event_id']} has no selections")
            requested_ids = [str(decision.get("selectedMarketId") or "") for decision in trade_decisions]
            if len(requested_ids) != len(set(requested_ids)):
                raise RuntimeError(f"AI selected a duplicate market for event {event['event_id']}")

            for decision in event_decisions:
                action = str(decision.get("action") or "")
                requested_id = str(decision.get("selectedMarketId") or "")
                selected = candidate_by_id.get(requested_id) if action == "trade" else None
                probability = as_float(decision.get("noWinProbability"))
                rejection = decision.get("rejectionReason")
                if action == "trade" and (selected is None or probability is None):
                    raise RuntimeError(f"AI selected an invalid candidate for event {event['event_id']}")

                raw_edge = edge_after = None
                if selected is not None and probability is not None:
                    raw_edge = probability - selected["noExecutablePrice5"]
                    edge_after = raw_edge - float(self.config["feeAndUncertaintyBuffer"])
                    min_probability = float(self.config.get("minNoWinProbability", 0.0))
                    if probability + 1e-12 < min_probability:
                        action = "no_trade"
                        rejection = f"guardrail rejected no_win_probability={probability:.4f} below {min_probability:.4f}"
                    elif self.config.get("requirePositiveEdge", True) and edge_after + 1e-12 < float(self.config["minEdgeAfterBuffer"]):
                        action = "no_trade"
                        rejection = f"guardrail rejected edge_after_buffer={edge_after:.4f}"

                price = selected["noExecutablePrice5"] if selected else None
                shares = float(self.config["shares"])
                notional = price * shares if action == "trade" and price is not None else None
                fee = (notional or 0.0) * float(self.config["feeRate"])
                if action == "trade":
                    decision_key = f"market:{requested_id}"
                elif requested_id:
                    decision_key = f"rejected:{requested_id}"
                else:
                    decision_key = "no_trade"
                cursor = self.db.execute(
                    """
                    INSERT OR IGNORE INTO weather_no_paper_decisions(
                        strategy_name,run_id,event_id,city,station_id,timezone,target_date,
                        scheduled_local_time,analyzed_at_utc,action,decision_key,selected_market_id,outcome_range,
                        no_entry_price,available_shares,shares,notional_usdc,entry_fee_usdc,
                        no_win_probability,confidence_low,confidence_high,raw_edge,edge_after_buffer,
                        weather_thesis,model_evidence_json,key_risk,resolution_risk,rejection_reason,
                        weather_payload_json,candidates_json,ai_response_json,settlement_status
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        self.config["strategyName"], run_id, event["event_id"], event["city"],
                        event["station_id"], event["timezone"], event["target_date"],
                        event["scheduled_local_time"], iso_utc(), action, decision_key,
                        requested_id if action == "trade" else None,
                        selected["outcomeRange"] if selected else None,
                        price, selected["availableShares"] if selected else None, shares,
                        notional, fee, probability, as_float(decision.get("confidenceLow")),
                        as_float(decision.get("confidenceHigh")), raw_edge, edge_after,
                        decision.get("weatherThesis"), json.dumps(decision.get("modelEvidence") or [], ensure_ascii=False),
                        decision.get("keyRisk"), decision.get("resolutionRisk"), rejection,
                        json.dumps(weather, ensure_ascii=False, separators=(",", ":"), default=str),
                        json.dumps(candidates, ensure_ascii=False, separators=(",", ":"), default=str),
                        json.dumps(decision, ensure_ascii=False, separators=(",", ":"), default=str),
                        "pending" if action == "trade" else "not_applicable",
                    ),
                )
                count += int(cursor.rowcount > 0)
        return count

    def settle(self) -> int:
        rows = self.db.execute(
            """
            SELECT d.decision_id,d.no_entry_price,d.shares,d.entry_fee_usdc,
                   r.resolved_at_utc,r.winning_outcome,r.no_final_price
            FROM weather_no_paper_decisions d
            JOIN market_resolutions r ON r.market_id=d.selected_market_id
            WHERE d.action='trade' AND d.settlement_status='pending' AND r.is_resolved=1
            """
        ).fetchall()
        count = 0
        for row in rows:
            no_won = str(row["winning_outcome"] or "").upper() == "NO" or float(row["no_final_price"] or 0) >= 0.99
            price, shares = float(row["no_entry_price"]), float(row["shares"])
            gross = shares * ((1.0 - price) if no_won else -price)
            net = gross - float(row["entry_fee_usdc"] or 0)
            self.db.execute(
                """
                UPDATE weather_no_paper_decisions
                SET settlement_status=?,settled_at_utc=?,final_outcome=?,gross_pnl_usdc=?,net_pnl_usdc=?
                WHERE decision_id=?
                """,
                ("won" if no_won else "lost", row["resolved_at_utc"] or iso_utc(),
                 "NO" if no_won else "YES", gross, net, row["decision_id"]),
            )
            count += 1
        return count

    def run_once(self) -> dict[str, Any]:
        started = utc_now()
        due, missed = self.events_for_today(started)
        fresh_due = [event for event in due if self.has_fresh_local_data(event, started)]
        settlement_ready = self.has_settlements_ready()
        if not fresh_due and not missed and not settlement_ready:
            return {
                "run_id": None,
                "due_events": len(due),
                "fresh_due_events": 0,
                "decisions_written": 0,
                "settlements_updated": 0,
                "message": "no actionable work",
            }
        cursor = self.db.execute(
            "INSERT INTO weather_no_paper_runs(started_at_utc,status) VALUES(?,'running')",
            (iso_utc(started),),
        )
        run_id = int(cursor.lastrowid)
        due_count = decisions_written = 0
        try:
            settled = self.settle()
            due_count = len(due)
            decisions_written += self.record_missed(run_id, missed)
            if self.config.get("enabled", True) and fresh_due:
                inputs = self.build_inputs(fresh_due)
                ready = [item for item in inputs if item["weather"].get("meteoblue") and item["candidates"]]
                not_ready = [item for item in inputs if item not in ready]
                if not_ready:
                    decisions_written += self.record_not_ready(run_id, not_ready)
                if ready:
                    try:
                        response = self.call_ai(ready)
                        decisions_written += self.persist_decisions(run_id, ready, response)
                    except Exception as exc:
                        logging.exception("AI paper analysis failed")
                        decisions_written += self.record_ai_error(run_id, ready, exc)
            self.db.execute(
                """
                UPDATE weather_no_paper_runs SET completed_at_utc=?,status='completed',due_events=?,
                    decisions_written=?,settlements_updated=? WHERE run_id=?
                """,
                (iso_utc(), due_count, decisions_written, settled, run_id),
            )
            self.db.commit()
            return {
                "run_id": run_id,
                "due_events": due_count,
                "fresh_due_events": len(fresh_due),
                "decisions_written": decisions_written,
                "settlements_updated": settled,
            }
        except Exception as exc:
            self.db.rollback()
            self.db.execute(
                "UPDATE weather_no_paper_runs SET completed_at_utc=?,status='failed',error=? WHERE run_id=?",
                (iso_utc(), str(exc)[:2000], run_id),
            )
            self.db.commit()
            raise

    def record_not_ready(self, run_id: int, items: list[dict[str, Any]]) -> int:
        count = 0
        for item in items:
            event = item["event"]
            self.db.execute(
                "DELETE FROM weather_no_paper_decisions WHERE strategy_name=? AND event_id=? AND action='error'",
                (self.config["strategyName"], event["event_id"]),
            )
            reasons = []
            if not item["weather"].get("meteoblue"):
                reasons.append("Meteoblue unavailable")
            if not item["candidates"]:
                reasons.append("no 5-share NO ask in 0.80-1.00")
            cursor = self.db.execute(
                """
                INSERT OR IGNORE INTO weather_no_paper_decisions(
                    strategy_name,run_id,event_id,city,station_id,timezone,target_date,
                    scheduled_local_time,analyzed_at_utc,action,decision_key,shares,rejection_reason,
                    weather_payload_json,candidates_json
                ) VALUES(?,?,?,?,?,?,?,?,?,'no_trade','no_trade',?,?,?,?)
                """,
                (
                    self.config["strategyName"], run_id, event["event_id"], event["city"],
                    event["station_id"], event["timezone"], event["target_date"],
                    event["scheduled_local_time"], iso_utc(), float(self.config["shares"]),
                    "; ".join(reasons), json.dumps(item["weather"], ensure_ascii=False, default=str),
                    json.dumps(item["candidates"], ensure_ascii=False, default=str),
                ),
            )
            count += int(cursor.rowcount > 0)
        return count

    def record_ai_error(self, run_id: int, items: list[dict[str, Any]], exc: Exception) -> int:
        count = 0
        message = f"AI analysis failed: {str(exc)[:1000]}"
        for item in items:
            event = item["event"]
            cursor = self.db.execute(
                """
                INSERT OR IGNORE INTO weather_no_paper_decisions(
                    strategy_name,run_id,event_id,city,station_id,timezone,target_date,
                    scheduled_local_time,analyzed_at_utc,action,decision_key,shares,rejection_reason,
                    weather_payload_json,candidates_json
                ) VALUES(?,?,?,?,?,?,?,?,?,'error','error',?,?,?,?)
                """,
                (
                    self.config["strategyName"], run_id, event["event_id"], event["city"],
                    event["station_id"], event["timezone"], event["target_date"],
                    event["scheduled_local_time"], iso_utc(), float(self.config["shares"]), message,
                    json.dumps(item["weather"], ensure_ascii=False, default=str),
                    json.dumps(item["candidates"], ensure_ascii=False, default=str),
                ),
            )
            count += int(cursor.rowcount > 0)
        return count


def configure_logging(config: dict[str, Any]) -> None:
    path = ROOT / str(config["logPath"])
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
        force=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local-10:00 AI weather NO paper strategy")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--init-only", action="store_true")
    mode.add_argument("--replay-date", metavar="YYYY-MM-DD")
    mode.add_argument("--reanalyze-missed-date", metavar="YYYY-MM-DD")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    configure_logging(config)
    lock_path = ROOT / "data/weather_no_paper.lock"
    lock_handle = lock_path.open("w")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.info("another weather NO paper process is running")
        return 0
    engine = WeatherNoPaper(config)
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        if args.init_only:
            return 0
        if args.replay_date:
            print(json.dumps(engine.replay_decided_events_for_date(args.replay_date), ensure_ascii=False, indent=2))
            return 0
        if args.reanalyze_missed_date:
            print(json.dumps(engine.reanalyze_missed_events_for_date(args.reanalyze_missed_date), ensure_ascii=False, indent=2))
            return 0
        if not args.loop:
            print(json.dumps(engine.run_once(), ensure_ascii=False, indent=2))
            return 0
        while not stop:
            try:
                result = engine.run_once()
                if result["due_events"] or result["settlements_updated"]:
                    logging.info("paper run: %s", json.dumps(result, ensure_ascii=False))
            except Exception:
                logging.exception("weather NO paper run failed")
            for _ in range(int(config["pollSeconds"])):
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
