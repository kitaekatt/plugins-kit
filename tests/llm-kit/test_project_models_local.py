"""The personal override layer over the project model file.

``<file-stem>.local.yaml`` beside the tracked file holds ``{version, selections}``;
its keys win over the tracked selections. Every refusal is asserted with the stored
files byte-identical afterwards; the CLI and the Python API are both exercised.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import llm_kit.cli as cli
from llm_kit.registry import project_models as pm
from test_cli_harness import home, run, write  # noqa: F401 -- home is a fixture

_REGISTRY = """\
default: local
models:
  local: {base_url: http://h.invalid/v1, model: m-local, effort_style: ninfer, key_env: LOCAL_KEY}
  cdx: {harness: codex, model: gpt-x, effort: high, tier: 3, family: beta}
"""

_DOC = {
    "version": 1,
    "catalog": [
        {"id": "luna", "label": "Luna", "endpoint": "cdx", "model": "gpt-6-luna", "efforts": ["low", "high"]},
        {"id": "alpha", "label": "Alpha", "endpoint": "local", "model": "m-alpha", "efforts": ["none", "low"]},
    ],
    "selections": {
        "draft": {"model": "luna", "effort": "high"},
        "art": {"model": "alpha", "effort": "low"},
    },
}

TRACKED = ".llm-kit/models.yaml"
LOCAL = ".llm-kit/models.local.yaml"


@pytest.fixture()
def project(home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _REGISTRY)))
    root = tmp_path / "proj"
    root.mkdir()
    pm.write_project_models(_DOC, project_root=str(root))
    return root


def _local_doc(**selections):
    return {"version": 1, "selections": selections}


def _put(root, **selections):
    return pm.write_local_overrides(_local_doc(**selections), project_root=str(root))


def _cli(capsys, monkeypatch, *argv, stdin=None):
    if stdin is not None:
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    out = run(cli.main, list(argv), capsys, monkeypatch=monkeypatch)
    return out, (json.loads(out.out) if out.out.strip() else None)


def _project(capsys, monkeypatch, verb, root, *extra, stdin=None):
    return _cli(capsys, monkeypatch, "models", "project", verb, "--project-root", str(root), *extra, stdin=stdin)


# --- resolution -------------------------------------------------------------------------


def test_local_wins_per_key(project):
    _put(project, draft={"model": "luna", "effort": "low"})
    got = pm.read_resolved_models(project_root=str(project)).selections
    assert got["draft"] == {
        "effective": {"model": "luna", "effort": "low"},
        "default": {"model": "luna", "effort": "high"},
        "source": "override",
    }
    # the key the local file does not name keeps the tracked value
    assert got["art"] == {
        "effective": {"model": "alpha", "effort": "low"},
        "default": {"model": "alpha", "effort": "low"},
        "source": "default",
    }


def test_no_local_file_means_every_selection_is_default(project):
    resolved = pm.read_resolved_models(project_root=str(project))
    assert not resolved.local.present and resolved.local.sha256 is None
    assert {v["source"] for v in resolved.selections.values()} == {"default"}


def test_a_key_only_the_local_file_names_has_no_default(project):
    _put(project, mine={"model": "alpha", "effort": None})
    got = pm.read_resolved_models(project_root=str(project)).selections["mine"]
    assert got == {"effective": {"model": "alpha", "effort": None}, "default": None, "source": "override"}


def test_the_tracked_selections_and_digest_do_not_change_when_an_override_exists(project):
    before = (project / TRACKED).read_bytes()
    shown_before = pm.read_project_models(project_root=str(project))
    _put(project, draft={"model": "luna", "effort": "low"})
    assert (project / TRACKED).read_bytes() == before
    shown = pm.read_resolved_models(project_root=str(project))
    assert shown.project.document["selections"]["draft"]["effort"] == "high"
    assert shown.project.sha256 == shown_before.sha256


def test_a_hand_edited_entry_equal_to_the_default_is_no_override(project):
    (project / LOCAL).write_text("version: 1\nselections:\n  draft: {model: luna, effort: high}\n")
    got = pm.read_resolved_models(project_root=str(project)).selections["draft"]
    assert got["source"] == "default" and got["effective"] == got["default"]


# --- writing ----------------------------------------------------------------------------


def test_write_stores_canonical_yaml_beside_the_tracked_file(project):
    result = _put(project, draft={"model": "luna", "effort": "low"}, art={"model": "alpha"})
    assert result.path == LOCAL and result.changed and not result.deleted and result.selections == 2
    assert (project / LOCAL).read_text() == (
        "version: 1\nselections:\n"
        "  art:\n    model: alpha\n    effort: low\n".replace("effort: low", "effort: null")
        + "  draft:\n    model: luna\n    effort: low\n"
    )
    assert result.sha256 == pm.digest((project / LOCAL).read_bytes())
    assert _put(project, draft={"model": "luna", "effort": "low"}, art={"model": "alpha"}).changed is False
    assert not list(project.rglob("*.tmp"))


def test_an_override_equal_to_the_default_is_dropped(project):
    result = _put(project, draft={"model": "luna", "effort": "high"}, art={"model": "alpha", "effort": "none"})
    assert result.dropped_defaults == ["draft"] and result.selections == 1
    assert pm.read_resolved_models(project_root=str(project)).local.selections == {
        "art": {"model": "alpha", "effort": "none"}
    }


def test_setting_an_override_back_to_the_default_clears_it_and_deletes_the_empty_file(project):
    _put(project, draft={"model": "luna", "effort": "low"})
    result = pm.set_local_override("draft", "luna", "high", project_root=str(project))
    assert result.deleted and result.changed and result.sha256 is None
    assert not (project / LOCAL).exists()
    # and with nothing stored, writing a default-equal override creates nothing
    again = pm.set_local_override("draft", "luna", "high", project_root=str(project))
    assert not again.changed and not (project / LOCAL).exists()


def test_set_one_override_keeps_the_others(project):
    pm.set_local_override("draft", "luna", "low", project_root=str(project))
    pm.set_local_override("art", "alpha", "none", project_root=str(project))
    assert set(pm.read_resolved_models(project_root=str(project)).local.selections) == {"draft", "art"}


def test_clear_removes_one_key_and_deletes_the_file_when_empty(project):
    _put(project, draft={"model": "luna", "effort": "low"}, art={"model": "alpha", "effort": "none"})
    first = pm.clear_local_override("draft", project_root=str(project))
    assert first.changed and not first.deleted and first.selections == 1
    second = pm.clear_local_override("art", project_root=str(project))
    assert second.changed and second.deleted and not (project / LOCAL).exists()
    third = pm.clear_local_override("art", project_root=str(project))
    assert not third.changed


def test_clear_of_an_absent_key_changes_nothing(project):
    _put(project, draft={"model": "luna", "effort": "low"})
    before = (project / LOCAL).read_bytes()
    result = pm.clear_local_override("nope", project_root=str(project))
    assert not result.changed and (project / LOCAL).read_bytes() == before


def test_clear_repairs_a_stale_override(project):
    _put(project, draft={"model": "luna", "effort": "low"}, art={"model": "alpha", "effort": "none"})
    # the tracked catalogue drops 'alpha'; the personal file now fails validation
    doc = json.loads(json.dumps(_DOC))
    doc["catalog"].pop(1)
    doc["selections"].pop("art")
    pm.write_project_models(doc, project_root=str(project))
    with pytest.raises(pm.ProjectModelsError) as err:
        pm.read_resolved_models(project_root=str(project))
    assert LOCAL in str(err.value) and "not a catalog id" in str(err.value)
    pm.clear_local_override("art", project_root=str(project))
    assert pm.read_resolved_models(project_root=str(project)).local.selections["draft"]["effort"] == "low"


def test_if_match_guards_the_local_file(project):
    stored = _put(project, draft={"model": "luna", "effort": "low"})
    with pytest.raises(pm.ProjectModelsError) as err:
        pm.write_local_overrides(_local_doc(), project_root=str(project), if_match="0" * 64)
    assert err.value.code == "stale" and LOCAL in str(err.value)
    with pytest.raises(pm.ProjectModelsError):
        pm.clear_local_override("draft", project_root=str(project), if_match="0" * 64)
    assert (project / LOCAL).exists()
    done = pm.clear_local_override("draft", project_root=str(project), if_match=stored.sha256)
    assert done.deleted


def test_an_empty_write_without_a_file_creates_nothing(project):
    result = _put(project)
    assert not result.changed and not (project / LOCAL).exists()


def test_local_file_name_follows_the_file_stem(project, tmp_path):
    assert pm.local_file_name(".llm-kit/models.yaml") == ".llm-kit/models.local.yaml"
    assert pm.local_file_name("cfg/m.yml") == "cfg/m.local.yaml"
    assert pm.local_file_name("m") == "m.local.yaml"
    pm.write_project_models(_DOC, project_root=str(project), file="cfg/m.yaml")
    pm.write_local_overrides(
        _local_doc(draft={"model": "luna", "effort": "low"}), project_root=str(project), file="cfg/m.yaml"
    )
    assert (project / "cfg" / "m.local.yaml").is_file() and not (project / LOCAL).exists()


# --- validation: exactly the tracked checks, naming the local file ---------------------


@pytest.mark.parametrize(
    "selection, fragment",
    [
        ({"model": "ghost", "effort": None}, "'ghost' is not a catalog id"),
        ({"model": "luna", "effort": "none"}, "'none' is not accepted for 'luna'"),
        ({"model": "luna", "effort": "medium"}, "'medium' is not accepted for 'luna'"),
        ({"effort": "low"}, "'model' is required"),
        ({"model": "luna", "extra": 1}, "unknown key(s)"),
    ],
)
def test_invalid_overrides_are_refused_with_the_local_file_named(project, selection, fragment):
    with pytest.raises(pm.ProjectModelsError) as err:
        _put(project, bad=selection)
    assert err.value.code == "invalid" and str(err.value).startswith(f"{LOCAL}: ") and fragment in str(err.value)
    assert not (project / LOCAL).exists() and not list(project.rglob("*.tmp"))


def test_the_local_file_carries_no_catalogue_and_needs_a_version(project):
    for document, fragment in (
        ({"version": 1, "catalog": [], "selections": {}}, "unknown key(s)"),
        ({"selections": {}}, "version: is required"),
        ({"version": 2, "selections": {}}, "must be 1"),
    ):
        with pytest.raises(pm.ProjectModelsError) as err:
            pm.write_local_overrides(document, project_root=str(project))
        assert str(err.value).startswith(LOCAL) and fragment in str(err.value)


def test_a_bad_stored_local_file_is_refused_not_ignored(project):
    (project / LOCAL).write_text("version: 1\nselections:\n  draft: {model: luna, effort: none}\n")
    with pytest.raises(pm.ProjectModelsError) as err:
        pm.read_resolved_models(project_root=str(project))
    assert str(err.value).startswith(f"{LOCAL}: ")
    (project / LOCAL).write_text("")
    with pytest.raises(pm.ProjectModelsError) as err:
        pm.read_resolved_models(project_root=str(project))
    assert str(err.value).startswith(f"{LOCAL}: ") and "empty" in str(err.value)
    (project / LOCAL).write_text("version: 1\nselections: [x\n")
    with pytest.raises(pm.ProjectModelsError) as err:
        pm.read_resolved_models(project_root=str(project))
    assert str(err.value).startswith(f"{LOCAL}: ") and "not valid YAML" in str(err.value)


def test_a_local_write_needs_the_tracked_file(home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _REGISTRY)))
    root = tmp_path / "bare"
    root.mkdir()
    with pytest.raises(pm.ProjectModelsError) as err:
        _put(root, draft={"model": "luna"})
    assert err.value.code == "missing" and not (root / ".llm-kit").exists()


def test_the_local_file_may_not_be_a_symlink(project, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    target.write_text("version: 1\nselections: {}\n")
    (project / LOCAL).symlink_to(target)
    with pytest.raises(pm.ProjectModelsError) as err:
        pm.read_resolved_models(project_root=str(project))
    assert err.value.code == "symlink"


def test_validate_reports_what_a_write_would_store(project):
    check = pm.validate_local_overrides(
        _local_doc(draft={"model": "luna", "effort": "high"}, art={"model": "alpha", "effort": "none"}),
        project_root=str(project),
    )
    assert check.dropped_defaults == ["draft"] and check.selections == 1
    stored = _put(project, art={"model": "alpha", "effort": "none"})
    assert check.canonical_sha256 == stored.sha256
    assert pm.validate_local_overrides(_local_doc(), project_root=str(project)).canonical_sha256 is None


def test_this_library_never_writes_gitignore(project):
    _put(project, draft={"model": "luna", "effort": "low"})
    assert not (project / ".gitignore").exists() and not (project / ".llm-kit" / ".gitignore").exists()


# --- CLI --------------------------------------------------------------------------------


def test_cli_show_reports_effective_default_source(project, capsys, monkeypatch):
    out, got = _project(capsys, monkeypatch, "show", project)
    assert out.code == 0
    assert got["local"] == {"path": LOCAL, "present": False, "sha256": None, "selections": {}}
    assert got["document"]["selections"]["draft"] == {"model": "luna", "effort": "high"}
    assert got["resolved_selections"]["draft"]["source"] == "default"

    out, got = _project(capsys, monkeypatch, "write", project, "--layer", "local", "--key", "draft",
                        "--model", "luna", "--effort", "low")
    assert out.code == 0 and got["layer"] == "local" and got["path"] == LOCAL and got["changed"] is True

    out, got = _project(capsys, monkeypatch, "show", project)
    assert got["resolved_selections"]["draft"] == {
        "effective": {"model": "luna", "effort": "low"},
        "default": {"model": "luna", "effort": "high"},
        "source": "override",
    }
    assert got["local"]["present"] is True and got["local"]["sha256"] == pm.digest((project / LOCAL).read_bytes())
    assert got["document"]["selections"]["draft"]["effort"] == "high"  # the stored tracked file, as stored


def test_cli_catalog_reports_the_resolution(project, capsys, monkeypatch):
    pm.set_local_override("draft", "luna", "low", project_root=str(project))
    out, got = _cli(capsys, monkeypatch, "models", "catalog", "--project-root", str(project))
    assert out.code == 0
    assert got["selections"]["draft"] == {"model": "luna", "effort": "high"}
    assert got["resolved_selections"]["draft"]["effective"] == {"model": "luna", "effort": "low"}
    assert got["resolved_selections"]["draft"]["source"] == "override"
    assert got["resolved_selections"]["art"]["source"] == "default"
    assert got["local"] == {"path": LOCAL, "present": True, "sha256": pm.digest((project / LOCAL).read_bytes())}


def test_cli_write_local_from_stdin_validate_and_clear(project, capsys, monkeypatch):
    body = json.dumps(_local_doc(draft={"model": "luna", "effort": "low"}))
    out, got = _project(capsys, monkeypatch, "validate", project, "--layer", "local", "--input", "-", stdin=body)
    assert out.code == 0 and got["valid"] is True and got["layer"] == "local" and not (project / LOCAL).exists()
    out, got = _project(capsys, monkeypatch, "write", project, "--layer", "local", "--input", "-", stdin=body)
    assert out.code == 0 and got["selections"] == 1 and (project / LOCAL).is_file()
    out, got = _project(capsys, monkeypatch, "clear", project, "--key", "draft")
    assert out.code == 0 and got["deleted"] is True and not (project / LOCAL).exists()


def test_cli_local_refusals_name_the_file_and_write_nothing(project, capsys, monkeypatch):
    out, _ = _project(capsys, monkeypatch, "write", project, "--layer", "local", "--key", "draft",
                      "--model", "luna", "--effort", "none")
    err = json.loads(out.err)["error"]
    assert out.code == 2 and err["code"] == "invalid" and err["message"].startswith(f"{LOCAL}: ")
    assert not (project / LOCAL).exists()
    for argv in (
        ("write", "--layer", "local"),  # neither input nor key
        ("write", "--layer", "local", "--key", "draft"),  # key without model
        ("write", "--layer", "local", "--model", "luna", "--input", "-"),  # model without key
        ("write", "--key", "draft", "--model", "luna"),  # tracked layer cannot take --key
        ("write",),  # tracked write needs input
    ):
        out, _ = _project(capsys, monkeypatch, argv[0], project, *argv[1:], stdin=json.dumps(_local_doc()))
        assert out.code == 2 and json.loads(out.err)["error"]["code"] == "invalid", argv
    assert not (project / LOCAL).exists()


def test_cli_tracked_write_is_unchanged_and_ignores_the_local_layer(project, capsys, monkeypatch):
    pm.set_local_override("draft", "luna", "low", project_root=str(project))
    before = (project / LOCAL).read_bytes()
    out, got = _project(capsys, monkeypatch, "write", project, "--input", "-", stdin=json.dumps(_DOC))
    assert out.code == 0 and got["changed"] is False and "layer" not in got
    assert (project / LOCAL).read_bytes() == before


def test_a_tracked_write_that_orphans_an_override_surfaces_on_the_next_read(project):
    _put(project, art={"model": "alpha", "effort": "none"})
    doc = json.loads(json.dumps(_DOC))
    doc["catalog"].pop(1)
    doc["selections"].pop("art")
    pm.write_project_models(doc, project_root=str(project))  # the tracked write itself succeeds
    with pytest.raises(pm.ProjectModelsError) as err:
        pm.read_resolved_models(project_root=str(project))
    assert str(err.value).startswith(f"{LOCAL}: ")
