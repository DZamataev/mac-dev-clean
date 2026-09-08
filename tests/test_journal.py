from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from mac_dev_clean.journal import (
    ActionJournal,
    JournalRecord,
    MAX_JOURNAL_BYTES,
    open_journal,
)


def build_record(**overrides):
    defaults = dict(
        recommendation_id="abc123",
        detector_id="docker-build-cache",
        category="tool-managed",
        target="docker:build-cache",
        action="invoke_tool",
        argv=("docker", "builder", "prune", "-f"),
        reclaimable_bytes=7499000000,
        dry_run=False,
        outcome="invoked",
        detail="",
    )
    defaults.update(overrides)
    return JournalRecord(**defaults)


class JournalAppendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "logs" / "actions.jsonl"
        self.addCleanup(self.tmp.cleanup)

    def test_append_creates_parent_and_writes_one_json_line(self):
        journal = ActionJournal(self.path)

        self.assertTrue(journal.append(build_record()))

        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["outcome"], "invoked")
        self.assertEqual(payload["argv"], ["docker", "builder", "prune", "-f"])
        self.assertEqual(payload["detector_id"], "docker-build-cache")
        self.assertFalse(payload["dry_run"])

    def test_timestamp_is_utc_iso8601(self):
        journal = ActionJournal(self.path)
        journal.append(build_record())

        payload = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        parsed = datetime.fromisoformat(payload["timestamp"])
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(None))

    def test_appends_accumulate(self):
        journal = ActionJournal(self.path)
        journal.append(build_record(outcome="invoked"))
        journal.append(build_record(outcome="failed", detail="exit 1"))

        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["outcome"] for line in lines],
                         ["invoked", "failed"])

    def test_dry_run_is_recorded_distinctly(self):
        journal = ActionJournal(self.path)
        journal.append(build_record(dry_run=True, outcome="skipped"))

        payload = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["outcome"], "skipped")

    def test_refusal_is_recorded_with_its_reason(self):
        journal = ActionJournal(self.path)
        journal.append(
            build_record(outcome="changed_since_scan", detail="the project was edited recently")
        )

        payload = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(payload["outcome"], "changed_since_scan")
        self.assertEqual(payload["detail"], "the project was edited recently")

    def test_path_action_records_no_argv(self):
        journal = ActionJournal(self.path)
        journal.append(
            build_record(action="delete_tree", argv=(), target="/Users/test/app/node_modules")
        )

        payload = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(payload["argv"], [])
        self.assertEqual(payload["target"], "/Users/test/app/node_modules")

    def test_surrogate_paths_do_not_raise(self):
        journal = ActionJournal(self.path)

        self.assertTrue(journal.append(build_record(target="/Users/test/app/\udcff")))
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)


class JournalFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_unwritable_directory_returns_false_without_raising(self):
        blocked = Path(self.tmp.name) / "blocked"
        blocked.mkdir()
        os.chmod(str(blocked), 0o500)
        self.addCleanup(os.chmod, str(blocked), 0o700)

        journal = ActionJournal(blocked / "actions.jsonl")

        self.assertFalse(journal.append(build_record()))

    def test_parent_path_is_a_file_returns_false(self):
        clash = Path(self.tmp.name) / "clash"
        clash.write_text("not a directory", encoding="utf-8")

        journal = ActionJournal(clash / "actions.jsonl")

        self.assertFalse(journal.append(build_record()))

    def test_last_warning_is_set_when_append_fails(self):
        blocked = Path(self.tmp.name) / "blocked2"
        blocked.mkdir()
        os.chmod(str(blocked), 0o500)
        self.addCleanup(os.chmod, str(blocked), 0o700)

        journal = ActionJournal(blocked / "actions.jsonl")
        self.assertIsNone(journal.last_warning)

        self.assertFalse(journal.append(build_record()))

        self.assertIsNotNone(journal.last_warning)
        self.assertIsInstance(journal.last_warning, str)
        self.assertLessEqual(len(journal.last_warning), 200)
        # Non-sensitive: must not leak the full absolute path.
        self.assertNotIn(str(blocked), journal.last_warning)

    def test_non_serializable_field_returns_false_without_raising(self):
        class Unserializable:
            pass

        journal = ActionJournal(self.tmp_journal_path())
        record = build_record(detail=Unserializable())

        self.assertFalse(journal.append(record))

        self.assertIsNotNone(journal.last_warning)
        self.assertIsInstance(journal.last_warning, str)
        self.assertLessEqual(len(journal.last_warning), 200)
        self.assertNotIn(self.tmp.name, journal.last_warning)

    def tmp_journal_path(self):
        return Path(self.tmp.name) / "actions.jsonl"

    def test_last_warning_clears_after_a_later_successful_append(self):
        blocked = Path(self.tmp.name) / "blocked3"
        blocked.mkdir()
        os.chmod(str(blocked), 0o500)
        self.addCleanup(os.chmod, str(blocked), 0o700)

        target = blocked / "actions.jsonl"
        journal = ActionJournal(target)
        self.assertFalse(journal.append(build_record()))
        self.assertIsNotNone(journal.last_warning)

        os.chmod(str(blocked), 0o700)
        self.assertTrue(journal.append(build_record()))

        self.assertIsNone(journal.last_warning)


class JournalRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "actions.jsonl"
        self.addCleanup(self.tmp.cleanup)

    def test_rotates_when_the_file_exceeds_the_limit(self):
        self.path.write_text("x" * (MAX_JOURNAL_BYTES + 1), encoding="utf-8")
        journal = ActionJournal(self.path)

        journal.append(build_record())

        rotated = self.path.with_suffix(".jsonl.1")
        self.assertTrue(rotated.exists())
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)

    def test_rotation_replaces_an_older_rotated_file(self):
        rotated = self.path.with_suffix(".jsonl.1")
        rotated.write_text("older\n", encoding="utf-8")
        self.path.write_text("x" * (MAX_JOURNAL_BYTES + 1), encoding="utf-8")
        journal = ActionJournal(self.path)

        journal.append(build_record())

        self.assertNotIn("older", rotated.read_text(encoding="utf-8"))

    def test_small_file_is_not_rotated(self):
        self.path.write_text("small\n", encoding="utf-8")
        journal = ActionJournal(self.path)

        journal.append(build_record())

        self.assertFalse(self.path.with_suffix(".jsonl.1").exists())

    def test_append_succeeds_but_retains_warning_when_rotation_fails(self):
        self.path.write_text("x" * (MAX_JOURNAL_BYTES + 1), encoding="utf-8")
        journal = ActionJournal(self.path)

        with mock.patch(
            "mac_dev_clean.journal.os.replace",
            side_effect=OSError(13, "Permission denied"),
        ):
            self.assertTrue(journal.append(build_record()))

        self.assertIsNotNone(journal.last_warning)
        self.assertIsInstance(journal.last_warning, str)
        self.assertLessEqual(len(journal.last_warning), 200)
        # Non-sensitive: must not leak the full absolute path.
        self.assertNotIn(str(self.path), journal.last_warning)


class OpenJournalTests(unittest.TestCase):
    def test_open_journal_honours_an_explicit_path(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name) / "custom.jsonl"

        journal = open_journal(target)

        self.assertEqual(journal.path, target)

    def test_open_journal_default_is_under_library_logs(self):
        journal = open_journal()

        self.assertEqual(journal.path.name, "actions.jsonl")
        self.assertIn("mac-dev-clean", str(journal.path))


if __name__ == "__main__":
    unittest.main()
