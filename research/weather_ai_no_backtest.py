#!/usr/bin/env python3
"""Point-in-time Hermes backtest where AI independently chooses NO trades."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
SCHEMA_PATH = ROOT / "weather_ai_no_backtest.schema.json"
CITY_SCHEMA_PATH = ROOT / "weather_ai_no_city_analysis.schema.json"
DEFAULT_CONFIG_PATH = ROOT / "weather_ai_no_backtest_config.json"


def parse_utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def iso_utc(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat(timespec="seconds")


def finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def json_value(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str):
        return value if value is not None else fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def minutes_label(value: int) -> str:
    hours, minutes = divmod(int(value), 60)
    return f"{hours:02d}:{minutes:02d}"


def local_review_time(target_date: str, minutes: int, timezone_name: str) -> datetime:
    target = date.fromisoformat(target_date)
    local = datetime.combine(target, time.min, tzinfo=ZoneInfo(timezone_name))
    return (local + timedelta(minutes=minutes)).astimezone(UTC)


def ensure_isolated_hermes_profile(config: dict[str, Any]) -> tuple[Path, Path]:
    source = Path(str(config.get("hermesCredentialSourceHome") or "")).expanduser()
    target = Path(str(config.get("hermesHome") or "")).expanduser()
    if not source.is_dir():
        raise RuntimeError(f"Hermes credential source profile is unavailable: {source}")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    links = {
        "config.yaml": ROOT / "hermes_backtest_config.yaml",
        "SOUL.md": ROOT / "hermes_backtest_soul.md",
        ".env": source / ".env",
        "auth.json": source / "auth.json",
    }
    for name, source_path in links.items():
        if not source_path.exists():
            continue
        destination = target / name
        if destination.is_symlink() and destination.resolve() == source_path.resolve():
            continue
        if destination.exists() or destination.is_symlink():
            raise RuntimeError(
                f"Isolated Hermes profile contains an unmanaged file: {destination}"
            )
        destination.symlink_to(source_path)
    for generated_directory in (target / "memories", target / "skills"):
        if not generated_directory.exists():
            continue
        nonempty_files = [
            path for path in generated_directory.rglob("*")
            if path.is_file() and path.stat().st_size > 0
        ]
        if nonempty_files:
            raise RuntimeError(
                "Isolated Hermes profile contains non-empty memory or skills: "
                + ", ".join(str(path) for path in nonempty_files[:5])
            )
    workspace = target / "workspace"
    workspace.mkdir(exist_ok=True)
    return target, workspace


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "No data available at this point in time."

    def cell(value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, float):
            value = round(value, 4)
        return str(value).replace("|", "\\|").replace("\n", " ")

    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    output.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return "\n".join(output)


def latest_resolved_dates(
    db: sqlite3.Connection, cities: list[str], count: int,
) -> list[str]:
    placeholders = ",".join("?" for _ in cities)
    rows = db.execute(
        f"""SELECT e.target_date
            FROM events e
            WHERE e.city IN ({placeholders})
            GROUP BY e.target_date
            HAVING COUNT(DISTINCT e.city)=?
               AND COUNT(DISTINCT CASE WHEN e.winning_market_id IS NOT NULL THEN e.city END)=?
               AND EXISTS(
                   SELECT 1 FROM market_snapshots ms
                   WHERE ms.event_id IN (
                       SELECT e2.event_id FROM events e2
                       WHERE e2.target_date=e.target_date AND e2.city IN ({placeholders})
                   )
               )
            ORDER BY e.target_date DESC
            LIMIT ?""",
        (*cities, len(cities), len(cities), *cities, int(count)),
    ).fetchall()
    return sorted(str(row[0]) for row in rows)


def events_for_date(
    db: sqlite3.Connection, target_date: str, cities: list[str], *, include_settlement: bool,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in cities)
    settlement_fields = ",e.winning_market_id,e.winning_range,e.resolved_at_utc" if include_settlement else ""
    rows = db.execute(
        f"""SELECT e.event_id,e.city,e.target_date,e.station_id,e.station_name,
                   s.timezone,e.end_date_utc{settlement_fields}
            FROM events e JOIN stations s ON s.station_id=e.station_id
            WHERE e.target_date=? AND e.city IN ({placeholders})
            ORDER BY e.city""",
        (target_date, *cities),
    ).fetchall()
    return [dict(row) for row in rows]


def _observation_rows(
    db: sqlite3.Connection, event: dict[str, Any], as_of: datetime, limit: int,
) -> list[dict[str, Any]]:
    rows = db.execute(
        """SELECT observation_time_utc,temperature_c,observed_daily_max_c,dewpoint_c,
                  relative_humidity,precipitation_mm,cloud_cover_pct,wind_direction_deg,
                  wind_speed,wind_speed_unit,wind_gust,visibility_m,pressure_hpa,
                  solar_radiation_wm2,sky_conditions_json,metar_type
           FROM (
               SELECT w.*,ROW_NUMBER() OVER(
                   PARTITION BY observation_time_utc
                   ORDER BY fetched_at_utc DESC,run_id DESC
               ) AS row_number
               FROM weather_observations w
               WHERE station_id=? AND sample_local_date=? AND source='metar' AND status='ok'
                 AND observation_time_utc<=? AND fetched_at_utc<=?
           )
           WHERE row_number=1
           ORDER BY observation_time_utc DESC LIMIT ?""",
        (
            event["station_id"], event["target_date"], iso_utc(as_of),
            iso_utc(as_of), max(1, int(limit)),
        ),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def _auxiliary_observation(
    db: sqlite3.Connection, event: dict[str, Any], as_of: datetime,
) -> dict[str, Any] | None:
    row = db.execute(
        """SELECT observation_time_utc,temperature_c,observed_daily_max_c,dewpoint_c,
                  relative_humidity,precipitation_mm,cloud_cover_pct,wind_direction_deg,
                  wind_speed,wind_speed_unit,wind_gust,visibility_m,pressure_hpa,
                  solar_radiation_wm2,weather_code
           FROM weather_observations
           WHERE station_id=? AND sample_local_date=? AND source='open_meteo_current'
             AND status='ok' AND observation_time_utc<=? AND fetched_at_utc<=?
           ORDER BY observation_time_utc DESC,fetched_at_utc DESC,run_id DESC LIMIT 1""",
        (event["station_id"], event["target_date"], iso_utc(as_of), iso_utc(as_of)),
    ).fetchone()
    return dict(row) if row else None


def _forecast_rows(
    db: sqlite3.Connection, event: dict[str, Any], as_of: datetime, per_model: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    sources = (
        (
            "external_forecasts",
            "SELECT model,slot_utc,fetched_at_utc,forecast_max_c,forecast_peak_local "
            "FROM external_forecasts WHERE station_id=? AND target_date=? AND status='ok' "
            "AND slot_utc<=? AND fetched_at_utc<=? ORDER BY slot_utc DESC,run_id DESC",
        ),
        (
            "windy_forecasts",
            "SELECT model,slot_utc,fetched_at_utc,forecast_max_c,forecast_peak_local "
            "FROM windy_forecasts WHERE station_id=? AND target_date=? AND status='ok' "
            "AND slot_utc<=? AND fetched_at_utc<=? ORDER BY slot_utc DESC,run_id DESC",
        ),
    )
    for source, sql in sources:
        counts: dict[str, int] = {}
        for row in db.execute(
            sql,
            (event["station_id"], event["target_date"], iso_utc(as_of), iso_utc(as_of)),
        ):
            model = str(row["model"] or "unknown")
            if counts.get(model, 0) >= per_model:
                continue
            counts[model] = counts.get(model, 0) + 1
            result.append({"source": source, **dict(row)})

    ensemble = db.execute(
        """SELECT model,slot_utc,fetched_at_utc,member_count,mean_max_c,std_max_c,
                  min_max_c,max_max_c,q10_max_c,q50_max_c,q90_max_c
           FROM ensemble_forecasts
           WHERE station_id=? AND target_date=? AND status='ok'
             AND slot_utc<=? AND fetched_at_utc<=?
           ORDER BY slot_utc DESC,run_id DESC""",
        (event["station_id"], event["target_date"], iso_utc(as_of), iso_utc(as_of)),
    ).fetchall()
    seen_ensemble: set[str] = set()
    for row in ensemble:
        model = str(row["model"] or "unknown")
        if model in seen_ensemble:
            continue
        seen_ensemble.add(model)
        result.append({"source": "ensemble_forecasts", **dict(row)})
    return sorted(result, key=lambda row: (str(row.get("source")), str(row.get("model")), str(row.get("slot_utc"))))


def _reference_no_ask(
    db: sqlite3.Connection, market_id: str, before: datetime,
) -> float | None:
    row = db.execute(
        """SELECT no_best_ask FROM market_snapshots
           WHERE market_id=? AND slot_utc<=? AND fetched_at_utc<=? AND no_best_ask IS NOT NULL
           ORDER BY slot_utc DESC,fetched_at_utc DESC,run_id DESC LIMIT 1""",
        (market_id, iso_utc(before), iso_utc(before)),
    ).fetchone()
    return finite(row[0]) if row else None


def _current_markets(
    db: sqlite3.Connection, event_ids: list[str], as_of: datetime,
) -> list[dict[str, Any]]:
    if not event_ids:
        return []
    placeholders = ",".join("?" for _ in event_ids)
    rows = db.execute(
        f"""SELECT market_id,event_id,outcome_range,bucket_low,bucket_high,bucket_unit,
                   slot_utc,fetched_at_utc,yes_best_bid,yes_best_ask,no_best_bid,no_best_ask,
                   no_ask_size,no_book_json,market_liquidity
            FROM (
                SELECT m.market_id,m.event_id,m.outcome_range,m.bucket_low,m.bucket_high,m.bucket_unit,
                       ms.slot_utc,ms.fetched_at_utc,ms.yes_best_bid,ms.yes_best_ask,
                       ms.no_best_bid,ms.no_best_ask,ms.no_ask_size,ms.no_book_json,
                       ms.market_liquidity,ROW_NUMBER() OVER(
                           PARTITION BY ms.market_id
                           ORDER BY ms.slot_utc DESC,ms.fetched_at_utc DESC,ms.run_id DESC
                       ) AS row_number
                FROM markets m JOIN market_snapshots ms ON ms.market_id=m.market_id
                WHERE m.event_id IN ({placeholders})
                  AND ms.slot_utc<=? AND ms.fetched_at_utc<=?
            ) WHERE row_number=1 ORDER BY event_id,bucket_low,outcome_range""",
        (*event_ids, iso_utc(as_of), iso_utc(as_of)),
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["noAsk30mAgo"] = _reference_no_ask(db, str(row["market_id"]), as_of - timedelta(minutes=30))
        item["noAsk60mAgo"] = _reference_no_ask(db, str(row["market_id"]), as_of - timedelta(minutes=60))
        output.append(item)
    return output


def build_replay_packet(
    db: sqlite3.Connection,
    target_date: str,
    as_of: datetime,
    cities: list[str],
    account: dict[str, Any],
    history: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build an AI-visible packet and an internal executable-market lookup."""
    events = events_for_date(db, target_date, cities, include_settlement=False)
    event_by_id = {str(event["event_id"]): event for event in events}
    markets = _current_markets(db, list(event_by_id), as_of)
    market_lookup: dict[str, dict[str, Any]] = {}
    visible_markets: dict[str, list[dict[str, Any]]] = {str(event["event_id"]): [] for event in events}
    max_age = float(config.get("marketSnapshotMaxAgeMinutes", 20))
    for market in markets:
        event = event_by_id[str(market["event_id"])]
        fetched = parse_utc(market.get("fetched_at_utc"))
        age = (as_of - fetched).total_seconds() / 60.0 if fetched else None
        internal = {**market, "city": event["city"], "snapshotAgeMinutes": age}
        market_id = str(market["market_id"])
        market_lookup[market_id] = internal
        visible_markets[str(event["event_id"])].append({
            "marketId": market_id,
            "outcomeRange": market["outcome_range"],
            "NO bid": finite(market["no_best_bid"]),
            "NO ask": finite(market["no_best_ask"]),
            "NO ask size": finite(market["no_ask_size"]),
            "NO ask 30m ago": market["noAsk30mAgo"],
            "NO ask 60m ago": market["noAsk60mAgo"],
            "YES bid": finite(market["yes_best_bid"]),
            "YES ask": finite(market["yes_best_ask"]),
            "liquidity": finite(market["market_liquidity"]),
            "snapshot age min": round(age, 2) if age is not None else None,
            "fresh": bool(age is not None and 0 <= age <= max_age),
        })

    city_packets = []
    for event in events:
        observations = _observation_rows(
            db, event, as_of, int(config.get("maxMetarRowsPerCity", 48))
        )
        city_packets.append({
            "eventId": str(event["event_id"]),
            "city": event["city"],
            "stationId": event["station_id"],
            "timezone": event["timezone"],
            "metarTimeline": observations,
            "auxiliaryCurrent": _auxiliary_observation(db, event, as_of),
            "forecastRevisions": _forecast_rows(
                db, event, as_of, int(config.get("maxForecastRevisionsPerModel", 6))
            ),
            "markets": visible_markets[str(event["event_id"])],
        })

    packet = {
        "experiment": {
            "objective": "Maximize final portfolio cash using only NO purchases.",
            "initialCashUsdc": float(config["initialCashUsdc"]),
            "fixedCashPerOrderUsdc": float(config["stakeUsdc"]),
            "weatherTakerFeeRate": float(config.get("weatherTakerFeeRate", 0.05)),
            "sideAllowed": "NO only",
            "AIChooses": "whether, when, city, and exact temperature market",
            "futureDataExcluded": True,
            "currentTradingAIOrRidgeIncluded": False,
        },
        "targetDate": target_date,
        "asOfUtc": iso_utc(as_of),
        "asOfLocal": as_of.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "account": account,
        "settledHistoryAvailableAtAsOf": history,
        "cities": city_packets,
    }
    return packet, market_lookup


def render_packet_markdown(packet: dict[str, Any]) -> str:
    experiment = packet["experiment"]
    account = packet["account"]
    lines = [
        "# AI NO Backtest Point-in-Time Packet",
        "",
        f"- Target date: {packet['targetDate']}",
        f"- As of UTC: {packet['asOfUtc']}",
        f"- As of local: {packet['asOfLocal']}",
        "- Future data excluded: yes",
        "- Existing trading AI, Ridge, and deterministic candidates included: no",
        "",
        "## Objective And Account",
        "",
        f"Objective: {experiment['objective']}",
        f"Allowed side: {experiment['sideAllowed']}",
        f"Fixed total cash per order: {experiment['fixedCashPerOrderUsdc']:.2f} USDC",
        f"Weather taker fee rate parameter: {experiment['weatherTakerFeeRate']:.4f}",
        f"Available cash: {float(account['availableCashUsdc']):.4f} USDC",
        f"Open cash committed: {float(account['openCostUsdc']):.4f} USDC",
        f"Realized PnL: {float(account['realizedPnlUsdc']):.4f} USDC",
        "",
        "The AI may buy any currently listed NO market or buy nothing. It chooses the city, exact bucket, timing, and reasoning. "
        "A displayed market price is evidence, not ground truth. Settlement uses the official recorded daily maximum.",
        "",
    ]
    if packet.get("settledHistoryAvailableAtAsOf"):
        lines.extend(["## Prior Settled Decisions", "", markdown_table(
            ["date", "city", "bucket", "NO won", "PnL"],
            [[row.get("targetDate"), row.get("city"), row.get("outcomeRange"), row.get("noWon"), row.get("realizedPnlUsdc")]
             for row in packet["settledHistoryAvailableAtAsOf"]],
        ), ""])

    for city in packet["cities"]:
        lines.extend([
            f"## {city['city']} ({city['stationId']})",
            "",
            "### METAR timeline",
            "",
            markdown_table(
                ["UTC", "temp C", "daily max C", "dew C", "RH", "rain mm", "cloud %", "wind deg", "wind", "gust", "vis m", "solar", "pressure", "sky"],
                [[row.get("observation_time_utc"), row.get("temperature_c"), row.get("observed_daily_max_c"),
                  row.get("dewpoint_c"), row.get("relative_humidity"), row.get("precipitation_mm"),
                  row.get("cloud_cover_pct"), row.get("wind_direction_deg"),
                  f"{row.get('wind_speed')} {row.get('wind_speed_unit') or ''}".strip(), row.get("wind_gust"),
                  row.get("visibility_m"), row.get("solar_radiation_wm2"), row.get("pressure_hpa"),
                  row.get("sky_conditions_json") or row.get("metar_type")]
                 for row in city["metarTimeline"]],
            ),
            "",
            "### Forecast revisions",
            "",
            markdown_table(
                ["source", "model", "sample UTC", "forecast max C", "peak local", "members", "mean", "std", "q10", "q50", "q90"],
                [[row.get("source"), row.get("model"), row.get("slot_utc"), row.get("forecast_max_c"),
                  row.get("forecast_peak_local"), row.get("member_count"), row.get("mean_max_c"),
                  row.get("std_max_c"), row.get("q10_max_c"), row.get("q50_max_c"), row.get("q90_max_c")]
                 for row in city["forecastRevisions"]],
            ),
            "",
            "### Current executable markets and recent price change",
            "",
            markdown_table(
                ["marketId", "bucket", "NO bid", "NO ask", "NO size", "NO 30m", "NO 60m", "age min", "fresh"],
                [[row.get("marketId"), row.get("outcomeRange"), row.get("NO bid"), row.get("NO ask"),
                  row.get("NO ask size"), row.get("NO ask 30m ago"), row.get("NO ask 60m ago"),
                  row.get("snapshot age min"), row.get("fresh")]
                 for row in city["markets"]],
            ),
            "",
        ])
        auxiliary = city.get("auxiliaryCurrent")
        if auxiliary:
            lines.extend([
                "Auxiliary current observation (not settlement source): "
                + json.dumps({
                    "UTC": auxiliary.get("observation_time_utc"),
                    "tempC": auxiliary.get("temperature_c"),
                    "dailyMaxC": auxiliary.get("observed_daily_max_c"),
                    "rh": auxiliary.get("relative_humidity"),
                    "rainMm": auxiliary.get("precipitation_mm"),
                    "cloudPct": auxiliary.get("cloud_cover_pct"),
                    "windDeg": auxiliary.get("wind_direction_deg"),
                    "wind": auxiliary.get("wind_speed"),
                    "solar": auxiliary.get("solar_radiation_wm2"),
                }, ensure_ascii=False, separators=(",", ":")),
                "",
            ])
    return "\n".join(lines)


def compact_packet_for_ai(packet: dict[str, Any]) -> dict[str, Any]:
    """Remove repeated labels without removing point-in-time evidence."""
    cities = []
    for city in packet["cities"]:
        observations = [
            [
                row.get("observation_time_utc"), row.get("temperature_c"),
                row.get("observed_daily_max_c"), row.get("dewpoint_c"),
                row.get("relative_humidity"), row.get("precipitation_mm"),
                row.get("cloud_cover_pct"), row.get("wind_direction_deg"),
                row.get("wind_speed"), row.get("wind_speed_unit"), row.get("wind_gust"),
                row.get("visibility_m"), row.get("solar_radiation_wm2"),
                row.get("pressure_hpa"), row.get("sky_conditions_json"),
            ]
            for row in city["metarTimeline"]
        ]
        forecasts = [
            [
                row.get("source"), row.get("model"), row.get("slot_utc"),
                row.get("forecast_max_c"), row.get("forecast_peak_local"),
                row.get("member_count"), row.get("mean_max_c"), row.get("std_max_c"),
                row.get("min_max_c"), row.get("max_max_c"), row.get("q10_max_c"),
                row.get("q50_max_c"), row.get("q90_max_c"),
            ]
            for row in city["forecastRevisions"]
        ]
        markets = [
            [
                row.get("marketId"), row.get("outcomeRange"), row.get("NO bid"),
                row.get("NO ask"), row.get("NO ask size"), row.get("NO ask 30m ago"),
                row.get("NO ask 60m ago"), row.get("snapshot age min"), row.get("fresh"),
            ]
            for row in city["markets"]
        ]
        auxiliary = city.get("auxiliaryCurrent") or {}
        cities.append({
            "city": city["city"],
            "eventId": city["eventId"],
            "stationId": city["stationId"],
            "metar": observations,
            "aux": [
                auxiliary.get("observation_time_utc"), auxiliary.get("temperature_c"),
                auxiliary.get("observed_daily_max_c"), auxiliary.get("relative_humidity"),
                auxiliary.get("precipitation_mm"), auxiliary.get("cloud_cover_pct"),
                auxiliary.get("wind_direction_deg"), auxiliary.get("wind_speed"),
                auxiliary.get("visibility_m"), auxiliary.get("solar_radiation_wm2"),
            ] if auxiliary else None,
            "forecasts": forecasts,
            "markets": markets,
        })
    return {
        "objective": packet["experiment"],
        "targetDate": packet["targetDate"],
        "asOfUtc": packet["asOfUtc"],
        "asOfLocal": packet["asOfLocal"],
        "account": packet["account"],
        "settledHistoryAvailableAtAsOf": packet["settledHistoryAvailableAtAsOf"],
        "legends": {
            "metar": ["utc", "tempC", "dailyMaxC", "dewC", "rhPct", "rainMm", "cloudPct", "windDeg", "wind", "windUnit", "gust", "visibilityM", "solarWm2", "pressureHpa", "sky"],
            "aux": ["utc", "tempC", "dailyMaxC", "rhPct", "rainMm", "cloudPct", "windDeg", "wind", "visibilityM", "solarWm2"],
            "forecasts": ["source", "model", "sampleUtc", "maxC", "peakLocal", "members", "mean", "std", "min", "max", "q10", "q50", "q90"],
            "markets": ["marketId", "bucket", "noBid", "noAsk", "noAskSize", "noAsk30m", "noAsk60m", "ageMinutes", "fresh"],
        },
        "cities": cities,
    }


def city_packet_for_ai(compact_packet: dict[str, Any], city: dict[str, Any]) -> dict[str, Any]:
    return {
        "objective": compact_packet["objective"],
        "targetDate": compact_packet["targetDate"],
        "asOfUtc": compact_packet["asOfUtc"],
        "asOfLocal": compact_packet["asOfLocal"],
        "account": compact_packet["account"],
        "settledHistoryAvailableAtAsOf": compact_packet["settledHistoryAvailableAtAsOf"],
        "legends": compact_packet["legends"],
        "city": city,
    }


def parse_ai_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AI returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("AI response must be one JSON object")
    return payload


def validate_ai_response(
    response: dict[str, Any], as_of: datetime, market_lookup: dict[str, dict[str, Any]],
    available_cash: float, config: dict[str, Any],
) -> None:
    expected = {
        "asOfUtc", "portfolioReasoning", "actions", "nextReviewMinutes",
        "nextReviewReason", "stopForDay",
    }
    if set(response) != expected:
        raise RuntimeError(f"AI response fields differ: expected={sorted(expected)} actual={sorted(response)}")
    response_time = parse_utc(response["asOfUtc"])
    if response_time is None or abs((response_time - as_of).total_seconds()) > 1:
        raise RuntimeError("AI asOfUtc does not match the replay point")
    for field_name in ("portfolioReasoning", "nextReviewReason"):
        if not isinstance(response[field_name], str) or not response[field_name].strip():
            raise RuntimeError(f"AI {field_name} must be non-empty")
    if not isinstance(response["stopForDay"], bool):
        raise RuntimeError("AI stopForDay must be boolean")
    next_review = response["nextReviewMinutes"]
    if next_review is not None and (
        not isinstance(next_review, int) or isinstance(next_review, bool) or not 30 <= next_review <= 180
    ):
        raise RuntimeError("AI nextReviewMinutes must be null or an integer from 30 to 180")
    if response["stopForDay"] and next_review is not None:
        raise RuntimeError("AI nextReviewMinutes must be null when stopForDay=true")
    if not response["stopForDay"] and next_review is None:
        raise RuntimeError("AI nextReviewMinutes is required when stopForDay=false")
    actions = response["actions"]
    if not isinstance(actions, list) or len(actions) > 7:
        raise RuntimeError("AI actions must be a list with at most seven entries")
    affordable = int((available_cash + 1e-9) // float(config["stakeUsdc"]))
    if len(actions) > affordable:
        raise RuntimeError("AI requested more fixed-stake orders than available cash permits")
    action_fields = {
        "action", "marketId", "city", "outcomeRange", "estimatedNoWinProbability",
        "reason", "contraryEvidence", "invalidationCondition",
    }
    seen: set[str] = set()
    max_age = float(config.get("marketSnapshotMaxAgeMinutes", 20))
    for action in actions:
        if not isinstance(action, dict) or set(action) != action_fields:
            raise RuntimeError("AI action fields are malformed")
        if action["action"] != "BUY_NO":
            raise RuntimeError("Backtest permits only BUY_NO actions")
        market_id = str(action["marketId"])
        if market_id in seen:
            raise RuntimeError("AI may request a market only once in one review")
        seen.add(market_id)
        market = market_lookup.get(market_id)
        if market is None:
            raise RuntimeError(f"AI selected unknown market {market_id}")
        if str(action["city"]) != str(market["city"]) or str(action["outcomeRange"]) != str(market["outcome_range"]):
            raise RuntimeError(f"AI metadata does not match market {market_id}")
        probability = finite(action["estimatedNoWinProbability"])
        if probability is None or not 0 <= probability <= 1:
            raise RuntimeError("AI estimatedNoWinProbability must be from 0 to 1")
        for field_name in ("reason", "contraryEvidence", "invalidationCondition"):
            if not isinstance(action[field_name], str) or not action[field_name].strip():
                raise RuntimeError(f"AI action {field_name} must be non-empty")
        price = finite(market.get("no_best_ask"))
        age = finite(market.get("snapshotAgeMinutes"))
        if price is None or not 0 < price < 1 or age is None or not 0 <= age <= max_age:
            raise RuntimeError(f"AI selected non-executable or stale market {market_id}")


def validate_city_analysis(
    response: dict[str, Any], as_of: datetime, city_packet: dict[str, Any],
    market_lookup: dict[str, dict[str, Any]], config: dict[str, Any],
) -> None:
    expected = {
        "asOfUtc", "eventId", "city", "weatherAssessment", "recommendations",
        "suggestedNextReviewMinutes", "nextEvidenceToWatch",
    }
    if set(response) != expected:
        raise RuntimeError("city analysis fields are malformed")
    response_time = parse_utc(response["asOfUtc"])
    if response_time is None or abs((response_time - as_of).total_seconds()) > 1:
        raise RuntimeError("city analysis asOfUtc does not match the replay point")
    city = city_packet["city"]
    if str(response["eventId"]) != str(city["eventId"]) or str(response["city"]) != str(city["city"]):
        raise RuntimeError("city analysis identity does not match its input")
    for field_name in ("weatherAssessment", "nextEvidenceToWatch"):
        if not isinstance(response[field_name], str) or not response[field_name].strip():
            raise RuntimeError(f"city analysis {field_name} must be non-empty")
    interval = response["suggestedNextReviewMinutes"]
    if not isinstance(interval, int) or isinstance(interval, bool) or not 30 <= interval <= 180:
        raise RuntimeError("city suggestedNextReviewMinutes must be from 30 to 180")
    recommendations = response["recommendations"]
    if not isinstance(recommendations, list) or len(recommendations) > 3:
        raise RuntimeError("city recommendations must contain at most three markets")
    expected_fields = {
        "marketId", "outcomeRange", "estimatedNoWinProbability", "reason",
        "contraryEvidence", "invalidationCondition",
    }
    city_market_ids = {str(row[0]) for row in city["markets"]}
    seen: set[str] = set()
    max_age = float(config.get("marketSnapshotMaxAgeMinutes", 20))
    for recommendation in recommendations:
        if not isinstance(recommendation, dict) or set(recommendation) != expected_fields:
            raise RuntimeError("city recommendation fields are malformed")
        market_id = str(recommendation["marketId"])
        if market_id in seen or market_id not in city_market_ids:
            raise RuntimeError("city recommendation contains a duplicate or unknown market")
        seen.add(market_id)
        market = market_lookup.get(market_id)
        if market is None or str(recommendation["outcomeRange"]) != str(market["outcome_range"]):
            expected_range = market.get("outcome_range") if market else None
            raise RuntimeError(
                "city recommendation market metadata is inconsistent: "
                f"marketId={market_id}, suppliedRange={recommendation['outcomeRange']!r}, "
                f"expectedRange={expected_range!r}"
            )
        probability = finite(recommendation["estimatedNoWinProbability"])
        if probability is None or not 0 <= probability <= 1:
            raise RuntimeError("city estimatedNoWinProbability must be from 0 to 1")
        for field_name in ("reason", "contraryEvidence", "invalidationCondition"):
            if not isinstance(recommendation[field_name], str) or not recommendation[field_name].strip():
                raise RuntimeError(f"city recommendation {field_name} must be non-empty")
        price = finite(market.get("no_best_ask"))
        age = finite(market.get("snapshotAgeMinutes"))
        if price is None or not 0 < price < 1 or age is None or not 0 <= age <= max_age:
            raise RuntimeError("city recommendation selected a stale or non-executable market")


class HermesBacktestClient:
    def __init__(self, config: dict[str, Any], schema_path: Path):
        self.config = config
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))

    def call(self, prompt: str) -> dict[str, Any]:
        binary = Path(str(self.config.get("hermesBinary", "~/.local/bin/hermes"))).expanduser()
        try:
            launcher = binary.resolve(strict=True)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Hermes executable not found: {binary}") from exc
        python = Path(str(self.config.get("hermesPython") or launcher.parent / "python3")).expanduser()
        profile_home = Path(str(
            self.config.get("hermesHome")
            or Path.home() / ".hermes" / "profiles" / str(self.config.get("hermesProfile") or "")
        )).expanduser()
        if not python.exists() or not profile_home.is_dir():
            raise RuntimeError("Hermes Python runtime or isolated profile is unavailable")
        full_prompt = (
            prompt
            + "\n\nReturn exactly one JSON object with no prose or Markdown. It must satisfy this schema:\n"
            + json.dumps(self.schema, ensure_ascii=False, separators=(",", ":"))
        )
        env = os.environ.copy()
        env["HERMES_HOME"] = str(profile_home)
        env["WEATHER_HERMES_TOOLSETS"] = str(self.config.get("hermesToolsets") or "memory")
        provider = str(self.config.get("hermesProvider") or "").strip()
        model = str(self.config.get("hermesModel") or "").strip()
        if bool(provider) != bool(model):
            raise RuntimeError("hermesProvider and hermesModel must be configured together")
        if provider:
            env["WEATHER_HERMES_PROVIDER"] = provider
            env["WEATHER_HERMES_MODEL"] = model
        effort = str(self.config.get("hermesReasoningEffort") or "").strip()
        if effort:
            env["WEATHER_HERMES_REASONING_EFFORT"] = effort
        retries = max(0, int(self.config.get("aiMalformedResponseRetries", 1)))
        errors = []
        for attempt in range(retries + 1):
            retry_note = ""
            if attempt:
                retry_note = "\n\nYour previous output was invalid. Recalculate and return only one complete JSON object."
            try:
                completed = subprocess.run(
                    [str(python), str(ROOT / "hermes_weather_bridge.py")],
                    input=full_prompt + retry_note,
                    text=True,
                    capture_output=True,
                    env=env,
                    cwd=Path(str(self.config.get("hermesWorkingDir") or ROOT)),
                    timeout=int(self.config.get("aiTimeoutSeconds", 180)),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                errors.append(
                    f"Hermes timed out after {self.config.get('aiTimeoutSeconds', 180)} seconds"
                )
                continue
            if completed.returncode == 0:
                try:
                    return parse_ai_json(completed.stdout)
                except RuntimeError as exc:
                    errors.append(str(exc))
                    continue
            errors.append((completed.stderr or completed.stdout)[-1000:])
        raise RuntimeError("Hermes backtest call failed: " + " | ".join(errors))


def _book_asks(market: dict[str, Any]) -> list[tuple[float, float]]:
    payload = json_value(market.get("no_book_json"), {})
    asks = payload.get("asks") if isinstance(payload, dict) else None
    output = []
    for row in asks or []:
        price, size = finite(row.get("price")), finite(row.get("size"))
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            output.append((price, size))
    if not output:
        price, size = finite(market.get("no_best_ask")), finite(market.get("no_ask_size"))
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            output.append((price, size))
    return sorted(output)


def fill_fixed_cash(
    market: dict[str, Any], cash: float, fee_rate: float,
) -> dict[str, float] | None:
    remaining = float(cash)
    shares = notional = fee = 0.0
    for price, available_shares in _book_asks(market):
        unit_fee = fee_rate * price * (1.0 - price)
        unit_cash = price + unit_fee
        take = min(available_shares, remaining / unit_cash)
        if take <= 0:
            continue
        shares += take
        notional += take * price
        fee += take * unit_fee
        remaining -= take * unit_cash
        if remaining <= 1e-8:
            break
    if remaining > 1e-6 or shares <= 0:
        return None
    return {
        "shares": shares,
        "vwap": notional / shares,
        "notionalUsdc": notional,
        "feeUsdc": fee,
        "cashDebitedUsdc": notional + fee,
    }


@dataclass
class Portfolio:
    initial_cash: float
    cash: float = field(init=False)
    fills: list[dict[str, Any]] = field(default_factory=list)
    settlements: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = float(self.initial_cash)

    def account(self) -> dict[str, Any]:
        open_fills = [row for row in self.fills if not row.get("settled")]
        return {
            "initialCashUsdc": round(self.initial_cash, 6),
            "availableCashUsdc": round(self.cash, 6),
            "openCostUsdc": round(sum(float(row["cashDebitedUsdc"]) for row in open_fills), 6),
            "openOrders": len(open_fills),
            "realizedPnlUsdc": round(sum(float(row["realizedPnlUsdc"]) for row in self.settlements), 6),
        }

    def execute(
        self, action: dict[str, Any], market: dict[str, Any], as_of: datetime,
        config: dict[str, Any], settlement: dict[str, Any],
    ) -> dict[str, Any]:
        stake = float(config["stakeUsdc"])
        if self.cash + 1e-9 < stake:
            return {"status": "rejected", "reason": "insufficient_cash", "action": action}
        execution = fill_fixed_cash(
            market, stake, float(config.get("weatherTakerFeeRate", 0.05))
        )
        if execution is None:
            return {"status": "rejected", "reason": "insufficient_NO_ask_depth", "action": action}
        fill = {
            "fillId": len(self.fills) + 1,
            "status": "filled",
            "side": "NO",
            "targetDate": settlement["target_date"],
            "eventId": str(market["event_id"]),
            "marketId": str(market["market_id"]),
            "city": market["city"],
            "outcomeRange": market["outcome_range"],
            "filledAtUtc": iso_utc(as_of),
            "estimatedNoWinProbability": float(action["estimatedNoWinProbability"]),
            "reason": action["reason"],
            "contraryEvidence": action["contraryEvidence"],
            "invalidationCondition": action["invalidationCondition"],
            **{key: round(value, 8) for key, value in execution.items()},
            "liveOrderMinSatisfied": execution["shares"] >= float(config.get("liveMinimumShares", 5)),
            "settled": False,
            "resolvedAtUtc": settlement.get("resolved_at_utc"),
            "winningMarketId": str(settlement.get("winning_market_id") or ""),
            "winningRange": settlement.get("winning_range"),
        }
        self.cash -= float(fill["cashDebitedUsdc"])
        self.fills.append(fill)
        return fill

    def settle_through(self, as_of: datetime | None = None) -> list[dict[str, Any]]:
        created = []
        for fill in self.fills:
            if fill.get("settled"):
                continue
            resolved_at = parse_utc(fill.get("resolvedAtUtc"))
            if resolved_at is None or (as_of is not None and resolved_at > as_of):
                continue
            no_won = str(fill["marketId"]) != str(fill["winningMarketId"])
            payout = float(fill["shares"]) if no_won else 0.0
            realized = payout - float(fill["cashDebitedUsdc"])
            settlement = {
                "fillId": fill["fillId"],
                "targetDate": fill["targetDate"],
                "city": fill["city"],
                "marketId": fill["marketId"],
                "outcomeRange": fill["outcomeRange"],
                "winningRange": fill["winningRange"],
                "noWon": no_won,
                "payoutUsdc": round(payout, 8),
                "realizedPnlUsdc": round(realized, 8),
                "settledAtUtc": iso_utc(resolved_at),
            }
            fill["settled"] = True
            self.cash += payout
            self.settlements.append(settlement)
            created.append(settlement)
        return created


def build_city_prompt(packet: dict[str, Any]) -> str:
    return (
        "Analyze exactly one city's historical point-in-time weather market data. You are the independent weather analyst, not the order executor. "
        "The final portfolio may only buy NO for a fixed cash amount per order. Decide freely whether zero, one, two, or three exact buckets deserve consideration. "
        "There are no Ridge candidates, deterministic thresholds, or required theses. Estimate each recommended NO's chance of winning from the data, price, "
        "weather evolution, boundary risk, and contrary evidence. Empty recommendations is valid and preferred when no positive expected value is supported. "
        "Do not infer or claim knowledge of the later settlement. Suggest when this city should next be reviewed. Keep every text field concise; analyze instead of repeating the input.\n\n"
        + json.dumps(packet, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def portfolio_packet_for_ai(
    packet: dict[str, Any], city_analyses: list[dict[str, Any]],
    market_lookup: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    candidate_ids = {
        str(row["marketId"])
        for analysis in city_analyses for row in analysis["recommendations"]
    }
    candidate_lookup = {
        market_id: market_lookup[market_id]
        for market_id in sorted(candidate_ids) if market_id in market_lookup
    }
    markets = [{
        "marketId": market_id,
        "city": row["city"],
        "outcomeRange": row["outcome_range"],
        "noBid": finite(row.get("no_best_bid")),
        "noAsk": finite(row.get("no_best_ask")),
        "noAskSize": finite(row.get("no_ask_size")),
        "noAsk30mAgo": row.get("noAsk30mAgo"),
        "noAsk60mAgo": row.get("noAsk60mAgo"),
        "snapshotAgeMinutes": finite(row.get("snapshotAgeMinutes")),
    } for market_id, row in candidate_lookup.items()]
    return ({
        "objective": packet["experiment"],
        "targetDate": packet["targetDate"],
        "asOfUtc": packet["asOfUtc"],
        "asOfLocal": packet["asOfLocal"],
        "account": packet["account"],
        "cityAnalyses": city_analyses,
        "candidateMarkets": markets,
    }, candidate_lookup)


def build_portfolio_prompt(packet: dict[str, Any]) -> str:
    return (
        "You are the final portfolio decision layer for a historical paper backtest. Seven city analysts have already read their own raw weather contexts. "
        "You receive only their short conclusions and current executable prices. Your sole objective is to maximize final cash. You may only BUY_NO, "
        "each order spends exactly the fixed cash amount, and empty actions means HOLD. Compare opportunities across cities, account for price and correlated risk, "
        "choose any subset that fits available cash, and independently choose the next portfolio review time. Do not assume the analysts are correct and do not "
        "infer later settlement information. Keep every text field concise and do not restate all city evidence.\n\n"
        + json.dumps(packet, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def call_with_semantic_validation(
    client: HermesBacktestClient,
    prompt: str,
    validator: Any,
    retries: int,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    validation_error = None
    for attempt in range(max(0, int(retries)) + 1):
        retry_note = ""
        if validation_error:
            retry_note = (
                "\n\nYour previous JSON violated an invariant: " + validation_error
                + ". Recalculate from the same point-in-time input."
            )
        response = client.call(prompt + retry_note)
        if audit_path is not None:
            attempt_path = audit_path.with_name(
                f"{audit_path.stem}_attempt{attempt + 1}{audit_path.suffix}"
            )
            _write_json(attempt_path, response)
        try:
            validator(response)
            return response
        except RuntimeError as exc:
            validation_error = str(exc)
            if attempt >= retries:
                raise
    raise RuntimeError(validation_error or "AI semantic validation failed")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def report_markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        "# AI NO Backtest Report",
        "",
        f"- Dates: {', '.join(result['dates'])}",
        f"- Model: {result['model']}",
        f"- Initial cash: {summary['initialCashUsdc']:.4f} USDC",
        f"- Ending cash: {summary['endingCashUsdc']:.4f} USDC",
        f"- Realized PnL: {summary['realizedPnlUsdc']:.4f} USDC",
        f"- Return: {summary['returnPct']:.2f}%",
        f"- Orders: {summary['orders']} ({summary['wins']} wins / {summary['losses']} losses)",
        f"- AI reviews: {summary['reviews']}",
        f"- Total AI calls: {summary['aiCalls']}",
        f"- Research fills below live 5-share minimum: {summary['belowLiveMinimumOrders']}",
        "",
        "## Trades",
        "",
        markdown_table(
            ["date", "time UTC", "city", "bucket", "NO price", "shares", "AI P(NO)", "result", "PnL", "live min"],
            [[fill["targetDate"], fill["filledAtUtc"], fill["city"], fill["outcomeRange"], fill["vwap"],
              fill["shares"], fill["estimatedNoWinProbability"],
              "WIN" if settlement and settlement["noWon"] else "LOSS" if settlement else "OPEN",
              settlement["realizedPnlUsdc"] if settlement else None, fill["liveOrderMinSatisfied"]]
             for fill in result["fills"]
             for settlement in [next((row for row in result["settlements"] if row["fillId"] == fill["fillId"]), None)]],
        ),
        "",
        "## AI Review Timeline",
        "",
        markdown_table(
            ["date", "as of UTC", "actions requested", "fills", "next min", "stop", "reasoning"],
            [[step["targetDate"], step["asOfUtc"], len(step["response"]["actions"]),
              sum(1 for row in step["executions"] if row.get("status") == "filled"),
              step["response"]["nextReviewMinutes"], step["response"]["stopForDay"],
              step["response"]["portfolioReasoning"]]
             for step in result["steps"]],
        ),
        "",
        f"This is an exploratory {len(result['dates'])}-day backtest, not statistical validation. The prompts and raw responses in the run directory are the audit trail.",
    ]
    return "\n".join(lines) + "\n"


def run_backtest(
    config: dict[str, Any], dates: list[str] | None = None, *, prepare_only: bool = False,
) -> tuple[Path, dict[str, Any]]:
    db_path = Path(str(config["databasePath"]))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cities = [str(city) for city in config["allowedCities"]]
    dates = dates or latest_resolved_dates(db, cities, int(config.get("lookbackResolvedDates", 3)))
    if not dates:
        raise RuntimeError("No fully resolved dates with historical snapshots were found")
    settlement_by_event: dict[str, dict[str, Any]] = {}
    for target_date in dates:
        events = events_for_date(db, target_date, cities, include_settlement=True)
        if len(events) != len(cities):
            raise RuntimeError(f"{target_date} does not have exactly one event for every configured city")
        settlement_by_event.update({str(event["event_id"]): event for event in events})

    output_root = Path(str(config.get("outputDir", "data/ai_no_backtests")))
    if not output_root.is_absolute():
        output_root = ROOT / output_root
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_json(run_dir / "config.json", config)

    portfolio = Portfolio(float(config["initialCashUsdc"]))
    runtime_config = dict(config)
    if not prepare_only:
        profile_home, hermes_workdir = ensure_isolated_hermes_profile(runtime_config)
        runtime_config["hermesHome"] = str(profile_home)
        runtime_config["hermesWorkingDir"] = str(hermes_workdir)
    city_client = None if prepare_only else HermesBacktestClient(runtime_config, CITY_SCHEMA_PATH)
    portfolio_client = None if prepare_only else HermesBacktestClient(runtime_config, SCHEMA_PATH)
    steps: list[dict[str, Any]] = []
    first_minutes = int(config.get("firstReviewLocalMinutes", 420))
    last_minutes = int(config.get("lastReviewLocalMinutes", 1140))
    timezone_name = "Asia/Shanghai"
    stop_all = False
    for target_date in dates:
        review_at = local_review_time(target_date, first_minutes, timezone_name)
        day_end = local_review_time(target_date, last_minutes, timezone_name)
        for review_index in range(int(config.get("maxReviewsPerDay", 8))):
            portfolio.settle_through(review_at)
            packet, market_lookup = build_replay_packet(
                db, target_date, review_at, cities, portfolio.account(),
                list(portfolio.settlements), config,
            )
            markdown = render_packet_markdown(packet)
            compact_packet = compact_packet_for_ai(packet)
            stem = f"{len(steps) + 1:03d}_{target_date}_{review_at.astimezone(ZoneInfo(timezone_name)).strftime('%H%M')}"
            (run_dir / f"{stem}_input.md").write_text(markdown, encoding="utf-8")
            city_analyses = []
            for city in compact_packet["cities"]:
                city_packet = city_packet_for_ai(compact_packet, city)
                city_name = str(city["city"]).replace("/", "_")
                _write_json(run_dir / f"{stem}_{city_name}_input.json", city_packet)
                if prepare_only:
                    continue
                city_response = call_with_semantic_validation(
                    city_client,
                    build_city_prompt(city_packet),
                    lambda value, city_packet=city_packet: validate_city_analysis(
                        value, review_at, city_packet, market_lookup, config
                    ),
                    int(config.get("aiMalformedResponseRetries", 1)),
                    run_dir / f"{stem}_{city_name}_raw.json",
                )
                city_analyses.append(city_response)
                _write_json(run_dir / f"{stem}_{city_name}_response.json", city_response)
            if prepare_only:
                stop_all = True
                break
            portfolio_packet, candidate_lookup = portfolio_packet_for_ai(
                packet, city_analyses, market_lookup
            )
            _write_json(run_dir / f"{stem}_portfolio_input.json", portfolio_packet)
            response = call_with_semantic_validation(
                portfolio_client,
                build_portfolio_prompt(portfolio_packet),
                lambda value: validate_ai_response(
                    value, review_at, candidate_lookup, portfolio.cash, config
                ),
                int(config.get("aiMalformedResponseRetries", 1)),
                run_dir / f"{stem}_portfolio_raw.json",
            )
            _write_json(run_dir / f"{stem}_response.json", response)
            executions = []
            for action in response["actions"]:
                market = market_lookup[str(action["marketId"])]
                settlement = settlement_by_event[str(market["event_id"])]
                executions.append(portfolio.execute(action, market, review_at, config, settlement))
            step = {
                "targetDate": target_date,
                "asOfUtc": iso_utc(review_at),
                "cityAnalyses": city_analyses,
                "response": response,
                "executions": executions,
            }
            steps.append(step)
            _write_json(run_dir / f"{stem}_executions.json", executions)
            if response["stopForDay"]:
                break
            interval = int(response.get("nextReviewMinutes") or config.get("defaultNextReviewMinutes", 60))
            review_at += timedelta(minutes=interval)
            if review_at > day_end:
                break
        if stop_all:
            break

    if not prepare_only:
        portfolio.settle_through(None)
    ending_cash = portfolio.cash
    realized = sum(float(row["realizedPnlUsdc"]) for row in portfolio.settlements)
    wins = sum(1 for row in portfolio.settlements if row["noWon"])
    result = {
        "runId": run_id,
        "preparedOnly": prepare_only,
        "dates": dates,
        "model": str(config.get("hermesModel") or "profile-default"),
        "summary": {
            "initialCashUsdc": float(config["initialCashUsdc"]),
            "endingCashUsdc": round(ending_cash, 8),
            "realizedPnlUsdc": round(realized, 8),
            "returnPct": round((ending_cash / float(config["initialCashUsdc"]) - 1.0) * 100.0, 4),
            "orders": len(portfolio.fills),
            "wins": wins,
            "losses": len(portfolio.settlements) - wins,
            "reviews": len(steps),
            "aiCalls": sum(len(step.get("cityAnalyses") or []) + 1 for step in steps),
            "belowLiveMinimumOrders": sum(1 for row in portfolio.fills if not row["liveOrderMinSatisfied"]),
        },
        "fills": portfolio.fills,
        "settlements": portfolio.settlements,
        "steps": steps,
    }
    _write_json(run_dir / "result.json", result)
    (run_dir / "report.md").write_text(report_markdown(result), encoding="utf-8")
    db.close()
    return run_dir, result


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a free-decision, NO-only Hermes weather backtest")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--dates", nargs="*", help="Explicit YYYY-MM-DD dates; default is latest resolved dates")
    parser.add_argument("--prepare-only", action="store_true", help="Build the first leak-free packet without calling AI")
    args = parser.parse_args()
    config = load_config(args.config.expanduser().resolve())
    run_dir, result = run_backtest(config, args.dates or None, prepare_only=args.prepare_only)
    print(json.dumps({"runDir": str(run_dir), "summary": result["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
