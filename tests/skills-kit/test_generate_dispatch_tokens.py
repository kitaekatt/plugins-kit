"""Pinning: every `/md-domain generate <token>` cited in a shipped skills-kit
doc names an artifact the `generate` verb actually dispatches.

md-domain/SKILL.md:569-574 is authoritative: `generate` takes `claude-md` or
`human-html` and nothing else -- "generate a skill" routes to `author`. A doc
that still shows `/md-domain generate skill` or `/md-domain generate
project-doc` teaches a dispatch that does not exist.
"""

import re
from pathlib import Path

from test_domain_members_resolve import LANE_RECORDS

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_KIT = REPO_ROOT / "plugins" / "skills-kit"

GENERATE_ARTIFACTS = {
    r["artifact"] for r in LANE_RECORDS if r.get("verb") == "generate" and r.get("artifact")
}

CITE_RE = re.compile(r"/md-domain generate ([a-z][a-z-]*)")


def _all_md_files():
    return sorted(SKILLS_KIT.rglob("*.md"))


def test_generate_dispatch_tokens_are_real_artifacts():
    bad = []
    for path in _all_md_files():
        text = path.read_text(encoding="utf-8")
        for token in CITE_RE.findall(text):
            if token not in GENERATE_ARTIFACTS:
                bad.append((str(path.relative_to(REPO_ROOT)), token))
    assert not bad, f"docs cite `/md-domain generate <token>` for a non-generate artifact: {bad}"
