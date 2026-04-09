"""Agent skill profiles — domain expertise per tech stack.

Each ``SkillProfile`` is a dense, opinionated description of how to ship
production-quality code in a specific stack. It's the difference between
a generic "implement code" prompt and a senior engineer who knows
Next.js + Vitest conventions cold.

Profiles are picked by the Tech Detector based on the requirements text
and/or the existing repo, persisted to ``state["skill_profile"]``, and
read by every downstream agent (Senior Developer, QA Tester, Staff
Reviewer, DB Engineer, CI Monitor) so they all build, test, review, and
ship in the same idiom.

Adding a new profile: drop a new ``profiles/<stack>.py`` file that
defines a ``PROFILE = SkillProfile(...)`` and import it from
``profiles/__init__.py``.
"""

from devai.agents.skills.profiles import (
    PROFILES,
    SkillProfile,
    detect_skill_profile,
    get_skill_profile,
)

__all__ = [
    "PROFILES",
    "SkillProfile",
    "detect_skill_profile",
    "get_skill_profile",
]
