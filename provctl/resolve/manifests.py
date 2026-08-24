"""Parse requirements/pyproject/Pipfile and pick up extra index URLs."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from provctl.naming import normalize

MANIFEST_FILENAMES = {
    "requirements.txt",
    "requirements.in",
    "pyproject.toml",
    "Pipfile",
    "setup.cfg",
}

# requirements.txt files are frequently named things like `requirements-dev.txt`
# or live under `requirements/`.
_REQUIREMENTS_PATTERN = re.compile(r"(^|/)requirements[^/]*\.(txt|in)$")

_PYPI_HOSTS = {"pypi.org", "www.pypi.org", "files.pythonhosted.org", "pypi.python.org"}

# A requirement line: name, optional extras, then version specifier / marker.
_REQUIREMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[(?P<extras>[^\]]*)\])?\s*(?P<rest>.*)$"
)

_OPTION_LINE_RE = re.compile(r"^\s*(?P<opt>-{1,2}[A-Za-z-]+)[=\s]+(?P<value>\S+)")


@dataclass
class ManifestEntry:
    """One declared dependency."""

    raw_name: str
    name: str  # normalized
    manifest: str
    lineno: int
    extras: tuple[str, ...] = ()
    is_url: bool = False  # direct URL / VCS / local path requirement

    def __post_init__(self) -> None:
        if not self.name:
            self.name = normalize(self.raw_name)


@dataclass
class ManifestScan:
    entries: list[ManifestEntry] = field(default_factory=list)
    index_urls: list[str] = field(default_factory=list)
    project_name: str | None = None
    parse_errors: list[str] = field(default_factory=list)
    # Names redirected away from PyPI by tooling config rather than by syntax
    # on the requirement line itself.
    url_requirements: set[str] = field(default_factory=set)
    workspace_members: set[str] = field(default_factory=set)

    @property
    def has_private_index(self) -> bool:
        """True if any configured index is something other than public PyPI."""
        return any(not _is_public_pypi(url) for url in self.index_urls)


def _is_public_pypi(url: str) -> bool:
    lowered = url.lower()
    return any(host in lowered for host in _PYPI_HOSTS)


def is_manifest(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name in MANIFEST_FILENAMES:
        return True
    return bool(_REQUIREMENTS_PATTERN.search(path))




def parse_requirements(text: str, source: str) -> ManifestScan:
    scan = ManifestScan()
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Strip inline comments, but only when ` #` -- a bare '#' can appear
        # inside a URL fragment such as `...#egg=name`.
        if " #" in line:
            line = line.split(" #", 1)[0].strip()

        if line.startswith("-"):
            option = _OPTION_LINE_RE.match(line)
            if option:
                opt = option.group("opt")
                value = option.group("value")
                if opt in {"--index-url", "-i", "--extra-index-url"}:
                    scan.index_urls.append(value)
            continue

        # Direct URL / VCS / local requirements name a distribution only
        # incidentally; they are never fetched from an index by name, so an
        # existence check against PyPI would be meaningless.
        if _looks_like_url_requirement(line):
            scan.entries.append(
                ManifestEntry(
                    raw_name=line, name=normalize(_egg_name(line) or line),
                    manifest=source, lineno=lineno, is_url=True,
                )
            )
            continue

        match = _REQUIREMENT_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        extras = match.group("extras") or ""
        scan.entries.append(
            ManifestEntry(
                raw_name=name,
                name=normalize(name),
                manifest=source,
                lineno=lineno,
                extras=tuple(e.strip() for e in extras.split(",") if e.strip()),
            )
        )
    return scan


def _looks_like_url_requirement(line: str) -> bool:
    return bool(
        re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", line)
        or line.startswith((".", "/", "git+", "file:"))
        or " @ " in line
    )


def _egg_name(line: str) -> str | None:
    match = re.search(r"#egg=([A-Za-z0-9._-]+)", line)
    if match:
        return match.group(1)
    if " @ " in line:
        return line.split(" @ ", 1)[0].strip()
    return None




def parse_pyproject(text: str, source: str) -> ManifestScan:
    scan = ManifestScan()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        scan.parse_errors.append(f"{source}: {exc}")
        return scan

    project = data.get("project", {})
    if isinstance(project, dict):
        name = project.get("name")
        if isinstance(name, str):
            scan.project_name = normalize(name)
        _collect_pep621(project.get("dependencies"), scan, source)
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for group in optional.values():
                _collect_pep621(group, scan, source)

    # PEP 735 dependency groups.
    groups = data.get("dependency-groups", {})
    if isinstance(groups, dict):
        for group in groups.values():
            _collect_pep621(group, scan, source)

    tool = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
    _collect_poetry(tool.get("poetry", {}), scan, source)
    _collect_uv(tool.get("uv", {}), scan)
    return scan


def _collect_pep621(items, scan: ManifestScan, source: str) -> None:
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, str):
            continue
        sub = parse_requirements(item, source)
        scan.entries.extend(sub.entries)


def _collect_poetry(poetry, scan: ManifestScan, source: str) -> None:
    if not isinstance(poetry, dict):
        return
    name = poetry.get("name")
    if isinstance(name, str) and not scan.project_name:
        scan.project_name = normalize(name)

    def add_group(table) -> None:
        if not isinstance(table, dict):
            return
        for dep_name, spec in table.items():
            if dep_name.lower() == "python":
                continue
            # `{ path = ... }` and `{ git = ... }` are first-party or vendored;
            # `{ source = "internal" }` names a private index.
            is_url = isinstance(spec, dict) and any(
                key in spec for key in ("path", "git", "url")
            )
            if isinstance(spec, dict) and isinstance(spec.get("source"), str):
                scan.index_urls.append(f"poetry-source:{spec['source']}")
            scan.entries.append(
                ManifestEntry(
                    raw_name=dep_name, name=normalize(dep_name),
                    manifest=source, lineno=0, is_url=is_url,
                )
            )

    add_group(poetry.get("dependencies"))
    add_group(poetry.get("dev-dependencies"))
    for group in (poetry.get("group") or {}).values():
        if isinstance(group, dict):
            add_group(group.get("dependencies"))

    for source_entry in poetry.get("source", []) or []:
        if isinstance(source_entry, dict) and isinstance(source_entry.get("url"), str):
            scan.index_urls.append(source_entry["url"])


def _collect_uv(uv, scan: ManifestScan) -> None:
    if not isinstance(uv, dict):
        return

    # `[tool.uv.sources]` redirects a dependency away from PyPI: to a git repo,
    # a local path, or a workspace sibling. Measured: this was the root cause of
    # both remaining false positives on the 30-repo corpus (pydantic-docs,
    # mypy-primer). Checking such a name against PyPI is meaningless -- it is
    # never resolved by name from an index.
    sources = uv.get("sources")
    if isinstance(sources, dict):
        for dep_name, spec in sources.items():
            if not isinstance(spec, dict):
                continue
            key = normalize(dep_name)
            if spec.get("workspace") is True:
                scan.workspace_members.add(key)
            elif any(k in spec for k in ("git", "url", "path")):
                scan.url_requirements.add(key)
            elif isinstance(spec.get("index"), str):
                scan.index_urls.append(f"uv-source:{spec['index']}")

    for entry in uv.get("index", []) or []:
        if isinstance(entry, dict) and isinstance(entry.get("url"), str):
            scan.index_urls.append(entry["url"])
    for key in ("index-url", "extra-index-url"):
        value = uv.get(key)
        if isinstance(value, str):
            scan.index_urls.append(value)
        elif isinstance(value, list):
            scan.index_urls.extend(v for v in value if isinstance(v, str))




def parse_pipfile(text: str, source: str) -> ManifestScan:
    scan = ManifestScan()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        scan.parse_errors.append(f"{source}: {exc}")
        return scan

    for section in ("packages", "dev-packages"):
        table = data.get(section, {})
        if not isinstance(table, dict):
            continue
        for name, spec in table.items():
            is_url = isinstance(spec, dict) and any(
                key in spec for key in ("path", "git", "file")
            )
            scan.entries.append(
                ManifestEntry(
                    raw_name=name, name=normalize(name),
                    manifest=source, lineno=0, is_url=is_url,
                )
            )

    for source_entry in data.get("source", []) or []:
        if isinstance(source_entry, dict) and isinstance(source_entry.get("url"), str):
            scan.index_urls.append(source_entry["url"])
    return scan




def parse_manifest(path: str, text: str) -> ManifestScan:
    name = path.rsplit("/", 1)[-1]
    if name == "pyproject.toml":
        return parse_pyproject(text, path)
    if name == "Pipfile":
        return parse_pipfile(text, path)
    if name == "setup.cfg":
        return ManifestScan()  # install_requires lives in an ini blob; not worth it for v0
    return parse_requirements(text, path)


def scan_paths(root: Path, paths: list[str]) -> ManifestScan:
    """Parse every manifest in `paths`, merging into one scan."""
    merged = ManifestScan()
    for rel in paths:
        if not is_manifest(rel):
            continue
        full = root / rel
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sub = parse_manifest(rel, text)
        merged.entries.extend(sub.entries)
        merged.index_urls.extend(sub.index_urls)
        merged.parse_errors.extend(sub.parse_errors)
        merged.url_requirements |= sub.url_requirements
        merged.workspace_members |= sub.workspace_members
        if sub.project_name and not merged.project_name:
            merged.project_name = sub.project_name
    return merged


def environment_index_urls(env: dict[str, str]) -> list[str]:
    """Index URLs configured via environment, which pip honours globally."""
    urls = []
    for key in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL"):
        value = env.get(key)
        if value:
            urls.extend(value.split())
    return urls
