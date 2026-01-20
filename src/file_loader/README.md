# File Loader

A lightweight, container-friendly indexer that watches a documents folder and loads files into Meilisearch. It performs an initial full load, then watches for file changes (create/modify/delete) and updates the appropriate Meilisearch index in near real time.

This service is typically paired with `downloader_web`, which populates `./output/` with content from multiple sources. `file_loader` then indexes those files so they are searchable.

## Highlights

- Index local files into Meilisearch
- Debounced filesystem watcher that coalesces bursts of changes
- Watches for changes and upserts/deletes accordingly (uses delete-by-filter when available)
- Smart content handling:
  - Frontmatter-aware Markdown (extracts YAML frontmatter plus body)
  - YAML, JSON, and CSV are parsed and stored as structured `data`
  - Text-like files (for example, `.md`, `.txt`, `.py`) are stored as `content`
- Chunking support for long documents via `CHUNK_SIZE` and `CHUNK_OVERLAP`
- Optional semantic embeddings configuration for Meilisearch embedders (OpenAI)
- Skips common temp/swap/hidden files, and respects max-bytes and allowed extensions
- Avoids rehashing unchanged files by checking stored `(bytes, mtime_ns)` first
- Caches per-index settings to avoid repetitive updates
- Minimal, clear logs; resilient upsert with retries
- Uses Meilisearch master key for authenticated API calls


## How indexing is organized

- The target directory is `DOCS_DIR` (mounted to `./output` in Docker Compose).
- Each file is indexed into an index named after its top-level folder under `DOCS_DIR`.
  - Example: `output/nuxt/guide/intro.md` → index `nuxt`
  - Example: `output/nitro/1.docs/hello.md` → index `nitro`
  - Files placed directly under `DOCS_DIR` (no top-level folder) are skipped.
- Index UIDs derived from folder names are sanitized to Meilisearch’s allowed charset
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
docker compose up -d
```

Environment variables from your shell will be used (see the table below). There is no HTTP server exposed by `file_loader`.

## Configuration schema (quick reference)

This service reads optional loader rules from the same YAML used by `downloader_web` via `CONFIG_FILE`.

Schema shape:

```yaml
config:
  sources: []
  loaders: []
  destinations: {}
  collections: {}
```

Full example:

```yaml
config:
  destinations:
    docs:
      description: |
        Docs for the main project
    guides:
      description: |
        Guides and tutorials

  collections:
    project:
      description: |
        Core project documentation
      destinations:
        - docs

  loaders:
    - path: guides
      type: frontmatter

  sources:
    - type: git
      repo: https://github.com/example/docs.git
      subpath: docs
      destination: docs
    - type: http
      url: https://example.com/guide.md
      filename: getting-started.md
      destination: guides
```

See also: shared configuration docs in `src/shared/README.md`.


## Environment variables (quick reference)

| Variable | Default | Notes |
|---|---|---|
| `MEILISEARCH_HOST` | `http://meilisearch:7700` | Base URL for Meilisearch |
| `MEILISEARCH_MASTER_KEY` | (empty) | Required for authenticated requests; set to your master key |
| `MEILISEARCH_BATCH_SIZE` | `200` | Max documents per upsert batch |
| `MEILISEARCH_MAX_BYTES` | `2097152` (2 MiB) | Skip files larger than this size |
| `MEILISEARCH_ALLOWED_EXTS` | `.md,.mdx,.txt,.json,.yml,.yaml,.toml,.js,.ts,.vue,.css,.html,.sh,.py,.csv` | Comma-separated list of allowed file extensions |
| `DOCS_DIR` | `/volumes/input` | Directory to scan/watch (mounted to `./output` in Compose) |
| `CONFIG_FILE` | `/config/download.yml` | YAML config used for optional loader rules (see below) |
| `LOG_LEVEL` | `INFO` | Python logging level (for example, DEBUG, INFO, WARNING) |
| `WATCH_DEBOUNCE_SECONDS` | `0.35` | Debounce window for coalescing rapid file events |
| `CHUNK_SIZE` | `1200` | Approx. characters per chunk for long documents |
| `CHUNK_OVERLAP` | `150` | Characters of overlap between adjacent chunks |
| `EMBEDDINGS_ENABLED` | `false` | Enable Meilisearch embedder configuration (requires OpenAI key) |
| `OPENAI_API_KEY` | (empty) | Required when `EMBEDDINGS_ENABLED=true` |
| `MEILI_EMBEDDER_NAME` | `openai` | Embedder name key in Meilisearch settings |
| `OPENAI_EMBED_MODEL` | `text-embedding-3-small` | OpenAI embedding model name |
| `OPENAI_EMBED_DIMENSIONS` | (empty) | Optional integer dimensions override for the embedding model |
| `MEILI_DOCUMENT_TEMPLATE` | `"{{doc.filename}}\n{{doc.path}}\n\n{{doc.text}}"` | Template used by the embedder to create text to embed |
| `MEILI_TEMPLATE_MAX_BYTES` | `20000` | Max bytes of rendered template per document for embedding |

> [!TIP]
> When running with the provided `docker-compose.yml`, volumes and envs are set for you:
 - `./output` → `/volumes/input`
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

Note: If `MEILISEARCH_ALLOWED_EXTS` is set, only files with those extensions are considered. Hidden files and large files over `MEILISEARCH_MAX_BYTES` are skipped. Common temp/swap files are also ignored.


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


## Optional embeddings (Meilisearch embedders)

The loader can configure Meilisearch’s embedders settings automatically when `EMBEDDINGS_ENABLED=true` and an `OPENAI_API_KEY` is provided. This enables semantic search capabilities in Meilisearch.

- Set `EMBEDDINGS_ENABLED=true`
- Provide `OPENAI_API_KEY`
- Optional tuning:
  - `MEILI_EMBEDDER_NAME` (default `openai`)
  - `OPENAI_EMBED_MODEL` (default `text-embedding-3-small`)
  - `OPENAI_EMBED_DIMENSIONS` (integer; optional)
  - `MEILI_DOCUMENT_TEMPLATE` (default renders filename, path, and text)
  - `MEILI_TEMPLATE_MAX_BYTES` (default `20000`)

At startup, the loader will ensure:

- `filterableAttributes` includes `source_path`
- `embedders[MEILI_EMBEDDER_NAME]` is set with the provided OpenAI configuration

If `EMBEDDINGS_ENABLED=true` but `OPENAI_API_KEY` is missing, embeddings will be disabled and a warning is logged.


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
- Starts a debounced filesystem watcher; on changes:
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

### Notes on tasks and retries

- The loader waits for Meilisearch tasks and logs failures with error payloads.
- Where supported, it uses `delete_documents_by_filter` for precise deletions; otherwise falls back to search + delete.
- Index creation and settings updates are cached per index for the duration of a run to reduce repeated calls.


## Related services in this repo

- [Downloader - Web](../downloader_web/README.md): fetches and refreshes files into `./output`
- [MCP Server](../mcp_server/README.md): queries Meilisearch over the indexed files

This loader keeps Meilisearch synchronized with files produced by the downloader.
