"""Core data types shared across layers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from enum import Enum


class Verdict(str, Enum):
    """Ordered by severity; `max` over a run gives the exit code."""

    OK = "ok"
    INFO = "info"
    WARN = "warn"
    BLOCK = "block"

    @property
    def rank(self) -> int:
        return _VERDICT_RANK[self]


_VERDICT_RANK = {Verdict.OK: 0, Verdict.INFO: 1, Verdict.WARN: 2, Verdict.BLOCK: 3}


class Origin(str, Enum):
    """Where the candidate dependency came from."""

    MANIFEST = "manifest"   # declared dependency: authoritative, may block
    IMPORT = "import"       # inferred from source: advisory only


class Reason(str, Enum):
    """Why a finding got its verdict. Stable identifiers, safe to grep in CI."""

    ABSENT_FROM_INDEX = "absent_from_index"
    ABSENT_BUT_FIRST_PARTY = "absent_but_first_party"
    ABSENT_BUT_PRIVATE_INDEX = "absent_but_private_index"
    ABSENT_UNRESOLVABLE_IMPORT = "absent_unresolvable_import"
    AMBIGUOUS_MAPPING = "ambiguous_mapping"
    DIRECT_URL_REQUIREMENT = "direct_url_requirement"
    SUSPICIOUS_METADATA = "suspicious_metadata"
    ACKNOWLEDGED = "acknowledged"
    PRESENT = "present"
    INDEX_UNAVAILABLE = "index_unavailable"


# Findings in this band are the only ones whose verdict provenance can change.
# Sizing this band is the cheapest falsification of the product thesis, so it is
# a named constant rather than an implicit condition.
PROVENANCE_SENSITIVE_REASONS = frozenset({Reason.SUSPICIOUS_METADATA})


@dataclass
class ProvenanceSummary:
    """Scrubbed provenance for a finding. Never contains free text."""

    state: str = "untracked"          # agent | known_human | untracked
    tool: str | None = None
    model: str | None = None
    session_id: str | None = None
    human_reviewed: bool = False      # agent lines subsequently edited by a human
    source: str | None = None         # which adapter produced this

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Finding:
    name: str                  # normalized distribution name
    raw_name: str
    origin: Origin
    verdict: Verdict
    reason: Reason
    detail: str = ""
    location: str = ""         # "path:line"
    candidates: tuple[str, ...] = ()
    confidence: str = ""
    provenance: ProvenanceSummary | None = None
    baseline_verdict: Verdict | None = None  # pre-provenance verdict, for A/B

    @property
    def provenance_sensitive(self) -> bool:
        return self.reason in PROVENANCE_SENSITIVE_REASONS

    def fingerprint(self) -> str:
        """Stable identity for acknowledgement, independent of line numbers."""
        payload = json.dumps(
            {"name": self.name, "origin": self.origin.value, "reason": self.reason.value},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        data = {
            "name": self.name,
            "raw_name": self.raw_name,
            "origin": self.origin.value,
            "verdict": self.verdict.value,
            "reason": self.reason.value,
            "detail": self.detail,
            "location": self.location,
            "fingerprint": self.fingerprint(),
        }
        if self.candidates:
            data["candidates"] = list(self.candidates)
        if self.confidence:
            data["confidence"] = self.confidence
        if self.provenance is not None:
            data["provenance"] = self.provenance.to_dict()
        if self.baseline_verdict is not None:
            data["baseline_verdict"] = self.baseline_verdict.value
        return data


@dataclass
class BandCounts:
    """The numbers that validate or kill the thesis."""

    total: int = 0
    hard_absent: int = 0
    provenance_sensitive: int = 0
    first_party_suppressed: int = 0
    private_index_suppressed: int = 0
    unresolvable_imports: int = 0
    ambiguous_imports: int = 0
    acknowledged: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckResult:
    findings: list[Finding] = field(default_factory=list)
    bands: BandCounts = field(default_factory=BandCounts)
    warnings: list[str] = field(default_factory=list)
    scanned_files: int = 0
    index_project_count: int = 0
    index_age_days: float | None = None
    provenance_source: str = "none"

    @property
    def worst(self) -> Verdict:
        if not self.findings:
            return Verdict.OK
        return max((f.verdict for f in self.findings), key=lambda v: v.rank)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict is Verdict.BLOCK]

    def exit_code(self) -> int:
        return 1 if self.blocking else 0

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "worst_verdict": self.worst.value,
            "findings": [f.to_dict() for f in self.findings],
            "bands": self.bands.to_dict(),
            "warnings": self.warnings,
            "scanned_files": self.scanned_files,
            "index": {
                "project_count": self.index_project_count,
                "age_days": self.index_age_days,
            },
            "provenance_source": self.provenance_source,
        }
