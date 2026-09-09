import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mac_dev_clean.cli import main
from mac_dev_clean.journal import ActionJournal

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
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
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
        journal_path = self.home / "Library" / "Logs" / "mac-dev-clean" / "actions.jsonl"
        records = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(records[-1]["recommendation_id"], item_id)
        self.assertEqual(records[-1]["action"], "delete_tree")
        self.assertTrue(records[-1]["dry_run"])

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

    def test_apply_text_surfaces_a_safe_nonfatal_journal_warning(self):
        class BrokenJournal(ActionJournal):
            def append(self, record):
                raise RuntimeError("secret /Users/test/private path")

        _, output = self.run_cli(
            ["deep-scan", "--root", str(self.home), "--index", str(self.index_path), "--no-fsevents", "--json"]
        )
        item_id = json.loads(output)["recommendations"][0]["id"]
        with patch(
            "mac_dev_clean.cli.open_journal",
            return_value=BrokenJournal(self.home / "broken.jsonl"),
        ):
            code, apply_output = self.run_cli(
                ["apply", "--id", item_id, "--index", str(self.index_path), "--dry-run"]
            )

        self.assertEqual(code, 0)
        self.assertIn("journal write failed", apply_output)
        self.assertNotIn("private", apply_output)

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
