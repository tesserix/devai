"""A2A runtime consumer — card parsing, discovery, and message envelope."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from devai.a2a import A2AClient, A2AError, AgentCard


def _card_dict(name: str, *, url: str = "https://agent.svc/a2a/v1", skills=None) -> dict:
    return {
        "protocolVersion": "0.3.0",
        "name": name,
        "description": f"{name} does things",
        "version": "1.0.0",
        "url": url,
        "preferredTransport": "JSONRPC",
        "capabilities": {
            "streaming": True,
            "extensions": [
                {
                    "uri": "https://registry.agentic.dev/ext/provenance",
                    "params": {"arn": f"arn:agentic:registry:sre:agents/sre/{name}", "digest": "sha256:abc"},
                }
            ],
        },
        "skills": skills
        or [{"id": "triage", "name": "Triage", "description": "triages incidents", "tags": ["sre"]}],
    }


@dataclass
class _StubAgent:
    name: str


class FakeRegistry:
    def __init__(self, cards: dict[str, dict] | None = None) -> None:
        self._cards = cards or {}
        self.calls: list[tuple] = []

    def get_agent_card(self, name: str, *, namespace: str = "", tag: str = "") -> dict:
        self.calls.append((name, namespace, tag))
        if name not in self._cards:
            raise RuntimeError("404 not found")
        return self._cards[name]

    def list_agents(self) -> list[_StubAgent]:
        return [_StubAgent(n) for n in self._cards]


# --------------------------------------------------------------------- #
# Card parsing
# --------------------------------------------------------------------- #


def test_agentcard_parses_core_fields_and_provenance() -> None:
    card = AgentCard.from_dict(_card_dict("oncall"))
    assert card.name == "oncall"
    assert card.url == "https://agent.svc/a2a/v1"
    assert card.protocol_version == "0.3.0"
    assert card.skill_ids() == ["triage"]
    assert card.has_skill("triage")
    # provenance pulled out of capabilities.extensions
    assert card.provenance["digest"] == "sha256:abc"


def test_agentcard_matches_on_name_and_skill_tags() -> None:
    card = AgentCard.from_dict(_card_dict("oncall"))
    assert card.matches("")  # empty matches all
    assert card.matches("oncall")
    assert card.matches("sre")  # skill tag
    assert not card.matches("billing")


def test_agentcard_tolerates_sparse_document() -> None:
    card = AgentCard.from_dict({"name": "bare"})
    assert card.name == "bare"
    assert card.preferred_transport == "JSONRPC"  # default
    assert card.skills == []
    assert card.provenance == {}


# --------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------- #


def test_fetch_card_normalizes_registry_error() -> None:
    client = A2AClient(FakeRegistry())
    with pytest.raises(A2AError):
        client.fetch_card("missing")


def test_discover_filters_by_capability() -> None:
    reg = FakeRegistry(
        {
            "oncall": _card_dict("oncall"),
            "writer": _card_dict(
                "writer", skills=[{"id": "draft", "name": "Draft", "tags": ["docs"]}]
            ),
        }
    )
    client = A2AClient(reg)
    all_cards = client.discover()
    assert {c.name for c in all_cards} == {"oncall", "writer"}
    sre = client.discover("sre")
    assert [c.name for c in sre] == ["oncall"]


def test_find_for_capability_returns_first_match() -> None:
    reg = FakeRegistry({"oncall": _card_dict("oncall")})
    client = A2AClient(reg)
    assert client.find_for_capability("triage").name == "oncall"
    assert client.find_for_capability("nonexistent") is None


# --------------------------------------------------------------------- #
# Invocation envelope
# --------------------------------------------------------------------- #


def test_send_message_builds_jsonrpc_envelope() -> None:
    reg = FakeRegistry({"oncall": _card_dict("oncall")})
    # verify_cards=False: this test exercises the JSON-RPC envelope mechanics,
    # not the trust guards (those are covered in test_a2a_security.py).
    client = A2AClient(reg, verify_cards=False)
    card = client.fetch_card("oncall")

    captured: dict = {}

    def fake_rpc(url, method, params):
        captured["url"] = url
        captured["method"] = method
        captured["params"] = params
        return {"kind": "task", "status": {"state": "completed"}}

    client._rpc = fake_rpc  # type: ignore[method-assign]
    out = client.send_message(card, "Pod is CrashLoopBackOff — propose a fix.")

    assert captured["url"] == "https://agent.svc/a2a/v1"
    assert captured["method"] == "message/send"
    msg = captured["params"]["message"]
    assert msg["role"] == "user"
    assert msg["parts"] == [{"kind": "text", "text": "Pod is CrashLoopBackOff — propose a fix."}]
    assert msg["messageId"]  # a generated id is present
    assert out["status"]["state"] == "completed"


def test_send_message_requires_url() -> None:
    client = A2AClient(FakeRegistry(), verify_cards=False)
    card = AgentCard.from_dict({"name": "no-endpoint"})
    with pytest.raises(A2AError):
        client.send_message(card, "hi")
