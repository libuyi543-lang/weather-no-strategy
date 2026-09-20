import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from weather_market_monitor import WeatherMarketMonitor
from weather_metar_fast_collector import FastMetarCollector


class FastMetarCollectorTests(unittest.TestCase):
    def test_batch_request_persists_metar_and_speci_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = json.loads((Path(__file__).resolve().parent / "monitor_config.json").read_text())
            config["databasePath"] = str(root / "monitor.sqlite3")
            config["reportDirectory"] = str(root / "reports")
            monitor = WeatherMarketMonitor(config)
            monitor.db.execute(
                """
                INSERT INTO stations(station_id,city,latitude,longitude,timezone,first_seen_utc,last_seen_utc)
                VALUES('ZSPD','Shanghai',31.14,121.8,'Asia/Shanghai','2026-07-27T00:00:00+00:00','2026-07-27T00:00:00+00:00')
                """
            )
            monitor.db.commit()
            monitor.close()
            collector = FastMetarCollector(config)
            collector.client.get = Mock(return_value=[
                {
                    "icaoId": "ZSPD", "obsTime": 1785142800,
                    "reportTime": "2026-07-27T05:00:00Z",
                    "receiptTime": "2026-07-27T05:03:10Z", "metarType": "SPECI",
                    "rawOb": "SPECI ZSPD 270500Z 18005MPS 9999 SCT020 32/24 Q1002",
                    "lat": 31.14, "lon": 121.8, "temp": 32, "dewp": 24,
                }
            ])
            try:
                result = collector.collect_once()
                row = collector.db.execute("SELECT * FROM fast_metar_reports").fetchone()
                self.assertEqual(result["station_ids"], ["ZSPD"])
                self.assertEqual(row["metar_type"], "SPECI")
                self.assertEqual(row["receipt_time_utc"], "2026-07-27T05:03:10+00:00")
                collector.client.get.assert_called_once()
                self.assertIn("ZSPD", collector.client.get.call_args.args[1]["ids"])
                self.assertEqual(collector.client.get.call_args.args[1]["hours"], 6)
                collector.collect_once()
                self.assertEqual(
                    collector.db.execute("SELECT COUNT(*) FROM fast_metar_reports").fetchone()[0], 1
                )
            finally:
                collector.close()


if __name__ == "__main__":
    unittest.main()
