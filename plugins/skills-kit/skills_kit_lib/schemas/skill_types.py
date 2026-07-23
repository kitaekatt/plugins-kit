"""Skill-type schemas.

One schema per canonical skill type. A SKILL.md carries exactly one
skill-type unit; more than one signals mixed-type drift (see schema_registry
SKILL_TYPE_ROOTS + audit-time cross-block drift detection).

Each schema declares owner_doc -- the plugin-root-relative path to the
canonical prose spec for the type. The corpus audit asserts each owner doc
contains a valid instance of its schema.
"""

from ..rule_fragments import (
    ANTI_PATTERNS_RULE,
    FACT_ITEM_RULE,
    IDENTITY_RULE,
    KEYWORDS_RULE,
    SCOPE_RULE,
    TECHNIQUE_STEP_RULE,
)
from ..schema_registry import register_schema


REFERENCE_SKILL_SCHEMA = {
    "root": "reference_skill",
    "owner_doc": "skills/skill-authoring/references/framework.md",
    "keys": {
        "_schema_version": {"type": "string", "required": False},
        "identity": IDENTITY_RULE,
        "scope": SCOPE_RULE,
        "facts": {
            "type": "list",
            "required": False,
            "min_len": 1,
            "items": FACT_ITEM_RULE,
            "note": "facts may live nested here OR as a top-level facts: portable unit; "
                    "the document must contain at least one fact source somewhere "
                    "(audit-time cross-source rule)",
        },
        "groupings": {"type": "list", "required": False, "items": {"keys": {
            "name": {"type": "string", "required": True},
            "keywords": KEYWORDS_RULE,
            "fact_ids": {"type": "list", "required": True, "min_len": 1},
        }}},
        "references": {"type": "list", "required": False, "items": {"keys": {
            "id": {"type": "string", "required": True},
            "path": {"type": "string", "required": True},
            "keywords": KEYWORDS_RULE,
            "summary": {"type": "string", "required": True},
        }}},
    },
    "forbidden_keys": ["procedures", "steps", "rules", "counters", "patterns",
                       "apply_when", "do_not_apply_when", "members", "index",
                       "standards_set"],
}


PATTERN_SKILL_SCHEMA = {
    "root": "pattern_skill",
    "owner_doc": "skills/skill-authoring/references/framework.md",
    "keys": {
        "_schema_version": {"type": "string", "required": False},
        "identity": IDENTITY_RULE,
        "scope": SCOPE_RULE,
        "patterns": {
            "type": "list",
            "required": True,
            "min_len": 1,
            "items": {"keys": {
                "id": {"type": "string", "required": True},
                "name": {"type": "string", "required": True},
                "keywords": KEYWORDS_RULE,
                "problem": {"type": "string", "required": True},
                "mechanic": {"type": "string", "required": True},
                "why": {"type": "string", "required": True},
                "apply_when": {"type": "list", "required": True, "min_len": 1, "items": {"keys": {
                    "signal": {"type": "string", "required": True},
                    "example": {"type": "string", "required": True},
                }}},
                "do_not_apply_when": {"type": "list", "required": True, "min_len": 1, "items": {"keys": {
                    "signal": {"type": "string", "required": True},
                    "counter_example": {"type": "string", "required": True},
                }}},
                "examples": {"type": "list", "required": True, "min_len": 1, "items": {"keys": {
                    "title": {"type": "string", "required": True},
                    "before": {"type": "string", "required": True},
                    "after": {"type": "string", "required": True},
                }}},
            }},
        },
    },
    "forbidden_keys": ["procedures", "steps", "rules", "counters", "facts",
                       "tools", "scripts", "members", "index", "standards_set"],
}


# Unified technique_skill schema. Every technique requires ordered steps
# (min_len 1). output_template is an optional companion field carrying the
# output-shape contract for the agent's reply -- not an alternative to
# steps. Trigger model (auto vs user-only) is metadata; it does not change
# what the body must contain. The caution surface is a document-level
# OR-rule: >=1 gotcha across techniques OR >=1 anti_patterns record
# (enforced by check_technique_caution_cross_rule in audit.py, following
# the facts cross-source precedent).
TECHNIQUE_SKILL_SCHEMA = {
    "root": "technique_skill",
    "owner_doc": "skills/skill-authoring/references/framework.md",
    "keys": {
        "_schema_version": {"type": "string", "required": False},
        "identity": IDENTITY_RULE,
        "scope": SCOPE_RULE,
        "trigger_model": {"type": "string", "required": False,
                          "note": "'auto' (default) or 'user-only'"},
        "techniques": {
            "type": "list",
            "required": True,
            "min_len": 1,
            "items": {"keys": {
                "id": {"type": "string", "required": True},
                "name": {"type": "string", "required": True},
                "keywords": KEYWORDS_RULE,
                "goal": {"type": "string", "required": True},
                "preconditions": {"type": "list", "required": False},
                "steps": {"type": "list", "required": True, "min_len": 1, "items": TECHNIQUE_STEP_RULE,
                          "note": "ordered-step procedure body; required for every technique regardless of trigger_model"},
                "output_template": {"type": "string", "required": False,
                                    "note": "optional output-shape contract for the agent's reply"},
                "arguments": {"type": "list", "required": False, "items": {"keys": {
                    "name": {"type": "string", "required": True},
                    "required": {"type": "bool", "required": True},
                    "description": {"type": "string", "required": True},
                }}},
                "gotchas": {"type": "list", "required": False, "min_len": 1,
                            "note": "caution-surface OR-rule: the document must carry >=1 gotcha "
                                    "across techniques OR >=1 anti_patterns record (document-level "
                                    "cross-rule in audit.py). State each caution once -- duplicating "
                                    "it across both containers makes the reader pay twice."},
                "validator": {"type": "dict", "required": False, "keys": {
                    "type": {"type": "string", "required": True},
                    "ref": {"type": "string", "required": False},
                }},
                "checklist": {"type": "list", "required": False,
                              "note": "Dec-8: when the technique has >3 steps, explicit step-tracking is required, satisfied by EITHER this paste-able checklist field OR an explicit step-tracker invocation. Audit enforces the OR-form on the rendered body."},
            }},
        },
        "anti_patterns": ANTI_PATTERNS_RULE,
    },
    "forbidden_keys": ["rules", "counters", "facts", "patterns",
                       "apply_when", "do_not_apply_when", "members", "index",
                       "standards_set"],
}

DISCIPLINE_SKILL_SCHEMA = {
    "root": "discipline_skill",
    "owner_doc": "skills/skill-authoring/references/framework.md",
    "keys": {
        "_schema_version": {"type": "string", "required": False},
        "identity": IDENTITY_RULE,
        "scope": SCOPE_RULE,
        "target": {"type": "dict", "required": True, "keys": {
            "type": {"type": "string", "required": True},
            "ref": {"type": "string", "required": True},
        }},
        "rules": {
            "type": "list",
            "required": True,
            "min_len": 1,
            "items": {"keys": {
                "id": {"type": "string", "required": True},
                "keywords": KEYWORDS_RULE,
                "statement": {"type": "string", "required": True,
                              "forbid_regex": r"\b(should|might|try to|consider|usually|prefer)\b",
                              "msg": "discipline rule statements must not hedge"},
                "why": {"type": "string", "required": True,
                        "note": "explain-the-why on every rule"},
                "counters": {"type": "list", "required": True, "min_len": 1, "items": {"keys": {
                    "excuse": {"type": "string", "required": True},
                    "reality": {"type": "string", "required": True},
                    "observed_in": {"type": "string", "required": True,
                                    "note": "every counter cites a baseline run; no hypotheticals"},
                }}},
                "red_flags": {"type": "list", "required": True, "min_len": 1},
                "exceptions": {"type": "list", "required": False, "items": {"keys": {
                    "case": {"type": "string", "required": True},
                    "rationale": {"type": "string", "required": True,
                                  "note": "bounded exceptions require explicit rationale"},
                }}},
            }},
        },
        "pressure_test": {"type": "dict", "required": True, "keys": {
            "baseline": {"type": "string", "required": True},
            "green": {"type": "string", "required": True},
            "refactor": {"type": "list", "required": True, "min_len": 1, "items": {"keys": {
                "loophole": {"type": "string", "required": True},
                "closed_by": {"type": "string", "required": True},
            }}},
        }},
        "anti_patterns": ANTI_PATTERNS_RULE,
    },
    "forbidden_keys": ["facts", "patterns", "apply_when", "do_not_apply_when",
                       "members", "index", "tools", "scripts", "standards_set"],
}


DOMAIN_SKILL_SCHEMA = {
    "root": "domain_skill",
    "owner_doc": "skills/skill-authoring/references/framework.md",
    "keys": {
        "_schema_version": {"type": "string", "required": False},
        "identity": IDENTITY_RULE,
        "companions": {"type": "dict", "required": True, "keys": {
            "siblings": {"type": "list", "required": True},
            "note": {"type": "string", "required": False},
        }},
        "scope": SCOPE_RULE,
        "orientation": {"type": "dict", "required": True, "keys": {
            "summary": {"type": "string", "required": True},
            "vocabulary": {"type": "list", "required": False, "items": {"keys": {
                "term": {"type": "string", "required": True},
                "definition": {"type": "string", "required": True},
            }}},
            "behavioral_guardrails": {"type": "list", "required": False},
        }},
        "index": {"type": "dict", "required": True, "keys": {
            "references": {"type": "list", "required": True, "min_len": 1, "items": {"keys": {
                "id": {"type": "string", "required": True},
                "path": {"type": "string", "required": True},
                "keywords": KEYWORDS_RULE,
                "summary": {"type": "string", "required": True},
            }}},
            "members": {"type": "list", "required": False, "items": {"keys": {
                "name": {"type": "string", "required": True},
                "type": {"type": "string", "required": True},
                "ref": {"type": "string", "required": True},
                "keywords": KEYWORDS_RULE,
            }}},
        }},
        "capabilities": {"type": "list", "required": False, "items": {"keys": {
            "id": {"type": "string", "required": True},
            "keywords": KEYWORDS_RULE,
            "description": {"type": "string", "required": True},
            "operation": {"type": "string", "required": True},
            "tool": {"type": "string", "required": False},
            "scope_axes": {"type": "list", "required": False},
            "reference_section": {"type": "string", "required": False},
        }}},
        "tools": {"type": "list", "required": False, "items": {"keys": {
            "name": {"type": "string", "required": True},
            "command": {"type": "string", "required": True},
            "description": {"type": "string", "required": True},
            "tests": {"type": "string", "required": False,
                      "note": "path to the tool's companion test (skill-dir-relative or "
                              "project-root-relative); audit resolves it. Convention: "
                              "skill-shipped script tests live next to the script under "
                              "the skill's scripts/, stdlib-only, and the project's test "
                              "inventory names them alongside the main suite."},
        }}},
        "agent_binding": {"type": "dict", "required": False, "keys": {
            "agent_name": {"type": "string", "required": True},
            "auto_load": {"type": "bool", "required": True},
        }},
    },
    "forbidden_keys": ["rules", "counters", "facts", "patterns",
                       "apply_when", "do_not_apply_when", "steps_at_root",
                       "standards_set"],
    "max_orientation_summary_words": 300,
}


CAPABILITY_SKILL_SCHEMA = {
    "root": "capability_skill",
    "owner_doc": "skills/skill-authoring/references/framework.md",
    "keys": {
        "_schema_version": {"type": "string", "required": False},
        "identity": IDENTITY_RULE,
        "scope": SCOPE_RULE,
        "external_capability": {"type": "dict", "required": True, "keys": {
            "kind": {"type": "string", "required": True,
                     "note": "tool | mcp_server | api | service | ide | framework | harness"},
            "name": {"type": "string", "required": True},
            "description": {"type": "string", "required": True},
        }},
        "layering": {"type": "dict", "required": True, "keys": {
            "claude_md": {"type": "list", "required": True,
                          "note": "L1 -- ambient setup-for-success in CLAUDE.md"},
            "skill_md": {"type": "list", "required": True, "min_len": 1,
                         "note": "L2 -- orientation + index + capabilities in this SKILL.md"},
            "references": {"type": "list", "required": True,
                           "note": "L3 -- deep mechanics in references/"},
        }},
        "capabilities": {
            "type": "list",
            "required": True,
            "min_len": 1,
            "items": {"keys": {
                "id": {"type": "string", "required": True},
                "keywords": KEYWORDS_RULE,
                "user_objective": {"type": "string", "required": True},
                "operation": {"type": "string", "required": True},
                "tool": {"type": "string", "required": False},
                "sub_cases": {"type": "list", "required": False},
                "scope_axes": {"type": "list", "required": False},
                "reference_section": {"type": "string", "required": False},
                "steps": {"type": "list", "required": False, "items": TECHNIQUE_STEP_RULE},
                "gotchas": {"type": "list", "required": False},
            }},
        },
        "members": {"type": "list", "required": False, "items": {"keys": {
            "name": {"type": "string", "required": True},
            "type": {"type": "string", "required": True},
            "ref": {"type": "string", "required": True},
            "keywords": KEYWORDS_RULE,
        }}},
        "companion": {"type": "dict", "required": False, "keys": {
            "skill": {"type": "string", "required": True},
            "description": {"type": "string", "required": True},
        }},
        "gotchas": {"type": "list", "required": True, "min_len": 1},
        "anti_patterns": ANTI_PATTERNS_RULE,
        "subdomain_config": {"type": "list", "required": False,
            "note": "legacy field; superseded by area_config portable unit. Retained for backward compatibility.",
            "items": {"keys": {
                "name": {"type": "string", "required": True},
                "state_terms": {"type": "list", "required": False},
                "operations": {"type": "list", "required": False},
                "scope_axes": {"type": "list", "required": False},
                "canonical_phrasing": {"type": "string", "required": False},
                "llm_dependent_content": {"type": "list", "required": False},
                "dependency_order": {"type": "list", "required": False},
            }},
        },
    },
    "forbidden_keys": ["rules", "counters", "facts", "patterns",
                       "apply_when", "do_not_apply_when", "techniques",
                       "index", "standards_set"],
}


AUDIT_SKILL_SCHEMA = {
    "root": "audit_skill",
    "owner_doc": "skills/skill-authoring/references/framework.md",
    "keys": {
        "_schema_version": {"type": "string", "required": False},
        "identity": IDENTITY_RULE,
        "scope": SCOPE_RULE,
        "subject": {"type": "dict", "required": True, "keys": {
            "what": {"type": "string", "required": True},
            "subject_type": {"type": "string", "required": True,
                             "note": "single-file | corpus | namespace | stream"},
        }},
        "criteria": {
            "type": "list",
            "required": True,
            "min_len": 1,
            "items": {"keys": {
                "id": {"type": "string", "required": True},
                "name": {"type": "string", "required": True},
                "keywords": KEYWORDS_RULE,
                "summary": {"type": "string", "required": True},
                "severity": {"type": "string", "required": True,
                             "note": "FAIL | INFO | JUDGMENT"},
                "detail": {"required": True},
                "gotchas": {"type": "list", "required": False, "min_len": 1},
            }},
        },
        "taxonomy": {
            "type": "list",
            "required": True,
            "min_len": 1,
            "items": {"keys": {
                "id": {"type": "string", "required": True},
                "name": {"type": "string", "required": True},
                "keywords": KEYWORDS_RULE,
                "detection_signal": {"type": "string", "required": True},
                "default_remediation": {"type": "string", "required": True},
                "bucket": {"type": "string", "required": True,
                           "note": "default disposition per id: FIX | SERIOUS | IMPROVE | SILENT | SPECIAL "
                                   "(the final per-finding disposition is assigned instance-level by the "
                                   "detect.js lane classifier against explicit predicates; value is a free "
                                   "string, not enum-constrained here)"},
                "examples": {"type": "list", "required": False, "items": {"keys": {
                    "before": {"type": "string", "required": True},
                    "after": {"type": "string", "required": True},
                }}},
            }},
        },
        "procedures": {
            "type": "list",
            "required": True,
            "min_len": 1,
            "items": {"keys": {
                "id": {"type": "string", "required": True},
                "name": {"type": "string", "required": True},
                "keywords": KEYWORDS_RULE,
                "goal": {"type": "string", "required": True},
                "preconditions": {"type": "list", "required": False},
                "steps": {"type": "list", "required": True, "min_len": 1, "items": TECHNIQUE_STEP_RULE},
                "output_template": {"type": "string", "required": False},
                "gotchas": {"type": "list", "required": True, "min_len": 1},
            }},
        },
        "remediations": {
            "type": "dict",
            "required": True,
            # Structural lanes retained for schema stability across all audit
            # members (incl. references-audit). Disposition mapping under the
            # four-disposition model: auto = FIX categories (auto-applied);
            # discuss = SERIOUS + IMPROVE + SILENT-default categories (surfaced
            # for a decision or deliberately not surfaced, per-entry noted);
            # special = K / unclassified escape hatch. Per-finding disposition
            # is instance-level (the detect.js classifier), not fixed by lane.
            "keys": {
                "auto": {"type": "list", "required": True, "items": {"keys": {
                    "category": {"type": "string", "required": True},
                    "procedure": {"type": "string", "required": True},
                    "agent_template": {"type": "string", "required": False},
                }}},
                "discuss": {"type": "list", "required": True, "items": {"keys": {
                    "category": {"type": "string", "required": True},
                    "procedure": {"type": "string", "required": True},
                }}},
                "special": {"type": "dict", "required": True, "keys": {
                    "procedure": {"type": "string", "required": True},
                }},
            },
        },
        "enforcement": {
            "type": "dict",
            "required": False,
            "keys": {
                "gate_kind": {"type": "string", "required": True,
                              "note": "audit-finding | merge-gate | ci-gate | submit-gate"},
                "gating_rule": {"type": "string", "required": True},
                "appeal_process": {"type": "string", "required": False},
            },
        },
        "gotchas": {"type": "list", "required": True, "min_len": 1},
        "anti_patterns": ANTI_PATTERNS_RULE,
    },
    "forbidden_keys": ["techniques", "rules", "patterns", "apply_when",
                       "do_not_apply_when", "facts", "index", "members",
                       "standards_set"],
}


register_schema("reference_skill", REFERENCE_SKILL_SCHEMA, role="skill_type",
                owner_doc=REFERENCE_SKILL_SCHEMA["owner_doc"])
register_schema("pattern_skill", PATTERN_SKILL_SCHEMA, role="skill_type",
                owner_doc=PATTERN_SKILL_SCHEMA["owner_doc"])
register_schema("technique_skill", TECHNIQUE_SKILL_SCHEMA, role="skill_type",
                owner_doc=TECHNIQUE_SKILL_SCHEMA["owner_doc"])
register_schema("discipline_skill", DISCIPLINE_SKILL_SCHEMA, role="skill_type",
                owner_doc=DISCIPLINE_SKILL_SCHEMA["owner_doc"])
register_schema("domain_skill", DOMAIN_SKILL_SCHEMA, role="skill_type",
                owner_doc=DOMAIN_SKILL_SCHEMA["owner_doc"])
register_schema("capability_skill", CAPABILITY_SKILL_SCHEMA, role="skill_type",
                owner_doc=CAPABILITY_SKILL_SCHEMA["owner_doc"])
register_schema("audit_skill", AUDIT_SKILL_SCHEMA, role="skill_type",
                owner_doc=AUDIT_SKILL_SCHEMA["owner_doc"])
