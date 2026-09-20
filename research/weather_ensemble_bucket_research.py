#!/usr/bin/env python3
"""Ensemble-member bucket probabilities vs point+sigma (point-in-time).

Spec frozen before first run (2026-08-25), motivated by the audited
yannbellec/polymarket-weather-bot approach (ECMWF-ENS member fractions ARE
bucket probabilities):

Data
    ensemble_forecasts rows, source='open_meteo_ensemble', model='ecmwf_ifs025',
    member_count=51, status='ok'. member_maxima_json holds raw daily-max
    values per member ('control' + 50 perturbed). Point-in-time rule: the
    row with the LATEST slot_utc <= entry tick (first snapshot slot at or
    after 03:00 UTC, skipped when later than entry+60min) for that
    (station_id, target_date); days without such a row are excluded from
    this study entirely (exclusion counts reported).

Part A - probability quality of RAW members (no trading), scored against
    settled winning_temp over exact buckets listed at the entry slot:
    MEMB : empirical member fractions, P(x) = #{round-half-up(v)==x}/N,
           mass below/above the listed exact range goes to 'below'/'above'.
    NRM  : Normal(mu, sigma) fitted on the SAME members, bucket x covers
           [x-0.5, x+0.5).
    MKT  : entry-slot YES mids renormalized over ALL listed outcomes.
    Metrics: summed binary log-loss (floor 1e-6) and Brier sum over listed
    exact buckets (identical formula across predictors, differences only);
    claimed-vs-realized center hit rates; MEMB reliability quintiles.
    Declared hypothesis: MEMB beats NRM; MEMB beating MKT would surprise.

Extension (second freeze, 2026-08-25, after the FIRST run's calibration
    verdict - the documented next step, not a post hoc pick):
    First run found RAW members unusable: winner - ens_mean averages
    +1.01C (per-station bias -0.85..+2.44), member sd 0.67 vs residual
    sd 1.38 -> biased AND narrow. Fix per the audited bot's core idea -
    per-station calibration from that station's OWN past only:
      training pairs : every (station, date) with an ok ensemble row and
        a METAR daily max (weather_observations, source='metar'); residual
        r = metar_max - ens_mean of that date's LATEST ensemble row;
        params from the rolling last 14 pairs STRICTLY BEFORE the target
        date (min 7 pairs, else the day is excluded from CAL scoring);
        bias b = mean(r); scale s = max(pstdev(r), 0.3).
      CAL_NRM   : Normal(ens_mean + b, s).
      CAL_MEMB  : members shifted by +b, empirical fractions (shape kept).
      CAL_MEMBK : members rescaled around the shifted mean by
                  k = clamp(s / member_sd, 1, 4), then fractions -
                  declared PRIMARY because the diagnosed truth is wider
                  than the ensemble, so inflation is principled.
    Part A2 scores all three on the CAL-covered subset. Part B2 re-runs
    the EV-gated MIXE10 with CAL_MEMBK probabilities:
      MX10_CAL_EEV0 (gate package EV > 0), MX10_CAL_EEV3 (> +0.03),
    paired against BASE on CAL-covered days only (days lacking training
    history cannot trade under this rule; that is part of the rule).

Part B - trading variants on the frozen MIXE10 structure (identical
    gates/legs/exits as weather_dynamic_rebalance_backtest; BASE calls the
    original simulate() unchanged):
    MX10_BASE : frozen MIXE10, re-run on the overlap window.
    MX10_EEV0 : plus gate full-package EV > 0 with RAW MEMB probabilities,
        EV = sum_over_legs(shares * P(leg wins)) - actual cash cost incl.
        taker fees (NO legs win iff their bucket loses).
    MX10_EEV3 : same with EV > +0.03 (declared sensitivity twin).

Integrity checks (the MIX15 dirty-data lesson):
    1. The local MIXE10 re-simulation with gate disabled must reproduce
       simulate()'s status and pnl exactly on every overlap city-day;
       any mismatch aborts the study loudly.
    2. Mirror identity no_ask = 1 - yes_bid spot-checked per entry.

Anti-lookahead: ensemble row chosen by slot_utc <= entry tick;
    calibration pairs strictly before the target date; books read only at
    the entry slot and later; settlement label from events.winning_range.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import weather_dynamic_rebalance_backtest as wdrb  # noqa: E402

DB = Path("data/weather_market_monitor.sqlite3")
OUT_JSON = Path("research/output/weather_ensemble_bucket_research.json")
OUT_MD = Path("research/output/weather_ensemble_bucket_research.md")
ENTRY_HOUR = wdrb.ENTRY_HOUR_DEFAULT  # 3.0 UTC = 11:00 Beijing
ENSEMBLE_SOURCE = "open_meteo_ensemble"
ENSEMBLE_MODEL = "ecmwf_ifs025"
PROB_FLOOR = 1e-6
CENTER_SHARES = 10.0
REACH_RATE = 1.0
EEV_THRESHOLDS = {"MX10_EEV0": 0.0, "MX10_EEV3": 0.03}
CAL_THRESHOLDS = {"MX10_CAL_EEV0": 0.0, "MX10_CAL_EEV3": 0.03}
CAL_ROLLING_WINDOW = 14
CAL_MIN_PAIRS = 7
CAL_SIGMA_FLOOR = 0.3
CAL_K_CLAMP = (1.0, 4.0)


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def round_half_up(v: float) -> int:
    return int(math.floor(v + 0.5))


def member_values(payload_json: str | None) -> list[float]:
    payload = json.loads(payload_json or "[]")
    return [
        float(m["maxC"]) for m in payload
        if isinstance(m, dict) and m.get("maxC") is not None
    ]


# ---------------------------------------------------------------- predictors


def memb_probabilities(values: list[float], temps: list[int]) -> dict[int | str, float]:
    """Empirical member fractions over listed exact buckets; tail mass to
    the synthetic 'below'/'above' ends."""
    lowest, highest = min(temps), max(temps)
    counts: dict[int | str, float] = {t: 0.0 for t in temps}
    counts["below"] = 0.0
    counts["above"] = 0.0
    for v in values:
        b = round_half_up(v)
        if b < lowest:
            counts["below"] += 1.0
        elif b > highest:
            counts["above"] += 1.0
        elif b in counts:
            counts[b] += 1.0
        else:  # interior gap (non-contiguous listing): nearest listed bucket
            near = min(temps, key=lambda t: (abs(t - b), t))
            counts[near] += 1.0
    n = float(len(values))
    return {k: c / n for k, c in counts.items()} if n else {}


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def normal_bucket_probabilities(mu: float, sigma: float,
                                temps: list[int]) -> dict[int | str, float]:
    probs: dict[int | str, float] = {}
    if sigma <= 1e-9:
        b = round_half_up(mu)
        for t in temps:
            probs[t] = 1.0 if t == b else 0.0
        probs["below"] = 1.0 if b < min(temps) else 0.0
        probs["above"] = 1.0 if b > max(temps) else 0.0
        return probs
    for t in temps:
        probs[t] = phi((t + 0.5 - mu) / sigma) - phi((t - 0.5 - mu) / sigma)
    probs["below"] = phi((min(temps) - 0.5 - mu) / sigma)
    probs["above"] = 1.0 - phi((max(temps) + 0.5 - mu) / sigma)
    return probs


def nrm_probabilities(values: list[float], temps: list[int]) -> dict[int | str, float]:
    mu = statistics.fmean(values)
    sigma = statistics.pstdev(values) if len(values) > 1 else 0.0
    return normal_bucket_probabilities(mu, sigma, temps)


def mkt_probabilities(books: dict[str, dict], temps: list[int]) -> dict[int | str, float]:
    """Entry-slot YES mids renormalized over ALL listed outcomes."""
    raw: dict[str, float] = {}
    for label, row in books.items():
        bid, ask = row["yes_best_bid"], row["yes_best_ask"]
        if bid is None or ask is None:
            continue
        raw[label] = max(0.0, (bid + ask) / 2.0)
    total = sum(raw.values())
    if total <= 0:
        return {}
    out: dict[int | str, float] = {t: raw.get(f"{t}°C", 0.0) / total for t in temps}
    out["below"] = sum(v for k, v in raw.items() if k.endswith("°C or below")) / total
    out["above"] = sum(v for k, v in raw.items() if k.endswith("°C or higher")) / total
    return out


# ------------------------------------------------------- calibration (freeze 2)


def load_calibration_pairs(db: sqlite3.Connection) -> dict[str, list[tuple[str, float]]]:
    """Per station, chronological [(target_date, residual)] with residual =
    METAR daily max - ens_mean of that date's LATEST ok ensemble row."""
    latest_row: dict[tuple[str, str], str] = {}
    for r in db.execute(
        """
        SELECT station_id, target_date, member_maxima_json FROM ensemble_forecasts
        WHERE source=? AND model=? AND status='ok'
        ORDER BY station_id, target_date, slot_utc
        """,
        (ENSEMBLE_SOURCE, ENSEMBLE_MODEL),
    ):
        latest_row[(r["station_id"], r["target_date"])] = r["member_maxima_json"]
    metar_max: dict[tuple[str, str], float] = {
        (r["station_id"], r["d"]): r["mx"]
        for r in db.execute(
            """
            SELECT station_id, sample_local_date AS d, MAX(temperature_c) AS mx
            FROM weather_observations
            WHERE source='metar' AND status='ok' AND temperature_c IS NOT NULL
            GROUP BY station_id, sample_local_date
            """
        )
    }
    pairs: dict[str, list[tuple[str, float]]] = {}
    for (station, date), payload in sorted(latest_row.items()):
        obs = metar_max.get((station, date))
        if obs is None:
            continue
        values = member_values(payload)
        if len(values) < 10:
            continue
        pairs.setdefault(station, []).append((date, obs - statistics.fmean(values)))
    return pairs


def calibration_params(
    pairs: list[tuple[str, float]], target_date: str,
) -> tuple[float, float] | None:
    """Rolling last WINDOW residuals strictly before target_date; min count."""
    history = [r for d, r in pairs if d < target_date][-CAL_ROLLING_WINDOW:]
    if len(history) < CAL_MIN_PAIRS:
        return None
    return statistics.fmean(history), max(statistics.pstdev(history), CAL_SIGMA_FLOOR)


def cal_nrm_probabilities(values: list[float], temps: list[int],
                          bias: float, scale: float) -> dict[int | str, float]:
    return normal_bucket_probabilities(statistics.fmean(values) + bias, scale, temps)


def score_multiclass(probs: dict[int | str, float], winner: int,
                     temps: list[int]) -> tuple[float, float] | None:
    if winner not in temps:
        return None
    ll = 0.0
    brier = 0.0
    for t in temps:
        p = max(PROB_FLOOR, min(1.0, probs.get(t, 0.0)))
        y = 1.0 if t == winner else 0.0
        ll += -(y * math.log(p) + (1 - y) * math.log(1 - p))
        brier += (p - y) ** 2
    return ll, brier


# ------------------------------------------------------------------ context


class DayContext:
    """Light point-in-time loader: entry-slot book + pre-entry ensemble row."""

    def __init__(self, db: sqlite3.Connection, event: dict):
        self.event = event
        self.winning_temp = wdrb.parse_bucket(event["winning_range"] or "")
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
        self.entry_slot = parse_ts(row[0]) if row else None
        self.books: dict[str, dict] = {}
        if self.entry_slot is not None:
            for r in db.execute(
                """
                SELECT outcome_range, yes_best_bid, yes_best_ask, yes_book_json,
                       no_best_bid, no_best_ask, no_book_json
                FROM market_snapshots WHERE event_id=? AND slot_utc=?
                """,
                (event["event_id"], row[0]),
            ):
                self.books[r["outcome_range"]] = dict(r)
        rows = db.execute(
            """
            SELECT slot_utc, member_maxima_json FROM ensemble_forecasts
            WHERE station_id=? AND target_date=? AND source=? AND model=?
              AND status='ok' AND slot_utc<=?
            """,
            (
                event["station_id"], event["target_date"], ENSEMBLE_SOURCE,
                ENSEMBLE_MODEL, entry_dt.isoformat(),
            ),
        ).fetchall()
        chosen = max(rows, key=lambda r: r["slot_utc"], default=None)
        self.members: list[float] = member_values(chosen["member_maxima_json"]) if chosen else []
        self.ens_slot = chosen["slot_utc"] if chosen else None

    def market_center(self) -> tuple[int | None, float | None]:
        best: tuple[int, float] | None = None
        for label, row in self.books.items():
            temp = wdrb.parse_bucket(label)
            bid, ask = row["yes_best_bid"], row["yes_best_ask"]
            if temp is None or bid is None or ask is None:
                continue
            spread = ask - bid
            if spread < 0 or spread > wdrb.MAX_LEG_SPREAD:
                continue
            mid = (bid + ask) / 2.0
            if best is None or (-mid, temp) < (-best[1], best[0]):
                best = (temp, mid)
        return (best[0], best[1]) if best else (None, None)


# ---------------------------------------------------------------- simulation


def mixe10_simulate_with_gate(data: wdrb.EventData, entry: dict,
                              eev_threshold: float | None,
                              win_probs: dict[int | str, float]) -> dict:
    """Frozen MIXE10 holding/exits, optionally gated on full-package EV."""
    base = {
        "city": data.event["city"],
        "target_date": data.event["target_date"],
        "winning_temp": data.winning_temp,
    }
    plan = entry["plan"]
    cash_spent = sum(leg["cost"] for leg in plan)
    ev = sum(
        leg["shares"] * (
            (1.0 - win_probs.get(leg["bucket"], 0.0))
            if leg.get("side") == "NO"
            else win_probs.get(leg["bucket"], 0.0)
        ) - leg["cost"]
        for leg in plan
    )
    if eev_threshold is not None and ev < eev_threshold:
        return {**base, "status": "ensemble_ev_below_gate", "pnl": None,
                "package_ev": round(ev, 4)}

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
        if not any(s > 0 for s in positions.values()):
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
                continue  # keep shares; retry next tick
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

    payout = sum(s for t, s in positions.items() if s > 0 and t == data.winning_temp)
    payout += sum(s for t, s in no_positions.items() if s > 0 and t != data.winning_temp)
    return {
        **base,
        "status": "ok",
        "center": entry["center"],
        "entry_tick": entry["entry_tick"].isoformat(),
        "cash_spent": round(cash_spent, 4),
        "cash_in": round(cash_in, 4),
        "payout": round(payout, 4),
        "pnl": round(payout + cash_in - cash_spent, 4),
        "actions": actions,
        "sell_dead": sum(1 for a in actions if a["action"] == "SELL_DEAD"),
        "dead_unsold": len(marked_dead),
        "package_ev": round(ev, 4),
    }


def run_day(db: sqlite3.Connection, event: dict,
            cal: tuple[float, float] | None) -> tuple[dict, str] | None:
    data = wdrb.EventData(db, event)
    ctx = DayContext(db, event)

    def excluded(reason: str) -> tuple[dict, str]:
        return {"city": event["city"], "target_date": event["target_date"]}, reason

    if ctx.entry_slot is None:
        return excluded("no_entry_slot")
    if not ctx.members:
        return excluded("no_pre_entry_ensemble")
    if ctx.winning_temp is None:
        return excluded("winner_not_exact_bucket")
    temps = sorted({t for t in (wdrb.parse_bucket(l) for l in ctx.books) if t is not None})
    if len(temps) < 3:
        return excluded("too_few_exact_buckets")

    out: dict = {
        "city": event["city"],
        "target_date": event["target_date"],
        "winning_temp": ctx.winning_temp,
        "entry_slot": ctx.entry_slot.isoformat(),
        "ens_slot": ctx.ens_slot,
        "n_members": len(ctx.members),
        "member_mean": round(statistics.fmean(ctx.members), 3),
        "member_std": round(statistics.pstdev(ctx.members), 3),
        "cal_bias": round(cal[0], 4) if cal else None,
        "cal_scale": round(cal[1], 4) if cal else None,
    }
    center, center_mid = ctx.market_center()
    out["market_center"], out["market_center_mid"] = center, center_mid

    probs_raw = memb_probabilities(ctx.members, temps)
    predictors: dict[str, dict[int | str, float]] = {
        "MEMB": probs_raw,
        "NRM": nrm_probabilities(ctx.members, temps),
        "MKT": mkt_probabilities(ctx.books, temps),
    }
    if cal is not None:
        bias, scale = cal
        member_sd = statistics.pstdev(ctx.members)
        k = min(max(scale / member_sd, CAL_K_CLAMP[0]), CAL_K_CLAMP[1]) if member_sd > 1e-9 else 1.0
        mu = statistics.fmean(ctx.members)
        predictors["CAL_NRM"] = cal_nrm_probabilities(ctx.members, temps, bias, scale)
        predictors["CAL_MEMB"] = memb_probabilities([v + bias for v in ctx.members], temps)
        predictors["CAL_MEMBK"] = memb_probabilities(
            [mu + bias + (v - mu) * k for v in ctx.members], temps
        )
        out["cal_k"] = round(k, 4)
    out["has_cal"] = cal is not None

    for name, probs in predictors.items():
        scored = score_multiclass(probs, ctx.winning_temp, temps)
        if scored is None:
            return excluded("winner_outside_listed_buckets")
        out[f"{name}_logloss"] = round(scored[0], 6)
        out[f"{name}_brier"] = round(scored[1], 6)
        out[f"{name}_p_at_center"] = round(probs.get(center, 0.0), 6) if center else None

    entry = wdrb.enter(data, no_neighbors=True, center_yes_shares=CENTER_SHARES)
    out["gate_ok"] = bool(entry.get("ok"))
    sims: dict[str, dict] = {}
    if entry.get("ok"):
        mirror_failures = []
        for leg in entry["plan"]:
            brow = ctx.books.get(f"{leg['bucket']}°C") or {}
            bid, no_ask = brow.get("yes_best_bid"), brow.get("no_best_ask")
            if bid is not None and no_ask is not None \
                    and abs(bid + no_ask - 1.0) > 1e-6:
                mirror_failures.append(leg["bucket"])
        out["mirror_failures"] = mirror_failures
        sims["MX10_LOCAL_BASE"] = mixe10_simulate_with_gate(data, entry, None, probs_raw)
        for label, thr in EEV_THRESHOLDS.items():
            sims[f"sim_{label}"] = mixe10_simulate_with_gate(data, entry, thr, probs_raw)
        if cal is not None:
            for label, thr in CAL_THRESHOLDS.items():
                sims[f"sim_{label}"] = mixe10_simulate_with_gate(
                    data, entry, thr, predictors["CAL_MEMBK"]
                )
    sims["sim_MX10_BASE"] = wdrb.simulate(data, "MIXE10")
    out["sims"] = sims
    return out, ""


# ------------------------------------------------------------------ reporting


def summarize(results: list[dict]) -> dict:
    ok = [r for r in results if r["status"] == "ok"]
    daily: dict[str, float] = {}
    for r in ok:
        daily[r["target_date"]] = daily.get(r["target_date"], 0.0) + r["pnl"]
    cost = sum(r["cash_spent"] - r["cash_in"] for r in ok)
    pnl = sum(r["pnl"] for r in ok)
    boot = wdrb.date_block_bootstrap(daily)
    return {
        "city_days": len(ok),
        "net_cost": round(cost, 3),
        "pnl": round(pnl, 3),
        "roi": round(pnl / cost, 4) if cost else None,
        "hit_rate": round(sum(1 for r in ok if r["pnl"] > 0) / len(ok), 4) if ok else None,
        "daily_mean": boot["mean_diff"],
        "lb05": boot["mean_lb05"],
        "pos_dates": boot["positive_dates"],
        "neg_dates": boot["negative_dates"],
    }


def paired_vs(days: list[dict], var_key: str, subset: bool) -> dict:
    diffs: dict[str, float] = {}
    for d in days:
        if subset and not d["has_cal"]:
            continue
        base = d["sims"]["sim_MX10_BASE"]
        var = d["sims"].get(var_key)
        base_pnl = base["pnl"] if base["status"] == "ok" else 0.0
        var_pnl = var["pnl"] if var and var["status"] == "ok" else 0.0
        if base_pnl == 0.0 and var_pnl == 0.0:
            continue
        diffs[d["target_date"]] = diffs.get(d["target_date"], 0.0) + (var_pnl - base_pnl)
    return wdrb.date_block_bootstrap(diffs)


def main() -> None:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    universe = wdrb.load_universe(db)
    pairs = load_calibration_pairs(db)

    days: list[dict] = []
    excluded: dict[str, int] = {}
    integrity_failures: list[str] = []
    mirror_failure_days = 0
    for event in universe:
        cal = calibration_params(pairs.get(event["station_id"], []), event["target_date"])
        result = run_day(db, event, cal)
        if result is None:
            excluded["context_unusable"] = excluded.get("context_unusable", 0) + 1
            continue
        record, reason = result
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        sims = record["sims"]
        if record["gate_ok"]:
            ref, local = sims["sim_MX10_BASE"], sims["MX10_LOCAL_BASE"]
            same = ref["status"] == local["status"] and (
                ref["status"] != "ok" or abs(ref["pnl"] - local["pnl"]) < 1e-6
            )
            if not same:
                integrity_failures.append(f'{record["city"]} {record["target_date"]}')
            if record.get("mirror_failures"):
                mirror_failure_days += 1
        days.append(record)
    db.close()

    if integrity_failures:
        raise SystemExit(
            "INTEGRITY FAILURE: local MIXE10 re-simulation diverges from "
            f"wdrb.simulate on {len(integrity_failures)} days: {integrity_failures[:5]}"
        )

    def center_hit(d: dict) -> float:
        return 1.0 if d["market_center"] == d["winning_temp"] else 0.0

    part_a: dict[str, dict] = {}
    for pred in ("MEMB", "NRM", "MKT", "CAL_NRM", "CAL_MEMB", "CAL_MEMBK"):
        scored_days = [
            d for d in days if d.get(f"{pred}_logloss") is not None
        ]
        if not scored_days:
            continue
        s: dict = {
            "n_days": len(scored_days),
            "logloss_mean": round(statistics.fmean(d[f"{pred}_logloss"] for d in scored_days), 5),
            "brier_mean": round(statistics.fmean(d[f"{pred}_brier"] for d in scored_days), 5),
            "mean_p_at_center": round(
                statistics.fmean(d[f"{pred}_p_at_center"] for d in scored_days), 5
            ),
        }
        gate_days = [d for d in scored_days if d["gate_ok"]]
        if gate_days:
            s["claimed_p_center_gated"] = round(
                statistics.fmean(d[f"{pred}_p_at_center"] for d in gate_days), 5
            )
            s["realized_center_hit_gated"] = round(
                statistics.fmean(center_hit(d) for d in gate_days), 5
            )
        part_a[pred] = s

    def reliability(pred: str) -> list[dict]:
        ranked = sorted(
            [d for d in days if d.get(f"{pred}_p_at_center") is not None],
            key=lambda d: d[f"{pred}_p_at_center"],
        )
        q = max(1, len(ranked) // 5)
        table = []
        for i in range(0, len(ranked), q):
            chunk = ranked[i:i + q]
            if chunk:
                table.append({
                    "claim_mean": round(
                        statistics.fmean(d[f"{pred}_p_at_center"] for d in chunk), 4
                    ),
                    "hit_rate": round(statistics.fmean(center_hit(d) for d in chunk), 4),
                    "n": len(chunk),
                })
        return table

    part_b = {"MX10_BASE": summarize([d["sims"]["sim_MX10_BASE"] for d in days])}
    for label in (*EEV_THRESHOLDS, *CAL_THRESHOLDS):
        results = [
            d["sims"][f"sim_{label}"] for d in days if f"sim_{label}" in d["sims"]
        ]
        part_b[label] = summarize(results)

    paired = {f"{label}-vs-BASE": paired_vs(days, f"sim_{label}", subset=label.startswith("MX10_CAL"))
              for label in (*EEV_THRESHOLDS, *CAL_THRESHOLDS)}

    skip_reasons: dict[str, int] = {}
    for d in days:
        for sim in d["sims"].values():
            if sim["status"] != "ok":
                skip_reasons[sim["status"]] = skip_reasons.get(sim["status"], 0) + 1

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe_city_days": len(universe),
        "scored_days": len(days),
        "cal_covered_days": sum(1 for d in days if d["has_cal"]),
        "excluded": excluded,
        "mirror_failure_days": mirror_failure_days,
        "part_a_probability_quality": part_a,
        "reliability_quintiles": {p: reliability(p) for p in ("MEMB", "CAL_MEMBK")},
        "part_b_variants": part_b,
        "paired_vs_base": paired,
        "skip_reasons": skip_reasons,
        "detail": days,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 集合成员分数 → 桶概率(MEMB/NRM/MKT + 校准版)+ MIXE10 集合EV门槛回测", "",
        f"> 生成:{report['generated_at_utc']};全宇宙 {len(universe)} 城-日,"
        f"可评分 {len(days)},其中校准覆盖 {report['cal_covered_days']};"
        f"排除 {json.dumps(excluded, ensure_ascii=False)}。"
        f"时序规则:集合行 slot_utc ≤ 入场时刻(11:00 北京);校准仅用该站目标日之前的滚动 14 个残差对。"
        f"完整性:本地重放与原引擎逐日一致(失败即中止);镜像恒等失败天数 {mirror_failure_days}。", "",
        "## A/A2. 概率质量(对结算胜出桶;同式跨桶 log-loss/Brier 求和,只比差异)", "",
        "| 预测器 | 天数 | log-loss ↓ | Brier ↓ | 均值P(中心) | 门槛日声称P(中心) | 门槛日实际命中率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for pred, s in part_a.items():
        lines.append(
            f"| {pred} | {s['n_days']} | {s['logloss_mean']} | {s['brier_mean']} | "
            f"{s['mean_p_at_center']} | {s.get('claimed_p_center_gated', '-')} | "
            f"{s.get('realized_center_hit_gated', '-')} |"
        )
    lines += ["", "### 可靠性五分位", "",
              "| 预测器 | 声称P(中心) | 实际命中率 | 天数 |", "|---|---:|---:|---:|"]
    for pred, table in report["reliability_quintiles"].items():
        for r in table:
            lines.append(f"| {pred} | {r['claim_mean']} | {r['hit_rate']} | {r['n']} |")

    lines += ["", "## B/B2. MIXE10 + 集合EV门槛(结构/门槛/出场与冻结版一致)", "",
              "| 变体 | 成交城-日 | 净成本 | 净PnL | ROI | 胜率 | 日均PnL | 日期块5%下界 | 正/负日期 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for label, s in part_b.items():
        lines.append(
            f"| {label} | {s['city_days']} | {s['net_cost']} | {s['pnl']} | "
            f"{round(s['roi'] * 100, 1) if s['roi'] is not None else '-'}% | {s['hit_rate']} | "
            f"{s['daily_mean']} | {s['lb05']} | {s['pos_dates']}/{s['neg_dates']} |"
        )
    lines += ["", "### 配对差(vs MX10_BASE,CAL 系只在有校准覆盖的日子配对)", "",
              "| 对比 | 平均日差 | 为正日期 | 为负日期 | 5%下界 |", "|---|---:|---:|---:|---:|"]
    for key, stat in paired.items():
        lines.append(
            f"| {key} | {stat['mean_diff']} | {stat['positive_dates']} | "
            f"{stat['negative_dates']} | {stat['mean_lb05']} |"
        )
    lines += ["", f"跳过原因:`{json.dumps(skip_reasons, ensure_ascii=False)}`", "", "## 边界", "",
              "- 全部预测器、门槛与窗口在首跑前冻结于 docstring(A2 为首跑诊断后的文档化第二步)。",
              "- 校准目标用 METAR 日最高温(连续量),胜出桶由市场口径另行给出;两者≈取整关系。",
              "- 集合数据自 2026-07-29 起,重叠窗口短于主回测;同日城市相关,自助法只部分缓解。",
              "- 本结果是历史研究,不构成实盘认证。"]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(OUT_JSON), "md": str(OUT_MD)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
