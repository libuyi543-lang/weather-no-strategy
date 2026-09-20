from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


UTC = timezone.utc


SOURCE_THRESHOLDS_SECONDS = {
    "meteoblue": 12 * 3600,
    "ecmwf": 12 * 3600,
    "metar": 2 * 3600,
    "fast_metar": 2 * 3600,
    "rainviewer": 30 * 60,
    "nict_himawari": 75 * 60,
    "jaxa_swr": 45 * 60,
    "jaxa_cloud": 45 * 60,
}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def init_shadow_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS shadow_source_snapshots (
            slot_utc TEXT NOT NULL,station_id TEXT NOT NULL,target_date TEXT NOT NULL,
            source TEXT NOT NULL,status TEXT NOT NULL,source_time_utc TEXT,
            source_age_seconds REAL,version_hash TEXT,details_json TEXT NOT NULL,
            captured_at_utc TEXT NOT NULL,
            PRIMARY KEY(slot_utc,station_id,target_date,source)
        );
        CREATE INDEX IF NOT EXISTS idx_shadow_source_date
            ON shadow_source_snapshots(target_date,station_id,source,slot_utc);
        CREATE TABLE IF NOT EXISTS shadow_ablation_predictions (
            variant TEXT NOT NULL,station_id TEXT NOT NULL,target_date TEXT NOT NULL,
            cutoff_local TEXT NOT NULL,signal_time_utc TEXT NOT NULL,
            predicted_max_c REAL,top_buckets_json TEXT,probabilities_json TEXT,
            is_oos INTEGER NOT NULL DEFAULT 0,trained_through_date TEXT,
            market_first_90_at_utc TEXT,lead_minutes REAL,
            executable_price REAL,hypothetical_5share_pnl REAL,
            payload_json TEXT NOT NULL,created_at_utc TEXT NOT NULL,
            PRIMARY KEY(variant,station_id,target_date,cutoff_local,signal_time_utc)
        );
        """
    )


def _latest(db: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
    return db.execute(query, params).fetchone()


def _entry(
    source: str,
    row: sqlite3.Row | None,
    source_time_field: str,
    slot: datetime,
    details: dict[str, Any] | None = None,
    version_hash: str | None = None,
) -> dict[str, Any]:
    if row is None:
        return {"source": source, "status": "missing", "details": details or {}}
    source_time = _parse_time(row[source_time_field])
    age = (slot - source_time).total_seconds() if source_time else None
    status = str(row["status"] if "status" in row.keys() else "ok")
    if status == "ok":
        status = "available"
    if status == "available" and age is not None and age > SOURCE_THRESHOLDS_SECONDS.get(source, 24 * 3600):
        status = "stale"
    payload = details or {key: row[key] for key in row.keys()}
    return {
        "source": source,
        "status": status,
        "source_time_utc": _iso(source_time) if source_time else None,
        "source_age_seconds": round(age, 1) if age is not None else None,
        "version_hash": version_hash or _hash(payload),
        "details": payload,
    }


def capture_shadow_sources(
    db: sqlite3.Connection,
    slot: datetime,
    tracked_rows: list[sqlite3.Row] | list[dict[str, Any]],
) -> int:
    init_shadow_schema(db)
    slot_text = _iso(slot)
    captured_at = _iso(datetime.now(UTC))
    unique = {
        (str(row["station_id"]), str(row["target_date"])): row
        for row in tracked_rows
        if row["station_id"] and row["target_date"]
    }
    written = 0
    for (station_id, target_date), _item in unique.items():
        entries: list[dict[str, Any]] = []
        mblue = _latest(
            db,
            "SELECT status,slot_utc,model_ref_time_utc,model_updated_at_utc,forecast_max_c,points_json "
            "FROM windy_forecasts WHERE station_id=? AND target_date=? AND slot_utc<=? "
            "ORDER BY slot_utc DESC LIMIT 1",
            (station_id, target_date, slot_text),
        )
        entries.append(_entry("meteoblue", mblue, "slot_utc", slot))
        ecmwf = _latest(
            db,
            "SELECT status,slot_utc,forecast_max_c,forecast_peak_local,points_json "
            "FROM external_forecasts WHERE station_id=? AND target_date=? AND model='ecmwf_ifs025' "
            "AND status='ok' AND slot_utc<=? ORDER BY slot_utc DESC LIMIT 1",
            (station_id, target_date, slot_text),
        )
        entries.append(_entry("ecmwf", ecmwf, "slot_utc", slot))
        metar = _latest(
            db,
            "SELECT status,observation_time_utc,temperature_c,dewpoint_c,wind_direction_deg,"
            "wind_speed,raw_metar FROM weather_observations WHERE station_id=? AND source='metar' "
            "AND slot_utc<=? ORDER BY observation_time_utc DESC LIMIT 1",
            (station_id, slot_text),
        )
        entries.append(_entry("metar", metar, "observation_time_utc", slot))
        fast_metar = _latest(
            db,
            "SELECT 'ok' AS status,observation_time_utc,report_time_utc,receipt_time_utc,"
            "metar_type,raw_metar,first_fetched_at_utc FROM fast_metar_reports "
            "WHERE station_id=? AND observation_time_utc<=? ORDER BY observation_time_utc DESC LIMIT 1",
            (station_id, slot_text),
        )
        entries.append(_entry("fast_metar", fast_metar, "observation_time_utc", slot))
        for source, stored_source in (
            ("rainviewer", "rainviewer"),
            ("nict_himawari", "nict_himawari_true_colour"),
            ("jaxa_swr", "jaxa_himawari_swr_l2"),
            ("jaxa_cloud", "jaxa_himawari_cloud_l2"),
        ):
            remote = _latest(
                db,
                "SELECT status,frame_time_utc,quality,features_json,raw_sha256,error "
                "FROM remote_sensing_snapshots WHERE station_id=? AND source=? AND slot_utc<=? "
                "ORDER BY slot_utc DESC LIMIT 1",
                (station_id, stored_source, slot_text),
            )
            details = None
            digest = None
            if remote:
                try:
                    features = json.loads(remote["features_json"] or "{}")
                except json.JSONDecodeError:
                    features = {}
                details = {"quality": remote["quality"], "features": features, "error": remote["error"]}
                digest = remote["raw_sha256"]
            entries.append(_entry(source, remote, "frame_time_utc", slot, details, digest))

        for source, product in (
            ("cma_station", "automatic_station"),
            ("cma_radar", "radar_composite_reflectivity"),
            ("cma_radiation", "surface_solar_radiation"),
        ):
            probe = _latest(
                db,
                "SELECT status,checked_at_utc,endpoint,latency_ms,detail FROM source_access_probes "
                "WHERE source='cma' AND product=? ORDER BY checked_at_utc DESC LIMIT 1",
                (product,),
            )
            entries.append(_entry(source, probe, "checked_at_utc", slot))

        for entry in entries:
            db.execute(
                """
                INSERT INTO shadow_source_snapshots(
                    slot_utc,station_id,target_date,source,status,source_time_utc,
                    source_age_seconds,version_hash,details_json,captured_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(slot_utc,station_id,target_date,source) DO UPDATE SET
                    status=excluded.status,source_time_utc=excluded.source_time_utc,
                    source_age_seconds=excluded.source_age_seconds,version_hash=excluded.version_hash,
                    details_json=excluded.details_json,captured_at_utc=excluded.captured_at_utc
                """,
                (
                    slot_text, station_id, target_date, entry["source"], entry["status"],
                    entry.get("source_time_utc"), entry.get("source_age_seconds"),
                    entry.get("version_hash"),
                    json.dumps(entry.get("details") or {}, ensure_ascii=False, separators=(",", ":")),
                    captured_at,
                ),
            )
            written += 1
    db.commit()
    return written
