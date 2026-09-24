#!/usr/bin/env python3
"""Does the market price METAR information before the METAR is public?

Research-only. Reads the monitor database read-only plus the trade cache from
weather_maker_trades_fetch.py; writes reports under research/output.

Design: for every METAR/SPECI release k (local 08-18h) take the bucket that
currently contains the running daily max M_prev (only buckets with a finite
upper edge). Release k either KILLS it (temp_k > bucket_high, YES -> 0) or it
SURVIVES. Anchors per release:
  S = our first fetch of the previous release + 2 min (all prior METAR public)
  O = observation time of release k (the measurement itself)
  R = AWC receipt time of release k
  F = our first fetch of release k (public API availability)
  P = F + 15 min
If nobody has information faster than METAR, the price change over S->O must
not differ between KILL and SURVIVE. Mid prices come from the nearest 5-minute
snapshot (measurement only, not a tradable signal); taker flow from prints.
"""

from __future__ import annotations

import argparse
import bisect
import json
import random
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MONITOR_DB = ROOT / "data" / "weather_market_monitor.sqlite3"
CACHE_DB = ROOT / "research" / "output" / "maker_trades_cache.sqlite3"
OUT_DIR = ROOT / "research" / "output"
CITIES = ("Shanghai", "Beijing", "Guangzhou", "Qingdao", "Wuhan", "Chongqing", "Chengdu")
HOURLY_STATIONS = {"ZHHH", "ZSQD", "ZUCK", "ZUUU"}
TZ = ZoneInfo("Asia/Shanghai")
NEAREST_TOL = 200  # seconds; snapshots are 5 minutes apart
WINDOWS = (("S_O", "S", "O"), ("O_R", "O", "R"), ("R_F", "R", "F"), ("F_P", "F", "P"))


def ts_of(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def load_metars(mon):
    """station -> list of (obs_ts, receipt_ts, fetch_ts, temp, type), one per obs time."""
    best = {}
    for station, obs, receipt, fetched, mtype, payload in mon.execute(
        "SELECT station_id, observation_time_utc, receipt_time_utc, first_fetched_at_utc, metar_type, payload_json "
        "FROM fast_metar_reports"
    ):
        try:
            temp = json.loads(payload).get("temp")
        except (TypeError, ValueError):
            temp = None
        if temp is None or receipt is None:
            continue
        key = (station, obs)
        row = (ts_of(obs), ts_of(receipt), ts_of(fetched), float(temp), mtype)
        if key not in best or row[2] < best[key][2]:
            best[key] = row
    out = defaultdict(list)
    for (station, _), row in best.items():
        out[station].append(row)
    for rows in out.values():
        rows.sort()
    return out


def nearest(ts_list, vals, t):
    j = bisect.bisect_left(ts_list, t)
    best, dist = None, NEAREST_TOL + 1
    for k in (j - 1, j):
        if 0 <= k < len(ts_list) and vals[k] is not None and abs(ts_list[k] - t) < dist:
            best, dist = vals[k], abs(ts_list[k] - t)
    return best


def signed_flow(cache, market_id):
    """(ts, signed size) with + = taker buys YES (incl. taker sells NO)."""
    out = []
    for outcome, side, size, ts in cache.execute(
        "SELECT outcome, side, size, ts FROM trades WHERE market_id=?", (market_id,)
    ):
        sign = (1 if side == "BUY" else -1) * (1 if outcome == "Yes" else -1)
        out.append((ts, sign * size))
    out.sort()
    return out


def collect(mon, cache):
    metars = load_metars(mon)
    events = mon.execute(
        f"""SELECT event_id, city, target_date, station_id FROM events
            WHERE winning_market_id IS NOT NULL AND target_date >= '2026-07-28'
            AND city IN ({','.join('?' * len(CITIES))})""", CITIES,
    ).fetchall()
    cases = []
    for event_id, city, target_date, station in events:
        releases = [r for r in metars.get(station, [])
                    if datetime.fromtimestamp(r[0], TZ).date().isoformat() == target_date]
        if len(releases) < 3:
            continue
        buckets = mon.execute(
            "SELECT market_id, bucket_low, bucket_high FROM markets WHERE event_id=?", (event_id,)
        ).fetchall()
        snaps = defaultdict(lambda: ([], []))
        for market_id, fetched, bid, ask in mon.execute(
            "SELECT market_id, fetched_at_utc, yes_best_bid, yes_best_ask FROM market_snapshots "
            "WHERE event_id=? ORDER BY fetched_at_utc", (event_id,),
        ):
            ts_list, mids = snaps[market_id]
            ts_list.append(ts_of(fetched))
            # A killed bucket loses all YES bids; an empty side means 0 (bid) or 1 (ask).
            if bid is None and ask is None:
                mids.append(None)
            else:
                mids.append(((bid if bid is not None else 0.0) + (ask if ask is not None else 1.0)) / 2)
        running = releases[0][3]
        for k in range(1, len(releases)):
            obs, receipt, fetch, temp, mtype = releases[k]
            prev = releases[k - 1]
            m_prev = running
            running = max(running, temp)
            hour = datetime.fromtimestamp(obs, TZ).hour
            if not 8 <= hour < 18:
                continue
            holder = [b for b in buckets if b[2] is not None
                      and (b[1] is None or b[1] <= m_prev) and m_prev <= b[2]]
            if len(holder) != 1:
                continue
            market_id, _, high = holder[0]
            ts_list, mids = snaps.get(market_id, ([], []))
            anchors = {"S": prev[2] + 120, "O": obs, "R": receipt, "F": fetch, "P": fetch + 900}
            if not anchors["S"] < anchors["O"] <= anchors["R"] <= anchors["F"]:
                continue
            prices = {a: nearest(ts_list, mids, t) for a, t in anchors.items()}
            if any(v is None for v in prices.values()) or not 0.05 <= prices["S"] <= 0.95:
                continue
            cases.append({
                "event_id": event_id, "city": city, "date": target_date, "station": station,
                "market_id": market_id, "hour": hour, "type": mtype,
                "hourly": station in HOURLY_STATIONS,
                "at_max": prev[3] >= m_prev, "killed": temp > high,
                "prices": prices, "anchors": anchors,
            })
    flows = {}
    for case in cases:
        mid = case["market_id"]
        if mid not in flows:
            flows[mid] = signed_flow(cache, mid)
        series = flows[mid]
        ts_only = [f[0] for f in series]
        case["flow"] = {}
        for name, a, b in WINDOWS:
            lo = bisect.bisect_left(ts_only, case["anchors"][a])
            hi = bisect.bisect_left(ts_only, case["anchors"][b])
            case["flow"][name] = sum(f[1] for f in series[lo:hi])
    return cases


def boot_diff(kill, surv, key, n=3000, seed=11):
    """Date-block bootstrap of mean(kill) - mean(surv)."""
    by_date = defaultdict(lambda: ([], []))
    for c in kill:
        by_date[c["date"]][0].append(key(c))
    for c in surv:
        by_date[c["date"]][1].append(key(c))
    dates = sorted(by_date)
    rng = random.Random(seed)

    def stat(sample):
        k = [v for d in sample for v in by_date[d][0]]
        s = [v for d in sample for v in by_date[d][1]]
        return (sum(k) / len(k) - sum(s) / len(s)) if k and s else None

    point = stat(dates)
    sims = sorted(x for x in (stat([rng.choice(dates) for _ in dates]) for _ in range(n)) if x is not None)
    return point, sims[int(0.025 * len(sims))], sims[int(0.975 * len(sims))]


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def analyze(cases):
    def dp(a, b):
        return lambda c: c["prices"][b] - c["prices"][a]

    result = {}
    strata = {
        "ALL": lambda c: True,
        "at_max": lambda c: c["at_max"],
        "half_hourly_stations": lambda c: not c["hourly"],
        "hourly_stations": lambda c: c["hourly"],
        "at_max_half_hourly": lambda c: c["at_max"] and not c["hourly"],
        "at_max_hourly": lambda c: c["at_max"] and c["hourly"],
    }
    for sname, pred in strata.items():
        sub = [c for c in cases if pred(c)]
        kill = [c for c in sub if c["killed"]]
        surv = [c for c in sub if not c["killed"]]
        if len(kill) < 5 or len(surv) < 5:
            continue
        row = {"n_kill": len(kill), "n_survive": len(surv),
               "p_start_kill": round(mean(c["prices"]["S"] for c in kill), 4),
               "p_start_survive": round(mean(c["prices"]["S"] for c in surv), 4)}
        for name, a, b in WINDOWS:
            point, lo, hi = boot_diff(kill, surv, dp(a, b))
            row[f"dp_{name}_kill"] = round(mean(dp(a, b)(c) for c in kill), 4)
            row[f"dp_{name}_survive"] = round(mean(dp(a, b)(c) for c in surv), 4)
            row[f"diff_{name}"] = [round(point, 4), round(lo, 4), round(hi, 4)]
            fpoint, flo, fhi = boot_diff(kill, surv, lambda c, n=name: c["flow"][n])
            row[f"flow_diff_{name}"] = [round(fpoint, 1), round(flo, 1), round(fhi, 1)]
        total = sum(c["prices"]["P"] - c["prices"]["S"] for c in kill) - \
            len(kill) * mean(c["prices"]["P"] - c["prices"]["S"] for c in surv)
        cum = 0.0
        shares = {}
        for name, a, b in WINDOWS:
            cum += sum(dp(a, b)(c) for c in kill) - len(kill) * mean(dp(a, b)(c) for c in surv)
            shares[f"share_by_{b}"] = round(cum / total, 3) if total else None
        row["kill_move_share_excess_over_survive"] = shares
        result[sname] = row
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-json", default=str(OUT_DIR / "weather_pre_metar_leakage.json"))
    args = parser.parse_args()
    mon = sqlite3.connect(f"file:{MONITOR_DB}?mode=ro", uri=True)
    cache = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True)
    cases = collect(mon, cache)
    result = {"cases": len(cases), "dates": len({c["date"] for c in cases}), "strata": analyze(cases)}
    Path(args.output_json).write_text(json.dumps(result, indent=2, ensure_ascii=False))
    with open(OUT_DIR / "weather_pre_metar_leakage_cases.jsonl", "w") as fh:
        for c in cases:
            fh.write(json.dumps(c) + "\n")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
