#!/usr/bin/env python3
"""Full-board arbitrage scan + neighbor-NO counterfactual (point-in-time).

Spec frozen before first run (2026-08-24):

A. Full-board scan over every snapshot slot of the 210-city-day universe
   - BUY side: cost of one share of EVERY listed outcome at best ask;
     margin/share = 1 - sum(asks) - 0.05*(sum(asks) - sum(asks^2)).
     Positive margin is executable risk-free profit before depth/size
     limits (Polymarket min order 5 shares checked separately).
   - SELL side (mint complete set for $1, sell every outcome at best bid):
     margin/share = sum(bids) - 1 - 0.05*(sum(bids) - sum(bids^2)).
   Reported as window counts per city-day and margin distribution; no
   strategy simulated (descriptive first).

B. Neighbor-NO counterfactual on the frozen ladder entry (11:00 Beijing,
   gate identical to weather_dynamic_rebalance_backtest S0): for each
   gate-qualified city-day buy 5 NO shares on EACH exact neighbor leg at
   the same entry tick. Executable NO price mirrors the YES book:
   p_no_vwap = 1 - bid_side_VWAP(5 shares). Fee = shares*0.05*p*(1-p) on
   the NO price. NO pays $1 iff that bucket does NOT win.
   Structures reported:
     N55        : the two NO legs standalone
     S0+N55     : frozen 5/15/5 YES ladder plus both NO legs
     YC15+N55   : center-only 15 YES plus both NO legs
   All PnL hold-to-settlement; no exits. Paired date-block bootstrap vs
   S0 and C15 uses identical city-days.

Anti-lookahead: only the entry slot's own book rows are read.
"""

from __future__ import annotations

import json
import random
import sqlite3
import statistics
from bisect import bisect_right
from datetime import datetime, timedelta
from pathlib import Path

DB = Path("data/weather_market_monitor.sqlite3")
DETAIL_JSON = Path("research/output/weather_dynamic_rebalance_backtest.json")
OUT_JSON = Path("research/output/weather_spread_arb_research.json")
OUT_MD = Path("research/output/weather_spread_arb_research.md")
FEE = 0.05
CITIES = ("Shanghai", "Beijing", "Guangzhou", "Qingdao", "Wuhan", "Chongqing", "Chengdu")
BOOTSTRAP_ITERS, BOOTSTRAP_SEED = 10000, 42


def ts(v: str) -> datetime:
    return datetime.fromisoformat(v)


def parse_ts_value(value: str) -> datetime:
    return ts(value)


def fee(shares: float, p: float) -> float:
    return shares * FEE * p * (1 - p)


def load_universe(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        """
        SELECT event_id, city, target_date, station_id FROM events
        WHERE resolved_at_utc IS NOT NULL AND winning_range IS NOT NULL
          AND city IN (?,?,?,?,?,?,?)
        ORDER BY target_date, city
        """,
        CITIES,
    ).fetchall()
    out = []
    for row in rows:
        slots = db.execute(
            "SELECT COUNT(DISTINCT slot_utc) FROM market_snapshots WHERE event_id=?",
            (row["event_id"],),
        ).fetchone()[0]
        metar = db.execute(
            """
            SELECT COUNT(*) FROM weather_observations
            WHERE station_id=? AND sample_local_date=? AND source='metar'
              AND status='ok' AND temperature_c IS NOT NULL
            """,
            (row["station_id"], row["target_date"]),
        ).fetchone()[0]
        if slots >= 100 and metar >= 20:
            out.append(dict(row))
    return out


def scan_full_board(db: sqlite3.Connection, universe: list[dict]) -> dict:
    buy_windows = []  # (city, date, slot, margin_per_share)
    sell_windows = []
    for ev in universe:
        rows = db.execute(
            "SELECT slot_utc, outcome_range, yes_best_bid, yes_best_ask "
            "FROM market_snapshots WHERE event_id=? ORDER BY slot_utc",
            (ev["event_id"],),
        ).fetchall()
        by_slot: dict[str, dict[str, tuple[float, float]]] = {}
        for r in rows:
            if r["yes_best_bid"] is None or r["yes_best_ask"] is None:
                continue
            by_slot.setdefault(r["slot_utc"], {})[r["outcome_range"]] = (
                float(r["yes_best_bid"]), float(r["yes_best_ask"]),
            )
        if not by_slot:
            continue
        # Full coverage requires every outcome ever listed for the event.
        all_ranges = set().union(*(set(v) for v in by_slot.values()))
        for slot, quotes in by_slot.items():
            if len(quotes) < len(all_ranges):
                continue
            asks = [q[1] for q in quotes.values()]
            bids = [q[0] for q in quotes.values()]
            sa, sb = sum(asks), sum(bids)
            buy_margin = 1 - sa - FEE * (sa - sum(a * a for a in asks))
            sell_margin = sb - 1 - FEE * (sb - sum(b * b for b in bids))
            stamp = parse_ts_value(slot)
            if buy_margin > 0:
                buy_windows.append((ev["city"], ev["target_date"], stamp, round(buy_margin, 4)))
            if sell_margin > 0:
                sell_windows.append((ev["city"], ev["target_date"], stamp, round(sell_margin, 4)))
    def dist(windows):
        margins = sorted(w[3] for w in windows)
        if not margins:
            return {"count": 0}
        return {
            "count": len(margins),
            "city_days": len({(w[0], w[1]) for w in windows}),
            "dates": len({w[1] for w in windows}),
            "max_margin": margins[-1],
            "p90_margin": margins[int(0.9 * len(margins))],
            "median_margin": margins[len(margins) // 2],
            "min_margin": margins[0],
        }
    return {"buy_under_par": dist(buy_windows), "sell_over_par": dist(sell_windows),
            "_buy_rows": buy_windows, "_sell_rows": sell_windows}


def bid_vwap(book_json: str | None, shares: float) -> float | None:
    if not book_json or shares <= 0:
        return None
    try:
        payload = json.loads(book_json)
    except (TypeError, ValueError):
        return None
    levels = payload.get("bids") if isinstance(payload, dict) else payload
    cleaned = []
    for lv in levels or []:
        if isinstance(lv, dict):
            price, size = float(lv.get("price", 0)), float(lv.get("size", 0))
        elif isinstance(lv, (list, tuple)) and len(lv) >= 2:
            price, size = float(lv[0]), float(lv[1])
        else:
            continue
        if price > 0 and size > 0:
            cleaned.append((price, size))
    if not cleaned:
        return None
    cleaned.sort(key=lambda x: x[0], reverse=True)
    remaining, cost = shares, 0.0
    for price, size in cleaned:
        take = min(remaining, size)
        cost += take * price
        remaining -= take
        if remaining <= 1e-9:
            break
    return None if remaining > 1e-9 else cost / shares


def neighbor_no_counterfactual(db: sqlite3.Connection, detail: list[dict]) -> list[dict]:
    """For each gate-qualified city-day: 5 NO shares on each neighbor."""
    results = []
    for row in detail:
        if row["status"] != "ok":
            continue
        tick = parse_ts_value(row["entry_tick"])
        legs = row["legs"]
        neighbors = [t for t in legs if t != row["center"]]
        no_pnls = {}
        no_cost = 0.0
        for temp in neighbors:
            book = db.execute(
                """
                SELECT yes_book_json FROM market_snapshots
                WHERE event_id=? AND slot_utc=? AND outcome_range=?
                """,
                (_EVENT_IDS[(row["city"], row["target_date"])], _slot_str(db, row, tick), f"{temp}°C"),
            ).fetchone()
            vwap_bid = bid_vwap(book["yes_book_json"] if book else None, 5.0)
            if vwap_bid is None:
                no_pnls[temp] = None
                continue
            p_no = 1 - vwap_bid
            cost = 5.0 * p_no
            no_cost += cost + fee(5.0, p_no)
            payout = 5.0 if row["winning_temp"] != temp else 0.0
            no_pnls[temp] = payout - cost - fee(5.0, p_no)
        vals = [v for v in no_pnls.values() if v is not None]
        if len(vals) != len(neighbors):
            continue
        results.append({
            "city": row["city"], "target_date": row["target_date"],
            "center": row["center"],
            "s0_pnl": row["pnl"], "cash_spent": row.get("cash_spent", 0.0),
            "no_cost": round(no_cost, 4),
            "no_lower": no_pnls[min(neighbors)],
            "no_upper": no_pnls[max(neighbors)],
            "n55": sum(vals),
        })
    return results


def _slot_str(db: sqlite3.Connection, row: dict, tick: datetime) -> str:
    """Snapshot timestamps are stored exactly as returned by SQLite; match
    on the raw string seen at that slot."""
    raw = db.execute(
        "SELECT DISTINCT slot_utc FROM market_snapshots WHERE event_id=?",
        (_EVENT_IDS[(row["city"], row["target_date"])],),
    ).fetchall()
    for (s,) in raw:
        if parse_ts_value(s) == tick:
            return s
    raise KeyError(f"slot not found for {row['city']} {row['target_date']} {tick}")


_EVENT_IDS: dict[tuple[str, str], str] = {}


def date_block_bootstrap(daily: dict[str, float]) -> dict:
    dates = sorted(daily)
    values = [daily[d] for d in dates]
    if not values:
        return {"mean": 0.0, "lb05": 0.0, "pos": 0, "neg": 0}
    rng = random.Random(BOOTSTRAP_SEED)
    means = []
    for _ in range(BOOTSTRAP_ITERS):
        means.append(statistics.fmean(values[rng.randrange(len(values))] for _ in values))
    means.sort()
    return {
        "mean": round(statistics.fmean(values), 4),
        "lb05": round(means[int(0.05 * len(means))], 4),
        "pos": sum(1 for v in values if v > 0),
        "neg": sum(1 for v in values if v < 0),
    }


def paired_diff(a: list[dict], b: list[dict], key_a: str, key_b: str) -> dict:
    ka = {(r["target_date"], r["city"]): r[key_a] for r in a}
    kb = {(r["target_date"], r["city"]): r[key_b] for r in b}
    daily: dict[str, float] = {}
    for k, va in ka.items():
        if k in kb:
            d = k[0]
            daily[d] = daily.get(d, 0.0) + (va - kb[k])
    return date_block_bootstrap(daily)


def summarize(rows: list[dict], pnl_key: str, cost_key: str | None = None) -> dict:
    ok = rows
    total_pnl = sum(r[pnl_key] for r in ok)
    total_cost = sum(r[cost_key] for r in ok) if cost_key else None
    daily: dict[str, float] = {}
    for r in ok:
        daily[r["target_date"]] = daily.get(r["target_date"], 0.0) + r[pnl_key]
    boot = date_block_bootstrap(daily)
    wins = sum(1 for r in ok if r[pnl_key] > 0)
    out = {
        "city_days": len(ok),
        "total_pnl": round(total_pnl, 2),
        "daily_mean": boot["mean"],
        "lb05": boot["lb05"],
        "pos_neg_dates": f"{boot['pos']}/{boot['neg']}",
        "win_rate_city_day": round(wins / len(ok), 4) if ok else None,
    }
    if cost_key:
        out["roi"] = round(total_pnl / total_cost, 4) if total_cost else None
    return out


def main() -> None:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    report_src = json.loads(DETAIL_JSON.read_text())
    s0_detail = report_src["detail"]["S0|all_eligible"]
    c15_detail = {
        (r["target_date"], r["city"]): r
        for r in report_src["detail"]["C15|all_eligible"] if r["status"] == "ok"
    }

    universe = load_universe(db)
    for ev in universe:
        _EVENT_IDS[(ev["city"], ev["target_date"])] = ev["event_id"]

    board = scan_full_board(db, universe)

    cf = neighbor_no_counterfactual(db, s0_detail)
    db.close()

    structures = {
        "N55_standalone": summarize(cf, "n55", "no_cost"),
    }
    # Composed packages: reuse stored S0/C15 accounting plus actual NO cost.
    s0_plus = [
        {**r, "pnl": r["s0_pnl"] + r["n55"], "cost": r["cash_spent"] + r["no_cost"]}
        for r in cf
    ]
    yc_plus = []
    for r in cf:
        c = c15_detail.get((r["target_date"], r["city"]))
        if c:
            yc_plus.append({**r, "pnl": c["pnl"] + r["n55"], "cost": c["cash_spent"] + r["no_cost"]})
    structures["S0_plus_N55"] = summarize(s0_plus, "pnl", "cost")
    structures["YC15_plus_N55"] = summarize(yc_plus, "pnl", "cost")

    comparisons = {
        "N55_vs_0": paired_diff(cf, [{"target_date": r["target_date"], "city": r["city"], "zero": 0.0} for r in cf], "n55", "zero") if cf else {},
        "S0+N55_vs_S0": paired_diff(s0_plus, cf, "pnl", "s0_pnl"),
        "YC15+N55_vs_C15": paired_diff(
            yc_plus,
            [c15_detail[(r["target_date"], r["city"])] for r in yc_plus],
            "pnl", "pnl",
        ),
        "YC15+N55_vs_S0": paired_diff(yc_plus, cf, "pnl", "s0_pnl"),
    }

    board_out = {k: v for k, v in board.items() if not k.startswith("_")}
    out = {
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "universe_city_days": len(universe),
        "full_board_scan": board_out,
        "neighbor_no_sample": len(cf),
        "structures": structures,
        "paired_comparisons": comparisons,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2))

    lines = [
        "# 全板套利扫描 + 邻腿NO反事实", "",
        f"> 生成:{out['generated_at_utc']};样本 {len(universe)} 城-日;费用 0.05·p·(1−p);NO价格=1−YES买盘VWAP。", "",
        "## A. 全板套利窗口(所有桶同侧总价偏离1)", "",
        "| 方向 | 窗口数 | 涉及城-日 | 中位边际 | P90 | 最大 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for key, name in (("buy_under_par", "全买<1"), ("sell_over_par", "全卖>1")):
        d = board_out[key]
        if d.get("count"):
            lines.append(f"| {name} | {d['count']} | {d['city_days']} | {d['median_margin']} | {d['p90_margin']} | {d['max_margin']} |")
        else:
            lines.append(f"| {name} | 0 | 0 | - | - | - |")
    lines += ["", "## B. 邻腿NO结构(门槛日,持有到结算)", "", "| 结构 | 城-日 | 总PnL | ROI | 日均 | 5%下界 | 正/负日期 |",
              "|---|---:|---:|---:|---:|---:|---|"]
    for name, s in structures.items():
        roi = f"{s['roi']*100:+.1f}%" if s.get("roi") is not None else "-"
        lines.append(f"| {name} | {s['city_days']} | {s['total_pnl']} | {roi} | {s['daily_mean']} | {s['lb05']} | {s['pos_neg_dates']} |")
    lines += ["", "## 配对差", "", "| 对比 | 日均差 | 5%下界 | 正/负日期 |", "|---|---:|---:|---|"]
    for name, s in comparisons.items():
        lines.append(f"| {name} | {s.get('mean')} | {s.get('lb05')} | {s.get('pos')}/{s.get('neg')} |")
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(OUT_JSON), "md": str(OUT_MD)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
