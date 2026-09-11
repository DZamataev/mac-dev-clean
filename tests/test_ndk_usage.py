import os
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mac_dev_clean.discovery import Repository, RepositoryKind
from mac_dev_clean.ndk_usage import (
    NdkUsageSnapshot,
    ProjectNdkUsage,
    analyze_project_ndk_usage,
)


VERSION = "27.0.12077973"


def make_repository(root: Path, relative_path: str, source: str) -> Repository:
    (root / ".git").mkdir(parents=True)
    metadata = root / relative_path
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(source, encoding="utf-8")
    return Repository(
        path=root,
        kind=RepositoryKind.PRIMARY,
        git_dir=root / ".git",
    )


class ProjectNdkUsageTests(unittest.TestCase):
    def test_literal_versions(self):
        cases = (
            ("android/build.gradle", 'android { ndkVersion "27.0.12077973" }', "ndkVersion"),
            (
                "android/app/build.gradle.kts",
                'android { ndkVersion = "27.0.12077973" }',
                "ndkVersion",
            ),
            (
                "android/gradle.properties",
                "android.ndkVersion=27.0.12077973\n",
                "android.ndkVersion",
            ),
            (
                "android/local.properties",
                "ndk.dir=/Users/test/home/Library/Android/sdk/ndk/27.0.12077973\n",
                "ndk.dir",
            ),
        )
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            for index, (relative_path, source, declaration_kind) in enumerate(cases):
                with self.subTest(relative_path=relative_path):
                    repository = make_repository(root / str(index), relative_path, source)

                    usage = analyze_project_ndk_usage(repository)

                    self.assertIsNotNone(usage)
                    self.assertEqual(usage.version, VERSION)
                    self.assertEqual(usage.project_path, repository.path)
                    self.assertEqual(
                        usage.evidence,
                        ("{0}:{1}".format(relative_path, declaration_kind),),
                    )
                    self.assertNotIn(source, usage.evidence)

    def test_dynamic_ndk_version_is_unpinned(self):
        with TemporaryDirectory() as raw_tmp:
            repository = make_repository(
                Path(raw_tmp) / "app",
                "android/build.gradle",
                "android { ndkVersion rootProject.ext.ndkVersion }",
            )

            usage = analyze_project_ndk_usage(repository)

            self.assertIsNotNone(usage)
            self.assertIsNone(usage.version)
            self.assertEqual(usage.evidence, ("android/build.gradle:ndkVersion",))

    def test_quoted_prefix_dynamic_ndk_versions_are_unpinned(self):
        expressions = (
            'ndkVersion = "27.0.12077973" + suffix',
            "ndkVersion '27.0.12077973' + suffix",
        )
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            for index, expression in enumerate(expressions):
                with self.subTest(expression=expression):
                    repository = make_repository(
                        root / str(index),
                        "android/build.gradle",
                        "android {{ {0} }}".format(expression),
                    )

                    usage = analyze_project_ndk_usage(repository)

                    self.assertIsNotNone(usage)
                    self.assertIsNone(usage.version)
                    self.assertEqual(
                        usage.evidence, ("android/build.gradle:ndkVersion",)
                    )

    def test_multiline_quoted_prefix_expressions_are_unpinned(self):
        continuation_operators = (
            "+",
            "?:",
            ".",
            "==",
            "!=",
            "<=",
            ">=",
            "=~",
            "==~",
            "**",
            "as",
        )
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            for index, operator in enumerate(continuation_operators):
                with self.subTest(operator=operator):
                    repository = make_repository(
                        root / str(index),
                        "android/build.gradle",
                        'android {{\n  ndkVersion = "27.0.12077973"\n    {0} suffix\n}}'.format(
                            operator
                        ),
                    )

                    usage = analyze_project_ndk_usage(repository)

                    self.assertIsNotNone(usage)
                    self.assertIsNone(usage.version)
                    self.assertEqual(
                        usage.evidence, ("android/build.gradle:ndkVersion",)
                    )

    def test_literal_line_boundary_and_division_code_remain_supported(self):
        with TemporaryDirectory() as raw_tmp:
            repository = make_repository(
                Path(raw_tmp) / "app",
                "android/build.gradle",
                'def ratio = total / count\nndkVersion = "27.0.12077973"\ndef next = 1',
            )

            usage = analyze_project_ndk_usage(repository)

            self.assertIsNotNone(usage)
            self.assertEqual(usage.version, VERSION)
            self.assertEqual(usage.evidence, ("android/build.gradle:ndkVersion",))

    def test_comments_and_unrelated_strings_do_not_create_usage(self):
        sources = (
            '// ndkVersion = "27.0.12077973"; externalNativeBuild { }; ndk { }',
            '/* ndkVersion = "27.0.12077973"; externalNativeBuild { }; ndk { } */',
            'val message = "ndkVersion = \'27.0.12077973\'; externalNativeBuild { }; ndk { }"',
        )
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            for index, source in enumerate(sources):
                with self.subTest(source=source):
                    repository = make_repository(
                        root / str(index), "android/build.gradle.kts", source
                    )

                    self.assertIsNone(analyze_project_ndk_usage(repository))

    def test_groovy_slashy_strings_do_not_create_usage(self):
        marker_text = (
            'ndkVersion = "27.0.12077973"; externalNativeBuild { }; ndk { }'
        )
        sources = (
            "def pattern = /{0}/".format(marker_text),
            "def pattern = $/{0}/$".format(marker_text),
            "return /{0}/".format(marker_text),
            "throw /{0}/".format(marker_text),
            "assert /{0}/".format(marker_text),
            "def pattern = $/escaped close /$$ then {0}/$".format(marker_text),
        )
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            for index, source in enumerate(sources):
                with self.subTest(source=source):
                    repository = make_repository(
                        root / str(index), "android/build.gradle", source
                    )

                    self.assertIsNone(analyze_project_ndk_usage(repository))

    def test_conflicting_literal_versions_are_unpinned(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "app"
            repository = make_repository(
                root,
                "android/build.gradle",
                'android { ndkVersion "27.0.12077973" }',
            )
            (root / "android/app").mkdir()
            (root / "android/app/build.gradle.kts").write_text(
                'android { ndkVersion = "26.3.11579264" }', encoding="utf-8"
            )

            usage = analyze_project_ndk_usage(repository)

            self.assertIsNotNone(usage)
            self.assertIsNone(usage.version)
            self.assertEqual(len(usage.evidence), 2)

    def test_native_markers_are_unpinned(self):
        cases = (
            (
                "android/build.gradle",
                "android { externalNativeBuild { cmake { } } }",
                "android/build.gradle",
                "externalNativeBuild",
            ),
            (
                "android/build.gradle",
                "android { ndk { abiFilters 'arm64-v8a' } }",
                "android/build.gradle",
                "ndk",
            ),
            (
                "android/CMakeLists.txt",
                "cmake_minimum_required(VERSION 3.22)",
                "android/CMakeLists.txt",
                "native-metadata",
            ),
            (
                "android/app/src/main/cpp/CMakeLists.txt",
                "project(native_app)",
                "android/app/src/main/cpp/CMakeLists.txt",
                "native-metadata",
            ),
            (
                "android/Android.mk",
                "LOCAL_PATH := $(call my-dir)",
                "android/Android.mk",
                "native-metadata",
            ),
            (
                "android/Application.mk",
                "APP_ABI := arm64-v8a",
                "android/Application.mk",
                "native-metadata",
            ),
            (
                "android/app/.cxx/configure_model.json",
                "{}",
                "android/app/.cxx",
                "native-metadata",
            ),
        )
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            for index, case in enumerate(cases):
                relative_path, source, evidence_path, marker_kind = case
                with self.subTest(relative_path=relative_path):
                    repository = make_repository(root / str(index), relative_path, source)

                    usage = analyze_project_ndk_usage(repository)

                    self.assertIsNotNone(usage)
                    self.assertIsNone(usage.version)
                    self.assertEqual(
                        usage.evidence,
                        ("{0}:{1}".format(evidence_path, marker_kind),),
                    )

    def test_plain_repository_has_no_ndk_usage(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "app"
            (root / ".git").mkdir(parents=True)
            repository = Repository(root, RepositoryKind.PRIMARY, root / ".git")

            self.assertIsNone(analyze_project_ndk_usage(repository))

    def test_duplicate_version_declarations_keep_both_evidence_items(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "app"
            repository = make_repository(
                root,
                "android/build.gradle",
                'android { ndkVersion "27.0.12077973" }',
            )
            (root / "android/library").mkdir()
            (root / "android/library/build.gradle.kts").write_text(
                'android { ndkVersion = "27.0.12077973" }', encoding="utf-8"
            )

            usage = analyze_project_ndk_usage(repository)

            self.assertIsNotNone(usage)
            self.assertEqual(usage.version, VERSION)
            self.assertEqual(len(usage.evidence), 2)

    def test_symlinked_metadata_outside_repository_is_ignored(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            outside = root / "outside.gradle"
            outside.write_text(
                'android { ndkVersion "27.0.12077973" }', encoding="utf-8"
            )
            repository_root = root / "app"
            (repository_root / ".git").mkdir(parents=True)
            (repository_root / "android").mkdir()
            os.symlink(str(outside), str(repository_root / "android/build.gradle"))
            repository = Repository(
                repository_root,
                RepositoryKind.PRIMARY,
                repository_root / ".git",
            )

            self.assertIsNone(analyze_project_ndk_usage(repository))

    def test_symlinked_metadata_parent_outside_repository_is_ignored(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            outside_android = root / "outside-android"
            outside_android.mkdir()
            (outside_android / "build.gradle").write_text(
                'android { ndkVersion "27.0.12077973" }', encoding="utf-8"
            )
            repository_root = root / "app"
            (repository_root / ".git").mkdir(parents=True)
            os.symlink(str(outside_android), str(repository_root / "android"))
            repository = Repository(
                repository_root,
                RepositoryKind.PRIMARY,
                repository_root / ".git",
            )

            self.assertIsNone(analyze_project_ndk_usage(repository))

    def test_malformed_literal_versions_are_unpinned(self):
        malformed_versions = ("", "../27.0", "27.0 beta", "版本")
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            for index, malformed in enumerate(malformed_versions):
                with self.subTest(version=malformed):
                    repository = make_repository(
                        root / str(index),
                        "android/build.gradle",
                        'android {{ ndkVersion "{0}" }}'.format(malformed),
                    )

                    usage = analyze_project_ndk_usage(repository)

                    self.assertIsNotNone(usage)
                    self.assertIsNone(usage.version)

    def test_nested_dependency_tree_is_not_traversed(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "app"
            (root / ".git").mkdir(parents=True)
            fake = root / "android/node_modules/vendor/deep/build.gradle"
            fake.parent.mkdir(parents=True)
            fake.write_text(
                'android { ndkVersion "27.0.12077973" }', encoding="utf-8"
            )
            repository = Repository(root, RepositoryKind.PRIMARY, root / ".git")

            self.assertIsNone(analyze_project_ndk_usage(repository))

    def test_declaration_beyond_per_file_cap_is_not_inspected(self):
        with TemporaryDirectory() as raw_tmp:
            repository = make_repository(
                Path(raw_tmp) / "app",
                "android/build.gradle",
                (" " * (256 * 1024))
                + 'android { ndkVersion "27.0.12077973" }',
            )

            self.assertIsNone(analyze_project_ndk_usage(repository))

    def test_truncated_quoted_prefix_dynamic_expression_is_unpinned(self):
        declaration = 'ndkVersion = "27.0.12077973"'
        prefix = " " * ((256 * 1024) - len(declaration)) + declaration
        with TemporaryDirectory() as raw_tmp:
            repository = make_repository(
                Path(raw_tmp) / "app",
                "android/build.gradle",
                prefix + " + suffix",
            )

            usage = analyze_project_ndk_usage(repository)

            self.assertIsNotNone(usage)
            self.assertIsNone(usage.version)
            self.assertEqual(usage.evidence, ("android/build.gradle:ndkVersion",))

    def test_invalid_utf8_is_replaced_while_reading_bounded_metadata(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp) / "app"
            repository = make_repository(root, "android/build.gradle", "placeholder")
            (root / "android/build.gradle").write_bytes(
                b"\xffandroid { ndkVersion \"27.0.12077973\" }"
            )

            usage = analyze_project_ndk_usage(repository)

            self.assertIsNotNone(usage)
            self.assertEqual(usage.version, VERSION)

    def test_supported_metadata_read_error_propagates(self):
        with TemporaryDirectory() as raw_tmp:
            repository = make_repository(
                Path(raw_tmp) / "app",
                "android/build.gradle",
                'android { ndkVersion "27.0.12077973" }',
            )

            with patch.object(Path, "open", side_effect=OSError("metadata denied")):
                with self.assertRaisesRegex(OSError, "metadata denied"):
                    analyze_project_ndk_usage(repository)

    def test_usage_types_are_immutable(self):
        usage = ProjectNdkUsage(Path("/project"), VERSION, ("build.gradle:ndkVersion",))
        snapshot = NdkUsageSnapshot(
            datetime(2026, 9, 11, tzinfo=timezone.utc),
            (usage,),
        )

        with self.assertRaises(FrozenInstanceError):
            usage.version = None
        with self.assertRaises(FrozenInstanceError):
            snapshot.usages = ()


if __name__ == "__main__":
    unittest.main()
