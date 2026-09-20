import unittest

import numpy as np
import pandas as pd

from research.weather_ridge_feature_ablation import add_derived_fields, metrics


class WeatherRidgeFeatureAblationTests(unittest.TestCase):
    def test_wind_direction_is_encoded_circularly(self):
        frame = add_derived_fields(pd.DataFrame({
            "wind_direction_deg": [0.0, 360.0, 90.0],
            "wind_gust_kt": [None, 20.0, None],
            "visibility_m": [10000.0, 10000.0, 5000.0],
        }))
        self.assertAlmostEqual(frame.loc[0, "wind_direction_cos"], frame.loc[1, "wind_direction_cos"])
        self.assertAlmostEqual(frame.loc[2, "wind_direction_sin"], 1.0)
        self.assertEqual(frame["wind_gust_present"].tolist(), [0.0, 1.0, 0.0])

    def test_metrics_use_integer_bucket_coverage(self):
        result = metrics(np.array([30.0, 30.0]), np.array([31.4, 32.0]))
        self.assertEqual(result["centerBucketAccuracy"], 0.0)
        self.assertEqual(result["threeBucketCoverage"], 0.5)


if __name__ == "__main__":
    unittest.main()
