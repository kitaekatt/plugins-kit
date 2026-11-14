"""clean-code-kit's custom bootstrap script: the parser cache pre-fetch.

Contracts:

- an already-warm cache is an ``ok`` entry (verbose-only) and spawns NO
  subprocess, so a healthy pass stays silent and cheap;
- a cold cache is fetched by running ``parsers.py`` under the PROVISIONED venv
  interpreter into the cache the runner reads, and the fetch is an action entry;
- a failed fetch is an action entry plus a deferred requirement, never silence;
- the script and the runner name the same cache directory (one source).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

from cck_support import PLUGIN_ROOT

from clean_code_kit import environment, parsers

_SCRIPT = PLUGIN_ROOT / "custom_bootstrap.py"
VERSION = "1.25.0"


def _load_module():
    spec = importlib.util.spec_from_file_location("cck_custom_bootstrap", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


custom_bootstrap = _load_module()


class _RecordingCtx:
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
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _make_venv(data_dir: Path, version: str | None = VERSION) -> Path:
    python = data_dir / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    if version is not None:
        site = data_dir / ".venv" / "lib" / "python3.12" / "site-packages"
        (site / f"tree_sitter_language_pack-{version}.dist-info").mkdir(parents=True)
    return python


def _warm(cache: Path, version: str = VERSION) -> None:
    directory = parsers.version_dir(cache, version)
    (directory / "bundles").mkdir(parents=True)
    (directory / "manifest.json").write_text("{}")
    (directory / "bundles" / "macos-arm64-0000.tar.zst").write_bytes(b"bundle")


def _patch_run(monkeypatch, *, returncode=0, stderr="", fill=True):
    """Fake the fetch subprocess; on success it fills the cache it was given."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if returncode == 0 and fill:
            _warm(Path(argv[-1]))
        return _Completed(returncode, "", stderr)

    monkeypatch.setattr(custom_bootstrap.subprocess, "run", run)
    return calls


def test_warm_cache_is_an_ok_entry_and_spawns_nothing(tmp_path, monkeypatch):
    _make_venv(tmp_path)
    _warm(parsers.cache_dir(tmp_path))
    calls = _patch_run(monkeypatch)
    ctx = _RecordingCtx(tmp_path)
    custom_bootstrap.bootstrap(ctx)
    assert calls == []
    assert ctx.actions == [] and ctx.deferred == []
    assert ctx.oks == [f"code-audit: parsers already cached (tree-sitter-language-pack {VERSION})"]


def test_cold_cache_is_fetched_under_the_venv_interpreter(tmp_path, monkeypatch):
    python = _make_venv(tmp_path)
    calls = _patch_run(monkeypatch)
    ctx = _RecordingCtx(tmp_path)
    custom_bootstrap.bootstrap(ctx)
    cache = parsers.cache_dir(tmp_path)
    assert calls == [[str(python), "-I", str(PLUGIN_ROOT / "scripts" / "clean_code_kit" / "parsers.py"),
                      str(cache)]]
    assert ctx.actions == [f"code-audit: parsers fetched (tree-sitter-language-pack {VERSION})"]
    assert ctx.oks == [] and ctx.deferred == []
    assert parsers.is_warm(cache, VERSION)


def test_a_cache_warm_for_another_pack_version_is_fetched_again(tmp_path, monkeypatch):
    _make_venv(tmp_path)
    _warm(parsers.cache_dir(tmp_path), "1.24.0")
    calls = _patch_run(monkeypatch)
    custom_bootstrap.bootstrap(_RecordingCtx(tmp_path))
    assert len(calls) == 1


def test_failed_fetch_is_logged_and_deferred(tmp_path, monkeypatch):
    _make_venv(tmp_path)
    _patch_run(monkeypatch, returncode=1, stderr=(
        "Traceback\ntree_sitter_language_pack.DownloadError: Download error: "
        "Connection refused (os error 61)\n"))
    ctx = _RecordingCtx(tmp_path)
    custom_bootstrap.bootstrap(ctx)
    assert len(ctx.actions) == 1
    assert ctx.actions[0].startswith("code-audit: parser fetch failed: ")
    assert "DownloadError" in ctx.actions[0]
    assert ctx.oks == [] and ctx.failures == []
    (req,) = ctx.deferred
    assert req["name"] == "tree_sitter_parsers"
    assert str(parsers.cache_dir(tmp_path)) in req["satisfied_by"]
    assert req["satisfied_by"] in req["agent_msg"]


def test_fetch_that_leaves_no_bundle_is_deferred(tmp_path, monkeypatch):
    _make_venv(tmp_path)
    _patch_run(monkeypatch, fill=False)
    ctx = _RecordingCtx(tmp_path)
    custom_bootstrap.bootstrap(ctx)
    assert "left no parser bundle" in ctx.actions[0]
    assert [r["name"] for r in ctx.deferred] == ["tree_sitter_parsers"]


def test_missing_venv_or_pack_is_deferred_without_a_subprocess(tmp_path, monkeypatch):
    calls = _patch_run(monkeypatch)
    ctx = _RecordingCtx(tmp_path)
    custom_bootstrap.bootstrap(ctx)
    assert "provisioned venv python is missing" in ctx.actions[0]
    other = tmp_path / "other"
    _make_venv(other, version=None)
    ctx = _RecordingCtx(other)
    custom_bootstrap.bootstrap(ctx)
    assert "tree-sitter-language-pack is not installed" in ctx.actions[0]
    assert calls == []


def test_script_and_runner_share_the_cache_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path))
    data_dir = tmp_path / "plugins-kit" / "clean-code-kit"
    assert data_dir == environment.plugin_data_dir()
    _make_venv(data_dir)
    calls = _patch_run(monkeypatch)
    custom_bootstrap.bootstrap(_RecordingCtx(data_dir))
    assert Path(calls[0][-1]) == environment.parser_cache_dir()


def test_parsers_program_downloads_the_grammars_into_the_named_cache(tmp_path, monkeypatch):
    seen = {}
    fake = types.ModuleType("tree_sitter_language_pack")
    fake.download = lambda names: seen.update(names=names, cache=os.environ[parsers.CACHE_ENV])
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", fake)
    monkeypatch.delenv(parsers.CACHE_ENV, raising=False)
    assert parsers._main([str(tmp_path / "c")]) == 0
    assert seen == {"names": list(parsers.GRAMMARS), "cache": str(tmp_path / "c")}
    assert parsers._main([]) == 2
