# Android NDK Project Usage and Tool Scan Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the unique projects using each installed Android NDK version and replace the Tool-managed refresh icon with cancellable in-page scan controls.

**Architecture:** Deep Scan performs a bounded metadata inspection for each discovered repository and stores one NDK usage fact per project in its generation-scoped SQLite index. Tool Scan reads that snapshot without modifying it, attaches typed usage information to NDK recommendations, and the SwiftUI page renders counts and disclosure lists. Tool inventory remains independently cancellable; destructive tool actions retain their existing non-cancellable handoff contract.

**Tech Stack:** Python 3.9 standard library, SQLite, `unittest`, Swift 6, SwiftUI, Swift Testing, XcodeGen.

**Spec:** `docs/superpowers/specs/2026-09-11-android-ndk-project-usage-and-tool-scan-controls-design.md`

## Global Constraints

- Python 3.9 is the floor (`requires-python = ">=3.9"`): use `Optional[T]`, `Tuple[T, ...]`, and `List[T]`, not PEP 604 unions, built-in generic aliases, `match`, or `tomllib`.
- Add no third-party Python or Swift dependencies.
- Count normalized repository roots, never declaration matches.
- Dynamic or conflicting Gradle expressions lower certainty to unpinned; they never become an exact version match.
- A cancelled or failed Deep Scan leaves the prior complete NDK usage generation authoritative.
- Tool Scan reads the Deep Scan index without creating, rebuilding, or mutating it.
- Android components remain unselected by default and removable only through the existing `sdkmanager` preview/revalidation/apply contract.
- Tool inventory cancellation must reach the existing contained process-tree cleanup; tool cleanup remains non-cancellable after handoff.
- Every new Python module must appear in both `inputFiles` and `outputFiles` in `macos/project.yml`, followed by `xcodegen generate --spec macos/project.yml`.
- Preserve the unrelated modifications already present in `AppModel.swift`, `ContentView.swift`, `DeepScanTests.swift`, `ToolsTests.swift`, `tool_executor.py`, and `test_tool_executor.py`; stage only reviewed feature hunks.
- Worker subagents do not commit or run the full suite. The coordinating agent performs review, focused verification, commits, and final full gates.
- Tests never invoke a real destructive command or read the developer's real cache indexes.

## File Structure

- Create `src/mac_dev_clean/ndk_usage.py`: bounded project metadata analyzer plus immutable usage/snapshot types.
- Create `tests/test_ndk_usage.py`: exact, unpinned, conflicting, malformed, and traversal-boundary tests.
- Modify `src/mac_dev_clean/index.py`: schema v2 table, generation lifecycle, read-only snapshot loader, and recommendation usage decoding.
- Modify `src/mac_dev_clean/deep_scan.py`: record one optional NDK usage fact per discovered repository.
- Modify `tests/test_index.py` and `tests/test_deep_scan.py`: persistence and generation atomicity.
- Modify `src/mac_dev_clean/recommendation.py`: typed tool usage payload.
- Modify `src/mac_dev_clean/tools/android.py`: match installed `ndk;<version>` resource identities to the snapshot.
- Modify `src/mac_dev_clean/tools/registry.py`: pass the immutable snapshot only to Android analysis.
- Modify `src/mac_dev_clean/cli.py`: add the explicit read-only project-index seam to `tools`.
- Modify `tests/test_android_tool.py`, `tests/test_tool_registry.py`, and `tests/test_tools_cli.py`: matching and CLI degradation tests.
- Modify `macos/Sources/MacDevCleanApp/DeepScanModels.swift`: decode usage states and provide pure display helpers.
- Modify `macos/Sources/MacDevCleanApp/AppModel.swift`: own the cancellable Tool Scan task and terminal state.
- Modify `macos/Sources/MacDevCleanApp/ContentView.swift`: remove toolbar refresh and add Start/Cancel/Scan Again controls plus NDK disclosure lists.
- Modify `macos/Tests/MacDevCleanAppTests/ToolsTests.swift`: payload, model cancellation, and presentation tests.
- Modify `macos/project.yml` and regenerate `macos/MacDevClean.xcodeproj`: bundle the new Python module.

---

### Task 1: Bounded project NDK metadata analyzer

**Files:**
- Create: `src/mac_dev_clean/ndk_usage.py`
- Create: `tests/test_ndk_usage.py`

**Interfaces:**
- Consumes: `mac_dev_clean.discovery.Repository`, repository-local metadata files.
- Produces: `ProjectNdkUsage`, `NdkUsageSnapshot`, and `analyze_project_ndk_usage(repository: Repository) -> Optional[ProjectNdkUsage]`.

- [ ] **Step 1: Write failing tests for exact declarations**

Create table-driven tests that build a temporary `.git` repository and assert these literals all produce the same normalized version:

```python
cases = (
    ("android/build.gradle", 'android { ndkVersion "27.0.12077973" }'),
    ("android/app/build.gradle.kts", 'android { ndkVersion = "27.0.12077973" }'),
    ("android/gradle.properties", "android.ndkVersion=27.0.12077973\n"),
    ("android/local.properties", "ndk.dir=/Users/test/home/Library/Android/sdk/ndk/27.0.12077973\n"),
)
with TemporaryDirectory() as raw_tmp:
    root = Path(raw_tmp)
    for index, (relative_path, source) in enumerate(cases):
        repository = make_repository(root / str(index), relative_path, source)
        usage = analyze_project_ndk_usage(repository)
        self.assertEqual(usage.version, "27.0.12077973")
        self.assertEqual(usage.project_path, repository.path)
```

Also assert evidence names the relative file and declaration kind without storing file contents.

- [ ] **Step 2: Run the exact-declaration tests and confirm RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_ndk_usage.ProjectNdkUsageTests.test_literal_versions
```

Expected: import failure because `mac_dev_clean.ndk_usage` does not exist.

- [ ] **Step 3: Define immutable usage types and bounded candidate discovery**

Implement these public types:

```python
@dataclass(frozen=True)
class ProjectNdkUsage:
    project_path: Path
    version: Optional[str]
    evidence: Tuple[str, ...]


@dataclass(frozen=True)
class NdkUsageSnapshot:
    completed_at: datetime
    usages: Tuple[ProjectNdkUsage, ...]
```

Implement `analyze_project_ndk_usage(repository)` using compiled regexes for literal Groovy/Kotlin `ndkVersion`, `android.ndkVersion`, and `ndk.dir`. Inspect only:

```python
_EXACT_FILES = (
    "build.gradle", "build.gradle.kts", "gradle.properties", "local.properties",
    "android/build.gradle", "android/build.gradle.kts",
    "android/gradle.properties", "android/local.properties",
    "android/app/build.gradle", "android/app/build.gradle.kts",
)
```

Add immediate Android module `build.gradle`/`build.gradle.kts` files via one bounded `android.iterdir()` pass. Refuse symlink files and symlink parent segments; open text as UTF-8 with bounded replacement and a per-file character cap. Let an `OSError` from a supported metadata file propagate so the caller can emit a warning. Normalize only printable version tokens matching `^[A-Za-z0-9][A-Za-z0-9._+-]*$`.

- [ ] **Step 4: Write failing tests for uncertainty and boundaries**

Cover all of these independently:

```python
self.assertIsNone(analyze_project_ndk_usage(plain_repository))
self.assertIsNone(dynamic_usage.version)       # ndkVersion rootProject.ext.ndkVersion
self.assertIsNone(conflicting_usage.version)   # two distinct literal versions
self.assertEqual(len(duplicate_usage.evidence), 2)
self.assertIsNone(outside_symlink_usage)
```

Add native markers for `externalNativeBuild`, an `ndk {` block, `android/CMakeLists.txt`, `android/app/src/main/cpp/CMakeLists.txt`, `Android.mk`, `Application.mk`, and conventional `android/app/.cxx` metadata. Assert each yields one unpinned record. Add a large nested dependency tree containing a fake declaration and assert it is never traversed.

- [ ] **Step 5: Implement conservative unpinned and conflict classification**

Collect all exact literals before deciding. Return an exact version only when the distinct normalized version set has one member. Return an unpinned record when native use exists but no exact version resolves, or when distinct exact versions conflict. Sort and deduplicate evidence before constructing the dataclass.

- [ ] **Step 6: Run analyzer tests and confirm GREEN**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_ndk_usage
```

Expected: all Task 1 tests pass.

- [ ] **Step 7: Coordinator review and commit**

After diff review:

```bash
git add src/mac_dev_clean/ndk_usage.py tests/test_ndk_usage.py
git commit -m "feat: detect project Android NDK usage"
```

---

### Task 2: Persist NDK usage with Deep Scan generation semantics

**Files:**
- Modify: `src/mac_dev_clean/index.py`
- Modify: `src/mac_dev_clean/deep_scan.py`
- Modify: `tests/test_index.py`
- Modify: `tests/test_deep_scan.py`

**Interfaces:**
- Consumes: `ProjectNdkUsage` and `analyze_project_ndk_usage` from Task 1.
- Produces: `ScanIndex.record_project_ndk_usage(usage: ProjectNdkUsage) -> None`, `ScanIndex.load_project_ndk_usage_snapshot() -> Optional[NdkUsageSnapshot]`, and `read_project_ndk_usage_snapshot(path: Path) -> Optional[NdkUsageSnapshot]`.

- [ ] **Step 1: Write failing index lifecycle tests**

Add tests that record exact and unpinned usages, complete the generation, and assert deterministic load order. Add a second incomplete generation with different records and assert the first snapshot remains authoritative. Then complete a third generation and assert old usage rows are gone.

```python
first = self.index.begin_generation(self.volume, event_id=100)
self.index.record_project_ndk_usage(
    ProjectNdkUsage(Path("/Users/test/app"), "27.0.12077973", ("android/build.gradle:ndkVersion",))
)
self.index.complete_generation()
original = self.index.load_project_ndk_usage_snapshot()

self.index.begin_generation(self.volume, event_id=200)
self.index.record_project_ndk_usage(
    ProjectNdkUsage(Path("/Users/test/new"), None, ("android/CMakeLists.txt:native-marker",))
)
self.index.abandon_generation()
self.assertEqual(self.index.load_project_ndk_usage_snapshot(), original)
```

Add a read-only loader test: absent, corrupt, and old-schema files return `None`, and their bytes/existence are unchanged by the call.

- [ ] **Step 2: Run index tests and confirm RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_index.ScanIndexTests
```

Expected: missing project usage methods.

- [ ] **Step 3: Add schema v2 storage and read-only loading**

Advance `SCHEMA_VERSION` to `2` and add:

```sql
CREATE TABLE IF NOT EXISTS project_ndk_usages (
    generation INTEGER NOT NULL,
    project_path TEXT NOT NULL,
    version TEXT,
    evidence TEXT NOT NULL,
    PRIMARY KEY (generation, project_path)
);
CREATE INDEX IF NOT EXISTS project_ndk_usages_by_generation
    ON project_ndk_usages (generation);
```

`record_project_ndk_usage` must require an active generation, normalize the path, serialize evidence as JSON, and use `INSERT OR REPLACE`. `complete_generation` must delete usage rows outside the newly completed generation in the same transaction as recommendation cleanup.

Implement `read_project_ndk_usage_snapshot(path)` with SQLite URI read-only mode:

```python
uri = "file:{}?mode=ro".format(quote(str(path.resolve()), safe="/"))
connection = sqlite3.connect(uri, uri=True)
```

Check schema version before querying, close on every path, return `None` on missing/corrupt/incompatible data, and never call `open_index` from this function.

- [ ] **Step 4: Write failing Deep Scan integration tests**

Create two Git repositories that both pin the same NDK version, one with no cleanup artifact. Run Deep Scan and assert the usage snapshot contains both unique repository roots. Add a cancellation test that first saves a complete exact snapshot, changes project metadata, cancels the next scan, and asserts the old exact snapshot remains.

Patch `analyze_project_ndk_usage` to raise `OSError("metadata denied")` for one repository and assert a warning is emitted while that repository's normal artifact recommendations are still processed.

- [ ] **Step 5: Record usage independently from artifact analysis**

In the repository loop, call the NDK analyzer in its own `try/except OSError` block before `analyze_repository`. Emit the existing warning event shape on failure, but continue into artifact analysis. Record a non-`None` usage through the same `pending`/`COMMIT_EVERY` batching lifecycle. Do not emit a cleanup candidate event for usage facts.

- [ ] **Step 6: Run focused persistence tests and confirm GREEN**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_index tests.test_deep_scan
```

Expected: all index and Deep Scan tests pass.

- [ ] **Step 7: Coordinator review and commit**

```bash
git add src/mac_dev_clean/index.py src/mac_dev_clean/deep_scan.py tests/test_index.py tests/test_deep_scan.py
git commit -m "feat: persist NDK usage from deep scans"
```

---

### Task 3: Attach project usage to installed NDK recommendations

**Files:**
- Modify: `src/mac_dev_clean/recommendation.py`
- Modify: `src/mac_dev_clean/index.py`
- Modify: `src/mac_dev_clean/tools/android.py`
- Modify: `src/mac_dev_clean/tools/registry.py`
- Modify: `tests/test_index.py`
- Modify: `tests/test_android_tool.py`
- Modify: `tests/test_tool_registry.py`

**Interfaces:**
- Consumes: `Optional[NdkUsageSnapshot]` from Task 2 and installed SDK resource IDs such as `ndk;27.0.12077973`.
- Produces: `ToolUsageState`, `ToolUsage`, optional `Recommendation.tool_usage`, and `analyze_android(..., ndk_usage_snapshot: Optional[NdkUsageSnapshot] = None)`.

- [ ] **Step 1: Write failing value-model and persistence tests**

Define expected construction and JSON shape in tests:

```python
usage = ToolUsage(
    state=ToolUsageState.MATCHED,
    projects=("/Users/test/app-a", "/Users/test/app-b"),
    unpinned_projects=("/Users/test/app-c",),
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
```

Round-trip a tool recommendation with usage through `ScanIndex`, and verify older stored recommendation payloads without `tool_usage` still decode to `None`. Reject a `tool_usage` payload attached to a non-`INVOKE_TOOL` recommendation.

- [ ] **Step 2: Run value-model tests and confirm RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_index.ToolRecommendationRoundTripTests
```

Expected: `ToolUsage`/`tool_usage` do not exist.

- [ ] **Step 3: Implement the typed payload**

Add:

```python
class ToolUsageState(Enum):
    MATCHED = "matched"
    UNREFERENCED = "unreferenced"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolUsage:
    state: ToolUsageState
    projects: Tuple[str, ...] = ()
    unpinned_projects: Tuple[str, ...] = ()
    scan_completed_at: Optional[datetime] = None
```

Normalize each path lexically, sort/deduplicate both path tuples, and enforce invariants: `MATCHED` requires non-empty `projects`; `UNREFERENCED` and `UNKNOWN` require empty `projects`; `UNKNOWN` has no completion time. Add `tool_usage: Optional[ToolUsage] = None` to `Recommendation`, serialize it as `tool_usage`, and decode the field backward-compatibly in `index._decode`.

- [ ] **Step 4: Write failing Android matching tests**

Use an SDK fixture whose sdkmanager row deliberately differs between resource and display revision:

```text
ndk;27.0.12077973 | 27.0.1 | NDK | ndk/27.0.12077973
```

Pass a snapshot containing two exact `27.0.12077973` projects, a different version, and one unpinned project. Assert the recommendation matches by `package.path.split(";", 1)[1]`, not `package.version`; paths are unique/sorted; and the unpinned path is separate. Also assert:

- complete snapshot with no exact match => `UNREFERENCED`;
- `None` snapshot => `UNKNOWN`;
- CMake/platform/system-image/AVD recommendations have `tool_usage is None`.

- [ ] **Step 5: Implement Android matching and registry plumbing**

Add the optional snapshot parameter at the end of `analyze_android` so current callers remain source-compatible. Build one immutable lookup before iterating packages:

```python
exact_projects = {}  # type: Dict[str, Set[str]]
unpinned_projects = set()  # type: Set[str]
```

Attach `ToolUsage` only when `package.path` has exactly the `ndk;<version>` shape. Pass `ndk_usage_snapshot` through `collect_tool_recommendations(..., ndk_usage_snapshot=None)` to Android only. Preserve analyzer isolation and all existing status behavior.

- [ ] **Step 6: Run matching tests and confirm GREEN**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_android_tool tests.test_tool_registry tests.test_index
```

Expected: all focused tests pass.

- [ ] **Step 7: Coordinator review and commit**

```bash
git add src/mac_dev_clean/recommendation.py src/mac_dev_clean/index.py src/mac_dev_clean/tools/android.py src/mac_dev_clean/tools/registry.py tests/test_index.py tests/test_android_tool.py tests/test_tool_registry.py
git commit -m "feat: annotate installed NDKs with project usage"
```

---

### Task 4: Read the project snapshot in the `tools` command

**Files:**
- Modify: `src/mac_dev_clean/cli.py`
- Modify: `tests/test_tools_cli.py`

**Interfaces:**
- Consumes: `read_project_ndk_usage_snapshot(path)` and `collect_tool_recommendations(..., ndk_usage_snapshot=...)` from Tasks 2–3.
- Produces: `tools --project-index PATH`; default is `DEFAULT_INDEX_PATH` while `--index` remains the dedicated actionable tool index.

- [ ] **Step 1: Write failing CLI tests**

Patch `read_project_ndk_usage_snapshot` and `collect_tool_recommendations`, invoke the parser/`run_tools` through existing test seams with two temporary index paths, and assert:

```python
read_snapshot.assert_called_once_with(project_index_path)
collect.assert_called_once_with(
    generation=unittest.mock.ANY,
    home=Path.home(),
    ndk_usage_snapshot=snapshot,
)
```

Add cases where the read-only loader returns `None` and raises `OSError`; both must still collect and return independent tool results with an unknown NDK usage state. Confirm the tool recommendation index path remains the target of apply persistence, never the project index path.

- [ ] **Step 2: Run CLI tests and confirm RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_tools_cli
```

Expected: parser rejects `--project-index` or the snapshot is not passed.

- [ ] **Step 3: Add the explicit index seam and fail-open read path**

Add to the `tools` parser:

```python
tools_parser.add_argument(
    "--project-index",
    type=Path,
    default=None,
    help="Read-only Deep Scan project index path.",
)
```

In `run_tools`, resolve `getattr(args, "project_index", None)` or `DEFAULT_INDEX_PATH.expanduser()` so direct legacy `Namespace` test callers remain compatible. Call the read-only loader inside a narrow `try/except (OSError, sqlite3.DatabaseError, ValueError)` after adding the standard-library `sqlite3` import, and pass either the snapshot or `None` into collection. Do not include this path in `tools-apply`; apply continues to trust only the dedicated tool index. Update existing `collect(generation, home)` test doubles in `tests/test_tools_cli.py` to accept `ndk_usage_snapshot=None` explicitly; this is a signature migration, not a reason to loosen assertions with arbitrary `**kwargs`.

- [ ] **Step 4: Run CLI tests and confirm GREEN**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_tools_cli
```

Expected: all CLI tests pass.

- [ ] **Step 5: Verify JSON output contract**

Run a fixture-backed test of `run_tools` and assert each NDK recommendation includes `tool_usage`, while existing recommendation keys and `reclaimable_total_bytes` remain unchanged.

- [ ] **Step 6: Coordinator review and commit**

```bash
git add src/mac_dev_clean/cli.py tests/test_tools_cli.py
git commit -m "feat: join tool inventory with deep scan usage"
```

---

### Task 5: Decode and render NDK usage in SwiftUI

**Files:**
- Modify: `macos/Sources/MacDevCleanApp/DeepScanModels.swift`
- Modify: `macos/Sources/MacDevCleanApp/ContentView.swift`
- Modify: `macos/Tests/MacDevCleanAppTests/ToolsTests.swift`

**Interfaces:**
- Consumes: optional `tool_usage` JSON from Task 3.
- Produces: `ToolUsagePayload`, `ToolUsageState`, `ToolRecommendation.usageSummary`, and disclosure sections rendered by `ToolManagedView`.

- [ ] **Step 1: Write failing decode and formatting tests**

Extend the NDK JSON fixture with:

```json
"tool_usage": {
  "state": "matched",
  "projects": ["/Users/test/app-a", "/Users/test/app-b"],
  "unpinned_projects": ["/Users/test/app-c"],
  "scan_completed_at": "2026-09-11T00:00:00+00:00"
}
```

Assert matched decodes to two exact paths and formats `Used by 2 projects`. Add unreferenced (`Not referenced by scanned projects`), unknown (`Usage unknown — run Deep Scan`), singular (`Used by 1 project`), and missing-key (`nil`) fixtures. Assert disclosure sections are titled `Matching version` and `Uses an unpinned NDK` with deterministic paths.

- [ ] **Step 2: Run focused Swift tests and confirm RED**

Run:

```bash
swift test --package-path macos --filter toolReportDecodesNdkUsage
```

Expected: missing usage model/property.

- [ ] **Step 3: Add Swift usage models and pure display helpers**

Add:

```swift
enum ToolUsageState: String, Decodable, Hashable, Sendable {
    case matched
    case unreferenced
    case unknown
}

struct ToolUsagePayload: Decodable, Hashable, Sendable {
    let state: ToolUsageState
    let projects: [String]
    let unpinnedProjects: [String]
    let scanCompletedAt: String?

    private enum CodingKeys: String, CodingKey {
        case state, projects
        case unpinnedProjects = "unpinned_projects"
        case scanCompletedAt = "scan_completed_at"
    }
}
```

Add `let toolUsage: ToolUsagePayload?` to `ToolRecommendation` mapped from `tool_usage`. Keep missing-key decoding backward-compatible through optional synthesis/manual decoding. Implement nonisolated pure computed helpers for summary text and sections so tests do not need to inspect the SwiftUI tree.

- [ ] **Step 4: Render the usage summary and disclosure list**

In `recommendationRow`, place the summary after `reason`. If exact or unpinned paths exist, render a `DisclosureGroup` with separately labelled sections. Each path must use caption monospaced text, `.lineLimit(1)`, `.truncationMode(.middle)`, and `.textSelection(.enabled)`. Do not add Finder buttons or change the destructive confirmation.

- [ ] **Step 5: Run focused Swift tests and confirm GREEN**

Run:

```bash
swift test --package-path macos --filter Tool
```

Expected: usage decode/format tests and existing Tool tests pass.

- [ ] **Step 6: Coordinator review and commit**

Stage only Task 5 hunks in files that already contain unrelated edits:

```bash
git add -p macos/Sources/MacDevCleanApp/DeepScanModels.swift macos/Sources/MacDevCleanApp/ContentView.swift macos/Tests/MacDevCleanAppTests/ToolsTests.swift
git commit -m "feat: show projects using Android NDKs"
```

---

### Task 6: Replace Tool Scan toolbar refresh with cancellable in-page controls

**Files:**
- Modify: `macos/Sources/MacDevCleanApp/AppModel.swift`
- Modify: `macos/Sources/MacDevCleanApp/ContentView.swift`
- Modify: `macos/Tests/MacDevCleanAppTests/ToolsTests.swift`

**Interfaces:**
- Consumes: cancellation-aware `ToolBackendProtocol.loadTools()`.
- Produces: `AppModel.startToolScan() async`, `AppModel.cancelToolScan()`, `toolScanWasCancelled`, and `toolScanButtonTitle` values `Start Tool Scan`/`Scan Again`.

- [ ] **Step 1: Write failing AppModel state tests**

Extend the stub with a hanging backend whose cancellation handler records cancellation, following the existing `HangingDeepScanBackend` pattern. Assert:

```swift
let scan = Task { await model.startToolScan() }
// wait until backend reports started
#expect(model.activity == .loadingTools)
#expect(model.toolScanButtonTitle == "Start Tool Scan")
model.cancelToolScan()
await scan.value
#expect(!model.isBusy)
#expect(model.toolScanWasCancelled)
#expect(model.errorMessage == nil)
#expect(await recorder.wasCancelled)
```

Add a completed scan test asserting `toolScanButtonTitle == "Scan Again"`, and a cancelled scan test asserting no partial report is exposed. Keep the existing real backend process-tree cancellation tests as the lower-layer proof.

- [ ] **Step 2: Run AppModel tests and confirm RED**

Run:

```bash
swift test --package-path macos --filter cancelToolScanStopsTheRunningScan
```

Expected: missing start/cancel APIs and cancellation state.

- [ ] **Step 3: Make AppModel own the Tool Scan task**

Add:

```swift
@Published private(set) var toolScanWasCancelled = false
private var toolScanTask: Task<ToolReport, Error>?
```

Rename the user-facing entry point to `startToolScan() async`; keep `loadTools(preservingMessages:)` private only if apply-error refresh still requires it. At scan start: guard idle/backend, clear `toolReport`, clear cancellation state, set `.loadingTools`, and dismiss messages. Store the backend task before awaiting it. Catch `CancellationError` separately, set `toolScanWasCancelled = true`, and do not set an error/warning. On every terminal path, clear the stored task, restore `.idle`, and refresh disk space.

`cancelToolScan()` sets the cancellation flag and calls `toolScanTask?.cancel()` without prematurely setting activity idle; completion owns final state. Preserve the current rule that a failed tool action does not automatically replace its reviewed report unless the existing indeterminate-error path explicitly calls the private refresh helper.

- [ ] **Step 4: Replace toolbar UI with in-page controls**

Delete the `selectedPage == .tools` toolbar branch. In `ToolManagedView`, add an in-page controls row below the overview:

```swift
if model.activity.showsToolScanIndicator {
    Button("Cancel Scan") { model.cancelToolScan() }
} else {
    Button(model.toolScanButtonTitle) {
        Task { await model.startToolScan() }
    }
    .buttonStyle(.borderedProminent)
}
```

Show the existing progress indicator/message alongside the controls. Show `Scan cancelled.` in orange when `toolScanWasCancelled`. Avoid a duplicate full-page progress placeholder: the empty state remains below the controls while status is visible above it. Continue disabling per-item `Run` through `model.isBusy`.

- [ ] **Step 5: Run focused Swift tests and confirm GREEN**

Run:

```bash
swift test --package-path macos --filter Tool
```

Expected: model state, process cancellation, decoding, and prior tool cleanup tests pass.

- [ ] **Step 6: Mutation check the cancellation proof**

Temporarily replace `toolScanTask?.cancel()` with a no-op, rerun `cancelToolScanStopsTheRunningScan`, and confirm it fails or times out within its bounded test deadline. Restore the implementation and rerun the focused test to GREEN.

- [ ] **Step 7: Coordinator review and commit**

```bash
git add -p macos/Sources/MacDevCleanApp/AppModel.swift macos/Sources/MacDevCleanApp/ContentView.swift macos/Tests/MacDevCleanAppTests/ToolsTests.swift
git commit -m "feat: add cancellable Tool Scan controls"
```

---

### Task 7: Bundle, verify, and manually accept the integrated feature

**Files:**
- Modify: `macos/project.yml`
- Regenerate: `macos/MacDevClean.xcodeproj/project.pbxproj`
- Verify only: all feature and existing source/test files.

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces: a native app bundle containing `ndk_usage.py`, passing automated gates, and read-only manual acceptance evidence.

- [ ] **Step 1: Register the new Python module**

Add these exact entries beside the other root Python modules:

```yaml
# inputFiles
- $(SRCROOT)/../src/mac_dev_clean/ndk_usage.py

# outputFiles
- $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/ndk_usage.py
```

- [ ] **Step 2: Regenerate the Xcode project**

Run:

```bash
xcodegen generate --spec macos/project.yml
```

Expected: command succeeds and only the intended project-file changes appear.

- [ ] **Step 3: Run the complete Python suite**

Run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Expected: all tests pass with no real destructive external-tool calls.

- [ ] **Step 4: Run the complete Swift suite**

Run:

```bash
swift test --package-path macos
```

Expected: all tests pass.

- [ ] **Step 5: Build and inspect the native artifact**

Run:

```bash
./scripts/build_macos_app.sh
/usr/bin/test -f dist/mac-dev-clean.app/Contents/Resources/python/mac_dev_clean/ndk_usage.py
```

Expected: app build succeeds and the bundled module exists.

- [ ] **Step 6: Run repository safety gates**

Run:

```bash
./scripts/audit_public_repo.sh
git diff --check
```

Expected: public-repository audit, Gitleaks when available, and whitespace checks pass.

- [ ] **Step 7: Read-only CLI acceptance against controlled indexes**

Use temporary fixture repositories and explicit temporary paths:

```bash
PYTHONPATH=src python3 -m mac_dev_clean deep-scan --root /tmp/mac-dev-clean-ndk-fixtures --index /tmp/mac-dev-clean-project-index.sqlite3 --json --no-fsevents
PYTHONPATH=src python3 -m mac_dev_clean tools --project-index /tmp/mac-dev-clean-project-index.sqlite3 --index /tmp/mac-dev-clean-tools-index.sqlite3 --json
```

Expected: the installed matching NDK row contains the fixture project paths; unpinned fixtures are separate; no tool action is invoked. If no matching NDK is installed on the machine, use the tested fixture runner for this integration proof rather than fabricating CLI output.

- [ ] **Step 8: Native UI acceptance**

Launch the built app and verify:

1. Tool-managed has `Start Tool Scan` and no toolbar refresh icon.
2. Starting changes the control to `Cancel Scan` and shows progress.
3. Cancelling returns to idle, shows `Scan cancelled.`, and leaves no contained inventory subprocess.
4. Completing changes the button to `Scan Again`.
5. An NDK row shows the correct summary and disclosure lists; paths middle-truncate and remain selectable.
6. Existing per-row cleanup confirmation still shows the exact command and remains disabled during scans.

Do not invoke a destructive Tool-managed `Run` action for this feature acceptance.

- [ ] **Step 9: Coordinator final diff review and commit**

Verify the staged set excludes pre-existing unrelated hunks, then:

```bash
git add macos/project.yml macos/MacDevClean.xcodeproj/project.pbxproj
git commit -m "build: bundle NDK project usage analysis"
```

- [ ] **Step 10: Adversarial review**

Review the complete feature diff against the spec, with special attention to path traversal, symlink handling, SQLite read-only behavior, generation atomicity, dynamic Gradle false positives, cancellation propagation, and accidental broadening of SDK removal authority. Resolve all Important or higher findings and rerun affected focused tests plus Tasks 3–6 gates.
