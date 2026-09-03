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

    def test_a_denied_protected_folder_emits_permission_required(self):
        # macOS TCC lets `stat`/`is_dir` succeed on Desktop/Documents/
        # Downloads without Full Disk Access, but denies listing their
        # contents. Without a dedicated probe, that folder's repositories
        # just vanish from the results with no explanation. `chmod 0o000`
        # reproduces the same PermissionError-on-scandir shape locally.
        build_stale_project(self.home / "app")
        documents = self.home / "Documents"
        documents.mkdir()
        os.chmod(str(documents), 0o000)
        stream = io.StringIO()
        try:
            deep_scan(
                [self.home],
                self.index,
                emitter=EventEmitter(stream, generation=1),
                now=NOW,
                use_fsevents=False,
            )
        finally:
            os.chmod(str(documents), 0o755)

        events = [json.loads(line) for line in stream.getvalue().splitlines() if line]
        permission_events = [e for e in events if e["event"] == "permission_required"]
        self.assertEqual(len(permission_events), 1)
        self.assertEqual(permission_events[0]["folder"], "Documents")

    def test_an_ordinary_folder_never_emits_permission_required(self):
        # No-regression: a folder that is not Desktop/Documents/Downloads,
        # and one of those three folders that is perfectly readable, must
        # never trigger the event.
        build_stale_project(self.home / "app")
        (self.home / "Desktop").mkdir()
        (self.home / "Documents").mkdir()
        (self.home / "Downloads").mkdir()
        stream = io.StringIO()

        deep_scan(
            [self.home],
            self.index,
            emitter=EventEmitter(stream, generation=1),
            now=NOW,
            use_fsevents=False,
        )

        events = [json.loads(line) for line in stream.getvalue().splitlines() if line]
        self.assertEqual([e for e in events if e["event"] == "permission_required"], [])


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
