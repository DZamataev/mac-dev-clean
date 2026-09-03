import os
import shutil
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

    def test_a_hand_edited_file_inside_the_artifact_blocks_the_deletion(self):
        # The ship-blocking data-loss defect: a file hand-edited today
        # *inside* node_modules (a patch-package fix, a vendored local fork)
        # must survive `apply` even though the project source around it is
        # 400 days old and the stale index entry says REMOVE. The index is
        # only ever a hint; the live filesystem is authoritative.
        touch(self.artifact / "leftpad" / "hand_patched.js", NOW)

        results = apply_recommendations([self.item.id], self.index, now=NOW)

        self.assertEqual(results[0].outcome, ApplyOutcome.CHANGED_SINCE_SCAN)
        self.assertTrue(self.artifact.exists())
        self.assertTrue((self.artifact / "leftpad" / "hand_patched.js").exists())

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

    def test_an_artifact_reached_through_an_ancestor_symlink_is_refused(self):
        # The executor's own guard, isolated: the project is honest at scan time
        # (real ios/build inside it), so a valid recommendation ID exists. Only
        # AFTERWARDS is the ancestor `ios` swapped for a symlink to outside
        # storage. The leaf `build` stays a genuine directory, so `islink` on the
        # target sees nothing wrong -- only the realpath containment check does.
        home = self.home
        project = home / "ios-app"
        (project / ".git").mkdir(parents=True)
        touch(project / ".git" / "HEAD", OLD)
        touch(project / "src" / "main.swift", OLD)
        touch(project / "ios" / "App.xcodeproj" / "project.pbxproj", OLD, b"{}")
        touch(project / "ios" / "build" / "Release" / "app.bin", OLD, BIG)

        index = open_index(home / "ios-index.sqlite3")
        try:
            result = deep_scan([home], index, now=NOW, use_fsevents=False)
            item = next(
                i for i in result.recommendations if i.detector_id == "ios-build"
            )

            # Swap the ancestor for a symlink to data that lives outside.
            outside = home / "external_storage"
            touch(outside / "App.xcodeproj" / "project.pbxproj", OLD, b"{}")
            precious = outside / "build" / "Release" / "keepme.bin"
            touch(precious, OLD, BIG)
            shutil.rmtree(str(project / "ios"))
            os.symlink(str(outside), str(project / "ios"))

            outcome = apply_recommendations([item.id], index, now=NOW)[0].outcome
        finally:
            index.close()

        self.assertEqual(outcome, ApplyOutcome.FAILED)
        self.assertTrue(precious.exists(), "data outside the project must survive")

    def test_a_linked_package_inside_the_artifact_loses_only_its_link(self):
        # `npm link` / `pip install -e` leave a symlink INSIDE the artifact that
        # points at a real sibling checkout. Removing the artifact must unlink it,
        # never follow it: the sibling's sources are somebody's actual work.
        sibling = self.home / "other-project"
        touch(sibling / "src" / "lib.ts", OLD, b"REAL SOURCE")
        link = self.artifact / "linked-pkg"
        os.symlink(str(sibling), str(link))
        os.utime(str(link), (OLD.timestamp(), OLD.timestamp()), follow_symlinks=False)

        results = apply_recommendations([self.item.id], self.index, now=NOW)

        self.assertEqual(results[0].outcome, ApplyOutcome.REMOVED)
        self.assertFalse(self.artifact.exists())
        self.assertTrue(
            (sibling / "src" / "lib.ts").exists(),
            "a linked-in project must survive removal of the artifact",
        )
        self.assertEqual((sibling / "src" / "lib.ts").read_bytes(), b"REAL SOURCE")

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
