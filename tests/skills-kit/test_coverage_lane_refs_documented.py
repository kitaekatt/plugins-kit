"""Pinning: every `input.refs.<key>` / `refs.<key>` that coverage-detect.js
actually dereferences is documented in coverage-lane.md.

coverage-lane.md's Step 3 told the caller to pass only `refs.criteria`, but
coverage-detect.js:737 also reads `refs.observationKinds` (with a placeholder
fallback when it is omitted). `refs.placement` appears in the workflow's
header comment but is never read anywhere in the file -- it is documentation
of a ref that does not exist, not a real input.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MD_DOMAIN = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
COVERAGE_LANE_MD = MD_DOMAIN / "references" / "lanes" / "coverage-lane.md"
COVERAGE_DETECT_JS = MD_DOMAIN / "workflow" / "coverage-detect.js"

# A dereference: `input.refs.<key>` or a bare `refs.<key>` used as a value
# (excludes the header-comment declaration line, which uses `refs: { ... }`).
DEREF_RE = re.compile(r"\brefs\.([a-zA-Z_]+)")


def _dereferenced_keys() -> set[str]:
    text = COVERAGE_DETECT_JS.read_text(encoding="utf-8")
    keys = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        keys.update(DEREF_RE.findall(line))
    return keys


def test_every_dereferenced_ref_key_is_documented_in_coverage_lane_md():
    lane_text = COVERAGE_LANE_MD.read_text(encoding="utf-8")
    dereferenced = _dereferenced_keys()
    assert dereferenced, "coverage-detect.js dereferences no refs.<key> -- sanity check failed"
    missing = [key for key in dereferenced if f"refs.{key}" not in lane_text]
    assert not missing, f"coverage-lane.md does not document refs keys actually read: {missing}"


def test_placement_is_not_documented_as_a_real_ref_in_the_workflow_header():
    text = COVERAGE_DETECT_JS.read_text(encoding="utf-8")
    assert "placement" not in _dereferenced_keys(), (
        "coverage-detect.js dereferences refs.placement, so it must stay documented"
    )
    # The header comment must not advertise an input the workflow never reads.
    header = text.split("PRECEDENCE:", 1)[0]
    assert "placement:" not in header
