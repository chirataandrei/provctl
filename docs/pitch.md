# Interview pitch (about 3 minutes)

Public name: **provctl**. (The local folder was called PhantomScan; that name is not used on GitHub or the CV.)

## CV line

Built `provctl`, a local-only pre-commit gate that blocks hallucinated PyPI package names (slopsquatting). Evaluated on 30 OSS Python repos (0 false positives); documented a threat model and why agent provenance does not reduce FPs.

## Pitch

**The attack.** An LLM suggests a package that does not exist — `fastapi-turbo-helper`. A developer adds it to `requirements.txt`. An attacker who enumerates names models commonly invent has already registered it. `pip install` runs the attacker's code.

**What provctl does.** As a pre-commit hook it blocks a declared dependency that is absent from PyPI and is not first-party. That is the signature of a hallucinated top-level name. Lookups hit a local exact name index (~12 MB, ~4 µs). No package name leaves the machine during a check, so internal names are not leaked to pypi.org.

**The hard rule.** Absence + not first-party + not on a private index → BLOCK. Nothing relaxes this. Agent provenance is self-reported and forgeable; if it could unblock a missing package, that would be a bypass.

**What I measured.** On 30 widely used Python repos the first run had 22 false-positive blocks. Almost all were monorepo workspace members and git redirects, not "suspicious packages." After those suppressions: **0 blocks**. A synthetic-name regression guard still fires. I do **not** claim real-world recall — there is no public labelled slopsquatting corpus.

**The hypothesis that died.** I expected "which agent added this" to cut false positives. The band where provenance can change a verdict is 0.14% of real dependencies. Provenance stays for triage, not for accuracy.

**What it does not cover.** Transitive dependencies, typosquats of names that exist, and packages that exist but are malicious. Those are `pip-audit` / OSV / lockfile-diff territory.

## Talking points if asked

- Why not a Bloom filter: a false "exists" is a missed hallucinated package.
- Why imports never block: import-name → distribution-name mapping is the weakest link.
- Why one runtime dependency (`packaging`): a supply-chain tool with a large tree is the risk it claims to protect against.
