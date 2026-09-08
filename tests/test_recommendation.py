from pathlib import Path
import unittest

from mac_dev_clean.recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    recommendation_id,
)


class RecommendationIdTests(unittest.TestCase):
    def test_id_is_stable_for_the_same_detector_and_path(self):
        first = recommendation_id("node-modules", Path("/Users/test/app/node_modules"))
        second = recommendation_id("node-modules", Path("/Users/test/app/node_modules"))

        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)

    def test_id_changes_with_detector(self):
        self.assertNotEqual(
            recommendation_id("node-modules", Path("/Users/test/app/node_modules")),
            recommendation_id("ios-pods", Path("/Users/test/app/node_modules")),
        )

    def test_id_normalizes_trailing_separators_and_dot_segments(self):
        self.assertEqual(
            recommendation_id("node-modules", Path("/Users/test/app/node_modules")),
            recommendation_id("node-modules", Path("/Users/test/app/./node_modules/")),
        )


class RecommendationSerializationTests(unittest.TestCase):
    def build(self, **overrides):
        defaults = dict(
            detector_id="node-modules",
            category="project-dependencies",
            label="node_modules",
            path=Path("/Users/test/app/node_modules"),
            action=ActionKind.DELETE_TREE,
            allocated_bytes=2048,
            reclaimable_bytes=2048,
            confidence=Confidence.STRONG,
            restoration=RestorationCost.REBUILD,
            selected_by_default=True,
            evidence=(Evidence("lock-file", "pnpm-lock.yaml exists"),),
            safety_root=Path("/Users/test/app"),
            last_activity_at=None,
            reason="Project inactive for 143 days.",
            generation=7,
        )
        defaults.update(overrides)
        return Recommendation(**defaults)

    def test_to_dict_exposes_the_streaming_contract(self):
        payload = self.build().to_dict()

        self.assertEqual(payload["id"], recommendation_id("node-modules", Path("/Users/test/app/node_modules")))
        self.assertEqual(payload["action"], "delete_tree")
        self.assertEqual(payload["confidence"], "strong")
        self.assertEqual(payload["restoration"], "rebuild")
        self.assertEqual(payload["path"], "/Users/test/app/node_modules")
        self.assertEqual(payload["safety_root"], "/Users/test/app")
        self.assertEqual(payload["size"], "2.0 KB")
        self.assertTrue(payload["selected_by_default"])
        self.assertEqual(payload["evidence"], [{"code": "lock-file", "detail": "pnpm-lock.yaml exists"}])

    def test_report_only_recommendations_are_never_selected(self):
        item = self.build(action=ActionKind.REVEAL_ONLY, selected_by_default=True)

        self.assertFalse(item.selected_by_default)
        self.assertFalse(item.to_dict()["selected_by_default"])

    def test_unknown_confidence_is_never_selected(self):
        item = self.build(confidence=Confidence.UNKNOWN, selected_by_default=True)

        self.assertFalse(item.selected_by_default)


class ToolActionTests(unittest.TestCase):
    def build_tool_recommendation(self, **overrides):
        from mac_dev_clean.recommendation import ToolAction

        defaults = dict(
            detector_id="docker-build-cache",
            category="tool-managed",
            label="Docker build cache",
            path=Path("/Users/test/home"),
            action=ActionKind.INVOKE_TOOL,
            allocated_bytes=18920000000,
            reclaimable_bytes=7499000000,
            confidence=Confidence.EXACT,
            restoration=RestorationCost.EXTERNAL_STATE,
            selected_by_default=False,
            evidence=(Evidence("tool-report", "docker reports 7.499GB reclaimable"),),
            safety_root=Path("/Users/test/home"),
            reason="Docker reports reclaimable build cache.",
            generation=1,
            tool_action=ToolAction(
                tool="docker",
                resource="build-cache",
                argv=("docker", "builder", "prune", "-f"),
                preview_argv=("docker", "system", "df", "--format", "{{json .}}"),
                reported="7.499GB",
            ),
        )
        defaults.update(overrides)
        return Recommendation(**defaults)

    def test_tool_action_serializes_exact_argument_vectors(self):
        from mac_dev_clean.recommendation import ToolAction

        action = ToolAction(
            tool="docker",
            resource="build-cache",
            argv=("docker", "builder", "prune", "--filter=label=a b", "-f"),
            preview_argv=("docker", "system", "df", "--format", "{{json .}}"),
            reported="7.499GB",
        )

        self.assertEqual(
            action.to_dict(),
            {
                "tool": "docker",
                "resource": "build-cache",
                "argv": ["docker", "builder", "prune", "--filter=label=a b", "-f"],
                "preview_argv": [
                    "docker",
                    "system",
                    "df",
                    "--format",
                    "{{json .}}",
                ],
                "reported": "7.499GB",
            },
        )

    def test_argument_sequences_are_normalized_to_tuples(self):
        from mac_dev_clean.recommendation import ToolAction

        argv = ["docker", "builder", "prune"]
        preview_argv = ["docker", "system", "df"]
        action = ToolAction("docker", "build-cache", argv, preview_argv)
        argv.append("-f")
        preview_argv.append("--verbose")

        self.assertEqual(action.argv, ("docker", "builder", "prune"))
        self.assertEqual(action.preview_argv, ("docker", "system", "df"))
        self.assertIsInstance(action.argv, tuple)
        self.assertIsInstance(action.preview_argv, tuple)

    def test_string_and_bytes_argument_vectors_are_rejected(self):
        from mac_dev_clean.recommendation import ToolAction

        valid = ("docker", "system", "df")
        for field, invalid in (
            ("argv", "docker builder prune"),
            ("argv", b"docker builder prune"),
            ("preview_argv", "docker system df"),
            ("preview_argv", b"docker system df"),
        ):
            values = {"argv": valid, "preview_argv": valid}
            values[field] = invalid
            with self.subTest(field=field, vector_type=type(invalid).__name__):
                with self.assertRaises(TypeError):
                    ToolAction("docker", "build-cache", **values)

    def test_empty_argv_is_rejected(self):
        from mac_dev_clean.recommendation import ToolAction

        with self.assertRaises(ValueError):
            ToolAction("docker", "build-cache", (), ("docker", "system", "df"))

    def test_empty_preview_argv_is_rejected(self):
        from mac_dev_clean.recommendation import ToolAction

        with self.assertRaises(ValueError):
            ToolAction("docker", "build-cache", ("docker", "builder", "prune"), ())

    def test_tool_recommendation_serializes_its_tool_action(self):
        payload = self.build_tool_recommendation().to_dict()

        self.assertEqual(payload["tool_action"]["tool"], "docker")
        self.assertEqual(payload["tool_action"]["resource"], "build-cache")
        self.assertEqual(
            payload["tool_action"]["argv"], ["docker", "builder", "prune", "-f"]
        )
        self.assertEqual(payload["tool_action"]["reported"], "7.499GB")

    def test_invoke_tool_without_a_tool_action_is_rejected(self):
        with self.assertRaises(ValueError):
            self.build_tool_recommendation(tool_action=None)

    def test_non_tool_action_with_a_tool_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            self.build_tool_recommendation(action=ActionKind.DELETE_TREE)

    def test_path_recommendations_carry_no_tool_action(self):
        item = Recommendation(
            detector_id="node-modules",
            category="project-dependencies",
            label="node_modules",
            path=Path("/Users/test/app/node_modules"),
            action=ActionKind.DELETE_TREE,
            allocated_bytes=2048,
            reclaimable_bytes=2048,
            confidence=Confidence.STRONG,
            restoration=RestorationCost.REDOWNLOAD,
            selected_by_default=True,
            evidence=(),
            safety_root=Path("/Users/test/app"),
            reason="",
            generation=1,
        )

        self.assertIsNone(item.to_dict()["tool_action"])

    def test_invoke_tool_is_never_selected_by_default(self):
        item = self.build_tool_recommendation(selected_by_default=True)

        self.assertFalse(item.selected_by_default)


if __name__ == "__main__":
    unittest.main()
