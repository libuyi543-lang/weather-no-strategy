#!/usr/bin/env python3
"""Multi-bucket ladder + dynamic rebalance backtest (point-in-time, no lookahead).

Strategy spec frozen before first run (2026-08-24):

Universe
    Tracked China cities (Shanghai, Beijing, Guangzhou, Qingdao, Wuhan,
    Chongqing, Chengdu); resolved high-temp events with winning_range;
    >=100 market snapshot slots and >=20 ok METAR rows on the city-day.
    Independent unit for statistics: target_date (city-days are correlated).

Entry (identical across all variants)
    - First snapshot slot >= 11:00 Beijing (03:00 UTC) on the target local
      date; skipped if no slot before 04:00 UTC.
    - Center bucket = exact-degree bucket with highest YES midpoint at that
      slot among two-sided quotes; must lead the runner-up by >= 0.03.
    - Legs = [center-1, center, center+1], all exact-degree buckets with
      spread <= 0.20, executable ask-side VWAP for the target shares,
      package cost including fee <= 15 USDC.
    - Shares 5 / 15 / 5 (lower / center / upper): the already frozen
      V4-style structure; weights are NOT re-optimized here.

Dynamic rules (evaluated at every later slot using only that slot's book
and METAR rows already fetched by then)
    R1 path-failure exit: a held exact bucket x is physically dead once the
        running observed daily max O(t) exceeds x (daily max never
        decreases, so the final max can never land on x again). Sell the
        full leg at bid-side executable VWAP when that price > 0.01 and
        depth fills the size; otherwise retry on later slots; unsold dead
        legs ride to settlement at zero. Proceeds pay the same taker fee.
    R2 winner add (at most once per city-day): while the center leg is
        alive and O(t) >= center - 1, buy 5 more center shares if ask VWAP
        <= 0.75 and open capital stays <= 20 USDC.
    R3 salvage reinvest (after each R1 sale, at most twice per city-day):
        buy 5 shares of the alive exact bucket with the highest YES
        midpoint (ask VWAP <= 0.80, spread <= 0.20) while open capital
        stays <= 20 USDC.
    New buys (R2/R3) disabled after 19:00 Beijing (11:00 UTC); exits run to
    settlement; winners otherwise held to settlement (no take-profit).

Variants (all reported, none selected post hoc)
    S0 static hold to settlement; D1 = S0+R1; D2 = D1+R3; D3 = D2+R2.
    Selection A = every eligible city-day; B = one city per day, the
    eligible candidate with the lowest max leg spread (their frozen
    selector family; known data-mining risk, kept for comparison).

Extension (second freeze, 2026-08-24, before any of these runs)
    H09..H15: identical S0 structure entered at 09:00..15:00 Beijing
        instead of 11:00 (deadline stays 60 minutes after entry; all
        gates evaluated on that slot's book).
    Skew tilt at the frozen 11:00 entry (the user's directional-rebalance
        idea, executable version): delta = point-in-time forecast daily
        max minus center bucket temperature,
          delta >= +0.5 -> shares 5/15/10 (extra 5 on the upper leg),
          delta <= -0.5 -> shares 10/15/5 (extra 5 on the lower leg),
          otherwise symmetric 5/15/5; missing signal falls back to
        symmetric (fallback counted). Two declared signals only:
        AMB = latest Windy/Meteoblue forecast_max_c with
              fetched_at_utc <= entry tick;
        ARDG = latest weather_ridge_v2_snapshots.primary_path_c with
              status='ok', primary_path_c not null,
              generated_at_utc <= entry tick.
    Both grids are reported whole; picking winners inside them is
    exploratory (multiple-testing discipline applies).

Extension (third freeze, 2026-08-24, before these runs)
    Center-vs-rivals mix (motivated by the neighbor-NO counterfactual,
    which showed both neighbors overpriced as YES on gated days):
      MIX15 : gate unchanged; buy 15 center YES at ask VWAP plus 5 NO on
              EACH exact neighbor at the REAL no-book ask VWAP
              (mirror identity verified: no_ask = 1 - yes_bid exactly);
              everything held to settlement, no dynamic rules.
      MIXE15: MIX15 plus the E0 two-sided exits applied ONLY to the
              center YES leg (hard O>center and reachability); NO legs
              always ride to settlement because a passed bucket makes
              its NO a sure winner, never a dead position.
    Package budget still capped by MAX_PACKAGE_COST; days where either
    NO book cannot fill 5 shares are skipped as insufficient_no_depth.
      MIX10 / MIXE10: same structures with a 10-share center leg instead
              of 15 — declared up front because the 15-share version
              breaches the 15-USDC cap on roughly half of gated days
              (execution feasibility variant, not a tuned alternative).

Accounting
    Fee = shares * 0.05 * price * (1 - price) on buys and sells alike
    (Polymarket weather taker formula used throughout this repo).
    PnL = settlement payout + net sale proceeds - gross cash spent.

Anti-lookahead measures
    Every decision reads only book rows with slot_utc <= decision time and
    observations with fetched_at_utc <= decision time; settlement labels
    come from events.winning_range only.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import statistics
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CITIES = ("Shanghai", "Beijing", "Guangzhou", "Qingdao", "Wuhan", "Chongqing", "Chengdu")
FEE_RATE = 0.05
WEIGHTS = (5.0, 15.0, 5.0)
CENTER_LEAD_MIN = 0.03
MAX_LEG_SPREAD = 0.20
MAX_PACKAGE_COST = 15.0
TOTAL_DEPLOY_CAP = 20.0
WINNER_ADD_ASK_MAX = 0.75
REINVEST_ASK_MAX = 0.80
SELL_FLOOR_PRICE = 0.01
ENTRY_HOUR_DEFAULT = 3.0
ENTRY_DEADLINE_GAP = timedelta(hours=1)
NEW_BUY_CUTOFF = timedelta(hours=11)
MIN_SLOTS = 100
MIN_METAR_ROWS = 20
BUCKET_RE = re.compile(r"^(\d+)°C( or below| or higher)?$")
BOOTSTRAP_ITERS = 10000
BOOTSTRAP_SEED = 42
VARIANTS = (
    "S0", "D1", "D2", "D3", "E0", "E3", "E3_r075", "E3_r150", "C10", "C15", "C20",
    "H09", "H10", "H11", "H12", "H13", "H14", "H15", "AMB", "ARDG",
    "MIX15", "MIXE15", "MIX10", "MIXE10",
)
SKEW_THRESHOLD_C = 0.5
WEIGHTS_TILT_UP = (5.0, 15.0, 10.0)
WEIGHTS_TILT_DOWN = (10.0, 15.0, 5.0)
# (variant, reach_rate, entry_hour_utc, skew_signal) — timing grid and the
# two declared skew signals live here instead of ad-hoc reruns.
RUN_SPECS = {
    "S0": ("S0", None, ENTRY_HOUR_DEFAULT, None),
    "D1": ("D1", None, ENTRY_HOUR_DEFAULT, None),
    "D2": ("D2", None, ENTRY_HOUR_DEFAULT, None),
    "D3": ("D3", None, ENTRY_HOUR_DEFAULT, None),
    "E0": ("E0", None, ENTRY_HOUR_DEFAULT, None),
    "E3": ("E3", None, ENTRY_HOUR_DEFAULT, None),
    "E3_r075": ("E3", 0.75, ENTRY_HOUR_DEFAULT, None),
    "E3_r150": ("E3", 1.5, ENTRY_HOUR_DEFAULT, None),
    "C10": ("C10", None, ENTRY_HOUR_DEFAULT, None),
    "C15": ("C15", None, ENTRY_HOUR_DEFAULT, None),
    "C20": ("C20", None, ENTRY_HOUR_DEFAULT, None),
    "H09": ("S0", None, 1.0, None), "H10": ("S0", None, 2.0, None),
    "H11": ("S0", None, 3.0, None), "H12": ("S0", None, 4.0, None),
    "H13": ("S0", None, 5.0, None), "H14": ("S0", None, 6.0, None),
    "H15": ("S0", None, 7.0, None),
    "AMB": ("S0", None, ENTRY_HOUR_DEFAULT, "mb"),
    "ARDG": ("S0", None, ENTRY_HOUR_DEFAULT, "ridge"),
    "MIX15": ("MIX15", None, ENTRY_HOUR_DEFAULT, None),
    "MIXE15": ("MIXE15", None, ENTRY_HOUR_DEFAULT, None),
    "MIX10": ("MIX10", None, ENTRY_HOUR_DEFAULT, None),
    "MIXE10": ("MIXE10", None, ENTRY_HOUR_DEFAULT, None),
}
MIX_CENTER_SHARES = {"MIX15": 15.0, "MIXE15": 15.0, "MIX10": 10.0, "MIXE10": 10.0}
B_SELECTION_VARIANTS = ("S0", "D3", "E3")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def parse_bucket(label: str) -> int | None:
    """Exact-degree buckets only ('34°C'); open ends return None so they
    never collide with an exact bucket of the same degree."""
    match = re.match(r"^(\d+)°C$", label or "")
    return int(match.group(1)) if match else None


def day_start(target_date: str) -> datetime:
    return parse_ts(f"{target_date}T00:00:00+00:00")


def before_new_buy_cutoff(tick: datetime) -> bool:
    return tick - day_start(tick.strftime("%Y-%m-%d")) < NEW_BUY_CUTOFF


def book_vwap(book_json: str | None, shares: float, side: str) -> tuple[float | None, float]:
    """Executable VWAP walking one side of the book ('ask' = buy taker,
    'bid' = sell taker). Returns (vwap_or_None, filled_shares); None when
    depth cannot fill the full size."""
    if not book_json or shares <= 0:
        return None, 0.0
    try:
        payload = json.loads(book_json)
    except (TypeError, ValueError):
        return None, 0.0
    levels = payload.get("asks" if side == "ask" else "bids") if isinstance(payload, dict) else payload
    cleaned: list[tuple[float, float]] = []
    for level in levels or []:
        if isinstance(level, dict):
            price, size = float(level.get("price", 0.0)), float(level.get("size", 0.0))
        elif isinstance(level, (list, tuple)):
            price, size = float(level[0]), float(level[1])
        else:
            continue
        if price > 0 and size > 0:
            cleaned.append((price, size))
    if not cleaned:
        return None, 0.0
    cleaned.sort(key=lambda item: item[0], reverse=(side == "bid"))
    remaining, cost = shares, 0.0
    for price, size in cleaned:
        take = min(remaining, size)
        cost += take * price
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None, 0.0
    return cost / shares, shares


def fee_for(shares: float, price: float) -> float:
    return shares * FEE_RATE * price * (1.0 - price)


def load_universe(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(
        """
        SELECT e.event_id, e.city, e.target_date, e.station_id, e.winning_range
        FROM events e
        WHERE e.resolved_at_utc IS NOT NULL AND e.winning_range IS NOT NULL
          AND e.city IN (?,?,?,?,?,?,?)
        ORDER BY e.target_date, e.city
        """,
        CITIES,
    ).fetchall()
    universe = []
    for row in rows:
        slots = db.execute(
            "SELECT COUNT(DISTINCT slot_utc) FROM market_snapshots WHERE event_id=?",
            (row["event_id"],),
        ).fetchone()[0]
        metar_rows = db.execute(
            """
            SELECT COUNT(*) FROM weather_observations
            WHERE station_id=? AND sample_local_date=? AND source='metar'
              AND status='ok' AND temperature_c IS NOT NULL
            """,
            (row["station_id"], row["target_date"]),
        ).fetchone()[0]
        if slots >= MIN_SLOTS and metar_rows >= MIN_METAR_ROWS:
            universe.append(dict(row))
    return universe


class EventData:
    """Point-in-time inputs for one city-day."""

    def __init__(self, db: sqlite3.Connection, event: dict[str, Any]):
        self.event = event
        self.winning_temp = parse_bucket(event["winning_range"] or "")
        self.slots: list[datetime] = []
        self.books: list[dict[str, dict[str, Any]]] = []
        slot_index: dict[str, int] = {}
        rows = db.execute(
            """
            SELECT slot_utc, outcome_range, yes_best_bid, yes_best_ask, yes_book_json,
                   no_best_bid, no_best_ask, no_book_json
            FROM market_snapshots WHERE event_id=? ORDER BY slot_utc
            """,
            (event["event_id"],),
        ).fetchall()
        for row in rows:
            stamp = parse_ts(row["slot_utc"])
            if row["slot_utc"] not in slot_index:
                slot_index[row["slot_utc"]] = len(self.slots)
                self.slots.append(stamp)
                self.books.append({})
            self.books[slot_index[row["slot_utc"]]][row["outcome_range"]] = dict(row)
        self.obs_times: list[datetime] = []
        self.obs_temps: list[float] = []
        obs_rows = db.execute(
            """
            SELECT fetched_at_utc, temperature_c FROM weather_observations
            WHERE station_id=? AND sample_local_date=? AND source='metar'
              AND status='ok' AND temperature_c IS NOT NULL
            ORDER BY fetched_at_utc
            """,
            (event["station_id"], event["target_date"]),
        ).fetchall()
        for row in obs_rows:
            self.obs_times.append(parse_ts(row["fetched_at_utc"]))
            self.obs_temps.append(float(row["temperature_c"]))
        # Point-in-time skew signals: Meteoblue predicted daily max and the
        # Ridge primary path, both indexed by the moment they became known.
        self.mb_times: list[datetime] = []
        self.mb_max: list[float] = []
        for row in db.execute(
            """
            SELECT fetched_at_utc, forecast_max_c FROM windy_forecasts
            WHERE station_id=? AND sample_local_date=? AND status='ok'
              AND forecast_max_c IS NOT NULL
            ORDER BY fetched_at_utc
            """,
            (event["station_id"], event["target_date"]),
        ):
            self.mb_times.append(parse_ts(row["fetched_at_utc"]))
            self.mb_max.append(float(row["forecast_max_c"]))
        self.ridge_times: list[datetime] = []
        self.ridge_path: list[float] = []
        for row in db.execute(
            """
            SELECT generated_at_utc, primary_path_c FROM weather_ridge_v2_snapshots
            WHERE event_id=? AND status='ok' AND primary_path_c IS NOT NULL
            ORDER BY generated_at_utc
            """,
            (event["event_id"],),
        ):
            self.ridge_times.append(parse_ts(row["generated_at_utc"]))
            self.ridge_path.append(float(row["primary_path_c"]))

    def observed_max_at(self, tick: datetime) -> float | None:
        window = self.obs_temps[: bisect_right(self.obs_times, tick)]
        return max(window) if window else None

    def mb_max_at(self, tick: datetime) -> float | None:
        idx = bisect_right(self.mb_times, tick)
        return self.mb_max[idx - 1] if idx else None

    def ridge_path_at(self, tick: datetime) -> float | None:
        idx = bisect_right(self.ridge_times, tick)
        return self.ridge_path[idx - 1] if idx else None

    def target_day(self) -> str:
        return self.event["target_date"]

    def quote(self, idx: int, temp: int) -> dict[str, Any] | None:
        return self.books[idx].get(f"{temp}°C")


def enter(
    data: EventData,
    center_only_shares: float | None = None,
    entry_hours: float = ENTRY_HOUR_DEFAULT,
    skew: str | None = None,
    no_neighbors: bool = False,
    center_yes_shares: float = 15.0,
) -> dict[str, Any]:
    """Entry decision shared by every variant; returns legs or a reason.
    center_only_shares: buy just the center bucket with this size instead
    of the three-bucket ladder (tests the concentration gradient endpoint).
    entry_hours: UTC hours after local-midnight for the entry slot.
    skew: 'mb'/'ridge' tilts side-leg shares by forecast-minus-center.
    no_neighbors: replace the YES side legs with 5 NO shares each at the
    real no-book ask VWAP (center-vs-rivals mix)."""
    start = day_start(data.target_day())
    entry_dt = start + timedelta(hours=entry_hours)
    deadline = entry_dt + ENTRY_DEADLINE_GAP
    entry_idx = next((i for i, s in enumerate(data.slots) if s >= entry_dt), None)
    if entry_idx is None or data.slots[entry_idx] > deadline:
        return {"ok": False, "status": "no_entry_slot"}
    tick = data.slots[entry_idx]
    mids: dict[int, float] = {}
    spreads: dict[int, float] = {}
    for temp in {parse_bucket(label) for label in data.books[entry_idx]}:
        if temp is None:
            continue
        row = data.quote(entry_idx, temp)
        if row is None or row["yes_best_bid"] is None or row["yes_best_ask"] is None:
            continue
        spread = row["yes_best_ask"] - row["yes_best_bid"]
        if spread < 0 or spread > MAX_LEG_SPREAD:
            continue
        mids[temp] = (row["yes_best_bid"] + row["yes_best_ask"]) / 2.0
        spreads[temp] = spread
    ranked = sorted(mids.items(), key=lambda item: (-item[1], item[0]))
    if not ranked:
        return {"ok": False, "status": "not_enough_exact_buckets"}
    center, top_mid = ranked[0]

    if center_only_shares is not None:
        if len(ranked) < 2 or top_mid - ranked[1][1] < CENTER_LEAD_MIN:
            return {"ok": False, "status": "center_lead_too_small"}
        row = data.quote(entry_idx, center)
        vwap, _filled = book_vwap(row["yes_book_json"], center_only_shares, "ask") if row else (None, 0)
        if vwap is None:
            return {"ok": False, "status": "insufficient_entry_depth"}
        fee = fee_for(center_only_shares, vwap)
        cost = center_only_shares * vwap + fee
        if cost > MAX_PACKAGE_COST + 1e-9:
            return {"ok": False, "status": "package_over_budget"}
        return {
            "ok": True,
            "entry_idx": entry_idx,
            "entry_tick": tick,
            "center": center,
            "legs": (center,),
            "plan": [{"bucket": center, "shares": center_only_shares, "price": vwap, "fee": fee, "cost": cost}],
            "max_leg_spread": spreads[center],
        }

    if len(mids) < 3:
        return {"ok": False, "status": "not_enough_exact_buckets"}
    if top_mid - ranked[1][1] < CENTER_LEAD_MIN:
        return {"ok": False, "status": "center_lead_too_small"}
    legs = (center - 1, center, center + 1)
    if any(temp not in mids for temp in legs):
        return {"ok": False, "status": "missing_neighbor_leg"}

    # Frozen V1-era candidate gate: the 1/3/1 baseline package must be
    # neither too cheap nor too expensive. Evaluated identically for every
    # ladder-family structure (S0/tilt/MIX) so eligibility stays comparable.
    baseline_notional = 0.0
    for temp, unit in zip(legs, (1.0, 3.0, 1.0)):
        brow = data.quote(entry_idx, temp)
        base_vwap, _b = book_vwap(brow["yes_book_json"], unit, "ask") if brow else (None, 0)
        if base_vwap is None:
            return {"ok": False, "status": "insufficient_entry_depth"}
        baseline_notional += base_vwap * unit
    if not (1.50 < baseline_notional <= 2.00):
        return {"ok": False, "status": "baseline_cost_out_of_band"}

    # Directional tilt: extra 5 shares on the side the signal leans toward.
    # Shares stay multiples of the 5-share minimum order size; a missing
    # signal falls back to the symmetric ladder and is counted as such.
    weights = WEIGHTS
    tilt = None
    signal = None
    if skew is not None:
        signal = data.mb_max_at(tick) if skew == "mb" else data.ridge_path_at(tick)
        if signal is not None:
            delta = float(signal) - center
            if delta >= SKEW_THRESHOLD_C:
                weights, tilt = WEIGHTS_TILT_UP, "up"
            elif delta <= -SKEW_THRESHOLD_C:
                weights, tilt = WEIGHTS_TILT_DOWN, "down"
            else:
                tilt = "flat"

    plan = []
    total = 0.0

    if no_neighbors:
        # Center-vs-rivals mix: 15 center YES + 5 NO per neighbor at the
        # real no-book ask VWAP; the shared gates above already ran so
        # eligibility stays identical to the frozen ladder.
        crow = data.quote(entry_idx, center)
        yes_vwap, _f = (
            book_vwap(crow["yes_book_json"], center_yes_shares, "ask") if crow else (None, 0)
        )
        if yes_vwap is None:
            return {"ok": False, "status": "insufficient_entry_depth"}
        fee_yes = fee_for(center_yes_shares, yes_vwap)
        cost_yes = center_yes_shares * yes_vwap + fee_yes
        plan.append({"bucket": center, "shares": center_yes_shares, "price": yes_vwap,
                     "fee": fee_yes, "cost": cost_yes, "side": "YES"})
        total += cost_yes
        for temp in (center - 1, center + 1):
            nrow = data.quote(entry_idx, temp)
            no_json = (nrow or {}).get("no_book_json") if isinstance(nrow, dict) else None
            no_vwap, _nf = book_vwap(no_json, 5.0, "ask")
            if no_vwap is None:
                return {"ok": False, "status": "insufficient_no_depth"}
            fee_no = fee_for(5.0, no_vwap)
            cost_no = 5.0 * no_vwap + fee_no
            plan.append({"bucket": temp, "shares": 5.0, "price": no_vwap,
                         "fee": fee_no, "cost": cost_no, "side": "NO"})
            total += cost_no
        if total > MAX_PACKAGE_COST + 1e-9:
            return {"ok": False, "status": "package_over_budget"}
        return {
            "ok": True,
            "entry_idx": entry_idx,
            "entry_tick": tick,
            "center": center,
            "legs": legs,
            "plan": plan,
            "weights": [center_yes_shares, 5.0, 5.0],
            "tilt": "mix",
            "skew_signal": None,
            "max_leg_spread": max(spreads[t] for t in legs),
        }

    for temp, weight in zip(legs, weights):
        row = data.quote(entry_idx, temp)
        vwap, _filled = book_vwap(row["yes_book_json"], weight, "ask")
        if vwap is None:
            return {"ok": False, "status": "insufficient_entry_depth"}
        fee = fee_for(weight, vwap)
        cost = weight * vwap + fee
        plan.append({"bucket": temp, "shares": weight, "price": vwap, "fee": fee, "cost": cost})
        total += cost
    if total > MAX_PACKAGE_COST + 1e-9:
        return {"ok": False, "status": "package_over_budget"}
    return {
        "ok": True,
        "entry_idx": entry_idx,
        "entry_tick": tick,
        "center": center,
        "legs": legs,
        "plan": plan,
        "weights": list(weights),
        "tilt": tilt,
        "skew_signal": signal,
        "max_leg_spread": max(spreads[t] for t in legs),
    }


def simulate(
    data: EventData,
    variant: str,
    reach_rate: float | None = None,
    entry_hours: float = ENTRY_HOUR_DEFAULT,
    skew: str | None = None,
) -> dict[str, Any]:
    """reach_rate: °C/hour sustained-rise bound used by the two-sided
    reachability exit (None disables that exit; the hard O > temp rule
    stays). Variants: S0 static; D1=R1 hard exits; D2=D1+R3; D3=D2+R2;
    E0=two-sided exits only; E3=two-sided+R3+R2."""
    use_center_only = variant.startswith("C") or variant.startswith("MIX")
    use_mix = variant.startswith("MIX")
    use_r1 = variant in ("D1", "D2", "D3")
    use_reach = variant in ("E0", "E3", "MIXE15", "MIXE10")
    use_r3 = variant in ("D2", "D3", "E3") and not use_center_only
    use_r2 = variant in ("D3", "E3") and not use_center_only
    if use_reach and reach_rate is None:
        reach_rate = 1.0

    entry = enter(
        data,
        {"C10": 10.0, "C15": 15.0, "C20": 20.0}.get(variant) if use_center_only and not use_mix else None,
        entry_hours=entry_hours,
        skew=skew if not use_center_only else None,
        no_neighbors=use_mix,
        center_yes_shares=MIX_CENTER_SHARES.get(variant, 15.0) if use_mix else 15.0,
    )
    base = {
        "variant": variant,
        "city": data.event["city"],
        "target_date": data.event["target_date"],
        "winning_temp": data.winning_temp,
    }
    if not entry["ok"]:
        return {**base, "status": entry["status"], "pnl": None}

    positions = {
        leg["bucket"]: leg["shares"] for leg in entry["plan"] if leg.get("side") != "NO"
    }
    no_positions = {
        leg["bucket"]: leg["shares"] for leg in entry["plan"] if leg.get("side") == "NO"
    }
    actions: list[dict[str, Any]] = [
        {
            "tick": entry["entry_tick"].isoformat(),
            "action": "BUY",
            "bucket": leg["bucket"],
            "shares": leg["shares"],
            "side": leg.get("side", "YES"),
            "price": round(leg["price"], 4),
            "fee": round(leg["fee"], 4),
            "cash_flow": -round(leg["cost"], 4),
        }
        for leg in entry["plan"]
    ]
    cash_spent = sum(leg["cost"] for leg in entry["plan"])
    cash_in = 0.0
    winner_adds = 0
    reinvests = 0
    marked_dead: dict[int, str] = {}

    def open_capital() -> float:
        return cash_spent - cash_in

    center = entry["center"]
    for idx in range(entry["entry_idx"] + 1, len(data.slots)):
        tick = data.slots[idx]
        if not any(shares > 0 for shares in positions.values()):
            break
        observed_max = data.observed_max_at(tick)

        # Exits: hard path-failure (O passed the bucket) plus optional
        # reachability failure (bucket cannot be reached before ~18:00
        # local even at a sustained rise of `reach_rate` °C/hour). Failed
        # sells keep the shares and retry on every later tick.
        exit_reasons: dict[int, str] = {}
        if (use_r1 or use_reach) and observed_max is not None:
            hours_left = None
            if use_reach:
                local_dt = tick + timedelta(hours=8)
                if local_dt.strftime("%Y-%m-%d") == data.event["target_date"]:
                    hours_left = max(0.0, 18.0 - (local_dt.hour + local_dt.minute / 60.0))
            reachable = (
                observed_max + reach_rate * hours_left
                if hours_left is not None else None
            )
            for temp in sorted(positions):
                shares = positions[temp]
                if shares <= 0:
                    continue
                if observed_max > temp:
                    exit_reasons[temp] = "PASSED_ABOVE"
                elif reachable is not None and reachable < temp - 1e-9:
                    exit_reasons[temp] = "UNREACHABLE"
        for temp in sorted(exit_reasons):
            shares = positions[temp]
            row = data.quote(idx, temp)
            if row is None:
                continue
            vwap, _depth = book_vwap(row["yes_book_json"], shares, "bid")
            if vwap is None or vwap <= SELL_FLOOR_PRICE:
                continue  # keep shares; retry next tick
            fee = fee_for(shares, vwap)
            cash = shares * vwap - fee
            cash_in += cash
            positions[temp] = 0.0
            actions.append({
                "tick": tick.isoformat(), "action": "SELL_DEAD", "bucket": temp,
                "reason": exit_reasons[temp], "shares": shares,
                "price": round(vwap, 4), "fee": round(fee, 4),
                "cash_flow": round(cash, 4),
            })
        if exit_reasons:
            for temp, reason in exit_reasons.items():
                if positions.get(temp, 0.0) > 0:
                    marked_dead[temp] = reason

        # R3: recycle salvage into the current alive leader whenever a leg
        # exited this tick (sold or marked).
        if use_r3 and exit_reasons and reinvests < 2 and observed_max is not None \
                and before_new_buy_cutoff(tick):
            best: tuple[int, float, dict[str, Any]] | None = None
            for label, candidate in data.books[idx].items():
                cand_temp = parse_bucket(label)
                if (
                    cand_temp is None or observed_max > cand_temp
                    or candidate["yes_best_bid"] is None
                    or candidate["yes_best_ask"] is None
                ):
                    continue
                spread = candidate["yes_best_ask"] - candidate["yes_best_bid"]
                if spread < 0 or spread > MAX_LEG_SPREAD:
                    continue
                mid = (candidate["yes_best_bid"] + candidate["yes_best_ask"]) / 2.0
                if best is None or mid > best[1]:
                    best = (cand_temp, mid, candidate)
            if best is not None and best[1] <= REINVEST_ASK_MAX:
                vwap, _depth = book_vwap(best[2]["yes_book_json"], 5.0, "ask")
                if vwap is not None:
                    fee = fee_for(5.0, vwap)
                    cost = 5.0 * vwap + fee
                    if cost + open_capital() <= TOTAL_DEPLOY_CAP + 1e-9:
                        cash_spent += cost
                        positions[best[0]] = positions.get(best[0], 0.0) + 5.0
                        reinvests += 1
                        actions.append({
                            "tick": tick.isoformat(), "action": "REINVEST",
                            "bucket": best[0], "shares": 5.0,
                            "price": round(vwap, 4), "fee": round(fee, 4),
                            "cash_flow": -round(cost, 4),
                        })

        # R2: add to the live center once observations confirm the path.
        if (
            use_r2 and winner_adds < 1 and positions.get(center, 0) > 0
            and observed_max is not None and observed_max >= center - 1
            and before_new_buy_cutoff(tick)
        ):
            row = data.quote(idx, center)
            if row is not None:
                vwap, _depth = book_vwap(row["yes_book_json"], 5.0, "ask")
                if vwap is not None and vwap <= WINNER_ADD_ASK_MAX:
                    fee = fee_for(5.0, vwap)
                    cost = 5.0 * vwap + fee
                    if cost + open_capital() <= TOTAL_DEPLOY_CAP + 1e-9:
                        cash_spent += cost
                        positions[center] += 5.0
                        winner_adds += 1
                        actions.append({
                            "tick": tick.isoformat(), "action": "WINNER_ADD", "bucket": center,
                            "shares": 5.0, "price": round(vwap, 4), "fee": round(fee, 4),
                            "cash_flow": -round(cost, 4),
                        })

    payout = sum(
        shares for temp, shares in positions.items()
        if shares > 0 and temp == data.winning_temp
    )
    # NO legs pay $1 per share when their bucket does NOT win; they are
    # never exited intraday (a passed bucket makes its NO a sure winner).
    payout += sum(
        shares for temp, shares in no_positions.items()
        if shares > 0 and temp != data.winning_temp
    )
    pnl = payout + cash_in - cash_spent
    return {
        **base,
        "status": "ok",
        "center": center,
        "legs": list(entry["legs"]),
        "weights": entry.get("weights"),
        "tilt": entry.get("tilt"),
        "skew_signal": entry.get("skew_signal"),
        "entry_tick": entry["entry_tick"].isoformat(),
        "entry_max_spread": round(entry["max_leg_spread"], 4),
        "cash_spent": round(cash_spent, 4),
        "cash_in": round(cash_in, 4),
        "payout": round(payout, 4),
        "pnl": round(pnl, 4),
        "actions": actions,
        "winner_adds": winner_adds,
        "reinvests": reinvests,
        "sell_dead": sum(1 for a in actions if a["action"] == "SELL_DEAD"),
        "dead_unsold": len(marked_dead),
        "exit_reasons": sorted(set(marked_dead.values())),
    }


def date_block_bootstrap(daily_values: dict[str, float], seed: int = BOOTSTRAP_SEED) -> dict[str, float | int]:
    dates = sorted(daily_values)
    if not dates:
        return {"mean_lb05": 0.0, "mean_diff": 0.0, "positive_dates": 0, "negative_dates": 0}
    values = [daily_values[d] for d in dates]
    rng = random.Random(seed)
    means = []
    for _ in range(BOOTSTRAP_ITERS):
        sample = [values[rng.randrange(len(values))] for _ in range(len(values))]
        means.append(statistics.fmean(sample))
    means.sort()
    return {
        "mean_lb05": round(means[int(0.05 * len(means))], 4),
        "mean_diff": round(statistics.fmean(values), 4),
        "positive_dates": sum(1 for v in values if v > 0),
        "negative_dates": sum(1 for v in values if v < 0),
    }


def summarize(results: list[dict[str, Any]], label: str) -> dict[str, Any]:
    ok = [r for r in results if r["status"] == "ok"]
    daily: dict[str, float] = {}
    for r in ok:
        daily[r["target_date"]] = daily.get(r["target_date"], 0.0) + r["pnl"]
    total_cost = sum(r["cash_spent"] - r["cash_in"] for r in ok)
    total_pnl = sum(r["pnl"] for r in ok)
    wins = sum(1 for r in ok if r["pnl"] > 0)
    boot = date_block_bootstrap(daily)
    return {
        "label": label,
        "city_days": len(ok),
        "dates": len(daily),
        "total_net_cost": round(total_cost, 3),
        "total_pnl": round(total_pnl, 3),
        "roi": round(total_pnl / total_cost, 4) if total_cost else None,
        "hit_rate": round(wins / len(ok), 4) if ok else None,
        "worst_city_day": round(min((r["pnl"] for r in ok), default=0.0), 3),
        "best_city_day": round(max((r["pnl"] for r in ok), default=0.0), 3),
        "daily_mean_pnl": boot["mean_diff"],
        "date_block_mean_pnl_lb05": boot["mean_lb05"],
        "positive_dates": boot["positive_dates"],
        "negative_dates": boot["negative_dates"],
        "sell_dead": sum(r["sell_dead"] for r in ok),
        "dead_unsold": sum(
            1 for r in ok for a in r["actions"] if str(a.get("action", "")).startswith("DEAD_")
        ),
        "winner_adds": sum(r["winner_adds"] for r in ok),
        "reinvests": sum(r["reinvests"] for r in ok),
        "daily_pnl_by_date": {d: round(v, 4) for d, v in sorted(daily.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("data/weather_market_monitor.sqlite3"))
    parser.add_argument("--output-json", type=Path, default=Path("research/output/weather_dynamic_rebalance_backtest.json"))
    parser.add_argument("--output-md", type=Path, default=Path("research/output/weather_dynamic_rebalance_backtest.md"))
    args = parser.parse_args()

    db = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    universe = load_universe(db)

    # One pass over the universe; simulate every variant on identical data.
    variant_results: dict[str, list[dict[str, Any]]] = {label: [] for label in RUN_SPECS}
    skipped: dict[str, int] = {}
    for event in universe:
        data = EventData(db, event)
        probe = simulate(data, "S0")  # reused only by the identical default spec
        for label, (variant, rate, hours, skew) in RUN_SPECS.items():
            if label == "S0":
                result = probe
            else:
                result = simulate(data, variant, rate, entry_hours=hours, skew=skew)
            result["variant"] = label
            variant_results[label].append(result)
            if result["status"] != "ok":
                skipped[result["status"]] = skipped.get(result["status"], 0) + 1
    db.close()

    runs: dict[str, dict[str, Any]] = {}
    detail: dict[str, list[dict[str, Any]]] = {}
    for selection in ("A", "B"):
        sel_name = "all_eligible" if selection == "A" else "one_city_min_spread"
        for variant_label in (VARIANTS if selection == "A" else B_SELECTION_VARIANTS):
            results = variant_results[variant_label]
            if selection == "B":
                by_date: dict[str, list[dict[str, Any]]] = {}
                for result in results:
                    by_date.setdefault(result["target_date"], []).append(result)
                results = [
                    min(
                        [r for r in group if r["status"] == "ok"],
                        key=lambda r: (r["entry_max_spread"], r["city"]),
                    )
                    for _date, group in sorted(by_date.items())
                    if any(r["status"] == "ok" for r in group)
                ]
            label = f"{variant_label}|{sel_name}"
            runs[label] = summarize(results, label)
            detail[label] = results

    paired: dict[str, dict[str, float | int]] = {}
    for selection in ("A", "B"):
        sel_name = "all_eligible" if selection == "A" else "one_city_min_spread"
        static_by_key = {
            (r["target_date"], r["city"]): r["pnl"]
            for r in detail[f"S0|{sel_name}"] if r["status"] == "ok"
        }
        for variant in ("D1", "D2", "D3", "E0", "E3", "C10", "C15", "C20",
                        "H09", "H10", "H11", "H12", "H13", "H14", "H15",
                        "AMB", "ARDG", "MIX15", "MIXE15", "MIX10", "MIXE10"):
            if f"{variant}|{sel_name}" not in detail:
                continue
            dyn_by_key = {
                (r["target_date"], r["city"]): r["pnl"]
                for r in detail[f"{variant}|{sel_name}"] if r["status"] == "ok"
            }
            diffs: dict[str, float] = {}
            for key, base_pnl in static_by_key.items():
                if key in dyn_by_key:
                    date = key[0]
                    diffs[date] = diffs.get(date, 0.0) + (dyn_by_key[key] - base_pnl)
            paired[f"{variant}-S0|{sel_name}"] = date_block_bootstrap(diffs)

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe_city_days": len(universe),
        "skipped_reasons": skipped,
        "runs": runs,
        "paired_vs_static": paired,
        "detail": detail,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 多桶 + 动态调仓 回测(无未来函数)", "",
        f"> 生成:{report['generated_at_utc']};样本:{len(universe)} 个城-日;"
        f"费用口径 shares×0.05×p×(1−p),买卖同式;基准入场 11:00 北京时间,5/15/5 三桶;"
        f"H09–H15 为入场时段网格,AMB/ARDG 为预测偏斜加仓(±0.5° 触发,5/15/10 或 10/15/5)。", "",
        "| 规则 | 城-日 | 净成本 | 净PnL | ROI | 胜率(城-日) | 最差单日 | 日均PnL | 正日期/负日期 | 日期块5%下界 | 死腿卖出 | 无买盘死腿 | 赢家补仓 | 残值再投 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, s in runs.items():
        lines.append(
            f"| {label} | {s['city_days']} | {s['total_net_cost']} | {s['total_pnl']} | "
            f"{round(s['roi'] * 100, 1) if s['roi'] is not None else 'N/A'}% | {s['hit_rate']} | "
            f"{s['worst_city_day']} | {s['daily_mean_pnl']} | {s['positive_dates']}/{s['negative_dates']} | "
            f"{s['date_block_mean_pnl_lb05']} | {s['sell_dead']} | {s['dead_unsold']} | "
            f"{s['winner_adds']} | {s['reinvests']} |"
        )
    lines += ["", "## 动态 vs 静态 配对差(同城-日相减,按目标日期聚合)", "",
              "| 对比 | 选择 | 平均日差 | 为正日期 | 为负日期 | 差值日期块5%下界 |", "|---|---|---:|---:|---:|---:|"]
    for key, stat in paired.items():
        lines.append(
            f"| {key.split('|')[0]} | {key.split('|')[1]} | {stat['mean_diff']} | "
            f"{stat['positive_dates']} | {stat['negative_dates']} | {stat['mean_lb05']} |"
        )
    lines += ["", f"跳过原因分布:`{json.dumps(skipped, ensure_ascii=False)}`", "", "## 边界", "",
              "- 规则在运行前冻结于脚本 docstring;权重沿用已冻结的 5/15/5,未做网格搜索。",
              "- 同日城市高度相关,日期块自助法只部分缓解;卖出按当时买盘VWAP、深度不足顺延下一快照。",
              "- 本结果是历史研究,不构成实盘认证。"]
    args.output_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(args.output_json), "md": str(args.output_md)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
