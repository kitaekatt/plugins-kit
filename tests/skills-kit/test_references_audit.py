"""Tests for references-audit's scanner script (arch-review S5, S13, S16, S17).

- S5: plugin-tier discovery goes through skills_kit_lib.corpus -- ONE
  installed_plugins.json parse feeds the pool, the skill-dir set, and the
  scan roots.
- S13: frontmatter parsing is the shared light parser.
- S16: NON_SKILL_WORDS no longer hardcodes a foreign project's vocabulary --
  formerly-masked refs (e.g. /players) are reported again; generic path
  segments (/tmp) stay excluded.
- S17: end-to-end smoke of the documented behaviors (scopes, exit codes,
  allow-stale, fence masking).
"""

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (REPO_ROOT / "plugins" / "skills-kit" / "skills" / "references-audit"
          / "scripts" / "references_audit.py")

_spec = importlib.util.spec_from_file_location("references_audit_mod", SCRIPT)
ra = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ra)


# ---------------------------------------------------------------------------
# Fixture tree builder
# ---------------------------------------------------------------------------


def _build_tree(tmp_path: Path, alpha_body: str) -> dict:
    """Project skill `alpha` (with the given body), plugin skill `beta`
    (installed via a fake installed_plugins.json), empty user dir."""
    proj = tmp_path / "proj"
    skill_dir = proj / ".claude" / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: d\n---\n" + alpha_body, encoding="utf-8"
    )

    install = tmp_path / "demo-install"
    beta = install / "skills" / "beta"
    beta.mkdir(parents=True)
    (beta / "SKILL.md").write_text(
        "---\nname: beta\ndescription: d\n---\n# Beta\n", encoding="utf-8"
    )

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "installed_plugins.json").write_text(json.dumps({
        "plugins": {"demo@mkt": [{"installPath": str(install), "version": "1.0"}]}
    }), encoding="utf-8")

    return {
        "project_dir": proj / ".claude" / "skills",
        "user_dir": tmp_path / "nouser",
        "plugins_dir": plugins_dir,
    }


def _analyze(dirs: dict, capsys, scopes=("skills",), paths=()) -> tuple[int, dict]:
    rc = ra.analyze(
        dirs["project_dir"], dirs["user_dir"], dirs["plugins_dir"],
        set(scopes), [Path(p) for p in paths], [], [],
        verbose=False, json_output=True,
    )
    payload = json.loads(capsys.readouterr().out)
    return rc, payload


def _refs(payload: dict) -> set:
    return {f["ref"] for f in payload["findings"]}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPluginTierViaCorpus:
    def test_plugin_skill_resolves_bare_and_qualified(self, tmp_path, capsys):
        dirs = _build_tree(tmp_path, "# A\nSee /beta and /demo:beta.\n")
        rc, payload = _analyze(dirs, capsys)
        assert rc == 0
        assert payload["skill_pool"]["plugin"] == 1
        assert _refs(payload) == set()

    def test_one_manifest_parse_feeds_everything(self, tmp_path, capsys, monkeypatch):
        dirs = _build_tree(tmp_path, "# A\nSee /beta.\n")
        calls = []
        real = ra.discover_corpus

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(ra, "discover_corpus", counting)
        rc, _ = _analyze(dirs, capsys, scopes=("skills", "references"))
        assert rc == 0
        assert len(calls) == 1, "installed_plugins.json must be discovered exactly once"

    def test_missing_manifest_yields_empty_plugin_tier(self, tmp_path, capsys):
        dirs = _build_tree(tmp_path, "# A\nplain\n")
        dirs["plugins_dir"] = tmp_path / "no-plugins"
        rc, payload = _analyze(dirs, capsys)
        assert rc == 0
        assert payload["skill_pool"]["plugin"] == 0


class TestNonSkillWords:
    def test_foreign_vocabulary_is_reported_again(self, tmp_path, capsys):
        dirs = _build_tree(tmp_path, "# A\nTalk to /players about /spawn-rules.\n")
        rc, payload = _analyze(dirs, capsys)
        assert rc == 0  # soft refs are warnings, not errors
        assert {"players", "spawn-rules"} <= _refs(payload)

    def test_generic_path_segments_stay_excluded(self, tmp_path, capsys):
        dirs = _build_tree(tmp_path, "# A\nFiles under /tmp and /usr and /bin.\n")
        rc, payload = _analyze(dirs, capsys)
        assert _refs(payload) == set()

    def test_list_is_generic_only(self):
        # Guard: project-specific vocabulary must not creep back in.
        foreign = {"players", "entities", "scenarios", "cheat", "spawn-rules",
                   "investigations", "health", "backend"}
        assert ra.NON_SKILL_WORDS & foreign == set()


class TestScannerBehaviors:
    def test_broken_hard_dep_exits_nonzero(self, tmp_path, capsys):
        dirs = _build_tree(tmp_path, '# A\nUse skill: "ghost-skill" here.\n')
        rc, payload = _analyze(dirs, capsys)
        assert rc == 1
        assert payload["summary"]["errors"] == 1

    def test_allow_stale_silences_listed_names_only(self, tmp_path, capsys):
        body = "# A\nLegacy /old-thing and new /new-ghost.\n"
        dirs = _build_tree(tmp_path, body)
        sk = dirs["project_dir"] / "alpha" / "SKILL.md"
        sk.write_text(
            "---\nname: alpha\ndescription: d\n"
            "references-audit-allow-stale: old-thing\n---\n" + body,
            encoding="utf-8",
        )
        rc, payload = _analyze(dirs, capsys)
        refs = _refs(payload)
        assert "old-thing" not in refs
        assert "new-ghost" in refs

    def test_fenced_and_frontmatter_refs_are_masked(self, tmp_path, capsys):
        body = "# A\n```\n/fenced-ghost --flag\n```\nNarrative /real-ghost.\n"
        dirs = _build_tree(tmp_path, body)
        rc, payload = _analyze(dirs, capsys)
        refs = _refs(payload)
        assert "fenced-ghost" not in refs
        assert "real-ghost" in refs

    def test_example_prefix_never_reported(self, tmp_path, capsys):
        dirs = _build_tree(tmp_path, "# A\nSyntax: /example:anything and /proposed:later.\n")
        rc, payload = _analyze(dirs, capsys)
        assert _refs(payload) == set()

    def test_references_scope_scans_non_skill_md(self, tmp_path, capsys):
        dirs = _build_tree(tmp_path, "# A\nplain\n")
        ref = dirs["project_dir"] / "alpha" / "references"
        ref.mkdir()
        (ref / "notes.md").write_text("# N\nSee /ref-ghost.\n", encoding="utf-8")
        rc, payload = _analyze(dirs, capsys, scopes=("references",))
        assert "ref-ghost" in _refs(payload)
        assert payload["source_files_by_kind"].get("reference") == 1


class TestSharedFrontmatterParser:
    def test_parse_frontmatter_uses_lib_light_parser(self):
        fm = ra.parse_frontmatter("---\nname: x\nskill-type: t\n---\nbody\n")
        assert fm == {"name": "x", "skill-type": "t"}

    def test_no_frontmatter_is_empty_dict(self):
        assert ra.parse_frontmatter("# no fm\n") == {}
