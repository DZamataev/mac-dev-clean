import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from mac_dev_clean.discovery import Repository, RepositoryKind
from mac_dev_clean.policy import recommend
from mac_dev_clean.projects import (
    INACTIVITY_DAYS,
    ArtifactKind,
    analyze_repository,
    last_meaningful_activity,
)
from mac_dev_clean.recommendation import Confidence

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=400)
RECENT = NOW - timedelta(days=2)
BIG = b"x" * (2 * 1024 * 1024)


def touch(path: Path, when: datetime, payload: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stamp = when.timestamp()
    os.utime(str(path), (stamp, stamp))


def primary(root: Path) -> Repository:
    git_dir = root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    touch(git_dir / "HEAD", RECENT)
    return Repository(path=root, kind=RepositoryKind.PRIMARY, git_dir=git_dir)


class MeaningfulActivityTests(unittest.TestCase):
    def test_source_changes_set_the_activity_timestamp(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            touch(root / "package.json", OLD)

            activity = last_meaningful_activity(root, now=NOW)

            self.assertIsNotNone(activity)
            self.assertLess(activity, NOW - timedelta(days=INACTIVITY_DAYS))

    def test_git_internals_do_not_count_as_activity(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            repo = primary(root)
            touch(repo.git_dir / "index", RECENT)
            touch(root / "src" / "main.ts", OLD)

            activity = last_meaningful_activity(root, now=NOW)

            self.assertLess(activity, NOW - timedelta(days=INACTIVITY_DAYS))

    def test_generated_directories_do_not_count_as_activity(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            touch(root / "node_modules" / "pkg" / "index.js", RECENT)
            touch(root / "ios" / "build" / "app.o", RECENT)
            touch(root / ".DS_Store", RECENT)

            activity = last_meaningful_activity(root, now=NOW)

            self.assertLess(activity, NOW - timedelta(days=INACTIVITY_DAYS))

    def test_a_recent_source_edit_keeps_the_project_active(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            touch(root / "src" / "util.ts", RECENT)

            activity = last_meaningful_activity(root, now=NOW)

            self.assertGreater(activity, NOW - timedelta(days=INACTIVITY_DAYS))

    def test_a_lock_file_edit_counts_as_activity(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            touch(root / "pnpm-lock.yaml", RECENT)

            activity = last_meaningful_activity(root, now=NOW)

            self.assertGreater(activity, NOW - timedelta(days=INACTIVITY_DAYS))

    def test_a_deeply_nested_source_edit_keeps_the_project_active(self):
        # Java/Kotlin package layouts under android/src/main/java/com/... are
        # routine in React Native / Android projects and commonly exceed 8
        # directory levels. A recent edit that far down must still count.
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            deep_file = (
                root
                / "android"
                / "src"
                / "main"
                / "java"
                / "com"
                / "example"
                / "app"
                / "feature"
                / "module"
                / "detail"
                / "view"
                / "widget"
                / "Widget.kt"
            )
            self.assertGreaterEqual(
                len(deep_file.relative_to(root).parts) - 1,
                12,
                "fixture must place the file at least 12 levels below the repo root",
            )
            touch(deep_file, RECENT)

            activity = last_meaningful_activity(root, now=NOW)

            self.assertGreater(activity, NOW - timedelta(days=INACTIVITY_DAYS))

            repo = Repository(path=root, kind=RepositoryKind.PRIMARY, git_dir=root / ".git")
            facts = analyze_repository(repo, now=NOW)
            for fact in facts:
                self.assertFalse(
                    fact.inactive,
                    "a project with a recent deeply-nested edit must not be inactive",
                )


class NodeModulesRecipeTests(unittest.TestCase):
    def build(self, root: Path, lock_name: str = "pnpm-lock.yaml", age=OLD) -> Path:
        primary(root)
        touch(root / "src" / "main.ts", age)
        touch(root / "package.json", age, b"{}")
        if lock_name:
            touch(root / lock_name, age)
        touch(root / "node_modules" / "pkg" / "index.js", age, BIG)
        return root / "node_modules"

    def test_node_modules_with_a_lock_file_is_found(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            expected = self.build(root)

            facts = analyze_repository(primary(root), now=NOW)

            node = [item for item in facts if item.recipe.detector_id == "node-modules"]
            self.assertEqual(len(node), 1)
            self.assertEqual(node[0].path, expected)
            self.assertTrue(node[0].lock_satisfied)
            self.assertGreater(node[0].allocated_bytes, 1024)

    def test_node_modules_without_a_lock_file_is_reported_but_not_lock_satisfied(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.build(root, lock_name="")

            facts = analyze_repository(primary(root), now=NOW)

            node = [item for item in facts if item.recipe.detector_id == "node-modules"]
            self.assertEqual(len(node), 1)
            self.assertFalse(node[0].lock_satisfied)

    def test_node_modules_without_a_package_manifest_is_not_a_candidate(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "node_modules" / "pkg" / "index.js", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)

            self.assertEqual(
                [item for item in facts if item.recipe.detector_id == "node-modules"], []
            )

    def test_every_supported_javascript_lock_file_satisfies_the_recipe(self):
        for lock_name in (
            "package-lock.json",
            "npm-shrinkwrap.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "bun.lock",
            "bun.lockb",
        ):
            with self.subTest(lock=lock_name), TemporaryDirectory() as temp:
                root = Path(temp)
                self.build(root, lock_name=lock_name)

                facts = analyze_repository(primary(root), now=NOW)
                node = [item for item in facts if item.recipe.detector_id == "node-modules"]

                self.assertTrue(node[0].lock_satisfied, lock_name)


class NativeRecipeTests(unittest.TestCase):
    def test_ios_pods_requires_a_podfile_lock(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "ios" / "Podfile", OLD)
            touch(root / "ios" / "Pods" / "lib.a", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)
            pods = [item for item in facts if item.recipe.detector_id == "ios-pods"]

            self.assertEqual(len(pods), 1)
            self.assertFalse(pods[0].lock_satisfied)

            touch(root / "ios" / "Podfile.lock", OLD)
            facts = analyze_repository(primary(root), now=NOW)
            pods = [item for item in facts if item.recipe.detector_id == "ios-pods"]

            self.assertTrue(pods[0].lock_satisfied)

    def test_ios_build_requires_an_xcode_project_or_workspace(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "ios" / "build" / "app.o", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)
            self.assertEqual(
                [item for item in facts if item.recipe.detector_id == "ios-build"], []
            )

            (root / "ios" / "App.xcodeproj").mkdir(parents=True)
            facts = analyze_repository(primary(root), now=NOW)
            builds = [item for item in facts if item.recipe.detector_id == "ios-build"]

            self.assertEqual(len(builds), 1)
            self.assertEqual(builds[0].kind, ArtifactKind.BUILD_OUTPUT)

    def test_android_build_requires_gradle_settings_and_an_app_module(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "android" / "app" / "build" / "out.dex", OLD, BIG)
            touch(root / "android" / "app" / ".cxx" / "obj.o", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)
            self.assertEqual(
                [item for item in facts if item.recipe.detector_id.startswith("android")], []
            )

            touch(root / "android" / "settings.gradle", OLD)
            touch(root / "android" / "app" / "build.gradle", OLD)
            facts = analyze_repository(primary(root), now=NOW)
            ids = {item.recipe.detector_id for item in facts}

            self.assertIn("android-app-build", ids)
            self.assertIn("android-app-cxx", ids)

    def test_rust_target_requires_cargo_manifest_and_lock(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "Cargo.toml", OLD)
            touch(root / "target" / "debug" / "bin", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)
            rust = [item for item in facts if item.recipe.detector_id == "rust-target"]
            self.assertFalse(rust[0].lock_satisfied)

            touch(root / "Cargo.lock", OLD)
            facts = analyze_repository(primary(root), now=NOW)
            rust = [item for item in facts if item.recipe.detector_id == "rust-target"]
            self.assertTrue(rust[0].lock_satisfied)

    def test_python_venv_needs_a_deterministic_lock_file(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "pyproject.toml", OLD)
            touch(root / "requirements.txt", OLD)
            touch(root / ".venv" / "lib" / "site.py", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)
            venv = [item for item in facts if item.recipe.detector_id == "python-venv"]

            self.assertEqual(len(venv), 1)
            self.assertFalse(
                venv[0].lock_satisfied,
                "requirements.txt alone must not qualify a venv for default selection",
            )

            touch(root / "uv.lock", OLD)
            facts = analyze_repository(primary(root), now=NOW)
            venv = [item for item in facts if item.recipe.detector_id == "python-venv"]

            self.assertTrue(venv[0].lock_satisfied)


class ArtifactRecencyTests(unittest.TestCase):
    # The activity signal that drives preselection must be blind about the
    # *source tree* only being allowed to skip generated directories; it must
    # never be blind about the artifact itself. A file hand-edited today
    # inside node_modules/.venv/vendor/bundle (a patch-package fix, a
    # `pip install -e`, a locally patched gem) has to keep that specific
    # artifact out of default selection, even though the surrounding project
    # looks abandoned.
    def test_a_fresh_file_inside_node_modules_keeps_the_artifact_active(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            touch(root / "package.json", OLD, b"{}")
            touch(root / "pnpm-lock.yaml", OLD)
            touch(root / "node_modules" / "leftpad" / "index.js", OLD, BIG)
            # A patch-package-style hand fix landed inside node_modules today.
            touch(root / "node_modules" / "leftpad" / "patched.js", NOW)

            facts = analyze_repository(primary(root), now=NOW)
            node = next(f for f in facts if f.recipe.detector_id == "node-modules")

            self.assertFalse(
                node.inactive,
                "node_modules containing a file edited today must not be "
                "reported inactive, even though the project source is old",
            )

    def test_a_fresh_file_inside_a_python_venv_keeps_the_artifact_active(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "pyproject.toml", OLD)
            touch(root / "uv.lock", OLD)
            touch(root / ".venv" / "lib" / "site.py", OLD, BIG)
            # e.g. `pip install -e .` writing an egg-link/pth file today.
            touch(root / ".venv" / "lib" / "editable_install.pth", NOW)

            facts = analyze_repository(primary(root), now=NOW)
            venv = next(f for f in facts if f.recipe.detector_id == "python-venv")

            self.assertFalse(
                venv.inactive,
                "a .venv touched today must not be reported inactive",
            )

    def test_a_fresh_file_inside_vendor_bundle_keeps_the_artifact_active(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "Gemfile", OLD)
            touch(root / "Gemfile.lock", OLD)
            touch(root / "vendor" / "bundle" / "gems" / "foo" / "lib.rb", OLD, BIG)
            # A hand-patched gem landed inside vendor/bundle today.
            touch(root / "vendor" / "bundle" / "gems" / "foo" / "patched.rb", NOW)

            facts = analyze_repository(primary(root), now=NOW)
            gem = next(f for f in facts if f.recipe.detector_id == "ruby-bundle")

            self.assertFalse(
                gem.inactive,
                "vendor/bundle containing a file edited today must not be "
                "reported inactive",
            )

    def test_a_genuinely_abandoned_node_modules_is_still_inactive(self):
        # No-regression: when the artifact is exactly as old as everything
        # else, it must still be reported inactive and still preselected.
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            touch(root / "package.json", OLD, b"{}")
            touch(root / "pnpm-lock.yaml", OLD)
            touch(root / "node_modules" / "leftpad" / "index.js", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)
            node = next(f for f in facts if f.recipe.detector_id == "node-modules")

            self.assertTrue(node.inactive)

            item = recommend(node, generation=1, now=NOW)
            self.assertTrue(item.selected_by_default)
            self.assertEqual(item.confidence, Confidence.STRONG)

    def test_a_fresh_node_modules_is_not_preselected_by_policy(self):
        # End-to-end analyzer + policy: the recommendation must not be
        # offered as STRONG/preselected when the artifact itself is fresh.
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            touch(root / "package.json", OLD, b"{}")
            touch(root / "pnpm-lock.yaml", OLD)
            touch(root / "node_modules" / "leftpad" / "index.js", OLD, BIG)
            touch(root / "node_modules" / "leftpad" / "patched.js", NOW)

            facts = analyze_repository(primary(root), now=NOW)
            node = next(f for f in facts if f.recipe.detector_id == "node-modules")
            item = recommend(node, generation=1, now=NOW)

            self.assertFalse(item.selected_by_default)
            self.assertNotEqual(item.confidence, Confidence.STRONG)


class SafetyTests(unittest.TestCase):
    def test_a_symlinked_artifact_is_never_a_candidate(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "package.json", OLD, b"{}")
            touch(root / "pnpm-lock.yaml", OLD)
            elsewhere = root / "shared_modules"
            elsewhere.mkdir()
            touch(elsewhere / "pkg" / "index.js", OLD, BIG)
            os.symlink(str(elsewhere), str(root / "node_modules"))

            facts = analyze_repository(primary(root), now=NOW)

            self.assertEqual(
                [item for item in facts if item.recipe.detector_id == "node-modules"], []
            )

    def test_a_symlinked_marker_does_not_gate_a_recipe(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            outside = Path(temp) / "outside_project"
            touch(outside / "project.pbxproj", OLD, b"{}")
            (root / "ios").mkdir()
            os.symlink(str(outside), str(root / "ios" / "App.xcodeproj"))
            touch(root / "ios" / "Podfile.lock", OLD)
            touch(root / "ios" / "build" / "output.bin", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)

            self.assertEqual(
                [item for item in facts if item.recipe.detector_id == "ios-build"], []
            )

    def test_a_worktree_is_analysed_but_never_yields_its_own_root(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            main = root / "main"
            primary(main)
            worktree = root / "wt"
            worktree.mkdir()
            (worktree / ".git").write_text("gitdir: {}/.git/worktrees/wt\n".format(main))
            touch(worktree / "package.json", OLD, b"{}")
            touch(worktree / "pnpm-lock.yaml", OLD)
            touch(worktree / "node_modules" / "pkg" / "index.js", OLD, BIG)

            repo = Repository(
                path=worktree,
                kind=RepositoryKind.WORKTREE,
                git_dir=main / ".git" / "worktrees" / "wt",
            )
            facts = analyze_repository(repo, now=NOW)

            self.assertTrue(facts)
            for fact in facts:
                self.assertNotEqual(fact.path, worktree)
                self.assertTrue(str(fact.path).startswith(str(worktree) + os.sep))

    def test_tiny_artifacts_are_ignored(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "package.json", OLD, b"{}")
            touch(root / "pnpm-lock.yaml", OLD)
            touch(root / "node_modules" / "pkg" / "index.js", OLD, b"tiny")

            facts = analyze_repository(primary(root), now=NOW)

            self.assertEqual(facts, [])

    def test_an_ancestor_symlink_never_yields_a_candidate_outside_the_repo(self):
        # The `android` directory itself is a symlink pointing at storage
        # outside the repository. `android/app/build` is not itself a
        # symlink, so a leaf-only `is_symlink()` check would miss this: the
        # artifact resolves outside the project and must never be a
        # candidate.
        with TemporaryDirectory() as outside_temp, TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)

            outside = Path(outside_temp) / "SHARED_STORAGE_OUTSIDE_PROJECT"
            touch(outside / "settings.gradle", OLD)
            touch(outside / "app" / "build.gradle", OLD)
            touch(outside / "app" / "build" / "important.bin", OLD, BIG)

            os.symlink(str(outside), str(root / "android"))

            facts = analyze_repository(primary(root), now=NOW)

            self.assertEqual(
                [item for item in facts if item.recipe.detector_id.startswith("android")],
                [],
            )

    def test_a_real_deep_path_inside_the_repo_is_still_a_candidate(self):
        # Proves the ancestor-symlink guard does not simply disable the
        # recipe: a genuine, non-symlinked android/app/build inside the
        # repository must still be found.
        with TemporaryDirectory() as temp:
            root = Path(temp)
            primary(root)
            touch(root / "src" / "main.ts", OLD)
            touch(root / "android" / "settings.gradle", OLD)
            touch(root / "android" / "app" / "build.gradle", OLD)
            touch(root / "android" / "app" / "build" / "important.bin", OLD, BIG)

            facts = analyze_repository(primary(root), now=NOW)
            ids = {item.recipe.detector_id for item in facts}

            self.assertIn("android-app-build", ids)


if __name__ == "__main__":
    unittest.main()
