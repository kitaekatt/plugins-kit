"""Tests for llm_scripting_kit.api_key precedence resolution."""

import copy
from pathlib import Path

import pytest

from llm_scripting_kit import constants
from llm_scripting_kit.api_key import get_api_key
from llm_scripting_kit.env_file import write_env_file

CANONICAL_REL = (".local-data", "plugins-kit", "llm-scripting-kit", ".env")
LEGACY_REL = (".local-data", "llm-scripting-kit", ".env")


def _canonical(project_root):
    return project_root.joinpath(*CANONICAL_REL)


def _legacy(project_root):
    return project_root.joinpath(*LEGACY_REL)


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path):
    """Redirect USER_ENV_FILE and project_env_file into tmp_path so tests
    never touch the developer's real credential file."""
    user_env = tmp_path / "user" / ".env"
    monkeypatch.setattr(constants, "USER_ENV_FILE", user_env)
    # api_key.py imported USER_ENV_FILE at module level; also patch there.
    monkeypatch.setattr("llm_scripting_kit.api_key.USER_ENV_FILE", user_env)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # The one-time legacy notice is process-global; clear it so each test sees it.
    monkeypatch.setattr("llm_scripting_kit.api_key._LEGACY_NOTICE_SEEN", set())
    return tmp_path


class TestProjectPathShape:
    def test_canonical_project_path_is_marketplace_namespaced(self, tmp_path):
        """The project key path must mirror the project config.yaml path.

        Both live under ``<project>/.local-data/<marketplace>/<plugin>/`` --
        that symmetry is the whole point of the canonical location.
        """
        assert constants.project_env_file(tmp_path) == _canonical(tmp_path)

    def test_legacy_project_path_omits_the_marketplace_segment(self, tmp_path):
        assert constants.legacy_project_env_file(tmp_path) == _legacy(tmp_path)

    def test_project_env_files_is_highest_precedence_first(self, tmp_path):
        assert constants.project_env_files(tmp_path) == [
            _canonical(tmp_path),
            _legacy(tmp_path),
        ]


class TestGetApiKey:
    def test_missing_returns_missing_source(self, isolated_paths):
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key is None
        assert result.source == "missing"
        assert result.source_path is None
        assert result.legacy_location is False

    def test_env_var_wins(self, isolated_paths, monkeypatch):
        # Populate every layer; env should still win.
        monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
        write_env_file(
            _canonical(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-project"},
        )
        write_env_file(
            _legacy(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-legacy"},
        )
        write_env_file(
            isolated_paths / "user" / ".env",
            {"OPENROUTER_API_KEY": "from-user"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-env"
        assert result.source == "env"
        assert result.source_path is None

    def test_project_wins_over_user(self, isolated_paths):
        write_env_file(
            _canonical(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-project"},
        )
        write_env_file(
            isolated_paths / "user" / ".env",
            {"OPENROUTER_API_KEY": "from-user"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-project"
        assert result.source == "project"
        assert result.source_path == _canonical(isolated_paths / "project")
        assert result.legacy_location is False

    def test_user_when_only_user_set(self, isolated_paths):
        write_env_file(
            isolated_paths / "user" / ".env",
            {"OPENROUTER_API_KEY": "from-user"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-user"
        assert result.source == "user"
        assert result.source_path == isolated_paths / "user" / ".env"

    def test_empty_env_var_falls_through_to_files(self, isolated_paths, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        write_env_file(
            isolated_paths / "user" / ".env",
            {"OPENROUTER_API_KEY": "from-user"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-user"
        assert result.source == "user"


class TestLegacyProjectLocation:
    """The superseded project path stays readable; it is flagged, not ignored."""

    def test_legacy_location_still_resolves(self, isolated_paths):
        write_env_file(
            _legacy(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-legacy"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-legacy"
        assert result.source == "project"
        assert result.source_path == _legacy(isolated_paths / "project")
        assert result.legacy_location is True

    def test_legacy_location_wins_over_user(self, isolated_paths):
        write_env_file(
            _legacy(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-legacy"},
        )
        write_env_file(
            isolated_paths / "user" / ".env",
            {"OPENROUTER_API_KEY": "from-user"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-legacy"
        assert result.legacy_location is True

    def test_canonical_wins_over_legacy(self, isolated_paths):
        write_env_file(
            _canonical(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-canonical"},
        )
        write_env_file(
            _legacy(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-legacy"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-canonical"
        assert result.source_path == _canonical(isolated_paths / "project")
        assert result.legacy_location is False

    def test_legacy_hit_emits_a_visible_notice(self, isolated_paths, capsys):
        """Silence is the defect being fixed -- the fallback must announce itself."""
        write_env_file(
            _legacy(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-legacy"},
        )
        get_api_key(project_root=isolated_paths / "project")
        err = capsys.readouterr().err
        assert str(_legacy(isolated_paths / "project")) in err
        assert str(_canonical(isolated_paths / "project")) in err

    def test_notice_is_emitted_once_per_process(self, isolated_paths, capsys):
        write_env_file(
            _legacy(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-legacy"},
        )
        get_api_key(project_root=isolated_paths / "project")
        capsys.readouterr()
        get_api_key(project_root=isolated_paths / "project")
        assert capsys.readouterr().err == ""

    def test_canonical_hit_emits_no_notice(self, isolated_paths, capsys):
        write_env_file(
            _canonical(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-canonical"},
        )
        get_api_key(project_root=isolated_paths / "project")
        assert capsys.readouterr().err == ""


class TestKeyFileLayer:
    """The bottom-precedence key_file layer, for a named endpoint's
    ``key_file:`` config (see models.resolve_endpoint)."""

    ENDPOINT_CFG = {
        "default_endpoint": "openrouter",
        "endpoints": {
            "keyfiled": {
                "base_url": "http://localhost:9/v1",
                "key_env": "KEYFILED_API_KEY",
                "key_file": None,  # set per-test
            },
        },
    }

    def _cfg(self, key_file_path):
        cfg = copy.deepcopy(self.ENDPOINT_CFG)
        cfg["endpoints"]["keyfiled"]["key_file"] = str(key_file_path)
        return cfg

    def _get(self, monkeypatch, isolated_paths, key_file_path):
        monkeypatch.setattr(
            "llm_scripting_kit.models.load_model_config",
            lambda **kw: self._cfg(key_file_path),
        )
        monkeypatch.delenv("KEYFILED_API_KEY", raising=False)
        return get_api_key(project_root=isolated_paths / "project", endpoint="keyfiled")

    def test_bare_value_file_resolves(self, monkeypatch, isolated_paths, tmp_path):
        key_file = tmp_path / "secret"
        key_file.write_text("sekrit-value")
        result = self._get(monkeypatch, isolated_paths, key_file)
        assert result.key == "sekrit-value"
        assert result.source == "key_file"
        assert result.source_path == key_file

    def test_trailing_newline_is_stripped(self, monkeypatch, isolated_paths, tmp_path):
        key_file = tmp_path / "secret"
        key_file.write_text("sekrit-value\n")
        result = self._get(monkeypatch, isolated_paths, key_file)
        assert result.key == "sekrit-value"

    def test_missing_file_resolves_to_no_key_without_raising(
        self, monkeypatch, isolated_paths, tmp_path
    ):
        key_file = tmp_path / "does-not-exist"
        result = self._get(monkeypatch, isolated_paths, key_file)
        assert result.key is None
        assert result.source == "missing"

    def test_empty_file_resolves_to_no_key(self, monkeypatch, isolated_paths, tmp_path):
        key_file = tmp_path / "secret"
        key_file.write_text("   \n")
        result = self._get(monkeypatch, isolated_paths, key_file)
        assert result.key is None
        assert result.source == "missing"

    def test_tilde_expansion(self, monkeypatch, isolated_paths, tmp_path):
        # Point HOME at tmp_path so "~/secret" resolves inside the sandbox.
        # ntpath.expanduser prefers USERPROFILE and ignores HOME, so a
        # HOME-only sandbox would resolve to the real profile on Windows.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        (tmp_path / "secret").write_text("home-value")
        result = self._get(monkeypatch, isolated_paths, Path("~/secret"))
        assert result.key == "home-value"
        assert result.source == "key_file"
        assert result.source_path == (tmp_path / "secret")

    def test_non_utf8_file_resolves_to_no_key_without_raising(self, tmp_path):
        """UnicodeDecodeError is a ValueError, not an OSError -- a binary or
        latin-1 credential file must not turn a lookup into a traceback."""
        from llm_scripting_kit.api_key import _read_key_file

        binary = tmp_path / "binary.key"
        binary.write_bytes(b"\xff\xfe\x00\x80not-utf8\xff")
        assert _read_key_file(str(binary)) is None

    def test_multiline_file_resolves_to_no_key(self, tmp_path):
        """A bare-value file holds ONE value. Multi-line content means the path
        names something else, and CR/LF would reach an Authorization header."""
        from llm_scripting_kit.api_key import _read_key_file

        multi = tmp_path / "multi.key"
        multi.write_text("line-one\nline-two\n")
        assert _read_key_file(str(multi)) is None
        crlf = tmp_path / "crlf.key"
        crlf.write_text("value\r\nsecond")
        assert _read_key_file(str(crlf)) is None

    def test_default_endpoint_key_file_refuses_a_foreign_key_env(self, tmp_path, monkeypatch):
        """IDENTITY GUARD. For endpoint=None the earlier layers resolve the
        hardcoded API_KEY_ENV while resolve_endpoint(None) follows the config's
        default_endpoint. When those disagree, layer 5 must NOT answer -- else
        one provider's credential is returned labelled as another's and sent to
        the default endpoint's base_url."""
        from llm_scripting_kit import api_key as module

        secret = tmp_path / "other-provider.key"
        secret.write_text("other-provider-secret")

        def foreign(name, project_root=None):
            return {"name": "my-local", "key_env": "MY_LOCAL_KEY", "key_file": str(secret)}

        monkeypatch.setattr("llm_scripting_kit.models.resolve_endpoint", foreign)
        assert module._default_endpoint_key_file(None) is None

        def matching(name, project_root=None):
            return {
                "name": "openrouter",
                "key_env": module.API_KEY_ENV,
                "key_file": str(secret),
            }

        monkeypatch.setattr("llm_scripting_kit.models.resolve_endpoint", matching)
        assert module._default_endpoint_key_file(None) == str(secret)

    def test_unimportable_model_layer_resolves_to_missing(self, monkeypatch):
        """A consuming plugin's venv need not carry every optional dependency
        the model layer might import (git-kit and p4-kit deliberately omit
        openai). An ImportError from the lazy import must mean "no key_file
        layer", not a raise out of get_api_key -- so the import lives INSIDE
        the try, not above it."""
        import builtins
        import sys

        from llm_scripting_kit import api_key as module

        real_import = builtins.__import__

        def poisoned(name, *args, **kwargs):
            if name.endswith("models"):
                raise ModuleNotFoundError("No module named 'openai'")
            return real_import(name, *args, **kwargs)

        monkeypatch.delitem(sys.modules, "llm_scripting_kit.models", raising=False)
        monkeypatch.setattr(builtins, "__import__", poisoned)
        assert module._default_endpoint_key_file(None) is None

    def test_every_higher_layer_wins_over_key_file(self, monkeypatch, isolated_paths, tmp_path):
        """The load-bearing precedence proof: with every layer populated,
        key_file must never be the one that answers."""
        key_file = tmp_path / "secret"
        key_file.write_text("from-key-file")
        monkeypatch.setattr(
            "llm_scripting_kit.models.load_model_config",
            lambda **kw: self._cfg(key_file),
        )

        project_root = isolated_paths / "project"

        # Only key_file set: it wins, proving the layer works at all.
        only_file = get_api_key(project_root=project_root, endpoint="keyfiled")
        assert only_file.source == "key_file"

        # User .env layer added: user wins over key_file.
        write_env_file(isolated_paths / "user" / ".env", {"KEYFILED_API_KEY": "from-user"})
        beats_user = get_api_key(project_root=project_root, endpoint="keyfiled")
        assert beats_user.key == "from-user"
        assert beats_user.source == "user"

        # Legacy project layer added: it wins over user (and key_file).
        write_env_file(_legacy(project_root), {"KEYFILED_API_KEY": "from-legacy"})
        beats_legacy = get_api_key(project_root=project_root, endpoint="keyfiled")
        assert beats_legacy.key == "from-legacy"
        assert beats_legacy.source == "project"

        # Canonical project layer added: it wins over legacy.
        write_env_file(_canonical(project_root), {"KEYFILED_API_KEY": "from-canonical"})
        beats_canonical = get_api_key(project_root=project_root, endpoint="keyfiled")
        assert beats_canonical.key == "from-canonical"
        assert beats_canonical.source == "project"

        # Env var added: it wins over everything, including key_file.
        monkeypatch.setenv("KEYFILED_API_KEY", "from-env")
        beats_env = get_api_key(project_root=project_root, endpoint="keyfiled")
        assert beats_env.key == "from-env"
        assert beats_env.source == "env"

    def test_default_endpoint_never_loads_config_when_env_var_resolves(
        self, monkeypatch, isolated_paths
    ):
        """The endpoint=None fast path must stay config-free when a higher
        layer already resolves -- key_file lookup is lazy."""

        def _boom(**kw):
            raise AssertionError("config should not be loaded when the env var resolves")

        monkeypatch.setattr("llm_scripting_kit.models.load_model_config", _boom)
        monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-env"
        assert result.source == "env"
