"""Index store: exactness is the whole point, so test both error directions."""

from __future__ import annotations

import pytest

from provctl.index.store import IndexMissingError, IndexStore, NameIndex


@pytest.fixture
def store(tmp_path):
    store = IndexStore(tmp_path / "cache")
    store.write(
        ["requests", "PyYAML", "scikit-learn", "Django", "zzz-last", "aaa-first"],
        last_serial=42,
        source_url="test://",
        fetched_at="2026-01-01T00:00:00+00:00",
    )
    return store


def test_finds_every_written_name(store):
    index = store.open_index()
    for name in ["requests", "pyyaml", "scikit-learn", "django", "zzz-last", "aaa-first"]:
        assert index.contains(name), name
    index.close()


def test_normalizes_query(store):
    """PEP 503 equivalence: case folds, and runs of -_. collapse to a hyphen.

    Note `py_yaml` is deliberately absent: PEP 503 normalizes it to `py-yaml`,
    which is a *different* project from `PyYAML` -> `pyyaml`. Treating them as
    equivalent would be a security hole, since it would let a lookup for a
    non-existent name succeed against a real one.
    """
    index = store.open_index()
    for variant in ["PyYAML", "pyyaml", "PYYAML"]:
        assert index.contains(variant), variant
    for variant in ["scikit-learn", "scikit_learn", "scikit.learn", "SciKit--Learn"]:
        assert index.contains(variant), variant
    assert not index.contains("py_yaml")
    index.close()


def test_absent_names_are_absent(store):
    index = store.open_index()
    for name in ["totally-fake", "reques", "requestss", "", "zzzz", "a"]:
        assert not index.contains(name), name
    index.close()


def test_boundary_entries(store):
    """First and last lines are where a binary search off-by-one shows up."""
    index = store.open_index()
    assert index.contains("aaa-first")
    assert index.contains("zzz-last")
    index.close()


def test_no_false_positives_at_scale(tmp_path):
    """A Bloom filter would fail this test. That is precisely why we do not use one."""
    store = IndexStore(tmp_path / "cache")
    real = [f"pkg-{i:06d}" for i in range(50_000)]
    store.write(real, last_serial=1, source_url="test://", fetched_at="2026-01-01T00:00:00+00:00")

    index = store.open_index()
    assert all(index.contains(name) for name in real)
    fakes = [f"nope-{i:06d}" for i in range(50_000)]
    assert not any(index.contains(name) for name in fakes)
    index.close()


def test_missing_index_raises_clearly(tmp_path):
    index = NameIndex(tmp_path / "absent.txt")
    with pytest.raises(IndexMissingError):
        index.contains("requests")


def test_empty_index_is_not_a_crash(tmp_path):
    store = IndexStore(tmp_path / "cache")
    store.write([], last_serial=0, source_url="test://", fetched_at="2026-01-01T00:00:00+00:00")
    index = store.open_index()
    assert not index.contains("requests")
    index.close()


def test_write_is_atomic_and_leaves_no_temp_files(store, tmp_path):
    store.write(["one", "two"], last_serial=2, source_url="test://",
                fetched_at="2026-01-02T00:00:00+00:00")
    leftovers = [p.name for p in (tmp_path / "cache").iterdir() if p.name.startswith(".")]
    assert leftovers == []
    meta = store.read_meta()
    assert meta.project_count == 2
    assert meta.last_serial == 2
