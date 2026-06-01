"""ConversationGateway — the single conversational brain behind every channel.

Slack, the remote URL/thread endpoint, and the MCP server are all thin
transports (``devai.adapters.messaging.*``) that call exactly one method here:
``handle_turn``. This is the only place that touches the chat agent, the
per-conversation history, A2A attribution, and the audit trail — so the brain
is written once and never duplicated across transports.

It deliberately does NOT know about HTTP, Slack, or MCP. It takes a
transport-neutral ``ConversationTurn`` and returns a ``ConversationReply``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from devai.adapters.messaging.base import ConversationReply, ConversationTurn
from devai.identity import Principal, new_trace_id

if TYPE_CHECKING:
    from devai.chat.agent import DevAIChatAgent
    from devai.config import Settings
    from devai.core.state import StateManager
    from devai.services.database import Database

logger = logging.getLogger(__name__)


class ConversationGateway:
    """Transport-agnostic front door to the DevAI chat agent.

    Holds (lazily) one shared ``DevAIChatAgent`` instance — the same agent the
    dashboard chat routes use — and runs each turn against it, keyed by the
    turn's ``conversation_id`` so every transport gets isolated, resumable
    history. Every turn is best-effort audited.
    """

    def __init__(
        self,
        config: Settings,
        state_manager: StateManager,
        database: Database | None = None,
    ) -> None:
        self._config = config
        self._state = state_manager
        self._db = database
        self._agent: DevAIChatAgent | None = None

    def _get_agent(self) -> DevAIChatAgent:
        # Lazy so importing the gateway doesn't construct an LLM client.
        if self._agent is None:
            from devai.chat.agent import DevAIChatAgent

            self._agent = DevAIChatAgent(self._config, self._state, database=self._db)
        return self._agent

    async def handle_turn(self, turn: ConversationTurn) -> ConversationReply:
        """Run one conversational turn. Never raises — returns a friendly error.

        ``conversation_id`` is used directly as the chat agent's session key, so
        a Slack thread, a remote URL thread, and an MCP session each keep their
        own history.
        """
        principal = turn.principal or Principal.system()
        trace_id = turn.trace_id or new_trace_id()

        if not (turn.text or "").strip():
            return ConversationReply(text="Please send a message.")

        try:
            agent = self._get_agent()
            text = await agent.chat(
                turn.text,
                session_id=turn.conversation_id,
                principal=principal,
                trace_id=trace_id,
            )
            reply = ConversationReply(text=text, metadata={"trace_id": trace_id})
        except Exception as exc:
            logger.exception(
                "gateway turn failed (channel=%s conv=%s)",
                turn.channel,
                turn.conversation_id,
            )
            reply = ConversationReply(
                text=f"Sorry, I hit an error handling that: {exc}",
                metadata={"trace_id": trace_id, "error": True},
            )

        await self._audit(turn, principal, trace_id, error="error" in reply.metadata)
        return reply

    async def _audit(
        self,
        turn: ConversationTurn,
        principal: Principal,
        trace_id: str,
        *,
        error: bool,
    ) -> None:
        """Best-effort audit row for a remote conversational turn.

        Never blocks or breaks the turn — a missing/failing DB just means the
        turn isn't recorded (matches the rest of the codebase's audit policy).
        """
        if self._db is None:
            return
        try:
            await self._db.audit(
                action="remote_chat_turn",
                actor=principal.email,
                actor_type="human",
                entity_type="conversation",
                entity_ref=turn.conversation_id,
                details={
                    "channel": turn.channel,
                    "user_id": turn.user_id,
                    "trace_id": trace_id,
                    "text_len": len(turn.text or ""),
                    "error": error,
                },
            )
        except Exception:
            logger.debug("audit write failed for remote chat turn", exc_info=True)

    def clear(self, conversation_id: str) -> None:
        """Drop a conversation's history (e.g. a Slack thread ended)."""
        if self._agent is not None:
            self._agent.clear_session(conversation_id)


__all__ = ["ConversationGateway"]
