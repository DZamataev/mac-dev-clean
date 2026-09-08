from __future__ import annotations

import os
import signal
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, TextIO, Tuple

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


def _drain_text(
    stream: TextIO, chunks: List[str], limit: Optional[int] = None
) -> None:
    retained = 0
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            if limit is None:
                chunks.append(chunk)
            elif retained < limit:
                kept = chunk[: limit - retained]
                chunks.append(kept)
                retained += len(kept)
    finally:
        stream.close()


def find_binary(name: str, extra_dirs: Sequence[Path] = ()) -> Optional[str]:
    """Locate an executable, preferring explicitly supplied directories.

    A name containing a path separator is an explicit path and is validated
    directly; search directories and PATH are only used for bare names.
    `extra_dirs` exists for tools that are not on PATH by default -- the
    Android command-line tools are the motivating case, since they live under
    a versioned directory inside the SDK.
    """
    separators = [os.sep]
    if os.altsep is not None:
        separators.append(os.altsep)
    if any(separator in name for separator in separators):
        explicit = Path(name)
        if explicit.is_file() and os.access(str(explicit), os.X_OK):
            return str(explicit)
        return None

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
        process = subprocess.Popen(
            list(vector),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except FileNotFoundError:
        raise ToolUnavailable(vector[0], "not installed")
    except PermissionError:
        raise ToolUnavailable(vector[0], "not executable")
    except OSError as exc:
        raise ToolUnavailable(vector[0], str(exc))

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_chunks = []  # type: List[str]
    stderr_chunks = []  # type: List[str]
    stdout_thread = threading.Thread(
        target=_drain_text, args=(process.stdout, stdout_chunks)
    )
    stderr_thread = threading.Thread(
        target=_drain_text,
        args=(process.stderr, stderr_chunks, MAX_STDERR_CHARS),
    )
    stdout_thread.start()
    stderr_thread.start()
    deadline = time.monotonic() + timeout

    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
        for thread in (stdout_thread, stderr_thread):
            thread.join(max(0.0, deadline - time.monotonic()))
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            raise subprocess.TimeoutExpired(list(vector), timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            if process.poll() is None:
                process.kill()
        process.wait()
        stdout_thread.join()
        stderr_thread.join()
        raise ToolUnavailable(vector[0], "did not answer within {:g}s".format(timeout))

    return ToolResult(
        argv=vector,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
        exit_code=process.returncode,
    )
