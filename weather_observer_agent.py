#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import signal
import sqlite3
import time
from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "weather_observer_config.json"
SCHEMA_PATH = ROOT / "weather_observer.schema.json"
UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(UTC).isoformat(timespec="seconds")


def parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def json_value(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return fallback


def as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def rounded(value: Any, digits: int = 2) -> float | None:
    number = as_float(value)
    return round(number, digits) if number is not None else None


def heating_phase(hours: Any) -> str:
    remaining = as_float(hours)
    if remaining is None:
        return "unclear"
    if remaining <= 0:
        return "ended"
    if remaining <= 1.5:
        return "capping"
    return "active"


def feature_changed(old: dict[str, Any], new: dict[str, Any], keys: tuple[str, ...], threshold: float) -> bool:
    for key in keys:
        before, after = as_float(old.get(key)), as_float(new.get(key))
        if before is not None and after is not None and abs(after - before) >= threshold:
            return True
    return False


def numeric_band(value: Any, boundaries: tuple[float, ...]) -> int | None:
    number = as_float(value)
    if number is None:
        return None
    return sum(number >= boundary for boundary in boundaries)


def crossed_band(old: dict[str, Any], new: dict[str, Any], key: str, boundaries: tuple[float, ...]) -> bool:
    before, after = numeric_band(old.get(key), boundaries), numeric_band(new.get(key), boundaries)
    return before is not None and after is not None and before != after


def material_trigger_types(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    if not previous:
        return ["initial_state"]
    triggers: list[str] = []
    if previous.get("primaryObservationTimeUtc") != current.get("primaryObservationTimeUtc"):
        triggers.append("primary_metar")
    if set(previous.get("detectedProcesses") or []) != set(current.get("detectedProcesses") or []):
        triggers.append("weather_process")
    if previous.get("heatingPhase") != current.get("heatingPhase"):
        triggers.append("radiation_regime")
    if feature_changed(
        previous.get("radiation") or {}, current.get("radiation") or {},
        ("solarRadiationWm2", "directRadiationWm2"),
        float(config.get("radiationChangeWm2", 100)),
    ):
        triggers.append("radiation_regime")
    old_models, new_models = previous.get("models") or {}, current.get("models") or {}
    for model in set(old_models) | set(new_models):
        before, after = old_models.get(model) or {}, new_models.get(model) or {}
        if before.get("referenceTimeUtc") != after.get("referenceTimeUtc") and after.get("referenceTimeUtc"):
            triggers.append("model_revision")
            break
        if before.get("contentSignature") and before.get("contentSignature") != after.get("contentSignature"):
            triggers.append("model_revision")
            break
        old_max, new_max = as_float(before.get("forecastMaxC")), as_float(after.get("forecastMaxC"))
        if old_max is not None and new_max is not None and abs(new_max - old_max) >= float(config.get("modelMaxChangeC", 0.25)):
            triggers.append("model_revision")
            break
    old_network, new_network = previous.get("stationNetwork") or {}, current.get("stationNetwork") or {}
    network_updated = old_network.get("latestObservationUtc") != new_network.get("latestObservationUtc")
    network_mechanism_changed = (
        set(old_network.get("upwindStationIds") or []) != set(new_network.get("upwindStationIds") or [])
        or feature_changed(old_network, new_network, ("upwindTemperatureMinusPrimaryC",), 0.5)
    )
    if network_updated and network_mechanism_changed:
        triggers.append("upwind_change")
    old_remote, new_remote = previous.get("remoteSensing") or {}, current.get("remoteSensing") or {}
    old_radar, new_radar = old_remote.get("rainviewer") or {}, new_remote.get("rainviewer") or {}
    radar_regime_changed = (
        crossed_band(old_radar, new_radar, "nearestEchoKm", (25, 50, 100, 150))
        or any(
            crossed_band(old_radar, new_radar, key, (0.005, 0.02, 0.05))
            for key in ("echoCoverage25Km", "echoCoverage50Km", "echoCoverage100Km", "upwindEchoCoverage150Km")
        )
    )
    if old_radar.get("frameTimeUtc") != new_radar.get("frameTimeUtc") and radar_regime_changed:
        triggers.append("radar_arrival")
    old_sat, new_sat = old_remote.get("himawari") or {}, new_remote.get("himawari") or {}
    satellite_regime_changed = any(
        crossed_band(old_sat, new_sat, key, (0.2, 0.5, 0.8))
        for key in ("cloudProxyCoverage25Km", "cloudProxyCoverage50Km", "deepCloudProxyCoverage50Km")
    )
    if old_sat.get("frameTimeUtc") != new_sat.get("frameTimeUtc") and satellite_regime_changed:
        triggers.append("satellite_regime")
    return list(dict.fromkeys(triggers))


class WeatherObserverAgent:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        db_path = Path(str(config["databasePath"]))
        if not db_path.is_absolute():
            db_path = ROOT / db_path
        self.db = sqlite3.connect(db_path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._init_schema()

    def close(self) -> None:
        self.db.close()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS weather_observer_city_states (
                event_id TEXT PRIMARY KEY,
                city TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                state_version INTEGER NOT NULL,
                source_run_id INTEGER NOT NULL,
                source_slot_utc TEXT NOT NULL,
                raw_state_json TEXT NOT NULL,
                semantic_state_json TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS weather_observer_events (
                observer_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                city TEXT NOT NULL,
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                source_run_id INTEGER NOT NULL,
                source_slot_utc TEXT NOT NULL,
                trigger_types_json TEXT NOT NULL,
                source_observed_at_utc TEXT,
                source_published_at_utc TEXT,
                source_fetched_at_utc TEXT,
                market_snapshot_at_utc TEXT,
                observer_started_at_utc TEXT NOT NULL,
                observer_completed_at_utc TEXT,
                materiality TEXT,
                changed_process TEXT,
                prior_state_json TEXT,
                state_json TEXT,
                input_json TEXT,
                response_json TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                status TEXT NOT NULL,
                error TEXT,
                escalation_status TEXT NOT NULL DEFAULT 'none',
                trade_cycle_id INTEGER,
                trade_attempts INTEGER NOT NULL DEFAULT 0,
                trade_last_error TEXT,
                trade_retry_after_utc TEXT,
                UNIQUE(event_id,source_slot_utc)
            );
            CREATE INDEX IF NOT EXISTS idx_weather_observer_pending
            ON weather_observer_events(escalation_status,source_slot_utc);
            CREATE TABLE IF NOT EXISTS weather_observer_market_gates (
                event_id TEXT NOT NULL,
                weather_state_hash TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_range TEXT NOT NULL,
                outcome_side TEXT NOT NULL,
                first_opened_at_utc TEXT NOT NULL,
                executable_price REAL NOT NULL,
                PRIMARY KEY(event_id,weather_state_hash,market_id,outcome_side)
            );
            """
        )
        self.db.commit()

    def allowed_cities(self) -> set[str]:
        return {str(city).strip().casefold() for city in self.config.get("allowedCities", [])}

    def latest_candidates(self, now: datetime) -> list[dict[str, Any]]:
        allowed = self.allowed_cities()
        rows = self.db.execute(
            """
            WITH current_events AS (
                SELECT e.*,s.timezone,
                       ROW_NUMBER() OVER(PARTITION BY lower(e.city),e.target_date ORDER BY e.last_seen_utc DESC) AS rn
                FROM events e JOIN stations s ON s.station_id=e.station_id
                WHERE e.resolved_at_utc IS NULL AND e.target_date IS NOT NULL
            ), latest_process AS (
                SELECT w.*,
                       ROW_NUMBER() OVER(PARTITION BY w.station_id,w.target_date ORDER BY w.slot_utc DESC) AS rn
                FROM weather_process_states w JOIN runs r ON r.run_id=w.run_id AND r.status='completed'
            )
            SELECT e.event_id,e.city,e.station_id,e.station_name,e.target_date,e.timezone,
                   p.run_id,p.slot_utc,p.primary_observation_time_utc,p.state_json,p.created_at_utc
            FROM current_events e JOIN latest_process p
              ON p.station_id=e.station_id AND p.target_date=e.target_date AND p.rn=1
            WHERE e.rn=1 ORDER BY e.city
            """
        ).fetchall()
        output: list[dict[str, Any]] = []
        start, end = int(self.config.get("activeLocalStartHour", 7)), int(self.config.get("activeLocalEndHour", 19))
        for row in rows:
            if str(row["city"] or "").strip().casefold() not in allowed:
                continue
            try:
                local_now = now.astimezone(ZoneInfo(row["timezone"]))
                target = date.fromisoformat(row["target_date"])
            except (ValueError, ZoneInfoNotFoundError):
                continue
            if local_now.date() != target or not start <= local_now.hour < end:
                continue
            state = self.db.execute(
                "SELECT source_slot_utc FROM weather_observer_city_states WHERE event_id=?",
                (row["event_id"],),
            ).fetchone()
            if state and str(state["source_slot_utc"]) >= str(row["slot_utc"]):
                continue
            output.append(dict(row))
        return output

    def _latest_model_signatures(self, station_id: str, target_date: str, slot: str) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for table, model_name in (("windy_forecasts", "meteoblue"), ("external_forecasts", "ecmwf")):
            columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            ref_select = "model_ref_time_utc" if "model_ref_time_utc" in columns else "NULL AS model_ref_time_utc"
            updated_select = "model_updated_at_utc" if "model_updated_at_utc" in columns else "NULL AS model_updated_at_utc"
            row = self.db.execute(
                f"""
                SELECT model,forecast_max_c,points_json,{ref_select},{updated_select}
                FROM {table} WHERE station_id=? AND target_date=? AND status='ok' AND slot_utc<=?
                ORDER BY slot_utc DESC LIMIT 1
                """,
                (station_id, target_date, slot),
            ).fetchone()
            if row:
                output[model_name] = {
                    "model": row["model"], "forecastMaxC": rounded(row["forecast_max_c"]),
                    "referenceTimeUtc": row["model_ref_time_utc"],
                    "updatedAtUtc": row["model_updated_at_utc"],
                    "contentSignature": hashlib.sha256(str(row["points_json"] or "").encode()).hexdigest()[:16],
                }
        return output

    def compact_state(self, candidate: dict[str, Any]) -> dict[str, Any]:
        state = json_value(candidate.get("state_json"), {})
        remote = state.get("remoteSensing") or {}
        radar_payload = ((remote.get("rainviewer") or {}).get("features") or {})
        satellite_payload = ((remote.get("nict_himawari_true_colour") or {}).get("features") or {})
        slot = str(candidate["slot_utc"])
        radiation = self.db.execute(
            """
            SELECT solar_radiation_wm2,direct_radiation_wm2,diffuse_radiation_wm2,observation_time_utc
            FROM weather_observations WHERE station_id=? AND source='open_meteo_current'
              AND status='ok' AND slot_utc<=? ORDER BY slot_utc DESC LIMIT 1
            """,
            (candidate["station_id"], slot),
        ).fetchone()
        network_latest = self.db.execute(
            "SELECT MAX(observation_time_utc) FROM station_network_reports WHERE primary_station_id=?",
            (candidate["station_id"],),
        ).fetchone()[0]
        solar = state.get("solarHeating") or {}
        radar = remote.get("rainviewer") or {}
        satellite = remote.get("nict_himawari_true_colour") or {}
        return {
            "sourceSlotUtc": slot,
            "primaryObservationTimeUtc": state.get("primaryObservationTimeUtc"),
            "primaryStationTrend": state.get("primaryStationTrend") or {},
            "stationNetwork": {
                **(state.get("stationNetwork") or {}),
                "latestObservationUtc": network_latest,
            },
            "remoteSensing": {
                "rainviewer": {
                    "frameTimeUtc": radar.get("frameTimeUtc"),
                    **{key: rounded(radar_payload.get(key), 4) for key in (
                        "nearestEchoKm", "echoCoverage25Km", "echoCoverage50Km",
                        "echoCoverage100Km", "upwindEchoCoverage150Km",
                    )},
                },
                "himawari": {
                    "frameTimeUtc": satellite.get("frameTimeUtc"),
                    **{key: rounded(satellite_payload.get(key), 4) for key in (
                        "cloudProxyCoverage25Km", "cloudProxyCoverage50Km", "deepCloudProxyCoverage50Km",
                    )},
                },
            },
            "radiation": {
                "observationTimeUtc": radiation["observation_time_utc"] if radiation else None,
                "solarRadiationWm2": rounded(radiation["solar_radiation_wm2"] if radiation else None),
                "directRadiationWm2": rounded(radiation["direct_radiation_wm2"] if radiation else None),
                "diffuseRadiationWm2": rounded(radiation["diffuse_radiation_wm2"] if radiation else None),
            },
            "solarHeating": solar,
            "heatingPhase": heating_phase(solar.get("hoursUntilAstronomicalSunset")),
            "models": self._latest_model_signatures(candidate["station_id"], candidate["target_date"], slot),
            "modelRealityComparison": state.get("modelRealityComparison") or {},
            "detectedProcesses": state.get("detectedProcesses") or [],
            "processEvidence": state.get("processEvidence") or [],
            "qualityWarnings": state.get("qualityWarnings") or [],
        }

    def outcome_ranges(self, event_id: str) -> list[str]:
        return [
            str(row[0]) for row in self.db.execute(
                "SELECT outcome_range FROM markets WHERE event_id=? ORDER BY COALESCE(bucket_low,-999),COALESCE(bucket_high,999)",
                (event_id,),
            ).fetchall()
        ]

    def _api_key(self) -> str:
        path = Path(os.path.expanduser(str(self.config["apiKeyFile"])))
        if path.stat().st_mode & 0o077:
            raise RuntimeError("observer API key file permissions must be 600 or stricter")
        key = path.read_text(encoding="utf-8").strip()
        if not key:
            raise RuntimeError("observer API key file is empty")
        return key

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        value = text.strip()
        if value.startswith("```") and value.endswith("```"):
            value = value.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise RuntimeError("observer model did not return a JSON object")
        return parsed

    def _validate_response(self, response: dict[str, Any], ranges: list[str]) -> None:
        required = set(self.schema["required"])
        if set(response) != required:
            raise RuntimeError(f"observer response fields mismatch missing={sorted(required-set(response))} extra={sorted(set(response)-required)}")
        if response["materiality"] not in {"IGNORE", "UPDATE_STATE", "REANALYZE", "POSITION_ALERT"}:
            raise RuntimeError("observer materiality is invalid")
        if response["heatingStatus"] not in {"active", "capping", "ended", "unclear"}:
            raise RuntimeError("observer heatingStatus is invalid")
        allowed = set(ranges)
        for name in ("primaryBuckets", "plausibleBuckets", "tailBuckets", "excludedBuckets", "affectedBuckets"):
            value = response.get(name)
            if not isinstance(value, list) or any(str(item) not in allowed for item in value):
                raise RuntimeError(f"observer {name} contains an unknown bucket")
        if not isinstance(response.get("evidence"), list) or not isinstance(response.get("uncertain"), bool):
            raise RuntimeError("observer evidence/uncertain types are invalid")

    def call_ai(self, payload: dict[str, Any], ranges: list[str]) -> tuple[dict[str, Any], dict[str, int]]:
        prompt = (
            "You are the low-latency weather observer for a paper-only exact-temperature market system. "
            "You see weather evidence only, never market prices. Compare the previous semantic state with the new timestamped weather delta. "
            "Decide whether the plausible FINAL DAILY MAXIMUM buckets changed, not whether a raw number merely moved. "
            "Use IGNORE for noise, UPDATE_STATE when the process changed without changing actionable bucket paths, REANALYZE when a unique YES bucket may have emerged or a bucket may now be excluded from all primary/plausible paths, and POSITION_ALERT when new evidence may invalidate an open position. "
            "Do not assign exact probabilities. Never claim missing radar/satellite data exists. Use only bucket labels supplied in outcomeRanges. "
            "REANALYZE is only a request to wake the strong trader and never authorizes an order. Return exactly one JSON object with the required schema fields.\n\n"
            "Be concise: evidence must contain at most five short observable facts; summaries, invalidation, and escalation reason must each be one short sentence. "
            f"The exact top-level keys are: {','.join(self.schema['required'])}.\n\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
        attempts = 1 + int(self.config.get("invalidResponseRetries", 1))
        last_error: Exception | None = None
        prompt_tokens = completion_tokens = 0
        for attempt in range(attempts):
            retry_note = (
                "\n\nThe previous response was invalid. Follow the exact key list and JSON schema."
                if attempt else ""
            )
            body = json.dumps(
                {
                    "model": self.config["model"], "temperature": self.config.get("temperature", 0),
                    "reasoning_effort": self.config.get("reasoningEffort", "low"),
                    "max_tokens": int(self.config.get("maxOutputTokens", 900)),
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "weather_observer", "strict": True, "schema": self.schema},
                    },
                    "messages": [{"role": "user", "content": prompt + retry_note}],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = Request(
                str(self.config["baseUrl"]).rstrip("/") + "/chat/completions",
                data=body,
                headers={"Authorization": f"Bearer {self._api_key()}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=float(self.config.get("timeoutSeconds", 60))) as response:
                    envelope = json.loads(response.read().decode("utf-8"))
                content = (((envelope.get("choices") or [{}])[0].get("message") or {}).get("content"))
                if not content:
                    raise RuntimeError(f"observer API returned no content: {str(envelope.get('error'))[:300]}")
                usage = envelope.get("usage") or {}
                prompt_tokens += int(usage.get("prompt_tokens") or 0)
                completion_tokens += int(usage.get("completion_tokens") or 0)
                result = self._parse_json(str(content))
                self._validate_response(result, ranges)
                return result, {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                }
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    logging.warning("observer response invalid; retrying once: %s", exc)
                    time.sleep(0.25)
        raise RuntimeError(f"observer API failed after {attempts} attempt(s): {last_error}") from last_error

    def _open_position_count(self, event_id: str) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM weather_ai_agent_positions WHERE strategy_name=? AND event_id=? AND shares>0",
            (self.config.get("traderStrategyName"), event_id),
        ).fetchone()[0])

    def apply_observer_cooldown(
        self, city: str, source_slot: str, triggers: list[str], has_open_position: bool
    ) -> list[str]:
        remote_only = {"radar_arrival", "satellite_regime", "weather_process", "radiation_regime"}
        if not triggers or has_open_position or not set(triggers).issubset(remote_only):
            return triggers
        row = self.db.execute(
            """
            SELECT source_slot_utc FROM weather_observer_events
            WHERE city=? AND status='completed' AND prompt_tokens>0 AND source_slot_utc<?
            ORDER BY source_slot_utc DESC LIMIT 1
            """,
            (city, source_slot),
        ).fetchone()
        previous, current = parse_ts(row["source_slot_utc"]) if row else None, parse_ts(source_slot)
        cooldown = float(self.config.get("remoteOnlyObserverCooldownMinutes", 10))
        if previous and current and (current - previous).total_seconds() < cooldown * 60:
            return []
        return triggers

    @staticmethod
    def _book_vwap(book_json: Any, shares: float = 5) -> float | None:
        payload = json_value(book_json, {})
        asks = payload.get("asks", []) if isinstance(payload, dict) else []
        levels = sorted(
            ((as_float(row.get("price")), as_float(row.get("size"))) for row in asks if isinstance(row, dict)),
            key=lambda row: row[0] if row[0] is not None else 2,
        )
        remaining, total = shares, 0.0
        for price, size in levels:
            if price is None or size is None or not 0 < price < 1 or size <= 0:
                continue
            take = min(remaining, size)
            total += take * price
            remaining -= take
            if remaining <= 1e-9:
                return total / shares
        return None

    def market_gate_candidates(
        self, event_id: str, slot: str, semantic: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not self.config.get("marketInterruptEnabled", True):
            return []
        primary = [str(item) for item in semantic.get("primaryBuckets") or []]
        plausible = {str(item) for item in semantic.get("plausibleBuckets") or []}
        candidates: list[tuple[str, str]] = []
        if len(primary) == 1 and plausible.issubset(set(primary)):
            candidates.append((primary[0], "YES"))
        candidates.extend((str(item), "NO") for item in semantic.get("excludedBuckets") or [])
        if not candidates:
            return []
        state_hash = hashlib.sha256(json.dumps(
            {"primary": primary, "plausible": sorted(plausible),
             "excluded": sorted(str(item) for item in semantic.get("excludedBuckets") or []),
             "heating": semantic.get("heatingStatus")},
            sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest()[:20]
        market_slot = self.db.execute(
            "SELECT MAX(slot_utc) FROM market_snapshots WHERE event_id=? AND slot_utc<=?",
            (event_id, slot),
        ).fetchone()[0]
        if not market_slot:
            return []
        opened = []
        for outcome_range, side in candidates:
            row = self.db.execute(
                """
                SELECT m.market_id,ms.yes_book_json,ms.no_book_json
                FROM markets m JOIN market_snapshots ms ON ms.market_id=m.market_id
                WHERE m.event_id=? AND m.outcome_range=? AND ms.slot_utc=?
                """,
                (event_id, outcome_range, market_slot),
            ).fetchone()
            if not row:
                continue
            price = self._book_vwap(row[f"{side.lower()}_book_json"])
            cap = float(self.config["yesEntryPriceExclusive"] if side == "YES" else self.config["noEntryPriceExclusive"])
            if price is None or price >= cap:
                continue
            cursor = self.db.execute(
                """
                INSERT OR IGNORE INTO weather_observer_market_gates(
                    event_id,weather_state_hash,market_id,outcome_range,outcome_side,first_opened_at_utc,executable_price
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (event_id, state_hash, row["market_id"], outcome_range, side, iso_utc(), price),
            )
            if cursor.rowcount:
                opened.append({"outcomeRange": outcome_range, "outcomeSide": side})
        return opened

    def process_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        previous_row = self.db.execute(
            "SELECT * FROM weather_observer_city_states WHERE event_id=?", (candidate["event_id"],)
        ).fetchone()
        previous_raw = json_value(previous_row["raw_state_json"], {}) if previous_row else None
        previous_semantic = json_value(previous_row["semantic_state_json"], {}) if previous_row else {}
        current_raw = self.compact_state(candidate)
        triggers = material_trigger_types(previous_raw, current_raw, self.config)
        has_open_position = self._open_position_count(candidate["event_id"]) > 0
        triggers = self.apply_observer_cooldown(
            candidate["city"], candidate["slot_utc"], triggers, has_open_position
        )
        ranges = self.outcome_ranges(candidate["event_id"])
        started = iso_utc()
        market_slot = self.db.execute(
            "SELECT MAX(slot_utc) FROM market_snapshots WHERE event_id=? AND slot_utc<=?",
            (candidate["event_id"], candidate["slot_utc"]),
        ).fetchone()[0]
        fetched = self.db.execute(
            "SELECT MAX(fetched_at_utc) FROM weather_observations WHERE station_id=? AND slot_utc<=?",
            (candidate["station_id"], candidate["slot_utc"]),
        ).fetchone()[0]
        input_payload = {
            "eventId": candidate["event_id"], "city": candidate["city"],
            "targetDate": candidate["target_date"], "sourceSlotUtc": candidate["slot_utc"],
            "outcomeRanges": ranges, "triggerTypes": triggers,
            "previousSemanticState": previous_semantic, "newWeatherState": current_raw,
            "hasOpenPosition": has_open_position,
        }
        self.db.execute(
            """
            INSERT INTO weather_observer_events(
                event_id,city,station_id,target_date,source_run_id,source_slot_utc,trigger_types_json,
                source_observed_at_utc,source_published_at_utc,source_fetched_at_utc,market_snapshot_at_utc,
                observer_started_at_utc,prior_state_json,input_json,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'running')
            ON CONFLICT(event_id,source_slot_utc) DO UPDATE SET
                trigger_types_json=excluded.trigger_types_json,observer_started_at_utc=excluded.observer_started_at_utc,
                prior_state_json=excluded.prior_state_json,input_json=excluded.input_json,status='running',error=NULL
            """,
            (
                candidate["event_id"], candidate["city"], candidate["station_id"], candidate["target_date"],
                candidate["run_id"], candidate["slot_utc"], json.dumps(triggers),
                current_raw.get("primaryObservationTimeUtc"), None, fetched, market_slot, started,
                json.dumps(previous_semantic, ensure_ascii=False), json.dumps(input_payload, ensure_ascii=False),
            ),
        )
        self.db.commit()
        event_id = int(self.db.execute(
            "SELECT observer_event_id FROM weather_observer_events WHERE event_id=? AND source_slot_utc=?",
            (candidate["event_id"], candidate["slot_utc"]),
        ).fetchone()[0])
        try:
            if not triggers:
                response = {
                    **previous_semantic,
                    "materiality": "IGNORE", "changedProcess": "no_material_source_delta",
                    "affectedBuckets": [], "evidence": [], "uncertain": False,
                    "escalationReason": "No source change crossed the deterministic noise filter.",
                }
                response.setdefault("primaryBuckets", [])
                response.setdefault("plausibleBuckets", [])
                response.setdefault("tailBuckets", [])
                response.setdefault("excludedBuckets", [])
                response.setdefault("heatingStatus", current_raw.get("heatingPhase", "unclear"))
                response.setdefault("processSummary", "No material source delta.")
                response.setdefault("invalidation", "A genuinely new weather observation.")
                usage = {"prompt_tokens": 0, "completion_tokens": 0}
            else:
                response, usage = self.call_ai(input_payload, ranges)
            semantic = {key: response[key] for key in (
                "primaryBuckets", "plausibleBuckets", "tailBuckets", "excludedBuckets",
                "heatingStatus", "processSummary", "invalidation",
            )}
            materiality = response["materiality"]
            market_candidates = self.market_gate_candidates(candidate["event_id"], candidate["slot_utc"], semantic)
            if market_candidates:
                triggers = list(dict.fromkeys([*triggers, "market_entry"]))
                response["affectedBuckets"] = list(dict.fromkeys([
                    *(response.get("affectedBuckets") or []),
                    *(item["outcomeRange"] for item in market_candidates),
                ]))
                response["escalationReason"] = (str(response.get("escalationReason") or "") +
                    " A deterministic post-weather price gate entered its executable safety zone.").strip()
                if materiality not in {"POSITION_ALERT", "REANALYZE"}:
                    materiality = "REANALYZE"
                    response["materiality"] = materiality
                    response["changedProcess"] = "market_entry_for_fixed_weather_state"
            escalation = "pending" if materiality in {"REANALYZE", "POSITION_ALERT"} else "none"
            version = int(previous_row["state_version"] or 0) + 1 if previous_row else 1
            self.db.execute(
                """
                INSERT INTO weather_observer_city_states(
                    event_id,city,station_id,target_date,state_version,source_run_id,source_slot_utc,
                    raw_state_json,semantic_state_json,updated_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET
                    city=excluded.city,station_id=excluded.station_id,target_date=excluded.target_date,
                    state_version=excluded.state_version,source_run_id=excluded.source_run_id,
                    source_slot_utc=excluded.source_slot_utc,raw_state_json=excluded.raw_state_json,
                    semantic_state_json=excluded.semantic_state_json,updated_at_utc=excluded.updated_at_utc
                """,
                (
                    candidate["event_id"], candidate["city"], candidate["station_id"], candidate["target_date"],
                    version, candidate["run_id"], candidate["slot_utc"],
                    json.dumps(current_raw, ensure_ascii=False), json.dumps(semantic, ensure_ascii=False), iso_utc(),
                ),
            )
            self.db.execute(
                """
                UPDATE weather_observer_events SET observer_completed_at_utc=?,materiality=?,changed_process=?,
                    trigger_types_json=?,state_json=?,response_json=?,prompt_tokens=?,completion_tokens=?,status='completed',
                    escalation_status=?,error=NULL WHERE observer_event_id=?
                """,
                (
                    iso_utc(), materiality, response.get("changedProcess"), json.dumps(triggers),
                    json.dumps(semantic, ensure_ascii=False), json.dumps(response, ensure_ascii=False),
                    usage["prompt_tokens"], usage["completion_tokens"], escalation, event_id,
                ),
            )
            self.db.commit()
            return {"observer_event_id": event_id, "city": candidate["city"], "materiality": materiality, "escalation": escalation}
        except Exception as exc:
            self.db.rollback()
            self.db.execute(
                "UPDATE weather_observer_events SET observer_completed_at_utc=?,status='error',error=? WHERE observer_event_id=?",
                (iso_utc(), str(exc)[:2000], event_id),
            )
            self.db.commit()
            raise

    def run_once(self) -> dict[str, Any]:
        now = utc_now()
        if not self.config.get("enabled", True):
            return {"processed": 0, "message": "disabled"}
        candidates = self.latest_candidates(now)
        results = []
        def evaluate(candidate: dict[str, Any]) -> dict[str, Any]:
            worker = WeatherObserverAgent(self.config)
            try:
                return worker.process_candidate(candidate)
            finally:
                worker.close()

        workers = min(int(self.config.get("maxConcurrentEvaluations", 4)), len(candidates))
        if workers:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(evaluate, candidate): candidate for candidate in candidates}
                for future in as_completed(futures):
                    candidate = futures[future]
                    try:
                        results.append(future.result())
                    except Exception:
                        logging.exception("observer evaluation failed city=%s", candidate.get("city"))
        return {"processed": len(results), "candidates": len(candidates), "results": results}


def configure_logging(config: dict[str, Any]) -> None:
    path = ROOT / str(config["logPath"])
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")], force=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Low-latency weather-state observer")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--init-only", action="store_true")
    args = parser.parse_args()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    configure_logging(config)
    lock_path = ROOT / "data/weather_observer_agent.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("w")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.info("another weather observer process is running")
        return 0
    observer = WeatherObserverAgent(config)
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        if args.init_only:
            return 0
        if not args.loop:
            print(json.dumps(observer.run_once(), ensure_ascii=False, indent=2))
            return 0
        while not stop:
            result = observer.run_once()
            if result.get("processed"):
                logging.info("observer run: %s", json.dumps(result, ensure_ascii=False))
            for _ in range(int(config.get("pollSeconds", 15))):
                if stop:
                    break
                time.sleep(1)
        return 0
    finally:
        observer.close()
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
