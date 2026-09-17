"""A plugin that calls the interpreter contract declares the bootstrap floor.

plugins/CLAUDE.md: "Set the requires_bootstrap floor from the CALLS a plugin
makes". The guarded launch form ``"${BOOTSTRAP_PYTHON:?requires bootstrap >=
X}"`` (and its nested project form) only works where bootstrap X's
SessionStart hook exported the name, and ``bootstrap_lib.interpreter_env``
first shipped in X, so any plugin whose shipped files use either must declare
``requires_bootstrap`` >= ``interpreter_env.MIN_VERSION``. A launcher shim
that only reads ``${BOOTSTRAP_PYTHON:-}`` as an optional fallback does not
call the contract and needs no floor.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from bootstrap_lib.interpreter_env import MIN_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]

CALL_MARKERS = (
    "${BOOTSTRAP_PYTHON:?",
    "${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?",
    "bootstrap_lib.interpreter_env",
    "PLUGIN_CALL_SITE_EXPR",
)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.lstrip(">=").split("."))


def _callers() -> set[str]:
    args = ["git", "-C", str(REPO_ROOT), "grep", "-l", "-F"]
    for marker in CALL_MARKERS:
        args += ["-e", marker]
    out = subprocess.run(args + ["--", "plugins"], capture_output=True, text=True,
                         check=False).stdout
    plugins = set()
    for rel in out.splitlines():
        parts = rel.split("/")
        if len(parts) >= 3 and parts[-1] != "bootstrap.json":
            plugins.add(parts[1])
    plugins.discard("bootstrap")
    return plugins


def test_the_caller_set_is_the_known_one():
    """A consistency check on the detector: a new caller must be seen."""
    assert _callers() == {
        "awesome-kit", "cache-kit", "git-kit", "hue-kit", "p4-kit",
        "skills-kit", "unreal-kit",
    }


def test_every_caller_declares_the_interpreter_floor():
    """Revert that turns this RED: set any caller's bootstrap.json
    requires_bootstrap back below MIN_VERSION (e.g. git-kit to 0.113.0), or
    delete hue-kit's requires_bootstrap line."""
    offenders = []
    for plugin in sorted(_callers()):
        manifest = json.loads(
            (REPO_ROOT / "plugins" / plugin / "bootstrap.json").read_text(encoding="utf-8"))
        floor = manifest.get("requires_bootstrap")
        if floor is None or _version(floor) < _version(MIN_VERSION):
            offenders.append(f"{plugin}: requires_bootstrap={floor!r} < {MIN_VERSION}")
    assert not offenders, "\n".join(offenders)
