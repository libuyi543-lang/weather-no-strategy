import unittest
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pandas as pd

from weather_ai_agent import WeatherAIAgent
from weather_market_alignment import (
    RidgeV2Adapter, market_alignment, ridge_v3_bucket_distribution,
    stabilize_ridge_center,
)


def market(market_id, bucket, midpoint):
    return {
        "marketId": market_id,
        "outcomeRange": f"{bucket} C",
        "bucketLow": bucket,
        "bucketHigh": bucket,
        "yesBestBid": midpoint - 0.01,
        "yesBestAsk": midpoint + 0.01,
        "yesExecutableBuyPrice5": midpoint + 0.01,
    }


CONFIG = {
    "marketLeaderMinMidpoint": 0.20,
    "marketLeaderMinGap": 0.05,
    "marketPrematureConvergenceMidpoint": 0.75,
    "ridgeV2StrongDeviationC": 2.0,
}


class MarketAlignmentTests(unittest.TestCase):
    @staticmethod
    def runtime_row(observation_time="2026-07-29T06:00:00+00:00"):
        return pd.DataFrame([{
            "event_id": "e", "target_date": "2026-07-29",
            "observation_age_minutes": 15.0,
            "feature_as_of_utc": "2026-07-29T06:15:00+00:00",
            "latest_observation_time_utc": observation_time,
            "latest_observation_fetched_at_utc": "2026-07-29T06:05:00+00:00",
            "meteoblue_fetched_at_utc": "2026-07-29T05:00:00+00:00",
            "ecmwf_fetched_at_utc": "2026-07-29T05:00:00+00:00",
            "prior_center": 32.0, "observed_max": 31.0,
            "remaining_usable_solar": 0.8, "trend_120": 0.2,
            "cloud_cover_pct": 20.0, "current_below_max": 0.0,
            "model_spread": 0.5, "ecmwf": 32.0, "mblue": 32.0,
            "remaining_solar_fraction": 0.4,
        }])

    def test_v3_distribution_preserves_asymmetry_and_normalizes(self):
        result = ridge_v3_bucket_distribution(32.0, 30.0, [-1.2, -0.8, 0.1, 1.1])
        probabilities = {row["bucketC"]: row["probability"] for row in result["bucketProbabilities"]}
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=6)
        self.assertGreater(result["coldTailProbability"], result["warmTailProbability"])
        self.assertEqual(result["probabilitySum"], 1.0)

    def test_v3_distribution_applies_observed_max_floor(self):
        result = ridge_v3_bucket_distribution(31.0, 32.2, [-5.0, -1.0, 0.0])
        self.assertEqual(result["bucketProbabilities"], [{"bucketC": 32, "probability": 1.0}])

    def test_v3_distribution_applies_temperature_and_keeps_raw_probabilities(self):
        result = ridge_v3_bucket_distribution(
            32.0, 30.0, [0.0, 0.0, 0.0, 1.0], temperature=2.0
        )
        raw = {row["bucketC"]: row["probability"] for row in result["rawBucketProbabilities"]}
        calibrated = {row["bucketC"]: row["probability"] for row in result["bucketProbabilities"]}
        self.assertEqual(raw[32], 0.75)
        self.assertLess(calibrated[32], raw[32])
        self.assertTrue(result["probabilityCalibrationApplied"])
        self.assertAlmostEqual(sum(calibrated.values()), 1.0, places=6)

    def test_same_metar_reuses_snapshot_and_new_metar_recomputes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            model_dir = root / "data" / "models"
            model_dir.mkdir(parents=True)
            (model_dir / "weather_exact_high_v21_shared.joblib").touch()
            model = Mock()
            model.predict.return_value = [0.0]
            artifact = {
                "model": model, "model_version": "ridge_v2.1_shared_time",
                "distribution_version": "ridge_v3_asymmetric_empirical",
                "residuals_c": [-1.0, 0.0, 1.0], "oos_dates": [],
            }
            frame_holder = {"frame": self.runtime_row()}

            class FakeBuilder:
                def __init__(self, _path):
                    pass

                def build(self, *_args, **_kwargs):
                    return frame_holder["frame"]

                def close(self):
                    pass

            adapter = RidgeV2Adapter(root, root / "unused.sqlite3", {
                "ridgeV2Enabled": True, "ridgeV2StartLocalMinutes": 600,
                "ridgeV2EndLocalMinutes": 1140, "ridgeV2IntervalMinutes": 30,
                "ridgeV2PersistSnapshots": False, "ridgeV21DailyRetrain": False,
            })
            event = {
                "event_id": "e", "target_date": "2026-07-29",
                "timezone": "Asia/Shanghai",
            }
            now = datetime(2026, 7, 29, 6, 15, tzinfo=timezone.utc)
            with patch("joblib.load", return_value=artifact), patch(
                "research.weather_exact_high_model_v2.DatasetBuilderV2", FakeBuilder
            ):
                adapter.snapshot(event, now, "state-1")
                adapter.snapshot(event, now, "state-2")
                frame_holder["frame"] = self.runtime_row("2026-07-29T06:10:00+00:00")
                adapter.snapshot(event, now, "state-3")
            self.assertEqual(model.predict.call_count, 2)

    def test_cached_metar_cannot_bypass_staleness_limit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            model_dir = root / "data" / "models"
            model_dir.mkdir(parents=True)
            (model_dir / "weather_exact_high_v21_shared.joblib").touch()
            model = Mock()
            model.predict.return_value = [0.0]
            artifact = {"model": model, "residuals_c": [0.0], "oos_dates": []}
            frame_holder = {"frame": self.runtime_row()}

            class FakeBuilder:
                def __init__(self, _path):
                    pass

                def build(self, *_args, **_kwargs):
                    return frame_holder["frame"]

                def close(self):
                    pass

            adapter = RidgeV2Adapter(root, root / "unused.sqlite3", {
                "ridgeV2Enabled": True, "ridgeV2StartLocalMinutes": 600,
                "ridgeV2EndLocalMinutes": 1140, "ridgeV2IntervalMinutes": 30,
                "ridgeV2PersistSnapshots": False, "ridgeV21DailyRetrain": False,
                "ridgeV2MaxObservationAgeMinutes": 90,
            })
            event = {"event_id": "e", "target_date": "2026-07-29", "timezone": "Asia/Shanghai"}
            now = datetime(2026, 7, 29, 6, 15, tzinfo=timezone.utc)
            with patch("joblib.load", return_value=artifact), patch(
                "research.weather_exact_high_model_v2.DatasetBuilderV2", FakeBuilder
            ):
                first = adapter.snapshot(event, now)
                stale_frame = self.runtime_row()
                stale_frame.loc[0, "observation_age_minutes"] = 95.0
                frame_holder["frame"] = stale_frame
                second = adapter.snapshot(event, now)
            self.assertEqual(first["status"], "ok")
            self.assertEqual(second["status"], "stale")

    def test_daily_retraining_is_attempted_only_once(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "weather.sqlite3"
            db = sqlite3.connect(db_path)
            db.execute("CREATE TABLE events(target_date TEXT,resolved_at_utc TEXT)")
            db.execute("INSERT INTO events VALUES('2026-07-28','2026-07-29T00:00:00Z')")
            db.commit()
            db.close()
            model_dir = root / "data" / "models"
            model_dir.mkdir(parents=True)
            artifact_path = model_dir / "weather_exact_high_v21_shared.joblib"
            import joblib
            joblib.dump({"trained_through": "2026-07-27"}, artifact_path)
            adapter = RidgeV2Adapter(root, db_path, {"ridgeV21DailyRetrain": True})
            trainer = Mock(return_value=({
                "trained_through": "2026-07-28", "train_events": 10,
            }, artifact_path))
            with patch(
                "research.weather_exact_high_model_v2.train_shared_artifact", trainer
            ):
                first = adapter._ensure_daily_shared_artifact(
                    "2026-07-29", datetime(2026, 7, 29, tzinfo=timezone.utc)
                )
                second = adapter._ensure_daily_shared_artifact(
                    "2026-07-29", datetime(2026, 7, 29, tzinfo=timezone.utc)
                )
            self.assertEqual(first["status"], "retrained")
            self.assertEqual(second["status"], "already_attempted_today")
            trainer.assert_called_once()

    def test_visibility_challenger_is_disabled_by_default(self):
        adapter = RidgeV2Adapter(Path("/tmp"), Path("/tmp/missing.sqlite3"), {})
        result = adapter._visibility_challenger(
            self.runtime_row().iloc[0], 31.0, "1030", "2026-07-29",
            datetime(2026, 7, 29, 2, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(result, {"status": "disabled", "shadowOnly": True})

    def test_visibility_challenger_only_runs_at_selected_cutoffs(self):
        adapter = RidgeV2Adapter(
            Path("/tmp"), Path("/tmp/missing.sqlite3"),
            {"visibilityChallengerEnabled": True},
        )
        result = adapter._visibility_challenger(
            self.runtime_row().iloc[0], 31.0, "1000", "2026-07-29",
            datetime(2026, 7, 29, 2, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "not_due")
        self.assertTrue(result["shadowOnly"])

    def test_visibility_challenger_is_nested_and_does_not_replace_ridge(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            model_dir = root / "data" / "models"
            model_dir.mkdir(parents=True)
            (model_dir / "weather_exact_high_v21_shared.joblib").touch()
            (model_dir / "weather_visibility_challenger_v1.joblib").touch()
            baseline_model = Mock()
            baseline_model.predict.return_value = [0.0]
            challenger_model = Mock()
            challenger_model.predict.return_value = [1.0]
            baseline_artifact = {
                "model": baseline_model, "residuals_c": [0.0], "oos_dates": [],
            }
            challenger_artifact = {
                "model": challenger_model,
                "model_version": "ridge_visibility_challenger_v1",
                "trained_through": "2026-07-28",
            }
            frame = self.runtime_row()
            frame.loc[0, "visibility_m"] = 8000.0

            class FakeBuilder:
                def __init__(self, _path):
                    pass

                def build(self, *_args, **_kwargs):
                    return frame

                def close(self):
                    pass

            adapter = RidgeV2Adapter(root, root / "missing.sqlite3", {
                "ridgeV2Enabled": True, "ridgeV2StartLocalMinutes": 600,
                "ridgeV2EndLocalMinutes": 1140, "ridgeV2IntervalMinutes": 30,
                "ridgeV2PersistSnapshots": False, "ridgeV21DailyRetrain": False,
                "visibilityChallengerEnabled": True,
                "visibilityChallengerDailyRetrain": False,
            })

            def load_artifact(path):
                if Path(path).name == "weather_visibility_challenger_v1.joblib":
                    return challenger_artifact
                return baseline_artifact

            event = {"event_id": "e", "target_date": "2026-07-29", "timezone": "Asia/Shanghai"}
            now = datetime(2026, 7, 29, 2, 30, tzinfo=timezone.utc)
            with patch("joblib.load", side_effect=load_artifact), patch(
                "research.weather_exact_high_model_v2.DatasetBuilderV2", FakeBuilder
            ):
                result = adapter.snapshot(event, now)

            self.assertEqual(result["primaryPathC"], 32.0)
            self.assertEqual(result["primaryBucketC"], 32)
            self.assertEqual(result["visibilityChallenger"]["primaryPathC"], 33.0)
            self.assertEqual(result["visibilityChallenger"]["primaryBucketC"], 33)
            self.assertTrue(result["visibilityChallenger"]["shadowOnly"])
            self.assertFalse(result["visibilityChallenger"]["authoritative"])

    def test_visibility_failure_does_not_break_ridge(self):
        adapter = RidgeV2Adapter(
            Path("/tmp"), Path("/tmp/missing.sqlite3"),
            {"visibilityChallengerEnabled": True},
        )
        with patch.object(
            adapter, "_ensure_visibility_artifact",
            return_value={"status": "failed_previous_artifact_retained"},
        ):
            result = adapter._visibility_challenger(
                self.runtime_row().iloc[0], 31.0, "1030", "2026-07-29",
                datetime(2026, 7, 29, 2, 30, tzinfo=timezone.utc),
            )
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["shadowOnly"])

    def test_stability_clamps_unexplained_jump(self):
        previous = {
            "primaryPathC": 32.0, "artifact": "shared.joblib",
            "inputs": {
                "observedMaxC": 31.0, "meteoblueFetchedAtUtc": "m1",
                "ecmwfFetchedAtUtc": "e1",
            },
            "processSignature": ["active_heating"],
        }
        center, state = stabilize_ridge_center(
            34.0, 31.0, previous,
            {"observedMaxC": 31.0, "meteoblueFetchedAtUtc": "m1", "ecmwfFetchedAtUtc": "e1"},
            "shared.joblib", ["active_heating"], 0.75,
        )
        self.assertEqual(center, 32.75)
        self.assertTrue(state["constraintApplied"])

    def test_stability_allows_jump_when_new_observed_max_supports_it(self):
        previous = {
            "primaryPathC": 32.0, "artifact": "shared.joblib",
            "inputs": {"observedMaxC": 31.0}, "processSignature": [],
        }
        center, state = stabilize_ridge_center(
            34.0, 33.0, previous, {"observedMaxC": 33.0},
            "shared.joblib", [], 0.75,
        )
        self.assertEqual(center, 34.0)
        self.assertIn("new_observed_max", state["allowedReasons"])
    def test_ridge_adapter_is_not_due_before_first_1000_cutoff(self):
        adapter = RidgeV2Adapter(
            __import__("pathlib").Path("/tmp/no-ridge-artifacts"),
            __import__("pathlib").Path("/tmp/missing.sqlite3"),
            {
                "ridgeV2Enabled": True, "ridgeV2IntervalMinutes": 30,
                "ridgeV2StartLocalMinutes": 600, "ridgeV2EndLocalMinutes": 1140,
            },
        )
        result = adapter.snapshot(
            {"event_id": "e", "target_date": "2026-07-26", "timezone": "Asia/Shanghai"},
            __import__("datetime").datetime(2026, 7, 26, 1, 59, tzinfo=__import__("datetime").timezone.utc),
        )
        self.assertEqual(result["status"], "not_due")
        self.assertEqual(result["reason"], "first Ridge V2 cutoff is 10:00 local")

    def test_ridge_adapter_selects_latest_half_hour_artifact(self):
        adapter = RidgeV2Adapter(
            __import__("pathlib").Path("/tmp/no-ridge-artifacts"),
            __import__("pathlib").Path("/tmp/missing.sqlite3"),
            {
                "ridgeV2Enabled": True, "ridgeV2IntervalMinutes": 30,
                "ridgeV2StartLocalMinutes": 600, "ridgeV2EndLocalMinutes": 1140,
            },
        )
        result = adapter.snapshot(
            {"event_id": "e", "target_date": "2026-07-26", "timezone": "Asia/Shanghai"},
            __import__("datetime").datetime(2026, 7, 26, 6, 45, tzinfo=__import__("datetime").timezone.utc),
        )
        self.assertIn("weather_exact_high_v2_1430.joblib", result["reason"])

    def ridge(self, primary=35, warm=36, status="ok", supported="plausible", trend=0.1):
        return {
            "status": status,
            "primaryBucketC": primary,
            "warmTailBucketC": warm,
            "plausibleBucketsC": list(range(min(primary, warm) - 1, max(primary, warm) + 2)),
            "pathStatus": {"warmTail": supported},
            "inputs": {"trend120CPerHour": trend, "remainingSolarFraction": 0.5},
        }

    def test_follow_when_market_and_ridge_primary_agree(self):
        result = market_alignment(
            [market("m35", 35, 0.70), market("m34", 34, 0.20), market("m36", 36, 0.10)],
            self.ridge(), {}, CONFIG,
        )
        self.assertEqual(result["mode"], "FOLLOW")
        self.assertEqual(result["candidates"][0]["entryType"], "YES_CONVERGENCE")

    def test_watch_when_weather_is_close_but_not_confirmed(self):
        result = market_alignment(
            [market("m35", 35, 0.60), market("m34", 34, 0.20), market("m36", 36, 0.10)],
            self.ridge(primary=36, warm=37), {}, CONFIG,
        )
        self.assertEqual(result["mode"], "WATCH")
        self.assertEqual(result["candidates"], [])

    def test_fade_when_market_prematurely_converges_with_warm_tail(self):
        result = market_alignment(
            [market("m39", 39, 0.86), market("m40", 40, 0.08), market("m41", 41, 0.03)],
            self.ridge(primary=39, warm=41, supported="supported_competitor", trend=1.0),
            {"detectedProcesses": ["no_high_confidence_regime_change"]}, CONFIG,
        )
        self.assertEqual(result["mode"], "FADE")
        self.assertEqual(result["candidates"][0]["entryType"], "NO_LEADER_OVERSHOOT")
        self.assertEqual(result["candidates"][0]["outcomeSide"], "NO")

    def test_no_edge_without_distinct_market_leader(self):
        result = market_alignment(
            [market("m35", 35, 0.40), market("m34", 34, 0.38), market("m36", 36, 0.20)],
            self.ridge(), {}, CONFIG,
        )
        self.assertEqual(result["mode"], "NO_EDGE")

    def test_leader_overshoot_buy_passes_only_with_required_context(self):
        engine = object.__new__(WeatherAIAgent)
        engine.config = {"marketAlignmentEnforced": True}
        action = {
            "exactBucketRiskAssessment": "The touched leader can lose to a higher final bucket.",
            "outcomeAssessment": "plausible", "probabilityBand": "15_30",
            "priceAssessment": "favorable", "priceRiskAssessment": "room for tail risk",
            "outcomeSide": "NO", "entryType": "NO_LEADER_OVERSHOOT", "marketId": "m39",
            "heatingProcessStatus": "active", "newEvidenceSincePrior": [],
        }
        decision = {"settlementDistribution": [{
            "outcomeRange": "39 C", "rank": 2, "classification": "plausible",
            "probabilityBand": "15_30",
        }]}
        context = {
            "marketAlignment": {
            "mode": "FADE", "marketPrematureConvergence": True, "activeHeating": True,
            "marketLeader": {"marketId": "m39", "bucketC": 39},
            "candidates": [{"marketId": "m39", "outcomeSide": "NO", "entryType": "NO_LEADER_OVERSHOOT"}],
            },
            "ridgeV2": {"status": "ok", "warmTailBucketC": 41},
            "metar": {"current": {"temperature_c": 39}},
        }
        market_row = {"marketId": "m39", "outcomeRange": "39 C", "bucketLow": 39}
        self.assertIsNone(engine._buy_reasoning_rejection(action, decision, context, market_row, None))

    def test_follow_aligned_market_leader_no_is_hard_blocked(self):
        engine = object.__new__(WeatherAIAgent)
        engine.config = {"blockFollowAlignedLeaderNo": True}
        action = {
            "action": "buy", "marketId": "m32", "outcomeSide": "NO",
            "entryType": "NO_OVERSHOOT",
        }
        context = {
            "marketAlignment": {
                "mode": "FOLLOW", "weatherAlignment": "aligned",
                "marketPrematureConvergence": False,
                "marketLeader": {"marketId": "m32", "bucketC": 32},
            }
        }
        reason = engine._follow_aligned_leader_no_rejection(action, context)
        self.assertIn("FOLLOW+aligned", reason)

        context["marketAlignment"].update({
            "mode": "FADE", "weatherAlignment": "contradicted",
            "marketPrematureConvergence": True,
        })
        self.assertIsNone(engine._follow_aligned_leader_no_rejection(action, context))


if __name__ == "__main__":
    unittest.main()
