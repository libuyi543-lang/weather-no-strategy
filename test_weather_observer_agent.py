import unittest
import sqlite3

from weather_observer_agent import WeatherObserverAgent, material_trigger_types


def config():
    return {
        "radarDistanceChangeKm": 10,
        "radarCoverageChange": 0.02,
        "satelliteCoverageChange": 0.05,
        "modelMaxChangeC": 0.25,
        "radiationChangeWm2": 100,
    }


def state():
    return {
        "primaryObservationTimeUtc": "2026-07-25T03:00:00+00:00",
        "detectedProcesses": ["active_heating"],
        "heatingPhase": "active",
        "radiation": {"solarRadiationWm2": 700, "directRadiationWm2": 500},
        "models": {"meteoblue": {"forecastMaxC": 35.2, "referenceTimeUtc": "run-1"}},
        "stationNetwork": {"latestObservationUtc": "2026-07-25T03:00:00+00:00"},
        "remoteSensing": {
            "rainviewer": {"frameTimeUtc": "frame-1", "nearestEchoKm": 80, "echoCoverage50Km": 0.01},
            "himawari": {"frameTimeUtc": "sat-1", "cloudProxyCoverage50Km": 0.10},
        },
    }


class MaterialTriggerTests(unittest.TestCase):
    def test_initial_state_wakes_observer(self):
        self.assertEqual(material_trigger_types(None, state(), config()), ["initial_state"])

    def test_identical_state_is_filtered_without_ai(self):
        current = state()
        self.assertEqual(material_trigger_types(current, current, config()), [])

    def test_new_metar_and_radar_boundary_are_detected(self):
        previous, current = state(), state()
        current["primaryObservationTimeUtc"] = "2026-07-25T03:30:00+00:00"
        current["remoteSensing"]["rainviewer"] = {
            "frameTimeUtc": "frame-2", "nearestEchoKm": 45, "echoCoverage50Km": 0.04,
        }
        triggers = material_trigger_types(previous, current, config())
        self.assertIn("primary_metar", triggers)
        self.assertIn("radar_arrival", triggers)

    def test_model_revision_and_radiation_regime_are_detected(self):
        previous, current = state(), state()
        current["models"] = {"meteoblue": {"forecastMaxC": 35.6, "referenceTimeUtc": "run-2"}}
        current["radiation"] = {"solarRadiationWm2": 520, "directRadiationWm2": 340}
        triggers = material_trigger_types(previous, current, config())
        self.assertIn("model_revision", triggers)
        self.assertIn("radiation_regime", triggers)


class ObserverSchemaTests(unittest.TestCase):
    def setUp(self):
        self.observer = object.__new__(WeatherObserverAgent)

    def response(self):
        return {
            "materiality": "REANALYZE",
            "changedProcess": "rain_arrival_advanced",
            "primaryBuckets": ["35 C"],
            "plausibleBuckets": ["34 C", "35 C"],
            "tailBuckets": ["36 C"],
            "excludedBuckets": ["37 C"],
            "heatingStatus": "capping",
            "processSummary": "Rain now reaches the station before peak heating.",
            "affectedBuckets": ["35 C", "37 C"],
            "evidence": ["radar ETA moved earlier"],
            "invalidation": "radar turns away",
            "uncertain": False,
            "escalationReason": "Bucket paths changed.",
        }

    def test_valid_response_uses_only_market_bucket_labels(self):
        self.observer.schema = {"required": list(self.response())}
        self.observer._validate_response(self.response(), ["34 C", "35 C", "36 C", "37 C"])

    def test_unknown_bucket_is_rejected(self):
        self.observer.schema = {"required": list(self.response())}
        response = self.response()
        response["excludedBuckets"] = ["99 C"]
        with self.assertRaisesRegex(RuntimeError, "unknown bucket"):
            self.observer._validate_response(response, ["34 C", "35 C", "36 C", "37 C"])


class ObserverCooldownTests(unittest.TestCase):
    def setUp(self):
        self.observer = object.__new__(WeatherObserverAgent)
        self.observer.config = {"remoteOnlyObserverCooldownMinutes": 10}
        self.observer.db = sqlite3.connect(":memory:")
        self.observer.db.row_factory = sqlite3.Row
        self.observer.db.execute(
            "CREATE TABLE weather_observer_events(city TEXT,status TEXT,prompt_tokens INTEGER,source_slot_utc TEXT)"
        )
        self.observer.db.execute(
            "INSERT INTO weather_observer_events VALUES('Wuhan','completed',100,'2026-07-25T05:10:00+00:00')"
        )

    def tearDown(self):
        self.observer.db.close()

    def test_remote_only_change_is_debounced_but_metar_is_not(self):
        self.assertEqual(
            self.observer.apply_observer_cooldown(
                "Wuhan", "2026-07-25T05:15:00+00:00", ["radar_arrival"], False
            ),
            [],
        )
        self.assertEqual(
            self.observer.apply_observer_cooldown(
                "Wuhan", "2026-07-25T05:15:00+00:00", ["primary_metar"], False
            ),
            ["primary_metar"],
        )


if __name__ == "__main__":
    unittest.main()
