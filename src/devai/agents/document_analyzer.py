"""Document Analyzer Agent — ingests requirements from documents, URLs, and specs.

First agent in the pipeline. Accepts:
- PDF documents (product briefs, design docs)
- Markdown files (README, wiki pages)
- OpenAPI/Swagger specs
- URLs (wiki, docs sites, Confluence pages)
- Raw text

Extracts structured requirements and feeds them to the Requirements Analyst.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from devai.core.base_agent import BaseAgent

# Primary: Gemini | Fallback: OpenAI
from devai.providers.gemini_provider import GeminiProvider

# Groq available as fallback: from devai.providers.groq_provider import GroqProvider
from devai.tools.document_tools import DocumentToolExecutor

if TYPE_CHECKING:
    from devai.graph.a2a import A2ABus
    from devai.graph.state import ALMState

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a Document Analyst specializing in extracting software requirements from various document formats.

Your role is to:
1. Read the provided documents using the available tools
2. Extract ALL functional and non-functional requirements
3. Identify constraints, assumptions, and acceptance criteria
4. Detect the intended tech stack from the document context
5. Flag any ambiguities or missing information

For each document, extract:
- Functional requirements (features, user stories, use cases)
- Non-functional requirements (performance, security, scalability)
- Technical constraints (languages, frameworks, infrastructure)
- Integration requirements (APIs, third-party services)
- Deployment requirements (cloud, on-prem, K8s, VM)

Output a comprehensive requirements summary that the Requirements Analyst can work with.

If the input contains an OpenAPI spec, extract ALL endpoints as functional requirements.
If the input contains architecture diagrams descriptions, extract infrastructure requirements.
If the input references specific technologies, note them as tech stack requirements."""


class DocumentAnalyzerAgent(BaseAgent):
    """Ingests documents and extracts structured requirements."""

    name = "document_analyzer"
    subscribe_subject = "devai.pipeline.trigger"
    publish_subject = "devai.pipeline.docs_analyzed"

    async def _execute_graph(self, state: ALMState, a2a: A2ABus) -> dict[str, Any]:
        """Analyze input documents and extract requirements."""
        gemini = GeminiProvider(self.config)
        doc_tools = DocumentToolExecutor()

        requirements_text = state.get("requirements", "")
        repo = state.get("repo_full_name", "")

        # Detect if input contains file paths, URLs, or raw text
        doc_sources = self._detect_sources(requirements_text)

        extracted_texts: list[str] = []

        for source in doc_sources:
            if source["type"] == "pdf":
                result = await doc_tools.execute("doc_read_pdf", {"file_path": source["value"]})
                extracted_texts.append(f"[PDF: {source['value']}]\n{result}")

            elif source["type"] == "url":
                result = await doc_tools.execute("doc_read_url", {"url": source["value"]})
                extracted_texts.append(f"[URL: {source['value']}]\n{result}")

            elif source["type"] == "markdown":
                result = await doc_tools.execute("doc_read_markdown", {"file_path": source["value"]})
                extracted_texts.append(f"[Markdown: {source['value']}]\n{result}")

            elif source["type"] == "openapi":
                result = await doc_tools.execute("doc_parse_openapi", {"file_path": source["value"]})
                extracted_texts.append(f"[OpenAPI: {source['value']}]\n{result}")

            elif source["type"] == "wiki":
                result = await doc_tools.execute(
                    "doc_read_github_wiki",
                    {
                        "repo": repo,
                        "page": source["value"],
                    },
                )
                extracted_texts.append(f"[Wiki: {source['value']}]\n{result}")

        # Always include the raw requirements text
        extracted_texts.append(f"[Raw Requirements]\n{requirements_text}")

        # Use Gemini for fast synthesis
        combined = "\n\n---\n\n".join(extracted_texts)

        synthesis_prompt = f"""Analyze these documents and extract ALL requirements for building this software:

{combined[:12000]}

Extract and synthesize into a comprehensive requirements document covering:
1. Functional requirements (what the system should do)
2. Non-functional requirements (performance, security, scalability)
3. Technical constraints (specific tech stack, infrastructure)
4. Deployment requirements (K8s, VM, cloud provider, region)
5. Integration requirements (APIs, databases, message queues)
6. User roles and access levels
7. Data requirements (storage, schema, migration)

Output as structured text that a Requirements Analyst can work with.
Be specific and actionable. Don't add requirements not in the source documents."""

        synthesized = await gemini.generate(
            prompt=synthesis_prompt,
            system=SYSTEM_PROMPT,
            max_tokens=4096,
        )

        # Detect tech stack hints from documents
        tech_hints = self._detect_tech_hints(combined)

        # Detect deployment target hints
        deploy_hints = self._detect_deploy_hints(combined)

        # Notify Requirements Analyst
        a2a.handoff(
            "requirements_analyst",
            "Documents Analyzed",
            f"Analyzed {len(doc_sources)} document sources. "
            f"Tech stack hints: {', '.join(tech_hints[:5])}. "
            f"Deploy target: {deploy_hints}.",
            payload={
                "doc_count": len(doc_sources),
                "tech_hints": tech_hints,
                "deploy_hints": deploy_hints,
            },
        )

        # If OpenAPI spec found, notify Engineering Manager directly
        api_sources = [s for s in doc_sources if s["type"] == "openapi"]
        if api_sources:
            a2a.notify(
                "engineering_manager",
                "OpenAPI Spec Found",
                f"Found {len(api_sources)} API spec(s) in requirements. Use these for implementation planning.",
            )

        return {
            "requirements": synthesized,
            "analyzed_requirements": [],  # Will be populated by Requirements Analyst
            "requirement_gaps": [],
        }

    def _detect_sources(self, text: str) -> list[dict[str, str]]:
        """Detect document sources from the requirements text."""
        import re

        sources: list[dict[str, str]] = []

        # PDF files
        for match in re.finditer(r"(/[^\s]+\.pdf|[A-Za-z]:\\[^\s]+\.pdf)", text, re.IGNORECASE):
            sources.append({"type": "pdf", "value": match.group()})

        # URLs
        for match in re.finditer(r'https?://[^\s<>"\']+', text):
            url = match.group().rstrip(".,;)")
            sources.append({"type": "url", "value": url})

        # Markdown files
        for match in re.finditer(r"(/[^\s]+\.md|[A-Za-z]:\\[^\s]+\.md)", text, re.IGNORECASE):
            sources.append({"type": "markdown", "value": match.group()})

        # OpenAPI specs
        for match in re.finditer(r"(/[^\s]+\.(yaml|yml|json))", text, re.IGNORECASE):
            path = match.group()
            if any(kw in path.lower() for kw in ["openapi", "swagger", "api-spec"]):
                sources.append({"type": "openapi", "value": path})

        # Wiki page references
        for match in re.finditer(r"wiki:(\S+)", text, re.IGNORECASE):
            sources.append({"type": "wiki", "value": match.group(1)})

        return sources

    def _detect_tech_hints(self, text: str) -> list[str]:
        """Detect technology stack hints from document content."""
        text_lower = text.lower()
        hints: list[str] = []

        tech_keywords = {
            "react": "React",
            "next.js": "Next.js",
            "nextjs": "Next.js",
            "vue": "Vue.js",
            "angular": "Angular",
            "svelte": "Svelte",
            "tailwind": "Tailwind CSS",
            "typescript": "TypeScript",
            "python": "Python",
            "django": "Django",
            "flask": "Flask",
            "fastapi": "FastAPI",
            "go ": "Go",
            "golang": "Go",
            "gin": "Gin",
            "echo": "Echo",
            "rust": "Rust",
            "java": "Java",
            "spring": "Spring Boot",
            "node.js": "Node.js",
            "express": "Express",
            "nestjs": "NestJS",
            "postgresql": "PostgreSQL",
            "postgres": "PostgreSQL",
            "mongodb": "MongoDB",
            "redis": "Redis",
            "mysql": "MySQL",
            "kubernetes": "Kubernetes",
            "docker": "Docker",
            "helm": "Helm",
            "terraform": "Terraform",
            "aws": "AWS",
            "gcp": "GCP",
            "azure": "Azure",
            "nats": "NATS",
            "rabbitmq": "RabbitMQ",
            "kafka": "Kafka",
            "graphql": "GraphQL",
            "grpc": "gRPC",
            "rest api": "REST API",
        }

        for keyword, tech in tech_keywords.items():
            if keyword in text_lower and tech not in hints:
                hints.append(tech)

        return hints

    def _detect_deploy_hints(self, text: str) -> str:
        """Detect deployment target from document content."""
        text_lower = text.lower()

        if any(kw in text_lower for kw in ["kubernetes", "k8s", "gke", "eks", "aks", "cluster"]):
            return "kubernetes"
        if any(kw in text_lower for kw in ["vm", "virtual machine", "ec2", "compute engine", "ssh"]):
            return "vm"
        if any(kw in text_lower for kw in ["serverless", "lambda", "cloud function", "cloud run"]):
            return "serverless"
        if any(kw in text_lower for kw in ["docker", "container", "compose"]):
            return "container"

        return "kubernetes"  # Default for Tesserix
