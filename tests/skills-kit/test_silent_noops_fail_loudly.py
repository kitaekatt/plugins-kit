"""Inputs that used to be accepted and then ignored now fail loudly.

Each class pins one former silent no-op:

- H5: standards resolution without pyyaml returned empty defaults plus a note,
  and resolve_standards.py exited 0 -- every config and standards file was
  ignored while the output read like "nothing configured". resolve() now raises
  StandardsUnavailableError; the CLI and `audit --config` exit non-zero.
- H4: `resolve_standards.py --primitive code_directory` always answered `[]`,
  because authored code_directory sets are rejected at load. A --primitive no
  lane consumes, or an unknown one, is now a usage error.
- H3: the per-file detect lanes read an absent `disabledCriteria` as "nothing
  disabled". It is now a required input. (coverage-detect.js is pinned in
  test_coverage_batching.py; the remediate lanes' `fixMode`, H2, in
  test_audit_autoedit_seam.py.)
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from skills_kit_lib import audit as audit_mod
from skills_kit_lib import standards_resolve as sr

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "skills-kit"
SCRIPT = PLUGIN / "scripts" / "resolve_standards.py"
WORKFLOW = PLUGIN / "skills" / "md-domain" / "workflow"

NODE = shutil.which("node")


def _cli(args, tmp_path, block_yaml=False):
    """Run resolve_standards.py in a child, optionally with pyyaml unimportable."""
    env = {
        "CLAUDE_CONFIG_DIR": str(tmp_path / "config"),
        "SYSTEMROOT": str(Path(sys.executable).anchor),
        "PATH": "",
        # The plugin venv links bootstrap_lib (lane_models needs it); the test
        # interpreter does not. Never re-exec into the installed venv.
        "PYTHONPATH": str(REPO_ROOT / "plugins" / "bootstrap"),
        "_BOOTSTRAP_GUARD_VENV_REEXEC": "1",
    }
    argv = [str(SCRIPT), *args]
    code = (
        "import runpy, sys\n"
        + ("sys.modules['yaml'] = None\n" if block_yaml else "")
        + f"sys.argv = {argv!r}\n"
        + f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, timeout=60,
    )


class TestNoPyyamlFailsLoudly:
    def test_resolve_raises_instead_of_returning_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sr, "HAVE_YAML", False)
        with pytest.raises(sr.StandardsUnavailableError) as exc:
            sr.resolve(tmp_path)
        msg = str(exc.value)
        assert "pyyaml" in msg
        assert "skills-kit/.venv" in msg

    def test_the_unavailable_error_is_a_config_error(self):
        # Every caller that already stops on a malformed layer stops here too.
        assert issubclass(sr.StandardsUnavailableError, sr.StandardsConfigError)

    def test_cli_exits_nonzero_and_names_the_dependency(self, tmp_path):
        result = _cli(["--project-root", str(tmp_path)], tmp_path, block_yaml=True)
        assert result.returncode == 1, result.stdout + result.stderr
        assert result.stdout == ""
        assert "pyyaml" in result.stderr
        assert "skills-kit/.venv" in result.stderr

    def test_cli_still_succeeds_with_pyyaml(self, tmp_path):
        # The blocking shim is what makes the test above fail, not the harness.
        result = _cli(["--project-root", str(tmp_path)], tmp_path)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["notes"] == []

    def test_audit_config_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        skill = tmp_path / "demo" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("---\nname: demo\ndescription: Use when x.\n---\n\n# Demo\n",
                         encoding="utf-8")
        monkeypatch.setattr(sr, "HAVE_YAML", False)
        assert audit_mod.main([str(skill), "--config"]) == 1
        assert "pyyaml" in capsys.readouterr().err

    @staticmethod
    def _load_emit():
        import importlib.util

        path = PLUGIN / "skills" / "md-domain" / "scripts" / "emit_audit_jobs.py"
        spec = importlib.util.spec_from_file_location("_emit_noops", path)
        emit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(emit)
        return emit

    def test_emit_audit_jobs_empty_admission_when_nothing_is_ignored(
        self, tmp_path, monkeypatch
    ):
        """No layer file on disk: nothing configured is being ignored, so the
        empty admission is correct and silent."""
        emit = self._load_emit()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setattr(sr, "HAVE_YAML", False)
        assert emit.resolve_admitted_endpoints(tmp_path / "proj") == frozenset()

    @pytest.mark.parametrize(
        "layer, name",
        [
            ("project", "config.yaml"),
            ("project", "config.local.yaml"),
            ("project", "claude-md-standards.md"),
            ("user", "config.yaml"),
            ("user", "config.local.yaml"),
        ],
    )
    def test_emit_audit_jobs_fails_loudly_when_a_layer_file_would_be_ignored(
        self, tmp_path, monkeypatch, layer, name
    ):
        emit = self._load_emit()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        proj = tmp_path / "proj"
        base = (
            proj / ".claude" / "skills-kit"
            if layer == "project"
            else tmp_path / "config" / "skills-kit"
        )
        base.mkdir(parents=True)
        (base / name).write_text("x: 1\n", encoding="utf-8")
        monkeypatch.setattr(sr, "HAVE_YAML", False)
        with pytest.raises(sr.StandardsUnavailableError) as exc:
            emit.resolve_admitted_endpoints(proj)
        assert "pyyaml" in str(exc.value)
        assert "skills-kit/.venv" in str(exc.value)

    def test_emit_audit_jobs_main_exits_nonzero_with_a_layer_file(
        self, tmp_path, monkeypatch, capsys
    ):
        emit = self._load_emit()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        subject = tmp_path / "proj" / "docs"
        subject.mkdir(parents=True)
        (subject / "a.md").write_text("# A\n\ntext\n", encoding="utf-8")
        layer = tmp_path / "proj" / ".claude" / "skills-kit"
        layer.mkdir(parents=True)
        (layer / "config.yaml").write_text("x: 1\n", encoding="utf-8")
        monkeypatch.setattr(sr, "HAVE_YAML", False)
        rc = emit.main([str(subject), "--repo-root", str(tmp_path / "proj")])
        assert rc == 5
        assert "pyyaml" in capsys.readouterr().err

    def test_layer_paths_is_pure_and_matches_resolve(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        config_files, standards_dirs = sr.layer_paths(tmp_path / "proj")
        assert [p.name for p in config_files] == [
            "config.yaml", "config.local.yaml", "config.yaml", "config.local.yaml",
        ]
        assert len(standards_dirs) == 2
        assert not (tmp_path / "config").exists()
        assert sr.existing_layer_files(tmp_path / "proj") == []


class TestUnconsumedPrimitiveIsAUsageError:
    @pytest.mark.parametrize(
        "name", sorted(sr.APPLIES_TO_NOT_CONSUMED) + sorted(sr.APPLIES_TO_ALIASES)
    )
    def test_a_subject_no_lane_consumes_is_refused(self, tmp_path, name):
        result = _cli(["--project-root", str(tmp_path), "--primitive", name], tmp_path)
        assert result.returncode == 2, result.stdout + result.stderr
        assert result.stdout == ""
        assert "no lane consumes authored standards" in result.stderr
        assert sr.APPLIES_TO_ALIASES.get(name, name) in result.stderr

    def test_an_unknown_subject_is_refused(self, tmp_path):
        result = _cli(["--project-root", str(tmp_path), "--primitive", "skil_md"], tmp_path)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "unknown subject" in result.stderr

    def test_a_consumed_primitive_with_nothing_authored_is_an_empty_list(self, tmp_path):
        result = _cli(["--project-root", str(tmp_path), "--primitive", "skill_md"], tmp_path)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["standards"] == {"skill_md": []}


HARNESS = r"""
const fs = require('fs')
const [scriptPath, argsJson] = process.argv.slice(2)
const src = fs.readFileSync(scriptPath, 'utf8').replace(/^export const /gm, 'const ')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
let calls = 0
const run = new AsyncFunction('args', 'agent', 'parallel', 'phase', 'log', src)
run(
  JSON.parse(argsJson),
  async () => { calls++; return { files: [] } },
  (thunks) => Promise.all(thunks.map((t) => t())),
  () => {},
  () => {},
).then(() => {
  console.log(JSON.stringify({ calls }))
}).catch((e) => { console.log(JSON.stringify({ calls, error: String(e) })) })
"""


def _run_detect(tmp_path, lane, args):
    harness = tmp_path / "harness.cjs"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(harness), str(WORKFLOW / f"{lane}-detect.js"), json.dumps(args)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node is required to execute the workflow lane")
class TestDetectLanesRequireDisabledCriteria:
    LANES = ["claude-md", "skill", "project-doc"]

    @pytest.mark.parametrize("lane", LANES)
    @pytest.mark.parametrize("value", ["absent", None, "off", [1]])
    def test_absent_or_malformed_list_throws_before_dispatch(self, tmp_path, lane, value):
        args = {"files": [{"path": str(tmp_path / "CLAUDE.md")}]}
        if value != "absent":
            args["disabledCriteria"] = value
        out = _run_detect(tmp_path, lane, args)
        assert out["calls"] == 0, out
        assert "requires args.disabledCriteria" in out.get("error", ""), out
        assert "resolve_standards.py" in out["error"], out

    @pytest.mark.parametrize("lane", LANES)
    def test_an_empty_list_passes_the_guard(self, tmp_path, lane):
        args = {"files": [{"path": str(tmp_path / "CLAUDE.md")}], "disabledCriteria": []}
        out = _run_detect(tmp_path, lane, args)
        assert "requires args.disabledCriteria" not in out.get("error", ""), out
