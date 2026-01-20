# Shared Utilities

Utilities shared by all services in this repository (`downloader_web`, `file_loader`, and `mcp_server`).

This package focuses on configuration loading and normalization for a unified YAML schema.

## Contents

- `config.py` — helpers to load and normalize configuration:
  - `CONFIG_ENV_VAR` — environment variable name used to locate the YAML config file (`CONFIG_FILE`).
  - `load_raw_yaml(path: Optional[Path])` — loads raw YAML dict from a path or env.
  - `load_config(path: Optional[Path])` — loads and normalizes the config dict.
  - `get_sources(cfg)` — returns a list of configured sources.
  - `get_loaders(cfg)` — returns a list of configured loaders.
  - `get_destinations(cfg)` — returns the destinations mapping.
  - `get_collections(cfg)` — returns the collections mapping.

These are re-exported from `src/shared/__init__.py` for convenience.

## How configuration works

Configuration schema shape:

```yaml
config:
  sources: []
  loaders: []
  destinations: {}
  collections: {}
```

Normalization returns a flattened dict with the keys:

```yaml
sources: []
loaders: []
destinations: {}
collections: {}
```

### Full example

```yaml
config:
  destinations:
    nuxt:
      description: |
        Nuxt Documentation
    nitro:
      description: |
        Nuxt Nitro documentation

  collections:
    examples:
      description: |
        Personal examples of collections
      destinations:
        - readme-examples
    nuxt:
      description: |
        Nuxt
      destinations:
        - nuxt
        - nitro
        - nuxt-ui
        - nuxt-content

  loaders:
    - path: nuxt
      type: frontmatter

  sources:
    - type: git
      repo: https://github.com/nuxt/nuxt.git
      subpath: docs
      destination: nuxt

    - type: git
      repo: https://github.com/nitrojs/nitro.git
      subpath: docs
      destination: nitro
      exclude:
        - "pnpm-lock.yaml"

    - type: http
      url: https://example.com/guide.md
      filename: guide.md
      destination: readme-examples
```

### Where the file is located

- By default, consumers look for `/config/download.yml` inside their containers.
- You can override this path via the `CONFIG_FILE` environment variable.

Environment variable name is exposed as `shared.CONFIG_ENV_VAR` and equals `"CONFIG_FILE"`.

## Usage examples

### In Python

```python
from shared import (
    CONFIG_ENV_VAR,
    load_config,
    get_sources,
    get_loaders,
    get_destinations,
    get_collections,
)

# Option A: rely on CONFIG_FILE env var
cfg = load_config()

# Option B: provide an explicit path
# from pathlib import Path
# cfg = load_config(Path("./data-sources.yml"))

sources = get_sources(cfg)
loaders = get_loaders(cfg)
destinations = get_destinations(cfg)
collections = get_collections(cfg)

print(f"Found {len(sources)} sources; env var is {CONFIG_ENV_VAR}")
```

### Setting the config path via environment

```bash
export CONFIG_FILE=/abs/path/to/data-sources.yml
python -m your_module
```

In Docker Compose provided by this repo, `./data-sources.yml` on the host is mounted to `/config/download.yml` in containers, so the default works without changes.

## Notes and behavior

- Missing files: `load_raw_yaml` returns an empty dict if the file does not exist.
- Safety: YAML is read via `yaml.safe_load`.
- The helper getters always return the expected type (empty list/dict when absent).

## Related services

- Downloader Web (`src/downloader_web/`) uses per-source filtering and mounts the YAML at `/config/download.yml` by default.
- File Loader (`src/file_loader/`) and MCP Server (`src/mcp_server/`) may also rely on the same config file to understand sources, destinations, and collections.

## Contributing

Keep public functions small, typed, and documented. New helpers should be added to `__all__` in `src/shared/__init__.py` if intended for external use.
