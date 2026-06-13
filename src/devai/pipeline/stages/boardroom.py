"""Boardroom debate stage — structured multi-agent decision making.

Big tech/product/architecture decisions deserve a proper boardroom
discussion, not one agent's first idea. The supervisor (moderator)
convenes a panel, PULLS IN extra specialists when the topic demands
them, runs bounded debate rounds where every panelist must take a
position and challenge the others, then synthesizes the agreed plan —
with dissent recorded honestly — for the user's one-click approval.

Scenario coverage, by design:

  - topic routing      — a core quartet (product, engineering, architecture,
                         security) always sits; data/infra/QA/release/UX
                         specialists join when the topic mentions their
                         domain ("pull agents from other blueprints").
  - early consensus    — if a round produces no new challenges, the debate
                         ends early instead of burning rounds.
  - deadlock           — persistent disagreement is NOT papered over: the
                         moderator records the majority recommendation AND
                         the dissent, and the approval gate shows both.
  - flaky panelist     — an LLM error skips that seat for the round (noted
                         as absent) instead of killing the meeting.
  - flaky moderator    — synthesis degrades to a mechanical digest of the
                         final positions.
  - no LLM at all      — the stage no-ops visibly (boardroom_skipped) and
                         the pipeline continues; planning quality degrades,
                         the run does not crash.
  - visibility         — every statement is an A2A message (the Timeline
                         reads like meeting minutes) and the decision is
                         posted to the epic when one exists.
  - downstream reuse   — the decision lands in `technical_plan` (what
                         create-plan/implement read) and
                         `boardroom_decision`; pair with plan_approval
                         (require_approval: true) so the user signs off.

Registered as `boardroom_debate`; usable from ANY blueprint.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from devai.pipeline.interfaces import PipelineStage, StageDeps
from devai.pipeline.types import DevAITask, StageResult

logger = logging.getLogger(__name__)

# Core seats — always present. (role key, display name, persona brief)
_CORE_PANEL: list[tuple[str, str, str]] = [
    (
        "product_director",
        "Product Director",
        "You own user value and scope. Fight scope creep, demand measurable "
        "success criteria, and challenge any tech choice that doesn't serve "
        "the user outcome.",
    ),
    (
        "engineering_manager",
        "Engineering Manager",
        "You own delivery. Challenge anything that can't be built and "
        "operated by a small team on a realistic timeline; push for the "
        "simplest architecture that meets the requirements.",
    ),
    (
        "staff_architect",
        "Staff Architect",
        "You own long-term technical health. Challenge short-cuts that "
        "create lock-in, hidden coupling, or migration pain; insist on "
        "clear data models and interface boundaries.",
    ),
    (
        "security_expert",
        "Security Expert",
        "You own risk. Challenge auth, data-handling, supply-chain and "
        "compliance gaps; no plan passes with an unaddressed critical risk.",
    ),
]

# Specialist seats pulled in when the topic demands them.
_SPECIALISTS: list[tuple[tuple[str, ...], tuple[str, str, str]]] = [
    (
        ("database", "schema", "sql", "data model", "storage", "migration"),
        (
            "db_engineer",
            "DB Engineer",
            "You own the data layer. Challenge schema designs, consistency "
            "assumptions, and any ORM/storage choice that won't survive scale "
            "or migration.",
        ),
    ),
    (
        ("deploy", "kubernetes", "k8s", "cloud", "infra", "cost", "scale", "hosting"),
        (
            "infra_provisioner",
            "Infra Engineer",
            "You own runtime and cost. Challenge anything that's painful to "
            "deploy, observe, or pay for; demand a concrete deploy story.",
        ),
    ),
    (
        ("test", "quality", "qa", "coverage", "e2e"),
        (
            "qa_tester",
            "QA Lead",
            "You own verifiability. Challenge anything that can't be tested "
            "automatically; demand acceptance criteria that map to tests.",
        ),
    ),
    (
        ("release", "rollout", "versioning", "feature flag", "launch"),
        (
            "release_manager",
            "Release Manager",
            "You own safe delivery. Challenge launches without rollback plans, staged rollouts, or telemetry.",
        ),
    ),
    (
        ("ui", "ux", "frontend", "design", "mobile", "responsive"),
        (
            "ux_specialist",
            "UX Specialist",
            "You own the experience. Challenge flows that confuse users and "
            "any UI plan without states for loading/error/empty.",
        ),
    ),
]

# Generic panel for NON-ENGINEERING topics (no repo on the run): the
# boardroom doubles as a general-purpose multi-agent debate — research a
# market, weigh a strategy, analyze anything — same mesh, different seats.
_GENERIC_PANEL: list[tuple[str, str, str]] = [
    (
        "research_analyst",
        "Research Analyst",
        "You own evidence. Ground every claim in concrete facts, data points, "
        "and named sources of uncertainty; challenge anything asserted without support.",
    ),
    (
        "domain_strategist",
        "Domain Strategist",
        "You own the big picture. Frame the drivers, trends, and second-order "
        "effects; challenge analysis that misses context or interactions.",
    ),
    (
        "risk_analyst",
        "Risk Analyst",
        "You own downside scenarios. Quantify what can go wrong, how likely, "
        "and what would falsify the thesis; challenge optimism without hedges.",
    ),
    (
        "devils_advocate",
        "Devil's Advocate",
        "You own the opposite case. Construct the strongest counter-argument "
        "to whatever the table currently believes; concede only to evidence.",
    ),
]

_MODERATOR_BRIEF = (
    "You are the Supervisor moderating a boardroom of specialist agents. "
    "You do not take sides — you synthesize: where the panel AGREES, where "
    "it still DISAGREES (name who holds which position), and what the "
    "strongest open challenges are."
)


class _BoardroomDebateStage(PipelineStage):
    def __init__(self, deps: StageDeps, config: dict[str, str]) -> None:
        self.deps = deps
        self.config = config

    def name(self) -> str:
        return str(self.config.get("__stage_name") or "boardroom-debate")

    # ── helpers ─────────────────────────────────────────────────────

    def _llm_usable(self) -> bool:
        llm = self.deps.llm
        return llm is not None and getattr(llm, "provider_name", "noop") != "noop"

    def _chain(self):
        """Role-routed adapter (anthropic → gateway/Vertex, same model id);
        degrades to deps.llm when the role factory can't build."""
        cached = getattr(self, "_role_chain", None)
        if cached is not None:
            return cached
        try:
            from devai.adapters.llm import create_role_llm

            chain = create_role_llm(self.deps.config, "")
            # Tests/minimal deps inject deps.llm directly with no provider
            # keys configured — a noop role chain must not shadow it.
            if "noop" in str(getattr(chain, "provider_name", "noop")):
                chain = self.deps.llm
            self._role_chain = chain
        except Exception:  # noqa: BLE001
            self._role_chain = self.deps.llm
        return self._role_chain

    async def _say(self, system: str, prompt: str, *, max_tokens: int = 700, moderator: bool = False) -> str:
        """Panel seats run on the CHEAP boardroom model (many parallel
        debater calls); the moderator's syntheses and the decision document
        get the stronger one."""
        from devai.adapters.llm.base import LLMMessage, LLMRequest, LLMRole

        model = str(
            getattr(
                self.deps.config,
                "llm_model_boardroom_moderator" if moderator else "llm_model_boardroom_panel",
                "",
            )
            or ""
        )
        response = await self._chain().generate(
            LLMRequest(
                system=system,
                messages=[LLMMessage(role=LLMRole.USER, content=prompt)],
                model=model,
                max_tokens=max_tokens,
                temperature=0.4,
                # triggered_by/run_id let the usage ledger attribute boardroom
                # spend to the user and run this debate belongs to.
                extra={
                    "agent": "boardroom",
                    "triggered_by": getattr(self, "_triggered_by", ""),
                    "run_id": getattr(self, "_run_id", ""),
                },
            )
        )
        return (response.text or "").strip()

    def _select_panel(self, topic: str, *, has_repo: bool = True) -> list[tuple[str, str, str]]:
        # No repo → this is a general-purpose debate (market analysis,
        # strategy, research), not a software delivery — seat the generic
        # analyst panel instead of the engineering quartet.
        panel = list(_CORE_PANEL) if has_repo else list(_GENERIC_PANEL)
        lowered = topic.lower()
        if has_repo:
            for keywords, seat in _SPECIALISTS:
                if any(k in lowered for k in keywords):
                    panel.append(seat)
        max_seats = int(self.config.get("panel_max", 7) or 7)
        return panel[:max_seats]

    async def _maybe_recruit(
        self, topic: str, positions: dict[str, str], panel: list[tuple[str, str, str]]
    ) -> list[tuple[str, str, str]]:
        """Moderator judgement call: does the table LACK expertise for this
        topic? Up to 2 new specialist seats are created with full context
        and join the debate — 'fetch and borrow any other agents' without
        being limited to a fixed catalog."""
        if not self._llm_usable():
            return []
        try:
            import json as _json

            seated = ", ".join(name for _, name, _ in panel)
            digest = "\n".join(f"{w}: {t[:200]}" for w, t in positions.items())
            text = await self._say(
                _MODERATOR_BRIEF,
                f"Topic under debate:\n{topic[:800]}\n\nSeated panel: {seated}\n\n"
                f"Round 1 positions:\n{digest}\n\n"
                "Does this panel LACK expertise the topic genuinely needs? Reply STRICT JSON only:\n"
                '{"recruit": [{"name": "<Role Title>", "brief": "<1-2 sentence persona: what they own and challenge>"}]}\n'
                "Maximum 2 recruits; reply {'recruit': []} when the table is sufficient.",
                max_tokens=300,
                moderator=True,
            )
            t = text.strip()
            if t.startswith("```"):
                t = t.split("\n", 1)[1].rsplit("```", 1)[0]
            data = _json.loads(t)
            seats: list[tuple[str, str, str]] = []
            for r in (data.get("recruit") or [])[:2]:
                name = str(r.get("name") or "").strip()
                brief = str(r.get("brief") or "").strip()
                if name and brief:
                    key = name.lower().replace(" ", "_")[:40]
                    seats.append((key, name[:40], brief[:300]))
            return seats
        except Exception:  # noqa: BLE001 — recruitment is an enhancer, never fatal
            logger.debug("boardroom recruitment skipped", exc_info=True)
            return []

    def _a2a(self, task: DevAITask, bag: list[dict[str, Any]], frm: str, to: str, subject: str, body: str) -> None:
        bag.append(
            {
                "id": uuid.uuid4().hex,
                "from_agent": frm,
                "to_agent": to,
                "message_type": "broadcast" if to == "boardroom" else "response",
                "subject": subject[:140],
                "body": body[:1000],
                "payload": {"stage": self.name()},
                "in_reply_to": None,
                "timestamp": time.time(),
                "triggered_by": task.triggered_by or "",
                "trace_id": task.trace_id or task.id,
            }
        )

    # ── the meeting ─────────────────────────────────────────────────

    async def execute(self, task: DevAITask) -> StageResult:
        # Honor the triggering user's own LLM connector (their keys, with the
        # boardroom's role-priced models requested per call) for the whole
        # debate. Set before any _say() so every seat + the moderator run on
        # it. A user with no connector rides the trial-metered platform chain;
        # at exhaustion the debate refuses clearly instead of leaking onto the
        # shared keys.
        try:
            user_llm = await self.deps.role_llm_for_principal(task.triggered_by or "", "")
            if user_llm is None and self._llm_usable():
                # A platform LLM exists but the policy refused it: the user has
                # no connector and their trial budget is spent/disabled. (When
                # NO LLM exists at all, fall through to the visible skip.)
                raise RuntimeError(
                    "no LLM available for this debate — your free trial allowance "
                    "is used up. Add your own LLM API key in Settings → LLM Provider."
                )
            if (
                user_llm is not None
                and getattr(user_llm, "provider_name", "noop") != "noop"
                and user_llm is not self.deps.llm
            ):
                self._role_chain = user_llm
                logger.info("boardroom: using per-user LLM for %s", task.triggered_by)
        except RuntimeError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("boardroom: per-user LLM resolution failed — using role chain", exc_info=True)
        self._triggered_by = task.triggered_by or ""
        self._run_id = task.id

        topic = (
            str(self.config.get("topic") or "")
            or str(task.agent_context.get("technical_plan") or "")[:500]
            or task.intent
        )
        has_repo = bool(task.repo)
        if not self._llm_usable():
            return StageResult(
                message="boardroom skipped — no usable LLM (plan quality degraded, run continues)",
                data={"boardroom_skipped": True},
            )

        rounds = max(1, min(4, int(self.config.get("rounds", 2) or 2)))
        recruited = False
        # Hard wall-clock budget: a real debate takes minutes, but NEVER more
        # than this — when the budget runs out we synthesize from whatever is
        # on the table and project it to the user, who chooses to debate
        # further (reject at the gate) or proceed to build (approve).
        budget_seconds = max(60.0, float(self.config.get("time_budget_seconds", 900) or 900))
        started_at = time.monotonic()
        budget_hit = False
        panel = self._select_panel(topic, has_repo=has_repo)
        ctx = task.agent_context
        background = "\n".join(
            f"- {k}: {str(v)[:300]}"
            for k, v in (
                ("detected tech stack", ctx.get("detected_tech_stack")),
                ("analyzed requirements", ctx.get("analyzed_requirements")),
                ("existing plan draft", ctx.get("technical_plan")),
            )
            if v
        )
        if background:
            # Repo-derived context (tech-stack detection reads repo files) —
            # fence it so a poisoned README can't steer the whole boardroom.
            from devai.services.prompt_guard import wrap_untrusted

            background = wrap_untrusted(background, "prior-stage context", limit=1200)

        a2a_bag: list[dict[str, Any]] = list(ctx.get("a2a_messages") or [])
        self._a2a(
            task,
            a2a_bag,
            "supervisor",
            "boardroom",
            "Boardroom convened",
            f"Panel: {', '.join(name for _, name, _ in panel)}. Topic: {topic[:300]}",
        )

        positions: dict[str, str] = {}
        transcript: list[str] = []
        consensus_reached = False

        for round_no in range(1, rounds + 1):
            if time.monotonic() - started_at > budget_seconds:
                budget_hit = True
                transcript.append(
                    f"[round {round_no}] Supervisor: time budget "
                    f"({int(budget_seconds)}s) reached — closing the debate with the positions on the table."
                )
                break
            prior = (
                "\n\n".join(f"**{who}**: {text[:600]}" for who, text in positions.items())
                or "(opening round — no positions yet)"
            )
            new_positions: dict[str, str] = {}
            challenges_made = False

            for _role, display, brief in panel:
                prompt = (
                    f"BOARDROOM ROUND {round_no}/{rounds}.\n\nDecision topic:\n{topic[:1200]}\n\n"
                    + (f"Background:\n{background}\n\n" if background else "")
                    + f"Current positions on the table:\n{prior}\n\n"
                    "Speak as your role. Reply in EXACTLY this format:\n"
                    "POSITION: <your concrete recommendation, 2-4 sentences>\n"
                    "CHALLENGE: <the weakest point of another panelist's position and why "
                    "(or 'none — I agree with the table')>\n"
                    "CONCEDE: <anything you now accept that you previously opposed, or 'nothing'>"
                )
                try:
                    text = await self._say(
                        f"You are the {display} in a boardroom of specialist agents. {brief} "
                        "Be direct and aggressive on substance, professional in tone.",
                        prompt,
                    )
                except Exception:  # noqa: BLE001 — one absent seat ≠ cancelled meeting
                    logger.exception("boardroom: %s failed to respond in round %d", display, round_no)
                    transcript.append(f"[round {round_no}] {display}: (absent — LLM error)")
                    continue
                new_positions[display] = text
                transcript.append(f"[round {round_no}] {display}: {text}")
                self._a2a(task, a2a_bag, _role, "boardroom", f"Round {round_no} position", text)
                challenge = next((ln for ln in text.splitlines() if ln.upper().startswith("CHALLENGE:")), "")
                if challenge and "none" not in challenge.lower()[:30]:
                    challenges_made = True

            positions.update(new_positions)

            # Moderator round synthesis.
            try:
                summary = await self._say(
                    _MODERATOR_BRIEF,
                    f"Round {round_no} positions:\n\n"
                    + "\n\n".join(f"**{w}**: {t[:500]}" for w, t in new_positions.items())
                    + "\n\nReply with:\nAGREED: <bullet list>\nDISPUTED: <bullet list with names, or 'nothing'>",
                    max_tokens=400,
                    moderator=True,
                )
                transcript.append(f"[round {round_no}] Supervisor synthesis: {summary}")
                self._a2a(task, a2a_bag, "supervisor", "boardroom", f"Round {round_no} synthesis", summary)
            except Exception:  # noqa: BLE001
                logger.exception("boardroom: moderator synthesis failed in round %d", round_no)

            # Early consensus only counts from round 2 — in round 1 nobody
            # has seen anyone else's position yet, so "no challenges" is
            # vacuous, not agreement.
            # Supervisor recruitment: after round 1 the moderator may PULL
            # IN missing expertise — new seats get the full table context
            # and join from the next round.
            if round_no == 1 and not recruited and round_no < rounds:
                recruited = True
                extra = await self._maybe_recruit(topic, new_positions, panel)
                for seat in extra:
                    panel.append(seat)
                    transcript.append(f"[round {round_no}] Supervisor recruited {seat[1]}: {seat[2][:120]}")
                    self._a2a(
                        task,
                        a2a_bag,
                        "supervisor",
                        "boardroom",
                        f"Recruited {seat[1]}",
                        f"The table lacks this expertise — {seat[1]} joins from the next round. Brief: {seat[2][:300]}",
                    )

            if not challenges_made and round_no >= 2 and new_positions:
                consensus_reached = True
                break  # genuine consensus — don't burn remaining rounds

        # An empty table is a FAILURE, not a meeting: if every panelist call
        # errored, raise so the executor retries / the recovery agent
        # engages — returning a hollow "decision" silently downgraded the
        # whole feature (live incident: an SDK kwarg bug made all seats
        # 'absent' in 0.7s and the run sailed on without any debate).
        if not positions:
            raise RuntimeError(
                "boardroom collected zero positions — every panelist call failed; "
                "see transcript: " + " | ".join(transcript[-3:])[:400]
            )

        # Final decision document.
        decision = await self._final_decision(topic, positions, consensus_reached)
        transcript.append(f"[final] Supervisor decision: {decision[:800]}")
        self._a2a(task, a2a_bag, "supervisor", "boardroom", "Boardroom decision", decision)

        await self._post_to_epic(task, panel, decision)

        return StageResult(
            message=(
                f"boardroom: {len(panel)} seats, "
                + ("consensus" if consensus_reached else "majority + recorded dissent")
                + (" (time budget reached)" if budget_hit else "")
            ),
            data={
                "boardroom_decision": decision[:6000],
                "boardroom_consensus": consensus_reached,
                "boardroom_budget_hit": budget_hit,
                "boardroom_panel": [name for _, name, _ in panel],
                "boardroom_transcript": "\n\n".join(transcript)[-12000:],
                # Downstream stages (plan approval, implement) read this.
                "technical_plan": decision[:6000],
                # The plan-approval banner surfaces this summary.
                "plan_summary": decision[:1200],
                "a2a_messages": a2a_bag,
            },
        )

    async def _final_decision(self, topic: str, positions: dict[str, str], consensus: bool) -> str:
        digest = "\n\n".join(f"**{w}**: {t[:600]}" for w, t in positions.items())
        try:
            return await self._say(
                _MODERATOR_BRIEF,
                f"The boardroom has finished debating:\n{topic[:1000]}\n\nFinal positions:\n{digest}\n\n"
                "Write the DECISION DOCUMENT in markdown with EXACTLY these sections:\n"
                "## Decision\n## Why (alternatives considered and why rejected)\n"
                "## Risks & mitigations\n## Plan outline (numbered, buildable steps)\n"
                "## Dissent\n(name who disagreed and with what — 'none' only if the table truly agreed)",
                max_tokens=1200,
                moderator=True,
            )
        except Exception:  # noqa: BLE001 — degrade to a mechanical digest
            logger.exception("boardroom: final decision synthesis failed")
            return (
                "## Decision\n(moderator synthesis unavailable — final positions below)\n\n"
                + digest
                + "\n\n## Dissent\nUnresolved — review the positions above."
            )

    async def _post_to_epic(self, task: DevAITask, panel: list[tuple[str, str, str]], decision: str) -> None:
        scm = self.deps.scm
        if scm is None or not task.epic_issue_number or getattr(task, "dry_run", False):
            return
        try:
            await scm.add_comment(
                task.repo,
                task.epic_issue_number,
                "## Boardroom decision\n\n"
                f"_Panel: {', '.join(name for _, name, _ in panel)} — run `{task.id}`_\n\n" + decision[:5000],
            )
        except Exception:  # noqa: BLE001 — minutes are best-effort
            logger.exception("boardroom: epic comment failed")


def boardroom_debate_stage(deps: StageDeps, config: dict[str, str]) -> PipelineStage:
    return _BoardroomDebateStage(deps, config)


__all__ = ["boardroom_debate_stage"]
