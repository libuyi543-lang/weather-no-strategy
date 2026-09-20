#!/usr/bin/env python3
"""Date-block cross-validated probability calibration for three-bucket ladders.

This script estimates outcome-category probabilities only from earlier target
dates, then computes ex-ante package EV using the executable ask-book VWAP and
the current weather taker fee. It is read-only and does not write orders.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scipy.stats import beta as beta_distribution

from weather_ladder_microstructure_research import (
    ALLOWED_CITIES,
    WEATHER_TAKER_FEE_RATE,
    base_eligible,
    bootstrap_dates,
    event_state,
    evaluate_weights,
    metrics,
    select_one_per_date,
)


UTC = timezone.utc
CATEGORIES = ("lower", "center", "upper", "outside")
STRUCTURES = {
    "5/15/5": (5.0, 15.0, 5.0),
    "5/20/5": (5.0, 20.0, 5.0),
}


def load_rows(database: Path) -> list[dict[str, Any]]:
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    events = db.execute(
        """
        SELECT e.event_id,e.city,e.target_date,e.winning_market_id,e.winning_range,
               COALESCE(s.timezone,'Asia/Shanghai') timezone
        FROM events e LEFT JOIN stations s ON s.station_id=e.station_id
        WHERE e.resolved_at_utc IS NOT NULL AND e.winning_range IS NOT NULL
        ORDER BY e.target_date,e.city
        """
    ).fetchall()
    rows = []
    for event in events:
        if event["city"] not in ALLOWED_CITIES:
            continue
        state = event_state(db, event, 660)
        if state is None:
            continue
        eligible, _ = base_eligible(state)
        if not eligible:
            continue
        for structure, weights in STRUCTURES.items():
            result = evaluate_weights(state, weights)
            if result is None or result["cost"] > 15.0 + 1e-9:
                continue
            category = "outside"
            for index, leg in enumerate(state["legs"]):
                if leg["market_id"] == str(event["winning_market_id"]):
                    category = CATEGORIES[index]
                    break
            center_midpoint = state["center_midpoint"]
            center_bin = "lt_0.40" if center_midpoint < 0.40 else (
                "0.40_0.55" if center_midpoint < 0.55 else "ge_0.55"
            )
            gap = state["center_lead"]
            gap_bin = "0.03_0.08" if gap < 0.08 else ("0.08_0.15" if gap < 0.15 else "ge_0.15")
            rows.append({
                "event_id": state["event_id"], "target_date": state["target_date"],
                "city": state["city"], "structure": structure, "category": category,
                "center_bin": center_bin, "gap_bin": gap_bin,
                "center_midpoint": center_midpoint, "center_lead": gap,
                "max_spread": max(leg["spread"] for leg in state["legs"]),
                **result,
            })
    db.close()
    return rows


def posterior(
    train: list[dict[str, Any]], group_field: str | None, group_value: str | None,
    prior_strength: float = 1.0,
) -> dict[str, dict[str, float]]:
    selected = [
        row for row in train
        if group_field is None or row[group_field] == group_value
    ]
    counts = Counter(row["category"] for row in selected)
    n = len(selected)
    result = {}
    for category in CATEGORIES:
        successes = counts[category]
        alpha = prior_strength + successes
        beta = prior_strength * (len(CATEGORIES) - 1) + n - successes
        result[category] = {
            "mean": alpha / (alpha + beta),
            "lower05": float(beta_distribution.ppf(0.05, alpha, beta)),
            "n": n, "successes": successes,
        }
    return result


def probability_model(
    train: list[dict[str, Any]], row: dict[str, Any], model: str,
) -> dict[str, dict[str, float]]:
    if model == "market_anchor":
        leg_probabilities = [max(0.0, float(leg["midpoint"])) for leg in row["legs"]]
        outside = max(0.0, 1.0 - sum(leg_probabilities))
        values = leg_probabilities + [outside]
        total = sum(values)
        if total <= 0:
            values = [0.25] * len(CATEGORIES)
        else:
            values = [value / total for value in values]
        return {
            category: {"mean": value, "lower05": 0.0, "n": 0, "successes": 0}
            for category, value in zip(CATEGORIES, values)
        }
    if model == "global":
        return posterior(train, None, None)
    if model == "center_bin":
        return posterior(train, "center_bin", row["center_bin"])
    if model == "gap_bin":
        return posterior(train, "gap_bin", row["gap_bin"])
    raise ValueError(model)


def annotate(row: dict[str, Any], probabilities: dict[str, dict[str, float]]) -> dict[str, Any]:
    payout_mean = sum(row["legs"][index]["shares"] * probabilities[category]["mean"] for index, category in enumerate(CATEGORIES[:3]))
    payout_lower = sum(row["legs"][index]["shares"] * probabilities[category]["lower05"] for index, category in enumerate(CATEGORIES[:3]))
    return {
        **row,
        "predicted_payout": payout_mean,
        "predicted_ev": payout_mean - row["cost"],
        "component_lower_ev": payout_lower - row["cost"],
        "probabilities": probabilities,
    }


def choose(rows: list[dict[str, Any]], model: str, require_lower_ev: bool) -> list[dict[str, Any]]:
    by_date = defaultdict(list)
    for row in rows:
        if row["predicted_ev"] > 0 and (not require_lower_ev or row["component_lower_ev"] > 0):
            by_date[row["target_date"]].append(row)
    selected = []
    for day, candidates in by_date.items():
        # Max predicted EV is an explicit utility rule; max spread is a deterministic tie-break.
        selected.append(min(
            candidates,
            key=lambda row: (-row["predicted_ev"], row["max_spread"], row["city"], row["event_id"]),
        ))
    return sorted(selected, key=lambda row: row["target_date"])


def brier_log(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    if not rows:
        return {"brier": None, "log_score": None}
    brier_values = []
    log_values = []
    for row in rows:
        probs = row["probabilities"]
        for category in CATEGORIES:
            observed = 1.0 if row["category"] == category else 0.0
            predicted = min(max(probs[category]["mean"], 1e-9), 1 - 1e-9)
            brier_values.append((predicted - observed) ** 2)
            log_values.append(math.log(predicted) if observed else math.log(1 - predicted))
    return {
        "brier": statistics.fmean(brier_values),
        "log_score": statistics.fmean(log_values),
    }


def row_scores(row: dict[str, Any]) -> tuple[float, float]:
    brier_values = []
    log_values = []
    for category in CATEGORIES:
        observed = 1.0 if row["category"] == category else 0.0
        predicted = min(max(row["probabilities"][category]["mean"], 1e-9), 1 - 1e-9)
        brier_values.append((predicted - observed) ** 2)
        log_values.append(math.log(predicted) if observed else math.log(1 - predicted))
    return statistics.fmean(brier_values), statistics.fmean(log_values)


def run(database: Path) -> dict[str, Any]:
    raw = load_rows(database)
    dates = sorted({row["target_date"] for row in raw})
    output = {}
    selected_audits = {}
    calibration = {}
    cv_by_model_structure = {}
    for model in ("market_anchor", "global", "center_bin", "gap_bin"):
        for structure in STRUCTURES:
            test_rows = [row for row in raw if row["structure"] == structure]
            cv_rows = []
            for day in dates:
                train = [row for row in test_rows if row["target_date"] != day]
                for row in test_rows:
                    if row["target_date"] == day:
                        cv_rows.append(annotate(row, probability_model(train, row, model)))
            calibration[f"{model}|{structure}"] = {
                "events": len(cv_rows), "independent_dates": len(dates),
                **brier_log(cv_rows),
            }
            cv_by_model_structure[(model, structure)] = cv_rows
            for lower_filter in (False, True):
                name = f"{model}|{structure}|{'lower_ev_positive' if lower_filter else 'ev_positive'}"
                selected = choose(cv_rows, model, lower_filter)
                output[name] = metrics(selected)
                selected_audits[name] = [
                    {
                        "target_date": row["target_date"], "city": row["city"],
                        "predicted_ev": row["predicted_ev"],
                        "component_lower_ev": row["component_lower_ev"],
                        "actual_pnl": row["pnl"], "cost": row["cost"],
                        "category": row["category"], "center_bin": row["center_bin"],
                    }
                    for row in selected
                ]
    market_rows = cv_by_model_structure[("market_anchor", "5/15/5")]
    global_rows = cv_by_model_structure[("global", "5/15/5")]
    global_by_event = {row["event_id"]: row for row in global_rows}
    score_deltas = []
    for market_row in market_rows:
        global_row = global_by_event[market_row["event_id"]]
        market_brier, market_log = row_scores(market_row)
        global_brier, global_log = row_scores(global_row)
        score_deltas.append({
            "target_date": market_row["target_date"],
            "brier_improvement": market_brier - global_brier,
            "log_improvement": global_log - market_log,
        })
    comparison = {
        "events": len(score_deltas),
        "independent_dates": len({row["target_date"] for row in score_deltas}),
        "mean_brier_improvement": statistics.fmean(row["brier_improvement"] for row in score_deltas),
        "mean_log_improvement": statistics.fmean(row["log_improvement"] for row in score_deltas),
        "date_block_brier_improvement": bootstrap_dates(
            score_deltas,
            lambda sample: statistics.fmean(row["brier_improvement"] for row in sample),
        ),
        "date_block_log_improvement": bootstrap_dates(
            score_deltas,
            lambda sample: statistics.fmean(row["log_improvement"] for row in sample),
        ),
    }
    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "method": {
            "independent_unit": "target_date",
            "dates": len(dates), "raw_events": len(raw),
            "training": "leave one complete target date out",
            "prior": "Dirichlet(1,1,1,1); marginal Beta posterior per category",
            "fee_rate": WEATHER_TAKER_FEE_RATE,
            "selection": "one candidate per date, highest predicted EV, max spread tie-break",
            "shadow_only": True,
        },
        "calibration": calibration,
        "global_vs_market_calibration": comparison,
        "rules": output,
        "audits": selected_audits,
    }


def fmt(value: Any, digits: int = 3) -> str:
    return "N/A" if value is None else f"{float(value):.{digits}f}"


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 连续三桶概率校准与交叉验证", "",
        "> 概率只使用其他目标日期估计；全部结果为历史shadow研究。", "",
        f"- 独立日期：{report['method']['dates']}；原始候选事件：{report['method']['raw_events']}。",
        "", "## 交叉验证组合结果", "",
        "| 规则 | 日期 | 事件 | 净ROI | 净PnL | 5%下界 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, item in report["rules"].items():
        roi = item["roi"] * 100 if item["roi"] is not None else None
        lower = item["date_block_bootstrap_pnl_per_event"]["p05"]
        lines.append(
            f"| `{name}` | {item['independent_dates']} | {item['events']} | {fmt(roi, 1)}% | "
            f"{fmt(item['total_pnl'])} | {fmt(lower)} |"
        )
    lines += ["", "## 概率质量", "", "| 模型/结构 | Brier | 平均log score |", "|---|---:|---:|"]
    for name, item in report["calibration"].items():
        lines.append(f"| `{name}` | {fmt(item['brier'], 4)} | {fmt(item['log_score'], 4)} |")
    comparison = report["global_vs_market_calibration"]
    lines += [
        "", "## Global校准相对市场锚", "",
        f"- Brier平均改善：{fmt(comparison['mean_brier_improvement'], 4)}；"
        f"日期块5%下界：{fmt(comparison['date_block_brier_improvement']['p05'], 4)}。",
        f"- Log score平均改善：{fmt(comparison['mean_log_improvement'], 4)}；"
        f"日期块5%下界：{fmt(comparison['date_block_log_improvement']['p05'], 4)}。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("data/weather_market_monitor.sqlite3"))
    parser.add_argument("--output-json", type=Path, default=Path("research/output/weather_ladder_probability_calibration.json"))
    parser.add_argument("--output-md", type=Path, default=Path("research/output/weather_ladder_probability_calibration.md"))
    args = parser.parse_args()
    report = run(args.database)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output_md.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({
        "json": str(args.output_json), "markdown": str(args.output_md),
        "rules": len(report["rules"]), "dates": report["method"]["dates"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
