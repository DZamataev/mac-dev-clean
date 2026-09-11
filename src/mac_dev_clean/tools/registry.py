from __future__ import annotations

import os
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..ndk_usage import NdkUsageSnapshot
from ..recommendation import Recommendation
from ..sim_prune import SimctlError
from .android import analyze_android, cmdline_tool_dirs, find_sdk_root
from .docker import analyze_docker
from .homebrew import analyze_homebrew
from .runner import (
    MAX_STDERR_CHARS,
    ToolResult,
    ToolRunner,
    ToolUnavailable,
    find_binary,
    inventory_argv_matches,
    run_tool,
)
from .simulator import (
    SIMCTL_DEVICES_PREVIEW_ARGV,
    SIMCTL_RUNTIMES_PREVIEW_ARGV,
    XCRUN,
    analyze_simulator,
)


_STANDARD_BINARY_DIRS = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
_TRUNCATION_MARKER = "...[truncated]"
Analyzer = Callable[[], Tuple[List[Recommendation], Optional[str]]]


@dataclass(frozen=True)
class ToolStatus:
    tool: str
    available: bool
    reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "tool": self.tool,
            "available": self.available,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ToolReport:
    recommendations: List[Recommendation] = field(default_factory=list)
    statuses: List[ToolStatus] = field(default_factory=list)


def _bounded_reason(reason: str) -> str:
    if len(reason) <= MAX_STDERR_CHARS:
        return reason
    retained = MAX_STDERR_CHARS - len(_TRUNCATION_MARKER)
    return reason[:retained] + _TRUNCATION_MARKER


def _exception_reason(exc: Exception) -> str:
    if isinstance(exc, ToolUnavailable):
        return _bounded_reason(exc.reason)
    return _bounded_reason(str(exc) or exc.__class__.__name__)


def binary_runner(
    name: str,
    extra_dirs: Tuple[Path, ...] = (),
    allow_fallback: bool = True,
    containment_root: Optional[Path] = None,
) -> ToolRunner:
    """Return a runner permanently bound to one declared executable.

    ``allow_fallback=False`` confines resolution to ``extra_dirs`` and requires
    the resolved executable to remain under ``containment_root``. Apply uses
    that mode for sdkmanager so a stale recommendation cannot fall through to
    another SDK on PATH or through an escaping symlink.
    """
    if not isinstance(name, str):
        raise TypeError("binary name must be a string")
    if not name:
        raise ValueError("binary name must not be empty")
    if "\x00" in name:
        raise ValueError("binary name must not contain NUL")
    if not allow_fallback and containment_root is None:
        raise ValueError("strict binary resolution requires a containment root")

    search_dirs = tuple(extra_dirs) + (_STANDARD_BINARY_DIRS if allow_fallback else ())
    resolved_root = (
        Path(containment_root).resolve() if containment_root is not None else None
    )

    def resolve() -> Optional[str]:
        if allow_fallback:
            return find_binary(name, extra_dirs=search_dirs)
        for directory in search_dirs:
            candidate = Path(directory) / name
            try:
                resolved_candidate = candidate.resolve(strict=True)
                if resolved_root is None:
                    continue
                resolved_candidate.relative_to(resolved_root)
                if resolved_candidate.is_file() and os.access(
                    str(resolved_candidate), os.X_OK
                ):
                    # Execute the path whose containment was checked. Reusing
                    # the lexical symlink would reopen a swap between the
                    # check above and process creation.
                    return str(resolved_candidate)
            except (OSError, RuntimeError, ValueError):
                continue
        return None

    def run(argv: Sequence[str]):
        if isinstance(argv, (str, bytes)) or not isinstance(argv, SequenceABC):
            raise TypeError("tool runner requires an argument sequence")
        vector = tuple(argv)
        if not vector:
            raise ValueError("tool runner requires at least one argument")
        if any(not isinstance(argument, str) for argument in vector):
            raise TypeError("tool runner arguments must be strings")
        if any("\x00" in argument for argument in vector):
            raise ValueError("tool runner arguments must not contain NUL")
        if vector[0] != name:
            raise ValueError("tool runner executable does not match declared name")

        resolved = resolve()
        if resolved is None:
            raise ToolUnavailable(name, "not installed")
        return run_tool((resolved,) + vector[1:])

    return run


def _simctl_inventory_adapter(runner: ToolRunner):
    """Adapt sim_prune's legacy relative arguments to hardened full vectors."""

    def run(args: Sequence[str]) -> str:
        if isinstance(args, (str, bytes)) or not isinstance(args, SequenceABC):
            raise SimctlError("malformed simctl inventory arguments")
        relative = tuple(args)
        if any(
            not isinstance(argument, str) or "\x00" in argument
            for argument in relative
        ):
            raise SimctlError("malformed simctl inventory arguments")
        full_argv = (XCRUN, "simctl") + relative
        if full_argv not in (
            SIMCTL_DEVICES_PREVIEW_ARGV,
            SIMCTL_RUNTIMES_PREVIEW_ARGV,
        ):
            raise SimctlError("unexpected simctl inventory command")

        result = runner(full_argv)
        if not isinstance(result, ToolResult):
            raise SimctlError("malformed simctl inventory result")
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip()
            reason = detail or "simctl exited with {}".format(result.exit_code)
            raise SimctlError(_bounded_reason(reason))
        if not inventory_argv_matches(result.argv, full_argv):
            raise SimctlError(
                "simctl inventory provenance did not match requested command"
            )
        if not isinstance(result.stdout, str):
            raise SimctlError("malformed simctl inventory stdout")
        return result.stdout

    return run


def collect_tool_recommendations(
    generation: int,
    home: Path,
    env: Optional[Dict[str, str]] = None,
    runners: Optional[Dict[str, ToolRunner]] = None,
    ndk_usage_snapshot: Optional[NdkUsageSnapshot] = None,
) -> ToolReport:
    """Collect every tool independently without starting or installing it."""
    environment = dict(os.environ) if env is None else dict(env)
    supplied = {} if runners is None else runners
    sdk_root = find_sdk_root(environment, home)
    android_dirs = cmdline_tool_dirs(sdk_root) if sdk_root is not None else ()

    def runner_for(name: str, extra_dirs: Tuple[Path, ...] = ()) -> ToolRunner:
        if name in supplied:
            return supplied[name]
        return binary_runner(name, extra_dirs=extra_dirs)

    recommendations = []  # type: List[Recommendation]
    statuses = []  # type: List[ToolStatus]

    analyzers = (
        (
            "docker",
            lambda: analyze_docker(
                runner_for("docker"), generation=generation, home=home
            ),
        ),
        (
            "homebrew",
            lambda: analyze_homebrew(
                runner_for("brew"), generation=generation, home=home
            ),
        ),
        (
            "android",
            lambda: analyze_android(
                sdk_runner=runner_for("sdkmanager", android_dirs),
                avd_runner=runner_for("avdmanager", android_dirs),
                generation=generation,
                sdk_root=sdk_root,
                ndk_usage_snapshot=ndk_usage_snapshot,
            ),
        ),
        (
            "simulator",
            lambda: analyze_simulator(
                _simctl_inventory_adapter(
                    runner_for("simctl")
                    if "simctl" in supplied
                    else binary_runner(XCRUN)
                ),
                generation=generation,
                home=home,
            ),
        ),
    )  # type: Tuple[Tuple[str, Analyzer], ...]

    for tool, analyzer in analyzers:
        try:
            items, reason = analyzer()
        except Exception as exc:
            items, reason = [], _exception_reason(exc)
        bounded = _bounded_reason(reason) if reason is not None else None
        recommendations.extend(items)
        statuses.append(ToolStatus(tool, bounded is None, bounded or ""))

    return ToolReport(recommendations=recommendations, statuses=statuses)
