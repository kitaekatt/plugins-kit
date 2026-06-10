"""Registry of typed-unit schemas.

A typed unit is a top-level YAML key with a registered schema. Schemas live in
the schemas/ modules and self-register on import via register_schema().

Two role classes:
- SKILL_TYPE_ROOTS: roots that identify a SKILL.md as being a particular kind
  of skill (reference_skill, pattern_skill, etc.). Mutual exclusion: more than
  one in a single document signals mixed-type drift.
- PORTABLE_UNIT_ROOTS: roots that may appear standalone or nested inside any
  skill-type unit (references, facts, area_config, sub_areas, actions). No
  mutual exclusion.

claude_md is registered separately -- it identifies a CLAUDE.md, not a SKILL.md.

Every registered schema declares owner_doc: a plugin-root-relative path to the
canonical prose spec for the unit. A corpus audit asserts each owner doc
contains a valid instance of its schema (see checks.py).
"""

from __future__ import annotations

# Registry data: populated by self-registering schema modules.
SCHEMAS_BY_ROOT: dict[str, dict] = {}
OWNER_DOCS: dict[str, str] = {}

# Role membership: each role is a tuple ordered by registration (treated as
# canonical iteration order for stable audit output).
SKILL_TYPE_ROOTS: tuple[str, ...] = ()
PORTABLE_UNIT_ROOTS: tuple[str, ...] = ()


def register_schema(
    root: str,
    schema: dict,
    *,
    role: str,
    owner_doc: str | None,
) -> None:
    """Register a schema with the registry.

    Args:
        root: the top-level YAML key the schema validates.
        schema: the schema dict (must carry "root": <root> matching this name).
        role: "skill_type" | "portable" | "claude_md".
        owner_doc: plugin-root-relative path to the canonical prose spec, or
            None to opt out of the owner-doc check (existing schemas without a
            single-file owner can pass None and the check will skip them).
    """
    global SKILL_TYPE_ROOTS, PORTABLE_UNIT_ROOTS

    if root in SCHEMAS_BY_ROOT:
        raise ValueError(f"schema already registered for root '{root}'")
    if schema.get("root") != root:
        raise ValueError(
            f"schema['root']={schema.get('root')!r} does not match registration root {root!r}"
        )

    SCHEMAS_BY_ROOT[root] = schema
    if owner_doc is not None:
        OWNER_DOCS[root] = owner_doc

    if role == "skill_type":
        SKILL_TYPE_ROOTS = SKILL_TYPE_ROOTS + (root,)
    elif role == "portable":
        PORTABLE_UNIT_ROOTS = PORTABLE_UNIT_ROOTS + (root,)
    elif role == "claude_md":
        pass  # claude_md is its own role, neither skill-type nor portable
    else:
        raise ValueError(f"unknown role '{role}' for schema '{root}'")


def resolve_schema(yaml_data: dict) -> tuple[str, dict] | tuple[None, None]:
    """Return (root_key, schema) for the YAML data, or (None, None) if no
    recognized root is present.

    When a block carries both a skill-type root AND a portable root at the same
    top level (e.g. `reference_skill:` + top-level `references:` in a single
    fenced block -- a valid layout per content-authoring's typed-unit composition),
    skill-type roots take precedence. A document identifies as ONE skill type;
    portable units (references, facts, area_config, ...) are subordinate content.
    """
    if not isinstance(yaml_data, dict):
        return None, None

    # Prefer skill-type roots over portable roots when both are present.
    for root in SKILL_TYPE_ROOTS:
        if root in yaml_data:
            return root, SCHEMAS_BY_ROOT[root]
    for root, schema in SCHEMAS_BY_ROOT.items():
        if root in yaml_data:
            return root, schema
    return None, None


def detect_mixed_type_yaml(yaml_data: dict) -> list[str]:
    """Return a list of skill-type root keys present in yaml_data.

    More than one means mixed-type drift within a single block.
    """
    if not isinstance(yaml_data, dict):
        return []
    return [root for root in SKILL_TYPE_ROOTS if root in yaml_data]
