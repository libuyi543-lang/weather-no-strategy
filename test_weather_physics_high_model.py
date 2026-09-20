import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from research.weather_physics_high_model import (
    PhysicsHighModel,
    round_half_up,
    solar_energy_kwh_m2,
)
from research.weather_sounding import SoundingFeatures


UTC = timezone.utc


class WeatherPhysicsHighModelTests(unittest.TestCase):
    def observations(self, temperatures):
        return [
            {
                "observation_time_utc": f"2026-07-25T{hour:02d}:00:00+00:00",
                "temperature_c": temperature,
                "sky_conditions_json": '[{"cover":"FEW"}]',
            }
            for hour, temperature in zip(range(0, len(temperatures)), temperatures)
        ]

    def test_solar_energy_is_positive_during_china_afternoon(self):
        energy = solar_energy_kwh_m2(
            30.57, 103.95,
            datetime(2026, 7, 25, 4, tzinfo=UTC),
            datetime(2026, 7, 25, 10, tzinfo=UTC),
        )
        self.assertGreater(energy, 1.0)

    def test_observed_max_is_irreversible_floor_after_cooling(self):
        model = PhysicsHighModel()
        result = model.predict(
            self.observations([28, 31, 34, 36, 35]),
            30.57, 103.95, ZoneInfo("Asia/Shanghai"),
            datetime(2026, 7, 25, 4, tzinfo=UTC),
        )
        self.assertGreaterEqual(result.prediction_c, 36.0)
        self.assertEqual(result.observed_max_c, 36.0)

    def test_active_heating_projects_positive_remaining_rise(self):
        model = PhysicsHighModel()
        result = model.predict(
            self.observations([27, 29, 31, 33, 35]),
            30.57, 103.95, ZoneInfo("Asia/Shanghai"),
            datetime(2026, 7, 25, 4, tzinfo=UTC),
        )
        self.assertGreater(result.remaining_rise_c, 0.0)
        self.assertGreater(result.prediction_c, result.observed_max_c)

    def test_round_half_up_matches_integer_settlement_bucket(self):
        self.assertEqual(round_half_up(34.49), 34)
        self.assertEqual(round_half_up(34.50), 35)

    def test_sounding_is_exposed_in_three_path_output(self):
        profile = SoundingFeatures(
            source="test", station_id="56187", station_name="Wenjiang",
            observation_time_utc="2026-07-25T00:00:00+00:00", distance_km=20.0,
            surface_pressure_hpa=941.5, surface_temperature_c=25.4, surface_dewpoint_c=22.1,
            temperature_925_c=28.8, temperature_850_c=25.3, temperature_700_c=13.2,
            dewpoint_depression_850_c=9.3, low_level_inversion_c=3.4,
            inversion_top_agl_m=151.0, surface_equivalent_850_c=34.1,
            surface_equivalent_700_c=38.5, layer_850_700_lapse_c_per_km=7.24,
            vertical_regime="morning_inversion_reservoir", levels=80,
        )
        result = PhysicsHighModel().predict(
            self.observations([27, 29, 31, 33, 35]),
            30.57, 103.95, ZoneInfo("Asia/Shanghai"),
            datetime(2026, 7, 25, 4, tzinfo=UTC), sounding=profile,
        )
        self.assertEqual(result.sounding["station_id"], "56187")
        self.assertEqual(result.path_status["warm_tail"], "supported_competitor")
        self.assertGreaterEqual(result.warm_tail_path_c, result.primary_path_c)

    def test_700_hpa_equivalent_temperature_is_not_assumed_reachable(self):
        profile = SoundingFeatures(
            source="test", station_id="58362", station_name="Baoshan",
            observation_time_utc="2026-07-25T00:00:00+00:00", distance_km=45.0,
            surface_pressure_hpa=1000.0, surface_temperature_c=30.0, surface_dewpoint_c=24.0,
            temperature_925_c=25.0, temperature_850_c=22.0, temperature_700_c=15.0,
            dewpoint_depression_850_c=7.0, low_level_inversion_c=0.0,
            inversion_top_agl_m=None, surface_equivalent_850_c=36.0,
            surface_equivalent_700_c=45.0, layer_850_700_lapse_c_per_km=5.0,
            vertical_regime="neutral_vertical_profile", levels=80,
        )
        result = PhysicsHighModel().predict(
            self.observations([29, 31, 33, 34, 35]),
            31.14, 121.80, ZoneInfo("Asia/Shanghai"),
            datetime(2026, 7, 25, 4, tzinfo=UTC), sounding=profile,
        )
        self.assertLess(result.warm_tail_path_c, 45.0)


if __name__ == "__main__":
    unittest.main()
