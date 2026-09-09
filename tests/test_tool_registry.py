from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mac_dev_clean.tools.registry import binary_runner, collect_tool_recommendations
from mac_dev_clean.tools.runner import MAX_STDERR_CHARS, ToolResult, ToolUnavailable
from mac_dev_clean.tools.simulator import (
    SIMCTL_DEVICES_PREVIEW_ARGV,
    SIMCTL_RUNTIMES_PREVIEW_ARGV,
)


HOME = Path("/Users/test/home")
DOCKER_OUTPUT = (
    '{"Active":"0","Reclaimable":"7.499GB","Size":"18.92GB",'
    '"TotalCount":"144","Type":"Build Cache"}\n'
)
BREW_OUTPUT = "==> This operation would free approximately 799.8MB of disk space.\n"
SIMULATOR_DEVICES = """{"devices":{"runtime":[
  {"name":"old phone","udid":"AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
   "state":"Shutdown","isAvailable":false,"dataPathSize":99}
]}}"""
SIMULATOR_RUNTIMES = '{"runtimes":[]}'


def fixed(stdout: str, exit_code: int = 0, stderr: str = ""):
    def run(argv):
        return ToolResult(
            argv=tuple(argv), stdout=stdout, stderr=stderr, exit_code=exit_code
        )

    return run


def unavailable(reason: str):
    def run(argv):
        raise ToolUnavailable(str(argv[0]) if argv else "tool", reason)

    return run


def simulator_fixed(calls=None):
    def run(argv):
        vector = tuple(argv)
        if calls is not None:
            calls.append(vector)
        if vector == SIMCTL_DEVICES_PREVIEW_ARGV:
            stdout = SIMULATOR_DEVICES
        elif vector == SIMCTL_RUNTIMES_PREVIEW_ARGV:
            stdout = SIMULATOR_RUNTIMES
        else:
            raise AssertionError("unexpected simulator vector: {!r}".format(vector))
        return ToolResult(vector, stdout, "", 0)

    return run


class BinaryRunnerTests(unittest.TestCase):
    def test_resolves_declared_name_in_fallback_order_and_preserves_tail(self):
        captured = []

        def fake_find(name, extra_dirs=()):
            captured.append((name, tuple(extra_dirs)))
            return "/resolved/bin/widget"

        def fake_run(argv):
            captured.append(tuple(argv))
            return ToolResult(tuple(argv), "ok", "", 0)

        with patch("mac_dev_clean.tools.registry.find_binary", fake_find), patch(
            "mac_dev_clean.tools.registry.run_tool", fake_run
        ):
            result = binary_runner("widget", (Path("/caller/bin"),))(
                ("widget", "literal;not-shell", "$HOME", "")
            )

        self.assertEqual(
            captured,
            [
                (
                    "widget",
                    (
                        Path("/caller/bin"),
                        Path("/opt/homebrew/bin"),
                        Path("/usr/local/bin"),
                    ),
                ),
                ("/resolved/bin/widget", "literal;not-shell", "$HOME", ""),
            ],
        )
        self.assertEqual(
            result.argv,
            ("/resolved/bin/widget", "literal;not-shell", "$HOME", ""),
        )

    def test_explicit_xcrun_is_resolved_directly(self):
        captured = []

        def fake_find(name, extra_dirs=()):
            captured.append((name, tuple(extra_dirs)))
            return name

        def fake_run(argv):
            return ToolResult(tuple(argv), "", "", 0)

        with patch("mac_dev_clean.tools.registry.find_binary", fake_find), patch(
            "mac_dev_clean.tools.registry.run_tool", fake_run
        ):
            binary_runner("/usr/bin/xcrun")(
                ("/usr/bin/xcrun", "simctl", "list", "--json", "devices")
            )

        self.assertEqual(
            captured,
            [
                (
                    "/usr/bin/xcrun",
                    (Path("/opt/homebrew/bin"), Path("/usr/local/bin")),
                )
            ],
        )

    def test_strict_resolution_never_falls_through_to_path_or_standard_dirs(self):
        with tempfile.TemporaryDirectory() as temp:
            reviewed_bin = Path(temp) / "reviewed" / "bin"
            reviewed_bin.mkdir(parents=True)
            executable = reviewed_bin / "sdkmanager"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            captured = []

            def fake_run(argv):
                captured.append(tuple(argv))
                return ToolResult(tuple(argv), "ok", "", 0)

            with patch("mac_dev_clean.tools.registry.find_binary") as find, patch(
                "mac_dev_clean.tools.registry.run_tool", fake_run
            ):
                binary_runner(
                    "sdkmanager", (reviewed_bin,), allow_fallback=False
                )(("sdkmanager", "--list_installed"))

            find.assert_not_called()
            self.assertEqual(
                captured, [(str(executable), "--list_installed")]
            )

    def test_missing_declared_binary_raises_unavailable(self):
        with patch("mac_dev_clean.tools.registry.find_binary", return_value=None):
            with self.assertRaises(ToolUnavailable) as raised:
                binary_runner("widget")(("widget", "--version"))

        self.assertEqual(raised.exception.tool, "widget")
        self.assertEqual(raised.exception.reason, "not installed")

    def test_wrong_executable_is_rejected_before_resolution(self):
        with patch("mac_dev_clean.tools.registry.find_binary") as find:
            with self.assertRaises(ValueError):
                binary_runner("docker")(("podman", "system", "df"))

        find.assert_not_called()

    def test_malformed_vectors_are_rejected_before_resolution(self):
        cases = (
            "widget --version",
            b"widget --version",
            7,
            {"widget", "--version"},
            iter(("widget", "--version")),
            (),
            (7, "--version"),
            ("widget", "bad\x00tail"),
            ("widget\x00suffix",),
        )
        for argv in cases:
            with self.subTest(argv=argv), patch(
                "mac_dev_clean.tools.registry.find_binary"
            ) as find:
                with self.assertRaises((TypeError, ValueError)):
                    binary_runner("widget")(argv)
                find.assert_not_called()


class CollectToolRecommendationsTests(unittest.TestCase):
    def base_runners(self, simulator=None):
        return {
            "docker": fixed(DOCKER_OUTPUT),
            "brew": fixed(BREW_OUTPUT),
            "sdkmanager": unavailable("not installed"),
            "avdmanager": unavailable("not installed"),
            "simctl": simulator or simulator_fixed(),
        }

    def test_collects_each_tool_and_returns_one_deterministic_status(self):
        calls = []

        report = collect_tool_recommendations(
            generation=5,
            home=HOME,
            env={},
            runners=self.base_runners(simulator_fixed(calls)),
        )

        self.assertEqual(
            [status.tool for status in report.statuses],
            ["docker", "homebrew", "android", "simulator"],
        )
        self.assertEqual(len(report.statuses), 4)
        self.assertEqual(
            calls, [SIMCTL_DEVICES_PREVIEW_ARGV, SIMCTL_RUNTIMES_PREVIEW_ARGV]
        )
        detectors = {item.detector_id for item in report.recommendations}
        self.assertIn("docker-build-cache", detectors)
        self.assertIn("homebrew-cleanup", detectors)
        self.assertIn("simulator-unavailable-device", detectors)

    def test_one_unavailable_or_malformed_tool_does_not_suppress_others(self):
        runners = self.base_runners()

        def malformed(argv):
            raise ValueError("malformed docker inventory")

        runners["docker"] = malformed
        report = collect_tool_recommendations(
            generation=1, home=HOME, env={}, runners=runners
        )

        docker = report.statuses[0]
        self.assertFalse(docker.available)
        self.assertIn("malformed", docker.reason)
        self.assertTrue(
            any(item.detector_id == "homebrew-cleanup" for item in report.recommendations)
        )
        self.assertTrue(report.statuses[3].available)

    def test_simulator_adapter_rejects_provenance_and_bounds_stdout(self):
        cases = (
            (
                lambda argv: ToolResult(
                    ("/wrong/xcrun",) + tuple(argv[1:]), "{}", "", 0
                ),
                "provenance",
            ),
            (
                lambda argv: ToolResult("not an argv", "{}", "", 0),
                "provenance",
            ),
            (
                lambda argv: ToolResult(
                    tuple(argv), "o" * (MAX_STDERR_CHARS + 50), "", 9
                ),
                "...[truncated]",
            ),
        )
        for simulator, expected in cases:
            with self.subTest(expected=expected):
                report = collect_tool_recommendations(
                    generation=1,
                    home=HOME,
                    env={},
                    runners=self.base_runners(simulator),
                )

                status = report.statuses[3]
                self.assertFalse(status.available)
                self.assertLessEqual(len(status.reason), MAX_STDERR_CHARS)
                self.assertIn(expected, status.reason)
                self.assertFalse(
                    any(
                        item.detector_id.startswith("simulator-")
                        for item in report.recommendations
                    )
                )

    def test_combined_android_status_is_bounded(self):
        with tempfile.TemporaryDirectory() as raw:
            sdk_root = Path(raw) / "sdk"
            sdk_root.mkdir()
            runners = self.base_runners()
            runners["sdkmanager"] = unavailable("s" * MAX_STDERR_CHARS)
            runners["avdmanager"] = unavailable("a" * MAX_STDERR_CHARS)

            report = collect_tool_recommendations(
                generation=1,
                home=HOME,
                env={"ANDROID_HOME": str(sdk_root)},
                runners=runners,
            )

        android = report.statuses[2]
        self.assertFalse(android.available)
        self.assertEqual(len(android.reason), MAX_STDERR_CHARS)
        self.assertTrue(android.reason.endswith("...[truncated]"))


if __name__ == "__main__":
    unittest.main()
