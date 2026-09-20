import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import joblib
import pandas as pd

from research.weather_visibility_challenger import (
    VISIBILITY_FEATURES,
    train_visibility_artifact,
)


class WeatherVisibilityChallengerTests(unittest.TestCase):
    def test_artifact_is_visibility_only_shadow_and_excludes_target_date(self):
        frame = pd.DataFrame({
            "target_date": ["2026-07-28", "2026-07-29"],
            "resolved": [True, True],
            "visibility_log_m": [9.0, 8.0],
        })
        model = "visibility_model"
        baseline = {"events": 1, "metrics": {"maeC": 0.8}}
        challenger = {"events": 1, "metrics": {"maeC": 0.7}}

        with TemporaryDirectory() as directory:
            artifact_path = Path(directory) / "visibility.joblib"
            with patch(
                "research.weather_visibility_challenger.build_visibility_frame",
                return_value=frame,
            ), patch(
                "research.weather_visibility_challenger.walk_forward",
                side_effect=[baseline, challenger],
            ) as evaluator, patch(
                "research.weather_visibility_challenger.make_model",
                return_value=model,
            ) as trainer:
                report, written_path = train_visibility_artifact(
                    Path(directory) / "weather.sqlite3", "2026-07-29",
                    datetime(2026, 7, 29, tzinfo=timezone.utc), artifact_path,
                )

            artifact = joblib.load(written_path)
            self.assertEqual(VISIBILITY_FEATURES, ["visibility_log_m"])
            self.assertEqual(report["trained_through"], "2026-07-28")
            self.assertEqual(report["train_rows"], 1)
            self.assertTrue(artifact["shadow_only"])
            self.assertFalse(artifact["authoritative"])
            self.assertEqual(artifact["feature"], "visibility_log_m")
            self.assertEqual(evaluator.call_args_list[0].args[1], [])
            self.assertEqual(evaluator.call_args_list[1].args[1], ["visibility_log_m"])
            trained_frame, trained_features = trainer.call_args.args
            self.assertEqual(trained_frame["target_date"].tolist(), ["2026-07-28"])
            self.assertEqual(trained_features, ["visibility_log_m"])


if __name__ == "__main__":
    unittest.main()
