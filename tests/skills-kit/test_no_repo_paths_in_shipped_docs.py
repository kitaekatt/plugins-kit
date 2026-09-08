"""OP-1 pinning: shipped skills-kit docs must not name this repo's own
checkout layout.

A path like `plugins/skills-kit/...`, `tests/skills-kit/...`,
`docs/planning/...`, or `docs/reference/...` resolves only inside the
plugins-kit dev checkout. On a consumer's machine the plugin is installed
under a version-keyed cache directory, so a shipped doc that names one of
these paths as an authority points nowhere real -- the checkable-authority
requirement (`${CLAUDE_PLUGIN_ROOT}/...` resolves everywhere; a repo-relative
path does not).

CLAUDE.md files are excluded: they are maintainer insight files and cite
tests/docs deliberately (plugins-kit/CLAUDE.md, "Published-plugin boundaries").
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_KIT = REPO_ROOT / "plugins" / "skills-kit"

FORBIDDEN = re.compile(r"tests/skills-kit|docs/planning/|docs/reference/|plugins/skills-kit/")


def _shipped_md_files():
    files = sorted(SKILLS_KIT.rglob("*.md"))
    return [f for f in files if f.name != "CLAUDE.md"]


def test_no_repo_relative_paths_in_shipped_docs():
    bad = []
    for path in _shipped_md_files():
        text = path.read_text(encoding="utf-8")
        for m in FORBIDDEN.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            bad.append((str(path.relative_to(REPO_ROOT)), line_no, m.group(0)))
    assert not bad, f"shipped docs name a repo-relative maintainer path: {bad}"


def test_no_concept_capture_doc_ships_under_plugin_docs():
    """A maintainer-only planning doc (e.g. repo-independent-standards.md,
    "Status: concept capture") must not live under plugins/skills-kit/docs/ --
    that directory is copied into a consumer's plugin cache. Such content
    belongs in docs/planning/skills-kit/ at the repo root.
    """
    plugin_docs = SKILLS_KIT / "docs"
    if not plugin_docs.exists():
        return
    concept_capture = [
        f for f in plugin_docs.rglob("*.md") if "Status: concept capture" in f.read_text(encoding="utf-8")
    ]
    assert not concept_capture, f"concept-capture doc(s) shipped under plugin docs/: {concept_capture}"
