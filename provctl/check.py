"""Changed files in, findings out."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from provctl import gitio
from provctl.enrich.pypi import MetadataCache
from provctl.index import adoption as adoption_mod
from provctl.index.refresh import is_stale, staleness
from provctl.index.store import IndexMissingError, IndexStore, NameIndex
from provctl.model import (
    BandCounts,
    CheckResult,
    Finding,
    Origin,
    Reason,
    Verdict,
)
from provctl.paths import decisions_path
from provctl.policy import verdict as verdict_mod
from provctl.policy.config import Policy
from provctl.policy.decisions import DecisionLog
from provctl.provenance.gitai import GitAiAdapter
from provctl.provenance.hook import HookProvenanceReader
from provctl.provenance.model import UNTRACKED, ProvenanceMap
from provctl.policy.verdict import MetadataSignals
from provctl.resolve import firstparty, imports as imports_mod, manifests
from provctl.resolve.distmap import DistributionResolver


@dataclass
class CheckOptions:
    scope: str = "staged"          # staged | worktree | all | range
    rev_range: str | None = None
    use_provenance: bool = True
    enrich: bool = False           # Phase 2 metadata lookup (off the hot path)
    include_imports: bool = True


class Checker:
    def __init__(
        self,
        repo: Path,
        policy: Policy,
        store: IndexStore,
        *,
        resolver: DistributionResolver | None = None,
        adoption: adoption_mod.AdoptionSet | None = None,
        metadata_cache: MetadataCache | None = None,
    ):
        self.repo = repo
        self.policy = policy
        self.store = store
        self.resolver = resolver or DistributionResolver(overrides=policy.mapping_overrides)
        self.adoption = adoption if adoption is not None else adoption_mod.load(store)
        self._metadata_cache = metadata_cache
        self._owns_cache = False
        if metadata_cache is None:
            # Missing cache just means no age signal.
            cache_path = store.cache_dir / "metadata.sqlite3"
            if cache_path.exists():
                try:
                    self._metadata_cache = MetadataCache(cache_path)
                    self._owns_cache = True
                except Exception:
                    self._metadata_cache = None

    def close(self) -> None:
        if self._owns_cache and self._metadata_cache is not None:
            self._metadata_cache.close()
            self._metadata_cache = None


    def run(self, options: CheckOptions) -> CheckResult:
        result = CheckResult()

        changes = self._collect_changes(options, result)
        if changes is None:
            return result

        index = self.store.open_index()
        meta = self.store.read_meta()
        if meta is not None:
            result.index_project_count = meta.project_count
            age = staleness(meta)
            result.index_age_days = round(age.total_seconds() / 86400, 2) if age else None
            if is_stale(meta):
                result.warnings.append(
                    "local PyPI index is more than 7 days old; "
                    "run `provctl index refresh` (checks still ran)"
                )

        try:
            self._probe_index(index, result)
        except IndexMissingError as exc:
            result.warnings.append(str(exc))
            return result

        manifest_scan = self._scan_manifests(changes, options)
        first_party = self._build_first_party(manifest_scan)
        private_index = self._detect_private_index(manifest_scan)

        declared = {entry.name for entry in manifest_scan.entries}

        findings: list[Finding] = []
        findings.extend(
            self._check_manifest_entries(manifest_scan, index, first_party, private_index, changes)
        )
        if options.include_imports:
            findings.extend(
                self._check_imports(changes, options, index, first_party, private_index, declared, result)
            )

        findings = self._deduplicate(findings)
        findings = self._apply_provenance(findings, options, result)
        findings = self._apply_acknowledgements(findings)
        result.findings = findings
        result.warnings.extend(manifest_scan.parse_errors)
        result.warnings.extend(self.policy.warnings)
        result.bands = self._count_bands(findings)
        index.close()
        return result


    def _collect_changes(self, options: CheckOptions, result: CheckResult):
        # Don't let git dump usage text into a commit hook.
        if not gitio.is_git_repo(self.repo):
            result.warnings.append(f"{self.repo} is not a git repository")
            return None
        try:
            if options.scope == "staged":
                return gitio.staged_changes(self.repo)
            if options.scope == "worktree":
                return gitio.worktree_changes(self.repo)
            if options.scope == "range" and options.rev_range:
                return gitio.range_changes(self.repo, options.rev_range)
            return gitio.all_tracked_files(self.repo)
        except gitio.GitError as exc:
            result.warnings.append(f"could not read git changes: {exc}")
            return None

    @staticmethod
    def _probe_index(index: NameIndex, result: CheckResult) -> None:
        # Open the mmap once so a missing index is one warning, not N.
        index.contains_normalized("pip")

    def _scan_manifests(self, changes, options: CheckOptions) -> manifests.ManifestScan:
        merged = manifests.ManifestScan()
        staged = options.scope == "staged"

        for rel in changes.files:
            if not manifests.is_manifest(rel):
                continue
            text = gitio.file_content(self.repo, rel, staged=staged)
            if text is None:
                continue
            sub = manifests.parse_manifest(rel, text)

            # requirements.txt: only added lines. TOML has no reliable line
            # numbers, so we re-check the whole file.
            if not changes.is_fully_new(rel):
                added = changes.lines_for(rel)
                if added and rel.endswith((".txt", ".in")):
                    sub.entries = [e for e in sub.entries if e.lineno in added]

            merged.entries.extend(sub.entries)
            merged.index_urls.extend(sub.index_urls)
            merged.parse_errors.extend(sub.parse_errors)
            merged.url_requirements |= sub.url_requirements
            merged.workspace_members |= sub.workspace_members
            if sub.project_name and not merged.project_name:
                merged.project_name = sub.project_name

        # Root pyproject still names first-party packages if it wasn't edited.
        if merged.project_name is None:
            root_pyproject = self.repo / "pyproject.toml"
            if root_pyproject.exists():
                try:
                    scan = manifests.parse_pyproject(
                        root_pyproject.read_text(encoding="utf-8", errors="replace"),
                        "pyproject.toml",
                    )
                    merged.project_name = scan.project_name
                    merged.index_urls.extend(scan.index_urls)
                    merged.url_requirements |= scan.url_requirements
                    merged.workspace_members |= scan.workspace_members
                except OSError:
                    pass
        return merged

    def _build_first_party(self, scan: manifests.ManifestScan) -> firstparty.FirstPartyIndex:
        return firstparty.discover(
            self.repo,
            project_name=scan.project_name,
            prefixes=self.policy.first_party_prefixes,
            allowlist=self.policy.first_party_allow,
        )

    def _detect_private_index(self, scan: manifests.ManifestScan) -> bool:
        if self.policy.private_index:
            return True
        if scan.has_private_index:
            return True
        env_urls = manifests.environment_index_urls(dict(os.environ))
        return any(not manifests._is_public_pypi(url) for url in env_urls)

    def _check_manifest_entries(
        self, scan, index: NameIndex, first_party, private_index: bool, changes
    ) -> list[Finding]:
        findings = []
        seen: set[str] = set()
        for entry in scan.entries:
            if entry.name in seen:
                continue
            seen.add(entry.name)

            exists = index.contains_normalized(entry.name)
            finding = verdict_mod.evaluate_manifest_entry(
                name=entry.name,
                raw_name=entry.raw_name,
                location=f"{entry.manifest}:{entry.lineno}" if entry.lineno else entry.manifest,
                exists=exists,
                is_first_party=(
                    first_party.is_first_party_dist(entry.name)
                    or entry.name in scan.workspace_members
                ),
                private_index=private_index,
                is_url_requirement=entry.is_url or entry.name in scan.url_requirements,
                signals=self._signals_for(entry.name) if exists else None,
                policy=self.policy,
            )
            findings.append(finding)
        return findings

    def _check_imports(
        self, changes, options, index, first_party, private_index, declared, result
    ) -> list[Finding]:
        findings = []
        staged = options.scope == "staged"
        seen: set[str] = set()
        siblings = firstparty.LocalSiblingResolver(self.repo)

        for rel in changes.files:
            if not imports_mod.is_python_file(rel):
                continue
            text = gitio.file_content(self.repo, rel, staged=staged)
            if text is None:
                continue
            result.scanned_files += 1

            sites = imports_mod.extract_third_party(text, rel)
            if not changes.is_fully_new(rel):
                added = changes.lines_for(rel)
                if added:
                    sites = [s for s in sites if any(n in added for n in s.lines)]

            for site in sites:
                if site.module in seen:
                    continue
                # Sibling modules are first-party; skip the index lookup.
                if siblings.resolves(rel, site.module):
                    continue
                seen.add(site.module)

                resolution = self.resolver.resolve(site.module)
                any_exists = any(
                    index.contains_normalized(c) for c in resolution.candidates
                )
                declared_here = any(c in declared for c in resolution.candidates)

                finding = verdict_mod.evaluate_import(
                    module=site.module,
                    candidates=resolution.candidates,
                    confidence=resolution.confidence.value,
                    location=f"{rel}:{site.lineno}",
                    any_candidate_exists=any_exists,
                    is_first_party=first_party.is_first_party_module(site.module),
                    private_index=private_index,
                    declared=declared_here,
                )
                if finding is not None:
                    findings.append(finding)
        return findings

    def _signals_for(self, name: str) -> MetadataSignals | None:
        """Age/adoption signals from the local cache only. Miss = no signal."""
        widely_adopted = None
        if self.policy.use_adoption_signal and len(self.adoption):
            widely_adopted = self.adoption.is_widely_adopted(name)

        age_days = None
        if self._metadata_cache is not None:
            meta = self._metadata_cache.get(name)
            if meta is not None:
                age_days = meta.age_days()

        return MetadataSignals(widely_adopted=widely_adopted, age_days=age_days)

    def _load_provenance(self) -> ProvenanceMap:
        """Hook first (per-line, uncommitted); git-ai only as fallback."""
        git_dir = self.repo / ".git"
        hook_map = HookProvenanceReader(git_dir).load()
        if not hook_map.is_empty:
            return hook_map

        adapter = GitAiAdapter(self.repo)
        if adapter.available:
            return adapter.committed_provenance()
        return ProvenanceMap()

    def _apply_provenance(
        self, findings: list[Finding], options: CheckOptions, result: CheckResult
    ) -> list[Finding]:
        """Apply provenance only inside the suspicious band."""
        if not options.use_provenance or not self.policy.provenance_enabled:
            result.provenance_source = "disabled"
            for finding in findings:
                finding.baseline_verdict = finding.verdict
            return findings

        provenance = self._load_provenance()
        result.provenance_source = provenance.source or "none"

        for finding in findings:
            summary = UNTRACKED
            if finding.location and ":" in finding.location:
                path, _, lineno = finding.location.rpartition(":")
                if lineno.isdigit():
                    summary = provenance.summarize(path, int(lineno))
            verdict_mod.apply_provenance(finding, summary, self.policy)
        return findings

    def _apply_acknowledgements(self, findings: list[Finding]) -> list[Finding]:
        """Drop findings already acked. Fingerprint ignores line numbers."""
        log = DecisionLog(decisions_path(self.repo))
        acknowledged = log.acknowledged_fingerprints()
        if not acknowledged:
            return findings

        for finding in findings:
            if finding.fingerprint() in acknowledged and finding.verdict is not Verdict.OK:
                finding.verdict = Verdict.INFO
                finding.reason = Reason.ACKNOWLEDGED
                finding.detail = (
                    f"previously acknowledged; original finding: {finding.detail}"
                    if finding.detail else "previously acknowledged"
                )
        return findings

    @staticmethod
    def _deduplicate(findings: list[Finding]) -> list[Finding]:
        """Keep the worst finding per (name, origin)."""
        best: dict[tuple[str, str], Finding] = {}
        for finding in findings:
            key = (finding.name, finding.origin.value)
            current = best.get(key)
            if current is None or finding.verdict.rank > current.verdict.rank:
                best[key] = finding

        # Manifest finding already covers this name.
        manifest_names = {
            f.name for f in best.values() if f.origin is Origin.MANIFEST
        }
        ordered = [
            f for f in best.values()
            if not (f.origin is Origin.IMPORT and f.name in manifest_names)
        ]
        ordered.sort(key=lambda f: (-f.verdict.rank, f.origin.value, f.name))
        return ordered

    @staticmethod
    def _count_bands(findings: list[Finding]) -> BandCounts:
        bands = BandCounts(total=len(findings))
        for finding in findings:
            if finding.reason is Reason.ABSENT_FROM_INDEX and finding.verdict is Verdict.BLOCK:
                bands.hard_absent += 1
            elif finding.reason is Reason.ABSENT_BUT_FIRST_PARTY:
                bands.first_party_suppressed += 1
            elif finding.reason is Reason.ABSENT_BUT_PRIVATE_INDEX:
                bands.private_index_suppressed += 1
            elif finding.reason is Reason.ABSENT_UNRESOLVABLE_IMPORT:
                bands.unresolvable_imports += 1
            elif finding.reason is Reason.AMBIGUOUS_MAPPING:
                bands.ambiguous_imports += 1
            elif finding.reason is Reason.ACKNOWLEDGED:
                bands.acknowledged += 1
            if finding.provenance_sensitive:
                bands.provenance_sensitive += 1
        return bands
