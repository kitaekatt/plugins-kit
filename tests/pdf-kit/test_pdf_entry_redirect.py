"""html_to_pdf.py honours a redirected bootstrap data root (a claudx session).

The script calls bootstrap_guard.reexec_under_plugin_venv("pdf-kit") at module
top. With CLAUDE_BOOTSTRAP_DATA_ROOT set, the guard must re-exec into the
REDIRECTED venv, and must exit 2 when that venv is missing rather than run
against the production one. Both behaviours are observed in a real subprocess,
because the guard's effect is a process-level re-exec or exit.
"""

import os
import subprocess
import sys
import venv
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "pdf-kit" / "skills" / "html-pdf" / "scripts" / "html_to_pdf.py"
)

_MARKER = "PDFKIT_REDIRECTED_VENV_MARKER"


def _child_env(tmp_path: Path, data_root: Path) -> dict:
    """A scrubbed environment: HOME redirected, redirection on, opt-outs removed."""
    env = dict(os.environ)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["CLAUDE_BOOTSTRAP_DATA_ROOT"] = str(data_root)
    # The test conftest sets the skip variable; a real re-exec needs it absent.
    env.pop("_BOOTSTRAP_GUARD_SKIP_REEXEC", None)
    env.pop("_BOOTSTRAP_GUARD_VENV_REEXEC", None)
    return env


def _run_help(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(_SCRIPT), "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_missing_redirected_venv_exits_2(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()

    result = _run_help(_child_env(tmp_path, data_root))

    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "redirected plugin interpreter is missing" in result.stderr
    assert str(data_root) in result.stderr
    assert "usage:" not in result.stdout


def test_redirected_venv_is_the_interpreter_that_runs(tmp_path):
    data_root = tmp_path / "data"
    venv_dir = data_root / "plugins-kit" / "pdf-kit" / ".venv"
    # Symlinked on POSIX, as uv and bootstrap build it: EnvBuilder's default copies
    # the interpreter binary, and a copied standalone python (macOS, the
    # @executable_path/../lib libpython rpath) cannot find its library from bin/.
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(str(venv_dir))
    site_packages = [p for p in venv_dir.rglob("site-packages") if p.is_dir()]
    assert site_packages, f"no site-packages under {venv_dir}"
    # A .pth import line runs at interpreter start: the marker appears only
    # when the redirected venv's interpreter is the one running the script.
    (site_packages[0] / "marker.pth").write_text(
        f"import sys; sys.stderr.write('{_MARKER}\\n')\n", encoding="utf-8"
    )

    result = _run_help(_child_env(tmp_path, data_root))

    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert "usage:" in result.stdout.lower()
    assert _MARKER in result.stderr
