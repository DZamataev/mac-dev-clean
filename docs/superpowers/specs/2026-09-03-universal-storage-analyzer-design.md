# Universal Storage Analyzer Design

**Date:** 2026-09-03  
**Last revised:** 2026-09-07  
**Status:** Approved  
**Project:** mac-dev-clean

## Summary

Evolve mac-dev-clean from a list of fixed developer-cache locations into a two-phase universal storage analyzer with a developer-focused cleanup policy.

The analyzer will combine a fast scan of known locations with an explicit deep scan of the user's home directory. It will discover Git repositories without relying on a conventional developer-root name, identify reproducible project artifacts, analyze Downloads, inventory applications and external tools, and explain why each cleanup action is safe or requires review.

The safety boundary remains explicit: known caches and validated reproducible project artifacts may be deleted directly after confirmation; personal files and applications go to the macOS Trash; tool-managed state is changed only through the owning tool's supported interface; unknown data is never promoted to direct deletion.

## Motivation and Observed Opportunities

A metadata-only investigation of the development machine found several large categories not covered by the existing fixed-location scanner:

- 39.38 GiB in project-local `ios/build` directories;
- 29.33 GiB in `android/app/build` directories;
- 6.26 GiB in Android `.cxx` outputs;
- 48.46 GiB in project-root `node_modules` directories;
- 8.25 GiB in CocoaPods `Pods` directories;
- 26.16 GiB in known but unsupported caches such as Yarn, uv, JetBrains, LLDB, Codex, and Playwright;
- 11.7 GiB in Downloads installers and archives older than six months;
- 5.16 GiB in unavailable shutdown Apple simulator devices;
- 15.6 GB reported reclaimable by Docker;
- 16.68 GiB in Android virtual devices and 29.85 GiB in Android SDK components.

A second metadata-only survey on 2026-09-07, prompted by a comparative review of
`thanhdevapp/mac-dev-cleaner-cli`, measured the categories the current scanner
still cannot explain. All figures are `du` output on the development machine:

| Category | Measured | Lane |
|---|---|---|
| Docker unused images | 15.03 GB (`docker system df`) | tool-managed |
| Docker build cache | 7.50 GB (`docker system df`) | tool-managed |
| Android SDK NDK | 17 GiB | tool-managed inventory |
| Android AVDs | 17 GiB | tool-managed inventory |
| Android system images | 8.4 GiB | tool-managed inventory |
| Yarn cache (`~/Library/Caches/Yarn`) | 6.0 GiB | curated cache |
| uv cache (`~/.cache/uv`) | 5.6 GiB | curated cache |
| JetBrains caches | 4.3 GiB | curated cache |
| NuGet packages (`~/.nuget/packages`) | 1.2 GiB | curated cache |
| Playwright browsers | 1.0 GiB | curated cache |
| CocoaPods cache + repos | 960 MiB | curated cache |
| Electron / electron-builder caches | 847 MiB | curated cache |
| Homebrew removable versions | 799.8 MB (`brew cleanup -n`) | tool-managed |
| Puppeteer cache | 495 MiB | curated cache |
| Maven `~/.m2/repository` | 190 MiB | curated cache |
| Ollama models (`~/.ollama`) | 5.8 GiB | review only |
| Hugging Face cache | 888 MiB | review only |

Two findings from that survey shape the design rather than merely adding rows:

1. **A directory listing is not an inventory.** `~/Library/Caches/Homebrew`
   measures 64 MiB, while `brew cleanup -n` reports 799.8 MB removable — a
   twelvefold difference, because most of Homebrew's removable storage is old
   installed versions under the Cellar, which must never be deleted by path.
   The same holds for Docker: the VM disk image measures 37.4 GB but only
   22.5 GB is reclaimable, and only through Docker's own prune verbs.
2. **Tool-managed inventory is nearly free.** `docker system df --format
   '{{json .}}'`, `brew cleanup -n`, `sdkmanager --list_installed`, and
   `avdmanager list avd` each answer in one command with no filesystem walk,
   so they belong in the earliest delivery phases rather than the last.

Local model stores (`~/.ollama`, Hugging Face) are large and look cache-shaped
but are not regenerable without a deliberate multi-gigabyte redownload, and the
user may have no network budget for it. They are review-only, never cleanable.

The current scanner has two constraints that prevent it from explaining this storage well:

1. fixed locations and a small number of glob patterns dominate discovery;
2. `ScanTarget.cleanable` is a boolean, which cannot express confidence, restoration cost, Trash semantics, external-tool actions, or item-level default selection.

Adding `~/dev` as another conventional root would help this one machine but would not generalize. The deep scanner will instead find repositories recursively within explicitly scanned roots.

## Goals

1. Provide useful results quickly through a non-recursive Fast Scan.
2. Provide broad, explainable discovery through an explicit Deep Scan.
3. Discover Git repositories and worktrees regardless of their parent directory name.
4. Identify project artifacts without touching project source files.
5. Distinguish direct deletion, Trash, external-tool actions, and review-only results.
6. Use evidence and confidence rather than treating every large path as removable.
7. Make scanning incremental, cancellable, and resumable.
8. Preserve existing CLI scan and cleanup behavior while the new system is introduced.
9. Keep all analysis local and avoid reading file contents except where required for exact duplicate hashing or structured project metadata.

## Non-goals

1. Automatically deleting arbitrary old files outside supported policies.
2. Directly deleting mixed application state from `Application Support`, `Group Containers`, Docker VM storage, Android SDK internals, or simulator internals.
3. Inferring inactivity from running processes, open IDEs, shell history, or background services.
4. Downloading cloud placeholders to measure or hash them.
5. Removing application support files when an application bundle is trashed.
6. Treating a missing Spotlight last-used date as evidence that an application is unused.
7. Providing transaction rollback for completed filesystem or external-tool actions.
8. Scanning the entire macOS Data volume without a supported cleanup policy for the scanned system location.
9. Starting, waking, or installing an external tool in order to inventory it.
10. Deleting locally downloaded model weights, which cannot be regenerated without a large redownload the user may be unable to afford.
11. Making a path actionable because the user browsed to it in the Tree Explorer.

## Design Principles

- **Evidence before action:** every recommendation states why it exists.
- **Regeneratable is not the same as free:** display rebuild, redownload, or reinstall cost.
- **Unknown lowers confidence:** missing metadata never strengthens a deletion recommendation.
- **No arbitrary path execution:** the UI applies indexed recommendation IDs, not user-supplied deletion paths.
- **Owning tools manage their state:** use `simctl`, Docker, Homebrew, Android tools, and Git rather than deleting their internals.
- **Personal data gets recovery:** Downloads and applications move to Trash.
- **Direct deletion stays narrow:** only detector-validated caches and project artifacts qualify.
- **Partial truth is reported:** skipped roots and partial cleanup are visible and do not invalidate independent results.
- **Every action is recorded:** each attempted action, including dry runs and refusals, is appended to a local audit journal before the result is reported.

## Rejected Approaches

A review of `thanhdevapp/mac-dev-cleaner-cli` (Go CLI plus Wails GUI, MIT) in
September 2026 confirmed several category ideas adopted above. Its safety
mechanics are recorded here as explicitly rejected, because they are the
tempting shortcuts a future implementer is most likely to reach for.

1. **Prefix-based path authority.** That project authorizes deletion when the
   path string starts with `$HOME` and contains no blacklisted substring. We
   require a safety root plus an exact registered path shape plus symlink
   refusal, evaluated on resolved path segments. A blacklist cannot enumerate
   what matters; an allowlist of validated shapes can.
2. **Existence as sufficient evidence.** That project deletes `node_modules`,
   `venv`, `target`, and `build` because the directory exists. We require the
   project's inactivity window, the artifact's own recency check, and, for
   dependency trees, a lock file that proves exact restoration.
3. **Substring matching for protection.** Matching `".env"` as a directory name
   also matches an `.env` secrets file, and matching `"Keychain"` protects only
   paths that happen to spell it. Protection is a property of classification,
   not of the characters in a path.
4. **Unrestricted tool verbs.** `docker image prune -a -f` removes every image
   not attached to a running container, which is far broader than the
   `Reclaimable` figure shown to the user. Any external-tool action must
   execute the exact argument vector whose effect the preview described.
5. **Logical size as reclaim estimate.** Summing `stat` sizes over APFS clones
   (simulator device sets, DerivedData, Time Machine local snapshots)
   systematically overstates what deletion frees. Allocated size is the
   default measurement, and estimate confidence is displayed.
6. **Fixed conventional roots.** Walking `~/Documents ~/Projects ~/Code …` to a
   depth of three misses projects and cannot generalize. Recursive `.git`
   discovery within explicitly scanned roots is already specified above and
   remains the only project-discovery mechanism.

## Architecture

The subsystem consists of six independently testable components.

### 1. Discovery

Discovery emits filesystem and tool-owned entities without deciding whether to clean them.

Fast Scan discovers:

- existing fixed cache locations;
- known application roots;
- supported system developer locations;
- lightweight inventory from available external tools that can respond without starting a service.

Deep Scan discovers:

- Git repositories by `.git` directory or `.git` file at any depth under an allowed root;
- registered Git worktrees and submodules without double-counting them;
- candidate project artifact paths;
- Downloads files and directories;
- large unclassified directories for the review lane.

Traversal boundaries:

- never descend through `.git` internals;
- do not recurse through already-classified dependencies, build outputs, caches, or logs;
- do not follow symlinks;
- do not hydrate cloud placeholders;
- skip system locations unless a supported analyzer and action policy exists for them.

There is no `developer root` concept. A scan root is only a filesystem boundary, not a statement about directory purpose.

### 2. Local Index

Use SQLite at `~/Library/Caches/mac-dev-clean/index.sqlite3`. Losing or deleting the index is harmless; the next Deep Scan rebuilds it.

The index stores:

- normalized path;
- filesystem device and inode;
- entry kind;
- allocated and logical size;
- modification and creation timestamps where available;
- directory summary fingerprints;
- detector facts and recommendation status;
- content hash only for files considered during duplicate detection;
- volume identity, FSEvents stream identity, and the last complete event ID when available;
- scan generation and completion state.

The index does not store user file contents. Hashes are local and are not sent anywhere.

Incremental invalidation uses FSEvents when a continuous event history is available. Changed paths and their indexed ancestors are marked dirty. If the event stream is unavailable, its history has been dropped, the volume identity changed, or the prior scan was incomplete, the analyzer falls back to a full metadata walk. Even during a fallback walk, unchanged file hashes may be reused only when device, inode, size, modification time, and creation time still match. Directory mtime alone is never treated as proof that descendants are unchanged.

Writes use bounded atomic transactions after each completed directory batch. A cancelled or crashed scan retains the last committed generation and marks the new generation incomplete. Incomplete results may accelerate resumption but are not eligible for cleanup until revalidated.

A Reset Index action closes the database, deletes it, and returns the analyzer to an unindexed state.

### 3. Analyzers

Each analyzer accepts discovered entities and emits facts. It does not delete anything.

Initial analyzers:

- `ProjectAnalyzer`
- `DownloadsAnalyzer`
- `ApplicationAnalyzer`
- `CacheAnalyzer`
- `SimulatorAnalyzer`
- `DockerAnalyzer`
- `HomebrewAnalyzer`
- `AndroidAnalyzer`
- `TreeExplorer`

Analyzers are registered explicitly. There is no general rule that turns an arbitrary directory named `build`, `cache`, `Pods`, or `vendor` into a cleanable item.

Analyzers fall into two acquisition styles:

- **Filesystem analyzers** (`ProjectAnalyzer`, `DownloadsAnalyzer`, `ApplicationAnalyzer`, `CacheAnalyzer`, `TreeExplorer`) measure paths directly.
- **Tool-managed analyzers** (`SimulatorAnalyzer`, `DockerAnalyzer`, `HomebrewAnalyzer`, `AndroidAnalyzer`) ask the owning tool for its inventory and never measure or delete its internal paths.

A tool-managed analyzer must satisfy the same contract:

1. Detect the tool without starting or waking it. A missing binary, a stopped daemon, or a non-zero exit produces an `unavailable` fact carrying the reason, never a scan failure and never a prompt to start the service.
2. Parse a machine-readable inventory form where the tool offers one, and pin the exact invocation in the analyzer.
3. Record the tool's own reclaim figure with its unit as reported, plus the exact preview command that produced it, as evidence.
4. Emit at most one recommendation per distinct tool resource class, so a user can prune Docker's build cache without touching its images.
5. Produce an `invoke_tool` action whose argument vector is fixed by the analyzer and matches what the preview described. Actions are never selected by default.

### 4. Policy Engine

The policy engine converts analyzer facts into recommendations. It owns:

- action kind;
- confidence;
- explanation and evidence;
- restoration cost;
- default selection;
- risk and warning text;
- estimated reclaimable bytes.

The policy engine is deterministic and testable without touching the filesystem.

### 5. Action Executor

The executor resolves recommendation IDs from the current index, revalidates the detector evidence, and performs the approved action.

Supported action kinds:

- `delete_contents`
- `delete_tree`
- `move_to_trash`
- `invoke_tool`
- `reveal_only`

The executor never accepts an arbitrary filesystem path as sufficient authority to delete it.

### 6. Audit Journal

Every action attempt is appended to a newline-delimited JSON journal at
`~/Library/Logs/mac-dev-clean/actions.jsonl` before its outcome is returned to
the caller. The journal is append-only, local, and never transmitted.

One record per attempted item, containing:

- ISO-8601 UTC timestamp;
- recommendation ID, detector ID, and category;
- normalized path, or the tool resource identifier for `invoke_tool`;
- action kind;
- the exact argument vector for `invoke_tool` actions;
- estimated reclaimable bytes;
- dry-run flag;
- terminal outcome (`removed`, `trashed`, `invoked`, `skipped`, `changed_since_scan`, `failed`);
- the refusal or error reason when the outcome is not a success.

Requirements:

- A refused or failed action is journalled with the same weight as a successful one; the journal must answer "why did nothing happen?" as well as "what was deleted?".
- Dry runs are journalled and unambiguously marked, so a preview is never mistaken for a deletion during a later investigation.
- Journalling happens before the outcome is reported, so a crash between the action and the report cannot hide a completed deletion.
- A journal write failure is reported as a warning and never blocks or reverses an action that already happened.
- The journal is rotated by size, retaining a bounded number of previous files. Rotation never blocks an action.
- Because paths are personal data, the journal is covered by the privacy policy and is excluded from any diagnostic bundle the user has not explicitly chosen to share.

Every action path — the legacy category `clean`, item-level `apply`, Trash
operations, and external-tool invocations — writes to this one journal through a
single shared writer. A new action kind is not complete until it journals.

## Recommendation Model

The new internal recommendation model contains at least:

- stable recommendation ID;
- category and specific detector ID;
- display label and normalized path;
- item kind;
- action kind;
- allocated size;
- logical size when useful;
- estimated reclaimable size and estimate confidence;
- last meaningful activity timestamp;
- confidence: `exact`, `strong`, `heuristic`, or `unknown`;
- default-selection state;
- evidence records;
- restoration cost: `rebuild`, `redownload`, `reinstall`, `external_state`, or `not_applicable`;
- safety root;
- scan generation and source analyzer.

Legacy `cleanable` and `delete_mode` fields remain derivable during migration so the existing CLI and native UI can continue decoding Fast Scan reports.

## Project Analyzer

### Repository discovery

Deep Scan recursively detects `.git` directories and `.git` files. It uses Git metadata to distinguish:

- primary repositories;
- linked worktrees;
- submodules;
- broken or prunable worktree registrations.

A valid active worktree is never presented as a filesystem deletion target. Worktree removal is tool-managed through Git and is not part of the first implementation milestone.

### Meaningful activity

A project is considered inactive when no meaningful file has changed for 90 days.

Meaningful files include source, project configuration, manifests, and lock files. Activity calculation excludes only explicitly registered generated paths for the detected project type:

- `.git`;
- dependency trees;
- known build outputs;
- known cache and log paths;
- detector-validated temporary output paths;
- editor-generated state such as `.DS_Store`.

A directory is not excluded merely because its generic name is `tmp`, `cache`, `build`, or `vendor`; its path shape must match a registered recipe.

The analyzer does not inspect active processes. Before direct cleanup, it repeats the meaningful-activity calculation and displays a warning to close IDEs and build tools.

### Initial project artifact recipes

The first implementation milestone supports these exact recipes:

- project-root `node_modules` with `package.json` and one of `package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`, `pnpm-lock.yaml`, `bun.lock`, or `bun.lockb`;
- `ios/Pods` with `ios/Podfile` and `ios/Podfile.lock`;
- `ios/build` in a project with an Xcode project or workspace under `ios`;
- `android/app/build` in a project with Android Gradle settings and an application module;
- `android/app/.cxx` under the same validated Android application structure;
- project-local Android `build` and `.gradle` outputs only through dedicated path-shape validators;
- SwiftPM `.build` with `Package.swift` and `Package.resolved`;
- Rust `target` with `Cargo.toml` and `Cargo.lock`;
- Ruby `vendor/bundle` with `Gemfile` and `Gemfile.lock`;
- Python `.venv` or `venv` only when a supported project manifest and deterministic lock file are present, such as `uv.lock`, `poetry.lock`, or `Pipfile.lock`.

A generic `requirements.txt` alone does not qualify a Python environment for default selection because it need not describe an exact reproducible environment.

Recipes added later require their own validator, policy, and safety tests.

### Manifest-derived recipes

Every recipe above is keyed on a path shape plus the existence of a manifest
file. Some ecosystems cannot be identified that way, because the deciding
evidence lives *inside* a manifest rather than in the directory layout: a
React Native project and a plain Node project have identical trees, and a
Flutter package and a pure Dart package both carry `pubspec.yaml`.

A recipe may therefore declare a **manifest predicate**: a bounded, read-only
parse of one declared manifest file whose result gates the recipe. Rules:

- Only the manifest file named by the recipe is read, and only up to a declared byte ceiling; the parse never recurses and never reads project source.
- A parse failure, a size overrun, or an absent key yields "predicate not satisfied", which disqualifies the recipe. A malformed manifest must never widen what is offered.
- The predicate result is recorded as evidence in its own right, so the user reads "package.json declares react-native" instead of "path matched".
- A predicate can only *restrict* a recipe. It never substitutes for the path shape, the manifest's existence, the inactivity window, or the lock-file requirement.

The initial manifest-derived recipes:

- **React Native** — `package.json` declaring `react-native` in `dependencies` or `devDependencies`, enabling `ios/build`, `android/build`, `android/app/build`, `android/app/.cxx`, and `android/.gradle` as build outputs. The lock-file rule for `node_modules` is unchanged; these are build outputs and require inactivity only.
- **Flutter** — `pubspec.yaml` declaring a `flutter` dependency or an `sdk: flutter` constraint, enabling `build`, `.dart_tool`, and the platform build outputs `ios/build`, `android/build`, `macos/build`, `linux/build`, `windows/build`, and `web/build`. Dart-only packages that lack the Flutter constraint get `.dart_tool` and `build` only when `pubspec.lock` is present.
- **Maven** — `pom.xml` with a readable `<artifactId>`, enabling `target` as a build output. A `target` directory beside a `pom.xml` that fails to parse is not offered.
- **Gradle module** — a `build.gradle` or `build.gradle.kts` in a directory that also has a sibling `settings.gradle`/`settings.gradle.kts` at or above it inside the same repository, enabling that module's `build` and `.gradle` outputs. This generalizes the existing `android/app/build` recipe to multi-module JVM projects without ever treating a bare directory named `build` as an artifact.

`Metro` and `Haste` bundler caches under `$TMPDIR` are **not** project recipes.
They are unowned temporary state, are covered by the Cache Analyzer's
`$TMPDIR` rules below, and are never attributed to a specific project.

### Project default selection

Validated build outputs are selected by default only for projects inactive for 90 days. Dependency trees are selected by default only when the project is inactive for 90 days and the corresponding supported lock file exists.

Project source, configuration, manifests, lock files, Git metadata, and valid worktree roots are never selected.

## Downloads Analyzer

Deep Scan explicitly requests macOS access to Downloads. Access denial produces a skipped-root result and does not fail the rest of the scan.

### Duplicate detection

1. Group regular local files by logical size.
2. Do not read files in singleton size groups.
3. Hash possible duplicates with SHA-256.
4. Compare filesystem metadata before and after hashing.
5. Discard a hash result if the file changed during reading.
6. Group only exact full-file hash matches.

For each exact duplicate group, choose one keeper:

1. prefer a name without generated suffixes such as `(1)`, `(2)`, or `copy`;
2. then prefer the oldest creation timestamp;
3. then use a stable normalized-path ordering.

The user may choose a different keeper before cleanup. Duplicate copies other than the keeper are selected by default and moved to Trash.

### Age policies

Selected by default:

- zero-byte regular files;
- partial downloads with suffixes such as `.part`, `.crdownload`, or `.download` older than 7 days;
- exact duplicate copies other than the keeper;
- installer and archive files older than 90 days.

Initial installer suffixes:

- `.dmg`
- `.pkg`
- `.exe`
- `.apk`
- `.ipa`

Initial archive suffixes:

- `.zip`
- `.tar`
- `.tar.gz`
- `.tgz`
- `.gz`
- `.bz2`
- `.xz`
- `.7z`
- `.rar`

All other Downloads items older than 180 days are shown in a separate group but are not selected by default.

Policy precedence prevents duplicate rows. For example, a 200-day-old duplicate installer appears once with all matching evidence and the strongest applicable recommendation.

All Downloads cleanup uses `move_to_trash`.

## Application Analyzer

Analyze application bundles in `/Applications` and `~/Applications`. Mounted external volumes are excluded from the default application inventory.

For each bundle:

- measure allocated size;
- read bundle ID and version from `Contents/Info.plist`;
- query Spotlight for `kMDItemLastUsedDate`;
- retain `unknown` if Spotlight has no date;
- group duplicate installations by bundle ID;
- sort by last use and size.

User-facing groups:

- duplicate applications or multiple versions with the same bundle ID;
- applications not used for more than one year;
- applications with unknown last-used date.

Applications are never selected by default. The only filesystem action is `move_to_trash`; associated `Application Support`, preferences, containers, and documents remain untouched. If moving an application requires permission or fails, the analyzer reports the failure and offers no permanent-delete fallback.

## Cache Analyzer

Continue using exact fixed paths, contents-only deletion, safety roots, and symlink refusal.

The expanded curated set covers these verified paths. Each was confirmed to
exist and was measured on the development machine; each needs its own
documentation entry and test before it ships.

| Cache | Path | Restoration |
|---|---|---|
| Yarn classic | `~/Library/Caches/Yarn` | redownload |
| Yarn Berry | `~/.yarn/berry/cache` | redownload |
| uv | `~/.cache/uv` | redownload |
| pdm | `~/.cache/pdm` | redownload |
| pipenv virtualenvs | `~/.local/share/virtualenvs` | redownload |
| JetBrains caches | `~/Library/Caches/JetBrains` | rebuild (reindexing on next IDE launch) |
| NuGet packages | `~/.nuget/packages` | redownload |
| Playwright browsers | `~/Library/Caches/ms-playwright` | redownload |
| Puppeteer browsers | `~/.cache/puppeteer` | redownload |
| Electron | `~/Library/Caches/electron` | redownload |
| electron-builder | `~/Library/Caches/electron-builder` | redownload |
| CocoaPods cache | `~/Library/Caches/CocoaPods` | redownload |
| CocoaPods spec repos | `~/.cocoapods/repos` | redownload |
| Maven repository | `~/.m2/repository` | redownload |
| Dart pub | `~/.pub-cache` | redownload |
| Flutter/Dart tool caches | `~/Library/Caches/Flutter`, `~/Library/Caches/dart` | rebuild |
| Deno | `~/Library/Caches/deno`, `~/.deno` | redownload |
| RubyGems | `~/.gem` | redownload |
| .NET | `~/.dotnet` | redownload |
| LLDB module cache | `~/Library/Caches/lldb` | rebuild |
| Android build cache | `~/.android/cache`, `~/.android/build-cache` | rebuild |

Notes that change behavior rather than merely listing paths:

- **JetBrains** owns two roots with different meanings. `~/Library/Caches/JetBrains` is regenerable index and cache data and is cleanable; `~/Library/Application Support/JetBrains` holds settings, plugins, and licences and is **protected**. A rule that matched "JetBrains" anywhere would take both.
- **`~/.m2/repository` and `~/.nuget/packages`** are also offline build inputs. They are cleanable but never selected by default: a machine with no network cannot rebuild from them.
- **`~/.gradle/caches`** is already covered by the existing `gradle-cache` category and is not duplicated here.
- **Homebrew** appears here only as `~/Library/Caches/Homebrew`, the download cache. Removable installed versions belong to the Homebrew tool-managed analyzer, which finds an order of magnitude more.

Each added location must be independently documented and tested. A wildcard over all of `~/Library/Caches` is forbidden.

### Bundler temporary caches

React Native's Metro and Jest's Haste map write to `$TMPDIR` with generated
suffixes: `metro-*`, `haste-map-*`, `react-native-packager-cache-*`. These are
cleanable under narrow rules:

- match only inside the value of `$TMPDIR` resolved for the current user, never a hard-coded `/tmp` or `/private/tmp`;
- match only the declared prefixes, and only directories;
- require the entry to be owned by the current user;
- require the entry to be older than 7 days, because a running bundler holds a live cache;
- `delete_tree`, and never `reveal_only` promotion into a broader `$TMPDIR` sweep.

The remainder of `$TMPDIR` and all of `/private/tmp` stay review-only. They can
hold active build and test workspaces belonging to running processes.

### Review-only large stores

Some large directories look cache-shaped but are not regenerable at acceptable
cost. They are reported with size and a Finder link, are never cleanable, and
are never selected:

- local model stores such as `~/.ollama` and `~/.cache/huggingface`, where "regeneration" means redownloading gigabytes and may be impossible offline;
- `~/.codex/sessions` and other assistant task history already reported today;
- application-managed state under `Application Support`, `Containers`, and `Group Containers`.

## Tool-managed Analyzers

### Simulator

Reuse the existing `xcode-sim-prune` inventory and `simctl` operations. Surface unavailable shutdown devices, per-device size, runtime size, and last-used date in the main report. Booted devices are never selected. Runtime and device actions require separate confirmation.

### Docker

Inventory comes from `docker system df --format '{{json .}}'`, which emits one
JSON object per line with `Type`, `TotalCount`, `Active`, `Size`, and
`Reclaimable`. The `Reclaimable` value carries a unit suffix and sometimes a
percentage in parentheses; both are parsed, and the tool's own string is kept
verbatim as evidence.

Do not start Docker. `docker system df` failing or the daemon being stopped
produces an `unavailable` fact with the reason.

Four resource classes stay distinct, each with its own recommendation and its
own confirmation:

| Class | Action | Argument vector |
|---|---|---|
| Build cache | `invoke_tool` | `docker builder prune -f` |
| Dangling images | `invoke_tool` | `docker image prune -f` |
| Stopped containers | `invoke_tool` | `docker container prune -f` |
| Unused volumes | `invoke_tool` | `docker volume prune -f` |

`docker image prune -a` is deliberately excluded. It removes every image not
attached to a running container, including images the user pulled deliberately
and images referenced by stopped containers, which is far broader than the
"unused" figure `system df` reports. If a later release offers it, it must be a
separate recommendation with its own preview, its own explanation, and its own
confirmation.

The Docker VM disk image at `~/Library/Containers/com.docker.docker/Data/vms`
remains report-only. Docker does not necessarily shrink it after a prune, so
the reclaim estimate is labelled as Docker's figure, not as free space that
will appear on the volume.

### Homebrew

Preview with `brew cleanup -n`. Its output includes `Would remove:` lines with a
path, a file count, and a size, plus a trailing total line. The analyzer parses
the total for the estimate and keeps the raw preview available for display.
Lines beginning `Warning:` are informational and must not be parsed as removals.

The measured gap matters: the download cache directory holds 64 MiB while
`brew cleanup -n` reports 799.8 MB removable, because most of it is old
installed versions in the Cellar. Those are removed only by
`brew cleanup` — never by deleting Cellar paths directly.

The action is `invoke_tool` with the exact vector `brew cleanup`. Homebrew's
`--prune` variants and any argument that also removes current versions are out
of scope. `brew` being absent produces an `unavailable` fact.

### Android

Inventory uses the Android command-line tools, located through `ANDROID_HOME`,
`ANDROID_SDK_ROOT`, or the default `~/Library/Android/sdk`, checking
`cmdline-tools/latest/bin` before any pinned version directory:

- `sdkmanager --list_installed` lists installed packages with path, version, description, and location. Its progress output is written before the table and must be discarded.
- `avdmanager list avd` lists virtual devices with name, path, target, and ABI. It also reports devices that failed to load, under a separate "could not be loaded" heading.

The analyzer groups results into: build-tools versions, platforms, NDK
versions, CMake versions, system images, emulator, command-line tools versions,
and AVDs. Directory sizes come from the reported `Location` under the SDK root.

Nothing here is selected by default, and nothing is deleted by path. Removal is
`invoke_tool` with `sdkmanager --uninstall <package-path>` or
`avdmanager delete avd -n <name>`. Rationale: a toolchain version may be
required by a project this scan never indexed, and an AVD contains device state
the user cannot recreate.

AVDs that `avdmanager` reports as failing to load are surfaced in their own
group with the tool's error text. They are still not selected by default: a
device definition can fail to resolve because an SDK component is missing
rather than because the AVD is garbage.

Comparing installed components against versions referenced by Gradle files in
indexed projects is a later refinement. Until it exists, the analyzer reports
inventory and never claims a component is unused.

## Tree Explorer

Every analyzer above answers "what can I clean?". None answers "where did the
rest of my disk go?" — on the development machine the detectors explain a
minority of 759 GiB in use. The Tree Explorer is a read-only directory
drilldown that closes that gap without inventing new deletion authority.

Behavior:

- Start from a chosen root, defaulting to the home directory, and list immediate children with allocated size, entry count, and kind.
- Compute one level at a time on demand. A child's subtree is measured only when the user opens it, so the first screen appears without a full walk.
- Cache measured nodes for the session and invalidate a node when the user explicitly refreshes it or when an action changes a path beneath it.
- Never follow symlinks, never hydrate cloud placeholders, and never descend into another volume without an explicit choice.
- Report an unreadable directory in place, with the reason, rather than silently reporting zero.
- Present sizes as allocated bytes, matching the rest of the report, and label the unit.

Safety:

- Every node is `reveal_only`. The Tree Explorer creates no cleanable items and no default selections.
- Where a node coincides with an existing recommendation, show that recommendation's action rather than offering a new one. Deletion authority always comes from a detector, never from the fact that the user navigated somewhere.
- A path the user reaches by browsing is never accepted as an action target; the executor still resolves recommendation IDs only.

## Scan Phases

### Fast Scan

Fast Scan runs at application launch and does not recursively enter protected user folders.

It:

- evaluates fixed locations;
- enumerates application bundles and available metadata;
- queries lightweight supported external-tool inventory;
- streams useful findings as soon as they are available;
- remains independently cleanable if Deep Scan never runs.

An unavailable tool or stopped daemon produces an unavailable fact, not a scan failure and not a request to start the service.

### Deep Scan

Deep Scan is an explicit user action.

It:

1. requests access to Desktop, Documents, and Downloads;
2. traverses the home directory using the defined boundaries;
3. discovers repositories and worktrees;
4. analyzes project activity and artifact recipes;
5. analyzes Downloads metadata;
6. hashes only same-size duplicate candidates;
7. records large unclassified paths for review;
8. commits index batches throughout the scan.

The UI displays phase, current path, discovered reclaimable size, warnings, and progress where a denominator is available. Cancel stops after the current directory operation or file hash and retains committed index progress.

## Streaming Backend Protocol

The current one-shot JSON backend remains for compatibility. Deep Scan adds newline-delimited JSON events:

- `scan_started`
- `permission_required`
- `root_started`
- `progress`
- `candidate_found`
- `root_finished`
- `warning`
- `scan_completed`
- `scan_cancelled`

Each line is a complete JSON object with a protocol version, scan generation, event type, and event-specific payload.

The Swift backend reads stdout incrementally instead of waiting for process exit. Stderr remains diagnostic and is bounded before presentation. Cancelling the Swift task terminates the Python child process and records the generation as incomplete.

Existing `scan --json`, `report --json`, and category-based `clean` commands retain their current contracts during migration. New item-level commands use recommendation IDs and support dry-run. They never accept a path without resolving it through current indexed evidence.

## UI Organization

Order result sections by expected benefit:

1. Recommended Cleanup
2. Old Projects
3. Downloads
4. Unused and Duplicate Applications
5. Tool-managed Storage
6. Needs Review
7. Explore Disk

Within a group, sort by estimated reclaimable size. Applications additionally support last-used sorting and explicit one-year/unknown groups.

Tool-managed Storage groups by owning tool, and within a tool by resource class,
so Docker's build cache and its images are separately selectable. Each row
carries the tool's own reported figure and the preview command that produced
it. An unavailable tool is shown as a row stating why, not hidden — a missing
Docker daemon must not read as "Docker uses no space".

Explore Disk hosts the Tree Explorer. It is a navigation surface, not a result
list: nothing in it is selectable and it contributes nothing to the totals.

Every row shows:

- size and estimate confidence;
- relevant age;
- evidence-based reason;
- action kind;
- restoration cost;
- selection state;
- Reveal in Finder where applicable.

The summary separately totals:

- direct deletion;
- data moved to Trash;
- potential external-tool reclamation.

## Execution and Revalidation

Before any action, resolve the recommendation ID and repeat all safety-critical checks.

For project artifacts:

- path exists and is not a symlink;
- path remains under the indexed project root;
- exact detector path shape still matches;
- the recipe's required manifest and, for dependency recipes, its required lock file still exist;
- no meaningful project file changed inside the 90-day boundary or since the scan;
- safety root and filesystem identity still match expectations.

For duplicate files:

- path exists and is a regular local file;
- size and metadata match the indexed value;
- duplicate hash remains valid or is recomputed;
- the keeper is excluded.

For personal files and applications, call `/usr/bin/trash`. If Trash fails, record failure and leave the item in place. Never fall back to permanent deletion.

For external tools, present preview output when supported, require explicit confirmation, and execute only the exact supported argument vector produced by the analyzer.

External-tool execution has additional requirements, because the executor cannot
revalidate a tool's internal state the way it revalidates a path:

- Re-run the preview immediately before acting and show the user the current figure. A `Reclaimable` value measured minutes ago may be stale.
- Run the argument vector with no shell interpretation: an argument list, never a command string.
- Treat the tool's exit status as authoritative. A non-zero exit is `failed`, and its stderr, bounded in size, becomes the error reason.
- Capture the tool's reported result where it produces one, and present observed reclamation separately from the estimate.
- Never retry automatically. A tool action that partially succeeded must be re-previewed before another attempt.
- The terminal outcome for a successful external-tool action is `invoked`, not `removed`: the tool reports what it freed, and we do not restate it as our own measurement.

Every branch above — success, refusal, and failure alike — writes an audit
journal record before returning.

## Errors, Partial Success, and Cancellation

Per-item terminal states:

- `removed`
- `trashed`
- `invoked`
- `skipped`
- `changed_since_scan`
- `failed`

An independent item failure does not erase successful results and does not cause unrelated validated items to be reported as failed. Completed destructive actions are not rolled back.

Cleanup cancellation is checked between items. The current deletion, Trash, or external-tool process is allowed to finish or report its own error before the batch stops.

After execution, rescan only affected paths and refresh disk-space totals. Report estimated and observed reclaimed space separately when available.

## Privacy and Permissions

- All indexing and hashing stays on the Mac.
- No analytics or remote service receives paths, metadata, or hashes.
- Fast Scan avoids protected Desktop, Documents, and Downloads access.
- Deep Scan explicitly initiates access requests for those folders.
- Denied roots remain visible as skipped.
- Cloud placeholders are not hydrated.
- The index reset action is available from the UI and CLI.
- External tools are invoked only to inventory or act on their own state. Their output is parsed locally and is never forwarded anywhere.
- The audit journal is local, append-only, and contains local paths. It is never uploaded, and it is excluded from any diagnostic bundle unless the user explicitly chooses to share it. Clearing the journal is available from the UI and CLI.
- The Tree Explorer reads directory metadata only. It never opens file contents.

## Testing Strategy

### Unit tests

- repository, `.git` file, worktree, and submodule discovery;
- meaningful-activity exclusions and 90-day boundary;
- every artifact path-shape validator;
- manifest and lock-file combinations;
- Downloads 7/90/180-day boundaries;
- duplicate grouping and keeper selection;
- application bundle metadata and unknown last-used date;
- deterministic policy output;
- manifest predicates: a `package.json` declaring `react-native` in each of `dependencies` and `devDependencies`, one declaring neither, one that is invalid JSON, and one exceeding the byte ceiling;
- `pubspec.yaml` with a `flutter` dependency, with `sdk: flutter`, and a Dart-only package with neither;
- `pom.xml` with a readable `artifactId` and one that fails to parse;
- Gradle module detection with and without a `settings.gradle` ancestor inside the same repository;
- external-tool output parsing: `docker system df --format '{{json .}}'` lines including a `0B` reclaimable class and a percentage suffix; `brew cleanup -n` output containing `Warning:` lines, `Would remove:` lines, and the total line; `sdkmanager --list_installed` output with its progress prefix; `avdmanager list avd` output with both a loadable device and a "could not be loaded" entry;
- audit journal record shape for each terminal outcome, including dry runs and refusals.

### Safety tests

- symlink refusal;
- path traversal and safety-root escape;
- hard-link double-count prevention;
- filesystem identity replacement after scan;
- missing lock file after scan;
- source modification after scan;
- misleading directory names;
- duplicate file changed during hashing;
- Trash failure without permanent fallback;
- arbitrary recommendation ID and stale generation rejection;
- an external-tool action never executes an argument vector other than the one its analyzer declared, and no action passes through a shell;
- a stopped or absent external tool yields `unavailable` and never a scan failure or a start prompt;
- `~/Library/Application Support/JetBrains` is never offered while `~/Library/Caches/JetBrains` is;
- a `$TMPDIR` bundler cache newer than 7 days, owned by another user, or outside `$TMPDIR` is refused;
- a Tree Explorer node is never actionable, and a browsed path is refused as an action target;
- a failed and a refused action each produce a journal record, and a journal write failure does not block or reverse the action.

### Integration tests

- temporary home tree containing multiple unrelated and nested repositories;
- linked worktree and submodule fixtures;
- fake Spotlight and external-tool runners;
- fake Trash runner;
- streaming event order;
- scan cancellation and resume;
- incomplete, corrupt, and schema-old index handling;
- item-level dry-run and apply;
- partial cleanup success;
- a fake external-tool runner covering: available with reclaimable space, available with nothing to reclaim, binary absent, daemon stopped, non-zero exit with stderr, and output that changed between preview and execution;
- Tree Explorer drilldown over a fixture tree including an unreadable directory, a symlink, and a nested already-classified artifact;
- journal append, rotation at the size boundary, and a read-only journal directory.

Destructive tests call exported functions with explicit temporary roots. Tests must never invoke the real entry point against the developer's home directory.

### Native UI tests

- Fast Scan remains interactive;
- progressive Deep Scan rendering;
- cancellation;
- denied and granted folder access states;
- default selection policy;
- keeper replacement for duplicates;
- direct-delete, Trash, and external-tool totals;
- partial-result messaging.

### Live macOS acceptance

- read-only Deep Scan on a real development Mac;
- compare largest allocated-size findings with `du`;
- verify missing Spotlight metadata remains `unknown`;
- verify Fast Scan does not request protected-folder access;
- exercise destructive behavior only in purpose-built temporary project trees and test files intentionally sent to Trash.

## Performance Requirements

- Fast Scan never blocks the main UI thread.
- Findings may stream before the full scan completes.
- Deep Scan can be cancelled after the current directory operation or current file hash.
- When a continuous FSEvents history is available, Repeat Deep Scan does not rewalk or rehash unchanged indexed branches; after an event gap it performs the documented metadata-walk fallback.
- Only possible duplicates with matching sizes are hashed.
- Index writes are batched and bounded.
- The scanner does not retain all filesystem entries in memory; completed batches are persisted and released.

## Compatibility and Migration

1. Preserve existing Fast Scan JSON fields and category-based cleanup flags.
2. Add optional recommendation fields that old decoders ignore.
3. Introduce versioned streaming events for Deep Scan.
4. Add item-level dry-run and apply through recommendation IDs.
5. Migrate the native UI to the recommendation model while retaining legacy category support until all existing categories are represented.
6. Remove the legacy boolean-only UI path only in a later compatibility-breaking release.

## Delivery Phases

Phase order follows measured benefit per unit of implementation risk, not the
order the analyzers were designed in. Tool-managed inventory and curated caches
were promoted ahead of Downloads and Applications after the 2026-09-07 survey:
together they account for roughly 30 GB of directly reclaimable storage and
47 GiB of meaningful review on the development machine, and they need almost no
filesystem traversal — while Downloads and Applications require protected-folder
access, content hashing, and Spotlight metadata for a smaller measured return.

### Phase 1: Foundation and Project Analyzer — complete

- recommendation model and IDs;
- SQLite index and FSEvents-based dirty-path invalidation with safe full-walk fallback;
- streaming protocol and cancellation;
- recursive repository/worktree discovery;
- meaningful activity;
- initial project artifact recipes;
- item-level dry-run and apply;
- first Deep Scan native UI vertical slice.

### Phase 2: Tool-managed Storage and the Audit Journal

- audit journal writer, rotation, and retrofitting of every existing action path, including the legacy category `clean` and item-level `apply`;
- the `invoke_tool` action kind end to end: preview, re-preview before execution, argument-vector execution without a shell, and the `invoked` outcome;
- shared external-tool runner with `unavailable` facts, bounded stderr capture, and a fake runner for tests;
- Docker analyzer: `system df` inventory, four separate resource classes, four pinned prune vectors;
- Homebrew analyzer: `brew cleanup -n` preview and the `brew cleanup` action;
- Android analyzer: `sdkmanager --list_installed` and `avdmanager list avd` inventory, grouped, review-only;
- simulator integration through the existing `xcode-sim-prune` inventory, brought into the main report;
- Tool-managed Storage section in the native UI, including rows for unavailable tools.

The journal ships first inside this phase, because `invoke_tool` is the first
action kind whose effects we cannot re-derive from the filesystem afterwards.

### Phase 3: Expanded Caches and Manifest-derived Recipes

- the curated cache table: Yarn classic and Berry, uv, pdm, pipenv, JetBrains caches, NuGet, Playwright, Puppeteer, Electron and electron-builder, CocoaPods cache and repos, Maven, pub, Flutter/Dart, Deno, RubyGems, .NET, LLDB, Android build caches;
- the "cleanable but never default-selected" rule for offline build inputs;
- `$TMPDIR` bundler caches under the ownership and 7-day rules;
- review-only large stores, including local model directories;
- the manifest-predicate mechanism plus the React Native, Flutter, Maven, and Gradle-module recipes;
- documentation and a test for every added location and predicate.

### Phase 4: Downloads Analyzer

- protected-folder access;
- age policies;
- incremental hashing;
- duplicate keeper selection;
- Trash execution.

### Phase 5: Application Analyzer

- bundle inventory and size;
- bundle ID and version;
- Spotlight last-used date and unknown state;
- duplicate and one-year groups;
- Trash execution for bundles.

### Phase 6: Tree Explorer

- on-demand single-level directory measurement with session caching and invalidation;
- unreadable-directory, symlink, volume-boundary, and cloud-placeholder handling;
- the Explore Disk UI surface;
- coincidence with existing recommendations shown without creating new actions.

Tree Explorer is last because it is the only component that adds no cleanup
capability. It becomes most useful once the detectors above have removed the
storage they can explain, leaving a genuinely unexplained remainder to browse.

Each phase after Phase 1 receives its own implementation plan and may receive a focused design addendum if tool behavior discovered during implementation changes the approved assumptions.

## Acceptance Criteria for Phase 1

1. A Deep Scan can discover Git repositories anywhere beneath allowed home-directory roots without a developer-root naming heuristic.
2. Valid repositories inactive for 90 days produce explainable project-artifact recommendations.
3. Source files, manifests, lock files, `.git`, and valid worktree roots are never deletion targets.
4. Supported dependency trees require the matching lock file before default selection.
5. Recommendation IDs cannot be used to delete arbitrary paths or stale changed targets.
6. The native app shows streamed progress and can cancel Deep Scan.
7. A cancelled scan leaves a valid resumable index and no cleanup-eligible incomplete results.
8. Existing Fast Scan and category cleanup behavior remain operational.
9. A repeat scan with continuous FSEvents history revisits dirty branches only; an event-history gap triggers a safe full metadata walk.
10. Python and native test suites pass.
11. Live acceptance confirms findings against a real macOS filesystem while destructive testing remains confined to temporary fixtures.

## Acceptance Criteria for Phase 2

1. Every action path — legacy category `clean`, item-level `apply`, and `invoke_tool` — appends a journal record before reporting its outcome, and refusals and dry runs are journalled as distinctly as successes.
2. A journal write failure produces a warning and never blocks or reverses an action.
3. Docker, Homebrew, Android, and simulator inventory is obtained without starting or waking any tool, and an absent binary or stopped daemon produces a visible `unavailable` row rather than a scan failure or an omission.
4. Docker's build cache, images, containers, and volumes are separately previewable and separately actionable.
5. No external-tool action executes an argument vector other than the one its analyzer declared, and no action passes through a shell.
6. Every external-tool action re-runs its preview immediately before execution and reports the tool's own result separately from the pre-action estimate.
7. Nothing under a tool's managed storage is deleted by path, and no tool-managed item is selected by default.
8. Existing Fast Scan, Deep Scan, and category cleanup behavior remain operational.
9. Python and native test suites pass, including the fake external-tool runner covering unavailable, empty, failing, and changed-between-preview-and-execution cases.
10. Live acceptance on a real machine reconciles each tool's reported reclaim figure against the tool's own post-action output, with observed and estimated values reported separately.
