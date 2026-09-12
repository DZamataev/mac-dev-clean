# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### Added

- `deep-scan`, `apply`, and `reset-index` commands. Deep Scan recursively finds
  Git repositories, measures each project's last meaningful change, and offers
  reproducible build outputs and lock-file-backed dependency trees from projects
  inactive for 90 days.
- A streamed Deep Scan page in the macOS app with live progress, cancellation,
  per-item evidence, and per-item selection.
- A local SQLite index at `~/Library/Caches/mac-dev-clean/index.sqlite3` with
  FSEvents-based incremental rescanning and a safe full-walk fallback whenever
  the event history is incomplete.

### Tool-managed storage and action journal

- Added read-only `tools` inventory for Docker, Homebrew, Android SDK/AVD, and
  Simulator storage, with explicit unavailable states and exact command previews.
- Added `tools-apply` for one current opaque recommendation ID at a time, with
  fresh preview revalidation, frozen argv, no shell execution, no default
  selection, and official Docker, Homebrew, Android, and `simctl` commands.
- Added a native Tool-managed Storage page with status, command preview,
  selection, confirmation, progress, refusal, and refresh states.
- Added an append-only, rotating local action journal for cleanup/apply attempts,
  including refusals, failures, skipped dry runs, and successful actions, plus
  active-segment `journal` inspection and explicit `journal --clear`.
- Extended the same journal to legacy clean and confirmed interactive cleanup.

### Fixed

- Switching to Deep Scan or Tool-managed while the initial storage scan was still
  running cancelled that scan and surfaced its cancellation as
  `The operation couldn't be completed. (Swift.CancellationError error 1.)`. The
  initial scan now survives tab changes, cancellation is never reported as an
  error, and both manual scan tabs disable their start button while another scan
  owns the backend while explaining that the initial scan must finish first.

## 0.5.2 - 2026-07-11

- Added `update_app.sh` to rebuild, safely replace, and relaunch the locally
  installed native app without manually moving bundles into Applications.

## 0.5.1 - 2026-07-11

- Added a native SwiftUI macOS app with cleanup grouping, selection, confirmation, progress/error states, Finder reveal actions, a separate review-only screen, and automatic rescanning.
- Added `run_gui.sh` as a stable packaged-app launcher and `scripts/build_macos_app.sh` for an ad-hoc-signed standalone `.app` containing the Python engine and existing raven branding.
- Prevented automatic GUI scans from touching protected Documents, Desktop, or Downloads folders; explicit project-local DerivedData cleanup remains available from the CLI.
- Added a universal Developer ID signing and notarization pipeline with secure Keychain credentials, hardened runtime, ticket stapling, Gatekeeper validation, and final ZIP packaging.
- Added a native About page with appearance-aware Raven Vector branding, app version information, and a link to ravenvector.com.
- Made cleanup-card disclosure immediate and easier to hit by removing the costly first-use insertion work and turning the full non-checkbox header into a large disclosure target.
- Changed the native app bundle identifier to `com.ravenvector.mac-dev-clean` to align with Raven Vector branding.
- Added a shared Xcode project with app and test targets, automatic signing, hardened runtime, universal Release settings, embedded resources/Python engine, and Organizer-ready archiving.
- Added a guarded release helper that bumps semantic versions, finalizes the changelog, runs tests, pushes `main`, and publishes the corresponding GitHub release.
- Added native-app privacy, architecture, branding, publication-audit, and maintainer documentation; tightened Git ignores and CI for safe public development.
- Hardened native Python launching against inherited module/startup injection and enabled Xcode user-script sandboxing for the embedded engine build phase.
- Kept Python and native Xcode marketing versions synchronized in the release helper.
- Added ready-to-paste App Store Connect metadata, privacy and review answers,
  screenshot copy, and a documented sandbox/self-contained-build readiness gate.

## 0.5.0 - 2026-07-11

- Reworked human-readable scan and cleanup output into summary, quick-win, cleanable, and review-only sections with spaced two-line entries, wrapped notes, and home-relative paths.
- Suppressed report-only locations under 1 MiB so empty directories such as an unused MobileSync backup folder are not presented as meaningful review items.

- Added scanning and dry-run-first cleanup for `~/Library/Developer/XCTestDevices`, where parallel Xcode tests can leave hundreds of multi-gigabyte simulator clones.
- Delegated XCTest clone deletion to `simctl` with the dedicated device set instead of deleting simulator internals directly, and refuse cleanup while any clone is booted.
- Added Xcode device-log cleanup plus `--xcode-caches` as a broad explicit cleanup option for supported data under `~/Library/Developer`.
- Kept the no-argument `mac-dev-clean` shortcut inclusive of XCTest clone cleanup, with regression coverage so no explicit category flag is required.
- Stopped presenting the sum of `simctl` XCTest clone logical sizes as reclaimable disk usage; clone storage is now labeled `shared/unknown` because APFS sharing can make the logical total vastly exceed physical allocation.
- Updated `run_local.sh` to invoke the standard one-confirmation cleanup instead of hard-coding clone-only commands.
- Report XCTest clone cleanup as an asynchronous delete request and explain that CoreSimulator/APFS free-space reclamation may lag for several minutes.
- Added supported `simctl` cleanup for system-wide CoreSimulator dyld shared caches.
- Added cleanup for Chrome's downloaded on-device AI model, browser component/update caches, Cursor/Windsurf/Codex desktop caches, Bun's install cache, and downloaded aerial wallpapers.
- Added strict discovery and path-marker validation for project-local Xcode DerivedData directories.
- Added report-only guidance for Codex history/generated images, local Apple device backups, Command Line Tools, and safe manual iCloud offloading.

## 0.4.0 - 2026-06-07

- Skipped cleanable scan targets under 1 MiB to avoid recommending no-op cleanup for tiny cache directories.
- Added cleanup support for Xcode documentation and DeviceSupport caches, pnpm and other package/tool caches, browser caches, plus report-only surfacing for Xcode Archives and Codex runtime caches.
- Added `xcode-sim-prune delete-devices` for dry-run-first deletion of selected shutdown simulator devices by exact name or UDID.
- Made `xcode-sim-prune` without arguments scan safe simulator device cleanup candidates and prompt before deleting anything.
- Updated the no-argument `xcode-sim-prune` scan to prioritize meaningful wins by skipping tiny never-booted devices and including shutdown devices that are at least 1 GB.
- Added an immediate `mac-dev-clean` scan progress message so slow disk usage scans do not leave users staring at a silent terminal.
- Added cleanable/report-only scan totals and top dry-run command suggestions to human and JSON scan output.

## 0.1.0 - 2026-06-06

Initial open-source release.

- Added `mac-dev-clean` with `scan`, `clean`, and `report` commands.
- Added safe cleanup support for Xcode DerivedData, simulator caches, Homebrew cache, npm cache/logs, Gradle caches, and aged `node_modules`.
- Added report-only Docker Desktop storage visibility.
- Added `xcode-sim-prune` for focused CoreSimulator listing, unavailable device deletion, old runtime deletion, and unused simulator erase workflows.
- Added dry-run mode, JSON output, human-readable tables, explicit deletion flags, and age filters.
- Added safety-root validation, known cache path-shape checks, symlink refusal, and malformed simulator UDID refusal.
- Added unit tests, macOS GitHub Actions CI, MIT licensing, contributing guidance, and a security policy.
