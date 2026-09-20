import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from research.weather_ai_no_backtest import (
    Portfolio,
    _current_markets,
    _observation_rows,
    ensure_isolated_hermes_profile,
    events_for_date,
    fill_fixed_cash,
    portfolio_packet_for_ai,
    validate_city_analysis,
    validate_ai_response,
)


UTC = timezone.utc


class WeatherAiNoBacktestTests(unittest.TestCase):
    def test_observations_exclude_future_observation_and_late_fetch(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            """CREATE TABLE weather_observations(
                run_id INTEGER,station_id TEXT,sample_local_date TEXT,source TEXT,status TEXT,
                observation_time_utc TEXT,fetched_at_utc TEXT,temperature_c REAL,
                observed_daily_max_c REAL,dewpoint_c REAL,relative_humidity REAL,
                precipitation_mm REAL,cloud_cover_pct REAL,wind_direction_deg REAL,
                wind_speed REAL,wind_speed_unit TEXT,wind_gust REAL,visibility_m REAL,
                pressure_hpa REAL,solar_radiation_wm2 REAL,sky_conditions_json TEXT,metar_type TEXT
            )"""
        )
        rows = [
            (1, "TEST", "2026-08-12", "metar", "ok", "2026-08-11T22:00:00+00:00", "2026-08-11T22:01:00+00:00", 25.0),
            (2, "TEST", "2026-08-12", "metar", "ok", "2026-08-11T22:30:00+00:00", "2026-08-11T23:01:00+00:00", 26.0),
            (3, "TEST", "2026-08-12", "metar", "ok", "2026-08-11T23:30:00+00:00", "2026-08-11T23:31:00+00:00", 27.0),
        ]
        db.executemany(
            """INSERT INTO weather_observations(
                run_id,station_id,sample_local_date,source,status,observation_time_utc,
                fetched_at_utc,temperature_c
            ) VALUES(?,?,?,?,?,?,?,?)""",
            rows,
        )
        event = {"station_id": "TEST", "target_date": "2026-08-12"}
        output = _observation_rows(db, event, datetime(2026, 8, 11, 23, tzinfo=UTC), 20)
        self.assertEqual([row["temperature_c"] for row in output], [25.0])

    def test_current_market_excludes_snapshot_fetched_after_as_of(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute(
            """CREATE TABLE markets(
                market_id TEXT,event_id TEXT,outcome_range TEXT,bucket_low REAL,
                bucket_high REAL,bucket_unit TEXT
            )"""
        )
        db.execute(
            """CREATE TABLE market_snapshots(
                run_id INTEGER,market_id TEXT,event_id TEXT,slot_utc TEXT,fetched_at_utc TEXT,
                yes_best_bid REAL,yes_best_ask REAL,no_best_bid REAL,no_best_ask REAL,
                no_ask_size REAL,no_book_json TEXT,market_liquidity REAL
            )"""
        )
        db.execute("INSERT INTO markets VALUES('m1','e1','35 C',35,35,'C')")
        db.executemany(
            "INSERT INTO market_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (1, "m1", "e1", "2026-08-11T22:30:00+00:00", "2026-08-11T22:31:00+00:00", 0.4, 0.42, 0.57, 0.6, 100, "{}", 1000),
                (2, "m1", "e1", "2026-08-11T22:55:00+00:00", "2026-08-11T23:01:00+00:00", 0.7, 0.72, 0.27, 0.3, 100, "{}", 1000),
            ],
        )
        output = _current_markets(db, ["e1"], datetime(2026, 8, 11, 23, tzinfo=UTC))
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["no_best_ask"], 0.6)

    def test_ai_response_is_free_but_no_only_and_cash_bounded(self):
        as_of = datetime(2026, 8, 11, 23, tzinfo=UTC)
        config = {"stakeUsdc": 3, "marketSnapshotMaxAgeMinutes": 20}
        market = {
            "market_id": "m1", "city": "Beijing", "outcome_range": "35 C",
            "no_best_ask": 0.4, "snapshotAgeMinutes": 5,
        }
        action = {
            "action": "BUY_NO", "marketId": "m1", "city": "Beijing", "outcomeRange": "35 C",
            "estimatedNoWinProbability": 0.7, "reason": "weather evidence",
            "contraryEvidence": "forecast disagreement", "invalidationCondition": "rapid heating",
        }
        response = {
            "asOfUtc": as_of.isoformat(timespec="seconds"),
            "portfolioReasoning": "positive expected value", "actions": [action],
            "nextReviewMinutes": 60, "nextReviewReason": "wait for observation", "stopForDay": False,
        }
        validate_ai_response(response, as_of, {"m1": market}, 20, config)
        response["actions"][0]["action"] = "BUY_YES"
        with self.assertRaisesRegex(RuntimeError, "only BUY_NO"):
            validate_ai_response(response, as_of, {"m1": market}, 20, config)

    def test_ai_cannot_spend_more_fixed_orders_than_cash(self):
        as_of = datetime(2026, 8, 11, 23, tzinfo=UTC)
        config = {"stakeUsdc": 3, "marketSnapshotMaxAgeMinutes": 20}
        markets = {}
        actions = []
        for index in range(2):
            market_id = f"m{index}"
            markets[market_id] = {
                "market_id": market_id, "city": "Beijing", "outcome_range": f"{index} C",
                "no_best_ask": 0.4, "snapshotAgeMinutes": 5,
            }
            actions.append({
                "action": "BUY_NO", "marketId": market_id, "city": "Beijing", "outcomeRange": f"{index} C",
                "estimatedNoWinProbability": 0.7, "reason": "evidence",
                "contraryEvidence": "risk", "invalidationCondition": "change",
            })
        response = {
            "asOfUtc": as_of.isoformat(timespec="seconds"), "portfolioReasoning": "test",
            "actions": actions, "nextReviewMinutes": 60, "nextReviewReason": "test", "stopForDay": False,
        }
        with self.assertRaisesRegex(RuntimeError, "available cash"):
            validate_ai_response(response, as_of, markets, 5.99, config)

    def test_fixed_cash_includes_weather_fee(self):
        market = {
            "no_book_json": '{"asks":[{"price":0.5,"size":100}]}',
            "no_best_ask": 0.5,
            "no_ask_size": 100,
        }
        result = fill_fixed_cash(market, 3.0, 0.05)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["cashDebitedUsdc"], 3.0, places=7)
        self.assertAlmostEqual(result["shares"], 3.0 / 0.5125, places=7)

    def test_portfolio_settles_no_win_and_loss(self):
        config = {"stakeUsdc": 3, "weatherTakerFeeRate": 0, "liveMinimumShares": 5}
        market = {
            "event_id": "e1", "market_id": "m1", "city": "Beijing", "outcome_range": "35 C",
            "no_book_json": '{"asks":[{"price":0.5,"size":100}]}', "no_best_ask": 0.5,
            "no_ask_size": 100,
        }
        action = {
            "estimatedNoWinProbability": 0.7, "reason": "evidence",
            "contraryEvidence": "risk", "invalidationCondition": "change",
        }
        portfolio = Portfolio(20)
        settlement = {
            "target_date": "2026-08-12", "resolved_at_utc": "2026-08-12T17:00:00+00:00",
            "winning_market_id": "other", "winning_range": "36 C",
        }
        portfolio.execute(action, market, datetime(2026, 8, 11, 23, tzinfo=UTC), config, settlement)
        portfolio.settle_through(None)
        self.assertAlmostEqual(portfolio.cash, 23.0)
        self.assertTrue(portfolio.settlements[0]["noWon"])

        losing = Portfolio(20)
        settlement["winning_market_id"] = "m1"
        losing.execute(action, market, datetime(2026, 8, 11, 23, tzinfo=UTC), config, settlement)
        losing.settle_through(None)
        self.assertAlmostEqual(losing.cash, 17.0)
        self.assertFalse(losing.settlements[0]["noWon"])

    def test_ai_visible_event_rows_do_not_include_settlement(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE stations(station_id TEXT,timezone TEXT)")
        db.execute(
            """CREATE TABLE events(
                event_id TEXT,city TEXT,target_date TEXT,station_id TEXT,station_name TEXT,
                end_date_utc TEXT,winning_market_id TEXT,winning_range TEXT,resolved_at_utc TEXT
            )"""
        )
        db.execute("INSERT INTO stations VALUES('TEST','Asia/Shanghai')")
        db.execute(
            "INSERT INTO events VALUES('e1','Beijing','2026-08-12','TEST','Station',NULL,'secret','35 C','later')"
        )
        visible = events_for_date(db, "2026-08-12", ["Beijing"], include_settlement=False)
        self.assertNotIn("winning_market_id", visible[0])
        self.assertNotIn("winning_range", visible[0])
        self.assertNotIn("resolved_at_utc", visible[0])

    def test_city_analysis_is_scoped_to_one_city_and_known_markets(self):
        as_of = datetime(2026, 8, 11, 23, tzinfo=UTC)
        city_packet = {
            "city": {
                "eventId": "e1", "city": "Beijing",
                "markets": [["m1", "26 C", 0.5, 0.55, 20, 0.54, 0.53, 5, True]],
            }
        }
        market_lookup = {
            "m1": {
                "market_id": "m1", "outcome_range": "26 C", "no_best_ask": 0.55,
                "snapshotAgeMinutes": 5,
            }
        }
        response = {
            "asOfUtc": as_of.isoformat(timespec="seconds"), "eventId": "e1", "city": "Beijing",
            "weatherAssessment": "cloudy", "recommendations": [{
                "marketId": "m1", "outcomeRange": "26 C", "estimatedNoWinProbability": 0.7,
                "reason": "positive expectation", "contraryEvidence": "could clear",
                "invalidationCondition": "temperature reaches 26 C",
            }],
            "suggestedNextReviewMinutes": 60, "nextEvidenceToWatch": "next observation",
        }
        validate_city_analysis(response, as_of, city_packet, market_lookup, {"marketSnapshotMaxAgeMinutes": 20})
        response["recommendations"][0]["marketId"] = "other"
        with self.assertRaisesRegex(RuntimeError, "unknown market"):
            validate_city_analysis(response, as_of, city_packet, market_lookup, {"marketSnapshotMaxAgeMinutes": 20})

    def test_portfolio_receives_only_short_candidate_set(self):
        packet = {
            "experiment": {"fixedCashPerOrderUsdc": 3}, "targetDate": "2026-08-12",
            "asOfUtc": "2026-08-11T23:00:00+00:00", "asOfLocal": "2026-08-12T07:00:00+08:00",
            "account": {"availableCashUsdc": 20},
        }
        analyses = [{
            "city": "Beijing", "recommendations": [{"marketId": "m1"}],
        }]
        lookup = {
            "m1": {
                "city": "Beijing", "outcome_range": "26 C", "no_best_bid": 0.5,
                "no_best_ask": 0.55, "no_ask_size": 20, "noAsk30mAgo": 0.54,
                "noAsk60mAgo": 0.53, "snapshotAgeMinutes": 5,
            },
            "m2": {"city": "Shanghai", "outcome_range": "30 C"},
        }
        portfolio_packet, candidates = portfolio_packet_for_ai(packet, analyses, lookup)
        self.assertEqual(set(candidates), {"m1"})
        self.assertEqual([row["marketId"] for row in portfolio_packet["candidateMarkets"]], ["m1"])
        self.assertNotIn("metar", str(portfolio_packet).lower())

    def test_isolated_profile_links_credentials_but_has_no_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.mkdir()
            (source / ".env").write_text("TEST_KEY=x\n", encoding="utf-8")
            (source / "auth.json").write_text("{}\n", encoding="utf-8")
            profile, workspace = ensure_isolated_hermes_profile({
                "hermesCredentialSourceHome": str(source), "hermesHome": str(target),
            })
            self.assertTrue((profile / ".env").is_symlink())
            self.assertTrue((profile / "auth.json").is_symlink())
            self.assertTrue((profile / "SOUL.md").is_symlink())
            self.assertFalse((profile / "memories").exists())
            self.assertTrue(workspace.is_dir())


if __name__ == "__main__":
    unittest.main()
