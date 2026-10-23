"""The consent file store (llm_kit.registry.consent): load, save, update and path resolution.

Every test runs against a fake home; nothing touches the real one. The effective
policy (scopes, provenance) and the opt-in surfaces live in test_consent_layer.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_kit.registry import consent as consent_mod
from llm_kit.registry.consent import (
    CLASSES,
    ConsentConfig,
    ConsentConfigError,
    default_consent_path,
    load_config,
    policy,
    save_config,
    update_config,
)

DEFAULT = {"local": True, "subscription": True, "metered": False}


def _set_home(monkeypatch, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert Path.home() == home


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _set_home(monkeypatch, home)
    return home


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    elif isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _valid(**over):
    data = {
        "schema": 2, "local": True, "subscription": True, "metered": False,
        "scopes": {}, "updated_at": "2026-10-21T00:00:00+00:00",
    }
    data.update(over)
    return data


def _globals(config: ConsentConfig) -> dict:
    return dict(config.globals_)


# ---- a missing file is the default policy, never an error -------------------------------------


def test_missing_file_is_the_default_policy_and_creates_nothing(tmp_path):
    target = tmp_path / "missing" / "consent.json"
    config = load_config(target)
    assert config.source == "default" and config.path == target
    assert dict(config.globals_) == {} and dict(config.scopes) == {}
    result = policy(path=target)
    assert {name: state.allowed for name, state in result.classes.items()} == DEFAULT
    assert all(state.provenance == "default" for state in result.classes.values())
    assert result.source == "default"
    assert not target.parent.exists()  # reading created nothing, not even the directory


def test_default_path_absent_uses_fake_home(fake_home):
    expected = fake_home / ".config/llm-kit/consent.json"
    assert default_consent_path() == expected
    assert policy().source == "default" and policy().path == expected
    assert not (fake_home / ".config").exists()


# ---- a present file that cannot be trusted is a loud error ---------------------------------------


@pytest.mark.parametrize("field", list(CLASSES))
@pytest.mark.parametrize("bad", [1, 0, "true", "false", "yes", None, [], {}, 1.0])
def test_non_bool_class_is_a_configuration_error(tmp_path, field, bad):
    target = _write(tmp_path / "c.json", _valid(**{field: bad}))
    with pytest.raises(ConsentConfigError, match=field):
        load_config(target)


@pytest.mark.parametrize("schema", [0, 1, 3, True, "2", 2.0, None])
def test_bad_schema_is_a_configuration_error(tmp_path, schema):
    target = _write(tmp_path / "c.json", _valid(schema=schema))
    with pytest.raises(ConsentConfigError, match="schema"):
        load_config(target)


def test_missing_schema_is_a_configuration_error(tmp_path):
    data = _valid()
    del data["schema"]
    with pytest.raises(ConsentConfigError, match="schema"):
        load_config(_write(tmp_path / "c.json", data))


def test_the_retired_usage_spend_file_is_refused_not_translated(tmp_path):
    target = _write(tmp_path / "c.json", {"schema": 1, "usage": True, "spend": True, "updated_at": "x"})
    with pytest.raises(ConsentConfigError, match="usage/spend"):
        load_config(target)


@pytest.mark.parametrize("payload", [[], "x", 3, None, True])
def test_non_object_json_is_a_configuration_error(tmp_path, payload):
    target = _write(tmp_path / "c.json", json.dumps(payload))
    with pytest.raises(ConsentConfigError, match="JSON object"):
        load_config(target)


@pytest.mark.parametrize("payload", ["", "{not json", b"\xff\xfe\x00bad"])
def test_decode_error_is_a_configuration_error(tmp_path, payload):
    target = _write(tmp_path / "c.json", payload)
    with pytest.raises(ConsentConfigError, match="not valid JSON"):
        load_config(target)


def test_unreadable_path_is_a_configuration_error(tmp_path):
    target = tmp_path / "c.json"
    target.mkdir()  # a directory raises an OSError other than FileNotFoundError
    with pytest.raises(ConsentConfigError, match="cannot be read"):
        load_config(target)


def test_parent_is_a_file_is_a_configuration_error(tmp_path):
    blocker = _write(tmp_path / "blocker", "x")
    with pytest.raises(ConsentConfigError):
        load_config(blocker / "consent.json")


@pytest.mark.parametrize(
    "mutation,match",
    [
        ({"free": True}, "free"),
        ({"usage": True}, "usage"),
        ({"scopes": []}, "scopes"),
        ({"scopes": {"x": []}}, "scope 'x'"),
        ({"scopes": {"x": {"free": True}}}, "free"),
        ({"scopes": {"x": {"metered": "no"}}}, "metered"),
        ({"scopes": {"": {"metered": False}}}, "non-empty"),
        ({"scopes": {" pad": {"metered": False}}}, "whitespace"),
    ],
)
def test_unknown_keys_and_bad_scopes_are_configuration_errors(tmp_path, mutation, match):
    target = _write(tmp_path / "c.json", _valid(**mutation))
    with pytest.raises(ConsentConfigError, match=match):
        load_config(target)
    with pytest.raises(ConsentConfigError):
        policy(path=target)


def test_the_error_is_a_value_error_so_the_cli_reports_a_configuration_error():
    assert issubclass(ConsentConfigError, ValueError)


def test_absent_global_keys_fall_back_to_the_default_with_default_provenance(tmp_path):
    target = _write(tmp_path / "c.json", {"schema": 2, "metered": True})
    config = load_config(target)
    assert _globals(config) == {"metered": True}
    result = policy(path=target)
    assert result.classes["metered"].allowed and result.classes["metered"].provenance == "global"
    assert result.classes["local"].allowed and result.classes["local"].provenance == "default"


@pytest.mark.parametrize(
    "sub,met",
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_valid_file_allowed_classes(tmp_path, sub, met):
    target = _write(tmp_path / "c.json", _valid(subscription=sub, metered=met))
    result = policy(path=target)
    expected = {"local"} | ({"subscription"} if sub else set()) | ({"metered"} if met else set())
    assert result.allowed == frozenset(expected) and isinstance(result.allowed, frozenset)
    assert result.source == "file" and result.path == target


# ---- the path is resolved per call ---------------------------------------------------------------


def test_path_resolved_per_call(tmp_path, monkeypatch):
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    _set_home(monkeypatch, home_a)
    first = default_consent_path()
    _set_home(monkeypatch, home_b)
    second = default_consent_path()
    assert first == home_a / ".config/llm-kit/consent.json"
    assert second == home_b / ".config/llm-kit/consent.json"
    # The default path is also honoured by load and save, not only by the accessor.
    saved, _ = update_config({"metered": True})
    assert saved == second
    assert policy().source == "file"
    _set_home(monkeypatch, home_a)
    assert policy().source == "default"


# ---- save and update -----------------------------------------------------------------------------


def test_save_writes_format_and_roundtrips(tmp_path):
    target = tmp_path / "deep" / "dir" / "consent.json"
    written = save_config(
        globals_=DEFAULT, scopes={"databench": {"metered": True}}, path=target
    )
    assert written == target
    data = json.loads(target.read_text(encoding="utf-8"))
    assert set(data) == {"schema", "local", "subscription", "metered", "scopes", "updated_at"}
    assert data["schema"] == 2 and data["scopes"] == {"databench": {"metered": True}}
    assert [data[c] for c in CLASSES] == [True, True, False]
    from datetime import datetime

    datetime.fromisoformat(data["updated_at"])
    loaded = load_config(target)
    assert _globals(loaded) == DEFAULT and dict(loaded.scopes) == {"databench": {"metered": True}}
    assert loaded.source == "file"


def test_a_global_change_preserves_every_scope_entry(tmp_path):
    target = tmp_path / "consent.json"
    save_config(globals_=DEFAULT, scopes={"a": {"metered": False}, "b": {"subscription": False}}, path=target)
    _, config = update_config({"metered": True}, path=target)
    assert _globals(config)["metered"] is True
    assert {k: dict(v) for k, v in config.scopes.items()} == {
        "a": {"metered": False}, "b": {"subscription": False},
    }


def test_a_scope_change_preserves_the_global_policy_and_other_scopes(tmp_path):
    target = tmp_path / "consent.json"
    save_config(globals_={"local": True, "subscription": False, "metered": True}, scopes={"a": {"metered": False}}, path=target)
    _, config = update_config({"subscription": False}, scope="b", path=target)
    assert _globals(config) == {"local": True, "subscription": False, "metered": True}
    assert {k: dict(v) for k, v in config.scopes.items()} == {
        "a": {"metered": False}, "b": {"subscription": False},
    }
    _, config = update_config({"metered": False}, scope="b", path=target)
    assert dict(config.scopes["b"]) == {"subscription": False, "metered": False}


def test_update_starts_from_the_default_when_there_is_no_file(tmp_path):
    target = tmp_path / "consent.json"
    _, config = update_config({"metered": True}, path=target)
    assert _globals(config) == {"local": True, "subscription": True, "metered": True}


def test_update_refuses_to_overwrite_a_malformed_file(tmp_path):
    target = _write(tmp_path / "c.json", "{broken")
    with pytest.raises(ConsentConfigError):
        update_config({"metered": True}, path=target)
    assert target.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize("bad_scope", ["", "  ", " x", "x ", 3])
def test_update_rejects_an_unusable_scope_name(tmp_path, bad_scope):
    with pytest.raises(ValueError, match="scope"):
        update_config({"metered": True}, scope=bad_scope, path=tmp_path / "c.json")
    assert not (tmp_path / "c.json").exists()


def test_update_rejects_an_unknown_class(tmp_path):
    with pytest.raises(ValueError, match="free"):
        update_config({"free": True}, path=tmp_path / "c.json")


def test_save_atomic(tmp_path, monkeypatch):
    tmp_path = tmp_path / "d"
    target = tmp_path / "consent.json"
    save_config(globals_=DEFAULT, scopes={}, path=target)
    old = target.read_bytes()

    calls = []
    real_replace = Path.replace

    def spy(self, dst):
        calls.append((Path(self), Path(dst)))
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", spy)
    save_config(globals_={"local": True, "subscription": True, "metered": True}, scopes={}, path=target)
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == target
    assert src != target and src.parent == target.parent
    assert policy(path=target).allowed == frozenset({"local", "subscription", "metered"})
    assert list(tmp_path.iterdir()) == [target]

    # A failure before the replace leaves the old content intact and no tmp behind.
    good = target.read_bytes()

    def boom(self, dst):
        raise OSError("injected failure before replace")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="injected"):
        save_config(globals_=DEFAULT, scopes={}, path=target)
    assert target.read_bytes() == good
    assert list(tmp_path.iterdir()) == [target]
    assert old != good


def test_save_failure_keeps_old_content_when_write_fails(tmp_path, monkeypatch):
    tmp_path = tmp_path / "d"
    target = tmp_path / "consent.json"
    save_config(globals_=DEFAULT, scopes={}, path=target)
    before = target.read_bytes()

    def bad_dump(*args, **kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr(consent_mod.json, "dump", bad_dump)
    with pytest.raises(OSError, match="injected"):
        save_config(globals_={"local": True, "subscription": False, "metered": True}, scopes={}, path=target)
    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]


def test_save_error_propagates(tmp_path):
    blocker = _write(tmp_path / "blocker", "x")
    with pytest.raises(OSError):
        save_config(globals_=DEFAULT, scopes={}, path=blocker / "sub" / "consent.json")


def test_save_replace_error_propagates(tmp_path, monkeypatch):
    def boom(self, dst):
        raise PermissionError("injected")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(PermissionError):
        save_config(globals_=DEFAULT, scopes={}, path=tmp_path / "consent.json")


@pytest.mark.parametrize(
    "globals_",
    [
        {"local": 1, "subscription": True, "metered": False},
        {"local": True, "subscription": "no", "metered": False},
        {"local": True, "subscription": True},
    ],
)
def test_save_rejects_non_bool_or_incomplete_globals(tmp_path, globals_):
    target = tmp_path / "consent.json"
    with pytest.raises(TypeError):
        save_config(globals_=globals_, scopes={}, path=target)
    assert not target.exists()
