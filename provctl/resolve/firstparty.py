"""Decide which names belong to this repo, not PyPI."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from provctl.naming import normalize

# Directories that never contain first-party source but often contain
# directories whose names collide with real package names (e.g. a `docs/`
# folder next to a vendored `requests/`).
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env",
    "node_modules", ".tox", ".nox", "build", "dist", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "site-packages", ".eggs",
}

# Conventional roots for a src-layout project.
_SRC_ROOTS = ("", "src", "lib", "python")


@dataclass
class FirstPartyIndex:
    """Everything we consider 'ours' rather than 'from an index'."""

    modules: set[str] = field(default_factory=set)
    distributions: set[str] = field(default_factory=set)
    prefixes: tuple[str, ...] = ()
    allowlist: frozenset[str] = frozenset()

    def is_first_party_module(self, module: str) -> bool:
        if module in self.modules:
            return True
        if normalize(module) in self.allowlist:
            return True
        return self._matches_prefix(module)

    def is_first_party_dist(self, dist: str) -> bool:
        key = normalize(dist)
        if key in self.distributions or key in self.allowlist:
            return True
        # A distribution named `acme-billing` installs the module
        # `acme_billing`. If that module is present in the repo, the
        # distribution is ours -- this is the monorepo case, and missing it
        # blocks every internal package on the first run.
        module_form = key.replace("-", "_")
        if module_form in self.modules or key in self.modules:
            return True
        return self._matches_prefix(module_form) or self._matches_prefix(key)

    def _matches_prefix(self, name: str) -> bool:
        return any(fnmatch(name, pattern) for pattern in self.prefixes)


def discover(
    repo_root: Path,
    *,
    project_name: str | None = None,
    src_roots: tuple[str, ...] = _SRC_ROOTS,
    prefixes: tuple[str, ...] = (),
    allowlist: frozenset[str] = frozenset(),
    extra_modules: set[str] | None = None,
    include_workspace: bool = True,
) -> FirstPartyIndex:
    """Scan the repo for importable top-level names that belong to it."""
    modules: set[str] = set(extra_modules or set())

    for root_name in src_roots:
        root = repo_root / root_name if root_name else repo_root
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.name.startswith(".") or child.name in _SKIP_DIRS:
                continue
            if child.is_dir():
                # A package (has __init__.py) or a namespace package dir that
                # contains Python at all. The second case matters for implicit
                # namespace packages, which have no __init__.py.
                if (child / "__init__.py").exists() or _contains_python(child):
                    modules.add(child.name)
            elif child.suffix == ".py" and child.stem != "setup":
                modules.add(child.stem)

    distributions: set[str] = set()
    if project_name:
        distributions.add(normalize(project_name))
        modules.add(project_name.replace("-", "_"))

    if include_workspace:
        distributions.update(discover_workspace_members(repo_root))

    return FirstPartyIndex(
        modules=modules,
        distributions=distributions,
        prefixes=prefixes,
        allowlist=allowlist,
    )


class LocalSiblingResolver:
    """Resolve an import against the directories between a file and the repo root."""

    def __init__(self, repo_root: Path):
        self._root = repo_root.resolve()
        self._cache: dict[tuple[str, str], bool] = {}

    def resolves(self, importing_file: str, module: str) -> bool:
        key = (importing_file, module)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = self._probe(importing_file, module)
        self._cache[key] = result
        return result

    def _probe(self, importing_file: str, module: str) -> bool:
        try:
            start = (self._root / importing_file).resolve().parent
        except OSError:
            return False

        current = start
        while True:
            if (current / f"{module}.py").exists() or (current / module).is_dir():
                return True
            if current == self._root or current.parent == current:
                return False
            if self._root not in current.parents:
                return False
            current = current.parent


def discover_workspace_members(
    repo_root: Path, *, max_depth: int = 4, max_manifests: int = 500
) -> set[str]:
    """Distribution names declared by nested manifests in the same repository."""
    names: set[str] = set()
    manifests_seen = 0

    def walk(directory: Path, depth: int) -> None:
        nonlocal manifests_seen
        if depth > max_depth or manifests_seen >= max_manifests:
            return
        try:
            entries = list(directory.iterdir())
        except OSError:
            return

        for entry in entries:
            if manifests_seen >= max_manifests:
                return
            if entry.is_dir():
                if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                    continue
                walk(entry, depth + 1)
            elif entry.name == "pyproject.toml":
                manifests_seen += 1
                name = _project_name_of(entry)
                if name:
                    names.add(name)

    walk(repo_root, 0)
    return names


def _project_name_of(pyproject: Path) -> str | None:
    """Read just the project name, tolerating anything malformed."""
    import tomllib

    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return None

    project = data.get("project")
    if isinstance(project, dict) and isinstance(project.get("name"), str):
        return normalize(project["name"])

    poetry = (data.get("tool") or {}).get("poetry")
    if isinstance(poetry, dict) and isinstance(poetry.get("name"), str):
        return normalize(poetry["name"])
    return None


def _contains_python(directory: Path, *, max_entries: int = 200) -> bool:
    """Cheap check for any .py file one level down."""
    try:
        for count, entry in enumerate(directory.iterdir()):
            if count >= max_entries:
                return False
            if entry.suffix == ".py":
                return True
    except OSError:
        return False
    return False
