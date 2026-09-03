from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from .discovery import Repository
from .recommendation import RestorationCost
from .scanner import path_size

INACTIVITY_DAYS = 90
MIN_ARTIFACT_BYTES = 1024 * 1024

# Directory names that never represent human work on the project. Excluded from
# the meaningful-activity calculation only; a directory is not treated as an
# artifact just because it appears here.
GENERATED_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "Pods",
        "build",
        ".build",
        "target",
        ".gradle",
        ".cxx",
        "DerivedData",
        "vendor",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        ".nuxt",
        "dist",
        "coverage",
        "tmp",
        ".idea",
        ".vscode",
    }
)

GENERATED_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db"})
GENERATED_FILE_SUFFIXES = (".log", ".pyc", ".swp", ".tmp", ".orig")


class ArtifactKind(Enum):
    DEPENDENCY_TREE = "dependency_tree"
    BUILD_OUTPUT = "build_output"


@dataclass(frozen=True)
class Recipe:
    detector_id: str
    label: str
    relative_path: str
    kind: ArtifactKind
    #: At least one of these must exist for the artifact to be a candidate.
    manifests: Tuple[str, ...]
    #: At least one must exist before default selection is allowed. Empty means
    #: the recipe's manifest is itself sufficient (build outputs).
    locks: Tuple[str, ...]
    #: Extra directory that must exist, e.g. an .xcodeproj sibling.
    marker_globs: Tuple[str, ...]
    category: str
    restoration: RestorationCost
    note: str


RECIPES: Tuple[Recipe, ...] = (
    Recipe(
        detector_id="node-modules",
        label="node_modules",
        relative_path="node_modules",
        kind=ArtifactKind.DEPENDENCY_TREE,
        manifests=("package.json",),
        locks=(
            "package-lock.json",
            "npm-shrinkwrap.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "bun.lock",
            "bun.lockb",
        ),
        marker_globs=(),
        category="project-dependencies",
        restoration=RestorationCost.REDOWNLOAD,
        note="JavaScript dependencies. Reinstall with npm, yarn, pnpm, or bun.",
    ),
    Recipe(
        detector_id="ios-pods",
        label="CocoaPods Pods",
        relative_path="ios/Pods",
        kind=ArtifactKind.DEPENDENCY_TREE,
        manifests=("ios/Podfile",),
        locks=("ios/Podfile.lock",),
        marker_globs=(),
        category="project-dependencies",
        restoration=RestorationCost.REDOWNLOAD,
        note="CocoaPods dependencies. Restore with pod install.",
    ),
    Recipe(
        detector_id="ios-build",
        label="iOS build output",
        relative_path="ios/build",
        kind=ArtifactKind.BUILD_OUTPUT,
        manifests=(),
        locks=(),
        marker_globs=("ios/*.xcodeproj", "ios/*.xcworkspace"),
        category="project-build-output",
        restoration=RestorationCost.REBUILD,
        note="Generated Xcode build products. Xcode recreates them on the next build.",
    ),
    Recipe(
        detector_id="android-app-build",
        label="Android app build output",
        relative_path="android/app/build",
        kind=ArtifactKind.BUILD_OUTPUT,
        manifests=("android/app/build.gradle", "android/app/build.gradle.kts"),
        locks=(),
        marker_globs=("android/settings.gradle", "android/settings.gradle.kts"),
        category="project-build-output",
        restoration=RestorationCost.REBUILD,
        note="Generated Gradle build products. Gradle recreates them on the next build.",
    ),
    Recipe(
        detector_id="android-app-cxx",
        label="Android native build output",
        relative_path="android/app/.cxx",
        kind=ArtifactKind.BUILD_OUTPUT,
        manifests=("android/app/build.gradle", "android/app/build.gradle.kts"),
        locks=(),
        marker_globs=("android/settings.gradle", "android/settings.gradle.kts"),
        category="project-build-output",
        restoration=RestorationCost.REBUILD,
        note="Generated native (CMake/NDK) build products.",
    ),
    Recipe(
        detector_id="swiftpm-build",
        label="SwiftPM build output",
        relative_path=".build",
        kind=ArtifactKind.BUILD_OUTPUT,
        manifests=("Package.swift",),
        locks=("Package.resolved",),
        marker_globs=(),
        category="project-build-output",
        restoration=RestorationCost.REBUILD,
        note="Generated Swift Package Manager build products.",
    ),
    Recipe(
        detector_id="rust-target",
        label="Rust target directory",
        relative_path="target",
        kind=ArtifactKind.BUILD_OUTPUT,
        manifests=("Cargo.toml",),
        locks=("Cargo.lock",),
        marker_globs=(),
        category="project-build-output",
        restoration=RestorationCost.REBUILD,
        note="Generated Cargo build products. Rebuild with cargo build.",
    ),
    Recipe(
        detector_id="ruby-bundle",
        label="Bundler vendor/bundle",
        relative_path="vendor/bundle",
        kind=ArtifactKind.DEPENDENCY_TREE,
        manifests=("Gemfile",),
        locks=("Gemfile.lock",),
        marker_globs=(),
        category="project-dependencies",
        restoration=RestorationCost.REDOWNLOAD,
        note="Bundler gems. Restore with bundle install.",
    ),
    Recipe(
        detector_id="python-venv",
        label="Python virtual environment",
        relative_path=".venv",
        kind=ArtifactKind.DEPENDENCY_TREE,
        manifests=("pyproject.toml", "Pipfile"),
        # requirements.txt is deliberately absent: it need not describe an exact
        # reproducible environment, so it must never enable default selection.
        locks=("uv.lock", "poetry.lock", "Pipfile.lock"),
        marker_globs=(),
        category="project-dependencies",
        restoration=RestorationCost.REDOWNLOAD,
        note="Python virtual environment. Recreate from the project's lock file.",
    ),
    Recipe(
        detector_id="python-venv-legacy",
        label="Python virtual environment",
        relative_path="venv",
        kind=ArtifactKind.DEPENDENCY_TREE,
        manifests=("pyproject.toml", "Pipfile"),
        locks=("uv.lock", "poetry.lock", "Pipfile.lock"),
        marker_globs=(),
        category="project-dependencies",
        restoration=RestorationCost.REDOWNLOAD,
        note="Python virtual environment. Recreate from the project's lock file.",
    ),
)


@dataclass(frozen=True)
class ArtifactFact:
    repository: Repository
    recipe: Recipe
    path: Path
    kind: ArtifactKind
    allocated_bytes: int
    lock_satisfied: bool
    lock_detail: str
    manifest_detail: str
    last_activity_at: Optional[datetime]
    inactive: bool


def _is_generated_file(name: str) -> bool:
    if name in GENERATED_FILE_NAMES:
        return True
    return name.endswith(GENERATED_FILE_SUFFIXES)


def _walk_files(
    root: Path, exclude_dir_names: frozenset = frozenset()
) -> Iterator[os.DirEntry]:
    """Yield every non-symlink file entry beneath `root`.

    Shared traversal core: directories whose name is in `exclude_dir_names`
    are pruned from the walk entirely (never descended into or yielded from).
    Symlinks -- both files and directories -- are never followed or yielded,
    matching every other safety check in this module.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(str(current)) as entries:
                children = list(entries)
        except OSError:
            continue
        for entry in children:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in exclude_dir_names:
                        stack.append(Path(entry.path))
                    continue
                yield entry
            except OSError:
                continue


def last_meaningful_activity(
    repo_path: Path, now: Optional[datetime] = None
) -> Optional[datetime]:
    """Newest mtime among files that represent human work on the project.

    Generated directories, Git internals, editor state, and log files are
    excluded, so a nightly CI build or a stray `.DS_Store` cannot make a
    long-abandoned project look active.

    This is a signal about the *source tree*, not about any one artifact:
    `GENERATED_DIR_NAMES` is pruned here on purpose. Callers that need to
    know whether a specific artifact directory (e.g. `node_modules`) has
    itself been touched must use `artifact_recent_activity` instead --
    that function walks the artifact with nothing excluded.
    """
    newest = 0.0
    for entry in _walk_files(repo_path, GENERATED_DIR_NAMES):
        if _is_generated_file(entry.name):
            continue
        try:
            mtime = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if mtime > newest:
            newest = mtime

    if newest <= 0:
        return None
    return datetime.fromtimestamp(newest, timezone.utc)


def artifact_recent_activity(
    artifact_path: Path, threshold: datetime
) -> Optional[datetime]:
    """Return an mtime at or after `threshold` found among the files inside
    `artifact_path`, or None if nothing that recent exists.

    This is the artifact-level recency check: `last_meaningful_activity`
    deliberately excludes directories like `node_modules`, `.venv`, and
    `vendor` from the *source tree's* activity signal, which means a file
    hand-edited today inside one of those directories (a patch-package fix,
    a `pip install -e`, a locally patched gem) is otherwise invisible. Before
    an artifact is offered for deletion -- or deleted -- its own contents
    must be checked directly.

    Not the true maximum mtime: this returns as soon as one qualifying entry
    is found, because only a yes/no judgement is needed. Nothing is excluded
    from this walk (unlike `last_meaningful_activity`): every file inside the
    artifact counts, because the whole artifact is what would be deleted.

    Directory mtimes are deliberately not consulted: a directory's own mtime
    only reflects direct entries being added/removed/renamed, which is
    already implied by (and less precise than) the mtimes of the files that
    change lead to, so checking it separately would add noise without adding
    coverage.
    """
    threshold_ts = threshold.timestamp()
    for entry in _walk_files(artifact_path):
        try:
            mtime = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if mtime >= threshold_ts:
            return datetime.fromtimestamp(mtime, timezone.utc)
    return None


def _first_existing(root: Path, names: Tuple[str, ...]) -> Optional[str]:
    for name in names:
        if (root / name).exists():
            return name
    return None


def _first_matching_glob(root: Path, patterns: Tuple[str, ...]) -> Optional[str]:
    for pattern in patterns:
        for match in root.glob(pattern):
            # A marker only opens the gate for a recipe, so a symlinked one is
            # evidence about somewhere else's project, not this one. Refuse it
            # rather than letting an outside link vouch for a deletion here.
            if match.exists() and not match.is_symlink():
                return pattern
    return None


def analyze_repository(
    repository: Repository, now: Optional[datetime] = None
) -> List[ArtifactFact]:
    """Return every artifact candidate inside one repository.

    The repository root itself is never a candidate: only registered recipe
    subpaths are, and each must be a real directory rather than a symlink.
    """
    moment = now or datetime.now(timezone.utc)
    root = repository.path
    activity = last_meaningful_activity(root, now=moment)
    inactive = activity is not None and activity < moment - timedelta(days=INACTIVITY_DAYS)

    resolved_root = os.path.realpath(str(root))

    facts: List[ArtifactFact] = []
    for recipe in RECIPES:
        artifact = root / recipe.relative_path
        if artifact == root:
            continue
        try:
            if artifact.is_symlink() or not artifact.is_dir():
                continue
        except OSError:
            continue

        # Guard against an ancestor path segment (not the leaf) being a
        # symlink that routes the artifact outside the repository. The leaf
        # check above only inspects the final component, so e.g. `android`
        # being a symlink while `android/app/build` "looks" like a plain
        # directory would otherwise slip through. Resolve fully and require
        # containment by path segments, never a bare string prefix test.
        resolved_artifact = os.path.realpath(str(artifact))
        if resolved_artifact != resolved_root and not resolved_artifact.startswith(
            resolved_root + os.sep
        ):
            continue

        manifest = None
        if recipe.manifests:
            manifest = _first_existing(root, recipe.manifests)
            if manifest is None:
                continue
        marker = None
        if recipe.marker_globs:
            marker = _first_matching_glob(root, recipe.marker_globs)
            if marker is None:
                continue

        lock = _first_existing(root, recipe.locks) if recipe.locks else None
        lock_satisfied = bool(lock) if recipe.locks else True

        size = path_size(artifact)
        if size < MIN_ARTIFACT_BYTES:
            continue

        # The source-tree activity signal deliberately excludes generated
        # directories like this artifact, so a file hand-edited today inside
        # it (a patch-package fix, a `pip install -e`, a locally patched
        # gem) would otherwise be invisible. Check the artifact itself: if
        # anything inside it is newer than the inactivity threshold, the
        # artifact is not stale, regardless of how old the surrounding
        # project looks.
        artifact_inactive = inactive
        artifact_activity = activity
        if inactive:
            threshold = moment - timedelta(days=INACTIVITY_DAYS)
            recent = artifact_recent_activity(artifact, threshold)
            if recent is not None:
                artifact_inactive = False
                if artifact_activity is None or recent > artifact_activity:
                    artifact_activity = recent

        facts.append(
            ArtifactFact(
                repository=repository,
                recipe=recipe,
                path=artifact,
                kind=recipe.kind,
                allocated_bytes=size,
                lock_satisfied=lock_satisfied,
                lock_detail="{} exists".format(lock) if lock else "no supported lock file",
                manifest_detail="{} exists".format(manifest or marker or recipe.relative_path),
                last_activity_at=artifact_activity,
                inactive=artifact_inactive,
            )
        )
    return facts
