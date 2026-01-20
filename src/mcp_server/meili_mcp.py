#!/usr/bin/env python3
"""
Meilisearch MCP Server — User Documents / Memory (auth via MEILISEARCH_MASTER_KEY only)

This MCP exposes a high-signal “user memory” corpus: documents the user explicitly asked to load so the AI
can reliably reference them. Treat these indexes as the user’s primary knowledge base for preferences,
project docs, playbooks, decisions, and other durable context.

Install:
  pip install fastmcp httpx starlette pydantic

Run:
  export MEILISEARCH_HOST="http://127.0.0.1:7700"
  export MEILISEARCH_MASTER_KEY="..."
  # Optional ceiling allowlist:
  export MEILISEARCH_ALLOWED_INDEXES="nuxt,odoo,nuxt-auth-utils"
  python meili_mcp.py
"""

from __future__ import annotations

import base64
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Set, Tuple, Union, Literal, cast
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from pydantic import BaseModel, ConfigDict
from starlette.requests import Request


# ------------------------------
# Types / Models (FastMCP-friendly)
# ------------------------------

class MCPError(BaseModel):
    """Standard error payload returned by tools when access is denied or input is invalid."""
    ok: Literal[False] = False
    error: str
    hint: str = "Call list_document_indexes() to check the available indexes you can access."
    uid: Optional[str] = None
    requested: Optional[str] = None
    disallowed: Optional[List[str]] = None

    model_config = ConfigDict(extra="allow")


class MCPData(BaseModel):
    """
    Pass-through payload for Meilisearch JSON responses.
    Extra keys are allowed because Meilisearch responses vary by endpoint and version.
    """
    ok: bool = True

    model_config = ConfigDict(extra="allow")


ToolResult = Union[MCPData, MCPError]


# ------------------------------
# Configuration
# ------------------------------

MEILISEARCH_HOST = (os.getenv("MEILISEARCH_HOST", "http://meilisearch:7700")).rstrip("/")
FILES_ROOT = os.path.abspath(os.getenv("FILES_ROOT", "/volumes/input"))
MEILISEARCH_MASTER_KEY = (os.getenv("MEILISEARCH_MASTER_KEY") or "").strip()

# ENV ceiling allowlist:
# - empty => all indexes allowed
# - non-empty => only these allowed
_ENV_ALLOWED_RAW = os.getenv("MEILISEARCH_ALLOWED_INDEXES", "")


# ------------------------------
# Helpers
# ------------------------------

def _parse_allowed(raw: Optional[str]) -> List[str]:
    """Parse comma/whitespace/newline separated index names."""
    if not raw:
        return []
    parts: List[str] = []
    for chunk in raw.replace("\n", ",").replace(" ", ",").split(","):
        s = chunk.strip()
        if s:
            parts.append(s)
    return parts


ENV_ALLOWED_INDEXES: List[str] = [s.lower() for s in _parse_allowed(_ENV_ALLOWED_RAW)]


def _deny(
    message: str,
    *,
    uid: Optional[str] = None,
    requested: Optional[str] = None,
    disallowed: Optional[List[str]] = None,
    **extra: Any
) -> MCPError:
    err = MCPError(
        error=message,
        uid=uid,
        requested=requested,
        disallowed=disallowed
    )
    for k, v in extra.items():
        setattr(err, k, v)
    return err


def _require_master_key() -> str:
    if MEILISEARCH_MASTER_KEY:
        return MEILISEARCH_MASTER_KEY
    raise RuntimeError("MEILISEARCH_MASTER_KEY is required but not set")


def _request_allowed_indexes() -> Optional[Set[str]]:
    """
    Reads ?allowed_indexes=... from the current HTTP request.

    Returns:
      - None if param not provided or no request context (stdio / not in a request)
      - set() / set(values) if provided (possibly empty)
    """
    req: Request = cast(Request, get_http_request())

    if "allowed_indexes" not in req.query_params:
        return None

    raw = req.query_params.get("allowed_indexes", "")
    return set(
        s.lower()
        for s in _parse_allowed(raw)
    )


def _effective_allowed_indexes() -> Optional[Set[str]]:
    """
    Effective allowlist for *this request*.

    Returns:
      - None => unrestricted (allow all)
      - set() => allow nothing
      - set({...}) => allow only these

    Rules:
      - ENV allowlist (if set) is the ceiling.
      - Query param (if provided) can only restrict further.
      - If ENV is empty (unrestricted), query param becomes the restriction when present.
    """
    env_set: Optional[Set[str]]

    if ENV_ALLOWED_INDEXES:
        env_set = set(ENV_ALLOWED_INDEXES)
    else:
        env_set = None

    req_set = _request_allowed_indexes()

    if req_set is None:
        return env_set

    if env_set is None:
        return req_set

    return env_set.intersection(req_set)


def _is_allowed_index(uid: str) -> bool:
    allowed = _effective_allowed_indexes()
    return allowed is None or uid.lower() in allowed


def _is_allowed_path(path: str) -> bool:
    """
    When restricted, only allow files under first-segment folders in allowed set.
    Example: allowed={"nuxt","docs"} => "nuxt/3.guide/..." is allowed.
    """
    allowed = _effective_allowed_indexes()

    if allowed is None:
        return True

    rel = path.lstrip("/\\")
    first = rel.split("/", 1)[0].split("\\", 1)[0]

    return first.lower() in allowed


def _safe_join(base: str, *paths: str) -> Tuple[bool, str]:
    """Prevent path traversal by requiring the resolved target to remain under base."""
    target = os.path.abspath(os.path.join(base, *paths))
    base_abs = os.path.abspath(base)

    try:
        common = os.path.commonpath([base_abs, target])
    except ValueError:
        return False, target

    return (common == base_abs), target


# ------------------------------
# FastMCP app + lifespan
# ------------------------------

STATE: Dict[str, Any] = {
    "client": None
}


@asynccontextmanager
async def lifespan(_: FastMCP):
    """
    FastMCP lifespan hook: initializes an HTTP client for Meilisearch.
    """
    _require_master_key()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {MEILISEARCH_MASTER_KEY}"
    }

    STATE["client"] = httpx.AsyncClient(
        base_url=MEILISEARCH_HOST,
        headers=headers
    )

    try:
        yield
    finally:
        await STATE["client"].aclose()
        STATE["client"] = None


mcp = FastMCP(
    "meilisearch-mcp",
    lifespan=lifespan,
    stateless_http=True
)


async def _client() -> httpx.AsyncClient:
    c = STATE["client"]

    if c is None:
        raise RuntimeError("HTTP client not initialized.")

    return cast(httpx.AsyncClient, c)


# ------------------------------
# Tools
# ------------------------------

@mcp.tool()
async def list_document_indexes(
    limit: int = 200,
    offset: int = 0
) -> ToolResult:
    """
    List available document indexes (start here).

    Access rules:
      - MEILISEARCH_ALLOWED_INDEXES (env) is a ceiling allowlist if set.
      - ?allowed_indexes=... further restricts to the subset of the ceiling (or all, if ceiling unset).
      - If the effective allowlist is empty, returns zero indexes and ok=false so the agent
        learns it has no access and should re-check available indexes.
    """
    c = await _client()

    r = await c.get(
        "/indexes",
        params={
            "limit": limit,
            "offset": offset
        }
    )
    r.raise_for_status()

    data = r.json()
    allowed = _effective_allowed_indexes()

    if not isinstance(data, dict):
        return MCPData(
            ok=True,
            data=data
        )

    if allowed is not None and len(allowed) == 0:
        data["results"] = []
        data["total"] = 0
        data["ok"] = False
        data["error"] = "No indexes are allowed for this request (check ?allowed_indexes)."
        data["hint"] = "Call list_document_indexes() to check the available indexes you can access."

        return MCPData(
            **data
        )

    if allowed is not None:
        results = data.get("results")

        if isinstance(results, list):
            filtered: List[Any] = []

            for item in results:
                if isinstance(item, dict):
                    uid = item.get("uid") or item.get("UID") or item.get("Uid")

                    if isinstance(uid, str) and uid.lower() in allowed:
                        filtered.append(item)

            data["results"] = filtered
            data["total"] = len(filtered)

    data.setdefault("ok", True)

    return MCPData(
        **data
    )


@mcp.tool()
async def search_documents(
    uid: str,
    q: str,
    limit: int = 20,
    offset: int = 0
) -> ToolResult:
    """
    Search a single Meilisearch index by UID.

    Denies access if uid is not allowed by the effective allowlist and instructs
    the agent to call list_document_indexes().
    """
    if not _is_allowed_index(uid):
        return _deny(
            "Index not allowed for this request.",
            uid=uid
        )

    body: Dict[str, Any] = {
        "q": q,
        "limit": limit,
        "offset": offset
    }

    c = await _client()

    try:
        r = await c.post(
            f"/indexes/{uid}/search",
            json=body
        )
        r.raise_for_status()

        data = r.json()

        if isinstance(data, dict):
            data.setdefault("ok", True)
            data.setdefault("uid", uid)

            return MCPData(
                **data
            )

        return MCPData(
            ok=True,
            uid=uid,
            data=data
        )

    except httpx.HTTPStatusError as e:
        return _deny(
            "Meilisearch HTTP error",
            uid=uid,
            status=e.response.status_code,
            detail=e.response.text
        )

    except Exception as e:
        return _deny(
            "Request failed",
            uid=uid,
            detail=str(e)
        )


@mcp.tool()
async def get_document_by_id(
    uid: str,
    document_id: Union[str, int],
    fields: Optional[str] = None
) -> ToolResult:
    """
    Fetch a single document from a Meilisearch index by its document ID.

    - Respects the effective allowlist for indexes; denies if `uid` not allowed.
    - `fields` (optional): comma-separated list of fields to retrieve (Meilisearch `fields` query param).
    """
    if not _is_allowed_index(uid):
        return _deny(
            "Index not allowed for this request.",
            uid=uid
        )

    c = await _client()

    params: Dict[str, Any] = {}
    if fields:
        params["fields"] = fields

    # URL-encode the ID to safely include in the path
    encoded_id = quote(str(document_id), safe="")

    try:
        r = await c.get(
            f"/indexes/{uid}/documents/{encoded_id}",
            params=params or None
        )
        r.raise_for_status()

        data = r.json()

        if isinstance(data, dict):
            return MCPData(
                ok=True,
                uid=uid,
                document_id=str(document_id),
                document=data
            )

        return MCPData(
            ok=True,
            uid=uid,
            document_id=str(document_id),
            data=data
        )

    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 404:
            return _deny(
                "Document not found",
                uid=uid,
                requested=str(document_id),
                status=status
            )
        return _deny(
            "Meilisearch HTTP error",
            uid=uid,
            requested=str(document_id),
            status=status,
            detail=e.response.text
        )
    except Exception as e:
        return _deny(
            "Request failed",
            uid=uid,
            requested=str(document_id),
            detail=str(e)
        )


@mcp.tool()
async def search_all_documents(
    queries: List[Dict[str, Any]]
) -> ToolResult:
    """
    Multi-search across multiple indexes in one call (Meilisearch /multi-search).

    Filters out disallowed indexUid entries based on the effective allowlist.
    If everything is disallowed, returns ok=false with a hint to call list_document_indexes().
    """
    allowed = _effective_allowed_indexes()

    if allowed is not None and len(allowed) == 0:
        return _deny(
            "No indexes are allowed for this request (check ?allowed_indexes)."
        )

    filtered_queries: List[Dict[str, Any]] = []
    disallowed: List[str] = []

    for item in queries or []:
        if not isinstance(item, dict):
            continue

        idx = item.get("indexUid") or item.get("indexuid") or item.get("uid")

        if not isinstance(idx, str):
            continue

        if allowed is None or idx.lower() in allowed:
            filtered_queries.append(item)
        else:
            disallowed.append(idx)

    if not filtered_queries:
        return _deny(
            "No allowed queries to search.",
            disallowed=disallowed
        )

    c = await _client()

    r = await c.post(
        "/multi-search",
        json={
            "queries": filtered_queries
        }
    )

    r.raise_for_status()

    data = r.json()

    if isinstance(data, dict):
        data.setdefault("ok", True)

        if disallowed:
            data.setdefault("meta", {})

            if isinstance(data["meta"], dict):
                data["meta"]["disallowed_indexes"] = disallowed

        return MCPData(
            **data
        )

    return MCPData(
        ok=True,
        meta={
            "disallowed_indexes": disallowed
        },
        data=data
    )


@mcp.tool()
async def get_document_file(
    path: str
) -> ToolResult:
    """
    Read a file from FILES_ROOT, used to fetch ground-truth source text.

    Access control:
      - If restricted, the file must live under an allowed top-level folder (first segment).
      - Blocks path traversal (resolved path must remain under FILES_ROOT).
    """
    if not _is_allowed_path(path):
        return _deny(
            "Folder not allowed for this request.",
            requested=path
        )

    safe, target = _safe_join(
        FILES_ROOT,
        path
    )

    if not safe:
        return _deny(
            "Path escapes FILES_ROOT.",
            requested=path
        )

    if not os.path.exists(target):
        return _deny(
            "File not found.",
            requested=path
        )

    if os.path.isdir(target):
        return _deny(
            "Path is a directory.",
            requested=path
        )

    try:
        with open(target, "r", encoding="utf-8") as f:
            text = f.read()

        size = len(text.encode("utf-8"))

        return MCPData(
            ok=True,
            path=target,
            size=size,
            encoding="utf-8",
            content=text
        )

    except UnicodeDecodeError:
        with open(target, "rb") as f:
            data = f.read()

        return MCPData(
            ok=True,
            path=target,
            size=len(data),
            encoding="base64",
            content=base64.b64encode(data).decode("ascii")
        )


# ------------------------------
# Entrypoint
# ------------------------------

def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "http").strip().lower()

    if transport == "http":
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8000"))

        mcp.run(
            transport="http",
            host=host,
            port=port
        )
    else:
        mcp.run(
            transport="stdio"
        )


if __name__ == "__main__":
    main()
