"""Provenance adapters and the invariants that keep them honest."""

from __future__ import annotations

import json

from provctl.provenance.gitai import _parse_range, _scrub_session
from provctl.provenance.hook import (
    AgentEvent,
    HookProvenanceReader,
    parse_hook_payload,
    record_event,
)
from provctl.provenance.model import LineProvenance, ProvenanceMap, State


class TestScrubbing:
    def test_prompt_text_is_dropped_at_the_boundary(self):
        """Constraint 5. An allow-list, so new upstream fields cannot leak."""
        scrubbed = _scrub_session({
            "tool": "claude-code",
            "model": "claude-opus-5",
            "id": "sess-123",
            "prompt": "here is my proprietary source code ...",
            "messages": [{"role": "user", "content": "secret"}],
            "diff": "--- a/secret.py",
            "some_future_free_text_field": "leaked?",
        })
        assert scrubbed == {"tool": "claude-code", "model": "claude-opus-5", "id": "sess-123"}
        assert "prompt" not in scrubbed
        assert "messages" not in scrubbed
        assert "some_future_free_text_field" not in scrubbed

    def test_non_scalar_values_are_dropped(self):
        assert _scrub_session({"tool": {"nested": "object"}}) == {}

    def test_non_dict_input_is_safe(self):
        assert _scrub_session("not a dict") == {}


class TestRangeParsing:
    def test_single_line_and_span(self):
        assert _parse_range("12") == (12, 12)
        assert _parse_range("12-18") == (12, 18)

    def test_garbage_returns_none(self):
        assert _parse_range("not-a-range") is None
        assert _parse_range("") is None


class TestProvenanceMap:
    def test_lookup_by_line(self):
        provenance = ProvenanceMap(source="test")
        provenance.add(LineProvenance("a.py", 5, 10, State.AGENT, tool="claude"))

        assert provenance.lookup("a.py", 7) is not None
        assert provenance.lookup("a.py", 4) is None
        assert provenance.lookup("b.py", 7) is None

    def test_missing_line_summarizes_as_untracked(self):
        """INVARIANT: absence of data is 'untracked', never 'human'."""
        summary = ProvenanceMap().summarize("a.py", 1)
        assert summary.state == "untracked"
        assert summary.human_reviewed is False

    def test_overridden_agent_line_counts_as_human_reviewed(self):
        provenance = ProvenanceMap()
        provenance.add(
            LineProvenance("a.py", 1, 1, State.AGENT, tool="claude", overridden=True)
        )
        assert provenance.summarize("a.py", 1).human_reviewed is True

    def test_model_string_is_preserved_verbatim(self):
        """Never parsed for meaning: it is an unattested self-report."""
        provenance = ProvenanceMap()
        provenance.add(
            LineProvenance("a.py", 1, 1, State.AGENT, model="some-weird/model:v2")
        )
        assert provenance.summarize("a.py", 1).model == "some-weird/model:v2"


class TestHookPayload:
    def test_extracts_structure_only(self, tmp_path):
        (tmp_path / "app.py").write_text("import os\nimport requests\n", encoding="utf-8")
        events = parse_hook_payload(
            {
                "tool_name": "Edit",
                "session_id": "sess-abc",
                "tool_input": {
                    "file_path": str(tmp_path / "app.py"),
                    "new_string": "import requests",
                },
            },
            repo_root=tmp_path,
        )
        assert len(events) == 1
        assert events[0].file == "app.py"
        assert events[0].session_id == "sess-abc"
        # The recorded event carries no content anywhere in its serialization.
        assert "import requests" not in events[0].to_json()

    def test_ignores_read_only_tools(self, tmp_path):
        assert parse_hook_payload(
            {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "a.py")}},
            repo_root=tmp_path,
        ) == []

    def test_ignores_writes_outside_the_repo(self, tmp_path):
        assert parse_hook_payload(
            {"tool_name": "Edit", "tool_input": {"file_path": "/etc/passwd",
                                                 "new_string": "x"}},
            repo_root=tmp_path,
        ) == []

    def test_multiline_insert_spans_lines(self, tmp_path):
        (tmp_path / "a.py").write_text("first\nsecond\nthird\n", encoding="utf-8")
        events = parse_hook_payload(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(tmp_path / "a.py"),
                               "content": "first\nsecond\nthird"},
            },
            repo_root=tmp_path,
        )
        assert events[0].end_line - events[0].start_line == 2

    def test_malformed_payload_yields_nothing(self, tmp_path):
        assert parse_hook_payload({}, repo_root=tmp_path) == []
        assert parse_hook_payload({"tool_name": "Edit"}, repo_root=tmp_path) == []


class TestHookReader:
    def test_round_trip(self, tmp_path):
        git_dir = tmp_path / ".git"
        record_event(git_dir, AgentEvent(
            schema_version=1, timestamp="2026-08-02T00:00:00+00:00",
            file="requirements.txt", start_line=3, end_line=3,
            tool="Edit", model="claude-opus-5", session_id="s1",
        ))
        provenance = HookProvenanceReader(git_dir).load()
        summary = provenance.summarize("requirements.txt", 3)
        assert summary.state == "agent"
        assert summary.model == "claude-opus-5"
        assert summary.source == "provctl-hook"

    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert HookProvenanceReader(tmp_path / ".git").load().is_empty

    def test_corrupt_lines_are_skipped(self, tmp_path):
        git_dir = tmp_path / ".git"
        path = git_dir / "provctl" / "agent-events.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            "garbage\n"
            + json.dumps({"file": "a.py", "start_line": 1, "end_line": 1}) + "\n",
            encoding="utf-8",
        )
        assert len(HookProvenanceReader(git_dir).load()) == 1
