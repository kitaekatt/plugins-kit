"""Shader-language extensions are source the lanes do not read: listed as source, unmeasured."""

from __future__ import annotations

from pathlib import Path

from clean_code_kit.languages import KIND_SOURCE_UNMEASURED, SOURCE_EXTENSIONS, kind_of

SHADER_EXTENSIONS = (".gdshader", ".glsl", ".hlsl", ".shader", ".vert", ".frag", ".comp", ".wgsl", ".metal")


def test_shader_extensions_are_source_unmeasured():
    assert all(ext in SOURCE_EXTENSIONS for ext in SHADER_EXTENSIONS)
    assert {ext: kind_of(ext) for ext in SHADER_EXTENSIONS} == {
        ext: KIND_SOURCE_UNMEASURED for ext in SHADER_EXTENSIONS}


import pytest
import yaml

from clean_code_kit import config as config_mod
from clean_code_kit import coverage, languages, parsers


def test_config_accepts_gdscript_and_cpp(tmp_path):
    text = ("schema: 1\nsnapshot: head\nlanguages: [gdscript, cpp]\nsource_roots: [src]\n"
            "test_roots: []\nexclude: []\nlanes:\n"
            "  duplication: {enabled: true, threshold: 0.82, min_lines: 4, group_identical: true}\n"
            "  complexity: {enabled: true, coverage: false, top: 50}\n")
    cfg = config_mod.parse(Path("c.yaml"), yaml.safe_load(text))
    assert cfg.languages == ["gdscript", "cpp"]


def test_duplication_lane_reads_javascript_gdscript_and_cpp():
    assert set(languages.LANE_LANGUAGES["duplication"]) == {
        "python", "typescript", "javascript", "gdscript", "cpp"}


def test_complexity_lane_reads_gdscript_and_cpp():
    assert set(languages.LANE_LANGUAGES["complexity"]) == {
        "python", "typescript", "javascript", "gdscript", "cpp"}


def test_duplication_cpp_extensions_exclude_the_complexity_only_ones():
    dup = languages.lane_extensions("duplication")
    assert {".cpp", ".cc", ".cxx", ".hpp", ".hh", ".h", ".gd", ".js"} <= dup
    assert not {".c++", ".hxx"} & dup


def test_complexity_cpp_extensions_include_cplusplus_and_hxx():
    comp = languages.lane_extensions("complexity")
    assert {".cpp", ".cc", ".cxx", ".c++", ".h", ".hpp", ".hh", ".hxx", ".gd"} <= comp


def test_pruning_table_owns_the_union_of_each_languages_lane_extensions():
    assert {".c++", ".hxx", ".cpp"} <= set(languages.EXTENSIONS["cpp"])
    assert languages.unconfigured_extensions(["python"]) >= {".gd", ".cpp", ".hxx", ".c++"}
    assert not {".gd", ".cpp"} & languages.unconfigured_extensions(["gdscript", "cpp"])


def test_dryer_ignores_cplusplus_only_extensions_in_the_ignored_by_count():
    assert coverage.ignored_by("duplication", ["a.c++", "b.hxx", "c.cpp"]) == {".c++": 1, ".hxx": 1}
    assert coverage.ignored_by("complexity", ["a.c++", "b.hxx"]) == {}


@pytest.mark.parametrize("lane", ["duplication", "complexity"])
@pytest.mark.parametrize("rel", [
    "godot/tests/_brush_down_check.gd", "src/test/a.cpp", "src/__tests__/a.js", "src/spec/a.ts",
    "src/a.test.js", "src/a.spec.mjs", "src/a.test.ts", "src/a.spec.tsx",
    "src/test_a.py", "src/a_test.py", "src/conftest.py"])
def test_both_tools_skip_test_dirs_and_test_names(lane, rel):
    assert languages.lane_skips(lane, rel)


@pytest.mark.parametrize("lane", ["duplication", "complexity"])
@pytest.mark.parametrize("rel", [
    "godot/scripts/a.gd", "src/a.js", "src/contest.py", "src/attest.py", "src/a.test.gd",
    "src/latest/a.gd", "src/a.test"])
def test_ordinary_files_are_not_tool_skipped(lane, rel):
    assert not languages.lane_skips(lane, rel)


def test_select_keeps_tool_skipped_files_out_of_the_measured_selection(tmp_path):
    (tmp_path / "godot" / "tests").mkdir(parents=True)
    (tmp_path / "godot" / "main.gd").write_text("x\n")
    (tmp_path / "godot" / "tests" / "t.gd").write_text("x\n")
    (tmp_path / "godot" / "a.test.js").write_text("x\n")
    lanes = ["duplication", "complexity"]
    picked = coverage.select(tmp_path, ["godot"], lanes)
    skipped = coverage.select_skipped(tmp_path, ["godot"], lanes)
    for lane in lanes:
        assert picked[lane] == ["godot/main.gd"], lane
        assert skipped[lane] == ["godot/a.test.js", "godot/tests/t.gd"], lane


def test_table_reports_skipped_counts_separately_from_measured():
    selection = {"duplication": ["g/a.gd"], "complexity": ["g/a.gd"]}
    skipped = {"duplication": ["g/tests/t.gd", "g/tests/u.gd"], "complexity": ["g/tests/t.gd", "g/tests/u.gd"]}
    row = coverage.table(["g/a.gd", "g/tests/t.gd", "g/tests/u.gd"], selection, skipped)["by_extension"][".gd"]
    assert row["tracked"] == 3
    assert row["measured"] == {"duplication": 1, "complexity": 1}
    assert row["skipped"] == {"duplication": 2, "complexity": 2}


def test_parser_prefetch_covers_every_configurable_language():
    assert {"python", "typescript", "javascript", "gdscript", "cpp"} <= set(parsers.GRAMMARS)
