"""Human and machine output."""

from __future__ import annotations

import json
import sys
from typing import TextIO

from provctl.model import CheckResult, Origin, Reason, Verdict

_SYMBOL = {
    Verdict.BLOCK: "BLOCK",
    Verdict.WARN: "WARN ",
    Verdict.INFO: "INFO ",
    Verdict.OK: "OK   ",
}

_USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in __import__("os").environ
_COLOR = {
    Verdict.BLOCK: "\033[31m",
    Verdict.WARN: "\033[33m",
    Verdict.INFO: "\033[36m",
    Verdict.OK: "\033[32m",
}
_RESET = "\033[0m"
_DIM = "\033[2m"


def _paint(text: str, verdict: Verdict) -> str:
    if not _USE_COLOR:
        return text
    return f"{_COLOR[verdict]}{text}{_RESET}"


def _dim(text: str) -> str:
    return f"{_DIM}{text}{_RESET}" if _USE_COLOR else text


def render_json(result: CheckResult, stream: TextIO) -> None:
    json.dump(result.to_dict(), stream, indent=2, sort_keys=False)
    stream.write("\n")


def render_human(result: CheckResult, stream: TextIO, *, verbose: bool = False) -> None:
    shown = [f for f in result.findings if f.verdict is not Verdict.OK]
    if not verbose:
        shown = [f for f in shown if f.verdict is not Verdict.INFO]

    for warning in result.warnings:
        stream.write(_dim(f"note: {warning}\n"))

    if not shown:
        checked = result.bands.total
        stream.write(f"provctl: no dependency risks in {checked} checked name(s)\n")
        return

    for finding in shown:
        stream.write(f"{_paint(_SYMBOL[finding.verdict], finding.verdict)} {finding.name}")
        if finding.origin is Origin.IMPORT and finding.raw_name != finding.name:
            stream.write(f" {_dim('(import ' + finding.raw_name + ')')}")
        stream.write("\n")
        if finding.detail:
            stream.write(f"       {finding.detail}\n")
        if finding.location:
            stream.write(_dim(f"       at {finding.location}\n"))
        if finding.provenance is not None and finding.provenance.state != "untracked":
            prov = finding.provenance
            who = prov.tool or prov.state
            model = f" / {prov.model}" if prov.model else ""
            reviewed = "reviewed by a human" if prov.human_reviewed else "not human-reviewed"
            stream.write(_dim(f"       provenance: {who}{model}, {reviewed}\n"))

    stream.write("\n")
    _render_summary(result, stream)

    if result.blocking:
        stream.write("\nThis commit is blocked. To proceed:\n")
        for finding in result.blocking:
            if finding.reason is Reason.ABSENT_FROM_INDEX:
                stream.write(
                    f"  - '{finding.name}' does not exist on PyPI. If the name is a typo, fix it.\n"
                    f"    If it is an internal package, add it to .provenance/policy.toml:\n"
                    f"        [first_party]\n        allow = [\"{finding.name}\"]\n"
                )
            else:
                stream.write(
                    f"  - '{finding.name}': {finding.detail}\n"
                    f"    Acknowledge with: provctl ack {finding.name}\n"
                )


def _render_summary(result: CheckResult, stream: TextIO) -> None:
    bands = result.bands
    parts = [f"{bands.total} name(s) checked"]
    if bands.hard_absent:
        parts.append(f"{bands.hard_absent} absent from PyPI")
    if bands.first_party_suppressed:
        parts.append(f"{bands.first_party_suppressed} first-party")
    if bands.private_index_suppressed:
        parts.append(f"{bands.private_index_suppressed} unverifiable (private index)")
    if bands.unresolvable_imports:
        parts.append(f"{bands.unresolvable_imports} unresolvable import(s)")
    if bands.ambiguous_imports:
        parts.append(f"{bands.ambiguous_imports} ambiguous mapping(s)")
    stream.write(_dim("summary: " + ", ".join(parts) + "\n"))


def render_bands(result: CheckResult, stream: TextIO) -> None:
    """Band report -- the measurement that validates or kills the thesis."""
    bands = result.bands
    total = bands.total or 1
    stream.write("Provenance band analysis\n")
    stream.write(f"  findings total            {bands.total}\n")
    stream.write(f"  hard absent (blocking)    {bands.hard_absent}\n")
    stream.write(
        f"  provenance-sensitive      {bands.provenance_sensitive} "
        f"({100 * bands.provenance_sensitive / total:.1f}% of findings)\n"
    )
    stream.write(f"  first-party suppressed    {bands.first_party_suppressed}\n")
    stream.write(f"  private-index suppressed  {bands.private_index_suppressed}\n")
    stream.write(f"  unresolvable imports      {bands.unresolvable_imports}\n")
    stream.write(f"  ambiguous mappings        {bands.ambiguous_imports}\n")
