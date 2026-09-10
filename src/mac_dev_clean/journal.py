from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

DEFAULT_JOURNAL_PATH = Path("~/Library/Logs/mac-dev-clean/actions.jsonl")

#: Rotate at 4 MiB. Large enough to hold months of ordinary use, small enough
#: that reading the file during an investigation stays instant.
MAX_JOURNAL_BYTES = 4 * 1024 * 1024

ROTATED_SUFFIX = ".jsonl.1"


def rotated_journal_path(path: Path) -> Path:
    """Return the one retained rotation for any active journal path."""
    return Path(path).with_suffix(ROTATED_SUFFIX)


@dataclass(frozen=True)
class JournalRecord:
    """One attempted action.

    `target` is a normalized filesystem path for path actions, or a tool
    resource identifier such as `docker:build-cache` for tool actions.
    `argv` is empty for path actions and holds the preview or action vector
    associated with a tool attempt. A refusal or unavailable tool can record the
    requested vector before a process starts, so `outcome` and `detail` remain
    authoritative about what happened.
    """

    recommendation_id: str
    detector_id: str
    category: str
    target: str
    action: str
    outcome: str
    reclaimable_bytes: int
    dry_run: bool
    argv: Tuple[str, ...] = ()
    detail: str = ""
    recorded_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, object]:
        moment = self.recorded_at or datetime.now(timezone.utc)
        return {
            "timestamp": moment.astimezone(timezone.utc).isoformat(),
            "recommendation_id": self.recommendation_id,
            "detector_id": self.detector_id,
            "category": self.category,
            "target": self.target,
            "action": self.action,
            "argv": list(self.argv),
            "reclaimable_bytes": self.reclaimable_bytes,
            "dry_run": self.dry_run,
            "outcome": self.outcome,
            "detail": self.detail,
        }


class ActionJournal:
    """Append-only NDJSON record of every attempted action.

    A journal failure is never allowed to change what happens to the user's
    files: `append` reports success as a bool and swallows I/O errors, so a
    caller that has already deleted something can still report that fact.

    When `append` fails it records a short, non-sensitive explanation on
    `last_warning` (e.g. the OSError's class name) so a caller can surface
    "journal write failed" without us leaking full filesystem paths or other
    sensitive detail into logs or UI. A later successful append clears it.
    """

    #: Keep any stored warning short and free of path/content detail.
    _MAX_WARNING_LEN = 120

    def __init__(self, path: Path) -> None:
        self._path = Path(path).expanduser()
        self._last_warning: Optional[str] = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_warning(self) -> Optional[str]:
        return self._last_warning

    def append(self, record: JournalRecord) -> bool:
        rotation_warning = None
        try:
            # ensure_ascii keeps surrogate-escaped filenames from raising
            # during encoding; one odd path must never cost us the audit
            # record. Serialization runs inside the try so a future
            # non-serializable field can never raise uncaught here either.
            line = json.dumps(record.to_dict(), ensure_ascii=True, sort_keys=True)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            rotation_warning = self._rotate_if_needed()
            with open(str(self._path), "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, TypeError, ValueError) as exc:
            self._last_warning = self._summarize(exc)
            return False
        self._last_warning = rotation_warning
        return True

    def _summarize(self, exc: Exception) -> str:
        # Use only the exception's class name and errno (when present),
        # never its filename or message text, so the stored detail can
        # never carry a path or other sensitive content.
        name = type(exc).__name__
        errno = getattr(exc, "errno", None)
        summary = "{0} (errno {1})".format(name, errno) if errno is not None else name
        return summary[: self._MAX_WARNING_LEN]

    def _rotate_if_needed(self) -> Optional[str]:
        try:
            size = self._path.stat().st_size
        except OSError:
            return None
        if size <= MAX_JOURNAL_BYTES:
            return None
        rotated = rotated_journal_path(self._path)
        try:
            os.replace(str(self._path), str(rotated))
        except OSError as exc:
            # Rotation is housekeeping. Failing to rotate must never stop an
            # action from being recorded, so fall through and keep
            # appending, but surface the failure via last_warning instead
            # of silently letting the file grow unboundedly.
            return self._summarize(exc)
        return None


def append_with_warning(journal: ActionJournal, record: JournalRecord) -> str:
    """Append without blocking the action and return a caller-safe warning."""
    try:
        appended = journal.append(record)
        warning = getattr(journal, "last_warning", None)
    except Exception:
        return "journal write failed"
    if not appended or warning:
        return "journal write failed"
    return ""


def open_journal(path: Optional[Path] = None) -> ActionJournal:
    target = Path(path) if path is not None else DEFAULT_JOURNAL_PATH
    return ActionJournal(target.expanduser())
