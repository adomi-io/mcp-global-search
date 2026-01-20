#!/usr/bin/env python3
from __future__ import annotations

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
from typing import Any, Callable

import requests
import yaml
from flask import Flask, jsonify

# -----------------------------------------------------------------------------
# Config / constants
# -----------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", "8080"))
DOCS_ROOT = Path(os.environ.get("DOCS_ROOT", "/volumes/output"))
STATE_ROOT = Path(os.environ.get("STATE_ROOT", "/volumes/state"))
CONFIG_PATH = Path(os.environ.get("CONFIG_FILE", "/config/download.yml"))

READY_MARKER = DOCS_ROOT / ".ready"

# dest refresh parallelism (safe now that repos/staging are per-dest)
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))
HTTP_TIMEOUT_SECS = int(os.environ.get("HTTP_TIMEOUT_SECS", "30"))

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
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout}")
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
    if not CONFIG_PATH.exists():
        return {"sources": [], "include": [], "exclude": [], "loaders": []}
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"sources": [], "include": [], "exclude": [], "loaders": []}


def _norm_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def _match_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch(path, p) for p in patterns)


def safe_dest(dest: str) -> str:
    dest = (dest or "").strip().replace("\\", "/").strip("/")
    if not dest:
        raise ValueError("dest is required and cannot be empty")
    if dest.startswith("..") or "/.." in dest or dest.startswith("/"):
        raise ValueError(f"Invalid dest (path traversal): {dest!r}")
    return dest


def escape_dest_for_fs(dest: str) -> str:
    return safe_dest(dest).replace("/", "__")


# -----------------------------------------------------------------------------
# Filtering semantics
# -----------------------------------------------------------------------------

def file_allowed(
    *,
    rel_from_source: str,
    dest_rel: str,
    include_global: list[str],
    exclude_global: list[str],
    include_source: list[str],
    exclude_source: list[str],
) -> bool:
    """
    rel_from_source: relative path inside the source root (e.g. "foo/bar.md")
    dest_rel: dest folder (e.g. "nuxt")
    Global patterns match paths relative to DOCS_ROOT (e.g. "nuxt/pnpm-lock.yaml").
    Source patterns match paths relative to the dest root (e.g. "**/*.md").
    """
    rel_to_docs = f"{dest_rel}/{rel_from_source}" if dest_rel else rel_from_source

    allowed = True

    # includes restrict
    if include_global:
        allowed = _match_any(rel_to_docs, include_global)
    if allowed and include_source:
        allowed = _match_any(rel_from_source, include_source)

    # excludes override
    if allowed and exclude_global and _match_any(rel_to_docs, exclude_global):
        return False
    if allowed and exclude_source and _match_any(rel_from_source, exclude_source):
        return False

    return allowed


def single_file_allowed(
    *,
    filename: str,
    dest_rel: str,
    include_global: list[str],
    exclude_global: list[str],
    include_source: list[str],
    exclude_source: list[str],
) -> bool:
    rel_to_docs = f"{dest_rel}/{filename}" if dest_rel else filename

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
# Staging build (per-dest)
# -----------------------------------------------------------------------------

def copy_tree_contents(
    src_dir: Path,
    dst_dir: Path,
    *,
    dest_rel: str,
    include_global: list[str],
    exclude_global: list[str],
    include_source: list[str],
    exclude_source: list[str],
) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)

    for root, _dirs, files in os.walk(src_dir):
        root_path = Path(root)
        for fname in files:
            src_file = root_path / fname
            rel = src_file.relative_to(src_dir).as_posix()

            if not file_allowed(
                rel_from_source=rel,
                dest_rel=dest_rel,
                include_global=include_global,
                exclude_global=exclude_global,
                include_source=include_source,
                exclude_source=exclude_source,
            ):
                logger.debug("Skipping (filtered): %s/%s", dest_rel, rel)
                continue

            target = dst_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, target)


def download_git_source_into_dest(
    *,
    session: requests.Session,
    src: dict[str, Any],
    dest: str,
    dest_staging: Path,
    repos_root: Path,
    include_global: list[str],
    exclude_global: list[str],
) -> None:
    repo = src["repo"]
    ref = src.get("ref")
    subpath = src.get("subpath", "") or ""
    include_source = _norm_list(src.get("include", []) or [])
    exclude_source = _norm_list(src.get("exclude", []) or [])

    key = hashlib.sha256(f"{repo}|{ref}|{subpath}".encode("utf-8")).hexdigest()[:16]
    repo_dir = repos_root / f"repo_{key}"

    rm_rf(repo_dir)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    logger.info("git: repo=%s ref=%s subpath=%s -> dest=%s", repo, ref, subpath, dest)
    sh(["git", "clone", "--depth=1", repo, str(repo_dir)])

    if ref:
        sh(["git", "fetch", "--depth=1", "origin", str(ref)], cwd=repo_dir)
        sh(["git", "checkout", "FETCH_HEAD"], cwd=repo_dir)

    src_dir = repo_dir / subpath if subpath else repo_dir
    if not src_dir.exists():
        raise RuntimeError(f"subpath does not exist: repo={repo} subpath={subpath}")

    copy_tree_contents(
        src_dir,
        dest_staging,
        dest_rel=dest,
        include_global=include_global,
        exclude_global=exclude_global,
        include_source=include_source,
        exclude_source=exclude_source,
    )


def download_http_source_into_dest(
    *,
    session: requests.Session,
    src: dict[str, Any],
    dest: str,
    dest_staging: Path,
    include_global: list[str],
    exclude_global: list[str],
) -> None:
    url = src["url"]
    filename = src.get("filename") or (Path(url).name or "file.txt")
    headers = resolve_headers(src.get("headers", {}) or {})
    include_source = _norm_list(src.get("include", []) or [])
    exclude_source = _norm_list(src.get("exclude", []) or [])

    if not single_file_allowed(
        filename=filename,
        dest_rel=dest,
        include_global=include_global,
        exclude_global=exclude_global,
        include_source=include_source,
        exclude_source=exclude_source,
    ):
        logger.info("http: skipping due to filters: %s -> %s/%s", url, dest, filename)
        return

    logger.info("http: url=%s -> dest=%s/%s", url, dest, filename)
    r = session.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECS)
    r.raise_for_status()

    dest_staging.mkdir(parents=True, exist_ok=True)
    (dest_staging / filename).write_bytes(r.content)


def build_dest_staging(
    *,
    dest: str,
    sources: list[dict[str, Any]],
    staging_root: Path,
    include_global: list[str],
    exclude_global: list[str],
) -> None:
    """
    Populates staging_root with the desired final content for this dest ONLY.
    """
    ensure_empty_dir(staging_root)

    # IMPORTANT: per-dest repos directory avoids cross-dest races.
    repos_root = staging_root / ".__repos__"
    repos_root.mkdir(parents=True, exist_ok=True)

    session = requests.Session()

    for src in sources:
        kind = src.get("type")
        if kind == "git":
            download_git_source_into_dest(
                session=session,
                src=src,
                dest=dest,
                dest_staging=staging_root,
                repos_root=repos_root,
                include_global=include_global,
                exclude_global=exclude_global,
            )
        elif kind == "http":
            download_http_source_into_dest(
                session=session,
                src=src,
                dest=dest,
                dest_staging=staging_root,
                include_global=include_global,
                exclude_global=exclude_global,
            )
        else:
            raise ValueError(f"Unknown source type: {kind}")

    rm_rf(repos_root)


# -----------------------------------------------------------------------------
# Per-dest Git + apply
# -----------------------------------------------------------------------------

def git_for_dest(dest: str) -> tuple[Callable[[list[str]], str], Path, Path]:
    """
    Returns (git_runner, work_tree, git_dir)
    - work_tree = DOCS_ROOT/<dest> (final output monitored by watcher)
    - git_dir lives in STATE_ROOT (no .git inside DOCS_ROOT)
    """
    dest = safe_dest(dest)

    work_tree = DOCS_ROOT / dest
    work_tree.mkdir(parents=True, exist_ok=True)

    git_dir = STATE_ROOT / "dests" / escape_dest_for_fs(dest) / "repo.git"
    git_dir.parent.mkdir(parents=True, exist_ok=True)

    if not git_dir.exists():
        sh(["git", "init", "--bare", str(git_dir)])

    def git(cmd: list[str]) -> str:
        return sh(["git", f"--git-dir={git_dir}", f"--work-tree={work_tree}", *cmd])

    git(["config", "user.email", "downloader@local"])
    git(["config", "user.name", "downloader"])

    # ensure HEAD exists
    try:
        git(["rev-parse", "--verify", "HEAD"])
    except Exception:
        git(["add", "-A"])
        git(["commit", "--allow-empty", "-m", "initial snapshot"])

    return git, work_tree, git_dir


def commit_if_dirty(git: Callable[[list[str]], str], message: str) -> None:
    git(["add", "-A"])
    if git(["status", "--porcelain"]).strip():
        git(["commit", "-m", message])


def rsync_apply(src_dir: Path, dst_dir: Path) -> None:
    """
    Applies staging -> dest subtree. Deletions are scoped to dst_dir only.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    sh([
        "rsync",
        "-a",
        "--delete",
        "--exclude=.git",
        f"{src_dir}/",
        f"{dst_dir}/",
    ])


def diff_name_status(git: Callable[[list[str]], str], ref: str = "HEAD") -> list[dict[str, str]]:
    out = git(["diff", "--name-status", ref])
    changes: list[dict[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        status, path = line.split("\t", 1)
        changes.append({"status": status, "path": path})
    return changes


def refresh_one_dest(
    *,
    dest: str,
    sources: list[dict[str, Any]],
    include_global: list[str],
    exclude_global: list[str],
) -> dict[str, Any]:
    git, work_tree, git_dir = git_for_dest(dest)

    # baseline snapshot of current dest subtree
    commit_if_dirty(git, f"baseline {int(time.time())}")

    staging_root = STATE_ROOT / "staging" / escape_dest_for_fs(dest)
    build_dest_staging(
        dest=dest,
        sources=sources,
        staging_root=staging_root,
        include_global=include_global,
        exclude_global=exclude_global,
    )

    rsync_apply(staging_root, work_tree)

    changes = diff_name_status(git, "HEAD")

    # commit refreshed state (audit/debug)
    commit_if_dirty(git, f"refreshed {int(time.time())}")

    rm_rf(staging_root)

    counts: dict[str, int] = {"A": 0, "M": 0, "D": 0, "R": 0, "C": 0}
    for ch in changes:
        k = ch["status"][0]
        counts[k] = counts.get(k, 0) + 1

    return {
        "dest": dest,
        "counts": counts,
        "changes_sample": changes[:200],
        "git_dir": str(git_dir),
        "work_tree": str(work_tree),
    }


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------

def group_sources_by_dest(cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for src in (cfg.get("sources", []) or []):
        dest = src.get("dest")
        if not dest:
            # Only manage sources with dest
            logger.warning("Skipping source with no dest: %s", src)
            continue
        dest = safe_dest(str(dest))
        grouped.setdefault(dest, []).append(src)
    return grouped


def ensure_state_dirs() -> None:
    DOCS_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_ROOT.mkdir(parents=True, exist_ok=True)


def perform_refresh() -> dict[str, Any]:
    ensure_state_dirs()
    cfg = load_config()

    loaders = cfg.get("loaders", []) or []
    if loaders:
        logger.info("Config includes loaders (%d). Loader processing is not implemented in this script.", len(loaders))

    include_global = _norm_list(cfg.get("include", []) or [])
    exclude_global = _norm_list(cfg.get("exclude", []) or [])
    by_dest = group_sources_by_dest(cfg)

    if not by_dest:
        READY_MARKER.write_text(str(int(time.time())), encoding="utf-8")
        return {"dests": {}, "errors": {}, "note": "no sources with dest"}

    results: dict[str, Any] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {}
        for dest, sources in by_dest.items():
            futs[ex.submit(
                refresh_one_dest,
                dest=dest,
                sources=sources,
                include_global=include_global,
                exclude_global=exclude_global,
            )] = dest

        for fut in as_completed(futs):
            dest = futs[fut]
            try:
                results[dest] = fut.result()
            except Exception as e:
                logger.exception("Dest refresh failed: %s", dest)
                errors[dest] = str(e)

    if results:
        READY_MARKER.write_text(str(int(time.time())), encoding="utf-8")

    return {"dests": results, "errors": errors}


def _refresh_and_update_state(initial: bool = False) -> None:
    try:
        stats = perform_refresh()
        state["initial_done"] = True
        state["last_stats"] = stats
        state["last_error"] = None if not stats.get("errors") else "one or more dests failed"
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
