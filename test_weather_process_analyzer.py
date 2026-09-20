import json
import sqlite3
import unittest
from datetime import datetime, timezone

from weather_process_analyzer import (
    RemoteSensingCollector,
    analyze_weather_process,
    bearing_deg,
    haversine_km,
    himawari_full_disk_pixel,
    solar_position,
)


UTC = timezone.utc


class WeatherProcessGeometryTests(unittest.TestCase):
    def test_himawari_projection_maps_shanghai_to_expected_eight_tile_cell(self):
        x, y = himawari_full_disk_pixel(31.14, 121.80, tile_count=8, tile_size=550)
        self.assertEqual((int(x // 550), int(y // 550)), (2, 1))

    def test_distance_and_bearing_identify_western_upwind_station(self):
        distance = haversine_km(31.14, 121.80, 31.20, 120.80)
        bearing = bearing_deg(31.14, 121.80, 31.20, 120.80)
        self.assertGreater(distance, 90)
        self.assertLess(distance, 100)
        self.assertTrue(260 <= bearing <= 285)

    def test_solar_position_exposes_remaining_heating_window(self):
        result = solar_position(31.14, 121.80, datetime(2026, 7, 24, 4, tzinfo=UTC))
        self.assertGreater(result["solarElevationDeg"], 60)
        self.assertGreater(result["hoursUntilAstronomicalSunset"], 5)
        self.assertGreater(result["clearSkyShortwaveProxyWm2"], 800)

    def test_failed_shared_metadata_is_attempted_once_per_cycle(self):
        calls = []

        def failed_get(url, **_kwargs):
            calls.append(url)
            raise RuntimeError("temporary outage")

        collector = RemoteSensingCollector(failed_get, retries=0)
        collector.prepare_metadata()
        with self.assertRaisesRegex(RuntimeError, "temporary outage"):
            collector.satellite(31.14, 121.8)
        with self.assertRaisesRegex(RuntimeError, "temporary outage"):
            collector.satellite(31.14, 121.8)
        self.assertEqual(len(calls), 2)  # one RainViewer and one Himawari metadata request

        collector.begin_cycle()
        collector.prepare_metadata()
        self.assertEqual(len(calls), 4)

    def test_jaxa_point_products_use_independent_latest_frames(self):
        def json_get(_url, **_kwargs):
            return {
                "latest": {
                    "L2_SWR": {"date": 202607270810},
                    "L2_CLOT": {"date": 202607270800},
                    "L2_CLTYPE": {"date": 202607270800},
                }
            }

        collector = RemoteSensingCollector(json_get, retries=0)

        def fake_bytes(url):
            value = b"236.550" if "prod=SWR" in url else b"2.630" if "prod=CLOT" in url else b"1"
            collector._bytes_cache[url] = value
            return value

        collector._get_bytes = fake_bytes
        products = collector.jaxa_products(31.1439, 121.805)
        self.assertEqual(products[0]["features"]["shortwaveRadiationWm2"], 236.55)
        self.assertEqual(products[0]["frame_time_utc"], "2026-07-27T08:10:00+00:00")
        self.assertEqual(products[1]["features"]["cloudOpticalThickness"], 2.63)
        self.assertEqual(products[1]["features"]["cloudTypeIsccpCode"], 1)
        self.assertEqual(products[1]["frame_time_utc"], "2026-07-27T08:00:00+00:00")

    def test_jaxa_fill_values_become_missing_retrievals(self):
        def json_get(_url, **_kwargs):
            return {
                "latest": {
                    "L2_SWR": {"date": 202607270810},
                    "L2_CLOT": {"date": 202607270800},
                    "L2_CLTYPE": {"date": 202607270800},
                }
            }

        collector = RemoteSensingCollector(json_get, retries=0)

        def fake_bytes(url):
            value = b"236.550" if "prod=SWR" in url else b"-327.66"
            collector._bytes_cache[url] = value
            return value

        collector._get_bytes = fake_bytes
        cloud = collector.jaxa_products(40.07, 116.60)[1]
        self.assertIsNone(cloud["features"]["cloudOpticalThickness"])
        self.assertIsNone(cloud["features"]["cloudTypeIsccpCode"])
        self.assertFalse(cloud["features"]["opticalThicknessRetrievalAvailable"])


class WeatherProcessDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE station_network_reports (
                primary_station_id TEXT,station_id TEXT,observation_time_utc TEXT,
                temperature_c REAL,dewpoint_c REAL,pressure_hpa REAL,wind_direction_deg REAL,
                wind_speed_kt REAL,wind_gust_kt REAL,distance_km REAL,bearing_from_primary_deg REAL,
                raw_metar TEXT,clouds_json TEXT
            );
            CREATE TABLE weather_observations (
                station_id TEXT,source TEXT,status TEXT,observation_time_utc TEXT,
                temperature_c REAL,dewpoint_c REAL,wind_direction_deg REAL,wind_speed REAL,
                wind_gust REAL,pressure_hpa REAL,sky_conditions_json TEXT,raw_metar TEXT
            );
            CREATE TABLE remote_sensing_snapshots (
                station_id TEXT,slot_utc TEXT,source TEXT,frame_time_utc TEXT,status TEXT,
                quality TEXT,features_json TEXT,error TEXT
            );
            CREATE TABLE windy_forecasts (
                station_id TEXT,target_date TEXT,model TEXT,status TEXT,slot_utc TEXT,
                forecast_max_c REAL,points_json TEXT
            );
            CREATE TABLE external_forecasts (
                station_id TEXT,target_date TEXT,model TEXT,status TEXT,slot_utc TEXT,
                forecast_max_c REAL,points_json TEXT
            );
            """
        )

    def tearDown(self):
        self.db.close()

    def test_joint_observation_signals_detect_convective_cold_pool(self):
        for observed, temp, pressure, wind in (
            ("2026-07-24T03:00:00+00:00", 34.0, 1004.0, 180.0),
            ("2026-07-24T04:00:00+00:00", 32.0, 1005.0, 250.0),
        ):
            self.db.execute(
                "INSERT INTO station_network_reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ZSPD", "ZSPD", observed, temp, 24, pressure, wind, 12, 22, 0, 0, "METAR OVC030", "[]"),
            )
        self.db.execute(
            "INSERT INTO station_network_reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ZSPD", "ZSSS", "2026-07-24T04:00:00+00:00", 31, 24, 1005, 260, 10, None, 35, 270, "METAR BKN030", "[]"),
        )
        radar = {
            "nearestEchoKm": 12,
            "upwindEchoCoverage150Km": 0.18,
            "echoCoverage100KmChange": 0.08,
        }
        self.db.execute(
            "INSERT INTO remote_sensing_snapshots VALUES(?,?,?,?,?,?,?,?)",
            ("ZSPD", "2026-07-24T04:00:00+00:00", "rainviewer", "2026-07-24T04:00:00+00:00", "ok", "proxy", json.dumps(radar), None),
        )
        points = json.dumps([{"time_utc": "2026-07-24T04:00:00+00:00", "temp_c": 33, "shortwave_radiation_wm2": 700}])
        self.db.execute("INSERT INTO windy_forecasts VALUES(?,?,?,?,?,?,?)", ("ZSPD", "2026-07-24", "mblue", "ok", "2026-07-24T04:00:00+00:00", 35, points))
        self.db.execute("INSERT INTO external_forecasts VALUES(?,?,?,?,?,?,?)", ("ZSPD", "2026-07-24", "ecmwf_ifs025", "ok", "2026-07-24T04:00:00+00:00", 35, points))

        state = analyze_weather_process(
            self.db,
            {"station_id": "ZSPD", "city": "Shanghai", "latitude": 31.14, "longitude": 121.80},
            "2026-07-24",
            datetime(2026, 7, 24, 4, tzinfo=UTC),
            {"shanghai": [45, 160]},
        )
        self.assertIn("convective_cold_pool", state["detectedProcesses"])
        self.assertIn("upwind_cloud_or_rain_approach", state["detectedProcesses"])
        self.assertEqual(state["modelRealityComparison"]["meteoblue"]["observationMinusSameHourForecastC"], -1)
        self.assertFalse(state["mechanisticModelAdjustment"]["automaticDegreeCorrectionApplied"])

    def test_official_jaxa_swr_and_cloud_are_exposed_to_process_state(self):
        self.db.execute(
            "INSERT INTO weather_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ZSPD", "metar", "ok", "2026-07-24T04:00:00+00:00", 34, 24, 180, 8, None, 1004, "[]", "METAR CAVOK"),
        )
        for source, features in (
            ("jaxa_himawari_swr_l2", {"shortwaveRadiationWm2": 620}),
            ("jaxa_himawari_cloud_l2", {"cloudOpticalThickness": 1.2, "cloudTypeIsccpCode": 1}),
        ):
            self.db.execute(
                "INSERT INTO remote_sensing_snapshots VALUES(?,?,?,?,?,?,?,?)",
                ("ZSPD", "2026-07-24T04:00:00+00:00", source, "2026-07-24T03:50:00+00:00", "ok", "official", json.dumps(features), None),
            )
        state = analyze_weather_process(
            self.db,
            {"station_id": "ZSPD", "city": "Shanghai", "latitude": 31.14, "longitude": 121.80},
            "2026-07-24",
            datetime(2026, 7, 24, 4, tzinfo=UTC),
            {},
        )
        self.assertEqual(state["solarHeating"]["jaxaShortwaveRadiationWm2"], 620.0)
        self.assertEqual(state["solarHeating"]["jaxaCloudOpticalThickness"], 1.2)


if __name__ == "__main__":
    unittest.main()
