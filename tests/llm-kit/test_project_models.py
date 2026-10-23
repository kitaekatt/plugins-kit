"""The project model file: Python API and ``llm-kit models project`` CLI.

Every refusal is asserted twice, once through the Python API and once through
the CLI, and each time the stored file must be byte-identical afterwards with no
temporary file left behind: a refusal happens BEFORE the file is replaced.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

from llm_kit.registry import project_models as pm
from test_cli_harness import home, run, write  # noqa: F401 -- home is a fixture

_REGISTRY = """\
default: local
models:
  local: {base_url: http://h.invalid/v1, model: m-local, effort_style: ninfer, key_env: LOCAL_KEY}
  plain: {base_url: http://h.invalid/v1, model: m-plain}
  cl: {harness: claude, model: opus, tier: 2, family: alpha}
  cdx: {harness: codex, model: gpt-x, effort: high, tier: 3, family: beta}
"""

_DOC = {
    "version": 1,
    "catalog": [
        {
            "id": "zeta", "label": "Zeta", "endpoint": "cdx", "model": "gpt-6.1-sol",
            "efforts": ["low", "high", "max"],
        },
        {
            "id": "alpha", "label": "Alpha", "endpoint": "local", "model": "m-alpha",
            "efforts": ["none", "low"],
            "request_defaults": {"extras": {"provider": {"sort": "latency"}, "a": [1, 2]}},
        },
        {"id": "mid", "label": "Mid", "endpoint": "plain", "model": "deepseek/x"},
    ],
    "selections": {
        "draft.generate.primary": {"model": "zeta", "effort": "high"},
        "art": {"model": "alpha", "effort": None},
        "build": {"model": "mid"},
    },
}

_CANONICAL = """\
version: 1
catalog:
  - id: zeta
    label: Zeta
    endpoint: cdx
    model: gpt-6.1-sol
    efforts: [low, high, max]
  - id: alpha
    label: Alpha
    endpoint: local
    model: m-alpha
    efforts: [none, low]
    request_defaults:
      extras:
        a:
          - 1
          - 2
        provider:
          sort: latency
  - id: mid
    label: Mid
    endpoint: plain
    model: deepseek/x
selections:
  art:
    model: alpha
    effort: null
  build:
    model: mid
    effort: null
  draft.generate.primary:
    model: zeta
    effort: high
"""

DEFAULT_REL = ".llm-kit/models.yaml"


def doc(**edits):
    out = copy.deepcopy(_DOC)
    out.update(edits)
    return out


@pytest.fixture()
def project(home, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(write(tmp_path / "registry.yaml", _REGISTRY)))
    root = tmp_path / "proj"
    root.mkdir()
    return root


def stored(root: Path, rel: str = DEFAULT_REL) -> bytes:
    return (root / rel).read_bytes()


def leftovers(root: Path) -> list[str]:
    return sorted(p.name for p in root.rglob("*.tmp"))


def seed(root: Path, rel: str = DEFAULT_REL) -> bytes:
    pm.write_project_models(doc(), project_root=str(root), file=rel)
    return stored(root, rel)


# --- order and canonical round trip -----------------------------------------


def test_write_stores_the_canonical_text(project):
    result = pm.write_project_models(doc(), project_root=str(project))
    assert stored(project) == _CANONICAL.encode()
    assert result.path == DEFAULT_REL and result.changed is True
    assert (result.catalog_rows, result.selections) == (3, 3)
    assert result.sha256 == pm.digest(_CANONICAL.encode())


def test_catalogue_order_survives_a_round_trip(project):
    pm.write_project_models(doc(), project_root=str(project))
    shown = pm.read_project_models(project_root=str(project))
    assert [row["id"] for row in shown.document["catalog"]] == ["zeta", "alpha", "mid"]
    assert list(shown.document["selections"]) == sorted(_DOC["selections"])
    assert shown.sha256 == pm.digest(stored(project))


def test_rewriting_what_show_returned_is_a_byte_identical_noop(project):
    pm.write_project_models(doc(), project_root=str(project))
    before = stored(project)
    inode = (project / DEFAULT_REL).stat().st_ino
    shown = pm.read_project_models(project_root=str(project))
    again = pm.write_project_models(
        shown.document, project_root=str(project), if_match=shown.sha256
    )
    assert again.changed is False and stored(project) == before
    assert (project / DEFAULT_REL).stat().st_ino == inode  # not even replaced


def test_a_hand_written_file_with_comments_reads_and_is_canonicalised_on_write(project):
    hand = "# mine\nversion: 1\ncatalog:\n- {id: b, label: B, endpoint: cdx, model: m}\n- {id: a, label: A, endpoint: cdx, model: m}  # second\n"
    write(project / DEFAULT_REL, hand)
    shown = pm.read_project_models(project_root=str(project))
    assert [r["id"] for r in shown.document["catalog"]] == ["b", "a"]
    pm.write_project_models(shown.document, project_root=str(project), if_match=shown.sha256)
    assert b"# mine" not in stored(project)
    assert pm.read_project_models(project_root=str(project)).document == shown.document


def test_default_path_and_file_override(project):
    pm.write_project_models(doc(), project_root=str(project))
    assert (project / ".llm-kit" / "models.yaml").is_file()
    pm.write_project_models(doc(), project_root=str(project), file="cfg/sub/m.yaml")
    assert (project / "cfg" / "sub" / "m.yaml").is_file()
    absolute = project / "abs.yaml"
    pm.write_project_models(doc(), project_root=str(project), file=str(absolute))
    assert absolute.is_file()


def test_project_root_defaults_to_the_working_directory(project, monkeypatch):
    monkeypatch.chdir(project)
    pm.write_project_models(doc())
    assert (project / DEFAULT_REL).is_file()


def test_new_file_takes_the_umask_mode_and_a_replaced_file_keeps_its_mode(project):
    pm.write_project_models(doc(), project_root=str(project))
    path = project / DEFAULT_REL
    assert path.stat().st_mode & 0o777 != 0o600 or os.name == "nt"  # not mkstemp's 0600
    path.chmod(0o640)
    changed = doc()
    changed["catalog"][0]["label"] = "Changed"
    pm.write_project_models(changed, project_root=str(project))
    assert path.stat().st_mode & 0o777 == 0o640 or os.name == "nt"


# --- refusals before the file is replaced -----------------------------------


def _drop(key):
    def edit(d):
        del d[key]
    return edit


def _edit(fn):
    def mutate():
        d = doc()
        fn(d)
        return d
    return mutate


REFUSALS = {
    "unknown_selection_model": (_edit(lambda d: d["selections"].update(x={"model": "ghost"})), "is not a catalog id"),
    "invalid_selection_effort": (_edit(lambda d: d["selections"].update(x={"model": "zeta", "effort": "xhigh"})), "is not accepted"),
    "effort_outside_library_menu": (_edit(lambda d: d["catalog"][0].update(efforts=["low", "bogus"])), "not in the library menu"),
    "effort_widens_menu": (_edit(lambda d: d["catalog"][0].update(efforts=["none"])), "not in the library menu"),
    "effort_reordered": (_edit(lambda d: d["catalog"][0].update(efforts=["high", "low"])), "library order"),
    "effort_duplicated": (_edit(lambda d: d["catalog"][0].update(efforts=["low", "low"])), "library order"),
    "efforts_on_menuless_endpoint": (_edit(lambda d: d["catalog"][2].update(efforts=["low"])), "no effort menu"),
    "effort_on_menuless_endpoint": (_edit(lambda d: d["selections"].update(x={"model": "mid", "effort": "low"})), "is not accepted"),
    "none_on_a_menu_without_it": (_edit(lambda d: d["selections"].update(x={"model": "zeta", "effort": "none"})), "is not accepted"),
    "off_is_not_translated": (_edit(lambda d: d["selections"].update(x={"model": "zeta", "effort": False})), "must be a non-empty string or null"),
    "efforts_not_a_list": (_edit(lambda d: d["catalog"][0].update(efforts="low")), "must be a list"),
    "unresolved_endpoint": (_edit(lambda d: d["catalog"][0].update(endpoint="nowhere")), "resolves to no registry endpoint"),
    "duplicate_id": (_edit(lambda d: d["catalog"][1].update(id="zeta")), "duplicate id"),
    "empty_id": (_edit(lambda d: d["catalog"][0].update(id="")), "non-empty"),
    "empty_label": (_edit(lambda d: d["catalog"][0].update(label=" ")), "non-empty"),
    "empty_model": (_edit(lambda d: d["catalog"][0].update(model="")), "non-empty"),
    "missing_label": (_edit(lambda d: d["catalog"][0].pop("label")), "'label' is required"),
    "unknown_row_key": (_edit(lambda d: d["catalog"][0].update(base_url="http://x")), "unknown key"),
    "unknown_top_key": (_edit(lambda d: d.update(extra=1)), "unknown key"),
    "unknown_selection_key": (_edit(lambda d: d["selections"]["art"].update(label="x")), "unknown key"),
    "unknown_default_key": (_edit(lambda d: d["catalog"][1]["request_defaults"].update(api_key="k")), "unknown key"),
    "wrong_version": (_edit(lambda d: d.update(version=2)), "must be 1"),
    "boolean_version": (_edit(lambda d: d.update(version=True)), "must be 1"),
    "missing_version": (_edit(_drop("version")), "is required"),
    "non_json_extras": (_edit(lambda d: d["catalog"][1]["request_defaults"]["extras"].update(bad={1, 2})), "JSON data"),
    "non_finite_extras": (_edit(lambda d: d["catalog"][1]["request_defaults"]["extras"].update(bad=float("inf"))), "finite"),
    "reserved_extras_effort": (_edit(lambda d: d["catalog"][1]["request_defaults"]["extras"].update(reasoning_effort="low")), "reserved"),
    "reserved_nested_effort": (_edit(lambda d: d["catalog"][1]["request_defaults"]["extras"].update(chat_template_kwargs={"reasoning_effort": "low"})), "reserved"),
    "catalog_not_a_list": (_edit(lambda d: d.update(catalog={})), "must be a list"),
    "selection_without_model": (_edit(lambda d: d["selections"].update(x={"effort": "low"})), "'model' is required"),
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_python_write_refuses_before_replacing(project, name):
    build, needle = REFUSALS[name]
    before = seed(project)
    with pytest.raises(pm.ProjectModelsError, match=needle) as caught:
        pm.write_project_models(build(), project_root=str(project))
    assert caught.value.code == pm.CODE_INVALID
    assert stored(project) == before and leftovers(project) == []
    with pytest.raises(pm.ProjectModelsError):
        pm.validate_project_models(build(), project_root=str(project))


# A set and Infinity are not expressible as JSON input; the CLI refuses
# Infinity at parse time (test_cli_rejects_json_constants_and_a_missing_input_flag).
NOT_JSON = {"non_json_extras", "non_finite_extras"}


@pytest.mark.parametrize("name", sorted(set(REFUSALS) - NOT_JSON))
def test_cli_write_and_validate_refuse_before_replacing(project, capsys, monkeypatch, name):
    import llm_kit.cli as cli

    build, needle = REFUSALS[name]
    document = build()
    before = seed(project)
    for verb in ("write", "validate"):
        got = run(cli.main, ["models", "project", verb, "--project-root", str(project), "--input", "-"],
                  capsys, stdin=json.dumps(document), monkeypatch=monkeypatch)
        assert got.code == 2 and got.out == ""
        error = json.loads(got.err)["error"]
        assert error["kind"] == "project-models" and error["code"] == "invalid"
        assert needle in error["message"]
        assert stored(project) == before and leftovers(project) == []


def test_validation_names_the_first_problem_by_path(project):
    bad = doc()
    bad["catalog"][1]["efforts"] = ["none", "bogus"]
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.validate_project_models(bad, project_root=str(project))
    assert str(caught.value).startswith("catalog[1].efforts[1]:")


def test_validate_writes_nothing_and_reports_the_canonical_digest(project):
    got = pm.validate_project_models(doc(), project_root=str(project))
    assert got.canonical_sha256 == pm.digest(_CANONICAL.encode())
    assert not (project / ".llm-kit").exists()


def test_yaml_with_a_duplicate_key_or_a_boolean_effort_is_refused_on_read(project):
    write(project / DEFAULT_REL, "version: 1\nversion: 1\ncatalog: []\n")
    with pytest.raises(pm.ProjectModelsError, match="duplicate mapping key"):
        pm.read_project_models(project_root=str(project))
    write(project / DEFAULT_REL, "version: 1\ncatalog:\n- {id: a, label: A, endpoint: cdx, model: m}\nselections:\n  k: {model: a, effort: off}\n")
    with pytest.raises(pm.ProjectModelsError, match="non-empty string or null"):
        pm.read_project_models(project_root=str(project))


def test_json_input_refuses_duplicate_keys_and_constants():
    with pytest.raises(pm.ProjectModelsError, match="duplicate key"):
        pm.parse_json_document('{"version": 1, "version": 1}')
    with pytest.raises(pm.ProjectModelsError, match="NaN"):
        pm.parse_json_document('{"version": NaN}')
    with pytest.raises(pm.ProjectModelsError, match="not valid JSON"):
        pm.parse_json_document("{nope")


def test_missing_file_is_missing_not_empty(project):
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.read_project_models(project_root=str(project))
    assert caught.value.code == pm.CODE_MISSING


# --- stale hash --------------------------------------------------------------


def test_stale_digest_refuses_before_replacing(project):
    seed(project)
    shown = pm.read_project_models(project_root=str(project))
    other = doc()
    other["catalog"][0]["label"] = "Edited elsewhere"
    pm.write_project_models(other, project_root=str(project), if_match=shown.sha256)  # first writer wins
    after_first = stored(project)
    mine = doc()
    mine["catalog"][0]["label"] = "Mine"
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.write_project_models(mine, project_root=str(project), if_match=shown.sha256)
    assert caught.value.code == pm.CODE_STALE
    assert stored(project) == after_first and leftovers(project) == []


def test_if_match_on_an_absent_file_is_stale_and_creates_nothing(project):
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.write_project_models(doc(), project_root=str(project), if_match="0" * 64)
    assert caught.value.code == pm.CODE_STALE
    assert not (project / ".llm-kit").exists()


def test_malformed_if_match_is_invalid(project):
    seed(project)
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.write_project_models(doc(), project_root=str(project), if_match="abc")
    assert caught.value.code == pm.CODE_INVALID


def test_unconditional_write_overwrites(project):
    seed(project)
    changed = doc()
    changed["catalog"][0]["label"] = "Changed"
    assert pm.write_project_models(changed, project_root=str(project)).changed is True


# --- confinement ---------------------------------------------------------------


@pytest.fixture()
def outside(tmp_path):
    victim = tmp_path / "outside" / "victim.yaml"
    victim.parent.mkdir()
    victim.write_text("untouched\n", encoding="utf-8")
    return victim


def _escape_cases(project, outside):
    return {
        "dotdot": "../outside/victim.yaml",
        "nested_dotdot": "a/../../outside/victim.yaml",
        "absolute": str(outside),
    }


@pytest.mark.parametrize("case", ["dotdot", "nested_dotdot", "absolute"])
def test_a_path_outside_the_root_is_refused_everywhere(project, outside, capsys, monkeypatch, case):
    import llm_kit.cli as cli

    name = _escape_cases(project, outside)[case]
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.write_project_models(doc(), project_root=str(project), file=name)
    assert caught.value.code == pm.CODE_PATH
    for verb in ("show", "validate", "write"):
        argv = ["models", "project", verb, "--project-root", str(project), "--file", name]
        if verb != "show":
            argv += ["--input", "-"]
        got = run(cli.main, argv, capsys, stdin=json.dumps(doc()), monkeypatch=monkeypatch)
        assert got.code == 2 and json.loads(got.err)["error"]["code"] == "path"
    assert outside.read_text(encoding="utf-8") == "untouched\n"
    assert not (project.parent / "a").exists() and not (project / "a").exists()


def test_the_root_itself_is_not_a_file(project):
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.resolve_project_file(str(project), ".")
    assert caught.value.code == pm.CODE_PATH


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_file_is_refused_and_its_target_untouched(project, outside, capsys, monkeypatch):
    import llm_kit.cli as cli

    (project / ".llm-kit").mkdir()
    (project / DEFAULT_REL).symlink_to(outside)
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.write_project_models(doc(), project_root=str(project))
    assert caught.value.code == pm.CODE_SYMLINK
    with pytest.raises(pm.ProjectModelsError):
        pm.read_project_models(project_root=str(project))
    got = run(cli.main, ["models", "project", "write", "--project-root", str(project), "--input", "-"],
              capsys, stdin=json.dumps(doc()), monkeypatch=monkeypatch)
    assert got.code == 2 and json.loads(got.err)["error"]["code"] == "symlink"
    assert outside.read_text(encoding="utf-8") == "untouched\n"
    assert (project / DEFAULT_REL).is_symlink() and leftovers(project) == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_directory_is_refused(project, outside, capsys, monkeypatch):
    import llm_kit.cli as cli

    (project / ".llm-kit").symlink_to(outside.parent, target_is_directory=True)
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.write_project_models(doc(), project_root=str(project))
    assert caught.value.code == pm.CODE_SYMLINK
    got = run(cli.main, ["models", "project", "write", "--project-root", str(project), "--input", "-"],
              capsys, stdin=json.dumps(doc()), monkeypatch=monkeypatch)
    assert got.code == 2 and json.loads(got.err)["error"]["code"] == "symlink"
    assert sorted(p.name for p in outside.parent.iterdir()) == ["victim.yaml"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_an_absolute_name_spelled_through_a_symlinked_root_prefix_is_accepted(project, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(project, target_is_directory=True)
    pm.write_project_models(doc(), project_root=str(project), file=str(link / "viaLink.yaml"))
    assert (project / "viaLink.yaml").is_file()


def test_a_missing_root_and_a_directory_in_place_of_the_file_are_refused(project, tmp_path):
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.write_project_models(doc(), project_root=str(tmp_path / "absent"))
    assert caught.value.code == pm.CODE_MISSING
    (project / DEFAULT_REL).mkdir(parents=True)
    with pytest.raises(pm.ProjectModelsError, match="not a regular file"):
        pm.write_project_models(doc(), project_root=str(project))


# --- atomic replacement --------------------------------------------------------


def test_the_save_is_a_same_directory_temp_file_replaced_into_place(project, monkeypatch):
    seed(project)
    real, calls = os.replace, []

    def spy(src, dst):
        calls.append((Path(src), Path(dst), Path(src).read_bytes()))
        return real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    changed = doc()
    changed["catalog"][0]["label"] = "Changed"
    pm.write_project_models(changed, project_root=str(project))
    (src, dst, payload), = calls
    assert dst == (project / DEFAULT_REL).resolve() and src.parent == dst.parent and src != dst
    assert payload == stored(project)


def test_a_failed_replace_leaves_the_original_and_no_temp_file(project, monkeypatch):
    before = seed(project)

    def boom(src, dst):
        raise OSError("disk says no")

    monkeypatch.setattr(os, "replace", boom)
    changed = doc()
    changed["catalog"][0]["label"] = "Changed"
    with pytest.raises(OSError, match="disk says no"):
        pm.write_project_models(changed, project_root=str(project))
    assert stored(project) == before and leftovers(project) == []


# --- the exact-model seam --------------------------------------------------------


def test_the_effort_menu_is_replaceable_per_endpoint_and_model(project):
    calls = []

    def exact(endpoint, model):
        calls.append((endpoint.name, model))
        return ("none", "low") if model == "gpt-6.1-sol" else ()

    narrow = doc()
    narrow["catalog"][0]["efforts"] = ["none"]
    narrow["catalog"][1].pop("efforts")
    narrow["catalog"][1].pop("request_defaults")
    narrow["selections"] = {"k": {"model": "zeta", "effort": "none"}}
    pm.validate_project_models(narrow, project_root=str(project), menu_for=exact)
    assert ("cdx", "gpt-6.1-sol") in calls
    # The library default for the same document refuses `none` on a codex harness.
    with pytest.raises(pm.ProjectModelsError, match="library menu"):
        pm.validate_project_models(narrow, project_root=str(project))


def test_library_menu_is_the_exact_model_menu(project):
    info = pm.EndpointInfo
    codex = info("cdx", "harness", harness="codex", route="codex")
    assert pm.library_effort_menu(codex, "gpt-6-luna") == ("low", "medium", "high", "xhigh", "max")
    assert pm.library_effort_menu(codex, "gpt-6.1-sol") == ("low", "medium", "high", "xhigh", "max", "ultra")
    # A harness model nobody measured has no menu: the library invents none.
    assert pm.library_effort_menu(codex, "gpt-x") == ()
    assert pm.library_effort_menu_with_source(codex, "gpt-x") == ((), "none")
    assert pm.library_effort_menu_with_source(codex, "gpt-6-luna")[1] == "library-model"
    # A transport with a resolved style and no profile keeps the style's wire menu.
    local = info("local", "transport", effort_style="ninfer")
    assert pm.library_effort_menu(local, "any") == ("none", "low", "medium", "xhigh")
    assert pm.library_effort_menu_with_source(local, "any")[1] == "library-endpoint-style"
    assert pm.library_effort_menu(info("openrouter", "wrapper"), "any") == ()
    # openrouter.ai rows take the measured vendor menu, `none` included.
    router = info("openrouter", "wrapper", route="openrouter")
    assert pm.library_effort_menu(router, "deepseek/deepseek-v4.1-flash") == ("none", "low", "high", "max")
    assert pm.library_effort_menu(router, "deepseek/unmeasured") == ()


def test_missing_pyyaml_is_a_named_dependency_error(project, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(pm.ProjectModelsError) as caught:
        pm.write_project_models(doc(), project_root=str(project))
    assert caught.value.code == pm.CODE_DEPENDENCY and "katsoft-llm-kit[config]" in str(caught.value)


# --- the CLI shapes ---------------------------------------------------------------


def _cli(capsys, monkeypatch, *argv, stdin=None):
    import llm_kit.cli as cli

    return run(cli.main, list(argv), capsys, stdin=stdin, monkeypatch=monkeypatch)


def test_cli_write_show_validate_flow(project, capsys, monkeypatch):
    root = str(project)
    got = _cli(capsys, monkeypatch, "models", "project", "validate", "--project-root", root, "--input", "-",
               stdin=json.dumps(doc()))
    assert got.code == 0
    assert json.loads(got.out) == {
        "schema": pm.SCHEMA, "valid": True, "path": DEFAULT_REL,
        "canonical_sha256": pm.digest(_CANONICAL.encode()), "catalog_rows": 3, "selections": 3,
    }
    assert not (project / ".llm-kit").exists()

    got = _cli(capsys, monkeypatch, "models", "project", "write", "--project-root", root, "--input", "-",
               stdin=json.dumps(doc()))
    assert got.code == 0
    written = json.loads(got.out)
    assert written == {
        "schema": pm.SCHEMA, "path": DEFAULT_REL, "sha256": pm.digest(_CANONICAL.encode()),
        "changed": True, "catalog_rows": 3, "selections": 3,
    }
    assert stored(project) == _CANONICAL.encode()

    got = _cli(capsys, monkeypatch, "models", "project", "show", "--project-root", root)
    shown = json.loads(got.out)
    assert got.code == 0 and shown["sha256"] == written["sha256"] and shown["path"] == DEFAULT_REL
    assert [r["id"] for r in shown["document"]["catalog"]] == ["zeta", "alpha", "mid"]
    assert shown["document"]["selections"]["build"] == {"effort": None, "model": "mid"}

    # The shown document is the next write's input; the digest guards it.
    got = _cli(capsys, monkeypatch, "models", "project", "write", "--project-root", root, "--input", "-",
               "--if-match", shown["sha256"], stdin=json.dumps(shown["document"]))
    assert got.code == 0 and json.loads(got.out)["changed"] is False


def test_cli_stale_if_match_refuses(project, capsys, monkeypatch):
    before = seed(project)
    got = _cli(capsys, monkeypatch, "models", "project", "write", "--project-root", str(project), "--input", "-",
               "--if-match", "f" * 64, stdin=json.dumps(doc()))
    assert got.code == 2 and got.out == ""
    assert json.loads(got.err)["error"]["code"] == "stale"
    assert stored(project) == before and leftovers(project) == []


def test_cli_file_option_is_honoured(project, capsys, monkeypatch):
    got = _cli(capsys, monkeypatch, "models", "project", "write", "--project-root", str(project),
               "--file", "cfg/m.yaml", "--input", "-", stdin=json.dumps(doc()))
    assert got.code == 0 and json.loads(got.out)["path"] == "cfg/m.yaml"
    assert (project / "cfg" / "m.yaml").is_file() and not (project / ".llm-kit").exists()
    got = _cli(capsys, monkeypatch, "models", "project", "show", "--project-root", str(project), "--file", "cfg/m.yaml")
    assert got.code == 0 and json.loads(got.out)["path"] == "cfg/m.yaml"


def test_cli_show_of_a_missing_file_and_bad_input(project, capsys, monkeypatch):
    got = _cli(capsys, monkeypatch, "models", "project", "show", "--project-root", str(project))
    assert got.code == 2 and json.loads(got.err)["error"]["code"] == "missing"
    got = _cli(capsys, monkeypatch, "models", "project", "validate", "--project-root", str(project),
               "--input", "-", stdin="{nope")
    assert got.code == 2 and json.loads(got.err)["error"]["code"] == "invalid"


def test_cli_rejects_json_constants_and_a_missing_input_flag(project, capsys, monkeypatch):
    got = _cli(capsys, monkeypatch, "models", "project", "validate", "--project-root", str(project),
               "--input", "-", stdin='{"version": 1, "catalog": [], "x": Infinity}')
    assert got.code == 2 and "Infinity" in json.loads(got.err)["error"]["message"]
    got = _cli(capsys, monkeypatch, "models", "project", "write", "--project-root", str(project))
    assert got.code == 2 and got.out == ""


def test_cli_without_pyyaml_names_the_extra(project, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    got = _cli(capsys, monkeypatch, "models", "project", "validate", "--project-root", str(project),
               "--input", "-", stdin=json.dumps(doc()))
    assert got.code == 2 and json.loads(got.err)["error"]["code"] == "dependency"


def test_the_module_imports_no_harness_or_plugin_code():
    import ast

    tree = ast.parse(Path(pm.__file__).read_text(encoding="utf-8"))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
    assert not {n for n in names if n.split(".")[0] in {"bootstrap_lib", "katsoft_contracts", "llm_kit"}}
    source = Path(pm.__file__).read_text(encoding="utf-8")
    assert ".claude" not in source
