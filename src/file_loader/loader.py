#!/usr/bin/env python3
import os
import time
import json
import hashlib
import logging
from pathlib import Path
from threading import Event

import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

MEILISEARCH_HOST = os.environ.get("MEILISEARCH_HOST", "http://meilisearch:7700").rstrip("/")
DOCS_DIR = Path(os.environ.get("DOCS_DIR", "/volumes/output"))
MEILISEARCH_INDEX = os.environ.get("MEILISEARCH_INDEX", "docs")
MEILISEARCH_MASTER_KEY = os.environ.get("MEILISEARCH_MASTER_KEY", "").strip()
BATCH_SIZE = int(os.environ.get("MEILISEARCH_BATCH_SIZE", "200"))
MAX_BYTES = int(os.environ.get("MEILISEARCH_MAX_BYTES", str(2 * 1024 * 1024)))

ALLOWED_EXTS = set(
    e.strip().lower()
    for e in os.environ.get(
        "MEILISEARCH_ALLOWED_EXTS",
        ".md,.mdx,.txt,.json,.yml,.yaml,.toml,.js,.ts,.vue,.css,.html,.sh,.py",
    ).split(",")
    if e.strip()
)

session = requests.Session()

headers = {
    "Content-Type": "application/json"
}
if MEILISEARCH_MASTER_KEY:
    headers["Authorization"] = f"Bearer {MEILISEARCH_MASTER_KEY}"

# Logging setup
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [file_loader] %(levelname)s: %(message)s",
)

logger = logging.getLogger("file_loader")


def require_master_key() -> str:
    """Return the MEILISEARCH_MASTER_KEY or raise if missing."""
    if MEILISEARCH_MASTER_KEY:
        return MEILISEARCH_MASTER_KEY

    raise RuntimeError("MEILISEARCH_MASTER_KEY is required but not set")


def wait_meili_ready(timeout_s: int = 600):
    deadline = time.time() + timeout_s

    while time.time() < deadline:
        try:
            r = session.get(f"{MEILISEARCH_HOST}/health", timeout=2)
            if r.ok:
                return
        except Exception:
            pass
        time.sleep(0.5)

    raise TimeoutError("Meilisearch not healthy in time")


def ensure_index(uid: str):
    """Ensure the Meilisearch index exists without triggering failing create tasks.

    Prior behavior always POSTed to /indexes which, when the index already exists,
    could yield a 202 task that the scheduler later marks as failed ("Index already exists"),
    causing noisy logs. We first check with GET and only create on 404.
    """
    # Fast path: check existence
    try:
        gr = session.get(f"{MEILISEARCH_HOST}/indexes/{uid}", headers=headers, timeout=10)
        if gr.status_code == 200:
            logger.debug("Index '%s' already exists (GET)", uid)
            return
    except Exception:
        # If GET fails for transient reasons, we fall back to create attempt below
        pass

    # Create only if not found
    r = session.post(
        f"{MEILISEARCH_HOST}/indexes",
        headers=headers,
        data=json.dumps({"uid": uid, "primaryKey": "id"}),
        timeout=10,
    )

    if r.status_code in (200, 202):
        logger.debug("Index '%s' created or acknowledged", uid)
        return

    if r.status_code == 409:
        # Treat conflict as already existing, silence scheduler error spam
        logger.debug("Index '%s' already exists (409)", uid)
        return

    r.raise_for_status()


def doc_id_for(rel_path: str) -> str:
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()


def read_doc(path: Path):
    try:
        if not path.is_file():
            return None

        if any(part.startswith(".") for part in path.relative_to(DOCS_DIR).parts):
            return None

        ext = path.suffix.lower()

        if ALLOWED_EXTS and ext and ext not in ALLOWED_EXTS:
            return None

        size = path.stat().st_size

        if size > MAX_BYTES:
            return None

        content = path.read_text(encoding="utf-8", errors="replace")
        rel = str(path.relative_to(DOCS_DIR)).replace("\\", "/")

        return {
            "id": doc_id_for(rel),
            "path": rel,
            "filename": path.name,
            "ext": ext.lstrip("."),
            "bytes": size,
            "content": content,
        }
    except FileNotFoundError:
        return None


def top_level_index_for(rel_path: str) -> str | None:
    """Return the index uid based on the top-level directory under DOCS_DIR.

    If the file is directly under DOCS_DIR (no folder), return None to skip.
    """
    parts = rel_path.split("/") if rel_path else []

    # Require at least two path segments: [top_level_dir, filename or subdir, ...]
    if len(parts) < 2:
        return None

    top = parts[0].strip()

    if not top:
        return None

    return top


def upsert_docs(index_uid: str, docs):
    if not docs:
        return

    for attempt in range(10):
        try:
            r = session.post(
                f"{MEILISEARCH_HOST}/indexes/{index_uid}/documents?primaryKey=id",
                headers=headers,
                data=json.dumps(docs),
                timeout=30,
            )
            r.raise_for_status()
            logger.debug("Upserted %d docs into index '%s'", len(docs), index_uid)
            return
        except Exception as e:
            logger.warning("Upsert failed for index '%s' (attempt %d): %s", index_uid, attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))


def delete_doc_ids(index_uid: str, ids):
    if not ids:
        return
    for attempt in range(10):
        try:
            r = session.post(
                f"{MEILISEARCH_HOST}/indexes/{index_uid}/documents/delete-batch",
                headers=headers,
                data=json.dumps(ids),
                timeout=30,
            )

            r.raise_for_status()
            logger.debug("Deleted %d doc ids from index '%s'", len(ids), index_uid)
            return
        except Exception as e:
            logger.warning("Delete failed for index '%s' (attempt %d): %s", index_uid, attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))


def initial_full_load():
    # Batches per index uid
    batches: dict[str, list] = {}
    total = 0
    logger.info("Starting initial full load from %s (per-folder indexes)", DOCS_DIR)

    for p in DOCS_DIR.rglob("*"):
        if not p.is_file():
            continue

        doc = read_doc(p)

        if not doc:
            continue

        index_uid = top_level_index_for(doc["path"])  # decide index by first folder

        if not index_uid:
            # Skip files at DOCS_DIR root to adhere to 'each folder' requirement
            logger.debug("Skipping file not in a folder: %s", doc["path"])
            continue

        # ensure index exists on first reference
        if index_uid not in batches:
            ensure_index(index_uid)
            batches[index_uid] = []

        batch = batches[index_uid]
        batch.append(doc)

        if len(batch) >= BATCH_SIZE:
            upsert_docs(index_uid, batch)
            total += len(batch)
            batch.clear()

    # flush remaining batches
    for index_uid, batch in batches.items():
        if batch:
            upsert_docs(index_uid, batch)
            total += len(batch)

    logger.info("Initial load complete: %d documents across %d indexes", total, len(batches))


class Handler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return

        path = Path(event.src_path)
        doc = read_doc(path)

        if doc:
            index_uid = top_level_index_for(doc["path"])

            if not index_uid:
                logger.debug("Create ignored (no folder index): %s", doc["path"])
                return

            ensure_index(index_uid)
            upsert_docs(index_uid, [doc])
            logger.info("Added %s to index '%s'", doc['path'], index_uid)

    def on_modified(self, event):
        if event.is_directory:
            return

        path = Path(event.src_path)
        doc = read_doc(path)

        if doc:
            index_uid = top_level_index_for(doc["path"])

            if not index_uid:
                logger.debug("Modify ignored (no folder index): %s", doc["path"])
                return

            ensure_index(index_uid)
            upsert_docs(index_uid, [doc])
            logger.info("Updated %s in index '%s'", doc['path'], index_uid)

    def on_deleted(self, event):
        if event.is_directory:
            return
        try:
            rel = str(Path(event.src_path).relative_to(DOCS_DIR)).replace("\\", "/")
        except Exception:
            return

        index_uid = top_level_index_for(rel)

        if not index_uid:
            logger.debug("Delete ignored (no folder index): %s", rel)
            return

        delete_doc_ids(index_uid, [doc_id_for(rel)])
        logger.info("Deleted %s from index '%s'", rel, index_uid)


def main():
    logger.info(
        "Loader starting; host=%s base_index=%s docs_dir=%s (multi-index by folder)",
        MEILISEARCH_HOST,
        MEILISEARCH_INDEX,
        DOCS_DIR,
    )

    # Ensure we have an API key
    if not headers.get("Authorization"):
        key = require_master_key()
        headers["Authorization"] = f"Bearer {key}"

    logger.info("Master key set; waiting for Meilisearch health at %s/health", MEILISEARCH_HOST)
    wait_meili_ready()
    logger.info("Meilisearch is healthy; ensuring indexes for top-level folders under %s", DOCS_DIR)

    # Ensure an index exists for each top-level folder present at startup
    if DOCS_DIR.exists():
        for child in DOCS_DIR.iterdir():
            if child.is_dir() and not child.name.startswith('.'):
                ensure_index(child.name)

    # Ensure docs dir exists
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    # Initial load (per-folder indexes)
    initial_full_load()

    # Watch for changes
    observer = Observer()
    observer.schedule(Handler(), str(DOCS_DIR), recursive=True)
    observer.start()
    logger.info("Watching for filesystem changes...")
    stop = Event()

    try:
        while not stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
