"""Tests for the model-endpoints registry reader.

Every test redirects the home directory, so the CONVENTION path resolves inside
a tmp_path and no test can read (or create) the developer's real
``~/.claude/config/model-endpoints.yaml``.
"""

import pytest

from llm_scripting_kit.model_endpoints import (
    REGISTRY_ENV,
    EndpointRegistryError,
    default_registry_path,
    load_endpoint_registry,
    resolve_registry_entry,
)


VALID_YAML = """\
version: 1
default: alpha
models:
  alpha:
    name: Alpha on a box
    base_url: http://alpha.invalid:8080/v1
    model: alpha-27b
    context_window: 262144
    reasoning_effort: medium
  beta:
    base_url: http://beta.invalid:8080/v1
    model: beta-9b
    key_env: BETA_API_KEY
"""


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect ``Path.home()`` at a tmp dir on every platform.

    Both variables are set because ``expanduser`` reads HOME on POSIX and
    USERPROFILE on Windows, and this suite runs on both.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv(REGISTRY_ENV, raising=False)
    return home


def _write_convention(home, text):
    path = home / ".claude" / "config" / "model-endpoints.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestPathResolution:
    def test_convention_path_is_home_relative(self, fake_home):
        assert default_registry_path() == (
            fake_home / ".claude" / "config" / "model-endpoints.yaml"
        )

    def test_no_override_no_file_is_an_empty_registry(self, fake_home):
        reg = load_endpoint_registry()
        assert reg.entries == {}
        assert reg.default_id is None
        assert reg.path is None

    def test_override_wins_over_an_existing_convention_file(
        self, fake_home, tmp_path, monkeypatch
    ):
        _write_convention(fake_home, VALID_YAML)
        other = tmp_path / "elsewhere.yaml"
        other.write_text(
            "models:\n  only:\n    base_url: http://only.invalid/v1\n    model: only-1\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(REGISTRY_ENV, str(other))
        reg = load_endpoint_registry()
        assert sorted(reg.entries) == ["only"]
        assert reg.path == other

    def test_override_expands_a_tilde(self, fake_home, monkeypatch):
        target = fake_home / "reg.yaml"
        target.write_text(
            "models:\n  a:\n    base_url: http://a.invalid/v1\n    model: a-1\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(REGISTRY_ENV, "~/reg.yaml")
        reg = load_endpoint_registry()
        assert sorted(reg.entries) == ["a"]

    def test_dangling_override_is_loud(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.setenv(REGISTRY_ENV, str(tmp_path / "nope.yaml"))
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert REGISTRY_ENV in str(exc.value)
        assert "nope.yaml" in str(exc.value)

    def test_explicit_environ_mapping_is_honored(self, fake_home, tmp_path):
        target = tmp_path / "explicit.yaml"
        target.write_text(
            "models:\n  x:\n    base_url: http://x.invalid/v1\n    model: x-1\n",
            encoding="utf-8",
        )
        reg = load_endpoint_registry({REGISTRY_ENV: str(target)})
        assert sorted(reg.entries) == ["x"]


class TestSchema:
    def test_valid_file_parses_every_field(self, fake_home):
        path = _write_convention(fake_home, VALID_YAML)
        reg = load_endpoint_registry()
        assert reg.default_id == "alpha"
        assert reg.path == path
        alpha = reg.entries["alpha"]
        assert alpha.id == "alpha"
        assert alpha.base_url == "http://alpha.invalid:8080/v1"
        assert alpha.model == "alpha-27b"
        assert alpha.name == "Alpha on a box"
        assert alpha.context_window == 262144
        assert alpha.reasoning_effort == "medium"
        assert alpha.key_env is None  # omitted = keyless
        assert reg.entries["beta"].key_env == "BETA_API_KEY"

    def test_unknown_keys_are_ignored(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n  a:\n    base_url: http://a.invalid/v1\n"
            "    model: a-1\n    someday: whatever\n",
        )
        assert load_endpoint_registry().entries["a"].model == "a-1"

    def test_malformed_yaml_at_the_convention_path_raises(self, fake_home):
        path = _write_convention(fake_home, "models: [unclosed\n")
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert str(path) in str(exc.value)

    def test_a_non_mapping_document_raises(self, fake_home):
        _write_convention(fake_home, "- a\n- b\n")
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert "not a YAML mapping" in str(exc.value)

    def test_missing_models_map_raises(self, fake_home):
        _write_convention(fake_home, "version: 1\ndefault: alpha\n")
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert "no 'models' map" in str(exc.value)

    def test_entry_without_base_url_raises_naming_the_entry(self, fake_home):
        _write_convention(fake_home, "models:\n  alpha:\n    model: alpha-27b\n")
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert "alpha" in str(exc.value)
        assert "base_url" in str(exc.value)

    def test_entry_without_model_raises_naming_the_entry(self, fake_home):
        _write_convention(
            fake_home, "models:\n  alpha:\n    base_url: http://a.invalid/v1\n"
        )
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert "alpha" in str(exc.value)
        assert "'model'" in str(exc.value)

    def test_entry_that_is_not_a_mapping_raises(self, fake_home):
        _write_convention(fake_home, "models:\n  alpha: http://a.invalid/v1\n")
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert "not a mapping" in str(exc.value)

    def test_non_integer_context_window_raises(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n  alpha:\n    base_url: http://a.invalid/v1\n"
            "    model: a-1\n    context_window: lots\n",
        )
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert "context_window" in str(exc.value)

    def test_default_naming_no_entry_raises(self, fake_home):
        _write_convention(
            fake_home,
            "default: missing\nmodels:\n  alpha:\n"
            "    base_url: http://a.invalid/v1\n    model: a-1\n",
        )
        with pytest.raises(EndpointRegistryError) as exc:
            load_endpoint_registry()
        assert "missing" in str(exc.value)
        assert "alpha" in str(exc.value)

    def test_no_default_is_allowed(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n  alpha:\n    base_url: http://a.invalid/v1\n    model: a-1\n",
        )
        assert load_endpoint_registry().default_id is None


class TestResolveRegistryEntry:
    def test_none_resolves_the_default_entry(self, fake_home):
        _write_convention(fake_home, VALID_YAML)
        assert resolve_registry_entry(None).id == "alpha"

    def test_explicit_id_resolves_that_entry(self, fake_home):
        _write_convention(fake_home, VALID_YAML)
        assert resolve_registry_entry("beta").model == "beta-9b"

    def test_unknown_id_lists_the_known_ids(self, fake_home):
        _write_convention(fake_home, VALID_YAML)
        with pytest.raises(EndpointRegistryError) as exc:
            resolve_registry_entry("gamma")
        assert "gamma" in str(exc.value)
        assert "alpha, beta" in str(exc.value)

    def test_no_default_declared_raises(self, fake_home):
        _write_convention(
            fake_home,
            "models:\n  alpha:\n    base_url: http://a.invalid/v1\n    model: a-1\n",
        )
        with pytest.raises(EndpointRegistryError) as exc:
            resolve_registry_entry(None)
        assert "default" in str(exc.value)

    def test_an_injected_registry_skips_the_file_read(self, fake_home):
        _write_convention(fake_home, VALID_YAML)
        reg = load_endpoint_registry()
        (fake_home / ".claude" / "config" / "model-endpoints.yaml").unlink()
        assert resolve_registry_entry("beta", registry=reg).id == "beta"
