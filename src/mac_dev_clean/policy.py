from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from .projects import INACTIVITY_DAYS, ArtifactFact, ArtifactKind
from .recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
)

CLOSE_TOOLS_WARNING = (
    "Close Xcode, Android Studio, and any running build before cleaning this project."
)


def _days_since(moment: datetime, now: datetime) -> int:
    return max(0, int((now - moment).total_seconds() // 86400))


def recommend(
    fact: ArtifactFact, generation: int, now: Optional[datetime] = None
) -> Recommendation:
    """Turn one artifact fact into an explainable recommendation.

    Deterministic and filesystem-free: everything it needs was already measured
    by the analyzer, so the same facts always produce the same output.
    """
    moment = now or datetime.now(timezone.utc)
    evidence: List[Evidence] = [
        Evidence("manifest", fact.manifest_detail),
        Evidence("recipe", "path matches {}".format(fact.recipe.relative_path)),
    ]

    if fact.last_activity_at is None:
        confidence = Confidence.UNKNOWN
        reason = "Last meaningful change is unknown, so nothing is preselected."
        evidence.append(Evidence("inactivity", "no readable source timestamps"))
        selected = False
    else:
        age = _days_since(fact.last_activity_at, moment)
        evidence.append(
            Evidence("inactivity", "newest meaningful change {} days ago".format(age))
        )
        if fact.inactive:
            confidence = Confidence.STRONG
            reason = "Project has not changed in {} days.".format(age)
            selected = True
        else:
            confidence = Confidence.HEURISTIC
            reason = "Project changed {} days ago, inside the {}-day window.".format(
                age, INACTIVITY_DAYS
            )
            selected = False

    if fact.recipe.locks:
        evidence.append(Evidence("lock-file", fact.lock_detail))
        if not fact.lock_satisfied:
            selected = False
            confidence = (
                Confidence.HEURISTIC if confidence is not Confidence.UNKNOWN else confidence
            )
            reason = "{} No supported lock file, so this cannot be restored exactly.".format(
                reason
            )

    if fact.kind is ArtifactKind.DEPENDENCY_TREE and not fact.lock_satisfied:
        selected = False

    return Recommendation(
        detector_id=fact.recipe.detector_id,
        category=fact.recipe.category,
        label=fact.recipe.label,
        path=fact.path,
        action=ActionKind.DELETE_TREE,
        allocated_bytes=fact.allocated_bytes,
        reclaimable_bytes=fact.allocated_bytes,
        confidence=confidence,
        restoration=fact.recipe.restoration,
        selected_by_default=selected,
        evidence=tuple(evidence),
        safety_root=fact.repository.path,
        reason=reason,
        generation=generation,
        last_activity_at=fact.last_activity_at,
        warning=CLOSE_TOOLS_WARNING,
    )
