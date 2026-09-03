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


if __name__ == "__main__":
    unittest.main()
