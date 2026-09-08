from __future__ import annotations

import os
import signal
import stat
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

from mac_dev_clean.tools.runner import (
    MAX_STDERR_CHARS,
    ToolResult,
    ToolUnavailable,
    find_binary,
    run_tool,
)


def write_script(directory: Path, name: str, body: str) -> Path:
    script = directory / name
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RunToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_captures_stdout_and_reports_ok(self):
        script = write_script(self.dir, "hello", 'echo "line one"\n')

        result = run_tool([str(script)])

        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("line one", result.stdout)

    def test_non_zero_exit_is_not_ok_and_keeps_stderr(self):
        script = write_script(self.dir, "boom", 'echo "went wrong" >&2\nexit 3\n')

        result = run_tool([str(script)])

        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 3)
        self.assertIn("went wrong", result.stderr)

    def test_non_utf8_stdout_and_stderr_are_decoded_with_replacement(self):
        result = run_tool(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.stdout.buffer.write(b'out\\xffdone'); "
                    "sys.stderr.buffer.write(b'err\\xfedone')"
                ),
            ]
        )

        self.assertEqual(result.stdout, "out\ufffddone")
        self.assertEqual(result.stderr, "err\ufffddone")

    def test_stderr_is_bounded(self):
        script = write_script(
            self.dir, "loud", 'python3 -c "import sys; sys.stderr.write(\'x\' * 100000)"\n'
        )

        result = run_tool([str(script)])

        self.assertLessEqual(len(result.stderr), MAX_STDERR_CHARS)

    def test_stderr_collection_does_not_buffer_entire_stream(self):
        payload_size = 2_000_000

        tracemalloc.start()
        try:
            result = run_tool(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stderr.write('x' * {})".format(payload_size),
                ]
            )
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(len(result.stderr), MAX_STDERR_CHARS)
        self.assertLess(peak_bytes, payload_size)

    def test_missing_binary_raises_tool_unavailable(self):
        with self.assertRaises(ToolUnavailable) as raised:
            run_tool([str(self.dir / "does-not-exist")])

        self.assertIn("does-not-exist", str(raised.exception))

    def test_non_executable_binary_raises_tool_unavailable(self):
        script = write_script(self.dir, "not-executable", "echo no\n")
        script.chmod(stat.S_IRUSR | stat.S_IWUSR)

        with self.assertRaises(ToolUnavailable) as raised:
            run_tool([str(script)])

        self.assertEqual(raised.exception.reason, "not executable")

    def test_result_keeps_the_argv_it_ran(self):
        script = write_script(self.dir, "argv", "echo ok\n")

        result = run_tool([str(script), "--flag", "value"])

        self.assertEqual(result.argv, (str(script), "--flag", "value"))

    def test_a_string_command_is_refused(self):
        script = write_script(self.dir, "shellish", "echo ok\n")

        with self.assertRaises(TypeError):
            run_tool(str(script))

    def test_empty_argv_is_refused(self):
        with self.assertRaises(ValueError):
            run_tool([])

    def test_timeout_raises_tool_unavailable(self):
        script = write_script(self.dir, "slow", "sleep 5\n")

        with self.assertRaises(ToolUnavailable):
            run_tool([str(script)], timeout=0.2)

    def test_timeout_terminates_descendant_process_group(self):
        pid_file = self.dir / "child.pid"
        script = write_script(
            self.dir,
            "process-tree",
            'sleep 30 >/dev/null 2>&1 &\necho "$!" > "$1"\nsleep 30\n',
        )
        child_pid = None

        try:
            with self.assertRaises(ToolUnavailable):
                run_tool([str(script), str(pid_file)], timeout=1.0)

            child_pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2.0
            while process_exists(child_pid) and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertFalse(process_exists(child_pid))
        finally:
            if child_pid is None and pid_file.exists():
                child_pid = int(pid_file.read_text(encoding="utf-8"))
            if child_pid is not None and process_exists(child_pid):
                os.kill(child_pid, signal.SIGKILL)


class FindBinaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_finds_a_binary_on_path(self):
        write_script(self.dir, "widget", "echo ok\n")
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.dir) + os.pathsep + original
        self.addCleanup(os.environ.__setitem__, "PATH", original)

        self.assertIsNotNone(find_binary("widget"))

    def test_finds_a_binary_in_an_extra_directory(self):
        write_script(self.dir, "gadget", "echo ok\n")

        found = find_binary("gadget", extra_dirs=(self.dir,))

        self.assertIsNotNone(found)
        self.assertTrue(str(found).endswith("gadget"))

    def test_missing_binary_returns_none(self):
        self.assertIsNone(find_binary("definitely-not-installed-xyz"))

    def test_a_non_executable_file_is_not_a_binary(self):
        (self.dir / "plain").write_text("not executable", encoding="utf-8")

        self.assertIsNone(find_binary("plain", extra_dirs=(self.dir,)))

    def test_explicit_paths_are_validated_directly(self):
        executable = write_script(self.dir, "explicit", "echo ok\n")
        non_executable = self.dir / "plain-explicit"
        non_executable.write_text("not executable", encoding="utf-8")
        extra_dir = self.dir / "extra"
        nested_dir = extra_dir / "nested"
        nested_dir.mkdir(parents=True)
        write_script(nested_dir, "tool", "echo wrong\n")

        self.assertEqual(
            find_binary(str(executable), extra_dirs=(extra_dir,)), str(executable)
        )
        self.assertIsNone(find_binary(str(self.dir / "missing"), extra_dirs=(extra_dir,)))
        self.assertIsNone(find_binary(str(non_executable), extra_dirs=(extra_dir,)))
        self.assertIsNone(find_binary("nested/tool", extra_dirs=(extra_dir,)))


class ToolResultTests(unittest.TestCase):
    def test_ok_is_false_for_non_zero_exit(self):
        result = ToolResult(argv=("x",), stdout="", stderr="", exit_code=1)

        self.assertFalse(result.ok)

    def test_lines_skips_blank_lines(self):
        result = ToolResult(argv=("x",), stdout="a\n\nb\n", stderr="", exit_code=0)

        self.assertEqual(result.lines(), ["a", "b"])


class PackageExportTests(unittest.TestCase):
    def test_max_stderr_chars_is_available_from_tools_package(self):
        from mac_dev_clean.tools import MAX_STDERR_CHARS as package_limit

        self.assertEqual(package_limit, MAX_STDERR_CHARS)


if __name__ == "__main__":
    unittest.main()
