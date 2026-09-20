import unittest
from unittest.mock import Mock, patch

from weather_dashboard.server import WeatherDashboardHandler


class WeatherDashboardHandlerTests(unittest.TestCase):
    def test_access_log_is_silent(self):
        handler = object.__new__(WeatherDashboardHandler)
        self.assertIsNone(handler.log_message('%s', 'request'))

    def test_broken_client_connection_does_not_trigger_second_response(self):
        handler = object.__new__(WeatherDashboardHandler)
        handler.path = "/api/overview"
        handler.send_json = Mock(side_effect=BrokenPipeError)
        with patch("weather_dashboard.server.DATA.overview", return_value={"ok": True}):
            handler.do_GET()
        handler.send_json.assert_called_once_with({"ok": True})


if __name__ == "__main__":
    unittest.main()
