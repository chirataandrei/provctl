"""Size the provenance-sensitive band. The single most important number here."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from provctl.enrich.pypi import MetadataCache, enrich
from provctl.index import adoption as adoption_mod
from provctl.index.store import IndexStore
from provctl.paths import cache_dir
from provctl.policy.verdict import MetadataSignals

BASELINE_JSON = Path("measurements/out/baseline.json")
OUT_JSON = Path("measurements/out/band.json")

# Sensitivity sweep: the band's size depends entirely on where "new" is drawn,
# so reporting a single threshold would be cherry-picking.
AGE_THRESHOLDS = (7, 30, 90, 180, 365)


def collect_present_names(baseline: dict) -> list[str]:
    """Every distribution the corpus depends on that does exist on PyPI."""
    names: set[str] = set()
    for repo in baseline["repos"]:
        names.update(repo.get("present_names", []))
    return sorted(names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enrich", action="store_true", help="fetch missing metadata first")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--baseline", default=str(BASELINE_JSON))
    args = parser.parse_args(argv)

    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        print(f"error: {baseline_path} missing. Run `python -m measurements.run` first.",
              file=sys.stderr)
        return 2
    baseline = json.loads(baseline_path.read_text())

    names = collect_present_names(baseline)
    if not names:
        print("error: baseline has no recorded present names; re-run measurements.run",
              file=sys.stderr)
        return 2

    store = IndexStore(cache_dir())
    if not store.exists():
        print("error: no local index.", file=sys.stderr)
        return 2

    index = store.open_index()
    cache = MetadataCache(store.cache_dir / "metadata.sqlite3")

    if args.enrich:
        print(f"enriching {len(names)} package(s) ...")
        def progress(done: int, total: int) -> None:
            print(f"  {done}/{total}", flush=True)
        fetched, skipped = enrich(names, cache, index, workers=args.workers, progress=progress)
        print(f"  fetched {fetched}, skipped {skipped} (absent from index / already cached)")

    adoption = adoption_mod.load(store)
    now = datetime.now(timezone.utc)

    resolved = 0
    no_metadata = 0
    ages: list[float] = []
    band_by_threshold: dict[int, int] = {t: 0 for t in AGE_THRESHOLDS}
    band_members: dict[int, list[str]] = {t: [] for t in AGE_THRESHOLDS}
    low_adoption = 0

    for name in names:
        meta = cache.get(name)
        if meta is None or meta.first_release is None:
            no_metadata += 1
            continue
        resolved += 1
        age = meta.age_days(now)
        ages.append(age)
        adopted = adoption.is_widely_adopted(name) if len(adoption) else None
        if adopted is False:
            low_adoption += 1

        for threshold in AGE_THRESHOLDS:
            signals = MetadataSignals(age_days=age, widely_adopted=adopted)
            suspicious, _ = signals.is_suspicious(
                new_package_days=threshold, use_adoption=True
            )
            if suspicious:
                band_by_threshold[threshold] += 1
                band_members[threshold].append(name)

    index.close()
    cache.close()

    print("\n" + "=" * 72)
    print("PROVENANCE BAND SIZE")
    print("=" * 72)
    print(f"distinct existing dependencies    {len(names)}")
    print(f"  with age metadata               {resolved}")
    print(f"  without (uncached / failed)     {no_metadata}")
    if ages:
        ages_sorted = sorted(ages)
        median = ages_sorted[len(ages_sorted) // 2]
        print(f"  median age                      {median / 365:.1f} years")
        print(f"  newer than 1 year               {sum(1 for a in ages if a < 365)}")
    print(f"  not in top-15k downloads        {low_adoption}")

    print("\nBand size by 'new package' threshold:")
    print("  (a finding is in the band only if it is BOTH recent AND low-adoption)")
    for threshold in AGE_THRESHOLDS:
        count = band_by_threshold[threshold]
        share = 100 * count / resolved if resolved else 0.0
        print(f"    <= {threshold:3d} days   {count:5d}  ({share:.2f}% of existing deps)")
        if count and count <= 12:
            print(f"                     {', '.join(band_members[threshold])}")

    verdict_line = _interpret(band_by_threshold, resolved)
    print("\n" + "-" * 72)
    print(verdict_line)
    print("-" * 72)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(
            {
                "distinct_existing_dependencies": len(names),
                "with_metadata": resolved,
                "without_metadata": no_metadata,
                "low_adoption": low_adoption,
                "band_by_threshold": band_by_threshold,
                "band_members": band_members,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {OUT_JSON}")
    return 0


def _interpret(band: dict[int, int], resolved: int) -> str:
    if not resolved:
        return "No metadata resolved; run with --enrich."
    at_30 = band.get(30, 0)
    share = 100 * at_30 / resolved
    if at_30 == 0:
        return (
            "VERDICT: the band is EMPTY at the default 30-day threshold.\n"
            "Provenance weighting could not have changed a single verdict on this\n"
            "corpus. The thesis that provenance reduces false positives is not\n"
            "supported by this evidence."
        )
    if share < 1.0:
        return (
            f"VERDICT: the band is {share:.2f}% of existing dependencies ({at_30}).\n"
            "That is a very small target. Provenance weighting would affect a\n"
            "marginal number of findings, so it should not be the headline claim."
        )
    return (
        f"VERDICT: the band is {share:.2f}% of existing dependencies ({at_30}).\n"
        "Large enough to be worth weighting. Proceed to Phase 2 and A/B it."
    )


if __name__ == "__main__":
    raise SystemExit(main())
