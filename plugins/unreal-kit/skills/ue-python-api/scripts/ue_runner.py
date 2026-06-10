"""
UE Python Script Runner — Execute UE Python scripts from the terminal.

Auto-detects whether UE Editor is running:
  - Editor running + remote execution enabled → fast UDP execution via upyrc
  - Editor not running → headless commandlet (slow, ~30-120s)

Usage:
    python ue_runner.py script.py
    python ue_runner.py script.py --mode commandlet
    python ue_runner.py script.py --mode remote
    python ue_runner.py script.py --copy-output ./results/
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Add lib/ to path (at plugin root: scripts/ -> skills/ue-python-api/ -> unreal-kit/lib/)
_SKILL_DIR = Path(__file__).resolve().parent.parent
_PLUGIN_DIR = _SKILL_DIR.parent.parent
_LIB_DIR = _PLUGIN_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

# Restore registry-canonical PATH before subprocess fan-out — see
# unreal-kit/lib/path_repair.py for the cmd.exe overflow failure mode.
from path_repair import repair_path  # noqa: E402
repair_path()

# Re-exec under the bootstrap-provisioned plugin venv (no-op when already
# there) so upyrc/pyyaml resolve regardless of which interpreter launched the
# script; then fail fast with an actionable message if the bootstrap plugin
# never provisioned this plugin at all (e.g. a stray system Python and no venv).
from bootstrap_guard import reexec_under_plugin_venv, require_bootstrap  # noqa: E402
reexec_under_plugin_venv("unreal-kit")
require_bootstrap("unreal-kit", feature="Unreal Python automation")

from ue_discovery import find_engine_dir as _find_engine_dir, find_uproject_from_cwd, find_uproject_from_path
from ue_runner_config import RunnerConfig, load_config


@dataclass
class RunResult:
    success: bool
    mode: str  # "remote" or "commandlet"
    stdout: str = ""
    stderr: str = ""
    output_file: str | None = None
    elapsed: float = 0.0
    error: str = ""


def run_ue_script(
    script_path: str,
    force_mode: str | None = None,
    config: RunnerConfig | None = None,
    copy_output_to: str | None = None,
    project: str | None = None,
    fallback_on_error: bool = False,
) -> RunResult:
    """
    Execute a UE Python script, auto-selecting the best execution path.

    Args:
        script_path: Path to the .py script to run.
        force_mode: "remote", "commandlet", or None (auto-detect).
        config: RunnerConfig instance, or None to load from default config.
        copy_output_to: If set, copy any output YAML to this directory.
        project: Explicit path to a .uproject file. Overrides config and
                 auto-discovery.
        fallback_on_error: If True, a remote SCRIPT error (editor reachable,
                 script ran and failed) retries via commandlet. Off by
                 default because the failed remote run may already have
                 executed side effects; re-running a non-idempotent script
                 doubles them. Connection-level failures (editor not
                 reachable) always fall back regardless of this flag.

    Returns:
        RunResult with execution details.
    """
    if config is None:
        config = load_config()

    script_path = os.path.abspath(script_path)
    if not os.path.isfile(script_path):
        return RunResult(success=False, mode="none", error=f"Script not found: {script_path}")

    # Resolve which project to target: explicit flag > CWD > script path > config
    config = _resolve_project(config, script_path, project)

    # Validate config (commandlet needs valid paths; remote can work without them)
    errors = config.validate()
    if force_mode == "commandlet" and errors:
        return RunResult(success=False, mode="commandlet", error="\n".join(errors))

    # Try remote execution first (unless forced to commandlet)
    if force_mode != "commandlet":
        result = _try_remote(script_path, config)
        if result is not None:
            # Remote connected and the script actually RAN. A failure here is
            # a script-level error, not a connection problem: falling back to
            # commandlet would RE-EXECUTE the script, repeating any side
            # effects the failed remote run already performed. Auto-fallback
            # is therefore opt-in via --fallback-on-error (useful for
            # idempotent scripts hitting editor-memory quirks, e.g. TMap
            # properties returning None because assets aren't loaded in the
            # Editor -- commandlet loads them from disk).
            if (
                not result.success
                and fallback_on_error
                and force_mode != "remote"
                and not errors
            ):
                _warn(
                    f"Remote script error, retrying via commandlet (--fallback-on-error)...\n"
                    f"  Remote error: {result.error}"
                )
            else:
                if not result.success and force_mode != "remote":
                    result.error = (
                        f"{result.error}\n"
                        "  Not retrying via commandlet: the script already ran "
                        "remotely, so a retry would re-execute its side effects. "
                        "Pass --fallback-on-error or --mode commandlet to run it "
                        "headless."
                    )
                if copy_output_to and result.output_file:
                    result.output_file = _copy_output(result.output_file, copy_output_to)
                return result
        if force_mode == "remote":
            return RunResult(
                success=False,
                mode="remote",
                error="Remote execution failed. Is UE Editor running with Remote Execution enabled?\n"
                      "  Run: python ue_runner.py --setup",
            )

    # Fall back to commandlet
    if errors:
        return RunResult(success=False, mode="commandlet", error="\n".join(errors))

    result = _run_commandlet(script_path, config)
    if copy_output_to and result.output_file:
        result.output_file = _copy_output(result.output_file, copy_output_to)
    return result


def _try_remote(script_path: str, config: RunnerConfig) -> RunResult | None:
    """
    Attempt remote execution via upyrc. Returns RunResult on success, None if
    the editor isn't reachable (so caller can fall back to commandlet).
    """
    try:
        from upyrc import upyre
    except ImportError:
        _warn(
            "upyrc not installed — skipping remote execution.\n"
            "  upyrc lives in the bootstrap-provisioned plugin venv; start a new\n"
            "  session so the bootstrap plugin can (re)provision unreal-kit.\n"
            "  Falling back to commandlet..."
        )
        return None

    remote_cfg = upyre.RemoteExecutionConfig(
        multicast_group=(config.remote.multicast_group, config.remote.multicast_port),
        multicast_bind_address=config.remote.multicast_bind_address,
    )

    # Snapshot output dir before execution
    output_dir = _get_output_dir(config)
    pre_snapshot = _snapshot_output_dir(output_dir)

    start = time.time()
    try:
        with upyre.PythonRemoteConnection(remote_cfg) as conn:
            # EXECUTE_FILE tells UE to load and run a .py file by path.
            # EXECUTE_STATEMENT would exec() inline code instead.
            cmd_result = conn.execute_python_command(
                script_path,
                exec_type=upyre.ExecTypes.EXECUTE_FILE,
                raise_exc=False,
            )
    except Exception as e:
        err_str = str(e)
        # Connection refused / timeout / failed = editor not running
        if any(keyword in err_str.lower() for keyword in ("timed out", "timeout", "refused", "unreachable", "connection failed")):
            _warn(f"Editor not responding ({err_str}). Falling back to commandlet...")
            return None
        # Other errors may be script errors — still a valid execution attempt
        elapsed = time.time() - start
        return RunResult(
            success=False, mode="remote", stderr=err_str, elapsed=elapsed,
            error=f"Remote execution error: {err_str}",
        )

    elapsed = time.time() - start
    raw_result = cmd_result.result if hasattr(cmd_result, 'result') else str(cmd_result)
    stdout = raw_result if raw_result and raw_result != "None" else ""
    success = cmd_result.success if hasattr(cmd_result, 'success') else True

    # Check for new output files
    output_file = _find_new_output(output_dir, pre_snapshot)

    error = ""
    if not success:
        error = f"Script error (see editor Output Log): {stdout[:200]}" if stdout else "Script execution failed"

    return RunResult(
        success=success, mode="remote", stdout=stdout,
        output_file=output_file, elapsed=elapsed, error=error,
    )


def _run_commandlet(script_path: str, config: RunnerConfig) -> RunResult:
    """Run script via UnrealEditor-Cmd.exe -run=pythonscript."""
    exe = config.editor_cmd_exe
    uproject = config.uproject

    command = [
        exe,
        uproject,
        "-run=pythonscript",
        f"-script={script_path}",
        "-stdout",
        "-Unattended",
        "-NoLoadStartupPackages",
        "-FullStdOutLogOutput",
    ]

    _info(f"Running commandlet...")
    _info(f"  {' '.join(command)}")

    # Snapshot output dir before execution
    output_dir = _get_output_dir(config)
    pre_snapshot = _snapshot_output_dir(output_dir)

    start = time.time()
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            # No timeout — user can Ctrl-C if needed
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except FileNotFoundError:
        return RunResult(
            success=False, mode="commandlet",
            error=f"Editor executable not found: {exe}",
        )

    elapsed = time.time() - start
    output_file = _find_new_output(output_dir, pre_snapshot)

    # UE commandlets frequently exit non-zero due to asset loading warnings
    # (e.g. Niagara modules) that are unrelated to the Python script.
    # Consider it a success if the output file was produced, or if stdout
    # contains no Python-level error indicators.
    has_script_error = _detect_script_error(proc.stdout, script_path)

    if has_script_error:
        success = False
    elif output_file is not None:
        # Script produced output — success regardless of UE exit code
        success = True
    else:
        success = proc.returncode == 0

    error = ""
    if not success:
        if has_script_error:
            error = "Python script error detected in output (see stdout)"
        else:
            error = proc.stderr

    return RunResult(
        success=success,
        mode="commandlet",
        stdout=proc.stdout,
        stderr=proc.stderr,
        output_file=output_file,
        elapsed=elapsed,
        error=error,
    )


def _get_output_dir(config: RunnerConfig) -> Path | None:
    """Get the PythonOutput directory under the project's Saved folder."""
    if not config.uproject:
        return None
    project_dir = Path(config.uproject).parent
    return project_dir / "Saved" / "PythonOutput"


def _detect_script_error(stdout: str, script_path: str) -> bool:
    """Check UE commandlet stdout for Python errors from our script.

    UE emits Python errors -- including full multi-line tracebacks -- as
    contiguous "LogPython: Error:" lines. UE also auto-runs startup scripts
    (Content/Python/) which commonly fail in commandlet mode (no UI, missing
    debug modules), so an error block only counts when at least one of its
    lines references the script we actually ran (a traceback frame line
    carries the script's file name). Matching whole blocks instead of a
    hand-picked exception-type whitelist means KeyError, RuntimeError, and
    every other exception type are detected equally.
    """
    # Normalize path for matching (UE logs use mixed separators)
    script_name = os.path.basename(script_path)

    block: list[str] = []
    for line in stdout.split("\n"):
        if "LogPython: Error:" in line:
            block.append(line)
            continue
        # Block ended -- flag it if any line referenced our script.
        if any(script_name in b for b in block):
            return True
        block = []

    return any(script_name in b for b in block)


def _snapshot_output_dir(output_dir: Path | None) -> dict[str, float]:
    """Return {filename: mtime} for all YAML files in the output directory."""
    if output_dir is None or not output_dir.is_dir():
        return {}
    snapshot = {}
    for f in output_dir.iterdir():
        if f.suffix in (".yaml", ".yml"):
            snapshot[str(f)] = f.stat().st_mtime
    return snapshot


def _find_new_output(output_dir: Path | None, pre_snapshot: dict[str, float]) -> str | None:
    """Poll for new/modified YAML files after script execution."""
    if output_dir is None or not output_dir.is_dir():
        return None

    # Poll a few times to handle slight delay in file writes
    for _ in range(5):
        for f in output_dir.iterdir():
            if f.suffix not in (".yaml", ".yml"):
                continue
            fpath = str(f)
            mtime = f.stat().st_mtime
            if fpath not in pre_snapshot or mtime > pre_snapshot[fpath]:
                return fpath
        time.sleep(1)

    return None


def _copy_output(output_file: str, dest_dir: str) -> str:
    """Copy output file to destination directory, return new path."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(output_file))
    shutil.copy2(output_file, dest)
    _info(f"Copied output to {dest}")
    return dest


def _info(msg: str):
    print(f"[ue_runner] {msg}", file=sys.stderr)


def _warn(msg: str):
    print(f"[ue_runner] WARNING: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Project resolution — pick the right project for this invocation
# ---------------------------------------------------------------------------

def _resolve_project(
    config: RunnerConfig,
    script_path: str,
    explicit_project: str | None = None,
) -> RunnerConfig:
    """Override config's uproject/engine_dir based on context.

    Resolution order:
      1. Explicit --project flag
      2. Walk up from CWD looking for a .uproject
      3. Walk up from the script's location looking for a .uproject
      4. Keep config values (backward compat — from setup's config.yaml)

    When a project is discovered via (1-3), engine_dir is re-resolved from
    the discovered .uproject so the two always stay in sync.
    """
    from dataclasses import replace

    discovered = None
    source = None

    if explicit_project:
        p = Path(explicit_project).resolve()
        if p.is_file() and p.suffix == ".uproject":
            discovered = p
            source = "--project flag"
        else:
            _warn(f"--project path is not a .uproject file: {p}")

    if not discovered:
        cwd_project = find_uproject_from_cwd()
        if cwd_project:
            discovered = cwd_project
            source = f"CWD ({Path.cwd()})"

    if not discovered:
        script_project = find_uproject_from_path(Path(script_path))
        if script_project:
            discovered = script_project
            source = f"script path ({script_path})"

    if not discovered:
        return config  # fall back to config file values

    new_uproject = str(discovered)
    new_engine_dir = ""
    engine = _find_engine_dir(discovered)
    if engine:
        new_engine_dir = str(engine)

    # Only log if the project actually changed
    if new_uproject != config.uproject:
        _info(f"Project resolved from {source}: {discovered.name}")
        if config.uproject:
            _info(f"  (config had: {config.uproject})")

    return replace(config, uproject=new_uproject, engine_dir=new_engine_dir or config.engine_dir)


# ---------------------------------------------------------------------------
# Setup — interactive .uproject disambiguation + per-project config write
# ---------------------------------------------------------------------------

from ue_discovery import find_engine_dir, find_uproject_files  # noqa: E402


def run_setup(config: RunnerConfig) -> bool:
    """Interactive setup: pick the .uproject and write the per-project config.

    Everything else the runner needs is provisioned by the bootstrap plugin's
    manifest (bootstrap.json), not duplicated here: the UserEngine.ini settings
    (bRemoteExecution, bIsDeveloperMode) via its ini_settings section, and the
    host-side Python deps (upyrc, pyyaml) via its venv section. Setup's only
    job is the part that genuinely needs a human: choosing which .uproject
    this project uses.
    """
    from ue_runner_config import (
        PROJECT_CONFIG_NAME,
        write_project_config as _write_project_config,
    )

    # Always prompt for .uproject path (show current value as default)
    current_uproject = Path(config.uproject) if config.uproject else None
    uproject = _ask_uproject_path(current=current_uproject)
    if not uproject:
        return False

    engine_dir = find_engine_dir(uproject)
    if not engine_dir:
        print(f"  ERROR: Could not find Engine/ directory relative to {uproject}")
        print(f"  (Walked up looking for Engine/Binaries/Win64/UnrealEditor-Cmd.exe)")
        return False

    # Write per-project config
    project_root = uproject.parent
    config_path = project_root / PROJECT_CONFIG_NAME
    print(f"\n  Project:")
    print(f"    uproject:   {uproject}")
    print(f"    engine_dir: {engine_dir}")
    print(f"\n  Write project config?")
    print(f"    File: {config_path}")
    if not _confirm("  Save?", default_yes=True):
        print("  Skipped.")
        return False

    data = {
        "engine_dir": str(engine_dir),
        "uproject": str(uproject),
    }
    written = _write_project_config(project_root, data)
    print(f"  WROTE {written}")

    # Reload config with the new file
    from ue_runner_config import load_config as _reload
    config = _reload()
    print(f"\n  OK    uproject: {config.uproject}")
    print(f"  OK    engine:   {config.engine_dir}")
    print(
        "\n  Editor ini settings (bRemoteExecution, bIsDeveloperMode) and host\n"
        "  Python deps (upyrc, pyyaml) are provisioned by the bootstrap plugin\n"
        "  at session start -- start a new session to apply them."
    )

    return True


def _confirm(prompt: str, default_yes: bool = False) -> bool:
    """Ask user yes/no. Returns default on empty input."""
    hint = "[Y/n]" if default_yes else "[y/N]"
    try:
        answer = input(f"{prompt} {hint} ").strip().lower()
        if not answer:
            return default_yes
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _ask_uproject_path(current: Path | None = None) -> Path | None:
    """Ask user for a .uproject file or directory containing one.

    If current is set, it's shown as the default — pressing Enter keeps it.
    """
    if current:
        print(f"\n  Enter path to your .uproject file (or a directory to search)")
        print(f"  Current: {current}")
        try:
            raw = input(f"  [{current.name}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw:
            return current
    else:
        print("\n  Enter path to your .uproject file (or a directory to search):")
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw:
            return None

    # Expand ~ and resolve
    p = Path(os.path.expanduser(raw)).resolve()

    # Direct .uproject file
    if p.is_file() and p.suffix == ".uproject":
        return p

    # Directory — search it for .uproject files
    if p.is_dir():
        found = find_uproject_files(p, max_depth=4)
        if not found:
            print(f"  No .uproject files found under {p}")
            return None
        if len(found) == 1:
            print(f"  Found: {found[0]}")
            return found[0]
        # Multiple — let user pick
        print(f"  Found {len(found)} .uproject files:\n")
        for i, f in enumerate(found, 1):
            print(f"    [{i}] {f}")
        print()
        try:
            choice = input("  Enter number (or 'q' to quit): ").strip()
            if choice.lower() == 'q' or not choice:
                return None
            idx = int(choice) - 1
            if 0 <= idx < len(found):
                return found[idx]
        except (ValueError, EOFError, KeyboardInterrupt):
            print()
        return None

    print(f"  Path not found: {p}")
    return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run UE Python scripts from the terminal.",
        epilog="Examples:\n"
               "  python ue_runner.py --setup                # check/fix project settings\n"
               "  python ue_runner.py script.py              # auto-detect mode\n"
               "  python ue_runner.py script.py --mode remote # force remote only\n"
               "  python ue_runner.py script.py --mode commandlet\n"
               "  python ue_runner.py script.py --copy-output ./results/\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("script", nargs="?", help="Path to the .py script to execute")
    parser.add_argument(
        "--setup", action="store_true",
        help="Check and fix UE project settings for remote execution",
    )
    parser.add_argument(
        "--mode", choices=["remote", "commandlet"],
        help="Force execution mode (default: auto-detect)",
    )
    parser.add_argument(
        "--fallback-on-error", action="store_true",
        help="Retry via commandlet when a remote run reaches the editor but the "
             "script errors. Off by default: the failed remote run may already "
             "have executed side effects, and a commandlet retry re-executes "
             "them. (Connection failures always fall back.)",
    )
    parser.add_argument(
        "--project", metavar="UPROJECT",
        help="Path to the .uproject file (overrides auto-detection and config)",
    )
    parser.add_argument(
        "--config", help="Path to config YAML",
    )
    parser.add_argument(
        "--copy-output", metavar="DIR",
        help="Copy output YAML to this directory",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    if args.setup:
        print("[ue_runner] Setup check:")
        ok = run_setup(config)
        sys.exit(0 if ok else 1)

    if not args.script:
        parser.error("script is required (or use --setup)")

    result = run_ue_script(
        script_path=args.script,
        force_mode=args.mode,
        config=config,
        copy_output_to=getattr(args, "copy_output", None),
        project=args.project,
        fallback_on_error=args.fallback_on_error,
    )

    # Print results
    if result.stdout:
        print(result.stdout)

    if result.error:
        print(f"\nERROR: {result.error}", file=sys.stderr)

    # Summary line
    status = "OK" if result.success else "FAILED"
    summary = f"[{status}] mode={result.mode} elapsed={result.elapsed:.1f}s"
    if result.output_file:
        summary += f" output={result.output_file}"
    print(f"\n{summary}", file=sys.stderr)

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
