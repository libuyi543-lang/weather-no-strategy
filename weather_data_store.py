#!/usr/bin/env python3
"""Read-only weather context shared by the active Hermes trading system."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
UTC = timezone.utc


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


def json_value(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed


class WeatherDataStore:
    """Database access for active weather inputs, with no legacy strategy state."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        configured = Path(str(config["databasePath"])).expanduser()
        self.db_path = configured if configured.is_absolute() else ROOT / configured
        self.db = sqlite3.connect(self.db_path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self._ensure_observation_columns()

    def _ensure_observation_columns(self) -> None:
        """Keep the Agent compatible with databases created before METAR enrichment."""
        try:
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(weather_observations)")}
        except sqlite3.OperationalError:
            return
        if not columns:
            return
        for column, definition in (
            ("wind_gust", "REAL"),
            ("visibility_m", "REAL"),
            ("pressure_hpa", "REAL"),
            ("flight_category", "TEXT"),
            ("sky_conditions_json", "TEXT"),
            ("raw_metar", "TEXT"),
            ("metar_parser_status", "TEXT"),
            ("metar_parser_error", "TEXT"),
        ):
            if column not in columns:
                self.db.execute(f"ALTER TABLE weather_observations ADD COLUMN {column} {definition}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def latest_meteoblue(
        self, event: dict[str, Any], as_of_utc: datetime | None = None
    ) -> dict[str, Any] | None:
        as_of_text = iso_utc(as_of_utc) if as_of_utc is not None else None
        rows = self.db.execute(
            """
            SELECT slot_utc,model_ref_time_utc,model_updated_at_utc,forecast_max_c,
                   forecast_max_f,forecast_peak_local,points_json
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
        current_max = as_float(current["forecast_max_c"])
        previous_max = as_float(rows[1]["forecast_max_c"]) if len(rows) > 1 else None
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
            "revisionC": (
                current_max - previous_max
                if current_max is not None and previous_max is not None else None
            ),
            "peakLocal": current["forecast_peak_local"],
            "hourly": points,
        }

    def persisted_external_weather(
        self, event: dict[str, Any], as_of_utc: datetime | None = None
    ) -> dict[str, Any]:
        as_of_text = iso_utc(as_of_utc) if as_of_utc is not None else None
        try:
            forecast_rows = self.db.execute(
                """
                SELECT model,forecast_max_c,forecast_peak_local,slot_utc,status
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
                       wind_speed,wind_speed_unit,wind_gust,visibility_m,pressure_hpa,
                       flight_category,sky_conditions_json,raw_metar,metar_parser_status,
                       weather_code,observed_daily_max_c,status
                FROM weather_observations WHERE station_id=? AND slot_utc=(
                    SELECT MAX(slot_utc) FROM weather_observations
                    WHERE station_id=? AND (? IS NULL OR slot_utc<=?)
                )
                """,
                (event["station_id"], event["station_id"], as_of_text, as_of_text),
            ).fetchall()
        except sqlite3.OperationalError:
            return {
                "otherModelsAndCurrent": {
                    "source": "database", "error": "external tables not initialized"
                },
                "metar": None,
            }

        allowed_models = {
            str(model) for model in self.config.get("openMeteoModels", ["ecmwf_ifs025"])
            if str(model)
        }
        maxima = [
            {
                "model": row["model"], "maxC": row["forecast_max_c"],
                "peakLocal": row["forecast_peak_local"],
            }
            for row in forecast_rows
            if row["status"] == "ok"
            and row["forecast_max_c"] is not None
            and row["model"] in allowed_models
        ]
        current_row = next(
            (row for row in observation_rows if row["source"] == "open_meteo_current"), None
        )
        metar_row = next((row for row in observation_rows if row["source"] == "metar"), None)
        current = None
        if current_row:
            current = {
                "time": current_row["observation_time_utc"],
                "temperature_2m": current_row["temperature_c"],
                "relative_humidity_2m": current_row["relative_humidity"],
                "precipitation": current_row["precipitation_mm"],
                "cloud_cover": current_row["cloud_cover_pct"],
                "wind_direction_10m": current_row["wind_direction_deg"],
                "wind_speed_10m": current_row["wind_speed"],
                "wind_speed_unit": current_row["wind_speed_unit"],
            }
        metar = None
        if metar_row:
            metar = {
                "obsTime": metar_row["observation_time_utc"],
                "temp": metar_row["temperature_c"],
                "dewp": metar_row["dewpoint_c"],
                "wdir": metar_row["wind_direction_deg"],
                "wspd": metar_row["wind_speed"],
                "wspdUnit": metar_row["wind_speed_unit"],
                "wgst": metar_row["wind_gust"],
                "visibilityM": metar_row["visibility_m"],
                "pressureHpa": metar_row["pressure_hpa"],
                "flightCategory": metar_row["flight_category"],
                "skyConditions": json_value(metar_row["sky_conditions_json"], []),
                "rawOb": metar_row["raw_metar"],
                "parserStatus": metar_row["metar_parser_status"],
                "wxString": metar_row["weather_code"],
                "dailyMaxC": metar_row["observed_daily_max_c"],
            }
        values = [row["maxC"] for row in maxima if row["maxC"] is not None]
        return {
            "otherModelsAndCurrent": {
                "source": "open-meteo-persisted",
                "sampleSlotUtc": forecast_rows[0]["slot_utc"] if forecast_rows else None,
                "current": current,
                "modelMaxima": maxima,
                "modelRangeC": [min(values), max(values)] if values else None,
            },
            "metar": metar,
        }

    def local_weather_payload(
        self, event: dict[str, Any], as_of_utc: datetime | None = None
    ) -> dict[str, Any]:
        persisted = self.persisted_external_weather(event, as_of_utc)
        return {
            "city": event["city"],
            "targetDate": event["target_date"],
            "analysisAsOfUtc": iso_utc(as_of_utc) if as_of_utc is not None else None,
            "station": {
                "id": event["station_id"],
                "name": event["station_name"],
                "latitude": event["latitude"],
                "longitude": event["longitude"],
                "timezone": event["timezone"],
                "resolutionSource": event["resolution_source"],
            },
            "resolutionRules": event["rules"],
            "meteoblue": self.latest_meteoblue(event, as_of_utc),
            "otherModelsAndCurrent": persisted["otherModelsAndCurrent"],
            "metar": persisted["metar"],
        }
