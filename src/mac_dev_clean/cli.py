from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence, Set

from .age import parse_age
from .cleaner import clean_targets
from .deep_scan import deep_scan, default_deep_scan_roots
from .events import EventEmitter
from .executor import ApplyOutcome, apply_recommendations
from .fsevents import VolumeIdentity
from .index import (
    DEFAULT_INDEX_PATH,
    DEFAULT_TOOL_INDEX_PATH,
    open_index,
    read_project_ndk_usage_snapshot,
)
from .journal import open_journal, rotated_journal_path
from .output import (
    clean_report_json,
    render_clean_table,
    render_scan_table,
    render_tool_table,
    scan_report_json,
    tool_report_json,
)
from .scanner import scan
from .tool_executor import invoke_tool_recommendations
from .tools.registry import collect_tool_recommendations


FLAG_TO_CATEGORIES = {
    "xcode_derived_data": {"xcode-derived-data", "xcode-module-cache"},
    "xcode_documentation_cache": {"xcode-documentation-cache"},
    "xcode_device_support": {"xcode-device-support"},
    "xcode_device_logs": {"xcode-device-logs"},
    "xcode_test_devices": {"xcode-test-devices"},
    "xcode_caches": {
        "xcode-derived-data",
        "xcode-module-cache",
        "xcode-documentation-cache",
        "xcode-device-support",
        "xcode-device-logs",
        "xcode-test-devices",
        "simulator-caches",
        "simulator-dyld-cache",
        "project-derived-data",
    },
    "simulator_caches": {"simulator-caches"},
    "brew_cache": {"brew-cache"},
    "npm_cache": {"npm-cache"},
    "pnpm_cache": {"pnpm-cache"},
    "node_tool_caches": {"node-tool-cache"},
    "python_caches": {"python-cache"},
    "swiftpm_cache": {"swiftpm-cache"},
    "go_cache": {"go-cache"},
    "rust_cache": {"rust-cache"},
    "gradle_cache": {"gradle-cache"},
    "package_caches": {
        "npm-cache",
        "pnpm-cache",
        "node-tool-cache",
        "python-cache",
        "swiftpm-cache",
        "go-cache",
        "rust-cache",
        "gradle-cache",
    },
    "browser_caches": {"browser-cache"},
    "editor_caches": {"editor-cache", "updater-cache"},
    "wallpaper_cache": {"wallpaper-cache"},
    "project_derived_data": {"project-derived-data"},
    "simulator_dyld_cache": {"simulator-dyld-cache"},
    "node_modules": {"node-modules"},
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command in {"scan", "report"}:
            return run_scan(args)
        if args.command == "clean":
            return run_clean(args, parser)
        if args.command == "deep-scan":
            return run_deep_scan(args)
        if args.command == "apply":
            return run_apply(args, parser)
        if args.command == "reset-index":
            return run_reset_index(args)
        if args.command == "tools":
            return run_tools(args)
        if args.command == "tools-abandon":
            return run_tools_abandon(args)
        if args.command == "tools-apply":
            return run_tools_apply(args)
        if args.command == "journal":
            return run_journal(args)
        if args.command in {None, "interactive"}:
            return run_interactive(args, parser)
    except ValueError as exc:
        parser.error(str(exc))
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mac-dev-clean",
        description="Safely scan and clean common macOS developer caches.",
    )
    parser.set_defaults(
        command="interactive",
        search_root=[],
        include_node_modules=False,
        no_project_derived_data=False,
        older_than=None,
    )
    subparsers = parser.add_subparsers(dest="command")

    scan_parser = subparsers.add_parser(
        "scan",
        help="Scan common developer cache locations without deleting anything.",
    )
    add_scan_options(scan_parser)

    clean_parser = subparsers.add_parser(
        "clean",
        help="Clean selected cache categories. Requires explicit category flags.",
    )
    add_scan_root_options(clean_parser)
    clean_parser.add_argument(
        "--xcode-derived-data",
        action="store_true",
        help="Clean Xcode DerivedData and module cache.",
    )
    clean_parser.add_argument(
        "--xcode-documentation-cache",
        action="store_true",
        help="Clean Xcode's documentation cache.",
    )
    clean_parser.add_argument(
        "--xcode-device-support",
        action="store_true",
        help="Clean Xcode DeviceSupport symbols and files recreated by Xcode.",
    )
    clean_parser.add_argument(
        "--xcode-device-logs",
        action="store_true",
        help="Clean imported Xcode device logs and diagnostics.",
    )
    clean_parser.add_argument(
        "--xcode-test-devices",
        action="store_true",
        help="Delete shutdown XCTest simulator clones through simctl.",
    )
    clean_parser.add_argument(
        "--xcode-caches",
        action="store_true",
        help="Clean all supported Xcode, XCTest clone, and simulator cache categories.",
    )
    clean_parser.add_argument(
        "--simulator-caches",
        action="store_true",
        help="Clean CoreSimulator caches and logs.",
    )
    clean_parser.add_argument(
        "--brew-cache",
        action="store_true",
        help="Clean Homebrew's cache directory.",
    )
    clean_parser.add_argument(
        "--npm-cache",
        action="store_true",
        help="Clean npm cache and logs.",
    )
    clean_parser.add_argument(
        "--pnpm-cache",
        action="store_true",
        help="Clean pnpm store and cache.",
    )
    clean_parser.add_argument(
        "--node-tool-caches",
        action="store_true",
        help="Clean node-gyp, TypeScript, and Bun caches.",
    )
    clean_parser.add_argument(
        "--python-caches",
        action="store_true",
        help="Clean pip and Poetry caches.",
    )
    clean_parser.add_argument(
        "--swiftpm-cache",
        action="store_true",
        help="Clean Swift Package Manager caches.",
    )
    clean_parser.add_argument(
        "--go-cache",
        action="store_true",
        help="Clean Go build and module caches.",
    )
    clean_parser.add_argument(
        "--rust-cache",
        action="store_true",
        help="Clean Cargo registry and git dependency caches.",
    )
    clean_parser.add_argument(
        "--gradle-cache",
        action="store_true",
        help="Clean Gradle caches, daemons, and wrapper distributions.",
    )
    clean_parser.add_argument(
        "--package-caches",
        action="store_true",
        help="Clean npm, pnpm, node tooling, Python, SwiftPM, Go, Rust, and Gradle caches.",
    )
    clean_parser.add_argument(
        "--browser-caches",
        action="store_true",
        help="Clean supported browser caches, downloaded components, and Chrome's on-device AI model.",
    )
    clean_parser.add_argument(
        "--editor-caches",
        action="store_true",
        help="Clean Cursor, Windsurf, Codex desktop, and updater caches.",
    )
    clean_parser.add_argument(
        "--wallpaper-cache",
        action="store_true",
        help="Clean downloaded aerial wallpaper videos.",
    )
    clean_parser.add_argument(
        "--project-derived-data",
        action="store_true",
        help="Clean strictly validated project-local Xcode DerivedData directories.",
    )
    clean_parser.add_argument(
        "--simulator-dyld-cache",
        action="store_true",
        help="Remove generated simulator runtime dyld caches through simctl.",
    )
    clean_parser.add_argument(
        "--node-modules",
        action="store_true",
        help="Clean discovered node_modules directories.",
    )
    clean_parser.add_argument(
        "--older-than",
        help="Only include node_modules older than this age, such as 60d or 2w.",
    )
    clean_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without deleting files.",
    )
    clean_parser.add_argument("--json", action="store_true", help="Print JSON output.")

    interactive_parser = subparsers.add_parser(
        "interactive",
        help="Scan cleanable cache locations and prompt before deleting them.",
    )
    add_scan_root_options(interactive_parser)
    interactive_parser.add_argument(
        "--include-node-modules",
        action="store_true",
        help="Also include discovered node_modules directories. Requires --older-than.",
    )
    interactive_parser.add_argument(
        "--older-than",
        help="Only include node_modules older than this age, such as 60d or 2w.",
    )

    report_parser = subparsers.add_parser(
        "report",
        help="Produce a disk usage report. This never deletes files.",
    )
    add_scan_options(report_parser)

    deep_parser = subparsers.add_parser(
        "deep-scan",
        help="Recursively analyse projects and stream findings as NDJSON events.",
    )
    deep_parser.add_argument(
        "--root",
        action="append",
        type=Path,
        default=[],
        dest="deep_root",
        help="Directory to analyse. Defaults to the home directory. Repeatable.",
    )
    deep_parser.add_argument("--index", type=Path, default=None, help="Index database path.")
    deep_parser.add_argument(
        "--json",
        action="store_true",
        help="Print a single summary object instead of streaming NDJSON events.",
    )
    deep_parser.add_argument(
        "--no-fsevents",
        action="store_true",
        help="Always perform a full metadata walk instead of an incremental scan.",
    )

    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply deep-scan recommendations by ID after revalidating them.",
    )
    apply_parser.add_argument(
        "--id",
        action="append",
        default=[],
        dest="recommendation_id",
        help="Recommendation ID from a deep scan. Repeatable.",
    )
    apply_parser.add_argument("--index", type=Path, default=None, help="Index database path.")
    apply_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be removed."
    )
    apply_parser.add_argument("--json", action="store_true", help="Print JSON output.")

    reset_parser = subparsers.add_parser(
        "reset-index",
        help="Delete the local deep-scan index. The next deep scan rebuilds it.",
    )
    reset_parser.add_argument("--index", type=Path, default=None, help="Index database path.")

    tools_parser = subparsers.add_parser(
        "tools",
        help="Report storage managed by Docker, Homebrew, Android tools, and simctl.",
    )
    tools_parser.add_argument(
        "--index",
        type=Path,
        default=None,
        help="Dedicated tool recommendation index path.",
    )
    tools_parser.add_argument(
        "--project-index",
        type=Path,
        default=None,
        help="Read-only Deep Scan project index path.",
    )
    tools_parser.add_argument("--json", action="store_true", help="Print JSON output.")
    tools_parser.add_argument("--staging-token", help=argparse.SUPPRESS)

    tools_abandon_parser = subparsers.add_parser("tools-abandon", help=argparse.SUPPRESS)
    tools_abandon_parser.add_argument("--token", required=True)
    tools_abandon_parser.add_argument("--index", type=Path, default=None)
    tools_abandon_parser.add_argument("--json", action="store_true")

    tools_apply_parser = subparsers.add_parser(
        "tools-apply",
        help="Run one tool-managed action by recommendation id.",
    )
    tools_apply_parser.add_argument("--id", required=True, help="Recommendation id.")
    tools_apply_parser.add_argument(
        "--index",
        type=Path,
        default=None,
        help="Dedicated tool recommendation index path.",
    )
    tools_apply_parser.add_argument(
        "--journal", type=Path, default=None, help="Action journal path."
    )
    tools_apply_parser.add_argument(
        "--dry-run", action="store_true", help="Preview the command without running it."
    )
    tools_apply_parser.add_argument(
        "--json", action="store_true", help="Print JSON output."
    )

    journal_parser = subparsers.add_parser(
        "journal", help="Show or clear the local action journal."
    )
    journal_parser.add_argument(
        "--journal", type=Path, default=None, help="Action journal path."
    )
    journal_parser.add_argument(
        "--tail", type=_positive_int, help="Show only the last N records."
    )
    journal_parser.add_argument(
        "--clear", action="store_true", help="Delete active and rotated journals."
    )
    journal_parser.add_argument("--json", action="store_true", help="Print JSON output.")

    return parser


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer")
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def add_scan_options(parser: argparse.ArgumentParser) -> None:
    add_scan_root_options(parser)
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--older-than", help="Only include node_modules older than this age, such as 60d or 2w.")
    parser.add_argument("--no-node-modules", action="store_true", help="Skip recursive node_modules discovery.")
    parser.add_argument(
        "--no-project-derived-data",
        action="store_true",
        help="Skip project-local DerivedData discovery under top-level home folders.",
    )


def add_scan_root_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--search-root",
        action="append",
        type=Path,
        default=[],
        help="Directory to search for node_modules. Can be passed more than once.",
    )


def run_scan(args: argparse.Namespace) -> int:
    older_than = parse_age(args.older_than) if args.older_than else None
    announce_scan_start(include_node_modules=not args.no_node_modules)
    items = scan(
        search_roots=args.search_root or None,
        include_node_modules=not args.no_node_modules,
        include_project_derived_data=not args.no_project_derived_data,
        node_modules_older_than=older_than,
        now=datetime.now(timezone.utc),
    )
    print(scan_report_json(items) if args.json else render_scan_table(items))
    return 0


def run_clean(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    selected = selected_categories(args)
    if not selected:
        parser.error("clean requires at least one explicit category flag")
    if args.node_modules and not args.older_than:
        parser.error("--node-modules requires --older-than, for example --older-than 60d")

    older_than = parse_age(args.older_than) if args.older_than else None
    include_node_modules = "node-modules" in selected
    announce_scan_start(include_node_modules=include_node_modules)
    items = scan(
        search_roots=args.search_root or None,
        include_node_modules=include_node_modules,
        node_modules_older_than=older_than,
        now=datetime.now(timezone.utc),
        categories=selected,
    )
    targets = [item for item in items if item.category in selected and item.cleanable]
    journal = open_journal()
    results = clean_targets(targets, dry_run=args.dry_run, journal=journal)
    print(clean_report_json(results) if args.json else render_clean_table(results))
    return 1 if any(result.error for result in results) else 0


def run_interactive(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.include_node_modules and not args.older_than:
        parser.error(
            "interactive --include-node-modules requires --older-than, for example --older-than 60d"
        )

    older_than = parse_age(args.older_than) if args.older_than else None
    announce_scan_start(include_node_modules=args.include_node_modules)
    items = scan(
        search_roots=args.search_root or None,
        include_node_modules=args.include_node_modules,
        node_modules_older_than=older_than,
        now=datetime.now(timezone.utc),
    )
    if not items:
        print("No supported developer cache locations found.")
        return 0

    targets = [item for item in items if item.cleanable]
    print(render_scan_table(items))
    if not targets:
        print("No cleanable developer cache locations found.")
        return 0

    print()
    if not prompt_yes_no("Delete the cleanable items listed above? [y/N] "):
        print("Canceled. Nothing deleted.")
        return 0

    journal = open_journal()
    results = clean_targets(targets, journal=journal)
    print(render_clean_table(results))
    return 1 if any(result.error for result in results) else 0


def run_deep_scan(args: argparse.Namespace) -> int:
    roots = list(args.deep_root) or default_deep_scan_roots(Path.home())
    if not roots:
        print("No readable scan roots were found.", file=sys.stderr)
        return 1

    index = open_index(args.index)
    try:
        # The generation is assigned by the index once the scan starts, so the
        # envelope cannot carry it yet. Emit -1 rather than a plausible-looking
        # 0, which reads as a real generation number next to the recommendation
        # payloads that carry the true one.
        emitter = None if args.json else EventEmitter(sys.stdout, generation=-1)
        result = deep_scan(
            roots,
            index,
            emitter=emitter,
            use_fsevents=not args.no_fsevents,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "cancelled": result.cancelled,
                        "incremental": result.incremental,
                        "reclaimable_bytes": result.reclaimable_bytes,
                        "count": len(result.recommendations),
                        "recommendations": [
                            item.to_dict() for item in result.recommendations
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
    finally:
        index.close()
    return 0


def run_apply(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if not args.recommendation_id:
        parser.error("apply requires at least one --id")

    index = open_index(args.index)
    journal = open_journal()
    try:
        results = apply_recommendations(
            args.recommendation_id,
            index,
            dry_run=args.dry_run,
            journal=journal,
        )
    finally:
        index.close()

    if args.json:
        print(
            json.dumps(
                {"results": [item.to_dict() for item in results]},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for item in results:
            details = [detail for detail in (item.error, item.journal_warning) if detail]
            suffix = " — {}".format("; ".join(details)) if details else ""
            print("{:<20} {}{}".format(item.outcome.value, item.path, suffix))

    failed = any(item.outcome is ApplyOutcome.FAILED for item in results)
    return 1 if failed else 0


def run_tools(args: argparse.Namespace) -> int:
    index_path = args.index if args.index is not None else DEFAULT_TOOL_INDEX_PATH.expanduser()
    project_index = getattr(args, "project_index", None)
    project_index_path = (
        project_index if project_index is not None else DEFAULT_INDEX_PATH.expanduser()
    )
    index = None
    report = None
    started = False
    failed_safely = False
    staging_token = getattr(args, "staging_token", None)
    try:
        index = open_index(index_path)
        if staging_token:
            generation = index.stage_generation(
                VolumeIdentity(device=0, uuid=None), event_id=0, token=staging_token
            )
        else:
            generation = index.begin_generation(VolumeIdentity(device=0, uuid=None), event_id=0)
        started = True
        try:
            ndk_usage_snapshot = read_project_ndk_usage_snapshot(project_index_path)
        except (OSError, sqlite3.DatabaseError, ValueError):
            ndk_usage_snapshot = None
        report = collect_tool_recommendations(
            generation=generation,
            home=Path.home(),
            ndk_usage_snapshot=ndk_usage_snapshot,
        )
        for item in report.recommendations:
            index.record_recommendation(item)
        if staging_token:
            index.commit_batch()
            index.abandon_generation()
        else:
            index.complete_generation()
        started = False
    except Exception:
        failed_safely = True
        if index is not None and started:
            try:
                index.abandon_generation()
            except Exception:
                pass
    finally:
        if index is not None:
            try:
                index.close()
            except Exception:
                failed_safely = True

    if failed_safely or report is None:
        message = "Could not collect tool-managed storage."
        if args.json:
            print(json.dumps({"error": message}, indent=2, sort_keys=True))
        else:
            print(message)
        return 1

    if args.json:
        payload = tool_report_json(report)
        if staging_token:
            payload["staging_token"] = staging_token
            payload["staging_index_path"] = str(Path(index_path).expanduser().resolve())
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_tool_table(report))
    return 0


def run_tools_abandon(args: argparse.Namespace) -> int:
    index_path = args.index if args.index is not None else DEFAULT_TOOL_INDEX_PATH.expanduser()
    index = None
    try:
        index = open_index(index_path)
        index.abandon_staged_generation(args.token)
    except Exception:
        if args.json:
            print(json.dumps({"ok": False}, sort_keys=True))
        return 1
    finally:
        if index is not None:
            try:
                index.close()
            except Exception:
                return 1
    if args.json:
        print(json.dumps({"ok": True}, sort_keys=True))
    return 0


def run_tools_apply(args: argparse.Namespace) -> int:
    index_path = args.index if args.index is not None else DEFAULT_TOOL_INDEX_PATH.expanduser()
    index = None
    results = None
    failed_safely = False
    close_warning = ""
    try:
        index = open_index(index_path)
        journal = open_journal(args.journal) if args.journal is not None else open_journal()
        results = invoke_tool_recommendations(
            [args.id], index, journal, dry_run=args.dry_run
        )
    except Exception:
        failed_safely = True
    finally:
        if index is not None:
            try:
                index.close()
            except Exception:
                close_warning = "Could not close the tool recommendation index."

    if failed_safely or results is None:
        message = "Could not apply the tool-managed recommendation."
        if args.json:
            print(json.dumps({"error": message, "results": []}, indent=2, sort_keys=True))
        else:
            print(message)
        return 1

    if args.json:
        payload = {  # type: Dict[str, object]
            "results": [result.to_dict() for result in results]
        }
        if close_warning:
            payload["warning"] = close_warning
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for result in results:
            print("{}: {}".format(result.outcome.value, result.label or args.id))
            if result.reported:
                print("  reported: {}".format(result.reported))
            if result.error:
                print("  error: {}".format(result.error))
            if result.journal_warning:
                print("  warning: {}".format(result.journal_warning))
        if close_warning:
            print("warning: {}".format(close_warning))

    return 1 if any(result.outcome is ApplyOutcome.FAILED for result in results) else 0


def run_journal(args: argparse.Namespace) -> int:
    try:
        journal = open_journal(args.journal) if args.journal is not None else open_journal()
    except Exception:
        message = "Could not open the action journal."
        if args.json:
            print(json.dumps({"error": message, "records": []}, indent=2, sort_keys=True))
        else:
            print(message)
        return 1

    path = journal.path
    if args.clear:
        failed = False
        for candidate in (path, rotated_journal_path(path)):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                failed = True
        if failed:
            print("Could not clear one or more journal files.")
            return 1
        return 0

    records = []
    try:
        with open(str(path), "rb") as handle:
            for raw_line in handle:
                try:
                    line = raw_line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except FileNotFoundError:
        records = []
    except OSError:
        message = "Could not read the action journal."
        if args.json:
            print(
                json.dumps(
                    {"error": message, "records": []}, indent=2, sort_keys=True
                )
            )
        else:
            print(message)
        return 1

    if args.tail is not None:
        records = records[-args.tail :]

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


def run_reset_index(args: argparse.Namespace) -> int:
    index = open_index(args.index)
    try:
        index.reset()
    finally:
        index.close()
    print("Deep scan index cleared.")
    return 0


def announce_scan_start(include_node_modules: bool) -> None:
    detail = " including project node_modules discovery" if include_node_modules else ""
    print(
        f"Scanning developer cache locations{detail}. This can take a moment...",
        file=sys.stderr,
        flush=True,
    )


def prompt_yes_no(message: str) -> bool:
    while True:
        try:
            response = input(message)
        except EOFError:
            return False
        normalized = response.strip().lower()
        if normalized in {"y", "yes"}:
            return True
        if normalized in {"", "n", "no"}:
            return False
        print("Please answer y or n.")


def selected_categories(args: argparse.Namespace) -> Set[str]:
    selected: Set[str] = set()
    for arg_name, categories in FLAG_TO_CATEGORIES.items():
        if getattr(args, arg_name):
            selected.update(categories)
    return selected


if __name__ == "__main__":
    raise SystemExit(main())
