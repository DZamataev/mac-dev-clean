from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    ToolAction,
    normalized_path,
)
from ..scanner import path_size
from .runner import (
    MAX_STDERR_CHARS,
    ToolResult,
    ToolRunner,
    ToolUnavailable,
    inventory_argv_matches,
)

SDK_LIST_ARGV = ("sdkmanager", "--list_installed")
AVD_LIST_ARGV = ("avdmanager", "list", "avd")

_GROUPS = (
    ("ndk;", "android-ndk", "Android NDK"),
    ("cmake;", "android-cmake", "Android CMake"),
    ("system-images;", "android-system-image", "Android system image"),
    ("build-tools;", "android-build-tools", "Android build-tools"),
    ("platforms;", "android-platform", "Android platform"),
    ("cmdline-tools;", "android-cmdline-tools", "Android command-line tools"),
    ("emulator", "android-emulator", "Android emulator"),
)
_GROUP_ORDER = {
    detector_id: index for index, (_, detector_id, _) in enumerate(_GROUPS)
}
_TRUNCATION_MARKER = "...[truncated]"


def _bounded_reason(reason: str) -> str:
    if len(reason) <= MAX_STDERR_CHARS:
        return reason
    retained = MAX_STDERR_CHARS - len(_TRUNCATION_MARKER)
    return reason[:retained] + _TRUNCATION_MARKER


def _result_failure(result: ToolResult, tool: str) -> str:
    stderr = result.stderr.strip()
    if stderr:
        return _bounded_reason(stderr)
    stdout = result.stdout.strip()
    if stdout:
        return _bounded_reason(stdout)
    return _bounded_reason("{} exited with {}".format(tool, result.exit_code))


@dataclass(frozen=True)
class SdkPackage:
    path: str
    version: str
    description: str
    location: str


@dataclass(frozen=True)
class Avd:
    name: str
    path: str
    target: str = ""
    error: str = ""


def find_sdk_root(env: Dict[str, str], home: Path) -> Optional[Path]:
    """Return the first existing SDK root in Android's standard precedence."""
    candidates = []  # type: List[Path]
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        raw = env.get(key)
        if raw:
            try:
                candidate = Path(raw).expanduser()
                if candidate.is_absolute():
                    candidates.append(candidate)
            except (OSError, RuntimeError, ValueError):
                pass
    try:
        candidates.append(Path(home).expanduser() / "Library" / "Android" / "sdk")
    except (OSError, RuntimeError, ValueError):
        return None
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate
        except (OSError, RuntimeError, ValueError):
            continue
    return None


def cmdline_tool_dirs(sdk_root: Path) -> Tuple[Path, ...]:
    """Return deterministic candidate directories for Android CLI binaries."""
    base = Path(sdk_root) / "cmdline-tools"
    directories = [base / "latest" / "bin"]  # type: List[Path]
    try:
        entries = list(base.iterdir())
    except (OSError, RuntimeError, ValueError):
        entries = []
    numeric = []  # type: List[Tuple[Tuple[int, ...], Path]]
    fallback = []  # type: List[Path]
    for entry in entries:
        try:
            if entry.name == "latest" or not entry.is_dir():
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        segments = entry.name.split(".")
        if segments and all(segment.isdigit() for segment in segments):
            numeric.append((tuple(int(segment) for segment in segments), entry))
        else:
            fallback.append(entry)
    numeric.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    fallback.sort(key=lambda entry: entry.name)
    directories.extend(entry / "bin" for _, entry in numeric)
    directories.extend(entry / "bin" for entry in fallback)
    directories.append(Path(sdk_root) / "tools" / "bin")
    return tuple(directories)


def parse_sdk_packages(stdout: str) -> List[SdkPackage]:
    """Parse complete rows from only sdkmanager's installed-package section."""
    packages = []  # type: List[SdkPackage]
    installed = False
    for raw_line in stdout.splitlines():
        line = raw_line.replace("\r", " ").strip()
        if not installed:
            if line == "Installed packages:":
                installed = True
            continue
        if line.endswith(":") and "|" not in line:
            break
        if "|" not in line:
            continue
        columns = [column.strip() for column in line.split("|")]
        if len(columns) != 4 or not all(columns):
            continue
        if columns[0] == "Path" or set(columns[0]) == {"-"}:
            continue
        packages.append(SdkPackage(*columns))
    return packages


def parse_avds(stdout: str) -> Tuple[List[Avd], List[Avd]]:
    """Return avdmanager records as ``(loadable, unloadable)`` lists."""
    loadable = []  # type: List[Avd]
    unloadable = []  # type: List[Avd]
    section = None  # type: Optional[str]
    current = {}  # type: Dict[str, str]
    invalid_record = False

    def flush() -> None:
        nonlocal invalid_record
        name = current.get("Name", "")
        if name and not invalid_record:
            avd = Avd(
                name=name,
                path=current.get("Path", ""),
                target=current.get("Target", ""),
                error=current.get("Error", ""),
            )
            if section == "loadable":
                loadable.append(avd)
            elif section == "unloadable":
                unloadable.append(avd)
        current.clear()
        invalid_record = False

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line == "Available Android Virtual Devices:":
            flush()
            section = "loadable"
            continue
        if "could not be loaded:" in line:
            flush()
            section = "unloadable"
            continue
        if section is None:
            continue
        if line.startswith("Name:"):
            flush()
        for key in ("Name", "Path", "Target", "Error"):
            prefix = key + ":"
            if line.startswith(prefix):
                if key != "Name" and key in current:
                    invalid_record = True
                else:
                    current[key] = line[len(prefix) :].strip()
                break
    flush()
    return loadable, unloadable


def _group_for_package(package_path: str) -> Optional[Tuple[str, str]]:
    if package_path == "emulator":
        return "android-emulator", "Android emulator"
    segments = package_path.split(";")
    if not segments or any(not segment for segment in segments):
        return None
    required_segments = 3 if segments[0] == "system-images" else 2
    if len(segments) < required_segments:
        return None
    if segments[0] != "system-images" and len(segments) != required_segments:
        return None
    for prefix, detector_id, label in _GROUPS:
        if prefix == segments[0] + ";":
            return detector_id, label
    return None


def analyze_android(
    sdk_runner: ToolRunner,
    avd_runner: ToolRunner,
    generation: int,
    sdk_root: Optional[Path],
) -> Tuple[List[Recommendation], Optional[str]]:
    """Inventory Android resources without selecting any removal by default."""
    if sdk_root is None:
        return [], "Android SDK not found"
    try:
        resolved_root = Path(sdk_root).resolve()
    except (OSError, RuntimeError, ValueError):
        return [], "Android SDK not found"

    items = []  # type: List[Recommendation]
    problems = []  # type: List[str]
    try:
        sdk_result = sdk_runner(SDK_LIST_ARGV)
    except ToolUnavailable as exc:
        sdk_result = None
        problems.append(_bounded_reason(exc.reason))
    if (
        sdk_result is not None
        and sdk_result.ok
        and not inventory_argv_matches(sdk_result.argv, SDK_LIST_ARGV)
    ):
        problems.append(
            _bounded_reason(
                "sdkmanager inventory provenance did not match requested command"
            )
        )
        sdk_result = None
    if sdk_result is not None and sdk_result.ok:
        packages = parse_sdk_packages(sdk_result.stdout)
        identity_counts = {}  # type: Dict[str, int]
        for package in packages:
            identity_counts[package.path] = identity_counts.get(package.path, 0) + 1
        for package in packages:
            if identity_counts[package.path] != 1:
                continue
            if not package.path or "\x00" in package.path:
                continue
            group = _group_for_package(package.path)
            if group is None:
                continue
            if not package.location or "\x00" in package.location:
                continue
            relative = Path(package.location)
            if relative.is_absolute():
                continue
            try:
                location = (resolved_root / relative).resolve()
                location.relative_to(resolved_root)
            except (OSError, RuntimeError, ValueError):
                continue
            if not location.is_dir():
                continue
            size = path_size(location)
            if size <= 0:
                continue
            detector_id, label = group
            items.append(
                Recommendation(
                    detector_id=detector_id,
                    category="tool-managed",
                    label="{} {}".format(label, package.version),
                    path=location,
                    action=ActionKind.INVOKE_TOOL,
                    allocated_bytes=size,
                    reclaimable_bytes=size,
                    confidence=Confidence.STRONG,
                    restoration=RestorationCost.REDOWNLOAD,
                    selected_by_default=False,
                    evidence=(
                        Evidence("tool-report", package.description),
                        Evidence("preview-command", " ".join(SDK_LIST_ARGV)),
                    ),
                    safety_root=resolved_root,
                    reason=(
                        "Installed SDK component. A project not covered by this "
                        "scan may require it."
                    ),
                    generation=generation,
                    warning=(
                        "Removing a toolchain version breaks any project pinned to it."
                    ),
                    tool_action=ToolAction(
                        tool="sdkmanager",
                        resource=package.path,
                        argv=("sdkmanager", "--uninstall", package.path),
                        preview_argv=SDK_LIST_ARGV,
                        reported=package.version,
                    ),
                )
            )
        items.sort(
            key=lambda item: (
                _GROUP_ORDER[item.detector_id],
                item.tool_action.resource if item.tool_action is not None else "",
            )
        )
    elif sdk_result is not None:
        problems.append(_result_failure(sdk_result, "sdkmanager"))
    try:
        avd_result = avd_runner(AVD_LIST_ARGV)
    except ToolUnavailable as exc:
        avd_result = None
        problems.append(_bounded_reason(exc.reason))
    if (
        avd_result is not None
        and avd_result.ok
        and not inventory_argv_matches(avd_result.argv, AVD_LIST_ARGV)
    ):
        problems.append(
            _bounded_reason(
                "avdmanager inventory provenance did not match requested command"
            )
        )
        avd_result = None
    if avd_result is not None and avd_result.ok:
        loadable, unloadable = parse_avds(avd_result.stdout)
        avd_counts = {}  # type: Dict[str, int]
        for avd in loadable + unloadable:
            avd_counts[avd.name] = avd_counts.get(avd.name, 0) + 1
        ordered_avds = [
            (entry, False) for entry in sorted(loadable, key=lambda item: item.name)
        ]
        ordered_avds += [
            (entry, True) for entry in sorted(unloadable, key=lambda item: item.name)
        ]
        for avd, broken in ordered_avds:
            if avd_counts[avd.name] != 1:
                continue
            if not avd.name or "\x00" in avd.name:
                continue
            if not avd.path or "\x00" in avd.path:
                continue
            location = Path(avd.path)
            if not location.is_absolute():
                continue
            size = path_size(location)
            detector_id = "android-avd-broken" if broken else "android-avd"
            label = "Android virtual device {}".format(avd.name)
            if broken:
                label += " (will not load)"
            report = avd.error if broken else avd.target
            items.append(
                Recommendation(
                    detector_id=detector_id,
                    category="tool-managed",
                    label=label,
                    path=location,
                    action=ActionKind.INVOKE_TOOL,
                    allocated_bytes=size,
                    reclaimable_bytes=size,
                    confidence=(
                        Confidence.HEURISTIC if broken else Confidence.STRONG
                    ),
                    restoration=RestorationCost.EXTERNAL_STATE,
                    selected_by_default=False,
                    evidence=(
                        Evidence("tool-report", report or "no details reported"),
                        Evidence("preview-command", " ".join(AVD_LIST_ARGV)),
                    ),
                    safety_root=location.parent,
                    reason=(
                        "This device does not load. It may only need a missing SDK "
                        "component rather than deletion."
                        if broken
                        else "Emulator device. Its installed apps and data are deleted with it."
                    ),
                    generation=generation,
                    warning="Device state inside the AVD cannot be recovered.",
                    tool_action=ToolAction(
                        tool="avdmanager",
                        resource=avd.name,
                        argv=("avdmanager", "delete", "avd", "-n", avd.name),
                        preview_argv=AVD_LIST_ARGV,
                        reported=report,
                    ),
                )
            )
    elif avd_result is not None:
        problems.append(_result_failure(avd_result, "avdmanager"))
    location_counts = {}  # type: Dict[Tuple[str, str], int]
    for item in items:
        key = (item.detector_id, normalized_path(item.path))
        location_counts[key] = location_counts.get(key, 0) + 1
    items = [
        item
        for item in items
        if location_counts[(item.detector_id, normalized_path(item.path))] == 1
    ]
    combined = _bounded_reason("; ".join(problems)) if problems else None
    return items, combined
