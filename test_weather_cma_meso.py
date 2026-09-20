import json
import tempfile
import unittest
from pathlib import Path

from weather_cma_meso import CmaMesoAdapter


class CmaMesoAdapterTests(unittest.TestCase):
    def test_explicit_field_mapping_parses_target_day_and_run_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cma.json"
            path.write_text(json.dumps({
                "userId": "user",
                "pwd": "secret",
                "products": {
                    "cma_meso": {
                        "interfaceId": "example",
                        "params": {"staId": "{stationId}", "date": "{targetDateCompact}"},
                        "response": {
                            "rowsPath": "DS",
                            "validTimeField": "valid",
                            "temperatureField": "t2m",
                            "runTimeField": "run",
                            "temperatureUnit": "K",
                            "timeZone": "UTC",
                        },
                    }
                },
            }), encoding="utf-8")
            path.chmod(0o600)
            seen = {}

            def getter(url, params, **kwargs):
                seen.update({"url": url, "params": params, "kwargs": kwargs})
                return {"DS": [
                    {"valid": "202608050500", "t2m": 313.15, "run": "202608042100"},
                    {"valid": "202608050600", "t2m": 306.15, "run": "202608050000"},
                    {"valid": "202608050700", "t2m": 308.15, "run": "202608050000"},
                    {"valid": "202608051700", "t2m": 301.15, "run": "202608050000"},
                ]}

            adapter = CmaMesoAdapter({
                "cma": {"credentialFile": str(path), "apiBaseUrl": "https://example.test/api"}
            }, getter)
            self.assertTrue(adapter.configured)
            _payload, parsed = adapter.fetch(
                station_id="ZSPD", latitude=31.14, longitude=121.8,
                target_date="2026-08-05", timezone_name="Asia/Shanghai",
            )
            self.assertEqual(seen["params"]["staId"], "ZSPD")
            self.assertEqual(seen["params"]["date"], "20260805")
            self.assertEqual(parsed["max_c"], 35.0)
            self.assertEqual(parsed["model_run_time_utc"], "2026-08-05T00:00:00+00:00")
            self.assertEqual(parsed["run_time_confidence"], "high")

    def test_missing_response_mapping_is_not_treated_as_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cma.json"
            path.write_text(json.dumps({
                "products": {"cma_meso": {"interfaceId": "example"}}
            }), encoding="utf-8")
            path.chmod(0o600)
            adapter = CmaMesoAdapter({"cma": {"credentialFile": str(path)}}, lambda *_a, **_k: {})
            self.assertFalse(adapter.configured)
            self.assertIn("field mapping", adapter.error)


if __name__ == "__main__":
    unittest.main()
