#!/usr/bin/env python3
"""Feed a stdin prompt into the installed Hermes oneshot runtime."""

from __future__ import annotations

import os
import sys
from functools import wraps


def _patch_custom_beeapi_reasoning() -> None:
    """Forward the project's reasoning level through Hermes custom BeeAPI.

    Hermes' generic custom-provider profile does not expose a reasoning knob.
    BeeAPI accepts the OpenAI-compatible top-level ``reasoning_effort`` field,
    so add it only for the BeeAPI reasoning-model routes used by this project.
    """
    effort = os.environ.get("WEATHER_HERMES_REASONING_EFFORT", "").strip().lower()
    if effort not in {"low", "medium", "high"}:
        return
    from agent.transports.chat_completions import ChatCompletionsTransport

    if getattr(ChatCompletionsTransport, "_weather_beeapi_reasoning_patched", False):
        return
    original = ChatCompletionsTransport._build_kwargs_from_profile

    @wraps(original)
    def build_kwargs(self, profile, model, sanitized, tools, params):
        kwargs = original(self, profile, model, sanitized, tools, params)
        base_url = str(params.get("base_url") or "").lower()
        model_name = str(model or "").lower()
        supports_reasoning_effort = "grok" in model_name or model_name.startswith("gpt-5.6-")
        if "beeapi.ai" in base_url and supports_reasoning_effort:
            kwargs["reasoning_effort"] = effort
        return kwargs

    ChatCompletionsTransport._build_kwargs_from_profile = build_kwargs
    ChatCompletionsTransport._weather_beeapi_reasoning_patched = True


def _disable_beeapi_streaming() -> None:
    """Use the stable non-streaming Responses path for BeeAPI oneshots."""
    from run_agent import AIAgent

    if getattr(AIAgent, "_weather_beeapi_streaming_patched", False):
        return
    original = AIAgent.__init__

    @wraps(original)
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if "beeapi.ai" in str(getattr(self, "base_url", "") or "").lower():
            self._disable_streaming = True

    AIAgent.__init__ = init
    AIAgent._weather_beeapi_streaming_patched = True


def _disable_weather_persistence() -> None:
    """Keep automated weather calls stateless and suppress giant failure dumps.

    The project already persists its audit trail in weather_market_monitor.sqlite3.
    Persisting the same multi-megabyte prompt in Hermes SessionDB and request
    dumps makes one transient provider failure consume tens of megabytes.
    """
    from hermes_cli import oneshot
    from run_agent import AIAgent

    oneshot._create_session_db_for_oneshot = lambda: None
    if not getattr(AIAgent, "_weather_request_dump_patched", False):
        AIAgent._dump_api_request_debug = lambda self, *args, **kwargs: None
        AIAgent._weather_request_dump_patched = True


def main() -> int:
    if not os.environ.get("HERMES_HOME"):
        print("HERMES_HOME is required", file=sys.stderr)
        return 2
    prompt = sys.stdin.read()
    if not prompt.strip():
        print("Hermes prompt is empty", file=sys.stderr)
        return 2

    from hermes_cli.oneshot import run_oneshot

    toolsets = os.environ.get("WEATHER_HERMES_TOOLSETS", "memory")
    if toolsets not in {"memory", "profile"}:
        print("weather agent only permits the memory toolset or an isolated profile", file=sys.stderr)
        return 2
    _patch_custom_beeapi_reasoning()
    _disable_beeapi_streaming()
    _disable_weather_persistence()
    model = os.environ.get("WEATHER_HERMES_MODEL") or None
    provider = os.environ.get("WEATHER_HERMES_PROVIDER") or None
    if bool(model) != bool(provider):
        print("WEATHER_HERMES_MODEL and WEATHER_HERMES_PROVIDER must be set together", file=sys.stderr)
        return 2
    explicit_toolsets = None if toolsets == "profile" else toolsets
    return run_oneshot(prompt=prompt, model=model, provider=provider, toolsets=explicit_toolsets)


if __name__ == "__main__":
    raise SystemExit(main())
