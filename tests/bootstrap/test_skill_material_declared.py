"""Tests for bootstrap_lib.skill_material -- the declared-resource rows.

A `full` ref loads the resources the skill itself declares: the `path` of
each record in a `references` list, in the fenced YAML blocks of its
SKILL.md. These tests pin where a declaration is found, the order and the
termination of the walk that finds it, how a declared path is resolved, and
what the report records.

The alias, merge and cycle rows assert on the RAW declaration walk
(`_raw_declarations`), before repeated strings are folded: only there does
the identity guard show.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

from bootstrap_lib import skill_material as sm


MODULE_PATH = Path(sm.__file__)
REPO_ROOT = MODULE_PATH.parents[3]
REPO_SKILLS = sorted(REPO_ROOT.glob("plugins/*/skills/*/SKILL.md"))

BIG = 1_000_000
FENCE = "```"


def fenced(block: str, tag: str = "yaml") -> str:
    return f"{FENCE}{tag}\n{block.rstrip()}\n{FENCE}"


def write_skill(root, dirname="alpha", *, body="Body.", files=None, name=None):
    skill_dir = Path(root) / dirname
    skill_dir.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name or dirname}\ndescription: Alpha does A.\n---\n\n{body}\n"
    (skill_dir / "SKILL.md").write_bytes(text.encode("utf-8"))
    for relative, content in (files or {}).items():
        target = skill_dir.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    return skill_dir


def declaring(root, paths, dirname="alpha", *, files=None, extra_body=""):
    """A skill whose one YAML block declares `paths` at top level."""
    records = "\n".join(f"  - id: r{index}\n    path: {path}" for index, path in enumerate(paths))
    body = "# Skill\n\n" + fenced("references:\n" + records) + extra_body
    return write_skill(root, dirname, body=body, files=files)


def render(*refs, budget=BIG, **kwargs):
    return sm.materialize(
        sm.SkillSelection(skills=tuple(refs), token_budget=budget), **kwargs
    )


def rendered_paths(result, index=0):
    return [item.path for item in result.report.skills[index].resources]


def walk(block: str):
    return sm._raw_declarations(yaml.safe_load(block))


# --------------------------------------------------------------------------
# The default, and the switch
# --------------------------------------------------------------------------


def test_full_ref_loads_declared_resources_by_default(tmp_path) -> None:
    skill = declaring(
        tmp_path, ["references/a.md", "b.md"],
        files={"references/a.md": "A text.", "b.md": "B text.", "undeclared.md": "No."},
    )
    assert sm.SkillRef(str(skill)).declared_resources is True
    result = render(sm.SkillRef(str(skill)))
    assert rendered_paths(result) == ["references/a.md", "b.md"]
    assert (
        "</instructions>\n"
        '<resource path="references/a.md">\nA text.\n</resource>\n'
        '<resource path="b.md">\nB text.\n</resource>\n'
        "</skill>"
    ) in result.text
    assert "undeclared.md" not in result.text


def test_declared_false_loads_only_named_resources(tmp_path) -> None:
    skill = declaring(
        tmp_path, ["a.md", "b.md", "c.md"],
        files={"a.md": "A", "b.md": "B", "c.md": "C", "other.md": "O"},
    )
    result = render(
        sm.SkillRef(str(skill), declared_resources=False, resources=("b.md", "other.md"))
    )
    assert rendered_paths(result) == ["b.md", "other.md"]
    assert [item.declared for item in result.report.skills[0].resources] == [False, False]
    assert result.report.skills[0].declared == ("a.md", "b.md", "c.md")
    none = render(sm.SkillRef(str(skill), declared_resources=False))
    assert rendered_paths(none) == []
    assert "<resource" not in none.text


# --------------------------------------------------------------------------
# Where a declaration is found
# --------------------------------------------------------------------------


TOP_LEVEL = """
references:
  - id: a
    path: references/top.md
    summary: top
"""

UNDER_TYPE_ROOT = """
reference_skill:
  _schema_version: "1"
  facts: []
  references:
    - id: a
      path: references/nested.md
"""

UNDER_INDEX = """
domain_skill:
  index:
    groupings: []
    references:
      - id: a
        path: references/index.md
"""


def test_declared_records_found_top_level_nested_and_under_index(tmp_path) -> None:
    expected = {
        "top": (TOP_LEVEL, ("references/top.md",)),
        "nested": (UNDER_TYPE_ROOT, ("references/nested.md",)),
        "index": (UNDER_INDEX, ("references/index.md",)),
    }
    for dirname, (block, declared) in expected.items():
        skill = write_skill(tmp_path, dirname, body=fenced(block))
        assert sm.read_skill(skill).declared == declared, dirname


def test_string_reference_items_declare_nothing(tmp_path) -> None:
    block = """
capability_skill:
  layering:
    references:
      - "handoff-template.md -- how to fill in the handoff"
      - references/looks-like-a-path.md
"""
    skill = write_skill(tmp_path, body=fenced(block))
    assert sm.read_skill(skill).declared == ()
    assert walk(block) == []


def test_reference_record_without_path_declares_nothing(tmp_path) -> None:
    block = """
references:
  - id: a
    summary: no path here
  - id: b
    path: 12
  - id: c
    path: [x.md]
  - id: d
    path: kept.md
"""
    skill = write_skill(tmp_path, body=fenced(block))
    assert sm.read_skill(skill).declared == ("kept.md",)


def test_other_keys_declare_nothing(tmp_path) -> None:
    block = """
resources:
  - path: a.md
reference:
  - path: b.md
References:
  - path: c.md
refs:
  references_list:
    - path: d.md
references:
  path: e.md
path: f.md
"""
    skill = write_skill(tmp_path, body=fenced(block))
    assert sm.read_skill(skill).declared == ()
    assert walk(block) == []


# --------------------------------------------------------------------------
# The order of the walk
# --------------------------------------------------------------------------


def test_declared_order_list_records_before_nested() -> None:
    block = """
references:
  - path: a.md
    references:
      - path: nested.md
  - path: b.md
"""
    assert walk(block) == ["a.md", "b.md", "nested.md"]


def test_declared_order_follows_mapping_order() -> None:
    block = """
z:
  references:
    - path: z1.md
    - path: z2.md
a:
  references:
    - path: a1.md
m:
  - references:
      - path: m1.md
"""
    assert walk(block) == ["z1.md", "z2.md", "a1.md", "m1.md"]


def test_declared_order_follows_block_order(tmp_path) -> None:
    body = "\n\n".join([
        fenced("references:\n  - path: second-letter-z.md"),
        "Prose between the blocks.",
        fenced("references:\n  - path: first-letter-a.md"),
        fenced("references:\n  - path: middle.md"),
    ])
    skill = write_skill(tmp_path, body=body)
    assert sm.read_skill(skill).declared == (
        "second-letter-z.md", "first-letter-a.md", "middle.md",
    )


# --------------------------------------------------------------------------
# Aliases, merge keys, cycles: the identity guard, seen on the raw walk
# --------------------------------------------------------------------------


def test_aliased_references_list_yields_one_raw_entry() -> None:
    block = """
a:
  references: &r
    - path: x.md
b:
  references: *r
"""
    parsed = yaml.safe_load(block)
    assert parsed["b"]["references"] is parsed["a"]["references"]
    assert sm._raw_declarations(parsed) == ["x.md"]


def test_merged_references_list_yields_one_raw_entry() -> None:
    block = """
base: &b
  references:
    - path: x.md
other:
  <<: *b
  extra: 1
"""
    parsed = yaml.safe_load(block)
    assert parsed["other"] is not parsed["base"]
    assert parsed["other"]["references"] is parsed["base"]["references"]
    assert sm._raw_declarations(parsed) == ["x.md"]


class _Bounded(dict):
    """A mapping that fails once it has been entered more often than any
    terminating walk could enter it."""

    LIMIT = 50

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.entered = 0

    def items(self):
        self.entered += 1
        if self.entered > self.LIMIT:
            raise AssertionError("the walk entered one mapping more than 50 times")
        return super().items()


def test_recursive_yaml_terminates_with_one_raw_entry() -> None:
    block = """
a: &a
  references:
    - path: x.md
  self: *a
"""
    parsed = yaml.safe_load(block)
    assert parsed["a"]["self"] is parsed["a"]
    # The same cycle, in a mapping that counts how often it is entered: a
    # walk that does not end is stopped by the count, not by a clock.
    cyclic = _Bounded(parsed["a"])
    cyclic["self"] = cyclic
    assert sm._raw_declarations({"a": cyclic}) == ["x.md"]
    assert cyclic.entered == 1
    # And the parsed structure itself.
    assert sm._raw_declarations(parsed) == ["x.md"]


class _BoundedList(list):
    LIMIT = 50

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.walked = 0

    def __iter__(self):
        self.walked += 1
        if self.walked > self.LIMIT:
            raise AssertionError("the walk iterated one list more than 50 times")
        return super().__iter__()


def test_recursive_list_terminates() -> None:
    parsed = yaml.safe_load("references: &r\n  - path: x.md\n  - *r\n")
    assert parsed["references"][1] is parsed["references"]
    cyclic = _BoundedList([{"path": "x.md"}])
    cyclic.append(cyclic)
    assert sm._raw_declarations({"references": cyclic}) == ["x.md"]
    # Once to record its items, once to walk them.
    assert cyclic.walked == 2


def test_two_records_naming_one_path_give_two_raw_entries_one_declared(tmp_path) -> None:
    block = """
first:
  references:
    - path: x.md
second:
  references:
    - path: x.md
    - path: y.md
"""
    assert walk(block) == ["x.md", "x.md", "y.md"]
    assert sm._unique_in_order(["x.md", "x.md", "y.md", "x.md"]) == ["x.md", "y.md"]
    skill = write_skill(tmp_path, body=fenced(block))
    assert sm.read_skill(skill).declared == ("x.md", "y.md")


def test_alias_walk_order_is_first_position() -> None:
    block = """
one:
  references:
    - path: a.md
two:
  references: &r
    - path: b.md
    - path: c.md
three:
  references: *r
four:
  references:
    - path: d.md
"""
    assert walk(block) == ["a.md", "b.md", "c.md", "d.md"]


def test_list_entered_before_its_references_key_is_still_recorded() -> None:
    block = """
pool: &r
  - path: x.md
uses:
  references: *r
again:
  references: *r
"""
    assert walk(block) == ["x.md"]


def test_deeply_nested_block_does_not_hit_the_recursion_limit() -> None:
    depth = sys.getrecursionlimit() + 500
    value = {"references": [{"path": "deep.md"}]}
    for level in range(depth):
        value = {"level": value} if level % 2 else [value]
    assert sm._raw_declarations(value) == ["deep.md"]


def test_scalars_and_empty_blocks_declare_nothing() -> None:
    for value in (None, "text", 3, [], {}, [1, "a", None]):
        assert sm._raw_declarations(value) == []


# --------------------------------------------------------------------------
# Fences, and blocks PyYAML does not parse
# --------------------------------------------------------------------------


def test_multi_document_block_is_counted_unparsed(tmp_path) -> None:
    block = "references:\n  - path: a.md\n---\nreferences:\n  - path: b.md"
    skill = write_skill(tmp_path, body=fenced(block))
    document = sm.read_skill(skill)
    assert document.declared == ()
    assert document.unparsed_yaml_blocks == 1


def test_yml_fence_is_read(tmp_path) -> None:
    body = "\n\n".join([
        fenced("references:\n  - path: yml.md", tag="yml"),
        fenced("references:\n  - path: trailing.md", tag="yaml  \t"),
        fenced("references:\n  - path: json.md", tag="json"),
        fenced("references:\n  - path: bare.md", tag=""),
        fenced("references:\n  - path: upper.md", tag="YAML"),
        "    " + fenced("references:\n  - path: indented.md").replace("\n", "\n    "),
    ])
    skill = write_skill(tmp_path, body=body)
    document = sm.read_skill(skill)
    assert document.declared == ("yml.md", "trailing.md")
    assert document.unparsed_yaml_blocks == 0


def test_unterminated_fence_declares_nothing_and_is_counted(tmp_path) -> None:
    body = (
        fenced("references:\n  - path: closed.md")
        + "\n\nThe next fence never closes.\n\n```yaml\nreferences:\n  - path: open.md"
    )
    skill = write_skill(tmp_path, body=body)
    document = sm.read_skill(skill)
    assert document.declared == ("closed.md",)
    assert document.unparsed_yaml_blocks == 1


def test_unparsed_yaml_block_is_counted_not_fatal(tmp_path) -> None:
    body = "\n\n".join([
        fenced("bad: mapping: values are not allowed here"),
        fenced("references:\n  - path: good.md"),
        fenced("also: [unclosed"),
    ])
    skill = write_skill(tmp_path, body=body, files={"good.md": "Good."})
    document = sm.read_skill(skill)
    assert document.declared == ("good.md",)
    assert document.unparsed_yaml_blocks == 2
    assert rendered_paths(render(sm.SkillRef(str(skill)))) == ["good.md"]


def test_unparsed_block_count_is_in_the_report(tmp_path) -> None:
    body = fenced("bad: mapping: values are not allowed here")
    skill = write_skill(tmp_path, body=body)
    clean = write_skill(tmp_path, "clean")
    report = render(sm.SkillRef(str(skill)), sm.SkillRef(str(clean), level="catalog")).report
    assert [item.unparsed_yaml_blocks for item in report.skills] == [1, 0]
    assert [item["unparsed_yaml_blocks"] for item in report.to_json()["skills"]] == [1, 0]


# --------------------------------------------------------------------------
# Resolving a declared path
# --------------------------------------------------------------------------


def test_declared_directory_expands_sorted_recursive(tmp_path) -> None:
    # A top-down directory walk meets z.md and m.md before a/b.md; the
    # sorted POSIX order puts a/b.md first.
    skill = declaring(
        tmp_path, ["references/", "single.md"],
        files={
            "references/z.md": "Z",
            "references/m.md": "M",
            "references/a/b.md": "AB",
            "references/a/deep/c.md": "ADC",
            "single.md": "S",
        },
    )
    result = render(sm.SkillRef(str(skill)))
    assert rendered_paths(result) == [
        "references/a/b.md",
        "references/a/deep/c.md",
        "references/m.md",
        "references/z.md",
        "single.md",
    ]
    assert all(item.declared for item in result.report.skills[0].resources)
    assert result.report.skills[0].declared == ("references/", "single.md")


def test_trailing_slash_allowed_declared_refused_named(tmp_path) -> None:
    skill = declaring(tmp_path, ["references/"], files={"references/a.md": "A"})
    assert rendered_paths(render(sm.SkillRef(str(skill)))) == ["references/a.md"]
    with pytest.raises(sm.SkillMaterialError, match=r"'references/' ends with '/'; a named resource is one regular file"):
        sm.SkillRef(str(skill), resources=("references/",))
    doubled = declaring(tmp_path, ["references//"], "doubled", files={"references/a.md": "A"})
    with pytest.raises(sm.SkillMaterialError, match="has an empty segment"):
        render(sm.SkillRef(str(doubled)))


def test_declared_missing_file_refused_names_skill_and_record(tmp_path) -> None:
    skill = declaring(tmp_path, ["present.md", "references/missing.md"], files={"present.md": "P"})
    # The refusal names the first ref that switched declarations on: here the
    # catalog ref, whose declared_resources defaults to True.
    with pytest.raises(sm.SkillMaterialError) as caught:
        render(sm.SkillRef(str(skill), level="catalog"), sm.SkillRef(str(skill)))
    assert str(caught.value) == (
        "skills[0]: skill 'alpha' declares resource 'references/missing.md', "
        "which does not exist. To load this skill without it, set "
        "declared_resources=False on the ref and name the files wanted in "
        "resources."
    )


def test_declared_path_outside_skill_refused(tmp_path, monkeypatch) -> None:
    skill = declaring(
        tmp_path, ["a.md", "references/"],
        files={"a.md": "A", "references/b.md": "B", "references/c.md": "C"},
    )
    real = sm._contained
    monkeypatch.setattr(sm, "_contained", lambda root, candidate: candidate.name != "a.md" and real(root, candidate))
    with pytest.raises(sm.SkillMaterialError) as caught:
        render(sm.SkillRef(str(skill)))
    assert str(caught.value).startswith(
        "skills[0]: skill 'alpha' declares resource 'a.md', which resolves "
        "outside the skill directory. To load this skill without it, set "
        "declared_resources=False"
    )
    # A file found by expanding a declared directory passes the same check.
    monkeypatch.setattr(sm, "_contained", lambda root, candidate: candidate.name != "c.md" and real(root, candidate))
    with pytest.raises(sm.SkillMaterialError) as caught:
        render(sm.SkillRef(str(skill)))
    assert str(caught.value).startswith(
        "skills[0]: skill 'alpha' declares resource 'references/', which "
        "holds 'references/c.md', which resolves outside the skill directory."
    )


def test_declared_path_through_a_link_outside_the_skill_refused(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    skill = declaring(tmp_path / "skills", ["linked/secret.md"])
    link = skill / "linked"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if sys.platform != "win32":
            pytest.skip("this host cannot create a directory link")
        import _winapi

        try:
            _winapi.CreateJunction(str(outside), str(link))
        except OSError:
            pytest.skip("this host cannot create a directory link")
    with pytest.raises(sm.SkillMaterialError, match="declares resource 'linked/secret.md', which resolves outside the skill directory"):
        render(sm.SkillRef(str(skill)))


@pytest.mark.parametrize(
    "path, reason",
    [
        ("../x.md", r"has a '\.\.' segment"),
        ("/abs.md", "is absolute"),
        ("'a b.md'", "has a segment outside"),
        ("'C:/x.md'", "names a drive"),
        ("'back\\slash.md'", "contains a backslash"),
        ("''", "a resource path is empty"),
    ],
)
def test_declared_bad_grammar_refused(tmp_path, path, reason) -> None:
    skill = declaring(tmp_path, [path])
    # Reading the skill lists the declaration as written; loading refuses.
    assert len(sm.read_skill(skill).declared) == 1
    with pytest.raises(sm.SkillMaterialError) as caught:
        render(sm.SkillRef(str(skill)))
    message = str(caught.value)
    assert message.startswith("skills[0]: skill 'alpha' declares resource ")
    assert "which is refused (" in message
    assert __import__("re").search(reason, message), message
    assert "set declared_resources=False on the ref" in message


def test_bad_declared_path_ignored_when_not_loading(tmp_path) -> None:
    skill = declaring(tmp_path, ["../x.md", "missing.md"], files={"ok.md": "OK"})
    declared = ("../x.md", "missing.md")
    switched_off = render(sm.SkillRef(str(skill), declared_resources=False, resources=("ok.md",)))
    assert rendered_paths(switched_off) == ["ok.md"]
    assert switched_off.report.skills[0].declared == declared
    catalog = render(sm.SkillRef(str(skill), level="catalog"))
    assert catalog.report.skills[0].declared == declared
    assert sm.read_skill(skill).declared == declared


def test_declared_then_named_order(tmp_path) -> None:
    skill = declaring(
        tmp_path, ["m.md", "b.md"],
        files={"m.md": "M", "b.md": "B", "z.md": "Z", "a.md": "A"},
    )
    result = render(sm.SkillRef(str(skill), resources=("z.md", "a.md")))
    assert rendered_paths(result) == ["m.md", "b.md", "z.md", "a.md"]
    assert [item.declared for item in result.report.skills[0].resources] == [
        True, True, False, False,
    ]


def test_declared_and_named_same_file_rendered_once(tmp_path) -> None:
    skill = declaring(
        tmp_path, ["a.md", "references/", "references/b.md"],
        files={"a.md": "A", "references/b.md": "B", "new.md": "N"},
    )
    result = render(sm.SkillRef(str(skill), resources=("a.md", "new.md")))
    assert rendered_paths(result) == ["a.md", "references/b.md", "new.md"]
    assert result.text.count('<resource path="a.md">') == 1
    assert result.text.count('<resource path="references/b.md">') == 1
    assert [item.declared for item in result.report.skills[0].resources] == [True, True, False]
    assert result.report.suppressed == (
        sm.SuppressedRef(
            ref_index=0, name="alpha", reason="duplicate-resource",
            kept_index=0, detail="references/b.md",
        ),
        sm.SuppressedRef(
            ref_index=0, name="alpha", reason="duplicate-resource",
            kept_index=0, detail="a.md",
        ),
    )


def test_catalog_ref_renders_no_resources_and_reports_declared(tmp_path) -> None:
    skill = declaring(tmp_path, ["a.md", "gone.md"], files={"a.md": "A"})
    result = render(sm.SkillRef(str(skill), level="catalog"))
    assert "<resource" not in result.text
    assert "<instructions>" not in result.text
    assert result.report.skills[0].resources == ()
    assert result.report.skills[0].declared == ("a.md", "gone.md")
    assert result.report.to_json()["skills"][0]["declared"] == ["a.md", "gone.md"]


def test_provenance_lists_declared_and_marks_origin(tmp_path) -> None:
    skill = declaring(tmp_path, ["d.md"], files={"d.md": "Declared.", "n.md": "Named."})
    other = write_skill(tmp_path, "other")
    result = render(sm.SkillRef(str(other), level="catalog"), sm.SkillRef(str(skill), resources=("n.md",)))
    provenance = result.report.skills[1]
    assert provenance.declared == ("d.md",)
    assert [(item.path, item.declared, item.ref_index) for item in provenance.resources] == [
        ("d.md", True, 1), ("n.md", False, 1),
    ]
    assert [item.source for item in provenance.resources] == [
        str(skill.resolve() / "d.md"), str(skill.resolve() / "n.md"),
    ]
    assert [item["declared"] for item in result.report.to_json()["skills"][1]["resources"]] == [True, False]


def test_over_budget_itemizes_each_resource_and_names_both_remedies(tmp_path) -> None:
    skill = declaring(
        tmp_path, ["references/a.md"],
        files={"references/a.md": "a" * 400, "b.md": "b" * 200},
    )
    with pytest.raises(sm.SkillMaterialBudgetExceeded) as caught:
        render(sm.SkillRef(str(skill), resources=("b.md",)), budget=150)
    lines = str(caught.value).split("\n")
    assert lines[0] == "skill material is over budget: estimated 277 tokens (chars/4), budget 150."
    assert lines[1] == "  skill 'alpha' (full): 217"
    assert lines[2] == "    resource 'references/a.md': 112"
    assert lines[3] == "    resource 'b.md': 59"
    assert lines[4] == (
        'To fit: give a skill level "catalog", or set declared_resources=False '
        "on it and name a shorter list in resources. Nothing was truncated and "
        "nothing is returned."
    )
    assert len(lines) == 5


# --------------------------------------------------------------------------
# Merging refs to one skill
# --------------------------------------------------------------------------


def test_same_file_merge_declared_if_either(tmp_path) -> None:
    skill = declaring(tmp_path, ["a.md"], files={"a.md": "A", "n.md": "N"})
    off_then_on = render(
        sm.SkillRef(str(skill), declared_resources=False, resources=("n.md",)),
        sm.SkillRef(str(skill)),
    )
    assert rendered_paths(off_then_on) == ["a.md", "n.md"]
    assert [(item.declared, item.ref_index) for item in off_then_on.report.skills[0].resources] == [
        (True, 1), (False, 0),
    ]
    on_then_off = render(
        sm.SkillRef(str(skill)),
        sm.SkillRef(str(skill), declared_resources=False),
    )
    assert rendered_paths(on_then_off) == ["a.md"]
    both_off = render(
        sm.SkillRef(str(skill), declared_resources=False),
        sm.SkillRef(str(skill), declared_resources=False),
    )
    assert rendered_paths(both_off) == []


def test_catalog_ref_flag_loads_declared_on_merge(tmp_path) -> None:
    # Plan 2.4: for one SKILL.md named twice, declared_resources is true if
    # either ref sets it, a catalog ref included. A catalog ref's default is
    # True, so it switches the declared files on for a full ref that switched
    # them off, and the declared files are attributed to the ref that did.
    skill = declaring(tmp_path, ["a.md"], files={"a.md": "A", "n.md": "N"})
    catalog_first = render(
        sm.SkillRef(str(skill), level="catalog"),
        sm.SkillRef(str(skill), declared_resources=False, resources=("n.md",)),
    )
    assert catalog_first.report.skills[0].level == "full"
    assert rendered_paths(catalog_first) == ["a.md", "n.md"]
    assert [(item.declared, item.ref_index) for item in catalog_first.report.skills[0].resources] == [
        (True, 0), (False, 1),
    ]
    full_first = render(
        sm.SkillRef(str(skill), declared_resources=False),
        sm.SkillRef(str(skill), level="catalog"),
    )
    assert rendered_paths(full_first) == ["a.md"]
    assert [item.ref_index for item in full_first.report.skills[0].resources] == [1]
    both_off = render(
        sm.SkillRef(str(skill), level="catalog", declared_resources=False),
        sm.SkillRef(str(skill), declared_resources=False),
    )
    assert rendered_paths(both_off) == []
    # The flag of a catalog-only skill still renders nothing.
    assert rendered_paths(render(sm.SkillRef(str(skill), level="catalog"))) == []


def test_mirrored_copies_render_declared_resources_once(tmp_path) -> None:
    one = declaring(tmp_path / "one", ["a.md"], files={"a.md": "A"})
    two = declaring(tmp_path / "two", ["a.md"], files={"a.md": "A"})
    result = render(sm.SkillRef(str(one)), sm.SkillRef(str(two)))
    assert rendered_paths(result) == ["a.md"]
    assert [(item.reason, item.detail) for item in result.report.suppressed] == [
        ("same-content", ""), ("duplicate-resource", "a.md"),
    ]
    # Each copy's declared files resolve against THAT copy's directory: a
    # mirrored copy that does not ship a declared file is refused, by ref.
    lacking = declaring(tmp_path / "three", ["a.md"])
    with pytest.raises(
        sm.SkillMaterialError,
        match=r"^skills\[1\]: skill 'alpha' declares resource 'a\.md', which does not exist",
    ):
        render(sm.SkillRef(str(one)), sm.SkillRef(str(lacking)))


def test_catalog_ref_to_mirrored_copy_loads_that_copys_declared_files(tmp_path) -> None:
    # The flag is merged per skill directory, so a catalog ref to a mirrored
    # copy (default declared_resources=True) loads THAT copy's declared files
    # once the merged skill is full: equal files are rendered once, and a
    # copy that lacks a declared file is refused, naming the catalog ref.
    one = declaring(tmp_path / "one", ["a.md"], files={"a.md": "A"})
    two = declaring(tmp_path / "two", ["a.md"], files={"a.md": "A"})
    result = render(sm.SkillRef(str(one)), sm.SkillRef(str(two), level="catalog"))
    assert rendered_paths(result) == ["a.md"]
    assert [(item.reason, item.ref_index, item.detail) for item in result.report.suppressed] == [
        ("same-content", 1, ""), ("duplicate-resource", 1, "a.md"),
    ]
    lacking = declaring(tmp_path / "three", ["a.md"])
    with pytest.raises(
        sm.SkillMaterialError,
        match=r"^skills\[1\]: skill 'alpha' declares resource 'a\.md', which does not exist",
    ):
        render(sm.SkillRef(str(one)), sm.SkillRef(str(lacking), level="catalog"))
    # Switched off on the catalog ref, the copy contributes nothing.
    quiet = render(sm.SkillRef(str(one)), sm.SkillRef(str(lacking), level="catalog", declared_resources=False))
    assert rendered_paths(quiet) == ["a.md"]


# --------------------------------------------------------------------------
# The repo's own skills
# --------------------------------------------------------------------------


def test_every_repo_skill_materializes_full_with_declared() -> None:
    assert len(REPO_SKILLS) >= 20, len(REPO_SKILLS)
    declaring_skills = 0
    rendered_resources = 0
    expanded_directories = 0
    for path in REPO_SKILLS:
        result = render(sm.SkillRef(str(path)), budget=10_000_000)
        provenance = result.report.skills[0]
        assert provenance.level == "full", path
        if provenance.declared:
            declaring_skills += 1
            assert provenance.resources, path
        rendered_resources += len(provenance.resources)
        expanded_directories += sum(
            1 for written in provenance.declared if (path.parent / written).is_dir()
        )
        assert all(item.declared for item in provenance.resources), path
    # The corpus really exercises the rule: several skills declare, and at
    # least one declares a directory.
    assert declaring_skills >= 5, declaring_skills
    assert rendered_resources >= 40, rendered_resources
    assert expanded_directories >= 1, expanded_directories
    everything = render(*(sm.SkillRef(str(path)) for path in REPO_SKILLS), budget=10_000_000)
    assert len(everything.report.skills) == len(REPO_SKILLS)
    assert [item.reason for item in everything.report.suppressed if item.reason != "duplicate-resource"] == []
