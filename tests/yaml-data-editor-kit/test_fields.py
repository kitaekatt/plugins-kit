"""Field types, annotations, enums, refs, and size/cardinality constraints."""

from pathlib import Path
from typing import Callable

from yaml_data_editor_kit.schema import (
    ADVISORY,
    Diagnostic,
    errors_only,
    load_profile,
    validate_corpus,
)

# The `write` fixture's signature. Named locally on purpose: importing it
# from conftest would resolve by module name, and every tests/<plugin>/
# directory in this repo has one.
Writer = Callable[[str, str], Path]

CATALOG_PROFILE = """
dialect: type/1
id: label
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: label
layout: rows
path: content/labels.yaml
---
dialect: type/1
id: tier
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: tier
layout: rows
path: content/tiers.yaml
---
dialect: type/1
id: product
identified_by: id
fields:
  id:       { type: id }
  name:     { type: string, max_chars: 12 }
  note:     { type: text, required: false }
  active:   { type: bool }
  weight:   { type: float, unit: grams, min: 0 }
  stock:    { type: int, min: 0, sentinel: { -1: "no limit" } }
  tier:     { type: enum, values_from: tier.id }
  shape:    { type: enum, stored: int, values: { 0: none, 1: circle, 2: rect } }
  hidden:   { type: enum, values: [true, false, TBD] }
  labels:   { type: list, of: { type: ref, to: label }, min_length: 1 }
  xp:       { type: list, of: { type: int }, length: 3 }
  totals:   { type: map, key: { type: ref, to: tier }, value: { type: float }, max_length: 2 }
  box:      { type: record, fields: { width: { type: int }, height: { type: int } } }
  computed_rank: { type: int, derived: "position in the tier order" }
open:
  prefix: flag_
  type: { type: text }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
"""

GOOD_PRODUCT = """
- id: bolt
  name: Bolt
  active: true
  weight: 2.5
  stock: -1
  tier: standard
  shape: 1
  hidden: TBD
  labels: [metal]
  xp: [1, 2, 3]
  totals: { standard: 1.0 }
  box: { width: 2, height: 3 }
"""


def _setup(
    write: Writer,
    product_rows: str,
    labels: str = "- { id: metal }\n",
    tiers: str = "- { id: standard }\n",
) -> None:
    write("profile/catalog.yaml", CATALOG_PROFILE)
    write("content/labels.yaml", labels)
    write("content/tiers.yaml", tiers)
    write("content/products.yaml", product_rows)


def test_a_well_formed_record_produces_no_errors(tmp_path, profile_dir, write) -> None:
    _setup(write, GOOD_PRODUCT)
    profile = load_profile(profile_dir)
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_a_derived_field_is_never_demanded(tmp_path, profile_dir, write) -> None:
    _setup(write, GOOD_PRODUCT)
    profile = load_profile(profile_dir)
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert [p for p in problems if p.field == "computed_rank"] == []


def _one(
    tmp_path: Path, profile_dir: Path, write: Writer, rows: str, field: str
) -> Diagnostic:
    _setup(write, rows)
    profile = load_profile(profile_dir)
    problems = [p for p in errors_only(validate_corpus(profile, tmp_path)) if p.field == field]
    assert len(problems) == 1, problems
    problem = problems[0]
    assert problem.file == "content/products.yaml"
    assert problem.record == "bolt"
    assert problem.field == field
    return problem


def test_a_wrong_scalar_type_names_file_record_and_field(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT.replace("weight: 2.5", "weight: heavy")
    problem = _one(tmp_path, profile_dir, write, rows, "weight")
    assert "declared 'float'" in problem.message


def test_a_bool_is_not_an_int(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT.replace("stock: -1", "stock: true")
    problem = _one(tmp_path, profile_dir, write, rows, "stock")
    assert "declared 'int'" in problem.message


def test_a_sentinel_value_is_exempt_from_the_declared_range(tmp_path, profile_dir, write) -> None:
    _setup(write, GOOD_PRODUCT)
    profile = load_profile(profile_dir)
    # stock is `min: 0` and holds -1, declared as a sentinel meaning "no limit".
    assert [p for p in errors_only(validate_corpus(profile, tmp_path)) if p.field == "stock"] == []


def test_a_value_below_min_is_reported(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT.replace("weight: 2.5", "weight: -3.0")
    problem = _one(tmp_path, profile_dir, write, rows, "weight")
    assert "below the declared min of 0" in problem.message


def test_max_chars_is_enforced(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT.replace("name: Bolt", "name: An Extremely Long Name")
    problem = _one(tmp_path, profile_dir, write, rows, "name")
    assert "above the declared max_chars of 12" in problem.message


def test_an_exact_length_is_enforced(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT.replace("xp: [1, 2, 3]", "xp: [1, 2]")
    problem = _one(tmp_path, profile_dir, write, rows, "xp")
    assert "exactly 3 are declared" in problem.message


def test_min_length_is_enforced(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT.replace("labels: [metal]", "labels: []")
    problem = _one(tmp_path, profile_dir, write, rows, "labels")
    assert "below the declared min_length of 1" in problem.message


def test_max_length_is_enforced_on_a_map(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT.replace(
        "totals: { standard: 1.0 }", "totals: { standard: 1.0, premium: 2.0 , bulk: 3.0 }"
    )
    _setup(write, rows, tiers="- { id: standard }\n- { id: premium }\n- { id: bulk }\n")
    profile = load_profile(profile_dir)
    problems = [
        p for p in errors_only(validate_corpus(profile, tmp_path)) if p.field == "totals"
    ]
    assert len(problems) == 1
    assert "above the declared max_length of 2" in problems[0].message


def test_a_ref_that_names_no_record_is_reported_at_its_index(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT.replace("labels: [metal]", "labels: [metal, plastic]")
    problem = _one(tmp_path, profile_dir, write, rows, "labels[1]")
    assert "names no record of type 'label'" in problem.message


def test_a_map_key_that_is_not_a_legal_ref_is_reported(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT.replace("totals: { standard: 1.0 }", "totals: { deluxe: 1.0 }")
    problem = _one(tmp_path, profile_dir, write, rows, "totals.deluxe")
    assert "names no record of type 'tier'" in problem.message


def test_values_from_draws_the_legal_set_from_another_types_ids(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT.replace("tier: standard", "tier: deluxe")
    problem = _one(tmp_path, profile_dir, write, rows, "tier")
    assert "not one of the declared values" in problem.message
    assert "'standard'" in problem.message


def test_a_stored_int_enum_accepts_the_integer_on_disk(tmp_path, profile_dir, write) -> None:
    _setup(write, GOOD_PRODUCT)
    profile = load_profile(profile_dir)
    assert [p for p in errors_only(validate_corpus(profile, tmp_path)) if p.field == "shape"] == []


def test_a_stored_int_enum_refuses_a_label_written_in_place_of_the_integer(
    tmp_path, profile_dir, write
) -> None:
    rows = GOOD_PRODUCT.replace("shape: 1", "shape: circle")
    problem = _one(tmp_path, profile_dir, write, rows, "shape")
    assert "'stored: int'" in problem.message


def test_a_stored_int_enum_refuses_an_undeclared_integer(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT.replace("shape: 1", "shape: 9")
    problem = _one(tmp_path, profile_dir, write, rows, "shape")
    assert "not one of the declared values" in problem.message


def test_an_enum_may_hold_a_third_value_beside_true_and_false(tmp_path, profile_dir, write) -> None:
    for value in ("true", "false", "TBD"):
        _setup(write, GOOD_PRODUCT.replace("hidden: TBD", "hidden: " + value))
        profile = load_profile(profile_dir)
        problems = errors_only(validate_corpus(profile, tmp_path))
        assert [p for p in problems if p.field == "hidden"] == []


def test_an_undeclared_field_is_refused(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT + "  colour: red\n"
    problem = _one(tmp_path, profile_dir, write, rows, "colour")
    assert "is not a field type 'product' declares" in problem.message


def test_an_open_prefixed_field_is_an_advisory_not_an_error(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT + "  flag_pending_review: needs a second look\n"
    _setup(write, rows)
    profile = load_profile(profile_dir)
    diagnostics = validate_corpus(profile, tmp_path)
    assert errors_only(diagnostics) == []
    advisories = [d for d in diagnostics if d.severity == ADVISORY]
    assert len(advisories) == 1
    assert advisories[0].file == "content/products.yaml"
    assert advisories[0].record == "bolt"
    assert advisories[0].field == "flag_pending_review"


def test_an_open_field_is_still_checked_against_its_declared_type(
    tmp_path, profile_dir, write
) -> None:
    rows = GOOD_PRODUCT + "  flag_tbd: 4\n"
    problem = _one(tmp_path, profile_dir, write, rows, "flag_tbd")
    assert "declared 'text'" in problem.message


def test_a_nested_record_field_is_addressed_by_its_path(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT.replace("box: { width: 2, height: 3 }", "box: { width: wide, height: 3 }")
    problem = _one(tmp_path, profile_dir, write, rows, "box.width")
    assert "declared 'int'" in problem.message


def test_a_missing_nested_field_is_reported_by_its_path(tmp_path, profile_dir, write) -> None:
    rows = GOOD_PRODUCT.replace("box: { width: 2, height: 3 }", "box: { width: 2 }")
    problem = _one(tmp_path, profile_dir, write, rows, "box.height")
    assert "required but absent" in problem.message


def test_values_from_also_accepts_a_scalar_list_path(tmp_path, profile_dir, write) -> None:
    write(
        "profile/matrix.yaml",
        """
dialect: type/1
id: permission_matrix
identified_by: id
fields:
  id: { type: id }
  categories:
    type: list
    of: { type: string }
    ordered:
      significance: "index is the bit position in the permission mask"
---
dialect: source/1
of: permission_matrix
layout: rows
path: content/matrix.yaml
---
dialect: type/1
id: rule
identified_by: id
fields:
  id:      { type: id }
  belongs: { type: enum, values_from: permission_matrix.categories }
---
dialect: source/1
of: rule
layout: rows
path: content/rules.yaml
""",
    )
    write("content/matrix.yaml", "- id: main\n  categories: [read, write, admin]\n")
    write("content/rules.yaml", "- { id: r1, belongs: write }\n")
    profile = load_profile(profile_dir)
    assert errors_only(validate_corpus(profile, tmp_path)) == []

    write("content/rules.yaml", "- { id: r1, belongs: execute }\n")
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "content/rules.yaml"
    assert problems[0].record == "r1"
    assert problems[0].field == "belongs"
    assert "'read'" in problems[0].message
