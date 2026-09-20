#!/usr/bin/env python3
"""Point-in-time Grok backtest for event-driven two-bucket YES convergence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import time
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data/weather_market_monitor.sqlite3"
CONFIG_PATH = ROOT / "weather_ai_agent_config.json"
DEFAULT_OUTPUT_PATH = ROOT / "research/output/weather_pair_lock_grok_backtest.json"
UTC = timezone.utc
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
SHARES_PER_BUCKET = 5.0

if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from research.weather_exact_high_model_v2 import (  # noqa: E402
    DatasetBuilderV2,
    finite,
    make_model,
    physically_constrained_center,
    round_half_up,
    walk_forward,
)


RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "decision", "pair", "outsidePairRisk", "weatherLockReason",
        "marketAlreadyPriced", "evidence", "invalidation", "dataQuality",
    ],
    "properties": {
        "decision": {"type": "string", "enum": ["WAIT", "PAIR_CONVERGING", "PAIR_LOCKED"]},
        "pair": {
            "type": "array", "items": {"type": "string"},
            "minItems": 0, "maxItems": 2,
        },
        "outsidePairRisk": {"type": "string", "enum": ["low", "moderate", "high"]},
        "weatherLockReason": {"type": "string"},
        "marketAlreadyPriced": {"type": "boolean"},
        "evidence": {
            "type": "array", "items": {"type": "string"},
            "minItems": 1, "maxItems": 6,
        },
        "invalidation": {"type": "string"},
        "dataQuality": {"type": "string"},
    },
}


def parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def json_value(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return fallback


def exact_bucket(value: Any) -> int | None:
    text = str(value or "").replace("°C", "").replace(" C", "").strip()
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def vwap(book_json: Any, shares: float = SHARES_PER_BUCKET) -> float | None:
    levels = json_value(book_json, {}).get("asks", [])
    remaining, cost = float(shares), 0.0
    for level in sorted(levels, key=lambda item: float(item.get("price") or 2)):
        price, size = finite(level.get("price")), finite(level.get("size"))
        if price is None or size is None or not 0 < price < 1 or size <= 0:
            continue
        take = min(remaining, size)
        cost += take * price
        remaining -= take
        if remaining <= 1e-9:
            return cost / shares
    return None


class HistoricalRidge:
    def __init__(self, db_path: Path):
        self.builder = DatasetBuilderV2(db_path)
        self.model_cache: dict[tuple[str, int], tuple[Any, float, str | None, int, list[str]]] = {}
        self.snapshot_cache: dict[tuple[str, int, str], dict[str, Any]] = {}

    def close(self) -> None:
        self.builder.close()

    def snapshot(
        self, event: dict[str, Any], as_of: datetime, state_version: str = "",
    ) -> dict[str, Any]:
        local = as_of.astimezone(ZoneInfo(event["timezone"]))
        cutoff_minutes = min(19 * 60, max(10 * 60, (local.hour * 60 + local.minute) // 30 * 30))
        hour, minute = divmod(cutoff_minutes, 60)
        snapshot_key = (str(event["event_id"]), cutoff_minutes, state_version)
        if snapshot_key in self.snapshot_cache:
            return self.snapshot_cache[snapshot_key]
        key = (str(event["target_date"]), cutoff_minutes)
        if key not in self.model_cache:
            frame = self.builder.build(hour, minute)
            train = frame[
                frame["resolved"] & (frame["target_date"] < str(event["target_date"]))
            ].copy()
            if train.empty:
                return {"status": "unavailable", "reason": "no prior resolved training rows"}
            model, _features = make_model(train)
            prior_frame = frame[
                frame["resolved"] & (frame["target_date"] < str(event["target_date"]))
            ].copy()
            evaluation = walk_forward(prior_frame)
            q80 = float(evaluation.get("abs_error_q80_c") or 1.0)
            trained_through = str(max(train["target_date"])) if not train.empty else None
            self.model_cache[key] = (
                model, q80, trained_through, int(len(train)),
                list(evaluation.get("test_dates") or []),
            )
        model, q80, trained_through, train_rows, oos_dates = self.model_cache[key]
        frame = self.builder.build(hour, minute, available_as_of_utc=as_of)
        current = frame[
            (frame["event_id"] == str(event["event_id"]))
            & (frame["target_date"] == str(event["target_date"]))
        ]
        if current.empty:
            return {"status": "unavailable", "reason": "no point-in-time feature row"}
        row = current.sort_values("observation_age_minutes").iloc[0]
        age = finite(row.get("observation_age_minutes"))
        if age is None or age < 0 or age > 90:
            return {"status": "stale", "observationAgeMinutes": age}
        residual = float(model.predict(pd.DataFrame([row]))[0])
        central = physically_constrained_center(row, float(row["prior_center"]) + residual)
        observed_max = float(row["observed_max"])
        warm = central + q80
        trend = finite(row.get("trend_120"))
        remaining_solar = finite(row.get("remaining_usable_solar")) or 0.0
        cloud = finite(row.get("cloud_cover_pct"))
        spread = finite(row.get("model_spread")) or 0.0
        ecmwf = finite(row.get("ecmwf"))
        if (
            trend is not None and trend > 0 and remaining_solar > 0.25
            and (cloud is None or cloud <= 45) and spread >= 2.0 and ecmwf is not None
        ):
            warm = max(warm, ecmwf)
        capping_bucket = int(round_half_up(observed_max))
        primary_bucket = int(round_half_up(central))
        warm_bucket = int(round_half_up(warm))
        lower, upper = sorted((capping_bucket, warm_bucket))
        result = {
            "status": "ok", "sourceCutoffLocal": f"{hour:02d}:{minute:02d}",
            "trainedThrough": trained_through, "trainRows": train_rows,
            "oosDatesAvailableBeforeTarget": oos_dates,
            "primaryPathC": round(central, 2), "primaryBucketC": primary_bucket,
            "cappingPathC": round(observed_max, 2), "warmTailPathC": round(warm, 2),
            "plausibleBucketsC": list(range(lower, upper + 1)),
            "warmTailBufferC": round(q80, 3),
            "featureAsOfUtc": row.get("feature_as_of_utc"),
            "latestObservationTimeUtc": row.get("latest_observation_time_utc"),
            "latestObservationFetchedAtUtc": row.get("latest_observation_fetched_at_utc"),
            "observationAgeMinutes": round(age, 2),
            "inputs": {
                "meteoblueC": finite(row.get("mblue")), "ecmwfC": ecmwf,
                "equalWeightPriorC": round(float(row["prior_center"]), 2),
                "observedMaxC": round(observed_max, 2),
                "trend120CPerHour": trend,
                "remainingSolarFraction": round(float(row["remaining_solar_fraction"]), 3),
                "cloudCoverPct": cloud,
            },
        }
        self.snapshot_cache[snapshot_key] = result
        return result


class PairLockBacktest:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.output_path = Path(args.output_path).expanduser().resolve()
        self.db = sqlite3.connect(DB_PATH, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.ridge = HistoricalRidge(DB_PATH)
        self.output = self._load_output()
        self.cache = {
            str(item["contextHash"]): item
            for item in self.output.get("evaluations", [])
            if item.get("contextHash")
        }
        self.api_key = self._api_key()

    def close(self) -> None:
        self.ridge.close()
        self.db.close()

    def _api_key(self) -> str:
        path = Path(str(self.config["hermesApiKeyFile"])).expanduser()
        if path.stat().st_mode & 0o077:
            raise RuntimeError("Grok API key file permissions must be 600 or stricter")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise RuntimeError("Grok API key file is empty")
        return value

    def _load_output(self) -> dict[str, Any]:
        if self.output_path.exists() and not self.args.restart:
            parsed = json.loads(self.output_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                return parsed
        return {
            "researchOnly": True,
            "model": self.config.get("hermesModel", "grok-4.5"),
            "reasoningEffort": self.config.get("hermesReasoningEffort", "high"),
            "dates": self.args.dates,
            "evaluations": [],
        }

    def save(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output["updatedAtUtc"] = iso_utc(datetime.now(UTC))
        temporary = self.output_path.with_suffix(self.output_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.output, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.output_path)

    def events(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in self.args.dates)
        city_placeholders = ",".join("?" for _ in self.args.cities)
        rows = self.db.execute(
            f"""SELECT e.event_id,e.city,e.target_date,e.station_id,e.station_name,
                       e.resolution_source,e.rules,e.winning_range,
                       s.latitude,s.longitude,s.timezone
                FROM events e JOIN stations s USING(station_id)
                WHERE e.target_date IN ({placeholders})
                  AND e.city IN ({city_placeholders})
                ORDER BY e.target_date,e.city""",
            [*self.args.dates, *self.args.cities],
        ).fetchall()
        return [dict(row) for row in rows]

    def process_slots(self, event: dict[str, Any]) -> list[datetime]:
        rows = self.db.execute(
            """SELECT slot_utc,primary_observation_time_utc,status,state_json
               FROM weather_process_states
               WHERE station_id=? AND target_date=? AND status='ok'
               ORDER BY slot_utc""",
            (event["station_id"], event["target_date"]),
        ).fetchall()
        output: list[datetime] = []
        for row in rows:
            slot = parse_ts(row["slot_utc"])
            if slot is None:
                continue
            local = slot.astimezone(ZoneInfo(event["timezone"]))
            if local.date().isoformat() != event["target_date"] or not 10 <= local.hour < 19:
                continue
            state = json_value(row["state_json"], {})
            remote = state.get("remoteSensing") or {}
            radar = remote.get("rainviewer") or {}
            satellite = remote.get("nict_himawari_true_colour") or {}
            network = state.get("stationNetwork") or {}
            if (
                radar.get("status") != "ok" or satellite.get("status") != "ok"
                or int(network.get("reportsInWindow") or 0) < 1
            ):
                continue
            output.append(slot)
        return output

    def latest_process(self, event: dict[str, Any], as_of: datetime) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT slot_utc,primary_observation_time_utc,state_json
               FROM weather_process_states
               WHERE station_id=? AND target_date=? AND status='ok'
                 AND julianday(slot_utc)<=julianday(?)
               ORDER BY julianday(slot_utc) DESC LIMIT 1""",
            (event["station_id"], event["target_date"], iso_utc(as_of)),
        ).fetchone()
        if not row:
            return {"status": "missing"}
        state = json_value(row["state_json"], {})
        return {
            **state, "status": "ok", "snapshotSlotUtc": row["slot_utc"],
            "primaryObservationTimeUtc": row["primary_observation_time_utc"],
        }

    def model_state(self, event: dict[str, Any], as_of: datetime) -> dict[str, Any]:
        as_text = iso_utc(as_of)
        mb = self.db.execute(
            """SELECT slot_utc,model_ref_time_utc,model_updated_at_utc,forecast_max_c,
                      forecast_peak_local,points_json
               FROM windy_forecasts WHERE station_id=? AND target_date=? AND model='mblue'
                 AND status='ok' AND julianday(slot_utc)<=julianday(?)
                 AND julianday(fetched_at_utc)<=julianday(?)
               ORDER BY julianday(slot_utc) DESC LIMIT 1""",
            (event["station_id"], event["target_date"], as_text, as_text),
        ).fetchone()
        ec = self.db.execute(
            """SELECT slot_utc,forecast_max_c,forecast_peak_local,points_json
               FROM external_forecasts WHERE station_id=? AND target_date=? AND model='ecmwf_ifs025'
                 AND status='ok' AND julianday(slot_utc)<=julianday(?)
                 AND julianday(fetched_at_utc)<=julianday(?)
               ORDER BY julianday(slot_utc) DESC LIMIT 1""",
            (event["station_id"], event["target_date"], as_text, as_text),
        ).fetchone()

        def compact(row: sqlite3.Row | None, primary: bool) -> dict[str, Any] | None:
            if not row:
                return None
            points = json_value(row["points_json"], [])
            future = []
            for point in points:
                when = parse_ts(point.get("time_utc")) if isinstance(point, dict) else None
                if when is not None and when >= as_of - timedelta(minutes=15):
                    future.append({key: point.get(key) for key in (
                        "time_utc", "time_local", "temp_c", "dewpoint_c",
                        "relative_humidity_pct", "wind_speed_mps", "wind_direction_deg",
                        "cloud_low_pct", "cloud_mid_pct", "cloud_high_pct",
                        "shortwave_radiation_wm2", "precipitation_mm",
                    ) if key in point})
            return {
                "sampleSlotUtc": row["slot_utc"], "maxC": row["forecast_max_c"],
                "peakLocal": row["forecast_peak_local"],
                "modelVersion": (
                    row["model_updated_at_utc"] or row["model_ref_time_utc"]
                    if primary else row["slot_utc"]
                ),
                "futureProcess": future[:8],
            }
        return {"meteoblue": compact(mb, True), "ecmwf": compact(ec, False)}

    def market_state(self, event: dict[str, Any], as_of: datetime) -> dict[str, Any]:
        as_text = iso_utc(as_of)
        slot_row = self.db.execute(
            """SELECT MAX(slot_utc) slot FROM market_snapshots
               WHERE event_id=? AND julianday(slot_utc)<=julianday(?)
                 AND julianday(fetched_at_utc)<=julianday(?)""",
            (event["event_id"], as_text, as_text),
        ).fetchone()
        slot = slot_row["slot"] if slot_row else None
        if not slot:
            return {"snapshotUtc": None, "markets": [], "adjacentPairs": []}
        rows = [dict(row) for row in self.db.execute(
            """SELECT m.market_id,m.outcome_range,m.bucket_low,m.bucket_high,
                      ms.yes_best_bid,ms.yes_best_ask,ms.yes_book_json,
                      ms.market_volume_24h,ms.market_liquidity
               FROM market_snapshots ms JOIN markets m USING(market_id)
               WHERE ms.event_id=? AND ms.slot_utc=?
               ORDER BY COALESCE(m.bucket_low,-999)""",
            (event["event_id"], slot),
        )]
        markets = []
        exact: dict[int, dict[str, Any]] = {}
        for row in rows:
            executable = vwap(row["yes_book_json"])
            item = {
                "marketId": row["market_id"], "outcomeRange": row["outcome_range"],
                "bucketLow": row["bucket_low"], "bucketHigh": row["bucket_high"],
                "yesBid": row["yes_best_bid"], "yesAsk": row["yes_best_ask"],
                "yesVwap5": executable, "volume24h": row["market_volume_24h"],
                "liquidity": row["market_liquidity"],
            }
            markets.append(item)
            if (
                row["bucket_low"] is not None and row["bucket_high"] is not None
                and float(row["bucket_low"]) == float(row["bucket_high"])
            ):
                exact[int(round(float(row["bucket_low"])))] = item
        pairs = []
        for bucket in sorted(exact):
            if bucket + 1 not in exact:
                continue
            left, right = exact[bucket], exact[bucket + 1]
            if left["yesVwap5"] is None or right["yesVwap5"] is None:
                continue
            pairs.append({
                "ranges": [left["outcomeRange"], right["outcomeRange"]],
                "bucketsC": [bucket, bucket + 1],
                "combinedVwap5": round(float(left["yesVwap5"] + right["yesVwap5"]), 5),
            })
        return {"snapshotUtc": slot, "markets": markets, "adjacentPairs": pairs}

    @staticmethod
    def semantic_signature(context: dict[str, Any]) -> str:
        process = context["weatherProcess"]
        radar = ((process.get("remoteSensing") or {}).get("rainviewer") or {}).get("features") or {}
        satellite = ((process.get("remoteSensing") or {}).get("nict_himawari_true_colour") or {}).get("features") or {}
        trend = (process.get("primaryStationTrend") or {}).get("temperatureTrendCPerHour")
        ridge = context["ridgeV2"]
        signature = {
            "primaryObservation": process.get("primaryObservationTimeUtc"),
            "observedMax": (ridge.get("inputs") or {}).get("observedMaxC"),
            "ridgeBuckets": ridge.get("plausibleBucketsC"),
            "trendBand": None if trend is None else -1 if trend < -0.2 else 1 if trend > 0.2 else 0,
            "nearestEchoBand": None if radar.get("nearestEchoKm") is None else int(float(radar["nearestEchoKm"]) // 25),
            "radar25Band": None if radar.get("echoCoverage25Km") is None else round(float(radar["echoCoverage25Km"]), 1),
            "cloud25Band": None if satellite.get("cloudProxyCoverage25Km") is None else round(float(satellite["cloudProxyCoverage25Km"]), 1),
            "modelMax": {
                key: round(float(value["maxC"]) * 2) / 2 if value and value.get("maxC") is not None else None
                for key, value in context["models"].items()
            },
            "tradeablePairs": [
                [*pair["bucketsC"], round(float(pair["combinedVwap5"]), 2)]
                for pair in context["market"]["adjacentPairs"]
                if pair["combinedVwap5"] < 1
            ],
        }
        return hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()

    def context(
        self, event: dict[str, Any], as_of: datetime, previous: dict[str, Any] | None,
    ) -> dict[str, Any]:
        process = self.latest_process(event, as_of)
        models = self.model_state(event, as_of)
        model_signature = {
            key: {
                "maxC": value.get("maxC") if value else None,
                "peakLocal": value.get("peakLocal") if value else None,
                "futureProcess": value.get("futureProcess") if value else None,
            }
            for key, value in models.items()
        }
        ridge_state_version = hashlib.sha256(json.dumps({
            "primaryObservation": process.get("primaryObservationTimeUtc"),
            "models": model_signature,
        }, sort_keys=True).encode()).hexdigest()[:16]
        return {
            "event": {
                "eventId": event["event_id"], "city": event["city"],
                "targetDate": event["target_date"], "stationId": event["station_id"],
                "resolutionSource": event["resolution_source"], "rules": event["rules"],
            },
            "asOfUtc": iso_utc(as_of),
            "asOfLocal": as_of.astimezone(ZoneInfo(event["timezone"])).isoformat(timespec="minutes"),
            "ridgeV2": self.ridge.snapshot(event, as_of, ridge_state_version),
            "models": models,
            "weatherProcess": process,
            "market": self.market_state(event, as_of),
            "previousGrokAssessment": previous,
        }

    def candidate_contexts(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        last_signature = None
        last_selected: datetime | None = None
        for slot in self.process_slots(event):
            context = self.context(event, slot, None)
            ridge = context["ridgeV2"]
            if ridge.get("status") != "ok":
                continue
            plausible = set(int(item) for item in ridge.get("plausibleBucketsC") or [])
            if not plausible or len(plausible) > self.args.max_ridge_plausible_buckets:
                continue
            tradeable = [
                pair for pair in context["market"]["adjacentPairs"]
                if pair["combinedVwap5"] < 1
                and any(bucket in plausible for bucket in pair["bucketsC"])
            ]
            if not tradeable:
                continue
            signature = self.semantic_signature(context)
            if signature == last_signature:
                continue
            material = (
                last_selected is None
                or slot - last_selected >= timedelta(minutes=self.args.minimum_review_minutes)
                or (
                    candidates
                    and context["weatherProcess"].get("primaryObservationTimeUtc")
                    != candidates[-1]["weatherProcess"].get("primaryObservationTimeUtc")
                )
            )
            if not material:
                continue
            context["market"]["tradeableAdjacentPairs"] = tradeable
            candidates.append(context)
            last_signature, last_selected = signature, slot
        return candidates

    def call_grok(self, context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        prompt = (
            "You are performing a strict point-in-time backtest for exact daily maximum temperature markets. "
            "Use only the timestamped evidence in the input. You must not infer or assume the later settlement. "
            "Decide whether the FINAL daily maximum is now effectively confined to exactly two adjacent listed "
            "temperature buckets. PAIR_LOCKED means every normal primary and plausible alternative weather path "
            "ends inside those two buckets; escaping the pair requires an exceptional tail event. PAIR_CONVERGING "
            "means the pair leads but ordinary paths still escape it. WAIT means no defensible pair exists. "
            "Meteoblue, ECMWF, Ridge V2, radar, satellite proxies, nearby/upwind stations, observed heating, "
            "remaining sunlight and market consensus are evidence, not authorities. Explicitly account for short "
            "touches because one brief recorded maximum determines settlement. Do not lock merely because the market "
            "favors two buckets or because their combined price is below one. If PAIR_LOCKED, return exactly two "
            "adjacent outcomeRange labels that exist in input.market.markets. Otherwise pair must be empty. "
            "marketAlreadyPriced describes whether the pair's executable combined price leaves little economic room; "
            "it must not change the weather-lock judgment. Return only the required JSON object.\n\n"
            + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        )
        body = json.dumps({
            "model": self.config.get("hermesModel", "grok-4.5"),
            "temperature": 0,
            "reasoning_effort": (
                self.args.reasoning_effort
                or self.config.get("hermesReasoningEffort", "high")
            ),
            "max_tokens": 1400,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "weather_pair_lock", "strict": True, "schema": RESPONSE_SCHEMA},
            },
            "messages": [{"role": "user", "content": prompt}],
        }, ensure_ascii=False).encode("utf-8")
        request = Request(
            str(self.config["hermesBaseUrl"]).rstrip("/") + "/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urlopen(request, timeout=float(self.args.timeout_seconds)) as response:
                    envelope = json.loads(response.read().decode("utf-8"))
                content = (((envelope.get("choices") or [{}])[0].get("message") or {}).get("content"))
                if not content:
                    raise RuntimeError(f"Grok returned no content: {str(envelope.get('error'))[:300]}")
                parsed = json.loads(str(content).strip().removeprefix("```json").removesuffix("```").strip())
                self.validate_response(parsed, context)
                usage = envelope.get("usage") or {}
                return parsed, {
                    "promptTokens": int(usage.get("prompt_tokens") or 0),
                    "completionTokens": int(usage.get("completion_tokens") or 0),
                }
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1 + attempt)
        raise RuntimeError(f"Grok failed after three attempts: {last_error}") from last_error

    @staticmethod
    def validate_response(response: dict[str, Any], context: dict[str, Any]) -> None:
        required = set(RESPONSE_SCHEMA["required"])
        if set(response) != required:
            raise RuntimeError("Grok response fields do not match the strict schema")
        if response["decision"] not in {"WAIT", "PAIR_CONVERGING", "PAIR_LOCKED"}:
            raise RuntimeError("invalid Grok pair-lock decision")
        pair = response.get("pair")
        if not isinstance(pair, list):
            raise RuntimeError("Grok pair is not an array")
        if response["decision"] != "PAIR_LOCKED" and pair:
            raise RuntimeError("non-locked response must return an empty pair")
        if response["decision"] == "PAIR_LOCKED":
            allowed = {str(item["outcomeRange"]) for item in context["market"]["markets"]}
            buckets = [exact_bucket(item) for item in pair]
            if len(pair) != 2 or any(item not in allowed for item in pair):
                raise RuntimeError("locked pair is not two listed outcomes")
            if any(item is None for item in buckets) or abs(int(buckets[0]) - int(buckets[1])) != 1:
                raise RuntimeError("locked pair is not two adjacent exact buckets")

    def evaluate(self) -> None:
        for event in self.events():
            previous = None
            for context in self.candidate_contexts(event):
                context["previousGrokAssessment"] = previous
                context_hash = hashlib.sha256(
                    json.dumps(context, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()
                if context_hash in self.cache:
                    cached = self.cache[context_hash]
                    previous = cached.get("response")
                    continue
                response, usage = self.call_grok(context)
                record = {
                    "contextHash": context_hash, "eventId": event["event_id"],
                    "city": event["city"], "targetDate": event["target_date"],
                    "asOfUtc": context["asOfUtc"], "asOfLocal": context["asOfLocal"],
                    "input": context, "response": response, "usage": usage,
                }
                self.output["evaluations"].append(record)
                self.cache[context_hash] = record
                previous = response
                self.save()
                print(
                    f"{event['target_date']} {event['city']} {context['asOfLocal']} "
                    f"{response['decision']} {response['pair']}",
                    flush=True,
                )
                if response["decision"] == "PAIR_LOCKED":
                    break

    def actual(self, event_id: str) -> tuple[int | None, str]:
        event = self.db.execute(
            "SELECT winning_range FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        official = exact_bucket(event["winning_range"]) if event else None
        if official is not None:
            return official, "official"
        slot = self.db.execute(
            "SELECT MAX(slot_utc) slot FROM market_snapshots WHERE event_id=?", (event_id,)
        ).fetchone()["slot"]
        row = self.db.execute(
            """SELECT m.bucket_low,m.bucket_high,COALESCE(ms.yes_best_bid,ms.gamma_yes_price) price
               FROM market_snapshots ms JOIN markets m USING(market_id)
               WHERE ms.event_id=? AND ms.slot_utc=? ORDER BY price DESC LIMIT 1""",
            (event_id, slot),
        ).fetchone()
        if (
            row and float(row["price"] or 0) >= 0.99
            and row["bucket_low"] is not None and row["bucket_low"] == row["bucket_high"]
        ):
            return int(round(float(row["bucket_low"]))), "market_99"
        return None, "unknown"

    def execution_price(
        self, event_id: str, pair: list[str], decision_time: datetime,
    ) -> tuple[float | None, str | None]:
        execution_time = decision_time + timedelta(minutes=self.args.execution_delay_minutes)
        state = self.market_state({"event_id": event_id}, execution_time)
        by_range = {str(item["outcomeRange"]): item for item in state["markets"]}
        prices = [by_range.get(item, {}).get("yesVwap5") for item in pair]
        if len(prices) != 2 or any(item is None for item in prices):
            return None, state.get("snapshotUtc")
        return float(sum(prices)), state.get("snapshotUtc")

    def summarize(self) -> None:
        opportunities = []
        seen: set[tuple[str, str]] = set()
        for record in sorted(self.output["evaluations"], key=lambda item: item["asOfUtc"]):
            response = record.get("response") or {}
            key = (str(record["targetDate"]), str(record["city"]))
            if key in seen or response.get("decision") != "PAIR_LOCKED":
                continue
            pair = [str(item) for item in response.get("pair") or []]
            combined, market_slot = self.execution_price(
                str(record["eventId"]), pair, parse_ts(record["asOfUtc"])
            )
            actual, label = self.actual(str(record["eventId"]))
            buckets = [exact_bucket(item) for item in pair]
            opportunities.append({
                "eventId": record["eventId"], "targetDate": record["targetDate"],
                "city": record["city"], "decisionTimeUtc": record["asOfUtc"],
                "pair": pair, "pairBucketsC": buckets, "outsidePairRisk": response["outsidePairRisk"],
                "marketAlreadyPriced": response["marketAlreadyPriced"],
                "executionMarketSlotUtc": market_slot, "combinedVwap5": combined,
                "priceEligible": combined is not None and combined < 1,
                "actualBucketC": actual, "labelStatus": label,
                "pairCovered": actual is not None and actual in buckets,
            })
            seen.add(key)

        cash = float(self.args.initial_cash)
        reserve = float(self.args.cash_reserve)
        trades = []
        for target_date in sorted({item["targetDate"] for item in opportunities}):
            day_rows = [
                item for item in opportunities
                if item["targetDate"] == target_date and item["priceEligible"]
            ]
            day_rows.sort(key=lambda item: (
                item["decisionTimeUtc"], float(item["combinedVwap5"]), item["city"]
            ))
            positions = []
            for item in day_rows:
                cost = SHARES_PER_BUCKET * float(item["combinedVwap5"])
                executed = cash - cost >= reserve - 1e-9
                trade = {**item, "costUsdc": round(cost, 6), "executed": executed}
                if executed:
                    cash -= cost
                    positions.append(trade)
                trades.append(trade)
            for trade in positions:
                payout = SHARES_PER_BUCKET if trade["pairCovered"] else 0.0
                pnl = payout - trade["costUsdc"]
                cash += payout
                trade["payoutUsdc"] = payout
                trade["pnlUsdc"] = round(pnl, 6)
                trade["cashAfterSettlementUsdc"] = round(cash, 6)

        self.output["opportunities"] = opportunities
        self.output["trades"] = trades
        self.output["account"] = {
            "initialCashUsdc": self.args.initial_cash,
            "cashReserveUsdc": self.args.cash_reserve,
            "sharesPerBucket": SHARES_PER_BUCKET,
            "executionDelayMinutes": self.args.execution_delay_minutes,
            "endingCashUsdc": round(cash, 6),
            "pnlUsdc": round(cash - self.args.initial_cash, 6),
        }
        self.output["metrics"] = {}
        for label, accepted in (
            ("officialOnly", {"official"}),
            ("includingMarket99", {"official", "market_99"}),
        ):
            eligible = [item for item in opportunities if item["labelStatus"] in accepted]
            priced = [item for item in eligible if item["priceEligible"]]
            executed = [
                item for item in trades
                if item["labelStatus"] in accepted and item["executed"]
            ]
            self.output["metrics"][label] = {
                "lockedPairs": len(eligible),
                "lockedPairCoverage": (
                    sum(bool(item["pairCovered"]) for item in eligible) / len(eligible)
                    if eligible else None
                ),
                "priceEligiblePairs": len(priced),
                "priceEligibleCoverage": (
                    sum(bool(item["pairCovered"]) for item in priced) / len(priced)
                    if priced else None
                ),
                "executedTrades": len(executed),
                "executedPnlUsdc": round(sum(float(item.get("pnlUsdc") or 0) for item in executed), 6),
            }
        self.save()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", nargs="+", default=["2026-07-24", "2026-07-25", "2026-07-26"])
    parser.add_argument(
        "--cities", nargs="+",
        default=["Shanghai", "Beijing", "Guangzhou", "Qingdao", "Wuhan", "Chongqing", "Chengdu"],
    )
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--output-path", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--reasoning-effort", choices=["low", "medium", "high"])
    parser.add_argument("--merge-files", nargs="*")
    parser.add_argument("--minimum-review-minutes", type=int, default=20)
    parser.add_argument("--max-ridge-plausible-buckets", type=int, default=4)
    parser.add_argument("--execution-delay-minutes", type=int, default=5)
    parser.add_argument("--initial-cash", type=float, default=20.0)
    parser.add_argument("--cash-reserve", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    engine = PairLockBacktest(args)
    try:
        if args.merge_files:
            merged: dict[str, dict[str, Any]] = {}
            for filename in args.merge_files:
                payload = json.loads(Path(filename).expanduser().read_text(encoding="utf-8"))
                for item in payload.get("evaluations") or []:
                    if item.get("contextHash"):
                        merged[str(item["contextHash"])] = item
            engine.output["evaluations"] = list(merged.values())
            engine.cache = merged
            engine.summarize()
            print(json.dumps({
                "output": str(engine.output_path), "metrics": engine.output.get("metrics"),
                "account": engine.output.get("account"), "evaluations": len(merged),
            }, ensure_ascii=False, indent=2))
            return 0
        if args.dry_run:
            counts = {
                f"{event['target_date']}:{event['city']}": len(engine.candidate_contexts(event))
                for event in engine.events()
            }
            print(json.dumps({"candidateCounts": counts, "total": sum(counts.values())}, ensure_ascii=False, indent=2))
            return 0
        engine.evaluate()
        engine.summarize()
        print(json.dumps({
            "output": str(engine.output_path), "metrics": engine.output.get("metrics"),
            "account": engine.output.get("account"),
            "evaluations": len(engine.output.get("evaluations") or []),
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
