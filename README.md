# provctl

[![CI](https://github.com/chirataandrei/provctl/actions/workflows/ci.yml/badge.svg)](https://github.com/chirataandrei/provctl/actions/workflows/ci.yml)

Pre-commit hook that blocks Python deps which don't exist on PyPI.

Coding assistants sometimes invent package names. If you add one to
`requirements.txt` and somebody already registered it, `pip install` runs
their code. provctl checks staged manifests against a local copy of the
PyPI name list and fails the commit if the name isn't there and isn't yours.

Lookups stay on disk. Package names are not sent to pypi.org during a check.

```console
$ echo "fastapi-turbo-helper" >> requirements.txt && git add -A && git commit -m "add dep"
BLOCK fastapi-turbo-helper
       no project by this name exists on PyPI
       at requirements.txt:2

This commit is blocked. To proceed:
  - 'fastapi-turbo-helper' does not exist on PyPI. If the name is a typo, fix it.
    If it is an internal package, add it to .provenance/policy.toml
```

| Measured | Result |
|---|---|
| Invented package names absent from PyPI, blocked | 310 / 310 |
| Invented names already registered on PyPI (not catchable by a name check) | 10 / 320 |
| Real popular packages wrongly blocked | 0 / 499 |
| Real OSS repos with a false block | 0 / 30 |
| Time added to a commit | ~90 ms median |

The invented-name set is small and I wrote it myself (a model, imitating
slopsquat-style names), so read it as a sanity check, not a benchmark.
Method and caveats: [docs/measurements.md](docs/measurements.md).

## Install

```bash
pip install provctl
provctl index refresh    # ~10 MB, once
```

## Use

```bash
provctl init
provctl check
```

pre-commit:

```yaml
repos:
  - repo: https://github.com/chirataandrei/provctl
    rev: v0.1.0
    hooks:
      - id: provctl
```

Use `provctl-warn-only` first if you don't want it to fail commits yet.

Other commands: `explain`, `ack`, `enrich`, `doctor`, `hook install`.

## Rules

- Missing from PyPI, not first-party, not on a private index → block.
  Provenance cannot override this (it's self-reported).
- Package exists but is new and unused → warn. Provenance can only move
  things in this band.
- Imports never block. `import yaml` vs `PyYAML` is too easy to get wrong.

Without config it already treats as first-party: workspace packages, uv/poetry
git/path sources, private indexes (`PIP_INDEX_URL` etc.), local modules,
`_private` modules, stdlib from other Python versions.

```toml
[first_party]
prefixes = ["acme_*"]
allow = ["acme-billing"]
```

## What it doesn't do

Transitive deps, typos of names that exist, and malicious packages that *are*
on PyPI. Use `pip-audit` / OSV for those.

Agent provenance (git-ai / hook) is for "who added this", not a security
control. Details in [docs/threat-model.md](docs/threat-model.md).

Exact name index is ~12 MB. A Bloom filter would be smaller but a false
"exists" is a missed fake package, so we don't use one.

Runtime dep: `packaging`. git-ai is optional.

## License

[Apache-2.0](LICENSE). https://github.com/chirataandrei/provctl
