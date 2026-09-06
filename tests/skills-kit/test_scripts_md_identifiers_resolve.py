"""Pinning: every backticked ALL_CAPS identifier in scripts.md names a real
skills_kit_lib module attribute.

scripts.md:67 named `MIXED_THRESHOLD = 2`, a constant that does not exist --
the actual knob is the config-tunable `thresholds: {mixed_min_score: N}`,
resolved through skills_kit_lib.audit.THRESHOLDS and read by classify.py:122.
"""

import re
from pathlib import Path

import skills_kit_lib.audit as audit_mod
import skills_kit_lib.classify as classify_mod
import skills_kit_lib.rule_catalog as rule_catalog_mod
import skills_kit_lib.tag as tag_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_MD = (
    REPO_ROOT
    / "plugins"
    / "skills-kit"
    / "skills"
    / "md-domain"
    / "references"
    / "skill-domain"
    / "scripts.md"
)

MODULES = (audit_mod, classify_mod, rule_catalog_mod, tag_mod)

# Backticked ALL_CAPS identifier, optionally followed by " = <value>" (a
# constant-with-value citation).
IDENT_RE = re.compile(r"`([A-Z][A-Z0-9_]*)(?: = [^`]*)?`")


def test_all_caps_identifiers_resolve_as_module_attributes():
    text = SCRIPTS_MD.read_text(encoding="utf-8")
    bad = []
    for name in IDENT_RE.findall(text):
        if not any(hasattr(m, name) for m in MODULES):
            bad.append(name)
    assert not bad, f"scripts.md cites identifiers no skills_kit_lib module defines: {bad}"
