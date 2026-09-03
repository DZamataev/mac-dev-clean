from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence, Set, Tuple

MAX_DEPTH = 12

# Directories that never contain a project we should analyse and that are
# expensive or unsafe to walk. Matching is by exact directory name.
PRUNED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".Trash",
        "node_modules",
        "Pods",
        "DerivedData",
        ".build",
        ".gradle",
        ".cxx",
        "build",
        "target",
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
        "Library",
        "Applications",
        ".Spotlight-V100",
        ".fseventsd",
        ".DocumentRevisions-V100",
    }
)

_CLOUD_PLACEHOLDER_SUFFIXES = (".icloud",)


class RepositoryKind(Enum):
    PRIMARY = "primary"
    WORKTREE = "worktree"
    SUBMODULE = "submodule"


@dataclass(frozen=True)
class Repository:
    path: Path
    kind: RepositoryKind
    git_dir: Path


def classify_git_entry(path: Path) -> Optional[Repository]:
    """Classify a candidate directory by inspecting its `.git` entry only.

    Git records enough in the pointer file to tell a linked worktree from a
    submodule, so this needs no subprocess: a worktree points into
    `.git/worktrees/<name>` and a submodule into `.git/modules/<name>`.
    """
    marker = path / ".git"
    try:
        if marker.is_dir() and not marker.is_symlink():
            return Repository(path=path, kind=RepositoryKind.PRIMARY, git_dir=marker)
        if not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None

    if not text.startswith("gitdir:"):
        return None
    pointer = text[len("gitdir:") :].strip()
    if not pointer:
        return None

    git_dir = Path(pointer)
    if not git_dir.is_absolute():
        git_dir = (path / git_dir)
    parts = git_dir.parts
    if "worktrees" in parts:
        kind = RepositoryKind.WORKTREE
    elif "modules" in parts:
        kind = RepositoryKind.SUBMODULE
    else:
        return None
    return Repository(path=path, kind=kind, git_dir=git_dir)


def _is_skippable(name: str) -> bool:
    if name in PRUNED_DIR_NAMES:
        return True
    return name.endswith(_CLOUD_PLACEHOLDER_SUFFIXES)


def discover_repositories(
    roots: Sequence[Path],
    on_progress: Optional[Callable[[str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> Iterator[Repository]:
    """Yield every Git repository beneath `roots`.

    There is no developer-root heuristic: a root is only a traversal boundary.
    Symlinks are never followed, and each (device, inode) pair is visited once
    so overlapping roots and hard-linked trees cannot be double-counted.
    """
    visited: Set[Tuple[int, int]] = set()
    stack = []
    for root in roots:
        expanded = Path(root).expanduser()
        stack.append((expanded, 0))

    while stack:
        if should_cancel is not None and should_cancel():
            return
        current, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        try:
            stat_result = os.stat(str(current), follow_symlinks=False)
        except OSError:
            continue
        if not os.path.isdir(str(current)) or os.path.islink(str(current)):
            continue
        key = (stat_result.st_dev, stat_result.st_ino)
        if key in visited:
            continue
        visited.add(key)

        if on_progress is not None:
            on_progress(str(current))

        repository = classify_git_entry(current)
        if repository is not None:
            yield repository

        try:
            with os.scandir(str(current)) as entries:
                children = list(entries)
        except OSError:
            continue

        for entry in children:
            if _is_skippable(entry.name):
                continue
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            stack.append((Path(entry.path), depth + 1))
