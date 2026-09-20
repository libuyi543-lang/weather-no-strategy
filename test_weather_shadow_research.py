import sqlite3
import unittest
from datetime import datetime, timezone

from weather_shadow_research import capture_shadow_sources, init_shadow_schema


class ShadowResearchTests(unittest.TestCase):
    def test_missing_sources_are_preserved_for_honest_coverage_metrics(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(
            """
            CREATE TABLE windy_forecasts(station_id TEXT,target_date TEXT,slot_utc TEXT,status TEXT,
                model_ref_time_utc TEXT,model_updated_at_utc TEXT,forecast_max_c REAL,points_json TEXT);
            CREATE TABLE external_forecasts(station_id TEXT,target_date TEXT,slot_utc TEXT,status TEXT,
                model TEXT,forecast_max_c REAL,forecast_peak_local TEXT,points_json TEXT);
            CREATE TABLE weather_observations(station_id TEXT,slot_utc TEXT,source TEXT,status TEXT,
                observation_time_utc TEXT,temperature_c REAL,dewpoint_c REAL,wind_direction_deg REAL,
                wind_speed REAL,raw_metar TEXT);
            CREATE TABLE fast_metar_reports(station_id TEXT,observation_time_utc TEXT,report_time_utc TEXT,
                receipt_time_utc TEXT,metar_type TEXT,raw_metar TEXT,first_fetched_at_utc TEXT);
            CREATE TABLE remote_sensing_snapshots(station_id TEXT,slot_utc TEXT,source TEXT,status TEXT,
                frame_time_utc TEXT,quality TEXT,features_json TEXT,raw_sha256 TEXT,error TEXT);
            CREATE TABLE source_access_probes(source TEXT,product TEXT,status TEXT,checked_at_utc TEXT,
                endpoint TEXT,latency_ms REAL,detail TEXT);
            """
        )
        init_shadow_schema(db)
        slot = datetime(2026, 7, 27, 2, tzinfo=timezone.utc)
        written = capture_shadow_sources(
            db, slot, [{"station_id": "ZSPD", "target_date": "2026-07-27"}]
        )
        self.assertEqual(written, 11)
        statuses = dict(db.execute(
            "SELECT source,status FROM shadow_source_snapshots"
        ).fetchall())
        self.assertEqual(statuses["jaxa_swr"], "missing")
        self.assertEqual(statuses["fast_metar"], "missing")
        db.close()


if __name__ == "__main__":
    unittest.main()
