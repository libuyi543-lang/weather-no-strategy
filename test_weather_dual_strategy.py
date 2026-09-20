import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from weather_dual_strategy import DualStrategyEngine
from weather_ai_agent import validate_json_schema
from weather_context_markdown import (
    _compress_market_timeline,
    build_daily_context_markdown,
    timeline_context_markdown,
)


UTC = timezone.utc


def config() -> dict:
    return {
        "strategyName": "dual_test",
        "paperOnly": True,
        "initialCashUsdc": 20,
        "minCashReserveUsdc": 5,
        "maxOpenNotionalPerCity": 20,
        "maxOpenNotionalTotal": 20,
        "threeBucketEnabled": True,
        "threeBucketReviewStartLocalMinutes": 630,
        "threeBucketEntryEndLocalMinutes": 675,
        "threeBucketHardStopLocalMinutes": 720,
        "threeBucketMinimumCenterLead": 0.03,
        "threeBucketMaxCombinedCostUsdc": 15,
        "threeBucketShares": {
            "COLD": [10, 15, 5],
            "NEUTRAL": [5, 20, 5],
            "HOT": [5, 15, 10],
        },
        "singleNoEnabled": True,
        "singleNoBaseShares": 5,
        "singleNoStrongShares": 10,
        "singleNoBaseEdge": 0.10,
        "singleNoStrongEdge": 0.25,
        "singleNoMaxBuyPriceExclusive": 0.92,
        "singleNoMaxMetarGapMinutes": 75,
        "feeRate": 0.05,
    }


def book(price: float, size: float = 50) -> dict:
    return {"asks": [{"price": price, "size": size}], "bids": [{"price": max(0.01, price - 0.02), "size": size}]}


def context() -> dict:
    markets = []
    for bucket, market_id in ((34, "m34"), (35, "m35"), (36, "m36")):
        markets.append({
            "marketId": market_id,
            "outcomeRange": f"{bucket} C",
            "bucketLow": bucket,
            "bucketHigh": bucket,
            "yesBook": book(0.10),
            "noBook": book(0.40),
            "yesExecutableBuyPrice5": 0.10,
            "yesBuyAvailableShares": 50,
            "noExecutableBuyPrice5": 0.40,
            "noBuyAvailableShares": 50,
            "snapshotUtc": "2026-08-12T03:00:00+00:00",
            "yesBestAsk": 0.10,
            "noBestAsk": 0.40,
        })
    return {
        "event": {
            "event_id": "event-1", "city": "Shanghai", "target_date": "2026-08-12",
            "timezone": "Asia/Shanghai", "station_id": "ZSPD",
        },
        "trigger": {"type": "scheduled_review", "slotUtc": "2026-08-12T03:00:00+00:00"},
        "markets": markets,
        "marketConsensus": {"distribution": []},
        "ridgeV2": {
            "bucketProbabilities": [
                {"bucketC": 34, "probability": 0.20},
                {"bucketC": 35, "probability": 0.55},
                {"bucketC": 36, "probability": 0.25},
            ],
            "cappingBucketC": 36,
            "pathStatus": {"capping": "supported_competitor"},
        },
        "metar": {"current": {"observation_time_utc": "2026-08-12T03:00:00+00:00", "temperature_c": 32}},
        "modelUpdates": {},
        "weatherProcess": {},
    }


def no_review(
    thesis: str, *, path=(0.70, 0.85), information=None,
    inefficiency: str = "WRONG_PRICING",
) -> dict:
    return {
        "marketId": "m34",
        "hypothesisId": "H1",
        "decision": "BUY",
        "thesis": thesis,
        "sizingTier": "BASE",
        "conservativePathProbabilityLow": path[0],
        "conservativePathProbabilityHigh": path[1],
        "pathAssessment": "the selected one-sided path excludes the target bucket",
        "ridgeAssessment": "Ridge is considered as a research prior, not a veto",
        "marketInefficiencyType": inefficiency,
        "marketInefficiencyAssessment": "the quote has not absorbed the new weather evidence",
        "unpricedInformation": ["fresh station change not reflected in the quote"],
        "reason": "reason",
        "supportingEvidence": ["fresh weather evidence"],
        "contradictingEvidence": ["contradicting evidence"],
        "newInformationTypes": information or ["metar_change"],
        "whyMarketMayBeRight": "market countercase",
        "whyMarketMayBeWrong": "explicit mispricing evidence",
        "invalidationCondition": "new observation invalidates the thesis",
    }


def problem_solution(
    thesis: str = "NO_CEILING", *, market_id: str | None = "m34",
) -> dict:
    selection = market_id is not None
    return {
        "hypotheses": [{
            "hypothesisId": "H1", "thesis": thesis,
            "targetMarketIds": [market_id or "m34"],
            "claim": "a falsifiable one-sided weather path",
            "supportingEvidence": ["fresh evidence"],
            "contradictingEvidence": ["strongest countercase"],
            "invalidationCondition": "new evidence invalidates the path",
        }],
        "hypothesisTests": [{
            "hypothesisId": "H1", "verdict": "SUPPORTED" if selection else "UNRESOLVED",
            "assessment": "tested against the evidence timeline",
            "strongestSupport": "fresh evidence",
            "strongestContradiction": "strongest countercase",
        }],
        "mispricingChecks": ([{
            "hypothesisId": "H1", "marketId": market_id,
            "status": "UNPRICED", "assessment": "price has not absorbed evidence",
            "unpricedInformation": ["fresh evidence"],
            "priceAlreadyReflectsEvidence": False,
        }] if selection else []),
        "selection": {
            "decision": "BUY" if selection else "WAIT",
            "marketId": market_id, "hypothesisId": "H1" if selection else None,
            "reason": "unique best target" if selection else "no complete evidence chain",
            "adjacentBucketComparison": "adjacent buckets are inferior" if selection else "none qualifies",
        },
    }


class DualStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = object.__new__(DualStrategyEngine)
        self.engine.config = config()
        self.engine.db = sqlite3.connect(":memory:")
        self.engine.db.row_factory = sqlite3.Row
        self.engine.db.execute("PRAGMA foreign_keys=ON")
        self.engine._init_runtime_schema()
        self.engine._init_dual_schema()
        self.engine.db.execute(
            "CREATE TABLE market_resolutions(market_id TEXT PRIMARY KEY,is_resolved INTEGER,winning_outcome TEXT)"
        )
        self.engine.db.execute(
            """INSERT INTO weather_dual_reviews(
                strategy_name,event_id,city,target_date,trigger_type,trigger_time_utc,state_hash,
                reviewed_at_utc,status,input_json
            ) VALUES('dual_test','event-1','Shanghai','2026-08-12','test','2026-08-12T03:00:00+00:00',
                     'fixture-review','2026-08-12T03:00:00+00:00','completed','{}')"""
        )

    def tearDown(self) -> None:
        self.engine.db.close()

    def test_three_bucket_share_mapping_is_fixed_and_center_heaviest(self):
        self.assertEqual(self.engine.three_bucket_weights("COLD", self.engine.config), (10, 15, 5))
        self.assertEqual(self.engine.three_bucket_weights("NEUTRAL", self.engine.config), (5, 20, 5))
        self.assertEqual(self.engine.three_bucket_weights("HOT", self.engine.config), (5, 15, 10))

    def test_lightweight_init_does_not_run_legacy_agent_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            values = config() | {"databasePath": str(Path(directory) / "test.sqlite3")}
            with patch("weather_ai_agent.WeatherAIAgent.__init__", side_effect=AssertionError("legacy init")):
                engine = DualStrategyEngine(values)
            try:
                tables = {row[0] for row in engine.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                self.assertIn("weather_dual_reviews", tables)
                self.assertNotIn("weather_ai_agent_runs", tables)
            finally:
                engine.close()

    def test_metar_scheduler_does_not_require_legacy_cycle_table(self):
        self.engine.config.update({
            "allowedCities": ["Shanghai"], "metarTriggersEnabled": True,
            "scheduledReviewsEnabled": False, "activeLocalStartMinutes": 420,
            "activeLocalEndHour": 20,
        })
        self.engine.db.executescript(
            """
            CREATE TABLE stations(
                station_id TEXT PRIMARY KEY,latitude REAL,longitude REAL,timezone TEXT
            );
            CREATE TABLE events(
                event_id TEXT PRIMARY KEY,city TEXT,target_date TEXT,station_id TEXT,
                station_name TEXT,resolution_source TEXT,rules TEXT,end_date_utc TEXT,
                resolved_at_utc TEXT,last_seen_utc TEXT
            );
            CREATE TABLE weather_observations(
                station_id TEXT,source TEXT,status TEXT,slot_utc TEXT,
                observation_time_utc TEXT,fetched_at_utc TEXT,temperature_c REAL,
                dewpoint_c REAL,relative_humidity REAL,wind_direction_deg REAL,
                wind_speed REAL,wind_speed_unit TEXT,weather_code TEXT,
                observed_daily_max_c REAL
            );
            INSERT INTO stations VALUES('ZSPD',31.1,121.8,'Asia/Shanghai');
            INSERT INTO events VALUES(
                'event-live','Shanghai','2026-08-12','ZSPD','Pudong','station','{}',
                NULL,NULL,'2026-08-12T00:00:00+00:00'
            );
            INSERT INTO weather_observations VALUES(
                'ZSPD','metar','ok','2026-08-12T03:00:00+00:00',
                '2026-08-12T03:00:00+00:00','2026-08-12T03:01:00+00:00',32,
                25,60,180,5,'kt','',32
            );
            """
        )
        events = self.engine.due_events(datetime(2026, 8, 12, 3, 5, tzinfo=UTC))
        self.assertEqual([row["event_id"] for row in events], ["event-live"])

    def test_scheduled_context_uses_shared_analysis_timestamp(self):
        self.engine.ridge_v2 = MagicMock()
        self.engine.ridge_v2.snapshot.return_value = {}
        state = context()["event"] | {
            "metar_trigger": {
                "slot_utc": "2026-08-12T03:00:00+00:00",
                "observation_time_utc": "2026-08-12T03:00:00+00:00",
            },
            "decision_trigger": {
                "type": "scheduled_review",
                "analysisAsOfUtc": "2026-08-12T03:05:00+00:00",
                "sourceSlotUtc": "2026-08-12T03:00:00+00:00",
            },
        }
        with patch.object(self.engine, "_latest_metar_for_event", return_value=state["metar_trigger"]), \
                patch.object(self.engine, "market_states", return_value=[]), \
                patch.object(self.engine, "_fast_metar_timeline", return_value=[]), \
                patch.object(self.engine, "previous_metar", return_value={}), \
                patch.object(self.engine, "local_weather_payload", return_value={"meteoblue": {}}), \
                patch.object(self.engine, "model_update_state", return_value={}), \
                patch.object(self.engine, "weather_process_state", return_value={}):
            built = self.engine.build_context(state)
        self.assertEqual(built["trigger"]["analysisAsOfUtc"], "2026-08-12T03:05:00+00:00")
        self.assertEqual(
            self.engine.ridge_v2.snapshot.call_args.args[1],
            datetime(2026, 8, 12, 3, 5, tzinfo=UTC),
        )

    def test_review_input_includes_all_tradable_exact_buckets_without_mutating_context(self):
        state = context()
        state["markets"].append({
            "marketId": "unrelated", "outcomeRange": "99 C", "bucketLow": 99,
            "bucketHigh": 99, "noExecutableBuyPrice5": 0.99,
        })
        state["recentLessons"] = [{"large": "legacy"}]
        review = self.engine.build_review_input(
            state, datetime(2026, 8, 12, 3, 0, tzinfo=UTC)
        )
        market_ids = {row["marketId"] for row in review["weatherState"]["markets"]}
        self.assertIn("unrelated", market_ids)
        self.assertIn("unrelated", {
            row["marketId"] for row in review["singleNoUniverse"]
        })
        self.assertNotIn("recentLessons", review["weatherState"])
        self.assertIn("unrelated", {row["marketId"] for row in state["markets"]})

    def test_daily_markdown_context_excludes_future_weather_and_market_rows(self):
        self.engine.db.executescript(
            """
            CREATE TABLE weather_observations(
                station_id TEXT,source TEXT,status TEXT,observation_time_utc TEXT,
                temperature_c REAL,observed_daily_max_c REAL,dewpoint_c REAL,
                relative_humidity REAL,wind_direction_deg REAL,wind_speed REAL,
                wind_speed_unit TEXT,wind_gust REAL,visibility_m REAL,
                precipitation_mm REAL,cloud_cover_pct REAL,solar_radiation_wm2 REAL,
                pressure_hpa REAL,weather_code TEXT,metar_type TEXT
            );
            CREATE TABLE market_snapshots(
                event_id TEXT,slot_utc TEXT,outcome_range TEXT,yes_best_bid REAL,
                yes_best_ask REAL,no_best_bid REAL,no_best_ask REAL,yes_ask_size REAL,
                no_ask_size REAL,market_liquidity REAL
            );
            INSERT INTO weather_observations VALUES
                ('ZSPD','metar','ok','2026-08-12T03:00:00+00:00',32,32,25,60,180,5,'kt',null,10000,0,10,500,1005,'clear','METAR'),
                ('ZSPD','metar','ok','2026-08-12T04:00:00+00:00',34,34,25,55,180,6,'kt',null,10000,0,5,600,1004,'clear','METAR');
            INSERT INTO market_snapshots VALUES
                ('event-1','2026-08-12T03:00:00+00:00','34 C',0.1,0.2,0.8,0.9,10,10,100),
                ('event-1','2026-08-12T04:00:00+00:00','34 C',0.05,0.1,0.9,0.95,10,10,100);
            """
        )
        state = context()
        markdown = build_daily_context_markdown(
            self.engine.db, state["event"], state,
            datetime(2026, 8, 12, 3, 5, tzinfo=UTC),
        )
        self.assertIn("2026-08-12T03:00:00+00:00", markdown)
        self.assertNotIn("2026-08-12T04:00:00+00:00", markdown)
        self.assertIn("METAR / Station Timeline", markdown)
        self.assertIn("Market Price Timeline", markdown)

    def test_daily_markdown_is_embedded_in_hermes_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            self.engine.config.update({
                "contextDocumentEnabled": True,
                "contextDocumentDir": directory,
            })
            state = context()
            review = self.engine.build_review_input(
                state, datetime(2026, 8, 12, 3, 5, tzinfo=UTC),
            )
            self.assertTrue(review["contextDocumentPath"].endswith("Shanghai.md"))
            self.assertIn("# Weather-Market Context", review["contextMarkdown"])
            self.assertNotIn("Current Model and Tradable Universe", review["contextMarkdown"])
            self.assertIsNotNone(review["contextDocumentHash"])
            self.assertEqual(
                review["problemDefinition"]["problemType"],
                "WEATHER_MARKET_INFORMATION_GAP",
            )
            self.assertEqual(
                review["problemDefinition"]["decisionScope"]["maximumSelectedTargets"], 1,
            )
            prompt = self.engine._prompt(review)
            self.assertIn("DAILY CITY CONTEXT MARKDOWN BEGIN", prompt)
            self.assertIn("METAR / Station Timeline", prompt)
            self.assertIn("天气市场问题求解器", prompt)
            self.assertIn("problemDefinition是本轮不可改写的问题契约", prompt)
            self.assertIn("Python没有按Ridge", prompt)
            document = Path(review["contextDocumentPath"])
            self.assertTrue(document.exists())
            self.assertIn("Current Model and Tradable Universe", document.read_text(encoding="utf-8"))

    def test_timeline_context_removes_only_structured_state_section(self):
        full = "# Context\n\n## 1. Timeline\nrow\n\n## 5. Current Model and Tradable Universe\njson\n"
        self.assertEqual(
            timeline_context_markdown(full), "# Context\n\n## 1. Timeline\nrow\n",
        )

    def test_market_timeline_compression_keeps_changes_and_latest(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE prices(outcome_range TEXT,yes_best_bid REAL,yes_best_ask REAL,no_best_bid REAL,no_best_ask REAL,slot_utc TEXT)")
        db.executemany("INSERT INTO prices VALUES(?,?,?,?,?,?)", [
            ("35 C", 0.10, 0.20, 0.80, 0.90, "t1"),
            ("35 C", 0.10, 0.20, 0.80, 0.90, "t2"),
            ("35 C", 0.15, 0.25, 0.75, 0.85, "t3"),
            ("35 C", 0.15, 0.25, 0.75, 0.85, "t4"),
        ])
        rows = db.execute("SELECT * FROM prices ORDER BY slot_utc").fetchall()
        compressed = _compress_market_timeline(rows)
        self.assertEqual([row["slot_utc"] for row in compressed], ["t1", "t3", "t4"])
        db.close()

    def test_ai_review_uses_event_scoped_circuit(self):
        review_input = {
            "eventId": "event-1", "asOfUtc": "2026-08-12T03:00:00+00:00",
            "stateHash": "expired-state", "threeBucketCandidate": None,
            "singleNoUniverse": [{"marketId": "m34"}],
        }
        with patch.object(self.engine, "_prompt", return_value="review"), \
                patch.object(self.engine, "_run_ai", return_value={}) as run:
            self.engine.call_review_ai(review_input)
        self.assertEqual(
            run.call_args.kwargs["circuit_scope"], "dual_test:event-1"
        )

    def test_due_events_are_deduplicated_and_pending_checkpoint_is_prioritized(self):
        self.engine.config["maxReviewEventsPerRun"] = 2
        metar_event = {
            "event_id": "event-1", "city": "Shanghai",
            "metar_trigger": {"slot_utc": "2026-08-12T03:00:00+00:00"},
        }
        scheduled_event = {
            **metar_event,
            "decision_trigger": {
                "type": "scheduled_review",
                "analysisAsOfUtc": "2026-08-12T03:05:00+00:00",
            },
        }
        other_metar = {
            "event_id": "event-2", "city": "Beijing",
            "metar_trigger": {"slot_utc": "2026-08-12T03:04:00+00:00"},
        }
        selected = self.engine._select_due_events([
            metar_event, other_metar, scheduled_event,
        ])
        self.assertEqual([row["event_id"] for row in selected], ["event-1", "event-2"])
        self.assertEqual(selected[0]["decision_trigger"]["type"], "scheduled_review")

    def test_pending_checkpoint_wins_over_newer_metar_for_same_event(self):
        scheduled = {
            "event_id": "event-1", "city": "Shanghai",
            "metar_trigger": {"slot_utc": "2026-08-12T03:00:00+00:00"},
            "decision_trigger": {
                "type": "scheduled_review",
                "analysisAsOfUtc": "2026-08-12T03:01:00+00:00",
            },
        }
        newer_metar = {
            "event_id": "event-1", "city": "Shanghai",
            "metar_trigger": {"slot_utc": "2026-08-12T03:05:00+00:00"},
        }
        selected = self.engine._select_due_events([scheduled, newer_metar])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["decision_trigger"]["type"], "scheduled_review")

    def test_due_event_limit_is_enforced(self):
        self.engine.config["maxReviewEventsPerRun"] = 1
        events = [
            {
                "event_id": f"event-{index}", "city": f"city-{index}",
                "metar_trigger": {"slot_utc": f"2026-08-12T03:0{index}:00+00:00"},
            }
            for index in range(2)
        ]
        self.assertEqual(len(self.engine._select_due_events(events)), 1)

    def test_entry_refreshes_market_books_before_execution(self):
        state = context()
        latest = [{**row, "noBook": book(0.55)} for row in state["markets"]]
        response = {
            "threeBucketReview": None,
            "singleNoReviews": [{"marketId": "m34", "decision": "BUY"}],
        }
        with patch.object(self.engine, "market_states", return_value=latest):
            reason = self.engine._refresh_execution_markets(
                state, response, datetime(2026, 8, 12, 3, 5, tzinfo=UTC)
            )
        self.assertIsNone(reason)
        self.assertEqual(state["markets"][0]["noBook"], book(0.55))

    def test_invalid_market_timestamp_is_deferred_instead_of_crashing(self):
        state = context()
        latest = [{**row, "snapshotUtc": "not-a-timestamp"} for row in state["markets"]]
        response = {
            "threeBucketReview": None,
            "singleNoReviews": [{"marketId": "m34", "decision": "BUY"}],
        }
        with patch.object(self.engine, "market_states", return_value=latest):
            reason = self.engine._refresh_execution_markets(
                state, response, datetime(2026, 8, 12, 3, 5, tzinfo=UTC)
            )
        self.assertEqual(reason, "latest executable market timestamp is unavailable")

    def test_stale_market_context_is_deferred_before_ai_call(self):
        self.engine.config["enabled"] = True
        self.engine.ridge_v2 = MagicMock()
        state = context()
        review_input = {
            "eventId": "event-1", "asOfUtc": "2026-08-12T03:00:00+00:00",
            "stateHash": "expired-state", "threeBucketCandidate": None,
            "singleNoUniverse": [{"marketId": "m34"}],
        }
        with patch.object(self.engine, "compact_audit_history", return_value=0), \
                patch.object(self.engine, "settle_positions", return_value=0), \
                patch.object(self.engine, "due_review_events", return_value=[state["event"]]), \
                patch.object(self.engine, "build_context", return_value=state), \
                patch.object(self.engine, "build_review_input", return_value=review_input), \
                patch.object(self.engine, "call_review_ai") as call_review, \
                patch.object(
                    self.engine, "_decision_context_expiry_reason",
                    return_value="decision context expired",
                ), \
                patch.object(self.engine, "_refresh_execution_markets") as refresh, \
                patch.object(self.engine, "persist_review") as persist, \
                patch.object(self.engine, "_defer_review") as defer:
            result = self.engine.run_once()
        call_review.assert_not_called()
        persist.assert_not_called()
        refresh.assert_not_called()
        defer.assert_called_once()
        self.assertEqual(result["reviews"], [])
        self.assertEqual(result["errors"][0]["error"], "decision context expired")

    def test_empty_ai_result_is_persisted_without_post_call_freshness_retry(self):
        self.engine.config["enabled"] = True
        self.engine.ridge_v2 = MagicMock()
        state = context()
        review_input = {
            "eventId": "event-1", "asOfUtc": "2026-08-12T03:00:00+00:00",
            "stateHash": "empty-state", "threeBucketCandidate": None,
            "singleNoUniverse": [{"marketId": "m34"}],
        }
        response = {"threeBucketReview": None, "singleNoReviews": []}
        with patch.object(self.engine, "compact_audit_history", return_value=0), \
                patch.object(self.engine, "settle_positions", return_value=0), \
                patch.object(self.engine, "due_review_events", return_value=[state["event"]]), \
                patch.object(self.engine, "build_context", return_value=state), \
                patch.object(self.engine, "build_review_input", return_value=review_input), \
                patch.object(self.engine, "_decision_context_expiry_reason", return_value=None), \
                patch.object(self.engine, "call_review_ai", return_value=response), \
                patch.object(self.engine, "_weather_context_change_reason") as weather_change, \
                patch.object(self.engine, "_refresh_execution_markets") as refresh, \
                patch.object(self.engine, "persist_review", return_value={"review_id": 2}) as persist:
            result = self.engine.run_once()
        weather_change.assert_not_called()
        refresh.assert_not_called()
        persist.assert_called_once()
        self.assertEqual(result["reviews"], [{"review_id": 2}])

    def test_new_weather_during_candidate_review_defers_execution(self):
        self.engine.config["enabled"] = True
        self.engine.ridge_v2 = MagicMock()
        state = context()
        review_input = {
            "eventId": "event-1", "asOfUtc": "2026-08-12T03:00:00+00:00",
            "stateHash": "candidate-state", "threeBucketCandidate": None,
            "singleNoUniverse": [{"marketId": "m34"}],
        }
        response = {
            "threeBucketReview": None,
            "singleNoReviews": [{"marketId": "m34", "decision": "BUY"}],
        }
        with patch.object(self.engine, "compact_audit_history", return_value=0), \
                patch.object(self.engine, "settle_positions", return_value=0), \
                patch.object(self.engine, "due_review_events", return_value=[state["event"]]), \
                patch.object(self.engine, "build_context", return_value=state), \
                patch.object(self.engine, "build_review_input", return_value=review_input), \
                patch.object(self.engine, "_decision_context_expiry_reason", return_value=None), \
                patch.object(self.engine, "call_review_ai", return_value=response), \
                patch.object(self.engine, "_trade_window_open", return_value=True), \
                patch.object(
                    self.engine, "_weather_context_change_reason",
                    return_value="new METAR arrived during AI review",
                ), \
                patch.object(self.engine, "_refresh_execution_markets") as refresh, \
                patch.object(self.engine, "persist_review") as persist, \
                patch.object(self.engine, "_defer_review") as defer:
            result = self.engine.run_once()
        refresh.assert_not_called()
        persist.assert_not_called()
        defer.assert_called_once()
        self.assertIn("new METAR", result["errors"][0]["error"])

    def test_run_once_executes_cross_city_candidate_in_quality_order(self):
        self.engine.config["enabled"] = True
        self.engine.ridge_v2 = MagicMock()
        first = context()
        second = context()
        first["event"] = first["event"] | {"event_id": "event-low", "city": "Guangzhou"}
        second["event"] = second["event"] | {"event_id": "event-high", "city": "Chengdu"}
        events = [first["event"], second["event"]]
        low = no_review("NO_CEILING", path=(0.60, 0.70), information=["metar_change"])
        high = no_review("NO_CEILING", path=(0.88, 0.95), information=["metar_change", "model_update", "orderbook_change"])
        responses = {"event-low": {"threeBucketReview": None, "singleNoReviews": [low]},
                     "event-high": {"threeBucketReview": None, "singleNoReviews": [high]}}
        persisted: list[str] = []

        def build(event):
            return first if event["event_id"] == "event-low" else second

        def review_input(context_value, _now):
            return {
                "eventId": context_value["event"]["event_id"],
                "asOfUtc": "2026-08-12T03:00:00+00:00",
                "stateHash": context_value["event"]["event_id"],
                "threeBucketCandidate": None,
                "singleNoUniverse": [{"marketId": "m34"}],
            }

        def persist(context_value, _review_input, _response):
            persisted.append(context_value["event"]["event_id"])
            return {"review_id": len(persisted)}

        with patch.object(self.engine, "compact_audit_history", return_value=0), \
                patch.object(self.engine, "settle_positions", return_value=0), \
                patch.object(self.engine, "due_review_events", return_value=events), \
                patch.object(self.engine, "build_context", side_effect=build), \
                patch.object(self.engine, "build_review_input", side_effect=review_input), \
                patch.object(self.engine, "call_review_ai", side_effect=lambda value: responses[value["eventId"]]), \
                patch.object(self.engine, "_decision_context_expiry_reason", return_value=None), \
                patch.object(self.engine, "_weather_context_change_reason", return_value=None), \
                patch.object(self.engine, "_refresh_execution_markets", return_value=None), \
                patch.object(self.engine, "_trade_window_open", return_value=True), \
                patch.object(self.engine, "persist_review", side_effect=persist):
            result = self.engine.run_once()
        self.assertEqual(result["errors"], [])
        self.assertEqual(persisted, ["event-high", "event-low"])

    def test_audit_compaction_preserves_actions_and_review_row(self):
        self.engine.config["skippedReviewPayloadRetentionDays"] = 14
        self.engine.db.execute(
            "UPDATE weather_dual_reviews SET status='skipped',reviewed_at_utc='2026-01-01T00:00:00+00:00',input_json=? WHERE review_id=1",
            ('{"large":"payload"}',),
        )
        self.engine.db.execute(
            "INSERT INTO weather_dual_actions(review_id,strategy_name,event_id,city,strategy_type,executed_action,state_hash,created_at_utc) VALUES(1,'dual_test','event-1','Shanghai','SINGLE_NO','REJECTED','s','2026-01-01T00:00:00+00:00')"
        )
        self.assertEqual(self.engine.compact_audit_history(datetime(2026, 8, 12, tzinfo=UTC)), 1)
        self.assertEqual(self.engine.db.execute(
            "SELECT input_json FROM weather_dual_reviews WHERE review_id=1"
        ).fetchone()[0], "{}")
        self.assertEqual(self.engine.db.execute("SELECT COUNT(*) FROM weather_dual_actions").fetchone()[0], 1)

    def test_invalid_three_bucket_weights_are_rejected(self):
        self.engine.config["threeBucketShares"]["HOT"] = [10, 10, 10]
        with self.assertRaisesRegex(ValueError, "center"):
            self.engine.three_bucket_weights("HOT", self.engine.config)

    def test_three_bucket_window_reviews_at_1030_but_not_noon(self):
        candidate = self.engine._three_bucket_candidate(
            context(), datetime(2026, 8, 12, 2, 30, tzinfo=UTC)
        )
        self.assertIsNotNone(candidate)
        self.assertTrue(candidate["entryAllowed"])
        self.assertIsNone(self.engine._three_bucket_candidate(
            context(), datetime(2026, 8, 12, 4, 0, tzinfo=UTC)
        ))

    def test_hot_three_bucket_executes_5_15_10(self):
        state = context()
        candidate = self.engine._three_bucket_candidate(
            state, datetime(2026, 8, 12, 3, 0, tzinfo=UTC)
        )
        fills = self.engine._execute_three_bucket(
            1, state, candidate, {"decision": "ENTER", "skew": "HOT"}, "state-a"
        )
        self.assertEqual(fills, 3)
        rows = self.engine.db.execute(
            "SELECT market_id,shares FROM weather_dual_positions ORDER BY outcome_range"
        ).fetchall()
        self.assertEqual([(row["market_id"], row["shares"]) for row in rows], [
            ("m34", 5), ("m35", 15), ("m36", 10),
        ])

    def test_single_no_universe_exposes_ridge_as_non_binding_context(self):
        state = context()
        universe = self.engine._single_no_universe(state)
        self.assertEqual({row["marketId"] for row in universe}, {"m34", "m35", "m36"})
        roles = {row["marketId"]: row["ridgeRoles"] for row in universe}
        self.assertIn("RIDGE_CENTER", roles["m35"])
        self.assertIn("RIDGE_CAPPING", roles["m36"])
        self.assertNotIn("requiresCappingPathRejection", universe[0])
        self.assertAlmostEqual(
            next(row for row in universe if row["marketId"] == "m34")["noAllInCostPerShare5"],
            0.412,
        )

    def test_single_no_universe_respects_trade_window(self):
        state = context()
        self.engine.config.update({
            "tradeLocalStartMinutes": 7 * 60,
            "tradeLocalEndMinutes": 19 * 60,
        })
        self.assertEqual(
            self.engine._single_no_universe(
                state, datetime(2026, 8, 11, 22, 59, tzinfo=UTC),
            ),
            [],
        )
        self.assertTrue(self.engine._single_no_universe(
            state, datetime(2026, 8, 11, 23, 0, tzinfo=UTC),
        ))
        self.assertEqual(
            self.engine._single_no_universe(
                state, datetime(2026, 8, 12, 11, 0, tzinfo=UTC),
            ),
            [],
        )

    def test_decision_phase_prioritizes_overshoot_before_10(self):
        state = context()
        phase = self.engine._decision_phase(
            state, datetime(2026, 8, 12, 1, 0, tzinfo=UTC),
        )
        self.assertEqual(phase["name"], "EARLY_WARMING")
        self.assertFalse(phase["ceilingAllowed"])
        late = self.engine._decision_phase(
            state, datetime(2026, 8, 12, 4, 0, tzinfo=UTC),
        )
        self.assertTrue(late["ceilingAllowed"])

    def test_early_no_ceiling_is_rejected_but_overshoot_can_execute(self):
        state = context()
        state["executionCheckedAtUtc"] = "2026-08-12T01:00:00+00:00"
        universe = self.engine._single_no_universe(state)
        ceiling = [{
            **no_review("NO_CEILING"), "conservativePathProbabilityLow": 0.70,
        }]
        self.assertEqual(self.engine._execute_single_no(1, state, universe, ceiling, "early-ceiling"), 0)
        rejection = self.engine.db.execute(
            "SELECT rejection_reason FROM weather_dual_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertIn("early phase", rejection)

        overshoot = [{
            **no_review("NO_OVERSHOOT"), "conservativePathProbabilityLow": 0.70,
        }]
        self.assertEqual(self.engine._execute_single_no(1, state, universe, overshoot, "early-overshoot"), 1)

    def test_no_ceiling_is_rejected_after_target_was_observed(self):
        state = context()
        state["metar"]["current"]["observed_daily_max_c"] = 34
        universe = self.engine._single_no_universe(
            state, datetime(2026, 8, 12, 4, 0, tzinfo=UTC),
        )
        ceiling = [{**no_review("NO_CEILING"), "conservativePathProbabilityLow": 0.90}]
        self.assertEqual(self.engine._execute_single_no(1, state, universe, ceiling, "reached"), 0)
        rejection = self.engine.db.execute(
            "SELECT rejection_reason FROM weather_dual_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertIn("already reached", rejection)

    def test_related_single_no_targets_are_consolidated_to_one(self):
        state = context()
        universe = self.engine._single_no_universe(state)
        first = {**no_review("NO_CEILING"), "marketId": "m34", "conservativePathProbabilityLow": 0.70}
        second = {**no_review("NO_CEILING"), "marketId": "m35", "conservativePathProbabilityLow": 0.69}
        self.assertEqual(self.engine._execute_single_no(1, state, universe, [first, second], "one-target"), 1)
        rejection = self.engine.db.execute(
            "SELECT rejection_reason FROM weather_dual_actions WHERE executed_action='REJECTED'"
        ).fetchone()[0]
        self.assertIn("one target bucket", rejection)

    def test_ridge_center_and_paths_are_not_execution_vetoes(self):
        state = context()
        review = [{
            "marketId": "m35", "decision": "BUY", "thesis": "NO_OVERSHOOT",
            "sizingTier": "BASE", "conservativePathProbabilityLow": 0.90,
            "supportingEvidence": ["fresh heating"],
            "newInformationTypes": ["metar_change"],
        }]
        self.assertEqual(
            self.engine._execute_single_no(
                1, state, self.engine._single_no_universe(state), review, "center",
            ),
            1,
        )

    def test_single_no_universe_does_not_require_ridge_or_prefilter_high_price(self):
        state = context()
        state["ridgeV2"] = {}
        market = next(row for row in state["markets"] if row["marketId"] == "m34")
        market["noExecutableBuyPrice5"] = 0.99
        universe = self.engine._single_no_universe(state)
        self.assertEqual({row["marketId"] for row in universe}, {"m34", "m35", "m36"})
        self.assertTrue(all(row["ridgeBucketProbability"] is None for row in universe))

    def test_ai_generates_a_subset_or_an_empty_candidate_list(self):
        review_input = {
            "eventId": "event-1", "threeBucketCandidate": None,
            "singleNoUniverse": [
                {"marketId": "m34"},
                {"marketId": "m35"},
            ],
        }
        selected = {
            "eventId": "event-1", "threeBucketReview": None,
            "problemSolution": problem_solution("NO_CEILING"),
            "singleNoReviews": [no_review("NO_CEILING")],
        }
        self.engine.validate_ai_response(review_input, selected)
        self.engine.validate_ai_response(review_input, {
            "eventId": "event-1", "threeBucketReview": None,
            "problemSolution": problem_solution(market_id=None), "singleNoReviews": [],
        })

    def test_dual_schema_accepts_only_generated_buy_candidates(self):
        schema = json.loads(Path("weather_dual_strategy.schema.json").read_text(encoding="utf-8"))
        empty = {
            "generatedAt": "2026-08-12T03:00:00+00:00", "eventId": "event-1",
            "problemSolution": problem_solution(market_id=None),
            "threeBucketReview": None, "singleNoReviews": [],
        }
        validate_json_schema(empty, schema)
        selected = {
            **empty, "problemSolution": problem_solution("NO_CEILING"),
            "singleNoReviews": [no_review("NO_CEILING")],
        }
        validate_json_schema(selected, schema)
        selected["singleNoReviews"][0]["decision"] = "OBSERVE"
        with self.assertRaisesRegex(RuntimeError, "unsupported value"):
            validate_json_schema(selected, schema)

    def test_ai_cannot_duplicate_or_invent_a_single_no_market(self):
        review_input = {
            "eventId": "event-1", "threeBucketCandidate": None,
            "singleNoUniverse": [{"marketId": "m34"}],
        }
        duplicate = {
            "eventId": "event-1", "threeBucketReview": None,
            "problemSolution": problem_solution("NO_CEILING"),
            "singleNoReviews": [no_review("NO_CEILING"), no_review("NO_CEILING")],
        }
        with self.assertRaisesRegex(RuntimeError, "unique market IDs"):
            self.engine.validate_ai_response(review_input, duplicate)
        invented = no_review("NO_CEILING") | {"marketId": "m99"}
        with self.assertRaisesRegex(RuntimeError, "outside the supplied universe"):
            self.engine.validate_ai_response(review_input, {
                "eventId": "event-1", "threeBucketReview": None,
                "problemSolution": problem_solution("NO_CEILING"),
                "singleNoReviews": [invented],
            })

    def test_ai_accepts_only_the_two_one_sided_no_theses(self):
        review_input = {
            "eventId": "event-1", "threeBucketCandidate": None,
            "singleNoUniverse": [{"marketId": "m34"}],
        }
        ceiling_response = {
            "eventId": "event-1", "threeBucketReview": None,
            "problemSolution": problem_solution("NO_CEILING"),
            "singleNoReviews": [no_review("NO_CEILING")],
        }
        self.engine.validate_ai_response(review_input, ceiling_response)
        self.engine.validate_ai_response(review_input, {
            "eventId": "event-1", "threeBucketReview": None,
            "problemSolution": problem_solution("NO_OVERSHOOT"),
            "singleNoReviews": [no_review("NO_OVERSHOOT")],
        })
        invalid = no_review("NO_MARKET_TAIL_REJECTION")
        with self.assertRaisesRegex(RuntimeError, "valid thesis"):
            self.engine.validate_ai_response(review_input, {
                "eventId": "event-1", "threeBucketReview": None,
                "problemSolution": problem_solution("NO_MARKET_TAIL_REJECTION"),
                "singleNoReviews": [invalid],
            })

    def test_market_lag_requires_market_specific_evidence(self):
        review_input = {
            "eventId": "event-1", "threeBucketCandidate": None,
            "singleNoUniverse": [{"marketId": "m34"}],
        }
        lagged = no_review("NO_CEILING", inefficiency="MARKET_LAG")
        response = {
            "eventId": "event-1", "threeBucketReview": None,
            "problemSolution": problem_solution("NO_CEILING"),
            "singleNoReviews": [lagged],
        }
        with self.assertRaisesRegex(RuntimeError, "market-lag or order-book evidence"):
            self.engine.validate_ai_response(review_input, response)
        lagged["newInformationTypes"].append("market_lag")
        self.engine.validate_ai_response(review_input, response)

    def test_single_no_base_uses_conservative_probability_edge(self):
        state = context()
        candidates = [{"marketId": "m34"}]
        weak = [{
            "marketId": "m34", "decision": "BUY", "thesis": "NO_OVERSHOOT", "sizingTier": "BASE",
            "conservativePathProbabilityLow": 0.511, "supportingEvidence": ["cloud cap"],
            "newInformationTypes": ["radar_cloud_radiation_change"],
        }]
        self.assertEqual(self.engine._execute_single_no(1, state, candidates, weak, "state-a"), 0)
        rejection = self.engine.db.execute(
            "SELECT rejection_reason FROM weather_dual_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertIn("0.10 requirement", rejection)

        strong_enough = [{**weak[0], "conservativePathProbabilityLow": 0.513}]
        self.assertEqual(self.engine._execute_single_no(1, state, candidates, strong_enough, "state-b"), 1)
        fill = self.engine.db.execute(
            "SELECT notional_usdc,fee_usdc FROM weather_dual_fills WHERE fill_type='paper_buy'"
        ).fetchone()
        self.assertAlmostEqual(fill["notional_usdc"], 2.0)
        self.assertAlmostEqual(fill["fee_usdc"], 0.06)

    def test_high_evidence_base_uses_relaxed_edge_at_reasonable_price(self):
        state = context()
        market = next(row for row in state["markets"] if row["marketId"] == "m34")
        market["noBook"] = book(0.80)
        market["noExecutableBuyPrice5"] = 0.80
        universe = self.engine._single_no_universe(state)
        review = [{
            "marketId": "m34", "decision": "BUY", "thesis": "NO_CEILING", "sizingTier": "BASE",
            "conservativePathProbabilityLow": 0.83,
            "supportingEvidence": ["a", "b", "c", "d", "e"],
            "newInformationTypes": ["metar_change", "daily_high_change", "model_update", "wind_process_change", "market_lag"],
        }]
        self.assertEqual(self.engine._execute_single_no(1, state, universe, review, "relaxed"), 1)
        audit = self.engine.db.execute(
            "SELECT candidate_status,required_edge,raw_edge,rejection_reason FROM weather_dual_candidate_audits"
        ).fetchone()
        self.assertEqual(audit["candidate_status"], "EXECUTED")
        self.assertAlmostEqual(audit["required_edge"], 0.02)
        self.assertIsNone(audit["rejection_reason"])

    def test_candidate_audit_backfills_rejected_candidate_at_settlement(self):
        state = context()
        candidates = [{"marketId": "m34"}]
        review = [{
            "marketId": "m34", "decision": "BUY", "thesis": "NO_CEILING", "sizingTier": "BASE",
            "conservativePathProbabilityLow": 0.511,
            "supportingEvidence": ["cloud cap"],
            "newInformationTypes": ["radar_cloud_radiation_change"],
        }]
        self.assertEqual(self.engine._execute_single_no(1, state, candidates, review, "audit-reject"), 0)
        self.engine._backfill_candidate_audits(
            "event-1", "m34", "No", "2026-08-12T16:00:00+00:00",
        )
        audit = self.engine.db.execute(
            "SELECT candidate_status,final_outcome,signal_correct,hypothetical_pnl_usdc FROM weather_dual_candidate_audits"
        ).fetchone()
        self.assertEqual(audit["candidate_status"], "REJECTED")
        self.assertEqual(audit["final_outcome"], "No")
        self.assertEqual(audit["signal_correct"], 1)
        self.assertAlmostEqual(audit["hypothetical_pnl_usdc"], 2.94)

    def test_early_portfolio_reserve_blocks_only_before_release_time(self):
        state = context()
        state["executionCheckedAtUtc"] = "2026-08-12T01:00:00+00:00"  # 09:00 local
        self.engine.config.update({
            "singleNoEarlyPortfolioReserveUsdc": 8,
            "singleNoEarlyPortfolioReserveEndLocalMinutes": 600,
        })
        self.engine.db.execute(
            """INSERT INTO weather_dual_positions(
                strategy_name,event_id,city,market_id,outcome_range,outcome_side,strategy_type,
                shares,cost_basis_usdc,entry_count,last_state_hash,opened_at_utc,updated_at_utc
            ) VALUES('dual_test','other-event','Beijing','other-market','35 C','NO','SINGLE_NO',
                     20,11,1,'old','2026-08-12T00:00:00+00:00','2026-08-12T00:00:00+00:00')"""
        )
        universe = self.engine._single_no_universe(state)
        review = [{
            "marketId": "m34", "decision": "BUY", "thesis": "NO_OVERSHOOT", "sizingTier": "BASE",
            "conservativePathProbabilityLow": 0.90,
            "supportingEvidence": ["a"], "newInformationTypes": ["metar_change"],
        }]
        self.assertEqual(self.engine._execute_single_no(1, state, universe, review, "reserve"), 0)
        rejection = self.engine.db.execute(
            "SELECT rejection_reason FROM weather_dual_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertIn("early portfolio budget reserved", rejection)

    def test_single_no_response_finishing_after_cutoff_is_rejected(self):
        state = context()
        state["executionCheckedAtUtc"] = "2026-08-12T11:00:00+00:00"
        universe = self.engine._single_no_universe(state)
        review = [{
            "marketId": "m34", "decision": "BUY", "thesis": "NO_CEILING",
            "sizingTier": "BASE", "conservativePathProbabilityLow": 0.90,
            "supportingEvidence": ["cloud cap"],
            "newInformationTypes": ["radar_cloud_radiation_change"],
        }]
        self.assertEqual(
            self.engine._execute_single_no(1, state, universe, review, "late"), 0,
        )
        rejection = self.engine.db.execute(
            "SELECT rejection_reason FROM weather_dual_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertIn("trading window is closed", rejection)

    def test_ai_sees_high_price_market_but_execution_still_enforces_price_cap(self):
        state = context()
        market = next(row for row in state["markets"] if row["marketId"] == "m34")
        market["noBook"] = book(0.95)
        market["noExecutableBuyPrice5"] = 0.95
        universe = self.engine._single_no_universe(state)
        self.assertIn("m34", {row["marketId"] for row in universe})
        review = [{
            "marketId": "m34", "decision": "BUY", "thesis": "NO_CEILING",
            "sizingTier": "BASE", "conservativePathProbabilityLow": 0.99,
            "supportingEvidence": ["cloud cap"], "newInformationTypes": ["metar_change"],
        }]
        self.assertEqual(self.engine._execute_single_no(1, state, universe, review, "high-price"), 0)
        rejection = self.engine.db.execute(
            "SELECT rejection_reason FROM weather_dual_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertIn("configured cap", rejection)

    def test_single_no_upgrade_requires_new_state_and_strong_evidence(self):
        state = context()
        candidates = [{"marketId": "m34"}]
        base = [{
            "marketId": "m34", "decision": "BUY", "thesis": "NO_CEILING", "sizingTier": "BASE",
            "conservativePathProbabilityLow": 0.60, "supportingEvidence": ["cloud cap"],
            "newInformationTypes": ["radar_cloud_radiation_change"],
        }]
        self.assertEqual(self.engine._execute_single_no(1, state, candidates, base, "same-state"), 1)
        upgrade = [{
            **base[0], "sizingTier": "STRONG", "conservativePathProbabilityLow": 0.70,
            "supportingEvidence": ["a", "b", "c", "d"],
            "newInformationTypes": ["metar_change", "model_update", "wind_process_change"],
        }]
        self.assertEqual(self.engine._execute_single_no(1, state, candidates, upgrade, "same-state"), 0)
        rejection = self.engine.db.execute(
            "SELECT rejection_reason FROM weather_dual_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertIn("new observable state", rejection)

    def test_single_no_uses_one_sided_path_without_rounding_boundary_fields(self):
        state = context()
        universe = self.engine._single_no_universe(state)
        universe_market = next(row for row in universe if row["marketId"] == "m34")
        self.assertNotIn("boundaryAttentionRequired", universe_market)
        self.assertIn("不需要预测最终落入哪个其他桶", universe_market["strategyQuestion"])
        review = [{
            "marketId": "m34", "decision": "BUY", "thesis": "NO_CEILING", "sizingTier": "STRONG",
            "conservativePathProbabilityLow": 0.90, "supportingEvidence": ["a", "b", "c", "d"],
            "newInformationTypes": ["metar_change", "model_update", "wind_process_change"],
        }]
        self.assertEqual(self.engine._execute_single_no(1, state, universe, review, "boundary"), 1)

    def test_metar_gap_blocks_strong(self):
        state = context()
        state["metarCoverage"] = {"maxGapMinutes": 180, "missingDataRisk": True}
        universe = self.engine._single_no_universe(state)
        review = [{
            "marketId": "m34", "decision": "BUY", "thesis": "NO_CEILING", "sizingTier": "STRONG",
            "conservativePathProbabilityLow": 0.90, "supportingEvidence": ["a", "b", "c", "d"],
            "newInformationTypes": ["metar_change", "model_update", "wind_process_change"],
        }]
        self.assertEqual(self.engine._execute_single_no(1, state, universe, review, "gap"), 0)
        rejection = self.engine.db.execute(
            "SELECT rejection_reason FROM weather_dual_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()[0]
        self.assertIn("METAR-gap", rejection)

    def test_settlement_uses_a_real_review_foreign_key(self):
        state = context()
        candidate = self.engine._three_bucket_candidate(
            state, datetime(2026, 8, 12, 3, 0, tzinfo=UTC)
        )
        self.engine.db.execute(
            """INSERT INTO weather_dual_reviews(
                strategy_name,event_id,city,target_date,trigger_type,trigger_time_utc,state_hash,
                reviewed_at_utc,status,input_json
            ) VALUES('dual_test','event-1','Shanghai','2026-08-12','test','2026-08-12T03:00:00+00:00',
                     'state-a','2026-08-12T03:00:00+00:00','completed','{}')"""
        )
        self.engine._execute_three_bucket(
            1, state, candidate, {"decision": "ENTER", "skew": "HOT"}, "state-a"
        )
        self.engine.db.execute(
            "INSERT INTO market_resolutions VALUES('m34',1,'YES')"
        )
        self.assertEqual(self.engine.settle_positions(), 1)
        row = self.engine.db.execute(
            "SELECT a.review_id,f.realized_pnl_usdc FROM weather_dual_actions a "
            "JOIN weather_dual_fills f USING(action_id) WHERE f.fill_type='settlement'"
        ).fetchone()
        self.assertGreater(row["review_id"], 0)
        self.assertAlmostEqual(row["realized_pnl_usdc"], 4.4775)


if __name__ == "__main__":
    unittest.main()
