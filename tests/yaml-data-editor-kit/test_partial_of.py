"""``partial_of`` -- named override layers, with ``routes`` for foreign keys."""

from pathlib import Path
from typing import Callable

from yaml_data_editor_kit.schema import (
    Corpus,
    Profile,
    Validator,
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
id: app
identified_by: id
fields:
  id:      { type: id }
  retries: { type: int, min: 0 }
  theme:   { type: string }
---
dialect: source/1
of: app
layout: rows
path: content/apps.yaml
---
dialect: type/1
id: combat_config
identified_by: id
fields:
  id:              { type: id }
  duration_ratio:  { type: float }
---
dialect: source/1
of: combat_config
layout: rows
path: content/combat_configs.yaml
---
dialect: type/1
id: deployment
identified_by: id
fields:
  id: { type: id }
  configs:
    type: map
    key: { type: id }
    value:
      partial_of: app
      routes: { duration_ratio: combat_config.duration_ratio }
---
dialect: source/1
of: deployment
layout: rows
path: content/deployments.yaml
"""


SYNTHETIC_IDENTITY_PROFILE = """
dialect: type/1
id: app
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: app
layout: rows
path: content/apps.yaml
---
dialect: type/1
id: lookup
identified_by: id
fields:
  value: { type: int }
---
dialect: source/1
of: lookup
layout: keyed_map
path: content/lookups.yaml
record_keys: [default]
---
dialect: type/1
id: deployment
identified_by: id
fields:
  id: { type: id }
  configs:
    type: map
    key: { type: id }
    value:
      partial_of: app
      routes: { lookup_name: lookup.id }
---
dialect: source/1
of: deployment
layout: rows
path: content/deployments.yaml
"""


ROUTE_KEY_PROFILE = (
    """
dialect: type/1
id: widget
identified_by: id
fields:
  id: { type: id }
---
dialect: source/1
of: widget
layout: rows
path: content/widgets.yaml
---
"""
    + PROFILE.replace(
        "  duration_ratio:  { type: float }",
        """  tuning:
    type: map
    key: { type: ref, to: widget }
    value: { type: float }""",
    ).replace(
        "routes: { duration_ratio: combat_config.duration_ratio }",
        "routes: { turret_ratio: combat_config.tuning.turret }",
    )
)

_ROUTE_OWNER_MARKER = "\n---\ndialect: type/1\nid: deployment\n"
ROUTE_KEY_TARGET_PROFILE, _route_key_owner = ROUTE_KEY_PROFILE.rsplit(
    _ROUTE_OWNER_MARKER,
    maxsplit=1,
)
ROUTE_KEY_OWNER_PROFILE = "dialect: type/1\nid: deployment\n" + _route_key_owner


def _write_route_key_profile(write: Writer) -> None:
    write("profile/targets.yaml", ROUTE_KEY_TARGET_PROFILE)
    write("profile/deployment.yaml", ROUTE_KEY_OWNER_PROFILE)


def _load(profile_dir: Path, write: Writer, deployments: str) -> Profile:
    write("profile/deployment.yaml", PROFILE)
    write("content/apps.yaml", "- { id: main, retries: 2, theme: plain }\n")
    write("content/combat_configs.yaml", "- { id: default, duration_ratio: 1.0 }\n")
    write("content/deployments.yaml", deployments)
    return load_profile(profile_dir)


def test_a_layer_may_carry_a_sparse_subset_of_the_targets_fields(
    tmp_path, profile_dir, write
) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: staging
  configs:
    quiet: { retries: 0 }
""",
    )
    # `theme` is absent, and absent means "this layer does not override it".
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_a_present_field_is_validated_by_its_declaration_on_the_target_type(
    tmp_path, profile_dir, write
) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: staging
  configs:
    quiet: { retries: -1 }
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "content/deployments.yaml"
    assert problems[0].record == "staging"
    assert problems[0].field == "configs.quiet.retries"
    assert "below the declared min of 0" in problems[0].message


def test_a_declared_route_carries_a_key_belonging_to_another_record(
    tmp_path, profile_dir, write
) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: staging
  configs:
    quiet: { retries: 0, duration_ratio: 0.5 }
""",
    )
    assert errors_only(validate_corpus(profile, tmp_path)) == []


def test_a_routed_key_is_validated_against_the_field_it_routes_to(
    tmp_path, profile_dir, write
) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: staging
  configs:
    quiet: { duration_ratio: fast }
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].field == "configs.quiet.duration_ratio"
    assert "declared 'float'" in problems[0].message


def test_a_route_to_a_declared_identity_field_keeps_its_field_spec(
    tmp_path, profile_dir, write
) -> None:
    routed_to_identity = PROFILE.replace(
        "routes: { duration_ratio: combat_config.duration_ratio }",
        "routes: { config_name: combat_config.id }",
    )
    write("profile/deployment.yaml", routed_to_identity)
    write("content/apps.yaml", "- { id: main, retries: 2, theme: plain }\n")
    write("content/combat_configs.yaml", "- { id: default, duration_ratio: 1.0 }\n")
    write(
        "content/deployments.yaml",
        "- id: staging\n  configs:\n    quiet: { config_name: [not, a, string] }\n",
    )

    profile = load_profile(profile_dir)
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].field == "configs.quiet.config_name"
    assert "is declared 'id' but holds ['not', 'a', 'string']" in problems[0].message


def test_a_route_to_a_synthetic_identity_has_explicit_id_semantics(
    tmp_path, profile_dir, write
) -> None:
    write("profile/deployment.yaml", SYNTHETIC_IDENTITY_PROFILE)
    write("content/apps.yaml", "- { id: main }\n")
    write("content/lookups.yaml", "default: { value: 1 }\n")
    write(
        "content/deployments.yaml",
        "- id: staging\n  configs:\n    quiet: { lookup_name: [not, an, id] }\n",
    )

    profile = load_profile(profile_dir)
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].field == "configs.quiet.lookup_name"
    assert "is declared 'id' but holds ['not', 'an', 'id']" in problems[0].message


def test_a_route_target_map_key_must_name_a_real_ref_identity(
    tmp_path, profile_dir, write
) -> None:
    _write_route_key_profile(write)
    write("content/widgets.yaml", "- { id: hull }\n")
    write("content/apps.yaml", "- { id: main, retries: 2, theme: plain }\n")
    write("content/combat_configs.yaml", "- { id: default, tuning: { hull: 1.0 } }\n")
    write(
        "content/deployments.yaml",
        "- id: staging\n  configs:\n    quiet: {}\n",
    )

    profile = load_profile(profile_dir)
    problems = [
        diagnostic
        for diagnostic in errors_only(validate_corpus(profile, tmp_path))
        if diagnostic.field == "combat_config.tuning.turret"
    ]
    assert len(problems) == 1
    assert problems[0].file == "deployment.yaml"
    assert "key 'turret'" in problems[0].message
    assert "map 'combat_config.tuning'" in problems[0].message
    assert "names no record of type 'widget'" in problems[0].message


def test_the_public_validator_boundary_checks_recorded_route_paths(
    tmp_path, profile_dir, write
) -> None:
    _write_route_key_profile(write)
    profile = load_profile(profile_dir)

    problems = [
        diagnostic
        for diagnostic in errors_only(Validator(profile, Corpus(root=tmp_path)).run())
        if diagnostic.field == "combat_config.tuning.turret"
    ]
    assert len(problems) == 1
    assert problems[0].file == "deployment.yaml"


def test_an_unrouted_foreign_key_is_refused(tmp_path, profile_dir, write) -> None:
    profile = _load(
        profile_dir,
        write,
        """
- id: staging
  configs:
    quiet: { retires: 0 }
""",
    )
    problems = errors_only(validate_corpus(profile, tmp_path))
    assert len(problems) == 1
    assert problems[0].file == "content/deployments.yaml"
    assert problems[0].record == "staging"
    assert problems[0].field == "configs.quiet.retires"
    assert "nor a declared route" in problems[0].message
