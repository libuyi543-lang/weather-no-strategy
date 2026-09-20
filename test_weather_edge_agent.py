import json
import sqlite3
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from weather_edge_agent import EdgeAgent


class WeatherEdgeAgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = object.__new__(EdgeAgent)
        self.agent.config = {
            "strategyName": "edge_test", "reviewStartLocalMinutes": 0,
            "entryCutoffLocalMinutes": 1440, "minEdge": 0.05,
            "maxSharesPerOrder": 10, "maxAllInCostPerShare": 0.99,
            "minCashReserveUsdc": 0, "maxOpenNotionalPerCity": 100,
            "maxOpenNotionalTotal": 100, "initialCashUsdc": 100, "feeRate": 0,
        }
        self.agent.tz = ZoneInfo("UTC")
        self.agent.db = sqlite3.connect(":memory:")
        self.agent.db.row_factory = sqlite3.Row
        self.agent.db.executescript(
            """CREATE TABLE weather_edge_orders(
              order_id INTEGER PRIMARY KEY, strategy_name TEXT, event_id TEXT,
              market_id TEXT, city TEXT, status TEXT, settled_at_utc TEXT,
              notional_usdc REAL, fee_usdc REAL, settled_payout_usdc REAL);
            """
        )

    def tearDown(self):
        self.agent.db.close()

    def test_distribution_rejects_duplicate_bucket(self):
        payload = {"eventId": "e1", "buckets": [{"outcomeRange": "30 C"}, {"outcomeRange": "31 C"}]}
        response = {
            "eventId": "e1",
            "distribution": [
                {"outcomeRange": "30 C", "probability": 0.5},
                {"outcomeRange": "30 C", "probability": 0.5},
            ],
            "orders": [],
        }
        with self.assertRaisesRegex(RuntimeError, "exactly once"):
            self.agent.validate(payload, response)

    def test_order_below_edge_is_rejected(self):
        record = {
            "event_id": "e1", "market_id": "m1", "city": "Beijing", "edge": 0.01,
            "requested_shares": 1, "ai_probability": 0.42,
        }
        bucket = {"noBookJson": json.dumps({"asks": [{"price": 0.4, "size": 10}]}),
                  "yesBookJson": None, "noBid": 0.39, "noAsk": 0.41,
                  "yesBid": 0.59, "yesAsk": 0.61}
        self.assertIn("below", self.agent._reject_reason(record, bucket, "no", datetime.now(timezone.utc)))

    def test_open_position_is_rejected(self):
        self.agent.db.execute(
            "INSERT INTO weather_edge_orders VALUES(1,'edge_test','e1','m1','Beijing','FILLED',NULL,1,0,NULL)"
        )
        self.agent.db.commit()
        record = {"event_id": "e1", "market_id": "m1", "city": "Beijing", "edge": 0.2,
                  "requested_shares": 1, "ai_probability": 0.7}
        bucket = {"noBookJson": json.dumps({"asks": [{"price": 0.4, "size": 10}]}),
                  "yesBookJson": None, "noBid": 0.39, "noAsk": 0.41,
                  "yesBid": 0.59, "yesAsk": 0.61}
        self.assertIn("open position", self.agent._reject_reason(record, bucket, "no", datetime.now(timezone.utc)))

    def test_decision_allows_only_one_order(self):
        payload = {"eventId": "e1", "buckets": [{"outcomeRange": "30 C"}, {"outcomeRange": "31 C"}]}
        response = {
            "eventId": "e1",
            "distribution": [
                {"outcomeRange": "30 C", "probability": 0.5},
                {"outcomeRange": "31 C", "probability": 0.5},
            ],
            "orders": [
                {"outcomeRange": "30 C", "side": "YES", "shares": 1, "edgeReason": "a"},
                {"outcomeRange": "31 C", "side": "NO", "shares": 1, "edgeReason": "b"},
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "at most 1 order"):
            self.agent.validate(payload, response)

    def test_market_calibration_is_price_bucketed(self):
        self.agent.db.execute(
            """CREATE TABLE weather_edge_decisions(
                decision_id INTEGER PRIMARY KEY, strategy_name TEXT, city TEXT,
                market_json TEXT, winning_range TEXT, target_date TEXT,
                ai_brier REAL, market_brier REAL)"""
        )
        self.agent.db.execute(
            """INSERT INTO weather_edge_decisions VALUES(
                1,'edge_test','Beijing',?, '30 C','2026-08-01',0.1,0.2)""",
            (json.dumps([
                {"outcomeRange": "30 C", "marketProbability": 0.8},
                {"outcomeRange": "31 C", "marketProbability": 0.2},
            ]),),
        )
        result = self.agent.market_calibration("Beijing")
        self.assertEqual(result["sampleDecisions"], 1)
        high = next(item for item in result["reliability"] if item["range"] == "0.8-0.9")
        self.assertEqual(high["observedRate"], 1.0)

    def test_execution_probability_shrinks_toward_market(self):
        self.agent.config.update({
            "executionShrinkEarly": 0.5,
            "executionShrinkLate": 0.5,
            "executionShrinkSettlement": 0.25,
        })
        early = self.agent._execution_probability(0.9, 0.6, 1.0, 10)
        late = self.agent._execution_probability(0.9, 0.6, 1.0, 3)
        self.assertAlmostEqual(early, 0.75)
        self.assertAlmostEqual(late, 0.675)

    def test_deterministic_sizing_never_exceeds_cap(self):
        self.agent.config.update({"fractionalKelly": 0.15, "maxSharesPerOrder": 10})
        shares = self.agent._deterministic_shares(0.9, 0.4, 1.0)
        self.assertGreater(shares, 0)
        self.assertLessEqual(shares, 10)

    def test_review_due_uses_cooldown_and_material_market_move(self):
        self.agent.config.update({"minDecisionIntervalMinutes": 30, "marketMoveTrigger": 0.04})
        self.agent.db.execute(
            "CREATE TABLE weather_edge_decisions(decision_id INTEGER PRIMARY KEY,strategy_name TEXT,event_id TEXT,decided_at_utc TEXT,state_hash TEXT,weather_state_json TEXT)"
        )
        event = {"event_id": "e1", "end_date_utc": "2026-09-05T12:00:00Z", "city": "Beijing", "target_date": "2026-09-05"}
        buckets = [{"marketId": "m1", "marketProbability": 0.5, "yesAsk": 0.51, "noAsk": 0.51}]
        state = {"market": {"m1": {"probability": 0.5, "yesAsk": 0.51, "noAsk": 0.51}}, "weather": {}, "hoursToClose": 10}
        self.agent._trigger_state = lambda *_args: state
        due, _, reason = self.agent.review_due(event, buckets, datetime(2026, 9, 5, 10, tzinfo=timezone.utc))
        self.assertTrue(due)
        self.assertEqual(reason, "initial")
        self.agent.db.execute(
            "INSERT INTO weather_edge_decisions VALUES(?,?,?,?,?,?)",
            (1, "edge_test", "e1", "2026-09-05T09:40:00+00:00", "x", json.dumps(state)),
        )
        self.agent.db.commit()
        due, _, reason = self.agent.review_due(event, buckets, datetime(2026, 9, 5, 9, 50, tzinfo=timezone.utc))
        self.assertFalse(due)
        self.assertEqual(reason, "cooldown")


if __name__ == "__main__":
    unittest.main()
