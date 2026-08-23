"""``shape_from`` and the adapter that reads a foreign type language.

The foreign language in these fixtures (``f32``, ``i32``, ``count``) is the one
the dialect names as observed: a corpus that already stores its schemas as data
wrote them in ITS own vocabulary, not the dialect's.
"""

from pathlib import Path
from typing import Callable

from yaml_data_editor_kit.schema import (
    Profile,
    errors_only,
    load_profile,
    validate_corpus,
)

# The `write` fixture's signature. Named locally on purpose: importing it
# from conftest would resolve by module name, and every tests/<plugin>/
# directory in this repo has one.
Writer = Callable[[str, str], Path]

PROFILE = """
dialect: type/1
id: part_def
identified_by: id
fields:
  id: { type: id }
  fields:
    type: map
    key:   { type: id }
    value:
      type: record
      fields:
        type:  { type: string }
        count: { type: int, required: false }
---
dialect: source/1
of: part_def
layout: rows
path: content/part_defs.yaml
---
dialect: type/1
id: assembly
identified_by: id
fields:
  id: { type: id }
  parts:
    type: map
    key:   { type: ref, to: part_def }
    value: { shape_from: part_def.fields }
adapter:
  type_key: type
  types: { f32: float, i32: int, i64: int, bool: bool, string: string }
  cardinality_key: count
---
dialect: source/1
of: assembly
layout: rows
path: content/assemblies.yaml
"""

PART_DEFS = """
- id: spring
  fields:
    tension: { type: f32 }
    steps:   { type: i32 }
    profile: { type: f32, count: 3 }
"""


NESTED_SHAPE_PATH_PROFILE = """
dialect: type/1
id: schema_slot
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: schema_slot
layout: rows
path: content/schema_slots.yaml
---
dialect: type/1
id: part_def
identified_by: id
fields:
  id: { type: id }
  shape_tables:
    type: map
    key: { type: ref, to: schema_slot }
    value:
      type: map
      key: { type: id }
      value:
        type: record
        fields:
          type: { type: string }
---
dialect: source/1
of: part_def
layout: rows
path: content/part_defs.yaml
---
dialect: type/1
id: assembly
identified_by: id
fields:
  id: { type: id }
  parts:
    type: map
    key: { type: ref, to: part_def }
    value: { shape_from: part_def.shape_tables.missing }
adapter:
  type_key: type
  types: { f32: float }
---
dialect: source/1
of: assembly
layout: rows
path: content/assemblies.yaml
"""


def _load(
    profile_dir: Path, write: Writer, assemblies: str, part_defs: str = PART_DEFS
) -> Profile:
    write("profile/assembly.yaml", PROFILE)
    write("content/part_defs.yaml", part_defs)
    write("content/assemblies.yaml", assemblies)
    return load_profile(profile_dir)


def test_a_map_value_takes_its_shape_from_the_record_its_key_names(
    tmp_path, profile_dir, write
) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: frame
  parts:
    spring:
      tension: 1.5
      steps: 4
      profile: [0.1, 0.2, 0.3]
""",
    )
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_the_adapter_maps_the_foreign_scalar_types(tmp_path, profile_dir, write) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: frame
  parts:
    spring:
      tension: 1.5
      steps: not_a_number
      profile: [0.1, 0.2, 0.3]
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "content/assemblies.yaml"
    assert problems[0].record == "frame"
    assert problems[0].field == "parts.spring.steps"
    assert "declared 'int'" in problems[0].message


def test_the_cardinality_key_means_a_fixed_length_list(tmp_path, profile_dir, write) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: frame
  parts:
    spring:
      tension: 1.5
      steps: 4
      profile: [0.1, 0.2]
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].field == "parts.spring.profile"
    assert "exactly 3 are declared" in problems[0].message


def test_a_key_not_declared_by_the_shape_source_is_refused(tmp_path, profile_dir, write) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: frame
  parts:
    spring:
      tension: 1.5
      steps: 4
      profile: [0.1, 0.2, 0.3]
      damping: 0.5
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].field == "parts.spring.damping"


def test_a_foreign_type_the_adapter_does_not_map_is_reported(tmp_path, profile_dir, write) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: frame
  parts:
    spring:
      tension: 1.5
""",
        part_defs="- id: spring\n  fields:\n    tension: { type: f64 }\n",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    unmapped = [p for p in problems if "the adapter does not map" in p.message]
    assert len(unmapped) == 1
    assert unmapped[0].file == "content/assemblies.yaml"
    assert unmapped[0].record == "frame"
    assert unmapped[0].field == "parts.spring"


def test_a_key_naming_no_shape_record_is_reported(tmp_path, profile_dir, write) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: frame
  parts:
    damper: { tension: 1.0 }
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    fields = {p.field for p in problems}
    assert fields == {"parts.damper"}
    assert any("no record of type 'part_def'" in p.message for p in problems)


def test_a_shape_from_path_key_is_checked_without_a_shaped_map_entry(
    tmp_path, profile_dir, write
) -> None:
    write("profile/assembly.yaml", NESTED_SHAPE_PATH_PROFILE)
    write("content/schema_slots.yaml", "- { id: present }\n")
    write(
        "content/part_defs.yaml",
        "- id: spring\n  shape_tables:\n    present:\n      tension: { type: f32 }\n",
    )
    write("content/assemblies.yaml", "- { id: frame, parts: {} }\n")

    profile = load_profile(profile_dir)
    problems = [
        diagnostic
        for diagnostic in errors_only(validate_corpus(profile, tmp_path))
        if diagnostic.field == "part_def.shape_tables.missing"
    ]
    assert len(problems) == 1
    assert "key 'missing'" in problems[0].message
    assert "map 'part_def.shape_tables'" in problems[0].message
    assert "names no record of type 'schema_slot'" in problems[0].message
