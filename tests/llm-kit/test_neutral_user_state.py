"""The per-user files (consent, endpoint registry, API keys) live under a neutral directory.

A person who installs the library with ``pip`` and runs no AI harness has no
``~/.claude``. The user files therefore resolve to ``$XDG_CONFIG_HOME/llm-kit/``
(fallback ``~/.config/llm-kit/``), and the files an earlier release kept under
``~/.claude`` stay readable as a lower-precedence LEGACY location. Reading
creates nothing; a write targets the neutral path or the explicit override.

Every test runs under a throwaway HOME (the ``home`` fixture) with the steering
variables scrubbed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import llm_kit.cli as cli
from llm_kit.registry import api_key, consent, constants, model_endpoints, user_paths
from test_cli_harness import home, run, write  # noqa: F401 -- home is a fixture

NEUTRAL = Path(".config") / "llm-kit"
LEGACY_CONSENT = Path(".claude/plugins/data/plugins-kit/llm-kit/consent.json")
LEGACY_REGISTRY = Path(".claude/config/model-endpoints.yaml")
LEGACY_ENV = Path(".claude/plugins/data/plugins-kit/llm-scripting-kit/.env")

_REGISTRY = "models:\n  box: {base_url: http://box.invalid/v1, model: bm}\n"


@pytest.fixture(autouse=True)
def _legacy_env_in_this_home(home, monkeypatch):  # noqa: F811
    """``USER_ENV_FILE`` is bound when the module is imported, so a test that switches HOME re-points it."""
    import llm_kit.cli.account as account_cli

    legacy = home / LEGACY_ENV
    for module in (constants, api_key, account_cli):
        monkeypatch.setattr(module, "USER_ENV_FILE", legacy)


def _consent_doc(subscription: bool, metered: bool) -> str:
    return json.dumps({
        "schema": 2, "local": True, "subscription": subscription, "metered": metered,
        "scopes": {}, "updated_at": "2026-10-23T00:00:00+00:00",
    })


def _states(loaded: "consent.Policy") -> tuple:
    return (loaded.classes["subscription"].allowed, loaded.classes["metered"].allowed)


# --- locations --------------------------------------------------------------------------


def test_the_neutral_directory_follows_xdg_config_home_when_absolute(home, monkeypatch, tmp_path):
    assert user_paths.neutral_dir() == home / NEUTRAL
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert user_paths.neutral_dir() == tmp_path / "xdg" / "llm-kit"
    # XDG: a relative or empty value is ignored
    for ignored in ("relative/dir", "", "   "):
        monkeypatch.setenv("XDG_CONFIG_HOME", ignored)
        assert user_paths.neutral_dir() == home / NEUTRAL


def test_paths_are_resolved_per_call_not_at_import(home, monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("HOME", str(other))
    monkeypatch.setenv("USERPROFILE", str(other))
    monkeypatch.setattr(api_key, "USER_ENV_FILE", other / LEGACY_ENV)
    assert consent.default_consent_path() == other / NEUTRAL / "consent.json"
    assert model_endpoints.default_registry_path() == other / NEUTRAL / "model-endpoints.yaml"
    assert api_key.user_key_file() == other / NEUTRAL / ".env"


# --- consent ------------------------------------------------------------------------------


def test_consent_is_written_to_the_neutral_file_and_never_to_claude(home, capsys):
    out = run(cli.main, ["consent", "set", "--metered", "on"], capsys)
    assert out.code == 0
    assert json.loads((home / NEUTRAL / "consent.json").read_text())["metered"] is True
    assert not (home / ".claude").exists()


def test_the_override_names_the_one_consent_file(home, monkeypatch, tmp_path, capsys):
    target = tmp_path / "elsewhere" / "c.json"
    monkeypatch.setenv("LLM_KIT_CONSENT_FILE", str(target))
    assert run(cli.main, ["consent", "set", "--metered", "on"], capsys).code == 0
    assert json.loads(target.read_text())["metered"] is True
    assert not (home / NEUTRAL).exists() and not (home / ".claude").exists()
    shown = json.loads(run(cli.main, ["consent", "show", "--json"], capsys).out)
    assert shown["path"] == shown["write_path"] == str(target) and shown["classes"]["metered"]["allowed"] is True
    # the override wins even over a neutral file that exists
    write(home / NEUTRAL / "consent.json", _consent_doc(True, False))
    assert consent.policy().classes["metered"].allowed is True and consent.policy().path == target


def test_a_legacy_consent_file_is_read_when_the_neutral_one_is_absent(home, capsys):
    write(home / LEGACY_CONSENT, _consent_doc(True, False))
    loaded = consent.policy()
    assert (_states(loaded), loaded.source) == ((True, False), "file")
    assert loaded.path == home / LEGACY_CONSENT
    shown = json.loads(run(cli.main, ["consent", "show", "--json"], capsys).out)
    assert shown["path"] == str(home / LEGACY_CONSENT)
    assert shown["write_path"] == str(home / NEUTRAL / "consent.json")  # never written back there
    assert not (home / NEUTRAL).exists()  # reading created nothing


def test_the_neutral_consent_file_wins_over_the_legacy_one(home):
    write(home / LEGACY_CONSENT, _consent_doc(True, True))
    write(home / NEUTRAL / "consent.json", _consent_doc(False, False))
    loaded = consent.policy()
    assert _states(loaded) == (False, False)
    assert loaded.path == home / NEUTRAL / "consent.json"


def test_an_unreadable_neutral_file_is_reported_and_never_replaced_by_the_legacy_one(home):
    write(home / LEGACY_CONSENT, _consent_doc(True, True))
    write(home / NEUTRAL / "consent.json", "{not json")
    with pytest.raises(consent.ConsentConfigError, match="not valid JSON"):
        consent.policy()


def test_the_first_set_on_a_legacy_machine_carries_its_values_to_the_neutral_file(home, capsys):
    legacy = write(home / LEGACY_CONSENT, _consent_doc(False, False))  # subscription off differs from the default
    before = legacy.read_text()
    assert run(cli.main, ["consent", "set", "--metered", "on"], capsys).code == 0
    stored = json.loads((home / NEUTRAL / "consent.json").read_text())
    assert (stored["subscription"], stored["metered"]) == (False, True)  # subscription off came from the legacy file
    assert legacy.read_text() == before  # the legacy file is left alone


# --- endpoint registry ----------------------------------------------------------------------


def test_the_neutral_registry_is_the_convention(home):
    write(home / NEUTRAL / "model-endpoints.yaml", _REGISTRY)
    registry = model_endpoints.load_endpoint_registry()
    assert list(registry.entries) == ["box"] and registry.path == home / NEUTRAL / "model-endpoints.yaml"


def test_a_legacy_registry_is_read_only_when_the_neutral_one_is_absent(home):
    write(home / LEGACY_REGISTRY, _REGISTRY)
    registry = model_endpoints.load_endpoint_registry()
    assert list(registry.entries) == ["box"] and registry.path == home / LEGACY_REGISTRY
    write(home / NEUTRAL / "model-endpoints.yaml", _REGISTRY.replace("box", "newer"))
    assert list(model_endpoints.load_endpoint_registry().entries) == ["newer"]


def test_no_registry_anywhere_is_an_empty_registry_and_creates_nothing(home):
    assert list(model_endpoints.load_endpoint_registry().entries) == []
    assert not (home / ".claude").exists() and not (home / ".config").exists()


def test_a_dangling_registry_override_is_still_loud(home, monkeypatch, tmp_path):
    write(home / NEUTRAL / "model-endpoints.yaml", _REGISTRY)
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(tmp_path / "gone.yaml"))
    with pytest.raises(model_endpoints.EndpointRegistryError, match="not a readable file"):
        model_endpoints.load_endpoint_registry()


# --- API keys ---------------------------------------------------------------------------------


def _key(project):
    return api_key.get_api_key(project)


def test_the_user_key_resolves_neutral_first_then_legacy(home, tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    assert _key(project).source == "missing"
    write(home / LEGACY_ENV, "OPENROUTER_API_KEY=legacy-key\n")
    found = _key(project)
    assert (found.key, found.source, found.source_path) == ("legacy-key", "user", home / LEGACY_ENV)
    write(home / NEUTRAL / ".env", "OPENROUTER_API_KEY=neutral-key\n")
    found = _key(project)
    assert (found.key, found.source_path) == ("neutral-key", home / NEUTRAL / ".env")


def test_the_env_file_override_replaces_the_whole_user_layer(home, tmp_path, monkeypatch):
    write(home / LEGACY_ENV, "OPENROUTER_API_KEY=legacy-key\n")
    write(home / NEUTRAL / ".env", "OPENROUTER_API_KEY=neutral-key\n")
    chosen = write(tmp_path / "keys.env", "OPENROUTER_API_KEY=chosen-key\n")
    monkeypatch.setenv("LLM_KIT_ENV_FILE", str(chosen))
    found = _key(tmp_path)
    assert (found.key, found.source_path) == ("chosen-key", chosen)
    monkeypatch.setenv("LLM_KIT_ENV_FILE", str(tmp_path / "missing.env"))
    assert _key(tmp_path).source == "missing"  # a set override is never skipped for another file


def test_the_project_key_file_is_dot_llm_kit_and_outranks_the_user_layer(home, tmp_path):
    write(home / NEUTRAL / ".env", "OPENROUTER_API_KEY=user-key\n")
    project = tmp_path / "p"
    write(project / ".llm-kit" / ".env", "OPENROUTER_API_KEY=project-key\n")
    found = _key(project)
    assert (found.key, found.source, found.source_path) == ("project-key", "project", project / ".llm-kit" / ".env")
    # the plugin-era project files remain readable below it
    other = tmp_path / "q"
    write(other / ".local-data" / "plugins-kit" / "llm-scripting-kit" / ".env", "OPENROUTER_API_KEY=old-project\n")
    assert _key(other).key == "old-project"
    assert constants.layered_project_env_files(project)[0] == project / ".llm-kit" / ".env"


def test_set_key_writes_the_neutral_file_seeded_from_the_legacy_one(home, capsys):
    legacy = write(home / LEGACY_ENV, "OTHER_KEY=keep-me\n")
    out = run(cli.main, ["account", "set-key", "--key", "k-new", "--no-validate"], capsys)
    assert out.code == 0 and str(home / NEUTRAL / ".env") in out.out
    stored = (home / NEUTRAL / ".env").read_text()
    assert "OPENROUTER_API_KEY=k-new" in stored and "OTHER_KEY=keep-me" in stored  # the neutral file shadows the legacy one whole
    assert legacy.read_text() == "OTHER_KEY=keep-me\n"
    assert (os.stat(home / NEUTRAL / ".env").st_mode & 0o077) == 0 or os.name == "nt"


def test_nothing_under_claude_is_created_by_the_user_state_commands(home, tmp_path, capsys):
    for argv in (
        ["consent", "show"],
        ["consent", "set", "--subscription", "on", "--metered", "on"],
        ["account", "set-key", "--key", "k", "--no-validate"],
        ["account", "which"],
    ):
        run(cli.main, argv, capsys)
    assert not (home / ".claude").exists()
    assert sorted(p.name for p in (home / NEUTRAL).iterdir()) == [".env", "consent.json"]
