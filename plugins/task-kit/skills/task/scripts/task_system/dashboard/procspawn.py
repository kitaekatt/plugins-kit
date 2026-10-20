"""How the dashboard creates processes: the one place that reads the platform.

Three launches share one decision, "is this Windows", and each needs different
``Popen`` keyword arguments:

- ``detached`` -- the server and each run's runner. POSIX starts a new session.
  Windows uses ``CREATE_NO_WINDOW`` (the process gets a hidden console of its
  own, so no console grandchild ever flashes a window) plus a new process
  group. ``DETACHED_PROCESS`` is NOT combined with it: that pairing makes
  ``CREATE_NO_WINDOW`` a no-op and lets console grandchildren open windows.
- ``hidden`` -- the runner's command. POSIX starts a new session (so the whole
  tree can be signalled as a group); Windows uses ``CREATE_NO_WINDOW`` only,
  because the command must stay inside the runner's Job Object.

Callers pass ``windows=WINDOWS``. The parameter exists so a test can compose
both answers on any host: the ``subprocess.CREATE_*`` constants are read
directly, never through a default, so a missing constant on the wrong host
fails loudly rather than silently becoming 0.
"""

from __future__ import annotations

import os
import subprocess

WINDOWS = os.name == "nt"


def detached(*, windows: bool) -> dict:
    """Keyword arguments that detach a long-lived process from its launcher."""
    if windows:
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def hidden(*, windows: bool) -> dict:
    """Keyword arguments for a command the runner starts and later signals or kills as a tree."""
    if windows:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}
