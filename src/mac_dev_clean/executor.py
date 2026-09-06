from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence

from .cleaner import _remove_path
from .index import ScanIndex
from .model import human_bytes
from .projects import (
    INACTIVITY_DAYS,
    RECIPES,
    artifact_recent_activity,
    last_meaningful_activity,
)
from .recommendation import ActionKind, Recommendation, normalized_path

RECIPES_BY_ID = {recipe.detector_id: recipe for recipe in RECIPES}


class ApplyOutcome(Enum):
    REMOVED = "removed"
    SKIPPED = "skipped"
    CHANGED_SINCE_SCAN = "changed_since_scan"
    FAILED = "failed"


@dataclass(frozen=True)
class ApplyResult:
    recommendation_id: str
    label: str
    path: str
    reclaimable_bytes: int
    outcome: ApplyOutcome
    dry_run: bool
    error: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.recommendation_id,
            "label": self.label,
            "path": self.path,
            "reclaimable_bytes": self.reclaimable_bytes,
            "size": human_bytes(self.reclaimable_bytes),
            "outcome": self.outcome.value,
            "dry_run": self.dry_run,
            "error": self.error,
        }


def _revalidate(item: Recommendation, now: datetime) -> ApplyResult:
    """Repeat every safety-critical check against the live filesystem.

    The index is only a hint. Nothing is deleted unless the exact recipe still
    matches, the manifest and lock file are still present, and the project is
    still outside the inactivity window right now.
    """
    path = item.path
    recipe = RECIPES_BY_ID.get(item.detector_id)

    def fail(outcome: ApplyOutcome, error: str) -> ApplyResult:
        return ApplyResult(
            recommendation_id=item.id,
            label=item.label,
            path=normalized_path(path),
            reclaimable_bytes=item.reclaimable_bytes,
            outcome=outcome,
            dry_run=False,
            error=error,
        )

    if recipe is None:
        return fail(ApplyOutcome.FAILED, "unknown detector: {}".format(item.detector_id))
    if item.action is not ActionKind.DELETE_TREE:
        return fail(
            ApplyOutcome.FAILED, "unsupported action: {}".format(item.action.value)
        )
    if os.path.islink(str(path)):
        return fail(ApplyOutcome.FAILED, "refusing to clean a symlink")
    if not path.exists():
        return fail(ApplyOutcome.SKIPPED, "path no longer exists")
    if not path.is_dir():
        return fail(ApplyOutcome.FAILED, "expected a directory")

    root = item.safety_root
    expected = root / recipe.relative_path
    if normalized_path(expected) != normalized_path(path):
        return fail(ApplyOutcome.FAILED, "path no longer matches its recipe")
    if os.path.islink(str(root)) or not root.is_dir():
        return fail(ApplyOutcome.FAILED, "safety root is missing or is a symlink")

    # `islink` only tests the final component, so an ancestor symlink can point
    # the whole candidate outside the project. Compare fully resolved paths on
    # segment boundaries -- a raw startswith would accept /a/bcd inside /a/bc,
    # and both sides must be resolved because /tmp and /var are themselves
    # symlinks on macOS.
    resolved_path = os.path.realpath(str(path))
    resolved_root = os.path.realpath(str(root))
    if resolved_path != resolved_root and not resolved_path.startswith(
        resolved_root + os.sep
    ):
        return fail(
            ApplyOutcome.FAILED, "target resolves outside its project directory"
        )

    if recipe.manifests and not any((root / name).exists() for name in recipe.manifests):
        return fail(ApplyOutcome.CHANGED_SINCE_SCAN, "project manifest disappeared")
    if recipe.locks and not any((root / name).exists() for name in recipe.locks):
        return fail(
            ApplyOutcome.CHANGED_SINCE_SCAN,
            "the lock file needed to restore this is gone",
        )

    activity = last_meaningful_activity(root, now=now)
    if activity is None:
        return fail(ApplyOutcome.CHANGED_SINCE_SCAN, "project activity is unknown")
    threshold = now - timedelta(days=INACTIVITY_DAYS)
    if activity >= threshold:
        return fail(ApplyOutcome.CHANGED_SINCE_SCAN, "the project was edited recently")

    # The source-tree activity check above deliberately excludes generated
    # directories like this artifact, so a file hand-edited today inside it
    # (a patch-package fix, a `pip install -e`, a locally patched gem) would
    # otherwise be invisible right up until deletion. This is the check that
    # actually prevents deletion: re-derive it from the live filesystem
    # rather than trusting whatever the index recorded at scan time.
    artifact_activity = artifact_recent_activity(path, threshold)
    if artifact_activity is not None:
        return fail(
            ApplyOutcome.CHANGED_SINCE_SCAN,
            "the artifact itself was edited recently",
        )

    return ApplyResult(
        recommendation_id=item.id,
        label=item.label,
        path=normalized_path(path),
        reclaimable_bytes=item.reclaimable_bytes,
        outcome=ApplyOutcome.REMOVED,
        dry_run=False,
    )


def apply_recommendations(
    recommendation_ids: Sequence[str],
    index: ScanIndex,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> List[ApplyResult]:
    """Apply recommendations by ID only.

    A caller can never hand this function a path: authority comes from the
    current complete generation of the index plus a live revalidation.
    """
    moment = now or datetime.now(timezone.utc)
    results: List[ApplyResult] = []

    for recommendation_id in recommendation_ids:
        if should_cancel is not None and should_cancel():
            break

        item = index.load_recommendation(recommendation_id)
        if item is None:
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

        checked = _revalidate(item, now=moment)
        if checked.outcome is not ApplyOutcome.REMOVED:
            results.append(
                ApplyResult(
                    recommendation_id=checked.recommendation_id,
                    label=checked.label,
                    path=checked.path,
                    reclaimable_bytes=checked.reclaimable_bytes,
                    outcome=checked.outcome,
                    dry_run=dry_run,
                    error=checked.error,
                )
            )
            continue

        if dry_run:
            results.append(
                ApplyResult(
                    recommendation_id=item.id,
                    label=item.label,
                    path=normalized_path(item.path),
                    reclaimable_bytes=item.reclaimable_bytes,
                    outcome=ApplyOutcome.SKIPPED,
                    dry_run=True,
                    error="dry run: nothing was removed",
                )
            )
            continue

        try:
            _remove_path(item.path)
        except OSError as exc:
            results.append(
                ApplyResult(
                    recommendation_id=item.id,
                    label=item.label,
                    path=normalized_path(item.path),
                    reclaimable_bytes=item.reclaimable_bytes,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=False,
                    error=str(exc),
                )
            )
            continue

        results.append(checked)

    return results
