import json
import gzip
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import weather_market_monitor as monitor_module

from weather_market_monitor import (
    JsonClient,
    WeatherMarketMonitor,
    daily_max_from_meteogram,
    meteogram_step_hours,
    normalized_city,
    extract_station_id,
    parse_temperature_bucket,
    next_sample_time,
    interval_floor,
    resolution_precision,
    round_to_precision,
    unit_to_celsius,
    open_meteo_model_forecasts,
    merge_shadow_temperature_forecasts,
    open_meteo_ensemble_daily_max,
    split_multi_location_response,
    forecast_version_hash,
    parsed_metar_fields,
    epoch_to_iso_utc,
)


class ParsingTests(unittest.TestCase):
    def test_ensemble_daily_max_keeps_members_separate(self):
        payload = {
            "hourly": {
                "time": ["2026-07-29T10:00", "2026-07-29T11:00", "2026-07-30T10:00"],
                "temperature_2m": [30, 32, 20],
                "temperature_2m_member01": [31, 33, 21],
                "temperature_2m_member02": [29, 30, 22],
            }
        }
        result = open_meteo_ensemble_daily_max(payload, "2026-07-29")
        self.assertEqual(result["memberCount"], 3)
        self.assertEqual(
            {row["member"]: row["maxC"] for row in result["members"]},
            {"control": 32.0, "member01": 33.0, "member02": 30.0},
        )
        self.assertEqual(result["q50MaxC"], 32.0)

    def test_open_meteo_multi_location_response_preserves_request_order(self):
        rows = split_multi_location_response(
            [{"latitude": 31.1}, {"latitude": 40.0}], 2
        )
        self.assertEqual([row["latitude"] for row in rows], [31.1, 40.0])
        with self.assertRaisesRegex(RuntimeError, "1 locations for 2"):
            split_multi_location_response({"latitude": 31.1}, 2)

    def test_forecast_version_hash_ignores_fetch_side_current_weather(self):
        parsed = {
            "max_c": 33.0,
            "peak_local": "2026-07-21T14:00+08:00",
            "points": [{"time_local": "2026-07-21T14:00+08:00", "temp_c": 33.0}],
        }
        first = forecast_version_hash("ecmwf_ifs025", "2026-07-21", parsed)
        second = forecast_version_hash("ecmwf_ifs025", "2026-07-21", dict(parsed))
        self.assertEqual(first, second)

    def test_optional_orderbook_404_is_not_retried_or_counted_as_failure(self):
        error = monitor_module.HTTPError("https://example.test/book", 404, "Not Found", {}, None)
        with patch("weather_market_monitor.urlopen", side_effect=error) as request:
            result = JsonClient(timeout=1, retries=3).get(
                "https://example.test/book", allow_not_found=True
            )
        self.assertIsNone(result)
        self.assertEqual(request.call_count, 1)

    def test_open_meteo_models_are_split_into_replayable_local_and_utc_points(self):
        payload = {
            "hourly": {
                "time": ["2026-07-21T09:00", "2026-07-21T10:00", "2026-07-22T10:00"],
                "temperature_2m_ecmwf_ifs025": [31.0, 33.0, 30.0],
                "temperature_2m_gfs_seamless": [30.0, 32.0, 29.0],
            }
        }
        rows = open_meteo_model_forecasts(payload, "2026-07-21", "Asia/Shanghai")
        self.assertEqual({row["model"] for row in rows}, {"ecmwf_ifs025", "gfs_seamless"})
        ecmwf = next(row for row in rows if row["model"] == "ecmwf_ifs025")
        self.assertEqual(ecmwf["max_c"], 33.0)
        self.assertEqual(ecmwf["peak_local"], "2026-07-21T10:00+08:00")
        self.assertEqual(ecmwf["points"][1]["time_utc"], "2026-07-21T02:00:00+00:00")

    def test_aifs_is_collected_as_an_independent_forecast_model(self):
        payload = {
            "hourly": {
                "time": ["2026-07-21T10:00", "2026-07-21T11:00"],
                "temperature_2m_ecmwf_ifs025": [31.0, 32.0],
                "temperature_2m_ecmwf_aifs025_single": [30.5, 33.0],
            }
        }
        rows = open_meteo_model_forecasts(
            payload, "2026-07-21", "Asia/Shanghai",
            ["ecmwf_ifs025", "ecmwf_aifs025_single"],
        )
        maxima = {row["model"]: row["max_c"] for row in rows}
        self.assertEqual(
            maxima, {"ecmwf_ifs025": 32.0, "ecmwf_aifs025_single": 33.0}
        )

    def test_aifs_config_is_shadow_collection_only(self):
        config = json.loads(
            (Path(__file__).resolve().parent / "monitor_config.json").read_text()
        )
        self.assertNotIn("ecmwf_aifs025_single", config["externalForecastModels"])
        self.assertIn("ecmwf_aifs025_single", config["shadowExternalForecastModels"])
        self.assertNotIn("ecmwf_aifs025_single", config["ensembleForecastModels"])

    def test_shadow_temperature_is_aligned_without_replacing_primary_fields(self):
        primary = {
            "hourly": {
                "time": ["2026-07-21T10:00", "2026-07-21T11:00"],
                "temperature_2m_ecmwf_ifs025": [31.0, 32.0],
                "cloud_cover_ecmwf_ifs025": [20, 30],
            }
        }
        shadow = {
            "hourly": {
                "time": ["2026-07-21T11:00", "2026-07-21T10:00"],
                "temperature_2m": [33.0, 30.5],
            }
        }
        merged = merge_shadow_temperature_forecasts(
            primary, shadow, ["ecmwf_aifs025_single"]
        )
        self.assertEqual(
            merged["hourly"]["temperature_2m_ecmwf_aifs025_single"], [30.5, 33.0]
        )
        self.assertEqual(
            merged["hourly"]["temperature_2m_ecmwf_ifs025"], [31.0, 32.0]
        )
        self.assertEqual(merged["hourly"]["cloud_cover_ecmwf_ifs025"], [20, 30])

    def test_single_open_meteo_model_uses_generic_temperature_field(self):
        payload = {
            "hourly": {
                "time": ["2026-07-21T09:00", "2026-07-21T10:00"],
                "temperature_2m": [31.0, 33.0],
            }
        }
        rows = open_meteo_model_forecasts(payload, "2026-07-21", "Asia/Shanghai", ["ecmwf_ifs025"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "ecmwf_ifs025")
        self.assertEqual(rows[0]["max_c"], 33.0)

    def test_open_meteo_future_process_preserves_cloud_radiation_rain_and_wind(self):
        payload = {
            "hourly": {
                "time": ["2026-07-21T10:00", "2026-07-21T11:00"],
                "temperature_2m": [30.0, 31.0],
                "cloud_cover": [80, 20],
                "shortwave_radiation": [250, 620],
                "precipitation_probability": [40, 10],
                "wind_speed_10m": [8, 12],
                "wind_direction_10m": [180, 210],
            }
        }
        row = open_meteo_model_forecasts(payload, "2026-07-21", "Asia/Shanghai", ["ecmwf_ifs025"])[0]
        self.assertEqual(row["points"][0]["cloud_cover_pct"], 80.0)
        self.assertEqual(row["points"][1]["shortwave_radiation_wm2"], 620.0)
        self.assertEqual(row["points"][0]["precipitation_probability_pct"], 40.0)
        self.assertEqual(row["points"][1]["wind_direction_deg"], 210.0)

    def test_extracts_wunderground_station(self):
        source = "https://www.wunderground.com/history/daily/gb/london/EGLC"
        self.assertEqual(extract_station_id(source, ""), "EGLC")

    def test_extracts_noaa_site_query(self):
        rules = "Available at https://www.weather.gov/wrh/timeseries?site=LLBG."
        self.assertEqual(extract_station_id("", rules), "LLBG")

    def test_maps_hong_kong_observatory(self):
        rules = "Highest temperature recorded by the Hong Kong Observatory in degrees Celsius."
        self.assertEqual(extract_station_id("", rules), "HKO")

    def test_temperature_buckets(self):
        self.assertEqual(parse_temperature_bucket("21 C or below"), (None, 21.0, "C"))
        self.assertEqual(parse_temperature_bucket("22-23 C"), (22.0, 23.0, "C"))
        self.assertEqual(parse_temperature_bucket("91 F or higher"), (91.0, None, "F"))

    def test_resolution_rounding_matches_source_precision(self):
        self.assertEqual(resolution_precision("measures temperatures to whole degrees Celsius"), 1.0)
        self.assertEqual(resolution_precision("measures Celsius to one decimal place"), 0.1)
        self.assertEqual(round_to_precision(30.5, 1.0), 31.0)
        self.assertEqual(round_to_precision(30.74, 0.1), 30.7)
        self.assertAlmostEqual(unit_to_celsius(95.0, "F"), 35.0)

    def test_daily_max_uses_station_local_date(self):
        timestamps = [
            int(datetime(2026, 7, 18, hour, tzinfo=timezone.utc).timestamp() * 1000)
            for hour in (0, 6, 12)
        ]
        payload = {
            "data": {
                "hours": timestamps,
                "temp-surface": [290.15, 301.15, 296.15],
                "dewpoint-surface": [285.15, 290.15, 289.15],
                "rh-surface": [70, 45, 60],
                "wind-surface": [2, 4, 3],
                "windDir-surface": [120, 180, 220],
                "cloud-950h": [80, 10, 30],
                "cloud-500h": [20, 40, 10],
            }
        }
        maximum, peak, points = daily_max_from_meteogram(payload, "2026-07-18", "Asia/Shanghai")
        self.assertAlmostEqual(maximum, 28.0)
        self.assertIn("14:00:00+08:00", peak)
        self.assertEqual(len(points), 3)
        self.assertEqual(points[1]["dewpoint_c"], 17.0)
        self.assertEqual(points[0]["cloud_low_pct"], 80.0)
        self.assertEqual(points[1]["cloud_high_pct"], 40.0)

    def test_meteogram_step_requires_consistent_hourly_cadence(self):
        timestamps = [
            int(datetime(2026, 7, 18, hour, tzinfo=timezone.utc).timestamp() * 1000)
            for hour in (0, 1, 2)
        ]
        payload = {"header": {"step": 1}, "data": {"hours": timestamps}}
        self.assertEqual(meteogram_step_hours(payload), 1.0)

    def test_meteogram_step_accepts_premium_mixed_horizon(self):
        timestamps = [
            int(datetime(2026, 7, 18, hour, tzinfo=timezone.utc).timestamp() * 1000)
            for hour in (0, 1, 2, 5, 8)
        ]
        payload = {"header": {"step": 1}, "data": {"hours": timestamps}}
        self.assertEqual(meteogram_step_hours(payload), 1.0)

    def test_meteogram_step_rejects_header_timestamp_mismatch(self):
        timestamps = [
            int(datetime(2026, 7, 18, hour, tzinfo=timezone.utc).timestamp() * 1000)
            for hour in (0, 3, 6)
        ]
        payload = {"header": {"step": 1}, "data": {"hours": timestamps}}
        with self.assertRaises(RuntimeError):
            meteogram_step_hours(payload)

    def test_normalized_city_handles_english_and_chinese_shenzhen(self):
        self.assertEqual(normalized_city(" Shenzhen "), "shenzhen")
        self.assertEqual(normalized_city("深圳"), "深圳")

    def test_python_metar_enrichment_extracts_gust_pressure_and_cloud_layers(self):
        parsed = parsed_metar_fields(
            "METAR NZWN 231530Z AUTO 36022G41KT 9999 BKN025/// OVC032/// 13/08 Q1009 NOSIG"
        )
        self.assertEqual(parsed["parser_status"], "ok")
        self.assertEqual(parsed["wind_gust"], 41.0)
        self.assertEqual(parsed["pressure_hpa"], 1009.0)
        self.assertIn('"cover":"BKN"', parsed["sky_conditions_json"])

    def test_epoch_to_iso_utc_preserves_metar_observation_time(self):
        self.assertEqual(epoch_to_iso_utc(1784820600), "2026-07-23T15:30:00+00:00")


class TimezoneAndDatabaseTests(unittest.TestCase):
    def test_resolution_label_separates_official_value_from_metar_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = json.loads((Path(__file__).resolve().parent / "monitor_config.json").read_text())
            config["databasePath"] = str(root / "monitor.sqlite3")
            config["reportDirectory"] = str(root / "reports")
            monitor = WeatherMarketMonitor(config)
            try:
                monitor.db.execute(
                    "INSERT INTO runs(slot_utc,started_at_utc,status) VALUES(?,?,?)",
                    ("2026-07-29T00:00:00+00:00", "2026-07-29T00:00:00+00:00", "completed"),
                )
                monitor.db.execute(
                    """INSERT INTO stations(
                        station_id,city,timezone,first_seen_utc,last_seen_utc
                    ) VALUES('ZSPD','Shanghai','Asia/Shanghai','x','x')"""
                )
                monitor.db.execute(
                    """INSERT INTO events(
                        event_id,city,target_date,station_id,rules,resolution_source,
                        first_seen_utc,last_seen_utc,resolved_at_utc,winning_market_id,winning_range
                    ) VALUES('e','Shanghai','2026-07-28','ZSPD',?,?,'x','x','2026-07-29T00:00:00Z','m','35 C')""",
                    ("measures temperatures to whole degrees Celsius", "https://example.test/ZSPD"),
                )
                monitor.db.execute(
                    """INSERT INTO markets(
                        market_id,event_id,outcome_range,bucket_low,bucket_high,bucket_unit,
                        first_seen_utc,last_seen_utc
                    ) VALUES('m','e','35 C',35,35,'C','x','x')"""
                )
                monitor.db.execute(
                    """INSERT INTO weather_observations(
                        run_id,slot_utc,station_id,sample_local_date,sample_local_time,
                        timezone,source,observation_time_utc,temperature_c,status,fetched_at_utc
                    ) VALUES(1,'2026-07-28T06:00:00Z','ZSPD','2026-07-28','14:00',
                             'Asia/Shanghai','metar','2026-07-28T06:00:00Z',34,'ok','x')"""
                )
                self.assertEqual(monitor.refresh_resolution_labels(), 1)
                self.assertEqual(monitor.refresh_resolution_labels(), 0)
                row = monitor.db.execute("SELECT * FROM weather_resolution_labels").fetchone()
                self.assertEqual(row["official_temperature_c"], 35.0)
                self.assertEqual(row["station_observed_max_c"], 34.0)
                self.assertEqual(row["station_audit_delta_c"], -1.0)
                self.assertEqual(row["label_status"], "exact_at_declared_precision")
            finally:
                monitor.close()

    def test_reports_are_throttled_without_settlement_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = json.loads((Path(__file__).resolve().parent / "monitor_config.json").read_text())
            config["databasePath"] = str(root / "monitor.sqlite3")
            config["reportDirectory"] = str(root / "reports")
            config["reportRefreshIntervalMinutes"] = 1440
            monitor = WeatherMarketMonitor(config)
            try:
                now = datetime.now(timezone.utc)
                for name in (
                    "status_latest.json",
                    "forecast_calibration_latest.json",
                    "forecast_cutoff_status_latest.json",
                    "shadow_ablation_latest.json",
                ):
                    (monitor.report_dir / name).write_text("{}", encoding="utf-8")
                self.assertFalse(monitor._reports_due(0, now))
                self.assertTrue(monitor._reports_due(1, now))
                (monitor.report_dir / "status_latest.json").unlink()
                self.assertTrue(monitor._reports_due(0, now))
            finally:
                monitor.close()

    def test_ensemble_capture_marks_provider_run_time_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = json.loads((Path(__file__).resolve().parent / "monitor_config.json").read_text())
            config["databasePath"] = str(root / "monitor.sqlite3")
            config["reportDirectory"] = str(root / "reports")
            monitor = WeatherMarketMonitor(config)
            try:
                run_id = monitor.db.execute(
                    "INSERT INTO runs(slot_utc,started_at_utc,status) VALUES('2026-07-29T02:00:00+00:00','x','running')"
                ).lastrowid
                monitor.db.execute(
                    """INSERT INTO stations(
                        station_id,city,latitude,longitude,timezone,first_seen_utc,last_seen_utc
                    ) VALUES('ZSPD','Shanghai',31.14,121.8,'Asia/Shanghai','x','x')"""
                )
                tracked = [{
                    "station_id": "ZSPD", "target_date": "2026-07-29",
                    "latitude": 31.14, "longitude": 121.8, "timezone": "Asia/Shanghai",
                }]
                payload = {"generationtime_ms": 2.5, "hourly": {
                    "time": ["2026-07-29T10:00", "2026-07-29T11:00"],
                    "temperature_2m": [30, 32],
                    "temperature_2m_member01": [31, 33],
                }}
                with patch.object(monitor, "_tracked_station_dates", return_value=tracked), patch.object(
                    monitor, "_fetch_open_meteo_ensemble_batch", return_value={"ZSPD": payload}
                ):
                    written = monitor.capture_ensemble_forecasts(
                        run_id, datetime(2026, 7, 29, 2, tzinfo=timezone.utc)
                    )
                self.assertEqual(written, 1)
                row = monitor.db.execute("SELECT * FROM ensemble_forecasts").fetchone()
                self.assertEqual(row["member_count"], 2)
                self.assertIsNone(row["model_run_time_utc"])
                self.assertEqual(row["model_run_confidence"], "unavailable")
                audit = monitor.db.execute("SELECT * FROM forecast_model_runs").fetchone()
                self.assertEqual(audit["run_time_source"], "provider_not_exposed")
            finally:
                monitor.close()

    def test_source_version_only_marks_changed_forecast_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = json.loads((Path(__file__).resolve().parent / "monitor_config.json").read_text())
            config["databasePath"] = str(root / "monitor.sqlite3")
            config["reportDirectory"] = str(root / "reports")
            monitor = WeatherMarketMonitor(config)
            try:
                self.assertTrue(monitor._register_source_version(
                    "open_meteo", "ZSPD", "2026-07-21", "ecmwf_ifs025", "hash-a"
                ))
                self.assertFalse(monitor._register_source_version(
                    "open_meteo", "ZSPD", "2026-07-21", "ecmwf_ifs025", "hash-a"
                ))
                self.assertTrue(monitor._register_source_version(
                    "open_meteo", "ZSPD", "2026-07-21", "ecmwf_ifs025", "hash-b"
                ))
            finally:
                monitor.close()

    def test_open_meteo_poll_backoff_prevents_repeated_429_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = json.loads((Path(__file__).resolve().parent / "monitor_config.json").read_text())
            config["databasePath"] = str(root / "monitor.sqlite3")
            config["reportDirectory"] = str(root / "reports")
            monitor = WeatherMarketMonitor(config)
            try:
                now = datetime(2026, 7, 27, 2, tzinfo=timezone.utc)
                self.assertTrue(monitor._source_poll_due("open_meteo", now))
                monitor._update_source_poll_state("open_meteo", now, False, "HTTP 429")
                self.assertFalse(monitor._source_poll_due("open_meteo", now))
                later = datetime(2026, 7, 27, 2, 31, tzinfo=timezone.utc)
                self.assertTrue(monitor._source_poll_due("open_meteo", later))
            finally:
                monitor.close()
    def test_raw_weather_payload_is_gzipped_and_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = json.loads((Path(__file__).resolve().parent / "monitor_config.json").read_text())
            config["databasePath"] = str(root / "monitor.sqlite3")
            config["reportDirectory"] = str(root / "reports")
            monitor = WeatherMarketMonitor(config)
            try:
                run_id = monitor.db.execute(
                    "INSERT INTO runs(slot_utc,started_at_utc,status) VALUES('2026-07-21T02:00:00+00:00','2026-07-21T02:00:00+00:00','running')"
                ).lastrowid
                monitor.db.execute(
                    """
                    INSERT INTO stations(station_id,city,latitude,longitude,timezone,first_seen_utc,last_seen_utc)
                    VALUES('ZSPD','Shanghai',31.14,121.8,'Asia/Shanghai','2026-07-21T00:00:00+00:00','2026-07-21T00:00:00+00:00')
                    """
                )
                payload_id = monitor._store_raw_weather_payload(
                    run_id, datetime(2026, 7, 21, 2, tzinfo=timezone.utc), "ZSPD", None,
                    "open_meteo_multi", {"current": {"temperature_2m": 31.2}},
                )
                row = monitor.db.execute(
                    "SELECT body_gzip,sha256 FROM raw_weather_payloads WHERE payload_id=?", (payload_id,)
                ).fetchone()
                restored = json.loads(gzip.decompress(row["body_gzip"]).decode())
                self.assertEqual(restored["current"]["temperature_2m"], 31.2)
                self.assertEqual(len(row["sha256"]), 64)
            finally:
                monitor.close()

    def test_next_sample_time_rolls_only_once(self):
        before = datetime(2026, 7, 20, 12, 29, 50, tzinfo=timezone.utc)
        target = next_sample_time(before, 30)
        self.assertEqual(target, datetime(2026, 7, 20, 12, 30, tzinfo=timezone.utc))

        after = datetime(2026, 7, 20, 12, 30, 1, tzinfo=timezone.utc)
        with (
            patch.object(monitor_module, "utc_now", side_effect=[before, before, after]),
            patch.object(monitor_module.time, "sleep") as mocked_sleep,
        ):
            monitor_module.sleep_until_next_slot(30, lambda: False)
        mocked_sleep.assert_called_once_with(10.0)

    def test_interval_floor_aligns_to_half_hour(self):
        value = datetime(2026, 7, 20, 12, 47, 12, tzinfo=timezone.utc)
        self.assertEqual(interval_floor(value, 30), datetime(2026, 7, 20, 12, 30, tzinfo=timezone.utc))

    def test_dst_days_have_23_and_25_slots(self):
        now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        spring, _ = WeatherMarketMonitor._local_day_slots("2026-03-08", "America/New_York", now)
        autumn, _ = WeatherMarketMonitor._local_day_slots("2026-11-01", "America/New_York", now)
        self.assertEqual(len(spring), 23)
        self.assertEqual(len(autumn), 25)

    def test_half_hour_days_have_46_and_50_slots_across_dst(self):
        now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        spring, _ = WeatherMarketMonitor._local_day_slots("2026-03-08", "America/New_York", now, 30)
        autumn, _ = WeatherMarketMonitor._local_day_slots("2026-11-01", "America/New_York", now, 30)
        self.assertEqual(len(spring), 46)
        self.assertEqual(len(autumn), 50)

    def test_empty_database_and_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = json.loads(
                (Path(__file__).resolve().parent / "monitor_config.json").read_text(encoding="utf-8")
            )
            config["databasePath"] = str(root / "monitor.sqlite3")
            config["reportDirectory"] = str(root / "reports")
            monitor = WeatherMarketMonitor(config)
            try:
                monitor.write_reports()
                self.assertEqual(monitor.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
                self.assertTrue((root / "reports/status_latest.json").exists())
                self.assertTrue((root / "reports/coverage_latest.csv").exists())
                self.assertTrue((root / "reports/accuracy_latest.csv").exists())
                self.assertTrue((root / "reports/resolutions_latest.csv").exists())
            finally:
                monitor.close()


if __name__ == "__main__":
    unittest.main()
