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
