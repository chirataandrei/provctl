"""Recall against synthetic hallucinated names."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from provctl.check import Checker, CheckOptions
from provctl.index.store import IndexStore
from provctl.paths import cache_dir
from provctl.policy.config import Policy
from provctl.resolve.distmap import DistributionResolver

SYNTHETIC_HALLUCINATIONS = [
    "fastapi-turbo-helper",
    "requests-async-client",
    "django-rest-toolkit-pro",
    "numpy-fast-utils",
    "pandas-dataframe-helper",
    "flask-jwt-auth-extended",
    "sqlalchemy-async-orm",
    "pytest-mock-helper-utils",
    "boto3-s3-easy-client",
    "openai-python-helper-sdk",
]


def check_names(names: list[str], store: IndexStore) -> dict[str, str]:
    """Run the gate over a throwaway repo containing only these names."""
    results: dict[str, str] = {}
    tmp = Path(tempfile.mkdtemp(prefix="provctl-recall-"))
    try:
        subprocess.run(["git", "init", "-q", "."], cwd=tmp, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "r@e.com"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "R"], cwd=tmp, check=True)

        (tmp / "requirements.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=tmp, check=True, capture_output=True)

        checker = Checker(
            tmp, Policy.default(), store, resolver=DistributionResolver(installed={})
        )
        result = checker.run(CheckOptions(scope="staged"))
        for finding in result.findings:
            results[finding.name] = finding.verdict.value
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    store = IndexStore(cache_dir())
    if not store.exists():
        print("error: no local index. Run `provctl index refresh`.", file=sys.stderr)
        return 2

    index = store.open_index()
    truly_absent = [n for n in SYNTHETIC_HALLUCINATIONS if not index.contains(n)]
    index.close()

    skipped = set(SYNTHETIC_HALLUCINATIONS) - set(truly_absent)
    if skipped:
        # If someone registers one of these names, it stops being a valid
        # negative and must be excluded rather than counted as a miss.
        print(f"note: {sorted(skipped)} now exist on PyPI; excluded from the test")

    verdicts = check_names(truly_absent, store)
    caught = [n for n in truly_absent if verdicts.get(n) == "block"]
    missed = [n for n in truly_absent if verdicts.get(n) != "block"]

    print("\n" + "=" * 72)
    print("RECALL ON SYNTHETIC HALLUCINATIONS")
    print("=" * 72)
    print(f"names tested   {len(truly_absent)}")
    print(f"blocked        {len(caught)}")
    print(f"missed         {len(missed)}")
    if truly_absent:
        print(f"recall         {100 * len(caught) / len(truly_absent):.1f}%")
    for name in missed:
        print(f"  MISSED {name} -> {verdicts.get(name, 'no finding')}")

    print("\nThis is a floor, not evidence of real-world recall: these names were")
    print("chosen to be absent, so the test only proves the hard rule still fires.")
    return 0 if not missed else 1


if __name__ == "__main__":
    raise SystemExit(main())
