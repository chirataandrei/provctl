"""Sorted, mmap'd list of PyPI names. Exact lookup, no Bloom filter."""

from __future__ import annotations

import json
import mmap
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from provctl.naming import normalize

# Bumped when the on-disk layout changes in a way older readers cannot parse.
INDEX_FORMAT_VERSION = 1

_NAMES_FILE = "names.txt"
_META_FILE = "index-meta.json"


@dataclass(frozen=True)
class IndexMeta:
    """Provenance of the index itself, so `doctor` can report staleness."""

    format_version: int
    project_count: int
    last_serial: int | None
    fetched_at: str
    source_url: str

    @classmethod
    def from_dict(cls, raw: dict) -> "IndexMeta":
        return cls(
            format_version=raw.get("format_version", 0),
            project_count=raw.get("project_count", 0),
            last_serial=raw.get("last_serial"),
            fetched_at=raw.get("fetched_at", ""),
            source_url=raw.get("source_url", ""),
        )


class IndexMissingError(RuntimeError):
    """Raised when a lookup is attempted before the index has been fetched."""


class NameIndex:
    """Read-only, mmap-backed exact membership test over sorted names."""

    def __init__(self, path: Path):
        self._path = path
        self._fh = None
        self._mm: mmap.mmap | None = None
        self._empty = False


    def _ensure_open(self) -> mmap.mmap | None:
        """Open the mmap, or return None for a legitimately empty index."""
        if self._mm is not None or self._empty:
            return self._mm
        if not self._path.exists():
            raise IndexMissingError(
                f"no local index at {self._path}; run `provctl index refresh`"
            )
        self._fh = open(self._path, "rb")
        if os.fstat(self._fh.fileno()).st_size == 0:
            self._empty = True
            return None
        self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        return self._mm

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "NameIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


    def contains(self, name: str) -> bool:
        """True if `name` (normalized) is a real project on the index."""
        return self.contains_normalized(normalize(name))

    def contains_normalized(self, key: str) -> bool:
        if not key:
            return False
        mm = self._ensure_open()
        if mm is None:
            return False
        size = len(mm)
        if size == 0:
            return False

        target = key.encode("utf-8")
        lo, hi = 0, size

        # Binary search over byte offsets. Each probe snaps backwards to the
        # start of the line it lands in, which is what makes this work on a
        # flat newline-delimited buffer without an offset table.
        while lo < hi:
            mid = (lo + hi) // 2
            start = mm.rfind(b"\n", 0, mid) + 1  # rfind -> -1 becomes 0
            end = mm.find(b"\n", start)
            if end == -1:
                end = size
            line = mm[start:end]

            if line == target:
                return True
            if line < target:
                # Advance past this line; guard against a zero-width step when
                # the probe lands inside the final line.
                lo = end + 1
                if lo <= mid:
                    lo = mid + 1
            else:
                hi = start
        return False

    def __contains__(self, name: str) -> bool:
        return self.contains(name)


class IndexStore:
    """Owns the cache directory: atomic writes, metadata, and reads."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.names_path = cache_dir / _NAMES_FILE
        self.meta_path = cache_dir / _META_FILE

    def exists(self) -> bool:
        return self.names_path.exists() and self.meta_path.exists()

    def open_index(self) -> NameIndex:
        return NameIndex(self.names_path)

    def read_meta(self) -> IndexMeta | None:
        if not self.meta_path.exists():
            return None
        try:
            return IndexMeta.from_dict(json.loads(self.meta_path.read_text()))
        except (json.JSONDecodeError, OSError):
            return None

    def write(self, names: list[str], *, last_serial: int | None, source_url: str,
              fetched_at: str) -> IndexMeta:
        """Normalize, sort, dedupe and atomically swap in a new index."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Normalizing before sorting is what lets lookups be a pure byte
        # comparison rather than needing per-probe normalization.
        unique = sorted({normalize(n) for n in names if n})
        payload = "\n".join(unique)
        if payload:
            payload += "\n"

        self._atomic_write(self.names_path, payload.encode("utf-8"))

        meta = IndexMeta(
            format_version=INDEX_FORMAT_VERSION,
            project_count=len(unique),
            last_serial=last_serial,
            fetched_at=fetched_at,
            source_url=source_url,
        )
        self._atomic_write(
            self.meta_path,
            json.dumps(meta.__dict__, indent=2, sort_keys=True).encode("utf-8"),
        )
        return meta

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            # Never leave a stray temp file behind on failure.
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
