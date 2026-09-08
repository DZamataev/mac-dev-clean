from __future__ import annotations

import unittest
from pathlib import Path

from mac_dev_clean.recommendation import ActionKind
from mac_dev_clean.tools.docker import DOCKER_PREVIEW_ARGV, analyze_docker
from mac_dev_clean.tools.runner import ToolResult, ToolUnavailable

HOME = Path("/Users/test/home")

REAL_OUTPUT = (
    '{"Active":"4","Reclaimable":"15.03GB (68%)","Size":"22.03GB",'
    '"TotalCount":"18","Type":"Images"}\n'
    '{"Active":"2","Reclaimable":"106.5kB (0%)","Size":"62.55MB",'
    '"TotalCount":"4","Type":"Containers"}\n'
    '{"Active":"6","Reclaimable":"0B (0%)","Size":"1.133GB",'
    '"TotalCount":"6","Type":"Local Volumes"}\n'
    '{"Active":"0","Reclaimable":"7.499GB","Size":"18.92GB",'
    '"TotalCount":"144","Type":"Build Cache"}\n'
)


def runner_returning(stdout: str, exit_code: int = 0, stderr: str = ""):
    def run(argv):
        return ToolResult(
            argv=tuple(argv), stdout=stdout, stderr=stderr, exit_code=exit_code
        )

    return run


def runner_raising(reason: str = "not installed"):
    def run(argv):
        raise ToolUnavailable("docker", reason)

    return run


class DockerInventoryTests(unittest.TestCase):
    def test_produces_one_recommendation_per_reclaimable_class(self):
        items, unavailable = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        self.assertIsNone(unavailable)
        resources = sorted(item.tool_action.resource for item in items)
        self.assertEqual(resources, ["build-cache", "containers", "images"])

    def test_zero_reclaimable_classes_are_skipped(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        self.assertNotIn("volumes", [item.tool_action.resource for item in items])

    def test_reclaimable_bytes_use_si_units(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )
        by_resource = {item.tool_action.resource: item for item in items}

        self.assertEqual(by_resource["build-cache"].reclaimable_bytes, 7499000000)
        self.assertEqual(by_resource["images"].reclaimable_bytes, 15030000000)

    def test_argument_vectors_are_the_narrow_prune_verbs(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )
        vectors = {item.tool_action.resource: item.tool_action.argv for item in items}

        self.assertEqual(vectors["build-cache"], ("docker", "builder", "prune", "-f"))
        self.assertEqual(vectors["images"], ("docker", "image", "prune", "-f"))
        self.assertEqual(vectors["containers"], ("docker", "container", "prune", "-f"))

    def test_image_prune_is_never_the_all_variant(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        for item in items:
            self.assertNotIn("-a", item.tool_action.argv)
            self.assertNotIn("--all", item.tool_action.argv)

    def test_every_item_is_invoke_tool_and_unselected(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        for item in items:
            self.assertIs(item.action, ActionKind.INVOKE_TOOL)
            self.assertFalse(item.selected_by_default)

    def test_evidence_keeps_dockers_own_string(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )
        build_cache = next(
            item for item in items if item.tool_action.resource == "build-cache"
        )

        details = " ".join(entry.detail for entry in build_cache.evidence)
        self.assertIn("7.499GB", details)
        self.assertEqual(build_cache.tool_action.reported, "7.499GB")

    def test_preview_argv_is_recorded_for_re_preview(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        for item in items:
            self.assertEqual(item.tool_action.preview_argv, DOCKER_PREVIEW_ARGV)

    def test_invokes_the_exact_preview_vector_once(self):
        calls = []

        def run(argv):
            calls.append(tuple(argv))
            return ToolResult(
                argv=tuple(argv), stdout=REAL_OUTPUT, stderr="", exit_code=0
            )

        analyze_docker(run, generation=1, home=HOME)

        self.assertEqual(calls, [DOCKER_PREVIEW_ARGV])

    def test_duplicate_recognized_class_is_omitted_without_hiding_other_classes(self):
        duplicate_images = (
            '{"Reclaimable":"1GB","Size":"2GB","Type":"Images"}\n'
            '{"Reclaimable":"2GB","Size":"3GB","Type":"Build Cache"}\n'
            '{"Reclaimable":"3GB","Size":"4GB","Type":"Images"}\n'
        )

        items, unavailable = analyze_docker(
            runner_returning(duplicate_images), generation=1, home=HOME
        )

        self.assertIsNone(unavailable)
        self.assertEqual(
            [item.tool_action.resource for item in items], ["build-cache"]
        )

    def test_recommendations_follow_declared_class_order_not_docker_row_order(self):
        scrambled = (
            '{"Reclaimable":"1GB","Size":"2GB","Type":"Containers"}\n'
            '{"Reclaimable":"2GB","Size":"3GB","Type":"Local Volumes"}\n'
            '{"Reclaimable":"3GB","Size":"4GB","Type":"Images"}\n'
            '{"Reclaimable":"4GB","Size":"5GB","Type":"Build Cache"}\n'
        )

        items, unavailable = analyze_docker(
            runner_returning(scrambled), generation=1, home=HOME
        )

        self.assertIsNone(unavailable)
        self.assertEqual(
            [item.tool_action.resource for item in items],
            ["build-cache", "images", "containers", "volumes"],
        )


class DockerUnavailableTests(unittest.TestCase):
    def test_missing_binary_yields_a_reason_and_no_items(self):
        items, unavailable = analyze_docker(
            runner_raising("not installed"), generation=1, home=HOME
        )

        self.assertEqual(items, [])
        self.assertIn("not installed", unavailable)

    def test_stopped_daemon_yields_a_reason_and_no_items(self):
        items, unavailable = analyze_docker(
            runner_returning("", exit_code=1, stderr="Cannot connect to the Docker daemon"),
            generation=1,
            home=HOME,
        )

        self.assertEqual(items, [])
        self.assertIn("Cannot connect", unavailable)

    def test_unparseable_line_is_skipped_without_failing_the_scan(self):
        items, unavailable = analyze_docker(
            runner_returning('not json\n' + REAL_OUTPUT), generation=1, home=HOME
        )

        self.assertIsNone(unavailable)
        self.assertEqual(len(items), 3)

    def test_empty_output_produces_no_items_and_no_error(self):
        items, unavailable = analyze_docker(
            runner_returning(""), generation=1, home=HOME
        )

        self.assertEqual(items, [])
        self.assertIsNone(unavailable)


if __name__ == "__main__":
    unittest.main()
