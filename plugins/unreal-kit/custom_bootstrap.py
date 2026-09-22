"""Custom bootstrap script for unreal-kit.

Two entry points:
- autodetect(): Discovers .uproject and engine_dir from CWD (no-arg, returns dict | None)
- bootstrap(ctx): Checks whether the durable enriched stub is present and fresh
"""

import filecmp
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional


_P4_MARKERS = (".p4config.txt", ".p4config", ".p4ignore.txt", ".p4ignore")


def _in_p4_workspace(path: Path) -> bool:
    """Detect a Perforce workspace without spawning the optional p4 client."""
    directory = path.resolve()
    while True:
        if any((directory / marker).is_file() for marker in _P4_MARKERS):
            return True
        parent = directory.parent
        if parent == directory:
            return False
        directory = parent


def _check_redirector_p4(ctx: Any, project_root: Path) -> None:
    """Record P4 only for a detected workspace that can use redirector cleanup."""
    if not _in_p4_workspace(project_root):
        ctx.log_ok("redirectors: skipped - no Perforce workspace marker")
        return
    if shutil.which("p4"):
        ctx.log_ok("redirectors: P4 client is available")
        return

    defer = getattr(ctx, "add_deferred_requirement", None)
    if not callable(defer):
        ctx.log(
            "redirectors: P4 client unavailable - bootstrap engine does not "
            "support deferred requirements"
        )
        return
    defer(
        "unreal_redirector_p4",
        user_msg=(
            "Redirector cleanup is available for this Perforce workspace, "
            "but its optional P4 provider is not installed."
        ),
        agent_msg=(
            "The fix-up-redirectors capability requires the P4 client in a "
            "detected Perforce workspace. Install the optional provider with "
            "`claude plugin install p4-kit@plugins-kit`, then retry the "
            "redirector cleanup action. Python and MCP capabilities do not "
            "need this provider."
        ),
        satisfied_by="claude plugin install p4-kit@plugins-kit",
    )
    ctx.log_ok("redirectors: P4 client missing - requirement deferred to point of need")


def autodetect() -> Optional[Dict[str, str]]:
    """Discover .uproject and engine_dir from CWD.

    Returns dict of discovered field values, or None if no project found.
    Called by the engine's project_config primitive (no arguments).
    """
    skill_lib = os.path.join(os.path.dirname(__file__), "lib")
    if skill_lib not in sys.path:
        sys.path.insert(0, skill_lib)

    from ue_discovery import find_uproject_files, find_engine_dir

    # Search CWD only (no walk-up) -- autodetect runs from the project root,
    # so walking up would find unrelated .uproject files in parent dirs.
    found = find_uproject_files(Path.cwd().resolve(), max_depth=2)
    uproject = found[0] if found else None
    if not uproject:
        return None

    result: Dict[str, str] = {"uproject": str(uproject)}
    engine = find_engine_dir(uproject)
    if engine:
        result["engine_dir"] = str(engine)
    return result


def bootstrap(ctx: Any) -> None:
    """Check the optional durable enriched stub without writing project data."""
    config = getattr(ctx, "config", None) or {}
    uproject = config.get("uproject") if hasattr(config, "get") else None
    project_root = getattr(ctx, "project_dir", None)
    if not uproject:
        ctx.log("stubs: skipped - no uproject configured")
        return
    if not project_root:
        ctx.log("stubs: skipped - project directory is unavailable")
        return

    _check_redirector_p4(ctx, Path(project_root))

    from bootstrap_lib.config_resolve import resolve_plugin_data_dir
    from bootstrap_lib.interpreter_env import PLUGIN_CALL_SITE_EXPR

    # refresh_unreal_stub.py re-execs into unreal-kit's own provisioned venv
    # (bootstrap_guard.reexec_under_plugin_venv), so any interpreter that can
    # reach it is sufficient -- but an agent typing this message verbatim has
    # neither `uv` nor the plugin venv resolved for it. Route through the
    # guarded $BOOTSTRAP_PYTHON expression bootstrap exports into every
    # session instead of a bare `python` (see /bootstrap fact
    # python_interpreter and python-interpreter.md).
    refresh_stub_launcher = (
        f"{PLUGIN_CALL_SITE_EXPR} ${{CLAUDE_PLUGIN_ROOT}}/scripts/"
        "refresh_unreal_stub.py --project-root <project-root>"
    )

    generated_stub = (
        Path(uproject).parent / "Intermediate" / "PythonStub" / "unreal.py"
    )
    durable_stub = (
        resolve_plugin_data_dir(
            project_root,
            marketplace="plugins-kit",
            plugin="unreal-kit",
            config=ctx.config,
        )
        / "unreal.py"
    )

    durable_present = durable_stub.is_file() and durable_stub.stat().st_size > 0
    stale = (
        durable_present
        and generated_stub.is_file()
        and not filecmp.cmp(generated_stub, durable_stub, shallow=False)
    )
    if durable_present and not stale:
        if generated_stub.is_file():
            ctx.log_ok("stubs: durable enriched stub is current")
        else:
            ctx.log_ok(
                "stubs: durable enriched stub is present; generated source is "
                "unavailable for comparison"
            )
        return

    defer = getattr(ctx, "add_deferred_requirement", None)
    if not callable(defer):
        ctx.log(
            "stubs: unavailable - bootstrap engine does not support "
            "deferred requirements"
        )
        return
    defer(
        "unreal_enriched_stub",
        user_msg=(
            "The consuming project's enriched Unreal API stub is absent or stale. "
            "Stock API search remains available when its machine-local stub exists."
        ),
        agent_msg=(
            "Unreal API search prefers the consuming project's enriched stub, "
            "which is absent or stale. If the machine-local stock stub is also "
            "missing, start a new Claude Code session so bootstrap can download "
            "it. If project-specific API search is needed, enable Developer Mode, "
            "complete a full compile so Intermediate/PythonStub/unreal.py exists, "
            f"then run `{refresh_stub_launcher}`."
        ),
        satisfied_by=refresh_stub_launcher,
    )
    ctx.log_ok("stubs: durable enriched stub refresh deferred to explicit action")
