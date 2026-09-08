#!/usr/bin/env python3
"""Bootstrap and dispatch one configured endpoint review lane.

This thin wrapper is byte-identical in git-kit and p4-kit. It owns bootstrap
setup and the REFUSE probe; the seam-calling implementation lives in
llm_scripting_kit.review_lane.
"""

from __future__ import annotations

import contextlib
import importlib
import io
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
        "the llm-scripting-kit plugin is not installed, so this endpoint lane "
        "cannot start. Install it with "
        "`claude plugin install llm-scripting-kit@plugins-kit` and start a "
        "new session so bootstrap links its shared library.",
        file=sys.stderr,
    )
    raise SystemExit(_EXIT_USAGE) from exc


def _refuse_too_old(
    reason: str,
    min_version: str = "0.29.0",
    capability: str = "llm_scripting_kit.review_lane.main",
) -> None:
    print(
        f"the installed llm-scripting-kit is too old for this endpoint lane: "
        f"{capability} requires owner version {min_version}. "
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

# review_lane.main is present but that alone does not mean it accepts every
# flag THIS wrapper's caller may pass. --claimed-file first became a valid
# review_lane._parse_args argument in llm-scripting-kit 0.37.0 (commit
# 3d047281); against an owner in 0.29.0..0.36.x the module/main presence
# checks above both PASS, and calling main() with --claimed-file on argv
# reaches argparse's own "unrecognized arguments" error, which exits 2 --
# the SAME code this wrapper uses for "this lane is not endpoint-eligible".
# The skill then tells the agent a 2 is a configuration error for the user
# to fix, never mentioning that llm-scripting-kit itself needs an update.
# Probe the CAPABILITY (does the parser accept --claimed-file) rather than
# comparing a version string, so a genuinely newer-but-differently-versioned
# owner is not misdiagnosed.
_CLAIMED_FILE_MIN_VERSION = "0.37.0"


def _supports_claimed_file() -> bool:
    """True if the installed review_lane._parse_args accepts --claimed-file.

    Parses a throwaway argv containing every required flag (--lane, --model,
    --chunk) plus --claimed-file. `--chunk` is `type=Path` in every version
    seen so far, which accepts any string without touching the filesystem,
    so this is safe to call before any real dispatch. A pre-0.37.0 parser
    raises SystemExit(2) ("unrecognized arguments: --claimed-file"); this
    function converts that signal into a plain bool and swallows the stderr
    argparse would otherwise print, since the caller reports its own message.
    """
    parse_args = getattr(_review_lane, "_parse_args", None)
    if not callable(parse_args):
        return False
    probe_argv = [
        "--lane", "_probe", "--model", "_probe", "--chunk", "_probe",
        "--claimed-file", "_probe",
    ]
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parse_args(probe_argv)
    except SystemExit:
        return False
    return True


if __name__ == "__main__":
    if "--claimed-file" in sys.argv[1:] and not _supports_claimed_file():
        _refuse_too_old(
            "review_lane._parse_args does not accept --claimed-file",
            min_version=_CLAIMED_FILE_MIN_VERSION,
            capability="--claimed-file support",
        )
    sys.exit(_main())
