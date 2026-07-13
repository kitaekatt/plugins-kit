"""CLAUDE.md schema.

CLAUDE.md is a document type (not a skill type) -- the persistent insights
file Claude reads at session start. Schema validates the insights, conventions,
and glossary fields.
"""

from ..rule_fragments import KEYWORDS_RULE
from ..schema_registry import register_schema


CLAUDE_MD_SCHEMA = {
    "root": "claude_md",
    "owner_doc": "skills/knowledge-encoding/SKILL.md",
    "keys": {
        "_schema_version": {"type": "string", "required": False},
        "scope": {"type": "dict", "required": True, "keys": {
            "directory": {"type": "string", "required": True},
            "covers": {"type": "list", "required": True, "min_len": 1},
            "excludes": {"type": "list", "required": False},
        }},
        "insights": {"type": "list", "required": False, "min_len": 1,
                     "note": "optional as a KEY: a conventions-only CLAUDE.md is valid. The "
                             "non-empty floor is the insights/conventions UNION, enforced as "
                             "a document-level check in audit.py (check_claude_md_record_floor) "
                             "-- an empty block still fails. When present, min 1 record.",
                     "items": {"keys": {
            "id": {"type": "string", "required": True},
            "keywords": KEYWORDS_RULE,
            "summary": {"type": "string", "required": True},
            "detail": {"required": True},
            "origin": {"type": "string", "required": True},
            "added": {"type": "string", "required": True},
            "stale_when": {"type": "string", "required": False},
        }}},
        "conventions": {"type": "list", "required": False, "items": {"keys": {
            "rule": {"type": "string", "required": True},
            "keywords": KEYWORDS_RULE,
            "why": {"type": "string", "required": True},
        }}},
        "glossary": {"type": "list", "required": False, "items": {"keys": {
            "term": {"type": "string", "required": True},
            "definition": {"type": "string", "required": True},
        }}},
    },
}


register_schema("claude_md", CLAUDE_MD_SCHEMA, role="claude_md",
                owner_doc=CLAUDE_MD_SCHEMA["owner_doc"])
