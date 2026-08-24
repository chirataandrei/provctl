"""Decision log: append-only, union-mergeable, forward-compatible."""

from __future__ import annotations

import json

from provctl.model import Finding, Origin, Reason, Verdict
from provctl.policy.decisions import Decision, DecisionLog, ensure_merge_driver


def make_finding(name="fake-pkg", reason=Reason.ABSENT_FROM_INDEX):
    return Finding(
        name=name, raw_name=name, origin=Origin.MANIFEST,
        verdict=Verdict.BLOCK, reason=reason, location="requirements.txt:1",
    )


class TestDecisionLog:
    def test_acknowledge_appends_one_line(self, tmp_path):
        log = DecisionLog(tmp_path / "decisions.jsonl")
        log.acknowledge(make_finding(), note="internal mirror", actor="dev@example.com")

        lines = (tmp_path / "decisions.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["name"] == "fake-pkg"
        assert record["note"] == "internal mirror"
        assert record["schema_version"] == 1

    def test_appends_never_rewrite(self, tmp_path):
        log = DecisionLog(tmp_path / "decisions.jsonl")
        log.acknowledge(make_finding("one"))
        log.acknowledge(make_finding("two"))
        log.acknowledge(make_finding("three"))
        assert len((tmp_path / "decisions.jsonl").read_text().strip().splitlines()) == 3
        assert len(log.acknowledged_fingerprints()) == 3

    def test_fingerprint_is_stable_across_line_moves(self):
        """Moving an import must not revoke an acknowledgement."""
        a = make_finding()
        b = make_finding()
        b.location = "requirements.txt:99"
        b.detail = "some other wording"
        assert a.fingerprint() == b.fingerprint()

    def test_fingerprint_differs_by_reason(self):
        a = make_finding(reason=Reason.ABSENT_FROM_INDEX)
        b = make_finding(reason=Reason.SUSPICIOUS_METADATA)
        assert a.fingerprint() != b.fingerprint()

    def test_unknown_fields_are_ignored_not_rejected(self, tmp_path):
        """Forward compatibility: fields may be added, never repurposed."""
        path = tmp_path / "decisions.jsonl"
        path.write_text(
            json.dumps({
                "schema_version": 99, "fingerprint": "abc123", "name": "x",
                "some_future_field": {"nested": True},
            }) + "\n",
            encoding="utf-8",
        )
        decisions = DecisionLog(path).load()
        assert len(decisions) == 1
        assert decisions[0].fingerprint == "abc123"

    def test_malformed_lines_are_skipped(self, tmp_path):
        path = tmp_path / "decisions.jsonl"
        path.write_text(
            "not json at all\n"
            + json.dumps({"fingerprint": "good1", "name": "x"}) + "\n"
            + "{broken\n",
            encoding="utf-8",
        )
        assert [d.fingerprint for d in DecisionLog(path).load()] == ["good1"]

    def test_conflict_markers_do_not_break_parsing(self, tmp_path):
        """If the union driver was not registered, degrade rather than crash."""
        path = tmp_path / "decisions.jsonl"
        path.write_text(
            "<<<<<<< HEAD\n"
            + json.dumps({"fingerprint": "ours", "name": "a"}) + "\n"
            + "=======\n"
            + json.dumps({"fingerprint": "theirs", "name": "b"}) + "\n"
            + ">>>>>>> branch\n",
            encoding="utf-8",
        )
        assert {d.fingerprint for d in DecisionLog(path).load()} == {"ours", "theirs"}

    def test_union_merge_semantics_are_order_independent(self, tmp_path):
        """Two branches appending different lines union to both, either way round."""
        ours = [Decision(1, "f1", "a", "manifest", "r", "block", "x", "t").to_json()]
        theirs = [Decision(1, "f2", "b", "manifest", "r", "block", "y", "t").to_json()]

        for combination in ([*ours, *theirs], [*theirs, *ours]):
            path = tmp_path / f"decisions-{len(combination)}-{combination[0][:12]}.jsonl"
            path.write_text("\n".join(combination) + "\n", encoding="utf-8")
            assert DecisionLog(path).acknowledged_fingerprints() == {"f1", "f2"}

    def test_no_free_text_from_agents_is_recorded(self, tmp_path):
        """Constraint 5: prompt text must never reach disk through this path."""
        from provctl.model import ProvenanceSummary

        finding = make_finding()
        finding.provenance = ProvenanceSummary(
            state="agent", tool="claude", model="claude-opus-5", session_id="abc",
        )
        log = DecisionLog(tmp_path / "decisions.jsonl")
        log.acknowledge(finding)

        record = json.loads((tmp_path / "decisions.jsonl").read_text().strip())
        assert set(record["provenance"]) <= {
            "state", "tool", "model", "session_id", "human_reviewed", "source"
        }


class TestMergeDriver:
    def test_registers_union_driver(self, tmp_path):
        assert ensure_merge_driver(tmp_path) is True
        content = (tmp_path / ".gitattributes").read_text()
        assert ".provenance/decisions.jsonl merge=union" in content

    def test_is_idempotent(self, tmp_path):
        ensure_merge_driver(tmp_path)
        assert ensure_merge_driver(tmp_path) is False
        content = (tmp_path / ".gitattributes").read_text()
        assert content.count("decisions.jsonl") == 1

    def test_preserves_existing_gitattributes(self, tmp_path):
        (tmp_path / ".gitattributes").write_text("*.py text eol=lf\n", encoding="utf-8")
        ensure_merge_driver(tmp_path)
        content = (tmp_path / ".gitattributes").read_text()
        assert "*.py text eol=lf" in content
        assert "decisions.jsonl" in content

    def test_appends_newline_when_file_lacks_trailing_one(self, tmp_path):
        (tmp_path / ".gitattributes").write_text("*.py text", encoding="utf-8")
        ensure_merge_driver(tmp_path)
        lines = (tmp_path / ".gitattributes").read_text().splitlines()
        assert lines[0] == "*.py text"
