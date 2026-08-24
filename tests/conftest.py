from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from provctl.index.store import IndexStore

# A small stand-in for the 862k-name PyPI index. Contains only real names, so a
# test asserting "this blocks" is asserting the name genuinely does not exist.
KNOWN_PACKAGES = [
    "requests", "PyYAML", "scikit-learn", "opencv-python", "Django", "numpy",
    "pillow", "beautifulsoup4", "fastapi", "flask", "pytest", "psycopg2",
    "psycopg2-binary", "internal-utils", "attrs", "urllib3",
]


@pytest.fixture
def index_store(tmp_path) -> IndexStore:
    store = IndexStore(tmp_path / "cache")
    store.write(
        KNOWN_PACKAGES,
        last_serial=1,
        source_url="test://fixture",
        fetched_at="2026-08-01T00:00:00+00:00",
    )
    return store


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", ".")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    return root


@pytest.fixture
def write_and_stage(repo):
    def _write(relative: str, content: str) -> Path:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git(repo, "add", relative)
        return path

    return _write
