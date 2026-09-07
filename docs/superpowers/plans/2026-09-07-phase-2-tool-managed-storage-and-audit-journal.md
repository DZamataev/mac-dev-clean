# Phase 2: Tool-managed Storage and the Audit Journal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let mac-dev-clean report and reclaim storage that belongs to Docker, Homebrew, the Android SDK, and the iOS simulator by asking each tool for its own inventory and acting only through that tool's supported commands, with every action — including refusals and dry runs — recorded in a local audit journal.

**Architecture:** A new `tools/` package holds one analyzer per external tool behind a shared `ToolRunner` seam, so every tool is testable with a fake runner and no tool is ever started by us. Each analyzer emits `Recommendation` objects with the existing `ActionKind.INVOKE_TOOL`, carrying a frozen argument vector. A new `journal.py` sits under every action path — legacy `clean`, item-level `apply`, and the new tool executor — and appends one NDJSON record per attempt before the outcome is returned.

**Tech Stack:** Python 3.9+ stdlib only (`subprocess`, `json`, `unittest`), Swift 6 / SwiftUI (macOS 14+), `swift-testing`.

**Spec:** `docs/superpowers/specs/2026-09-03-universal-storage-analyzer-design.md`

## Global Constraints

- Python 3.9 is the floor (`pyproject.toml` `requires-python = ">=3.9"`). Every new module starts with `from __future__ import annotations`. Never use `X | Y` unions, `match`, or built-in generics like `dict[str, int]` at runtime.
- Zero third-party Python dependencies. `pyproject.toml` has no `dependencies` key and must keep none.
- Python tests run with `PYTHONPATH=src python3 -m unittest discover -s tests` and must pass on 3.9 and 3.13.
- Swift tests run with `swift test --package-path macos`.
- `./scripts/audit_public_repo.sh` must pass. It greps every tracked **and untracked** file for absolute home paths and fails on any that is not under the allowlisted `test`, `example`, or `<local-user>` user names. **The allowlist requires a following path separator**, so a user directory with nothing after it is still rejected — always write a further segment, e.g. `/Users/test/home` or `/Users/test/app`. This applies to fixtures, docstrings, tests, and this plan itself.
- **Every new Python module must be added to BOTH `inputFiles` and `outputFiles` in `macos/project.yml`, then `xcodegen generate` must be re-run.** The post-build script copies `*.py` by glob, so a missing entry still builds under `swift test` but fails `xcodebuild` with a stale-output error. This plan's Task 12 does it once for all new modules; do not skip it.
- No external tool is ever started, woken, installed, or prompted for. Detection is "does this binary answer right now"; a `No` is a fact, not an error.
- No action is executed through a shell. `subprocess.run` always receives a list, never a string, and never `shell=True`.
- Destructive tests call exported functions with explicit temporary roots and fake runners. Never invoke `main()` against a real home directory, and never let a test execute a real `docker`, `brew`, `sdkmanager`, or `avdmanager` mutation.
- Existing public names that must keep working unchanged: `mac_dev_clean.scanner.scan`, `mac_dev_clean.cleaner.clean_targets`, `mac_dev_clean.model.ScanTarget`, `mac_dev_clean.executor.apply_recommendations`, and the `scan --json` / `report --json` / `clean` / `deep-scan` / `apply` CLI contracts.
- Reuse `mac_dev_clean.model.human_bytes` for display strings and `mac_dev_clean.recommendation.Recommendation` for results. Do not write a second byte formatter or a second result type.

---

## Verified Platform Facts

Confirmed by running the real tools on macOS 26.5.2 on 2026-09-07. Do not re-derive; do not design around contradicting assumptions.

1. **`docker system df --format '{{json .}}'` emits one JSON object per line**, with string values throughout:
   ```
   {"Active":"4","Reclaimable":"15.03GB (68%)","Size":"22.03GB","TotalCount":"18","Type":"Images"}
   {"Active":"2","Reclaimable":"106.5kB (0%)","Size":"62.55MB","TotalCount":"4","Type":"Containers"}
   {"Active":"6","Reclaimable":"0B (0%)","Size":"1.133GB","TotalCount":"6","Type":"Local Volumes"}
   {"Active":"0","Reclaimable":"7.499GB","Size":"18.92GB","TotalCount":"144","Type":"Build Cache"}
   ```
   Note: `Build Cache` has **no** percentage suffix while the others do; `TotalCount` and `Active` are strings, not numbers; units are mixed-case SI (`kB`, `GB`).

2. **`brew cleanup -n` writes `Warning:` lines before the removals** and ends with a total line:
   ```
   Warning: Skipping aom: most recent version 3.15.0 not installed
   Would remove: /opt/homebrew/Library/Homebrew/vendor/portable-ruby/4.0.3 (1,704 files, 34.6MB)
   ==> This operation would free approximately 799.8MB of disk space.
   ```
   The measured total was 799.8MB while `~/Library/Caches/Homebrew` held only 64 MiB, because most removable storage is old Cellar versions.

3. **`sdkmanager --list_installed` writes progress output before the table.** Lines such as `Loading package information...` and `[=====] 25% Loading local repository...` are separated by carriage returns and precede `Installed packages:`. The table has a `Path | Version | Description | Location` header and a `------- | ...` separator row.

4. **`avdmanager list avd` has two sections.** Loadable devices appear under `Available Android Virtual Devices:` as indented `Name:`/`Device:`/`Path:`/`Target:` blocks separated by blank lines. Devices that fail to resolve appear under `The following Android Virtual Devices could not be loaded:` with `Name:`, `Path:`, and `Error:` keys.

5. **Android command-line tools live under a versioned directory.** On the dev machine both `cmdline-tools/11.0/bin` and `cmdline-tools/latest/bin` exist. `latest` is checked first.

6. **`/usr/bin/trash` exists on macOS 26.5.2** and is the Trash mechanism referenced by the spec. Phase 2 does not use it; Phase 4 does.

7. **Existing `sim_prune.run_simctl` already implements the runner shape this phase generalizes:** `subprocess.run([...], capture_output=True, text=True, check=False)`, raising on non-zero. Its `Runner = Callable[[Sequence[str]], str]` seam is what `load_inventory(runner=...)` uses in tests.

---

## File Structure

**New Python modules** (all under `src/mac_dev_clean/`):

| File | Responsibility |
|---|---|
| `journal.py` | Append-only NDJSON action journal: record construction, atomic append, size-based rotation. No knowledge of any analyzer. |
| `tools/__init__.py` | Package marker; re-exports `ToolRunner`, `ToolResult`, `ToolUnavailable`, `run_tool`. |
| `tools/runner.py` | The single subprocess seam. Locates a binary, runs an argument vector without a shell, bounds stderr, and converts absence/failure into facts. |
| `tools/sizes.py` | Parse tool-reported size strings (`15.03GB`, `106.5kB`, `799.8MB`, `0B`) into bytes, preserving the original string. |
| `tools/docker.py` | `docker system df` inventory; four resource classes; four pinned prune vectors. |
| `tools/homebrew.py` | `brew cleanup -n` preview parsing; the `brew cleanup` action. |
| `tools/android.py` | `sdkmanager --list_installed` and `avdmanager list avd` inventory; grouping; SDK-root discovery. |
| `tools/simulator.py` | Adapts the existing `sim_prune` inventory into tool-managed recommendations. |
| `tools/registry.py` | Ordered list of analyzers; runs each, collects recommendations and unavailability facts. |
| `tool_executor.py` | Re-previews, executes a recommendation's argument vector, journals, and returns an `ApplyResult`. |

**Modified Python modules:** `recommendation.py` (tool payload fields), `cleaner.py` (journal), `executor.py` (journal), `cli.py` (new commands), `output.py` (tool section rendering).

**New Python tests** (under `tests/`): `test_journal.py`, `test_tool_runner.py`, `test_tool_sizes.py`, `test_docker_tool.py`, `test_homebrew_tool.py`, `test_android_tool.py`, `test_simulator_tool.py`, `test_tool_registry.py`, `test_tool_executor.py`, `test_tools_cli.py`.

**Modified Swift:** `Backend.swift` (decode tool rows), `AppModel.swift` (tool state), `ContentView.swift` (Tool-managed Storage section), `macos/project.yml`.

---

### Task 1: Audit journal

**Files:**
- Create: `src/mac_dev_clean/journal.py`
- Test: `tests/test_journal.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `JournalRecord` dataclass, `ActionJournal` class with `.append(record) -> bool` and `.path`, `open_journal(path: Optional[Path] = None) -> ActionJournal`, `DEFAULT_JOURNAL_PATH`, `MAX_JOURNAL_BYTES`, `ROTATED_SUFFIX`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_journal.py`:

```python
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mac_dev_clean.journal import (
    ActionJournal,
    JournalRecord,
    MAX_JOURNAL_BYTES,
    open_journal,
)


def build_record(**overrides):
    defaults = dict(
        recommendation_id="abc123",
        detector_id="docker-build-cache",
        category="tool-managed",
        target="docker:build-cache",
        action="invoke_tool",
        argv=("docker", "builder", "prune", "-f"),
        reclaimable_bytes=7499000000,
        dry_run=False,
        outcome="invoked",
        detail="",
    )
    defaults.update(overrides)
    return JournalRecord(**defaults)


class JournalAppendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "logs" / "actions.jsonl"
        self.addCleanup(self.tmp.cleanup)

    def test_append_creates_parent_and_writes_one_json_line(self):
        journal = ActionJournal(self.path)

        self.assertTrue(journal.append(build_record()))

        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["outcome"], "invoked")
        self.assertEqual(payload["argv"], ["docker", "builder", "prune", "-f"])
        self.assertEqual(payload["detector_id"], "docker-build-cache")
        self.assertFalse(payload["dry_run"])

    def test_timestamp_is_utc_iso8601(self):
        journal = ActionJournal(self.path)
        journal.append(build_record())

        payload = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        parsed = datetime.fromisoformat(payload["timestamp"])
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(None))

    def test_appends_accumulate(self):
        journal = ActionJournal(self.path)
        journal.append(build_record(outcome="invoked"))
        journal.append(build_record(outcome="failed", detail="exit 1"))

        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["outcome"] for line in lines],
                         ["invoked", "failed"])

    def test_dry_run_is_recorded_distinctly(self):
        journal = ActionJournal(self.path)
        journal.append(build_record(dry_run=True, outcome="skipped"))

        payload = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["outcome"], "skipped")

    def test_refusal_is_recorded_with_its_reason(self):
        journal = ActionJournal(self.path)
        journal.append(
            build_record(outcome="changed_since_scan", detail="the project was edited recently")
        )

        payload = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(payload["outcome"], "changed_since_scan")
        self.assertEqual(payload["detail"], "the project was edited recently")

    def test_path_action_records_no_argv(self):
        journal = ActionJournal(self.path)
        journal.append(
            build_record(action="delete_tree", argv=(), target="/Users/test/app/node_modules")
        )

        payload = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(payload["argv"], [])
        self.assertEqual(payload["target"], "/Users/test/app/node_modules")

    def test_surrogate_paths_do_not_raise(self):
        journal = ActionJournal(self.path)

        self.assertTrue(journal.append(build_record(target="/Users/test/app/\udcff")))
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)


class JournalFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_unwritable_directory_returns_false_without_raising(self):
        blocked = Path(self.tmp.name) / "blocked"
        blocked.mkdir()
        os.chmod(str(blocked), 0o500)
        self.addCleanup(os.chmod, str(blocked), 0o700)

        journal = ActionJournal(blocked / "actions.jsonl")

        self.assertFalse(journal.append(build_record()))

    def test_parent_path_is_a_file_returns_false(self):
        clash = Path(self.tmp.name) / "clash"
        clash.write_text("not a directory", encoding="utf-8")

        journal = ActionJournal(clash / "actions.jsonl")

        self.assertFalse(journal.append(build_record()))


class JournalRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "actions.jsonl"
        self.addCleanup(self.tmp.cleanup)

    def test_rotates_when_the_file_exceeds_the_limit(self):
        self.path.write_text("x" * (MAX_JOURNAL_BYTES + 1), encoding="utf-8")
        journal = ActionJournal(self.path)

        journal.append(build_record())

        rotated = self.path.with_suffix(".jsonl.1")
        self.assertTrue(rotated.exists())
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)

    def test_rotation_replaces_an_older_rotated_file(self):
        rotated = self.path.with_suffix(".jsonl.1")
        rotated.write_text("older\n", encoding="utf-8")
        self.path.write_text("x" * (MAX_JOURNAL_BYTES + 1), encoding="utf-8")
        journal = ActionJournal(self.path)

        journal.append(build_record())

        self.assertNotIn("older", rotated.read_text(encoding="utf-8"))

    def test_small_file_is_not_rotated(self):
        self.path.write_text("small\n", encoding="utf-8")
        journal = ActionJournal(self.path)

        journal.append(build_record())

        self.assertFalse(self.path.with_suffix(".jsonl.1").exists())


class OpenJournalTests(unittest.TestCase):
    def test_open_journal_honours_an_explicit_path(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name) / "custom.jsonl"

        journal = open_journal(target)

        self.assertEqual(journal.path, target)

    def test_open_journal_default_is_under_library_logs(self):
        journal = open_journal()

        self.assertEqual(journal.path.name, "actions.jsonl")
        self.assertIn("mac-dev-clean", str(journal.path))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_journal -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.journal'`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/journal.py`:

```python
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

DEFAULT_JOURNAL_PATH = Path("~/Library/Logs/mac-dev-clean/actions.jsonl")

#: Rotate at 4 MiB. Large enough to hold months of ordinary use, small enough
#: that reading the file during an investigation stays instant.
MAX_JOURNAL_BYTES = 4 * 1024 * 1024

ROTATED_SUFFIX = ".jsonl.1"


@dataclass(frozen=True)
class JournalRecord:
    """One attempted action.

    `target` is a normalized filesystem path for path actions, or a tool
    resource identifier such as `docker:build-cache` for tool actions.
    `argv` is empty for path actions and holds the exact executed vector for
    tool actions, so the journal answers "what command ran?" without the
    reader having to guess it from the detector id.
    """

    recommendation_id: str
    detector_id: str
    category: str
    target: str
    action: str
    outcome: str
    reclaimable_bytes: int
    dry_run: bool
    argv: Tuple[str, ...] = ()
    detail: str = ""
    recorded_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, object]:
        moment = self.recorded_at or datetime.now(timezone.utc)
        return {
            "timestamp": moment.astimezone(timezone.utc).isoformat(),
            "recommendation_id": self.recommendation_id,
            "detector_id": self.detector_id,
            "category": self.category,
            "target": self.target,
            "action": self.action,
            "argv": list(self.argv),
            "reclaimable_bytes": self.reclaimable_bytes,
            "dry_run": self.dry_run,
            "outcome": self.outcome,
            "detail": self.detail,
        }


class ActionJournal:
    """Append-only NDJSON record of every attempted action.

    A journal failure is never allowed to change what happens to the user's
    files: `append` reports success as a bool and swallows I/O errors, so a
    caller that has already deleted something can still report that fact.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path).expanduser()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: JournalRecord) -> bool:
        # ensure_ascii keeps surrogate-escaped filenames from raising during
        # encoding; one odd path must never cost us the audit record.
        line = json.dumps(record.to_dict(), ensure_ascii=True, sort_keys=True)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed()
            with open(str(self._path), "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            return False
        return True

    def _rotate_if_needed(self) -> None:
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size <= MAX_JOURNAL_BYTES:
            return
        rotated = self._path.with_suffix(ROTATED_SUFFIX)
        try:
            os.replace(str(self._path), str(rotated))
        except OSError:
            # Rotation is housekeeping. Failing to rotate must never stop an
            # action from being recorded, so fall through and keep appending.
            return


def open_journal(path: Optional[Path] = None) -> ActionJournal:
    target = Path(path) if path is not None else DEFAULT_JOURNAL_PATH
    return ActionJournal(target.expanduser())
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_journal -v`
Expected: PASS, all cases.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/journal.py tests/test_journal.py
git commit -m "feat: record every attempted action in a local audit journal"
```

---

### Task 2: Tool size parsing

**Files:**
- Create: `src/mac_dev_clean/tools/__init__.py`, `src/mac_dev_clean/tools/sizes.py`
- Test: `tests/test_tool_sizes.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `parse_tool_size(text: str) -> int` in `mac_dev_clean.tools.sizes`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tool_sizes.py`:

```python
from __future__ import annotations

import unittest

from mac_dev_clean.tools.sizes import parse_tool_size


class ParseToolSizeTests(unittest.TestCase):
    def test_parses_docker_gigabytes(self):
        self.assertEqual(parse_tool_size("15.03GB"), 15030000000)

    def test_parses_docker_kilobytes_with_lowercase_prefix(self):
        self.assertEqual(parse_tool_size("106.5kB"), 106500)

    def test_strips_a_percentage_suffix(self):
        self.assertEqual(parse_tool_size("15.03GB (68%)"), 15030000000)

    def test_parses_a_value_without_a_percentage_suffix(self):
        self.assertEqual(parse_tool_size("7.499GB"), 7499000000)

    def test_zero_bytes(self):
        self.assertEqual(parse_tool_size("0B"), 0)

    def test_bare_bytes(self):
        self.assertEqual(parse_tool_size("512B"), 512)

    def test_parses_homebrew_megabytes(self):
        self.assertEqual(parse_tool_size("799.8MB"), 799800000)

    def test_tolerates_surrounding_whitespace(self):
        self.assertEqual(parse_tool_size("  22.03GB  "), 22030000000)

    def test_terabytes(self):
        self.assertEqual(parse_tool_size("1.5TB"), 1500000000000)

    def test_empty_string_is_zero(self):
        self.assertEqual(parse_tool_size(""), 0)

    def test_unparseable_text_is_zero(self):
        self.assertEqual(parse_tool_size("N/A"), 0)

    def test_missing_unit_is_treated_as_bytes(self):
        self.assertEqual(parse_tool_size("1024"), 1024)

    def test_negative_values_are_clamped_to_zero(self):
        self.assertEqual(parse_tool_size("-5GB"), 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_tool_sizes -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.tools'`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/tools/__init__.py`:

```python
from __future__ import annotations

from .runner import ToolResult, ToolRunner, ToolUnavailable, run_tool

__all__ = ["ToolResult", "ToolRunner", "ToolUnavailable", "run_tool"]
```

Note: `runner.py` is created in Task 3. Write `sizes.py` first, then create a
temporary `__init__.py` containing only `from __future__ import annotations`
and replace it at the end of Task 3. To keep this task self-contained, create
`src/mac_dev_clean/tools/__init__.py` with just:

```python
from __future__ import annotations
```

Create `src/mac_dev_clean/tools/sizes.py`:

```python
from __future__ import annotations

import re

#: Docker and Homebrew report SI units, not binary ones: `15.03GB` means
#: 15.03 * 10^9 bytes, not 15.03 GiB. Converting with 1024 would overstate
#: every figure by 7% and make our number disagree with the tool's own.
_MULTIPLIERS = (
    ("TB", 1000 ** 4),
    ("GB", 1000 ** 3),
    ("MB", 1000 ** 2),
    ("KB", 1000),
    ("B", 1),
)

_VALUE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z]*)")


def parse_tool_size(text: str) -> int:
    """Convert a tool-reported size such as `15.03GB (68%)` into bytes.

    Returns 0 for anything unparseable. A tool that reports a figure we cannot
    read must degrade to "no estimate", never to a guess: the estimate drives
    what the user sees before confirming a destructive command.
    """
    if not text:
        return 0
    match = _VALUE.match(text)
    if match is None:
        return 0

    try:
        value = float(match.group(1))
    except ValueError:
        return 0
    if value <= 0:
        return 0

    unit = match.group(2).upper()
    if not unit:
        return int(value)
    for suffix, multiplier in _MULTIPLIERS:
        if unit == suffix:
            return int(value * multiplier)
    return 0
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_tool_sizes -v`
Expected: PASS, 13 cases.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/tools/__init__.py src/mac_dev_clean/tools/sizes.py tests/test_tool_sizes.py
git commit -m "feat: parse tool-reported size strings in SI units"
```

---

### Task 3: Tool runner

**Files:**
- Create: `src/mac_dev_clean/tools/runner.py`
- Modify: `src/mac_dev_clean/tools/__init__.py`
- Test: `tests/test_tool_runner.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ToolResult(argv, stdout, stderr, exit_code)` with `.ok`, `ToolUnavailable(tool, reason)`, `ToolRunner` protocol-shaped callable `Callable[[Sequence[str]], ToolResult]`, `run_tool(argv, timeout=...) -> ToolResult`, `find_binary(name, extra_dirs=()) -> Optional[str]`, `MAX_STDERR_CHARS`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tool_runner.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_tool_runner -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.tools.runner'`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/tools/runner.py`:

```python
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
```

Replace `src/mac_dev_clean/tools/__init__.py` with:

```python
from __future__ import annotations

from .runner import ToolResult, ToolRunner, ToolUnavailable, find_binary, run_tool

__all__ = [
    "ToolResult",
    "ToolRunner",
    "ToolUnavailable",
    "find_binary",
    "run_tool",
]
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_tool_runner tests.test_tool_sizes -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/tools/runner.py src/mac_dev_clean/tools/__init__.py tests/test_tool_runner.py
git commit -m "feat: add a shell-free runner seam for external tools"
```

---

### Task 4: Tool recommendation payload

**Files:**
- Modify: `src/mac_dev_clean/recommendation.py`
- Test: `tests/test_recommendation.py` (append)

**Interfaces:**
- Consumes: `Recommendation`, `ActionKind` from Task 1 of Phase 1.
- Produces: `ToolAction(tool: str, resource: str, argv: Tuple[str, ...], preview_argv: Tuple[str, ...], reported: str)` with `.to_dict()`; `Recommendation.tool_action: Optional[ToolAction]` field; `Recommendation.to_dict()` gains a `tool_action` key that is `None` for path recommendations.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_recommendation.py`:

```python
class ToolActionTests(unittest.TestCase):
    def build_tool_recommendation(self, **overrides):
        from mac_dev_clean.recommendation import ToolAction

        defaults = dict(
            detector_id="docker-build-cache",
            category="tool-managed",
            label="Docker build cache",
            path=Path("/Users/test/home"),
            action=ActionKind.INVOKE_TOOL,
            allocated_bytes=18920000000,
            reclaimable_bytes=7499000000,
            confidence=Confidence.EXACT,
            restoration=RestorationCost.EXTERNAL_STATE,
            selected_by_default=False,
            evidence=(Evidence("tool-report", "docker reports 7.499GB reclaimable"),),
            safety_root=Path("/Users/test/home"),
            reason="Docker reports reclaimable build cache.",
            generation=1,
            tool_action=ToolAction(
                tool="docker",
                resource="build-cache",
                argv=("docker", "builder", "prune", "-f"),
                preview_argv=("docker", "system", "df", "--format", "{{json .}}"),
                reported="7.499GB",
            ),
        )
        defaults.update(overrides)
        return Recommendation(**defaults)

    def test_tool_action_serializes(self):
        payload = self.build_tool_recommendation().to_dict()

        self.assertEqual(payload["tool_action"]["tool"], "docker")
        self.assertEqual(payload["tool_action"]["resource"], "build-cache")
        self.assertEqual(
            payload["tool_action"]["argv"], ["docker", "builder", "prune", "-f"]
        )
        self.assertEqual(payload["tool_action"]["reported"], "7.499GB")

    def test_path_recommendations_carry_no_tool_action(self):
        item = Recommendation(
            detector_id="node-modules",
            category="project-dependencies",
            label="node_modules",
            path=Path("/Users/test/app/node_modules"),
            action=ActionKind.DELETE_TREE,
            allocated_bytes=2048,
            reclaimable_bytes=2048,
            confidence=Confidence.STRONG,
            restoration=RestorationCost.REDOWNLOAD,
            selected_by_default=True,
            evidence=(),
            safety_root=Path("/Users/test/app"),
            reason="",
            generation=1,
        )

        self.assertIsNone(item.to_dict()["tool_action"])

    def test_invoke_tool_is_never_selected_by_default(self):
        item = self.build_tool_recommendation(selected_by_default=True)

        self.assertFalse(item.selected_by_default)

    def test_invoke_tool_without_a_tool_action_is_rejected(self):
        with self.assertRaises(ValueError):
            self.build_tool_recommendation(tool_action=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_recommendation -v`
Expected: FAIL with `ImportError: cannot import name 'ToolAction'`

- [ ] **Step 3: Write the implementation**

In `src/mac_dev_clean/recommendation.py`, add after the `Evidence` dataclass:

```python
@dataclass(frozen=True)
class ToolAction:
    """The exact commands behind a tool-managed recommendation.

    `preview_argv` is re-run immediately before `argv` executes, because a
    tool's internal state can change between the scan and the confirmation in
    a way no filesystem check can detect. Both vectors are frozen here at
    analysis time so nothing downstream can widen them.
    """

    tool: str
    resource: str
    argv: Tuple[str, ...]
    preview_argv: Tuple[str, ...]
    reported: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "tool": self.tool,
            "resource": self.resource,
            "argv": list(self.argv),
            "preview_argv": list(self.preview_argv),
            "reported": self.reported,
        }
```

Add the field to `Recommendation`, after `last_activity_at`:

```python
    tool_action: Optional[ToolAction] = None
```

Extend `__post_init__`:

```python
    def __post_init__(self) -> None:
        if self.action is ActionKind.INVOKE_TOOL and self.tool_action is None:
            raise ValueError("invoke_tool recommendations require a tool_action")
        eligible = (
            self.action in DESTRUCTIVE_ACTIONS
            and self.confidence is not Confidence.UNKNOWN
        )
        if self.selected_by_default and not eligible:
            object.__setattr__(self, "selected_by_default", False)
```

`ActionKind.INVOKE_TOOL` is deliberately absent from `DESTRUCTIVE_ACTIONS`, so
this existing clause already forces `selected_by_default = False` for every
tool action — which is exactly the spec's "never selected by default" rule.

Extend `to_dict()` with:

```python
            "tool_action": self.tool_action.to_dict() if self.tool_action else None,
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_recommendation tests.test_index tests.test_policy -v`
Expected: PASS. `tests/test_index.py` must still pass because `_decode` tolerates a missing key.

- [ ] **Step 5: Teach the index to round-trip the new field**

In `src/mac_dev_clean/index.py`, import `ToolAction` and add to `_decode`, before the `return`:

```python
    raw_tool = payload.get("tool_action")
    tool_action = (
        ToolAction(
            tool=raw_tool["tool"],
            resource=raw_tool["resource"],
            argv=tuple(raw_tool["argv"]),
            preview_argv=tuple(raw_tool["preview_argv"]),
            reported=raw_tool.get("reported", ""),
        )
        if raw_tool
        else None
    )
```

and pass `tool_action=tool_action` to the `Recommendation(...)` call.

- [ ] **Step 6: Write the index round-trip test**

Append to `tests/test_index.py`:

```python
class ToolRecommendationRoundTripTests(unittest.TestCase):
    def test_a_tool_recommendation_survives_a_round_trip(self):
        from mac_dev_clean.recommendation import ToolAction

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index = open_index(Path(tmp.name) / "index.sqlite3")
        self.addCleanup(index.close)
        index.begin_generation(VolumeIdentity(device=1, uuid="U"), event_id=1)

        item = Recommendation(
            detector_id="docker-build-cache",
            category="tool-managed",
            label="Docker build cache",
            path=Path("/Users/test/home"),
            action=ActionKind.INVOKE_TOOL,
            allocated_bytes=1,
            reclaimable_bytes=1,
            confidence=Confidence.EXACT,
            restoration=RestorationCost.EXTERNAL_STATE,
            selected_by_default=False,
            evidence=(),
            safety_root=Path("/Users/test/home"),
            reason="",
            generation=1,
            tool_action=ToolAction(
                tool="docker",
                resource="build-cache",
                argv=("docker", "builder", "prune", "-f"),
                preview_argv=("docker", "system", "df", "--format", "{{json .}}"),
                reported="7.499GB",
            ),
        )
        index.record_recommendation(item)
        index.complete_generation()

        loaded = index.load_recommendation(item.id)

        self.assertIsNotNone(loaded.tool_action)
        self.assertEqual(loaded.tool_action.argv, ("docker", "builder", "prune", "-f"))
```

Adjust the imports at the top of `tests/test_index.py` if `VolumeIdentity`,
`Recommendation`, `ActionKind`, `Confidence`, `RestorationCost`, `tempfile`, or
`Path` are not already imported there.

- [ ] **Step 7: Run the tests**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests -v 2>&1 | tail -5`
Expected: OK.

- [ ] **Step 8: Commit**

```bash
git add src/mac_dev_clean/recommendation.py src/mac_dev_clean/index.py tests/test_recommendation.py tests/test_index.py
git commit -m "feat: carry a frozen argument vector on tool-managed recommendations"
```

---

### Task 5: Docker analyzer

**Files:**
- Create: `src/mac_dev_clean/tools/docker.py`
- Test: `tests/test_docker_tool.py`

**Interfaces:**
- Consumes: `ToolResult`, `ToolRunner`, `ToolUnavailable` from `tools.runner`; `parse_tool_size` from `tools.sizes`; `Recommendation`, `ToolAction`, `ActionKind`, `Confidence`, `RestorationCost`, `Evidence`.
- Produces: `DOCKER_PREVIEW_ARGV`, `analyze_docker(runner: ToolRunner, generation: int, home: Path) -> Tuple[List[Recommendation], Optional[str]]` returning `(recommendations, unavailable_reason)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_docker_tool.py`:

```python
from __future__ import annotations

import unittest
from pathlib import Path

from mac_dev_clean.recommendation import ActionKind
from mac_dev_clean.tools.docker import DOCKER_PREVIEW_ARGV, analyze_docker
from mac_dev_clean.tools.runner import ToolResult, ToolUnavailable

HOME = Path("/Users/test/home")

REAL_OUTPUT = (
    '{"Active":"4","Reclaimable":"15.03GB (68%)","Size":"22.03GB",'
    '"TotalCount":"18","Type":"Images"}\n'
    '{"Active":"2","Reclaimable":"106.5kB (0%)","Size":"62.55MB",'
    '"TotalCount":"4","Type":"Containers"}\n'
    '{"Active":"6","Reclaimable":"0B (0%)","Size":"1.133GB",'
    '"TotalCount":"6","Type":"Local Volumes"}\n'
    '{"Active":"0","Reclaimable":"7.499GB","Size":"18.92GB",'
    '"TotalCount":"144","Type":"Build Cache"}\n'
)


def runner_returning(stdout: str, exit_code: int = 0, stderr: str = ""):
    def run(argv):
        return ToolResult(
            argv=tuple(argv), stdout=stdout, stderr=stderr, exit_code=exit_code
        )

    return run


def runner_raising(reason: str = "not installed"):
    def run(argv):
        raise ToolUnavailable("docker", reason)

    return run


class DockerInventoryTests(unittest.TestCase):
    def test_produces_one_recommendation_per_reclaimable_class(self):
        items, unavailable = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        self.assertIsNone(unavailable)
        resources = sorted(item.tool_action.resource for item in items)
        self.assertEqual(resources, ["build-cache", "containers", "images"])

    def test_zero_reclaimable_classes_are_skipped(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        self.assertNotIn("volumes", [item.tool_action.resource for item in items])

    def test_reclaimable_bytes_use_si_units(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )
        by_resource = {item.tool_action.resource: item for item in items}

        self.assertEqual(by_resource["build-cache"].reclaimable_bytes, 7499000000)
        self.assertEqual(by_resource["images"].reclaimable_bytes, 15030000000)

    def test_argument_vectors_are_the_narrow_prune_verbs(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )
        vectors = {item.tool_action.resource: item.tool_action.argv for item in items}

        self.assertEqual(vectors["build-cache"], ("docker", "builder", "prune", "-f"))
        self.assertEqual(vectors["images"], ("docker", "image", "prune", "-f"))
        self.assertEqual(vectors["containers"], ("docker", "container", "prune", "-f"))

    def test_image_prune_is_never_the_all_variant(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        for item in items:
            self.assertNotIn("-a", item.tool_action.argv)
            self.assertNotIn("--all", item.tool_action.argv)

    def test_every_item_is_invoke_tool_and_unselected(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        for item in items:
            self.assertIs(item.action, ActionKind.INVOKE_TOOL)
            self.assertFalse(item.selected_by_default)

    def test_evidence_keeps_dockers_own_string(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )
        build_cache = next(
            item for item in items if item.tool_action.resource == "build-cache"
        )

        details = " ".join(entry.detail for entry in build_cache.evidence)
        self.assertIn("7.499GB", details)
        self.assertEqual(build_cache.tool_action.reported, "7.499GB")

    def test_preview_argv_is_recorded_for_re_preview(self):
        items, _ = analyze_docker(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        for item in items:
            self.assertEqual(item.tool_action.preview_argv, DOCKER_PREVIEW_ARGV)


class DockerUnavailableTests(unittest.TestCase):
    def test_missing_binary_yields_a_reason_and_no_items(self):
        items, unavailable = analyze_docker(
            runner_raising("not installed"), generation=1, home=HOME
        )

        self.assertEqual(items, [])
        self.assertIn("not installed", unavailable)

    def test_stopped_daemon_yields_a_reason_and_no_items(self):
        items, unavailable = analyze_docker(
            runner_returning("", exit_code=1, stderr="Cannot connect to the Docker daemon"),
            generation=1,
            home=HOME,
        )

        self.assertEqual(items, [])
        self.assertIn("Cannot connect", unavailable)

    def test_unparseable_line_is_skipped_without_failing_the_scan(self):
        items, unavailable = analyze_docker(
            runner_returning('not json\n' + REAL_OUTPUT), generation=1, home=HOME
        )

        self.assertIsNone(unavailable)
        self.assertEqual(len(items), 3)

    def test_empty_output_produces_no_items_and_no_error(self):
        items, unavailable = analyze_docker(
            runner_returning(""), generation=1, home=HOME
        )

        self.assertEqual(items, [])
        self.assertIsNone(unavailable)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_docker_tool -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.tools.docker'`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/tools/docker.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from ..recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    ToolAction,
)
from .runner import ToolRunner, ToolUnavailable
from .sizes import parse_tool_size

DOCKER_PREVIEW_ARGV = ("docker", "system", "df", "--format", "{{json .}}")


@dataclass(frozen=True)
class _ResourceClass:
    docker_type: str
    resource: str
    label: str
    argv: Tuple[str, ...]
    note: str


#: `docker image prune -a` is deliberately absent. It removes every image not
#: attached to a *running* container, which is far broader than the
#: `Reclaimable` figure `system df` reports and would delete images the user
#: pulled on purpose. Offering it would break the rule that an executed vector
#: matches the effect its preview described.
_CLASSES = (
    _ResourceClass(
        docker_type="Build Cache",
        resource="build-cache",
        label="Docker build cache",
        argv=("docker", "builder", "prune", "-f"),
        note="Layer cache from previous builds. Docker rebuilds it on the next build.",
    ),
    _ResourceClass(
        docker_type="Images",
        resource="images",
        label="Docker dangling images",
        argv=("docker", "image", "prune", "-f"),
        note="Untagged images. Tagged images and images in use are not touched.",
    ),
    _ResourceClass(
        docker_type="Containers",
        resource="containers",
        label="Docker stopped containers",
        argv=("docker", "container", "prune", "-f"),
        note="Stopped containers. Running containers are not touched.",
    ),
    _ResourceClass(
        docker_type="Local Volumes",
        resource="volumes",
        label="Docker unused volumes",
        argv=("docker", "volume", "prune", "-f"),
        note="Volumes no container references. Volume data cannot be recovered.",
    ),
)

_CLASSES_BY_TYPE = {item.docker_type: item for item in _CLASSES}


def analyze_docker(
    runner: ToolRunner, generation: int, home: Path
) -> Tuple[List[Recommendation], Optional[str]]:
    """Ask Docker what it can reclaim, without starting it.

    Returns `(recommendations, unavailable_reason)`. A stopped daemon or a
    missing binary is reported as a reason string, never as an exception:
    "Docker is not answering" is information the user needs, not a scan
    failure.
    """
    try:
        result = runner(DOCKER_PREVIEW_ARGV)
    except ToolUnavailable as exc:
        return [], exc.reason

    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip()
        return [], detail or "docker exited with {}".format(result.exit_code)

    items: List[Recommendation] = []
    for line in result.lines():
        try:
            payload = json.loads(line)
        except ValueError:
            # One unreadable line must not cost us the other classes.
            continue
        if not isinstance(payload, dict):
            continue

        spec = _CLASSES_BY_TYPE.get(str(payload.get("Type", "")))
        if spec is None:
            continue

        reported = str(payload.get("Reclaimable", ""))
        reclaimable = parse_tool_size(reported)
        if reclaimable <= 0:
            continue

        total = str(payload.get("Size", ""))
        evidence = (
            Evidence("tool-report", "docker reports {} reclaimable".format(reported)),
            Evidence("tool-total", "docker reports {} in total".format(total)),
            Evidence("preview-command", " ".join(DOCKER_PREVIEW_ARGV)),
        )

        items.append(
            Recommendation(
                detector_id="docker-{}".format(spec.resource),
                category="tool-managed",
                label=spec.label,
                path=home,
                action=ActionKind.INVOKE_TOOL,
                allocated_bytes=parse_tool_size(total),
                reclaimable_bytes=reclaimable,
                confidence=Confidence.EXACT,
                restoration=RestorationCost.EXTERNAL_STATE,
                selected_by_default=False,
                evidence=evidence,
                safety_root=home,
                reason=spec.note,
                generation=generation,
                warning="Docker reports this figure. Freed space may not appear on the volume until Docker compacts its disk image.",
                tool_action=ToolAction(
                    tool="docker",
                    resource=spec.resource,
                    argv=spec.argv,
                    preview_argv=DOCKER_PREVIEW_ARGV,
                    reported=reported,
                ),
            )
        )

    return items, None
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_docker_tool -v`
Expected: PASS, all cases.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/tools/docker.py tests/test_docker_tool.py
git commit -m "feat: report Docker reclaimable storage through docker system df"
```

---

### Task 6: Homebrew analyzer

**Files:**
- Create: `src/mac_dev_clean/tools/homebrew.py`
- Test: `tests/test_homebrew_tool.py`

**Interfaces:**
- Consumes: `ToolRunner`, `ToolUnavailable`, `parse_tool_size`, recommendation types.
- Produces: `BREW_PREVIEW_ARGV`, `BREW_CLEANUP_ARGV`, `parse_brew_preview(stdout: str) -> Tuple[int, int]` returning `(total_bytes, removal_count)`, `analyze_homebrew(runner, generation, home) -> Tuple[List[Recommendation], Optional[str]]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_homebrew_tool.py`:

```python
from __future__ import annotations

import unittest
from pathlib import Path

from mac_dev_clean.recommendation import ActionKind
from mac_dev_clean.tools.homebrew import (
    BREW_CLEANUP_ARGV,
    BREW_PREVIEW_ARGV,
    analyze_homebrew,
    parse_brew_preview,
)
from mac_dev_clean.tools.runner import ToolResult, ToolUnavailable

HOME = Path("/Users/test/home")

REAL_OUTPUT = """Warning: Skipping aom: most recent version 3.15.0 not installed
Warning: Skipping assimp: most recent version 6.0.5 not installed
Would remove: /opt/homebrew/Library/Homebrew/vendor/portable-ruby/4.0.3 (1,704 files, 34.6MB)
Would remove: /opt/homebrew/Library/Homebrew/vendor/portable-ruby/4.0.5_1 (1,707 files, 34.6MB)
==> This operation would free approximately 799.8MB of disk space.
"""


def runner_returning(stdout: str, exit_code: int = 0, stderr: str = ""):
    def run(argv):
        return ToolResult(
            argv=tuple(argv), stdout=stdout, stderr=stderr, exit_code=exit_code
        )

    return run


class ParseBrewPreviewTests(unittest.TestCase):
    def test_reads_the_total_line(self):
        total, _ = parse_brew_preview(REAL_OUTPUT)

        self.assertEqual(total, 799800000)

    def test_counts_only_removal_lines(self):
        _, count = parse_brew_preview(REAL_OUTPUT)

        self.assertEqual(count, 2)

    def test_warning_lines_are_never_counted_as_removals(self):
        _, count = parse_brew_preview(
            "Warning: Skipping aom: most recent version 3.15.0 not installed\n"
        )

        self.assertEqual(count, 0)

    def test_output_without_a_total_yields_zero(self):
        total, count = parse_brew_preview(
            "Would remove: /opt/homebrew/x (1 files, 1MB)\n"
        )

        self.assertEqual(total, 0)
        self.assertEqual(count, 1)

    def test_empty_output_yields_zeroes(self):
        self.assertEqual(parse_brew_preview(""), (0, 0))

    def test_nothing_to_do_output_yields_zeroes(self):
        self.assertEqual(parse_brew_preview("Warning: nothing to do\n"), (0, 0))


class AnalyzeHomebrewTests(unittest.TestCase):
    def test_produces_one_recommendation(self):
        items, unavailable = analyze_homebrew(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        self.assertIsNone(unavailable)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].reclaimable_bytes, 799800000)

    def test_action_is_the_plain_cleanup_vector(self):
        items, _ = analyze_homebrew(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        self.assertEqual(items[0].tool_action.argv, BREW_CLEANUP_ARGV)
        self.assertEqual(items[0].tool_action.preview_argv, BREW_PREVIEW_ARGV)
        self.assertIs(items[0].action, ActionKind.INVOKE_TOOL)
        self.assertFalse(items[0].selected_by_default)

    def test_no_prune_arguments_are_ever_used(self):
        items, _ = analyze_homebrew(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        joined = " ".join(items[0].tool_action.argv)
        self.assertNotIn("--prune", joined)
        self.assertNotIn("-s", items[0].tool_action.argv)

    def test_nothing_reclaimable_produces_no_items(self):
        items, unavailable = analyze_homebrew(
            runner_returning("Warning: nothing to do\n"), generation=1, home=HOME
        )

        self.assertEqual(items, [])
        self.assertIsNone(unavailable)

    def test_missing_binary_is_reported_as_unavailable(self):
        def run(argv):
            raise ToolUnavailable("brew", "not installed")

        items, unavailable = analyze_homebrew(run, generation=1, home=HOME)

        self.assertEqual(items, [])
        self.assertIn("not installed", unavailable)

    def test_non_zero_exit_is_reported_as_unavailable(self):
        items, unavailable = analyze_homebrew(
            runner_returning("", exit_code=1, stderr="brew is broken"),
            generation=1,
            home=HOME,
        )

        self.assertEqual(items, [])
        self.assertIn("brew is broken", unavailable)

    def test_evidence_names_the_preview_command_and_the_count(self):
        items, _ = analyze_homebrew(
            runner_returning(REAL_OUTPUT), generation=1, home=HOME
        )

        details = " ".join(entry.detail for entry in items[0].evidence)
        self.assertIn("brew cleanup -n", details)
        self.assertIn("2", details)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_homebrew_tool -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/tools/homebrew.py`:

```python
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from ..recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    ToolAction,
)
from .runner import ToolRunner, ToolUnavailable
from .sizes import parse_tool_size

BREW_PREVIEW_ARGV = ("brew", "cleanup", "-n")

#: Plain `brew cleanup` only. `--prune` and `-s` also remove the download cache
#: for *currently installed* versions, which is a different and larger effect
#: than the preview describes.
BREW_CLEANUP_ARGV = ("brew", "cleanup")

_TOTAL = re.compile(r"would free approximately\s+([0-9.]+\s*[A-Za-z]+)", re.IGNORECASE)


def parse_brew_preview(stdout: str) -> Tuple[int, int]:
    """Read `brew cleanup -n` output into `(total_bytes, removal_count)`.

    `Warning:` lines precede the removals and describe formulae Homebrew is
    *skipping*; counting them as removals would inflate the count and mislead
    the user about what the action does.
    """
    total = 0
    count = 0
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Would remove:"):
            count += 1
            continue
        match = _TOTAL.search(stripped)
        if match:
            total = parse_tool_size(match.group(1))
    return total, count


def analyze_homebrew(
    runner: ToolRunner, generation: int, home: Path
) -> Tuple[List[Recommendation], Optional[str]]:
    """Ask Homebrew what it would remove.

    The directory listing is not a substitute for this: on the development
    machine `~/Library/Caches/Homebrew` held 64 MiB while `brew cleanup -n`
    reported 799.8 MB, because most removable storage is old installed
    versions in the Cellar that must never be deleted by path.
    """
    try:
        result = runner(BREW_PREVIEW_ARGV)
    except ToolUnavailable as exc:
        return [], exc.reason

    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip()
        return [], detail or "brew exited with {}".format(result.exit_code)

    total, count = parse_brew_preview(result.stdout)
    if total <= 0 and count == 0:
        return [], None

    evidence = (
        Evidence("preview-command", " ".join(BREW_PREVIEW_ARGV)),
        Evidence("tool-report", "brew would remove {} items".format(count)),
    )

    item = Recommendation(
        detector_id="homebrew-cleanup",
        category="tool-managed",
        label="Homebrew removable versions and downloads",
        path=home,
        action=ActionKind.INVOKE_TOOL,
        allocated_bytes=total,
        reclaimable_bytes=total,
        confidence=Confidence.EXACT,
        restoration=RestorationCost.REDOWNLOAD,
        selected_by_default=False,
        evidence=evidence,
        safety_root=home,
        reason="Homebrew reports old installed versions and stale downloads it can remove.",
        generation=generation,
        warning="Reinstalling an older version afterwards requires downloading it again.",
        tool_action=ToolAction(
            tool="brew",
            resource="cleanup",
            argv=BREW_CLEANUP_ARGV,
            preview_argv=BREW_PREVIEW_ARGV,
            reported="{} items".format(count),
        ),
    )
    return [item], None
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_homebrew_tool -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/tools/homebrew.py tests/test_homebrew_tool.py
git commit -m "feat: report Homebrew removable storage through brew cleanup -n"
```

---

### Task 7: Android analyzer

**Files:**
- Create: `src/mac_dev_clean/tools/android.py`
- Test: `tests/test_android_tool.py`

**Interfaces:**
- Consumes: `ToolRunner`, `ToolUnavailable`, `find_binary`, `path_size` from `..scanner`, recommendation types.
- Produces: `SdkPackage(path, version, description, location)`, `Avd(name, path, target, error)`, `parse_sdk_packages(stdout) -> List[SdkPackage]`, `parse_avds(stdout) -> Tuple[List[Avd], List[Avd]]` returning `(loadable, unloadable)`, `find_sdk_root(env, home) -> Optional[Path]`, `analyze_android(sdk_runner, avd_runner, generation, sdk_root) -> Tuple[List[Recommendation], Optional[str]]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_android_tool.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mac_dev_clean.recommendation import ActionKind
from mac_dev_clean.tools.android import (
    analyze_android,
    find_sdk_root,
    parse_avds,
    parse_sdk_packages,
)
from mac_dev_clean.tools.runner import ToolResult, ToolUnavailable

SDK_OUTPUT = """Loading package information...
[=========                              ] 25% Loading local repository...
Installed packages:
  Path                          | Version | Description                    | Location
  -------                       | ------- | -------                        | -------
  build-tools;35.0.0            | 35.0.0  | Android SDK Build-Tools 35     | build-tools/35.0.0
  cmake;3.31.6                  | 3.31.6  | CMake 3.31.6                   | cmake/3.31.6
  ndk;27.0.12077973             | 27.0.1  | NDK (Side by side) 27.0.12077  | ndk/27.0.12077973
  system-images;android-34;x    | 1       | Google APIs ARM 64 Image       | system-images/android-34/x
  emulator                      | 36.2.12 | Android Emulator               | emulator
"""

AVD_OUTPUT = """Available Android Virtual Devices:
    Name: Pixel_2
  Device: pixel_2 (Google)
    Path: /Users/test/home/.android/avd/Pixel_2.avd
  Target: Google APIs (Google Inc.)
          Based on: Android 8.1 ("Oreo") Tag/ABI: google_apis/arm64-v8a
    Skin: pixel_2
  Sdcard: 512M

The following Android Virtual Devices could not be loaded:
    Name: Pixel_9
    Path: /Users/test/home/.android/avd/Pixel_9.avd
   Error: Google pixel_9 no longer exists as a device
"""


def runner_returning(stdout: str, exit_code: int = 0, stderr: str = ""):
    def run(argv):
        return ToolResult(
            argv=tuple(argv), stdout=stdout, stderr=stderr, exit_code=exit_code
        )

    return run


def runner_raising(reason: str = "not installed"):
    def run(argv):
        raise ToolUnavailable("sdkmanager", reason)

    return run


class ParseSdkPackagesTests(unittest.TestCase):
    def test_discards_progress_output_and_headers(self):
        packages = parse_sdk_packages(SDK_OUTPUT)

        self.assertEqual(len(packages), 5)
        self.assertNotIn("Path", [package.path for package in packages])

    def test_reads_path_version_and_location(self):
        packages = parse_sdk_packages(SDK_OUTPUT)
        ndk = next(p for p in packages if p.path.startswith("ndk;"))

        self.assertEqual(ndk.path, "ndk;27.0.12077973")
        self.assertEqual(ndk.version, "27.0.1")
        self.assertEqual(ndk.location, "ndk/27.0.12077973")

    def test_empty_output_yields_nothing(self):
        self.assertEqual(parse_sdk_packages(""), [])

    def test_output_without_the_installed_header_yields_nothing(self):
        self.assertEqual(parse_sdk_packages("Loading package information...\n"), [])


class ParseAvdsTests(unittest.TestCase):
    def test_reads_loadable_devices(self):
        loadable, _ = parse_avds(AVD_OUTPUT)

        self.assertEqual([avd.name for avd in loadable], ["Pixel_2"])
        self.assertTrue(loadable[0].path.endswith("Pixel_2.avd"))

    def test_reads_unloadable_devices_with_their_error(self):
        _, unloadable = parse_avds(AVD_OUTPUT)

        self.assertEqual([avd.name for avd in unloadable], ["Pixel_9"])
        self.assertIn("no longer exists", unloadable[0].error)

    def test_empty_output_yields_two_empty_lists(self):
        self.assertEqual(parse_avds(""), ([], []))


class FindSdkRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_prefers_android_home(self):
        sdk = self.home / "custom-sdk"
        sdk.mkdir()

        found = find_sdk_root({"ANDROID_HOME": str(sdk)}, self.home)

        self.assertEqual(found, sdk)

    def test_falls_back_to_android_sdk_root(self):
        sdk = self.home / "other-sdk"
        sdk.mkdir()

        found = find_sdk_root({"ANDROID_SDK_ROOT": str(sdk)}, self.home)

        self.assertEqual(found, sdk)

    def test_falls_back_to_the_default_location(self):
        default = self.home / "Library" / "Android" / "sdk"
        default.mkdir(parents=True)

        self.assertEqual(find_sdk_root({}, self.home), default)

    def test_returns_none_when_nothing_exists(self):
        self.assertIsNone(find_sdk_root({}, self.home))

    def test_ignores_an_environment_path_that_does_not_exist(self):
        default = self.home / "Library" / "Android" / "sdk"
        default.mkdir(parents=True)

        found = find_sdk_root({"ANDROID_HOME": str(self.home / "missing")}, self.home)

        self.assertEqual(found, default)


class AnalyzeAndroidTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sdk = Path(self.tmp.name) / "sdk"
        (self.sdk / "ndk" / "27.0.12077973").mkdir(parents=True)
        (self.sdk / "ndk" / "27.0.12077973" / "blob").write_bytes(b"x" * 4096)
        self.addCleanup(self.tmp.cleanup)

    def test_groups_packages_and_avds(self):
        items, unavailable = analyze_android(
            sdk_runner=runner_returning(SDK_OUTPUT),
            avd_runner=runner_returning(AVD_OUTPUT),
            generation=1,
            sdk_root=self.sdk,
        )

        self.assertIsNone(unavailable)
        detectors = sorted({item.detector_id for item in items})
        self.assertIn("android-ndk", detectors)
        self.assertIn("android-avd", detectors)

    def test_nothing_is_selected_by_default(self):
        items, _ = analyze_android(
            sdk_runner=runner_returning(SDK_OUTPUT),
            avd_runner=runner_returning(AVD_OUTPUT),
            generation=1,
            sdk_root=self.sdk,
        )

        for item in items:
            self.assertFalse(item.selected_by_default)
            self.assertIs(item.action, ActionKind.INVOKE_TOOL)

    def test_uninstall_uses_the_supported_vector(self):
        items, _ = analyze_android(
            sdk_runner=runner_returning(SDK_OUTPUT),
            avd_runner=runner_returning(AVD_OUTPUT),
            generation=1,
            sdk_root=self.sdk,
        )
        ndk = next(item for item in items if item.detector_id == "android-ndk")

        self.assertEqual(ndk.tool_action.argv[:2], ("sdkmanager", "--uninstall"))
        self.assertIn("ndk;27.0.12077973", ndk.tool_action.argv)

    def test_avd_delete_uses_the_supported_vector(self):
        items, _ = analyze_android(
            sdk_runner=runner_returning(SDK_OUTPUT),
            avd_runner=runner_returning(AVD_OUTPUT),
            generation=1,
            sdk_root=self.sdk,
        )
        avd = next(item for item in items if item.detector_id == "android-avd")

        self.assertEqual(
            avd.tool_action.argv, ("avdmanager", "delete", "avd", "-n", "Pixel_2")
        )

    def test_unloadable_avds_are_reported_but_never_selected(self):
        items, _ = analyze_android(
            sdk_runner=runner_returning(SDK_OUTPUT),
            avd_runner=runner_returning(AVD_OUTPUT),
            generation=1,
            sdk_root=self.sdk,
        )
        broken = [item for item in items if item.detector_id == "android-avd-broken"]

        self.assertEqual(len(broken), 1)
        self.assertFalse(broken[0].selected_by_default)
        details = " ".join(entry.detail for entry in broken[0].evidence)
        self.assertIn("no longer exists", details)

    def test_missing_sdk_root_is_unavailable(self):
        items, unavailable = analyze_android(
            sdk_runner=runner_returning(SDK_OUTPUT),
            avd_runner=runner_returning(AVD_OUTPUT),
            generation=1,
            sdk_root=None,
        )

        self.assertEqual(items, [])
        self.assertIn("SDK", unavailable)

    def test_missing_sdkmanager_still_reports_avds(self):
        items, unavailable = analyze_android(
            sdk_runner=runner_raising("not installed"),
            avd_runner=runner_returning(AVD_OUTPUT),
            generation=1,
            sdk_root=self.sdk,
        )

        self.assertIn("not installed", unavailable)
        self.assertTrue(any(item.detector_id == "android-avd" for item in items))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_android_tool -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

Create `src/mac_dev_clean/tools/android.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    ToolAction,
)
from ..scanner import path_size
from .runner import ToolRunner, ToolUnavailable, find_binary

SDK_LIST_ARGV = ("sdkmanager", "--list_installed")
AVD_LIST_ARGV = ("avdmanager", "list", "avd")

_INSTALLED_HEADER = "Installed packages:"
_AVAILABLE_HEADER = "Available Android Virtual Devices:"
_UNLOADABLE_HEADER = "could not be loaded"

#: Maps an sdkmanager package-path prefix to a detector id and label. Anything
#: that does not match stays out of the report rather than being lumped into a
#: vague "other" bucket the user cannot reason about.
_GROUPS = (
    ("ndk;", "android-ndk", "Android NDK"),
    ("cmake;", "android-cmake", "Android CMake"),
    ("system-images;", "android-system-image", "Android system image"),
    ("build-tools;", "android-build-tools", "Android build-tools"),
    ("platforms;", "android-platform", "Android platform"),
    ("cmdline-tools;", "android-cmdline-tools", "Android command-line tools"),
    ("emulator", "android-emulator", "Android emulator"),
)


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
    """Locate the Android SDK, preferring the environment over the default.

    An environment variable pointing at a directory that does not exist is
    ignored rather than trusted: a stale `ANDROID_HOME` in a shell profile is
    common and must not hide a real SDK at the default location.
    """
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        raw = env.get(key)
        if raw:
            candidate = Path(raw).expanduser()
            if candidate.is_dir():
                return candidate
    default = Path(home).expanduser() / "Library" / "Android" / "sdk"
    return default if default.is_dir() else None


def cmdline_tool_dirs(sdk_root: Path) -> Tuple[Path, ...]:
    """Directories that may hold sdkmanager/avdmanager, newest convention first."""
    base = Path(sdk_root) / "cmdline-tools"
    dirs: List[Path] = [base / "latest" / "bin"]
    try:
        for entry in sorted(base.iterdir(), reverse=True):
            if entry.is_dir() and entry.name != "latest":
                dirs.append(entry / "bin")
    except OSError:
        pass
    dirs.append(Path(sdk_root) / "tools" / "bin")
    return tuple(dirs)


def parse_sdk_packages(stdout: str) -> List[SdkPackage]:
    """Read `sdkmanager --list_installed` output.

    Everything before `Installed packages:` is progress noise written with
    carriage returns; the separator row of dashes is not a package either.
    """
    packages: List[SdkPackage] = []
    started = False
    for raw_line in stdout.splitlines():
        line = raw_line.replace("\r", " ").strip()
        if not started:
            if line.startswith(_INSTALLED_HEADER):
                started = True
            continue
        if not line or "|" not in line:
            continue
        columns = [column.strip() for column in line.split("|")]
        if len(columns) < 4:
            continue
        if columns[0] in ("Path", "-------") or set(columns[0]) == {"-"}:
            continue
        packages.append(
            SdkPackage(
                path=columns[0],
                version=columns[1],
                description=columns[2],
                location=columns[3],
            )
        )
    return packages


def parse_avds(stdout: str) -> Tuple[List[Avd], List[Avd]]:
    """Read `avdmanager list avd` into `(loadable, unloadable)`.

    The two sections carry different keys: loadable devices have `Target:`,
    unloadable ones have `Error:`. A device that failed to load is still
    reported, because "your AVD is broken" is information, but it is never
    treated as garbage the user obviously wants gone.
    """
    loadable: List[Avd] = []
    unloadable: List[Avd] = []
    section: Optional[str] = None
    current: Dict[str, str] = {}

    def flush() -> None:
        if not current.get("Name"):
            current.clear()
            return
        entry = Avd(
            name=current.get("Name", ""),
            path=current.get("Path", ""),
            target=current.get("Target", ""),
            error=current.get("Error", ""),
        )
        if section == "unloadable":
            unloadable.append(entry)
        elif section == "loadable":
            loadable.append(entry)
        current.clear()

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line.startswith(_AVAILABLE_HEADER):
            flush()
            section = "loadable"
            continue
        if _UNLOADABLE_HEADER in line:
            flush()
            section = "unloadable"
            continue
        if not line:
            continue
        if line.startswith("Name:"):
            flush()
        for key in ("Name", "Path", "Target", "Error"):
            prefix = key + ":"
            if line.startswith(prefix):
                current[key] = line[len(prefix):].strip()
                break

    flush()
    return loadable, unloadable


def _group_for(package_path: str) -> Optional[Tuple[str, str]]:
    for prefix, detector_id, label in _GROUPS:
        if package_path == prefix or package_path.startswith(prefix):
            return detector_id, label
    return None


def analyze_android(
    sdk_runner: ToolRunner,
    avd_runner: ToolRunner,
    generation: int,
    sdk_root: Optional[Path],
) -> Tuple[List[Recommendation], Optional[str]]:
    """Inventory the Android SDK and its virtual devices.

    Nothing here is ever selected by default and nothing is deleted by path: a
    toolchain version may be required by a project this scan never indexed, and
    an AVD holds device state the user cannot recreate.
    """
    if sdk_root is None:
        return [], "Android SDK not found"

    items: List[Recommendation] = []
    problems: List[str] = []

    try:
        sdk_result = sdk_runner(SDK_LIST_ARGV)
        if sdk_result.ok:
            for package in parse_sdk_packages(sdk_result.stdout):
                group = _group_for(package.path)
                if group is None:
                    continue
                detector_id, label = group
                location = Path(sdk_root) / package.location
                size = path_size(location) if location.is_dir() else 0
                if size <= 0:
                    continue
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
                        safety_root=Path(sdk_root),
                        reason="Installed SDK component. A project not covered by this scan may require it.",
                        generation=generation,
                        warning="Removing a toolchain version breaks any project pinned to it.",
                        tool_action=ToolAction(
                            tool="sdkmanager",
                            resource=package.path,
                            argv=("sdkmanager", "--uninstall", package.path),
                            preview_argv=SDK_LIST_ARGV,
                            reported=package.version,
                        ),
                    )
                )
        else:
            problems.append(
                sdk_result.stderr.strip() or "sdkmanager exited with {}".format(
                    sdk_result.exit_code
                )
            )
    except ToolUnavailable as exc:
        problems.append(exc.reason)

    try:
        avd_result = avd_runner(AVD_LIST_ARGV)
        if avd_result.ok:
            loadable, unloadable = parse_avds(avd_result.stdout)
            for avd in loadable:
                location = Path(avd.path)
                size = path_size(location) if location.is_dir() else 0
                items.append(
                    Recommendation(
                        detector_id="android-avd",
                        category="tool-managed",
                        label="Android virtual device {}".format(avd.name),
                        path=location,
                        action=ActionKind.INVOKE_TOOL,
                        allocated_bytes=size,
                        reclaimable_bytes=size,
                        confidence=Confidence.STRONG,
                        restoration=RestorationCost.EXTERNAL_STATE,
                        selected_by_default=False,
                        evidence=(
                            Evidence("tool-report", avd.target or "no target reported"),
                            Evidence("preview-command", " ".join(AVD_LIST_ARGV)),
                        ),
                        safety_root=location.parent if location.parent != location else location,
                        reason="Emulator device. Its installed apps and data are deleted with it.",
                        generation=generation,
                        warning="Device state inside the AVD cannot be recovered.",
                        tool_action=ToolAction(
                            tool="avdmanager",
                            resource=avd.name,
                            argv=("avdmanager", "delete", "avd", "-n", avd.name),
                            preview_argv=AVD_LIST_ARGV,
                            reported=avd.target,
                        ),
                    )
                )
            for avd in unloadable:
                location = Path(avd.path)
                size = path_size(location) if location.is_dir() else 0
                items.append(
                    Recommendation(
                        detector_id="android-avd-broken",
                        category="tool-managed",
                        label="Android virtual device {} (will not load)".format(avd.name),
                        path=location,
                        action=ActionKind.INVOKE_TOOL,
                        allocated_bytes=size,
                        reclaimable_bytes=size,
                        confidence=Confidence.HEURISTIC,
                        restoration=RestorationCost.EXTERNAL_STATE,
                        selected_by_default=False,
                        evidence=(
                            Evidence("tool-report", avd.error),
                            Evidence("preview-command", " ".join(AVD_LIST_ARGV)),
                        ),
                        safety_root=location.parent if location.parent != location else location,
                        reason="This device does not load. It may only need a missing SDK component rather than deletion.",
                        generation=generation,
                        warning="Device state inside the AVD cannot be recovered.",
                        tool_action=ToolAction(
                            tool="avdmanager",
                            resource=avd.name,
                            argv=("avdmanager", "delete", "avd", "-n", avd.name),
                            preview_argv=AVD_LIST_ARGV,
                            reported=avd.error,
                        ),
                    )
                )
        else:
            problems.append(
                avd_result.stderr.strip() or "avdmanager exited with {}".format(
                    avd_result.exit_code
                )
            )
    except ToolUnavailable as exc:
        problems.append(exc.reason)

    return items, "; ".join(problems) if problems else None
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_android_tool -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mac_dev_clean/tools/android.py tests/test_android_tool.py
git commit -m "feat: inventory the Android SDK and virtual devices through their own tools"
```

---

### Task 8: Simulator adapter and registry

**Files:**
- Create: `src/mac_dev_clean/tools/simulator.py`, `src/mac_dev_clean/tools/registry.py`
- Test: `tests/test_simulator_tool.py`, `tests/test_tool_registry.py`

**Interfaces:**
- Consumes: `sim_prune.load_inventory`, `sim_prune.select_unused_devices`, `SimctlError`; the four analyzers.
- Produces: `analyze_simulator(runner, generation, home) -> Tuple[List[Recommendation], Optional[str]]`; `ToolReport(recommendations, unavailable)` and `collect_tool_recommendations(generation, home, env=None, runners=None) -> ToolReport` in `registry.py`; `ToolStatus(tool, available, reason)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_simulator_tool.py`:

```python
from __future__ import annotations

import unittest
from pathlib import Path

from mac_dev_clean.recommendation import ActionKind
from mac_dev_clean.sim_prune import SimctlError
from mac_dev_clean.tools.simulator import XCRUN, analyze_simulator

HOME = Path("/Users/test/home")

DEVICES_JSON = """{
  "devices": {
    "com.apple.CoreSimulator.SimRuntime.iOS-17-0": [
      {"name": "iPhone 15", "udid": "11111111-1111-1111-1111-111111111111",
       "state": "Shutdown", "isAvailable": true,
       "dataPathSize": 4096, "logPathSize": 1024},
      {"name": "iPhone 12", "udid": "22222222-2222-2222-2222-222222222222",
       "state": "Shutdown", "isAvailable": false,
       "dataPathSize": 8192, "logPathSize": 2048},
      {"name": "iPhone 14", "udid": "33333333-3333-3333-3333-333333333333",
       "state": "Booted", "isAvailable": false,
       "dataPathSize": 16384, "logPathSize": 4096}
    ]
  }
}"""

RUNTIMES_JSON = '{"runtimes": []}'


def simctl_runner(devices=DEVICES_JSON, runtimes=RUNTIMES_JSON):
    def run(args):
        if "runtime" in args:
            return runtimes
        return devices

    return run


class AnalyzeSimulatorTests(unittest.TestCase):
    def test_unavailable_shutdown_devices_are_offered(self):
        items, unavailable = analyze_simulator(
            simctl_runner(), generation=1, home=HOME
        )

        self.assertIsNone(unavailable)
        self.assertTrue(any("iPhone 12" in item.label for item in items))

    def test_booted_devices_are_never_offered(self):
        items, _ = analyze_simulator(simctl_runner(), generation=1, home=HOME)

        self.assertFalse(any("iPhone 14" in item.label for item in items))

    def test_items_are_invoke_tool_and_unselected(self):
        items, _ = analyze_simulator(simctl_runner(), generation=1, home=HOME)

        for item in items:
            self.assertIs(item.action, ActionKind.INVOKE_TOOL)
            self.assertFalse(item.selected_by_default)

    def test_delete_vector_targets_the_udid(self):
        items, _ = analyze_simulator(simctl_runner(), generation=1, home=HOME)
        unavailable_device = next(item for item in items if "iPhone 12" in item.label)

        self.assertEqual(unavailable_device.tool_action.argv[0], XCRUN)
        self.assertEqual(
            unavailable_device.tool_action.argv[-2:],
            ("delete", "22222222-2222-2222-2222-222222222222"),
        )

    def test_size_comes_from_simctls_own_figures(self):
        items, _ = analyze_simulator(simctl_runner(), generation=1, home=HOME)
        unavailable_device = next(item for item in items if "iPhone 12" in item.label)

        self.assertEqual(unavailable_device.reclaimable_bytes, 8192 + 2048)

    def test_a_malformed_udid_is_never_offered(self):
        devices = DEVICES_JSON.replace(
            "22222222-2222-2222-2222-222222222222", "not-a-udid"
        )
        items, _ = analyze_simulator(
            simctl_runner(devices=devices), generation=1, home=HOME
        )

        self.assertFalse(any("iPhone 12" in item.label for item in items))

    def test_simctl_failure_is_reported_as_unavailable(self):
        def failing(args):
            raise SimctlError("simctl is unhappy")

        items, unavailable = analyze_simulator(failing, generation=1, home=HOME)

        self.assertEqual(items, [])
        self.assertIn("unhappy", unavailable)


if __name__ == "__main__":
    unittest.main()
```

Create `tests/test_tool_registry.py`:

```python
from __future__ import annotations

import unittest
from pathlib import Path

from mac_dev_clean.tools.registry import collect_tool_recommendations
from mac_dev_clean.tools.runner import ToolResult, ToolUnavailable

HOME = Path("/Users/test/home")

DOCKER_OUTPUT = (
    '{"Active":"0","Reclaimable":"7.499GB","Size":"18.92GB",'
    '"TotalCount":"144","Type":"Build Cache"}\n'
)
BREW_OUTPUT = "==> This operation would free approximately 799.8MB of disk space.\n"


def fixed(stdout: str, exit_code: int = 0, stderr: str = ""):
    def run(argv):
        return ToolResult(
            argv=tuple(argv), stdout=stdout, stderr=stderr, exit_code=exit_code
        )

    return run


def unavailable(reason: str):
    def run(argv):
        raise ToolUnavailable("tool", reason)

    return run


class CollectToolRecommendationsTests(unittest.TestCase):
    def test_collects_from_every_available_tool(self):
        report = collect_tool_recommendations(
            generation=1,
            home=HOME,
            env={},
            runners={
                "docker": fixed(DOCKER_OUTPUT),
                "brew": fixed(BREW_OUTPUT),
                "sdkmanager": unavailable("not installed"),
                "avdmanager": unavailable("not installed"),
                "simctl": unavailable("not installed"),
            },
        )

        detectors = {item.detector_id for item in report.recommendations}
        self.assertIn("docker-build-cache", detectors)
        self.assertIn("homebrew-cleanup", detectors)

    def test_an_unavailable_tool_is_recorded_not_hidden(self):
        report = collect_tool_recommendations(
            generation=1,
            home=HOME,
            env={},
            runners={
                "docker": unavailable("Cannot connect to the Docker daemon"),
                "brew": fixed(BREW_OUTPUT),
                "sdkmanager": unavailable("not installed"),
                "avdmanager": unavailable("not installed"),
                "simctl": unavailable("not installed"),
            },
        )

        docker_status = next(s for s in report.statuses if s.tool == "docker")
        self.assertFalse(docker_status.available)
        self.assertIn("Cannot connect", docker_status.reason)

    def test_one_failing_tool_does_not_suppress_the_others(self):
        report = collect_tool_recommendations(
            generation=1,
            home=HOME,
            env={},
            runners={
                "docker": unavailable("boom"),
                "brew": fixed(BREW_OUTPUT),
                "sdkmanager": unavailable("not installed"),
                "avdmanager": unavailable("not installed"),
                "simctl": unavailable("not installed"),
            },
        )

        self.assertTrue(
            any(item.detector_id == "homebrew-cleanup" for item in report.recommendations)
        )

    def test_every_tool_appears_in_statuses(self):
        report = collect_tool_recommendations(
            generation=1,
            home=HOME,
            env={},
            runners={
                "docker": fixed(DOCKER_OUTPUT),
                "brew": fixed(BREW_OUTPUT),
                "sdkmanager": unavailable("not installed"),
                "avdmanager": unavailable("not installed"),
                "simctl": unavailable("not installed"),
            },
        )

        self.assertEqual(
            sorted(status.tool for status in report.statuses),
            ["android", "docker", "homebrew", "simulator"],
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -m unittest tests.test_simulator_tool tests.test_tool_registry -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `simulator.py`**

Create `src/mac_dev_clean/tools/simulator.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from ..recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    ToolAction,
)
from ..sim_prune import SimctlError, is_safe_simctl_udid, load_inventory, run_simctl

XCRUN = "/usr/bin/xcrun"

SIMCTL_PREVIEW_ARGV = (XCRUN, "simctl", "list", "--json", "devices")


def analyze_simulator(
    runner=run_simctl, generation: int = 0, home: Optional[Path] = None
) -> Tuple[List[Recommendation], Optional[str]]:
    """Surface simulator devices simctl reports as unavailable and shut down.

    Booted devices are excluded outright: deleting a running device is the one
    simulator mistake that loses work in progress. A device whose udid does not
    match simctl's own format is skipped rather than passed through, because
    the udid becomes an argument to a delete command.

    Sizes come from simctl's `dataPathSize` and `logPathSize` rather than from
    walking the device directory. That keeps this analyzer free of filesystem
    traversal, and the warning states plainly that APFS clones make the figure
    an upper bound on what deletion actually frees.
    """
    root = Path(home).expanduser() if home is not None else Path.home()
    try:
        inventory = load_inventory(runner=runner)
    except SimctlError as exc:
        return [], str(exc)

    items: List[Recommendation] = []
    for device in inventory.devices:
        if device.state.lower() == "booted":
            continue
        if device.is_available:
            continue
        if not is_safe_simctl_udid(device.udid):
            continue

        size = device.total_size_bytes
        items.append(
            Recommendation(
                detector_id="simulator-unavailable-device",
                category="tool-managed",
                label="Simulator device {}".format(device.name),
                path=root,
                action=ActionKind.INVOKE_TOOL,
                allocated_bytes=size,
                reclaimable_bytes=size,
                confidence=Confidence.HEURISTIC,
                restoration=RestorationCost.EXTERNAL_STATE,
                selected_by_default=False,
                evidence=(
                    Evidence("tool-report", "simctl reports this device unavailable"),
                    Evidence("tool-report", "state is {}".format(device.state)),
                    Evidence("preview-command", " ".join(SIMCTL_PREVIEW_ARGV)),
                ),
                safety_root=root,
                reason="simctl reports this device as unavailable and shut down.",
                generation=generation,
                warning="Apps and data installed on the device are deleted with it. APFS clones mean reclaimed space may be less than the reported size.",
                tool_action=ToolAction(
                    tool="simctl",
                    resource=device.udid,
                    argv=(XCRUN, "simctl", "delete", device.udid),
                    preview_argv=SIMCTL_PREVIEW_ARGV,
                    reported=device.state,
                ),
            )
        )

    return items, None
```

`sim_prune.Device` was read directly to write this: its real fields are
`runtime_identifier`, `name`, `udid`, `state`, `is_available`,
`last_booted_at`, `data_size_bytes`, `log_size_bytes`, plus the
`total_size_bytes` property. There is no `data_path` or `log_path` — simctl
reports sizes, not paths, so `path` is set to the home root and the udid in
`tool_action.resource` is what identifies the device.

- [ ] **Step 4: Write `registry.py`**

Create `src/mac_dev_clean/tools/registry.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..recommendation import Recommendation
from .android import analyze_android, cmdline_tool_dirs, find_sdk_root
from .docker import analyze_docker
from .homebrew import analyze_homebrew
from .runner import ToolRunner, ToolUnavailable, find_binary, run_tool
from .simulator import analyze_simulator


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


def binary_runner(name: str, extra_dirs: Tuple[Path, ...] = ()) -> ToolRunner:
    """A runner that resolves `name` to a real binary before running it.

    Public because `tool_executor` needs the same resolution at execution time
    that the registry used at scan time; a second, subtly different lookup is
    how an executed command drifts from the one that was previewed.
    """

    def run(argv):
        vector = list(argv)
        resolved = find_binary(vector[0], extra_dirs=extra_dirs)
        if resolved is None:
            raise ToolUnavailable(vector[0], "not installed")
        return run_tool([resolved] + vector[1:])

    return run


def collect_tool_recommendations(
    generation: int,
    home: Path,
    env: Optional[Dict[str, str]] = None,
    runners: Optional[Dict[str, ToolRunner]] = None,
) -> ToolReport:
    """Ask every registered tool for its inventory.

    One tool's absence or failure never suppresses another's results: each
    analyzer is isolated, and every tool ends up in `statuses` whether it
    answered or not, so the UI can say "Docker is not running" instead of
    silently implying Docker uses no space.
    """
    environment = dict(os.environ) if env is None else dict(env)
    supplied = runners or {}
    sdk_root = find_sdk_root(environment, home)
    android_dirs = cmdline_tool_dirs(sdk_root) if sdk_root else ()

    def runner_for(name: str, extra_dirs: Tuple[Path, ...] = ()) -> ToolRunner:
        if name in supplied:
            return supplied[name]
        return binary_runner(name, extra_dirs=extra_dirs)

    recommendations: List[Recommendation] = []
    statuses: List[ToolStatus] = []

    docker_items, docker_reason = analyze_docker(
        runner_for("docker"), generation=generation, home=home
    )
    recommendations.extend(docker_items)
    statuses.append(ToolStatus("docker", docker_reason is None, docker_reason or ""))

    brew_items, brew_reason = analyze_homebrew(
        runner_for("brew"), generation=generation, home=home
    )
    recommendations.extend(brew_items)
    statuses.append(ToolStatus("homebrew", brew_reason is None, brew_reason or ""))

    android_items, android_reason = analyze_android(
        sdk_runner=runner_for("sdkmanager", android_dirs),
        avd_runner=runner_for("avdmanager", android_dirs),
        generation=generation,
        sdk_root=sdk_root,
    )
    recommendations.extend(android_items)
    statuses.append(ToolStatus("android", android_reason is None, android_reason or ""))

    if "simctl" in supplied:
        simulator_items, simulator_reason = analyze_simulator(
            supplied["simctl"], generation=generation, home=home
        )
    else:
        simulator_items, simulator_reason = analyze_simulator(
            generation=generation, home=home
        )
    recommendations.extend(simulator_items)
    statuses.append(
        ToolStatus("simulator", simulator_reason is None, simulator_reason or "")
    )

    return ToolReport(recommendations=recommendations, statuses=statuses)
```

The `simctl` fake in `tests/test_tool_registry.py` raises `ToolUnavailable`, but
`analyze_simulator` catches `SimctlError`. Make the registry's simulator branch
tolerate both by wrapping the call:

```python
    try:
        if "simctl" in supplied:
            simulator_items, simulator_reason = analyze_simulator(
                supplied["simctl"], generation=generation, home=home
            )
        else:
            simulator_items, simulator_reason = analyze_simulator(
                generation=generation, home=home
            )
    except ToolUnavailable as exc:
        simulator_items, simulator_reason = [], exc.reason
```

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_simulator_tool tests.test_tool_registry -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mac_dev_clean/tools/simulator.py src/mac_dev_clean/tools/registry.py tests/test_simulator_tool.py tests/test_tool_registry.py
git commit -m "feat: collect tool-managed storage from every available tool"
```

---

### Task 9: Tool executor

**Files:**
- Create: `src/mac_dev_clean/tool_executor.py`
- Test: `tests/test_tool_executor.py`

**Interfaces:**
- Consumes: `Recommendation`, `ToolAction`, `ActionKind`, `ActionJournal`, `JournalRecord`, `ToolRunner`, `ToolUnavailable`, `ApplyOutcome`, `ApplyResult`.
- Produces: `INVOKED` added to `ApplyOutcome` in `executor.py`; `invoke_tool_recommendations(ids, index, journal, runners=None, dry_run=False, now=None) -> List[ApplyResult]`.

- [ ] **Step 1: Extend `ApplyOutcome`**

In `src/mac_dev_clean/executor.py`, add to the enum:

```python
class ApplyOutcome(Enum):
    REMOVED = "removed"
    INVOKED = "invoked"
    SKIPPED = "skipped"
    CHANGED_SINCE_SCAN = "changed_since_scan"
    FAILED = "failed"
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_tool_executor.py`:

```python
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mac_dev_clean.executor import ApplyOutcome
from mac_dev_clean.fsevents import VolumeIdentity
from mac_dev_clean.index import open_index
from mac_dev_clean.journal import ActionJournal
from mac_dev_clean.recommendation import (
    ActionKind,
    Confidence,
    Evidence,
    Recommendation,
    RestorationCost,
    ToolAction,
)
from mac_dev_clean.tool_executor import invoke_tool_recommendations
from mac_dev_clean.tools.runner import ToolResult, ToolUnavailable

HOME = Path("/Users/test/home")

PREVIEW = (
    '{"Active":"0","Reclaimable":"7.499GB","Size":"18.92GB",'
    '"TotalCount":"144","Type":"Build Cache"}\n'
)


def build_item(generation: int = 1) -> Recommendation:
    return Recommendation(
        detector_id="docker-build-cache",
        category="tool-managed",
        label="Docker build cache",
        path=HOME,
        action=ActionKind.INVOKE_TOOL,
        allocated_bytes=18920000000,
        reclaimable_bytes=7499000000,
        confidence=Confidence.EXACT,
        restoration=RestorationCost.EXTERNAL_STATE,
        selected_by_default=False,
        evidence=(Evidence("tool-report", "docker reports 7.499GB reclaimable"),),
        safety_root=HOME,
        reason="",
        generation=generation,
        tool_action=ToolAction(
            tool="docker",
            resource="build-cache",
            argv=("docker", "builder", "prune", "-f"),
            preview_argv=("docker", "system", "df", "--format", "{{json .}}"),
            reported="7.499GB",
        ),
    )


class RecordingRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, argv):
        vector = tuple(argv)
        self.calls.append(vector)
        response = self.responses.get(vector)
        if response is None:
            raise AssertionError("unexpected vector {}".format(vector))
        if isinstance(response, Exception):
            raise response
        return response


def ok(stdout: str = "") -> ToolResult:
    return ToolResult(argv=("x",), stdout=stdout, stderr="", exit_code=0)


class InvokeToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index = open_index(Path(self.tmp.name) / "index.sqlite3")
        self.addCleanup(self.index.close)
        self.index.begin_generation(VolumeIdentity(device=1, uuid="U"), event_id=1)
        self.item = build_item()
        self.index.record_recommendation(self.item)
        self.index.complete_generation()
        self.journal_path = Path(self.tmp.name) / "actions.jsonl"
        self.journal = ActionJournal(self.journal_path)

    def records(self):
        text = self.journal_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def test_re_previews_before_executing(self):
        runner = RecordingRunner(
            {
                ("docker", "system", "df", "--format", "{{json .}}"): ok(PREVIEW),
                ("docker", "builder", "prune", "-f"): ok("Total reclaimed space: 7.499GB"),
            }
        )

        results = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.INVOKED)
        self.assertEqual(runner.calls[0][:3], ("docker", "system", "df"))
        self.assertEqual(runner.calls[1], ("docker", "builder", "prune", "-f"))

    def test_dry_run_previews_but_never_executes(self):
        runner = RecordingRunner(
            {("docker", "system", "df", "--format", "{{json .}}"): ok(PREVIEW)}
        )

        results = invoke_tool_recommendations(
            [self.item.id],
            self.index,
            self.journal,
            runners={"docker": runner},
            dry_run=True,
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.SKIPPED)
        self.assertNotIn(("docker", "builder", "prune", "-f"), runner.calls)

    def test_only_the_declared_vector_is_executed(self):
        runner = RecordingRunner(
            {
                ("docker", "system", "df", "--format", "{{json .}}"): ok(PREVIEW),
                ("docker", "builder", "prune", "-f"): ok(""),
            }
        )

        invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        for call in runner.calls:
            self.assertNotIn("-a", call)
            self.assertNotIn("--all", call)

    def test_non_zero_exit_is_failed_with_stderr(self):
        runner = RecordingRunner(
            {
                ("docker", "system", "df", "--format", "{{json .}}"): ok(PREVIEW),
                ("docker", "builder", "prune", "-f"): ToolResult(
                    argv=("x",), stdout="", stderr="permission denied", exit_code=1
                ),
            }
        )

        results = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.FAILED)
        self.assertIn("permission denied", results[0].error)

    def test_tool_gone_between_scan_and_action_is_failed(self):
        runner = RecordingRunner(
            {
                ("docker", "system", "df", "--format", "{{json .}}"): ToolUnavailable(
                    "docker", "Cannot connect to the Docker daemon"
                )
            }
        )

        results = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.FAILED)
        self.assertIn("Cannot connect", results[0].error)

    def test_nothing_left_to_reclaim_is_skipped(self):
        empty = (
            '{"Active":"0","Reclaimable":"0B","Size":"0B",'
            '"TotalCount":"0","Type":"Build Cache"}\n'
        )
        runner = RecordingRunner(
            {("docker", "system", "df", "--format", "{{json .}}"): ok(empty)}
        )

        results = invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.SKIPPED)
        self.assertNotIn(("docker", "builder", "prune", "-f"), runner.calls)

    def test_unknown_id_is_failed_and_journalled(self):
        results = invoke_tool_recommendations(
            ["deadbeefdeadbeef"], self.index, self.journal, runners={}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.FAILED)
        self.assertEqual(len(self.records()), 1)

    def test_a_path_recommendation_is_refused(self):
        index2 = open_index(Path(self.tmp.name) / "index2.sqlite3")
        self.addCleanup(index2.close)
        index2.begin_generation(VolumeIdentity(device=1, uuid="U"), event_id=1)
        path_item = Recommendation(
            detector_id="node-modules",
            category="project-dependencies",
            label="node_modules",
            path=HOME / "app" / "node_modules",
            action=ActionKind.DELETE_TREE,
            allocated_bytes=1,
            reclaimable_bytes=1,
            confidence=Confidence.STRONG,
            restoration=RestorationCost.REDOWNLOAD,
            selected_by_default=True,
            evidence=(),
            safety_root=HOME / "app",
            reason="",
            generation=1,
        )
        index2.record_recommendation(path_item)
        index2.complete_generation()

        results = invoke_tool_recommendations(
            [path_item.id], index2, self.journal, runners={}
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.FAILED)


class JournalIntegrationTests(InvokeToolTests):
    def test_success_is_journalled_with_the_executed_vector(self):
        runner = RecordingRunner(
            {
                ("docker", "system", "df", "--format", "{{json .}}"): ok(PREVIEW),
                ("docker", "builder", "prune", "-f"): ok(""),
            }
        )

        invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        record = self.records()[-1]
        self.assertEqual(record["outcome"], "invoked")
        self.assertEqual(record["argv"], ["docker", "builder", "prune", "-f"])
        self.assertFalse(record["dry_run"])

    def test_dry_run_is_journalled_as_a_dry_run(self):
        runner = RecordingRunner(
            {("docker", "system", "df", "--format", "{{json .}}"): ok(PREVIEW)}
        )

        invoke_tool_recommendations(
            [self.item.id],
            self.index,
            self.journal,
            runners={"docker": runner},
            dry_run=True,
        )

        self.assertTrue(self.records()[-1]["dry_run"])

    def test_failure_is_journalled_with_its_reason(self):
        runner = RecordingRunner(
            {
                ("docker", "system", "df", "--format", "{{json .}}"): ToolUnavailable(
                    "docker", "daemon stopped"
                )
            }
        )

        invoke_tool_recommendations(
            [self.item.id], self.index, self.journal, runners={"docker": runner}
        )

        record = self.records()[-1]
        self.assertEqual(record["outcome"], "failed")
        self.assertIn("daemon stopped", record["detail"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_tool_executor -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mac_dev_clean.tool_executor'`

- [ ] **Step 4: Write the implementation**

Create `src/mac_dev_clean/tool_executor.py`:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

from .executor import ApplyOutcome, ApplyResult
from .index import ScanIndex
from .journal import ActionJournal, JournalRecord
from .recommendation import ActionKind, Recommendation, normalized_path
from .tools.homebrew import parse_brew_preview
from .tools.registry import binary_runner
from .tools.runner import ToolRunner, ToolUnavailable
from .tools.sizes import parse_tool_size


def _journal(
    journal: ActionJournal,
    item: Optional[Recommendation],
    recommendation_id: str,
    outcome: ApplyOutcome,
    dry_run: bool,
    detail: str,
    argv: Sequence[str] = (),
) -> None:
    record = JournalRecord(
        recommendation_id=recommendation_id,
        detector_id=item.detector_id if item else "",
        category=item.category if item else "",
        target=(
            "{}:{}".format(item.tool_action.tool, item.tool_action.resource)
            if item is not None and item.tool_action is not None
            else (normalized_path(item.path) if item is not None else "")
        ),
        action=item.action.value if item else ActionKind.INVOKE_TOOL.value,
        outcome=outcome.value,
        reclaimable_bytes=item.reclaimable_bytes if item else 0,
        dry_run=dry_run,
        argv=tuple(argv),
        detail=detail,
    )
    journal.append(record)


def _current_reclaimable(item: Recommendation, runner: ToolRunner) -> Optional[int]:
    """Re-run the recommendation's own preview and read the current figure.

    Returns None when the preview could not be read at all, which the caller
    treats as "do not act". A tool's internal state can change between the scan
    and the confirmation in ways no filesystem check can detect, so acting on
    the scan-time figure would execute a command whose effect the user was
    never shown.
    """
    action = item.tool_action
    if action is None:
        return None

    result = runner(action.preview_argv)
    if not result.ok:
        return None

    if action.tool == "docker":
        for line in result.lines():
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if _docker_resource(payload) == action.resource:
                return parse_tool_size(str(payload.get("Reclaimable", "")))
        return 0

    if action.tool == "brew":
        total, _ = parse_brew_preview(result.stdout)
        return total

    # Android and simulator previews are inventories rather than size reports.
    # Presence in the current inventory is the check; the figure is unchanged.
    return item.reclaimable_bytes if action.resource in result.stdout else 0


def _docker_resource(payload: Dict[str, object]) -> str:
    mapping = {
        "Build Cache": "build-cache",
        "Images": "images",
        "Containers": "containers",
        "Local Volumes": "volumes",
    }
    return mapping.get(str(payload.get("Type", "")), "")


def invoke_tool_recommendations(
    recommendation_ids: Sequence[str],
    index: ScanIndex,
    journal: ActionJournal,
    runners: Optional[Dict[str, ToolRunner]] = None,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> List[ApplyResult]:
    """Execute tool-managed recommendations by ID.

    Authority comes from the index plus a fresh preview, never from a caller
    supplied path or command. Every branch journals before returning, so a
    crash between the action and the report cannot hide what happened.
    """
    supplied = runners or {}
    results: List[ApplyResult] = []

    for recommendation_id in recommendation_ids:
        if should_cancel is not None and should_cancel():
            break

        item = index.load_recommendation(recommendation_id)
        if item is None:
            _journal(
                journal,
                None,
                recommendation_id,
                ApplyOutcome.FAILED,
                dry_run,
                "recommendation not found in the current scan",
            )
            results.append(
                ApplyResult(
                    recommendation_id=recommendation_id,
                    label="",
                    path="",
                    reclaimable_bytes=0,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=dry_run,
                    error="recommendation not found in the current scan",
                )
            )
            continue

        action = item.tool_action
        if item.action is not ActionKind.INVOKE_TOOL or action is None:
            detail = "not a tool-managed recommendation"
            _journal(journal, item, recommendation_id, ApplyOutcome.FAILED, dry_run, detail)
            results.append(
                ApplyResult(
                    recommendation_id=recommendation_id,
                    label=item.label,
                    path=normalized_path(item.path),
                    reclaimable_bytes=item.reclaimable_bytes,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=dry_run,
                    error=detail,
                )
            )
            continue

        runner = supplied.get(action.tool) or binary_runner(action.argv[0])

        try:
            current = _current_reclaimable(item, runner)
        except ToolUnavailable as exc:
            _journal(
                journal, item, recommendation_id, ApplyOutcome.FAILED, dry_run, exc.reason
            )
            results.append(
                ApplyResult(
                    recommendation_id=recommendation_id,
                    label=item.label,
                    path=normalized_path(item.path),
                    reclaimable_bytes=item.reclaimable_bytes,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=dry_run,
                    error=exc.reason,
                )
            )
            continue

        if current is None:
            detail = "could not re-read the tool's current state"
            _journal(journal, item, recommendation_id, ApplyOutcome.FAILED, dry_run, detail)
            results.append(
                ApplyResult(
                    recommendation_id=recommendation_id,
                    label=item.label,
                    path=normalized_path(item.path),
                    reclaimable_bytes=item.reclaimable_bytes,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=dry_run,
                    error=detail,
                )
            )
            continue

        if current <= 0:
            detail = "the tool now reports nothing to reclaim"
            _journal(
                journal, item, recommendation_id, ApplyOutcome.SKIPPED, dry_run, detail
            )
            results.append(
                ApplyResult(
                    recommendation_id=recommendation_id,
                    label=item.label,
                    path=normalized_path(item.path),
                    reclaimable_bytes=0,
                    outcome=ApplyOutcome.SKIPPED,
                    dry_run=dry_run,
                    error=detail,
                )
            )
            continue

        if dry_run:
            detail = "dry run: {} would run".format(" ".join(action.argv))
            _journal(
                journal,
                item,
                recommendation_id,
                ApplyOutcome.SKIPPED,
                True,
                detail,
                argv=action.argv,
            )
            results.append(
                ApplyResult(
                    recommendation_id=recommendation_id,
                    label=item.label,
                    path=normalized_path(item.path),
                    reclaimable_bytes=current,
                    outcome=ApplyOutcome.SKIPPED,
                    dry_run=True,
                    error=detail,
                )
            )
            continue

        try:
            executed = runner(action.argv)
        except ToolUnavailable as exc:
            _journal(
                journal,
                item,
                recommendation_id,
                ApplyOutcome.FAILED,
                False,
                exc.reason,
                argv=action.argv,
            )
            results.append(
                ApplyResult(
                    recommendation_id=recommendation_id,
                    label=item.label,
                    path=normalized_path(item.path),
                    reclaimable_bytes=current,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=False,
                    error=exc.reason,
                )
            )
            continue

        if not executed.ok:
            detail = executed.stderr.strip() or "exited with {}".format(
                executed.exit_code
            )
            _journal(
                journal,
                item,
                recommendation_id,
                ApplyOutcome.FAILED,
                False,
                detail,
                argv=action.argv,
            )
            results.append(
                ApplyResult(
                    recommendation_id=recommendation_id,
                    label=item.label,
                    path=normalized_path(item.path),
                    reclaimable_bytes=current,
                    outcome=ApplyOutcome.FAILED,
                    dry_run=False,
                    error=detail,
                )
            )
            continue

        detail = executed.stdout.strip().splitlines()[-1] if executed.stdout.strip() else ""
        _journal(
            journal,
            item,
            recommendation_id,
            ApplyOutcome.INVOKED,
            False,
            detail,
            argv=action.argv,
        )
        results.append(
            ApplyResult(
                recommendation_id=recommendation_id,
                label=item.label,
                path=normalized_path(item.path),
                reclaimable_bytes=current,
                outcome=ApplyOutcome.INVOKED,
                dry_run=False,
                error="",
            )
        )

    return results
```

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_tool_executor -v`
Expected: PASS. If `_current_reclaimable`'s Docker branch is awkward, simplify it to use `_docker_resource(payload) == action.resource` only, and delete the duplicated inline mapping expression.

- [ ] **Step 6: Commit**

```bash
git add src/mac_dev_clean/tool_executor.py src/mac_dev_clean/executor.py tests/test_tool_executor.py
git commit -m "feat: execute tool-managed actions after a fresh preview"
```

---

### Task 10: Journal every existing action path

**Files:**
- Modify: `src/mac_dev_clean/cleaner.py`, `src/mac_dev_clean/executor.py`
- Test: `tests/test_scanner_cleaner.py` (append), `tests/test_executor.py` (append)

**Interfaces:**
- Consumes: `ActionJournal`, `JournalRecord`.
- Produces: `clean_target(target, dry_run=False, journal=None)` and `clean_targets(targets, dry_run=False, journal=None)` keep their existing positional signatures; `apply_recommendations(..., journal=None)` gains a keyword.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scanner_cleaner.py`:

```python
class CleanerJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal_path = Path(self.tmp.name) / "actions.jsonl"

    def journal(self):
        from mac_dev_clean.journal import ActionJournal

        return ActionJournal(self.journal_path)

    def records(self):
        import json

        text = self.journal_path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def build_target(self, root: Path) -> ScanTarget:
        cache = root / "Library" / "Caches" / "Homebrew"
        cache.mkdir(parents=True)
        (cache / "blob").write_bytes(b"x" * 16)
        return ScanTarget(
            category="brew-cache",
            label="Homebrew cache",
            path=cache,
            size_bytes=16,
            modified_at=None,
            cleanable=True,
            delete_mode="contents",
            safety_root=root,
        )

    def test_successful_cleanup_is_journalled(self):
        root = Path(self.tmp.name) / "home"
        root.mkdir()
        target = self.build_target(root)

        clean_target(target, dry_run=False, journal=self.journal())

        record = self.records()[-1]
        self.assertEqual(record["outcome"], "removed")
        self.assertEqual(record["category"], "brew-cache")
        self.assertFalse(record["dry_run"])

    def test_dry_run_is_journalled_distinctly(self):
        root = Path(self.tmp.name) / "home2"
        root.mkdir()
        target = self.build_target(root)

        clean_target(target, dry_run=True, journal=self.journal())

        record = self.records()[-1]
        self.assertTrue(record["dry_run"])
        self.assertEqual(record["outcome"], "skipped")

    def test_a_refused_target_is_journalled_with_its_reason(self):
        root = Path(self.tmp.name) / "home3"
        root.mkdir()
        target = ScanTarget(
            category="xcode-archives",
            label="Xcode Archives",
            path=root / "Archives",
            size_bytes=1,
            modified_at=None,
            cleanable=False,
            delete_mode="none",
            safety_root=root,
        )

        clean_target(target, dry_run=False, journal=self.journal())

        record = self.records()[-1]
        self.assertEqual(record["outcome"], "failed")
        self.assertIn("report-only", record["detail"])

    def test_cleanup_without_a_journal_still_works(self):
        root = Path(self.tmp.name) / "home4"
        root.mkdir()
        target = self.build_target(root)

        result = clean_target(target, dry_run=False)

        self.assertTrue(result.removed)
```

Ensure `tempfile`, `Path`, `ScanTarget`, and `clean_target` are imported at the
top of `tests/test_scanner_cleaner.py`; add any that are missing.

Append to `tests/test_executor.py`:

```python
class ApplyJournalTests(unittest.TestCase):
    def test_a_refused_apply_is_journalled(self):
        import json
        import tempfile
        from pathlib import Path

        from mac_dev_clean.journal import ActionJournal

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        journal_path = Path(tmp.name) / "actions.jsonl"
        index = open_index(Path(tmp.name) / "index.sqlite3")
        self.addCleanup(index.close)

        results = apply_recommendations(
            ["deadbeefdeadbeef"],
            index,
            dry_run=False,
            journal=ActionJournal(journal_path),
        )

        self.assertEqual(results[0].outcome, ApplyOutcome.FAILED)
        record = json.loads(journal_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["outcome"], "failed")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -m unittest tests.test_scanner_cleaner tests.test_executor -v`
Expected: FAIL with `TypeError: clean_target() got an unexpected keyword argument 'journal'`

- [ ] **Step 3: Wire the journal into `cleaner.py`**

Change the two entry points, keeping the journal optional so no existing caller breaks:

```python
def clean_targets(
    targets: Iterable[ScanTarget],
    dry_run: bool = False,
    journal: Optional["ActionJournal"] = None,
) -> List[CleanResult]:
    return [clean_target(target, dry_run=dry_run, journal=journal) for target in targets]


def clean_target(
    target: ScanTarget,
    dry_run: bool = False,
    journal: Optional["ActionJournal"] = None,
) -> CleanResult:
    result = _clean_target_uncounted(target, dry_run=dry_run)
    if journal is not None:
        journal.append(
            JournalRecord(
                recommendation_id="",
                detector_id=target.category,
                category=target.category,
                target=str(target.path),
                action=target.delete_mode,
                outcome=_outcome_for(result, dry_run),
                reclaimable_bytes=target.size_bytes,
                dry_run=dry_run,
                detail=result.error,
            )
        )
    return result
```

Rename the current body of `clean_target` to `_clean_target_uncounted`, and add:

```python
def _outcome_for(result: CleanResult, dry_run: bool) -> str:
    if result.error:
        return "failed"
    if dry_run:
        return "skipped"
    return "removed" if result.removed else "skipped"
```

Add `from .journal import ActionJournal, JournalRecord` and `Optional` to the
imports.

- [ ] **Step 4: Wire the journal into `executor.py`**

Add a `journal: Optional[ActionJournal] = None` keyword to
`apply_recommendations`, and before each `results.append(...)`, append a
`JournalRecord` built from the same fields. Extract a small local helper so the
five call sites do not repeat the construction:

```python
    def record(result: ApplyResult, detector_id: str = "", category: str = "") -> None:
        if journal is None:
            return
        journal.append(
            JournalRecord(
                recommendation_id=result.recommendation_id,
                detector_id=detector_id,
                category=category,
                target=result.path,
                action=ActionKind.DELETE_TREE.value,
                outcome=result.outcome.value,
                reclaimable_bytes=result.reclaimable_bytes,
                dry_run=result.dry_run,
                detail=result.error,
            )
        )
```

Call `record(...)` immediately before every `results.append(...)`, passing
`item.detector_id` and `item.category` where an `item` is in scope.

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=src python3 -m unittest discover -s tests 2>&1 | tail -5`
Expected: OK.

- [ ] **Step 6: Commit**

```bash
git add src/mac_dev_clean/cleaner.py src/mac_dev_clean/executor.py tests/test_scanner_cleaner.py tests/test_executor.py
git commit -m "feat: journal every cleanup and apply attempt"
```

---

### Task 11: CLI commands

**Files:**
- Modify: `src/mac_dev_clean/cli.py`, `src/mac_dev_clean/output.py`
- Test: `tests/test_tools_cli.py`

**Interfaces:**
- Consumes: `collect_tool_recommendations`, `invoke_tool_recommendations`, `open_journal`.
- Produces: `mac-dev-clean tools [--json]`, `mac-dev-clean tools-apply --id <id> [--dry-run] [--json]`, `mac-dev-clean journal [--tail N] [--json]`, `mac-dev-clean journal --clear`; `render_tool_table(report) -> str` and `tool_report_json(report) -> dict` in `output.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tools_cli.py`:

```python
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from mac_dev_clean.cli import main


class ToolsCommandTests(unittest.TestCase):
    def test_tools_json_lists_statuses(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["tools", "--json"])

        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertIn("statuses", payload)
        self.assertIn("recommendations", payload)
        tools = {status["tool"] for status in payload["statuses"]}
        self.assertEqual(tools, {"docker", "homebrew", "android", "simulator"})

    def test_tools_human_output_mentions_every_tool(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            main(["tools"])

        text = buffer.getvalue()
        for name in ("docker", "homebrew", "android", "simulator"):
            self.assertIn(name, text.lower())


class ToolsApplyArgumentTests(unittest.TestCase):
    def test_tools_apply_requires_an_id(self):
        with self.assertRaises(SystemExit):
            main(["tools-apply"])

    def test_unknown_id_reports_failure(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                [
                    "tools-apply",
                    "--id",
                    "deadbeefdeadbeef",
                    "--json",
                    "--index",
                    str(Path(tmp.name) / "index.sqlite3"),
                    "--journal",
                    str(Path(tmp.name) / "actions.jsonl"),
                ]
            )

        self.assertEqual(code, 1)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["results"][0]["outcome"], "failed")


class JournalCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "actions.jsonl"
        self.path.write_text(
            '{"timestamp":"2026-09-07T10:00:00+00:00","outcome":"removed",'
            '"target":"/Users/test/app/node_modules","action":"delete_tree",'
            '"dry_run":false,"detail":"","argv":[],"category":"c",'
            '"detector_id":"d","recommendation_id":"r","reclaimable_bytes":1}\n',
            encoding="utf-8",
        )

    def test_journal_json_returns_records(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["journal", "--json", "--journal", str(self.path)])

        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["records"][0]["outcome"], "removed")

    def test_journal_tail_limits_output(self):
        with open(str(self.path), "a", encoding="utf-8") as handle:
            handle.write(
                '{"timestamp":"2026-09-07T11:00:00+00:00","outcome":"failed",'
                '"target":"x","action":"delete_tree","dry_run":false,"detail":"",'
                '"argv":[],"category":"c","detector_id":"d",'
                '"recommendation_id":"r","reclaimable_bytes":1}\n'
            )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            main(["journal", "--json", "--tail", "1", "--journal", str(self.path)])

        payload = json.loads(buffer.getvalue())
        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["records"][0]["outcome"], "failed")

    def test_journal_clear_removes_the_file(self):
        code = main(["journal", "--clear", "--journal", str(self.path)])

        self.assertEqual(code, 0)
        self.assertFalse(self.path.exists())

    def test_missing_journal_is_not_an_error(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(
                ["journal", "--json", "--journal", str(Path(self.tmp.name) / "none.jsonl")]
            )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["records"], [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.test_tools_cli -v`
Expected: FAIL — `tools` is not a known command.

- [ ] **Step 3: Add the output renderers**

In `src/mac_dev_clean/output.py`, add:

```python
def tool_report_json(report) -> Dict[str, object]:
    return {
        "recommendations": [item.to_dict() for item in report.recommendations],
        "statuses": [status.to_dict() for status in report.statuses],
        "reclaimable_total_bytes": sum(
            item.reclaimable_bytes for item in report.recommendations
        ),
    }


def render_tool_table(report) -> str:
    lines: List[str] = ["Tool-managed storage", ""]
    for status in report.statuses:
        if status.available:
            lines.append("{}: available".format(status.tool))
        else:
            lines.append("{}: unavailable ({})".format(status.tool, status.reason))
    lines.append("")

    if not report.recommendations:
        lines.append("No tool reported reclaimable storage.")
        return "\n".join(lines)

    for item in sorted(
        report.recommendations, key=lambda entry: entry.reclaimable_bytes, reverse=True
    ):
        action = item.tool_action
        lines.append(
            "{:>10}  {}  [{}]".format(
                human_bytes(item.reclaimable_bytes), item.label, item.id
            )
        )
        lines.append("            runs: {}".format(" ".join(action.argv)))
    total = sum(item.reclaimable_bytes for item in report.recommendations)
    lines.append("")
    lines.append("Reported reclaimable: {}".format(human_bytes(total)))
    lines.append("Nothing is selected. Apply one with: mac-dev-clean tools-apply --id <id> --dry-run")
    return "\n".join(lines)
```

Add `Dict` to the `typing` import in `output.py` if it is not already there.

- [ ] **Step 4: Add the CLI commands**

In `src/mac_dev_clean/cli.py`, register three subparsers inside `build_parser`:

```python
    tools_parser = subparsers.add_parser(
        "tools",
        help="Report storage managed by Docker, Homebrew, Android tools, and simctl.",
    )
    tools_parser.add_argument("--json", action="store_true", help="Print JSON output.")

    tools_apply_parser = subparsers.add_parser(
        "tools-apply",
        help="Run one tool-managed action by recommendation id.",
    )
    tools_apply_parser.add_argument("--id", required=True, help="Recommendation id.")
    tools_apply_parser.add_argument("--index", help="Path to the scan index.")
    tools_apply_parser.add_argument("--journal", help="Path to the action journal.")
    tools_apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the command without running it.",
    )
    tools_apply_parser.add_argument("--json", action="store_true", help="Print JSON output.")

    journal_parser = subparsers.add_parser(
        "journal",
        help="Show or clear the local action journal.",
    )
    journal_parser.add_argument("--journal", help="Path to the action journal.")
    journal_parser.add_argument("--tail", type=int, help="Show only the last N records.")
    journal_parser.add_argument(
        "--clear", action="store_true", help="Delete the journal file."
    )
    journal_parser.add_argument("--json", action="store_true", help="Print JSON output.")
```

Add the dispatch in `main`:

```python
        if args.command == "tools":
            return run_tools(args)
        if args.command == "tools-apply":
            return run_tools_apply(args)
        if args.command == "journal":
            return run_journal(args)
```

Add the three handlers:

```python
def run_tools(args) -> int:
    from .tools.registry import collect_tool_recommendations

    report = collect_tool_recommendations(generation=0, home=Path.home())
    if args.json:
        print(json.dumps(tool_report_json(report), indent=2, sort_keys=True))
    else:
        print(render_tool_table(report))
    return 0


def run_tools_apply(args) -> int:
    from .journal import open_journal
    from .tool_executor import invoke_tool_recommendations

    index = open_index(Path(args.index) if args.index else None)
    journal = open_journal(Path(args.journal) if args.journal else None)
    try:
        results = invoke_tool_recommendations(
            [args.id], index, journal, dry_run=args.dry_run
        )
    finally:
        index.close()

    payload = {"results": [result.to_dict() for result in results]}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for result in results:
            print("{}: {}".format(result.outcome.value, result.label or args.id))
            if result.error:
                print("  {}".format(result.error))

    return 0 if all(
        result.outcome in {ApplyOutcome.INVOKED, ApplyOutcome.SKIPPED}
        for result in results
    ) else 1


def run_journal(args) -> int:
    from .journal import open_journal

    journal = open_journal(Path(args.journal) if args.journal else None)

    if args.clear:
        try:
            journal.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print("could not clear the journal: {}".format(exc))
            return 1
        return 0

    records = []
    try:
        with open(str(journal.path), "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        records = []

    if args.tail:
        records = records[-args.tail:]

    if args.json:
        print(json.dumps({"records": records}, indent=2, sort_keys=True))
    else:
        for record in records:
            print(
                "{}  {:<18}  {}  {}".format(
                    record.get("timestamp", ""),
                    record.get("outcome", ""),
                    "(dry run)" if record.get("dry_run") else "         ",
                    record.get("target", ""),
                )
            )
    return 0
```

Import `render_tool_table` and `tool_report_json` from `.output`, and
`ApplyOutcome` is already imported.

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=src python3 -m unittest tests.test_tools_cli -v`
Expected: PASS. The `tools` tests run against the real machine's tools but only read; if a tool is present they still pass because the assertions are about structure, not content.

- [ ] **Step 6: Commit**

```bash
git add src/mac_dev_clean/cli.py src/mac_dev_clean/output.py tests/test_tools_cli.py
git commit -m "feat: add tools, tools-apply, and journal commands"
```

---

### Task 12: Register the new modules with the Xcode project

**Files:**
- Modify: `macos/project.yml`
- Regenerate: `macos/MacDevClean.xcodeproj`

The post-build script copies `src/mac_dev_clean/*.py` by glob, but Xcode's
input/output file lists are explicit. A module missing from those lists builds
fine under `swift test` and fails under `xcodebuild` with a stale-output error —
so this task is mandatory, not cosmetic.

The `tools/` subpackage is new: the existing script copies only `*.py` from the
package root and will not copy a subdirectory. The script must be extended.

- [ ] **Step 1: Add the new root modules to both lists**

In `macos/project.yml`, add to `inputFiles` (keeping alphabetical order):

```
          - $(SRCROOT)/../src/mac_dev_clean/journal.py
          - $(SRCROOT)/../src/mac_dev_clean/tool_executor.py
```

and the matching entries to `outputFiles`:

```
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/journal.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/tool_executor.py
```

- [ ] **Step 2: Add the tools subpackage to both lists**

Add to `inputFiles`:

```
          - $(SRCROOT)/../src/mac_dev_clean/tools/__init__.py
          - $(SRCROOT)/../src/mac_dev_clean/tools/android.py
          - $(SRCROOT)/../src/mac_dev_clean/tools/docker.py
          - $(SRCROOT)/../src/mac_dev_clean/tools/homebrew.py
          - $(SRCROOT)/../src/mac_dev_clean/tools/registry.py
          - $(SRCROOT)/../src/mac_dev_clean/tools/runner.py
          - $(SRCROOT)/../src/mac_dev_clean/tools/simulator.py
          - $(SRCROOT)/../src/mac_dev_clean/tools/sizes.py
```

and to `outputFiles`:

```
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/tools/__init__.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/tools/android.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/tools/docker.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/tools/homebrew.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/tools/registry.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/tools/runner.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/tools/simulator.py
          - $(TARGET_BUILD_DIR)/$(UNLOCALIZED_RESOURCES_FOLDER_PATH)/python/mac_dev_clean/tools/sizes.py
```

- [ ] **Step 3: Extend the copy script to include the subpackage**

Replace the `script:` body with:

```
          ENGINE_SOURCE="${SRCROOT}/../src/mac_dev_clean"
          ENGINE_DEST="${TARGET_BUILD_DIR}/${UNLOCALIZED_RESOURCES_FOLDER_PATH}/python/mac_dev_clean"
          /bin/mkdir -p "${ENGINE_DEST}/tools"
          /bin/cp "${ENGINE_SOURCE}"/*.py "${ENGINE_DEST}/"
          /bin/cp "${ENGINE_SOURCE}"/tools/*.py "${ENGINE_DEST}/tools/"
```

- [ ] **Step 4: Regenerate and verify**

```bash
cd macos && xcodegen generate
```

- [ ] **Step 5: Build the app bundle and confirm the engine landed**

```bash
./scripts/build_macos_app.sh
ls dist/mac-dev-clean.app/Contents/Resources/python/mac_dev_clean/tools/
```

Expected: the eight `tools/*.py` files are present. If the directory is
missing, the copy script edit did not take; do not proceed until it does.

- [ ] **Step 6: Commit**

```bash
git add macos/project.yml macos/MacDevClean.xcodeproj
git commit -m "build: bundle the tools subpackage with the native app"
```

---

### Task 13: Native Tool-managed Storage section

**Files:**
- Modify: `macos/Sources/MacDevCleanApp/DeepScanModels.swift`, `macos/Sources/MacDevCleanApp/Backend.swift`, `macos/Sources/MacDevCleanApp/AppModel.swift`, `macos/Sources/MacDevCleanApp/ContentView.swift`
- Test: `macos/Tests/MacDevCleanAppTests/ToolsTests.swift`

**Interfaces:**
- Consumes: the `tools --json` payload from Task 11.
- Produces: `ToolStatus`, `ToolAction`, `ToolRecommendation` Swift models; `Backend.loadTools() async throws -> ToolReport`; a `SidebarPage.tools` case labelled "Tool-managed".

- [ ] **Step 1: Write the failing Swift test**

Create `macos/Tests/MacDevCleanAppTests/ToolsTests.swift`:

```swift
import XCTest
@testable import MacDevCleanApp

final class ToolReportDecodingTests: XCTestCase {
    func testDecodesStatusesAndRecommendations() throws {
        let json = """
        {
          "reclaimable_total_bytes": 7499000000,
          "statuses": [
            {"tool": "docker", "available": true, "reason": ""},
            {"tool": "homebrew", "available": false, "reason": "not installed"}
          ],
          "recommendations": [
            {
              "id": "abc123",
              "detector_id": "docker-build-cache",
              "category": "tool-managed",
              "label": "Docker build cache",
              "path": "/Users/test/home",
              "action": "invoke_tool",
              "allocated_bytes": 18920000000,
              "reclaimable_bytes": 7499000000,
              "size": "7.0 GB",
              "confidence": "exact",
              "restoration": "external_state",
              "selected_by_default": false,
              "evidence": [{"code": "tool-report", "detail": "docker reports 7.499GB reclaimable"}],
              "safety_root": "/Users/test/home",
              "reason": "Docker reports reclaimable build cache.",
              "warning": "",
              "generation": 0,
              "last_activity_at": null,
              "tool_action": {
                "tool": "docker",
                "resource": "build-cache",
                "argv": ["docker", "builder", "prune", "-f"],
                "preview_argv": ["docker", "system", "df", "--format", "{{json .}}"],
                "reported": "7.499GB"
              }
            }
          ]
        }
        """

        let report = try JSONDecoder().decode(ToolReport.self, from: Data(json.utf8))

        XCTAssertEqual(report.statuses.count, 2)
        XCTAssertFalse(report.statuses[1].available)
        XCTAssertEqual(report.statuses[1].reason, "not installed")
        XCTAssertEqual(report.recommendations.count, 1)
        XCTAssertEqual(
            report.recommendations[0].toolAction?.argv,
            ["docker", "builder", "prune", "-f"]
        )
    }

    func testUnavailableToolsAreStillPresented() throws {
        let json = """
        {"reclaimable_total_bytes": 0,
         "statuses": [{"tool": "docker", "available": false, "reason": "daemon stopped"}],
         "recommendations": []}
        """

        let report = try JSONDecoder().decode(ToolReport.self, from: Data(json.utf8))

        XCTAssertEqual(report.statuses.count, 1)
        XCTAssertTrue(report.recommendations.isEmpty)
    }

    func testNothingIsSelectedByDefault() throws {
        let json = """
        {"reclaimable_total_bytes": 1,
         "statuses": [],
         "recommendations": [
           {"id": "x", "detector_id": "d", "category": "tool-managed", "label": "L",
            "path": "/Users/test/home", "action": "invoke_tool", "allocated_bytes": 1,
            "reclaimable_bytes": 1, "size": "1 B", "confidence": "exact",
            "restoration": "external_state", "selected_by_default": false,
            "evidence": [], "safety_root": "/Users/test/home", "reason": "",
            "warning": "", "generation": 0, "last_activity_at": null,
            "tool_action": {"tool": "t", "resource": "r", "argv": ["t"],
                            "preview_argv": ["t"], "reported": ""}}
         ]}
        """

        let report = try JSONDecoder().decode(ToolReport.self, from: Data(json.utf8))

        XCTAssertFalse(report.recommendations[0].selectedByDefault)
    }
}
```

- [ ] **Step 2: Run the Swift tests to verify they fail**

Run: `swift test --package-path macos 2>&1 | tail -20`
Expected: FAIL — `ToolReport` is undefined.

- [ ] **Step 3: Add the Swift models**

In `macos/Sources/MacDevCleanApp/DeepScanModels.swift`, add:

```swift
struct ToolStatus: Decodable, Identifiable, Hashable {
    let tool: String
    let available: Bool
    let reason: String

    var id: String { tool }
}

struct ToolActionPayload: Decodable, Hashable {
    let tool: String
    let resource: String
    let argv: [String]
    let previewArgv: [String]
    let reported: String

    private enum CodingKeys: String, CodingKey {
        case tool, resource, argv, reported
        case previewArgv = "preview_argv"
    }
}

struct ToolRecommendation: Decodable, Identifiable, Hashable {
    let id: String
    let detectorId: String
    let category: String
    let label: String
    let size: String
    let reclaimableBytes: Int
    let reason: String
    let warning: String
    let selectedByDefault: Bool
    let toolAction: ToolActionPayload?

    private enum CodingKeys: String, CodingKey {
        case id, category, label, size, reason, warning
        case detectorId = "detector_id"
        case reclaimableBytes = "reclaimable_bytes"
        case selectedByDefault = "selected_by_default"
        case toolAction = "tool_action"
    }
}

struct ToolReport: Decodable {
    let statuses: [ToolStatus]
    let recommendations: [ToolRecommendation]
    let reclaimableTotalBytes: Int

    private enum CodingKeys: String, CodingKey {
        case statuses, recommendations
        case reclaimableTotalBytes = "reclaimable_total_bytes"
    }
}
```

- [ ] **Step 4: Add the backend call**

In `Backend.swift`, add a method mirroring the existing one-shot JSON call used
for `report --json`, invoking `tools --json` and decoding `ToolReport`. Follow
the file's existing process-launch and error-handling pattern exactly; do not
introduce a second launching style.

- [ ] **Step 5: Add the UI section**

In `ContentView.swift`, add `case tools = "Tool-managed"` to `SidebarPage` with
the SF Symbol `wrench.and.screwdriver`, and a view that shows:

- one row per `ToolStatus`, with unavailable tools stating their reason;
- one row per recommendation with size, label, the command that will run, and its warning;
- an explicit "Run" button per row, never a checkbox, because nothing here is selected by default;
- a confirmation alert naming the exact command before invoking `tools-apply`.

- [ ] **Step 6: Run all tests**

```bash
swift test --package-path macos 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add macos/Sources/MacDevCleanApp macos/Tests/MacDevCleanAppTests/ToolsTests.swift
git commit -m "feat: show tool-managed storage in the native app"
```

---

### Task 14: Full gates, live acceptance, and documentation

**Files:**
- Modify: `README.md`, `PRIVACY.md`, `CHANGELOG.md`

- [ ] **Step 1: Run every gate**

```bash
PYTHONPATH=src python3 -m unittest discover -s tests 2>&1 | tail -5
swift test --package-path macos 2>&1 | tail -5
./scripts/audit_public_repo.sh
```

Expected: Python OK, Swift OK, audit passed.

- [ ] **Step 2: Live read-only acceptance**

```bash
PYTHONPATH=src python3 -m mac_dev_clean tools
```

Verify by eye:
- Docker, Homebrew, Android, and simulator each appear, either with findings or with an explicit unavailable reason.
- No tool was started by the command: Docker Desktop's state is unchanged, and no Android Studio or Simulator window opened.
- Every row names the exact command it would run.
- The reported figures match what `docker system df` and `brew cleanup -n` say when run by hand.

- [ ] **Step 3: Live dry-run acceptance**

```bash
PYTHONPATH=src python3 -m mac_dev_clean tools-apply --id <a docker id from step 2> --dry-run
PYTHONPATH=src python3 -m mac_dev_clean journal --tail 5
```

Verify: the dry run reports `skipped`, names the command, and appears in the
journal with `"dry_run": true`. Nothing was deleted.

- [ ] **Step 4: Live destructive acceptance, one action only**

Choose the lowest-stakes finding — Docker build cache is the usual choice — and
run it for real:

```bash
PYTHONPATH=src python3 -m mac_dev_clean tools-apply --id <the same id>
docker system df
PYTHONPATH=src python3 -m mac_dev_clean journal --tail 3
```

Verify: the outcome is `invoked`, `docker system df` now reports less
reclaimable build cache, and the journal holds the executed vector. Record the
before/after figures in the commit message.

- [ ] **Step 5: Update the documentation**

In `README.md`, add a "Tool-managed storage" section documenting the three new
commands, the four Docker resource classes, why `docker image prune -a` is not
offered, why Homebrew's figure differs from its cache directory's size, and that
Android SDK/AVD items are inventory-only. Add a "Action journal" subsection
under the Safety Model documenting the journal path, its contents, and
`journal --clear`.

In `PRIVACY.md`, add the journal: local, append-only, contains local paths,
never uploaded, excluded from shared diagnostics, and clearable.

In `CHANGELOG.md`, add an entry under a new version heading describing
tool-managed storage and the audit journal.

- [ ] **Step 6: Final gate and commit**

```bash
PYTHONPATH=src python3 -m unittest discover -s tests 2>&1 | tail -3
./scripts/audit_public_repo.sh
git add README.md PRIVACY.md CHANGELOG.md
git commit -m "docs: document tool-managed storage and the action journal"
```

---

## Self-Review

**Spec coverage.** Phase 2's bullets map to tasks as follows: audit journal → Tasks 1, 10; `invoke_tool` end to end → Tasks 4, 9; shared runner with `unavailable` facts → Task 3; Docker → Task 5; Homebrew → Task 6; Android → Task 7; simulator → Task 8; UI section → Task 13. Acceptance criteria 1-2 are covered by Tasks 1, 9, 10; criterion 3 by Tasks 5-8 and the registry's `statuses`; criterion 4 by Task 5; criteria 5-6 by Tasks 3, 9; criterion 7 by the `ActionKind.INVOKE_TOOL` exclusion from `DESTRUCTIVE_ACTIONS` verified in Task 4; criterion 8 by the full-suite runs in Tasks 4, 10, 14; criterion 9 by the fake runners throughout; criterion 10 by Task 14 steps 2-4.

**Known rough edges an implementer should expect.** None deliberately left. The simulator analyzer, the Docker resource lookup in `_current_reclaimable`, and the shared `binary_runner` were all corrected against the real sources before this plan was committed. If a signature in this plan still disagrees with the code you find, trust the code and say so rather than adding an alias.

**Ordering.** Tasks 1-3 have no dependencies on each other beyond the `tools/__init__.py` placeholder that Task 2 creates and Task 3 replaces. Tasks 5-8 all depend on Tasks 3 and 4. Task 9 depends on 1, 4, 5, 6. Task 10 depends on 1. Task 11 depends on 8, 9. Task 12 depends on every Python module existing. Task 13 depends on 11. Task 14 is last.
