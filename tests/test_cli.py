import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from mac_dev_clean.cli import main
from mac_dev_clean.model import CleanResult, ScanTarget


class CliTests(unittest.TestCase):
    def test_clean_requires_explicit_flag(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(stderr):
            main(["clean"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("clean requires at least one explicit category flag", stderr.getvalue())

    def test_node_modules_clean_requires_age_filter(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(stderr):
            main(["clean", "--node-modules", "--dry-run"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--node-modules requires --older-than", stderr.getvalue())

    def test_report_json_outputs_valid_json(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("mac_dev_clean.cli.scan", return_value=[]))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            code = main(["report", "--json", "--no-node-modules"])

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertIn("items", payload)
        self.assertIn("total_bytes", payload)
        self.assertIn("Scanning developer cache locations", stderr.getvalue())

    def test_scan_announces_before_scanning(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        def fake_scan(**_kwargs):
            self.assertIn("Scanning developer cache locations", stderr.getvalue())
            return []

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("mac_dev_clean.cli.scan", side_effect=fake_scan))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            code = main(["scan", "--no-node-modules"])

        self.assertEqual(code, 0)
        self.assertIn("No supported developer cache locations found.", stdout.getvalue())

    def test_scan_can_skip_project_derived_data_discovery(self):
        with contextlib.ExitStack() as stack:
            scan_mock = stack.enter_context(
                patch("mac_dev_clean.cli.scan", return_value=[])
            )
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            code = main(
                ["scan", "--no-node-modules", "--no-project-derived-data"]
            )

        self.assertEqual(code, 0)
        self.assertFalse(scan_mock.call_args.kwargs["include_project_derived_data"])

    def test_default_command_cancel_does_not_clean(self):
        target = ScanTarget(
            category="brew-cache",
            label="Homebrew cache",
            path=Path("/tmp/home/Library/Caches/Homebrew"),
            size_bytes=1,
            modified_at=None,
            cleanable=True,
            delete_mode="contents",
            safety_root=Path("/tmp/home"),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("mac_dev_clean.cli.scan", return_value=[target]))
            clean_targets = stack.enter_context(patch("mac_dev_clean.cli.clean_targets"))
            open_journal = stack.enter_context(patch("mac_dev_clean.cli.open_journal"))
            stack.enter_context(patch("builtins.input", return_value="n"))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            code = main([])

        self.assertEqual(code, 0)
        clean_targets.assert_not_called()
        open_journal.assert_not_called()
        self.assertIn("Canceled. Nothing deleted.", stdout.getvalue())
        self.assertIn("Scanning developer cache locations", stderr.getvalue())

    def test_default_command_includes_xcode_test_device_clones(self):
        clone_target = ScanTarget(
            category="xcode-test-devices",
            label="XCTest simulator clones",
            path=Path("/tmp/home/Library/Developer/XCTestDevices"),
            size_bytes=2 * 1024 * 1024 * 1024,
            modified_at=None,
            cleanable=True,
            delete_mode="simctl-device-set",
            safety_root=Path("/tmp/home"),
        )

        journal = object()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("mac_dev_clean.cli.scan", return_value=[clone_target]))
            stack.enter_context(
                patch("mac_dev_clean.cli.open_journal", return_value=journal)
            )
            clean_targets = stack.enter_context(
                patch("mac_dev_clean.cli.clean_targets", return_value=[])
            )
            stack.enter_context(patch("builtins.input", return_value="y"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            code = main([])

        self.assertEqual(code, 0)
        clean_targets.assert_called_once_with([clone_target], journal=journal)

    def test_interactive_yes_cleans_cleanable_targets(self):
        cleanable = ScanTarget(
            category="brew-cache",
            label="Homebrew cache",
            path=Path("/tmp/home/Library/Caches/Homebrew"),
            size_bytes=1,
            modified_at=None,
            cleanable=True,
            delete_mode="contents",
            safety_root=Path("/tmp/home"),
        )
        report_only = ScanTarget(
            category="docker",
            label="Docker Desktop VM data",
            path=Path("/tmp/home/Library/Containers/com.docker.docker/Data/vms"),
            size_bytes=1,
            modified_at=None,
            cleanable=False,
            delete_mode="none",
            safety_root=Path("/tmp/home"),
        )
        result = CleanResult(
            category="brew-cache",
            label="Homebrew cache",
            path=cleanable.path,
            size_bytes=1,
            dry_run=False,
            removed=True,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        journal = object()

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch("mac_dev_clean.cli.scan", return_value=[cleanable, report_only])
            )
            stack.enter_context(
                patch("mac_dev_clean.cli.open_journal", return_value=journal)
            )
            clean_targets = stack.enter_context(
                patch("mac_dev_clean.cli.clean_targets", return_value=[result])
            )
            stack.enter_context(patch("builtins.input", return_value="y"))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            code = main(["interactive"])

        self.assertEqual(code, 0)
        clean_targets.assert_called_once_with([cleanable], journal=journal)
        self.assertIn("removed", stdout.getvalue())
        self.assertIn("/tmp/home/Library/Containers/com.docker.docker/Data/vms", stdout.getvalue())

    def test_interactive_yes_opens_one_default_journal_after_confirmation(self):
        target = ScanTarget(
            category="brew-cache",
            label="Homebrew cache",
            path=Path("/tmp/home/Library/Caches/Homebrew"),
            size_bytes=1,
            modified_at=None,
            cleanable=True,
            delete_mode="contents",
            safety_root=Path("/tmp/home"),
        )
        journal = object()

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("mac_dev_clean.cli.scan", return_value=[target]))
            open_journal = stack.enter_context(
                patch("mac_dev_clean.cli.open_journal", return_value=journal)
            )
            clean_targets = stack.enter_context(
                patch("mac_dev_clean.cli.clean_targets", return_value=[])
            )
            stack.enter_context(patch("builtins.input", return_value="y"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            code = main(["interactive"])

        self.assertEqual(code, 0)
        open_journal.assert_called_once_with()
        clean_targets.assert_called_once_with([target], journal=journal)

    def test_interactive_include_node_modules_requires_age_filter(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(stderr):
            main(["interactive", "--include-node-modules"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("interactive --include-node-modules requires --older-than", stderr.getvalue())

    def test_clean_opens_one_default_journal_and_passes_it_to_all_targets(self):
        target = ScanTarget(
            category="brew-cache",
            label="Homebrew cache",
            path=Path("/tmp/home/Library/Caches/Homebrew"),
            size_bytes=1,
            modified_at=None,
            cleanable=True,
            delete_mode="contents",
            safety_root=Path("/tmp/home"),
        )
        journal = object()

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("mac_dev_clean.cli.scan", return_value=[target]))
            open_journal = stack.enter_context(
                patch("mac_dev_clean.cli.open_journal", return_value=journal)
            )
            clean_targets = stack.enter_context(
                patch("mac_dev_clean.cli.clean_targets", return_value=[])
            )
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            code = main(["clean", "--brew-cache", "--dry-run"])

        self.assertEqual(code, 0)
        open_journal.assert_called_once_with()
        clean_targets.assert_called_once_with(
            [target], dry_run=True, journal=journal
        )

    def test_clean_warning_is_nonfatal_and_visible_in_text_and_json(self):
        target = ScanTarget(
            category="brew-cache",
            label="Homebrew cache",
            path=Path("/tmp/home/Library/Caches/Homebrew"),
            size_bytes=1,
            modified_at=None,
            cleanable=True,
            delete_mode="contents",
            safety_root=Path("/tmp/home"),
        )
        result = CleanResult(
            category=target.category,
            label=target.label,
            path=target.path,
            size_bytes=target.size_bytes,
            dry_run=False,
            removed=True,
            journal_warning="journal write failed",
        )

        for extra_args in ([], ["--json"]):
            with self.subTest(extra_args=extra_args):
                stdout = io.StringIO()
                with contextlib.ExitStack() as stack:
                    stack.enter_context(
                        patch("mac_dev_clean.cli.scan", return_value=[target])
                    )
                    stack.enter_context(
                        patch("mac_dev_clean.cli.open_journal", return_value=object())
                    )
                    stack.enter_context(
                        patch("mac_dev_clean.cli.clean_targets", return_value=[result])
                    )
                    stack.enter_context(contextlib.redirect_stdout(stdout))
                    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                    code = main(["clean", "--brew-cache"] + extra_args)

                self.assertEqual(code, 0)
                self.assertIn("journal write failed", stdout.getvalue())
                self.assertNotIn("private", stdout.getvalue())

    def test_package_caches_flag_selects_dependency_cache_targets(self):
        target = ScanTarget(
            category="pnpm-cache",
            label="pnpm store",
            path=Path("/tmp/home/Library/pnpm/store"),
            size_bytes=1,
            modified_at=None,
            cleanable=True,
            delete_mode="contents",
            safety_root=Path("/tmp/home"),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        journal = object()

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("mac_dev_clean.cli.scan", return_value=[target]))
            stack.enter_context(
                patch("mac_dev_clean.cli.open_journal", return_value=journal)
            )
            clean_targets = stack.enter_context(
                patch("mac_dev_clean.cli.clean_targets", return_value=[])
            )
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            code = main(["clean", "--package-caches", "--dry-run"])

        self.assertEqual(code, 0)
        clean_targets.assert_called_once_with(
            [target], dry_run=True, journal=journal
        )
        self.assertIn("Scanning developer cache locations", stderr.getvalue())

    def test_xcode_caches_flag_includes_test_device_clones_and_device_logs(self):
        clone_target = ScanTarget(
            category="xcode-test-devices",
            label="XCTest simulator clones",
            path=Path("/tmp/home/Library/Developer/XCTestDevices"),
            size_bytes=1,
            modified_at=None,
            cleanable=True,
            delete_mode="simctl-device-set",
            safety_root=Path("/tmp/home"),
        )
        log_target = ScanTarget(
            category="xcode-device-logs",
            label="Xcode device logs",
            path=Path("/tmp/home/Library/Developer/Xcode/DeviceLogs"),
            size_bytes=1,
            modified_at=None,
            cleanable=True,
            delete_mode="contents",
            safety_root=Path("/tmp/home"),
        )

        journal = object()
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch("mac_dev_clean.cli.scan", return_value=[clone_target, log_target])
            )
            stack.enter_context(
                patch("mac_dev_clean.cli.open_journal", return_value=journal)
            )
            clean_targets = stack.enter_context(
                patch("mac_dev_clean.cli.clean_targets", return_value=[])
            )
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            code = main(["clean", "--xcode-caches", "--dry-run"])

        self.assertEqual(code, 0)
        clean_targets.assert_called_once_with(
            [clone_target, log_target], dry_run=True, journal=journal
        )


if __name__ == "__main__":
    unittest.main()
