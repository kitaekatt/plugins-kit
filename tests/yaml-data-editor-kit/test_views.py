"""``view`` documents, and the checkable ``covers:`` superset rule."""

from pathlib import Path
from typing import Callable

import pytest

from yaml_data_editor_kit.schema import (
    ProfileError,
    errors_only,
    load_profile,
    validate_corpus,
)

# The `write` fixture's signature. Named locally on purpose: importing it
# from conftest would resolve by module name, and every tests/<plugin>/
# directory in this repo has one.
Writer = Callable[[str, str], Path]

TYPE_AND_SOURCE = """
dialect: type/1
id: product
identified_by: id
fields:
  id:       { type: id }
  name:     { type: string }
  price:    { type: int, unit: cents }
  category: { type: enum, values: [single_purchase, subscription] }
variants:
  on: category
  when:
    subscription:
      seats: { type: int }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
"""

SUMMARY = """
dialect: view/1
id: product_summary
of: product
form: summary
fields:
  - { field: name, label: Name }
  - { field: price, label: Price, format: "%.2f" }
"""


def _write_corpus(write: Writer) -> None:
    write(
        "content/products.yaml",
        "- { id: bolt, name: Bolt, price: 30, category: single_purchase }\n",
    )


def test_a_view_declares_order_labels_and_formatting_only(profile_dir, write) -> None:
    write("profile/product.yaml", TYPE_AND_SOURCE)
    write("profile/summary.yaml", SUMMARY)
    view = load_profile(profile_dir).views["product_summary"]
    assert [e.label for e in view.entries] == ["Name", "Price"]
    assert view.entries[1].format == "%.2f"


def test_covers_passes_when_the_card_shows_every_summary_field(tmp_path, profile_dir, write) -> None:
    write("profile/product.yaml", TYPE_AND_SOURCE)
    write("profile/summary.yaml", SUMMARY)
    write(
        "profile/card.yaml",
        """
dialect: view/1
id: product_card
of: product
form: card
covers: product_summary
fields:
  - { field: price }
  - { field: name }
  - { field: category }
""",
    )
    _write_corpus(write)
    profile = load_profile(profile_dir)
    # The card orders its fields differently on purpose: covers constrains the
    # field SET, never the order.
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_covers_names_the_field_the_covering_view_omits(tmp_path, profile_dir, write) -> None:
    write("profile/product.yaml", TYPE_AND_SOURCE)
    write("profile/summary.yaml", SUMMARY)
    write(
        "profile/card.yaml",
        """
dialect: view/1
id: product_card
of: product
form: card
covers: product_summary
fields:
  - { field: name }
""",
    )
    _write_corpus(write)
    profile = load_profile(profile_dir)
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "card.yaml"
    assert problems[0].record == "product_card"
    assert problems[0].field == "price"


def test_a_view_entry_may_be_scoped_to_a_discriminator_value(profile_dir, write) -> None:
    write("profile/product.yaml", TYPE_AND_SOURCE)
    write(
        "profile/card.yaml",
        """
dialect: view/1
id: product_card
of: product
form: card
fields:
  - { field: name }
  - { field: seats, when: subscription }
""",
    )
    view = load_profile(profile_dir).views["product_card"]
    assert view.entries[1].when == "subscription"


def test_a_when_that_is_not_a_variant_value_is_refused(profile_dir, write) -> None:
    write("profile/product.yaml", TYPE_AND_SOURCE)
    write(
        "profile/card.yaml",
        """
dialect: view/1
id: product_card
of: product
form: card
fields:
  - { field: seats, when: rental }
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert "is not a variant value" in str(caught.value)


def test_covers_naming_an_unknown_view_is_refused(profile_dir, write) -> None:
    write("profile/product.yaml", TYPE_AND_SOURCE)
    write(
        "profile/card.yaml",
        """
dialect: view/1
id: product_card
of: product
form: card
covers: product_digest
fields:
  - { field: name }
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert "unknown view 'product_digest'" in str(caught.value)


def test_a_computed_entry_is_presentation_only(profile_dir, write) -> None:
    write("profile/product.yaml", TYPE_AND_SOURCE)
    write(
        "profile/table.yaml",
        """
dialect: view/1
id: product_table
of: product
form: table
fields:
  - { field: name }
  - { computed: used_by, label: "used by", from: "count(product where price > 0)" }
""",
    )
    view = load_profile(profile_dir).views["product_table"]
    assert view.field_names() == ["name"]
    assert view.entries[1].from_expr.startswith("count(")
