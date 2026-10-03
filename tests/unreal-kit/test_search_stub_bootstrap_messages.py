"""search_unreal_stub.py tells an absent bootstrap_lib from an older one.

plugins/CLAUDE.md "Optional use of another plugin": the absent and too-old
messages must differ, and the probe targets the newest symbol used
(``bootstrap_lib.interpreter_env.PLUGIN_CALL_SITE_EXPR``, bootstrap 0.120.0).
Each case runs the real script in a subprocess whose ``bootstrap_lib`` is
controlled through PYTHONPATH: none, a copy with ``interpreter_env.py``
removed, and a copy whose ``interpreter_env`` lacks the symbol. Each subprocess
uses a real isolated plugin venv under the redirected data root, without the
suite's re-exec guard. The in-process exception probe keeps that guard to avoid
replacing pytest, but also provisions the required target interpreter.
"""

from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import venv
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "plugins" / "unreal-kit" / "scripts" / "search_unreal_stub.py"
BOOTSTRAP_LIB = REPO_ROOT / "plugins" / "bootstrap" / "bootstrap_lib"

ABSENT = "has not provisioned unreal-kit's Unreal API search (missing: bootstrap_lib)"
TOO_OLD = (
    "[unreal-kit] the installed 'plugins-kit:bootstrap' plugin is too old for "
    "unreal-kit's Unreal API search (requires bootstrap >= 0.120.0, which ships "
    "bootstrap_lib.interpreter_env.PLUGIN_CALL_SITE_EXPR). Run "
    "`claude plugin update bootstrap@plugins-kit`, then start a new session "
    "and retry."
)


def _provision_runtime(tmp_path: Path) -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    plugin_data = data_root / "plugins-kit" / "unreal-kit"
    plugin_data.mkdir(parents=True, exist_ok=True)
    (plugin_data / "bootstrap.log").write_text("provisioned\n", encoding="utf-8")
    runtime = plugin_data / ".venv"
    # uv's macOS Python loads its dylib relative to the real executable.
    # Symlink on POSIX rather than copying that executable away from its lib.
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(runtime)
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return data_root, python


def _run(tmp_path: Path, pythonpath: str) -> subprocess.CompletedProcess[str]:
    data_root, python = _provision_runtime(tmp_path)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        k: v for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "_BOOTSTRAP_GUARD_VENV_REEXEC"}
    }
    env.update({
        "CLAUDE_BOOTSTRAP_DATA_ROOT": str(data_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(home),
        "USERPROFILE": str(home),
    })
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        [str(python), str(SCRIPT), "SomePattern", "--project-root", str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, env=env, timeout=120)


def _older_bootstrap(tmp_path: Path, shape: str) -> str:
    root = tmp_path / "older"
    shutil.copytree(BOOTSTRAP_LIB, root / "bootstrap_lib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    interp = root / "bootstrap_lib" / "interpreter_env.py"
    if shape == "module-missing":
        interp.unlink()
    else:
        interp.write_text('"""An interpreter_env without the call-site forms."""\n',
                          encoding="utf-8")
    return str(root)


def test_absent_bootstrap_lib_keeps_the_provisioning_message(tmp_path):
    run = _run(tmp_path, "")
    assert run.returncode == 3, run.stderr
    assert ABSENT in run.stderr
    assert "too old" not in run.stderr


@pytest.mark.parametrize("shape", ["module-missing", "symbol-missing"])
def test_older_bootstrap_lib_gets_the_too_old_message(tmp_path, shape):
    """Revert that turns this RED: move the interpreter_env import back into
    the first try block (the older copy is then reported as absent)."""
    run = _run(tmp_path, _older_bootstrap(tmp_path, shape))
    assert run.returncode == 3, run.stderr
    assert TOO_OLD in run.stderr
    assert "has not provisioned" not in run.stderr
    assert "bootstrap.json" not in run.stderr


def test_current_bootstrap_lib_reaches_the_search(tmp_path):
    """Both bootstrap probes pass and search reports the isolated missing stub."""
    run = _run(tmp_path, str(BOOTSTRAP_LIB.parent))
    assert run.returncode == 2, run.stderr
    assert (
        "Unreal API search is unavailable: neither the consuming project's "
        "enriched stub nor the machine-local stock stub exists."
    ) in run.stderr
    assert "redirected plugin interpreter" not in run.stderr
    assert "too old" not in run.stderr and "has not provisioned" not in run.stderr


def test_real_stock_stub_search_returns_match(tmp_path):
    """The real search path must search a provisioned temporary stock stub."""
    home = tmp_path / "home"
    stock = home / ".claude" / "plugins" / "data" / "plugins-kit" / "unreal-kit" / "stubs" / "unreal.py"
    stock.parent.mkdir(parents=True)
    stock.write_text("class SomePattern:\n    pass\n", encoding="utf-8")
    run = _run(tmp_path, str(BOOTSTRAP_LIB.parent))
    assert run.returncode == 0, run.stderr
    assert f"{stock}:1:class SomePattern:" in run.stdout


def test_unrelated_post_import_exception_is_not_reported_as_search_success(tmp_path, monkeypatch):
    """A post-import failure must escape instead of becoming exit 0/1."""
    data_root, _python = _provision_runtime(tmp_path)
    monkeypatch.setenv("_BOOTSTRAP_GUARD_VENV_REEXEC", "1")
    monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(data_root))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    module = runpy.run_path(str(SCRIPT), run_name="search_unreal_stub_test")

    def explode(*_args, **_kwargs):
        raise RuntimeError("unrelated post-import failure")

    module["main"].__globals__["select_search_stub"] = explode
    with pytest.raises(RuntimeError, match="unrelated post-import failure"):
        module["main"](["SomePattern", "--project-root", str(tmp_path)])
