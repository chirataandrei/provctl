"""Name normalization, shared by every layer that compares package names."""

from __future__ import annotations

import re

from packaging.utils import canonicalize_name

# PEP 503 normalization, used for distribution names.
_normalize_cache: dict[str, str] = {}


def normalize(name: str) -> str:
    """PEP 503 normalized distribution name (lowercase, runs of -_. -> -)."""
    cached = _normalize_cache.get(name)
    if cached is not None:
        return cached
    try:
        result = canonicalize_name(name)
    except Exception:
        # canonicalize_name is strict about some inputs; fall back to the
        # documented regex rather than raising out of a lookup path.
        result = re.sub(r"[-_.]+", "-", name).lower()
    _normalize_cache[name] = result
    return result


def module_to_candidate_dist(module: str) -> str:
    """Best-effort distribution name for a top-level import name."""
    return normalize(module.replace("_", "-"))


def top_level(module: str) -> str:
    """First path component of a dotted module name."""
    return module.split(".", 1)[0]
