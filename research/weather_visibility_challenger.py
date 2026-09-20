#!/usr/bin/env python3
"""Train the visibility-only Ridge challenger for shadow comparison."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.weather_exact_high_model_v2 import (
    DB_PATH,
    DatasetBuilderV2,
    _atomic_joblib_dump,
)
from research.weather_ridge_feature_ablation import make_model, walk_forward


ARTIFACT_PATH = ROOT / "data/models/weather_visibility_challenger_v1.joblib"
CUTOFFS = ((10, 30), (11, 0))
VISIBILITY_FEATURES = ["visibility_log_m"]


def build_visibility_frame(db_path: Path) -> pd.DataFrame:
    builder = DatasetBuilderV2(db_path)
    try:
        frames = [builder.build(hour, minute) for hour, minute in CUTOFFS]
    finally:
        builder.close()
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    frame = pd.concat(frames, ignore_index=True)
    visibility = pd.to_numeric(frame["visibility_m"], errors="coerce")
    frame["visibility_log_m"] = np.log1p(visibility.clip(lower=0))
    return frame


def train_visibility_artifact(
    db_path: Path, target_date: str, as_of_utc: datetime,
    artifact_path: Path = ARTIFACT_PATH,
) -> tuple[dict[str, Any], Path]:
    frame = build_visibility_frame(db_path)
    if frame.empty:
        raise RuntimeError("visibility challenger dataset is empty")
    train = frame[frame["resolved"] & (frame["target_date"] < target_date)].copy()
    if train.empty:
        raise RuntimeError(f"no resolved visibility rows before {target_date}")
    baseline = walk_forward(train, [])
    challenger = walk_forward(train, VISIBILITY_FEATURES)
    model = make_model(train, VISIBILITY_FEATURES)
    payload = {
        "model": model,
        "model_version": "ridge_visibility_challenger_v1",
        "feature": "visibility_log_m",
        "source": "METAR visibility_m",
        "cutoffs_local": ["10:30", "11:00"],
        "trained_at_utc": as_of_utc.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "trained_through": str(max(train["target_date"])),
        "training_target_date": target_date,
        "train_rows": int(len(train)),
        "visibility_rows": int(train["visibility_log_m"].notna().sum()),
        "visibility_coverage": round(float(train["visibility_log_m"].notna().mean()), 4),
        "point_in_time_data": True,
        "shadow_only": True,
        "authoritative": False,
        "baseline_evaluation": baseline,
        "challenger_evaluation": challenger,
    }
    _atomic_joblib_dump(payload, artifact_path)
    report = {key: value for key, value in payload.items() if key != "model"}
    return report, artifact_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DB_PATH)
    parser.add_argument("--target-date", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--artifact", type=Path, default=ARTIFACT_PATH)
    args = parser.parse_args()
    report, path = train_visibility_artifact(
        args.database, args.target_date, datetime.now(timezone.utc), args.artifact
    )
    print(json.dumps({"artifact": str(path), **report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
