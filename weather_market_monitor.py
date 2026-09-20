#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import fcntl
import gzip
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, unquote, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from metar import Metar

from weather_forecast_evaluator import WeatherForecastEvaluator
from weather_forecast_cutoff_tracker import ForecastCutoffTracker
from weather_cma_meso import CmaMesoAdapter
from weather_shadow_research import capture_shadow_sources
from weather_ablation_evaluator import evaluate as evaluate_shadow_ablation
from weather_ablation_evaluator import write_report as write_ablation_report
from weather_process_analyzer import (
    RemoteSensingCollector,
    analyze_weather_process,
    bearing_deg,
    haversine_km,
)


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "monitor_config.json"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_EVENT_URL = "https://gamma-api.polymarket.com/events/{event_id}"
GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets/{market_id}"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
AVIATION_AIRPORT_URL = "https://aviationweather.gov/api/data/airport"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
WINDY_MINIFEST_URL = "https://node.windy.com/metadata/v1.0/forecast/ecmwf-hres/minifest.json"
WINDY_METEOGRAM_URL = "https://node.windy.com/forecast/meteogram/{model}/v1.2/{lat}/{lon}"
WINDY_TIMEZONE_URL = "https://node.windy.com/services/v1/timezone/{lat}/{lon}"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
METAR_URL = "https://aviationweather.gov/api/data/metar"
USER_AGENT = "weather-market-monitor/1.0 (+local research collector)"
UTC = timezone.utc


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def split_multi_location_response(payload: Any, expected: int) -> list[dict[str, Any]]:
    """Normalize Open-Meteo's single-object and multi-location response shapes."""
    rows = payload if isinstance(payload, list) else [payload]
    if len(rows) != expected or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError(
            f"Open-Meteo returned {len(rows)} locations for {expected} requested locations"
        )
    return rows


def forecast_version_hash(model: str, target_date: str, parsed: dict[str, Any]) -> str:
    """Fingerprint only model forecast content, excluding fetch timestamps/current weather."""
    return canonical_sha256(
        {
            "model": model,
            "target_date": target_date,
            "max_c": parsed.get("max_c"),
            "peak_local": parsed.get("peak_local"),
            "points": parsed.get("points") or [],
        }
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None = None, timespec: str = "seconds") -> str:
    return (value or utc_now()).astimezone(UTC).isoformat(timespec=timespec)


def hour_slot(value: datetime | None = None) -> datetime:
    return (value or utc_now()).astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def interval_floor(value: datetime, interval_minutes: int) -> datetime:
    if interval_minutes <= 0 or 1440 % interval_minutes:
        raise ValueError("sample interval must be a positive divisor of 1440 minutes")
    current = value.astimezone(UTC)
    minute_of_day = current.hour * 60 + current.minute
    floored = minute_of_day // interval_minutes * interval_minutes
    return current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=floored)


def parse_json_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def normalize_event_date(value: Any) -> str | None:
    if not value:
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value))
    return match.group(0) if match else None


def is_highest_temperature_event(event: dict[str, Any]) -> bool:
    text = f"{event.get('title', '')} {event.get('slug', '')}"
    return bool(re.search(r"\bHighest temperature in .+ on\b", text, re.IGNORECASE))


def city_from_title(title: str) -> str:
    match = re.search(r"Highest temperature in (.+?) on\b", title, re.IGNORECASE)
    return match.group(1).strip() if match else title.strip()


def normalized_city(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def extract_station_id(resolution_source: str, rules: str) -> str | None:
    text = "\n".join(item for item in (resolution_source, rules) if item)
    urls = [resolution_source] if resolution_source else []
    urls.extend(re.findall(r"https?://[^\s<>\"]+", rules or ""))
    for raw_url in urls:
        parsed = urlparse(raw_url.rstrip(".,);]"))
        query = parse_qs(parsed.query)
        for key in ("station", "stid", "station_id", "icao", "site"):
            value = (query.get(key) or [None])[0]
            if value and re.fullmatch(r"[A-Za-z0-9]{3,8}", value):
                return value.upper()
        parts = [unquote(item) for item in parsed.path.split("/") if item]
        if parts and re.fullmatch(r"[A-Z0-9]{4,6}", parts[-1].upper()):
            return parts[-1].upper()
    targeted = re.search(r"(?:Airport|Station)[^A-Z0-9]{0,100}\b([A-Z]{4})\b", text)
    if targeted:
        return targeted.group(1)
    if re.search(r"Hong Kong Observatory", text, re.IGNORECASE):
        return "HKO"
    return None


def extract_station_name(rules: str, city: str) -> str:
    patterns = (
        r"highest temperature recorded at the (.+?) Station",
        r"highest temperature recorded at (.+?) in degrees",
        r"highest temperature recorded by NOAA at (.+?) in degrees",
        r"highest temperature recorded by the (Hong Kong Observatory) in degrees",
        r"information from .+? for the (.+?) Station",
    )
    for pattern in patterns:
        match = re.search(pattern, rules or "", re.IGNORECASE | re.DOTALL)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return f"{city} resolution station"


def parse_temperature_bucket(text: str) -> tuple[float | None, float | None, str | None]:
    normalized = (text or "").replace("−", "-").replace("–", "-").strip()
    unit_match = re.search(r"°?\s*([CF])\b", normalized, re.IGNORECASE)
    unit = unit_match.group(1).upper() if unit_match else None
    range_match = re.search(
        r"(-?\d+(?:\.\d+)?)\s*(?:-|\bto\b)\s*(-?\d+(?:\.\d+)?)",
        normalized,
        re.IGNORECASE,
    )
    numbers = [float(item) for item in re.findall(r"-?\d+(?:\.\d+)?", normalized)]
    if not numbers:
        return None, None, unit
    lower_text = normalized.lower()
    if any(token in lower_text for token in ("or below", "or lower", "at most", "≤", "below")):
        return None, numbers[0], unit
    if any(token in lower_text for token in ("or above", "or higher", "at least", "≥", "above")):
        return numbers[0], None, unit
    if range_match:
        endpoints = (float(range_match.group(1)), float(range_match.group(2)))
        return min(endpoints), max(endpoints), unit
    if len(numbers) >= 2:
        return min(numbers[0], numbers[1]), max(numbers[0], numbers[1]), unit
    return numbers[0], numbers[0], unit


def celsius_to_unit(value_c: float, unit: str | None) -> float:
    return value_c * 9.0 / 5.0 + 32.0 if unit == "F" else value_c


def unit_to_celsius(value: float, unit: str | None) -> float:
    return (value - 32.0) * 5.0 / 9.0 if unit == "F" else value


def value_in_bucket(value: float, low: float | None, high: float | None) -> bool:
    if low is not None and value < low:
        return False
    if high is not None and value > high:
        return False
    return True


def distance_to_bucket(value: float, low: float | None, high: float | None) -> float:
    if low is not None and value < low:
        return low - value
    if high is not None and value > high:
        return value - high
    return 0.0


def resolution_precision(rules: str) -> float:
    if re.search(r"one decimal place|to one decimal", rules or "", re.IGNORECASE):
        return 0.1
    return 1.0


def round_to_precision(value: float, precision: float) -> float:
    quantum = Decimal(str(precision))
    units = (Decimal(str(value)) / quantum).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(units * quantum)


def meteogram_step_hours(payload: dict[str, Any]) -> float | None:
    """Return the cadence advertised by Windy and verified from its timestamps."""
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    advertised = as_float(header.get("step"))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    hours = data.get("hours") if isinstance(data.get("hours"), list) else []
    timestamps = [as_float(value) for value in hours]
    timestamps = [value for value in timestamps if value is not None]
    observed: set[float] = set()
    for before, after in zip(timestamps, timestamps[1:]):
        delta = (after - before) / 3_600_000.0
        if delta > 0:
            observed.add(round(delta, 6))
    if advertised is not None and observed and not any(abs(value - advertised) <= 0.001 for value in observed):
        raise RuntimeError(f"Windy cadence mismatch: header={advertised:g}h timestamps={sorted(observed)}h")
    if advertised is not None:
        return advertised
    if len(observed) == 1:
        return observed.pop()
    return None


def daily_max_from_meteogram(
    payload: dict[str, Any], target_date: str, timezone_name: str
) -> tuple[float | None, str | None, list[dict[str, Any]]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    hours = data.get("hours") if isinstance(data.get("hours"), list) else []
    temps = data.get("temp-surface") if isinstance(data.get("temp-surface"), list) else []
    dewpoints = data.get("dewpoint-surface") if isinstance(data.get("dewpoint-surface"), list) else []
    humidity = data.get("rh-surface") if isinstance(data.get("rh-surface"), list) else []
    winds = data.get("wind-surface") if isinstance(data.get("wind-surface"), list) else []
    wind_dirs = data.get("windDir-surface") if isinstance(data.get("windDir-surface"), list) else []
    cloud_groups = {
        "cloud_low_pct": ("cloud-1000h", "cloud-950h", "cloud-925h", "cloud-900h", "cloud-850h"),
        "cloud_mid_pct": ("cloud-800h", "cloud-700h", "cloud-600h"),
        "cloud_high_pct": ("cloud-500h", "cloud-400h", "cloud-300h", "cloud-250h", "cloud-200h", "cloud-150h"),
    }
    try:
        local_tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return None, None, []
    points: list[dict[str, Any]] = []
    for index, (raw_ts, raw_temp) in enumerate(zip(hours, temps)):
        ts = as_float(raw_ts)
        temp_k = as_float(raw_temp)
        if ts is None or temp_k is None:
            continue
        point_utc = datetime.fromtimestamp(ts / 1000.0, tz=UTC)
        point_local = point_utc.astimezone(local_tz)
        if point_local.date().isoformat() != target_date:
            continue
        point = {
            "time_utc": point_utc.isoformat(timespec="seconds"),
            "time_local": point_local.isoformat(timespec="seconds"),
            "temp_c": round(temp_k - 273.15, 3),
        }
        dewpoint_k = as_float(dewpoints[index]) if index < len(dewpoints) else None
        point["dewpoint_c"] = round(dewpoint_k - 273.15, 3) if dewpoint_k is not None else None
        point["relative_humidity_pct"] = as_float(humidity[index]) if index < len(humidity) else None
        point["wind_speed_mps"] = as_float(winds[index]) if index < len(winds) else None
        point["wind_direction_deg"] = as_float(wind_dirs[index]) if index < len(wind_dirs) else None
        cloud_values = []
        for output_name, keys in cloud_groups.items():
            values = [
                as_float(data[key][index])
                for key in keys
                if isinstance(data.get(key), list) and index < len(data[key])
            ]
            values = [value for value in values if value is not None]
            point[output_name] = max(values) if values else None
            cloud_values.extend(values)
        point["cloud_max_pct"] = max(cloud_values) if cloud_values else None
        points.append(point)
    if not points:
        return None, None, []
    peak = max(points, key=lambda item: item["temp_c"])
    return float(peak["temp_c"]), str(peak["time_local"]), points


def open_meteo_model_forecasts(
    payload: dict[str, Any], target_date: str, timezone_name: str,
    model_names: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    hourly = payload.get("hourly") if isinstance(payload.get("hourly"), dict) else {}
    times = hourly.get("time") if isinstance(hourly.get("time"), list) else []
    try:
        local_tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return []
    output: list[dict[str, Any]] = []
    requested_models = [str(model) for model in (model_names or []) if str(model)]
    discovered = list(requested_models)
    if not discovered:
        discovered = [key.removeprefix("temperature_2m_") for key in hourly if key.startswith("temperature_2m_")]

    variables = {
        "temp_c": "temperature_2m",
        "dewpoint_c": "dew_point_2m",
        "relative_humidity_pct": "relative_humidity_2m",
        "precipitation_probability_pct": "precipitation_probability",
        "precipitation_mm": "precipitation",
        "cloud_cover_pct": "cloud_cover",
        "cloud_low_pct": "cloud_cover_low",
        "cloud_mid_pct": "cloud_cover_mid",
        "cloud_high_pct": "cloud_cover_high",
        "shortwave_radiation_wm2": "shortwave_radiation",
        "direct_radiation_wm2": "direct_radiation",
        "diffuse_radiation_wm2": "diffuse_radiation",
        "wind_speed_kmh": "wind_speed_10m",
        "wind_direction_deg": "wind_direction_10m",
        "wind_gust_kmh": "wind_gusts_10m",
    }

    for model_name in discovered:
        def series(base: str) -> list[Any]:
            generic = hourly.get(base)
            if len(discovered) == 1 and isinstance(generic, list):
                return generic
            suffixed = hourly.get(f"{base}_{model_name}")
            return suffixed if isinstance(suffixed, list) else []

        temperatures = series("temperature_2m")
        if not temperatures:
            continue
        points: list[dict[str, Any]] = []
        for index, raw_time in enumerate(times):
            value = as_float(temperatures[index]) if index < len(temperatures) else None
            if value is None or not str(raw_time).startswith(target_date):
                continue
            try:
                local_time = datetime.fromisoformat(str(raw_time)).replace(tzinfo=local_tz)
            except ValueError:
                continue
            point = {
                "time_local": local_time.isoformat(timespec="minutes"),
                "time_utc": local_time.astimezone(UTC).isoformat(timespec="seconds"),
                "temp_c": round(value, 3),
            }
            for output_name, base in variables.items():
                if output_name == "temp_c":
                    continue
                values = series(base)
                point[output_name] = as_float(values[index]) if index < len(values) else None
            points.append(point)
        if not points:
            continue
        peak = max(points, key=lambda item: item["temp_c"])
        output.append(
            {
                "model": model_name,
                "max_c": peak["temp_c"],
                "peak_local": peak["time_local"],
                "points": points,
            }
        )
    return output


def merge_shadow_temperature_forecasts(
    primary: dict[str, Any], shadow: dict[str, Any], model_names: list[str],
) -> dict[str, Any]:
    """Attach temperature-only shadow series without replacing primary fields."""
    primary_hourly = primary.get("hourly")
    shadow_hourly = shadow.get("hourly")
    if not isinstance(primary_hourly, dict) or not isinstance(shadow_hourly, dict):
        return primary
    primary_times = primary_hourly.get("time")
    shadow_times = shadow_hourly.get("time")
    if not isinstance(primary_times, list) or not isinstance(shadow_times, list):
        return primary
    for model in model_names:
        source_key = (
            "temperature_2m"
            if len(model_names) == 1
            else f"temperature_2m_{model}"
        )
        values = shadow_hourly.get(source_key)
        if not isinstance(values, list):
            continue
        by_time = {
            str(when): values[index]
            for index, when in enumerate(shadow_times) if index < len(values)
        }
        primary_hourly[f"temperature_2m_{model}"] = [
            by_time.get(str(when)) for when in primary_times
        ]
    return primary


def open_meteo_ensemble_daily_max(
    payload: dict[str, Any], target_date: str,
) -> dict[str, Any] | None:
    """Summarize each ensemble member's target-day maximum without mixing members."""
    hourly = payload.get("hourly") if isinstance(payload.get("hourly"), dict) else {}
    times = hourly.get("time") if isinstance(hourly.get("time"), list) else []
    target_indices = [index for index, value in enumerate(times) if str(value).startswith(target_date)]
    if not target_indices:
        return None
    member_keys = sorted(
        key for key, values in hourly.items()
        if (key == "temperature_2m" or key.startswith("temperature_2m_member"))
        and isinstance(values, list)
    )
    members = []
    for key in member_keys:
        values = [
            as_float(hourly[key][index])
            for index in target_indices if index < len(hourly[key])
        ]
        values = [value for value in values if value is not None]
        if values:
            members.append({
                "member": "control" if key == "temperature_2m" else key.removeprefix("temperature_2m_"),
                "maxC": round(max(values), 3),
            })
    if not members:
        return None
    maxima = sorted(float(item["maxC"]) for item in members)

    def quantile(fraction: float) -> float:
        if len(maxima) == 1:
            return maxima[0]
        position = fraction * (len(maxima) - 1)
        left = int(math.floor(position))
        right = int(math.ceil(position))
        weight = position - left
        return maxima[left] * (1.0 - weight) + maxima[right] * weight

    mean = sum(maxima) / len(maxima)
    variance = sum((value - mean) ** 2 for value in maxima) / len(maxima)
    return {
        "memberCount": len(members), "members": members,
        "meanMaxC": round(mean, 3), "stdMaxC": round(math.sqrt(variance), 3),
        "minMaxC": round(min(maxima), 3), "maxMaxC": round(max(maxima), 3),
        "q10MaxC": round(quantile(0.10), 3), "q50MaxC": round(quantile(0.50), 3),
        "q90MaxC": round(quantile(0.90), 3),
    }


def epoch_to_iso_utc(value: Any) -> str | None:
    timestamp = as_float(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def parsed_metar_fields(raw_report: Any) -> dict[str, Any]:
    """Parse decision-relevant METAR fields from the canonical raw report."""
    raw = str(raw_report or "").strip()
    if not raw:
        return {"parser_status": "missing", "parser_error": "raw METAR is empty"}
    try:
        observation = Metar.Metar(raw)
        sky = []
        for cover, height, cloud_type in observation.sky:
            sky.append(
                {
                    "cover": cover,
                    "base_ft": height.value("FT") if height is not None else None,
                    "cloud_type": cloud_type or None,
                }
            )
        weather = [
            " ".join(str(part) for part in group if part).strip()
            for group in observation.weather
        ]
        return {
            "parser_status": "ok",
            "parser_error": None,
            "temperature_c": observation.temp.value("C") if observation.temp else None,
            "dewpoint_c": observation.dewpt.value("C") if observation.dewpt else None,
            "wind_direction_deg": observation.wind_dir.value() if observation.wind_dir else None,
            "wind_speed": observation.wind_speed.value("KT") if observation.wind_speed else None,
            "wind_gust": observation.wind_gust.value("KT") if observation.wind_gust else None,
            "visibility_m": observation.vis.value("M") if observation.vis else None,
            "pressure_hpa": observation.press.value("HPA") if observation.press else None,
            "weather_code": "; ".join(value for value in weather if value) or None,
            "sky_conditions_json": json.dumps(sky, ensure_ascii=False, separators=(",", ":")),
        }
    except Exception as exc:
        return {"parser_status": "error", "parser_error": str(exc)[:500]}
class JsonClient:
    def __init__(self, timeout: float, retries: int):
        self.timeout = timeout
        self.retries = retries

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        decode_base64: bool = False,
        error_label: str | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode({k: v for k, v in params.items() if v is not None})}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            request_headers = {
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            }
            request_headers.update(headers or {})
            request = Request(
                url,
                headers=request_headers,
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    if decode_base64:
                        body = base64.b64decode(body + b"=" * ((4 - len(body) % 4) % 4), validate=True)
                    return json.loads(body.decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                retry_delay = 0.5 * (2**attempt)
                if isinstance(exc, HTTPError):
                    status = exc.code
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    exc.close()
                    if status == 404 and allow_not_found:
                        return None
                    if status == 429:
                        try:
                            retry_delay = max(retry_delay, min(float(retry_after or 0), 300.0))
                        except (TypeError, ValueError):
                            retry_delay = max(retry_delay, 30.0 * (2**attempt))
                if attempt < self.retries:
                    time.sleep(retry_delay)
        raise RuntimeError(f"GET failed: {error_label or url}: {last_error}")


class WeatherMarketMonitor:
    def __init__(self, config: dict[str, Any], event_limit_override: int | None = None):
        self.config = config
        self.fixed_city_order = [
            normalized_city(item)
            for item in config.get("fixedCities", [])
            if str(item).strip()
        ]
        self.fixed_cities = set(self.fixed_city_order)
        self.event_limit = event_limit_override or len(self.fixed_city_order) or int(config["topEventCount"])
        self.excluded_cities = {
            normalized_city(item)
            for item in config.get("excludedCities", [])
            if str(item).strip()
        }
        self.db_path = ROOT / config["databasePath"]
        self.report_dir = ROOT / config["reportDirectory"]
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.client = JsonClient(
            timeout=float(config["requestTimeoutSeconds"]),
            retries=int(config["requestRetries"]),
        )
        self.cma_meso = CmaMesoAdapter(self.config, self.client.get)
        self.windy_user_token = self._load_windy_user_token()
        self.windy_session_id = str(uuid.uuid4())
        self.db = sqlite3.connect(self.db_path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=60000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self.forecast_evaluator = WeatherForecastEvaluator(self.db, self.report_dir)
        self.forecast_cutoff_tracker = ForecastCutoffTracker(
            self.db, self.config, self.report_dir
        )
        self.remote_sensing = RemoteSensingCollector(
            self.client.get,
            timeout=float(config.get("remoteSensingTimeoutSeconds", config["requestTimeoutSeconds"])),
            retries=int(config.get("remoteSensingRetries", config["requestRetries"])),
        )

    def close(self) -> None:
        self.db.close()

    def _load_windy_user_token(self) -> str | None:
        """Load a user-exported Windy session token without ever persisting credentials."""
        token = os.environ.get("WINDY_USER_TOKEN", "").strip()
        path_text = str(self.config.get("windyAuthTokenFile", "~/.config/weather-market-monitor/windy_user_token"))
        path = Path(os.path.expanduser(path_text))
        if not token and path.exists():
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                logging.warning("Windy token file permissions must be 600 or stricter: %s", path)
            else:
                token = path.read_text(encoding="utf-8").strip()
        if not token:
            return None
        if len(token) > 4096 or any(char.isspace() for char in token):
            raise RuntimeError("Invalid Windy user token format")
        return token

    def _reload_windy_user_token(self) -> bool:
        previous = self.windy_user_token
        self.windy_user_token = self._load_windy_user_token()
        return bool(self.windy_user_token and self.windy_user_token != previous)

    def _renew_windy_user_token(self) -> bool:
        request_path = Path("~/.config/weather-market-monitor/windy_token_refresh.request").expanduser()
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.touch()
        try:
            completed = subprocess.run(
                [sys.executable, str(ROOT / "windy_token_keeper.py"), "--once"],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0 and self._reload_windy_user_token()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_utc TEXT NOT NULL UNIQUE,
                started_at_utc TEXT NOT NULL,
                completed_at_utc TEXT,
                status TEXT NOT NULL,
                selected_events INTEGER NOT NULL DEFAULT 0,
                market_snapshots INTEGER NOT NULL DEFAULT 0,
                windy_snapshots INTEGER NOT NULL DEFAULT 0,
                external_forecast_snapshots INTEGER NOT NULL DEFAULT 0,
                ensemble_forecast_snapshots INTEGER NOT NULL DEFAULT 0,
                observation_snapshots INTEGER NOT NULL DEFAULT 0,
                station_network_reports INTEGER NOT NULL DEFAULT 0,
                remote_sensing_snapshots INTEGER NOT NULL DEFAULT 0,
                weather_process_states INTEGER NOT NULL DEFAULT 0,
                resolutions_updated INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                message TEXT
            );

            CREATE TABLE IF NOT EXISTS stations (
                station_id TEXT PRIMARY KEY,
                station_name TEXT,
                city TEXT,
                country TEXT,
                latitude REAL,
                longitude REAL,
                timezone TEXT,
                resolution_source TEXT,
                coordinate_source TEXT,
                windy_url TEXT,
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                slug TEXT,
                title TEXT,
                city TEXT,
                target_date TEXT,
                station_id TEXT,
                station_name TEXT,
                resolution_source TEXT,
                rules TEXT,
                end_date_utc TEXT,
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                resolved_at_utc TEXT,
                winning_market_id TEXT,
                winning_range TEXT,
                FOREIGN KEY(station_id) REFERENCES stations(station_id)
            );

            CREATE TABLE IF NOT EXISTS event_rankings (
                run_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                rank INTEGER NOT NULL,
                volume_24h REAL,
                liquidity REAL,
                selection_score REAL,
                PRIMARY KEY(run_id, event_id),
                FOREIGN KEY(run_id) REFERENCES runs(run_id),
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            );

            CREATE TABLE IF NOT EXISTS markets (
                market_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                question TEXT,
                outcome_range TEXT,
                bucket_low REAL,
                bucket_high REAL,
                bucket_unit TEXT,
                yes_token_id TEXT,
                no_token_id TEXT,
                end_date_utc TEXT,
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES events(event_id)
            );

            CREATE TABLE IF NOT EXISTS market_snapshots (
                run_id INTEGER NOT NULL,
                slot_utc TEXT NOT NULL,
                market_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_rank INTEGER NOT NULL,
                outcome_range TEXT,
                event_volume_24h REAL,
                event_liquidity REAL,
                market_volume_24h REAL,
                market_liquidity REAL,
                gamma_yes_price REAL,
                gamma_no_price REAL,
                yes_best_bid REAL,
                yes_best_ask REAL,
                no_best_bid REAL,
                no_best_ask REAL,
                yes_bid_size REAL,
                yes_ask_size REAL,
                no_bid_size REAL,
                no_ask_size REAL,
                yes_book_json TEXT,
                no_book_json TEXT,
                fetched_at_utc TEXT NOT NULL,
                PRIMARY KEY(slot_utc, market_id),
                FOREIGN KEY(run_id) REFERENCES runs(run_id),
                FOREIGN KEY(market_id) REFERENCES markets(market_id)
            );

            CREATE TABLE IF NOT EXISTS windy_forecasts (
                run_id INTEGER NOT NULL,
                slot_utc TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                sample_local_date TEXT NOT NULL,
                sample_local_hour INTEGER NOT NULL,
                sample_local_offset TEXT,
                timezone TEXT NOT NULL,
                model TEXT,
                model_ref_time_utc TEXT,
                model_updated_at_utc TEXT,
                forecast_step_hours REAL,
                forecast_max_c REAL,
                forecast_max_f REAL,
                forecast_peak_local TEXT,
                point_count INTEGER NOT NULL DEFAULT 0,
                points_json TEXT,
                source_url TEXT,
                raw_payload_id INTEGER,
                status TEXT NOT NULL,
                error TEXT,
                fetched_at_utc TEXT NOT NULL,
                PRIMARY KEY(slot_utc, station_id, target_date),
                FOREIGN KEY(run_id) REFERENCES runs(run_id),
                FOREIGN KEY(station_id) REFERENCES stations(station_id)
            );

            CREATE TABLE IF NOT EXISTS raw_weather_payloads (
                payload_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                slot_utc TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                sha256 TEXT,
                content_encoding TEXT NOT NULL DEFAULT 'gzip+json',
                body_gzip BLOB,
                error TEXT,
                fetched_at_utc TEXT NOT NULL,
                UNIQUE(slot_utc, station_id, target_date, source),
                FOREIGN KEY(run_id) REFERENCES runs(run_id),
                FOREIGN KEY(station_id) REFERENCES stations(station_id)
            );

            CREATE TABLE IF NOT EXISTS external_forecasts (
                run_id INTEGER NOT NULL,
                slot_utc TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                sample_local_date TEXT NOT NULL,
                sample_local_time TEXT NOT NULL,
                timezone TEXT NOT NULL,
                model TEXT NOT NULL,
                forecast_max_c REAL,
                forecast_peak_local TEXT,
                point_count INTEGER NOT NULL DEFAULT 0,
                points_json TEXT,
                source TEXT NOT NULL,
                raw_payload_id INTEGER,
                status TEXT NOT NULL,
                error TEXT,
                fetched_at_utc TEXT NOT NULL,
                PRIMARY KEY(slot_utc, station_id, target_date, model),
                FOREIGN KEY(run_id) REFERENCES runs(run_id),
                FOREIGN KEY(station_id) REFERENCES stations(station_id),
                FOREIGN KEY(raw_payload_id) REFERENCES raw_weather_payloads(payload_id)
            );

            CREATE TABLE IF NOT EXISTS ensemble_forecasts (
                run_id INTEGER NOT NULL,
                slot_utc TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                timezone TEXT NOT NULL,
                source TEXT NOT NULL,
                model TEXT NOT NULL,
                version_hash TEXT NOT NULL,
                model_run_time_utc TEXT,
                model_run_time_source TEXT NOT NULL,
                model_run_confidence TEXT NOT NULL,
                member_count INTEGER NOT NULL,
                mean_max_c REAL,
                std_max_c REAL,
                min_max_c REAL,
                max_max_c REAL,
                q10_max_c REAL,
                q50_max_c REAL,
                q90_max_c REAL,
                member_maxima_json TEXT NOT NULL,
                raw_payload_id INTEGER,
                status TEXT NOT NULL,
                error TEXT,
                fetched_at_utc TEXT NOT NULL,
                PRIMARY KEY(slot_utc,station_id,target_date,model),
                FOREIGN KEY(run_id) REFERENCES runs(run_id),
                FOREIGN KEY(station_id) REFERENCES stations(station_id),
                FOREIGN KEY(raw_payload_id) REFERENCES raw_weather_payloads(payload_id)
            );

            CREATE TABLE IF NOT EXISTS forecast_model_runs (
                source TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                model TEXT NOT NULL,
                version_hash TEXT NOT NULL,
                model_run_time_utc TEXT,
                run_time_source TEXT NOT NULL,
                run_time_confidence TEXT NOT NULL,
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                raw_payload_id INTEGER,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY(source,station_id,target_date,model,version_hash)
            );

            CREATE TABLE IF NOT EXISTS source_collection_versions (
                source TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL DEFAULT '',
                product TEXT NOT NULL,
                version_hash TEXT NOT NULL,
                model_ref_time_utc TEXT,
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                raw_payload_id INTEGER,
                PRIMARY KEY(source,station_id,target_date,product,version_hash)
            );

            CREATE TABLE IF NOT EXISTS fast_metar_reports (
                station_id TEXT NOT NULL,
                observation_time_utc TEXT NOT NULL,
                report_time_utc TEXT,
                receipt_time_utc TEXT,
                metar_type TEXT,
                raw_metar TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                first_fetched_at_utc TEXT NOT NULL,
                last_fetched_at_utc TEXT NOT NULL,
                PRIMARY KEY(station_id,observation_time_utc,raw_metar)
            );

            CREATE TABLE IF NOT EXISTS source_access_probes (
                source TEXT NOT NULL,
                product TEXT NOT NULL,
                checked_at_utc TEXT NOT NULL,
                status TEXT NOT NULL,
                endpoint TEXT,
                latency_ms REAL,
                detail TEXT,
                PRIMARY KEY(source,product,checked_at_utc)
            );

            CREATE TABLE IF NOT EXISTS source_poll_state (
                source TEXT PRIMARY KEY,
                last_attempt_utc TEXT,
                next_attempt_utc TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_status TEXT,
                last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS shadow_source_snapshots (
                slot_utc TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                source_time_utc TEXT,
                source_age_seconds REAL,
                version_hash TEXT,
                details_json TEXT NOT NULL,
                captured_at_utc TEXT NOT NULL,
                PRIMARY KEY(slot_utc,station_id,target_date,source)
            );

            CREATE TABLE IF NOT EXISTS weather_observations (
                run_id INTEGER NOT NULL,
                slot_utc TEXT NOT NULL,
                station_id TEXT NOT NULL,
                sample_local_date TEXT NOT NULL,
                sample_local_time TEXT NOT NULL,
                timezone TEXT NOT NULL,
                source TEXT NOT NULL,
                observation_time_utc TEXT,
                temperature_c REAL,
                dewpoint_c REAL,
                relative_humidity REAL,
                precipitation_mm REAL,
                cloud_cover_pct REAL,
                wind_direction_deg REAL,
                wind_speed REAL,
                wind_speed_unit TEXT,
                wind_gust REAL,
                visibility_m REAL,
                pressure_hpa REAL,
                solar_radiation_wm2 REAL,
                direct_radiation_wm2 REAL,
                diffuse_radiation_wm2 REAL,
                flight_category TEXT,
                sky_conditions_json TEXT,
                raw_metar TEXT,
                metar_type TEXT,
                metar_parser_status TEXT,
                metar_parser_error TEXT,
                weather_code TEXT,
                observed_daily_max_c REAL,
                raw_payload_id INTEGER,
                status TEXT NOT NULL,
                error TEXT,
                fetched_at_utc TEXT NOT NULL,
                PRIMARY KEY(slot_utc, station_id, source),
                FOREIGN KEY(run_id) REFERENCES runs(run_id),
                FOREIGN KEY(station_id) REFERENCES stations(station_id),
                FOREIGN KEY(raw_payload_id) REFERENCES raw_weather_payloads(payload_id)
            );

            CREATE TABLE IF NOT EXISTS station_network_reports (
                primary_station_id TEXT NOT NULL,
                station_id TEXT NOT NULL,
                station_name TEXT,
                observation_time_utc TEXT NOT NULL,
                report_time_utc TEXT,
                receipt_time_utc TEXT,
                metar_type TEXT,
                latitude REAL,
                longitude REAL,
                elevation_m REAL,
                distance_km REAL,
                bearing_from_primary_deg REAL,
                temperature_c REAL,
                dewpoint_c REAL,
                wind_direction_deg REAL,
                wind_speed_kt REAL,
                wind_gust_kt REAL,
                pressure_hpa REAL,
                visibility_m REAL,
                flight_category TEXT,
                cover TEXT,
                clouds_json TEXT,
                weather_code TEXT,
                raw_metar TEXT,
                raw_payload_id INTEGER,
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                PRIMARY KEY(primary_station_id,station_id,observation_time_utc),
                FOREIGN KEY(primary_station_id) REFERENCES stations(station_id),
                FOREIGN KEY(raw_payload_id) REFERENCES raw_weather_payloads(payload_id)
            );

            CREATE TABLE IF NOT EXISTS remote_sensing_snapshots (
                run_id INTEGER NOT NULL,
                slot_utc TEXT NOT NULL,
                station_id TEXT NOT NULL,
                source TEXT NOT NULL,
                frame_time_utc TEXT,
                status TEXT NOT NULL,
                quality TEXT NOT NULL,
                features_json TEXT,
                source_url TEXT,
                raw_sha256 TEXT,
                error TEXT,
                fetched_at_utc TEXT NOT NULL,
                PRIMARY KEY(slot_utc,station_id,source),
                FOREIGN KEY(run_id) REFERENCES runs(run_id),
                FOREIGN KEY(station_id) REFERENCES stations(station_id)
            );

            CREATE TABLE IF NOT EXISTS weather_process_states (
                run_id INTEGER NOT NULL,
                slot_utc TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                primary_observation_time_utc TEXT,
                status TEXT NOT NULL,
                detected_processes_json TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                PRIMARY KEY(slot_utc,station_id,target_date),
                FOREIGN KEY(run_id) REFERENCES runs(run_id),
                FOREIGN KEY(station_id) REFERENCES stations(station_id)
            );

            CREATE TABLE IF NOT EXISTS market_resolutions (
                market_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                checked_at_utc TEXT NOT NULL,
                resolved_at_utc TEXT,
                is_closed INTEGER NOT NULL DEFAULT 0,
                is_resolved INTEGER NOT NULL DEFAULT 0,
                winning_outcome TEXT,
                yes_final_price REAL,
                no_final_price REAL,
                payload_json TEXT,
                FOREIGN KEY(market_id) REFERENCES markets(market_id)
            );

            CREATE TABLE IF NOT EXISTS weather_resolution_labels (
                event_id TEXT PRIMARY KEY,
                city TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                resolved_at_utc TEXT NOT NULL,
                winning_market_id TEXT,
                winning_range TEXT NOT NULL,
                official_temperature_c REAL,
                resolution_precision_c REAL NOT NULL,
                exact_at_resolution_precision INTEGER NOT NULL,
                label_status TEXT NOT NULL,
                label_source TEXT NOT NULL,
                resolution_source_url TEXT,
                station_observed_max_c REAL,
                station_observed_max_rounded_c REAL,
                station_audit_delta_c REAL,
                station_observation_count INTEGER NOT NULL DEFAULT 0,
                source_payload_json TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES events(event_id),
                FOREIGN KEY(station_id) REFERENCES stations(station_id)
            );

            CREATE TABLE IF NOT EXISTS collection_errors (
                error_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                stage TEXT NOT NULL,
                object_id TEXT,
                message TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_events_station_date ON events(station_id, target_date);
            CREATE INDEX IF NOT EXISTS idx_markets_event ON markets(event_id);
            CREATE INDEX IF NOT EXISTS idx_snapshots_event_slot ON market_snapshots(event_id, slot_utc);
            CREATE INDEX IF NOT EXISTS idx_windy_station_date ON windy_forecasts(station_id, target_date, slot_utc);
            CREATE INDEX IF NOT EXISTS idx_external_station_date ON external_forecasts(station_id, target_date, slot_utc);
            CREATE INDEX IF NOT EXISTS idx_ensemble_station_date ON ensemble_forecasts(station_id,target_date,slot_utc);
            CREATE INDEX IF NOT EXISTS idx_model_runs_lookup ON forecast_model_runs(source,model,target_date,last_seen_utc);
            CREATE INDEX IF NOT EXISTS idx_source_versions_latest ON source_collection_versions(source,station_id,target_date,product,last_seen_utc);
            CREATE INDEX IF NOT EXISTS idx_fast_metar_latest ON fast_metar_reports(station_id,observation_time_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_shadow_source_date ON shadow_source_snapshots(target_date,station_id,source,slot_utc);
            CREATE INDEX IF NOT EXISTS idx_observation_station_date ON weather_observations(station_id, sample_local_date, slot_utc);
            CREATE INDEX IF NOT EXISTS idx_station_network_primary_time ON station_network_reports(primary_station_id,observation_time_utc);
            CREATE INDEX IF NOT EXISTS idx_remote_sensing_station_time ON remote_sensing_snapshots(station_id,slot_utc);
            CREATE INDEX IF NOT EXISTS idx_weather_process_station_time ON weather_process_states(station_id,target_date,slot_utc);
            CREATE INDEX IF NOT EXISTS idx_raw_weather_slot ON raw_weather_payloads(slot_utc, source);
            CREATE INDEX IF NOT EXISTS idx_resolution_status ON market_resolutions(is_resolved, checked_at_utc);
            CREATE INDEX IF NOT EXISTS idx_resolution_labels_date ON weather_resolution_labels(target_date,city);
            """
        )
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(windy_forecasts)")}
        if "forecast_step_hours" not in columns:
            self.db.execute("ALTER TABLE windy_forecasts ADD COLUMN forecast_step_hours REAL")
        if "raw_payload_id" not in columns:
            self.db.execute("ALTER TABLE windy_forecasts ADD COLUMN raw_payload_id INTEGER")
        run_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(runs)")}
        if "external_forecast_snapshots" not in run_columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN external_forecast_snapshots INTEGER NOT NULL DEFAULT 0")
        if "ensemble_forecast_snapshots" not in run_columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN ensemble_forecast_snapshots INTEGER NOT NULL DEFAULT 0")
        if "observation_snapshots" not in run_columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN observation_snapshots INTEGER NOT NULL DEFAULT 0")
        if "station_network_reports" not in run_columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN station_network_reports INTEGER NOT NULL DEFAULT 0")
        if "remote_sensing_snapshots" not in run_columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN remote_sensing_snapshots INTEGER NOT NULL DEFAULT 0")
        if "weather_process_states" not in run_columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN weather_process_states INTEGER NOT NULL DEFAULT 0")
        observation_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(weather_observations)")}
        for column, definition in (
            ("wind_gust", "REAL"),
            ("visibility_m", "REAL"),
            ("pressure_hpa", "REAL"),
            ("solar_radiation_wm2", "REAL"),
            ("direct_radiation_wm2", "REAL"),
            ("diffuse_radiation_wm2", "REAL"),
            ("flight_category", "TEXT"),
            ("sky_conditions_json", "TEXT"),
            ("raw_metar", "TEXT"),
            ("metar_type", "TEXT"),
            ("metar_parser_status", "TEXT"),
            ("metar_parser_error", "TEXT"),
        ):
            if column not in observation_columns:
                self.db.execute(f"ALTER TABLE weather_observations ADD COLUMN {column} {definition}")
        self.db.execute(
            """
            INSERT OR IGNORE INTO forecast_model_runs(
                source,station_id,target_date,model,version_hash,model_run_time_utc,
                run_time_source,run_time_confidence,first_seen_utc,last_seen_utc,
                raw_payload_id,metadata_json
            )
            SELECT source,station_id,target_date,product,version_hash,NULL,
                   CASE WHEN model_ref_time_utc IS NULL
                        THEN 'provider_not_exposed' ELSE 'legacy_provider_time_unverified' END,
                   CASE WHEN model_ref_time_utc IS NULL THEN 'unavailable' ELSE 'low' END,
                   first_seen_utc,last_seen_utc,raw_payload_id,
                   '{"backfilledFrom":"source_collection_versions","claimedRunTimeNotTrusted":true}'
            FROM source_collection_versions
            """
        )
        self.db.commit()

    def _record_error(self, run_id: int | None, stage: str, object_id: str | None, exc: Any) -> None:
        message = str(exc)[:2000]
        logging.warning("%s %s: %s", stage, object_id or "", message)
        self.db.execute(
            "INSERT INTO collection_errors(run_id, stage, object_id, message, created_at_utc) VALUES(?,?,?,?,?)",
            (run_id, stage, object_id, message, iso_utc()),
        )

    def _store_raw_weather_payload(
        self,
        run_id: int,
        slot: datetime,
        station_id: str,
        target_date: str | None,
        source: str,
        payload: Any,
        status: str = "ok",
        error: str | None = None,
    ) -> int:
        raw = None
        digest = None
        if payload is not None:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            raw = gzip.compress(encoded, compresslevel=6)
        self.db.execute(
            """
            INSERT INTO raw_weather_payloads(
                run_id,slot_utc,station_id,target_date,source,status,sha256,
                content_encoding,body_gzip,error,fetched_at_utc
            ) VALUES(?,?,?,?,?,?,?,'gzip+json',?,?,?)
            ON CONFLICT(slot_utc,station_id,target_date,source) DO UPDATE SET
                run_id=excluded.run_id,status=excluded.status,sha256=excluded.sha256,
                body_gzip=excluded.body_gzip,error=excluded.error,fetched_at_utc=excluded.fetched_at_utc
            """,
            (
                run_id, iso_utc(slot), station_id, target_date or "", source, status,
                digest, raw, error, iso_utc(),
            ),
        )
        row = self.db.execute(
            """
            SELECT payload_id FROM raw_weather_payloads
            WHERE slot_utc=? AND station_id=? AND target_date=? AND source=?
            """,
            (iso_utc(slot), station_id, target_date or "", source),
        ).fetchone()
        return int(row["payload_id"])

    def _begin_run(self, slot: datetime, force: bool) -> tuple[int, bool]:
        slot_text = iso_utc(slot)
        row = self.db.execute("SELECT run_id, status FROM runs WHERE slot_utc=?", (slot_text,)).fetchone()
        if row:
            if force:
                self.db.execute("DELETE FROM event_rankings WHERE run_id=?", (row["run_id"],))
                self.db.execute("DELETE FROM market_snapshots WHERE run_id=?", (row["run_id"],))
                self.db.execute("DELETE FROM external_forecasts WHERE run_id=?", (row["run_id"],))
                self.db.execute("DELETE FROM ensemble_forecasts WHERE run_id=?", (row["run_id"],))
                self.db.execute("DELETE FROM weather_observations WHERE run_id=?", (row["run_id"],))
                self.db.execute("DELETE FROM remote_sensing_snapshots WHERE run_id=?", (row["run_id"],))
                self.db.execute("DELETE FROM weather_process_states WHERE run_id=?", (row["run_id"],))
                self.db.execute("DELETE FROM windy_forecasts WHERE run_id=?", (row["run_id"],))
                self.db.execute(
                    "DELETE FROM station_network_reports WHERE raw_payload_id IN "
                    "(SELECT payload_id FROM raw_weather_payloads WHERE run_id=?)",
                    (row["run_id"],),
                )
                self.db.execute("DELETE FROM raw_weather_payloads WHERE run_id=?", (row["run_id"],))
                self.db.execute("DELETE FROM collection_errors WHERE run_id=?", (row["run_id"],))
            self.db.execute(
                "UPDATE runs SET started_at_utc=?, status='running', message=NULL WHERE run_id=?",
                (iso_utc(), row["run_id"]),
            )
            self.db.commit()
            return int(row["run_id"]), bool(row["status"] == "completed" and not force)
        cursor = self.db.execute(
            "INSERT INTO runs(slot_utc, started_at_utc, status) VALUES(?,?,'running')",
            (slot_text, iso_utc()),
        )
        self.db.commit()
        return int(cursor.lastrowid), False

    def discover_top_events(self) -> list[dict[str, Any]]:
        endpoint_params = {
            "limit": int(self.config["eventApiLimit"]),
            "active": "true",
            "closed": "false",
            "tag_slug": self.config["weatherTag"],
            "order": "volume24hr",
            "ascending": "false",
        }
        payload = self.client.get(GAMMA_EVENTS_URL, endpoint_params)
        events = payload if isinstance(payload, list) else []
        today = utc_now().date()
        horizon = today + timedelta(days=int(self.config["marketDateHorizonDays"]))
        candidates: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict) or not is_highest_temperature_event(event):
                continue
            city = city_from_title(str(event.get("title") or ""))
            city_key = normalized_city(city)
            if city_key in self.excluded_cities:
                continue
            if self.fixed_cities and city_key not in self.fixed_cities:
                continue
            target_text = normalize_event_date(event.get("eventDate") or event.get("endDate"))
            if not target_text:
                continue
            target = date.fromisoformat(target_text)
            if target < today or target > horizon:
                continue
            volume = max(as_float(event.get("volume24hr")) or 0.0, 0.0)
            liquidity = max(as_float(event.get("liquidity")) or 0.0, 0.0)
            event["_target_date"] = target_text
            event["_volume_24h"] = volume
            event["_liquidity"] = liquidity
            event["_selection_score"] = math.sqrt(max(volume, 1.0) * max(liquidity, 1.0))
            candidates.append(event)
        if self.fixed_city_order:
            candidates.sort(
                key=lambda item: (
                    item["_target_date"],
                    -(item["_selection_score"]),
                )
            )
            by_city: dict[str, dict[str, Any]] = {}
            for event in candidates:
                city_key = normalized_city(city_from_title(str(event.get("title") or "")))
                by_city.setdefault(city_key, event)
            selected = [by_city[city] for city in self.fixed_city_order if city in by_city]
            selected = selected[: self.event_limit]
        else:
            candidates.sort(key=lambda item: item["_selection_score"], reverse=True)
            selected = candidates[: self.event_limit]
        for rank, event in enumerate(selected, start=1):
            event["_current_rank"] = rank
        return selected

    def _tracked_event_rows(self, now: datetime) -> list[sqlite3.Row]:
        rows = self.db.execute(
            """
            SELECT e.event_id, e.target_date, e.city, s.timezone
            FROM events e LEFT JOIN stations s ON s.station_id=e.station_id
            WHERE e.target_date IS NOT NULL
            """
        ).fetchall()
        selected: list[sqlite3.Row] = []
        for row in rows:
            try:
                local_tz = ZoneInfo(row["timezone"]) if row["timezone"] else UTC
                target = date.fromisoformat(row["target_date"])
            except (ValueError, ZoneInfoNotFoundError):
                continue
            if normalized_city(row["city"]) in self.excluded_cities:
                continue
            if self.fixed_cities and normalized_city(row["city"]) not in self.fixed_cities:
                continue
            if now.astimezone(local_tz).date() <= target:
                selected.append(row)
        return selected

    def include_tracked_events(
        self, run_id: int, current_top: list[dict[str, Any]], now: datetime
    ) -> list[dict[str, Any]]:
        current_ids = {str(event.get("id") or "") for event in current_top}
        rows = [row for row in self._tracked_event_rows(now) if row["event_id"] not in current_ids]
        if not rows:
            return current_top
        fetched: list[dict[str, Any]] = []
        workers = min(int(self.config["maxConcurrentRequests"]), len(rows))
        with ThreadPoolExecutor(max_workers=max(workers, 1)) as executor:
            futures = {
                executor.submit(
                    self.client.get, GAMMA_EVENT_URL.format(event_id=row["event_id"])
                ): row
                for row in rows
            }
            for future in as_completed(futures):
                row = futures[future]
                try:
                    event = future.result()
                    if not isinstance(event, dict):
                        raise RuntimeError("Gamma event response is not an object")
                    if normalized_city(city_from_title(str(event.get("title") or ""))) in self.excluded_cities:
                        continue
                    volume = max(as_float(event.get("volume24hr")) or 0.0, 0.0)
                    liquidity = max(as_float(event.get("liquidity")) or 0.0, 0.0)
                    event["_target_date"] = row["target_date"]
                    event["_volume_24h"] = volume
                    event["_liquidity"] = liquidity
                    event["_selection_score"] = math.sqrt(max(volume, 1.0) * max(liquidity, 1.0))
                    event["_current_rank"] = 0
                    fetched.append(event)
                except Exception as exc:
                    self._record_error(run_id, "tracked_event", row["event_id"], exc)
        return current_top + fetched

    def _cached_station(self, station_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM stations WHERE station_id=?", (station_id,)).fetchone()

    def _airport_coordinates(self, station_id: str) -> tuple[float, float, str | None, str | None] | None:
        payload = self.client.get(AVIATION_AIRPORT_URL, {"ids": station_id, "format": "json"})
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        lat, lon = as_float(row.get("lat")), as_float(row.get("lon"))
        if lat is None or lon is None:
            return None
        return lat, lon, row.get("country"), row.get("name")

    def _nominatim_coordinates(self, query: str) -> tuple[float, float, str | None, str | None] | None:
        payload = self.client.get(NOMINATIM_URL, {"format": "jsonv2", "limit": 1, "q": query})
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        lat, lon = as_float(row.get("lat")), as_float(row.get("lon"))
        if lat is None or lon is None:
            return None
        return lat, lon, None, row.get("display_name")

    def _windy_timezone(self, lat: float, lon: float) -> str:
        payload = self.client.get(
            WINDY_TIMEZONE_URL.format(lat=round(lat, 5), lon=round(lon, 5)),
            {"ts": iso_utc()},
        )
        name = payload.get("TZname") if isinstance(payload, dict) else None
        if not name:
            raise RuntimeError("Windy timezone response has no TZname")
        ZoneInfo(str(name))
        return str(name)

    def ensure_station(
        self,
        run_id: int,
        event_id: str,
        city: str,
        resolution_source: str,
        rules: str,
    ) -> dict[str, Any]:
        parsed_id = extract_station_id(resolution_source, rules)
        station_id = parsed_id or f"EVENT-{event_id}"
        station_name = extract_station_name(rules, city)
        cached = self._cached_station(station_id)
        if cached and cached["latitude"] is not None and cached["timezone"]:
            self.db.execute(
                "UPDATE stations SET last_seen_utc=?, resolution_source=?, city=? WHERE station_id=?",
                (iso_utc(), resolution_source, city, station_id),
            )
            return dict(cached)

        coordinates = None
        coordinate_source = None
        overrides = self.config.get("stationOverrides", {})
        override = overrides.get(station_id) if isinstance(overrides, dict) else None
        if isinstance(override, dict):
            lat, lon = as_float(override.get("latitude")), as_float(override.get("longitude"))
            if lat is not None and lon is not None:
                coordinates = (lat, lon, override.get("country"), override.get("stationName"))
                coordinate_source = "monitor_config.json"
        if coordinates is None and parsed_id:
            try:
                coordinates = self._airport_coordinates(parsed_id)
                coordinate_source = "aviationweather.gov"
            except Exception as exc:
                self._record_error(run_id, "station_airport", station_id, exc)
        if coordinates is None:
            query = " ".join(item for item in (station_name, city, parsed_id) if item)
            try:
                coordinates = self._nominatim_coordinates(query)
                coordinate_source = "nominatim.openstreetmap.org"
            except Exception as exc:
                self._record_error(run_id, "station_nominatim", station_id, exc)

        lat = lon = None
        country = None
        resolved_name = station_name
        timezone_name = None
        if coordinates:
            lat, lon, country, source_name = coordinates
            resolved_name = source_name or station_name
            try:
                timezone_name = self._windy_timezone(lat, lon)
            except Exception as exc:
                self._record_error(run_id, "station_timezone", station_id, exc)
        windy_model = str(self.config.get("windyModel", "ecmwf"))
        windy_url = f"https://www.windy.com/{lat:.4f}/{lon:.4f}/{windy_model}/meteogram" if lat is not None and lon is not None else None
        now = iso_utc()
        self.db.execute(
            """
            INSERT INTO stations(
                station_id, station_name, city, country, latitude, longitude, timezone,
                resolution_source, coordinate_source, windy_url, first_seen_utc, last_seen_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(station_id) DO UPDATE SET
                station_name=excluded.station_name, city=excluded.city, country=COALESCE(excluded.country, stations.country),
                latitude=COALESCE(excluded.latitude, stations.latitude), longitude=COALESCE(excluded.longitude, stations.longitude),
                timezone=COALESCE(excluded.timezone, stations.timezone), resolution_source=excluded.resolution_source,
                coordinate_source=COALESCE(excluded.coordinate_source, stations.coordinate_source),
                windy_url=COALESCE(excluded.windy_url, stations.windy_url), last_seen_utc=excluded.last_seen_utc
            """,
            (
                station_id,
                resolved_name,
                city,
                country,
                lat,
                lon,
                timezone_name,
                resolution_source,
                coordinate_source,
                windy_url,
                now,
                now,
            ),
        )
        return dict(self._cached_station(station_id) or {"station_id": station_id})

    def upsert_event(self, run_id: int, event: dict[str, Any], rank: int | None) -> dict[str, Any]:
        markets = [item for item in event.get("markets", []) if isinstance(item, dict)]
        representative = markets[0] if markets else {}
        title = str(event.get("title") or "")
        city = city_from_title(title)
        rules = str(representative.get("description") or event.get("description") or "")
        resolution_source = str(
            representative.get("resolutionSource") or event.get("resolutionSource") or ""
        )
        event_id = str(event.get("id") or "")
        station = self.ensure_station(run_id, event_id, city, resolution_source, rules)
        now = iso_utc()
        self.db.execute(
            """
            INSERT INTO events(
                event_id, slug, title, city, target_date, station_id, station_name,
                resolution_source, rules, end_date_utc, first_seen_utc, last_seen_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET
                slug=excluded.slug, title=excluded.title, city=excluded.city, target_date=excluded.target_date,
                station_id=excluded.station_id, station_name=excluded.station_name,
                resolution_source=excluded.resolution_source, rules=excluded.rules,
                end_date_utc=excluded.end_date_utc, last_seen_utc=excluded.last_seen_utc
            """,
            (
                event_id,
                event.get("slug"),
                title,
                city,
                event["_target_date"],
                station.get("station_id"),
                station.get("station_name") or extract_station_name(rules, city),
                resolution_source,
                rules,
                event.get("endDate"),
                now,
                now,
            ),
        )
        if rank:
            self.db.execute(
                """
                INSERT INTO event_rankings(run_id, event_id, rank, volume_24h, liquidity, selection_score)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(run_id, event_id) DO UPDATE SET
                    rank=excluded.rank, volume_24h=excluded.volume_24h,
                    liquidity=excluded.liquidity, selection_score=excluded.selection_score
                """,
                (
                    run_id,
                    event_id,
                    rank,
                    event["_volume_24h"],
                    event["_liquidity"],
                    event["_selection_score"],
                ),
            )
        return station

    def upsert_market(self, event_id: str, market: dict[str, Any]) -> dict[str, Any]:
        outcomes = [str(item) for item in parse_json_array(market.get("outcomes"))]
        tokens = [str(item) for item in parse_json_array(market.get("clobTokenIds"))]
        token_by_outcome = {name.lower(): tokens[index] for index, name in enumerate(outcomes) if index < len(tokens)}
        yes_token = token_by_outcome.get("yes") or (tokens[0] if tokens else None)
        no_token = token_by_outcome.get("no") or (tokens[1] if len(tokens) > 1 else None)
        outcome_range = str(market.get("groupItemTitle") or market.get("question") or "")
        low, high, unit = parse_temperature_bucket(outcome_range)
        market_id = str(market.get("id") or "")
        now = iso_utc()
        self.db.execute(
            """
            INSERT INTO markets(
                market_id, event_id, question, outcome_range, bucket_low, bucket_high,
                bucket_unit, yes_token_id, no_token_id, end_date_utc, first_seen_utc, last_seen_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(market_id) DO UPDATE SET
                question=excluded.question, outcome_range=excluded.outcome_range,
                bucket_low=excluded.bucket_low, bucket_high=excluded.bucket_high,
                bucket_unit=excluded.bucket_unit, yes_token_id=excluded.yes_token_id,
                no_token_id=excluded.no_token_id, end_date_utc=excluded.end_date_utc,
                last_seen_utc=excluded.last_seen_utc
            """,
            (
                market_id,
                event_id,
                market.get("question"),
                outcome_range,
                low,
                high,
                unit,
                yes_token,
                no_token,
                market.get("endDate"),
                now,
                now,
            ),
        )
        return {
            "market_id": market_id,
            "event_id": event_id,
            "outcome_range": outcome_range,
            "yes_token": yes_token,
            "no_token": no_token,
            "market": market,
        }

    def _fetch_book(self, token_id: str | None) -> dict[str, Any] | None:
        if not token_id:
            return None
        payload = self.client.get(
            CLOB_BOOK_URL, {"token_id": token_id}, allow_not_found=True
        )
        if not isinstance(payload, dict):
            return None
        bids = sorted(
            (
                {"price": as_float(item.get("price")), "size": as_float(item.get("size"))}
                for item in payload.get("bids", [])
                if isinstance(item, dict)
            ),
            key=lambda item: item["price"] if item["price"] is not None else -1,
            reverse=True,
        )[:5]
        asks = sorted(
            (
                {"price": as_float(item.get("price")), "size": as_float(item.get("size"))}
                for item in payload.get("asks", [])
                if isinstance(item, dict)
            ),
            key=lambda item: item["price"] if item["price"] is not None else 2,
        )[:5]
        return {"bids": bids, "asks": asks}

    def _market_books(self, item: dict[str, Any]) -> dict[str, Any]:
        result = dict(item)
        result["yes_book"] = self._fetch_book(item["yes_token"])
        result["no_book"] = self._fetch_book(item["no_token"])
        return result

    @staticmethod
    def _best(book: dict[str, Any] | None, side: str) -> tuple[float | None, float | None]:
        rows = book.get(side, []) if isinstance(book, dict) else []
        if not rows:
            return None, None
        return rows[0].get("price"), rows[0].get("size")

    def capture_market_structure(
        self,
        run_id: int,
        slot: datetime,
        ranked_events: list[dict[str, Any]],
    ) -> int:
        tasks: list[dict[str, Any]] = []
        for fallback_rank, event in enumerate(ranked_events, start=1):
            rank = int(event.get("_current_rank", fallback_rank))
            event_id = str(event.get("id") or "")
            self.upsert_event(run_id, event, rank or None)
            for market in event.get("markets", []):
                if not isinstance(market, dict):
                    continue
                item = self.upsert_market(event_id, market)
                item.update(
                    {
                        "rank": rank,
                        "event_volume_24h": event["_volume_24h"],
                        "event_liquidity": event["_liquidity"],
                    }
                )
                tasks.append(item)
        self.db.commit()

        completed: list[dict[str, Any]] = []
        workers = int(self.config["maxConcurrentRequests"])
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self._market_books, item): item for item in tasks}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    completed.append(future.result())
                except Exception as exc:
                    self._record_error(run_id, "orderbook", item["market_id"], exc)
                    item["yes_book"] = None
                    item["no_book"] = None
                    completed.append(item)

        slot_text = iso_utc(slot)
        fetched_at = iso_utc()
        for item in completed:
            market = item["market"]
            prices = [as_float(value) for value in parse_json_array(market.get("outcomePrices"))]
            yes_price = prices[0] if prices else None
            no_price = prices[1] if len(prices) > 1 else None
            yes_bid, yes_bid_size = self._best(item["yes_book"], "bids")
            yes_ask, yes_ask_size = self._best(item["yes_book"], "asks")
            no_bid, no_bid_size = self._best(item["no_book"], "bids")
            no_ask, no_ask_size = self._best(item["no_book"], "asks")
            self.db.execute(
                """
                INSERT INTO market_snapshots(
                    run_id, slot_utc, market_id, event_id, event_rank, outcome_range,
                    event_volume_24h, event_liquidity, market_volume_24h, market_liquidity,
                    gamma_yes_price, gamma_no_price, yes_best_bid, yes_best_ask,
                    no_best_bid, no_best_ask, yes_bid_size, yes_ask_size, no_bid_size,
                    no_ask_size, yes_book_json, no_book_json, fetched_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(slot_utc, market_id) DO UPDATE SET
                    run_id=excluded.run_id, event_rank=excluded.event_rank,
                    event_volume_24h=excluded.event_volume_24h, event_liquidity=excluded.event_liquidity,
                    market_volume_24h=excluded.market_volume_24h, market_liquidity=excluded.market_liquidity,
                    gamma_yes_price=excluded.gamma_yes_price, gamma_no_price=excluded.gamma_no_price,
                    yes_best_bid=excluded.yes_best_bid, yes_best_ask=excluded.yes_best_ask,
                    no_best_bid=excluded.no_best_bid, no_best_ask=excluded.no_best_ask,
                    yes_bid_size=excluded.yes_bid_size, yes_ask_size=excluded.yes_ask_size,
                    no_bid_size=excluded.no_bid_size, no_ask_size=excluded.no_ask_size,
                    yes_book_json=excluded.yes_book_json, no_book_json=excluded.no_book_json,
                    fetched_at_utc=excluded.fetched_at_utc
                """,
                (
                    run_id,
                    slot_text,
                    item["market_id"],
                    item["event_id"],
                    item["rank"],
                    item["outcome_range"],
                    item["event_volume_24h"],
                    item["event_liquidity"],
                    as_float(market.get("volume24hr")),
                    as_float(market.get("liquidity")),
                    yes_price,
                    no_price,
                    yes_bid,
                    yes_ask,
                    no_bid,
                    no_ask,
                    yes_bid_size,
                    yes_ask_size,
                    no_bid_size,
                    no_ask_size,
                    json.dumps(item["yes_book"], ensure_ascii=False, separators=(",", ":")),
                    json.dumps(item["no_book"], ensure_ascii=False, separators=(",", ":")),
                    fetched_at,
                ),
            )
        self.db.commit()
        return len(completed)

    def _windy_reference(self) -> dict[str, Any]:
        if str(self.config.get("windyModel", "ecmwf")).casefold() == "mblue":
            return {
                "ref": iso_utc(utc_now(), timespec="seconds"),
                "update": iso_utc(utc_now(), timespec="seconds"),
                "model": "mblue",
            }
        params: dict[str, Any] = {"v": "50.1.2"}
        if int(self.config["windyStepHours"]) == 1:
            params["premium"] = "true"
        payload = self.client.get(WINDY_MINIFEST_URL, params)
        if not isinstance(payload, dict) or not payload.get("ref"):
            raise RuntimeError("Windy minifest missing ref")
        return payload

    @staticmethod
    def _windy_encoded_forecast_url(
        source_url: str,
        params: dict[str, Any],
        token: str,
        session_id: str,
    ) -> str:
        """Build the authenticated forecast URL used by Windy's web client."""
        parsed = urlparse(source_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 5 or parts[0] != "forecast":
            raise RuntimeError("Unexpected Windy forecast URL")
        model, version = parts[1], parts[2]
        query = dict(params)
        query.update(
            {
                "token2": token,
                "uid": session_id,
                "sc": 1,
                "pr": 1,
                "v": "50.1.2",
                "poc": 1,
            }
        )
        inner = f"{model}/{version}/{'/'.join(parts[3:])}?{urlencode(query)}"

        def encode(value: str) -> str:
            return base64.b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")

        origin = f"{parsed.scheme}://{parsed.netloc}"
        return f"{origin}/Zm9yZWNhc3Q/{encode(version)}/{encode(inner)}"

    def _tracked_station_dates(
        self, now: datetime, allowed_cities: set[str] | None = None
    ) -> list[sqlite3.Row]:
        rows = self.db.execute(
            """
            SELECT DISTINCT e.station_id, e.target_date, s.station_name, s.city,
                   s.latitude, s.longitude, s.timezone, s.windy_url
            FROM events e JOIN stations s ON s.station_id=e.station_id
            WHERE e.target_date IS NOT NULL
              AND s.latitude IS NOT NULL AND s.longitude IS NOT NULL AND s.timezone IS NOT NULL
            ORDER BY e.target_date, e.station_id
            """
        ).fetchall()
        selected = []
        for row in rows:
            if allowed_cities and normalized_city(row["city"]) not in allowed_cities:
                continue
            try:
                local_now = now.astimezone(ZoneInfo(row["timezone"]))
                target = date.fromisoformat(row["target_date"])
            except (ValueError, ZoneInfoNotFoundError):
                continue
            if local_now.date() <= target <= local_now.date() + timedelta(days=int(self.config["marketDateHorizonDays"])):
                selected.append(row)
        return selected

    def _windy_forecast(self, row: sqlite3.Row, windy_ref: dict[str, Any]) -> dict[str, Any]:
        requested_step = int(self.config["windyStepHours"])
        if requested_step == 1 and not self.windy_user_token:
            raise RuntimeError(
                "Windy Premium token is missing; hourly forecasts were not collected. "
                "Configure windyAuthTokenFile with mode 600."
            )
        params = {
            "refTime": windy_ref["ref"],
            "step": requested_step,
        }
        source_url = WINDY_METEOGRAM_URL.format(
            model=self.config["windyModel"],
            lat=round(float(row["latitude"]), 5),
            lon=round(float(row["longitude"]), 5),
        )
        if self.windy_user_token:
            request_url = self._windy_encoded_forecast_url(
                source_url,
                params,
                self.windy_user_token,
                self.windy_session_id,
            )
            payload = self.client.get(
                request_url,
                headers={
                    "Accept": "application/json binary/hcadae$indcd28",
                    "Authorization": f"Bearer {self.windy_user_token}",
                    "Origin": "https://www.windy.com",
                },
                decode_base64=True,
                error_label="Windy Premium forecast",
            )
        else:
            payload = self.client.get(source_url, params)
        if not isinstance(payload, dict):
            raise RuntimeError("Windy meteogram response is not an object")
        observed_step = meteogram_step_hours(payload)
        if observed_step is None or abs(observed_step - requested_step) > 0.001:
            observed_text = "unknown" if observed_step is None else f"{observed_step:g}h"
            raise RuntimeError(
                f"Windy returned {observed_text} data after {requested_step}h was requested; "
                "Premium hourly access is unavailable or expired"
            )
        max_c, peak_local, points = daily_max_from_meteogram(payload, row["target_date"], row["timezone"])
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        return {
            "row": row,
            "source_url": f"{source_url}?{urlencode(params)}",
            "model": header.get("model") or self.config["windyModel"],
            "ref_time": header.get("refTime") or windy_ref.get("ref"),
            "updated_at": header.get("update") or windy_ref.get("update"),
            "step_hours": observed_step,
            "max_c": max_c,
            "peak_local": peak_local,
            "points": points,
            "payload": payload,
            "status": "ok" if max_c is not None else "target_date_not_in_forecast",
            "error": None,
        }

    def capture_windy(self, run_id: int, slot: datetime) -> int:
        self._reload_windy_user_token()
        rows = self._tracked_station_dates(slot)
        if not rows:
            return 0
        if int(self.config["windyStepHours"]) == 1 and not self.windy_user_token:
            self._record_error(
                run_id,
                "windy_auth",
                None,
                "Windy Premium user token is not configured; hourly forecasts skipped",
            )
            self.db.commit()
            return 0
        try:
            windy_ref = self._windy_reference()
        except Exception as exc:
            self._record_error(run_id, "windy_minifest", None, exc)
            self.db.commit()
            return 0
        results: list[dict[str, Any]] = []
        workers = min(int(self.config["maxConcurrentRequests"]), len(rows))
        with ThreadPoolExecutor(max_workers=max(workers, 1)) as executor:
            futures = {executor.submit(self._windy_forecast, row, windy_ref): row for row in rows}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    self._record_error(run_id, "windy_meteogram", row["station_id"], exc)
                    results.append(
                        {
                            "row": row,
                            "source_url": row["windy_url"],
                            "model": self.config["windyModel"],
                            "ref_time": windy_ref.get("ref"),
                            "updated_at": windy_ref.get("update"),
                            "step_hours": None,
                            "max_c": None,
                            "peak_local": None,
                            "points": [],
                            "payload": None,
                            "status": "error",
                            "error": str(exc)[:2000],
                        }
                    )
        premium_failures = [
            result["row"] for result in results
            if result["status"] == "error"
            and "Premium hourly access is unavailable or expired" in str(result.get("error") or "")
        ]
        if premium_failures and self._renew_windy_user_token():
            retried_by_station: dict[str, dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=min(workers, len(premium_failures))) as executor:
                futures = {executor.submit(self._windy_forecast, row, windy_ref): row for row in premium_failures}
                for future in as_completed(futures):
                    row = futures[future]
                    try:
                        retried_by_station[row["station_id"]] = future.result()
                    except Exception as exc:
                        logging.warning("Windy retry failed for %s: %s", row["station_id"], exc)
            if retried_by_station:
                results = [retried_by_station.get(item["row"]["station_id"], item) for item in results]
        slot_text = iso_utc(slot)
        fetched_at = iso_utc()
        for result in results:
            row = result["row"]
            local_sample = slot.astimezone(ZoneInfo(row["timezone"]))
            max_c = result["max_c"]
            raw_payload_id = self._store_raw_weather_payload(
                run_id,
                slot,
                row["station_id"],
                row["target_date"],
                f"windy_{result['model']}",
                result.get("payload"),
                result["status"],
                result["error"],
            )
            windy_version = forecast_version_hash(
                str(result["model"]), str(row["target_date"]), result
            )
            self._record_forecast_model_run(
                "windy", str(row["station_id"]), str(row["target_date"]),
                str(result["model"]), windy_version, None,
                "provider_header_or_minifest_unverified", "low", raw_payload_id,
                {
                    "claimedRefTime": result.get("ref_time"),
                    "claimedUpdatedAt": result.get("updated_at"),
                    "fetchedAtUtc": fetched_at,
                    "note": "Provider values are retained as claims, not treated as verified model run time.",
                },
            )
            self.db.execute(
                """
                INSERT INTO windy_forecasts(
                    run_id, slot_utc, station_id, target_date, sample_local_date,
                    sample_local_hour, sample_local_offset, timezone, model,
                    model_ref_time_utc, model_updated_at_utc, forecast_max_c,
                    forecast_step_hours, forecast_max_f, forecast_peak_local, point_count, points_json,
                    source_url, raw_payload_id, status, error, fetched_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(slot_utc, station_id, target_date) DO UPDATE SET
                    run_id=excluded.run_id, model=excluded.model,
                    model_ref_time_utc=excluded.model_ref_time_utc,
                    model_updated_at_utc=excluded.model_updated_at_utc,
                    forecast_step_hours=excluded.forecast_step_hours,
                    forecast_max_c=excluded.forecast_max_c, forecast_max_f=excluded.forecast_max_f,
                    forecast_peak_local=excluded.forecast_peak_local, point_count=excluded.point_count,
                    points_json=excluded.points_json, source_url=excluded.source_url,
                    raw_payload_id=excluded.raw_payload_id,
                    status=excluded.status, error=excluded.error, fetched_at_utc=excluded.fetched_at_utc
                """,
                (
                    run_id,
                    slot_text,
                    row["station_id"],
                    row["target_date"],
                    local_sample.date().isoformat(),
                    local_sample.hour,
                    local_sample.strftime("%z"),
                    row["timezone"],
                    result["model"],
                    result["ref_time"],
                    result["updated_at"],
                    max_c,
                    result["step_hours"],
                    round(max_c * 9.0 / 5.0 + 32.0, 3) if max_c is not None else None,
                    result["peak_local"],
                    len(result["points"]),
                    json.dumps(result["points"], ensure_ascii=False, separators=(",", ":")),
                    result["source_url"],
                    raw_payload_id,
                    result["status"],
                    result["error"],
                    fetched_at,
                ),
            )
        self.db.commit()
        return len(results)

    def _fetch_open_meteo_batch(
        self, items: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        if not items:
            return {}
        primary_models = list(self.config.get("externalForecastModels", []))
        shadow_models = list(self.config.get("shadowExternalForecastModels", []))
        payload = self.client.get(
            OPEN_METEO_URL,
            {
                "latitude": ",".join(str(item["latitude"]) for item in items),
                "longitude": ",".join(str(item["longitude"]) for item in items),
                "timezone": "auto",
                "past_days": 1,
                "forecast_days": max(3, int(self.config["marketDateHorizonDays"]) + 1),
                "models": ",".join(primary_models),
                "current": (
                    "temperature_2m,relative_humidity_2m,precipitation,cloud_cover,"
                    "shortwave_radiation,direct_radiation,diffuse_radiation,"
                    "wind_speed_10m,wind_direction_10m"
                ),
                "hourly": (
                    "temperature_2m,dew_point_2m,relative_humidity_2m,"
                    "precipitation_probability,precipitation,cloud_cover,cloud_cover_low,"
                    "cloud_cover_mid,cloud_cover_high,shortwave_radiation,direct_radiation,"
                    "diffuse_radiation,wind_speed_10m,wind_direction_10m,wind_gusts_10m"
                ),
            },
            error_label=f"Open-Meteo batch forecast ({len(items)} locations)",
        )
        rows = split_multi_location_response(payload, len(items))
        if shadow_models:
            try:
                shadow_payload = self.client.get(
                    OPEN_METEO_URL,
                    {
                        "latitude": ",".join(str(item["latitude"]) for item in items),
                        "longitude": ",".join(str(item["longitude"]) for item in items),
                        "timezone": "auto",
                        "past_days": 1,
                        "forecast_days": max(3, int(self.config["marketDateHorizonDays"]) + 1),
                        "models": ",".join(shadow_models),
                        "hourly": "temperature_2m",
                    },
                    error_label=f"Open-Meteo shadow forecast ({len(items)} locations)",
                )
                shadow_rows = split_multi_location_response(shadow_payload, len(items))
                rows = [
                    merge_shadow_temperature_forecasts(primary, shadow, shadow_models)
                    for primary, shadow in zip(rows, shadow_rows)
                ]
            except Exception as exc:
                logging.warning("Open-Meteo shadow forecast unavailable: %s", exc)
        return {str(item["station_id"]): row for item, row in zip(items, rows)}

    def _fetch_open_meteo_ensemble_batch(
        self, items: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if not items:
            return {}
        payload = self.client.get(
            OPEN_METEO_ENSEMBLE_URL,
            {
                "latitude": ",".join(str(item["latitude"]) for item in items),
                "longitude": ",".join(str(item["longitude"]) for item in items),
                "timezone": "auto",
                "forecast_days": max(3, int(self.config["marketDateHorizonDays"]) + 1),
                "models": ",".join(self.config.get("ensembleForecastModels", ["ecmwf_ifs025"])),
                "hourly": "temperature_2m",
            },
            error_label=f"Open-Meteo batch ensemble ({len(items)} locations)",
        )
        rows = split_multi_location_response(payload, len(items))
        return {str(item["station_id"]): row for item, row in zip(items, rows)}

    def _fast_metar_histories(
        self, items: list[dict[str, Any]], now: datetime
    ) -> dict[str, list[dict[str, Any]]]:
        cutoff = iso_utc(now - timedelta(hours=3))
        output: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            station_id = str(item["station_id"])
            rows = self.db.execute(
                """
                SELECT payload_json FROM fast_metar_reports
                WHERE station_id=? AND observation_time_utc>=?
                ORDER BY observation_time_utc DESC,first_fetched_at_utc DESC
                """,
                (station_id, cutoff),
            ).fetchall()
            reports = []
            for row in rows:
                try:
                    report = json.loads(row["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(report, dict):
                    reports.append(report)
            output[station_id] = reports

        missing = [item for item in items if not output.get(str(item["station_id"]))]
        if missing:
            station_ids = [str(item["station_id"]) for item in missing]
            response = self.client.get(
                METAR_URL,
                {"ids": ",".join(station_ids), "format": "json", "hours": 3},
                error_label="bootstrap batch METAR",
            )
            for report in response if isinstance(response, list) else []:
                station_id = str(report.get("icaoId") or "").upper()
                if station_id in output:
                    output[station_id].append(report)
        return output

    def _fetch_external_station_weather(self, item: dict[str, Any]) -> dict[str, Any]:
        station_network_payload = None
        station_network_error = None
        station_id = str(item["station_id"] or "")
        if len(station_id) == 4 and station_id.isascii() and station_id.isalnum():
            process_cities = {
                normalized_city(city)
                for city in self.config.get("processAnalysisCities", [])
                if str(city).strip()
            }
            if not process_cities or normalized_city(item.get("city")) in process_cities:
                try:
                    radius_km = float(self.config.get("stationNetworkRadiusKm", 250))
                    latitude = float(item["latitude"])
                    longitude = float(item["longitude"])
                    lat_delta = radius_km / 111.32
                    lon_delta = radius_km / (111.32 * max(0.2, math.cos(math.radians(latitude))))
                    bbox = f"{latitude-lat_delta:.4f},{longitude-lon_delta:.4f},{latitude+lat_delta:.4f},{longitude+lon_delta:.4f}"
                    response = self.client.get(
                        METAR_URL,
                        {"bbox": bbox, "format": "json", "hours": 3},
                        error_label=f"nearby METAR network {station_id}",
                    )
                    station_network_payload = response if isinstance(response, list) else []
                    if not station_network_payload:
                        station_network_error = "nearby METAR query returned no reports"
                except Exception as exc:
                    station_network_error = str(exc)[:2000]
            else:
                station_network_payload = []
        else:
            station_network_error = "station does not have a four-character ICAO identifier"
        return {
            "item": item,
            "station_network_payload": station_network_payload,
            "station_network_error": station_network_error,
        }

    def _insert_observation(
        self,
        run_id: int,
        slot: datetime,
        item: dict[str, Any],
        source: str,
        payload_id: int,
        values: dict[str, Any],
        status: str,
        error: str | None,
    ) -> None:
        local_sample = slot.astimezone(ZoneInfo(item["timezone"]))
        local_date = local_sample.date().isoformat()
        temperature = as_float(values.get("temperature_c"))
        previous = self.db.execute(
            """
            SELECT MAX(observed_daily_max_c) FROM weather_observations
            WHERE station_id=? AND sample_local_date=? AND source=?
            """,
            (item["station_id"], local_date, source),
        ).fetchone()
        previous_max = as_float(previous[0]) if previous else None
        daily_max = max(value for value in (previous_max, temperature) if value is not None) if any(
            value is not None for value in (previous_max, temperature)
        ) else None
        self.db.execute(
            """
            INSERT INTO weather_observations(
                run_id,slot_utc,station_id,sample_local_date,sample_local_time,timezone,source,
                observation_time_utc,temperature_c,dewpoint_c,relative_humidity,precipitation_mm,
                cloud_cover_pct,wind_direction_deg,wind_speed,wind_speed_unit,wind_gust,visibility_m,
                pressure_hpa,solar_radiation_wm2,direct_radiation_wm2,diffuse_radiation_wm2,
                flight_category,sky_conditions_json,raw_metar,metar_type,metar_parser_status,
                metar_parser_error,weather_code,
                observed_daily_max_c,raw_payload_id,status,error,fetched_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(slot_utc,station_id,source) DO UPDATE SET
                run_id=excluded.run_id,observation_time_utc=excluded.observation_time_utc,
                temperature_c=excluded.temperature_c,dewpoint_c=excluded.dewpoint_c,
                relative_humidity=excluded.relative_humidity,precipitation_mm=excluded.precipitation_mm,
                cloud_cover_pct=excluded.cloud_cover_pct,wind_direction_deg=excluded.wind_direction_deg,
                wind_speed=excluded.wind_speed,wind_speed_unit=excluded.wind_speed_unit,wind_gust=excluded.wind_gust,
                visibility_m=excluded.visibility_m,pressure_hpa=excluded.pressure_hpa,
                solar_radiation_wm2=excluded.solar_radiation_wm2,
                direct_radiation_wm2=excluded.direct_radiation_wm2,
                diffuse_radiation_wm2=excluded.diffuse_radiation_wm2,
                flight_category=excluded.flight_category,sky_conditions_json=excluded.sky_conditions_json,
                raw_metar=excluded.raw_metar,metar_type=excluded.metar_type,
                metar_parser_status=excluded.metar_parser_status,
                metar_parser_error=excluded.metar_parser_error,
                weather_code=excluded.weather_code,observed_daily_max_c=excluded.observed_daily_max_c,
                raw_payload_id=excluded.raw_payload_id,status=excluded.status,error=excluded.error,
                fetched_at_utc=excluded.fetched_at_utc
            """,
            (
                run_id, iso_utc(slot), item["station_id"], local_date,
                local_sample.isoformat(timespec="minutes"), item["timezone"], source,
                values.get("observation_time_utc"), temperature, as_float(values.get("dewpoint_c")),
                as_float(values.get("relative_humidity")), as_float(values.get("precipitation_mm")),
                as_float(values.get("cloud_cover_pct")), as_float(values.get("wind_direction_deg")),
                as_float(values.get("wind_speed")), values.get("wind_speed_unit"), as_float(values.get("wind_gust")),
                as_float(values.get("visibility_m")), as_float(values.get("pressure_hpa")),
                as_float(values.get("solar_radiation_wm2")), as_float(values.get("direct_radiation_wm2")),
                as_float(values.get("diffuse_radiation_wm2")), values.get("flight_category"),
                values.get("sky_conditions_json"), values.get("raw_metar"), values.get("metar_type"),
                values.get("metar_parser_status"), values.get("metar_parser_error"),
                values.get("weather_code"), daily_max, payload_id, status, error, iso_utc(),
            ),
        )

    def _register_source_version(
        self,
        source: str,
        station_id: str,
        target_date: str,
        product: str,
        version_hash: str,
        raw_payload_id: int | None = None,
        model_ref_time_utc: str | None = None,
    ) -> bool:
        latest = self.db.execute(
            """
            SELECT version_hash FROM source_collection_versions
            WHERE source=? AND station_id=? AND target_date=? AND product=?
            ORDER BY last_seen_utc DESC LIMIT 1
            """,
            (source, station_id, target_date, product),
        ).fetchone()
        changed = latest is None or latest["version_hash"] != version_hash
        now_text = iso_utc()
        self.db.execute(
            """
            INSERT INTO source_collection_versions(
                source,station_id,target_date,product,version_hash,model_ref_time_utc,
                first_seen_utc,last_seen_utc,raw_payload_id
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source,station_id,target_date,product,version_hash) DO UPDATE SET
                last_seen_utc=excluded.last_seen_utc,
                model_ref_time_utc=COALESCE(excluded.model_ref_time_utc,model_ref_time_utc),
                raw_payload_id=COALESCE(excluded.raw_payload_id,raw_payload_id)
            """,
            (
                source, station_id, target_date, product, version_hash, model_ref_time_utc,
                now_text, now_text, raw_payload_id,
            ),
        )
        return changed

    def _record_forecast_model_run(
        self, source: str, station_id: str, target_date: str, model: str,
        version_hash: str, model_run_time_utc: str | None,
        run_time_source: str, run_time_confidence: str,
        raw_payload_id: int | None, metadata: dict[str, Any],
    ) -> None:
        now_text = iso_utc()
        self.db.execute(
            """
            INSERT INTO forecast_model_runs(
                source,station_id,target_date,model,version_hash,model_run_time_utc,
                run_time_source,run_time_confidence,first_seen_utc,last_seen_utc,
                raw_payload_id,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source,station_id,target_date,model,version_hash) DO UPDATE SET
                last_seen_utc=excluded.last_seen_utc,
                model_run_time_utc=COALESCE(excluded.model_run_time_utc,model_run_time_utc),
                raw_payload_id=COALESCE(excluded.raw_payload_id,raw_payload_id),
                metadata_json=excluded.metadata_json
            """,
            (
                source, station_id, target_date, model, version_hash, model_run_time_utc,
                run_time_source, run_time_confidence, now_text, now_text,
                raw_payload_id,
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), default=str),
            ),
        )

    def _source_poll_due(self, source: str, now: datetime) -> bool:
        row = self.db.execute(
            "SELECT next_attempt_utc FROM source_poll_state WHERE source=?", (source,)
        ).fetchone()
        if not row or not row["next_attempt_utc"]:
            return True
        try:
            return datetime.fromisoformat(str(row["next_attempt_utc"])) <= now.astimezone(UTC)
        except ValueError:
            return True

    def _update_source_poll_state(
        self, source: str, now: datetime, success: bool, error: str | None = None
    ) -> None:
        row = self.db.execute(
            "SELECT consecutive_failures FROM source_poll_state WHERE source=?", (source,)
        ).fetchone()
        failures = 0 if success else min(12, int(row[0] if row else 0) + 1)
        if success:
            interval_key = {
                "open_meteo_ensemble": "ensemblePollIntervalMinutes",
                "cma_meso": "cmaMesoPollIntervalMinutes",
            }.get(source, "openMeteoPollIntervalMinutes")
            delay = int(self.config.get(interval_key, 180)) * 60
        else:
            delay = min(
                int(self.config.get("openMeteoFailureBackoffMaxMinutes", 360)) * 60,
                int(self.config.get("openMeteoFailureBackoffMinutes", 30)) * 60 * (2 ** max(0, failures - 1)),
            )
        next_attempt = now.astimezone(UTC) + timedelta(seconds=delay)
        self.db.execute(
            """
            INSERT INTO source_poll_state(
                source,last_attempt_utc,next_attempt_utc,consecutive_failures,last_status,last_error
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(source) DO UPDATE SET
                last_attempt_utc=excluded.last_attempt_utc,next_attempt_utc=excluded.next_attempt_utc,
                consecutive_failures=excluded.consecutive_failures,last_status=excluded.last_status,
                last_error=excluded.last_error
            """,
            (
                source, iso_utc(now), iso_utc(next_attempt), failures,
                "ok" if success else "error", error,
            ),
        )

    def _persist_station_network(
        self,
        item: dict[str, Any],
        reports: list[dict[str, Any]],
        raw_payload_id: int,
    ) -> int:
        primary_lat = as_float(item.get("latitude"))
        primary_lon = as_float(item.get("longitude"))
        written = 0
        seen_at = iso_utc()
        for report in reports:
            station_id = str(report.get("icaoId") or "").strip().upper()
            observation_time = epoch_to_iso_utc(report.get("obsTime"))
            latitude = as_float(report.get("lat"))
            longitude = as_float(report.get("lon"))
            if not station_id or observation_time is None or latitude is None or longitude is None:
                continue
            parsed = parsed_metar_fields(report.get("rawOb"))
            distance = (
                haversine_km(primary_lat, primary_lon, latitude, longitude)
                if primary_lat is not None and primary_lon is not None else None
            )
            bearing = (
                bearing_deg(primary_lat, primary_lon, latitude, longitude)
                if primary_lat is not None and primary_lon is not None and distance is not None and distance > 0.01
                else 0.0 if distance is not None else None
            )
            visibility = as_float(report.get("visib"))
            self.db.execute(
                """
                INSERT INTO station_network_reports(
                    primary_station_id,station_id,station_name,observation_time_utc,
                    report_time_utc,receipt_time_utc,metar_type,latitude,longitude,elevation_m,
                    distance_km,bearing_from_primary_deg,temperature_c,dewpoint_c,
                    wind_direction_deg,wind_speed_kt,wind_gust_kt,pressure_hpa,visibility_m,
                    flight_category,cover,clouds_json,weather_code,raw_metar,raw_payload_id,
                    first_seen_utc,last_seen_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(primary_station_id,station_id,observation_time_utc) DO UPDATE SET
                    station_name=excluded.station_name,report_time_utc=excluded.report_time_utc,
                    receipt_time_utc=excluded.receipt_time_utc,metar_type=excluded.metar_type,
                    latitude=excluded.latitude,longitude=excluded.longitude,elevation_m=excluded.elevation_m,
                    distance_km=excluded.distance_km,bearing_from_primary_deg=excluded.bearing_from_primary_deg,
                    temperature_c=excluded.temperature_c,dewpoint_c=excluded.dewpoint_c,
                    wind_direction_deg=excluded.wind_direction_deg,wind_speed_kt=excluded.wind_speed_kt,
                    wind_gust_kt=excluded.wind_gust_kt,pressure_hpa=excluded.pressure_hpa,
                    visibility_m=excluded.visibility_m,flight_category=excluded.flight_category,
                    cover=excluded.cover,clouds_json=excluded.clouds_json,weather_code=excluded.weather_code,
                    raw_metar=excluded.raw_metar,raw_payload_id=excluded.raw_payload_id,last_seen_utc=excluded.last_seen_utc
                """,
                (
                    item["station_id"], station_id, report.get("name"), observation_time,
                    report.get("reportTime"), report.get("receiptTime"), report.get("metarType"),
                    latitude, longitude, as_float(report.get("elev")),
                    round(distance, 3) if distance is not None else None,
                    round(bearing, 2) if bearing is not None else None,
                    as_float(report.get("temp")), as_float(report.get("dewp")),
                    as_float(report.get("wdir")), as_float(report.get("wspd")),
                    as_float(report.get("wgst", parsed.get("wind_gust"))),
                    as_float(report.get("altim", parsed.get("pressure_hpa"))),
                    visibility * 1609.344 if visibility is not None else parsed.get("visibility_m"),
                    report.get("fltCat"), report.get("cover"),
                    json.dumps(report.get("clouds") or [], ensure_ascii=False, separators=(",", ":")),
                    report.get("wxString"), report.get("rawOb"), raw_payload_id, seen_at, seen_at,
                ),
            )
            written += 1
        return written

    def capture_external_weather(
        self,
        run_id: int,
        slot: datetime,
        allowed_cities: set[str] | None = None,
        include_forecasts: bool = True,
    ) -> tuple[int, int, int]:
        if not self.config.get("captureExternalWeather", True):
            return 0, 0, 0
        tracked = self._tracked_station_dates(slot, allowed_cities)
        stations: dict[str, dict[str, Any]] = {}
        for row in tracked:
            item = stations.setdefault(
                row["station_id"],
                {
                    "station_id": row["station_id"], "station_name": row["station_name"],
                    "city": row["city"], "latitude": row["latitude"], "longitude": row["longitude"],
                    "timezone": row["timezone"], "target_dates": [],
                },
            )
            if row["target_date"] not in item["target_dates"]:
                item["target_dates"].append(row["target_date"])
        if not stations:
            return 0, 0, 0

        station_items = list(stations.values())
        forecast_payloads: dict[str, dict[str, Any]] = {}
        forecast_error = None
        include_forecasts = include_forecasts and self._source_poll_due("open_meteo", slot)
        if include_forecasts:
            try:
                forecast_payloads = self._fetch_open_meteo_batch(station_items)
                self._update_source_poll_state("open_meteo", slot, True)
            except Exception as exc:
                forecast_error = str(exc)[:2000]
                self._record_error(run_id, "open_meteo_batch", None, forecast_error)
                self._update_source_poll_state("open_meteo", slot, False, forecast_error)
        try:
            metar_histories = self._fast_metar_histories(station_items, slot)
        except Exception as exc:
            metar_histories = {}
            self._record_error(run_id, "metar_batch", None, exc)

        results: list[dict[str, Any]] = []
        workers = min(int(self.config["maxConcurrentRequests"]), len(stations))
        with ThreadPoolExecutor(max_workers=max(workers, 1)) as executor:
            futures = {
                executor.submit(self._fetch_external_station_weather, item): item
                for item in station_items
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(
                        {
                            "item": item,
                            "station_network_payload": None,
                            "station_network_error": str(exc)[:2000],
                        }
                    )

        for result in results:
            station_id = str(result["item"]["station_id"])
            history = metar_histories.get(station_id, [])
            result["forecast_payload"] = forecast_payloads.get(station_id)
            result["forecast_error"] = forecast_error
            result["metar_history"] = history
            result["metar_payload"] = next(
                (
                    row for row in history
                    if str(row.get("icaoId") or "").upper() == station_id.upper()
                ),
                history[0] if history else None,
            )
            result["metar_error"] = None if result["metar_payload"] else "batch METAR contained no observation"

        forecast_count = observation_count = network_reports_count = 0
        for result in results:
            item = result["item"]
            forecast_status = "ok" if isinstance(result["forecast_payload"], dict) else "error"
            parsed_forecasts: list[tuple[str, str, dict[str, Any], str]] = []
            if isinstance(result["forecast_payload"], dict):
                for target_date in item["target_dates"]:
                    forecast_models = [
                        *self.config.get("externalForecastModels", []),
                        *self.config.get("shadowExternalForecastModels", []),
                    ]
                    forecasts = open_meteo_model_forecasts(
                        result["forecast_payload"], target_date, item["timezone"],
                        forecast_models,
                    )
                    by_model = {row["model"]: row for row in forecasts}
                    for model in forecast_models:
                        parsed = by_model.get(model)
                        if parsed:
                            parsed_forecasts.append(
                                (target_date, model, parsed, forecast_version_hash(model, target_date, parsed))
                            )

            changed_forecasts: list[tuple[str, str, dict[str, Any], str]] = []
            for target_date, model, parsed, version_hash in parsed_forecasts:
                if self._register_source_version(
                    "open_meteo", item["station_id"], target_date, model, version_hash
                ):
                    changed_forecasts.append((target_date, model, parsed, version_hash))

            forecast_payload_id = None
            if changed_forecasts:
                forecast_payload_id = self._store_raw_weather_payload(
                    run_id, slot, item["station_id"], None, "open_meteo_multi",
                    result["forecast_payload"], forecast_status, result["forecast_error"],
                )
                for target_date, model, _parsed, version_hash in changed_forecasts:
                    self.db.execute(
                        """
                        UPDATE source_collection_versions SET raw_payload_id=?
                        WHERE source='open_meteo' AND station_id=? AND target_date=?
                          AND product=? AND version_hash=?
                        """,
                        (forecast_payload_id, item["station_id"], target_date, model, version_hash),
                    )
            current = (
                result["forecast_payload"].get("current", {})
                if isinstance(result["forecast_payload"], dict)
                else {}
            )
            observation_time = None
            if current.get("time"):
                try:
                    observation_time = datetime.fromisoformat(str(current["time"])).replace(
                        tzinfo=ZoneInfo(item["timezone"])
                    ).astimezone(UTC).isoformat(timespec="seconds")
                except (ValueError, ZoneInfoNotFoundError):
                    observation_time = None
            if include_forecasts and isinstance(result["forecast_payload"], dict):
                self._insert_observation(
                    run_id, slot, item, "open_meteo_current", forecast_payload_id,
                    {
                        "observation_time_utc": observation_time,
                        "temperature_c": current.get("temperature_2m"),
                        "relative_humidity": current.get("relative_humidity_2m"),
                        "precipitation_mm": current.get("precipitation"),
                        "cloud_cover_pct": current.get("cloud_cover"),
                        "solar_radiation_wm2": current.get("shortwave_radiation"),
                        "direct_radiation_wm2": current.get("direct_radiation"),
                        "diffuse_radiation_wm2": current.get("diffuse_radiation"),
                        "wind_direction_deg": current.get("wind_direction_10m"),
                        "wind_speed": current.get("wind_speed_10m"),
                        "wind_speed_unit": "km/h",
                    },
                    forecast_status, None,
                )
                observation_count += 1

            for target_date, model, _parsed, version_hash in parsed_forecasts:
                self._record_forecast_model_run(
                    "open_meteo", str(item["station_id"]), target_date, model,
                    version_hash, None, "provider_not_exposed", "unavailable",
                    forecast_payload_id,
                    {
                        "fetchedAtUtc": iso_utc(),
                        "generationTimeMs": (result["forecast_payload"] or {}).get("generationtime_ms"),
                        "note": "Open-Meteo generation time is API processing latency, not model run time.",
                    },
                )

            for target_date, model, parsed, version_hash in changed_forecasts:
                local_sample = slot.astimezone(ZoneInfo(item["timezone"]))
                fetched_at = iso_utc()
                points_json = json.dumps(
                    parsed["points"], ensure_ascii=False, separators=(",", ":")
                )
                self.db.execute(
                    """
                    INSERT INTO external_forecasts(
                        run_id,slot_utc,station_id,target_date,sample_local_date,sample_local_time,
                        timezone,model,forecast_max_c,forecast_peak_local,point_count,points_json,
                        source,raw_payload_id,status,error,fetched_at_utc
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(slot_utc,station_id,target_date,model) DO UPDATE SET
                        run_id=excluded.run_id,forecast_max_c=excluded.forecast_max_c,
                        forecast_peak_local=excluded.forecast_peak_local,point_count=excluded.point_count,
                        points_json=excluded.points_json,raw_payload_id=excluded.raw_payload_id,
                        status=excluded.status,error=excluded.error,fetched_at_utc=excluded.fetched_at_utc
                    """,
                    (
                        run_id, iso_utc(slot), item["station_id"], target_date,
                        local_sample.date().isoformat(), local_sample.isoformat(timespec="minutes"),
                        item["timezone"], model, parsed["max_c"], parsed["peak_local"],
                        len(parsed["points"]), points_json,
                        "open_meteo", forecast_payload_id, "ok", None, fetched_at,
                    ),
                )
                self.forecast_cutoff_tracker.capture_new_run(
                    source="open_meteo", model=model,
                    station_id=str(item["station_id"]), target_date=target_date,
                    sample_slot_utc=iso_utc(slot), fetched_at_utc=fetched_at,
                    version_hash=version_hash, forecast_max_c=float(parsed["max_c"]),
                    forecast_peak_local=parsed["peak_local"], points_json=points_json,
                    model_run_time_source="provider_not_exposed",
                )
                forecast_count += 1

            metar_status = "ok" if isinstance(result["metar_payload"], dict) else "error"
            metar_payload_id = self._store_raw_weather_payload(
                run_id, slot, item["station_id"], None, "aviationweather_metar",
                result.get("metar_history") or result["metar_payload"], metar_status, result["metar_error"],
            )
            if result["metar_error"]:
                self._record_error(run_id, "metar", item["station_id"], result["metar_error"])
            metar = result["metar_payload"] if isinstance(result["metar_payload"], dict) else {}
            parsed = parsed_metar_fields(metar.get("rawOb"))
            self._insert_observation(
                run_id, slot, item, "metar", metar_payload_id,
                {
                    "observation_time_utc": epoch_to_iso_utc(metar.get("obsTime")),
                    "temperature_c": metar.get("temp"), "dewpoint_c": metar.get("dewp"),
                    "wind_direction_deg": metar.get("wdir"), "wind_speed": metar.get("wspd"),
                    "wind_speed_unit": "kt", "weather_code": metar.get("wxString") or metar.get("rawOb"),
                    "wind_gust": metar.get("wgst", parsed.get("wind_gust")),
                    "visibility_m": (
                        as_float(metar.get("visib")) * 1609.344
                        if isinstance(metar.get("visib"), (int, float)) else parsed.get("visibility_m")
                    ),
                    "pressure_hpa": metar.get("altim", parsed.get("pressure_hpa")),
                    "flight_category": metar.get("fltCat"),
                    "sky_conditions_json": json.dumps(metar.get("clouds", []), ensure_ascii=False, separators=(",", ":"))
                    if metar.get("clouds") else parsed.get("sky_conditions_json"),
                    "raw_metar": metar.get("rawOb"),
                    "metar_type": metar.get("metarType"),
                    "metar_parser_status": parsed.get("parser_status"),
                    "metar_parser_error": parsed.get("parser_error"),
                },
                metar_status, result["metar_error"],
            )
            observation_count += 1
            nearby_payload = result.get("station_network_payload")
            primary_history = result.get("metar_history")
            network_payload = []
            if isinstance(primary_history, list):
                network_payload.extend(primary_history)
            if isinstance(nearby_payload, list):
                network_payload.extend(nearby_payload)
            network_status = "ok" if network_payload else "error"
            network_payload_id = self._store_raw_weather_payload(
                run_id, slot, item["station_id"], None, "aviationweather_station_network",
                network_payload, network_status, result.get("station_network_error"),
            )
            if result.get("station_network_error"):
                self._record_error(run_id, "metar_network", item["station_id"], result["station_network_error"])
            if network_payload:
                network_reports_count += self._persist_station_network(item, network_payload, network_payload_id)
        self.db.commit()
        return forecast_count, observation_count, network_reports_count

    def capture_cma_meso(self, run_id: int, slot: datetime) -> int:
        """Capture authenticated CMA-MESO forecasts when an explicit mapping exists."""
        if not self.config.get("captureCmaMesoForecasts", True) or not self.cma_meso.configured:
            return 0
        if not self._source_poll_due("cma_meso", slot):
            return 0
        allowed = {
            normalized_city(city)
            for city in self.config.get("processAnalysisCities", [])
            if str(city).strip()
        }
        tracked = self._tracked_station_dates(slot, allowed)
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for item in tracked:
            unique[(str(item["station_id"]), str(item["target_date"]))] = dict(item)
        if not unique:
            return 0

        results: list[tuple[dict[str, Any], Any, dict[str, Any] | None, str | None, str]] = []

        def fetch(item: dict[str, Any]) -> tuple[dict[str, Any], Any, dict[str, Any] | None, str | None, str]:
            fetched_at = iso_utc()
            try:
                payload, parsed = self.cma_meso.fetch(
                    station_id=str(item["station_id"]),
                    latitude=float(item["latitude"]), longitude=float(item["longitude"]),
                    target_date=str(item["target_date"]), timezone_name=str(item["timezone"]),
                )
                return item, payload, parsed, None, iso_utc()
            except Exception as exc:
                return item, None, None, str(exc)[:2000], fetched_at

        workers = min(4, len(unique))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = [executor.submit(fetch, item) for item in unique.values()]
            for future in as_completed(futures):
                results.append(future.result())

        successes = 0
        for item, payload, parsed, error, fetched_at in results:
            station_id = str(item["station_id"])
            target_date = str(item["target_date"])
            payload_id = self._store_raw_weather_payload(
                run_id, slot, station_id, target_date, "cma_meso", payload,
                "ok" if parsed else "error", error,
            )
            if not parsed:
                self._record_error(run_id, "cma_meso", f"{station_id}:{target_date}", error)
                continue
            version_hash = forecast_version_hash("cma_meso_3km", target_date, parsed)
            changed = self._register_source_version(
                "cma_meso", station_id, target_date, "cma_meso_3km", version_hash,
                payload_id, parsed.get("model_run_time_utc"),
            )
            self._record_forecast_model_run(
                "cma_meso", station_id, target_date, "cma_meso_3km", version_hash,
                parsed.get("model_run_time_utc"), parsed["run_time_source"],
                parsed["run_time_confidence"], payload_id,
                {"fetchedAtUtc": fetched_at, "adapter": "configured_field_mapping"},
            )
            if not changed:
                successes += 1
                continue
            local_sample = slot.astimezone(ZoneInfo(str(item["timezone"])))
            points_json = json.dumps(parsed["points"], ensure_ascii=False, separators=(",", ":"))
            self.db.execute(
                """
                INSERT INTO external_forecasts(
                    run_id,slot_utc,station_id,target_date,sample_local_date,sample_local_time,
                    timezone,model,forecast_max_c,forecast_peak_local,point_count,points_json,
                    source,raw_payload_id,status,error,fetched_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(slot_utc,station_id,target_date,model) DO UPDATE SET
                    run_id=excluded.run_id,forecast_max_c=excluded.forecast_max_c,
                    forecast_peak_local=excluded.forecast_peak_local,point_count=excluded.point_count,
                    points_json=excluded.points_json,source=excluded.source,
                    raw_payload_id=excluded.raw_payload_id,status=excluded.status,
                    error=excluded.error,fetched_at_utc=excluded.fetched_at_utc
                """,
                (
                    run_id, iso_utc(slot), station_id, target_date,
                    local_sample.date().isoformat(), local_sample.isoformat(timespec="minutes"),
                    item["timezone"], "cma_meso_3km", parsed["max_c"], parsed["peak_local"],
                    len(parsed["points"]), points_json, "cma_meso", payload_id, "ok", None, fetched_at,
                ),
            )
            self.forecast_cutoff_tracker.capture_new_run(
                source="cma_meso", model="cma_meso_3km", station_id=station_id,
                target_date=target_date, sample_slot_utc=iso_utc(slot),
                fetched_at_utc=fetched_at, version_hash=version_hash,
                forecast_max_c=float(parsed["max_c"]),
                forecast_peak_local=parsed["peak_local"], points_json=points_json,
                model_run_time_utc=parsed.get("model_run_time_utc"),
                model_run_time_source=parsed["run_time_source"],
            )
            successes += 1
        self._update_source_poll_state(
            "cma_meso", slot, successes > 0,
            None if successes > 0 else "all CMA-MESO requests failed",
        )
        self.db.commit()
        return successes

    def capture_ensemble_forecasts(self, run_id: int, slot: datetime) -> int:
        """Collect member-level daily maxima as a separate, auditable source."""
        if not self.config.get("captureEnsembleForecasts", True):
            return 0
        if not self._source_poll_due("open_meteo_ensemble", slot):
            return 0
        tracked = self._tracked_station_dates(slot)
        stations: dict[str, dict[str, Any]] = {}
        for row in tracked:
            item = stations.setdefault(str(row["station_id"]), {
                "station_id": row["station_id"], "latitude": row["latitude"],
                "longitude": row["longitude"], "timezone": row["timezone"],
                "target_dates": [],
            })
            if row["target_date"] not in item["target_dates"]:
                item["target_dates"].append(row["target_date"])
        items = list(stations.values())
        if not items:
            return 0
        try:
            payloads = self._fetch_open_meteo_ensemble_batch(items)
            self._update_source_poll_state("open_meteo_ensemble", slot, True)
        except Exception as exc:
            self._record_error(run_id, "open_meteo_ensemble_batch", None, exc)
            self._update_source_poll_state("open_meteo_ensemble", slot, False, str(exc)[:2000])
            self.db.commit()
            return 0
        written = 0
        fetched_at = iso_utc()
        models = list(self.config.get("ensembleForecastModels", ["ecmwf_ifs025"]))
        model = str(models[0]) if models else "ecmwf_ifs025"
        for item in items:
            payload = payloads.get(str(item["station_id"]))
            if not isinstance(payload, dict):
                continue
            parsed_rows = []
            for target_date in item["target_dates"]:
                parsed = open_meteo_ensemble_daily_max(payload, target_date)
                if parsed:
                    version_hash = canonical_sha256({
                        "model": model, "targetDate": target_date,
                        "members": parsed["members"],
                    })
                    changed = self._register_source_version(
                        "open_meteo_ensemble", str(item["station_id"]), target_date,
                        model, version_hash,
                    )
                    parsed_rows.append((target_date, parsed, version_hash, changed))
            if not parsed_rows:
                continue
            raw_payload_id = self._store_raw_weather_payload(
                run_id, slot, str(item["station_id"]), None,
                "open_meteo_ensemble_multi", payload,
            )
            for target_date, parsed, version_hash, changed in parsed_rows:
                self._record_forecast_model_run(
                    "open_meteo_ensemble", str(item["station_id"]), target_date,
                    model, version_hash, None, "provider_not_exposed", "unavailable",
                    raw_payload_id,
                    {
                        "fetchedAtUtc": fetched_at,
                        "memberCount": parsed["memberCount"],
                        "generationTimeMs": payload.get("generationtime_ms"),
                    },
                )
                if not changed:
                    continue
                self.db.execute(
                    """
                    INSERT INTO ensemble_forecasts(
                        run_id,slot_utc,station_id,target_date,timezone,source,model,
                        version_hash,model_run_time_utc,model_run_time_source,
                        model_run_confidence,member_count,mean_max_c,std_max_c,min_max_c,
                        max_max_c,q10_max_c,q50_max_c,q90_max_c,member_maxima_json,
                        raw_payload_id,status,error,fetched_at_utc
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(slot_utc,station_id,target_date,model) DO UPDATE SET
                        version_hash=excluded.version_hash,member_count=excluded.member_count,
                        mean_max_c=excluded.mean_max_c,std_max_c=excluded.std_max_c,
                        min_max_c=excluded.min_max_c,max_max_c=excluded.max_max_c,
                        q10_max_c=excluded.q10_max_c,q50_max_c=excluded.q50_max_c,
                        q90_max_c=excluded.q90_max_c,member_maxima_json=excluded.member_maxima_json,
                        raw_payload_id=excluded.raw_payload_id,status=excluded.status,
                        error=excluded.error,fetched_at_utc=excluded.fetched_at_utc
                    """,
                    (
                        run_id, iso_utc(slot), item["station_id"], target_date,
                        item["timezone"], "open_meteo_ensemble", model, version_hash,
                        None, "provider_not_exposed", "unavailable", parsed["memberCount"],
                        parsed["meanMaxC"], parsed["stdMaxC"], parsed["minMaxC"],
                        parsed["maxMaxC"], parsed["q10MaxC"], parsed["q50MaxC"],
                        parsed["q90MaxC"],
                        json.dumps(parsed["members"], ensure_ascii=False, separators=(",", ":")),
                        raw_payload_id, "ok", None, fetched_at,
                    ),
                )
                written += 1
        self.db.commit()
        return written

    def _process_station_items(self, slot: datetime) -> list[dict[str, Any]]:
        allowed = {
            normalized_city(item)
            for item in self.config.get("processAnalysisCities", [])
            if str(item).strip()
        }
        output: dict[str, dict[str, Any]] = {}
        for row in self._tracked_station_dates(slot):
            if allowed and normalized_city(row["city"]) not in allowed:
                continue
            output.setdefault(
                row["station_id"],
                {
                    "station_id": row["station_id"], "station_name": row["station_name"],
                    "city": row["city"], "latitude": row["latitude"], "longitude": row["longitude"],
                    "timezone": row["timezone"], "target_date": row["target_date"],
                },
            )
        return list(output.values())

    def _fetch_remote_sensing_station(
        self, item: dict[str, Any], include_jaxa: bool = False
    ) -> dict[str, Any]:
        wind_from = as_float(item.get("wind_from_deg"))
        output = {"item": item, "results": []}
        for source, fetch in (
            ("rainviewer", lambda: self.remote_sensing.radar(float(item["latitude"]), float(item["longitude"]), wind_from)),
            ("nict_himawari_true_colour", lambda: self.remote_sensing.satellite(float(item["latitude"]), float(item["longitude"]))),
        ):
            try:
                output["results"].append(fetch())
            except Exception as exc:
                output["results"].append(
                    {
                        "source": source, "frame_time_utc": None, "status": "error",
                        "quality": "unavailable", "features": {}, "source_url": None,
                        "raw_sha256": None, "error": str(exc)[:2000],
                    }
                )
        if include_jaxa:
            try:
                output["results"].extend(
                    self.remote_sensing.jaxa_products(
                        float(item["latitude"]), float(item["longitude"])
                    )
                )
            except Exception as exc:
                for source in ("jaxa_himawari_swr_l2", "jaxa_himawari_cloud_l2"):
                    output["results"].append(
                        {
                            "source": source, "frame_time_utc": None, "status": "error",
                            "quality": "unavailable", "features": {}, "source_url": None,
                            "raw_sha256": None, "error": str(exc)[:2000],
                        }
                    )
        return output

    def capture_remote_sensing_and_process(self, run_id: int, slot: datetime) -> tuple[int, int]:
        if not self.config.get("captureRemoteSensing", True):
            return 0, 0
        items = self._process_station_items(slot)
        if not items:
            return 0, 0
        self.remote_sensing.begin_cycle()
        self.remote_sensing.prepare_metadata()
        jaxa_interval = int(self.config.get("jaxaIntervalMinutes", 10))
        latest_jaxa = self.db.execute(
            """
            SELECT MAX(fetched_at_utc) FROM remote_sensing_snapshots
            WHERE source IN ('jaxa_himawari_swr_l2','jaxa_himawari_cloud_l2') AND status='ok'
            """
        ).fetchone()
        last_jaxa_at = (
            datetime.fromisoformat(str(latest_jaxa[0]))
            if latest_jaxa and latest_jaxa[0] else None
        )
        include_jaxa = bool(self.config.get("captureJaxaProducts", True)) and (
            last_jaxa_at is None
            or slot.astimezone(UTC) - last_jaxa_at.astimezone(UTC) >= timedelta(minutes=max(1, jaxa_interval))
        )
        if include_jaxa:
            self.remote_sensing.prepare_jaxa_metadata()
        for item in items:
            wind_row = self.db.execute(
                "SELECT wind_direction_deg FROM station_network_reports WHERE primary_station_id=? "
                "ORDER BY observation_time_utc DESC LIMIT 1",
                (item["station_id"],),
            ).fetchone()
            item["wind_from_deg"] = as_float(wind_row[0]) if wind_row else None
        remote_count = process_count = 0
        workers = min(int(self.config.get("maxConcurrentRequests", 6)), len(items))
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = [
                executor.submit(self._fetch_remote_sensing_station, item, include_jaxa)
                for item in items
            ]
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:
                    logging.warning("remote sensing worker failed: %s", exc)
        for result in results:
            item = result["item"]
            for snapshot in result["results"]:
                self.db.execute(
                    """
                    INSERT INTO remote_sensing_snapshots(
                        run_id,slot_utc,station_id,source,frame_time_utc,status,quality,
                        features_json,source_url,raw_sha256,error,fetched_at_utc
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(slot_utc,station_id,source) DO UPDATE SET
                        run_id=excluded.run_id,frame_time_utc=excluded.frame_time_utc,
                        status=excluded.status,quality=excluded.quality,features_json=excluded.features_json,
                        source_url=excluded.source_url,raw_sha256=excluded.raw_sha256,error=excluded.error,
                        fetched_at_utc=excluded.fetched_at_utc
                    """,
                    (
                        run_id, iso_utc(slot), item["station_id"], snapshot["source"], snapshot.get("frame_time_utc"),
                        snapshot.get("status", "error"), snapshot.get("quality", "unavailable"),
                        json.dumps(snapshot.get("features") or {}, ensure_ascii=False, separators=(",", ":")),
                        snapshot.get("source_url"), snapshot.get("raw_sha256"), snapshot.get("error"), iso_utc(),
                    ),
                )
                remote_count += 1
                if snapshot.get("error"):
                    self._record_error(run_id, snapshot["source"], item["station_id"], snapshot["error"])
            state = analyze_weather_process(
                self.db,
                item,
                item["target_date"],
                slot,
                self.config.get("coastalWindSectors", {}),
            )
            self.db.execute(
                """
                INSERT INTO weather_process_states(
                    run_id,slot_utc,station_id,target_date,primary_observation_time_utc,
                    status,detected_processes_json,state_json,created_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(slot_utc,station_id,target_date) DO UPDATE SET
                    run_id=excluded.run_id,primary_observation_time_utc=excluded.primary_observation_time_utc,
                    status=excluded.status,detected_processes_json=excluded.detected_processes_json,
                    state_json=excluded.state_json,created_at_utc=excluded.created_at_utc
                """,
                (
                    run_id, iso_utc(slot), item["station_id"], item["target_date"],
                    state.get("primaryObservationTimeUtc"), state.get("status", "error"),
                    json.dumps(state.get("detectedProcesses") or [], ensure_ascii=False),
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")), iso_utc(),
                ),
            )
            process_count += 1
        self.db.commit()
        return remote_count, process_count

    def _due_unresolved_markets(self, now: datetime) -> list[sqlite3.Row]:
        lookback = (now.date() - timedelta(days=int(self.config["resolutionLookbackDays"]))).isoformat()
        rows = self.db.execute(
            """
            SELECT m.market_id, m.event_id, m.outcome_range, m.end_date_utc, e.target_date, e.city
            FROM markets m JOIN events e ON e.event_id=m.event_id
            LEFT JOIN market_resolutions r ON r.market_id=m.market_id
            WHERE e.target_date>=? AND COALESCE(r.is_resolved, 0)=0
              AND (m.end_date_utc<=? OR e.target_date<=?)
            """,
            (lookback, iso_utc(now), now.date().isoformat()),
        ).fetchall()
        return [
            row
            for row in rows
            if normalized_city(row["city"]) not in self.excluded_cities
            and (not self.fixed_cities or normalized_city(row["city"]) in self.fixed_cities)
        ]

    def _fetch_resolution(self, row: sqlite3.Row) -> tuple[sqlite3.Row, dict[str, Any]]:
        payload = self.client.get(GAMMA_MARKET_URL.format(market_id=row["market_id"]))
        if not isinstance(payload, dict):
            raise RuntimeError("Gamma market response is not an object")
        return row, payload

    def refresh_resolutions(self, run_id: int, now: datetime) -> int:
        due = self._due_unresolved_markets(now)
        if not due:
            return 0
        results: list[tuple[sqlite3.Row, dict[str, Any]]] = []
        workers = min(int(self.config["maxConcurrentRequests"]), len(due))
        with ThreadPoolExecutor(max_workers=max(workers, 1)) as executor:
            futures = {executor.submit(self._fetch_resolution, row): row for row in due}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    self._record_error(run_id, "resolution", row["market_id"], exc)
        updated = 0
        for row, payload in results:
            outcomes = [str(item) for item in parse_json_array(payload.get("outcomes"))]
            prices = [as_float(item) for item in parse_json_array(payload.get("outcomePrices"))]
            closed = bool(payload.get("closed"))
            winner_index = next((index for index, value in enumerate(prices) if value is not None and value >= 0.99), None)
            resolved = bool(closed and winner_index is not None)
            winner = (
                outcomes[winner_index]
                if resolved and winner_index is not None and winner_index < len(outcomes)
                else None
            )
            yes_index = next((index for index, value in enumerate(outcomes) if value.lower() == "yes"), 0)
            no_index = next((index for index, value in enumerate(outcomes) if value.lower() == "no"), 1)
            yes_price = prices[yes_index] if yes_index < len(prices) else None
            no_price = prices[no_index] if no_index < len(prices) else None
            checked_at = iso_utc()
            self.db.execute(
                """
                INSERT INTO market_resolutions(
                    market_id, event_id, checked_at_utc, resolved_at_utc, is_closed,
                    is_resolved, winning_outcome, yes_final_price, no_final_price, payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(market_id) DO UPDATE SET
                    checked_at_utc=excluded.checked_at_utc,
                    resolved_at_utc=COALESCE(excluded.resolved_at_utc, market_resolutions.resolved_at_utc),
                    is_closed=excluded.is_closed, is_resolved=excluded.is_resolved,
                    winning_outcome=excluded.winning_outcome,
                    yes_final_price=excluded.yes_final_price, no_final_price=excluded.no_final_price,
                    payload_json=excluded.payload_json
                """,
                (
                    row["market_id"],
                    row["event_id"],
                    checked_at,
                    checked_at if resolved else None,
                    int(closed),
                    int(resolved),
                    winner,
                    yes_price,
                    no_price,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            if resolved:
                updated += 1
                if str(winner).lower() == "yes":
                    self.db.execute(
                        """
                        UPDATE events SET resolved_at_utc=COALESCE(resolved_at_utc, ?),
                            winning_market_id=?, winning_range=? WHERE event_id=?
                        """,
                        (checked_at, row["market_id"], row["outcome_range"], row["event_id"]),
                    )
        self.db.commit()
        return updated

    def refresh_resolution_labels(self) -> int:
        """Materialize the official label separately from an independent station audit."""
        rows = self.db.execute(
            """
            SELECT e.event_id,e.city,e.station_id,e.target_date,e.resolved_at_utc,
                   e.winning_market_id,e.winning_range,e.rules,e.resolution_source,
                   m.bucket_low,m.bucket_high,m.bucket_unit,r.payload_json
            FROM events e
            LEFT JOIN markets m ON m.market_id=e.winning_market_id
            LEFT JOIN market_resolutions r ON r.market_id=e.winning_market_id
            WHERE e.resolved_at_utc IS NOT NULL AND e.winning_range IS NOT NULL
            """
        ).fetchall()
        now_text = iso_utc()
        updated = 0
        for row in rows:
            low, high = as_float(row["bucket_low"]), as_float(row["bucket_high"])
            unit = str(row["bucket_unit"] or "C").upper()
            exact = bool(low is not None and high is not None and abs(low - high) <= 1e-9)
            official_c = unit_to_celsius(low, unit) if exact and low is not None else None
            native_precision = resolution_precision(str(row["rules"] or ""))
            precision_c = native_precision * 5.0 / 9.0 if unit == "F" else native_precision
            audit = self.db.execute(
                """
                SELECT MAX(temperature_c) AS observed_max,
                       COUNT(DISTINCT observation_time_utc) AS observations
                FROM weather_observations
                WHERE station_id=? AND sample_local_date=? AND source='metar'
                  AND status='ok' AND temperature_c IS NOT NULL
                """,
                (row["station_id"], row["target_date"]),
            ).fetchone()
            observed_max = as_float(audit["observed_max"]) if audit else None
            rounded_audit = (
                round_to_precision(observed_max, precision_c)
                if observed_max is not None else None
            )
            delta = (
                rounded_audit - official_c
                if rounded_audit is not None and official_c is not None else None
            )
            status = (
                "exact_at_declared_precision" if exact
                else "censored_winning_bucket"
            )
            cursor = self.db.execute(
                """
                INSERT INTO weather_resolution_labels(
                    event_id,city,station_id,target_date,resolved_at_utc,winning_market_id,
                    winning_range,official_temperature_c,resolution_precision_c,
                    exact_at_resolution_precision,label_status,label_source,
                    resolution_source_url,station_observed_max_c,
                    station_observed_max_rounded_c,station_audit_delta_c,
                    station_observation_count,source_payload_json,created_at_utc,updated_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET
                    resolved_at_utc=excluded.resolved_at_utc,
                    winning_market_id=excluded.winning_market_id,
                    winning_range=excluded.winning_range,
                    official_temperature_c=excluded.official_temperature_c,
                    resolution_precision_c=excluded.resolution_precision_c,
                    exact_at_resolution_precision=excluded.exact_at_resolution_precision,
                    label_status=excluded.label_status,label_source=excluded.label_source,
                    resolution_source_url=excluded.resolution_source_url,
                    station_observed_max_c=excluded.station_observed_max_c,
                    station_observed_max_rounded_c=excluded.station_observed_max_rounded_c,
                    station_audit_delta_c=excluded.station_audit_delta_c,
                    station_observation_count=excluded.station_observation_count,
                    source_payload_json=excluded.source_payload_json,
                    updated_at_utc=excluded.updated_at_utc
                WHERE weather_resolution_labels.resolved_at_utc IS NOT excluded.resolved_at_utc
                   OR weather_resolution_labels.winning_market_id IS NOT excluded.winning_market_id
                   OR weather_resolution_labels.winning_range IS NOT excluded.winning_range
                   OR weather_resolution_labels.official_temperature_c IS NOT excluded.official_temperature_c
                   OR weather_resolution_labels.resolution_precision_c IS NOT excluded.resolution_precision_c
                   OR weather_resolution_labels.exact_at_resolution_precision IS NOT excluded.exact_at_resolution_precision
                   OR weather_resolution_labels.label_status IS NOT excluded.label_status
                   OR weather_resolution_labels.resolution_source_url IS NOT excluded.resolution_source_url
                   OR weather_resolution_labels.station_observed_max_c IS NOT excluded.station_observed_max_c
                   OR weather_resolution_labels.station_observed_max_rounded_c IS NOT excluded.station_observed_max_rounded_c
                   OR weather_resolution_labels.station_audit_delta_c IS NOT excluded.station_audit_delta_c
                   OR weather_resolution_labels.station_observation_count IS NOT excluded.station_observation_count
                   OR weather_resolution_labels.source_payload_json IS NOT excluded.source_payload_json
                """,
                (
                    row["event_id"], row["city"], row["station_id"], row["target_date"],
                    row["resolved_at_utc"], row["winning_market_id"], row["winning_range"],
                    official_c, precision_c, int(exact), status,
                    "polymarket_settlement_referencing_declared_resolution_source",
                    row["resolution_source"], observed_max, rounded_audit, delta,
                    int(audit["observations"] or 0) if audit else 0,
                    row["payload_json"], now_text, now_text,
                ),
            )
            updated += int(cursor.rowcount > 0)
        self.db.commit()
        return updated

    def _reports_due(self, settlement_changes: int, now: datetime) -> bool:
        """Throttle large historical exports while refreshing on new settlements."""
        if settlement_changes > 0:
            return True
        report_files = (
            "status_latest.json",
            "forecast_calibration_latest.json",
            "forecast_cutoff_status_latest.json",
            "shadow_ablation_latest.json",
        )
        paths = [self.report_dir / name for name in report_files]
        if any(not path.exists() for path in paths):
            return True
        interval = max(int(self.config.get("reportRefreshIntervalMinutes", 1440)), 30)
        stale_before = now.timestamp() - interval * 60
        return any(path.stat().st_mtime <= stale_before for path in paths)

    @staticmethod
    def _local_day_slots(
        day_text: str, timezone_name: str, now: datetime, interval_minutes: int = 60
    ) -> tuple[list[str], list[str]]:
        if interval_minutes <= 0 or 1440 % interval_minutes:
            raise ValueError("sample interval must be a positive divisor of 1440 minutes")
        local_tz = ZoneInfo(timezone_name)
        day = date.fromisoformat(day_text)
        start_local = datetime(day.year, day.month, day.day, tzinfo=local_tz)
        next_day = day + timedelta(days=1)
        end_local = datetime(next_day.year, next_day.month, next_day.day, tzinfo=local_tz)
        cursor = start_local.astimezone(UTC)
        end_utc = end_local.astimezone(UTC)
        all_slots: list[str] = []
        elapsed_slots: list[str] = []
        while cursor < end_utc:
            text = iso_utc(cursor)
            all_slots.append(text)
            if cursor <= now.astimezone(UTC):
                elapsed_slots.append(text)
            cursor += timedelta(minutes=interval_minutes)
        return all_slots, elapsed_slots

    def _write_coverage_report(self, now: datetime) -> list[dict[str, Any]]:
        since = (now.date() - timedelta(days=16)).isoformat()
        rows = self.db.execute(
            """
            SELECT w.station_id, s.station_name, s.first_seen_utc, w.timezone, w.sample_local_date,
                   GROUP_CONCAT(DISTINCT w.slot_utc) AS slots,
                   COUNT(DISTINCT CASE WHEN w.status='ok' THEN w.slot_utc END) AS ok_rows,
                   COUNT(DISTINCT w.slot_utc) AS total_rows
            FROM windy_forecasts w JOIN stations s ON s.station_id=w.station_id
            WHERE w.sample_local_date>=?
            GROUP BY w.station_id, w.timezone, w.sample_local_date
            ORDER BY w.sample_local_date DESC, w.station_id
            """,
            (since,),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            try:
                full_slots, elapsed_slots = self._local_day_slots(
                    row["sample_local_date"],
                    row["timezone"],
                    now,
                    int(self.config.get("sampleIntervalMinutes", 60)),
                )
                tracking_start = interval_floor(
                    datetime.fromisoformat(row["first_seen_utc"]),
                    int(self.config.get("sampleIntervalMinutes", 60)),
                )
            except (ValueError, ZoneInfoNotFoundError):
                continue
            captured = set((row["slots"] or "").split(",")) - {""}
            expected = elapsed_slots if row["sample_local_date"] == now.astimezone(ZoneInfo(row["timezone"])).date().isoformat() else full_slots
            expected = [slot for slot in expected if datetime.fromisoformat(slot) >= tracking_start]
            missing = [slot for slot in expected if slot not in captured]
            output.append(
                {
                    "station_id": row["station_id"],
                    "station_name": row["station_name"],
                    "timezone": row["timezone"],
                    "local_date": row["sample_local_date"],
                    "tracking_started_at_utc": row["first_seen_utc"],
                    "full_day_eligible": bool(full_slots and datetime.fromisoformat(full_slots[0]) >= tracking_start),
                    "captured_slots": len(captured),
                    "expected_elapsed_slots": len(expected),
                    "expected_full_day_slots": len(full_slots),
                    "ok_rows": int(row["ok_rows"] or 0),
                    "total_rows": int(row["total_rows"] or 0),
                    "missing_elapsed_slots": missing,
                    "coverage_complete_so_far": not missing,
                }
            )
        path = self.report_dir / "coverage_latest.csv"
        fields = [
            "station_id",
            "station_name",
            "timezone",
            "local_date",
            "tracking_started_at_utc",
            "full_day_eligible",
            "captured_slots",
            "expected_elapsed_slots",
            "expected_full_day_slots",
            "ok_rows",
            "total_rows",
            "coverage_complete_so_far",
            "missing_elapsed_slots",
        ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for item in output:
                writer.writerow({**item, "missing_elapsed_slots": json.dumps(item["missing_elapsed_slots"])})
        return output

    @staticmethod
    def _lead_band(hours: float) -> str:
        if hours < 0:
            return "after_target_day"
        if hours <= 6:
            return "0-6h"
        if hours <= 12:
            return "6-12h"
        if hours <= 24:
            return "12-24h"
        if hours <= 48:
            return "24-48h"
        return "48h+"

    def _write_accuracy_report(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT e.event_id, e.title, e.city, e.target_date, e.station_id, e.winning_range, e.rules,
                   e.resolved_at_utc, m.bucket_low, m.bucket_high, m.bucket_unit,
                   w.slot_utc, w.timezone, w.model_ref_time_utc, w.model_updated_at_utc,
                   w.forecast_max_c, w.forecast_max_f
            FROM events e
            JOIN markets m ON m.market_id=e.winning_market_id
            JOIN windy_forecasts w ON w.station_id=e.station_id AND w.target_date=e.target_date
            WHERE e.resolved_at_utc IS NOT NULL AND w.status='ok' AND w.forecast_max_c IS NOT NULL
            ORDER BY e.target_date, e.event_id, w.slot_utc
            """
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            try:
                local_tz = ZoneInfo(row["timezone"])
                target = date.fromisoformat(row["target_date"])
                next_day = target + timedelta(days=1)
                target_end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=local_tz).astimezone(UTC)
                sampled = datetime.fromisoformat(row["slot_utc"])
            except (ValueError, ZoneInfoNotFoundError):
                continue
            predicted_raw = celsius_to_unit(float(row["forecast_max_c"]), row["bucket_unit"])
            precision = resolution_precision(row["rules"])
            predicted = round_to_precision(predicted_raw, precision)
            remaining = (target_end - sampled).total_seconds() / 3600.0
            output.append(
                {
                    "event_id": row["event_id"],
                    "title": row["title"],
                    "city": row["city"],
                    "station_id": row["station_id"],
                    "target_date": row["target_date"],
                    "sample_slot_utc": row["slot_utc"],
                    "lead_hours_to_day_end": round(remaining, 3),
                    "lead_band": self._lead_band(remaining),
                    "model_ref_time_utc": row["model_ref_time_utc"],
                    "model_updated_at_utc": row["model_updated_at_utc"],
                    "forecast_max_c": row["forecast_max_c"],
                    "forecast_max_f": row["forecast_max_f"],
                    "resolution_precision": precision,
                    "forecast_raw_in_market_unit": round(predicted_raw, 3),
                    "forecast_in_market_unit": round(predicted, 3),
                    "winning_range": row["winning_range"],
                    "bucket_low": row["bucket_low"],
                    "bucket_high": row["bucket_high"],
                    "bucket_unit": row["bucket_unit"],
                    "bucket_hit": value_in_bucket(predicted, row["bucket_low"], row["bucket_high"]),
                    "distance_to_winning_bucket": round(
                        distance_to_bucket(predicted, row["bucket_low"], row["bucket_high"]), 3
                    ),
                }
            )
        fields = list(output[0].keys()) if output else [
            "event_id",
            "title",
            "city",
            "station_id",
            "target_date",
            "sample_slot_utc",
            "lead_hours_to_day_end",
            "lead_band",
            "forecast_max_c",
            "forecast_max_f",
            "winning_range",
            "bucket_hit",
            "distance_to_winning_bucket",
        ]
        with (self.report_dir / "accuracy_latest.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(output)
        by_band: dict[str, dict[str, Any]] = {}
        for item in output:
            bucket = by_band.setdefault(item["lead_band"], {"samples": 0, "hits": 0, "distance_sum": 0.0})
            bucket["samples"] += 1
            bucket["hits"] += int(item["bucket_hit"])
            bucket["distance_sum"] += float(item["distance_to_winning_bucket"])
        for bucket in by_band.values():
            bucket["hit_rate"] = bucket["hits"] / bucket["samples"] if bucket["samples"] else None
            bucket["mean_distance_to_bucket"] = bucket["distance_sum"] / bucket["samples"] if bucket["samples"] else None
            bucket.pop("distance_sum", None)
        summary = {
            "resolved_events": len({item["event_id"] for item in output}),
            "forecast_samples": len(output),
            "overall_hit_rate": (
                sum(int(item["bucket_hit"]) for item in output) / len(output) if output else None
            ),
            "by_lead_band": by_band,
        }
        return output, summary

    def _write_daily_accuracy_report(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT e.event_id, e.title, e.city, e.target_date, e.station_id, e.station_name,
                   e.rules, e.resolved_at_utc, e.winning_range,
                   m.bucket_low, m.bucket_high, m.bucket_unit,
                   w.slot_utc, w.timezone, w.model, w.model_ref_time_utc,
                   w.model_updated_at_utc, w.forecast_max_c, w.forecast_peak_local
            FROM events e
            JOIN windy_forecasts w ON w.station_id=e.station_id AND w.target_date=e.target_date
            LEFT JOIN markets m ON m.market_id=e.winning_market_id
            WHERE w.model='mblue' AND w.status='ok' AND w.forecast_max_c IS NOT NULL
            ORDER BY e.target_date DESC, e.event_id, w.slot_utc
            """
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            if normalized_city(row["city"]) in self.excluded_cities:
                continue
            if self.fixed_cities and normalized_city(row["city"]) not in self.fixed_cities:
                continue
            grouped.setdefault(row["event_id"], []).append(row)

        output: list[dict[str, Any]] = []
        for event_rows in grouped.values():
            first = event_rows[0]
            try:
                local_tz = ZoneInfo(first["timezone"])
                target = date.fromisoformat(first["target_date"])
                canonical_local = datetime(target.year, target.month, target.day, 7, 30, tzinfo=local_tz)
                canonical_utc = canonical_local.astimezone(UTC)
                chosen = min(
                    event_rows,
                    key=lambda row: abs(
                        (datetime.fromisoformat(row["slot_utc"]) - canonical_utc).total_seconds()
                    ),
                )
                sample_local = datetime.fromisoformat(chosen["slot_utc"]).astimezone(local_tz)
            except (ValueError, ZoneInfoNotFoundError):
                continue

            forecast_c = float(chosen["forecast_max_c"])
            unit = chosen["bucket_unit"] or "C"
            precision = resolution_precision(chosen["rules"])
            forecast_market_raw = celsius_to_unit(forecast_c, unit)
            forecast_market = round_to_precision(forecast_market_raw, precision)
            low = as_float(chosen["bucket_low"])
            high = as_float(chosen["bucket_high"])
            resolved = bool(chosen["resolved_at_utc"] and chosen["winning_range"])
            exact_settlement = bool(resolved and low is not None and high is not None and low == high)
            accurate = value_in_bucket(forecast_market, low, high) if resolved else None
            distance_market = distance_to_bucket(forecast_market, low, high) if resolved else None
            distance_c = (
                distance_market * 5.0 / 9.0 if distance_market is not None and unit == "F" else distance_market
            )
            settlement_value = low if exact_settlement else None
            settlement_c = unit_to_celsius(settlement_value, unit) if settlement_value is not None else None
            signed_error_c = forecast_c - settlement_c if settlement_c is not None else None
            output.append(
                {
                    "target_date": chosen["target_date"],
                    "city": chosen["city"],
                    "event_id": chosen["event_id"],
                    "station_id": chosen["station_id"],
                    "station_name": chosen["station_name"],
                    "model": chosen["model"],
                    "canonical_local_time": canonical_local.isoformat(timespec="seconds"),
                    "sample_slot_utc": chosen["slot_utc"],
                    "sample_local_time": sample_local.isoformat(timespec="seconds"),
                    "sample_offset_from_0730": round(
                        (sample_local - canonical_local).total_seconds() / 3600.0, 3
                    ),
                    "forecast_max_c": round(forecast_c, 3),
                    "forecast_peak_local": chosen["forecast_peak_local"],
                    "forecast_in_market_unit": round(forecast_market, 3),
                    "market_unit": unit,
                    "resolution_status": "resolved" if resolved else "pending",
                    "winning_range": chosen["winning_range"],
                    "settlement_exact": exact_settlement,
                    "settlement_value_market_unit": settlement_value,
                    "settlement_value_c": round(settlement_c, 3) if settlement_c is not None else None,
                    "accuracy": "accurate" if accurate is True else "inaccurate" if accurate is False else "pending",
                    "signed_error_c": round(signed_error_c, 3) if signed_error_c is not None else None,
                    "absolute_error_c": round(abs(signed_error_c), 3) if signed_error_c is not None else None,
                    "minimum_distance_to_winning_range_c": (
                        round(float(distance_c), 3) if distance_c is not None else None
                    ),
                    "difference_kind": "exact" if exact_settlement else "minimum_to_range" if resolved else "pending",
                    "resolved_at_utc": chosen["resolved_at_utc"],
                }
            )

        output.sort(key=lambda item: (item["target_date"], item["city"]), reverse=True)
        fields = list(output[0].keys()) if output else [
            "target_date", "city", "event_id", "station_id", "model", "sample_local_time",
            "forecast_max_c", "resolution_status", "winning_range", "accuracy",
            "absolute_error_c", "minimum_distance_to_winning_range_c", "difference_kind",
        ]
        with (self.report_dir / "daily_accuracy_latest.csv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(output)

        resolved_rows = [item for item in output if item["resolution_status"] == "resolved"]
        exact_rows = [item for item in resolved_rows if item["absolute_error_c"] is not None]
        summary = {
            "records": len(output),
            "resolved": len(resolved_rows),
            "pending": len(output) - len(resolved_rows),
            "accurate": sum(item["accuracy"] == "accurate" for item in resolved_rows),
            "inaccurate": sum(item["accuracy"] == "inaccurate" for item in resolved_rows),
            "exact_error_records": len(exact_rows),
            "mean_absolute_error_c": (
                sum(float(item["absolute_error_c"]) for item in exact_rows) / len(exact_rows)
                if exact_rows
                else None
            ),
        }
        (self.report_dir / "daily_accuracy_latest.json").write_text(
            json.dumps({"generated_at_utc": iso_utc(), "summary": summary, "records": output}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return output, summary

    def _write_resolution_report(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT e.event_id, e.title, e.city, e.target_date, e.station_id,
                   m.market_id, m.outcome_range, r.checked_at_utc, r.resolved_at_utc,
                   r.is_closed, r.is_resolved, r.winning_outcome,
                   CASE WHEN r.is_resolved=1 THEN r.yes_final_price END AS yes_final_price,
                   CASE WHEN r.is_resolved=1 THEN r.no_final_price END AS no_final_price
            FROM market_resolutions r
            JOIN markets m ON m.market_id=r.market_id
            JOIN events e ON e.event_id=r.event_id
            ORDER BY e.target_date, e.city, m.bucket_low, m.bucket_high
            """
        ).fetchall()
        output = [dict(row) for row in rows]
        fields = [
            "event_id", "title", "city", "target_date", "station_id", "market_id",
            "outcome_range", "checked_at_utc", "resolved_at_utc", "is_closed",
            "is_resolved", "winning_outcome", "yes_final_price", "no_final_price",
        ]
        with (self.report_dir / "resolutions_latest.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(output)
        return output

    def _latest_structure(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT er.rank, er.volume_24h, er.liquidity, er.selection_score,
                   e.event_id, e.slug, e.title, e.city, e.target_date, e.station_id,
                   e.station_name, e.resolution_source, s.timezone, s.windy_url,
                   ms.market_id, ms.outcome_range, ms.gamma_yes_price, ms.gamma_no_price,
                   ms.yes_best_bid, ms.yes_best_ask, ms.no_best_bid, ms.no_best_ask
            FROM event_rankings er JOIN events e ON e.event_id=er.event_id
            LEFT JOIN stations s ON s.station_id=e.station_id
            LEFT JOIN market_snapshots ms ON ms.run_id=er.run_id AND ms.event_id=e.event_id
            WHERE er.run_id=? ORDER BY er.rank, ms.outcome_range
            """,
            (run_id,),
        ).fetchall()
        events: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = events.setdefault(
                row["event_id"],
                {
                    "rank": row["rank"],
                    "event_id": row["event_id"],
                    "slug": row["slug"],
                    "title": row["title"],
                    "city": row["city"],
                    "target_date": row["target_date"],
                    "volume_24h": row["volume_24h"],
                    "liquidity": row["liquidity"],
                    "selection_score": row["selection_score"],
                    "station_id": row["station_id"],
                    "station_name": row["station_name"],
                    "timezone": row["timezone"],
                    "resolution_source": row["resolution_source"],
                    "windy_url": row["windy_url"],
                    "temperature_buckets": [],
                },
            )
            if row["market_id"]:
                item["temperature_buckets"].append(
                    {
                        "market_id": row["market_id"],
                        "range": row["outcome_range"],
                        "gamma_yes": row["gamma_yes_price"],
                        "gamma_no": row["gamma_no_price"],
                        "yes_bid": row["yes_best_bid"],
                        "yes_ask": row["yes_best_ask"],
                        "no_bid": row["no_best_bid"],
                        "no_ask": row["no_best_ask"],
                    }
                )
        return sorted(events.values(), key=lambda item: item["rank"])

    def _write_data_quality_report(self, run_id: int) -> dict[str, Any]:
        run = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        events = self.db.execute(
            """
            SELECT er.rank,e.event_id,e.city,e.target_date,e.station_id,s.station_name,s.timezone
            FROM event_rankings er JOIN events e ON e.event_id=er.event_id
            JOIN stations s ON s.station_id=e.station_id
            WHERE er.run_id=?
            ORDER BY er.rank
            """,
            (run_id,),
        ).fetchall()
        expected_models = list(self.config.get("externalForecastModels", []))
        cities: list[dict[str, Any]] = []
        for event in events:
            model_rows = self.db.execute(
                """
                SELECT model,status,forecast_max_c,slot_utc FROM external_forecasts
                WHERE run_id=? AND station_id=? AND target_date=? ORDER BY model
                """,
                (run_id, event["station_id"], event["target_date"]),
            ).fetchall()
            model_by_name = {row["model"]: row for row in model_rows}
            mblue = self.db.execute(
                """
                SELECT status,forecast_max_c,slot_utc FROM windy_forecasts
                WHERE run_id=? AND station_id=? AND target_date=? LIMIT 1
                """,
                (run_id, event["station_id"], event["target_date"]),
            ).fetchone()
            observation_rows = self.db.execute(
                """
                SELECT source,status,temperature_c,observed_daily_max_c,observation_time_utc,slot_utc
                FROM weather_observations WHERE run_id=? AND station_id=?
                """,
                (run_id, event["station_id"]),
            ).fetchall()
            observations = {row["source"]: dict(row) for row in observation_rows}
            market_count = int(
                self.db.execute(
                    "SELECT COUNT(*) FROM market_snapshots WHERE run_id=? AND event_id=?",
                    (run_id, event["event_id"]),
                ).fetchone()[0]
            )
            raw_count = int(
                self.db.execute(
                    "SELECT COUNT(*) FROM raw_weather_payloads WHERE run_id=? AND station_id=? AND status='ok'",
                    (run_id, event["station_id"]),
                ).fetchone()[0]
            )
            missing: list[str] = []
            if not mblue or mblue["status"] != "ok":
                missing.append("meteoblue")
            for model in expected_models:
                row = model_by_name.get(model)
                if not row or row["status"] != "ok":
                    missing.append(model)
            for source in ("open_meteo_current", "metar"):
                if source not in observations or observations[source]["status"] != "ok":
                    missing.append(source)
            if market_count == 0:
                missing.append("market")
            if raw_count < 3:
                missing.append("raw_payloads")
            maxima = [as_float(row["forecast_max_c"]) for row in model_rows if row["status"] == "ok"]
            maxima = [value for value in maxima if value is not None]
            cities.append(
                {
                    "rank": event["rank"], "event_id": event["event_id"], "city": event["city"],
                    "station_id": event["station_id"], "station_name": event["station_name"],
                    "timezone": event["timezone"], "target_date": event["target_date"],
                    "market_snapshots": market_count,
                    "meteoblue_max_c": as_float(mblue["forecast_max_c"]) if mblue else None,
                    "external_models_ok": len(maxima), "external_models_expected": len(expected_models),
                    "external_model_min_c": min(maxima) if maxima else None,
                    "external_model_max_c": max(maxima) if maxima else None,
                    "open_meteo_current_c": as_float(observations.get("open_meteo_current", {}).get("temperature_c")),
                    "metar_current_c": as_float(observations.get("metar", {}).get("temperature_c")),
                    "observed_daily_max_c": max(
                        (as_float(row.get("observed_daily_max_c")) for row in observations.values()),
                        default=None,
                    ),
                    "raw_payloads_ok": raw_count,
                    "complete": not missing,
                    "missing": missing,
                }
            )
        complete = sum(item["complete"] for item in cities)
        payload = {
            "generated_at_utc": iso_utc(), "run_id": run_id, "slot_utc": run["slot_utc"] if run else None,
            "summary": {
                "cities": len(cities), "complete_cities": complete,
                "incomplete_cities": len(cities) - complete,
                "external_models_expected_per_city": len(expected_models),
                "raw_payloads": int(self.db.execute(
                    "SELECT COUNT(*) FROM raw_weather_payloads WHERE run_id=?", (run_id,)
                ).fetchone()[0]),
            },
            "cities": cities,
        }
        (self.report_dir / "data_quality_latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return payload

    def write_reports(self, run_id: int | None = None) -> None:
        now = utc_now()
        if run_id is None:
            row = self.db.execute("SELECT run_id FROM runs ORDER BY slot_utc DESC LIMIT 1").fetchone()
            run_id = int(row["run_id"]) if row else 0
        coverage = self._write_coverage_report(now)
        _, accuracy = self._write_accuracy_report()
        daily_accuracy, daily_accuracy_summary = self._write_daily_accuracy_report()
        resolutions = self._write_resolution_report()
        data_quality = self._write_data_quality_report(run_id) if run_id else {
            "summary": {"cities": 0, "complete_cities": 0, "incomplete_cities": 0}, "cities": []
        }
        latest_structure = self._latest_structure(run_id) if run_id else []
        latest_forecasts = [
            dict(row)
            for row in self.db.execute(
                """
                SELECT w.station_id, s.station_name, s.city, w.target_date, w.slot_utc,
                       w.model, w.model_ref_time_utc, w.forecast_step_hours,
                       w.forecast_max_c, w.forecast_max_f,
                       w.forecast_peak_local, w.status
                FROM windy_forecasts w JOIN stations s ON s.station_id=w.station_id
                WHERE w.slot_utc=(SELECT MAX(slot_utc) FROM windy_forecasts)
                ORDER BY w.station_id, w.target_date
                """
            ).fetchall()
        ]
        counts = {
            name: int(self.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in (
                "runs", "events", "markets", "market_snapshots", "windy_forecasts",
                "external_forecasts", "ensemble_forecasts", "forecast_model_runs",
                "forecast_cutoff_snapshots",
                "weather_observations", "raw_weather_payloads", "market_resolutions",
                "weather_resolution_labels",
                "weather_forecast_evaluations",
            )
        }
        latest_run = self.db.execute("SELECT * FROM runs ORDER BY slot_utc DESC LIMIT 1").fetchone()
        status = {
            "generated_at_utc": iso_utc(now),
            "database": str(self.db_path),
            "latest_run": dict(latest_run) if latest_run else None,
            "counts": counts,
            "coverage": {
                "rows": len(coverage),
                "incomplete_elapsed_days": sum(not item["coverage_complete_so_far"] for item in coverage),
                "latest": coverage[:50],
            },
            "accuracy": accuracy,
            "daily_accuracy": {
                "summary": daily_accuracy_summary,
                "latest": daily_accuracy[:100],
            },
            "data_quality": data_quality,
            "resolution_rows": len(resolutions),
            "forecast_evaluations": int(self.db.execute(
                "SELECT COUNT(*) FROM weather_forecast_evaluations"
            ).fetchone()[0]),
            "latest_forecasts": latest_forecasts,
        }
        (self.report_dir / "status_latest.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (self.report_dir / "market_structure_latest.json").write_text(
            json.dumps(
                {"generated_at_utc": iso_utc(now), "run_id": run_id, "events": latest_structure},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def run_once(self, force: bool = False) -> dict[str, Any]:
        # A prior failed write must never leak a transaction into the next
        # scheduled slot. This is safe when no transaction is active.
        self.db.rollback()
        started = utc_now()
        stage_durations: dict[str, float] = {}
        # Preserve the real collection minute. Scheduled loop runs land on :30;
        # ad-hoc starts must not be mislabeled as canonical 07:30 snapshots.
        slot = started.astimezone(UTC).replace(second=0, microsecond=0)
        full_interval = int(self.config.get("sampleIntervalMinutes", 30))
        fast_interval = int(self.config.get("fastSampleIntervalMinutes", full_interval))
        fast_enabled = fast_interval < full_interval
        full_collection = not fast_enabled or slot.minute % full_interval == 0
        run_id, already_completed = self._begin_run(slot, force)
        selected_events = market_count = windy_count = external_count = ensemble_count = observation_count = network_count = 0
        cutoff_snapshot_count = cma_meso_count = 0
        remote_count = process_count = resolution_count = 0
        if already_completed:
            previous = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if previous:
                selected_events = int(previous["selected_events"] or 0)
                market_count = int(previous["market_snapshots"] or 0)
                windy_count = int(previous["windy_snapshots"] or 0)
                external_count = int(previous["external_forecast_snapshots"] or 0)
                ensemble_count = int(previous["ensemble_forecast_snapshots"] or 0)
                observation_count = int(previous["observation_snapshots"] or 0)
                network_count = int(previous["station_network_reports"] or 0)
                remote_count = int(previous["remote_sensing_snapshots"] or 0)
                process_count = int(previous["weather_process_states"] or 0)
                resolution_count = int(previous["resolutions_updated"] or 0)
        message = None
        try:
            if not already_completed:
                stage_started = time.monotonic()
                top_events = self.discover_top_events()
                stage_durations["discovery"] = round(time.monotonic() - stage_started, 3)
                if full_collection:
                    selected_events = len(top_events)
                    capture_events = self.include_tracked_events(run_id, top_events, started)
                else:
                    fast_cities = {
                        normalized_city(city)
                        for city in self.config.get("fastAnalysisCities", self.config.get("processAnalysisCities", []))
                    }
                    capture_events = [
                        event for event in top_events
                        if normalized_city(city_from_title(str(event.get("title") or ""))) in fast_cities
                    ]
                    selected_events = len(capture_events)
                stage_started = time.monotonic()
                market_count = self.capture_market_structure(run_id, slot, capture_events)
                stage_durations["markets"] = round(time.monotonic() - stage_started, 3)
                stage_started = time.monotonic()
                if full_collection:
                    windy_count = self.capture_windy(run_id, slot)
                    external_count, observation_count, network_count = self.capture_external_weather(
                        run_id, slot, include_forecasts=True
                    )
                    cma_meso_count = self.capture_cma_meso(run_id, slot)
                    ensemble_count = self.capture_ensemble_forecasts(run_id, slot)
                else:
                    external_count, observation_count, network_count = self.capture_external_weather(
                        run_id, slot, fast_cities, include_forecasts=False
                    )
                stage_durations["forecast_and_observations"] = round(
                    time.monotonic() - stage_started, 3
                )
                stage_started = time.monotonic()
                remote_count, process_count = self.capture_remote_sensing_and_process(run_id, slot)
                stage_durations["remote_sensing_and_process"] = round(
                    time.monotonic() - stage_started, 3
                )
                stage_started = time.monotonic()
                shadow_cities = {
                    normalized_city(city)
                    for city in self.config.get("processAnalysisCities", [])
                    if str(city).strip()
                }
                capture_shadow_sources(
                    self.db, slot, self._tracked_station_dates(slot, shadow_cities)
                )
                stage_durations["shadow_sources"] = round(time.monotonic() - stage_started, 3)
                stage_started = time.monotonic()
                cutoff_snapshot_count = self.forecast_cutoff_tracker.capture_due(slot)
                stage_durations["forecast_cutoffs"] = round(
                    time.monotonic() - stage_started, 3
                )
                if not full_collection:
                    message = "fast leading-signal collection"
            else:
                message = "collection slot already completed; skipped duplicate sampling"
            evaluation_rows = 0
            cutoff_evaluation_rows = 0
            resolution_labels = 0
            if full_collection:
                stage_started = time.monotonic()
                resolution_count += self.refresh_resolutions(run_id, started)
                resolution_labels = self.refresh_resolution_labels()
                evaluation_rows = self.forecast_evaluator.refresh()
                cutoff_evaluation_rows = self.forecast_cutoff_tracker.refresh_evaluations()
                stage_durations["resolution_and_evaluation"] = round(
                    time.monotonic() - stage_started, 3
                )
            error_count = self.db.execute("SELECT COUNT(*) FROM collection_errors WHERE run_id=?", (run_id,)).fetchone()[0]
            self.db.execute(
                """
                UPDATE runs SET completed_at_utc=?, status='completed', selected_events=?,
                    market_snapshots=?, windy_snapshots=?, external_forecast_snapshots=?,
                    ensemble_forecast_snapshots=?,
                    observation_snapshots=?, station_network_reports=?, remote_sensing_snapshots=?,
                    weather_process_states=?, resolutions_updated=?, errors=?, message=?
                WHERE run_id=?
                """,
                (
                    iso_utc(),
                    selected_events,
                    market_count,
                    windy_count,
                    external_count,
                    ensemble_count,
                    observation_count,
                    network_count,
                    remote_count,
                    process_count,
                    resolution_count,
                    int(error_count),
                    message,
                    run_id,
                ),
            )
            self.db.commit()
            settlement_changes = resolution_count + resolution_labels + cutoff_evaluation_rows
            reports_written = bool(
                full_collection and self._reports_due(settlement_changes, started)
            )
            if reports_written:
                self.forecast_evaluator.write_reports()
                self.write_reports(run_id)
                self.forecast_cutoff_tracker.write_reports()
                write_ablation_report(self.config, evaluate_shadow_ablation(self.db))
        except Exception as exc:
            self.db.rollback()
            try:
                self._record_error(run_id, "run", None, exc)
            except sqlite3.OperationalError:
                logging.exception("could not persist collection error after rollback")
                self.db.rollback()
            self.db.execute(
                "UPDATE runs SET completed_at_utc=?, status='failed', message=? WHERE run_id=?",
                (iso_utc(), str(exc)[:2000], run_id),
            )
            self.db.commit()
            raise
        result = {
            "run_id": run_id,
            "slot_utc": iso_utc(slot),
            "selected_events": selected_events,
            "market_snapshots": market_count,
            "windy_snapshots": windy_count,
            "external_forecast_snapshots": external_count,
            "cma_meso_forecast_snapshots": cma_meso_count,
            "ensemble_forecast_snapshots": ensemble_count,
            "observation_snapshots": observation_count,
            "station_network_reports": network_count,
            "remote_sensing_snapshots": remote_count,
            "weather_process_states": process_count,
            "resolutions_updated": resolution_count,
            "forecast_evaluations": evaluation_rows,
            "forecast_cutoff_evaluations": cutoff_evaluation_rows,
            "forecast_cutoff_snapshots": cutoff_snapshot_count,
            "duration_seconds": round((utc_now() - started).total_seconds(), 3),
            "stage_seconds": stage_durations,
            "message": message,
            "collection_mode": "full" if full_collection else "fast",
            "reports_written": reports_written,
        }
        logging.info("run completed: %s", json.dumps(result, ensure_ascii=False))
        return result


def configure_logging(config: dict[str, Any]) -> None:
    log_path = ROOT / config["logPath"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=int(config.get("logMaxBytes", 5 * 1024 * 1024)),
        backupCount=int(config.get("logBackupCount", 2)),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def next_sample_time(now: datetime, interval_minutes: int) -> datetime:
    current = now.astimezone(UTC)
    return interval_floor(current, interval_minutes) + timedelta(minutes=interval_minutes)


def sleep_until_next_slot(interval_minutes: int, stop_requested: Callable[[], bool]) -> None:
    target = next_sample_time(utc_now(), interval_minutes)
    while not stop_requested():
        remaining = (target - utc_now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket weather and Windy meteogram monitor")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one collection")
    mode.add_argument("--loop", action="store_true", help="run continuously at the configured interval")
    mode.add_argument("--report", action="store_true", help="regenerate reports without network collection")
    parser.add_argument("--force", action="store_true", help="overwrite the current collection slot")
    parser.add_argument("--limit-events", type=int, help="temporary event limit for a smoke test")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    configure_logging(config)
    if args.report:
        monitor = WeatherMarketMonitor(config, args.limit_events)
        try:
            monitor.forecast_evaluator.refresh()
            monitor.forecast_cutoff_tracker.refresh_evaluations()
            monitor.forecast_evaluator.write_reports()
            monitor.write_reports()
            monitor.forecast_cutoff_tracker.write_reports()
            logging.info("reports regenerated")
            return 0
        finally:
            monitor.close()

    lock_path = ROOT / "data/weather_market_monitor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("w")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.info("another weather market monitor process is already running")
        return 0

    monitor = WeatherMarketMonitor(config, args.limit_events)
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        if not args.loop:
            result = monitor.run_once(force=args.force)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        while not stop:
            try:
                monitor.run_once(force=False)
            except Exception:
                logging.exception("scheduled collection failed")
            sleep_until_next_slot(
                int(config.get("fastSampleIntervalMinutes", config["sampleIntervalMinutes"])),
                lambda: stop,
            )
        return 0
    finally:
        monitor.close()
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
