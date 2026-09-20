import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from windy_token_keeper import WindyTokenKeeper, atomic_write, token_health


UTC = timezone.utc


def jwt(payload: dict) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


class WindyTokenKeeperTests(unittest.TestCase):
    def test_token_health_requires_premium_tier(self):
        now = datetime(2026, 7, 22, 10, tzinfo=UTC)
        premium = token_health(jwt({"exp": now.timestamp() + 7200, "subscriptionTiers": ["premium"]}), now)
        anonymous = token_health(jwt({"exp": now.timestamp() + 7200}), now)
        self.assertTrue(premium["premium"])
        self.assertFalse(anonymous["premium"])
        self.assertEqual(premium["remaining_seconds"], 7200)

    def test_atomic_write_replaces_token_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            atomic_write(path, "first\n")
            atomic_write(path, "second\n")
            self.assertEqual(path.read_text(), "second\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_browser_token_unwraps_json_encoded_local_storage_value(self):
        keeper = WindyTokenKeeper()
        keeper.ensure_chrome = lambda: None
        keeper._windy_page = lambda: {"webSocketDebuggerUrl": "unused"}
        keeper._cdp = lambda *_args, **_kwargs: {"result": {"value": '"header.payload.signature"'}}
        self.assertEqual(keeper.browser_token(), "header.payload.signature")


if __name__ == "__main__":
    unittest.main()
