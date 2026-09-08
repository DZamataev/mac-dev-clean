from __future__ import annotations

import json
from dataclasses import dataclass
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
from .runner import (
    MAX_STDERR_CHARS,
    ToolRunner,
    ToolUnavailable,
    inventory_argv_matches,
)
from .sizes import parse_tool_size

DOCKER_PREVIEW_ARGV = ("docker", "system", "df", "--format", "{{json .}}")
_TRUNCATION_MARKER = "...[truncated]"


def _bounded_reason(reason: str) -> str:
    if len(reason) <= MAX_STDERR_CHARS:
        return reason
    retained = MAX_STDERR_CHARS - len(_TRUNCATION_MARKER)
    return reason[:retained] + _TRUNCATION_MARKER


@dataclass(frozen=True)
class _ResourceClass:
    docker_type: str
    resource: str
    label: str
    argv: Tuple[str, ...]
    target: str
    note: str
    confidence: Confidence


#: `docker image prune -a` is deliberately absent. It removes every image not
#: attached to a *running* container, which is far broader than the
#: `Reclaimable` figure `system df` reports and would delete images the user
#: pulled on purpose. Offering it would break the rule that an executed vector
#: matches the effect its preview described.
_CLASSES = (
    _ResourceClass(
        docker_type="Build Cache",
        resource="build-cache",
        label="Docker default-prune build cache",
        argv=("docker", "builder", "prune", "-f"),
        target="build cache eligible for Docker's default builder prune",
        note=(
            "Build cache eligible for Docker's default builder prune. Docker "
            "rebuilds removed cache on the next build."
        ),
        confidence=Confidence.HEURISTIC,
    ),
    _ResourceClass(
        docker_type="Images",
        resource="images",
        label="Docker dangling images",
        argv=("docker", "image", "prune", "-f"),
        target="dangling images",
        note="Dangling images. Tagged images and images in use are not touched.",
        confidence=Confidence.HEURISTIC,
    ),
    _ResourceClass(
        docker_type="Containers",
        resource="containers",
        label="Docker stopped containers",
        argv=("docker", "container", "prune", "-f"),
        target="stopped containers",
        note="Stopped containers. Running containers are not touched.",
        confidence=Confidence.EXACT,
    ),
    _ResourceClass(
        docker_type="Local Volumes",
        resource="volumes",
        label="Docker unused anonymous volumes",
        argv=("docker", "volume", "prune", "-f"),
        target="unused anonymous volumes",
        note=(
            "Unused anonymous volumes with no container references. Volume data "
            "cannot be recovered."
        ),
        confidence=Confidence.HEURISTIC,
    ),
)

_CLASSES_BY_TYPE = {item.docker_type: item for item in _CLASSES}
_CLASS_ORDER = {item.resource: index for index, item in enumerate(_CLASSES)}


def analyze_docker(
    runner: ToolRunner, generation: int, home: Path
) -> Tuple[List[Recommendation], Optional[str]]:
    """Ask Docker what it can reclaim, without starting it.

    Returns `(recommendations, unavailable_reason)`. A stopped daemon or a
    missing binary is reported as a reason string, never as an exception:
    "Docker is not answering" is information the user needs, not a scan
    failure.
    """
    try:
        result = runner(DOCKER_PREVIEW_ARGV)
    except ToolUnavailable as exc:
        return [], _bounded_reason(exc.reason)

    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip()
        reason = detail or "docker exited with {}".format(result.exit_code)
        return [], _bounded_reason(reason)
    if not inventory_argv_matches(result.argv, DOCKER_PREVIEW_ARGV):
        return [], _bounded_reason(
            "docker inventory provenance did not match requested command"
        )

    items: List[Recommendation] = []
    seen_resources = set()
    duplicated_resources = set()
    for line in result.lines():
        try:
            payload = json.loads(line)
        except ValueError:
            # One unreadable line must not cost us the other classes.
            continue
        if not isinstance(payload, dict):
            continue

        docker_type = payload.get("Type")
        if not isinstance(docker_type, str):
            continue
        spec = _CLASSES_BY_TYPE.get(docker_type)
        if spec is None:
            continue
        if spec.resource in seen_resources:
            duplicated_resources.add(spec.resource)
            continue
        seen_resources.add(spec.resource)

        reported = payload.get("Reclaimable")
        total = payload.get("Size")
        if not isinstance(reported, str) or not isinstance(total, str):
            continue
        reclaimable = parse_tool_size(reported)
        allocated = parse_tool_size(total)
        if reclaimable <= 0 or allocated <= 0:
            continue

        if spec.confidence is Confidence.EXACT:
            report_detail = "docker reports {} reclaimable".format(reported)
            warning = (
                "Docker reports this figure. Freed space may not appear on the "
                "volume until Docker compacts its disk image."
            )
        else:
            report_detail = (
                "docker reports {} reclaimable class-wide; this is an upper bound "
                "for {}"
            ).format(reported, spec.target)
            warning = (
                "Docker's class-wide figure is an upper bound for {}; actual "
                "reclaimed space may be lower. Freed space may not "
                "appear on the volume until Docker compacts its disk image."
            ).format(spec.target)
        evidence = (
            Evidence("tool-report", report_detail),
            Evidence("tool-total", "docker reports {} in total".format(total)),
            Evidence("preview-command", " ".join(DOCKER_PREVIEW_ARGV)),
        )

        items.append(
            Recommendation(
                detector_id="docker-{}".format(spec.resource),
                category="tool-managed",
                label=spec.label,
                path=home,
                action=ActionKind.INVOKE_TOOL,
                allocated_bytes=allocated,
                reclaimable_bytes=reclaimable,
                confidence=spec.confidence,
                restoration=RestorationCost.EXTERNAL_STATE,
                selected_by_default=False,
                evidence=evidence,
                safety_root=home,
                reason=spec.note,
                generation=generation,
                warning=warning,
                tool_action=ToolAction(
                    tool="docker",
                    resource=spec.resource,
                    argv=spec.argv,
                    preview_argv=DOCKER_PREVIEW_ARGV,
                    reported=reported,
                ),
            )
        )

    unambiguous = [
        item
        for item in items
        if item.tool_action.resource not in duplicated_resources
    ]
    unambiguous.sort(key=lambda item: _CLASS_ORDER[item.tool_action.resource])
    return unambiguous, None
