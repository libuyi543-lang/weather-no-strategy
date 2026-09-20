#!/usr/bin/env python3
"""METAR event-time research for market-centered three-bucket ladders.

The script is read-only and shadow-only. It uses the first time a METAR was
fetched, reconstructs the last executable order book available after that
receipt, and evaluates a small pre-declared set of physics-motivated rules.
Independent uncertainty units are target dates, never snapshots or cities.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from weather_ladder_microstructure_research import (
    ALLOWED_CITIES,
    WEATHER_TAKER_FEE_RATE,
    as_float,
    book_levels,
    exact_markets,
    parse_json,
    vwap,
)


UTC = timezone.utc
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
ENTRY_START_MINUTES = 10 * 60
ENTRY_END_MINUTES = 15 * 60 + 30
MAX_BOOK_AGE_MINUTES = 10.0
MAX_ENTRY_DELAY_MINUTES = 10.0
MAX_SPREAD = 0.20
MIN_CENTER_LEAD = 0.03
MAX_STALE_PRICE_MOVE = 0.02

STRUCTURES = {
    "5/20/5": (5.0, 20.0, 5.0),
    "5/15/10": (5.0, 15.0, 10.0),
    "10/15/5": (10.0, 15.0, 5.0),
    "5/15/5": (5.0, 15.0, 5.0),
}


def parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def event_state_at(
    db: sqlite3.Connection, event: sqlite3.Row, as_of: datetime,
    *, after: bool = False,
) -> dict[str, Any] | None:
    operator, ordering = (">=", "ASC") if after else ("<=", "DESC")
    slot_row = db.execute(
        f"""
        SELECT slot_utc FROM market_snapshots
        WHERE event_id=? AND slot_utc {operator} ?
        ORDER BY slot_utc {ordering} LIMIT 1
        """,
        (event["event_id"], as_of.isoformat()),
    ).fetchone()
    if not slot_row:
        return None
    slot = parse_ts(slot_row["slot_utc"])
    if slot is None:
        return None
    rows = db.execute(
        """
        SELECT m.market_id,m.outcome_range,m.bucket_low,m.bucket_high,
               s.yes_best_bid,s.yes_best_ask,s.yes_book_json
        FROM markets m JOIN market_snapshots s ON s.market_id=m.market_id
        WHERE m.event_id=? AND s.slot_utc=? ORDER BY m.bucket_low
        """,
        (event["event_id"], slot_row["slot_utc"]),
    ).fetchall()
    exact = exact_markets(rows)
    if len(exact) < 3:
        return None
    ranked = sorted(exact, key=lambda item: (-item["midpoint"], item["bucket"]))
    center, second = ranked[0], ranked[1]
    by_bucket = {item["bucket"]: item for item in exact}
    legs = [by_bucket.get(center["bucket"] + delta) for delta in (-1, 0, 1)]
    if any(leg is None for leg in legs):
        return None
    return {
        "slot_utc": slot.isoformat(),
        "slot_dt": slot,
        "center_bucket": center["bucket"],
        "center_midpoint": center["midpoint"],
        "center_lead": center["midpoint"] - second["midpoint"],
        "legs": legs,
        "book_age_minutes": abs((as_of - slot).total_seconds()) / 60.0,
    }


def evaluate(
    state: dict[str, Any], weights: tuple[float, float, float], winning_market_id: str,
) -> dict[str, Any] | None:
    notional = fees = payout = 0.0
    legs = []
    for leg, shares in zip(state["legs"], weights):
        price, available = vwap(leg["yes_book_json"], shares)
        if price is None or available + 1e-9 < shares:
            return None
        fee = shares * WEATHER_TAKER_FEE_RATE * price * (1.0 - price)
        leg_payout = shares if leg["market_id"] == winning_market_id else 0.0
        notional += shares * price
        fees += fee
        payout += leg_payout
        legs.append({
            "bucket": leg["bucket"], "market_id": leg["market_id"],
            "shares": shares, "vwap": price, "fee": fee,
            "payout": leg_payout,
        })
    cost = notional + fees
    return {
        "notional": notional, "fees": fees, "cost": cost,
        "payout": payout, "pnl": payout - cost, "legs": legs,
    }


def weather_features(row: sqlite3.Row) -> dict[str, Any]:
    weather = parse_json(row["weather_state_json"], {})
    process = weather.get("process") or {}
    trend = process.get("primaryStationTrend") or {}
    remote = process.get("remoteSensing") or {}
    radar = ((remote.get("rainviewer") or {}).get("features") or {})
    solar = process.get("solarHeating") or {}
    comparison = process.get("modelRealityComparison") or {}
    metar = weather.get("metar") or {}
    detected = process.get("detectedProcesses") or []
    return {
        "temperature_c": as_float(metar.get("temp")),
        "daily_max_c": as_float(metar.get("dailyMaxC")),
        "trend_c_per_hour": as_float(trend.get("temperatureTrendCPerHour")),
        "radar_25km": as_float(radar.get("echoCoverage25Km")),
        "radar_100km_change": as_float(radar.get("echoCoverage100KmChange")),
        "solar_ratio": as_float(solar.get("jaxaToClearSkyRatio")),
        "ecmwf_same_hour_error_c": as_float(
            (comparison.get("ecmwf") or {}).get("observationMinusSameHourForecastC")
        ),
        "detected_processes": detected if isinstance(detected, list) else [],
    }


def eligible_state(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    return (
        state["book_age_minutes"] <= MAX_BOOK_AGE_MINUTES + 1e-9
        and state["center_lead"] >= MIN_CENTER_LEAD - 1e-9
        and max(leg["spread"] for leg in state["legs"]) <= MAX_SPREAD + 1e-9
    )


def candidate_events(db: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in ALLOWED_CITIES)
    events = db.execute(
        f"""
        SELECT e.event_id,e.city,e.target_date,e.station_id,e.winning_market_id,
               e.winning_range,s.timezone
        FROM events e JOIN stations s ON s.station_id=e.station_id
        WHERE e.city IN ({placeholders}) AND e.resolved_at_utc IS NOT NULL
          AND e.winning_market_id IS NOT NULL
        ORDER BY e.target_date,e.city
        """,
        tuple(sorted(ALLOWED_CITIES)),
    ).fetchall()
    output = []
    for event in events:
        snapshots = db.execute(
            """
            SELECT * FROM weather_ai_research_snapshots
            WHERE event_id=? AND winning_range IS NOT NULL
              AND observed_at_utc IS NOT NULL
            ORDER BY sample_slot_utc
            """,
            (event["event_id"],),
        ).fetchall()
        previous_observation = None
        previous_daily_max = None
        for row in snapshots:
            observation = str(row["observed_at_utc"] or "")
            if not observation or observation == previous_observation:
                continue
            features = weather_features(row)
            daily_max = features["daily_max_c"]
            daily_max_increased = (
                previous_daily_max is not None and daily_max is not None
                and daily_max > previous_daily_max + 1e-9
            )
            previous_observation = observation
            if daily_max is not None:
                previous_daily_max = max(previous_daily_max or daily_max, daily_max)

            report = db.execute(
                """
                SELECT first_fetched_at_utc FROM fast_metar_reports
                WHERE station_id=? AND observation_time_utc=?
                ORDER BY first_fetched_at_utc LIMIT 1
                """,
                (event["station_id"], observation),
            ).fetchone()
            receipt = parse_ts(report["first_fetched_at_utc"] if report else None)
            receipt_source = "fast_metar_first_fetched" if receipt else "research_snapshot_fetched"
            if receipt is None:
                observation_row = db.execute(
                    """
                    SELECT MIN(fetched_at_utc) AS first_fetched_at_utc
                    FROM weather_observations
                    WHERE station_id=? AND source='metar' AND status='ok'
                      AND observation_time_utc=?
                    """,
                    (event["station_id"], observation),
                ).fetchone()
                receipt = parse_ts(
                    observation_row["first_fetched_at_utc"] if observation_row else None
                )
                if receipt is not None:
                    receipt_source = "weather_observation_first_fetched"
            if receipt is None:
                receipt = parse_ts(row["source_fetched_at_utc"])
            if receipt is None:
                continue
            local = receipt.astimezone(LOCAL_TZ)
            local_minutes = local.hour * 60 + local.minute
            if not ENTRY_START_MINUTES <= local_minutes <= ENTRY_END_MINUTES:
                continue

            entry = event_state_at(db, event, receipt, after=True)
            before = event_state_at(db, event, receipt - timedelta(microseconds=1))
            if not eligible_state(entry) or not eligible_state(before):
                continue
            entry_delay = (entry["slot_dt"] - receipt).total_seconds() / 60.0
            if entry_delay < -1e-9 or entry_delay > MAX_ENTRY_DELAY_MINUTES + 1e-9:
                continue
            same_center = before["center_bucket"] == entry["center_bucket"]
            leg_moves = [
                entry_leg["midpoint"] - before_leg["midpoint"]
                for before_leg, entry_leg in zip(before["legs"], entry["legs"])
                if before_leg["bucket"] == entry_leg["bucket"]
            ]
            stale_market = (
                same_center and len(leg_moves) == 3
                and max(abs(move) for move in leg_moves) <= MAX_STALE_PRICE_MOVE + 1e-9
            )
            gap = (
                entry["center_bucket"] - daily_max
                if daily_max is not None else None
            )
            response = {}
            for horizon in (5, 10, 15):
                future = event_state_at(db, event, receipt + timedelta(minutes=horizon), after=True)
                if future is None:
                    continue
                winning_leg = next(
                    (leg for leg in future["legs"] if leg["market_id"] == str(event["winning_market_id"])),
                    None,
                )
                entry_winning_leg = next(
                    (leg for leg in entry["legs"] if leg["market_id"] == str(event["winning_market_id"])),
                    None,
                )
                response[f"{horizon}m"] = {
                    "center_bucket_changed": future["center_bucket"] != entry["center_bucket"],
                    "winning_bucket_present": winning_leg is not None and entry_winning_leg is not None,
                    "winning_midpoint_move": (
                        winning_leg["midpoint"] - entry_winning_leg["midpoint"]
                        if winning_leg is not None and entry_winning_leg is not None else None
                    ),
                    "entry_to_future_minutes": (
                        future["slot_dt"] - entry["slot_dt"]
                    ).total_seconds() / 60.0,
                }
            output.append({
                "event_id": str(event["event_id"]), "city": event["city"],
                "target_date": event["target_date"], "station_id": event["station_id"],
                "winning_market_id": str(event["winning_market_id"]),
                "winning_range": event["winning_range"], "observation_time_utc": observation,
                "receipt_time_utc": receipt.isoformat(), "receipt_source": receipt_source,
                "entry_slot_utc": entry["slot_utc"], "entry_delay_minutes": entry_delay,
                "local_minutes": local_minutes, "daily_max_increased": daily_max_increased,
                "same_center": same_center, "stale_market": stale_market,
                "max_leg_midpoint_move": max((abs(move) for move in leg_moves), default=None),
                "center_bucket": entry["center_bucket"], "center_lead": entry["center_lead"],
                "center_minus_daily_max_c": gap, "state": entry, **features,
                "market_response": response,
            })
    return output


def select_first_per_city_date(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = {}
    for row in sorted(rows, key=lambda item: (item["receipt_time_utc"], item["city"])):
        selected.setdefault((row["target_date"], row["city"]), row)
    return list(selected.values())


def select_first_per_date(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = {}
    for row in sorted(rows, key=lambda item: (item["receipt_time_utc"], item["city"])):
        selected.setdefault(row["target_date"], row)
    return list(selected.values())


def bootstrap_dates(
    rows: list[dict[str, Any]], value: Callable[[list[dict[str, Any]]], float],
    samples: int = 20000, seed: int = 20260805,
) -> dict[str, float | None]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[row["target_date"]].append(row)
    dates = sorted(by_date)
    if not dates:
        return {"p05": None, "median": None, "p95": None}
    rng = random.Random(seed)
    values = []
    for _ in range(samples):
        sampled = [item for _day in dates for item in by_date[rng.choice(dates)]]
        values.append(value(sampled))
    values.sort()
    index = lambda q: min(len(values) - 1, int(q * len(values)))
    return {"p05": values[index(0.05)], "median": values[index(0.5)], "p95": values[index(0.95)]}


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cost = sum(row["cost"] for row in rows)
    pnl = sum(row["pnl"] for row in rows)
    by_date = defaultdict(float)
    for row in rows:
        by_date[row["target_date"]] += row["pnl"]
    return {
        "events": len(rows), "independent_dates": len(by_date),
        "total_cost": cost, "total_pnl": pnl, "roi": pnl / cost if cost else None,
        "profitable_dates": sum(value > 0 for value in by_date.values()),
        "worst_date_pnl": min(by_date.values()) if by_date else None,
        "best_date_pnl": max(by_date.values()) if by_date else None,
        "date_block_bootstrap_pnl_per_event": bootstrap_dates(
            rows, lambda sample: sum(row["pnl"] for row in sample) / max(1, len(sample))
        ),
    }


def response_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    output = {}
    for horizon in ("5m", "10m", "15m"):
        values = [
            row["market_response"].get(horizon, {}).get("winning_midpoint_move")
            for row in rows
        ]
        values = [float(value) for value in values if value is not None and math.isfinite(float(value))]
        switches = sum(
            bool(row["market_response"].get(horizon, {}).get("center_bucket_changed"))
            for row in rows
        )
        output[horizon] = {
            "events_with_response": len(values),
            "mean_winning_midpoint_move": statistics.fmean(values) if values else None,
            "positive_winning_midpoint_moves": sum(value > 0 for value in values),
            "center_bucket_switches": switches,
        }
    return output


def apply_rule(
    candidates: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool],
    weights: tuple[float, float, float], frequency: str,
) -> list[dict[str, Any]]:
    selected = [row for row in candidates if predicate(row)]
    if frequency == "first_city_date":
        selected = select_first_per_city_date(selected)
    elif frequency == "first_date":
        selected = select_first_per_date(selected)
    output = []
    for row in selected:
        result = evaluate(row["state"], weights, row["winning_market_id"])
        if result is not None and result["cost"] <= 15.0 + 1e-9:
            output.append({**{key: value for key, value in row.items() if key != "state"}, **result})
    return output


def white_reality_check(
    daily_values: dict[str, dict[str, float]], samples: int = 50000,
    seed: int = 20260805,
) -> dict[str, Any]:
    dates = sorted({day for values in daily_values.values() for day in values})
    rules = sorted(daily_values)
    if not dates or not rules:
        return {"rules": len(rules), "dates": len(dates), "p_value": None}
    matrix = {rule: [daily_values[rule].get(day, 0.0) for day in dates] for rule in rules}
    means = {rule: statistics.fmean(values) for rule, values in matrix.items()}
    best = max(rules, key=means.get)
    observed = means[best]
    centered = {rule: [value - means[rule] for value in values] for rule, values in matrix.items()}
    rng = random.Random(seed)
    exceed = 0
    for _ in range(samples):
        indexes = [rng.randrange(len(dates)) for _day in dates]
        simulated = max(
            statistics.fmean(centered[rule][index] for index in indexes)
            for rule in rules
        )
        exceed += simulated >= observed - 1e-12
    return {
        "rules": len(rules), "dates": len(dates), "best_rule": best,
        "best_mean_daily_pnl": observed,
        "p_value": (exceed + 1) / (samples + 1), "bootstrap_samples": samples,
    }


def run(database: Path) -> dict[str, Any]:
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    candidates = candidate_events(db)

    def available(value: Any) -> bool:
        return value is not None and math.isfinite(float(value))

    predicates: dict[str, Callable[[dict[str, Any]], bool]] = {
        "all_new_metar": lambda row: True,
        "stale_after_new_metar": lambda row: row["stale_market"],
        "stale_after_new_daily_max": lambda row: row["stale_market"] and row["daily_max_increased"],
        "active_heating_stale": lambda row: (
            row["stale_market"] and row["daily_max_increased"]
            and available(row["trend_c_per_hour"]) and row["trend_c_per_hour"] > 0
            and available(row["center_minus_daily_max_c"])
            and row["center_minus_daily_max_c"] >= 2
        ),
        "clear_heating_stale": lambda row: (
            row["stale_market"] and row["daily_max_increased"]
            and available(row["trend_c_per_hour"]) and row["trend_c_per_hour"] > 0
            and available(row["center_minus_daily_max_c"])
            and row["center_minus_daily_max_c"] >= 2
            and (not available(row["radar_25km"]) or row["radar_25km"] <= 0.05)
            and (not available(row["solar_ratio"]) or row["solar_ratio"] >= 0.5)
        ),
        "stall_or_convection_stale": lambda row: (
            row["stale_market"] and (
                (available(row["trend_c_per_hour"]) and row["trend_c_per_hour"] <= 0)
                or (available(row["radar_25km"]) and row["radar_25km"] >= 0.10)
                or "convective_cold_pool" in row["detected_processes"]
            )
        ),
    }
    rule_specs = {
        "all_new_metar|5/20/5": ("all_new_metar", "5/20/5"),
        "stale_after_new_metar|5/20/5": ("stale_after_new_metar", "5/20/5"),
        "stale_after_new_daily_max|5/20/5": ("stale_after_new_daily_max", "5/20/5"),
        "active_heating_stale|5/20/5": ("active_heating_stale", "5/20/5"),
        "active_heating_stale|5/15/10": ("active_heating_stale", "5/15/10"),
        "clear_heating_stale|5/20/5": ("clear_heating_stale", "5/20/5"),
        "clear_heating_stale|5/15/10": ("clear_heating_stale", "5/15/10"),
        "stall_or_convection_stale|5/20/5": ("stall_or_convection_stale", "5/20/5"),
        "stall_or_convection_stale|10/15/5": ("stall_or_convection_stale", "10/15/5"),
    }
    results = {}
    audit = {}
    daily_values = {}
    for name, (predicate_name, structure_name) in rule_specs.items():
        rows = apply_rule(
            candidates, predicates[predicate_name], STRUCTURES[structure_name], "first_date"
        )
        results[name] = metrics(rows)
        audit[name] = rows
        daily_values[name] = {row["target_date"]: row["pnl"] for row in rows}

    report = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "method": {
            "independent_unit": "target_date",
            "receipt_time": (
                "fast_metar_reports.first_fetched_at_utc, then earliest weather_observations.fetched_at_utc; "
                "research snapshot fallback disclosed"
            ),
            "entry_window_local": "10:00-15:30 Asia/Shanghai",
            "entry_delay_max_minutes": MAX_ENTRY_DELAY_MINUTES,
            "stale_market_definition": "same center and max absolute three-leg midpoint move <= 0.02",
            "fee_rate": WEATHER_TAKER_FEE_RATE,
            "shadow_only": True,
        },
        "raw_candidates": len(candidates),
        "candidate_dates": len({row["target_date"] for row in candidates}),
        "receipt_source_counts": dict(
            sorted(defaultdict(int, {
                source: sum(row["receipt_source"] == source for row in candidates)
                for source in {row["receipt_source"] for row in candidates}
            }).items())
        ),
        "rules": results,
        "rule_market_response": {
            name: response_metrics(audit[name]) for name in rule_specs
        },
        "all_candidate_market_response": response_metrics(candidates),
        "multiple_rule_reality_check": white_reality_check(daily_values),
        "rule_audit": audit,
    }
    db.close()
    return report


def fmt(value: float | None, digits: int = 3) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}"


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# METAR事件时间连续三桶研究", "",
        "> 历史探索，独立单位为目标日期，严格shadow-only。", "",
        f"- 原始事件候选：{report['raw_candidates']}；独立日期：{report['candidate_dates']}。",
        f"- METAR接收时间来源：`{json.dumps(report['receipt_source_counts'], ensure_ascii=False)}`。",
        "", "## 规则结果", "",
        "| 规则 | 日期 | 事件 | 净ROI | 净PnL | 盈利日期 | 5%下界 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in report["rules"].items():
        lower = item["date_block_bootstrap_pnl_per_event"]["p05"]
        roi = item["roi"] * 100 if item["roi"] is not None else None
        lines.append(
            f"| `{name}` | {item['independent_dates']} | {item['events']} | "
            f"{fmt(roi, 1)}% | {fmt(item['total_pnl'])} | {item['profitable_dates']} | {fmt(lower)} |"
        )
    check = report["multiple_rule_reality_check"]
    response = report["rule_market_response"]["stale_after_new_daily_max|5/20/5"]
    lines += [
        "", "## 市场反应窗口", "",
        "| 新高后市场未动 | 样本 | 胜出桶均值变动 | 正向变动 | 中心换档 |",
        "|---|---:|---:|---:|---:|",
    ]
    for horizon in ("5m", "10m", "15m"):
        item = response[horizon]
        lines.append(
            f"| {horizon} | {item['events_with_response']} | "
            f"{fmt(item['mean_winning_midpoint_move'], 4)} | "
            f"{item['positive_winning_midpoint_moves']} | {item['center_bucket_switches']} |"
        )
    lines += [
        "", "## 多重检验", "",
        f"- 同时比较{check['rules']}条规则、{check['dates']}个日期；最佳规则："
        f"`{check.get('best_rule')}`。",
        f"- 日期块Reality Check p={fmt(check.get('p_value'), 4)}。",
        "- 该p值仍来自同一历史样本，只用于淘汰明显的数据挖掘结果。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("data/weather_market_monitor.sqlite3"))
    parser.add_argument(
        "--output-json", type=Path,
        default=Path("research/output/weather_ladder_event_window_report.json"),
    )
    parser.add_argument(
        "--output-md", type=Path,
        default=Path("research/output/weather_ladder_event_window_report.md"),
    )
    args = parser.parse_args()
    report = run(args.database)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output_md.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({
        "json": str(args.output_json), "markdown": str(args.output_md),
        "raw_candidates": report["raw_candidates"],
        "reality_check": report["multiple_rule_reality_check"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
