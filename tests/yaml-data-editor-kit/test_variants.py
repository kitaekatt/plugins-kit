"""``variants`` -- a record whose shape depends on one of its own field values."""

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
id: billing_period
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: billing_period
layout: rows
path: content/billing_periods.yaml
---
dialect: type/1
id: product
identified_by: id
fields:
  id:       { type: id }
  name:     { type: string }
  category: { type: enum, values: [single_purchase, subscription] }
variants:
  on: category
  when:
    subscription:
      billing_period: { type: ref, to: billing_period }
      seats:          { type: int, min: 1 }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
"""


def _load(profile_dir: Path, write: Writer, rows: str) -> Profile:
    write("profile/catalog.yaml", PROFILE)
    write("content/billing_periods.yaml", "- { id: monthly }\n")
    write("content/products.yaml", rows)
    return load_profile(profile_dir)


def test_a_record_with_the_discriminating_value_must_carry_the_variant_fields(
    tmp_path, profile_dir, write
) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: plan
  name: Plan
  category: subscription
  billing_period: monthly
  seats: 5
""",
    )
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_a_record_without_the_discriminating_value_adds_nothing(tmp_path, profile_dir, write) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: bolt
  name: Bolt
  category: single_purchase
""",
    )
    # No variant fields are demanded, and none may be written either.
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_a_variant_field_missing_from_a_matching_record_is_reported(
    tmp_path, profile_dir, write
) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: plan
  name: Plan
  category: subscription
  seats: 5
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "content/products.yaml"
    assert problems[0].record == "plan"
    assert problems[0].field == "billing_period"
    assert "required but absent" in problems[0].message


def test_a_variant_field_written_on_a_non_matching_record_is_refused(
    tmp_path, profile_dir, write
) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: bolt
  name: Bolt
  category: single_purchase
  seats: 5
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].record == "bolt"
    assert problems[0].field == "seats"
    assert "is not a field type 'product' declares" in problems[0].message


def test_a_variant_field_is_validated_by_its_own_declaration(tmp_path, profile_dir, write) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: plan
  name: Plan
  category: subscription
  billing_period: yearly
  seats: 0
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    by_field = {p.field: p for p in problems}
    assert set(by_field) == {"billing_period", "seats"}
    assert "names no record of type 'billing_period'" in by_field["billing_period"].message
    assert "below the declared min of 1" in by_field["seats"].message
    for problem in problems:
        assert problem.file == "content/products.yaml"
        assert problem.record == "plan"
