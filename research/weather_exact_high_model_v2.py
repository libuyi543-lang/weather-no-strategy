#!/usr/bin/env python3
"""Ridge V2 exact-high research model without upper-air sounding inputs."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.weather_exact_high_model import exact_temperature
from weather_process_analyzer import solar_position
from weather_market_monitor import parsed_metar_fields


DB_PATH = ROOT / "data/weather_market_monitor.sqlite3"
OUTPUT_DIR = ROOT / "research/output"
MODEL_DIR = ROOT / "data/models"
UTC = timezone.utc
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
TRADE_CITIES = [
    "Shanghai", "Beijing", "Guangzhou", "Qingdao", "Wuhan", "Chongqing", "Chengdu"
]
CUTOFF_MINUTES = tuple(range(10 * 60, 19 * 60 + 1, 30))
SHARED_ARTIFACT_NAME = "weather_exact_high_v21_shared.joblib"
NUMERIC_FEATURES = [
    "prior_center", "model_spread", "absolute_model_spread",
    "prior_same_hour_error", "observed_max_minus_prior",
    "observed_temp", "observed_max", "current_below_max",
    "trend_60", "trend_120", "trend_acceleration",
    "time_since_new_max_minutes", "dewpoint_depression", "dewpoint_trend_120",
    "cloud_cover_pct", "wind_speed_kt", "pressure_trend_120",
    "remaining_usable_solar", "elapsed_usable_solar", "remaining_solar_fraction",
    "trend_x_remaining_solar", "spread_x_clear_heating",
]
SHARED_TIME_FEATURES = [
    "cutoff_local_minutes", "cutoff_day_fraction",
    "elapsed_solar_fraction", "cutoff_x_trend_120",
    "cutoff_x_remaining_solar_fraction",
]
# The database currently stores the forecast collection slot in both
# model_updated_at_utc and model_ref_time_utc. Their derived "vintage" is a
# collection-schedule artifact, not model age, so it must remain diagnostic-only.
DIAGNOSTIC_FEATURES = [
    "model_vintage_age_hours", "wind_direction_deg", "wind_gust_kt",
    "precipitation_mm", "visibility_m", "solar_radiation_wm2",
]
SOLAR_MAINTENANCE_WM2 = 250.0
DEFAULT_MAX_OBSERVATION_AGE_MINUTES = 90.0
BUCKET_TEMPERATURE_GRID = (1.0, 1.05, 1.1, 1.15, 1.25, 1.5, 1.75, 2.0)
MIN_TEMPERATURE_CALIBRATION_CASES = 14


def finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def first_finite(*values: Any) -> float | None:
    """Return the first valid number without treating zero as missing."""
    for value in values:
        parsed = finite(value)
        if parsed is not None:
            return parsed
    return None


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def cutoff_is_due(
    target_date: str, timezone_name: str, cutoff_hour: int, cutoff_minute: int,
    as_of_utc: datetime,
) -> bool:
    try:
        local_cutoff = datetime.combine(
            date.fromisoformat(target_date), time(cutoff_hour, cutoff_minute),
            tzinfo=ZoneInfo(timezone_name),
        )
    except (TypeError, ValueError, KeyError):
        return False
    normalized = as_of_utc.replace(tzinfo=UTC) if as_of_utc.tzinfo is None else as_of_utc.astimezone(UTC)
    return normalized >= local_cutoff.astimezone(UTC)


def cloud_cover_from_sky_json(value: str | None) -> float | None:
    weights = {
        "SKC": 0.0, "CLR": 0.0, "NSC": 0.0, "FEW": 20.0,
        "SCT": 45.0, "BKN": 75.0, "OVC": 100.0,
    }
    try:
        rows = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    values = [
        weights.get(str(row.get("cover") or "").upper())
        for row in rows if isinstance(row, dict)
    ]
    values = [item for item in values if item is not None]
    return max(values) if values else None


def pairwise_slope(rows: list[dict[str, Any]], minutes: int) -> float | None:
    if not rows:
        return None
    newest = parse_time(rows[-1].get("observation_time_utc"))
    if newest is None:
        return None
    selected = [
        row for row in rows
        if (when := parse_time(row.get("observation_time_utc"))) is not None
        and newest - when <= timedelta(minutes=minutes)
    ]
    slopes = []
    for left_index, left in enumerate(selected):
        left_time = parse_time(left.get("observation_time_utc"))
        left_temp = finite(left.get("temperature_c"))
        if left_time is None or left_temp is None:
            continue
        for right in selected[left_index + 1:]:
            right_time = parse_time(right.get("observation_time_utc"))
            right_temp = finite(right.get("temperature_c"))
            if right_time is None or right_temp is None:
                continue
            hours = (right_time - left_time).total_seconds() / 3600.0
            if hours >= 0.45:
                slopes.append((right_temp - left_temp) / hours)
    return median(slopes) if slopes else None


def solar_energy_kwh_m2(
    latitude: float, longitude: float, start: datetime, end: datetime,
) -> float:
    if end <= start:
        return 0.0
    cursor = start
    step = timedelta(minutes=10)
    energy_wh = 0.0
    while cursor < end:
        next_cursor = min(end, cursor + step)
        midpoint = cursor + (next_cursor - cursor) / 2
        shortwave = solar_position(latitude, longitude, midpoint)["clearSkyShortwaveProxyWm2"]
        energy_wh += max(0.0, shortwave - SOLAR_MAINTENANCE_WM2) * (
            next_cursor - cursor
        ).total_seconds() / 3600.0
        cursor = next_cursor
    return energy_wh / 1000.0


def nearest_point(points_json: str | None, cutoff: datetime) -> dict[str, Any]:
    try:
        points = json.loads(points_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    candidates = []
    for point in points:
        when = parse_time(point.get("time_utc")) if isinstance(point, dict) else None
        if when is not None:
            candidates.append((abs((when - cutoff.astimezone(UTC)).total_seconds()), point))
    return min(candidates, key=lambda item: item[0])[1] if candidates else {}


def robust_field_trend(rows: list[dict[str, Any]], field: str, minutes: int) -> float | None:
    usable = []
    for row in rows:
        when = parse_time(row.get("observation_time_utc"))
        value = finite(row.get(field))
        if when is not None and value is not None:
            usable.append((when, value))
    if len(usable) < 2:
        return None
    newest = usable[-1][0]
    selected = [item for item in usable if (newest - item[0]).total_seconds() <= minutes * 60]
    if len(selected) < 2:
        return None
    hours = (selected[-1][0] - selected[0][0]).total_seconds() / 3600.0
    return (selected[-1][1] - selected[0][1]) / hours if hours > 0 else None


def round_half_up(values: np.ndarray | float) -> np.ndarray:
    return np.floor(np.asarray(values, dtype=float) + 0.5)


def observed_floor(frame: pd.DataFrame, prediction: np.ndarray) -> np.ndarray:
    floor = frame["observed_max"].to_numpy(dtype=float)
    floor = np.where(np.isfinite(floor), floor, -np.inf)
    return np.maximum(np.asarray(prediction, dtype=float), floor)


def physically_constrained_center(row: Any, raw_prediction: float) -> float:
    """Stop extrapolating once the observed heating process is physically capped."""
    observed_max = float(row["observed_max"])
    central = max(observed_max, float(raw_prediction))
    remaining_solar = finite(row.get("remaining_usable_solar"))
    trend = finite(row.get("trend_120"))
    below_max = finite(row.get("current_below_max")) or 0.0
    heating_ended = (trend is not None and trend <= 0) or below_max > 0.01
    if (
        remaining_solar is not None
        and remaining_solar <= 0.25
        and heating_ended
    ):
        return observed_max
    return central


def physically_constrained_predictions(
    frame: pd.DataFrame, raw_prediction: np.ndarray,
) -> np.ndarray:
    return np.asarray([
        physically_constrained_center(row, value)
        for (_, row), value in zip(frame.iterrows(), np.asarray(raw_prediction, dtype=float))
    ])


def temperature_scale_probabilities(
    probabilities: dict[int, float], temperature: float,
) -> dict[int, float]:
    """Flatten or sharpen non-zero bucket probabilities without changing their ranking."""
    parsed_temperature = finite(temperature) or 1.0
    parsed_temperature = max(0.25, parsed_temperature)
    powered = {
        int(bucket): max(0.0, float(probability)) ** (1.0 / parsed_temperature)
        for bucket, probability in probabilities.items()
    }
    total = sum(powered.values())
    if total <= 0:
        return {bucket: 0.0 for bucket in powered}
    return {bucket: value / total for bucket, value in powered.items()}


class DatasetBuilderV2:
    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.has_resolution_labels = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='weather_resolution_labels'"
        ).fetchone() is not None
        self.has_fast_metar_reports = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fast_metar_reports'"
        ).fetchone() is not None

    def close(self) -> None:
        self.db.close()

    def forecast_rows(
        self, table: str, station_id: str, target_date: str,
        available_as_of_utc: str, model: str,
    ) -> list[sqlite3.Row]:
        return self.db.execute(
            f"""SELECT * FROM {table}
                WHERE station_id=? AND target_date=? AND model=? AND status='ok'
                  AND forecast_max_c IS NOT NULL
                  AND julianday(slot_utc)<=julianday(?)
                  AND julianday(fetched_at_utc)<=julianday(?)
                ORDER BY julianday(slot_utc) DESC,julianday(fetched_at_utc) DESC LIMIT 12""",
            (station_id, target_date, model, available_as_of_utc, available_as_of_utc),
        ).fetchall()

    @staticmethod
    def genuine_revision(rows: list[sqlite3.Row], reference_field: str | None = None) -> float | None:
        if len(rows) < 2:
            return None
        current = finite(rows[0]["forecast_max_c"])
        current_reference = rows[0][reference_field] if reference_field else None
        for previous in rows[1:]:
            previous_value = finite(previous["forecast_max_c"])
            if previous_value is None or current is None:
                continue
            if reference_field and previous[reference_field] == current_reference:
                continue
            if abs(previous_value - current) >= 0.05 or reference_field:
                return current - previous_value
        return 0.0

    def _observations(
        self, station_id: str, start_utc: str, available_as_of_utc: str,
    ) -> list[dict[str, Any]]:
        """Return the best METAR timeline visible at a historical cutoff.

        Enriched observations win when the same observation time exists in both
        tables; fast reports only fill missing times.  ``first_fetched_at_utc``
        is the receipt boundary for the fast table so later backfills cannot
        leak into an earlier training decision.
        """
        observations = [dict(row) for row in self.db.execute(
            """SELECT * FROM (
                   SELECT observation_time_utc,temperature_c,dewpoint_c,relative_humidity,
                          precipitation_mm,cloud_cover_pct,wind_direction_deg,wind_speed,
                          wind_speed_unit,wind_gust,visibility_m,pressure_hpa,
                          solar_radiation_wm2,direct_radiation_wm2,diffuse_radiation_wm2,
                          sky_conditions_json,weather_code,metar_type,slot_utc,fetched_at_utc,
                          ROW_NUMBER() OVER (
                              PARTITION BY observation_time_utc
                              ORDER BY julianday(fetched_at_utc) DESC,julianday(slot_utc) DESC
                          ) AS point_in_time_rank
                   FROM weather_observations
                   WHERE station_id=? AND source='metar' AND status='ok'
                     AND julianday(observation_time_utc)>=julianday(?)
                     AND julianday(observation_time_utc)<=julianday(?)
                     AND julianday(slot_utc)<=julianday(?)
                     AND julianday(fetched_at_utc)<=julianday(?)
               ) WHERE point_in_time_rank=1
               ORDER BY julianday(observation_time_utc)""",
            (station_id, start_utc, available_as_of_utc, available_as_of_utc, available_as_of_utc),
        ).fetchall()]
        if not self.has_fast_metar_reports:
            return observations

        fast_rows = self.db.execute(
            """SELECT * FROM fast_metar_reports
               WHERE station_id=?
                 AND julianday(observation_time_utc)>=julianday(?)
                 AND julianday(observation_time_utc)<=julianday(?)
                 AND julianday(first_fetched_at_utc)<=julianday(?)
               ORDER BY julianday(observation_time_utc),julianday(first_fetched_at_utc) DESC""",
            (station_id, start_utc, available_as_of_utc, available_as_of_utc),
        ).fetchall()
        existing_times = {str(row.get("observation_time_utc")) for row in observations}
        for row in fast_rows:
            observation_time = str(row["observation_time_utc"])
            if observation_time in existing_times:
                continue
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            parsed = parsed_metar_fields(row["raw_metar"])
            def payload_number(key: str) -> float | None:
                return finite(payload.get(key))

            visibility = finite(parsed.get("visibility_m"))
            if visibility is None:
                visib_miles = payload_number("visib")
                visibility = visib_miles * 1609.344 if visib_miles is not None else None
            observations.append({
                "observation_time_utc": observation_time,
                "temperature_c": first_finite(parsed.get("temperature_c"), payload.get("temp")),
                "dewpoint_c": first_finite(parsed.get("dewpoint_c"), payload.get("dewp")),
                "relative_humidity": None, "precipitation_mm": None,
                "cloud_cover_pct": first_finite(payload.get("cover")),
                "wind_direction_deg": first_finite(parsed.get("wind_direction_deg"), payload.get("wdir")),
                "wind_speed": first_finite(parsed.get("wind_speed"), payload.get("wspd")),
                "wind_speed_unit": "kt", "wind_gust": first_finite(parsed.get("wind_gust"), payload.get("wgst")),
                "visibility_m": visibility,
                "pressure_hpa": first_finite(parsed.get("pressure_hpa"), payload.get("altim")),
                "solar_radiation_wm2": None, "direct_radiation_wm2": None,
                "diffuse_radiation_wm2": None,
                "sky_conditions_json": parsed.get("sky_conditions_json") or json.dumps(payload.get("clouds") or []),
                "weather_code": parsed.get("weather_code") or payload.get("wxString"),
                "metar_type": row["metar_type"], "slot_utc": row["report_time_utc"],
                "fetched_at_utc": row["first_fetched_at_utc"],
            })
            existing_times.add(observation_time)
        return sorted(observations, key=lambda item: item.get("observation_time_utc") or "")

    def build(
        self, cutoff_hour: int, cutoff_minute: int = 0,
        available_as_of_utc: datetime | None = None,
    ) -> pd.DataFrame:
        label_columns = (
            "l.official_temperature_c,l.exact_at_resolution_precision"
            if self.has_resolution_labels
            else "NULL AS official_temperature_c,0 AS exact_at_resolution_precision"
        )
        label_join = (
            "LEFT JOIN weather_resolution_labels l ON l.event_id=e.event_id"
            if self.has_resolution_labels else ""
        )
        city_placeholders = ",".join("?" for _ in TRADE_CITIES)
        events = self.db.execute(
            f"""SELECT e.*,s.latitude,s.longitude,s.timezone,{label_columns}
               FROM events e JOIN stations s ON s.station_id=e.station_id
               {label_join}
               WHERE e.city IN ({city_placeholders}) AND s.timezone IS NOT NULL
               ORDER BY e.target_date,e.city""",
            TRADE_CITIES,
        ).fetchall()
        output: list[dict[str, Any]] = []
        for event in events:
            try:
                tz = ZoneInfo(event["timezone"])
                day = date.fromisoformat(event["target_date"])
                local_start = datetime.combine(day, time(0), tzinfo=tz)
                local_cutoff = datetime.combine(day, time(cutoff_hour, cutoff_minute), tzinfo=tz)
            except (TypeError, ValueError):
                continue
            start_utc = local_start.astimezone(UTC).isoformat(timespec="seconds")
            availability = available_as_of_utc or local_cutoff.astimezone(UTC)
            if availability.tzinfo is None:
                availability = availability.replace(tzinfo=UTC)
            availability_utc = availability.astimezone(UTC).isoformat(timespec="seconds")
            mblue_rows = self.forecast_rows(
                "windy_forecasts", event["station_id"], event["target_date"],
                availability_utc, "mblue",
            )
            ecmwf_rows = self.forecast_rows(
                "external_forecasts", event["station_id"], event["target_date"],
                availability_utc, "ecmwf_ifs025",
            )
            if not mblue_rows:
                continue
            observations = self._observations(
                event["station_id"], start_utc, availability_utc,
            )
            if not observations:
                continue
            current = observations[-1]
            current_time = parse_time(current["observation_time_utc"])
            if current_time is None:
                continue
            observed_temp = finite(current["temperature_c"])
            temperatures = [finite(row["temperature_c"]) for row in observations]
            temperatures = [value for value in temperatures if value is not None]
            if observed_temp is None or not temperatures:
                continue
            observed_max = max(temperatures)
            max_times = [
                parse_time(row["observation_time_utc"])
                for row in observations if finite(row["temperature_c"]) == observed_max
            ]
            last_max_time = max(item for item in max_times if item is not None)
            time_since_max = (current_time - last_max_time).total_seconds() / 60.0

            mblue = finite(mblue_rows[0]["forecast_max_c"])
            ecmwf = finite(ecmwf_rows[0]["forecast_max_c"]) if ecmwf_rows else None
            if mblue is None:
                continue
            prior = (mblue + ecmwf) / 2.0 if ecmwf is not None else mblue
            spread = ecmwf - mblue if ecmwf is not None else 0.0
            mb_point = nearest_point(mblue_rows[0]["points_json"], local_cutoff)
            ec_point = nearest_point(ecmwf_rows[0]["points_json"], local_cutoff) if ecmwf_rows else {}
            mb_same = finite(mb_point.get("temp_c"))
            ec_same = finite(ec_point.get("temp_c"))
            same_prior = (
                (mb_same + ec_same) / 2.0 if mb_same is not None and ec_same is not None
                else mb_same if mb_same is not None else ec_same
            )
            same_error = observed_temp - same_prior if same_prior is not None else None

            trend60 = pairwise_slope(observations, 75)
            trend120 = pairwise_slope(observations, 135)
            if trend60 is None:
                trend60 = trend120
            if trend120 is None:
                trend120 = trend60
            acceleration = (
                trend60 - trend120 if trend60 is not None and trend120 is not None else None
            )
            dewpoint = finite(current.get("dewpoint_c"))
            cloud = cloud_cover_from_sky_json(current.get("sky_conditions_json"))
            if cloud is None:
                cloud = finite(current.get("cloud_cover_pct"))
            wind_speed = finite(current.get("wind_speed"))
            if str(current.get("wind_speed_unit") or "").lower() in {"m/s", "mps"} and wind_speed is not None:
                wind_speed *= 1.94384

            local_solar_start = datetime.combine(day, time(7), tzinfo=tz).astimezone(UTC)
            local_solar_end = datetime.combine(day, time(20), tzinfo=tz).astimezone(UTC)
            elapsed_solar = solar_energy_kwh_m2(
                float(event["latitude"]), float(event["longitude"]),
                local_solar_start, current_time,
            )
            remaining_solar = solar_energy_kwh_m2(
                float(event["latitude"]), float(event["longitude"]),
                current_time, local_solar_end,
            )
            total_solar = elapsed_solar + remaining_solar
            cutoff_local_minutes = cutoff_hour * 60 + cutoff_minute
            cutoff_day_fraction = cutoff_local_minutes / (24.0 * 60.0)
            elapsed_solar_fraction = elapsed_solar / total_solar if total_solar > 0 else 0.0
            clear_indicator = 1.0 if cloud is not None and cloud <= 45.0 else 0.0
            vintage_time = parse_time(mblue_rows[0]["model_updated_at_utc"] or mblue_rows[0]["model_ref_time_utc"])
            vintage_age = (
                (local_cutoff.astimezone(UTC) - vintage_time).total_seconds() / 3600.0
                if vintage_time else None
            )
            official_target = finite(event["official_temperature_c"])
            target = (
                official_target
                if int(event["exact_at_resolution_precision"] or 0) == 1
                else exact_temperature(event["winning_range"])
            )
            output.append({
                "event_id": event["event_id"], "target_date": event["target_date"],
                "city": event["city"], "station_id": event["station_id"],
                "timezone": event["timezone"],
                "cutoff_hour": cutoff_hour, "cutoff_minute": cutoff_minute,
                "cutoff_local_minutes": cutoff_local_minutes,
                "cutoff_day_fraction": cutoff_day_fraction,
                "feature_as_of_utc": availability_utc,
                "latest_observation_time_utc": current["observation_time_utc"],
                "latest_observation_fetched_at_utc": current["fetched_at_utc"],
                "latest_observation_slot_utc": current["slot_utc"],
                "meteoblue_fetched_at_utc": mblue_rows[0]["fetched_at_utc"],
                "ecmwf_fetched_at_utc": ecmwf_rows[0]["fetched_at_utc"] if ecmwf_rows else None,
                "resolved": target is not None, "target": target,
                "winning_range": event["winning_range"],
                "mblue": mblue, "ecmwf": ecmwf, "prior_center": prior,
                "model_spread": spread, "absolute_model_spread": abs(spread),
                "mblue_revision": self.genuine_revision(mblue_rows, "model_ref_time_utc"),
                "ecmwf_revision": self.genuine_revision(ecmwf_rows),
                "prior_same_hour_error": same_error,
                "observed_max_minus_prior": observed_max - prior,
                "observed_temp": observed_temp, "observed_max": observed_max,
                "current_below_max": observed_max - observed_temp,
                "trend_60": trend60, "trend_120": trend120,
                "trend_acceleration": acceleration,
                "time_since_new_max_minutes": time_since_max,
                "dewpoint": dewpoint,
                "dewpoint_depression": observed_temp - dewpoint if dewpoint is not None else None,
                "dewpoint_trend_120": robust_field_trend(observations, "dewpoint_c", 135),
                "cloud_cover_pct": cloud, "wind_speed_kt": wind_speed,
                "wind_direction_deg": finite(current.get("wind_direction_deg")),
                "wind_gust_kt": finite(current.get("wind_gust")),
                "precipitation_mm": finite(current.get("precipitation_mm")),
                "visibility_m": finite(current.get("visibility_m")),
                "solar_radiation_wm2": finite(current.get("solar_radiation_wm2")),
                "pressure_trend_120": robust_field_trend(observations, "pressure_hpa", 135),
                "remaining_usable_solar": remaining_solar,
                "elapsed_usable_solar": elapsed_solar,
                "remaining_solar_fraction": remaining_solar / total_solar if total_solar > 0 else 0.0,
                "elapsed_solar_fraction": elapsed_solar_fraction,
                "trend_x_remaining_solar": (
                    trend120 * remaining_solar if trend120 is not None else None
                ),
                "cutoff_x_trend_120": (
                    cutoff_day_fraction * trend120 if trend120 is not None else None
                ),
                "cutoff_x_remaining_solar_fraction": (
                    cutoff_day_fraction * (remaining_solar / total_solar)
                    if total_solar > 0 else 0.0
                ),
                "spread_x_clear_heating": spread * clear_indicator * (1.0 if remaining_solar > 0.25 else 0.0),
                "model_vintage_age_hours": vintage_age,
                "observation_age_minutes": (
                    availability.astimezone(UTC) - current_time
                ).total_seconds() / 60.0,
                "observation_count": len(observations),
            })
        return pd.DataFrame(output).drop_duplicates(
            ["event_id", "cutoff_hour", "cutoff_minute"], keep="last"
        ).reset_index(drop=True) if output else pd.DataFrame()


def available_features(frame: pd.DataFrame, shared_time: bool = False) -> list[str]:
    candidates = NUMERIC_FEATURES + (SHARED_TIME_FEATURES if shared_time else [])
    return [name for name in candidates if name in frame and frame[name].notna().any()]


def make_model(train: pd.DataFrame, shared_time: bool = False) -> tuple[Pipeline, list[str]]:
    numeric = available_features(train, shared_time=shared_time)
    preprocessing = ColumnTransformer([
        ("numeric", Pipeline([
            ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
            ("scaler", StandardScaler()),
        ]), numeric),
        ("city", OneHotEncoder(handle_unknown="ignore"), ["city"]),
    ])
    model = Pipeline([
        ("preprocessing", preprocessing),
        # Stronger shrinkage than V1 because V2 has explicit interaction terms
        # and the number of independent event dates remains very small.
        ("regressor", Ridge(alpha=20.0)),
    ])
    model.fit(train, train["target"] - train["prior_center"])
    return model, numeric


def build_shared_frame(
    builder: DatasetBuilderV2, available_as_of_utc: datetime | None = None,
) -> pd.DataFrame:
    """Pool all cutoffs into one time-aware dataset without crossing PIT boundaries."""
    frames = []
    for cutoff_minutes in CUTOFF_MINUTES:
        hour, minute = divmod(cutoff_minutes, 60)
        frame = builder.build(hour, minute, available_as_of_utc=available_as_of_utc)
        if not frame.empty:
            frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def metrics(target: np.ndarray, prediction: np.ndarray) -> dict[str, float]:
    error = np.asarray(prediction) - np.asarray(target)
    return {
        "mae_c": round(float(np.mean(np.abs(error))), 4),
        "rmse_c": round(float(np.sqrt(np.mean(error * error))), 4),
        "bias_c": round(float(np.mean(error)), 4),
        "exact_bucket_accuracy": round(float(np.mean(round_half_up(prediction) == round_half_up(target))), 4),
        "within_one_c": round(float(np.mean(np.abs(error) <= 1.0)), 4),
    }


def walk_forward(frame: pd.DataFrame) -> dict[str, Any]:
    resolved = frame[frame["resolved"]].copy()
    dates = sorted(resolved["target_date"].unique())
    rows: list[dict[str, Any]] = []
    folds: list[dict[str, Any]] = []
    for index in range(2, len(dates)):
        train = resolved[resolved["target_date"].isin(dates[:index])]
        test = resolved[resolved["target_date"] == dates[index]]
        if train.empty or test.empty:
            continue
        model, features = make_model(train)
        prediction = physically_constrained_predictions(
            test, test["prior_center"].to_numpy() + model.predict(test)
        )
        prior_prediction = observed_floor(test, test["prior_center"].to_numpy())
        mb_prediction = observed_floor(test, test["mblue"].to_numpy())
        observed_prediction = test["observed_max"].to_numpy(dtype=float)
        target = test["target"].to_numpy()
        folds.append({
            "test_date": dates[index], "train_events": int(len(train)), "test_events": int(len(test)),
            "features": features, "v2": metrics(target, prediction),
            "ensemble_prior": metrics(target, prior_prediction), "meteoblue": metrics(target, mb_prediction),
            "observed_max": metrics(target, observed_prediction),
        })
        for offset, (_, row) in enumerate(test.iterrows()):
            rows.append({
                "event_id": row["event_id"], "target_date": row["target_date"], "city": row["city"],
                "actual_c": float(row["target"]), "prediction_c": float(prediction[offset]),
                "prior_c": float(prior_prediction[offset]), "mblue_c": float(mb_prediction[offset]),
                "observed_max_c": float(observed_prediction[offset]),
            })
    if not rows:
        return {"oos_events": 0, "folds": folds, "predictions": []}
    actual = np.array([row["actual_c"] for row in rows])
    v2 = np.array([row["prediction_c"] for row in rows])
    prior = np.array([row["prior_c"] for row in rows])
    mb = np.array([row["mblue_c"] for row in rows])
    observed = np.array([row["observed_max_c"] for row in rows])
    v2_metrics = metrics(actual, v2)
    observed_metrics = metrics(actual, observed)
    return {
        "oos_events": len(rows), "test_dates": sorted({row["target_date"] for row in rows}),
        "metrics": {"ridge_v2": v2_metrics, "ensemble_prior": metrics(actual, prior),
                    "meteoblue": metrics(actual, mb), "observed_max": observed_metrics},
        "incremental_mae_vs_observed_max_c": round(
            observed_metrics["mae_c"] - v2_metrics["mae_c"], 4
        ),
        "mean_remaining_headroom_c": round(float(np.mean(actual - observed)), 4),
        "abs_error_q80_c": round(float(np.quantile(np.abs(v2 - actual), 0.8)), 3),
        "abs_error_q90_c": round(float(np.quantile(np.abs(v2 - actual), 0.9)), 3),
        "folds": folds, "predictions": rows,
    }


def _bucket_case_metrics(cases: list[dict[str, Any]], temperature: float) -> dict[str, float | None]:
    if not cases:
        return {
            "brierScore": None, "logLoss": None,
            "meanTopBucketProbability": None, "topBucketAccuracy": None,
        }
    brier_scores = []
    log_losses = []
    top_probabilities = []
    top_hits = []
    for case in cases:
        probabilities = temperature_scale_probabilities(
            case["probabilities"], temperature
        )
        actual_bucket = int(case["actualBucket"])
        candidate_buckets = set(probabilities) | {actual_bucket}
        brier_scores.append(sum(
            (probabilities.get(bucket, 0.0) - float(bucket == actual_bucket)) ** 2
            for bucket in candidate_buckets
        ))
        log_losses.append(-math.log(max(probabilities.get(actual_bucket, 0.0), 1e-6)))
        top_bucket, top_probability = max(
            probabilities.items(), key=lambda item: (item[1], -item[0])
        )
        top_probabilities.append(top_probability)
        top_hits.append(float(top_bucket == actual_bucket))
    return {
        "brierScore": float(np.mean(brier_scores)),
        "logLoss": float(np.mean(log_losses)),
        "meanTopBucketProbability": float(np.mean(top_probabilities)),
        "topBucketAccuracy": float(np.mean(top_hits)),
    }


def _best_bucket_temperature(cases: list[dict[str, Any]]) -> float:
    if len(cases) < MIN_TEMPERATURE_CALIBRATION_CASES:
        return 1.0
    scored = []
    for temperature in BUCKET_TEMPERATURE_GRID:
        metrics_row = _bucket_case_metrics(cases, temperature)
        scored.append((float(metrics_row["logLoss"]), abs(temperature - 1.0), temperature))
    return float(min(scored)[2])


def _prequential_calibration(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Evaluate each date with residuals from earlier OOS dates only."""
    history: dict[int, list[float]] = {}
    calibration_history: dict[int, list[dict[str, Any]]] = {}
    coverage_hits = {target: [] for target in (0.5, 0.8, 0.9)}
    coverage_radii = {target: [] for target in (0.5, 0.8, 0.9)}
    raw_cases: list[dict[str, Any]] = []
    calibrated_cases: list[dict[str, Any]] = []
    applied_temperatures: list[float] = []
    evaluated_dates: set[str] = set()
    dates = sorted({str(row["target_date"]) for row in rows})
    for target_date in dates:
        current = [row for row in rows if str(row["target_date"]) == target_date]
        pending_cases: list[tuple[int, dict[str, Any]]] = []
        for row in current:
            cutoff = int(row["cutoffLocalMinutes"])
            prior = history.get(cutoff) or []
            if not prior:
                continue
            evaluated_dates.add(target_date)
            absolute = np.abs(np.asarray(prior, dtype=float))
            actual_residual = abs(float(row["residualC"]))
            for target in coverage_hits:
                radius = float(np.quantile(absolute, target))
                coverage_radii[target].append(radius)
                coverage_hits[target].append(float(actual_residual <= radius + 1e-12))

            counts: dict[int, int] = {}
            prediction = float(row["predictionC"])
            observed_max = float(row["observedMaxC"])
            for residual in prior:
                bucket = int(round_half_up(max(observed_max, prediction + residual)))
                counts[bucket] = counts.get(bucket, 0) + 1
            total = float(sum(counts.values()))
            actual_bucket = int(round_half_up(float(row["actualC"])))
            case = {
                "probabilities": {
                    bucket: count / total for bucket, count in counts.items()
                },
                "actualBucket": actual_bucket,
            }
            temperature = _best_bucket_temperature(calibration_history.get(cutoff) or [])
            raw_cases.append(case)
            calibrated_cases.append({
                "probabilities": temperature_scale_probabilities(
                    case["probabilities"], temperature
                ),
                "actualBucket": actual_bucket,
            })
            applied_temperatures.append(temperature)
            pending_cases.append((cutoff, case))
        for row in current:
            history.setdefault(int(row["cutoffLocalMinutes"]), []).append(float(row["residualC"]))
        for cutoff, case in pending_cases:
            calibration_history.setdefault(cutoff, []).append(case)

    coverage: dict[str, Any] = {}
    for target, hits in coverage_hits.items():
        coverage[str(int(target * 100))] = {
            "target": target, "meanRadiusC": (
                round(float(np.mean(coverage_radii[target])), 3) if hits else None
            ),
            "observedCoverage": round(float(np.mean(hits)), 4) if hits else None,
            "absoluteError": (
                round(abs(float(np.mean(hits)) - target), 4) if hits else None
            ),
            "evaluatedEvents": len(hits), "evaluatedDates": len(evaluated_dates),
            "method": "prior_oos_dates_same_cutoff_only",
        }
    raw_metrics = _bucket_case_metrics(raw_cases, 1.0)
    calibrated_metrics = _bucket_case_metrics(calibrated_cases, 1.0)
    temperatures_by_cutoff = {
        f"{cutoff // 60:02d}{cutoff % 60:02d}": _best_bucket_temperature(cases)
        for cutoff, cases in sorted(calibration_history.items())
    }
    raw_gap = (
        abs(float(raw_metrics["meanTopBucketProbability"]) - float(raw_metrics["topBucketAccuracy"]))
        if raw_metrics["meanTopBucketProbability"] is not None else None
    )
    calibrated_gap = (
        abs(
            float(calibrated_metrics["meanTopBucketProbability"])
            - float(calibrated_metrics["topBucketAccuracy"])
        )
        if calibrated_metrics["meanTopBucketProbability"] is not None else None
    )
    has_nontrivial_temperature = any(
        abs(temperature - 1.0) > 1e-9
        for temperature in temperatures_by_cutoff.values()
    )
    calibration_improved = bool(
        len(evaluated_dates) >= 10
        and has_nontrivial_temperature
        and calibrated_metrics["logLoss"] is not None
        and raw_metrics["logLoss"] is not None
        and float(calibrated_metrics["logLoss"]) < float(raw_metrics["logLoss"]) - 1e-4
        and calibrated_gap is not None and raw_gap is not None
        and calibrated_gap <= raw_gap + 1e-12
    )
    selected_metrics = calibrated_metrics if calibration_improved else raw_metrics
    bucket_calibration = {
        "evaluatedEvents": len(raw_cases), "evaluatedDates": len(evaluated_dates),
        **{
            key: round(float(value), 4) if value is not None else None
            for key, value in selected_metrics.items()
        },
        "rawBrierScore": round(float(raw_metrics["brierScore"]), 4) if raw_metrics["brierScore"] is not None else None,
        "rawLogLoss": round(float(raw_metrics["logLoss"]), 4) if raw_metrics["logLoss"] is not None else None,
        "rawMeanTopBucketProbability": round(float(raw_metrics["meanTopBucketProbability"]), 4) if raw_metrics["meanTopBucketProbability"] is not None else None,
        "method": "prior_oos_dates_same_cutoff_prequential_temperature",
    }
    probability_calibration = {
        "method": "temperature_scaling_by_cutoff",
        "enabled": calibration_improved,
        "temperaturesByCutoff": temperatures_by_cutoff,
        "minimumCasesPerCutoff": MIN_TEMPERATURE_CALIBRATION_CASES,
        "prequentialEvaluatedEvents": len(raw_cases),
        "prequentialEvaluatedDates": len(evaluated_dates),
        "meanAppliedTemperature": (
            round(float(np.mean(applied_temperatures)), 4)
            if applied_temperatures else None
        ),
        "rawLogLoss": bucket_calibration["rawLogLoss"],
        "calibratedLogLoss": (
            round(float(calibrated_metrics["logLoss"]), 4)
            if calibrated_metrics["logLoss"] is not None else None
        ),
        "rawTopConfidenceGap": round(raw_gap, 4) if raw_gap is not None else None,
        "calibratedTopConfidenceGap": (
            round(calibrated_gap, 4) if calibrated_gap is not None else None
        ),
    }
    return coverage, bucket_calibration, probability_calibration


def walk_forward_shared(frame: pd.DataFrame) -> dict[str, Any]:
    """Date-grouped OOS evaluation for the shared center and V3 residuals."""
    resolved = frame[frame["resolved"]].copy()
    dates = sorted(resolved["target_date"].unique())
    rows: list[dict[str, Any]] = []
    folds: list[dict[str, Any]] = []
    for index in range(2, len(dates)):
        train = resolved[resolved["target_date"].isin(dates[:index])]
        test = resolved[resolved["target_date"] == dates[index]]
        if train.empty or test.empty:
            continue
        model, features = make_model(train, shared_time=True)
        prediction = physically_constrained_predictions(
            test, test["prior_center"].to_numpy() + model.predict(test)
        )
        target = test["target"].to_numpy(dtype=float)
        folds.append({
            "test_date": dates[index], "train_events": int(len(train)),
            "test_events": int(len(test)), "features": features,
            "ridge_v21": metrics(target, prediction),
        })
        for offset, (_, row) in enumerate(test.iterrows()):
            residual = float(target[offset] - prediction[offset])
            rows.append({
                "event_id": row["event_id"], "target_date": row["target_date"],
                "city": row["city"], "cutoffLocalMinutes": int(row["cutoff_local_minutes"]),
                "actualC": float(target[offset]), "predictionC": float(prediction[offset]),
                "observedMaxC": float(row["observed_max"]), "residualC": residual,
            })
    if not rows:
        return {
            "oos_events": 0, "oos_dates": [], "residuals_c": [],
            "calibration_status": "insufficient_independent_oos_dates",
            "coverage_targets": {}, "folds": folds,
        }
    residuals = np.asarray([row["residualC"] for row in rows], dtype=float)
    oos_dates = sorted({row["target_date"] for row in rows})
    coverage_targets, bucket_calibration, probability_calibration = _prequential_calibration(rows)
    residuals_by_cutoff: dict[str, list[float]] = {}
    for row in rows:
        label = f"{int(row['cutoffLocalMinutes']) // 60:02d}{int(row['cutoffLocalMinutes']) % 60:02d}"
        residuals_by_cutoff.setdefault(label, []).append(round(float(row["residualC"]), 4))
    coverage_errors = [
        finite(item.get("absoluteError"))
        for item in coverage_targets.values() if isinstance(item, dict)
    ]
    coverage_errors = [value for value in coverage_errors if value is not None]
    confidence_gap = None
    if (
        finite(bucket_calibration.get("meanTopBucketProbability")) is not None
        and finite(bucket_calibration.get("topBucketAccuracy")) is not None
    ):
        confidence_gap = abs(
            float(bucket_calibration["meanTopBucketProbability"])
            - float(bucket_calibration["topBucketAccuracy"])
        )
    enough_dates = len(oos_dates) >= 30
    calibration_passed = bool(
        enough_dates and coverage_errors and max(coverage_errors) <= 0.08
        and confidence_gap is not None and confidence_gap <= 0.10
    )
    calibration_status = (
        "calibrated_research_only" if calibration_passed
        else "calibration_failed" if enough_dates
        else "insufficient_independent_oos_dates"
    )
    return {
        "oos_events": len(rows), "oos_dates": oos_dates,
        "metrics": metrics(
            np.asarray([row["actualC"] for row in rows], dtype=float),
            np.asarray([row["predictionC"] for row in rows], dtype=float),
        ),
        "residuals_c": [round(float(value), 4) for value in residuals],
        "cold_residuals_c": [round(float(value), 4) for value in residuals if value < 0],
        "warm_residuals_c": [round(float(value), 4) for value in residuals if value > 0],
        "zero_residual_count": int(np.sum(np.isclose(residuals, 0.0))),
        "cold_tail_rate": round(float(np.mean(residuals < 0)), 4),
        "warm_tail_rate": round(float(np.mean(residuals > 0)), 4),
        "coverage_targets": coverage_targets,
        "bucket_calibration": bucket_calibration,
        "bucket_probability_calibration": probability_calibration,
        "calibration_quality": {
            "minimumIndependentOosDates": 30,
            "independentOosDates": len(oos_dates),
            "maximumCoverageAbsoluteError": (
                round(max(coverage_errors), 4) if coverage_errors else None
            ),
            "topConfidenceAccuracyGap": (
                round(confidence_gap, 4) if confidence_gap is not None else None
            ),
            "passed": calibration_passed,
        },
        "residuals_by_cutoff": residuals_by_cutoff,
        "calibration_status": calibration_status,
        "folds": folds, "predictions": rows,
    }


def _atomic_joblib_dump(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        joblib.dump(payload, temporary)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def train_shared_artifact(
    db_path: Path, target_date: str, as_of_utc: datetime,
    artifact_path: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    """Train V2.1 once for a date and attach the research-only V3.1 distribution."""
    builder = DatasetBuilderV2(db_path)
    try:
        frame = build_shared_frame(builder)
    finally:
        builder.close()
    if frame.empty:
        raise RuntimeError("shared Ridge V2.1 dataset is empty")
    train = frame[frame["resolved"] & (frame["target_date"] < target_date)].copy()
    if train.empty:
        raise RuntimeError(f"no resolved Ridge V2.1 rows before {target_date}")
    evaluation = walk_forward_shared(train)
    model, features = make_model(train, shared_time=True)
    oos_dates = list(evaluation.get("oos_dates") or [])
    calibration_status = str(
        evaluation.get("calibration_status") or "insufficient_independent_oos_dates"
    )
    artifact = artifact_path or MODEL_DIR / SHARED_ARTIFACT_NAME
    payload = {
        "model": model, "features": features,
        "model_version": "ridge_v2.1_shared_time",
        "distribution_version": "ridge_v3.1_temperature_calibrated",
        "trained_at_utc": as_of_utc.astimezone(UTC).isoformat(timespec="seconds"),
        "trained_through": str(max(train["target_date"])),
        "training_target_date": target_date,
        "train_events": int(len(train)), "research_only": True,
        "authoritative": False, "uses_sounding": False,
        "prior": "equal Meteoblue/ECMWF center",
        "point_in_time_data": True,
        "warm_tail_buffer_c": float(
            np.quantile(np.abs(evaluation.get("residuals_c") or [1.0]), 0.8)
        ),
        "oos_events": int(evaluation.get("oos_events") or 0),
        "oos_dates": oos_dates,
        "residuals_c": list(evaluation.get("residuals_c") or []),
        "residuals_by_cutoff": dict(evaluation.get("residuals_by_cutoff") or {}),
        "cold_residuals_c": list(evaluation.get("cold_residuals_c") or []),
        "warm_residuals_c": list(evaluation.get("warm_residuals_c") or []),
        "cold_tail_rate": evaluation.get("cold_tail_rate"),
        "warm_tail_rate": evaluation.get("warm_tail_rate"),
        "coverage_targets": evaluation.get("coverage_targets") or {},
        "bucket_calibration": evaluation.get("bucket_calibration") or {},
        "bucket_probability_calibration": (
            evaluation.get("bucket_probability_calibration") or {}
        ),
        "calibration_quality": evaluation.get("calibration_quality") or {},
        "calibration_status": calibration_status,
        "minimum_authoritative_oos_dates": 30,
        "evaluation": evaluation,
    }
    _atomic_joblib_dump(payload, artifact)
    return {
        "artifact": str(artifact), "trained_through": payload["trained_through"],
        "train_events": payload["train_events"], "features": features,
        "evaluation": evaluation,
    }, artifact


def train_current(
    frame: pd.DataFrame, cutoff_hour: int, cutoff_minute: int,
    evaluation: dict[str, Any], target_date: str, as_of_utc: datetime,
) -> tuple[dict[str, Any], Path]:
    train = frame[frame["resolved"] & (frame["target_date"] < target_date)].copy()
    current_candidates = frame[
        (~frame["resolved"]) & (frame["target_date"] == target_date)
    ].copy()
    due_mask = current_candidates.apply(
        lambda row: cutoff_is_due(
            str(row["target_date"]), str(row["timezone"]), cutoff_hour, cutoff_minute,
            as_of_utc,
        ),
        axis=1,
    ) if not current_candidates.empty else pd.Series(dtype=bool)
    current_due = current_candidates[due_mask].copy() if not current_candidates.empty else current_candidates
    fresh_mask = (
        current_due["observation_age_minutes"].between(
            0.0, DEFAULT_MAX_OBSERVATION_AGE_MINUTES, inclusive="both"
        )
        if not current_due.empty else pd.Series(dtype=bool)
    )
    current = current_due[fresh_mask].copy() if not current_due.empty else current_due
    model, features = make_model(train)
    cutoff_label = f"{cutoff_hour:02d}{cutoff_minute:02d}"
    artifact = MODEL_DIR / f"weather_exact_high_v2_{cutoff_label}.joblib"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    q80 = float(evaluation.get("abs_error_q80_c") or 1.0)
    joblib.dump({
        "model": model, "features": features, "cutoff_hour": cutoff_hour,
        "cutoff_minute": cutoff_minute,
        "trained_through": max(train["target_date"]), "research_only": True,
        "uses_sounding": False, "prior": "equal Meteoblue/ECMWF center",
        "warm_tail_buffer_c": q80,
        "point_in_time_data": True,
        "oos_events": int(evaluation.get("oos_events") or 0),
        "oos_dates": list(evaluation.get("test_dates") or []),
    }, artifact)
    predictions = []
    for _, row in current.sort_values("city").iterrows():
        central = physically_constrained_center(
            row, float(row["prior_center"] + model.predict(pd.DataFrame([row]))[0]),
        )
        cap = float(row["observed_max"])
        warm = central + q80
        heating_support = (
            finite(row["trend_120"]) is not None and float(row["trend_120"]) > 0
            and float(row["remaining_usable_solar"]) > 0.25
            and (finite(row["cloud_cover_pct"]) is None or float(row["cloud_cover_pct"]) <= 45)
            and float(row["current_below_max"]) <= 0.01
        )
        extreme_warm_disagreement = float(row["model_spread"]) >= 2.0
        if heating_support and extreme_warm_disagreement and finite(row["ecmwf"]) is not None:
            warm = max(warm, float(row["ecmwf"]))
        cap_supported = (
            float(row["current_below_max"]) > 0.01
            or (finite(row["trend_120"]) is not None and float(row["trend_120"]) <= 0)
            or float(row["remaining_usable_solar"]) <= 0.25
            or (finite(row["cloud_cover_pct"]) is not None and float(row["cloud_cover_pct"]) >= 75)
        )
        predictions.append({
            "city": row["city"], "target_date": target_date, "cutoff_hour": cutoff_hour,
            "cutoff_minute": cutoff_minute,
            "prediction_c": round(central, 2), "predicted_bucket_c": int(round_half_up(central)),
            "capping_path_c": round(cap, 2), "warm_tail_path_c": round(warm, 2),
            "plausible_buckets_c": list(range(int(round_half_up(cap)), int(round_half_up(warm)) + 1)),
            "path_status": {
                "capping": "supported_competitor" if cap_supported else "plausible",
                "primary": "reference_path",
                "warm_tail": "supported_competitor" if heating_support and extreme_warm_disagreement else "plausible",
            },
            "meteoblue_c": finite(row["mblue"]), "ecmwf_c": finite(row["ecmwf"]),
            "prior_center_c": round(float(row["prior_center"]), 2),
            "observed_max_c": round(cap, 2), "same_hour_error_c": finite(row["prior_same_hour_error"]),
            "trend_120_c_per_hour": finite(row["trend_120"]),
            "remaining_solar_fraction": round(float(row["remaining_solar_fraction"]), 3),
            "feature_as_of_utc": row["feature_as_of_utc"],
            "latest_observation_time_utc": row["latest_observation_time_utc"],
            "latest_observation_fetched_at_utc": row["latest_observation_fetched_at_utc"],
            "observation_age_minutes": round(float(row["observation_age_minutes"]), 2),
        })
    skipped = []
    for _, row in current_candidates.sort_values("city").iterrows():
        due = cutoff_is_due(
            str(row["target_date"]), str(row["timezone"]), cutoff_hour, cutoff_minute,
            as_of_utc,
        )
        age = finite(row.get("observation_age_minutes"))
        reason = None
        if not due:
            reason = "future_cutoff"
        elif age is None or age < 0 or age > DEFAULT_MAX_OBSERVATION_AGE_MINUTES:
            reason = "stale_observation"
        if reason:
            skipped.append({"city": row["city"], "reason": reason, "observation_age_minutes": age})
    status = "ok" if predictions else "not_due" if any(
        row["reason"] == "future_cutoff" for row in skipped
    ) else "no_fresh_current_rows"
    return {
        "features": features, "train_events": len(train), "predictions": predictions,
        "prediction_status": status, "skipped": skipped,
        "point_in_time_data": True, "warm_tail_buffer_c": q80,
    }, artifact


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 具体最高温 Ridge V2", "",
        f"生成时间：{report['generated_at_utc']}", "",
        "> 研究/影子模式；不使用探空和盘口价格。所有样本外测试按目标日期走步。", "",
        "## 样本外比较", "",
        "| 截点 | OOS事件 | V2 MAE | V2单档 | V2 ±1°C | 实况最高MAE | V2增量 | 双模型先验MAE | Meteoblue MAE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["cutoffs"]:
        result = item["evaluation"]
        methods = result.get("metrics") or {}
        v2, prior, mb = methods.get("ridge_v2", {}), methods.get("ensemble_prior", {}), methods.get("meteoblue", {})
        observed = methods.get("observed_max", {})
        lines.append(
            f"| {item['cutoff_hour']:02d}:{item.get('cutoff_minute', 0):02d} | {result.get('oos_events', 0)} | {v2.get('mae_c')} | "
            f"{v2.get('exact_bucket_accuracy')} | {v2.get('within_one_c')} | "
            f"{observed.get('mae_c')} | {result.get('incremental_mae_vs_observed_max_c')} | "
            f"{prior.get('mae_c')} | {mb.get('mae_c')} |"
        )
    lines.extend(["", "## 当前14:00三路径", ""])
    chosen = next((item for item in report["cutoffs"] if item.get("cutoff_label") == "14:00"), None)
    rows = ((chosen or {}).get("final_model") or {}).get("predictions") or []
    if rows:
        lines.extend([
            "| 城市 | 封顶 | V2主路径 | 暖尾 | 主档 | MB / ECMWF | 路径状态 |",
            "|---|---:|---:|---:|---:|---|---|",
        ])
        for row in rows:
            lines.append(
                f"| {row['city']} | {row['capping_path_c']:.2f}°C | {row['prediction_c']:.2f}°C | "
                f"{row['warm_tail_path_c']:.2f}°C | {row['predicted_bucket_c']}°C | "
                f"{row['meteoblue_c']} / {row['ecmwf_c']} | "
                f"{row['path_status']['capping']}, {row['path_status']['warm_tail']} |"
            )
    lines.extend([
        "", "## 约束", "",
        "- 严格日期与实际获取时间边界后，当前走步测试仍只有两个独立OOS日期；城市日不能当作完全独立样本。",
        "- 当前结果不能证明V2优于V1，也不能据此继续选择特征或调整阈值。",
        "- V2修复了按采集日期而非观测当地日期过滤METAR的问题。",
        "- 暖尾守卫只保留风险路径，不改变Ridge中心预测。",
        "- 至少积累30个独立日期后，才能比较V1与V2是否存在稳定增益。",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-date", default=datetime.now(LOCAL_TZ).date().isoformat())
    args = parser.parse_args()
    builder = DatasetBuilderV2(DB_PATH)
    generated_at = datetime.now(UTC)
    report: dict[str, Any] = {
        "generated_at_utc": generated_at.isoformat(timespec="seconds"),
        "target_date": args.target_date, "research_only": True, "uses_sounding": False,
        "validation_status": "insufficient_independent_oos_dates",
        "point_in_time_data": True,
        "cutoffs": [],
    }
    try:
        for cutoff_minutes in CUTOFF_MINUTES:
            hour, minute = divmod(cutoff_minutes, 60)
            frame = builder.build(hour, minute)
            evaluation = walk_forward(frame)
            final_model, artifact = train_current(
                frame, hour, minute, evaluation, args.target_date, generated_at
            )
            report["cutoffs"].append({
                "cutoff_hour": hour, "cutoff_minute": minute,
                "cutoff_label": f"{hour:02d}:{minute:02d}", "dataset_rows": len(frame),
                "evaluation": evaluation, "final_model": final_model, "artifact": str(artifact),
            })
    finally:
        builder.close()
    shared, shared_artifact = train_shared_artifact(
        DB_PATH, args.target_date, generated_at
    )
    report["ridge_v21_shared"] = shared
    report["ridge_v21_shared"]["artifact"] = str(shared_artifact)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / "weather_exact_high_model_v2_report.json"
    markdown_path = OUTPUT_DIR / "weather_exact_high_model_v2_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({
        "json": str(json_path), "markdown": str(markdown_path),
        "cutoffs": [{"time": item["cutoff_label"], "metrics": item["evaluation"].get("metrics")}
                    for item in report["cutoffs"]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
