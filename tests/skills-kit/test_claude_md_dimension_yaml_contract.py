"""The yaml_contract row must not FAIL a code-directory CLAUDE.md.

claude-md-standards.md 6.2: a code-directory review-notes file carries no
`claude_md:` block and must not be schema-validated. Before this, every such
file reported a yaml_contract FAIL, and acting on it -- authoring the block the
standard forbids -- is what the standards doc calls "the recurring error".
"""

from skills_kit_lib.audit import FAIL, JUDGMENT, NA, audit


CODE_DIR_BODY = """# src/cache

## The eviction path drops the lock before the callback

`evict()` releases `mutex_` at cache.cpp before invoking the callback, so a
callback that re-enters the cache does not deadlock. Do not hoist the lock.
"""

CLASSIC_BODY = """# Some project

Guidance for a directory that carries no code.
"""


def _yaml_rows(report):
    return report["yaml_contract"]


class TestCodeDirectory:
    def test_no_fail_when_siblings_are_code(self, tmp_path):
        (tmp_path / "cache.cpp").write_text("int main() { return 0; }\n")
        (tmp_path / "cache.h").write_text("#pragma once\n")
        md = tmp_path / "CLAUDE.md"
        md.write_text(CODE_DIR_BODY)

        rows = _yaml_rows(audit(md))

        assert [r["verdict"] for r in rows] == [NA]
        assert "code-directory" in rows[0]["row"]

    def test_still_fails_a_classic_file(self, tmp_path):
        (tmp_path / "notes.md").write_text("# notes\n")
        md = tmp_path / "CLAUDE.md"
        md.write_text(CLASSIC_BODY)

        rows = _yaml_rows(audit(md))

        assert [r["verdict"] for r in rows] == [FAIL]

    def test_skill_directory_stays_classic(self, tmp_path):
        # The classifier's negative guard: a CLAUDE.md beside a SKILL.md is
        # decision provenance, not review notes, so the block is still required.
        (tmp_path / "SKILL.md").write_text("---\nname: x\n---\n")
        (tmp_path / "helper.py").write_text("x = 1\n")
        md = tmp_path / "CLAUDE.md"
        md.write_text(CODE_DIR_BODY)

        rows = _yaml_rows(audit(md))

        assert [r["verdict"] for r in rows] == [FAIL]


class TestClassifierUnavailable:
    def test_unknown_dimension_is_judgment_not_fail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "skills_kit_lib.audit._claude_md_dimension", lambda _p: None)
        (tmp_path / "notes.md").write_text("# notes\n")
        md = tmp_path / "CLAUDE.md"
        md.write_text(CLASSIC_BODY)

        rows = _yaml_rows(audit(md))

        assert [r["verdict"] for r in rows] == [JUDGMENT]


class TestSignalB:
    def test_body_markers_alone_are_enough(self, tmp_path):
        # No code siblings, so Signal A is off; the file anchor in the body is
        # what classifies it. Signal B's regexes are heuristic and the likelier
        # half to drift, so pin it separately from the sibling-tally path.
        (tmp_path / "notes.md").write_text("# notes\n")
        md = tmp_path / "CLAUDE.md"
        md.write_text(
            "# some directory\n\n"
            "## The eviction path drops the lock before the callback\n\n"
            "`cache.cpp` releases the mutex before invoking the callback.\n"
        )

        rows = _yaml_rows(audit(md))

        assert [r["verdict"] for r in rows] == [NA]
