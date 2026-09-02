from __future__ import annotations

import json
from typing import Any

from devai.tools.registry import Handler, ToolContext, register

_OBSERVED_AT = "2026-08-29T00:00:00Z"
_OBSERVATIONS: dict[str, tuple[str, float, str]] = {
    "london": ("London", 12.0, "light rain"),
    "melbourne": ("Melbourne", 18.0, "partly cloudy"),
    "sydney": ("Sydney", 22.0, "clear"),
}


def _factory(_: ToolContext) -> Handler:
    async def current_weather(arguments: dict[str, Any]) -> str:
        raw_location = arguments.get("location")
        if not isinstance(raw_location, str) or not raw_location.strip() or len(raw_location) > 100:
            return _render({"error": "invalid_location"})

        location = raw_location.strip()
        observation = _OBSERVATIONS.get(location.casefold())
        if observation is None:
            return _render(
                {
                    "error": "unsupported_location",
                    "location": location,
                    "supported_locations": sorted(value[0] for value in _OBSERVATIONS.values()),
                }
            )

        unit = arguments.get("unit", "celsius")
        if unit not in {"celsius", "fahrenheit"}:
            return _render({"error": "invalid_unit", "unit": unit})
        canonical_location, temperature_c, condition = observation
        temperature = temperature_c if unit == "celsius" else round((temperature_c * 9 / 5) + 32, 1)
        return _render(
            {
                "condition": condition,
                "location": canonical_location,
                "observed_at": _OBSERVED_AT,
                "source": "deterministic-fixture",
                "temperature": temperature,
                "unit": unit,
            }
        )

    return current_weather


def _render(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


register(
    "weather-current",
    "Return a deterministic current-weather fixture for a supported city.",
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["location"],
        "properties": {
            "location": {"type": "string", "minLength": 1, "maxLength": 100},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"], "default": "celsius"},
        },
    },
    _factory,
)


__all__ = []
