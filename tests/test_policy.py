import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mac_dev_clean.discovery import Repository, RepositoryKind
from mac_dev_clean.policy import recommend
from mac_dev_clean.projects import RECIPES, ArtifactFact
from mac_dev_clean.recommendation import ActionKind, Confidence, RestorationCost

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
RECIPE_BY_ID = {recipe.detector_id: recipe for recipe in RECIPES}


def fact(detector_id="node-modules", **overrides):
    recipe = RECIPE_BY_ID[detector_id]
    repository = Repository(
        path=Path("/Users/test/app"),
        kind=RepositoryKind.PRIMARY,
        git_dir=Path("/Users/test/app/.git"),
    )
    defaults = dict(
        repository=repository,
        recipe=recipe,
        path=Path("/Users/test/app") / recipe.relative_path,
        kind=recipe.kind,
        allocated_bytes=5 * 1024 * 1024,
        lock_satisfied=True,
        lock_detail="pnpm-lock.yaml exists",
        manifest_detail="package.json exists",
        last_activity_at=NOW - timedelta(days=143),
        inactive=True,
    )
    defaults.update(overrides)
    return ArtifactFact(**defaults)


class PolicyTests(unittest.TestCase):
    def test_inactive_project_with_a_lock_file_is_selected(self):
        item = recommend(fact(), generation=1, now=NOW)

        self.assertTrue(item.selected_by_default)
        self.assertEqual(item.action, ActionKind.DELETE_TREE)
        self.assertEqual(item.confidence, Confidence.STRONG)
        self.assertEqual(item.restoration, RestorationCost.REDOWNLOAD)
        self.assertIn("143 days", item.reason)

    def test_active_project_is_reported_but_not_selected(self):
        item = recommend(
            fact(inactive=False, last_activity_at=NOW - timedelta(days=3)),
            generation=1,
            now=NOW,
        )

        self.assertFalse(item.selected_by_default)
        self.assertIn("changed 3 days ago", item.reason)

    def test_dependency_tree_without_a_lock_file_is_never_selected(self):
        item = recommend(fact(lock_satisfied=False, lock_detail="no supported lock file"), generation=1, now=NOW)

        self.assertFalse(item.selected_by_default)
        self.assertEqual(item.confidence, Confidence.HEURISTIC)
        self.assertIn("lock file", item.reason)

    def test_unknown_activity_is_never_selected(self):
        item = recommend(fact(last_activity_at=None, inactive=False), generation=1, now=NOW)

        self.assertFalse(item.selected_by_default)
        self.assertEqual(item.confidence, Confidence.UNKNOWN)

    def test_inactive_build_output_is_selected_without_a_lock_file(self):
        item = recommend(
            fact("ios-build", lock_satisfied=True, lock_detail="", manifest_detail="ios/*.xcodeproj exists"),
            generation=1,
            now=NOW,
        )

        self.assertTrue(item.selected_by_default)
        self.assertEqual(item.restoration, RestorationCost.REBUILD)

    def test_every_recommendation_carries_evidence_and_a_safety_root(self):
        item = recommend(fact(), generation=9, now=NOW)

        codes = {entry.code for entry in item.evidence}
        self.assertIn("manifest", codes)
        self.assertIn("inactivity", codes)
        self.assertEqual(item.safety_root, Path("/Users/test/app"))
        self.assertEqual(item.generation, 9)

    def test_recommendations_warn_about_open_build_tools(self):
        item = recommend(fact(), generation=1, now=NOW)

        self.assertIn("Close", item.warning)

    def test_policy_is_deterministic(self):
        first = recommend(fact(), generation=1, now=NOW).to_dict()
        second = recommend(fact(), generation=1, now=NOW).to_dict()

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
