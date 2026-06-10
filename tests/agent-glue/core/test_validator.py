from __future__ import annotations

import textwrap
from pathlib import Path

from agent_glue_lib.core import (
    load_catalog,
    load_instances,
    validate_all,
    validate_instances,
    validate_kit,
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def make_kit(root: Path) -> None:
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
    write(root / "components" / "Outputs.yaml", """
        kind: Outputs
        fields:
          artifacts:
            type: list
            required: true
            items:
              type: map
              keys:
                path:
                  type: string
                  required: true
                format:
                  type: enum
                  required: true
                  values: [yaml, json, text, managed]
    """)
    write(root / "components" / "Config.yaml", """
        kind: Config
        fields:
          settings:
            type: map
            required: true
            value_type:
              type: any
    """)
    write(root / "entities" / "Node.yaml", """
        name: Node
        components:
          required:
            - Name
            - Topology
          optional:
            - Outputs
            - Config
    """)


def load(root: Path):
    catalog = load_catalog([root / "components"], [root / "entities"])
    return catalog


# ---------- kit consistency ----------

def test_validate_kit_clean(tmp_path):
    make_kit(tmp_path)
    catalog = load(tmp_path)
    assert validate_kit(catalog) == []


def test_validate_kit_unknown_component_reference(tmp_path):
    make_kit(tmp_path)
    write(tmp_path / "entities" / "Bad.yaml", """
        name: Bad
        components:
          required:
            - Name
            - Missing
    """)
    catalog = load(tmp_path)
    errors = validate_kit(catalog)
    assert any("'Missing' is not a known component schema" in e for e in errors)


def test_validate_kit_required_and_optional_overlap(tmp_path):
    make_kit(tmp_path)
    write(tmp_path / "entities" / "Dup.yaml", """
        name: Dup
        components:
          required:
            - Name
          optional:
            - Name
    """)
    catalog = load(tmp_path)
    errors = validate_kit(catalog)
    assert any("listed as both required and optional" in e for e in errors)


def test_validate_kit_unknown_field_type(tmp_path):
    write(tmp_path / "components" / "Bogus.yaml", """
        kind: Bogus
        fields:
          x:
            type: gibberish
    """)
    catalog = load(tmp_path)
    errors = validate_kit(catalog)
    assert any("unknown type 'gibberish'" in e for e in errors)


def test_validate_kit_enum_requires_values(tmp_path):
    write(tmp_path / "components" / "Bad.yaml", """
        kind: Bad
        fields:
          x:
            type: enum
    """)
    catalog = load(tmp_path)
    errors = validate_kit(catalog)
    assert any("type=enum requires non-empty `values`" in e for e in errors)


def test_validate_kit_list_requires_items(tmp_path):
    write(tmp_path / "components" / "Bad.yaml", """
        kind: Bad
        fields:
          x:
            type: list
    """)
    catalog = load(tmp_path)
    errors = validate_kit(catalog)
    assert any("type=list requires `items` schema" in e for e in errors)


def test_validate_kit_map_keys_and_value_type_mutually_exclusive(tmp_path):
    write(tmp_path / "components" / "Bad.yaml", """
        kind: Bad
        fields:
          x:
            type: map
            keys:
              a:
                type: string
            value_type:
              type: any
    """)
    catalog = load(tmp_path)
    errors = validate_kit(catalog)
    assert any("cannot set both `keys` and `value_type`" in e for e in errors)


# ---------- instance shape ----------

def test_validate_instance_missing_required_component(tmp_path):
    make_kit(tmp_path)
    catalog = load(tmp_path)
    write(tmp_path / "graphs" / "p" / "nodes" / "n" / "node.yaml", """
        type: Node
        components:
          name:
            name: n
    """)
    instances = load_instances(tmp_path / "graphs")
    errors = validate_instances(catalog, instances)
    assert any("missing required components" in e and "topology" in e for e in errors)


def test_validate_instance_unknown_entity_type(tmp_path):
    make_kit(tmp_path)
    catalog = load(tmp_path)
    write(tmp_path / "graphs" / "x.yaml", """
        type: BogusEntity
        components: {}
    """)
    instances = load_instances(tmp_path / "graphs")
    errors = validate_instances(catalog, instances)
    assert any("unknown entity type 'BogusEntity'" in e for e in errors)


def test_validate_instance_unknown_component(tmp_path):
    make_kit(tmp_path)
    catalog = load(tmp_path)
    write(tmp_path / "graphs" / "n" / "node.yaml", """
        type: Node
        components:
          name:
            name: n
          topology:
            in: A
            out: B
          extras:
            anything: 1
    """)
    instances = load_instances(tmp_path / "graphs")
    errors = validate_instances(catalog, instances)
    assert any("components not allowed on entity 'Node'" in e and "extras" in e for e in errors)


def test_validate_instance_missing_required_field(tmp_path):
    make_kit(tmp_path)
    catalog = load(tmp_path)
    write(tmp_path / "graphs" / "n" / "node.yaml", """
        type: Node
        components:
          name:
            name: n
          topology:
            in: A
    """)
    instances = load_instances(tmp_path / "graphs")
    errors = validate_instances(catalog, instances)
    assert any("missing required fields" in e and "out" in e for e in errors)


def test_validate_instance_wrong_field_type(tmp_path):
    make_kit(tmp_path)
    catalog = load(tmp_path)
    write(tmp_path / "graphs" / "n" / "node.yaml", """
        type: Node
        components:
          name:
            name: 7
          topology:
            in: A
            out: B
    """)
    instances = load_instances(tmp_path / "graphs")
    errors = validate_instances(catalog, instances)
    assert any("expected string, got int" in e for e in errors)


def test_validate_instance_bool_not_accepted_as_int(tmp_path):
    write(tmp_path / "components" / "Version.yaml", """
        kind: Version
        fields:
          value:
            type: int
            required: true
    """)
    write(tmp_path / "entities" / "Versioned.yaml", """
        name: Versioned
        components:
          required:
            - Version
    """)
    catalog = load(tmp_path)
    write(tmp_path / "ix" / "v.yaml", """
        type: Versioned
        components:
          version:
            value: true
    """)
    instances = load_instances(tmp_path / "ix")
    errors = validate_instances(catalog, instances)
    assert any("expected int" in e for e in errors)


def test_validate_instance_enum_bad_value(tmp_path):
    make_kit(tmp_path)
    catalog = load(tmp_path)
    write(tmp_path / "n.yaml", """
        type: Node
        components:
          name:
            name: n
          topology:
            in: A
            out: B
          outputs:
            artifacts:
              - path: out/x.yaml
                format: xml
    """)
    instances = load_instances(tmp_path)
    errors = validate_instances(catalog, instances)
    assert any("enum value 'xml' not in allowed values" in e for e in errors)


def test_validate_instance_list_items_validated(tmp_path):
    make_kit(tmp_path)
    catalog = load(tmp_path)
    write(tmp_path / "n.yaml", """
        type: Node
        components:
          name:
            name: n
          topology:
            in: A
            out: B
          outputs:
            artifacts:
              - path: 5
                format: yaml
    """)
    instances = load_instances(tmp_path)
    errors = validate_instances(catalog, instances)
    assert any("expected string" in e and "path" in e for e in errors)


def test_validate_instance_map_value_type_validated(tmp_path):
    write(tmp_path / "components" / "Settings.yaml", """
        kind: Settings
        fields:
          values:
            type: map
            required: true
            value_type:
              type: int
    """)
    write(tmp_path / "entities" / "Configured.yaml", """
        name: Configured
        components:
          required:
            - Settings
    """)
    catalog = load(tmp_path)
    write(tmp_path / "ix" / "c.yaml", """
        type: Configured
        components:
          settings:
            values:
              a: 1
              b: "two"
    """)
    instances = load_instances(tmp_path / "ix")
    errors = validate_instances(catalog, instances)
    assert any("expected int" in e for e in errors)


def test_validate_instance_unknown_field(tmp_path):
    make_kit(tmp_path)
    catalog = load(tmp_path)
    write(tmp_path / "n.yaml", """
        type: Node
        components:
          name:
            name: n
            uninvited: yes
          topology:
            in: A
            out: B
    """)
    instances = load_instances(tmp_path)
    errors = validate_instances(catalog, instances)
    assert any("unknown fields" in e and "uninvited" in e for e in errors)


def test_validate_all_passes_when_kit_and_instances_clean(tmp_path):
    make_kit(tmp_path)
    catalog = load(tmp_path)
    write(tmp_path / "n.yaml", """
        type: Node
        components:
          name:
            name: n
          topology:
            in: A
            out: B
          config:
            settings:
              foo: 1
              bar: true
    """)
    instances = load_instances(tmp_path)
    assert validate_all(catalog, instances) == []
