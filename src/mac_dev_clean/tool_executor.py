from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .executor import ApplyOutcome, ApplyResult
from .index import ScanIndex
from .journal import ActionJournal, JournalRecord, append_with_warning
from .recommendation import ActionKind, Recommendation, ToolAction, normalized_path
from .scanner import path_size
from .sim_prune import (
    is_safe_simctl_udid,
    parse_devices_json,
    parse_runtime_images_json,
)
from .tools.android import (
    AVD_LIST_ARGV,
    SDK_LIST_ARGV,
    cmdline_tool_dirs,
    find_sdk_root,
    parse_avds,
    parse_sdk_packages,
)
from .tools.docker import DOCKER_PREVIEW_ARGV
from .tools.homebrew import BREW_CLEANUP_ARGV, BREW_PREVIEW_ARGV, parse_brew_preview
from .tools.registry import binary_runner
from .tools.runner import (
    MAX_STDERR_CHARS,
    ToolResult,
    ToolRunner,
    ToolUnavailable,
    inventory_argv_matches,
)
from .tools.simulator import (
    SIMCTL_DEVICES_PREVIEW_ARGV,
    SIMCTL_RUNTIMES_PREVIEW_ARGV,
    XCRUN,
)
from .tools.sizes import parse_tool_size

_TRUNCATION_MARKER = "...[truncated]"

#: The generation of the contract set below. Bump it when a vector changes, so
#: a review can tell an intentional widening from an accident.
CONTRACT_VERSION = 1

#: The action vector this program will run for each Docker resource class.
#:
#: `docker image prune -a` and `docker system prune` are absent by
#: construction: their effect is broader than the `Reclaimable` figure the
#: preview reports, so no preview could authorise them.
_DOCKER_ACTION_ARGV = {
    "build-cache": ("docker", "builder", "prune", "-f"),
    "images": ("docker", "image", "prune", "-f"),
    "containers": ("docker", "container", "prune", "-f"),
    "volumes": ("docker", "volume", "prune", "-f"),
}


def _is_usable_identity(resource: str) -> bool:
    """Whether a resource can be an argument at all.

    A contract builds its vector *around* the resource, so identity checking
    and vector checking are not the same check: a vector can match its own
    template perfectly while the identity inside it is `--force`. A leading
    dash would reach the tool as an option rather than as a name; control
    characters and padding could not have come from any parser here, which
    means they came from somewhere that should not be deciding this.
    """
    if not isinstance(resource, str) or not resource:
        return False
    if resource.startswith("-"):
        return False
    if resource != resource.strip():
        return False
    return not any(character < " " or character == "\x7f" for character in resource)


def _contract_for(tool, resource):
    # type: (str, str) -> Optional[Tuple[Tuple[str, ...], Tuple[str, ...]]]
    """Regenerate the one (action, preview) pair allowed for this identity.

    Nothing stored alongside the recommendation is consulted. The pair is
    rebuilt here from the tool and the resource alone, so a stored vector can
    only ever be compared against it -- never be the source of it.
    """
    if tool == "docker":
        argv = _DOCKER_ACTION_ARGV.get(resource)
        if argv is None:
            return None
        return argv, DOCKER_PREVIEW_ARGV
    if tool == "brew":
        if resource != "cleanup":
            return None
        return BREW_CLEANUP_ARGV, BREW_PREVIEW_ARGV
    if tool == "sdkmanager":
        return ("sdkmanager", "--uninstall", resource), SDK_LIST_ARGV
    if tool == "avdmanager":
        return ("avdmanager", "delete", "avd", "-n", resource), AVD_LIST_ARGV
    if tool == "simctl":
        # One tool name, two contracts. They are kept disjoint on the resource
        # so a device deletion can never be paired with the runtime listing:
        # a UDID selects the device contract and nothing else can.
        if is_safe_simctl_udid(resource):
            return (
                (XCRUN, "simctl", "delete", resource),
                SIMCTL_DEVICES_PREVIEW_ARGV,
            )
        return (
            (XCRUN, "simctl", "runtime", "delete", resource),
            SIMCTL_RUNTIMES_PREVIEW_ARGV,
        )
    return None


def contract_refusal(action: ToolAction) -> Optional[str]:
    """Return why this action is outside the contracts, or None if it is one.

    A recommendation arrives from an index file, which is only as trustworthy
    as the disk it sits on. Treating its stored vectors as commands would make
    that file the authority over what runs; comparing them against a freshly
    regenerated contract keeps the authority here.
    """
    origin = "contract set v{}".format(CONTRACT_VERSION)
    if not _is_usable_identity(action.resource):
        return _bounded_reason(
            "{}: the recorded resource is not a usable identity for {}".format(
                origin, action.tool
            )
        )
    contract = _contract_for(action.tool, action.resource)
    if contract is None:
        return _bounded_reason(
            "{}: no contract covers {} resource {!r}".format(
                origin, action.tool, action.resource
            )
        )
    argv, preview_argv = contract
    if tuple(action.argv) != argv:
        return _bounded_reason(
            "{}: the recorded command is not the contracted command for {} {!r}".format(
                origin, action.tool, action.resource
            )
        )
    if tuple(action.preview_argv) != preview_argv:
        return _bounded_reason(
            "{}: the recorded preview is not the contracted preview for {} {!r}".format(
                origin, action.tool, action.resource
            )
        )
    return None


class _ObservedJournal:
    """Collect one safe persistence warning for each attempted audit row."""

    def __init__(self, journal: ActionJournal) -> None:
        self._journal = journal
        self.warnings = []  # type: List[str]

    def append(self, record: JournalRecord) -> bool:
        warning = append_with_warning(self._journal, record)
        self.warnings.append(warning)
        return not warning


class _PreviewRefused(Exception):
    """The preview ran but cannot be allowed to authorise anything."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _default_runner(item: Recommendation) -> ToolRunner:
    """Resolve the executable that owns this reviewed recommendation.

    Bound to argv[0] rather than to the tool name: the simulator's vectors run
    through xcrun, so the two diverge exactly where the declared vector is the
    thing that matters. sdkmanager is additionally bound to the stored SDK root;
    resolving it from today's environment could uninstall the same package from
    a different SDK than the one whose path and size the user reviewed.
    """
    action = item.tool_action
    if action is None:
        raise ValueError("tool recommendation has no action")
    executable = action.argv[0]
    if os.path.basename(executable) == "sdkmanager":
        return binary_runner(
            executable,
            extra_dirs=cmdline_tool_dirs(item.safety_root),
            allow_fallback=False,
        )
    if os.path.basename(executable) == "avdmanager":
        sdk_root = find_sdk_root(dict(os.environ), Path.home())
        extra_dirs = cmdline_tool_dirs(sdk_root) if sdk_root is not None else ()
        return binary_runner(executable, extra_dirs=extra_dirs)
    return binary_runner(executable)


def _bounded_reason(reason: str) -> str:
    if len(reason) <= MAX_STDERR_CHARS:
        return reason
    retained = MAX_STDERR_CHARS - len(_TRUNCATION_MARKER)
    return reason[:retained] + _TRUNCATION_MARKER


def _bounded_output(output: str) -> str:
    """Keep both command context and the owner's final summary line."""
    if len(output) <= MAX_STDERR_CHARS:
        return output
    separator = "\n{}\n".format(_TRUNCATION_MARKER)
    retained = MAX_STDERR_CHARS - len(separator)
    head = retained // 2
    tail = retained - head
    return output[:head] + separator + output[-tail:]


def _journal_target(item: Optional[Recommendation]) -> str:
    """Name what was acted on the way a reader of the journal needs it.

    A tool action has no filesystem target to record, so it is identified by
    the resource its own tool names; a path action keeps its normalized path.
    """
    if item is None:
        return ""
    if item.tool_action is not None:
        return "{}:{}".format(item.tool_action.tool, item.tool_action.resource)
    return normalized_path(item.path)


def _journal(
    journal: _ObservedJournal,
    item: Optional[Recommendation],
    recommendation_id: str,
    outcome: ApplyOutcome,
    dry_run: bool,
    detail: str,
    reclaimable_bytes: int,
    argv: Sequence[str] = (),
    now: Optional[datetime] = None,
) -> None:
    record = JournalRecord(
        recommendation_id=recommendation_id,
        detector_id=item.detector_id if item is not None else "",
        category=item.category if item is not None else "",
        target=_journal_target(item),
        # An unknown ID names no action at all. Recording one here would put a
        # command in the evidence trail that was never attempted.
        action=item.action.value if item is not None else "",
        outcome=outcome.value,
        reclaimable_bytes=reclaimable_bytes,
        dry_run=dry_run,
        argv=tuple(argv),
        detail=_bounded_reason(detail),
        recorded_at=now,
    )
    try:
        journal.append(record)
    except Exception:
        # Audit persistence is best effort. The concrete ActionJournal already
        # contains ordinary I/O failures; another implementation must not be
        # able to change whether an action is attempted or how it is reported.
        pass


#: Docker's own class names, mapped to the resources this program contracts
#: for. Exact equality only: "Build Cache Extra" shares every character of
#: "Build Cache" and names a different thing, so any containment or prefix
#: test here would prune one class on the evidence of another.
_DOCKER_TYPES = {
    "Build Cache": "build-cache",
    "Images": "images",
    "Containers": "containers",
    "Local Volumes": "volumes",
}

#: A tool-reported figure, for telling "zero" apart from "unreadable".
#: `parse_tool_size` answers 0 for both, which is right for an estimate and
#: wrong for an authorisation: one means there is nothing to do, the other
#: means the tool said something this program cannot understand.
_FIGURE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)?\s*$", re.IGNORECASE
)


@dataclass(frozen=True)
class CurrentEffect:
    """What the owning tool says about this one target, right now.

    `reclaimable_bytes` is the figure measured or reported during this apply,
    never the one the scan stored; `reported` keeps the tool's own words for
    it so a later report can quote the owner rather than paraphrase it.
    """

    tool: str
    resource: str
    reclaimable_bytes: int
    reported: str
    detail: str


def _nothing(action: ToolAction, detail: str) -> CurrentEffect:
    return CurrentEffect(action.tool, action.resource, 0, "", detail)


def _refuse(action: ToolAction, detail: str) -> "_PreviewRefused":
    return _PreviewRefused(
        _bounded_reason(
            "{} {}: {}".format(action.tool, action.resource, detail)
        )
    )


def _figure_bytes(text: object) -> Optional[int]:
    """Return a tool figure in bytes, or None when it cannot be read as one."""
    if not isinstance(text, str):
        return None
    match = _FIGURE.match(text)
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if value == 0:
        return 0
    parsed = parse_tool_size(text)
    return parsed if parsed > 0 else None


def _same_path(observed: Path, stored: Path) -> bool:
    """Whether the tool's location for this identity is the stored one.

    Compared through `normalized_path`, the same function that wrote the
    stored value, so the two sides cannot disagree over a trailing separator
    or a `.` segment and call it a drift.
    """
    return normalized_path(observed) == normalized_path(stored)


def _docker_effect(action: ToolAction, result: ToolResult) -> CurrentEffect:
    """Read the one `system df` line that describes this resource class."""
    matches = []  # type: List[Dict[str, object]]
    for line in result.lines():
        try:
            payload = json.loads(line)
        except ValueError:
            # One unreadable line must not cost us a readable one, exactly as
            # the analyzer treats it.
            continue
        if not isinstance(payload, dict):
            continue
        docker_type = payload.get("Type")
        if not isinstance(docker_type, str):
            continue
        if _DOCKER_TYPES.get(docker_type) == action.resource:
            matches.append(payload)

    if len(matches) > 1:
        # Two figures for one class: the prune would be authorised by whichever
        # we happened to read first. Duplicates are suppressed per class, so an
        # ambiguity here says nothing about the other classes.
        raise _refuse(action, "docker reported this class more than once")
    if not matches:
        return _nothing(action, "docker no longer reports this resource class")

    payload = matches[0]
    reported = payload.get("Reclaimable")
    reclaimable = _figure_bytes(reported)
    allocated = _figure_bytes(payload.get("Size"))
    if reclaimable is None or allocated is None:
        raise _refuse(action, "docker reported a figure that could not be read")
    if reclaimable == 0:
        return _nothing(action, "docker reports nothing reclaimable in this class")
    if reclaimable > allocated:
        # A class cannot give back more than it holds. The pair is incoherent,
        # so neither number is evidence of anything.
        raise _refuse(
            action, "docker reports more reclaimable than the class holds"
        )
    return CurrentEffect(
        action.tool,
        action.resource,
        reclaimable,
        str(reported),
        "docker reports {} reclaimable for {}".format(reported, action.resource),
    )


def _brew_effect(action: ToolAction, result: ToolResult) -> CurrentEffect:
    """Require brew's own unique, well-formed, positive cleanup total."""
    total, count = parse_brew_preview(result.stdout)
    if total <= 0 and count == 0:
        return _nothing(action, "brew now reports nothing to remove")
    if total <= 0:
        # `parse_brew_preview` collapses "no summary", "two summaries", and "a
        # summary we cannot read" into a zero total. With removals listed, none
        # of those is a statement that there is nothing to do.
        plural = "" if count == 1 else "s"
        raise _refuse(
            action,
            "brew reported {} removal{} but no unique valid positive cleanup "
            "total".format(count, plural),
        )
    return CurrentEffect(
        action.tool,
        action.resource,
        total,
        "{} items".format(count),
        "brew would free {} bytes across {} items".format(total, count),
    )


def _sdkmanager_effect(
    item: Recommendation, action: ToolAction, result: ToolResult
) -> CurrentEffect:
    """Require the exact package identity, still installed under this SDK root."""
    packages = [
        package
        for package in parse_sdk_packages(result.stdout)
        if package.path == action.resource
    ]
    if len(packages) > 1:
        raise _refuse(action, "sdkmanager listed this package more than once")
    if not packages:
        return _nothing(action, "sdkmanager no longer lists this package")

    package = packages[0]
    if not package.location or "\x00" in package.location:
        raise _refuse(action, "sdkmanager reported no usable location")
    relative = Path(package.location)
    if relative.is_absolute():
        # The analyzer only ever accepts a location relative to the SDK root;
        # an absolute one could name anything on the volume.
        raise _refuse(action, "sdkmanager reported a location outside the SDK root")
    try:
        root = Path(item.safety_root).resolve()
        location = (root / relative).resolve()
        location.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise _refuse(action, "sdkmanager reported a location outside the SDK root")
    if not _same_path(location, item.path):
        # The identity is right but the thing it names has moved. The size the
        # user reviewed belongs to the old location, so the recommendation no
        # longer describes what would happen.
        raise _refuse(action, "sdkmanager reports this package at another location")
    if not location.is_dir():
        return _nothing(action, "the package location is no longer present")

    size = path_size(location)
    if size <= 0:
        return _nothing(action, "the package location measures nothing")
    return CurrentEffect(
        action.tool,
        action.resource,
        size,
        package.version,
        "sdkmanager lists {} ({})".format(action.resource, package.description),
    )


def _avdmanager_effect(
    item: Recommendation, action: ToolAction, result: ToolResult
) -> CurrentEffect:
    """Require the exact AVD name, still listed at the path that was measured."""
    loadable, unloadable = parse_avds(result.stdout)
    # Both sections, because a device that stopped loading since the scan is
    # the same device and the same directory on disk.
    matches = [
        (avd, broken)
        for avd, broken in (
            [(entry, False) for entry in loadable]
            + [(entry, True) for entry in unloadable]
        )
        if avd.name == action.resource
    ]
    if len(matches) > 1:
        raise _refuse(action, "avdmanager listed this name more than once")
    if not matches:
        return _nothing(action, "avdmanager no longer lists this device")

    avd, broken = matches[0]
    if not avd.path or "\x00" in avd.path:
        raise _refuse(action, "avdmanager reported no usable path")
    location = Path(avd.path)
    if not location.is_absolute():
        raise _refuse(action, "avdmanager reported a path that is not absolute")
    if not _same_path(location, item.path):
        raise _refuse(action, "avdmanager reports this device at another path")

    size = path_size(location)
    if size <= 0:
        return _nothing(action, "the device directory is no longer present")
    reported = (avd.error if broken else avd.target) or "no details reported"
    return CurrentEffect(
        action.tool,
        action.resource,
        size,
        reported,
        "avdmanager lists {} at {}".format(action.resource, normalized_path(location)),
    )


def _simctl_device_effect(
    item: Recommendation, action: ToolAction, result: ToolResult
) -> CurrentEffect:
    """Require this exact UDID, still unavailable, shut down, and measured."""
    try:
        devices = parse_devices_json(result.stdout)
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
        raise _refuse(action, "simctl returned a device inventory that cannot be read")

    identity = action.resource.upper()
    same_identity = [
        device for device in devices if device.udid.upper() == identity
    ]
    if len(same_identity) > 1:
        raise _refuse(action, "simctl listed this device more than once")
    if not same_identity:
        return _nothing(action, "simctl no longer lists this device")
    exact = [device for device in same_identity if device.udid == action.resource]
    if not exact:
        # The contract's argument is the stored string. Substituting another
        # spelling of it would delete something the review never named.
        raise _refuse(action, "simctl reports this UDID in another form")

    device = exact[0]
    if not is_safe_simctl_udid(device.udid):
        raise _refuse(action, "simctl reported an unusable UDID")
    if not _same_path(Path(item.safety_root) / device.udid, item.path):
        raise _refuse(action, "the stored path is not this device's own")
    if device.is_available or device.state.lower() != "shutdown":
        # The condition that justified offering the deletion is gone: the
        # device is in use again, or simctl can use it again.
        return _nothing(
            action, "simctl no longer reports this device as unavailable and shut down"
        )
    if device.total_size_bytes <= 0:
        return _nothing(action, "simctl reports no storage for this device")
    return CurrentEffect(
        action.tool,
        action.resource,
        device.total_size_bytes,
        device.state,
        "simctl reports {} bytes for device {}".format(
            device.total_size_bytes, device.name
        ),
    )


def _simctl_runtime_effect(
    item: Recommendation, action: ToolAction, result: ToolResult
) -> CurrentEffect:
    """Require this exact runtime identifier, still marked deletable."""
    try:
        runtimes = parse_runtime_images_json(result.stdout)
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
        raise _refuse(action, "simctl returned a runtime inventory that cannot be read")

    matches = [
        runtime for runtime in runtimes if runtime.identifier == action.resource
    ]
    if len(matches) > 1:
        raise _refuse(action, "simctl listed this runtime more than once")
    if not matches:
        return _nothing(action, "simctl no longer lists this runtime")

    runtime = matches[0]
    if not runtime.path or "\x00" in runtime.path:
        raise _refuse(action, "simctl reported no usable path for this runtime")
    location = Path(runtime.path)
    if not location.is_absolute():
        raise _refuse(action, "simctl reported a runtime path that is not absolute")
    if not _same_path(location, item.path):
        raise _refuse(action, "simctl reports this runtime at another path")
    if runtime.deletable is not True:
        return _nothing(action, "simctl no longer marks this runtime deletable")
    if runtime.size_bytes <= 0:
        return _nothing(action, "simctl reports no storage for this runtime")
    return CurrentEffect(
        action.tool,
        action.resource,
        runtime.size_bytes,
        "{} bytes".format(runtime.size_bytes),
        "simctl reports {} bytes for runtime {}".format(
            runtime.size_bytes, runtime.name
        ),
    )


def current_tool_effect(item: Recommendation, runner: ToolRunner) -> CurrentEffect:
    """Re-derive this recommendation's effect from its own tool, right now.

    The preview runs exactly once and every answer is read through the same
    parser the analyzer used, then checked against the identity and the path
    the recommendation carries. Nothing is matched by substring and no figure
    is read from a line that does not name this target, so a tool that has
    changed its mind can only ever produce a skip or a refusal -- never an
    action against something the scan never described.

    Raises `_PreviewRefused` when the evidence is missing its provenance,
    ambiguous, or malformed: a figure of unknown origin is not a smaller
    authority than a correct one, it is no authority at all.
    """
    action = item.tool_action
    if action is None:
        raise _PreviewRefused("the recommendation carries no tool action")

    result = runner(action.preview_argv)
    if not isinstance(result, ToolResult):
        # A runner is an injection point. Reading fields off whatever it
        # returned would let any object that merely has the right attribute
        # names decide that a deletion is warranted.
        raise _PreviewRefused("the preview did not return a tool result")
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip()
        raise _PreviewRefused(
            _bounded_reason(
                detail or "the preview exited with {}".format(result.exit_code)
            )
        )
    if not inventory_argv_matches(result.argv, action.preview_argv):
        raise _PreviewRefused(
            "the preview's provenance did not match the command that was asked for"
        )

    if action.tool == "docker":
        return _docker_effect(action, result)
    if action.tool == "brew":
        return _brew_effect(action, result)
    if action.tool == "sdkmanager":
        return _sdkmanager_effect(item, action, result)
    if action.tool == "avdmanager":
        return _avdmanager_effect(item, action, result)
    if action.tool == "simctl":
        # The same split the contract makes, so the preview that ran and the
        # parser that reads it can never belong to different halves of simctl.
        if is_safe_simctl_udid(action.resource):
            return _simctl_device_effect(item, action, result)
        return _simctl_runtime_effect(item, action, result)
    # Unreachable while every contracted tool is handled above; a new contract
    # without a revalidation must refuse rather than silently authorise.
    raise _refuse(action, "no current-effect check covers this tool")


def invoke_tool_recommendations(
    recommendation_ids: Sequence[str],
    index: ScanIndex,
    journal: ActionJournal,
    runners: Optional[Dict[str, ToolRunner]] = None,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> List[ApplyResult]:
    """Execute tool-managed recommendations by ID.

    Authority comes from the index plus a fresh preview, never from a caller
    supplied path or command. Every branch journals before returning, so a
    crash between the action and the report cannot hide what happened.
    """
    supplied = runners or {}
    observed_journal = _ObservedJournal(journal)
    moment = now or datetime.now(timezone.utc)
    results = []  # type: List[ApplyResult]

    for recommendation_id in recommendation_ids:
        # Checked before the work rather than after it: cancellation must not
        # be able to discard an action that has already run.
        if should_cancel is not None and should_cancel():
            break

        item = index.load_recommendation(recommendation_id)
        if item is None:
            _journal(
                observed_journal,
                None,
                recommendation_id,
                ApplyOutcome.FAILED,
                dry_run,
                "recommendation not found in the current scan",
                0,
                now=moment,
            )
            results.append(
                ApplyResult(
                    recommendation_id=recommendation_id,
                    label="",
                    path="",
                    reclaimable_bytes=0,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=dry_run,
                    error="recommendation not found in the current scan",
                )
            )
            continue

        action = item.tool_action
        if item.action is not ActionKind.INVOKE_TOOL or action is None:
            _journal(
                observed_journal,
                item,
                item.id,
                ApplyOutcome.FAILED,
                dry_run,
                "not a tool-managed recommendation",
                item.reclaimable_bytes,
                now=moment,
            )
            results.append(
                ApplyResult(
                    recommendation_id=item.id,
                    label=item.label,
                    path=normalized_path(item.path),
                    reclaimable_bytes=item.reclaimable_bytes,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=dry_run,
                    error="not a tool-managed recommendation",
                )
            )
            continue

        # Before a runner is even chosen: the executable a runner would be
        # bound to comes out of `action.argv[0]`, so an unchecked action would
        # already have decided which binary the machine goes looking for.
        refusal = contract_refusal(action)
        if refusal is not None:
            _journal(
                observed_journal,
                item,
                item.id,
                ApplyOutcome.FAILED,
                dry_run,
                refusal,
                item.reclaimable_bytes,
                argv=action.preview_argv,
                now=moment,
            )
            results.append(
                ApplyResult(
                    recommendation_id=item.id,
                    label=item.label,
                    path=_journal_target(item),
                    reclaimable_bytes=item.reclaimable_bytes,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=dry_run,
                    error=refusal,
                )
            )
            continue

        runner = supplied.get(action.tool)
        if runner is None:
            runner = _default_runner(item)

        try:
            effect = current_tool_effect(item, runner)
        except ToolUnavailable as exc:
            _journal(
                observed_journal,
                item,
                item.id,
                ApplyOutcome.FAILED,
                dry_run,
                exc.reason,
                item.reclaimable_bytes,
                argv=action.preview_argv,
                now=moment,
            )
            results.append(
                ApplyResult(
                    recommendation_id=item.id,
                    label=item.label,
                    path=_journal_target(item),
                    reclaimable_bytes=item.reclaimable_bytes,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=dry_run,
                    error=exc.reason,
                )
            )
            continue
        except _PreviewRefused as exc:
            _journal(
                observed_journal,
                item,
                item.id,
                ApplyOutcome.FAILED,
                dry_run,
                exc.reason,
                item.reclaimable_bytes,
                argv=action.preview_argv,
                now=moment,
            )
            results.append(
                ApplyResult(
                    recommendation_id=item.id,
                    label=item.label,
                    path=_journal_target(item),
                    reclaimable_bytes=item.reclaimable_bytes,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=dry_run,
                    error=exc.reason,
                )
            )
            continue

        current = effect.reclaimable_bytes
        if current <= 0:
            # The tool's own account of why, rather than a single sentence for
            # every cause: "no longer listed", "no longer deletable", and "now
            # measures nothing" are different facts about the machine.
            detail = effect.detail or "the tool now reports nothing to reclaim"
            _journal(
                observed_journal,
                item,
                item.id,
                ApplyOutcome.SKIPPED,
                dry_run,
                detail,
                0,
                argv=action.preview_argv,
                now=moment,
            )
            results.append(
                ApplyResult(
                    recommendation_id=item.id,
                    label=item.label,
                    path=_journal_target(item),
                    reclaimable_bytes=0,
                    outcome=ApplyOutcome.SKIPPED,
                    dry_run=dry_run,
                    error=detail,
                )
            )
            continue

        if should_cancel is not None and should_cancel():
            detail = "cancelled after preview: the action was not attempted"
            _journal(
                observed_journal,
                item,
                item.id,
                ApplyOutcome.SKIPPED,
                dry_run,
                detail,
                current,
                argv=action.preview_argv,
                now=moment,
            )
            results.append(
                ApplyResult(
                    recommendation_id=item.id,
                    label=item.label,
                    path=_journal_target(item),
                    reclaimable_bytes=current,
                    outcome=ApplyOutcome.SKIPPED,
                    dry_run=dry_run,
                    error=detail,
                    reported=_bounded_reason(effect.reported),
                )
            )
            continue

        if dry_run:
            detail = "dry run: {} would run ({})".format(
                " ".join(action.argv), effect.detail
            )
            _journal(
                observed_journal,
                item,
                item.id,
                ApplyOutcome.SKIPPED,
                True,
                detail,
                current,
                argv=action.preview_argv,
                now=moment,
            )
            results.append(
                ApplyResult(
                    recommendation_id=item.id,
                    label=item.label,
                    path=_journal_target(item),
                    reclaimable_bytes=current,
                    outcome=ApplyOutcome.SKIPPED,
                    dry_run=True,
                    error=detail,
                    reported=_bounded_reason(effect.reported),
                )
            )
            continue

        try:
            executed = runner(action.argv)
        except ToolUnavailable as exc:
            # The vector is journalled even though it never ran: the evidence
            # trail has to say which command was attempted, not merely that
            # something failed.
            _journal(
                observed_journal,
                item,
                item.id,
                ApplyOutcome.FAILED,
                False,
                exc.reason,
                current,
                argv=action.argv,
                now=moment,
            )
            results.append(
                ApplyResult(
                    recommendation_id=item.id,
                    label=item.label,
                    path=_journal_target(item),
                    reclaimable_bytes=current,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=False,
                    error=exc.reason,
                )
            )
            continue

        if not isinstance(executed, ToolResult):
            detail = "the action did not return a tool result"
        elif not inventory_argv_matches(executed.argv, action.argv):
            detail = "the action's provenance did not match the command attempted"
        elif not executed.ok:
            detail = _bounded_reason(
                executed.stderr.strip()
                or executed.stdout.strip()
                or "exited with {}".format(executed.exit_code)
            )
        else:
            detail = ""

        if detail:
            _journal(
                observed_journal,
                item,
                item.id,
                ApplyOutcome.FAILED,
                False,
                detail,
                current,
                argv=action.argv,
                now=moment,
            )
            results.append(
                ApplyResult(
                    recommendation_id=item.id,
                    label=item.label,
                    path=_journal_target(item),
                    reclaimable_bytes=current,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=False,
                    error=detail,
                )
            )
            continue

        # Tools report what they actually freed on the last line of their own
        # output. Keeping it is the only way the journal records the tool's
        # figure rather than the estimate that led us to run the tool.
        # Falling back to the preview's own account keeps the journal saying
        # what was acted on even for a tool that prints nothing on success.
        reported = _bounded_output(executed.stdout.strip())
        _journal(
            observed_journal,
            item,
            item.id,
            ApplyOutcome.INVOKED,
            False,
            reported.splitlines()[-1] if reported else effect.detail,
            current,
            argv=action.argv,
            now=moment,
        )
        results.append(
            ApplyResult(
                recommendation_id=item.id,
                label=item.label,
                path=_journal_target(item),
                reclaimable_bytes=current,
                outcome=ApplyOutcome.INVOKED,
                dry_run=False,
                reported=reported or _bounded_reason(effect.reported),
            )
        )

    return [
        replace(result, journal_warning=observed_journal.warnings[index])
        if index < len(observed_journal.warnings) and observed_journal.warnings[index]
        else result
        for index, result in enumerate(results)
    ]
