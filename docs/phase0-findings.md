# Phase 0 findings — git-ai provenance spike

Gate for the whole plan. Run against **git-ai v1.6.20** (Apache-2.0, macOS arm64
release binary, sha256 `9bf4631f09397bd8b7161dda1bd89d5b1b3c302874472cbddc787f4806b9d419`)
on git 2.39.5 / Python 3.14.3.

Every claim below is reproducible; the commands are given.

## Result: the plan's central pre-commit mechanism does not work in v1.6.20

The plan assumed `git ai blame --json --contents -` could read per-line provenance
for staged-but-uncommitted lines. It cannot. This triggers the fallback the plan
already anticipated ("our own Claude Code `PostToolUse` hook").

### 1. `blame --contents -` is deterministically broken

git-ai builds a git command that unconditionally places `HEAD` *and* `--contents -`
on the same command line:

```
git -C <repo> --no-pager blame --line-porcelain -L 1,3 HEAD --contents - -- app.py
fatal: cannot use --contents with final commit object name
```

git rejects that combination unconditionally — verified by running the same
argv against plain `git`, with no git-ai involved. So this is an argument-
construction bug in git-ai, **not** a environment or setup problem, and no amount
of configuration will make it work.

Consequence: there is no supported way to ask git-ai "who wrote line N of this
dirty file" before the commit exists.

### 2. `blame --json` returns empty attribution maps

Both before and after committing, `blame --json` returned:

```json
{ "lines": {}, "prompts": {}, "metadata": { "is_logged_in": false, ... } }
```

The documented shape (a `lines` map of ranges to session keys, plus a `prompts`
map) is correct, but it is only populated once the daemon has ingested the commit
via Trace2. See (4).

### 3. `status --json` *does* expose uncommitted provenance — but only in aggregate

This worked with no hook installation at all, because we invoked `checkpoint`
directly:

```json
{"stats":{"human_additions":0,"unknown_additions":0,"ai_additions":3,
          "ai_accepted":3,"git_diff_added_lines":3,"tool_model_breakdown":{}},
 "checkpoints":[{"additions":3,"deletions":0,"tool_model":"mock_ai unknown","is_human":false}]}
```

Useful, and it confirms `unknown_additions` exists as a first-class state
(matching the AI / known-human / untracked model). But it is **repo-wide
aggregate** — no file paths, no line ranges. It can tell us "the working tree
contains agent-authored lines"; it cannot tell us "*this import* was
agent-authored", which is what the verdict logic needs.

### 4. Full operation requires a global system modification

git-ai observes git through **Trace2**, not git hooks. `git ai debug` reports:

```
trace2 global config is not configured
attribution self-check for configured git failed
```

`git ai install-hooks` fixes this by writing `trace2.eventTarget` /
`trace2.eventNesting` into **global** git config, plus agent hook configs.
Setting those keys repo-locally (pointing at the daemon's
`~/.git-ai/internal/daemon/trace2.sock`) was **not** sufficient — no note was
written to `refs/notes/ai`.

We deliberately did not run `install-hooks`; global git config was verified
untouched throughout.

### 5. Constraint-5 landmine confirmed: prompt text is stored on disk

git-ai maintains `~/.git-ai/internal/transcripts-db` (SQLite) alongside
`metrics-db`. Prompt/transcript content is persisted locally, and the documented
blame JSON embeds a `messages` array in each prompt record.

This makes the plan's boundary rule mandatory rather than precautionary:
**`provenance/gitai.py` must whitelist the fields it copies and never pass agent
free-text through.** Implemented as an explicit allow-list in
`_scrub_session()` — anything not named is dropped, so a future git-ai release
adding new free-text fields cannot leak into our ledger by default.

## Architectural consequence

Provenance is read through an adapter with graceful degradation, and no path
hard-depends on git-ai:

| Source | Granularity | Availability |
|---|---|---|
| Our Claude Code `PostToolUse` hook | per-file, per-line-range | pre-commit, no global changes |
| `git ai status --json` | repo-wide aggregate | pre-commit, if git-ai present |
| `git ai diff --json <commit>` | per-hunk annotations | post-commit, needs install-hooks |

Because the plan already mandates **untracked → fall back to baseline**, every
one of these degradations is safe: missing provenance never invents an answer, it
just yields Phase 1 behaviour.

Our own hook is the primary path. It is strictly better for this purpose than the
mechanism the plan assumed: it works pre-commit, gives per-line ranges, requires
no global system modification, and has no dependency on git-ai's daemon.

## Re-running this spike

`tests/test_gitai_adapter.py` encodes findings 1-3 against recorded fixtures, so
a future git-ai release that fixes the `--contents` bug will show up as a
skipped-guard rather than a silent behaviour change. To re-verify against a live
binary:

```bash
python -m provctl.cli doctor --verbose
```
