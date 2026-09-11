# Android NDK Project Usage and Tool Scan Controls Design

**Date:** 2026-09-11  
**Status:** Proposed  
**Project:** mac-dev-clean

## Summary

Connect Android NDK inventory to the latest successful Deep Scan so each installed NDK version shows how many scanned projects use it and exposes the matching project paths. Replace the Tool-managed toolbar refresh icon with in-page text controls matching the Deep Scan interaction: start, cancel, progress, and scan again.

The project usage result is advisory evidence. It improves deletion decisions but does not broaden cleanup authority: Android SDK components remain unselected by default and are removed only through `sdkmanager` after the existing preview and revalidation checks.

## Goals

1. Count unique scanned repositories that explicitly reference each installed Android NDK version.
2. Let the user expand an NDK recommendation to inspect the matching project paths.
3. Distinguish an exact version match, unpinned NDK use, no reference, and unavailable Deep Scan evidence.
4. Preserve the last complete project usage snapshot when a Deep Scan is cancelled or fails.
5. Make Tool Scan start and cancellation visible inside the Tool-managed page.
6. Terminate a cancelled Tool Scan through the existing contained process-tree cancellation path.

## Non-goals

1. Resolve arbitrary Gradle expressions, custom plugins, version catalogs, or environment-dependent build logic.
2. Claim that an unreferenced NDK is safe to remove; projects outside the scanned roots may still require it.
3. Attribute an unpinned project to whichever NDK happens to be newest or installed.
4. Rescan repositories as part of Tool Scan.
5. Add automatic selection or automatic NDK removal.
6. Cancel a tool cleanup after its destructive command has been handed off.

## Domain Model

### Project NDK usage

A Deep Scan emits at most one `ProjectNdkUsage` record per normalized repository path:

- `project_path`: normalized repository root;
- `version`: exact normalized NDK version, or absent when use is detected but no version can be resolved;
- `evidence`: the relative file and declaration kind that established the result;
- `generation`: the Deep Scan generation that produced the record.

The persistent index treats `(generation, project_path)` as the identity. Multiple matching declarations inside one repository do not increase the count. If declarations in one repository resolve to conflicting exact versions, the repository is recorded as unpinned/ambiguous rather than attributed to either version.

### Tool usage presentation

A tool recommendation may carry an optional typed `usage` payload:

- `state`: `matched`, `unreferenced`, or `unknown`;
- `projects`: sorted unique project paths with an exact version match;
- `unpinned_projects`: sorted unique paths that use NDK without a resolvable version;
- `scan_completed_at`: completion time of the Deep Scan snapshot, when available.

Only Android NDK recommendations populate this payload initially. `matched` means `projects` is non-empty. `unreferenced` means a complete Deep Scan snapshot exists but contains no exact match. `unknown` means no complete Deep Scan snapshot is available. Unpinned projects are displayed separately and never counted against a concrete installed version.

## Detecting NDK Use

Deep Scan performs a bounded, read-only inspection after discovering each Git repository. It does not execute Gradle or source shell configuration.

Exact versions are accepted only from literal declarations in supported project metadata:

- `ndkVersion "<version>"` and `ndkVersion = "<version>"` in Groovy or Kotlin Gradle files;
- `android.ndkVersion=<version>` in `gradle.properties`;
- `ndk.dir=<path>` in `local.properties` when the final canonical path component is an NDK version.

Inspection is limited to repository-root Gradle files and conventional Android locations, including `android/`, `android/app/`, and their immediate Gradle module files. Generated dependency trees and unrelated source files are not recursively searched.

NDK use without a resolvable version is established conservatively by supported native-build markers such as a literal `externalNativeBuild`/`ndk` Gradle block, `CMakeLists.txt`, `Android.mk`, `Application.mk`, or existing `.cxx` metadata under the conventional Android tree. A version declaration itself also establishes NDK use.

Version strings are compared to the version segment in the installed package resource (`ndk;<version>`), not to a display label. Values must be non-empty printable version tokens; malformed values are ignored. Symlinks are not followed. Read failures produce no record for that repository and follow Deep Scan's existing warning behavior rather than inventing an unpinned result.

## Persistence and Snapshot Semantics

The main Deep Scan SQLite index gains a `project_ndk_usages` table keyed by generation and normalized project path. The schema version is advanced; because this index is a disposable cache, an incompatible prior index is rebuilt through the existing reset-on-version-mismatch behavior.

Records are written in the same generation transaction lifecycle as recommendations:

1. begin a Deep Scan generation;
2. record repository recommendations and NDK usage facts in bounded batches;
3. mark the generation complete only after the scan completes;
4. retain only the completed generation;
5. leave the previous completed generation authoritative if the new scan is cancelled or fails.

The Tool Scan continues to write actionable tool recommendations to its separate tool index. Before collecting Android recommendations, the `tools` command reads the latest complete usage snapshot from the main Deep Scan index and passes it as immutable input to the Android analyzer. Absence, corruption, or incompatible schema in the Deep Scan index degrades to `usage.state = unknown`; it does not fail independent Docker, Homebrew, Simulator, or Android inventory.

Tests and non-default CLI callers receive an explicit project-index path seam so they do not read a developer's real cache accidentally.

## Tool-managed Interface

The Tool-managed page owns its scan controls in the content area; the tools refresh item is removed from the window toolbar.

Idle states:

- before the first result: `Start Tool Scan`;
- after a completed result: `Scan Again`.

Running state:

- `Cancel Scan` remains enabled;
- a progress indicator and `Checking tool-managed storage…` are visible;
- cleanup `Run` buttons and other scan starts remain disabled through the existing global activity state.

Starting a Tool Scan clears the previous tool report, matching the current behavior and Deep Scan's fresh-run presentation. Cancellation stops the inventory task, terminates its contained process tree through the backend's existing cancellation-aware runner, returns activity to idle, and shows `Scan cancelled.` without presenting cancellation as an engine failure. A cancelled inventory never replaces the last complete tool recommendation generation in the tool index.

Tool cleanup remains non-cancellable after handoff. Its per-item `Run` and confirmation flow do not change.

## NDK Recommendation UI

An Android NDK row shows one usage summary beneath its reason:

- `Used by N projects` for exact matches;
- `Not referenced by scanned projects` when a complete snapshot has no exact match;
- `Usage unknown — run Deep Scan` when no complete snapshot exists.

When exact or unpinned project paths exist, a DisclosureGroup reveals two labelled lists:

- `Matching version` for exact matches;
- `Uses an unpinned NDK` for unresolved projects.

Paths use middle truncation, remain text-selectable, and are sorted deterministically. The initial implementation does not add Finder actions. The warning already attached to NDK removal remains visible, including the existing qualification that projects outside the scan may require the component.

## Error Handling

- A malformed or unreadable project metadata file is skipped without producing an exact match.
- Conflicting versions in one repository become an ambiguous/unpinned usage record.
- A missing or unreadable Deep Scan index yields `unknown` usage while Tool Scan continues.
- Failure of one external tool remains isolated by the existing tool registry.
- Tool Scan cancellation is a normal terminal state, not an alert.
- Tool Scan timeout, malformed JSON, or process-containment failure remains an error and preserves the previous complete tool index generation.

## Testing

### Python

1. Detect literal Groovy and Kotlin `ndkVersion` declarations.
2. Detect `android.ndkVersion` and a conventional `ndk.dir` version.
3. Classify native markers without a version as unpinned.
4. Collapse multiple declarations in one repository to one usage record.
5. Classify conflicting versions as ambiguous/unpinned.
6. Ignore malformed values, symlinks, generated dependency trees, and unsupported dynamic Gradle expressions.
7. Preserve the prior usage snapshot after cancellation or failure.
8. Match installed `ndk;<version>` resources to exact projects and sort paths.
9. Return unknown usage when the project index is absent or unreadable without breaking other tool analyzers.
10. Round-trip the new usage payload through recommendation persistence and JSON output.

### Swift

1. Decode matched, unreferenced, unknown, and absent usage payloads.
2. Verify the Tool Scan control title for initial, running, cancelled, and completed states.
3. Verify cancellation invokes backend task cancellation, restores idle activity, and does not show an error.
4. Verify a completed scan replaces the report and a cancelled scan does not expose a partial report.
5. Verify NDK usage summary formatting and deterministic project grouping through pure helpers/model tests.

### Integration and manual verification

1. Run the complete Python test suite.
2. Run the complete Swift test suite and native application build.
3. Run Deep Scan against fixtures containing pinned and unpinned Android projects, then verify Tool Scan reports the expected installed NDK usage.
4. Start and cancel a real Tool Scan in the app; verify the subprocess tree exits and the UI returns to idle.
5. Expand a real NDK row and verify project paths render, truncate, and remain selectable.
6. Confirm the toolbar no longer contains the tools refresh icon.

## Acceptance Criteria

1. Every installed NDK recommendation displays one of the three defined usage states.
2. Exact usage counts equal the number of unique matching repository roots in the latest complete Deep Scan.
3. Expanding the row exposes the exact matching paths and separately labels unpinned projects.
4. Cancelling or failing Deep Scan cannot replace the last complete usage snapshot.
5. Tool Scan is started, cancelled, and repeated through text buttons inside the Tool-managed page.
6. Cancelling Tool Scan leaves no contained inventory subprocess running and produces no engine-error alert.
7. Existing tool cleanup validation, default-selection policy, and external-tool isolation remain unchanged.
