#!/usr/bin/env python3
"""Do my RECORDED observational/process data contain information the market
has not priced?

Question (user, 2026-08-26): 我记录的那些天气数据有优于市场的吗?

Scope frozen before first run. The bakeoff (weather_source_vs_market_bakeoff)
showed no source beats the market on POINT accuracy. A source can still carry
INCREMENTAL value: correlate with the settlement residual r = Y - MKT_ET.
This audit tests every recorded observational/process stream for exactly that,
on the same universe and ticks as the bakeoff.

Data
    weather_process_states.state_json (status='ok'), latest row with
    slot_utc <= tick within 90 min, joined to MKT_ET recomputed identically
    to the bakeoff (same renormalization/folding/staleness rules).

Features (all point-in-time, null -> excluded per feature)
    F1 trend_c_per_h   primaryStationTrend.temperatureTrendCPerHour
    F2 cur_minus_mket  primaryStationTrend.currentTemperatureC - MKT_ET
    F3 upwind_delta    stationNetwork.upwindTemperatureMinusPrimaryC
    F4 echo25          rainviewer echoCoverage25Km
    F5 upwind_echo150  rainviewer upwindEchoCoverage150Km
    F6 nict_cloud50    nict cloudProxyCoverage50Km
    F7 jaxa_swr_ratio  solarHeating.jaxaToClearSkyRatio
    F8 mblue_now_err   modelRealityComparison.meteoblue.observationMinusSameHourForecastC
    F9 cooling_flag    1 if detectedProcesses intersects the declared cooling set
                       {convective_cold_pool, sea_breeze, cold_pool,
                        upwind_cloud_or_rain_approach, rain_band}, else 0

Tests per feature x tick (multiple comparisons: 9 x 4 = 36; stated upfront -
a finding is believed only if its date-block 5% lower bound clears zero AND
the quintile table is monotone-ish AND the sign is stable across ticks)
    1. Pearson corr(feature, r); quintiles of feature -> mean residual.
    2. Trading form: leave-one-date-out univariate OLS r ~ a + b*f fitted on
       other dates, applied to the held-out date; paired daily delta
       |Y - (MKT_ET + pred)| - |Y - MKT_ET| < 0 means the recorded feature
       improves the market forecast; date-block bootstrap p05.

Integrity
    - MKT_ET identical implementation reused from the bakeoff module.
    - Features read only from rows with slot_utc <= tick.
    - Settlement only events.winning_range; per-feature exclusion counts kept.

Output: research/output/weather_recorded_data_incremental_audit.{json,md}
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import weather_dynamic_rebalance_backtest as wdrb  # noqa: E402
import weather_source_vs_market_bakeoff as wsb  # noqa: E402

DB = Path("data/weather_market_monitor.sqlite3")
OUT_JSON = Path("research/output/weather_recorded_data_incremental_audit.json")
OUT_MD = Path("research/output/weather_recorded_data_incremental_audit.md")
TICKS = wsb.TICKS
STALE_H = 1.5
COOLING_PROCESSES = {
    "convective_cold_pool", "sea_breeze", "cold_pool",
    "upwind_cloud_or_rain_approach", "rain_band",
}


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def extract_features(state_json: str, processes_json: str,
                     mk_et: float) -> dict[str, float | None]:
    try:
        state = json.loads(state_json)
    except (ValueError, TypeError):
        return {}
    trend = state.get("primaryStationTrend") or {}
    network = state.get("stationNetwork") or {}
    sensing = state.get("remoteSensing") or {}
    rainviewer = ((sensing.get("rainviewer") or {}).get("features")) or {}
    nict = ((sensing.get("nict_himawari_true_colour") or {}).get("features")) or {}
    solar = state.get("solarHeating") or {}
    mblue = ((state.get("modelRealityComparison") or {}).get("meteoblue")) or {}

    def num(value) -> float | None:
        return float(value) if isinstance(value, (int, float)) else None

    cur = num(trend.get("currentTemperatureC"))
    processes = set()
    try:
        processes = {str(p) for p in json.loads(processes_json or "[]")}
    except (ValueError, TypeError):
        pass
    return {
        "F1_trend_c_per_h": num(trend.get("temperatureTrendCPerHour")),
        "F2_cur_minus_mket": (cur - mk_et) if cur is not None else None,
        "F3_upwind_delta": num(network.get("upwindTemperatureMinusPrimaryC")),
        "F4_echo25": num(rainviewer.get("echoCoverage25Km")),
        "F5_upwind_echo150": num(rainviewer.get("upwindEchoCoverage150Km")),
        "F6_nict_cloud50": num(nict.get("cloudProxyCoverage50Km")),
        "F7_jaxa_swr_ratio": num(solar.get("jaxaToClearSkyRatio")),
        "F8_mblue_now_err": num(mblue.get("observationMinusSameHourForecastC")),
        "F9_cooling_flag": 1.0 if processes & COOLING_PROCESSES else 0.0,
    }


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 5 or statistics.pstdev(xs) <= 1e-12 or statistics.pstdev(ys) <= 1e-12:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = statistics.fmean([(x - mx) * (y - my) for x, y in zip(xs, ys)])
    return round(cov / (statistics.pstdev(xs) * statistics.pstdev(ys)), 4)


def block_boot_two_sided(daily: dict[str, float], seed: int = wdrb.BOOTSTRAP_SEED,
                         iters: int = wdrb.BOOTSTRAP_ITERS) -> dict[str, float]:
    """Daily-mean bootstrap returning BOTH tails: improvement is significant
    iff ub95 < 0 (delta<0 good); harm is significant iff lb05 > 0."""
    values = [daily[d] for d in sorted(daily)]
    rng = __import__("random").Random(seed)
    means = []
    for _ in range(iters):
        sample = [values[rng.randrange(len(values))] for _ in range(len(values))]
        means.append(statistics.fmean(sample))
    means.sort()
    return {
        "mean": round(statistics.fmean(values), 4),
        "lb05": round(means[int(0.05 * len(means))], 4),
        "ub95": round(means[min(len(means) - 1, int(0.95 * len(means)))], 4),
        "pos_dates": sum(1 for v in values if v > 0),
        "neg_dates": sum(1 for v in values if v < 0),
    }


def oos_slope_apply(rows: list[dict], field: str) -> list[dict]:
    """Leave-one-date-out univariate OLS on residual r = Y - MKT_ET."""
    scored = []
    dates = sorted({row["target_date"] for row in rows})
    for holdout in dates:
        train = [row for row in rows if row["target_date"] != holdout]
        xs = [row[field] for row in train]
        ys = [row["y"] - row["mkt_et"] for row in train]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx <= 1e-12:
            continue
        beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        alpha = my - beta * mx
        for row in rows:
            if row["target_date"] == holdout:
                pred = alpha + beta * row[field]
                scored.append({**row, "pred": pred})
    return scored


def main() -> None:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    universe = wsb.load_universe(db)

    records: list[dict] = []
    excluded: dict[str, int] = {}
    for event in universe:
        day0 = wdrb.day_start(event["target_date"])
        for tick_h in TICKS:
            tick = day0 + timedelta(hours=tick_h)
            mkt = wsb.mkt_state(db, event["event_id"], tick, wsb.STALE["MKT"])
            if mkt is None:
                excluded[f"mkt_no_book@{tick_h:g}"] = excluded.get(f"mkt_no_book@{tick_h:g}", 0) + 1
                continue
            prow = db.execute(
                """
                SELECT slot_utc, state_json, detected_processes_json
                FROM weather_process_states
                WHERE station_id=? AND target_date=? AND status='ok'
                  AND slot_utc<=? AND slot_utc>=?
                ORDER BY slot_utc DESC LIMIT 1
                """,
                (
                    event["station_id"], event["target_date"], tick.isoformat(),
                    (tick - timedelta(hours=STALE_H)).isoformat(),
                ),
            ).fetchone()
            if prow is None:
                excluded[f"no_process_state@{tick_h:g}"] = excluded.get(
                    f"no_process_state@{tick_h:g}", 0) + 1
                continue
            features = extract_features(
                prow["state_json"], prow["detected_processes_json"], mkt["et"]
            )
            records.append({
                "event_id": event["event_id"], "city": event["city"],
                "target_date": event["target_date"], "station_id": event["station_id"],
                "tick_h": tick_h, "y": float(event["winning_temp"]),
                "mkt_et": mkt["et"], **features,
            })
    db.close()

    fields = [
        "F1_trend_c_per_h", "F2_cur_minus_mket", "F3_upwind_delta", "F4_echo25",
        "F5_upwind_echo150", "F6_nict_cloud50", "F7_jaxa_swr_ratio",
        "F8_mblue_now_err", "F9_cooling_flag",
    ]

    results: dict[str, dict] = {}
    for tick_h in TICKS:
        subset = [r for r in records if r["tick_h"] == tick_h]
        base_mae = statistics.fmean(abs(r["y"] - r["mkt_et"]) for r in subset)
        per_feature: dict[str, dict] = {"_base": {
            "n_city_days": len(subset),
            "independent_dates": len({r["target_date"] for r in subset}),
            "mkt_mae": round(base_mae, 4),
        }}
        for field in fields:
            rows = [r for r in subset if r.get(field) is not None]
            if len(rows) < 40:
                per_feature[field] = {"n": len(rows), "skipped": "insufficient"}
                continue
            resid = [r["y"] - r["mkt_et"] for r in rows]
            ranked = sorted(rows, key=lambda r: r[field])
            q = max(1, len(ranked) // 5)
            quintiles = []
            for i in range(0, len(ranked), q):
                chunk = ranked[i:i + q]
                if chunk:
                    quintiles.append({
                        "f_mean": round(statistics.fmean(r[field] for r in chunk), 4),
                        "resid_mean": round(statistics.fmean(r["y"] - r["mkt_et"] for r in chunk), 4),
                        "n": len(chunk),
                    })
            scored = oos_slope_apply(rows, field)
            daily_delta: dict[str, list[float]] = {}
            for r in scored:
                delta = abs(r["y"] - (r["mkt_et"] + r["pred"])) - abs(r["y"] - r["mkt_et"])
                daily_delta.setdefault(r["target_date"], []).append(delta)
            boot = block_boot_two_sided(
                {d: statistics.fmean(v) for d, v in daily_delta.items()}
            )
            per_feature[field] = {
                "n_city_days": len(rows),
                "independent_dates": len(daily_delta),
                "corr_with_residual": pearson(
                    [r[field] for r in rows], resid),
                "quintiles": quintiles,
                "oos_mae_delta_mean": boot["mean"],
                "oos_mae_delta_lb05": boot["lb05"],
                "oos_mae_delta_ub95": boot["ub95"],
                "significant_improvement": boot["ub95"] < 0.0,
                "significant_harm": boot["lb05"] > 0.0,
                "pos_dates": boot["pos_dates"],
                "neg_dates": boot["neg_dates"],
            }
        results[f"t{tick_h:g}"] = per_feature

    report = {
        "generated_at_utc": datetime.now().astimezone().isoformat(),
        "spec": "see module docstring; frozen before first run",
        "records": len(records),
        "excluded": excluded,
        "results_by_tick": results,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 记录观测/过程数据相对市场的增量信息审计", "",
        f"> 生成:{report['generated_at_utc']};{len(records)} 条城-日×时刻;"
        f"排除 {json.dumps(excluded, ensure_ascii=False)}。"
        "残差 r=Y−MKT_ET;OOS 为留一日期的单变量斜率外推。"
        "36 组检验(9 特征×4 时刻),只有 5% 下界<0 且五分位单调且跨时刻同号的才值得信。", "",
        "| 时刻 | 特征 | n | corr(r) | Q1残差 | Q5残差 | OOS ΔMAE 均值[95%区间] | 判定 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for tick_label, feats in results.items():
        lines.append(f"| {tick_label} | _基准MKT_MAE_ | {feats['_base']['n_city_days']} | - | - | - | "
                     f"基准 {feats['_base']['mkt_mae']} | - |")
        for name, item in feats.items():
            if name == "_base" or item.get("skipped"):
                continue
            q = item["quintiles"]
            verdict = "改善✓" if item["significant_improvement"] else (
                "有害✗" if item["significant_harm"] else "无显著")
            lines.append(
                f"| {tick_label} | {name} | {item['n_city_days']} | "
                f"{item['corr_with_residual']} | {q[0]['resid_mean']} | {q[-1]['resid_mean']} | "
                f"{item['oos_mae_delta_mean']}[{item['oos_mae_delta_lb05']},{item['oos_mae_delta_ub95']}] | "
                f"{verdict} ({item['neg_dates']}负/{item['pos_dates']}正) |"
            )
    lines += ["", "## 边界", "",
              "- 单变量审计;显著特征仍可能是其他变量的代理,组合增量需另测。",
              "- 雷达/云量为代理指标(process 内自带 quality 警告),非校准物理量。",
              "- 同日多城相关,日期块自助法只部分缓解。",
              "- 本结果是历史研究,不构成实盘认证。"]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(OUT_JSON), "md": str(OUT_MD)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
