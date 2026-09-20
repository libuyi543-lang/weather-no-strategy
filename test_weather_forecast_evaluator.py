import sqlite3
import unittest

from weather_forecast_evaluator import WeatherForecastEvaluator


class ForecastEvaluatorTests(unittest.TestCase):
    def test_refresh_pairs_exact_settlements_and_calibration_deduplicates_snapshots(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(
            """
            CREATE TABLE stations(station_id TEXT PRIMARY KEY,timezone TEXT);
            CREATE TABLE events(event_id TEXT PRIMARY KEY,city TEXT,station_id TEXT,target_date TEXT,
                                resolved_at_utc TEXT,winning_market_id TEXT);
            CREATE TABLE markets(market_id TEXT PRIMARY KEY,bucket_low REAL,bucket_high REAL,bucket_unit TEXT);
            CREATE TABLE windy_forecasts(
                station_id TEXT,target_date TEXT,model TEXT,status TEXT,slot_utc TEXT,
                model_updated_at_utc TEXT,model_ref_time_utc TEXT,forecast_max_c REAL);
            CREATE TABLE external_forecasts(
                station_id TEXT,target_date TEXT,model TEXT,status TEXT,slot_utc TEXT,forecast_max_c REAL);
            """
        )
        db.execute("INSERT INTO stations VALUES('ZSPD','Asia/Shanghai')")
        for index, (event_id, day, final, forecast) in enumerate(
            (("e1", "2026-07-21", 33, 32.4), ("e2", "2026-07-22", 35, 34.2))
        ):
            market_id = f"m{index}"
            db.execute("INSERT INTO markets VALUES(?,?,?,?)", (market_id, final, final, "C"))
            db.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?)",
                (event_id, "Shanghai", "ZSPD", day, f"{day}T16:00:00+00:00", market_id),
            )
            for slot, value in ((f"{day}T02:00:00+00:00", forecast - 1), (f"{day}T02:30:00+00:00", forecast)):
                db.execute(
                    "INSERT INTO windy_forecasts VALUES(?,?,?,?,?,?,?,?)",
                    ("ZSPD", day, "mblue", "ok", slot, slot, slot, value),
                )
        evaluator = WeatherForecastEvaluator(db)
        self.assertEqual(evaluator.refresh(), 4)
        self.assertEqual(evaluator.refresh(), 0)
        calibration = evaluator.calibration("Shanghai", "2026-07-24", 630)
        self.assertEqual(len(calibration), 1)
        self.assertEqual(calibration[0]["model"], "meteoblue")
        self.assertEqual(calibration[0]["sampleCount"], 2)
        self.assertFalse(calibration[0]["sampleSufficient"])

    def test_refresh_revisits_existing_snapshots_when_resolution_arrives(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(
            """
            CREATE TABLE stations(station_id TEXT PRIMARY KEY,timezone TEXT);
            CREATE TABLE events(event_id TEXT PRIMARY KEY,city TEXT,station_id TEXT,target_date TEXT,
                                resolved_at_utc TEXT,winning_market_id TEXT);
            CREATE TABLE markets(market_id TEXT PRIMARY KEY,bucket_low REAL,bucket_high REAL,bucket_unit TEXT);
            CREATE TABLE windy_forecasts(
                station_id TEXT,target_date TEXT,model TEXT,status TEXT,slot_utc TEXT,
                model_updated_at_utc TEXT,model_ref_time_utc TEXT,forecast_max_c REAL);
            CREATE TABLE external_forecasts(
                station_id TEXT,target_date TEXT,model TEXT,status TEXT,slot_utc TEXT,forecast_max_c REAL);
            INSERT INTO stations VALUES('ZSPD','Asia/Shanghai');
            INSERT INTO events VALUES('e1','Shanghai','ZSPD','2026-07-21',NULL,NULL);
            INSERT INTO windy_forecasts VALUES(
                'ZSPD','2026-07-21','mblue','ok','2026-07-21T02:00:00+00:00',NULL,NULL,32.5
            );
            """
        )
        evaluator = WeatherForecastEvaluator(db)
        self.assertEqual(evaluator.refresh(), 1)
        self.assertEqual(evaluator.refresh(), 0)
        db.execute("INSERT INTO markets VALUES('m1',33,33,'C')")
        db.execute(
            "UPDATE events SET resolved_at_utc='2026-07-22T00:00:00+00:00',winning_market_id='m1'"
        )
        self.assertEqual(evaluator.refresh(), 1)
        row = db.execute("SELECT * FROM weather_forecast_evaluations").fetchone()
        self.assertEqual(row["final_settlement_c"], 33.0)
        self.assertEqual(row["resolution_exact"], 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
