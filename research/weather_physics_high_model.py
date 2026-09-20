#!/usr/bin/env python3
"""Physics-constrained daily-high model driven by same-day observations.

This is a research/shadow model. Forecast models and market prices are not
features: the target is the remaining temperature rise implied by the observed
surface-energy and weather-process trajectory.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from weather_process_analyzer import solar_position
from research.weather_sounding import SoundingClient, SoundingFeatures


DB_PATH = ROOT / "data/weather_market_monitor.sqlite3"
OUTPUT_DIR = ROOT / "research/output"
SOUNDING_CACHE_DIR = ROOT / "data/soundings"
TRADE_CITIES = [
    "Shanghai", "Beijing", "Guangzhou", "Qingdao", "Wuhan", "Chongqing", "Chengdu"
]
CUTOFF_HOURS = (10, 12, 14, 16)
UTC = timezone.utc

# A clear-sky proxy below this level usually cannot sustain positive sensible
# heating against long-wave, turbulent and ground heat losses. It is a physical
# structural assumption, not a coefficient selected against PnL or bucket hits.
SOLAR_MAINTENANCE_WM2 = 250.0


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def exact_temperature(range_text: str | None) -> float | None:
    if not range_text or "or" in range_text.casefold():
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", range_text)
    return float(match.group()) if match else None


def round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def pairwise_slope(rows: list[dict[str, Any]], minutes: int) -> float | None:
    if not rows:
        return None
    newest = parse_utc(rows[-1]["observation_time_utc"])
    if newest is None:
        return None
    selected = [
        row for row in rows
        if (when := parse_utc(row["observation_time_utc"])) is not None
        and newest - when <= timedelta(minutes=minutes)
    ]
    slopes: list[float] = []
    for left_index, left in enumerate(selected):
        left_time = parse_utc(left["observation_time_utc"])
        left_temp = finite(left["temperature_c"])
        if left_time is None or left_temp is None:
            continue
        for right in selected[left_index + 1:]:
            right_time = parse_utc(right["observation_time_utc"])
            right_temp = finite(right["temperature_c"])
            if right_time is None or right_temp is None:
                continue
            hours = (right_time - left_time).total_seconds() / 3600.0
            if hours >= 0.45:
                slopes.append((right_temp - left_temp) / hours)
    return median(slopes) if slopes else None


def solar_energy_kwh_m2(
    latitude: float, longitude: float, start: datetime, end: datetime,
    maintenance_wm2: float = SOLAR_MAINTENANCE_WM2,
) -> float:
    """Integrate usable clear-sky solar proxy above the maintenance load."""
    if end <= start:
        return 0.0
    step = timedelta(minutes=10)
    cursor = start
    energy_wh = 0.0
    while cursor < end:
        next_cursor = min(end, cursor + step)
        midpoint = cursor + (next_cursor - cursor) / 2
        shortwave = solar_position(latitude, longitude, midpoint)["clearSkyShortwaveProxyWm2"]
        useful = max(0.0, shortwave - maintenance_wm2)
        energy_wh += useful * (next_cursor - cursor).total_seconds() / 3600.0
        cursor = next_cursor
    return energy_wh / 1000.0


def cloud_cover_from_sky_json(value: str | None) -> float | None:
    weights = {"SKC": 0.0, "CLR": 0.0, "NSC": 0.0, "FEW": 20.0, "SCT": 45.0, "BKN": 75.0, "OVC": 100.0}
    try:
        rows = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    values = [weights.get(str(row.get("cover") or "").upper()) for row in rows if isinstance(row, dict)]
    values = [item for item in values if item is not None]
    return max(values) if values else None


@dataclass
class PhysicsPrediction:
    prediction_c: float
    predicted_bucket_c: int
    cool_scenario_c: float
    warm_scenario_c: float
    plausible_buckets_c: list[int]
    observed_max_c: float
    current_temperature_c: float
    remaining_rise_c: float
    momentum_rise_c: float
    energy_balance_rise_c: float
    recent_trend_c_per_hour: float
    response_c_per_kwh_m2: float
    past_usable_solar_kwh_m2: float
    remaining_usable_solar_kwh_m2: float
    process_modifier: float
    process_flags: list[str]
    capping_path_c: float
    primary_path_c: float
    warm_tail_path_c: float
    path_status: dict[str, str]
    sounding: dict[str, Any] | None
    confidence: str
    data_warnings: list[str]


class PhysicsHighModel:
    def predict(
        self,
        observations: list[dict[str, Any]],
        latitude: float,
        longitude: float,
        local_tz: ZoneInfo,
        cutoff: datetime,
        process_state: dict[str, Any] | None = None,
        sounding: SoundingFeatures | None = None,
    ) -> PhysicsPrediction:
        usable = [
            row for row in observations
            if finite(row.get("temperature_c")) is not None
            and parse_utc(row.get("observation_time_utc")) is not None
            and parse_utc(row.get("observation_time_utc")) <= cutoff.astimezone(UTC)
        ]
        usable.sort(key=lambda row: parse_utc(row["observation_time_utc"]))
        if len(usable) < 2:
            raise ValueError("at least two same-day temperature observations are required")

        current = usable[-1]
        current_time = parse_utc(current["observation_time_utc"])
        assert current_time is not None
        current_temp = float(current["temperature_c"])
        observed_max = max(float(row["temperature_c"]) for row in usable)
        local_day = current_time.astimezone(local_tz).date()
        morning_start = datetime.combine(local_day, time(7), tzinfo=local_tz).astimezone(UTC)
        morning_rows = [row for row in usable if parse_utc(row["observation_time_utc"]) >= morning_start]
        base_row = morning_rows[0] if morning_rows else usable[0]
        base_time = parse_utc(base_row["observation_time_utc"])
        assert base_time is not None
        base_temp = float(base_row["temperature_c"])

        fast = pairwise_slope(usable, 100)
        slow = pairwise_slope(usable, 210)
        if fast is None:
            fast = slow if slow is not None else 0.0
        if slow is None:
            slow = fast
        trend = max(-3.0, min(4.0, 0.65 * fast + 0.35 * slow))

        local_end = datetime.combine(local_day, time(20), tzinfo=local_tz).astimezone(UTC)
        past_energy = solar_energy_kwh_m2(latitude, longitude, base_time, current_time)
        remaining_energy = solar_energy_kwh_m2(latitude, longitude, current_time, local_end)
        observed_rise = max(0.0, current_temp - base_temp)
        response = observed_rise / past_energy if past_energy >= 0.25 else 1.5
        # These broad thermodynamic bounds prevent integer-rounded METAR noise
        # from implying an impossible boundary-layer heat response.
        response = max(0.25, min(3.5, response))
        energy_rise = response * remaining_energy

        current_solar = solar_position(latitude, longitude, current_time)["clearSkyShortwaveProxyWm2"]
        denominator = max(100.0, current_solar - SOLAR_MAINTENANCE_WM2)
        equivalent_hours = remaining_energy * 1000.0 / denominator
        momentum_rise = max(0.0, trend) * equivalent_hours

        flags: list[str] = []
        modifier = 1.0
        process_state = process_state or {}
        detected = process_state.get("detectedProcesses") or []
        if isinstance(detected, list):
            flags.extend(str(item) for item in detected)
        if any(flag in flags for flag in ("convective_cold_pool", "sea_breeze_intrusion")):
            modifier *= 0.35
        elif "upwind_cloud_or_rain_approach" in flags:
            modifier *= 0.65
        network = process_state.get("stationNetwork") or {}
        upwind_delta = finite(network.get("upwindTemperatureMinusPrimaryC"))
        advection = max(-1.0, min(1.0, 0.25 * upwind_delta)) if upwind_delta is not None else 0.0

        # Recent observed momentum is the primary estimate. Accumulated-energy
        # response is deliberately retained only as a warm-tail bound: mixed
        # layer growth makes morning heat response non-stationary, so directly
        # extrapolating it systematically overstates the afternoon maximum.
        if trend <= -0.35 and observed_max > current_temp:
            raw_remaining = 0.0
        else:
            raw_remaining = momentum_rise
        remaining_rise = max(0.0, modifier * raw_remaining + advection)
        prediction = max(observed_max, current_temp + remaining_rise)

        cloud = cloud_cover_from_sky_json(current.get("sky_conditions_json"))
        uncertainty = 0.55
        warnings: list[str] = []
        if len(usable) < 5:
            uncertainty += 0.5
            warnings.append("fewer than five same-day observations")
        if cutoff.astimezone(UTC) - current_time > timedelta(minutes=75):
            uncertainty += 0.4
            warnings.append("latest METAR is older than 75 minutes")
        if fast is not None and slow is not None and abs(fast - slow) > 1.25:
            uncertainty += 0.45
            warnings.append("short and medium temperature trends disagree")
        if cloud is None:
            uncertainty += 0.2
            warnings.append("current METAR cloud layer is unavailable")
        if not process_state:
            uncertainty += 0.25
            warnings.append("radar/satellite/upwind process state is unavailable")
        elif "no_high_confidence_regime_change" in flags:
            warnings.append("remote-sensing state has no diagnosed regime change")

        # Immediate capping is always physically possible; the realized daily
        # maximum can therefore finish at the irreversible observed floor even
        # while the primary path still contains additional heating.
        cool = observed_max
        # Warm tails are asymmetric while heating remains active: unexpected
        # clearing or a deeper mixed layer can release the energy-balance path.
        energy_tail = max(0.0, modifier * energy_rise + advection)
        warm = max(
            prediction + uncertainty * (1.15 if remaining_energy > 0.25 else 1.0),
            current_temp + energy_tail,
            observed_max,
        )
        path_status = {
            "capping": "plausible",
            "primary": "reference_path",
            "warm_tail": "plausible",
        }
        sounding_payload = asdict(sounding) if sounding else None
        if sounding:
            observed_sounding = parse_utc(sounding.observation_time_utc)
            sounding_age_hours = (
                (cutoff.astimezone(UTC) - observed_sounding).total_seconds() / 3600.0
                if observed_sounding else None
            )
            sounding_payload["age_at_cutoff_hours"] = (
                round(sounding_age_hours, 2) if sounding_age_hours is not None else None
            )
            if sounding.vertical_regime in {
                "morning_inversion_reservoir", "deep_dry_mixing_support",
            } and trend >= 0.25 and not any(
                flag in flags for flag in (
                    "convective_cold_pool", "sea_breeze_intrusion", "upwind_cloud_or_rain_approach",
                )
            ):
                path_status["warm_tail"] = "supported_competitor"
            if sounding.vertical_regime == "moist_low_level_capping_risk":
                # A moist low-level profile makes the accumulated clear-sky
                # energy response an invalid warm-tail extrapolation because a
                # larger fraction goes into latent rather than sensible heat.
                warm = min(warm, prediction + uncertainty * 1.15)
                path_status["capping"] = "supported_competitor"
            if any(
                flag in flags for flag in ("convective_cold_pool", "sea_breeze_intrusion")
            ):
                path_status["capping"] = "supported_competitor"
            if sounding.distance_km > 100:
                warnings.append("upper-air station is more than 100 km from settlement station")
            if sounding_age_hours is not None and sounding_age_hours > 10:
                warnings.append("00Z sounding is older than 10 hours at this cutoff")
        else:
            warnings.append("same-day observed upper-air sounding is unavailable")
        buckets = list(range(round_half_up(cool), round_half_up(warm) + 1))
        confidence = "moderate" if len(buckets) <= 2 else "low"

        return PhysicsPrediction(
            prediction_c=round(prediction, 2), predicted_bucket_c=round_half_up(prediction),
            cool_scenario_c=round(cool, 2), warm_scenario_c=round(warm, 2),
            plausible_buckets_c=buckets, observed_max_c=round(observed_max, 2),
            current_temperature_c=round(current_temp, 2), remaining_rise_c=round(remaining_rise, 2),
            momentum_rise_c=round(momentum_rise, 2), energy_balance_rise_c=round(energy_rise, 2),
            recent_trend_c_per_hour=round(trend, 3), response_c_per_kwh_m2=round(response, 3),
            past_usable_solar_kwh_m2=round(past_energy, 3),
            remaining_usable_solar_kwh_m2=round(remaining_energy, 3),
            process_modifier=round(modifier, 3), process_flags=flags,
            capping_path_c=round(cool, 2), primary_path_c=round(prediction, 2),
            warm_tail_path_c=round(warm, 2), path_status=path_status,
            sounding=sounding_payload,
            confidence=confidence, data_warnings=warnings,
        )


class Dataset:
    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row

    def close(self) -> None:
        self.db.close()

    def process_state(self, station_id: str, target_date: str, cutoff_utc: str) -> dict[str, Any] | None:
        row = self.db.execute(
            """SELECT state_json FROM weather_process_states
               WHERE station_id=? AND target_date=? AND slot_utc<=? AND status='ok'
               ORDER BY slot_utc DESC LIMIT 1""",
            (station_id, target_date, cutoff_utc),
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["state_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def observations(self, station_id: str, target_date: str, cutoff_utc: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(
            """SELECT observation_time_utc,temperature_c,dewpoint_c,relative_humidity,
                      wind_direction_deg,wind_speed,pressure_hpa,sky_conditions_json,weather_code
               FROM weather_observations
               WHERE station_id=? AND sample_local_date=? AND source='metar' AND status='ok'
                 AND observation_time_utc IS NOT NULL AND observation_time_utc<=?
               GROUP BY observation_time_utc ORDER BY observation_time_utc""",
            (station_id, target_date, cutoff_utc),
        ).fetchall()]

    def rows(self, target_date: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(
            """SELECT e.event_id,e.target_date,e.city,e.station_id,e.winning_range,
                      s.latitude,s.longitude,s.timezone
               FROM events e JOIN stations s ON s.station_id=e.station_id
               WHERE e.city IN ({}) AND e.target_date<=?
               ORDER BY e.target_date,e.city""".format(",".join("?" for _ in TRADE_CITIES)),
            (*TRADE_CITIES, target_date),
        ).fetchall()]


def metric(actual: list[float], predicted: list[float]) -> dict[str, Any]:
    errors = [forecast - outcome for forecast, outcome in zip(predicted, actual)]
    return {
        "events": len(errors),
        "mae_c": round(sum(abs(error) for error in errors) / len(errors), 4) if errors else None,
        "bias_c": round(sum(errors) / len(errors), 4) if errors else None,
        "exact_bucket_accuracy": round(sum(
            round_half_up(forecast) == round_half_up(outcome)
            for forecast, outcome in zip(predicted, actual)
        ) / len(errors), 4) if errors else None,
        "within_one_c": round(sum(abs(error) <= 1.0 for error in errors) / len(errors), 4) if errors else None,
    }


def build_report(db_path: Path, target_date: str) -> dict[str, Any]:
    dataset = Dataset(db_path)
    model = PhysicsHighModel()
    sounding_client = SoundingClient(SOUNDING_CACHE_DIR)
    rows = dataset.rows(target_date)
    sounding_profiles: dict[str, SoundingFeatures | None] = {}

    # Soundings are shared by all four cutoffs. Fetch each city/date once and
    # retain a local cache so research reruns do not repeatedly hit the source.
    eligible = []
    for event in rows:
        tz = ZoneInfo(event["timezone"])
        cutoff = datetime.combine(date.fromisoformat(event["target_date"]), time(10), tzinfo=tz)
        if len(dataset.observations(
            event["station_id"], event["target_date"], cutoff.astimezone(UTC).isoformat(timespec="seconds")
        )) >= 2:
            eligible.append(event)

    def load_sounding(event: dict[str, Any]) -> tuple[str, SoundingFeatures | None]:
        return event["event_id"], sounding_client.fetch(
            event["city"], event["target_date"],
            float(event["latitude"]), float(event["longitude"]),
        )

    with ThreadPoolExecutor(max_workers=7) as executor:
        futures = [executor.submit(load_sounding, event) for event in eligible]
        for future in as_completed(futures):
            event_id, profile = future.result()
            sounding_profiles[event_id] = profile
    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "target_date": target_date,
        "method": "observed maximum plus physics-constrained remaining sensible heating and observed 00Z upper-air profile",
        "uses_forecast_models": False,
        "uses_market_prices": False,
        "research_only": True,
        "cutoffs": [],
    }
    try:
        for hour in CUTOFF_HOURS:
            predictions: list[dict[str, Any]] = []
            for event in rows:
                tz = ZoneInfo(event["timezone"])
                cutoff = datetime.combine(date.fromisoformat(event["target_date"]), time(hour), tzinfo=tz)
                cutoff_utc = cutoff.astimezone(UTC).isoformat(timespec="seconds")
                observations = dataset.observations(event["station_id"], event["target_date"], cutoff_utc)
                if len(observations) < 2:
                    continue
                process = dataset.process_state(event["station_id"], event["target_date"], cutoff_utc)
                try:
                    result = model.predict(
                        observations, float(event["latitude"]), float(event["longitude"]),
                        tz, cutoff, process, sounding_profiles.get(event["event_id"]),
                    )
                except ValueError:
                    continue
                predictions.append({
                    "event_id": event["event_id"], "target_date": event["target_date"],
                    "city": event["city"], "actual_c": exact_temperature(event["winning_range"]),
                    **asdict(result),
                })
            evaluated = [row for row in predictions if row["actual_c"] is not None]
            evaluation = metric(
                [float(row["actual_c"]) for row in evaluated],
                [float(row["prediction_c"]) for row in evaluated],
            )
            evaluation["scenario_coverage"] = round(sum(
                float(row["cool_scenario_c"]) <= float(row["actual_c"]) <= float(row["warm_scenario_c"])
                for row in evaluated
            ) / len(evaluated), 4) if evaluated else None
            nearest_paths = {"capping": 0, "primary": 0, "warm_tail": 0}
            for row in evaluated:
                actual = float(row["actual_c"])
                paths = {
                    "capping": float(row["capping_path_c"]),
                    "primary": float(row["primary_path_c"]),
                    "warm_tail": float(row["warm_tail_path_c"]),
                }
                nearest = min(paths, key=lambda name: abs(paths[name] - actual))
                nearest_paths[nearest] += 1
            evaluation["nearest_path_counts"] = nearest_paths
            report["cutoffs"].append({
                "cutoff_hour": hour,
                "status": "not_point_predictive" if hour <= 12 else "experimental_small_sample",
                "evaluation": evaluation,
                "current_predictions": [row for row in predictions if row["target_date"] == target_date],
                "historical_predictions": evaluated,
            })
    finally:
        dataset.close()
    return report


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 实况物理约束最高温模型", "",
        f"生成时间：{report['generated_at_utc']}", "",
        "> 研究/影子模式。核心不读取 Meteoblue、ECMWF 或盘口价格。", "",
        "## 第一性原理", "",
        "最终最高温 = 已经观测到的最高温 + 尚未释放的有效地表加热。", "",
        "模型用最近实测升温动量推算主路径；当天累计升温相对于太阳可用能量的响应只定义暖尾，",
        "再由云雨、海风、冷池和上风向状态修正剩余升温。",
        "已观测最高温是不可逆下限；降温后不会凭模型预报把最高温重新抬高。", "",
        "## 历史回放", "",
        "| 当地截点 | 状态 | 可评估事件 | MAE | 整数档命中 | ±1°C | 情景覆盖 | 偏差 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["cutoffs"]:
        result = item["evaluation"]
        lines.append(
            f"| {item['cutoff_hour']:02d}:00 | {item['status']} | {result['events']} | {result['mae_c']} | "
            f"{result['exact_bucket_accuracy']} | {result['within_one_c']} | "
            f"{result['scenario_coverage']} | {result['bias_c']} |"
        )
    lines.extend(["", "### 最接近最终结果的路径", "",
                  "| 当地截点 | 封顶路径 | 主路径 | 暖尾路径 |",
                  "|---|---:|---:|---:|"])
    for item in report["cutoffs"]:
        counts = item["evaluation"]["nearest_path_counts"]
        lines.append(
            f"| {item['cutoff_hour']:02d}:00 | {counts['capping']} | "
            f"{counts['primary']} | {counts['warm_tail']} |"
        )
    chosen = next((item for item in report["cutoffs"] if item["cutoff_hour"] == 14), None)
    lines.extend(["", "## 当前 14:00 预测", ""])
    current = (chosen or {}).get("current_predictions") or []
    if current:
        lines.extend([
            "| 城市 | 封顶路径 | 主路径 | 暖尾路径 | 主档 | 可行档 | 探空垂直状态 | 置信度 |",
            "|---|---:|---:|---:|---:|---|---|---|",
        ])
        for row in current:
            plausible = ", ".join(f"{value}°C" for value in row["plausible_buckets_c"])
            sounding = row.get("sounding") or {}
            lines.append(
                f"| {row['city']} | {row['capping_path_c']:.2f}°C | {row['primary_path_c']:.2f}°C | "
                f"{row['warm_tail_path_c']:.2f}°C | {row['predicted_bucket_c']}°C | {plausible} | "
                f"{sounding.get('vertical_regime', 'unavailable')} | {row['confidence']} |"
            )
    else:
        lines.append("当前目标日没有足够的 14:00 观测。")
    lines.extend([
        "", "## 当前限制", "",
        "- 只有约 4 个较完整观测日；历史回放用于发现结构性问题，不足以证明泛化能力。",
        "- METAR 为整数温度且站点频率不同，短时升温速度存在量化噪声。",
        "- Himawari 云量和 RainViewer 回波是过程代理，不是校准后的站点辐射/降水观测。",
        "- 当前区间是物理情景区间，不是校准概率区间；至少积累 30 个独立事件日后再做概率校准。",
        "- 10:00 前后仅靠地面观测无法识别边界层深度和逆温储热，早段点预测应视为低置信。",
        "- 已加入当天00Z实测探空，但单次晨间剖面不能直接观测下午混合层演变。",
        "- 当前仍缺盘中混合层高度与站点实测太阳辐射；卫星/雷达只能提供过程代理。",
        "- 模型必须先影子运行，不能仅凭现有回放接入 Paper 入场。",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--target-date", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
    args = parser.parse_args()
    report = build_report(args.db, args.target_date)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / "weather_physics_high_model_report.json"
    markdown_path = OUTPUT_DIR / "weather_physics_high_model_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({
        "json": str(json_path), "markdown": str(markdown_path),
        "evaluations": [item["evaluation"] for item in report["cutoffs"]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
