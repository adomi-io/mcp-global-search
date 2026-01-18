#!/usr/bin/env python3
"""
Meilisearch MCP Server — User Documents / Memory (auth via MEILISEARCH_MASTER_KEY only)

This MCP exposes a high-signal “user memory” corpus: documents the user explicitly asked to load so the AI
can reliably reference them. Treat these indexes as the user’s primary knowledge base for preferences,
project docs, playbooks, decisions, and other durable context.

Recommended agent workflow:
1) Discover indexes first via `list_document_indexes()` whenever you don’t already know the best index.
2) Pick the most relevant index(es) for the user’s prompt and search immediately.
3) If the user prompt contains multiple sub-questions, run multiple searches (either multiple calls to
   `search_documents()` or one call to `search_all_documents()` with multiple query objects).
4) If search results include file paths (or you already have a path), fetch the exact source text using
   `get_document_file()` and ground answers in it.

Install:
  pip install fastmcp httpx

Run:
  export MEILISEARCH_HOST="http://127.0.0.1:7700"
  export MEILISEARCH_MASTER_KEY="..."
  python meili_mcp.py
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Union, Tuple

import httpx
from fastmcp import FastMCP

Json = Optional[Union[bool, int, float, str, List[Any], Dict[str, Any]]]

MEILISEARCH_HOST = (os.getenv("MEILISEARCH_HOST", "http://meilisearch:7700")).rstrip("/")
FILES_ROOT = os.path.abspath(os.getenv("FILES_ROOT", "/volumes/output"))


def _parse_allowed(raw: Optional[str]) -> List[str]:
    if not raw:
        return []

    # support both comma and whitespace separated lists
    parts: List[str] = []

    for chunk in raw.replace("\n", ",").replace(" ", ",").split(","):
        s = chunk.strip()
        if s:
            parts.append(s)

    return parts


_ALLOWED_RAW = os.getenv("MEILISEARCH_ALLOWED_INDEXES", "")
ALLOWED_INDEXES: List[str] = [s.lower() for s in _parse_allowed(_ALLOWED_RAW)]


def _is_restricted() -> bool:
    return len(ALLOWED_INDEXES) > 0


def _is_allowed_index(uid: str) -> bool:
    if not _is_restricted():
        return True

    return uid.lower() in ALLOWED_INDEXES


def _is_allowed_path(path: str) -> bool:
    """
    When restricted, only allow files under first-segment folders in ALLOWED_INDEXES.
    Example: if ALLOWED_INDEXES=["nuxt", "docs"], then path "nuxt/3.guide/..." is allowed.
    """
    if not _is_restricted():
        return True

    rel = path.lstrip("/\\")
    first = rel.split("/", 1)[0].split("\\", 1)[0]

    return first.lower() in ALLOWED_INDEXES


def require_master_key() -> str:
    key = (os.getenv("MEILISEARCH_MASTER_KEY") or "").strip()
    if key:
        return key

    raise RuntimeError("MEILISEARCH_MASTER_KEY is required but not set")


MEILISEARCH_MASTER_KEY = (os.getenv("MEILISEARCH_MASTER_KEY") or "").strip()

STATE: Dict[str, Any] = {
    "client": None
}

@asynccontextmanager
async def lifespan(_: FastMCP):
    headers = {
        "Content-Type": "application/json"
    }

    if not MEILISEARCH_MASTER_KEY:
        headers.clear()
        raise RuntimeError("MEILISEARCH_MASTER_KEY is required but not set")

    headers["Authorization"] = f"Bearer {MEILISEARCH_MASTER_KEY}"

    STATE["client"] = httpx.AsyncClient(base_url=MEILISEARCH_HOST, headers=headers)

    try:
        yield
    finally:
        await STATE["client"].aclose()
        STATE["client"] = None


mcp = FastMCP(
    "meilisearch-mcp",
    lifespan=lifespan,
    stateless_http=True,
)


async def client() -> httpx.AsyncClient:
    c = STATE["client"]

    if c is None:
        raise RuntimeError("HTTP client not initialized.")

    return c


@mcp.tool()
async def list_document_indexes(limit: int = 200, offset: int = 0) -> Json:
    """
    Discover available document indexes (start here).

    These indexes are the user’s loaded documents (“memory”). When you need context, preferences,
    recent documentation, project notes, or anything the user asked to load, list indexes first, then search the best match.

    Recommended workflow:
      1) Call `list_document_indexes()` to see what indexes exist
      2) Choose the most relevant index UID(s)
      3) Search using `search_documents()` or `search_all_documents()`
      4) If you need the exact source text, call `get_document_file()`

    Args:
      - limit: Max number of indexes to return (default 200)
      - offset: Pagination offset (default 0)

    Returns:
      - The Meilisearch `/indexes` JSON response (usually includes `results`, `limit`, `offset`, `total`).
      - If index restrictions are configured, results are filtered to only allowed indexes.
    """
    c = await client()

    r = await c.get("/indexes", params={"limit": limit, "offset": offset})
    r.raise_for_status()

    data = r.json()

    # If restriction is active, filter the results to allowed indexes only
    if _is_restricted() and isinstance(data, dict):
        results = data.get("results")

        if isinstance(results, list):
            filtered = []

            for item in results:
                uid = None
                if isinstance(item, dict):
                    uid = item.get("uid") or item.get("uid".upper())

                if isinstance(uid, str) and _is_allowed_index(uid):
                    filtered.append(item)

            data["results"] = filtered

            # Optionally adjust total to reflect filtered count
            try:
                data["total"] = len(filtered)
            except Exception:
                pass

    return data


@mcp.tool()
async def search_documents(
    uid: str,
    q: str,
    limit: int = 20,
    offset: int = 0,
) -> Json:
    """
    Search a document index (the user’s loaded documents / “memory”).

    Use this as your default step before answering questions that depend on user-provided context.
    If you don’t know the best index, call `list_document_indexes()` first.

    Multi-part prompts:
      - If the user asks multiple things, run multiple searches (either multiple calls to
        `search_documents()` or one call to `search_all_documents()` with multiple query objects).

    Args:
      - uid: Meilisearch index UID (e.g. "memory", "docs", "playbook")
      - q: keywords or a short natural-language question
      - limit: number of hits (default 20)
      - offset: pagination offset (default 0)

    Returns:
      - On success: Meilisearch search JSON plus `ok: true` and `uid`.
      - On failure: {ok:false, error, status?, detail, uid}.
    """
    if not _is_allowed_index(uid):
        return {
            "ok": False,
            "error": "Index not allowed",
            "uid": uid,
        }

    body: Dict[str, Any] = {
        "q": q,
        "limit": limit,
        "offset": offset,
    }

    c = await client()

    try:
        r = await c.post(f"/indexes/{uid}/search", json=body)
        r.raise_for_status()

        data = r.json()

        if isinstance(data, dict):
            data.setdefault("ok", True)
            data.setdefault("uid", uid)

        return data

    except httpx.HTTPStatusError as e:
        return {
            "ok": False,
            "error": "Meilisearch HTTP error",
            "status": e.response.status_code,
            "detail": e.response.text,
            "uid": uid,
        }

    except Exception as e:
        return {
            "ok": False,
            "error": "Request failed",
            "detail": str(e),
            "uid": uid,
        }


@mcp.tool()
async def search_all_documents(queries: List[Dict[str, Any]]) -> Json:
    """
    Search multiple document indexes and/or multiple sub-queries in one call (Meilisearch multi-search).

    Use this when:
      - multiple indexes might contain the answer, or
      - the user prompt has multiple sub-questions and you want parallel retrieval.

    Arguments:
      - queries: A list of Meilisearch multi-search query objects. Each item should include at least:
          - indexUid: str (the target index UID)
          - q: str (the search query)
        Optional fields (per Meilisearch) include `limit`, `offset`, `filter`, `sort`, etc.

        Examples:
          # same question across multiple indexes
          [
            {"indexUid": "docs",   "q": "authentication headers", "limit": 10},
            {"indexUid": "guides", "q": "authentication headers", "limit": 10}
          ]

          # multiple sub-questions within one index
          [
            {"indexUid": "memory", "q": "preferred error handling style", "limit": 10},
            {"indexUid": "memory", "q": "deployment checklist", "limit": 10}
          ]

    Returns:
      - The Meilisearch multi-search JSON response containing results for each query in order.
      - If restrictions are active, disallowed indexes are excluded and reported in `meta.disallowed_indexes`.
      - If nothing is allowed, returns {ok:false, ...}.
    """
    filtered_queries: List[Dict[str, Any]] = []
    disallowed_indexes: List[str] = []

    if _is_restricted():
        for item in queries or []:
            idx = None
            if isinstance(item, dict):
                idx = item.get("indexUid") or item.get("indexuid") or item.get("uid")

            if isinstance(idx, str) and _is_allowed_index(idx):
                filtered_queries.append(item)
            else:
                if isinstance(idx, str):
                    disallowed_indexes.append(idx)
    else:
        filtered_queries = queries

    if not filtered_queries:
        return {
            "ok": False,
            "error": "No allowed queries to search",
            "disallowed": disallowed_indexes,
        }

    c = await client()

    r = await c.post("/multi-search", json={"queries": filtered_queries})
    r.raise_for_status()

    data = r.json()

    # Attach information about disallowed queries (if any) for transparency
    if disallowed_indexes and isinstance(data, dict):
        data.setdefault("meta", {})

        if isinstance(data["meta"], dict):
            data["meta"]["disallowed_indexes"] = disallowed_indexes

    return data


def _safe_join(base: str, *paths: str) -> Tuple[bool, str]:
    target = os.path.abspath(os.path.join(base, *paths))
    base_abs = os.path.abspath(base)

    try:
        common = os.path.commonpath([base_abs, target])
    except ValueError:
        # Different drives on Windows, treat as unsafe
        return False, target

    return (common == base_abs, target)


@mcp.tool()
async def get_document_file(path: str) -> Json:
    """
    Read a file from the user’s loaded documents (ground truth).

    Use when:
      - search results reference a file path, or
      - you need the exact source text to answer precisely.

    Args:
      - path: Relative file path under FILES_ROOT (example: "nuxt/3.guide/3.ai/index.md")

    Returns:
      - On success:
          {
            "ok": true,
            "path": "<absolute normalized path>",
            "size": <bytes>,
            "encoding": "utf-8" | "base64",
            "content": "<text or base64>"
          }
      - On failure:
          { "ok": false, "error": "<reason>", "requested": "<path>" }

    Notes:
      - Path traversal is blocked; the resolved path must remain under FILES_ROOT.
      - Text files are returned as UTF-8 when possible; otherwise bytes are returned as base64.
    """
    import base64

    if not _is_allowed_path(path):
        return {
            "ok": False,
            "error": "Folder not allowed",
            "requested": path,
        }

    safe, target = _safe_join(
        FILES_ROOT,
        path
    )

    if not safe:
        return {
            "ok": False,
            "error": "Path escapes FILES_ROOT",
            "requested": path,
        }

    if not os.path.exists(target):
        return {
            "ok": False,
            "error": "File not found",
            "requested": path,
        }

    if os.path.isdir(target):
        return {
            "ok": False,
            "error": "Path is a directory",
            "requested": path,
        }

    try:
        with open(target, "r", encoding="utf-8") as f:
            text = f.read()

        size = len(text.encode("utf-8"))

        return {
            "ok": True,
            "path": target,
            "size": size,
            "encoding": "utf-8",
            "content": text,
        }

    except UnicodeDecodeError:
        with open(target, "rb") as f:
            data = f.read()

        return {
            "ok": True,
            "path": target,
            "size": len(data),
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
        }


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "http").strip().lower()

    if transport == "http":
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8000"))

        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
