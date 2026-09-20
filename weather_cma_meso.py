#!/usr/bin/env python3
"""Config-driven adapter for authenticated CMA-MESO point forecasts."""

from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


UTC = timezone.utc


def nested_value(payload: Any, path: str) -> Any:
    current = payload
    for token in [part for part in path.split(".") if part]:
        if isinstance(current, dict):
            current = current.get(token)
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_provider_time(value: Any, timezone_name: str) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    compact_formats = {
        10: "%Y%m%d%H",
        12: "%Y%m%d%H%M",
        14: "%Y%m%d%H%M%S",
    }
    try:
        if text.isdigit() and len(text) in compact_formats:
            parsed = datetime.strptime(text, compact_formats[len(text)])
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except Exception:
            return None
    return parsed.astimezone(UTC)


class CmaMesoAdapter:
    """Read a declared CMA interface without assuming a private response schema."""

    def __init__(self, config: dict[str, Any], getter: Callable[..., Any]):
        self.config = config
        self.getter = getter
        self.credentials: dict[str, Any] | None = None
        self.mapping: dict[str, Any] | None = None
        self.error: str | None = None
        self._load()

    def _load(self) -> None:
        cma = self.config.get("cma") if isinstance(self.config.get("cma"), dict) else {}
        path = Path(os.path.expanduser(str(
            cma.get("credentialFile", "~/.config/weather-market-monitor/cma_api.json")
        )))
        if not path.exists():
            self.error = "CMA credential file is missing"
            return
        if path.stat().st_mode & 0o077:
            self.error = "CMA credential file must have mode 600"
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.error = f"CMA credential file is unreadable: {exc}"
            return
        products = payload.get("products") if isinstance(payload, dict) else None
        mapping = products.get("cma_meso") if isinstance(products, dict) else None
        response = mapping.get("response") if isinstance(mapping, dict) else None
        required = ("interfaceId",)
        response_required = ("rowsPath", "validTimeField", "temperatureField")
        if not isinstance(mapping, dict) or any(not mapping.get(key) for key in required):
            self.error = "CMA-MESO interfaceId mapping is missing"
            return
        if not isinstance(response, dict) or any(not response.get(key) for key in response_required):
            self.error = "CMA-MESO response field mapping is incomplete"
            return
        self.credentials = payload
        self.mapping = mapping

    @property
    def configured(self) -> bool:
        return self.credentials is not None and self.mapping is not None

    @staticmethod
    def _format(value: Any, context: dict[str, Any]) -> Any:
        return value.format(**context) if isinstance(value, str) else value

    def fetch(
        self,
        *,
        station_id: str,
        latitude: float,
        longitude: float,
        target_date: str,
        timezone_name: str,
    ) -> tuple[Any, dict[str, Any]]:
        if not self.configured:
            raise RuntimeError(self.error or "CMA-MESO is not configured")
        assert self.credentials is not None and self.mapping is not None
        cma = self.config.get("cma") if isinstance(self.config.get("cma"), dict) else {}
        context = {
            "stationId": station_id,
            "latitude": latitude,
            "longitude": longitude,
            "targetDate": target_date,
            "targetDateCompact": target_date.replace("-", ""),
            "timezone": timezone_name,
        }
        params = {
            key: self._format(value, context)
            for key, value in dict(self.mapping.get("params") or {}).items()
        }
        params.update({
            "userId": self.credentials.get("userId"),
            "pwd": self.credentials.get("pwd"),
            "dataFormat": params.get("dataFormat", "json"),
            "interfaceId": self.mapping["interfaceId"],
        })
        payload = self.getter(
            str(cma.get("apiBaseUrl", "http://api.data.cma.cn:8090/api")),
            params,
            error_label=f"CMA-MESO {station_id} {target_date}",
        )
        return payload, self.parse(payload, target_date, timezone_name)

    def parse(self, payload: Any, target_date: str, station_timezone: str) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError(self.error or "CMA-MESO is not configured")
        assert self.mapping is not None
        response = self.mapping["response"]
        rows = nested_value(payload, str(response["rowsPath"]))
        if not isinstance(rows, list):
            raise RuntimeError("CMA-MESO rowsPath did not resolve to a list")
        source_timezone = str(response.get("timeZone") or "UTC")
        target = date.fromisoformat(target_date)
        records: list[tuple[dict[str, Any], datetime | None]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            valid = parse_provider_time(row.get(response["validTimeField"]), source_timezone)
            temperature = finite_float(row.get(response["temperatureField"]))
            if valid is None or temperature is None:
                continue
            unit = str(response.get("temperatureUnit") or "C").upper()
            if unit == "K":
                temperature -= 273.15
            elif unit == "F":
                temperature = (temperature - 32.0) * 5.0 / 9.0
            local = valid.astimezone(ZoneInfo(station_timezone))
            if local.date() != target:
                continue
            point = {
                "time_local": local.isoformat(timespec="minutes"),
                "time_utc": valid.isoformat(timespec="seconds"),
                "temp_c": round(temperature, 3),
            }
            run_field = response.get("runTimeField")
            run_time = parse_provider_time(row.get(run_field), source_timezone) if run_field else None
            records.append((point, run_time))
        if not records:
            raise RuntimeError("CMA-MESO response contained no valid target-date temperature points")
        run_times = [run_time for _point, run_time in records if run_time is not None]
        unique_runs = sorted(set(run_times))
        selected_run = unique_runs[-1] if unique_runs else None
        points = [
            point for point, run_time in records
            if selected_run is None or run_time == selected_run
        ]
        if not points:
            raise RuntimeError("CMA-MESO latest run contained no valid target-date temperature points")
        peak = max(points, key=lambda item: item["temp_c"])
        return {
            "model": "cma_meso_3km",
            "max_c": peak["temp_c"],
            "peak_local": peak["time_local"],
            "points": points,
            "model_run_time_utc": selected_run.isoformat(timespec="seconds") if selected_run else None,
            "run_time_source": "provider_field" if unique_runs else "provider_not_exposed",
            "run_time_confidence": "high" if selected_run else "unavailable",
        }


__all__ = ["CmaMesoAdapter", "nested_value", "parse_provider_time"]
