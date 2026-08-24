"""Adoption signal, such as it is."""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from provctl.index.store import IndexStore
from provctl.naming import normalize

TOP_PACKAGES_URL = (
    "https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/"
    "top-pypi-packages.min.json"
)
_ADOPTION_FILE = "adoption.txt"
_ADOPTION_META = "adoption-meta.json"

FETCH_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class AdoptionSet:
    """Normalized set of widely-adopted project names."""

    names: frozenset[str]
    generated_at: str = ""

    def is_widely_adopted(self, name: str) -> bool:
        return normalize(name) in self.names

    def __len__(self) -> int:
        return len(self.names)


EMPTY_ADOPTION = AdoptionSet(names=frozenset())


def adoption_paths(store: IndexStore) -> tuple[Path, Path]:
    return store.cache_dir / _ADOPTION_FILE, store.cache_dir / _ADOPTION_META


def load(store: IndexStore) -> AdoptionSet:
    """Load the cached adoption set, or an empty one if never fetched."""
    names_path, meta_path = adoption_paths(store)
    if not names_path.exists():
        return EMPTY_ADOPTION
    try:
        raw = names_path.read_text(encoding="utf-8")
    except OSError:
        return EMPTY_ADOPTION

    generated_at = ""
    if meta_path.exists():
        try:
            generated_at = json.loads(meta_path.read_text()).get("generated_at", "")
        except (json.JSONDecodeError, OSError):
            pass

    return AdoptionSet(
        names=frozenset(line for line in raw.splitlines() if line),
        generated_at=generated_at,
    )


def refresh(store: IndexStore, *, source: str | None = None) -> AdoptionSet:
    """Fetch the top-packages list and cache the normalized name set."""
    source = source or os.environ.get("PROVCTL_ADOPTION_SOURCE") or TOP_PACKAGES_URL

    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "provctl/0.1"})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            document = json.loads(response.read())
    else:
        path = Path(source[len("file://"):] if source.startswith("file://") else source)
        document = json.loads(path.read_text(encoding="utf-8"))

    rows = document.get("rows", [])
    names = sorted({normalize(row["project"]) for row in rows if row.get("project")})

    store.cache_dir.mkdir(parents=True, exist_ok=True)
    names_path, meta_path = adoption_paths(store)
    IndexStore._atomic_write(names_path, ("\n".join(names) + "\n").encode("utf-8"))
    IndexStore._atomic_write(
        meta_path,
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source_url": TOP_PACKAGES_URL,
                "count": len(names),
                "upstream_last_update": document.get("last_update", ""),
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8"),
    )
    return AdoptionSet(names=frozenset(names))
