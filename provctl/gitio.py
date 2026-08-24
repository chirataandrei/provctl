"""Git plumbing: which files changed, and which lines are new."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT = 60


class GitError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path, *, check: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            timeout=GIT_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found") from exc
    except subprocess.SubprocessError as exc:
        raise GitError(f"git {' '.join(args)} failed: {exc}") from exc
    if check and proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} exited {proc.returncode}")
    return proc.stdout


@dataclass
class ChangeSet:
    """Files touched, and the line numbers added in each."""

    files: list[str]
    added_lines: dict[str, set[int]]
    mode: str

    def lines_for(self, path: str) -> set[int]:
        return self.added_lines.get(path, set())

    def is_fully_new(self, path: str) -> bool:
        return self.mode == "all"


def _parse_numstat_names(output: str) -> list[str]:
    names = []
    for line in output.splitlines():
        line = line.strip()
        if line:
            names.append(line)
    return names


def _parse_added_lines(diff: str) -> dict[str, set[int]]:
    """Parse `-U0` unified diff hunk headers into added line numbers."""
    added: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/"):].strip()
            added.setdefault(current, set())
        elif line.startswith("+++ /dev/null"):
            current = None
        elif line.startswith("@@") and current is not None:
            # @@ -old,count +new,count @@
            try:
                plus = line.split("+", 1)[1].split("@@", 1)[0].strip()
            except IndexError:
                continue
            if "," in plus:
                start_s, count_s = plus.split(",", 1)
            else:
                start_s, count_s = plus, "1"
            try:
                start, count = int(start_s), int(count_s)
            except ValueError:
                continue
            added[current].update(range(start, start + count))
    return added


def staged_changes(repo: Path) -> ChangeSet:
    """Files and added lines in the index (what `git commit` would record)."""
    names = _parse_numstat_names(
        _run(["diff", "--cached", "--name-only", "--diff-filter=ACMR"], repo)
    )
    diff = _run(["diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR"], repo)
    return ChangeSet(files=names, added_lines=_parse_added_lines(diff), mode="staged")


def worktree_changes(repo: Path) -> ChangeSet:
    """Files and added lines in the working tree relative to HEAD."""
    names = _parse_numstat_names(
        _run(["diff", "HEAD", "--name-only", "--diff-filter=ACMR"], repo, check=False)
    )
    diff = _run(["diff", "HEAD", "-U0", "--no-color", "--diff-filter=ACMR"], repo, check=False)
    return ChangeSet(files=names, added_lines=_parse_added_lines(diff), mode="worktree")


def range_changes(repo: Path, rev_range: str) -> ChangeSet:
    names = _parse_numstat_names(
        _run(["diff", rev_range, "--name-only", "--diff-filter=ACMR"], repo)
    )
    diff = _run(["diff", rev_range, "-U0", "--no-color", "--diff-filter=ACMR"], repo)
    return ChangeSet(files=names, added_lines=_parse_added_lines(diff), mode="range")


def all_tracked_files(repo: Path) -> ChangeSet:
    """Every tracked file, with all lines treated as new."""
    names = _parse_numstat_names(_run(["ls-files"], repo))
    return ChangeSet(files=names, added_lines={}, mode="all")


def file_content(repo: Path, path: str, *, staged: bool) -> str | None:
    """Read a file's content, from the index when staged."""
    if staged:
        try:
            return _run(["show", f":{path}"], repo)
        except GitError:
            return None
    full = repo / path
    try:
        return full.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def is_git_repo(path: Path) -> bool:
    try:
        _run(["rev-parse", "--git-dir"], path)
        return True
    except GitError:
        return False
