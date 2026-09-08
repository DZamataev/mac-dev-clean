from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from ..recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    ToolAction,
    normalized_path,
)
from ..sim_prune import SimctlError, is_safe_simctl_udid, load_inventory
from .runner import MAX_STDERR_CHARS, ToolUnavailable


XCRUN = "/usr/bin/xcrun"
SIMCTL_DEVICES_PREVIEW_ARGV = (XCRUN, "simctl", "list", "--json", "devices")
SIMCTL_RUNTIMES_PREVIEW_ARGV = (XCRUN, "simctl", "runtime", "list", "-j")
_TRUNCATION_MARKER = "...[truncated]"
_CLASS_ORDER = {
    "simulator-unavailable-device": 0,
    "simulator-runtime": 1,
}


def _bounded_reason(reason: str) -> str:
    if len(reason) <= MAX_STDERR_CHARS:
        return reason
    retained = MAX_STDERR_CHARS - len(_TRUNCATION_MARKER)
    return reason[:retained] + _TRUNCATION_MARKER


def analyze_simulator(
    runner, generation: int = 0, home: Optional[Path] = None
) -> Tuple[List[Recommendation], Optional[str]]:
    root = Path(home).expanduser() if home is not None else Path.home()
    device_root = root / "Library/Developer/CoreSimulator/Devices"
    try:
        inventory = load_inventory(runner=runner)
    except ToolUnavailable as exc:
        return [], _bounded_reason(exc.reason)
    except SimctlError as exc:
        return [], _bounded_reason(str(exc))
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as exc:
        return [], _bounded_reason(str(exc) or "malformed simulator inventory")
    items = []  # type: List[Recommendation]

    device_identity_counts = {}  # type: dict[str, int]
    for device in inventory.devices:
        identity = device.udid.upper()
        device_identity_counts[identity] = device_identity_counts.get(identity, 0) + 1

    for device in inventory.devices:
        if device.is_available or device.state.lower() != "shutdown":
            continue
        if not is_safe_simctl_udid(device.udid) or device.total_size_bytes <= 0:
            continue
        if device_identity_counts[device.udid.upper()] != 1:
            continue
        items.append(
            Recommendation(
                detector_id="simulator-unavailable-device",
                category="tool-managed",
                label="Simulator device {}".format(device.name),
                path=device_root / device.udid,
                action=ActionKind.INVOKE_TOOL,
                allocated_bytes=device.total_size_bytes,
                reclaimable_bytes=device.total_size_bytes,
                confidence=Confidence.HEURISTIC,
                restoration=RestorationCost.EXTERNAL_STATE,
                selected_by_default=False,
                evidence=(
                    Evidence("tool-report", "simctl reports this device unavailable"),
                    Evidence("tool-report", "state is {}".format(device.state)),
                    Evidence(
                        "tool-size",
                        "simctl reports {} bytes".format(device.total_size_bytes),
                    ),
                    Evidence(
                        "preview-command", " ".join(SIMCTL_DEVICES_PREVIEW_ARGV)
                    ),
                ),
                safety_root=device_root,
                reason="simctl reports this device as unavailable and shut down.",
                generation=generation,
                warning=(
                    "Apps and data installed on the device are deleted with it. "
                    "APFS clones mean reclaimed space may be less than the reported size."
                ),
                tool_action=ToolAction(
                    tool="simctl",
                    resource=device.udid,
                    argv=(XCRUN, "simctl", "delete", device.udid),
                    preview_argv=SIMCTL_DEVICES_PREVIEW_ARGV,
                    reported=device.state,
                ),
            )
        )

    runtime_identity_counts = {}  # type: dict[str, int]
    for runtime in inventory.runtimes:
        runtime_identity_counts[runtime.identifier] = (
            runtime_identity_counts.get(runtime.identifier, 0) + 1
        )

    for runtime in inventory.runtimes:
        if runtime.deletable is not True or runtime.size_bytes <= 0:
            continue
        if (
            not runtime.identifier
            or "\x00" in runtime.identifier
            or runtime.identifier.startswith("-")
        ):
            continue
        if runtime_identity_counts[runtime.identifier] != 1:
            continue
        if not runtime.path or "\x00" in runtime.path:
            continue
        runtime_path = Path(runtime.path)
        if not runtime_path.is_absolute():
            continue
        evidence = [
            Evidence(
                "tool-report",
                "simctl reports {} bytes for this runtime".format(runtime.size_bytes),
            ),
            Evidence("preview-command", " ".join(SIMCTL_RUNTIMES_PREVIEW_ARGV)),
        ]
        if runtime.last_used_at is not None:
            evidence.append(
                Evidence(
                    "last-used",
                    "simctl reports last use at {}".format(
                        runtime.last_used_at.isoformat()
                    ),
                )
            )
        items.append(
            Recommendation(
                detector_id="simulator-runtime",
                category="tool-managed",
                label="Simulator runtime {}".format(runtime.name),
                path=runtime_path,
                action=ActionKind.INVOKE_TOOL,
                allocated_bytes=runtime.size_bytes,
                reclaimable_bytes=runtime.size_bytes,
                confidence=Confidence.STRONG,
                restoration=RestorationCost.REDOWNLOAD,
                selected_by_default=False,
                evidence=tuple(evidence),
                safety_root=runtime_path.parent,
                reason="Simulator runtime that simctl marks as deletable.",
                generation=generation,
                last_activity_at=runtime.last_used_at,
                warning=(
                    "Projects targeting this runtime require downloading it again. "
                    "APFS clones may make reclaimed space lower than reported."
                ),
                tool_action=ToolAction(
                    tool="simctl",
                    resource=runtime.identifier,
                    argv=(
                        XCRUN,
                        "simctl",
                        "runtime",
                        "delete",
                        runtime.identifier,
                    ),
                    preview_argv=SIMCTL_RUNTIMES_PREVIEW_ARGV,
                    reported="{} bytes".format(runtime.size_bytes),
                ),
            )
        )

    path_counts = {}  # type: dict[Tuple[str, str], int]
    for item in items:
        key = (item.detector_id, normalized_path(item.path))
        path_counts[key] = path_counts.get(key, 0) + 1
    unambiguous = [
        item
        for item in items
        if path_counts[(item.detector_id, normalized_path(item.path))] == 1
    ]
    unambiguous.sort(
        key=lambda item: (
            _CLASS_ORDER[item.detector_id],
            item.tool_action.resource if item.tool_action is not None else "",
        )
    )
    return unambiguous, None
