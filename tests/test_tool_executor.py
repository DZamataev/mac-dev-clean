from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mac_dev_clean.executor import ApplyOutcome, ApplyResult
from mac_dev_clean.fsevents import VolumeIdentity
from mac_dev_clean.index import open_index
from mac_dev_clean.journal import ActionJournal
from mac_dev_clean.recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    ToolAction,
)
from mac_dev_clean.scanner import path_size
from mac_dev_clean.tool_executor import (
    CONTRACT_VERSION,
    CurrentEffect,
    contract_refusal,
    current_tool_effect,
    invoke_tool_recommendations,
)
from mac_dev_clean.tools.android import AVD_LIST_ARGV, SDK_LIST_ARGV, analyze_android
from mac_dev_clean.tools.docker import analyze_docker
from mac_dev_clean.tools.homebrew import analyze_homebrew
from mac_dev_clean.tools.runner import ToolResult, ToolUnavailable
from mac_dev_clean.tools.simulator import (
    SIMCTL_DEVICES_PREVIEW_ARGV,
    SIMCTL_RUNTIMES_PREVIEW_ARGV,
    analyze_simulator,
)

HOME = Path("/Users/test/home")

PREVIEW = (
    '{"Active":"0","Reclaimable":"7.499GB","Size":"18.92GB",'
    '"TotalCount":"144","Type":"Build Cache"}\n'
)

DOCKER_PREVIEW_ARGV = ("docker", "system", "df", "--format", "{{json .}}")
DOCKER_PRUNE_ARGV = ("docker", "builder", "prune", "-f")
XCRUN = "/usr/bin/xcrun"


def build_item(generation: int = 1) -> Recommendation:
    return Recommendation(
        detector_id="docker-build-cache",
        category="tool-managed",
        label="Docker build cache",
        path=HOME,
        action=ActionKind.INVOKE_TOOL,
        allocated_bytes=18920000000,
        reclaimable_bytes=7499000000,
        confidence=Confidence.EXACT,
        restoration=RestorationCost.EXTERNAL_STATE,
        selected_by_default=False,
        evidence=(Evidence("tool-report", "docker reports 7.499GB reclaimable"),),
        safety_root=HOME,
        reason="",
        generation=generation,
        tool_action=ToolAction(
            tool="docker",
            resource="build-cache",
            argv=DOCKER_PRUNE_ARGV,
            preview_argv=DOCKER_PREVIEW_ARGV,
            reported="7.499GB",
        ),
    )


class RecordingRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, argv):
        vector = tuple(argv)
        self.calls.append(vector)
        response = self.responses.get(vector)
        if response is None:
            raise AssertionError("unexpected vector {}".format(vector))
        if isinstance(response, Exception):
            raise response
        return response


def ok(argv, stdout: str = "") -> ToolResult:
    """A success carrying the provenance a real runner would have carried.

    A fake whose argv names no real command could never come back from
    `run_tool`, so a test built on one proves nothing about a caller that
    checks where its output came from.
    """
    return ToolResult(argv=tuple(argv), stdout=stdout, stderr="", exit_code=0)


class InvokeToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index = open_index(Path(self.tmp.name) / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.index.begin_generation(VolumeIdentity(device=1, uuid="U"), event_id=1)
        self.item = build_item()
        self.index.record_recommendation(self.item)
        self.index.complete_generation()
        self.journal_path = Path(self.tmp.name) / "actions.jsonl"
        self.journal = ActionJournal(self.journal_path)

    def records(self):
        text = self.journal_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def test_re_previews_before_executing(self):
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW),
                DOCKER_PRUNE_ARGV: ok(
                    DOCKER_PRUNE_ARGV, "Total reclaimed space: 7.499GB"
                ),
            }
        )

        results = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.INVOKED)
        self.assertEqual(runner.calls[0][:3], ("docker", "system", "df"))
        self.assertEqual(runner.calls[1], ("docker", "builder", "prune", "-f"))

    def test_dry_run_previews_but_never_executes(self):
        runner = RecordingRunner(
            {DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW)}
        )

        results = invoke_tool_recommendations(
            [self.item.id],
            self.index,
            self.journal,
            runners={"docker": runner},
            dry_run=True,
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.SKIPPED)
        self.assertNotIn(("docker", "builder", "prune", "-f"), runner.calls)

    def test_only_the_declared_vector_is_executed(self):
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW),
                DOCKER_PRUNE_ARGV: ok(DOCKER_PRUNE_ARGV, ""),
            }
        )

        invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        for call in runner.calls:
            self.assertNotIn("-a", call)
            self.assertNotIn("--all", call)

    def test_non_zero_exit_is_failed_with_stderr(self):
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW),
                DOCKER_PRUNE_ARGV: ToolResult(
                    argv=DOCKER_PRUNE_ARGV,
                    stdout="",
                    stderr="permission denied",
                    exit_code=1,
                ),
            }
        )

        results = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.FAILED)
        self.assertIn("permission denied", results[0].error)

    def test_tool_gone_between_scan_and_action_is_failed(self):
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ToolUnavailable(
                    "docker", "Cannot connect to the Docker daemon"
                )
            }
        )

        results = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.FAILED)
        self.assertIn("Cannot connect", results[0].error)

    def test_nothing_left_to_reclaim_is_skipped(self):
        empty = (
            '{"Active":"0","Reclaimable":"0B","Size":"0B",'
            '"TotalCount":"0","Type":"Build Cache"}\n'
        )
        runner = RecordingRunner({DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, empty)})

        results = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.SKIPPED)
        self.assertNotIn(("docker", "builder", "prune", "-f"), runner.calls)

    def test_unknown_id_is_failed_and_journalled(self):
        results = invoke_tool_recommendations(
            ["deadbeefdeadbeef"], self.index, self.journal, runners={}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.FAILED)
        self.assertEqual(len(self.records()), 1)

    def test_a_path_recommendation_is_refused(self):
        index2 = open_index(Path(self.tmp.name) / "index2.sqlite3")
        self.addCleanup(index2.close)
        index2.begin_generation(VolumeIdentity(device=1, uuid="U"), event_id=1)
        path_item = Recommendation(
            detector_id="node-modules",
            category="project-dependencies",
            label="node_modules",
            path=HOME / "app" / "node_modules",
            action=ActionKind.DELETE_TREE,
            allocated_bytes=1,
            reclaimable_bytes=1,
            confidence=Confidence.STRONG,
            restoration=RestorationCost.REDOWNLOAD,
            selected_by_default=True,
            evidence=(),
            safety_root=HOME / "app",
            reason="",
            generation=1,
        )
        index2.record_recommendation(path_item)
        index2.complete_generation()

        results = invoke_tool_recommendations(
            [path_item.id], index2, self.journal, runners={}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.FAILED)


class JournalIntegrationTests(InvokeToolTests):
    def test_success_is_journalled_with_the_executed_vector(self):
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW),
                DOCKER_PRUNE_ARGV: ok(DOCKER_PRUNE_ARGV, ""),
            }
        )

        invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        record = self.records()[-1]
        self.assertEqual(record["outcome"], "invoked")
        self.assertEqual(record["argv"], ["docker", "builder", "prune", "-f"])
        self.assertFalse(record["dry_run"])

    def test_dry_run_is_journalled_as_a_dry_run(self):
        runner = RecordingRunner(
            {DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW)}
        )

        invoke_tool_recommendations(
            [self.item.id],
            self.index,
            self.journal,
            runners={"docker": runner},
            dry_run=True,
        )

        self.assertTrue(self.records()[-1]["dry_run"])

    def test_failure_is_journalled_with_its_reason(self):
        runner = RecordingRunner(
            {DOCKER_PREVIEW_ARGV: ToolUnavailable("docker", "daemon stopped")}
        )

        invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        record = self.records()[-1]
        self.assertEqual(record["outcome"], "failed")
        self.assertIn("daemon stopped", record["detail"])


# Imported here rather than beside the other imports so the block above stays
# byte-identical to the one the brief specifies.
from datetime import datetime, timezone  # noqa: E402


class ClockAndCancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index = open_index(Path(self.tmp.name) / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.index.begin_generation(VolumeIdentity(device=1, uuid="U"), event_id=1)
        self.item = build_item()
        self.index.record_recommendation(self.item)
        self.index.complete_generation()
        self.journal_path = Path(self.tmp.name) / "actions.jsonl"
        self.journal = ActionJournal(self.journal_path)

    def records(self):
        text = self.journal_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def test_cancellation_stops_before_the_next_action(self):
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW),
                DOCKER_PRUNE_ARGV: ok(DOCKER_PRUNE_ARGV, ""),
            }
        )
        seen = []

        def should_cancel():
            seen.append(1)
            return len(seen) > 2

        results = invoke_tool_recommendations(
            [self.item.id, self.item.id],
            self.index,
            self.journal,
            runners={"docker": runner},
            should_cancel=should_cancel,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(runner.calls.count(("docker", "builder", "prune", "-f")), 1)

    def test_the_supplied_clock_stamps_the_journal(self):
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW),
                DOCKER_PRUNE_ARGV: ok(DOCKER_PRUNE_ARGV, ""),
            }
        )

        invoke_tool_recommendations(
            [self.item.id],
            self.index,
            self.journal,
            runners={"docker": runner},
            now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )

        self.assertTrue(self.records()[-1]["timestamp"].startswith("2026-01-02T03:04:05"))


BREW_PREVIEW = (
    "Would remove: /Users/test/home/Library/Caches/Homebrew/node--20.tar.gz\n"
    "Would remove: /opt/homebrew/Cellar/git/2.44.0\n"
    "==> This operation would free approximately 1.2GB of disk space.\n"
)


def build_brew_item(generation: int = 1) -> Recommendation:
    """Mirror what analyze_homebrew emits, so the executor sees a real shape."""
    return Recommendation(
        detector_id="homebrew-cleanup",
        category="tool-managed",
        label="Homebrew removable versions and downloads",
        path=HOME,
        action=ActionKind.INVOKE_TOOL,
        allocated_bytes=1200000000,
        reclaimable_bytes=1200000000,
        confidence=Confidence.EXACT,
        restoration=RestorationCost.REDOWNLOAD,
        selected_by_default=False,
        evidence=(Evidence("tool-report", "brew would remove 2 items"),),
        safety_root=HOME,
        reason="",
        generation=generation,
        tool_action=ToolAction(
            tool="brew",
            resource="cleanup",
            argv=("brew", "cleanup"),
            preview_argv=("brew", "cleanup", "-n"),
            reported="2 items",
        ),
    )


class BrewInvocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index = open_index(Path(self.tmp.name) / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.index.begin_generation(VolumeIdentity(device=1, uuid="U"), event_id=1)
        self.item = build_brew_item()
        self.index.record_recommendation(self.item)
        self.index.complete_generation()
        self.journal_path = Path(self.tmp.name) / "actions.jsonl"
        self.journal = ActionJournal(self.journal_path)

    def records(self):
        text = self.journal_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def test_the_brew_preview_total_authorises_the_cleanup(self):
        runner = RecordingRunner(
            {
                ("brew", "cleanup", "-n"): ok(("brew", "cleanup", "-n"), BREW_PREVIEW),
                ("brew", "cleanup"): ok(
                    ("brew", "cleanup"), "Removing: /opt/homebrew/Cellar/git/2.44.0"
                ),
            }
        )

        results = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"brew": runner}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.INVOKED)
        self.assertEqual(results[0].reclaimable_bytes, 1200000000)
        self.assertEqual(runner.calls[1], ("brew", "cleanup"))

    def test_a_brew_preview_with_nothing_to_remove_is_skipped(self):
        runner = RecordingRunner(
            {("brew", "cleanup", "-n"): ok(("brew", "cleanup", "-n"), "")}
        )

        results = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"brew": runner}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.SKIPPED)
        self.assertNotIn(("brew", "cleanup"), runner.calls)


def build_tool_item(action: ToolAction, generation: int = 1) -> Recommendation:
    """Wrap an arbitrary tool action in an otherwise ordinary recommendation.

    The recommendation is deliberately well formed everywhere else, so a
    refusal can only be attributable to the action itself.
    """
    return Recommendation(
        detector_id="contract-probe",
        category="tool-managed",
        label="Contract probe",
        path=HOME,
        action=ActionKind.INVOKE_TOOL,
        allocated_bytes=1000,
        reclaimable_bytes=1000,
        confidence=Confidence.HEURISTIC,
        restoration=RestorationCost.EXTERNAL_STATE,
        selected_by_default=False,
        evidence=(),
        safety_root=HOME,
        reason="",
        generation=generation,
        tool_action=action,
    )


class NotAToolResult:
    """Quacks like a result without being one.

    Nothing in the runner protocol stops a caller from handing back an object
    of its own shape; the point of the test is that the executor refuses to
    trust one rather than reading fields off whatever it was given.
    """

    def __init__(self, argv, stdout: str) -> None:
        self.argv = tuple(argv)
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = 0
        self.ok = True

    def lines(self):
        return [line for line in self.stdout.splitlines() if line.strip()]


class ContractRefusalTests(unittest.TestCase):
    """An action outside the emitted contracts must never reach a runner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal_path = Path(self.tmp.name) / "actions.jsonl"
        self.journal = ActionJournal(self.journal_path)
        self.sequence = 0

    def records(self):
        text = self.journal_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def run_action(self, action: ToolAction, responses=None):
        self.sequence += 1
        index = open_index(
            Path(self.tmp.name) / "index{}.sqlite3".format(self.sequence)
        )
        self.addCleanup(index.close)
        index.begin_generation(VolumeIdentity(device=1, uuid="U"), event_id=1)
        item = build_tool_item(action)
        index.record_recommendation(item)
        index.complete_generation()
        # Keyed by the action's own tool so a refusal can never fall through to
        # a real binary: an empty mapping would send the executor to the
        # default runner and out to the machine.
        runner = RecordingRunner(responses or {})
        results = invoke_tool_recommendations(
            [item.id], index, self.journal, runners={action.tool: runner}
        )
        return results[0], runner

    def assertRefused(self, action: ToolAction) -> None:
        result, runner = self.run_action(action)

        self.assertEqual(result.outcome, ApplyOutcome.FAILED)
        self.assertEqual(runner.calls, [])
        record = self.records()[-1]
        self.assertEqual(record["outcome"], "failed")
        self.assertEqual(record["argv"], list(action.preview_argv))
        self.assertTrue(record["detail"])
        self.assertLessEqual(len(record["detail"]), 4000)

    def test_an_unknown_tool_is_refused_before_any_runner_call(self):
        self.assertRefused(
            ToolAction(
                tool="rm",
                resource="everything",
                argv=("rm", "-rf", "/"),
                preview_argv=("rm", "--version"),
            )
        )

    def test_an_unknown_resource_of_a_known_tool_is_refused(self):
        self.assertRefused(
            ToolAction(
                tool="docker",
                resource="secrets",
                argv=("docker", "secret", "prune", "-f"),
                preview_argv=DOCKER_PREVIEW_ARGV,
            )
        )

    def test_a_tampered_action_argv_is_refused(self):
        self.assertRefused(
            ToolAction(
                tool="docker",
                resource="build-cache",
                argv=("docker", "image", "prune", "-a"),
                preview_argv=DOCKER_PREVIEW_ARGV,
            )
        )

    def test_a_tampered_preview_argv_is_refused(self):
        self.assertRefused(
            ToolAction(
                tool="docker",
                resource="build-cache",
                argv=DOCKER_PRUNE_ARGV,
                preview_argv=("docker", "system", "prune", "-f"),
            )
        )

    def test_a_tampered_brew_argv_is_refused(self):
        self.assertRefused(
            ToolAction(
                tool="brew",
                resource="cleanup",
                argv=("brew", "cleanup", "-s", "--prune=all"),
                preview_argv=("brew", "cleanup", "-n"),
            )
        )

    def test_an_sdkmanager_argv_naming_another_package_is_refused(self):
        self.assertRefused(
            ToolAction(
                tool="sdkmanager",
                resource="platforms;android-33",
                argv=("sdkmanager", "--uninstall", "platforms;android-34"),
                preview_argv=("sdkmanager", "--list_installed"),
            )
        )

    def test_an_avdmanager_argv_naming_another_avd_is_refused(self):
        self.assertRefused(
            ToolAction(
                tool="avdmanager",
                resource="Pixel_7",
                argv=("avdmanager", "delete", "avd", "-n", "Pixel_8"),
                preview_argv=("avdmanager", "list", "avd"),
            )
        )

    def test_a_simctl_argv_naming_another_device_is_refused(self):
        self.assertRefused(
            ToolAction(
                tool="simctl",
                resource="AAAAAAAA-1111-2222-3333-444444444444",
                argv=(XCRUN, "simctl", "delete", "BBBBBBBB-1111-2222-3333-444444444444"),
                preview_argv=(XCRUN, "simctl", "list", "--json", "devices"),
            )
        )

    def test_a_device_action_paired_with_the_runtimes_preview_is_refused(self):
        # One tool name carries two contracts. Pairing a device deletion with
        # the runtime listing would let a preview describe an effect the action
        # does not have.
        self.assertRefused(
            ToolAction(
                tool="simctl",
                resource="AAAAAAAA-1111-2222-3333-444444444444",
                argv=(XCRUN, "simctl", "delete", "AAAAAAAA-1111-2222-3333-444444444444"),
                preview_argv=(XCRUN, "simctl", "runtime", "list", "-j"),
            )
        )

    def test_a_runtime_action_paired_with_the_devices_preview_is_refused(self):
        self.assertRefused(
            ToolAction(
                tool="simctl",
                resource="com.apple.CoreSimulator.SimRuntime.iOS-17-0",
                argv=(
                    XCRUN,
                    "simctl",
                    "runtime",
                    "delete",
                    "com.apple.CoreSimulator.SimRuntime.iOS-17-0",
                ),
                preview_argv=(XCRUN, "simctl", "list", "--json", "devices"),
            )
        )

    def test_an_sdkmanager_argv_with_extra_arguments_is_refused(self):
        self.assertRefused(
            ToolAction(
                tool="sdkmanager",
                resource="platforms;android-33",
                argv=(
                    "sdkmanager",
                    "--uninstall",
                    "platforms;android-33",
                    "platforms;android-34",
                ),
                preview_argv=("sdkmanager", "--list_installed"),
            )
        )

    def test_a_refusal_names_the_contract_set_that_refused_it(self):
        # A journal read months later has to distinguish "this build allowed
        # nothing of the kind" from "the tool declined". Naming the contract
        # set makes the refusing authority, and its version, part of the
        # evidence rather than something to be inferred.
        self.assertRefused(
            ToolAction(
                tool="docker",
                resource="secrets",
                argv=("docker", "secret", "prune", "-f"),
                preview_argv=DOCKER_PREVIEW_ARGV,
            )
        )

        self.assertIn(
            "contract set v{}".format(CONTRACT_VERSION), self.records()[-1]["detail"]
        )

    def test_an_option_like_avd_name_is_refused(self):
        # The vector matches its template exactly, so vector comparison alone
        # cannot catch this: the tampering is in the identity the template is
        # built from, which arrives here as an argument to `avdmanager`.
        self.assertRefused(
            ToolAction(
                tool="avdmanager",
                resource="--force",
                argv=("avdmanager", "delete", "avd", "-n", "--force"),
                preview_argv=("avdmanager", "list", "avd"),
            )
        )

    def test_an_option_like_sdk_package_is_refused(self):
        self.assertRefused(
            ToolAction(
                tool="sdkmanager",
                resource="--uninstall",
                argv=("sdkmanager", "--uninstall", "--uninstall"),
                preview_argv=("sdkmanager", "--list_installed"),
            )
        )

    def test_an_option_like_simctl_resource_is_refused(self):
        self.assertRefused(
            ToolAction(
                tool="simctl",
                resource="--all",
                argv=(XCRUN, "simctl", "runtime", "delete", "--all"),
                preview_argv=(XCRUN, "simctl", "runtime", "list", "-j"),
            )
        )

    def test_an_empty_resource_is_refused(self):
        self.assertRefused(
            ToolAction(
                tool="sdkmanager",
                resource="",
                argv=("sdkmanager", "--uninstall", ""),
                preview_argv=("sdkmanager", "--list_installed"),
            )
        )

    def test_a_resource_carrying_a_newline_is_refused(self):
        # No parser this program owns can produce one: `parse_sdk_packages`
        # reads line by line. An identity that could not have been observed is
        # not an identity to act on.
        self.assertRefused(
            ToolAction(
                tool="sdkmanager",
                resource="platforms;android-33\nplatforms;android-34",
                argv=(
                    "sdkmanager",
                    "--uninstall",
                    "platforms;android-33\nplatforms;android-34",
                ),
                preview_argv=("sdkmanager", "--list_installed"),
            )
        )

    def test_a_padded_resource_is_refused(self):
        self.assertRefused(
            ToolAction(
                tool="avdmanager",
                resource=" Pixel_7 ",
                argv=("avdmanager", "delete", "avd", "-n", " Pixel_7 "),
                preview_argv=("avdmanager", "list", "avd"),
            )
        )

    def test_every_emitted_contract_reaches_its_own_preview(self):
        allowed = (
            (
                ToolAction(
                    tool="sdkmanager",
                    resource="platforms;android-33",
                    argv=("sdkmanager", "--uninstall", "platforms;android-33"),
                    preview_argv=("sdkmanager", "--list_installed"),
                ),
                ("sdkmanager", "--list_installed"),
            ),
            (
                ToolAction(
                    tool="avdmanager",
                    resource="Pixel_7",
                    argv=("avdmanager", "delete", "avd", "-n", "Pixel_7"),
                    preview_argv=("avdmanager", "list", "avd"),
                ),
                ("avdmanager", "list", "avd"),
            ),
            (
                ToolAction(
                    tool="simctl",
                    resource="AAAAAAAA-1111-2222-3333-444444444444",
                    argv=(
                        XCRUN,
                        "simctl",
                        "delete",
                        "AAAAAAAA-1111-2222-3333-444444444444",
                    ),
                    preview_argv=(XCRUN, "simctl", "list", "--json", "devices"),
                ),
                (XCRUN, "simctl", "list", "--json", "devices"),
            ),
            (
                ToolAction(
                    tool="simctl",
                    resource="com.apple.CoreSimulator.SimRuntime.iOS-17-0",
                    argv=(
                        XCRUN,
                        "simctl",
                        "runtime",
                        "delete",
                        "com.apple.CoreSimulator.SimRuntime.iOS-17-0",
                    ),
                    preview_argv=(XCRUN, "simctl", "runtime", "list", "-j"),
                ),
                (XCRUN, "simctl", "runtime", "list", "-j"),
            ),
            (
                ToolAction(
                    tool="docker",
                    resource="volumes",
                    argv=("docker", "volume", "prune", "-f"),
                    preview_argv=DOCKER_PREVIEW_ARGV,
                ),
                DOCKER_PREVIEW_ARGV,
            ),
        )

        for action, preview in allowed:
            with self.subTest(resource=action.resource):
                _, runner = self.run_action(
                    action, {preview: ok(preview, "")}
                )

                # The preview ran, so the contract was allowed; the action did
                # not, because these previews report nothing to reclaim.
                self.assertEqual(runner.calls, [preview])
                self.assertNotIn(tuple(action.argv), runner.calls)


class PreviewProvenanceTests(unittest.TestCase):
    """A preview whose output cannot be shown to be its own never authorises."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index = open_index(Path(self.tmp.name) / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.index.begin_generation(VolumeIdentity(device=1, uuid="U"), event_id=1)
        self.item = build_item()
        self.index.record_recommendation(self.item)
        self.index.complete_generation()
        self.journal_path = Path(self.tmp.name) / "actions.jsonl"
        self.journal = ActionJournal(self.journal_path)

    def records(self):
        text = self.journal_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def refuse(self, preview_result):
        runner = RecordingRunner({DOCKER_PREVIEW_ARGV: preview_result})

        results = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.FAILED)
        self.assertEqual(runner.calls, [DOCKER_PREVIEW_ARGV])
        self.assertNotIn(DOCKER_PRUNE_ARGV, runner.calls)
        record = self.records()[-1]
        self.assertEqual(record["outcome"], "failed")
        self.assertEqual(record["argv"], list(DOCKER_PREVIEW_ARGV))
        self.assertTrue(record["detail"])
        self.assertLessEqual(len(record["detail"]), 4000)

    def test_a_preview_from_another_command_never_executes(self):
        self.refuse(
            ToolResult(
                argv=("docker", "system", "df"),
                stdout=PREVIEW,
                stderr="",
                exit_code=0,
            )
        )

    def test_a_preview_with_empty_provenance_never_executes(self):
        self.refuse(ToolResult(argv=(), stdout=PREVIEW, stderr="", exit_code=0))

    def test_a_preview_with_malformed_provenance_never_executes(self):
        self.refuse(
            ToolResult(
                argv=("docker", "system", "df", "--format", "{{json .}}\x00"),
                stdout=PREVIEW,
                stderr="",
                exit_code=0,
            )
        )

    def test_a_preview_that_is_not_a_tool_result_never_executes(self):
        self.refuse(NotAToolResult(DOCKER_PREVIEW_ARGV, PREVIEW))

    def test_a_preview_that_exited_non_zero_never_executes(self):
        self.refuse(
            ToolResult(
                argv=DOCKER_PREVIEW_ARGV,
                stdout=PREVIEW,
                stderr="Cannot connect to the Docker daemon",
                exit_code=1,
            )
        )

    def test_an_absolute_preview_path_is_accepted_as_the_same_command(self):
        # `inventory_argv_matches` treats an absolute executable whose basename
        # is the requested bare name as the same command; the executor must not
        # be stricter than the check it delegates to.
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ToolResult(
                    argv=("/usr/local/bin/docker",) + DOCKER_PREVIEW_ARGV[1:],
                    stdout=PREVIEW,
                    stderr="",
                    exit_code=0,
                ),
                DOCKER_PRUNE_ARGV: ok(DOCKER_PRUNE_ARGV, ""),
            }
        )

        results = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.INVOKED)
        self.assertIn(DOCKER_PRUNE_ARGV, runner.calls)


class EmittedContractsAreAcceptedTests(unittest.TestCase):
    """Every action an analyzer emits must be one the executor will run.

    The contract table restates vectors the analyzers also state. Hand-written
    examples alone would let the two drift apart in silence -- a drift whose
    symptom is a recommendation the scan offers and the executor then refuses,
    with no test failing. These feed the analyzers' own output back in.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_every_docker_class_the_analyzer_emits_is_contracted(self):
        payload = "\n".join(
            json.dumps(
                {"Type": docker_type, "Reclaimable": "1GB", "Size": "2GB"}
            )
            for docker_type in ("Build Cache", "Images", "Containers", "Local Volumes")
        )
        runner = RecordingRunner(
            {DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, payload)}
        )

        items, reason = analyze_docker(runner, generation=1, home=HOME)

        self.assertIsNone(reason)
        self.assertEqual(len(items), 4)
        for item in items:
            with self.subTest(resource=item.tool_action.resource):
                self.assertIsNone(contract_refusal(item.tool_action))

    def test_the_homebrew_cleanup_the_analyzer_emits_is_contracted(self):
        preview = ("brew", "cleanup", "-n")
        runner = RecordingRunner({preview: ok(preview, BREW_PREVIEW)})

        items, reason = analyze_homebrew(runner, generation=1, home=HOME)

        self.assertIsNone(reason)
        self.assertEqual(len(items), 1)
        self.assertIsNone(contract_refusal(items[0].tool_action))

    def test_the_android_actions_the_analyzer_emits_are_contracted(self):
        sdk_root = Path(self.tmp.name) / "sdk"
        platform = sdk_root / "platforms" / "android-33"
        platform.mkdir(parents=True)
        (platform / "android.jar").write_text("x", encoding="utf-8")
        avd_dir = Path(self.tmp.name) / "Pixel_7.avd"
        avd_dir.mkdir()
        (avd_dir / "config.ini").write_text("x", encoding="utf-8")
        sdk_stdout = (
            "Installed packages:\n"
            "  Path | Version | Description | Location\n"
            "  ------- | ------- | ------- | -------\n"
            "  platforms;android-33 | 2 | Android SDK Platform 33 | "
            "platforms/android-33\n"
        )
        avd_stdout = (
            "Available Android Virtual Devices:\n"
            "    Name: Pixel_7\n"
            "    Path: {}\n"
            "  Target: Android 14\n"
        ).format(avd_dir)
        sdk_runner = RecordingRunner({SDK_LIST_ARGV: ok(SDK_LIST_ARGV, sdk_stdout)})
        avd_runner = RecordingRunner({AVD_LIST_ARGV: ok(AVD_LIST_ARGV, avd_stdout)})

        items, reason = analyze_android(
            sdk_runner, avd_runner, generation=1, sdk_root=sdk_root
        )

        self.assertIsNone(reason)
        self.assertEqual(
            sorted(item.tool_action.tool for item in items),
            ["avdmanager", "sdkmanager"],
        )
        for item in items:
            with self.subTest(tool=item.tool_action.tool):
                self.assertIsNone(contract_refusal(item.tool_action))


# ---------------------------------------------------------------------------
# Batch B: the current effect is revalidated per tool, from that tool's own
# preview, using the same semantics the analyzer used to emit the item.
# ---------------------------------------------------------------------------

UDID_A = "AAAAAAAA-1111-2222-3333-444444444444"
UDID_B = "AAAAAAAA-1111-2222-3333-444444444445"
RUNTIME_A = "com.apple.CoreSimulator.SimRuntime.iOS-17-0"
RUNTIME_B = "com.apple.CoreSimulator.SimRuntime.iOS-17-0-1"
DEVICE_SUBPATH = "Library/Developer/CoreSimulator/Devices"

SDK_HEADER = (
    "Installed packages:\n"
    "  Path | Version | Description | Location\n"
    "  ------- | ------- | ------- | -------\n"
)


def sdk_stdout(rows) -> str:
    """Render sdkmanager's installed-package table for the given rows."""
    return SDK_HEADER + "".join("  {} | {} | {} | {}\n".format(*row) for row in rows)


def avd_stdout(loadable=(), unloadable=()) -> str:
    """Render avdmanager's listing, including its unloadable section."""
    text = "Available Android Virtual Devices:\n"
    for name, path, target in loadable:
        text += "    Name: {}\n    Path: {}\n  Target: {}\n".format(name, path, target)
    if unloadable:
        text += "The following Android Virtual Devices could not be loaded:\n"
        for name, path, error in unloadable:
            text += "    Name: {}\n    Path: {}\n   Error: {}\n".format(
                name, path, error
            )
    return text


def devices_json(buckets) -> str:
    """Render `simctl list --json devices` for a mapping of runtime -> devices."""
    return json.dumps({"devices": buckets})


def device_entry(
    udid: str = UDID_A,
    name: str = "iPhone 15",
    state: str = "Shutdown",
    is_available: bool = False,
    data_size: int = 2000000000,
    log_size: int = 0,
):
    return {
        "udid": udid,
        "name": name,
        "state": state,
        "isAvailable": is_available,
        "dataPathSize": data_size,
        "logPathSize": log_size,
    }


def runtimes_json(entries) -> str:
    """Render `simctl runtime list -j` in its list form.

    The list form is used deliberately: the object form is keyed by identifier
    and so cannot express the duplicate identity this suite has to cover.
    """
    return json.dumps({"runtimes": list(entries)})


def runtime_entry(
    identifier: str = RUNTIME_A,
    path: str = "",
    deletable: bool = True,
    size_bytes: int = 6000000000,
    name: str = "iOS 17.0",
):
    return {
        "identifier": identifier,
        "runtimeIdentifier": identifier,
        "name": name,
        "version": "17.0",
        "build": "21A328",
        "deletable": deletable,
        "sizeBytes": size_bytes,
        "path": path,
        "state": "Ready",
    }


def build_sdk_item(root: Path, location: Path, resource="platforms;android-33"):
    """Mirror what analyze_android emits for an installed SDK package."""
    return Recommendation(
        detector_id="android-platform",
        category="tool-managed",
        label="Android platform 2",
        path=location,
        action=ActionKind.INVOKE_TOOL,
        allocated_bytes=4096,
        reclaimable_bytes=4096,
        confidence=Confidence.STRONG,
        restoration=RestorationCost.REDOWNLOAD,
        selected_by_default=False,
        evidence=(),
        safety_root=root,
        reason="",
        generation=1,
        tool_action=ToolAction(
            tool="sdkmanager",
            resource=resource,
            argv=("sdkmanager", "--uninstall", resource),
            preview_argv=SDK_LIST_ARGV,
            reported="2",
        ),
    )


def build_avd_item(location: Path, name: str = "Pixel_7"):
    return Recommendation(
        detector_id="android-avd",
        category="tool-managed",
        label="Android virtual device {}".format(name),
        path=location,
        action=ActionKind.INVOKE_TOOL,
        allocated_bytes=4096,
        reclaimable_bytes=4096,
        confidence=Confidence.STRONG,
        restoration=RestorationCost.EXTERNAL_STATE,
        selected_by_default=False,
        evidence=(),
        safety_root=location.parent,
        reason="",
        generation=1,
        tool_action=ToolAction(
            tool="avdmanager",
            resource=name,
            argv=("avdmanager", "delete", "avd", "-n", name),
            preview_argv=AVD_LIST_ARGV,
            reported="Android 14",
        ),
    )


def build_device_item(device_root: Path, udid: str = UDID_A, path: Path = None):
    return Recommendation(
        detector_id="simulator-unavailable-device",
        category="tool-managed",
        label="Simulator device iPhone 15",
        path=path if path is not None else device_root / udid,
        action=ActionKind.INVOKE_TOOL,
        allocated_bytes=2000000000,
        reclaimable_bytes=2000000000,
        confidence=Confidence.HEURISTIC,
        restoration=RestorationCost.EXTERNAL_STATE,
        selected_by_default=False,
        evidence=(),
        safety_root=device_root,
        reason="",
        generation=1,
        tool_action=ToolAction(
            tool="simctl",
            resource=udid,
            argv=(XCRUN, "simctl", "delete", udid),
            preview_argv=SIMCTL_DEVICES_PREVIEW_ARGV,
            reported="Shutdown",
        ),
    )


def build_runtime_item(location: Path, identifier: str = RUNTIME_A):
    return Recommendation(
        detector_id="simulator-runtime",
        category="tool-managed",
        label="Simulator runtime iOS 17.0",
        path=location,
        action=ActionKind.INVOKE_TOOL,
        allocated_bytes=6000000000,
        reclaimable_bytes=6000000000,
        confidence=Confidence.HEURISTIC,
        restoration=RestorationCost.REDOWNLOAD,
        selected_by_default=False,
        evidence=(),
        safety_root=location.parent,
        reason="",
        generation=1,
        tool_action=ToolAction(
            tool="simctl",
            resource=identifier,
            argv=(XCRUN, "simctl", "runtime", "delete", identifier),
            preview_argv=SIMCTL_RUNTIMES_PREVIEW_ARGV,
            reported="6000000000 bytes",
        ),
    )


class SimctlInventoryRunner:
    """The string-returning runner `analyze_simulator` drives simctl through.

    It is a different protocol from `ToolRunner` on purpose: the analyzer and
    the executor reach simctl by different routes, which is exactly where a
    drift between what the scan offered and what apply will accept could hide.
    """

    def __init__(self, devices: str, runtimes: str) -> None:
        self.devices = devices
        self.runtimes = runtimes
        self.calls = []

    def __call__(self, args):
        vector = tuple(args)
        self.calls.append(vector)
        if vector == ("list", "--json", "devices"):
            return self.devices
        if vector == ("runtime", "list", "-j"):
            return self.runtimes
        raise AssertionError("unexpected simctl args {}".format(vector))


class ToolEffectCase(unittest.TestCase):
    """Run one recommendation against one fake preview and read the outcome."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Resolved, because the analyzers store resolved locations and a
        # comparison against an unresolved /var symlink would fail for a
        # reason that has nothing to do with what is under test.
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.journal_path = self.root / "actions.jsonl"
        self.journal = ActionJournal(self.journal_path)
        self.sequence = 0

    def records(self):
        text = self.journal_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def run_item(self, item, preview_stdout: str, action_stdout: str = ""):
        action = item.tool_action
        self.sequence += 1
        index = open_index(
            self.root / "index{}.sqlite3".format(self.sequence)
        )
        self.addCleanup(index.close)
        index.begin_generation(VolumeIdentity(device=1, uuid="U"), event_id=1)
        index.record_recommendation(item)
        index.complete_generation()
        runner = RecordingRunner(
            {
                tuple(action.preview_argv): ok(action.preview_argv, preview_stdout),
                tuple(action.argv): ok(action.argv, action_stdout),
            }
        )
        results = invoke_tool_recommendations(
            [item.id], index, self.journal, runners={action.tool: runner}
        )
        # One preview, always: a second look at the tool could disagree with
        # the first, and then nothing would say which one authorised the run.
        self.assertEqual(runner.calls.count(tuple(action.preview_argv)), 1)
        return results[0], runner

    def assertInvoked(self, item, preview_stdout: str, action_stdout: str = ""):
        result, runner = self.run_item(item, preview_stdout, action_stdout)

        self.assertEqual(result.outcome, ApplyOutcome.INVOKED)
        self.assertIn(tuple(item.tool_action.argv), runner.calls)
        return result

    def assertSkipped(self, item, preview_stdout: str):
        result, runner = self.run_item(item, preview_stdout)

        self.assertEqual(result.outcome, ApplyOutcome.SKIPPED)
        self.assertNotIn(tuple(item.tool_action.argv), runner.calls)
        self.assertEqual(result.reclaimable_bytes, 0)
        return result

    def assertBoundedFailure(self, item, preview_stdout: str):
        result, runner = self.run_item(item, preview_stdout)

        self.assertEqual(result.outcome, ApplyOutcome.FAILED)
        self.assertNotIn(tuple(item.tool_action.argv), runner.calls)
        record = self.records()[-1]
        self.assertEqual(record["outcome"], "failed")
        self.assertEqual(record["argv"], list(item.tool_action.preview_argv))
        self.assertTrue(record["detail"])
        self.assertLessEqual(len(record["detail"]), 4000)
        return result


def docker_line(docker_type: str, reclaimable: str, size: str) -> str:
    return json.dumps(
        {
            "Type": docker_type,
            "Reclaimable": reclaimable,
            "Size": size,
            "TotalCount": "1",
            "Active": "0",
        }
    )


class DockerCurrentEffectTests(ToolEffectCase):
    """Docker's figure is per resource class, and only one line may state it."""

    def item(self):
        return build_item()

    def test_the_class_line_authorises_with_its_own_current_figure(self):
        result = self.assertInvoked(
            self.item(), docker_line("Build Cache", "3.5GB", "10GB") + "\n"
        )

        # The figure carried forward is the one read just now, not the one the
        # scan stored: acting on the stale 7.499GB would report a reclaim that
        # Docker never agreed to.
        self.assertEqual(result.reclaimable_bytes, 3500000000)

    def test_a_second_line_for_the_same_class_is_ambiguous(self):
        payload = (
            docker_line("Build Cache", "3.5GB", "10GB")
            + "\n"
            + docker_line("Build Cache", "9GB", "10GB")
            + "\n"
        )

        result = self.assertBoundedFailure(self.item(), payload)

        self.assertIn("build-cache", result.error)

    def test_a_duplicate_of_another_class_leaves_this_one_alone(self):
        # Independent records survive: the ambiguity belongs to Images, and
        # nothing about the build cache line became less certain because of it.
        payload = "\n".join(
            (
                docker_line("Images", "1GB", "2GB"),
                docker_line("Images", "2GB", "2GB"),
                docker_line("Build Cache", "3.5GB", "10GB"),
            )
        )

        self.assertInvoked(self.item(), payload)

    def test_an_unreadable_line_beside_the_class_line_is_ignored(self):
        payload = "not json at all\n" + docker_line("Build Cache", "3.5GB", "10GB")

        self.assertInvoked(self.item(), payload)

    def test_a_near_match_type_is_not_this_class(self):
        # "Build Cache Extra" contains the whole of "Build Cache". Anything
        # matching on containment would prune this machine's build cache on the
        # strength of a class it has never heard of.
        payload = docker_line("Build Cache Extra", "9GB", "10GB")

        self.assertSkipped(self.item(), payload)

    def test_an_absent_class_reclaims_nothing(self):
        self.assertSkipped(self.item(), docker_line("Images", "9GB", "10GB"))

    def test_a_canonical_zero_reclaims_nothing(self):
        self.assertSkipped(self.item(), docker_line("Build Cache", "0B", "10GB"))

    def test_an_unreadable_figure_is_not_treated_as_zero(self):
        # `parse_tool_size` answers 0 for both "0B" and "plenty". Collapsing
        # the two would turn "Docker said something we cannot read" into the
        # confident claim that there is nothing to reclaim.
        self.assertBoundedFailure(
            self.item(), docker_line("Build Cache", "plenty", "10GB")
        )

    def test_a_missing_figure_is_refused(self):
        payload = json.dumps({"Type": "Build Cache", "Size": "10GB"})

        self.assertBoundedFailure(self.item(), payload)

    def test_a_figure_larger_than_the_class_total_is_refused(self):
        self.assertBoundedFailure(
            self.item(), docker_line("Build Cache", "11GB", "10GB")
        )

    def test_the_effect_carries_docker_s_own_words(self):
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(
                    DOCKER_PREVIEW_ARGV, docker_line("Build Cache", "3.5GB", "10GB")
                )
            }
        )

        effect = current_tool_effect(self.item(), runner)

        self.assertIsInstance(effect, CurrentEffect)
        self.assertEqual(effect.reclaimable_bytes, 3500000000)
        self.assertEqual(effect.reported, "3.5GB")
        self.assertIn("3.5GB", effect.detail)


class BrewCurrentEffectTests(ToolEffectCase):
    """Only brew's own unique, positive, well-formed total authorises cleanup."""

    def item(self):
        return build_brew_item()

    def test_the_unique_total_authorises_with_its_current_figure(self):
        result = self.assertInvoked(self.item(), BREW_PREVIEW)

        self.assertEqual(result.reclaimable_bytes, 1200000000)

    def test_two_totals_are_ambiguous(self):
        payload = BREW_PREVIEW + (
            "==> This operation would free approximately 9GB of disk space.\n"
        )

        self.assertBoundedFailure(self.item(), payload)

    def test_removals_with_an_unreadable_total_are_refused(self):
        payload = (
            "Would remove: /opt/homebrew/Cellar/git/2.44.0\n"
            "==> This operation would free approximately some disk space.\n"
        )

        self.assertBoundedFailure(self.item(), payload)

    def test_removals_with_a_zero_total_are_refused(self):
        payload = (
            "Would remove: /opt/homebrew/Cellar/git/2.44.0\n"
            "==> This operation would free approximately 0B of disk space.\n"
        )

        self.assertBoundedFailure(self.item(), payload)

    def test_no_removals_and_no_total_reclaims_nothing(self):
        self.assertSkipped(self.item(), "")

    def test_the_effect_carries_brew_s_own_count(self):
        runner = RecordingRunner(
            {("brew", "cleanup", "-n"): ok(("brew", "cleanup", "-n"), BREW_PREVIEW)}
        )

        effect = current_tool_effect(self.item(), runner)

        self.assertEqual(effect.reclaimable_bytes, 1200000000)
        self.assertEqual(effect.reported, "2 items")


class SdkmanagerCurrentEffectTests(ToolEffectCase):
    """The package must still be installed, under the SDK root it was found in."""

    def setUp(self):
        super().setUp()
        self.sdk_root = self.root / "sdk"
        self.location = self.sdk_root / "platforms" / "android-33"
        self.location.mkdir(parents=True)
        (self.location / "android.jar").write_text("x" * 4096, encoding="utf-8")

    def item(self, resource="platforms;android-33", location=None):
        return build_sdk_item(
            self.sdk_root, location or self.location, resource=resource
        )

    def rows(self, *rows):
        return sdk_stdout(rows)

    def installed(self, package="platforms;android-33", location="platforms/android-33"):
        return self.rows((package, "2", "Android SDK Platform 33", location))

    def test_the_installed_package_authorises_with_a_measured_size(self):
        result = self.assertInvoked(self.item(), self.installed())

        # Measured now, at the location sdkmanager itself reports: the stored
        # figure is a scan-time observation, not a current one.
        self.assertEqual(result.reclaimable_bytes, path_size(self.location))
        self.assertGreater(result.reclaimable_bytes, 0)

    def test_a_second_row_for_the_same_package_is_ambiguous(self):
        payload = self.rows(
            ("platforms;android-33", "2", "Android SDK Platform 33", "platforms/android-33"),
            ("platforms;android-33", "3", "Android SDK Platform 33", "platforms/android-33"),
        )

        self.assertBoundedFailure(self.item(), payload)

    def test_a_near_match_package_is_not_this_package(self):
        self.assertSkipped(
            self.item(),
            self.installed(
                package="platforms;android-330", location="platforms/android-330"
            ),
        )

    def test_a_package_no_longer_listed_reclaims_nothing(self):
        self.assertSkipped(self.item(), SDK_HEADER)

    def test_a_row_outside_the_installed_section_is_not_an_installation(self):
        payload = (
            "Available Packages:\n"
            "  Path | Version | Description | Location\n"
            "  platforms;android-33 | 2 | Android SDK Platform 33 | "
            "platforms/android-33\n"
        )

        self.assertSkipped(self.item(), payload)

    def test_a_location_escaping_the_sdk_root_is_refused(self):
        payload = self.installed(location="../../../etc")

        self.assertBoundedFailure(self.item(), payload)

    def test_an_absolute_location_is_refused(self):
        payload = self.installed(location=str(self.root / "elsewhere"))

        self.assertBoundedFailure(self.item(), payload)

    def test_a_location_that_moved_since_the_scan_is_refused(self):
        # The stored path is what the user was shown a size for. If sdkmanager
        # now places the package somewhere else, the recommendation describes a
        # thing that no longer exists, whatever the identity says.
        payload = self.installed(location="platforms/android-34")

        self.assertBoundedFailure(self.item(), payload)

    def test_a_location_missing_from_disk_reclaims_nothing(self):
        gone = self.sdk_root / "platforms" / "android-35"
        payload = self.installed(location="platforms/android-35")

        self.assertSkipped(self.item(location=gone), payload)


class AvdmanagerCurrentEffectTests(ToolEffectCase):
    """The AVD must still be listed, by name, at the path it was measured at."""

    def setUp(self):
        super().setUp()
        self.location = self.root / "avd" / "Pixel_7.avd"
        self.location.mkdir(parents=True)
        (self.location / "userdata.img").write_text("x" * 4096, encoding="utf-8")

    def item(self, name="Pixel_7", location=None):
        return build_avd_item(location or self.location, name=name)

    def listed(self, name="Pixel_7", path=None):
        return avd_stdout(
            loadable=((name, str(path or self.location), "Android 14"),)
        )

    def test_the_listed_avd_authorises_with_a_measured_size(self):
        result = self.assertInvoked(self.item(), self.listed())

        self.assertEqual(result.reclaimable_bytes, path_size(self.location))
        self.assertGreater(result.reclaimable_bytes, 0)

    def test_an_unloadable_avd_is_still_this_avd(self):
        payload = avd_stdout(
            unloadable=(("Pixel_7", str(self.location), "Missing system image"),)
        )

        self.assertInvoked(self.item(), payload)

    def test_the_same_name_in_both_sections_is_ambiguous(self):
        payload = avd_stdout(
            loadable=(("Pixel_7", str(self.location), "Android 14"),),
            unloadable=(("Pixel_7", str(self.location), "Missing system image"),),
        )

        self.assertBoundedFailure(self.item(), payload)

    def test_a_near_match_name_is_not_this_avd(self):
        self.assertSkipped(self.item(), self.listed(name="Pixel_70"))

    def test_an_avd_no_longer_listed_reclaims_nothing(self):
        self.assertSkipped(self.item(), "Available Android Virtual Devices:\n")

    def test_a_path_that_moved_since_the_scan_is_refused(self):
        self.assertBoundedFailure(
            self.item(), self.listed(path=self.root / "avd" / "Other.avd")
        )

    def test_a_relative_path_is_refused(self):
        self.assertBoundedFailure(self.item(), self.listed(path="avd/Pixel_7.avd"))

    def test_a_path_missing_from_disk_reclaims_nothing(self):
        gone = self.root / "avd" / "Gone.avd"

        self.assertSkipped(
            self.item(location=gone), self.listed(path=gone)
        )


class SimctlDeviceCurrentEffectTests(ToolEffectCase):
    """A device deletion needs that exact UDID, still unavailable and shut down."""

    def setUp(self):
        super().setUp()
        self.device_root = self.root / DEVICE_SUBPATH

    def item(self, udid=UDID_A, path=None):
        return build_device_item(self.device_root, udid=udid, path=path)

    def listing(self, *entries, runtime=RUNTIME_A):
        return devices_json({runtime: list(entries)})

    def test_the_named_device_authorises_with_simctl_s_current_size(self):
        result = self.assertInvoked(
            self.item(),
            self.listing(device_entry(data_size=1500000000, log_size=500000)),
        )

        self.assertEqual(result.reclaimable_bytes, 1500500000)

    def test_the_same_udid_in_two_runtimes_is_ambiguous(self):
        payload = devices_json(
            {
                RUNTIME_A: [device_entry()],
                RUNTIME_B: [device_entry()],
            }
        )

        self.assertBoundedFailure(self.item(), payload)

    def test_a_near_match_udid_is_not_this_device(self):
        self.assertSkipped(self.item(), self.listing(device_entry(udid=UDID_B)))

    def test_a_device_simctl_no_longer_lists_reclaims_nothing(self):
        self.assertSkipped(self.item(), self.listing())

    def test_a_device_that_became_available_reclaims_nothing(self):
        self.assertSkipped(
            self.item(), self.listing(device_entry(is_available=True))
        )

    def test_a_booted_device_reclaims_nothing(self):
        self.assertSkipped(self.item(), self.listing(device_entry(state="Booted")))

    def test_a_zero_sized_device_reclaims_nothing(self):
        self.assertSkipped(
            self.item(), self.listing(device_entry(data_size=0, log_size=0))
        )

    def test_a_malformed_inventory_is_refused(self):
        self.assertBoundedFailure(self.item(), "{not json")

    def test_a_stored_path_that_is_not_this_device_s_is_refused(self):
        # The UDID is the argument, but the path is what the user was shown.
        # A stored path pointing at another device means the two disagree, and
        # a deletion would not be the thing that was reviewed.
        self.assertBoundedFailure(
            self.item(path=self.device_root / UDID_B),
            self.listing(device_entry()),
        )

    def test_a_udid_in_another_form_is_refused(self):
        # simctl would accept the lowercase form, but the contract's argument
        # is the stored string; acting on a differently-spelled identity is a
        # judgement this program is not entitled to make on its own.
        self.assertBoundedFailure(
            self.item(), self.listing(device_entry(udid=UDID_A.lower()))
        )


class SimctlRuntimeCurrentEffectTests(ToolEffectCase):
    """A runtime deletion needs that exact identifier, still marked deletable."""

    def setUp(self):
        super().setUp()
        self.location = self.root / "Runtimes" / "iOS 17.0.simruntime"

    def item(self, identifier=RUNTIME_A, location=None):
        return build_runtime_item(location or self.location, identifier=identifier)

    def listing(self, *entries):
        return runtimes_json(entries)

    def test_the_named_runtime_authorises_with_simctl_s_current_size(self):
        result = self.assertInvoked(
            self.item(),
            self.listing(runtime_entry(path=str(self.location), size_bytes=7000000000)),
        )

        self.assertEqual(result.reclaimable_bytes, 7000000000)

    def test_a_duplicate_identifier_is_ambiguous(self):
        payload = self.listing(
            runtime_entry(path=str(self.location)),
            runtime_entry(path=str(self.location), size_bytes=1000000),
        )

        self.assertBoundedFailure(self.item(), payload)

    def test_a_near_match_identifier_is_not_this_runtime(self):
        self.assertSkipped(
            self.item(),
            self.listing(runtime_entry(identifier=RUNTIME_B, path=str(self.location))),
        )

    def test_a_runtime_no_longer_deletable_reclaims_nothing(self):
        self.assertSkipped(
            self.item(),
            self.listing(runtime_entry(path=str(self.location), deletable=False)),
        )

    def test_a_runtime_no_longer_listed_reclaims_nothing(self):
        self.assertSkipped(self.item(), self.listing())

    def test_a_zero_sized_runtime_reclaims_nothing(self):
        self.assertSkipped(
            self.item(),
            self.listing(runtime_entry(path=str(self.location), size_bytes=0)),
        )

    def test_a_path_that_moved_since_the_scan_is_refused(self):
        self.assertBoundedFailure(
            self.item(),
            self.listing(runtime_entry(path=str(self.root / "Elsewhere.simruntime"))),
        )

    def test_a_relative_path_is_refused(self):
        self.assertBoundedFailure(
            self.item(), self.listing(runtime_entry(path="Runtimes/iOS 17.0.simruntime"))
        )

    def test_a_malformed_inventory_is_refused(self):
        self.assertBoundedFailure(self.item(), "{not json")


class AnalyzerEmittedEffectsRevalidateTests(ToolEffectCase):
    """What an analyzer emits must survive its own preview, unchanged.

    Batch A checked that emitted actions are contracted. That is not enough:
    the executor now re-derives the resource, the location, and the figure from
    the preview, so a recommendation the scan offers could still be refused at
    apply time -- with no test failing -- if the two derivations drift apart.
    """

    def simulator_items(self, devices: str, runtimes: str):
        analyzer_runner = SimctlInventoryRunner(devices, runtimes)
        items, reason = analyze_simulator(analyzer_runner, generation=1, home=self.root)
        self.assertIsNone(reason)
        return items

    def test_the_emitted_simulator_device_revalidates_against_its_preview(self):
        devices = devices_json({RUNTIME_A: [device_entry()]})
        runtimes = runtimes_json([])

        items = self.simulator_items(devices, runtimes)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIsNone(contract_refusal(item.tool_action))
        result = self.assertInvoked(item, devices)
        self.assertEqual(result.reclaimable_bytes, 2000000000)

    def test_the_emitted_simulator_runtime_revalidates_against_its_preview(self):
        location = self.root / "Runtimes" / "iOS 17.0.simruntime"
        devices = devices_json({})
        runtimes = runtimes_json([runtime_entry(path=str(location))])

        items = self.simulator_items(devices, runtimes)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIsNone(contract_refusal(item.tool_action))
        result = self.assertInvoked(item, runtimes)
        self.assertEqual(result.reclaimable_bytes, 6000000000)

    def test_the_emitted_android_actions_revalidate_against_their_previews(self):
        sdk_root = self.root / "sdk"
        platform = sdk_root / "platforms" / "android-33"
        platform.mkdir(parents=True)
        (platform / "android.jar").write_text("x" * 4096, encoding="utf-8")
        avd_dir = self.root / "avd" / "Pixel_7.avd"
        avd_dir.mkdir(parents=True)
        (avd_dir / "config.ini").write_text("x" * 4096, encoding="utf-8")
        sdk_text = sdk_stdout(
            (
                (
                    "platforms;android-33",
                    "2",
                    "Android SDK Platform 33",
                    "platforms/android-33",
                ),
            )
        )
        avd_text = avd_stdout(loadable=(("Pixel_7", str(avd_dir), "Android 14"),))
        items, reason = analyze_android(
            RecordingRunner({SDK_LIST_ARGV: ok(SDK_LIST_ARGV, sdk_text)}),
            RecordingRunner({AVD_LIST_ARGV: ok(AVD_LIST_ARGV, avd_text)}),
            generation=1,
            sdk_root=sdk_root,
        )

        self.assertIsNone(reason)
        self.assertEqual(len(items), 2)
        for item in items:
            with self.subTest(tool=item.tool_action.tool):
                preview = sdk_text if item.tool_action.tool == "sdkmanager" else avd_text
                self.assertInvoked(item, preview)

    def test_the_emitted_docker_classes_revalidate_against_their_preview(self):
        payload = "\n".join(
            docker_line(docker_type, "1GB", "2GB")
            for docker_type in ("Build Cache", "Images", "Containers", "Local Volumes")
        )
        items, reason = analyze_docker(
            RecordingRunner({DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, payload)}),
            generation=1,
            home=self.root,
        )

        self.assertIsNone(reason)
        self.assertEqual(len(items), 4)
        for item in items:
            with self.subTest(resource=item.tool_action.resource):
                result = self.assertInvoked(item, payload)
                self.assertEqual(result.reclaimable_bytes, 1000000000)

    def test_the_emitted_homebrew_cleanup_revalidates_against_its_preview(self):
        items, reason = analyze_homebrew(
            RecordingRunner(
                {("brew", "cleanup", "-n"): ok(("brew", "cleanup", "-n"), BREW_PREVIEW)}
            ),
            generation=1,
            home=self.root,
        )

        self.assertIsNone(reason)
        result = self.assertInvoked(items[0], BREW_PREVIEW)
        self.assertEqual(result.reclaimable_bytes, 1200000000)


class BatchCExecutionBoundaryTests(InvokeToolTests):
    """The preview authorises one bounded, provenanced action attempt."""

    def test_a_figure_must_fill_the_entire_tool_token(self):
        payload = PREVIEW.replace('"7.499GB"', '"7.499GB unexpected"')
        runner = RecordingRunner(
            {DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, payload)}
        )

        result = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )[0]

        self.assertEqual(result.outcome, ApplyOutcome.FAILED)
        self.assertEqual(runner.calls, [DOCKER_PREVIEW_ARGV])

    def test_cancellation_after_preview_prevents_the_action(self):
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW),
                DOCKER_PRUNE_ARGV: ok(DOCKER_PRUNE_ARGV, "must not run"),
            }
        )
        checks = []

        def should_cancel():
            checks.append(len(runner.calls))
            return len(runner.calls) == 1

        result = invoke_tool_recommendations(
            [self.item.id],
            self.index,
            self.journal,
            runners={"docker": runner},
            should_cancel=should_cancel,
        )[0]

        self.assertEqual(result.outcome, ApplyOutcome.SKIPPED)
        self.assertEqual(runner.calls, [DOCKER_PREVIEW_ARGV])
        self.assertEqual(self.records()[-1]["argv"], list(DOCKER_PREVIEW_ARGV))

    def test_skip_refusal_and_dry_run_record_the_exact_preview_vector(self):
        cases = (
            ("skip", PREVIEW.replace("7.499GB", "0B"), False),
            ("refusal", PREVIEW.replace("7.499GB", "unreadable"), False),
            ("dry-run", PREVIEW, True),
        )
        for name, payload, dry_run in cases:
            with self.subTest(name=name):
                runner = RecordingRunner(
                    {DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, payload)}
                )
                invoke_tool_recommendations(
                    [self.item.id],
                    self.index,
                    self.journal,
                    runners={"docker": runner},
                    dry_run=dry_run,
                )
                self.assertEqual(
                    self.records()[-1]["argv"], list(DOCKER_PREVIEW_ARGV)
                )

    def test_contract_refusal_records_the_recorded_preview_vector(self):
        item = build_tool_item(
            ToolAction(
                tool="docker",
                resource="build-cache",
                argv=("docker", "system", "prune", "-f"),
                preview_argv=DOCKER_PREVIEW_ARGV,
            )
        )
        self.index.begin_generation(VolumeIdentity(device=1, uuid="U"), event_id=2)
        self.index.record_recommendation(item)
        self.index.complete_generation()

        invoke_tool_recommendations(
            [item.id], self.index, self.journal, runners={"docker": RecordingRunner({})}
        )

        self.assertEqual(self.records()[-1]["argv"], list(DOCKER_PREVIEW_ARGV))

    def test_an_action_result_must_be_a_real_tool_result(self):
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW),
                DOCKER_PRUNE_ARGV: NotAToolResult(DOCKER_PRUNE_ARGV, "done"),
            }
        )

        result = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )[0]

        self.assertEqual(result.outcome, ApplyOutcome.FAILED)
        self.assertEqual(runner.calls.count(DOCKER_PRUNE_ARGV), 1)

    def test_an_action_result_must_have_exact_provenance(self):
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW),
                DOCKER_PRUNE_ARGV: ok(("docker", "image", "prune", "-f"), "done"),
            }
        )

        result = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )[0]

        self.assertEqual(result.outcome, ApplyOutcome.FAILED)
        self.assertEqual(runner.calls.count(DOCKER_PRUNE_ARGV), 1)

    def test_a_nonzero_action_is_not_retried_and_surfaces_bounded_output(self):
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW),
                DOCKER_PRUNE_ARGV: ToolResult(
                    argv=DOCKER_PRUNE_ARGV,
                    stdout="",
                    stderr="x" * 10000,
                    exit_code=23,
                ),
            }
        )

        result = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )[0]

        self.assertEqual(result.outcome, ApplyOutcome.FAILED)
        self.assertEqual(runner.calls.count(DOCKER_PRUNE_ARGV), 1)
        self.assertLessEqual(len(result.error), 4000)

    def test_success_has_tool_target_and_compatible_serialized_reported_output(self):
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW),
                DOCKER_PRUNE_ARGV: ok(DOCKER_PRUNE_ARGV, "y" * 10000),
            }
        )

        result = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )[0]

        self.assertEqual(result.path, "docker:build-cache")
        self.assertLessEqual(len(result.reported), 4000)
        self.assertEqual(result.to_dict()["reported"], result.reported)

    def test_long_success_output_preserves_the_owners_final_summary(self):
        summary = "Total reclaimed space: 7.499GB"
        stdout = "Starting cleanup\n{}\n{}\n".format("x" * 10000, summary)
        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW),
                DOCKER_PRUNE_ARGV: ok(DOCKER_PRUNE_ARGV, stdout),
            }
        )

        result = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )[0]

        self.assertLessEqual(len(result.reported), 4000)
        self.assertIn(summary, result.reported)
        self.assertEqual(self.records()[-1]["detail"], summary)

    def test_legacy_apply_result_construction_keeps_reported_optional(self):
        result = ApplyResult(
            recommendation_id="old",
            label="Old caller",
            path="/tmp/old",
            reclaimable_bytes=0,
            outcome=ApplyOutcome.SKIPPED,
            dry_run=True,
        )

        self.assertEqual(result.reported, "")
        self.assertEqual(result.to_dict()["reported"], "")

    def test_a_journal_exception_does_not_block_the_action_result(self):
        class BrokenJournal:
            def append(self, record):
                raise OSError("journal unavailable")

        runner = RecordingRunner(
            {
                DOCKER_PREVIEW_ARGV: ok(DOCKER_PREVIEW_ARGV, PREVIEW),
                DOCKER_PRUNE_ARGV: ok(DOCKER_PRUNE_ARGV, "done"),
            }
        )

        result = invoke_tool_recommendations(
            [self.item.id],
            self.index,
            BrokenJournal(),
            runners={"docker": runner},
        )[0]

        self.assertEqual(result.outcome, ApplyOutcome.INVOKED)
        self.assertEqual(runner.calls.count(DOCKER_PRUNE_ARGV), 1)
        self.assertEqual(result.journal_warning, "journal write failed")
        self.assertNotIn("journal unavailable", result.journal_warning)
        self.assertEqual(result.to_dict()["journal_warning"], "journal write failed")


class AndroidSdkBindingTests(unittest.TestCase):
    def test_default_sdkmanager_runner_is_bound_to_the_reviewed_sdk_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            reviewed_sdk = root / "sdk-a"
            current_sdk = root / "sdk-b"
            location = reviewed_sdk / "platforms" / "android-33"
            location.mkdir(parents=True)
            (location / "android.jar").write_bytes(b"payload")
            (reviewed_sdk / "cmdline-tools" / "latest" / "bin").mkdir(parents=True)
            (current_sdk / "cmdline-tools" / "latest" / "bin").mkdir(parents=True)
            item = build_sdk_item(reviewed_sdk, location)
            index = open_index(root / "index.sqlite3")
            self.addCleanup(index.close)
            index.begin_generation(VolumeIdentity(device=1, uuid="U"), event_id=1)
            index.record_recommendation(item)
            index.complete_generation()
            journal = ActionJournal(root / "actions.jsonl")
            preview = sdk_stdout(
                [("platforms;android-33", "2", "Android SDK Platform 33", "platforms/android-33")]
            )
            action = item.tool_action
            assert action is not None
            runner = RecordingRunner(
                {
                    SDK_LIST_ARGV: ok(SDK_LIST_ARGV, preview),
                    action.argv: ok(action.argv, "uninstalled"),
                }
            )
            bound_roots = []
            fallback_modes = []

            def bind(name, extra_dirs=(), allow_fallback=True):
                bound_roots.append(tuple(extra_dirs))
                fallback_modes.append(allow_fallback)
                return runner

            with patch("mac_dev_clean.tool_executor.find_sdk_root", return_value=current_sdk), patch(
                "mac_dev_clean.tool_executor.binary_runner", side_effect=bind
            ):
                result = invoke_tool_recommendations([item.id], index, journal)[0]

            self.assertEqual(result.outcome, ApplyOutcome.INVOKED, result.error)
            self.assertTrue(all(str(path).startswith(str(reviewed_sdk)) for path in bound_roots[0]))
            self.assertFalse(any(str(path).startswith(str(current_sdk)) for path in bound_roots[0]))
            self.assertEqual(fallback_modes, [False])


if __name__ == "__main__":
    unittest.main()
