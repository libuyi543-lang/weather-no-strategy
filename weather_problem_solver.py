#!/usr/bin/env python3
"""Problem definition layer for exact-temperature NO reviews."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from weather_data_store import iso_utc


PROBLEM_VERSION = "weather_no_mispricing_v1"


class WeatherNoProblemSolver:
    """Build the binding problem contract without making a trade decision."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def define(
        self,
        context: dict[str, Any],
        universe: list[dict[str, Any]],
        as_of: datetime,
        decision_phase: dict[str, Any],
    ) -> dict[str, Any]:
        event = context.get("event") or {}
        buckets = [row.get("bucketC") for row in universe if row.get("bucketC") is not None]
        return {
            "version": PROBLEM_VERSION,
            "problemType": "WEATHER_MARKET_INFORMATION_GAP",
            "objective": (
                "在市场完成重新定价前，找到一个可由单侧天气路径排除且NO价格尚未充分反映该路径的精确温度桶；"
                "扣除手续费和执行成本后必须具有正期望，否则等待"
            ),
            "decisionQuestion": (
                f"截至{iso_utc(as_of)}，{event.get('city')}在{event.get('target_date')}的可交易温度桶中，"
                "是否存在唯一一个仍有可执行错误定价的NO？"
            ),
            "decisionScope": {
                "city": event.get("city"),
                "targetDate": event.get("target_date"),
                "knownThroughUtc": iso_utc(as_of),
                "decisionPhase": decision_phase.get("name"),
                "tradableBucketCount": len(universe),
                "tradableBucketsC": buckets,
                "maximumSelectedTargets": 1,
            },
            "allowedConclusions": ["BUY_NO_OVERSHOOT", "BUY_NO_CEILING", "WAIT"],
            "validPaths": {
                "NO_OVERSHOOT": "仅用P(最终最高温>目标桶)排除目标桶",
                "NO_CEILING": "仅用P(最终最高温<目标桶)排除目标桶",
            },
            "profitabilityCriterion": {
                "formula": "conservativePathProbabilityLow - noAllInCostPerShare5 >= requiredMinimumEdge",
                "baseMinimumEdge": float(self.config.get("singleNoBaseEdge", 0.10)),
                "strongMinimumEdge": float(self.config.get("singleNoStrongEdge", 0.25)),
                "maximumNoBuyPriceExclusive": float(
                    self.config.get("singleNoMaxBuyPriceExclusive", 0.92)
                ),
                "marketRequirement": (
                    "必须指出尚未被当前价格充分吸收的新增天气信息或具体错误定价；"
                    "天气方向正确但价格没有优势不构成交易"
                ),
            },
            "informationBoundary": {
                "futureInformationForbidden": True,
                "useOnlyEvidenceKnownByUtc": iso_utc(as_of),
                "marketPriceIsPriorNotFact": True,
                "ridgeIsEvidenceNotDecision": True,
            },
            "nonGoals": [
                "不预测最终最高温落入哪个桶",
                "不追求每轮都交易",
                "不把多个相邻桶视为独立机会",
                "不把天气判断正确等同于交易有盈利价值",
            ],
            "waitWhen": [
                "没有明确且可证伪的单侧天气路径",
                "无法从相邻桶中选出唯一更优目标",
                "市场价格已经吸收天气变化",
                "保守概率相对全包成本没有足够净edge",
                "数据不完整、过期或证据互相冲突",
            ],
            "outcomeEvaluation": [
                "TRADE_PNL: 交易扣费后是否盈利",
                "THESIS_CORRECTNESS: 所选单侧天气路径是否成立",
                "MISPRICING_CORRECTNESS: 入场时市场是否确有未吸收信息",
            ],
        }
