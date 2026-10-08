"""Extend the corpus to ~100 real Python repos, selected by a fixed rule.

Rule (so the choice is not cherry-picked): top GitHub Python repos by stars,
size <= 300 MB, not already in CORPUS, and after a shallow clone must contain a
dependency manifest (requirements*.txt, pyproject.toml, setup.py/cfg, Pipfile).
Lists, tutorials and docs-only repos fail the manifest test and are dropped.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from measurements.corpus import CORPUS
from measurements.run import WORK_DIR, clone
from measurements.corpus import Repo

TARGET_TOTAL = 100
OUT = Path(__file__).parent / "data" / "extra_repos.txt"
MANIFESTS = ("requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile")


def has_manifest(path: Path) -> bool:
    return any(next(path.rglob(pat), None) for pat in MANIFESTS)


def main() -> int:
    have = {r.url.rstrip("/").lower() for r in CORPUS}
    out = subprocess.run(
        ["gh", "search", "repos", "--language=python", "--sort=stars", "--limit=300",
         "--json", "fullName,url,size"],
        capture_output=True, text=True, check=True,
    )
    candidates = json.loads(out.stdout)
    picked: list[str] = []
    for c in candidates:
        if len(CORPUS) + len(picked) >= TARGET_TOTAL:
            break
        if c["url"].lower() in have or c["size"] > 300_000:  # size is KB
            continue
        name = c["fullName"].replace("/", "__")
        repo = Repo(name, c["url"])
        if not clone(repo, WORK_DIR / name):
            continue
        if not has_manifest(WORK_DIR / name):
            print(f"  dropped {c['fullName']} (no manifest)")
            continue
        picked.append(f"{name} {c['url']}")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(picked) + "\n")
    print(f"picked {len(picked)} extra repos -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
