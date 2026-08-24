"""Normalized provenance, independent of which adapter produced it."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from provctl.model import ProvenanceSummary


class State(str, Enum):
    AGENT = "agent"
    KNOWN_HUMAN = "known_human"
    UNTRACKED = "untracked"


@dataclass(frozen=True)
class LineProvenance:
    """Provenance for a contiguous run of lines in one file."""

    file: str
    start_line: int
    end_line: int
    state: State
    tool: str | None = None
    model: str | None = None
    session_id: str | None = None
    overridden: bool = False  # agent line later edited by a human
    source: str = ""

    def covers(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line


UNTRACKED = ProvenanceSummary(state=State.UNTRACKED.value)


class ProvenanceMap:
    """Per-file line ranges, queried by (file, line)."""

    def __init__(self, source: str = ""):
        self._by_file: dict[str, list[LineProvenance]] = {}
        self.source = source

    def add(self, entry: LineProvenance) -> None:
        self._by_file.setdefault(entry.file, []).append(entry)

    def lookup(self, file: str, line: int) -> LineProvenance | None:
        for entry in self._by_file.get(file, []):
            if entry.covers(line):
                return entry
        return None

    def summarize(self, file: str, line: int) -> ProvenanceSummary:
        entry = self.lookup(file, line)
        if entry is None:
            return UNTRACKED
        return ProvenanceSummary(
            state=entry.state.value,
            tool=entry.tool,
            # Model strings are recorded verbatim and never parsed for meaning.
            # They are unattested self-reports, so inferring "this is a weak
            # model" from one would be building policy on an attacker-controlled
            # string.
            model=entry.model,
            session_id=entry.session_id,
            human_reviewed=entry.overridden or entry.state is State.KNOWN_HUMAN,
            source=entry.source or self.source,
        )

    def __len__(self) -> int:
        return sum(len(entries) for entries in self._by_file.values())

    @property
    def is_empty(self) -> bool:
        return len(self) == 0
