from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

#: Enough to show a tool's real complaint, bounded so a runaway tool cannot
#: fill the UI or the audit journal with megabytes of output.
MAX_STDERR_CHARS = 4000

#: Inventory commands are read-only and must never hang the scan.
DEFAULT_TIMEOUT_SECONDS = 30.0


class ToolUnavailable(RuntimeError):
    """The tool could not be asked at all.

    This is a *fact about the environment*, not a failure of the scan: a
    missing binary, a stopped daemon, or a command that never answered. The
    caller turns it into an `unavailable` row, never into an error dialog and
    never into a prompt to install or start anything.
    """

    def __init__(self, tool: str, reason: str) -> None:
        super().__init__("{}: {}".format(tool, reason))
        self.tool = tool
        self.reason = reason


@dataclass(frozen=True)
class ToolResult:
    argv: Tuple[str, ...]
    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def lines(self) -> List[str]:
        return [line for line in self.stdout.splitlines() if line.strip()]


#: Every analyzer takes one of these so tests can substitute a fake without
#: any monkeypatching of subprocess.
ToolRunner = Callable[[Sequence[str]], ToolResult]


def find_binary(name: str, extra_dirs: Sequence[Path] = ()) -> Optional[str]:
    """Locate an executable, preferring explicitly supplied directories.

    `extra_dirs` exists for tools that are not on PATH by default -- the
    Android command-line tools are the motivating case, since they live under
    a versioned directory inside the SDK.
    """
    for directory in extra_dirs:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    found = shutil.which(name)
    return found


def run_tool(
    argv: Sequence[str], timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> ToolResult:
    """Run an argument vector with no shell interpretation.

    A string argument is refused outright rather than being split: accepting
    one would be the single change that reintroduces shell semantics, and the
    safety model forbids it. Absence and timeout become `ToolUnavailable`; a
    non-zero exit is a normal result the caller inspects.
    """
    if isinstance(argv, (str, bytes)):
        raise TypeError("run_tool requires an argument list, not a command string")

    vector = tuple(str(item) for item in argv)
    if not vector:
        raise ValueError("run_tool requires at least one argument")

    try:
        process = subprocess.run(
            list(vector),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise ToolUnavailable(vector[0], "not installed")
    except PermissionError:
        raise ToolUnavailable(vector[0], "not executable")
    except subprocess.TimeoutExpired:
        raise ToolUnavailable(vector[0], "did not answer within {:g}s".format(timeout))
    except OSError as exc:
        raise ToolUnavailable(vector[0], str(exc))

    return ToolResult(
        argv=vector,
        stdout=process.stdout or "",
        stderr=(process.stderr or "")[:MAX_STDERR_CHARS],
        exit_code=process.returncode,
    )
