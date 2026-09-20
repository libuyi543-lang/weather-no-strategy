import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.weather_exact_high_model_v2 import (
    DIAGNOSTIC_FEATURES,
    DatasetBuilderV2,
    NUMERIC_FEATURES,
    SHARED_TIME_FEATURES,
    available_features,
    cutoff_is_due,
    nearest_point,
    observed_floor,
    physically_constrained_center,
    robust_field_trend,
    temperature_scale_probabilities,
    train_shared_artifact,
    walk_forward_shared,
)


class WeatherExactHighV2Tests(unittest.TestCase):
    @staticmethod
    def shared_frame(dates=("2026-07-24", "2026-07-25", "2026-07-26", "2026-07-27")):
        rows = []
        for index, target_date in enumerate(dates):
            rows.append({
                "event_id": f"e{index}", "target_date": target_date,
                "city": "Beijing", "resolved": True,
                "target": 30.0 + index, "prior_center": 30.2 + index,
                "mblue": 30.0 + index, "observed_max": 29.5 + index,
                "remaining_usable_solar": 0.8, "trend_120": 0.3,
                "current_below_max": 0.0, "cutoff_local_minutes": 600 + index * 30,
                "cutoff_day_fraction": (600 + index * 30) / 1440,
                "elapsed_solar_fraction": 0.4,
                "cutoff_x_trend_120": 0.1,
                "cutoff_x_remaining_solar_fraction": 0.2,
            })
        return pd.DataFrame(rows)

    def test_collection_age_is_diagnostic_only(self):
        frame = pd.DataFrame({
            "prior_center": [30.0, 31.0],
            "model_vintage_age_hours": [0.0, 1.0],
        })
        self.assertIn("model_vintage_age_hours", DIAGNOSTIC_FEATURES)
        self.assertNotIn("model_vintage_age_hours", NUMERIC_FEATURES)
        self.assertNotIn("model_vintage_age_hours", available_features(frame))

    def test_shared_model_includes_time_features(self):
        frame = self.shared_frame()
        features = available_features(frame, shared_time=True)
        self.assertTrue(set(SHARED_TIME_FEATURES).issubset(features))

    def test_shared_walk_forward_uses_independent_dates_for_calibration(self):
        result = walk_forward_shared(self.shared_frame())
        self.assertEqual(result["oos_dates"], ["2026-07-26", "2026-07-27"])
        self.assertEqual(result["calibration_status"], "insufficient_independent_oos_dates")
        self.assertEqual(len(result["residuals_c"]), 2)
        self.assertIn("80", result["coverage_targets"])

    def test_shared_training_excludes_target_date_and_writes_atomically(self):
        frame = self.shared_frame(("2026-07-27", "2026-07-28", "2026-07-29"))
        with TemporaryDirectory() as directory:
            artifact = Path(directory) / "shared.joblib"
            with patch(
                "research.weather_exact_high_model_v2.build_shared_frame",
                return_value=frame,
            ):
                report, _ = train_shared_artifact(
                    Path(directory) / "unused.sqlite3", "2026-07-29",
                    datetime(2026, 7, 29, tzinfo=timezone.utc), artifact,
                )
            self.assertEqual(report["trained_through"], "2026-07-28")
            self.assertTrue(artifact.exists())
            self.assertFalse((artifact.parent / f".{artifact.name}.tmp").exists())

    def test_nearest_point_uses_same_hour_path(self):
        points = '[{"time_utc":"2026-07-25T04:00:00+00:00","temp_c":35},{"time_utc":"2026-07-25T05:00:00+00:00","temp_c":36}]'
        result = nearest_point(points, datetime(2026, 7, 25, 4, 10, tzinfo=timezone.utc))
        self.assertEqual(result["temp_c"], 35)

    def test_observed_floor_is_irreversible(self):
        frame = pd.DataFrame({"observed_max": [35.0, 31.0]})
        np.testing.assert_allclose(observed_floor(frame, np.array([34.0, 32.0])), [35.0, 32.0])

    def test_physical_cap_stops_late_positive_extrapolation(self):
        row = pd.Series({
            "observed_max": 35.0, "remaining_usable_solar": 0.20,
            "trend_120": -0.5, "current_below_max": 1.0,
        })
        self.assertEqual(physically_constrained_center(row, 36.2), 35.0)

    def test_physical_cap_preserves_active_heating_path(self):
        row = pd.Series({
            "observed_max": 35.0, "remaining_usable_solar": 0.70,
            "trend_120": 0.5, "current_below_max": 0.0,
        })
        self.assertEqual(physically_constrained_center(row, 36.2), 36.2)

    def test_temperature_scaling_flattens_overconfident_distribution(self):
        raw = {31: 0.1, 32: 0.8, 33: 0.1}
        calibrated = temperature_scale_probabilities(raw, 2.0)
        self.assertLess(calibrated[32], raw[32])
        self.assertAlmostEqual(sum(calibrated.values()), 1.0)

    def test_robust_field_trend_uses_observation_time(self):
        rows = [
            {"observation_time_utc": "2026-07-25T03:00:00+00:00", "dewpoint_c": 22},
            {"observation_time_utc": "2026-07-25T04:00:00+00:00", "dewpoint_c": 20},
        ]
        self.assertEqual(robust_field_trend(rows, "dewpoint_c", 120), -2.0)

    def test_future_cutoff_is_not_due(self):
        now = datetime(2026, 7, 26, 6, 59, tzinfo=timezone.utc)
        self.assertFalse(cutoff_is_due("2026-07-26", "Asia/Shanghai", 15, 0, now))
        self.assertTrue(cutoff_is_due(
            "2026-07-26", "Asia/Shanghai", 15, 0,
            datetime(2026, 7, 26, 7, 0, tzinfo=timezone.utc),
        ))

    def test_builder_excludes_reports_not_yet_fetched(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "point_in_time.sqlite3"
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE stations (
                    station_id TEXT PRIMARY KEY,latitude REAL,longitude REAL,timezone TEXT
                );
                CREATE TABLE events (
                    event_id TEXT PRIMARY KEY,target_date TEXT,city TEXT,station_id TEXT,
                    winning_range TEXT
                );
                CREATE TABLE windy_forecasts (
                    station_id TEXT,target_date TEXT,model TEXT,status TEXT,forecast_max_c REAL,
                    slot_utc TEXT,fetched_at_utc TEXT,model_ref_time_utc TEXT,
                    model_updated_at_utc TEXT,points_json TEXT
                );
                CREATE TABLE external_forecasts (
                    station_id TEXT,target_date TEXT,model TEXT,status TEXT,forecast_max_c REAL,
                    slot_utc TEXT,fetched_at_utc TEXT,points_json TEXT
                );
                CREATE TABLE weather_observations (
                    station_id TEXT,source TEXT,status TEXT,observation_time_utc TEXT,
                    temperature_c REAL,dewpoint_c REAL,relative_humidity REAL,
                    precipitation_mm REAL,cloud_cover_pct REAL,wind_direction_deg REAL,
                    wind_speed REAL,wind_speed_unit TEXT,wind_gust REAL,visibility_m REAL,
                    pressure_hpa REAL,solar_radiation_wm2 REAL,direct_radiation_wm2 REAL,
                    diffuse_radiation_wm2 REAL,sky_conditions_json TEXT,weather_code TEXT,
                    metar_type TEXT,slot_utc TEXT,fetched_at_utc TEXT
                );
                """
            )
            db.execute("INSERT INTO stations VALUES('ZBAA',40.08,116.58,'Asia/Shanghai')")
            db.execute("INSERT INTO events VALUES('e1','2026-07-25','Beijing','ZBAA','35 C')")
            db.execute(
                "INSERT INTO windy_forecasts VALUES(?,?,?,?,?,?,?,?,?,?)",
                ('ZBAA','2026-07-25','mblue','ok',34.0,
                 '2026-07-25T01:00:00+00:00','2026-07-25T01:01:00+00:00',
                 '2026-07-25T00:00:00+00:00','2026-07-25T00:00:00+00:00',
                 '[{"time_utc":"2026-07-25T02:00:00+00:00","temp_c":32}]'),
            )
            db.execute(
                "INSERT INTO external_forecasts VALUES(?,?,?,?,?,?,?,?)",
                ('ZBAA','2026-07-25','ecmwf_ifs025','ok',36.0,
                 '2026-07-25T01:00:00+00:00','2026-07-25T01:01:00+00:00',
                 '[{"time_utc":"2026-07-25T02:00:00+00:00","temp_c":33}]'),
            )
            observation_sql = (
                "INSERT INTO weather_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            )
            db.execute(observation_sql, (
                'ZBAA','metar','ok','2026-07-25T01:00:00+00:00',30.0,20.0,50.0,
                0.0,20.0,180.0,5.0,'kt',None,10000.0,1005.0,None,None,None,
                '[]',None,'METAR','2026-07-25T01:05:00+00:00','2026-07-25T01:05:00+00:00',
            ))
            db.execute(observation_sql, (
                'ZBAA','metar','ok','2026-07-25T02:00:00+00:00',35.0,20.0,40.0,
                0.0,20.0,180.0,5.0,'kt',None,10000.0,1004.0,None,None,None,
                '[]',None,'METAR','2026-07-25T02:10:00+00:00','2026-07-25T02:10:00+00:00',
            ))
            db.commit()
            db.close()

            builder = DatasetBuilderV2(path)
            try:
                exact_cutoff = builder.build(10, 0)
                delayed_decision = builder.build(
                    10, 0, datetime(2026, 7, 25, 2, 15, tzinfo=timezone.utc)
                )
            finally:
                builder.close()
            self.assertEqual(float(exact_cutoff.iloc[0]["observed_max"]), 30.0)
            self.assertEqual(float(delayed_decision.iloc[0]["observed_max"]), 35.0)
            self.assertEqual(
                delayed_decision.iloc[0]["latest_observation_fetched_at_utc"],
                "2026-07-25T02:10:00+00:00",
            )

    def test_builder_merges_fast_metar_with_point_in_time_cutoff(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fast_point_in_time.sqlite3"
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE stations (station_id TEXT PRIMARY KEY,latitude REAL,longitude REAL,timezone TEXT);
                CREATE TABLE events (event_id TEXT PRIMARY KEY,target_date TEXT,city TEXT,station_id TEXT,winning_range TEXT);
                CREATE TABLE windy_forecasts (station_id TEXT,target_date TEXT,model TEXT,status TEXT,forecast_max_c REAL,
                    slot_utc TEXT,fetched_at_utc TEXT,model_ref_time_utc TEXT,model_updated_at_utc TEXT,points_json TEXT);
                CREATE TABLE external_forecasts (station_id TEXT,target_date TEXT,model TEXT,status TEXT,forecast_max_c REAL,
                    slot_utc TEXT,fetched_at_utc TEXT,points_json TEXT);
                CREATE TABLE weather_observations (
                    station_id TEXT,source TEXT,status TEXT,observation_time_utc TEXT,temperature_c REAL,dewpoint_c REAL,
                    relative_humidity REAL,precipitation_mm REAL,cloud_cover_pct REAL,wind_direction_deg REAL,
                    wind_speed REAL,wind_speed_unit TEXT,wind_gust REAL,visibility_m REAL,pressure_hpa REAL,
                    solar_radiation_wm2 REAL,direct_radiation_wm2 REAL,diffuse_radiation_wm2 REAL,sky_conditions_json TEXT,
                    weather_code TEXT,metar_type TEXT,slot_utc TEXT,fetched_at_utc TEXT
                );
                CREATE TABLE fast_metar_reports (
                    station_id TEXT,observation_time_utc TEXT,report_time_utc TEXT,receipt_time_utc TEXT,
                    metar_type TEXT,raw_metar TEXT,payload_json TEXT,first_fetched_at_utc TEXT,last_fetched_at_utc TEXT
                );
                """
            )
            db.execute("INSERT INTO stations VALUES('ZBAA',40.08,116.58,'Asia/Shanghai')")
            db.execute("INSERT INTO events VALUES('e1','2026-07-25','Beijing','ZBAA','35 C')")
            db.execute(
                "INSERT INTO windy_forecasts VALUES(?,?,?,?,?,?,?,?,?,?)",
                ('ZBAA','2026-07-25','mblue','ok',34.0,'2026-07-25T01:00:00+00:00',
                 '2026-07-25T01:01:00+00:00','2026-07-25T00:00:00+00:00','2026-07-25T00:00:00+00:00','[]'),
            )
            db.execute(
                "INSERT INTO external_forecasts VALUES(?,?,?,?,?,?,?,?)",
                ('ZBAA','2026-07-25','ecmwf_ifs025','ok',34.0,'2026-07-25T01:00:00+00:00',
                 '2026-07-25T01:01:00+00:00','[]'),
            )
            observation_sql = "INSERT INTO weather_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            db.execute(observation_sql, (
                'ZBAA','metar','ok','2026-07-25T01:00:00+00:00',30.0,20.0,50.0,0.0,20.0,180.0,
                5.0,'kt',None,10000.0,1005.0,None,None,None,'[]',None,'METAR',
                '2026-07-25T01:05:00+00:00','2026-07-25T01:05:00+00:00',
            ))
            fast_insert = "INSERT INTO fast_metar_reports VALUES(?,?,?,?,?,?,?,?,?)"
            db.execute(fast_insert, (
                'ZBAA','2026-07-25T01:30:00+00:00','2026-07-25T01:30:00+00:00',
                '2026-07-25T01:35:00+00:00','METAR','ZBAA 250130Z 18005KT 9999 FEW020 35/20 Q1005',
                '{"temp":35,"dewp":20,"wdir":180,"wspd":5}',
                '2026-07-25T01:35:00+00:00','2026-07-25T01:35:00+00:00',
            ))
            db.execute(fast_insert, (
                'ZBAA','2026-07-25T01:45:00+00:00','2026-07-25T01:45:00+00:00',
                '2026-07-25T02:05:00+00:00','METAR','ZBAA 250145Z 18005KT 9999 FEW020 38/20 Q1005',
                '{"temp":38,"dewp":20,"wdir":180,"wspd":5}',
                '2026-07-25T02:05:00+00:00','2026-07-25T02:05:00+00:00',
            ))
            db.commit()
            db.close()

            builder = DatasetBuilderV2(path)
            try:
                frame = builder.build(10, 0)
            finally:
                builder.close()
            self.assertEqual(len(frame), 1)
            row = frame.iloc[0]
            self.assertEqual(float(row["observed_max"]), 35.0)
            self.assertEqual(int(row["observation_count"]), 2)
            self.assertEqual(row["latest_observation_time_utc"], "2026-07-25T01:30:00+00:00")
            self.assertEqual(row["latest_observation_fetched_at_utc"], "2026-07-25T01:35:00+00:00")


if __name__ == "__main__":
    unittest.main()
