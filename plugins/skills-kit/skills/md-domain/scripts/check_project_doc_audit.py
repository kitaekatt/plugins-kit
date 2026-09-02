#!/usr/bin/env python3
"""Acceptance contract for a project-doc audit run.

Reads an audit report as JSON on stdin and exits 0 only if every finding is
GROUNDED: it cites a criterion id and a taxonomy id that actually appear in the
project-doc standards document, carries the bucket that document assigns to
that taxonomy id, and points at a line inside the audited file.

    check_project_doc_audit.py <subject-path> <repo-root> [--standards PATH]

Designed as the `contract` command of an unattended runner (job-kit), so it
reads THIS attempt's completion text on stdin rather than a file an earlier
attempt may have left behind, and it is stdlib-only: it must run under whatever
interpreter the runner has.

WHAT IT DOES NOT DO. It does not check whether a finding is TRUE. A grounded
finding can still be wrong about the document, and deciding that stays the
reader's job. The floor this raises is that a finding which cannot even cite
correctly is rejected mechanically -- the failure mode an unattended run cannot
otherwise catch, and one that keeps the acceptance falsifiable. Anything richer
(an LLM judging finding quality) would remove that property, which is why it is
excluded rather than merely absent.

The valid ids are PARSED from the standards document at run time, never
restated here. A second copy of that id set is a source of truth that drifts
out of the first one silently; the id tables are contract surface precisely so
that a checker can read them.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

DEFAULT_STANDARDS = (
    Path(__file__).resolve().parents[1]
    / "references" / "standards" / "project-doc-standards.md"
)

VERDICTS = {"PASS", "FAIL"}

_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|")


def fail(message: str) -> None:
    print(f"REJECT: {message}", file=sys.stderr)
    raise SystemExit(1)


def _table_after(lines: list[str], heading: str) -> dict[str, str]:
    """Map first column -> second column for the table under `heading`.

    Stops at the next heading, so an id table is read whole and nothing beyond
    it leaks in.
    """
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == heading)
    except StopIteration:
        fail(f"standards document has no {heading!r} section")
    out: dict[str, str] = {}
    for line in lines[start + 1:]:
        if line.startswith("#"):
            break
        match = _ROW.match(line)
        if match:
            out[match.group(1)] = match.group(2).strip()
    if not out:
        fail(f"no id rows found under {heading!r} in the standards document")
    return out


def load_contract(standards: Path) -> tuple[set[str], dict[str, str]]:
    """Return (criterion ids, taxonomy id -> bucket) read from the standards doc."""
    if not standards.is_file():
        fail(f"standards document not found: {standards}")
    lines = standards.read_text(encoding="utf-8").splitlines()
    criteria = set(_table_after(lines, "### Criteria ids"))
    taxonomy = _table_after(lines, "### Taxonomy ids")
    return criteria, taxonomy


def extract_json(text: str) -> dict:
    """Take the outermost JSON object, tolerating a fenced code block."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    if not candidate.strip():
        fail("no JSON object in the completion text")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        fail(f"completion text is not valid JSON: {exc}")
    raise AssertionError("unreachable")


def check_report(
    report: dict,
    subject_arg: str,
    subject_lines: int,
    criteria: set[str],
    taxonomy: dict[str, str],
) -> None:
    """Reject the report unless every finding is grounded. Raises SystemExit(1)."""
    if set(report) != {"subject", "verdict", "findings"}:
        fail(f"report keys must be exactly subject/verdict/findings, got {sorted(report)}")
    if report["subject"] != subject_arg:
        fail(f"report subject {report['subject']!r} is not the audited {subject_arg!r}")
    if report["verdict"] not in VERDICTS:
        fail(f"verdict must be one of {sorted(VERDICTS)}, got {report['verdict']!r}")
    findings = report["findings"]
    if not isinstance(findings, list):
        fail("findings must be a list")

    for n, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            fail(f"finding {n} is not an object")
        expected = {"criterion", "taxonomy", "bucket", "line", "remediation"}
        if set(finding) != expected:
            fail(f"finding {n} keys must be exactly {'/'.join(sorted(expected))}, "
                 f"got {sorted(finding)}")
        if finding["criterion"] not in criteria:
            fail(f"finding {n} cites unknown criterion {finding['criterion']!r}")
        if finding["taxonomy"] not in taxonomy:
            fail(f"finding {n} cites unknown taxonomy id {finding['taxonomy']!r}")
        declared = taxonomy[finding["taxonomy"]]
        if finding["bucket"] != declared:
            fail(f"finding {n} cites bucket {finding['bucket']!r} but the standards "
                 f"document assigns {declared!r} to {finding['taxonomy']!r}")
        line = finding["line"]
        if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= subject_lines:
            fail(f"finding {n} line {line!r} is outside the audited file (1..{subject_lines})")
        if not isinstance(finding["remediation"], str) or not finding["remediation"].strip():
            fail(f"finding {n} has no remediation")

    if (report["verdict"] == "PASS") != (not findings):
        fail(f"verdict {report['verdict']} is inconsistent with {len(findings)} finding(s)")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    standards = DEFAULT_STANDARDS
    if "--standards" in argv:
        i = argv.index("--standards")
        if i + 1 >= len(argv):
            fail("--standards needs a path")
        standards = Path(argv[i + 1]).expanduser()
        del argv[i:i + 2]
    if len(argv) != 2:
        fail("usage: check_project_doc_audit.py <subject-path> <repo-root> "
             "[--standards PATH]")

    subject_arg, repo_root = argv[0], Path(argv[1]).resolve()
    subject_file = (repo_root / subject_arg).resolve()
    if not subject_file.is_file():
        fail(f"subject does not exist: {subject_file}")
    subject_lines = len(subject_file.read_text(encoding="utf-8").splitlines())

    criteria, taxonomy = load_contract(standards)

    text = sys.stdin.read()
    if not text.strip():
        fail("this attempt produced no output at all")
    report = extract_json(text)
    check_report(report, subject_arg, subject_lines, criteria, taxonomy)

    destination = Path(os.environ.get("JOB_KIT_REPORT_DIR", ".")) / (
        f"{os.environ.get('JOB_KIT_JOB_ID', 'job')}"
        f".{os.environ.get('JOB_KIT_RUN_ID', 'run')}"
        f".{os.environ.get('JOB_KIT_ATTEMPT_NO', '0')}.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"ACCEPT: {subject_arg}: {report['verdict']}, "
          f"{len(report['findings'])} finding(s) -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
