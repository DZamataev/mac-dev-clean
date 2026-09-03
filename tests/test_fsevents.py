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
