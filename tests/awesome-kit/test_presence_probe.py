"""Tests for orchestrate's presence-kit enabling probe (EN-1..EN-7).

Contract: plugins/bootstrap/skills/plugin-dev/references/enabling.md.
Every test runs against a scratch HOME; nothing reads the real ~/.claude.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

import orchestration_guidance as og
import presence_probe as pp

_REPO = Path(__file__).resolve().parents[2]
_PLUGINS = _REPO / "plugins"
_ORCHESTRATE = _PLUGINS / "awesome-kit" / "skills" / "orchestrate"
_SKILL_MD = _ORCHESTRATE / "SKILL.md"
_PROBE = _ORCHESTRATE / "scripts" / "presence_probe.py"

_PRESENCE_WITH_HEADING = (
    "# Presence\n\n## Modes\n\nbody\n\n## Waiting rule\n\n`waiting-guard`, all modes.\n"
)
_PRESENCE_WITHOUT_HEADING = "# Presence\n\n## Modes\n\nbody\n\n## While away\n\nbody\n"


@pytest.fixture(autouse=True)
def scratch_home(tmp_path, monkeypatch):
    """Point HOME at a scratch directory so no test can read the real one."""
    home = tmp_path / "home"
    (home / ".claude" / "plugins").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert Path.home() == home
    return home


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".claude").mkdir(parents=True)
    return root


def _claude(home: Path) -> Path:
    return home / ".claude"


def _install(home: Path, version: str = "0.3.0", text: Optional[str] = _PRESENCE_WITH_HEADING) -> Path:
    """A presence-kit install in the cache layout. text None omits presence.md."""
    root = _claude(home) / "plugins" / "cache" / "plugins-kit" / "presence-kit" / version
    (root / "references").mkdir(parents=True)
    if text is not None:
        (root / "references" / "presence.md").write_text(text, encoding="utf-8")
    return root


def _registry(home: Path, install: Optional[Path], version: str = "0.3.0") -> None:
    plugins = {}
    if install is not None:
        plugins["presence-kit@plugins-kit"] = [
            {"scope": "user", "installPath": str(install), "version": version}
        ]
    (_claude(home) / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": plugins}), encoding="utf-8"
    )


def _enable(home: Path) -> None:
    (_claude(home) / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"presence-kit@plugins-kit": True}}), encoding="utf-8"
    )


def _run(capfd, project, *flags) -> "tuple[str, str]":
    assert pp.main([*flags, "--project-root", str(project)]) == 0
    captured = capfd.readouterr()
    return captured.out, captured.err


# --------------------------------------------------------------------------
# EN-1 / EN-2: the consuming skill hosts the probe, and owns when and where
# --------------------------------------------------------------------------


class TestSkillHostsTheProbe:
    def test_probe_lives_in_the_orchestrate_scripts_directory(self):
        assert _PROBE.is_file()
        assert _PROBE.parent == _ORCHESTRATE / "scripts"

    def test_skill_md_states_when_it_runs_and_where_stdout_goes(self):
        text = _SKILL_MD.read_text(encoding="utf-8")
        assert "scripts/presence_probe.py" in text
        # The command goes through the interpreter variable and the skill-dir variable.
        assert re.search(
            r'"\$\{BOOTSTRAP_PYTHON:\?[^}]+\}" "\$\{CLAUDE_SKILL_DIR\}/scripts/presence_probe\.py"',
            text,
        )
        span = _probe_span(text)
        assert "once" in span and "inline" in span  # when
        assert "beside the rendered policy" in span  # where stdout goes
        assert "nothing otherwise" in span  # silence is the stated absent behaviour

    def test_orchestrate_is_complete_without_presence_kit(self):
        text = _SKILL_MD.read_text(encoding="utf-8")
        assert "complete without it" in _probe_span(text)
        plugin = json.loads(
            (_PLUGINS / "awesome-kit" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        assert "presence-kit" not in plugin["dependencies"]
        bootstrap = (_PLUGINS / "awesome-kit" / "bootstrap.json").read_text(encoding="utf-8")
        assert "presence-kit" not in bootstrap


def _probe_span(skill_text: str) -> str:
    start = skill_text.index("run the presence probe")
    return skill_text[start : skill_text.index("- n: 3", start)]


# --------------------------------------------------------------------------
# EN-3: the probe checks the heading, not only the entry
# --------------------------------------------------------------------------


class TestProbeChecksTheHeading:
    def test_entry_and_doc_without_the_heading_is_not_current_enough(self, scratch_home, project, capfd):
        install = _install(scratch_home, text=_PRESENCE_WITHOUT_HEADING)
        _registry(scratch_home, install)
        out, _ = _run(capfd, project)
        assert out == ""

    def test_the_same_install_with_the_heading_is_present(self, scratch_home, project, capfd):
        install = _install(scratch_home, text=_PRESENCE_WITH_HEADING)
        _registry(scratch_home, install)
        out, _ = _run(capfd, project)
        assert out != ""

    def test_a_heading_mentioned_in_prose_does_not_count(self, scratch_home, project, capfd):
        install = _install(scratch_home, text="# Presence\n\nSee the ## Waiting rule section.\n")
        _registry(scratch_home, install)
        out, _ = _run(capfd, project)
        assert out == ""

    def test_the_probe_names_the_frontier_heading_the_owner_ships(self):
        owner = (_PLUGINS / "presence-kit" / "references" / "presence.md").read_text(encoding="utf-8")
        assert re.search(r"^## " + re.escape(pp.FRONTIER_HEADING) + r"\s*$", owner, re.MULTILINE)
        assert pp.PRESENCE_DOC.as_posix() == "references/presence.md"


# --------------------------------------------------------------------------
# EN-4 / EN-7: the four rungs
# --------------------------------------------------------------------------


class TestFourRungs:
    def test_present_discloses_one_to_three_lines_and_the_pointer_resolves(
        self, scratch_home, project, capfd
    ):
        install = _install(scratch_home)
        _registry(scratch_home, install)
        out, err = _run(capfd, project)
        lines = [line for line in out.splitlines() if line.strip()]
        assert 1 <= len(lines) <= 3
        assert err == ""  # a quiet run writes nothing to stderr
        assert "0.3.0" in lines[0]
        pointer = next(line for line in lines if "Waiting rule" in line)
        path = Path(re.search(r": (.+?), section", pointer).group(1))
        assert path == install / "references" / "presence.md"
        assert path.is_file()
        assert "## Waiting rule" in path.read_text(encoding="utf-8")
        # Disclosure and pointer only: no procedure, no fenced example.
        assert "```" not in out

    def test_present_via_the_cache_fallback_when_the_registry_is_empty(
        self, scratch_home, project, capfd
    ):
        _install(scratch_home, "0.2.0")
        newest = _install(scratch_home, "0.10.0")
        _registry(scratch_home, None)
        _enable(scratch_home)
        out, _ = _run(capfd, project)
        assert str(newest / "references" / "presence.md") in out  # 0.10.0 beats 0.2.0

    def test_absent_is_byte_empty_and_the_diagnostic_names_absence(self, scratch_home, project, capfd):
        _registry(scratch_home, None)
        out, err = _run(capfd, project, "--explain")
        assert out == ""
        assert "absent" in err
        assert "no installed_plugins.json record" in err

    def test_absent_with_no_registry_file_at_all(self, scratch_home, project, capfd):
        out, err = _run(capfd, project, "--explain")
        assert out == ""
        assert "absent" in err

    def test_a_cached_but_not_enabled_plugin_is_absent(self, scratch_home, project, capfd):
        _install(scratch_home)
        _registry(scratch_home, None)
        out, err = _run(capfd, project, "--explain")
        assert out == ""
        assert "absent" in err

    def test_too_old_is_byte_empty_and_the_diagnostic_names_the_missing_capability(
        self, scratch_home, project, capfd
    ):
        install = _install(scratch_home, "0.1.0", text=_PRESENCE_WITHOUT_HEADING)
        _registry(scratch_home, install, "0.1.0")
        out, err = _run(capfd, project, "--explain")
        assert out == ""
        assert "too old" in err
        assert "## Waiting rule" in err
        assert "absent" not in err
        assert "no longer exists" not in err

    def test_too_old_when_the_install_has_no_presence_doc(self, scratch_home, project, capfd):
        install = _install(scratch_home, "0.1.0", text=None)
        _registry(scratch_home, install, "0.1.0")
        out, err = _run(capfd, project, "--explain")
        assert out == ""
        assert "too old" in err
        assert "references/presence.md" in err

    def test_stale_after_uninstall_is_byte_empty_and_distinct(self, scratch_home, project, capfd):
        install = _install(scratch_home)
        _registry(scratch_home, install)
        for child in sorted(install.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        install.rmdir()
        out, err = _run(capfd, project, "--explain")
        assert out == ""
        assert "stale after uninstall" in err
        assert "no longer exists" in err
        assert "too old" not in err and "absent" not in err

    def test_the_three_silent_diagnostics_are_pairwise_distinct(self, scratch_home, project):
        absent = pp.probe(_claude(scratch_home), project)
        install = _install(scratch_home, text=_PRESENCE_WITHOUT_HEADING)
        _registry(scratch_home, install)
        too_old = pp.probe(_claude(scratch_home), project)
        _registry(scratch_home, install.parent / "gone")
        stale = pp.probe(_claude(scratch_home), project)
        assert [absent.state, too_old.state, stale.state] == [pp.ABSENT, pp.TOO_OLD, pp.STALE]
        assert len({absent.detail, too_old.detail, stale.detail}) == 3

    def test_silent_states_write_nothing_without_explain(self, scratch_home, project, capfd):
        out, err = _run(capfd, project)
        assert (out, err) == ("", "")

    def test_the_script_run_as_a_program_is_byte_empty_when_absent(self, scratch_home, project):
        _registry(scratch_home, None)
        result = subprocess.run(
            [sys.executable, "-I", str(_PROBE), "--project-root", str(project)],
            capture_output=True,
            env={"HOME": str(scratch_home), "USERPROFILE": str(scratch_home)},
            check=False,
        )
        assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")

    def test_the_script_run_as_a_program_discloses_when_present(self, scratch_home, project):
        install = _install(scratch_home)
        _registry(scratch_home, install)
        result = subprocess.run(
            [sys.executable, "-I", str(_PROBE), "--project-root", str(project)],
            capture_output=True,
            env={"HOME": str(scratch_home), "USERPROFILE": str(scratch_home)},
            check=False,
        )
        assert result.returncode == 0
        assert str(install / "references" / "presence.md") in result.stdout.decode()


# --------------------------------------------------------------------------
# EN-5: the no-presence artifact makes no presence-kit claim
# --------------------------------------------------------------------------


class TestNoPresenceArtifactStaysTrue:
    def test_skill_md_mentions_presence_kit_only_inside_the_conditional_probe_step(self):
        text = _SKILL_MD.read_text(encoding="utf-8")
        span = _probe_span(text)
        assert text.count("presence-kit") == span.count("presence-kit") >= 1
        # The one mention is conditional and states the silent default.
        assert "when the sibling plugin presence-kit is installed and" in span
        assert "nothing otherwise" in span

    def test_rendered_policy_with_no_presence_kit_makes_no_claim_about_it(
        self, scratch_home, project, monkeypatch
    ):
        monkeypatch.setattr(og, "DEFAULTS_PATH", _ORCHESTRATE / "defaults" / "orchestration.yaml")
        monkeypatch.setattr(og, "user_config_path", lambda: scratch_home / "user.yaml")
        monkeypatch.setattr(og, "machine_config_path", lambda: scratch_home / "machine.yaml")
        config, provenance = og.resolve_config(project)
        text = og.render(config, provenance)
        assert "## 1. Shape the unit" in text  # the policy is complete
        assert "presence-kit" not in text.lower()
        assert "presence_probe" not in text

    def test_the_shipped_policy_source_makes_no_presence_kit_claim(self):
        for path in (_ORCHESTRATE / "defaults").rglob("*"):
            if path.is_file():
                assert "presence-kit" not in path.read_text(encoding="utf-8").lower(), path


# --------------------------------------------------------------------------
# EN-6: consent ends with the invocation -- no cache, no hook caller
# --------------------------------------------------------------------------


class TestConsentEndsWithTheInvocation:
    def test_only_the_orchestrate_skill_names_the_probe(self):
        callers = []
        for path in _PLUGINS.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".html"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if "presence_probe" in text:
                callers.append(path.relative_to(_PLUGINS).as_posix())
        assert sorted(callers) == [
            "awesome-kit/skills/orchestrate/SKILL.md",
            "awesome-kit/skills/orchestrate/scripts/presence_probe.py",
        ]

    def test_no_hook_or_startup_surface_reaches_the_probe(self):
        for path in _PLUGINS.glob("*/hooks/**/*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                assert "presence_probe" not in path.read_text(encoding="utf-8"), path
        for manifest in _PLUGINS.glob("*/bootstrap.json"):
            assert "presence_probe" not in manifest.read_text(encoding="utf-8"), manifest

    def test_the_probe_never_writes_or_caches(self):
        source = _PROBE.read_text(encoding="utf-8")
        for needle in (
            "write_text(",
            "write_bytes(",
            ".mkdir(",
            "os.environ",
            "open(",
            "pickle",
            "lru_cache",
            "functools",
            "tempfile",
            "shutil",
        ):
            assert needle not in source, needle
        # The only read of presence.md is the heading check, once per call.
        assert source.count("read_text(") == 2  # _read_json and the heading check

    def test_two_invocations_with_presence_kit_removed_between_give_a_silent_second_run(
        self, scratch_home, project, capfd
    ):
        install = _install(scratch_home)
        _registry(scratch_home, install)
        first, _ = _run(capfd, project)
        assert first != ""
        # Uninstall: the registry record goes, then the cache directory.
        _registry(scratch_home, None)
        for child in sorted(install.parent.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        install.parent.rmdir()
        second, err = _run(capfd, project)
        assert (second, err) == ("", "")

    def test_a_leftover_record_after_removal_is_also_silent_on_the_second_run(
        self, scratch_home, project, capfd
    ):
        install = _install(scratch_home)
        _registry(scratch_home, install)
        first, _ = _run(capfd, project)
        assert first != ""
        for child in sorted(install.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        install.rmdir()
        second, _ = _run(capfd, project)
        assert second == ""


# --------------------------------------------------------------------------
# Fail loudly on malformed input (not one of the documented rungs)
# --------------------------------------------------------------------------


class TestMalformedInputFailsLoudly:
    def test_an_unparsable_registry_raises(self, scratch_home, project):
        (_claude(scratch_home) / "plugins" / "installed_plugins.json").write_text("{nope", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            pp.probe(_claude(scratch_home), project)
