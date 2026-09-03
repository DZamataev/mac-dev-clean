import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mac_dev_clean.discovery import (
    Repository,
    RepositoryKind,
    classify_git_entry,
    discover_repositories,
)


def make_primary(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return root


class ClassifyTests(unittest.TestCase):
    def test_a_git_directory_is_a_primary_repository(self):
        with TemporaryDirectory() as temp:
            repo = make_primary(Path(temp) / "app")

            found = classify_git_entry(repo)

            self.assertEqual(found.kind, RepositoryKind.PRIMARY)
            self.assertEqual(found.path, repo)
            self.assertEqual(found.git_dir, repo / ".git")

    def test_a_worktree_git_file_is_recognised(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            main = make_primary(root / "main")
            worktree = root / "wt"
            worktree.mkdir()
            (worktree / ".git").write_text(
                "gitdir: {}/.git/worktrees/wt\n".format(main)
            )

            found = classify_git_entry(worktree)

            self.assertEqual(found.kind, RepositoryKind.WORKTREE)

    def test_a_submodule_git_file_is_recognised(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_primary(root / "super")
            sub = root / "super" / "sub"
            sub.mkdir()
            (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")

            found = classify_git_entry(sub)

            self.assertEqual(found.kind, RepositoryKind.SUBMODULE)

    def test_a_submodule_under_a_directory_named_worktrees_is_a_submodule(self):
        # Regression: classification must be anchored to the segment
        # directly above the gitdir target, not a substring search over
        # the whole path. A user directory literally named "worktrees"
        # must not hijack the decision when the real marker is "modules".
        with TemporaryDirectory() as temp:
            root = Path(temp)
            outer = root / "worktrees" / "super"
            make_primary(outer)
            sub = outer / "sub"
            sub.mkdir()
            (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")

            found = classify_git_entry(sub)

            self.assertEqual(found.kind, RepositoryKind.SUBMODULE)

    def test_a_linked_worktree_of_a_submodule_is_a_worktree(self):
        # Regression: the gitdir contains both "modules" and "worktrees"
        # segments (.../.git/modules/sub/worktrees/wt). Only the segment
        # directly above the target ("worktrees") should decide the kind.
        with TemporaryDirectory() as temp:
            root = Path(temp)
            main = make_primary(root / "super")
            worktree = root / "wt-of-sub"
            worktree.mkdir()
            (worktree / ".git").write_text(
                "gitdir: {}/.git/modules/sub/worktrees/wt\n".format(main)
            )

            found = classify_git_entry(worktree)

            self.assertEqual(found.kind, RepositoryKind.WORKTREE)

    def test_a_directory_without_git_metadata_is_not_a_repository(self):
        with TemporaryDirectory() as temp:
            self.assertIsNone(classify_git_entry(Path(temp)))

    def test_an_unparseable_git_file_is_not_a_repository(self):
        with TemporaryDirectory() as temp:
            root = Path(temp) / "weird"
            root.mkdir()
            (root / ".git").write_text("this is not a gitdir pointer\n")

            self.assertIsNone(classify_git_entry(root))


class DiscoveryTests(unittest.TestCase):
    def test_repositories_are_found_at_any_depth_without_a_developer_root(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            shallow = make_primary(root / "app")
            deep = make_primary(root / "a" / "b" / "c" / "deep")

            found = {item.path for item in discover_repositories([root])}

            self.assertIn(shallow, found)
            self.assertIn(deep, found)

    def test_nested_repositories_are_both_reported(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            outer = make_primary(root / "outer")
            inner = make_primary(root / "outer" / "packages" / "inner")

            found = {item.path for item in discover_repositories([root])}

            self.assertEqual(found, {outer, inner})

    def test_dependency_and_build_directories_are_never_descended(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_primary(root / "app")
            buried = make_primary(root / "app" / "node_modules" / "pkg")

            found = {item.path for item in discover_repositories([root])}

            self.assertNotIn(buried, found)

    def test_git_internals_are_never_descended(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            repo = make_primary(root / "app")
            buried = repo / ".git" / "modules" / "x"
            buried.mkdir(parents=True)
            (buried / ".git").mkdir()

            found = {item.path for item in discover_repositories([root])}

            self.assertEqual(found, {repo})

    def test_symlinked_directories_are_not_followed(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            real = make_primary(root / "real")
            link = root / "link"
            os.symlink(str(root / "real"), str(link))

            found = {item.path for item in discover_repositories([root])}

            self.assertEqual(found, {real})

    def test_the_same_directory_reached_twice_is_reported_once(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            repo = make_primary(root / "app")

            found = list(discover_repositories([root, root, root / "app"]))

            self.assertEqual([item.path for item in found], [repo])

    def test_cancellation_stops_the_walk(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(20):
                make_primary(root / "repo{}".format(index))

            found = list(discover_repositories([root], should_cancel=lambda: True))

            self.assertEqual(found, [])

    def test_unreadable_directories_are_skipped_without_failing(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            good = make_primary(root / "good")
            locked = root / "locked"
            locked.mkdir()
            os.chmod(str(locked), 0o000)
            try:
                found = {item.path for item in discover_repositories([root])}
            finally:
                os.chmod(str(locked), 0o755)

            self.assertEqual(found, {good})

    def test_progress_reports_visited_directories(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            make_primary(root / "app")
            seen = []

            list(discover_repositories([root], on_progress=seen.append))

            self.assertTrue(any(str(root) in path for path in seen))


if __name__ == "__main__":
    unittest.main()
