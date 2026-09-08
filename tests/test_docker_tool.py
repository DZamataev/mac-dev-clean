from __future__ import annotations

import unittest
from pathlib import Path

from mac_dev_clean.recommendation import (
    ActionKind,
    Confidence,
    RestorationCost,
)
from mac_dev_clean.tools.docker import DOCKER_PREVIEW_ARGV, analyze_docker
from mac_dev_clean.tools.runner import MAX_STDERR_CHARS, ToolResult, ToolUnavailable

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

ALL_CLASSES_OUTPUT = (
    '{"Reclaimable":"4GB","Size":"5GB","Type":"Build Cache"}\n'
    '{"Reclaimable":"3GB (60%)","Size":"6GB","Type":"Images"}\n'
    '{"Reclaimable":"2GB","Size":"7GB","Type":"Containers"}\n'
    '{"Reclaimable":"1GB (25%)","Size":"8GB","Type":"Local Volumes"}\n'
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
    def test_all_classes_have_safe_actions_and_accurate_estimate_metadata(self):
        items, unavailable = analyze_docker(
            runner_returning(ALL_CLASSES_OUTPUT), generation=9, home=HOME
        )

        self.assertIsNone(unavailable)
        expected = {
            "build-cache": {
                "detector_id": "docker-build-cache",
                "id": "a5cc7a314bac4a78",
                "label": "Docker default-prune build cache",
                "argv": ("docker", "builder", "prune", "-f"),
                "allocated": 5000000000,
                "reclaimable": 4000000000,
                "confidence": Confidence.HEURISTIC,
                "reported": "4GB",
                "target": "default builder prune",
                "report_evidence": (
                    "docker reports 4GB reclaimable class-wide; this is an upper "
                    "bound for build cache eligible for Docker's default builder "
                    "prune"
                ),
                "total_evidence": "docker reports 5GB in total",
            },
            "images": {
                "detector_id": "docker-images",
                "id": "0d518fe91dd3f7b4",
                "label": "Docker dangling images",
                "argv": ("docker", "image", "prune", "-f"),
                "allocated": 6000000000,
                "reclaimable": 3000000000,
                "confidence": Confidence.HEURISTIC,
                "reported": "3GB (60%)",
                "target": "dangling images",
                "report_evidence": (
                    "docker reports 3GB (60%) reclaimable class-wide; this is an "
                    "upper bound for dangling images"
                ),
                "total_evidence": "docker reports 6GB in total",
            },
            "containers": {
                "detector_id": "docker-containers",
                "id": "3fe4686bf493d2a3",
                "label": "Docker stopped containers",
                "argv": ("docker", "container", "prune", "-f"),
                "allocated": 7000000000,
                "reclaimable": 2000000000,
                "confidence": Confidence.EXACT,
                "reported": "2GB",
                "target": "stopped containers",
                "report_evidence": "docker reports 2GB reclaimable",
                "total_evidence": "docker reports 7GB in total",
            },
            "volumes": {
                "detector_id": "docker-volumes",
                "id": "11c986542fd47298",
                "label": "Docker unused anonymous volumes",
                "argv": ("docker", "volume", "prune", "-f"),
                "allocated": 8000000000,
                "reclaimable": 1000000000,
                "confidence": Confidence.HEURISTIC,
                "reported": "1GB (25%)",
                "target": "unused anonymous volumes",
                "report_evidence": (
                    "docker reports 1GB (25%) reclaimable class-wide; this is an "
                    "upper bound for unused anonymous volumes"
                ),
                "total_evidence": "docker reports 8GB in total",
            },
        }

        self.assertEqual(len(items), 4)
        self.assertEqual(len({item.id for item in items}), 4)
        for item in items:
            resource = item.tool_action.resource
            want = expected[resource]
            self.assertEqual(item.detector_id, want["detector_id"])
            self.assertEqual(item.id, want["id"])
            self.assertEqual(item.label, want["label"])
            self.assertEqual(item.tool_action.argv, want["argv"])
            self.assertEqual(item.allocated_bytes, want["allocated"])
            self.assertEqual(item.reclaimable_bytes, want["reclaimable"])
            self.assertIs(item.confidence, want["confidence"])
            self.assertIs(item.action, ActionKind.INVOKE_TOOL)
            self.assertIs(item.restoration, RestorationCost.EXTERNAL_STATE)
            self.assertFalse(item.selected_by_default)
            self.assertEqual(item.generation, 9)
            self.assertEqual(item.path, HOME)
            self.assertEqual(item.safety_root, HOME)
            self.assertEqual(item.tool_action.tool, "docker")
            self.assertEqual(item.tool_action.reported, want["reported"])
            self.assertEqual(item.tool_action.preview_argv, DOCKER_PREVIEW_ARGV)
            self.assertNotIn("-a", item.tool_action.argv)
            self.assertNotIn("--all", item.tool_action.argv)
            self.assertIn(want["target"], item.reason.lower())

            evidence = {entry.code: entry.detail for entry in item.evidence}
            self.assertEqual(
                evidence["preview-command"],
                "docker system df --format {{json .}}",
            )
            self.assertEqual(evidence["tool-report"], want["report_evidence"])
            self.assertEqual(evidence["tool-total"], want["total_evidence"])
            if resource == "containers":
                self.assertNotIn("upper bound", item.warning.lower())
            else:
                self.assertIn("class-wide", evidence["tool-report"].lower())
                self.assertIn("upper bound", evidence["tool-report"].lower())
                self.assertIn(want["target"], item.warning.lower())
                self.assertIn("upper bound", item.warning.lower())
                self.assertIn(
                    "actual reclaimed space may be lower", item.warning.lower()
                )

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

    def test_successful_output_requires_matching_inventory_provenance(self):
        bad_vectors = (
            ("podman", "system", "df", "--format", "{{json .}}"),
            ("docker", "system", "df"),
            "docker system df --format {{json .}}",
            ("docker", "system", 7, "--format", "{{json .}}"),
            ("docker\x00suffix", "system", "df", "--format", "{{json .}}"),
        )

        for actual in bad_vectors:
            with self.subTest(actual=actual):
                def run(argv):
                    return ToolResult(actual, REAL_OUTPUT, "", 0)

                items, unavailable = analyze_docker(run, generation=1, home=HOME)

                self.assertEqual(items, [])
                self.assertIn("provenance", unavailable)
                self.assertLessEqual(len(unavailable), MAX_STDERR_CHARS)

    def test_accepts_resolved_inventory_executable_provenance(self):
        def run(argv):
            return ToolResult(
                ("/usr/local/bin/docker",) + tuple(argv[1:]), REAL_OUTPUT, "", 0
            )

        items, unavailable = analyze_docker(run, generation=1, home=HOME)

        self.assertIsNone(unavailable)
        self.assertTrue(items)

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

    def test_duplicate_variants_omit_the_ambiguous_class_only(self):
        independent = '{"Reclaimable":"2GB","Size":"3GB","Type":"Containers"}\n'
        cases = {
            "valid then zero": (
                '{"Reclaimable":"1GB","Size":"2GB","Type":"Images"}\n'
                '{"Reclaimable":"0B","Size":"2GB","Type":"Images"}\n'
            ),
            "valid then malformed": (
                '{"Reclaimable":"1GB","Size":"2GB","Type":"Images"}\n'
                '{"Reclaimable":1,"Size":"2GB","Type":"Images"}\n'
            ),
            "malformed then valid": (
                '{"Reclaimable":1,"Size":"2GB","Type":"Images"}\n'
                '{"Reclaimable":"1GB","Size":"2GB","Type":"Images"}\n'
            ),
        }

        for name, duplicate_rows in cases.items():
            with self.subTest(name=name):
                items, unavailable = analyze_docker(
                    runner_returning(duplicate_rows + independent),
                    generation=1,
                    home=HOME,
                )

                self.assertIsNone(unavailable)
                self.assertEqual(
                    [item.tool_action.resource for item in items], ["containers"]
                )

    def test_malformed_non_object_and_unknown_rows_do_not_hide_valid_classes(self):
        cases = {
            "invalid json": "not json",
            "array": "[]",
            "scalar": '"Images"',
            "null": "null",
            "missing type": '{"Reclaimable":"1GB","Size":"2GB"}',
            "unknown type": '{"Reclaimable":"1GB","Size":"2GB","Type":"Networks"}',
            "non-string type": '{"Reclaimable":"1GB","Size":"2GB","Type":7}',
            "missing reclaimable": '{"Size":"2GB","Type":"Images"}',
            "non-string reclaimable": (
                '{"Reclaimable":1,"Size":"2GB","Type":"Images"}'
            ),
            "unparseable reclaimable": (
                '{"Reclaimable":"many","Size":"2GB","Type":"Images"}'
            ),
            "missing size": '{"Reclaimable":"1GB","Type":"Images"}',
            "non-string size": '{"Reclaimable":"1GB","Size":2,"Type":"Images"}',
            "unparseable size": (
                '{"Reclaimable":"1GB","Size":"many","Type":"Images"}'
            ),
        }
        valid = '{"Reclaimable":"2GB","Size":"3GB","Type":"Containers"}'

        for name, malformed in cases.items():
            with self.subTest(name=name):
                items, unavailable = analyze_docker(
                    runner_returning(malformed + "\n" + valid + "\n"),
                    generation=1,
                    home=HOME,
                )

                self.assertIsNone(unavailable)
                self.assertEqual(
                    [item.tool_action.resource for item in items], ["containers"]
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
    def assert_reason_is_bounded(self, reason):
        self.assertEqual(len(reason), MAX_STDERR_CHARS)
        self.assertTrue(reason.endswith("...[truncated]"))

    def test_exception_reason_is_bounded_with_marker(self):
        boundary = "e" * MAX_STDERR_CHARS
        _, exact = analyze_docker(runner_raising(boundary), generation=1, home=HOME)
        _, truncated = analyze_docker(
            runner_raising(boundary + "x"), generation=1, home=HOME
        )

        self.assertEqual(exact, boundary)
        self.assert_reason_is_bounded(truncated)

    def test_stderr_reason_is_bounded_with_marker(self):
        boundary = "e" * MAX_STDERR_CHARS
        _, exact = analyze_docker(
            runner_returning("", exit_code=1, stderr=boundary),
            generation=1,
            home=HOME,
        )
        _, truncated = analyze_docker(
            runner_returning("", exit_code=1, stderr=boundary + "x"),
            generation=1,
            home=HOME,
        )

        self.assertEqual(exact, boundary)
        self.assert_reason_is_bounded(truncated)

    def test_stdout_fallback_reason_is_bounded_with_marker(self):
        boundary = "o" * MAX_STDERR_CHARS
        _, exact = analyze_docker(
            runner_returning(boundary, exit_code=1), generation=1, home=HOME
        )
        _, truncated = analyze_docker(
            runner_returning(boundary + "x", exit_code=1), generation=1, home=HOME
        )

        self.assertEqual(exact, boundary)
        self.assert_reason_is_bounded(truncated)

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
