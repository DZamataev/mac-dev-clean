from __future__ import annotations

import os
import stat
import tempfile
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

    def test_stderr_is_bounded(self):
        script = write_script(
            self.dir, "loud", 'python3 -c "import sys; sys.stderr.write(\'x\' * 100000)"\n'
        )

        result = run_tool([str(script)])

        self.assertLessEqual(len(result.stderr), MAX_STDERR_CHARS)

    def test_missing_binary_raises_tool_unavailable(self):
        with self.assertRaises(ToolUnavailable) as raised:
            run_tool([str(self.dir / "does-not-exist")])

        self.assertIn("does-not-exist", str(raised.exception))

    def test_result_keeps_the_argv_it_ran(self):
        script = write_script(self.dir, "argv", "echo ok\n")

        result = run_tool([str(script), "--flag", "value"])

        self.assertEqual(result.argv, (str(script), "--flag", "value"))

    def test_a_string_command_is_refused(self):
        script = write_script(self.dir, "shellish", "echo ok\n")

        with self.assertRaises(TypeError):
            run_tool(str(script))

    def test_timeout_raises_tool_unavailable(self):
        script = write_script(self.dir, "slow", "sleep 5\n")

        with self.assertRaises(ToolUnavailable):
            run_tool([str(script)], timeout=0.2)


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


class ToolResultTests(unittest.TestCase):
    def test_ok_is_false_for_non_zero_exit(self):
        result = ToolResult(argv=("x",), stdout="", stderr="", exit_code=1)

        self.assertFalse(result.ok)

    def test_lines_skips_blank_lines(self):
        result = ToolResult(argv=("x",), stdout="a\n\nb\n", stderr="", exit_code=0)

        self.assertEqual(result.lines(), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
