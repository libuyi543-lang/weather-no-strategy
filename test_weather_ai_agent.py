import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from weather_ai_agent import (
    WEATHER_TAKER_FEE_RATE,
    WeatherAIAgent,
    executable_vwap,
    validate_json_schema,
)


UTC = timezone.utc


def config() -> dict:
    return {
        "strategyName": "test_agent",
        "allowedCities": ["Shanghai"],
        "activeLocalStartHour": 0,
        "activeLocalEndHour": 24,
        "tradeLocalStartHour": 0,
        "tradeLocalEndHour": 24,
        "initialCashUsdc": 20,
        "shares": 5,
        "strongNoShares": 10,
        "feeRate": 0.0,
        "minSharesPerBuy": 5,
        "maxSharesPerAction": 10,
        "maxSharesPerMarket": 10,
        "noBaseEdgeMargin": 0.10,
        "noStrongEdgeMargin": 0.25,
        "strongNoMinEvidenceItems": 4,
        "strongNoMinInformationTypes": 3,
        "maxOpenNotionalPerCity": 20,
        "maxOpenNotionalTotal": 20,
        "maxMarketDataAgeMinutes": 40,
        "realtimeExecutionGuards": False,
        "refreshMarketBeforeExecution": False,
        "refreshWeatherBeforeExecution": False,
        "blockFollowAlignedLeaderNo": True,
        "enforcePositionStateExit": False,
        "experimentalYesLadderEnabled": True,
        "yesLadderMinLegs": 2,
        "yesLadderMaxLegs": 3,
        "yesLadderMaxCombinedPrice": 0.99,
        "metarCadenceLookbackDays": 10000,
        "recentLessonsPerCity": 12,
        "maxReviewEventsPerRun": 5,
        "scheduledReviewsEnabled": False,
    }


def action(
    kind: str, market_id: str | None = None, shares: float | None = None,
    outcome_side: str | None = None,
) -> dict:
    side = outcome_side or ("NO" if market_id else None)
    is_trade = market_id is not None
    return {
        "action": kind,
        "entryType": (
            "YES_CONVERGENCE" if kind == "buy" and side == "YES"
            else "NO_EXCLUSION" if kind == "buy" and side == "NO"
            else "POSITION_MANAGEMENT" if kind in {"hold", "sell"}
            else "NONE"
        ),
        "sizingTier": "BASE" if kind == "buy" else "NOT_APPLICABLE",
        "marketId": market_id,
        "outcomeSide": side,
        "shares": shares,
        "outcomeAssessment": ("clear_leader" if side == "YES" else "highly_unlikely") if is_trade else "not_applicable",
        "probabilityBand": ("30_50" if side == "YES" else "lt_5") if is_trade else None,
        "priceAssessment": "favorable" if is_trade else "not_applicable",
        "priceRiskAssessment": "The executable price leaves room below the conservative coarse-band cap.",
        "marketImpliedProbability": (0.20 if side == "YES" else 0.80) if is_trade else None,
        "consensusPosition": "against_consensus" if side == "YES" and is_trade else "with_consensus" if is_trade else "unclear",
        "evidenceStrength": "strong" if is_trade else "weak",
        "whyMarketMayBeRight": "The market may correctly price adjacent-bucket and brief-touch risk.",
        "whyMarketMayBeWrong": "A fresh observation and weather-process change may not yet be reflected.",
        "disagreementEvidence": ["fresh METAR divergence", "weather regime evidence", "market price lag"] if is_trade else [],
        "newInformationTypes": ["metar_model_divergence", "market_lag"] if is_trade else [],
        "exactBucketRiskAssessment": "Exact final bucket can lose by undershoot or overshoot." if is_trade else "No trade.",
        "upperBucketRisk": "moderate" if side == "YES" and is_trade else "unknown",
        "heatingProcessStatus": "capping" if is_trade else "unclear",
        "newEvidenceSincePrior": ["fresh METAR changed the path"] if is_trade else [],
        "thesis": "test thesis",
        "evidence": ["test evidence"],
        "keyRisk": "unexpected temperature spike",
        "invalidationCondition": "new METAR breaks the trajectory",
    }


def context(observation: str, book: dict | None = None) -> dict:
    return {
        "event": {
            "event_id": "event-1", "city": "Shanghai", "station_id": "ZSPD",
            "timezone": "Asia/Shanghai", "target_date": "2026-07-23",
        },
        "trigger": {
            "observationTimeUtc": observation, "slotUtc": observation,
        },
        "markets": [{
            "marketId": "market-1", "outcomeRange": "35 C",
            "bucketLow": 35, "bucketHigh": 35,
            "yesBook": {
                "asks": [{"price": 0.20, "size": 10}],
                "bids": [{"price": 0.18, "size": 10}],
            },
            "noBook": book or {
                "asks": [{"price": 0.80, "size": 10}],
                "bids": [{"price": 0.75, "size": 10}],
            },
        }, {
            "marketId": "market-0", "outcomeRange": "34 C", "bucketLow": 34, "bucketHigh": 34,
            "yesBook": {"asks": [{"price": 0.20, "size": 10}], "bids": [{"price": 0.18, "size": 10}]},
            "noBook": {"asks": [{"price": 0.80, "size": 10}], "bids": [{"price": 0.75, "size": 10}]},
        }, {
            "marketId": "market-2", "outcomeRange": "36 C", "bucketLow": 36, "bucketHigh": 36,
            "yesBook": {"asks": [{"price": 0.20, "size": 10}], "bids": [{"price": 0.18, "size": 10}]},
            "noBook": {"asks": [{"price": 0.80, "size": 10}], "bids": [{"price": 0.75, "size": 10}]},
        }],
        "dataFreshness": {"marketAgeMinutes": 0},
    }


def response(observation: str, actions: list[dict]) -> dict:
    selected_side = next(
        (item.get("outcomeSide") for item in actions if item.get("marketId") == "market-1" and item.get("action") == "buy"),
        None,
    )
    selected_is_yes = selected_side == "YES"
    return {
        "generatedAt": observation,
        "cycles": [{
            "eventId": "event-1", "city": "Shanghai",
            "metarObservationTimeUtc": observation,
            "stateAssessment": "test state",
            "modelRealityGap": "test model versus observation gap",
            "remainingHeatingAssessment": "test remaining heating window",
            "marketConsensusAssessment": "test market consensus assessment",
            "weatherProcessAssessment": "test observed weather process",
            "modelCorrectionAssessment": "test model correction with uncertainty",
            "processConfidence": "moderate",
            "marketDecisionMode": "WATCH",
            "futureScenarios": [
                {"name": "base", "plausibility": "primary", "weatherEvolution": "stable", "peakLowC": 34, "peakHighC": 35, "supportingEvidence": ["test"], "invalidationSignal": "change"},
                {"name": "warm", "plausibility": "plausible", "weatherEvolution": "clearing", "peakLowC": 35, "peakHighC": 36, "supportingEvidence": ["test"], "invalidationSignal": "cloud"},
            ],
            "settlementDistribution": [
                {"outcomeRange": "34 C", "rank": 2 if selected_is_yes else 1, "classification": "contender" if selected_is_yes else "clear_leader", "probabilityBand": "15_30" if selected_is_yes else "30_50", "reason": "test"},
                {"outcomeRange": "35 C", "rank": 1 if selected_is_yes else 3, "classification": "clear_leader" if selected_is_yes else "highly_unlikely", "probabilityBand": "30_50" if selected_is_yes else "lt_5", "reason": "test"},
                {"outcomeRange": "36 C", "rank": 3 if selected_is_yes else 2, "classification": "plausible", "probabilityBand": "15_30", "reason": "test"},
            ],
            "temperatureThesis": "test temperature path",
            "uncertaintyAssessment": "test uncertainty",
            "nextReviewReason": "wait for next METAR",
            "actions": actions,
        }],
    }


def uncertain_overshoot_response(
    observation: str, shares: float = 5, sizing_tier: str = "BASE",
) -> dict:
    trade = action("buy", "market-1", shares, "NO")
    trade.update({
        "entryType": "NO_OVERSHOOT",
        "sizingTier": sizing_tier,
        "outcomeAssessment": "contender",
        "probabilityBand": "30_50",
        "marketImpliedProbability": 0.12,
        "consensusPosition": "against_consensus",
        "evidenceStrength": "strong",
        "disagreementEvidence": [
            "fresh METAR leaves the upper path open",
            "radar shows no nearby cold-pool trigger",
            "satellite shows clearing",
            "the order book converged before the process ended",
        ],
        "newInformationTypes": [
            "weather_regime_change", "station_mechanism", "orderbook_dislocation",
        ],
        "heatingProcessStatus": "active",
        "newEvidenceSincePrior": ["fresh clearing and radar observations preserve overshoot"],
    })
    result = response(observation, [trade])
    cycle = result["cycles"][0]
    cycle["marketDecisionMode"] = "NO_RESEARCH"
    selected = next(
        item for item in cycle["settlementDistribution"]
        if item["outcomeRange"] == "35 C"
    )
    selected.update({"classification": "contender", "probabilityBand": "30_50"})
    return result


class WeatherAIAgentTests(unittest.TestCase):
    def setUp(self):
        self.engine = object.__new__(WeatherAIAgent)
        self.engine.config = config()
        self.engine.db = sqlite3.connect(":memory:")
        self.engine.db.row_factory = sqlite3.Row
        self.engine._init_agent_schema()
        self.engine.db.executescript(
            """
            CREATE TABLE stations (
                station_id TEXT PRIMARY KEY,station_name TEXT,city TEXT,latitude REAL,
                longitude REAL,timezone TEXT
            );
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,city TEXT,target_date TEXT,station_id TEXT,
                station_name TEXT,resolution_source TEXT,rules TEXT,end_date_utc TEXT,
                last_seen_utc TEXT,resolved_at_utc TEXT,winning_range TEXT
            );
            CREATE TABLE weather_observations (
                station_id TEXT,source TEXT,status TEXT,slot_utc TEXT,observation_time_utc TEXT,
                fetched_at_utc TEXT,temperature_c REAL,dewpoint_c REAL,relative_humidity REAL,
                wind_direction_deg REAL,wind_speed REAL,wind_speed_unit TEXT,weather_code TEXT,
                observed_daily_max_c REAL
            );
            CREATE TABLE market_resolutions (
                market_id TEXT PRIMARY KEY,resolved_at_utc TEXT,is_resolved INTEGER,
                winning_outcome TEXT,no_final_price REAL
            );
            CREATE TABLE markets (
                market_id TEXT PRIMARY KEY,event_id TEXT,outcome_range TEXT,
                bucket_low REAL,bucket_high REAL,bucket_unit TEXT
            );
            CREATE TABLE market_snapshots (
                slot_utc TEXT,market_id TEXT,event_id TEXT,yes_best_bid REAL,yes_best_ask REAL,
                no_best_bid REAL,no_best_ask REAL,yes_book_json TEXT,no_book_json TEXT,
                market_volume_24h REAL,market_liquidity REAL
            );
            """
        )
        self.engine.db.execute(
            "INSERT INTO stations VALUES('ZSPD','Pudong','Shanghai',31.14,121.8,'Asia/Shanghai')"
        )
        self.engine.db.execute(
            "INSERT INTO events VALUES('event-1','Shanghai','2026-07-23','ZSPD','Pudong','WU','rules',NULL,'2026-07-23T05:00:00+00:00',NULL,NULL)"
        )
        self.engine.db.execute(
            "INSERT INTO markets VALUES('market-1','event-1','35 C',35,35,'C')"
        )
        for slot, yes_bid, yes_ask, no_bid, no_ask in (
            ("2026-07-23T01:00:00+00:00", 0.17, 0.19, 0.79, 0.81),
            ("2026-07-23T01:30:00+00:00", 0.18, 0.20, 0.78, 0.80),
        ):
            yes_book = json.dumps({"bids": [{"price": yes_bid, "size": 10}], "asks": [{"price": yes_ask, "size": 10}]})
            no_book = json.dumps({"bids": [{"price": no_bid, "size": 10}], "asks": [{"price": no_ask, "size": 10}]})
            self.engine.db.execute(
                "INSERT INTO market_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (slot, "market-1", "event-1", yes_bid, yes_ask, no_bid, no_ask, yes_book, no_book, 100, 200),
            )
        for minute in (0, 30, 60, 90):
            observed = datetime(2026, 7, 23, minute // 60, minute % 60, tzinfo=UTC)
            fetched = observed.replace(minute=(observed.minute + 20) % 60)
            if fetched < observed:
                fetched = fetched.replace(hour=fetched.hour + 1)
            self.engine.db.execute(
                "INSERT INTO weather_observations VALUES('ZSPD','metar','ok',?,?,?,30,25,70,180,5,'mps','CAVOK',30)",
                (observed.isoformat(), observed.isoformat(), fetched.isoformat()),
            )
        self.engine.db.commit()

    def tearDown(self):
        self.engine.db.close()

    def insert_market_snapshot(
        self, slot: str, no_bid: float | None, no_ask: float | None,
        no_bid_size: float = 10, no_ask_size: float = 10,
        market_id: str = "market-1",
    ) -> None:
        yes_bid = 1 - no_ask if no_ask is not None else None
        yes_ask = 1 - no_bid if no_bid is not None else None
        yes_book = {
            "bids": [{"price": yes_bid, "size": no_ask_size}] if yes_bid is not None else [],
            "asks": [{"price": yes_ask, "size": no_bid_size}] if yes_ask is not None else [],
        }
        no_book = {
            "bids": [{"price": no_bid, "size": no_bid_size}] if no_bid is not None else [],
            "asks": [{"price": no_ask, "size": no_ask_size}] if no_ask is not None else [],
        }
        self.engine.db.execute(
            "INSERT INTO market_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                slot, market_id, "event-1", yes_bid, yes_ask, no_bid, no_ask,
                json.dumps(yes_book), json.dumps(no_book), 100, 200,
            ),
        )
        self.engine.db.commit()

    def test_bid_and_ask_vwap_use_correct_book_sides(self):
        book = json.dumps({
            "asks": [{"price": 0.80, "size": 2}, {"price": 0.82, "size": 4}],
            "bids": [{"price": 0.77, "size": 3}, {"price": 0.75, "size": 4}],
        })
        ask, _ = executable_vwap(book, 5, "asks")
        bid, _ = executable_vwap(book, 5, "bids")
        self.assertAlmostEqual(ask, (0.80 * 2 + 0.82 * 3) / 5)
        self.assertAlmostEqual(bid, (0.77 * 3 + 0.75 * 2) / 5)

    def test_active_agent_initialization_does_not_create_legacy_no_paper_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            isolated_config = config()
            isolated_config["databasePath"] = str(Path(directory) / "agent.sqlite3")
            engine = WeatherAIAgent(isolated_config)
            try:
                tables = {
                    row[0] for row in engine.db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            finally:
                engine.close()
        self.assertIn("weather_ai_agent_cycles", tables)
        self.assertFalse(any(name.startswith("weather_no_paper_") for name in tables))

    def test_metar_cadence_learns_half_hour_schedule(self):
        cadence = self.engine.metar_cadence("ZSPD", "Shanghai")
        self.assertEqual(cadence["sampleReports"], 4)
        self.assertEqual(cadence["medianIntervalMinutes"], 30)
        self.assertEqual(cadence["usualReportMinutesUtcHour"], [0, 30])
        self.assertEqual(cadence["medianPublicationLagMinutes"], 20)
        self.assertFalse(self.engine.db.in_transaction)

    def test_previous_metar_supports_legacy_and_enriched_observation_schemas(self):
        previous = self.engine.previous_metar("ZSPD", "2026-07-23T01:30:00+00:00")
        self.assertIsNotNone(previous)
        self.assertEqual(previous["observation_time_utc"], "2026-07-23T01:00:00+00:00")
        self.assertEqual(previous["temperature_c"], 30)

    def test_model_context_exposes_ensemble_without_inventing_model_run_time(self):
        self.engine.db.executescript(
            """
            CREATE TABLE windy_forecasts (
                station_id TEXT,target_date TEXT,model TEXT,status TEXT,slot_utc TEXT,
                model_ref_time_utc TEXT,model_updated_at_utc TEXT,forecast_max_c REAL,
                forecast_peak_local TEXT,points_json TEXT
            );
            CREATE TABLE source_collection_versions (
                source TEXT,station_id TEXT,target_date TEXT,product TEXT,version_hash TEXT,
                raw_payload_id INTEGER
            );
            CREATE TABLE external_forecasts (
                station_id TEXT,target_date TEXT,model TEXT,status TEXT,slot_utc TEXT,
                forecast_max_c REAL,forecast_peak_local TEXT,points_json TEXT,raw_payload_id INTEGER
            );
            CREATE TABLE ensemble_forecasts (
                station_id TEXT,target_date TEXT,model TEXT,status TEXT,slot_utc TEXT,
                version_hash TEXT,model_run_time_utc TEXT,model_run_time_source TEXT,
                model_run_confidence TEXT,member_count INTEGER,mean_max_c REAL,std_max_c REAL,
                min_max_c REAL,max_max_c REAL,q10_max_c REAL,q50_max_c REAL,q90_max_c REAL
            );
            INSERT INTO source_collection_versions VALUES(
                'open_meteo','ZSPD','2026-07-23','ecmwf_ifs025','version-hash',11
            );
            INSERT INTO external_forecasts VALUES(
                'ZSPD','2026-07-23','ecmwf_ifs025','ok','2026-07-23T01:00:00+00:00',
                35.0,'2026-07-23T14:00:00+08:00','[]',11
            );
            INSERT INTO ensemble_forecasts VALUES(
                'ZSPD','2026-07-23','ecmwf_ifs025','ok','2026-07-23T01:00:00+00:00',
                'ensemble-hash',NULL,'provider_not_exposed','unavailable',51,
                34.8,0.7,33.1,36.2,34.0,34.8,35.7
            );
            """
        )
        state = self.engine.model_update_state(
            {"station_id": "ZSPD", "target_date": "2026-07-23"},
            datetime(2026, 7, 23, 1, 30, tzinfo=UTC),
        )
        self.assertEqual(state["ecmwf"]["modelVersion"], "version-hash")
        self.assertIsNone(state["ecmwf"]["modelRunTimeUtc"])
        self.assertEqual(state["ecmwf"]["modelRunTimeConfidence"], "unavailable")
        self.assertEqual(state["ecmwfEnsemble"]["memberCount"], 51)
        self.assertEqual(state["ecmwfEnsemble"]["role"], "research_evidence_only")
        self.assertIsNone(state["ecmwfEnsemble"]["modelRunTimeUtc"])

    def test_market_state_exposes_executable_yes_and_no_books(self):
        event = {"event_id": "event-1"}
        states = self.engine.market_states(event, datetime(2026, 7, 23, 1, 30, tzinfo=UTC))
        self.assertEqual(len(states), 1)
        self.assertAlmostEqual(states[0]["yesExecutableBuyPrice5"], 0.20)
        self.assertAlmostEqual(states[0]["yesExecutableSellPrice5"], 0.18)
        self.assertAlmostEqual(states[0]["noExecutableBuyPrice5"], 0.80)
        self.assertAlmostEqual(states[0]["noExecutableSellPrice5"], 0.78)
        self.assertAlmostEqual(states[0]["yesAskChange"], 0.01)
        self.assertAlmostEqual(states[0]["noAskChange"], -0.01)

    def test_execution_refreshes_price_after_ai_latency(self):
        self.engine.config.update({
            "realtimeExecutionGuards": True,
            "refreshMarketBeforeExecution": True,
        })
        observed = "2026-07-23T02:00:00+00:00"
        self.insert_market_snapshot("2026-07-23T02:01:00+00:00", 0.79, 0.82)
        completed = datetime(2026, 7, 23, 2, 2, tzinfo=UTC)
        with patch("weather_ai_agent.utc_now", return_value=completed):
            self.engine.persist_response(
                1, [context(observed)], response(observed, [action("buy", "market-1", 5, "NO")])
            )
        row = self.engine.db.execute(
            """
            SELECT executed_action,execution_price,analysis_market_snapshot_at_utc,
                   execution_market_snapshot_at_utc,execution_market_age_minutes
            FROM weather_ai_agent_actions
            """
        ).fetchone()
        self.assertEqual(row["executed_action"], "buy")
        self.assertAlmostEqual(row["execution_price"], 0.82)
        self.assertEqual(row["execution_market_snapshot_at_utc"], "2026-07-23T02:01:00+00:00")
        self.assertAlmostEqual(row["execution_market_age_minutes"], 1.0)

    def test_paper_market_order_records_new_metar_and_executes_at_latest_price(self):
        self.engine.config.update({
            "realtimeExecutionGuards": True,
            "refreshMarketBeforeExecution": True,
            "refreshWeatherBeforeExecution": True,
            "paperMarketOrderExecution": True,
        })
        observed = "2026-07-23T02:00:00+00:00"
        decision_context = context(observed)
        self.insert_market_snapshot("2026-07-23T02:01:00+00:00", 0.01, 0.99)
        execution_context = dict(decision_context)
        metadata = {
            "executionMetarObservationTimeUtc": "2026-07-23T02:30:00+00:00",
            "executionWeatherProcessSlotUtc": "2026-07-23T02:30:00+00:00",
            "metarChanged": True,
            "alignmentChanged": False,
            "alignment": {"mode": "WATCH", "weatherAlignment": "unresolved"},
        }
        completed = datetime(2026, 7, 23, 2, 32, tzinfo=UTC)
        with (
            patch("weather_ai_agent.utc_now", return_value=completed),
            patch.object(
                self.engine, "_execution_weather_context",
                return_value=(execution_context, metadata),
            ),
        ):
            self.engine.persist_response(
                1, [decision_context],
                response(observed, [action("buy", "market-1", 5, "NO")]),
            )
        row = self.engine.db.execute(
            """
            SELECT executed_action,executed_shares,execution_price,rejection_reason,
                   weather_revalidation_status,
                   execution_metar_observation_time_utc,execution_weather_process_slot_utc
            FROM weather_ai_agent_actions
            """
        ).fetchone()
        self.assertEqual(row["executed_action"], "buy")
        self.assertEqual(row["executed_shares"], 5)
        self.assertAlmostEqual(row["execution_price"], 0.99)
        self.assertIsNone(row["rejection_reason"])
        self.assertEqual(
            row["weather_revalidation_status"], "new_metar_recorded_paper_market_order"
        )
        self.assertEqual(
            row["execution_metar_observation_time_utc"], "2026-07-23T02:30:00+00:00"
        )
        self.assertEqual(row["execution_weather_process_slot_utc"], "2026-07-23T02:30:00+00:00")

    def test_weather_change_still_rejects_when_paper_market_order_mode_is_disabled(self):
        self.engine.config.update({
            "realtimeExecutionGuards": True,
            "refreshMarketBeforeExecution": True,
            "refreshWeatherBeforeExecution": True,
            "paperMarketOrderExecution": False,
        })
        observed = "2026-07-23T02:00:00+00:00"
        decision_context = context(observed)
        self.insert_market_snapshot("2026-07-23T02:01:00+00:00", 0.79, 0.82)
        metadata = {
            "executionMetarObservationTimeUtc": "2026-07-23T02:30:00+00:00",
            "executionWeatherProcessSlotUtc": "2026-07-23T02:30:00+00:00",
            "metarChanged": True,
            "alignmentChanged": False,
            "alignment": {"mode": "WATCH", "weatherAlignment": "unresolved"},
        }
        with (
            patch("weather_ai_agent.utc_now", return_value=datetime(2026, 7, 23, 2, 32, tzinfo=UTC)),
            patch.object(
                self.engine, "_execution_weather_context",
                return_value=(decision_context, metadata),
            ),
        ):
            self.engine.persist_response(
                1, [decision_context],
                response(observed, [action("buy", "market-1", 5, "NO")]),
            )
        row = self.engine.db.execute(
            "SELECT executed_action,rejection_reason,weather_revalidation_status "
            "FROM weather_ai_agent_actions"
        ).fetchone()
        self.assertEqual(row["executed_action"], "rejected")
        self.assertIn("new METAR", row["rejection_reason"])
        self.assertEqual(row["weather_revalidation_status"], "new_metar_requires_reanalysis")

    def test_no_only_decision_rejects_follow_aligned_market_leader_no(self):
        self.engine.config["noOnlyPaperMode"] = True
        observed = "2026-07-23T02:00:00+00:00"
        decision_context = context(observed)
        decision_context["marketAlignment"] = {
            "mode": "FOLLOW",
            "weatherAlignment": "aligned",
            "marketPrematureConvergence": False,
            "marketLeader": {"marketId": "market-1", "bucketC": 35},
        }
        buy = action("buy", "market-1", 5, "NO")
        buy["entryType"] = "NO_OVERSHOOT"
        buy["heatingProcessStatus"] = "active"
        decision = response(observed, [buy])
        decision["cycles"][0]["marketDecisionMode"] = "NO_RESEARCH"
        with self.assertRaisesRegex(RuntimeError, "FOLLOW\\+aligned"):
            self.engine._validate_cycle_reasoning(decision["cycles"][0], decision_context)

    def test_execution_rejects_when_latest_depth_disappears(self):
        self.engine.config.update({
            "realtimeExecutionGuards": True,
            "refreshMarketBeforeExecution": True,
        })
        observed = "2026-07-23T02:00:00+00:00"
        self.insert_market_snapshot("2026-07-23T02:01:00+00:00", 0.79, None)
        with patch(
            "weather_ai_agent.utc_now", return_value=datetime(2026, 7, 23, 2, 2, tzinfo=UTC)
        ):
            self.engine.persist_response(
                1, [context(observed)], response(observed, [action("buy", "market-1", 5, "NO")])
            )
        row = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions"
        ).fetchone()
        self.assertEqual(row["executed_action"], "rejected")
        self.assertIn("0.0000 shares executable", row["rejection_reason"])

    def test_execution_rechecks_window_after_ai_completes(self):
        self.engine.config.update({
            "tradeLocalStartHour": 10,
            "tradeLocalEndHour": 19,
            "realtimeExecutionGuards": True,
            "refreshMarketBeforeExecution": True,
        })
        observed = "2026-07-23T10:59:00+00:00"  # 18:59 Shanghai
        self.insert_market_snapshot(observed, 0.78, 0.80)
        completed = datetime(2026, 7, 23, 11, 1, tzinfo=UTC)  # 19:01 Shanghai
        with patch("weather_ai_agent.utc_now", return_value=completed):
            self.engine.persist_response(
                1, [context(observed)], response(observed, [action("buy", "market-1", 5, "NO")])
            )
        row = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions"
        ).fetchone()
        self.assertEqual(row["executed_action"], "observe")
        self.assertIn("10:00-19:00", row["rejection_reason"])

    def test_watch_position_is_not_sold_before_invalid_state(self):
        first = "2026-07-23T02:00:00+00:00"
        self.engine.persist_response(
            1, [context(first)], response(first, [action("buy", "market-1", 5, "NO")])
        )
        self.engine.config["enforcePositionStateExit"] = True
        second = "2026-07-23T02:30:00+00:00"
        decision = response(second, [action("sell", "market-1", 5, "NO")])
        distribution = decision["cycles"][0]["settlementDistribution"]
        distribution[1]["classification"] = "contender"
        distribution[1]["rank"] = 2
        distribution[0]["classification"] = "clear_leader"
        distribution[0]["rank"] = 1
        distribution[2]["classification"] = "plausible"
        distribution[2]["rank"] = 3
        self.engine.persist_response(2, [context(second)], decision)
        row = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["executed_action"], "hold")
        self.assertIn("WATCH", row["rejection_reason"])

    def test_touched_watch_position_can_be_sold(self):
        first = "2026-07-23T02:00:00+00:00"
        self.engine.persist_response(
            1, [context(first)], response(first, [action("buy", "market-1", 5, "NO")])
        )
        self.engine.config["enforcePositionStateExit"] = True
        second_context = context("2026-07-23T02:30:00+00:00")
        second_context["metar"] = {"current": {"temperature_c": 35, "observed_daily_max_c": 35}}
        sell_action = action("sell", "market-1", 5, "NO")
        sell_action["heatingProcessStatus"] = "active"
        decision = response("2026-07-23T02:30:00+00:00", [sell_action])
        distribution = decision["cycles"][0]["settlementDistribution"]
        distribution[1]["classification"] = "contender"
        distribution[1]["rank"] = 2
        distribution[0]["classification"] = "clear_leader"
        distribution[0]["rank"] = 1
        distribution[2]["classification"] = "plausible"
        distribution[2]["rank"] = 3
        self.engine.persist_response(2, [second_context], decision)
        row = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["executed_action"], "sell")
        self.assertIsNone(row["rejection_reason"])

    def test_yes_ladder_experiment_requires_adjacent_top_legs_and_combined_cost(self):
        self.engine.db.execute(
            "INSERT INTO markets VALUES('market-0','event-1','34 C',34,34,'C')"
        )
        self.engine.db.execute(
            "INSERT INTO market_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-07-23T01:30:00+00:00", "market-0", "event-1", 0.79, 0.80, 0.19, 0.20,
                json.dumps({"bids": [{"price": 0.79, "size": 10}], "asks": [{"price": 0.80, "size": 10}]}),
                json.dumps({"bids": [{"price": 0.19, "size": 10}], "asks": [{"price": 0.20, "size": 10}]}), 100, 200,
            ),
        )
        self.engine.db.commit()
        observed = "2026-07-23T01:30:00+00:00"
        first = action("buy", "market-1", 5, "YES")
        first["entryType"] = "YES_LADDER_EXPERIMENT"
        second = action("buy", "market-0", 5, "YES")
        second.update({
            "entryType": "YES_LADDER_EXPERIMENT",
            "outcomeAssessment": "contender",
            "probabilityBand": "15_30",
        })
        self.engine.persist_response(
            1, [context(observed)], response(observed, [first, second])
        )
        rows = self.engine.db.execute(
            "SELECT executed_action,entry_type,execution_price FROM weather_ai_agent_actions ORDER BY action_id"
        ).fetchall()
        self.assertEqual([row["executed_action"] for row in rows], ["buy", "buy"])
        self.assertEqual(
            [row["entry_type"] for row in rows],
            ["YES_LADDER_EXPERIMENT", "YES_LADDER_EXPERIMENT"],
        )
        self.assertAlmostEqual(sum(row["execution_price"] for row in rows), 0.4)

    def test_yes_ladder_rejects_combined_price_at_or_above_one(self):
        self.engine.db.execute(
            "INSERT INTO markets VALUES('market-0','event-1','34 C',34,34,'C')"
        )
        self.engine.db.execute(
            "INSERT INTO market_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-07-23T01:30:00+00:00", "market-0", "event-1", 0.19, 0.20, 0.79, 0.80,
                json.dumps({"bids": [{"price": 0.19, "size": 10}], "asks": [{"price": 0.20, "size": 10}]}),
                json.dumps({"bids": [{"price": 0.79, "size": 10}], "asks": [{"price": 0.80, "size": 10}]}), 100, 200,
            ),
        )
        self.engine.db.execute(
            "UPDATE market_snapshots SET yes_book_json=?,yes_best_ask=?,yes_best_bid=? WHERE market_id='market-1'",
            (json.dumps({"bids": [{"price": 0.19, "size": 10}], "asks": [{"price": 0.80, "size": 10}]}), 0.80, 0.19),
        )
        self.engine.db.commit()
        observed = "2026-07-23T01:30:00+00:00"
        first = action("buy", "market-1", 5, "YES")
        second = action("buy", "market-0", 5, "YES")
        first["entryType"] = second["entryType"] = "YES_LADDER_EXPERIMENT"
        first["marketImpliedProbability"] = 0.80
        second["outcomeAssessment"] = "contender"
        second["probabilityBand"] = "15_30"
        ladder_context = context(observed)
        ladder_context["markets"][0]["yesBook"]["asks"][0]["price"] = 0.80
        self.engine.persist_response(1, [ladder_context], response(observed, [first, second]))
        rows = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions ORDER BY action_id"
        ).fetchall()
        self.assertEqual([row["executed_action"] for row in rows], ["rejected", "rejected"])
        self.assertTrue(all("combined executable price" in row["rejection_reason"] for row in rows))

    def test_decision_prompt_compacts_duplicate_raw_payloads(self):
        original = context("2026-07-23T02:00:00+00:00")
        original["weather"] = {"meteoblue": {"hourly": [{"time": "x"}], "maxC": 35}}
        compact = self.engine._decision_prompt_context(original)
        self.assertNotIn("yesBook", compact["markets"][0])
        self.assertNotIn("noBook", compact["markets"][0])
        self.assertNotIn("hourly", compact["weather"]["meteoblue"])
        self.assertIn("yesBook", original["markets"][0])

    def test_opportunity_ledger_records_skipped_single_leg_candidates(self):
        observed = "2026-07-23T01:30:00+00:00"
        opportunity_context = context(observed)
        for market in opportunity_context["markets"]:
            market["yesExecutableBuyPrice5"] = 0.20
            market["noExecutableBuyPrice5"] = 0.80
        self.engine.persist_response(
            1, [opportunity_context], response(observed, [action("observe")])
        )
        rows = self.engine.db.execute(
            "SELECT opportunity_type,candidate_status,market_id,outcome_side FROM weather_ai_opportunity_evaluations ORDER BY opportunity_id"
        ).fetchall()
        self.assertEqual(len(rows), 6)
        self.assertEqual({row["opportunity_type"] for row in rows}, {"YES_SINGLE", "NO_SINGLE"})
        self.assertIn("NO_ELIGIBLE_SKIPPED", {row["candidate_status"] for row in rows})
        self.assertIn("NOT_ELIGIBLE", {row["candidate_status"] for row in rows})

    def test_opportunity_ledger_backfills_resolution_and_hypothetical_pnl(self):
        observed = "2026-07-23T01:30:00+00:00"
        opportunity_context = context(observed)
        for market in opportunity_context["markets"]:
            market["yesExecutableBuyPrice5"] = 0.20
            market["noExecutableBuyPrice5"] = 0.80
        self.engine.persist_response(
            1, [opportunity_context], response(observed, [action("observe")])
        )
        self.engine.db.execute(
            "UPDATE events SET resolved_at_utc=?,winning_range=? WHERE event_id='event-1'",
            ("2026-07-23T05:00:00+00:00", "35 C"),
        )
        self.engine.db.commit()
        self.assertEqual(self.engine.refresh_opportunity_evaluations(), 6)
        row = self.engine.db.execute(
            "SELECT resolved_at_utc,winning_range,signal_correct,hypothetical_pnl_usdc FROM weather_ai_opportunity_evaluations WHERE opportunity_type='YES_SINGLE' AND outcome_range='35 C'"
        ).fetchone()
        self.assertEqual(row["winning_range"], "35 C")
        self.assertEqual(row["signal_correct"], 1)
        self.assertAlmostEqual(row["hypothetical_pnl_usdc"], 4.0)

    def test_duplicate_research_snapshot_closes_empty_write_transaction(self):
        now = datetime(2026, 7, 23, 1, 30, tzinfo=UTC)
        event = {
            "event_id": "event-1", "city": "Shanghai", "target_date": "2026-07-23",
            "station_id": "ZSPD", "timezone": "Asia/Shanghai",
        }
        markets = [{
            "marketId": "market-1", "outcomeRange": "35 C", "snapshotUtc": now.isoformat(),
            "yesBestBid": 0.18, "yesBestAsk": 0.20, "noBestBid": 0.78, "noBestAsk": 0.80,
            "yesExecutableBuyPrice5": 0.20, "noExecutableBuyPrice5": 0.80,
            "yesBuyAvailableShares": 10, "noBuyAvailableShares": 10,
            "volume24h": 100, "liquidity": 200,
        }]
        with patch.object(self.engine, "research_events", return_value=[event]), \
                patch.object(self.engine, "market_states", return_value=markets), \
                patch.object(self.engine, "local_weather_payload", return_value={
                    "metar": {"obsTime": now.isoformat(), "temp": 30, "dailyMaxC": 30}
                }), \
                patch.object(self.engine, "model_update_state", return_value={
                    "meteoblue": {"maxC": 35}, "ecmwf": {"maxC": 34.5}
                }), \
                patch.object(self.engine, "weather_process_state", return_value={"status": "ok"}):
            self.assertEqual(self.engine.record_research_snapshots(now), 1)
            self.assertFalse(self.engine.db.in_transaction)
            self.assertEqual(self.engine.record_research_snapshots(now), 0)
            self.assertFalse(self.engine.db.in_transaction)

    def test_research_snapshot_collects_ridge_without_an_ai_request(self):
        now = datetime(2026, 7, 23, 2, 0, tzinfo=UTC)
        event = {
            "event_id": "event-1", "city": "Shanghai", "target_date": "2026-07-23",
            "station_id": "ZSPD", "timezone": "Asia/Shanghai",
        }
        ridge = {
            "status": "ok", "featureAsOfUtc": now.isoformat(),
            "bucketProbabilities": [{"bucketC": 35, "probability": 0.60}],
        }
        self.engine.ridge_v2 = MagicMock()
        self.engine.ridge_v2.snapshot.return_value = ridge
        slot_text = self.engine._research_snapshot_slot(now).isoformat()
        with patch.object(self.engine, "research_events", return_value=[event]), \
                patch.object(self.engine, "market_states", return_value=[{
                    "marketId": "market-1", "outcomeRange": "35 C",
                    "snapshotUtc": now.isoformat(), "yesBestBid": 0.18,
                    "yesBestAsk": 0.20, "noBestBid": 0.78, "noBestAsk": 0.80,
                }]), \
                patch.object(self.engine, "local_weather_payload", return_value={
                    "metar": {"obsTime": now.isoformat(), "temp": 30, "dailyMaxC": 30}
                }), \
                patch.object(self.engine, "model_update_state", return_value={}), \
                patch.object(self.engine, "weather_process_state", return_value={"status": "ok"}), \
                patch.object(self.engine, "record_ladder_shadow_snapshots", return_value=0) as ladder:
            self.assertEqual(self.engine.record_research_snapshots(now), 1)

        self.engine.ridge_v2.snapshot.assert_called_once_with(
            event, now, f"research:{slot_text}", {"status": "ok"}
        )
        saved = self.engine.db.execute(
            "SELECT forecast_state_json FROM weather_ai_research_snapshots"
        ).fetchone()
        self.assertEqual(json.loads(saved["forecast_state_json"])["ridgeV2"], ridge)
        self.assertEqual(ladder.call_args.args[4], ridge)

    def test_frozen_ladder_catches_completed_1100_cutoff_before_deadline(self):
        now = datetime(2026, 8, 7, 3, 6, tzinfo=UTC)
        cutoff = "2026-08-07T03:00:00+00:00"
        event = {
            "event_id": "event-aug-7", "city": "Shanghai", "target_date": "2026-08-07",
            "station_id": "ZSPD", "timezone": "Asia/Shanghai",
        }
        self.engine.db.executescript(
            """
            CREATE TABLE runs(
                slot_utc TEXT,status TEXT,completed_at_utc TEXT
            );
            INSERT INTO runs VALUES(
                '2026-08-07T03:00:00+00:00','completed','2026-08-07T03:03:00+00:00'
            );
            """
        )
        with patch.object(self.engine, "research_events", return_value=[event]), \
                patch.object(self.engine, "market_states", return_value=self.frozen_ladder_markets(
                    snapshot=cutoff
                )), \
                patch.object(self.engine, "weather_process_state", return_value={
                    "status": "ok", "primaryStationTrend": {"temperatureTrendCPerHour": 0.8}
                }), \
                patch.object(self.engine, "local_weather_payload", return_value={
                    "metar": {"obsTime": cutoff, "temp": 33, "dailyMaxC": 33}
                }), \
                patch.object(self.engine, "model_update_state", return_value={
                    "ecmwfEnsemble": {"meanMaxC": 36, "stdMaxC": 0.8, "sampleSlotUtc": cutoff}
                }), \
                patch.object(self.engine, "_latest_ridge_for_shadow", return_value=(None, None)):
            self.assertEqual(self.engine.record_due_frozen_ladder_cutoff(now), (1, 2))
            self.assertEqual(self.engine.record_due_frozen_ladder_cutoff(now), (0, 0))

        row = self.engine.db.execute(
            "SELECT frozen_slot_utc,frozen_at_utc,market_snapshot_at_utc FROM weather_ladder_frozen_candidates"
        ).fetchone()
        self.assertEqual(row["frozen_slot_utc"], cutoff)
        self.assertEqual(row["market_snapshot_at_utc"], cutoff)
        self.assertEqual(row["frozen_at_utc"], now.isoformat())

    def test_frozen_ladder_cutoff_is_not_backfilled_after_deadline(self):
        self.engine.db.executescript(
            """
            CREATE TABLE runs(
                slot_utc TEXT,status TEXT,completed_at_utc TEXT
            );
            INSERT INTO runs VALUES(
                '2026-08-07T03:00:00+00:00','completed','2026-08-07T03:03:00+00:00'
            );
            """
        )
        self.assertIsNone(self.engine._due_frozen_ladder_cutoff_slot(
            datetime(2026, 8, 7, 3, 12, 1, tzinfo=UTC)
        ))

    def test_ridge_failure_does_not_abort_research_collection(self):
        now = datetime(2026, 7, 23, 2, 0, tzinfo=UTC)
        event = {
            "event_id": "event-1", "city": "Shanghai", "target_date": "2026-07-23",
            "station_id": "ZSPD", "timezone": "Asia/Shanghai",
        }
        self.engine.ridge_v2 = MagicMock()
        self.engine.ridge_v2.snapshot.side_effect = RuntimeError("model unavailable")
        with patch.object(self.engine, "research_events", return_value=[event]), \
                patch.object(self.engine, "market_states", return_value=[{
                    "marketId": "market-1", "outcomeRange": "35 C",
                    "snapshotUtc": now.isoformat(), "yesBestAsk": 0.20,
                }]), \
                patch.object(self.engine, "local_weather_payload", return_value={"metar": {}}), \
                patch.object(self.engine, "model_update_state", return_value={}), \
                patch.object(self.engine, "weather_process_state", return_value={"status": "ok"}), \
                patch.object(self.engine, "record_ladder_shadow_snapshots", return_value=0) as ladder:
            self.assertEqual(self.engine.record_research_snapshots(now), 1)

        ridge = ladder.call_args.args[4]
        self.assertEqual(ridge["status"], "unavailable")
        self.assertIn("model unavailable", ridge["reason"])
        self.assertIsNone(ladder.call_args.args[3])

    def test_ladder_shadow_records_every_adjacent_triple_and_resolves_package(self):
        observed = "2026-07-23T02:00:00+00:00"
        markets = []
        for bucket in (34, 35, 36, 37):
            markets.append({
                "marketId": f"m{bucket}", "outcomeRange": f"{bucket} C",
                "bucketLow": bucket, "bucketHigh": bucket, "bucketUnit": "C",
                "snapshotUtc": observed, "yesExecutableBuyPrice5": 0.20,
                "yesBuyAvailableShares": 10,
            })
        ridge = {
            "status": "ok", "modelVersion": "ridge_v2.1_shared_time",
            "distributionVersion": "ridge_v3_asymmetric_empirical",
            "calibrationStatus": "insufficient_independent_oos_dates",
            "calibrationDates": 5, "featureAsOfUtc": observed,
            "latestObservationTimeUtc": observed,
            "bucketProbabilities": [
                {"bucketC": bucket, "probability": probability}
                for bucket, probability in zip((34, 35, 36, 37), (0.10, 0.35, 0.30, 0.15))
            ],
            "stability": {"constraintApplied": False},
        }
        event = {"event_id": "event-1", "city": "Shanghai", "target_date": "2026-07-23"}
        self.assertEqual(
            self.engine.record_ladder_shadow_snapshots(event, observed, markets, 1, ridge), 2
        )
        rows = self.engine.db.execute(
            "SELECT triple_key,package_probability,combined_yes_price_5,package_edge,candidate_status "
            "FROM weather_ladder_shadow_snapshots ORDER BY triple_key"
        ).fetchall()
        self.assertEqual([row["triple_key"] for row in rows], ["34:35:36", "35:36:37"])
        self.assertAlmostEqual(rows[0]["package_probability"], 0.75)
        self.assertAlmostEqual(rows[0]["combined_yes_price_5"], 0.60)
        self.assertAlmostEqual(rows[0]["package_edge"], 0.15)
        self.assertTrue(all(row["candidate_status"] == "UNCALIBRATED_SHADOW" for row in rows))

        self.engine.db.execute(
            "UPDATE events SET resolved_at_utc=?,winning_range=? WHERE event_id='event-1'",
            ("2026-07-23T12:00:00+00:00", "35 C"),
        )
        self.engine.db.commit()
        self.assertEqual(self.engine.refresh_ladder_shadow_snapshots(), 2)
        settled = self.engine.db.execute(
            "SELECT package_hit,hypothetical_pnl_usdc FROM weather_ladder_shadow_snapshots"
        ).fetchall()
        self.assertTrue(all(row["package_hit"] == 1 for row in settled))
        self.assertTrue(all(abs(row["hypothetical_pnl_usdc"] - 2.0) < 1e-9 for row in settled))

    def frozen_ladder_markets(
        self, center_ask: float = 0.42, center_bid: float = 0.38,
        snapshot: str = "2026-07-23T03:00:00+00:00",
    ) -> list[dict]:
        quotes = {
            34: (0.10, 0.12), 35: (0.28, 0.32),
            36: (center_bid, center_ask), 37: (0.20, 0.22),
        }
        return [
            {
                "marketId": f"m{bucket}", "outcomeRange": f"{bucket} C",
                "bucketLow": bucket, "bucketHigh": bucket, "bucketUnit": "C",
                "snapshotUtc": snapshot, "yesBestBid": bid, "yesBestAsk": ask,
                "yesBook": {
                    "bids": [{"price": bid, "size": 20}],
                    "asks": [{"price": ask, "size": 20}],
                },
            }
            for bucket, (bid, ask) in quotes.items()
        ]

    def test_frozen_ladder_selects_market_favorite_and_exact_weighted_vwap(self):
        event = {
            "event_id": "event-1", "city": "Shanghai", "target_date": "2026-07-23",
            "timezone": "Asia/Shanghai",
        }
        markets = self.frozen_ladder_markets()
        now = datetime(2026, 7, 23, 3, 1, 30, tzinfo=UTC)
        self.assertEqual(
            self.engine.record_frozen_ladder_candidate(
                event, "2026-07-23T03:00:00+00:00", now, markets,
                {"status": "active_heating"}, {"status": "ok"},
                {
                    "observedDailyMaxC": 34.0, "temperatureTrendCPerHour": 1.0,
                    "ensembleMeanMaxC": 35.5, "ensembleStdMaxC": 0.8,
                },
            ),
            1,
        )
        row = self.engine.db.execute(
            "SELECT * FROM weather_ladder_frozen_candidates"
        ).fetchone()
        self.assertEqual(row["eligibility_status"], "ELIGIBLE_SHADOW")
        self.assertEqual(row["center_bucket_c"], 36)
        self.assertEqual((row["lower_bucket_c"], row["upper_bucket_c"]), (35, 37))
        self.assertAlmostEqual(row["center_lead"], 0.10)
        self.assertAlmostEqual(row["combined_cost_usdc"], 0.32 + 3 * 0.42 + 0.22)
        self.assertAlmostEqual(row["center_minus_observed_max_c"], 2.0)
        self.assertAlmostEqual(row["temperature_trend_c_per_hour"], 1.0)
        self.assertAlmostEqual(row["ensemble_std_max_c"], 0.8)
        self.assertAlmostEqual(row["ensemble_mean_minus_center_c"], -0.5)
        variants = self.engine.db.execute(
            """
            SELECT rule_version,lower_weight,center_weight,upper_weight,
                   combined_cost_usdc,weight_interpretation,execution_feasibility_status
            FROM weather_ladder_frozen_variants ORDER BY center_weight
            """
        ).fetchall()
        self.assertEqual(len(variants), 4)
        self.assertEqual(tuple(variants[1][name] for name in (
            "lower_weight", "center_weight", "upper_weight",
        )), (0.5, 4.0, 0.5))
        self.assertAlmostEqual(variants[1]["combined_cost_usdc"], 0.5 * 0.32 + 4 * 0.42 + 0.5 * 0.22)
        self.assertEqual(variants[1]["weight_interpretation"], "normalized_shadow_units")
        self.assertEqual(
            variants[1]["execution_feasibility_status"],
            "NORMALIZED_ONLY_CLOB_MINIMUM_NOT_VALIDATED",
        )
        self.assertEqual(tuple(variants[2][name] for name in (
            "lower_weight", "center_weight", "upper_weight",
        )), (5.0, 15.0, 5.0))
        self.assertEqual(variants[2]["weight_interpretation"], "actual_share_counts")
        self.assertEqual(tuple(variants[3][name] for name in (
            "lower_weight", "center_weight", "upper_weight",
        )), (5.0, 20.0, 5.0))
        self.assertEqual(variants[3]["weight_interpretation"], "actual_share_counts")
        self.assertEqual(
            variants[3]["execution_feasibility_status"],
            "CLOB_MINIMUM_5_SHARES_SATISFIED_SHADOW_ONLY",
        )
        self.assertEqual(
            self.engine.record_frozen_ladder_portfolio_selection(
                "2026-07-23T03:00:00+00:00"
            ),
            1,
        )
        portfolio = self.engine.db.execute(
            """
            SELECT * FROM weather_ladder_frozen_portfolio_selections
            WHERE portfolio_version='ladder_portfolio_v3_1100_5_20_5_lowest_center_lead'
            """
        ).fetchone()
        self.assertEqual(portfolio["selection_status"], "SELECTED_SHADOW")
        self.assertEqual(portfolio["city"], "Shanghai")
        self.assertLessEqual(portfolio["selected_cost_usdc"], 15.0)
        self.assertEqual(portfolio["diagnostic_positive_trend"], 1)
        self.assertEqual(portfolio["diagnostic_center_at_least_two_above_observed"], 1)
        self.assertEqual(self.engine.db.execute(
            "SELECT COUNT(*) FROM weather_ai_agent_actions"
        ).fetchone()[0], 0)
        self.assertEqual(self.engine.db.execute(
            "SELECT COUNT(*) FROM weather_ai_agent_fills"
        ).fetchone()[0], 0)

    def test_frozen_ladder_cost_boundaries_and_idempotency(self):
        event = {
            "event_id": "event-1", "city": "Shanghai", "target_date": "2026-07-23",
            "timezone": "Asia/Shanghai",
        }
        now = datetime(2026, 7, 23, 3, 1, 30, tzinfo=UTC)
        lower_boundary = self.frozen_ladder_markets(center_ask=0.32, center_bid=0.30)
        lower_boundary[1]["yesBestAsk"] = 0.32
        lower_boundary[1]["yesBook"]["asks"][0]["price"] = 0.32
        lower_boundary[3]["yesBestAsk"] = 0.22
        lower_boundary[3]["yesBook"]["asks"][0]["price"] = 0.22
        # 0.32 + 3*0.32 + 0.22 = 1.50, which is excluded.
        self.assertEqual(self.engine.record_frozen_ladder_candidate(
            event, "2026-07-23T03:00:00+00:00", now, lower_boundary, {}, {}
        ), 1)
        row = self.engine.db.execute(
            "SELECT eligibility_status,rejection_reasons_json FROM weather_ladder_frozen_candidates"
        ).fetchone()
        self.assertEqual(row["eligibility_status"], "REJECTED_SHADOW")
        self.assertIn("COMBINED_COST_NOT_ABOVE_1.50", json.loads(row["rejection_reasons_json"]))
        self.assertEqual(self.engine.record_frozen_ladder_candidate(
            event, "2026-07-23T03:00:00+00:00", now, lower_boundary, {}, {}
        ), 0)

        self.engine.db.execute("DELETE FROM weather_ladder_frozen_candidates")
        upper_boundary = self.frozen_ladder_markets(center_ask=0.4866666666666667, center_bid=0.44)
        self.assertEqual(self.engine.record_frozen_ladder_candidate(
            event, "2026-07-23T03:00:00+00:00", now, upper_boundary, {}, {}
        ), 1)
        row = self.engine.db.execute(
            "SELECT eligibility_status,combined_cost_usdc FROM weather_ladder_frozen_candidates"
        ).fetchone()
        self.assertAlmostEqual(row["combined_cost_usdc"], 2.0)
        self.assertEqual(row["eligibility_status"], "ELIGIBLE_SHADOW")

    def test_frozen_ladder_rejects_gap_spread_depth_and_staleness(self):
        event = {
            "event_id": "event-1", "city": "Shanghai", "target_date": "2026-07-23",
            "timezone": "Asia/Shanghai",
        }
        markets = self.frozen_ladder_markets(
            center_ask=0.50, center_bid=0.12,
            snapshot="2026-07-23T02:40:00+00:00",
        )
        markets[2]["yesBook"]["asks"] = [{"price": 0.50, "size": 2}]
        now = datetime(2026, 7, 23, 3, 1, 30, tzinfo=UTC)
        self.engine.record_frozen_ladder_candidate(
            event, "2026-07-23T03:00:00+00:00", now, markets, {}, {}
        )
        row = self.engine.db.execute(
            "SELECT rejection_reasons_json FROM weather_ladder_frozen_candidates"
        ).fetchone()
        reasons = json.loads(row["rejection_reasons_json"])
        self.assertIn("CENTER_LEAD_BELOW_0.03", reasons)
        self.assertIn("MAX_SPREAD_ABOVE_0.20", reasons)
        self.assertIn("INCOMPLETE_WEIGHTED_VWAP_DEPTH", reasons)
        self.assertIn("MARKET_SNAPSHOT_STALE", reasons)

    def test_frozen_ladder_settlement_uses_center_and_adjacent_weights(self):
        event = {
            "event_id": "event-1", "city": "Shanghai", "target_date": "2026-07-23",
            "timezone": "Asia/Shanghai",
        }
        now = datetime(2026, 7, 23, 3, 1, 30, tzinfo=UTC)
        self.engine.record_frozen_ladder_candidate(
            event, "2026-07-23T03:00:00+00:00", now,
            self.frozen_ladder_markets(), {}, {},
        )
        self.assertEqual(self.engine.record_frozen_ladder_portfolio_selection(
            "2026-07-23T03:00:00+00:00"
        ), 1)
        self.engine.db.execute(
            "UPDATE events SET resolved_at_utc=?,winning_range=? WHERE event_id='event-1'",
            ("2026-07-23T12:00:00+00:00", "36 C"),
        )
        self.engine.db.commit()
        self.assertEqual(self.engine.refresh_frozen_ladder_candidates(), 1)
        row = self.engine.db.execute(
            "SELECT payout_usdc,package_hit,hypothetical_pnl_usdc FROM weather_ladder_frozen_candidates"
        ).fetchone()
        self.assertAlmostEqual(row["payout_usdc"], 3.0)
        self.assertEqual(row["package_hit"], 1)
        expected_fee = 1.0 * WEATHER_TAKER_FEE_RATE * 0.32 * (1.0 - 0.32)
        expected_fee += 3.0 * WEATHER_TAKER_FEE_RATE * 0.42 * (1.0 - 0.42)
        expected_fee += 1.0 * WEATHER_TAKER_FEE_RATE * 0.22 * (1.0 - 0.22)
        self.assertAlmostEqual(row["hypothetical_pnl_usdc"], 3.0 - 1.80 - expected_fee)
        variants = self.engine.db.execute(
            "SELECT center_weight,payout_usdc,hypothetical_pnl_usdc FROM weather_ladder_frozen_variants ORDER BY center_weight"
        ).fetchall()
        self.assertEqual(len(variants), 4)
        self.assertAlmostEqual(variants[0]["payout_usdc"], 3.0)
        self.assertAlmostEqual(variants[1]["payout_usdc"], 4.0)
        variant_cost = 0.5 * 0.32 + 4 * 0.42 + 0.5 * 0.22
        variant_fee = sum(
            shares * WEATHER_TAKER_FEE_RATE * price * (1.0 - price)
            for shares, price in ((0.5, 0.32), (4.0, 0.42), (0.5, 0.22))
        )
        self.assertAlmostEqual(variants[1]["hypothetical_pnl_usdc"], 4.0 - variant_cost - variant_fee)
        self.assertAlmostEqual(variants[2]["payout_usdc"], 15.0)
        self.assertAlmostEqual(variants[3]["payout_usdc"], 20.0)
        portfolio = self.engine.db.execute(
            """
            SELECT payout_usdc,hypothetical_pnl_usdc
            FROM weather_ladder_frozen_portfolio_selections
            WHERE portfolio_version='ladder_portfolio_v3_1100_5_20_5_lowest_center_lead'
            """
        ).fetchone()
        self.assertAlmostEqual(portfolio["payout_usdc"], 20.0)
        self.assertIsNotNone(portfolio["hypothetical_pnl_usdc"])

    def test_frozen_ladder_records_missing_market_as_rejected_denominator(self):
        event = {
            "event_id": "event-1", "city": "Shanghai", "target_date": "2026-07-23",
            "timezone": "Asia/Shanghai",
        }
        now = datetime(2026, 7, 23, 3, 1, 30, tzinfo=UTC)
        self.assertEqual(self.engine.record_frozen_ladder_candidate(
            event, "2026-07-23T03:00:00+00:00", now, [], None, None
        ), 1)
        row = self.engine.db.execute(
            "SELECT eligibility_status,rejection_reasons_json FROM weather_ladder_frozen_candidates"
        ).fetchone()
        self.assertEqual(row["eligibility_status"], "REJECTED_SHADOW")
        reasons = json.loads(row["rejection_reasons_json"])
        self.assertIn("INSUFFICIENT_EXACT_BUCKET_MIDPOINTS", reasons)
        self.assertIn("MISSING_MARKET_SNAPSHOT_TIME", reasons)
        self.assertEqual(self.engine.record_frozen_ladder_portfolio_selection(
            "2026-07-23T03:00:00+00:00"
        ), 1)
        portfolio = self.engine.db.execute(
            "SELECT selection_status,eligible_candidates,hypothetical_pnl_usdc FROM weather_ladder_frozen_portfolio_selections"
        ).fetchall()
        self.assertEqual(len(portfolio), 1)
        self.assertTrue(all(row["selection_status"] == "NO_ELIGIBLE_SHADOW" for row in portfolio))
        self.assertTrue(all(row["eligible_candidates"] == 0 for row in portfolio))
        self.assertTrue(all(row["hypothetical_pnl_usdc"] == 0.0 for row in portfolio))

    def test_v4_1_records_every_eligible_city_without_a_daily_cap(self):
        now = datetime(2026, 8, 7, 3, 1, 30, tzinfo=UTC)
        first = {
            "event_id": "event-1", "city": "Shanghai", "target_date": "2026-08-07",
            "timezone": "Asia/Shanghai",
        }
        second = {
            "event_id": "event-2", "city": "Beijing", "target_date": "2026-08-07",
            "timezone": "Asia/Shanghai",
        }
        first_markets = self.frozen_ladder_markets(snapshot="2026-08-07T03:00:00+00:00")
        second_markets = self.frozen_ladder_markets(
            center_ask=0.37, center_bid=0.35, snapshot="2026-08-07T03:00:00+00:00",
        )
        second_markets[3]["yesBestBid"] = 0.05
        second_markets[3]["yesBook"]["bids"][0]["price"] = 0.05
        for market in second_markets:
            market["marketId"] = f"second-{market['marketId']}"
        self.engine.record_frozen_ladder_candidate(
            first, "2026-08-07T03:00:00+00:00", now, first_markets, {}, {}
        )
        self.engine.record_frozen_ladder_candidate(
            second, "2026-08-07T03:00:00+00:00", now, second_markets, {}, {}
        )
        self.assertEqual(self.engine.record_frozen_ladder_portfolio_selection(
            "2026-08-07T03:00:00+00:00"
        ), 3)
        selected = self.engine.db.execute(
            """
            SELECT event_id,city,eligible_candidates,center_lead
            FROM weather_ladder_frozen_portfolio_selections
            WHERE portfolio_version='ladder_portfolio_v3_1100_5_20_5_lowest_center_lead'
            """
        ).fetchone()
        self.assertEqual(selected["event_id"], "event-2")
        self.assertEqual(selected["city"], "Beijing")
        self.assertEqual(selected["eligible_candidates"], 2)
        self.assertAlmostEqual(selected["center_lead"], 0.06)
        v4 = self.engine.db.execute(
            """
            SELECT event_id,city,eligible_candidates,selector,max_events_per_date
            FROM weather_ladder_frozen_portfolio_selections
            WHERE portfolio_version='ladder_portfolio_v4_1_1100_5_15_5_all_eligible'
            ORDER BY city
            """
        ).fetchall()
        self.assertEqual([row["event_id"] for row in v4], ["event-2", "event-1"])
        self.assertTrue(all(row["eligible_candidates"] == 2 for row in v4))
        self.assertTrue(all(row["selector"] == "all_eligible_by_max_spread_then_city" for row in v4))
        self.assertTrue(all(row["max_events_per_date"] == 0 for row in v4))
        self.assertEqual(self.engine.record_frozen_ladder_portfolio_selection(
            "2026-08-07T03:00:00+00:00"
        ), 0)
        self.assertEqual(self.engine.record_frozen_ladder_portfolio_selection(
            "2026-08-07T03:00:00+00:00"
        ), 0)

    def test_startup_recovers_interrupted_run_rows(self):
        self.engine.db.execute(
            "INSERT INTO weather_ai_agent_runs(started_at_utc,status) VALUES(?,'running')",
            ("2026-07-23T01:00:00+00:00",),
        )
        self.assertEqual(self.engine._recover_interrupted_runs(), 1)
        row = self.engine.db.execute(
            "SELECT status,completed_at_utc,error FROM weather_ai_agent_runs"
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIsNotNone(row["completed_at_utc"])
        self.assertIn("interrupted", row["error"])

    def test_completed_metar_cycle_is_idempotent(self):
        now = datetime(2026, 7, 23, 2, 0, tzinfo=UTC)
        due = self.engine.due_events(now)
        self.assertEqual(len(due), 1)
        observation = due[0]["metar_trigger"]["observation_time_utc"]
        self.engine.persist_response(1, [context(observation)], response(observation, [action("observe")]))
        self.engine.db.commit()
        self.assertEqual(self.engine.due_events(now), [])

    def test_scheduled_review_checkpoint_is_deduplicated_and_marked_complete(self):
        self.engine.config.update({
            "scheduledReviewsEnabled": True,
            "scheduledReviewHours": [10],
            "scheduledPositionReviewHour": None,
        })
        now = datetime(2026, 7, 23, 2, 10, tzinfo=UTC)
        due = self.engine.scheduled_review_events(now)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["decision_trigger"]["type"], "scheduled_review")
        self.assertEqual(due[0]["decision_trigger"]["sourceSlotUtc"], "2026-07-23T02:00:00+00:00")
        self.assertEqual(due[0]["decision_trigger"]["analysisAsOfUtc"], "2026-07-23T02:10:00+00:00")
        self.engine.mark_scheduled_review_completed([{
            "event": {"event_id": "event-1"},
            "trigger": {"type": "scheduled_review", "decisionTriggerTimeUtc": "2026-07-23T02:00:00+00:00"},
        }])
        self.engine.db.commit()
        self.assertEqual(self.engine.scheduled_review_events(now), [])

    def test_no_only_half_hour_schedule_starts_at_seven(self):
        self.engine.config.update({
            "scheduledReviewsEnabled": True,
            "scheduledReviewIntervalMinutes": 30,
            "scheduledReviewStartLocalMinutes": 420,
            "scheduledReviewEndLocalMinutes": 1140,
        })
        due = self.engine.scheduled_review_events(datetime(2026, 7, 23, 0, 35, tzinfo=UTC))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["decision_trigger"]["sourceSlotUtc"], "2026-07-23T00:30:00+00:00")
        slots = self.engine.db.execute(
            "SELECT review_slot_utc FROM weather_ai_agent_scheduled_reviews ORDER BY review_slot_utc"
        ).fetchall()
        self.assertEqual([row[0] for row in slots], [
            "2026-07-22T23:00:00+00:00",
            "2026-07-22T23:30:00+00:00",
            "2026-07-23T00:00:00+00:00",
            "2026-07-23T00:30:00+00:00",
        ])

    def test_no_only_mode_accepts_base_five_share_no_research_buys(self):
        self.engine.config.update({
            "noOnlyPaperMode": True,
            "shares": 5,
            "strongNoShares": 10,
            "maxSharesPerAction": 10,
            "maxSharesPerMarket": 10,
        })
        observed = "2026-07-23T01:30:00+00:00"
        no_action = action("buy", "market-1", 5, "NO")
        no_action["entryType"] = "NO_CEILING"
        no_response = response(observed, [no_action])
        no_response["cycles"][0]["marketDecisionMode"] = "NO_RESEARCH"
        self.engine.persist_response(1, [context(observed)], no_response)
        row = self.engine.db.execute(
            "SELECT executed_action,executed_shares,entry_type FROM weather_ai_agent_actions"
        ).fetchone()
        self.assertEqual((row["executed_action"], row["executed_shares"], row["entry_type"]), (
            "buy", 5, "NO_CEILING"
        ))

        later = "2026-07-23T02:00:00+00:00"
        invalid = action("buy", "market-2", 4, "NO")
        invalid["entryType"] = "NO_MARKET_TAIL_REJECTION"
        invalid_response = response(later, [invalid])
        invalid_response["cycles"][0]["marketDecisionMode"] = "NO_RESEARCH"
        with self.assertRaisesRegex(RuntimeError, "BASE sizing must request 5 shares"):
            self.engine.persist_response(2, [context(later)], invalid_response)

    def test_no_only_base_allows_uncertain_overshoot_when_price_edge_is_clear(self):
        self.engine.config["noOnlyPaperMode"] = True
        observed = "2026-07-23T01:30:00+00:00"
        cheap_no = {
            "asks": [{"price": 0.12, "size": 20}],
            "bids": [{"price": 0.10, "size": 20}],
        }
        self.engine.persist_response(
            1, [context(observed, cheap_no)], uncertain_overshoot_response(observed)
        )
        row = self.engine.db.execute(
            "SELECT executed_action,executed_shares,sizing_tier FROM weather_ai_agent_actions"
        ).fetchone()
        self.assertEqual(tuple(row), ("buy", 5, "BASE"))

    def test_no_only_uncertain_overshoot_rejects_an_insufficient_price_edge(self):
        self.engine.config["noOnlyPaperMode"] = True
        observed = "2026-07-23T01:30:00+00:00"
        marginal_no = {
            "asks": [{"price": 0.45, "size": 20}],
            "bids": [{"price": 0.43, "size": 20}],
        }
        decision = uncertain_overshoot_response(observed)
        decision["cycles"][0]["actions"][0]["marketImpliedProbability"] = 0.45
        self.engine.persist_response(1, [context(observed, marginal_no)], decision)
        row = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions"
        ).fetchone()
        self.assertEqual(row["executed_action"], "rejected")
        self.assertIn("coarse-band safety cap", row["rejection_reason"])

    def test_no_only_buy_rejects_yes_price_masquerading_as_no_probability(self):
        self.engine.config["noOnlyPaperMode"] = True
        observed = "2026-07-23T01:30:00+00:00"
        cheap_no = {
            "asks": [{"price": 0.27, "size": 20}],
            "bids": [{"price": 0.17, "size": 20}],
        }
        decision = uncertain_overshoot_response(observed)
        decision["cycles"][0]["actions"][0]["marketImpliedProbability"] = 0.78
        with self.assertRaisesRegex(
            RuntimeError, "marketImpliedProbability must match the executable side price"
        ):
            self.engine.persist_response(1, [context(observed, cheap_no)], decision)
        self.assertIsNone(
            self.engine.db.execute("SELECT 1 FROM weather_ai_agent_actions").fetchone()
        )

    def test_decision_context_expires_after_ten_minutes(self):
        self.engine.config["decisionContextMaxAgeMinutes"] = 10
        analysis_context = context("2026-07-23T01:30:00+00:00")
        for market in analysis_context["markets"]:
            market["snapshotUtc"] = "2026-07-23T01:30:00+00:00"
        self.assertIsNone(self.engine._decision_context_expiry_reason(
            [analysis_context], datetime(2026, 7, 23, 1, 40, tzinfo=UTC)
        ))
        reason = self.engine._decision_context_expiry_reason(
            [analysis_context], datetime(2026, 7, 23, 1, 40, 1, tzinfo=UTC)
        )
        self.assertIn("decision context expired", reason)
        self.assertIn("age=10.0m", reason)

    def test_expired_ai_response_writes_no_cycle_or_action(self):
        observation = "2026-07-23T01:30:00+00:00"
        event = {"event_id": "event-1", "metar_trigger": {"observation_time_utc": observation}}
        with (
            patch.object(self.engine, "maybe_send_daily_report", return_value={"status": "not_due"}),
            patch.object(self.engine, "due_events", return_value=[event]),
            patch.object(self.engine, "build_context", return_value=context(observation)),
            patch.object(
                self.engine, "call_decision_ai",
                return_value=response(observation, [action("observe")]),
            ),
            patch.object(
                self.engine, "_decision_context_expiry_reason",
                return_value="decision context expired; latest data reanalysis required",
            ),
            patch.object(self.engine, "pending_lesson_inputs", return_value=[]),
        ):
            result = self.engine.run_once()
        self.assertEqual(result["cycles_written"], 0)
        self.assertEqual(result["actions_written"], 0)
        self.assertIn("decision context expired", result["decision_errors"][0])
        self.assertIsNone(self.engine.db.execute("SELECT 1 FROM weather_ai_agent_cycles").fetchone())
        self.assertIsNone(self.engine.db.execute("SELECT 1 FROM weather_ai_agent_actions").fetchone())

    def test_no_only_strong_allows_ten_shares_for_extreme_price_dislocation(self):
        self.engine.config["noOnlyPaperMode"] = True
        observed = "2026-07-23T01:30:00+00:00"
        cheap_no = {
            "asks": [{"price": 0.12, "size": 20}],
            "bids": [{"price": 0.10, "size": 20}],
        }
        self.engine.persist_response(
            1,
            [context(observed, cheap_no)],
            uncertain_overshoot_response(observed, shares=10, sizing_tier="STRONG"),
        )
        row = self.engine.db.execute(
            "SELECT executed_action,executed_shares,sizing_tier FROM weather_ai_agent_actions"
        ).fetchone()
        self.assertEqual(tuple(row), ("buy", 10, "STRONG"))

    def test_no_only_strong_can_upgrade_base_to_ten_with_new_evidence(self):
        self.engine.config["noOnlyPaperMode"] = True
        cheap_no = {
            "asks": [{"price": 0.12, "size": 20}],
            "bids": [{"price": 0.10, "size": 20}],
        }
        first = "2026-07-23T01:30:00+00:00"
        self.engine.persist_response(
            1, [context(first, cheap_no)], uncertain_overshoot_response(first)
        )
        second = "2026-07-23T02:00:00+00:00"
        self.engine.persist_response(
            2,
            [context(second, cheap_no)],
            uncertain_overshoot_response(second, shares=5, sizing_tier="STRONG"),
        )
        position = self.engine.db.execute(
            "SELECT shares FROM weather_ai_agent_positions WHERE market_id='market-1'"
        ).fetchone()
        rows = self.engine.db.execute(
            "SELECT executed_shares,sizing_tier FROM weather_ai_agent_actions ORDER BY action_id"
        ).fetchall()
        self.assertEqual(position["shares"], 10)
        self.assertEqual([tuple(row) for row in rows], [(5, "BASE"), (5, "STRONG")])

    def test_no_only_strong_upgrade_requires_new_observable_evidence(self):
        self.engine.config["noOnlyPaperMode"] = True
        cheap_no = {
            "asks": [{"price": 0.12, "size": 20}],
            "bids": [{"price": 0.10, "size": 20}],
        }
        first = "2026-07-23T01:30:00+00:00"
        self.engine.persist_response(
            1, [context(first, cheap_no)], uncertain_overshoot_response(first)
        )
        second = "2026-07-23T02:00:00+00:00"
        decision = uncertain_overshoot_response(second, shares=5, sizing_tier="STRONG")
        decision["cycles"][0]["actions"][0]["newEvidenceSincePrior"] = []
        with self.assertRaisesRegex(RuntimeError, "new observable evidence"):
            self.engine.persist_response(2, [context(second, cheap_no)], decision)

    def test_position_review_window_disables_new_entries(self):
        review_context = {
            "event": {"timezone": "Asia/Shanghai"},
            "trigger": {"slotUtc": "2026-07-23T10:00:00+00:00", "positionOnly": True},
        }
        window = self.engine.decision_window(review_context, datetime(2026, 7, 23, 10, 0, tzinfo=UTC))
        self.assertTrue(window["ordersAllowed"])
        self.assertFalse(window["newEntriesAllowed"])

    def test_run_once_releases_sqlite_writer_lock_before_ai_call(self):
        observation = "2026-07-23T01:30:00+00:00"
        event = {"event_id": "event-1", "metar_trigger": {"observation_time_utc": observation}}

        def ai_response(_contexts):
            self.assertFalse(self.engine.db.in_transaction)
            return response(observation, [action("observe")])

        with (
            patch.object(self.engine, "maybe_send_daily_report", return_value={"status": "not_due"}),
            patch.object(self.engine, "due_events", return_value=[event]),
            patch.object(self.engine, "build_context", return_value=context(observation)),
            patch.object(self.engine, "call_decision_ai", side_effect=ai_response),
            patch.object(self.engine, "pending_lesson_inputs", return_value=[]),
        ):
            result = self.engine.run_once()
        self.assertEqual(result["cycles_written"], 1)

    def test_serial_decisions_build_and_persist_each_context_just_in_time(self):
        events = [{"event_id": "event-1"}, {"event_id": "event-2"}]
        trace: list[str] = []

        def build(event):
            trace.append(f"build:{event['event_id']}")
            return {
                "event": {"event_id": event["event_id"], "city": event["event_id"]},
                "trigger": {}, "markets": [],
            }

        def decide(contexts):
            trace.append(f"decide:{contexts[0]['event']['event_id']}")
            return {}

        def persist(_run_id, contexts, _response):
            trace.append(f"persist:{contexts[0]['event']['event_id']}")
            return 1, 0, 0

        with (
            patch.object(self.engine, "maybe_send_daily_report", return_value={"status": "not_due"}),
            patch.object(self.engine, "due_events", return_value=events),
            patch.object(self.engine, "build_context", side_effect=build),
            patch.object(self.engine, "call_decision_ai", side_effect=decide),
            patch.object(self.engine, "persist_response", side_effect=persist),
            patch.object(self.engine, "_decision_context_expiry_reason", return_value=None),
            patch.object(self.engine, "mark_scheduled_reviews_covered_by_interrupts"),
            patch.object(self.engine, "mark_scheduled_review_completed"),
            patch.object(self.engine, "pending_lesson_inputs", return_value=[]),
        ):
            result = self.engine.run_once()
        self.assertEqual(result["cycles_written"], 2)
        self.assertEqual(trace, [
            "build:event-1", "decide:event-1", "persist:event-1",
            "build:event-2", "decide:event-2", "persist:event-2",
        ])

    def test_run_once_stops_remaining_ai_work_after_circuit_opens(self):
        events = [{"event_id": "event-1"}, {"event_id": "event-2"}]
        lesson_input = {"event": {"event_id": "settled-event"}}

        def fail_and_open(_contexts):
            self.engine.open_ai_circuit(datetime.now(UTC), "provider unavailable")
            raise RuntimeError("provider unavailable")

        with (
            patch.object(self.engine, "maybe_send_daily_report", return_value={"status": "not_due"}),
            patch.object(self.engine, "due_events", return_value=events),
            patch.object(self.engine, "build_context", return_value={"event": {}, "trigger": {}}) as build,
            patch.object(self.engine, "call_decision_ai", side_effect=fail_and_open) as decide,
            patch.object(self.engine, "pending_lesson_inputs", return_value=[lesson_input]) as pending,
            patch.object(self.engine, "call_lesson_ai") as lesson,
        ):
            result = self.engine.run_once()
        self.assertEqual(len(result["decision_errors"]), 1)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(decide.call_count, 1)
        self.assertEqual(pending.call_count, 1)
        lesson.assert_not_called()

    def test_lesson_api_failure_does_not_discard_or_block_decision_cycle(self):
        observation = "2026-07-23T01:30:00+00:00"
        event = {"event_id": "event-1", "metar_trigger": {"observation_time_utc": observation}}
        lesson_input = {"event": {"event_id": "settled-event"}}
        with (
            patch.object(self.engine, "maybe_send_daily_report", return_value={"status": "not_due"}),
            patch.object(self.engine, "due_events", return_value=[event]),
            patch.object(self.engine, "build_context", return_value=context(observation)),
            patch.object(
                self.engine,
                "call_decision_ai",
                return_value=response(observation, [action("observe")]),
            ),
            patch.object(self.engine, "pending_lesson_inputs", return_value=[lesson_input]),
            patch.object(self.engine, "call_lesson_ai", side_effect=RuntimeError("upstream unavailable")),
        ):
            result = self.engine.run_once()
        run = self.engine.db.execute(
            "SELECT status,cycles_written,error FROM weather_ai_agent_runs WHERE run_id=?",
            (result["run_id"],),
        ).fetchone()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["cycles_written"], 1)
        self.assertIn("upstream unavailable", run["error"])
        self.assertIn("upstream unavailable", result["lesson_error"])
        self.assertFalse(self.engine.lesson_retry_allowed(datetime.now(UTC)))

    def test_ai_circuit_blocks_calls_until_cleared(self):
        now = datetime.now(UTC)
        self.engine.config["aiCircuitBreakMinutes"] = 30
        self.engine.open_ai_circuit(now, "provider unavailable")
        self.assertFalse(self.engine.ai_calls_allowed(now))
        self.engine.clear_ai_circuit()
        self.assertTrue(self.engine.ai_calls_allowed(now))

    def test_disabled_agent_skips_research_and_ai_work(self):
        self.engine.config["enabled"] = False
        with (
            patch.object(self.engine, "maybe_send_daily_report", return_value={"status": "not_due"}),
            patch.object(self.engine, "record_research_snapshots", return_value=3) as snapshots,
            patch.object(self.engine, "refresh_research_snapshots", return_value=1) as resolved,
            patch.object(self.engine, "refresh_ladder_shadow_snapshots", return_value=2) as ladder_resolved,
            patch.object(self.engine, "due_events") as due_events,
            patch.object(self.engine, "pending_lesson_inputs") as lessons,
        ):
            result = self.engine.run_once()
        self.assertEqual(result["message"], "AI disabled; data collection active")
        snapshots.assert_not_called()
        resolved.assert_not_called()
        ladder_resolved.assert_not_called()
        due_events.assert_not_called()
        lessons.assert_not_called()

    def test_trading_window_is_local_0700_to_1900_and_does_not_backfill_early_metar(self):
        self.engine.config["activeLocalStartHour"] = 7
        self.engine.config["activeLocalEndHour"] = 19
        self.assertEqual(self.engine.due_events(datetime(2026, 7, 22, 22, 0, tzinfo=UTC)), [])
        self.assertEqual(len(self.engine.due_events(datetime(2026, 7, 23, 2, 0, tzinfo=UTC))), 1)
        self.assertEqual(self.engine.due_events(datetime(2026, 7, 23, 11, 0, tzinfo=UTC)), [])

        self.engine.db.execute("DELETE FROM weather_observations")
        self.engine.db.execute(
            "INSERT INTO weather_observations VALUES('ZSPD','metar','ok',?,?,?,30,25,70,180,5,'mps','CAVOK',30)",
            (
                "2026-07-22T22:30:00+00:00",
                "2026-07-22T22:30:00+00:00",
                "2026-07-22T22:45:00+00:00",
            ),
        )
        self.engine.db.commit()
        self.assertEqual(self.engine.due_events(datetime(2026, 7, 22, 23, 0, tzinfo=UTC)), [])

    def test_0700_to_1000_is_observation_only_but_1000_allows_orders(self):
        self.engine.config["tradeLocalStartHour"] = 10
        self.engine.config["tradeLocalEndHour"] = 19
        before = "2026-07-23T01:30:00+00:00"  # 09:30 Shanghai
        self.engine.persist_response(
            1, [context(before)], response(before, [action("buy", "market-1", 5, "NO")])
        )
        early = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(early["executed_action"], "observe")
        self.assertIn("observation-only", early["rejection_reason"])
        self.assertIsNone(self.engine.db.execute("SELECT 1 FROM weather_ai_agent_positions").fetchone())

        opening = "2026-07-23T02:00:00+00:00"  # 10:00 Shanghai
        self.engine.persist_response(
            2, [context(opening)], response(opening, [action("buy", "market-1", 5, "NO")])
        )
        allowed = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(allowed["executed_action"], "buy")
        self.assertIsNone(allowed["rejection_reason"])

        next_morning = "2026-07-24T01:30:00+00:00"
        self.engine.persist_response(
            3, [context(next_morning)], response(next_morning, [action("sell", "market-1", 5, "NO")])
        )
        early_exit = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(early_exit["executed_action"], "hold")
        self.assertIn("observation-only", early_exit["rejection_reason"])

    def test_buy_partial_sell_and_settlement_preserve_cost_basis(self):
        first = "2026-07-23T01:30:00+00:00"
        self.engine.persist_response(1, [context(first)], response(first, [action("buy", "market-1", 5, "NO")]))
        position = self.engine.db.execute("SELECT * FROM weather_ai_agent_positions").fetchone()
        self.assertEqual(position["shares"], 5)
        self.assertEqual(position["outcome_side"], "NO")
        self.assertAlmostEqual(position["cost_basis_usdc"], 4.0)

        second = "2026-07-23T02:00:00+00:00"
        self.engine.persist_response(2, [context(second)], response(second, [action("sell", "market-1", 2, "NO")]))
        position = self.engine.db.execute("SELECT * FROM weather_ai_agent_positions").fetchone()
        self.assertEqual(position["shares"], 3)
        self.assertAlmostEqual(position["cost_basis_usdc"], 2.4)
        self.assertAlmostEqual(position["realized_pnl_usdc"], -0.1)

        self.engine.db.execute(
            "INSERT INTO market_resolutions VALUES('market-1','2026-07-24T00:00:00+00:00',1,'NO',1)"
        )
        self.assertEqual(self.engine.settle_positions(), 1)
        position = self.engine.db.execute("SELECT * FROM weather_ai_agent_positions").fetchone()
        self.assertEqual(position["status"], "settled")
        self.assertEqual(position["shares"], 0)
        self.assertAlmostEqual(position["realized_pnl_usdc"], 0.5)

    def test_kernel_enforces_coarse_band_price_cap_and_rejects_stale_data(self):
        expensive = {"asks": [{"price": 0.95, "size": 10}], "bids": [{"price": 0.93, "size": 10}]}
        first = "2026-07-23T01:30:00+00:00"
        expensive_action = action("buy", "market-1", 5, "NO")
        expensive_action["marketImpliedProbability"] = 0.95
        self.engine.persist_response(1, [context(first, expensive)], response(first, [expensive_action]))
        row = self.engine.db.execute("SELECT executed_action,rejection_reason FROM weather_ai_agent_actions").fetchone()
        self.assertEqual(row["executed_action"], "rejected")
        self.assertIn("coarse-band safety cap", row["rejection_reason"])

        second_context = context("2026-07-23T02:00:00+00:00")
        second_context["dataFreshness"]["marketAgeMinutes"] = 41
        second = "2026-07-23T02:00:00+00:00"
        self.engine.persist_response(2, [second_context], response(second, [action("buy", "market-1", 5, "NO")]))
        rows = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions ORDER BY action_id"
        ).fetchall()
        self.assertEqual(rows[-1]["executed_action"], "rejected")
        self.assertIn("stale", rows[-1]["rejection_reason"])

    def test_market_disagreement_buy_requires_strong_multi_source_evidence(self):
        observed = "2026-07-23T01:30:00+00:00"
        weak = action("buy", "market-1", 5, "YES")
        weak["evidenceStrength"] = "moderate"
        weak["disagreementEvidence"] = ["model midpoint differs"]
        self.engine.persist_response(1, [context(observed)], response(observed, [weak]))
        row = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions"
        ).fetchone()
        self.assertEqual(row["executed_action"], "rejected")
        self.assertIn("strong evidence", row["rejection_reason"])

    def test_yes_buy_must_match_clear_leader_classification(self):
        observed = "2026-07-23T01:30:00+00:00"
        inconsistent = action("buy", "market-1", 5, "YES")
        inconsistent["outcomeAssessment"] = "highly_unlikely"
        self.engine.persist_response(1, [context(observed)], response(observed, [inconsistent]))
        row = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions"
        ).fetchone()
        self.assertEqual(row["executed_action"], "rejected")
        self.assertIn("inconsistent", row["rejection_reason"])

    def test_entry_type_cannot_mix_yes_convergence_and_no_exclusion(self):
        observed = "2026-07-23T01:30:00+00:00"
        invalid = action("buy", "market-1", 5, "YES")
        invalid["entryType"] = "NO_EXCLUSION"
        self.engine.persist_response(1, [context(observed)], response(observed, [invalid]))
        row = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions"
        ).fetchone()
        self.assertEqual(row["executed_action"], "rejected")
        self.assertIn("YES_CONVERGENCE", row["rejection_reason"])

    def test_add_requires_new_observable_evidence(self):
        first = "2026-07-23T01:30:00+00:00"
        self.engine.persist_response(
            1, [context(first)], response(first, [action("buy", "market-1", 5, "NO")])
        )
        second = "2026-07-23T02:00:00+00:00"
        add = action("buy", "market-1", 5, "NO")
        add["newEvidenceSincePrior"] = []
        self.engine.persist_response(2, [context(second)], response(second, [add]))
        row = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["executed_action"], "rejected")
        self.assertIn("new observable evidence", row["rejection_reason"])

    def test_sell_at_one_cent_is_retained_for_settlement(self):
        first = "2026-07-23T01:30:00+00:00"
        self.engine.persist_response(
            1, [context(first)], response(first, [action("buy", "market-1", 5, "NO")])
        )
        penny_book = {"asks": [{"price": 0.99, "size": 10}], "bids": [{"price": 0.01, "size": 10}]}
        second = "2026-07-23T02:00:00+00:00"
        self.engine.persist_response(
            2, [context(second, penny_book)], response(second, [action("sell", "market-1", 5, "NO")])
        )
        row = self.engine.db.execute(
            "SELECT executed_action,rejection_reason FROM weather_ai_agent_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()
        position = self.engine.db.execute("SELECT shares FROM weather_ai_agent_positions").fetchone()
        self.assertEqual(row["executed_action"], "hold")
        self.assertIn("residual-value floor", row["rejection_reason"])
        self.assertEqual(position["shares"], 5)

    def test_full_position_sell_is_not_limited_by_buy_action_cap(self):
        first = "2026-07-23T01:00:00+00:00"
        second = "2026-07-23T01:30:00+00:00"
        third = "2026-07-23T02:00:00+00:00"
        self.engine.persist_response(1, [context(first)], response(first, [action("buy", "market-1", 5, "NO")]))
        self.engine.persist_response(2, [context(second)], response(second, [action("buy", "market-1", 5, "NO")]))
        self.engine.persist_response(3, [context(third)], response(third, [action("sell", "market-1", 10, "NO")]))
        row = self.engine.db.execute(
            "SELECT executed_action,executed_shares,rejection_reason FROM weather_ai_agent_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()
        position = self.engine.db.execute("SELECT shares,status FROM weather_ai_agent_positions").fetchone()
        self.assertEqual(row["executed_action"], "sell")
        self.assertEqual(row["executed_shares"], 10)
        self.assertIsNone(row["rejection_reason"])
        self.assertEqual(position["shares"], 0)
        self.assertEqual(position["status"], "closed")

    def test_future_scenarios_require_one_primary_path(self):
        observed = "2026-07-23T01:30:00+00:00"
        invalid = response(observed, [action("observe")])
        invalid["cycles"][0]["futureScenarios"][0]["plausibility"] = "plausible"
        with self.assertRaisesRegex(RuntimeError, "exactly one primary"):
            self.engine.persist_response(1, [context(observed)], invalid)

    def test_settlement_distribution_must_cover_every_market_bucket(self):
        observed = "2026-07-23T01:30:00+00:00"
        invalid = response(observed, [action("observe")])
        invalid["cycles"][0]["settlementDistribution"] = [
            {"outcomeRange": "35 C", "rank": 1, "classification": "clear_leader", "probabilityBand": "30_50", "reason": "test"},
            {"outcomeRange": "36 C", "rank": 2, "classification": "plausible", "probabilityBand": "15_30", "reason": "test"},
        ]
        with self.assertRaisesRegex(RuntimeError, "cover every market outcome"):
            self.engine.persist_response(1, [context(observed)], invalid)

    def test_yes_position_settles_against_yes_outcome(self):
        observed = "2026-07-23T01:30:00+00:00"
        self.engine.persist_response(
            1, [context(observed)], response(observed, [action("buy", "market-1", 5, "YES")])
        )
        self.engine.db.execute(
            "INSERT INTO market_resolutions VALUES('market-1','2026-07-24T00:00:00+00:00',1,'YES',0)"
        )
        self.assertEqual(self.engine.settle_positions(), 1)
        position = self.engine.db.execute("SELECT * FROM weather_ai_agent_positions").fetchone()
        self.assertEqual(position["outcome_side"], "YES")
        self.assertEqual(position["final_outcome"], "YES")
        self.assertAlmostEqual(position["realized_pnl_usdc"], 4.0)

    def test_switching_direction_requires_sell_then_buy(self):
        first = "2026-07-23T01:30:00+00:00"
        self.engine.persist_response(
            1, [context(first)], response(first, [action("buy", "market-1", 5, "NO")])
        )
        second = "2026-07-23T02:00:00+00:00"
        self.engine.persist_response(
            2, [context(second)], response(second, [action("buy", "market-1", 5, "YES")])
        )
        rejected = self.engine.db.execute(
            "SELECT rejection_reason FROM weather_ai_agent_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()
        self.assertIn("close the opposite", rejected["rejection_reason"])

        third = "2026-07-23T02:30:00+00:00"
        self.engine.persist_response(
            3,
            [context(third)],
            response(third, [
                action("sell", "market-1", 5, "NO"),
                action("buy", "market-1", 5, "YES"),
            ]),
        )
        position = self.engine.db.execute("SELECT * FROM weather_ai_agent_positions").fetchone()
        self.assertEqual(position["outcome_side"], "YES")
        self.assertEqual(position["shares"], 5)

    def test_minimum_buy_and_available_cash_are_enforced(self):
        self.engine.db.execute(
            "UPDATE weather_ai_agent_meta SET value='4' WHERE key=?",
            (f"initial_cash_usdc:{self.engine.strategy_name}",),
        )
        first = "2026-07-23T01:00:00+00:00"
        self.engine.persist_response(
            1, [context(first)], response(first, [action("buy", "market-1", 4, "NO")])
        )
        rejected = self.engine.db.execute(
            "SELECT rejection_reason FROM weather_ai_agent_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()
        self.assertIn("at least 5", rejected["rejection_reason"])

        second = "2026-07-23T01:30:00+00:00"
        self.engine.persist_response(
            2, [context(second)], response(second, [action("buy", "market-1", 5, "NO")])
        )
        self.assertAlmostEqual(self.engine.account_state()["availableCashUsdc"], 0.0)

        third = "2026-07-23T02:00:00+00:00"
        self.engine.persist_response(
            3, [context(third)], response(third, [action("buy", "market-1", 5, "NO")])
        )
        rejected = self.engine.db.execute(
            "SELECT rejection_reason FROM weather_ai_agent_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()
        self.assertIn("insufficient available cash", rejected["rejection_reason"])

    def test_autonomous_mode_enforces_five_usdc_cash_reserve(self):
        self.engine.config.update({
            "autonomousMode": True,
            "minCashReserveUsdc": 5,
            "maxSharesPerAction": 100,
            "maxSharesPerMarket": 100,
            "maxOpenNotionalPerCity": 10000,
            "maxOpenNotionalTotal": 10000,
        })
        rich_book = {
            "asks": [{"price": 0.80, "size": 100}],
            "bids": [{"price": 0.75, "size": 100}],
        }
        observed = "2026-07-23T01:30:00+00:00"
        autonomous_action = action("buy", "market-1", 19, "NO")
        autonomous_action["entryType"] = "AI_DISCRETION"
        autonomous_response = response(observed, [autonomous_action])
        autonomous_response["cycles"][0]["marketDecisionMode"] = "AUTONOMOUS"
        self.engine.persist_response(
            1, [context(observed, rich_book)], autonomous_response
        )
        rejected = self.engine.db.execute(
            "SELECT rejection_reason FROM weather_ai_agent_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()
        self.assertIn("minimum cash reserve", rejected["rejection_reason"])
        self.assertEqual(self.engine.account_state()["availableCashUsdc"], 20)

        later = "2026-07-23T02:00:00+00:00"
        allowed_action = action("buy", "market-1", 18, "NO")
        allowed_action["entryType"] = "AI_DISCRETION"
        allowed_response = response(later, [allowed_action])
        allowed_response["cycles"][0]["marketDecisionMode"] = "AUTONOMOUS"
        self.engine.persist_response(2, [context(later, rich_book)], allowed_response)
        self.assertAlmostEqual(self.engine.account_state()["availableCashUsdc"], 5.6)

    def test_half_hour_scheduler_selects_latest_due_slot(self):
        self.engine.config.update({
            "scheduledReviewIntervalMinutes": 30,
            "scheduledReviewStartLocalMinutes": 600,
            "scheduledReviewEndLocalMinutes": 1140,
        })
        self.engine.db.execute(
            "INSERT OR REPLACE INTO stations VALUES('ZSPD','Pudong','Shanghai',31.1,121.8,'Asia/Shanghai')"
        )
        self.engine.db.execute(
            "INSERT OR REPLACE INTO events(event_id,city,target_date,station_id,station_name,last_seen_utc) VALUES(?,?,?,?,?,?)",
            ("event-1", "Shanghai", "2026-07-23", "ZSPD", "Pudong", "2026-07-23T03:00:00+00:00"),
        )
        self.engine.db.execute(
            """INSERT INTO weather_observations(
                station_id,source,status,slot_utc,observation_time_utc,fetched_at_utc,
                temperature_c,observed_daily_max_c
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("ZSPD", "metar", "ok", "2026-07-23T03:00:00+00:00", "2026-07-23T03:00:00+00:00", "2026-07-23T03:01:00+00:00", 32, 32),
        )
        due = self.engine.scheduled_review_events(datetime(2026, 7, 23, 3, 5, tzinfo=UTC))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["decision_trigger"]["sourceSlotUtc"], "2026-07-23T03:00:00+00:00")
        slots = self.engine.db.execute(
            "SELECT review_slot_utc,status FROM weather_ai_agent_scheduled_reviews ORDER BY review_slot_utc"
        ).fetchall()
        self.assertEqual([row["status"] for row in slots], ["covered", "covered", "pending"])

    def test_newer_retry_wait_prevents_replaying_older_checkpoint(self):
        self.engine.config.update({
            "scheduledReviewIntervalMinutes": 30,
            "scheduledReviewStartLocalMinutes": 600,
            "scheduledReviewEndLocalMinutes": 1140,
        })
        self.engine.db.execute(
            "INSERT OR REPLACE INTO stations VALUES('ZSPD','Pudong','Shanghai',31.1,121.8,'Asia/Shanghai')"
        )
        self.engine.db.execute(
            "INSERT OR REPLACE INTO events(event_id,city,target_date,station_id,station_name,last_seen_utc) VALUES(?,?,?,?,?,?)",
            ("event-1", "Shanghai", "2026-07-23", "ZSPD", "Pudong", "2026-07-23T03:00:00+00:00"),
        )
        self.engine.db.execute(
            """INSERT INTO weather_observations(
                station_id,source,status,slot_utc,observation_time_utc,fetched_at_utc,
                temperature_c,observed_daily_max_c
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("ZSPD", "metar", "ok", "2026-07-23T03:00:00+00:00", "2026-07-23T03:00:00+00:00", "2026-07-23T03:01:00+00:00", 32, 32),
        )
        self.engine.scheduled_review_events(datetime(2026, 7, 23, 3, 5, tzinfo=UTC))
        self.engine.db.execute(
            """UPDATE weather_ai_agent_scheduled_reviews
               SET status='retry_wait',retry_after_utc='2026-07-23T03:20:00+00:00'
               WHERE review_slot_utc='2026-07-23T03:00:00+00:00'"""
        )
        self.engine.db.execute(
            """UPDATE weather_ai_agent_scheduled_reviews
               SET status='retry_wait',retry_after_utc='2026-07-23T03:00:00+00:00'
               WHERE review_slot_utc='2026-07-23T02:30:00+00:00'"""
        )
        self.engine.db.commit()

        self.assertEqual(
            self.engine.scheduled_review_events(datetime(2026, 7, 23, 3, 10, tzinfo=UTC)),
            [],
        )
        rows = self.engine.db.execute(
            "SELECT review_slot_utc,status FROM weather_ai_agent_scheduled_reviews ORDER BY review_slot_utc"
        ).fetchall()
        self.assertEqual([row["status"] for row in rows], ["covered", "covered", "retry_wait"])

    def test_daily_pnl_report_is_sent_once_after_1900_shanghai(self):
        self.engine.config.update({
            "dailyReportTimezone": "Asia/Shanghai",
            "dailyReportHour": 19,
            "dailyReportMinute": 0,
            "dailyReportRetryMinutes": 10,
        })
        now = datetime(2026, 7, 23, 11, 0, tzinfo=UTC)
        with patch.object(self.engine, "_send_feishu_text") as send:
            result = self.engine.maybe_send_daily_report(now)
            duplicate = self.engine.maybe_send_daily_report(now)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(duplicate["status"], "already_sent")
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["initialCashUsdc"], 20)
        self.assertEqual(snapshot["availableCashUsdc"], 20)
        self.assertEqual(snapshot["totalPnlUsdc"], 0)
        self.assertIn("天气 AI Paper 日报", send.call_args.args[0])

    def test_daily_pnl_conservatively_marks_no_bid_position_at_zero(self):
        self.engine.db.execute(
            """
            INSERT INTO weather_ai_agent_positions(
                strategy_name,event_id,market_id,city,outcome_range,outcome_side,shares,
                cost_basis_usdc,realized_pnl_usdc,status,opened_at_utc,updated_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (self.engine.strategy_name, "event-1", "market-1", "Shanghai", "35 C", "YES", 5, 4, 0, "open", "2026-07-23T01:00:00+00:00", "2026-07-23T01:00:00+00:00"),
        )
        self.engine.db.execute(
            "INSERT INTO market_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "2026-07-23T02:00:00+00:00", "market-1", "event-1", None, 0.001, 0.999, None,
                json.dumps({"bids": [], "asks": [{"price": 0.001, "size": 100}]}),
                json.dumps({"bids": [{"price": 0.999, "size": 100}], "asks": []}), 100, 200,
            ),
        )
        snapshot = self.engine.daily_pnl_snapshot(date(2026, 7, 23), ZoneInfo("Asia/Shanghai"))
        self.assertEqual(snapshot["zeroBidPositions"], 1)
        self.assertEqual(snapshot["unpricedPositions"], 0)
        self.assertEqual(snapshot["unrealizedPnlUsdc"], -4)
        self.assertEqual(snapshot["totalPnlUsdc"], -4)
        self.assertEqual(snapshot["equityUsdc"], 16)
        self.assertIn("无买盘按 0 计", self.engine.daily_report_text(snapshot))

    def test_failed_daily_report_waits_before_retry(self):
        self.engine.config.update({
            "dailyReportTimezone": "Asia/Shanghai",
            "dailyReportHour": 19,
            "dailyReportMinute": 0,
            "dailyReportRetryMinutes": 10,
        })
        now = datetime(2026, 7, 23, 11, 0, tzinfo=UTC)
        with patch.object(self.engine, "_send_feishu_text", side_effect=RuntimeError("network down")):
            with self.assertRaisesRegex(RuntimeError, "network down"):
                self.engine.maybe_send_daily_report(now)
        self.assertEqual(self.engine.maybe_send_daily_report(now)["status"], "retry_wait")

    def test_parse_hermes_json_accepts_plain_and_fenced_objects(self):
        expected = {"generatedAt": "now", "cycles": []}
        self.assertEqual(self.engine._parse_hermes_json(json.dumps(expected)), expected)
        self.assertEqual(
            self.engine._parse_hermes_json("```json\n" + json.dumps(expected) + "\n```"),
            expected,
        )

    def test_parse_hermes_json_rejects_prose_after_object(self):
        with self.assertRaisesRegex(RuntimeError, "one valid JSON object"):
            self.engine._parse_hermes_json('{"cycles": []} extra commentary')
        with self.assertRaisesRegex(RuntimeError, "one valid JSON object"):
            self.engine._parse_hermes_json('Here is the result: {"cycles": []}')

    def test_parse_hermes_json_accepts_unescaped_newline_inside_string(self):
        parsed = self.engine._parse_hermes_json(
            '{"generatedAt":"now","cycles":[],"note":"line one\nline two"}'
        )
        self.assertEqual(parsed["note"], "line one\nline two")

    def test_local_schema_validation_rejects_unknown_fields_and_bad_probability(self):
        schema = json.loads(Path("weather_ai_agent.schema.json").read_text(encoding="utf-8"))
        valid = response("2026-07-23T01:30:00+00:00", [action("observe")])
        validate_json_schema(valid, schema)
        valid["unexpected"] = True
        with self.assertRaisesRegex(RuntimeError, "extra"):
            validate_json_schema(valid, schema)
        del valid["unexpected"]
        valid["cycles"][0]["actions"][0]["marketImpliedProbability"] = 1.2
        with self.assertRaisesRegex(RuntimeError, "above maximum"):
            validate_json_schema(valid, schema)
        valid["cycles"][0]["actions"][0]["marketImpliedProbability"] = float("nan")
        with self.assertRaisesRegex(RuntimeError, "non-finite"):
            validate_json_schema(valid, schema)

    def test_run_ai_uses_isolated_hermes_profile_and_memory_only(self):
        profile_home = "/tmp/test-hermes-profile"
        binary = "/tmp/test-hermes/bin/hermes"
        python = "/tmp/test-hermes/bin/python3"
        self.engine.config.update({
            "aiRuntime": "hermes",
            "hermesProfile": "weathertrader",
            "hermesHome": profile_home,
            "hermesBinary": binary,
            "hermesPython": python,
            "hermesProvider": "custom",
            "hermesModel": "gpt-5.5",
            "hermesBaseUrl": "https://beeapi.ai/v1",
            "aiTimeoutSeconds": 10,
        })
        completed = type("Completed", (), {
            "returncode": 0,
            "stdout": '{"generatedAt":"now","cycles":[]}',
            "stderr": "",
        })()
        with patch("weather_ai_agent.Path.resolve", return_value=Path(binary)), \
                patch("weather_ai_agent.Path.exists", return_value=True), \
                patch("weather_ai_agent.Path.is_dir", return_value=True), \
                patch("weather_ai_agent.subprocess.run", return_value=completed) as run:
            result = self.engine._run_ai(Path("weather_ai_agent.schema.json"), "decide")
        self.assertEqual(result["cycles"], [])
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["env"]["HERMES_HOME"], profile_home)
        self.assertEqual(kwargs["env"]["WEATHER_HERMES_TOOLSETS"], "memory")
        self.assertEqual(kwargs["env"]["WEATHER_HERMES_PROVIDER"], "custom")
        self.assertEqual(kwargs["env"]["WEATHER_HERMES_MODEL"], "gpt-5.5")
        self.assertEqual(kwargs["env"]["CUSTOM_BASE_URL"], "https://beeapi.ai/v1")
        self.assertIn("JSON Schema", kwargs["input"])

    def test_run_ai_falls_back_to_next_configured_model(self):
        profile_home = "/tmp/test-hermes-profile"
        binary = "/tmp/test-hermes/bin/hermes"
        python = "/tmp/test-hermes/bin/python3"
        self.engine.config.update({
            "aiRuntime": "hermes",
            "hermesProfile": "weathertrader",
            "hermesHome": profile_home,
            "hermesBinary": binary,
            "hermesPython": python,
            "hermesProvider": "custom",
            "hermesModel": "gpt-5.6-sol",
            "hermesFallbackModels": ["gpt-5.6-terra"],
            "aiTimeoutSeconds": 10,
        })
        failed = type("Completed", (), {
            "returncode": 0,
            "stdout": "API call failed: HTTP 502",
            "stderr": "",
        })()
        completed = type("Completed", (), {
            "returncode": 0,
            "stdout": '{"generatedAt":"now","cycles":[]}',
            "stderr": "",
        })()
        with patch("weather_ai_agent.Path.resolve", return_value=Path(binary)), \
                patch("weather_ai_agent.Path.exists", return_value=True), \
                patch("weather_ai_agent.Path.is_dir", return_value=True), \
                patch("weather_ai_agent.subprocess.run", side_effect=[failed, completed]) as run:
            result = self.engine._run_ai(Path("weather_ai_agent.schema.json"), "decide")
        self.assertEqual(result["cycles"], [])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].kwargs["env"]["WEATHER_HERMES_MODEL"], "gpt-5.6-sol")
        self.assertEqual(run.call_args_list[1].kwargs["env"]["WEATHER_HERMES_MODEL"], "gpt-5.6-terra")

    def test_run_ai_retries_only_malformed_response_before_opening_circuit(self):
        self.engine.config.update({
            "aiRuntime": "hermes",
            "hermesProfile": "weathertrader",
            "hermesHome": "/tmp/test-hermes-profile",
            "hermesBinary": "/tmp/test-hermes/bin/hermes",
            "hermesPython": "/tmp/test-hermes/bin/python3",
            "hermesProvider": "test-provider",
            "hermesModel": "test-model",
            "hermesFallbackModels": [],
            "aiMalformedResponseRetries": 1,
            "aiTimeoutSeconds": 10,
        })
        malformed = type("Completed", (), {
            "returncode": 0, "stdout": '{"generatedAt":', "stderr": "",
        })()
        completed = type("Completed", (), {
            "returncode": 0,
            "stdout": '{"generatedAt":"now","cycles":[]}',
            "stderr": "",
        })()
        with patch("weather_ai_agent.Path.resolve", return_value=Path("/tmp/test-hermes/bin/hermes")), \
                patch("weather_ai_agent.Path.exists", return_value=True), \
                patch("weather_ai_agent.Path.is_dir", return_value=True), \
                patch("weather_ai_agent.subprocess.run", side_effect=[malformed, completed]) as run:
            result = self.engine._run_ai(Path("weather_ai_agent.schema.json"), "decide")
        self.assertEqual(result["cycles"], [])
        self.assertEqual(run.call_count, 2)
        self.assertIn("上一次响应不是", run.call_args_list[1].kwargs["input"])

    def test_ai_circuit_scopes_do_not_block_each_other(self):
        now = datetime.now(UTC)
        self.engine.config["aiCircuitBreakMinutes"] = 30
        self.engine.open_ai_circuit(now, "city one failed", "strategy:event-1")
        self.assertFalse(self.engine.ai_calls_allowed(now, "strategy:event-1"))
        self.assertTrue(self.engine.ai_calls_allowed(now, "strategy:event-2"))
        self.assertTrue(self.engine.ai_calls_allowed(now))
        self.engine.clear_ai_circuit("strategy:event-1")
        self.assertTrue(self.engine.ai_calls_allowed(now, "strategy:event-1"))

    def test_run_ai_failure_opens_circuit_before_another_subprocess(self):
        self.engine.config.update({
            "aiRuntime": "hermes",
            "hermesProfile": "weathertrader",
            "hermesHome": "/tmp/test-hermes-profile",
            "hermesBinary": "/tmp/test-hermes/bin/hermes",
            "hermesPython": "/tmp/test-hermes/bin/python3",
            "hermesProvider": "test-provider",
            "hermesModel": "test-model",
            "hermesFallbackModels": [],
            "aiCircuitBreakMinutes": 30,
            "aiTimeoutSeconds": 10,
        })
        failed = type("Completed", (), {
            "returncode": 1,
            "stdout": "",
            "stderr": "provider unavailable",
        })()
        with patch("weather_ai_agent.Path.resolve", return_value=Path("/tmp/test-hermes/bin/hermes")), \
                patch("weather_ai_agent.Path.exists", return_value=True), \
                patch("weather_ai_agent.Path.is_dir", return_value=True), \
                patch("weather_ai_agent.subprocess.run", return_value=failed) as run:
            with self.assertRaisesRegex(RuntimeError, "All test-provider routes failed"):
                self.engine._run_ai(Path("weather_ai_agent.schema.json"), "decide")
            self.assertFalse(self.engine.ai_calls_allowed(datetime.now(UTC)))
            with self.assertRaisesRegex(RuntimeError, "AI circuit open until"):
                self.engine._run_ai(Path("weather_ai_agent.schema.json"), "decide again")
        self.assertEqual(run.call_count, 1)

    def test_direct_responses_fallback_extracts_output_text(self):
        body = {
            "status": "completed",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": '{"generatedAt":"now","cycles":[]}'}],
            }],
        }
        response_handle = MagicMock()
        response_handle.__enter__.return_value.read.return_value = json.dumps(body).encode("utf-8")
        with patch("weather_ai_agent.urllib.request.urlopen", return_value=response_handle) as urlopen:
            result = self.engine._run_direct_responses(
                base_url="https://beeapi.ai/v1",
                api_key="secret",
                model="gpt-5.6-sol",
                prompt="decide",
                reasoning_effort="medium",
                timeout_seconds=10,
            )
        self.assertEqual(result, '{"generatedAt":"now","cycles":[]}')
        request = urlopen.call_args.args[0]
        request_body = json.loads(request.data)
        self.assertEqual(request_body["model"], "gpt-5.6-sol")
        self.assertEqual(request_body["reasoning"], {"effort": "medium"})
        self.assertNotIn("secret", request_body)


if __name__ == "__main__":
    unittest.main()
