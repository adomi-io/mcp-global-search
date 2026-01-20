"""
Shared utilities usable by all apps (downloader_web, file_loader, mcp_server).

Currently includes:
- Config loading/normalization for data-sources.yml supporting a top-level
  `config` key (new schema) while remaining compatible with the legacy schema.
"""

from .config import (
    CONFIG_ENV_VAR,
    load_raw_yaml,
    load_config,
    get_sources,
    get_loaders,
    get_destinations,
    get_collections,
)

__all__ = [
    "CONFIG_ENV_VAR",
    "load_raw_yaml",
    "load_config",
    "get_sources",
    "get_loaders",
    "get_destinations",
    "get_collections",
]
