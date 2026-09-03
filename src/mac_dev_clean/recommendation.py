from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, Tuple

from .model import human_bytes


class ActionKind(Enum):
    DELETE_CONTENTS = "delete_contents"
    DELETE_TREE = "delete_tree"
    MOVE_TO_TRASH = "move_to_trash"
    INVOKE_TOOL = "invoke_tool"
    REVEAL_ONLY = "reveal_only"


class Confidence(Enum):
    EXACT = "exact"
    STRONG = "strong"
    HEURISTIC = "heuristic"
    UNKNOWN = "unknown"


class RestorationCost(Enum):
    REBUILD = "rebuild"
    REDOWNLOAD = "redownload"
    REINSTALL = "reinstall"
    EXTERNAL_STATE = "external_state"
    NOT_APPLICABLE = "not_applicable"


DESTRUCTIVE_ACTIONS = frozenset(
    {ActionKind.DELETE_CONTENTS, ActionKind.DELETE_TREE, ActionKind.MOVE_TO_TRASH}
)


@dataclass(frozen=True)
class Evidence:
    code: str
    detail: str

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "detail": self.detail}


def normalized_path(path: Path) -> str:
    """Lexical normalization only. Never resolves symlinks: two different
    symlinks pointing at one target must keep distinct identities so the
    executor can refuse them individually."""
    return os.path.normpath(str(Path(path).expanduser()))


def recommendation_id(detector_id: str, path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(detector_id.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(normalized_path(path).encode("utf-8"))
    return digest.hexdigest()[:16]


@dataclass(frozen=True)
class Recommendation:
    detector_id: str
    category: str
    label: str
    path: Path
    action: ActionKind
    allocated_bytes: int
    reclaimable_bytes: int
    confidence: Confidence
    restoration: RestorationCost
    selected_by_default: bool
    evidence: Tuple[Evidence, ...]
    safety_root: Path
    reason: str
    generation: int
    last_activity_at: Optional[datetime] = None
    warning: str = ""

    def __post_init__(self) -> None:
        eligible = (
            self.action in DESTRUCTIVE_ACTIONS
            and self.confidence is not Confidence.UNKNOWN
        )
        if self.selected_by_default and not eligible:
            object.__setattr__(self, "selected_by_default", False)

    @property
    def id(self) -> str:
        return recommendation_id(self.detector_id, self.path)

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "detector_id": self.detector_id,
            "category": self.category,
            "label": self.label,
            "path": normalized_path(self.path),
            "action": self.action.value,
            "allocated_bytes": self.allocated_bytes,
            "reclaimable_bytes": self.reclaimable_bytes,
            "size": human_bytes(self.reclaimable_bytes),
            "confidence": self.confidence.value,
            "restoration": self.restoration.value,
            "selected_by_default": self.selected_by_default,
            "evidence": [item.to_dict() for item in self.evidence],
            "safety_root": normalized_path(self.safety_root),
            "reason": self.reason,
            "warning": self.warning,
            "generation": self.generation,
            "last_activity_at": self.last_activity_at.astimezone(timezone.utc).isoformat()
            if self.last_activity_at
            else None,
        }
