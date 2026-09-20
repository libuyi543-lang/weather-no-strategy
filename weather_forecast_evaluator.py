#!/usr/bin/env python3
"""Leakage-aware forecast evaluation for the weather research database.

This is intentionally a small SQLite implementation of the useful part of
WeatherBench-X: explicit forecast rows, a station/event target, and metrics
that can be grouped by model, city, and forecast time.  It does not add the
large xarray/Beam dependency stack to the live collector.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


UTC = timezone.utc


def as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def round_half_up(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fahrenheit_to_celsius(value: float) -> float:
    return (value - 32.0) * 5.0 / 9.0


class WeatherForecastEvaluator:
    """Materialize forecast/settlement pairs and query past-only calibration."""

    def __init__(self, db: sqlite3.Connection, report_dir: str | Path | None = None):
        self.db = db
        self.report_dir = Path(report_dir) if report_dir else None
        self.init_schema()

    def init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS weather_forecast_evaluations (
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                forecast_model TEXT NOT NULL,
                sample_slot_utc TEXT NOT NULL,
                forecast_issued_at_utc TEXT,
                sample_local_date TEXT,
                sample_local_time TEXT,
                sample_local_minute INTEGER,
                lead_hours REAL,
                forecast_max_c REAL,
                final_settlement_c REAL,
                signed_error_c REAL,
                absolute_error_c REAL,
                squared_error_c REAL,
                rounded_hit INTEGER,
                within_one_c INTEGER,
                resolution_exact INTEGER NOT NULL DEFAULT 0,
                created_at_utc TEXT NOT NULL,
                PRIMARY KEY(event_id, forecast_model, sample_slot_utc)
            );
            CREATE INDEX IF NOT EXISTS idx_forecast_eval_calibration
            ON weather_forecast_evaluations(city, forecast_model, target_date, sample_local_minute);
            """
        )
        self.db.commit()
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(weather_forecast_evaluations)")}
        if "sample_local_date" not in columns:
            self.db.execute("ALTER TABLE weather_forecast_evaluations ADD COLUMN sample_local_date TEXT")
            self.db.commit()

    @staticmethod
    def _final_settlement_c(row: sqlite3.Row) -> float | None:
        if (
            "exact_at_resolution_precision" in row.keys()
            and int(row["exact_at_resolution_precision"] or 0) == 1
        ):
            official = as_float(row["official_temperature_c"])
            if official is not None:
                return official
        low, high = as_float(row["bucket_low"]), as_float(row["bucket_high"])
        if low is None or high is None or abs(low - high) > 1e-9:
            return None
        unit = str(row["bucket_unit"] or "C").upper()
        return fahrenheit_to_celsius(low) if unit == "F" else low

    @staticmethod
    def _sample_local(slot_utc: str, timezone_name: str) -> tuple[str | None, str | None, int | None, float | None]:
        instant = parse_ts(slot_utc)
        if instant is None:
            return None, None, None, None
        try:
            local = instant.astimezone(ZoneInfo(timezone_name))
        except Exception:
            return None, None, None, None
        local_iso = local.isoformat(timespec="minutes")
        minute = local.hour * 60 + local.minute
        day_end = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=local.tzinfo)
        lead_hours = (day_end - local).total_seconds() / 3600.0
        return local.date().isoformat(), local_iso, minute, lead_hours

    def refresh(self) -> int:
        """Incrementally materialize new or newly resolved forecast snapshots."""
        has_labels = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='weather_resolution_labels'"
        ).fetchone() is not None
        label_columns = (
            "l.official_temperature_c,l.exact_at_resolution_precision"
            if has_labels else "NULL AS official_temperature_c,0 AS exact_at_resolution_precision"
        )
        label_join = (
            "LEFT JOIN weather_resolution_labels l ON l.event_id=e.event_id"
            if has_labels else ""
        )
        rows = self.db.execute(
            f"""
            WITH source_rows AS (
                SELECT e.event_id,e.city,e.station_id,e.target_date,e.resolved_at_utc,
                       s.timezone,m.bucket_low,m.bucket_high,m.bucket_unit,
                       {label_columns},
                       'meteoblue' AS forecast_model,w.slot_utc,
                       NULL AS issued_at,w.forecast_max_c
                FROM windy_forecasts w JOIN events e
                  ON e.station_id=w.station_id AND e.target_date=w.target_date
                JOIN stations s ON s.station_id=e.station_id
                LEFT JOIN markets m ON m.market_id=e.winning_market_id
                {label_join}
                WHERE w.model='mblue' AND w.status='ok'
                UNION ALL
                SELECT e.event_id,e.city,e.station_id,e.target_date,e.resolved_at_utc,
                       s.timezone,m.bucket_low,m.bucket_high,m.bucket_unit,
                       {label_columns},
                       f.model AS forecast_model,f.slot_utc,NULL AS issued_at,
                       f.forecast_max_c
                FROM external_forecasts f JOIN events e
                  ON e.station_id=f.station_id AND e.target_date=f.target_date
                JOIN stations s ON s.station_id=e.station_id
                LEFT JOIN markets m ON m.market_id=e.winning_market_id
                {label_join}
                WHERE f.status='ok'
            )
            SELECT source_rows.*
            FROM source_rows
            LEFT JOIN weather_forecast_evaluations v
              ON v.event_id=source_rows.event_id
             AND v.forecast_model=source_rows.forecast_model
             AND v.sample_slot_utc=source_rows.slot_utc
            WHERE v.event_id IS NULL
               OR v.forecast_max_c IS NOT source_rows.forecast_max_c
               OR (
                    source_rows.exact_at_resolution_precision=1
                    AND source_rows.official_temperature_c IS NOT NULL
                    AND (
                        v.resolution_exact=0
                        OR v.final_settlement_c IS NOT source_rows.official_temperature_c
                    )
               )
               OR (
                    source_rows.resolved_at_utc IS NOT NULL
                    AND source_rows.bucket_low IS NOT NULL
                    AND source_rows.bucket_high IS NOT NULL
                    AND ABS(source_rows.bucket_low-source_rows.bucket_high)<=1e-9
                    AND v.resolution_exact=0
               )
            """
        ).fetchall()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        count = 0
        for row in rows:
            forecast = as_float(row["forecast_max_c"])
            if forecast is None:
                continue
            final = self._final_settlement_c(row)
            local_date, local_iso, local_minute, lead_hours = self._sample_local(row["slot_utc"], row["timezone"])
            signed = forecast - final if final is not None else None
            absolute = abs(signed) if signed is not None else None
            squared = signed * signed if signed is not None else None
            rounded_hit = int(round_half_up(forecast) == round_half_up(final)) if final is not None else None
            within_one = int(absolute <= 1.0 + 1e-9) if absolute is not None else None
            self.db.execute(
                """
                INSERT INTO weather_forecast_evaluations(
                    event_id,city,station_id,target_date,forecast_model,sample_slot_utc,
                    forecast_issued_at_utc,sample_local_date,sample_local_time,sample_local_minute,lead_hours,
                    forecast_max_c,final_settlement_c,signed_error_c,absolute_error_c,squared_error_c,
                    rounded_hit,within_one_c,resolution_exact,created_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id,forecast_model,sample_slot_utc) DO UPDATE SET
                    forecast_issued_at_utc=excluded.forecast_issued_at_utc,
                    sample_local_date=excluded.sample_local_date,
                    sample_local_time=excluded.sample_local_time,sample_local_minute=excluded.sample_local_minute,
                    lead_hours=excluded.lead_hours,forecast_max_c=excluded.forecast_max_c,
                    final_settlement_c=excluded.final_settlement_c,signed_error_c=excluded.signed_error_c,
                    absolute_error_c=excluded.absolute_error_c,squared_error_c=excluded.squared_error_c,
                    rounded_hit=excluded.rounded_hit,within_one_c=excluded.within_one_c,
                    resolution_exact=excluded.resolution_exact,created_at_utc=excluded.created_at_utc
                """,
                (
                    row["event_id"], row["city"], row["station_id"], row["target_date"],
                    row["forecast_model"], row["slot_utc"], row["issued_at"], local_date, local_iso,
                    local_minute, lead_hours, forecast, final, signed, absolute, squared,
                    rounded_hit, within_one, int(final is not None), now,
                ),
            )
            count += 1
        self.db.commit()
        return count

    def calibration(self, city: str, target_date: str, local_minute: int) -> list[dict[str, Any]]:
        """Return one latest same-day snapshot per prior event/model.

        The target date is strictly earlier than the current event, and only
        snapshots observed by the equivalent local time are used. This avoids
        resolution leakage and half-hour sample duplication.
        """
        rows = self.db.execute(
            """
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY event_id,forecast_model
                    ORDER BY sample_local_minute DESC, sample_slot_utc DESC
                ) AS rn
                FROM weather_forecast_evaluations
                WHERE city=? AND target_date<? AND resolution_exact=1
                  AND sample_local_date=target_date AND sample_local_minute<=?
            )
            SELECT forecast_model,COUNT(*) AS sample_count,
                   AVG(absolute_error_c) AS mae,
                   AVG(signed_error_c) AS bias,
                   SQRT(AVG(squared_error_c)) AS rmse,
                   AVG(rounded_hit) AS rounded_accuracy,
                   AVG(within_one_c) AS within_one_accuracy
            FROM ranked WHERE rn=1 GROUP BY forecast_model ORDER BY forecast_model
            """,
            (city, target_date, int(local_minute)),
        ).fetchall()
        return [
            {
                "model": row["forecast_model"],
                "sampleCount": int(row["sample_count"]),
                "maeC": round(float(row["mae"]), 3),
                "biasC": round(float(row["bias"]), 3),
                "rmseC": round(float(row["rmse"]), 3),
                "roundedAccuracy": round(float(row["rounded_accuracy"]), 3),
                "withinOneAccuracy": round(float(row["within_one_accuracy"]), 3),
                "sampleSufficient": int(row["sample_count"]) >= 10,
            }
            for row in rows
        ]

    def write_reports(self) -> None:
        if not self.report_dir:
            return
        self.report_dir.mkdir(parents=True, exist_ok=True)
        rows = [dict(row) for row in self.db.execute(
            "SELECT * FROM weather_forecast_evaluations ORDER BY target_date,city,forecast_model,sample_slot_utc"
        ).fetchall()]
        fields = list(rows[0]) if rows else [
            "event_id", "city", "station_id", "target_date", "forecast_model", "sample_slot_utc",
            "forecast_issued_at_utc", "sample_local_date", "sample_local_time", "sample_local_minute", "lead_hours",
            "forecast_max_c", "final_settlement_c", "signed_error_c", "absolute_error_c",
            "squared_error_c", "rounded_hit", "within_one_c", "resolution_exact", "created_at_utc",
        ]
        with (self.report_dir / "forecast_evaluations_latest.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row["resolution_exact"]:
                grouped[(row["city"], row["forecast_model"])].append(row)
        summary = []
        for (city, model), items in sorted(grouped.items()):
            by_event = {}
            for item in items:
                by_event.setdefault(item["event_id"], item)
            sample = list(by_event.values())
            errors = [float(item["absolute_error_c"]) for item in sample if item["absolute_error_c"] is not None]
            signed = [float(item["signed_error_c"]) for item in sample if item["signed_error_c"] is not None]
            summary.append({
                "city": city, "model": model, "eventDays": len(sample),
                "maeC": round(sum(errors) / len(errors), 3) if errors else None,
                "biasC": round(sum(signed) / len(signed), 3) if signed else None,
                "roundedAccuracy": round(sum(int(item["rounded_hit"]) for item in sample) / len(sample), 3) if sample else None,
            })
        (self.report_dir / "forecast_calibration_latest.json").write_text(
            json.dumps({"generatedAtUtc": datetime.now(UTC).isoformat(timespec="seconds"), "summary": summary}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


__all__ = ["WeatherForecastEvaluator"]
