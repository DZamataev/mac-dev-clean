from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, cast
from unittest.mock import Mock, patch

from mac_dev_clean.cli import build_parser, main
from mac_dev_clean.executor import ApplyOutcome, ApplyResult
from mac_dev_clean.index import DEFAULT_TOOL_INDEX_PATH, open_index
from mac_dev_clean.output import render_tool_table, tool_report_json
from mac_dev_clean.recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    ToolAction,
)
from mac_dev_clean.tools.registry import ToolReport, ToolStatus


def tool_recommendation(generation: int, detector_id: str = "docker-build", size: int = 10):
    return Recommendation(
        detector_id=detector_id,
        category="tool-managed",
        label="Docker build cache",
        path=Path("/tool/docker/build-cache"),
        action=ActionKind.INVOKE_TOOL,
        allocated_bytes=size,
        reclaimable_bytes=size,
        confidence=Confidence.EXACT,
        restoration=RestorationCost.REBUILD,
        selected_by_default=False,
        evidence=(Evidence("tool-report", "Docker reported this storage"),),
        safety_root=Path("/tool/docker"),
        reason="reported by Docker",
        generation=generation,
        tool_action=ToolAction(
            tool="docker",
            resource="build-cache",
            argv=("docker", "builder", "prune", "-f"),
            preview_argv=("docker", "system", "df", "--format", "{{json .}}"),
            reported="10 B",
        ),
    )


class ToolOutputTests(unittest.TestCase):
    def test_tool_json_and_human_output_are_deterministic_and_non_mutating(self):
        small = tool_recommendation(1, detector_id="small", size=1)
        tie_b = tool_recommendation(1, detector_id="tie-b", size=20)
        tie_a = tool_recommendation(1, detector_id="tie-a", size=20)
        original = [small, tie_b, tie_a]
        report = ToolReport(
            recommendations=original,
            statuses=[ToolStatus("simulator", False, "not installed"), ToolStatus("docker", True)],
        )

        payload = tool_report_json(report)
        text = render_tool_table(report)

        expected = sorted(original, key=lambda item: (-item.reclaimable_bytes, item.id))
        recommendations = cast(List[Dict[str, object]], payload["recommendations"])
        statuses = cast(List[Dict[str, object]], payload["statuses"])
        self.assertEqual(
            [entry["id"] for entry in recommendations],
            [item.id for item in expected],
        )
        self.assertEqual([entry["tool"] for entry in statuses], ["docker", "simulator"])
        self.assertEqual(payload["reclaimable_total_bytes"], 41)
        self.assertEqual(report.recommendations, original)
        self.assertTrue(all(not item.selected_by_default for item in report.recommendations))
        self.assertLess(text.index(expected[0].id), text.index(expected[-1].id))
        self.assertIn("Nothing is selected.", text)

    def test_duplicate_statuses_have_a_stable_tie_break(self):
        statuses = [
            ToolStatus("docker", False, "z reason"),
            ToolStatus("docker", True, ""),
            ToolStatus("docker", False, "a reason"),
        ]

        forward = tool_report_json(ToolReport([], statuses))
        reverse = tool_report_json(ToolReport([], list(reversed(statuses))))

        self.assertEqual(forward, reverse)

    def test_human_output_quotes_each_argv_boundary(self):
        item = tool_recommendation(1)
        item = replace(
            item,
            tool_action=ToolAction(
                tool="docker",
                resource="build-cache",
                argv=("docker", "builder", "two words", "$HOME"),
                preview_argv=("docker", "system", "df"),
            ),
        )

        text = render_tool_table(ToolReport([item], []))

        self.assertIn("runs: docker builder 'two words' '$HOME'", text)


class ToolsInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.index_path = Path(self.temp.name) / "tools.sqlite3"

    def run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_tools_json_persists_every_displayed_id_in_completed_generation(self):
        real_open = open_index
        generations = []

        def collect(generation, home):
            generations.append(generation)
            return ToolReport(
                recommendations=[tool_recommendation(generation)],
                statuses=[
                    ToolStatus("simulator", False, "not installed"),
                    ToolStatus("homebrew", True),
                    ToolStatus("docker", True),
                    ToolStatus("android", False, "not installed"),
                ],
            )

        with patch("mac_dev_clean.cli.open_index", side_effect=real_open) as opened, patch(
            "mac_dev_clean.cli.collect_tool_recommendations", side_effect=collect
        ):
            code, output, error = self.run_cli(
                ["tools", "--json", "--index", str(self.index_path)]
            )

        self.assertEqual((code, error), (0, ""))
        opened.assert_called_once_with(self.index_path)
        payload = json.loads(output)
        self.assertEqual(generations, [1])
        self.assertEqual(
            [entry["tool"] for entry in payload["statuses"]],
            ["android", "docker", "homebrew", "simulator"],
        )
        self.assertEqual(payload["reclaimable_total_bytes"], 10)
        displayed_ids = [entry["id"] for entry in payload["recommendations"]]
        self.assertEqual(len(displayed_ids), 1)

        index = real_open(self.index_path)
        try:
            self.assertEqual(index.latest_complete_generation(), 1)
            for recommendation_id in displayed_ids:
                self.assertIsNotNone(index.load_recommendation(recommendation_id))
        finally:
            index.close()
    def test_tools_empty_refresh_removes_previously_displayed_ids(self):
        real_open = open_index
        reports = []

        def collect(generation, home):
            if not reports:
                report = ToolReport([tool_recommendation(generation)], [])
            else:
                report = ToolReport([], [])
            reports.append(report)
            return report

        with patch("mac_dev_clean.cli.open_index", side_effect=real_open), patch(
            "mac_dev_clean.cli.collect_tool_recommendations", side_effect=collect
        ):
            first_code, first_output, _ = self.run_cli(
                ["tools", "--json", "--index", str(self.index_path)]
            )
            second_code, second_output, _ = self.run_cli(
                ["tools", "--json", "--index", str(self.index_path)]
            )

        old_id = json.loads(first_output)["recommendations"][0]["id"]
        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(json.loads(second_output)["recommendations"], [])
        index = real_open(self.index_path)
        try:
            self.assertEqual(index.latest_complete_generation(), 2)
            self.assertIsNone(index.load_recommendation(old_id))
        finally:
            index.close()

    def test_tools_collection_failure_retains_prior_generation_and_is_bounded(self):
        real_open = open_index

        def initial(generation, home):
            return ToolReport([tool_recommendation(generation)], [])

        with patch("mac_dev_clean.cli.open_index", side_effect=real_open), patch(
            "mac_dev_clean.cli.collect_tool_recommendations", side_effect=initial
        ):
            _, first_output, _ = self.run_cli(
                ["tools", "--json", "--index", str(self.index_path)]
            )
        old_id = json.loads(first_output)["recommendations"][0]["id"]

        with patch("mac_dev_clean.cli.open_index", side_effect=real_open), patch(
            "mac_dev_clean.cli.collect_tool_recommendations",
            side_effect=RuntimeError("secret tool stderr /Users/private"),
        ):
            code, output, error = self.run_cli(
                ["tools", "--json", "--index", str(self.index_path)]
            )

        self.assertEqual((code, error), (1, ""))
        self.assertEqual(
            json.loads(output), {"error": "Could not collect tool-managed storage."}
        )
        self.assertNotIn("secret", output)
        index = real_open(self.index_path)
        try:
            self.assertEqual(index.latest_complete_generation(), 1)
            self.assertIsNotNone(index.load_recommendation(old_id))
        finally:
            index.close()

    def test_tools_persistence_failure_abandons_and_closes(self):
        index = Mock()
        index.begin_generation.return_value = 4
        index.record_recommendation.side_effect = OSError("secret persistence path")
        report = ToolReport([tool_recommendation(4)], [])
        with patch("mac_dev_clean.cli.open_index", return_value=index), patch(
            "mac_dev_clean.cli.collect_tool_recommendations", return_value=report
        ):
            code, output, _ = self.run_cli(
                ["tools", "--json", "--index", str(self.index_path)]
            )

        self.assertEqual(code, 1)
        index.abandon_generation.assert_called_once_with()
        index.complete_generation.assert_not_called()
        index.close.assert_called_once_with()
        self.assertNotIn("secret", output)

    def test_tools_close_failure_is_safe_and_does_not_claim_success(self):
        index = Mock()
        index.begin_generation.return_value = 3
        index.close.side_effect = OSError("secret close path")
        with patch("mac_dev_clean.cli.open_index", return_value=index), patch(
            "mac_dev_clean.cli.collect_tool_recommendations",
            return_value=ToolReport([], []),
        ):
            code, output, error = self.run_cli(["tools", "--json"])

        self.assertEqual((code, error), (1, ""))
        self.assertEqual(
            json.loads(output), {"error": "Could not collect tool-managed storage."}
        )
        self.assertNotIn("secret", output)

    def test_tools_default_is_the_dedicated_index(self):
        index = Mock()
        index.begin_generation.return_value = 1
        with patch("mac_dev_clean.cli.open_index", return_value=index) as opened, patch(
            "mac_dev_clean.cli.collect_tool_recommendations",
            return_value=ToolReport([], []),
        ):
            code, _, _ = self.run_cli(["tools", "--json"])

        self.assertEqual(code, 0)
        opened.assert_called_once_with(DEFAULT_TOOL_INDEX_PATH.expanduser())


class ToolsApplyTests(unittest.TestCase):
    def run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_tools_apply_uses_exact_override_id_once_and_honors_dry_run(self):
        index = Mock()
        journal = Mock()
        result = ApplyResult(
            recommendation_id="exact-display-id",
            label="Docker build cache",
            path="docker:build-cache",
            reclaimable_bytes=10,
            outcome=ApplyOutcome.SKIPPED,
            dry_run=True,
            reported="10 B",
            error="dry run",
            journal_warning="journal write failed",
        )
        with patch("mac_dev_clean.cli.open_index", return_value=index) as opened, patch(
            "mac_dev_clean.cli.open_journal", return_value=journal
        ) as journal_opened, patch(
            "mac_dev_clean.cli.invoke_tool_recommendations", return_value=[result]
        ) as invoked:
            code, output, error = self.run_cli(
                [
                    "tools-apply",
                    "--id",
                    "exact-display-id",
                    "--index",
                    "/tmp/dedicated.sqlite3",
                    "--journal",
                    "/tmp/actions.jsonl",
                    "--dry-run",
                    "--json",
                ]
            )

        self.assertEqual((code, error), (0, ""))
        opened.assert_called_once_with(Path("/tmp/dedicated.sqlite3"))
        journal_opened.assert_called_once_with(Path("/tmp/actions.jsonl"))
        invoked.assert_called_once_with(
            ["exact-display-id"], index, journal, dry_run=True
        )
        index.close.assert_called_once_with()
        self.assertEqual(json.loads(output), {"results": [result.to_dict()]})

    def test_tools_apply_defaults_to_dedicated_index_and_default_journal(self):
        index = Mock()
        journal = Mock()
        result = ApplyResult(
            recommendation_id="shown-id",
            label="",
            path="",
            reclaimable_bytes=0,
            outcome=ApplyOutcome.FAILED,
            dry_run=False,
            error="recommendation not found in the current scan",
        )
        with patch("mac_dev_clean.cli.open_index", return_value=index) as opened, patch(
            "mac_dev_clean.cli.open_journal", return_value=journal
        ) as journal_opened, patch(
            "mac_dev_clean.cli.invoke_tool_recommendations", return_value=[result]
        ) as invoked:
            code, output, _ = self.run_cli(
                ["tools-apply", "--id", "shown-id", "--json"]
            )

        self.assertEqual(code, 1)
        opened.assert_called_once_with(DEFAULT_TOOL_INDEX_PATH.expanduser())
        journal_opened.assert_called_once_with()
        invoked.assert_called_once_with(["shown-id"], index, journal, dry_run=False)
        self.assertEqual(json.loads(output)["results"][0]["outcome"], "failed")

    def test_tools_apply_requires_id_before_opening_boundaries(self):
        with patch("mac_dev_clean.cli.open_index") as opened, self.assertRaises(SystemExit):
            self.run_cli(["tools-apply"])
        opened.assert_not_called()

    def test_tools_apply_invocation_failure_is_safe_and_closes_index(self):
        index = Mock()
        with patch("mac_dev_clean.cli.open_index", return_value=index), patch(
            "mac_dev_clean.cli.open_journal", return_value=Mock()
        ), patch(
            "mac_dev_clean.cli.invoke_tool_recommendations",
            side_effect=RuntimeError("raw stderr /Users/private"),
        ):
            code, output, error = self.run_cli(
                ["tools-apply", "--id", "shown-id", "--json"]
            )

        self.assertEqual((code, error), (1, ""))
        self.assertEqual(
            json.loads(output),
            {
                "error": "Could not apply the tool-managed recommendation.",
                "results": [],
            },
        )
        self.assertNotIn("private", output)
        index.close.assert_called_once_with()

    def test_tools_apply_close_failure_preserves_a_completed_action_result(self):
        index = Mock()
        index.close.side_effect = OSError("secret /Users/private/tools.sqlite3")
        result = ApplyResult(
            recommendation_id="shown-id",
            label="Docker build cache",
            path="docker:build-cache",
            reclaimable_bytes=10,
            outcome=ApplyOutcome.INVOKED,
            dry_run=False,
            reported="Total reclaimed space: 10 B",
        )
        with patch("mac_dev_clean.cli.open_index", return_value=index), patch(
            "mac_dev_clean.cli.open_journal", return_value=Mock()
        ), patch(
            "mac_dev_clean.cli.invoke_tool_recommendations", return_value=[result]
        ):
            code, output, error = self.run_cli(
                ["tools-apply", "--id", "shown-id", "--json"]
            )

        payload = json.loads(output)
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(payload["results"], [result.to_dict()])
        self.assertEqual(
            payload["warning"], "Could not close the tool recommendation index."
        )
        self.assertNotIn("secret", output)
        self.assertNotIn("private", output)
        index.close.assert_called_once_with()

    def test_tools_apply_human_output_surfaces_result_fields(self):
        index = Mock()
        result = ApplyResult(
            recommendation_id="shown-id",
            label="Docker build cache",
            path="docker:build-cache",
            reclaimable_bytes=10,
            outcome=ApplyOutcome.FAILED,
            dry_run=False,
            reported="owner reported 10 B",
            error="bounded executor detail",
            journal_warning="journal write failed",
        )
        with patch("mac_dev_clean.cli.open_index", return_value=index), patch(
            "mac_dev_clean.cli.open_journal", return_value=Mock()
        ), patch(
            "mac_dev_clean.cli.invoke_tool_recommendations", return_value=[result]
        ):
            code, output, error = self.run_cli(
                ["tools-apply", "--id", "shown-id"]
            )

        self.assertEqual((code, error), (1, ""))
        self.assertIn("reported: owner reported 10 B", output)
        self.assertIn("error: bounded executor detail", output)
        self.assertIn("warning: journal write failed", output)


class JournalCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "actions.jsonl"

    def run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_clear_attempts_active_and_rotated_journal(self):
        self.path.write_text("active\n", encoding="utf-8")
        rotated = self.path.with_name(self.path.name + ".1")
        rotated.write_text("rotated\n", encoding="utf-8")
        journal = Mock(path=self.path)
        with patch("mac_dev_clean.cli.open_journal", return_value=journal) as opened:
            code, output, error = self.run_cli(
                ["journal", "--clear", "--journal", str(self.path)]
            )

        self.assertEqual((code, output, error), (0, "", ""))
        opened.assert_called_once_with(self.path)
        self.assertFalse(self.path.exists())
        self.assertFalse(rotated.exists())

    def test_journal_json_skips_malformed_lines_and_positive_tail_limits_records(self):
        first = {
            "timestamp": "2026-09-07T10:00:00+00:00",
            "outcome": "removed",
            "dry_run": False,
            "target": "first",
        }
        second = {
            "timestamp": "2026-09-07T11:00:00+00:00",
            "outcome": "failed",
            "dry_run": True,
            "target": "second",
        }
        self.path.write_text(
            "\n" + json.dumps(first) + "\nnot-json\n[]\n" + json.dumps(second) + "\n",
            encoding="utf-8",
        )
        journal = Mock(path=self.path)
        with patch("mac_dev_clean.cli.open_journal", return_value=journal):
            code, output, error = self.run_cli(
                ["journal", "--json", "--tail", "1", "--journal", str(self.path)]
            )

        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output), {"records": [second]})

    def test_journal_json_skips_invalid_utf8_line_between_valid_records(self):
        first = {"timestamp": "first", "target": "one"}
        second = {"timestamp": "second", "target": "two"}
        self.path.write_bytes(
            json.dumps(first).encode("utf-8")
            + b"\n\xff\n"
            + json.dumps(second).encode("utf-8")
            + b"\n"
        )
        with patch(
            "mac_dev_clean.cli.open_journal", return_value=Mock(path=self.path)
        ):
            code, output, error = self.run_cli(
                ["journal", "--json", "--journal", str(self.path)]
            )

        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output), {"records": [first, second]})

    def test_unreadable_journal_is_a_fixed_nonzero_error_not_empty_success(self):
        self.path.write_text("present\n", encoding="utf-8")
        with patch(
            "mac_dev_clean.cli.open_journal", return_value=Mock(path=self.path)
        ), patch(
            "builtins.open",
            side_effect=PermissionError("secret /Users/private/actions.jsonl"),
        ):
            code, output, error = self.run_cli(
                ["journal", "--json", "--journal", str(self.path)]
            )

        self.assertEqual((code, error), (1, ""))
        self.assertEqual(
            json.loads(output),
            {"error": "Could not read the action journal.", "records": []},
        )
        self.assertNotIn("secret", output)
        self.assertNotIn("private", output)

    def test_unreadable_journal_human_output_is_fixed_and_nonzero(self):
        self.path.write_text("present\n", encoding="utf-8")
        with patch(
            "mac_dev_clean.cli.open_journal", return_value=Mock(path=self.path)
        ), patch(
            "builtins.open",
            side_effect=PermissionError("secret /Users/private/actions.jsonl"),
        ):
            code, output, error = self.run_cli(
                ["journal", "--journal", str(self.path)]
            )

        self.assertEqual((code, error), (1, ""))
        self.assertEqual(output, "Could not read the action journal.\n")
        self.assertNotIn("secret", output)
        self.assertNotIn("private", output)

    def test_iteration_error_discards_partial_records_and_is_safe(self):
        class FailingReader:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def __iter__(self):
                yield b'{"target":"partial"}\n'
                raise OSError("secret /Users/private/actions.jsonl")

        with patch(
            "mac_dev_clean.cli.open_journal", return_value=Mock(path=self.path)
        ), patch("builtins.open", return_value=FailingReader()):
            code, output, error = self.run_cli(
                ["journal", "--json", "--journal", str(self.path)]
            )

        self.assertEqual((code, error), (1, ""))
        self.assertEqual(
            json.loads(output),
            {"error": "Could not read the action journal.", "records": []},
        )
        self.assertNotIn("partial", output)
        self.assertNotIn("secret", output)
        self.assertNotIn("private", output)

    def test_missing_journal_human_output_is_empty_success(self):
        with patch(
            "mac_dev_clean.cli.open_journal", return_value=Mock(path=self.path)
        ):
            code, output, error = self.run_cli(
                ["journal", "--journal", str(self.path)]
            )

        self.assertEqual((code, output, error), (0, "", ""))

    def test_tail_is_applied_after_mixed_invalid_records_are_filtered(self):
        first = {"target": "first"}
        second = {"target": "second"}
        third = {"target": "third"}
        self.path.write_bytes(
            json.dumps(first).encode("utf-8")
            + b"\n"
            + json.dumps(second).encode("utf-8")
            + b"\n\xff\nnot-json\n[]\n"
            + json.dumps(third).encode("utf-8")
            + b"\n"
        )
        with patch(
            "mac_dev_clean.cli.open_journal", return_value=Mock(path=self.path)
        ):
            code, output, error = self.run_cli(
                ["journal", "--json", "--tail", "2", "--journal", str(self.path)]
            )

        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output), {"records": [second, third]})

    def test_journal_tail_rejects_zero_and_negative_before_opening(self):
        for value in ("0", "-1"):
            with self.subTest(value=value), patch(
                "mac_dev_clean.cli.open_journal"
            ) as opened, self.assertRaises(SystemExit):
                self.run_cli(["journal", "--tail", value])
            opened.assert_not_called()

    def test_missing_journal_view_and_clear_are_successful(self):
        journal = Mock(path=self.path)
        with patch("mac_dev_clean.cli.open_journal", return_value=journal):
            view_code, output, view_error = self.run_cli(["journal", "--json"])
            clear_code, clear_output, clear_error = self.run_cli(["journal", "--clear"])

        self.assertEqual((view_code, view_error), (0, ""))
        self.assertEqual(json.loads(output), {"records": []})
        self.assertEqual((clear_code, clear_output, clear_error), (0, "", ""))

    def test_clear_attempts_rotated_after_active_failure_and_hides_details(self):
        self.path.write_text("active\n", encoding="utf-8")
        rotated = self.path.with_name(self.path.name + ".1")
        rotated.write_text("rotated\n", encoding="utf-8")
        original_unlink = Path.unlink
        attempted = []

        def failing_active(candidate, *args, **kwargs):
            attempted.append(candidate)
            if candidate == self.path:
                raise PermissionError("secret /Users/private/actions.jsonl")
            return original_unlink(candidate, *args, **kwargs)

        with patch("mac_dev_clean.cli.open_journal", return_value=Mock(path=self.path)), patch(
            "pathlib.Path.unlink", autospec=True, side_effect=failing_active
        ):
            code, output, error = self.run_cli(["journal", "--clear"])

        self.assertEqual((code, error), (1, ""))
        self.assertEqual(attempted, [self.path, rotated])
        self.assertTrue(self.path.exists())
        self.assertFalse(rotated.exists())
        self.assertEqual(output, "Could not clear one or more journal files.\n")
        self.assertNotIn("private", output)

    def test_clear_unlinks_symlink_without_touching_target(self):
        target = Path(self.temp.name) / "target.jsonl"
        target.write_text("keep\n", encoding="utf-8")
        self.path.symlink_to(target)
        with patch("mac_dev_clean.cli.open_journal", return_value=Mock(path=self.path)):
            code, output, error = self.run_cli(["journal", "--clear", "--json"])

        self.assertEqual((code, output, error), (0, "", ""))
        self.assertFalse(self.path.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")


class ExistingRouteCompatibilityTests(unittest.TestCase):
    def test_existing_routes_and_interactive_default_still_parse(self):
        parser = build_parser()
        self.assertIsNone(parser.parse_args([]).command)
        for command in (
            "scan",
            "clean",
            "interactive",
            "report",
            "deep-scan",
            "apply",
            "reset-index",
        ):
            with self.subTest(command=command):
                self.assertEqual(parser.parse_args([command]).command, command)


if __name__ == "__main__":
    unittest.main()
