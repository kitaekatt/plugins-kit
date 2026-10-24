"""The output cap of a transport call, the typed ``max_output_tokens`` row field, and the provider
finish reason on a failed protocol 2 result.

Built on the fake transports of ``test_invoke_protocol`` (no network, no CLI, throwaway
HOME). The OpenAI-SDK-shaped client is replaced so the test chooses the content and the
``finish_reason`` the "provider" returns, and records the request kwargs actually sent.

Precedence under test, for a transport row: the request's ``max_tokens`` > the row's
``max_output_tokens`` > the row's ``request_defaults.extras.max_tokens`` > no cap on
openrouter.ai (any other OpenAI-compatible endpoint keeps 4096). Claude and Codex rows
take no cap.

Red-first record (each group was run with its change reverted): the default tests fail
with ``BackendOptions.max_tokens = 4096``; the row-field tests fail without the field in
``project_models``/``invoke``; the finish-reason tests fail when ``dispatch.invoke`` stops
copying the reason into the failure object; the explicit-beats-extras test fails when the
extras key is left in the request body.
"""

from __future__ import annotations

import copy
import types

import pytest

from llm_kit.adapters import backends as backends_mod
from llm_kit.adapters.capabilities import OPENROUTER_CAPABILITIES
from llm_kit.completion.types import BackendOptions
from llm_kit.registry import catalog as catalog_mod
from llm_kit.registry import project_models as pm
from test_cli_harness import home, write  # noqa: F401 -- fixture the world fixture depends on
from test_invoke_protocol import _DOC, _SCHEMA, allow, call, world  # noqa: F401 -- world is a fixture


def _rewrite(world, row="deepseek", **fields):
    """Rewrite the project file with ``fields`` merged into one catalogue row."""
    document = copy.deepcopy(_DOC)
    next(r for r in document["catalog"] if r["id"] == row).update(fields)
    pm.write_project_models(document, project_root=str(world.root))


def _extras(max_tokens):
    return {"request_defaults": {"extras": {"provider": {"sort": "latency"}, "max_tokens": max_tokens}}}


def _client_returning(world, *, content, finish_reason, completion_tokens=7):
    """Replace the world's HTTP client with one that answers ``content`` / ``finish_reason``."""

    def client():
        def create(**kwargs):
            world.http.append(kwargs)
            usage = types.SimpleNamespace(prompt_tokens=11, completion_tokens=completion_tokens, prompt_tokens_details={})
            message = types.SimpleNamespace(content=content, reasoning_content=None)
            choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
            return types.SimpleNamespace(choices=[choice], usage=usage, model=world.http_model)

        result = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        result.with_options = lambda **_: result
        return result

    world.client = client


# --- no library cap on openrouter.ai ------------------------------------------------------------


def test_the_library_default_is_no_cap():
    assert BackendOptions().max_tokens is None
    assert OPENROUTER_CAPABILITIES.params["max_tokens"].default is None


def test_an_openrouter_call_with_no_cap_anywhere_sends_no_max_tokens(world, capsys):
    allow()
    code, doc, err = call(world, capsys, selection="art")
    assert code == 0, (doc, err)
    assert "max_tokens" not in world.http[0]
    assert "max_tokens" not in world.http[0].get("extra_body", {})


def _local_backend(options):
    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        message = types.SimpleNamespace(content="ok", reasoning_content=None)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=message, finish_reason="stop")],
            usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1, prompt_tokens_details={}),
            model="m",
        )

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    client.with_options = lambda **_: client
    return client, sent


_ENDPOINTS = """\
version: 1
default: orr
models:
  orr: {base_url: https://openrouter.ai/api/v1, model: a/b, key_env: OPENROUTER_API_KEY}
  eu: {base_url: https://eu.openrouter.ai/api/v1, model: a/b, key_env: OPENROUTER_API_KEY}
  local: {base_url: "http://127.0.0.1:8080/v1", model: a/b}
  lookalike: {base_url: https://notopenrouter.ai.example.invalid/v1, model: a/b}
"""


@pytest.fixture()
def endpoints(home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _ENDPOINTS)))
    monkeypatch.setenv("OPENROUTER_API_KEY", "unused")


@pytest.mark.parametrize("endpoint,expected", [("orr", None), ("eu", None), ("local", 4096), ("lookalike", 4096)])
def test_only_openrouter_goes_uncapped_other_endpoints_keep_a_finite_cap(endpoints, endpoint, expected):
    client, sent = _local_backend(None)
    backends_mod.OpenRouterBackend(endpoint=endpoint, client=client).complete(
        "s", "u", model="a/b", options=BackendOptions()
    )
    assert ("max_tokens" in sent[0]) == (expected is not None)
    assert sent[0].get("max_tokens") == expected


def test_an_unresolvable_endpoint_keeps_the_finite_cap():
    backend = backends_mod.OpenRouterBackend(endpoint="nowhere", client=object())
    backend._resolved_cache = backends_mod.OpenRouterBackend._UNRESOLVED  # noqa: SLF001
    assert backend._default_max_tokens() == 4096  # noqa: SLF001


def test_an_explicit_cap_reaches_a_non_openrouter_endpoint_unchanged(endpoints):
    client, sent = _local_backend(None)
    backends_mod.OpenRouterBackend(endpoint="local", client=client).complete(
        "s", "u", model="a/b", options=BackendOptions(max_tokens=99999)
    )
    assert sent[0]["max_tokens"] == 99999


# --- precedence --------------------------------------------------------------------------------


def test_the_row_field_is_the_cap_when_the_request_names_none(world, capsys):
    allow()
    _rewrite(world, max_output_tokens=65536)
    code, doc, err = call(world, capsys, selection="art")
    assert code == 0, (doc, err)
    assert world.http[0]["max_tokens"] == 65536


@pytest.mark.parametrize("explicit", [1000, 4096, 65536, 200000])
def test_an_explicit_request_value_beats_the_row_field_and_is_never_clamped(world, capsys, explicit):
    allow()
    _rewrite(world, max_output_tokens=65536)
    code, doc, err = call(world, capsys, selection="art", max_tokens=explicit)
    assert code == 0, (doc, err)
    assert world.http[0]["max_tokens"] == explicit


def test_an_explicit_request_value_is_sent_unchanged_with_no_row_field(world, capsys):
    allow()
    code, doc, err = call(world, capsys, selection="art", max_tokens=32768)
    assert code == 0, (doc, err)
    assert world.http[0]["max_tokens"] == 32768


def test_the_extras_bridge_is_honoured_as_the_cap_and_leaves_the_request_body(world, capsys):
    """request_defaults.extras.max_tokens is honoured as the cap; the key is removed from the body's extra fields."""
    allow()
    _rewrite(world, **_extras(12345))
    code, doc, err = call(world, capsys, selection="art")
    assert code == 0, (doc, err)
    assert world.http[0]["max_tokens"] == 12345
    assert "max_tokens" not in world.http[0]["extra_body"]
    assert world.http[0]["extra_body"]["provider"] == {"sort": "latency"}


def test_the_row_field_beats_the_extras_bridge(world, capsys):
    allow()
    _rewrite(world, max_output_tokens=50000, **_extras(12345))
    code, doc, err = call(world, capsys, selection="art")
    assert code == 0, (doc, err)
    assert world.http[0]["max_tokens"] == 50000
    assert "max_tokens" not in world.http[0]["extra_body"]


def test_an_explicit_request_value_beats_the_extras_bridge(world, capsys):
    """Red on revert: leave extras.max_tokens in the body and the SDK's body merge overrides the explicit value."""
    allow()
    _rewrite(world, **_extras(12345))
    code, doc, err = call(world, capsys, selection="art", max_tokens=777)
    assert code == 0, (doc, err)
    assert world.http[0]["max_tokens"] == 777
    assert "max_tokens" not in world.http[0]["extra_body"]


@pytest.mark.parametrize("bad", [0, -5, "big", True, 1.5])
def test_a_bad_extras_bridge_value_is_refused_naming_the_row(world, capsys, bad):
    allow()
    _rewrite(world, **_extras(bad))
    code, doc, err = call(world, capsys, selection="art")
    assert code == 4 and doc is None and err["error"]["kind"] == "configuration"
    assert "'deepseek'" in err["error"]["message"] and "extras.max_tokens" in err["error"]["message"]
    assert world.runner_calls == 0


# --- the typed field: validation and the catalogue ---------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, "big", True, 1.5, None, [8]])
def test_a_bad_row_value_fails_loudly_naming_the_field_and_the_model(world, bad):
    document = copy.deepcopy(_DOC)
    document["catalog"][2]["max_output_tokens"] = bad
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.validate_project_models(document, project_root=str(world.root))
    message = str(caught.value)
    assert "catalog[2].max_output_tokens" in message and "deepseek/deepseek-v4.1-flash" in message


def test_a_valid_row_value_is_stored_in_canonical_order_and_round_trips(world):
    _rewrite(world, max_output_tokens=65536, **_extras(1))
    stored = pm.read_project_models(project_root=str(world.root))
    row = next(r for r in stored.document["catalog"] if r["id"] == "deepseek")
    assert list(row) == ["id", "label", "endpoint", "model", "efforts", "max_output_tokens", "request_defaults"]
    assert row["max_output_tokens"] == 65536
    assert b"max_output_tokens: 65536" in pm.render_yaml(stored.document)


def _build(world):
    stored = pm.read_project_models(project_root=str(world.root))
    return catalog_mod.build_catalog(
        stored,
        project_root=str(world.root),
        modes_by_adapter={"openrouter": ["completion"], "claude-cli": ["completion"], "codex-cli": ["completion"]},
    )


def test_the_catalogue_exposes_the_declared_cap_and_omits_it_when_undeclared(world):
    _rewrite(world, max_output_tokens=65536)
    rows = {r["id"]: r for r in _build(world)["models"]}
    assert rows["deepseek"]["max_output_tokens"] == 65536
    assert "max_output_tokens" not in rows["luna"] and "max_output_tokens" not in rows["sonnet"]


@pytest.mark.parametrize("row", ["luna", "sonnet"])
def test_a_harness_row_may_declare_the_field_and_the_catalogue_says_it_is_not_applied(world, row):
    _rewrite(world, row=row, max_output_tokens=9999)
    built = _build(world)
    shown = next(r for r in built["models"] if r["id"] == row)
    assert shown["max_output_tokens"] == 9999 and shown["kind"] == "harness"
    assert any(row in note and "not applied" in note for note in built["notes"])


# --- claude and codex keep their native behaviour -----------------------------------------------


def test_a_codex_row_with_the_field_and_a_request_cap_sends_neither(world, capsys):
    allow()
    _rewrite(world, row="luna", max_output_tokens=9999)
    code, doc, err = call(world, capsys, model="luna", max_tokens=777)
    assert code == 0, (doc, err)
    argv = " ".join(world.codex[-1])
    assert "9999" not in argv and "777" not in argv and "max_tokens" not in argv
    assert world.http == []


def test_a_claude_row_with_the_field_sends_no_cap(world, capsys):
    allow()
    _rewrite(world, row="sonnet", max_output_tokens=9999)
    code, doc, err = call(world, capsys, selection="chat")
    assert code == 0, (doc, err)
    argv = " ".join(world.claude[-1])
    assert "9999" not in argv


# --- the provider finish reason on a failure ---------------------------------------------------


def test_a_truncated_answer_fails_the_contract_and_carries_finish_reason_length(world, capsys):
    allow()
    _client_returning(world, content='{"summary": "cut off mid-str', finish_reason="length", completion_tokens=16384)
    code, doc, err = call(world, capsys, selection="art", output_schema=_SCHEMA)
    assert code == 1, (doc, err)
    assert doc["status"] == "failed"
    assert doc["error"]["code"] == "output-contract-violation"
    assert doc["error"]["finish_reason"] == "length"
    assert doc["usage"]["output_tokens"] == 16384
    assert doc["output"]["text"] == '{"summary": "cut off mid-str'


def test_malformed_json_that_ended_on_its_own_carries_finish_reason_stop(world, capsys):
    allow()
    _client_returning(world, content="this is not json at all", finish_reason="stop")
    code, doc, err = call(world, capsys, selection="art", output_schema=_SCHEMA)
    assert code == 1, (doc, err)
    assert doc["error"]["code"] == "output-contract-violation"
    assert doc["error"]["finish_reason"] == "stop"


def test_an_empty_answer_that_exhausted_the_budget_carries_finish_reason_and_tokens(world, capsys):
    """Reasoning spent the whole cap: the transport raises EmptyCompletionError, which holds the reason."""
    allow()
    _client_returning(world, content="", finish_reason="length", completion_tokens=16384)
    code, doc, err = call(world, capsys, selection="art")
    assert code == 1, (doc, err)
    assert doc["error"]["finish_reason"] == "length"
    assert doc["usage"]["output_tokens"] == 16384


def test_a_failure_before_the_provider_answered_has_no_finish_reason(world, capsys):
    allow()
    world.http_error = RuntimeError("the model exploded")
    code, doc, err = call(world, capsys, selection="art")
    assert code == 1, (doc, err)
    assert doc["error"] == {"code": "execution", "message": "the model exploded"}


def test_a_completed_result_has_no_error_block(world, capsys):
    allow()
    code, doc, err = call(world, capsys, selection="art")
    assert code == 0 and "error" not in doc


# --- jobs ------------------------------------------------------------------------------------------


def _job(tmp_path, **options):
    from llm_kit.jobs import model as jobs_model

    path = tmp_path / "job.json"
    doc = {"id": "j", "prompt": {"user": "x"}, "models": ["openrouter"], "directory": str(tmp_path),
           "contract": {"command": "true"}}
    if options:
        doc["options"] = options
    path.write_text(__import__("json").dumps({"jobs": [doc]}), encoding="utf-8")
    return jobs_model.load_job_file(path).jobs[0]


def test_a_job_that_names_no_max_tokens_carries_none_and_a_named_one_is_kept(tmp_path):
    """Red on revert: put the old 4096 default back in jobs/run.py and the first assertion fails."""
    from llm_kit.jobs import run as jobs_run

    selection = types.SimpleNamespace(endpoint="openrouter", effort=None)
    build = lambda job: jobs_run._backend_options("r", job, selection, tmp_path, 30.0, None)  # noqa: E731, SLF001
    assert build(_job(tmp_path)).max_tokens is None
    assert build(_job(tmp_path, max_tokens=77)).max_tokens == 77
    assert build(_job(tmp_path, max_tokens=4096)).max_tokens == 4096
