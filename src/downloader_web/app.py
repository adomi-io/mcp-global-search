#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from fnmatch import fnmatch
from typing import Any
import logging

import requests
import yaml
from flask import Flask, jsonify

PORT = int(os.environ.get("PORT", "8080"))
DOCS_ROOT = Path(os.environ.get("DOCS_ROOT", "/volumes/output"))
CONFIG_PATH = Path(os.environ.get("CONFIG_FILE", "/config/download.yml"))
READY_MARKER = DOCS_ROOT / ".ready"

# Logging setup
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [downloader_web] %(levelname)s: %(message)s",
)

logger = logging.getLogger("downloader_web")

app = Flask(__name__)

state: dict[str, Any] = {
    "initial_done": False,
    "last_error": None,
    "refreshing": False,
}

_refresh_lock = threading.Lock()

def sh(cmd: list[str], cwd: Path | None = None) -> str:
    logger.debug(f"Running command: {' '.join(cmd)} (cwd={cwd})")

    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if proc.returncode != 0:
        logger.error("Command failed (%s): %s", proc.returncode, " ".join(cmd))
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout}")

    logger.debug(f"Command succeeded: {' '.join(cmd)}")

    return proc.stdout


def rm_rf(path: Path) -> None:
    if not path.exists():
        return

    if path.is_file() or path.is_symlink():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)


def copy_tree_contents(
    src_dir: Path,
    dst_dir: Path,
    *,
    include_global: list[str] | None = None,
    exclude_global: list[str] | None = None,
    include_source: list[str] | None = None,
    exclude_source: list[str] | None = None,
    dest_rel: str = "",
) -> None:
    """
    Copy contents of src_dir into dst_dir with optional filtering rules.

    Pattern semantics:
    - Global patterns (`include_global`/`exclude_global`) match paths relative to DOCS_ROOT
      (e.g., "nitro/pnpm-lock.yaml"). These can affect any source.
    - Source patterns (`include_source`/`exclude_source`) match paths relative to the
      source destination root (e.g., "**/*.md" inside that source's `dest`).

    Precedence:
    - If any include list is provided, only files matching it are allowed at that scope.
    - Excludes always win last: if a file matches any exclude pattern (global or source), it is skipped.
    """

    def _norm_list(v: list[str] | str | None) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v]

    include_global = _norm_list(include_global)
    exclude_global = _norm_list(exclude_global)
    include_source = _norm_list(include_source)
    exclude_source = _norm_list(exclude_source)

    def _allowed(rel_file_from_source: str) -> bool:
        # relative to DOCS_ROOT
        rel_to_docs = f"{dest_rel}/{rel_file_from_source}" if dest_rel else rel_file_from_source

        def _match_any(path: str, patterns: list[str]) -> bool:
            return any(fnmatch(path, p) for p in patterns)

        # start allowed
        allowed = True

        # apply includes (restrictive)
        if include_global:
            allowed = _match_any(rel_to_docs, include_global)
        if allowed and include_source:
            allowed = _match_any(rel_file_from_source, include_source)

        # apply excludes (override)
        if allowed and exclude_global and _match_any(rel_to_docs, exclude_global):
            return False
        if allowed and exclude_source and _match_any(rel_file_from_source, exclude_source):
            return False

        return allowed

    dst_dir.mkdir(parents=True, exist_ok=True)

    # Walk src_dir to copy files that pass filters
    for root, _dirs, files in os.walk(src_dir):
        root_path = Path(root)
        for fname in files:
            src_file = root_path / fname
            rel = src_file.relative_to(src_dir).as_posix()
            if not _allowed(rel):
                logger.debug("Skipping excluded file: %s (dest=%s)", rel, dest_rel)
                continue

            target = dst_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, target)


def resolve_headers(headers: dict[str, Any]) -> dict[str, str]:
    resolved: dict[str, str] = {}

    for k, v in (headers or {}).items():
        if isinstance(v, str) and v.startswith("$"):
            resolved[k] = os.environ.get(v[1:], "")
        else:
            resolved[k] = str(v)

    return resolved


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {
            "sources": []
        }

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"sources": []}


def download_sources_into(staging_root: Path) -> None:
    cfg = load_config()
    sources = cfg.get("sources", []) or []
    global_include = cfg.get("include", []) or []
    global_exclude = cfg.get("exclude", []) or []

    session = requests.Session()

    for src in sources:
        kind = src.get("type")
        dest_rel = src.get("dest", "") or ""
        dest_path = staging_root / dest_rel
        dest_path.mkdir(parents=True, exist_ok=True)

        if kind == "git":
            repo = src["repo"]
            ref = src.get("ref")  # branch/tag/sha
            subpath = src.get("subpath", "") or ""
            src_include = src.get("include", []) or []
            src_exclude = src.get("exclude", []) or []

            logger.info("Downloading git repo=%s ref=%s subpath=%s -> %s", repo, ref, subpath, dest_rel)

            repo_dir = staging_root / "_repos" / f"repo_{abs(hash((repo, ref))) }"
            rm_rf(repo_dir)
            repo_dir.parent.mkdir(parents=True, exist_ok=True)

            sh(["git", "clone", "--depth=1", repo, str(repo_dir)])

            if ref:
                sh(["git", "fetch", "--depth=1", "origin", str(ref)], cwd=repo_dir)
                sh(["git", "checkout", "FETCH_HEAD"], cwd=repo_dir)

            src_dir = repo_dir / subpath if subpath else repo_dir

            if not src_dir.exists():
                raise RuntimeError(f"subpath does not exist: repo={repo} subpath={subpath}")

            copy_tree_contents(
                src_dir,
                dest_path,
                include_global=global_include,
                exclude_global=global_exclude,
                include_source=src_include,
                exclude_source=src_exclude,
                dest_rel=dest_rel,
            )
            logger.info("Copied repo contents into %s", dest_path)

        elif kind == "http":
            url = src["url"]
            filename = src.get("filename") or (Path(url).name or "file.txt")
            headers = resolve_headers(src.get("headers", {}) or {})
            src_include = src.get("include", []) or []
            src_exclude = src.get("exclude", []) or []

            logger.info("Downloading http url=%s -> %s/%s", url, dest_rel, filename)

            rel_to_docs = f"{dest_rel}/{filename}" if dest_rel else filename

            def _norm_list(v: Any) -> list[str]:
                if v is None:
                    return []
                if isinstance(v, str):
                    return [v]
                return [str(x) for x in v]

            g_inc = _norm_list(global_include)
            g_exc = _norm_list(global_exclude)
            s_inc = _norm_list(src_include)
            s_exc = _norm_list(src_exclude)

            def _match_any(path: str, patterns: list[str]) -> bool:
                return any(fnmatch(path, p) for p in patterns)

            # Apply include/exclude logic for single file
            allowed = True
            if g_inc:
                allowed = _match_any(rel_to_docs, g_inc)
            if allowed and s_inc:
                allowed = _match_any(filename, s_inc)
            if allowed and g_exc and _match_any(rel_to_docs, g_exc):
                allowed = False
            if allowed and s_exc and _match_any(filename, s_exc):
                allowed = False

            if not allowed:
                logger.info("Skipping HTTP download due to filters: %s -> %s/%s", url, dest_rel, filename)
            else:
                r = session.get(url, headers=headers, timeout=30)
                r.raise_for_status()

                (dest_path / filename).write_bytes(r.content)

                logger.info("Saved %s (%d bytes)", dest_path / filename, len(r.content))

        else:
            raise ValueError(f"Unknown source type: {kind}")

    # cleanup any cloned repos inside staging
    rm_rf(staging_root / "_repos")
    logger.debug("Cleaned up staging repos")


def replace_docs_root_from(staging_root: Path) -> None:
    DOCS_ROOT.mkdir(parents=True, exist_ok=True)

    # Remove everything in DOCS_ROOT (including old .ready), then move staging contents in.
    logger.info("Replacing DOCS_ROOT at %s from staging %s", DOCS_ROOT, staging_root)

    for child in list(DOCS_ROOT.iterdir()):
        # If staging_root is inside DOCS_ROOT, do not delete it before moving
        if child == staging_root:
            logger.debug("Skipping deletion of staging directory: %s", child)
            continue

        rm_rf(child)

    for child in staging_root.iterdir():
        shutil.move(str(child), str(DOCS_ROOT / child.name))

    READY_MARKER.write_text(str(int(time.time())), encoding="utf-8")
    logger.info("Wrote ready marker at %s", READY_MARKER)


def perform_download() -> None:
    # Create staging directory inside DOCS_ROOT to avoid permission issues when
    # the parent of DOCS_ROOT is not writable within the container.
    staging = DOCS_ROOT / ".__staging__"
    rm_rf(staging)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        logger.info("Starting download into staging %s", staging)
        download_sources_into(staging)
        replace_docs_root_from(staging)
        logger.info("Download and replace completed successfully")
    finally:
        rm_rf(staging)
        logger.debug("Removed staging %s", staging)


def _download_and_update_state(initial: bool = False) -> None:
    try:
        perform_download()
        state["initial_done"] = True
        state["last_error"] = None
        logger.info("Download completed (initial=%s)", initial)

    except Exception as e:
        state["last_error"] = str(e)
        logger.exception("Download failed (initial=%s): %s", initial, e)

        if initial:
            state["initial_done"] = False
    finally:
        state["refreshing"] = False
        logger.debug("State updated: %s", state)


@app.route("/refresh", methods=["POST"])
def refresh():
    if not _refresh_lock.acquire(blocking=False):
        logger.info("Refresh requested but already in progress")
        return jsonify({"status": "already refreshing"}), 202

    state["refreshing"] = True
    logger.info("Refresh requested: starting background refresh")

    def runner():
        try:
            _download_and_update_state(initial=False)
        finally:
            _refresh_lock.release()

    threading.Thread(target=runner, daemon=True).start()
    return jsonify({"status": "started"}), 202


@app.route("/health", methods=["GET"])
def health():
    ok = bool(state["initial_done"] and READY_MARKER.exists())
    logger.debug("Health check: healthy=%s", ok)
    return (
        jsonify(
            {
                "healthy": ok,
                "initial_done": state["initial_done"],
                "refreshing": state["refreshing"],
                "error": state["last_error"],
            }
        ),
        200 if ok else 503,
    )


def main() -> None:
    logger.info("Downloader starting on port %s; docs root=%s; config=%s", PORT, DOCS_ROOT, CONFIG_PATH)

    with _refresh_lock:
        state["refreshing"] = True
        _download_and_update_state(initial=True)

    app.run(host="0.0.0.0", port=PORT)


def build_app():
    """
    WSGI app factory for production servers (e.g., Gunicorn).

    Ensures the initial download completes BEFORE the server starts
    accepting requests, then returns the Flask app instance.
    """
    logger.info(
        "Downloader (WSGI) starting; docs root=%s; config=%s",
        DOCS_ROOT,
        CONFIG_PATH,
    )

    with _refresh_lock:
        state["refreshing"] = True
        _download_and_update_state(initial=True)

    return app


if __name__ == "__main__":
    main()
