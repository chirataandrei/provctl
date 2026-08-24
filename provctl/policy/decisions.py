"""Append-only ack log in .provenance/decisions.jsonl."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from provctl.model import Finding

DECISIONS_SCHEMA_VERSION = 1

_GITATTRIBUTES_LINE = ".provenance/decisions.jsonl merge=union"


@dataclass(frozen=True)
class Decision:
    schema_version: int
    fingerprint: str
    name: str
    origin: str
    reason: str
    verdict: str
    actor: str
    timestamp: str
    note: str = ""
    provenance: dict | None = None

    def to_json(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "fingerprint": self.fingerprint,
            "name": self.name,
            "origin": self.origin,
            "reason": self.reason,
            "verdict": self.verdict,
            "actor": self.actor,
            "timestamp": self.timestamp,
        }
        if self.note:
            payload["note"] = self.note
        if self.provenance:
            payload["provenance"] = self.provenance
        # sort_keys so a line is byte-identical wherever it is produced, which
        # keeps union merges from creating near-duplicate lines.
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> "Decision | None":
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict) or "fingerprint" not in raw:
            return None
        # Unknown fields are ignored rather than rejected: the stability
        # guarantee is that fields may be added, never repurposed.
        return cls(
            schema_version=raw.get("schema_version", 0),
            fingerprint=raw["fingerprint"],
            name=raw.get("name", ""),
            origin=raw.get("origin", ""),
            reason=raw.get("reason", ""),
            verdict=raw.get("verdict", ""),
            actor=raw.get("actor", ""),
            timestamp=raw.get("timestamp", ""),
            note=raw.get("note", ""),
            provenance=raw.get("provenance"),
        )


class DecisionLog:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[Decision]:
        if not self.path.exists():
            return []
        decisions = []
        try:
            content = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        for line in content.splitlines():
            line = line.strip()
            # Union merges can leave conflict markers if the driver is not
            # registered. Skip them rather than failing the whole run.
            if not line or line.startswith(("<<<<<<<", "=======", ">>>>>>>")):
                continue
            decision = Decision.from_json(line)
            if decision is not None:
                decisions.append(decision)
        return decisions

    def acknowledged_fingerprints(self) -> set[str]:
        return {d.fingerprint for d in self.load()}

    def acknowledge(
        self, finding: Finding, *, note: str = "", actor: str | None = None
    ) -> Decision:
        decision = Decision(
            schema_version=DECISIONS_SCHEMA_VERSION,
            fingerprint=finding.fingerprint(),
            name=finding.name,
            origin=finding.origin.value,
            reason=finding.reason.value,
            verdict=finding.verdict.value,
            actor=actor or _git_identity(self.path.parent),
            timestamp=datetime.now(timezone.utc).isoformat(),
            note=note,
            provenance=finding.provenance.to_dict() if finding.provenance else None,
        )
        self.append(decision)
        return decision

    def append(self, decision: Decision) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Append in one write with O_APPEND so two concurrent processes cannot
        # interleave partial lines.
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(decision.to_json() + "\n")

    def stats(self) -> dict:
        """Adoption health, per the plan's own metric."""
        decisions = self.load()
        by_reason: dict[str, int] = {}
        for decision in decisions:
            by_reason[decision.reason] = by_reason.get(decision.reason, 0) + 1
        return {
            "total_acknowledgements": len(decisions),
            "distinct_findings": len({d.fingerprint for d in decisions}),
            "by_reason": by_reason,
        }


def _git_identity(cwd: Path) -> str:
    for key in ("user.email", "user.name"):
        try:
            proc = subprocess.run(
                ["git", "config", "--get", key], cwd=str(cwd),
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
    return os.environ.get("USER", "unknown")


def ensure_merge_driver(repo_root: Path) -> bool:
    """Register the union merge driver in `.gitattributes`."""
    attributes = repo_root / ".gitattributes"
    existing = ""
    if attributes.exists():
        try:
            existing = attributes.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    if "decisions.jsonl" in existing:
        return False

    prefix = "" if (not existing or existing.endswith("\n")) else "\n"
    with open(attributes, "a", encoding="utf-8") as handle:
        handle.write(
            f"{prefix}# provctl: union-merge the ack log\n"
            f"{_GITATTRIBUTES_LINE}\n"
        )
    return True
