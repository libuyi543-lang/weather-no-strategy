#!/usr/bin/env python3
"""Event study: do single weather signals (radar echo arrival, sea-breeze onset)
predict a capped daily max before the market prices it?

Research-only. Reads the monitor database read-only plus nothing else; writes
reports under research/output.

Point-in-time state at signal availability time t (radar: snapshot fetch time;
sea breeze: our first fetch of the METAR):
  M      = running max of METARs received by t, T_now = latest METAR temp
  holder = bucket containing M; "UP" = settlement bucket above holder
  P_up   = market probability of UP from the last snapshot <= t
           (sum of YES mids above holder / sum of all YES mids)
Only 11:00-16:00 local and T_now >= M - 1 (still near the max).
If the signal is faster than the market: realized UP rate after triggers is
well below P_up(t), and P_up keeps falling after t. Controls are the same
state on a 30-minute grid with no echo within 50 km in the last 30 minutes.
Trade check: at t buy NO (ask, 5 shares, taker fee) on every bucket above the
holder whose YES mid >= 0.02; hold to settlement.
"""

from __future__ import annotations

import argparse
import bisect
import json
import random
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MONITOR_DB = ROOT / "data" / "weather_market_monitor.sqlite3"
OUT_DIR = ROOT / "research" / "output"
CITIES = ("Shanghai", "Beijing", "Guangzhou", "Qingdao", "Wuhan", "Chongqing", "Chengdu")
TZ = ZoneInfo("Asia/Shanghai")
FEE_RATE = 0.05
SHARES = 5.0
# Onshore wind sectors (direction wind blows FROM), approximate coastline geometry.
ONSHORE = {"ZSPD": (30.0, 170.0), "ZSQD": (90.0, 210.0)}
RADAR_TRIGGERS = {
    "R25_new": lambda cur, hist: cur["c25"] >= 0.05 and max(h["c25"] for h in hist) < 0.01,
    "R25_strong": lambda cur, hist: cur["c25"] >= 0.20 and max(h["c25"] for h in hist) < 0.05,
    "R10_near": lambda cur, hist: cur["near"] is not None and cur["near"] <= 10
    and all(h["near"] is None or h["near"] > 25 for h in hist),
    "R50_approach": lambda cur, hist: cur["c50"] >= 0.10 and max(h["c50"] for h in hist) < 0.02,
}


def ts_of(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def local_hour(ts: float) -> float:
    d = datetime.fromtimestamp(ts, TZ)
    return d.hour + d.minute / 60


def load_metars(mon):
    rows = defaultdict(dict)
    for station, obs, fetched, mtype, payload in mon.execute(
        "SELECT station_id, observation_time_utc, first_fetched_at_utc, metar_type, payload_json FROM fast_metar_reports"
    ):
        try:
            p = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if p.get("temp") is None:
            continue
        wdir = p.get("wdir")
        rec = (ts_of(fetched), ts_of(obs), float(p["temp"]),
               float(wdir) if isinstance(wdir, (int, float)) else None, float(p.get("wspd") or 0))
        if obs not in rows[station] or rec[0] < rows[station][obs][0]:
            rows[station][obs] = rec
    return {s: sorted(v.values()) for s, v in rows.items()}  # sorted by availability time


def load_radar(mon):
    out = defaultdict(list)
    for station, fetched, feats in mon.execute(
        "SELECT station_id, fetched_at_utc, features_json FROM remote_sensing_snapshots "
        "WHERE source='rainviewer' AND status='ok'"
    ):
        try:
            f = json.loads(feats)
        except (TypeError, ValueError):
            continue
        if f.get("echoCoverage25Km") is None:
            continue
        out[station].append((ts_of(fetched), {
            "c25": f["echoCoverage25Km"], "c50": f.get("echoCoverage50Km") or 0.0,
            "near": f.get("nearestEchoKm")}))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


class EventMarket:
    def __init__(self, mon, event_id):
        self.buckets = sorted(mon.execute(
            "SELECT market_id, bucket_low, bucket_high FROM markets WHERE event_id=?", (event_id,)
        ).fetchall(), key=lambda b: (b[1] if b[1] is not None else -999))
        self.index = {b[0]: i for i, b in enumerate(self.buckets)}
        self.slots = defaultdict(dict)
        for mid, fetched, bid, ask, no_ask in mon.execute(
            "SELECT market_id, fetched_at_utc, yes_best_bid, yes_best_ask, no_best_ask FROM market_snapshots "
            "WHERE event_id=?", (event_id,),
        ):
            self.slots[fetched[:16]][mid] = (ts_of(fetched), bid, ask, no_ask)
        self.slot_keys = sorted(self.slots, key=lambda k: min(v[0] for v in self.slots[k].values()))
        self.slot_ts = [max(v[0] for v in self.slots[k].values()) for k in self.slot_keys]

    def holder(self, m):
        for i, (_, lo, hi) in enumerate(self.buckets):
            if (lo is None or lo <= m) and (hi is None or m <= hi):
                return i
        return None

    def state(self, t):
        j = bisect.bisect_right(self.slot_ts, t) - 1
        if j < 0 or t - self.slot_ts[j] > 600:
            return None
        return self.slots[self.slot_keys[j]]

    def p_up(self, t, hold):
        snap = self.state(t)
        if not snap:
            return None
        mids = {}
        for mid, (_, bid, ask, _) in snap.items():
            if bid is None and ask is None:
                continue
            mids[mid] = ((bid or 0.0) + (ask if ask is not None else 1.0)) / 2
        total = sum(mids.values())
        if total <= 0 or len(mids) < len(self.buckets) - 1:
            return None
        return sum(v for mid, v in mids.items() if self.index[mid] > hold) / total

    def trade(self, t, hold, winner_idx):
        snap = self.state(t)
        if not snap:
            return None
        pnl, cost = 0.0, 0.0
        for mid, (_, bid, ask, no_ask) in snap.items():
            if self.index[mid] <= hold or bid is None or ask is None or no_ask is None:
                continue
            if (bid + ask) / 2 < 0.02:
                continue
            fee = SHARES * FEE_RATE * no_ask * (1 - no_ask)
            payoff = 0.0 if self.index[mid] == winner_idx else 1.0
            pnl += SHARES * (payoff - no_ask) - fee
            cost += SHARES * no_ask + fee
        return (pnl, cost) if cost else None


def metar_state(series, t):
    """Latest METARs available at t for the local day of t: (M, T_now, last_record)."""
    day = datetime.fromtimestamp(t, TZ).date()
    avail = [r for r in series[: bisect.bisect_right([r[0] for r in series], t)]
             if datetime.fromtimestamp(r[1], TZ).date() == day]
    if len(avail) < 3:
        return None
    return max(r[2] for r in avail), avail[-1][2], avail


def collect(mon):
    metars, radar = load_metars(mon), load_radar(mon)
    events = mon.execute(
        f"""SELECT event_id, city, target_date, station_id, winning_market_id FROM events
            WHERE winning_market_id IS NOT NULL AND target_date >= '2026-07-28'
            AND city IN ({','.join('?' * len(CITIES))})""", CITIES,
    ).fetchall()
    rows = []
    for event_id, city, target_date, station, winner in events:
        series = metars.get(station, [])
        if not series:
            continue
        market = EventMarket(mon, event_id)
        if not market.slot_ts or winner not in market.index:
            continue
        winner_idx = market.index[winner]
        day0 = datetime.fromisoformat(target_date).replace(tzinfo=TZ)
        lo_ts, hi_ts = (day0 + timedelta(hours=11)).timestamp(), (day0 + timedelta(hours=16)).timestamp()

        def record(kind, t):
            st = metar_state(series, t)
            if not st:
                return
            m, t_now, _ = st
            if t_now < m - 1:
                return
            hold = market.holder(m)
            if hold is None or market.buckets[hold][2] is None:
                return
            p = {h: market.p_up(t + h * 60, hold) for h in (-30, 0, 30, 60, 120)}
            if p[0] is None:
                return
            trade = market.trade(t, hold, winner_idx)
            rows.append({
                "kind": kind, "city": city, "date": target_date, "t": t, "hour": round(local_hour(t), 2),
                "M": m, "T_now": t_now, "up": int(winner_idx > hold), "p_up": p,
                "trade_pnl": trade[0] if trade else None, "trade_cost": trade[1] if trade else None,
            })

        frames = [f for f in radar.get(station, []) if lo_ts - 3600 <= f[0] <= hi_ts]
        fired = set()
        for i, (t, cur) in enumerate(frames):
            if not lo_ts <= t <= hi_ts:
                continue
            hist = [f[1] for f in frames if t - 1860 <= f[0] < t - 60]
            if len(hist) < 3:
                continue
            for name, rule in RADAR_TRIGGERS.items():
                if name not in fired and rule(cur, hist):
                    fired.add(name)
                    record(name, t)
        # Controls: 30-minute grid with no echo within 50 km during the preceding 30 minutes.
        for k in range(11):
            t = lo_ts + k * 1800
            recent = [f[1] for f in frames if t - 1800 <= f[0] <= t]
            if len(recent) >= 3 and max(h["c50"] for h in recent) < 0.005:
                record("CONTROL_clear", t)
            if len(recent) >= 3:
                record("CONTROL_all", t)
        if station in ONSHORE:
            lo_deg, hi_deg = ONSHORE[station]
            onshore = lambda r: r[3] is not None and lo_deg <= r[3] <= hi_deg and r[4] >= 6
            day_series = [r for r in series if datetime.fromtimestamp(r[1], TZ).date() == day0.date()]
            for a, b in zip(day_series, day_series[1:]):
                if lo_ts <= b[0] <= hi_ts and onshore(b) and not onshore(a):
                    record("SEA_BREEZE_onset", b[0])
                    break
    return rows


def boot(rows, fn, n=3000, seed=5):
    by_date = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r)
    dates = sorted(by_date)
    rng = random.Random(seed)

    def stat(ds):
        xs = [x for d in ds for x in by_date[d]]
        return fn(xs) if xs else None

    sims = sorted(s for s in (stat([rng.choice(dates) for _ in dates]) for _ in range(n)) if s is not None)
    return stat(dates), sims[int(0.025 * len(sims))], sims[int(0.975 * len(sims))]


def summarize(rows):
    out = {}
    kinds = sorted({r["kind"] for r in rows})
    for kind in kinds:
        sub = [r for r in rows if r["kind"] == kind]
        mean = lambda xs, f: sum(f(x) for x in xs) / len(xs)
        gap = boot(sub, lambda xs: mean(xs, lambda r: r["up"] - r["p_up"][0]))
        drift = {}
        for h in (-30, 30, 60, 120):
            ok = [r for r in sub if r["p_up"][h] is not None]
            if ok:
                drift[f"dp_{h:+d}m"] = round(mean(ok, lambda r: (r["p_up"][h] - r["p_up"][0]) * (1 if h > 0 else -1)), 4)
        trades = [r for r in sub if r["trade_pnl"] is not None]
        tr = boot(trades, lambda xs: sum(r["trade_pnl"] for r in xs)) if len(trades) >= 5 else (None, None, None)
        out[kind] = {
            "n": len(sub), "city_days": len({(r["city"], r["date"]) for r in sub}),
            "dates": len({r["date"] for r in sub}),
            "cities": dict(sorted(defaultdict(int, {c: sum(1 for r in sub if r["city"] == c) for c in CITIES}).items())),
            "mean_hour": round(mean(sub, lambda r: r["hour"]), 2),
            "p_up_at_t": round(mean(sub, lambda r: r["p_up"][0]), 4),
            "realized_up": round(mean(sub, lambda r: r["up"]), 4),
            "up_minus_p": [round(x, 4) for x in gap],
            "p_up_change": drift,
            "trades": len(trades),
            "trade_pnl_total": [None if x is None else round(x, 2) for x in tr],
            "trade_cost_total": round(sum(r["trade_cost"] for r in trades), 2) if trades else None,
        }
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-json", default=str(OUT_DIR / "weather_single_signal_event_study.json"))
    args = parser.parse_args()
    mon = sqlite3.connect(f"file:{MONITOR_DB}?mode=ro", uri=True)
    rows = collect(mon)
    result = summarize(rows)
    Path(args.output_json).write_text(json.dumps(result, indent=2, ensure_ascii=False))
    with open(OUT_DIR / "weather_single_signal_event_rows.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(json.dumps(result, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
