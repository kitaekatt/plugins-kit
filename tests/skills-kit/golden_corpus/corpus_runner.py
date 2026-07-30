"""Golden-corpus runner: stage fixtures, run the mechanical audit surfaces,
normalize their output to machine-independent JSON.

Shared by record.py (writes expected/) and test_golden_corpus.py (compares
live output to expected/). The corpus covers the DETERMINISTIC layer only --
audit.py reports, the two discover.py scripts, references_audit.py. The LLM
detect/remediate lanes consume the same fixtures but are exercised out of
band (see README.md).

Staging: fixtures are copied to a temp dir and `.git` marker FILES are
planted per case so upward project-root walks stop at the fixture boundary
instead of escaping into this repo. Markers cannot be committed (git refuses
to track `.git`), which is why staging exists at all.

Normalization: every occurrence of the staged root (either slash flavor) in
any string becomes "<CORPUS>", then backslashes become forward slashes. The
result is byte-identical across machines and operating systems.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parent
FIXTURES = CORPUS_DIR / "fixtures"
EXPECTED = CORPUS_DIR / "expected"
REPO_ROOT = CORPUS_DIR.parents[2]
PLUGIN = REPO_ROOT / "plugins" / "skills-kit"

CLAUDE_MD_DISCOVER = PLUGIN / "skills" / "md-domain" / "scripts" / "discover_claude_md.py"
PROJECT_DOC_DISCOVER = PLUGIN / "skills" / "md-domain" / "scripts" / "discover_project_doc.py"
REFERENCES_AUDIT = PLUGIN / "skills" / "md-domain" / "scripts" / "references_audit.py"

if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

from skills_kit_lib import audit as audit_mod  # noqa: E402


def stage(dest: Path) -> Path:
    """Copy fixtures into dest and plant .git boundary markers; return root."""
    staged = dest / "fixtures"
    shutil.copytree(FIXTURES, staged)
    for case_dir in (staged / "claude-md").iterdir():
        if case_dir.is_dir():
            (case_dir / ".git").write_text("golden-corpus boundary marker\n", encoding="utf-8")
    (staged / "project-doc" / ".git").write_text("golden-corpus boundary marker\n", encoding="utf-8")
    return staged


def normalize(obj, staged_root: Path):
    """Recursively replace the staged root with <CORPUS> and unify slashes."""
    fwd = str(staged_root).replace("\\", "/")
    back = str(staged_root).replace("/", "\\")
    if isinstance(obj, dict):
        return {k: normalize(v, staged_root) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize(v, staged_root) for v in obj]
    if isinstance(obj, str):
        out = obj.replace(back, "<CORPUS>").replace(fwd, "<CORPUS>")
        return out.replace("\\", "/")
    return obj


def _run_json(argv: list[str], cwd: Path) -> object:
    proc = subprocess.run(
        [sys.executable, *argv], cwd=str(cwd), capture_output=True, text=True
    )
    if not proc.stdout.strip():
        raise RuntimeError(
            f"no JSON on stdout from {argv}: rc={proc.returncode} stderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def _skill_case(staged: Path, case: str) -> dict:
    report = audit_mod.audit(staged / "skill" / case / "SKILL.md")
    return {"audit": report}


def _claude_md_case(staged: Path, case: str) -> dict:
    case_dir = staged / "claude-md" / case
    discovered = _run_json(
        [str(CLAUDE_MD_DISCOVER), "--json", "--cwd", str(case_dir)], cwd=case_dir
    )
    report = audit_mod.audit(case_dir / "CLAUDE.md")
    return {"discover": discovered, "audit": report}


def _project_doc_case(staged: Path, case: str) -> dict:
    root = staged / "project-doc"
    discovered = _run_json(
        [
            str(PROJECT_DOC_DISCOVER), "--json",
            "--root", str(root),
            "--citer-root", str(root),
            "--skip-plugin-cache",
        ],
        cwd=root,
    )
    return {"discover": discovered}


def _references_case(staged: Path, case: str) -> dict:
    empty = staged / "references" / "empty"
    findings = _run_json(
        [
            str(REFERENCES_AUDIT), "--json",
            "--project-dir", str(staged / "references" / ".claude" / "skills"),
            "--user-dir", str(empty),
            "--plugins-dir", str(empty),
            "--scope", "skills,references",
        ],
        cwd=staged,
    )
    return {"references_audit": findings}


CASES: dict[str, object] = {
    "skill-s1-valid-reference": lambda s: _skill_case(s, "s1-valid-reference"),
    "skill-s2-technique-missing-steps": lambda s: _skill_case(s, "s2-technique-missing-steps"),
    "skill-s3-mixed-type": lambda s: _skill_case(s, "s3-mixed-type"),
    "skill-s4-unreachable-reference": lambda s: _skill_case(s, "s4-unreachable-reference"),
    "skill-s5-legacy-prose": lambda s: _skill_case(s, "s5-legacy-prose"),
    "claude-md-c1-classic-schema": lambda s: _claude_md_case(s, "c1-classic-schema"),
    "claude-md-c2-code-directory": lambda s: _claude_md_case(s, "c2-code-directory"),
    "claude-md-c3-gotcha-prose": lambda s: _claude_md_case(s, "c3-gotcha-prose"),
    "claude-md-c4-conventions-only": lambda s: _claude_md_case(s, "c4-conventions-only"),
    "claude-md-c5-empty-block": lambda s: _claude_md_case(s, "c5-empty-block"),
    "project-doc-signals": lambda s: _project_doc_case(s, "project-doc"),
    "references-broken-refs": lambda s: _references_case(s, "references"),
}


def run_case(case_id: str, staged: Path) -> dict:
    result = CASES[case_id](staged)
    return normalize(result, staged)
