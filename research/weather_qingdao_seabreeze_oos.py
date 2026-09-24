#!/usr/bin/env python3
"""Out-of-sample check of the frozen Qingdao sea-breeze rule.

Research-only. Rebuilds the rule from public history instead of the monitor
database: gamma (market structure + winner), CLOB prices-history (1-minute YES
prices), IEM ASOS archive (ZSQD METAR). Cache goes to research/output.

Frozen rule (from weather_single_signal_event_study.py, SEA_BREEZE_onset):
  METAR wind from 90-210 deg at >= 6 kt, previous METAR not onshore,
  available 11:00-16:00 local, latest temp >= running max - 1,
  first trigger per day. METAR availability = obs + 13 min (median ZSQD lag).
Periods: IS = 2026-07-28..09-18 (method replication), OOS = everything else.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "research" / "output"
CACHE = OUT_DIR / "qingdao_seabreeze_oos_cache.json"
TZ = ZoneInfo("Asia/Shanghai")
ONSHORE = (90.0, 210.0)
MIN_KT = 6.0
AVAIL_LAG = 13 * 60
FEE_RATE = 0.05
SHARES = 5.0
IS_RANGE = ("2026-07-28", "2026-09-18")


def get(url, as_json=True, retries=5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "weather-research/1.0"})
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = resp.read().decode()
            return json.loads(body) if as_json else body
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def parse_bucket(title):
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", title)]
    if not nums:
        return None
    if "below" in title or "lower" in title:
        return (None, nums[0])
    if "higher" in title or "above" in title:
        return (nums[0], None)
    return (nums[0], nums[-1])


def fetch_event(day: date):
    slug = f"highest-temperature-in-qingdao-on-{day.strftime('%B').lower()}-{day.day}-{day.year}"
    events = get(f"https://gamma-api.polymarket.com/events?slug={slug}")
    if not events:
        return None
    buckets = []
    for m in events[0]["markets"]:
        rng = parse_bucket(m.get("groupItemTitle") or "")
        prices = json.loads(m.get("outcomePrices") or "[]")
        if rng is None or len(prices) != 2:
            return None
        buckets.append({"low": rng[0], "high": rng[1], "token": json.loads(m["clobTokenIds"])[0],
                        "won": prices[0] == "1"})
    buckets.sort(key=lambda b: b["low"] if b["low"] is not None else -999)
    if sum(b["won"] for b in buckets) != 1:
        return None
    start = int(datetime.combine(day, datetime.min.time(), TZ).timestamp()) + 9 * 3600
    end = start + 10 * 3600
    for b in buckets:
        hist = get(f"https://clob.polymarket.com/prices-history?market={b['token']}"
                   f"&startTs={start}&endTs={end}&fidelity=1")
        b["hist"] = [(h["t"], h["p"]) for h in hist.get("history", [])]
    return {"date": day.isoformat(), "buckets": buckets}


def fetch_metars(first: date, last: date):
    url = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=ZSQD&data=tmpc&data=drct&data=sknt"
           f"&year1={first.year}&month1={first.month}&day1={first.day}"
           f"&year2={last.year}&month2={last.month}&day2={last.day}"
           "&tz=Etc/UTC&format=onlycomma&latlon=no&missing=M&direct=no&report_type=3&report_type=4")
    rows = []
    for r in csv.DictReader(io.StringIO(get(url, as_json=False))):
        if r["tmpc"] in ("M", ""):
            continue
        obs = datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
        rows.append((obs, float(r["tmpc"]),
                     None if r["drct"] in ("M", "") else float(r["drct"]),
                     0.0 if r["sknt"] in ("M", "") else float(r["sknt"])))
    rows.sort()
    return rows


def price_at(hist, t, tol=600):
    best = None
    for ts, p in hist:
        if ts > t:
            break
        best = (ts, p)
    return best[1] if best and t - best[0] <= tol else None


def evaluate(event, metars, half_spreads=(0.01, 0.02)):
    day = date.fromisoformat(event["date"])
    day_rows = [r for r in metars if datetime.fromtimestamp(r[0], TZ).date() == day]
    onshore = lambda r: r[2] is not None and ONSHORE[0] <= r[2] <= ONSHORE[1] and r[3] >= MIN_KT
    lo = datetime.combine(day, datetime.min.time(), TZ).timestamp() + 11 * 3600
    hi = lo + 5 * 3600
    for a, b in zip(day_rows, day_rows[1:]):
        t = b[0] + AVAIL_LAG
        if not (lo <= t <= hi and onshore(b) and not onshore(a)):
            continue
        avail = [r for r in day_rows if r[0] + AVAIL_LAG <= t]
        if len(avail) < 3:
            return None
        m, t_now = max(r[1] for r in avail), avail[-1][1]
        if t_now < m - 1:
            return None
        buckets = event["buckets"]
        hold = next((i for i, x in enumerate(buckets)
                     if (x["low"] is None or x["low"] <= m) and (x["high"] is None or m <= x["high"])), None)
        if hold is None or buckets[hold]["high"] is None:
            return None
        win = next(i for i, x in enumerate(buckets) if x["won"])

        def p_up(tt):
            ps = [price_at(x["hist"], tt) for x in buckets]
            if sum(p is None for p in ps) > 1:
                return None
            ps = [p or 0.0 for p in ps]
            total = sum(ps)
            return sum(ps[hold + 1:]) / total if total > 0 else None

        p0 = p_up(t)
        if p0 is None:
            return None
        trades = {}
        for hs in half_spreads:
            pnl = cost = 0.0
            for i, x in enumerate(buckets):
                p = price_at(x["hist"], t)
                if i <= hold or p is None or p < 0.02:
                    continue
                q = min(0.999, 1 - p + hs)
                fee = SHARES * FEE_RATE * q * (1 - q)
                pnl += SHARES * ((0.0 if i == win else 1.0) - q) - fee
                cost += SHARES * q + fee
            trades[f"hs{hs}"] = (round(pnl, 3), round(cost, 3))
        return {"date": event["date"], "t": t, "hour": round((t - lo) / 3600 + 11, 2), "M": m, "T_now": t_now,
                "wind": [b[2], b[3]], "p_up": p0, "p_up_120": p_up(t + 7200), "up": int(win > hold),
                "trades": trades}
    return None


def boot(rows, fn, n=4000, seed=9):
    if not rows:
        return None
    rng = random.Random(seed)
    sims = sorted(fn([rng.choice(rows) for _ in rows]) for _ in range(n))
    return [round(fn(rows), 4), round(sims[int(0.025 * n)], 4), round(sims[int(0.975 * n)], 4)]


def summarize(rows):
    if not rows:
        return {"n": 0}
    out = {
        "n": len(rows), "p_up": round(sum(r["p_up"] for r in rows) / len(rows), 4),
        "realized_up": round(sum(r["up"] for r in rows) / len(rows), 4),
        "up_minus_p": boot(rows, lambda xs: sum(r["up"] - r["p_up"] for r in xs) / len(xs)),
    }
    later = [r for r in rows if r["p_up_120"] is not None]
    if later:
        out["p_up_change_2h"] = round(sum(r["p_up_120"] - r["p_up"] for r in later) / len(later), 4)
    for key in rows[0]["trades"]:
        tr = [r for r in rows if r["trades"][key][1] > 0]
        if tr:
            out[f"pnl_{key}"] = boot(tr, lambda xs, k=key: sum(r["trades"][k][0] for r in xs))
            out[f"cost_{key}"] = round(sum(r["trades"][key][1] for r in tr), 2)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-04-27")
    parser.add_argument("--end", default="2026-09-23")
    args = parser.parse_args()
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {"events": {}, "metars": None}
    first, last = date.fromisoformat(args.start), date.fromisoformat(args.end)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    todo = [d for d in days if d.isoformat() not in cache["events"]]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for d, ev in zip(todo, pool.map(fetch_event, todo)):
            cache["events"][d.isoformat()] = ev
    if not cache["metars"]:
        cache["metars"] = fetch_metars(first - timedelta(days=1), last + timedelta(days=1))
    CACHE.write_text(json.dumps(cache))

    metars = [tuple(r) for r in cache["metars"]]
    rows = [r for r in (evaluate(ev, metars) for ev in cache["events"].values() if ev) if r]
    is_rows = [r for r in rows if IS_RANGE[0] <= r["date"] <= IS_RANGE[1]]
    oos_rows = [r for r in rows if not IS_RANGE[0] <= r["date"] <= IS_RANGE[1]]
    result = {
        "events_with_data": sum(1 for ev in cache["events"].values() if ev),
        "IS_replication": summarize(is_rows),
        "OOS": summarize(oos_rows),
        "OOS_pre_july28": summarize([r for r in oos_rows if r["date"] < IS_RANGE[0]]),
        "OOS_sept": summarize([r for r in oos_rows if r["date"] > IS_RANGE[1]]),
        "OOS_by_month": {m: summarize([r for r in oos_rows if r["date"][5:7] == m])
                         for m in sorted({r["date"][5:7] for r in oos_rows})},
        "IS_dates": sorted(r["date"] for r in is_rows),
    }
    (OUT_DIR / "weather_qingdao_seabreeze_oos.json").write_text(json.dumps(result, indent=2))
    with open(OUT_DIR / "weather_qingdao_seabreeze_oos_rows.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
