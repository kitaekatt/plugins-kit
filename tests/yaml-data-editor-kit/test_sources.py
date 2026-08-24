"""Every ``source`` layout: rows (with and without a containing key),
file_per_record, keyed_map and single."""

from pathlib import Path
from typing import Callable

from yaml_data_editor_kit.schema import (
    Profile,
    errors_only,
    load_corpus,
    load_profile,
    validate_corpus,
)

# The `write` fixture's signature. Named locally on purpose: importing it
# from conftest would resolve by module name, and every tests/<plugin>/
# directory in this repo has one.
Writer = Callable[[str, str], Path]

PRODUCT_TYPE = """
dialect: type/1
id: product
identified_by: id
fields:
  id:   { type: id }
  name: { type: string }
"""


def _profile(
    profile_dir: Path, write: Writer, source_doc: str, type_doc: str = PRODUCT_TYPE
) -> Profile:
    write("profile/product.yaml", type_doc)
    write("profile/product_source.yaml", source_doc)
    return load_profile(profile_dir)


def test_rows_with_a_containing_key(tmp_path, profile_dir, write) -> None:
    profile = _profile(
        profile_dir,
        write,
        """
dialect: source/1
of: product
layout: rows
path: content/products.yaml
key: items
""",
    )
    write(
        "content/products.yaml",
        """
items:
  - { id: bolt, name: Bolt }
  - { id: nut, name: Nut }
""",
    )
    corpus = load_corpus(profile, tmp_path)
    assert [r.identity for r in corpus.records] == ["bolt", "nut"]
    assert corpus.records[0].file == "content/products.yaml"
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_rows_where_the_document_is_the_sequence(tmp_path, profile_dir, write) -> None:
    profile = _profile(
        profile_dir,
        write,
        """
dialect: source/1
of: product
layout: rows
path: content/products.yaml
""",
    )
    write(
        "content/products.yaml",
        """
# a generated table opens straight into the sequence
- { id: bolt, name: Bolt }
- { id: nut, name: Nut }
""",
    )
    corpus = load_corpus(profile, tmp_path)
    assert [r.identity for r in corpus.records] == ["bolt", "nut"]
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_rows_without_identified_by_load_as_identity_less_records(
    tmp_path, profile_dir, write
) -> None:
    profile = _profile(
        profile_dir,
        write,
        """
dialect: source/1
of: product
layout: rows
path: content/products.yaml
""",
        type_doc="""
dialect: type/1
id: product
fields:
  name:     { type: string }
  category: { type: string }
""",
    )
    write(
        "content/products.yaml",
        """
- { name: Bolt, category: fastener }
- { name: Washer, category: spacer }
""",
    )

    corpus = load_corpus(profile, tmp_path)

    assert [record.identity for record in corpus.records] == [None, None]
    assert [record.data["name"] for record in corpus.records] == ["Bolt", "Washer"]
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_rows_missing_the_declared_containing_key_names_the_file(tmp_path, profile_dir, write) -> None:
    profile = _profile(
        profile_dir,
        write,
        """
dialect: source/1
of: product
layout: rows
path: content/products.yaml
key: items
""",
    )
    write("content/products.yaml", "entries: []\n")
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "content/products.yaml"
    assert "no containing key 'items'" in problems[0].message


def test_file_per_record(tmp_path, profile_dir, write) -> None:
    profile = _profile(
        profile_dir,
        write,
        """
dialect: source/1
of: product
layout: file_per_record
path: content/products/*.yaml
""",
    )
    write("content/products/bolt.yaml", "id: bolt\nname: Bolt\n")
    write("content/products/nut.yaml", "id: nut\nname: Nut\n")
    corpus = load_corpus(profile, tmp_path)
    assert sorted(r.identity for r in corpus.records) == ["bolt", "nut"]
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_file_per_record_filename_disagreeing_with_the_identity_names_both(
    tmp_path, profile_dir, write
) -> None:
    profile = _profile(
        profile_dir,
        write,
        """
dialect: source/1
of: product
layout: file_per_record
path: content/products/*.yaml
""",
    )
    write("content/products/bolt.yaml", "id: washer\nname: Washer\n")
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "content/products/bolt.yaml"
    assert problems[0].record == "washer"
    assert problems[0].field == "id"
    assert "bolt" in problems[0].message and "washer" in problems[0].message


def test_keyed_map_separated_by_metadata_keys(tmp_path, profile_dir, write) -> None:
    profile = _profile(
        profile_dir,
        write,
        """
dialect: source/1
of: product
layout: keyed_map
path: content/catalog.yaml
metadata_keys: [revision, note]
""",
    )
    write(
        "content/catalog.yaml",
        """
revision: 4
note: hand authored
bolt: { id: bolt, name: Bolt }
nut:  { id: nut, name: Nut }
""",
    )
    corpus = load_corpus(profile, tmp_path)
    assert sorted(r.identity for r in corpus.records) == ["bolt", "nut"]
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_keyed_map_uses_document_keys_as_identity_without_identified_by(
    tmp_path, profile_dir, write
) -> None:
    profile = _profile(
        profile_dir,
        write,
        """
dialect: source/1
of: product
layout: keyed_map
path: content/catalog.yaml
metadata_keys: [revision]
""",
        type_doc="""
dialect: type/1
id: product
fields:
  name: { type: string }
""",
    )
    write(
        "content/catalog.yaml",
        """
revision: 4
bolt: { name: Bolt }
nut:  { name: Nut }
""",
    )

    corpus = load_corpus(profile, tmp_path)

    assert sorted(record.identity for record in corpus.records) == ["bolt", "nut"]
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_keyed_map_document_keys_are_ref_identities_without_identified_by(
    tmp_path, profile_dir, write
) -> None:
    write(
        "profile/category.yaml",
        """
dialect: type/1
id: category
fields:
  label: { type: string }
---
dialect: source/1
of: category
layout: keyed_map
path: content/categories.yaml
metadata_keys: [revision]
""",
    )
    write(
        "profile/product.yaml",
        """
dialect: type/1
id: product
identified_by: id
fields:
  id:       { type: id }
  category: { type: ref, to: category }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
""",
    )
    write(
        "content/categories.yaml",
        """
revision: 1
hardware: { label: Hardware }
""",
    )
    write(
        "content/products.yaml",
        """
- { id: bolt, category: hardware }
- { id: mystery, category: missing }
""",
    )

    profile = load_profile(profile_dir)
    problems = errors_only(validate_corpus(profile, tmp_path))

    assert len(problems) == 1
    assert problems[0].record == "mystery"
    assert problems[0].field == "category"
    assert "names no record of type 'category'" in problems[0].message


def test_keyed_map_with_record_keys_from_another_types_id_set(tmp_path, profile_dir, write) -> None:
    write(
        "profile/product.yaml",
        PRODUCT_TYPE
        + """
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
""",
    )
    write(
        "profile/price.yaml",
        """
dialect: type/1
id: price
identified_by: id
fields:
  amount: { type: int, unit: cents }
---
dialect: source/1
of: price
layout: keyed_map
path: content/prices.yaml
record_keys_from: product.id
metadata_keys: [revision]
""",
    )
    write("content/products.yaml", "- { id: bolt, name: Bolt }\n- { id: nut, name: Nut }\n")
    write(
        "content/prices.yaml",
        """
revision: 2
bolt: { amount: 30 }
nut:  { amount: 45 }
""",
    )
    profile = load_profile(profile_dir)
    corpus = load_corpus(profile, tmp_path)
    assert sorted(r.identity for r in corpus.of_type("price")) == ["bolt", "nut"]
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_keyed_map_refuses_a_key_declared_as_neither_record_nor_metadata(
    tmp_path, profile_dir, write
) -> None:
    write(
        "profile/product.yaml",
        PRODUCT_TYPE
        + """
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
""",
    )
    write(
        "profile/price.yaml",
        """
dialect: type/1
id: price
identified_by: id
fields:
  amount: { type: int, unit: cents }
---
dialect: source/1
of: price
layout: keyed_map
path: content/prices.yaml
record_keys_from: product.id
metadata_keys: [revision]
""",
    )
    write("content/products.yaml", "- { id: bolt, name: Bolt }\n")
    write(
        "content/prices.yaml",
        """
revision: 2
revision_typo: 3
bolt: { amount: 30 }
""",
    )
    profile = load_profile(profile_dir)

    problems = errors_only(load_corpus(profile, tmp_path).diagnostics)

    assert len(problems) == 1
    problem = problems[0]
    assert problem.file == "content/prices.yaml"
    assert problem.record == "revision_typo"
    assert "unknown keyed_map key 'revision_typo'" in problem.message
    assert "declared record keys from 'product.id': [bolt]" in problem.message
    assert "declared metadata keys: [revision]" in problem.message


def test_keyed_map_reports_a_record_key_the_document_does_not_carry(
    tmp_path, profile_dir, write
) -> None:
    profile = _profile(
        profile_dir,
        write,
        """
dialect: source/1
of: product
layout: keyed_map
path: content/catalog.yaml
record_keys: [bolt, nut]
""",
    )
    write("content/catalog.yaml", "bolt: { id: bolt, name: Bolt }\n")
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "content/catalog.yaml"
    assert problems[0].record == "nut"


def test_single_is_one_record_with_no_identity(tmp_path, profile_dir, write) -> None:
    write(
        "profile/settings.yaml",
        """
dialect: type/1
id: settings
fields:
  theme:   { type: string }
  retries: { type: int }
---
dialect: source/1
of: settings
layout: single
path: content/settings.yaml
""",
    )
    profile = load_profile(profile_dir)
    write("content/settings.yaml", "theme: plain\nretries: 3\n")
    corpus = load_corpus(profile, tmp_path)
    assert len(corpus.records) == 1
    assert corpus.records[0].identity is None
    assert corpus.records[0].data == {"theme": "plain", "retries": 3}
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_a_single_document_diagnostic_still_names_file_and_field(tmp_path, profile_dir, write) -> None:
    write(
        "profile/settings.yaml",
        """
dialect: type/1
id: settings
fields:
  theme:   { type: string }
  retries: { type: int }
---
dialect: source/1
of: settings
layout: single
path: content/settings.yaml
""",
    )
    profile = load_profile(profile_dir)
    write("content/settings.yaml", "theme: plain\nretries: many\n")
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "content/settings.yaml"
    assert problems[0].field == "retries"
    assert problems[0].record == "<the document>"


def test_a_declared_source_that_does_not_exist_is_reported(tmp_path, profile_dir, write) -> None:
    profile = _profile(
        profile_dir,
        write,
        """
dialect: source/1
of: product
layout: rows
path: content/products.yaml
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert [p.file for p in problems] == ["content/products.yaml"]
    assert "does not exist" in problems[0].message
