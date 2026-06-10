"""skills_kit_lib -- plugin-level Python library for the skills-kit ecosystem.

Provides:
- schema_engine: rule-DSL validator (validate, _typecheck)
- document_walker: fenced-yaml-block extraction (collect_yaml_units)
- rule_fragments: shared schema rule fragments (KEYWORDS_RULE, SCOPE_RULE, ...)
- schema_registry: registry of typed-unit roots and their schemas
- schemas/: lib-owned schema definitions (skill-type schemas, portable units, claude_md)
- markdown_heuristics: structural-shape detectors + the one frontmatter parser
- corpus: SKILL.md corpus discovery across user/project/plugin tiers
- dirwalk: depth-limited cwd-downward walk shared by the audit discover scripts
- checks: corpus-level audit checks (owner-doc validation)
- audit, classify, tag: per-skill CLI utilities

All schemas declare an owner_doc field naming the canonical prose spec; a corpus
audit asserts each owner doc contains a valid instance of its schema. This is
the bidirectional drift protection between Python schema literals and the
reference docs that prose-spec them.

Importing this package triggers registration of every lib-owned schema with
the registry, so callers that do `from skills_kit_lib.document_walker import X`
see the populated registry by the time the submodule loads.
"""

# Trigger schema registration on package import. Order matters -- schemas must
# register before any module reads SKILL_TYPE_ROOTS / SCHEMAS_BY_ROOT at module
# load time (e.g. document_walker's CONTRACT_ROOT_KEYS).
from . import schemas  # noqa: F401
