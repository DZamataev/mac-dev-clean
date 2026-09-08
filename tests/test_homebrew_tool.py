from __future__ import annotations

import unittest
from pathlib import Path

from mac_dev_clean.recommendation import ActionKind, Confidence, RestorationCost
from mac_dev_clean.tools.homebrew import (
    BREW_CLEANUP_ARGV,
    BREW_PREVIEW_ARGV,
    analyze_homebrew,
    parse_brew_preview,
)
from mac_dev_clean.tools.runner import MAX_STDERR_CHARS, ToolResult, ToolUnavailable

HOME = Path("/Users/test/home")
REAL_OUTPUT = (
    "Warning: Skipping aom: most recent version 3.15.0 not installed\n"
    "Warning: Skipping assimp: most recent version 6.0.5 not installed\n"
    "Would remove: /opt/homebrew/Library/Homebrew/vendor/portable-ruby/4.0.3 "
    "(1,704 files, 34.6MB)\n"
    "Would remove: /opt/homebrew/Library/Homebrew/vendor/portable-ruby/4.0.5_1 "
    "(1,707 files, 34.6MB)\n"
    "==> This operation would free approximately 799.8MB of disk space.\n"
)


def runner_returning(stdout: str, exit_code: int = 0, stderr: str = ""):
    def run(argv):
        return ToolResult(tuple(argv), stdout, stderr, exit_code)

    return run


def runner_raising(reason: str):
    def run(argv):
        raise ToolUnavailable("brew", reason)

    return run


class ParseBrewPreviewTests(unittest.TestCase):
    def test_reads_the_total_line(self):
        total, _ = parse_brew_preview(
            "==> This operation would free approximately 799.8MB of disk space.\n"
        )

        self.assertEqual(total, 799800000)

    def test_rejects_a_malformed_decimal_in_the_summary(self):
        total, _ = parse_brew_preview(
            "==> This operation would free approximately 1.2.3MB of disk space.\n"
        )

        self.assertEqual(total, 0)

    def test_rejects_trailing_junk_after_the_canonical_summary(self):
        total, _ = parse_brew_preview(
            "==> This operation would free approximately 12MB of disk space. junk\n"
        )

        self.assertEqual(total, 0)

    def test_ignores_an_embedded_summary_like_fragment(self):
        total, _ = parse_brew_preview(
            "log: ==> This operation would free approximately 12MB of disk space.\n"
        )

        self.assertEqual(total, 0)

    def test_rejects_a_malformed_summary_even_with_one_valid_summary(self):
        total, _ = parse_brew_preview(
            "==> This operation would free approximately 1.2.3MB of disk space.\n"
            "==> This operation would free approximately 12MB of disk space.\n"
        )

        self.assertEqual(total, 0)

    def test_rejects_multiple_canonical_summaries(self):
        total, _ = parse_brew_preview(
            "==> This operation would free approximately 12MB of disk space.\n"
            "==> This operation would free approximately 34MB of disk space.\n"
        )

        self.assertEqual(total, 0)

    def test_counts_only_removal_lines(self):
        _, count = parse_brew_preview(
            "Would remove: /opt/homebrew/old-a (1 files, 1MB)\n"
            "Would remove: /opt/homebrew/old-b (2 files, 2MB)\n"
        )

        self.assertEqual(count, 2)

    def test_warning_lines_are_never_counted_as_removals(self):
        _, count = parse_brew_preview(
            "Warning: Skipping aom: most recent version 3.15.0 not installed\n"
            "Would remove: /opt/homebrew/old-a (1 files, 1MB)\n"
        )

        self.assertEqual(count, 1)

    def test_output_without_a_total_keeps_the_count_and_zero_size(self):
        self.assertEqual(
            parse_brew_preview(
                "Would remove: /opt/homebrew/old-a (1 files, 1MB)\n"
            ),
            (0, 1),
        )

    def test_empty_output_yields_zeroes(self):
        self.assertEqual(parse_brew_preview(""), (0, 0))

    def test_nothing_to_do_output_yields_zeroes(self):
        self.assertEqual(parse_brew_preview("Warning: nothing to do\n"), (0, 0))


class AnalyzeHomebrewTests(unittest.TestCase):
    def test_invokes_the_exact_preview_vector_once(self):
        calls = []

        def run(argv):
            calls.append(tuple(argv))
            return ToolResult(tuple(argv), "Warning: nothing to do\n", "", 0)

        analyze_homebrew(run, generation=1, home=HOME)

        self.assertEqual(calls, [("brew", "cleanup", "-n")])
        self.assertEqual(calls, [BREW_PREVIEW_ARGV])

    def test_nothing_reclaimable_produces_no_items_and_no_error(self):
        items, unavailable = analyze_homebrew(
            runner_returning("Warning: nothing to do\n"),
            generation=1,
            home=HOME,
        )

        self.assertEqual(items, [])
        self.assertIsNone(unavailable)

    def test_removal_without_a_unique_positive_total_is_a_parse_error(self):
        items, unavailable = analyze_homebrew(
            runner_returning("Would remove: /opt/homebrew/old-a\n"),
            generation=1,
            home=HOME,
        )

        self.assertEqual(items, [])
        self.assertEqual(
            unavailable,
            "brew reported 1 removal but no unique valid positive cleanup total",
        )
        self.assertLessEqual(len(unavailable), MAX_STDERR_CHARS)

    def test_recommendation_matches_the_homebrew_cleanup_contract(self):
        items, unavailable = analyze_homebrew(
            runner_returning(REAL_OUTPUT), generation=9, home=HOME
        )

        self.assertIsNone(unavailable)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.detector_id, "homebrew-cleanup")
        self.assertEqual(item.id, "48d74467cfb8f473")
        self.assertEqual(item.category, "tool-managed")
        self.assertEqual(item.label, "Homebrew removable versions and downloads")
        self.assertEqual(item.path, HOME)
        self.assertIs(item.action, ActionKind.INVOKE_TOOL)
        self.assertEqual(item.allocated_bytes, 799800000)
        self.assertEqual(item.reclaimable_bytes, 799800000)
        self.assertIs(item.confidence, Confidence.EXACT)
        self.assertIs(item.restoration, RestorationCost.REDOWNLOAD)
        self.assertFalse(item.selected_by_default)
        self.assertEqual(item.safety_root, HOME)
        self.assertEqual(
            item.reason,
            "Homebrew reports old installed versions and stale downloads it can remove.",
        )
        self.assertEqual(item.generation, 9)
        self.assertEqual(
            item.warning,
            "Reinstalling an older version afterwards requires downloading it again.",
        )
        self.assertIsNone(item.last_activity_at)
        self.assertEqual(
            [(entry.code, entry.detail) for entry in item.evidence],
            [
                ("preview-command", "brew cleanup -n"),
                ("tool-report", "brew would remove 2 items"),
            ],
        )
        self.assertEqual(item.tool_action.tool, "brew")
        self.assertEqual(item.tool_action.resource, "cleanup")
        self.assertEqual(item.tool_action.argv, ("brew", "cleanup"))
        self.assertEqual(item.tool_action.argv, BREW_CLEANUP_ARGV)
        self.assertEqual(item.tool_action.preview_argv, ("brew", "cleanup", "-n"))
        self.assertEqual(item.tool_action.preview_argv, BREW_PREVIEW_ARGV)
        self.assertEqual(item.tool_action.reported, "2 items")

    def test_cleanup_vectors_are_frozen_and_never_use_forbidden_flags(self):
        items, _ = analyze_homebrew(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )
        action = items[0].tool_action

        self.assertIsInstance(action.argv, tuple)
        self.assertIsInstance(action.preview_argv, tuple)
        for vector in (action.argv, action.preview_argv):
            self.assertNotIn("--prune", vector)
            self.assertNotIn("-s", vector)

    def test_missing_binary_is_reported_as_unavailable(self):
        def run(argv):
            raise ToolUnavailable("brew", "not installed")

        items, unavailable = analyze_homebrew(run, generation=1, home=HOME)

        self.assertEqual(items, [])
        self.assertEqual(unavailable, "not installed")

    def test_non_zero_exit_prefers_stderr_as_the_unavailable_reason(self):
        items, unavailable = analyze_homebrew(
            runner_returning("ignored stdout", exit_code=1, stderr="brew is broken"),
            generation=1,
            home=HOME,
        )

        self.assertEqual(items, [])
        self.assertEqual(unavailable, "brew is broken")

    def test_non_zero_exit_falls_back_to_stdout_for_the_unavailable_reason(self):
        items, unavailable = analyze_homebrew(
            runner_returning("brew failed on stdout\n", exit_code=2),
            generation=1,
            home=HOME,
        )

        self.assertEqual(items, [])
        self.assertEqual(unavailable, "brew failed on stdout")

    def test_non_zero_exit_falls_back_to_the_exit_code(self):
        items, unavailable = analyze_homebrew(
            runner_returning(" \n", exit_code=7, stderr="\t"),
            generation=1,
            home=HOME,
        )

        self.assertEqual(items, [])
        self.assertEqual(unavailable, "brew exited with 7")


class HomebrewUnavailableReasonBoundsTests(unittest.TestCase):
    def assert_reason_is_bounded(self, reason):
        self.assertEqual(len(reason), MAX_STDERR_CHARS)
        self.assertTrue(reason.endswith("...[truncated]"))

    def test_exception_reason_keeps_boundary_and_bounds_boundary_plus_one(self):
        boundary = "e" * MAX_STDERR_CHARS
        _, exact = analyze_homebrew(
            runner_raising(boundary), generation=1, home=HOME
        )
        _, truncated = analyze_homebrew(
            runner_raising(boundary + "x"), generation=1, home=HOME
        )

        self.assertEqual(exact, boundary)
        self.assert_reason_is_bounded(truncated)

    def test_stderr_reason_keeps_boundary_and_bounds_boundary_plus_one(self):
        boundary = "e" * MAX_STDERR_CHARS
        _, exact = analyze_homebrew(
            runner_returning("", exit_code=1, stderr=boundary),
            generation=1,
            home=HOME,
        )
        _, truncated = analyze_homebrew(
            runner_returning("", exit_code=1, stderr=boundary + "x"),
            generation=1,
            home=HOME,
        )

        self.assertEqual(exact, boundary)
        self.assert_reason_is_bounded(truncated)

    def test_stdout_reason_keeps_boundary_and_bounds_boundary_plus_one(self):
        boundary = "o" * MAX_STDERR_CHARS
        _, exact = analyze_homebrew(
            runner_returning(boundary, exit_code=1), generation=1, home=HOME
        )
        _, truncated = analyze_homebrew(
            runner_returning(boundary + "x", exit_code=1),
            generation=1,
            home=HOME,
        )

        self.assertEqual(exact, boundary)
        self.assert_reason_is_bounded(truncated)

    def test_exit_code_reason_keeps_boundary_and_bounds_boundary_plus_one(self):
        prefix = "brew exited with "
        exact_code = int("7" * (MAX_STDERR_CHARS - len(prefix)))
        long_code = int("7" * (MAX_STDERR_CHARS - len(prefix) + 1))
        _, exact = analyze_homebrew(
            runner_returning("", exit_code=exact_code), generation=1, home=HOME
        )
        _, truncated = analyze_homebrew(
            runner_returning("", exit_code=long_code), generation=1, home=HOME
        )

        self.assertEqual(exact, prefix + str(exact_code))
        self.assert_reason_is_bounded(truncated)


if __name__ == "__main__":
    unittest.main()
