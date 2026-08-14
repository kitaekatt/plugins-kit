"""Tests for the engine wiring of the agent_skills_link check.

Exercises bootstrap_lib.engine._run_agent_skills_link_check directly (the
function Step 3d3 calls once per pass) rather than spawning the full engine
subprocess: it is the smallest unit that proves the engine (a) builds the
exact ok/action/fail messages, (b) routes them through
_ManifestContext.ok/action/fail with the right failure type and kwargs, (c)
respects real layered-manifest boolean precedence via
_load_layered_manifests + manifest_merge, and (d) gates a success on the
authoritative re-check. The underlying check/fix decision logic is covered
by test_agent_skills_check.py and test_agent_skills_vcs.py.
"""

import json
import os
import subprocess

import pytest

from bootstrap_lib import agent_skills_check as asc
from bootstrap_lib import engine
from bootstrap_lib.codex import CodexDetection


def _git(*args, cwd):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _init_repo(path):
    os.makedirs(path, exist_ok=True)
    _git("init", "-q", cwd=path)
    return path


def _make_source(root, name="demo"):
    skill_dir = os.path.join(root, ".claude", "skills", name)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
        f.write("---\nname: demo\ndescription: demo\n---\n")


def _run(project_dir, value):
    return engine._run_agent_skills_link_check(
        project_dir, value, "test-os", "/does/not/matter/data",
        "/does/not/matter/plugin-root",
    )


@pytest.fixture(autouse=True)
def _codex_available(monkeypatch):
    monkeypatch.setattr(
        asc, "detect_codex",
        lambda: CodexDetection(available=True, reason="fake codex 1.2.3"),
    )


class TestNoProjectDir:
    def test_none_project_dir_is_one_ok_entry(self):
        # D8: project_dir is optional; never build a path from None.
        actions, oks, failures = _run(None, None)
        assert actions == []
        assert failures == []
        assert oks == ["agent skills link: skipped (no project directory)"]


class TestExactMessagesAndOneOutcomePerRoot:
    def test_no_opt_in_field_and_no_layered_manifest_links(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        actions, oks, failures = _run(repo, None)
        assert failures == []
        assert oks == []
        assert len(actions) == 1
        label = "project:%s" % os.path.abspath(repo)
        expected_prefix = "agent skills link (%s): created .agents/skills -> .claude/skills using" % label
        assert actions[0].startswith(expected_prefix)
        assert os.path.islink(os.path.join(repo, ".agents", "skills")) or \
            os.path.isdir(os.path.join(repo, ".agents", "skills"))

    def test_opt_out_message_is_exact(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        actions, oks, failures = _run(repo, False)
        label = "project:%s" % os.path.abspath(repo)
        assert failures == []
        assert actions == []
        assert oks == ["agent skills link (%s): skipped; agent_skills_link is false" % label]

    def test_invalid_option_message_is_exact_and_is_a_failure(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        actions, oks, failures = _run(repo, "yes")
        label = "project:%s" % os.path.abspath(repo)
        assert oks == []
        assert len(failures) == 1
        assert failures[0]["type"] == "agent_skills_link"
        assert failures[0]["persist_across_sessions"] is True
        assert failures[0]["ask_reason"] == "action"
        assert actions == [
            "agent skills link (%s): failed; agent_skills_link must be a boolean" % label
        ]

    def test_codex_unavailable_message_is_exact(self, tmp_path, monkeypatch):
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)
        monkeypatch.setattr(
            asc, "detect_codex",
            lambda: CodexDetection(available=False, reason="`codex` not found on PATH"),
        )
        actions, oks, failures = _run(repo, None)
        label = "project:%s" % os.path.abspath(repo)
        assert failures == []
        assert actions == []
        assert oks == [
            "agent skills link (%s): skipped; Codex CLI unavailable: "
            "`codex` not found on PATH" % label
        ]

    def test_not_toplevel_message_names_the_toplevel(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        nested = os.path.join(repo, "sub")
        os.makedirs(nested)
        actions, oks, failures = _run(nested, None)
        label = "project:%s" % os.path.abspath(nested)
        assert failures == []
        assert actions == []
        assert len(oks) == 1
        assert oks[0].startswith(
            "agent skills link (%s): skipped; not the repository root (" % label
        )

    def test_existing_agents_message_is_exact(self, tmp_path):
        repo = _init_repo(str(tmp_path / "repo"))
        os.makedirs(os.path.join(repo, ".agents"))
        actions, oks, failures = _run(repo, None)
        label = "project:%s" % os.path.abspath(repo)
        assert failures == []
        assert actions == []
        assert oks == [
            "agent skills link (%s): skipped; .agents already exists; "
            "delete .agents to rebuild" % label
        ]


class TestReCheckGatesSuccess:
    def test_fixer_success_but_recheck_finds_agents_absent_is_a_failure(self, tmp_path, monkeypatch):
        """If .agents somehow vanished between the fixer's own verification
        and the engine's authoritative re-check, the engine must FAIL rather
        than report success -- proving the re-check, not the fixer's return
        value, controls the final outcome."""
        repo = _init_repo(str(tmp_path / "repo"))
        _make_source(repo)

        real_create = asc.create_agent_skills_link

        def _create_then_vanish(project_dir):
            result = real_create(project_dir)
            if result.ok:
                import shutil
                shutil.rmtree(os.path.join(project_dir, ".agents"))
            return result

        monkeypatch.setattr(asc, "create_agent_skills_link", _create_then_vanish)
        actions, oks, failures = _run(repo, None)

        assert oks == []
        assert len(failures) == 1
        assert failures[0]["type"] == "agent_skills_link"
        assert actions == [
            "agent skills link (project:%s): failed; .agents is absent after "
            "creation" % os.path.abspath(repo)
        ]


class TestLayeredPrecedence:
    """Real _load_layered_manifests + manifest_merge, feeding the merged
    value into the engine check -- the same path Step 3d3 exercises."""

    def _write(self, path, value):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"agent_skills_link": value}, f)

    def test_project_true_overrides_user_false(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        project = tmp_path / "proj"
        _init_repo(str(project))
        _make_source(str(project))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        self._write(home / ".claude" / "bootstrap.json", False)
        self._write(project / ".claude" / "bootstrap.json", True)

        merged, errors = engine._load_layered_manifests(str(project), None)
        assert errors == []
        assert merged.get("agent_skills_link") is True

        actions, oks, failures = _run(str(project), merged.get("agent_skills_link"))
        assert failures == []
        assert oks == []
        assert len(actions) == 1
        assert "created .agents/skills" in actions[0]

    def test_project_false_overrides_user_true(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        project = tmp_path / "proj"
        _init_repo(str(project))
        _make_source(str(project))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        self._write(home / ".claude" / "bootstrap.json", True)
        self._write(project / ".claude" / "bootstrap.json", False)

        merged, errors = engine._load_layered_manifests(str(project), None)
        assert errors == []
        assert merged.get("agent_skills_link") is False

        actions, oks, failures = _run(str(project), merged.get("agent_skills_link"))
        assert failures == []
        assert actions == []
        assert len(oks) == 1
        assert "agent_skills_link is false" in oks[0]

    def test_user_false_applies_with_no_project_layer(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        project = tmp_path / "proj"
        _init_repo(str(project))
        _make_source(str(project))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        self._write(home / ".claude" / "bootstrap.json", False)

        merged, errors = engine._load_layered_manifests(str(project), None)
        assert errors == []
        assert merged.get("agent_skills_link") is False

        actions, oks, failures = _run(str(project), merged.get("agent_skills_link"))
        assert failures == []
        assert actions == []
        assert len(oks) == 1
        assert "agent_skills_link is false" in oks[0]
