"""Map facts to a verdict. Missing + not ours = block. Imports never block."""

from __future__ import annotations

from dataclasses import dataclass

from provctl.model import (
    Finding,
    Origin,
    ProvenanceSummary,
    Reason,
    Verdict,
)
from provctl.policy.config import Policy


@dataclass
class MetadataSignals:
    """Enrichment inputs for the suspicious band. All optional."""

    age_days: float | None = None
    widely_adopted: bool | None = None
    has_repository_url: bool | None = None

    def is_suspicious(self, *, new_package_days: int, use_adoption: bool) -> tuple[bool, list[str]]:
        """Suspicion requires *corroboration*, never a single weak signal."""
        reasons: list[str] = []
        recently_created = self.age_days is not None and self.age_days <= new_package_days
        if recently_created:
            reasons.append(f"first released {self.age_days:.0f} days ago")

        low_adoption = use_adoption and self.widely_adopted is False
        if low_adoption:
            reasons.append("not in the top-15k downloaded packages")

        if self.has_repository_url is False:
            reasons.append("no source repository link")

        # need recency; otherwise this flags every niche package
        return (recently_created and len(reasons) >= 2), reasons


def evaluate_manifest_entry(
    *,
    name: str,
    raw_name: str,
    location: str,
    exists: bool,
    is_first_party: bool,
    private_index: bool,
    is_url_requirement: bool,
    signals: MetadataSignals | None,
    policy: Policy,
) -> Finding:
    """Verdict for a declared dependency. The only path that may block."""
    if is_url_requirement:
        return Finding(
            name=name, raw_name=raw_name, origin=Origin.MANIFEST,
            verdict=Verdict.INFO, reason=Reason.DIRECT_URL_REQUIREMENT,
            detail="installed from a direct URL or path, not resolved by name",
            location=location,
        )

    if not exists:
        if is_first_party:
            return Finding(
                name=name, raw_name=raw_name, origin=Origin.MANIFEST,
                verdict=Verdict.INFO, reason=Reason.ABSENT_BUT_FIRST_PARTY,
                detail="not on PyPI, but matches this repository's own packages",
                location=location,
            )
        if private_index:
            return Finding(
                name=name, raw_name=raw_name, origin=Origin.MANIFEST,
                verdict=Verdict.WARN, reason=Reason.ABSENT_BUT_PRIVATE_INDEX,
                detail=(
                    "not on public PyPI; a private index is configured so this "
                    "cannot be verified without credentials"
                ),
                location=location,
            )
        return Finding(
            name=name, raw_name=raw_name, origin=Origin.MANIFEST,
            verdict=Verdict.BLOCK, reason=Reason.ABSENT_FROM_INDEX,
            detail="no project by this name exists on PyPI",
            location=location,
        )

    if signals is not None:
        suspicious, reasons = signals.is_suspicious(
            new_package_days=policy.new_package_days,
            use_adoption=policy.use_adoption_signal,
        )
        if suspicious:
            return Finding(
                name=name, raw_name=raw_name, origin=Origin.MANIFEST,
                verdict=Verdict.WARN, reason=Reason.SUSPICIOUS_METADATA,
                detail="; ".join(reasons), location=location,
            )

    return Finding(
        name=name, raw_name=raw_name, origin=Origin.MANIFEST,
        verdict=Verdict.OK, reason=Reason.PRESENT, location=location,
    )


def evaluate_import(
    *,
    module: str,
    candidates: tuple[str, ...],
    confidence: str,
    location: str,
    any_candidate_exists: bool,
    is_first_party: bool,
    private_index: bool,
    declared: bool,
) -> Finding | None:
    """Verdict for an inferred import. Never blocks; often stays silent."""
    if is_first_party or declared:
        return None

    if not candidates:
        return Finding(
            name=module, raw_name=module, origin=Origin.IMPORT,
            verdict=Verdict.WARN, reason=Reason.ABSENT_UNRESOLVABLE_IMPORT,
            detail="could not map this import to any distribution",
            location=location, confidence=confidence,
        )

    if len(candidates) > 1:
        return Finding(
            name=module, raw_name=module, origin=Origin.IMPORT,
            verdict=Verdict.WARN, reason=Reason.AMBIGUOUS_MAPPING,
            detail=(
                "several distributions provide this import: "
                + ", ".join(candidates)
                + " -- not guessing"
            ),
            location=location, candidates=candidates, confidence=confidence,
        )

    if any_candidate_exists:
        return None

    if private_index:
        return Finding(
            name=candidates[0], raw_name=module, origin=Origin.IMPORT,
            verdict=Verdict.INFO, reason=Reason.ABSENT_BUT_PRIVATE_INDEX,
            detail="not on public PyPI; a private index is configured",
            location=location, candidates=candidates, confidence=confidence,
        )

    return Finding(
        name=candidates[0], raw_name=module, origin=Origin.IMPORT,
        verdict=Verdict.WARN, reason=Reason.ABSENT_FROM_INDEX,
        detail=(
            f"imported but undeclared, and no PyPI project named "
            f"'{candidates[0]}' exists"
        ),
        location=location, candidates=candidates, confidence=confidence,
    )


def apply_provenance(finding: Finding, provenance: ProvenanceSummary, policy: Policy) -> Finding:
    """Phase 2 weighting. Only ever touches the suspicious band."""
    finding.provenance = provenance
    if finding.baseline_verdict is None:
        finding.baseline_verdict = finding.verdict

    if not policy.provenance_enabled:
        return finding
    if not finding.provenance_sensitive:
        return finding
    if provenance.state == "untracked":
        return finding

    if policy.provenance_mode == "deescalate":
        # Agent-authored and never reviewed by a human is the risky case: the
        # text went from model output to manifest without anyone reading it.
        if provenance.state == "agent" and not provenance.human_reviewed:
            finding.verdict = Verdict.BLOCK
            finding.detail += "; introduced by an unreviewed agent edit"
        elif provenance.human_reviewed or provenance.state == "known_human":
            finding.verdict = Verdict.WARN
            finding.detail += "; a human authored or reviewed this line"
    elif policy.provenance_mode == "escalate":
        if provenance.state == "agent" and not provenance.human_reviewed:
            finding.verdict = Verdict.BLOCK
            finding.detail += "; introduced by an unreviewed agent edit"

    return finding
