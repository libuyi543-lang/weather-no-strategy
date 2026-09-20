#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import signal
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from weather_market_monitor import (
    CONFIG_PATH,
    METAR_URL,
    JsonClient,
    as_float,
    epoch_to_iso_utc,
    iso_utc,
    normalized_city,
    parsed_metar_fields,
)


ROOT = Path(__file__).resolve().parent
UTC = timezone.utc


def _time_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return epoch_to_iso_utc(value)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).astimezone(UTC).isoformat(timespec="seconds")
    except ValueError:
        return str(value)


class FastMetarCollector:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.db_path = ROOT / config["databasePath"]
        busy_timeout_seconds = max(60, int(config.get("fastMetarBusyTimeoutSeconds", 300)))
        self.db = sqlite3.connect(self.db_path, timeout=busy_timeout_seconds)
        self.db.row_factory = sqlite3.Row
        self.db.execute(f"PRAGMA busy_timeout={busy_timeout_seconds * 1000}")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.client = JsonClient(
            timeout=float(config.get("requestTimeoutSeconds", 20)),
            retries=int(config.get("requestRetries", 2)),
        )
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
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
            CREATE INDEX IF NOT EXISTS idx_fast_metar_latest
                ON fast_metar_reports(station_id,observation_time_utc DESC);
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def station_ids(self) -> list[str]:
        allowed = {
            normalized_city(city)
            for city in self.config.get("processAnalysisCities", [])
            if str(city).strip()
        }
        rows = self.db.execute(
            "SELECT station_id,city FROM stations WHERE length(station_id)=4 ORDER BY station_id"
        ).fetchall()
        station_ids = [
            str(row["station_id"]).upper()
            for row in rows
            if not allowed or normalized_city(row["city"]) in allowed
        ]
        configured = [
            str(value).upper()
            for value in self.config.get("fastMetarStationIds", [])
            if len(str(value)) == 4
        ]
        return sorted(set(station_ids or configured))

    def _persist_network_report(self, report: dict[str, Any], fetched_at: str) -> None:
        station_id = str(report.get("icaoId") or "").strip().upper()
        observation_time = epoch_to_iso_utc(report.get("obsTime"))
        if not station_id or observation_time is None:
            return
        parsed = parsed_metar_fields(report.get("rawOb"))
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
                report_time_utc=excluded.report_time_utc,
                receipt_time_utc=excluded.receipt_time_utc,metar_type=excluded.metar_type,
                temperature_c=excluded.temperature_c,dewpoint_c=excluded.dewpoint_c,
                wind_direction_deg=excluded.wind_direction_deg,wind_speed_kt=excluded.wind_speed_kt,
                wind_gust_kt=excluded.wind_gust_kt,pressure_hpa=excluded.pressure_hpa,
                visibility_m=excluded.visibility_m,flight_category=excluded.flight_category,
                cover=excluded.cover,clouds_json=excluded.clouds_json,weather_code=excluded.weather_code,
                raw_metar=excluded.raw_metar,last_seen_utc=excluded.last_seen_utc
            """,
            (
                station_id, station_id, report.get("name"), observation_time,
                _time_text(report.get("reportTime")), _time_text(report.get("receiptTime")),
                report.get("metarType"), as_float(report.get("lat")), as_float(report.get("lon")),
                as_float(report.get("elev")), 0.0, 0.0, as_float(report.get("temp")),
                as_float(report.get("dewp")), as_float(report.get("wdir")), as_float(report.get("wspd")),
                as_float(report.get("wgst", parsed.get("wind_gust"))),
                as_float(report.get("altim", parsed.get("pressure_hpa"))),
                visibility * 1609.344 if visibility is not None else parsed.get("visibility_m"),
                report.get("fltCat"), report.get("cover"),
                json.dumps(report.get("clouds") or [], ensure_ascii=False, separators=(",", ":")),
                report.get("wxString"), report.get("rawOb"), None, fetched_at, fetched_at,
            ),
        )

    def collect_once(self) -> dict[str, Any]:
        station_ids = self.station_ids()
        if not station_ids:
            raise RuntimeError("No four-character METAR stations are configured")
        started = time.monotonic()
        fetched_at = iso_utc()
        payload = self.client.get(
            METAR_URL,
            {
                "ids": ",".join(station_ids), "format": "json",
                "hours": int(self.config.get("fastMetarRequestHours", 1)),
            },
            error_label=f"one-minute batch METAR ({len(station_ids)} stations)",
        )
        reports = payload if isinstance(payload, list) else []
        inserted = 0
        report_types: dict[str, int] = {}
        seen_stations: set[str] = set()
        for report in reports:
            station_id = str(report.get("icaoId") or "").strip().upper()
            observation_time = epoch_to_iso_utc(report.get("obsTime"))
            raw_metar = str(report.get("rawOb") or "").strip()
            if station_id not in station_ids or not observation_time or not raw_metar:
                continue
            seen_stations.add(station_id)
            metar_type = str(report.get("metarType") or "UNKNOWN").upper()
            report_types[metar_type] = report_types.get(metar_type, 0) + 1
            before = self.db.total_changes
            cursor = self.db.execute(
                """
                INSERT INTO fast_metar_reports(
                    station_id,observation_time_utc,report_time_utc,receipt_time_utc,metar_type,
                    raw_metar,payload_json,first_fetched_at_utc,last_fetched_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(station_id,observation_time_utc,raw_metar) DO NOTHING
                """,
                (
                    station_id, observation_time, _time_text(report.get("reportTime")),
                    _time_text(report.get("receiptTime")), report.get("metarType"), raw_metar,
                    json.dumps(report, ensure_ascii=False, separators=(",", ":")), fetched_at, fetched_at,
                ),
            )
            inserted += int(self.db.total_changes > before)
            if cursor.rowcount:
                self._persist_network_report(report, fetched_at)
        self.db.commit()
        result = {
            "fetched_at_utc": fetched_at,
            "station_ids": station_ids,
            "stations_seen": sorted(seen_stations),
            "reports_received": len(reports),
            "rows_upserted": inserted,
            "report_types": report_types,
            "missing_stations": sorted(set(station_ids) - seen_stations),
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        status_path = ROOT / self.config["reportDirectory"] / "metar_fast_status_latest.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("batch METAR completed: %s", json.dumps(result, ensure_ascii=False))
        return result


def configure_logging(config: dict[str, Any]) -> None:
    log_path = ROOT / "logs/weather_metar_fast_collector.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")],
        force=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="One-minute batch METAR/SPECI collector")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    configure_logging(config)
    lock_path = ROOT / "data/weather_metar_fast_collector.lock"
    lock_handle = lock_path.open("w")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    collector = FastMetarCollector(config)
    stopped = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        while True:
            started = time.monotonic()
            try:
                result = collector.collect_once()
                if args.once or not args.loop:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 0
            except Exception:
                collector.db.rollback()
                logging.exception("batch METAR collection failed")
                if args.once or not args.loop:
                    return 1
            if stopped:
                return 0
            remaining = max(0.0, 60.0 - (time.monotonic() - started))
            end = time.monotonic() + remaining
            while not stopped and time.monotonic() < end:
                time.sleep(min(1.0, end - time.monotonic()))
    finally:
        collector.close()
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
