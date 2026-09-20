"""hue-kit's bootstrap check and `init`'s refusal of the live working dir.

Named for its subject rather than `test_custom_bootstrap.py`: pytest derives
a module name from the basename, and tests/llm-scripting-kit already ships
that name, so two same-named files collide in a full-suite run.


Pairing needs a physical button press on a bridge. A machine that cannot
reach one -- and a developer who never invokes hue-kit -- can never satisfy
it, so registering it as a failure prompts them at every session start
forever. Bootstrap's own rule sends a credential only SOME capability needs
to add_deferred_requirement, which produces no prompt and no fix-all entry;
the point-of-need code asks when the user has the context to decide.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE = (Path(__file__).resolve().parents[2]
           / "plugins" / "hue-kit" / "custom_bootstrap.py")


@pytest.fixture
def custom_bootstrap():
    spec = importlib.util.spec_from_file_location("hue_custom_bootstrap", _MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Ctx:
    """Records what the check asked bootstrap to do."""

    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.failures = []
        self.deferred = []
        self.visible_logs = []
        self.ok_logs = []

    def add_failure(self, name, **kw):
        self.failures.append((name, kw))

    def add_deferred_requirement(self, name, **kw):
        self.deferred.append((name, kw))

    def log(self, message):
        self.visible_logs.append(message)

    def log_ok(self, message):
        self.ok_logs.append(message)


class TestUnpairedMachineIsNotPromptedEverySession:
    def test_missing_key_defers_rather_than_failing(
            self, custom_bootstrap, tmp_path, monkeypatch):
        monkeypatch.delenv("HUE_APP_KEY", raising=False)
        monkeypatch.delenv("HUE_KEY_FILE", raising=False)
        ctx = _Ctx(tmp_path)

        custom_bootstrap.bootstrap(ctx)

        assert ctx.failures == [], (
            "an unpairable machine would be prompted at every session start"
        )
        assert len(ctx.deferred) == 1
        name, kw = ctx.deferred[0]
        assert name == "hue_bridge_pairing"
        assert kw["satisfied_by"] == "hue-kit pair"
        assert kw["user_msg"] and kw["agent_msg"]

    def test_missing_key_logs_verbose_only(
            self, custom_bootstrap, tmp_path, monkeypatch):
        # Every check logs its outcome, but an unmet deferred requirement is
        # not an action taken -- a visible line is a per-session nag.
        monkeypatch.delenv("HUE_APP_KEY", raising=False)
        monkeypatch.delenv("HUE_KEY_FILE", raising=False)
        ctx = _Ctx(tmp_path)

        custom_bootstrap.bootstrap(ctx)

        assert ctx.visible_logs == [], f"visible nag: {ctx.visible_logs}"
        assert len(ctx.ok_logs) == 1

    @pytest.mark.parametrize("var", ["HUE_APP_KEY", "HUE_KEY_FILE"])
    def test_env_key_short_circuits(
            self, custom_bootstrap, tmp_path, monkeypatch, var):
        monkeypatch.delenv("HUE_APP_KEY", raising=False)
        monkeypatch.delenv("HUE_KEY_FILE", raising=False)
        monkeypatch.setenv(var, "k")
        ctx = _Ctx(tmp_path)

        custom_bootstrap.bootstrap(ctx)

        assert ctx.failures == [] and ctx.deferred == []
        assert ctx.visible_logs == []

    def test_paired_key_file_short_circuits(
            self, custom_bootstrap, tmp_path, monkeypatch):
        monkeypatch.delenv("HUE_APP_KEY", raising=False)
        monkeypatch.delenv("HUE_KEY_FILE", raising=False)
        (tmp_path / custom_bootstrap.PAIRED_KEY_FILENAME).write_text("key\n")
        ctx = _Ctx(tmp_path)

        custom_bootstrap.bootstrap(ctx)

        assert ctx.failures == [] and ctx.deferred == []
        assert ctx.visible_logs == []


class TestInitRefusesTheLiveWorkingDirectory:
    """`hue-kit init` ships the AUTHOR's registry and design as a worked
    example. Written into the live working directory they make both YAML
    files exist, so `start` reads an established workdir, skips first run,
    and every verb then fails on zones no bridge has."""

    def test_bare_init_refuses_and_writes_nothing(self, hue_cli, tmp_path,
                                                  monkeypatch, capfd):
        from argparse import Namespace
        monkeypatch.setattr(hue_cli, "DEFAULT_WORKDIR", tmp_path)
        rc = hue_cli._cmd_init(Namespace(init_dir=None, dir=str(tmp_path),
                                         force=False))

        assert rc != 0
        assert list(tmp_path.iterdir()) == [], "wrote into the live workdir"
        err = capfd.readouterr().err
        assert "refusing" in err
        assert "hue-kit init" in err  # names the way forward

    def test_explicit_other_directory_still_works(self, hue_cli, tmp_path,
                                                  monkeypatch):
        from argparse import Namespace
        live = tmp_path / "live"
        live.mkdir()
        monkeypatch.setattr(hue_cli, "DEFAULT_WORKDIR", live)
        dest = tmp_path / "example"
        rc = hue_cli._cmd_init(Namespace(init_dir=str(dest), dir=str(live),
                                         force=False))

        assert rc == 0
        assert sorted(f.name for f in dest.iterdir()) == sorted(
            hue_cli.EXAMPLE_FILES)
