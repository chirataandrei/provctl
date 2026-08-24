"""Import extraction, mapping, and first-party detection."""

from __future__ import annotations

from provctl.resolve import firstparty, manifests
from provctl.resolve.distmap import Confidence, DistributionResolver
from provctl.resolve.imports import extract_third_party


def modules(source: str) -> set[str]:
    return {site.module for site in extract_third_party(source)}


class TestImportExtraction:
    def test_ignores_stdlib(self):
        assert modules("import os\nimport sys\nimport requests\n") == {"requests"}

    def test_handles_guarded_imports(self):
        """The construct a regex extractor gets wrong, and agents write often."""
        source = (
            "try:\n"
            "    import ujson as json\n"
            "except ImportError:\n"
            "    import json\n"
        )
        assert modules(source) == {"ujson"}

    def test_handles_function_local_and_type_checking_imports(self):
        source = (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    import pandas\n"
            "def f():\n"
            "    import boto3\n"
            "    return boto3\n"
        )
        assert modules(source) == {"pandas", "boto3"}

    def test_handles_parenthesised_multiline_import(self):
        source = "from fastapi import (\n    FastAPI,\n    Request,\n)\n"
        assert modules(source) == {"fastapi"}

    def test_relative_imports_are_never_third_party(self):
        source = "from . import sibling\nfrom ..pkg import thing\n"
        assert modules(source) == set()

    def test_takes_top_level_of_dotted_module(self):
        assert modules("import concurrent.futures\nimport google.cloud.storage\n") == {"google"}

    def test_unparseable_file_yields_nothing_rather_than_raising(self):
        """A half-written file during a pre-commit run must not break the hook."""
        assert extract_third_party("def broken(:\n  pass\n") == []

    def test_records_line_numbers(self):
        sites = extract_third_party("import os\n\nimport requests\n")
        assert [(s.module, s.lineno) for s in sites] == [("requests", 3)]


class TestDistributionMapping:
    def test_curated_exceptions(self):
        resolver = DistributionResolver(installed={})
        assert resolver.resolve("yaml").candidates == ("pyyaml",)
        assert resolver.resolve("cv2").candidates == ("opencv-python",)
        assert resolver.resolve("sklearn").candidates == ("scikit-learn",)
        assert resolver.resolve("PIL").candidates == ("pillow",)
        assert resolver.resolve("bs4").candidates == ("beautifulsoup4",)

    def test_ambiguous_mapping_reports_all_candidates_and_never_guesses(self):
        resolver = DistributionResolver(installed={})
        result = resolver.resolve("psycopg2")
        assert result.confidence is Confidence.AMBIGUOUS
        assert len(result.candidates) > 1
        assert result.primary is None
        assert not result.is_definite

    def test_installed_environment_wins_over_curation(self):
        resolver = DistributionResolver(installed={"yaml": ("some-vendored-yaml",)})
        result = resolver.resolve("yaml")
        assert result.candidates == ("some-vendored-yaml",)
        assert result.confidence is Confidence.INSTALLED

    def test_namespace_package_installed_in_two_dists_is_ambiguous(self):
        resolver = DistributionResolver(installed={"jaraco": ("jaraco-classes", "jaraco-functools")})
        assert resolver.resolve("jaraco").confidence is Confidence.AMBIGUOUS

    def test_heuristic_fallback_is_marked_low_confidence(self):
        resolver = DistributionResolver(installed={})
        result = resolver.resolve("some_new_lib")
        assert result.candidates == ("some-new-lib",)
        assert result.confidence is Confidence.HEURISTIC
        assert not result.is_definite

    def test_user_override_beats_everything(self):
        resolver = DistributionResolver(
            installed={"mypkg": ("wrong-dist",)}, overrides={"mypkg": ("right-dist",)}
        )
        assert resolver.resolve("mypkg").candidates == ("right-dist",)


class TestFirstParty:
    def test_detects_flat_and_src_layout_packages(self, tmp_path):
        (tmp_path / "internal_utils").mkdir()
        (tmp_path / "internal_utils" / "__init__.py").touch()
        (tmp_path / "src" / "acme_billing").mkdir(parents=True)
        (tmp_path / "src" / "acme_billing" / "__init__.py").touch()

        index = firstparty.discover(tmp_path)
        assert index.is_first_party_module("internal_utils")
        assert index.is_first_party_module("acme_billing")

    def test_distribution_name_maps_back_to_repo_module(self, tmp_path):
        """`acme-billing` in a manifest must match `acme_billing/` on disk.

        Regression: missing this blocked every internal package on first run.
        """
        (tmp_path / "src" / "acme_billing").mkdir(parents=True)
        (tmp_path / "src" / "acme_billing" / "__init__.py").touch()
        index = firstparty.discover(tmp_path)
        assert index.is_first_party_dist("acme-billing")

    def test_prefix_globs(self, tmp_path):
        index = firstparty.discover(tmp_path, prefixes=("acme_*",))
        assert index.is_first_party_module("acme_anything")
        assert index.is_first_party_dist("acme-anything")
        assert not index.is_first_party_module("requests")

    def test_skips_virtualenvs_and_caches(self, tmp_path):
        for junk in (".venv", "node_modules", "__pycache__", "build"):
            (tmp_path / junk).mkdir()
            (tmp_path / junk / "__init__.py").touch()
        index = firstparty.discover(tmp_path)
        assert not index.is_first_party_module(".venv")
        assert not index.is_first_party_module("node_modules")
        assert not index.is_first_party_module("build")


class TestManifests:
    def test_requirements_parsing(self):
        scan = manifests.parse_requirements(
            "requests==2.31.0\n"
            "# a comment\n"
            "flask[async]>=2  # inline comment\n"
            "\n"
            "-r other.txt\n",
            "requirements.txt",
        )
        assert {e.name for e in scan.entries} == {"requests", "flask"}

    def test_extracts_index_urls(self):
        scan = manifests.parse_requirements(
            "--index-url https://pypi.internal.acme.com/simple\nrequests\n",
            "requirements.txt",
        )
        assert scan.has_private_index

    def test_public_pypi_is_not_a_private_index(self):
        scan = manifests.parse_requirements(
            "--index-url https://pypi.org/simple\nrequests\n", "requirements.txt"
        )
        assert not scan.has_private_index

    def test_direct_url_requirements_flagged(self):
        scan = manifests.parse_requirements(
            "mypkg @ git+https://github.com/acme/mypkg.git\n", "requirements.txt"
        )
        assert scan.entries[0].is_url

    def test_pyproject_pep621_and_optional_groups(self):
        scan = manifests.parse_pyproject(
            """
[project]
name = "acme-service"
dependencies = ["requests>=2", "PyYAML"]
[project.optional-dependencies]
dev = ["pytest"]
""",
            "pyproject.toml",
        )
        assert scan.project_name == "acme-service"
        assert {e.name for e in scan.entries} == {"requests", "pyyaml", "pytest"}

    def test_poetry_sources_are_private_indexes(self):
        scan = manifests.parse_pyproject(
            """
[tool.poetry]
name = "svc"
[tool.poetry.dependencies]
requests = "^2.0"
[[tool.poetry.source]]
name = "internal"
url = "https://pypi.internal.acme.com/simple"
""",
            "pyproject.toml",
        )
        assert scan.has_private_index
        assert "requests" in {e.name for e in scan.entries}

    def test_uv_index_detected(self):
        scan = manifests.parse_pyproject(
            """
[project]
name = "svc"
dependencies = []
[[tool.uv.index]]
url = "https://internal.acme.com/simple"
""",
            "pyproject.toml",
        )
        assert scan.has_private_index

    def test_malformed_toml_reports_error_instead_of_raising(self):
        scan = manifests.parse_pyproject("[project\nbroken", "pyproject.toml")
        assert scan.parse_errors
        assert scan.entries == []
