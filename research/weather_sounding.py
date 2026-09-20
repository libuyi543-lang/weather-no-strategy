"""Observed upper-air sounding support for the weather high model."""

from __future__ import annotations

import html
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


UTC = timezone.utc
UWYO_URL = "https://weather.uwyo.edu/wsgi/sounding"
KAPPA = 0.2854

# Nearest currently reporting mainland upper-air stations. Distances are
# calculated against each settlement station at runtime.
CITY_SOUNDING_STATIONS = {
    "Beijing": {"station_id": "54511", "name": "Beijing", "latitude": 39.9333, "longitude": 116.2833},
    "Chengdu": {"station_id": "56187", "name": "Wenjiang", "latitude": 30.7500, "longitude": 103.8667},
    "Chongqing": {"station_id": "57516", "name": "Chongqing", "latitude": 29.5833, "longitude": 106.4667},
    "Guangzhou": {"station_id": "59280", "name": "Qingyuan", "latitude": 23.7167, "longitude": 113.0833},
    "Qingdao": {"station_id": "54857", "name": "Qingdao", "latitude": 36.0667, "longitude": 120.3333},
    "Shanghai": {"station_id": "58362", "name": "Shanghai Baoshan", "latitude": 31.4167, "longitude": 121.4500},
    "Wuhan": {"station_id": "57494", "name": "Wuhan", "latitude": 30.6000, "longitude": 114.0500},
}


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    value = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


@dataclass(frozen=True)
class SoundingLevel:
    pressure_hpa: float
    height_m: float
    temperature_c: float
    dewpoint_c: float | None
    relative_humidity_pct: float | None
    wind_direction_deg: float | None
    wind_speed_ms: float | None
    potential_temperature_k: float | None
    virtual_potential_temperature_k: float | None


@dataclass(frozen=True)
class SoundingFeatures:
    source: str
    station_id: str
    station_name: str
    observation_time_utc: str
    distance_km: float
    surface_pressure_hpa: float
    surface_temperature_c: float
    surface_dewpoint_c: float | None
    temperature_925_c: float | None
    temperature_850_c: float | None
    temperature_700_c: float | None
    dewpoint_depression_850_c: float | None
    low_level_inversion_c: float
    inversion_top_agl_m: float | None
    surface_equivalent_850_c: float | None
    surface_equivalent_700_c: float | None
    layer_850_700_lapse_c_per_km: float | None
    vertical_regime: str
    levels: int


def parse_sounding_html(
    body: str, station: dict[str, Any], primary_latitude: float, primary_longitude: float,
) -> SoundingFeatures:
    title = re.search(
        r"Observations for Station\s+(\d+)\s+at\s+(\d{2}) UTC (\d{2}) ([A-Z][a-z]{2}) (\d{4})",
        body,
    )
    if not title:
        raise ValueError("sounding observation header is missing")
    station_id, hour, day, month_name, year = title.groups()
    observed = datetime.strptime(
        f"{year}-{month_name}-{day} {hour}:00", "%Y-%b-%d %H:%M"
    ).replace(tzinfo=UTC)
    block = re.search(r"<PRE>(.*?)</PRE>", body, flags=re.IGNORECASE | re.DOTALL)
    if not block:
        raise ValueError("sounding level table is missing")
    levels: list[SoundingLevel] = []
    for line in html.unescape(block.group(1)).splitlines():
        fields = line.split()
        if len(fields) != 11:
            continue
        values = [finite(item) for item in fields]
        if values[0] is None or values[1] is None or values[2] is None:
            continue
        levels.append(SoundingLevel(
            pressure_hpa=float(values[0]), height_m=float(values[1]), temperature_c=float(values[2]),
            dewpoint_c=values[3], relative_humidity_pct=values[4], wind_direction_deg=values[6],
            wind_speed_ms=values[7], potential_temperature_k=values[8],
            virtual_potential_temperature_k=values[10],
        ))
    if len(levels) < 8:
        raise ValueError("sounding has too few usable levels")
    levels.sort(key=lambda item: item.height_m)
    surface = levels[0]

    def nearest_pressure(target: float, tolerance: float = 18.0) -> SoundingLevel | None:
        candidate = min(levels, key=lambda item: abs(item.pressure_hpa - target))
        return candidate if abs(candidate.pressure_hpa - target) <= tolerance else None

    p925, p850, p700 = nearest_pressure(925), nearest_pressure(850), nearest_pressure(700)
    low_levels = [item for item in levels if 0 <= item.height_m - surface.height_m <= 1200]
    warmest = max(low_levels, key=lambda item: item.temperature_c) if low_levels else surface
    inversion = max(0.0, warmest.temperature_c - surface.temperature_c)
    inversion_top = warmest.height_m - surface.height_m if inversion >= 0.5 else None

    def surface_equivalent(level: SoundingLevel | None) -> float | None:
        theta = level.potential_temperature_k if level else None
        if theta is None:
            return None
        return theta * (surface.pressure_hpa / 1000.0) ** KAPPA - 273.15

    lapse = None
    if p850 and p700 and p700.height_m > p850.height_m:
        lapse = (p850.temperature_c - p700.temperature_c) / ((p700.height_m - p850.height_m) / 1000.0)
    depression_850 = (
        p850.temperature_c - p850.dewpoint_c
        if p850 and p850.dewpoint_c is not None else None
    )
    if inversion >= 2.0:
        regime = "morning_inversion_reservoir"
    elif depression_850 is not None and depression_850 >= 10.0 and lapse is not None and lapse >= 7.0:
        regime = "deep_dry_mixing_support"
    elif depression_850 is not None and depression_850 <= 4.0:
        regime = "moist_low_level_capping_risk"
    else:
        regime = "neutral_vertical_profile"
    return SoundingFeatures(
        source="University of Wyoming BUFR observed sounding",
        station_id=station_id, station_name=str(station["name"]),
        observation_time_utc=observed.isoformat(timespec="seconds"),
        distance_km=round(haversine_km(
            primary_latitude, primary_longitude,
            float(station["latitude"]), float(station["longitude"]),
        ), 1),
        surface_pressure_hpa=surface.pressure_hpa,
        surface_temperature_c=surface.temperature_c,
        surface_dewpoint_c=surface.dewpoint_c,
        temperature_925_c=p925.temperature_c if p925 else None,
        temperature_850_c=p850.temperature_c if p850 else None,
        temperature_700_c=p700.temperature_c if p700 else None,
        dewpoint_depression_850_c=round(depression_850, 2) if depression_850 is not None else None,
        low_level_inversion_c=round(inversion, 2),
        inversion_top_agl_m=round(inversion_top, 0) if inversion_top is not None else None,
        surface_equivalent_850_c=round(surface_equivalent(p850), 2) if p850 else None,
        surface_equivalent_700_c=round(surface_equivalent(p700), 2) if p700 else None,
        layer_850_700_lapse_c_per_km=round(lapse, 2) if lapse is not None else None,
        vertical_regime=regime, levels=len(levels),
    )


class SoundingClient:
    def __init__(self, cache_dir: Path, timeout: int = 20):
        self.cache_dir = cache_dir
        self.timeout = timeout
        self._memory: dict[tuple[str, str], SoundingFeatures | None] = {}
        self._lock = Lock()

    def fetch(
        self, city: str, target_date: str, primary_latitude: float, primary_longitude: float,
    ) -> SoundingFeatures | None:
        station = CITY_SOUNDING_STATIONS.get(city)
        if not station:
            return None
        key = (city, target_date)
        with self._lock:
            if key in self._memory:
                return self._memory[key]
        cache_path = self.cache_dir / f"{station['station_id']}_{target_date}_00Z.json"
        try:
            if cache_path.exists():
                value = SoundingFeatures(**json.loads(cache_path.read_text(encoding="utf-8")))
            else:
                params = urlencode({
                    "datetime": f"{target_date} 00:00:00", "id": station["station_id"],
                    "type": "TEXT:LIST", "src": "BUFR",
                })
                request = Request(f"{UWYO_URL}?{params}", headers={"User-Agent": "weather-high-research/1.0"})
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8", errors="replace")
                value = parse_sounding_html(body, station, primary_latitude, primary_longitude)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(asdict(value), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            value = None
        with self._lock:
            self._memory[key] = value
        return value
