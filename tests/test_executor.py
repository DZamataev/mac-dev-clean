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
