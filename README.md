<p align="center">
  <img src="assets/mac-dev-clean-logo.png" alt="mac-dev-clean raven logo" width="240">
</p>

<h1 align="center">mac-dev-clean</h1>

<p align="center">
  <strong>Safe, dry-run-first cleanup tools for macOS developer disk bloat.</strong>
</p>

<p align="center">
  <a href="https://github.com/starforged-guardian/mac-dev-clean/actions/workflows/ci.yml"><img src="https://github.com/starforged-guardian/mac-dev-clean/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/Code-MIT-yellow.svg" alt="Code license: MIT"></a>
  <a href="BRANDING.md"><img src="https://img.shields.io/badge/Brand_assets-All_rights_reserved-red.svg" alt="Brand assets: All rights reserved"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python 3.9+"></a>
  <a href="README.md"><img src="https://img.shields.io/badge/platform-macOS-lightgrey.svg" alt="macOS"></a>
  <a href="#install"><img src="https://img.shields.io/badge/install-pipx-6650a4.svg" alt="Install with pipx"></a>
</p>

## Reclaim your Mac. Keep your confidence.

Developer tools are hungry. Xcode builds, simulator caches, package managers,
browsers, and abandoned dependencies can quietly consume tens of gigabytes.
`mac-dev-clean` turns that storage sprawl into a clear, reviewable cleanup plan.

Scan first. See exactly what is safe to clean. Reclaim the space only when you
are ready.

<p align="center">
  <img src="docs/images/mac-dev-clean-cleanup.png" alt="mac-dev-clean showing cleanable developer storage grouped by category" width="100%">
</p>

### A safer way to make room

- **See the win before you clean.** Live totals show cleanable storage, review-only files, selected items, and free disk space at a glance.
- **Clean by category.** Reclaim Xcode build artifacts, simulator caches, browser and model caches, package caches, and more without hunting through Library folders.
- **Keep important files out of harm's way.** Archives, installed apps, and other personal storage stay in a separate Review Only lane and are never included in automatic cleanup.
- **Stay local and in control.** No account, cloud service, analytics, or mystery background cleanup. The app and CLI run on your Mac, with dry-run-first behavior and explicit confirmation.

### Know what not to delete

Some large files deserve judgment, not automation. mac-dev-clean surfaces them
with context and one-click Finder access, while leaving the final decision to
you.

<p align="center">
  <img src="docs/images/mac-dev-clean-review-only.png" alt="mac-dev-clean Review Only screen for files that require a manual decision" width="100%">
</p>

## Built for developer Macs

The native SwiftUI app and Python CLI share the same cleanup engine and safety
checks. `mac-dev-clean` reports disk usage without deleting anything by default,
while `xcode-sim-prune` manages simulator storage through `xcrun simctl` instead
of reaching into simulator internals directly.

Under the hood, the project includes dry-run output, JSON reports, safety-root
validation, automated tests, CI, and MIT-licensed source code—so you can inspect
the tool before trusting it with your disk.

## Install

Recommended GitHub install with `pipx`:

```sh
pipx install git+https://github.com/starforged-guardian/mac-dev-clean.git
```

Install with `pip`:

```sh
python3 -m pip install git+https://github.com/starforged-guardian/mac-dev-clean.git
```

Install from a local checkout:

```sh
python3 -m pip install -e .
```

On macOS, `pip` may install the commands into your user Python scripts directory. If `mac-dev-clean` or `xcode-sim-prune` is not found after installation, add that directory to your shell path:

```sh
export PATH="$HOME/Library/Python/3.9/bin:$PATH"
```

To make that permanent for zsh:

```sh
printf '\nexport PATH="$HOME/Library/Python/3.9/bin:$PATH"\n' >> ~/.zprofile
```

Or run without installing:

```sh
PYTHONPATH=src python3 -m mac_dev_clean scan
```

## Native macOS App

Build or update the copy in `/Applications`, then launch it:

```sh
./update_app.sh
```

Run that command again whenever the checkout changes. It rebuilds the latest
code, safely replaces `/Applications/mac-dev-clean.app`, and reopens the app, so
there is no need to drag a newly built bundle into Applications. Pass
`--no-open` to update without launching, or set `MACOS_INSTALL_DIR` to install
somewhere else.

Launch the SwiftUI interface directly from the checkout:

```sh
./run_gui.sh
```

The native app scans through the same Python engine, groups related cleanup
locations, selects safe cleanable categories by default, requires a macOS
confirmation before deletion, and rescans after cleanup. Review-only locations
stay on a separate screen and can be revealed in Finder. An About page includes
Raven Vector branding, the installed app version, and a link to
[ravenvector.com](https://ravenvector.com).

Automatic GUI scans avoid project discovery under protected folders such as
Documents, Desktop, and Downloads, so opening the app does not trigger macOS
folder-access prompts. Project-local DerivedData remains available from the CLI
with the explicit `--project-derived-data` cleanup option.

Deep Scan is the exception, and only when you ask for it: it walks the whole
home directory, protected folders included. If macOS denies access to one, the
scan reports which folder was blocked instead of quietly returning fewer
results. It also warns when a directory tree is too deep to walk to the bottom,
so a truncated walk is never mistaken for a clean one.

Build a distributable, ad-hoc-signed app bundle:

```sh
./scripts/build_macos_app.sh
open dist/mac-dev-clean.app
```

The bundle contains the Python cleanup engine and uses the existing raven logo.
Python 3 from Xcode Command Line Tools is still required. Close Xcode, Simulator,
browsers, and editors before cleaning their caches for the best result.

For distribution to other Macs, follow [DISTRIBUTING.md](DISTRIBUTING.md). The
release pipeline builds a universal app, signs it with Developer ID and hardened
runtime, submits it through `notarytool`, staples the accepted ticket, validates
it with Gatekeeper, and creates the final ZIP.

To build, sign, archive, and notarize through Xcode instead, open
`macos/MacDevClean.xcodeproj`. Select the `MacDevCleanApp` target, choose your
Apple Developer team under Signing & Capabilities, then use Product > Archive.
The shared scheme includes the native app tests and the archive bundles the same
Python cleanup engine and branding resources as the command-line build script.
See [Native macOS App](docs/NATIVE_MACOS_APP.md) for the UI architecture,
privacy boundaries, build checks, and Xcode project workflow.

## Quick Demo

Find developer cache usage:

```sh
mac-dev-clean scan
mac-dev-clean report --json
```

Human-readable scan output includes cleanable/report-only totals and suggested dry-run commands for the biggest quick wins.

Scan cleanable cache locations and confirm deletion interactively:

```sh
mac-dev-clean
```

This shortcut automatically includes every supported cleanable category,
including project-local DerivedData, generated XCTest clones, simulator runtime
caches, browser AI models, editor/updater caches, and package caches. Stop active
developer tools, review the displayed targets and report-only guidance, then
confirm once.

Preview cleanup without deleting files:

```sh
mac-dev-clean clean --xcode-caches --dry-run
mac-dev-clean clean --xcode-test-devices --dry-run
mac-dev-clean clean --xcode-derived-data --dry-run
mac-dev-clean clean --xcode-device-support --dry-run
mac-dev-clean clean --package-caches --browser-caches --dry-run
mac-dev-clean clean --node-modules --older-than 60d --search-root ~/Code --dry-run
```

Inspect and prune simulator storage:

```sh
xcode-sim-prune
xcode-sim-prune list
xcode-sim-prune erase-unused --dry-run
xcode-sim-prune delete-runtimes --older-than 180d --dry-run
```

## Commands

Scan common locations and print a table:

```sh
mac-dev-clean scan
```

Print the same report as JSON:

```sh
mac-dev-clean report --json
```

Scan cleanable cache locations and delete them after a `y/N` prompt:

```sh
mac-dev-clean
```

Include old `node_modules` directories in the interactive scan:

```sh
mac-dev-clean interactive --include-node-modules --older-than 60d --search-root ~/Code
```

Preview cleaning Xcode build artifacts:

```sh
mac-dev-clean clean --xcode-derived-data --dry-run
```

Preview all supported cleanup under `~/Library/Developer`, including generated
XCTest simulator clones:

```sh
mac-dev-clean clean --xcode-caches --dry-run
```

After reviewing the paths and sizes, rerun without `--dry-run`. Stop tests and
close Xcode first; cleanup refuses to delete an XCTest device set unless every
clone is shut down.

Clean only the simulator clones generated by parallel XCTest runs:

```sh
mac-dev-clean clean --xcode-test-devices --dry-run
mac-dev-clean clean --xcode-test-devices
```

Clean Xcode DerivedData and module cache:

```sh
mac-dev-clean clean --xcode-derived-data
```

Clean Xcode documentation cache:

```sh
mac-dev-clean clean --xcode-documentation-cache
```

Clean Xcode DeviceSupport files:

```sh
mac-dev-clean clean --xcode-device-support --dry-run
mac-dev-clean clean --xcode-device-support
```

Clean imported Xcode device logs and diagnostics:

```sh
mac-dev-clean clean --xcode-device-logs --dry-run
mac-dev-clean clean --xcode-device-logs
```

Clean simulator caches and logs:

```sh
mac-dev-clean clean --simulator-caches
```

Remove generated system-wide simulator dyld caches through `simctl`:

```sh
mac-dev-clean clean --simulator-dyld-cache --dry-run
mac-dev-clean clean --simulator-dyld-cache
```

Clean strictly validated project-local Xcode DerivedData directories:

```sh
mac-dev-clean clean --project-derived-data --dry-run
mac-dev-clean clean --project-derived-data
```

Clean Homebrew cache:

```sh
mac-dev-clean clean --brew-cache
```

Clean old `node_modules` directories:

```sh
mac-dev-clean clean --node-modules --older-than 60d --dry-run
mac-dev-clean clean --node-modules --older-than 60d
```

Clean dependency and tool caches:

```sh
mac-dev-clean clean --package-caches --dry-run
mac-dev-clean clean --package-caches
```

Clean supported browser caches:

```sh
mac-dev-clean clean --browser-caches --dry-run
mac-dev-clean clean --browser-caches
```

This includes Chrome's downloaded on-device AI model. Chrome manages and may
redownload that model when the machine again meets its free-space requirements.

Clean editor, desktop-app, and updater caches:

```sh
mac-dev-clean clean --editor-caches --dry-run
mac-dev-clean clean --editor-caches
```

Clean downloaded aerial wallpaper videos:

```sh
mac-dev-clean clean --wallpaper-cache --dry-run
mac-dev-clean clean --wallpaper-cache
```

Search specific roots for `node_modules`:

```sh
mac-dev-clean scan --search-root ~/Code --search-root ~/Projects
mac-dev-clean clean --node-modules --older-than 90d --search-root ~/Code
```

### Deep Scan (project artifacts)

Deep Scan finds Git repositories anywhere under your home folder and offers the
reproducible artifacts of projects that have not changed in 90 days. Source
files, manifests, lock files, and `.git` are never deletion targets.

```sh
# Stream findings as newline-delimited JSON events
mac-dev-clean deep-scan

# Print one summary object instead
mac-dev-clean deep-scan --json

# Preview, then apply a specific recommendation
mac-dev-clean apply --id <id> --dry-run
mac-dev-clean apply --id <id>

# Discard the local index; the next deep scan rebuilds it
mac-dev-clean reset-index
```

A dependency tree is only preselected when its lock file is present, so it can
be restored exactly. Build outputs are preselected on inactivity alone. Every
recommendation is revalidated against the live filesystem before anything is
removed: if the lock file disappeared or the project was edited in the
meantime, the item is refused rather than deleted.

Staleness is judged on the artifact itself, not only on the surrounding
project. Deep Scan's 90-day inactivity window deliberately ignores generated
directories like `node_modules`, `.venv`, and `vendor` when it looks at a
project's *source*, so those directories can never fake staleness for the
project around them. But before an artifact from one of those directories is
offered or removed, Deep Scan separately checks whether anything inside the
artifact itself has changed recently. A hand-patched file inside a dependency
or build directory — a `patch-package` fix, an editable install, a locally
patched vendored gem — keeps that specific artifact out of preselection and
out of `apply`, even when the rest of the project looks abandoned.

Recommendations are identified by opaque IDs rather than raw paths. `apply`
looks the ID up in the local index to find the authoritative path, then
re-checks that path directly on disk before deleting anything — so an ID that
no longer matches live, safe evidence never deletes anything. Nothing that
touches source files, manifests, lock files, `.git`, or worktree roots is ever
preselected, and applying one recommendation never affects any other project.

The index lives at `~/Library/Caches/mac-dev-clean/index.sqlite3` and is purely
a cache. It stores paths, sizes, and timestamps — never file contents — and is
never sent anywhere. Every Deep Scan currently performs a full walk. The scan
reports whether FSEvents history was complete enough to have skipped unchanged
directories, which is the groundwork for incremental rescans in a later
release; today that flag changes nothing about how much is scanned.

### Tool-managed storage

Docker, Homebrew, Android SDK/AVD, and Simulator storage must be managed through
their official command-line tools rather than by deleting their private files.
Inventory, preview one exact recommendation, and inspect the local action
journal with:

```sh
mac-dev-clean tools
mac-dev-clean tools-apply --id <id> --dry-run
mac-dev-clean journal --tail 20
```

`tools` is read-only and does not start Docker Desktop, Android Studio, or
Simulator. Each finding shows the exact command a real apply would invoke and
is stored under an opaque ID in
`~/Library/Caches/mac-dev-clean/tools.sqlite3`. `tools-apply` accepts only an ID
from the current inventory, re-runs the read-only preview, revalidates its
reported state, and invokes only the frozen command vector for that one item.
Nothing is selected automatically.

Docker inventory covers default-prune build cache, dangling images, stopped
containers, and unused anonymous volumes. It deliberately does not offer
`docker image prune -a`: that command removes tagged and untagged images not
referenced by any container, including images pulled deliberately. That is
broader than the offered dangling-image action and its preview. Docker's
class-wide figures can also be upper bounds, and space inside Docker Desktop's
disk image may not return to the macOS volume until Docker compacts it.

Homebrew's number comes from `brew cleanup -n`, not from measuring only
`~/Library/Caches/Homebrew`. It can include old installed formula versions,
stale downloads, and Homebrew-managed runtime files elsewhere under the
Homebrew prefix, so the figure can differ from the cache directory's size.
Displayed KB/MB/GB values use decimal SI scaling, matching Docker and Homebrew.

Android SDK packages and Android Virtual Devices (AVDs) are inventory-only
during `tools`: no component, emulator, app, or device data is changed. A real
`tools-apply` for one current ID delegates removal to `sdkmanager --uninstall`
or `avdmanager delete avd`; these recommendations are never selected by
default because projects may depend on old toolchains and AVD deletion loses
its installed apps and data.

## What It Scans

Cleanable locations:

- Xcode DerivedData: `~/Library/Developer/Xcode/DerivedData`
- Xcode module cache: `~/Library/Developer/Xcode/ModuleCache.noindex`
- Xcode documentation cache: `~/Library/Developer/Xcode/DocumentationCache`
- Xcode documentation index: `~/Library/Developer/Xcode/DocumentationIndex`
- Xcode DeviceSupport: `~/Library/Developer/Xcode/{iOS,tvOS,watchOS,visionOS} DeviceSupport`
- Xcode device logs: `~/Library/Developer/Xcode/DeviceLogs`
- XCTest simulator clones: `~/Library/Developer/XCTestDevices` (deleted through `simctl`)
- Project-local Xcode DerivedData: `~/*/DerivedData*`, `~/*/{Build,build}/DerivedData` (only with Xcode markers)
- CoreSimulator caches: `~/Library/Developer/CoreSimulator/Caches`
- CoreSimulator runtime dyld caches: `/Library/Developer/CoreSimulator/Caches/dyld` (removed through `simctl`)
- CoreSimulator logs: `~/Library/Logs/CoreSimulator`
- Simulator device caches: `~/Library/Developer/CoreSimulator/Devices/*/data/Library/Caches`
- Homebrew cache: `~/Library/Caches/Homebrew`
- npm cache and logs: `~/.npm/_cacache`, `~/.npm/_logs`
- pnpm store and cache: `~/Library/pnpm/store`, `~/Library/Caches/pnpm`
- Node tooling caches: `~/Library/Caches/node-gyp`, `~/Library/Caches/typescript`, `~/Library/Caches/bun`
- Python caches: `~/Library/Caches/pip`, `~/.cache/pip`, `~/Library/Caches/pypoetry`, `~/.cache/pypoetry`
- SwiftPM cache: `~/Library/Caches/org.swift.swiftpm`
- Go caches: `~/Library/Caches/go-build`, `~/go/pkg/mod`
- Rust caches: `~/.cargo/registry`, `~/.cargo/git`
- Gradle caches: `~/.gradle/caches`, `~/.gradle/daemon`, `~/.gradle/wrapper/dists`
- Browser caches: `~/Library/Caches/Google`, `~/Library/Caches/BraveSoftware`, `~/Library/Caches/Firefox`, `~/Library/Caches/com.apple.Safari`
- Chrome on-device AI model and Chrome/Brave downloaded component caches
- Google Updater package cache
- Cursor, Windsurf, and Codex desktop cache directories
- Bun install cache: `~/.bun/install/cache`
- Downloaded aerial wallpaper videos
- Discovered project `node_modules` directories

Report-only locations:

- Xcode Archives: `~/Library/Developer/Xcode/Archives`
- Docker Desktop logs: `~/Library/Containers/com.docker.docker/Data/log`
- Docker Desktop VM data: `~/Library/Containers/com.docker.docker/Data/vms`
- Codex runtime cache: `~/.cache/codex-runtimes`
- Codex task history and generated images
- Local Apple device backups
- Command Line Tools installation
- Installed applications, with guidance to review them in macOS Storage settings

Docker VM data is intentionally report-only because deleting it directly can remove images, containers, and volumes. Use Docker's own prune commands for Docker cleanup.

## iCloud and Long-Term Storage

`mac-dev-clean` reports data that may be worth archiving, but it does not move
active projects, Xcode state, Codex task history, or device backups into iCloud.
Moving those live directories can break tools when macOS evicts files locally.

Good manual candidates include finished generated images, exported screenshots,
old release artifacts you no longer need in Xcode Organizer, and other completed
documents. Copy them into iCloud Drive, verify the upload, then use Finder's
**Remove Download** action to release the local copy without deleting the cloud
file. Apple documents this workflow in [Work with folders and files in iCloud
Drive](https://support.apple.com/en-euro/guide/mac-help/mchl1a02d711/mac).

For ongoing personal files, enable **Optimize Mac Storage** in System Settings >
your Apple Account > iCloud > Drive. Keep source checkouts and active build
directories outside iCloud Drive.

## Safety Model

- `scan` and `report` never delete files.
- Running `mac-dev-clean` without arguments scans cleanable cache locations and prompts before deleting anything.
- `interactive --include-node-modules` requires `--older-than`, such as `60d`.
- `clean` fails unless you pass at least one explicit category flag.
- `--node-modules` requires `--older-than`, such as `60d`.
- Fixed cache directories use contents-only cleanup, preserving the parent directory.
- `node_modules` cleanup removes the selected `node_modules` directory tree.
- Xcode DeviceSupport cleanup removes cached support files that Xcode can recreate when needed.
- XCTest clone cleanup delegates to `xcrun simctl --set ~/Library/Developer/XCTestDevices`.
- XCTest clone cleanup refuses to run unless every clone is shut down; stop tests and close Xcode first.
- XCTest clones are displayed as `shared/unknown` instead of summing `simctl`'s logical clone sizes; APFS clones may share nearly all of their blocks, so logical totals do not predict reclaimed disk space.
- Successful clone cleanup is reported as `delete requested`; CoreSimulator and APFS can continue reclaiming shared blocks for several minutes after `simctl` returns.
- `--xcode-caches` selects all supported Xcode, DeviceSupport, XCTest clone, and simulator cache categories.
- System simulator dyld-cache cleanup delegates to `xcrun simctl runtime dyld_shared_cache remove --all`.
- Project-local DerivedData cleanup requires both `info.plist` and `Build` markers and only accepts known path shapes.
- Report-only items are never included in the confirmation batch.
- Xcode Archives, Docker data, and active-session runtime caches are report-only.
- Cleanup targets must match known cache path shapes and stay inside their scan safety root.
- Symlink targets are refused.
- Scan targets under 1 MiB are skipped to avoid surfacing empty directories or insignificant cleanup/review suggestions.
- Use `--dry-run` before deleting to see the selected paths.

### Action journal

Each cleanup or apply attempt — including a refusal, error, skipped dry run, or
successful action — is appended locally to
`~/Library/Logs/mac-dev-clean/actions.jsonl`. Records include the timestamp,
opaque recommendation ID when applicable, category and target, the preview or
action argv associated with the attempt, dry-run flag, outcome, reported
reclaimable bytes, and bounded detail suitable for auditing. An argv records
what was requested for that attempt; a refusal can be journaled before a
process starts. Read-only `scan`, `report`, and `tools` inventory are not
journaled. The journal may contain local paths and command arguments; review it
before sharing.

The journal is never uploaded and is not included in shared diagnostics. It is
bounded by rotation and retains one rotated segment. `mac-dev-clean journal`
shows the active segment only; inspect `actions.jsonl.1` directly when older
retained records are needed. Explicitly remove both active and rotated local
files with:

```sh
mac-dev-clean journal --clear
```

Supported age values are `s`, `m`, `h`, `d`, and `w`, for example `30d`, `12h`, or `2w`.

## xcode-sim-prune

This repository also includes `xcode-sim-prune`, a focused CLI for Xcode simulator storage. It uses `xcrun simctl` for simulator operations instead of deleting simulator internals directly.

Run an automatic scan and confirm safe simulator device deletion interactively:

```sh
xcode-sim-prune
```

By default, `xcode-sim-prune` prompts before deleting shutdown simulator devices that are unavailable to the current Xcode or at least 1 GB.

List simulator devices and runtime images:

```sh
xcode-sim-prune list
xcode-sim-prune list --json
```

Preview and delete unavailable devices:

```sh
xcode-sim-prune delete-unavailable --dry-run
xcode-sim-prune delete-unavailable
```

Preview and delete specific shutdown simulator devices by exact name or UDID:

```sh
xcode-sim-prune delete-devices --name "iPhone 17" --dry-run
xcode-sim-prune delete-devices --name "iPhone 17"
xcode-sim-prune delete-devices --udid BB56347C-413D-42CF-AA83-98F77434BE4A --dry-run
```

Preview a broader pass that keeps named devices:

```sh
xcode-sim-prune delete-devices --all-shutdown --keep-name "iPhone 17 Pro" --dry-run
```

Preview and delete runtime images not used within an age threshold:

```sh
xcode-sim-prune delete-runtimes --older-than 180d --dry-run
xcode-sim-prune delete-runtimes --older-than 180d
```

Preview and erase unused simulator device data:

```sh
xcode-sim-prune erase-unused --dry-run
xcode-sim-prune erase-unused
```

By default, `erase-unused` only targets shutdown devices that have never been booted. To also include stale shutdown devices, pass an age filter:

```sh
xcode-sim-prune erase-unused --older-than 60d --dry-run
```

Safety notes:

- `list` never changes anything.
- Running `xcode-sim-prune` without arguments scans meaningful simulator device deletion candidates and prompts before deleting anything.
- `delete-unavailable` delegates to `xcrun simctl delete unavailable`.
- `delete-devices` only deletes shutdown devices with safe UDIDs; names are exact matches, so `iPhone 17` does not match `iPhone 17 Pro`.
- `delete-runtimes` requires `--older-than` and delegates to `xcrun simctl runtime delete --notUsedSinceDays`.
- `erase-unused` never erases booted devices.
- Malformed simulator device IDs are ignored before calling `simctl`.
- All destructive commands support `--dry-run` and `--json`.

## Development

Run tests:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
swift test --package-path macos
```

Run smoke checks:

```sh
PYTHONPATH=src python3 -m mac_dev_clean report --json --no-node-modules --no-project-derived-data
PYTHONPATH=src python3 -m mac_dev_clean.xcode_sim_prune list --json
```

## Contributing

Contributions are welcome. Please keep changes aligned with the safety model: scans must be read-only, deletion must be explicit, and new cleanup behavior should include tests.

Project policies and maintainer references:

- [Native macOS App](docs/NATIVE_MACOS_APP.md)
- [Privacy](PRIVACY.md)
- [Security](SECURITY.md)
- [Open Source Publication Audit](docs/OPEN_SOURCE_AUDIT.md)
- [Branding and Trademarks](BRANDING.md)
- [Contributing](CONTRIBUTING.md)
- [Developer ID Distribution](DISTRIBUTING.md)
- [App Store Connect Content](APP_STORE_CONNECT.md)

## License

The source code and documentation are open source under the MIT License. Raven
Vector names, logos, app icons, and product identity are expressly excluded
from that license and remain all rights reserved. Forks and modified builds must
remove or replace them before distribution. See [LICENSE](LICENSE) and
[BRANDING.md](BRANDING.md) for the exact scope and limited permissions.
