"""Measured exact-model effort menus and their enforcement.

The menu is a property of one model reached over one route
(``llm_kit.registry.model_profiles``). One validator, ``check_effort``, serves
catalogue writes, selection resolution, ``CodexAdapter`` and ``CodexCliBackend``,
so these tests drive each of the four and assert that none can bypass the
exact-model menu. A profile is shipped data: nothing reads a user cache at
runtime (``test_model_catalog.py`` blocks the cache and the home directory).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from llm_kit.adapters import codex_backend, harness_adapters
from llm_kit.completion.types import BackendOptions
from llm_kit.registry import model_profiles as mp
from llm_kit.registry import project_models as pm
from llm_kit.registry.effort import CODEX_EFFORT_ORDER, HarnessAdapterError
from llm_kit.registry.model_endpoints import HARNESS_KIND, EndpointEntry
from llm_kit.registry.models import DEFAULT_MODEL_CONFIG
from test_cli_harness import home, write  # noqa: F401 -- home is a fixture

LOW_TO_MAX = ("low", "medium", "high", "xhigh", "max")
LOW_TO_ULTRA = LOW_TO_MAX + ("ultra",)


# --- the shipped data -----------------------------------------------------------


def test_every_profile_cites_its_source_and_day():
    assert mp.PROFILES
    for profile in mp.PROFILES:
        assert profile.source in (mp.SOURCE_CODEX, mp.SOURCE_CLAUDE, mp.SOURCE_OPENROUTER), profile
        assert profile.measured_on == "2026-10-23", profile
        assert profile.route in mp.ROUTES
        scale = [mp.EFFORT_SCALE.index(e) for e in profile.efforts]
        assert scale == sorted(set(scale)), profile


@pytest.mark.parametrize(
    "route,model,menu",
    [
        ("codex", "gpt-6.1-sol", LOW_TO_ULTRA),
        ("codex", "gpt-6-sol", LOW_TO_ULTRA),
        ("codex", "gpt-6-astra", LOW_TO_ULTRA),
        ("codex", "gpt-6-luna", LOW_TO_MAX),
        ("claude", "claude-sonnet-5-5", LOW_TO_MAX),
        ("claude", "claude-opus-5-5", LOW_TO_MAX),
        ("claude", "claude-fable-5-1", LOW_TO_MAX),
        ("claude", "claude-opus-4-6", ("low", "medium", "high", "max")),
        ("openrouter", "deepseek/deepseek-v4.1-flash", ("none", "low", "high", "max")),
        ("openrouter", "google/gemini-3.8-flash", ("low", "medium", "high")),
        ("openrouter", "z-ai/glm-5.3", ("low", "high", "max")),
    ],
)
def test_the_measured_menus(route, model, menu):
    assert mp.exact_effort_menu(route, model) == menu


def test_none_is_listed_only_where_the_vendor_lists_it():
    with_none = [(p.route, p.model) for p in mp.PROFILES if "none" in p.efforts]
    assert with_none == [("openrouter", "deepseek/deepseek-v4.1-flash")]


def test_an_unmeasured_model_has_no_exact_menu():
    assert mp.exact_effort_menu("codex", "gpt-7-imaginary") is None
    assert mp.exact_effort_menu("claude", "opus") is None  # an alias tracks upgrades; no exact model
    assert mp.exact_effort_menu("openrouter", "deepseek/deepseek-v9") is None
    assert mp.exact_effort_menu(None, "gpt-6-luna") is None
    # the route is part of the key: a codex id says nothing about a claude route
    assert mp.exact_effort_menu("claude", "gpt-6-luna") is None


def test_shipped_codex_defaults_name_measured_current_models():
    endpoints = DEFAULT_MODEL_CONFIG["endpoints"]
    assert endpoints["sol"]["model"] == "gpt-6.1-sol"
    assert endpoints["luna"]["model"] == "gpt-6-luna"
    for name, entry in endpoints.items():
        if entry.get("harness") == "codex":
            profile = mp.profile_for("codex", entry["model"])
            assert profile is not None, f"shipped {name} names a model with no measured profile"
            assert entry["effort"] in profile.efforts


def test_a_profile_with_an_off_scale_or_unordered_effort_fails_loudly():
    with pytest.raises(ValueError, match="effort scale"):
        mp.ModelProfile("codex", "m", ("low", "bogus"), mp.SOURCE_CODEX, "2026-10-23")
    with pytest.raises(ValueError, match="low to high"):
        mp.ModelProfile("codex", "m", ("high", "low"), mp.SOURCE_CODEX, "2026-10-23")


# --- the one validator -----------------------------------------------------------


def test_check_effort_semantics():
    ok = dict(route="codex", model="gpt-6-luna", fallback=CODEX_EFFORT_ORDER, subject="codex")
    mp.check_effort(None, **ok)  # null omits
    mp.check_effort("max", **ok)
    with pytest.raises(HarnessAdapterError, match="gpt-6-luna.*codex-models-cache.*accepted efforts: low\\|medium"):
        mp.check_effort("ultra", **ok)
    with pytest.raises(HarnessAdapterError, match="omit the effort"):
        mp.check_effort("none", **ok)
    mp.check_effort("ultra", **{**ok, "model": "gpt-6.1-sol"})
    # no profile: the harness-wide fallback, in the wording the adapter always used
    with pytest.raises(HarnessAdapterError) as caught:
        mp.check_effort("ultra", **{**ok, "model": "gpt-x"})
    assert str(caught.value) == "codex does not accept effort 'ultra'; accepted efforts: high|low|max|medium|xhigh"
    mp.check_effort("anything", **{**ok, "model": "gpt-x", "fallback": None})  # provider-defined menu


def test_explicit_none_passes_only_where_the_menu_lists_it():
    mp.check_effort("none", route="openrouter", model="deepseek/deepseek-v4.1-flash", fallback=None, subject="x")
    for route, model in (("codex", "gpt-6.1-sol"), ("claude", "claude-opus-5-5")):
        with pytest.raises(HarnessAdapterError, match="does not accept effort 'none'"):
            mp.check_effort("none", route=route, model=model, fallback=None, subject="x")


# --- both Codex paths enforce the exact menu --------------------------------------


def _entry(model, **kw):
    return EndpointEntry(id="h", base_url=None, model=model, kind=HARNESS_KIND, harness="codex", **kw)


@pytest.mark.parametrize("model,effort", [("gpt-6-luna", "ultra"), ("gpt-6-luna", "none"), ("gpt-6.1-sol", "none")])
def test_codex_adapter_refuses_an_effort_outside_the_exact_model_menu(tmp_path, model, effort):
    adapter = harness_adapters.CodexAdapter(argv_prefix=("codex",))
    with pytest.raises(HarnessAdapterError, match="does not accept effort"):
        adapter.build_argv(_entry(model), tmp_path, prompt="p", effort=effort)
    # the registry entry's own default is held to the same menu
    with pytest.raises(HarnessAdapterError, match="does not accept effort"):
        adapter.build_argv(_entry(model, effort=effort), tmp_path, prompt="p")


def test_codex_adapter_sends_an_effort_the_exact_menu_lists(tmp_path):
    adapter = harness_adapters.CodexAdapter(argv_prefix=("codex",))
    argv = adapter.build_argv(_entry("gpt-6.1-sol"), tmp_path, prompt="p", effort="ultra")
    assert "model_reasoning_effort=ultra" in argv
    argv = adapter.build_argv(_entry("gpt-6-luna"), tmp_path, prompt="p", effort="max")
    assert "model_reasoning_effort=max" in argv
    # null omits
    assert not any("model_reasoning_effort" in part for part in adapter.build_argv(_entry("gpt-6-luna"), tmp_path, prompt="p"))
    # an unmeasured model keeps the harness-wide menu it always had
    assert "model_reasoning_effort=high" in adapter.build_argv(_entry("gpt-x"), tmp_path, prompt="p", effort="high")
    with pytest.raises(HarnessAdapterError, match="accepted efforts: high\\|low\\|max\\|medium\\|xhigh"):
        adapter.build_argv(_entry("gpt-x"), tmp_path, prompt="p", effort="ultra")


class _Runner:
    """A stub codex runner: records calls and writes the ``-o`` file."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(list(cmd))
        Path(cmd[cmd.index("-o") + 1]).write_text("answer", encoding="utf-8")
        return "", "", 0


def _backend(runner):
    return codex_backend.CodexCliBackend(argv_prefix=("codex",), runner=runner)


@pytest.mark.parametrize("model,effort", [("gpt-6-luna", "ultra"), ("gpt-6-luna", "none"), ("gpt-6.1-sol", "none")])
def test_codex_backend_refuses_an_effort_outside_the_exact_model_menu(tmp_path, model, effort):
    runner = _Runner()
    with pytest.raises(HarnessAdapterError, match="does not accept effort"):
        _backend(runner).complete("s", "u", model=model, options=BackendOptions(effort=effort, cwd=tmp_path))
    assert runner.calls == []  # refused before any argv, temp file or spawn


def test_codex_backend_sends_an_effort_the_exact_menu_lists(tmp_path):
    runner = _Runner()
    _backend(runner).complete("s", "u", model="gpt-6.1-sol", options=BackendOptions(effort="ultra", cwd=tmp_path))
    assert "model_reasoning_effort=ultra" in runner.calls[0]
    runner = _Runner()
    _backend(runner).complete("s", "u", model="gpt-6-luna", options=BackendOptions(cwd=tmp_path))
    assert not any("model_reasoning_effort" in part for part in runner.calls[0])  # null omits


def test_codex_backend_without_a_model_id_keeps_the_harness_wide_menu(tmp_path):
    runner = _Runner()
    with pytest.raises(HarnessAdapterError):
        _backend(runner).complete("s", "u", model="", options=BackendOptions(effort="ultra", cwd=tmp_path))
    _backend(runner).complete("s", "u", model="", options=BackendOptions(effort="high", cwd=tmp_path))
    assert runner.calls


# --- catalogue writes and selections use the same menu ----------------------------

_REGISTRY = """\
default: or-deepseek
models:
  or-deepseek: {base_url: https://openrouter.ai/api/v1, model: deepseek/deepseek-v4.1-flash, effort_style: top-level, key_env: OPENROUTER_API_KEY}
  or-other: {base_url: https://openrouter.ai/api/v1, model: deepseek/not-measured, effort_style: top-level, key_env: OPENROUTER_API_KEY}
  astra: {harness: codex, model: gpt-6-astra, tier: 4, family: openai}
"""


@pytest.fixture()
def project(home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _REGISTRY)))
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _doc():
    return {
        "version": 1,
        "catalog": [
            {"id": "luna", "label": "Luna", "endpoint": "luna", "model": "gpt-6-luna"},
            {"id": "sol", "label": "Sol", "endpoint": "sol", "model": "gpt-6.1-sol",
             "efforts": ["low", "high", "ultra"]},
            {"id": "flash", "label": "DeepSeek Flash", "endpoint": "or-deepseek",
             "model": "deepseek/deepseek-v4.1-flash", "efforts": ["none", "low", "high"]},
        ],
        "selections": {
            "a": {"model": "luna", "effort": "max"},
            "b": {"model": "sol", "effort": "ultra"},
            "c": {"model": "flash", "effort": "none"},
            "d": {"model": "flash", "effort": None},
        },
    }


def test_a_project_menu_is_a_subset_of_the_exact_model_menu(project):
    result = pm.write_project_models(_doc(), project_root=str(project))
    assert result.catalog_rows == 3
    stored = pm.read_project_models(project_root=str(project)).document
    assert stored["catalog"][1]["efforts"] == ["low", "high", "ultra"]


@pytest.mark.parametrize(
    "edit,message",
    [
        (lambda d: d["catalog"][0].update(efforts=["low", "ultra"]), "'ultra' is not in the library menu"),
        (lambda d: d["catalog"][1].update(efforts=["none", "low"]), "'none' is not in the library menu"),
        (lambda d: d["catalog"][2].update(efforts=["low", "medium"]), "'medium' is not in the library menu"),
        (lambda d: d["catalog"][2].update(efforts=["high", "low"]), "library order"),
        (lambda d: d["selections"]["a"].update(effort="ultra"), "'ultra' is not accepted for 'luna'"),
        (lambda d: d["selections"]["b"].update(effort="none"), "'none' is not accepted for 'sol'"),
        (lambda d: d["selections"]["c"].update(effort="max"), "'max' is not accepted for 'flash'"),
    ],
)
def test_widening_a_menu_is_refused_before_anything_is_written(project, edit, message):
    document = copy.deepcopy(_doc())
    edit(document)
    with pytest.raises(pm.ProjectModelsError, match=message):
        pm.write_project_models(document, project_root=str(project))
    assert not (project / ".llm-kit").exists()


def test_explicit_none_is_accepted_only_for_the_model_whose_menu_has_it(project):
    pm.validate_project_models(_doc(), project_root=str(project))
    bad = _doc()
    bad["selections"]["a"]["effort"] = "none"
    with pytest.raises(pm.ProjectModelsError, match="'none' is not accepted for 'luna'"):
        pm.validate_project_models(bad, project_root=str(project))
    # the consumer word `off` is never translated into none
    bad["selections"]["a"]["effort"] = "off"
    with pytest.raises(pm.ProjectModelsError, match="'off' is not accepted"):
        pm.validate_project_models(bad, project_root=str(project))


def test_null_effort_omits_and_needs_no_menu(project):
    document = {
        "version": 1,
        "catalog": [{"id": "x", "label": "X", "endpoint": "or-other", "model": "deepseek/not-measured"}],
        "selections": {"k": {"model": "x", "effort": None}},
    }
    pm.validate_project_models(document, project_root=str(project))


def test_an_openrouter_row_can_now_declare_efforts(project):
    """The interim limitation is lifted: an openrouter.ai model with a measured menu declares efforts."""
    document = {
        "version": 1,
        "catalog": [{"id": "f", "label": "F", "endpoint": "or-deepseek",
                     "model": "deepseek/deepseek-v4.1-flash", "efforts": ["none", "low", "high", "max"]}],
        "selections": {"k": {"model": "f", "effort": "none"}},
    }
    pm.validate_project_models(document, project_root=str(project))
    # an openrouter model nobody measured still cannot invent a menu
    unmeasured = copy.deepcopy(document)
    unmeasured["catalog"][0].update(endpoint="or-other", model="deepseek/not-measured")
    # (or-other resolves a top-level style, so the style menu applies: low|medium|high)
    unmeasured["catalog"][0]["efforts"] = ["none"]
    with pytest.raises(pm.ProjectModelsError, match="'none' is not in the library menu"):
        pm.validate_project_models(unmeasured, project_root=str(project))


def test_an_unmeasured_harness_model_declares_no_efforts(project):
    document = {
        "version": 1,
        "catalog": [{"id": "x", "label": "X", "endpoint": "sol", "model": "gpt-7-imaginary", "efforts": ["low"]}],
    }
    with pytest.raises(pm.ProjectModelsError, match="no effort menu for endpoint 'sol' model 'gpt-7-imaginary'"):
        pm.validate_project_models(document, project_root=str(project))


def test_cli_write_refuses_a_widened_menu_and_leaves_the_file(project, capsys, monkeypatch):
    import llm_kit.cli as cli
    from test_cli_harness import run

    pm.write_project_models(_doc(), project_root=str(project))
    stored = (project / ".llm-kit" / "models.yaml").read_bytes()
    bad = _doc()
    bad["catalog"][0]["efforts"] = ["low", "ultra"]
    out = run(
        cli.main,
        ["models", "project", "write", "--project-root", str(project), "--input", "-"],
        capsys, stdin=json.dumps(bad), monkeypatch=monkeypatch,
    )
    assert out.code == 2
    assert "'ultra' is not in the library menu" in json.loads(out.err)["error"]["message"]
    assert (project / ".llm-kit" / "models.yaml").read_bytes() == stored
