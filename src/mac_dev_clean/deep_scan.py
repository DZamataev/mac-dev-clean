from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from . import fsevents
from .discovery import discover_repositories
from .events import EventEmitter
from .index import ScanIndex
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
COMMIT_EVERY = 20


@dataclass
class DeepScanResult:
    recommendations: List[Recommendation] = field(default_factory=list)
    cancelled: bool = False
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


def deep_scan(
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

        for repository in discover_repositories(
            [root], on_progress=_on_progress, should_cancel=_cancelled
        ):
            if _cancelled():
                cancelled = True
                break
            repositories += 1
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
