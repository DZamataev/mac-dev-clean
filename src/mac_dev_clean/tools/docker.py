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
from .runner import ToolRunner, ToolUnavailable
from .sizes import parse_tool_size

DOCKER_PREVIEW_ARGV = ("docker", "system", "df", "--format", "{{json .}}")


@dataclass(frozen=True)
class _ResourceClass:
    docker_type: str
    resource: str
    label: str
    argv: Tuple[str, ...]
    note: str


#: `docker image prune -a` is deliberately absent. It removes every image not
#: attached to a *running* container, which is far broader than the
#: `Reclaimable` figure `system df` reports and would delete images the user
#: pulled on purpose. Offering it would break the rule that an executed vector
#: matches the effect its preview described.
_CLASSES = (
    _ResourceClass(
        docker_type="Build Cache",
        resource="build-cache",
        label="Docker build cache",
        argv=("docker", "builder", "prune", "-f"),
        note="Layer cache from previous builds. Docker rebuilds it on the next build.",
    ),
    _ResourceClass(
        docker_type="Images",
        resource="images",
        label="Docker dangling images",
        argv=("docker", "image", "prune", "-f"),
        note="Untagged images. Tagged images and images in use are not touched.",
    ),
    _ResourceClass(
        docker_type="Containers",
        resource="containers",
        label="Docker stopped containers",
        argv=("docker", "container", "prune", "-f"),
        note="Stopped containers. Running containers are not touched.",
    ),
    _ResourceClass(
        docker_type="Local Volumes",
        resource="volumes",
        label="Docker unused volumes",
        argv=("docker", "volume", "prune", "-f"),
        note="Volumes no container references. Volume data cannot be recovered.",
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
        return [], exc.reason

    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip()
        return [], detail or "docker exited with {}".format(result.exit_code)

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

        spec = _CLASSES_BY_TYPE.get(str(payload.get("Type", "")))
        if spec is None:
            continue
        if spec.resource in seen_resources:
            duplicated_resources.add(spec.resource)
            continue
        seen_resources.add(spec.resource)

        reported = str(payload.get("Reclaimable", ""))
        reclaimable = parse_tool_size(reported)
        if reclaimable <= 0:
            continue

        total = str(payload.get("Size", ""))
        evidence = (
            Evidence("tool-report", "docker reports {} reclaimable".format(reported)),
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
                allocated_bytes=parse_tool_size(total),
                reclaimable_bytes=reclaimable,
                confidence=Confidence.EXACT,
                restoration=RestorationCost.EXTERNAL_STATE,
                selected_by_default=False,
                evidence=evidence,
                safety_root=home,
                reason=spec.note,
                generation=generation,
                warning="Docker reports this figure. Freed space may not appear on the volume until Docker compacts its disk image.",
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
