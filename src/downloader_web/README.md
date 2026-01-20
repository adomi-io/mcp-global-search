 # Downloader - Web

 A tiny, container-friendly service that fetches docs and files from multiple sources (Git and HTTP) and writes them into subfolders of a target directory. It’s typically used to populate a shared `output/` directory that other services (like a file loader or search) can consume.

 On startup the service performs an initial sync from a YAML config, then exposes simple endpoints so you can trigger refreshes and check health.

 ## Highlights

 - 🔗 Pull content from multiple sources:
   - Git repositories (optional `subpath`, and specific `ref` branch/tag/SHA)
   - HTTP endpoints (with optional custom headers)
 - 🗂️ Merge results under a single root (`DOCS_ROOT`), organized by each source’s `destination` subfolder
 - 🧪 Health endpoint that reports when the initial sync has completed
 - 🔁 On-demand refresh endpoint to re-sync without restarting the container
 - 🪵 Minimal logs and a `.ready` marker to coordinate with dependent services

 ## What you can do

 - Define a list of sources in a YAML file and download them all in one go
 - Keep a local folder of docs/files up to date for downstream tools
 - Trigger re-downloads via an HTTP call


 ## Getting started

 This repository includes a Docker Compose setup that runs the downloader and related services. Most users should start there.

 > [!WARNING]
 > This application is designed to run via Docker. Install Docker Desktop if you’re on Windows or macOS.
 >
 > https://www.docker.com/products/docker-desktop/

 ### Docker Compose

 There’s a `docker-compose.yml` at the project root that defines the `downloader_web` service.

 Run just this service and write files to a local Docker volume mounted at `./output` (via the `output_data` volume):

 ```
 # Start only the downloader_web service
 docker compose up -d downloader_web

 # Check health
 curl -s http://localhost:8080/health | jq
 ```

 The compose file maps:
 - `./data-sources.yml` (read-only) → container `/config/download.yml`
 - `output_data` volume → container `/volumes/output` (exposed locally via a named Docker volume)

 If you want to directly write to a host folder instead, you can replace the volume in compose with a bind mount, for example:

 ```
     volumes:
       - ./output:/volumes/output
       - ./data-sources.yml:/config/download.yml:ro
 ```

 ### Running from source

 If you want to run the Flask app directly from your host:

 ```
 python3 src/downloader_web/app.py
 ```

 Environment variables you set in your shell will be respected (see the table below). By default the server binds to port `8080`.


 ## Open the application

 The service exposes a simple HTTP API (not a UI).

 - Health: `GET http://localhost:8080/health`
 - Refresh: `POST http://localhost:8080/refresh`

 Examples:

 ```
 # Health
 curl -s http://localhost:8080/health | jq

 # Trigger a background refresh
 curl -X POST http://localhost:8080/refresh
 ```

 A healthy response looks like:

 ```
 {
   "healthy": true,
   "initial_done": true,
   "refreshing": false,
   "error": null,
   "last_stats": { /* present after a refresh with details per destination */ }
 }
 ```

 The service returns `503` from `/health` until the initial download completes and the `.ready` marker exists under `DOCS_ROOT`.


 ## Configure sources

 Create a YAML file describing what to download. This repository already uses `./data-sources.yml` mounted to `/config/download.yml` in the container.

 ```
 config:
   sources: [ ... ]
   loaders: [ ... ]
   destinations: { ... }
   collections: { ... }
 ```

 Notes on schema and filtering:
 - Each `source.destination` is relative to `DOCS_ROOT` and will be created if missing.
 - HTTP `headers` values beginning with `$` are resolved from environment variables.
 - Git `ref` is optional; if omitted, the default branch will be used.
 - Filtering rules:
   - Patterns use shell-style globs (fnmatch), e.g., `**/*.md`, `docs/**`, `nitro/pnpm-lock.yaml`.
   - Per-source `include`/`exclude` match paths relative to that source's `destination`.
   - Precedence: includes are restrictive (if provided, only matching files pass); excludes always win last and remove matches even if included.
   - If no filters are set, behavior is unchanged and all files are copied as before.

 Example for the request “do not copy nitro/pnpm-lock.yaml”: add this to the corresponding `source` entry that targets the `nitro` destination:

 ```
 config:
   sources:
     - type: git
       repo: https://github.com/nitrojs/nitro.git
       subpath: docs
       destination: nitro
       exclude:
         - "pnpm-lock.yaml"
 ```

 ### Full configuration example

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
     learning:
       description: |
         Guides and tutorials
       destinations:
         - guides

   loaders:
     - path: guides
       type: frontmatter

   sources:
     - type: git
       repo: https://github.com/example/docs.git
       subpath: docs
       ref: main
       destination: docs
       include:
         - "**/*.md"

     - type: git
       repo: https://github.com/example/guides.git
       subpath: content
       destination: guides
       exclude:
         - "**/pnpm-lock.yaml"

     - type: http
       url: https://example.com/guide.md
       filename: getting-started.md
       destination: guides
 ```


 ## Authentication for Git/GitHub

 - For generic HTTPS Git repos, set `GIT_TOKEN` (and optionally `GIT_USERNAME`, defaults to `x-access-token`). The token is injected via an HTTP header for `git` commands.
 - For GitHub repos and shorthand like `owner/repo`, the service uses the GitHub CLI (`gh`) for shallow clone. Set `GH_TOKEN` with a GitHub token that has repo read access.

 If `GH_TOKEN` is not provided for GitHub-based sources, cloning will fail early with an error.


 ## Environment variables (quick reference)

 | Variable | Default | Notes |
 |---|---|---|
 | `PORT` | `8080` | HTTP server port |
 | `DOCS_ROOT` | `/volumes/output` | Destination directory for downloaded files |
 | `STATE_ROOT` | `/volumes/state` | Working/state directory used for staging/clones |
 | `CONFIG_FILE` | `/config/download.yml` | Path to the YAML config inside the container |
 | `MAX_WORKERS` | `4` | Parallelism for destination refreshes |
 | `HTTP_TIMEOUT_SECS` | `30` | HTTP timeout for downloads |
 | `LOG_LEVEL` | `INFO` | Python logging level (e.g., DEBUG, INFO, WARNING) |
 | `GIT_TOKEN` | empty | Token for generic Git over HTTPS |
 | `GIT_USERNAME` | `x-access-token` | Username paired with `GIT_TOKEN` for Basic auth header |
 | `GH_TOKEN` | empty | GitHub token used by the `gh` CLI for GitHub sources |

 > [!TIP]
 > With the provided `docker-compose.yml`, volumes and envs are set for you:
 > - `output_data` volume → `/volumes/output`
 > - `./data-sources.yml` (read-only) → `/config/download.yml`


 ## Typical data flow

 - Service starts and reads `CONFIG_FILE`
 - Sources are grouped by their `destination` and downloaded into per-destination staging dirs under `STATE_ROOT`
 - The contents of staging replace the corresponding subfolder under `DOCS_ROOT` (a `.ready` file is written under `DOCS_ROOT`)
 - Dependent services wait for `/health` to be healthy, then process files from `DOCS_ROOT`
 - You can `POST /refresh` to perform the download/swap again without restarting


 ## Updating

 If you edit `data-sources.yml`, trigger a refresh:

 ```
 curl -X POST http://localhost:8080/refresh
 ```

 Or restart the service:

 ```
 docker compose restart downloader_web
 ```


 ## Troubleshooting

 - Health is red (`503`): check logs for errors with `docker compose logs -f downloader_web`.
 - No files are written: ensure your sources include a valid `destination` and that your filters aren’t excluding everything.
 - Permission issues on the host: when using a bind mount, verify the `output/` folder exists and is writable.
 - HTTP source with auth: set an environment variable and reference it in `headers` using `$VARNAME`.
 - GitHub source fails: ensure `GH_TOKEN` is set and valid.
 - Git subpath not found: ensure `subpath` exists at the specified `ref`.


 ## Related services in this repo

 - [File Loader](../file_loader/README.md): watches/loads files from `./output` into Meilisearch
 - [MCP Server](../mcp_server/README.md): a small MCP server that queries Meilisearch over the indexed files

 This downloader keeps `./output` up to date so those services see fresh content.
