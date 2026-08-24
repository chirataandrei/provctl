"""Load .provenance/policy.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from provctl.naming import normalize

POLICY_SCHEMA_VERSION = 1

DEFAULT_POLICY_TOML = """\
# provctl policy
schema_version = 1

[first_party]
# globs ok: "acme_*"
prefixes = []
allow = []

[indexes]
# true = missing names are unverifiable, not blocked
private = false

[thresholds]
new_package_days = 30
use_adoption_signal = true

[provenance]
enabled = true
mode = "deescalate"

[mapping]
# "import_name" = "distribution-name"
"""


@dataclass
class Policy:
    schema_version: int = POLICY_SCHEMA_VERSION
    first_party_prefixes: tuple[str, ...] = ()
    first_party_allow: frozenset[str] = frozenset()
    private_index: bool = False
    new_package_days: int = 30
    use_adoption_signal: bool = True
    provenance_enabled: bool = True
    provenance_mode: str = "deescalate"
    mapping_overrides: dict[str, tuple[str, ...]] = field(default_factory=dict)
    source_path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def default(cls) -> "Policy":
        return cls()

    @classmethod
    def load(cls, path: Path) -> "Policy":
        """Read policy from disk, falling back to defaults if absent."""
        if not path.exists():
            return cls.default()
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            policy = cls.default()
            policy.warnings.append(f"could not read {path}: {exc}; using defaults")
            return policy

        first_party = raw.get("first_party", {}) or {}
        indexes = raw.get("indexes", {}) or {}
        thresholds = raw.get("thresholds", {}) or {}
        provenance = raw.get("provenance", {}) or {}
        mapping = raw.get("mapping", {}) or {}

        overrides: dict[str, tuple[str, ...]] = {}
        for module, value in mapping.items():
            if isinstance(value, str):
                overrides[module] = (normalize(value),)
            elif isinstance(value, list):
                overrides[module] = tuple(normalize(v) for v in value if isinstance(v, str))

        return cls(
            schema_version=int(raw.get("schema_version", POLICY_SCHEMA_VERSION)),
            first_party_prefixes=tuple(
                p for p in first_party.get("prefixes", []) if isinstance(p, str)
            ),
            first_party_allow=frozenset(
                normalize(a) for a in first_party.get("allow", []) if isinstance(a, str)
            ),
            private_index=bool(indexes.get("private", False)),
            new_package_days=int(thresholds.get("new_package_days", 30)),
            use_adoption_signal=bool(thresholds.get("use_adoption_signal", True)),
            provenance_enabled=bool(provenance.get("enabled", True)),
            provenance_mode=str(provenance.get("mode", "deescalate")),
            mapping_overrides=overrides,
            source_path=path,
        )
