# File Loader

A lightweight, container-friendly indexer that watches a documents folder and loads files into Meilisearch. It performs an initial full load, then watches for file changes (create/modify/delete) and updates the appropriate Meilisearch index in near real time.

This service is typically paired with `downloader_web`, which populates `./output/` with content from multiple sources. `file_loader` then indexes those files so they are searchable.

## Highlights

- Index local files into Meilisearch
- Watches for changes and upserts/deletes accordingly
- Smart content handling:
  - Frontmatter-aware Markdown (extracts YAML frontmatter plus body)
  - YAML, JSON, and CSV are parsed and stored as structured `data`
  - Text-like files (for example, `.md`, `.txt`, `.py`) are stored as `content`
- Size and extension filters to avoid indexing unsuitable files
- Minimal, clear logs; resilient upsert with retries
- Uses Meilisearch master key for authenticated API calls


## How indexing is organized

- The target directory is `DOCS_DIR` (mounted to `./output` in Docker Compose).
- Each file is indexed into an index named after its top-level folder under `DOCS_DIR`.
  - Example: `output/nuxt/guide/intro.md` → index `nuxt`
  - Example: `output/nitro/1.docs/hello.md` → index `nitro`
  - Files placed directly under `DOCS_DIR` (no top-level folder) are skipped.
- Document IDs are stable SHA1 hashes of each file’s relative path.


## Getting started

This repository includes a Docker Compose setup that runs Meilisearch, `downloader_web`, and `file_loader`. Most users should start there.

> [!WARNING]
> This application is designed to run via Docker. Install Docker Desktop if you are on Windows or macOS.
>
> https://www.docker.com/products/docker-desktop/

### Docker Compose

The root `docker-compose.yml` defines a `file_loader` service. Typical flow:

- `downloader_web` fetches sources into `./output`
- `file_loader` waits for Meilisearch to be healthy and then indexes files from `./output`
- As files change, `file_loader` updates Meilisearch automatically

Bring the stack up:

```
docker compose up -d meilisearch downloader_web file_loader
```

### Running from source

If you prefer to run the loader directly:

```
python3 src/file_loader/loader.py
```

Environment variables from your shell will be used (see the table below). There is no HTTP server exposed by `file_loader`.


## Environment variables (quick reference)

| Variable | Default | Notes |
|---|---|---|
| `MEILISEARCH_HOST` | `http://meilisearch:7700` | Base URL for Meilisearch |
| `MEILISEARCH_MASTER_KEY` | (empty) | Required for authenticated requests; set to your master key |
| `MEILISEARCH_BATCH_SIZE` | `200` | Max documents per upsert batch |
| `MEILISEARCH_MAX_BYTES` | `2097152` (2 MiB) | Skip files larger than this size |
| `MEILISEARCH_ALLOWED_EXTS` | `.md,.mdx,.txt,.json,.yml,.yaml,.toml,.js,.ts,.vue,.css,.html,.sh,.py` | Comma-separated list of allowed file extensions |
| `DOCS_DIR` | `/volumes/output` | Directory to scan/watch (mounted to `./output` in Compose) |
| `CONFIG_FILE` | `/config/download.yml` | YAML config used for optional loader rules (see below) |
| `LOG_LEVEL` | `INFO` | Python logging level (for example, DEBUG, INFO, WARNING) |

> [!TIP]
> When running with the provided `docker-compose.yml`, volumes and envs are set for you:
- `./output` → `/volumes/output`
- `./data-sources.yml` (read-only) → `/config/download.yml`


## File formats and loader types

`file_loader` uses a combination of extension inference and optional rules to decide how to interpret content. Supported loader types:

| Loader type | What it does | Typical extensions |
|---|---|---|
| `frontmatter` | Extracts `data` (YAML frontmatter) and `content` (Markdown body) | `.md`, `.mdx` |
| `yaml` | Parses file content into structured `data` | `.yml`, `.yaml` |
| `json` | Parses file content into structured `data` | `.json` |
| `csv` | Parses into an array of objects in `data` | `.csv` |
| default | Stores text content as `content` (with basic metadata) | `.txt`, `.py`, `.js`, `.ts`, `.vue`, `.css`, `.html`, `.sh`, etc. |

Note: If `MEILISEARCH_ALLOWED_EXTS` is set, only files with those extensions are considered. Hidden files and large files over `MEILISEARCH_MAX_BYTES` are skipped.


## Optional loader rules (via CONFIG_FILE)

You can define rules to influence how certain files are handled. These rules are read from `CONFIG_FILE` (same YAML file used by `downloader_web`). The `loaders` key is optional.

Schema (simplified):

```
loaders:
  - path: nuxt                 # required; relative folder under DOCS_DIR to watch
    type: frontmatter          # optional; one of: frontmatter, yaml, json, csv
    match:                     # optional filename matching
      glob: "*.md"            # shell-style pattern
```

Notes:
- `path` is a top-level folder under `DOCS_DIR` (for example, `nuxt`, `nitro`, `odoo`).
- If `type` is omitted, the loader will infer from file extension (`.md` → `frontmatter`, `.json` → `json`, etc.).
- If `match.glob` is provided, the rule is applied only to filenames matching that pattern within the specified `path`.


## Document shape

Each indexed document contains metadata and (depending on loader type) content and or parsed data. Example for a Markdown file with frontmatter:

```
{
  "id": "<sha1 of relative path>",
  "path": "nuxt/guide/intro.md",
  "filename": "intro.md",
  "ext": "md",
  "bytes": 1234,
  "data": { "title": "Intro", "tags": ["guide"] },
  "content": "# Intro\n..."
}
```

For YAML, JSON, and CSV, the parsed structure is placed in `data`. Plain text-like files will typically only include `content`.


## Typical data flow

- Service starts and reads `CONFIG_FILE` (for optional loader rules)
- Waits for Meilisearch to become healthy
- Ensures target indexes are available as files are discovered
- Performs an initial full load from `DOCS_DIR` (batched upserts)
- Starts a filesystem watcher; on changes:
  - Create or Modify → upsert document
  - Delete → remove document by ID


## Updating and troubleshooting

- If indexing seems stale, ensure `downloader_web` is healthy and that files exist under `./output`.
- Check logs:
  - `docker compose logs -f file_loader`
- Permission or ownership issues:
  - The Compose file runs the container with your UID or GID. Ensure `./output` exists and is writable by your user.
- Authentication errors:
  - Verify `MEILISEARCH_MASTER_KEY` is set in your environment or Compose.
- Files not appearing:
  - Confirm the file’s extension is in `MEILISEARCH_ALLOWED_EXTS` and its size is under `MEILISEARCH_MAX_BYTES`.
  - Files directly under `./output` (no top-level folder) are intentionally skipped.


## Related services in this repo

- [Downloader - Web](../downloader_web/README.md): fetches and refreshes files into `./output`
- [MCP Server](../mcp_server/README.md): queries Meilisearch over the indexed files

This loader keeps Meilisearch synchronized with files produced by the downloader.
