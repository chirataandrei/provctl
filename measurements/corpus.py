"""Corpus of real, presumed-clean Python repositories."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Repo:
    name: str
    url: str
    # Repos that legitimately use private/internal packages or unusual layouts.
    # Recorded so the report can separate "expected hard case" from "surprise".
    notes: str = ""


CORPUS: tuple[Repo, ...] = (
    Repo("requests", "https://github.com/psf/requests"),
    Repo("flask", "https://github.com/pallets/flask"),
    Repo("django", "https://github.com/django/django"),
    Repo("fastapi", "https://github.com/fastapi/fastapi"),
    Repo("pydantic", "https://github.com/pydantic/pydantic"),
    Repo("httpx", "https://github.com/encode/httpx"),
    Repo("rich", "https://github.com/Textualize/rich"),
    Repo("textual", "https://github.com/Textualize/textual"),
    Repo("click", "https://github.com/pallets/click"),
    Repo("black", "https://github.com/psf/black"),
    Repo("pytest", "https://github.com/pytest-dev/pytest"),
    Repo("poetry", "https://github.com/python-poetry/poetry"),
    Repo("scrapy", "https://github.com/scrapy/scrapy"),
    Repo("celery", "https://github.com/celery/celery"),
    Repo("sqlalchemy", "https://github.com/sqlalchemy/sqlalchemy"),
    Repo("pandas", "https://github.com/pandas-dev/pandas", notes="large, C extensions"),
    Repo("numpy", "https://github.com/numpy/numpy", notes="large, C extensions"),
    Repo("scikit-learn", "https://github.com/scikit-learn/scikit-learn"),
    Repo("transformers", "https://github.com/huggingface/transformers", notes="very large"),
    Repo("langchain", "https://github.com/langchain-ai/langchain", notes="monorepo, namespace pkgs"),
    Repo("airflow", "https://github.com/apache/airflow", notes="monorepo, provider packages"),
    Repo("ansible", "https://github.com/ansible/ansible"),
    Repo("pipenv", "https://github.com/pypa/pipenv", notes="vendored dependencies"),
    Repo("mypy", "https://github.com/python/mypy"),
    Repo("ruff", "https://github.com/astral-sh/ruff", notes="mostly Rust"),
    Repo("uvicorn", "https://github.com/encode/uvicorn"),
    Repo("starlette", "https://github.com/encode/starlette"),
    Repo("boto3", "https://github.com/boto/boto3"),
    Repo("dbt-core", "https://github.com/dbt-labs/dbt-core", notes="monorepo"),
    Repo("home-assistant", "https://github.com/home-assistant/core", notes="very large, many deps"),
)
