"""Unit tests for scripts/llm_kit_repoint.py: name resolution, test rewriting, edits.

Every test drives the generator's functions on a synthetic map (a fake package
pair, a fake final tree). Nothing touches the real repository: the module-level
paths are monkeypatched where a function reads them.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from repoint import load_generator

LEGACY_PKG = "plugins/job-kit/lib/job_kit"


@pytest.fixture()
def gen():
    return load_generator()


def _mapping() -> dict[str, Any]:
    def row(kind: str, **extra: Any) -> dict[str, Any]:
        return {"kind": kind, "legacy_sha256": "0" * 64, **extra}

    return {
        "schema": 2,
        "packages": {"job_kit": {
            "plugin": "job-kit", "plugin_dir": "plugins/job-kit",
            "lib_dir": "plugins/job-kit/lib", "source_dir": LEGACY_PKG,
        }},
        "guard": {"legacy": "bootstrap_guard", "module": "llm_kit.bootstrap_guard", "owner": "cli",
                  "consumers": ["cli", "jobs"]},
        "dropped": {}, "excluded": {}, "data_files": [],
        "modules": {
            f"{LEGACY_PKG}/__init__.py": row("facade-names", names={
                "thing": ["llm_kit.final.thing", "thing"],
                "renamed": ["llm_kit.final.other", "Original"],
                "mod": ["llm_kit.final.thing", "mod_function"],   # a name that is also a child module
            }, constants={"__version__": "9.9.9"}),
            f"{LEGACY_PKG}/thing.py": row("module", final="llm_kit.final.thing"),
            f"{LEGACY_PKG}/mod.py": row("module", final="llm_kit.final.mod"),
            f"{LEGACY_PKG}/sub/__init__.py": row("facade-package", final="llm_kit.final.sub",
                                                  dropped_names=["gone"]),
            f"{LEGACY_PKG}/sub/leaf.py": row("module", final="llm_kit.final.sub.leaf"),
            f"{LEGACY_PKG}/cli.py": row("forwarded", homes=["llm_kit.final.cli_a", "llm_kit.final.cli_b"],
                                        verbs={"go": ["grp", "go"]}),
        },
        "test_text_edits": [], "test_site_overrides": {},
    }


@pytest.fixture()
def names(gen):
    return gen.Names(_mapping())


# --------------------------------------------------------------------------
# name resolution
# --------------------------------------------------------------------------


def test_a_plain_module_resolves_to_its_final_module_with_its_attributes(names):
    assert names.dotted("job_kit.thing") == "llm_kit.final.thing"
    assert names.dotted("job_kit.thing.Attr.method") == "llm_kit.final.thing.Attr.method"
    assert names.module_target("job_kit.thing") == "llm_kit.final.thing"


def test_a_facade_package_resolves_to_its_final_package(names):
    assert names.dotted("job_kit.sub") == "llm_kit.final.sub"
    assert names.dotted("job_kit.sub.name") == "llm_kit.final.sub.name"
    assert names.dotted("job_kit.sub.leaf.X") == "llm_kit.final.sub.leaf.X"


def test_a_dropped_name_of_a_facade_package_is_unmapped(gen, names):
    with pytest.raises(gen.Unmapped, match="dropped name"):
        names.dotted("job_kit.sub.gone")
    with pytest.raises(gen.Unmapped, match="dropped name"):
        names.from_import("job_kit.sub", "gone")


def test_a_names_facade_resolves_a_name_to_its_owner_and_a_bare_use_to_the_alias(gen, names):
    assert names.dotted("job_kit") == "legacy_names.job_kit"
    assert names.dotted("job_kit.thing") == "llm_kit.final.thing"        # a child module wins the dotted spelling
    assert names.dotted("job_kit.renamed.x") == "llm_kit.final.other.Original.x"
    assert names.dotted("job_kit.__version__") == "legacy_names.job_kit.__version__"
    with pytest.raises(gen.Unmapped, match="exports no name"):
        names.dotted("job_kit.nowhere")


def test_from_import_resolves_each_name_to_where_it_lives(names):
    assert names.from_import("job_kit", "renamed") == ("attr", "llm_kit.final.other", "Original")
    assert names.from_import("job_kit.thing", "X") == ("attr", "llm_kit.final.thing", "X")
    assert names.from_import("job_kit.sub", "leaf") == ("module", "llm_kit.final.sub.leaf", "")
    assert names.from_import("job_kit.sub", "other") == ("attr", "llm_kit.final.sub", "other")


def test_a_name_the_facade_binds_beats_a_submodule_of_the_same_name(names):
    # `from job_kit import mod` binds the function the package __init__ imported over the submodule
    assert names.from_import("job_kit", "mod") == ("attr", "llm_kit.final.thing", "mod_function")


def test_a_forwarded_module_resolves_to_its_alias(names):
    assert names.module_target("job_kit.cli") == "legacy_names.job_kit.cli"
    assert names.from_import("job_kit.cli", "main") == ("attr", "legacy_names.job_kit.cli", "main")


def test_an_unknown_legacy_module_is_unmapped(gen, names):
    with pytest.raises(gen.Unmapped, match="no legacy module"):
        names.module_target("job_kit.absent")


def test_name_overrides_are_exact_and_win(gen):
    mapping = _mapping()
    mapping["name_overrides"] = {"job_kit.removed": {"to": "legacy_names.job_kit.removed", "reason": "r"}}
    names = gen.Names(mapping)
    assert names.dotted("job_kit.removed") == "legacy_names.job_kit.removed"


def test_children_list_the_submodules_and_forwarded_views(names):
    assert names.children("job_kit") == {
        "cli": "legacy_names.job_kit.cli", "mod": "llm_kit.final.mod",
        "sub": "llm_kit.final.sub", "thing": "llm_kit.final.thing",
    }


# --------------------------------------------------------------------------
# test rewriting
# --------------------------------------------------------------------------


def _rewrite(gen, source: str, mapping=None, relpath="tests/fake/test_x.py"):
    bad: list[str] = []
    data, classes = gen.rewrite_test_source(source.encode(), relpath, mapping or _mapping(), None, bad)
    return data.decode(), classes, bad


def test_imports_are_rewritten_from_the_syntax_tree(gen):
    text, classes, bad = _rewrite(gen, (
        "import job_kit.thing as t\n"
        "from job_kit.thing import X, Y as Z\n"
        "from job_kit.sub import leaf, other\n"
        "from job_kit import renamed, thing\n"
    ))
    assert text == (
        "import llm_kit.final.thing as t\n"
        "from llm_kit.final.thing import X; from llm_kit.final.thing import Y as Z\n"
        "import llm_kit.final.sub.leaf as leaf; from llm_kit.final.sub import other\n"
        "from llm_kit.final.other import Original as renamed; from llm_kit.final.thing import thing\n"
    )
    assert classes == ["import"] and bad == []


def test_a_bare_import_binds_the_top_name_to_the_alias_tree(gen):
    text, _, bad = _rewrite(gen, "import job_kit\nimport job_kit.thing\n")
    assert text == (
        "import legacy_names.job_kit as job_kit\n"
        "import llm_kit.final.thing; import legacy_names.job_kit as job_kit\n"
    )
    assert bad == []


def test_the_guard_import_is_rewritten(gen):
    text, _, _ = _rewrite(gen, "import bootstrap_guard\nfrom bootstrap_guard import reexec_under_plugin_venv\n")
    assert text == (
        "from llm_kit import bootstrap_guard\n"
        "from llm_kit.bootstrap_guard import reexec_under_plugin_venv\n"
    )


def test_an_unmapped_name_is_collected_and_the_statement_left(gen):
    text, _, bad = _rewrite(gen, "from job_kit import nowhere\nfrom job_kit.sub import gone\n")
    assert text == "from job_kit import nowhere\nfrom job_kit.sub import gone\n"
    assert len(bad) == 2 and "nowhere" in bad[0] and "gone" in bad[1]


def test_a_non_legacy_import_is_untouched(gen):
    text, classes, bad = _rewrite(gen, "import os\nfrom pathlib import Path\nimport llm_kit.x\n")
    assert text == "import os\nfrom pathlib import Path\nimport llm_kit.x\n"
    assert classes == [] and bad == []


def test_module_strings_resolve_to_final_names(gen):
    text, classes, _ = _rewrite(gen, (
        "A = 'job_kit.thing'\n"
        "B = 'job_kit.sub.leaf.attr'\n"
        "C = 'job_kit'\n"
        "D = 'prose naming job_kit is left alone'\n"
    ))
    assert text == (
        "A = 'llm_kit.final.thing'\n"
        "B = 'llm_kit.final.sub.leaf.attr'\n"
        "C = 'legacy_names.job_kit'\n"
        "D = 'prose naming job_kit is left alone'\n"
    )
    assert classes == ["module-string", "prose-kept"]


def test_a_path_segment_is_never_a_module_name(gen):
    text, _, bad = _rewrite(gen, "from pathlib import Path\nP = Path('x') / 'job_kit' / 'thing'\n")
    assert "'job_kit'" in text and bad == []


def test_code_strings_rewrite_statements_and_quoted_names_but_not_bare_uses(gen):
    text, classes, bad = _rewrite(gen, (
        "CODE = 'import sys; from job_kit.thing import X; print(job_kit.thing.Y)'\n"
        "MULTI = 'import job_kit.thing as t\\nfrom job_kit.sub import leaf\\nprint(\"job_kit.thing\" in sys.modules)'\n"
    ))
    first, second = text.splitlines()
    assert "from llm_kit.final.thing import X" in first and "print(job_kit.thing.Y)" in first
    assert "import llm_kit.final.thing as t" in second and "import llm_kit.final.sub.leaf as leaf" in second
    assert '"llm_kit.final.thing" in sys.modules' in second
    assert classes == ["code-string"] and bad == []


def test_a_code_string_that_names_nothing_to_change_is_left_byte_identical(gen):
    source = "CODE = 'import sys\\nprint(1)'\n"
    text, classes, _ = _rewrite(gen, source)
    assert text == source


def test_slash_paths_map_a_module_file_to_its_final_file(gen):
    text, classes, bad = _rewrite(gen, (
        "A = 'plugins/job-kit/lib/job_kit/thing.py'\n"
        "B = 'plugins/job-kit/lib'\n"
        "C = 'plugins/job-kit/lib/job_kit'\n"
        "D = 'plugins/job-kit/lib/job_kit/sub/__init__.py'\n"
    ))
    assert text == (
        f"A = '{gen.LLM_KIT_LIB.as_posix()}/llm_kit/final/thing.py'\n"
        f"B = '{gen.LLM_KIT_LIB.as_posix()}'\n"
        f"C = '{gen.LLM_KIT_LIB.as_posix()}/llm_kit'\n"
        f"D = '{gen.LLM_KIT_LIB.as_posix()}/llm_kit/final/sub/__init__.py'\n"
    )
    assert classes == ["path-string", "prose-kept"] and bad == []


def test_a_slash_path_with_no_final_file_is_collected_unless_overridden(gen):
    source = "A = 'plugins/job-kit/lib/job_kit/absent.py'\n"
    _, _, bad = _rewrite(gen, source)
    assert bad and "no final file" in bad[0]
    mapping = _mapping()
    mapping["path_overrides"] = {"plugins/job-kit/lib/job_kit/absent.py": {"to": "plugins/llm-kit/x.py", "reason": "r"}}
    text, _, bad = _rewrite(gen, source, mapping)
    assert text == "A = 'plugins/llm-kit/x.py'\n" and bad == []


def test_plugin_directory_segments_point_at_llm_kit(gen):
    text, classes, _ = _rewrite(gen, "from pathlib import Path\nP = Path('r') / 'plugins' / 'job-kit' / 'bootstrap.json'\n")
    assert "'plugins' / 'llm-kit'" in text and classes == ["path-plugin-dir"]


def test_plugin_lib_segments_point_at_the_package_source(gen):
    text, classes, bad = _rewrite(gen, (
        "from pathlib import Path\n"
        "P = Path('r') / 'plugins' / 'job-kit' / 'lib' / 'job_kit' / 'store.py'\n"
        "Q = Path('r') / 'plugins' / 'job-kit' / 'lib'\n"
        "R = Path(plugin) / 'lib' / 'job_kit'\n"
    ))
    assert text == (
        "from pathlib import Path\n"
        f"P = Path('r') / {str(gen.LLM_KIT_LIB)!r} / 'llm_kit' / 'store.py'\n"
        f"Q = Path('r') / {str(gen.LLM_KIT_LIB)!r}\n"
        f"R = Path(plugin) / '..' / '..' / {str(gen.LLM_KIT_LIB)!r} / 'llm_kit'\n"
    )
    assert "path-package-src" in classes and bad == []


def test_a_declared_skip_is_injected_on_the_named_test(gen):
    bad: list[str] = []
    data, classes = gen.rewrite_test_source(
        b"class TestA:\n    def test_x(self):\n        pass\n\ndef test_y():\n    pass\n",
        "tests/fake/test_x.py", _mapping(), {"TestA.test_x": "why"}, bad)
    text = data.decode()
    assert '    @__import__("pytest").mark.skip(reason="why")\n    def test_x' in text
    assert "declared-skip" in classes
    with pytest.raises(SystemExit, match="declared skip names no test"):
        gen.rewrite_test_source(b"def test_y():\n    pass\n", "tests/fake/test_x.py", _mapping(), {"nope": "r"}, [])


def test_accepted_unmapped_filters_a_deliberate_gap_and_a_stale_row_fails(gen):
    mapping = _mapping()
    mapping["accepted_unmapped"] = [{"file": "tests/fake/test_x.py", "name": "job_kit.nowhere", "reason": "r"}]
    bad = ["tests/fake/test_x.py:3: job_kit.nowhere: job_kit exports no name 'nowhere'", "other: x"]
    assert gen.filter_accepted(mapping, bad) == ["other: x"]
    with pytest.raises(SystemExit, match="matches nothing"):
        gen.filter_accepted(mapping, ["other: x"])


# --------------------------------------------------------------------------
# edits
# --------------------------------------------------------------------------

TEST_FILE = "tests/fake/test_x.py"
TEST_TEXT = 'assert name == "old.name"\nassert other == "old.name"\n'


def _edit_map(count):
    row: dict[str, Any] = {"file": TEST_FILE, "old": 'name == "old.name"', "new": 'name == "new.name"', "reason": "moved"}
    if count is not None:
        row["count"] = count
    return {"test_text_edits": [row]}


def test_text_edit_applies_when_count_matches(gen):
    text, changed = gen.apply_text_edits(TEST_TEXT, TEST_FILE, _edit_map(1))
    assert changed is True
    assert text == 'assert name == "new.name"\nassert other == "old.name"\n'


def test_text_edit_default_count_is_one(gen):
    text, changed = gen.apply_text_edits(TEST_TEXT, TEST_FILE, _edit_map(None))
    assert changed is True and text.count("new.name") == 1


def test_text_edit_explicit_count_two(gen):
    mapping = {"test_text_edits": [{"file": TEST_FILE, "old": '"old.name"', "new": '"new.name"',
                                    "reason": "r", "count": 2}]}
    text, changed = gen.apply_text_edits(TEST_TEXT, TEST_FILE, mapping)
    assert changed and "old.name" not in text


@pytest.mark.parametrize("count", [0, 2])
def test_text_edit_fails_when_count_off_by_one(gen, count):
    with pytest.raises(SystemExit) as exc:
        gen.apply_text_edits(TEST_TEXT, TEST_FILE, _edit_map(count))
    assert f"expected {count}" in str(exc.value)


def test_text_edit_fails_when_old_absent(gen):
    with pytest.raises(SystemExit) as exc:
        gen.apply_text_edits("nothing relevant\n", TEST_FILE, _edit_map(None))
    assert "matches 0 time(s), expected 1" in str(exc.value)


def test_text_edit_ignores_other_files(gen):
    text, changed = gen.apply_text_edits("nothing\n", "tests/other.py", _edit_map(None))
    assert (text, changed) == ("nothing\n", False)


def test_wording_edits_apply_everywhere_and_report_whether_they_did(gen):
    mapping = {"wording_edits": [{"old": "update job-kit", "new": "update llm-kit", "reason": "r"}]}
    assert gen.apply_wording_edits('assert "update job-kit" in m', "any.py", mapping) == (
        'assert "update llm-kit" in m', True)
    assert gen.apply_wording_edits("nothing", "any.py", mapping) == ("nothing", False)


# --------------------------------------------------------------------------
# the alias tree
# --------------------------------------------------------------------------


@pytest.fixture()
def alias_tree(gen, tmp_path, monkeypatch):
    """Write the synthetic alias tree and a synthetic final package, importable from tmp_path."""
    for rel, (data, _classes) in gen.build_facades(_mapping()).items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    final = tmp_path / "llm_kit" / "final"
    (final / "sub").mkdir(parents=True)
    (tmp_path / "llm_kit" / "__init__.py").write_text("", encoding="utf-8")
    (final / "__init__.py").write_text("", encoding="utf-8")
    (final / "thing.py").write_text("thing = 'T'\nmod_function = 'F'\nX = 'x'\n", encoding="utf-8")
    (final / "other.py").write_text("Original = 'O'\n", encoding="utf-8")
    (final / "mod.py").write_text("MODULE = True\n", encoding="utf-8")
    (final / "sub" / "__init__.py").write_text("PKG = 'P'\n", encoding="utf-8")
    (final / "sub" / "leaf.py").write_text("LEAF = 'L'\n", encoding="utf-8")
    (final / "cli_a.py").write_text("shared = 'a'\nonly_a = 1\n", encoding="utf-8")
    (final / "cli_b.py").write_text("shared = 'b'\nonly_b = 2\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [n for n in sys.modules if n.split(".")[0] in ("legacy_names", "llm_kit")]:
        monkeypatch.delitem(sys.modules, name)
    yield tmp_path
    for name in [n for n in sys.modules if n.split(".")[0] in ("legacy_names", "llm_kit")]:
        sys.modules.pop(name, None)


def test_the_alias_tree_resolves_names_children_and_constants(alias_tree):
    root = importlib.import_module("legacy_names.job_kit")
    assert root.renamed == "O" and root.__version__ == "9.9.9"
    assert root.thing == "T"                                   # a name the facade binds
    assert root.mod == "F"                                     # beats the child module of the same name
    assert root.sub is importlib.import_module("llm_kit.final.sub")
    with pytest.raises(AttributeError):
        root.nowhere
    sub = importlib.import_module("legacy_names.job_kit.sub")
    assert sub.PKG == "P"                                      # delegation to the final package
    assert sub.leaf is importlib.import_module("llm_kit.final.sub.leaf")


def test_a_forwarded_view_reads_from_the_first_home_and_writes_to_every_owner(alias_tree):
    cli = importlib.import_module("legacy_names.job_kit.cli")
    a = importlib.import_module("llm_kit.final.cli_a")
    b = importlib.import_module("llm_kit.final.cli_b")
    assert cli.shared == "a" and cli.only_b == 2
    cli.shared = "patched"
    assert (a.shared, b.shared) == ("patched", "patched")
    del cli.shared
    assert not hasattr(a, "shared") and not hasattr(b, "shared")
    with pytest.raises(AttributeError):
        cli.nowhere


def test_a_forwarded_main_translates_a_legacy_verb(alias_tree, monkeypatch):
    seen = []
    entry = types_module = importlib.import_module("types")
    fake = entry.ModuleType("llm_kit.cli")
    fake.main = lambda argv=None: seen.append(argv) or 7
    monkeypatch.setitem(sys.modules, "llm_kit.cli", fake)
    cli = importlib.import_module("legacy_names.job_kit.cli")
    assert cli.main(["go", "--flag"]) == 7
    assert cli.main(["other"]) == 7
    assert seen == [["grp", "go", "--flag"], ["other"]]
    assert types_module is entry


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


def test_check_reports_a_row_whose_final_module_does_not_exist(gen, tmp_path, monkeypatch):
    legacy = tmp_path / LEGACY_PKG / "thing.py"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"A = 1\n")
    mapping = {"schema": 2, "modules": {f"{LEGACY_PKG}/thing.py": {
        "kind": "module", "final": "llm_kit.final.thing", "legacy_sha256": gen._sha(b"A = 1\n")}}}
    (tmp_path / "packages/katsoft-llm-kit/src/llm_kit").mkdir(parents=True)
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"schema": 1, "entries": []}), encoding="utf-8")
    monkeypatch.setattr(gen, "REPO", tmp_path)
    monkeypatch.setattr(gen, "LLM_KIT_LIB", tmp_path / "packages/katsoft-llm-kit/src")
    monkeypatch.setattr(gen, "PORTING_LEDGER", ledger)
    monkeypatch.setattr(gen, "load_map", lambda: mapping)
    monkeypatch.setattr(gen, "legacy_package_files", lambda _m: [
        {"legacy": f"{LEGACY_PKG}/thing.py", "package": "job_kit", "state": "module"}])
    problems = gen.check_problems()
    assert any("final module llm_kit.final.thing does not exist" in p for p in problems), problems
    final = tmp_path / "packages/katsoft-llm-kit/src/llm_kit/final/thing.py"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"A = 1\n")
    assert gen.check_problems() == []


def test_check_reports_a_facade_name_the_final_module_does_not_bind(gen, tmp_path, monkeypatch):
    path = f"{LEGACY_PKG}/__init__.py"
    (tmp_path / LEGACY_PKG).mkdir(parents=True)
    (tmp_path / path).write_bytes(b"")
    mapping = {"schema": 2, "modules": {path: {
        "kind": "facade-names", "names": {"x": ["llm_kit.final.thing", "missing"]},
        "legacy_sha256": gen._sha(b"")}}}
    final = tmp_path / "packages/katsoft-llm-kit/src/llm_kit/final/thing.py"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"present = 1\n")
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"schema": 1, "entries": []}), encoding="utf-8")
    monkeypatch.setattr(gen, "REPO", tmp_path)
    monkeypatch.setattr(gen, "LLM_KIT_LIB", tmp_path / "packages/katsoft-llm-kit/src")
    monkeypatch.setattr(gen, "PORTING_LEDGER", ledger)
    monkeypatch.setattr(gen, "load_map", lambda: mapping)
    monkeypatch.setattr(gen, "legacy_package_files", lambda _m: [
        {"legacy": path, "package": "job_kit", "state": "module"}])
    problems = gen.check_problems()
    assert any("binds no 'missing'" in p for p in problems), problems
