#!/usr/bin/env python3
"""Center-bucket binary event: my model's probability vs the market's.

Question (user, 2026-08-26): 对中心桶,我的模型是否比市场更准?

Spec frozen before first run. Data source is the ALREADY-FROZEN point-in-time
detail of weather_ensemble_bucket_research.json (2026-08-25 run): entry slot
11:00 Beijing, ensemble row slot_utc <= entry, books read only at the entry
slot, winner from events.winning_range. No new data is pulled, so no new
lookahead surface exists.

Event (single definition for every predictor)
    Y = 1 if market_center == winning_temp, else 0.
    market_center is the source study's frozen rule: listed exact bucket with
    the highest YES mid among spread-eligible legs. This is the bucket MIXE10
    actually trades, so it is the event whose pricing matters.

Predictors (probability claimed for that same center bucket)
    MKT_RENORM : MKT_p_at_center   - YES mids renormalized over ALL listed
                 outcomes; the market's proper probability forecast. PRIMARY
                 market benchmark.
    MKT_RAWMID : market_center_mid - raw unnormalized mid; reference only
                 (carries cross-bucket vig).
    MEMB, NRM  : raw ECMWF-ens member fractions / fitted Normal.
    CAL_NRM, CAL_MEMB, CAL_MEMBK : per-station rolling-calibrated forms of the
                 source study (freeze 2; CAL_MEMBK declared primary there).
Subsets: raw predictors scored on all scored days; CAL_* only on has_cal days;
every paired comparison uses the SAME matched subset.

Metrics
    1. Binary quality for Y: log-loss -[y ln p + (1-y) ln(1-p)] and Brier
       (p-y)^2, shared formula across predictors, floor 1e-6; mean claim vs
       realized frequency.
    2. Paired daily delta model - MKT_RENORM on matched subsets; date-block
       bootstrap (wdrb.date_block_bootstrap) of the daily mean delta -> p05.
    3. Reliability quintiles per predictor.
    4. Edge test (the trading-relevant form of the question):
       e = P_model(center) - P_MKT(center); buckets e<0, [0,0.03),
       [0.03,0.08), >=0.08 -> n, realized hit rate, mean MKT price, mean
       residual Y - MKT price; date-block bootstrap of the daily mean residual
       inside e >= 0; plus Pearson r between e and residual.
    5. Gate interaction: edge table restricted to gate_ok days (frozen alpha
       gate), i.e. does the model add selectivity BEYOND the market-structure
       gate?

Declared hypothesis before running: from the source study's multiclass scores,
MKT is expected to beat every model form on the binary event as well; the open
question is whether e carries incremental information (residual slope > 0).

Known boundary: who better NAMES the winner (argmax contest over full vectors)
cannot be recomputed from the stored detail (only p_at_center was persisted);
listed as follow-up if needed.

Output: research/output/weather_center_bucket_model_vs_market.{json,md}
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import weather_dynamic_rebalance_backtest as wdrb  # noqa: E402

SOURCE_JSON = Path("research/output/weather_ensemble_bucket_research.json")
OUT_JSON = Path("research/output/weather_center_bucket_model_vs_market.json")
OUT_MD = Path("research/output/weather_center_bucket_model_vs_market.md")
PROB_FLOOR = 1e-6
RAW_PREDICTORS = ("MEMB", "NRM")
CAL_PREDICTORS = ("CAL_NRM", "CAL_MEMB", "CAL_MEMBK")
EDGE_BUCKETS = (
    ("e_lt_0", lambda e: e < 0.0),
    ("e_0_0.03", lambda e: 0.0 <= e < 0.03),
    ("e_0.03_0.08", lambda e: 0.03 <= e < 0.08),
    ("e_ge_0.08", lambda e: e >= 0.08),
)


def load_records() -> list[dict]:
    data = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))
    records = []
    for row in data["detail"]:
        if row.get("winning_temp") is None or row.get("market_center") is None:
            continue
        if row.get("MKT_p_at_center") is None:
            continue
        records.append({
            **row,
            "y": 1.0 if row["market_center"] == row["winning_temp"] else 0.0,
        })
    return records


def binary_scores(rows: list[dict], field: str) -> dict:
    ll_values, brier_values = [], []
    for row in rows:
        p = min(max(row[field], PROB_FLOOR), 1.0 - PROB_FLOOR)
        y = row["y"]
        ll_values.append(-(y * math.log(p) + (1.0 - y) * math.log(1.0 - p)))
        brier_values.append((p - y) ** 2)
    return {
        "n": len(rows),
        "logloss_mean": round(statistics.fmean(ll_values), 5),
        "brier_mean": round(statistics.fmean(brier_values), 5),
        "claim_mean": round(statistics.fmean(row[field] for row in rows), 5),
        "realized_rate": round(statistics.fmean(row["y"] for row in rows), 5),
    }


def paired_delta_vs_market(rows: list[dict], field: str,
                           market_field: str = "MKT_p_at_center") -> dict:
    """Per-day mean (model LL - market LL, model Brier - market Brier)."""
    ll_delta: dict[str, list[float]] = {}
    brier_delta: dict[str, list[float]] = {}
    for row in rows:
        pm = min(max(row[field], PROB_FLOOR), 1.0 - PROB_FLOOR)
        pk = min(max(row[market_field], PROB_FLOOR), 1.0 - PROB_FLOOR)
        y = row["y"]
        ll_m = -(y * math.log(pm) + (1.0 - y) * math.log(1.0 - pm))
        ll_k = -(y * math.log(pk) + (1.0 - y) * math.log(1.0 - pk))
        ll_delta.setdefault(row["target_date"], []).append(ll_m - ll_k)
        brier_delta.setdefault(row["target_date"], []).append((pm - y) ** 2 - (pk - y) ** 2)
    return {
        "logloss": wdrb.date_block_bootstrap(
            {d: statistics.fmean(v) for d, v in ll_delta.items()}),
        "brier": wdrb.date_block_bootstrap(
            {d: statistics.fmean(v) for d, v in brier_delta.items()}),
    }


def reliability(rows: list[dict], field: str) -> list[dict]:
    ranked = sorted(rows, key=lambda row: row[field])
    q = max(1, len(ranked) // 5)
    table = []
    for i in range(0, len(ranked), q):
        chunk = ranked[i:i + q]
        if chunk:
            table.append({
                "claim_mean": round(statistics.fmean(r[field] for r in chunk), 4),
                "hit_rate": round(statistics.fmean(r["y"] for r in chunk), 4),
                "n": len(chunk),
            })
    return table


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or statistics.pstdev(xs) <= 1e-12 or statistics.pstdev(ys) <= 1e-12:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = statistics.fmean([(x - mx) * (y - my) for x, y in zip(xs, ys)])
    return round(cov / (statistics.pstdev(xs) * statistics.pstdev(ys)), 4)


def edge_table(rows: list[dict], model_field: str) -> tuple[list[dict], dict]:
    enriched = [
        {**row, "edge": row[model_field] - row["MKT_p_at_center"],
         "residual": row["y"] - row["MKT_p_at_center"]}
        for row in rows
    ]
    table = []
    for name, predicate in EDGE_BUCKETS:
        chunk = [row for row in enriched if predicate(row["edge"])]
        if not chunk:
            table.append({"bucket": name, "n": 0})
            continue
        daily_residual = {}
        for row in chunk:
            daily_residual.setdefault(row["target_date"], []).append(row["residual"])
        table.append({
            "bucket": name,
            "n": len(chunk),
            "edge_mean": round(statistics.fmean(r["edge"] for r in chunk), 4),
            "model_claim": round(statistics.fmean(r[model_field] for r in chunk), 4),
            "mkt_price": round(statistics.fmean(r["MKT_p_at_center"] for r in chunk), 4),
            "realized_hit": round(statistics.fmean(r["y"] for r in chunk), 4),
            "residual_mean": round(statistics.fmean(r["residual"] for r in chunk), 4),
            "residual_daily_lb05": wdrb.date_block_bootstrap({
                d: statistics.fmean(v) for d, v in daily_residual.items()
            })["mean_lb05"],
        })
    positive = [row for row in enriched if row["edge"] >= 0.0]
    daily_positive = {}
    for row in positive:
        daily_positive.setdefault(row["target_date"], []).append(row["residual"])
    summary = {
        "n_edge_ge_0": len(positive),
        "residual_daily_lb05_e_ge_0": wdrb.date_block_bootstrap({
            d: statistics.fmean(v) for d, v in daily_positive.items()
        }),
        "corr_edge_residual": pearson(
            [row["edge"] for row in enriched], [row["residual"] for row in enriched]
        ),
    }
    return table, summary


def main() -> None:
    rows_all = load_records()
    rows_cal = [row for row in rows_all if row["has_cal"]]
    gate_rows = [row for row in rows_all if row.get("gate_ok")]
    gate_cal_rows = [row for row in rows_cal if row.get("gate_ok")]

    quality: dict[str, dict] = {"MKT_RENORM": binary_scores(rows_all, "MKT_p_at_center")}
    for pred in RAW_PREDICTORS:
        quality[pred] = binary_scores(rows_all, f"{pred}_p_at_center")
    quality["MKT_RAWMID"] = binary_scores(rows_all, "market_center_mid")
    for pred in CAL_PREDICTORS:
        quality[f"{pred}|cal_subset"] = binary_scores(rows_cal, f"{pred}_p_at_center")
    quality["MKT_RENORM|cal_subset"] = binary_scores(rows_cal, "MKT_p_at_center")

    paired = {"MKT_RAWMID-vs-MKT_RENORM": paired_delta_vs_market(rows_all, "market_center_mid")}
    for pred in (*RAW_PREDICTORS, *CAL_PREDICTORS):
        subset = rows_cal if pred in CAL_PREDICTORS else rows_all
        paired[f"{pred}-vs-MKT_RENORM"] = paired_delta_vs_market(subset, f"{pred}_p_at_center")

    edge_main: dict[str, object] = {}
    for pred in (*RAW_PREDICTORS, *CAL_PREDICTORS):
        subset = rows_cal if pred in CAL_PREDICTORS else rows_all
        table, summary = edge_table(subset, f"{pred}_p_at_center")
        edge_main[pred] = {"table": table, "summary": summary}
    gate_edge: dict[str, object] = {}
    for pred in (*RAW_PREDICTORS, *CAL_PREDICTORS):
        subset = gate_cal_rows if pred in CAL_PREDICTORS else gate_rows
        if not subset:
            continue
        table, summary = edge_table(subset, f"{pred}_p_at_center")
        gate_edge[pred] = {"table": table, "summary": summary}

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_json": str(SOURCE_JSON),
        "spec": "see module docstring; frozen before first run",
        "n_scored_days_city_days": len(rows_all),
        "n_cal_covered": len(rows_cal),
        "n_gate_days": len(gate_rows),
        "n_gate_and_cal": len(gate_cal_rows),
        "independent_dates": sorted({row["target_date"] for row in rows_all}),
        "quality_binary_center_event": quality,
        "paired_logloss_brier_delta": paired,
        "reliability_quintiles": {
            "MKT_RENORM": reliability(rows_all, "MKT_p_at_center"),
            **{pred: reliability(rows_all, f"{pred}_p_at_center") for pred in RAW_PREDICTORS},
            **{pred: reliability(rows_cal, f"{pred}_p_at_center") for pred in CAL_PREDICTORS},
        },
        "edge_vs_market": edge_main,
        "edge_vs_market_on_gate_days": gate_edge,
        "detail": [
            {
                "city": row["city"], "target_date": row["target_date"],
                "market_center": row["market_center"], "winning_temp": row["winning_temp"],
                "y": row["y"], "mkt_price": row["MKT_p_at_center"],
                **{pred: row.get(f"{pred}_p_at_center") for pred in (*RAW_PREDICTORS, *CAL_PREDICTORS)},
                "gate_ok": row.get("gate_ok"), "has_cal": row["has_cal"],
            }
            for row in rows_all
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def fmt(value, digits=4):
        return "-" if value is None else f"{float(value):.{digits}f}"

    lines = [
        "# 中心桶二元事件:我的模型 vs 市场", "",
        f"> 生成:{report['generated_at_utc']};样本 {len(rows_all)} 城-日"
        f"(校准覆盖 {len(rows_cal)},门槛日 {len(gate_rows)},门槛∩校准 {len(gate_cal_rows)});"
        f"独立日期 {len(report['independent_dates'])} 个。事件定义:市场中心桶获胜;"
        "全部概率取自 2026-08-25 冻结的集合研究的点内时序明细。", "",
        "## 1. 概率质量(二元事件:中心桶获胜;log-loss/Brier 越低越准)", "",
        "| 预测器 | 子集 | 天数 | log-loss ↓ | Brier ↓ | 声称P | 实际频率 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    subset_label = {
        "MKT_RENORM": "全部", "MEMB": "全部", "NRM": "全部", "MKT_RAWMID": "全部(参考)",
        "MKT_RENORM|cal_subset": "校准覆盖", "CAL_NRM|cal_subset": "校准覆盖",
        "CAL_MEMB|cal_subset": "校准覆盖", "CAL_MEMBK|cal_subset": "校准覆盖",
    }
    for name, item in quality.items():
        lines.append(
            f"| {name} | {subset_label.get(name, '-')} | {item['n']} | "
            f"{item['logloss_mean']} | {item['brier_mean']} | "
            f"{item['claim_mean']} | {item['realized_rate']} |"
        )
    lines += ["", "## 2. 配对逐日差(model − 市场,同子集;负值=模型更差)", "",
              "| 对比 | 平均Δlog-loss(5%下界) | 平均ΔBrier(5%下界) | 正/负日期(log-loss) |",
              "|---|---:|---:|---:|"]
    for name, stat in paired.items():
        lines.append(
            f"| {name} | {stat['logloss']['mean_diff']}({stat['logloss']['mean_lb05']}) | "
            f"{stat['brier']['mean_diff']}({stat['brier']['mean_lb05']}) | "
            f"{stat['logloss']['positive_dates']}/{stat['logloss']['negative_dates']} |"
        )
    lines += ["", "## 3. 可靠性五分位(声称P vs 实际命中率)", "",
              "| 预测器 | 声称P | 实际命中率 | 天数 |", "|---|---:|---:|---:|"]
    for pred, table in report["reliability_quintiles"].items():
        for r in table:
            lines.append(f"| {pred} | {r['claim_mean']} | {r['hit_rate']} | {r['n']} |")

    for section, source in (("## 4. 边际信息检验:e=P模型−P市场 分组(交易相关形式)", edge_main),
                            ("## 5. 同表,仅门槛日(alpha 结构门槛之上模型是否增量)", gate_edge)):
        lines += ["", section, ""]
        for pred, payload in source.items():
            lines.append(f"### {pred}")
            lines += ["| e分组 | n | e均值 | 模型P | 市场价 | 实际命中 | 残差均值(Y−价) | 日块5%下界 |",
                      "|---|---:|---:|---:|---:|---:|---:|---:|"]
            for r in payload["table"]:
                if r["n"] == 0:
                    lines.append(f"| {r['bucket']} | 0 | - | - | - | - | - | - |")
                    continue
                lines.append(
                    f"| {r['bucket']} | {r['n']} | {r['edge_mean']} | {r['model_claim']} | "
                    f"{r['mkt_price']} | {r['realized_hit']} | {r['residual_mean']} | "
                    f"{fmt(r['residual_daily_lb05'])} |"
                )
            summary = payload["summary"]
            boot = summary["residual_daily_lb05_e_ge_0"]
            lines.append(
                f"- e≥0 组:n={summary['n_edge_ge_0']},残差日均 {boot['mean_diff']},"
                f"日期块5%下界 {boot['mean_lb05']},正/负日期 {boot['positive_dates']}/{boot['negative_dates']}。"
            )
            lines.append(f"- corr(e, Y−市场价) = {fmt(summary['corr_edge_residual'])}。")
            lines.append("")
    lines += ["## 边界", "",
              "- 全部输入来自已冻结的集合研究明细;本脚本未触碰任何新数据,无新增未来函数面。",
              "- argmax“谁更会点名胜出桶”需要完整概率向量,明细未持久化,留作后续。",
              "- 同日城市相关,日期块自助法只部分缓解;窗口 2026-07-29 起,样本有限。",
              "- 本结果是历史研究,不构成实盘认证。"]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(OUT_JSON), "md": str(OUT_MD)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
