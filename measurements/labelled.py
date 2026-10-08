"""Labelled set: model-invented package names vs real ones, plus commit latency."""

from __future__ import annotations

import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from measurements.recall import check_names
from provctl.index.store import IndexStore
from provctl.paths import cache_dir

DATA = Path(__file__).parent / "data"


def _names(fname: str) -> list[str]:
    return [l.strip() for l in (DATA / fname).read_text().splitlines() if l.strip()]


def latency(runs: int = 20) -> tuple[float, float]:
    """Wall-clock ms of `python -m provctl.cli check` in a repo with one staged manifest."""
    tmp = Path(tempfile.mkdtemp(prefix="provctl-lat-"))
    for cmd in (["git", "init", "-q", "."], ["git", "config", "user.email", "a@b.c"],
                ["git", "config", "user.name", "x"]):
        subprocess.run(cmd, cwd=tmp, check=True)
    (tmp / "requirements.txt").write_text("requests\nnumpy\npandas\nfastapi\nflask\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
    times = []
    for _ in range(runs + 1):
        t = time.perf_counter()
        proc = subprocess.run([sys.executable, "-m", "provctl.cli", "check"], cwd=tmp,
                              capture_output=True, text=True)
        if proc.returncode not in (0, 1) or "summary" not in proc.stdout:  # failed run: timing it is meaningless
            raise RuntimeError(f"provctl failed: {proc.stderr.strip()}")
        times.append((time.perf_counter() - t) * 1000)
    times = times[1:]  # drop cold start
    return statistics.median(times), max(times)


def main() -> int:
    store = IndexStore(cache_dir())
    if not store.exists():
        print("error: run `provctl index refresh` first", file=sys.stderr)
        return 2
    index = store.open_index()
    candidates = _names("hallucinated_candidates.txt")
    fake = [n for n in candidates if not index.contains(n)]
    registered = [n for n in candidates if index.contains(n)]
    real = [n for n in _names("real_names.txt") if index.contains(n)]
    index.close()

    fake_v = check_names(fake, store)
    real_v = check_names(real, store)
    caught = [n for n in fake if fake_v.get(n) == "block"]
    fp = [n for n in real if real_v.get(n) == "block"]

    print(f"invented names            {len(candidates)}")
    print(f"  absent from PyPI        {len(fake)}   -> blocked {len(caught)}/{len(fake)}")
    print(f"  already registered      {len(registered)}   -> not catchable by a name check")
    for n in registered:
        print(f"    {n}")
    print(f"  caught of all invented  {len(caught)}/{len(candidates)}")
    print(f"real names                {len(real)}   -> false positives {len(fp)}/{len(real)}")
    med, worst = latency()
    print(f"commit latency            median {med:.0f} ms, worst {worst:.0f} ms (20 runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
