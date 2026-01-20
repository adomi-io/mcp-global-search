#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import requests
import yaml
from flask import Flask, jsonify
from shared.config import load_config as load_shared_config

# -----------------------------------------------------------------------------
# Config / constants
# -----------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", "8080"))
DOCS_ROOT = Path(os.environ.get("DOCS_ROOT", "/volumes/output"))
STATE_ROOT = Path(os.environ.get("STATE_ROOT", "/volumes/state"))
CONFIG_PATH = Path(os.environ.get("CONFIG_FILE", "/config/download.yml"))
READY_MARKER = DOCS_ROOT / ".ready"
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))
HTTP_TIMEOUT_SECS = int(os.environ.get("HTTP_TIMEOUT_SECS", "30"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
GIT_TOKEN = os.environ.get("GIT_TOKEN", "").strip()

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
    "last_stats": None,
}

_refresh_lock = threading.Lock()


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
def sh(cmd: list[str], cwd: Path | None = None) -> str:
    logger.debug("Running: %s (cwd=%s)", " ".join(cmd), cwd)

    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if proc.returncode != 0:
        out = proc.stdout or ""

        # redact both tokens
        for env_name in ("GIT_TOKEN", "GH_TOKEN"):
            token = (os.environ.get(env_name) or "").strip()
            if token:
                out = out.replace(token, "[REDACTED]")
                # also redact any common "token in URL" form if it made it in
        safe_cmd = " ".join(cmd)
        for env_name in ("GIT_TOKEN", "GH_TOKEN"):
            token = (os.environ.get(env_name) or "").strip()
            if token:
                safe_cmd = safe_cmd.replace(token, "[REDACTED]")

        raise RuntimeError(f"Command failed ({proc.returncode}): {safe_cmd}\n{out}")

    return proc.stdout




def rm_rf(path: Path) -> None:
    if not path.exists():
        return

    if path.is_file() or path.is_symlink():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)


def ensure_empty_dir(path: Path) -> None:
    rm_rf(path)

    path.mkdir(parents=True, exist_ok=True)


def resolve_headers(headers: dict[str, Any]) -> dict[str, str]:
    resolved: dict[str, str] = {}

    for k, v in (headers or {}).items():
        if isinstance(v, str) and v.startswith("$"):
            resolved[k] = os.environ.get(v[1:], "")
        else:
            resolved[k] = str(v)

    return resolved


def load_config() -> dict[str, Any]:
    """
    Load the shared configuration and return a flattened dict with keys:
    sources, loaders, destinations, collections.
    """
    return load_shared_config(CONFIG_PATH)


def _norm_list(v: Any) -> list[str]:
    if v is None:
        return []

    if isinstance(v, str):
        return [v]

    return [str(x) for x in v]


def _match_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch(path, p) for p in patterns)


def safe_destination(destination: str) -> str:
    destination = (destination or "").strip().replace("\\", "/").strip("/")

    if not destination:
        raise ValueError("destination is required and cannot be empty")

    if destination.startswith("..") or "/.." in destination or destination.startswith("/"):
        raise ValueError(f"Invalid destination (path traversal): {destination!r}")

    return destination


def escape_destination_for_fs(destination: str) -> str:
    return safe_destination(destination).replace("/", "__")


# -----------------------------------------------------------------------------
# Filtering semantics
# -----------------------------------------------------------------------------

def file_allowed(
    *,
    rel_from_source: str,
    destination_rel: str,
    include_global: list[str],
    exclude_global: list[str],
    include_source: list[str],
    exclude_source: list[str],
) -> bool:
    """
    rel_from_source: relative path inside the source root (e.g. "foo/bar.md")
    destination_rel: destination folder (e.g. "nuxt")
    Global patterns match paths relative to DOCS_ROOT (e.g. "nuxt/pnpm-lock.yaml").
    Source patterns match paths relative to the destination root (e.g. "**/*.md").
    """
    rel_to_docs = f"{destination_rel}/{rel_from_source}" if destination_rel else rel_from_source

    allowed = True

    if include_global:
        allowed = _match_any(
            rel_to_docs,
            include_global
        )

    if allowed and include_source:
        allowed = _match_any(
            rel_from_source,
            include_source
        )

    if allowed and exclude_global and _match_any(rel_to_docs, exclude_global):
        return False

    if allowed and exclude_source and _match_any(rel_from_source, exclude_source):
        return False

    return allowed


def single_file_allowed(
    *,
    filename: str,
    destination_rel: str,
    include_global: list[str],
    exclude_global: list[str],
    include_source: list[str],
    exclude_source: list[str],
) -> bool:
    rel_to_docs = f"{destination_rel}/{filename}" if destination_rel else filename

    allowed = True

    if include_global:
        allowed = _match_any(rel_to_docs, include_global)

    if allowed and include_source:
        allowed = _match_any(filename, include_source)

    if allowed and exclude_global and _match_any(rel_to_docs, exclude_global):
        allowed = False

    if allowed and exclude_source and _match_any(filename, exclude_source):
        allowed = False

    return allowed


# -----------------------------------------------------------------------------
# Staging build (per-destination)
# -----------------------------------------------------------------------------
def copy_tree_contents(
    src_dir: Path,
    dst_dir: Path,
    *,
    destination_rel: str,
    include_global: list[str],
    exclude_global: list[str],
    include_source: list[str],
    exclude_source: list[str],
) -> None:
    dst_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    for root, _dirs, files in os.walk(src_dir):
        root_path = Path(root)

        for fname in files:
            src_file = root_path / fname
            rel = src_file.relative_to(src_dir).as_posix()

            if not file_allowed(
                rel_from_source=rel,
                destination_rel=destination_rel,
                include_global=include_global,
                exclude_global=exclude_global,
                include_source=include_source,
                exclude_source=exclude_source,
            ):
                logger.debug("Skipping (filtered): %s/%s", destination_rel, rel)
                continue

            target = dst_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)

            shutil.copy2(src_file, target)


def git_cmd(repo: str, *args: str, cwd: Path | None = None) -> str:
    token = (os.environ.get("GIT_TOKEN") or "").strip()
    base: list[str] = ["git"]

    if token and repo.startswith("http"):
        username = (os.environ.get("GIT_USERNAME") or "x-access-token").strip()
        basic = base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")
        base += ["-c", f"http.extraHeader=Authorization: Basic {basic}"]

    return sh([*base, *args], cwd=cwd)

def gh_cmd(*args: str, cwd: Path | None = None) -> str:
    """
    Uses GH_TOKEN if available (falls back to GIT_TOKEN).
    Uses a temporary process environment so `sh()` doesn't need an env= param.
    """
    token = (os.environ.get("GH_TOKEN")).strip()

    if not token:
        raise RuntimeError("gh requires GH_TOKEN (or GIT_TOKEN) to be set")

    if not shutil.which("gh"):
        raise RuntimeError("gh CLI not found in PATH")

    # gh will automatically pick up GH_TOKEN
    with _temp_environ(GH_TOKEN=token):
        return sh(["gh", *args], cwd=cwd)


def _is_github_repo(repo: str) -> bool:
    r = repo.strip()

    # SSH forms
    if r.startswith("git@github.com:") or r.startswith("ssh://git@github.com/"):
        return True

    # HTTPS form
    if r.startswith("http://") or r.startswith("https://"):
        try:
            return (urlparse(r).hostname or "").lower() == "github.com"
        except Exception:
            return False

    # "OWNER/REPO" shorthand (best-effort)
    if r.count("/") == 1 and "://" not in r and "@" not in r and ":" not in r:
        return True

    return False

def download_git_source_into_destination(
    *,
    session: "requests.Session",
    source: dict[str, Any],
    destination: str,
    destination_staging: Path,
    repos_root: Path,
    include_global: list[str],
    exclude_global: list[str],
) -> None:
    repo = str(source["repo"])
    ref = source.get("ref")
    subpath = source.get("subpath", "") or ""
    include_source = _norm_list(source.get("include", []) or [])
    exclude_source = _norm_list(source.get("exclude", []) or [])

    key = hashlib.sha256(f"{repo}|{ref}|{subpath}".encode("utf-8")).hexdigest()[:16]
    repo_dir = repos_root / f"repo_{key}"

    rm_rf(repo_dir)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    use_gh = _is_github_repo(repo)

    logger.info(
        "git: repo=%s ref=%s subpath=%s -> destination=%s (use_gh=%s)",
        repo, ref, subpath, destination, use_gh
    )

    if use_gh:
        # Supports: owner/repo, https://github.com/owner/repo(.git), git@github.com:owner/repo.git
        # Pass --depth=1 through to the underlying git clone:
        gh_cmd("repo", "clone", repo, str(repo_dir), "--", "--depth=1")
    else:
        git_cmd(repo, "clone", "--depth=1", repo, str(repo_dir))

    if ref:
        git_cmd(repo, "fetch", "--depth=1", "origin", str(ref), cwd=repo_dir)
        git_cmd(repo, "checkout", "FETCH_HEAD", cwd=repo_dir)

    src_dir = (repo_dir / subpath) if subpath else repo_dir

    if not src_dir.exists():
        raise RuntimeError(f"subpath does not exist: repo={repo} subpath={subpath}")

    copy_tree_contents(
        src_dir,
        destination_staging,
        destination_rel=destination,
        include_global=include_global,
        exclude_global=exclude_global,
        include_source=include_source,
        exclude_source=exclude_source,
    )

def download_http_source_into_destination(
    *,
    session: requests.Session,
    source: dict[str, Any],
    destination: str,
    destination_staging: Path,
    include_global: list[str],
    exclude_global: list[str],
) -> None:
    url = source["url"]
    filename = source.get("filename") or (Path(url).name or "file.txt")
    headers = resolve_headers(source.get("headers", {}) or {})
    include_source = _norm_list(source.get("include", []) or [])
    exclude_source = _norm_list(source.get("exclude", []) or [])

    if not single_file_allowed(
        filename=filename,
        destination_rel=destination,
        include_global=include_global,
        exclude_global=exclude_global,
        include_source=include_source,
        exclude_source=exclude_source,
    ):
        logger.info("http: skipping due to filters: %s -> %s/%s", url, destination, filename)
        return

    logger.info("http: url=%s -> destination=%s/%s", url, destination, filename)

    r = session.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECS)
    r.raise_for_status()

    destination_staging.mkdir(parents=True, exist_ok=True)
    (destination_staging / filename).write_bytes(r.content)


def build_destination_staging(
    *,
    destination: str,
    sources: list[dict[str, Any]],
    staging_root: Path,
    include_global: list[str],
    exclude_global: list[str],
) -> None:
    """
    Populates staging_root with the desired final content for this destination ONLY.
    """
    ensure_empty_dir(staging_root)

    repos_root = staging_root / ".__repos__"
    repos_root.mkdir(parents=True, exist_ok=True)

    session = requests.Session()

    for source in sources:
        kind = source.get("type")

        if kind == "git":
            download_git_source_into_destination(
                session=session,
                source=source,
                destination=destination,
                destination_staging=staging_root,
                repos_root=repos_root,
                include_global=include_global,
                exclude_global=exclude_global,
            )
        elif kind == "http":
            download_http_source_into_destination(
                session=session,
                source=source,
                destination=destination,
                destination_staging=staging_root,
                include_global=include_global,
                exclude_global=exclude_global,
            )
        else:
            raise ValueError(f"Unknown source type: {kind}")

    rm_rf(repos_root)


# -----------------------------------------------------------------------------
# Apply (no git; rsync dry-run gate)
# -----------------------------------------------------------------------------

def rsync_plan_and_apply(src_dir: Path, dst_dir: Path, *, delete: bool = True) -> list[dict[str, str]]:
    """
    Applies staging -> destination subtree, but only runs if a dry-run indicates changes.
    Deletions are scoped to dst_dir only.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "rsync",
        "-r",
        *( ["--delete"] if delete else [] ),
        "--checksum",
        "--no-times",
        "--no-perms",
        "--no-owner",
        "--no-group",
        "--omit-dir-times",
        "--exclude=.git",
        "--itemize-changes",
        f"{src_dir}/",
        f"{dst_dir}/",
    ]

    out = sh([*cmd, "--dry-run"])

    changes: list[dict[str, str]] = []

    for line in out.splitlines():
        line = line.strip()

        if not line:
            continue

        parts = line.split(maxsplit=1)

        if len(parts) == 2:
            changes.append({
                "item": parts[0],
                "path": parts[1]
            })
        else:
            changes.append({"item": parts[0], "path": ""})

    if not changes:
        return []

    sh(cmd)

    return changes


def summarize_rsync_changes(changes: list[dict[str, str]]) -> dict[str, Any]:
    counts: dict[str, int] = {
        "A": 0,
        "M": 0,
        "D": 0
    }

    for ch in changes:
        item = ch["item"]
        path = ch["path"]

        if path.startswith("deleting "):
            counts["D"] += 1
            continue

        if item.startswith(">f") or item.startswith(">d") or item.startswith("cd") or item.startswith("c"):
            counts["M"] += 1
            continue

        counts["M"] += 1

    return {
        "counts": counts,
        "changes_sample": changes[:200],
    }


def refresh_one_destination(
    *,
    destination: str,
    sources: list[dict[str, Any]],
    include_global: list[str],
    exclude_global: list[str],
) -> dict[str, Any]:
    destination = safe_destination(destination)

    work_tree = DOCS_ROOT / destination
    work_tree.mkdir(parents=True, exist_ok=True)

    staging_root = STATE_ROOT / "staging" / escape_destination_for_fs(destination)

    build_destination_staging(
        destination=destination,
        sources=sources,
        staging_root=staging_root,
        include_global=include_global,
        exclude_global=exclude_global,
    )

    # Default strategy is "merge" (delete extras). Allow overrides via destinations meta.
    delete = True

    try:
        cfg = load_config()
        destinations_meta = (cfg.get("destinations") or {}) if isinstance(cfg.get("destinations"), dict) else {}
        meta = destinations_meta.get(destination, {}) if isinstance(destinations_meta, dict) else {}
        strategy = str(meta.get("strategy", "merge")).strip().lower()
        # Support both "merge" and historical "merge-source"
        if strategy in ("append",):
            delete = False
        elif strategy in ("none",):
            # Skip applying any file updates; just report current state
            rm_rf(staging_root)
            return {
                "destination": destination,
                "counts": {"A": 0, "M": 0, "D": 0},
                "changes_sample": [],
                "work_tree": str(work_tree),
                "note": "strategy=none (no sync)",
            }
        else:
            # merge / merge-source => delete extras
            delete = True
    except Exception:
        # If anything goes wrong reading meta, default to merge behavior
        delete = True

    changes = rsync_plan_and_apply(staging_root, work_tree, delete=delete)

    rm_rf(staging_root)

    summary = summarize_rsync_changes(changes)

    return {
        "destination": destination,
        "counts": summary["counts"],
        "changes_sample": summary["changes_sample"],
        "work_tree": str(work_tree),
    }


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------

def group_sources_by_destination(cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}

    for source in (cfg.get("sources", []) or []):
        destination = source.get("destination")

        if not destination:
            logger.warning("Skipping source with no destination: %s", source)
            continue

        destination = safe_destination(str(destination))
        grouped.setdefault(destination, []).append(source)

    return grouped


def ensure_state_dirs() -> None:
    DOCS_ROOT.mkdir(parents=True, exist_ok=True)

    STATE_ROOT.mkdir(parents=True, exist_ok=True)


def perform_refresh() -> dict[str, Any]:
    ensure_state_dirs()
    cfg = load_config()

    loaders = cfg.get("loaders", []) or []

    if loaders:
        logger.info(
            "Config includes loaders (%d). Loader processing is not implemented in this script.",
            len(loaders),
        )

    # Global include/exclude are no longer supported in the shared config.
    # Use per-source include/exclude instead.
    include_global: list[str] = []
    exclude_global: list[str] = []
    by_destination = group_sources_by_destination(cfg)

    if not by_destination:
        READY_MARKER.write_text(str(int(time.time())), encoding="utf-8")

        return {
            "destinations": {},
            "errors": {},
            "note": "no sources with destination",
        }

    results: dict[str, Any] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {}

        for destination, sources in by_destination.items():
            futs[
                ex.submit(
                    refresh_one_destination,
                    destination=destination,
                    sources=sources,
                    include_global=include_global,
                    exclude_global=exclude_global,
                )
            ] = destination

        for fut in as_completed(futs):
            destination = futs[fut]

            try:
                results[destination] = fut.result()
            except Exception as e:
                logger.exception("destination refresh failed: %s", destination)
                errors[destination] = str(e)

    if results:
        READY_MARKER.write_text(str(int(time.time())), encoding="utf-8")

    return {
        "destinations": results,
        "errors": errors,
    }


def _refresh_and_update_state(initial: bool = False) -> None:
    try:
        stats = perform_refresh()

        state["initial_done"] = True
        state["last_stats"] = stats
        state["last_error"] = None if not stats.get("errors") else "one or more destinations failed"

        logger.info("Refresh complete (initial=%s)", initial)
    except Exception as e:
        state["last_error"] = str(e)
        state["last_stats"] = None

        logger.exception("Refresh failed (initial=%s): %s", initial, e)

        if initial:
            state["initial_done"] = False
    finally:
        state["refreshing"] = False


# -----------------------------------------------------------------------------
# HTTP API
# -----------------------------------------------------------------------------

@app.route("/refresh", methods=["POST"])
def refresh():
    if not _refresh_lock.acquire(blocking=False):
        return jsonify({"status": "already refreshing"}), 202

    state["refreshing"] = True

    def runner():
        try:
            _refresh_and_update_state(initial=False)
        finally:
            _refresh_lock.release()

    threading.Thread(target=runner, daemon=True).start()

    return jsonify({"status": "started"}), 202


@app.route("/health", methods=["GET"])
def health():
    ok = bool(state["initial_done"] and READY_MARKER.exists())

    return (
        jsonify(
            {
                "healthy": ok,
                "initial_done": state["initial_done"],
                "refreshing": state["refreshing"],
                "error": state["last_error"],
                "last_stats": state.get("last_stats"),
            }
        ),
        200 if ok else 503,
    )


def main() -> None:
    logger.info(
        "Downloader starting on port=%s docs_root=%s state_root=%s config=%s",
        PORT,
        DOCS_ROOT,
        STATE_ROOT,
        CONFIG_PATH,
    )

    with _refresh_lock:
        state["refreshing"] = True
        _refresh_and_update_state(initial=True)

    app.run(host="0.0.0.0", port=PORT)


def build_app():
    """
    WSGI app factory (e.g., Gunicorn).
    Ensures initial refresh completes BEFORE serving requests.
    """
    logger.info(
        "Downloader (WSGI) starting; docs_root=%s state_root=%s config=%s",
        DOCS_ROOT,
        STATE_ROOT,
        CONFIG_PATH,
    )

    with _refresh_lock:
        state["refreshing"] = True
        _refresh_and_update_state(initial=True)

    return app


if __name__ == "__main__":
    main()
