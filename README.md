# Global Search: Docs → Meilisearch → MCP

A self‑hosted Meilisearch powered contextual search for AI agents. Allows AI agents to search in simple terms for documentation, files, and examples that you have provided.

An end‑to‑end, container‑friendly pipeline that:

- Downloads documentation and files from multiple sources into a local `output/` folder
- Indexes those files in Meilisearch for fast, flexible search
- Exposes a minimal Model Context Protocol (MCP) server so AI agents can reliably query “user‑loaded” documents and fetch exact source files for grounding

# Highlights

- 📥 Unified downloader: Git and HTTP sources merged into a single tree (`output/`)
- 🔎 Meilisearch indexing with smart content handling (frontmatter Markdown, YAML/JSON/CSV as structured data)
- 🧭 Safe, explicit scope: indexes are derived from your top‑level folders; optional allow‑list restricts searches and file fetches
- 🔌 MCP server over HTTP or stdio: list indexes, search, and fetch exact files for answer grounding
- 🐳 Batteries included: one `docker compose up -d --build` runs the whole stack
- 🛠️ Extensible by design: adjust loader rules, file filters, and environment without rebuilding images in most cases


# Getting started

> [!WARNING]
> This project is designed to run via Docker. Install Docker Desktop if you’re on Windows or macOS.
>
> https://www.docker.com/products/docker-desktop/

## Create an `.env`

At minimum you must set the Meilisearch master key so dependent services can authenticate.

```bash
echo "MEILISEARCH_MASTER_KEY=$(openssl rand -hex 32)" >> .env
```

Optional variables you can add now or later:

```
# Restrict which Meilisearch indexes the MCP server will expose (space/comma/newline separated)
MEILISEARCH_ALLOWED_INDEXES="docs guides examples"

# Run containers as your host user (helps with file ownership on ./output)
UID=1000
GID=1000
```

## Define your sources

Edit `data-sources.yml` to describe what to download. A minimal example:

```yaml
include:
  - "**/*.md"
exclude:
  - "**/pnpm-lock.yaml"

sources:
  - type: git
    repo: https://github.com/example/docs.git
    subpath: docs
    ref: main
    dest: docs

  - type: http
    url: https://example.com/guide.md
    filename: guide.md
    dest: examples
```

See the Downloader README for the full schema and filtering rules: `src/downloader_web/README.md`.

## Start the stack

```bash
docker compose up -d --build
```

Services and default ports:

- Meilisearch API: http://localhost:7700
- Downloader Web API: http://localhost:8080 (health, refresh)
- MCP server (HTTP): http://localhost:8000

First run will download sources, write into `./output`, index them, and then expose them via the MCP server.

> [!TIP]
> If you edit `data-sources.yml`, you can refresh downloads without restarting:
>
> ```bash
> curl -X POST http://localhost:8080/refresh
> ```


# Services overview

- 📥 downloader_web — fetches files into `./output` ([src/downloader_web/README.md](src/downloader_web/README.md))

- 📂 file_loader — indexes files from `./output` into Meilisearch ([src/file_loader/README.md](src/file_loader/README.md))

- 🔌 mcp_server — exposes your indexes to AI tooling via MCP ([src/mcp_server/README.md](src/mcp_server/README.md))

Compose file: `docker-compose.yml` ties everything together.


# Typical data flow

- downloader_web populates `./output` from your configured sources
- file_loader performs an initial full index into Meilisearch, then watches for changes
- mcp_server lists/searches those indexes and can fetch the exact file content under `./output`


# Updating

- Refresh downloads after changing `data-sources.yml`:

```bash
curl -X POST http://localhost:8080/refresh
```

- Restart services if you change environment variables:

```bash
docker compose restart
```


# Troubleshooting

- Meilisearch not healthy: `docker compose logs -f meilisearch`
- Downloader not ready (`/health` 503): check `docker compose logs -f downloader_web`
- Files not indexed: verify file extensions/size limits in file_loader README and that files live under a top‑level folder in `./output`
- MCP search shows no indexes: confirm `MEILISEARCH_ALLOWED_INDEXES` (if set) and that file_loader created indexes in Meilisearch
- File fetch denied from MCP: path traversal is blocked and, if an allow‑list is set, only first‑segment matches are allowed (e.g., `docs/...`)