import json
import sqlite3
import unittest
from pathlib import Path

from weather_ai_agent import validate_json_schema
from weather_dual_strategy import DualStrategyEngine


class WeatherOutcomeReviewerTests(unittest.TestCase):
    def setUp(self):
        self.engine = object.__new__(DualStrategyEngine)
        self.engine.config = {"strategyName": "dual_test"}
        self.engine.db = sqlite3.connect(":memory:")
        self.engine.db.row_factory = sqlite3.Row
        self.engine._init_runtime_schema()
        self.engine._init_dual_schema()
        self.engine.db.executescript(
            """
            CREATE TABLE events(
                event_id TEXT PRIMARY KEY,city TEXT,target_date TEXT,winning_range TEXT,resolved_at_utc TEXT
            );
            CREATE TABLE markets(market_id TEXT,event_id TEXT);
            INSERT INTO events VALUES('e1','Wuhan','2026-08-18','34°C','2026-08-18T23:00:00+00:00');
            INSERT INTO markets VALUES('m34','e1');
            INSERT INTO weather_dual_reviews(
                strategy_name,event_id,city,target_date,trigger_type,trigger_time_utc,state_hash,
                reviewed_at_utc,status,input_json,ai_response_json
            ) VALUES(
                'dual_test','e1','Wuhan','2026-08-18','scheduled','2026-08-18T03:00:00+00:00','s1',
                '2026-08-18T03:00:00+00:00','completed',
                '{"asOfUtc":"2026-08-18T03:00:00+00:00","problemDefinition":{"version":"v1"}}',
                '{"problemSolution":{"selection":{"decision":"WAIT"}},"singleNoReviews":[]}'
            );
            """
        )

    def tearDown(self):
        self.engine.db.close()

    def test_pending_input_uses_compact_decision_state(self):
        item = self.engine.pending_outcome_review_inputs()[0]
        self.assertEqual(item["event"]["event_id"], "e1")
        self.assertEqual(item["reviews"][0]["problemDefinition"], {"version": "v1"})
        self.assertEqual(item["reviews"][0]["problemSolution"]["selection"]["decision"], "WAIT")
        self.assertNotIn("input_json", item["reviews"][0])

    def test_historical_empty_candidate_response_maps_to_wait(self):
        item = self.engine.pending_outcome_review_inputs()[0]
        item["reviews"][0]["problemSolution"] = None
        item["reviews"][0]["hasAiResponse"] = True
        response = {
            "eventId": "e1", "executedTradeReviews": [], "missedOpportunities": [],
            "correctWaits": [{
                "decisionTimeUtc": "2026-08-18T03:00:00+00:00", "reason": "no edge",
            }],
        }
        self.engine.validate_outcome_review(item, response)

    def test_outcome_schema_supports_lucky_profit_and_valid_miss(self):
        schema = json.loads(Path("weather_outcome_review.schema.json").read_text(encoding="utf-8"))
        payload = {
            "generatedAt": "2026-08-19T00:00:00+00:00", "eventId": "e1",
            "overallAssessment": "mixed",
            "executedTradeReviews": [{
                "marketId": "m33", "classification": "LUCKY_PROFIT", "tradePnlUsdc": 2.0,
                "thesisCorrectness": "INCORRECT", "evidenceCorrectness": "INCORRECT",
                "mispricingCorrectness": "UNRESOLVED", "timingCorrectness": "CORRECT",
                "explanation": "NO won by the opposite path", "failureCause": "WEATHER_REASONING",
            }],
            "missedOpportunities": [{
                "decisionTimeUtc": "2026-08-18T03:00:00+00:00", "marketId": "m34",
                "counterfactualPnlUsdc": 2.0, "knowableThen": True, "executableThen": True,
                "classification": "VALID_MISS", "missedReason": "HYPOTHESIS_NOT_GENERATED",
                "evidenceAvailableThen": ["radiation recovery"],
                "minimalChange": "generate and test the rebound hypothesis",
            }],
            "correctWaits": [],
            "lessonCandidates": [{
                "level": "OBSERVATION", "statement": "one case only",
                "supportingCases": ["e1"], "validationNeeded": "more independent days",
            }],
        }
        validate_json_schema(payload, schema)

    def test_only_non_binding_lesson_candidates_enter_future_context(self):
        self.engine.db.execute(
            """INSERT INTO weather_dual_outcome_reviews(
                strategy_name,event_id,city,target_date,net_pnl_usdc,review_json,created_at_utc
            ) VALUES('dual_test','e1','Wuhan','2026-08-18',1.0,?, '2026-08-19T00:00:00+00:00')""",
            (json.dumps({"lessonCandidates": [{
                "level": "OBSERVATION", "statement": "radiation recovery preceded warming",
                "validationNeeded": "more days",
            }]}),),
        )
        state = self.engine.recent_outcome_learning_state("Wuhan")
        self.assertFalse(state["binding"])
        self.assertEqual(state["lessons"][0]["level"], "OBSERVATION")


if __name__ == "__main__":
    unittest.main()
