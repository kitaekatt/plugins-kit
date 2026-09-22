"""Outcome logging for the unreal-kit custom bootstrap consumer."""

import importlib.util
from pathlib import Path

import pytest


_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "unreal-kit"
    / "custom_bootstrap.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("unreal_custom_bootstrap_i13", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self, *, uproject=None, project_dir=None, defer=True):
        self.config = {} if uproject is None else {"uproject": str(uproject)}
        self.project_dir = None if project_dir is None else str(project_dir)
        self.outcomes = []
        self.deferred = []
        if defer:
            self.add_deferred_requirement = self._defer
        else:
            self.add_deferred_requirement = None

    def _defer(self, name, **kwargs):
        self.deferred.append((name, kwargs))

    def log(self, message):
        self.outcomes.append(("log", message))

    def log_ok(self, message):
        self.outcomes.append(("ok", message))


def _stub_paths(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    uproject = project_dir / "Game.uproject"
    uproject.write_text("{}", encoding="ascii")
    generated = project_dir / "Intermediate" / "PythonStub" / "unreal.py"
    generated.parent.mkdir(parents=True)
    durable = project_dir / ".plugin-data" / "plugins-kit" / "unreal-kit" / "unreal.py"
    durable.parent.mkdir(parents=True)
    return project_dir, uproject, generated, durable


def _assert_one_outcome(ctx, *, p4_checked=False):
    if p4_checked:
        assert ("ok", "redirectors: skipped - no Perforce workspace marker") in ctx.outcomes
        outcomes = [
            message for kind, message in ctx.outcomes
            if not message.startswith("redirectors:")
        ]
    else:
        outcomes = [message for _kind, message in ctx.outcomes]
    assert len(outcomes) == 1
    return outcomes[0]


def test_missing_uproject_logs_skipped_outcome(tmp_path):
    module = _load_module()
    ctx = FakeContext(project_dir=tmp_path)

    module.bootstrap(ctx)

    message = _assert_one_outcome(ctx)
    assert "skipped" in message
    assert "uproject" in message
    assert ctx.deferred == []


def test_missing_project_dir_logs_skipped_outcome(tmp_path):
    module = _load_module()
    uproject = tmp_path / "Game.uproject"
    uproject.write_text("{}", encoding="ascii")
    ctx = FakeContext(uproject=uproject, project_dir=None)

    module.bootstrap(ctx)

    message = _assert_one_outcome(ctx)
    assert "skipped" in message
    assert "project" in message
    assert ctx.deferred == []


def test_present_current_durable_stub_logs_success(tmp_path):
    module = _load_module()
    project_dir, uproject, generated, durable = _stub_paths(tmp_path)
    generated.write_text("same", encoding="ascii")
    durable.write_text("same", encoding="ascii")
    ctx = FakeContext(uproject=uproject, project_dir=project_dir)

    module.bootstrap(ctx)

    message = _assert_one_outcome(ctx, p4_checked=True)
    assert "current" in message
    assert ctx.deferred == []


def test_present_durable_without_generated_source_logs_truthfully(tmp_path):
    module = _load_module()
    project_dir, uproject, _generated, durable = _stub_paths(tmp_path)
    durable.write_text("durable", encoding="ascii")
    ctx = FakeContext(uproject=uproject, project_dir=project_dir)

    module.bootstrap(ctx)

    message = _assert_one_outcome(ctx, p4_checked=True)
    assert "present" in message
    assert "unavailable" in message
    assert ctx.deferred == []


@pytest.mark.parametrize("state", ["missing", "stale"])
def test_missing_or_stale_durable_stub_defers_and_logs_once(tmp_path, state):
    module = _load_module()
    project_dir, uproject, generated, durable = _stub_paths(tmp_path)
    generated.write_text("new", encoding="ascii")
    if state == "stale":
        durable.write_text("old", encoding="ascii")
    ctx = FakeContext(uproject=uproject, project_dir=project_dir)

    module.bootstrap(ctx)

    message = _assert_one_outcome(ctx, p4_checked=True)
    assert "deferred" in message
    assert [name for name, _kwargs in ctx.deferred] == ["unreal_enriched_stub"]


def test_missing_defer_api_is_diagnosed_and_logs_once(tmp_path):
    module = _load_module()
    project_dir, uproject, generated, _durable = _stub_paths(tmp_path)
    generated.write_text("generated", encoding="ascii")
    ctx = FakeContext(uproject=uproject, project_dir=project_dir, defer=False)

    module.bootstrap(ctx)

    message = _assert_one_outcome(ctx, p4_checked=True)
    assert "defer" in message
    assert "unavailable" in message or "unsupported" in message
    assert ctx.deferred == []
