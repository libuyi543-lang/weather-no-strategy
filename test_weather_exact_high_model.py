import unittest

import numpy as np
import pandas as pd

from research.weather_exact_high_model import exact_temperature, observed_floor, walk_forward


class WeatherExactHighModelTests(unittest.TestCase):
    def test_exact_temperature_excludes_censored_buckets(self):
        self.assertEqual(exact_temperature("35°C"), 35.0)
        self.assertIsNone(exact_temperature("35°C or higher"))
        self.assertIsNone(exact_temperature("29°C or below"))

    def test_observed_floor_prevents_impossible_prediction(self):
        frame = pd.DataFrame({"observed_max": [35.0, np.nan, 31.0]})
        result = observed_floor(frame, np.array([34.2, 30.5, 31.4]))
        np.testing.assert_allclose(result, [35.0, 30.5, 31.4])

    def test_walk_forward_never_uses_test_date_as_training_data(self):
        rows = []
        cities = ["Shanghai", "Beijing"]
        for day_index, target_date in enumerate(
            ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23"]
        ):
            for city_index, city in enumerate(cities):
                target = 30.0 + city_index + day_index * 0.2
                rows.append({
                    "event_id": f"{target_date}-{city}", "target_date": target_date,
                    "city": city, "resolved": True, "target": target,
                    "mblue": target - 1.0, "ecmwf": target - 0.8,
                    "model_spread": 0.2, "mblue_revision": 0.0, "ecmwf_revision": 0.0,
                    "observed_temp": target - 2.0, "observed_max": target - 2.0,
                    "observed_minus_mblue": -1.0, "dewpoint": target - 7.0,
                    "dewpoint_depression": 5.0, "relative_humidity": 60.0,
                    "wind_speed": 4.0, "temperature_trend_per_hour": 1.0,
                    "observation_age_minutes": 0.0, "observation_count": 10.0,
                    "latitude": 30.0, "longitude": 120.0,
                })
        result = walk_forward(pd.DataFrame(rows))
        self.assertEqual(result["oos_events"], 4)
        self.assertEqual(result["folds"][0]["test_date"], "2026-07-22")
        self.assertEqual(result["folds"][0]["train_event_days"], 4)


if __name__ == "__main__":
    unittest.main()
