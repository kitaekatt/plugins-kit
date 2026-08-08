"""Custom bootstrap script for unreal-kit.

Two entry points:
- autodetect(): Discovers .uproject and engine_dir from CWD (no-arg, returns dict | None)
- bootstrap(ctx): Checks whether the durable enriched stub is present and fresh
"""

import filecmp
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional


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
    uproject = ctx.config.get("uproject")
    project_root = getattr(ctx, "project_dir", None)
    if not uproject or not project_root:
        return

    from bootstrap_lib.config_resolve import resolve_plugin_data_dir

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
    if defer is None:
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
            "then run `python ${CLAUDE_PLUGIN_ROOT}/scripts/"
            "refresh_unreal_stub.py --project-root <project-root>`."
        ),
        satisfied_by=(
            "python ${CLAUDE_PLUGIN_ROOT}/scripts/refresh_unreal_stub.py "
            "--project-root <project-root>"
        ),
    )
    ctx.log_ok("stubs: durable enriched stub refresh deferred to explicit action")
