"""The profile loader: what a legal dialect document is, and what is refused."""

import pytest

from yaml_data_editor_kit.schema import ProfileError, load_profile

TYPE_DOC = """
dialect: type/1
id: product
title: Product
identified_by: id
fields:
  id:       { type: id }
  name:     { type: string }
  category: { type: enum, values: [tool, part] }
  price:    { type: int, unit: cents, meaning: "list price" }
  labels:   { type: list, of: { type: string } }
"""

VIEW_DOC = """
dialect: view/1
id: product_table
of: product
form: table
fields:
  - { field: name, label: Name }
  - { field: price, label: Price, format: "%.2f" }
  - { computed: label_count, label: "labels", from: "count(labels)" }
"""

SOURCE_DOC = """
dialect: source/1
of: product
layout: rows
path: content/products.yaml
key: items
generated_by: tools/build_catalog
"""


def test_loads_the_three_document_kinds(profile_dir, write) -> None:
    write("profile/product.yaml", TYPE_DOC)
    write("profile/product_view.yaml", VIEW_DOC)
    write("profile/product_source.yaml", SOURCE_DOC)

    profile = load_profile(profile_dir)

    product = profile.types["product"]
    assert product.title == "Product"
    assert product.identified_by == "id"
    assert product.fields["price"].unit == "cents"
    assert product.fields["price"].meaning == "list price"
    assert product.fields["labels"].of.kind == "string"

    view = profile.views["product_table"]
    assert view.form == "table"
    assert view.field_names() == ["name", "price"]
    assert view.entries[2].computed == "label_count"

    source = profile.sources_for("product")[0]
    assert (source.layout, source.key) == ("rows", "items")
    assert source.generated_by == "tools/build_catalog"


def test_a_field_defaults_to_required(profile_dir, write) -> None:
    write("profile/product.yaml", TYPE_DOC)
    profile = load_profile(profile_dir)
    assert profile.types["product"].fields["name"].required is True


def test_a_raw_scalar_map_key_is_refused(profile_dir, write) -> None:
    write(
        "profile/product.yaml",
        """
dialect: type/1
id: product
identified_by: id
fields:
  id:      { type: id }
  weights: { type: map, key: { type: string }, value: { type: float } }
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert "map key must be an id, a ref or an enum" in str(caught.value)


def test_an_integer_map_key_is_permitted_when_it_is_declared(profile_dir, write) -> None:
    write(
        "profile/product.yaml",
        """
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
  negative_fraction:
    type: map
    key:   { type: enum, stored: int, values: { 2: two_stats, 3: three_stats } }
    value: { type: float }
""",
    )
    profile = load_profile(profile_dir)
    key = profile.types["product"].fields["negative_fraction"].key
    assert key.stored == "int"
    assert key.enum_members == [2, 3]


def test_a_constraint_without_why_is_refused(profile_dir, write) -> None:
    write(
        "profile/measure.yaml",
        """
dialect: type/1
id: measure
identified_by: id
fields:
  id: { type: id }
constraints:
  - kind: unique
    ids: measure.id
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert "'why:' is required" in str(caught.value)


def test_ordered_without_significance_is_refused(profile_dir, write) -> None:
    write(
        "profile/matrix.yaml",
        """
dialect: type/1
id: matrix
identified_by: id
fields:
  id: { type: id }
  categories:
    type: list
    of: { type: string }
    ordered: {}
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert "'significance:'" in str(caught.value)


def test_extensible_fields_must_not_be_restated_under_fields(profile_dir, write) -> None:
    write(
        "profile/template.yaml",
        """
dialect: type/1
id: template
identified_by: name
fields:
  name:    { type: id }
  extends: { type: ref, to: template }
extensible:
  via: extends
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert "do not restate it" in str(caught.value)


def test_extensible_declares_both_of_the_fields_it_names(profile_dir, write) -> None:
    write(
        "profile/template.yaml",
        """
dialect: type/1
id: template
identified_by: name
fields:
  name: { type: id }
extensible:
  via: extends
  abstract_flag: abstract
""",
    )
    declared = load_profile(profile_dir).types["template"].declared_fields()
    assert declared["extends"].kind == "ref"
    assert declared["extends"].to == "template"
    assert declared["abstract"].kind == "bool"
    assert declared["abstract"].required is False


def test_an_unknown_key_is_refused_rather_than_ignored(profile_dir, write) -> None:
    write(
        "profile/product.yaml",
        """
dialect: type/1
id: product
identified_by: id
fields:
  id:   { type: id }
  name: { type: string, maxchars: 12 }
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert "unknown key(s) maxchars" in str(caught.value)


def test_shape_from_outside_a_map_value_is_refused(profile_dir, write) -> None:
    write(
        "profile/part.yaml",
        """
dialect: type/1
id: part_def
identified_by: id
fields:
  id:     { type: id }
  fields: { type: map, key: { type: id }, value: { type: record, fields: { type: { type: string } } } }
""",
    )
    write(
        "profile/assembly.yaml",
        """
dialect: type/1
id: assembly
identified_by: id
fields:
  id:    { type: id }
  block: { shape_from: part_def.fields }
adapter:
  type_key: type
  types: { f32: float }
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert "only legal as a map's 'value:'" in str(caught.value)


def test_shape_from_without_an_adapter_is_refused(profile_dir, write) -> None:
    write(
        "profile/part.yaml",
        """
dialect: type/1
id: part_def
identified_by: id
fields:
  id:     { type: id }
  fields: { type: map, key: { type: id }, value: { type: record, fields: { type: { type: string } } } }
""",
    )
    write(
        "profile/assembly.yaml",
        """
dialect: type/1
id: assembly
identified_by: id
fields:
  id: { type: id }
  parts:
    type: map
    key:   { type: ref, to: part_def }
    value: { shape_from: part_def.fields }
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert "must also declare an 'adapter:'" in str(caught.value)


def test_a_variant_discriminator_must_be_an_enum_field(profile_dir, write) -> None:
    write(
        "profile/product.yaml",
        """
dialect: type/1
id: product
identified_by: id
fields:
  id:       { type: id }
  category: { type: string }
variants:
  on: category
  when:
    subscription:
      billing_period: { type: string }
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert "must name an enum field" in str(caught.value)


def test_a_view_naming_an_undeclared_field_is_refused(profile_dir, write) -> None:
    write("profile/product.yaml", TYPE_DOC)
    write(
        "profile/bad_view.yaml",
        """
dialect: view/1
id: product_card
of: product
form: card
fields:
  - { field: colour }
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert "which type 'product' does not declare" in str(caught.value)


def test_a_non_single_layout_needs_the_type_to_have_an_identity(profile_dir, write) -> None:
    write(
        "profile/settings.yaml",
        """
dialect: type/1
id: settings
fields:
  theme: { type: string }
---
dialect: source/1
of: settings
layout: rows
path: content/settings.yaml
""",
    )
    with pytest.raises(ProfileError) as caught:
        load_profile(profile_dir)
    assert "declares no 'identified_by:'" in str(caught.value)
