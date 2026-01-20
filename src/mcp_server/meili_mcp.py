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
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set, Tuple, Union, cast

import httpx
from fastmcp import FastMCP
from fastmcp.prompts.prompt import Message
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

    model_config = ConfigDict(
        extra="allow",
    )


class MCPData(BaseModel):
    """
    Pass-through payload for Meilisearch JSON responses.
    Extra keys are allowed because Meilisearch responses vary by endpoint and version.
    """
    ok: bool = True

    model_config = ConfigDict(
        extra="allow",
    )


ToolResult = Union[MCPData, MCPError]


# ------------------------------
# Configuration
# ------------------------------

MEILISEARCH_HOST = (os.getenv("MEILISEARCH_HOST", "http://meilisearch:7700")).rstrip("/")
FILES_ROOT = os.path.abspath(os.getenv("FILES_ROOT", "/volumes/input"))
MEILISEARCH_MASTER_KEY = (os.getenv("MEILISEARCH_MASTER_KEY") or "").strip()

# Allow choosing Meili auth header (some setups prefer X-Meili-API-Key).
MEILI_AUTH_HEADER = (os.getenv("MEILI_AUTH_HEADER", "X-Meili-API-Key") or "X-Meili-API-Key").strip()

# ENV ceiling allowlist:
# - empty => all indexes allowed
# - non-empty => only these allowed
_ENV_ALLOWED_RAW = os.getenv("MEILISEARCH_ALLOWED_INDEXES", "")

# Safety/perf knobs
MAX_SEARCH_LIMIT = int(os.getenv("MCP_MAX_SEARCH_LIMIT", "50"))
MAX_LIST_INDEXES_LIMIT = int(os.getenv("MCP_MAX_LIST_INDEXES_LIMIT", "500"))
MAX_Q_LEN = int(os.getenv("MCP_MAX_Q_LEN", "8000"))
MAX_FILE_BYTES = int(os.getenv("MCP_MAX_FILE_BYTES", "1000000"))  # 1MB default


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
    **extra: Any,
) -> MCPError:
    payload: Dict[str, Any] = {
        "error": message,
        "uid": uid,
        "requested": requested,
        "disallowed": disallowed,
        **extra,
    }

    return MCPError(
        **{k: v for k, v in payload.items() if v is not None},
    )


def _require_master_key() -> str:
    if MEILISEARCH_MASTER_KEY:
        return MEILISEARCH_MASTER_KEY

    raise RuntimeError("MEILISEARCH_MASTER_KEY is required but not set")


def _clamp_int(v: int, lo: int, hi: int) -> int:
    try:
        v = int(v)
    except Exception:
        v = lo

    return max(lo, min(hi, v))


def _clean_q(q: str) -> str:
    q = (q or "").strip()

    if len(q) > MAX_Q_LEN:
        q = q[:MAX_Q_LEN]

    return q


def _request_allowed_indexes() -> Optional[Set[str]]:
    """
    Reads ?allowed_indexes=... from the current HTTP request.

    Returns:
      - None if param not provided or no request context (stdio / not in a request)
      - set() / set(values) if provided (possibly empty)
    """
    try:
        req = get_http_request()

        if req is None:
            return None

        req = cast(Request, req)
    except Exception:
        return None

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
    """Prevent path traversal (and symlink escape) by requiring the resolved target to remain under base."""
    base_abs = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base_abs, *paths))

    try:
        common = os.path.commonpath([base_abs, target])
    except ValueError:
        return False, target

    return (common == base_abs), target


# ------------------------------
# FastMCP app + lifespan
# ------------------------------

STATE: Dict[str, Any] = {
    "client": None,
}


@asynccontextmanager
async def lifespan(_: FastMCP):
    """
    FastMCP lifespan hook: initializes an HTTP client for Meilisearch.
    """
    _require_master_key()

    headers = {
        "Content-Type": "application/json",
        MEILI_AUTH_HEADER: MEILISEARCH_MASTER_KEY,
    }

    STATE["client"] = httpx.AsyncClient(
        base_url=MEILISEARCH_HOST,
        headers=headers,
        timeout=httpx.Timeout(
            10.0,
            connect=5.0,
        ),
    )

    try:
        yield
    finally:
        await STATE["client"].aclose()

        STATE["client"] = None


mcp = FastMCP(
    "meilisearch-mcp",
    lifespan=lifespan,
    stateless_http=True,
    instructions=(
        "This server provides a Meilisearch-backed user memory corpus.\n"
        "Workflow:\n"
        "1) Call list_document_indexes() or read meili://indexes to discover allowed indexes.\n"
        "2) Search using search_all_documents() (preferred) or search_documents(uid,...).\n"
        "3) If results mention file paths, fetch ground truth via get_document_file(path) or files://{path*}.\n"
        "Honor allowlists: env MEILISEARCH_ALLOWED_INDEXES is a ceiling; request ?allowed_indexes can further restrict."
    ),
)


async def _client() -> httpx.AsyncClient:
    c = STATE["client"]

    if c is None:
        raise RuntimeError("HTTP client not initialized.")

    return cast(httpx.AsyncClient, c)


# ------------------------------
# Prompts
# ------------------------------

@mcp.prompt(
    name="memory_search",
    title="Search memory (safe workflow)",
    description="Guides an assistant to discover allowed indexes and search them safely.",
    tags={"memory", "meili"},
    meta={"version": "1.0"},
)
def memory_search(query: str) -> List[Message]:
    return [
        Message(
            role="user",
            content=(
                "You have access to a Meilisearch-backed memory corpus.\n"
                "Follow this workflow strictly:\n"
                "1) Call list_document_indexes() OR read resource meili://indexes to discover allowed indexes.\n"
                "2) Search using search_all_documents() (preferred) or search_documents() per index.\n"
                "3) If a result references a file path, fetch ground truth using get_document_file(path) "
                "or read files://{path*}.\n"
                "Return a concise answer and quote/cite file content when available."
            ),
        ),
        Message(
            role="user",
            content=f"User query: {query}",
        ),
    ]


@mcp.prompt(
    name="memory_answer_citation_first",
    title="Answer using citations-first",
    description="Forces grounding in file content when available and explicit uncertainty otherwise.",
    tags={"memory", "files"},
    meta={"version": "1.0"},
)
def memory_answer_citation_first(question: str) -> List[Message]:
    return [
        Message(
            role="user",
            content=(
                "Answer the question using the user's memory corpus.\n"
                "Prefer quoting from files you fetch via files://... or get_document_file().\n"
                "If you cannot fetch a primary source, clearly say so and base your answer only on what you did retrieve."
            ),
        ),
        Message(
            role="user",
            content=f"Question: {question}",
        ),
    ]


# ------------------------------
# Resources
# ------------------------------

@mcp.resource(
    "meili://indexes{?limit,offset}",
    name="Meilisearch Indexes",
    description="Lists available Meilisearch indexes (filtered by allowlist).",
    mime_type="application/json",
    tags={"meili", "read"},
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
async def meili_indexes_resource(limit: int = 200, offset: int = 0) -> Dict[str, Any]:
    limit = _clamp_int(limit, 1, MAX_LIST_INDEXES_LIMIT)
    offset = _clamp_int(offset, 0, 10_000_000)

    c = await _client()

    r = await c.get(
        "/indexes",
        params={
            "limit": limit,
            "offset": offset,
        },
    )

    r.raise_for_status()

    data = r.json()
    allowed = _effective_allowed_indexes()

    if isinstance(data, dict):
        if allowed is not None and len(allowed) == 0:
            return {
                "ok": False,
                "results": [],
                "total": 0,
                "error": "No indexes are allowed for this request.",
            }

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

        return data

    return {
        "results": data,
    }


@mcp.resource(
    "meili://{uid}/search/{q}{?limit,offset}",
    name="Meilisearch Search",
    description="Search a single index (filtered by allowlist).",
    mime_type="application/json",
    tags={"meili", "read"},
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
async def meili_search_resource(uid: str, q: str, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    if not _is_allowed_index(uid):
        return {
            "ok": False,
            "error": "Index not allowed for this request.",
            "uid": uid,
            "hint": "Read meili://indexes",
        }

    limit = _clamp_int(limit, 1, MAX_SEARCH_LIMIT)
    offset = _clamp_int(offset, 0, 10_000_000)
    q = _clean_q(q)

    c = await _client()

    r = await c.post(
        f"/indexes/{uid}/search",
        json={
            "q": q,
            "limit": limit,
            "offset": offset,
        },
    )

    r.raise_for_status()

    data = r.json()

    if isinstance(data, dict):
        data.setdefault("uid", uid)

        return data

    return {
        "uid": uid,
        "data": data,
    }


@mcp.resource(
    "files://{path*}{?encoding,max_bytes}",
    name="Document File",
    description="Reads a file under FILES_ROOT (allowlist + traversal-safe).",
    mime_type="application/json",
    tags={"files", "read"},
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
async def files_resource(path: str, encoding: str = "utf-8", max_bytes: int = 500_000) -> Dict[str, Any]:
    if not _is_allowed_path(path):
        return {
            "ok": False,
            "error": "Folder not allowed for this request.",
            "requested": path,
        }

    safe, target = _safe_join(FILES_ROOT, path)

    if not safe:
        return {
            "ok": False,
            "error": "Path escapes FILES_ROOT.",
            "requested": path,
        }

    if not os.path.exists(target):
        return {
            "ok": False,
            "error": "File not found.",
            "requested": path,
        }

    if os.path.isdir(target):
        return {
            "ok": False,
            "error": "Path is a directory.",
            "requested": path,
        }

    max_bytes = _clamp_int(max_bytes, 1, MAX_FILE_BYTES)

    data = Path(target).read_bytes()
    truncated = False

    if len(data) > max_bytes:
        data = data[:max_bytes]
        truncated = True

    try:
        text = data.decode(encoding)

        return {
            "ok": True,
            "path": target,
            "encoding": encoding,
            "size": len(data),
            "truncated": truncated,
            "content": text,
        }
    except UnicodeDecodeError:
        return {
            "ok": True,
            "path": target,
            "encoding": "base64",
            "size": len(data),
            "truncated": truncated,
            "content": base64.b64encode(data).decode("ascii"),
        }


# ------------------------------
# Tools
# ------------------------------

@mcp.tool()
async def list_document_indexes(limit: int = 200, offset: int = 0) -> ToolResult:
    """
    List available document indexes (start here).

    Access rules:
      - MEILISEARCH_ALLOWED_INDEXES (env) is a ceiling allowlist if set.
      - ?allowed_indexes=... further restricts to the subset of the ceiling (or all, if ceiling unset).
      - If the effective allowlist is empty, returns zero indexes and ok=false so the agent
        learns it has no access and should re-check available indexes.
    """
    limit = _clamp_int(limit, 1, MAX_LIST_INDEXES_LIMIT)
    offset = _clamp_int(offset, 0, 10_000_000)

    c = await _client()

    r = await c.get(
        "/indexes",
        params={
            "limit": limit,
            "offset": offset,
        },
    )

    r.raise_for_status()

    data = r.json()
    allowed = _effective_allowed_indexes()

    if not isinstance(data, dict):
        return MCPData(
            ok=True,
            data=data,
        )

    if allowed is not None and len(allowed) == 0:
        data["results"] = []
        data["total"] = 0
        data["ok"] = False
        data["error"] = "No indexes are allowed for this request (check ?allowed_indexes)."
        data["hint"] = "Call list_document_indexes() to check the available indexes you can access."

        return MCPData(
            **data,
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

    data.setdefault(
        "ok",
        True,
    )

    return MCPData(
        **data,
    )


@mcp.tool()
async def search_documents(uid: str, q: str, limit: int = 20, offset: int = 0) -> ToolResult:
    """
    Search a single Meilisearch index by UID.

    Denies access if uid is not allowed by the effective allowlist and instructs
    the agent to call list_document_indexes().
    """
    if not _is_allowed_index(uid):
        return _deny(
            "Index not allowed for this request.",
            uid=uid,
        )

    q = _clean_q(q)
    limit = _clamp_int(limit, 1, MAX_SEARCH_LIMIT)
    offset = _clamp_int(offset, 0, 10_000_000)

    body: Dict[str, Any] = {
        "q": q,
        "limit": limit,
        "offset": offset,
    }

    c = await _client()

    try:
        r = await c.post(
            f"/indexes/{uid}/search",
            json=body,
        )

        r.raise_for_status()

        data = r.json()

        if isinstance(data, dict):
            data.setdefault("ok", True)
            data.setdefault("uid", uid)

            return MCPData(
                **data,
            )

        return MCPData(
            ok=True,
            uid=uid,
            data=data,
        )

    except httpx.HTTPStatusError as e:
        return _deny(
            "Meilisearch HTTP error",
            uid=uid,
            status=e.response.status_code,
            detail=e.response.text,
        )

    except Exception as e:
        return _deny(
            "Request failed",
            uid=uid,
            detail=str(e),
        )


@mcp.tool()
async def search_all_documents(queries: List[Dict[str, Any]]) -> ToolResult:
    """
    Multi-search across multiple indexes in one call (Meilisearch /multi-search).

    Filters out disallowed indexUid entries based on the effective allowlist.
    If everything is disallowed, returns ok=false with a hint to call list_document_indexes().
    """
    allowed = _effective_allowed_indexes()

    if allowed is not None and len(allowed) == 0:
        return _deny(
            "No indexes are allowed for this request (check ?allowed_indexes).",
        )

    filtered_queries: List[Dict[str, Any]] = []
    disallowed: List[str] = []

    for item in queries or []:
        if not isinstance(item, dict):
            continue

        idx = item.get("indexUid") or item.get("indexuid") or item.get("uid")

        if not isinstance(idx, str):
            continue

        if allowed is not None and idx.lower() not in allowed:
            disallowed.append(idx)
            continue

        q = item.get("q", "")
        item["q"] = _clean_q(q) if isinstance(q, str) else ""

        if "limit" in item:
            item["limit"] = _clamp_int(
                int(item.get("limit") or 0),
                1,
                MAX_SEARCH_LIMIT,
            )

        if "offset" in item:
            item["offset"] = _clamp_int(
                int(item.get("offset") or 0),
                0,
                10_000_000,
            )

        item["indexUid"] = idx
        filtered_queries.append(item)

    if not filtered_queries:
        return _deny(
            "No allowed queries to search.",
            disallowed=disallowed,
        )

    c = await _client()

    r = await c.post(
        "/multi-search",
        json={
            "queries": filtered_queries,
        },
    )

    r.raise_for_status()

    data = r.json()

    if isinstance(data, dict):
        data.setdefault(
            "ok",
            True,
        )

        if disallowed:
            data.setdefault(
                "meta",
                {},
            )

            if isinstance(data["meta"], dict):
                data["meta"]["disallowed_indexes"] = disallowed

        return MCPData(
            **data,
        )

    return MCPData(
        ok=True,
        meta={
            "disallowed_indexes": disallowed,
        },
        data=data,
    )


@mcp.tool()
async def get_document_file(path: str) -> ToolResult:
    """
    Read a file from FILES_ROOT, used to fetch ground-truth source text.

    Access control:
      - If restricted, the file must live under an allowed top-level folder (first segment).
      - Blocks path traversal + symlink escape (resolved path must remain under FILES_ROOT).
      - Caps file size (MCP_MAX_FILE_BYTES); returns truncated content when exceeded.
    """
    if not _is_allowed_path(path):
        return _deny(
            "Folder not allowed for this request.",
            requested=path,
        )

    safe, target = _safe_join(FILES_ROOT, path)

    if not safe:
        return _deny(
            "Path escapes FILES_ROOT.",
            requested=path,
        )

    if not os.path.exists(target):
        return _deny(
            "File not found.",
            requested=path,
        )

    if os.path.isdir(target):
        return _deny(
            "Path is a directory.",
            requested=path,
        )

    try:
        data = Path(target).read_bytes()
        truncated = False

        if len(data) > MAX_FILE_BYTES:
            data = data[:MAX_FILE_BYTES]
            truncated = True

        try:
            text = data.decode("utf-8")

            return MCPData(
                ok=True,
                path=target,
                size=len(data),
                encoding="utf-8",
                truncated=truncated,
                content=text,
            )
        except UnicodeDecodeError:
            return MCPData(
                ok=True,
                path=target,
                size=len(data),
                encoding="base64",
                truncated=truncated,
                content=base64.b64encode(data).decode("ascii"),
            )

    except Exception as e:
        return _deny(
            "File read failed.",
            requested=path,
            detail=str(e),
        )


# ------------------------------
# Entrypoint
# ------------------------------

def main() -> None:
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8000"))

    mcp.run(
        transport="http",
        host=host,
        port=port,
    )


if __name__ == "__main__":
    main()
