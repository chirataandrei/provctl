"""Map an import name to the distribution(s) that provide it."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum

from provctl.naming import module_to_candidate_dist, normalize

# Curated import-name -> distribution-name exceptions. Deliberately small and
# high-confidence: every entry here is a case where the normalization heuristic
# produces a name that either does not exist or belongs to a *different*
# project, which is the dangerous kind of wrong.
CURATED_MAP: dict[str, tuple[str, ...]] = {
    "yaml": ("pyyaml",),
    "cv2": ("opencv-python",),
    "sklearn": ("scikit-learn",),
    "skimage": ("scikit-image",),
    "PIL": ("pillow",),
    "bs4": ("beautifulsoup4",),
    "dateutil": ("python-dateutil",),
    "dotenv": ("python-dotenv",),
    "jose": ("python-jose",),
    "magic": ("python-magic",),
    "serial": ("pyserial",),
    "usb": ("pyusb",),
    "OpenSSL": ("pyopenssl",),
    "Crypto": ("pycryptodome",),
    "Cryptodome": ("pycryptodomex",),
    "jwt": ("pyjwt",),
    "attr": ("attrs",),
    "attrs": ("attrs",),
    "google": ("protobuf",),
    "pkg_resources": ("setuptools",),
    "setuptools": ("setuptools",),
    "pytest": ("pytest",),
    "docx": ("python-docx",),
    "pptx": ("python-pptx",),
    "fitz": ("pymupdf",),
    "MySQLdb": ("mysqlclient",),
    "psycopg2": ("psycopg2-binary", "psycopg2"),
    "memcache": ("python-memcached",),
    "redis": ("redis",),
    "zoneinfo": ("backports-zoneinfo",),
    "win32api": ("pywin32",),
    "win32com": ("pywin32",),
    "pythoncom": ("pywin32",),
    "gi": ("pygobject",),
    "cairo": ("pycairo",),
    "IPython": ("ipython",),
    "notebook": ("notebook",),
    "matplotlib": ("matplotlib",),
    "mpl_toolkits": ("matplotlib",),
    "ruamel": ("ruamel-yaml",),
    "lxml": ("lxml",),
    "grpc": ("grpcio",),
    "grpc_tools": ("grpcio-tools",),
    "pkg_config": ("pkgconfig",),
    "importlib_metadata": ("importlib-metadata",),
    "typing_extensions": ("typing-extensions",),
    "pydantic_settings": ("pydantic-settings",),
    "jinja2": ("jinja2",),
    "markdown_it": ("markdown-it-py",),
    "slugify": ("python-slugify",),
    "socks": ("pysocks",),
    "Xlib": ("python-xlib",),
    "nacl": ("pynacl",),
    "OpenGL": ("pyopengl",),
    "wx": ("wxpython",),
    "zmq": ("pyzmq",),
    "tkcalendar": ("tkcalendar",),
    "faiss": ("faiss-cpu", "faiss-gpu"),
    "torch": ("torch",),
    "tensorflow": ("tensorflow",),
    "cpuinfo": ("py-cpuinfo",),
    "ldap3": ("ldap3",),
    "ldap": ("python-ldap",),
    "snappy": ("python-snappy",),
    "yamlordereddictloader": ("yamlordereddictloader",),
}


class Confidence(str, Enum):
    """How the mapping was obtained. Drives whether a finding may block."""

    INSTALLED = "installed"      # from the project's own environment
    CURATED = "curated"          # shipped exception map
    HEURISTIC = "heuristic"      # name normalization
    AMBIGUOUS = "ambiguous"      # several distributions provide this module
    UNKNOWN = "unknown"          # nothing provides it


@dataclass(frozen=True)
class Resolution:
    module: str
    candidates: tuple[str, ...]
    confidence: Confidence

    @property
    def is_definite(self) -> bool:
        """True only when exactly one distribution is known to provide it."""
        return (
            len(self.candidates) == 1
            and self.confidence in (Confidence.INSTALLED, Confidence.CURATED)
        )

    @property
    def primary(self) -> str | None:
        return self.candidates[0] if len(self.candidates) == 1 else None


def installed_packages_distributions() -> dict[str, tuple[str, ...]]:
    """Snapshot of `module -> distributions` for the running environment."""
    try:
        from importlib.metadata import packages_distributions
    except ImportError:  # pragma: no cover - Python < 3.10
        return {}
    try:
        raw = packages_distributions()
    except Exception:
        return {}
    return {
        module: tuple(normalize(dist) for dist in dists)
        for module, dists in raw.items()
        if dists
    }


class DistributionResolver:
    def __init__(
        self,
        installed: dict[str, tuple[str, ...]] | None = None,
        overrides: dict[str, tuple[str, ...]] | None = None,
    ):
        # User overrides win over everything: they are the escape hatch for a
        # mapping we got wrong, and a tool that cannot be corrected gets
        # disabled rather than fixed.
        self._overrides = {k: tuple(normalize(v) for v in vs) for k, vs in (overrides or {}).items()}
        self._installed = installed if installed is not None else installed_packages_distributions()

    def resolve(self, module: str) -> Resolution:
        if module in self._overrides:
            candidates = self._overrides[module]
            return Resolution(
                module,
                candidates,
                Confidence.CURATED if len(candidates) == 1 else Confidence.AMBIGUOUS,
            )

        installed = self._installed.get(module)
        if installed:
            if len(installed) > 1:
                return Resolution(module, tuple(sorted(installed)), Confidence.AMBIGUOUS)
            return Resolution(module, installed, Confidence.INSTALLED)

        curated = CURATED_MAP.get(module)
        if curated:
            normalized = tuple(normalize(c) for c in curated)
            if len(normalized) > 1:
                return Resolution(module, normalized, Confidence.AMBIGUOUS)
            return Resolution(module, normalized, Confidence.CURATED)

        candidate = module_to_candidate_dist(module)
        if not candidate:
            return Resolution(module, (), Confidence.UNKNOWN)
        return Resolution(module, (candidate,), Confidence.HEURISTIC)


def is_stdlib_module(module: str) -> bool:
    return module in sys.stdlib_module_names
