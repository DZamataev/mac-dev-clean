from __future__ import annotations

import unittest
from pathlib import Path

from mac_dev_clean.recommendation import ActionKind
from mac_dev_clean.sim_prune import SimctlError
from mac_dev_clean.tools.runner import MAX_STDERR_CHARS, ToolUnavailable
from mac_dev_clean.tools.simulator import XCRUN, analyze_simulator


HOME = Path("/Users/test/home")
DEVICE_UDID = "22222222-2222-2222-2222-222222222222"

DEVICES_JSON = """{
  "devices": {
    "com.apple.CoreSimulator.SimRuntime.iOS-17-0": [
      {"name": "iPhone 15", "udid": "11111111-1111-1111-1111-111111111111",
       "state": "Shutdown", "isAvailable": true,
       "dataPathSize": 4096, "logPathSize": 1024},
      {"name": "iPhone 12", "udid": "22222222-2222-2222-2222-222222222222",
       "state": "Shutdown", "isAvailable": false,
       "dataPathSize": 8192, "logPathSize": 2048},
      {"name": "iPhone 14", "udid": "33333333-3333-3333-3333-333333333333",
       "state": "Booted", "isAvailable": false,
       "dataPathSize": 16384, "logPathSize": 4096}
    ]
  }
}"""

RUNTIMES_JSON = '{"runtimes": []}'

RUNTIMES_WITH_CANDIDATES_JSON = """{
  "runtimes": [
    {"identifier": "runtime-z", "runtimeIdentifier": "com.apple.runtime.z",
     "name": "iOS 17.0", "version": "17.0", "build": "21A123",
     "platformIdentifier": "com.apple.platform.iphonesimulator",
     "state": "Ready", "deletable": true,
     "lastUsedAt": "2025-01-02T03:04:05Z", "sizeBytes": 3000,
     "path": "/Library/Developer/CoreSimulator/Profiles/Runtimes/iOS 17.simruntime"},
    {"identifier": "runtime-not-deletable", "name": "iOS 18.0",
     "deletable": false, "sizeBytes": 4000,
     "path": "/Library/Developer/CoreSimulator/Profiles/Runtimes/iOS 18.simruntime"}
  ]
}"""


def simctl_runner(devices: str = DEVICES_JSON, runtimes: str = RUNTIMES_JSON):
    def run(args):
        if "runtime" in args:
            return runtimes
        return devices

    return run


class AnalyzeSimulatorDeviceTests(unittest.TestCase):
    def test_only_positive_sized_unavailable_shutdown_devices_are_offered(self):
        devices = DEVICES_JSON.replace(
            '"dataPathSize": 4096, "logPathSize": 1024',
            '"dataPathSize": 0, "logPathSize": 0',
        )

        items, unavailable = analyze_simulator(
            simctl_runner(devices=devices), generation=7, home=HOME
        )

        self.assertIsNone(unavailable)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.detector_id, "simulator-unavailable-device")
        self.assertIn("iPhone 12", item.label)
        self.assertEqual(
            item.path,
            HOME / "Library/Developer/CoreSimulator/Devices" / DEVICE_UDID,
        )
        self.assertEqual(
            item.safety_root, HOME / "Library/Developer/CoreSimulator/Devices"
        )
        self.assertEqual(item.reclaimable_bytes, 8192 + 2048)
        self.assertTrue(
            any(
                evidence.code == "tool-size"
                and evidence.detail == "simctl reports 10240 bytes"
                for evidence in item.evidence
            )
        )
        self.assertEqual(item.generation, 7)
        self.assertIs(item.action, ActionKind.INVOKE_TOOL)
        self.assertFalse(item.selected_by_default)
        self.assertEqual(
            item.tool_action.argv,
            (XCRUN, "simctl", "delete", DEVICE_UDID),
        )
        self.assertEqual(
            item.tool_action.preview_argv,
            (XCRUN, "simctl", "list", "--json", "devices"),
        )

    def test_malformed_udids_are_never_offered(self):
        devices = DEVICES_JSON.replace(DEVICE_UDID, "not-a-udid")

        items, unavailable = analyze_simulator(
            simctl_runner(devices=devices), generation=1, home=HOME
        )

        self.assertIsNone(unavailable)
        self.assertEqual(items, [])


class AnalyzeSimulatorRuntimeTests(unittest.TestCase):
    def test_deletable_positive_runtime_has_a_separate_owner_action(self):
        items, unavailable = analyze_simulator(
            simctl_runner(runtimes=RUNTIMES_WITH_CANDIDATES_JSON),
            generation=3,
            home=HOME,
        )

        self.assertIsNone(unavailable)
        runtime = next(
            item for item in items if item.detector_id == "simulator-runtime"
        )
        self.assertEqual(
            runtime.path,
            Path(
                "/Library/Developer/CoreSimulator/Profiles/Runtimes/"
                "iOS 17.simruntime"
            ),
        )
        self.assertEqual(runtime.reclaimable_bytes, 3000)
        self.assertEqual(
            runtime.last_activity_at.isoformat(), "2025-01-02T03:04:05+00:00"
        )
        self.assertIs(runtime.action, ActionKind.INVOKE_TOOL)
        self.assertFalse(runtime.selected_by_default)
        self.assertEqual(
            runtime.tool_action.argv,
            (XCRUN, "simctl", "runtime", "delete", "runtime-z"),
        )
        self.assertEqual(
            runtime.tool_action.preview_argv,
            (XCRUN, "simctl", "runtime", "list", "-j"),
        )
        self.assertEqual(runtime.tool_action.reported, "3000 bytes")

    def test_runtime_must_be_deletable_with_safe_identity_size_and_absolute_path(self):
        unsafe = """{"runtimes": [
          {"identifier":"ok-not-deletable","deletable":false,"sizeBytes":1,"path":"/a"},
          {"identifier":"zero","deletable":true,"sizeBytes":0,"path":"/b"},
          {"identifier":"","deletable":true,"sizeBytes":1,"path":"/c"},
          {"identifier":"bad\\u0000id","deletable":true,"sizeBytes":1,"path":"/d"},
          {"identifier":"-option","deletable":true,"sizeBytes":1,"path":"/e"},
          {"identifier":"relative","deletable":true,"sizeBytes":1,"path":"relative/path"},
          {"identifier":"empty-path","deletable":true,"sizeBytes":1,"path":""}
        ]}"""

        items, unavailable = analyze_simulator(
            simctl_runner(devices='{"devices": {}}', runtimes=unsafe),
            generation=1,
            home=HOME,
        )

        self.assertIsNone(unavailable)
        self.assertEqual(items, [])


class AnalyzeSimulatorAmbiguityAndErrorTests(unittest.TestCase):
    def assert_bounded_reason(self, reason):
        self.assertIsNotNone(reason)
        self.assertEqual(len(reason), MAX_STDERR_CHARS)
        self.assertTrue(reason.endswith("...[truncated]"))

    def test_duplicate_identities_and_path_collisions_are_omitted(self):
        devices = """{"devices":{"runtime":[
          {"name":"duplicate one","udid":"AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA","state":"Shutdown","isAvailable":false,"dataPathSize":1},
          {"name":"duplicate two","udid":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","state":"Shutdown","isAvailable":false,"dataPathSize":2},
          {"name":"independent","udid":"BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB","state":"Shutdown","isAvailable":false,"dataPathSize":3}
        ]}}"""
        runtimes = """{"runtimes":[
          {"identifier":"duplicate","name":"duplicate one","deletable":true,"sizeBytes":4,"path":"/r/a"},
          {"identifier":"duplicate","name":"duplicate two","deletable":true,"sizeBytes":5,"path":"/r/b"},
          {"identifier":"collision-one","name":"collision one","deletable":true,"sizeBytes":6,"path":"/r/c/../same"},
          {"identifier":"collision-two","name":"collision two","deletable":true,"sizeBytes":7,"path":"/r/same"},
          {"identifier":"independent-runtime","name":"independent runtime","deletable":true,"sizeBytes":8,"path":"/r/independent"}
        ]}"""

        items, unavailable = analyze_simulator(
            simctl_runner(devices=devices, runtimes=runtimes),
            generation=1,
            home=HOME,
        )

        self.assertIsNone(unavailable)
        self.assertEqual(
            [item.tool_action.resource for item in items],
            ["BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", "independent-runtime"],
        )

    def test_results_are_stably_ordered_by_class_then_resource_identity(self):
        devices = """{"devices":{"runtime":[
          {"name":"z","udid":"FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF","state":"Shutdown","isAvailable":false,"dataPathSize":1},
          {"name":"a","udid":"11111111-1111-1111-1111-111111111111","state":"Shutdown","isAvailable":false,"dataPathSize":9}
        ]}}"""
        runtimes = """{"runtimes":[
          {"identifier":"z-runtime","name":"z","deletable":true,"sizeBytes":1,"path":"/z"},
          {"identifier":"a-runtime","name":"a","deletable":true,"sizeBytes":9,"path":"/a"}
        ]}"""

        items, _ = analyze_simulator(
            simctl_runner(devices=devices, runtimes=runtimes),
            generation=1,
            home=HOME,
        )

        self.assertEqual(
            [item.tool_action.resource for item in items],
            [
                "11111111-1111-1111-1111-111111111111",
                "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF",
                "a-runtime",
                "z-runtime",
            ],
        )

    def test_simctl_and_unavailable_reasons_are_bounded(self):
        for exception in (
            SimctlError("s" * (MAX_STDERR_CHARS + 1)),
            ToolUnavailable("simctl", "u" * (MAX_STDERR_CHARS + 1)),
        ):
            with self.subTest(exception=type(exception).__name__):
                def failing(args, error=exception):
                    raise error

                items, reason = analyze_simulator(failing, generation=1, home=HOME)

                self.assertEqual(items, [])
                self.assert_bounded_reason(reason)

    def test_malformed_inventory_is_reported_and_bounded(self):
        malformed = "{" + ("m" * (MAX_STDERR_CHARS + 100))

        items, reason = analyze_simulator(
            simctl_runner(devices=malformed), generation=1, home=HOME
        )

        self.assertEqual(items, [])
        self.assertIsNotNone(reason)
        self.assertLessEqual(len(reason), MAX_STDERR_CHARS)

    def test_overflowing_inventory_record_is_omitted_independently(self):
        devices = """{"devices":{"runtime":[
          {"name":"overflow","udid":"AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
           "state":"Shutdown","isAvailable":false,"dataPathSize":1e400}
        ]}}"""

        items, reason = analyze_simulator(
            simctl_runner(devices=devices), generation=1, home=HOME
        )

        self.assertEqual(items, [])
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
