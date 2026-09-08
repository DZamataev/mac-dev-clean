from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from .fsevents import VolumeIdentity
from .recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    ToolAction,
)

SCHEMA_VERSION = 1

DEFAULT_INDEX_PATH = Path("~/Library/Caches/mac-dev-clean/index.sqlite3")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS generations (
    generation INTEGER PRIMARY KEY AUTOINCREMENT,
    device INTEGER NOT NULL,
    volume_uuid TEXT,
    event_id INTEGER NOT NULL,
    started_at REAL NOT NULL,
    completed_at REAL
);
CREATE TABLE IF NOT EXISTS recommendations (
    id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (id, generation)
);
CREATE INDEX IF NOT EXISTS recommendations_by_generation
    ON recommendations (generation);
"""


@dataclass(frozen=True)
class IndexSnapshot:
    generation: int
    device: int
    volume_uuid: Optional[str]
    event_id: int
    completed_at: float


class ScanIndex:
    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self._connection = connection
        self._path = path
        self._generation: Optional[int] = None

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._connection.close()

    def reset(self) -> None:
        self._connection.close()
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        self._connection = _connect(self._path)
        self._generation = None

    def schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row[0]) if row else 0

    def execute_for_test(self, sql: str, parameters: Tuple = ()) -> None:
        """Deliberate test seam so tests can simulate stale schema rows."""
        self._connection.execute(sql, parameters)
        self._connection.commit()

    # -- generations --------------------------------------------------------

    def begin_generation(self, volume: VolumeIdentity, event_id: int) -> int:
        cursor = self._connection.execute(
            "INSERT INTO generations (device, volume_uuid, event_id, started_at)"
            " VALUES (?, ?, ?, ?)",
            (volume.device, volume.uuid, event_id, time.time()),
        )
        self._connection.commit()
        self._generation = int(cursor.lastrowid)
        return self._generation

    def commit_batch(self) -> None:
        self._connection.commit()

    def complete_generation(self) -> None:
        if self._generation is None:
            raise ValueError("no generation is in progress")
        self._connection.execute(
            "UPDATE generations SET completed_at = ? WHERE generation = ?",
            (time.time(), self._generation),
        )
        self._connection.execute(
            "DELETE FROM recommendations WHERE generation != ?", (self._generation,)
        )
        self._connection.execute(
            "DELETE FROM generations WHERE generation != ?", (self._generation,)
        )
        self._connection.commit()

    def abandon_generation(self) -> None:
        """Leave an interrupted generation on disk but never complete it, so
        `latest_complete_generation` keeps returning the previous good scan."""
        self._generation = None

    def latest_complete_generation(self) -> Optional[int]:
        row = self._connection.execute(
            "SELECT generation FROM generations WHERE completed_at IS NOT NULL"
            " ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else None

    def snapshot(self) -> Optional[IndexSnapshot]:
        row = self._connection.execute(
            "SELECT generation, device, volume_uuid, event_id, completed_at"
            " FROM generations WHERE completed_at IS NOT NULL"
            " ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return IndexSnapshot(
            generation=int(row[0]),
            device=int(row[1]),
            volume_uuid=row[2],
            event_id=int(row[3]),
            completed_at=float(row[4]),
        )

    # -- recommendations ----------------------------------------------------

    def record_recommendation(self, item: Recommendation) -> None:
        if self._generation is None:
            raise ValueError("no generation is in progress")
        self._connection.execute(
            "INSERT OR REPLACE INTO recommendations (id, generation, payload)"
            " VALUES (?, ?, ?)",
            (item.id, self._generation, json.dumps(item.to_dict(), sort_keys=True)),
        )

    def load_recommendation(self, recommendation_id: str) -> Optional[Recommendation]:
        generation = self.latest_complete_generation()
        if generation is None:
            return None
        row = self._connection.execute(
            "SELECT payload FROM recommendations WHERE id = ? AND generation = ?",
            (recommendation_id, generation),
        ).fetchone()
        if row is None:
            return None
        return _decode(json.loads(row[0]))


def _decode(payload: dict) -> Recommendation:
    raw_activity = payload.get("last_activity_at")
    raw_tool = payload.get("tool_action")
    tool_action = (
        ToolAction(
            tool=raw_tool["tool"],
            resource=raw_tool["resource"],
            argv=tuple(raw_tool["argv"]),
            preview_argv=tuple(raw_tool["preview_argv"]),
            reported=raw_tool.get("reported", ""),
        )
        if raw_tool
        else None
    )
    return Recommendation(
        detector_id=payload["detector_id"],
        category=payload["category"],
        label=payload["label"],
        path=Path(payload["path"]),
        action=ActionKind(payload["action"]),
        allocated_bytes=int(payload["allocated_bytes"]),
        reclaimable_bytes=int(payload["reclaimable_bytes"]),
        confidence=Confidence(payload["confidence"]),
        restoration=RestorationCost(payload["restoration"]),
        selected_by_default=bool(payload["selected_by_default"]),
        evidence=tuple(
            Evidence(entry["code"], entry["detail"]) for entry in payload["evidence"]
        ),
        safety_root=Path(payload["safety_root"]),
        reason=payload.get("reason", ""),
        generation=int(payload["generation"]),
        last_activity_at=datetime.fromisoformat(raw_activity) if raw_activity else None,
        tool_action=tool_action,
        warning=payload.get("warning", ""),
    )


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # sqlite3.connect() succeeds even for a corrupt file because the database is
    # opened lazily; the failure surfaces on the first statement. Close the
    # handle before propagating, or a rebuilt index leaks the dead connection.
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        connection.commit()
    except Exception:
        connection.close()
        raise
    return connection


def _read_schema_version(path: Path) -> Optional[int]:
    """Read the stored schema version without mutating it.

    Returns None when the file, its meta table, or the schema_version row is
    missing, or when the file cannot be read as a database at all. Always
    closes its connection on every path, including failure: like `_connect`,
    `sqlite3.connect()` succeeds even against a corrupt file because the
    database is opened lazily, so the failure surfaces on the first
    statement rather than on connect.
    """
    try:
        connection = sqlite3.connect(str(path))
    except (sqlite3.DatabaseError, OSError):
        return None
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except (sqlite3.DatabaseError, OSError):
        return None
    finally:
        connection.close()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def open_index(path: Optional[Path] = None) -> ScanIndex:
    """Open the index, rebuilding it whenever it is corrupt or outdated.

    The index is a cache. Discarding it is always safe, so any failure to read
    it is resolved by deleting the file rather than by surfacing an error.
    """
    target = Path(path) if path is not None else DEFAULT_INDEX_PATH.expanduser()
    target = target.expanduser()

    if target.exists():
        # Decide from the version on disk BEFORE `_connect` gets a chance to
        # overwrite it with the current one -- that overwrite is exactly what
        # made the old-schema rebuild path unreachable.
        version = _read_schema_version(target)
        if version is None or version != SCHEMA_VERSION:
            try:
                target.unlink()
            except OSError:
                pass

    try:
        return ScanIndex(_connect(target), target)
    except (sqlite3.DatabaseError, ValueError, OSError):
        try:
            target.unlink()
        except OSError:
            pass
        return ScanIndex(_connect(target), target)
