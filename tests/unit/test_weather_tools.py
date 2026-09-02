from __future__ import annotations

import json

from devai.tools.dispatch import ToolDispatcher


async def test_weather_tool_returns_a_deterministic_normalized_observation() -> None:
    dispatcher = ToolDispatcher()
    specs = dispatcher.build_tool_specs(["weather-current"])

    result = json.loads(
        await dispatcher.execute(
            "weather-current",
            {"location": "  melbourne  ", "unit": "celsius"},
        )
    )

    assert [spec.name for spec in specs] == ["weather-current"]
    assert result == {
        "condition": "partly cloudy",
        "location": "Melbourne",
        "observed_at": "2026-08-29T00:00:00Z",
        "source": "deterministic-fixture",
        "temperature": 18.0,
        "unit": "celsius",
    }


async def test_weather_tool_reports_unsupported_locations_without_fabricating() -> None:
    dispatcher = ToolDispatcher()
    dispatcher.build_tool_specs(["weather-current"])

    result = json.loads(await dispatcher.execute("weather-current", {"location": "Atlantis"}))

    assert result["error"] == "unsupported_location"
    assert result["location"] == "Atlantis"
    assert result["supported_locations"] == ["London", "Melbourne", "Sydney"]
