#!/usr/bin/env python3
"""Bootstrap and dispatch one configured endpoint review lane.

This thin wrapper is byte-identical in git-kit and p4-kit. It owns bootstrap
setup and the REFUSE probe; the seam-calling implementation lives in
llm_scripting_kit.review_lane.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from bootstrap_guard import reexec_under_plugin_venv

_PLUGIN_NAME = Path(__file__).resolve().parents[1].name

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
        "the llm-scripting-kit plugin is not installed, so this endpoint lane "
        "cannot start. Install it with "
        "`claude plugin install llm-scripting-kit@plugins-kit` and start a "
        "new session so bootstrap links its shared library.",
        file=sys.stderr,
    )
    raise SystemExit(_EXIT_USAGE) from exc


def _refuse_too_old(reason: str) -> None:
    print(
        "the installed llm-scripting-kit is too old for this endpoint lane: "
        "llm_scripting_kit.review_lane.main requires owner version 0.29.0. "
        "Update it with "
        "`claude plugin update llm-scripting-kit@plugins-kit` and start a "
        "new session so bootstrap re-syncs its shared library. "
        f"Underlying error: {reason}",
        file=sys.stderr,
    )
    raise SystemExit(_EXIT_USAGE)


try:
    import llm_scripting_kit
except ModuleNotFoundError as exc:
    if exc.name == "llm_scripting_kit":
        _refuse_absent(exc)
    raise

try:
    _review_lane = importlib.import_module("llm_scripting_kit.review_lane")
except ModuleNotFoundError as exc:
    if exc.name == "llm_scripting_kit.review_lane":
        _refuse_too_old(str(exc))
    raise

_main = getattr(_review_lane, "main", None)
if not callable(_main):
    _refuse_too_old("review_lane.main is missing")

if __name__ == "__main__":
    sys.exit(_main())
