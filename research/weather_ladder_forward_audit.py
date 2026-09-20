#!/usr/bin/env python3
"""Audit frozen ladder portfolios on genuinely forward target dates.

The report is read-only and date-based. Missing captures and no-trade dates stay
in the denominator so the forward study cannot silently keep only opportunities.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


UTC = timezone.utc
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
FORWARD_START = date(2026, 8, 5)
CAPTURE_DEADLINE_MINUTES = 11 * 60 + 12
FEE_RATE = 0.05
ALLOWED_CITIES = {
    "Shanghai", "Beijing", "Guangzhou", "Qingdao", "Wuhan", "Chongqing", "Chengdu",
}
PORTFOLIOS = {
    "ladder_portfolio_v3_1100_5_20_5_lowest_center_lead": {
        "label": "V3", "forward_start": date(2026, 8, 5),
    },
    "ladder_portfolio_v4_1_1100_5_15_5_all_eligible": {
        "label": "V4.1", "forward_start": date(2026, 8, 7),
    },
}


def parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def dates_between(start: date, end: date) -> list[str]:
    if end < start:
        return []
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(quantile * len(ordered)))]


def bootstrap_daily_mean(
    rows: list[dict[str, Any]], samples: int = 20_000, seed: int = 20260805,
) -> dict[str, float | None]:
    if not rows:
        return {"p05": None, "median": None, "p95": None}
    values = [float(row["pnl"]) for row in rows]
    rng = random.Random(seed)
    estimates = [
        sum(rng.choice(values) for _ in values) / len(values)
        for _ in range(samples)
    ]
    return {
        "p05": percentile(estimates, 0.05),
        "median": percentile(estimates, 0.50),
        "p95": percentile(estimates, 0.95),
    }


def outcome_category(legs_json: Any, winning_range: Any) -> str | None:
    try:
        legs = json.loads(legs_json) if isinstance(legs_json, str) else legs_json
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(legs, list) or len(legs) != 3 or not winning_range:
        return None
    for index, leg in enumerate(legs):
        if isinstance(leg, dict) and str(leg.get("outcomeRange")) == str(winning_range):
            return ("lower", "center", "upper")[index]
    return "outside"


def market_anchor_scores(legs_json: Any, winning_range: Any) -> dict[str, float] | None:
    try:
        legs = json.loads(legs_json) if isinstance(legs_json, str) else legs_json
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(legs, list) or len(legs) != 3:
        return None
    probabilities = []
    for leg in legs:
        midpoint = leg.get("midpoint") if isinstance(leg, dict) else None
        if midpoint is None:
            return None
        probabilities.append(max(0.0, min(1.0, float(midpoint))))
    probabilities.append(max(0.0, 1.0 - sum(probabilities)))
    total = sum(probabilities)
    if total <= 0:
        return None
    probabilities = [value / total for value in probabilities]
    category = outcome_category(legs, winning_range)
    if category is None:
        return None
    index = ("lower", "center", "upper", "outside").index(category)
    brier = sum((probability - float(position == index)) ** 2 for position, probability in enumerate(probabilities))
    log_score = -math.log(max(1e-12, probabilities[index]))
    return {"brier": brier, "log_score": log_score}


def performance(rows: list[sqlite3.Row], seed: int) -> dict[str, Any]:
    completed: list[dict[str, Any]] = []
    categories: Counter[str] = Counter()
    scores: list[dict[str, float]] = []
    selected = no_trade = unresolved = 0
    by_date: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_date.setdefault(str(row["target_date"]), []).append(row)
    for target_date, daily_rows in by_date.items():
        selected_rows = [row for row in daily_rows if str(row["selection_status"]) == "SELECTED_SHADOW"]
        if not selected_rows:
            no_trade += 1
            completed.append({"target_date": target_date, "cost": 0.0, "pnl": 0.0})
            continue
        selected += 1
        if any(row["resolved_at_utc"] is None or row["hypothetical_pnl_usdc"] is None for row in selected_rows):
            unresolved += 1
            continue
        cost = sum(float(row["selected_cost_usdc"] or 0.0) for row in selected_rows)
        pnl = sum(float(row["hypothetical_pnl_usdc"] or 0.0) for row in selected_rows)
        completed.append({"target_date": target_date, "cost": cost, "pnl": pnl})
        for row in selected_rows:
            category = outcome_category(row["legs_json"], row["winning_range"])
            if category:
                categories[category] += 1
            score = market_anchor_scores(row["legs_json"], row["winning_range"])
            if score:
                scores.append(score)

    total_cost = sum(row["cost"] for row in completed)
    total_pnl = sum(row["pnl"] for row in completed)
    cumulative = peak = max_drawdown = 0.0
    losing_streak = max_losing_streak = 0
    for row in sorted(completed, key=lambda item: item["target_date"]):
        cumulative += row["pnl"]
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        losing_streak = losing_streak + 1 if row["pnl"] < 0 else 0
        max_losing_streak = max(max_losing_streak, losing_streak)
    return {
        "recorded_dates": len(by_date),
        "recorded_selections": len(rows),
        "completed_dates": len(completed),
        "selected_dates": selected,
        "no_eligible_dates": no_trade,
        "unresolved_selected_dates": unresolved,
        "total_net_cost_usdc": total_cost,
        "total_net_pnl_usdc": total_pnl,
        "net_roi": total_pnl / total_cost if total_cost else None,
        "mean_net_pnl_per_completed_date": total_pnl / len(completed) if completed else None,
        "date_bootstrap_mean_net_pnl": bootstrap_daily_mean(completed, seed=seed),
        "profitable_dates": sum(row["pnl"] > 0 for row in completed),
        "max_drawdown_usdc": max_drawdown,
        "max_consecutive_losing_dates": max_losing_streak,
        "outcome_categories": dict(categories),
        "market_anchor_probability_scores": {
            "scored_dates": len(scores),
            "mean_brier": sum(item["brier"] for item in scores) / len(scores) if scores else None,
            "mean_log_score": sum(item["log_score"] for item in scores) / len(scores) if scores else None,
        },
    }


def run(database: Path, as_of: datetime | None = None) -> dict[str, Any]:
    as_of = (as_of or datetime.now(UTC)).astimezone(UTC)
    local_now = as_of.astimezone(LOCAL_TZ)
    due_dates = dates_between(FORWARD_START, local_now.date())
    if due_dates and local_now.date() == FORWARD_START and local_now.hour * 60 + local_now.minute < CAPTURE_DEADLINE_MINUTES:
        due_dates.pop()
    elif due_dates and local_now.hour * 60 + local_now.minute < CAPTURE_DEADLINE_MINUTES:
        due_dates.pop()

    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    event_cities: dict[str, set[str]] = {}
    for row in db.execute(
        "SELECT target_date,city FROM events WHERE target_date>=? AND target_date<=?",
        (FORWARD_START.isoformat(), local_now.date().isoformat()),
    ):
        if row["city"] in ALLOWED_CITIES:
            event_cities.setdefault(str(row["target_date"]), set()).add(str(row["city"]))

    candidate_rows = db.execute(
        """
        SELECT target_date,eligibility_status,rejection_reasons_json,frozen_slot_utc,
               frozen_at_utc,estimated_fee_usdc,net_cost_usdc
        FROM weather_ladder_frozen_candidates WHERE target_date>=?
        """,
        (FORWARD_START.isoformat(),),
    ).fetchall()
    candidates_by_date: dict[str, list[sqlite3.Row]] = {}
    for row in candidate_rows:
        candidates_by_date.setdefault(str(row["target_date"]), []).append(row)

    placeholders = ",".join("?" for _ in PORTFOLIOS)
    selection_rows = db.execute(
        f"""
        SELECT s.*,v.legs_json,v.taker_fee_rate FROM weather_ladder_frozen_portfolio_selections s
        LEFT JOIN weather_ladder_frozen_variants v ON v.variant_id=s.variant_id
        WHERE s.target_date>=? AND s.portfolio_version IN ({placeholders})
        ORDER BY s.target_date,s.portfolio_version
        """,
        (FORWARD_START.isoformat(), *PORTFOLIOS),
    ).fetchall()
    selections_by_key: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in selection_rows:
        selections_by_key.setdefault(
            (str(row["target_date"]), str(row["portfolio_version"])), []
        ).append(row)

    date_audit = []
    rejection_counts: Counter[str] = Counter()
    for target_date in due_dates:
        candidates = candidates_by_date.get(target_date, [])
        for row in candidates:
            try:
                rejection_counts.update(json.loads(row["rejection_reasons_json"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                rejection_counts["INVALID_REJECTION_JSON"] += 1
        issues = []
        expected = len(event_cities.get(target_date, set()))
        if expected == 0:
            issues.append("MISSING_EVENT_UNIVERSE")
        if not candidates:
            issues.append("MISSING_CANDIDATE_CAPTURE")
        elif expected and len(candidates) < expected:
            issues.append("PARTIAL_CANDIDATE_CAPTURE")
        for candidate in candidates:
            slot = parse_ts(candidate["frozen_slot_utc"])
            frozen_at = parse_ts(candidate["frozen_at_utc"])
            if slot is None or slot.astimezone(LOCAL_TZ).hour * 60 + slot.astimezone(LOCAL_TZ).minute != 11 * 60:
                issues.append("INVALID_CANDIDATE_SLOT")
                break
            if frozen_at is None or not 0 <= (frozen_at - slot).total_seconds() <= 12 * 60:
                issues.append("LATE_OR_INVALID_CANDIDATE_CAPTURE")
                break
        portfolio_state = {}
        for version, portfolio in PORTFOLIOS.items():
            label = str(portfolio["label"])
            if date.fromisoformat(target_date) < portfolio["forward_start"]:
                portfolio_state[label] = "NOT_STARTED"
                continue
            selected_rows = selections_by_key.get((target_date, version), [])
            if not selected_rows:
                issues.append(f"MISSING_{label}_PORTFOLIO_SELECTION")
                portfolio_state[label] = "MISSING"
            else:
                active_rows = [row for row in selected_rows if row["selection_status"] == "SELECTED_SHADOW"]
                portfolio_state[label] = (
                    f"SELECTED_SHADOW:{len(active_rows)}"
                    if active_rows else str(selected_rows[0]["selection_status"])
                )
                for selected in active_rows:
                    notional = selected["selected_notional_usdc"]
                    fee = selected["selected_fee_usdc"]
                    net_cost = selected["selected_cost_usdc"]
                    if notional is None or fee is None or net_cost is None:
                        issues.append(f"{label}_MISSING_EXECUTABLE_COST_COMPONENT")
                    elif abs(float(net_cost) - float(notional) - float(fee)) > 1e-6:
                        issues.append(f"{label}_NET_COST_MISMATCH")
                    if net_cost is not None and float(net_cost) > 15.0 + 1e-9:
                        issues.append(f"{label}_NET_COST_ABOVE_15")
                    if selected["taker_fee_rate"] is None or abs(float(selected["taker_fee_rate"]) - FEE_RATE) > 1e-12:
                        issues.append(f"{label}_FEE_RATE_NOT_5_PERCENT")
        date_audit.append({
            "target_date": target_date,
            "expected_city_events": expected,
            "candidate_rows": len(candidates),
            "eligible_candidates": sum(row["eligibility_status"] == "ELIGIBLE_SHADOW" for row in candidates),
            "portfolio_state": portfolio_state,
            "quality_status": "PASS" if not issues else "FAIL",
            "issues": issues,
        })

    portfolio_reports = {}
    for index, (version, portfolio) in enumerate(PORTFOLIOS.items()):
        rows = [row for row in selection_rows if row["portfolio_version"] == version]
        portfolio_reports[str(portfolio["label"])] = {
            "portfolio_version": version,
            "forward_start_date": portfolio["forward_start"].isoformat(),
            **performance(rows, seed=20260805 + index),
        }

    completed_dates = min((value["completed_dates"] for value in portfolio_reports.values()), default=0)
    if completed_dates < 10:
        phase = "DATA_QUALITY_ONLY"
        next_checkpoint = 10
    elif completed_dates < 15:
        phase = "WAIT_FOR_INTERIM"
        next_checkpoint = 15
    elif completed_dates < 30:
        phase = "INTERIM_ONLY_NO_RULE_CHANGES"
        next_checkpoint = 30
    else:
        phase = "FORMAL_REVIEW_DUE"
        next_checkpoint = completed_dates

    db.close()
    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "as_of_utc": as_of.isoformat(),
        "forward_start_date": FORWARD_START.isoformat(),
        "independent_unit": "target_date",
        "shadow_only": True,
        "evidence_status": (
            "NO_FORWARD_DATES_YET" if not due_dates else
            "FORWARD_SAMPLE_INSUFFICIENT" if completed_dates < 30 else
            "READY_FOR_PRE_REGISTERED_REVIEW_NOT_LIVE_APPROVAL"
        ),
        "phase": phase,
        "next_checkpoint_dates": next_checkpoint,
        "dates_remaining_to_checkpoint": max(0, next_checkpoint - completed_dates),
        "due_dates": len(due_dates),
        "quality_pass_dates": sum(row["quality_status"] == "PASS" for row in date_audit),
        "date_audit": date_audit,
        "rejection_reason_counts": dict(rejection_counts.most_common()),
        "portfolios": portfolio_reports,
        "interpretation": {
            "supported": "前10个新日期只能判断数据链路是否完整，不能判断策略盈利能力。",
            "hypothesis": "V4可能保留历史样本中的市场中心低估、相邻尾桶高估现象。",
            "not_supported": "任何当前前向结果都不授权实盘，也不授权把天气过程条件加入准入。",
        },
    }


def number(value: Any, digits: int = 3) -> str:
    return "N/A" if value is None else f"{float(value):.{digits}f}"


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 连续三桶前向 Shadow 审计", "",
        f"> 前向起点：{report['forward_start_date']}；独立单位：目标日期；仅 shadow。", "",
        f"- 证据状态：`{report['evidence_status']}`。当前到期日期 {report['due_dates']} 个，质量通过 {report['quality_pass_dates']} 个。",
        f"- 当前阶段：`{report['phase']}`；距离下一检查点还差 {report['dates_remaining_to_checkpoint']} 个完整日期。",
        "- 10日期前只检查数据；15日期仅中期报告；30日期才按预注册条件正式复核。", "",
        "## 组合结果", "",
        "| 组合 | 已记录日 | 完整日 | 选中/无交易 | 净ROI | 净PnL | 日均PnL 5%下界 | 回撤 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, item in report["portfolios"].items():
        roi = item["net_roi"] * 100 if item["net_roi"] is not None else None
        roi_text = f"{number(roi, 1)}%" if roi is not None else "N/A"
        lines.append(
            f"| {label} | {item['recorded_dates']} | {item['completed_dates']} | "
            f"{item['selected_dates']}/{item['no_eligible_dates']} | {roi_text} | "
            f"{number(item['total_net_pnl_usdc'])} | "
            f"{number(item['date_bootstrap_mean_net_pnl']['p05'])} | "
            f"{number(item['max_drawdown_usdc'])} |"
        )
    lines += ["", "## 数据质量", "", "| 日期 | 事件 | 候选 | 合格 | V3 | V4.1 | 状态 | 问题 |", "|---|---:|---:|---:|---|---|---|---|"]
    if not report["date_audit"]:
        lines.append("| 尚未开始 | 0 | 0 | 0 | - | - | NOT_DUE | 2026-08-05 11:12 后产生首个检查点 |")
    for row in report["date_audit"]:
        lines.append(
            f"| {row['target_date']} | {row['expected_city_events']} | {row['candidate_rows']} | "
            f"{row['eligible_candidates']} | {row['portfolio_state'].get('V3')} | "
            f"{row['portfolio_state'].get('V4.1')} | {row['quality_status']} | "
            f"{', '.join(row['issues']) or '-'} |"
        )
    lines += [
        "", "## 结论边界", "",
        f"- 已有证据支持：{report['interpretation']['supported']}",
        f"- 仅假设：{report['interpretation']['hypothesis']}",
        f"- 尚不支持：{report['interpretation']['not_supported']}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("data/weather_market_monitor.sqlite3"))
    parser.add_argument("--as-of", help="ISO timestamp used for deterministic audits")
    parser.add_argument("--output-json", type=Path, default=Path("research/output/weather_ladder_forward_audit.json"))
    parser.add_argument("--output-md", type=Path, default=Path("research/output/weather_ladder_forward_audit.md"))
    args = parser.parse_args()
    as_of = parse_ts(args.as_of) if args.as_of else None
    if args.as_of and as_of is None:
        raise SystemExit("invalid --as-of timestamp")
    report = run(args.database, as_of)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output_md.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({
        "json": str(args.output_json), "markdown": str(args.output_md),
        "evidence_status": report["evidence_status"], "due_dates": report["due_dates"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
