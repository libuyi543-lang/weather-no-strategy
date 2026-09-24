#!/usr/bin/env python3
"""Fetch public Polymarket trade prints for resolved China-7 temperature events.

Research-only. Reads the monitor database read-only and writes a separate cache
database (default research/output/maker_trades_cache.sqlite3). The data-api
returns taker-side prints by default, which is exactly the flow a resting maker
quote would have traded against.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONITOR_DB = ROOT / "data" / "weather_market_monitor.sqlite3"
CACHE_DB = ROOT / "research" / "output" / "maker_trades_cache.sqlite3"
CITIES = ("Shanghai", "Beijing", "Guangzhou", "Qingdao", "Wuhan", "Chongqing", "Chengdu")
PAGE = 500
MAX_OFFSET = 20000
lock = threading.Lock()


def get_json(url: str, retries: int = 6):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "weather-research/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise


def init_cache(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS condition_map (
            market_id TEXT PRIMARY KEY, event_id TEXT, condition_id TEXT
        );
        CREATE TABLE IF NOT EXISTS trades (
            market_id TEXT NOT NULL, condition_id TEXT NOT NULL, tx TEXT, asset TEXT,
            outcome TEXT, side TEXT, price REAL, size REAL, ts INTEGER, wallet TEXT,
            UNIQUE(tx, asset, wallet, side, price, size, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_trades_market_ts ON trades(market_id, ts);
        CREATE TABLE IF NOT EXISTS fetch_status (
            market_id TEXT PRIMARY KEY, n_rows INTEGER, capped INTEGER, fetched_at REAL
        );
        """
    )


def map_conditions(cache: sqlite3.Connection, events: list[tuple[str, str]]) -> None:
    done = {row[0] for row in cache.execute("SELECT DISTINCT event_id FROM condition_map")}
    todo = [(eid, slug) for eid, slug in events if eid not in done]

    def one(item):
        eid, slug = item
        payload = get_json(f"https://gamma-api.polymarket.com/events?slug={slug}")
        rows = []
        for event in payload or []:
            for market in event.get("markets", []):
                if market.get("conditionId"):
                    rows.append((str(market["id"]), eid, market["conditionId"]))
        return rows

    with ThreadPoolExecutor(max_workers=6) as pool:
        for fut in as_completed([pool.submit(one, item) for item in todo]):
            rows = fut.result()
            with lock:
                cache.executemany("INSERT OR REPLACE INTO condition_map VALUES (?,?,?)", rows)
                cache.commit()
    print(f"condition map: {len(todo)} events fetched")


def fetch_market(market_id: str, condition_id: str):
    rows, offset, capped = [], 0, 0
    while True:
        url = f"https://data-api.polymarket.com/trades?market={condition_id}&limit={PAGE}&offset={offset}"
        page = get_json(url)
        if not isinstance(page, list):
            capped = 1
            break
        for t in page:
            rows.append((
                market_id, condition_id, t.get("transactionHash"), t.get("asset"), t.get("outcome"),
                t.get("side"), float(t.get("price") or 0), float(t.get("size") or 0),
                int(t.get("timestamp") or 0), t.get("proxyWallet"),
            ))
        if len(page) < PAGE:
            break
        offset += PAGE
        if offset > MAX_OFFSET:
            capped = 1
            break
    return market_id, rows, capped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", default=str(CACHE_DB))
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    mon = sqlite3.connect(f"file:{MONITOR_DB}?mode=ro", uri=True)
    events = mon.execute(
        f"SELECT event_id, slug FROM events WHERE winning_market_id IS NOT NULL "
        f"AND city IN ({','.join('?' * len(CITIES))})", CITIES,
    ).fetchall()
    local_markets = {row[0] for row in mon.execute(
        f"SELECT m.market_id FROM markets m JOIN events e ON e.event_id=m.event_id "
        f"WHERE e.winning_market_id IS NOT NULL AND e.city IN ({','.join('?' * len(CITIES))})", CITIES,
    )}
    mon.close()

    Path(args.cache).parent.mkdir(parents=True, exist_ok=True)
    cache = sqlite3.connect(args.cache, check_same_thread=False)
    init_cache(cache)
    map_conditions(cache, events)

    done = {row[0] for row in cache.execute("SELECT market_id FROM fetch_status")}
    todo = [(mid, cid) for mid, cid in cache.execute("SELECT market_id, condition_id FROM condition_map")
            if mid in local_markets and mid not in done]
    missing = local_markets - {row[0] for row in cache.execute("SELECT market_id FROM condition_map")}
    print(f"markets to fetch: {len(todo)}; local markets without condition id: {len(missing)}")

    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch_market, mid, cid) for mid, cid in todo]
        for index, fut in enumerate(as_completed(futures), 1):
            mid, rows, capped = fut.result()
            with lock:
                cache.executemany("INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
                cache.execute("INSERT OR REPLACE INTO fetch_status VALUES (?,?,?,?)",
                              (mid, len(rows), capped, time.time()))
                cache.commit()
            if index % 200 == 0:
                print(f"{index}/{len(todo)} markets, {time.time() - started:.0f}s", flush=True)
    total = cache.execute("SELECT COUNT(*), SUM(capped) FROM trades, (SELECT SUM(capped) capped FROM fetch_status)").fetchone()
    print(f"done: trades={total[0]} capped_markets={total[1]}")


if __name__ == "__main__":
    main()
