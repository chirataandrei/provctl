# Measurement results

Everything here is reproducible:

```bash
provctl index refresh
python -m measurements.run --clone   # 30 real OSS Python repositories
python -m measurements.expand_corpus  # grow to 100 repos (fixed selection rule)
python -m measurements.run --extended # measure all 100
python -m measurements.band --enrich # sizes the provenance band
python -m measurements.recall        # regression guard on synthetic negatives
python -m measurements.labelled      # labelled set + commit latency
```

Corpus: 30 widely-used open-source Python projects (requests, Django, FastAPI,
pandas, numpy, transformers, Airflow, Home Assistant, ...). They are presumed
clean, so **every block provctl emits against them is a false positive**.

## Results

The plan's central claim was that provenance-weighted thresholds reduce false
positives. Two numbers, measured before any provenance code was written, say
otherwise.

**1. There were no false positives left to reduce.** After the provenance-blind
fixes below, provctl blocks **0 times across all 30 repositories** — 100% of
repos pass cleanly.

**2. The band where provenance could act at all is 0.14%.** Provenance can only
move a verdict for a package that *exists but looks suspicious*. Of 2,130
distinct real dependencies with metadata resolved:

| "new package" threshold | in band | share of existing deps |
|---|---|---|
| ≤ 7 days | 0 | 0.00% |
| ≤ 30 days (default) | 3 | 0.14% |
| ≤ 90 days | 22 | 1.03% |
| ≤ 180 days | 45 | 2.12% |
| ≤ 365 days | 87 | 4.09% |

The plan set the bar itself: *"If that band is 2% of flags, the thesis is dead no
matter how good the provenance signal is."* At the default threshold it is
0.14%. Reaching even 2% requires calling a six-month-old package "new", which
would flag far more legitimate packages than attacks.

And the three packages in the 30-day band are all legitimate:
`apache-airflow-providers-anthropic`, `aioharmanluxury`, `harbor-python`. They
are new and niche, not malicious. So provenance-weighting on this corpus would
have operated exclusively on false positives.

**This is the outcome the phased plan was designed to surface cheaply**, and it
cost one measurement script rather than a full provenance implementation.

### What this does not say

It does not say provenance is worthless. The plan's own alternative framing
stands: provenance's defensible value is **triage and forensics** — which agent
session introduced this dependency, under which model, was it reviewed. That is
a real capability, and Phase 2 implements it. It is simply a different pitch
from "fewer false positives".

It also does not prove low real-world recall. This corpus contains no known
slopsquatting attacks, so it measures specificity only. See "recall" below.

## A/B: provenance on vs off

The naive A/B is uninformative — the corpus carries no agent attribution, so
"with provenance" changes nothing and the delta is trivially zero. That measures
missing input, not the mechanism.

So `measurements/ab.py` measures the **ceiling**: it rescores every finding under
counterfactuals, including the maximally verdict-changing one where *every*
dependency was added by an unreviewed agent. If the ceiling is small, no
improvement in provenance *quality* can matter, because the mechanism has
nowhere to act.

| scenario | block | warn | info | ok |
|---|---|---|---|---|
| baseline (no provenance) | 0 | 296 | 55 | 2777 |
| all agent, unreviewed | 2 | 294 | 55 | 2777 |
| all human | 0 | 296 | 55 | 2777 |
| all agent, human-reviewed | 0 | 296 | 55 | 2777 |

**Provenance changed 2 verdicts out of 3,128 findings — 0.06%.** Two further
observations matter more than that number:

1. **Every change was an escalation** (warn → block). De-escalation — the
   direction that would actually reduce false positives — moved nothing,
   because the baseline blocks nothing to begin with. There was nothing to
   de-escalate.
2. **Both escalated packages are legitimate**
   (`apache-airflow-providers-anthropic`, `aioharmanluxury`). So the entire
   measurable effect of provenance on this corpus was to *create* two false
   positives.

This is the ceiling, not an average: it assumes the most provenance-favourable
input possible. Real repositories with partial attribution would show less.

## Baseline false-positive rate, and how it got to zero

The first run was not clean. Iterating on it produced the four fixes that
actually matter, none of which involve provenance:

| Stage | Blocks | Repos with ≥1 block | Warnings |
|---|---|---|---|
| Initial | 22 | 3 / 30 | 524 |
| + workspace-member discovery | 2 | 2 / 30 | 524 |
| + `[tool.uv.sources]` redirects | 0 | 0 / 30 | 424 |
| + private modules & cross-version stdlib | 0 | 0 / 30 | 424 |
| + local sibling resolution | 0 | 0 / 30 | 294 |

**Monorepo workspace members were 20 of the 22 original false positives.**
Apache Airflow declares 139 nested `pyproject.toml` files and depends on ~20 of
them by name (`apache-airflow-dev`, `apache-airflow-scripts`, ...). None exists
on PyPI; all are first-party. Path-based detection alone cannot catch these,
because the distribution name has no relationship to any directory name — only
the nested manifest knows it. Fix: read them.

**The remaining 2 were `[tool.uv.sources]` git redirects** (`pydantic-docs`,
`mypy-primer`). Both are declared as ordinary dependencies but resolved from git,
so checking them against PyPI is meaningless.

This is direct evidence for the plan's Assumption 1 pushback: the dominant
false-positive source is private/internal packages and resolution failures, and
**provenance does nothing for any of them.**

### Warning noise

Warnings fell from 524 to 294 by fixing three classes of bogus import
extraction:

- **Package-private modules** (`_typeshed`, `_pytest`). A leading underscore
  means internal-to-some-package; such names are essentially never published
  standalone.
- **Cross-version stdlib.** `sys.stdlib_module_names` describes the interpreter
  running provctl, so a 3.13 runner called `annotationlib` (3.14) third-party,
  and every Python-2 compat shim reported `urllib2`, `urlparse`, `cPickle`.
- **Local siblings.** Python puts the importing file's own directory on
  `sys.path`, so `tests/fixtures/app/main.py` importing `helpers` resolves to
  `tests/fixtures/app/helpers.py`. Test suites and docs examples rely on this
  constantly; without it, four large repos produced 80% of all warnings.

The remaining 294 warnings are a long tail of 415 distinct names, concentrated
in test-fixture and documentation trees. They are advisory and never block.

## Recall

100% on 10 synthetic hallucinated names (`fastapi-turbo-helper`,
`requests-async-client`, ...). This is a **regression guard, not evidence**: the
names were chosen to be absent, so it only proves the hard rule still fires. It
matters because the false-positive work above repeatedly *reduced* blocking, and
over-suppression would otherwise be silent.

Real recall cannot be measured here. The only labelled corpus of hallucinated
package names (USENIX Security 2025, 205,474 names) is withheld from public
release and shared only with verified researchers on request. **Any tool
claiming a real-world slopsquatting recall number is inventing it.**

## Performance

- Index lookup: **4.2 µs** per name, 862,804 names, exact.
- 50,000 real-name lookups: 0 misses. 50,000 fake-name lookups: 0 false hits.
  A Bloom filter would fail the second test, which is the one that matters:
  a false "this exists" is a missed hallucinated package.
- Index on disk: 12.37 MB (vs 3.1 MB for a 1e-6 Bloom filter — 9 MB to buy
  correctness).
- Full corpus scan, 30 repos including Airflow and Home Assistant: ~47s.

## Labelled set: caught vs missed

`python -m measurements.labelled`, data in `measurements/data/`.

- 320 plausible-but-invented package names (`hallucinated_candidates.txt`),
  written by a language model asked to imitate names an assistant would make up.
  Label = "does not exist on PyPI" according to the local index.
- 499 real packages (`real_names.txt`): 59 hand-picked popular ones plus a
  seeded random sample (seed 0) of 440 from the top-5000 most downloaded.

| | count | outcome |
|---|---|---|
| invented, absent from PyPI | 310 | **310 blocked** |
| invented, already registered on PyPI | 10 | not catchable by a name check |
| real | 499 | **0 blocked** |

Overall: 310 of 320 invented names caught (96.9%). The 10 misses
(`python-dotenv-vault`, `pydantic-ai-helpers`, `scrapy-playwright-stealth`, ...)
are the slopsquatting case itself: the name is already registered, and no
local existence check can see that. Catching them needs reputation signals
(age, downloads, maintainers), which is what the warn band is for; whether
those 10 are benign or squatted was not investigated.
Caveats: one generator (a single model, written in one sitting), so names are
more uniform than real hallucinations; and 310/310 is close to tautological
(absent name -> block). The informative figure is the registered fraction
(3% here).

## Commit latency

Median **91 ms** (worst 105 ms, 20 runs after a warm-up) for `provctl check`
on a staged `requirements.txt` with 5 deps, including Python interpreter
startup, on a laptop (macOS). Lookups are local; no network calls.

## 100-repository run

`measurements/expand_corpus.py` extends the 30 repos above to 100 using a fixed
rule, so the choice is not cherry-picked: top GitHub Python repos by stars,
<= 300 MB, not already in the corpus, and containing a dependency manifest
(awesome-lists and tutorials without one are dropped). The 70 added repos are
listed in `measurements/data/extra_repos.txt`; results in
`measurements/out/baseline100.json`.

| | |
|---|---|
| repos measured | 100 (0 failed) |
| dependency findings | 6,141 |
| blocks | **1**, in 1 of 100 repos |
| warnings | 404 |

The single block is `codecs` in
`josephmisiti/awesome-machine-learning/scripts/requirements.txt`. `codecs` is a
standard-library module, not a PyPI project (PyPI returns 404), so
`pip install -r` on that file fails today and anyone could register the name.
I count it as a true catch, not a false positive, but it is the one place where
the "presumed clean" assumption is wrong, so the honest summary is "1 of 100 repos
blocked, and the block is a real mistake", not "0 false positives". The 70 new
repos skew toward recent AI/agent projects, which is the population most likely
to contain invented names, and still yielded no false block.

### The 401 "absent from index" findings (100-repo run)

- **400 are imports, 1 is a manifest entry.** Imports only ever warn; the one
  manifest entry is the `codecs` block above.
- 376 distinct import names. Roughly 55 of them match a PyPI project under a
  common alternative spelling (`miio` -> `python-miio`, `digitalocean` ->
  `python-digitalocean`, `debian` -> `python-debian`): real dependencies whose
  import name differs from the distribution name. This is a heuristic upper
  bound, not a manual review: it also produces false matches (`git` -> `pygit`,
  `api` -> `pyapi`).
- Most of the rest are repo-internal: script and tool names (Airflow's
  `check-*` hooks), test fixtures (`foo1`..`foo16`, `doesnotexist`,
  `name-of-a-company`), vendor hardware SDKs (`habana-frameworks`,
  `hailo-platform`), and Python 2 stdlib names (`htmlentitydefs`).
- This is why imports never block: the import-to-distribution mapping is the
  noisiest signal in the tool. The names were not individually verified.
