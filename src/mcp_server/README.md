# 🔌 Meilisearch MCP Server (User Documents / Memory)

A minimal, container‑friendly Model Context Protocol (MCP) server that exposes your user‑loaded documents in Meilisearch as high‑signal “memory.”

Use this MCP to help AI agents reliably reference documents the user explicitly loaded (preferences, project docs, playbooks, decisions, etc.).

The server supports HTTP transport by default (good for Docker) and can also run over stdio.

## Highlights

- 📚 Purpose‑built for “user memory” — search only what the user loaded
- 🔐 Auth via `MEILISEARCH_MASTER_KEY` (required)
- 🧭 Safe index filtering with `MEILISEARCH_ALLOWED_INDEXES` (optional allow‑list)
- 📂 File fetch helper to retrieve exact source text for grounding
- 🚦 Simple to run standalone or via Docker Compose

## What you can do

- List available Meilisearch indexes that represent the user’s loaded documents
- Search an index (or run multiple searches at once)
- Fetch the exact file contents referenced by a search hit

## Getting started

Most users should run this server via the provided Docker Compose setup in the project root. You’ll also need a running Meilisearch with a master key.

> This application is designed to run via Docker. Install Docker Desktop if you’re on Windows or macOS.
>
> https://www.docker.com/products/docker-desktop/

### Docker Compose

A service named `mcp_server` is defined in the root `docker-compose.yml`. It connects to the `meilisearch` service and exposes MCP over HTTP on port `8000`.

- Meilisearch: `http://localhost:7700`
- MCP server (HTTP): `http://localhost:8000`

Bring up the stack (Meilisearch + downloader + loader + MCP):

```
docker compose up -d --build
```

Ensure you provide `MEILISEARCH_MASTER_KEY` in your environment before starting compose, for example:

```
export MEILISEARCH_MASTER_KEY=your_meili_master_key
export MEILISEARCH_ALLOWED_INDEXES="nuxt docs"   # optional

docker compose up -d --build mcp_server
```

### Running from source

If you want to run the MCP server directly (outside Docker):

```
python3 src/mcp_server/meili_mcp.py
```

By default, it serves MCP over HTTP on `0.0.0.0:8000`. To use stdio instead, set `MCP_TRANSPORT=stdio`.

Required environment variables must be present in your shell (see the table below).

## How to connect (MCP transports)

This server uses the FastMCP framework and supports two transports:

- HTTP (default): controlled by `MCP_HOST` and `MCP_PORT`
- stdio: set `MCP_TRANSPORT=stdio` to run over stdio

Examples:

```
# HTTP (default)
MCP_TRANSPORT=http MCP_HOST=127.0.0.1 MCP_PORT=8000 python3 src/mcp_server/meili_mcp.py

# stdio
MCP_TRANSPORT=stdio python3 src/mcp_server/meili_mcp.py
```

Your MCP‑capable client/tooling can connect to the HTTP endpoint or spawn the process for stdio depending on its capabilities.

## Tools exposed by the MCP

All tools require a valid Meilisearch master key via `MEILISEARCH_MASTER_KEY`.

### `list_document_indexes(limit=200, offset=0)`

Discover available document indexes (start here). If `MEILISEARCH_ALLOWED_INDEXES` is set, results are filtered to only allowed indexes.

Returns the Meilisearch `/indexes` JSON (commonly includes `results`, `limit`, `offset`, `total`).

Typical flow:
- Call this first to see what indexes exist
- Choose the most relevant index UID(s)
- Then search with `search_documents()` or `search_all_documents()`

### `search_documents(uid, q, limit=20, offset=0)`

Search a single index with keywords or a short natural‑language question.

- If multiple sub‑questions exist, run multiple calls or use `search_all_documents()`
- On success, returns the Meilisearch search JSON plus `ok: true` and `uid`
- If `MEILISEARCH_ALLOWED_INDEXES` is set and `uid` is not allowed, returns `{ ok:false, error: "Index not allowed" }`

### `search_all_documents(queries)`

Batch version of `search_documents`. Provide a list of query objects like:

```
[
  { "uid": "docs", "q": "deployment steps", "limit": 5 },
  { "uid": "nuxt", "q": "content v4 breaking changes" }
]
```

Returns an array of per‑query results with `ok` flags and any error info inline.

### `get_document_file(path)`

Fetch the exact source bytes/text for a file under `FILES_ROOT` (used to ground answers in the original content).

- Returns UTF‑8 text when possible; otherwise Base64 bytes
- Blocks path traversal — the resolved path must remain under `FILES_ROOT`
- If `MEILISEARCH_ALLOWED_INDEXES` is set, only files whose first path segment matches an allowed index are accessible (e.g., with `ALLOWED=["nuxt","docs"]`, `nuxt/…` is allowed).

## Environment variables (quick reference)

| Variable | Default | Notes |
|---|---|---|
| `MEILISEARCH_HOST` | `http://meilisearch:7700` | Base URL for Meilisearch |
| `MEILISEARCH_MASTER_KEY` | — | Required. Bearer token used for Meilisearch API calls |
| `MEILISEARCH_ALLOWED_INDEXES` | empty | Optional allow‑list of index UIDs (space/comma/newline separated). Filters list/search and restricts file fetches by first path segment |
| `FILES_ROOT` | `/volumes/output` | Root directory of loaded files used by `get_document_file()` |
| `MCP_TRANSPORT` | `http` | `http` or `stdio` |
| `MCP_HOST` | `0.0.0.0` | HTTP bind host when `MCP_TRANSPORT=http` |
| `MCP_PORT` | `8000` | HTTP bind port when `MCP_TRANSPORT=http` |

When running with the provided `docker-compose.yml`:
- `./output` is mounted read‑only at `/volumes/output`
- `MEILISEARCH_HOST` is set to `http://meilisearch:7700`
- You must set `MEILISEARCH_MASTER_KEY` in your shell (export) before starting compose

## Typical data flow

1) Documents are downloaded into `./output` by `downloader_web`
2) `file_loader` reads from `./output` and indexes into Meilisearch
3) This MCP lists/searches those indexes and can read exact files from `./output` via `get_document_file()`

## Updating

If you change your allow‑list or other env vars, restart the service:

```
docker compose restart mcp_server
```

## Troubleshooting

- Missing/invalid master key: the server fails fast with `MEILISEARCH_MASTER_KEY is required but not set`
- Can’t see indexes you expect: verify `MEILISEARCH_ALLOWED_INDEXES` (if set) and that Meilisearch contains those indexes
- File fetch denied: when allow‑list is active, only files whose first path segment matches an allowed index are accessible
- Connection from client fails: confirm whether your client supports MCP over HTTP or stdio and configure accordingly

## Related services in this repo

- [Downloader - Web](../downloader_web/README.md): fetches docs to `./output`
- [File Loader](../file_loader/README.md): watches/loads files from `./output` into Meilisearch

This MCP sits on top of those services to provide a clean, safe interface to the AI.