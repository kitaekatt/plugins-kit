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
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
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
from ue_runner_config import ConfigError, RunnerConfig, load_config


@dataclass
class RunResult:
    success: bool
    mode: str  # "remote" or "commandlet"
    stdout: str = ""
    stderr: str = ""
    output_file: str | None = None
    elapsed: float = 0.0
    error: str = ""
    # True when the remote dispatch may have reached UE but its completion was
    # lost.  Such a result must never be replayed through a commandlet.
    completion_unknown: bool = False


@dataclass(frozen=True)
class _CommandletInvocation:
    """Private files used to correlate one commandlet process with its script."""

    directory: Path
    wrapper: Path
    completion: Path
    token: str


class ProjectResolutionError(ValueError):
    """An explicit project target was supplied but cannot be used."""


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _validate_commandlet_timeout(timeout_s):
    if timeout_s is None:
        return None
    try:
        value = float(timeout_s)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"commandlet_timeout_s must be a finite positive number; got {timeout_s!r}"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"commandlet_timeout_s must be a finite positive number; got {timeout_s!r}"
        )
    return value


def run_ue_script(
    script_path: str,
    force_mode: str | None = None,
    config: RunnerConfig | None = None,
    copy_output_to: str | None = None,
    project: str | None = None,
    fallback_on_error: bool = False,
    commandlet_timeout_s: float | None = None,
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
        commandlet_timeout_s: Optional finite positive limit for the headless
                 commandlet. Unset preserves the historical unbounded behavior.

    Returns:
        RunResult with execution details.
    """
    try:
        commandlet_timeout_s = _validate_commandlet_timeout(commandlet_timeout_s)
    except ValueError as exc:
        return RunResult(success=False, mode="commandlet", error=str(exc))

    if config is None:
        try:
            config = load_config()
        except ConfigError as exc:
            return RunResult(success=False, mode="none", error=str(exc))

    script_path = os.path.abspath(script_path)
    if not os.path.isfile(script_path):
        return RunResult(success=False, mode="none", error=f"Script not found: {script_path}")

    # Resolve which project to target: explicit flag > CWD > script path > config
    try:
        config = _resolve_project(config, script_path, project)
    except ProjectResolutionError as e:
        return RunResult(success=False, mode="none", error=str(e))

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
                and not result.completion_unknown
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

    result = _run_commandlet(script_path, config, timeout_s=commandlet_timeout_s)
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

    from ue_identity import ExpectedProjectMismatch, compare_project_paths

    remote_cfg = upyre.RemoteExecutionConfig(
        multicast_group=(config.remote.multicast_group, config.remote.multicast_port),
        multicast_bind_address=config.remote.multicast_bind_address,
    )

    # The editor for THIS workspace is identified by its absolute project dir.
    # Multiple editors on one host share the multicast group and the uproject
    # basename (e.g. "MyGame"), so the only safe disambiguator is the dir.
    expected_root = os.path.dirname(os.path.abspath(config.uproject)) if config.uproject else ""

    class _ProjectFilteredConnection(upyre.PythonRemoteConnection):
        """Connect only to the editor whose advertised ``project_root`` matches
        ``expected_root``. Other editors see one multicast ping/pong and nothing
        more — they never get an OpenConnectionMessage or a Python statement.

        Raises ``upyre.ConnectionError`` when no editor answers at all (caller
        falls back to commandlet silently), and ``ExpectedProjectMismatch`` when
        editor(s) answered but none belong to this workspace (caller warns, then
        falls back).
        """

        def open_connection(self):
            ping = upyre.PingMessage(self.config)
            ping.send(self.mcastsock)

            pongs = list(ping.raw_receive(self.mcastsock))
            if not pongs:
                raise upyre.ConnectionError("Connection failed.")

            match = None
            seen_roots = []
            for pong in pongs:
                data = pong.get("data", {}) or {}
                actual_root = data.get("project_root", "")
                seen_roots.append(actual_root or "<no project_root>")
                if compare_project_paths(expected_root, actual_root) is None:
                    match = pong
                    break

            if match is None:
                raise ExpectedProjectMismatch(
                    "No running editor matches this workspace "
                    f"('{expected_root}'). Editors that answered: "
                    f"{', '.join(seen_roots)}."
                )

            self.unreal_node_id = match["source"]
            self.connection_infos = match["data"]
            upyre.OpenConnectionMessage(self.unreal_node_id, self.config).send(self.mcastsock)
            self.connection_created = True
            self.remote_command_connection = upyre.PythonRemoteCommandConnection(
                self.unreal_node_id, self.config
            )

    # Snapshot output dir before execution
    output_dir = _get_output_dir(config)
    pre_snapshot = _snapshot_output_dir(output_dir)

    start = time.time()
    dispatch_started = False
    try:
        with _ProjectFilteredConnection(remote_cfg) as conn:
            # EXECUTE_FILE tells UE to load and run a .py file by path.
            # EXECUTE_STATEMENT would exec() inline code instead.
            # Mark this before crossing the library boundary: a timeout or
            # connection exception from this call cannot prove whether UE
            # received and started the script.
            dispatch_started = True
            cmd_result = conn.execute_python_command(
                script_path,
                exec_type=upyre.ExecTypes.EXECUTE_FILE,
                raise_exc=False,
            )
    except ExpectedProjectMismatch as e:
        # An editor is running, but not for this workspace. Do NOT execute in it
        # — fall back to the (workspace-correct) commandlet path.
        _warn(f"{e} Falling back to commandlet...")
        return None
    except Exception as e:
        err_str = str(e)
        if dispatch_started:
            elapsed = time.time() - start
            return RunResult(
                success=False,
                mode="remote",
                stderr=err_str,
                elapsed=elapsed,
                error=(
                    "Remote dispatch completion is unknown: "
                    f"{err_str}. Not retrying via commandlet because the "
                    "script may already have run."
                ),
                completion_unknown=True,
            )
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


def _run_commandlet(
    script_path: str, config: RunnerConfig, timeout_s: float | None = None
) -> RunResult:
    """Run script via UnrealEditor-Cmd.exe -run=pythonscript."""
    try:
        timeout_s = _validate_commandlet_timeout(timeout_s)
    except ValueError as exc:
        return RunResult(success=False, mode="commandlet", error=str(exc))

    exe = config.editor_cmd_exe
    uproject = config.uproject

    _info(f"Running commandlet...")

    # Snapshot output dir before execution
    output_dir = _get_output_dir(config)
    pre_snapshot = _snapshot_output_dir(output_dir)

    start = time.time()
    project_dir = Path(uproject).parent
    with _make_commandlet_invocation(script_path, output_dir, project_dir) as invocation:
        command = [
            exe,
            uproject,
            "-run=pythonscript",
            f"-script={invocation.wrapper}",
            "-stdout",
            "-Unattended",
            "-NoLoadStartupPackages",
            "-FullStdOutLogOutput",
        ]
        _info(f"  {' '.join(command)}")
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _as_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
            stderr = _as_text(getattr(exc, "stderr", None))
            detail = f"Commandlet timed out after {timeout_s:g}s; completion is unknown"
            evidence = "Partial stderr was retained." if stderr else "Partial output was retained."
            return RunResult(
                success=False,
                mode="commandlet",
                stdout=stdout,
                stderr=stderr,
                elapsed=time.time() - start,
                error=f"{detail}. {evidence}",
            )
        except FileNotFoundError:
            return RunResult(
                success=False, mode="commandlet",
                error=f"Editor executable not found: {exe}",
            )

        completion_outputs = _read_completion_outputs(invocation, script_path)

    elapsed = time.time() - start
    if completion_outputs is not None:
        output_file = _first_existing_output(completion_outputs)
    else:
        # Preserve the historical zero-exit behavior for an external commandlet
        # wrapper that did not emit our private record.  This branch is not a
        # reason to tolerate a nonzero exit: output alone is never completion
        # evidence for this invocation.
        output_file = _find_new_output(output_dir, pre_snapshot) if proc.returncode == 0 else None

    # UE commandlets frequently exit non-zero due to asset loading warnings
    # (e.g. Niagara modules) that are unrelated to the Python script.
    # A nonzero exit may be tolerated only when our wrapper recorded normal
    # completion for this exact invocation.  Output-file presence alone is not
    # evidence because another process may have changed the directory.
    has_script_error = _detect_script_error(
        f"{proc.stdout}\n{proc.stderr}", script_path
    )

    if has_script_error:
        success = False
    elif completion_outputs is not None:
        # The wrapper writes the record only after normal completion, including
        # an intentional SystemExit(0).
        success = True
    else:
        success = proc.returncode == 0

    error = ""
    if not success:
        if has_script_error:
            error = "Python script error detected in commandlet output (see stdout/stderr)"
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


def _make_commandlet_invocation(
    script_path: str, output_dir: Path | None, project_dir: Path
):
    """Create a disposable wrapper and its invocation-specific completion path.

    The wrapper runs the consumer script through ``runpy.run_path`` so the
    script sees its own ``__file__`` and ``__main__`` context.  It writes the
    completion record only after normal return or ``SystemExit(0)``.  The
    project-local ephemeral directory also prevents stale records from an
    earlier run from being accepted without writing user data beside the code.
    """
    invocation_root = project_dir / ".local-data" / "unreal-kit" / "commandlet"
    invocation_root.mkdir(parents=True, exist_ok=True)
    temp_dir = tempfile.TemporaryDirectory(prefix="ue-commandlet-", dir=invocation_root)
    directory = Path(temp_dir.name)
    token = secrets.token_urlsafe(32)
    wrapper = directory / "invoke.py"
    completion = directory / "completion.json"
    target = str(Path(script_path).resolve())
    output = str(output_dir.resolve()) if output_dir is not None else ""
    wrapper.write_text(
        _invocation_wrapper_source(
            target=target,
            output_dir=output,
            completion=str(completion),
            token=token,
        ),
        encoding="utf-8",
    )
    return _InvocationContext(temp_dir, _CommandletInvocation(directory, wrapper, completion, token))


class _InvocationContext:
    def __init__(self, temp_dir, invocation: _CommandletInvocation):
        self._temp_dir = temp_dir
        self.invocation = invocation

    def __enter__(self):
        return self.invocation

    def __exit__(self, exc_type, exc_value, traceback):
        self._temp_dir.cleanup()
        return False


def _invocation_wrapper_source(*, target: str, output_dir: str, completion: str, token: str) -> str:
    """Return source for the commandlet-side per-invocation wrapper."""
    return f'''import json
import runpy
from pathlib import Path

_TARGET = {target!r}
_OUTPUT_DIR = {output_dir!r}
_COMPLETION = {completion!r}
_TOKEN = {token!r}


def _snapshot():
    if not _OUTPUT_DIR:
        return {{}}
    root = Path(_OUTPUT_DIR)
    if not root.is_dir():
        return {{}}
    result = {{}}
    for item in root.iterdir():
        if item.suffix.lower() in (".yaml", ".yml") and item.is_file():
            stat = item.stat()
            result[str(item)] = (stat.st_mtime_ns, stat.st_size)
    return result


def _write_completion(before):
    after = _snapshot()
    changed = [
        path for path, signature in after.items()
        if before.get(path) != signature
    ]
    record = {{"token": _TOKEN, "script": _TARGET, "outputs": changed}}
    temporary = Path(_COMPLETION + ".tmp")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    temporary.replace(_COMPLETION)


_before = _snapshot()
try:
    # The commandlet invokes this wrapper, but the consumer must observe the
    # original script as argv[0] and receive no wrapper implementation args.
    import sys
    sys.argv = [_TARGET]
    runpy.run_path(_TARGET, run_name="__main__")
except SystemExit as exc:
    if exc.code not in (None, 0):
        raise
    _write_completion(_before)
else:
    _write_completion(_before)
'''


def _read_completion_outputs(
    invocation: _CommandletInvocation, script_path: str
) -> list[str] | None:
    """Validate this invocation's completion record and return its outputs."""
    try:
        record = json.loads(invocation.completion.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("token") != invocation.token:
        return None
    try:
        expected_script = str(Path(script_path).resolve())
    except (OSError, RuntimeError):
        return None
    if record.get("script") != expected_script:
        return None
    outputs = record.get("outputs", [])
    if not isinstance(outputs, list) or not all(isinstance(path, str) for path in outputs):
        return None
    return outputs


def _first_existing_output(outputs: list[str]) -> str | None:
    """Return the first output observed by the wrapper that still exists."""
    for path in outputs:
        if Path(path).is_file():
            return path
    return None


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
            raise ProjectResolutionError(
                f"--project path is not a usable .uproject file: {p}"
            )

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
        _load_yaml,
        _validate_layer,
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
    # Setup is an update operation. Preserve unrelated project settings while
    # replacing only the two fields selected by this interactive run.
    try:
        existing = _load_yaml(config_path, required=False)
        if not isinstance(existing, dict):
            existing = {}
        _validate_layer(existing, config_path)
        existing.update(data)
        _validate_layer(existing, config_path)
        written = _write_project_config(project_root, existing)
    except ConfigError as exc:
        print(f"  ERROR: {exc}")
        return False
    except (OSError, TypeError, ValueError) as exc:
        print(f"  ERROR: Could not write project config: {exc}")
        return False
    # Reload config with the new file
    from ue_runner_config import load_config as _reload
    try:
        config = _reload()
    except ConfigError as exc:
        print(f"  ERROR: {exc}")
        return False
    print(f"  WROTE {written}")
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
    parser.add_argument(
        "--commandlet-timeout", type=float, metavar="SECONDS",
        help="Bound headless commandlet execution; unset preserves legacy behavior",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"[ue_runner] ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

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
        commandlet_timeout_s=args.commandlet_timeout,
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
