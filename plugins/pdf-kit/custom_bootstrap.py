"""Bootstrap script for pdf-kit.

Single entry point ``bootstrap(ctx)`` ensures the html-pdf skill's browser
engine is present. Playwright (the Python package) is provisioned by the
declared venv (``uv sync --project``); the Chromium *browser binary* it drives
is a separate ~180 MB download that `playwright install chromium` fetches into
the shared Playwright cache (``~/.cache/ms-playwright`` /
``%LOCALAPPDATA%\\ms-playwright``).

The install is run via ``uv run --project`` so it executes against the plugin's
venv (and uv first syncs that venv, so this is correct even if the engine has
not run the venv check yet). It is idempotent -- Playwright skips browsers that
are already present -- but it is still a subprocess, so a marker file in the
plugin data dir short-circuits it on subsequent sessions.
"""

import subprocess
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent
MARKER_NAME = "chromium.installed"


def bootstrap(ctx: Any) -> None:
    """Ensure the Chromium browser html-pdf needs is installed."""
    marker = Path(ctx.data_dir) / MARKER_NAME
    if marker.exists():
        # Steady state: nothing to do. Verbose-only so a healthy bootstrap stays
        # silent instead of re-displaying this every session (see llm-scripting-kit's
        # cached-validation branch for the same idiom).
        ctx.log_ok("html-pdf: chromium already installed (cached)")
        return

    cmd = [
        "uv", "run", "--project", str(PLUGIN_ROOT),
        "python", "-m", "playwright", "install", "chromium",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        # uv not on PATH yet -- the tools/venv bootstrap will install it; we
        # retry next session. Don't block bootstrap on a transient ordering gap.
        ctx.log("html-pdf: chromium install deferred (uv not found yet)")
        return

    if result.returncode != 0:
        ctx.log(
            "html-pdf: chromium install failed, will retry next session: "
            + (result.stderr or result.stdout or "").strip()[:300]
        )
        return

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok\n", encoding="utf-8")
    except OSError:
        pass
    ctx.log("html-pdf: chromium installed")


if __name__ == "__main__":
    # Allow manual invocation for debugging: prints what bootstrap would do.
    class _Ctx:
        data_dir = str(PLUGIN_ROOT / ".bootstrap-data")

        def log(self, msg: str) -> None:
            print(msg, file=sys.stderr)

        def log_ok(self, msg: str) -> None:
            print(msg, file=sys.stderr)

    bootstrap(_Ctx())
