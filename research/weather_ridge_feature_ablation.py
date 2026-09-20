#!/usr/bin/env python3
"""Point-in-time ablation for weather-process fields in the Ridge center model."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.weather_exact_high_model_v2 import (
    DB_PATH,
    NUMERIC_FEATURES,
    SHARED_TIME_FEATURES,
    DatasetBuilderV2,
    physically_constrained_predictions,
    round_half_up,
)


OUTPUT_PATH = ROOT / "research/output/weather_ridge_feature_ablation.json"
CUTOFFS = ((10, 30), (11, 0))
MIN_TRAIN_DATES = 7
PROXY_MAX_AGE_MINUTES = 90.0

FIELD_GROUPS = {
    "wind_direction": ["wind_direction_sin", "wind_direction_cos"],
    "wind_gust": ["wind_gust_kt", "wind_gust_present"],
    "precipitation": ["precipitation_mm"],
    "visibility": ["visibility_log_m"],
    "solar_radiation": ["solar_radiation_wm2"],
}


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def add_derived_fields(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    radians = np.deg2rad(pd.to_numeric(output["wind_direction_deg"], errors="coerce"))
    output["wind_direction_sin"] = np.sin(radians)
    output["wind_direction_cos"] = np.cos(radians)
    gust = pd.to_numeric(output["wind_gust_kt"], errors="coerce")
    output["wind_gust_present"] = gust.notna().astype(float)
    visibility = pd.to_numeric(output["visibility_m"], errors="coerce")
    output["visibility_log_m"] = np.log1p(visibility.clip(lower=0))
    return output


def enrich_open_meteo_proxies(frame: pd.DataFrame, db_path: Path) -> pd.DataFrame:
    """Attach only proxy observations that were available at the feature cutoff."""
    output = frame.copy()
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cache: dict[tuple[str, str], tuple[float | None, float | None, float | None]] = {}
    precipitation: list[float | None] = []
    solar: list[float | None] = []
    proxy_age: list[float | None] = []
    try:
        for row in output.itertuples(index=False):
            key = (str(row.station_id), str(row.feature_as_of_utc))
            values = cache.get(key)
            if values is None:
                proxy = db.execute(
                    """
                    SELECT precipitation_mm,solar_radiation_wm2,
                           (julianday(?) - julianday(observation_time_utc))*1440.0 AS age_minutes
                    FROM weather_observations
                    WHERE station_id=? AND source='open_meteo_current' AND status='ok'
                      AND julianday(observation_time_utc)<=julianday(?)
                      AND julianday(slot_utc)<=julianday(?)
                      AND julianday(fetched_at_utc)<=julianday(?)
                    ORDER BY julianday(observation_time_utc) DESC,
                             julianday(fetched_at_utc) DESC LIMIT 1
                    """,
                    (key[1], key[0], key[1], key[1], key[1]),
                ).fetchone()
                age = finite(proxy["age_minutes"]) if proxy else None
                values = (
                    finite(proxy["precipitation_mm"]) if proxy and age is not None and age <= PROXY_MAX_AGE_MINUTES else None,
                    finite(proxy["solar_radiation_wm2"]) if proxy and age is not None and age <= PROXY_MAX_AGE_MINUTES else None,
                    age,
                )
                cache[key] = values
            precipitation.append(values[0])
            solar.append(values[1])
            proxy_age.append(values[2])
    finally:
        db.close()
    output["precipitation_mm"] = precipitation
    output["solar_radiation_wm2"] = solar
    output["open_meteo_proxy_age_minutes"] = proxy_age
    return output


def build_frame(db_path: Path) -> pd.DataFrame:
    builder = DatasetBuilderV2(db_path)
    try:
        frames = [builder.build(hour, minute) for hour, minute in CUTOFFS]
    finally:
        builder.close()
    usable = [frame for frame in frames if not frame.empty]
    if not usable:
        return pd.DataFrame()
    frame = pd.concat(usable, ignore_index=True)
    frame = enrich_open_meteo_proxies(frame, db_path)
    return add_derived_fields(frame)


def make_model(train: pd.DataFrame, extra_features: list[str]) -> Pipeline:
    candidates = NUMERIC_FEATURES + SHARED_TIME_FEATURES + extra_features
    numeric = [name for name in candidates if name in train and train[name].notna().any()]
    preprocessing = ColumnTransformer([
        ("numeric", Pipeline([
            ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
            ("scaler", StandardScaler()),
        ]), numeric),
        ("city", OneHotEncoder(handle_unknown="ignore"), ["city"]),
    ])
    model = Pipeline([
        ("preprocessing", preprocessing),
        ("regressor", Ridge(alpha=20.0)),
    ])
    model.fit(train, train["target"] - train["prior_center"])
    return model


def metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    error = predicted - actual
    actual_bucket = round_half_up(actual)
    predicted_bucket = round_half_up(predicted)
    return {
        "maeC": round(float(np.mean(np.abs(error))), 4),
        "rmseC": round(float(np.sqrt(np.mean(error * error))), 4),
        "biasC": round(float(np.mean(error)), 4),
        "centerBucketAccuracy": round(float(np.mean(predicted_bucket == actual_bucket)), 4),
        "threeBucketCoverage": round(float(np.mean(np.abs(predicted_bucket - actual_bucket) <= 1)), 4),
    }


def walk_forward(frame: pd.DataFrame, extra_features: list[str]) -> dict[str, Any]:
    resolved = frame[frame["resolved"]].copy()
    dates = sorted(str(value) for value in resolved["target_date"].unique())
    actual_rows: list[float] = []
    predicted_rows: list[float] = []
    evaluated_dates: list[str] = []
    daily: dict[str, dict[str, float]] = {}
    for index in range(MIN_TRAIN_DATES, len(dates)):
        train = resolved[resolved["target_date"].isin(dates[:index])]
        test = resolved[resolved["target_date"] == dates[index]]
        if train.empty or test.empty:
            continue
        model = make_model(train, extra_features)
        prediction = physically_constrained_predictions(
            test, test["prior_center"].to_numpy(dtype=float) + model.predict(test)
        )
        actual_rows.extend(test["target"].to_numpy(dtype=float))
        predicted_rows.extend(prediction)
        evaluated_dates.append(dates[index])
        daily[dates[index]] = metrics(
            test["target"].to_numpy(dtype=float), np.asarray(prediction)
        )
    if not actual_rows:
        return {"events": 0, "dates": [], "metrics": {}}
    return {
        "events": len(actual_rows),
        "dates": sorted(set(evaluated_dates)),
        "metrics": metrics(np.asarray(actual_rows), np.asarray(predicted_rows)),
        "dailyMetrics": daily,
    }


def availability(frame: pd.DataFrame) -> dict[str, Any]:
    fields = {
        "windDirection": "wind_direction_deg",
        "windGust": "wind_gust_kt",
        "precipitationOpenMeteoProxy": "precipitation_mm",
        "visibility": "visibility_m",
        "solarRadiationOpenMeteoProxy": "solar_radiation_wm2",
    }
    return {
        label: {
            "rows": int(frame[column].notna().sum()),
            "coverage": round(float(frame[column].notna().mean()), 4),
        }
        for label, column in fields.items()
    }


def run(db_path: Path) -> dict[str, Any]:
    frame = build_frame(db_path)
    if frame.empty:
        raise RuntimeError("ablation frame is empty")
    variants: dict[str, list[str]] = {"baseline": []}
    variants.update(FIELD_GROUPS)
    variants["wind_direction_visibility"] = (
        FIELD_GROUPS["wind_direction"] + FIELD_GROUPS["visibility"]
    )
    variants["visibility_precipitation"] = (
        FIELD_GROUPS["visibility"] + FIELD_GROUPS["precipitation"]
    )
    variants["all_five"] = [field for fields in FIELD_GROUPS.values() for field in fields]
    results = {name: walk_forward(frame, fields) for name, fields in variants.items()}
    baseline = results["baseline"].get("metrics") or {}
    for result in results.values():
        current = result.get("metrics") or {}
        result["deltaVsBaseline"] = {
            "maeC": round(float(current.get("maeC", 0)) - float(baseline.get("maeC", 0)), 4),
            "centerBucketAccuracy": round(
                float(current.get("centerBucketAccuracy", 0))
                - float(baseline.get("centerBucketAccuracy", 0)), 4
            ),
            "threeBucketCoverage": round(
                float(current.get("threeBucketCoverage", 0))
                - float(baseline.get("threeBucketCoverage", 0)), 4
            ),
        }
        daily = result.get("dailyMetrics") or {}
        baseline_daily = results["baseline"].get("dailyMetrics") or {}
        common_dates = sorted(set(daily) & set(baseline_daily))
        result["dailyWinsVsBaseline"] = {
            "mae": sum(daily[day]["maeC"] < baseline_daily[day]["maeC"] for day in common_dates),
            "centerBucketAccuracy": sum(
                daily[day]["centerBucketAccuracy"] > baseline_daily[day]["centerBucketAccuracy"]
                for day in common_dates
            ),
            "threeBucketCoverage": sum(
                daily[day]["threeBucketCoverage"] > baseline_daily[day]["threeBucketCoverage"]
                for day in common_dates
            ),
            "evaluatedDates": len(common_dates),
        }
    return {
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database": str(db_path),
        "cutoffsLocal": [f"{hour:02d}:{minute:02d}" for hour, minute in CUTOFFS],
        "minimumPriorTrainingDates": MIN_TRAIN_DATES,
        "frameRows": len(frame),
        "independentDates": sorted(str(value) for value in frame["target_date"].unique()),
        "availability": availability(frame),
        "sourceNotes": {
            "windDirection": "METAR; encoded as circular sine/cosine",
            "windGust": "METAR; sparse gust reports plus an availability indicator",
            "precipitation": "Open-Meteo current proxy, not a rain-gauge observation",
            "visibility": "METAR",
            "solarRadiation": "Open-Meteo current shortwave proxy, not station pyranometer data",
        },
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DB_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = run(args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
