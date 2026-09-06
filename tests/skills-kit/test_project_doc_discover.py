"""Tests for the project-doc audit lane's scripts/discover_project_doc.py.

discover_project_doc.py enumerates standalone PROJECT DOCUMENTS (plain_md
outside any skill references/ folder and outside the CLAUDE.md hierarchy) and
computes the mechanical signals the audit lanes consume: kind classification,
size, and the inbound-citation count that powers orphan detection.

Loaded via importlib under a unique module name because the md-domain scripts
directory also ships discover_skill.py and discover_claude_md.py; a bare
`import discover_project_doc` would collide with pytest's module cache across
the sibling discover_* test files.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISCOVER_PATH = (
    REPO_ROOT / "plugins" / "skills-kit" / "skills"
    / "md-domain" / "scripts" / "discover_project_doc.py"
)

_spec = importlib.util.spec_from_file_location("pd_discover", DISCOVER_PATH)
pd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pd)


def _write(path: Path, text: str = "# doc\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestIsDocFile:
    def test_markdown_txt_markdeep_are_docs(self):
        assert pd.is_doc_file("overview.md")
        assert pd.is_doc_file("notes.txt")
        assert pd.is_doc_file("QuestSystem.md.html")
        assert pd.is_doc_file("api.rst")

    def test_code_and_config_are_not_docs(self):
        assert not pd.is_doc_file("script.py")
        assert not pd.is_doc_file("config.yaml")
        assert not pd.is_doc_file("data.json")


class TestClassifyKind:
    def test_project_doc_at_project_path(self, tmp_path):
        p = tmp_path / "Docs" / "Overview.md"
        _write(p)
        assert pd.classify_kind(p) == "project_doc"

    def test_skill_reference_inside_references_folder(self, tmp_path):
        p = tmp_path / "plugins" / "x" / "skills" / "y" / "references" / "deep.md"
        _write(p)
        assert pd.classify_kind(p) == "skill_reference"

    def test_skill_md_and_claude_md_are_other_artifacts(self, tmp_path):
        assert pd.classify_kind(tmp_path / "skills" / "y" / "SKILL.md") == "other_claude_artifact"
        assert pd.classify_kind(tmp_path / "CLAUDE.md") == "other_claude_artifact"
        assert pd.classify_kind(tmp_path / "sub" / "CLAUDE.local.md") == "other_claude_artifact"


class TestCollectCandidates:
    def test_finds_project_docs_and_skips_vendored(self, tmp_path):
        _write(tmp_path / "Docs" / "a.md")
        _write(tmp_path / ".claude" / "docs" / "b.md")
        _write(tmp_path / "node_modules" / "pkg" / "README.md")  # vendored -> skipped
        _write(tmp_path / "src" / "main.py")  # not a doc
        found = {p.name for p in pd.collect_candidates(tmp_path)}
        assert "a.md" in found
        assert "b.md" in found
        assert "README.md" not in found  # node_modules excluded
        assert "main.py" not in found

    def test_skips_generated_and_build_dirs(self, tmp_path):
        _write(tmp_path / "Intermediate" / "gen.md")
        _write(tmp_path / "Docs" / "real.md")
        found = {p.name for p in pd.collect_candidates(tmp_path)}
        assert found == {"real.md"}

    def test_skips_gitignored_candidates_in_a_git_repo(self, tmp_path):
        import subprocess

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        _write(tmp_path / ".gitignore", "*.egg-info/\n")
        _write(tmp_path / "pkg.egg-info" / "SOURCES.txt", "src/a.py\n")
        _write(tmp_path / "Docs" / "real.md")
        found = {p.name for p in pd.collect_candidates(tmp_path)}
        assert "real.md" in found
        assert "SOURCES.txt" not in found

    def test_non_git_root_keeps_all_candidates(self, tmp_path):
        # No .git anywhere under tmp_path: the ignore filter must be a no-op,
        # not an error, and the egg-info doc is (still) enumerated.
        _write(tmp_path / "pkg.egg-info" / "SOURCES.txt", "src/a.py\n")
        _write(tmp_path / "Docs" / "real.md")
        found = {p.name for p in pd.collect_candidates(tmp_path)}
        assert found == {"real.md", "SOURCES.txt"}

    def test_force_added_tracked_doc_is_still_treated_as_ignored(self, tmp_path):
        """The question is whether the project's ignore RULES cover the path,
        not whether git happens to track it. Plain `git check-ignore` (no
        --no-index) consults the index first and reports a tracked path as NOT
        ignored -- the opposite of what this discovery step needs to know. A
        doc force-added despite matching a .gitignore rule must still be
        excluded from the candidate list."""
        import subprocess

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
        _write(tmp_path / ".gitignore", "forced/\n")
        _write(tmp_path / "forced" / "kept.md")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "forced/kept.md"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-qm", "force-add an ignored doc"], check=True
        )
        _write(tmp_path / "Docs" / "real.md")

        found = {p.name for p in pd.collect_candidates(tmp_path)}

        assert "real.md" in found
        assert "kept.md" not in found

    def test_skips_p4_ignored_candidates(self, tmp_path, monkeypatch):
        """Mirrors the p4-detection fixture pattern in
        tests/skills-kit/test_coverage_discover.py: a .p4config marker makes
        the tree look like a Perforce workspace, and the p4 subprocess call is
        faked to report one path ignored."""
        _write(tmp_path / ".p4config", "P4CLIENT=ws\n")
        _write(tmp_path / "Docs" / "real.md")
        _write(tmp_path / "Docs" / "ignored.md")

        real_run = pd.vcs_ignore.subprocess.run

        def _fake_run(cmd, **kwargs):
            if cmd[:1] != ["p4"]:
                return real_run(cmd, **kwargs)

            class _Result:
                returncode = 0
                stdout = f"{tmp_path / 'Docs' / 'ignored.md'} ignored\n"
                stderr = ""

            return _Result()

        monkeypatch.setattr(pd.vcs_ignore.subprocess, "run", _fake_run)

        found = {p.name for p in pd.collect_candidates(tmp_path)}

        assert "real.md" in found
        assert "ignored.md" not in found


class TestOrphanDetection:
    def test_uncited_doc_is_orphan(self, tmp_path):
        orphan = tmp_path / "Docs" / "lonely.md"
        _write(orphan)
        cands = pd.collect_candidates(tmp_path)
        inbound = pd.build_inbound_index(tmp_path, cands)
        rec = pd.describe(orphan, inbound, tmp_path)
        assert rec["inbound_citations"] == 0
        assert rec["kind"] == "project_doc"

    def test_cited_doc_is_not_orphan(self, tmp_path):
        cited = tmp_path / "Docs" / "useful.md"
        _write(cited)
        # A CLAUDE.md that points at the doc by basename.
        _write(tmp_path / "CLAUDE.md", "See Docs/useful.md when working on X.\n")
        cands = pd.collect_candidates(tmp_path)
        inbound = pd.build_inbound_index(tmp_path, cands)
        rec = pd.describe(cited, inbound, tmp_path)
        assert rec["inbound_citations"] >= 1
        assert any("CLAUDE.md" in c for c in rec["cited_by"])

    def test_self_mention_does_not_count_as_citation(self, tmp_path):
        # A doc that mentions its own basename must not cite itself.
        doc = tmp_path / "Docs" / "selfref.md"
        _write(doc, "This file selfref.md documents itself.\n")
        cands = pd.collect_candidates(tmp_path)
        inbound = pd.build_inbound_index(tmp_path, cands)
        rec = pd.describe(doc, inbound, tmp_path)
        assert rec["inbound_citations"] == 0


class TestCitationResolution:
    """Basename-collision handling: two candidates sharing a basename (e.g. two
    README.md files in different directories) must not falsely credit each
    other, and a path-qualified mention must resolve to the specific candidate
    it names."""

    def test_path_qualified_mention_credits_only_the_named_file(self, tmp_path):
        api_readme = tmp_path / "api" / "README.md"
        other_readme = tmp_path / "other" / "README.md"
        _write(api_readme)
        _write(other_readme)
        _write(tmp_path / "citer.md", "See api/README.md for the API surface.\n")

        cands = pd.collect_candidates(tmp_path)
        inbound = pd.build_inbound_index(tmp_path, cands)

        api_rec = pd.describe(api_readme, inbound, tmp_path)
        other_rec = pd.describe(other_readme, inbound, tmp_path)

        assert other_rec["inbound_citations"] == 0
        assert api_rec["inbound_citations"] >= 1

    def test_bare_basename_mention_with_two_candidates_credits_neither(self, tmp_path):
        api_readme = tmp_path / "api" / "README.md"
        other_readme = tmp_path / "other" / "README.md"
        _write(api_readme)
        _write(other_readme)
        _write(tmp_path / "citer.md", "See README.md for details.\n")

        cands = pd.collect_candidates(tmp_path)
        inbound = pd.build_inbound_index(tmp_path, cands)

        api_rec = pd.describe(api_readme, inbound, tmp_path)
        other_rec = pd.describe(other_readme, inbound, tmp_path)

        assert api_rec["inbound_citations"] == 0
        assert other_rec["inbound_citations"] == 0


class TestWalkAndScanEfficiency:
    """collect_candidates and build_inbound_index used to walk the same tree
    twice (once each) and, per citer file, run one substring search per
    candidate basename -- O(candidates x citers). Both are perf-shape guards,
    asserted by call count rather than wall-clock time."""

    def test_candidate_and_citer_scans_share_one_tree_walk(self, tmp_path, monkeypatch):
        _write(tmp_path / "Docs" / "a.md")
        _write(tmp_path / "Docs" / "b.md", "cites a.md\n")

        import os as os_module

        calls = []
        real_walk = os_module.walk

        def _counting_walk(top, *args, **kwargs):
            calls.append(str(top))
            return real_walk(top, *args, **kwargs)

        monkeypatch.setattr(os_module, "walk", _counting_walk)

        candidates = pd.collect_candidates(tmp_path)
        pd.build_inbound_index(tmp_path, candidates)

        # One walk of this root serves both the candidate scan and the citer
        # scan -- not one walk per consumer.
        assert calls.count(str(tmp_path)) == 1

    def test_citer_body_reads_scale_linearly_not_quadratically(self, tmp_path, monkeypatch):
        """With C candidates and K citers, a quadratic implementation still
        reads each citer body once (I/O), but the OLD implementation searched
        for every one of the C basenames inside each read body -- exercised
        here by asserting the read count itself stays exactly K regardless of
        C, and by asserting the module's own citation-mention regex is
        compiled once per index build rather than once per citer."""
        candidate_count = 200
        citer_count = 200
        for i in range(candidate_count):
            _write(tmp_path / "Docs" / f"doc{i}.md")
        # .yaml is a citer extension (_CITER_EXT) but not a doc extension
        # (DOC_EXT), so these contribute reads without inflating the
        # candidate count.
        for i in range(citer_count):
            _write(tmp_path / "notes" / f"citer{i}.yaml", f"mentions doc{i % candidate_count}.md\n")

        candidates = pd.collect_candidates(tmp_path)
        assert len(candidates) == candidate_count

        read_calls = {"n": 0}
        real_read_text = Path.read_text

        def _counting_read_text(self, *args, **kwargs):
            read_calls["n"] += 1
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _counting_read_text)

        compile_calls = {"n": 0}
        real_compile = pd._compile_mention_regex

        def _counting_compile(basenames):
            compile_calls["n"] += 1
            return real_compile(basenames)

        monkeypatch.setattr(pd, "_compile_mention_regex", _counting_compile)

        pd.build_inbound_index(tmp_path, candidates)

        # Exactly one read per citer-eligible file -- the .md candidates are
        # themselves valid citer sources (.md is in both DOC_EXT and
        # _CITER_EXT), so the total is candidates + citers, never their
        # product.
        assert read_calls["n"] == candidate_count + citer_count
        # The alternation is built once for the whole index, not once per citer.
        assert compile_calls["n"] == 1


class TestMeasure:
    def test_counts_effective_lines_and_tokens(self, tmp_path):
        doc = tmp_path / "Docs" / "sized.md"
        _write(doc, "line one\nline two\n\n\n")  # trailing blanks dropped
        lines, approx_tokens = pd._measure(doc)
        assert lines == 2
        assert approx_tokens > 0


class TestProjectRootCiterScope:
    """The orphan scan must cover the whole project even when auditing a
    subdirectory -- otherwise a .claude/docs doc cited from the root CLAUDE.md
    would false-positive as an orphan. find_project_root scopes the citer scan."""

    def test_find_project_root_finds_git_ancestor(self, tmp_path):
        (tmp_path / ".git").mkdir()
        deep = tmp_path / ".claude" / "docs"
        deep.mkdir(parents=True)
        assert pd.find_project_root(deep) == tmp_path

    def test_find_project_root_finds_perforce_marker(self, tmp_path):
        # Non-git project: Perforce workspace marker must also resolve.
        (tmp_path / ".p4config.txt").write_text("P4USER=x\n", encoding="utf-8")
        deep = tmp_path / "MyGame" / "Source"
        deep.mkdir(parents=True)
        assert pd.find_project_root(deep) == tmp_path

    def test_find_project_root_none_without_marker(self, tmp_path):
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        assert pd.find_project_root(deep) is None

    def test_orphan_scan_from_project_root_sees_distant_citer(self, tmp_path):
        # Doc in .claude/docs, cited only from a CLAUDE.md at the project root.
        (tmp_path / ".git").mkdir()
        doc = tmp_path / ".claude" / "docs" / "guide.md"
        _write(doc)
        _write(tmp_path / "CLAUDE.md", "For details see .claude/docs/guide.md\n")
        # Candidate is just the doc; citer scan rooted at the project root finds
        # the distant citer, so the doc is NOT flagged as an orphan.
        inbound = pd.build_inbound_index(tmp_path, [doc])
        rec = pd.describe(doc, inbound, tmp_path)
        assert rec["inbound_citations"] >= 1


class TestCiterRootFallback:
    """--citer-root falls back to the project root of the candidates, then to
    an explicit --root, then to cwd -- matching the CLI help text. The bug:
    the fallback chain skipped --root entirely and went straight to cwd, so a
    marker-less --root scanned from a different process cwd made every doc
    report inbound_citations == 0 (spuriously orphaned)."""

    def test_explicit_root_used_when_candidates_have_no_project_markers(
        self, tmp_path, monkeypatch, capsys
    ):
        target = tmp_path / "target"
        _write(target / "Docs" / "candidate.md")
        _write(target / "CLAUDE.md", "See Docs/candidate.md for details.\n")

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        monkeypatch.setattr(
            sys, "argv",
            ["discover_project_doc.py", "--root", str(target), "--json", "--skip-plugin-cache"],
        )
        rc = pd.main()
        assert rc == 0

        records = json.loads(capsys.readouterr().out)
        rec = next(r for r in records if r["path"].endswith("candidate.md"))
        assert rec["inbound_citations"] > 0


class TestGeneratedArtifactSignals:
    """machine_emitted_artifact role detection (stress-test gap 6): a sidecar
    generation record or an in-file marker identifies a committed generated
    output, which the audit then checks for provenance ONLY."""

    def test_params_sidecar_detected(self, tmp_path):
        doc = tmp_path / "renderings" / "top50-approaches.md"
        _write(doc, "# Top 50\n\ncontent\n")
        _write(doc.with_name("top50-approaches.params.json"), '{"query": "top50"}\n')
        rec = pd.generation_record(doc)
        assert rec == "sidecar:top50-approaches.params.json"

    def test_markdeep_compound_suffix_sidecar_detected(self, tmp_path):
        doc = tmp_path / "Docs" / "report.md.html"
        _write(doc, "<meta>\n")
        _write(doc.with_name("report.params.json"), "{}\n")
        assert pd.generation_record(doc) == "sidecar:report.params.json"

    def test_in_file_marker_detected(self, tmp_path):
        doc = tmp_path / "docs" / "analysis.md"
        _write(doc, "# Analysis\n\nThis document is generated analysis, exempt.\n")
        rec = pd.generation_record(doc)
        assert rec is not None and rec.startswith("marker:")

    def test_marker_beyond_head_is_ignored(self, tmp_path):
        doc = tmp_path / "docs" / "handwritten.md"
        _write(doc, "# Doc\n" + "\n" * 30 + "mentions generated by tooling later\n")
        assert pd.generation_record(doc) is None

    def test_plain_doc_has_no_signal(self, tmp_path):
        doc = tmp_path / "docs" / "notes.md"
        _write(doc, "# Notes\n\nHand-written notes.\n")
        assert pd.generation_record(doc) is None

    def test_describe_carries_generated_fields(self, tmp_path):
        (tmp_path / ".git").mkdir()
        doc = tmp_path / "renderings" / "out.md"
        _write(doc, "# Out\n")
        _write(doc.with_name("out.params.json"), "{}\n")
        inbound = pd.build_inbound_index(tmp_path, [doc])
        rec = pd.describe(doc, inbound, tmp_path)
        assert rec["generated"] is True
        assert rec["generation_record"] == "sidecar:out.params.json"


class TestReadmeRoleHint:
    """readme_md role detection (stress-test gap 2): READMEs are judged as the
    human-facing derived brief, not as generic project docs."""

    def test_readme_md_flagged(self, tmp_path):
        doc = tmp_path / "README.md"
        _write(doc)
        assert pd.role_hint(doc) == "readme"

    def test_readme_case_insensitive(self, tmp_path):
        assert pd.role_hint(tmp_path / "readme.md") == "readme"
        assert pd.role_hint(tmp_path / "Readme.txt") == "readme"

    def test_other_docs_have_no_hint(self, tmp_path):
        assert pd.role_hint(tmp_path / "guide.md") is None
        assert pd.role_hint(tmp_path / "readme-first-draft-notes.md") is None

    def test_describe_carries_role_hint(self, tmp_path):
        (tmp_path / ".git").mkdir()
        doc = tmp_path / "README.md"
        _write(doc, "# Project\n")
        inbound = pd.build_inbound_index(tmp_path, [doc])
        rec = pd.describe(doc, inbound, tmp_path)
        assert rec["role_hint"] == "readme"


class TestPluginCacheCiterScanning:
    """A repo doc referenced only by an INSTALLED plugin-cache skill is not an
    orphan. The inbound scan must index plugin-cache skills (SKILL.md +
    references) from <config>/plugins/cache, which live outside the project tree.
    """

    def _make_cache_skill(self, config_dir: Path, marketplace: str, plugin: str,
                          version: str, body: str) -> Path:
        skill = (config_dir / "plugins" / "cache" / marketplace / plugin
                 / version / "skills" / plugin / "SKILL.md")
        _write(skill, body)
        return skill

    def test_doc_cited_only_by_plugin_cache_skill_is_not_orphan(self, tmp_path):
        project = tmp_path / "project"
        (project / ".git").mkdir(parents=True)
        doc = project / ".claude" / "docs" / "cozy-ui-architecture.md"
        _write(doc, "# Cozy UI architecture\n")

        config_dir = tmp_path / "cfg"
        self._make_cache_skill(
            config_dir, "private-plugins", "prototype-ui", "0.3.0",
            "---\nname: prototype-ui\n---\nSee .claude/docs/cozy-ui-architecture.md for the layout.\n",
        )

        # Baseline: project-tree-only scan reports the doc as an orphan.
        base = pd.build_inbound_index(project, [doc])
        assert pd.describe(doc, base, project)["inbound_citations"] == 0

        # With plugin-cache citers indexed, it is no longer an orphan.
        extra = list(pd.plugin_cache_citer_files(config_dir, project_root=project))
        assert extra, "expected the plugin-cache SKILL.md to be discovered"
        inbound = pd.build_inbound_index(project, [doc], extra_citer_files=extra)
        rec = pd.describe(doc, inbound, project)
        assert rec["inbound_citations"] >= 1

    def test_reference_doc_in_plugin_cache_counts_as_citer(self, tmp_path):
        project = tmp_path / "p"
        (project / ".git").mkdir(parents=True)
        doc = project / "Docs" / "arch.md"
        _write(doc, "# arch\n")
        config_dir = tmp_path / "cfg"
        ref = (config_dir / "plugins" / "cache" / "mkt" / "plug" / "1.0.0"
               / "skills" / "plug" / "references" / "deep.md")
        _write(ref, "Detailed notes; cross-links Docs/arch.md in the repo.\n")
        extra = list(pd.plugin_cache_citer_files(config_dir, project_root=project))
        inbound = pd.build_inbound_index(project, [doc], extra_citer_files=extra)
        assert pd.describe(doc, inbound, project)["inbound_citations"] >= 1

    def test_highest_version_dir_is_chosen(self, tmp_path):
        config_dir = tmp_path / "cfg"
        self._make_cache_skill(config_dir, "m", "plug", "0.1.0", "old\n")
        self._make_cache_skill(config_dir, "m", "plug", "0.2.0", "cites target.md\n")
        files = list(pd.plugin_cache_citer_files(config_dir, project_root=None))
        # Only the highest version dir's skill is scanned.
        assert files
        assert all("0.2.0" in str(f) for f in files)
        assert not any("0.1.0" in str(f) for f in files)

    def test_missing_cache_root_yields_nothing(self, tmp_path):
        # No plugins/cache under the config dir -> empty, no error.
        assert list(pd.plugin_cache_citer_files(tmp_path / "cfg", project_root=None)) == []

    def test_enabled_filter_selects_named_plugin(self, tmp_path):
        project = tmp_path / "p"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "settings.json").write_text(
            '{"enabledPlugins": {"prototype-ui@private-plugins": true}}', encoding="utf-8")
        config_dir = tmp_path / "cfg"
        self._make_cache_skill(config_dir, "private-plugins", "prototype-ui", "1.0.0", "a\n")
        self._make_cache_skill(config_dir, "other-mkt", "unrelated", "1.0.0", "b\n")
        files = list(pd.plugin_cache_citer_files(config_dir, project_root=project))
        assert any("prototype-ui" in str(f) for f in files)
        assert not any("unrelated" in str(f) for f in files)

    def test_unresolvable_enabled_set_includes_all_plugins(self, tmp_path):
        # No settings anywhere -> enabled set empty -> fall back to every plugin.
        config_dir = tmp_path / "cfg"
        self._make_cache_skill(config_dir, "m", "one", "1.0.0", "a\n")
        self._make_cache_skill(config_dir, "m", "two", "1.0.0", "b\n")
        files = list(pd.plugin_cache_citer_files(config_dir, project_root=None))
        assert any("one" in str(f) for f in files)
        assert any("two" in str(f) for f in files)

    def test_read_enabled_parses_list_and_nested_forms(self, tmp_path):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / "settings.json").write_text(
            '{"enabledPlugins": ["a@mkt", "b@mkt"]}', encoding="utf-8")
        assert pd._read_enabled_plugin_names(cfg, None) == {"a", "b"}
        (cfg / "settings.json").write_text(
            '{"enabledPlugins": {"mkt": {"c": true, "d": false}}}', encoding="utf-8")
        assert pd._read_enabled_plugin_names(cfg, None) == {"c"}


class TestMentionPrefixIsSegmented:
    """A basename that is the SUFFIX of another basename ("notes.md" inside
    "release-notes.md") must not be credited by a mention of the longer name:
    the optional path prefix is whole segments ending in a separator, never a
    bare run of name characters."""

    def test_suffix_basename_is_not_credited(self):
        from discover_project_doc import _compile_mention_regex
        rx = _compile_mention_regex({"notes.md", "release-notes.md"})
        m = rx.search("see release-notes.md for details")
        assert m is not None
        assert m.group(2) == "release-notes.md"
        assert m.group(1) == ""
        m2 = rx.search("see docs/release-notes.md")
        assert m2.group(1) == "docs/" and m2.group(2) == "release-notes.md"
        assert rx.search("see notes.md.bak") is None
        assert rx.search("see docs/notes.md.").group(2) == "notes.md"  # sentence period


class TestSkippedDirsReporting:
    """discover_project_doc.py gets the same --include-dir / MD_DOMAIN_INCLUDE_DIRS
    / skipped_dirs treatment as discover_claude_md.py and discover_skill.py
    (slice 2): a doc under a noise-named directory (tmp/, Build/) is silently
    invisible to the default walk, and nothing said so."""

    def _run(self, cwd: Path, *extra_args, env=None):
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        return subprocess.run(
            [sys.executable, str(DISCOVER_PATH), "--json", "--root", str(cwd), *extra_args],
            capture_output=True, text=True, timeout=60, env=full_env,
        )

    def test_doc_under_noise_dir_is_absent_by_default_and_reported_skipped(self, tmp_path):
        buried = tmp_path / "tmp" / "Build" / "notes.md"
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
        buried = tmp_path / "tmp" / "Build" / "notes.md"
        _write(buried)

        result = self._run(tmp_path, "--include-dir", "tmp", "--include-dir", "Build")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert any(rec.get("path") == str(buried) for rec in payload)

    def test_env_var_fallback_makes_it_discoverable(self, tmp_path):
        buried = tmp_path / "tmp" / "Build" / "notes.md"
        _write(buried)

        result = self._run(
            tmp_path, env={"MD_DOMAIN_INCLUDE_DIRS": "tmp" + os.pathsep + "Build"}
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert any(rec.get("path") == str(buried) for rec in payload)

    def test_no_skips_produces_no_skip_records(self, tmp_path):
        _write(tmp_path / "Docs" / "a.md")

        result = self._run(tmp_path)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        assert all("skipped_dir" not in rec for rec in payload)


class TestIncludeDirsOverride:
    """In-process coverage of _resolve_include_dirs -- the CLI flag wins over
    the environment fallback, matching discover_claude_md.py's contract."""

    def test_cli_values_win_over_env(self, monkeypatch):
        monkeypatch.setenv(pd.INCLUDE_DIRS_ENV, "fromenv")
        assert pd._resolve_include_dirs(["fromcli"]) == ["fromcli"]

    def test_env_used_when_cli_absent(self, monkeypatch):
        monkeypatch.setenv(pd.INCLUDE_DIRS_ENV, "a" + os.pathsep + "b")
        assert pd._resolve_include_dirs(None) == ["a", "b"]

    def test_neither_present_is_empty(self, monkeypatch):
        monkeypatch.delenv(pd.INCLUDE_DIRS_ENV, raising=False)
        assert pd._resolve_include_dirs(None) == []
