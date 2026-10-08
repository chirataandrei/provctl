"""CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from provctl import __version__, paths, report
from provctl.check import Checker, CheckOptions
from provctl.index import adoption as adoption_mod
from provctl.index.refresh import refresh as refresh_index, staleness
from provctl.index.store import IndexStore
from provctl.policy.config import Policy


def _repo_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    root = paths.find_repo_root()
    if root is None:
        print("provctl: not inside a git repository", file=sys.stderr)
        raise SystemExit(2)
    return root


def _load(repo: Path) -> tuple[Policy, IndexStore]:
    policy = Policy.load(paths.policy_path(repo))
    store = IndexStore(paths.cache_dir())
    return policy, store


def cmd_check(args: argparse.Namespace) -> int:
    repo = _repo_root(args.repo)
    policy, store = _load(repo)

    if not store.exists():
        # First run (typically a pre-commit hook in an isolated env, where the
        # `provctl` command is not on PATH to refresh by hand). This downloads
        # the whole public name list; no package names leave the machine.
        print("provctl: no local PyPI index, downloading it once (~12 MB)...", file=sys.stderr)
        try:
            refresh_index(store)
        except Exception as exc:  # noqa: BLE001 - offline etc.; fall back to the manual hint
            print(f"provctl: automatic download failed: {exc}", file=sys.stderr)
    if not store.exists():
        print(
            "provctl: no local PyPI index found.\n"
            "  Run `provctl index refresh` once (about 12 MB, a few seconds).",
            file=sys.stderr,
        )
        return 2

    scope = "range" if args.rev_range else args.scope
    options = CheckOptions(
        scope=scope,
        rev_range=args.rev_range,
        use_provenance=not args.no_provenance,
        include_imports=not args.no_imports,
    )

    checker = Checker(repo, policy, store)
    try:
        result = checker.run(options)
    finally:
        checker.close()

    if args.bands:
        report.render_bands(result, sys.stdout)
        return 0
    if args.json:
        report.render_json(result, sys.stdout)
    else:
        report.render_human(result, sys.stdout, verbose=args.verbose)

    if args.warn_only:
        return 0
    return result.exit_code()


def cmd_index_refresh(args: argparse.Namespace) -> int:
    store = IndexStore(paths.cache_dir())
    print(f"provctl: refreshing index into {store.cache_dir}")
    result = refresh_index(store, force=args.force, source=args.source)
    if not result.changed:
        print(f"  {result.note} ({result.meta.project_count:,} projects)")
    else:
        print(f"  wrote {result.meta.project_count:,} project names")

    if not args.skip_adoption:
        try:
            names = adoption_mod.refresh(store, source=args.adoption_source)
            print(f"  adoption list: {len(names):,} widely-downloaded packages")
        except Exception as exc:
            print(f"  warning: could not fetch adoption list: {exc}", file=sys.stderr)
    return 0


def cmd_index_status(args: argparse.Namespace) -> int:
    store = IndexStore(paths.cache_dir())
    meta = store.read_meta()
    if meta is None:
        print("provctl: no index cached; run `provctl index refresh`")
        return 1
    age = staleness(meta)
    print(f"cache dir     {store.cache_dir}")
    print(f"projects      {meta.project_count:,}")
    print(f"last serial   {meta.last_serial}")
    print(f"fetched       {meta.fetched_at}")
    if age is not None:
        print(f"age           {age.days} day(s)")
    print(f"source        {meta.source_url}")
    adoption = adoption_mod.load(store)
    print(f"adoption list {len(adoption):,} names")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    """Print how a name maps to distributions and whether they are on PyPI."""
    repo = _repo_root(args.repo)
    policy, store = _load(repo)
    if not store.exists():
        print("provctl: no local index; run `provctl index refresh`", file=sys.stderr)
        return 2

    from provctl.resolve.distmap import DistributionResolver

    resolver = DistributionResolver(overrides=policy.mapping_overrides)
    resolution = resolver.resolve(args.name)
    index = store.open_index()
    adoption = adoption_mod.load(store)

    print(f"query            {args.name}")
    print(f"resolution       {resolution.confidence.value}")
    print(f"candidates       {', '.join(resolution.candidates) or '(none)'}")
    for candidate in resolution.candidates:
        exists = index.contains_normalized(candidate)
        adopted = adoption.is_widely_adopted(candidate) if len(adoption) else None
        print(f"  {candidate:32s} on PyPI: {exists}  widely-adopted: {adopted}")
    if resolution.confidence.value == "ambiguous":
        print("\nAmbiguous: several distributions provide this import name.")
        print("provctl will warn rather than guess. Pin the mapping in policy.toml:")
        print(f'  [mapping]\n  "{args.name}" = "<the-right-one>"')
    index.close()
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from provctl.policy.decisions import ensure_merge_driver
    from provctl.policy.config import DEFAULT_POLICY_TOML

    repo = _repo_root(args.repo)
    prov_dir = paths.provenance_dir(repo)
    prov_dir.mkdir(parents=True, exist_ok=True)

    policy_file = paths.policy_path(repo)
    if policy_file.exists() and not args.force:
        print(f"provctl: {policy_file} already exists (use --force to overwrite)")
    else:
        policy_file.write_text(DEFAULT_POLICY_TOML, encoding="utf-8")
        print(f"provctl: wrote {policy_file.relative_to(repo)}")

    decisions = paths.decisions_path(repo)
    if not decisions.exists():
        decisions.write_text("", encoding="utf-8")
        print(f"provctl: created {decisions.relative_to(repo)}")

    changed = ensure_merge_driver(repo)
    if changed:
        print("provctl: registered union merge for .provenance/decisions.jsonl")
    print("\nNext: run `provctl index refresh`, then add the pre-commit hook.")
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    from provctl.policy.decisions import DecisionLog

    repo = _repo_root(args.repo)
    policy, store = _load(repo)
    if not store.exists():
        print("provctl: no local index; run `provctl index refresh`", file=sys.stderr)
        return 2

    checker = Checker(repo, policy, store)
    try:
        result = checker.run(CheckOptions(scope=args.scope))
    finally:
        checker.close()
    targets = [f for f in result.findings if f.name == args.name or args.name == "all"]
    if not targets:
        print(f"provctl: no current finding for '{args.name}'")
        return 1

    log = DecisionLog(paths.decisions_path(repo))
    for finding in targets:
        if finding.verdict.value in ("ok", "info"):
            continue
        log.acknowledge(finding, note=args.note or "", actor=args.actor)
        print(f"provctl: acknowledged {finding.name} ({finding.reason.value})")
    return 0


def cmd_hook_record(args: argparse.Namespace) -> int:
    """Record an agent edit from stdin. Always exits 0 so a hook failure cannot kill the session."""
    from provctl.provenance.hook import hook_main

    repo = paths.find_repo_root()
    if repo is None:
        return 0
    return hook_main(sys.stdin.read(), repo, repo / ".git")


def cmd_hook_install(args: argparse.Namespace) -> int:
    """Install the PostToolUse hook in .claude/settings.json."""
    import json as json_mod

    repo = _repo_root(args.repo)
    settings_path = repo / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings = {}
    if settings_path.exists():
        try:
            settings = json_mod.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json_mod.JSONDecodeError):
            print(f"provctl: {settings_path} is not valid JSON; not modifying it",
                  file=sys.stderr)
            return 1

    hooks = settings.setdefault("hooks", {})
    post = hooks.setdefault("PostToolUse", [])
    if any("provctl hook record" in json_mod.dumps(entry) for entry in post):
        print("provctl: hook already installed")
        return 0

    post.append({
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [{"type": "command", "command": "provctl hook record"}],
    })
    settings_path.write_text(json_mod.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"provctl: installed PostToolUse hook in {settings_path.relative_to(repo)}")
    print("  Agent edits will now be recorded to .git/provctl/agent-events.jsonl")
    print("  (structural line ranges only; no file contents, no prompt text)")
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    """Fetch package-age metadata into the local cache. Not used by check."""
    from provctl.enrich.pypi import MetadataCache, enrich

    repo = _repo_root(args.repo)
    policy, store = _load(repo)
    if not store.exists():
        print("provctl: no local index; run `provctl index refresh`", file=sys.stderr)
        return 2

    checker = Checker(repo, policy, store)
    result = checker.run(CheckOptions(scope=args.scope, use_provenance=False))
    checker.close()

    names = sorted({f.name for f in result.findings})
    if not names:
        print("provctl: nothing to enrich")
        return 0

    index = store.open_index()
    with MetadataCache(store.cache_dir / "metadata.sqlite3") as cache:
        print(f"provctl: enriching up to {len(names)} package(s) ...")
        fetched, skipped = enrich(names, cache, index, workers=args.workers)
    index.close()
    print(f"  fetched {fetched}; skipped {skipped} "
          f"(already cached, or absent from the public index and never queried)")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Print environment / git-ai capability info."""
    from provctl.provenance.gitai import GitAiAdapter

    repo = paths.find_repo_root()
    store = IndexStore(paths.cache_dir())
    meta = store.read_meta()

    print(f"provctl        {__version__}")
    print(f"repository     {repo or '(not in a git repo)'}")
    print(f"cache          {store.cache_dir}")
    if meta:
        age = staleness(meta)
        print(f"index          {meta.project_count:,} projects, "
              f"{age.days if age else '?'} day(s) old")
    else:
        print("index          MISSING - run `provctl index refresh`")

    adapter = GitAiAdapter(repo or Path.cwd())
    caps = adapter.capabilities()
    print(f"git-ai         {caps.version or 'not installed'}")
    if caps.version:
        print(f"  status --json          {'yes' if caps.has_status_json else 'no'}")
        print(f"  per-line uncommitted   {'yes' if caps.has_dirty_blame else 'no (known bug)'}")
        if not caps.has_dirty_blame:
            print("  note: `blame --contents -` is broken upstream; provctl uses")
            print("        its own agent hook for per-line provenance instead.")
    else:
        print("  provctl runs provenance-blind (Phase 1 baseline) without it.")
    return 0




def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provctl",
        description="Provenance-weighted dependency risk gate for Python projects.",
    )
    parser.add_argument("--version", action="version", version=f"provctl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="check changed dependencies")
    check.add_argument("--repo", help="repository root (default: enclosing git repo)")
    check.add_argument(
        "--scope", choices=["staged", "worktree", "all"], default="staged",
        help="what to examine (default: staged, i.e. what a commit would record)",
    )
    check.add_argument("--rev-range", help="examine a revision range, e.g. main..HEAD")
    check.add_argument("--json", action="store_true", help="machine-readable output")
    check.add_argument("--bands", action="store_true", help="print band analysis only")
    check.add_argument("--verbose", "-v", action="store_true", help="include informational findings")
    check.add_argument("--warn-only", action="store_true", help="never exit non-zero")
    check.add_argument("--no-provenance", action="store_true",
                       help="run provenance-blind (the Phase 1 baseline)")
    check.add_argument("--no-imports", action="store_true",
                       help="only check declared manifest entries")
    check.set_defaults(func=cmd_check)

    index = sub.add_parser("index", help="manage the local PyPI name index")
    index_sub = index.add_subparsers(dest="index_command", required=True)

    refresh_p = index_sub.add_parser("refresh", help="download the PyPI name list")
    refresh_p.add_argument("--force", action="store_true", help="rewrite even if unchanged")
    refresh_p.add_argument("--source", help="URL or local path to a PEP 691 document")
    refresh_p.add_argument("--adoption-source", help="URL or local path to a top-packages JSON")
    refresh_p.add_argument("--skip-adoption", action="store_true")
    refresh_p.set_defaults(func=cmd_index_refresh)

    status_p = index_sub.add_parser("status", help="show cached index metadata")
    status_p.set_defaults(func=cmd_index_status)

    explain = sub.add_parser("explain", help="explain how a name resolves")
    explain.add_argument("name")
    explain.add_argument("--repo")
    explain.set_defaults(func=cmd_explain)

    init = sub.add_parser("init", help="create .provenance/ in this repository")
    init.add_argument("--repo")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    ack = sub.add_parser("ack", help="acknowledge a finding")
    ack.add_argument("name", help="distribution name, or 'all'")
    ack.add_argument("--repo")
    ack.add_argument("--scope", choices=["staged", "worktree", "all"], default="staged")
    ack.add_argument("--note", help="why this is acceptable")
    ack.add_argument("--actor", help="who acknowledged (default: git user)")
    ack.set_defaults(func=cmd_ack)

    hook = sub.add_parser("hook", help="agent provenance hook")
    hook_sub = hook.add_subparsers(dest="hook_command", required=True)

    hook_record = hook_sub.add_parser("record", help="record an agent edit (reads stdin)")
    hook_record.set_defaults(func=cmd_hook_record)

    hook_install = hook_sub.add_parser("install", help="register the hook with Claude Code")
    hook_install.add_argument("--repo")
    hook_install.set_defaults(func=cmd_hook_install)

    enrich_p = sub.add_parser(
        "enrich", help="fetch package age metadata into the local cache"
    )
    enrich_p.add_argument("--repo")
    enrich_p.add_argument("--scope", choices=["staged", "worktree", "all"], default="all")
    enrich_p.add_argument("--workers", type=int, default=8)
    enrich_p.set_defaults(func=cmd_enrich)

    doctor = sub.add_parser("doctor", help="report environment and capabilities")
    doctor.add_argument("--verbose", "-v", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
