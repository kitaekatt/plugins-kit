"""Pinning: md-domain/SKILL.md's `index.references[].summary` text names only
lane ids that exist in the dispatch table.

The index block is prose written by hand alongside the `lanes:` dispatch table;
nothing keeps the two in sync mechanically except this test. A summary that
names a lane id the dispatch table does not carry (e.g. a router-era name like
`generate_skill` that the fold renamed to `author_skill`) sends a reader to a
lane that does not exist.
"""

import re
from pathlib import Path

import yaml

from test_domain_members_resolve import LANE_IDS, MD_DOMAIN

LANE_TOKEN_RE = re.compile(r"\b(?:audit|author|generate|analyze)_[a-z_]+\b")


def _domain_skill_block() -> dict:
    text = (MD_DOMAIN / "SKILL.md").read_text(encoding="utf-8")
    for block in re.findall(r"```yaml\n(.*?)\n```", text, re.S):
        if block.lstrip().startswith("domain_skill:"):
            return yaml.safe_load(block)["domain_skill"]
    raise AssertionError("md-domain/SKILL.md has no fenced `domain_skill:` YAML block")


def test_index_summary_lane_tokens_are_real_lane_ids():
    block = _domain_skill_block()
    refs = block["index"]["references"]
    assert refs, "index.references[] is empty"
    bad = []
    for ref in refs:
        summary = ref.get("summary", "")
        for token in LANE_TOKEN_RE.findall(summary):
            if token not in LANE_IDS:
                bad.append((ref["id"], token))
    assert not bad, f"index summaries name lane ids absent from the dispatch table: {bad}"
