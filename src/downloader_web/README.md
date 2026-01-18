 # 📥 Downloader Web

 A tiny, container-friendly service that fetches docs and files from multiple sources (Git and HTTP) and writes them to a target folder. It’s typically used to populate a shared `output/` directory that other services (like a file indexer or search) can consume.

 The service starts, performs an initial download based on a YAML config, and then exposes simple endpoints so you can trigger refreshes and check health.

 ## Highlights

 - 🔗 Pull content from multiple sources:
   - Git repositories (optionally a subpath, and specific ref/branch/tag)
   - HTTP endpoints (with optional custom headers)
 - 🗂️ Merge results into a single destination directory (`DOCS_ROOT`)
 - 🧪 Health endpoint that reports when the initial sync has completed
 - 🔁 On-demand refresh endpoint to re-sync without restarting the container
 - 🪵 Minimal logs and a `.ready` marker to coordinate with dependent services

 ## What you can do

 - Define a list of sources in a YAML file and download them all in one go
 - Keep a local folder of docs/files up to date for downstream tools
 - Trigger re-downloads via an HTTP call

 ---

 ## Getting started

 This repository includes a Docker Compose setup that runs the downloader and related services. Most users should start there.

 > This application is designed to run via Docker. Install Docker Desktop if you’re on Windows or macOS.
 >
 > https://www.docker.com/products/docker-desktop/

 ### Docker Compose

 There’s a `docker-compose.yml` at the project root that defines the `downloader_web` service. To run just this service and write files to `./output`:

 ### Running from source

 If you want to run the Flask app directly:

 ```
 python3 src/downloader_web/app.py
 ```

 Environment variables you set in your shell will be respected (see the table below). By default the server binds to port `8080`.

 ---

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
   "error": null
 }
 ```

 The service returns `503` from `/health` until the initial download completes and the `.ready` marker exists.

 ---

 ## Configure sources

 Create a YAML file describing what to download. You can use the example as a starting point:

 - Example: `src/downloader_web/example.download.yml`
 - Default compose mount: `./data-sources.yml` → container path `/config/download.yml`

 Schema:

 ```
 sources:
   - type: git
     repo: https://github.com/org/repo.git
     subpath: docs            # optional
     ref: main                # optional (branch, tag, or SHA)
     dest: some/folder        # relative to DOCS_ROOT

   - type: http
     url: https://example.com/file.md
     filename: file.md        # optional; defaults to the last path segment
     headers:                 # optional; values starting with $ are env lookups
       Authorization: $TOKEN
     dest: another/folder
 ```

 Notes:
 - `dest` is relative to `DOCS_ROOT` and will be created if missing.
 - HTTP `headers` values beginning with `$` will be resolved from environment variables.
 - Git `ref` is optional; if omitted, the default branch will be used.

 ---

 ## Environment variables (quick reference)

 | Variable | Default | Notes |
 |---|---|---|
 | `PORT` | `8080` | HTTP server port |
 | `DOCS_ROOT` | `/volumes/output` | Destination directory for downloaded files. In compose, bound to `./output` |
 | `DOWNLOAD_CONFIG` | `/config/download.yml` | Path to the YAML sources config inside the container |
 | `LOG_LEVEL` | `INFO` | Python logging level (e.g., DEBUG, INFO, WARNING) |

 When running with the provided `docker-compose.yml`, volumes and envs are set for you:
 - `./output` → `/volumes/output`
 - `./data-sources.yml` (read-only) → `/config/download.yml`

 ---

 ## Typical data flow

 1) Service starts and reads `DOWNLOAD_CONFIG`
 2) Each source is downloaded into a staging directory
 3) The contents of staging replace everything in `DOCS_ROOT` (a `.ready` file is written)
 4) Dependent services (e.g., a file loader/indexer) wait for `/health` to turn healthy, then process files from `DOCS_ROOT`
 5) You can `POST /refresh` to perform steps 2–3 again without restarting

 ---

 ## Updating

 If you edit `data-sources.yml`, trigger a refresh:

 ```
 curl -X POST http://localhost:8080/refresh
 ```

 Or restart the service:

 ```
 docker compose restart downloader_web
 ```

 ---

 ## Troubleshooting

 - Health is red (`503`): check logs for errors with `docker compose logs -f downloader_web`.
 - Permission issues on the host: the compose file runs the container as your UID/GID to keep file ownership correct. Verify the `output/` directory exists and is writable.
 - HTTP source with auth: set an environment variable and reference it in `headers` using `$VARNAME`.
 - Git subpath not found: ensure `subpath` exists at the specified `ref`.

 ---

 ## Related services in this repo

 - `file_loader`: watches/loads files from `./output` into Meilisearch (see `src/file_loader`)
 - `mcp_server`: a small MCP server that queries Meilisearch over the indexed files (see `src/mcp_server`)

 This downloader keeps `./output` up to date so those services see fresh content.
