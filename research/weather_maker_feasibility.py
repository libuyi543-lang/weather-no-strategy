#!/usr/bin/env python3
"""Maker feasibility backtest for China-7 temperature bucket markets.

Research-only. Reads the monitor database read-only plus the trade cache built
by weather_maker_trades_fetch.py, and writes reports under research/output.

Quote model: at every 5-minute book snapshot, rest a YES bid and a YES ask
(= NO bid at 1-ask) of QUOTE_SIZE shares on each bucket, held for up to
HOLD_SECONDS or until the next snapshot. Public taker prints in that window
decide fills. All prints are mapped into YES-price space: a taker buying NO at
q is a taker selling YES at 1-q (and vice versa), because both books are one
matching engine.

Fill models (queue position is unknowable from public data):
  OPT   - front of queue: any taker print at or through our price fills us.
  QUEUE - back of queue: at-price prints first consume the displayed size at
          our level at snapshot time; trade-through prints fill us.
  CONS  - only prints strictly through our price fill us.
Quote variants:
  JOIN  - join best bid / best ask.
  PENNY - improve by one tick when spread >= 2 ticks (we are alone at the top,
          so at-or-through prints at the old best fill us; OPT==QUEUE there).
Makers pay no fee. Every fill is held to settlement; markouts versus later
mids measure adverse selection.
"""

from __future__ import annotations

import argparse
import bisect
import json
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MONITOR_DB = ROOT / "data" / "weather_market_monitor.sqlite3"
CACHE_DB = ROOT / "research" / "output" / "maker_trades_cache.sqlite3"
OUT_DIR = ROOT / "research" / "output"
CITIES = ("Shanghai", "Beijing", "Guangzhou", "Qingdao", "Wuhan", "Chongqing", "Chengdu")
TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
QUOTE_SIZE = 5.0
HOLD_SECONDS = 300
MAKER_REBATE_RATE = 0.25 * 0.05  # weather: 25% of taker fee 0.05*p*(1-p), paid pro rata to makers
EPS = 1e-9
MARKOUT_HORIZONS = (300, 1800, 7200)
BANDS = ((0, 9, "00-09"), (9, 13, "09-13"), (13, 18, "13-18"), (18, 24, "18-24"))
PRICE_BANDS = ((0.0, 0.05), (0.05, 0.20), (0.20, 0.50), (0.50, 0.80), (0.80, 0.95), (0.95, 1.0))


def ts_of(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def tick_for(price: float) -> float:
    return 0.001 if price < 0.04 or price > 0.96 else 0.01


def band_of(ts: float) -> str:
    hour = datetime.fromtimestamp(ts, TZ).hour
    for lo, hi, name in BANDS:
        if lo <= hour < hi:
            return name
    return "other"


def price_band(price: float) -> str:
    for lo, hi in PRICE_BANDS:
        if lo <= price < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return "other"


def load(mon: sqlite3.Connection, cache: sqlite3.Connection):
    events = mon.execute(
        f"""SELECT e.event_id, e.city, e.target_date, e.station_id, e.winning_market_id
            FROM events e WHERE e.winning_market_id IS NOT NULL
            AND e.city IN ({','.join('?' * len(CITIES))})""", CITIES,
    ).fetchall()
    metar = defaultdict(list)
    for station, obs, receipt, raw in mon.execute(
        "SELECT station_id, observation_time_utc, COALESCE(receipt_time_utc, first_fetched_at_utc), raw_metar "
        "FROM fast_metar_reports ORDER BY station_id, receipt_time_utc"
    ):
        metar[station].append(ts_of(receipt))
    for series in metar.values():
        series.sort()
    return events, metar


def load_trades(cache: sqlite3.Connection, market_id: str):
    """Return time-sorted taker prints as (ts, yes_price, taker_dir, size)."""
    out = []
    for outcome, side, price, size, ts in cache.execute(
        "SELECT outcome, side, price, size, ts FROM trades WHERE market_id=?", (market_id,)
    ):
        if not size or not price:
            continue
        buy = side == "BUY"
        if outcome == "Yes":
            out.append((ts, price, 1 if buy else -1, size))
        else:
            out.append((ts, 1.0 - price, -1 if buy else 1, size))
    out.sort()
    return out


def fill_quote(prints, start_idx, t0, t1, side, price, queue, model):
    """side=+1 is our bid (filled by taker sells), -1 our ask. Returns fills [(ts, shares)]."""
    remaining, queue_left, fills = QUOTE_SIZE, queue, []
    i = start_idx
    while i < len(prints) and prints[i][0] < t1 and remaining > EPS:
        ts, p, taker_dir, size = prints[i]
        i += 1
        if ts < t0 or taker_dir != -side:
            continue
        through = p < price - EPS if side == 1 else p > price + EPS
        at_level = abs(p - price) <= EPS
        if not (through or at_level):
            continue
        if model == "CONS" and not through:
            continue
        take = size
        if model == "QUEUE" and at_level:
            used = min(queue_left, take)
            queue_left -= used
            take -= used
        take = min(take, remaining)
        if take > EPS:
            fills.append((ts, take))
            remaining -= take
    return fills


def mid_at(snap_ts, snap_mid, t):
    j = bisect.bisect_left(snap_ts, t)
    while j < len(snap_ts) and snap_mid[j] is None:
        j += 1
    return snap_mid[j] if j < len(snap_ts) else None


def simulate(mon, cache, events, metar):
    fills = []
    for event_id, city, target_date, station, winner in events:
        day_end = datetime.fromisoformat(target_date).replace(tzinfo=TZ) + timedelta(days=1)
        day_end_ts = day_end.timestamp()
        receipts = metar.get(station, [])
        # market_snapshots is only indexed by (event_id, slot); never query it by market_id.
        by_market = defaultdict(list)
        for row in mon.execute(
            """SELECT market_id, fetched_at_utc, yes_best_bid, yes_best_ask, yes_bid_size, yes_ask_size
               FROM market_snapshots WHERE event_id=? ORDER BY fetched_at_utc""", (event_id,),
        ):
            by_market[row[0]].append(row[1:])
        for market_id, snaps in by_market.items():
            prints = load_trades(cache, market_id)
            print_ts = [p[0] for p in prints]
            snap_ts = [ts_of(s[0]) for s in snaps]
            snap_mid = [((s[1] + s[2]) / 2 if s[1] is not None and s[2] is not None else None) for s in snaps]
            payoff = 1.0 if market_id == winner else 0.0
            for k, (fetched, bid, ask, bid_sz, ask_sz) in enumerate(snaps):
                if bid is None or ask is None or ask <= bid + EPS:
                    continue
                t0 = snap_ts[k]
                if t0 >= day_end_ts:
                    break
                t1 = min(t0 + HOLD_SECONDS, snap_ts[k + 1] if k + 1 < len(snaps) else t0 + HOLD_SECONDS, day_end_ts)
                start = bisect.bisect_left(print_ts, t0)
                if start >= len(prints) or prints[start][0] >= t1:
                    continue
                mid0 = (bid + ask) / 2
                tick = tick_for(mid0)
                spread_ticks = round((ask - bid) / tick)
                quotes = [("JOIN", 1, bid, bid_sz or 0.0), ("JOIN", -1, ask, ask_sz or 0.0)]
                if spread_ticks >= 2:
                    quotes += [("PENNY", 1, bid + tick, 0.0), ("PENNY", -1, ask - tick, 0.0)]
                for variant, side, price, queue in quotes:
                    for model in ("OPT", "QUEUE", "CONS"):
                        if variant == "PENNY" and model == "QUEUE":
                            continue
                        for fts, shares in fill_quote(prints, start, t0, t1, side, price, queue, model):
                            r = bisect.bisect_right(receipts, fts) - 1
                            since_metar = (fts - receipts[r]) / 60 if r >= 0 else None
                            marks = {}
                            for h in MARKOUT_HORIZONS:
                                m = mid_at(snap_ts, snap_mid, fts + h)
                                marks[h] = None if m is None else side * (m - price)
                            fills.append({
                                "event_id": event_id, "city": city, "date": target_date,
                                "market_id": market_id, "variant": variant, "model": model,
                                "side": side, "price": price, "shares": shares, "ts": fts,
                                "band": band_of(fts), "pband": price_band(mid0), "mid0": mid0,
                                "spread_ticks": spread_ticks, "since_metar_min": since_metar,
                                "edge0": side * (mid0 - price),
                                "mk5": marks[300], "mk30": marks[1800], "mk120": marks[7200],
                                "pnl_per_share": side * (payoff - price),
                                "rebate_per_share": MAKER_REBATE_RATE * price * (1 - price),
                            })
    return fills


def date_bootstrap(per_date: dict[str, float], n_boot: int = 4000, seed: int = 7):
    dates = sorted(per_date)
    if not dates:
        return None, None, None
    vals = [per_date[d] for d in dates]
    rng = random.Random(seed)
    sims = sorted(sum(rng.choice(vals) for _ in vals) for _ in range(n_boot))
    return sum(vals), sims[int(0.05 * n_boot)], sims[int(0.95 * n_boot)]


def summarize(rows, all_dates):
    if not rows:
        return None
    shares = sum(r["shares"] for r in rows)
    pnl = sum(r["shares"] * r["pnl_per_share"] for r in rows)
    per_date = {d: 0.0 for d in all_dates}
    for r in rows:
        per_date[r["date"]] += r["shares"] * r["pnl_per_share"]
    total, lo, hi = date_bootstrap(per_date)
    rebate = sum(r["shares"] * r["rebate_per_share"] for r in rows)
    per_date_rb = {d: 0.0 for d in all_dates}
    for r in rows:
        per_date_rb[r["date"]] += r["shares"] * (r["pnl_per_share"] + r["rebate_per_share"])
    _, lo_rb, hi_rb = date_bootstrap(per_date_rb)

    def wavg(key):
        pairs = [(r["shares"], r[key]) for r in rows if r[key] is not None]
        w = sum(p[0] for p in pairs)
        return sum(a * b for a, b in pairs) / w if w else None

    return {
        "fills": len(rows), "shares": round(shares, 1), "notional": round(sum(
            r["shares"] * (r["price"] if r["side"] == 1 else 1 - r["price"]) for r in rows), 1),
        "pnl": round(pnl, 2), "pnl_per_share": round(pnl / shares, 4),
        "rebate": round(rebate, 2), "pnl_with_rebate": round(pnl + rebate, 2),
        "rebate_p05": round(lo_rb, 2), "rebate_p95": round(hi_rb, 2),
        "edge_at_fill": round(wavg("edge0"), 4), "markout_5m": _r(wavg("mk5")),
        "markout_30m": _r(wavg("mk30")), "markout_2h": _r(wavg("mk120")),
        "date_total_p05": round(lo, 2), "date_total_p95": round(hi, 2),
        "positive_dates": sum(1 for v in per_date.values() if v > 0),
        "negative_dates": sum(1 for v in per_date.values() if v < 0),
    }


def _r(x):
    return None if x is None else round(x, 4)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-json", default=str(OUT_DIR / "weather_maker_feasibility.json"))
    parser.add_argument("--output-md", default=str(OUT_DIR / "weather_maker_feasibility.md"))
    parser.add_argument("--fills-csv", default=str(OUT_DIR / "weather_maker_feasibility_fills.csv"))
    parser.add_argument("--hold-seconds", type=int, default=HOLD_SECONDS)
    args = parser.parse_args()
    globals()["HOLD_SECONDS"] = args.hold_seconds

    mon = sqlite3.connect(f"file:{MONITOR_DB}?mode=ro", uri=True)
    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    events, metar = load(mon, cache)
    fills = simulate(mon, cache, events, metar)
    all_dates = sorted({e[2] for e in events})

    report = {"events": len(events), "dates": len(all_dates), "groups": {}}
    groups = defaultdict(list)
    for f in fills:
        base = f"{f['variant']}/{f['model']}"
        groups[(base, "ALL")].append(f)
        groups[(base, "band=" + f["band"])].append(f)
        groups[(base, "price=" + f["pband"])].append(f)
        groups[(base, "side=" + ("bid" if f["side"] == 1 else "ask"))].append(f)
        sm = f["since_metar_min"]
        tag = "none" if sm is None else "0-10" if sm < 10 else "10-30" if sm < 30 else "30+"
        groups[(base, "since_metar=" + tag)].append(f)
        groups[(base, "city=" + f["city"])].append(f)
        groups[(base, "half=" + ("H1" if f["date"] <= "2026-08-23" else "H2"))].append(f)
    for (base, cut), rows in sorted(groups.items()):
        report["groups"].setdefault(base, {})[cut] = summarize(rows, all_dates)

    Path(args.output_json).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    keys = ["fills", "shares", "pnl", "pnl_per_share", "rebate", "pnl_with_rebate", "rebate_p05", "rebate_p95", "edge_at_fill", "markout_5m", "markout_30m",
            "markout_2h", "date_total_p05", "date_total_p95", "positive_dates", "negative_dates"]
    lines = [f"# Maker feasibility ({len(events)} events, {len(all_dates)} dates)", ""]
    for base, cuts in report["groups"].items():
        lines += [f"## {base}", "", "| cut | " + " | ".join(keys) + " |", "|" + "---|" * (len(keys) + 1)]
        for cut, s in cuts.items():
            lines.append(f"| {cut} | " + " | ".join(str(s[k]) for k in keys) + " |")
        lines.append("")
    Path(args.output_md).write_text("\n".join(lines))
    with open(args.fills_csv, "w") as fh:
        cols = list(fills[0].keys()) if fills else []
        fh.write(",".join(cols) + "\n")
        for f in fills:
            fh.write(",".join("" if f[c] is None else str(f[c]) for c in cols) + "\n")
    print(json.dumps({b: c["ALL"] for b, c in report["groups"].items()}, indent=1))


if __name__ == "__main__":
    main()
