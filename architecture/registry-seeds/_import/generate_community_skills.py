#!/usr/bin/env python3
"""Generate registry-seed ``Skill`` envelopes for community skills.

Parses the two upstream indexes (saved under ``_import/sources/``) and emits one
seed YAML per skill into ``architecture/registry-seeds/skills/community/``,
tagged + labelled to match DevAI's native seeds (see ``skills/cost-analyzer.yaml``).
The ``devai-registry-bootstrap`` Job walks ``architecture/registry-seeds/`` and
POSTs each to the custom aregistry's ``/v0`` API (idempotent upsert).

  * VoltAgent/awesome-agent-skills  — ~1,100 enumerated skills (officialskills.sh / GitHub)
  * agamm/awesome-ai-sre            — genuine OSS tools only (GitHub, no papers/blogs/SaaS)

Run:  python architecture/registry-seeds/_import/generate_community_skills.py
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE.parents[0] / "skills" / "community"
SRC = HERE / "sources"

# Skill line in the VoltAgent README:  - **[team/id](url)** - description
_VOLT_RE = re.compile(r"^- \*\*\[([^\]]+)\]\((https?://[^)]+)\)\*\* - (.+?)\s*$")
# Tool line in awesome-ai-sre:          - [Name](github-url) - description
_SRE_RE = re.compile(r"^- \[([^\]]+)\]\((https://github\.com/[^)]+)\) - (.+?)\s*$")

# Team → DevAI category. Long-tail teams default to "general"; everything still
# carries devai.io/team + tags so it's filterable.
TEAM_CATEGORY: dict[str, str] = {
    # ai / ml
    "anthropics": "ai", "openai": "ai", "google-gemini": "ai", "google": "ai",
    "fal-ai-community": "ai", "huggingface": "ai", "replicate": "ai", "veniceai": "ai",
    "nvidia": "ai", "firebase": "ai", "minimax-ai": "ai", "modelcontextprotocol": "ai",
    # coding / platform
    "microsoft": "coding", "wordpress": "coding", "garrytan": "coding", "obra": "coding",
    # security
    "trailofbits": "security",
    # infra / cloud
    "hashicorp": "infra", "cloudflare": "infra", "netlify": "infra", "google-cloud": "infra",
    # frontend / design
    "vercel-labs": "frontend", "angular": "frontend", "expo": "frontend", "flutter": "frontend",
    "figma": "frontend", "google-labs-code": "frontend", "gsap": "frontend", "addyosmani": "frontend",
    # data
    "clickhouse": "data", "neondatabase": "data", "supabase": "data", "redis": "data",
    "tinybirdco": "data", "mongodb": "data", "duckdb": "data",
    # auth
    "auth0": "auth", "better-auth": "auth",
    # fintech / web3
    "stripe": "fintech", "coinbase": "fintech", "binance": "fintech",
    # marketing
    "coreyhaines31": "marketing", "realkimbarrett": "marketing", "typefully": "marketing",
    # product
    "deanpeters": "product", "phuryn": "product",
    # sre / observability
    "getsentry": "sre", "datadog-labs": "sre",
    # review
    "coderabbitai": "review",
    # devtools / integrations
    "composiohq": "devtools", "firecrawl": "devtools", "brave": "devtools",
    "browserbase": "devtools", "apollographql": "devtools", "googleworkspace": "devtools",
    "trycourier": "devtools", "resend": "devtools", "notion": "devtools",
    # content
    "sanity-io": "content", "remotion-dev": "content",
    # agents
    "voltagent": "agents",
}


def _slug(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)[:63].strip("-")


def _title(skill_id: str) -> str:
    return skill_id.split("/")[-1].replace("-", " ").replace("_", " ").replace(".", " ").strip().title()


def _envelope(team: str, skill_id: str, description: str, *, category: str,
              origin: str, source: str, risk: str) -> dict:
    name = _slug(f"{team}-{skill_id.split('/')[-1]}")
    return {
        "apiVersion": "registry.solo.io/v1alpha1",
        "kind": "Skill",
        "metadata": {
            "name": name,
            "namespace": "devai",
            # Community catalog skills are public knowledge (sourced from public
            # awesome-lists), so they're browsable by anyone. Tenant-private
            # artifacts keep the registry's default 'private' visibility — that
            # (not namespace) is the real isolation boundary.
            "visibility": "public",
            "labels": {
                "devai.io/source": "community",
                "devai.io/origin": origin,
                "devai.io/category": category,
                "devai.io/team": _slug(team),
                "devai.io/risk-level": risk,
            },
        },
        "spec": {
            "displayName": _title(skill_id),
            "description": description[:500],
            "category": category,
            "version": "1",
            "tags": sorted({_slug(team), category, origin, source}),
            "metadata": {"owner": team, "origin": origin, "source": source, "upstream_id": skill_id},
        },
    }


def _risk_for(team_slug: str, category: str) -> str:
    if category in ("security", "infra", "fintech"):
        return "medium"
    return "low"


def parse_voltagent(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = _VOLT_RE.match(line)
        if not m:
            continue
        skill_id, url, desc = m.group(1).strip(), m.group(2), m.group(3).strip()
        if "/" not in skill_id:
            continue
        team = skill_id.split("/")[0]
        team_slug = _slug(team)
        category = TEAM_CATEGORY.get(team_slug, "general")
        source = "officialskills.sh" if "officialskills.sh" in url else "github"
        out.append(_envelope(team, skill_id, desc, category=category,
                             origin="voltagent-awesome-skills", source=source,
                             risk=_risk_for(team_slug, category)))
    return out


def parse_aisre(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = _SRE_RE.match(line)
        if not m:
            continue
        name, url, desc = m.group(1).strip(), m.group(2), m.group(3).strip()
        path = url.split("github.com/", 1)[1].strip("/")
        parts = path.split("/")
        if len(parts) < 2:
            continue  # owner-only link, not a repo
        owner, repo = parts[0], parts[1]
        # Exclude curated "awesome-*" lists (they're indexes, not tools).
        if repo.lower().startswith("awesome") or owner.lower() == "features":
            continue
        out.append(_envelope(owner, f"{owner}/{repo}", desc, category="sre",
                             origin="awesome-ai-sre", source="github", risk="low"))
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.yaml"):
        old.unlink()

    envelopes = []
    volt = SRC / "voltagent-README.md"
    sre = SRC / "awesome-ai-sre-README.md"
    if volt.exists():
        envelopes += parse_voltagent(volt.read_text(encoding="utf-8"))
    if sre.exists():
        envelopes += parse_aisre(sre.read_text(encoding="utf-8"))

    seen: dict[str, str] = {}
    cats: dict[str, int] = {}
    written = 0
    for env in envelopes:
        name = env["metadata"]["name"]
        if name in seen:
            continue  # first wins (stable)
        seen[name] = env["spec"]["metadata"]["upstream_id"]
        (OUT_DIR / f"{name}.yaml").write_text(
            yaml.safe_dump(env, sort_keys=False, default_flow_style=False, width=100),
            encoding="utf-8",
        )
        c = env["metadata"]["labels"]["devai.io/category"]
        cats[c] = cats.get(c, 0) + 1
        written += 1
    print(f"wrote {written} community skill seeds → {OUT_DIR}")
    print("by category:", dict(sorted(cats.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()
