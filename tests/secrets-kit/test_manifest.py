"""Declaration loading, validation, and variable expansion."""

import json

import pytest

from secrets_kit import SecretsError
from secrets_kit.manifest import Config, Manifest, expand


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- secrets.json ---------------------------------------------------------

def test_absent_config_loads_as_none(tmp_path):
    assert Config.load(tmp_path / "missing.json") is None


def test_config_without_repo_is_rejected(tmp_path):
    path = _write(tmp_path / "s.json", {"machines": {}})
    with pytest.raises(SecretsError, match="declares no 'repo'"):
        Config.load(path)


def test_malformed_config_names_the_file(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ nope", encoding="utf-8")
    with pytest.raises(SecretsError, match="not valid JSON"):
        Config.load(path)


def test_machine_vars_override_global_vars(tmp_path):
    """The knowledge bank sits at a different absolute path per machine."""
    path = _write(
        tmp_path / "s.json",
        {
            "repo": "git@example.com:a/b.git",
            "vars": {"BANK": "/default", "SHARED": "same"},
            "machines": {"win": {"profiles": [], "vars": {"BANK": "C:/dev"}}},
        },
    )
    config = Config.load(path)
    assert config.vars_for("win") == {"BANK": "C:/dev", "SHARED": "same"}


def test_host_resolution_prefers_the_exact_name(tmp_path, monkeypatch):
    path = _write(
        tmp_path / "s.json",
        {
            "repo": "git@example.com:a/b.git",
            "machines": {"box.local": {"profiles": ["x"]}, "box": {"profiles": ["y"]}},
        },
    )
    monkeypatch.setattr(
        "secrets_kit.manifest.resolve_host", lambda: ["box.local", "box"]
    )
    config = Config.load(path)
    assert config.machine_key() == "box.local"


def test_host_resolution_falls_back_to_the_short_form(tmp_path, monkeypatch):
    """Matches the engine's own rule so both files can key on the same names."""
    path = _write(
        tmp_path / "s.json",
        {"repo": "git@example.com:a/b.git", "machines": {"box": {"profiles": ["y"]}}},
    )
    monkeypatch.setattr(
        "secrets_kit.manifest.resolve_host", lambda: ["box.local", "box"]
    )
    assert Config.load(path).machine_key() == "box"


# --- manifest.json --------------------------------------------------------

def _manifest(tmp_path, **overrides):
    data = {
        "version": 1,
        "recipient": "age1abc",
        "profiles": {"p": ["one"]},
        "entries": {"one": {"blob": "blobs/one.age", "dest": "~/one.txt"}},
    }
    data.update(overrides)
    return Manifest.load(_write(tmp_path / "manifest.json", data))


def test_manifest_requires_a_recipient(tmp_path):
    with pytest.raises(SecretsError, match="declares no 'recipient'"):
        _manifest(tmp_path, recipient="")


def test_profile_naming_an_unknown_entry_is_rejected(tmp_path):
    """A dangling name would silently materialize nothing."""
    with pytest.raises(SecretsError, match="unknown entry 'ghost'"):
        _manifest(tmp_path, profiles={"p": ["one", "ghost"]})


def test_entry_requires_blob_and_dest(tmp_path):
    with pytest.raises(SecretsError, match="declares no 'dest'"):
        _manifest(tmp_path, entries={"one": {"blob": "b.age"}}, profiles={})


def test_mode_defaults_to_owner_only(tmp_path):
    assert _manifest(tmp_path).entries["one"].mode == 0o600


def test_mode_parses_octal_strings(tmp_path):
    m = _manifest(
        tmp_path,
        entries={"one": {"blob": "b.age", "dest": "~/x", "mode": "0644"}},
        profiles={},
    )
    assert m.entries["one"].mode == 0o644


def test_bad_mode_is_rejected(tmp_path):
    with pytest.raises(SecretsError, match="not an octal string"):
        _manifest(
            tmp_path,
            entries={"one": {"blob": "b.age", "dest": "~/x", "mode": "rw-"}},
            profiles={},
        )


def test_bad_newline_value_is_rejected(tmp_path):
    with pytest.raises(SecretsError, match="newline must be 'lf'"):
        _manifest(
            tmp_path,
            entries={"one": {"blob": "b.age", "dest": "~/x", "newline": "crlf"}},
            profiles={},
        )


def test_select_unions_profiles_and_dedupes(tmp_path):
    m = _manifest(
        tmp_path,
        profiles={"a": ["one", "two"], "b": ["two"]},
        entries={
            "one": {"blob": "1.age", "dest": "~/1"},
            "two": {"blob": "2.age", "dest": "~/2"},
        },
    )
    assert [e.name for e in m.select(["a", "b"])] == ["one", "two"]


def test_select_rejects_an_unknown_profile(tmp_path):
    m = _manifest(tmp_path)
    with pytest.raises(SecretsError, match="unknown profile 'typo'"):
        m.select(["typo"])


def test_dump_round_trips(tmp_path):
    m = _manifest(tmp_path)
    again = Manifest(tmp_path / "manifest.json", json.loads(m.dump()))
    assert again.recipient == m.recipient
    assert again.entries["one"].mode == m.entries["one"].mode


# --- expansion ------------------------------------------------------------

def test_expand_uses_declared_vars_first(monkeypatch):
    monkeypatch.setenv("X", "from-env")
    assert expand("${X}/y", {"X": "from-config"}, where="t") == "from-config/y"


def test_expand_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("DEVROOT", "/dev")
    assert expand("${DEVROOT}/y", {}, where="t") == "/dev/y"


def test_expand_refuses_to_leave_a_literal_placeholder():
    """Materializing a secret to a path containing ${VAR} is worse than failing."""
    with pytest.raises(SecretsError, match=r"cannot resolve \$\{NOPE\}"):
        expand("${NOPE}/y", {}, where="entry 'x'")


def test_per_os_dest_object(tmp_path, monkeypatch):
    m = _manifest(
        tmp_path,
        profiles={},
        entries={
            "one": {
                "blob": "b.age",
                "dest": {"default": "/posix/x", "windows": "C:/win/x"},
            }
        },
    )
    monkeypatch.setattr("secrets_kit.manifest.os.name", "posix")
    assert str(m.entries["one"].dest({})) == "/posix/x"
