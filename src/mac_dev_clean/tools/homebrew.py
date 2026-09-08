from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from ..recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    ToolAction,
)
from .runner import MAX_STDERR_CHARS, ToolRunner, ToolUnavailable
from .sizes import parse_tool_size

BREW_PREVIEW_ARGV = ("brew", "cleanup", "-n")

#: Plain `brew cleanup` only. `--prune` and `-s` also remove the download cache
#: for *currently installed* versions, which is a different and larger effect
#: than the preview describes.
BREW_CLEANUP_ARGV = ("brew", "cleanup")

_SUMMARY_PREFIX = "==> This operation would free approximately "
_TOTAL = re.compile(
    r"^==> This operation would free approximately "
    r"([0-9]+(?:\.[0-9]+)?\s*(?:TB|GB|MB|KB|B)) of disk space\.$",
    re.IGNORECASE,
)
_TRUNCATION_MARKER = "...[truncated]"


def _bounded_reason(reason: str) -> str:
    if len(reason) <= MAX_STDERR_CHARS:
        return reason
    retained = MAX_STDERR_CHARS - len(_TRUNCATION_MARKER)
    return reason[:retained] + _TRUNCATION_MARKER


def parse_brew_preview(stdout: str) -> Tuple[int, int]:
    """Read ``brew cleanup -n`` output into total bytes and removal count."""
    total = None  # type: Optional[int]
    count = 0
    ambiguous_summary = False
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Would remove:"):
            count += 1
            continue
        if not stripped.lower().startswith(_SUMMARY_PREFIX.lower()):
            continue
        match = _TOTAL.fullmatch(stripped)
        if match is None or total is not None:
            ambiguous_summary = True
            continue
        parsed_total = parse_tool_size(match.group(1))
        if parsed_total <= 0:
            ambiguous_summary = True
            continue
        total = parsed_total
    if ambiguous_summary or total is None:
        return 0, count
    return total, count


def analyze_homebrew(
    runner: ToolRunner, generation: int, home: Path
) -> Tuple[List[Recommendation], Optional[str]]:
    """Ask Homebrew what it would remove without performing cleanup."""
    try:
        result = runner(BREW_PREVIEW_ARGV)
    except ToolUnavailable as exc:
        return [], _bounded_reason(exc.reason)
    if not result.ok:
        stderr = result.stderr.strip()
        if stderr:
            return [], _bounded_reason(stderr)
        detail = result.stdout.strip()
        if detail:
            return [], _bounded_reason(detail)
        return [], _bounded_reason("brew exited with {}".format(result.exit_code))
    total, count = parse_brew_preview(result.stdout)
    if total <= 0 and count == 0:
        return [], None
    if total <= 0:
        plural = "" if count == 1 else "s"
        reason = (
            "brew reported {} removal{} but no unique valid positive cleanup total"
        ).format(count, plural)
        return [], _bounded_reason(reason)

    evidence = (
        Evidence("preview-command", " ".join(BREW_PREVIEW_ARGV)),
        Evidence("tool-report", "brew would remove {} items".format(count)),
    )
    item = Recommendation(
        detector_id="homebrew-cleanup",
        category="tool-managed",
        label="Homebrew removable versions and downloads",
        path=home,
        action=ActionKind.INVOKE_TOOL,
        allocated_bytes=total,
        reclaimable_bytes=total,
        confidence=Confidence.EXACT,
        restoration=RestorationCost.REDOWNLOAD,
        selected_by_default=False,
        evidence=evidence,
        safety_root=home,
        reason="Homebrew reports old installed versions and stale downloads it can remove.",
        generation=generation,
        warning="Reinstalling an older version afterwards requires downloading it again.",
        tool_action=ToolAction(
            tool="brew",
            resource="cleanup",
            argv=BREW_CLEANUP_ARGV,
            preview_argv=BREW_PREVIEW_ARGV,
            reported="{} items".format(count),
        ),
    )
    return [item], None
