"""Tests for pdf-kit's custom bootstrap script.

Three contracts, each of which has been broken in the past:

- the steady-state "chromium already installed (cached)" branch routes to
  ``log_ok`` (verbose-only) and spawns NO subprocess -- a healthy bootstrap
  stays silent and cheap, and it must not start the Playwright driver on
  every session just to ask which version it is;
- the install runs under the PROVISIONED venv's python (``<data_dir>/.venv``),
  never ``uv run --project <plugin root>``, which built a second venv inside
  the plugin cache, one per version;
- the marker is keyed on state (installed playwright version + the recorded
  Chromium executable still existing), so a playwright upgrade or a cleared
  browser cache re-installs instead of being masked forever, and a failed
  install becomes a deferred requirement instead of silent success.
"""

import importlib.util
import json
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "pdf-kit" / "custom_bootstrap.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("pdf_custom_bootstrap", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


custom_bootstrap = _load_module()


class _RecordingCtx:
    """Captures action (log) vs verbose-only (log_ok) routing and deferrals."""

    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.project_dir = None
        self.actions = []
        self.oks = []
        self.failures = []
        self.deferred = []

    def log(self, msg):
        self.actions.append(msg)

    def log_ok(self, msg):
        self.oks.append(msg)

    def add_failure(self, failure_type, **kwargs):
        self.failures.append({"type": failure_type, **kwargs})

    def add_deferred_requirement(self, name, **kwargs):
        self.deferred.append({"name": name, **kwargs})


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


def _make_venv(tmp_path, playwright_version="1.0"):
    """A fake provisioned venv: python binary plus playwright's dist-info."""
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    if playwright_version is not None:
        site = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
        (site / f"playwright-{playwright_version}.dist-info").mkdir(parents=True)
    return python


def _write_marker(tmp_path, version, executable):
    marker = tmp_path / custom_bootstrap.MARKER_NAME
    marker.write_text(json.dumps({
        "playwright_version": version, "executable_path": str(executable),
    }), encoding="utf-8")
    return marker


def _patch_runner(monkeypatch, *, executable, install_returncode=0, stderr=""):
    """Record every subprocess; the install returns install_returncode, the
    executable probe returns the given path."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        result = _Completed()
        if argv[1:] == ["-m", "playwright", "install", "chromium"]:
            result.returncode = install_returncode
            result.stderr = stderr
        else:
            result.stdout = json.dumps({"executable_path": str(executable)})
        return result

    monkeypatch.setattr(custom_bootstrap.subprocess, "run", run)
    return calls


def _install_argv(python):
    return [str(python), "-m", "playwright", "install", "chromium"]


class TestChromiumCachedRouting:
    def test_cached_marker_routes_to_verbose_only(self, tmp_path, monkeypatch):
        _make_venv(tmp_path)
        executable = tmp_path / "chrome"
        executable.touch()
        _write_marker(tmp_path, "1.0", executable)
        _patch_runner(monkeypatch, executable=executable)

        ctx = _RecordingCtx(tmp_path)
        custom_bootstrap.bootstrap(ctx)

        assert ctx.actions == [], "cached steady state must not produce an action entry"
        assert any("already installed (cached)" in m for m in ctx.oks)

    def test_consistent_marker_spawns_no_subprocess(self, tmp_path, monkeypatch):
        _make_venv(tmp_path)
        executable = tmp_path / "chrome"
        executable.touch()
        _write_marker(tmp_path, "1.0", executable)
        calls = _patch_runner(monkeypatch, executable=executable)

        custom_bootstrap.bootstrap(_RecordingCtx(tmp_path))

        assert calls == []


class TestChromiumInstall:
    def test_uses_provisioned_venv_and_does_not_write_plugin_root(self, tmp_path, monkeypatch):
        python = _make_venv(tmp_path)
        calls = _patch_runner(monkeypatch, executable=tmp_path / "chrome")

        custom_bootstrap.bootstrap(_RecordingCtx(tmp_path))

        assert _install_argv(python) in calls
        assert all(call[0] == str(python) for call in calls)
        assert not any("uv" in call[0] for call in calls)
        assert not any(_SCRIPT.parent.rglob("chromium.installed"))

    def test_fresh_install_writes_state_marker(self, tmp_path, monkeypatch):
        python = _make_venv(tmp_path, "1.2")
        executable = tmp_path / "chrome"
        calls = _patch_runner(monkeypatch, executable=executable)
        ctx = _RecordingCtx(tmp_path)

        custom_bootstrap.bootstrap(ctx)

        # install first, then ONE probe for the executable path
        assert calls[0] == _install_argv(python)
        assert len(calls) == 2 and calls[1][:2] == [str(python), "-c"]
        recorded = json.loads((tmp_path / custom_bootstrap.MARKER_NAME).read_text())
        assert recorded == {"playwright_version": "1.2", "executable_path": str(executable)}
        assert any("chromium installed" in m for m in ctx.actions)

    def test_missing_executable_reinstalls(self, tmp_path, monkeypatch):
        python = _make_venv(tmp_path)
        _write_marker(tmp_path, "1.0", tmp_path / "gone")
        calls = _patch_runner(monkeypatch, executable=tmp_path / "gone")

        custom_bootstrap.bootstrap(_RecordingCtx(tmp_path))

        assert calls[0] == _install_argv(python)

    def test_version_mismatch_reinstalls(self, tmp_path, monkeypatch):
        python = _make_venv(tmp_path, "new")
        executable = tmp_path / "chrome"
        executable.touch()
        _write_marker(tmp_path, "old", executable)
        calls = _patch_runner(monkeypatch, executable=executable)

        custom_bootstrap.bootstrap(_RecordingCtx(tmp_path))

        assert calls[0] == _install_argv(python)
        recorded = json.loads((tmp_path / custom_bootstrap.MARKER_NAME).read_text())
        assert recorded["playwright_version"] == "new"

    def test_install_failure_defers_and_does_not_write_marker(self, tmp_path, monkeypatch):
        _make_venv(tmp_path)
        calls = _patch_runner(
            monkeypatch, executable=tmp_path / "chrome",
            install_returncode=1, stderr="diagnostic " * 300,
        )
        ctx = _RecordingCtx(tmp_path)

        custom_bootstrap.bootstrap(ctx)

        assert calls
        assert any("diagnostic" in message for message in ctx.actions)
        assert ctx.deferred[0]["name"] == "chromium"
        assert ctx.deferred[0]["satisfied_by"] == (
            f"{tmp_path / '.venv' / 'bin' / 'python'} -m playwright install chromium"
        )
        assert not (tmp_path / custom_bootstrap.MARKER_NAME).exists()

    def test_missing_venv_python_defers_without_subprocess(self, tmp_path, monkeypatch):
        calls = _patch_runner(monkeypatch, executable=tmp_path / "chrome")
        ctx = _RecordingCtx(tmp_path)

        custom_bootstrap.bootstrap(ctx)

        assert calls == []
        assert ctx.deferred and ctx.deferred[0]["name"] == "chromium"

    def test_playwright_not_in_venv_defers_without_subprocess(self, tmp_path, monkeypatch):
        _make_venv(tmp_path, playwright_version=None)
        calls = _patch_runner(monkeypatch, executable=tmp_path / "chrome")
        ctx = _RecordingCtx(tmp_path)

        custom_bootstrap.bootstrap(ctx)

        assert calls == []
        assert ctx.deferred and ctx.deferred[0]["name"] == "chromium"


def test_custom_bootstrap_has_no_main_block():
    assert "__main__" not in _SCRIPT.read_text(encoding="utf-8")
