"""Download the PyPI name list. Never called from check."""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from provctl import http
from provctl.index.store import IndexMeta, IndexStore

SIMPLE_INDEX_URL = "https://pypi.org/simple/"
SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"
USER_AGENT = "provctl/0.1 (+https://github.com/chirataandrei/provctl)"

# The index is ~9.4 MB gzipped and fetches in under a second, so a generous
# timeout costs nothing and avoids spurious failures on slow links.
FETCH_TIMEOUT_SECONDS = 180

DEFAULT_MAX_AGE = timedelta(days=7)


@dataclass(frozen=True)
class RefreshResult:
    meta: IndexMeta
    changed: bool
    note: str = ""


_GZIP_MAGIC = b"\x1f\x8b"


def _load_source(source: str) -> bytes:
    """Read the index document from a URL, or from a local file / mirror."""
    if source.startswith(("http://", "https://")):
        return http.get(source, accept=SIMPLE_ACCEPT, timeout=FETCH_TIMEOUT_SECONDS)

    path = Path(source[len("file://"):] if source.startswith("file://") else source)
    raw = path.read_bytes()
    return gzip.decompress(raw) if raw[:2] == _GZIP_MAGIC else raw


def refresh(store: IndexStore, *, force: bool = False, source: str | None = None) -> RefreshResult:
    """Download the full project list and atomically replace the local index."""
    existing = store.read_meta()

    source = source or os.environ.get("PROVCTL_INDEX_SOURCE") or SIMPLE_INDEX_URL
    document = json.loads(_load_source(source))

    projects = document.get("projects", [])
    names = [entry.get("name", "") for entry in projects]
    meta_block = document.get("meta", {}) or {}
    last_serial = meta_block.get("_last-serial")

    # PyPI's _last-serial is a monotonic counter over all index events. If it
    # has not moved, nothing was published or yanked and the name set cannot
    # have changed, so we can skip rewriting 12 MB.
    if (
        not force
        and existing is not None
        and existing.last_serial is not None
        and last_serial == existing.last_serial
        and store.exists()
    ):
        return RefreshResult(meta=existing, changed=False, note="index already current")

    meta = store.write(
        names,
        last_serial=last_serial if isinstance(last_serial, int) else None,
        source_url=source,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    return RefreshResult(meta=meta, changed=True)


def staleness(meta: IndexMeta | None) -> timedelta | None:
    """Age of the cached index, or None if unknown."""
    if meta is None or not meta.fetched_at:
        return None
    try:
        fetched = datetime.fromisoformat(meta.fetched_at)
    except ValueError:
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched


def is_stale(meta: IndexMeta | None, max_age: timedelta = DEFAULT_MAX_AGE) -> bool:
    age = staleness(meta)
    if age is None:
        return True
    return age > max_age


def ensure_index(cache: Path, *, auto: bool = True) -> IndexStore:
    """Return a populated store, fetching once if the cache is empty."""
    store = IndexStore(cache)
    if not store.exists() and auto:
        refresh(store)
    return store
