#!/usr/bin/env python3
"""Bootstrap and dispatch one configured endpoint review lane.

This thin wrapper is byte-identical in git-kit and p4-kit. It owns bootstrap
setup and the absent-library diagnostic; the seam-calling implementation lives
in llm_kit.dispatch.review_lane, which each kit installs as the pinned
katsoft-llm-kit package (its pyproject.toml). The pin guarantees the lane's
flags and signatures, so this wrapper probes none of them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from bootstrap_guard import reexec_under_plugin_venv

_VERSION_DIR = re.compile(r"^\d+\.\d+\.\d+")


def _plugin_name() -> str:
    """The owning plugin's directory name, in BOTH install layouts.

    A dev checkout is ``plugins/<plugin>/scripts/``, so the plugin is the
    script's grandparent. An INSTALLED plugin is
    ``<marketplace>/<plugin>/<version>/scripts/``, so the grandparent is the
    VERSION -- and a fixed parent index resolved to e.g. "0.35.0". Then
    ``plugin_venv_python("0.35.0")`` finds no venv, ``reexec_under_plugin_venv``
    returns quietly by design, the shared-lib ``.pth`` never reaches sys.path,
    and the run dies on the bootstrap guard with a misleading "has not
    provisioned ... (missing: bootstrap_lib)" -- while the venv is in fact
    provisioned and correct.

    That failure is doubly costly because the guard exits 0: the caller reads a
    broken lane as a review that found nothing. So this only ever reproduced for
    INSTALLED users, never in a dev checkout, which is why it survived.

    Walking up and skipping the version segment resolves the plugin in either
    layout; the old expression stays as the fallback.
    """
    for part in reversed(Path(__file__).resolve().parts[:-1]):
        if part == "scripts" or _VERSION_DIR.match(part):
            continue
        return part
    return Path(__file__).resolve().parents[1].name


_PLUGIN_NAME = _plugin_name()

reexec_under_plugin_venv(_PLUGIN_NAME)

try:
    from bootstrap_lib.path_repair import repair_path
except ImportError:
    from bootstrap_guard import require_bootstrap

    require_bootstrap(
        _PLUGIN_NAME, feature="code review", missing="bootstrap_lib", force=True
    )

repair_path()

_EXIT_USAGE = 2


def _refuse_absent(exc: BaseException) -> None:
    print(
        "the katsoft-llm-kit package is not installed in this plugin's venv, "
        "so this endpoint lane cannot start. Bootstrap installs it from the "
        "plugin's pyproject.toml; run `bootstrap run` (or start a new "
        "session) so bootstrap provisions the venv. An Agent-model review "
        "lane needs no llm_kit.",
        file=sys.stderr,
    )
    raise SystemExit(_EXIT_USAGE) from exc


try:
    from llm_kit.dispatch.review_lane import main as _main
except ModuleNotFoundError as exc:
    if exc.name == "llm_kit":
        _refuse_absent(exc)
    raise

if __name__ == "__main__":
    sys.exit(_main())
