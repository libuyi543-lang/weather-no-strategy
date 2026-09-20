from __future__ import annotations

import hashlib
import io
import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from PIL import Image


UTC = timezone.utc
RAINVIEWER_METADATA_URL = "https://api.rainviewer.com/public/weather-maps.json"
NICT_LATEST_URL = "https://himawari8.nict.go.jp/img/D531106/latest.json"
NICT_TILE_BASE = "https://himawari8.nict.go.jp/img/D531106"
JAXA_PTREE_LATEST_URL = (
    "https://www.eorc.jaxa.jp/cgi-bin/ptree/tilemap/getAllLatest_v3r2.cgi"
)
JAXA_PTREE_POINT_URL = (
    "https://www.eorc.jaxa.jp/cgi-bin/ptree/tilemap/pickData_T10m_v2r1.py"
)
EARTH_RADIUS_KM = 6371.0088


def finite_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    value = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(value)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angular_difference_deg(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return None
    return abs((first - second + 180.0) % 360.0 - 180.0)


def cloud_cover_from_metar(row: dict[str, Any]) -> float | None:
    raw = str(row.get("raw_metar") or row.get("rawOb") or "").upper()
    if "CAVOK" in raw or " CLR" in f" {raw}" or " SKC" in f" {raw}":
        return 0.0
    clouds = row.get("clouds")
    if not isinstance(clouds, list):
        try:
            clouds = json.loads(str(row.get("sky_conditions_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            clouds = []
    weights = {"FEW": 20.0, "SCT": 45.0, "BKN": 80.0, "OVC": 100.0, "VV": 100.0}
    values = [weights.get(str(item.get("cover") or "").upper()) for item in clouds if isinstance(item, dict)]
    values = [value for value in values if value is not None]
    if values:
        return max(values)
    cover = str(row.get("cover") or "").upper()
    return weights.get(cover)


def web_mercator_pixel(latitude: float, longitude: float, zoom: int) -> tuple[float, float]:
    latitude = max(-85.05112878, min(85.05112878, latitude))
    scale = 256.0 * (2**zoom)
    x = (longitude + 180.0) / 360.0 * scale
    sin_lat = math.sin(math.radians(latitude))
    y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * scale
    return x, y


def himawari_full_disk_pixel(
    latitude: float, longitude: float, tile_count: int = 8, tile_size: int = 550
) -> tuple[float, float]:
    width = float(tile_count * tile_size)
    left = ((longitude + 180.0 - 140.7) % 360.0 - 180.0) / 180.0 * math.pi
    top = ((latitude + 90.0) % 180.0 - 90.0) / 180.0 * math.pi
    radius = width * (1.0 - 0.0045 - 0.0045) / 2.0
    eccentricity_sq = 0.00669438003
    normal = radius / math.sqrt(1.0 - eccentricity_sq * math.sin(top) ** 2)
    limit = math.radians(81.3025)
    left = max(-limit, min(limit, left))
    top = max(-limit, min(limit, top))
    z = radius * 6.613 - normal * math.cos(top) * math.cos(left)
    x = width / 2.0 * math.atan(normal * math.cos(top) * math.sin(left) / z) / 0.1535
    y = width / 2.0 * math.atan(normal * (1.0 - eccentricity_sq) * math.sin(top) / z) / 0.1535
    return x + width / 2.0, width / 2.0 - y


def solar_position(latitude: float, longitude: float, when: datetime) -> dict[str, float]:
    utc = when.astimezone(UTC)
    day = utc.timetuple().tm_yday
    fractional_hour = utc.hour + utc.minute / 60.0 + utc.second / 3600.0
    gamma = 2 * math.pi / 365.0 * (day - 1 + (fractional_hour - 12.0) / 24.0)
    equation = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma)
    )
    true_solar_minutes = (fractional_hour * 60.0 + equation + 4.0 * longitude) % 1440.0
    hour_angle = true_solar_minutes / 4.0 - 180.0
    lat_rad = math.radians(latitude)
    cosine_zenith = (
        math.sin(lat_rad) * math.sin(declination)
        + math.cos(lat_rad) * math.cos(declination) * math.cos(math.radians(hour_angle))
    )
    elevation = 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cosine_zenith))))
    sunset_angle = math.degrees(
        math.acos(max(-1.0, min(1.0, -math.tan(lat_rad) * math.tan(declination))))
    )
    minutes_until_sunset = max(0.0, (sunset_angle - hour_angle) * 4.0)
    return {
        "solarElevationDeg": round(elevation, 2),
        "hoursUntilAstronomicalSunset": round(minutes_until_sunset / 60.0, 2),
        "clearSkyShortwaveProxyWm2": round(max(0.0, 1000.0 * math.sin(math.radians(max(0.0, elevation)))), 1),
    }


class RemoteSensingCollector:
    def __init__(
        self,
        json_get: Callable[..., Any],
        timeout: float = 20.0,
        retries: int = 2,
    ) -> None:
        self.json_get = json_get
        self.timeout = timeout
        self.retries = retries
        self._bytes_cache: dict[str, bytes] = {}
        self._image_cache: dict[str, Image.Image] = {}
        self._url_locks: dict[str, threading.Lock] = {}
        self._url_locks_guard = threading.Lock()
        self._image_lock = threading.Lock()
        self._rainviewer_metadata: dict[str, Any] | None = None
        self._himawari_latest: dict[str, Any] | None = None
        self._rainviewer_metadata_error: str | None = None
        self._himawari_latest_error: str | None = None
        self._jaxa_latest: dict[str, Any] | None = None
        self._jaxa_latest_error: str | None = None
        self._metadata_lock = threading.Lock()

    def begin_cycle(self) -> None:
        """Discard prior-frame caches before a new monitor collection slot."""
        for image in self._image_cache.values():
            image.close()
        self._bytes_cache.clear()
        self._image_cache.clear()
        self._url_locks.clear()
        self._rainviewer_metadata = None
        self._himawari_latest = None
        self._rainviewer_metadata_error = None
        self._himawari_latest_error = None
        self._jaxa_latest = None
        self._jaxa_latest_error = None

    def _get_rainviewer_metadata(self) -> dict[str, Any]:
        if self._rainviewer_metadata is not None:
            return self._rainviewer_metadata
        if self._rainviewer_metadata_error:
            raise RuntimeError(self._rainviewer_metadata_error)
        with self._metadata_lock:
            if self._rainviewer_metadata is not None:
                return self._rainviewer_metadata
            if self._rainviewer_metadata_error:
                raise RuntimeError(self._rainviewer_metadata_error)
            try:
                value = self.json_get(RAINVIEWER_METADATA_URL, error_label="RainViewer metadata")
                if not isinstance(value, dict):
                    raise RuntimeError("RainViewer metadata is not an object")
                self._rainviewer_metadata = value
            except Exception as exc:
                self._rainviewer_metadata_error = str(exc)
                raise
        return self._rainviewer_metadata

    def _get_himawari_latest(self) -> dict[str, Any]:
        if self._himawari_latest is not None:
            return self._himawari_latest
        if self._himawari_latest_error:
            raise RuntimeError(self._himawari_latest_error)
        with self._metadata_lock:
            if self._himawari_latest is not None:
                return self._himawari_latest
            if self._himawari_latest_error:
                raise RuntimeError(self._himawari_latest_error)
            try:
                value = self.json_get(NICT_LATEST_URL, error_label="NICT Himawari latest frame")
                if not isinstance(value, dict):
                    raise RuntimeError("NICT latest-frame response is not an object")
                self._himawari_latest = value
            except Exception as exc:
                self._himawari_latest_error = str(exc)
                raise
        return self._himawari_latest

    def prepare_metadata(self) -> None:
        """Fetch shared frame metadata once before station workers start."""
        for load in (self._get_rainviewer_metadata, self._get_himawari_latest):
            try:
                load()
            except Exception:
                pass

    def _get_jaxa_latest(self) -> dict[str, Any]:
        if self._jaxa_latest is not None:
            return self._jaxa_latest
        if self._jaxa_latest_error:
            raise RuntimeError(self._jaxa_latest_error)
        with self._metadata_lock:
            if self._jaxa_latest is not None:
                return self._jaxa_latest
            if self._jaxa_latest_error:
                raise RuntimeError(self._jaxa_latest_error)
            try:
                value = self.json_get(JAXA_PTREE_LATEST_URL, error_label="JAXA P-Tree latest products")
                if not isinstance(value, dict) or not isinstance(value.get("latest"), dict):
                    raise RuntimeError("JAXA P-Tree latest response is not an object")
                self._jaxa_latest = value["latest"]
            except Exception as exc:
                self._jaxa_latest_error = str(exc)
                raise
        return self._jaxa_latest

    def prepare_jaxa_metadata(self) -> None:
        try:
            self._get_jaxa_latest()
        except Exception:
            pass

    def _get_bytes(self, url: str) -> bytes:
        if url in self._bytes_cache:
            return self._bytes_cache[url]
        with self._url_locks_guard:
            url_lock = self._url_locks.setdefault(url, threading.Lock())
        with url_lock:
            if url in self._bytes_cache:
                return self._bytes_cache[url]
            last_error: Exception | None = None
            for attempt in range(self.retries + 1):
                try:
                    request = Request(url, headers={"User-Agent": "weather-market-monitor/1.0"})
                    with urlopen(request, timeout=self.timeout) as response:
                        body = response.read()
                    self._bytes_cache[url] = body
                    return body
                except (HTTPError, URLError, TimeoutError, OSError) as exc:
                    last_error = exc
                    if attempt < self.retries:
                        time.sleep(0.4 * (2**attempt))
            raise RuntimeError(f"binary GET failed: {url}: {last_error}")

    def _image(self, url: str) -> Image.Image:
        body = self._get_bytes(url)
        with self._image_lock:
            if url not in self._image_cache:
                with Image.open(io.BytesIO(body)) as source:
                    self._image_cache[url] = source.convert("RGBA")
        return self._image_cache[url]

    def _content_digest(self, urls: list[str] | set[str]) -> str:
        digest = hashlib.sha256()
        for url in sorted(set(urls)):
            digest.update(url.encode())
            digest.update(hashlib.sha256(self._bytes_cache[url]).digest())
        return digest.hexdigest()

    def _radar_frame_features(
        self,
        host: str,
        path: str,
        latitude: float,
        longitude: float,
        wind_from_deg: float | None,
        zoom: int = 6,
    ) -> tuple[dict[str, Any], list[str]]:
        center_x, center_y = web_mercator_pixel(latitude, longitude, zoom)
        km_per_pixel = (
            math.cos(math.radians(latitude)) * 2 * math.pi * EARTH_RADIUS_KM / (256 * (2**zoom))
        )
        pixel_radius = int(math.ceil(150.0 / max(km_per_pixel, 0.01)))
        step = max(1, int(round(5.0 / max(km_per_pixel, 0.01))))
        totals = {25: 0, 50: 0, 100: 0, 150: 0}
        echoes = {25: 0, 50: 0, 100: 0, 150: 0}
        nearest_echo: float | None = None
        upwind_total = upwind_echo = 0
        tile_urls: set[str] = set()
        for dy in range(-pixel_radius, pixel_radius + 1, step):
            for dx in range(-pixel_radius, pixel_radius + 1, step):
                distance = math.hypot(dx, dy) * km_per_pixel
                if distance > 150.0:
                    continue
                global_x = int(center_x + dx)
                global_y = int(center_y + dy)
                tile_x, tile_y = global_x // 256, global_y // 256
                local_x, local_y = global_x % 256, global_y % 256
                url = f"{host}{path}/256/{zoom}/{tile_x}/{tile_y}/2/1_1.png"
                tile_urls.add(url)
                pixel = self._image(url).getpixel((local_x, local_y))
                is_echo = pixel[3] >= 20
                for radius in totals:
                    if distance <= radius:
                        totals[radius] += 1
                        echoes[radius] += int(is_echo)
                if is_echo and (nearest_echo is None or distance < nearest_echo):
                    nearest_echo = distance
                if wind_from_deg is not None and distance >= 5.0:
                    sample_bearing = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
                    if (angular_difference_deg(sample_bearing, wind_from_deg) or 999.0) <= 45.0:
                        upwind_total += 1
                        upwind_echo += int(is_echo)
        coverage = {
            f"echoCoverage{radius}Km": round(echoes[radius] / totals[radius], 4) if totals[radius] else None
            for radius in totals
        }
        return (
            {
                **coverage,
                "nearestEchoKm": round(nearest_echo, 1) if nearest_echo is not None else None,
                "upwindEchoCoverage150Km": round(upwind_echo / upwind_total, 4) if upwind_total else None,
                "windFromDegUsed": wind_from_deg,
                "sampleSpacingApproxKm": round(step * km_per_pixel, 2),
            },
            sorted(tile_urls),
        )

    def radar(self, latitude: float, longitude: float, wind_from_deg: float | None) -> dict[str, Any]:
        metadata = self._get_rainviewer_metadata()
        host = str(metadata.get("host") or "https://tilecache.rainviewer.com")
        frames = ((metadata.get("radar") or {}).get("past") or [])[-2:]
        if not frames:
            raise RuntimeError("RainViewer returned no radar frames")
        analyzed: list[dict[str, Any]] = []
        urls: list[str] = []
        for frame in frames:
            features, frame_urls = self._radar_frame_features(
                host, str(frame["path"]), latitude, longitude, wind_from_deg
            )
            analyzed.append({"frameTimeUtc": datetime.fromtimestamp(frame["time"], UTC).isoformat(), **features})
            urls.extend(frame_urls)
        latest = analyzed[-1]
        previous = analyzed[-2] if len(analyzed) > 1 else None
        trend = None
        if previous and latest.get("echoCoverage100Km") is not None and previous.get("echoCoverage100Km") is not None:
            trend = round(latest["echoCoverage100Km"] - previous["echoCoverage100Km"], 4)
        return {
            "source": "rainviewer",
            "frame_time_utc": latest["frameTimeUtc"],
            "status": "ok",
            "quality": "radar_composite_proxy_approximately_10km_sampling",
            "features": {**latest, "echoCoverage100KmChange": trend, "recentFrames": analyzed},
            "source_url": RAINVIEWER_METADATA_URL,
            "raw_sha256": self._content_digest(urls),
        }

    @staticmethod
    def _cloud_pixel(pixel: tuple[int, int, int, int]) -> tuple[bool, bool]:
        red, green, blue, alpha = pixel
        if alpha < 20:
            return False, False
        brightness = (red + green + blue) / 3.0
        spread = max(red, green, blue) - min(red, green, blue)
        cloud = (brightness >= 115 and spread <= 75 and blue >= 90) or brightness >= 205
        deep = brightness >= 220 and spread <= 45
        return cloud, deep

    def satellite(self, latitude: float, longitude: float) -> dict[str, Any]:
        latest = self._get_himawari_latest()
        frame = datetime.strptime(str(latest["date"]), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        tile_count, tile_size = 8, 550
        counts = {25: [0, 0, 0], 50: [0, 0, 0], 100: [0, 0, 0], 150: [0, 0, 0]}
        urls: set[str] = set()
        spacing_km = 10
        for north_km in range(-150, 151, spacing_km):
            for east_km in range(-150, 151, spacing_km):
                distance = math.hypot(east_km, north_km)
                if distance > 150:
                    continue
                sample_lat = latitude + north_km / 111.32
                sample_lon = longitude + east_km / (111.32 * max(0.2, math.cos(math.radians(latitude))))
                x, y = himawari_full_disk_pixel(sample_lat, sample_lon, tile_count, tile_size)
                tile_x, tile_y = int(x // tile_size), int(y // tile_size)
                if not (0 <= tile_x < tile_count and 0 <= tile_y < tile_count):
                    continue
                url = (
                    f"{NICT_TILE_BASE}/{tile_count}d/{tile_size}/"
                    f"{frame.strftime('%Y/%m/%d/%H%M%S')}_{tile_x}_{tile_y}.png"
                )
                urls.add(url)
                pixel = self._image(url).getpixel((int(x % tile_size), int(y % tile_size)))
                cloud, deep = self._cloud_pixel(pixel)
                for radius, values in counts.items():
                    if distance <= radius:
                        values[0] += 1
                        values[1] += int(cloud)
                        values[2] += int(deep)
        features: dict[str, Any] = {
            "classification": "heuristic cloud proxy from Himawari true-colour RGB; not calibrated cloud fraction",
            "sampleSpacingApproxKm": spacing_km,
        }
        for radius, (total, cloud, deep) in counts.items():
            features[f"cloudProxyCoverage{radius}Km"] = round(cloud / total, 4) if total else None
            features[f"deepCloudProxyCoverage{radius}Km"] = round(deep / total, 4) if total else None
        return {
            "source": "nict_himawari_true_colour",
            "frame_time_utc": frame.isoformat(),
            "status": "ok",
            "quality": "daylight_rgb_cloud_proxy_uncalibrated",
            "features": features,
            "source_url": NICT_LATEST_URL,
            "raw_sha256": self._content_digest(urls),
        }

    @staticmethod
    def _jaxa_frame(value: Any) -> tuple[str, str]:
        text = str(value or "")
        if len(text) != 12 or not text.isdigit():
            raise RuntimeError(f"invalid JAXA frame timestamp: {text}")
        frame = datetime.strptime(text, "%Y%m%d%H%M").replace(tzinfo=UTC)
        return text, frame.isoformat(timespec="seconds")

    def _jaxa_point(
        self, product: str, frame_text: str, latitude: float, longitude: float
    ) -> tuple[float | None, str]:
        params = {
            "lang": "en",
            "prod": product,
            "sdate": frame_text,
            "ulat": f"{latitude:.5f}",
            "llon": f"{longitude:.5f}",
            "dlat": f"{latitude:.5f}",
            "rlon": f"{longitude:.5f}",
        }
        url = f"{JAXA_PTREE_POINT_URL}?{urlencode(params)}"
        body = self._get_bytes(url)
        text = body.decode("utf-8", errors="replace").strip().splitlines()[0]
        value = finite_float(text.split(",", 1)[0])
        if value is None:
            raise RuntimeError(f"JAXA {product} returned no point value: {text[:200]}")
        if value <= -300:
            value = None
        return value, url

    def jaxa_products(self, latitude: float, longitude: float) -> list[dict[str, Any]]:
        """Read official 10-minute P-Tree L2 point products used by the public JAXA map."""
        latest = self._get_jaxa_latest()
        swr_text, swr_frame = self._jaxa_frame((latest.get("L2_SWR") or {}).get("date"))
        cloud_text, cloud_frame = self._jaxa_frame((latest.get("L2_CLOT") or {}).get("date"))
        cloud_type_text, cloud_type_frame = self._jaxa_frame(
            (latest.get("L2_CLTYPE") or {}).get("date")
        )
        swr, swr_url = self._jaxa_point("SWR", swr_text, latitude, longitude)
        optical_thickness, cloud_url = self._jaxa_point("CLOT", cloud_text, latitude, longitude)
        cloud_type, cloud_type_url = self._jaxa_point(
            "CLTP", cloud_type_text, latitude, longitude
        )
        return [
            {
                "source": "jaxa_himawari_swr_l2",
                "frame_time_utc": swr_frame,
                "status": "ok",
                "quality": "official_jaxa_ptree_l2_5km_point_value",
                "features": {
                    "shortwaveRadiationWm2": swr,
                    "retrievalAvailable": swr is not None,
                    "temporalResolutionMinutes": 10,
                    "spatialResolutionKm": 5,
                },
                "source_url": swr_url,
                "raw_sha256": self._content_digest([swr_url]),
            },
            {
                "source": "jaxa_himawari_cloud_l2",
                "frame_time_utc": min(cloud_frame, cloud_type_frame),
                "status": "ok",
                "quality": "official_jaxa_ptree_l2_5km_point_value",
                "features": {
                    "cloudOpticalThickness": optical_thickness,
                    "cloudTypeIsccpCode": int(cloud_type) if cloud_type is not None else None,
                    "opticalThicknessRetrievalAvailable": optical_thickness is not None,
                    "cloudTypeRetrievalAvailable": cloud_type is not None,
                    "opticalThicknessFrameUtc": cloud_frame,
                    "cloudTypeFrameUtc": cloud_type_frame,
                    "temporalResolutionMinutes": 10,
                    "spatialResolutionKm": 5,
                },
                "source_url": cloud_url,
                "raw_sha256": self._content_digest([cloud_url, cloud_type_url]),
            },
        ]


def _trend(rows: list[dict[str, Any]], field: str) -> float | None:
    usable = [(parse_time(row.get("observation_time_utc")), finite_float(row.get(field))) for row in rows]
    usable = [(when, value) for when, value in usable if when is not None and value is not None]
    if len(usable) < 2:
        return None
    newest_time, newest_value = usable[0]
    oldest_time, oldest_value = usable[-1]
    hours = (newest_time - oldest_time).total_seconds() / 3600.0
    if hours <= 0:
        return None
    return round((newest_value - oldest_value) / hours, 3)


def _latest_model_state(db: Any, station_id: str, target_date: str, as_of: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    queries = {
        "meteoblue": (
            "SELECT forecast_max_c,points_json,slot_utc FROM windy_forecasts "
            "WHERE station_id=? AND target_date=? AND model='mblue' AND status='ok' AND slot_utc<=? "
            "ORDER BY slot_utc DESC LIMIT 1"
        ),
        "ecmwf": (
            "SELECT forecast_max_c,points_json,slot_utc FROM external_forecasts "
            "WHERE station_id=? AND target_date=? AND model='ecmwf_ifs025' AND status='ok' AND slot_utc<=? "
            "ORDER BY slot_utc DESC LIMIT 1"
        ),
    }
    current_time = parse_time(as_of)
    for name, query in queries.items():
        row = db.execute(query, (station_id, target_date, as_of)).fetchone()
        if not row:
            output[name] = None
            continue
        try:
            points = json.loads(row["points_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            points = []
        nearest = None
        remaining = []
        for point in points:
            point_time = parse_time(point.get("time_utc")) if isinstance(point, dict) else None
            if point_time is None or current_time is None:
                continue
            if nearest is None or abs((point_time - current_time).total_seconds()) < nearest[0]:
                nearest = (abs((point_time - current_time).total_seconds()), point)
            if point_time >= current_time - timedelta(minutes=15):
                remaining.append(point)
        output[name] = {
            "sampleSlotUtc": row["slot_utc"],
            "forecastMaxC": finite_float(row["forecast_max_c"]),
            "sameHourForecastC": finite_float((nearest or (None, {}))[1].get("temp_c")),
            "remainingShortwaveEnergyWhM2": round(sum(
                finite_float(point.get("shortwave_radiation_wm2")) or 0.0 for point in remaining
            ), 1) if name == "ecmwf" else None,
        }
    return output


def analyze_weather_process(
    db: Any,
    station: dict[str, Any],
    target_date: str,
    as_of_utc: datetime,
    coastal_wind_sectors: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    station_id = str(station["station_id"])
    as_of = as_of_utc.astimezone(UTC).isoformat(timespec="seconds")
    since = (as_of_utc - timedelta(hours=3)).isoformat(timespec="seconds")
    network_rows = [dict(row) for row in db.execute(
        """
        SELECT * FROM station_network_reports
        WHERE primary_station_id=? AND observation_time_utc<=? AND observation_time_utc>=?
        ORDER BY observation_time_utc DESC
        """,
        (station_id, as_of, since),
    ).fetchall()]
    primary = [row for row in network_rows if row["station_id"] == station_id]
    if not primary:
        primary = [dict(row) for row in db.execute(
            """
            SELECT observation_time_utc,temperature_c,dewpoint_c,wind_direction_deg,wind_speed,
                   wind_gust,pressure_hpa,sky_conditions_json,raw_metar
            FROM weather_observations WHERE station_id=? AND source='metar' AND status='ok'
              AND observation_time_utc<=? AND observation_time_utc>=?
            GROUP BY observation_time_utc ORDER BY observation_time_utc DESC
            """,
            (station_id, as_of, since),
        ).fetchall()]
    current = primary[0] if primary else {}
    current_time = parse_time(current.get("observation_time_utc")) or as_of_utc
    recent_primary = [
        row for row in primary
        if parse_time(row.get("observation_time_utc"))
        and current_time - parse_time(row["observation_time_utc"]) <= timedelta(minutes=120)
    ]
    temperature_trend = _trend(recent_primary, "temperature_c")
    dewpoint_trend = _trend(recent_primary, "dewpoint_c")
    pressure_trend = _trend(recent_primary, "pressure_hpa")
    cloud_values = [cloud_cover_from_metar(row) for row in recent_primary]
    cloud_values = [value for value in cloud_values if value is not None]
    cloud_change = round(cloud_values[0] - cloud_values[-1], 1) if len(cloud_values) >= 2 else None
    current_wind = finite_float(current.get("wind_direction_deg"))
    old_wind = finite_float(recent_primary[-1].get("wind_direction_deg")) if recent_primary else None
    wind_shift = angular_difference_deg(current_wind, old_wind)

    latest_neighbors: dict[str, dict[str, Any]] = {}
    for row in network_rows:
        if row["station_id"] == station_id or row["station_id"] in latest_neighbors:
            continue
        observed = parse_time(row.get("observation_time_utc"))
        if observed and abs((current_time - observed).total_seconds()) <= 75 * 60:
            latest_neighbors[row["station_id"]] = row
    upwind = []
    for row in latest_neighbors.values():
        bearing = finite_float(row.get("bearing_from_primary_deg"))
        if current_wind is not None and bearing is not None and (angular_difference_deg(bearing, current_wind) or 999) <= 60:
            upwind.append(row)
    upwind_temp = None
    if upwind:
        weighted = [
            (finite_float(row.get("temperature_c")), max(1.0, finite_float(row.get("distance_km")) or 1.0))
            for row in upwind
        ]
        weighted = [(value, distance) for value, distance in weighted if value is not None]
        if weighted:
            upwind_temp = sum(value / distance for value, distance in weighted) / sum(1 / distance for _, distance in weighted)
    current_temp = finite_float(current.get("temperature_c"))
    upwind_delta = round(upwind_temp - current_temp, 2) if upwind_temp is not None and current_temp is not None else None

    remote: dict[str, dict[str, Any]] = {}
    for row in db.execute(
        """
        SELECT source,frame_time_utc,status,quality,features_json,error
        FROM remote_sensing_snapshots WHERE station_id=? AND slot_utc<=?
        ORDER BY slot_utc DESC
        """,
        (station_id, as_of),
    ).fetchall():
        if row["source"] in remote:
            continue
        try:
            features = json.loads(row["features_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            features = {}
        frame_time = parse_time(row["frame_time_utc"])
        frame_age = (
            max(0.0, (as_of_utc - frame_time).total_seconds() / 60.0)
            if frame_time is not None else None
        )
        remote[row["source"]] = {
            "frameTimeUtc": row["frame_time_utc"], "status": row["status"],
            "frameAgeMinutes": round(frame_age, 1) if frame_age is not None else None,
            "quality": row["quality"], "features": features, "error": row["error"],
        }
    radar = remote.get("rainviewer", {}).get("features", {})
    jaxa_swr = remote.get("jaxa_himawari_swr_l2", {}).get("features", {})
    jaxa_cloud = remote.get("jaxa_himawari_cloud_l2", {}).get("features", {})
    echo_near = finite_float(radar.get("nearestEchoKm"))
    radar_upwind = finite_float(radar.get("upwindEchoCoverage150Km"))
    radar_change = finite_float(radar.get("echoCoverage100KmChange"))

    city_key = str(station.get("city") or "").strip().casefold()
    sectors = coastal_wind_sectors or {}
    marine_sector = sectors.get(city_key)
    in_marine_sector = False
    if marine_sector and current_wind is not None:
        start, end = marine_sector
        in_marine_sector = start <= current_wind <= end if start <= end else current_wind >= start or current_wind <= end
    sea_breeze_score = sum((
        bool(in_marine_sector),
        dewpoint_trend is not None and dewpoint_trend >= 0.5,
        temperature_trend is not None and temperature_trend <= 0.5,
        wind_shift is not None and wind_shift >= 25,
    ))
    sea_breeze = bool(marine_sector and sea_breeze_score >= 3)
    cold_pool_score = sum((
        temperature_trend is not None and temperature_trend <= -1.0,
        pressure_trend is not None and pressure_trend >= 0.4,
        wind_shift is not None and wind_shift >= 30,
        echo_near is not None and echo_near <= 50,
    ))
    cold_pool = cold_pool_score >= 3
    clearing = bool(
        (cloud_change is not None and cloud_change <= -30)
        and (temperature_trend is None or temperature_trend >= 0)
    )
    approaching_cloud_or_rain = bool(
        (radar_upwind is not None and radar_upwind >= 0.08)
        or (radar_change is not None and radar_change >= 0.05)
    )
    solar = solar_position(float(station["latitude"]), float(station["longitude"]), current_time)
    solar["jaxaShortwaveRadiationWm2"] = finite_float(jaxa_swr.get("shortwaveRadiationWm2"))
    solar["jaxaToClearSkyRatio"] = (
        round(solar["jaxaShortwaveRadiationWm2"] / solar["clearSkyShortwaveProxyWm2"], 3)
        if solar["jaxaShortwaveRadiationWm2"] is not None
        and solar["clearSkyShortwaveProxyWm2"] > 0 else None
    )
    solar["jaxaCloudOpticalThickness"] = finite_float(jaxa_cloud.get("cloudOpticalThickness"))
    solar["jaxaCloudTypeIsccpCode"] = finite_float(jaxa_cloud.get("cloudTypeIsccpCode"))
    models = _latest_model_state(db, station_id, target_date, as_of)
    for model in models.values():
        if model and current_temp is not None and model.get("sameHourForecastC") is not None:
            model["observationMinusSameHourForecastC"] = round(current_temp - model["sameHourForecastC"], 2)

    flags = []
    evidence = []
    if sea_breeze:
        flags.append("sea_breeze_intrusion")
        evidence.append("wind shifted into the configured marine sector with dewpoint/temperature support")
    if cold_pool:
        flags.append("convective_cold_pool")
        evidence.append("temperature fall, pressure/wind change and nearby radar echo jointly support a cold pool")
    if clearing:
        flags.append("clearing")
        evidence.append("METAR cloud cover decreased while temperature held or rose")
    if approaching_cloud_or_rain:
        flags.append("upwind_cloud_or_rain_approach")
        evidence.append("radar echo is increasing or occupies the upwind sector")
    if temperature_trend is not None:
        evidence.append(f"primary-station temperature trend is {temperature_trend:+.2f} C/hour")
    if upwind_delta is not None:
        evidence.append(f"distance-weighted upwind temperature is {upwind_delta:+.2f} C versus the primary station")
    if not flags:
        flags.append("no_high_confidence_regime_change")

    directional = []
    if cold_pool or sea_breeze or approaching_cloud_or_rain:
        directional.append("downward_or_delayed_peak_risk")
    if clearing:
        directional.append("upward_catch_up_heating_risk")
    if temperature_trend is not None and temperature_trend >= 1.0 and solar["hoursUntilAstronomicalSunset"] >= 2:
        directional.append("continued_heating_capacity")
    if not directional:
        directional.append("no_mechanistic_adjustment_with_high_confidence")

    return {
        "asOfUtc": as_of,
        "primaryObservationTimeUtc": current.get("observation_time_utc"),
        "status": "ok" if current else "insufficient_primary_observations",
        "observationWindowMinutes": 120,
        "primaryStationTrend": {
            "temperatureTrendCPerHour": temperature_trend,
            "dewpointTrendCPerHour": dewpoint_trend,
            "pressureTrendHpaPerHour": pressure_trend,
            "cloudCoverChangePct": cloud_change,
            "windShiftDeg": wind_shift,
            "currentTemperatureC": current_temp,
            "currentWindFromDeg": current_wind,
        },
        "stationNetwork": {
            "reportsInWindow": len(network_rows),
            "nearbyStationsCurrent": len(latest_neighbors),
            "upwindStationsUsed": len(upwind),
            "upwindStationIds": sorted(row["station_id"] for row in upwind),
            "upwindTemperatureMinusPrimaryC": upwind_delta,
        },
        "remoteSensing": remote,
        "solarHeating": solar,
        "modelRealityComparison": models,
        "detectedProcesses": flags,
        "processEvidence": evidence,
        "mechanisticModelAdjustment": {
            "directionalSignals": directional,
            "automaticDegreeCorrectionApplied": False,
            "reason": "The deterministic layer diagnoses mechanisms; Hermes must size any model correction and preserve uncertainty.",
        },
        "qualityWarnings": [
            "RainViewer coverage is a radar-composite pixel proxy, not calibrated precipitation intensity.",
            "Himawari cloud coverage is an uncalibrated daylight RGB proxy and must not replace METAR cloud reports.",
            "JAXA P-Tree SWR/cloud values are official 5 km satellite retrievals, not station pyranometer measurements.",
            "Open-Meteo/ECMWF radiation is model-derived rather than a station pyranometer observation.",
        ] + [
            f"{source} frame is stale by {details['frameAgeMinutes']:.0f} minutes."
            for source, details in remote.items()
            if finite_float(details.get("frameAgeMinutes")) is not None
            and finite_float(details.get("frameAgeMinutes")) > 60
        ],
    }
