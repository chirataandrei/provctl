"""End-to-end checks through real git repositories."""

from __future__ import annotations

from provctl.check import Checker, CheckOptions
from provctl.model import Reason, Verdict
from provctl.policy.config import Policy
from provctl.resolve.distmap import DistributionResolver
from tests.conftest import git


def make_checker(repo, index_store, policy=None):
    # Pin an empty installed-environment map so results do not depend on
    # whatever happens to be installed in the test runner's venv.
    return Checker(
        repo,
        policy or Policy.default(),
        index_store,
        resolver=DistributionResolver(installed={}),
    )


def run(repo, index_store, policy=None, scope="staged"):
    return make_checker(repo, index_store, policy).run(CheckOptions(scope=scope))


def verdict_for(result, name):
    for finding in result.findings:
        if finding.name == name:
            return finding
    return None


class TestHallucinatedPackages:
    def test_blocks_nonexistent_manifest_entry(self, repo, index_store, write_and_stage):
        write_and_stage("requirements.txt", "requests\nfastapi-turbo-helper\n")
        result = run(repo, index_store)

        finding = verdict_for(result, "fastapi-turbo-helper")
        assert finding.verdict is Verdict.BLOCK
        assert finding.reason is Reason.ABSENT_FROM_INDEX
        assert result.exit_code() == 1

    def test_real_packages_do_not_block(self, repo, index_store, write_and_stage):
        write_and_stage("requirements.txt", "requests\nDjango\nnumpy\n")
        result = run(repo, index_store)
        assert result.exit_code() == 0
        assert not result.blocking


class TestFirstPartySuppression:
    def test_monorepo_internal_package_is_not_blocked(
        self, repo, index_store, write_and_stage
    ):
        """The single largest false-positive source. Must work with no config."""
        write_and_stage("src/acme_billing/__init__.py", "")
        write_and_stage(
            "pyproject.toml",
            '[project]\nname = "acme-service"\ndependencies = ["acme-billing", "requests"]\n',
        )
        result = run(repo, index_store)

        finding = verdict_for(result, "acme-billing")
        assert finding.verdict is Verdict.INFO
        assert finding.reason is Reason.ABSENT_BUT_FIRST_PARTY
        assert result.exit_code() == 0

    def test_allowlist_suppresses(self, repo, index_store, write_and_stage):
        write_and_stage("requirements.txt", "acme-secret-lib\n")
        policy = Policy.default()
        policy.first_party_allow = frozenset({"acme-secret-lib"})
        result = run(repo, index_store, policy)
        assert verdict_for(result, "acme-secret-lib").verdict is Verdict.INFO

    def test_prefix_rule_suppresses(self, repo, index_store, write_and_stage):
        write_and_stage("requirements.txt", "acme-anything\n")
        policy = Policy.default()
        policy.first_party_prefixes = ("acme-*",)
        result = run(repo, index_store, policy)
        assert verdict_for(result, "acme-anything").verdict is Verdict.INFO


class TestPrivateIndex:
    def test_private_index_downgrades_block_to_warn(
        self, repo, index_store, write_and_stage
    ):
        write_and_stage(
            "requirements.txt",
            "--index-url https://pypi.internal.acme.com/simple\ninternal-thing\n",
        )
        result = run(repo, index_store)
        finding = verdict_for(result, "internal-thing")
        assert finding.verdict is Verdict.WARN
        assert finding.reason is Reason.ABSENT_BUT_PRIVATE_INDEX
        assert result.exit_code() == 0


class TestImportHandling:
    def test_curated_mapping_keeps_real_imports_silent(
        self, repo, index_store, write_and_stage
    ):
        """`import yaml` is PyYAML. Getting this wrong would flag a real package."""
        write_and_stage("app.py", "import yaml\nimport cv2\nimport sklearn\n")
        result = run(repo, index_store)
        assert result.exit_code() == 0
        assert not [f for f in result.findings if f.verdict is Verdict.WARN]

    def test_hallucinated_import_warns_but_never_blocks(
        self, repo, index_store, write_and_stage
    ):
        write_and_stage("app.py", "import totally_made_up_pkg\n")
        result = run(repo, index_store)
        finding = verdict_for(result, "totally-made-up-pkg")
        assert finding.verdict is Verdict.WARN
        assert result.exit_code() == 0

    def test_stdlib_imports_are_ignored(self, repo, index_store, write_and_stage):
        write_and_stage("app.py", "import os\nimport sys\nimport json\nimport asyncio\n")
        result = run(repo, index_store)
        assert result.findings == []


class TestChangeScoping:
    def test_only_added_lines_are_reported(self, repo, index_store, write_and_stage):
        """A pre-existing bad dependency is not this commit's problem.

        Re-reporting the whole manifest on every commit is what makes a hook
        annoying enough to be removed.
        """
        write_and_stage("requirements.txt", "requests\nlegacy-fake-package\n")
        git(repo, "commit", "-qm", "initial")

        (repo / "requirements.txt").write_text(
            "requests\nlegacy-fake-package\nflask\n", encoding="utf-8"
        )
        git(repo, "add", "requirements.txt")

        result = run(repo, index_store)
        assert verdict_for(result, "legacy-fake-package") is None
        assert result.exit_code() == 0

    def test_newly_added_bad_line_is_caught(self, repo, index_store, write_and_stage):
        write_and_stage("requirements.txt", "requests\n")
        git(repo, "commit", "-qm", "initial")

        (repo / "requirements.txt").write_text(
            "requests\nbrand-new-fake-pkg\n", encoding="utf-8"
        )
        git(repo, "add", "requirements.txt")

        result = run(repo, index_store)
        assert verdict_for(result, "brand-new-fake-pkg").verdict is Verdict.BLOCK

    def test_staged_content_is_checked_not_worktree(
        self, repo, index_store, write_and_stage
    ):
        """The commit records the staged version, so that is what must be checked."""
        write_and_stage("requirements.txt", "staged-fake-pkg\n")
        (repo / "requirements.txt").write_text("requests\n", encoding="utf-8")

        result = run(repo, index_store)
        assert verdict_for(result, "staged-fake-pkg").verdict is Verdict.BLOCK


class TestRobustness:
    def test_unparseable_python_does_not_break_the_run(
        self, repo, index_store, write_and_stage
    ):
        write_and_stage("broken.py", "def f(:\n  pass\n")
        write_and_stage("requirements.txt", "requests\n")
        result = run(repo, index_store)
        assert result.exit_code() == 0

    def test_malformed_pyproject_warns_and_continues(
        self, repo, index_store, write_and_stage
    ):
        write_and_stage("pyproject.toml", "[project\nbroken =")
        result = run(repo, index_store)
        assert any("pyproject" in w for w in result.warnings)
        assert result.exit_code() == 0

    def test_empty_changeset_is_clean(self, repo, index_store):
        result = run(repo, index_store)
        assert result.findings == []
        assert result.exit_code() == 0


class TestBandCounts:
    def test_bands_are_reported_for_measurement(self, repo, index_store, write_and_stage):
        write_and_stage("src/mine/__init__.py", "")
        write_and_stage(
            "requirements.txt", "requests\nfake-pkg-one\nfake-pkg-two\nmine\n"
        )
        result = run(repo, index_store)

        assert result.bands.hard_absent == 2
        assert result.bands.first_party_suppressed == 1
        # Phase 1 has no enrichment, so the provenance-sensitive band must be
        # empty rather than estimated.
        assert result.bands.provenance_sensitive == 0


def test_first_run_downloads_missing_index(repo, write_and_stage, tmp_path, monkeypatch, capsys):
    import json

    from provctl.cli import main

    source = tmp_path / "simple.json"
    source.write_text(json.dumps({
        "meta": {"_last-serial": 1},
        "projects": [{"name": "requests"}],
    }))
    monkeypatch.setenv("PROVCTL_CACHE_DIR", str(tmp_path / "fresh-cache"))
    monkeypatch.setenv("PROVCTL_INDEX_SOURCE", str(source))
    write_and_stage("requirements.txt", "requests\nfastapi-turbo-helper\n")

    rc = main(["check", "--repo", str(repo)])

    assert rc == 1  # blocked: the fake name is absent from the freshly fetched index
    assert "downloading it once" in capsys.readouterr().err
