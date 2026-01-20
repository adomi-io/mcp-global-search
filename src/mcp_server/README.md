# Meilisearch MCP Server (User Documents / Memory)

A minimal, container‑friendly Model Context Protocol (MCP) server that exposes your user‑loaded documents in Meilisearch as high‑signal “memory.”

Use this MCP to help AI agents reliably reference documents the user explicitly loaded (preferences, project docs, playbooks, decisions, etc.).

The server supports HTTP transport by default (good for Docker) and can also run over stdio.

## Highlights

- 📚 Purpose‑built for “user memory” — search only what the user loaded
- 🔐 Auth via `MEILISEARCH_MASTER_KEY` (required)
- 🧭 Index filtering with `MEILISEARCH_ALLOWED_INDEXES` (optional allow‑list) and per‑request `?allowed_indexes=...` to further restrict
- 📂 File fetch helper to retrieve exact source text for grounding
- 🚦 Simple to run standalone or via Docker Compose
- 🧩 Extra MCP surfaces: resources and prompts in addition to tools

## What you can do

- List available Meilisearch indexes that represent the user’s loaded documents
- Search an index (or run multiple searches at once)
- Fetch the exact file contents referenced by a search hit
- Use built‑in MCP resources and prompts to guide safer flows in clients

## Getting started

Most users should run this server via the provided Docker Compose setup in the project root. You’ll also need a running Meilisearch with a master key.

> [!WARNING]
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

## MCP surfaces

This server exposes three kinds of MCP surfaces that your client may use:

- Tools (RPC‑style calls)
- Resources (readable URIs)
- Prompts (pre‑authored message templates)

Not all clients support all surfaces; the tools are universally useful, while resources/prompts enable safer, guided workflows.

### Resources

The following resources are available to read via MCP:

- `meili://indexes{?limit,offset,allowed_indexes}`
  - Lists available Meilisearch indexes (filtered by env allow‑list and optional per‑request `allowed_indexes`).
  - Returns the Meilisearch `/indexes` JSON. Each item is augmented with metadata when available:
    - `destination`: details from the shared config destination for that index UID
    - `collections`: any collections that include this UID (from shared config)

- `files://{path*}`
  - Fetch the exact bytes/text for a file under `FILES_ROOT`.
  - Respects path traversal protections and the allow‑list rules described below.

Examples (conceptual — use your MCP client’s resource read API):

```
meili://indexes?limit=50&allowed_indexes=docs,nuxt
files://nuxt/some/file.md
```

### Prompts

Two prompts are included to encourage safe and grounded usage:

- `memory_search(query: str)` — guides an assistant to discover allowed indexes, then search, then fetch files for grounding.
- `memory_answer_citation_first(question: str)` — encourages quoting from primary sources first; if none are retrievable, be explicit about uncertainty.

### Tools

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
- If a per‑request `allowed_indexes` is provided (HTTP transport), it further restricts the accessible set to a subset of the ceiling.

## Environment variables (quick reference)

| Variable | Default | Notes |
|---|---|---|
| `MEILISEARCH_HOST` | `http://meilisearch:7700` | Base URL for Meilisearch |
| `MEILISEARCH_MASTER_KEY` | — | Required. Bearer token used for Meilisearch API calls |
| `MEILISEARCH_ALLOWED_INDEXES` | empty | Optional allow‑list of index UIDs (space/comma/newline separated). Acts as a ceiling. Filters list/search and restricts file fetches by first path segment |
| `FILES_ROOT` | `/volumes/input` | Root directory of loaded files used by `get_document_file()` |
| `MCP_TRANSPORT` | `http` | `http` or `stdio` |
| `MCP_HOST` | `0.0.0.0` | HTTP bind host when `MCP_TRANSPORT=http` |
| `MCP_PORT` | `8000` | HTTP bind port when `MCP_TRANSPORT=http` |
| `MCP_MAX_Q_LEN` | `8000` | Max length (chars) for search query strings |
| `MCP_MAX_FILE_BYTES` | `1000000` | Max bytes to return from `get_document_file()` before truncation |

> [!TIP]
> When running with the provided `docker-compose.yml`:
> - `./output` is mounted read‑only at `/volumes/input`
> - `MEILISEARCH_HOST` is set to `http://meilisearch:7700`
> - You must set `MEILISEARCH_MASTER_KEY` in your shell (export) or your .env before starting compose

## Allow‑lists and request‑level restrictions

- `MEILISEARCH_ALLOWED_INDEXES` (env) is a ceiling allow‑list if set.
- `allowed_indexes` (HTTP query parameter) can be provided per request to further restrict to a subset. If the ceiling is unset, this parameter acts as the only allow‑list for that request.
- Both index discovery and search respect these rules. File fetching also checks the first path segment against the effective allow‑list.

## Typical data flow

- Documents are downloaded into `./output` by `downloader_web`
- `file_loader` reads from `./output` and indexes into Meilisearch
- This MCP lists/searches those indexes and can read exact files from `./output` (mounted at `/volumes/input` in the container) via `get_document_file()`

## Updating

If you change your allow‑list or other env vars, restart the service:

```
docker compose restart mcp_server
```

If you would like to update to the latest version of this script:

```
docker compose restart mcp_server
```

## Troubleshooting

- Missing/invalid master key: the server fails fast with `MEILISEARCH_MASTER_KEY is required but not set`
- Can’t see indexes you expect: verify `MEILISEARCH_ALLOWED_INDEXES` (if set), any per‑request `allowed_indexes` you passed, and that Meilisearch contains those indexes
- File fetch denied:
  - When an allow‑list is active, only files whose first path segment matches an allowed index are accessible
  - Ensure the `path` is under `FILES_ROOT` (no traversal outside is allowed)
- Search rejected: if no indexes are allowed for the request, the server returns an informative error. Adjust env allow‑list or the request’s `allowed_indexes`.
- Connection from client fails: confirm whether your client supports MCP over HTTP or stdio and configure accordingly

## Related services in this repo

- [Downloader - Web](../downloader_web/README.md): fetches docs to `./output`
- [File Loader](../file_loader/README.md): watches/loads files from `./output` into Meilisearch

This MCP sits on top of those services to provide a clean, safe interface to the AI.