"""Run provctl across the corpus and report the numbers that decide the project."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from measurements.corpus import CORPUS, Repo
from provctl.check import Checker, CheckOptions
from provctl.index.store import IndexStore
from provctl.model import Origin, Reason, Verdict
from provctl.paths import cache_dir
from provctl.policy.config import Policy
from provctl.resolve.distmap import DistributionResolver

WORK_DIR = Path(".work/corpus")
OUT_DIR = Path("measurements/out")

CLONE_TIMEOUT = 600


@dataclass
class RepoResult:
    name: str
    ok: bool = True
    error: str = ""
    duration_s: float = 0.0
    total_findings: int = 0
    blocks: list[dict] = field(default_factory=list)
    warns: list[dict] = field(default_factory=list)
    bands: dict = field(default_factory=dict)
    reason_counts: dict = field(default_factory=dict)
    # Distributions that do exist on PyPI. These are the only candidates for
    # the provenance-sensitive band, and the only names it is safe to enrich
    # over the network (they are public by definition).
    present_names: list[str] = field(default_factory=list)


def clone(repo: Repo, dest: Path) -> bool:
    if (dest / ".git").exists():
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  cloning {repo.name} ...", flush=True)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", repo.url, str(dest)],
            check=True, capture_output=True, timeout=CLONE_TIMEOUT,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"    failed: {exc}", file=sys.stderr)
        return False


def measure_repo(repo: Repo, path: Path, store: IndexStore) -> RepoResult:
    result = RepoResult(name=repo.name)
    started = time.time()
    try:
        checker = Checker(
            path,
            Policy.load(path / ".provenance" / "policy.toml"),
            store,
            # Empty installed map: the corpus repos are not installed in our
            # environment, so relying on it would silently change results
            # depending on the runner's venv.
            resolver=DistributionResolver(installed={}),
        )
        # `all` scope: the question is "what would provctl say about this
        # repository as it stands", not "what changed in the last commit".
        check = checker.run(CheckOptions(scope="all"))
    except Exception as exc:  # noqa: BLE001 - a harness must not die on one repo
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    result.duration_s = round(time.time() - started, 2)
    result.total_findings = len(check.findings)
    result.bands = check.bands.to_dict()
    result.reason_counts = dict(Counter(f.reason.value for f in check.findings))
    result.blocks = [
        {"name": f.name, "reason": f.reason.value, "location": f.location,
         "origin": f.origin.value}
        for f in check.findings if f.verdict is Verdict.BLOCK
    ]
    result.warns = [
        {"name": f.name, "reason": f.reason.value, "origin": f.origin.value}
        for f in check.findings if f.verdict is Verdict.WARN
    ]
    result.present_names = sorted(
        {
            f.name for f in check.findings
            if f.reason is Reason.PRESENT and f.origin is Origin.MANIFEST
        }
    )
    return result


def aggregate(results: list[RepoResult]) -> dict:
    usable = [r for r in results if r.ok]
    total_findings = sum(r.total_findings for r in usable)
    total_blocks = sum(len(r.blocks) for r in usable)
    total_warns = sum(len(r.warns) for r in usable)
    sensitive = sum(r.bands.get("provenance_sensitive", 0) for r in usable)
    repos_with_blocks = sum(1 for r in usable if r.blocks)

    band_totals: Counter = Counter()
    reason_totals: Counter = Counter()
    for r in usable:
        band_totals.update(r.bands)
        reason_totals.update(r.reason_counts)

    return {
        "repos_measured": len(usable),
        "repos_failed": len(results) - len(usable),
        "total_findings": total_findings,
        "total_blocks": total_blocks,
        "total_warns": total_warns,
        "repos_with_at_least_one_block": repos_with_blocks,
        "clean_repo_rate": round(1 - repos_with_blocks / max(len(usable), 1), 4),
        "provenance_sensitive": sensitive,
        "provenance_sensitive_share_of_findings": round(
            sensitive / total_findings, 6
        ) if total_findings else 0.0,
        "bands": dict(band_totals),
        "reasons": dict(reason_totals),
    }


def print_report(results: list[RepoResult], summary: dict) -> None:
    print("\n" + "=" * 72)
    print("BASELINE MEASUREMENT (provenance-blind, Phase 1)")
    print("=" * 72)
    print(f"repos measured                 {summary['repos_measured']}")
    print(f"repos failed                   {summary['repos_failed']}")
    print(f"total findings                 {summary['total_findings']}")
    print(f"total BLOCKs                   {summary['total_blocks']}")
    print(f"total WARNs                    {summary['total_warns']}")
    print(f"repos with >=1 block           {summary['repos_with_at_least_one_block']}"
          f" / {summary['repos_measured']}")
    print(f"repos passing cleanly          {summary['clean_repo_rate'] * 100:.1f}%")

    print("\n" + "-" * 72)
    print("QUESTION 1: is the provenance band big enough to matter?")
    print("-" * 72)
    share = summary["provenance_sensitive_share_of_findings"]
    print(f"provenance-sensitive findings  {summary['provenance_sensitive']}"
          f"  ({share * 100:.2f}% of all findings)")
    if summary["provenance_sensitive"] == 0:
        print("\n  Zero. On this corpus, provenance could not have changed a single")
        print("  verdict. Phase 1 ships no metadata enrichment, so the suspicious")
        print("  band is empty by construction -- this measures the *ceiling* only")
        print("  after enrichment lands. What it already shows is that every")
        print("  finding here is decided by rules provenance never touches.")

    print("\n" + "-" * 72)
    print("QUESTION 2: baseline false-positive rate")
    print("-" * 72)
    print("Every block below is a false positive: these repos are presumed clean.")
    for result in results:
        if result.blocks:
            print(f"\n  {result.name}: {len(result.blocks)} block(s)")
            for block in result.blocks[:10]:
                print(f"    - {block['name']}  [{block['reason']}] {block['location']}")
            if len(result.blocks) > 10:
                print(f"    ... and {len(result.blocks) - 10} more")

    print("\n" + "-" * 72)
    print("Where findings come from")
    print("-" * 72)
    for reason, count in sorted(summary["reasons"].items(), key=lambda kv: -kv[1]):
        print(f"  {reason:32s} {count}")

    failed = [r for r in results if not r.ok]
    if failed:
        print("\nFailures:")
        for result in failed:
            print(f"  {result.name}: {result.error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clone", action="store_true", help="clone missing repos first")
    parser.add_argument("--work-dir", default=str(WORK_DIR))
    parser.add_argument("--limit", type=int, help="only measure the first N repos")
    parser.add_argument("--json-out", default=str(OUT_DIR / "baseline.json"))
    args = parser.parse_args(argv)

    work = Path(args.work_dir)
    store = IndexStore(cache_dir())
    if not store.exists():
        print("error: no local index. Run `provctl index refresh` first.", file=sys.stderr)
        return 2

    repos = list(CORPUS)[: args.limit] if args.limit else list(CORPUS)

    if args.clone:
        print(f"Cloning {len(repos)} repositories into {work} ...")
        for repo in repos:
            clone(repo, work / repo.name)

    results: list[RepoResult] = []
    for repo in repos:
        path = work / repo.name
        if not (path / ".git").exists():
            print(f"  skipping {repo.name} (not cloned)")
            continue
        print(f"  measuring {repo.name} ...", flush=True)
        results.append(measure_repo(repo, path, store))

    if not results:
        print("No repositories measured. Run with --clone first.", file=sys.stderr)
        return 1

    summary = aggregate(results)
    print_report(results, summary)

    out_path = Path(args.json_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {"summary": summary, "repos": [r.__dict__ for r in results]},
            indent=2, sort_keys=False,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
