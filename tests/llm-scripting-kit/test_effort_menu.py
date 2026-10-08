"""effort_menu / lower_effort resolve from the endpoint registry's effort style."""
import pytest

from llm_scripting_kit import effort, model_endpoints
from llm_scripting_kit.model_endpoints import EndpointRegistryError


@pytest.fixture
def registry(monkeypatch):
    styles = {}

    def fake_entry(name=None, **_):
        if name not in styles:
            raise EndpointRegistryError(f"unknown {name}")
        return name

    def fake_style(entry):
        return model_endpoints.EffortDelivery(styles[entry], "endpoint")

    monkeypatch.setattr(model_endpoints, "resolve_registry_entry", fake_entry)
    monkeypatch.setattr(model_endpoints, "resolve_effort_style", fake_style)
    return styles


def test_menus(registry):
    registry.update(n="ninfer", t="top-level", c="chat_template_kwargs", u="unsupported")
    assert effort.effort_menu("n") == ("none", "low", "medium", "xhigh")
    assert effort.effort_menu("t") == ("low", "medium", "high")
    assert effort.effort_menu("c") == ("low", "medium", "high")
    assert effort.effort_menu("u") == ()


def test_lower_effort_walks_down(registry):
    registry["n"] = "ninfer"
    assert effort.lower_effort("n", "xhigh") == "medium"
    assert effort.lower_effort("n", "medium") == "low"
    assert effort.lower_effort("n", "low") == "none"
    assert effort.lower_effort("n", "none") is None


def test_lower_effort_remaps_high_on_ninfer(registry):
    registry["n"] = "ninfer"
    assert effort.lower_effort("n", "high") == "medium"


def test_lower_effort_unknown_value_or_no_menu(registry):
    registry.update(n="ninfer", u="unsupported", t="top-level")
    assert effort.lower_effort("n", "bogus") is None
    assert effort.lower_effort("u", "low") is None
    assert effort.lower_effort("t", "low") is None
    assert effort.lower_effort("t", "high") == "medium"


def test_unknown_endpoint_raises(registry):
    with pytest.raises(EndpointRegistryError):
        effort.effort_menu("nope")


def test_real_registry_entry_resolves(tmp_path, monkeypatch):
    path = tmp_path / "model-endpoints.yaml"
    path.write_text(
        "version: 1\ndefault: local\nmodels:\n  local:\n    kind: transport\n"
        "    base_url: http://127.0.0.1:1/v1\n    model: m\n    effort_style: ninfer\n"
    )
    monkeypatch.setenv(model_endpoints.REGISTRY_ENV, str(path))
    assert effort.effort_menu("local") == ("none", "low", "medium", "xhigh")
