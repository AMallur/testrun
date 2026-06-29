#!/usr/bin/env python3
"""
clause_finder_mcp (HTTP version) — a real, remotely-hosted MCP connector.

Key difference from the local stdio version: this server has no access
to a user's local filesystem (it runs on a remote machine, not theirs).
So instead of taking a `document_path`, the tools take EITHER:
  - `document_url`: a publicly fetchable URL to a .pdf/.docx/.txt, or
  - `document_text`: raw text pasted directly (e.g. by the calling agent,
    after it already extracted/has the text some other way)

Tools (same two as the local version, same logic, new input shape):
  - clause_finder_search
  - clause_finder_list_sections

Transport: streamable HTTP, so this can be deployed and added to Claude
by URL via Settings -> Connectors -> Add custom connector. No auth yet
(open access) -- see README for how to add an API key check later.
"""

import re
from io import BytesIO
from typing import Optional

import httpx
from pydantic import BaseModel, Field, ConfigDict, model_validator
from mcp.server.fastmcp import FastMCP
from pypdf import PdfReader
from docx import Document

# ---------------------------------------------------------------------------
# Server init
# ---------------------------------------------------------------------------

mcp = FastMCP("clause_finder_mcp")

CONTEXT_CHARS = 200
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # 25MB guardrail


# ---------------------------------------------------------------------------
# Shared extraction logic (pure Python, no AI)
# ---------------------------------------------------------------------------

async def _fetch_bytes(url: str) -> bytes:
    """Downloads a document from a URL with size and content-type guardrails."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            chunks = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"Document at '{url}' exceeds the 25MB size limit for this tool."
                    )
                chunks.append(chunk)
            content_type = resp.headers.get("content-type", "")
            return b"".join(chunks), content_type


def _extract_pages_from_bytes(data: bytes, content_type: str, url: str) -> list[str]:
    """Returns a list of page/chunk texts, inferring format from content-type
    and falling back to the URL's extension."""
    lower_url = url.lower()

    if "pdf" in content_type or lower_url.endswith(".pdf"):
        reader = PdfReader(BytesIO(data))
        return [page.extract_text() or "" for page in reader.pages]

    if "wordprocessingml" in content_type or lower_url.endswith(".docx"):
        doc = Document(BytesIO(data))
        return ["\n".join(p.text for p in doc.paragraphs)]

    # Fall back to treating it as plain text
    return [data.decode("utf-8", errors="replace")]


def _handle_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"Error: Could not fetch document (HTTP {e.response.status_code}). Verify the URL is publicly accessible."
    if isinstance(e, httpx.RequestError):
        return f"Error: Could not reach '{e.request.url}'. Check the URL is correct and publicly reachable."
    if isinstance(e, ValueError):
        return f"Error: {e}"
    return f"Error: Unexpected failure ({type(e).__name__}): {e}"


async def _get_pages(document_url: Optional[str], document_text: Optional[str]) -> list[str]:
    """Resolves either input mode into a list of page/chunk texts."""
    if document_text is not None:
        return [document_text]

    data, content_type = await _fetch_bytes(document_url)
    pages = _extract_pages_from_bytes(data, content_type, document_url)

    if not any(p.strip() for p in pages):
        raise ValueError(
            f"No extractable text found at '{document_url}'. "
            f"It may be a scanned/image-only document."
        )
    return pages


# ---------------------------------------------------------------------------
# Shared input model for "give me a document, one way or another"
# ---------------------------------------------------------------------------

class DocumentSourceMixin(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    document_url: Optional[str] = Field(
        default=None,
        description=(
            "Publicly fetchable URL to a .pdf, .docx, or .txt file, e.g. "
            "'https://example.com/contract.pdf'. Provide this OR document_text, not both."
        ),
    )
    document_text: Optional[str] = Field(
        default=None,
        description=(
            "Raw document text, if you already have it (e.g. extracted earlier "
            "in the conversation). Provide this OR document_url, not both."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_source(self):
        if bool(self.document_url) == bool(self.document_text):
            raise ValueError(
                "Provide exactly one of document_url or document_text, not both and not neither."
            )
        return self


# ---------------------------------------------------------------------------
# Tool 1: clause_finder_search
# ---------------------------------------------------------------------------

class SearchInput(DocumentSourceMixin):
    query: str = Field(
        ..., min_length=2, max_length=200,
        description="Keyword or phrase to search for, e.g. 'indemnification' or '30 days'. Case-insensitive."
    )
    max_results: int = Field(
        default=10, ge=1, le=50,
        description="Maximum number of matches to return (default: 10)."
    )


@mcp.tool(
    name="clause_finder_search",
    annotations={
        "title": "Search Document for Clause",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,  # fetches from arbitrary URLs
    },
)
async def clause_finder_search(params: SearchInput) -> str:
    """Searches a document for a literal keyword or phrase and returns
    each match with surrounding context, plus the page number for PDFs.
    Use this when you need to find a specific clause, term, or figure in
    a document — e.g. "find the indemnification clause". Provide the
    document as a publicly fetchable URL (document_url) or as raw text
    you already have (document_text) — exactly one of the two. Does NOT
    interpret or summarize matches, only locates them. For a structural
    outline first, use clause_finder_list_sections.

    Args:
        params (SearchInput): Validated input containing:
            - document_url (Optional[str]): Public URL to the document
            - document_text (Optional[str]): Raw text, if already available
            - query (str): Keyword/phrase to search for (case-insensitive)
            - max_results (int): Max matches to return (default 10)

    Returns:
        str: Numbered list of matches with context, or "No matches found
        for '<query>'", or "Error: ..." on failure.
    """
    try:
        pages = await _get_pages(params.document_url, params.document_text)
    except Exception as e:
        return _handle_error(e)

    pattern = re.compile(re.escape(params.query), re.IGNORECASE)
    matches = []

    for page_num, page_text in enumerate(pages, start=1):
        for m in pattern.finditer(page_text):
            start = max(0, m.start() - CONTEXT_CHARS)
            end = min(len(page_text), m.end() + CONTEXT_CHARS)
            snippet = page_text[start:end].strip().replace("\n", " ")
            matches.append((page_num, snippet))
            if len(matches) >= params.max_results:
                break
        if len(matches) >= params.max_results:
            break

    if not matches:
        return f"No matches found for '{params.query}'."

    lines = [f"Found {len(matches)} match(es) for '{params.query}':\n"]
    for i, (page_num, snippet) in enumerate(matches, start=1):
        page_label = f" (page {page_num})" if len(pages) > 1 else ""
        lines.append(f"{i}.{page_label} ...{snippet}...")

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 2: clause_finder_list_sections
# ---------------------------------------------------------------------------

class ListSectionsInput(DocumentSourceMixin):
    pass


_HEADING_PATTERN = re.compile(
    r"^\s*((?:ARTICLE|SECTION|CLAUSE)\s+[\dIVXLC]+\.?.*|"
    r"\d+\.\s+[A-Z][A-Za-z ,&'-]{2,60}|"
    r"[A-Z][A-Z ,&'-]{4,60})\s*$",
    re.MULTILINE,
)


@mcp.tool(
    name="clause_finder_list_sections",
    annotations={
        "title": "List Document Sections",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def clause_finder_list_sections(params: ListSectionsInput) -> str:
    """Scans a document for heading-like lines (e.g. 'ARTICLE 3', 'SECTION
    II', '4. Payment Terms', or ALL CAPS headers) and returns them as an
    outline. Use this to get a quick map of a document's structure before
    deciding what to search for with clause_finder_search. Provide the
    document as a publicly fetchable URL (document_url) or raw text
    (document_text) — exactly one of the two. Heading detection is
    pattern-based, not AI — it may miss unusual formats.

    Args:
        params (ListSectionsInput): Validated input containing:
            - document_url (Optional[str]): Public URL to the document
            - document_text (Optional[str]): Raw text, if already available

    Returns:
        str: Numbered list of detected headings in document order, or
        "No section headings detected.", or "Error: ..." on failure.
    """
    try:
        pages = await _get_pages(params.document_url, params.document_text)
    except Exception as e:
        return _handle_error(e)

    headings = []
    for page_text in pages:
        for m in _HEADING_PATTERN.finditer(page_text):
            heading = m.group(1).strip()
            if heading and heading not in headings:
                headings.append(heading)

    if not headings:
        return "No section headings detected. The document may not use a structured heading format."

    return "\n".join(f"{i}. {h}" for i, h in enumerate(headings, start=1))


# ---------------------------------------------------------------------------
# Entry point — streamable HTTP, the transport that makes this URL-addable
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
