"""git-ai adapter. Optional subprocess, never a Python dependency."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from provctl.provenance.model import LineProvenance, ProvenanceMap, State

GITAI_TIMEOUT = 30

# The only keys ever copied out of git-ai's output. Everything else -- prompts,
# messages, diffs, file contents -- is discarded at this boundary.
_ALLOWED_SESSION_FIELDS = frozenset({"tool", "model", "id", "session_id", "conversation_id"})


@dataclass(frozen=True)
class Capabilities:
    version: str | None = None
    has_status_json: bool = False
    has_dirty_blame: bool = False
    note: str = ""


def _scrub_session(raw: dict) -> dict:
    """Allow-list the fields we are willing to record."""
    if not isinstance(raw, dict):
        return {}
    scrubbed = {}
    for key, value in raw.items():
        if key in _ALLOWED_SESSION_FIELDS and isinstance(value, (str, int)):
            scrubbed[key] = str(value)
    return scrubbed


class GitAiAdapter:
    def __init__(self, repo: Path, executable: str | None = None):
        self.repo = repo
        self._executable = executable or shutil.which("git-ai")

    @property
    def available(self) -> bool:
        return self._executable is not None

    def _run(self, args: list[str]) -> tuple[int, str, str]:
        if not self._executable:
            return 127, "", "git-ai not installed"
        try:
            proc = subprocess.run(
                [self._executable, *args], cwd=str(self.repo),
                capture_output=True, text=True, timeout=GITAI_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, "", str(exc)
        return proc.returncode, proc.stdout, proc.stderr

    def capabilities(self) -> Capabilities:
        """Probe what this git-ai build supports. Used by `provctl doctor`."""
        if not self.available:
            return Capabilities(note="git-ai not on PATH")

        code, out, _ = self._run(["--version"])
        version = out.strip() if code == 0 else None

        code, out, _ = self._run(["status", "--json"])
        has_status = code == 0 and out.strip().startswith("{")

        # Probe the known-broken path rather than assuming: a future release may
        # fix it, and we would rather detect that than keep working around it.
        code, _out, err = self._run(["blame", "--json", "--contents", "-", "."])
        has_dirty_blame = code == 0 and "cannot use --contents" not in err

        return Capabilities(
            version=version,
            has_status_json=has_status,
            has_dirty_blame=has_dirty_blame,
            note="" if has_dirty_blame else "blame --contents - is broken upstream",
        )

    def working_tree_summary(self) -> dict | None:
        """Repo-wide uncommitted attribution counts, or None if unavailable."""
        code, out, _ = self._run(["status", "--json"])
        if code != 0 or not out.strip():
            return None
        try:
            document = json.loads(out)
        except json.JSONDecodeError:
            return None

        stats = document.get("stats", {}) or {}
        checkpoints = document.get("checkpoints", []) or []
        tools = []
        for checkpoint in checkpoints:
            tool_model = checkpoint.get("tool_model")
            if isinstance(tool_model, str) and tool_model:
                tools.append(tool_model)

        return {
            "ai_additions": stats.get("ai_additions", 0),
            "human_additions": stats.get("human_additions", 0),
            "unknown_additions": stats.get("unknown_additions", 0),
            "ai_accepted": stats.get("ai_accepted", 0),
            "tools": sorted(set(tools)),
        }

    def committed_provenance(self, rev: str = "HEAD") -> ProvenanceMap:
        """Per-hunk attribution for a commit, via `git ai diff --json`."""
        provenance = ProvenanceMap(source="git-ai")
        code, out, _ = self._run(["diff", "--json", rev])
        if code != 0 or not out.strip():
            return provenance

        try:
            document = json.loads(out)
        except json.JSONDecodeError:
            return provenance

        # Note: we read `annotations` and `hunks`, and deliberately never touch
        # `prompts`, which is where message bodies live.
        sessions = {
            key: _scrub_session(value)
            for key, value in (document.get("sessions") or {}).items()
        }

        for file_path, file_data in (document.get("files") or {}).items():
            annotations = (file_data or {}).get("annotations") or {}
            for line_range, session_key in annotations.items():
                bounds = _parse_range(line_range)
                if bounds is None:
                    continue
                start, end = bounds
                session = sessions.get(session_key, {})
                provenance.add(
                    LineProvenance(
                        file=file_path,
                        start_line=start,
                        end_line=end,
                        state=State.AGENT if session else State.UNTRACKED,
                        tool=session.get("tool"),
                        model=session.get("model"),
                        session_id=session.get("id") or session.get("session_id"),
                        source="git-ai",
                    )
                )
        return provenance


def _parse_range(value: str) -> tuple[int, int] | None:
    """Parse "12" or "12-18" into inclusive bounds."""
    try:
        if "-" in value:
            start_s, end_s = value.split("-", 1)
            return int(start_s), int(end_s)
        line = int(value)
        return line, line
    except (ValueError, AttributeError):
        return None
