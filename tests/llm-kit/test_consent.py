"""Checks C10-C13 for llm_kit.registry.consent (cost-class consent plan, unit U2).

Every test runs against a fake home; nothing touches the real one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_kit.registry import consent as consent_mod
from llm_kit.registry.consent import (
    Consent,
    default_consent_path,
    load_consent,
    save_consent,
)

REMEDY = "llm-kit consent set --usage on"


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
    data = {"schema": 1, "usage": True, "spend": False, "updated_at": "2026-10-21T00:00:00+00:00"}
    data.update(over)
    return data


def _assert_free_only(result: Consent, path: Path, source: str) -> None:
    assert result.usage is False and result.spend is False
    assert result.allowed == frozenset({"free"})
    assert result.source == source
    assert result.path == path
    assert str(path) in result.note
    assert REMEDY in result.note


# ---- C10 -------------------------------------------------------------------


def test_absent_free_only_with_note(tmp_path):
    target = tmp_path / "missing" / "consent.json"
    _assert_free_only(load_consent(target), target, "absent")


def test_default_path_absent_uses_fake_home(fake_home):
    result = load_consent()
    expected = fake_home / ".config/llm-kit/consent.json"
    assert default_consent_path() == expected
    _assert_free_only(result, expected, "absent")


@pytest.mark.parametrize("field", ["usage", "spend"])
@pytest.mark.parametrize("bad", [1, 0, "true", "false", "yes", None, [], {}, 1.0])
def test_non_bool_fails_closed(tmp_path, field, bad):
    # The other field is a real True, so a bool()/truthy shortcut would grant it.
    target = _write(tmp_path / "c.json", _valid(**{"usage": True, "spend": True, field: bad}))
    _assert_free_only(load_consent(target), target, "unreadable")


@pytest.mark.parametrize("missing", ["usage", "spend", "schema"])
def test_missing_key_fails_closed(tmp_path, missing):
    data = _valid(usage=True, spend=True)
    del data[missing]
    target = _write(tmp_path / "c.json", data)
    _assert_free_only(load_consent(target), target, "unreadable")


@pytest.mark.parametrize("schema", [0, 2, True, "1", 1.0, None])
def test_bad_schema_fails_closed(tmp_path, schema):
    target = _write(tmp_path / "c.json", _valid(usage=True, spend=True, schema=schema))
    _assert_free_only(load_consent(target), target, "unreadable")


@pytest.mark.parametrize("payload", [[], "x", 3, None, True])
def test_non_object_json_fails_closed(tmp_path, payload):
    target = _write(tmp_path / "c.json", json.dumps(payload))
    _assert_free_only(load_consent(target), target, "unreadable")


@pytest.mark.parametrize("payload", ["", "{not json", b"\xff\xfe\x00bad"])
def test_decode_error_free_only(tmp_path, payload):
    target = _write(tmp_path / "c.json", payload)
    _assert_free_only(load_consent(target), target, "unreadable")


def test_unreadable_path_free_only(tmp_path):
    # A directory at the path raises an OSError other than FileNotFoundError.
    target = tmp_path / "c.json"
    target.mkdir()
    _assert_free_only(load_consent(target), target, "unreadable")


def test_parent_is_a_file_free_only(tmp_path):
    blocker = _write(tmp_path / "blocker", "x")
    target = blocker / "consent.json"
    result = load_consent(target)
    assert result.allowed == frozenset({"free"})
    assert REMEDY in result.note


@pytest.mark.parametrize(
    "usage,spend,allowed",
    [
        (False, False, {"free"}),
        (True, False, {"free", "usage"}),
        (False, True, {"free", "spend"}),
        (True, True, {"free", "usage", "spend"}),
    ],
)
def test_valid_file_allowed_classes(tmp_path, usage, spend, allowed):
    target = _write(tmp_path / "c.json", _valid(usage=usage, spend=spend))
    result = load_consent(target)
    assert (result.usage, result.spend, result.source, result.note) == (usage, spend, "file", "")
    assert result.path == target
    assert result.allowed == frozenset(allowed)
    assert isinstance(result.allowed, frozenset)


# ---- C11 -------------------------------------------------------------------


def test_path_resolved_per_call(tmp_path, monkeypatch):
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    _set_home(monkeypatch, home_a)
    first = default_consent_path()
    _set_home(monkeypatch, home_b)
    second = default_consent_path()
    assert first == home_a / ".config/llm-kit/consent.json"
    assert second == home_b / ".config/llm-kit/consent.json"
    # The default path is also honoured by load and save, not only by the accessor.
    saved = save_consent(usage=True, spend=False)
    assert saved == second
    assert load_consent().source == "file"
    _set_home(monkeypatch, home_a)
    assert load_consent().source == "absent"


# ---- C12 -------------------------------------------------------------------


def test_save_writes_format_and_roundtrips(tmp_path):
    target = tmp_path / "deep" / "dir" / "consent.json"
    assert save_consent(usage=True, spend=False, path=target) == target
    data = json.loads(target.read_text(encoding="utf-8"))
    assert set(data) == {"schema", "usage", "spend", "updated_at"}
    assert data["schema"] == 1 and data["usage"] is True and data["spend"] is False
    assert isinstance(data["updated_at"], str) and data["updated_at"]
    from datetime import datetime

    datetime.fromisoformat(data["updated_at"])
    loaded = load_consent(target)
    assert (loaded.usage, loaded.spend, loaded.source) == (True, False, "file")


def test_save_atomic(tmp_path, monkeypatch):
    tmp_path = tmp_path / "d"
    target = tmp_path / "consent.json"
    save_consent(usage=False, spend=False, path=target)
    old = target.read_bytes()

    calls = []
    real_replace = Path.replace

    def spy(self, dst):
        calls.append((Path(self), Path(dst)))
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", spy)
    save_consent(usage=True, spend=True, path=target)
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == target
    assert src != target and src.parent == target.parent
    assert load_consent(target).allowed == frozenset({"free", "usage", "spend"})
    assert list(tmp_path.iterdir()) == [target]

    # A failure before the replace leaves the old content intact and no tmp behind.
    good = target.read_bytes()

    def boom(self, dst):
        raise OSError("injected failure before replace")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="injected"):
        save_consent(usage=False, spend=False, path=target)
    assert target.read_bytes() == good
    assert list(tmp_path.iterdir()) == [target]
    assert old != good


def test_save_failure_keeps_old_content_when_write_fails(tmp_path, monkeypatch):
    tmp_path = tmp_path / "d"
    target = tmp_path / "consent.json"
    save_consent(usage=True, spend=False, path=target)
    before = target.read_bytes()

    def bad_dump(*args, **kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr(consent_mod.json, "dump", bad_dump)
    with pytest.raises(OSError, match="injected"):
        save_consent(usage=False, spend=True, path=target)
    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]


# ---- C13 -------------------------------------------------------------------


def test_save_error_propagates(tmp_path):
    blocker = _write(tmp_path / "blocker", "x")
    with pytest.raises(OSError):
        save_consent(usage=True, spend=False, path=blocker / "sub" / "consent.json")


def test_save_replace_error_propagates(tmp_path, monkeypatch):
    def boom(self, dst):
        raise PermissionError("injected")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(PermissionError):
        save_consent(usage=True, spend=True, path=tmp_path / "consent.json")


@pytest.mark.parametrize("usage,spend", [(1, False), (True, "no"), (None, True)])
def test_save_rejects_non_bool(tmp_path, usage, spend):
    target = tmp_path / "consent.json"
    with pytest.raises(TypeError):
        save_consent(usage=usage, spend=spend, path=target)
    assert not target.exists()
