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
from typing import TYPE_CHECKING, Any

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
        *,
        settings_service: Any = None,
    ) -> None:
        self._config = config
        self._state = state_manager
        self._db = database
        # SettingsService (optional) resolves per-user/per-tenant connectors
        # into a settings overlay so a user's own LLM creds drive their chat.
        self._settings_service = settings_service
        self._agent: DevAIChatAgent | None = None
        # Per-overlay agent cache, keyed by a fingerprint of the overridden
        # attrs, so a user with custom creds gets their own agent without
        # rebuilding an LLM client on every turn.
        self._agent_cache: dict[str, DevAIChatAgent] = {}

    def _get_agent(self) -> DevAIChatAgent:
        # Lazy so importing the gateway doesn't construct an LLM client.
        if self._agent is None:
            from devai.chat.agent import DevAIChatAgent

            self._agent = DevAIChatAgent(self._config, self._state, database=self._db)
        return self._agent

    async def _agent_for(self, principal: Principal) -> DevAIChatAgent:
        """Resolve the chat agent for this principal, applying their settings
        overlay (per-user LLM creds/model). Falls back to the shared agent when
        no Settings capability is wired or the user configured nothing."""
        if self._settings_service is None:
            return self._get_agent()
        try:
            from devai.settings.overlay import PrincipalSettingsOverlay, build_overlay

            overlaid = await build_overlay(self._config, principal, self._settings_service)
            if not isinstance(overlaid, PrincipalSettingsOverlay) or not overlaid.overlaid_attrs:
                return self._get_agent()  # nothing user-specific → shared agent

            fingerprint = "|".join(f"{a}={getattr(overlaid, a)!r}" for a in overlaid.overlaid_attrs)
            cached = self._agent_cache.get(fingerprint)
            if cached is None:
                from devai.chat.agent import DevAIChatAgent

                cached = DevAIChatAgent(overlaid, self._state, database=self._db)
                # Bound cache so a flood of distinct users can't grow unbounded.
                if len(self._agent_cache) > 64:
                    self._agent_cache.clear()
                self._agent_cache[fingerprint] = cached
            return cached
        except Exception:  # noqa: BLE001
            logger.warning("overlay agent resolution failed — using shared agent", exc_info=True)
            return self._get_agent()

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

        trial_block = await self._trial_guard(principal)
        if trial_block is not None:
            return trial_block

        try:
            agent = await self._agent_for(principal)
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

    async def _trial_guard(self, principal: Principal) -> ConversationReply | None:
        """Trial enforcement for chat — same policy as the run path.

        Humans without their own LLM connector chat on the platform agent
        only while trial budget remains; each turn deducts a flat estimate
        (the LangChain path doesn't expose exact usage here). Exhausted →
        a clear add-your-own-key reply; the shared keys stay revoked.
        Returns None when the turn may proceed. Never raises.
        """
        try:
            email = getattr(principal, "email", "") or ""
            is_human = "@" in email and not email.startswith(("webhook:", "system:"))
            if not is_human or not bool(getattr(self._config, "llm_require_user_connector", False)):
                return None
            if self._settings_service is not None:
                from devai.settings.llm_resolver import PrincipalLLMResolver

                resolver = PrincipalLLMResolver(self._config, self._settings_service)
                if await resolver.resolve(principal) is not None:
                    return None  # user has their own connector — no trial involved

            from devai.settings.trial import get_trial_meter

            meter = get_trial_meter(self._config)
            if meter.budget <= 0 or await meter.exhausted(email):
                return ConversationReply(
                    text=(
                        "Your free trial allowance is used up. Add your own LLM "
                        "API key in Settings → LLM Provider to keep chatting — "
                        "the shared platform credentials are no longer available "
                        "for your account."
                    ),
                    metadata={"trial_exhausted": True},
                )
            # Flat per-turn estimate — the chat path doesn't surface exact
            # token usage; conservative so trials end predictably.
            total = await meter.add(email, 1500)
            if total >= int(meter.budget * 0.8):
                logger.info("trial: %s at %d/%d tokens (warning threshold)", email, total, meter.budget)
            return None
        except Exception:  # noqa: BLE001
            logger.warning("trial guard failed — allowing turn", exc_info=True)
            return None

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
