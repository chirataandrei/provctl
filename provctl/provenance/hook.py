"""Our own agent hook: the primary per-line provenance source."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from provctl.provenance.model import LineProvenance, ProvenanceMap, State

EVENTS_RELATIVE = "provctl/agent-events.jsonl"
EVENTS_SCHEMA_VERSION = 1

# Tools that insert text. Read-only tools carry no authorship implication.
_WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "str_replace_editor"}


@dataclass
class AgentEvent:
    """One agent write. Contains no file content and no prompt text."""

    schema_version: int
    timestamp: str
    file: str
    start_line: int
    end_line: int
    tool: str | None = None
    model: str | None = None
    session_id: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def events_path(git_dir: Path) -> Path:
    return git_dir / EVENTS_RELATIVE


def record_event(git_dir: Path, event: AgentEvent) -> None:
    path = events_path(git_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(event.to_json() + "\n")


def parse_hook_payload(payload: dict, *, repo_root: Path) -> list[AgentEvent]:
    """Turn a Claude Code PostToolUse payload into events."""
    tool_name = payload.get("tool_name") or payload.get("tool")
    if tool_name not in _WRITE_TOOLS:
        return []

    tool_input = payload.get("tool_input") or {}
    raw_path = tool_input.get("file_path") or tool_input.get("path")
    if not raw_path:
        return []

    try:
        relative = str(Path(raw_path).resolve().relative_to(repo_root.resolve()))
    except (ValueError, OSError):
        # A write outside the repository is not our concern.
        return []

    inserted = tool_input.get("new_string")
    if inserted is None:
        inserted = tool_input.get("content", "")
    line_count = max(1, str(inserted).count("\n") + 1) if inserted else 1

    start = _resolve_start_line(payload, tool_input, relative, repo_root, inserted)

    return [
        AgentEvent(
            schema_version=EVENTS_SCHEMA_VERSION,
            timestamp=datetime.now(timezone.utc).isoformat(),
            file=relative,
            start_line=start,
            end_line=start + line_count - 1,
            # Self-reported by the agent and never validated -- see
            # docs/threat-model.md. Recorded verbatim, never parsed for meaning.
            tool=_as_str(payload.get("tool_name")),
            model=_as_str(payload.get("model")),
            session_id=_as_str(payload.get("session_id")),
        )
    ]


def _resolve_start_line(payload, tool_input, relative, repo_root, inserted) -> int:
    """Find where the insertion landed."""
    if not inserted:
        return 1
    try:
        content = (repo_root / relative).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 1

    first_line = str(inserted).split("\n", 1)[0].strip()
    if not first_line:
        return 1
    for number, line in enumerate(content.splitlines(), start=1):
        if first_line in line:
            return number
    return 1


def _as_str(value) -> str | None:
    return str(value) if isinstance(value, (str, int)) and str(value) else None


class HookProvenanceReader:
    """Read recorded agent events into a ProvenanceMap."""

    def __init__(self, git_dir: Path):
        self.path = events_path(git_dir)

    def load(self) -> ProvenanceMap:
        provenance = ProvenanceMap(source="provctl-hook")
        if not self.path.exists():
            return provenance
        try:
            content = self.path.read_text(encoding="utf-8")
        except OSError:
            return provenance

        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict) or "file" not in raw:
                continue
            provenance.add(
                LineProvenance(
                    file=raw["file"],
                    start_line=int(raw.get("start_line", 1)),
                    end_line=int(raw.get("end_line", 1)),
                    state=State.AGENT,
                    tool=raw.get("tool"),
                    model=raw.get("model"),
                    session_id=raw.get("session_id"),
                    source="provctl-hook",
                )
            )
        return provenance

    def clear(self) -> None:
        """Drop recorded events, e.g. after a successful commit."""
        if self.path.exists():
            self.path.unlink()


CLAUDE_HOOK_SETTINGS = {
    "hooks": {
        "PostToolUse": [
            {
                "matcher": "Edit|Write|MultiEdit",
                "hooks": [{"type": "command", "command": "provctl hook record"}],
            }
        ]
    }
}


def hook_main(stdin_text: str, repo_root: Path, git_dir: Path) -> int:
    """Entry point for `provctl hook record`."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
        for event in parse_hook_payload(payload, repo_root=repo_root):
            record_event(git_dir, event)
    except Exception:
        return 0
    return 0
