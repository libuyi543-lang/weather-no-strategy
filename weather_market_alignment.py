#!/usr/bin/env python3
"""Ridge V2 candidate generation and market/weather alignment for paper trading."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def round_half_up(value: float) -> int:
    return int(math.floor(float(value) + 0.5))


def ridge_v3_bucket_distribution(
    center_c: float, observed_max_c: float, residuals_c: list[Any],
    temperature: float = 1.0,
) -> dict[str, Any]:
    """Map signed OOS residuals to buckets, then apply fitted temperature scaling."""
    residuals = [finite(value) for value in residuals_c]
    residuals = [value for value in residuals if value is not None]
    if not residuals:
        return {
            "bucketProbabilities": [], "coldTailProbability": None,
            "warmTailProbability": None, "probabilitySum": 0.0,
            "rawBucketProbabilities": [],
            "probabilityTemperature": round(max(0.25, finite(temperature) or 1.0), 4),
            "probabilityCalibrationApplied": False,
        }
    counts: dict[int, int] = {}
    for residual in residuals:
        final_c = max(float(observed_max_c), float(center_c) + residual)
        bucket = round_half_up(final_c)
        counts[bucket] = counts.get(bucket, 0) + 1
    total = float(sum(counts.values()))
    raw_probabilities = {
        bucket: count / total for bucket, count in sorted(counts.items())
    }
    from research.weather_exact_high_model_v2 import temperature_scale_probabilities

    calibrated = temperature_scale_probabilities(raw_probabilities, temperature)
    probabilities = [
        {"bucketC": bucket, "probability": probability}
        for bucket, probability in sorted(calibrated.items())
    ]
    center_bucket = round_half_up(center_c)
    cold = sum(item["probability"] for item in probabilities if item["bucketC"] < center_bucket)
    warm = sum(item["probability"] for item in probabilities if item["bucketC"] > center_bucket)
    return {
        "bucketProbabilities": [
            {"bucketC": item["bucketC"], "probability": round(item["probability"], 6)}
            for item in probabilities
        ],
        "rawBucketProbabilities": [
            {"bucketC": bucket, "probability": round(probability, 6)}
            for bucket, probability in sorted(raw_probabilities.items())
        ],
        "probabilityTemperature": round(max(0.25, finite(temperature) or 1.0), 4),
        "probabilityCalibrationApplied": abs((finite(temperature) or 1.0) - 1.0) > 1e-9,
        "coldTailProbability": round(cold, 6),
        "warmTailProbability": round(warm, 6),
        "probabilitySum": round(sum(item["probability"] for item in probabilities), 6),
    }


def stabilize_ridge_center(
    raw_center_c: float, observed_max_c: float, previous: dict[str, Any] | None,
    current_inputs: dict[str, Any], artifact_name: str,
    process_signature: list[str], max_unexplained_move_c: float,
) -> tuple[float, dict[str, Any]]:
    """Limit unexplained observation-to-observation jumps while preserving hard floors."""
    raw_center = max(float(raw_center_c), float(observed_max_c))
    previous_center = finite((previous or {}).get("primaryPathC"))
    if previous_center is None:
        return raw_center, {
            "constraintApplied": False, "rawCenterC": round(raw_center, 3),
            "previousCenterC": None, "allowedReasons": ["first_snapshot"],
        }
    previous_inputs = (previous or {}).get("inputs") or {}
    reasons = []
    previous_observed = finite(previous_inputs.get("observedMaxC"))
    if previous_observed is not None and observed_max_c > previous_observed + 0.01:
        reasons.append("new_observed_max")
    if str((previous or {}).get("artifact") or "") != artifact_name:
        reasons.append("model_retrained")
    for field in ("meteoblueFetchedAtUtc", "ecmwfFetchedAtUtc"):
        before, after = previous_inputs.get(field), current_inputs.get(field)
        if before and after and str(before) != str(after):
            reasons.append("forecast_model_update")
            break
    previous_process = sorted(str(value) for value in (previous or {}).get("processSignature") or [])
    current_process = sorted(str(value) for value in process_signature)
    if previous_process and current_process != previous_process:
        reasons.append("weather_process_regime_change")

    delta = raw_center - previous_center
    limit = max(0.0, float(max_unexplained_move_c))
    constrained = raw_center
    applied = False
    if abs(delta) > limit and not reasons:
        constrained = previous_center + math.copysign(limit, delta)
        constrained = max(float(observed_max_c), constrained)
        applied = abs(constrained - raw_center) > 1e-9
    return constrained, {
        "constraintApplied": applied,
        "rawCenterC": round(raw_center, 3),
        "previousCenterC": round(previous_center, 3),
        "maxUnexplainedMoveC": limit,
        "allowedReasons": sorted(set(reasons)),
    }


class RidgeV2Adapter:
    """Load the research artifact and produce a conservative path snapshot.

    The adapter deliberately reuses the research feature builder instead of
    copying its feature definitions into the trading loop. Ridge remains a
    candidate generator; the returned object never authorizes an order.
    """

    def __init__(self, root: Path, db_path: Path, config: dict[str, Any]):
        self.root = root
        self.db_path = db_path
        self.config = config
        self._cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._frame_cache: dict[tuple[int, int, str], Any] = {}
        self._frame_cache_enabled = False
        self._retrain_attempted_dates: set[str] = set()
        self._visibility_retrain_attempted_dates: set[str] = set()

    def begin_review_batch(self) -> None:
        self._frame_cache.clear()
        self._frame_cache_enabled = True

    def end_review_batch(self) -> None:
        self._frame_cache.clear()
        self._frame_cache_enabled = False

    def _feature_frame(
        self, cutoff_hour: int, cutoff_minute: int, as_of_utc: datetime,
    ) -> Any:
        """Build the expensive point-in-time frame once per review batch."""
        normalized = (
            as_of_utc.replace(tzinfo=timezone.utc)
            if as_of_utc.tzinfo is None else as_of_utc.astimezone(timezone.utc)
        )
        key = (cutoff_hour, cutoff_minute, normalized.isoformat(timespec="seconds"))
        if self._frame_cache_enabled:
            cached = self._frame_cache.get(key)
            if cached is not None:
                return cached
        from research.weather_exact_high_model_v2 import DatasetBuilderV2

        builder = DatasetBuilderV2(self.db_path)
        try:
            frame = builder.build(
                cutoff_hour, cutoff_minute, available_as_of_utc=normalized
            )
        finally:
            builder.close()
        if self._frame_cache_enabled:
            self._frame_cache[key] = frame
        return frame

    def _ensure_visibility_artifact(
        self, local_date: str, as_of_utc: datetime,
    ) -> dict[str, Any]:
        path = self.root / "data/models/weather_visibility_challenger_v1.joblib"
        status: dict[str, Any] = {"attempted": False, "artifact": path.name}
        try:
            import joblib
            existing = joblib.load(path) if path.exists() else {}
        except Exception as exc:
            existing = {}
            status["existingArtifactError"] = str(exc)
        latest_resolved = self._latest_resolved_training_date(local_date)
        due = bool(
            latest_resolved and (
                not path.exists()
                or str(existing.get("trained_through") or "") < latest_resolved
            )
        )
        status.update({
            "latestResolvedTrainingDate": latest_resolved,
            "trainedThroughBefore": existing.get("trained_through"),
        })
        if not self.config.get("visibilityChallengerDailyRetrain", True) or not due:
            status["status"] = "current" if path.exists() else "not_configured"
            return status
        if local_date in self._visibility_retrain_attempted_dates:
            status["status"] = "already_attempted_today"
            return status
        self._visibility_retrain_attempted_dates.add(local_date)
        status["attempted"] = True
        try:
            from research.weather_visibility_challenger import train_visibility_artifact
            report, _ = train_visibility_artifact(
                self.db_path, local_date, as_of_utc, path
            )
            status.update({
                "status": "retrained",
                "trainedThrough": report.get("trained_through"),
                "trainRows": report.get("train_rows"),
            })
        except Exception as exc:
            status.update({
                "status": "failed_previous_artifact_retained",
                "reason": str(exc),
            })
        return status

    def _visibility_challenger(
        self, row: Any, observed_max: float, cutoff_label: str,
        local_date: str, as_of_utc: datetime,
    ) -> dict[str, Any]:
        if not self.config.get("visibilityChallengerEnabled", False):
            return {"status": "disabled", "shadowOnly": True}
        if cutoff_label not in {"1030", "1100"}:
            return {
                "status": "not_due", "shadowOnly": True,
                "reason": "visibility challenger only runs at 10:30 and 11:00 local",
            }
        retraining = self._ensure_visibility_artifact(local_date, as_of_utc)
        path = self.root / "data/models/weather_visibility_challenger_v1.joblib"
        if not path.exists():
            return {
                "status": "unavailable", "shadowOnly": True,
                "reason": f"missing artifact {path.name}", "dailyRetraining": retraining,
            }
        visibility = finite(row.get("visibility_m"))
        try:
            import joblib
            import numpy as np
            import pandas as pd
            from research.weather_exact_high_model_v2 import physically_constrained_center

            artifact = joblib.load(path)
            challenger_row = row.copy()
            challenger_row["visibility_log_m"] = (
                float(np.log1p(max(0.0, visibility))) if visibility is not None else np.nan
            )
            residual = float(artifact["model"].predict(pd.DataFrame([challenger_row]))[0])
            center = physically_constrained_center(
                challenger_row, float(challenger_row["prior_center"]) + residual
            )
            evaluation = artifact.get("challenger_evaluation") or {}
            baseline = artifact.get("baseline_evaluation") or {}
            return {
                "status": "ok", "shadowOnly": True, "authoritative": False,
                "modelVersion": artifact.get("model_version"),
                "artifact": path.name,
                "trainedThrough": artifact.get("trained_through"),
                "sourceCutoffLocal": f"{cutoff_label[:2]}:{cutoff_label[2:]}",
                "featureAsOfUtc": row.get("feature_as_of_utc"),
                "visibilityM": visibility,
                "visibilityAvailable": visibility is not None,
                "primaryPathC": round(center, 2),
                "primaryBucketC": round_half_up(center),
                "oosMetrics": evaluation.get("metrics") or {},
                "baselineOosMetrics": baseline.get("metrics") or {},
                "dailyRetraining": retraining,
            }
        except Exception as exc:
            return {
                "status": "unavailable", "shadowOnly": True,
                "reason": f"visibility challenger failed: {exc}",
                "dailyRetraining": retraining,
            }

    def _latest_resolved_training_date(self, before_date: str) -> str | None:
        try:
            db = sqlite3.connect(self.db_path, timeout=5)
            row = db.execute(
                """SELECT MAX(target_date) FROM events
                   WHERE resolved_at_utc IS NOT NULL AND target_date<?""",
                (before_date,),
            ).fetchone()
            db.close()
            return str(row[0]) if row and row[0] else None
        except sqlite3.Error:
            return None

    def _ensure_daily_shared_artifact(
        self, local_date: str, as_of_utc: datetime,
    ) -> dict[str, Any]:
        path = self.root / "data" / "models" / "weather_exact_high_v21_shared.joblib"
        status: dict[str, Any] = {"attempted": False, "artifact": path.name}
        try:
            import joblib
            existing = joblib.load(path) if path.exists() else {}
        except Exception as exc:
            existing = {}
            status["existingArtifactError"] = str(exc)
        latest_resolved = self._latest_resolved_training_date(local_date)
        status.update({
            "latestResolvedTrainingDate": latest_resolved,
            "trainedThroughBefore": existing.get("trained_through"),
        })
        due = bool(
            latest_resolved
            and str(existing.get("trained_through") or "") < latest_resolved
        )
        if not path.exists() and latest_resolved:
            due = True
        if not self.config.get("ridgeV21DailyRetrain", False) or not due:
            status["status"] = "current" if path.exists() else "not_configured"
            return status
        if local_date in self._retrain_attempted_dates:
            status["status"] = "already_attempted_today"
            return status
        self._retrain_attempted_dates.add(local_date)
        status["attempted"] = True
        try:
            from research.weather_exact_high_model_v2 import train_shared_artifact
            report, _ = train_shared_artifact(
                self.db_path, local_date, as_of_utc, artifact_path=path
            )
            status.update({
                "status": "retrained", "trainedThrough": report.get("trained_through"),
                "trainEvents": report.get("train_events"),
            })
        except Exception as exc:
            status.update({"status": "failed_previous_artifact_retained", "reason": str(exc)})
        return status

    def _previous_ok_snapshot(
        self, event_id: str, observation_time_utc: str | None,
    ) -> dict[str, Any] | None:
        try:
            db = sqlite3.connect(self.db_path, timeout=5)
            if observation_time_utc:
                row = db.execute(
                    """SELECT payload_json FROM weather_ridge_v2_snapshots
                       WHERE event_id=? AND status='ok'
                         AND julianday(latest_observation_time_utc)<julianday(?)
                       ORDER BY julianday(latest_observation_time_utc) DESC,
                                julianday(feature_as_of_utc) DESC LIMIT 1""",
                    (event_id, observation_time_utc),
                ).fetchone()
            else:
                row = None
            db.close()
            return json.loads(row[0]) if row and row[0] else None
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _persist_snapshot(
        self, event: dict[str, Any], cutoff_local: str, as_of_utc: datetime,
        state_version: str, result: dict[str, Any],
    ) -> str | None:
        if not self.config.get("ridgeV2PersistSnapshots", True):
            return None
        normalized = as_of_utc.replace(tzinfo=timezone.utc) if as_of_utc.tzinfo is None else as_of_utc.astimezone(timezone.utc)
        feature_as_of = str(result.get("featureAsOfUtc") or normalized.isoformat(timespec="seconds"))
        try:
            db = sqlite3.connect(self.db_path, timeout=5)
            db.execute("PRAGMA busy_timeout=5000")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS weather_ridge_v2_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    city TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    cutoff_local TEXT NOT NULL,
                    feature_as_of_utc TEXT NOT NULL,
                    generated_at_utc TEXT NOT NULL,
                    state_version TEXT,
                    status TEXT NOT NULL,
                    artifact TEXT,
                    trained_through TEXT,
                    latest_observation_time_utc TEXT,
                    latest_observation_fetched_at_utc TEXT,
                    observation_age_minutes REAL,
                    primary_path_c REAL,
                    primary_bucket_c INTEGER,
                    capping_path_c REAL,
                    warm_tail_path_c REAL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(event_id,cutoff_local,feature_as_of_utc)
                );
                CREATE INDEX IF NOT EXISTS idx_weather_ridge_v2_event_time
                ON weather_ridge_v2_snapshots(event_id,feature_as_of_utc);
                """
            )
            db.execute(
                """
                INSERT INTO weather_ridge_v2_snapshots(
                    event_id,city,target_date,cutoff_local,feature_as_of_utc,
                    generated_at_utc,state_version,status,artifact,trained_through,
                    latest_observation_time_utc,latest_observation_fetched_at_utc,
                    observation_age_minutes,primary_path_c,primary_bucket_c,
                    capping_path_c,warm_tail_path_c,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id,cutoff_local,feature_as_of_utc) DO UPDATE SET
                    generated_at_utc=excluded.generated_at_utc,
                    state_version=excluded.state_version,status=excluded.status,
                    artifact=excluded.artifact,trained_through=excluded.trained_through,
                    latest_observation_time_utc=excluded.latest_observation_time_utc,
                    latest_observation_fetched_at_utc=excluded.latest_observation_fetched_at_utc,
                    observation_age_minutes=excluded.observation_age_minutes,
                    primary_path_c=excluded.primary_path_c,
                    primary_bucket_c=excluded.primary_bucket_c,
                    capping_path_c=excluded.capping_path_c,
                    warm_tail_path_c=excluded.warm_tail_path_c,
                    payload_json=excluded.payload_json
                """,
                (
                    str(event.get("event_id")), str(event.get("city")),
                    str(event.get("target_date")), cutoff_local, feature_as_of,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"), state_version,
                    str(result.get("status") or "unknown"), result.get("artifact"),
                    result.get("trainedThrough"), result.get("latestObservationTimeUtc"),
                    result.get("latestObservationFetchedAtUtc"),
                    finite(result.get("observationAgeMinutes")),
                    finite(result.get("primaryPathC")), result.get("primaryBucketC"),
                    finite(result.get("cappingPathC")), finite(result.get("warmTailPathC")),
                    json.dumps(result, ensure_ascii=False, default=str),
                ),
            )
            db.commit()
            db.close()
            return None
        except sqlite3.Error as exc:
            return str(exc)

    def snapshot(
        self, event: dict[str, Any], as_of_utc: datetime, state_version: str = "",
        stability_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.config.get("ridgeV2Enabled", True):
            return {"status": "disabled", "role": "candidate_generator_only"}
        try:
            local_time = as_of_utc.astimezone(ZoneInfo(str(event["timezone"])))
        except (KeyError, TypeError, ValueError):
            return {"status": "unavailable", "reason": "invalid event timezone"}

        interval = max(1, int(self.config.get("ridgeV2IntervalMinutes", 30)))
        start_minutes = int(self.config.get("ridgeV2StartLocalMinutes", 10 * 60))
        end_minutes = int(self.config.get("ridgeV2EndLocalMinutes", 19 * 60))
        cutoffs = list(range(start_minutes, end_minutes + 1, interval))
        local_minutes = local_time.hour * 60 + local_time.minute
        eligible = [value for value in cutoffs if value <= local_minutes]
        if not eligible:
            return {
                "status": "not_due",
                "role": "candidate_generator_only",
                "reason": f"first Ridge V2 cutoff is {cutoffs[0] // 60:02d}:{cutoffs[0] % 60:02d} local",
            }
        cutoff_minutes = eligible[-1]
        cutoff_hour, cutoff_minute = divmod(cutoff_minutes, 60)
        cutoff_label = f"{cutoff_hour:02d}{cutoff_minute:02d}"
        retraining = self._ensure_daily_shared_artifact(local_time.date().isoformat(), as_of_utc)
        shared_path = self.root / "data" / "models" / "weather_exact_high_v21_shared.joblib"
        legacy_path = self.root / "data" / "models" / f"weather_exact_high_v2_{cutoff_label}.joblib"
        artifact_path = shared_path if shared_path.exists() else legacy_path
        if not artifact_path.exists():
            return {
                "status": "unavailable", "role": "candidate_generator_only",
                "reason": f"missing artifact {artifact_path.name}",
            }
        try:
            import joblib
            import pandas as pd

            from research.weather_exact_high_model_v2 import physically_constrained_center

            frame = self._feature_frame(cutoff_hour, cutoff_minute, as_of_utc)
            current = frame[
                (frame["event_id"] == str(event.get("event_id")))
                & (frame["target_date"] == str(event.get("target_date")))
            ]
            if current.empty:
                raise RuntimeError("no feature row for this event and cutoff")
            row = current.sort_values("observation_age_minutes").iloc[0]
            process_signature = sorted(str(value) for value in (
                (stability_context or {}).get("detectedProcesses") or []
            ))
            cache_key = (
                str(event.get("event_id")), str(event.get("target_date")), cutoff_minutes,
                artifact_path.name, artifact_path.stat().st_mtime_ns,
                str(row.get("latest_observation_time_utc") or ""),
                str(row.get("meteoblue_fetched_at_utc") or ""),
                str(row.get("ecmwf_fetched_at_utc") or ""), tuple(process_signature),
            )
            observation_age = finite(row.get("observation_age_minutes"))
            max_observation_age = float(
                self.config.get("ridgeV2MaxObservationAgeMinutes", 90)
            )
            if (
                observation_age is None or observation_age < 0
                or observation_age > max_observation_age
            ):
                result = {
                    "status": "stale", "role": "candidate_generator_only",
                    "reason": "latest point-in-time METAR is too old for Ridge V2",
                    "sourceCutoffLocal": f"{cutoff_hour:02d}:{cutoff_minute:02d}",
                    "featureAsOfUtc": row.get("feature_as_of_utc"),
                    "latestObservationTimeUtc": row.get("latest_observation_time_utc"),
                    "latestObservationFetchedAtUtc": row.get("latest_observation_fetched_at_utc"),
                    "observationAgeMinutes": observation_age,
                    "maxObservationAgeMinutes": max_observation_age,
                }
                persistence_error = self._persist_snapshot(
                    event, f"{cutoff_hour:02d}:{cutoff_minute:02d}", as_of_utc,
                    state_version, result,
                )
                if persistence_error:
                    result["snapshotPersistenceError"] = persistence_error
                self._cache[cache_key] = result
                return result
            if cache_key in self._cache:
                return self._cache[cache_key]
            try:
                artifact = joblib.load(artifact_path)
            except Exception:
                if artifact_path == shared_path and legacy_path.exists():
                    artifact_path = legacy_path
                    artifact = joblib.load(artifact_path)
                    retraining = {
                        **retraining, "runtimeFallback": legacy_path.name,
                        "status": "shared_load_failed_using_legacy",
                    }
                else:
                    raise
            model = artifact["model"]
            residual = float(model.predict(pd.DataFrame([row]))[0])
            observed_max = float(row["observed_max"])
            raw_ridge_center = float(row["prior_center"]) + residual
            central = physically_constrained_center(
                row, raw_ridge_center
            )
            current_inputs = {
                "observedMaxC": round(observed_max, 2),
                "meteoblueFetchedAtUtc": row.get("meteoblue_fetched_at_utc"),
                "ecmwfFetchedAtUtc": row.get("ecmwf_fetched_at_utc"),
            }
            previous = self._previous_ok_snapshot(
                str(event.get("event_id")), row.get("latest_observation_time_utc")
            )
            central, stability = stabilize_ridge_center(
                central, observed_max, previous, current_inputs, artifact_path.name,
                process_signature,
                float(self.config.get("ridgeV21MaxUnexplainedMoveC", 0.75)),
            )
            warm_buffer = finite(artifact.get("warm_tail_buffer_c"))
            if warm_buffer is None:
                warm_buffer = float(self.config.get("ridgeV2WarmTailBufferC", 1.0))
            warm = central + warm_buffer

            trend = finite(row.get("trend_120"))
            remaining_solar = finite(row.get("remaining_usable_solar")) or 0.0
            cloud = finite(row.get("cloud_cover_pct"))
            current_below_max = finite(row.get("current_below_max")) or 0.0
            model_spread = finite(row.get("model_spread")) or 0.0
            heating_supported = (
                trend is not None and trend > 0
                and remaining_solar > 0.25
                and (cloud is None or cloud <= 45)
                and current_below_max <= 0.01
            )
            warm_model_support = heating_supported and model_spread >= 2.0
            ecmwf = finite(row.get("ecmwf"))
            if warm_model_support and ecmwf is not None:
                warm = max(warm, ecmwf)
            cap_supported = (
                current_below_max > 0.01
                or (trend is not None and trend <= 0)
                or remaining_solar <= 0.25
                or (cloud is not None and cloud >= 75)
            )
            cap_bucket = round_half_up(observed_max)
            primary_bucket = round_half_up(central)
            warm_bucket = round_half_up(warm)
            lower, upper = sorted((cap_bucket, warm_bucket))
            oos_dates = list(artifact.get("oos_dates") or [])
            calibration_status = str(
                artifact.get("calibration_status")
                or "insufficient_independent_oos_dates"
            )
            residuals_by_cutoff = artifact.get("residuals_by_cutoff") or {}
            distribution_residuals = list(
                residuals_by_cutoff.get(cutoff_label)
                or artifact.get("residuals_c") or []
            )
            probability_calibration = (
                artifact.get("bucket_probability_calibration") or {}
            )
            probability_temperature = 1.0
            if probability_calibration.get("enabled"):
                probability_temperature = finite(
                    (probability_calibration.get("temperaturesByCutoff") or {}).get(
                        cutoff_label
                    )
                ) or 1.0
            distribution = ridge_v3_bucket_distribution(
                central, observed_max, distribution_residuals,
                temperature=probability_temperature,
            )
            result = {
                "status": "ok",
                "role": "candidate_generator_only",
                "authoritative": False,
                "modelVersion": artifact.get("model_version") or "ridge_v2_cutoff",
                "distributionVersion": artifact.get("distribution_version"),
                "validationStatus": calibration_status,
                "calibrationStatus": calibration_status,
                "calibrationDates": len(oos_dates),
                "minimumAuthoritativeOosDates": int(
                    artifact.get("minimum_authoritative_oos_dates") or 30
                ),
                "coverageTargets": artifact.get("coverage_targets") or {},
                "bucketCalibration": artifact.get("bucket_calibration") or {},
                "bucketProbabilityCalibration": probability_calibration,
                "calibrationQuality": artifact.get("calibration_quality") or {},
                "distributionSampleEvents": len(distribution_residuals),
                "distributionCutoffLocal": f"{cutoff_hour:02d}:{cutoff_minute:02d}",
                "warning": (
                    "Ridge V2.1/V3.1 is research-only. Use its paths and bucket probabilities to "
                    "generate questions and candidates, never as an order authorization."
                ),
                "sourceCutoffLocal": f"{cutoff_hour:02d}:{cutoff_minute:02d}",
                "artifact": artifact_path.name,
                "trainedThrough": artifact.get("trained_through"),
                "oosEvents": artifact.get("oos_events"),
                "oosDates": oos_dates,
                "pointInTimeData": bool(artifact.get("point_in_time_data")),
                "featureAsOfUtc": row.get("feature_as_of_utc"),
                "latestObservationTimeUtc": row.get("latest_observation_time_utc"),
                "latestObservationFetchedAtUtc": row.get("latest_observation_fetched_at_utc"),
                "observationAgeMinutes": round(observation_age, 2),
                "warmTailBufferC": round(warm_buffer, 3),
                "rawRidgePathC": round(max(observed_max, raw_ridge_center), 2),
                "primaryPathC": round(central, 2),
                "primaryBucketC": primary_bucket,
                "cappingPathC": round(observed_max, 2),
                "cappingBucketC": cap_bucket,
                "warmTailPathC": round(warm, 2),
                "warmTailBucketC": warm_bucket,
                "plausibleBucketsC": list(range(lower, upper + 1)),
                **distribution,
                "settlementMapping": "round_half_up_integer_c_with_observed_max_floor",
                "probabilityRole": "research_evidence_only",
                "stability": stability,
                "processSignature": process_signature,
                "dailyRetraining": retraining,
                "pathStatus": {
                    "capping": "supported_competitor" if cap_supported else "plausible",
                    "primary": "reference_path",
                    "warmTail": "supported_competitor" if warm_model_support else "plausible",
                },
                "inputs": {
                    "meteoblueC": finite(row.get("mblue")),
                    "ecmwfC": ecmwf,
                    "ensemblePriorC": round(float(row["prior_center"]), 2),
                    "observedMaxC": round(observed_max, 2),
                    "meteoblueFetchedAtUtc": row.get("meteoblue_fetched_at_utc"),
                    "ecmwfFetchedAtUtc": row.get("ecmwf_fetched_at_utc"),
                    "trend120CPerHour": trend,
                    "remainingSolarFraction": round(float(row["remaining_solar_fraction"]), 3),
                    "cloudCoverPct": cloud,
                    "sameHourModelErrorC": finite(row.get("prior_same_hour_error")),
                    "precipitationMm": finite(row.get("precipitation_mm")),
                    "windDirectionDeg": finite(row.get("wind_direction_deg")),
                    "windGustKt": finite(row.get("wind_gust_kt")),
                    "visibilityM": finite(row.get("visibility_m")),
                    "measuredSolarRadiationWm2": finite(row.get("solar_radiation_wm2")),
                },
            }
            result["visibilityChallenger"] = self._visibility_challenger(
                row, observed_max, cutoff_label, local_time.date().isoformat(), as_of_utc
            )
        except Exception as exc:
            result = {
                "status": "unavailable", "role": "candidate_generator_only",
                "reason": f"Ridge V2 adapter failed: {exc}",
            }
        if result.get("status") in {"ok", "stale"}:
            persistence_error = self._persist_snapshot(
                event, f"{cutoff_hour:02d}:{cutoff_minute:02d}", as_of_utc,
                state_version, result,
            )
            if persistence_error:
                result["snapshotPersistenceError"] = persistence_error
        cache_key = locals().get("cache_key") or (
            str(event.get("event_id")), str(event.get("target_date")), cutoff_minutes,
            str(state_version), artifact_path.name,
        )
        self._cache[cache_key] = result
        if len(self._cache) > 64:
            self._cache.pop(next(iter(self._cache)))
        return result


def _market_bucket(market: dict[str, Any]) -> int | None:
    low, high = finite(market.get("bucketLow")), finite(market.get("bucketHigh"))
    if low is None and high is None:
        return None
    if low is None:
        return round_half_up(high)
    if high is None:
        return round_half_up(low)
    return round_half_up((low + high) / 2)


def market_alignment(
    markets: list[dict[str, Any]], ridge: dict[str, Any], weather_process: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Classify market/weather agreement and emit the only allowed new-entry candidates."""
    priced: list[tuple[dict[str, Any], float]] = []
    for market in markets:
        bid, ask = finite(market.get("yesBestBid")), finite(market.get("yesBestAsk"))
        midpoint = (bid + ask) / 2 if bid is not None and ask is not None else ask if ask is not None else bid
        if midpoint is not None:
            priced.append((market, midpoint))
    priced.sort(key=lambda item: item[1], reverse=True)
    if not priced:
        return {
            "mode": "NO_EDGE", "weatherAlignment": "unknown", "marketLeader": None,
            "reason": "No usable YES prices are available.", "candidates": [],
        }

    leader, leader_price = priced[0]
    second_price = priced[1][1] if len(priced) > 1 else 0.0
    leader_bucket = _market_bucket(leader)
    base = {
        "marketLeader": {
            "marketId": str(leader.get("marketId")),
            "outcomeRange": leader.get("outcomeRange"),
            "bucketC": leader_bucket,
            "yesMidpoint": round(leader_price, 4),
            "yesExecutableBuyPrice5": finite(leader.get("yesExecutableBuyPrice5")),
        },
        "leaderGap": round(leader_price - second_price, 4),
        "marketPriorPolicy": "The market is the default prior; disagreement needs strong process evidence.",
        "ridgeStatus": ridge.get("status"),
    }
    min_leader = float(config.get("marketLeaderMinMidpoint", 0.20))
    min_gap = float(config.get("marketLeaderMinGap", 0.05))
    if leader_price < min_leader or leader_price - second_price < min_gap:
        return {
            **base, "mode": "NO_EDGE", "weatherAlignment": "unclear",
            "reason": "The market has not formed a sufficiently distinct leader.", "candidates": [],
        }
    if ridge.get("status") != "ok" or leader_bucket is None:
        return {
            **base, "mode": "WATCH", "weatherAlignment": "unresolved",
            "reason": "The market has a leader, but Ridge V2 is unavailable or cannot map the leader bucket.",
            "candidates": [],
        }

    primary = int(ridge["primaryBucketC"])
    warm_tail = int(ridge["warmTailBucketC"])
    plausible = {int(value) for value in ridge.get("plausibleBucketsC") or []}
    trend = finite((ridge.get("inputs") or {}).get("trend120CPerHour"))
    remaining = finite((ridge.get("inputs") or {}).get("remainingSolarFraction")) or 0.0
    detected = {str(item) for item in weather_process.get("detectedProcesses") or []}
    cap_terms = ("rain", "cold", "sea_breeze", "cloud_arrival", "heating_ended", "capping")
    capping_signal = any(any(term in item.casefold() for term in cap_terms) for item in detected)
    active_heating = trend is not None and trend > 0.25 and remaining > 0.08 and not capping_signal
    warm_supported = (
        str((ridge.get("pathStatus") or {}).get("warmTail")) == "supported_competitor"
    )
    deviation = primary - leader_bucket
    premature_price = float(config.get("marketPrematureConvergenceMidpoint", 0.75))
    upward_overshoot = (
        leader_price >= premature_price and active_heating and warm_supported
        and warm_tail >= leader_bucket + 1 and primary >= leader_bucket
    )
    process_contradiction = (
        abs(deviation) >= float(config.get("ridgeV2StrongDeviationC", 2.0))
        and ((deviation > 0 and active_heating) or (deviation < 0 and capping_signal))
    )

    def candidate(market: dict[str, Any], side: str, entry_type: str, reason: str) -> dict[str, Any]:
        return {
            "marketId": str(market.get("marketId")), "outcomeRange": market.get("outcomeRange"),
            "outcomeSide": side, "entryType": entry_type, "reason": reason,
        }

    candidates: list[dict[str, Any]] = []
    if upward_overshoot:
        candidates.append(candidate(
            leader, "NO", "NO_LEADER_OVERSHOOT",
            "The priced leader may be an intermediate touched bucket while a supported upper path remains open.",
        ))
        for market, _price in priced:
            bucket = _market_bucket(market)
            if bucket is not None and leader_bucket < bucket <= warm_tail and bucket in plausible:
                candidates.append(candidate(
                    market, "YES", "YES_LADDER_EXPERIMENT",
                    "Upper plausible bucket available only as part of a fully validated adjacent YES ladder.",
                ))
        return {
            **base, "mode": "FADE", "weatherAlignment": "contradicted",
            "reason": "The market is highly concentrated on the current leader while active heating and a supported warm-tail path remain.",
            "activeHeating": active_heating, "marketPrematureConvergence": True,
            "ridgePrimaryBucketC": primary, "ridgeWarmTailBucketC": warm_tail,
            "candidates": candidates,
        }

    if process_contradiction:
        if leader_bucket not in plausible:
            candidates.append(candidate(
                leader, "NO", "NO_EXCLUSION",
                "The market leader lies outside the Ridge V2 plausible path and the observed process corroborates the deviation.",
            ))
        return {
            **base, "mode": "FADE", "weatherAlignment": "contradicted",
            "reason": "Ridge V2 and the observed weather process materially contradict the market leader.",
            "activeHeating": active_heating, "marketPrematureConvergence": False,
            "ridgePrimaryBucketC": primary, "ridgeWarmTailBucketC": warm_tail,
            "candidates": candidates,
        }

    if primary == leader_bucket:
        candidates.append(candidate(
            leader, "YES", "YES_CONVERGENCE",
            "Market leader and Ridge V2 primary bucket agree; AI must still verify unique-bucket convergence and value.",
        ))
        for market, _price in priced:
            bucket = _market_bucket(market)
            if bucket is not None and bucket not in plausible:
                candidates.append(candidate(
                    market, "NO", "NO_EXCLUSION",
                    "Bucket is outside the Ridge V2 plausible path; AI must independently verify physical exclusion.",
                ))
        return {
            **base, "mode": "FOLLOW", "weatherAlignment": "aligned",
            "reason": "The market leader agrees with the Ridge V2 primary path and no strong process contradiction is present.",
            "activeHeating": active_heating, "marketPrematureConvergence": False,
            "ridgePrimaryBucketC": primary, "ridgeWarmTailBucketC": warm_tail,
            "candidates": candidates,
        }

    return {
        **base, "mode": "WATCH", "weatherAlignment": "unresolved",
        "reason": "The market leader and Ridge V2 are close but not aligned, without enough process evidence to fade the market.",
        "activeHeating": active_heating, "marketPrematureConvergence": False,
        "ridgePrimaryBucketC": primary, "ridgeWarmTailBucketC": warm_tail,
        "candidates": [],
    }
