"""The content-pipeline runtime's output cap and its spend reservation.

Precedence under test, for a pipeline call through a transport wrapper
(``OpenRouterBackend`` / ``ModelEndpointBackend``): the call's ``options.max_tokens`` > the
selected model's ``max_output_tokens`` (the project model file row for the wrapper's endpoint
and the call's model) > that row's ``request_defaults.extras.max_tokens`` > no cap on
openrouter.ai (any other OpenAI-compatible endpoint keeps 4096). The spend reservation prices
the cap the call carries; with none carried it is priced at the model's ``context_window`` from
the pricing table and the number is never sent as ``max_tokens``.

No network and no process: the OpenAI-SDK-shaped client is a recorder, HOME is a throwaway
directory and the model-endpoints registry is a temporary file.

Red-first record (each group was run with its change reverted): the default tests fail with
``BackendOptions.max_tokens = 4096``; the row tests fail with the resolution in ``call_llm`` and
the wrapper's ``complete`` removed; the key tests fail without the resolution in ``_cache_key_for``;
the reservation tests fail with the unset-cap branch of ``_spend_reservation_usd`` removed.
"""

from __future__ import annotations

import copy
import types

import pytest

from llm_kit.adapters import backends as adapter_backends
from llm_kit.pipeline.runtime import backends as pipeline_backends
from llm_kit.pipeline.runtime import platform
from llm_kit.pipeline.runtime.backends import ModelEndpointBackend, OpenRouterBackend
from llm_kit.pipeline.runtime.platform import (
    UNDECLARED_RESERVATION_OUTPUT_TOKENS,
    BackendOptions,
    ConfigurationError,
    call_llm,
    estimate_cost,
    estimate_request_tokens,
)
from llm_kit.registry import project_models as pm
from test_cli_harness import home, write  # noqa: F401 -- fixture the project fixture depends on

_ENDPOINTS = """\
version: 1
default: orr
models:
  orr: {base_url: https://openrouter.ai/api/v1, model: a/b, key_env: OPENROUTER_API_KEY}
  local: {base_url: "http://127.0.0.1:8080/v1", model: a/b}
"""

_DOC = {
    "version": 1,
    "catalog": [
        {"id": "big", "label": "Big", "endpoint": "orr", "model": "a/big", "max_output_tokens": 65536},
        {"id": "bare", "label": "Bare", "endpoint": "orr", "model": "a/bare"},
        {
            "id": "bridged", "label": "Bridged", "endpoint": "orr", "model": "a/bridged",
            "request_defaults": {"extras": {"max_tokens": 777}},
        },
        {
            "id": "both", "label": "Both", "endpoint": "orr", "model": "a/both", "max_output_tokens": 5000,
            "request_defaults": {"extras": {"max_tokens": 777}},
        },
        {"id": "loc", "label": "Local", "endpoint": "local", "model": "a/loc", "max_output_tokens": 2048},
        {"id": "locbare", "label": "Local bare", "endpoint": "local", "model": "a/locbare"},
    ],
    "selections": {},
}

# a model priced per million tokens: 1 input, 10 output
_PRICING = {
    m: {"input": 1.0, "output": 10.0}
    for m in ("a/big", "a/bare", "a/bridged", "a/both", "a/loc", "a/locbare", "a/none")
}


class Project:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.monkeypatch = monkeypatch
        self.sent: list = []

    def client(self, finish_reason="stop"):
        def create(**kwargs):
            self.sent.append(kwargs)
            message = types.SimpleNamespace(content="ok", reasoning_content=None)
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=message, finish_reason=finish_reason)],
                usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1, prompt_tokens_details={}),
                model="m",
            )

        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        client.with_options = lambda **_: client
        return client

    def backend(self, endpoint="orr"):
        return OpenRouterBackend(endpoint=endpoint, project_root=self.root, client=self.client())

    def rewrite(self, document):
        pm.write_project_models(document, project_root=str(self.root))

    def edit_row(self, row_id, **fields):
        document = copy.deepcopy(_DOC)
        next(r for r in document["catalog"] if r["id"] == row_id).update(fields)
        self.rewrite(document)


@pytest.fixture()
def project(home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _ENDPOINTS)))
    monkeypatch.setenv("OPENROUTER_API_KEY", "unused")
    root = tmp_path / "proj"
    root.mkdir()
    p = Project(root, monkeypatch)
    p.rewrite(_DOC)
    return p


def _cap_sent(project):
    assert len(project.sent) == 1, project.sent
    return project.sent[0].get("max_tokens", "absent")


def _call(project, backend, model, **kwargs):
    return call_llm(backend, "sys", "user", model=model, **kwargs)


# --- the default is no cap ------------------------------------------------------------------


def test_the_library_default_is_no_cap():
    assert BackendOptions().max_tokens is None


def test_a_call_with_no_cap_anywhere_sends_no_max_tokens_to_openrouter(project):
    _call(project, project.backend(), "a/bare")
    assert _cap_sent(project) == "absent"


def test_a_model_the_file_does_not_list_sends_no_max_tokens_to_openrouter(project):
    _call(project, project.backend(), "a/none")
    assert _cap_sent(project) == "absent"


def test_a_project_without_a_model_file_sends_no_max_tokens_to_openrouter(project):
    (project.root / ".llm-kit" / "models.yaml").unlink()
    _call(project, project.backend(), "a/big")
    assert _cap_sent(project) == "absent"


# --- precedence --------------------------------------------------------------------------------


def test_the_models_max_output_tokens_is_the_default_cap_of_a_stage(project):
    _call(project, project.backend(), "a/big")
    assert _cap_sent(project) == 65536


@pytest.mark.parametrize("explicit", [10, 65536, 1_000_000])
def test_an_explicit_call_value_beats_the_models_cap_and_is_never_clamped(project, explicit):
    _call(project, project.backend(), "a/big", options=BackendOptions(max_tokens=explicit))
    assert _cap_sent(project) == explicit


def test_the_extras_bridge_is_the_cap_when_the_row_has_no_typed_field(project):
    _call(project, project.backend(), "a/bridged")
    assert _cap_sent(project) == 777


def test_the_typed_field_beats_the_extras_bridge(project):
    _call(project, project.backend(), "a/both")
    assert _cap_sent(project) == 5000


def test_an_explicit_value_beats_the_extras_bridge(project):
    _call(project, project.backend(), "a/bridged", options=BackendOptions(max_tokens=42))
    assert _cap_sent(project) == 42


def test_each_model_reads_its_own_row(project):
    backend = project.backend()
    _call(project, backend, "a/big")
    _call(project, backend, "a/bridged")
    assert [s.get("max_tokens") for s in project.sent] == [65536, 777]


def test_a_non_openrouter_endpoint_keeps_4096_without_a_row_and_uses_its_row(project):
    backend = project.backend("local")
    _call(project, backend, "a/locbare")
    _call(project, backend, "a/loc")
    assert [s.get("max_tokens") for s in project.sent] == [4096, 2048]


def test_a_row_on_another_endpoint_does_not_apply(project):
    # a/loc is declared for the local endpoint only
    _call(project, project.backend("orr"), "a/loc")
    assert _cap_sent(project) == "absent"


def test_the_registry_entry_wrapper_reads_the_same_rows(project):
    backend = ModelEndpointBackend(endpoint="local", project_root=project.root, client=project.client())
    _call(project, backend, "a/loc")
    assert _cap_sent(project) == 2048


def test_the_wrapper_applies_the_cap_when_complete_is_called_directly(project):
    project.backend().complete("s", "u", model="a/big")
    assert _cap_sent(project) == 65536


def test_the_declared_non_openrouter_default_is_the_adapters(project):
    assert pipeline_backends.NON_OPENROUTER_DEFAULT_MAX_TOKENS == adapter_backends.NON_OPENROUTER_DEFAULT_MAX_TOKENS


def test_a_cli_wrapper_declares_no_cap_to_resolve():
    assert not hasattr(pipeline_backends.ClaudeCliBackend(), "output_cap_for")
    assert not hasattr(pipeline_backends.CodexCliBackend(), "output_cap_for")


# --- the file is configuration: loud when wrong, followed when edited ------------------------------


def test_rows_that_disagree_about_one_model_refuse_naming_the_rows(project):
    document = copy.deepcopy(_DOC)
    document["catalog"] += [
        {"id": "dup1", "label": "D1", "endpoint": "orr", "model": "a/dup", "max_output_tokens": 100},
        {"id": "dup2", "label": "D2", "endpoint": "orr", "model": "a/dup", "max_output_tokens": 200},
    ]
    project.rewrite(document)
    with pytest.raises(ConfigurationError) as excinfo:
        _call(project, project.backend(), "a/dup")
    assert "dup1" in str(excinfo.value) and "dup2" in str(excinfo.value) and "a/dup" in str(excinfo.value)
    assert project.sent == []


def test_rows_that_agree_about_one_model_are_not_ambiguous(project):
    document = copy.deepcopy(_DOC)
    document["catalog"] += [
        {"id": "same1", "label": "S1", "endpoint": "orr", "model": "a/same", "max_output_tokens": 300},
        {"id": "same2", "label": "S2", "endpoint": "orr", "model": "a/same", "max_output_tokens": 300},
    ]
    project.rewrite(document)
    _call(project, project.backend(), "a/same")
    assert _cap_sent(project) == 300


@pytest.mark.parametrize("bad", [0, -5, "9", True, 1.5])
def test_a_bad_extras_bridge_value_refuses_naming_the_row(project, bad):
    project.edit_row("bridged", request_defaults={"extras": {"max_tokens": bad}})
    with pytest.raises(ConfigurationError) as excinfo:
        _call(project, project.backend(), "a/bridged")
    assert "bridged" in str(excinfo.value) and "max_tokens" in str(excinfo.value)
    assert project.sent == []


def test_a_present_file_that_does_not_validate_fails_loudly(project):
    (project.root / ".llm-kit" / "models.yaml").write_text("version: 1\ncatalog: nope\n", encoding="utf-8")
    with pytest.raises(pm.ProjectModelsError):
        _call(project, project.backend(), "a/big")
    assert project.sent == []


def test_an_edit_to_the_file_is_followed_by_the_next_call(project):
    backend = project.backend()
    _call(project, backend, "a/big")
    project.edit_row("big", max_output_tokens=1234)
    _call(project, backend, "a/big")
    assert [s.get("max_tokens") for s in project.sent] == [65536, 1234]


def test_the_working_directory_is_the_project_root_when_the_wrapper_names_none(project, monkeypatch):
    monkeypatch.chdir(project.root)
    backend = OpenRouterBackend(endpoint="orr", client=project.client())
    _call(project, backend, "a/big")
    assert _cap_sent(project) == 65536


# --- cache key and attempt observer -----------------------------------------------------------------


def test_the_default_options_key_is_distinct_from_an_explicit_4096_and_stable(project):
    backend = project.backend()
    unset = platform._cache_key_for(backend, "s", "u", "a/bare", BackendOptions())
    explicit = platform._cache_key_for(backend, "s", "u", "a/bare", BackendOptions(max_tokens=4096))
    assert unset != explicit
    assert unset == platform._cache_key_for(backend, "s", "u", "a/bare", BackendOptions())
    assert unset == platform.build_cache_key(backend="openrouter", model="a/bare", system="s", user="u")


def test_the_key_states_the_configured_cap_and_so_follows_the_file(project):
    backend = project.backend()
    configured = platform._cache_key_for(backend, "s", "u", "a/big", BackendOptions())
    assert configured == platform._cache_key_for(backend, "s", "u", "a/big", BackendOptions(max_tokens=65536))
    project.edit_row("big", max_output_tokens=1234)
    assert configured != platform._cache_key_for(backend, "s", "u", "a/big", BackendOptions())


def test_a_cached_answer_is_not_reused_after_the_configured_cap_changes(project, tmp_path):
    backend = project.backend()
    cache = tmp_path / "cache"
    _call(project, backend, "a/big", cache_dir=cache)
    again = _call(project, backend, "a/big", cache_dir=cache)
    assert again.from_cache is True and len(project.sent) == 1
    project.edit_row("big", max_output_tokens=1234)
    fresh = _call(project, backend, "a/big", cache_dir=cache)
    assert fresh.from_cache is False and project.sent[-1]["max_tokens"] == 1234


def test_a_registry_entry_off_openrouter_keeps_the_key_of_the_cap_it_sends(project):
    backend = ModelEndpointBackend(endpoint="local", project_root=project.root, client=project.client())
    assert backend.effective_options(BackendOptions()).max_tokens == 4096
    assert backend.effective_options(BackendOptions(max_tokens=7)).max_tokens == 7
    on_openrouter = ModelEndpointBackend(endpoint="orr", project_root=project.root, client=project.client())
    assert on_openrouter.effective_options(BackendOptions()).max_tokens is None


def test_the_attempt_observer_sees_the_resolved_cap(project):
    seen = []
    backend = project.backend()
    _call(project, backend, "a/big", on_attempt=seen.append)
    _call(project, backend, "a/bare", on_attempt=seen.append)
    assert [a.max_tokens for a in seen] == [65536, None]


# --- the spend reservation -----------------------------------------------------------------------


class Ledger:
    """A SpendGuard that records what it was asked to reserve."""

    def __init__(self):
        self.reserved: list = []
        self.settled: list = []

    def reserve(self, amount_usd, *, scope="", identifier="", model="", ttl_s=None):
        self.reserved.append(amount_usd)
        return object()

    def settle(self, reservation, cost_usd):
        self.settled.append(cost_usd)

    def release(self, reservation):  # pragma: no cover -- call_llm never releases
        raise AssertionError("release")

    def check_halted(self, *, identifier=""):  # pragma: no cover
        pass


def _priced(model, out_tokens):
    return estimate_cost(model, estimate_request_tokens("sys", "user"), out_tokens, 0, pricing=_PRICING)


def test_a_configured_cap_is_what_is_reserved(project):
    ledger = Ledger()
    _call(project, project.backend(), "a/big", pricing=_PRICING, spend=ledger)
    assert ledger.reserved == [pytest.approx(_priced("a/big", 65536))]


def test_an_explicit_cap_is_what_is_reserved(project):
    ledger = Ledger()
    _call(project, project.backend(), "a/big", options=BackendOptions(max_tokens=100), pricing=_PRICING, spend=ledger)
    assert ledger.reserved == [pytest.approx(_priced("a/big", 100))]


def test_an_endpoint_that_sends_4096_reserves_4096(project):
    ledger = Ledger()
    _call(project, project.backend("local"), "a/locbare", pricing=_PRICING, spend=ledger)
    assert ledger.reserved == [pytest.approx(_priced("a/locbare", 4096))]
    assert _cap_sent(project) == 4096


def test_an_uncapped_openrouter_call_is_reserved_at_the_context_window_and_sends_no_cap(project):
    pricing = copy.deepcopy(_PRICING)
    pricing["a/bare"]["context_window"] = 200_000
    ledger = Ledger()
    _call(project, project.backend(), "a/bare", pricing=pricing, spend=ledger)
    assert ledger.reserved == [pytest.approx(_priced("a/bare", 200_000))]
    assert _cap_sent(project) == "absent"  # the reservation bound is priced, never sent


def test_an_uncapped_openrouter_call_with_no_known_bound_is_refused_before_the_provider(project):
    ledger = Ledger()
    with pytest.raises(ValueError) as excinfo:
        _call(project, project.backend(), "a/bare", pricing=_PRICING, spend=ledger)
    message = str(excinfo.value)
    for remedy in ("options.max_tokens", "max_output_tokens", "context_window", "spend_reserve_usd"):
        assert remedy in message
    assert project.sent == [] and ledger.reserved == []


@pytest.mark.parametrize("bad", [0, -1, "200000", True])
def test_a_bad_context_window_is_refused(project, bad):
    pricing = copy.deepcopy(_PRICING)
    pricing["a/bare"]["context_window"] = bad
    with pytest.raises(ValueError, match="context_window"):
        _call(project, project.backend(), "a/bare", pricing=pricing, spend=Ledger())
    assert project.sent == []


def test_an_explicit_reservation_needs_no_bound(project):
    ledger = Ledger()
    _call(project, project.backend(), "a/bare", pricing=_PRICING, spend=ledger, spend_reserve_usd=2.5)
    assert ledger.reserved == [2.5] and _cap_sent(project) == "absent"


def test_a_cache_hit_reserves_nothing_so_it_needs_no_bound(project, tmp_path):
    cache = tmp_path / "cache"
    _call(project, project.backend(), "a/bare", cache_dir=cache)  # no spend: stored
    ledger = Ledger()
    hit = _call(project, project.backend(), "a/bare", cache_dir=cache, pricing=_PRICING, spend=ledger)
    assert hit.from_cache is True and ledger.reserved == []


def test_a_backend_that_declares_nothing_keeps_the_pre_existing_reservation_default():
    ledger = Ledger()
    backend = pipeline_backends.MockBackend(responses=["ok"])
    call_llm(backend, "sys", "user", model="a/none", pricing=_PRICING, spend=ledger)
    assert ledger.reserved == [pytest.approx(_priced("a/none", UNDECLARED_RESERVATION_OUTPUT_TOKENS))]
    assert UNDECLARED_RESERVATION_OUTPUT_TOKENS == 4096


def test_a_backend_cap_that_is_not_a_positive_integer_is_refused():
    backend = pipeline_backends.MockBackend(responses=["ok"])
    backend.output_cap_for = lambda model: 0
    with pytest.raises(ConfigurationError, match="output_cap_for"):
        call_llm(backend, "sys", "user", model="a/none")
