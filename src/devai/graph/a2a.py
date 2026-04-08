"""Agent-to-Agent (A2A) communication protocol for DevAI.

Agents communicate by appending messages to the shared graph state.
Each message has a sender, recipient, type, and payload. Agents can:

- REQUEST: Ask another agent for information or action
- RESPONSE: Reply to a request
- NOTIFICATION: Broadcast status updates
- CLARIFICATION: Ask for clarification on received input
- ESCALATION: Escalate an issue to a higher-level agent
- HANDOFF: Transfer responsibility for a subtask

The A2A bus reads from and writes to ALMState["a2a_messages"].
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ulid import ULID

if TYPE_CHECKING:
    from devai.graph.state import A2AMessageDict

logger = logging.getLogger(__name__)


class A2ABus:
    """In-process A2A message bus backed by the LangGraph state.

    Each agent gets an A2ABus instance scoped to its name. Messages are
    accumulated in a list and merged into the graph state after the node
    completes.
    """

    def __init__(self, agent_name: str, messages: list[A2AMessageDict] | None = None) -> None:
        self.agent_name = agent_name
        self._inbox: list[A2AMessageDict] = []
        self._outbox: list[A2AMessageDict] = []

        # Load existing messages addressed to this agent
        if messages:
            self._inbox = [m for m in messages if m.get("to_agent") == agent_name]

    # --- Sending ---

    def send(
        self,
        to_agent: str,
        message_type: str,
        subject: str,
        body: str,
        payload: dict[str, Any] | None = None,
        in_reply_to: str | None = None,
    ) -> A2AMessageDict:
        """Send a message to another agent."""
        msg: A2AMessageDict = {
            "id": str(ULID()),
            "from_agent": self.agent_name,
            "to_agent": to_agent,
            "message_type": message_type,
            "subject": subject,
            "body": body,
            "payload": payload or {},
            "in_reply_to": in_reply_to,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._outbox.append(msg)
        logger.debug("A2A [%s -> %s] %s: %s", self.agent_name, to_agent, message_type, subject)
        return msg

    def request(self, to_agent: str, subject: str, body: str, **kwargs: Any) -> A2AMessageDict:
        """Send a REQUEST message."""
        return self.send(to_agent, "request", subject, body, **kwargs)

    def respond(self, to_msg: A2AMessageDict, body: str, **kwargs: Any) -> A2AMessageDict:
        """Send a RESPONSE to a received message."""
        return self.send(
            to_msg["from_agent"],
            "response",
            f"Re: {to_msg['subject']}",
            body,
            in_reply_to=to_msg.get("id"),
            **kwargs,
        )

    def notify(self, to_agent: str, subject: str, body: str, **kwargs: Any) -> A2AMessageDict:
        """Send a NOTIFICATION message."""
        return self.send(to_agent, "notification", subject, body, **kwargs)

    def escalate(self, to_agent: str, subject: str, body: str, **kwargs: Any) -> A2AMessageDict:
        """Send an ESCALATION message."""
        return self.send(to_agent, "escalation", subject, body, **kwargs)

    def handoff(self, to_agent: str, subject: str, body: str, **kwargs: Any) -> A2AMessageDict:
        """Send a HANDOFF message."""
        return self.send(to_agent, "handoff", subject, body, **kwargs)

    def broadcast(self, subject: str, body: str, exclude: list[str] | None = None) -> list[A2AMessageDict]:
        """Broadcast a notification to all agents."""
        from devai.models import AgentRole

        sent = []
        for role in AgentRole:
            if role.value == self.agent_name:
                continue
            if exclude and role.value in exclude:
                continue
            sent.append(self.notify(role.value, subject, body))
        return sent

    # --- Receiving ---

    @property
    def inbox(self) -> list[A2AMessageDict]:
        """Messages addressed to this agent."""
        return self._inbox

    def get_requests(self) -> list[A2AMessageDict]:
        """Get pending REQUEST messages."""
        return [m for m in self._inbox if m.get("message_type") == "request"]

    def get_handoffs(self) -> list[A2AMessageDict]:
        """Get HANDOFF messages."""
        return [m for m in self._inbox if m.get("message_type") == "handoff"]

    def get_escalations(self) -> list[A2AMessageDict]:
        """Get ESCALATION messages."""
        return [m for m in self._inbox if m.get("message_type") == "escalation"]

    # --- State Integration ---

    def collect_outbox(self) -> list[A2AMessageDict]:
        """Return all outbound messages to merge into graph state."""
        return list(self._outbox)

    def format_inbox_context(self) -> str:
        """Format inbox messages as context for the LLM prompt."""
        if not self._inbox:
            return ""

        lines = ["## Messages from Other Agents\n"]
        for msg in self._inbox:
            lines.append(f"**From {msg['from_agent']}** ({msg['message_type']}): {msg['subject']}\n{msg['body']}\n")
        return "\n".join(lines)
