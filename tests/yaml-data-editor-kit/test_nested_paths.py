"""Nested field paths: the anchored/field path grammar, walking into a
record's ``fields:`` or a map's ``value:``, the three join refusals (through a
``ref``, a ``list``, or a ``shape_from`` value), and ``covers:`` by prefix.
"""

from pathlib import Path
from typing import Callable

import pytest

from yaml_data_editor_kit.schema import (
    ADVISORY,
    ProfileError,
    errors_only,
    load_profile,
    validate_corpus,
)

# The `write` fixture's signature. Named locally on purpose: importing it
# from conftest would resolve by module name, and every tests/<plugin>/
# directory in this repo has one.
Writer = Callable[[str, str], Path]


# --------------------------------------------------------------------------
# a nested record step -- a constraint's 'ids:' living under a nested record,
# same shape as 'app.templates.entities' in the motivating corpus.
# --------------------------------------------------------------------------

NESTED_RECORD_PROFILE = """
dialect: type/1
id: manifest
identified_by: id
fields:
  id: { type: id }
  bundle:
    type: record
    fields:
      items:
        type: list
        of: { type: string }
constraints:
  - kind: matches_files
    ids: manifest.bundle.items
    files: "items/*.yaml"
    why: "the bundle list and the directory must not drift"
---
dialect: source/1
of: manifest
layout: single
path: content/manifest.yaml
"""


def test_a_constraint_path_may_walk_into_a_nested_record(tmp_path, profile_dir, write) -> None:
    write("profile/manifest.yaml", NESTED_RECORD_PROFILE)
    write("content/manifest.yaml", "id: main\nbundle:\n  items: [alpha]\n")
    write("items/alpha.yaml", "name: alpha\n")
    profile = load_profile(profile_dir)
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_a_nested_record_path_names_the_failing_segment(profile_dir, write) -> None:
    write(
        "profile/manifest.yaml",
        NESTED_RECORD_PROFILE.replace("ids: manifest.bundle.items", "ids: manifest.bundle.missing"),
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    message = str(caught.value)
    assert "missing" in message
    assert "manifest.bundle" in message


# --------------------------------------------------------------------------
# a map key step -- checked against the map's key: declaration.
# --------------------------------------------------------------------------

ENUM_KEYED_MAP_PROFILE = """
dialect: type/1
id: loadout
identified_by: id
fields:
  id: { type: id }
  stats:
    type: map
    key:   { type: enum, values: [power, speed] }
    value: { type: float }
---
dialect: source/1
of: loadout
layout: rows
path: content/loadouts.yaml
"""


def test_a_map_key_step_resolves_the_maps_value_declaration(profile_dir, write) -> None:
    write("profile/loadout.yaml", ENUM_KEYED_MAP_PROFILE)
    write(
        "profile/table.yaml",
        """
dialect: view/1
id: loadout_table
of: loadout
form: table
fields:
  - { field: stats.power, label: Power }
""",
    )
    view = load_profile(profile_dir).views["loadout_table"]
    assert view.entries[0].field == "stats.power"


def test_an_enum_key_segment_not_in_the_declared_set_is_refused(profile_dir, write) -> None:
    write("profile/loadout.yaml", ENUM_KEYED_MAP_PROFILE)
    write(
        "profile/table.yaml",
        """
dialect: view/1
id: loadout_table
of: loadout
form: table
fields:
  - { field: stats.stamina, label: Stamina }
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    message = str(caught.value)
    assert "stamina" in message
    assert "not a member of the declared set" in message
    assert "map 'stats'" in message
    assert "map 'loadout.stats'" not in message


def test_an_empty_enum_key_set_names_the_empty_declaration_as_the_problem(
    profile_dir, write
) -> None:
    write(
        "profile/loadout.yaml",
        ENUM_KEYED_MAP_PROFILE.replace("values: [power, speed]", "values: []"),
    )
    write(
        "profile/table.yaml",
        """
dialect: view/1
id: loadout_table
of: loadout
form: table
fields:
  - { field: stats.power }
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert "declared set is empty" in str(caught.value)


VALUES_FROM_ENUM_KEY_PROFILE = """
dialect: type/1
id: metric_name
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: metric_name
layout: rows
path: content/metric_names.yaml
---
dialect: type/1
id: dashboard
identified_by: id
fields:
  id: { type: id }
  metrics:
    type: map
    key: { type: enum, values_from: metric_name.id }
    value: { type: list, of: { type: string } }
constraints:
  - kind: unique
    ids: dashboard.metrics.stamina
    why: "each selected metric value must be unique"
---
dialect: source/1
of: dashboard
layout: rows
path: content/dashboards.yaml
"""


def test_a_values_from_enum_key_segment_must_belong_to_the_resolved_set(
    tmp_path, profile_dir, write
) -> None:
    write("profile/dashboard.yaml", VALUES_FROM_ENUM_KEY_PROFILE)
    write("content/metric_names.yaml", "- { id: power }\n- { id: speed }\n")
    write("content/dashboards.yaml", "- { id: main, metrics: { power: [high] } }\n")

    profile = load_profile(profile_dir)
    problems = [
        diagnostic
        for diagnostic in errors_only(validate_corpus(profile, tmp_path))
        if diagnostic.field == "dashboard.metrics.stamina"
    ]
    assert len(problems) == 1
    assert "key 'stamina'" in problems[0].message
    assert "map 'dashboard.metrics'" in problems[0].message
    assert "declared set (power, speed)" in problems[0].message


def test_a_values_from_path_is_key_checked_when_every_record_omits_the_enum(
    tmp_path, profile_dir, write
) -> None:
    write("profile/assembly.yaml", REF_KEYED_MAP_PROFILE)
    write("content/component_defs.yaml", "- { id: hull }\n")
    write("content/assemblies.yaml", "- { id: frame, parts: { hull: 1 } }\n")

    profile = load_profile(profile_dir)
    problems = [
        diagnostic
        for diagnostic in errors_only(validate_corpus(profile, tmp_path))
        if diagnostic.field == "assembly.parts.turret"
    ]
    assert len(problems) == 1
    assert "key 'turret'" in problems[0].message
    assert "names no record of type 'component_def'" in problems[0].message


# --------------------------------------------------------------------------
# a bare 'id' key has no declared set -- the step resolves, and the validator
# reports the key as an advisory (same channel as 'open:').
# --------------------------------------------------------------------------

ID_KEYED_MAP_PROFILE = """
dialect: type/1
id: gear
identified_by: id
fields:
  id: { type: id }
  stats:
    type: map
    key:   { type: id }
    value: { type: float }
  favorite_stat:
    type: enum
    values_from: gear.stats.power
---
dialect: source/1
of: gear
layout: rows
path: content/gear.yaml
"""


def test_a_bare_id_key_step_resolves_but_is_reported_as_an_advisory(
    tmp_path, profile_dir, write
) -> None:
    write("profile/gear.yaml", ID_KEYED_MAP_PROFILE)
    write(
        # 'values_from' resolves to the VALUE stored at that map entry (3.0),
        # not the key name -- 'favorite_stat' must hold that value to pass.
        "content/gear.yaml",
        "- { id: sword, stats: { power: 3.0 }, favorite_stat: 3.0 }\n",
    )
    profile = load_profile(profile_dir)
    diagnostics = validate_corpus(profile, tmp_path)
    advisories = [d for d in diagnostics if d.severity == ADVISORY]
    assert any("no declared legal set" in d.message for d in advisories)
    # An unchecked key is an advisory, not an error.
    assert errors_only(diagnostics) == []


# --------------------------------------------------------------------------
# a ref-keyed map step IS checkable, once a corpus exists to check it against.
# --------------------------------------------------------------------------

REF_KEYED_MAP_PROFILE = """
dialect: type/1
id: component_def
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: component_def
layout: rows
path: content/component_defs.yaml
---
dialect: type/1
id: assembly
identified_by: id
fields:
  id: { type: id }
  parts:
    type: map
    key:   { type: ref, to: component_def }
    value: { type: int }
  selected_part:
    type: enum
    values_from: assembly.parts.turret
---
dialect: source/1
of: assembly
layout: rows
path: content/assemblies.yaml
"""


CYCLIC_VALUES_FROM_KEY_PROFILE = """
dialect: type/1
id: t
identified_by: id
fields:
  id: { type: id }
  m:
    type: map
    key: { type: enum, values_from: t.m.a }
    value: { type: string }
---
dialect: source/1
of: t
layout: rows
path: content/t.yaml
---
dialect: view/1
id: v
of: t
form: table
fields:
  - { field: m.a }
"""


def test_a_values_from_map_key_cycle_is_a_named_profile_error(profile_dir, write) -> None:
    write("profile/t.yaml", CYCLIC_VALUES_FROM_KEY_PROFILE)

    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)

    message = str(caught.value)
    assert "cyclic 'values_from:'" in message
    assert "path 't.m.a'" in message
    assert "map 't.m'" in message
    assert "types involved: t" in message


def test_a_ref_keyed_map_step_passes_when_the_key_names_a_real_record(
    tmp_path, profile_dir, write
) -> None:
    write("profile/assembly.yaml", REF_KEYED_MAP_PROFILE)
    write("content/component_defs.yaml", "- { id: turret }\n")
    write(
        "content/assemblies.yaml",
        "- { id: frame, parts: { turret: 1 }, selected_part: turret }\n",
    )
    profile = load_profile(profile_dir)
    diagnostics = validate_corpus(profile, tmp_path)
    assert not any("names no record of type 'component_def'" in d.message for d in diagnostics)


def test_a_ref_keyed_map_step_is_refused_when_the_key_names_no_record(
    tmp_path, profile_dir, write
) -> None:
    write("profile/assembly.yaml", REF_KEYED_MAP_PROFILE)
    # No 'turret' component_def exists at all -- the path segment is bogus.
    write("content/component_defs.yaml", "- { id: hull }\n")
    write(
        "content/assemblies.yaml",
        "- { id: frame, parts: { hull: 1 }, selected_part: hull }\n",
    )
    profile = load_profile(profile_dir)
    diagnostics = validate_corpus(profile, tmp_path)
    problems = [d for d in diagnostics if "names no record of type 'component_def'" in d.message]
    assert len(problems) == 1
    assert problems[0].is_error


REF_KEYED_VIEW_PROFILE = """
dialect: type/1
id: component_def
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: component_def
layout: rows
path: content/component_defs.yaml
---
dialect: type/1
id: assembly
identified_by: id
fields:
  id: { type: id }
  parts:
    type: map
    key: { type: ref, to: component_def }
    value: { type: int }
---
dialect: source/1
of: assembly
layout: rows
path: content/assemblies.yaml
---
dialect: view/1
id: assembly_table
of: assembly
form: table
fields:
  - { field: parts.turret }
"""


def test_a_view_ref_key_segment_must_name_a_real_identity(tmp_path, profile_dir, write) -> None:
    write("profile/assembly.yaml", REF_KEYED_VIEW_PROFILE)
    write("content/component_defs.yaml", "- { id: hull }\n")
    write("content/assemblies.yaml", "- { id: frame, parts: { hull: 1 } }\n")

    profile = load_profile(profile_dir)
    problems = [
        diagnostic
        for diagnostic in errors_only(validate_corpus(profile, tmp_path))
        if diagnostic.field == "parts.turret"
    ]
    assert len(problems) == 1
    assert "key 'turret'" in problems[0].message
    assert "map 'parts'" in problems[0].message
    assert "names no record of type 'component_def'" in problems[0].message


ID_KEYED_VIEW_PROFILE = """
dialect: type/1
id: gear
identified_by: id
fields:
  id: { type: id }
  stats:
    type: map
    key: { type: id }
    value: { type: float }
---
dialect: source/1
of: gear
layout: rows
path: content/gear.yaml
---
dialect: view/1
id: gear_table
of: gear
form: table
fields:
  - { field: stats.power }
"""


def test_a_view_bare_id_key_segment_is_an_advisory_only(tmp_path, profile_dir, write) -> None:
    write("profile/gear.yaml", ID_KEYED_VIEW_PROFILE)
    write("content/gear.yaml", "- { id: sword, stats: { power: 3.0 } }\n")

    profile = load_profile(profile_dir)
    diagnostics = validate_corpus(profile, tmp_path)
    advisories = [
        diagnostic
        for diagnostic in diagnostics
        if diagnostic.severity == ADVISORY and diagnostic.field == "stats.power"
    ]
    assert len(advisories) == 1
    assert "map 'stats'" in advisories[0].message
    assert "key 'power'" in advisories[0].message
    assert "no declared legal set" in advisories[0].message
    assert errors_only(diagnostics) == []


# --------------------------------------------------------------------------
# the three join refusals: through a ref, through a list, past a shape_from
# value. Each is refused because continuing would reach into another type.
# --------------------------------------------------------------------------

REFUSAL_PROFILE = """
dialect: type/1
id: weapon_family
identified_by: id
fields:
  id:     { type: id }
  damage: { type: int }
---
dialect: source/1
of: weapon_family
layout: rows
path: content/weapon_families.yaml
---
dialect: type/1
id: tag
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: tag
layout: rows
path: content/tags.yaml
---
dialect: type/1
id: gear
identified_by: id
fields:
  id:     { type: id }
  family: { type: ref, to: weapon_family }
  tags:   { type: list, of: { type: ref, to: tag } }
  favorite: { type: enum, values_from: %s }
---
dialect: source/1
of: gear
layout: rows
path: content/gear.yaml
"""


def test_a_path_may_not_continue_through_a_ref(profile_dir, write) -> None:
    write("profile/gear.yaml", REFUSAL_PROFILE % "gear.family.damage")
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    message = str(caught.value)
    assert "gear.family" in message
    assert "a ref" in message


def test_a_path_may_not_continue_through_a_list(profile_dir, write) -> None:
    write("profile/gear.yaml", REFUSAL_PROFILE % "gear.tags.id")
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    message = str(caught.value)
    assert "gear.tags" in message
    assert "a list" in message


SHAPE_FROM_REFUSAL_PROFILE = """
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
    key:   { type: ref, to: part_def }
    value: { shape_from: part_def.fields }
  favorite:
    type: enum
    values_from: assembly.parts.spring.tension
adapter:
  type_key: type
  types: { f32: float }
---
dialect: source/1
of: assembly
layout: rows
path: content/assemblies.yaml
"""


def test_a_path_may_not_continue_past_a_shape_from_value(profile_dir, write) -> None:
    write("profile/assembly.yaml", SHAPE_FROM_REFUSAL_PROFILE)
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    message = str(caught.value)
    assert "cannot continue with segment 'tension'" in message
    assert "past 'assembly.parts.spring'" in message
    assert "shape_from" in message


# --------------------------------------------------------------------------
# 'covers:' compares field paths by PREFIX -- naming the map covers naming a
# key inside it, the same shape as the motivating corpus's gear_card/table.
# --------------------------------------------------------------------------

PREFIX_COVERS_PROFILE = """
dialect: type/1
id: gear
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
  stats:
    type: map
    key:   { type: id }
    value: { type: float }
---
dialect: source/1
of: gear
layout: rows
path: content/gear.yaml
---
dialect: view/1
id: gear_table
of: gear
form: table
fields:
  - { field: stats.power, label: Power }
  - { field: stats.speed, label: Speed }
---
dialect: view/1
id: gear_card
of: gear
form: card
covers: gear_table
fields:
  - { field: name }
  - { field: stats }
"""


def test_covers_passes_when_an_ancestor_path_is_named(tmp_path, profile_dir, write) -> None:
    write("profile/gear.yaml", PREFIX_COVERS_PROFILE)
    write("content/gear.yaml", "- { id: sword, name: Sword, stats: { power: 1.0 } }\n")
    profile = load_profile(profile_dir)
    problems = [
        d
        for d in errors_only(validate_corpus(profile, tmp_path))
        if "covers:" in d.message
    ]
    assert problems == []


def test_covers_still_fails_when_no_ancestor_is_named(tmp_path, profile_dir, write) -> None:
    write(
        "profile/gear.yaml",
        PREFIX_COVERS_PROFILE.replace("  - { field: stats }\n", ""),
    )
    write("content/gear.yaml", "- { id: sword, name: Sword, stats: { power: 1.0 } }\n")
    profile = load_profile(profile_dir)
    problems = [
        d
        for d in errors_only(validate_corpus(profile, tmp_path))
        if "covers:" in d.message
    ]
    assert len(problems) == 2
    assert {p.field for p in problems} == {"stats.power", "stats.speed"}


def _covers_profile(covered_field: str, covering_field: str) -> str:
    return """
dialect: type/1
id: gear
identified_by: id
fields:
  id: { type: id }
  stats:
    type: map
    key: { type: id }
    value: { type: float }
---
dialect: source/1
of: gear
layout: rows
path: content/gear.yaml
---
dialect: view/1
id: gear_table
of: gear
form: table
fields:
  - { field: %s }
---
dialect: view/1
id: gear_card
of: gear
form: card
covers: gear_table
fields:
  - { field: %s }
""" % (covered_field, covering_field)


def test_covers_refuses_a_descendant_of_the_covered_entry(
    tmp_path, profile_dir, write
) -> None:
    write("profile/gear.yaml", _covers_profile("stats", "stats.power"))
    write("content/gear.yaml", "- { id: sword, stats: { power: 1.0 } }\n")

    profile = load_profile(profile_dir)
    problems = [
        diagnostic
        for diagnostic in errors_only(validate_corpus(profile, tmp_path))
        if "covers:" in diagnostic.message
    ]
    assert len(problems) == 1
    assert problems[0].field == "stats"


def test_covers_refuses_a_sibling_of_the_covered_entry(
    tmp_path, profile_dir, write
) -> None:
    write("profile/gear.yaml", _covers_profile("stats.speed", "stats.power"))
    write("content/gear.yaml", "- { id: sword, stats: { power: 1.0 } }\n")

    profile = load_profile(profile_dir)
    problems = [
        diagnostic
        for diagnostic in errors_only(validate_corpus(profile, tmp_path))
        if "covers:" in diagnostic.message
    ]
    assert len(problems) == 1
    assert problems[0].field == "stats.speed"
