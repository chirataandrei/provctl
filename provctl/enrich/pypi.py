"""Package age metadata, cached in SQLite, never on the hot path."""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from provctl import http
from provctl.index.store import NameIndex
from provctl.naming import normalize

SIMPLE_PROJECT_URL = "https://pypi.org/simple/{name}/"
SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"
REQUEST_TIMEOUT = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS package_metadata (
    name TEXT PRIMARY KEY,
    first_release TEXT,
    latest_release TEXT,
    release_count INTEGER,
    fetched_at TEXT NOT NULL,
    error TEXT
);
"""


@dataclass(frozen=True)
class PackageMetadata:
    name: str
    first_release: datetime | None
    latest_release: datetime | None
    release_count: int

    def age_days(self, now: datetime | None = None) -> float | None:
        if self.first_release is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - self.first_release).total_seconds() / 86400


class MetadataCache:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MetadataCache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get(self, name: str) -> PackageMetadata | None:
        row = self._conn.execute(
            "SELECT name, first_release, latest_release, release_count, error "
            "FROM package_metadata WHERE name = ?",
            (normalize(name),),
        ).fetchone()
        if row is None or row[4]:
            return None
        return PackageMetadata(
            name=row[0],
            first_release=_parse_time(row[1]),
            latest_release=_parse_time(row[2]),
            release_count=row[3] or 0,
        )

    def put(self, name: str, meta: PackageMetadata | None, error: str | None = None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO package_metadata "
            "(name, first_release, latest_release, release_count, fetched_at, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                normalize(name),
                meta.first_release.isoformat() if meta and meta.first_release else None,
                meta.latest_release.isoformat() if meta and meta.latest_release else None,
                meta.release_count if meta else 0,
                datetime.now(timezone.utc).isoformat(),
                error,
            ),
        )

    def commit(self) -> None:
        self._conn.commit()

    def known_names(self) -> set[str]:
        return {
            row[0]
            for row in self._conn.execute("SELECT name FROM package_metadata")
        }


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fetch_metadata(name: str) -> PackageMetadata:
    """Fetch first/latest release times from the PEP 691/700 simple API."""
    url = SIMPLE_PROJECT_URL.format(name=normalize(name))
    document = json.loads(http.get(url, accept=SIMPLE_ACCEPT, timeout=REQUEST_TIMEOUT))

    times = [
        parsed
        for parsed in (_parse_time(f.get("upload-time")) for f in document.get("files", []))
        if parsed is not None
    ]
    return PackageMetadata(
        name=normalize(name),
        first_release=min(times) if times else None,
        latest_release=max(times) if times else None,
        release_count=len(document.get("versions", [])),
    )


def enrich(
    names: list[str],
    cache: MetadataCache,
    index: NameIndex,
    *,
    workers: int = 8,
    progress=None,
) -> tuple[int, int]:
    """Populate the cache for `names`. Returns (fetched, skipped)."""
    known = cache.known_names()
    todo = [
        n for n in {normalize(x) for x in names}
        if n not in known and index.contains_normalized(n)
    ]
    skipped = len({normalize(x) for x in names}) - len(todo)

    def work(name: str) -> tuple[str, PackageMetadata | None, str | None]:
        try:
            return name, fetch_metadata(name), None
        except (http.HttpError, urllib.error.URLError, OSError, ValueError,
                json.JSONDecodeError) as exc:
            return name, None, f"{type(exc).__name__}: {exc}"

    fetched = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, meta, error in pool.map(work, todo):
            cache.put(name, meta, error)
            fetched += 1
            if progress and fetched % 50 == 0:
                progress(fetched, len(todo))
        cache.commit()
    return fetched, skipped
