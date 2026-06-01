"""AI agent crews — named squads of specialization "members" with a lead.

A crew is the AI half of the two-layer teams model: a human *team* owns one
or more *crews*; a crew autonomously completes a task by having its **lead**
plan the work and hand subtasks to its **members** (each member is one
`AgentRunner` invocation with its own skills/tools). Crews come from two
sources, unified by `resolve_crew`:

  * **DB** — team-created crews (the `agent_crews` table, via TeamService).
  * **Seed YAML** — a handful of ready-to-use crews shipped in `crews/`
    (frontend_crew, backend_crew) so the platform is usable day one.
"""

from devai.crews.loader import load_seed_crews, seed_crew_registry
from devai.crews.models import CrewMember, CrewSpec

__all__ = ["CrewMember", "CrewSpec", "load_seed_crews", "seed_crew_registry"]
