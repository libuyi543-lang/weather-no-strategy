#!/usr/bin/env python3
"""Point-in-time forecast freezes for leakage-safe model comparison."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


UTC = timezone.utc


def parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def round_half_up(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def content_hash(model: str, target_date: str, maximum: Any, peak: Any, points_json: Any) -> str:
    try:
        points = json.loads(points_json or "[]")
    except (TypeError, json.JSONDecodeError):
        points = []
    raw = json.dumps(
        {
            "model": model,
            "target_date": target_date,
            "max_c": as_float(maximum),
            "peak_local": peak,
            "points": points,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ForecastCutoffTracker:
    """Freeze only forecasts that were actually available by each cutoff."""

    def __init__(
        self,
        db: sqlite3.Connection,
        config: dict[str, Any],
        report_dir: str | Path | None = None,
    ):
        self.db = db
        self.config = config
        self.report_dir = Path(report_dir) if report_dir else None
        self.models = [
            item for item in config.get("forecastCutoffModels", [])
            if isinstance(item, dict) and item.get("source") and item.get("model")
        ]
        self.cutoffs = self._parse_cutoffs(config.get("forecastCutoffsLocal", []))
        self.cities = {
            str(city).strip().casefold()
            for city in config.get("processAnalysisCities", [])
            if str(city).strip()
        }
        self.init_schema()

    @staticmethod
    def _parse_cutoffs(values: Any) -> list[tuple[str, time]]:
        output: list[tuple[str, time]] = []
        for value in values if isinstance(values, list) else []:
            text = str(value).strip()
            try:
                parsed = time.fromisoformat(text)
            except ValueError:
                continue
            label = f"{parsed.hour:02d}{parsed.minute:02d}"
            output.append((label, parsed.replace(second=0, microsecond=0)))
        return output

    def init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS forecast_cutoff_snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                sample_local_date TEXT NOT NULL,
                timezone TEXT NOT NULL,
                source TEXT NOT NULL,
                model TEXT NOT NULL,
                snapshot_kind TEXT NOT NULL,
                snapshot_key TEXT NOT NULL,
                cutoff_label TEXT,
                scheduled_at_utc TEXT,
                captured_at_utc TEXT NOT NULL,
                capture_lag_seconds REAL,
                source_sample_slot_utc TEXT,
                model_run_time_utc TEXT,
                model_run_time_source TEXT,
                first_fetched_at_utc TEXT,
                arrival_latency_seconds REAL,
                source_age_seconds REAL,
                trigger_basis TEXT NOT NULL,
                version_hash TEXT,
                forecast_max_c REAL,
                forecast_peak_local TEXT,
                points_json TEXT,
                status TEXT NOT NULL,
                reason TEXT,
                final_settlement_c REAL,
                signed_error_c REAL,
                absolute_error_c REAL,
                rounded_bucket_hit INTEGER,
                within_one_c INTEGER,
                resolution_exact INTEGER NOT NULL DEFAULT 0,
                evaluated_at_utc TEXT,
                UNIQUE(event_id,source,model,snapshot_kind,snapshot_key)
            );
            CREATE INDEX IF NOT EXISTS idx_forecast_cutoff_evaluation
            ON forecast_cutoff_snapshots(model,cutoff_label,target_date,status);
            CREATE INDEX IF NOT EXISTS idx_forecast_cutoff_station
            ON forecast_cutoff_snapshots(station_id,target_date,snapshot_kind,captured_at_utc);
            """
        )
        self.db.commit()

    def _tracked_events(self, local_day: date) -> list[sqlite3.Row]:
        rows = self.db.execute(
            """
            SELECT e.event_id,e.city,e.station_id,e.target_date,s.timezone
            FROM events e JOIN stations s ON s.station_id=e.station_id
            WHERE e.station_id IS NOT NULL AND e.target_date>=?
            ORDER BY e.target_date,e.city
            """,
            (local_day.isoformat(),),
        ).fetchall()
        if not self.cities:
            return list(rows)
        return [row for row in rows if str(row["city"] or "").strip().casefold() in self.cities]

    def _provider_status(self, definition: dict[str, Any]) -> tuple[str | None, str | None]:
        if str(definition.get("source")) != "cma_meso":
            return None, None
        path = Path(os.path.expanduser(str(
            self.config.get("cma", {}).get(
                "credentialFile", "~/.config/weather-market-monitor/cma_api.json"
            )
        )))
        if not path.exists():
            return "provider_unconfigured", "CMA credentials are not configured"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "provider_unconfigured", "CMA credential file is unreadable"
        products = payload.get("products") if isinstance(payload, dict) else None
        mapping = products.get("cma_meso") if isinstance(products, dict) else None
        if not isinstance(mapping, dict) or not mapping.get("interfaceId"):
            return "provider_unconfigured", "CMA-MESO interfaceId mapping is not configured"
        response = mapping.get("response")
        required = ("rowsPath", "validTimeField", "temperatureField")
        if not isinstance(response, dict) or any(not response.get(key) for key in required):
            return "provider_unconfigured", "CMA-MESO response field mapping is incomplete"
        return None, None

    def _latest_available(
        self,
        event: sqlite3.Row,
        definition: dict[str, Any],
        scheduled_at: datetime,
    ) -> sqlite3.Row | None:
        return self.db.execute(
            """
            SELECT slot_utc,fetched_at_utc,forecast_max_c,forecast_peak_local,points_json,
                   status,error
            FROM external_forecasts
            WHERE station_id=? AND target_date=? AND source=? AND model=?
              AND status='ok' AND forecast_max_c IS NOT NULL
              AND slot_utc<=? AND fetched_at_utc<=?
            ORDER BY fetched_at_utc DESC,slot_utc DESC LIMIT 1
            """,
            (
                event["station_id"], event["target_date"], definition["source"],
                definition["model"], scheduled_at.isoformat(timespec="seconds"),
                scheduled_at.isoformat(timespec="seconds"),
            ),
        ).fetchone()

    def _model_run_audit(
        self,
        event: sqlite3.Row,
        definition: dict[str, Any],
        version_hash: str,
    ) -> sqlite3.Row | None:
        return self.db.execute(
            """
            SELECT model_run_time_utc,run_time_source,first_seen_utc
            FROM forecast_model_runs
            WHERE source=? AND station_id=? AND target_date=? AND model=? AND version_hash=?
            LIMIT 1
            """,
            (
                definition["source"], event["station_id"], event["target_date"],
                definition["model"], version_hash,
            ),
        ).fetchone()

    def capture_due(self, slot: datetime, captured_at: datetime | None = None) -> int:
        if not self.models or not self.cutoffs:
            return 0
        slot = slot.astimezone(UTC)
        captured_at = (captured_at or datetime.now(UTC)).astimezone(UTC)
        written = 0
        timezone_names = {
            row["timezone"] for row in self.db.execute(
                "SELECT DISTINCT timezone FROM stations WHERE timezone IS NOT NULL"
            ).fetchall()
        }
        for timezone_name in timezone_names:
            try:
                local_tz = ZoneInfo(str(timezone_name))
            except Exception:
                continue
            local_slot = slot.astimezone(local_tz)
            events = [
                row for row in self._tracked_events(local_slot.date())
                if row["timezone"] == timezone_name
            ]
            for cutoff_label, cutoff_time in self.cutoffs:
                scheduled_local = datetime.combine(local_slot.date(), cutoff_time, tzinfo=local_tz)
                scheduled_at = scheduled_local.astimezone(UTC)
                if slot < scheduled_at:
                    continue
                snapshot_key = f"{local_slot.date().isoformat()}:{cutoff_label}"
                for event in events:
                    for definition in self.models:
                        existing = self.db.execute(
                            """
                            SELECT 1 FROM forecast_cutoff_snapshots
                            WHERE event_id=? AND source=? AND model=?
                              AND snapshot_kind='fixed_cutoff' AND snapshot_key=?
                            """,
                            (
                                event["event_id"], definition["source"], definition["model"],
                                snapshot_key,
                            ),
                        ).fetchone()
                        if existing:
                            continue
                        status, reason = self._provider_status(definition)
                        forecast = None if status else self._latest_available(event, definition, scheduled_at)
                        version_hash = None
                        audit = None
                        if forecast:
                            version_hash = content_hash(
                                definition["model"], event["target_date"],
                                forecast["forecast_max_c"], forecast["forecast_peak_local"],
                                forecast["points_json"],
                            )
                            audit = self._model_run_audit(event, definition, version_hash)
                            status = "captured"
                        elif status is None:
                            status = "missing"
                            reason = "no successful forecast had been fetched by the cutoff"

                        forecast_fetch = parse_ts(forecast["fetched_at_utc"]) if forecast else None
                        audit_fetch = parse_ts(audit["first_seen_utc"]) if audit else None
                        known_fetches = [value for value in (forecast_fetch, audit_fetch) if value]
                        first_fetch_dt = min(known_fetches) if known_fetches else None
                        first_fetched = (
                            first_fetch_dt.isoformat(timespec="seconds") if first_fetch_dt else None
                        )
                        run_time = audit["model_run_time_utc"] if audit else None
                        run_source = audit["run_time_source"] if audit else None
                        first_dt, run_dt = first_fetch_dt, parse_ts(run_time)
                        arrival_latency = (
                            (first_dt - run_dt).total_seconds()
                            if first_dt is not None and run_dt is not None else None
                        )
                        source_age = (
                            (scheduled_at - first_dt).total_seconds()
                            if first_dt is not None else None
                        )
                        self.db.execute(
                            """
                            INSERT INTO forecast_cutoff_snapshots(
                                event_id,city,station_id,target_date,sample_local_date,timezone,
                                source,model,snapshot_kind,snapshot_key,cutoff_label,scheduled_at_utc,
                                captured_at_utc,capture_lag_seconds,source_sample_slot_utc,
                                model_run_time_utc,model_run_time_source,first_fetched_at_utc,
                                arrival_latency_seconds,source_age_seconds,trigger_basis,version_hash,
                                forecast_max_c,forecast_peak_local,points_json,status,reason
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                event["event_id"], event["city"], event["station_id"],
                                event["target_date"], local_slot.date().isoformat(), timezone_name,
                                definition["source"], definition["model"], "fixed_cutoff",
                                snapshot_key, cutoff_label, scheduled_at.isoformat(timespec="seconds"),
                                captured_at.isoformat(timespec="seconds"),
                                max(0.0, (captured_at - scheduled_at).total_seconds()),
                                forecast["slot_utc"] if forecast else None, run_time, run_source,
                                first_fetched, arrival_latency, source_age, "scheduled_cutoff",
                                version_hash, forecast["forecast_max_c"] if forecast else None,
                                forecast["forecast_peak_local"] if forecast else None,
                                forecast["points_json"] if forecast else None, status, reason,
                            ),
                        )
                        written += 1
        self.db.commit()
        return written

    def capture_new_run(
        self,
        *,
        source: str,
        model: str,
        station_id: str,
        target_date: str,
        sample_slot_utc: str,
        fetched_at_utc: str,
        version_hash: str,
        forecast_max_c: float,
        forecast_peak_local: str | None,
        points_json: str,
        model_run_time_utc: str | None = None,
        model_run_time_source: str | None = None,
    ) -> int:
        if not any(item["source"] == source and item["model"] == model for item in self.models):
            return 0
        events = self.db.execute(
            """
            SELECT e.event_id,e.city,e.station_id,e.target_date,s.timezone
            FROM events e JOIN stations s ON s.station_id=e.station_id
            WHERE e.station_id=? AND e.target_date=?
            """,
            (station_id, target_date),
        ).fetchall()
        fetched = parse_ts(fetched_at_utc) or datetime.now(UTC)
        model_run = parse_ts(model_run_time_utc)
        latency = (fetched - model_run).total_seconds() if model_run else None
        written = 0
        for event in events:
            if self.cities and str(event["city"] or "").strip().casefold() not in self.cities:
                continue
            local_date = fetched.astimezone(ZoneInfo(event["timezone"])).date().isoformat()
            cursor = self.db.execute(
                """
                INSERT OR IGNORE INTO forecast_cutoff_snapshots(
                    event_id,city,station_id,target_date,sample_local_date,timezone,
                    source,model,snapshot_kind,snapshot_key,captured_at_utc,
                    source_sample_slot_utc,model_run_time_utc,model_run_time_source,
                    first_fetched_at_utc,arrival_latency_seconds,trigger_basis,version_hash,
                    forecast_max_c,forecast_peak_local,points_json,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event["event_id"], event["city"], station_id, target_date, local_date,
                    event["timezone"], source, model, "new_run", version_hash,
                    fetched.isoformat(timespec="seconds"), sample_slot_utc, model_run_time_utc,
                    model_run_time_source, fetched.isoformat(timespec="seconds"), latency,
                    "model_run" if model_run else "forecast_content_change", version_hash,
                    forecast_max_c, forecast_peak_local, points_json, "captured",
                ),
            )
            written += int(cursor.rowcount > 0)
        return written

    def refresh_evaluations(self) -> int:
        has_labels = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='weather_resolution_labels'"
        ).fetchone()
        if not has_labels:
            return 0
        rows = self.db.execute(
            """
            SELECT s.snapshot_id,s.forecast_max_c,l.official_temperature_c
            FROM forecast_cutoff_snapshots s
            JOIN weather_resolution_labels l ON l.event_id=s.event_id
            WHERE l.exact_at_resolution_precision=1
              AND l.official_temperature_c IS NOT NULL
              AND s.status='captured' AND s.forecast_max_c IS NOT NULL
              AND (
                  s.resolution_exact=0
                  OR s.final_settlement_c IS NOT l.official_temperature_c
              )
            """
        ).fetchall()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        count = 0
        for row in rows:
            final = as_float(row["official_temperature_c"])
            forecast = as_float(row["forecast_max_c"])
            if final is None:
                continue
            if forecast is None:
                continue
            signed = forecast - final
            absolute = abs(signed)
            self.db.execute(
                """
                UPDATE forecast_cutoff_snapshots SET final_settlement_c=?,signed_error_c=?,
                    absolute_error_c=?,rounded_bucket_hit=?,within_one_c=?,resolution_exact=1,
                    evaluated_at_utc=? WHERE snapshot_id=?
                """,
                (
                    final, signed, absolute,
                    int(round_half_up(forecast) == round_half_up(final)),
                    int(absolute <= 1.0 + 1e-9), now, row["snapshot_id"],
                ),
            )
            count += 1
        self.db.commit()
        return count

    def write_reports(self) -> dict[str, Any]:
        if not self.report_dir:
            return {}
        self.report_dir.mkdir(parents=True, exist_ok=True)
        rows = [dict(row) for row in self.db.execute(
            "SELECT * FROM forecast_cutoff_snapshots ORDER BY target_date,city,model,captured_at_utc"
        ).fetchall()]
        csv_path = self.report_dir / "forecast_cutoff_snapshots_latest.csv"
        if rows:
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row["snapshot_kind"] == "fixed_cutoff":
                grouped[(row["source"], row["model"], row["cutoff_label"] or "")].append(row)
        summary = []
        city_bias = []
        for (source, model, cutoff), items in sorted(grouped.items()):
            captured = [item for item in items if item["status"] == "captured"]
            evaluated = [item for item in captured if item["resolution_exact"]]
            errors = [float(item["absolute_error_c"]) for item in evaluated]
            signed = [float(item["signed_error_c"]) for item in evaluated]
            summary.append({
                "source": source,
                "model": model,
                "cutoff": cutoff,
                "rows": len(items),
                "independentTargetDates": len({item["target_date"] for item in items}),
                "capturedRows": len(captured),
                "missingRate": round(1.0 - len(captured) / len(items), 4) if items else None,
                "evaluatedRows": len(evaluated),
                "evaluatedIndependentDates": len({item["target_date"] for item in evaluated}),
                "maeC": round(sum(errors) / len(errors), 3) if errors else None,
                "biasC": round(sum(signed) / len(signed), 3) if signed else None,
                "roundedBucketHitRate": (
                    round(sum(int(item["rounded_bucket_hit"]) for item in evaluated) / len(evaluated), 4)
                    if evaluated else None
                ),
                "withinOneCRate": (
                    round(sum(int(item["within_one_c"]) for item in evaluated) / len(evaluated), 4)
                    if evaluated else None
                ),
            })
            by_city: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in evaluated:
                by_city[item["city"]].append(item)
            for city, city_items in sorted(by_city.items()):
                city_bias.append({
                    "source": source,
                    "model": model,
                    "cutoff": cutoff,
                    "city": city,
                    "independentTargetDates": len({item["target_date"] for item in city_items}),
                    "biasC": round(
                        sum(float(item["signed_error_c"]) for item in city_items) / len(city_items), 3
                    ),
                })
        new_runs = [row for row in rows if row["snapshot_kind"] == "new_run"]
        latencies = [
            float(row["arrival_latency_seconds"])
            for row in new_runs if row["arrival_latency_seconds"] is not None
        ]
        report = {
            "generatedAtUtc": datetime.now(UTC).isoformat(timespec="seconds"),
            "pointInTimeRule": "fixed cutoffs may only use forecasts fetched at or before scheduled_at_utc",
            "cutoffsLocal": [label for label, _value in self.cutoffs],
            "models": self.models,
            "summary": summary,
            "cityBias": city_bias,
            "newRunAudit": {
                "rows": len(new_runs),
                "independentTargetDates": len({row["target_date"] for row in new_runs}),
                "runTimeKnownRows": len(latencies),
                "meanArrivalLatencySeconds": round(sum(latencies) / len(latencies), 1) if latencies else None,
            },
        }
        (self.report_dir / "forecast_cutoff_status_latest.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report


__all__ = ["ForecastCutoffTracker", "content_hash"]
