import json
import unittest

from research.weather_pair_lock_grok_backtest import (
    PairLockBacktest,
    exact_bucket,
    vwap,
)


class PairLockGrokBacktestTests(unittest.TestCase):
    def test_exact_bucket_rejects_boundary_ranges(self):
        self.assertEqual(exact_bucket("35°C"), 35)
        self.assertIsNone(exact_bucket("36°C or higher"))

    def test_vwap_requires_full_five_share_depth(self):
        book = json.dumps({"asks": [
            {"price": 0.2, "size": 2}, {"price": 0.3, "size": 3},
        ]})
        self.assertAlmostEqual(vwap(book), 0.26)
        self.assertIsNone(vwap(json.dumps({"asks": [{"price": 0.2, "size": 4}]})))

    def test_locked_pair_must_be_adjacent_and_listed(self):
        context = {"market": {"markets": [
            {"outcomeRange": "34°C"}, {"outcomeRange": "35°C"},
            {"outcomeRange": "36°C"},
        ]}}
        valid = {
            "decision": "PAIR_LOCKED", "pair": ["34°C", "35°C"],
            "outsidePairRisk": "low", "weatherLockReason": "test",
            "marketAlreadyPriced": False, "evidence": ["test"],
            "invalidation": "test", "dataQuality": "ok",
        }
        PairLockBacktest.validate_response(valid, context)
        invalid = {**valid, "pair": ["34°C", "36°C"]}
        with self.assertRaises(RuntimeError):
            PairLockBacktest.validate_response(invalid, context)


if __name__ == "__main__":
    unittest.main()
