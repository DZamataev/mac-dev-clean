from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mac_dev_clean.recommendation import ActionKind, Confidence, RestorationCost
from mac_dev_clean.scanner import path_size
from mac_dev_clean.tools.android import (
    Avd,
    SdkPackage,
    analyze_android,
    cmdline_tool_dirs,
    find_sdk_root,
    parse_avds,
    parse_sdk_packages,
)
from mac_dev_clean.tools.runner import MAX_STDERR_CHARS, ToolResult, ToolUnavailable

SDK_OUTPUT = """Loading package information...
[=========] 25% Loading local repository...
Installed packages:
  Path                          | Version | Description                    | Location
  -------                       | ------- | -------                        | -------
  build-tools;35.0.0            | 35.0.0  | Android SDK Build-Tools 35     | build-tools/35.0.0
  cmake;3.31.6                  | 3.31.6  | CMake 3.31.6                   | cmake/3.31.6
  ndk;27.0.12077973             | 27.0.1  | NDK (Side by side) 27.0.12077  | ndk/27.0.12077973
  system-images;android-34;x    | 1       | Google APIs ARM 64 Image       | system-images/android-34/x
  emulator                      | 36.2.12 | Android Emulator               | emulator
Available Packages:
  Path                          | Version | Description
  platforms;android-99          | 1       | Not installed
"""

AVD_OUTPUT = """Available Android Virtual Devices:
    Name: Pixel_2
  Device: pixel_2 (Google)
    Path: /Users/test/home/.android/avd/Pixel_2.avd
  Target: Google APIs (Google Inc.)
          Based on: Android 8.1 ("Oreo") Tag/ABI: google_apis/arm64-v8a
    Skin: pixel_2
  Sdcard: 512M

The following Android Virtual Devices could not be loaded:
    Name: Pixel_9
    Path: /Users/test/home/.android/avd/Pixel_9.avd
   Error: Google pixel_9 no longer exists as a device
"""


class ParseSdkPackagesTests(unittest.TestCase):
    def test_empty_output_yields_nothing(self):
        self.assertEqual(parse_sdk_packages(""), [])

    def test_reads_only_complete_rows_in_the_installed_section(self):
        packages = parse_sdk_packages(SDK_OUTPUT)

        self.assertEqual(
            [(item.path, item.version, item.location) for item in packages],
            [
                ("build-tools;35.0.0", "35.0.0", "build-tools/35.0.0"),
                ("cmake;3.31.6", "3.31.6", "cmake/3.31.6"),
                ("ndk;27.0.12077973", "27.0.1", "ndk/27.0.12077973"),
                ("system-images;android-34;x", "1", "system-images/android-34/x"),
                ("emulator", "36.2.12", "emulator"),
            ],
        )

    def test_skips_malformed_and_empty_rows_without_an_installed_header_leak(self):
        output = """ndk;before | 1 | before | ndk/before
Installed packages:
Path | Version | Description | Location
ndk;three | 1 | only three
ndk;five | 1 | too | many | columns
ndk;empty | 1 | description |
ndk;valid | 2 | Valid package | ndk/valid
"""

        self.assertEqual(
            parse_sdk_packages(output),
            [SdkPackage("ndk;valid", "2", "Valid package", "ndk/valid")],
        )

    def test_output_without_installed_header_yields_nothing(self):
        self.assertEqual(
            parse_sdk_packages("ndk;x | 1 | package | ndk/x\n"), []
        )


class ParseAvdsTests(unittest.TestCase):
    def test_reads_loadable_and_unloadable_sections(self):
        loadable, unloadable = parse_avds(AVD_OUTPUT)

        self.assertEqual(
            loadable,
            [
                Avd(
                    name="Pixel_2",
                    path="/Users/test/home/.android/avd/Pixel_2.avd",
                    target="Google APIs (Google Inc.)",
                    error="",
                )
            ],
        )
        self.assertEqual(
            unloadable,
            [
                Avd(
                    name="Pixel_9",
                    path="/Users/test/home/.android/avd/Pixel_9.avd",
                    target="",
                    error="Google pixel_9 no longer exists as a device",
                )
            ],
        )

    def test_missing_fields_remain_empty_and_headerless_records_are_ignored(self):
        output = """Name: BeforeHeader
Path: /tmp/BeforeHeader.avd
Available Android Virtual Devices:
Name: NoPath
Target: Android 35
Name:
Path: /tmp/NoName.avd
The following Android Virtual Devices could not be loaded:
Name: BrokenNoError
Path: /tmp/BrokenNoError.avd
"""

        self.assertEqual(
            parse_avds(output),
            (
                [Avd("NoPath", "", "Android 35", "")],
                [Avd("BrokenNoError", "/tmp/BrokenNoError.avd", "", "")],
            ),
        )

    def test_empty_output_yields_two_empty_lists(self):
        self.assertEqual(parse_avds(""), ([], []))


class FindSdkRootTests(unittest.TestCase):
    def test_uses_first_existing_candidate_in_declared_precedence(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            home = Path(raw_tmp)
            android_home = home / "android-home"
            sdk_root = home / "sdk-root"
            default = home / "Library" / "Android" / "sdk"
            for candidate in (android_home, sdk_root, default):
                candidate.mkdir(parents=True)

            self.assertEqual(
                find_sdk_root(
                    {
                        "ANDROID_HOME": str(android_home),
                        "ANDROID_SDK_ROOT": str(sdk_root),
                    },
                    home,
                ),
                android_home,
            )

    def test_malformed_and_stale_environment_paths_do_not_hide_valid_fallbacks(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            home = Path(raw_tmp)
            sdk_root = home / "sdk-root"
            sdk_root.mkdir()

            self.assertEqual(
                find_sdk_root(
                    {
                        "ANDROID_HOME": "bad\x00path",
                        "ANDROID_SDK_ROOT": str(sdk_root),
                    },
                    home,
                ),
                sdk_root,
            )

    def test_stale_variables_fall_through_to_default_and_absence_returns_none(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            home = Path(raw_tmp)
            default = home / "Library" / "Android" / "sdk"
            default.mkdir(parents=True)
            env = {
                "ANDROID_HOME": str(home / "missing-home"),
                "ANDROID_SDK_ROOT": str(home / "missing-root"),
            }

            self.assertEqual(find_sdk_root(env, home), default)
            default.rmdir()
            self.assertIsNone(find_sdk_root(env, home))


class AnalyzeAndroidTests(unittest.TestCase):
    def test_missing_sdk_root_returns_unavailable_without_inventory_calls(self):
        def runner(argv):
            self.fail("inventory must not run without an SDK root")

        items, unavailable = analyze_android(runner, runner, 1, None)

        self.assertEqual(items, [])
        self.assertEqual(unavailable, "Android SDK not found")

    def test_calls_exact_inventory_vectors_once_each(self):
        calls = []

        def runner(argv):
            calls.append(tuple(argv))
            return ToolResult(tuple(argv), "", "", 0)

        with tempfile.TemporaryDirectory() as raw_tmp:
            items, unavailable = analyze_android(
                runner, runner, generation=7, sdk_root=Path(raw_tmp)
            )

        self.assertEqual(
            calls,
            [("sdkmanager", "--list_installed"), ("avdmanager", "list", "avd")],
        )
        self.assertEqual(items, [])
        self.assertIsNone(unavailable)

    def test_sdk_recommendation_has_exact_metadata_and_action_identity(self):
        sdk_output = """Installed packages:
Path | Version | Description | Location
ndk;27.0.12077973 | 27.0.1 | NDK side by side | ndk/27.0.12077973
"""

        def sdk_runner(argv):
            return ToolResult(tuple(argv), sdk_output, "", 0)

        def avd_runner(argv):
            return ToolResult(tuple(argv), "", "", 0)

        with tempfile.TemporaryDirectory() as raw_tmp:
            sdk = Path(raw_tmp)
            location = sdk / "ndk" / "27.0.12077973"
            location.mkdir(parents=True)
            (location / "blob").write_bytes(b"x" * 4096)
            items, unavailable = analyze_android(sdk_runner, avd_runner, 11, sdk)

            self.assertIsNone(unavailable)
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item.detector_id, "android-ndk")
            self.assertEqual(item.category, "tool-managed")
            self.assertEqual(item.label, "Android NDK 27.0.1")
            self.assertEqual(item.path, location.resolve())
            self.assertIs(item.action, ActionKind.INVOKE_TOOL)
            self.assertEqual(item.allocated_bytes, path_size(location))
            self.assertEqual(item.reclaimable_bytes, path_size(location))
            self.assertIs(item.confidence, Confidence.STRONG)
            self.assertIs(item.restoration, RestorationCost.REDOWNLOAD)
            self.assertFalse(item.selected_by_default)
            self.assertEqual(item.safety_root, sdk.resolve())
            self.assertEqual(item.generation, 11)
            self.assertIsNone(item.last_activity_at)
            self.assertIn("project", item.reason.lower())
            self.assertIn("breaks", item.warning.lower())
            self.assertEqual(
                [(entry.code, entry.detail) for entry in item.evidence],
                [
                    ("tool-report", "NDK side by side"),
                    ("preview-command", "sdkmanager --list_installed"),
                ],
            )
            self.assertEqual(item.tool_action.tool, "sdkmanager")
            self.assertEqual(item.tool_action.resource, "ndk;27.0.12077973")
            self.assertEqual(
                item.tool_action.argv,
                ("sdkmanager", "--uninstall", "ndk;27.0.12077973"),
            )
            self.assertEqual(
                item.tool_action.preview_argv, ("sdkmanager", "--list_installed")
            )
            self.assertEqual(item.tool_action.reported, "27.0.1")

    def test_loadable_avd_accepts_custom_absolute_path_and_preserves_state_warning(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            sdk = root / "sdk"
            sdk.mkdir()
            avd_path = root / "custom-avds" / "Pixel_Custom.avd"
            avd_path.mkdir(parents=True)
            (avd_path / "userdata.img").write_bytes(b"x" * 8192)
            avd_output = """Available Android Virtual Devices:
Name: Pixel_Custom
Path: {path}
Target: Android 35
""".format(path=avd_path)

            def sdk_runner(argv):
                return ToolResult(tuple(argv), "", "", 0)

            def avd_runner(argv):
                return ToolResult(tuple(argv), avd_output, "", 0)

            items, unavailable = analyze_android(sdk_runner, avd_runner, 12, sdk)

            self.assertIsNone(unavailable)
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item.detector_id, "android-avd")
            self.assertEqual(item.label, "Android virtual device Pixel_Custom")
            self.assertEqual(item.path, avd_path)
            self.assertEqual(item.allocated_bytes, path_size(avd_path))
            self.assertEqual(item.reclaimable_bytes, path_size(avd_path))
            self.assertIs(item.confidence, Confidence.STRONG)
            self.assertIs(item.restoration, RestorationCost.EXTERNAL_STATE)
            self.assertFalse(item.selected_by_default)
            self.assertEqual(item.safety_root, avd_path.parent)
            self.assertIn("apps and data", item.reason)
            self.assertIn("cannot be recovered", item.warning)
            self.assertEqual(
                [(entry.code, entry.detail) for entry in item.evidence],
                [
                    ("tool-report", "Android 35"),
                    ("preview-command", "avdmanager list avd"),
                ],
            )
            self.assertEqual(item.tool_action.tool, "avdmanager")
            self.assertEqual(item.tool_action.resource, "Pixel_Custom")
            self.assertEqual(
                item.tool_action.argv,
                ("avdmanager", "delete", "avd", "-n", "Pixel_Custom"),
            )
            self.assertEqual(
                item.tool_action.preview_argv, ("avdmanager", "list", "avd")
            )
            self.assertEqual(item.tool_action.reported, "Android 35")

    def test_unloadable_avd_is_heuristic_and_reports_its_error(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            sdk = Path(raw_tmp) / "sdk"
            sdk.mkdir()
            broken = Path(raw_tmp) / "Broken.avd"
            broken.mkdir()
            output = """The following Android Virtual Devices could not be loaded:
Name: Broken
Path: {path}
Error: system image is missing
""".format(path=broken)

            def runner(argv):
                stdout = output if argv[0] == "avdmanager" else ""
                return ToolResult(tuple(argv), stdout, "", 0)

            items, unavailable = analyze_android(runner, runner, 13, sdk)

        self.assertIsNone(unavailable)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.detector_id, "android-avd-broken")
        self.assertEqual(item.label, "Android virtual device Broken (will not load)")
        self.assertIs(item.confidence, Confidence.HEURISTIC)
        self.assertIs(item.restoration, RestorationCost.EXTERNAL_STATE)
        self.assertFalse(item.selected_by_default)
        self.assertIn("missing SDK component", item.reason)
        self.assertEqual(item.evidence[0].detail, "system image is missing")
        self.assertEqual(item.tool_action.resource, "Broken")
        self.assertEqual(item.tool_action.reported, "system image is missing")

    def test_sdk_measurement_rejects_unsafe_or_malformed_locations(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            sdk = root / "sdk"
            valid = sdk / "ndk" / "valid"
            valid.mkdir(parents=True)
            (valid / "blob").write_bytes(b"valid")
            outside = root / "outside"
            outside.mkdir()
            (sdk / "ndk" / "escape").symlink_to(outside, target_is_directory=True)
            output = """Installed packages:
Path | Version | Description | Location
ndk;valid | 1 | valid | ndk/valid
ndk;absolute | 1 | absolute | {outside}
ndk;traversal | 1 | traversal | ../outside
ndk;symlink | 1 | symlink | ndk/escape
ndk;empty | 1 | empty |
ndk;nul-location | 1 | nul | ndk/bad\x00location
ndk;bad\x00identity | 1 | nul identity | ndk/valid
""".format(outside=outside)

            def runner(argv):
                stdout = output if argv[0] == "sdkmanager" else ""
                return ToolResult(tuple(argv), stdout, "", 0)

            items, unavailable = analyze_android(runner, runner, 1, sdk)

        self.assertIsNone(unavailable)
        self.assertEqual([item.tool_action.resource for item in items], ["ndk;valid"])

    def test_unknown_sdk_groups_are_omitted_conservatively(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            sdk = Path(raw_tmp)
            for relative in ("extras/vendor", "emulator-extra"):
                path = sdk / relative
                path.mkdir(parents=True)
                (path / "blob").write_bytes(b"x")
            output = """Installed packages:
Path | Version | Description | Location
extras;vendor | 1 | vendor extras | extras/vendor
emulator-extra | 1 | misleading emulator prefix | emulator-extra
"""

            def runner(argv):
                stdout = output if argv[0] == "sdkmanager" else ""
                return ToolResult(tuple(argv), stdout, "", 0)

            items, unavailable = analyze_android(runner, runner, 1, sdk)

        self.assertIsNone(unavailable)
        self.assertEqual(items, [])

    def test_all_declared_sdk_groups_have_specific_detector_ids(self):
        rows = [
            ("ndk;1", "ndk/1"),
            ("cmake;1", "cmake/1"),
            ("system-images;android-35;x", "system-images/android-35/x"),
            ("build-tools;35", "build-tools/35"),
            ("platforms;android-35", "platforms/android-35"),
            ("cmdline-tools;latest", "cmdline-tools/latest"),
            ("emulator", "emulator"),
        ]
        with tempfile.TemporaryDirectory() as raw_tmp:
            sdk = Path(raw_tmp)
            for _, relative in rows:
                path = sdk / relative
                path.mkdir(parents=True)
                (path / "blob").write_bytes(b"x")
            output = "Installed packages:\nPath | Version | Description | Location\n"
            output += "\n".join(
                "{} | 1 | package | {}".format(identity, location)
                for identity, location in reversed(rows)
            )

            def runner(argv):
                stdout = output if argv[0] == "sdkmanager" else ""
                return ToolResult(tuple(argv), stdout, "", 0)

            items, _ = analyze_android(runner, runner, 1, sdk)

        self.assertEqual(
            [item.detector_id for item in items],
            [
                "android-ndk",
                "android-cmake",
                "android-system-image",
                "android-build-tools",
                "android-platform",
                "android-cmdline-tools",
                "android-emulator",
            ],
        )

    def test_duplicate_sdk_identities_are_omitted_in_both_row_orders(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            sdk = Path(raw_tmp)
            for location in ("ndk/duplicate-a", "ndk/duplicate-b", "cmake/independent"):
                directory = sdk / location
                directory.mkdir(parents=True)
                (directory / "blob").write_bytes(b"x")
            rows = [
                "ndk;same | 1 | first | ndk/duplicate-a",
                "cmake;keep | 2 | keep | cmake/independent",
                "ndk;same | 1 | second | ndk/duplicate-b",
            ]

            for ordered_rows in (rows, list(reversed(rows))):
                output = "Installed packages:\nPath | Version | Description | Location\n"
                output += "\n".join(ordered_rows)

                def runner(argv):
                    stdout = output if argv[0] == "sdkmanager" else ""
                    return ToolResult(tuple(argv), stdout, "", 0)

                items, _ = analyze_android(runner, runner, 1, sdk)

                self.assertEqual(
                    [item.tool_action.resource for item in items], ["cmake;keep"]
                )

    def test_duplicate_avd_names_are_omitted_in_both_record_orders(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            sdk = root / "sdk"
            sdk.mkdir()
            paths = [root / name for name in ("first.avd", "second.avd", "keep.avd")]
            for path in paths:
                path.mkdir()
            records = [
                "Name: Same\nPath: {}\nTarget: one".format(paths[0]),
                "Name: Keep\nPath: {}\nTarget: keep".format(paths[2]),
                "Name: Same\nPath: {}\nTarget: two".format(paths[1]),
            ]

            for ordered_records in (records, list(reversed(records))):
                output = "Available Android Virtual Devices:\n" + "\n".join(
                    ordered_records
                )

                def runner(argv):
                    stdout = output if argv[0] == "avdmanager" else ""
                    return ToolResult(tuple(argv), stdout, "", 0)

                items, _ = analyze_android(runner, runner, 1, sdk)

                self.assertEqual(
                    [item.tool_action.resource for item in items], ["Keep"]
                )

    def test_output_uses_declared_group_and_identity_order(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            sdk = root / "sdk"
            for relative in ("cmake/z", "ndk/z", "ndk/a", "build-tools/x"):
                path = sdk / relative
                path.mkdir(parents=True)
                (path / "blob").write_bytes(b"x")
            for name in ("Zed", "Alpha", "BrokenZ", "BrokenA"):
                (root / (name + ".avd")).mkdir()
            sdk_output = """Installed packages:
Path | Version | Description | Location
build-tools;x | 1 | build | build-tools/x
ndk;z | 1 | ndk z | ndk/z
cmake;z | 1 | cmake | cmake/z
ndk;a | 1 | ndk a | ndk/a
"""
            avd_output = """Available Android Virtual Devices:
Name: Zed
Path: {root}/Zed.avd
Name: Alpha
Path: {root}/Alpha.avd
The following Android Virtual Devices could not be loaded:
Name: BrokenZ
Path: {root}/BrokenZ.avd
Error: z
Name: BrokenA
Path: {root}/BrokenA.avd
Error: a
""".format(root=root)

            def runner(argv):
                stdout = sdk_output if argv[0] == "sdkmanager" else avd_output
                return ToolResult(tuple(argv), stdout, "", 0)

            items, _ = analyze_android(runner, runner, 1, sdk)

        self.assertEqual(
            [item.tool_action.resource for item in items],
            [
                "ndk;a",
                "ndk;z",
                "cmake;z",
                "build-tools;x",
                "Alpha",
                "Zed",
                "BrokenA",
                "BrokenZ",
            ],
        )
        self.assertEqual(len({item.id for item in items}), len(items))
        for item in items:
            self.assertEqual(len(item.id), 16)
            self.assertIs(item.action, ActionKind.INVOKE_TOOL)
            self.assertFalse(item.selected_by_default)
            self.assertIsInstance(item.tool_action.argv, tuple)
            self.assertIsInstance(item.tool_action.preview_argv, tuple)

    def test_unavailable_sdk_is_bounded_and_does_not_suppress_avds(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            sdk = root / "sdk"
            sdk.mkdir()
            avd = root / "Keep.avd"
            avd.mkdir()

            def sdk_runner(argv):
                raise ToolUnavailable("sdkmanager", "s" * (MAX_STDERR_CHARS + 50))

            def avd_runner(argv):
                output = "Available Android Virtual Devices:\nName: Keep\nPath: {}".format(
                    avd
                )
                return ToolResult(tuple(argv), output, "", 0)

            items, unavailable = analyze_android(sdk_runner, avd_runner, 1, sdk)

        self.assertEqual([item.tool_action.resource for item in items], ["Keep"])
        self.assertEqual(len(unavailable), MAX_STDERR_CHARS)
        self.assertTrue(unavailable.endswith("...[truncated]"))

    def test_avd_validation_rejects_missing_relative_and_nul_fields(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            sdk = root / "sdk"
            sdk.mkdir()
            valid = root / "Valid.avd"
            valid.mkdir()
            output = """Available Android Virtual Devices:
Name: MissingPath
Target: Android 35
Name: Relative
Path: relative.avd
Name: BadName\x00suffix
Path: {valid}
Name: BadPath
Path: {valid}\x00suffix
Name: Valid
Path: {valid}
""".format(valid=valid)

            def runner(argv):
                stdout = output if argv[0] == "avdmanager" else ""
                return ToolResult(tuple(argv), stdout, "", 0)

            items, unavailable = analyze_android(runner, runner, 1, sdk)

        self.assertIsNone(unavailable)
        self.assertEqual([item.tool_action.resource for item in items], ["Valid"])
        self.assertNotIn(Path("."), [item.path for item in items])

    def test_nonzero_inventory_reason_prefers_stderr_then_stdout_then_exit_code(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            sdk = Path(raw_tmp)

            cases = [
                ("stderr detail", "stdout detail", 3, "stderr detail"),
                ("   ", "stdout detail", 4, "stdout detail"),
                ("", "", 5, "avdmanager exited with 5"),
                ("e" * MAX_STDERR_CHARS, "", 6, "e" * MAX_STDERR_CHARS),
            ]
            for stderr, stdout, exit_code, expected in cases:
                def sdk_runner(argv):
                    return ToolResult(tuple(argv), "", "", 0)

                def avd_runner(argv):
                    return ToolResult(tuple(argv), stdout, stderr, exit_code)

                items, unavailable = analyze_android(sdk_runner, avd_runner, 1, sdk)

                self.assertEqual(items, [])
                self.assertEqual(unavailable, expected)

    def test_nonzero_stderr_stdout_and_exit_fallbacks_are_bounded(self):
        prefix = "avdmanager exited with "
        cases = [
            ("e" * (MAX_STDERR_CHARS + 1), "", 1),
            ("", "o" * (MAX_STDERR_CHARS + 1), 2),
            ("", "", int("7" * (MAX_STDERR_CHARS - len(prefix) + 1))),
        ]
        with tempfile.TemporaryDirectory() as raw_tmp:
            sdk = Path(raw_tmp)
            for stderr, stdout, exit_code in cases:
                def sdk_runner(argv):
                    return ToolResult(tuple(argv), "", "", 0)

                def avd_runner(argv):
                    return ToolResult(tuple(argv), stdout, stderr, exit_code)

                _, unavailable = analyze_android(sdk_runner, avd_runner, 1, sdk)

                self.assertEqual(len(unavailable), MAX_STDERR_CHARS)
                self.assertTrue(unavailable.endswith("...[truncated]"))

    def test_combined_unavailable_reason_is_capped_with_marker_inside_limit(self):
        def sdk_runner(argv):
            raise ToolUnavailable("sdkmanager", "s" * (MAX_STDERR_CHARS - 2))

        def avd_runner(argv):
            raise ToolUnavailable("avdmanager", "avd failed")

        with tempfile.TemporaryDirectory() as raw_tmp:
            items, unavailable = analyze_android(
                sdk_runner, avd_runner, 1, Path(raw_tmp)
            )

        self.assertEqual(items, [])
        self.assertEqual(len(unavailable), MAX_STDERR_CHARS)
        self.assertTrue(unavailable.endswith("...[truncated]"))

    def test_unavailable_avd_is_bounded_and_does_not_suppress_sdk_packages(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            sdk = Path(raw_tmp)
            package = sdk / "cmake" / "1"
            package.mkdir(parents=True)
            (package / "blob").write_bytes(b"x")
            output = """Installed packages:
Path | Version | Description | Location
cmake;1 | 1 | cmake | cmake/1
"""

            def sdk_runner(argv):
                return ToolResult(tuple(argv), output, "", 0)

            def avd_runner(argv):
                raise ToolUnavailable("avdmanager", "a" * (MAX_STDERR_CHARS + 1))

            items, unavailable = analyze_android(sdk_runner, avd_runner, 1, sdk)

        self.assertEqual([item.tool_action.resource for item in items], ["cmake;1"])
        self.assertEqual(len(unavailable), MAX_STDERR_CHARS)
        self.assertTrue(unavailable.endswith("...[truncated]"))

    def test_cmdline_tool_dirs_are_deterministic_with_missing_entries(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            sdk = Path(raw_tmp)
            for version in ("9.0", "12.0", "latest"):
                (sdk / "cmdline-tools" / version).mkdir(parents=True)
            (sdk / "cmdline-tools" / "README").write_text("not a directory")

            self.assertEqual(
                cmdline_tool_dirs(sdk),
                (
                    sdk / "cmdline-tools" / "latest" / "bin",
                    sdk / "cmdline-tools" / "9.0" / "bin",
                    sdk / "cmdline-tools" / "12.0" / "bin",
                    sdk / "tools" / "bin",
                ),
            )


if __name__ == "__main__":
    unittest.main()
