# Threat model

What it covers, what it doesn't.

## Attack

An LLM suggests a package that does not exist. A developer accepts the
suggestion and adds it to `requirements.txt`. An attacker, who has been
enumerating names that models commonly hallucinate, has already registered it.
`pip install` fetches the attacker's code, which runs at install time.

The defining property is that a **hallucinated top-level name gets typed and
installed**. provctl blocks exactly that: a declared dependency absent from
PyPI and not first-party stops the commit.

## Adversary assumed

**The adversary controls a package on the public index.** They can register a
name, publish plausible metadata, and wait.

**The adversary does not control the developer's machine.** This bound is
load-bearing: if they do, they can edit the manifest after the check, disable
the hook, rewrite the local index, or edit the ledger. No pre-commit gate
survives a compromised host, and pretending otherwise would be dishonest.

## Guarantees

**The existence check cannot be relaxed by provenance.** This is enforced in
`policy/verdict.apply_provenance` and covered by a test named as an invariant.
Provenance is unattested; if it could unblock a non-existent package, an
attacker who influenced the agent could unblock their own payload.

**No package name leaves the machine during a check.** All lookups hit a local
index. This is a confidentiality guarantee as much as a latency one: querying
pypi.org for `acme_internal_billing` discloses that the name exists inside your
company, which is precisely the reconnaissance a dependency-confusion attacker
wants.

**Enrichment cannot leak internal names.** `enrich.pypi.enrich` refuses to
query any name absent from the public index, and the check is in the enrichment
function rather than left to callers.

**No source code, diffs, or prompt text is ever written to the ledger.**
Enforced structurally: `provenance/gitai._scrub_session` is an allow-list, so a
future git-ai release adding new free-text fields cannot leak by default; and
the hook records only file paths and line numbers, reading inserted text solely
to count newlines.

## Non-guarantees

**Agent identity is not attestable.** git-ai records what the agent passes to
`checkpoint`: tool, model string, session id. Nothing verifies any of it. A
buggy agent reports the wrong model; a malicious one reports whatever it likes.
Our own hook is no better — it records what the agent's payload claims.

Consequently, **model strings are recorded verbatim and never parsed for
meaning.** There is no "was this a weak model" inference anywhere, because that
would be policy built on an attacker-controlled string.

**Git notes and the event log are editable.** They are local files. Anyone with
write access can add, alter, or delete entries. Provenance is a convenience
signal among honest actors, not a control against an adversary.

**Transitive dependencies are not checked.** A malicious package can still
reach you as someone else's dependency, and provctl will not see it. This is
`pip-audit` and OSV territory, and the right unit of analysis there is the
lockfile diff.

**Typosquats of real packages are not detected.** `reqeusts` is caught only if
nobody has registered it. A name that exists passes the hard rule by
definition. Edit-distance detection against popular names is a plausible
addition and is not implemented.

**A package that exists but is malicious is not detected.** provctl checks
existence and metadata, never content.

## The forgeable direction, stated explicitly

Provenance weighting can run two ways.

**De-escalate** (the default): treat a suspicious package as blocked, and let
evidence of human review relax it to a warning. This is the direction that can
reduce false positives — and it is forgeable, because "a human reviewed this"
is an unverified claim.

**Escalate**: keep the baseline at warning and raise unreviewed agent edits to
a block. Unforgeable, but delivers zero false-positive reduction by
construction.

The default is de-escalate, accepted on the bound above: the adversary controls
the *package*, not the machine. If they control the machine, the ledger was
never going to save you.

Note that measurement made this decision largely academic: the de-escalating
path changed **zero** verdicts on a 30-repo corpus, because the baseline blocks
nothing to de-escalate. See [measurements.md](measurements.md).

Set `provenance.mode = "escalate"` in `.provenance/policy.toml` if you prefer
the unforgeable direction, or `enabled = false` to run provenance-blind.

## Failure modes, and why they are safe

Every degradation resolves toward baseline behaviour rather than toward a guess:

| Failure | Result |
|---|---|
| git-ai absent or broken | provenance-blind (Phase 1 behaviour) |
| File moved, attribution lost | `untracked` → no verdict modification |
| Metadata cache empty | no age signal → suspicious band empty |
| Index stale | warns, still checks |
| Index missing | refuses to run, tells you how to fix it |
| Private index configured | absent → *unverifiable*, not *fake* |
| Import unresolvable or ambiguous | warns, never blocks |
| Manifest malformed | warns, continues |
| Agent hook crashes | exits 0, loses one record |

The one deliberate hard failure is a missing index, because silently passing
every package would give false assurance.
