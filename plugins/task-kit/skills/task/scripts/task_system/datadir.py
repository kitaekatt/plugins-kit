"""The plugin's bootstrap data directory: ``<data root>/<marketplace>/task-kit``.

Machine-scoped state (the dashboard's runtime root and the working-session lock files) lives under it.
The resolver reads the directory from the venv this process runs in, so it works only under the
bootstrap-provisioned venv; anything else is an error, never a guess.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

PLUGIN_NAME = "task-kit"
VENV_DIRNAME = ".venv"
DATA_ROOT_ENV = "CLAUDE_BOOTSTRAP_DATA_ROOT"


class DataDirError(RuntimeError):
    """The data directory cannot be resolved for this interpreter."""


def resolve_data_dir(
    env: Mapping[str, str] | None = None, prefix: str | None = None
) -> Path:
    """The plugin's bootstrap data directory, from the venv this process runs in.

    The provisioned venv is ``<data root>/<marketplace>/task-kit/.venv``, so
    its parent IS the data directory and the marketplace comes from the
    resolved bootstrap context rather than a literal. When
    ``CLAUDE_BOOTSTRAP_DATA_ROOT`` redirects the data tree (a claudx session),
    a venv outside that root is refused: running the production venv's data
    directory in a test session would read the production ``server.json``.
    """
    env = os.environ if env is None else env
    venv = Path(sys.prefix if prefix is None else prefix)
    try:
        venv = venv.resolve()
    except OSError as exc:
        raise DataDirError(f"cannot resolve the venv path {venv}: {exc}") from exc
    if venv.name != VENV_DIRNAME or venv.parent.name != PLUGIN_NAME:
        raise DataDirError(
            "the task-kit dashboard must run under its bootstrap-provisioned venv "
            f"(<data root>/<marketplace>/{PLUGIN_NAME}/{VENV_DIRNAME}); this interpreter's "
            f"prefix is {venv}. Start it with the task-kit launcher, or pass --data-dir."
        )
    data_dir = venv.parent
    redirected = env.get(DATA_ROOT_ENV)
    if redirected:
        data_root = data_dir.parent.parent
        want = os.path.normcase(os.path.realpath(redirected))
        if os.path.normcase(str(data_root)) != want:
            raise DataDirError(
                f"{DATA_ROOT_ENV} redirects the data tree to {redirected}, but this "
                f"interpreter belongs to the data directory {data_dir}; refusing to use "
                "the dashboard state of another data tree."
            )
    return data_dir
