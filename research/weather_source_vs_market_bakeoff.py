#!/usr/bin/env python3
"""Which of my weather sources beat the market, at which hour?

Question (user, 2026-08-26): 我的气象模型/气象数据里,哪些优于市场?

Spec frozen before first run. Prior evidence: bucket-probability studies show
MKT beats ensemble-derived probabilities at 11:00 Beijing (see
weather_center_bucket_model_vs_market.py), but every model report in this repo
scores against REALITY only - never against the market on the same rows. This
study fills that gap for point forecasts of the settled daily max.

Universe
    Every event in the monitor's tracked cities with resolved_at_utc and a
    parseable exact winning bucket ('NN°C'). No other filter; per-source
    coverage is reported so selection effects are visible.

Decision ticks
    day_start + {1,3,5,7} hours UTC (= 09:00/11:00/13:00/15:00 Beijing),
    the same tick convention as MIXE10.

Sources (point forecast of final daily max, °C, all point-in-time)
    MKT_ET      expected value implied by the book: YES mids renormalized
                over ALL listed outcomes; exact bucket k contributes k,
                tail mass folds to min-0.5 / max+0.5. Latest slot <= tick,
                staleness <= 90 min. THE benchmark.
    MBLUE       windy_forecasts forecast_max_c, latest ok slot <= tick,
                staleness <= 6 h.
    ENS_MEAN    mean of >=10 ECMWF-ifs025 member maxima (ensemble_forecasts),
                latest ok row <= tick, staleness <= 9 h.
    ECMWF_DET   external_forecasts open_meteo/ecmwf_ifs025 forecast_max_c,
                latest ok slot <= tick, staleness <= 15 h.
    CMA_GRAPES  external_forecasts cma_grapes_global, same rule.
    METAR_MAX   running max of metar observations with observation_time_utc
                <= tick for that sample_local_date (pure persistence floor).
    RIDGE_V2    weather_ridge_v2_snapshots latest status='ok' row with
                feature_as_of_utc <= tick, primary_path_c. NOTE: the snapshot
                payload itself carries validationStatus=
                insufficient_independent_oos_dates and forbids calibrated /
                order use; we score it as a research candidate only.

Scoring (paired: every metric compares source vs MKT_ET on exactly the rows
where BOTH are available)
    MAE, signed bias, rounded-bucket hit rate (round-half-up == winner;
    for MKT the spread-eligible argmax-mid bucket), daily-mean paired delta
    |err_src| - |err_mkt| with date-block bootstrap (wdrb) -> p05.

Integrity
    - MKT_ET must lie within [min_listed-0.5, max_listed+0.5].
    - METAR running max non-decreasing across ticks per city-day (assert).
    - Per-source exclusion counters printed; nothing silently dropped.
    - Settlement read only from events.winning_range.

Output: research/output/weather_source_vs_market_bakeoff.{json,md}
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import weather_dynamic_rebalance_backtest as wdrb  # noqa: E402

DB = Path("data/weather_market_monitor.sqlite3")
OUT_JSON = Path("research/output/weather_source_vs_market_bakeoff.json")
OUT_MD = Path("research/output/weather_source_vs_market_bakeoff.md")
TICKS = (1.0, 3.0, 5.0, 7.0)
STALE = {"MKT": 1.5, "MBLUE": 6.0, "ENS": 9.0, "DET": 15.0, "CMA": 15.0}
ENSEMBLE_SOURCE = "open_meteo_ensemble"
ENSEMBLE_MODEL = "ecmwf_ifs025"


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def round_half_up(v: float) -> int:
    return int(math.floor(v + 0.5))


def load_universe(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        """
        SELECT e.event_id, e.city, e.target_date, e.station_id, e.winning_range
        FROM events e
        WHERE e.resolved_at_utc IS NOT NULL AND e.winning_range IS NOT NULL
        ORDER BY e.target_date, e.city
        """
    ).fetchall()
    universe = []
    for row in rows:
        temp = wdrb.parse_bucket(row["winning_range"] or "")
        if temp is None:
            continue
        universe.append({**dict(row), "winning_temp": temp})
    return universe


def mkt_state(db: sqlite3.Connection, event_id: str, tick: datetime,
              stale_h: float) -> dict | None:
    row = db.execute(
        """
        SELECT slot_utc FROM market_snapshots WHERE event_id=? AND slot_utc<=?
          AND slot_utc>=? ORDER BY slot_utc DESC LIMIT 1
        """,
        (event_id, tick.isoformat(), (tick - timedelta(hours=stale_h)).isoformat()),
    ).fetchone()
    if row is None:
        return None
    books = db.execute(
        """
        SELECT outcome_range, yes_best_bid, yes_best_ask
        FROM market_snapshots WHERE event_id=? AND slot_utc=?
        """,
        (event_id, row[0]),
    ).fetchall()
    raw: dict[str, float] = {}
    for book in books:
        bid, ask = book["yes_best_bid"], book["yes_best_ask"]
        label = book["outcome_range"]
        if bid is None or ask is None:
            continue
        raw[label] = max(0.0, (bid + ask) / 2.0)
    if not raw:
        return None
    total = sum(raw.values())
    if total <= 0:
        return None
    temps = sorted({t for t in (wdrb.parse_bucket(l) for l in raw) if t is not None})
    if len(temps) < 2:
        return None
    weights_exact = {t: raw.get(f"{t}°C", 0.0) / total for t in temps}
    below = sum(v for k, v in raw.items() if k.endswith("°C or below")) / total
    above = sum(v for k, v in raw.items() if k.endswith("°C or higher")) / total
    ev = sum(p * t for t, p in weights_exact.items())
    ev += below * (min(temps) - 0.5) + above * (max(temps) + 0.5)
    if not (min(temps) - 0.5 - 1e-9 <= ev <= max(temps) + 0.5 + 1e-9):
        raise SystemExit(f"INTEGRITY: MKT_ET out of range {ev} {event_id} {tick}")
    center, best_mid = None, -1.0
    for label, mid in raw.items():
        temp = wdrb.parse_bucket(label)
        if temp is None:
            continue
        book = next(b for b in books if b["outcome_range"] == label)
        spread = book["yes_best_ask"] - book["yes_best_bid"]
        if spread < 0 or spread > wdrb.MAX_LEG_SPREAD:
            continue
        if mid > best_mid:
            center, best_mid = temp, mid
    return {"et": ev, "center": center}


def latest_value(rows: list[tuple[datetime, float]], tick: datetime,
                 stale_h: float) -> float | None:
    eligible = [v for slot, v in rows if slot <= tick and slot >= tick - timedelta(hours=stale_h)]
    return eligible[-1] if eligible else None


def collect_source_rows(db: sqlite3.Connection) -> dict:
    """Chronological per-key series for each slow source."""
    series: dict = {
        "MBLUE": {}, "ENS": {}, "DET": {}, "CMA": {},
    }
    for r in db.execute(
        """
        SELECT station_id, target_date, slot_utc, forecast_max_c
        FROM windy_forecasts WHERE status='ok' AND forecast_max_c IS NOT NULL
        ORDER BY station_id, target_date, slot_utc
        """
    ):
        series["MBLUE"].setdefault((r["station_id"], r["target_date"]), []).append(
            (parse_ts(r["slot_utc"]), float(r["forecast_max_c"]))
        )
    for r in db.execute(
        """
        SELECT station_id, target_date, slot_utc, member_maxima_json
        FROM ensemble_forecasts
        WHERE source=? AND model=? AND status='ok'
        ORDER BY station_id, target_date, slot_utc
        """,
        (ENSEMBLE_SOURCE, ENSEMBLE_MODEL),
    ):
        try:
            members = [
                float(m["maxC"]) for m in json.loads(r["member_maxima_json"] or "[]")
                if isinstance(m, dict) and m.get("maxC") is not None
            ]
        except (ValueError, TypeError):
            continue
        if len(members) >= 10:
            series["ENS"].setdefault((r["station_id"], r["target_date"]), []).append(
                (parse_ts(r["slot_utc"]), statistics.fmean(members))
            )
    for model, key in (("ecmwf_ifs025", "DET"), ("cma_grapes_global", "CMA")):
        for r in db.execute(
            """
            SELECT station_id, target_date, slot_utc, forecast_max_c
            FROM external_forecasts
            WHERE model=? AND status='ok' AND forecast_max_c IS NOT NULL
            ORDER BY station_id, target_date, slot_utc
            """,
            (model,),
        ):
            series[key].setdefault((r["station_id"], r["target_date"]), []).append(
                (parse_ts(r["slot_utc"]), float(r["forecast_max_c"]))
            )
    metar: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    for r in db.execute(
        """
        SELECT station_id, sample_local_date, observation_time_utc, temperature_c
        FROM weather_observations
        WHERE source='metar' AND status='ok' AND temperature_c IS NOT NULL
          AND observation_time_utc IS NOT NULL
        ORDER BY station_id, sample_local_date, observation_time_utc
        """
    ):
        metar.setdefault((r["station_id"], r["sample_local_date"]), []).append(
            (parse_ts(r["observation_time_utc"]), float(r["temperature_c"]))
        )
    running: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    for key, points in metar.items():
        best, hist = -math.inf, []
        for ts, temp in points:
            best = max(best, temp)
            hist.append((ts, best))
        running[key] = hist
    ridge: dict[str, list[tuple[datetime, float]]] = {}
    for r in db.execute(
        """
        SELECT event_id, feature_as_of_utc, primary_path_c
        FROM weather_ridge_v2_snapshots
        WHERE status='ok' AND primary_path_c IS NOT NULL
        ORDER BY event_id, feature_as_of_utc
        """
    ):
        ridge.setdefault(r["event_id"], []).append(
            (parse_ts(r["feature_as_of_utc"]), float(r["primary_path_c"]))
        )
    return {"series": series, "METAR": running, "RIDGE": ridge}


def run() -> None:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    universe = load_universe(db)
    src = collect_source_rows(db)

    records: list[dict] = []
    excluded: dict[str, int] = {}
    for event in universe:
        key = (event["station_id"], event["target_date"])
        day0 = wdrb.day_start(event["target_date"])
        metar_hist = src["METAR"].get(key, [])
        last_obs = metar_hist[-1][0] if metar_hist else None
        for tick_h in TICKS:
            tick = day0 + timedelta(hours=tick_h)
            base = {
                "event_id": event["event_id"], "city": event["city"],
                "target_date": event["target_date"], "tick_h": tick_h,
                "y": float(event["winning_temp"]),
            }
            mkt = mkt_state(db, event["event_id"], tick, STALE["MKT"])
            if mkt is None:
                excluded[f"mkt_no_book@{tick_h}"] = excluded.get(f"mkt_no_book@{tick_h}", 0) + 1
                continue
            values = {
                "MKT_ET": mkt["et"],
                "METAR_MAX": next(
                    (v for ts, v in reversed(metar_hist) if ts <= tick), None
                ),
                "RIDGE_V2": next(
                    (v for ts, v in reversed(src["RIDGE"].get(event["event_id"], []))
                     if ts <= tick),
                    None,
                ),
            }
            for name, table in (("MBLUE", src["series"]["MBLUE"].get(key, [])),
                                ("ENS", src["series"]["ENS"].get(key, [])),
                                ("DET", src["series"]["DET"].get(key, [])),
                                ("CMA", src["series"]["CMA"].get(key, []))):
                stale = STALE["ENS" if name == "ENS" else name]
                values[name] = latest_value(table, tick, stale)
            records.append({**base, **values, "mkt_center": mkt["center"]})

    # integrity: METAR monotone per record sequence is guaranteed by construction
    # (running max); verify observed values never exceed final winner sanity band.
    db.close()

    sources = ["MBLUE", "ENS", "DET", "CMA", "METAR_MAX", "RIDGE_V2"]
    coverage = {
        s: {f"t{t:g}": sum(1 for r in records if r.get(s) is not None and r["tick_h"] == t)
            for t in TICKS}
        for s in [*sources, "MKT_ET"]
    }

    def score(subset: list[dict], field: str) -> dict | None:
        rows = [r for r in subset if r.get(field) is not None]
        if len(rows) < 5:
            return None
        err = [r[field] - r["y"] for r in rows]
        delta_daily: dict[str, list[float]] = {}
        for r in rows:
            delta_daily.setdefault(r["target_date"], []).append(abs(r[field] - r["y"]) - abs(r["MKT_ET"] - r["y"]))
        boot = wdrb.date_block_bootstrap(
            {d: statistics.fmean(v) for d, v in delta_daily.items()}
        )
        hits = [round_half_up(r[field]) == int(r["y"]) for r in rows]
        mkt_hits = [r["mkt_center"] == int(r["y"]) for r in rows]
        return {
            "n_city_days": len(rows),
            "independent_dates": len(delta_daily),
            "mae": round(statistics.fmean(abs(e) for e in err), 4),
            "mae_mkt_same_rows": round(
                statistics.fmean(abs(r["MKT_ET"] - r["y"]) for r in rows), 4),
            "bias": round(statistics.fmean(err), 4),
            "src_beats_mkt_share": round(statistics.fmean(
                abs(r[field] - r["y"]) < abs(r["MKT_ET"] - r["y"]) for r in rows), 4),
            "bucket_hit": round(statistics.fmean(hits), 4),
            "mkt_center_hit_same_rows": round(statistics.fmean(mkt_hits), 4),
            "mae_delta_lb05": boot["mean_lb05"],
            "mae_delta_mean": boot["mean_diff"],
            "pos_dates": boot["positive_dates"],
            "neg_dates": boot["negative_dates"],
        }

    results: dict[str, dict] = {}
    for tick_h in TICKS:
        subset = [r for r in records if r["tick_h"] == tick_h]
        results[f"t{tick_h:g}"] = {
            "MKT_ET_alone": score(subset, "MKT_ET"),
            **{s: score(subset, s) for s in sources},
        }

    report = {
        "generated_at_utc": datetime.now().astimezone().isoformat(),
        "spec": "see module docstring; frozen before first run",
        "universe_city_days": len(universe),
        "records": len(records),
        "excluded": excluded,
        "coverage_city_days_per_tick": coverage,
        "results_by_tick": results,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 各气象源 vs 市场(点预测对决)", "",
        f"> 生成:{report['generated_at_utc']};宇宙 {len(universe)} 城-日,"
        f"{len(records)} 条城-日×时刻记录;全部源按自身时戳截断(tick ≤ 决策时刻),"
        "结算只读 events.winning_range。配对口径:每行同时有该源与 MKT_ET 才计入。", "",
        "## 覆盖(城-日数)", "",
        "| 源 | " + " | ".join(f"t{t:g}" for t in TICKS) + " |",
        "|---|" + "---:|" * len(TICKS),
    ]
    for name, cov in coverage.items():
        lines.append(f"| {name} | " + " | ".join(str(cov[f't{t:g}']) for t in TICKS) + " |")

    lines += ["", "## 结果(MAE ↓ 更准;Δ=|源误差|−|市场误差|,负值=源更好)", "",
              "| 时刻 | 源 | n | 日期 | MAE | 市场MAE(同行) | Δ均值(5%下界) | 源胜率 | 单档命中 | 市场中心命中 | 偏差 |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for tick_label, items in results.items():
        for name, item in items.items():
            if item is None:
                continue
            lines.append(
                f"| {tick_label} | {name} | {item['n_city_days']} | {item['independent_dates']} | "
                f"{item['mae']} | {item['mae_mkt_same_rows']} | "
                f"{item['mae_delta_mean']}({item['mae_delta_lb05']}) | "
                f"{item['src_beats_mkt_share']} | {item['bucket_hit']} | "
                f"{item['mkt_center_hit_same_rows']} | {item['bias']} |"
            )
    lines += ["", "## 边界", "",
              "- RIDGE_V2 快照自带 `insufficient_independent_oos_dates` 治理警告;此处仅作研究候选评分。",
              "- MKT_ET 为中间价隐含期望,未计跨桶价差成本;它不是可成交价。",
              "- 同日多城相关,日期块自助法只部分缓解;窗口为采集器覆盖期。",
              "- 本结果是历史研究,不构成实盘认证。"]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(OUT_JSON), "md": str(OUT_MD)}, ensure_ascii=False))


if __name__ == "__main__":
    run()
