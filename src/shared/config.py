from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

# Environment variable used across apps to locate the YAML config file
CONFIG_ENV_VAR = "CONFIG_FILE"


def _default_config() -> Dict[str, Any]:
    return {
        "sources": [],
        "loaders": [],
        "destinations": {},
        "collections": {},
    }


def _normalize_config(raw: Dict[str, Any] | None) -> Dict[str, Any]:
    """
    Support two shapes:
    - New: { config: { sources: [...], loaders: [...], destinations: {...}, collections: {...} } }
    - Legacy: { sources: [...], include: [...], exclude: [...], loaders: [...] }

    We return a flattened dict with keys: sources, loaders, destinations, collections,
    plus pass-through of legacy include/exclude if present (for backwards compatibility).
    """
    if not raw:
        return _default_config()

    # If top-level key 'config' exists, use it; otherwise treat the whole dict as the config body.
    body: Dict[str, Any]
    if isinstance(raw.get("config"), dict):
        body = dict(raw["config"])  # shallow copy
    else:
        body = dict(raw)

    out: Dict[str, Any] = _default_config()

    # Core sections
    if isinstance(body.get("sources"), list):
        out["sources"] = body.get("sources") or []

    if isinstance(body.get("loaders"), list):
        out["loaders"] = body.get("loaders") or []

    if isinstance(body.get("destinations"), dict):
        out["destinations"] = body.get("destinations") or {}

    if isinstance(body.get("collections"), dict):
        out["collections"] = body.get("collections") or {}

    # Legacy passthroughs (used by downloader_web filtering logic)
    if isinstance(body.get("include"), list):
        out["include"] = body.get("include") or []
    if isinstance(body.get("exclude"), list):
        out["exclude"] = body.get("exclude") or []

    return out


def load_raw_yaml(path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Load YAML from CONFIG_FILE (env) or provided path. Returns empty dict if file missing.
    """
    if path is None:
        cfg_path = Path(os.environ.get(CONFIG_ENV_VAR, "/config/download.yml"))
    else:
        cfg_path = Path(path)

    if not cfg_path.exists():
        return {}

    with cfg_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        return data or {}


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load and normalize the config according to the new schema."""
    raw = load_raw_yaml(path)
    return _normalize_config(raw)


def get_sources(cfg: Dict[str, Any]) -> list[Dict[str, Any]]:
    return list(cfg.get("sources", []) or [])


def get_loaders(cfg: Dict[str, Any]) -> list[Dict[str, Any]]:
    return list(cfg.get("loaders", []) or [])


def get_destinations(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    dest = cfg.get("destinations", {}) or {}
    if isinstance(dest, dict):
        return dest
    return {}


def get_collections(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    col = cfg.get("collections", {}) or {}
    if isinstance(col, dict):
        return col
    return {}
