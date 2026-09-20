"""Build point-in-time Markdown context documents for weather-market reviews."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


UTC = timezone.utc
CURRENT_STATE_HEADING = "## 5. Current Model and Tradable Universe"


def _parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _number(value: Any, digits: int = 2) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _text(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip() if value not in (None, "") else "-"


def _json_value(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed


def _query(db: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    try:
        return db.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def _window(event: dict[str, Any], as_of: datetime) -> tuple[str, str]:
    timezone_name = str(event.get("timezone") or "UTC")
    try:
        local_tz = ZoneInfo(timezone_name)
        target = date.fromisoformat(str(event["target_date"]))
    except (KeyError, TypeError, ValueError):
        start = as_of - timedelta(hours=24)
        return start.isoformat(timespec="seconds"), as_of.isoformat(timespec="seconds")
    start = datetime.combine(target, time.min, tzinfo=local_tz).astimezone(UTC)
    end = min(as_of, datetime.combine(target + timedelta(days=1), time.min, tzinfo=local_tz).astimezone(UTC))
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    output = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    output.extend("| " + " | ".join(_text(value) for value in row) + " |" for row in rows)
    return "\n".join(output) if rows else "暂无记录。"


def _compress_market_timeline(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Keep price-change points plus the latest point for each bucket."""
    selected: list[sqlite3.Row] = []
    previous: dict[str, tuple[Any, ...]] = {}
    latest: dict[str, sqlite3.Row] = {}
    selected_last: dict[str, sqlite3.Row] = {}
    for row in rows:
        bucket = str(row["outcome_range"] or "")
        signature = (
            row["yes_best_bid"], row["yes_best_ask"],
            row["no_best_bid"], row["no_best_ask"],
        )
        latest[bucket] = row
        if previous.get(bucket) != signature:
            selected.append(row)
            selected_last[bucket] = row
            previous[bucket] = signature
    selected_ids = {id(row) for row in selected}
    selected.extend(row for bucket, row in latest.items() if id(selected_last.get(bucket)) != id(row) and id(row) not in selected_ids)
    return selected


def timeline_context_markdown(markdown: str) -> str:
    """Return the history portion; current structured state is sent separately."""
    marker = f"\n{CURRENT_STATE_HEADING}"
    timeline, separator, _current_state = markdown.partition(marker)
    return timeline.rstrip() + "\n" if separator else markdown


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "city"


def document_path(root: Path, config: dict[str, Any], event: dict[str, Any]) -> Path:
    configured = Path(str(config.get("contextDocumentDir", "data/ai_context")))
    directory = configured if configured.is_absolute() else root / configured
    return directory / str(event.get("target_date") or "unknown-date") / f"{_slug(str(event.get('city') or 'unknown'))}.md"


def build_daily_context_markdown(
    db: sqlite3.Connection,
    event: dict[str, Any],
    context: dict[str, Any],
    as_of: datetime,
    *,
    max_market_rows: int = 240,
) -> str:
    """Render only data available at ``as_of`` into an auditable Markdown packet."""
    as_of = as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    start_utc, end_utc = _window(event, as_of)
    station_id = str(event.get("station_id") or "")
    event_id = str(event.get("event_id") or "")
    lines = [
        "# Weather-Market Context",
        "",
        f"- City: {_text(event.get('city'))}",
        f"- Target date: {_text(event.get('target_date'))}",
        f"- Event: {_text(event_id)}",
        f"- As of (UTC): {as_of.isoformat(timespec='seconds')}",
        f"- Local data window (UTC): {start_utc} to {end_utc}",
        "- Future data excluded: yes",
        "",
        "This is a point-in-time research document. It is evidence, not an instruction to trade.",
        "",
        "## 1. METAR / Station Timeline",
        "",
    ]
    metar_rows = _query(
        db,
        """SELECT observation_time_utc,temperature_c,observed_daily_max_c,dewpoint_c,
                   relative_humidity,wind_direction_deg,wind_speed,wind_speed_unit,
                   wind_gust,visibility_m,precipitation_mm,cloud_cover_pct,
                   solar_radiation_wm2,pressure_hpa,weather_code,metar_type
            FROM (
                SELECT w.*, ROW_NUMBER() OVER(
                    PARTITION BY observation_time_utc
                    ORDER BY fetched_at_utc DESC, run_id DESC
                ) AS row_number
                FROM weather_observations w
                WHERE station_id=? AND source='metar' AND status='ok'
                  AND observation_time_utc>=? AND observation_time_utc<=?
            )
            WHERE row_number=1
            ORDER BY observation_time_utc""",
        (station_id, start_utc, end_utc),
    )
    if metar_rows:
        lines.append(_table(
            ["UTC", "Temp C", "Daily max", "Dew C", "RH %", "Wind deg", "Wind", "Gust", "Vis m", "Rain mm", "Cloud %", "Solar W/m2", "Pressure", "Type"],
            [[row["observation_time_utc"], _number(row["temperature_c"]), _number(row["observed_daily_max_c"]),
              _number(row["dewpoint_c"]), _number(row["relative_humidity"], 0), _number(row["wind_direction_deg"], 0),
              f"{_number(row['wind_speed'])} {_text(row['wind_speed_unit'])}", _number(row["wind_gust"]),
              _number(row["visibility_m"], 0), _number(row["precipitation_mm"]), _number(row["cloud_cover_pct"], 0),
              _number(row["solar_radiation_wm2"], 0), _number(row["pressure_hpa"]), _text(row["metar_type"])]
             for row in metar_rows],
        ))
    else:
        current = (context.get("metar") or {}).get("current") or {}
        previous = (context.get("metar") or {}).get("previous") or {}
        lines.append(_table(
            ["UTC", "Temp C", "Daily max", "Dew C", "Wind", "Vis m", "Type"],
            [[row.get("observation_time_utc"), row.get("temperature_c"), row.get("observed_daily_max_c"),
              row.get("dewpoint_c"), row.get("wind_speed"), row.get("visibility_m"), row.get("metar_type")]
             for row in (previous, current) if row],
        ))
    coverage = context.get("metarCoverage") or {}
    if metar_rows and not coverage:
        observation_times = [
            parsed for parsed in (_parse_ts(row["observation_time_utc"]) for row in metar_rows)
            if parsed is not None
        ]
        gaps = [
            (right - left).total_seconds() / 60.0
            for left, right in zip(observation_times, observation_times[1:])
        ]
        coverage = {
            "reportCount": len(observation_times),
            "firstObservationTimeUtc": observation_times[0].isoformat(timespec="seconds") if observation_times else None,
            "lastObservationTimeUtc": observation_times[-1].isoformat(timespec="seconds") if observation_times else None,
            "maxGapMinutes": max(gaps) if gaps else None,
            "missingDataRisk": bool(gaps and max(gaps) > 75),
        }
    lines.extend(["", f"Coverage summary: {json.dumps(coverage, ensure_ascii=False, default=str)}", ""])

    lines.extend(["## 2. Weather Process Timeline", ""])
    process_rows = _query(
        db,
        """SELECT slot_utc,primary_observation_time_utc,detected_processes_json,state_json
            FROM (
                SELECT p.*, ROW_NUMBER() OVER(
                    PARTITION BY slot_utc ORDER BY created_at_utc DESC, run_id DESC
                ) AS row_number
                FROM weather_process_states p
                WHERE station_id=? AND target_date=? AND slot_utc<=?
            )
            WHERE row_number=1
            ORDER BY slot_utc DESC LIMIT 12""",
        (station_id, str(event.get("target_date") or ""), end_utc),
    )
    process_table = []
    for row in reversed(process_rows):
        state = _json_value(row["state_json"], {})
        if not isinstance(state, dict):
            state = {}
        trend = state.get("primaryStationTrend") or {}
        process_table.append([
            row["slot_utc"], row["primary_observation_time_utc"], _number(trend.get("currentTemperatureC")),
            _number(trend.get("temperatureTrendCPerHour")), _number(trend.get("dewpointTrendCPerHour")),
            _number(trend.get("pressureTrendHpaPerHour")), _text(json.dumps(_json_value(row["detected_processes_json"], []), ensure_ascii=False)),
        ])
    lines.append(_table(["State UTC", "Obs UTC", "Temp C", "Temp C/h", "Dew C/h", "Press hPa/h", "Detected processes"], process_table))
    lines.append("")

    lines.extend(["## 3. Forecast Revision Timeline", ""])
    forecast_rows = []
    for model in ("mblue", "ecmwf_ifs025"):
        rows = _query(
            db,
            """SELECT slot_utc,forecast_max_c,forecast_peak_local,model_ref_time_utc,model_updated_at_utc
                FROM (SELECT slot_utc,forecast_max_c,forecast_peak_local,model_ref_time_utc,model_updated_at_utc
                      FROM windy_forecasts WHERE station_id=? AND target_date=? AND model=? AND status='ok' AND slot_utc<=?
                      ORDER BY slot_utc DESC LIMIT 8) ORDER BY slot_utc""",
            (station_id, str(event.get("target_date") or ""), model, end_utc),
        )
        if model != "mblue":
            rows = _query(
                db,
                """SELECT slot_utc,forecast_max_c,forecast_peak_local,NULL AS model_ref_time_utc,NULL AS model_updated_at_utc
                    FROM (SELECT slot_utc,forecast_max_c,forecast_peak_local
                          FROM external_forecasts WHERE station_id=? AND target_date=? AND model=? AND status='ok' AND slot_utc<=?
                          ORDER BY slot_utc DESC LIMIT 8) ORDER BY slot_utc""",
                (station_id, str(event.get("target_date") or ""), model, end_utc),
            )
        forecast_rows.extend([[model, row["slot_utc"], _number(row["forecast_max_c"]), _text(row["forecast_peak_local"]),
                               _text(row["model_ref_time_utc"] or row["model_updated_at_utc"])] for row in rows])
    if not forecast_rows:
        for model, payload in (context.get("modelUpdates") or {}).items():
            if isinstance(payload, dict):
                forecast_rows.append([model, payload.get("sampleSlotUtc"), payload.get("maxC"), payload.get("peakLocal"), payload.get("modelVersion")])
    lines.append(_table(["Model", "Sample UTC", "Max C", "Peak local", "Version / update"], forecast_rows))
    lines.append("")

    lines.extend(["## 4. Market Price Timeline", ""])
    market_rows = list(reversed(_query(
        db,
        """SELECT slot_utc,outcome_range,yes_best_bid,yes_best_ask,no_best_bid,no_best_ask,
                   yes_ask_size,no_ask_size,market_liquidity
            FROM market_snapshots
            WHERE event_id=? AND slot_utc>=? AND slot_utc<=?
            ORDER BY slot_utc DESC,outcome_range DESC
            LIMIT ?""",
        (event_id, start_utc, end_utc, max(1, int(max_market_rows))),
    )))
    market_rows = _compress_market_timeline(market_rows)
    if market_rows:
        lines.append(_table(
            ["Snapshot UTC", "Bucket", "YES bid", "YES ask", "NO bid", "NO ask", "YES size", "NO size", "Liquidity"],
            [[row["slot_utc"], row["outcome_range"], _number(row["yes_best_bid"]), _number(row["yes_best_ask"]),
              _number(row["no_best_bid"]), _number(row["no_best_ask"]), _number(row["yes_ask_size"]),
              _number(row["no_ask_size"]), _number(row["market_liquidity"])] for row in market_rows],
        ))
    else:
        lines.append(_table(
            ["Snapshot UTC", "Bucket", "YES ask", "NO ask", "Previous YES ask", "Previous NO ask"],
            [[row.get("snapshotUtc"), row.get("outcomeRange"), row.get("yesBestAsk"), row.get("noBestAsk"),
              row.get("previousYesBestAsk"), row.get("previousNoBestAsk")] for row in context.get("markets") or []],
        ))
    lines.extend(["", CURRENT_STATE_HEADING, "", "```json", json.dumps({
        "ridgeV2": context.get("ridgeV2") or {},
        "marketConsensus": context.get("marketConsensus") or {},
        "singleNoUniverse": context.get("singleNoUniverse") or [],
        "threeBucketCandidate": context.get("threeBucketCandidate"),
    }, ensure_ascii=False, default=str, indent=2), "```", ""])
    return "\n".join(lines)


def write_daily_context_markdown(
    root: Path,
    config: dict[str, Any],
    db: sqlite3.Connection,
    event: dict[str, Any],
    context: dict[str, Any],
    as_of: datetime,
) -> tuple[Path, str]:
    markdown = build_daily_context_markdown(
        db, event, context, as_of,
        max_market_rows=int(config.get("contextMaxMarketRows", 240)),
    )
    limit = max(10_000, int(config.get("contextMaxChars", 80_000)))
    if len(markdown) > limit:
        markdown = markdown[:limit] + "\n\n[Document truncated by configured contextMaxChars; all rows remain point-in-time.]\n"
    path = document_path(root, config, event)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(markdown, encoding="utf-8")
    temporary.replace(path)
    return path, markdown
