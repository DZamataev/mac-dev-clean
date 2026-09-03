from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Sequence, TextIO

from .recommendation import Recommendation, normalized_path

PROTOCOL_VERSION = 1


class EventEmitter:
    """Writes newline-delimited JSON events so the native app can render a
    Deep Scan while it is still running."""

    def __init__(self, stream: TextIO, generation: int) -> None:
        self._stream = stream
        self._generation = generation

    def _emit(self, event: str, **payload: Any) -> None:
        envelope: Dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "generation": self._generation,
            "event": event,
        }
        envelope.update(payload)
        # ensure_ascii keeps surrogate-escaped filenames from raising during
        # encoding; a single scan must never die on one odd path.
        self._stream.write(json.dumps(envelope, ensure_ascii=True, sort_keys=True))
        self._stream.write("\n")
        self._stream.flush()

    def scan_started(self, roots: Sequence[Path], incremental: bool) -> None:
        self._emit(
            "scan_started",
            roots=[normalized_path(root) for root in roots],
            incremental=incremental,
        )

    def root_started(self, path: Path) -> None:
        self._emit("root_started", path=normalized_path(path))

    def progress(self, path: str, scanned: int) -> None:
        self._emit("progress", path=path, scanned=scanned)

    def candidate_found(self, item: Recommendation) -> None:
        self._emit("candidate_found", recommendation=item.to_dict())

    def root_finished(self, path: Path, repositories: int) -> None:
        self._emit(
            "root_finished", path=normalized_path(path), repositories=repositories
        )

    def permission_required(self, path: Path, folder: str) -> None:
        self._emit("permission_required", path=normalized_path(path), folder=folder)

    def warning(self, message: str, path: str = "") -> None:
        self._emit("warning", message=message, path=path)

    def scan_completed(self, reclaimable_bytes: int, count: int) -> None:
        self._emit("scan_completed", reclaimable_bytes=reclaimable_bytes, count=count)

    def scan_cancelled(self, reclaimable_bytes: int, count: int) -> None:
        self._emit("scan_cancelled", reclaimable_bytes=reclaimable_bytes, count=count)
