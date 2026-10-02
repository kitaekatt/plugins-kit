"""scene-layers.py can be launched directly under the bootstrap interpreter.

hue-domain's SKILL.md names ``"${BOOTSTRAP_PYTHON:?...}"
"${HUE_KIT_ROOT:?...}/scripts/scene-layers.py"`` (and the bare ``hue-kit``
shim for hue_kit_cli.py) -- ``CLAUDE_PLUGIN_ROOT`` is unset in the Bash tool,
so an agent-typed command anchors on the ``<PLUGIN>_ROOT`` variable the
bootstrap engine exports. The bootstrap interpreter
has no requests/urllib3/pyyaml, so the script must re-exec under the plugin
venv before importing them -- but only when it runs as ``__main__``: the
suite (and nothing else) imports it as a module, and a re-exec at import
would relaunch the test runner.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "plugins" / "hue-kit" / "scripts" / "scene-layers.py"
SKILL = REPO_ROOT / "plugins" / "hue-kit" / "skills" / "hue-domain" / "SKILL.md"
THIRD_PARTY = {"requests", "urllib3", "yaml"}


def _imports(node) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom) and node.module:
        return {node.module.split(".")[0]}
    return set()


def test_main_guarded_reexec_precedes_third_party_imports():
    """Revert that turns this RED: delete the `if __name__ == "__main__":`
    re-exec block, or move it below `import requests`."""
    body = ast.parse(SCRIPT.read_text(encoding="utf-8")).body
    guard_at = next(
        i for i, node in enumerate(body)
        if isinstance(node, ast.If) and "__main__" in ast.dump(node.test)
        and "reexec_under_plugin_venv" in ast.dump(node))
    assert "'hue-kit'" in ast.dump(body[guard_at])
    first_third_party = next(i for i, node in enumerate(body) if _imports(node) & THIRD_PARTY)
    assert guard_at < first_third_party


def test_skill_md_names_the_launcher_form():
    from bootstrap_lib.interpreter_env import PLUGIN_CALL_SITE_EXPR as expr

    text = SKILL.read_text(encoding="utf-8")
    # hue_kit_cli.py is launched by the bare `hue-kit` name (bin/ is on the
    # session PATH), so no plugin-root form is needed for it.
    assert "command: hue-kit [--dir PATH]" in text
    assert not [
        line for line in text.splitlines()
        if "${CLAUDE_PLUGIN_ROOT}" in line and "hue_kit_cli.py" in line
    ], "hue_kit_cli.py must be launched as bare `hue-kit`, not via CLAUDE_PLUGIN_ROOT"
    # scene-layers.py has no bin/ shim, so it keeps the launcher form. The
    # path sits in the skill body, where Claude Code substitutes
    # CLAUDE_PLUGIN_ROOT before the command runs, so it is rooted on that
    # variable and not on the bootstrap-exported <PLUGIN>_ROOT name.
    assert f'{expr} "${{CLAUDE_PLUGIN_ROOT}}/scripts/scene-layers.py"' in text
    assert "HUE_KIT_ROOT" not in text
    assert "HUE_KIT_VENV" not in text
