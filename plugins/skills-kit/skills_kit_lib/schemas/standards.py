"""Configurable-standards schema.

A standards set is a portable typed unit that carries an authored set of
optional, tunable opinions for one file-type primitive (skill_md, claude_md,
reference_doc, plain_md). It lives as a fenced `standards_set:` YAML block in
a `*-standards.md` file inside a skills-kit config layer; the surrounding prose
documents the standards for a human reader, the block is the machine-validated
contract skills-kit's own audits consume.

Registered as a `portable` root (NOT a skill_type): a standards file is data
about a file type, not a kind of skill, so it must not trip mixed-type drift
detection. forbidden_keys names the seven skill-type roots so an authored
standards block that accidentally embeds a skill-type unit fails the audit;
each skill-type schema forbids `standards_set` in the other direction.

The owner_doc is the canonical prose spec for authoring a standards file. The
corpus audit asserts it contains a valid `standards_set:` instance.
"""

from ..rule_fragments import KEYWORDS_RULE
from ..schema_registry import register_schema


# STANDARDS_SET_SCHEMA -- one authored set of tunable opinions for a file type.
# severity and enforcement are enum-constrained via a negative-lookahead
# forbid_regex: the schema engine has no positive-enum rule, so an "is not one
# of {...}" forbid_regex is the strongest available value restriction (the
# discipline-skill hedge-word forbid_regex is the same technique). Matching is
# case-insensitive (schema_engine applies re.IGNORECASE); author values in
# lower case per the owner doc.
STANDARDS_SET_SCHEMA = {
    "root": "standards_set",
    "owner_doc": "skills/md-audit/references/authoring-standards.md",
    "keys": {
        "identity": {"type": "string", "required": True,
                     "note": "one sentence stating what this set governs and for which file type"},
        "applies_to": {"type": "string", "required": True, "min_len": 1,
                       "note": "the audit-framework.yaml file-type primitive id this set governs "
                               "(skill_md | claude_md | reference_doc | plain_md); authoritative "
                               "over the *-standards.md filename convention"},
        "criteria": {
            "type": "list",
            "required": True,
            "min_len": 1,
            "items": {"keys": {
                "id": {"type": "string", "required": True,
                       "note": "stable kebab-case identifier; the config knob and the audit-finding key"},
                "statement": {"type": "string", "required": True,
                              "note": "the standard, stated as a single checkable proposition"},
                "severity": {"type": "string", "required": True,
                             "forbid_regex": r"^(?!(?:fail|info|judgment)$).*",
                             "msg": "severity must be one of: fail, info, judgment",
                             "note": "fail = a violation blocks; info = surfaced, non-blocking; "
                                     "judgment = the agent decides per instance"},
                "keywords": KEYWORDS_RULE,
                "example": {"type": "string", "required": False,
                            "note": "optional illustrative exemplar or before/after for the statement"},
                "enforcement": {"type": "string", "required": False,
                                "forbid_regex": r"^(?!(?:mechanical|judgment)$).*",
                                "msg": "enforcement must be one of: mechanical, judgment",
                                "note": "mechanical = a registered evaluator checks it by id; "
                                        "judgment = the detect lane evaluates it from the statement. "
                                        "Default when absent: judgment"},
            }},
        },
    },
    "forbidden_keys": ["reference_skill", "pattern_skill", "technique_skill",
                       "discipline_skill", "domain_skill", "capability_skill",
                       "audit_skill"],
}


register_schema("standards_set", STANDARDS_SET_SCHEMA, role="portable",
                owner_doc=STANDARDS_SET_SCHEMA["owner_doc"])
