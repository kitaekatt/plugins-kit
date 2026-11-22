"""Claude-first plugin-root CLI for the opt-in lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from worktree_config import read_object
from worktree_lifecycle import Lifecycle, configure_repository
from worktree_pool import remote_url
from worktree_safety import process_identity
import worktrees as wt


def lifecycle_off(repo: Path) -> bool:
    """Check hook applicability before validating enabled lifecycle settings."""
    try:
        repo = wt.project_root(repo)
        remote_url(repo)
    except (ValueError, subprocess.CalledProcessError):
        return True
    raw = read_object(repo / ".claude/bootstrap.json").get("git_kit", {}).get("worktrees")
    return raw is None or (isinstance(raw, dict) and raw.get("enabled", False) is False)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure")
    mode = configure.add_mutually_exclusive_group(required=True)
    mode.add_argument("--propose", action="store_true")
    mode.add_argument("--apply", type=Path, metavar="APPROVED_JSON")
    take = commands.add_parser("take")
    take.add_argument("--agent", required=True)
    take.add_argument("--session", required=True)
    take.add_argument("--task")
    take.add_argument("--holder-pid", type=int, default=os.getppid(), help="Long-lived holder process (default: invoking parent)")
    adopt = commands.add_parser("adopt")
    adopt.add_argument("--id", required=True)
    adopt.add_argument("--agent", required=True)
    adopt.add_argument("--session", required=True)
    adopt.add_argument("--holder-pid", type=int, default=os.getppid())
    parked = commands.add_parser("parked")
    scope = parked.add_mutually_exclusive_group()
    scope.add_argument("--session")
    scope.add_argument("--task")
    close = commands.add_parser("check-task-close")
    close.add_argument("--task", required=True)
    close.add_argument("--json", action="store_true")
    discard = commands.add_parser("discard")
    discard.add_argument("--id", required=True)
    discard_mode = discard.add_mutually_exclusive_group(required=True)
    discard_mode.add_argument("--propose", action="store_true")
    discard_mode.add_argument("--apply", type=Path, metavar="APPROVED_JSON")
    discard.add_argument("--force", action="store_true", help="Grants no discard authority")
    release = commands.add_parser("release")
    release.add_argument("--claim", required=True)
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true")
    commands.add_parser("config", help="Read effective lifecycle settings")
    commands.add_parser("list", help="Alias for status")
    for name in ("new", "remove"):
        legacy = commands.add_parser(name, help="Refuse raw lifecycle mutation")
        legacy.add_argument("legacy_arguments", nargs=argparse.REMAINDER)
    pool = commands.add_parser("pool", help="Compatibility aliases for maintenance and status")
    pool.add_argument("pool_command", choices=["refill", "status"])
    pool.add_argument("--background", action="store_true")
    pool.add_argument("--log", action="store_true")
    pool.add_argument("--quiet-if-off", action="store_true")
    commands.add_parser("maintenance", help=argparse.SUPPRESS)
    migration_help = ("Inventory or apply legacy worktree cutover; exit Claude sessions whose cwd or open files "
                      "are inside candidate worktrees. Sessions elsewhere are reported as ignored_claude_processes; "
                      "uncertain process evidence blocks migration.")
    migrate = commands.add_parser("migrate", help=migration_help, description=migration_help)
    migration_mode = migrate.add_mutually_exclusive_group(required=True)
    migration_mode.add_argument("--inventory", action="store_true")
    migration_mode.add_argument("--apply", action="store_true")
    migrate.add_argument("--clone", action="append", type=Path, default=[], help="Include another source clone outside discovered locations")
    for child in commands.choices.values():
        child.add_argument("--repo", type=Path, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    obj = None
    try:
        if args.command == "configure":
            result = configure_repository(args.repo, read_object(args.apply) if args.apply else None)
            if args.apply:
                result["maintenance"] = Lifecycle(args.repo).request_maintenance()
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "pool" and args.quiet_if_off and lifecycle_off(args.repo):
            return 0
        if args.command == "check-task-close" and lifecycle_off(args.repo):
            print(json.dumps({"allowed": True, "parked": []}))
            return 0
        obj = Lifecycle(args.repo)
        if args.command in {"new", "remove"}:
            raise ValueError("Raw lifecycle mutation is unavailable; configure --propose, then take --agent --session or release --claim; legacy worktrees require migration")
        if args.command == "take":
            result = obj.take(args.agent, args.session, args.task, process=process_identity(args.holder_pid))
        elif args.command == "adopt":
            result = obj.adopt(args.id, args.agent, args.session, process=process_identity(args.holder_pid))
        elif args.command == "parked":
            result = obj.parked(session=args.session, task=args.task)
        elif args.command == "check-task-close":
            result = obj.check_task_close(args.task)
            result = {"allowed": result["allowed"], "parked": result["parked"]}
        elif args.command == "discard":
            result = obj.discard(args.id, approved=read_object(args.apply) if args.apply else None, force=args.force)
        elif args.command == "release":
            result = obj.release(args.claim)
        elif args.command == "migrate":
            result = obj.migrate(apply=args.apply, clones=args.clone)
        elif args.command in {"status", "list", "config"}:
            result = obj.status()
        elif args.command == "pool":
            if args.pool_command == "status":
                result = obj.status()
            elif args.background:
                result = obj.request_maintenance()
            else:
                result = obj.maintain()
        else:
            result = obj.maintain()
        print(json.dumps(result, indent=2))
        return 2 if result.get("outcome") == "failed" else 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        if obj is not None and args.command in {"new", "remove"}:
            detail = f"{detail}; maintenance: {json.dumps(obj.request_maintenance())}"
        if args.command == "check-task-close":
            print(json.dumps({"allowed": False, "parked": [], "error": detail}))
        print(f"git-kit worktrees: {detail}", file=sys.stderr)
        return 4 if "READY_POOL_EMPTY" in str(detail) else 2
