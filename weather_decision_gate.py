#!/usr/bin/env python3
"""Deterministic consistency gate for one-call AI problem solutions."""

from __future__ import annotations

from typing import Any


class WeatherDecisionGate:
    """Require a complete hypothesis-to-selection chain before execution checks."""

    @staticmethod
    def validate(review_input: dict[str, Any], response: dict[str, Any]) -> None:
        solution = response.get("problemSolution")
        if not isinstance(solution, dict):
            raise RuntimeError("AI response requires problemSolution")

        universe_ids = {
            str(row.get("marketId")) for row in review_input.get("singleNoUniverse") or []
        }
        hypotheses = solution.get("hypotheses") or []
        hypothesis_by_id = {str(row.get("hypothesisId")): row for row in hypotheses}
        if len(hypothesis_by_id) != len(hypotheses):
            raise RuntimeError("problemSolution hypothesis IDs must be unique")
        for hypothesis in hypotheses:
            targets = {str(value) for value in hypothesis.get("targetMarketIds") or []}
            if not targets or not targets.issubset(universe_ids):
                raise RuntimeError("problemSolution hypothesis targets must be in the supplied universe")

        tests = solution.get("hypothesisTests") or []
        test_ids = [str(row.get("hypothesisId")) for row in tests]
        if len(test_ids) != len(set(test_ids)) or set(test_ids) != set(hypothesis_by_id):
            raise RuntimeError("every generated hypothesis must be tested exactly once")
        tests_by_id = {str(row.get("hypothesisId")): row for row in tests}

        checks = solution.get("mispricingChecks") or []
        check_keys: set[tuple[str, str]] = set()
        for check in checks:
            hypothesis_id = str(check.get("hypothesisId"))
            market_id = str(check.get("marketId"))
            key = (hypothesis_id, market_id)
            if key in check_keys:
                raise RuntimeError("mispricing checks must be unique per hypothesis and market")
            check_keys.add(key)
            if hypothesis_id not in hypothesis_by_id or market_id not in universe_ids:
                raise RuntimeError("mispricing check is outside the tested hypotheses or market universe")
            if (tests_by_id.get(hypothesis_id) or {}).get("verdict") != "SUPPORTED":
                raise RuntimeError("only a supported hypothesis may enter mispricing checks")

        selection = solution.get("selection") or {}
        decision = selection.get("decision")
        reviews = response.get("singleNoReviews") or []
        if len(reviews) > 1:
            raise RuntimeError("the problem contract allows at most one selected NO target")
        if decision == "WAIT":
            if reviews or selection.get("marketId") is not None or selection.get("hypothesisId") is not None:
                raise RuntimeError("WAIT selection cannot include a candidate")
            return
        if decision != "BUY" or len(reviews) != 1:
            raise RuntimeError("BUY selection requires exactly one candidate")

        candidate = reviews[0]
        market_id = str(selection.get("marketId"))
        hypothesis_id = str(selection.get("hypothesisId"))
        hypothesis = hypothesis_by_id.get(hypothesis_id)
        if hypothesis is None or market_id not in universe_ids:
            raise RuntimeError("selected hypothesis or market is outside the problem")
        if str(candidate.get("marketId")) != market_id or str(candidate.get("hypothesisId")) != hypothesis_id:
            raise RuntimeError("selected candidate must match problemSolution.selection")
        if candidate.get("thesis") != hypothesis.get("thesis"):
            raise RuntimeError("selected candidate thesis must match its hypothesis")
        if market_id not in {str(value) for value in hypothesis.get("targetMarketIds") or []}:
            raise RuntimeError("selected market must be a target of the selected hypothesis")
        if (tests_by_id.get(hypothesis_id) or {}).get("verdict") != "SUPPORTED":
            raise RuntimeError("a BUY requires a supported hypothesis")
        valid_check = any(
            str(row.get("hypothesisId")) == hypothesis_id
            and str(row.get("marketId")) == market_id
            and row.get("status") in {"UNPRICED", "PARTIALLY_PRICED"}
            for row in checks
        )
        if not valid_check:
            raise RuntimeError("a BUY requires a matching unpriced or partially priced market check")
