# Phase 1: Foundation and Project Analyzer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give mac-dev-clean an explicit, cancellable Deep Scan that finds Git repositories anywhere under the home directory, recommends only lock-file-backed reproducible project artifacts from projects inactive for 90 days, and applies them through revalidated recommendation IDs.

**Architecture:** A new Python subsystem (`recommendation` → `index` → `fsevents` → `discovery` → `projects` → `policy` → `executor`, orchestrated by `deep_scan`) sits beside the existing fixed-location scanner. It streams NDJSON events on stdout so the SwiftUI app can render progress incrementally and cancel mid-scan. The legacy `scan`/`report`/`clean` commands and their JSON contracts are untouched.

**Tech Stack:** Python 3.9+ stdlib only (`sqlite3`, `ctypes` against CoreServices/CoreFoundation, `unittest`), Swift 6 / SwiftUI (macOS 14+), `swift-testing`.

**Spec:** `docs/superpowers/specs/2026-09-03-universal-storage-analyzer-design.md`

## Global Constraints

- Python 3.9 is the floor (`pyproject.toml` `requires-python = ">=3.9"`). Every new module starts with `from __future__ import annotations`. Never use `X | Y` unions, `match`, or `dict[str, int]` outside annotations at runtime.
- Zero third-party Python dependencies. `pyproject.toml` has no `dependencies` key and must keep none. FSEvents access is via `ctypes` only.
- Python tests run with `PYTHONPATH=src python -m unittest discover -s tests` and must pass on 3.9 and 3.13.
- Swift tests run with `swift test --package-path macos`.
- `./scripts/audit_public_repo.sh` must pass. It greps every tracked **and untracked** file for absolute home paths and fails on any that is not under the allowlisted `test`, `example`, or `<local-user>` user names. **The allowlist requires a following path separator**, so a bare `…/Users/test/home` with nothing after it is still rejected — always write a further segment, e.g. `/Users/test/home` or `/Users/test/app`. This applies to fixtures, docstrings, tests, and this plan itself.
- Destructive tests call exported functions with explicit temporary roots. Never invoke `main()` or `deep_scan()` against a real home directory in a test.
- Direct deletion stays narrow: only detector-validated recipes. Personal files and applications are out of scope for Phase 1 entirely.
- Existing public names that must keep working unchanged: `mac_dev_clean.scanner.scan`, `mac_dev_clean.cleaner.clean_targets`, `mac_dev_clean.model.ScanTarget`, and the `scan --json` / `report --json` / `clean` CLI contracts.
- Reuse `mac_dev_clean.scanner.path_size` for allocated-size measurement and `mac_dev_clean.model.human_bytes` for display strings. Do not write a second byte formatter.

---

## Verified Platform Facts

These were confirmed by executable spikes on macOS 26.5.2 before this plan was written. Do not re-derive them; do not design around contradicting assumptions.

1. **FSEvents history replay works from stdlib `ctypes`.** `CoreServices.FSEventStreamCreate` with a `sinceWhen` event id, driven by `CFRunLoopRunInMode`, delivers historical paths and then a `kFSEventStreamEventFlagHistoryDone` (0x10) sentinel event. Observed: 5 historical paths replayed for a temp tree.

2. **`mustScanSubDirs` is NOT a reliable gap detector.** Replaying with a deliberately ancient `sinceWhen` of `1` returned only the still-retained history with **zero** `kFSEventStreamEventFlagMustScanSubDirs` (0x1) events. The daemon silently truncates. Code that trusts this flag to detect history loss will miss changes and produce unsafe stale recommendations.

3. **The working gap detector is `FSEventsGetLastEventIdForDeviceBeforeTime`.** It returns `0` when no history is retained before the given wall-clock time. Observed on the dev machine: `now` → `2049811702`, `1 day ago` → `2008526162`, `7 days ago` → `1862620702`, `90 days ago` → `0`, `epoch+1` → `0`. So retained history was roughly a week. A `0` return, or a returned id greater than the stored `last_event_id`, means events were lost → fall back to a full metadata walk.

4. **`FSEventsCopyUUIDForDevice` yields a stable volume identity.** Observed for the home device (`st_dev` = 16777234): `6479BD6D-19A3-47E8-B341-B582896F9D14`. A `NULL` return means the volume does not support persistent events → always full walk.

5. **Git worktrees and submodules are distinguishable without shelling out to `git`.** A linked worktree's `.git` file contains `gitdir: <abs>/.git/worktrees/<name>`; a submodule's contains `gitdir: ../.git/modules/<name>` (relative). A primary repository has a `.git` **directory**.

---

## File Structure

**New Python modules** (all under `src/mac_dev_clean/`):

| File | Responsibility |
|---|---|
| `recommendation.py` | `Recommendation`, `Evidence`, action/confidence/restoration enums, stable ID derivation. Pure data, no I/O. |
| `fsevents.py` | ctypes binding: volume UUID, current event id, history-gap probe, historical path replay. The only file that touches CoreServices. |
| `index.py` | SQLite schema, generation lifecycle, entry upsert, dirty-path marking, reset. |
| `discovery.py` | Bounded filesystem walk; Git repo / worktree / submodule classification. |
| `projects.py` | Meaningful-activity calculation and the artifact recipe table with per-recipe validators. |
| `policy.py` | Facts → `Recommendation`. Deterministic, filesystem-free. |
| `executor.py` | Resolve recommendation ID from index, revalidate, perform `delete_tree`/`delete_contents`. |
| `events.py` | NDJSON event envelope construction and emission. |
| `deep_scan.py` | Orchestrates discovery → analysis → policy → index → events; cancellation. |

**New Python tests** (under `tests/`): `test_recommendation.py`, `test_fsevents.py`, `test_index.py`, `test_discovery.py`, `test_projects.py`, `test_policy.py`, `test_events.py`, `test_deep_scan.py`, `test_executor.py`, `test_deep_scan_cli.py`.

**Modified Python:** `cli.py` (add `deep-scan`, `apply`, `reset-index` subcommands; touch nothing else).

**New Swift** (under `macos/Sources/MacDevCleanApp/`): `DeepScanModels.swift`, `DeepScanBackend.swift`. **Modified Swift:** `AppModel.swift`, `ContentView.swift`. **New Swift test:** `macos/Tests/MacDevCleanAppTests/DeepScanTests.swift`.

---

### Task 1: Recommendation model

**Files:**
- Create: `src/mac_dev_clean/recommendation.py`
- Test: `tests/test_recommendation.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ActionKind`, `Confidence`, `RestorationCost`, `Evidence(code: str, detail: str)`, `Recommendation` dataclass with `.to_dict()`, and `recommendation_id(detector_id: str, path: Path) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_recommendation.py`:

```python
from pathlib import Path
import unittest

from mac_dev_clean.recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    recommendation_id,
)


class RecommendationIdTests(unittest.TestCase):
    def test_id_is_stable_for_the_same_detector_and_path(self):
        first = recommendation_id("node-modules", Path("/Users/test/app/node_modules"))
        second = recommendation_id("node-modules", Path("/Users/test/app/node_modules"))

        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)

    def test_id_changes_with_detector(self):
        self.assertNotEqual(
            recommendation_id("node-modules", Path("/Users/test/app/node_modules")),
            recommendation_id("ios-pods", Path("/Users/test/app/node_modules")),
        )

    def test_id_normalizes_trailing_separators_and_dot_segments(self):
        self.assertEqual(
            recommendation_id("node-modules", Path("/Users/test/app/node_modules")),
            recommendation_id("node-modules", Path("/Users/test/app/./node_modules/")),
        )


class RecommendationSerializationTests(unittest.TestCase):
    def build(self, **overrides):
        defaults = dict(
            detector_id="node-modules",
            category="project-dependencies",
            label="node_modules",
            path=Path("/Users/test/app/node_modules"),
            action=ActionKind.DELETE_TREE,
            allocated_bytes=2048,
            reclaimable_bytes=2048,
            confidence=Confidence.STRONG,
            restoration=RestorationCost.REBUILD,
            selected_by_default=True,
            evidence=(Evidence("lock-file", "pnpm-lock.yaml exists"),),
            safety_root=Path("/Users/test/app"),
            last_activity_at=None,
            reason="Project inactive for 143 days.",
            generation=7,
        )
        defaults.update(overrides)
        return Recommendation(**defaults)

    def test_to_dict_exposes_the_streaming_contract(self):
        payload = self.build().to_dict()

        self.assertEqual(payload["id"], recommendation_id("node-modules", Path("/Users/test/app/node_modules")))
        self.assertEqual(payload["action"], "delete_tree")
        self.assertEqual(payload["confidence"], "strong")
        self.assertEqual(payload["restoration"], "rebuild")
        self.assertEqual(payload["path"], "/Users/test/app/node_modules")
        self.assertEqual(payload["safety_root"], "/Users/test/app")
        self.assertEqual(payload["size"], "2.0 KB")
        self.assertTrue(payload["selected_by_default"])
        self.assertEqual(payload["evidence"], [{"code": "lock-file", "detail": "pnpm-lock.yaml exists"}])

    def test_report_only_recommendations_are_never_selected(self):
        item = self.build(action=ActionKind.REVEAL_ONLY, selected_by_default=True)

        self.assertFalse(item.selected_by_default)
        self.assertFalse(item.to_dict()["selected_by_default"])

    def test_unknown_confidence_is_never_selected(self):
        item = self.build(confidence=Confidence.UNKNOWN, selected_by_default=True)

        self.assertFalse(item.selected_by_default)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_recommendation -v` (from the repository root)
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.recommendation'`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/recommendation.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_recommendation -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/recommendation.py tests/test_recommendation.py
git commit -m "feat: add recommendation model with stable ids and selection guards"
```

---

### Task 2: FSEvents binding and gap detection

**Files:**
- Create: `src/mac_dev_clean/fsevents.py`
- Test: `tests/test_fsevents.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `HistoryProbe` dataclass, `volume_identity(path) -> Optional[VolumeIdentity]`, `current_event_id() -> int`, `probe_history(device, since_event_id, since_time) -> HistoryProbe`, `replay_changed_paths(roots, since_event_id, timeout_seconds) -> Optional[Set[str]]`, and `FSEVENTS_AVAILABLE: bool`.

**Critical:** per Verified Platform Fact 2, `mustScanSubDirs` must NOT be the gap detector. `probe_history` is authoritative.

- [ ] **Step 1: Write the failing test**

Create `tests/test_fsevents.py`:

```python
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mac_dev_clean import fsevents


class HistoryProbeTests(unittest.TestCase):
    def test_missing_history_forces_a_full_walk(self):
        with patch.object(fsevents, "_last_event_id_before_time", return_value=0):
            probe = fsevents.probe_history(device=1, since_event_id=500, since_time=1000.0)

        self.assertFalse(probe.usable)
        self.assertEqual(probe.reason, "no-retained-history")

    def test_truncated_history_forces_a_full_walk(self):
        # The daemon still remembers events, but only ones newer than what we
        # indexed. Anything between our snapshot and 900 was dropped.
        with patch.object(fsevents, "_last_event_id_before_time", return_value=900):
            probe = fsevents.probe_history(device=1, since_event_id=500, since_time=1000.0)

        self.assertFalse(probe.usable)
        self.assertEqual(probe.reason, "history-truncated")

    def test_continuous_history_is_usable(self):
        with patch.object(fsevents, "_last_event_id_before_time", return_value=400):
            probe = fsevents.probe_history(device=1, since_event_id=500, since_time=1000.0)

        self.assertTrue(probe.usable)
        self.assertEqual(probe.reason, "")

    def test_unavailable_framework_forces_a_full_walk(self):
        with patch.object(fsevents, "FSEVENTS_AVAILABLE", False):
            probe = fsevents.probe_history(device=1, since_event_id=500, since_time=1000.0)

        self.assertFalse(probe.usable)
        self.assertEqual(probe.reason, "fsevents-unavailable")

    def test_zero_since_event_id_is_never_usable(self):
        with patch.object(fsevents, "_last_event_id_before_time", return_value=1):
            probe = fsevents.probe_history(device=1, since_event_id=0, since_time=1000.0)

        self.assertFalse(probe.usable)
        self.assertEqual(probe.reason, "no-baseline")


class VolumeIdentityTests(unittest.TestCase):
    def test_volume_identity_reports_device_and_uuid_for_a_real_path(self):
        with TemporaryDirectory() as temp:
            identity = fsevents.volume_identity(Path(temp))
            expected_device = os.stat(temp).st_dev

        self.assertIsNotNone(identity)
        self.assertEqual(identity.device, expected_device)

    def test_volume_identity_is_none_for_a_missing_path(self):
        self.assertIsNone(fsevents.volume_identity(Path("/Users/test/does/not/exist")))

    def test_identity_without_uuid_is_not_indexable(self):
        identity = fsevents.VolumeIdentity(device=5, uuid=None)

        self.assertFalse(identity.supports_history)


class ReplayTests(unittest.TestCase):
    def test_replay_returns_none_when_history_is_unavailable(self):
        with patch.object(fsevents, "FSEVENTS_AVAILABLE", False):
            self.assertIsNone(
                fsevents.replay_changed_paths(
                    [Path("/Users/test/home")], since_event_id=1, timeout_seconds=0.1
                )
            )

    def test_replay_observes_a_real_change(self):
        if not fsevents.FSEVENTS_AVAILABLE:
            self.skipTest("FSEvents is unavailable on this host")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = fsevents.current_event_id()
            (root / "marker.txt").write_text("hello")
            changed = fsevents.replay_changed_paths(
                [root], since_event_id=baseline, timeout_seconds=6.0
            )

        if changed is None:
            self.skipTest("FSEvents did not deliver history for a temporary volume")
        self.assertTrue(any("marker.txt" in path for path in changed))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_fsevents -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.fsevents'`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/fsevents.py`:

```python
from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Set

# Documented CoreServices constants.
_FLAG_MUST_SCAN_SUBDIRS = 0x00000001
_FLAG_USER_DROPPED = 0x00000002
_FLAG_KERNEL_DROPPED = 0x00000004
_FLAG_EVENT_IDS_WRAPPED = 0x00000008
_FLAG_HISTORY_DONE = 0x00000010
_FLAG_ROOT_CHANGED = 0x00000020

_CREATE_FLAG_NO_DEFER = 0x00000002
_CREATE_FLAG_WATCH_ROOT = 0x00000004
_CREATE_FLAG_FILE_EVENTS = 0x00000010

_UTF8 = 0x08000100
_EVENT_ID_NOT_FOUND = 0

DROP_FLAGS = _FLAG_USER_DROPPED | _FLAG_KERNEL_DROPPED | _FLAG_EVENT_IDS_WRAPPED


def _load() -> tuple:
    try:
        cf_name = ctypes.util.find_library("CoreFoundation")
        cs_name = ctypes.util.find_library("CoreServices")
        if not cf_name or not cs_name:
            return (None, None)
        return (ctypes.CDLL(cf_name, use_errno=True), ctypes.CDLL(cs_name, use_errno=True))
    except OSError:
        return (None, None)


_CF, _CS = _load()
FSEVENTS_AVAILABLE = _CF is not None and _CS is not None

if FSEVENTS_AVAILABLE:
    _CFIndex = ctypes.c_long
    _EventId = ctypes.c_uint64

    _CF.CFStringCreateWithCString.restype = ctypes.c_void_p
    _CF.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    _CF.CFStringGetCString.restype = ctypes.c_bool
    _CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    _CF.CFArrayCreate.restype = ctypes.c_void_p
    _CF.CFArrayCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), _CFIndex, ctypes.c_void_p]
    _CF.CFRelease.argtypes = [ctypes.c_void_p]
    _CF.CFUUIDCreateString.restype = ctypes.c_void_p
    _CF.CFUUIDCreateString.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _CF.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    _CF.CFRunLoopRunInMode.restype = ctypes.c_int32
    _CF.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]

    _RUN_LOOP_DEFAULT_MODE = ctypes.c_void_p.in_dll(_CF, "kCFRunLoopDefaultMode")
    _ARRAY_CALLBACKS = ctypes.c_void_p.in_dll(_CF, "kCFTypeArrayCallBacks")

    _CS.FSEventsGetCurrentEventId.restype = _EventId
    _CS.FSEventsGetCurrentEventId.argtypes = []
    _CS.FSEventsGetLastEventIdForDeviceBeforeTime.restype = _EventId
    _CS.FSEventsGetLastEventIdForDeviceBeforeTime.argtypes = [ctypes.c_int32, ctypes.c_double]
    _CS.FSEventsCopyUUIDForDevice.restype = ctypes.c_void_p
    _CS.FSEventsCopyUUIDForDevice.argtypes = [ctypes.c_int32]

    _CALLBACK = ctypes.CFUNCTYPE(
        None,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(_EventId),
    )

    _CS.FSEventStreamCreate.restype = ctypes.c_void_p
    _CS.FSEventStreamCreate.argtypes = [
        ctypes.c_void_p,
        _CALLBACK,
        ctypes.c_void_p,
        ctypes.c_void_p,
        _EventId,
        ctypes.c_double,
        ctypes.c_uint32,
    ]
    _CS.FSEventStreamScheduleWithRunLoop.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _CS.FSEventStreamStart.restype = ctypes.c_bool
    _CS.FSEventStreamStart.argtypes = [ctypes.c_void_p]
    _CS.FSEventStreamFlushSync.argtypes = [ctypes.c_void_p]
    _CS.FSEventStreamStop.argtypes = [ctypes.c_void_p]
    _CS.FSEventStreamInvalidate.argtypes = [ctypes.c_void_p]
    _CS.FSEventStreamRelease.argtypes = [ctypes.c_void_p]


@dataclass(frozen=True)
class VolumeIdentity:
    device: int
    uuid: Optional[str]

    @property
    def supports_history(self) -> bool:
        return self.uuid is not None


@dataclass(frozen=True)
class HistoryProbe:
    usable: bool
    reason: str = ""


def current_event_id() -> int:
    if not FSEVENTS_AVAILABLE:
        return 0
    return int(_CS.FSEventsGetCurrentEventId())


def _last_event_id_before_time(device: int, when: float) -> int:
    if not FSEVENTS_AVAILABLE:
        return _EVENT_ID_NOT_FOUND
    return int(
        _CS.FSEventsGetLastEventIdForDeviceBeforeTime(
            ctypes.c_int32(device), ctypes.c_double(when)
        )
    )


def volume_identity(path: Path) -> Optional[VolumeIdentity]:
    try:
        device = os.stat(str(path)).st_dev
    except OSError:
        return None
    if not FSEVENTS_AVAILABLE:
        return VolumeIdentity(device=device, uuid=None)

    uuid_ref = _CS.FSEventsCopyUUIDForDevice(ctypes.c_int32(device))
    if not uuid_ref:
        return VolumeIdentity(device=device, uuid=None)
    string_ref = _CF.CFUUIDCreateString(None, uuid_ref)
    try:
        buffer = ctypes.create_string_buffer(64)
        if not _CF.CFStringGetCString(string_ref, buffer, 64, _UTF8):
            return VolumeIdentity(device=device, uuid=None)
        return VolumeIdentity(device=device, uuid=buffer.value.decode("ascii"))
    finally:
        if string_ref:
            _CF.CFRelease(string_ref)
        _CF.CFRelease(uuid_ref)


def probe_history(device: int, since_event_id: int, since_time: float) -> HistoryProbe:
    """Decide whether FSEvents can still explain what changed since our snapshot.

    A spike proved that replaying an ancient `sinceWhen` silently returns a
    truncated history with no mustScanSubDirs warning, so that flag cannot be
    trusted. `FSEventsGetLastEventIdForDeviceBeforeTime` is authoritative: it
    returns 0 when nothing before `since_time` is retained, and an id newer
    than our snapshot when the intervening events were discarded.
    """
    if not FSEVENTS_AVAILABLE:
        return HistoryProbe(usable=False, reason="fsevents-unavailable")
    if since_event_id <= 0:
        return HistoryProbe(usable=False, reason="no-baseline")

    oldest_retained = _last_event_id_before_time(device, since_time)
    if oldest_retained == _EVENT_ID_NOT_FOUND:
        return HistoryProbe(usable=False, reason="no-retained-history")
    if oldest_retained > since_event_id:
        return HistoryProbe(usable=False, reason="history-truncated")
    return HistoryProbe(usable=True)


def replay_changed_paths(
    roots: Sequence[Path],
    since_event_id: int,
    timeout_seconds: float = 10.0,
) -> Optional[Set[str]]:
    """Return every path FSEvents reports as touched since `since_event_id`.

    Returns None when the caller must fall back to a full metadata walk:
    FSEvents unavailable, stream creation failed, the daemon reported dropped
    events, or the history sentinel never arrived within the timeout.
    """
    if not FSEVENTS_AVAILABLE or not roots:
        return None

    changed: Set[str] = set()
    dropped = threading.Event()
    history_done = threading.Event()

    def _on_events(stream, info, count, event_paths, event_flags, event_ids):
        entries = ctypes.cast(event_paths, ctypes.POINTER(ctypes.c_char_p))
        for index in range(count):
            flags = event_flags[index]
            if flags & _FLAG_HISTORY_DONE:
                history_done.set()
                continue
            if flags & DROP_FLAGS:
                dropped.set()
            raw = entries[index]
            if not raw:
                continue
            path = raw.decode("utf-8", "replace")
            changed.add(path)
            if flags & (_FLAG_MUST_SCAN_SUBDIRS | _FLAG_ROOT_CHANGED):
                # Not a gap detector, but when it does arrive the whole
                # subtree below this path must be revisited.
                changed.add(os.path.join(path, "*"))

    callback = _CALLBACK(_on_events)
    string_refs: List[int] = []
    for root in roots:
        ref = _CF.CFStringCreateWithCString(None, str(root).encode("utf-8"), _UTF8)
        if ref:
            string_refs.append(ref)
    if not string_refs:
        return None

    array_items = (ctypes.c_void_p * len(string_refs))(*string_refs)
    paths_array = _CF.CFArrayCreate(
        None, array_items, len(string_refs), ctypes.byref(_ARRAY_CALLBACKS)
    )
    for ref in string_refs:
        _CF.CFRelease(ref)
    if not paths_array:
        return None

    stream = _CS.FSEventStreamCreate(
        None,
        callback,
        None,
        paths_array,
        ctypes.c_uint64(since_event_id),
        ctypes.c_double(0.2),
        _CREATE_FLAG_NO_DEFER | _CREATE_FLAG_FILE_EVENTS | _CREATE_FLAG_WATCH_ROOT,
    )
    _CF.CFRelease(paths_array)
    if not stream:
        return None

    try:
        _CS.FSEventStreamScheduleWithRunLoop(
            stream, _CF.CFRunLoopGetCurrent(), _RUN_LOOP_DEFAULT_MODE
        )
        if not _CS.FSEventStreamStart(stream):
            return None
        deadline = time.time() + timeout_seconds
        while time.time() < deadline and not history_done.is_set():
            _CF.CFRunLoopRunInMode(_RUN_LOOP_DEFAULT_MODE, ctypes.c_double(0.2), True)
        _CS.FSEventStreamFlushSync(stream)
        _CF.CFRunLoopRunInMode(_RUN_LOOP_DEFAULT_MODE, ctypes.c_double(0.3), True)
    finally:
        _CS.FSEventStreamStop(stream)
        _CS.FSEventStreamInvalidate(stream)
        _CS.FSEventStreamRelease(stream)

    if dropped.is_set() or not history_done.is_set():
        return None
    return changed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_fsevents -v`
Expected: PASS. The live replay test may report `skipped`; that is acceptable.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/fsevents.py tests/test_fsevents.py
git commit -m "feat: bind FSEvents history replay with an authoritative gap probe"
```

---

### Task 3: SQLite index

**Files:**
- Create: `src/mac_dev_clean/index.py`
- Test: `tests/test_index.py`

**Interfaces:**
- Consumes: `recommendation.Recommendation`, `recommendation.normalized_path`, `fsevents.VolumeIdentity`.
- Produces: `ScanIndex` with `open_index(path)`, `.begin_generation(volume, event_id)`, `.record_recommendation(rec)`, `.commit_batch()`, `.complete_generation()`, `.latest_complete_generation()`, `.load_recommendation(rec_id)`, `.snapshot()`, `.reset()`, `.close()`; plus `IndexSnapshot` and `SCHEMA_VERSION`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_index.py`:

```python
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from mac_dev_clean.fsevents import VolumeIdentity
from mac_dev_clean.index import SCHEMA_VERSION, open_index
from mac_dev_clean.recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
)


def build_recommendation(generation: int, path: str = "/Users/test/app/node_modules"):
    return Recommendation(
        detector_id="node-modules",
        category="project-dependencies",
        label="node_modules",
        path=Path(path),
        action=ActionKind.DELETE_TREE,
        allocated_bytes=4096,
        reclaimable_bytes=4096,
        confidence=Confidence.STRONG,
        restoration=RestorationCost.REBUILD,
        selected_by_default=True,
        evidence=(Evidence("lock-file", "pnpm-lock.yaml exists"),),
        safety_root=Path("/Users/test/app"),
        reason="Project inactive for 143 days.",
        generation=generation,
        last_activity_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class ScanIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "index.sqlite3"
        self.index = open_index(self.path)
        self.volume = VolumeIdentity(device=42, uuid="AAAA-BBBB")

    def tearDown(self):
        self.index.close()
        self.temp.cleanup()

    def test_schema_version_is_recorded(self):
        self.assertEqual(self.index.schema_version(), SCHEMA_VERSION)

    def test_incomplete_generation_is_not_returned_as_latest(self):
        generation = self.index.begin_generation(self.volume, event_id=100)
        self.index.record_recommendation(build_recommendation(generation))
        self.index.commit_batch()

        self.assertIsNone(self.index.latest_complete_generation())

    def test_completed_generation_becomes_latest(self):
        generation = self.index.begin_generation(self.volume, event_id=100)
        self.index.record_recommendation(build_recommendation(generation))
        self.index.commit_batch()
        self.index.complete_generation()

        self.assertEqual(self.index.latest_complete_generation(), generation)

    def test_recommendations_are_only_loadable_from_a_complete_generation(self):
        generation = self.index.begin_generation(self.volume, event_id=100)
        item = build_recommendation(generation)
        self.index.record_recommendation(item)
        self.index.commit_batch()

        self.assertIsNone(self.index.load_recommendation(item.id))

        self.index.complete_generation()
        loaded = self.index.load_recommendation(item.id)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.path, Path("/Users/test/app/node_modules"))
        self.assertEqual(loaded.action, ActionKind.DELETE_TREE)
        self.assertEqual(loaded.confidence, Confidence.STRONG)
        self.assertEqual(loaded.evidence[0].code, "lock-file")
        self.assertEqual(loaded.safety_root, Path("/Users/test/app"))

    def test_a_newer_generation_supersedes_the_old_one(self):
        first = self.index.begin_generation(self.volume, event_id=100)
        stale = build_recommendation(first, "/Users/test/old/node_modules")
        self.index.record_recommendation(stale)
        self.index.commit_batch()
        self.index.complete_generation()

        second = self.index.begin_generation(self.volume, event_id=200)
        self.index.record_recommendation(build_recommendation(second))
        self.index.commit_batch()
        self.index.complete_generation()

        self.assertEqual(self.index.latest_complete_generation(), second)
        self.assertIsNone(self.index.load_recommendation(stale.id))

    def test_snapshot_reports_the_last_complete_scan_baseline(self):
        generation = self.index.begin_generation(self.volume, event_id=321)
        self.index.commit_batch()
        self.index.complete_generation()

        snapshot = self.index.snapshot()

        self.assertEqual(snapshot.generation, generation)
        self.assertEqual(snapshot.event_id, 321)
        self.assertEqual(snapshot.volume_uuid, "AAAA-BBBB")
        self.assertEqual(snapshot.device, 42)
        self.assertGreater(snapshot.completed_at, 0)

    def test_snapshot_is_none_before_any_complete_scan(self):
        self.assertIsNone(self.index.snapshot())

    def test_reset_discards_everything(self):
        generation = self.index.begin_generation(self.volume, event_id=100)
        item = build_recommendation(generation)
        self.index.record_recommendation(item)
        self.index.commit_batch()
        self.index.complete_generation()

        self.index.reset()

        self.assertIsNone(self.index.snapshot())
        self.assertIsNone(self.index.load_recommendation(item.id))
        self.assertEqual(self.index.schema_version(), SCHEMA_VERSION)

    def test_a_corrupt_database_file_is_rebuilt(self):
        self.index.close()
        self.path.write_bytes(b"this is not a database")

        rebuilt = open_index(self.path)
        try:
            self.assertEqual(rebuilt.schema_version(), SCHEMA_VERSION)
            self.assertIsNone(rebuilt.snapshot())
        finally:
            rebuilt.close()

    def test_an_older_schema_is_rebuilt(self):
        generation = self.index.begin_generation(self.volume, event_id=100)
        item = build_recommendation(generation)
        self.index.record_recommendation(item)
        self.index.commit_batch()
        self.index.complete_generation()

        self.index.execute_for_test(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION - 1),)
        )
        self.index.close()

        rebuilt = open_index(self.path)
        try:
            self.assertEqual(rebuilt.schema_version(), SCHEMA_VERSION)
            # A real rebuild discards the old database file (and everything in
            # it), rather than merely patching the version number back onto
            # the still-intact old tables. Assert the old data is gone -- this
            # is the part a vacuous version-only check cannot catch.
            self.assertIsNone(rebuilt.snapshot())
            self.assertIsNone(rebuilt.load_recommendation(item.id))
        finally:
            rebuilt.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_index -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.index'`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/index.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_index -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/index.py tests/test_index.py
git commit -m "feat: add sqlite scan index with generation lifecycle and safe rebuild"
```

---

### Task 4: Repository discovery

**Files:**
- Create: `src/mac_dev_clean/discovery.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `RepositoryKind` enum, `Repository(path, kind, git_dir)` dataclass, `discover_repositories(roots, on_progress=None, should_cancel=None) -> Iterator[Repository]`, `classify_git_entry(path) -> Optional[Repository]`, and the module constants `PRUNED_DIR_NAMES` and `MAX_DEPTH`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_discovery.py`:

```python
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mac_dev_clean.discovery import (
    RepositoryKind,
    classify_git_entry,
    discover_repositories,
)


def make_primary(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return root


class ClassifyTests(unittest.TestCase):
    def test_a_git_directory_is_a_primary_repository(self):
        with TemporaryDirectory() as temp:
            repo = make_primary(Path(temp) / "app")

            found = classify_git_entry(repo)

            self.assertEqual(found.kind, RepositoryKind.PRIMARY)
            self.assertEqual(found.path, repo)
            self.assertEqual(found.git_dir, repo / ".git")

    def test_a_worktree_git_file_is_recognised(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            main = make_primary(root / "main")
            worktree = root / "wt"
            worktree.mkdir()
            (worktree / ".git").write_text(
                "gitdir: {}/.git/worktrees/wt\n".format(main)
            )

            found = classify_git_entry(worktree)

            self.assertEqual(found.kind, RepositoryKind.WORKTREE)

    def test_a_submodule_git_file_is_recognised(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_primary(root / "super")
            sub = root / "super" / "sub"
            sub.mkdir()
            (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")

            found = classify_git_entry(sub)

            self.assertEqual(found.kind, RepositoryKind.SUBMODULE)

    def test_a_submodule_under_a_directory_named_worktrees_is_a_submodule(self):
        # Regression: classification must be anchored to the segment
        # directly above the gitdir target, not a substring search over
        # the whole path. A user directory literally named "worktrees"
        # must not hijack the decision when the real marker is "modules".
        with TemporaryDirectory() as temp:
            root = Path(temp)
            outer = root / "worktrees" / "super"
            make_primary(outer)
            sub = outer / "sub"
            sub.mkdir()
            (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")

            found = classify_git_entry(sub)

            self.assertEqual(found.kind, RepositoryKind.SUBMODULE)

    def test_a_linked_worktree_of_a_submodule_is_a_worktree(self):
        # Regression: the gitdir contains both "modules" and "worktrees"
        # segments (.../.git/modules/sub/worktrees/wt). Only the segment
        # directly above the target ("worktrees") should decide the kind.
        with TemporaryDirectory() as temp:
            root = Path(temp)
            main = make_primary(root / "super")
            worktree = root / "wt-of-sub"
            worktree.mkdir()
            (worktree / ".git").write_text(
                "gitdir: {}/.git/modules/sub/worktrees/wt\n".format(main)
            )

            found = classify_git_entry(worktree)

            self.assertEqual(found.kind, RepositoryKind.WORKTREE)

    def test_a_directory_without_git_metadata_is_not_a_repository(self):
        with TemporaryDirectory() as temp:
            self.assertIsNone(classify_git_entry(Path(temp)))

    def test_an_unparseable_git_file_is_not_a_repository(self):
        with TemporaryDirectory() as temp:
            root = Path(temp) / "weird"
            root.mkdir()
            (root / ".git").write_text("this is not a gitdir pointer\n")

            self.assertIsNone(classify_git_entry(root))


class DiscoveryTests(unittest.TestCase):
    def test_repositories_are_found_at_any_depth_without_a_developer_root(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            shallow = make_primary(root / "app")
            deep = make_primary(root / "a" / "b" / "c" / "deep")

            found = {item.path for item in discover_repositories([root])}

            self.assertIn(shallow, found)
            self.assertIn(deep, found)

    def test_nested_repositories_are_both_reported(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            outer = make_primary(root / "outer")
            inner = make_primary(root / "outer" / "packages" / "inner")

            found = {item.path for item in discover_repositories([root])}

            self.assertEqual(found, {outer, inner})

    def test_dependency_and_build_directories_are_never_descended(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_primary(root / "app")
            buried = make_primary(root / "app" / "node_modules" / "pkg")

            found = {item.path for item in discover_repositories([root])}

            self.assertNotIn(buried, found)

    def test_git_internals_are_never_descended(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            repo = make_primary(root / "app")
            buried = repo / ".git" / "modules" / "x"
            buried.mkdir(parents=True)
            (buried / ".git").mkdir()

            found = {item.path for item in discover_repositories([root])}

            self.assertEqual(found, {repo})

    def test_symlinked_directories_are_not_followed(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            real = make_primary(root / "real")
            link = root / "link"
            os.symlink(str(root / "real"), str(link))

            found = {item.path for item in discover_repositories([root])}

            self.assertEqual(found, {real})

    def test_the_same_directory_reached_twice_is_reported_once(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            repo = make_primary(root / "app")

            found = list(discover_repositories([root, root, root / "app"]))

            self.assertEqual([item.path for item in found], [repo])

    def test_cancellation_stops_the_walk(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(20):
                make_primary(root / "repo{}".format(index))

            found = list(discover_repositories([root], should_cancel=lambda: True))

            self.assertEqual(found, [])

    def test_unreadable_directories_are_skipped_without_failing(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            good = make_primary(root / "good")
            locked = root / "locked"
            locked.mkdir()
            os.chmod(str(locked), 0o000)
            try:
                found = {item.path for item in discover_repositories([root])}
            finally:
                os.chmod(str(locked), 0o755)

            self.assertEqual(found, {good})

    def test_progress_reports_visited_directories(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_primary(root / "app")
            seen = []

            list(discover_repositories([root], on_progress=seen.append))

            self.assertTrue(any(str(root) in path for path in seen))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_discovery -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.discovery'`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/discovery.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence, Set, Tuple

MAX_DEPTH = 12

# Directories that never contain a project we should analyse and that are
# expensive or unsafe to walk. Matching is by exact directory name.
PRUNED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".Trash",
        "node_modules",
        "Pods",
        "DerivedData",
        ".build",
        ".gradle",
        ".cxx",
        "build",
        "target",
        "vendor",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        ".nuxt",
        "Library",
        "Applications",
        ".Spotlight-V100",
        ".fseventsd",
        ".DocumentRevisions-V100",
    }
)

_CLOUD_PLACEHOLDER_SUFFIXES = (".icloud",)


class RepositoryKind(Enum):
    PRIMARY = "primary"
    WORKTREE = "worktree"
    SUBMODULE = "submodule"


@dataclass(frozen=True)
class Repository:
    path: Path
    kind: RepositoryKind
    git_dir: Path


def classify_git_entry(path: Path) -> Optional[Repository]:
    """Classify a candidate directory by inspecting its `.git` entry only.

    Git records enough in the pointer file to tell a linked worktree from a
    submodule, so this needs no subprocess: a worktree points into
    `.git/worktrees/<name>` and a submodule into `.git/modules/<name>`.
    """
    marker = path / ".git"
    try:
        if marker.is_dir() and not marker.is_symlink():
            return Repository(path=path, kind=RepositoryKind.PRIMARY, git_dir=marker)
        if not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None

    if not text.startswith("gitdir:"):
        return None
    pointer = text[len("gitdir:") :].strip()
    if not pointer:
        return None

    git_dir = Path(pointer)
    if not git_dir.is_absolute():
        git_dir = (path / git_dir)
    # Real git layouts always place the marker directly above the entry's
    # own directory name: `<gitdir>/worktrees/<name>` or
    # `<gitdir>/modules/<name>`. Anchor classification to that position
    # (the second-to-last path segment) rather than searching the whole
    # path for "worktrees" or "modules" anywhere: a user directory that
    # happens to be named `worktrees` or `modules` must not hijack the
    # decision. Normalize first because the pointer may be relative
    # (e.g. `../.git/modules/sub`) and contain `..` segments that would
    # otherwise land in the wrong position.
    parts = Path(os.path.normpath(str(git_dir))).parts
    if len(parts) < 2:
        return None
    marker_segment = parts[-2]
    if marker_segment == "worktrees":
        kind = RepositoryKind.WORKTREE
    elif marker_segment == "modules":
        kind = RepositoryKind.SUBMODULE
    else:
        return None
    return Repository(path=path, kind=kind, git_dir=git_dir)


def _is_skippable(name: str) -> bool:
    if name in PRUNED_DIR_NAMES:
        return True
    return name.endswith(_CLOUD_PLACEHOLDER_SUFFIXES)


def discover_repositories(
    roots: Sequence[Path],
    on_progress: Optional[Callable[[str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_depth_limit: Optional[Callable[[Path], None]] = None,
    on_unreadable: Optional[Callable[[Path], None]] = None,
) -> Iterator[Repository]:
    """Yield every Git repository beneath `roots`.

    There is no developer-root heuristic: a root is only a traversal boundary.
    Symlinks are never followed, and each (device, inode) pair is visited once
    so overlapping roots and hard-linked trees cannot be double-counted.
    """
    visited: Set[Tuple[int, int]] = set()
    stack = []
    for root in roots:
        expanded = Path(root).expanduser()
        stack.append((expanded, 0))

    while stack:
        # Cancellation granularity is one directory: this check fires once
        # per popped directory, before its `scandir`. If that directory is
        # a single huge flat directory (a Mail store, a Photos library
        # package, etc.) that is not in PRUNED_DIR_NAMES, the scandir below
        # still runs to completion before the next check — worst-case
        # cancellation latency is one full scandir of the largest directory
        # encountered. Behaviour stays bounded, never hung.
        if should_cancel is not None and should_cancel():
            return
        current, depth = stack.pop()
        if depth > MAX_DEPTH:
            # Report rather than truncate silently: a repository below this
            # depth is simply absent from the results, which is indistinguishable
            # from "nothing to clean here" for anyone reading the UI.
            if on_depth_limit is not None:
                on_depth_limit(current)
            continue
        try:
            stat_result = os.stat(str(current), follow_symlinks=False)
        except OSError:
            continue
        if not os.path.isdir(str(current)) or os.path.islink(str(current)):
            continue
        key = (stat_result.st_dev, stat_result.st_ino)
        if key in visited:
            continue
        visited.add(key)

        if on_progress is not None:
            on_progress(str(current))

        repository = classify_git_entry(current)
        if repository is not None:
            yield repository

        try:
            with os.scandir(str(current)) as entries:
                children = list(entries)
        except PermissionError:
            # Same class of silence as the depth limit: an unreadable directory
            # simply contributes nothing, which reads as "clean" rather than
            # "never looked". Tell the caller which path was skipped.
            if on_unreadable is not None:
                on_unreadable(current)
            continue
        except OSError:
            continue

        for entry in children:
            if _is_skippable(entry.name):
                continue
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            stack.append((Path(entry.path), depth + 1))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_discovery -v`
Expected: PASS, 14 tests.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/discovery.py tests/test_discovery.py
git commit -m "feat: discover git repositories, worktrees, and submodules by traversal"
```

---

### Task 5: Meaningful activity and artifact recipes

**Files:**
- Create: `src/mac_dev_clean/projects.py`
- Test: `tests/test_projects.py`

**Interfaces:**
- Consumes: `discovery.Repository`, `discovery.RepositoryKind`, `scanner.path_size`.
- Produces: `INACTIVITY_DAYS = 90`, `RECIPES: Tuple[Recipe, ...]`, `Recipe(detector_id, label, relative_path, kind, manifests, locks, category, restoration, note)`, `ArtifactKind` enum, `last_meaningful_activity(repo_path, now) -> Optional[datetime]`, `ArtifactFact` dataclass, `analyze_repository(repo, now) -> List[ArtifactFact]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_projects.py`:

```python
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from mac_dev_clean.discovery import Repository, RepositoryKind
from mac_dev_clean.projects import (
    INACTIVITY_DAYS,
    ArtifactKind,
    analyze_repository,
    last_meaningful_activity,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=400)
RECENT = NOW - timedelta(days=2)
BIG = b"x" * (2 * 1024 * 1024)


def touch(path: Path, when: datetime, payload: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stamp = when.timestamp()
    os.utime(str(path), (stamp, stamp))


def primary(root: Path) -> Repository:
    git_dir = root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    touch(git_dir / "HEAD", RECENT)
    return Repository(path=root, kind=RepositoryKind.PRIMARY, git_dir=git_dir)


class MeaningfulActivityTests(unittest.TestCase):
    def test_source_changes_set_the_activity_timestamp(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            touch(root / "package.json", OLD)

            activity = last_meaningful_activity(root, now=NOW)

            self.assertIsNotNone(activity)
            self.assertLess(activity, NOW - timedelta(days=INACTIVITY_DAYS))

    def test_git_internals_do_not_count_as_activity(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            repo = primary(root)
            touch(repo.git_dir / "index", RECENT)
            touch(root / "src" / "main.ts", OLD)

            activity = last_meaningful_activity(root, now=NOW)

            self.assertLess(activity, NOW - timedelta(days=INACTIVITY_DAYS))

    def test_generated_directories_do_not_count_as_activity(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            touch(root / "node_modules" / "pkg" / "index.js", RECENT)
            touch(root / "ios" / "build" / "app.o", RECENT)
            touch(root / ".DS_Store", RECENT)

            activity = last_meaningful_activity(root, now=NOW)

            self.assertLess(activity, NOW - timedelta(days=INACTIVITY_DAYS))

    def test_a_recent_source_edit_keeps_the_project_active(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            touch(root / "src" / "util.ts", RECENT)

            activity = last_meaningful_activity(root, now=NOW)

            self.assertGreater(activity, NOW - timedelta(days=INACTIVITY_DAYS))

    def test_a_lock_file_edit_counts_as_activity(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            touch(root / "pnpm-lock.yaml", RECENT)

            activity = last_meaningful_activity(root, now=NOW)

            self.assertGreater(activity, NOW - timedelta(days=INACTIVITY_DAYS))

    def test_a_deeply_nested_source_edit_keeps_the_project_active(self):
        # Java/Kotlin package layouts under android/src/main/java/com/... are
        # routine in React Native / Android projects and commonly exceed 8
        # directory levels. A recent edit that far down must still count.
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            deep_file = (
                root
                / "android"
                / "src"
                / "main"
                / "java"
                / "com"
                / "example"
                / "app"
                / "feature"
                / "module"
                / "detail"
                / "view"
                / "widget"
                / "Widget.kt"
            )
            self.assertGreaterEqual(
                len(deep_file.relative_to(root).parts) - 1,
                12,
                "fixture must place the file at least 12 levels below the repo root",
            )
            touch(deep_file, RECENT)

            activity = last_meaningful_activity(root, now=NOW)

            self.assertGreater(activity, NOW - timedelta(days=INACTIVITY_DAYS))

            repo = Repository(path=root, kind=RepositoryKind.PRIMARY, git_dir=root / ".git")
            facts = analyze_repository(repo, now=NOW)
            for fact in facts:
                self.assertFalse(
                    fact.inactive,
                    "a project with a recent deeply-nested edit must not be inactive",
                )


class NodeModulesRecipeTests(unittest.TestCase):
    def build(self, root: Path, lock_name: str = "pnpm-lock.yaml", age=OLD) -> Path:
        primary(root)
        touch(root / "src" / "main.ts", age)
        touch(root / "package.json", age, b"{}")
        if lock_name:
            touch(root / lock_name, age)
        touch(root / "node_modules" / "pkg" / "index.js", age, BIG)
        return root / "node_modules"

    def test_node_modules_with_a_lock_file_is_found(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            expected = self.build(root)

            facts = analyze_repository(primary(root), now=NOW)

            node = [item for item in facts if item.recipe.detector_id == "node-modules"]
            self.assertEqual(len(node), 1)
            self.assertEqual(node[0].path, expected)
            self.assertTrue(node[0].lock_satisfied)
            self.assertGreater(node[0].allocated_bytes, 1024)

    def test_node_modules_without_a_lock_file_is_reported_but_not_lock_satisfied(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.build(root, lock_name="")

            facts = analyze_repository(primary(root), now=NOW)

            node = [item for item in facts if item.recipe.detector_id == "node-modules"]
            self.assertEqual(len(node), 1)
            self.assertFalse(node[0].lock_satisfied)

    def test_node_modules_without_a_package_manifest_is_not_a_candidate(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "node_modules" / "pkg" / "index.js", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)

            self.assertEqual(
                [item for item in facts if item.recipe.detector_id == "node-modules"], []
            )

    def test_every_supported_javascript_lock_file_satisfies_the_recipe(self):
        for lock_name in (
            "package-lock.json",
            "npm-shrinkwrap.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "bun.lock",
            "bun.lockb",
        ):
            with self.subTest(lock=lock_name), TemporaryDirectory() as temp:
                root = Path(temp)
                self.build(root, lock_name=lock_name)

                facts = analyze_repository(primary(root), now=NOW)
                node = [item for item in facts if item.recipe.detector_id == "node-modules"]

                self.assertTrue(node[0].lock_satisfied, lock_name)


class NativeRecipeTests(unittest.TestCase):
    def test_ios_pods_requires_a_podfile_lock(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "ios" / "Podfile", OLD)
            touch(root / "ios" / "Pods" / "lib.a", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)
            pods = [item for item in facts if item.recipe.detector_id == "ios-pods"]

            self.assertEqual(len(pods), 1)
            self.assertFalse(pods[0].lock_satisfied)

            touch(root / "ios" / "Podfile.lock", OLD)
            facts = analyze_repository(primary(root), now=NOW)
            pods = [item for item in facts if item.recipe.detector_id == "ios-pods"]

            self.assertTrue(pods[0].lock_satisfied)

    def test_ios_build_requires_an_xcode_project_or_workspace(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "ios" / "build" / "app.o", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)
            self.assertEqual(
                [item for item in facts if item.recipe.detector_id == "ios-build"], []
            )

            (root / "ios" / "App.xcodeproj").mkdir(parents=True)
            facts = analyze_repository(primary(root), now=NOW)
            builds = [item for item in facts if item.recipe.detector_id == "ios-build"]

            self.assertEqual(len(builds), 1)
            self.assertEqual(builds[0].kind, ArtifactKind.BUILD_OUTPUT)

    def test_android_build_requires_gradle_settings_and_an_app_module(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "android" / "app" / "build" / "out.dex", OLD, BIG)
            touch(root / "android" / "app" / ".cxx" / "obj.o", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)
            self.assertEqual(
                [item for item in facts if item.recipe.detector_id.startswith("android")], []
            )

            touch(root / "android" / "settings.gradle", OLD)
            touch(root / "android" / "app" / "build.gradle", OLD)
            facts = analyze_repository(primary(root), now=NOW)
            ids = {item.recipe.detector_id for item in facts}

            self.assertIn("android-app-build", ids)
            self.assertIn("android-app-cxx", ids)

    def test_rust_target_requires_cargo_manifest_and_lock(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "Cargo.toml", OLD)
            touch(root / "target" / "debug" / "bin", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)
            rust = [item for item in facts if item.recipe.detector_id == "rust-target"]
            self.assertFalse(rust[0].lock_satisfied)

            touch(root / "Cargo.lock", OLD)
            facts = analyze_repository(primary(root), now=NOW)
            rust = [item for item in facts if item.recipe.detector_id == "rust-target"]
            self.assertTrue(rust[0].lock_satisfied)

    def test_python_venv_needs_a_deterministic_lock_file(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "pyproject.toml", OLD)
            touch(root / "requirements.txt", OLD)
            touch(root / ".venv" / "lib" / "site.py", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)
            venv = [item for item in facts if item.recipe.detector_id == "python-venv"]

            self.assertEqual(len(venv), 1)
            self.assertFalse(
                venv[0].lock_satisfied,
                "requirements.txt alone must not qualify a venv for default selection",
            )

            touch(root / "uv.lock", OLD)
            facts = analyze_repository(primary(root), now=NOW)
            venv = [item for item in facts if item.recipe.detector_id == "python-venv"]

            self.assertTrue(venv[0].lock_satisfied)


class SafetyTests(unittest.TestCase):
    def test_a_symlinked_artifact_is_never_a_candidate(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "package.json", OLD, b"{}")
            touch(root / "pnpm-lock.yaml", OLD)
            elsewhere = root / "shared_modules"
            elsewhere.mkdir()
            touch(elsewhere / "pkg" / "index.js", OLD, BIG)
            os.symlink(str(elsewhere), str(root / "node_modules"))

            facts = analyze_repository(primary(root), now=NOW)

            self.assertEqual(
                [item for item in facts if item.recipe.detector_id == "node-modules"], []
            )

    def test_a_worktree_is_analysed_but_never_yields_its_own_root(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            main = root / "main"
            primary(main)
            worktree = root / "wt"
            worktree.mkdir()
            (worktree / ".git").write_text("gitdir: {}/.git/worktrees/wt\n".format(main))
            touch(worktree / "package.json", OLD, b"{}")
            touch(worktree / "pnpm-lock.yaml", OLD)
            touch(worktree / "node_modules" / "pkg" / "index.js", OLD, BIG)

            repo = Repository(
                path=worktree,
                kind=RepositoryKind.WORKTREE,
                git_dir=main / ".git" / "worktrees" / "wt",
            )
            facts = analyze_repository(repo, now=NOW)

            self.assertTrue(facts)
            for fact in facts:
                self.assertNotEqual(fact.path, worktree)
                self.assertTrue(str(fact.path).startswith(str(worktree) + os.sep))

    def test_tiny_artifacts_are_ignored(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "package.json", OLD, b"{}")
            touch(root / "pnpm-lock.yaml", OLD)
            touch(root / "node_modules" / "pkg" / "index.js", OLD, b"tiny")

            facts = analyze_repository(primary(root), now=NOW)

            self.assertEqual(facts, [])

    def test_an_ancestor_symlink_never_yields_a_candidate_outside_the_repo(self):
        # The `android` directory itself is a symlink pointing at storage
        # outside the repository. `android/app/build` is not itself a
        # symlink, so a leaf-only `is_symlink()` check would miss this: the
        # artifact resolves outside the project and must never be a
        # candidate.
        with TemporaryDirectory() as outside_temp, TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)

            outside = Path(outside_temp) / "SHARED_STORAGE_OUTSIDE_PROJECT"
            touch(outside / "settings.gradle", OLD)
            touch(outside / "app" / "build.gradle", OLD)
            touch(outside / "app" / "build" / "important.bin", OLD, BIG)

            os.symlink(str(outside), str(root / "android"))

            facts = analyze_repository(primary(root), now=NOW)

            self.assertEqual(
                [item for item in facts if item.recipe.detector_id.startswith("android")],
                [],
            )

    def test_a_real_deep_path_inside_the_repo_is_still_a_candidate(self):
        # Proves the ancestor-symlink guard does not simply disable the
        # recipe: a genuine, non-symlinked android/app/build inside the
        # repository must still be found.
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            touch(root / "android" / "settings.gradle", OLD)
            touch(root / "android" / "app" / "build.gradle", OLD)
            touch(root / "android" / "app" / "build" / "important.bin", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)
            ids = {item.recipe.detector_id for item in facts}

            self.assertIn("android-app-build", ids)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_projects -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.projects'`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/projects.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple

from .discovery import Repository
from .recommendation import RestorationCost
from .scanner import path_size

INACTIVITY_DAYS = 90
MIN_ARTIFACT_BYTES = 1024 * 1024

# Directory names that never represent human work on the project. Excluded from
# the meaningful-activity calculation only; a directory is not treated as an
# artifact just because it appears here.
GENERATED_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "Pods",
        "build",
        ".build",
        "target",
        ".gradle",
        ".cxx",
        "DerivedData",
        "vendor",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        ".nuxt",
        "dist",
        "coverage",
        "tmp",
        ".idea",
        ".vscode",
    }
)

GENERATED_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db"})
GENERATED_FILE_SUFFIXES = (".log", ".pyc", ".swp", ".tmp", ".orig")


class ArtifactKind(Enum):
    DEPENDENCY_TREE = "dependency_tree"
    BUILD_OUTPUT = "build_output"


@dataclass(frozen=True)
class Recipe:
    detector_id: str
    label: str
    relative_path: str
    kind: ArtifactKind
    #: At least one of these must exist for the artifact to be a candidate.
    manifests: Tuple[str, ...]
    #: At least one must exist before default selection is allowed. Empty means
    #: the recipe's manifest is itself sufficient (build outputs).
    locks: Tuple[str, ...]
    #: Extra directory that must exist, e.g. an .xcodeproj sibling.
    marker_globs: Tuple[str, ...]
    category: str
    restoration: RestorationCost
    note: str


RECIPES: Tuple[Recipe, ...] = (
    Recipe(
        detector_id="node-modules",
        label="node_modules",
        relative_path="node_modules",
        kind=ArtifactKind.DEPENDENCY_TREE,
        manifests=("package.json",),
        locks=(
            "package-lock.json",
            "npm-shrinkwrap.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "bun.lock",
            "bun.lockb",
        ),
        marker_globs=(),
        category="project-dependencies",
        restoration=RestorationCost.REDOWNLOAD,
        note="JavaScript dependencies. Reinstall with npm, yarn, pnpm, or bun.",
    ),
    Recipe(
        detector_id="ios-pods",
        label="CocoaPods Pods",
        relative_path="ios/Pods",
        kind=ArtifactKind.DEPENDENCY_TREE,
        manifests=("ios/Podfile",),
        locks=("ios/Podfile.lock",),
        marker_globs=(),
        category="project-dependencies",
        restoration=RestorationCost.REDOWNLOAD,
        note="CocoaPods dependencies. Restore with pod install.",
    ),
    Recipe(
        detector_id="ios-build",
        label="iOS build output",
        relative_path="ios/build",
        kind=ArtifactKind.BUILD_OUTPUT,
        manifests=(),
        locks=(),
        marker_globs=("ios/*.xcodeproj", "ios/*.xcworkspace"),
        category="project-build-output",
        restoration=RestorationCost.REBUILD,
        note="Generated Xcode build products. Xcode recreates them on the next build.",
    ),
    Recipe(
        detector_id="android-app-build",
        label="Android app build output",
        relative_path="android/app/build",
        kind=ArtifactKind.BUILD_OUTPUT,
        manifests=("android/app/build.gradle", "android/app/build.gradle.kts"),
        locks=(),
        marker_globs=("android/settings.gradle", "android/settings.gradle.kts"),
        category="project-build-output",
        restoration=RestorationCost.REBUILD,
        note="Generated Gradle build products. Gradle recreates them on the next build.",
    ),
    Recipe(
        detector_id="android-app-cxx",
        label="Android native build output",
        relative_path="android/app/.cxx",
        kind=ArtifactKind.BUILD_OUTPUT,
        manifests=("android/app/build.gradle", "android/app/build.gradle.kts"),
        locks=(),
        marker_globs=("android/settings.gradle", "android/settings.gradle.kts"),
        category="project-build-output",
        restoration=RestorationCost.REBUILD,
        note="Generated native (CMake/NDK) build products.",
    ),
    Recipe(
        detector_id="swiftpm-build",
        label="SwiftPM build output",
        relative_path=".build",
        kind=ArtifactKind.BUILD_OUTPUT,
        manifests=("Package.swift",),
        locks=("Package.resolved",),
        marker_globs=(),
        category="project-build-output",
        restoration=RestorationCost.REBUILD,
        note="Generated Swift Package Manager build products.",
    ),
    Recipe(
        detector_id="rust-target",
        label="Rust target directory",
        relative_path="target",
        kind=ArtifactKind.BUILD_OUTPUT,
        manifests=("Cargo.toml",),
        locks=("Cargo.lock",),
        marker_globs=(),
        category="project-build-output",
        restoration=RestorationCost.REBUILD,
        note="Generated Cargo build products. Rebuild with cargo build.",
    ),
    Recipe(
        detector_id="ruby-bundle",
        label="Bundler vendor/bundle",
        relative_path="vendor/bundle",
        kind=ArtifactKind.DEPENDENCY_TREE,
        manifests=("Gemfile",),
        locks=("Gemfile.lock",),
        marker_globs=(),
        category="project-dependencies",
        restoration=RestorationCost.REDOWNLOAD,
        note="Bundler gems. Restore with bundle install.",
    ),
    Recipe(
        detector_id="python-venv",
        label="Python virtual environment",
        relative_path=".venv",
        kind=ArtifactKind.DEPENDENCY_TREE,
        manifests=("pyproject.toml", "Pipfile"),
        # requirements.txt is deliberately absent: it need not describe an exact
        # reproducible environment, so it must never enable default selection.
        locks=("uv.lock", "poetry.lock", "Pipfile.lock"),
        marker_globs=(),
        category="project-dependencies",
        restoration=RestorationCost.REDOWNLOAD,
        note="Python virtual environment. Recreate from the project's lock file.",
    ),
    Recipe(
        detector_id="python-venv-legacy",
        label="Python virtual environment",
        relative_path="venv",
        kind=ArtifactKind.DEPENDENCY_TREE,
        manifests=("pyproject.toml", "Pipfile"),
        locks=("uv.lock", "poetry.lock", "Pipfile.lock"),
        marker_globs=(),
        category="project-dependencies",
        restoration=RestorationCost.REDOWNLOAD,
        note="Python virtual environment. Recreate from the project's lock file.",
    ),
)


@dataclass(frozen=True)
class ArtifactFact:
    repository: Repository
    recipe: Recipe
    path: Path
    kind: ArtifactKind
    allocated_bytes: int
    lock_satisfied: bool
    lock_detail: str
    manifest_detail: str
    last_activity_at: Optional[datetime]
    inactive: bool


def _is_generated_file(name: str) -> bool:
    if name in GENERATED_FILE_NAMES:
        return True
    return name.endswith(GENERATED_FILE_SUFFIXES)


def last_meaningful_activity(
    repo_path: Path, now: Optional[datetime] = None
) -> Optional[datetime]:
    """Newest mtime among files that represent human work on the project.

    Generated directories, Git internals, editor state, and log files are
    excluded, so a nightly CI build or a stray `.DS_Store` cannot make a
    long-abandoned project look active.
    """
    newest = 0.0
    stack = [repo_path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(str(current)) as entries:
                children = list(entries)
        except OSError:
            continue
        for entry in children:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in GENERATED_DIR_NAMES:
                        stack.append(Path(entry.path))
                    continue
                if _is_generated_file(entry.name):
                    continue
                mtime = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if mtime > newest:
                newest = mtime

    if newest <= 0:
        return None
    return datetime.fromtimestamp(newest, timezone.utc)


def _first_existing(root: Path, names: Tuple[str, ...]) -> Optional[str]:
    for name in names:
        if (root / name).exists():
            return name
    return None


def _first_matching_glob(root: Path, patterns: Tuple[str, ...]) -> Optional[str]:
    for pattern in patterns:
        for match in root.glob(pattern):
            # A marker only opens the gate for a recipe, so a symlinked one is
            # evidence about somewhere else's project, not this one. Refuse it
            # rather than letting an outside link vouch for a deletion here.
            if match.exists() and not match.is_symlink():
                return pattern
    return None


def analyze_repository(
    repository: Repository, now: Optional[datetime] = None
) -> List[ArtifactFact]:
    """Return every artifact candidate inside one repository.

    The repository root itself is never a candidate: only registered recipe
    subpaths are, and each must be a real directory rather than a symlink.
    """
    moment = now or datetime.now(timezone.utc)
    root = repository.path
    activity = last_meaningful_activity(root, now=moment)
    inactive = activity is not None and activity < moment - timedelta(days=INACTIVITY_DAYS)

    resolved_root = os.path.realpath(str(root))

    facts: List[ArtifactFact] = []
    for recipe in RECIPES:
        artifact = root / recipe.relative_path
        if artifact == root:
            continue
        try:
            if artifact.is_symlink() or not artifact.is_dir():
                continue
        except OSError:
            continue

        # Guard against an ancestor path segment (not the leaf) being a
        # symlink that routes the artifact outside the repository. The leaf
        # check above only inspects the final component, so e.g. `android`
        # being a symlink while `android/app/build` "looks" like a plain
        # directory would otherwise slip through. Resolve fully and require
        # containment by path segments, never a bare string prefix test.
        resolved_artifact = os.path.realpath(str(artifact))
        if resolved_artifact != resolved_root and not resolved_artifact.startswith(
            resolved_root + os.sep
        ):
            continue

        manifest = None
        if recipe.manifests:
            manifest = _first_existing(root, recipe.manifests)
            if manifest is None:
                continue
        marker = None
        if recipe.marker_globs:
            marker = _first_matching_glob(root, recipe.marker_globs)
            if marker is None:
                continue

        lock = _first_existing(root, recipe.locks) if recipe.locks else None
        lock_satisfied = bool(lock) if recipe.locks else True

        size = path_size(artifact)
        if size < MIN_ARTIFACT_BYTES:
            continue

        facts.append(
            ArtifactFact(
                repository=repository,
                recipe=recipe,
                path=artifact,
                kind=recipe.kind,
                allocated_bytes=size,
                lock_satisfied=lock_satisfied,
                lock_detail="{} exists".format(lock) if lock else "no supported lock file",
                manifest_detail="{} exists".format(manifest or marker or recipe.relative_path),
                last_activity_at=activity,
                inactive=inactive,
            )
        )
    return facts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_projects -v`
Expected: PASS, 17 tests.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/projects.py tests/test_projects.py
git commit -m "feat: add meaningful-activity analysis and validated artifact recipes"
```

---

### Task 6: Policy engine

**Files:**
- Create: `src/mac_dev_clean/policy.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: `projects.ArtifactFact`, `projects.ArtifactKind`, `projects.INACTIVITY_DAYS`, `recommendation.*`.
- Produces: `recommend(fact, generation, now) -> Recommendation`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_policy.py`:

```python
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mac_dev_clean.discovery import Repository, RepositoryKind
from mac_dev_clean.policy import recommend
from mac_dev_clean.projects import RECIPES, ArtifactFact
from mac_dev_clean.recommendation import ActionKind, Confidence, RestorationCost

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
RECIPE_BY_ID = {recipe.detector_id: recipe for recipe in RECIPES}


def fact(detector_id="node-modules", **overrides):
    recipe = RECIPE_BY_ID[detector_id]
    repository = Repository(
        path=Path("/Users/test/app"),
        kind=RepositoryKind.PRIMARY,
        git_dir=Path("/Users/test/app/.git"),
    )
    defaults = dict(
        repository=repository,
        recipe=recipe,
        path=Path("/Users/test/app") / recipe.relative_path,
        kind=recipe.kind,
        allocated_bytes=5 * 1024 * 1024,
        lock_satisfied=True,
        lock_detail="pnpm-lock.yaml exists",
        manifest_detail="package.json exists",
        last_activity_at=NOW - timedelta(days=143),
        inactive=True,
    )
    defaults.update(overrides)
    return ArtifactFact(**defaults)


class PolicyTests(unittest.TestCase):
    def test_inactive_project_with_a_lock_file_is_selected(self):
        item = recommend(fact(), generation=1, now=NOW)

        self.assertTrue(item.selected_by_default)
        self.assertEqual(item.action, ActionKind.DELETE_TREE)
        self.assertEqual(item.confidence, Confidence.STRONG)
        self.assertEqual(item.restoration, RestorationCost.REDOWNLOAD)
        self.assertIn("143 days", item.reason)

    def test_active_project_is_reported_but_not_selected(self):
        item = recommend(
            fact(inactive=False, last_activity_at=NOW - timedelta(days=3)),
            generation=1,
            now=NOW,
        )

        self.assertFalse(item.selected_by_default)
        self.assertIn("changed 3 days ago", item.reason)

    def test_dependency_tree_without_a_lock_file_is_never_selected(self):
        item = recommend(fact(lock_satisfied=False, lock_detail="no supported lock file"), generation=1, now=NOW)

        self.assertFalse(item.selected_by_default)
        self.assertEqual(item.confidence, Confidence.HEURISTIC)
        self.assertIn("lock file", item.reason)

    def test_unknown_activity_is_never_selected(self):
        item = recommend(fact(last_activity_at=None, inactive=False), generation=1, now=NOW)

        self.assertFalse(item.selected_by_default)
        self.assertEqual(item.confidence, Confidence.UNKNOWN)

    def test_inactive_build_output_is_selected_without_a_lock_file(self):
        item = recommend(
            fact("ios-build", lock_satisfied=True, lock_detail="", manifest_detail="ios/*.xcodeproj exists"),
            generation=1,
            now=NOW,
        )

        self.assertTrue(item.selected_by_default)
        self.assertEqual(item.restoration, RestorationCost.REBUILD)

    def test_every_recommendation_carries_evidence_and_a_safety_root(self):
        item = recommend(fact(), generation=9, now=NOW)

        codes = {entry.code for entry in item.evidence}
        self.assertIn("manifest", codes)
        self.assertIn("inactivity", codes)
        self.assertEqual(item.safety_root, Path("/Users/test/app"))
        self.assertEqual(item.generation, 9)

    def test_recommendations_warn_about_open_build_tools(self):
        item = recommend(fact(), generation=1, now=NOW)

        self.assertIn("Close", item.warning)

    def test_policy_is_deterministic(self):
        first = recommend(fact(), generation=1, now=NOW).to_dict()
        second = recommend(fact(), generation=1, now=NOW).to_dict()

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_policy -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.policy'`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/policy.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_policy -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/policy.py tests/test_policy.py
git commit -m "feat: add deterministic policy engine for project artifacts"
```

---

### Task 7: NDJSON streaming events

**Files:**
- Create: `src/mac_dev_clean/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: `recommendation.Recommendation`.
- Produces: `PROTOCOL_VERSION = 1`, `EventEmitter(stream, generation)` with `.scan_started`, `.root_started`, `.progress`, `.candidate_found`, `.root_finished`, `.permission_required`, `.warning`, `.scan_completed`, `.scan_cancelled`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_events.py`:

```python
import io
import json
import unittest
from pathlib import Path

from mac_dev_clean.events import PROTOCOL_VERSION, EventEmitter
from mac_dev_clean.recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
)


def build_recommendation():
    return Recommendation(
        detector_id="node-modules",
        category="project-dependencies",
        label="node_modules",
        path=Path("/Users/test/app/node_modules"),
        action=ActionKind.DELETE_TREE,
        allocated_bytes=2048,
        reclaimable_bytes=2048,
        confidence=Confidence.STRONG,
        restoration=RestorationCost.REDOWNLOAD,
        selected_by_default=True,
        evidence=(Evidence("lock-file", "pnpm-lock.yaml exists"),),
        safety_root=Path("/Users/test/app"),
        reason="Project has not changed in 143 days.",
        generation=3,
    )


class EventEmitterTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.emitter = EventEmitter(self.stream, generation=3)

    def lines(self):
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line]

    def test_every_event_is_one_complete_json_line(self):
        self.emitter.scan_started(roots=[Path("/Users/test/home")], incremental=False)
        self.emitter.progress(path="/Users/test/app", scanned=1)
        self.emitter.scan_completed(reclaimable_bytes=2048, count=1)

        raw = self.stream.getvalue()
        self.assertEqual(len(raw.splitlines()), 3)
        for event in self.lines():
            self.assertEqual(event["protocol_version"], PROTOCOL_VERSION)
            self.assertEqual(event["generation"], 3)
            self.assertIn("event", event)

    def test_scan_started_reports_roots_and_mode(self):
        self.emitter.scan_started(roots=[Path("/Users/test/home")], incremental=True)

        event = self.lines()[0]
        self.assertEqual(event["event"], "scan_started")
        self.assertEqual(event["roots"], ["/Users/test/home"])
        self.assertTrue(event["incremental"])

    def test_candidate_found_embeds_the_recommendation_payload(self):
        self.emitter.candidate_found(build_recommendation())

        event = self.lines()[0]
        self.assertEqual(event["event"], "candidate_found")
        self.assertEqual(event["recommendation"]["detector_id"], "node-modules")
        self.assertTrue(event["recommendation"]["selected_by_default"])

    def test_permission_required_names_the_blocked_root(self):
        self.emitter.permission_required(Path("/Users/test/Documents"), "Documents")

        event = self.lines()[0]
        self.assertEqual(event["event"], "permission_required")
        self.assertEqual(event["path"], "/Users/test/Documents")
        self.assertEqual(event["folder"], "Documents")

    def test_cancellation_is_a_distinct_terminal_event(self):
        self.emitter.scan_cancelled(reclaimable_bytes=0, count=0)

        event = self.lines()[0]
        self.assertEqual(event["event"], "scan_cancelled")

    def test_events_are_flushed_immediately(self):
        flushes = []

        class RecordingStream(io.StringIO):
            def flush(self):
                flushes.append(True)
                super().flush()

        EventEmitter(RecordingStream(), generation=1).progress(path="/Users/test/home", scanned=1)

        self.assertTrue(flushes)

    def test_non_utf8_paths_do_not_break_the_stream(self):
        self.emitter.progress(path="/Users/test/caf\udce9", scanned=1)

        self.assertEqual(len(self.lines()), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_events -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.events'`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/events.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_events -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/events.py tests/test_events.py
git commit -m "feat: add ndjson streaming event protocol for deep scan"
```

---

### Task 8: Deep scan orchestrator

**Files:**
- Create: `src/mac_dev_clean/deep_scan.py`
- Test: `tests/test_deep_scan.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces: `DeepScanResult(recommendations, cancelled, incremental, reclaimable_bytes)`, `deep_scan(roots, index, emitter=None, now=None, should_cancel=None, use_fsevents=True) -> DeepScanResult`, `default_deep_scan_roots(home) -> List[Path]`, `PROTECTED_FOLDER_NAMES`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_deep_scan.py`:

```python
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mac_dev_clean import deep_scan as deep_scan_module
from mac_dev_clean.deep_scan import deep_scan, default_deep_scan_roots
from mac_dev_clean.events import EventEmitter
from mac_dev_clean.fsevents import HistoryProbe
from mac_dev_clean.index import open_index

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=400)
BIG = b"x" * (2 * 1024 * 1024)


def touch(path: Path, when, payload: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    os.utime(str(path), (when.timestamp(), when.timestamp()))


def build_stale_project(root: Path) -> Path:
    (root / ".git").mkdir(parents=True, exist_ok=True)
    touch(root / ".git" / "HEAD", OLD)
    touch(root / "src" / "main.ts", OLD)
    touch(root / "package.json", OLD, b"{}")
    touch(root / "pnpm-lock.yaml", OLD)
    touch(root / "node_modules" / "pkg" / "index.js", OLD, BIG)
    return root / "node_modules"


class DeepScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.index = open_index(self.home / "index.sqlite3")

    def tearDown(self):
        self.index.close()
        self.temp.cleanup()

    def test_a_stale_project_produces_a_selected_recommendation(self):
        expected = build_stale_project(self.home / "projects" / "app")

        result = deep_scan([self.home], self.index, now=NOW, use_fsevents=False)

        self.assertFalse(result.cancelled)
        paths = {item.path for item in result.recommendations}
        self.assertIn(expected, paths)
        item = next(i for i in result.recommendations if i.path == expected)
        self.assertTrue(item.selected_by_default)

    def test_recommendations_are_persisted_and_the_generation_completes(self):
        expected = build_stale_project(self.home / "app")

        result = deep_scan([self.home], self.index, now=NOW, use_fsevents=False)
        item = next(i for i in result.recommendations if i.path == expected)

        self.assertIsNotNone(self.index.latest_complete_generation())
        self.assertIsNotNone(self.index.load_recommendation(item.id))

    def test_streamed_events_end_with_scan_completed(self):
        build_stale_project(self.home / "app")
        stream = io.StringIO()

        deep_scan(
            [self.home],
            self.index,
            emitter=EventEmitter(stream, generation=1),
            now=NOW,
            use_fsevents=False,
        )

        events = [json.loads(line)["event"] for line in stream.getvalue().splitlines() if line]
        self.assertEqual(events[0], "scan_started")
        self.assertEqual(events[-1], "scan_completed")
        self.assertIn("candidate_found", events)

    def test_cancellation_leaves_the_previous_generation_as_latest(self):
        build_stale_project(self.home / "app")
        deep_scan([self.home], self.index, now=NOW, use_fsevents=False)
        good_generation = self.index.latest_complete_generation()

        stream = io.StringIO()
        result = deep_scan(
            [self.home],
            self.index,
            emitter=EventEmitter(stream, generation=99),
            now=NOW,
            should_cancel=lambda: True,
            use_fsevents=False,
        )

        self.assertTrue(result.cancelled)
        self.assertEqual(self.index.latest_complete_generation(), good_generation)
        events = [json.loads(line)["event"] for line in stream.getvalue().splitlines() if line]
        self.assertEqual(events[-1], "scan_cancelled")

    def test_a_history_gap_forces_a_full_walk(self):
        build_stale_project(self.home / "app")
        deep_scan([self.home], self.index, now=NOW, use_fsevents=False)

        with patch.object(
            deep_scan_module.fsevents,
            "probe_history",
            return_value=HistoryProbe(usable=False, reason="no-retained-history"),
        ), patch.object(deep_scan_module.fsevents, "replay_changed_paths") as replay:
            result = deep_scan([self.home], self.index, now=NOW, use_fsevents=True)

        replay.assert_not_called()
        self.assertFalse(result.incremental)
        self.assertTrue(result.recommendations)

    def test_continuous_history_enables_an_incremental_scan(self):
        build_stale_project(self.home / "app")
        deep_scan([self.home], self.index, now=NOW, use_fsevents=False)

        with patch.object(
            deep_scan_module.fsevents, "probe_history", return_value=HistoryProbe(usable=True)
        ), patch.object(
            deep_scan_module.fsevents, "replay_changed_paths", return_value=set()
        ), patch.object(
            deep_scan_module.fsevents, "current_event_id", return_value=1234
        ):
            result = deep_scan([self.home], self.index, now=NOW, use_fsevents=True)

        self.assertTrue(result.incremental)

    def test_a_failed_replay_falls_back_to_a_full_walk(self):
        build_stale_project(self.home / "app")
        deep_scan([self.home], self.index, now=NOW, use_fsevents=False)

        with patch.object(
            deep_scan_module.fsevents, "probe_history", return_value=HistoryProbe(usable=True)
        ), patch.object(
            deep_scan_module.fsevents, "replay_changed_paths", return_value=None
        ):
            result = deep_scan([self.home], self.index, now=NOW, use_fsevents=True)

        self.assertFalse(result.incremental)

    def test_an_unreadable_root_is_reported_without_failing_the_scan(self):
        build_stale_project(self.home / "app")
        locked = self.home / "locked"
        locked.mkdir()
        os.chmod(str(locked), 0o000)
        stream = io.StringIO()
        try:
            result = deep_scan(
                [self.home],
                self.index,
                emitter=EventEmitter(stream, generation=1),
                now=NOW,
                use_fsevents=False,
            )
        finally:
            os.chmod(str(locked), 0o755)

        self.assertFalse(result.cancelled)
        self.assertTrue(result.recommendations)


class DefaultRootTests(unittest.TestCase):
    def test_default_roots_exclude_the_library_folder(self):
        with TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "Library").mkdir()
            (home / "projects").mkdir()

            roots = default_deep_scan_roots(home)

            self.assertEqual(roots, [home])
            self.assertNotIn(home / "Library", roots)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_deep_scan -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.deep_scan'`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/deep_scan.py`:

```python
from __future__ import annotations

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

PROTECTED_FOLDER_NAMES = ("Desktop", "Documents", "Downloads")

PROGRESS_EVERY = 25
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_deep_scan -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/deep_scan.py tests/test_deep_scan.py
git commit -m "feat: orchestrate deep scan with incremental probe and cancellation"
```

---

### Task 9: Executor with revalidation

**Files:**
- Create: `src/mac_dev_clean/executor.py`
- Test: `tests/test_executor.py`

**Interfaces:**
- Consumes: `index.ScanIndex`, `recommendation.Recommendation`, `projects.RECIPES`, `projects.last_meaningful_activity`, `cleaner._remove_path`.
- Produces: `ApplyOutcome` enum (`REMOVED`, `SKIPPED`, `CHANGED_SINCE_SCAN`, `FAILED`), `ApplyResult` dataclass with `.to_dict()`, `apply_recommendations(ids, index, dry_run=False, now=None, should_cancel=None) -> List[ApplyResult]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_executor.py`:

```python
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from mac_dev_clean.deep_scan import deep_scan
from mac_dev_clean.executor import ApplyOutcome, apply_recommendations
from mac_dev_clean.index import open_index

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=400)
BIG = b"x" * (2 * 1024 * 1024)


def touch(path: Path, when, payload: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    os.utime(str(path), (when.timestamp(), when.timestamp()))


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.index = open_index(self.home / "index.sqlite3")
        self.project = self.home / "app"
        (self.project / ".git").mkdir(parents=True)
        touch(self.project / ".git" / "HEAD", OLD)
        touch(self.project / "src" / "main.ts", OLD)
        touch(self.project / "package.json", OLD, b"{}")
        touch(self.project / "pnpm-lock.yaml", OLD)
        touch(self.project / "node_modules" / "pkg" / "index.js", OLD, BIG)
        self.artifact = self.project / "node_modules"
        result = deep_scan([self.home], self.index, now=NOW, use_fsevents=False)
        self.item = next(i for i in result.recommendations if i.path == self.artifact)

    def tearDown(self):
        self.index.close()
        self.temp.cleanup()

    def test_dry_run_reports_without_deleting(self):
        results = apply_recommendations([self.item.id], self.index, dry_run=True, now=NOW)

        self.assertEqual(results[0].outcome, ApplyOutcome.SKIPPED)
        self.assertTrue(results[0].dry_run)
        self.assertTrue(self.artifact.exists())

    def test_apply_removes_the_artifact_but_keeps_the_sources(self):
        results = apply_recommendations([self.item.id], self.index, now=NOW)

        self.assertEqual(results[0].outcome, ApplyOutcome.REMOVED)
        self.assertFalse(self.artifact.exists())
        self.assertTrue((self.project / "src" / "main.ts").exists())
        self.assertTrue((self.project / "package.json").exists())
        self.assertTrue((self.project / "pnpm-lock.yaml").exists())
        self.assertTrue((self.project / ".git").exists())

    def test_an_unknown_id_is_refused(self):
        results = apply_recommendations(["deadbeefdeadbeef"], self.index, now=NOW)

        self.assertEqual(results[0].outcome, ApplyOutcome.FAILED)
        self.assertIn("not found", results[0].error)

    def test_a_removed_lock_file_blocks_the_deletion(self):
        (self.project / "pnpm-lock.yaml").unlink()

        results = apply_recommendations([self.item.id], self.index, now=NOW)

        self.assertEqual(results[0].outcome, ApplyOutcome.CHANGED_SINCE_SCAN)
        self.assertTrue(self.artifact.exists())

    def test_a_recent_source_edit_blocks_the_deletion(self):
        touch(self.project / "src" / "main.ts", NOW - timedelta(days=1))

        results = apply_recommendations([self.item.id], self.index, now=NOW)

        self.assertEqual(results[0].outcome, ApplyOutcome.CHANGED_SINCE_SCAN)
        self.assertTrue(self.artifact.exists())

    def test_a_path_replaced_by_a_symlink_is_refused(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        for child in list(self.artifact.iterdir()):
            child.rename(elsewhere / child.name)
        self.artifact.rmdir()
        os.symlink(str(elsewhere), str(self.artifact))

        results = apply_recommendations([self.item.id], self.index, now=NOW)

        self.assertEqual(results[0].outcome, ApplyOutcome.FAILED)
        self.assertIn("symlink", results[0].error)
        self.assertTrue(elsewhere.exists())

    def test_a_missing_path_is_skipped_rather_than_failed(self):
        for child in list(self.artifact.iterdir()):
            if child.is_dir():
                for grandchild in child.iterdir():
                    grandchild.unlink()
                child.rmdir()
        self.artifact.rmdir()

        results = apply_recommendations([self.item.id], self.index, now=NOW)

        self.assertEqual(results[0].outcome, ApplyOutcome.SKIPPED)
        self.assertIn("no longer exists", results[0].error)

    def test_a_stale_generation_is_refused_after_a_new_scan(self):
        stale_id = self.item.id
        # A fresh scan of an emptied home retires the old generation entirely.
        import shutil

        shutil.rmtree(str(self.project))
        deep_scan([self.home], self.index, now=NOW, use_fsevents=False)

        results = apply_recommendations([stale_id], self.index, now=NOW)

        self.assertEqual(results[0].outcome, ApplyOutcome.FAILED)
        self.assertIn("not found", results[0].error)

    def test_partial_failure_does_not_hide_the_successful_item(self):
        results = apply_recommendations(
            [self.item.id, "deadbeefdeadbeef"], self.index, now=NOW
        )

        outcomes = [item.outcome for item in results]
        self.assertIn(ApplyOutcome.REMOVED, outcomes)
        self.assertIn(ApplyOutcome.FAILED, outcomes)

    def test_cancellation_stops_before_the_next_item(self):
        results = apply_recommendations(
            [self.item.id], self.index, now=NOW, should_cancel=lambda: True
        )

        self.assertEqual(results, [])
        self.assertTrue(self.artifact.exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_executor -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.executor'`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/executor.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence

from .cleaner import _remove_path
from .index import ScanIndex
from .model import human_bytes
from .projects import INACTIVITY_DAYS, RECIPES, last_meaningful_activity
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
    if activity >= now - timedelta(days=INACTIVITY_DAYS):
        return fail(ApplyOutcome.CHANGED_SINCE_SCAN, "the project was edited recently")

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python3 -m unittest tests.test_executor -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/executor.py tests/test_executor.py
git commit -m "feat: apply recommendations by id with live safety revalidation"
```

---

### Task 10: CLI commands

**Files:**
- Modify: `src/mac_dev_clean/cli.py` (add subparsers in `build_parser`, add three `run_*` functions, extend the `main` dispatch)
- Test: `tests/test_deep_scan_cli.py`

**Interfaces:**
- Consumes: `deep_scan.deep_scan`, `deep_scan.default_deep_scan_roots`, `index.open_index`, `executor.apply_recommendations`, `events.EventEmitter`.
- Produces: `mac-dev-clean deep-scan [--root PATH ...] [--json] [--no-fsevents] [--index PATH]`, `mac-dev-clean apply --id ID [--id ID ...] [--dry-run] [--json] [--index PATH]`, `mac-dev-clean reset-index [--index PATH]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_deep_scan_cli.py`:

```python
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from mac_dev_clean.cli import main

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=400)
BIG = b"x" * (2 * 1024 * 1024)


def touch(path: Path, when, payload: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    os.utime(str(path), (when.timestamp(), when.timestamp()))


def build_project(home: Path) -> Path:
    project = home / "app"
    (project / ".git").mkdir(parents=True)
    touch(project / ".git" / "HEAD", OLD)
    touch(project / "src" / "main.ts", OLD)
    touch(project / "package.json", OLD, b"{}")
    touch(project / "pnpm-lock.yaml", OLD)
    touch(project / "node_modules" / "pkg" / "index.js", OLD, BIG)
    return project / "node_modules"


class DeepScanCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.index_path = self.home / "index.sqlite3"
        self.artifact = build_project(self.home)

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def events(self, output):
        return [json.loads(line) for line in output.splitlines() if line.strip()]

    def test_deep_scan_streams_ndjson_by_default(self):
        code, output = self.run_cli(
            ["deep-scan", "--root", str(self.home), "--index", str(self.index_path), "--no-fsevents"]
        )

        self.assertEqual(code, 0)
        events = self.events(output)
        self.assertEqual(events[0]["event"], "scan_started")
        self.assertEqual(events[-1]["event"], "scan_completed")
        found = [e for e in events if e["event"] == "candidate_found"]
        self.assertTrue(found)
        self.assertEqual(found[0]["recommendation"]["detector_id"], "node-modules")

    def test_deep_scan_json_prints_one_summary_object(self):
        code, output = self.run_cli(
            ["deep-scan", "--root", str(self.home), "--index", str(self.index_path), "--no-fsevents", "--json"]
        )

        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["count"], len(payload["recommendations"]))
        self.assertGreater(payload["reclaimable_bytes"], 0)
        self.assertFalse(payload["cancelled"])

    def test_apply_dry_run_keeps_the_artifact(self):
        _, output = self.run_cli(
            ["deep-scan", "--root", str(self.home), "--index", str(self.index_path), "--no-fsevents", "--json"]
        )
        item_id = json.loads(output)["recommendations"][0]["id"]

        code, apply_output = self.run_cli(
            ["apply", "--id", item_id, "--index", str(self.index_path), "--dry-run", "--json"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(apply_output)["results"][0]["outcome"], "skipped")
        self.assertTrue(self.artifact.exists())

    def test_apply_removes_the_artifact(self):
        _, output = self.run_cli(
            ["deep-scan", "--root", str(self.home), "--index", str(self.index_path), "--no-fsevents", "--json"]
        )
        item_id = json.loads(output)["recommendations"][0]["id"]

        code, apply_output = self.run_cli(
            ["apply", "--id", item_id, "--index", str(self.index_path), "--json"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(apply_output)["results"][0]["outcome"], "removed")
        self.assertFalse(self.artifact.exists())

    def test_apply_requires_at_least_one_id(self):
        with self.assertRaises(SystemExit):
            self.run_cli(["apply", "--index", str(self.index_path)])

    def test_apply_reports_failures_with_exit_code_one(self):
        code, apply_output = self.run_cli(
            ["apply", "--id", "deadbeefdeadbeef", "--index", str(self.index_path), "--json"]
        )

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(apply_output)["results"][0]["outcome"], "failed")

    def test_reset_index_clears_stored_recommendations(self):
        _, output = self.run_cli(
            ["deep-scan", "--root", str(self.home), "--index", str(self.index_path), "--no-fsevents", "--json"]
        )
        item_id = json.loads(output)["recommendations"][0]["id"]

        code, _ = self.run_cli(["reset-index", "--index", str(self.index_path)])
        self.assertEqual(code, 0)

        code, apply_output = self.run_cli(
            ["apply", "--id", item_id, "--index", str(self.index_path), "--json"]
        )

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(apply_output)["results"][0]["outcome"], "failed")
        self.assertTrue(self.artifact.exists())


class LegacyContractTests(unittest.TestCase):
    def test_existing_scan_json_still_works(self):
        with TemporaryDirectory() as temp:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["scan", "--json", "--no-node-modules", "--no-project-derived-data"])

            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertIn("cleanable_total_bytes", payload)
            self.assertIn("items", payload)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_deep_scan_cli -v`
Expected: FAIL — argparse rejects `deep-scan` as an invalid choice, raising `SystemExit`.

- [ ] **Step 3: Add the imports**

In `src/mac_dev_clean/cli.py`, replace the import block at lines 9-12:

```python
from .age import parse_age
from .cleaner import clean_targets
from .output import clean_report_json, render_clean_table, render_scan_table, scan_report_json
from .scanner import scan
```

with:

```python
from .age import parse_age
from .cleaner import clean_targets
from .deep_scan import deep_scan, default_deep_scan_roots
from .events import EventEmitter
from .executor import ApplyOutcome, apply_recommendations
from .index import open_index
from .output import clean_report_json, render_clean_table, render_scan_table, scan_report_json
from .scanner import scan
```

- [ ] **Step 4: Add the three subparsers**

In `build_parser`, immediately before `return parser` (currently line 249), insert:

```python
    deep_parser = subparsers.add_parser(
        "deep-scan",
        help="Recursively analyse projects and stream findings as NDJSON events.",
    )
    deep_parser.add_argument(
        "--root",
        action="append",
        type=Path,
        default=[],
        dest="deep_root",
        help="Directory to analyse. Defaults to the home directory. Repeatable.",
    )
    deep_parser.add_argument("--index", type=Path, default=None, help="Index database path.")
    deep_parser.add_argument(
        "--json",
        action="store_true",
        help="Print a single summary object instead of streaming NDJSON events.",
    )
    deep_parser.add_argument(
        "--no-fsevents",
        action="store_true",
        help="Always perform a full metadata walk instead of an incremental scan.",
    )

    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply deep-scan recommendations by ID after revalidating them.",
    )
    apply_parser.add_argument(
        "--id",
        action="append",
        default=[],
        dest="recommendation_id",
        help="Recommendation ID from a deep scan. Repeatable.",
    )
    apply_parser.add_argument("--index", type=Path, default=None, help="Index database path.")
    apply_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be removed."
    )
    apply_parser.add_argument("--json", action="store_true", help="Print JSON output.")

    reset_parser = subparsers.add_parser(
        "reset-index",
        help="Delete the local deep-scan index. The next deep scan rebuilds it.",
    )
    reset_parser.add_argument("--index", type=Path, default=None, help="Index database path.")

    return parser
```

- [ ] **Step 5: Add the command handlers**

In `src/mac_dev_clean/cli.py`, insert these functions immediately before `def announce_scan_start` (currently line 345):

```python
def run_deep_scan(args: argparse.Namespace) -> int:
    roots = list(args.deep_root) or default_deep_scan_roots(Path.home())
    if not roots:
        print("No readable scan roots were found.", file=sys.stderr)
        return 1

    index = open_index(args.index)
    try:
        # The generation is assigned by the index once the scan starts, so the
        # envelope cannot carry it yet. Emit -1 rather than a plausible-looking
        # 0, which reads as a real generation number next to the recommendation
        # payloads that carry the true one.
        emitter = None if args.json else EventEmitter(sys.stdout, generation=-1)
        result = deep_scan(
            roots,
            index,
            emitter=emitter,
            use_fsevents=not args.no_fsevents,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "cancelled": result.cancelled,
                        "incremental": result.incremental,
                        "reclaimable_bytes": result.reclaimable_bytes,
                        "count": len(result.recommendations),
                        "recommendations": [
                            item.to_dict() for item in result.recommendations
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
    finally:
        index.close()
    return 0


def run_apply(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if not args.recommendation_id:
        parser.error("apply requires at least one --id")

    index = open_index(args.index)
    try:
        results = apply_recommendations(
            args.recommendation_id, index, dry_run=args.dry_run
        )
    finally:
        index.close()

    if args.json:
        print(
            json.dumps(
                {"results": [item.to_dict() for item in results]},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for item in results:
            detail = " — {}".format(item.error) if item.error else ""
            print("{:<20} {}{}".format(item.outcome.value, item.path, detail))

    failed = any(item.outcome is ApplyOutcome.FAILED for item in results)
    return 1 if failed else 0


def run_reset_index(args: argparse.Namespace) -> int:
    index = open_index(args.index)
    try:
        index.reset()
    finally:
        index.close()
    print("Deep scan index cleared.")
    return 0
```

Add `import json` to the top of `cli.py` — the module currently imports only `argparse` and `sys`. The import block becomes:

```python
import argparse
import json
import sys
```

- [ ] **Step 6: Extend the dispatch**

In `main`, replace the try block body (currently lines 66-71):

```python
        if args.command in {"scan", "report"}:
            return run_scan(args)
        if args.command == "clean":
            return run_clean(args, parser)
        if args.command in {None, "interactive"}:
            return run_interactive(args, parser)
```

with:

```python
        if args.command in {"scan", "report"}:
            return run_scan(args)
        if args.command == "clean":
            return run_clean(args, parser)
        if args.command == "deep-scan":
            return run_deep_scan(args)
        if args.command == "apply":
            return run_apply(args, parser)
        if args.command == "reset-index":
            return run_reset_index(args)
        if args.command in {None, "interactive"}:
            return run_interactive(args, parser)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `PYTHONPATH=src python3 -m unittest tests.test_deep_scan_cli -v`
Expected: PASS, 8 tests.

- [ ] **Step 8: Run the whole Python suite**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests`
Expected: OK, with the pre-existing tests still passing.

- [ ] **Step 9: Commit**

```bash
git add src/mac_dev_clean/cli.py tests/test_deep_scan_cli.py
git commit -m "feat: add deep-scan, apply, and reset-index cli commands"
```

---

### Task 11: Swift streaming backend

**Files:**
- Create: `macos/Sources/MacDevCleanApp/DeepScanModels.swift`
- Create: `macos/Sources/MacDevCleanApp/DeepScanBackend.swift`
- Create: `macos/Tests/MacDevCleanAppTests/DeepScanTests.swift`

**Interfaces:**
- Consumes: the NDJSON contract from Task 7 and the CLI from Task 10.
- Produces: `DeepScanEvent` enum, `RecommendationItem` struct, `DeepScanState` struct, `DeepScanBackendProtocol` with `deepScan(onEvent:) async throws` and `apply(ids:dryRun:) async throws -> [ApplyResultItem]`, plus `DeepScanEventParser`.

- [ ] **Step 1: Write the failing test**

Create `macos/Tests/MacDevCleanAppTests/DeepScanTests.swift`:

```swift
import Foundation
import Testing
@testable import MacDevCleanApp

@Test func parserDecodesACandidateEvent() throws {
    let line = #"""
    {"protocol_version":1,"generation":3,"event":"candidate_found","recommendation":{"id":"abc123","detector_id":"node-modules","category":"project-dependencies","label":"node_modules","path":"/Users/test/app/node_modules","action":"delete_tree","allocated_bytes":2048,"reclaimable_bytes":2048,"size":"2.0 KB","confidence":"strong","restoration":"redownload","selected_by_default":true,"evidence":[{"code":"lock-file","detail":"pnpm-lock.yaml exists"}],"safety_root":"/Users/test/app","reason":"Project has not changed in 143 days.","warning":"Close Xcode","generation":3,"last_activity_at":null}}
    """#

    let event = try #require(DeepScanEventParser.parse(line: line))

    guard case let .candidateFound(item) = event else {
        Issue.record("expected candidateFound, got \(event)")
        return
    }
    #expect(item.id == "abc123")
    #expect(item.selectedByDefault)
    #expect(item.action == "delete_tree")
    #expect(item.evidence.first?.detail == "pnpm-lock.yaml exists")
}

@Test func parserIgnoresBlankAndUnknownLines() throws {
    #expect(DeepScanEventParser.parse(line: "") == nil)
    #expect(DeepScanEventParser.parse(line: "   ") == nil)
    #expect(DeepScanEventParser.parse(line: "not json") == nil)

    let future = #"{"protocol_version":1,"generation":1,"event":"future_event_type"}"#
    #expect(DeepScanEventParser.parse(line: future) == nil)
}

@Test func parserRejectsAnIncompatibleProtocolVersion() throws {
    let line = #"{"protocol_version":99,"generation":1,"event":"scan_started","roots":["/Users/test/home"],"incremental":false}"#

    #expect(DeepScanEventParser.parse(line: line) == nil)
}

@Test func parserDecodesTerminalEvents() throws {
    let completed = #"{"protocol_version":1,"generation":1,"event":"scan_completed","reclaimable_bytes":100,"count":2}"#
    let cancelled = #"{"protocol_version":1,"generation":1,"event":"scan_cancelled","reclaimable_bytes":0,"count":0}"#

    guard case let .completed(bytes, count) = try #require(DeepScanEventParser.parse(line: completed)) else {
        Issue.record("expected completed")
        return
    }
    #expect(bytes == 100)
    #expect(count == 2)

    guard case .cancelled = try #require(DeepScanEventParser.parse(line: cancelled)) else {
        Issue.record("expected cancelled")
        return
    }
}

@Test func stateAccumulatesCandidatesAndTotals() {
    var state = DeepScanState()
    state.apply(.started(roots: ["/Users/test/home"], incremental: false))
    state.apply(.candidateFound(item(id: "a", bytes: 100, selected: true)))
    state.apply(.candidateFound(item(id: "b", bytes: 50, selected: false)))
    state.apply(.progress(path: "/Users/test/app", scanned: 25))

    #expect(state.isRunning)
    #expect(state.items.count == 2)
    #expect(state.selectedIds == ["a"])
    #expect(state.selectedBytes == 100)
    #expect(state.currentPath == "/Users/test/app")

    state.apply(.completed(reclaimableBytes: 150, count: 2))

    #expect(!state.isRunning)
    #expect(!state.wasCancelled)
}

@Test func stateSortsBySizeDescending() {
    var state = DeepScanState()
    state.apply(.candidateFound(item(id: "small", bytes: 10, selected: true)))
    state.apply(.candidateFound(item(id: "large", bytes: 900, selected: true)))

    #expect(state.items.map(\.id) == ["large", "small"])
}

@Test func stateNeverPreselectsAReportOnlyItem() {
    var state = DeepScanState()
    state.apply(.candidateFound(item(id: "a", bytes: 100, selected: false)))

    #expect(state.selectedIds.isEmpty)
    #expect(state.selectedBytes == 0)
}

@Test func cancellationIsReflectedInState() {
    var state = DeepScanState()
    state.apply(.started(roots: ["/Users/test/home"], incremental: false))
    state.apply(.cancelled(reclaimableBytes: 0, count: 0))

    #expect(!state.isRunning)
    #expect(state.wasCancelled)
}

@Test func warningsAreCollectedWithoutStoppingTheScan() {
    var state = DeepScanState()
    state.apply(.started(roots: ["/Users/test/home"], incremental: false))
    state.apply(.warning(message: "Permission denied", path: "/Users/test/locked"))
    state.apply(.candidateFound(item(id: "a", bytes: 10, selected: true)))
    state.apply(.completed(reclaimableBytes: 10, count: 1))

    #expect(state.warnings.count == 1)
    #expect(state.items.count == 1)
}

private func item(id: String, bytes: Int64, selected: Bool) -> RecommendationItem {
    RecommendationItem(
        id: id,
        detectorId: "node-modules",
        category: "project-dependencies",
        label: "node_modules",
        path: "/Users/test/\(id)/node_modules",
        action: "delete_tree",
        allocatedBytes: bytes,
        reclaimableBytes: bytes,
        size: ByteFormatter.string(bytes),
        confidence: "strong",
        restoration: "redownload",
        selectedByDefault: selected,
        evidence: [],
        safetyRoot: "/Users/test/\(id)",
        reason: "Project has not changed in 143 days.",
        warning: "",
        generation: 1,
        lastActivityAt: nil
    )
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `swift test --package-path macos`
Expected: FAIL — `cannot find 'DeepScanEventParser' in scope`.

- [ ] **Step 3: Write the models**

Create `macos/Sources/MacDevCleanApp/DeepScanModels.swift`:

```swift
import Foundation

struct EvidenceItem: Decodable, Hashable, Sendable {
    let code: String
    let detail: String
}

struct RecommendationItem: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    let detectorId: String
    let category: String
    let label: String
    let path: String
    let action: String
    let allocatedBytes: Int64
    let reclaimableBytes: Int64
    let size: String
    let confidence: String
    let restoration: String
    let selectedByDefault: Bool
    let evidence: [EvidenceItem]
    let safetyRoot: String
    let reason: String
    let warning: String
    let generation: Int
    let lastActivityAt: String?

    var displayPath: String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        if path == home { return "~" }
        if path.hasPrefix(home + "/") {
            return "~" + path.dropFirst(home.count)
        }
        return path
    }

    var restorationSummary: String {
        switch restoration {
        case "rebuild": "Rebuilt by the next build"
        case "redownload": "Reinstalled from the lock file"
        case "reinstall": "Reinstalled manually"
        case "external_state": "Managed by another tool"
        default: "No restore needed"
        }
    }

    enum CodingKeys: String, CodingKey {
        case id
        case detectorId = "detector_id"
        case category
        case label
        case path
        case action
        case allocatedBytes = "allocated_bytes"
        case reclaimableBytes = "reclaimable_bytes"
        case size
        case confidence
        case restoration
        case selectedByDefault = "selected_by_default"
        case evidence
        case safetyRoot = "safety_root"
        case reason
        case warning
        case generation
        case lastActivityAt = "last_activity_at"
    }
}

struct ApplyResultItem: Decodable, Sendable {
    let id: String
    let label: String
    let path: String
    let reclaimableBytes: Int64
    let size: String
    let outcome: String
    let dryRun: Bool
    let error: String

    var succeeded: Bool { outcome == "removed" }

    enum CodingKeys: String, CodingKey {
        case id
        case label
        case path
        case reclaimableBytes = "reclaimable_bytes"
        case size
        case outcome
        case dryRun = "dry_run"
        case error
    }
}

struct ApplyReport: Decodable, Sendable {
    let results: [ApplyResultItem]
}

enum DeepScanEvent: Sendable {
    case started(roots: [String], incremental: Bool)
    case rootStarted(path: String)
    case progress(path: String, scanned: Int)
    case candidateFound(RecommendationItem)
    case rootFinished(path: String, repositories: Int)
    case permissionRequired(path: String, folder: String)
    case warning(message: String, path: String)
    case completed(reclaimableBytes: Int64, count: Int)
    case cancelled(reclaimableBytes: Int64, count: Int)
}

enum DeepScanEventParser {
    static let supportedProtocolVersion = 1

    /// Decode one NDJSON line. Returns nil for blank lines, malformed JSON,
    /// unknown event names, and incompatible protocol versions so a future
    /// engine can add events without breaking an older app build.
    static func parse(line: String) -> DeepScanEvent? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let data = trimmed.data(using: .utf8) else { return nil }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        guard let version = object["protocol_version"] as? Int,
              version == supportedProtocolVersion,
              let event = object["event"] as? String
        else { return nil }

        switch event {
        case "scan_started":
            return .started(
                roots: object["roots"] as? [String] ?? [],
                incremental: object["incremental"] as? Bool ?? false
            )
        case "root_started":
            return .rootStarted(path: object["path"] as? String ?? "")
        case "progress":
            return .progress(
                path: object["path"] as? String ?? "",
                scanned: object["scanned"] as? Int ?? 0
            )
        case "candidate_found":
            guard let raw = object["recommendation"],
                  let payload = try? JSONSerialization.data(withJSONObject: raw),
                  let item = try? JSONDecoder().decode(RecommendationItem.self, from: payload)
            else { return nil }
            return .candidateFound(item)
        case "root_finished":
            return .rootFinished(
                path: object["path"] as? String ?? "",
                repositories: object["repositories"] as? Int ?? 0
            )
        case "permission_required":
            return .permissionRequired(
                path: object["path"] as? String ?? "",
                folder: object["folder"] as? String ?? ""
            )
        case "warning":
            return .warning(
                message: object["message"] as? String ?? "",
                path: object["path"] as? String ?? ""
            )
        case "scan_completed":
            return .completed(
                reclaimableBytes: (object["reclaimable_bytes"] as? NSNumber)?.int64Value ?? 0,
                count: object["count"] as? Int ?? 0
            )
        case "scan_cancelled":
            return .cancelled(
                reclaimableBytes: (object["reclaimable_bytes"] as? NSNumber)?.int64Value ?? 0,
                count: object["count"] as? Int ?? 0
            )
        default:
            return nil
        }
    }
}

struct DeepScanWarning: Identifiable, Hashable, Sendable {
    let id = UUID()
    let message: String
    let path: String
}

struct DeepScanState: Sendable {
    private(set) var items: [RecommendationItem] = []
    private(set) var selectedIds: Set<String> = []
    private(set) var warnings: [DeepScanWarning] = []
    private(set) var blockedFolders: [String] = []
    private(set) var currentPath: String = ""
    private(set) var scannedCount: Int = 0
    private(set) var isRunning: Bool = false
    private(set) var wasCancelled: Bool = false
    private(set) var isIncremental: Bool = false

    var selectedBytes: Int64 {
        items.filter { selectedIds.contains($0.id) }
            .reduce(0) { $0 + $1.reclaimableBytes }
    }

    var totalBytes: Int64 {
        items.reduce(0) { $0 + $1.reclaimableBytes }
    }

    mutating func reset() {
        self = DeepScanState()
    }

    mutating func toggle(_ id: String) {
        if selectedIds.contains(id) {
            selectedIds.remove(id)
        } else if items.contains(where: { $0.id == id }) {
            selectedIds.insert(id)
        }
    }

    mutating func apply(_ event: DeepScanEvent) {
        switch event {
        case let .started(_, incremental):
            isRunning = true
            wasCancelled = false
            isIncremental = incremental
        case let .rootStarted(path):
            currentPath = path
        case let .progress(path, scanned):
            currentPath = path
            scannedCount = scanned
        case let .candidateFound(item):
            items.append(item)
            items.sort { $0.reclaimableBytes > $1.reclaimableBytes }
            if item.selectedByDefault {
                selectedIds.insert(item.id)
            }
        case .rootFinished:
            break
        case let .permissionRequired(_, folder):
            if !blockedFolders.contains(folder) {
                blockedFolders.append(folder)
            }
        case let .warning(message, path):
            warnings.append(DeepScanWarning(message: message, path: path))
        case .completed:
            isRunning = false
        case .cancelled:
            isRunning = false
            wasCancelled = true
        }
    }
}
```

- [ ] **Step 4: Write the streaming backend**

Create `macos/Sources/MacDevCleanApp/DeepScanBackend.swift`:

```swift
import Foundation

protocol DeepScanBackendProtocol: Sendable {
    func deepScan(onEvent: @Sendable @escaping (DeepScanEvent) -> Void) async throws
    func apply(ids: [String], dryRun: Bool) async throws -> [ApplyResultItem]
}

struct DeepScanBackend: DeepScanBackendProtocol, Sendable {
    let location: BackendLocation

    init(location: BackendLocation? = nil) throws {
        self.location = try location ?? BackendLocator.locate()
    }

    /// Reads stdout incrementally so the UI can render findings while the scan
    /// is still walking the disk. Cancelling the enclosing Task terminates the
    /// Python child, which leaves its generation incomplete and therefore
    /// ineligible for cleanup.
    func deepScan(onEvent: @Sendable @escaping (DeepScanEvent) -> Void) async throws {
        let location = location
        try await withTaskCancellationHandler {
            let process = Process()
            let stdoutPipe = Pipe()
            let stderrPipe = Pipe()
            process.executableURL = location.pythonURL
            process.arguments = ["-m", "mac_dev_clean", "deep-scan"]
            process.currentDirectoryURL = location.workingDirectory
            process.standardOutput = stdoutPipe
            process.standardError = stderrPipe
            process.environment = CleanupBackend.pythonEnvironment(
                base: ProcessInfo.processInfo.environment,
                pythonPath: location.pythonPath
            )

            try process.run()
            defer {
                if process.isRunning { process.terminate() }
            }

            var buffer = Data()
            let handle = stdoutPipe.fileHandleForReading
            while true {
                let chunk = handle.availableData
                if chunk.isEmpty { break }
                buffer.append(chunk)
                while let newline = buffer.firstIndex(of: UInt8(ascii: "\n")) {
                    let lineData = buffer[buffer.startIndex..<newline]
                    buffer.removeSubrange(buffer.startIndex...newline)
                    guard let line = String(data: lineData, encoding: .utf8),
                          let event = DeepScanEventParser.parse(line: line)
                    else { continue }
                    onEvent(event)
                }
            }

            process.waitUntilExit()
            let stderrData = stderrPipe.fileHandleForReading.readDataToEndOfFile()
            if process.terminationStatus != 0 && !Task.isCancelled {
                throw CleanupBackend.commandFailure(
                    operation: "deep scan",
                    result: CommandResult(
                        stdout: Data(),
                        stderr: String(data: stderrData, encoding: .utf8) ?? "",
                        terminationStatus: process.terminationStatus
                    )
                )
            }
        } onCancel: {
            // The child sees EOF on the next write and exits; its generation is
            // never completed, so nothing partial becomes cleanup-eligible.
        }
    }

    func apply(ids: [String], dryRun: Bool) async throws -> [ApplyResultItem] {
        guard !ids.isEmpty else { return [] }
        var arguments = ["-m", "mac_dev_clean", "apply", "--json"]
        for id in ids {
            arguments.append("--id")
            arguments.append(id)
        }
        if dryRun { arguments.append("--dry-run") }

        let location = location
        let result = try await Task.detached(priority: .userInitiated) {
            let process = Process()
            let stdoutPipe = Pipe()
            let stderrPipe = Pipe()
            process.executableURL = location.pythonURL
            process.arguments = arguments
            process.currentDirectoryURL = location.workingDirectory
            process.standardOutput = stdoutPipe
            process.standardError = stderrPipe
            process.environment = CleanupBackend.pythonEnvironment(
                base: ProcessInfo.processInfo.environment,
                pythonPath: location.pythonPath
            )
            try process.run()
            // Both pipes must be drained BEFORE waiting, and concurrently with
            // each other: `apply` prints one JSON object per item, so a large
            // selection (~200 items) overflows the ~64KB pipe buffer and the
            // child blocks on write while the parent blocks in waitUntilExit.
            async let stdoutData = Self.drainToEnd(stdoutPipe.fileHandleForReading)
            async let stderrBytes = Self.drainToEnd(stderrPipe.fileHandleForReading)
            let stdout = await stdoutData
            let stderrData = await stderrBytes
            process.waitUntilExit()
            return CommandResult(
                stdout: stdout,
                stderr: String(data: stderrData, encoding: .utf8) ?? "",
                terminationStatus: process.terminationStatus
            )
        }.value

        // Exit code 1 means "finished, but some items were refused". The JSON
        // body is still the authoritative per-item result the UI must show.
        if let report = try? JSONDecoder().decode(ApplyReport.self, from: result.stdout) {
            return report.results
        }
        throw CleanupBackend.commandFailure(operation: "apply", result: result)
    }
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `swift test --package-path macos`
Expected: PASS, including the 9 new deep-scan tests and all pre-existing `ModelTests`.

- [ ] **Step 6: Commit**

```bash
git add macos/Sources/MacDevCleanApp/DeepScanModels.swift \
        macos/Sources/MacDevCleanApp/DeepScanBackend.swift \
        macos/Tests/MacDevCleanAppTests/DeepScanTests.swift
git commit -m "feat: add swift ndjson deep-scan backend and recommendation models"
```

---

### Task 12: Deep Scan UI slice

**Files:**
- Modify: `macos/Sources/MacDevCleanApp/AppModel.swift`
- Modify: `macos/Sources/MacDevCleanApp/ContentView.swift`
- Test: `macos/Tests/MacDevCleanAppTests/DeepScanTests.swift` (append)

**Interfaces:**
- Consumes: `DeepScanBackendProtocol`, `DeepScanState`, `DeepScanEvent`, `ApplyResultItem`.
- Produces: `AppModel.deepScanState`, `.startDeepScan()`, `.cancelDeepScan()`, `.applyDeepScanSelection()`, `.toggleDeepScanItem(_:)`, and a new `SidebarPage.deepScan` case.

- [ ] **Step 1: Write the failing test**

Append to `macos/Tests/MacDevCleanAppTests/DeepScanTests.swift`:

```swift
@MainActor
@Test func appModelStreamsDeepScanEventsIntoState() async {
    let events: [DeepScanEvent] = [
        .started(roots: ["/Users/test/home"], incremental: false),
        .candidateFound(item(id: "a", bytes: 500, selected: true)),
        .candidateFound(item(id: "b", bytes: 200, selected: false)),
        .completed(reclaimableBytes: 700, count: 2),
    ]
    let model = AppModel(
        backend: EmptyCleanupBackend(),
        deepScanBackend: StubDeepScanBackend(events: events, applyResults: [])
    )

    await model.startDeepScan()

    #expect(model.deepScanState.items.count == 2)
    #expect(model.deepScanState.selectedIds == ["a"])
    #expect(!model.deepScanState.isRunning)
    #expect(model.deepScanState.selectedBytes == 500)
}

@MainActor
@Test func appModelReportsPartialApplyFailures() async {
    let events: [DeepScanEvent] = [
        .started(roots: ["/Users/test/home"], incremental: false),
        .candidateFound(item(id: "a", bytes: 500, selected: true)),
        .candidateFound(item(id: "b", bytes: 200, selected: true)),
        .completed(reclaimableBytes: 700, count: 2),
    ]
    let results = [
        ApplyResultItem(
            id: "a", label: "node_modules", path: "/Users/test/a/node_modules",
            reclaimableBytes: 500, size: "500 B", outcome: "removed", dryRun: false, error: ""
        ),
        ApplyResultItem(
            id: "b", label: "node_modules", path: "/Users/test/b/node_modules",
            reclaimableBytes: 200, size: "200 B", outcome: "changed_since_scan",
            dryRun: false, error: "the project was edited recently"
        ),
    ]
    let model = AppModel(
        backend: EmptyCleanupBackend(),
        deepScanBackend: StubDeepScanBackend(events: events, applyResults: results)
    )

    await model.startDeepScan()
    await model.applyDeepScanSelection()

    #expect(model.errorMessage == nil)
    #expect(model.warningMessage?.contains("edited recently") == true)
    #expect(model.warningMessage?.contains("1 item was skipped") == true)
}

@MainActor
@Test func appModelNeverAppliesAnEmptySelection() async {
    let backend = StubDeepScanBackend(events: [], applyResults: [])
    let model = AppModel(backend: EmptyCleanupBackend(), deepScanBackend: backend)

    await model.applyDeepScanSelection()

    #expect(await backend.appliedIds.isEmpty)
}

private struct EmptyCleanupBackend: CleanupBackendProtocol {
    func scan() async throws -> ScanReport {
        ScanReport(
            totalBytes: 0, total: "0 B", cleanableTotalBytes: 0, cleanableTotal: "0 B",
            reportOnlyTotalBytes: 0, reportOnlyTotal: "0 B", count: 0, items: []
        )
    }

    func clean(flags: [String]) async throws -> CleanReport {
        CleanReport(totalBytes: 0, total: "0 B", count: 0, items: [])
    }
}

private actor RecordedIds {
    var value: [String] = []
    func set(_ ids: [String]) { value = ids }
}

private struct StubDeepScanBackend: DeepScanBackendProtocol {
    let events: [DeepScanEvent]
    let applyResults: [ApplyResultItem]
    private let recorded = RecordedIds()

    var appliedIds: [String] {
        get async { await recorded.value }
    }

    func deepScan(onEvent: @Sendable @escaping (DeepScanEvent) -> Void) async throws {
        for event in events { onEvent(event) }
    }

    func apply(ids: [String], dryRun: Bool) async throws -> [ApplyResultItem] {
        await recorded.set(ids)
        return applyResults
    }
}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `swift test --package-path macos`
Expected: FAIL — `AppModel` has no `deepScanBackend:` initializer parameter.

- [ ] **Step 3: Extend AppModel**

In `macos/Sources/MacDevCleanApp/AppModel.swift`, replace the `Activity` enum (lines 7-19) with:

```swift
    enum Activity: Equatable {
        case idle
        case scanning
        case cleaning
        case deepScanning
        case applying

        var message: String {
            switch self {
            case .idle: "Ready"
            case .scanning: "Scanning developer storage…"
            case .cleaning: "Cleaning selected categories…"
            case .deepScanning: "Analysing projects…"
            case .applying: "Removing selected project artifacts…"
            }
        }
    }
```

Replace the stored properties and initializer (lines 21-46) with:

```swift
    @Published private(set) var report: ScanReport?
    @Published private(set) var diskSpace: DiskSpace?
    @Published private(set) var activity: Activity = .idle
    @Published private(set) var deepScanState = DeepScanState()
    @Published var selectedFlags: Set<String> = []
    @Published var errorMessage: String?
    @Published var warningMessage: String?
    @Published var noticeMessage: String?

    private let backend: (any CleanupBackendProtocol)?
    private let deepScanBackend: (any DeepScanBackendProtocol)?
    private let startupError: Error?
    private var deepScanTask: Task<Void, Never>?

    init(
        backend: (any CleanupBackendProtocol)? = nil,
        deepScanBackend: (any DeepScanBackendProtocol)? = nil
    ) {
        diskSpace = try? DiskSpace.current()
        if let backend {
            self.backend = backend
            self.deepScanBackend = deepScanBackend
            startupError = nil
        } else {
            do {
                self.backend = try CleanupBackend()
                self.deepScanBackend = deepScanBackend ?? (try? DeepScanBackend())
                startupError = nil
            } catch {
                self.backend = nil
                self.deepScanBackend = nil
                startupError = error
            }
        }
    }
```

Insert these methods immediately before `func selectAll()` (currently line 155):

```swift
    var deepScanSelectionSummary: String {
        ByteFormatter.string(deepScanState.selectedBytes)
    }

    func toggleDeepScanItem(_ id: String) {
        deepScanState.toggle(id)
    }

    func startDeepScan() async {
        await startDeepScan(preservingMessages: false)
    }

    /// Mirrors the existing `scan(preservingMessages:)` pattern: the rescan that
    /// follows a cleanup must not wipe the warning describing what was skipped.
    private func startDeepScan(preservingMessages: Bool) async {
        guard !isBusy, let deepScanBackend else { return }

        activity = .deepScanning
        if !preservingMessages {
            dismissMessage()
        }
        deepScanState.reset()

        // `self` is captured once, immutably, before the Task is formed. Capturing
        // the @MainActor model directly inside the @Sendable event callback would
        // be a concurrency error under Swift 6 strict checking.
        let task = Task { @MainActor [self] in
            let stream = AsyncStream<DeepScanEvent> { continuation in
                Task.detached {
                    do {
                        try await deepScanBackend.deepScan { event in
                            continuation.yield(event)
                        }
                    } catch {
                        // The terminal event never arrived; `isRunning` is cleared
                        // when the stream finishes below.
                    }
                    continuation.finish()
                }
            }
            for await event in stream {
                deepScanState.apply(event)
            }
        }
        deepScanTask = task
        await task.value
        deepScanTask = nil
        activity = .idle
        refreshDiskSpace()
    }

    func cancelDeepScan() {
        deepScanTask?.cancel()
        deepScanTask = nil
    }

    func applyDeepScanSelection() async {
        guard !isBusy, let deepScanBackend else { return }
        let ids = deepScanState.items
            .filter { deepScanState.selectedIds.contains($0.id) }
            .map(\.id)
        guard !ids.isEmpty else { return }

        activity = .applying
        dismissMessage()
        do {
            let results = try await deepScanBackend.apply(ids: ids, dryRun: false)
            let failures = results.filter { !$0.succeeded }
            if failures.isEmpty {
                let removed = results.reduce(Int64(0)) { $0 + $1.reclaimableBytes }
                noticeMessage = "Removed \(ByteFormatter.string(removed)) across \(results.count) location(s)."
            } else {
                warningMessage = Self.applyWarning(for: results)
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        activity = .idle
        refreshDiskSpace()
        await startDeepScan(preservingMessages: true)
    }

    static func applyWarning(for results: [ApplyResultItem]) -> String? {
        let failures = results.filter { !$0.succeeded }
        guard !failures.isEmpty else { return nil }

        let removed = results.filter(\.succeeded)
        let removedBytes = removed.reduce(Int64(0)) { $0 + $1.reclaimableBytes }
        let itemWord = failures.count == 1 ? "item was" : "items were"
        let success = removed.isEmpty
            ? "Nothing was removed."
            : "Removed \(ByteFormatter.string(removedBytes)) across \(removed.count) location(s)."
        let details = failures.map { "• \($0.label): \($0.error)\n  \($0.path)" }
            .joined(separator: "\n")

        return "\(failures.count) \(itemWord) skipped. \(success) No additional files were removed.\n\n\(details)"
    }
```

- [ ] **Step 4: Add the sidebar page**

In `macos/Sources/MacDevCleanApp/ContentView.swift`, replace the `SidebarPage` enum (lines 3-16) with:

```swift
enum SidebarPage: String, CaseIterable, Identifiable {
    case cleanup = "Cleanup"
    case deepScan = "Deep Scan"
    case review = "Review Only"
    case about = "About"

    var id: String { rawValue }
    var symbol: String {
        switch self {
        case .cleanup: "sparkles"
        case .deepScan: "magnifyingglass.circle"
        case .review: "archivebox"
        case .about: "info.circle"
        }
    }
}
```

- [ ] **Step 5: Add the Deep Scan view**

In `macos/Sources/MacDevCleanApp/ContentView.swift`, append this view at the end of the file:

```swift
struct DeepScanView: View {
    @EnvironmentObject private var model: AppModel
    @State private var showsConfirmation = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            controls
            Divider()
            if model.deepScanState.items.isEmpty {
                emptyState
            } else {
                List {
                    ForEach(model.deepScanState.items) { item in
                        row(for: item)
                    }
                }
                .listStyle(.inset)
            }
        }
        .confirmationDialog(
            "Remove selected project artifacts?",
            isPresented: $showsConfirmation,
            titleVisibility: .visible
        ) {
            Button("Remove \(model.deepScanState.selectedIds.count) Item(s)", role: .destructive) {
                Task { await model.applyDeepScanSelection() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(
                "This removes \(model.deepScanSelectionSummary) of build output and dependencies. "
                + "Source files, manifests, and lock files are never touched. "
                + "Close Xcode, Android Studio, and any running build first."
            )
        }
    }

    private var controls: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 12) {
                if model.deepScanState.isRunning {
                    Button("Cancel Scan") { model.cancelDeepScan() }
                } else {
                    Button("Start Deep Scan") {
                        Task { await model.startDeepScan() }
                    }
                    .buttonStyle(.borderedProminent)
                }
                Button("Remove Selected") { showsConfirmation = true }
                    .disabled(model.isBusy || model.deepScanState.selectedIds.isEmpty)
                Spacer()
                Text("Selected: \(model.deepScanSelectionSummary)")
                    .font(.callout.monospacedDigit())
            }

            if model.deepScanState.isRunning {
                ProgressView()
                    .controlSize(.small)
                Text(model.deepScanState.currentPath)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            if model.deepScanState.wasCancelled {
                Text("Scan cancelled. Partial results are shown and were not saved for cleanup.")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            ForEach(model.deepScanState.warnings) { warning in
                Text("\(warning.message) \(warning.path)")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
        .padding()
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Spacer()
            Text("No project artifacts analysed yet.")
                .foregroundStyle(.secondary)
            Text("Deep Scan looks for Git projects that have not changed in 90 days.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .frame(maxWidth: .infinity)
    }

    private func row(for item: RecommendationItem) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Toggle(
                isOn: Binding(
                    get: { model.deepScanState.selectedIds.contains(item.id) },
                    set: { _ in model.toggleDeepScanItem(item.id) }
                )
            ) {
                EmptyView()
            }
            .labelsHidden()
            .disabled(model.isBusy)

            VStack(alignment: .leading, spacing: 3) {
                Text(item.label)
                    .font(.headline)
                Text(item.displayPath)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(item.reason)
                    .font(.caption)
                Text(item.restorationSummary)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            Text(item.size)
                .font(.callout.monospacedDigit())
        }
        .padding(.vertical, 4)
    }
}
```

- [ ] **Step 6: Route the new page**

In `ContentView.body`, replace the `Group` block (currently lines 34-44) with:

```swift
            Group {
                if page == .about {
                    AboutView()
                } else if page == .deepScan {
                    VStack(spacing: 0) {
                        header
                        Divider()
                        DeepScanView()
                    }
                } else {
                    VStack(spacing: 0) {
                        header
                        Divider()
                        content
                    }
                }
            }
```

In the same file, change the toolbar guard so cleanup-only buttons stay hidden on the new page. Replace `if page == .cleanup {` (currently line 59) with:

```swift
                    if page == .cleanup {
```

and change the outer condition on line 50 from `if page != .about {` to:

```swift
                if page != .about && page != .deepScan {
```

- [ ] **Step 7: Make the `content` switch exhaustive**

`content` switches over `page`, so adding a case to `SidebarPage` breaks the build with `switch must be exhaustive`. In `ContentView.content`, replace:

```swift
            switch page {
            case .review:
                ReviewOnlyView()
            case .cleanup, .none:
                CleanupGroupsView()
            case .about:
                EmptyView()
            }
```

with:

```swift
            switch page {
            case .review:
                ReviewOnlyView()
            case .cleanup, .none:
                CleanupGroupsView()
            case .deepScan, .about:
                EmptyView()
            }
```

`.deepScan` is unreachable here because the `Group` in `body` routes that page to `DeepScanView` before `content` is ever evaluated; the case exists only to keep the switch total.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `swift test --package-path macos`
Expected: PASS, including the 3 new AppModel tests.

- [ ] **Step 8: Verify the app still builds**

Run:
```bash
xcodebuild -project macos/MacDevClean.xcodeproj -scheme MacDevCleanApp \
  -configuration Release -destination 'generic/platform=macOS' \
  CODE_SIGNING_ALLOWED=NO build
```
Expected: `** BUILD SUCCEEDED **`

- [ ] **Step 9: Commit**

```bash
git add macos/Sources/MacDevCleanApp/AppModel.swift \
        macos/Sources/MacDevCleanApp/ContentView.swift \
        macos/Tests/MacDevCleanAppTests/DeepScanTests.swift
git commit -m "feat: add deep scan page with streamed progress, selection, and apply"
```

---

### Task 12b: Register the new engine modules with the Xcode project

**Files:**
- Modify: `macos/project.yml`
- Modify: `macos/MacDevClean.xcodeproj/project.pbxproj` (regenerated, not hand-edited)

**Why this task exists:** `swift test --package-path macos` compiles by *directory*, so it picks up new Swift files automatically and will pass without this task. The Xcode project does not — `project.pbxproj` lists files explicitly, and CI runs `xcodebuild` against it. Worse, `project.yml`'s "Bundle Python cleanup engine" script phase hardcodes every Python module in `inputFiles`/`outputFiles`; the nine new engine modules must be added there or the shipped `.app` will bundle an incomplete engine and `deep-scan` will fail at runtime with `ModuleNotFoundError` even though every test passed.

**Interfaces:**
- Consumes: the nine Python modules from Tasks 1–9 and the two Swift files from Task 11.
- Produces: a regenerated `MacDevClean.xcodeproj` that builds and bundles them.

- [ ] **Step 1: Verify the failure this task fixes**

Run:
```bash
xcodebuild -project macos/MacDevClean.xcodeproj -scheme MacDevCleanApp \
  -configuration Release -destination 'generic/platform=macOS' \
  CODE_SIGNING_ALLOWED=NO build 2>&1 | grep -E 'error:|BUILD' | head
```
Expected: `** BUILD FAILED **` with `cannot find 'DeepScanState' in scope` and `cannot find type 'DeepScanBackendProtocol' in scope`. The new Swift files exist on disk but are absent from the project's Sources build phase.

- [ ] **Step 2: Add the new Python modules to the bundling script phase**

In `macos/project.yml`, in the `postBuildScripts` entry named `Bundle Python cleanup engine`, add these nine lines to `inputFiles`, keeping the existing alphabetical order (`age.py`, then the new `deep_scan.py`, `discovery.py`, `events.py`, `executor.py`, `fsevents.py`, `index.py`, `policy.py`, `projects.py`, `recommendation.py` interleaved with the current entries):

```yaml
          - $(SRCROOT)/../src/mac_dev_clean/deep_scan.py
          - $(SRCROOT)/../src/mac_dev_clean/discovery.py
          - $(SRCROOT)/../src/mac_dev_clean/events.py
          - $(SRCROOT)/../src/mac_dev_clean/executor.py
          - $(SRCROOT)/../src/mac_dev_clean/fsevents.py
          - $(SRCROOT)/../src/mac_dev_clean/index.py
          - $(SRCROOT)/../src/mac_dev_clean/policy.py
          - $(SRCROOT)/../src/mac_dev_clean/projects.py
          - $(SRCROOT)/../src/mac_dev_clean/recommendation.py
```

Add the nine matching `outputFiles` entries:

```yaml
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/deep_scan.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/discovery.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/events.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/executor.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/fsevents.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/index.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/policy.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/projects.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/recommendation.py
```

The `script:` body itself needs no change — it already copies `"${ENGINE_SOURCE}"/*.py`. Only the dependency declarations were incomplete.

The `sources:` entry for `MacDevCleanApp` is a directory path (`Sources/MacDevCleanApp`), so the two new Swift files need no `project.yml` change; regenerating is enough to pick them up.

- [ ] **Step 3: Regenerate the Xcode project**

```bash
xcodegen generate --spec macos/project.yml
```
Expected: `Created project at .../macos/MacDevClean.xcodeproj`. If `xcodegen` is missing, install it with `brew install xcodegen`.

- [ ] **Step 4: Verify the new files were registered**

```bash
grep -c 'DeepScanModels.swift\|DeepScanBackend.swift' macos/MacDevClean.xcodeproj/project.pbxproj
grep -c 'recommendation.py' macos/project.yml
```
Expected: a non-zero count from each (the Swift files appear as both a file reference and a build file; `recommendation.py` appears in both `inputFiles` and `outputFiles`).

- [ ] **Step 5: Build to verify it now succeeds**

```bash
xcodebuild -project macos/MacDevClean.xcodeproj -scheme MacDevCleanApp \
  -configuration Release -destination 'generic/platform=macOS' \
  CODE_SIGNING_ALLOWED=NO build 2>&1 | tail -5
```
Expected: `** BUILD SUCCEEDED **`

- [ ] **Step 6: Verify the built app bundles the complete engine**

```bash
APP=$(find ~/Library/Developer/Xcode/DerivedData -name 'mac-dev-clean.app' -path '*Release*' -maxdepth 5 2>/dev/null | head -1)
ls "$APP/Contents/Resources/python/mac_dev_clean/" | sort
```
Expected: all nineteen modules, including `deep_scan.py`, `discovery.py`, `events.py`, `executor.py`, `fsevents.py`, `index.py`, `policy.py`, `projects.py`, and `recommendation.py`. A missing module here means the shipped app would crash on Deep Scan even though every test passed.

- [ ] **Step 7: Commit**

```bash
git add macos/project.yml macos/MacDevClean.xcodeproj/project.pbxproj
git commit -m "build: register deep scan swift and python modules with the xcode project"
```

---

### Task 13: Full gates, live acceptance, and documentation

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the complete Python suite on both supported versions**

```bash
# Run from the repository root
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/mac-dev-clean-pycache" python3 -m compileall -q src tests
```
Expected: `OK` and no compile errors.

- [ ] **Step 2: Run the Swift suite and the release build**

```bash
swift test --package-path macos
xcodebuild -project macos/MacDevClean.xcodeproj -scheme MacDevCleanApp \
  -configuration Release -destination 'generic/platform=macOS' \
  CODE_SIGNING_ALLOWED=NO build
```
Expected: all tests pass, `** BUILD SUCCEEDED **`.

- [ ] **Step 3: Run the public repository audit**

```bash
./scripts/audit_public_repo.sh
```
Expected: `Public repository audit passed.` If it reports machine-specific paths, the offending fixture used a real home path — change it to `/Users/test/...`.

- [ ] **Step 4: Live read-only acceptance on a purpose-built tree**

This exercises the real filesystem without touching any real project.

```bash
ACCEPT="${TMPDIR:-/tmp}/mdc-accept"
rm -rf "$ACCEPT" && mkdir -p "$ACCEPT/stale/src" "$ACCEPT/stale/node_modules/pkg" "$ACCEPT/fresh/src" "$ACCEPT/fresh/node_modules/pkg"
for p in stale fresh; do
  (cd "$ACCEPT/$p" && git init -q . && printf '{}' > package.json && touch pnpm-lock.yaml)
  mkfile 3m "$ACCEPT/$p/node_modules/pkg/blob.bin" 2>/dev/null || dd if=/dev/zero of="$ACCEPT/$p/node_modules/pkg/blob.bin" bs=1m count=3 2>/dev/null
  echo "console.log(1)" > "$ACCEPT/$p/src/main.js"
done
# Age the stale project's meaningful files past the 90-day boundary.
find "$ACCEPT/stale" -not -path '*/.git/*' -exec touch -t 202401010000 {} +

PYTHONPATH=src python3 -m mac_dev_clean deep-scan \
  --root "$ACCEPT" --index "$ACCEPT/index.sqlite3" --no-fsevents --json
```

Confirm in the output:
- the `stale` project's `node_modules` appears with `"selected_by_default": true`;
- the `fresh` project's `node_modules` appears with `"selected_by_default": false`;
- no `src`, `package.json`, `pnpm-lock.yaml`, or `.git` path appears anywhere in `recommendations`.

- [ ] **Step 5: Live destructive acceptance on the same temporary tree**

```bash
STALE_ID=$(PYTHONPATH=src python3 -m mac_dev_clean deep-scan \
  --root "$ACCEPT" --index "$ACCEPT/index.sqlite3" --no-fsevents --json \
  | python3 -c "import json,sys;print(next(r['id'] for r in json.load(sys.stdin)['recommendations'] if '/stale/' in r['path']))")

PYTHONPATH=src python3 -m mac_dev_clean apply --id "$STALE_ID" --index "$ACCEPT/index.sqlite3" --dry-run --json
test -d "$ACCEPT/stale/node_modules" && echo "DRY RUN OK: still present"

PYTHONPATH=src python3 -m mac_dev_clean apply --id "$STALE_ID" --index "$ACCEPT/index.sqlite3" --json
test ! -d "$ACCEPT/stale/node_modules" && echo "REMOVED OK"
test -f "$ACCEPT/stale/package.json" && test -f "$ACCEPT/stale/pnpm-lock.yaml" && test -d "$ACCEPT/stale/.git" && echo "SOURCES INTACT"
test -d "$ACCEPT/fresh/node_modules" && echo "ACTIVE PROJECT UNTOUCHED"
rm -rf "$ACCEPT"
```
Expected: all four confirmation lines print.

- [ ] **Step 6: Live incremental acceptance against a real home directory**

Read-only. This confirms the FSEvents gap probe behaves on real data.

```bash
IDX="${TMPDIR:-/tmp}/mdc-home-index.sqlite3"
rm -f "$IDX"
time PYTHONPATH=src python3 -m mac_dev_clean deep-scan --index "$IDX" --json > /tmp/mdc-pass1.json
time PYTHONPATH=src python3 -m mac_dev_clean deep-scan --index "$IDX" --json > /tmp/mdc-pass2.json
python3 -c "
import json
a=json.load(open('/tmp/mdc-pass1.json')); b=json.load(open('/tmp/mdc-pass2.json'))
print('pass1 incremental:', a['incremental'], 'count:', a['count'])
print('pass2 incremental:', b['incremental'], 'count:', b['count'])
print('selected in pass2:', sum(1 for r in b['recommendations'] if r['selected_by_default']))
"
rm -f "$IDX" /tmp/mdc-pass1.json /tmp/mdc-pass2.json
```
Expected: pass 1 reports `incremental: False`. Pass 2 reports either `True` (continuous history) or `False` (documented safe fallback); both are correct. Spot-check that no `selected_by_default` item points at a project you worked on recently.

- [ ] **Step 7: Document the new commands**

In `README.md`, add this section immediately after the existing usage/commands section:

```markdown
### Deep Scan (project artifacts)

Deep Scan finds Git repositories anywhere under your home folder and offers the
reproducible artifacts of projects that have not changed in 90 days. Source
files, manifests, lock files, and `.git` are never deletion targets.

```sh
# Stream findings as newline-delimited JSON events
mac-dev-clean deep-scan

# Print one summary object instead
mac-dev-clean deep-scan --json

# Preview, then apply a specific recommendation
mac-dev-clean apply --id <id> --dry-run
mac-dev-clean apply --id <id>

# Discard the local index; the next deep scan rebuilds it
mac-dev-clean reset-index
```

A dependency tree is only preselected when its lock file is present, so it can
be restored exactly. Build outputs are preselected on inactivity alone. Every
recommendation is revalidated against the live filesystem before anything is
removed: if the lock file disappeared or the project was edited in the
meantime, the item is refused rather than deleted.

The index lives at `~/Library/Caches/mac-dev-clean/index.sqlite3` and is purely
a cache. It stores paths, sizes, and timestamps — never file contents — and is
never sent anywhere.
```

- [ ] **Step 8: Update the changelog**

In `CHANGELOG.md`, add a new entry at the top of the unreleased/latest section:

```markdown
### Added

- `deep-scan`, `apply`, and `reset-index` commands. Deep Scan recursively finds
  Git repositories, measures each project's last meaningful change, and offers
  reproducible build outputs and lock-file-backed dependency trees from projects
  inactive for 90 days.
- A streamed Deep Scan page in the macOS app with live progress, cancellation,
  per-item evidence, and per-item selection.
- A local SQLite index at `~/Library/Caches/mac-dev-clean/index.sqlite3` with
  FSEvents-based incremental rescanning and a safe full-walk fallback whenever
  the event history is incomplete.
```

- [ ] **Step 9: Final full verification**

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
swift test --package-path macos
./scripts/audit_public_repo.sh
git status --short
```
Expected: `OK`, Swift tests pass, audit passes, and `git status` shows only the intended files.

- [ ] **Step 10: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: document deep scan, apply, and index reset"
```

---

## Self-Review

**Spec coverage.** Phase 1's eleven acceptance criteria map to tasks as follows: recursive discovery without a developer-root heuristic → Task 4; 90-day inactivity recommendations → Tasks 5–6; sources/manifests/locks/`.git`/worktree roots never targeted → Tasks 5 (`SafetyTests`) and 9 (`test_apply_removes_the_artifact_but_keeps_the_sources`); lock file required for dependency default selection → Tasks 5–6; IDs cannot delete arbitrary or stale paths → Task 9; streamed progress and cancel in the app → Tasks 11–12; cancelled scan leaves a valid resumable index with no cleanup-eligible partial results → Tasks 3 and 8; existing Fast Scan and category cleanup unchanged → Task 10 (`LegacyContractTests`) plus the untouched `scanner.py`/`cleaner.py`; incremental rescan with a safe fallback → Tasks 2 and 8; both suites pass → Task 13; live acceptance confined to fixtures → Task 13 steps 4–6.

**Deliberate Phase 1 scope reductions,** all deferred to their own phases by the spec's delivery plan: `move_to_trash`, `invoke_tool`, and `reveal_only` action kinds are defined in the model but only `delete_tree` is executable; Downloads, applications, expanded caches, and tool-managed storage are Phases 2–5; `permission_required` is emitted by the protocol and handled by the Swift parser and `DeepScanState`, but no protected-folder request is issued yet because Phase 1 does not scan Desktop/Documents/Downloads — `~/Library` and those folders are pruned during traversal. Git worktree removal stays tool-managed and out of scope, as the spec requires.

**Type consistency.** `Recommendation.to_dict()` (Task 1) is the single source of the wire shape; `index._decode` (Task 3), `EventEmitter.candidate_found` (Task 7), and Swift's `RecommendationItem` (Task 11) all use exactly those keys. `ArtifactFact` fields are constructed in Task 5 and consumed unchanged in Tasks 6 and 9. `RECIPES` entries are keyed by `detector_id` in both `policy.RECIPE_BY_ID` (test) and `executor.RECIPES_BY_ID`. `ApplyOutcome` values in `executor.py` match the strings asserted in `test_deep_scan_cli.py` and the `succeeded` check in Swift's `ApplyResultItem`.

**One known behavioural nuance, deliberate:** `python-venv` and `python-venv-legacy` are separate recipes rather than one recipe with two paths, because `Recipe.relative_path` is a single string and the executor revalidates by reconstructing exactly `safety_root / recipe.relative_path`. Collapsing them would weaken that equality check.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-03-phase-1-foundation-and-project-analyzer.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
