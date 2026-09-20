#!/usr/bin/env python3
"""Market-centered three-bucket ladder research with date-block uncertainty.

This script is deliberately research-only. It reconstructs each cutoff from
the last order book available at or before that time and never writes to the
production database.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


UTC = timezone.utc
WEATHER_TAKER_FEE_RATE = 0.05
ALLOWED_CITIES = {
    "Shanghai", "Beijing", "Guangzhou", "Qingdao", "Wuhan", "Chongqing", "Chengdu",
}
BASELINE_WEIGHTS = (1.0, 3.0, 1.0)
WEIGHT_STRUCTURES = {
    "1/3/1": (1.0, 3.0, 1.0),
    "0.5/3/0.5": (0.5, 3.0, 0.5),
    "0.5/4/0.5": (0.5, 4.0, 0.5),
    "0.25/4.5/0.25": (0.25, 4.5, 0.25),
    "0.5/2/0.5": (0.5, 2.0, 0.5),
    "1/4/1": (1.0, 4.0, 1.0),
    "1/2/2": (1.0, 2.0, 2.0),
    "2/2/1": (2.0, 2.0, 1.0),
    "1/1/1": (1.0, 1.0, 1.0),
}
EXECUTABLE_WEIGHT_STRUCTURES = {
    "5/5/5": (5.0, 5.0, 5.0),
    "5/10/5": (5.0, 10.0, 5.0),
    "5/15/5": (5.0, 15.0, 5.0),
    "5/20/5": (5.0, 20.0, 5.0),
    "5/25/5": (5.0, 25.0, 5.0),
}


def as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return fallback


def book_levels(book: Any, side: str) -> list[dict[str, float]]:
    payload = parse_json(book, {})
    rows = payload.get(side, []) if isinstance(payload, dict) else []
    output = []
    for row in rows:
        price, size = as_float(row.get("price")), as_float(row.get("size"))
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            output.append({"price": price, "size": size})
    return sorted(output, key=lambda row: row["price"], reverse=side == "bids")


def vwap(book: Any, shares: float) -> tuple[float | None, float]:
    remaining, total, filled = float(shares), 0.0, 0.0
    for level in book_levels(book, "asks"):
        take = min(remaining, level["size"])
        total += take * level["price"]
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            return total / shares, filled
    return None, filled


def cutoff_utc(target_date: str, timezone_name: str, local_minutes: int) -> datetime:
    local_date = date.fromisoformat(target_date)
    local = datetime.combine(
        local_date, time(local_minutes // 60, local_minutes % 60), ZoneInfo(timezone_name)
    )
    return local.astimezone(UTC)


def exact_markets(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        low, high = as_float(row["bucket_low"]), as_float(row["bucket_high"])
        bid, ask = as_float(row["yes_best_bid"]), as_float(row["yes_best_ask"])
        if (
            low is None or high is None or abs(low - high) > 1e-9
            or abs(low - round(low)) > 1e-9 or bid is None or ask is None
        ):
            continue
        output.append({
            "bucket": int(round(low)), "market_id": str(row["market_id"]),
            "outcome_range": str(row["outcome_range"]), "bid": bid, "ask": ask,
            "midpoint": (bid + ask) / 2.0, "spread": ask - bid,
            "yes_book_json": row["yes_book_json"],
        })
    return sorted(output, key=lambda item: item["bucket"])


def event_state(
    db: sqlite3.Connection, event: sqlite3.Row, local_minutes: int,
) -> dict[str, Any] | None:
    cutoff = cutoff_utc(event["target_date"], event["timezone"], local_minutes)
    cutoff_text = cutoff.isoformat()
    slot_row = db.execute(
        "SELECT MAX(slot_utc) slot FROM market_snapshots WHERE event_id=? AND slot_utc<=?",
        (event["event_id"], cutoff_text),
    ).fetchone()
    slot = slot_row["slot"] if slot_row else None
    if not slot:
        return None
    snapshot_time = datetime.fromisoformat(str(slot).replace("Z", "+00:00")).astimezone(UTC)
    age_minutes = (cutoff - snapshot_time).total_seconds() / 60.0
    rows = db.execute(
        """
        SELECT m.market_id,m.outcome_range,m.bucket_low,m.bucket_high,
               s.yes_best_bid,s.yes_best_ask,s.yes_book_json
        FROM markets m JOIN market_snapshots s ON s.market_id=m.market_id
        WHERE m.event_id=? AND s.slot_utc=? ORDER BY m.bucket_low
        """,
        (event["event_id"], slot),
    ).fetchall()
    exact = exact_markets(rows)
    if len(exact) < 2:
        return None
    ranked = sorted(exact, key=lambda item: (-item["midpoint"], item["bucket"]))
    center, second = ranked[0], ranked[1]
    by_bucket = {item["bucket"]: item for item in exact}
    legs = [by_bucket.get(center["bucket"] + delta) for delta in (-1, 0, 1)]
    if any(leg is None for leg in legs):
        return None
    return {
        "event_id": str(event["event_id"]), "city": event["city"],
        "target_date": event["target_date"], "winning_market_id": event["winning_market_id"],
        "winning_range": event["winning_range"], "cutoff_local_minutes": local_minutes,
        "slot_utc": slot, "age_minutes": age_minutes, "center_bucket": center["bucket"],
        "center_midpoint": center["midpoint"], "second_midpoint": second["midpoint"],
        "center_lead": center["midpoint"] - second["midpoint"], "legs": legs,
        "sum_exact_midpoints": sum(item["midpoint"] for item in exact),
    }


def evaluate_weights(
    state: dict[str, Any], weights: tuple[float, float, float],
    fee_rate: float = WEATHER_TAKER_FEE_RATE,
) -> dict[str, Any] | None:
    notional, fees, payout = 0.0, 0.0, 0.0
    leg_results = []
    for leg, shares in zip(state["legs"], weights):
        price, available = vwap(leg["yes_book_json"], shares)
        if price is None or available + 1e-9 < shares:
            return None
        leg_cost = price * shares
        leg_fee = shares * fee_rate * price * (1.0 - price)
        leg_payout = shares if leg["market_id"] == str(state["winning_market_id"] or "") else 0.0
        notional += leg_cost
        fees += leg_fee
        payout += leg_payout
        leg_results.append({
            **leg, "shares": shares, "vwap": price, "notional": leg_cost,
            "fee": leg_fee, "cost": leg_cost + leg_fee, "payout": leg_payout,
        })
    cost = notional + fees
    return {
        "notional": notional, "fees": fees, "cost": cost,
        "payout": payout, "pnl": payout - cost, "legs": leg_results,
    }


def base_eligible(state: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    result = evaluate_weights(state, BASELINE_WEIGHTS)
    if result is None:
        return False, None
    max_spread = max(leg["spread"] for leg in state["legs"])
    eligible = (
        state["age_minutes"] <= 10.0 and state["center_lead"] >= 0.03
        and max_spread <= 0.20
        and result["notional"] > 1.50 and result["notional"] <= 2.00
    )
    return eligible, result


def bootstrap_dates(
    rows: list[dict[str, Any]], value: Callable[[list[dict[str, Any]]], float],
    samples: int = 20000, seed: int = 20260804,
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
        sampled = [item for _date in dates for item in by_date[rng.choice(dates)]]
        values.append(value(sampled))
    values.sort()
    index = lambda quantile: min(len(values) - 1, int(quantile * len(values)))
    return {"p05": values[index(0.05)], "median": values[index(0.50)], "p95": values[index(0.95)]}


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cost = sum(row["cost"] for row in rows)
    notional = sum(row.get("notional", row["cost"]) for row in rows)
    fees = sum(row.get("fees", 0.0) for row in rows)
    pnl = sum(row["pnl"] for row in rows)
    dates = sorted({row["target_date"] for row in rows})
    by_date = defaultdict(float)
    for row in rows:
        by_date[row["target_date"]] += row["pnl"]
    cumulative = peak = max_drawdown = 0.0
    losing_streak = max_losing_streak = 0
    for target_date in sorted(by_date):
        daily_pnl = by_date[target_date]
        cumulative += daily_pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        losing_streak = losing_streak + 1 if daily_pnl < 0 else 0
        max_losing_streak = max(max_losing_streak, losing_streak)
    daily_costs = defaultdict(float)
    for row in rows:
        daily_costs[row["target_date"]] += row["cost"]
    bootstrap = bootstrap_dates(
        rows, lambda sample: sum(row["pnl"] for row in sample) / max(1, len(sample))
    )
    return {
        "events": len(rows), "independent_dates": len(dates), "total_notional": notional,
        "total_fees": fees, "total_cost": cost,
        "total_pnl": pnl, "pnl_per_event": pnl / len(rows) if rows else None,
        "roi": pnl / cost if cost else None,
        "average_event_cost": cost / len(rows) if rows else None,
        "max_event_cost": max((row["cost"] for row in rows), default=None),
        "max_daily_cost": max(daily_costs.values()) if daily_costs else None,
        "profitable_dates": sum(value > 0 for value in by_date.values()),
        "worst_date_pnl": min(by_date.values()) if by_date else None,
        "best_date_pnl": max(by_date.values()) if by_date else None,
        "max_date_drawdown": max_drawdown,
        "max_consecutive_losing_dates": max_losing_streak,
        "date_block_bootstrap_pnl_per_event": bootstrap,
    }


def paired_delta_metrics(
    baseline: list[dict[str, Any]], alternative: list[dict[str, Any]],
) -> dict[str, Any]:
    baseline_by_event = {row["event_id"]: row for row in baseline}
    deltas = []
    for row in alternative:
        prior = baseline_by_event.get(row["event_id"])
        if prior is None:
            continue
        deltas.append({
            "target_date": row["target_date"], "pnl": row["pnl"] - prior["pnl"],
        })
    bootstrap = bootstrap_dates(
        deltas, lambda sample: sum(row["pnl"] for row in sample) / max(1, len(sample))
    )
    return {
        "events": len(deltas), "independent_dates": len({row["target_date"] for row in deltas}),
        "total_pnl_improvement": sum(row["pnl"] for row in deltas),
        "pnl_improvement_per_event": (
            sum(row["pnl"] for row in deltas) / len(deltas) if deltas else None
        ),
        "date_block_bootstrap_improvement_per_event": bootstrap,
    }


def white_reality_check(
    daily_rule_values: dict[str, dict[str, float]], samples: int = 50000,
    seed: int = 20260804,
) -> dict[str, Any]:
    dates = sorted({day for values in daily_rule_values.values() for day in values})
    rules = sorted(daily_rule_values)
    if not dates or not rules:
        return {"rules": len(rules), "dates": len(dates), "p_value": None}
    matrix = {
        rule: [float(daily_rule_values[rule].get(day, 0.0)) for day in dates]
        for rule in rules
    }
    means = {rule: statistics.fmean(values) for rule, values in matrix.items()}
    best_rule = max(rules, key=lambda rule: means[rule])
    observed = means[best_rule]
    centered = {
        rule: [value - means[rule] for value in values]
        for rule, values in matrix.items()
    }
    rng = random.Random(seed)
    exceed = 0
    for _ in range(samples):
        indices = [rng.randrange(len(dates)) for _day in dates]
        bootstrap_max = max(
            statistics.fmean(centered[rule][index] for index in indices)
            for rule in rules
        )
        exceed += bootstrap_max >= observed - 1e-12
    return {
        "rules": len(rules), "dates": len(dates), "best_rule": best_rule,
        "best_mean_daily_value": observed,
        "p_value": (exceed + 1) / (samples + 1), "bootstrap_samples": samples,
        "missing_rule_dates_are_zero_no_trade": True,
    }


def weather_features(db: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
    row = db.execute(
        """
        SELECT observed_temperature_c,observed_daily_max_c,weather_state_json,forecast_state_json
        FROM weather_ai_research_snapshots
        WHERE event_id=? AND sample_slot_utc<=?
        ORDER BY sample_slot_utc DESC LIMIT 1
        """,
        (state["event_id"], state["slot_utc"]),
    ).fetchone()
    if not row:
        return {}
    weather = parse_json(row["weather_state_json"], {})
    forecast = parse_json(row["forecast_state_json"], {})
    process = weather.get("process") or {}
    trend = process.get("primaryStationTrend") or {}
    remote = process.get("remoteSensing") or {}
    radar = ((remote.get("rainviewer") or {}).get("features") or {})
    solar = process.get("solarHeating") or {}
    comparison = process.get("modelRealityComparison") or {}
    ensemble = forecast.get("ecmwfEnsemble") or {}
    return {
        "observed_temperature_c": as_float(row["observed_temperature_c"]),
        "observed_daily_max_c": as_float(row["observed_daily_max_c"]),
        "temperature_trend_c_per_hour": as_float(trend.get("temperatureTrendCPerHour")),
        "radar_coverage_25km": as_float(radar.get("echoCoverage25Km")),
        "jaxa_to_clear_sky_ratio": as_float(solar.get("jaxaToClearSkyRatio")),
        "ecmwf_same_hour_error_c": as_float(
            (comparison.get("ecmwf") or {}).get("observationMinusSameHourForecastC")
        ),
        "ensemble_mean_max_c": as_float(ensemble.get("meanMaxC")),
        "ensemble_std_max_c": as_float(ensemble.get("stdMaxC")),
    }


def grouped_metrics(
    rows: list[dict[str, Any]], groups: dict[str, Callable[[dict[str, Any]], bool]],
) -> dict[str, Any]:
    output = {}
    for name, predicate in groups.items():
        selected = [row for row in rows if predicate(row)]
        if selected:
            output[name] = metrics(selected)
    return output


def select_one_per_date(
    rows: list[dict[str, Any]], selector: str, max_cost: float = 15.0,
) -> list[dict[str, Any]]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["cost"] <= max_cost + 1e-9:
            by_date[row["target_date"]].append(row)
    keys: dict[str, Callable[[dict[str, Any]], tuple[Any, ...]]] = {
        "lowest_cost": lambda row: (row["cost"], row["city"]),
        "highest_center_midpoint": lambda row: (-row["center_midpoint"], row["city"]),
        "lowest_center_midpoint": lambda row: (row["center_midpoint"], row["city"]),
        "highest_center_lead": lambda row: (-row["center_lead"], row["city"]),
        "lowest_center_lead": lambda row: (row["center_lead"], row["city"]),
        "lowest_max_spread": lambda row: (
            max(leg["spread"] for leg in row["legs"]), row["city"]
        ),
    }
    key = keys[selector]
    return [min(by_date[day], key=key) for day in sorted(by_date) if by_date[day]]


def strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value).replace("&amp;lt;", "<").replace("&amp;gt;", ">")


def run(database: Path) -> dict[str, Any]:
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

    cutoff_results = {}
    states_by_cutoff: dict[int, list[dict[str, Any]]] = {}
    for local_minutes in (630, 645, 655, 660, 665, 670, 675, 690):
        states = []
        for event in events:
            state = event_state(db, event, local_minutes)
            if state is None:
                continue
            eligible, baseline = base_eligible(state)
            if eligible and baseline is not None:
                states.append({**state, **baseline})
        states_by_cutoff[local_minutes] = states
        cutoff_results[f"{local_minutes // 60:02d}:{local_minutes % 60:02d}"] = metrics(states)

    baseline_states = states_by_cutoff[660]
    structure_results = {}
    structure_rows: dict[str, list[dict[str, Any]]] = {}
    for name, weights in WEIGHT_STRUCTURES.items():
        rows = []
        for state in baseline_states:
            result = evaluate_weights(state, weights)
            if result is not None:
                rows.append({**state, **result})
        structure_rows[name] = rows
        structure_results[name] = {**metrics(rows), "weights": weights}

    executable_structure_results = {}
    executable_structure_rows: dict[str, list[dict[str, Any]]] = {}
    for name, weights in EXECUTABLE_WEIGHT_STRUCTURES.items():
        rows = []
        for state in baseline_states:
            result = evaluate_weights(state, weights)
            if result is not None:
                rows.append({**state, **result})
        executable_structure_rows[name] = rows
        executable_structure_results[name] = {
            **metrics(rows), "weights": weights,
            "baseline_candidate_events": len(baseline_states),
            "depth_complete_fraction": len(rows) / len(baseline_states) if baseline_states else None,
            "missing_depth_events": len(baseline_states) - len(rows),
        }
    executable_baseline = executable_structure_rows["5/15/5"]
    for name, rows in executable_structure_rows.items():
        common_ids = {row["event_id"] for row in executable_baseline} & {
            row["event_id"] for row in rows
        }
        common_baseline = [row for row in executable_baseline if row["event_id"] in common_ids]
        common_rows = [row for row in rows if row["event_id"] in common_ids]
        executable_structure_results[name]["paired_delta_vs_5_15_5"] = paired_delta_metrics(
            common_baseline, common_rows
        )

    baseline = structure_rows["1/3/1"]
    for name, rows in structure_rows.items():
        structure_results[name]["paired_delta_vs_1_3_1"] = paired_delta_metrics(baseline, rows)

    baseline_by_event = {row["event_id"]: row for row in baseline}
    daily_weight_deltas: dict[str, dict[str, float]] = {}
    for name, rows in structure_rows.items():
        if name == "1/3/1":
            continue
        grouped: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            prior = baseline_by_event[row["event_id"]]
            grouped[row["target_date"]].append(row["pnl"] - prior["pnl"])
        daily_weight_deltas[name] = {
            day: statistics.fmean(values) for day, values in grouped.items()
        }
    weight_search_reality_check = white_reality_check(daily_weight_deltas)

    cutoff_structure_stability = {}
    for local_minutes, states in states_by_cutoff.items():
        label = f"{local_minutes // 60:02d}:{local_minutes % 60:02d}"
        cutoff_structure_stability[label] = {}
        for name in ("1/3/1", "0.5/4/0.5", "0.25/4.5/0.25"):
            rows = []
            for state in states:
                result = evaluate_weights(state, WEIGHT_STRUCTURES[name])
                if result is not None:
                    rows.append({**state, **result})
            cutoff_structure_stability[label][name] = metrics(rows)

    total_five_structures = {
        name for name, weights in WEIGHT_STRUCTURES.items() if abs(sum(weights) - 5.0) < 1e-9
    }
    cutoff_weight_daily: dict[str, dict[str, float]] = {}
    all_dates = sorted({event["target_date"] for event in events})
    for local_minutes, states in states_by_cutoff.items():
        label = f"{local_minutes // 60:02d}:{local_minutes % 60:02d}"
        for name in sorted(total_five_structures):
            daily = defaultdict(float)
            for state in states:
                result = evaluate_weights(state, WEIGHT_STRUCTURES[name])
                if result is not None:
                    daily[state["target_date"]] += result["pnl"]
            cutoff_weight_daily[f"{label}|{name}"] = {
                day: float(daily.get(day, 0.0)) for day in all_dates
            }
    cutoff_weight_reality_check = white_reality_check(cutoff_weight_daily)
    leg_attribution = {}
    for index, name in enumerate(("lower", "center", "upper")):
        cost = sum(row["legs"][index]["cost"] for row in baseline)
        payout = sum(row["legs"][index]["payout"] for row in baseline)
        leg_attribution[name] = {
            "cost": cost, "payout": payout, "pnl": payout - cost,
            "roi": (payout - cost) / cost if cost else None,
            "wins": sum(row["legs"][index]["payout"] > 0 for row in baseline),
        }

    for row in baseline:
        row.update(weather_features(db, row))
        row["normalized_center_midpoint"] = (
            row["center_midpoint"] / row["sum_exact_midpoints"]
            if row["sum_exact_midpoints"] else None
        )
        observed_max = row.get("observed_daily_max_c")
        row["center_minus_observed_max"] = (
            row["center_bucket"] - observed_max if observed_max is not None else None
        )
        ensemble_mean = row.get("ensemble_mean_max_c")
        row["ensemble_mean_minus_center"] = (
            ensemble_mean - row["center_bucket"] if ensemble_mean is not None else None
        )

    price_groups = grouped_metrics(baseline, {
        "center_mid_lt_0.40": lambda row: row["center_midpoint"] < 0.40,
        "center_mid_0.40_0.55": lambda row: 0.40 <= row["center_midpoint"] < 0.55,
        "center_mid_ge_0.55": lambda row: row["center_midpoint"] >= 0.55,
        "gap_0.03_0.08": lambda row: 0.03 <= row["center_lead"] < 0.08,
        "gap_0.08_0.15": lambda row: 0.08 <= row["center_lead"] < 0.15,
        "gap_ge_0.15": lambda row: row["center_lead"] >= 0.15,
        "cost_1.50_1.70": lambda row: 1.50 < row["cost"] <= 1.70,
        "cost_1.70_1.85": lambda row: 1.70 < row["cost"] <= 1.85,
        "cost_1.85_2.00": lambda row: 1.85 < row["cost"] <= 2.00,
    })
    weather_rows = [row for row in baseline if row.get("observed_daily_max_c") is not None]
    weather_groups = grouped_metrics(weather_rows, {
        "observed_max_at_center": lambda row: row["center_minus_observed_max"] <= 0,
        "observed_max_one_below": lambda row: 0 < row["center_minus_observed_max"] <= 1,
        "observed_max_two_or_more_below": lambda row: row["center_minus_observed_max"] > 1,
        "trend_positive": lambda row: (row.get("temperature_trend_c_per_hour") or 0) > 0,
        "trend_nonpositive": lambda row: (row.get("temperature_trend_c_per_hour") or 0) <= 0,
        "ensemble_std_le_0.8": lambda row: row.get("ensemble_std_max_c") is not None and row["ensemble_std_max_c"] <= 0.8,
        "ensemble_std_gt_0.8": lambda row: row.get("ensemble_std_max_c") is not None and row["ensemble_std_max_c"] > 0.8,
        "ensemble_center_distance_le_1": lambda row: row.get("ensemble_mean_minus_center") is not None and abs(row["ensemble_mean_minus_center"]) <= 1,
        "ensemble_center_distance_gt_1": lambda row: row.get("ensemble_mean_minus_center") is not None and abs(row["ensemble_mean_minus_center"]) > 1,
    })
    best_rows = structure_rows["0.25/4.5/0.25"]
    best_by_city = {
        city: metrics([row for row in best_rows if row["city"] == city])
        for city in sorted({row["city"] for row in best_rows})
    }
    best_by_date = {}
    for target_date in sorted({row["target_date"] for row in best_rows}):
        selected = [row for row in best_rows if row["target_date"] == target_date]
        best_by_date[target_date] = {
            "events": len(selected), "pnl": sum(row["pnl"] for row in selected),
            "cost": sum(row["cost"] for row in selected),
        }
    executable_by_city = {}
    for structure_name, rows in executable_structure_rows.items():
        executable_by_city[structure_name] = {
            city: metrics([row for row in rows if row["city"] == city])
            for city in sorted({row["city"] for row in rows})
        }
    one_per_date_portfolios = {}
    portfolio_daily_returns: dict[str, dict[str, float]] = {}
    for structure_name, rows in executable_structure_rows.items():
        for selector in (
            "lowest_cost", "highest_center_midpoint", "lowest_center_midpoint",
            "highest_center_lead", "lowest_center_lead", "lowest_max_spread",
        ):
            selected = select_one_per_date(rows, selector, max_cost=15.0)
            rule_name = f"{structure_name}|{selector}"
            one_per_date_portfolios[rule_name] = {
                **metrics(selected), "structure": structure_name, "selector": selector,
                "max_cost_per_event": 15.0, "max_events_per_date": 1,
            }
            portfolio_daily_returns[rule_name] = {
                row["target_date"]: row["pnl"] / row["cost"] for row in selected if row["cost"]
            }
    portfolio_reality_check = white_reality_check(portfolio_daily_returns)
    focus_portfolio_name = "5/20/5|lowest_center_lead"
    focus_portfolio_rows = select_one_per_date(
        executable_structure_rows["5/20/5"], "lowest_center_lead", max_cost=15.0
    )
    for row in focus_portfolio_rows:
        row.update(weather_features(db, row))
        observed_max = row.get("observed_daily_max_c")
        row["center_minus_observed_max"] = (
            row["center_bucket"] - observed_max if observed_max is not None else None
        )
    focus_leg_attribution = {}
    for index, leg_name in enumerate(("lower", "center", "upper")):
        leg_cost = sum(row["legs"][index]["cost"] for row in focus_portfolio_rows)
        leg_payout = sum(row["legs"][index]["payout"] for row in focus_portfolio_rows)
        focus_leg_attribution[leg_name] = {
            "cost": leg_cost, "payout": leg_payout, "pnl": leg_payout - leg_cost,
            "wins": sum(row["legs"][index]["payout"] > 0 for row in focus_portfolio_rows),
        }
    focus_by_city = {
        city: metrics([row for row in focus_portfolio_rows if row["city"] == city])
        for city in sorted({row["city"] for row in focus_portfolio_rows})
    }
    focus_weather_rows = [
        row for row in focus_portfolio_rows if row.get("observed_daily_max_c") is not None
    ]
    focus_weather_groups = grouped_metrics(focus_weather_rows, {
        "center_at_most_one_above_observed_max": lambda row: row["center_minus_observed_max"] <= 1,
        "center_at_least_two_above_observed_max": lambda row: row["center_minus_observed_max"] >= 2,
        "positive_temperature_trend": lambda row: (row.get("temperature_trend_c_per_hour") or 0) > 0,
        "nonpositive_temperature_trend": lambda row: (row.get("temperature_trend_c_per_hour") or 0) <= 0,
    })
    focus_cutoff_stability = {}
    for local_minutes, states in states_by_cutoff.items():
        rows = []
        for state in states:
            result = evaluate_weights(state, EXECUTABLE_WEIGHT_STRUCTURES["5/20/5"])
            if result is not None:
                rows.append({**state, **result})
        selected = select_one_per_date(rows, "lowest_center_lead", max_cost=15.0)
        focus_cutoff_stability[f"{local_minutes // 60:02d}:{local_minutes % 60:02d}"] = metrics(selected)
    focus_audit = [{
        "target_date": row["target_date"], "city": row["city"],
        "event_id": row["event_id"], "center_bucket": row["center_bucket"],
        "center_midpoint": row["center_midpoint"], "center_lead": row["center_lead"],
        "cost": row["cost"], "payout": row["payout"], "pnl": row["pnl"],
        "winning_range": row["winning_range"],
    } for row in focus_portfolio_rows]
    candidate_audit = [{
        "target_date": row["target_date"], "city": row["city"],
        "event_id": row["event_id"], "slot_utc": row["slot_utc"],
        "center_bucket": row["center_bucket"], "center_midpoint": row["center_midpoint"],
        "center_lead": row["center_lead"], "baseline_cost": row["cost"],
        "baseline_payout": row["payout"], "baseline_pnl": row["pnl"],
    } for row in baseline]
    db.close()
    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "method": {
            "scope": sorted(ALLOWED_CITIES),
            "independence_unit": "target_date",
            "execution": "full YES ask-book VWAP at last snapshot at or before cutoff",
            "taker_fee": {
                "category": "Weather", "fee_rate": WEATHER_TAKER_FEE_RATE,
                "formula": "shares * fee_rate * price * (1-price)",
                "source": "https://docs.polymarket.com/trading/fees.md",
            },
            "baseline_rule": {
                "cutoff_local": "11:00", "weights": BASELINE_WEIGHTS,
                "center_lead_min": 0.03, "combined_cost": "(1.50,2.00]",
                "max_leg_spread": 0.20, "max_snapshot_age_minutes": 10,
            },
            "weight_interpretation": (
                "Normalized shadow units. They are not three directly executable orders: "
                "the live China weather token checked on 2026-08-04 reported min_order_size=5 shares."
            ),
            "clob_reference": "https://docs.polymarket.com/developers/CLOB/orders/create-order.md",
            "warning": "All alternatives are exploratory historical comparisons; only future frozen dates are out of sample.",
        },
        "cutoff_stability": cutoff_results,
        "cutoff_structure_stability": cutoff_structure_stability,
        "weight_structures_same_baseline_candidates": structure_results,
        "executable_min_5_share_structures": executable_structure_results,
        "executable_structures_by_city": executable_by_city,
        "one_per_date_cap_15_portfolios": one_per_date_portfolios,
        "one_per_date_portfolio_return_reality_check": portfolio_reality_check,
        "focus_executable_portfolio": {
            "rule": focus_portfolio_name, "metrics": metrics(focus_portfolio_rows),
            "leg_attribution": focus_leg_attribution, "by_city": focus_by_city,
            "weather_aligned_events": len(focus_weather_rows),
            "weather_aligned_dates": len({row["target_date"] for row in focus_weather_rows}),
            "weather_groups": focus_weather_groups,
            "cutoff_stability": focus_cutoff_stability, "candidate_audit": focus_audit,
        },
        "weight_search_reality_check": weight_search_reality_check,
        "cutoff_and_weight_reality_check": cutoff_weight_reality_check,
        "baseline_leg_attribution": leg_attribution,
        "baseline_price_groups": price_groups,
        "weather_gate_groups": weather_groups,
        "weather_aligned_events": len(weather_rows),
        "weather_aligned_dates": len({row["target_date"] for row in weather_rows}),
        "best_structure_by_city": best_by_city,
        "best_structure_by_date": best_by_date,
        "baseline_candidate_audit": candidate_audit,
    }


def format_pct(value: Any) -> str:
    return "NA" if value is None else f"{100 * float(value):.1f}%"


def format_num(value: Any) -> str:
    return "NA" if value is None else f"{float(value):+.3f}"


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 连续三桶梯度：盘口微结构与天气门控研究",
        "",
        "> 全部结果均为历史探索，独立单位为目标日期；不得据此转入实盘。",
        "",
        "## 截点稳定性",
        "",
        "| 截点 | 日期 | 事件 | ROI | PnL/事件 | 5%下界 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for cutoff, item in report["cutoff_stability"].items():
        lower = item["date_block_bootstrap_pnl_per_event"]["p05"]
        lines.append(
            f"| {cutoff} | {item['independent_dates']} | {item['events']} | "
            f"{format_pct(item['roi'])} | {format_num(item['pnl_per_event'])} | {format_num(lower)} |"
        )
    lines += [
        "", "## 相同候选集的仓位结构", "",
        "| 权重 | 日期 | 事件 | ROI | 总PnL | PnL/事件 | 5%下界 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in report["weight_structures_same_baseline_candidates"].items():
        lower = item["date_block_bootstrap_pnl_per_event"]["p05"]
        delta_lower = item["paired_delta_vs_1_3_1"][
            "date_block_bootstrap_improvement_per_event"
        ]["p05"]
        lines.append(
            f"| {name} | {item['independent_dates']} | {item['events']} | {format_pct(item['roi'])} | "
            f"{format_num(item['total_pnl'])} | {format_num(item['pnl_per_event'])} | "
            f"{format_num(lower)} (配对改善下界 {format_num(delta_lower)}) |"
        )
    lines += [
        "", "## 每腿至少5 shares的可执行结构", "",
        "| 权重 | 深度事件/基准 | 日期 | ROI | PnL/事件 | 平均成本 | 最大日成本 | 5%下界 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in report["executable_min_5_share_structures"].items():
        lower = item["date_block_bootstrap_pnl_per_event"]["p05"]
        lines.append(
            f"| {name} | {item['events']}/{item['baseline_candidate_events']} | "
            f"{item['independent_dates']} | {format_pct(item['roi'])} | "
            f"{format_num(item['pnl_per_event'])} | {item['average_event_cost']:.3f} | "
            f"{item['max_daily_cost']:.3f} | {format_num(lower)} |"
        )
    focus = report["focus_executable_portfolio"]
    focus_metrics = focus["metrics"]
    lines += [
        "", "## 20 USDC本金下的单日一城候选", "",
        f"- 规则：`{focus['rule']}`。",
        f"- {focus_metrics['independent_dates']}个独立日期、{focus_metrics['events']}个事件，"
        f"PnL {focus_metrics['total_pnl']:+.3f}，ROI {format_pct(focus_metrics['roi'])}。",
        f"- 平均成本 {focus_metrics['average_event_cost']:.3f}，最大单笔成本 "
        f"{focus_metrics['max_event_cost']:.3f}，最大日期回撤 {focus_metrics['max_date_drawdown']:.3f}。",
        f"- 日期块5%下界 {focus_metrics['date_block_bootstrap_pnl_per_event']['p05']:+.3f}/日；"
        f"30条组合规则Reality Check p={report['one_per_date_portfolio_return_reality_check']['p_value']:.4f}。",
        "- 该规则在历史15天恰好11次中心、4次上侧、0次下侧，三桶覆盖15/15；"
        "这一结果过强，必须视为高过拟合风险。",
    ]
    lines += ["", "## 基准1/3/1逐腿归因", "", "| 腿 | 成本 | Payout | PnL | ROI | 命中 |", "|---|---:|---:|---:|---:|---:|"]
    for name, item in report["baseline_leg_attribution"].items():
        lines.append(
            f"| {name} | {item['cost']:.3f} | {item['payout']:.3f} | {item['pnl']:+.3f} | "
            f"{format_pct(item['roi'])} | {item['wins']} |"
        )
    lines += [
        "", "## 样本边界", "",
        f"- 可对齐天气特征：{report['weather_aligned_events']}个事件，"
        f"{report['weather_aligned_dates']}个独立日期。",
        "- 价格分组和天气分组详见JSON；小组样本不能解释为稳定规则。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("data/weather_market_monitor.sqlite3"))
    parser.add_argument("--output-json", type=Path, default=Path("research/output/weather_ladder_microstructure_report.json"))
    parser.add_argument("--output-md", type=Path, default=Path("research/output/weather_ladder_microstructure_report.md"))
    args = parser.parse_args()
    report = run(args.database)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output_md.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({
        "json": str(args.output_json), "markdown": str(args.output_md),
        "baseline": report["weight_structures_same_baseline_candidates"]["1/3/1"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
