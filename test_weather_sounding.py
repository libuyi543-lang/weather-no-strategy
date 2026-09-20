import unittest

from research.weather_sounding import parse_sounding_html


HTML = """
<H1>Observations for Station 56187 at 00 UTC 25 Jul 2026</H1>
<PRE>
-----------------------------------------------------------------------------
   PRES   HGHT   TEMP   DWPT   RELH   MIXR   DRCT   SPED   THTA   THTE   THTV
  941.5    548   25.4   22.1     82  18.04    281    0.7  303.7  357.7  307.0
  925.0    699   28.8   20.4     61  16.47    212    3.4  308.7  359.0  311.7
  900.0    945   28.2   17.0     50  13.50    180    3.0  310.5  353.0  313.0
  875.0   1198   26.8   16.5     53  13.40    180    3.0  311.6  354.0  314.0
  850.0   1453   25.3   16.0     57  13.58    204    2.8  312.6  354.7  315.1
  800.0   1980   21.0   15.0     68  13.00    190    2.0  313.7  355.0  316.0
  750.0   2540   17.0   12.5     75  12.00    180    1.0  315.5  354.0  318.0
  700.0   3125   13.2    9.9     80  11.00    217    2.0  317.0  351.9  319.1
</PRE>
"""


class WeatherSoundingTests(unittest.TestCase):
    def test_parser_extracts_vertical_structure(self):
        profile = parse_sounding_html(
            HTML,
            {"name": "Wenjiang", "latitude": 30.75, "longitude": 103.8667},
            30.5779, 103.9477,
        )
        self.assertEqual(profile.station_id, "56187")
        self.assertEqual(profile.temperature_850_c, 25.3)
        self.assertEqual(profile.temperature_700_c, 13.2)
        self.assertAlmostEqual(profile.low_level_inversion_c, 3.4)
        self.assertEqual(profile.vertical_regime, "morning_inversion_reservoir")
        self.assertLess(profile.distance_km, 25)


if __name__ == "__main__":
    unittest.main()
