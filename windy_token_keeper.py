#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import requests
import websocket


ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
CONFIG_DIR = Path("~/.config/weather-market-monitor").expanduser()
TOKEN_PATH = CONFIG_DIR / "windy_user_token"
PROFILE_DIR = CONFIG_DIR / "chrome-profile"
STATE_PATH = CONFIG_DIR / "windy_token_keeper_state.json"
REFRESH_REQUEST_PATH = CONFIG_DIR / "windy_token_refresh.request"
CHROME_LOG_PATH = CONFIG_DIR / "chrome.log"
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
DEBUG_PORT = 9229
DEBUG_URL = f"http://127.0.0.1:{DEBUG_PORT}"
WINDY_URL = "https://www.windy.com/"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(UTC).isoformat(timespec="seconds")


def decode_token(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("token is not a JWT")
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    if not isinstance(payload, dict):
        raise ValueError("JWT payload is not an object")
    return payload


def token_health(token: str, now: datetime | None = None) -> dict[str, Any]:
    current = (now or utc_now()).astimezone(UTC)
    payload = decode_token(token)
    expiry_raw = payload.get("exp")
    if not isinstance(expiry_raw, (int, float)):
        raise ValueError("JWT has no expiry")
    expiry = datetime.fromtimestamp(float(expiry_raw), UTC)
    tiers = {str(item).casefold() for item in payload.get("subscriptionTiers") or []}
    return {
        "premium": "premium" in tiers,
        "expires_at_utc": iso_utc(expiry),
        "remaining_seconds": (expiry - current).total_seconds(),
        "user_id": payload.get("userID"),
    }


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_env(path: Path) -> dict[str, str]:
    output: dict[str, str] = {}
    if not path.exists():
        return output
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        output[key.strip()] = value.strip().strip("'\"")
    return output


class WindyTokenKeeper:
    def __init__(self, poll_seconds: int = 30, refresh_before_hours: int = 12):
        self.poll_seconds = poll_seconds
        self.refresh_before = timedelta(hours=refresh_before_hours)
        self.last_validation_at: datetime | None = None
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _save_state(self, **updates: Any) -> None:
        self.state.update(updates)
        atomic_write(STATE_PATH, json.dumps(self.state, ensure_ascii=False, indent=2) + "\n")

    def _send_feishu(self, text: str) -> None:
        env = {**load_env(ROOT / ".env"), **os.environ}
        webhook = env.get("FEISHU_WEATHER_NO_WEBHOOK", "").strip()
        if not webhook:
            logging.warning("FEISHU_WEATHER_NO_WEBHOOK is not configured")
            return
        response = requests.post(
            webhook,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=15,
        )
        response.raise_for_status()

    def alert(self, key: str, message: str) -> None:
        now = utc_now()
        last_key = self.state.get("last_alert_key")
        try:
            last_at = datetime.fromisoformat(str(self.state.get("last_alert_at_utc"))).astimezone(UTC)
        except (TypeError, ValueError):
            last_at = datetime.min.replace(tzinfo=UTC)
        if key == last_key and now - last_at < timedelta(hours=6):
            return
        try:
            self._send_feishu(f"【Windy Meteoblue 采集告警】\n时间：{iso_utc(now)}\n{message}")
        except Exception:
            logging.exception("failed to send Feishu alert")
        self._save_state(last_alert_key=key, last_alert_at_utc=iso_utc(now), healthy=False)

    def recovered(self, health: dict[str, Any]) -> None:
        if self.state.get("healthy") is False:
            try:
                self._send_feishu(
                    "【Windy Meteoblue 采集恢复】\n"
                    f"时间：{iso_utc()}\nPremium Token 已续签并通过 1 小时预报验证。\n"
                    f"新到期时间：{health['expires_at_utc']}"
                )
            except Exception:
                logging.exception("failed to send Feishu recovery")
        self._save_state(
            healthy=True,
            last_success_at_utc=iso_utc(),
            token_expires_at_utc=health["expires_at_utc"],
            last_error=None,
        )

    def ensure_chrome(self) -> None:
        try:
            with urlopen(f"{DEBUG_URL}/json/version", timeout=2):
                return
        except (OSError, URLError):
            pass
        if not CHROME_PATH.exists():
            raise RuntimeError("Google Chrome is not installed")
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(CONFIG_DIR, 0o700)
        os.chmod(PROFILE_DIR, 0o700)
        log_handle = CHROME_LOG_PATH.open("ab")
        subprocess.Popen(
            [
                str(CHROME_PATH),
                f"--remote-debugging-port={DEBUG_PORT}",
                f"--user-data-dir={PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                WINDY_URL,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_handle.close()
        for _ in range(40):
            try:
                with urlopen(f"{DEBUG_URL}/json/version", timeout=2):
                    return
            except (OSError, URLError):
                time.sleep(0.25)
        raise RuntimeError("Chrome debugging endpoint did not start")

    def _windy_page(self) -> dict[str, Any]:
        with urlopen(f"{DEBUG_URL}/json", timeout=5) as response:
            pages = json.load(response)
        windy_pages = [
            page for page in pages
            if page.get("type") == "page" and "windy.com" in str(page.get("url") or "")
        ]
        if windy_pages:
            return windy_pages[0]
        request = Request(f"{DEBUG_URL}/json/new?{WINDY_URL}", method="PUT")
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    @staticmethod
    def _cdp(page: dict[str, Any], method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        connection = websocket.create_connection(
            page["webSocketDebuggerUrl"], suppress_origin=True, timeout=10
        )
        try:
            connection.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                message = json.loads(connection.recv())
                if message.get("id") == 1:
                    if message.get("error"):
                        raise RuntimeError(str(message["error"]))
                    return message.get("result") or {}
        finally:
            connection.close()

    def browser_token(self, force_reload: bool = False) -> str:
        self.ensure_chrome()
        page = self._windy_page()
        if force_reload:
            self._cdp(page, "Page.reload", {"ignoreCache": True})
            time.sleep(5)
        result = self._cdp(
            page,
            "Runtime.evaluate",
            {"expression": 'localStorage.getItem("settings_userToken")', "returnByValue": True},
        )
        token = str((result.get("result") or {}).get("value") or "").strip()
        if token.startswith('"') and token.endswith('"'):
            try:
                decoded = json.loads(token)
                if isinstance(decoded, str):
                    token = decoded.strip()
            except json.JSONDecodeError:
                pass
        return token

    def validate_hourly(self, token: str) -> None:
        from weather_market_monitor import WeatherMarketMonitor

        config = json.loads((ROOT / "monitor_config.json").read_text(encoding="utf-8"))
        monitor = WeatherMarketMonitor(config)
        try:
            monitor.windy_user_token = token
            reference = monitor._windy_reference()
            row = {
                "station_id": "EGLC",
                "station_name": "London City Airport",
                "city": "London",
                "latitude": 51.5053,
                "longitude": 0.0553,
                "timezone": "Europe/London",
                "windy_url": WINDY_URL,
                "target_date": utc_now().astimezone(__import__("zoneinfo").ZoneInfo("Europe/London")).date().isoformat(),
            }
            result = monitor._windy_forecast(row, reference)
            if result.get("status") != "ok" or result.get("step_hours") != 1.0:
                raise RuntimeError("Windy hourly validation did not return a usable Meteoblue forecast")
        finally:
            monitor.close()

    def run_once(self) -> dict[str, Any]:
        force_refresh = REFRESH_REQUEST_PATH.exists()
        if force_refresh:
            REFRESH_REQUEST_PATH.unlink(missing_ok=True)
        token = self.browser_token(force_reload=force_refresh)
        if not token:
            self.alert("not_logged_in", "专用 Windy 浏览器尚未登录。请在已打开的窗口完成一次登录。")
            return {"healthy": False, "reason": "not_logged_in"}
        try:
            health = token_health(token)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.alert("invalid_browser_token", f"Windy 页面 Token 无法解析：{exc}")
            return {"healthy": False, "reason": "invalid_browser_token"}
        if not health["premium"]:
            self.alert("not_premium", "专用 Windy 浏览器当前未登录 Premium 账号，请完成登录。")
            return {"healthy": False, "reason": "not_premium", **health}

        try:
            last_refresh_attempt = datetime.fromisoformat(
                str(self.state.get("last_refresh_attempt_at_utc"))
            ).astimezone(UTC)
        except (TypeError, ValueError):
            last_refresh_attempt = datetime.min.replace(tzinfo=UTC)
        refresh_due = force_refresh or utc_now() - last_refresh_attempt >= timedelta(hours=1)
        if health["remaining_seconds"] <= self.refresh_before.total_seconds() and refresh_due:
            self._save_state(last_refresh_attempt_at_utc=iso_utc())
            refreshed = self.browser_token(force_reload=True)
            refreshed_health = token_health(refreshed)
            if refreshed_health["premium"] and refreshed_health["remaining_seconds"] > health["remaining_seconds"]:
                token, health = refreshed, refreshed_health

        if health["remaining_seconds"] <= 0:
            self.alert("expired", f"Windy Premium Token 已过期：{health['expires_at_utc']}")
            return {"healthy": False, "reason": "expired", **health}
        if health["remaining_seconds"] <= timedelta(hours=2).total_seconds():
            self.alert("expiring", f"Windy Premium Token 即将到期：{health['expires_at_utc']}，自动续签未成功。")
            return {"healthy": False, "reason": "expiring", **health}

        should_validate = (
            self.last_validation_at is None
            or utc_now() - self.last_validation_at >= timedelta(hours=1)
            or force_refresh
        )
        if should_validate:
            try:
                self.validate_hourly(token)
            except Exception as exc:
                self.alert("hourly_validation_failed", f"Premium Token 存在，但 1 小时 Meteoblue 验证失败：{exc}")
                return {"healthy": False, "reason": "hourly_validation_failed", **health}
            self.last_validation_at = utc_now()

        current = TOKEN_PATH.read_text(encoding="utf-8").strip() if TOKEN_PATH.exists() else ""
        if current != token:
            atomic_write(TOKEN_PATH, token + "\n")
            logging.info("Windy Premium token renewed; expires_at_utc=%s", health["expires_at_utc"])
        self.recovered(health)
        return {"healthy": True, **health}

    def run_loop(self) -> None:
        while True:
            try:
                result = self.run_once()
                logging.info("token keeper: %s", json.dumps(result, ensure_ascii=False))
            except Exception as exc:
                logging.exception("Windy token keeper failed")
                self.alert("keeper_error", f"自动续签守护器异常：{exc}")
            time.sleep(self.poll_seconds)


def configure_logging() -> None:
    log_path = ROOT / "logs/windy_token_keeper.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")],
        force=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Keep a Windy Premium userToken renewed from a persistent Chrome login")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--status", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging()
    if args.status:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    keeper = WindyTokenKeeper()
    if args.once or not args.loop:
        result = keeper.run_once()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("healthy") else 1
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stop:
        try:
            result = keeper.run_once()
            logging.info("token keeper: %s", json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            logging.exception("Windy token keeper failed")
            keeper.alert("keeper_error", f"自动续签守护器异常：{exc}")
        for _ in range(keeper.poll_seconds):
            if stop:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
