from devai.analytics.service import summarize_lifecycle_evals


def test_lifecycle_scores_are_attributed_to_every_executable_dependency() -> None:
    rows = [
        {
            "run_id": "eval-1",
            "agent": "weather-agent",
            "configuration": {
                "draft": {
                    "spec": {
                        "prompts": ["weather-prompt-v1"],
                        "skills": ["weather"],
                        "tools": ["weather-current"],
                        "mcpServers": ["weather-mcp"],
                    }
                }
            },
            "suite": {"name": "weather-golden-suite", "version": "1"},
            "summary": {
                "cases": 2,
                "passed": 1,
                "failed": 1,
                "pass_rate": 0.5,
                "total_tokens": 120,
                "cost_usd": 0.012,
                "p95_latency_ms": 420,
                "dimensions": {
                    "exact_output": {"average": 0.75, "pass_rate": 0.5},
                    "tool_trajectory": {"average": 1.0, "pass_rate": 1.0},
                },
            },
            "failing_cases": [{"case_id": "rain", "trace_url": "/api/traces/t-1", "failures": ["wrong unit"]}],
            "created_at": "2026-09-02T00:00:00Z",
        }
    ]

    result = summarize_lifecycle_evals(rows)

    assert result["summary"] == {
        "runs": 1,
        "cases": 2,
        "passed": 1,
        "failed": 1,
        "pass_rate": 0.5,
        "tokens": 120,
        "cost_usd": 0.012,
        "avg_p95_latency_ms": 420.0,
    }
    assert {(item["kind"], item["name"]) for item in result["artifacts"]} == {
        ("agent", "weather-agent"),
        ("prompt", "weather-prompt-v1"),
        ("skill", "weather"),
        ("tool", "weather-current"),
        ("mcp_server", "weather-mcp"),
    }
    assert result["dimensions"][0]["name"] == "exact_output"
    assert result["recent"][0]["failing_cases"][0]["trace_url"] == "/api/traces/t-1"


def test_lifecycle_summary_is_empty_when_no_runs_exist() -> None:
    result = summarize_lifecycle_evals([])

    assert result["summary"]["runs"] == 0
    assert result["artifacts"] == []
    assert result["dimensions"] == []
    assert result["recent"] == []
