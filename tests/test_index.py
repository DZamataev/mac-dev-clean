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
