"""Pinning: audit-framework.yaml's `audit_kinds[].skill` field embeds a real
lane id from md-domain's dispatch table.

The field reads `'skills-kit:md-domain (lane: <token>)'`; before this test the
four audit-kind entries embedded the audit-kind's own id (skill_md_audit,
claude_md_audit, project_doc_audit, references_audit) instead of the lane
record's actual id (audit_skill, audit_claude_md, audit_project_doc,
audit_references).

Checked before this fix landed: no consumer (plugins/awesome-kit,
plugins/prototypes, tests/awesome-kit) parses the `(lane: ...)` token out of
this field -- grepped for audit_kinds, skill_md_audit, claude_md_audit,
project_doc_audit, references_audit, and `(lane:` with zero hits -- so
correcting the token is not a cross-plugin breaking change.
"""

import re
from pathlib import Path

import yaml

from test_domain_members_resolve import LANE_IDS

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_FRAMEWORK_YAML = (
    REPO_ROOT
    / "plugins"
    / "skills-kit"
    / "skills"
    / "md-domain"
    / "references"
    / "audit-framework.yaml"
)

LANE_TOKEN_RE = re.compile(r"\(lane: ([a-z_]+)\)")


def test_every_audit_kind_skill_field_names_a_real_lane_id():
    data = yaml.safe_load(AUDIT_FRAMEWORK_YAML.read_text(encoding="utf-8"))
    kinds = data["audit_kinds"]
    assert kinds, "audit_kinds[] is empty"
    bad = []
    for kind in kinds:
        skill_field = kind.get("skill", "")
        m = LANE_TOKEN_RE.search(skill_field)
        assert m, f"{kind['id']}: skill field has no `(lane: ...)` token: {skill_field!r}"
        token = m.group(1)
        if token not in LANE_IDS:
            bad.append((kind["id"], token))
    assert not bad, f"audit_kinds[].skill lane tokens absent from the dispatch table: {bad}"
