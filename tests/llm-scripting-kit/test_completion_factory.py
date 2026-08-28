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
