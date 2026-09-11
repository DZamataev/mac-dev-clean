from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from . import fsevents
from .discovery import MAX_DEPTH, discover_repositories
from .events import EventEmitter
from .index import ScanIndex
from .ndk_usage import analyze_project_ndk_usage
from .policy import recommend
from .projects import analyze_repository
from .recommendation import Recommendation, normalized_path

# Folders macOS TCC (Transparency, Consent, and Control) treats specially:
# even without Full Disk Access, `stat` on these succeeds but *listing*
# their contents raises PermissionError. When that happens during a scan,
# `permission_required` tells the UI which folder to grant access to,
# instead of the folder's repositories silently and unexplainably vanishing
# from the results.
PROTECTED_FOLDER_NAMES = ("Desktop", "Documents", "Downloads")

PROGRESS_EVERY = 25

#: How many unreadable folders to name individually before summarising. Naming
#: every one turns a permissions problem into an unreadable wall of text.
UNREADABLE_REPORT_LIMIT = 5
COMMIT_EVERY = 20


@dataclass
class DeepScanResult:
    recommendations: List[Recommendation] = field(default_factory=list)
    cancelled: bool = False
    #: Whether FSEvents history was complete enough that an incremental walk
    #: WOULD have been safe. Reporting only: the walk is always full today, so
    #: do not read this as "less was scanned".
    incremental: bool = False
    reclaimable_bytes: int = 0


def default_deep_scan_roots(home: Path) -> List[Path]:
    """The home directory is the only default root.

    `~/Library` is excluded by the traversal pruning list rather than by root
    selection, so a user-supplied root inside Library still works if it is ever
    passed explicitly.
    """
    expanded = Path(home).expanduser()
    return [expanded] if expanded.is_dir() else []


def _probe_incremental(
    roots: Sequence[Path], index: ScanIndex, use_fsevents: bool
) -> bool:
    """Decide whether the previous generation can be trusted for unchanged
    branches. Any doubt resolves to a full walk."""
    if not use_fsevents:
        return False
    snapshot = index.snapshot()
    if snapshot is None or snapshot.volume_uuid is None:
        return False

    identity = fsevents.volume_identity(roots[0]) if roots else None
    if identity is None or identity.uuid != snapshot.volume_uuid:
        return False

    probe = fsevents.probe_history(
        device=identity.device,
        since_event_id=snapshot.event_id,
        since_time=snapshot.completed_at,
    )
    if not probe.usable:
        return False
    changed = fsevents.replay_changed_paths(roots, since_event_id=snapshot.event_id)
    return changed is not None


def _protected_folder_targets(root: Path) -> List[Path]:
    """Candidate paths under `root` that macOS TCC may deny even though the
    parent directory itself is readable: the root itself if it happens to be
    one of `PROTECTED_FOLDER_NAMES`, plus any direct child bearing one of
    those names."""
    targets: List[Path] = []
    if root.name in PROTECTED_FOLDER_NAMES:
        targets.append(root)
    for name in PROTECTED_FOLDER_NAMES:
        child = root / name
        if child not in targets:
            targets.append(child)
    return targets


def _check_protected_folder_access(root: Path, emitter: Optional[EventEmitter]) -> None:
    """Proactively probe well-known TCC-protected folders under `root` and
    report which ones are denied.

    `stat`/`is_dir` on a TCC-protected folder succeed even without access;
    only listing its contents (`scandir`) raises `PermissionError`. Without
    this, a folder Full Disk Access has not been granted for simply
    contributes zero repositories with no explanation -- indistinguishable
    from an empty folder. `discover_repositories` already tolerates the same
    `OSError` silently during its own walk (existing, tested behaviour); this
    check runs first only so the UI can name the folder and prompt for
    access, via the `permission_required` event the Swift side already
    consumes.
    """
    if emitter is None:
        return
    for target in _protected_folder_targets(root):
        try:
            is_dir = target.is_dir() and not target.is_symlink()
        except OSError:
            continue
        if not is_dir:
            continue
        try:
            with os.scandir(str(target)):
                pass
        except PermissionError:
            emitter.permission_required(target, target.name)
        except OSError:
            continue


def _deep_scan(
    roots: Sequence[Path],
    index: ScanIndex,
    emitter: Optional[EventEmitter] = None,
    now: Optional[datetime] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    use_fsevents: bool = True,
) -> DeepScanResult:
    """Walk `roots`, analyse every repository found, and persist the results.

    Cancellation is checked between directories and between repositories. A
    cancelled scan abandons its generation, so `latest_complete_generation`
    keeps pointing at the previous good scan and nothing half-analysed ever
    becomes cleanup-eligible.
    """
    moment = now or datetime.now(timezone.utc)
    root_paths = [Path(root).expanduser() for root in roots]
    result = DeepScanResult()
    result.incremental = _probe_incremental(root_paths, index, use_fsevents)

    identity = fsevents.volume_identity(root_paths[0]) if root_paths else None
    if identity is None:
        identity = fsevents.VolumeIdentity(device=0, uuid=None)
    event_id = fsevents.current_event_id()
    generation = index.begin_generation(identity, event_id=event_id)

    if emitter is not None:
        emitter.scan_started(roots=root_paths, incremental=result.incremental)

    scanned = 0
    pending = 0
    cancelled = False
    depth_limited = 0
    unreadable = 0

    def _cancelled() -> bool:
        return should_cancel is not None and should_cancel()

    for root in root_paths:
        if _cancelled():
            cancelled = True
            break
        if emitter is not None:
            emitter.root_started(root)
        repositories = 0

        _check_protected_folder_access(root, emitter)

        def _on_progress(path: str) -> None:
            nonlocal scanned
            scanned += 1
            if emitter is not None and scanned % PROGRESS_EVERY == 0:
                emitter.progress(path=path, scanned=scanned)

        # Both of these fire per directory, and a single tool cache can trip the
        # depth limit thousands of times. The user needs to know a limit was hit
        # once, with an example -- not one line per directory, which floods the
        # UI and buries every real finding.
        def _on_depth_limit(path: Path) -> None:
            nonlocal depth_limited
            if emitter is None:
                return
            depth_limited += 1
            if depth_limited == 1:
                emitter.warning(
                    "Stopped descending at {0} levels; anything deeper was not "
                    "scanned.".format(MAX_DEPTH),
                    path=normalized_path(path),
                )

        def _on_unreadable(path: Path) -> None:
            nonlocal unreadable
            if emitter is None:
                return
            unreadable += 1
            if unreadable <= UNREADABLE_REPORT_LIMIT:
                emitter.warning(
                    "Could not read this folder, so anything inside it was not "
                    "scanned.",
                    path=normalized_path(path),
                )

        for repository in discover_repositories(
            [root],
            on_progress=_on_progress,
            should_cancel=_cancelled,
            on_depth_limit=_on_depth_limit,
            on_unreadable=_on_unreadable,
        ):
            if _cancelled():
                cancelled = True
                break
            repositories += 1
            try:
                ndk_usage = analyze_project_ndk_usage(repository)
            except OSError as exc:
                if emitter is not None:
                    emitter.warning(str(exc), path=normalized_path(repository.path))
            else:
                if ndk_usage is not None:
                    index.record_project_ndk_usage(ndk_usage)
                    pending += 1
                    if pending >= COMMIT_EVERY:
                        index.commit_batch()
                        pending = 0
            try:
                facts = analyze_repository(repository, now=moment)
            except OSError as exc:
                if emitter is not None:
                    emitter.warning(str(exc), path=normalized_path(repository.path))
                continue

            for fact in facts:
                item = recommend(fact, generation=generation, now=moment)
                result.recommendations.append(item)
                result.reclaimable_bytes += item.reclaimable_bytes
                index.record_recommendation(item)
                pending += 1
                if emitter is not None:
                    emitter.candidate_found(item)
                if pending >= COMMIT_EVERY:
                    index.commit_batch()
                    pending = 0

        if emitter is not None:
            emitter.root_finished(root, repositories=repositories)
        if cancelled:
            break

    index.commit_batch()

    # Collapsing the per-directory warnings above would otherwise hide how much
    # was skipped, so report the totals once at the end.
    if emitter is not None:
        if depth_limited > 1:
            emitter.warning(
                "{0} folders were deeper than {1} levels and were not fully "
                "scanned.".format(depth_limited, MAX_DEPTH)
            )
        if unreadable > UNREADABLE_REPORT_LIMIT:
            emitter.warning(
                "{0} folders could not be read and were skipped.".format(unreadable)
            )

    if cancelled:
        index.abandon_generation()
        result.cancelled = True
        if emitter is not None:
            emitter.scan_cancelled(
                reclaimable_bytes=result.reclaimable_bytes,
                count=len(result.recommendations),
            )
        return result

    index.complete_generation()
    if emitter is not None:
        emitter.scan_completed(
            reclaimable_bytes=result.reclaimable_bytes, count=len(result.recommendations)
        )
    return result


def deep_scan(
    roots: Sequence[Path],
    index: ScanIndex,
    emitter: Optional[EventEmitter] = None,
    now: Optional[datetime] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    use_fsevents: bool = True,
) -> DeepScanResult:
    """Run a Deep Scan and abandon its generation if the scan raises."""
    try:
        return _deep_scan(
            roots,
            index,
            emitter=emitter,
            now=now,
            should_cancel=should_cancel,
            use_fsevents=use_fsevents,
        )
    except Exception:
        index.abandon_generation()
        raise
