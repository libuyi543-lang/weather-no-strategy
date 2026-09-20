import unittest
from datetime import datetime, timezone

from weather_problem_solver import PROBLEM_VERSION, WeatherNoProblemSolver


UTC = timezone.utc


class WeatherNoProblemSolverTests(unittest.TestCase):
    def test_defines_the_trading_problem_without_solving_it(self):
        solver = WeatherNoProblemSolver({
            "singleNoBaseEdge": 0.10,
            "singleNoStrongEdge": 0.25,
            "singleNoMaxBuyPriceExclusive": 0.92,
        })
        result = solver.define(
            {
                "event": {
                    "city": "Wuhan",
                    "target_date": "2026-08-19",
                },
            },
            [{"bucketC": 33}, {"bucketC": 34}],
            datetime(2026, 8, 19, 3, 0, tzinfo=UTC),
            {"name": "TRANSITION"},
        )

        self.assertEqual(result["version"], PROBLEM_VERSION)
        self.assertEqual(result["problemType"], "WEATHER_MARKET_INFORMATION_GAP")
        self.assertEqual(result["decisionScope"]["tradableBucketsC"], [33, 34])
        self.assertEqual(result["decisionScope"]["maximumSelectedTargets"], 1)
        self.assertEqual(result["decisionScope"]["knownThroughUtc"], "2026-08-19T03:00:00+00:00")
        self.assertEqual(result["allowedConclusions"], [
            "BUY_NO_OVERSHOOT", "BUY_NO_CEILING", "WAIT",
        ])
        self.assertEqual(result["profitabilityCriterion"]["baseMinimumEdge"], 0.10)
        self.assertTrue(result["informationBoundary"]["futureInformationForbidden"])
        self.assertEqual(len(result["outcomeEvaluation"]), 3)


if __name__ == "__main__":
    unittest.main()
