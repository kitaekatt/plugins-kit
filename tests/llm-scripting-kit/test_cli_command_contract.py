"""The CLI resolution contract: ``constants.CLI_COMMAND``.

What this file is and is NOT. The LOAD-BEARING check for the bare-invocation
regression is the source guard
``tests/repo-scripts/test_llm_cli_invocation_standard.py``, which reads every
tracked shipped surface and fails on a bare ``llm-scripting-kit`` in command
position. The two derivation tests here cannot see a shipped surface at all;
on their own they would be a CONSISTENCY check -- a constant compared against a
string spelled out beside the module that defines it, green whenever both move
together (root CLAUDE.md, ``guard_cannot_see_its_own_subject``). What they add
over that guard is narrow and worth stating exactly: they catch an INVERTED or
dropped ``sys.platform`` branch, which the text guard is blind to because both
branches read as resolved paths.

:func:`test_contract_path_exists_on_a_provisioned_host` is what keeps the pair
from being purely self-referential -- it resolves the string against the REAL
host and asserts the console script is actually there, so a contract that names
a layout bootstrap does not produce goes red on any provisioned machine. It
skips where the plugin venv is absent, which is the one state in which the
filesystem has nothing to say.

The platform branch is exercised by RELOADING the module under a patched
``sys.platform``: ``CLI_COMMAND`` is computed at import time, so patching
``sys.platform`` alone would leave the already-computed constant untouched and
the test would silently only ever check this host's own platform.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from llm_scripting_kit import constants

#: The real user home, captured at import time -- the package-wide autouse
#: ``_isolated_host_state`` fixture redirects HOME/USERPROFILE at a temp dir,
#: and the filesystem probe below needs the actual provisioned location.
_REAL_HOME = Path.home()

_CONTRACT_DIR = "~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.venv/"
_WINDOWS_LEAF = "Scripts/llm-scripting-kit.exe"
_POSIX_LEAF = "bin/llm-scripting-kit"


def _cli_command_for(platform: str) -> str:
    """``CLI_COMMAND`` as the module's own derivation computes it for
    ``platform``, with the module left reloaded under the REAL platform again.

    Not ``monkeypatch.setattr(sys, "platform", ...)``: the restoring reload has
    to happen while the patch is still in force, and a fixture's teardown runs
    BEFORE monkeypatch's undo when it is requested second -- which silently left
    the module holding a foreign platform's value for the rest of the session
    (observed: the filesystem probe below then failed on Windows looking for the
    posix leaf). Save-and-restore in one ``finally`` has no such ordering."""
    real = sys.platform
    try:
        sys.platform = platform
        return importlib.reload(constants).CLI_COMMAND
    finally:
        sys.platform = real
        importlib.reload(constants)


def test_cli_command_branches_on_win32():
    """win32 resolves the ``Scripts/*.exe`` console script and every other
    platform resolves the ``bin/`` one, both under the version-free plugin
    data venv.

    Shown to fail: swap the arms of the ``sys.platform == "win32"`` conditional
    in ``constants.CLI_COMMAND`` and both assertions go red. (That inverted
    branch is the ONLY defect this test catches -- see the module docstring.)
    """
    assert _cli_command_for("win32") == _CONTRACT_DIR + _WINDOWS_LEAF
    for platform in ("linux", "darwin", "freebsd13"):
        assert _cli_command_for(platform) == _CONTRACT_DIR + _POSIX_LEAF
    # The helper restored the real platform, so the module is usable again.
    assert constants.CLI_COMMAND == _CONTRACT_DIR + (
        _WINDOWS_LEAF if sys.platform == "win32" else _POSIX_LEAF
    )


def test_cli_command_is_a_display_string_not_a_resolved_path():
    """The contract is the literal ``~/``-prefixed text a shipped surface
    prints, with the ``plugins-kit`` marketplace segment and no version
    segment -- deliberately NOT a resolved ``Path`` like ``USER_ENV_FILE``.

    Shown to fail: expand the tilde in ``constants.CLI_COMMAND`` (or
    reintroduce a version-keyed cache segment) and this goes red."""
    assert constants.CLI_COMMAND.startswith("~/")
    assert constants.CLI_COMMAND.startswith(_CONTRACT_DIR)
    assert isinstance(constants.CLI_COMMAND, str)
    # A version-keyed path is what made a bare name the only convenient form;
    # the contract exists because this one survives an upgrade.
    assert "/cache/" not in constants.CLI_COMMAND


_VENV_DIR = (
    _REAL_HOME / ".claude" / "plugins" / "data" / "plugins-kit"
    / "llm-scripting-kit" / ".venv"
)


@pytest.mark.skipif(
    not _VENV_DIR.is_dir(),
    reason="llm-scripting-kit is not provisioned on this host "
           f"({_VENV_DIR} is absent), so the filesystem cannot answer",
)
def test_contract_path_exists_on_a_provisioned_host():
    """The file the contract names is really there, on this host, under the
    real home. This is what stops the two derivation tests above from being a
    closed loop over one module's own literals.

    Shown to fail: point ``CLI_COMMAND`` at a layout bootstrap does not create
    (``.venv/scripts/`` lowercase, a ``bin/`` leaf on Windows, or a
    version-keyed cache path) and this goes red on any provisioned machine."""
    # Expanded against the REAL home, not os.path.expanduser: the autouse
    # isolation fixture repoints HOME/USERPROFILE at a temp directory.
    resolved = _REAL_HOME.joinpath(*constants.CLI_COMMAND[len("~/"):].split("/"))
    assert resolved.is_file(), (
        f"the CLI resolution contract names {constants.CLI_COMMAND}, which "
        f"resolves to {resolved} and does not exist, although the plugin venv "
        f"at {_VENV_DIR} does"
    )
