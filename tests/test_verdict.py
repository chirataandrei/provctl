"""Verdict rules, including the invariants that are security properties."""

from __future__ import annotations

import pytest

from provctl.model import ProvenanceSummary, Reason, Verdict
from provctl.policy.config import Policy
from provctl.policy.verdict import (
    MetadataSignals,
    apply_provenance,
    evaluate_import,
    evaluate_manifest_entry,
)


def manifest(**kwargs):
    base = dict(
        name="somepkg", raw_name="somepkg", location="requirements.txt:1",
        exists=True, is_first_party=False, private_index=False,
        is_url_requirement=False, signals=None, policy=Policy.default(),
    )
    base.update(kwargs)
    return evaluate_manifest_entry(**base)


class TestHardRule:
    def test_absent_package_blocks(self):
        finding = manifest(exists=False)
        assert finding.verdict is Verdict.BLOCK
        assert finding.reason is Reason.ABSENT_FROM_INDEX

    def test_absent_but_first_party_is_informational(self):
        finding = manifest(exists=False, is_first_party=True)
        assert finding.verdict is Verdict.INFO
        assert finding.reason is Reason.ABSENT_BUT_FIRST_PARTY

    def test_absent_with_private_index_warns_rather_than_blocks(self):
        """We cannot verify a private index, so we must not claim it is fake."""
        finding = manifest(exists=False, private_index=True)
        assert finding.verdict is Verdict.WARN
        assert finding.reason is Reason.ABSENT_BUT_PRIVATE_INDEX

    def test_present_package_is_ok(self):
        assert manifest(exists=True).verdict is Verdict.OK

    def test_direct_url_requirement_is_not_name_resolved(self):
        finding = manifest(exists=False, is_url_requirement=True)
        assert finding.verdict is Verdict.INFO
        assert finding.reason is Reason.DIRECT_URL_REQUIREMENT


class TestSuspiciousBand:
    def test_low_adoption_alone_is_not_suspicious(self):
        """Most legitimate niche packages are outside the top 15k."""
        signals = MetadataSignals(widely_adopted=False, age_days=900)
        finding = manifest(signals=signals)
        assert finding.verdict is Verdict.OK

    def test_new_and_unadopted_is_suspicious(self):
        signals = MetadataSignals(widely_adopted=False, age_days=3)
        finding = manifest(signals=signals)
        assert finding.verdict is Verdict.WARN
        assert finding.reason is Reason.SUSPICIOUS_METADATA

    def test_new_but_widely_adopted_is_not_suspicious(self):
        signals = MetadataSignals(widely_adopted=True, age_days=3)
        assert manifest(signals=signals).verdict is Verdict.OK

    def test_missing_age_data_cannot_trigger_the_band(self):
        """Phase 1 has no enrichment, so the band must be empty, not guessed."""
        signals = MetadataSignals(widely_adopted=False, age_days=None)
        assert manifest(signals=signals).verdict is Verdict.OK


class TestImportsNeverBlock:
    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(candidates=(), confidence="unknown", any_candidate_exists=False),
            dict(candidates=("a", "b"), confidence="ambiguous", any_candidate_exists=False),
            dict(candidates=("nope",), confidence="heuristic", any_candidate_exists=False),
        ],
    )
    def test_invariant_no_import_finding_can_block(self, kwargs):
        """INVARIANT: the import->distribution mapping is the least reliable
        link in the chain, so a mapping mistake must never stop a commit."""
        finding = evaluate_import(
            module="mod", location="a.py:1", is_first_party=False,
            private_index=False, declared=False, **kwargs,
        )
        assert finding is not None
        assert finding.verdict is not Verdict.BLOCK

    def test_ambiguous_import_lists_candidates(self):
        finding = evaluate_import(
            module="psycopg2", candidates=("psycopg2", "psycopg2-binary"),
            confidence="ambiguous", location="a.py:1", any_candidate_exists=True,
            is_first_party=False, private_index=False, declared=False,
        )
        assert finding.reason is Reason.AMBIGUOUS_MAPPING
        assert "psycopg2-binary" in finding.detail

    def test_first_party_import_is_silent(self):
        assert evaluate_import(
            module="internal_utils", candidates=("internal-utils",), confidence="heuristic",
            location="a.py:1", any_candidate_exists=False, is_first_party=True,
            private_index=False, declared=False,
        ) is None

    def test_already_declared_import_is_silent(self):
        """The manifest entry is the authoritative finding; do not double-report."""
        assert evaluate_import(
            module="requests", candidates=("requests",), confidence="installed",
            location="a.py:1", any_candidate_exists=True, is_first_party=False,
            private_index=False, declared=True,
        ) is None

    def test_existing_undeclared_import_is_silent(self):
        assert evaluate_import(
            module="requests", candidates=("requests",), confidence="installed",
            location="a.py:1", any_candidate_exists=True, is_first_party=False,
            private_index=False, declared=False,
        ) is None


class TestProvenanceWeighting:
    def _suspicious(self):
        return manifest(signals=MetadataSignals(widely_adopted=False, age_days=2))

    def test_invariant_provenance_never_unblocks_a_hard_block(self):
        """INVARIANT: provenance is self-reported and forgeable. If it could
        relax the existence rule, an attacker who influences the agent could
        unblock their own payload."""
        blocked = manifest(exists=False)
        after = apply_provenance(
            blocked,
            ProvenanceSummary(state="known_human", human_reviewed=True),
            Policy.default(),
        )
        assert after.verdict is Verdict.BLOCK

    def test_untracked_provenance_changes_nothing(self):
        """INVARIANT: no provenance means fall back to baseline, never guess."""
        finding = self._suspicious()
        baseline = finding.verdict
        after = apply_provenance(finding, ProvenanceSummary(state="untracked"), Policy.default())
        assert after.verdict is baseline

    def test_unreviewed_agent_edit_escalates_in_the_band(self):
        after = apply_provenance(
            self._suspicious(),
            ProvenanceSummary(state="agent", tool="claude", human_reviewed=False),
            Policy.default(),
        )
        assert after.verdict is Verdict.BLOCK
        assert after.baseline_verdict is Verdict.WARN

    def test_human_reviewed_agent_edit_stays_a_warning(self):
        after = apply_provenance(
            self._suspicious(),
            ProvenanceSummary(state="agent", tool="claude", human_reviewed=True),
            Policy.default(),
        )
        assert after.verdict is Verdict.WARN

    def test_disabled_provenance_is_the_phase1_baseline(self):
        policy = Policy.default()
        policy.provenance_enabled = False
        finding = self._suspicious()
        baseline = finding.verdict
        after = apply_provenance(
            finding, ProvenanceSummary(state="agent", human_reviewed=False), policy
        )
        assert after.verdict is baseline

    def test_baseline_verdict_recorded_for_ab_measurement(self):
        after = apply_provenance(
            self._suspicious(),
            ProvenanceSummary(state="agent", human_reviewed=False),
            Policy.default(),
        )
        assert after.baseline_verdict is not None
        assert after.baseline_verdict != after.verdict
