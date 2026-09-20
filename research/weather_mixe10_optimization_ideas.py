#!/usr/bin/env python3
"""MIXE10 optimization directions not covered by earlier runs.

Already tested elsewhere (do NOT repeat): entry hour H09-H15, reach_rate
r075/r150, model anchors AMB/ARDG, weight tilts, cost caps C10-C20,
one-city-per-day selection, ensemble-EV gates (no value), Meteoblue/Ridge
delta tilts (harmful/insufficient).

Four NEW levers, spec frozen before first run:

    NOLOCK95 / NOLOCK90
        Sell a neighbor NO leg (full remaining shares) the first tick its
        no_best_bid >= threshold, walking the no-book bid VWAP with fees.
        Rationale: NO >= 0.95 embeds near-certain win; locking frees capital
        and removes boundary-settlement risk. Tradeoff tested honestly: a
        sold NO that would have lost $1 gave up nothing; one that would have
        won gave up (1 - price).
    GATE5
        Trailing-risk pause: process chronologically; skip today's entry if
        the last <=5 TRADED calendar dates' realized BASE daily PnL sum < 0.
        Uses only settled history (paper-record observable); this is the
        documented "de-risking, never size-up" form of dynamic rebalancing.
    TOPK2
        Same-day concentration control: among gated cities of one target
        date keep only the top-2 by entry-time center_lead (ties -> city).
        Rationale: same-day cities share weather regime; 95 city-days over
        ~26 dates already cluster.
    SIZE15_LEAD08
        Market-structure size scaling: center YES 15 shares when
        center_lead >= 0.08 else 10 (neighbors stay 5 NO). Falls back to
        the 10-share plan when the 15-share plan fails any gate.

Primary hypothesis (declared): NOLOCK95 improves PnL or holds it within noise
while cutting tail risk. Secondary: GATE5 cuts worst-date p05; TOPK2/SIZE15
are exploratory.

Integrity: BASE must reproduce wdrb.simulate('MIXE10') exactly per city-day
(status and pnl); any mismatch aborts loudly. Mirror identity no_ask =
1 - yes_bid spot-checked per entry. Settlement only from winning_temp.
Paired stats: daily-mean delta vs BASE with date-block bootstrap; improvement
claimed iff lb05 > 0 (positive good); harm iff ub95 < 0.

Output: research/output/weather_mixe10_optimization_ideas.{json,md}
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import deque
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import weather_dynamic_rebalance_backtest as wdrb  # noqa: E402

DB = Path("data/weather_market_monitor.sqlite3")
OUT_JSON = Path("research/output/weather_mixe10_optimization_ideas.json")
OUT_MD = Path("research/output/weather_mixe10_optimization_ideas.md")
CENTER_SHARES_BASE = 10.0
NEIGHBOR_NO_SHARES = 5.0
REACH_RATE = 1.0


def sim_mixe10(data: wdrb.EventData, entry: dict[str, Any],
               no_lock_threshold: float | None = None) -> dict[str, Any]:
    """Faithful MIXE10 replay (verified against wdrb.simulate) plus an
    optional early-lock exit for neighbor NO legs."""
    base = {
        "city": data.event["city"],
        "target_date": data.event["target_date"],
        "winning_temp": data.winning_temp,
    }
    plan = entry["plan"]
    cash_spent = sum(leg["cost"] for leg in plan)
    positions = {leg["bucket"]: leg["shares"] for leg in plan if leg.get("side") != "NO"}
    no_positions = {leg["bucket"]: leg["shares"] for leg in plan if leg.get("side") == "NO"}
    actions = [
        {
            "tick": entry["entry_tick"].isoformat(), "action": "BUY",
            "bucket": leg["bucket"], "shares": leg["shares"],
            "side": leg.get("side", "YES"), "price": round(leg["price"], 4),
            "fee": round(leg["fee"], 4), "cash_flow": -round(leg["cost"], 4),
        }
        for leg in plan
    ]
    cash_in = 0.0
    marked_dead: dict[int, str] = {}
    for idx in range(entry["entry_idx"] + 1, len(data.slots)):
        tick = data.slots[idx]
        if not any(s > 0 for s in positions.values()) and \
                not any(s > 0 for s in no_positions.values()):
            break
        observed_max = data.observed_max_at(tick)
        exit_reasons: dict[int, str] = {}
        if observed_max is not None:
            local_dt = tick + timedelta(hours=8)
            hours_left = None
            if local_dt.strftime("%Y-%m-%d") == data.event["target_date"]:
                hours_left = max(0.0, 18.0 - (local_dt.hour + local_dt.minute / 60.0))
            reachable = (
                observed_max + REACH_RATE * hours_left
                if hours_left is not None else None
            )
            for temp in sorted(positions):
                if positions[temp] <= 0:
                    continue
                if observed_max > temp:
                    exit_reasons[temp] = "PASSED_ABOVE"
                elif reachable is not None and reachable < temp - 1e-9:
                    exit_reasons[temp] = "UNREACHABLE"
        for temp in sorted(exit_reasons):
            shares = positions[temp]
            qrow = data.quote(idx, temp)
            if qrow is None:
                continue
            vwap, _depth = wdrb.book_vwap(qrow["yes_book_json"], shares, "bid")
            if vwap is None or vwap <= wdrb.SELL_FLOOR_PRICE:
                continue
            fee = wdrb.fee_for(shares, vwap)
            cash_in += shares * vwap - fee
            positions[temp] = 0.0
            actions.append({
                "tick": tick.isoformat(), "action": "SELL_DEAD", "bucket": temp,
                "reason": exit_reasons[temp], "shares": shares,
                "price": round(vwap, 4), "fee": round(fee, 4),
                "cash_flow": round(shares * vwap - fee, 4),
            })
        if exit_reasons:
            for temp, reason in exit_reasons.items():
                if positions.get(temp, 0.0) > 0:
                    marked_dead[temp] = reason
        if no_lock_threshold is not None:
            for temp in sorted(no_positions):
                shares = no_positions[temp]
                if shares <= 0:
                    continue
                qrow = data.quote(idx, temp)
                if qrow is None or qrow["no_best_bid"] is None:
                    continue
                if qrow["no_best_bid"] < no_lock_threshold:
                    continue
                vwap, _depth = wdrb.book_vwap(qrow["no_book_json"], shares, "bid")
                if vwap is None or vwap <= wdrb.SELL_FLOOR_PRICE:
                    continue
                fee = wdrb.fee_for(shares, vwap)
                cash_in += shares * vwap - fee
                no_positions[temp] = 0.0
                actions.append({
                    "tick": tick.isoformat(), "action": "SELL_NO_LOCK", "bucket": temp,
                    "reason": f"NO_BID_GE_{no_lock_threshold}", "shares": shares,
                    "price": round(vwap, 4), "fee": round(fee, 4),
                    "cash_flow": round(shares * vwap - fee, 4),
                })

    payout = sum(s for t, s in positions.items() if s > 0 and t == data.winning_temp)
    payout += sum(s for t, s in no_positions.items() if s > 0 and t != data.winning_temp)
    return {
        **base,
        "status": "ok",
        "center": entry["center"],
        "entry_tick": entry["entry_tick"].isoformat(),
        "center_lead": round(entry.get("lead", float("nan")), 4) if entry.get("lead") is not None else None,
        "cash_spent": round(cash_spent, 4),
        "cash_in": round(cash_in, 4),
        "payout": round(payout, 4),
        "pnl": round(payout + cash_in - cash_spent, 4),
        "actions": actions,
        "sell_dead": sum(1 for a in actions if a["action"] == "SELL_DEAD"),
        "sell_no_lock": sum(1 for a in actions if a["action"] == "SELL_NO_LOCK"),
        "dead_unsold": len(marked_dead),
    }


def entry_lead(data: wdrb.EventData) -> tuple[bool, dict[str, Any]]:
    """Entry-time center_lead without buying; mirrors enter()'s ranking."""
    start = wdrb.day_start(data.target_day())
    entry_dt = start + timedelta(hours=wdrb.ENTRY_HOUR_DEFAULT)
    deadline = entry_dt + wdrb.ENTRY_DEADLINE_GAP
    entry_idx = next((i for i, s in enumerate(data.slots) if s >= entry_dt), None)
    if entry_idx is None or data.slots[entry_idx] > deadline:
        return False, {"status": "no_entry_slot"}
    mids: dict[int, float] = {}
    for temp in {wdrb.parse_bucket(label) for label in data.books[entry_idx]}:
        if temp is None:
            continue
        row = data.quote(entry_idx, temp)
        if row is None or row["yes_best_bid"] is None or row["yes_best_ask"] is None:
            continue
        spread = row["yes_best_ask"] - row["yes_best_bid"]
        if spread < 0 or spread > wdrb.MAX_LEG_SPREAD:
            continue
        mids[temp] = (row["yes_best_bid"] + row["yes_best_ask"]) / 2.0
    ranked = sorted(mids.items(), key=lambda item: (-item[1], item[0]))
    if len(ranked) < 2:
        return False, {"status": "not_enough_exact_buckets"}
    return True, {
        "entry_idx": entry_idx, "entry_tick": data.slots[entry_idx],
        "center": ranked[0][0], "lead": ranked[0][1] - ranked[1][1],
    }


def main() -> None:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    universe = wdrb.load_universe(db)

    days: list[dict] = []
    integrity_failures: list[str] = []
    mirror_failures = 0
    excluded: dict[str, int] = {}
    for event in universe:
        try:
            data = wdrb.EventData(db, event)
            ref = wdrb.simulate(data, "MIXE10")
        except Exception:  # noqa: BLE001 - unusable context counts as exclusion
            excluded["context_unusable"] = excluded.get("context_unusable", 0) + 1
            continue
        ok, info = entry_lead(data)
        if not ok:
            excluded[info["status"]] = excluded.get(info["status"], 0) + 1
            continue
        entry10 = wdrb.enter(
            data, no_neighbors=True, center_yes_shares=CENTER_SHARES_BASE
        )
        if not entry10.get("ok"):
            excluded[entry10["status"]] = excluded.get(entry10["status"], 0) + 1
            continue
        entry15 = wdrb.enter(
            data, no_neighbors=True, center_yes_shares=15.0
        )
        for leg_bucket in entry10["legs"]:
            lrow = data.quote(entry10["entry_idx"], leg_bucket)
            bid = (lrow or {}).get("yes_best_bid")
            no_ask = (lrow or {}).get("no_best_ask")
            if bid is not None and no_ask is not None \
                    and abs(bid + no_ask - 1.0) > 1e-6:
                mirror_failures += 1
                break

        record = {
            "city": event["city"],
            "target_date": event["target_date"],
            "lead": info["lead"],
            "entry_tick": info["entry_tick"].isoformat(),
            "base_ref": {"status": ref["status"], "pnl": ref["pnl"]},
            "sim_base": sim_mixe10(data, entry10),
            "sim_nolock95": sim_mixe10(data, entry10, 0.95),
            "sim_nolock90": sim_mixe10(data, entry10, 0.90),
            "sim_size15": sim_mixe10(
                data,
                entry15 if (entry15.get("ok") and info["lead"] >= 0.08) else entry10,
            ),
        }
        local, remote = record["sim_base"], record["base_ref"]
        if local["pnl"] != remote["pnl"] or (
            (local["pnl"] is None) != (remote["pnl"] is None)
        ):
            integrity_failures.append(f'{event["city"]} {event["target_date"]}')
        days.append(record)
    db.close()
    if integrity_failures:
        raise SystemExit(
            "INTEGRITY FAILURE: local replay diverges on "
            f"{len(integrity_failures)} days: {integrity_failures[:5]}"
        )

    base_by_date: dict[str, float] = {}
    for r in days:
        base_by_date.setdefault(r["target_date"], 0.0)
        base_by_date[r["target_date"]] += r["sim_base"]["pnl"]

    def variant_days(name: str) -> list[tuple[dict, dict]]:
        """(record, sim) after applying each selection rule."""
        out = []
        if name == "TOPK2":
            by_date: dict[str, list[dict]] = {}
            for r in days:
                by_date.setdefault(r["target_date"], []).append(r)
            return [
                (r, r["sim_base"])
                for rows in by_date.values()
                for r in sorted(rows, key=lambda x: (-x["lead"], x["city"]))[:2]
            ]
        key = {
            "BASE": "sim_base", "NOLOCK95": "sim_nolock95",
            "NOLOCK90": "sim_nolock90", "SIZE15_LEAD08": "sim_size15",
        }[name]
        return [(r, r[key]) for r in days]

    def gate5_selection() -> list[tuple[dict, dict]]:
        chrono = sorted(days, key=lambda r: (r["target_date"], r["entry_tick"]))
        traded_dates: list[str] = []
        daily: dict[str, float] = {}
        out = []
        for r in chrono:
            recent = [daily[d] for d in traded_dates[-5:]]
            if len(recent) == 5 and sum(recent) < 0:
                continue
            out.append((r, r["sim_base"]))
            d = r["target_date"]
            daily[d] = daily.get(d, 0.0) + r["sim_base"]["pnl"]
            if not traded_dates or traded_dates[-1] != d:
                traded_dates.append(d)
        return out

    variants: dict[str, list[tuple[dict, dict]]] = {
        "BASE": variant_days("BASE"),
        "NOLOCK95": variant_days("NOLOCK95"),
        "NOLOCK90": variant_days("NOLOCK90"),
        "GATE5": gate5_selection(),
        "TOPK2": variant_days("TOPK2"),
        "SIZE15_LEAD08": variant_days("SIZE15_LEAD08"),
    }

    def summarize(pairs: list[tuple[dict, dict]]) -> dict:
        sims = [s for _r, s in pairs]
        daily: dict[str, float] = {}
        for _r, s in pairs:
            daily[s["target_date"]] = daily.get(s["target_date"], 0.0) + s["pnl"]
        cost = sum(s["cash_spent"] for _r, s in pairs)
        pnl = sum(s["pnl"] for _r, s in pairs)
        boot = wdrb.date_block_bootstrap(daily)
        return {
            "traded_city_days": len(sims),
            "net_cost": round(cost, 3),
            "pnl": round(pnl, 3),
            "roi": round(pnl / cost, 4) if cost else None,
            "hit_rate": round(sum(1 for _r, s in pairs if s["pnl"] > 0) / len(sims), 4) if sims else None,
            "daily_mean": boot["mean_diff"],
            "daily_p05": boot["mean_lb05"],
            "pos_neg_dates": f"{boot['positive_dates']}/{boot['negative_dates']}",
        }

    def paired(pairs_a: list, pairs_b: list) -> dict | None:
        """Daily-sum difference over the UNION of traded dates; a date a
        variant skips counts as 0 for that variant."""
        def daily_sums(pairs: list) -> dict[str, float]:
            out: dict[str, float] = {}
            for _r, s in pairs:
                out[s["target_date"]] = out.get(s["target_date"], 0.0) + s["pnl"]
            return out

        sums_a, sums_b = daily_sums(pairs_a), daily_sums(pairs_b)
        dates = sorted(set(sums_a) | set(sums_b))
        if not dates:
            return None
        return wdrb.date_block_bootstrap(
            {d: sums_a.get(d, 0.0) - sums_b.get(d, 0.0) for d in dates}
        )

    summary = {name: summarize(pairs) for name, pairs in variants.items()}
    paired_stats = {
        name: paired(variants[name], variants["BASE"])
        for name in ("NOLOCK95", "NOLOCK90", "GATE5", "TOPK2", "SIZE15_LEAD08")
    }

    report = {
        "generated_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "spec": "see module docstring; frozen before first run",
        "universe_city_days": len(universe),
        "gated_city_days": len(days),
        "excluded": excluded,
        "mirror_failure_days": mirror_failures,
        "summary": summary,
        "paired_daily_delta_vs_base": paired_stats,
        "detail": days,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# MIXE10 未测优化方向探索", "",
        f"> 生成:{report['generated_at_utc']};宇宙 {len(universe)} 城-日,"
        f"过门槛 {len(days)},排除 {json.dumps(excluded, ensure_ascii=False)};"
        f"镜像恒等失败天数 {mirror_failures}。"
        "完整性:本地重放与冻结引擎逐日一致(不一致即中止)。", "",
        "| 变体 | 成交城-日 | 净成本 | 净PnL | ROI | 胜率 | 日均PnL | 日PnL 5%下界 | 正/负日期 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, s in summary.items():
        lines.append(
            f"| {name} | {s['traded_city_days']} | {s['net_cost']} | {s['pnl']} | "
            f"{round(s['roi'] * 100, 1) if s['roi'] is not None else '-'}% | "
            f"{s['hit_rate']} | {s['daily_mean']} | {s['daily_p05']} | "
            f"{s['pos_neg_dates']} |"
        )
    lines += ["", "### 配对日差(vs BASE;正=更好;改善成立需 5% 下界>0)", "",
              "| 变体 | 平均日差 | 5%下界 | 正/负日期 |", "|---|---:|---:|---:|"]
    for name, stat in paired_stats.items():
        if stat is None:
            lines.append(f"| {name} | - | - | - |")
            continue
        lines.append(
            f"| {name} | {stat['mean_diff']} | {stat['mean_lb05']} | "
            f"{stat['positive_dates']}/{stat['negative_dates']} |"
        )
    n_locks = sum(r["sim_nolock95"]["sell_no_lock"] for r in days)
    lines += ["", "## 备注", "",
              f"- NOLOCK95 触发锁利次数:{n_locks}。",
              "- GATE5 用 BASE 已结算日历日的每日 PnL 作为可观察历史(声明过的因果代理)。",
              "- 同日多城相关,自助法只部分缓解;窗口为引擎既有覆盖期。",
              "- 本结果是历史研究,不构成实盘认证。"]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(OUT_JSON), "md": str(OUT_MD)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
