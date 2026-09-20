#!/usr/bin/env python3
"""Pre-settlement stale-quote sweep (point-in-time, no lookahead).

Spec frozen before first run (2026-08-25), motivated by the VibeTrader
third action (settlement-eve sweep of stale mispriced resting orders;
their London 1/28 case: 0.50-0.65 asks left standing 8h into a resolved
outcome) and by Kalshi Eric&Jerry's largest day (+$24k from evening jumps
already implied by the airport METAR).

Idea
    After local 18:00 the daily max is nearly locked: it physically cannot
    fall, evening rises past ~18h are rare, and the fast METAR feed already
    reports every candidate. Any ask far below certainty on an outcome that
    the received observations have effectively decided is stale paper.

Lock rule (point-in-time)
    A slot qualifies when its Beijing-local time >= 18:00 AND at least one
    ok METAR row with fetched_at_utc <= slot exists (observed_max from
    received rows only). Candidate YES bucket = round-half-up(observed_max).
    Physically dead buckets = exact listed buckets x with observed_max > x.

Sweep rules (declared, no tuning)
    - Scan every snapshot slot from lock time to the last slot.
    - YES side: buy 5 shares of the candidate bucket when two-sided with
      spread <= MAX_LEG_SPREAD and ask-side VWAP for 5 shares <= 0.96
      (>=4c margin vs certainty as residual-risk buffer). Once per bucket.
    - NO side: for each dead bucket, buy 5 NO at no-book ask VWAP <= 0.96
      (NO pays $1 iff that bucket loses; a passed bucket always loses).
      Once per bucket.
    - Per city-day cash cap 15 USDC including fees; first-come execution,
      depth must fill all 5 shares in one book snapshot.
    - Everything held to settlement; fee = shares*0.05*p*(1-p) both sides.

Reported
    Trades split YES/NO with executed price margins; city-day coverage;
    total PnL / ROI / date-block bootstrap vs zero; windows seen but not
    executed (budget) counted; worst trade. Descriptive only: stale BID
    windows (bid >= 0.04 on a dead bucket) are counted, not simulated.

Anti-lookahead: observations filtered by fetched_at_utc <= tick; books
read only at their own slot; settlement label from events.winning_range.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import weather_dynamic_rebalance_backtest as wdrb  # noqa: E402

DB = Path("data/weather_market_monitor.sqlite3")
OUT_JSON = Path("research/output/weather_stale_sweep_backtest.json")
OUT_MD = Path("research/output/weather_stale_sweep_backtest.md")
LOCK_LOCAL_HOUR = 18.0
SWEEP_ASK_MAX = 0.96
SHARES = 5.0
MAX_PACKAGE_COST = 15.0
STALE_BID_FLOOR = 0.04  # descriptive only


def round_half_up(v: float) -> int:
    return int(math.floor(v + 0.5))


def simulate_day(data: wdrb.EventData) -> dict:
    base = {
        "city": data.event["city"],
        "target_date": data.event["target_date"],
        "winning_temp": data.winning_temp,
    }
    if data.winning_temp is None:
        return {**base, "status": "no_exact_winner", "trades": [], "n_trades": 0,
                "cash_spent": 0.0, "pnl": None,
                "seen_yes_windows": 0, "seen_no_windows": 0,
                "budget_blocked": 0, "stale_bid_windows_descriptive": 0}

    actions: list[dict] = []
    cash_spent = 0.0
    yes_bought: set[int] = set()
    no_bought: set[int] = set()
    seen_yes_windows = 0
    seen_no_windows = 0
    budget_blocked = 0
    stale_bid_windows = 0

    for idx, tick in enumerate(data.slots):
        local_dt = tick + timedelta(hours=8)
        if local_dt.hour + local_dt.minute / 60.0 < LOCK_LOCAL_HOUR:
            continue
        if local_dt.strftime("%Y-%m-%d") != data.event["target_date"]:
            break  # past local midnight: settlement imminent, stop scanning
        window = data.obs_temps[: bisect_right(data.obs_times, tick)]
        if not window:
            continue
        observed_max = max(window)

        # descriptive: dead-bucket YES bids still standing at >= floor
        for label, row in data.books[idx].items():
            temp = wdrb.parse_bucket(label)
            if temp is None or observed_max <= temp:
                continue
            bid_vwap, _f = wdrb.book_vwap(row["yes_book_json"], SHARES, "bid") if row else (None, 0)
            if bid_vwap is not None and bid_vwap >= STALE_BID_FLOOR:
                stale_bid_windows += 1

        cand = round_half_up(observed_max)
        # YES side
        if cand not in yes_bought:
            row = data.quote(idx, cand)
            if row is not None and row["yes_best_bid"] is not None \
                    and row["yes_best_ask"] is not None \
                    and row["yes_best_ask"] - row["yes_best_bid"] <= wdrb.MAX_LEG_SPREAD:
                vwap, _depth = wdrb.book_vwap(row["yes_book_json"], SHARES, "ask")
                if vwap is not None:
                    fee = wdrb.fee_for(SHARES, vwap)
                    cost = SHARES * vwap + fee
                    if vwap <= SWEEP_ASK_MAX:
                        seen_yes_windows += 1
                        if cost + cash_spent <= MAX_PACKAGE_COST + 1e-9:
                            yes_bought.add(cand)
                            cash_spent += cost
                            actions.append({
                                "tick": tick.isoformat(), "side": "YES",
                                "bucket": cand, "shares": SHARES,
                                "price": round(vwap, 4), "fee": round(fee, 4),
                                "cost": round(cost, 4),
                                "margin_if_win_per_share": round(1.0 - vwap - wdrb.FEE_RATE * vwap * (1 - vwap), 4),
                                "observed_max": observed_max,
                            })
                        else:
                            budget_blocked += 1
        # NO side on physically dead buckets
        for label in sorted(data.books[idx]):
            temp = wdrb.parse_bucket(label)
            if temp is None or temp in no_bought or observed_max <= temp:
                continue
            row = data.quote(idx, temp)
            no_json = (row or {}).get("no_book_json")
            no_vwap, _depth = wdrb.book_vwap(no_json, SHARES, "ask")
            if no_vwap is None:
                continue
            if no_vwap <= SWEEP_ASK_MAX:
                seen_no_windows += 1
                fee = wdrb.fee_for(SHARES, no_vwap)
                cost = SHARES * no_vwap + fee
                if cost + cash_spent <= MAX_PACKAGE_COST + 1e-9:
                    no_bought.add(temp)
                    cash_spent += cost
                    actions.append({
                        "tick": tick.isoformat(), "side": "NO",
                        "bucket": temp, "shares": SHARES,
                        "price": round(no_vwap, 4), "fee": round(fee, 4),
                        "cost": round(cost, 4),
                        "margin_if_dead_per_share": round(1.0 - no_vwap - wdrb.FEE_RATE * no_vwap * (1 - no_vwap), 4),
                        "observed_max": observed_max,
                    })
                else:
                    budget_blocked += 1

    payout = 0.0
    for t in actions:
        won = (t["bucket"] == data.winning_temp) if t["side"] == "YES" \
            else (t["bucket"] != data.winning_temp)
        if won:
            payout += t["shares"]
    pnl = payout - cash_spent
    return {
        **base,
        "status": "ok" if actions or seen_yes_windows or seen_no_windows else "no_windows",
        "trades": actions,
        "n_trades": len(actions),
        "cash_spent": round(cash_spent, 4),
        "payout": round(payout, 4),
        "pnl": round(pnl, 4),
        "seen_yes_windows": seen_yes_windows,
        "seen_no_windows": seen_no_windows,
        "budget_blocked": budget_blocked,
        "stale_bid_windows_descriptive": stale_bid_windows,
    }


def main() -> None:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    universe = wdrb.load_universe(db)
    results = []
    for event in universe:
        data = wdrb.EventData(db, event)
        results.append(simulate_day(data))
    db.close()

    traded = [r for r in results if r["n_trades"] > 0]
    daily: dict[str, float] = {}
    for r in traded:
        daily[r["target_date"]] = daily.get(r["target_date"], 0.0) + r["pnl"]
    total_cost = sum(r["cash_spent"] for r in traded)
    total_pnl = sum(r["pnl"] for r in traded)
    boot = wdrb.date_block_bootstrap(daily) if daily else {
        "mean_lb05": 0.0, "mean_diff": 0.0, "positive_dates": 0, "negative_dates": 0,
    }
    yes_trades: list[dict] = []
    no_trades: list[dict] = []
    yes_wins = no_wins = 0
    margins: list[float] = []
    for r in traded:
        for t in r["trades"]:
            won = (t["bucket"] == r["winning_temp"]) if t["side"] == "YES" \
                else (t["bucket"] != r["winning_temp"])
            (yes_trades if t["side"] == "YES" else no_trades).append(t)
            if t["side"] == "YES":
                yes_wins += 1 if won else 0
            else:
                no_wins += 1 if won else 0
            margins.append(t.get("margin_if_win_per_share",
                                 t.get("margin_if_dead_per_share")))
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "universe_city_days": len(universe),
        "city_days_with_trades": len(traded),
        "total_trades": len(yes_trades) + len(no_trades),
        "yes_trades": len(yes_trades),
        "yes_wins": yes_wins,
        "no_trades": len(no_trades),
        "no_wins": no_wins,
        "total_cost": round(total_cost, 3),
        "total_pnl": round(total_pnl, 3),
        "roi": round(total_pnl / total_cost, 4) if total_cost else None,
        "daily_mean_pnl": boot["mean_diff"],
        "date_block_lb05_vs_zero": boot["mean_lb05"],
        "positive_dates": boot.get("positive_dates"),
        "negative_dates": boot.get("negative_dates"),
        "worst_trade_margin": round(min(margins), 4) if margins else None,
        "seen_but_budget_blocked": sum(r["budget_blocked"] for r in results),
        "descriptive_stale_bid_windows": sum(r["stale_bid_windows_descriptive"] for r in results),
        "detail": results,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 结算前扫陈旧挂单回测(VibeTrader 第三动作)", "",
        f"> 生成:{report['generated_at_utc']};样本 {len(universe)} 城-日;"
        f"锁规则:北京时间 ≥18:00 且已有 METAR(仅用 fetched_at_utc ≤ 当时的行);"
        f"Candidate=round(O(t));YES 挂单价≤0.96、物理死桶 NO 价≤0.96,各买 5 股,"
        f"每城-日现金上限 15 USDC,持有到结算。", "",
        "| 指标 | 值 |", "|---|---:|",
        f"| 有成交的城-日 | {report['city_days_with_trades']} |",
        f"| 总交易数(YES/NO) | {report['yes_trades']}/{report['no_trades']} |",
        f"| YES 胜出 / NO 胜出 | {report['yes_wins']} / {report['no_wins']} |",
        f"| 总成本 / 总PnL | {report['total_cost']} / {report['total_pnl']} |",
        f"| ROI | {round((report['roi'] or 0) * 100, 2)}% |",
        f"| 日均PnL / 日期块5%下界(vs 0) | {report['daily_mean_pnl']} / {report['date_block_lb05_vs_zero']} |",
        f"| 预算外放弃窗口 | {report['seen_but_budget_blocked']} |",
        f"| 描述性:死桶高价买盘窗口(≥0.04) | {report['descriptive_stale_bid_windows']} |",
    ]
    if traded:
        lines += ["", "## 成交明细(按 PnL 排序前 30)", "",
                  "| 日期 | 城市 | 方向 | 桶 | 价格 | 若胜边际/股 | 该日PnL |", "|---|---|---|---:|---:|---:|---:|"]
        rows = []
        for r in traded:
            for t in r["trades"]:
                rows.append((r["target_date"], r["city"], t["side"], t["bucket"],
                             t["price"], t.get("margin_if_win_per_share",
                                               t.get("margin_if_dead_per_share")),
                             r["pnl"]))
        rows.sort(key=lambda x: x[-1])
        for d, c, s, b, p, m, pl in rows[:30]:
            lines.append(f"| {d} | {c} | {s} | {b} | {p} | {m} | {pl} |")
    lines += ["", "## 边界", "",
              "- 锁定后残余风险(傍晚升温跨过取整边界)由 0.96 阈值缓冲,未显式建模尾部。",
              "- 快照为小时级,真实扫单在分钟级会有更好价格也可能已被别人抢走;本结果偏保守。",
              "- 本结果是历史研究,不构成实盘认证。"]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(OUT_JSON), "md": str(OUT_MD)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
