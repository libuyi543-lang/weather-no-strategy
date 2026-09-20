#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from weather_shadow_research import init_shadow_schema


ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
VARIANTS = [
    "baseline_mblue_ecmwf_metar",
    "plus_fast_metar",
    "plus_jaxa_swr",
    "plus_jaxa_cloud",
    "plus_cma_station",
    "plus_cma_radar_lightning",
]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _winning_temperatures(db: sqlite3.Connection) -> dict[tuple[str, str], float]:
    rows = db.execute(
        """
        SELECT e.station_id,e.target_date,m.bucket_low,m.bucket_high
        FROM market_resolutions r JOIN markets m ON m.market_id=r.market_id
        JOIN events e ON e.event_id=m.event_id
        WHERE r.is_resolved=1 AND lower(r.winning_outcome)='yes'
        """
    ).fetchall()
    output = {}
    for row in rows:
        low, high = row["bucket_low"], row["bucket_high"]
        if low is not None and high is not None and abs(float(low) - float(high)) < 0.001:
            output[(row["station_id"], row["target_date"])] = float(low)
    return output


def evaluate(db: sqlite3.Connection) -> dict[str, Any]:
    init_shadow_schema(db)
    dates = [row[0] for row in db.execute(
        "SELECT DISTINCT target_date FROM shadow_source_snapshots ORDER BY target_date"
    )]
    coverage_rows = db.execute(
        """
        SELECT source,status,COUNT(*) AS n,COUNT(DISTINCT target_date) AS dates
        FROM shadow_source_snapshots GROUP BY source,status ORDER BY source,status
        """
    ).fetchall()
    coverage: dict[str, dict[str, Any]] = defaultdict(lambda: {"statuses": {}})
    for row in coverage_rows:
        coverage[row["source"]]["statuses"][row["status"]] = int(row["n"])
        coverage[row["source"]]["independent_dates"] = max(
            int(row["dates"]), coverage[row["source"]].get("independent_dates", 0)
        )

    actual = _winning_temperatures(db)
    metrics = {}
    for variant in VARIANTS:
        rows = db.execute(
            """
            SELECT * FROM shadow_ablation_predictions
            WHERE variant=? AND is_oos=1 ORDER BY target_date,station_id,signal_time_utc
            """,
            (variant,),
        ).fetchall()
        errors = []
        exact = top2 = within_one = 0
        lead = []
        pnl = []
        brier_scores = []
        log_losses = []
        evaluated = 0
        used_dates: set[str] = set()
        for row in rows:
            key = (row["station_id"], row["target_date"])
            if key not in actual or row["predicted_max_c"] is None:
                continue
            evaluated += 1
            used_dates.add(row["target_date"])
            errors.append(abs(float(row["predicted_max_c"]) - actual[key]))
            exact += int(round(float(row["predicted_max_c"])) == round(actual[key]))
            within_one += int(abs(float(row["predicted_max_c"]) - actual[key]) <= 1.0)
            try:
                buckets = json.loads(row["top_buckets_json"] or "[]")
            except json.JSONDecodeError:
                buckets = []
            top2 += int(round(actual[key]) in [round(float(value)) for value in buckets[:2]])
            try:
                probabilities = json.loads(row["probabilities_json"] or "{}")
            except json.JSONDecodeError:
                probabilities = {}
            if isinstance(probabilities, dict) and probabilities:
                actual_bucket = str(round(actual[key]))
                normalized = {str(key): float(value) for key, value in probabilities.items()}
                total = sum(max(0.0, value) for value in normalized.values())
                if total > 0:
                    normalized = {key: max(0.0, value) / total for key, value in normalized.items()}
                    brier_scores.append(sum(
                        (probability - (1.0 if bucket == actual_bucket else 0.0)) ** 2
                        for bucket, probability in normalized.items()
                    ))
                    actual_probability = min(1.0 - 1e-12, max(1e-12, normalized.get(actual_bucket, 0.0)))
                    log_losses.append(-math.log(actual_probability))
            if row["lead_minutes"] is not None:
                lead.append(float(row["lead_minutes"]))
            if row["hypothetical_5share_pnl"] is not None:
                pnl.append(float(row["hypothetical_5share_pnl"]))
        metrics[variant] = {
            "oos_predictions": evaluated,
            "independent_oos_dates": len(used_dates),
            "mae_c": round(_mean(errors), 4) if errors else None,
            "exact_bucket_rate": round(exact / evaluated, 4) if evaluated else None,
            "top2_bucket_coverage": round(top2 / evaluated, 4) if evaluated else None,
            "within_1c_rate": round(within_one / evaluated, 4) if evaluated else None,
            "brier_score": round(_mean(brier_scores), 5) if brier_scores else None,
            "log_loss": round(_mean(log_losses), 5) if log_losses else None,
            "mean_market_lead_minutes": round(_mean(lead), 2) if lead else None,
            "hypothetical_5share_pnl": round(sum(pnl), 4) if pnl else None,
        }

    return {
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "independent_shadow_dates": len(dates),
        "minimum_dates": 30,
        "preferred_dates": 60,
        "ready_for_conclusions": len(dates) >= 30,
        "method": "walk-forward by target date; thresholds must not be tuned on evaluation dates",
        "coverage": dict(coverage),
        "variants": metrics,
        "required_metrics": [
            "OOS MAE", "exact bucket hit", "top-2 coverage", "within 1C",
            "Brier/log loss when probabilities exist", "market lead minutes", "5-share executable PnL",
        ],
    }


def write_report(config: dict[str, Any], result: dict[str, Any]) -> None:
    report_dir = ROOT / config["reportDirectory"]
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "shadow_ablation_latest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# Weather Source Shadow Ablation", "",
        f"Generated: {result['generated_at_utc']}", "",
        f"Independent dates: {result['independent_shadow_dates']} / 30 minimum / 60 preferred", "",
        f"Ready for conclusions: {'yes' if result['ready_for_conclusions'] else 'no'}", "",
        "No source should be accepted or rejected before the minimum independent-date gate.", "",
        "## Variants", "",
        "| Variant | OOS dates | OOS predictions | MAE C | Exact bucket | Top-2 | Within 1C | Brier | Log loss | Lead min | 5-share PnL |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant, row in result["variants"].items():
        lines.append(
            f"| {variant} | {row['independent_oos_dates']} | {row['oos_predictions']} | "
            f"{row['mae_c'] if row['mae_c'] is not None else '-'} | "
            f"{row['exact_bucket_rate'] if row['exact_bucket_rate'] is not None else '-'} | "
            f"{row['top2_bucket_coverage'] if row['top2_bucket_coverage'] is not None else '-'} | "
            f"{row['within_1c_rate'] if row['within_1c_rate'] is not None else '-'} | "
            f"{row['brier_score'] if row['brier_score'] is not None else '-'} | "
            f"{row['log_loss'] if row['log_loss'] is not None else '-'} | "
            f"{row['mean_market_lead_minutes'] if row['mean_market_lead_minutes'] is not None else '-'} | "
            f"{row['hypothetical_5share_pnl'] if row['hypothetical_5share_pnl'] is not None else '-'} |"
        )
    (report_dir / "shadow_ablation_latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate walk-forward weather source ablations")
    parser.add_argument("--report", action="store_true")
    parser.parse_args()
    config = json.loads((ROOT / "monitor_config.json").read_text(encoding="utf-8"))
    db = sqlite3.connect(ROOT / config["databasePath"], timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000")
    try:
        result = evaluate(db)
        write_report(config, result)
    finally:
        db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
