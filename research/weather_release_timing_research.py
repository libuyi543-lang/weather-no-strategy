#!/usr/bin/env python3
"""Model-release-window timing study (point-in-time, no lookahead).

Spec frozen before first run (2026-08-25), motivated by the audited Hans323
pattern (edge = ordering at ECMWF publication windows before market makers
reprice) and the unexplained H13+ entry-hour collapse found earlier
(entering after 13:00 Beijing loses -25.9% ROI with center-hit crashing to
32%).

Question
    With HOURLY snapshots we cannot replay minute-level front-running.
    What we CAN test: conditional on the frozen gates passing at the
    11:00-Beijing entry, does OUTCOME QUALITY differ by how FRESH the
    latest forecast information was at entry?
      FRESH : last meaningful revision arrived <= 2h before entry tick
      MID   : > 2h and <= 6h
      STALE : > 6h (or none all day)
    Mechanism check: Hans323's edge requires the market to lag the model.
    If hourly books have already absorbed revisions, group outcomes are
    equal and release-window timing adds nothing at this granularity -
    that would be a clean negative consistent with the semi-strong
    efficiency finding.

Revision signals (both declared, point-in-time)
    WINDY : windy_forecasts.forecast_max_c series (fetched_at order);
            revision when |v_t - v_prev| >= 0.2C at the first slot showing it.
    ENS   : ensemble_forecasts member-mean series (slot_utc order, latest ok
            row per slot); same 0.2C rule on the mean.
    Freshness = min over both signals of (entry_tick - last_revision_time).

Part A (descriptive)
    - Beijing-hour histogram of meaningful WINDY/ENS revisions (already
      known for WINDY: continuous flow peaking 08-14 local).
    - ENS dissemination lag proxy: slot_utc hour minus nominal run hour
      cannot be computed (model_run_time_utc NULL); instead report the
      hour-of-day histogram of ENS mean REVISION events as its empirical
      publication rhythm.
    - Freshness distribution of the 210-city-day universe at entry.

Part B (outcome quality by freshness group)
    For S0 and MIXE10 (frozen structures via weather_dynamic_rebalance_
    backtest simulate()): city-day counts, ROI, hit rate, daily-mean PnL,
    date-block 5% lower bound vs zero, plus market calibration per group:
    claimed P(center) = YES mid of the market's leading exact bucket at
    entry vs realized center-hit rate (undervaluation gap).
    Declared reading discipline: groups are NOT pairable (different days);
    differences are reported whole with date-block bounds, no selection.

Anti-lookahead: revision times come from fetched_at/slot stamps only;
    books at the entry slot; settlement from events.winning_range.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import weather_dynamic_rebalance_backtest as wdrb  # noqa: E402

DB = Path("data/weather_market_monitor.sqlite3")
OUT_JSON = Path("research/output/weather_release_timing_research.json")
OUT_MD = Path("research/output/weather_release_timing_research.md")
ENTRY_HOUR = wdrb.ENTRY_HOUR_DEFAULT
REV_THRESHOLD_C = 0.2
FRESH_HOURS = 2.0
STALE_HOURS = 6.0


def parse_ts(v: str) -> datetime:
    return datetime.fromisoformat(v)


def load_windy_revisions(db: sqlite3.Connection) -> dict[tuple[str, str], list[datetime]]:
    """Per (station, target_date): times when forecast_max_c changed by
    >= REV_THRESHOLD_C versus the previous distinct value."""
    revs: dict[tuple[str, str], list[datetime]] = {}
    prev_val: dict[tuple[str, str], float] = {}
    for r in db.execute(
        """
        SELECT station_id, target_date, fetched_at_utc, forecast_max_c
        FROM windy_forecasts WHERE status='ok' AND forecast_max_c IS NOT NULL
        ORDER BY station_id, target_date, fetched_at_utc
        """,
    ):
        key = (r["station_id"], r["target_date"])
        val = float(r["forecast_max_c"])
        prev = prev_val.get(key)
        if prev is not None and abs(val - prev) >= REV_THRESHOLD_C:
            revs.setdefault(key, []).append(parse_ts(r["fetched_at_utc"]))
        prev_val[key] = val
    return revs


def load_ens_revisions(db: sqlite3.Connection) -> dict[tuple[str, str], list[datetime]]:
    """Same rule on the per-slot ensemble member mean."""
    means: dict[tuple[str, str], dict[str, tuple[float, int]]] = {}
    for r in db.execute(
        """
        SELECT station_id, target_date, slot_utc, member_maxima_json
        FROM ensemble_forecasts
        WHERE source='open_meteo_ensemble' AND model='ecmwf_ifs025' AND status='ok'
        ORDER BY station_id, target_date, slot_utc
        """,
    ):
        vals = [float(m["maxC"]) for m in json.loads(r["member_maxima_json"])
                if isinstance(m, dict) and m.get("maxC") is not None]
        if not vals:
            continue
        key = (r["station_id"], r["target_date"])
        slot = r["slot_utc"]
        total, n = means.get(key, {}).get(slot, (0.0, 0))
        means.setdefault(key, {})[slot] = (total + sum(vals), n + len(vals))
    revs: dict[tuple[str, str], list[datetime]] = {}
    prev: dict[tuple[str, str], tuple[str, float]] = {}
    for key, slots in sorted(means.items()):
        for slot in sorted(slots):
            total, n = slots[slot]
            mu = total / n
            p = prev.get(key)
            if p is not None and abs(mu - p[1]) >= REV_THRESHOLD_C:
                revs.setdefault(key, []).append(parse_ts(slot))
            prev[key] = (slot, mu)
    return revs


def freshness_at(revs: list[datetime], tick: datetime) -> float:
    idx = bisect_right(revs, tick)
    if idx == 0:
        return float("inf")
    return (tick - revs[idx - 1]).total_seconds() / 3600.0


def group_of(freshness: float) -> str:
    if freshness <= FRESH_HOURS:
        return "FRESH"
    if freshness <= STALE_HOURS:
        return "MID"
    return "STALE"


class EntryContext:
    def __init__(self, db: sqlite3.Connection, event: dict):
        start = wdrb.day_start(event["target_date"])
        entry_dt = start + timedelta(hours=ENTRY_HOUR)
        deadline = entry_dt + wdrb.ENTRY_DEADLINE_GAP
        row = db.execute(
            """
            SELECT slot_utc FROM market_snapshots WHERE event_id=? AND slot_utc>=?
              AND slot_utc<=? ORDER BY slot_utc LIMIT 1
            """,
            (event["event_id"], entry_dt.isoformat(), deadline.isoformat()),
        ).fetchone()
        self.slot = row[0] if row else None
        self.center_mid: float | None = None
        self.center_temp: int | None = None
        if self.slot is not None:
            best = None
            for r in db.execute(
                """
                SELECT outcome_range, yes_best_bid, yes_best_ask
                FROM market_snapshots WHERE event_id=? AND slot_utc=?
                """,
                (event["event_id"], self.slot),
            ):
                temp = wdrb.parse_bucket(r["outcome_range"])
                bid, ask = r["yes_best_bid"], r["yes_best_ask"]
                if temp is None or bid is None or ask is None:
                    continue
                spread = ask - bid
                if spread < 0 or spread > wdrb.MAX_LEG_SPREAD:
                    continue
                mid = (bid + ask) / 2.0
                if best is None or (-mid, temp) < (-best[1], best[0]):
                    best = (temp, mid)
            if best is not None:
                self.center_temp, self.center_mid = best


def main() -> None:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    universe = wdrb.load_universe(db)
    windy_revs = load_windy_revisions(db)
    ens_revs = load_ens_revisions(db)

    records: list[dict] = []
    for event in universe:
        data = wdrb.EventData(db, event)
        ctx = EntryContext(db, event)
        base = {
            "city": event["city"],
            "target_date": event["target_date"],
            "winning_temp": data.winning_temp,
            "center_temp": ctx.center_temp,
            "center_mid": ctx.center_mid,
        }
        # freshness from BOTH signals, evaluated at the actual entry tick
        probe = wdrb.enter(data)
        if not probe.get("ok"):
            records.append({**base, "status": probe["status"], "group": None})
            continue
        tick = probe["entry_tick"]
        f_windy = freshness_at(windy_revs.get((event["station_id"], event["target_date"]), []), tick)
        f_ens = freshness_at(ens_revs.get((event["station_id"], event["target_date"]), []), tick)
        freshness = min(f_windy, f_ens)
        rec = {
            **base,
            "status": "ok",
            "entry_tick": tick.isoformat(),
            "freshness_h": round(min(freshness, 72.0), 3),
            "freshness_windy_h": round(min(f_windy, 72.0), 3),
            "freshness_ens_h": round(min(f_ens, 72.0), 3),
            "group": group_of(freshness),
            "sims": {
                "S0": wdrb.simulate(data, "S0"),
                "MX10": wdrb.simulate(data, "MIXE10"),
            },
        }
        records.append(rec)
    db.close()

    # ---- Part A descriptives -------------------------------------------
    def rev_hour_hist(revs: dict[tuple[str, str], list[datetime]]) -> dict[int, int]:
        hist: dict[int, int] = {h: 0 for h in range(24)}
        for times in revs.values():
            for t in times:
                hist[(t.hour + 8) % 24] += 1
        return hist

    part_a = {
        "windy_rev_beijing_hour_hist": rev_hour_hist(windy_revs),
        "ens_rev_beijing_hour_hist": rev_hour_hist(ens_revs),
        "freshness_group_counts": {},
        "gate_skipped": {},
    }
    for r in records:
        if r["status"] == "ok":
            part_a["freshness_group_counts"][r["group"]] = \
                part_a["freshness_group_counts"].get(r["group"], 0) + 1
        else:
            part_a["gate_skipped"][r["status"]] = \
                part_a["gate_skipped"].get(r["status"], 0) + 1

    # ---- Part B outcomes by group ----------------------------------------
    def summarize(rows: list[dict], sim_key: str) -> dict:
        ok = [r["sims"][sim_key] for r in rows if r["sims"][sim_key]["status"] == "ok"]
        daily: dict[str, float] = {}
        for s in ok:
            daily[s["target_date"]] = daily.get(s["target_date"], 0.0) + s["pnl"]
        cost = sum(s["cash_spent"] - s["cash_in"] for s in ok)
        pnl = sum(s["pnl"] for s in ok)
        boot = wdrb.date_block_bootstrap(daily) if daily else {
            "mean_lb05": 0.0, "mean_diff": 0.0, "positive_dates": 0, "negative_dates": 0}
        return {
            "city_days": len(ok),
            "net_cost": round(cost, 3),
            "pnl": round(pnl, 3),
            "roi": round(pnl / cost, 4) if cost else None,
            "daily_mean": boot["mean_diff"],
            "lb05": boot["mean_lb05"],
            "pos_neg": f'{boot["positive_dates"]}/{boot["negative_dates"]}',
        }

    def calibration(rows: list[dict]) -> dict:
        scored = [
            r for r in rows
            if r["center_temp"] is not None and r["winning_temp"] is not None
        ]
        if not scored:
            return {}
        claimed = statistics.fmean(r["center_mid"] for r in scored)
        realized = statistics.fmean(
            1.0 if r["center_temp"] == r["winning_temp"] else 0.0 for r in scored
        )
        return {"n": len(scored), "claimed_p_center": round(claimed, 4),
                "realized_center_hit": round(realized, 4),
                "undervaluation_gap": round(realized - claimed, 4)}

    part_b: dict[str, dict] = {}
    for group in ("FRESH", "MID", "STALE"):
        rows = [r for r in records if r["group"] == group]
        part_b[group] = {
            "S0": summarize(rows, "S0"),
            "MX10": summarize(rows, "MX10"),
            "calibration_S0_days": calibration(
                [r for r in rows if r["sims"]["S0"]["status"] == "ok"]),
            "calibration_gate_days": calibration(
                [r for r in rows if r["sims"]["MX10"]["status"] == "ok"]),
        }

    # freshness percentiles among gated days
    fr = sorted(r["freshness_h"] for r in records if r["status"] == "ok")
    if fr:
        part_a["freshness_percentiles_gated"] = {
            "p25": fr[len(fr) // 4], "p50": fr[len(fr) // 2],
            "p75": fr[3 * len(fr) // 4], "mean": round(statistics.fmean(fr), 2),
        }

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe_city_days": len(universe),
        "part_a_descriptives": part_a,
        "part_b_by_freshness_group": part_b,
        "detail": records,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 模型发布窗口对齐研究(Hans323 式时点,小时级可测版)", "",
        f"> 生成:{report['generated_at_utc']};样本 {len(universe)} 城-日;"
        f"修订信号:Windy 点预报与集合均值,|Δ|≥{REV_THRESHOLD_C}°C;"
        f"FRESH≤{int(FRESH_HOURS)}h / MID≤{int(STALE_HOURS)}h / STALE>6h,"
        f"以入场时刻(11:00 北京)倒推。", "",
        "## A. 描述", "",
        f"- 门槛通过日的信息新鲜度:`{json.dumps(part_a.get('freshness_percentiles_gated', {}), ensure_ascii=False)}` 小时",
        f"- 分组计数:`{json.dumps(part_a['freshness_group_counts'], ensure_ascii=False)}`;"
        f" 门槛未通过:`{json.dumps(part_a['gate_skipped'], ensure_ascii=False)}`",
        "",
        "### 集合均值修订的北京小时分布(ECMWF 经 Open-Meteo 的经验落地节奏)", "",
        "| 小时 | 修订数 |", "|---:|---:|",
    ]
    ens_hist = part_a["ens_rev_beijing_hour_hist"]
    mx = max(ens_hist.values()) if any(ens_hist.values()) else 1
    for h in range(24):
        lines.append(f"| {h:02d}:00 | {ens_hist[h]} {'#' * (ens_hist[h] * 30 // mx)} |")

    lines += ["", "## B. 按信息新鲜度分组的结局质量", "",
              "| 组 | 结构 | 城-日 | 净成本 | 净PnL | ROI | 日均PnL | 5%下界 | 正/负日期 | 声称P(中心) | 实际命中率 | 低估差 |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for group, d in part_b.items():
        for sim in ("S0", "MX10"):
            s = d[sim]
            cal = d[f"calibration_{'S0_days' if sim == 'S0' else 'gate_days'}"]
            lines.append(
                f"| {group} | {sim} | {s['city_days']} | {s['net_cost']} | {s['pnl']} | "
                f"{round((s['roi'] or 0) * 100, 1)}% | {s['daily_mean']} | {s['lb05']} | "
                f"{s['pos_neg']} | {cal.get('claimed_p_center', '-')} | "
                f"{cal.get('realized_center_hit', '-')} | {cal.get('undervaluation_gap', '-')} |"
            )
    lines += ["", "## 边界", "",
              "- 小时级快照无法复现分钟级抢跑;本研究的零假设是'各组无差异'(市场一小时内已重定价)。",
              "- 分组非配对(不同日期集合),日期块自助法只部分缓解同日相关。",
              "- 本结果是历史研究,不构成实盘认证。"]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(OUT_JSON), "md": str(OUT_MD)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
