"""Registrations guard for llm-kit: skills, agent, workflow and README.

Checks (each is shown to fail by ``test_*_can_fail`` below, which feeds the
same detector a planted violation):

* every skill's frontmatter parses and its name matches the qualified name
  fixed by the architecture ("Skills, agents, workflows, and CLIs");
* no shipped registration text names a legacy import package, CLI, plugin
  root or qualified name, except persisted-state locations and the explicit
  provenance lines listed in ``PROVENANCE``;
* every Python or CLI invocation obeys the interpreter-variable rule;
* the workflow script parses (``node --check``; skipped when node is absent).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "llm-kit"
SHIPPED_DIRS = ("skills", "agents", "workflows")

SKILLS = (
    "openrouter-account",
    "content-pipeline-domain",
    "background-pipeline",
    "execute-work-unit",
    "workflow-pipeline",
)
AGENTS = ("pipeline-worker",)
WORKFLOWS = ("run-ready-wave",)
QUALIFIED = {f"llm-kit:{n}" for n in (*SKILLS, *AGENTS, *WORKFLOWS)}

CLI_GROUPS = ("account", "completion", "models", "jobs", "pipeline", "frontdoor", "local", "consent")


def shipped_files() -> list[Path]:
    files = [p for d in SHIPPED_DIRS for p in sorted((PLUGIN / d).rglob("*")) if p.is_file()]
    files.append(PLUGIN / "README.md")
    return files


def rel(path: Path) -> str:
    return path.relative_to(PLUGIN).as_posix()


# --- frontmatter -----------------------------------------------------------

def parse_frontmatter(text: str) -> dict:
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    assert m, "no frontmatter block"
    data = yaml.safe_load(m.group(1))
    assert isinstance(data, dict), "frontmatter is not a mapping"
    return data


def test_plugin_name_is_llm_kit() -> None:
    import json

    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "llm-kit"


@pytest.mark.parametrize("name", SKILLS)
def test_skill_frontmatter_matches_qualified_name(name: str) -> None:
    path = PLUGIN / "skills" / name / "SKILL.md"
    fm = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["name"] == name
    assert f"llm-kit:{fm['name']}" in QUALIFIED
    assert isinstance(fm.get("description"), str) and fm["description"].strip()


@pytest.mark.parametrize("name", AGENTS)
def test_agent_frontmatter_matches_qualified_name(name: str) -> None:
    fm = parse_frontmatter((PLUGIN / "agents" / f"{name}.md").read_text(encoding="utf-8"))
    assert fm["name"] == name
    assert f"llm-kit:{name}" in QUALIFIED
    assert fm["description"].strip()


def test_registration_set_is_exactly_the_fixed_names() -> None:
    skills = {p.parent.name for p in (PLUGIN / "skills").glob("*/SKILL.md")}
    agents = {p.stem for p in (PLUGIN / "agents").glob("*.md")}
    workflows = {p.stem for p in (PLUGIN / "workflows").glob("*.js")}
    found = {f"llm-kit:{n}" for n in (*skills, *agents, *workflows)}
    assert found == QUALIFIED


def test_readme_lists_every_qualified_name() -> None:
    readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
    missing = sorted(q for q in QUALIFIED if f"`{q}`" not in readme)
    assert not missing, missing


# --- legacy references -----------------------------------------------------

LEGACY = re.compile(
    r"llm[-_]scripting[-_]kit"
    r"|content[-_]pipeline[-_]kit"
    r"|\bjob[-_]kit\b"
    r"|\bcontent_pipeline\b"
    r"|\bjob_kit\b"
    r"|LLM_SCRIPTING_KIT_ROOT|CONTENT_PIPELINE_KIT_ROOT|JOB_KIT_ROOT"
    r"|openrouter-kit"
)

# Persisted state genuinely shared with the legacy plugin keeps the legacy
# locations (design decision 7): the credentials .env, the usage-verdicts.json
# pacing cache, the config.yaml layer and the frontdoor access log. These exact
# path shapes are removed from a line before the legacy scan. Bootstrap's own
# state files (deferred_requirements.json, last_validated.sha256) are NOT
# shared: the engine writes them into llm-kit's own data directory.
PERSISTED_STATE = re.compile(
    r"llm-scripting-kit/(?:\.env|usage-verdicts\.json|config\.yaml|frontdoor-access\.jsonl)"
)

# Deliberate provenance lines: (path relative to the plugin, substring of the
# one line it permits, reason). An entry that matches no line fails.
PROVENANCE: tuple[tuple[str, str, str], ...] = (
    ("README.md", "It consolidates `llm-scripting-kit`, `job-kit`",
     "provenance: names the plugins llm-kit was migrated from"),
    ("README.md", "and `content-pipeline-kit` (migrated from those plugins;",
     "provenance: continuation of the migrated-from sentence"),
    ("skills/content-pipeline-domain/references/building-a-pipeline.md",
     "`content-pipeline-kit:` extension events",
     "persisted identity: the execution event extension owner is still "
     "recorded as content-pipeline-kit (llm_kit.pipeline.execution.events.PLUGIN)"),
)


def legacy_hits(rel_path: str, text: str, provenance=PROVENANCE) -> list[tuple[int, str]]:
    hits = []
    for n, line in enumerate(text.splitlines(), start=1):
        scrubbed = PERSISTED_STATE.sub("", line)
        if not LEGACY.search(scrubbed):
            continue
        if any(rel_path == p and anchor in line for p, anchor, _ in provenance):
            continue
        hits.append((n, line.strip()[:120]))
    return hits


def test_no_legacy_references_in_shipped_text() -> None:
    bad = {}
    for path in shipped_files():
        hits = legacy_hits(rel(path), path.read_text(encoding="utf-8"))
        if hits:
            bad[rel(path)] = hits
    assert not bad, bad


def test_provenance_entries_are_not_stale() -> None:
    for rel_path, anchor, _reason in PROVENANCE:
        text = (PLUGIN / rel_path).read_text(encoding="utf-8")
        assert any(anchor in line for line in text.splitlines()), (rel_path, anchor)


def test_legacy_check_can_fail() -> None:
    planted = "Run `llm-scripting-kit status` or `import llm_scripting_kit`.\n"
    assert legacy_hits("skills/x/SKILL.md", planted)
    assert legacy_hits("skills/x/SKILL.md", "from content_pipeline.store import x\n")
    assert legacy_hits("skills/x/SKILL.md", "Skill(content-pipeline-kit:execute-work-unit)\n")
    assert legacy_hits("skills/x/SKILL.md", '"${CONTENT_PIPELINE_KIT_ROOT:?x}/a"\n')
    # the persisted-state exemption is exactly the named path shapes
    assert not legacy_hits("skills/x/SKILL.md", "`~/.x/plugins-kit/llm-scripting-kit/.env`\n")
    assert legacy_hits("skills/x/SKILL.md", "`~/.x/plugins-kit/llm-scripting-kit/venv`\n")
    # provenance is per file and per line
    assert legacy_hits("README.md", "see content-pipeline-kit\n")
    assert not legacy_hits("README.md", "It consolidates `llm-scripting-kit`, `job-kit`\n")


# --- variable surfaces and interpreter rule ---------------------------------

_NOT_BEFORE = r"(?<![A-Za-z0-9_./\\-])(?<!uv )(?<!uv\t)"
_SAFE_AFTER = r"(?=[\s\"');|&`]|$)"
BARE_PYTHON = re.compile(_NOT_BEFORE + r"(python3(?:\.\d+)?|pythonw|python\.exe|python|py)" + _SAFE_AFTER)
UV_RUN_PYTHON = re.compile(
    r"\buv\s+run\b(?:\s+--project(?:=|\s+)\S+|\s+--[A-Za-z0-9][A-Za-z0-9_-]*(?!\S))*\s+python\b"
)
FENCE_LINE = re.compile(r"^\s*`{3,}[A-Za-z0-9_+-]*\s*$")
PY_SCRIPT_CALL = re.compile(r"\.py\b")
INTERPRETER_VAR = re.compile(r"\$\{BOOTSTRAP_(?:PROJECT_)?PYTHON[:}-]")

# Illustrative consumer-supplied WorkerCommand shown between generator-owned
# markers; the same entry exists in the repo-wide guard for the legacy skill.
PYTHON_ALLOW: tuple[tuple[str, str, str], ...] = (
    ("skills/execute-work-unit/SKILL.md", 'argv=("python", "mount.py", "run")',
     "illustrative CONSUMER WorkerCommand, not an llm-kit invocation"),
    ("skills/execute-work-unit/SKILL.md", ": python mount.py run protocol",
     "generated invocation strings for that illustrative WorkerCommand"),
)


def interpreter_violations(rel_path: str, text: str, allow=PYTHON_ALLOW) -> list[tuple[int, str]]:
    out = []
    for n, line in enumerate(text.splitlines(), start=1):
        if FENCE_LINE.match(line):
            continue
        bad = bool(UV_RUN_PYTHON.search(line) or BARE_PYTHON.search(line))
        # a documented script call must go through an interpreter variable
        if not bad and PY_SCRIPT_CALL.search(line) and "scripts/" in line and not INTERPRETER_VAR.search(line):
            bad = True
        if bad and any(rel_path == p and a in line for p, a, _ in allow):
            continue
        if bad:
            out.append((n, line.strip()[:120]))
    return out


def test_interpreter_variable_rule() -> None:
    bad = {}
    for path in shipped_files():
        v = interpreter_violations(rel(path), path.read_text(encoding="utf-8"))
        if v:
            bad[rel(path)] = v
    assert not bad, bad


def test_python_allowlist_is_not_stale() -> None:
    for rel_path, anchor, _ in PYTHON_ALLOW:
        text = (PLUGIN / rel_path).read_text(encoding="utf-8")
        assert any(anchor in line for line in text.splitlines()), (rel_path, anchor)


def test_interpreter_check_can_fail() -> None:
    assert interpreter_violations("README.md", "run `python scripts/x.py`\n")
    assert interpreter_violations("README.md", "uv run python -m x\n")
    assert interpreter_violations("README.md", "python3 -m venv\n")
    assert interpreter_violations("README.md", '"$LLM_KIT_ROOT/scripts/check.py" a\n')
    assert not interpreter_violations(
        "README.md", '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${LLM_KIT_ROOT:?x}/scripts/check.py" a\n'
    )
    assert not interpreter_violations("README.md", "```python\n")


CLI_CALL = re.compile(r"(?:^|[`>]\s*|\!\s)llm-kit\s+([a-z][a-z-]*)")


def test_cli_invocations_name_a_fixed_group() -> None:
    bad = []
    for path in shipped_files():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for m in CLI_CALL.finditer(line):
                if m.group(1) not in CLI_GROUPS:
                    bad.append((rel(path), n, m.group(0)))
    assert not bad, bad


def test_cli_check_can_fail() -> None:
    assert [m.group(1) for m in CLI_CALL.finditer("`llm-kit status`")] == ["status"]
    assert [m.group(1) for m in CLI_CALL.finditer("! llm-kit account set-key")] == ["account"]


def test_plugin_root_variable_surfaces() -> None:
    """${CLAUDE_PLUGIN_ROOT}/${CLAUDE_SKILL_DIR} only in a SKILL.md body; other
    documents anchor on ${LLM_KIT_ROOT:?...}."""
    for path in shipped_files():
        text = path.read_text(encoding="utf-8")
        if path.name != "SKILL.md":
            assert "CLAUDE_PLUGIN_ROOT" not in text and "CLAUDE_SKILL_DIR" not in text, rel(path)
        for m in re.finditer(r"\$\{LLM_KIT_ROOT([^}]*)\}", text):
            assert m.group(1).startswith(":?"), (rel(path), m.group(0))


# --- workflow script -------------------------------------------------------

def wrap_for_node(source: str) -> str:
    """Wrap a native Workflow script body as an async function in an ES module.

    The runtime supplies the top-level ``return`` and the ``export const meta``
    declaration is legal only at module level, so a plain ``node --check`` of
    the file accepts or rejects the wrong things (a planted syntax error after
    the final ``return`` is not reported for the bare file). Wrapping makes the
    whole body parse as code.
    """
    marker = "export const meta"
    assert source.count(marker) == 1, f"expected exactly one {marker!r}"
    body = source.replace(marker, "const meta", 1)
    return "export async function runWave(args, agent, parallel) {\n" + body + "\n}\n"


def node_check(source: str, workdir: Path) -> subprocess.CompletedProcess:
    target = workdir / "wrapped.mjs"
    target.write_text(wrap_for_node(source), encoding="utf-8")
    return subprocess.run(
        [shutil.which("node"), "--check", str(target)], capture_output=True, text=True, timeout=60
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_workflow_script_parses(tmp_path: Path) -> None:
    source = (PLUGIN / "workflows" / "run-ready-wave.js").read_text(encoding="utf-8")
    result = node_check(source, tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_workflow_parse_check_can_fail(tmp_path: Path) -> None:
    source = (PLUGIN / "workflows" / "run-ready-wave.js").read_text(encoding="utf-8")
    assert node_check(source + "\nconst = ;\n", tmp_path).returncode != 0
