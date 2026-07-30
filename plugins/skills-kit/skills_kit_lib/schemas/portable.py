"""Portable typed-unit schemas.

A portable unit is a typed YAML key that may appear standalone as its own
fenced block OR nested inside a skill-type unit. Portable units coexist
freely; there is no mixed-type drift detection across portable roots.

Each schema declares owner_doc -- the plugin-root-relative path to the
canonical prose spec for the unit. The corpus audit asserts each owner doc
contains a valid instance of its schema.
"""

from ..rule_fragments import FACT_ITEM_RULE, KEYWORDS_RULE
from ..schema_registry import register_schema


REFERENCES_SCHEMA = {
    "root": "references",
    "owner_doc": "skills/md-domain/references/skill-domain/schema-fixtures.md",
    "root_type": "list",
    "min_len": 1,
    "items": {"keys": {
        "id": {"type": "string", "required": True},
        "path": {"type": "string", "required": True},
        "keywords": KEYWORDS_RULE,
        "summary": {"type": "string", "required": True},
    }},
}


FACTS_SCHEMA = {
    "root": "facts",
    "owner_doc": "skills/md-domain/references/skill-domain/schema-fixtures.md",
    "root_type": "list",
    "min_len": 1,
    "items": FACT_ITEM_RULE,
}


# AREA_CONFIG_SCHEMA -- the runtime contract for an area.
# Six optional fields per area-config.md. The same shape attaches at the area
# level (single-area document) or per sub-area (multi-area document).
AREA_CONFIG_SCHEMA = {
    "root": "area_config",
    "owner_doc": "skills/md-domain/references/authoring-patterns/area-config.md",
    "root_type": "dict",
    "keys": {
        "state_terms": {"type": "list", "required": False,
                        "note": "canonical state vocabulary the agent uses verbatim"},
        "operations": {"type": "list", "required": False,
                       "note": "verbs the area supports"},
        "scope_axes": {"type": "list", "required": False,
                       "items": {"keys": {
                           "name": {"type": "string", "required": True},
                           "values": {"type": "list", "required": True, "min_len": 1},
                       }},
                       "note": "axes that decompose the operation space"},
        "canonical_phrasing": {"type": "string", "required": False,
                               "note": "readback rule with placeholders"},
        "llm_dependent_content": {"type": "list", "required": False,
                                  "note": "fields whose values are LLM-produced"},
        "dependency_order": {"type": "list", "required": False,
                             "note": "ordering constraints among operations or capabilities"},
    },
}


# SUB_AREAS_SCHEMA -- the sub-area registry for a multi-area document.
# Each entry names a sub-area with description and routing keyword_cues;
# reference is required when attachment Pattern 3c is in use.
SUB_AREAS_SCHEMA = {
    "root": "sub_areas",
    "owner_doc": "skills/md-domain/references/authoring-patterns/area-ownership.md",
    "root_type": "list",
    "min_len": 1,
    "items": {"keys": {
        "name": {"type": "string", "required": True,
                 "note": "canonical sub-area identifier"},
        "description": {"type": "string", "required": True,
                        "note": "one sentence stating the sub-area's scope"},
        "keyword_cues": {"type": "list", "required": True, "min_len": 1,
                         "note": "phrases that route a user request to this sub-area"},
        "reference": {"type": "string", "required": False,
                      "note": "path to deeper documentation; required for attachment Pattern 3c"},
    }},
}


# ASSET_DEPENDENCIES_SCHEMA -- runtime asset-dependency declarations.
# Each record names a repo file the skill consumes at RUNTIME (via a bundled
# script or a tool-argument example) -- an edge invisible to md-citation
# scanning. Declaring it makes the edge auditable: the per-skill audit
# resolves every declared path against the skill dir and the project root,
# so moving/renaming the asset FAILs the consumer's audit instead of
# breaking silently at runtime. The optional invariant field carries a
# mirrors-style sync contract ("script X mirrors section Y of the asset").
ASSET_DEPENDENCIES_SCHEMA = {
    "root": "asset_dependencies",
    "owner_doc": "skills/md-domain/references/skill-domain/schema-fixtures.md",
    "root_type": "list",
    "min_len": 1,
    "items": {"keys": {
        "path": {"type": "string", "required": True,
                 "note": "skill-dir-relative or project-root-relative path to the consumed file"},
        "consumer": {"type": "string", "required": False,
                     "note": "the script or doc inside this skill that consumes the asset (e.g. workflow.js)"},
        "purpose": {"type": "string", "required": True,
                    "note": "what the asset is consumed for at runtime"},
        "invariant": {"type": "string", "required": False,
                      "note": "sync contract the consumer must uphold against the asset (mirrors-style SSOT pointer)"},
    }},
}


# ACTIONS_SCHEMA -- ordered-list-of-operations recipes.
# Root is a dict of arbitrary action names; every value matches the
# action-record shape. Uses value_schema (dict-of-records with arbitrary keys).
ACTIONS_SCHEMA = {
    "root": "actions",
    "owner_doc": "skills/md-domain/references/authoring-patterns/actions-pattern.md",
    "root_type": "dict",
    "value_schema": {"keys": {
        "description": {"type": "string", "required": True,
                        "note": "one sentence stating what the action achieves end-to-end"},
        "prerequisite": {"type": "string", "required": False,
                         "note": "conditions verified or assumed true before starting"},
        "inputs": {"type": "dict", "required": False,
                   "note": "per-parameter dictionary; names appear as <param_name> placeholders"},
        "steps": {"type": "list", "required": True, "min_len": 1,
                  "note": "ordered list of tool steps and tell_user steps"},
    }},
}


register_schema("references", REFERENCES_SCHEMA, role="portable",
                owner_doc=REFERENCES_SCHEMA["owner_doc"])
register_schema("facts", FACTS_SCHEMA, role="portable",
                owner_doc=FACTS_SCHEMA["owner_doc"])
register_schema("area_config", AREA_CONFIG_SCHEMA, role="portable",
                owner_doc=AREA_CONFIG_SCHEMA["owner_doc"])
register_schema("sub_areas", SUB_AREAS_SCHEMA, role="portable",
                owner_doc=SUB_AREAS_SCHEMA["owner_doc"])
register_schema("actions", ACTIONS_SCHEMA, role="portable",
                owner_doc=ACTIONS_SCHEMA["owner_doc"])
register_schema("asset_dependencies", ASSET_DEPENDENCIES_SCHEMA, role="portable",
                owner_doc=ASSET_DEPENDENCIES_SCHEMA["owner_doc"])
