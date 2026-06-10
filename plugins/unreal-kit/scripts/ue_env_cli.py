"""
UE host-environment CLI -- describe and drive the Unreal Editor host.

Plugin-level script (not skill-scoped): the launch + detect primitives are
needed by multiple skills (ue-mcp-server for live driving, ue-python-api
when a remote-mode call wants the editor up, project-side facades like
dialog playtesting).

Subcommands:
    status         Snapshot of editor / MCP-bridge state. Distinguishes
                   interactive (window-up) editors from hung/zombie
                   processes that match the image name but have no window.
    launch-editor  Spawn the editor if no interactive instance is up. With
                   --wait-for-mcp, blocks until the MCP Automation Bridge
                   accepts a handshake.

Usage:
    python ue_env_cli.py status
    python ue_env_cli.py launch-editor
    python ue_env_cli.py launch-editor --map /Game/Maps/Foo
    python ue_env_cli.py launch-editor --wait-for-mcp --timeout 180
    python ue_env_cli.py launch-editor --force   # spawn even if one is running

(The file is named ue_env_cli.py, not ue_env.py, so this CLI module can never
shadow lib/ue_env.py -- the library it imports primitives from.)

The script reads the same per-project config as ue_runner.py
(<project_root>/.local-data/plugins-kit/unreal-kit/config.yaml) for engine_dir and
uproject, then derives the GUI editor binary from engine_dir.
"""

import argparse
import sys
from pathlib import Path

# scripts/ -> unreal-kit/lib
_SCRIPT_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _SCRIPT_DIR.parent
_LIB_DIR = _PLUGIN_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from path_repair import repair_path  # noqa: E402

repair_path()

# Re-exec under the bootstrap-provisioned plugin venv (no-op when already
# there) so plugin deps resolve regardless of which interpreter launched the
# script; then fail fast with an actionable message if the bootstrap plugin
# never provisioned this plugin at all (e.g. a stray system Python, no venv).
from bootstrap_guard import reexec_under_plugin_venv, require_bootstrap  # noqa: E402

reexec_under_plugin_venv("unreal-kit")
require_bootstrap("unreal-kit", feature="Unreal Python automation")

from ue_env import (  # noqa: E402
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORT,
    DEFAULT_READINESS_TIMEOUT_S,
    find_editor_processes,
    is_mcp_ready,
    launch_editor,
    wait_for_mcp_ready,
)
from ue_runner_config import load_config  # noqa: E402


def _info(msg: str) -> None:
    print(f"[ue_env] {msg}", file=sys.stderr)


def _err(msg: str) -> None:
    print(f"[ue_env] ERROR: {msg}", file=sys.stderr)


def _summarize_processes(procs: list[dict]) -> str:
    if not procs:
        return "none"
    parts: list[str] = []
    for p in procs:
        marker = "interactive" if p["has_window"] else "no-window"
        parts.append(f"pid={p['pid']} ({marker})")
    return ", ".join(parts)


def _interactive(procs: list[dict]) -> list[dict]:
    return [p for p in procs if p["has_window"]]


def _zombies(procs: list[dict]) -> list[dict]:
    return [p for p in procs if not p["has_window"]]


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    editor_exe = config.editor_exe
    uproject = config.uproject
    procs = find_editor_processes(editor_exe)
    interactive = _interactive(procs)
    zombies = _zombies(procs)
    ready = is_mcp_ready(args.host, args.port)
    print(f"editor_exe:  {editor_exe or '(not configured)'}")
    print(f"uproject:    {uproject or '(not configured)'}")
    print(f"processes:   {_summarize_processes(procs)}")
    print(f"interactive: {'yes' if interactive else 'no'}")
    if zombies:
        zpids = [str(z["pid"]) for z in zombies]
        print(
            f"zombies:     {', '.join(zpids)} "
            f"(UnrealEditor.exe processes with no main window -- likely "
            f"crashed or stuck in init; consider killing before relaunching)"
        )
    print(f"mcp_ready:   {ready}")
    print(f"mcp_target:  {args.host}:{args.port}")
    return 0


def cmd_launch_editor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    editor_exe = config.editor_exe
    uproject = config.uproject

    errors: list[str] = []
    if not editor_exe or not Path(editor_exe).is_file():
        errors.append(
            f"Editor binary not found or not configured: {editor_exe!r}. "
            f"Run scripts/ue_runner.py --setup to configure engine_dir."
        )
    if not uproject or not Path(uproject).is_file():
        errors.append(
            f".uproject not found or not configured: {uproject!r}. "
            f"Run scripts/ue_runner.py --setup."
        )
    if errors:
        for e in errors:
            _err(e)
        return 2

    if is_mcp_ready(args.host, args.port):
        _info("MCP bridge already reachable -- editor is up.")
        return 0

    procs = find_editor_processes(editor_exe)
    interactive = _interactive(procs)
    zombies = _zombies(procs)

    if zombies and not args.force:
        zpids = ", ".join(str(z["pid"]) for z in zombies)
        _err(
            f"UnrealEditor.exe process(es) running with no main window "
            f"(pid={zpids}) -- likely crashed or stuck in init. Kill them "
            f"before relaunching, or pass --force to spawn on top."
        )
        return 5

    if interactive and not args.force:
        ipid = interactive[0]["pid"]
        _info(
            f"Interactive editor already up (pid={ipid}) but MCP bridge is "
            f"not reachable. Check the McpAutomationBridge plugin is "
            f"enabled, or pass --force to spawn another editor anyway."
        )
        if args.wait_for_mcp:
            return _do_wait(args)
        return 0

    _info(
        f"Launching editor: {editor_exe} {uproject}"
        + (f" {args.map}" if args.map else "")
    )
    try:
        pid = launch_editor(editor_exe, uproject, map_arg=args.map)
    except (FileNotFoundError, OSError) as exc:
        _err(f"Failed to launch editor: {exc}")
        return 3
    _info(f"Spawned editor pid={pid}")

    if args.wait_for_mcp:
        return _do_wait(args)
    return 0


def _do_wait(args: argparse.Namespace) -> int:
    _info(
        f"Waiting up to {args.timeout:.0f}s for MCP bridge at "
        f"{args.host}:{args.port}..."
    )

    def _on_attempt(attempt: int, remaining: float) -> None:
        if attempt == 1 or attempt % 5 == 0:
            _info(f"  attempt {attempt} -- {remaining:.0f}s remaining")

    ok = wait_for_mcp_ready(
        host=args.host,
        port=args.port,
        total_timeout_s=args.timeout,
        on_attempt=_on_attempt,
    )
    if ok:
        _info("MCP bridge is ready.")
        return 0
    _err(f"MCP bridge did not respond within {args.timeout:.0f}s.")
    return 4


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ue_env_cli",
        description=(
            "Describe and drive the Unreal Editor host environment: detect "
            "running editors (interactive vs zombie), spawn one if needed, "
            "and wait for the MCP Automation Bridge to come up."
        ),
    )
    parser.add_argument("--config", help="Path to per-project config YAML.")
    parser.add_argument(
        "--host",
        default=DEFAULT_MCP_HOST,
        help=f"MCP bridge host (default: {DEFAULT_MCP_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_MCP_PORT,
        help=f"MCP bridge port (default: {DEFAULT_MCP_PORT}).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser(
        "status",
        help="Report editor + MCP bridge state without taking action.",
    )
    s.set_defaults(func=cmd_status)

    l = sub.add_parser(
        "launch-editor",
        help="Spawn the editor if no interactive instance is running.",
    )
    l.add_argument(
        "--map",
        help="Map asset path or .umap to open on startup.",
    )
    l.add_argument(
        "--wait-for-mcp",
        action="store_true",
        help="Block until the MCP bridge accepts a handshake.",
    )
    l.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_READINESS_TIMEOUT_S,
        help=(
            f"Readiness wait timeout in seconds "
            f"(default: {DEFAULT_READINESS_TIMEOUT_S})."
        ),
    )
    l.add_argument(
        "--force",
        action="store_true",
        help=(
            "Spawn a new editor even if one (interactive or zombie) is "
            "already running."
        ),
    )
    l.set_defaults(func=cmd_launch_editor)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
