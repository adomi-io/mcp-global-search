#!/usr/bin/env python3
"""
Meilisearch file watcher + (optional) embeddings config.

Fixes for meilisearch-python v0.40.x:
- Tasks come back as TaskInfo objects (not dicts). We handle both.

Improvements:
- Don't lie in logs: wait for tasks and log failures with Meili error payload.
- Prefer delete_documents_by_filter when available; fallback to search+delete.
- Avoid hashing unchanged files by comparing stored (bytes, mtime_ns) first.
- Cache ensure_index/ensure_settings per index per run (cuts settings spam).
- Coalesce watcher events into a debounced single-worker queue (reduces thrash).
- Sanitize index uids derived from folder names.
- Skip common temp/swap files.

Env:
  MEILISEARCH_HOST, MEILISEARCH_MASTER_KEY, DOCS_DIR, MEILISEARCH_BATCH_SIZE, MEILISEARCH_MAX_BYTES,
  MEILISEARCH_ALLOWED_EXTS, LOG_LEVEL

  EMBEDDINGS_ENABLED=false|true
  OPENAI_API_KEY (required if EMBEDDINGS_ENABLED=true)
  MEILI_EMBEDDER_NAME=openai
  OPENAI_EMBED_MODEL=text-embedding-3-small
  OPENAI_EMBED_DIMENSIONS=1536 (optional)
  MEILI_DOCUMENT_TEMPLATE, MEILI_TEMPLATE_MAX_BYTES

  CHUNK_SIZE, CHUNK_OVERLAP
  WATCH_DEBOUNCE_SECONDS
"""

from __future__ import annotations

import os
import time
import re
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

import meilisearch
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from langchain_community.document_loaders import TextLoader, CSVLoader
from langchain_core.documents import Document

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except Exception:  # pragma: no cover
    from langchain.text_splitter import RecursiveCharacterTextSplitter


# ----------------------------
# Config
# ----------------------------

MEILISEARCH_HOST = os.environ.get("MEILISEARCH_HOST", "http://meilisearch:7700").rstrip("/")
MEILISEARCH_MASTER_KEY = os.environ.get("MEILISEARCH_MASTER_KEY", "").strip()
DOCS_DIR = Path(os.environ.get("DOCS_DIR", "/volumes/input"))

BATCH_SIZE = int(os.environ.get("MEILISEARCH_BATCH_SIZE", "200"))
MAX_BYTES = int(os.environ.get("MEILISEARCH_MAX_BYTES", str(2 * 1024 * 1024)))

ALLOWED_EXTS = {
    e.strip().lower()
    for e in os.environ.get(
        "MEILISEARCH_ALLOWED_EXTS",
        ".md,.mdx,.txt,.json,.yml,.yaml,.toml,.js,.ts,.vue,.css,.html,.sh,.py,.csv",
    ).split(",")
    if e.strip()
}

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "1200"))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "150"))
DEBOUNCE_SECONDS = float(os.environ.get("WATCH_DEBOUNCE_SECONDS", "0.35"))


def _env_true(name: str, default: str = "") -> bool:
    v = os.environ.get(name, default)
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


# Optional embeddings config
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
EMBEDDINGS_ENABLED = _env_true("EMBEDDINGS_ENABLED", "false") and bool(OPENAI_API_KEY)

EMBEDDER_NAME = os.environ.get("MEILI_EMBEDDER_NAME", "openai").strip() or "openai"
OPENAI_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small").strip()
OPENAI_DIMENSIONS = os.environ.get("OPENAI_EMBED_DIMENSIONS", "").strip()
DOCUMENT_TEMPLATE = os.environ.get(
    "MEILI_DOCUMENT_TEMPLATE",
    "{{doc.filename}}\n{{doc.path}}\n\n{{doc.text}}",
).strip()
TEMPLATE_MAX_BYTES = int(os.environ.get("MEILI_TEMPLATE_MAX_BYTES", "20000"))


# ----------------------------
# Logging
# ----------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [meili_semantic_watcher] %(levelname)s: %(message)s",
)
logger = logging.getLogger("meili_semantic_watcher")


# ----------------------------
# Helpers
# ----------------------------

def require(name: str, value: str) -> str:
    if value:
        return value
    raise RuntimeError(f"{name} is required but not set")


def rel_posix(p: Path) -> str:
    return str(p).replace("\\", "/")


def sha1_str(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def is_hidden_rel(rel: Path) -> bool:
    return any(part.startswith(".") for part in rel.parts)


def sanitize_index_uid(uid: str) -> str:
    """
    Meili index UIDs should be reasonably url-safe.
    Lowercase, non [a-z0-9_-] -> '-', collapse dashes, trim.
    """
    uid = (uid or "").strip().lower()
    uid = re.sub(r"[^a-z0-9_-]+", "-", uid)
    uid = re.sub(r"-{2,}", "-", uid).strip("-")
    return uid


def top_level_index_for(rel_path_posix: str) -> str | None:
    parts = rel_path_posix.split("/") if rel_path_posix else []
    if len(parts) < 2:
        return None
    top = parts[0].strip()
    if not top:
        return None
    uid = sanitize_index_uid(top)
    return uid or None


def allowed_file(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        rel = path.relative_to(DOCS_DIR)
    except Exception:
        return False
    if is_hidden_rel(rel):
        return False

    # skip obvious junk/temp
    name = path.name
    if name.endswith("~") or name.endswith(".swp") or name.endswith(".tmp"):
        return False

    ext = path.suffix.lower()
    if ALLOWED_EXTS and ext and ext not in ALLOWED_EXTS:
        return False

    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size > MAX_BYTES:
        return False

    return True


def choose_loader(path: Path):
    ext = path.suffix.lower()
    if ext == ".csv":
        return CSVLoader(file_path=str(path), encoding="utf-8", csv_args={"delimiter": ","})
    return TextLoader(file_path=str(path), encoding="utf-8", autodetect_encoding=True)


def file_hash_for(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class ChunkDoc:
    index_uid: str
    source_path: str
    doc: dict[str, Any]


# ----------------------------
# Indexer
# ----------------------------

class Indexer:
    def __init__(self):
        api_key = require("MEILISEARCH_MASTER_KEY", MEILISEARCH_MASTER_KEY)
        self.client = meilisearch.Client(MEILISEARCH_HOST, api_key)
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
            separators=["\n\n", "\n", " ", ""],
        )

        # Cache ensure_* per index per run
        self._ensured_lock = Lock()
        self._ensured: set[str] = set()

    # ---- task helpers (v0.40 TaskInfo compatibility) ----

    def _task_uid(self, task: Any) -> int | None:
        # dict-style
        if isinstance(task, dict):
            uid = task.get("taskUid") or task.get("uid")
            return int(uid) if uid is not None else None

        # TaskInfo / similar object (v0.40)
        for attr in ("task_uid", "taskUid", "uid"):
            if hasattr(task, attr):
                uid = getattr(task, attr)
                return int(uid) if uid is not None else None

        # last resort: try item access
        try:
            uid = task["taskUid"]
            return int(uid)
        except Exception:
            return None

    def _get_task(self, uid: int) -> dict[str, Any] | None:
        try:
            t = self.client.get_task(uid)
            if isinstance(t, dict):
                return t
            return {
                "uid": getattr(t, "uid", getattr(t, "task_uid", None)),
                "status": getattr(t, "status", None),
                "type": getattr(t, "type", None),
                "error": getattr(t, "error", None),
                "details": getattr(t, "details", None),
            }
        except Exception:
            return None

    def _wait_task_ok(self, task: Any, timeout_ms: int = 60_000, context: str = "") -> bool:
        uid = self._task_uid(task)
        if uid is None:
            return True
        try:
            self.client.wait_for_task(uid, timeout_in_ms=timeout_ms, interval_in_ms=250)
        except Exception as e:
            logger.warning("Timed out waiting for task %s (%s): %s", uid, context, e)
            return False

        t = self._get_task(uid) or {}
        status = (t.get("status") or "").lower()

        if status == "failed":
            err = t.get("error") or {}
            logger.error("Task %s FAILED (%s): %s", uid, context, err if isinstance(err, dict) else str(err))
            return False

        if status and status != "succeeded":
            logger.warning("Task %s ended with status=%r (%s)", uid, status, context)

        return status != "failed"

    # ---- index + settings ----

    def ensure_index(self, index_uid: str) -> bool:
        try:
            self.client.get_index(index_uid)
            return True
        except Exception:
            pass

        try:
            task = self.client.create_index(index_uid, {"primaryKey": "id"})
            ok = self._wait_task_ok(task, context=f"create_index index={index_uid}")
            if ok:
                logger.info("Created index '%s'", index_uid)
            return ok
        except Exception:
            # race-safe fallback
            try:
                self.client.get_index(index_uid)
                return True
            except Exception as e:
                raise e

    def ensure_settings(self, index_uid: str) -> None:
        """
        Always ensure filterableAttributes contains source_path.
        If EMBEDDINGS_ENABLED: also ensure embedders config.
        """
        index = self.client.index(index_uid)

        try:
            settings = index.get_settings() or {}
        except Exception:
            settings = {}

        # filterableAttributes
        cur_filterable = settings.get("filterableAttributes") or []
        if not isinstance(cur_filterable, list):
            cur_filterable = []

        wanted = {"source_path"}
        merged = list(dict.fromkeys([*cur_filterable, *sorted(wanted)]))

        if set(cur_filterable) != set(merged):
            try:
                task = index.update_settings({"filterableAttributes": merged})
                self._wait_task_ok(task, context=f"update_settings(filterableAttributes) index={index_uid}")
                logger.info("Updated filterableAttributes on '%s'", index_uid)
            except Exception as e:
                logger.warning("Failed updating filterableAttributes on '%s': %s", index_uid, e)

        if not EMBEDDINGS_ENABLED:
            return

        embedder_cfg: dict[str, Any] = {
            "source": "openAi",
            "apiKey": OPENAI_API_KEY,
            "model": OPENAI_MODEL,
            "documentTemplate": DOCUMENT_TEMPLATE,
            "documentTemplateMaxBytes": TEMPLATE_MAX_BYTES,
        }
        if OPENAI_DIMENSIONS:
            try:
                embedder_cfg["dimensions"] = int(OPENAI_DIMENSIONS)
            except ValueError:
                logger.warning("OPENAI_EMBED_DIMENSIONS must be int; got %r", OPENAI_DIMENSIONS)

        cur_embedders = settings.get("embedders") or {}
        if not isinstance(cur_embedders, dict):
            cur_embedders = {}

        current_for_name = cur_embedders.get(EMBEDDER_NAME) or {}
        need_update = any(
            str(current_for_name.get(k)) != str(embedder_cfg.get(k))
            for k in ("source", "model", "dimensions", "documentTemplate", "documentTemplateMaxBytes")
        )

        if need_update:
            desired = {**cur_embedders, EMBEDDER_NAME: embedder_cfg}
            try:
                task = index.update_settings({"embedders": desired})
                self._wait_task_ok(task, context=f"update_settings(embedders) index={index_uid}")
                logger.info("Configured embedder '%s' on '%s'", EMBEDDER_NAME, index_uid)
            except Exception as e:
                logger.warning("Failed updating embedders on '%s': %s", index_uid, e)

    def ensure_index_and_settings_once(self, index_uid: str) -> None:
        with self._ensured_lock:
            if index_uid in self._ensured:
                return
            self.ensure_index(index_uid)
            self.ensure_settings(index_uid)
            self._ensured.add(index_uid)

    # ---- loading + chunking ----

    def load_and_chunk(self, path: Path) -> list[Document]:
        loader = choose_loader(path)
        docs = loader.load()

        rel = rel_posix(path.relative_to(DOCS_DIR))
        for d in docs:
            d.metadata = {
                **(d.metadata or {}),
                "source_path": rel,
                "filename": path.name,
                "ext": path.suffix.lower().lstrip("."),
            }

        return self.splitter.split_documents(docs)

    def build_chunk_docs(self, path: Path, fh: str, mtime_ns: int, size: int) -> list[ChunkDoc]:
        rel = rel_posix(path.relative_to(DOCS_DIR))
        index_uid = top_level_index_for(rel)
        if not index_uid:
            return []

        chunks = self.load_and_chunk(path)

        file_id = sha1_str(rel)
        base = file_id

        out: list[ChunkDoc] = []
        for i, c in enumerate(chunks):
            chunk_id = f"{base}-{i}"
            doc = {
                "id": chunk_id,
                "file_id": file_id,
                "file_hash": fh,
                "source_path": rel,
                "path": rel,
                "filename": path.name,
                "ext": path.suffix.lower().lstrip("."),
                "mtime_ns": mtime_ns,
                "bytes": size,
                "chunk": i,
                "text": c.page_content or "",
            }
            out.append(ChunkDoc(index_uid=index_uid, source_path=rel, doc=doc))

        return out

    # ---- index ops ----

    def get_existing_file_state(self, index_uid: str, source_path: str) -> dict[str, Any] | None:
        """
        Returns {file_hash, mtime_ns, bytes} for source_path if present.
        """
        index = self.client.index(index_uid)
        safe_val = source_path.replace('"', '\\"')
        filt = f'source_path = "{safe_val}"'

        try:
            res = index.search(
                "",
                {"limit": 1, "filter": filt, "attributesToRetrieve": ["file_hash", "mtime_ns", "bytes"]},
            )
            hits = (res or {}).get("hits") or []
            if not hits:
                return None
            return hits[0] or None
        except Exception:
            return None

    def delete_by_source_path(self, index_uid: str, source_path: str) -> bool:
        """
        Prefer delete-by-filter if supported; fallback to search+delete.
        """
        index = self.client.index(index_uid)
        safe_val = source_path.replace('"', '\\"')
        filt = f'source_path = "{safe_val}"'

        if hasattr(index, "delete_documents_by_filter"):
            try:
                task = index.delete_documents_by_filter(filt)
                return self._wait_task_ok(task, context=f"delete_documents_by_filter index={index_uid} path={source_path}")
            except Exception as e:
                logger.warning("delete_documents_by_filter failed; falling back: %s", e)

        total_deleted = 0
        limit = 1000

        while True:
            try:
                res = index.search(
                    "",
                    {"limit": limit, "offset": 0, "filter": filt, "attributesToRetrieve": ["id"]},
                )
            except Exception as e:
                logger.warning("Delete scan failed (index '%s', path '%s'): %s", index_uid, source_path, e)
                return False

            hits = (res or {}).get("hits") or []
            if not hits:
                break

            ids = [h.get("id") for h in hits if h.get("id")]
            if not ids:
                break

            for start in range(0, len(ids), BATCH_SIZE):
                batch = ids[start : start + BATCH_SIZE]
                try:
                    task = index.delete_documents(batch)
                    ok = self._wait_task_ok(task, context=f"delete_documents index={index_uid} ({len(batch)} docs)")
                    if not ok:
                        return False
                    total_deleted += len(batch)
                except Exception as e:
                    logger.warning("Delete batch failed (index '%s'): %s", index_uid, e)
                    return False

        if total_deleted:
            logger.info("Deleted %d docs for %s from index '%s'", total_deleted, source_path, index_uid)
        return True

    def upsert_docs(self, index_uid: str, docs: list[dict[str, Any]]) -> bool:
        if not docs:
            return True
        index = self.client.index(index_uid)

        last_task = None
        for start in range(0, len(docs), BATCH_SIZE):
            batch = docs[start : start + BATCH_SIZE]
            try:
                last_task = index.add_documents(batch, primary_key="id")
            except Exception as e:
                logger.warning("Upsert failed for '%s' (batch %d..%d): %s", index_uid, start, start + len(batch), e)
                return False

        if last_task is None:
            return True
        return self._wait_task_ok(last_task, context=f"add_documents index={index_uid} (last batch)")

    def index_file(self, path: Path) -> None:
        if not allowed_file(path):
            return

        rel = rel_posix(path.relative_to(DOCS_DIR))
        index_uid = top_level_index_for(rel)
        if not index_uid:
            logger.debug("Skipping file not in a top-level folder: %s", rel)
            return

        self.ensure_index_and_settings_once(index_uid)

        try:
            st = path.stat()
            mtime_ns = st.st_mtime_ns
            size = st.st_size
        except FileNotFoundError:
            return

        # Fast skip: if stored bytes+mtime_ns match, avoid hashing
        existing = self.get_existing_file_state(index_uid, rel)
        if existing:
            try:
                if int(existing.get("bytes") or -1) == int(size) and int(existing.get("mtime_ns") or -1) == int(mtime_ns):
                    logger.debug("Unchanged by stat; skipping %s (index '%s')", rel, index_uid)
                    return
            except Exception:
                pass

        # Hash only when needed
        try:
            current_hash = file_hash_for(path)
        except FileNotFoundError:
            return

        if existing and existing.get("file_hash") == current_hash:
            logger.debug("Content unchanged (hash); skipping %s (index '%s')", rel, index_uid)
            return

        # Reindex: delete old chunks then add new
        ok_del = self.delete_by_source_path(index_uid, rel)

        chunk_docs = self.build_chunk_docs(path, fh=current_hash, mtime_ns=mtime_ns, size=size)
        ok_up = self.upsert_docs(index_uid, [cd.doc for cd in chunk_docs])

        if ok_del and ok_up:
            logger.info("Indexed %s (%d chunks) -> index '%s'", rel, len(chunk_docs), index_uid)
        else:
            logger.error("Indexing FAILED for %s -> index '%s' (see task error above)", rel, index_uid)

    def delete_path(self, rel_path_posix: str) -> None:
        index_uid = top_level_index_for(rel_path_posix)
        if not index_uid:
            return
        self.ensure_index_and_settings_once(index_uid)
        ok = self.delete_by_source_path(index_uid, rel_path_posix)
        if ok:
            logger.info("Deleted %s from its index", rel_path_posix)

    def full_sync(self) -> None:
        logger.info("Starting full sync from %s", DOCS_DIR)
        if not DOCS_DIR.exists():
            DOCS_DIR.mkdir(parents=True, exist_ok=True)
            return

        count_files = 0
        for p in DOCS_DIR.rglob("*"):
            if not allowed_file(p):
                continue
            rel = rel_posix(p.relative_to(DOCS_DIR))
            if not top_level_index_for(rel):
                continue
            self.index_file(p)
            count_files += 1

        logger.info("Full sync complete (%d files considered)", count_files)


# ----------------------------
# Debounced work queue (coalesce events)
# ----------------------------

class DebouncedQueue:
    """
    Collects requested operations keyed by rel path and runs them after DEBOUNCE_SECONDS of quiet.
    Single worker thread => no overlapping indexing work.
    """
    def __init__(self, indexer: Indexer):
        self.indexer = indexer
        self._lock = Lock()
        self._pending: dict[str, tuple[float, str]] = {}  # rel -> (due_ts, op)
        self._stop = Event()
        self._thread = Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def schedule_index(self, path: Path) -> None:
        try:
            rel = rel_posix(path.relative_to(DOCS_DIR))
        except Exception:
            return
        due = time.time() + DEBOUNCE_SECONDS
        with self._lock:
            # index replaces prior index (but delete should win if present)
            prev = self._pending.get(rel)
            if prev and prev[1] == "delete":
                return
            self._pending[rel] = (due, "index")

    def schedule_delete(self, rel: str) -> None:
        due = time.time() + DEBOUNCE_SECONDS
        with self._lock:
            self._pending[rel] = (due, "delete")

    def _pop_ready(self) -> list[tuple[str, str]]:
        now = time.time()
        out: list[tuple[str, str]] = []
        with self._lock:
            ready = [k for k, (due, _) in self._pending.items() if due <= now]
            for k in ready:
                _, op = self._pending.pop(k)
                out.append((k, op))
        return out

    def _run(self) -> None:
        while not self._stop.is_set():
            for rel, op in self._pop_ready():
                try:
                    if op == "delete":
                        self.indexer.delete_path(rel)
                    else:
                        self.indexer.index_file(DOCS_DIR / rel)
                except Exception as e:
                    logger.exception("Work item failed (%s %s): %s", op, rel, e)
            time.sleep(0.10)


# ----------------------------
# Watchdog handler
# ----------------------------

class WatchHandler(FileSystemEventHandler):
    def __init__(self, queue: DebouncedQueue):
        self.queue = queue

    def on_created(self, event):
        if event.is_directory:
            return
        self.queue.schedule_index(Path(event.src_path))

    def on_modified(self, event):
        if event.is_directory:
            return
        self.queue.schedule_index(Path(event.src_path))

    def on_deleted(self, event):
        if event.is_directory:
            return
        try:
            rel = rel_posix(Path(event.src_path).relative_to(DOCS_DIR))
        except Exception:
            return
        self.queue.schedule_delete(rel)

    def on_moved(self, event):
        if event.is_directory:
            return

        src = Path(event.src_path)
        dst = Path(event.dest_path)

        try:
            rel_src = rel_posix(src.relative_to(DOCS_DIR))
            self.queue.schedule_delete(rel_src)
        except Exception:
            pass

        self.queue.schedule_index(dst)


# ----------------------------
# Startup
# ----------------------------

def wait_meili_ready(client: meilisearch.Client, timeout_s: int = 600) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if hasattr(client, "is_healthy") and client.is_healthy():
                return
            if hasattr(client, "health"):
                h = client.health()
                if isinstance(h, dict) and h.get("status") == "available":
                    return
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError("Meilisearch not healthy in time")


def main():
    require("MEILISEARCH_MASTER_KEY", MEILISEARCH_MASTER_KEY)

    if _env_true("EMBEDDINGS_ENABLED", "false") and not OPENAI_API_KEY:
        logger.warning("EMBEDDINGS_ENABLED=true but OPENAI_API_KEY missing; embeddings will be disabled")

    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    indexer = Indexer()
    logger.info("Waiting for Meilisearch health at %s", MEILISEARCH_HOST)
    wait_meili_ready(indexer.client)
    logger.info("Meilisearch healthy; starting full sync + watcher")

    indexer.full_sync()

    queue = DebouncedQueue(indexer)
    queue.start()

    observer = Observer()
    observer.schedule(WatchHandler(queue), str(DOCS_DIR), recursive=True)
    observer.start()
    logger.info("Watching %s for changes...", DOCS_DIR)

    stop = Event()
    try:
        while not stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        queue.stop()


if __name__ == "__main__":
    main()
