from llm_scripting_kit.completion import factory


def test_create_backend_constructs_configured_harness(monkeypatch):
    monkeypatch.setattr(factory, "load_model_config", lambda **_: {
        "default_endpoint": "reviewer",
        "endpoints": {"reviewer": {"harness": "codex", "model": "gpt-test", "effort": "high"}},
    })

    selected = factory.create_backend()

    assert selected.endpoint == "reviewer"
    assert selected.backend.name == "codex-cli"
    assert selected.model == "gpt-test"
    assert selected.effort == "high"


def test_create_backend_constructs_http_transport(monkeypatch):
    config = {"default_endpoint": "local"}
    monkeypatch.setattr(factory, "load_model_config", lambda **_: config)
    monkeypatch.setattr(factory, "discover_model_entries", lambda **_: {})
    monkeypatch.setattr(factory, "resolve_endpoint", lambda *_, **__: {"request_defaults": {}})
    monkeypatch.setattr(factory, "resolve_model", lambda *_, **__: "served/model")

    selected = factory.create_backend("local", model="alias")

    assert selected.kind == "transport"
    assert selected.backend.name == "openrouter"
    assert selected.backend.endpoint == "local"
    assert selected.model == "served/model"


# ---------------------------------------------------------------------------
# Migration step 9: a caller that dispatches transport entries only (the
# workflow-kit openrouter node) resolves ids through create_transport_backend,
# which refuses a harness entry as an EndpointResolveError -- describe() reads
# that as "unroutable here" and skips it silently.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from llm_scripting_kit.models import DEFAULT_MODEL_CONFIG, EndpointResolveError  # noqa: E402


@pytest.fixture
def shipped(monkeypatch):
    monkeypatch.setattr(factory, "load_model_config", lambda **_: DEFAULT_MODEL_CONFIG)


def test_transport_backend_resolves_a_shipped_openrouter_entry(shipped):
    selected = factory.create_transport_backend("or-qwen")
    assert selected.kind == "transport"
    assert selected.backend.name == "openrouter"
    assert selected.model == "qwen/qwen3-32b"


def test_transport_backend_refuses_a_harness_entry(shipped):
    with pytest.raises(EndpointResolveError, match="transport"):
        factory.create_transport_backend("sol")


def test_transport_backend_keeps_the_per_entry_override_and_cheap(shipped):
    assert factory.create_transport_backend("openrouter", cheap=True).model == "qwen/qwen3-32b"
    assert factory.create_transport_backend("openrouter", model="gpt-mini").model == "openai/gpt-4o-mini"


def test_transport_backend_reports_an_unknown_alias_as_unresolvable(shipped):
    with pytest.raises(EndpointResolveError, match="nope"):
        factory.create_transport_backend("openrouter", model="nope")


def test_the_floor_propagates_through_a_transport_only_describe(shipped, monkeypatch):
    from llm_scripting_kit import declaration
    from llm_scripting_kit.reachability import STATUS_UNREACHABLE, Reachability

    with pytest.raises(declaration.NoUsableRoutingTarget) as caught:
        declaration.describe(
            ["sol", "or-qwen", "typo"], caller="process",
            backend_factory=factory.create_transport_backend,
            reachability_cache={"or-qwen": Reachability(STATUS_UNREACHABLE, "models-probe", "down")},
        )
    assert [(d.id, d.disposition) for d in caught.value.dispositions] == [
        ("sol", declaration.DISPOSITION_UNROUTABLE),
        ("or-qwen", declaration.DISPOSITION_UNREACHABLE),
        ("typo", declaration.DISPOSITION_UNRESOLVED),
    ]
