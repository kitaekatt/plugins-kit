"""The ``llm-kit`` CLI by itself: routing, the verb map, composition and pinned messages.

Output parity with the legacy CLIs is in ``test_cli_parity.py``; the launchers are in
``test_launchers.py``. This file covers what has no legacy twin:

* ``TestVerbMap``: every verb of the three legacy CLIs has a home group, and the
  deliberate renames (``models`` -> ``models list``, ``acceptance`` under ``local``)
  are the only departures from the legacy spelling;
* ``TestRouting`` / ``TestUsageTokens``: the group router, its help and its
  migration hint, and the program names usage text carries;
* ``TestComposition``: the CLI is the composition root -- it injects the configured
  backend factory into the jobs runner and the registry-resolved credential into
  the account adapter, and it moves into the plugin venv only on the console path;
* ``TestPipelineMount``: the thin ``pipeline`` mount;
* ``TestRuntimeMessages``: the three runtime messages that name the plugin to
  install or update now name llm-kit.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import types
from typing import Any

import pytest

from test_cli_harness import (  # noqa: F401 -- fixtures
    REPO,
    final_mods,
    home,
    legacy_mods,
    run,
)

LIB = REPO / "plugins" / "llm-kit" / "lib"


def _choices(parser: Any) -> set[str]:
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            return set(action.choices)
    raise AssertionError("parser has no subcommands")


class TestVerbMap:
    def test_every_scripting_verb_has_exactly_one_home(self, legacy_mods, final_mods):
        legacy = _choices(legacy_mods.cli._parser())
        job_kit_verbs = {"run", "resume", "events", "gc"}  # job-kit's, not llm-scripting-kit's
        mapped = (set(final_mods.cli.LEGACY_VERBS) - job_kit_verbs) | {"frontdoor"}
        assert legacy == mapped, (legacy ^ mapped)

    def test_the_legacy_to_new_map_is_the_documented_one(self, final_mods):
        assert final_mods.cli.LEGACY_VERBS == {
            "status": "account status", "which": "account which", "set-key": "account set-key",
            "complete": "completion complete", "request-schema": "completion request-schema",
            "repair-json": "completion repair-json",
            "endpoints": "models endpoints", "probe": "models probe", "usage": "models usage",
            "record-halt": "models record-halt", "describe": "models describe", "seats": "models seats",
            "models": "models list", "resolve": "models resolve",
            "swapper": "local swapper", "acceptance": "local acceptance",
            "run": "jobs run", "resume": "jobs resume", "events": "jobs events", "gc": "jobs gc",
        }

    def test_every_mapped_path_parses_its_help(self, final_mods, capsys):
        for target in final_mods.cli.LEGACY_VERBS.values():
            code = run(final_mods.cli.main, [*target.split(), "--help"], capsys).code
            assert code == 0, target

    def test_the_jobs_group_carries_the_job_kit_verbs_and_cancel(self, legacy_mods, final_mods, capsys):
        legacy = _choices(legacy_mods.jobs_cli._parser())
        assert legacy == {"run", "status", "resume", "resolve", "events", "gc"}
        for verb in legacy:
            assert run(final_mods.cli.main, ["jobs", verb, "--help"], capsys).code == 0, verb
        # Deliberate difference (ledger schema 13): llm-kit adds `cancel`; the
        # legacy verbs are all still there.
        assert _choices(final_mods.jobs_cli._parser()) == legacy | {"cancel"}
        assert run(final_mods.cli.main, ["jobs", "cancel", "--help"], capsys).code == 0

    def test_the_pipeline_group_carries_the_store_only_command_set(self, legacy_mods, final_mods, tmp_path):
        store = legacy_mods.cpk_store.ExecutionStore(tmp_path / "legacy.db")
        legacy = set(legacy_mods.cpk_run.build_commands(store))
        final = set(final_mods.run.build_commands(final_mods.exec_store.ExecutionStore(tmp_path / "final.db")))
        assert legacy == final == set(final_mods.pipeline._COMMANDS)

    def test_swapper_and_acceptance_subverbs_are_unchanged(self, legacy_mods, final_mods):
        def nested(parser: Any, name: str) -> set[str]:
            for action in parser._actions:
                if action.__class__.__name__ == "_SubParsersAction":
                    return _choices(action.choices[name])
            raise AssertionError(name)

        legacy = legacy_mods.cli._parser()
        final = final_mods.local._parser()
        assert nested(final, "swapper") == nested(legacy, "swapper")
        assert nested(final, "acceptance") == nested(legacy, "acceptance")
        assert nested(final, "swapper") == {"running", "terminate", "unload", "strays", "terminate-listener",
                                            "guard-launch"}
        assert nested(final, "acceptance") == {"swapper", "frontdoor"}

    def test_the_groups_are_the_eight_in_the_design(self, final_mods):
        assert list(final_mods.cli.GROUPS) == [
            "account", "completion", "models", "jobs", "pipeline", "frontdoor", "local", "consent",
        ]


class TestRouting:
    def test_no_arguments_and_an_unknown_group_are_usage_errors(self, final_mods, capsys):
        assert run(final_mods.cli.main, [], capsys).code == 2
        out = run(final_mods.cli.main, ["nonesuch"], capsys)
        assert out.code == 2 and "invalid choice: 'nonesuch'" in out.err

    def test_top_level_help_lists_every_group_and_the_verb_map(self, final_mods, capsys):
        out = run(final_mods.cli.main, ["--help"], capsys)
        assert out.code == 0
        for group in final_mods.cli.GROUPS:
            assert group in out.out
        assert "legacy command -> llm-kit path:" in out.out and "llm-kit models list" in out.out

    @pytest.mark.parametrize("verb,expected", [
        ("set-key", "'set-key' is now 'llm-kit account set-key'"),
        ("swapper", "'swapper' is now 'llm-kit local swapper'"),
        ("gc", "'gc' is now 'llm-kit jobs gc'"),
    ])
    def test_a_bare_legacy_verb_gets_a_migration_hint(self, final_mods, capsys, verb, expected):
        out = run(final_mods.cli.main, [verb], capsys)
        assert out.code == 2 and out.out == "" and expected in out.err

    def test_a_verb_two_legacy_commands_share_names_every_home(self, final_mods, capsys):
        out = run(final_mods.cli.main, ["status"], capsys)
        assert out.code == 2
        assert "llm-kit account status, llm-kit jobs status, llm-kit pipeline status" in out.err

    def test_frontdoor_hands_every_argument_to_the_front_door(self, final_mods, capsys, monkeypatch):
        seen = []
        monkeypatch.setattr(final_mods.frontdoor, "main", lambda argv: seen.append(argv) or 7)
        assert final_mods.cli.main(["frontdoor", "--check", "--port", "1", "x"]) == 7
        assert seen == [["--check", "--port", "1", "x"]]

    def test_a_group_name_is_never_taken_for_a_verb(self, final_mods, capsys):
        out = run(final_mods.cli.main, ["models"], capsys)
        assert out.code == 2 and "required: cmd" in out.err


class TestUsageTokens:
    def test_group_parsers_name_llm_kit_and_the_group(self, final_mods, capsys):
        for group in ("account", "completion", "models", "jobs", "pipeline", "local", "consent"):
            out = run(final_mods.cli.main, [group, "--help"], capsys)
            assert out.code == 0 and f"usage: llm-kit {group}" in out.out, group

    def test_jobs_version_names_llm_kit_jobs(self, final_mods, capsys):
        out = run(final_mods.cli.main, ["jobs", "--version"], capsys)
        assert out.code == 0 and out.out == f"llm-kit jobs {final_mods.jobs_cli.__version__}\n"

    def test_the_resolve_description_names_the_new_resume_command(self, final_mods, capsys):
        out = run(final_mods.cli.main, ["jobs", "resolve", "--help"], capsys)
        assert "llm-kit jobs resume <run-id>" in " ".join(out.out.split())

    def test_job_errors_keep_the_legacy_prefix(self, final_mods, home, tmp_path, capsys):
        out = run(final_mods.cli.main, ["jobs", "status", "r", "--store", str(tmp_path / "absent.db")], capsys)
        assert out.code == 3 and out.err.startswith("job-kit: ")


class TestComposition:
    def test_jobs_run_and_resume_receive_the_configured_factory(self, final_mods, home, tmp_path, capsys,
                                                                monkeypatch):
        from llm_kit.dispatch.pipeline_backends import ConfiguredBackendFactory

        seen = {}
        snapshot = types.SimpleNamespace(jobs=[], to_mapping=lambda: {"run": {"id": "r"}, "jobs": []})

        def fake_run(path: Any, **kw: Any) -> Any:
            seen["run"] = kw["backend_factory"]
            return snapshot

        def fake_resume(run_id: str, store: Any, **kw: Any) -> Any:
            seen["resume"] = kw["backend_factory"]
            return snapshot

        monkeypatch.setattr(final_mods.jobs_cli, "run_job_file", fake_run)
        monkeypatch.setattr(final_mods.jobs_cli, "resume_run", fake_resume)
        run(final_mods.cli.main, ["jobs", "run", str(tmp_path / "j.yaml"), "--store", str(tmp_path / "s.db"),
                                  "--run-id", "r"], capsys)
        run(final_mods.cli.main, ["jobs", "resume", "r", "--store", str(tmp_path / "s.db")], capsys)
        assert isinstance(seen["run"], ConfiguredBackendFactory)
        assert isinstance(seen["resume"], ConfiguredBackendFactory)

    def test_account_status_injects_the_registry_resolved_key_into_the_adapter(self, final_mods, home, tmp_path,
                                                                                capsys, monkeypatch):
        env = tmp_path / "user.env"
        env.write_text("OPENROUTER_API_KEY=from-user-file\n", encoding="utf-8")
        monkeypatch.setattr(final_mods.api_key, "USER_ENV_FILE", env)
        seen = {}

        def validate(endpoint: Any, key: str) -> None:
            seen["key"] = key
            seen["endpoint"] = endpoint["name"]

        monkeypatch.setattr(final_mods.account, "validate_endpoint", validate)
        out = run(final_mods.cli.main, ["account", "status"], capsys)
        assert out.code == 0 and "Source: user" in out.out
        assert seen == {"key": "from-user-file", "endpoint": "openrouter"}

    def test_only_the_console_path_moves_into_the_plugin_venv(self, final_mods, monkeypatch, capsys):
        calls = []

        def guard(plugin: str, *a: Any, **k: Any) -> None:
            calls.append((plugin, list(sys.argv)))

        monkeypatch.setattr(final_mods.cli, "reexec_under_plugin_venv", guard)
        monkeypatch.setattr(sys, "argv", ["llm-kit-wrapper", "models", "--help"])
        out = run(lambda argv: final_mods.cli.main(), [], capsys)
        assert out.code == 0
        assert calls == [("llm-kit", ["-m", "llm_kit.cli", "models", "--help"])]
        assert sys.argv == ["llm-kit-wrapper", "models", "--help"]
        calls.clear()
        run(final_mods.cli.main, ["models", "--help"], capsys)
        assert calls == []

    def test_the_backend_seam_is_late_bound(self, final_mods, monkeypatch):
        seen = []
        monkeypatch.setattr(final_mods.factory, "create_backend", lambda endpoint=None, **kw: seen.append((endpoint, kw)))
        final_mods.compose.create_backend("e", model="m")
        assert seen == [("e", {"model": "m"})]

    def test_the_real_factory_path_builds_the_openrouter_backend_over_a_fake_client(self, final_mods, home,
                                                                                    tmp_path, capsys, monkeypatch):
        """No patched seam: dispatch.factory -> OpenRouterBackend -> a recording fake client."""
        calls = []

        class _Resp:
            class _Choice:
                class message:  # noqa: N801
                    content = "wire answer"
                    reasoning_content = None

                finish_reason = "stop"

            choices = [_Choice()]
            usage = None

        class _Client:
            class chat:  # noqa: N801
                class completions:  # noqa: N801
                    @staticmethod
                    def create(**kw: Any) -> Any:
                        calls.append(kw)
                        return _Resp()

        monkeypatch.setattr(final_mods.client, "make_openai_client", lambda **_: _Client())
        out = run(final_mods.cli.main, ["completion", "complete", "--prompt", "hi", "--format", "text"], capsys,
                  monkeypatch=monkeypatch)
        assert out.code == 0 and out.out == "wire answer\n"
        assert len(calls) == 1 and calls[0]["messages"][-1] == {"role": "user", "content": "hi"}


class TestPipelineMount:
    def test_store_is_required_and_a_typo_creates_no_ledger(self, final_mods, home, tmp_path, capsys):
        out = run(final_mods.cli.main, ["pipeline", "claim", "r", "u", "w"], capsys)
        assert out.code == 2 and "--store" in out.err
        ledger = tmp_path / "typo.db"
        out = run(final_mods.cli.main, ["pipeline", "--store", str(ledger), "clam"], capsys)
        assert out.code == 2 and "Did you mean: claim" in out.err
        assert not ledger.exists()
        assert run(final_mods.cli.main, ["pipeline", "--store", str(ledger)], capsys).code == 2
        assert not ledger.exists()

    def test_a_real_command_creates_the_ledger_and_renders_yaml(self, final_mods, home, tmp_path, capsys):
        ledger = tmp_path / "sub" / "run.db"
        out = run(final_mods.cli.main, ["pipeline", "--store", str(ledger), "create-run", "r", "inline", "mock",
                                        "m", "v1"], capsys)
        assert out.code == 0 and "id: r" in out.out and ledger.exists()

    def test_protocol_is_not_mounted_without_an_adapter(self, final_mods, home, tmp_path, capsys):
        out = run(final_mods.cli.main, ["pipeline", "--store", str(tmp_path / "x.db"), "protocol"], capsys)
        assert out.code == 2 and "unknown command 'protocol'" in out.err


class TestRuntimeMessages:
    """Each message that names the plugin to install or update names llm-kit."""

    def test_missing_openai_names_the_llm_kit_environment(self, final_mods, home, monkeypatch):
        monkeypatch.setitem(sys.modules, "openai", None)
        with pytest.raises(ImportError) as info:
            final_mods.client.make_openai_client(api_key="k")
        text = str(info.value)
        assert "bootstrap-provisioned llm-kit CLI environment" in text
        assert "llm-scripting-kit" not in text

    def test_claudx_unavailable_names_llm_kit(self):
        from llm_kit.adapters import backends

        err = backends._claudx_unavailable(ModuleNotFoundError("x", name="bootstrap_lib"), "nested_claude_args")
        assert str(err).startswith("llm-kit cannot start a `claude -p` child under claudx")
        assert "llm-scripting-kit" not in str(err)

    def test_missing_psutil_names_llm_kit(self, monkeypatch):
        from llm_kit.adapters import _swapper_process

        monkeypatch.setitem(sys.modules, "psutil", None)
        with pytest.raises(_swapper_process.InspectionIndeterminate) as info:
            _swapper_process.PsutilInspector()
        assert "declared in llm-kit's pyproject.toml" in str(info.value)
        assert "llm-scripting-kit" not in str(info.value)


class TestModuleEntry:
    def test_python_dash_m_runs_the_cli_in_a_scratch_home(self, tmp_path):
        """``python -m llm_kit.cli`` (the module form the re-exec uses) in a child process."""
        scratch = tmp_path / "home"
        scratch.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in (
            "CLAUDE_BOOTSTRAP_DATA_ROOT", "CLAUDE_PLUGIN_TEST", "MODEL_ENDPOINTS_REGISTRY", "OPENROUTER_API_KEY")}
        env.update({
            "HOME": str(scratch), "USERPROFILE": str(scratch), "_BOOTSTRAP_GUARD_SKIP_REEXEC": "1",
            "PYTHONPATH": os.pathsep.join([str(LIB), str(REPO / "plugins" / "bootstrap")]),
        })
        done = subprocess.run([sys.executable, "-m", "llm_kit.cli", "models", "endpoints"], env=env,
                              capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        assert "openrouter" in json.loads(done.stdout)["endpoints"]
        hint = subprocess.run([sys.executable, "-m", "llm_kit.cli", "set-key"], env=env,
                              capture_output=True, text=True, timeout=120)
        assert hint.returncode == 2 and "'set-key' is now 'llm-kit account set-key'" in hint.stderr
