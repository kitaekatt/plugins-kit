"""Install and cache the Chromium browser required by the html-pdf skill.

The Playwright package is provisioned in the plugin venv by the engine's venv
phase, which runs before this script. The Chromium *browser binary* it drives is
a separate ~180 MB download that ``playwright install chromium`` fetches into
Playwright's shared browser cache. This script runs that install with the
provisioned venv's python and records, in a marker in the plugin data dir, the
playwright version it installed for and the Chromium executable path it produced.

Steady state costs no subprocess: the venv's playwright version is read from its
``dist-info`` directory on disk and the recorded executable is checked with
``exists()``. A playwright version change (a different Chromium build) or a vanished executable
(cleared browser cache) re-runs the install; a failed install is recorded as a
deferred requirement so the html-pdf skill can relay the fix at the point of need
instead of the pass reading as success.
"""

import json
import subprocess
from pathlib import Path
from typing import Any, Optional

MARKER_NAME = "chromium.installed"
# Run only after an install: asks Playwright where it put Chromium.
EXECUTABLE_PROBE = (
    "import json; from playwright.sync_api import sync_playwright; "
    "p = sync_playwright().start(); "
    "print(json.dumps({'executable_path': p.chromium.executable_path})); p.stop()"
)


def _venv_python(data_dir: Path) -> Optional[Path]:
    """Return the provisioned venv python, using the engine's candidate order."""
    for candidate in (
        data_dir / ".venv" / "bin" / "python",
        data_dir / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.is_file():
            return candidate
    return None


def _playwright_version(python: Path) -> Optional[str]:
    """Read the installed playwright version from the venv's dist-info, no subprocess."""
    venv = python.parents[1]
    for pattern in ("lib/python*/site-packages", "Lib/site-packages"):
        for site in venv.glob(pattern):
            for info in site.glob("playwright-*.dist-info"):
                return info.name[len("playwright-"):-len(".dist-info")]
    return None


def _defer(ctx: Any, command: str, diagnostic: str) -> None:
    """Log the failure and record a point-of-need Chromium install request."""
    ctx.log(f"html-pdf: chromium install failed: {diagnostic[-2000:]}")
    ctx.add_deferred_requirement(
        "chromium",
        user_msg="html-pdf needs Chromium before it can render a PDF.",
        agent_msg=(
            "Chromium is not available for html-pdf. Run the prepared install "
            f"command, then retry: {command}"
        ),
        satisfied_by=command,
    )


def _marker_is_current(marker: Path, version: str) -> bool:
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(recorded, dict)
        and recorded.get("playwright_version") == version
        and bool(recorded.get("executable_path"))
        and Path(recorded["executable_path"]).exists()
    )


def bootstrap(ctx: Any) -> None:
    """Ensure the Chromium browser html-pdf needs is installed and current."""
    data_dir = Path(ctx.data_dir)
    python = _venv_python(data_dir)
    command = (
        f"{python or data_dir / '.venv' / 'bin' / 'python'} -m playwright install chromium"
    )
    if python is None:
        _defer(ctx, command, "provisioned venv python is missing")
        return
    version = _playwright_version(python)
    if version is None:
        _defer(ctx, command, "playwright is not installed in the plugin venv")
        return

    marker = data_dir / MARKER_NAME
    if _marker_is_current(marker, version):
        # Verbose-only so a healthy bootstrap stays silent (see the test module).
        ctx.log_ok("html-pdf: chromium already installed (cached)")
        return

    try:
        result = subprocess.run(
            [str(python), "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            _defer(ctx, command, (result.stderr or result.stdout or "").strip())
            return
        probe = subprocess.run(
            [str(python), "-c", EXECUTABLE_PROBE], capture_output=True, text=True,
        )
        if probe.returncode != 0:
            _defer(ctx, command, (probe.stderr or probe.stdout or "").strip())
            return
        executable = json.loads(probe.stdout)["executable_path"]
    except (OSError, ValueError, TypeError, KeyError) as error:
        _defer(ctx, command, str(error))
        return

    state = {"playwright_version": version, "executable_path": executable}
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(state) + "\n", encoding="utf-8")
    except OSError as error:
        _defer(ctx, command, f"could not write marker: {error}")
        return
    ctx.log("html-pdf: chromium installed")
