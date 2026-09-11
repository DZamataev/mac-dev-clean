from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Tuple

from .discovery import Repository

_EXACT_FILES = (
    "build.gradle",
    "build.gradle.kts",
    "gradle.properties",
    "local.properties",
    "android/build.gradle",
    "android/build.gradle.kts",
    "android/gradle.properties",
    "android/local.properties",
    "android/app/build.gradle",
    "android/app/build.gradle.kts",
)
_MAX_FILE_CHARACTERS = 256 * 1024
_NATIVE_MARKER_FILES = (
    "Android.mk",
    "Application.mk",
    "android/CMakeLists.txt",
    "android/Android.mk",
    "android/Application.mk",
    "android/app/src/main/cpp/CMakeLists.txt",
    "android/app/src/main/jni/Android.mk",
    "android/app/src/main/jni/Application.mk",
)
_NATIVE_MARKER_DIRECTORIES = ("android/.cxx", "android/app/.cxx")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_NDK_VERSION_MARKER_RE = re.compile(r"\bndkVersion\b")
_EXTERNAL_NATIVE_BUILD_RE = re.compile(r"\bexternalNativeBuild\s*\{")
_NDK_BLOCK_RE = re.compile(r"\bndk\s*\{")
_EXPRESSION_CONTINUATION_KEYWORDS = frozenset(("as", "in", "instanceof"))
_EXPRESSION_CONTINUATION_OPERATOR_STARTS = frozenset("+-*/%<>=!&|^?.[:,")
_SLASHY_EXPRESSION_KEYWORDS = frozenset(
    ("assert", "case", "in", "return", "throw", "yield")
)


@dataclass(frozen=True)
class ProjectNdkUsage:
    project_path: Path
    version: Optional[str]
    evidence: Tuple[str, ...]


@dataclass(frozen=True)
class NdkUsageSnapshot:
    completed_at: datetime
    usages: Tuple[ProjectNdkUsage, ...]


def _candidate_paths(root: Path) -> Iterable[Path]:
    candidates = [
        root / relative_path for relative_path in _EXACT_FILES + _NATIVE_MARKER_FILES
    ]
    android = root / "android"
    if android.is_dir() and not android.is_symlink():
        for child in android.iterdir():
            if child.is_dir() and not child.is_symlink():
                candidates.extend((child / "build.gradle", child / "build.gradle.kts"))
    return tuple(dict.fromkeys(candidates))


def _has_symlink_segment(root: Path, candidate: Path) -> bool:
    current = root
    for segment in candidate.relative_to(root).parts:
        current = current / segment
        if current.is_symlink():
            return True
    return False


def _read_supported_file(root: Path, candidate: Path) -> Optional[Tuple[str, bool]]:
    if _has_symlink_segment(root, candidate) or not candidate.is_file():
        return None
    with candidate.open("r", encoding="utf-8", errors="replace") as stream:
        bounded = stream.read(_MAX_FILE_CHARACTERS + 1)
    return bounded[:_MAX_FILE_CHARACTERS], len(bounded) > _MAX_FILE_CHARACTERS


def _gradle_code_mask(text: str) -> str:
    """Keep Gradle code positions while blanking comments and strings."""
    masked = list(text)
    index = 0
    while index < len(text):
        if text.startswith("$/", index):
            end = _dollar_slashy_end(text, index)
        elif text.startswith("//", index):
            end = text.find("\n", index + 2)
            end = len(text) if end == -1 else end
        elif text.startswith("/*", index):
            close = text.find("*/", index + 2)
            end = len(text) if close == -1 else close + 2
        elif text[index] in ("\"", "'"):
            quote = text[index]
            delimiter = quote * 3 if text.startswith(quote * 3, index) else quote
            cursor = index + len(delimiter)
            while cursor < len(text):
                if text.startswith(delimiter, cursor):
                    cursor += len(delimiter)
                    break
                if len(delimiter) == 1 and text[cursor] == "\\":
                    cursor += 2
                else:
                    cursor += 1
            end = min(cursor, len(text))
        elif text[index] == "/" and _starts_slashy_string(text, index):
            end = _slashy_end(text, index)
        else:
            index += 1
            continue
        for position in range(index, end):
            if masked[position] not in "\r\n":
                masked[position] = " "
        index = end
    return "".join(masked)


def _dollar_slashy_end(text: str, start: int) -> int:
    """Return the end of a dollar-slashy string, honoring dollar escapes."""
    cursor = start + 2
    while cursor < len(text):
        if text.startswith("/$$", cursor):
            cursor += 3
        elif text.startswith("/$", cursor):
            return cursor + 2
        elif text.startswith(("$$", "$/"), cursor):
            cursor += 2
        else:
            cursor += 1
    return len(text)


def _slashy_end(text: str, start: int) -> int:
    """Return the end of a slashy string, honoring backslash escapes."""
    cursor = start + 1
    while cursor < len(text):
        if text[cursor] == "\\":
            cursor = min(cursor + 2, len(text))
        elif text[cursor] == "/":
            return cursor + 1
        else:
            cursor += 1
    return len(text)


def _previous_code_token(text: str, index: int) -> str:
    cursor = index - 1
    while cursor >= 0 and text[cursor].isspace():
        cursor -= 1
    if cursor < 0:
        return ""
    if text[cursor].isalnum() or text[cursor] in "_$":
        end = cursor + 1
        while cursor >= 0 and (text[cursor].isalnum() or text[cursor] in "_$"):
            cursor -= 1
        return text[cursor + 1 : end]
    return text[cursor]


def _starts_slashy_string(text: str, index: int) -> bool:
    """Distinguish conservative slashy expression starts from division."""
    previous = _previous_code_token(text, index)
    return (
        not previous
        or previous in "=([{,:;!?&|+*-%^~<>"
        or previous in _SLASHY_EXPRESSION_KEYWORDS
    )


def _is_expression_continuation_token(code: str, start: int) -> bool:
    if start >= len(code):
        return False
    if code[start] in _EXPRESSION_CONTINUATION_OPERATOR_STARTS:
        return True
    match = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", code[start:])
    return bool(match and match.group(0) in _EXPRESSION_CONTINUATION_KEYWORDS)


def _has_multiline_expression_continuation(code: str, start: int) -> bool:
    suffix = code[start:]
    cursor = 0
    while cursor < len(suffix) and suffix[cursor] in " \t":
        cursor += 1
    if cursor >= len(suffix) or suffix[cursor] not in "\r\n":
        return False
    while cursor < len(suffix) and suffix[cursor].isspace():
        cursor += 1
    return _is_expression_continuation_token(suffix, cursor)


def _quoted_value(text: str, start: int) -> Optional[Tuple[str, int]]:
    if start >= len(text) or text[start] not in ("\"", "'"):
        return None
    quote = text[start]
    cursor = start + 1
    while cursor < len(text):
        if text[cursor] == "\\":
            cursor += 2
        elif text[cursor] == quote:
            return text[start + 1 : cursor], cursor + 1
        else:
            cursor += 1
    return None


def _gradle_literal_declarations(
    text: str, code: str
) -> Iterable[Tuple[str, str]]:
    for match in _NDK_VERSION_MARKER_RE.finditer(code):
        cursor = match.end()
        while cursor < len(text) and text[cursor] in " \t":
            cursor += 1
        if cursor < len(text) and text[cursor] == "=":
            cursor += 1
            while cursor < len(text) and text[cursor] in " \t":
                cursor += 1
        quoted = _quoted_value(text, cursor)
        if quoted is None:
            continue
        value, end = quoted
        suffix = code[end:]
        terminator = re.match(r"[ \t]*(?:[;}\r\n]|$)", suffix)
        if terminator and not _has_multiline_expression_continuation(code, end):
            yield "ndkVersion", value.strip()


def _properties_declarations(text: str) -> Iterable[Tuple[str, str]]:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "!")):
            continue
        if stripped.startswith("android.ndkVersion="):
            yield "android.ndkVersion", stripped.split("=", 1)[1].strip()
        elif stripped.startswith("ndk.dir="):
            path = stripped.split("=", 1)[1].strip().rstrip("/\\")
            yield "ndk.dir", re.split(r"[/\\]", path)[-1]


def analyze_project_ndk_usage(repository: Repository) -> Optional[ProjectNdkUsage]:
    root = repository.path
    declarations = []
    native_evidence = set()
    for candidate in _candidate_paths(root):
        read_result = _read_supported_file(root, candidate)
        if read_result is None:
            continue
        text, was_truncated = read_result
        relative_path = candidate.relative_to(root).as_posix()
        if relative_path in _NATIVE_MARKER_FILES:
            native_evidence.add("{0}:native-metadata".format(relative_path))
        is_gradle = candidate.name in ("build.gradle", "build.gradle.kts")
        code = _gradle_code_mask(text) if is_gradle else text
        if is_gradle:
            file_declarations = list(_gradle_literal_declarations(text, code))
            if was_truncated:
                file_declarations = []
        else:
            file_declarations = list(_properties_declarations(text))
        if is_gradle and _NDK_VERSION_MARKER_RE.search(code) and not file_declarations:
            native_evidence.add("{0}:ndkVersion".format(relative_path))
        if is_gradle and _EXTERNAL_NATIVE_BUILD_RE.search(code):
            native_evidence.add("{0}:externalNativeBuild".format(relative_path))
        if is_gradle and _NDK_BLOCK_RE.search(code):
            native_evidence.add("{0}:ndk".format(relative_path))
        for declaration_kind, version in file_declarations:
            evidence = "{0}:{1}".format(relative_path, declaration_kind)
            native_evidence.add(evidence)
            if _VERSION_RE.fullmatch(version):
                declarations.append((version, evidence))

    for relative_path in _NATIVE_MARKER_DIRECTORIES:
        marker = root / relative_path
        if (
            not _has_symlink_segment(root, marker)
            and marker.is_dir()
            and not marker.is_symlink()
        ):
            native_evidence.add("{0}:native-metadata".format(relative_path))

    if not native_evidence:
        return None
    versions = {version for version, _ in declarations}
    version = next(iter(versions)) if len(versions) == 1 else None
    return ProjectNdkUsage(
        project_path=root,
        version=version,
        evidence=tuple(sorted(native_evidence)),
    )
