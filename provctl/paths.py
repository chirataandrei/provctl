"""Filesystem locations for cache and in-repo state."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROVENANCE_DIR = ".provenance"
POLICY_FILE = "policy.toml"
DECISIONS_FILE = "decisions.jsonl"


def cache_dir() -> Path:
    """User-level cache for the PyPI index and enrichment database."""
    override = os.environ.get("PROVCTL_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "provctl"


def find_repo_root(start: Path | None = None) -> Path | None:
    """Locate the enclosing git work tree, or None outside a repo."""
    start = start or Path.cwd()
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    path = out.stdout.strip()
    return Path(path) if path else None


def provenance_dir(repo_root: Path) -> Path:
    return repo_root / PROVENANCE_DIR


def policy_path(repo_root: Path) -> Path:
    return provenance_dir(repo_root) / POLICY_FILE


def decisions_path(repo_root: Path) -> Path:
    return provenance_dir(repo_root) / DECISIONS_FILE
