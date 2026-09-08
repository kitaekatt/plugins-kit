"""Tests for md-domain/scripts/discover_claude_md.py role classification.

Two behaviours under test:

1. Root anchoring: the cwd CLAUDE.md is `root` only when no CLAUDE.md ancestor
   exists above it (within the project). An ancestor demotes it to `child` so
   the project-root-only hygiene checks (H1/H2/H3) do not false-positive on a
   subordinate file. A personal CLAUDE.local.md ancestor is not a project-root
   marker and does NOT demote it.

2. Project boundary: the upward walk stops at the project root (nearest .git
   ancestor) and never looks outside it. A CLAUDE.md above the project root is
   ignored, so it cannot demote the project root or be reported as an ancestor.

Each multi-level test plants a .git marker at the intended project root so the
boundary is deterministic regardless of the real filesystem above tmp_path.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import discover_claude_md as discover
import discover_skill


def _write(path: Path, text: str = "# CLAUDE.md\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _mkgit(directory: Path) -> None:
    (directory / ".git").mkdir(parents=True, exist_ok=True)


def _role_of(results, path: Path) -> str:
    for p, role in results:
        if p == path:
            return role
    raise AssertionError(f"{path} not found in discover() results: {results}")


def _paths(results) -> set:
    return {p for p, _ in results}


class TestCwdRootClassification:
    def test_cwd_claude_md_is_root_when_no_ancestor(self, tmp_path):
        proj = tmp_path / "proj"
        _mkgit(proj)
        cwd_md = proj / "CLAUDE.md"
        _write(cwd_md)
        results = discover.discover(proj)
        assert _role_of(results, cwd_md) == "root"

    def test_cwd_claude_md_is_child_when_ancestor_exists(self, tmp_path):
        _mkgit(tmp_path)  # project root
        _write(tmp_path / "CLAUDE.md")
        sub = tmp_path / "services" / "payments"
        cwd_md = sub / "CLAUDE.md"
        _write(cwd_md)
        results = discover.discover(sub)
        assert _role_of(results, cwd_md) == "child"

    def test_ancestor_claude_md_is_classified_ancestor(self, tmp_path):
        _mkgit(tmp_path)  # project root
        anc_md = tmp_path / "CLAUDE.md"
        _write(anc_md)
        sub = tmp_path / "services"
        _write(sub / "CLAUDE.md")
        results = discover.discover(sub)
        assert _role_of(results, anc_md) == "ancestor"

    def test_local_md_ancestor_does_not_demote_cwd(self, tmp_path):
        # A personal CLAUDE.local.md above cwd is not a project-root marker.
        _mkgit(tmp_path)  # project root
        _write(tmp_path / "CLAUDE.local.md")
        sub = tmp_path / "work"
        cwd_md = sub / "CLAUDE.md"
        _write(cwd_md)
        results = discover.discover(sub)
        assert _role_of(results, cwd_md) == "root"


class TestProjectBoundary:
    def test_find_project_root_returns_nearest_git_dir(self, tmp_path):
        _mkgit(tmp_path / "proj")
        deep = tmp_path / "proj" / "a" / "b"
        deep.mkdir(parents=True, exist_ok=True)
        assert discover.find_project_root(deep) == tmp_path / "proj"

    def test_find_project_root_is_git_only(self, tmp_path):
        """The claude-md audit lane's boundary is the git repo, deliberately.

        discover_coverage moved to the VCS-agnostic walk in project_root.py; this
        resolver must NOT follow it, or the audit lane's ancestor scope changes.
        """
        proj = tmp_path / "p4proj"
        (proj / "a").mkdir(parents=True, exist_ok=True)
        (proj / ".p4config.txt").write_text("P4CLIENT=x\n", encoding="utf-8")
        assert discover.find_project_root(proj / "a") is None

    def test_ancestor_above_project_root_is_excluded(self, tmp_path):
        # CLAUDE.md above the project root must not be scanned.
        outside_md = tmp_path / "CLAUDE.md"
        _write(outside_md)
        proj = tmp_path / "proj"
        _mkgit(proj)
        _write(proj / "CLAUDE.md")
        sub = proj / "sub"
        cwd_md = sub / "CLAUDE.md"
        _write(cwd_md)
        results = discover.discover(sub)
        assert outside_md not in _paths(results)
        assert _role_of(results, proj / "CLAUDE.md") == "ancestor"
        assert _role_of(results, cwd_md) == "child"

    def test_project_root_not_demoted_by_outside_ancestor(self, tmp_path):
        # Launching AT the project root: a CLAUDE.md above it must not demote it.
        _write(tmp_path / "CLAUDE.md")  # outside the project
        proj = tmp_path / "proj"
        _mkgit(proj)
        root_md = proj / "CLAUDE.md"
        _write(root_md)
        results = discover.discover(proj)
        assert _role_of(results, root_md) == "root"
        assert tmp_path / "CLAUDE.md" not in _paths(results)

    def test_no_ancestors_when_cwd_is_project_root(self, tmp_path):
        proj = tmp_path / "proj"
        _mkgit(proj)
        _write(proj / "CLAUDE.md")
        assert discover.collect_ancestors(proj) == []


class TestCollectAtCwd:
    def test_default_flag_is_root(self, tmp_path):
        # Backward-compatible default: no flag -> root.
        _write(tmp_path / "CLAUDE.md")
        out = discover.collect_at_cwd(tmp_path)
        assert "root" in [role for _, role in out]

    def test_flag_true_yields_child_not_root(self, tmp_path):
        _write(tmp_path / "CLAUDE.md")
        out = discover.collect_at_cwd(tmp_path, has_ancestor_root=True)
        roles = [role for _, role in out]
        assert "child" in roles and "root" not in roles

    def test_local_at_cwd_unaffected_by_flag(self, tmp_path):
        # CLAUDE.local.md at cwd stays `local` regardless of the ancestor flag.
        _write(tmp_path / "CLAUDE.local.md")
        out = discover.collect_at_cwd(tmp_path, has_ancestor_root=True)
        assert "local" in [role for _, role in out]


class TestSkippedDirsReporting:
    """A CLAUDE.md sitting under a noise-named directory (tmp/, Build/) is
    silently invisible to the default walk, and nothing said so. --include-dir
    (or MD_DOMAIN_INCLUDE_DIRS) opts a name back in; skipped_dirs in the JSON
    output reports every noise-named directory that was pruned instead."""

    SCRIPT = (
        Path(__file__).resolve().parents[2] / "plugins" / "skills-kit" / "skills"
        / "md-domain" / "scripts" / "discover_claude_md.py"
    )

    def _run(self, cwd: Path, *extra_args, env=None):
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), "--json", "--cwd", str(cwd), *extra_args],
            capture_output=True, text=True, timeout=60, env=full_env,
        )

    def test_claude_md_under_noise_dir_is_absent_by_default_and_reported_skipped(
        self, tmp_path
    ):
        """The JSON output stays a flat LIST (unchanged for the common case of
        no skips); a skipped directory is a distinct record shape within it
        (a "skipped_dir" key, no "path"/"role") rather than a new envelope."""
        _mkgit(tmp_path)
        buried = tmp_path / "tmp" / "Build" / "CLAUDE.md"
        _write(buried)

        result = self._run(tmp_path)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert not any(rec.get("path") == str(buried) for rec in payload)
        skip_records = [rec for rec in payload if "skipped_dir" in rec]
        assert "tmp" in {rec["skipped_dir"] for rec in skip_records}
        for rec in skip_records:
            assert rec["reason"] == "noise-name"

    def test_include_dir_flag_makes_it_discoverable(self, tmp_path):
        _mkgit(tmp_path)
        buried = tmp_path / "tmp" / "Build" / "CLAUDE.md"
        _write(buried)

        result = self._run(tmp_path, "--include-dir", "tmp", "--include-dir", "Build")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert any(rec.get("path") == str(buried) for rec in payload)

    def test_env_var_fallback_makes_it_discoverable(self, tmp_path):
        _mkgit(tmp_path)
        buried = tmp_path / "tmp" / "Build" / "CLAUDE.md"
        _write(buried)

        result = self._run(
            tmp_path, env={"MD_DOMAIN_INCLUDE_DIRS": "tmp" + os.pathsep + "Build"}
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert any(rec.get("path") == str(buried) for rec in payload)

    def test_no_skips_produces_no_skip_records(self, tmp_path):
        """The compatibility case: nothing pruned, nothing appended -- the
        output is byte-for-byte what it was before skipped_dirs existed. No
        .git marker here deliberately: `.git` is itself a pruned noise name,
        which would give this "nothing pruned" case something pruned."""
        _write(tmp_path / "CLAUDE.md")

        result = self._run(tmp_path)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert all("skipped_dir" not in rec for rec in payload)


class TestSkillDiscoverSkippedDirs:
    """discover_skill.py gets the same treatment as discover_claude_md.py:
    --include-dir / MD_DOMAIN_INCLUDE_DIRS opt a noise-named directory back
    in, and every pruned directory is reported rather than dropped."""

    SCRIPT = (
        Path(__file__).resolve().parents[2] / "plugins" / "skills-kit" / "skills"
        / "md-domain" / "scripts" / "discover_skill.py"
    )

    def _run(self, cwd: Path, *extra_args, env=None):
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), "--json", "--cwd", str(cwd), *extra_args],
            capture_output=True, text=True, timeout=60, env=full_env,
        )

    def test_skill_under_noise_dir_is_absent_by_default_and_reported_skipped(self, tmp_path):
        buried = tmp_path / "tmp" / "Build" / "SKILL.md"
        buried.parent.mkdir(parents=True)
        buried.write_text("---\nname: buried\n---\nbody\n", encoding="utf-8")

        result = self._run(tmp_path)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert not any(rec.get("path") == str(buried) for rec in payload)
        skip_records = [rec for rec in payload if "skipped_dir" in rec]
        assert "tmp" in {rec["skipped_dir"] for rec in skip_records}
        for rec in skip_records:
            assert rec["reason"] == "noise-name"

    def test_include_dir_flag_makes_it_discoverable(self, tmp_path):
        buried = tmp_path / "tmp" / "Build" / "SKILL.md"
        buried.parent.mkdir(parents=True)
        buried.write_text("---\nname: buried\n---\nbody\n", encoding="utf-8")

        result = self._run(tmp_path, "--include-dir", "tmp", "--include-dir", "Build")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert any(rec.get("path") == str(buried) for rec in payload)

    def test_env_var_fallback_makes_it_discoverable(self, tmp_path):
        buried = tmp_path / "tmp" / "Build" / "SKILL.md"
        buried.parent.mkdir(parents=True)
        buried.write_text("---\nname: buried\n---\nbody\n", encoding="utf-8")

        result = self._run(
            tmp_path, env={"MD_DOMAIN_INCLUDE_DIRS": "tmp" + os.pathsep + "Build"}
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert any(rec.get("path") == str(buried) for rec in payload)


class TestCollectSkillMdReferencesSafety:
    """collect_skill_md(..., include_references=True) used to enumerate a
    skill's references/*.md via plain rglob -- unbounded depth, no VCS-ignore
    query, and (pathlib's default) FOLLOWS directory symlinks. It now goes
    through the same bounded, no-symlink-follow walk plus vcs_ignore
    filtering the sibling discover scripts use."""

    def test_vcs_ignored_reference_is_excluded(self, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                        capture_output=True, text=True)
        skill = tmp_path / "skills" / "demo"
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: demo\n---\nbody\n", encoding="utf-8")
        (skill / "references" / "keep.md").write_text("keep\n", encoding="utf-8")
        (skill / "references" / "scratch.md").write_text("scratch\n", encoding="utf-8")
        (tmp_path / ".gitignore").write_text("scratch.md\n", encoding="utf-8")

        results = discover_skill.collect_skill_md(tmp_path, include_references=True)
        names = {p.name for p, _, _, kind in results if kind == "skill_reference"}

        assert "keep.md" in names
        assert "scratch.md" not in names

    def test_symlinked_references_dir_pointing_outside_root_is_not_followed(self, tmp_path):
        outside = tmp_path / "outside"
        (outside / "leaked").mkdir(parents=True)
        (outside / "leaked" / "secret.md").write_text("secret\n", encoding="utf-8")

        skill = tmp_path / "project" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: demo\n---\nbody\n", encoding="utf-8")
        try:
            (skill / "references").symlink_to(outside / "leaked", target_is_directory=True)
        except (OSError, NotImplementedError):
            import pytest
            pytest.skip("symlinks not supported on this platform/permission set")

        results = discover_skill.collect_skill_md(
            tmp_path / "project", include_references=True
        )
        names = {p.name for p, _, _, kind in results if kind == "skill_reference"}

        assert "secret.md" not in names


class TestClassifyDimension:
    """classify_dimension Level-1 trigger: Signal A (code siblings), Signal B
    (review-claim markers), and the narrowed gotcha rule -- bare "do not" /
    "don't" / "never" prose flips a file only when the same line anchors the
    claim to code (inline-code span, line anchor, or source-file name)."""

    def test_bare_gotcha_prose_alone_is_classic(self, tmp_path):
        # The over-fire case the narrowing fixes: opinionated policy prose
        # with no code anchor in a docs-only directory.
        md = tmp_path / "CLAUDE.md"
        _write(md, "# Conventions\n\nNever use the memory system.\nDo not overthink placement.\n")
        assert discover.classify_dimension(md) == "classic"

    def test_gotcha_with_inline_code_span_is_code_directory(self, tmp_path):
        md = tmp_path / "CLAUDE.md"
        _write(md, "# Notes\n\nNever call `frobnicate()` before init.\n")
        assert discover.classify_dimension(md) == "code-directory"

    def test_gotcha_with_source_file_name_is_code_directory(self, tmp_path):
        md = tmp_path / "CLAUDE.md"
        _write(md, "# Notes\n\nDo not edit generated_bindings.cpp by hand.\n")
        assert discover.classify_dimension(md) == "code-directory"

    def test_gotcha_and_code_anchor_on_different_lines_is_classic(self, tmp_path):
        # The anchor must be on the SAME line as the gotcha phrasing.
        md = tmp_path / "CLAUDE.md"
        _write(md, "# Notes\n\nNever skip the checklist.\n\nSee the config file.\n")
        assert discover.classify_dimension(md) == "classic"

    def test_review_checks_heading_is_code_directory(self, tmp_path):
        md = tmp_path / "CLAUDE.md"
        _write(md, "## Review Checks\n\n- check the thing\n")
        assert discover.classify_dimension(md) == "code-directory"

    def test_code_siblings_signal_a_is_code_directory(self, tmp_path):
        md = tmp_path / "CLAUDE.md"
        _write(md, "# Plain notes with no markers.\n")
        _write(tmp_path / "main.py", "print('x')\n")
        assert discover.classify_dimension(md) == "code-directory"

    def test_schema_block_guard_forces_classic(self, tmp_path):
        # A declared claude_md: contract block wins over Signal B markers.
        md = tmp_path / "CLAUDE.md"
        _write(md, "# Notes\n\nNever call `frobnicate()`.\n\n```yaml\nclaude_md:\n  scope: {}\n```\n")
        assert discover.classify_dimension(md) == "classic"

    def test_skill_dir_guard_forces_classic(self, tmp_path):
        md = tmp_path / "CLAUDE.md"
        _write(md, "Never call `frobnicate()`.\n")
        _write(tmp_path / "SKILL.md", "---\nname: x\n---\n")
        assert discover.classify_dimension(md) == "classic"


class TestReferencesWalkResolvesRoot:
    """The within-root guard compares resolved against resolved: a cwd with a
    symlink component (macOS /tmp -> /private/tmp) must not drop every
    skill's references/ as if they were symlinks out of the root."""

    def test_symlinked_cwd_keeps_references(self, tmp_path):
        import os
        from discover_skill import collect_skill_md
        real = tmp_path / "real"
        (real / "skills" / "s" / "references").mkdir(parents=True)
        (real / "skills" / "s" / "SKILL.md").write_text(
            "---\nname: s\ndescription: d\n---\n", encoding="utf-8")
        (real / "skills" / "s" / "references" / "r.md").write_text("# r\n", encoding="utf-8")
        link = tmp_path / "link"
        os.symlink(real, link, target_is_directory=True)
        kinds = [(p.name, kind) for p, _, _, kind in collect_skill_md(link, include_references=True)]
        assert ("r.md", "skill_reference") in kinds


class TestHasSchemaBlockRegexIsExportedPublicly:
    """evidence_pack.py's own has-contract-block check used to diverge from
    discover_claude_md's -- two regexes for one fact. The regex is exported
    under a public name so evidence_pack can import and reuse the SAME
    compiled pattern rather than maintaining a second one."""

    def test_public_name_exists(self):
        assert hasattr(discover, "HAS_SCHEMA_BLOCK")

    def test_it_matches_a_claude_md_block(self):
        assert discover.HAS_SCHEMA_BLOCK.search("claude_md:\n  scope: {}\n")

    def test_it_does_not_match_prose_mentioning_the_words(self):
        assert not discover.HAS_SCHEMA_BLOCK.search("See the claude_md file for details.\n")
