import unittest

from weather_decision_gate import WeatherDecisionGate


def solution(*, decision="BUY", verdict="SUPPORTED", status="UNPRICED"):
    buy = decision == "BUY"
    return {
        "hypotheses": [{
            "hypothesisId": "H1", "thesis": "NO_OVERSHOOT",
            "targetMarketIds": ["m34"],
        }],
        "hypothesisTests": [{"hypothesisId": "H1", "verdict": verdict}],
        "mispricingChecks": [{
            "hypothesisId": "H1", "marketId": "m34", "status": status,
        }],
        "selection": {
            "decision": decision,
            "marketId": "m34" if buy else None,
            "hypothesisId": "H1" if buy else None,
        },
    }


class WeatherDecisionGateTests(unittest.TestCase):
    def setUp(self):
        self.review_input = {"singleNoUniverse": [{"marketId": "m34"}]}

    def test_accepts_complete_buy_chain(self):
        WeatherDecisionGate.validate(self.review_input, {
            "problemSolution": solution(),
            "singleNoReviews": [{
                "marketId": "m34", "hypothesisId": "H1", "thesis": "NO_OVERSHOOT",
            }],
        })

    def test_rejects_buy_from_unsupported_hypothesis(self):
        with self.assertRaisesRegex(RuntimeError, "supported hypothesis"):
            WeatherDecisionGate.validate(self.review_input, {
                "problemSolution": solution(verdict="UNRESOLVED"),
                "singleNoReviews": [{
                    "marketId": "m34", "hypothesisId": "H1", "thesis": "NO_OVERSHOOT",
                }],
            })

    def test_wait_requires_empty_candidate_list(self):
        with self.assertRaisesRegex(RuntimeError, "WAIT selection"):
            WeatherDecisionGate.validate(self.review_input, {
                "problemSolution": solution(decision="WAIT"),
                "singleNoReviews": [{
                    "marketId": "m34", "hypothesisId": "H1", "thesis": "NO_OVERSHOOT",
                }],
            })


if __name__ == "__main__":
    unittest.main()
