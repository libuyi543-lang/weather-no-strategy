#!/usr/bin/env python3
"""Same-event price path after the frozen 11:00 selection.

It holds the 11:00 selected city fixed and only asks how the same package's
executable cost and winning-leg quote change at later cutoffs. This avoids
mistaking a change in city selection for a market reaction.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from weather_ladder_microstructure_research import (
    ALLOWED_CITIES,
    base_eligible,
    event_state,
    evaluate_weights,
)
from weather_ladder_selector_robustness import load_rule_rows


UTC = timezone.utc
WEIGHTS = (5.0, 15.0, 5.0)
CUTOFFS = (660, 665, 670, 675, 690)


def load_events(db: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = db.execute(
        """
        SELECT e.event_id,e.city,e.target_date,e.winning_market_id,e.winning_range,
               COALESCE(s.timezone,'Asia/Shanghai') timezone,e.station_id
        FROM events e LEFT JOIN stations s ON s.station_id=e.station_id
        WHERE e.resolved_at_utc IS NOT NULL AND e.winning_range IS NOT NULL
        """
    ).fetchall()
    return {str(row["event_id"]): row for row in rows if row["city"] in ALLOWED_CITIES}


def rows_for_cutoff(
    selected: list[dict[str, Any]], events: dict[str, sqlite3.Row],
    db: sqlite3.Connection, local_minutes: int,
) -> list[dict[str, Any]]:
    output = []
    for selected_row in selected:
        event = events.get(selected_row["event_id"])
        if event is None:
            continue
        state = event_state(db, event, local_minutes)
        if state is None:
            continue
        # Keep the same executable depth/spread/age guard, but do not require
        # the original (1/3/1) cost band after the market has moved.
        if state["age_minutes"] > 10.0 or max(leg["spread"] for leg in state["legs"]) > 0.20:
            continue
        result = evaluate_weights(state, WEIGHTS)
        if result is None:
            continue
        winning_leg = next(
            (leg for leg in state["legs"] if leg["market_id"] == selected_row["winning_market_id"]),
            None,
        )
        entry_winning_leg = next(
            (leg for leg in selected_row["legs"] if leg["market_id"] == selected_row["winning_market_id"]),
            None,
        )
        output.append({
            "target_date": selected_row["target_date"], "city": selected_row["city"],
            "event_id": selected_row["event_id"], "cost": result["cost"],
            "pnl": result["pnl"], "slot_utc": state["slot_utc"],
            "winning_midpoint_move": (
                winning_leg["midpoint"] - entry_winning_leg["midpoint"]
                if winning_leg is not None and entry_winning_leg is not None else None
            ),
            "center_bucket": state["center_bucket"],
            "entry_center_bucket": selected_row["center_bucket"],
            "center_bucket_changed": state["center_bucket"] != selected_row["center_bucket"],
        })
    return output


def run(database: Path) -> dict[str, Any]:
    dates, rules = load_rule_rows(database)
    selected = [rules["5/15/5|lowest_max_spread"][day] for day in dates]
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    events = load_events(db)
    output = {}
    audits = {}
    entry_rows = rows_for_cutoff(selected, events, db, 660)
    for cutoff in CUTOFFS:
        rows = rows_for_cutoff(selected, events, db, cutoff)
        deltas = [row["cost"] - entry["cost"] for row in rows for entry in entry_rows if entry["event_id"] == row["event_id"]]
        winner_moves = [row["winning_midpoint_move"] for row in rows if row["winning_midpoint_move"] is not None]
        output[f"{cutoff // 60:02d}:{cutoff % 60:02d}"] = {
            "events": len(rows), "selected_cohort_dates": len(selected),
            "missing_dates": len(selected) - len(rows),
            "average_cost": statistics.fmean(row["cost"] for row in rows) if rows else None,
            "average_cost_delta_vs_11": statistics.fmean(deltas) if deltas else None,
            "total_pnl": sum(row["pnl"] for row in rows),
            "roi": sum(row["pnl"] for row in rows) / sum(row["cost"] for row in rows) if rows else None,
            "winning_midpoint_move": {
                "events": len(winner_moves),
                "mean": statistics.fmean(winner_moves) if winner_moves else None,
                "positive": sum(value > 0 for value in winner_moves),
            },
            "center_bucket_switches": sum(row["center_bucket_changed"] for row in rows),
        }
        audits[str(cutoff)] = rows
    db.close()
    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "method": {
            "selection_rule": "11:00 5/15/5 lowest_max_spread",
            "same_event_cohort": True, "independent_unit": "target_date",
            "later_cutoff_guard": "book age <= 10m and max spread <= 0.20",
        },
        "selected_dates": len(selected), "entry_rows": len(entry_rows),
        "cutoffs": output, "audit": audits,
    }


def fmt(value: Any, digits: int = 3) -> str:
    return "N/A" if value is None else f"{float(value):.{digits}f}"


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 同事件盘口价格路径", "",
        "> 固定11:00选中的同一城市，不重新选择城市；历史shadow研究。", "",
        f"- 11:00选择日期：{report['selected_dates']}；可重建入场事件：{report['entry_rows']}。", "",
        "| 截点 | 同一事件数 | 缺失日期 | 平均成本 | 相对11:00成本变化 | 净ROI | 胜出桶价格变化 | 中心换档 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cutoff, item in report["cutoffs"].items():
        move = item["winning_midpoint_move"]
        lines.append(
            f"| {cutoff} | {item['events']} | {item['missing_dates']} | {fmt(item['average_cost'])} | "
            f"{fmt(item['average_cost_delta_vs_11'])} | {fmt(item['roi'] * 100 if item['roi'] is not None else None, 1)}% | "
            f"{fmt(move['mean'], 4)} ({move['positive']}/{move['events']}) | {item['center_bucket_switches']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("data/weather_market_monitor.sqlite3"))
    parser.add_argument("--output-json", type=Path, default=Path("research/output/weather_ladder_price_path.json"))
    parser.add_argument("--output-md", type=Path, default=Path("research/output/weather_ladder_price_path.md"))
    args = parser.parse_args()
    report = run(args.database)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output_md.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"json": str(args.output_json), "markdown": str(args.output_md)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
