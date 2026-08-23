"""``extensible`` and the merge algebra."""

from pathlib import Path
from typing import Callable

from yaml_data_editor_kit.schema import (
    Profile,
    errors_only,
    flatten_type,
    load_corpus,
    load_profile,
    merge_values,
    validate_corpus,
)

# The `write` fixture's signature. Named locally on purpose: importing it
# from conftest would resolve by module name, and every tests/<plugin>/
# directory in this repo has one.
Writer = Callable[[str, str], Path]

TEMPLATE_PROFILE = """
dialect: type/1
id: template
identified_by: name
fields:
  name:    { type: id }
  label:   { type: string }
  limits:  { type: map, key: { type: id }, value: { type: int } }
  tags:    { type: list, of: { type: string }, required: false }
extensible:
  via: extends
  abstract_flag: abstract
---
dialect: source/1
of: template
layout: rows
path: content/templates.yaml
"""


def _load(tmp_path: Path, profile_dir: Path, write: Writer, corpus_text: str) -> Profile:
    write("profile/template.yaml", TEMPLATE_PROFILE)
    write("content/templates.yaml", corpus_text)
    return load_profile(profile_dir)


def test_a_map_is_merged_by_key_and_a_list_is_replaced(tmp_path, profile_dir, write) -> None:
    profile = _load(
        tmp_path,
        profile_dir,
        write,
        """
- name: base
  abstract: true
  label: Base
  limits: { width: 4, height: 8, depth: 2 }
  tags: [alpha, beta]
- name: wide
  extends: base
  limits: { width: 12 }
  tags: [gamma]
""",
    )
    corpus = load_corpus(profile, tmp_path)
    flattened, problems = flatten_type(profile.types["template"], corpus)
    assert problems == []
    # The map keeps every key the parent set -- this is the case that makes
    # the construct work at all.
    assert flattened["wide"]["limits"] == {"width": 12, "height": 8, "depth": 2}
    # The list is replaced entirely.
    assert flattened["wide"]["tags"] == ["gamma"]
    # The scalar is replaced by the child, and an unstated one is inherited.
    assert flattened["wide"]["label"] == "Base"


def test_a_required_field_is_checked_after_flattening(tmp_path, profile_dir, write) -> None:
    profile = _load(
        tmp_path,
        profile_dir,
        write,
        """
- name: base
  label: Base
  limits: { width: 4 }
- name: narrow
  extends: base
  limits: { width: 1 }
""",
    )
    # `narrow` restates nothing but `limits`, and inherits `label`.
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_a_record_missing_a_required_field_with_no_parent_is_reported(
    tmp_path, profile_dir, write
) -> None:
    profile = _load(
        tmp_path,
        profile_dir,
        write,
        """
- name: orphan
  limits: { width: 1 }
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "content/templates.yaml"
    assert problems[0].record == "orphan"
    assert problems[0].field == "label"
    assert "required but absent" in problems[0].message


def test_a_chain_applies_the_deepest_ancestor_first(tmp_path, profile_dir, write) -> None:
    profile = _load(
        tmp_path,
        profile_dir,
        write,
        """
- name: base
  label: Base
  limits: { width: 1, height: 1 }
- name: middle
  extends: base
  limits: { height: 2 }
- name: leaf
  extends: middle
  limits: { width: 3 }
""",
    )
    corpus = load_corpus(profile, tmp_path)
    flattened, problems = flatten_type(profile.types["template"], corpus)
    assert problems == []
    assert flattened["leaf"]["limits"] == {"width": 3, "height": 2}
    assert flattened["leaf"]["label"] == "Base"


def test_a_cycle_names_every_record_in_it(tmp_path, profile_dir, write) -> None:
    profile = _load(
        tmp_path,
        profile_dir,
        write,
        """
- name: first
  extends: second
  label: First
  limits: {}
- name: second
  extends: first
  label: Second
  limits: {}
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    cycles = [p for p in problems if "cycle" in p.message]
    assert cycles
    for problem in cycles:
        assert problem.file == "content/templates.yaml"
        assert problem.field == "extends"
        assert "first" in problem.message and "second" in problem.message


def test_a_dangling_parent_is_reported_once_naming_record_and_field(
    tmp_path, profile_dir, write
) -> None:
    profile = _load(
        tmp_path,
        profile_dir,
        write,
        """
- name: leaf
  extends: absent_base
  label: Leaf
  limits: {}
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    dangling = [p for p in problems if "names no record" in p.message]
    # Reported exactly once: `extends` is a ref, so the ref check owns it.
    assert len(dangling) == 1
    assert dangling[0].record == "leaf"
    assert dangling[0].field == "extends"
    assert dangling[0].file == "content/templates.yaml"


def test_abstract_does_not_exempt_a_record_from_validation(tmp_path, profile_dir, write) -> None:
    profile = _load(
        tmp_path,
        profile_dir,
        write,
        """
- name: base
  abstract: true
  limits: {}
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert [(p.record, p.field) for p in problems] == [("base", "label")]


def test_deletion_is_not_expressible(tmp_path, profile_dir, write) -> None:
    profile = _load(
        tmp_path,
        profile_dir,
        write,
        """
- name: base
  label: Base
  limits: { width: 4, height: 8 }
- name: child
  extends: base
  limits: { width: 4 }
""",
    )
    corpus = load_corpus(profile, tmp_path)
    flattened, _ = flatten_type(profile.types["template"], corpus)
    assert "height" in flattened["child"]["limits"]


def test_merge_values_merges_nested_records_field_by_field() -> None:
    parent = {"box": {"inner": {"a": 1, "b": 2}}}
    child = {"box": {"inner": {"b": 3}}}
    assert merge_values(parent, child) == {"box": {"inner": {"a": 1, "b": 3}}}
