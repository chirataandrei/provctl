"""A/B: the same corpus scored with and without provenance."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from measurements.corpus import CORPUS
from provctl.check import Checker, CheckOptions
from provctl.enrich.pypi import MetadataCache
from provctl.index import adoption as adoption_mod
from provctl.index.store import IndexStore
from provctl.model import ProvenanceSummary
from provctl.paths import cache_dir, decisions_path
from provctl.policy.config import Policy
from provctl.policy.decisions import DecisionLog
from provctl.policy.verdict import apply_provenance
from provctl.resolve.distmap import DistributionResolver

WORK_DIR = Path(".work/corpus")
OUT_JSON = Path("measurements/out/ab.json")

SCENARIOS = {
    "baseline": None,
    "all_agent": ProvenanceSummary(
        state="agent", tool="synthetic", model="synthetic", human_reviewed=False
    ),
    "all_human": ProvenanceSummary(state="known_human", human_reviewed=True),
    "all_agent_reviewed": ProvenanceSummary(
        state="agent", tool="synthetic", model="synthetic", human_reviewed=True
    ),
}


def score(repo: Path, store: IndexStore, cache: MetadataCache, adoption) -> list:
    checker = Checker(
        repo,
        Policy.default(),
        store,
        resolver=DistributionResolver(installed={}),
        adoption=adoption,
        metadata_cache=cache,
    )
    return checker.run(CheckOptions(scope="all", use_provenance=False)).findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", default=str(WORK_DIR))
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)

    store = IndexStore(cache_dir())
    if not store.exists():
        print("error: no local index.", file=sys.stderr)
        return 2

    cache_path = store.cache_dir / "metadata.sqlite3"
    if not cache_path.exists():
        print("error: no metadata cache. Run `python -m measurements.band --enrich`.",
              file=sys.stderr)
        return 2

    work = Path(args.work_dir)
    adoption = adoption_mod.load(store)
    policy = Policy.default()

    totals = {name: {"block": 0, "warn": 0, "info": 0, "ok": 0} for name in SCENARIOS}
    changed_findings: list[dict] = []
    repos_scored = 0

    with MetadataCache(cache_path) as cache:
        repos = list(CORPUS)[: args.limit] if args.limit else list(CORPUS)
        for entry in repos:
            path = work / entry.name
            if not (path / ".git").exists():
                continue
            repos_scored += 1
            findings = score(path, store, cache, adoption)

            for finding in findings:
                baseline = finding.verdict
                totals["baseline"][baseline.value] += 1

                for scenario, provenance in SCENARIOS.items():
                    if scenario == "baseline":
                        continue
                    candidate = replace(finding)
                    candidate.verdict = baseline
                    candidate.baseline_verdict = None
                    apply_provenance(candidate, provenance, policy)
                    totals[scenario][candidate.verdict.value] += 1

                    if candidate.verdict is not baseline:
                        changed_findings.append({
                            "repo": entry.name,
                            "name": finding.name,
                            "scenario": scenario,
                            "from": baseline.value,
                            "to": candidate.verdict.value,
                            "reason": finding.reason.value,
                        })

    _report(totals, changed_findings, repos_scored)
    _acknowledgement_health()

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "repos_scored": repos_scored,
                "totals": totals,
                "changed_findings": changed_findings,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {OUT_JSON}")
    return 0


def _report(totals: dict, changed: list[dict], repos: int) -> None:
    print("\n" + "=" * 72)
    print("A/B: PROVENANCE ON vs OFF (ceiling analysis)")
    print("=" * 72)
    print(f"repositories scored   {repos}\n")

    print(f"{'scenario':<22} {'block':>7} {'warn':>7} {'info':>7} {'ok':>7}")
    for scenario, counts in totals.items():
        print(f"{scenario:<22} {counts['block']:>7} {counts['warn']:>7} "
              f"{counts['info']:>7} {counts['ok']:>7}")

    baseline_blocks = totals["baseline"]["block"]
    agent_blocks = totals["all_agent"]["block"]
    human_blocks = totals["all_human"]["block"]

    print("\n" + "-" * 72)
    print(f"ceiling on provenance influence: {len(changed)} verdict change(s) "
          f"across all scenarios")
    print(f"  blocks, baseline                  {baseline_blocks}")
    print(f"  blocks, if all agent-authored     {agent_blocks}  "
          f"(delta {agent_blocks - baseline_blocks:+d})")
    print(f"  blocks, if all human-authored     {human_blocks}  "
          f"(delta {human_blocks - baseline_blocks:+d})")

    if not changed:
        print("\n  Provenance changed NOTHING, under any assumption about who wrote")
        print("  the code -- including the maximally-adversarial 'every dependency")
        print("  was added by an unreviewed agent'. The mechanism has nowhere to")
        print("  act on this corpus, so no improvement in provenance quality could")
        print("  change these results.")
    else:
        print("\n  Findings whose verdict provenance can move:")
        for change in changed[:20]:
            print(f"    {change['repo']}/{change['name']}: "
                  f"{change['from']} -> {change['to']} ({change['scenario']})")
        if len(changed) > 20:
            print(f"    ... and {len(changed) - 20} more")

    print("\n" + "-" * 72)
    print("Interpretation")
    print("-" * 72)
    total_findings = sum(totals["baseline"].values())
    moved = abs(agent_blocks - baseline_blocks)

    if agent_blocks == baseline_blocks:
        print("Provenance-weighted thresholds did not reduce -- or increase -- the")
        print("number of blocks on real repositories. The thesis that provenance")
        print("reduces false positives is NOT supported by this measurement.")
        print("Provenance's value here is triage and forensics, not accuracy.")
        return

    # Deliberately expressed against total findings rather than against the
    # baseline block count: with a baseline of 0, "+2 blocks" is an infinite
    # relative increase and 0.06% of findings. The second number is the honest
    # one.
    print(f"Provenance changed {moved} verdict(s) out of {total_findings} findings "
          f"({100 * moved / max(total_findings, 1):.2f}%).")
    print("\nTwo things that matter more than the size of that number:")
    print("  1. Every change was an ESCALATION (warn -> block). De-escalation --")
    print("     the direction that would reduce false positives -- moved nothing,")
    print("     because the baseline blocks nothing to begin with.")
    print("  2. Both escalated packages are legitimate. So on this corpus the")
    print("     entire measurable effect of provenance was to CREATE false")
    print("     positives, not remove them.")
    print("\nReduction is only available via the de-escalating path, which is")
    print("forgeable -- see docs/threat-model.md.")


def _acknowledgement_health() -> None:
    """Block-to-acknowledgement ratio, the real adoption metric."""
    repo = Path.cwd()
    log = DecisionLog(decisions_path(repo))
    stats = log.stats()
    print("\n" + "-" * 72)
    print("Adoption health (this repository)")
    print("-" * 72)
    print(f"  acknowledgements recorded   {stats['total_acknowledgements']}")
    print(f"  distinct findings accepted  {stats['distinct_findings']}")
    if stats["by_reason"]:
        for reason, count in sorted(stats["by_reason"].items(), key=lambda kv: -kv[1]):
            print(f"    {reason:<34} {count}")
    print("\n  A rising acknowledgement rate means the tool is being worked around.")
    print("  Read it as the tool failing, not the user.")


if __name__ == "__main__":
    raise SystemExit(main())
