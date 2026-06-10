from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from agent_glue_lib.core import LoaderError, dump_instance, load_catalog, load_instances


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def make_minimal_kit(root: Path) -> None:
    write(root / "components" / "Name.yaml", """
        kind: Name
        fields:
          name:
            type: string
            required: true
    """)
    write(root / "components" / "Topology.yaml", """
        kind: Topology
        fields:
          in:
            type: string
            required: true
          out:
            type: string
            required: true
    """)
    write(root / "entities" / "Node.yaml", """
        name: Node
        components:
          required:
            - Name
            - Topology
    """)


# ---------- catalog loading ----------

def test_load_catalog_minimal(tmp_path):
    make_minimal_kit(tmp_path)
    catalog = load_catalog([tmp_path / "components"], [tmp_path / "entities"])
    assert set(catalog.component_schemas.keys()) == {"Name", "Topology"}
    assert "Node" in catalog.entity_types


def test_load_catalog_filename_kind_mismatch_rejected(tmp_path):
    write(tmp_path / "components" / "Foo.yaml", """
        kind: Bar
        fields: {}
    """)
    with pytest.raises(LoaderError, match="filename stem 'Foo' must match kind 'Bar'"):
        load_catalog([tmp_path / "components"], [])


def test_load_catalog_entity_filename_mismatch_rejected(tmp_path):
    write(tmp_path / "entities" / "Foo.yaml", """
        name: Bar
        components: {}
    """)
    with pytest.raises(LoaderError, match="filename stem 'Foo' must match name 'Bar'"):
        load_catalog([], [tmp_path / "entities"])


def test_load_catalog_duplicate_component_rejected(tmp_path):
    write(tmp_path / "a" / "Name.yaml", """
        kind: Name
        fields:
          name:
            type: string
    """)
    write(tmp_path / "b" / "Name.yaml", """
        kind: Name
        fields:
          name:
            type: string
    """)
    with pytest.raises(LoaderError, match="duplicate component schema 'Name'"):
        load_catalog([tmp_path / "a", tmp_path / "b"], [])


def test_load_catalog_unknown_top_level_key_rejected(tmp_path):
    write(tmp_path / "components" / "Bogus.yaml", """
        kind: Bogus
        bogus_key: nope
        fields: {}
    """)
    with pytest.raises(LoaderError, match="unexpected top-level keys"):
        load_catalog([tmp_path / "components"], [])


def test_load_catalog_invalid_field_schema(tmp_path):
    write(tmp_path / "components" / "Bad.yaml", """
        kind: Bad
        fields:
          x:
            bogus_attr: 1
    """)
    with pytest.raises(LoaderError, match="invalid field schema"):
        load_catalog([tmp_path / "components"], [])


# ---------- instance loading ----------

def test_load_instances_skips_non_instance_yaml(tmp_path):
    make_minimal_kit(tmp_path)
    # A yaml without `type:` is not an instance.
    write(tmp_path / "consumer" / "settings.yaml", """
        anything: goes
    """)
    instances = load_instances(tmp_path / "consumer")
    assert instances == []


def test_load_instances_rejects_unexpected_top_level_keys(tmp_path):
    write(tmp_path / "consumer" / "node.yaml", """
        type: Node
        components: {}
        sneaky: 1
    """)
    with pytest.raises(LoaderError, match="unexpected top-level keys"):
        load_instances(tmp_path / "consumer")


def test_load_instances_rejects_non_mapping_component(tmp_path):
    write(tmp_path / "consumer" / "node.yaml", """
        type: Node
        components:
          name: "bare string is wrong"
    """)
    with pytest.raises(LoaderError, match="must be a mapping"):
        load_instances(tmp_path / "consumer")


def test_load_instances_skip_dirs(tmp_path):
    make_minimal_kit(tmp_path)
    # An instance under components/ would otherwise be discovered; skip it.
    write(tmp_path / "consumer" / "node.yaml", """
        type: Node
        components:
          name: {name: load}
          topology: {in: A, out: B}
    """)
    instances = load_instances(tmp_path / "consumer", skip=[tmp_path / "components"])
    assert len(instances) == 1


# ---------- round-trip ----------

def test_round_trip_identity(tmp_path):
    text = textwrap.dedent("""
        type: Node
        components:
          name:
            name: load
          topology:
            in: LoadIn
            out: LoadResult
          implementation:
            module: impl
            function: execute
    """).lstrip()
    inst_path = tmp_path / "load.yaml"
    inst_path.write_text(text, encoding="utf-8")
    instances = load_instances(tmp_path)
    assert len(instances) == 1
    dumped = dump_instance(instances[0])
    reparsed = yaml.safe_load(dumped)
    original = yaml.safe_load(text)
    assert reparsed == original


def test_round_trip_with_nested_lists_and_maps(tmp_path):
    text = textwrap.dedent("""
        type: Graph
        components:
          name:
            name: pipeline
          state_decl:
            init:
              brief: str
              audit: "list[AuditRecord]"
            accumulated:
              dispositions: "list[Disposition]"
          config:
            settings:
              max_parallel: 4
              flags:
                show_work: true
    """).lstrip()
    inst_path = tmp_path / "graph.yaml"
    inst_path.write_text(text, encoding="utf-8")
    instances = load_instances(tmp_path)
    dumped = dump_instance(instances[0])
    assert yaml.safe_load(dumped) == yaml.safe_load(text)


# ---------- empty / odd inputs ----------

def test_load_instances_empty_directory_returns_empty(tmp_path):
    (tmp_path / "consumer").mkdir()
    assert load_instances(tmp_path / "consumer") == []


def test_load_instances_missing_directory_returns_empty(tmp_path):
    assert load_instances(tmp_path / "nonexistent") == []


def test_load_catalog_missing_directory_returns_empty():
    catalog = load_catalog(["nonexistent-dir"], [])
    assert catalog.component_schemas == {}
