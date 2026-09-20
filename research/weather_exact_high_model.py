#!/usr/bin/env python3
"""Research-only exact daily-high model with date-walk-forward evaluation."""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data/weather_market_monitor.sqlite3"
OUTPUT_DIR = ROOT / "research/output"
MODEL_DIR = ROOT / "data/models"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
TRADE_CITIES = [
    "Shanghai", "Beijing", "Guangzhou", "Qingdao", "Wuhan", "Chongqing", "Chengdu"
]
CUTOFF_HOURS = (10, 12, 14, 16)
BASE_NUMERIC_FEATURES = [
    "mblue", "ecmwf", "model_spread", "mblue_revision", "ecmwf_revision",
    "observed_temp", "observed_max", "observed_minus_mblue", "dewpoint",
    "dewpoint_depression", "relative_humidity", "wind_speed", "temperature_trend_per_hour",
    "observation_age_minutes", "observation_count", "latitude", "longitude",
]


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def exact_temperature(range_text: str | None) -> float | None:
    """Return an exact integer bucket label; open-ended buckets are censored."""
    if not range_text or "or" in range_text.casefold():
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", range_text)
    return float(match.group()) if match else None


def round_half_up(values: np.ndarray) -> np.ndarray:
    return np.floor(np.asarray(values, dtype=float) + 0.5)


class DatasetBuilder:
    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row

    def close(self) -> None:
        self.db.close()

    def one(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        return self.db.execute(sql, params).fetchone()

    def model_rows(
        self, table: str, station_id: str, target_date: str, cutoff_utc: str, model: str
    ) -> list[sqlite3.Row]:
        model_column = "model"
        return self.db.execute(
            f"""
            SELECT forecast_max_c,slot_utc FROM {table}
            WHERE station_id=? AND target_date=? AND {model_column}=? AND status='ok'
              AND forecast_max_c IS NOT NULL AND slot_utc<=?
            ORDER BY slot_utc DESC LIMIT 2
            """,
            (station_id, target_date, model, cutoff_utc),
        ).fetchall()

    def build(self, cutoff_hour: int) -> pd.DataFrame:
        events = self.db.execute(
            """
            SELECT e.*,s.latitude,s.longitude,s.timezone
            FROM events e JOIN stations s ON s.station_id=e.station_id
            WHERE e.station_id IS NOT NULL AND s.timezone IS NOT NULL
            ORDER BY e.target_date,e.city,e.last_seen_utc
            """
        ).fetchall()
        rows: list[dict[str, Any]] = []
        for event in events:
            try:
                local_tz = ZoneInfo(event["timezone"])
                local_cutoff = datetime.combine(
                    date.fromisoformat(event["target_date"]), time(cutoff_hour), tzinfo=local_tz
                )
            except (ValueError, TypeError):
                continue
            cutoff_utc = iso_utc(local_cutoff)
            mblue_rows = self.model_rows(
                "windy_forecasts", event["station_id"], event["target_date"], cutoff_utc, "mblue"
            )
            if not mblue_rows:
                continue
            ecmwf_rows = self.model_rows(
                "external_forecasts", event["station_id"], event["target_date"], cutoff_utc,
                "ecmwf_ifs025",
            )
            observations = self.db.execute(
                """
                SELECT * FROM weather_observations
                WHERE station_id=? AND source='metar' AND status='ok'
                  AND sample_local_date=? AND observation_time_utc IS NOT NULL
                  AND observation_time_utc<=?
                ORDER BY observation_time_utc DESC,slot_utc DESC LIMIT 2
                """,
                (event["station_id"], event["target_date"], cutoff_utc),
            ).fetchall()
            observation_count = self.db.execute(
                """
                SELECT COUNT(DISTINCT observation_time_utc) FROM weather_observations
                WHERE station_id=? AND source='metar' AND status='ok'
                  AND sample_local_date=? AND observation_time_utc<=?
                """,
                (event["station_id"], event["target_date"], cutoff_utc),
            ).fetchone()[0]

            current = observations[0] if observations else None
            previous = observations[1] if len(observations) > 1 else None
            mblue = as_float(mblue_rows[0]["forecast_max_c"])
            ecmwf = as_float(ecmwf_rows[0]["forecast_max_c"]) if ecmwf_rows else None
            observed_temp = as_float(current["temperature_c"]) if current else None
            observed_max = as_float(current["observed_daily_max_c"]) if current else None
            if observed_max is None:
                observed_max = observed_temp
            dewpoint = as_float(current["dewpoint_c"]) if current else None
            trend = None
            if current and previous:
                current_time = datetime.fromisoformat(current["observation_time_utc"])
                previous_time = datetime.fromisoformat(previous["observation_time_utc"])
                elapsed_hours = (current_time - previous_time).total_seconds() / 3600
                previous_temp = as_float(previous["temperature_c"])
                if elapsed_hours > 0 and observed_temp is not None and previous_temp is not None:
                    trend = (observed_temp - previous_temp) / elapsed_hours
            observation_age = None
            if current:
                observation_age = (
                    local_cutoff.astimezone(timezone.utc)
                    - datetime.fromisoformat(current["observation_time_utc"]).astimezone(timezone.utc)
                ).total_seconds() / 60

            target = exact_temperature(event["winning_range"])
            rows.append({
                "event_id": event["event_id"], "target_date": event["target_date"],
                "city": event["city"], "station_id": event["station_id"],
                "cutoff_hour": cutoff_hour, "cutoff_utc": cutoff_utc,
                "resolved": target is not None, "target": target,
                "mblue": mblue, "ecmwf": ecmwf,
                "model_spread": ecmwf - mblue if ecmwf is not None and mblue is not None else None,
                "mblue_revision": (
                    mblue - float(mblue_rows[1]["forecast_max_c"])
                    if len(mblue_rows) > 1 and mblue_rows[1]["forecast_max_c"] is not None else None
                ),
                "ecmwf_revision": (
                    ecmwf - float(ecmwf_rows[1]["forecast_max_c"])
                    if ecmwf is not None and len(ecmwf_rows) > 1
                    and ecmwf_rows[1]["forecast_max_c"] is not None else None
                ),
                "observed_temp": observed_temp, "observed_max": observed_max,
                "observed_minus_mblue": (
                    observed_max - mblue if observed_max is not None and mblue is not None else None
                ),
                "dewpoint": dewpoint,
                "dewpoint_depression": (
                    observed_temp - dewpoint
                    if observed_temp is not None and dewpoint is not None else None
                ),
                "relative_humidity": as_float(current["relative_humidity"]) if current else None,
                "wind_speed": as_float(current["wind_speed"]) if current else None,
                "temperature_trend_per_hour": trend,
                "observation_age_minutes": observation_age,
                "observation_count": float(observation_count),
                "latitude": as_float(event["latitude"]), "longitude": as_float(event["longitude"]),
                "winning_range": event["winning_range"],
            })
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        # Keep the most recently discovered event if duplicate city/date rows exist.
        return frame.drop_duplicates(["event_id", "cutoff_hour"], keep="last").reset_index(drop=True)


def available_numeric_features(train: pd.DataFrame) -> list[str]:
    return [name for name in BASE_NUMERIC_FEATURES if name in train and train[name].notna().any()]


def make_model(train: pd.DataFrame) -> tuple[Pipeline, list[str]]:
    numeric = available_numeric_features(train)
    preprocessing = ColumnTransformer([
        (
            "numeric",
            Pipeline([
                ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                ("scaler", StandardScaler()),
            ]),
            numeric,
        ),
        ("city", OneHotEncoder(handle_unknown="ignore"), ["city"]),
    ])
    # The model predicts residual to Meteoblue, which is more stable than fitting
    # absolute temperature from fewer than one hundred event-days.
    model = Pipeline([
        ("preprocessing", preprocessing),
        ("regressor", Ridge(alpha=10.0)),
    ])
    model.fit(train, train["target"] - train["mblue"])
    return model, numeric


def metric_row(target: np.ndarray, prediction: np.ndarray) -> dict[str, float]:
    error = prediction - target
    return {
        "mae_c": round(float(np.mean(np.abs(error))), 4),
        "rmse_c": round(float(np.sqrt(np.mean(error * error))), 4),
        "bias_c": round(float(np.mean(error)), 4),
        "exact_bucket_accuracy": round(float(np.mean(round_half_up(prediction) == round_half_up(target))), 4),
        "within_one_c": round(float(np.mean(np.abs(error) <= 1.0 + 1e-9)), 4),
    }


def observed_floor(frame: pd.DataFrame, prediction: np.ndarray) -> np.ndarray:
    """A daily maximum can never finish below an already observed maximum."""
    floor = frame["observed_max"].to_numpy(dtype=float)
    floor = np.where(np.isfinite(floor), floor, -np.inf)
    return np.maximum(np.asarray(prediction, dtype=float), floor)


def walk_forward(frame: pd.DataFrame) -> dict[str, Any]:
    resolved = frame[frame["resolved"]].copy()
    dates = sorted(resolved["target_date"].unique())
    predictions: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    for index in range(2, len(dates)):
        train_dates = dates[:index]
        test_date = dates[index]
        train = resolved[resolved["target_date"].isin(train_dates)]
        test = resolved[
            (resolved["target_date"] == test_date) & resolved["city"].isin(TRADE_CITIES)
        ]
        if train.empty or test.empty:
            continue
        model, numeric = make_model(train)
        model_prediction = observed_floor(
            test, test["mblue"].to_numpy() + model.predict(test)
        )
        train_bias = float((train["target"] - train["mblue"]).mean())
        bias_prediction = observed_floor(test, test["mblue"].to_numpy() + train_bias)
        blend_prediction = observed_floor(test, (
            0.75 * test["mblue"].to_numpy()
            + 0.25 * test["ecmwf"].fillna(test["mblue"]).to_numpy()
        ))
        mblue_prediction = observed_floor(test, test["mblue"].to_numpy())
        fold_target = test["target"].to_numpy()
        fold_rows.append({
            "test_date": test_date, "train_event_days": int(len(train)),
            "test_events": int(len(test)), "numeric_features": numeric,
            "meteoblue": metric_row(fold_target, mblue_prediction),
            "bias_corrected": metric_row(fold_target, bias_prediction),
            "blend_75_25": metric_row(fold_target, blend_prediction),
            "ridge_residual": metric_row(fold_target, model_prediction),
        })
        for row_index, (_, row) in enumerate(test.iterrows()):
            predictions.append({
                "event_id": row["event_id"], "target_date": row["target_date"],
                "city": row["city"], "actual_c": float(row["target"]),
                "meteoblue_c": float(mblue_prediction[row_index]),
                "bias_corrected_c": float(bias_prediction[row_index]),
                "blend_c": float(blend_prediction[row_index]),
                "model_c": float(model_prediction[row_index]),
            })

    if not predictions:
        return {"oos_events": 0, "folds": fold_rows, "predictions": []}
    target = np.array([row["actual_c"] for row in predictions])
    methods = {
        "meteoblue": np.array([row["meteoblue_c"] for row in predictions]),
        "bias_corrected": np.array([row["bias_corrected_c"] for row in predictions]),
        "blend_75_25": np.array([row["blend_c"] for row in predictions]),
        "ridge_residual": np.array([row["model_c"] for row in predictions]),
    }
    model_error = methods["ridge_residual"] - target
    aggregate_metrics = {name: metric_row(target, value) for name, value in methods.items()}
    worst_fold_mae = max(
        (float(row["ridge_residual"]["mae_c"]) for row in fold_rows), default=float("inf")
    )
    status = (
        "unstable"
        if aggregate_metrics["ridge_residual"]["mae_c"]
        >= aggregate_metrics["meteoblue"]["mae_c"] or worst_fold_mae > 1.5
        else "experimental_small_sample"
    )
    return {
        "oos_events": len(predictions),
        "test_dates": sorted({row["target_date"] for row in predictions}),
        "status": status,
        "worst_fold_mae_c": round(worst_fold_mae, 4),
        "metrics": aggregate_metrics,
        "model_abs_error_q80_c": round(float(np.quantile(np.abs(model_error), 0.80)), 3),
        "model_abs_error_q90_c": round(float(np.quantile(np.abs(model_error), 0.90)), 3),
        "folds": fold_rows,
        "predictions": predictions,
    }


def train_and_predict(
    frame: pd.DataFrame, cutoff_hour: int, evaluation: dict[str, Any], target_date: str
) -> tuple[dict[str, Any], Path]:
    train = frame[frame["resolved"] & (frame["target_date"] < target_date)].copy()
    current = frame[
        (~frame["resolved"]) & (frame["target_date"] == target_date)
        & frame["city"].isin(TRADE_CITIES)
    ].copy()
    model, numeric = make_model(train)
    artifact_path = MODEL_DIR / f"weather_exact_high_{cutoff_hour:02d}00.joblib"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model": model, "numeric_features": numeric, "cutoff_hour": cutoff_hour,
        "trained_through": max(train["target_date"]), "train_event_days": len(train),
        "cities": TRADE_CITIES, "research_only": True,
    }, artifact_path)
    predictions = []
    if not current.empty:
        q80 = float(evaluation.get("model_abs_error_q80_c") or 1.0)
        q90 = float(evaluation.get("model_abs_error_q90_c") or 1.5)
        for index, (_, row) in enumerate(current.sort_values("city").iterrows()):
            # Recalculate in sorted order so the value remains aligned.
            raw_prediction = float(row["mblue"] + model.predict(pd.DataFrame([row]))[0])
            observed_max = as_float(row["observed_max"])
            prediction = max(raw_prediction, observed_max) if observed_max is not None else raw_prediction
            predictions.append({
                "city": row["city"], "target_date": row["target_date"],
                "cutoff_local_hour": cutoff_hour, "prediction_c": round(prediction, 2),
                "predicted_bucket_c": int(math.floor(prediction + 0.5)),
                "interval80_c": [round(prediction - q80, 2), round(prediction + q80, 2)],
                "interval90_c": [round(prediction - q90, 2), round(prediction + q90, 2)],
                "meteoblue_c": as_float(row["mblue"]), "ecmwf_c": as_float(row["ecmwf"]),
                "observed_max_c": as_float(row["observed_max"]),
                "observation_age_minutes": as_float(row["observation_age_minutes"]),
            })
    return {
        "train_event_days": int(len(train)), "trained_through": max(train["target_date"]),
        "numeric_features": numeric, "predictions": predictions,
    }, artifact_path


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# 具体最高温研究模型", "",
        f"生成时间：{report['generated_at_utc']}", "",
        "> 研究用途，未接入 Paper 或实盘交易。所有评估按目标日期走步，测试日不会进入训练集。", "",
        "## 样本外结果", "",
        "| 当地截点 | 状态 | OOS事件 | 模型MAE | 模型整数档命中 | ±1°C | Meteoblue MAE |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for cutoff in report["cutoffs"]:
        evaluation = cutoff["evaluation"]
        model = (evaluation.get("metrics") or {}).get("ridge_residual") or {}
        baseline = (evaluation.get("metrics") or {}).get("meteoblue") or {}
        lines.append(
            f"| {cutoff['cutoff_hour']:02d}:00 | {evaluation.get('status', 'unknown')} | "
            f"{evaluation.get('oos_events', 0)} | "
            f"{model.get('mae_c', 'N/A')} | {model.get('exact_bucket_accuracy', 'N/A')} | "
            f"{model.get('within_one_c', 'N/A')} | {baseline.get('mae_c', 'N/A')} |"
        )
    lines.extend([
        "",
        f"当前研究首选截点：{report.get('preferred_cutoff_hour', 'N/A')}:00。",
        "",
        "## 当前目标日预测", "",
    ])
    chosen = next((item for item in report["cutoffs"] if item["cutoff_hour"] == 14), None)
    predictions = ((chosen or {}).get("final_model") or {}).get("predictions") or []
    if predictions:
        lines.extend([
            "| 城市 | 预测最高温 | 预测档 | 80%经验区间 | Meteoblue | ECMWF | 截点已观测最高 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ])
        for row in predictions:
            lines.append(
                f"| {row['city']} | {row['prediction_c']:.2f}°C | {row['predicted_bucket_c']}°C | "
                f"{row['interval80_c'][0]:.2f}-{row['interval80_c'][1]:.2f}°C | "
                f"{row['meteoblue_c']} | {row['ecmwf_c']} | {row['observed_max_c']} |"
            )
    else:
        lines.append("当前目标日没有可预测的未结算七城事件。")
    lines.extend([
        "", "## 限制", "",
        "- 当前只有少量独立事件日，30分钟快照不是独立样本。",
        "- 经验区间来自同一小样本走步残差，不是经过长期校准的概率区间。",
        "- 雷达、卫星和天气过程尚未加入模型，因为已结算历史不足。",
        "- 模型不读取盘口，输出不能直接解释为交易 edge。",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-date", default=datetime.now(LOCAL_TZ).date().isoformat())
    args = parser.parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    builder = DatasetBuilder(DB_PATH)
    report: dict[str, Any] = {
        "generated_at_utc": iso_utc(datetime.now(timezone.utc)),
        "target_date": args.target_date,
        "trade_cities": TRADE_CITIES,
        "method": "Ridge regression on final-minus-Meteoblue residual; date walk-forward OOS",
        "research_only": True,
        "cutoffs": [],
    }
    try:
        for cutoff_hour in CUTOFF_HOURS:
            frame = builder.build(cutoff_hour)
            evaluation = walk_forward(frame)
            final_model, artifact = train_and_predict(
                frame, cutoff_hour, evaluation, args.target_date
            )
            report["cutoffs"].append({
                "cutoff_hour": cutoff_hour,
                "dataset_rows": int(len(frame)),
                "resolved_rows": int(frame["resolved"].sum()),
                "evaluation": evaluation,
                "final_model": final_model,
                "artifact": str(artifact),
            })
    finally:
        builder.close()

    stable_candidates = [
        item for item in report["cutoffs"]
        if item["evaluation"].get("status") != "unstable"
        and item["evaluation"].get("metrics", {}).get("ridge_residual")
    ]
    report["preferred_cutoff_hour"] = min(
        stable_candidates,
        key=lambda item: item["evaluation"]["metrics"]["ridge_residual"]["mae_c"],
    )["cutoff_hour"] if stable_candidates else None

    json_path = OUTPUT_DIR / "weather_exact_high_model_report.json"
    markdown_path = OUTPUT_DIR / "weather_exact_high_model_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    print(json.dumps({
        "report": str(json_path), "markdown": str(markdown_path),
        "target_date": args.target_date,
        "cutoffs": [
            {
                "hour": item["cutoff_hour"],
                "oos_events": item["evaluation"].get("oos_events"),
                "metrics": item["evaluation"].get("metrics", {}).get("ridge_residual"),
                "predictions": len(item["final_model"].get("predictions") or []),
            }
            for item in report["cutoffs"]
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
