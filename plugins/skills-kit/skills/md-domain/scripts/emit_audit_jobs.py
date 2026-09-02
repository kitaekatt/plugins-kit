#!/usr/bin/env python3
"""emit_audit_jobs.py -- emit a job-kit job file auditing a directory of
project docs against md-domain's project-doc standards.

Discovers every project_doc under a subject directory (via
discover_project_doc.py) and emits one job-kit job per doc. Each job's
acceptance contract is the already-shipped check_project_doc_audit.py, run
against that one doc; the model completing the job is expected to produce an
audit report as its entire output, which the contract reads on stdin and
validates against ids parsed from the standards document at run time.

Stdlib-only. Sibling scripts (discover_project_doc.py,
check_project_doc_audit.py) are loaded by path via importlib rather than
imported normally, so this script runs under whatever interpreter invokes it
without depending on package layout.

Usage:
    emit_audit_jobs.py <subject-dir> [--repo-root PATH] [--standards PATH]
        [--endpoint NAME ...] [--report-dir PATH] [--max-parallel N]
        [--limit N] [--out PATH|-]

The emitted document is JSON (job-kit loads job files with yaml.safe_load, and
every JSON document is valid YAML, so no YAML serializer is needed here). The
runnable job-kit invocation, including the JOB_KIT_REPORT_DIR prefix, is
printed to stderr so `--out -` stays pipeable.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

_SCRIPTS_DIR = Path(__file__).resolve().parent
_DISCOVER_PATH = _SCRIPTS_DIR / "discover_project_doc.py"
_CHECKER_PATH = _SCRIPTS_DIR / "check_project_doc_audit.py"

DEFAULT_ENDPOINTS = ["sonnet", "opus", "luna"]
DEFAULT_MAX_PARALLEL = 4

# Deny floor: no job in this run may write, edit, or otherwise mutate files.
# The job is an audit -- it reads a subject doc and a standards doc and
# produces a report; it has no legitimate reason to touch the working tree.
WRITE_TOOL_DENY_FLOOR = "Write Edit MultiEdit NotebookEdit"


def _load_module(path: Path, name: str) -> ModuleType:
    """Load a sibling script as a module, by path, under a private name."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def discover_project_docs(subject_dir: Path) -> list[dict]:
    """Run discover_project_doc.py --root <subject_dir> --json in-process.

    Returns the records whose kind is "project_doc" only -- skill references
    and other CLAUDE artifacts are audited by other lanes, not this one.
    """
    module = _load_module(_DISCOVER_PATH, "_emit_audit_jobs_discover_project_doc")
    old_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["discover_project_doc.py", "--root", str(subject_dir), "--json"]
        with contextlib.redirect_stdout(buf):
            module.main()
    finally:
        sys.argv = old_argv
    records = json.loads(buf.getvalue())
    return [record for record in records if record.get("kind") == "project_doc"]


def load_checker_module() -> ModuleType:
    """Load check_project_doc_audit.py by path (single parser of the standards doc)."""
    return _load_module(_CHECKER_PATH, "_emit_audit_jobs_check_project_doc_audit")


def default_repo_root(subject_dir: Path) -> Path:
    """git rev-parse --show-toplevel from subject_dir."""
    try:
        result = subprocess.run(
            ["git", "-C", str(subject_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(
            f"cannot determine repo root from {subject_dir} (pass --repo-root): {exc}"
        )
    return Path(result.stdout.strip()).resolve()


_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def slugify(repo_relative_path: str) -> str:
    """docs/reference/testing.md -> docs_reference_testing_md."""
    slug = _SLUG_RE.sub("_", repo_relative_path).strip("_")
    return slug or "doc"


def unique_id(base: str, used: set[str]) -> str:
    """base, or base_2 / base_3 / ... the first time base collides."""
    if base not in used:
        used.add(base)
        return base
    n = 2
    while f"{base}_{n}" in used:
        n += 1
    candidate = f"{base}_{n}"
    used.add(candidate)
    return candidate


def subject_line_count(subject_file: Path) -> int:
    """The exact count check_project_doc_audit.py's check_report validates
    finding['line'] against -- must match its computation bit for bit."""
    text = subject_file.read_text(encoding="utf-8")
    return len(text.splitlines())


def build_example_report(
    subject_rel: str,
    subject_lines: int,
    criteria: set[str],
    taxonomy: dict[str, str],
) -> dict:
    """A two-finding example report, built from ids parsed from the standards
    document THIS run -- never a frozen id list. Degrades gracefully when the
    document carries fewer than two ids of a kind."""
    criteria_sorted = sorted(criteria)
    taxonomy_sorted = sorted(taxonomy)

    def pick(seq: list[str], index: int) -> str:
        return seq[index % len(seq)]

    line_a = 1
    line_b = min(2, subject_lines)

    findings = []
    for i, (line, remediation) in enumerate(
        (
            (line_a, "Move the escape-hatch content to its trigger-appropriate home."),
            (line_b, "Cite the target by one hop instead of chaining through an index."),
        )
    ):
        taxonomy_id = pick(taxonomy_sorted, i)
        findings.append(
            {
                "criterion": pick(criteria_sorted, i),
                "taxonomy": taxonomy_id,
                "bucket": taxonomy[taxonomy_id],
                "line": line,
                "remediation": remediation,
            }
        )

    return {
        "subject": subject_rel,
        "verdict": "FAIL",
        "findings": findings,
    }


def build_prompt(
    *,
    subject_rel: str,
    subject_abs: Path,
    standards_abs: Path,
    checker_abs: Path,
    subject_lines: int,
    example_report: dict,
) -> dict:
    """system/user prompt text for one audit job."""
    example_text = json.dumps(example_report, indent=2)
    system = (
        "You are a document auditor. You read one project document against a "
        "written standards document and emit a single JSON audit report. "
        "You do not edit, write, or otherwise change any file."
    )
    user = (
        "Audit this document against the md-domain project-doc standards.\n\n"
        "Read these files by path before you begin:\n"
        f"  standards document: {standards_abs}\n"
        f"  subject document:   {subject_abs}\n"
        f"  acceptance checker: {checker_abs}\n\n"
        "Your entire output is piped to the acceptance checker on stdin; it "
        "decides whether your work is accepted. Read it if anything below is "
        "ambiguous.\n\n"
        "Apply every criterion in the standards document's Criteria ids table "
        "to the subject document. For each violation you find, cite the "
        "criterion id and the taxonomy id from the standards document, and "
        "give the bucket the standards document's Taxonomy ids table assigns "
        "to that taxonomy id -- do not choose the bucket yourself.\n\n"
        "Worked example of the exact report shape (built from this run's "
        "standards document, illustrative content only -- do not audit "
        "against this example, audit against the subject document):\n\n"
        "```json\n"
        f"{example_text}\n"
        "```\n\n"
        "Four facts the checker enforces that you cannot infer from the "
        "example alone:\n\n"
        f"1. report[\"subject\"] must equal exactly the string "
        f"\"{subject_rel}\" -- the repo-relative path shown above, copied "
        "verbatim. Not an absolute path, not a different spelling.\n"
        f"2. Every finding[\"line\"] must be an integer between 1 and "
        f"{subject_lines} inclusive -- the subject document has "
        f"{subject_lines} lines.\n"
        "3. report[\"verdict\"] is \"PASS\" if and only if "
        "report[\"findings\"] is an empty list; otherwise it is \"FAIL\". "
        "If you find no violations, emit verdict \"PASS\" and an empty "
        "findings list -- do not invent a finding to have something to "
        "report.\n"
        "4. The report object's keys must be exactly subject, verdict, "
        "findings -- nothing more, nothing less. Each finding's keys must be "
        "exactly criterion, taxonomy, bucket, line, remediation.\n\n"
        "Emit the JSON object and nothing else."
    )
    return {"system": system, "user": user}


def build_job(
    *,
    record: dict,
    repo_root: Path,
    standards_abs: Path,
    checker_abs: Path,
    python_abs: Path,
    endpoints: list[str],
    used_ids: set[str],
    criteria: set[str],
    taxonomy: dict[str, str],
) -> dict:
    subject_abs = Path(record["path"]).resolve()
    subject_rel = subject_abs.relative_to(repo_root).as_posix()
    job_id = unique_id(slugify(subject_rel), used_ids)
    subject_lines = subject_line_count(subject_abs)
    example_report = build_example_report(subject_rel, subject_lines, criteria, taxonomy)
    prompt = build_prompt(
        subject_rel=subject_rel,
        subject_abs=subject_abs,
        standards_abs=standards_abs,
        checker_abs=checker_abs,
        subject_lines=subject_lines,
        example_report=example_report,
    )
    return {
        "id": job_id,
        "prompt": prompt,
        "endpoint_preference": list(endpoints),
        "requirements": {"params": ["cwd"]},
        "directory": str(repo_root),
        "workspace": {"isolate": False},
        "contract": {
            "command": [
                str(python_abs),
                str(checker_abs),
                subject_rel,
                str(repo_root),
                "--standards",
                str(standards_abs),
            ],
        },
    }


def build_job_file(
    *,
    subject_dir: Path,
    repo_root: Path,
    standards: Path,
    endpoints: list[str],
    max_parallel: int,
    limit: int | None,
) -> dict:
    records = discover_project_docs(subject_dir)
    records.sort(key=lambda r: r["path"])
    if limit is not None:
        records = records[:limit]

    checker_module = load_checker_module()
    criteria, taxonomy = checker_module.load_contract(standards)

    python_abs = Path(sys.executable).resolve()
    used_ids: set[str] = set()
    jobs = [
        build_job(
            record=record,
            repo_root=repo_root,
            standards_abs=standards,
            checker_abs=_CHECKER_PATH,
            python_abs=python_abs,
            endpoints=endpoints,
            used_ids=used_ids,
            criteria=criteria,
            taxonomy=taxonomy,
        )
        for record in records
    ]

    return {
        "jobs": jobs,
        "max_parallel": max_parallel,
        "disallowed_tools": WRITE_TOOL_DENY_FLOOR,
    }


def _has_non_ascii(text: str) -> bool:
    return any(ord(ch) > 127 for ch in text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subject_dir", type=Path)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--standards", type=Path, default=None)
    parser.add_argument("--endpoint", action="append", default=None)
    parser.add_argument("--report-dir", type=Path, default=None)
    parser.add_argument("--max-parallel", type=int, default=DEFAULT_MAX_PARALLEL)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", default="-")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    subject_dir = args.subject_dir.resolve()
    if not subject_dir.is_dir():
        print(f"not a directory: {subject_dir}", file=sys.stderr)
        return 2

    repo_root = (
        args.repo_root.resolve() if args.repo_root else default_repo_root(subject_dir)
    )

    checker_module = load_checker_module()
    standards = (
        args.standards.resolve() if args.standards else checker_module.DEFAULT_STANDARDS
    )

    endpoints = args.endpoint if args.endpoint else list(DEFAULT_ENDPOINTS)
    report_dir = (args.report_dir.resolve() if args.report_dir else Path.cwd() / "reports")

    document = build_job_file(
        subject_dir=subject_dir,
        repo_root=repo_root,
        standards=standards,
        endpoints=endpoints,
        max_parallel=args.max_parallel,
        limit=args.limit,
    )

    rendered = json.dumps(document, indent=2)
    if _has_non_ascii(rendered):
        print("emitted document contains non-ASCII text", file=sys.stderr)
        return 3

    if args.out in ("-", None):
        print(rendered)
        out_display = "jobs.yaml"
    else:
        out_path = Path(args.out).resolve()
        out_path.write_text(rendered, encoding="utf-8")
        out_display = str(out_path)

    job_count = len(document["jobs"])
    print(f"discovered {job_count} project doc job(s)", file=sys.stderr)
    print(
        f"JOB_KIT_REPORT_DIR={report_dir} job-kit run {out_display}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
