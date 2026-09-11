import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from mac_dev_clean.fsevents import VolumeIdentity
from mac_dev_clean.index import (
    SCHEMA_VERSION,
    open_index,
    read_project_ndk_usage_snapshot,
)
from mac_dev_clean.ndk_usage import ProjectNdkUsage
from mac_dev_clean.recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    ToolUsage,
    ToolUsageState,
)


def build_recommendation(generation: int, path: str = "/Users/test/app/node_modules"):
    return Recommendation(
        detector_id="node-modules",
        category="project-dependencies",
        label="node_modules",
        path=Path(path),
        action=ActionKind.DELETE_TREE,
        allocated_bytes=4096,
        reclaimable_bytes=4096,
        confidence=Confidence.STRONG,
        restoration=RestorationCost.REBUILD,
        selected_by_default=True,
        evidence=(Evidence("lock-file", "pnpm-lock.yaml exists"),),
        safety_root=Path("/Users/test/app"),
        reason="Project inactive for 143 days.",
        generation=generation,
        last_activity_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class ScanIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "index.sqlite3"
        self.index = open_index(self.path)
        self.volume = VolumeIdentity(device=42, uuid="AAAA-BBBB")

    def tearDown(self):
        self.index.close()
        self.temp.cleanup()

    def test_schema_version_is_recorded(self):
        self.assertEqual(self.index.schema_version(), SCHEMA_VERSION)

    def test_project_ndk_usage_tracks_only_the_latest_complete_generation(self):
        self.index.begin_generation(self.volume, event_id=100)
        self.index.record_project_ndk_usage(
            ProjectNdkUsage(
                Path("/Users/test/z-app"),
                "27.0.12077973",
                ("android/build.gradle:ndkVersion",),
            )
        )
        self.index.record_project_ndk_usage(
            ProjectNdkUsage(
                Path("/Users/test/a-app"),
                None,
                ("android/CMakeLists.txt:native-marker",),
            )
        )
        self.index.complete_generation()

        original = self.index.load_project_ndk_usage_snapshot()
        self.assertIsNotNone(original)
        self.assertEqual(
            original.usages,
            (
                ProjectNdkUsage(
                    Path("/Users/test/a-app"),
                    None,
                    ("android/CMakeLists.txt:native-marker",),
                ),
                ProjectNdkUsage(
                    Path("/Users/test/z-app"),
                    "27.0.12077973",
                    ("android/build.gradle:ndkVersion",),
                ),
            ),
        )

        self.index.begin_generation(self.volume, event_id=200)
        self.index.record_project_ndk_usage(
            ProjectNdkUsage(
                Path("/Users/test/new"),
                None,
                ("android/CMakeLists.txt:native-marker",),
            )
        )
        self.index.abandon_generation()
        self.assertEqual(self.index.load_project_ndk_usage_snapshot(), original)

        self.index.begin_generation(self.volume, event_id=300)
        replacement = ProjectNdkUsage(
            Path("/Users/test/replacement"),
            "28.0.13004108",
            ("build.gradle:ndkVersion",),
        )
        self.index.record_project_ndk_usage(replacement)
        self.index.complete_generation()

        latest = self.index.load_project_ndk_usage_snapshot()
        self.assertIsNotNone(latest)
        self.assertEqual(latest.usages, (replacement,))

    def test_completing_new_generation_deletes_stale_usage_rows(self):
        first = self.index.begin_generation(self.volume, event_id=310)
        self.index.record_project_ndk_usage(
            ProjectNdkUsage(Path("/Users/test/stale"), None, ("old",))
        )
        self.index.complete_generation()

        second = self.index.begin_generation(self.volume, event_id=320)
        self.index.record_project_ndk_usage(
            ProjectNdkUsage(Path("/Users/test/current"), None, ("new",))
        )
        self.index.complete_generation()

        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT generation, project_path FROM project_ndk_usages"
                " ORDER BY generation, project_path"
            ).fetchall()
        finally:
            connection.close()

        self.assertEqual(rows, [(second, "/Users/test/current")])
        self.assertNotEqual(first, second)

    def test_failed_completion_rolls_back_recommendations_and_usage_across_reopen(self):
        first = self.index.begin_generation(self.volume, event_id=330)
        old_recommendation = build_recommendation(first, "/Users/test/old/node_modules")
        old_usage = ProjectNdkUsage(Path("/Users/test/old"), "27.0", ("old",))
        self.index.record_recommendation(old_recommendation)
        self.index.record_project_ndk_usage(old_usage)
        self.index.complete_generation()

        second = self.index.begin_generation(self.volume, event_id=340)
        new_recommendation = build_recommendation(second, "/Users/test/new/node_modules")
        self.index.record_recommendation(new_recommendation)
        self.index.record_project_ndk_usage(
            ProjectNdkUsage(Path("/Users/test/new"), "28.0", ("new",))
        )
        self.index.commit_batch()
        self.index.execute_for_test(
            "CREATE TRIGGER fail_old_usage_delete BEFORE DELETE ON project_ndk_usages "
            "WHEN OLD.generation != {0} BEGIN SELECT RAISE(ABORT, 'injected'); END".format(
                second
            )
        )

        with self.assertRaisesRegex(sqlite3.DatabaseError, "injected"):
            self.index.complete_generation()

        self.assertEqual(self.index.latest_complete_generation(), first)
        self.assertIsNotNone(self.index.load_recommendation(old_recommendation.id))
        self.assertEqual(self.index.load_project_ndk_usage_snapshot().usages, (old_usage,))

        self.index.begin_generation(self.volume, event_id=350)
        self.assertEqual(self.index.latest_complete_generation(), first)
        self.index.close()
        self.index = open_index(self.path)

        self.assertEqual(self.index.latest_complete_generation(), first)
        self.assertIsNotNone(self.index.load_recommendation(old_recommendation.id))
        self.assertEqual(self.index.load_project_ndk_usage_snapshot().usages, (old_usage,))

    def test_read_only_usage_loader_does_not_mutate_unusable_indexes(self):
        absent = Path(self.temp.name) / "absent.sqlite3"
        self.assertIsNone(read_project_ndk_usage_snapshot(absent))
        self.assertFalse(absent.exists())

        corrupt = Path(self.temp.name) / "corrupt.sqlite3"
        corrupt.write_bytes(b"this is not a database")
        corrupt_bytes = corrupt.read_bytes()
        self.assertIsNone(read_project_ndk_usage_snapshot(corrupt))
        self.assertEqual(corrupt.read_bytes(), corrupt_bytes)

        old_schema = Path(self.temp.name) / "old.sqlite3"
        connection = sqlite3.connect(str(old_schema))
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION - 1),),
        )
        connection.commit()
        connection.close()
        old_bytes = old_schema.read_bytes()

        self.assertIsNone(read_project_ndk_usage_snapshot(old_schema))
        self.assertEqual(old_schema.read_bytes(), old_bytes)

    def test_read_only_usage_loader_returns_a_complete_snapshot(self):
        path = Path(self.temp.name) / "snapshot #1?.sqlite3"
        index = open_index(path)
        index.begin_generation(self.volume, event_id=400)
        usage = ProjectNdkUsage(
            Path("/Users/test/app"),
            "27.0.12077973",
            ("android/build.gradle:ndkVersion",),
        )
        index.record_project_ndk_usage(usage)
        index.complete_generation()
        index.close()

        snapshot = read_project_ndk_usage_snapshot(path)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.usages, (usage,))
        self.assertIsNotNone(snapshot.completed_at.tzinfo)

    def test_read_only_usage_loader_rejects_out_of_range_timestamp_without_mutation(self):
        generation = self.index.begin_generation(self.volume, event_id=425)
        self.index.complete_generation()
        self.index.execute_for_test(
            "UPDATE generations SET completed_at = ? WHERE generation = ?",
            (1e300, generation),
        )
        before = self.path.read_bytes()

        self.assertIsNone(read_project_ndk_usage_snapshot(self.path))
        self.assertEqual(self.path.read_bytes(), before)

    def test_read_only_usage_loader_rejects_invalid_evidence_shapes_without_mutation(self):
        generation = self.index.begin_generation(self.volume, event_id=430)
        self.index.record_project_ndk_usage(
            ProjectNdkUsage(Path("/Users/test/app"), None, ("valid",))
        )
        self.index.complete_generation()

        invalid_shapes = ({"source": "marker"}, "marker", [1], None)
        for invalid in invalid_shapes:
            with self.subTest(evidence=invalid):
                self.index.execute_for_test(
                    "UPDATE project_ndk_usages SET evidence = ? WHERE generation = ?",
                    (json.dumps(invalid), generation),
                )
                before = self.path.read_bytes()

                self.assertIsNone(read_project_ndk_usage_snapshot(self.path))
                self.assertEqual(self.path.read_bytes(), before)

    def test_read_only_usage_loader_rejects_invalid_path_and_version_types(self):
        generation = self.index.begin_generation(self.volume, event_id=435)
        self.index.record_project_ndk_usage(
            ProjectNdkUsage(Path("/Users/test/app"), None, ("valid",))
        )
        self.index.complete_generation()

        corruptions = (
            ("project_path", sqlite3.Binary(b"/Users/test/app")),
            ("version", sqlite3.Binary(b"27.0.12077973")),
        )
        for column, invalid in corruptions:
            with self.subTest(column=column):
                self.index.execute_for_test(
                    "UPDATE project_ndk_usages SET project_path = ?, version = ?"
                    " WHERE generation = ?",
                    ("/Users/test/app", None, generation),
                )
                self.index.execute_for_test(
                    "UPDATE project_ndk_usages SET {0} = ? WHERE generation = ?".format(
                        column
                    ),
                    (invalid, generation),
                )
                before = self.path.read_bytes()

                self.assertIsNone(read_project_ndk_usage_snapshot(self.path))
                self.assertEqual(self.path.read_bytes(), before)

    def test_read_only_usage_loader_rejects_invalid_generation_row_types(self):
        corruptions = (
            (sqlite3.Binary(b"1"), 1.0),
            (1, sqlite3.Binary(b"1")),
        )
        for case, (generation, completed_at) in enumerate(corruptions):
            with self.subTest(case=case):
                path = Path(self.temp.name) / "invalid-generation-{0}.sqlite3".format(
                    case
                )
                connection = sqlite3.connect(str(path))
                connection.execute("CREATE TABLE meta (key, value)")
                connection.execute("CREATE TABLE generations (generation, completed_at)")
                connection.execute(
                    "CREATE TABLE project_ndk_usages"
                    " (generation, project_path, version, evidence)"
                )
                connection.execute(
                    "INSERT INTO meta VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute(
                    "INSERT INTO generations VALUES (?, ?)",
                    (generation, completed_at),
                )
                connection.commit()
                connection.close()
                before = path.read_bytes()

                self.assertIsNone(read_project_ndk_usage_snapshot(path))
                self.assertEqual(path.read_bytes(), before)

    def test_record_project_ndk_usage_requires_an_active_generation(self):
        with self.assertRaisesRegex(ValueError, "no generation is in progress"):
            self.index.record_project_ndk_usage(
                ProjectNdkUsage(Path("/Users/test/app"), None, ())
            )

    def test_record_project_ndk_usage_rejects_records_after_completion(self):
        self.index.begin_generation(self.volume, event_id=450)
        self.index.complete_generation()

        with self.assertRaisesRegex(ValueError, "no generation is in progress"):
            self.index.record_project_ndk_usage(
                ProjectNdkUsage(Path("/Users/test/late"), None, ())
            )

    def test_record_project_ndk_usage_normalizes_and_replaces_a_project_path(self):
        self.index.begin_generation(self.volume, event_id=500)
        self.index.record_project_ndk_usage(
            ProjectNdkUsage(Path("/Users/test/other/../app"), None, ("old",))
        )
        replacement = ProjectNdkUsage(
            Path("/Users/test/app"),
            "27.0.12077973",
            ("android/build.gradle:ndkVersion",),
        )
        self.index.record_project_ndk_usage(replacement)
        self.index.complete_generation()

        snapshot = self.index.load_project_ndk_usage_snapshot()

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.usages, (replacement,))

    def test_incomplete_generation_is_not_returned_as_latest(self):
        generation = self.index.begin_generation(self.volume, event_id=100)
        self.index.record_recommendation(build_recommendation(generation))
        self.index.commit_batch()

        self.assertIsNone(self.index.latest_complete_generation())

    def test_completed_generation_becomes_latest(self):
        generation = self.index.begin_generation(self.volume, event_id=100)
        self.index.record_recommendation(build_recommendation(generation))
        self.index.commit_batch()
        self.index.complete_generation()

        self.assertEqual(self.index.latest_complete_generation(), generation)

    def test_recommendations_are_only_loadable_from_a_complete_generation(self):
        generation = self.index.begin_generation(self.volume, event_id=100)
        item = build_recommendation(generation)
        self.index.record_recommendation(item)
        self.index.commit_batch()

        self.assertIsNone(self.index.load_recommendation(item.id))

        self.index.complete_generation()
        loaded = self.index.load_recommendation(item.id)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.path, Path("/Users/test/app/node_modules"))
        self.assertEqual(loaded.action, ActionKind.DELETE_TREE)
        self.assertEqual(loaded.confidence, Confidence.STRONG)
        self.assertEqual(loaded.evidence[0].code, "lock-file")
        self.assertEqual(loaded.safety_root, Path("/Users/test/app"))

    def test_a_newer_generation_supersedes_the_old_one(self):
        first = self.index.begin_generation(self.volume, event_id=100)
        stale = build_recommendation(first, "/Users/test/old/node_modules")
        self.index.record_recommendation(stale)
        self.index.commit_batch()
        self.index.complete_generation()

        second = self.index.begin_generation(self.volume, event_id=200)
        self.index.record_recommendation(build_recommendation(second))
        self.index.commit_batch()
        self.index.complete_generation()

        self.assertEqual(self.index.latest_complete_generation(), second)
        self.assertIsNone(self.index.load_recommendation(stale.id))

    def test_snapshot_reports_the_last_complete_scan_baseline(self):
        generation = self.index.begin_generation(self.volume, event_id=321)
        self.index.commit_batch()
        self.index.complete_generation()

        snapshot = self.index.snapshot()

        self.assertEqual(snapshot.generation, generation)
        self.assertEqual(snapshot.event_id, 321)
        self.assertEqual(snapshot.volume_uuid, "AAAA-BBBB")
        self.assertEqual(snapshot.device, 42)
        self.assertGreater(snapshot.completed_at, 0)

    def test_snapshot_is_none_before_any_complete_scan(self):
        self.assertIsNone(self.index.snapshot())

    def test_reset_discards_everything(self):
        generation = self.index.begin_generation(self.volume, event_id=100)
        item = build_recommendation(generation)
        self.index.record_recommendation(item)
        self.index.commit_batch()
        self.index.complete_generation()

        self.index.reset()

        self.assertIsNone(self.index.snapshot())
        self.assertIsNone(self.index.load_recommendation(item.id))
        self.assertEqual(self.index.schema_version(), SCHEMA_VERSION)

    def test_a_corrupt_database_file_is_rebuilt(self):
        self.index.close()
        self.path.write_bytes(b"this is not a database")

        rebuilt = open_index(self.path)
        try:
            self.assertEqual(rebuilt.schema_version(), SCHEMA_VERSION)
            self.assertIsNone(rebuilt.snapshot())
        finally:
            rebuilt.close()

    def test_an_older_schema_is_rebuilt(self):
        generation = self.index.begin_generation(self.volume, event_id=100)
        item = build_recommendation(generation)
        self.index.record_recommendation(item)
        self.index.commit_batch()
        self.index.complete_generation()

        self.index.execute_for_test(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION - 1),)
        )
        self.index.close()

        rebuilt = open_index(self.path)
        try:
            self.assertEqual(rebuilt.schema_version(), SCHEMA_VERSION)
            # A real rebuild discards the old database file (and everything in
            # it), rather than merely patching the version number back onto
            # the still-intact old tables. Assert the old data is gone -- this
            # is the part a vacuous version-only check cannot catch.
            self.assertIsNone(rebuilt.snapshot())
            self.assertIsNone(rebuilt.load_recommendation(item.id))
        finally:
            rebuilt.close()


class ToolRecommendationRoundTripTests(unittest.TestCase):
    def test_tool_usage_normalizes_to_a_stable_json_shape(self):
        usage = ToolUsage(
            state=ToolUsageState.MATCHED,
            projects=(
                "/Users/test/app-b",
                "/Users/test/other/../app-a",
                "/Users/test/app-b",
            ),
            unpinned_projects=(
                "/Users/test/app-c/.",
                "/Users/test/app-c",
            ),
            scan_completed_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
        )

        self.assertEqual(
            usage.to_dict(),
            {
                "state": "matched",
                "projects": ["/Users/test/app-a", "/Users/test/app-b"],
                "unpinned_projects": ["/Users/test/app-c"],
                "scan_completed_at": "2026-09-11T00:00:00+00:00",
            },
        )

    def test_tool_usage_rejects_state_invariant_violations(self):
        completed_at = datetime(2026, 9, 11, tzinfo=timezone.utc)
        cases = (
            (
                ToolUsageState.MATCHED,
                {"projects": (), "scan_completed_at": completed_at},
            ),
            (
                ToolUsageState.UNREFERENCED,
                {
                    "projects": ("/Users/test/app",),
                    "scan_completed_at": completed_at,
                },
            ),
            (
                ToolUsageState.UNKNOWN,
                {"projects": ("/Users/test/app",), "scan_completed_at": None},
            ),
            (
                ToolUsageState.UNKNOWN,
                {"projects": (), "scan_completed_at": completed_at},
            ),
        )
        for state, keyword_arguments in cases:
            with self.subTest(state=state, arguments=keyword_arguments):
                with self.assertRaises(ValueError):
                    ToolUsage(state=state, **keyword_arguments)

    def load_tool_payload(self, mutate):
        from mac_dev_clean.recommendation import ToolAction

        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        index = open_index(Path(temp.name) / "index.sqlite3")
        self.addCleanup(index.close)
        generation = index.begin_generation(
            VolumeIdentity(device=1, uuid="U"), event_id=1
        )
        item = Recommendation(
            detector_id="docker-build-cache",
            category="tool-managed",
            label="Docker build cache",
            path=Path("/Users/test/home"),
            action=ActionKind.INVOKE_TOOL,
            allocated_bytes=1,
            reclaimable_bytes=1,
            confidence=Confidence.EXACT,
            restoration=RestorationCost.EXTERNAL_STATE,
            selected_by_default=False,
            evidence=(),
            safety_root=Path("/Users/test/home"),
            reason="",
            generation=generation,
            tool_action=ToolAction(
                tool="docker",
                resource="build-cache",
                argv=("docker", "builder", "prune", "-f"),
                preview_argv=("docker", "system", "df"),
                reported="7.499GB",
            ),
        )
        index.record_recommendation(item)
        payload = item.to_dict()
        mutate(payload)
        index.execute_for_test(
            "UPDATE recommendations SET payload = ? WHERE id = ? AND generation = ?",
            (json.dumps(payload), item.id, generation),
        )
        index.complete_generation()
        return index.load_recommendation(item.id)

    def load_path_payload(self, mutate):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        index = open_index(Path(temp.name) / "index.sqlite3")
        self.addCleanup(index.close)
        generation = index.begin_generation(
            VolumeIdentity(device=1, uuid="U"), event_id=1
        )
        item = build_recommendation(generation)
        index.record_recommendation(item)
        payload = item.to_dict()
        mutate(payload)
        index.execute_for_test(
            "UPDATE recommendations SET payload = ? WHERE id = ? AND generation = ?",
            (json.dumps(payload), item.id, generation),
        )
        index.complete_generation()
        return index.load_recommendation(item.id)

    def test_a_tool_recommendation_survives_a_round_trip(self):
        from mac_dev_clean.recommendation import ToolAction

        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        index = open_index(Path(temp.name) / "index.sqlite3")
        self.addCleanup(index.close)
        index.begin_generation(VolumeIdentity(device=1, uuid="U"), event_id=1)
        item = Recommendation(
            detector_id="docker-build-cache",
            category="tool-managed",
            label="Docker build cache",
            path=Path("/Users/test/home"),
            action=ActionKind.INVOKE_TOOL,
            allocated_bytes=1,
            reclaimable_bytes=1,
            confidence=Confidence.EXACT,
            restoration=RestorationCost.EXTERNAL_STATE,
            selected_by_default=False,
            evidence=(),
            safety_root=Path("/Users/test/home"),
            reason="",
            generation=1,
            tool_action=ToolAction(
                tool="docker",
                resource="build-cache",
                argv=("docker", "builder", "prune", "--filter=label=a b", "-f"),
                preview_argv=(
                    "docker",
                    "system",
                    "df",
                    "--format",
                    "{{json .}}",
                ),
                reported="7.499GB",
            ),
            tool_usage=ToolUsage(
                state=ToolUsageState.MATCHED,
                projects=("/Users/test/app",),
                unpinned_projects=("/Users/test/unpinned",),
                scan_completed_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
            ),
        )
        index.record_recommendation(item)
        index.complete_generation()

        loaded = index.load_recommendation(item.id)

        self.assertIsNotNone(loaded.tool_action)
        self.assertEqual(loaded.tool_action, item.tool_action)
        self.assertEqual(loaded.tool_usage, item.tool_usage)

    def test_existing_path_recommendation_without_tool_usage_key_decodes(self):
        loaded = self.load_path_payload(lambda payload: payload.pop("tool_usage"))

        self.assertIsNotNone(loaded)
        self.assertIsNone(loaded.tool_usage)

    def test_non_tool_recommendation_rejects_tool_usage(self):
        item = build_recommendation(1)

        with self.assertRaisesRegex(ValueError, "only invoke_tool"):
            Recommendation(
                **{
                    **item.__dict__,
                    "tool_usage": ToolUsage(
                        state=ToolUsageState.UNKNOWN,
                        unpinned_projects=("/Users/test/app",),
                    ),
                }
            )

    def test_existing_path_recommendation_without_tool_action_key_decodes(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        index = open_index(Path(temp.name) / "index.sqlite3")
        self.addCleanup(index.close)
        generation = index.begin_generation(
            VolumeIdentity(device=1, uuid="U"), event_id=1
        )
        item = build_recommendation(generation)
        index.record_recommendation(item)
        payload = item.to_dict()
        del payload["tool_action"]
        index.execute_for_test(
            "UPDATE recommendations SET payload = ? WHERE id = ? AND generation = ?",
            (json.dumps(payload), item.id, generation),
        )
        index.complete_generation()

        loaded = index.load_recommendation(item.id)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.action, ActionKind.DELETE_TREE)
        self.assertIsNone(loaded.tool_action)

    def test_path_recommendation_with_null_tool_action_decodes(self):
        loaded = self.load_path_payload(
            lambda payload: payload.__setitem__("tool_action", None)
        )

        self.assertIsNotNone(loaded)
        self.assertIsNone(loaded.tool_action)

    def test_falsey_malformed_tool_action_payloads_are_rejected(self):
        for malformed, exception in (
            ({}, KeyError),
            ([], TypeError),
            ("", TypeError),
            (0, TypeError),
            (False, TypeError),
        ):
            with self.subTest(payload=malformed):
                with self.assertRaises(exception):
                    self.load_path_payload(
                        lambda payload, value=malformed: payload.__setitem__(
                            "tool_action", value
                        )
                    )

    def test_stored_whole_string_argv_is_rejected(self):
        def replace_argv(payload):
            payload["tool_action"]["argv"] = "docker builder prune -f"

        with self.assertRaises(TypeError):
            self.load_tool_payload(replace_argv)

    def test_stored_whole_string_preview_argv_is_rejected(self):
        def replace_preview_argv(payload):
            payload["tool_action"]["preview_argv"] = "docker system df"

        with self.assertRaises(TypeError):
            self.load_tool_payload(replace_preview_argv)

    def test_stored_tool_action_without_reported_uses_empty_string(self):
        def remove_reported(payload):
            del payload["tool_action"]["reported"]

        loaded = self.load_tool_payload(remove_reported)

        self.assertIsNotNone(loaded.tool_action)
        self.assertEqual(loaded.tool_action.reported, "")


if __name__ == "__main__":
    unittest.main()
