#!/usr/bin/env python3
import os
import time
import json
import hashlib
import logging
import csv
from fnmatch import fnmatch
from pathlib import Path
from threading import Event

import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import yaml

MEILISEARCH_HOST = os.environ.get("MEILISEARCH_HOST", "http://meilisearch:7700").rstrip("/")
DOCS_DIR = Path(os.environ.get("DOCS_DIR", "/volumes/output"))
MEILISEARCH_INDEX = os.environ.get("MEILISEARCH_INDEX", "docs")
MEILISEARCH_MASTER_KEY = os.environ.get("MEILISEARCH_MASTER_KEY", "").strip()
BATCH_SIZE = int(os.environ.get("MEILISEARCH_BATCH_SIZE", "200"))
MAX_BYTES = int(os.environ.get("MEILISEARCH_MAX_BYTES", str(2 * 1024 * 1024)))
CONFIG_PATH = Path(os.environ.get("DOWNLOAD_CONFIG", "/config/download.yml"))

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


def _load_yaml_file(p: Path) -> dict:
    try:
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                return {}
            return data
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("Failed to read config YAML at %s: %s", p, e)
        return {}


def load_loader_rules() -> list[dict]:
    """Load `loaders` from CONFIG_PATH.

    Schema (each item):
      - path: relative folder under DOCS_DIR to watch (e.g., "nuxt")
        type: optional loader type (frontmatter, csv, json, yaml, etc.)
        match:
          glob: optional glob against filename (e.g., "*.md")
        processors: optional list of processors with optional match
    """
    cfg = _load_yaml_file(CONFIG_PATH)
    loaders = cfg.get("loaders") or []

    if not isinstance(loaders, list):
        logger.warning("Config 'loaders' is not a list; ignoring")
        return []

    # normalize
    norm = []

    for i, item in enumerate(loaders):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        if not path:
            logger.warning("loaders[%d] missing 'path'", i)
            continue
        ltype = (item.get("type") or "").strip().lower() or None
        match = item.get("match") or {}
        glob_pat = None

        if isinstance(match, dict):
            glob_pat = match.get("glob")
            if glob_pat is not None:
                glob_pat = str(glob_pat).strip()
                if not glob_pat:
                    glob_pat = None

        processors = item.get("processors") or []

        # normalize processors
        norm_procs = []

        if isinstance(processors, list):
            for p in processors:
                if not isinstance(p, dict):
                    continue
                ptype = str(p.get("type", "")).strip().lower() or "noop"
                pmatch = p.get("match") or {}
                pglob = None

                if isinstance(pmatch, dict):
                    pglob = pmatch.get("glob")

                    if pglob is not None:
                        pglob = str(pglob).strip() or None

                params = {
                    k: v for k, v in p.items()
                    if k not in ("type", "match")
                }

                norm_procs.append({"type": ptype, "glob": pglob, "params": params})

        norm.append({
            "path": path.strip("/"),
            "type": ltype,
            "glob": glob_pat,
            "processors": norm_procs,
        })
    if norm:
        logger.info("Loaded %d loader rule(s) from %s", len(norm), CONFIG_PATH)

    return norm


LOADER_RULES: list[dict] = []


def rule_matches(rule: dict, rel_path: str, filename: str) -> bool:
    # rule.path must be prefix of rel_path
    rule_path = rule.get("path") or ""

    if rule_path and not rel_path.startswith(rule_path.rstrip("/") + "/") and rel_path != rule_path:
        return False

    g = rule.get("glob")

    return fnmatch(filename, g) if g else True


def choose_rule_for(rel_path: str, filename: str) -> dict | None:
    for rule in LOADER_RULES:
        if rule_matches(rule, rel_path, filename):
            return rule

    return None


def infer_type_from_ext(ext: str) -> str | None:
    ext = ext.lower().lstrip(".")

    if ext in ("md", "mdx", "mdc"):
        return "frontmatter"
    if ext == "csv":
        return "csv"
    if ext in ("yml", "yaml"):
        return "yaml"
    if ext == "json":
        return "json"

    return None


def apply_processors(doc: dict, rule: dict) -> dict:
    filename = doc.get("filename", "")
    processors = rule.get("processors") or []

    for proc in processors:
        g = proc.get("glob")
        if g and not fnmatch(filename, g):
            continue

        ptype = proc.get("type") or "noop"
        params = proc.get("params") or {}

        try:
            if ptype == "noop":
                pass
            elif ptype == "add_fields":
                if isinstance(params, dict):
                    for k, v in params.items():
                        if k not in ("type", "glob"):
                            doc[k] = v
            else:
                logger.debug("Unknown processor '%s' – skipping", ptype)
        except Exception as e:
            logger.warning("Processor '%s' failed on %s: %s", ptype, filename, e)
    return doc


def parse_frontmatter(text: str) -> tuple[dict, str]:
    starts_unix = text.startswith("---\n")
    starts_win = text.startswith("---\r\n")

    if not starts_unix and not starts_win:
        return {}, text

    # Find the closing delimiter
    delim = "\n---\n"
    idx = text.find(delim, 4)

    if idx == -1:
        delim = "\r\n---\r\n"
        idx = text.find(delim, 4)
        if idx == -1:
            return {}, text

    start_len = 4 if starts_unix else 5
    header = text[start_len:idx]
    body = text[idx + len(delim):]

    try:
        fm = yaml.safe_load(header) or {}

        if not isinstance(fm, dict):
            fm = {
                "frontmatter_raw": header
            }

    except Exception:
        fm = {
            "frontmatter_raw": header
        }

    return fm, body


def require_master_key() -> str:
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
    try:
        gr = session.get(f"{MEILISEARCH_HOST}/indexes/{uid}", headers=headers, timeout=10)

        if gr.status_code == 200:
            logger.debug("Index '%s' already exists (GET)", uid)
            return
    except Exception:
        pass

    # Create only if not found
    r = session.post(
        f"{MEILISEARCH_HOST}/indexes",
        headers=headers,
        data=json.dumps({
            "uid": uid,
            "primaryKey": "id"
        }),
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

        base_doc = {
            "id": doc_id_for(rel),
            "path": rel,
            "filename": path.name,
            "ext": ext.lstrip("."),
            "bytes": size,
            "content": content,
        }

        # Determine loader rule and type
        rule = choose_rule_for(
            rel,
            path.name
        )

        ltype = None

        if rule and rule.get("type"):
            ltype = rule.get("type")
        else:
            ltype = infer_type_from_ext(ext)

        # If no special loader or ltype explicitly unknown, return base doc
        if not ltype:
            return base_doc

        # Apply loader-specific parsing/enrichment
        if ltype == "frontmatter":
            fm, body = parse_frontmatter(content)
            base_doc["frontmatter"] = fm
            base_doc["body"] = body
        elif ltype == "yaml":
            try:
                base_doc["yaml"] = yaml.safe_load(content)
            except Exception as e:
                logger.warning("YAML parse failed for %s: %s", rel, e)
        elif ltype == "json":
            try:
                base_doc["json"] = json.loads(content)
            except Exception as e:
                logger.warning("JSON parse failed for %s: %s", rel, e)
        elif ltype == "csv":
            try:
                # Read CSV into list of dicts
                rows = []

                with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        rows.append(row)

                base_doc["rows"] = rows

            except Exception as e:
                logger.warning("CSV parse failed for %s: %s", rel, e)
        else:
            logger.debug("Unknown loader type '%s' for %s; using default", ltype, rel)
            return base_doc

        # Run processors (if any)
        if rule:
            base_doc = apply_processors(base_doc, rule)

        return base_doc
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

    # Load loader rules from CONFIG_PATH if present
    global LOADER_RULES
    LOADER_RULES = load_loader_rules()
    if LOADER_RULES:
        for r in LOADER_RULES:
            logger.info(
                "Rule: path=%s type=%s glob=%s processors=%d",
                r.get("path"), r.get("type"), r.get("glob"), len(r.get("processors") or []),
            )
    else:
        logger.info("No loader rules configured; using default loader for all files")

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
