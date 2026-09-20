#!/usr/bin/env python3
"""Robustness checks for one-city-per-date market-centered ladders.

This is a read-only historical study. It adds nested date holdouts and rolling
selection so that the best-looking selector is not evaluated only on the same
dates used to choose it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from weather_ladder_microstructure_research import (
    ALLOWED_CITIES,
    EXECUTABLE_WEIGHT_STRUCTURES,
    base_eligible,
    event_state,
    evaluate_weights,
    metrics,
    select_one_per_date,
)


UTC = timezone.utc
SELECTORS = (
    "lowest_cost", "highest_center_midpoint", "lowest_center_midpoint",
    "highest_center_lead", "lowest_center_lead", "lowest_max_spread",
)


def load_rule_rows(database: Path) -> tuple[list[str], dict[str, dict[str, dict[str, Any]]]]:
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
    events = [row for row in events if row["city"] in ALLOWED_CITIES]
    baseline_states = []
    for event in events:
        state = event_state(db, event, 660)
        if state is None:
            continue
        eligible, result = base_eligible(state)
        if eligible and result is not None:
            baseline_states.append(state)

    rule_rows: dict[str, dict[str, dict[str, Any]]] = {}
    for structure, weights in EXECUTABLE_WEIGHT_STRUCTURES.items():
        rows = []
        for state in baseline_states:
            result = evaluate_weights(state, weights)
            if result is not None:
                rows.append({**state, **result})
        for selector in SELECTORS:
            name = f"{structure}|{selector}"
            selected = select_one_per_date(rows, selector, max_cost=15.0)
            rule_rows[name] = {row["target_date"]: row for row in selected}
    dates = sorted({state["target_date"] for state in baseline_states})
    db.close()
    return dates, rule_rows


def score(rule: dict[str, dict[str, Any]], dates: list[str]) -> float:
    values = [
        rule[day]["pnl"] / rule[day]["cost"] if day in rule and rule[day]["cost"] else 0.0
        for day in dates
    ]
    return sum(values) / len(values) if values else float("-inf")


def cross_validated_rows(
    dates: list[str], rules: dict[str, dict[str, dict[str, Any]]], mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    output, audit = [], []
    if mode == "leave_one_date_out":
        splits = [(day, [other for other in dates if other != day]) for day in dates]
    elif mode == "rolling_origin":
        splits = [(dates[index], dates[:index]) for index in range(7, len(dates))]
    else:
        raise ValueError(mode)
    for test_day, train_days in splits:
        best = max(sorted(rules), key=lambda name: score(rules[name], train_days))
        row = rules[best].get(test_day)
        audit.append({
            "test_date": test_day, "training_dates": len(train_days), "selected_rule": best,
            "training_mean_daily_return": score(rules[best], train_days),
            "test_city": row["city"] if row else None,
            "test_pnl": row["pnl"] if row else 0.0,
        })
        if row:
            output.append(row)
        else:
            output.append({
                "target_date": test_day, "city": None, "cost": 0.0,
                "notional": 0.0, "fees": 0.0, "payout": 0.0, "pnl": 0.0,
            })
    return output, audit


def leave_group_out(
    rows: list[dict[str, Any]], field: str,
) -> dict[str, dict[str, Any]]:
    output = {}
    for value in sorted({row[field] for row in rows}):
        kept = [row for row in rows if row[field] != value]
        output[str(value)] = metrics(kept)
    return output


def outcome_leg_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(outside=0, lower=0, center=0, upper=0)
    for row in rows:
        hits = [index for index, leg in enumerate(row["legs"]) if leg["payout"] > 0]
        counts[("lower", "center", "upper")[hits[0]] if hits else "outside"] += 1
    return dict(counts)


def overlap(
    left: dict[str, dict[str, Any]], right: dict[str, dict[str, Any]], dates: list[str],
) -> dict[str, Any]:
    common = [day for day in dates if day in left and day in right]
    same = sum(left[day]["event_id"] == right[day]["event_id"] for day in common)
    return {"common_dates": len(common), "same_event_dates": same, "fraction": same / len(common) if common else None}


def run(database: Path) -> dict[str, Any]:
    dates, rules = load_rule_rows(database)
    full_scores = {name: score(rows, dates) for name, rows in rules.items()}
    full_best = max(sorted(rules), key=lambda name: full_scores[name])
    full_rows = [rules[full_best][day] for day in dates if day in rules[full_best]]
    loodo_rows, loodo_audit = cross_validated_rows(dates, rules, "leave_one_date_out")
    rolling_rows, rolling_audit = cross_validated_rows(dates, rules, "rolling_origin")

    focus_rules = (
        "5/10/5|lowest_center_lead", "5/15/5|lowest_center_lead",
        "5/15/5|lowest_max_spread", "5/20/5|lowest_center_lead",
        "5/20/5|lowest_max_spread",
    )
    focus = {}
    for name in focus_rules:
        rows = [rules[name][day] for day in dates if day in rules[name]]
        midpoint = len(dates) // 2
        first = [row for row in rows if row["target_date"] in dates[:midpoint]]
        second = [row for row in rows if row["target_date"] in dates[midpoint:]]
        focus[name] = {
            "metrics": metrics(rows), "first_half": metrics(first), "second_half": metrics(second),
            "outcome_legs": outcome_leg_counts(rows),
            "leave_one_city_out": leave_group_out(rows, "city"),
        }
    overlaps = {}
    for index, left in enumerate(focus_rules):
        for right in focus_rules[index + 1:]:
            overlaps[f"{left} <> {right}"] = overlap(rules[left], rules[right], dates)

    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "method": {
            "cutoff_local": "11:00", "independent_unit": "target_date",
            "candidate_rules": len(rules), "full_dates": len(dates),
            "training_score": "mean daily net return; missing date is zero",
            "warning": "Historical robustness only; not an out-of-sample live result.",
        },
        "dates": dates,
        "full_sample_best_rule": full_best,
        "full_sample_best_metrics": metrics(full_rows),
        "leave_one_date_out": {"metrics": metrics(loodo_rows), "audit": loodo_audit},
        "rolling_origin": {"metrics": metrics(rolling_rows), "audit": rolling_audit},
        "focus_rules": focus,
        "selection_overlap": overlaps,
    }


def number(value: Any, digits: int = 3) -> str:
    return "N/A" if value is None else f"{float(value):.{digits}f}"


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 连续三桶选择器稳健性研究", "",
        "> 历史稳健性诊断，不是未来样本外证据。", "",
        f"- 独立日期：{report['method']['full_dates']}；候选组合规则：{report['method']['candidate_rules']}。",
        f"- 全样本最佳：`{report['full_sample_best_rule']}`。", "",
        "## 嵌套日期检验", "",
        "| 方法 | 日期 | 净ROI | 净PnL | 5%下界 |",
        "|---|---:|---:|---:|---:|",
    ]
    for label, key in (("留一日期", "leave_one_date_out"), ("滚动起点", "rolling_origin")):
        item = report[key]["metrics"]
        roi = item["roi"] * 100 if item["roi"] is not None else None
        lower = item["date_block_bootstrap_pnl_per_event"]["p05"]
        lines.append(
            f"| {label} | {item['independent_dates']} | {number(roi, 1)}% | "
            f"{number(item['total_pnl'])} | {number(lower)} |"
        )
    lines += ["", "## 预关注规则", "", "| 规则 | 日期 | 净ROI | 净PnL | 前半ROI | 后半ROI | 命中腿 |", "|---|---:|---:|---:|---:|---:|---|"]
    for name, value in report["focus_rules"].items():
        item, first, second = value["metrics"], value["first_half"], value["second_half"]
        lines.append(
            f"| `{name}` | {item['independent_dates']} | {number(item['roi'] * 100, 1)}% | "
            f"{number(item['total_pnl'])} | {number(first['roi'] * 100, 1)}% | "
            f"{number(second['roi'] * 100, 1)}% | `{json.dumps(value['outcome_legs'], ensure_ascii=False)}` |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("data/weather_market_monitor.sqlite3"))
    parser.add_argument("--output-json", type=Path, default=Path("research/output/weather_ladder_selector_robustness.json"))
    parser.add_argument("--output-md", type=Path, default=Path("research/output/weather_ladder_selector_robustness.md"))
    args = parser.parse_args()
    report = run(args.database)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output_md.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({
        "json": str(args.output_json), "markdown": str(args.output_md),
        "full_best": report["full_sample_best_rule"],
        "loocv": report["leave_one_date_out"]["metrics"],
        "rolling": report["rolling_origin"]["metrics"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
