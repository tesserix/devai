"""Document ingestion tools for the Document Analyzer agent.

Supports reading and parsing:
- PDF documents (requirements, design docs)
- Markdown files
- OpenAPI/Swagger specs (YAML/JSON)
- Plain text
- URL content fetching
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from devai.tools.path_guard import PathTraversalError, confine

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

DOCUMENT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "doc_read_pdf",
        "description": "Read and extract text from a PDF document. "
        "Supports requirements docs, design documents, and specs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the PDF file"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "doc_read_markdown",
        "description": "Read and parse a Markdown document, extracting headings, sections, and structured content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the Markdown file"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "doc_read_url",
        "description": "Fetch and extract text content from a URL (web page, wiki, docs site).",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch content from"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "doc_parse_openapi",
        "description": "Parse an OpenAPI/Swagger spec (YAML or JSON) and extract endpoints, "
        "request/response schemas, and authentication requirements.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the OpenAPI spec file"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "doc_parse_requirements_text",
        "description": "Parse a plain text requirements document and extract structured requirements.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Raw text content to parse"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "doc_read_github_wiki",
        "description": "Read pages from a GitHub repository's wiki.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in org/repo format"},
                "page": {"type": "string", "description": "Wiki page name (default: Home)"},
            },
            "required": ["repo"],
        },
    },
]


class DocumentToolExecutor:
    """Executes document ingestion tools."""

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        handler = getattr(self, f"_handle_{tool_name}", None)
        if handler is None:
            return f"Unknown tool: {tool_name}"
        result = await handler(tool_input)
        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2, default=str)

    def _resolve(self, file_path: str) -> Path:
        """Resolve a tool-supplied path, confined to the workspace root when
        ``DEVAI_TOOL_WORKSPACE_ROOT`` is set (otherwise unchanged)."""
        from devai.config import settings

        return confine(file_path, settings.tool_workspace_root)

    async def _handle_doc_read_pdf(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Extract text from PDF using PyPDF2 or pdfplumber."""
        file_path = inp["file_path"]
        try:
            path = self._resolve(file_path)
        except PathTraversalError as e:
            return {"error": str(e)}

        if not path.exists():
            return {"error": f"File not found: {file_path}"}

        text = ""
        pages: list[dict[str, Any]] = []

        # Try PyPDF2 first
        try:
            import pypdf

            reader = pypdf.PdfReader(str(path))
            for i, page in enumerate(reader.pages):
                page_text = page.extract_text() or ""
                pages.append({"page": i + 1, "text": page_text})
                text += page_text + "\n\n"

            return {
                "file": file_path,
                "total_pages": len(reader.pages),
                "text": text[:15000],  # Limit to 15K chars
                "pages": pages[:20],  # First 20 pages
                "metadata": {
                    "title": reader.metadata.title if reader.metadata else "",
                    "author": reader.metadata.author if reader.metadata else "",
                },
            }
        except ImportError:
            pass

        # Fallback: try pdfplumber
        try:
            import pdfplumber

            with pdfplumber.open(str(path)) as pdf:
                for i, page in enumerate(pdf.pages):
                    page_text = page.extract_text() or ""
                    pages.append({"page": i + 1, "text": page_text})
                    text += page_text + "\n\n"

                return {
                    "file": file_path,
                    "total_pages": len(pdf.pages),
                    "text": text[:15000],
                    "pages": pages[:20],
                }
        except ImportError:
            return {"error": "No PDF library available. Install pypdf or pdfplumber."}

    async def _handle_doc_read_markdown(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Parse markdown file into structured sections."""
        file_path = inp["file_path"]
        try:
            path = self._resolve(file_path)
        except PathTraversalError as e:
            return {"error": str(e)}

        if not path.exists():
            return {"error": f"File not found: {file_path}"}

        content = path.read_text(encoding="utf-8", errors="replace")

        # Extract headings and sections
        sections: list[dict[str, str]] = []
        current_heading = "Introduction"
        current_content: list[str] = []

        for line in content.split("\n"):
            if line.startswith("#"):
                if current_content:
                    sections.append(
                        {
                            "heading": current_heading,
                            "content": "\n".join(current_content).strip(),
                        }
                    )
                current_heading = line.lstrip("#").strip()
                current_content = []
            else:
                current_content.append(line)

        if current_content:
            sections.append(
                {
                    "heading": current_heading,
                    "content": "\n".join(current_content).strip(),
                }
            )

        return {
            "file": file_path,
            "total_sections": len(sections),
            "sections": sections,
            "full_text": content[:15000],
        }

    async def _handle_doc_read_url(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Fetch and extract text from URL.

        URLs can originate from prompt-injected requirement text, so each
        hop is validated against the SSRF guard before the request goes
        out. Redirects are followed manually (max 5) so a public URL can't
        302 the fetch onto an internal address.
        """
        import httpx

        from devai.tools.url_guard import assert_public_url

        url = inp["url"]

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                resp = None
                for _ in range(6):  # initial request + up to 5 redirects
                    assert_public_url(url)
                    resp = await client.get(url)
                    if resp.has_redirect_location:
                        # Resolve relative Location headers against the
                        # current URL, then re-validate before following.
                        url = str(httpx.URL(url).join(resp.headers["location"]))
                        continue
                    break
                if resp is None:
                    return {"error": "no response"}
                if resp.has_redirect_location:
                    return {"error": "too many redirects"}
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")

                if "json" in content_type:
                    return {"url": url, "type": "json", "content": resp.text[:15000]}

                # Strip HTML tags for basic text extraction
                text = resp.text
                if "html" in content_type:
                    import re

                    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
                    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text).strip()

                return {"url": url, "type": "text", "content": text[:15000]}

        except Exception as e:
            return {"error": f"Failed to fetch URL: {e}"}

    async def _handle_doc_parse_openapi(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Parse OpenAPI spec and extract structured API info."""
        file_path = inp["file_path"]
        try:
            path = self._resolve(file_path)
        except PathTraversalError as e:
            return {"error": str(e)}

        if not path.exists():
            return {"error": f"File not found: {file_path}"}

        content = path.read_text(encoding="utf-8")

        # Try YAML then JSON
        spec: dict[str, Any] = {}
        try:
            import yaml

            spec = yaml.safe_load(content)
        except (ImportError, Exception):
            try:
                spec = json.loads(content)
            except json.JSONDecodeError:
                return {"error": "Could not parse as YAML or JSON"}

        # Extract key info
        info = spec.get("info", {})
        paths = spec.get("paths", {})
        components = spec.get("components", {})

        endpoints: list[dict[str, Any]] = []
        for path_str, methods in paths.items():
            for method, details in methods.items():
                if method in ("get", "post", "put", "patch", "delete"):
                    endpoints.append(
                        {
                            "method": method.upper(),
                            "path": path_str,
                            "summary": details.get("summary", ""),
                            "tags": details.get("tags", []),
                            "parameters": len(details.get("parameters", [])),
                            "request_body": bool(details.get("requestBody")),
                            "responses": list(details.get("responses", {}).keys()),
                        }
                    )

        schemas = list(components.get("schemas", {}).keys())
        security_schemes = list(components.get("securitySchemes", {}).keys())

        return {
            "file": file_path,
            "title": info.get("title", ""),
            "version": info.get("version", ""),
            "description": info.get("description", "")[:500],
            "base_url": spec.get("servers", [{}])[0].get("url", ""),
            "total_endpoints": len(endpoints),
            "endpoints": endpoints[:50],
            "schemas": schemas[:30],
            "security_schemes": security_schemes,
        }

    async def _handle_doc_parse_requirements_text(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Structure raw text into a parseable format."""
        text = inp["text"]

        # Basic structuring: split by numbered items, bullet points, or paragraphs
        items: list[str] = []
        current_item: list[str] = []

        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                if current_item:
                    items.append("\n".join(current_item))
                    current_item = []
                continue

            # Detect numbered/bulleted items
            import re

            if re.match(r"^(\d+[\.\)]\s|[-*]\s|•\s)", stripped):
                if current_item:
                    items.append("\n".join(current_item))
                current_item = [stripped]
            else:
                current_item.append(stripped)

        if current_item:
            items.append("\n".join(current_item))

        return {
            "total_items": len(items),
            "items": items[:50],
            "raw_length": len(text),
        }

    async def _handle_doc_read_github_wiki(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Read GitHub wiki pages."""
        import httpx

        repo = inp["repo"]
        page = inp.get("page", "Home")

        # GitHub wikis are accessible via raw markdown URLs
        url = f"https://raw.githubusercontent.com/wiki/{repo}/{page}.md"

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return {
                        "repo": repo,
                        "page": page,
                        "content": resp.text[:15000],
                    }
                return {"error": f"Wiki page not found (HTTP {resp.status_code})"}
        except Exception as e:
            return {"error": f"Failed to fetch wiki: {e}"}
