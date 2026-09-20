import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from weather_forecast_cutoff_tracker import ForecastCutoffTracker, content_hash


class ForecastCutoffTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE stations(station_id TEXT PRIMARY KEY,timezone TEXT);
            CREATE TABLE events(
                event_id TEXT PRIMARY KEY,city TEXT,station_id TEXT,target_date TEXT
            );
            CREATE TABLE external_forecasts(
                slot_utc TEXT,station_id TEXT,target_date TEXT,source TEXT,model TEXT,
                fetched_at_utc TEXT,forecast_max_c REAL,forecast_peak_local TEXT,
                points_json TEXT,status TEXT,error TEXT
            );
            CREATE TABLE forecast_model_runs(
                source TEXT,station_id TEXT,target_date TEXT,model TEXT,version_hash TEXT,
                model_run_time_utc TEXT,run_time_source TEXT,first_seen_utc TEXT
            );
            CREATE TABLE weather_resolution_labels(
                event_id TEXT,official_temperature_c REAL,exact_at_resolution_precision INTEGER
            );
            INSERT INTO stations VALUES('ZSPD','Asia/Shanghai');
            INSERT INTO events VALUES('e1','Shanghai','ZSPD','2026-08-05');
            """
        )
        self.config = {
            "processAnalysisCities": ["Shanghai"],
            "forecastCutoffsLocal": ["10:00"],
            "forecastCutoffModels": [
                {"source": "open_meteo", "model": "ecmwf_ifs025"},
                {"source": "cma_meso", "model": "cma_meso_3km"},
            ],
            "cma": {"credentialFile": str(Path(self.temp.name) / "missing.json")},
        }
        self.tracker = ForecastCutoffTracker(self.db, self.config, self.temp.name)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def _insert_forecast(self, slot, fetched, maximum):
        points = json.dumps([{"time_local": "2026-08-05T15:00+08:00", "temp_c": maximum}])
        self.db.execute(
            "INSERT INTO external_forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                slot, "ZSPD", "2026-08-05", "open_meteo", "ecmwf_ifs025",
                fetched, maximum, "2026-08-05T15:00+08:00", points, "ok", None,
            ),
        )
        return points

    def test_fixed_cutoff_rejects_forecast_fetched_after_cutoff(self):
        points = self._insert_forecast(
            "2026-08-05T01:55:00+00:00", "2026-08-05T01:56:00+00:00", 34
        )
        version = content_hash(
            "ecmwf_ifs025", "2026-08-05", 34, "2026-08-05T15:00+08:00", points
        )
        self.db.execute(
            "INSERT INTO forecast_model_runs VALUES(?,?,?,?,?,?,?,?)",
            (
                "open_meteo", "ZSPD", "2026-08-05", "ecmwf_ifs025", version,
                None, "provider_not_exposed", "2026-08-05T02:01:00+00:00",
            ),
        )
        self._insert_forecast("2026-08-05T02:00:00+00:00", "2026-08-05T02:05:00+00:00", 36)
        written = self.tracker.capture_due(
            datetime(2026, 8, 5, 2, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 5, 2, 6, tzinfo=timezone.utc),
        )
        self.assertEqual(written, 2)
        row = self.db.execute(
            "SELECT * FROM forecast_cutoff_snapshots WHERE model='ecmwf_ifs025'"
        ).fetchone()
        self.assertEqual(row["status"], "captured")
        self.assertEqual(row["forecast_max_c"], 34)
        self.assertEqual(row["source_sample_slot_utc"], "2026-08-05T01:55:00+00:00")
        self.assertEqual(row["first_fetched_at_utc"], "2026-08-05T01:56:00+00:00")
        self.assertEqual(row["source_age_seconds"], 240)
        cma = self.db.execute(
            "SELECT * FROM forecast_cutoff_snapshots WHERE model='cma_meso_3km'"
        ).fetchone()
        self.assertEqual(cma["status"], "provider_unconfigured")

    def test_new_run_is_deduplicated_and_records_arrival_latency(self):
        points = self._insert_forecast(
            "2026-08-05T02:00:00+00:00", "2026-08-05T02:04:00+00:00", 35
        )
        version = content_hash(
            "ecmwf_ifs025", "2026-08-05", 35, "2026-08-05T15:00+08:00", points
        )
        args = dict(
            source="open_meteo", model="ecmwf_ifs025", station_id="ZSPD",
            target_date="2026-08-05", sample_slot_utc="2026-08-05T02:00:00+00:00",
            fetched_at_utc="2026-08-05T02:04:00+00:00", version_hash=version,
            forecast_max_c=35, forecast_peak_local="2026-08-05T15:00+08:00",
            points_json=points, model_run_time_utc="2026-08-05T00:00:00+00:00",
            model_run_time_source="provider",
        )
        self.assertEqual(self.tracker.capture_new_run(**args), 1)
        self.assertEqual(self.tracker.capture_new_run(**args), 0)
        row = self.db.execute(
            "SELECT * FROM forecast_cutoff_snapshots WHERE snapshot_kind='new_run'"
        ).fetchone()
        self.assertEqual(row["arrival_latency_seconds"], 7440)
        self.assertEqual(row["trigger_basis"], "model_run")

    def test_evaluation_report_preserves_independent_date_count(self):
        self._insert_forecast("2026-08-05T01:55:00+00:00", "2026-08-05T01:56:00+00:00", 34.4)
        self.tracker.capture_due(
            datetime(2026, 8, 5, 2, tzinfo=timezone.utc),
            datetime(2026, 8, 5, 2, 1, tzinfo=timezone.utc),
        )
        self.db.execute("INSERT INTO weather_resolution_labels VALUES('e1',35,1)")
        self.assertEqual(self.tracker.refresh_evaluations(), 1)
        self.assertEqual(self.tracker.refresh_evaluations(), 0)
        report = self.tracker.write_reports()
        ecmwf = next(item for item in report["summary"] if item["model"] == "ecmwf_ifs025")
        self.assertEqual(ecmwf["evaluatedIndependentDates"], 1)
        self.assertEqual(ecmwf["maeC"], 0.6)
        self.assertEqual(ecmwf["roundedBucketHitRate"], 0.0)


if __name__ == "__main__":
    unittest.main()
