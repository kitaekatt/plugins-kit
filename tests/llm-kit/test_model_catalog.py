"""``llm-kit models catalog``: the project's ordered catalogue, queried.

The customer's file in these tests names the display order of the first
customer (Luna, Sonnet, DeepSeek Flash, Sol, Opus, Astra, Fable, then gpt-6-sol).
That roster and order belong to the CUSTOMER's file; the library ships neither,
and a test below pins that the shipped defaults do not carry them.

The query dispatches nothing, probes no provider and reads no user cache: the
exact-model menus are shipped data (``registry/model_profiles.py``), taken once
at build time, so a machine with no harness gets the same answer.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import llm_kit.cli as cli
from llm_kit.registry import catalog as catalog_mod
from llm_kit.registry import project_models as pm
from llm_kit.registry.models import DEFAULT_MODEL_CONFIG
from test_cli_goldens import _check, _form
from test_cli_harness import home, run, write  # noqa: F401 -- home is a fixture

_REGISTRY = """\
default: or-deepseek
models:
  or-deepseek: {base_url: https://openrouter.ai/api/v1, model: deepseek/deepseek-v4.1-flash, effort_style: top-level, key_env: OPENROUTER_API_KEY, billing: {mode: provider-reported}}
  or-nostyle: {base_url: https://openrouter.ai/api/v1, model: deepseek/deepseek-v4.1-flash, key_env: OPENROUTER_API_KEY}
  astra: {harness: codex, model: gpt-6-astra, tier: 4, family: openai}
"""

#: The first customer's file: ids, labels and order are theirs.
_CUSTOMER = {
    "version": 1,
    "catalog": [
        {"id": "luna", "label": "Luna", "endpoint": "luna", "model": "gpt-6-luna"},
        {"id": "sonnet", "label": "Sonnet", "endpoint": "sonnet", "model": "claude-sonnet-5-5",
         "efforts": ["low", "medium", "high"]},
        {"id": "deepseek-flash", "label": "DeepSeek Flash", "endpoint": "or-deepseek",
         "model": "deepseek/deepseek-v4.1-flash", "efforts": ["none", "low", "high"],
         "request_defaults": {"extras": {"provider": {"sort": "latency"}}}},
        {"id": "sol", "label": "Sol", "endpoint": "sol", "model": "gpt-6.1-sol"},
        {"id": "opus", "label": "Opus", "endpoint": "opus", "model": "claude-opus-5-5"},
        {"id": "astra", "label": "Astra", "endpoint": "astra", "model": "gpt-6-astra",
         "efforts": ["medium", "high", "ultra"]},
        {"id": "fable", "label": "Fable", "endpoint": "fable", "model": "claude-fable-5-1"},
        {"id": "gpt-6-sol", "label": "gpt-6-sol", "endpoint": "sol", "model": "gpt-6-sol"},
    ],
    "selections": {
        "draft.generate.primary": {"model": "luna", "effort": "high"},
        "art": {"model": "deepseek-flash", "effort": "none"},
        "build": {"model": "sol", "effort": None},
    },
}

_ORDER = ["luna", "sonnet", "deepseek-flash", "sol", "opus", "astra", "fable", "gpt-6-sol"]


@pytest.fixture()
def project(home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _REGISTRY)))
    root = tmp_path / "proj"
    root.mkdir()
    pm.write_project_models(_CUSTOMER, project_root=str(root))
    return root


def _catalog(capsys, monkeypatch, root, *extra):
    out = run(cli.main, ["models", "catalog", "--project-root", str(root), *extra], capsys, monkeypatch=monkeypatch)
    return out, (json.loads(out.out) if out.out.strip() else None)


# --- the project's order ------------------------------------------------------------


def test_the_query_returns_the_projects_order_and_selections(project, capsys, monkeypatch):
    out, got = _catalog(capsys, monkeypatch, project)
    assert out.code == 0, out.err
    assert got["schema"] == "llm-kit.model-catalog/v1"
    assert [m["id"] for m in got["models"]] == _ORDER
    assert got["selections"] == {
        "art": {"effort": "none", "model": "deepseek-flash"},
        "build": {"effort": None, "model": "sol"},
        "draft.generate.primary": {"effort": "high", "model": "luna"},
    }
    show = pm.read_project_models(project_root=str(project))
    assert [row["id"] for row in show.document["catalog"]] == _ORDER
    assert (got["path"], got["sha256"]) == (show.path, show.sha256)


def test_the_order_follows_the_file_not_the_library(project, capsys, monkeypatch):
    reordered = json.loads(json.dumps(_CUSTOMER))
    reordered["catalog"].reverse()
    pm.write_project_models(reordered, project_root=str(project))
    _out, got = _catalog(capsys, monkeypatch, project)
    assert [m["id"] for m in got["models"]] == list(reversed(_ORDER))


def test_the_library_ships_no_roster_or_order():
    shipped = set(DEFAULT_MODEL_CONFIG["endpoints"])
    assert "astra" not in shipped  # the customer's roster entry is theirs, not a default
    assert not hasattr(catalog_mod, "ROSTER")
    src = Path(catalog_mod.__file__).read_text(encoding="utf-8")
    for label in ("Sonnet", "DeepSeek Flash", "Astra", "Fable"):
        assert label not in src


def test_each_row_reports_its_exact_model_menu_modes_and_adapter(project, capsys, monkeypatch):
    _out, got = _catalog(capsys, monkeypatch, project)
    rows = {m["id"]: m for m in got["models"]}
    assert rows["luna"]["efforts"] == ["low", "medium", "high", "xhigh", "max"]  # no ultra: luna stops at max
    assert rows["luna"]["effort_source"] == "library-model"
    assert rows["sol"]["efforts"] == ["low", "medium", "high", "xhigh", "max", "ultra"]
    assert rows["gpt-6-sol"]["efforts"] == rows["sol"]["efforts"]
    assert rows["astra"]["efforts"] == ["medium", "high", "ultra"]
    assert rows["astra"]["effort_source"] == "library-model+project-subset"
    assert rows["opus"]["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert rows["sonnet"]["efforts"] == ["low", "medium", "high"]
    assert rows["deepseek-flash"]["efforts"] == ["none", "low", "high"]
    assert rows["deepseek-flash"]["request_defaults"] == {"extras": {"provider": {"sort": "latency"}}}
    assert {i: (r["kind"], r["adapter"], r["modes"]) for i, r in rows.items()} == {
        "luna": ("harness", "codex-cli", ["completion", "workspace"]),
        "sonnet": ("harness", "claude-cli", ["completion"]),
        "deepseek-flash": ("transport", "openrouter", ["completion"]),
        "sol": ("harness", "codex-cli", ["completion", "workspace"]),
        "opus": ("harness", "claude-cli", ["completion"]),
        "astra": ("harness", "codex-cli", ["completion", "workspace"]),
        "fable": ("harness", "claude-cli", ["completion"]),
        "gpt-6-sol": ("harness", "codex-cli", ["completion", "workspace"]),
    }
    assert rows["deepseek-flash"]["effort_delivery"] == {"deliverable": True, "style": "top-level"}


def test_a_row_the_library_has_not_measured_says_so(project, capsys, monkeypatch):
    document = json.loads(json.dumps(_CUSTOMER))
    document["catalog"].append({"id": "new", "label": "New", "endpoint": "sol", "model": "gpt-7-imaginary"})
    pm.write_project_models(document, project_root=str(project))
    _out, got = _catalog(capsys, monkeypatch, project)
    row = [m for m in got["models"] if m["id"] == "new"][0]
    assert row["efforts"] == [] and row["effort_source"] == "none"
    assert any("no measured effort menu for 'gpt-7-imaginary'" in note for note in got["notes"])


def test_an_endpoint_that_cannot_send_an_effort_is_flagged(project, capsys, monkeypatch):
    """An endpoint that resolves no effort style: the menu is the model's, the wire is not."""
    document = {
        "version": 1,
        "catalog": [{"id": "d", "label": "D", "endpoint": "or-nostyle", "model": "deepseek/deepseek-v4.1-flash",
                     "efforts": ["none", "low", "high", "max"]}],
        "selections": {"k": {"model": "d", "effort": "none"}},
    }
    pm.write_project_models(document, project_root=str(project))
    _out, got = _catalog(capsys, monkeypatch, project)
    row = got["models"][0]
    assert row["efforts"] == ["none", "low", "high", "max"]
    assert row["effort_delivery"] == {"deliverable": False, "style": None}
    assert any("resolves no effort style" in note for note in got["notes"])


def test_the_shipped_openrouter_endpoint_can_send_an_effort(project, capsys, monkeypatch):
    """The shipped `openrouter` record declares effort_style top-level, so the exact menu is deliverable."""
    document = {
        "version": 1,
        "catalog": [{"id": "d", "label": "D", "endpoint": "openrouter", "model": "deepseek/deepseek-v4.1-flash",
                     "efforts": ["none", "low", "high", "max"]}],
        "selections": {"k": {"model": "d", "effort": "max"}},
    }
    pm.write_project_models(document, project_root=str(project))
    _out, got = _catalog(capsys, monkeypatch, project)
    assert got["models"][0]["effort_delivery"] == {"deliverable": True, "style": "top-level"}
    assert not any("resolves no effort style" in note for note in got["notes"])


# --- cost and consent ---------------------------------------------------------------


def test_cost_class_is_null_for_a_harness_until_auth_is_probed(project, capsys, monkeypatch):
    _out, got = _catalog(capsys, monkeypatch, project)
    rows = {m["id"]: m for m in got["models"]}
    for ident in ("luna", "sonnet", "opus"):
        assert rows[ident]["cost"]["class"] is None and rows[ident]["cost"]["allowed"] is None
        assert "not probed" in rows[ident]["cost"]["reason"]
    # a transport classifies without asking anyone: metered unless declared unmetered
    assert rows["deepseek-flash"]["cost"]["class"] == "spend"
    assert rows["deepseek-flash"]["cost"]["allowed"] is False
    assert got["consent"] == {"source": "absent", "spend": False, "usage": False}


def test_probe_auth_gives_a_harness_row_its_class_and_consent_decides_allowed(project, capsys, monkeypatch):
    from llm_kit.adapters import harness_auth
    from llm_kit.registry.consent import save_consent
    from llm_kit.registry.cost import SUBSCRIPTION, HarnessAuth

    asked = []

    def fake(harnesses, env=None, *, cwd):
        asked.append(sorted(harnesses))
        return {name: HarnessAuth(SUBSCRIPTION, "fake subscription") for name in harnesses}

    monkeypatch.setattr(harness_auth, "probe_harness_auth", fake)
    save_consent(usage=True, spend=False)
    _out, got = _catalog(capsys, monkeypatch, project, "--probe-auth")
    assert asked == [["claude", "codex"]]
    rows = {m["id"]: m for m in got["models"]}
    assert rows["luna"]["cost"] == {"class": "usage", "allowed": True, "reason": "codex on subscription auth: fake subscription"}
    assert rows["opus"]["cost"]["class"] == "usage" and rows["opus"]["cost"]["allowed"] is True
    assert rows["deepseek-flash"]["cost"]["allowed"] is False  # spend is not consented
    assert got["consent"] == {"source": "file", "spend": False, "usage": True}


# --- listing never dispatches, probes or reads a cache --------------------------------


class _Audit:
    """Records every path opened while armed (the interpreter's audit hook stays installed)."""

    opened: list = []
    armed = False

    @classmethod
    def hook(cls, event, args):
        if cls.armed and event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            cls.opened.append(os.fsdecode(args[0]))


sys.addaudithook(_Audit.hook)


def test_listing_reads_no_user_cache_and_runs_no_process(project, home, capsys, monkeypatch):  # noqa: F811
    """No ~/.codex, no models_cache.json, no subprocess: the menus are shipped data.

    A decoy cache that would give luna `ultra` sits under the throwaway HOME with
    its mode cleared; the answer must not change and the audit must see no open.
    """
    codex = home / ".codex"
    codex.mkdir()
    decoy = codex / "models_cache.json"
    decoy.write_text(json.dumps({"models": [{"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": "ultra"}]}]}))
    for path in (decoy, codex):
        path.chmod(0)

    def no_process(*args, **kwargs):
        raise AssertionError(f"the catalogue started a process: {args!r}")

    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    _Audit.opened.clear()
    _Audit.armed = True
    try:
        out, got = _catalog(capsys, monkeypatch, project)
    finally:
        _Audit.armed = False
        codex.chmod(stat.S_IRWXU)
        decoy.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert out.code == 0, out.err
    assert [m for m in got["models"] if m["id"] == "luna"][0]["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    touched = [p for p in _Audit.opened if "models_cache" in p or f"{os.sep}.codex" in p or p.endswith(".codex")]
    assert touched == []
    # Nothing under HOME/.claude is opened at all: the consent file, the endpoint registry and the
    # key files resolve to the neutral $XDG_CONFIG_HOME/llm-kit/ paths first, and a legacy ~/.claude
    # file is opened only when it exists (this HOME holds none). No harness path is touched.
    claude = [p for p in _Audit.opened if f"{os.sep}.claude" in p or p.endswith(".claude")]
    assert claude == [], claude
    assert not any(p.endswith(".claude.json") for p in _Audit.opened)


# --- the project file is the only input --------------------------------------------------


def test_file_option_is_honoured_and_an_escape_is_refused(project, capsys, monkeypatch):
    pm.write_project_models(_CUSTOMER, project_root=str(project), file="cfg/m.yaml")
    out, got = _catalog(capsys, monkeypatch, project, "--file", "cfg/m.yaml")
    assert out.code == 0 and got["path"] == "cfg/m.yaml"
    out, got = _catalog(capsys, monkeypatch, project, "--file", "../outside.yaml")
    assert out.code == 2 and got is None
    assert json.loads(out.err)["error"] == {
        "code": "path", "kind": "project-models",
        "message": json.loads(out.err)["error"]["message"],
    }


def test_a_missing_file_is_a_usage_error_not_an_empty_catalogue(home, tmp_path, capsys, monkeypatch):  # noqa: F811
    root = tmp_path / "empty"
    root.mkdir()
    out, got = _catalog(capsys, monkeypatch, root)
    assert out.code == 2 and got is None
    assert json.loads(out.err)["error"]["code"] == "missing"


def test_an_invalid_stored_file_is_refused_rather_than_shown(project, capsys, monkeypatch):
    path = project / ".llm-kit" / "models.yaml"
    path.write_text(path.read_text().replace("efforts: [medium, high, ultra]", "efforts: [medium, bogus]"))
    out, got = _catalog(capsys, monkeypatch, project)
    assert out.code == 2 and got is None
    assert "not in the library menu" in json.loads(out.err)["error"]["message"]


def test_the_python_api_matches_the_cli(project, capsys, monkeypatch):
    from llm_kit.adapters.capabilities import adapter_capabilities
    from llm_kit.registry.consent import load_consent

    tracked = pm.read_project_models(project_root=str(project))
    built = catalog_mod.build_catalog(
        tracked,
        project_root=str(project),
        modes_by_adapter={n: c.call_modes for n, c in adapter_capabilities().items()},
        consent=load_consent(),
        local=pm.read_local_overrides(tracked, project_root=str(project)),
    )
    _out, got = _catalog(capsys, monkeypatch, project)
    assert json.loads(json.dumps(built)) == got


def test_adapter_names_are_the_advertised_families():
    from llm_kit.adapters.capabilities import adapter_capabilities

    names = set(catalog_mod.ADAPTER_BY_HARNESS.values()) | {catalog_mod.TRANSPORT_ADAPTER}
    assert names == set(adapter_capabilities())


# --- the golden ---------------------------------------------------------------------------


def test_catalog_golden(project, capsys, tmp_path, monkeypatch):
    capsys.readouterr()
    code = cli.main(["models", "catalog", "--project-root", str(project)])
    cap = capsys.readouterr()
    observed = {
        "argv": ["models", "catalog", "--project-root", "<T>/proj"],
        "exit": code,
        "stdout": _form(cap.out, tmp_path),
        "stderr": _form(cap.err, tmp_path),
    }
    _check("models_catalog_customer", observed)


def test_catalog_golden_with_an_override(project, capsys, tmp_path, monkeypatch):
    pm.set_local_override("draft.generate.primary", "luna", "low", project_root=str(project))
    capsys.readouterr()
    code = cli.main(["models", "catalog", "--project-root", str(project)])
    cap = capsys.readouterr()
    observed = {
        "argv": ["models", "catalog", "--project-root", "<T>/proj"],
        "exit": code,
        "stdout": _form(cap.out, tmp_path),
        "stderr": _form(cap.err, tmp_path),
    }
    _check("models_catalog_override", observed)
    got = json.loads(cap.out)
    assert got["resolved_selections"]["draft.generate.primary"]["source"] == "override"
    assert got["selections"]["draft.generate.primary"]["effort"] == "high"
