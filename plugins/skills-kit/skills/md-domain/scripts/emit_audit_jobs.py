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

Each emitted job carries an `evidence_pack` record stating whether the md-audit
evidence pack was attached to its prompt and, when it was, that pack's sha256 and
character count. The pack attaches only for the endpoint ids configured under
`adapters: {md-audit-evidence-pack: {admitted_endpoints: [...]}}` in skills-kit's
layered config (see md-domain/references/configuring-standards.md). The shipped
default is EMPTY, so nothing attaches until a user admits an endpoint. A
preference list mixing admitted and non-admitted endpoints is an error and fails
the emit (exit 4).

The emitted document is JSON (job-kit loads job files with yaml.safe_load, and
every JSON document is valid YAML, so no YAML serializer is needed here). The
runnable job-kit invocation, including the JOB_KIT_REPORT_DIR prefix, is
printed to stderr so `--out -` stays pipeable.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

# skills_kit_lib lives at the plugin root; make it importable regardless of
# which interpreter launched this script. The import is deferred to
# resolve_admitted_endpoints so the rest of this script stays stdlib-only and
# keeps working on a bare interpreter.
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

_SCRIPTS_DIR = Path(__file__).resolve().parent
_DISCOVER_PATH = _SCRIPTS_DIR / "discover_project_doc.py"
_CHECKER_PATH = _SCRIPTS_DIR / "check_project_doc_audit.py"
_EVIDENCE_PACK_PATH = _SCRIPTS_DIR / "evidence_pack.py"

DEFAULT_ENDPOINTS = ["sonnet", "opus", "luna"]
DEFAULT_MAX_PARALLEL = 4

# The md-audit evidence pack is an ADAPTER: task-specific context admitted for
# the model-task pairs it was measured on, and for nothing else. It was measured
# on 2026-09-04 for a locally hosted 27B-class endpoint auditing markdown, where
# the compact pack at a single call raised F1 from 0.36 to 0.51 for 7 percent
# more tokens (docs/planning/adapters/adapter-design.md, Outcome).
#
# The admitted set names ENDPOINT IDS, which differ per user and per fleet, so it
# is configuration rather than a shipped list. The shipped default is EMPTY: an
# unconfigured run attaches no pack, which is exactly the behaviour of not having
# the adapter at all. Attaching the pack to a model that does not need it is a
# tax, so an id in the set is a claim that THAT pair was measured -- do not widen
# the set to make a run attach a pack.
# Must match skills_kit_lib.standards_resolve.ADAPTER_MD_AUDIT_EVIDENCE_PACK;
# spelled literally here so this script imports nothing at module scope.
ADAPTER_ID = "md-audit-evidence-pack"


def resolve_admitted_endpoints(project_root: Path | None) -> frozenset[str]:
    """The adapter-admitted endpoint ids for this run, from layered config.

    Reads skills-kit's own layered configuration (user layer, then its
    config.local.yaml overlay, then the project layer and its overlay) through
    skills_kit_lib.standards_resolve -- no second config file and no environment
    variable. Returns an EMPTY set when nothing is configured, and also when the
    library or pyyaml is unavailable: an unresolvable config must never widen
    admission, only narrow it. A malformed config still raises loudly, because a
    typo'd admitted_endpoints is indistinguishable from the empty default.
    """
    try:
        from skills_kit_lib import standards_resolve
    except ImportError:
        return frozenset()
    resolved = standards_resolve.resolve(project_root)
    return resolved.adapter_admitted_endpoints(ADAPTER_ID)


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


def load_evidence_pack_module() -> ModuleType:
    """Load evidence_pack.py by path (the adapter's one builder)."""
    return _load_module(_EVIDENCE_PACK_PATH, "_emit_audit_jobs_evidence_pack")


class MixedAdapterEndpointsError(ValueError):
    """Raised when a preference list mixes admitted and non-admitted endpoints.

    job-kit resolves the preference list at RUN time, so a pack chosen at emit
    time against a mixed list is wrong for whichever endpoint the run does not
    pick: either an admitted endpoint loses the pack, or a non-admitted one is
    taxed with it. There is no per-job answer -- the emit itself is the error.
    """


def adapter_applies(endpoints: list[str], admitted_set: frozenset[str]) -> bool:
    """True when EVERY endpoint in the preference list is adapter-admitted.

    False when none is -- including when admitted_set is empty, which is the
    shipped default and means "no endpoint is admitted", never a mixed list. A
    list mixing admitted and non-admitted endpoints raises
    MixedAdapterEndpointsError rather than guessing.
    """
    admitted = [name for name in endpoints if name in admitted_set]
    if not admitted:
        return False
    if len(admitted) != len(endpoints):
        rejected = [name for name in endpoints if name not in admitted_set]
        raise MixedAdapterEndpointsError(
            "endpoint_preference mixes adapter-admitted and non-admitted "
            f"endpoints: {endpoints} (admitted: {admitted}; not admitted: "
            f"{rejected}). The md-audit evidence pack is admitted only for "
            f"{sorted(admitted_set)} (configured under adapters: "
            f"{{{ADAPTER_ID}: {{admitted_endpoints: [...]}}}}). Emit one job "
            "file per endpoint class instead of one mixed list."
        )
    return True


def build_evidence_pack(
    evidence_module: ModuleType, repo_root: Path, subject_rel: str
) -> tuple[str | None, str | None]:
    """Build the compact pack for one document.

    Returns (pack_text, None) on success and (None, reason) on failure. A pack
    that cannot be built degrades that ONE document to no pack -- an emit over a
    directory must not abort because a single subject defeated the builder.
    """
    try:
        pack = evidence_module.build_pack(str(repo_root), subject_rel)
    except Exception as exc:  # noqa: BLE001 -- any builder failure degrades
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(pack, str) or not pack.strip():
        return None, "evidence_pack.build_pack returned no text"
    return pack, None


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


def _require_nonempty_contract(
    criteria: set[str], taxonomy: dict[str, str], standards: Path | None = None
) -> None:
    """Fail loudly, and by name, when a standards doc's id table(s) are empty.

    build_example_report's `pick(seq, i) -> seq[i % len(seq)]` divides by
    len(seq); a standards doc carrying no criterion id, no taxonomy id, or
    neither raises ZeroDivisionError deep inside the picker with nothing
    naming the actual defect (an empty "### Criteria ids" or "### Taxonomy
    ids" table in the standards doc). Called both at load time in
    build_job_file (once per file, before any job is built) and at the pick
    site in build_example_report itself (so a direct caller that bypasses
    load_contract is guarded too), and names which table is empty.
    """
    empty = []
    if not criteria:
        empty.append("Criteria ids")
    if not taxonomy:
        empty.append("Taxonomy ids")
    if empty:
        where = f"{standards} has" if standards is not None else "the standards document has"
        print(
            f"REJECT: {where} an empty {' and '.join(empty)} table -- "
            "cannot build a two-finding example report with fewer than one id "
            "of each kind",
            file=sys.stderr,
        )
        raise SystemExit(1)


def build_example_report(
    subject_rel: str,
    subject_lines: int,
    criteria: set[str],
    taxonomy: dict[str, str],
) -> dict:
    """A two-finding example report, built from ids parsed from the standards
    document THIS run -- never a frozen id list. Degrades gracefully when the
    document carries fewer than two ids of a kind (raises a named SystemExit,
    never ZeroDivisionError, when it carries fewer than one)."""
    _require_nonempty_contract(criteria, taxonomy)
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
    evidence_pack: str | None = None,
    standards_text: str = "",
    subject_text: str = "",
) -> dict:
    """system/user prompt text for one audit job.

    evidence_pack, when present, is inserted after the block that names the
    audited document and before the response schema (the worked example and the
    facts the checker enforces), matching the insertion point the adapter was
    measured at.
    """
    example_text = json.dumps(example_report, indent=2)
    system = (
        "You are a document auditor. You read one project document against a "
        "written standards document and emit a single JSON audit report. "
        "You do not edit, write, or otherwise change any file."
    )
    if evidence_pack is not None:
        # INLINED, deliberately. Telling the model to go read three files is the
        # very lookup the evidence pack exists to precompute, and it obliges the
        # endpoint to have a filesystem -- which excludes exactly the toolless
        # transports the pack is admitted for. The measured instrument
        # (run_audit_adapter.py) was a SINGLE call with the sources inlined, so
        # this is the shipped path matching what the F1 figures were taken
        # against rather than a new shape. The checker is not inlined: the job's
        # contract command runs it locally, and the facts it enforces are stated
        # below in full.
        user = (
            "Audit this document against the md-domain project-doc standards.\n\n"
            "Everything you need is below; there are no files to open.\n\n"
            f"STANDARDS DOCUMENT ({standards_abs.name}):\n"
            f"{standards_text.rstrip()}\n\n"
            f"SUBJECT DOCUMENT ({subject_rel}):\n"
            f"{subject_text.rstrip()}\n\n"
            "Your entire output is piped to an acceptance checker on stdin; it "
            "decides whether your work is accepted.\n\n"
        )
    else:
        user = (
            "Audit this document against the md-domain project-doc standards.\n\n"
            "Read these files by path before you begin:\n"
            f"  standards document: {standards_abs}\n"
            f"  subject document:   {subject_abs}\n"
            f"  acceptance checker: {checker_abs}\n\n"
            "Your entire output is piped to the acceptance checker on stdin; it "
            "decides whether your work is accepted. Read it if anything below is "
            "ambiguous.\n\n"
        )
    user += (
        "Apply every criterion in the standards document's Criteria ids table "
        "to the subject document. For each violation you find, cite the "
        "criterion id and the taxonomy id from the standards document, and "
        "give the bucket the standards document's Taxonomy ids table assigns "
        "to that taxonomy id -- do not choose the bucket yourself.\n\n"
    )
    if evidence_pack is not None:
        # This wrapper line is part of the measured stimulus, not decoration:
        # the 2026-09-04 F1 figures were produced with exactly this framing and
        # no code fence. Rewording it, or fencing the pack, changes the prompt
        # the measurement was taken against. The pack closes with its own
        # "rows are facts, not findings" line, so no further framing is needed.
        # A fence would also be unsafe here: pack rows carry backticked paths.
        user += (
            "EVIDENCE (pre-computed facts about FILE and its repository "
            "context):\n"
            f"{evidence_pack.rstrip()}\n\n"
        )
    user += (
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
    evidence_module: ModuleType | None = None,
    subject_lines: int | None = None,
) -> dict:
    subject_abs = Path(record["path"]).resolve()
    subject_rel = subject_abs.relative_to(repo_root).as_posix()
    job_id = unique_id(slugify(subject_rel), used_ids)
    # The caller (build_job_file) already computed this once while filtering
    # out zero-line subjects; accept it to avoid re-reading the file. Falls
    # back to computing it for any other caller that does not have it handy.
    if subject_lines is None:
        subject_lines = subject_line_count(subject_abs)
    example_report = build_example_report(subject_rel, subject_lines, criteria, taxonomy)

    pack: str | None = None
    pack_error: str | None = None
    if evidence_module is not None:
        pack, pack_error = build_evidence_pack(evidence_module, repo_root, subject_rel)
        if pack_error is not None:
            print(
                f"evidence pack unavailable for {subject_rel}: {pack_error}",
                file=sys.stderr,
            )

    # Read the sources only when they will be inlined -- an unadmitted endpoint
    # still gets the read-by-path prompt and pays nothing for this.
    standards_text = ""
    subject_text = ""
    if pack is not None:
        # Escaped with the pack's own helper, because the emitted job document is
        # ASCII-only and a real subject document is not: this repo's docs carry
        # em dashes and emoji. The pack already renders its rows this way, so an
        # inlined document reads in the same encoding as the evidence about it.
        # Nothing is lost for the audit -- the pack's MECHANICAL section
        # enumerates every non-ASCII character by line and codepoint, which is
        # what the hygiene criteria are judged from.
        to_ascii = evidence_module.ascii_text
        standards_text = to_ascii(standards_abs.read_text(encoding="utf-8"))
        subject_text = to_ascii(subject_abs.read_text(encoding="utf-8"))

    prompt = build_prompt(
        subject_rel=subject_rel,
        subject_abs=subject_abs,
        standards_abs=standards_abs,
        checker_abs=checker_abs,
        subject_lines=subject_lines,
        example_report=example_report,
        evidence_pack=pack,
        standards_text=standards_text,
        subject_text=subject_text,
    )

    evidence_record: dict[str, object] = {"attached": pack is not None}
    if pack is not None:
        evidence_record["sha256"] = hashlib.sha256(pack.encode("utf-8")).hexdigest()
        evidence_record["char_count"] = len(pack)
    elif pack_error is not None:
        evidence_record["error"] = pack_error

    return {
        "id": job_id,
        "prompt": prompt,
        "endpoint_preference": list(endpoints),
        "evidence_pack": evidence_record,
        # cwd is required only so the model can open the standards and subject
        # documents. When they are inlined there is nothing to open, and keeping
        # the requirement would exclude every toolless transport -- which is the
        # endpoint class the evidence pack is admitted for in the first place.
        "requirements": {} if pack is not None else {"params": ["cwd"]},
        # An adapter is admitted for a model-task pair AT A REQUEST
        # CONFIGURATION, so the shipped path has to reproduce the one the F1
        # figures were taken at -- max_tokens 60000 and reasoning_effort xhigh
        # (2026-09-04, NInfer a140e7ae, model-default sampling). job-kit's
        # default of 4096 is exhausted by this model's reasoning before it emits
        # any content at all, which fails the job with finish_reason=length and
        # no report. Sending the pack at an untested configuration would make
        # the admission claim false, so these travel with it.
        "options": (
            {"max_tokens": 60000, "extras": {"reasoning_effort": "xhigh"}}
            if pack is not None
            else {}
        ),
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

    # A zero-line subject admits no acceptable finding: the checker requires
    # 1 <= line <= subject_lines, which is unsatisfiable at 0, so every job
    # emitted for one could only ever be rejected. Drop it here rather than
    # emit a job that cannot pass its own contract.
    kept = []
    # Computed once here and reused in build_job below -- subject_line_count
    # used to be called a second time per file inside build_job, re-reading
    # and re-splitting a file this loop had just read.
    line_counts: dict[str, int] = {}
    for record in records:
        subject_abs = (repo_root / record["path"]).resolve()
        lines = subject_line_count(subject_abs)
        if lines < 1:
            print(
                f"skipping {record['path']}: empty document admits no finding",
                file=sys.stderr,
            )
            continue
        line_counts[record["path"]] = lines
        kept.append(record)
    records = kept

    if limit is not None:
        records = records[:limit]

    checker_module = load_checker_module()
    criteria, taxonomy = checker_module.load_contract(standards)
    _require_nonempty_contract(criteria, taxonomy, standards)

    # Raises on a mixed preference list -- deliberately before any job is built,
    # so a mixed list fails the emit rather than producing a half-adapted file.
    admitted_set = resolve_admitted_endpoints(repo_root)
    evidence_module = (
        load_evidence_pack_module() if adapter_applies(endpoints, admitted_set) else None
    )

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
            evidence_module=evidence_module,
            subject_lines=line_counts.get(record["path"]),
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


def _non_ascii_strings(value: object, path: str = "") -> list[str]:
    """Report every string in a nested structure that carries non-ASCII text.

    json.dumps escapes non-ASCII to \\uXXXX by default, so scanning the
    RENDERED document can never observe it. The source strings are the only
    place the check is meaningful.
    """
    hits: list[str] = []
    if isinstance(value, str):
        if _has_non_ascii(value):
            hits.append(path or "<root>")
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and _has_non_ascii(key):
                hits.append(f"{path}.{key} (key)" if path else f"{key} (key)")
            hits.extend(_non_ascii_strings(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits.extend(_non_ascii_strings(item, f"{path}[{index}]"))
    return hits


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

    try:
        document = build_job_file(
            subject_dir=subject_dir,
            repo_root=repo_root,
            standards=standards,
            endpoints=endpoints,
            max_parallel=args.max_parallel,
            limit=args.limit,
        )
    except MixedAdapterEndpointsError as exc:
        print(exc, file=sys.stderr)
        return 4

    non_ascii = _non_ascii_strings(document)
    if non_ascii:
        print(
            "emitted document contains non-ASCII text at: "
            + ", ".join(non_ascii[:5]),
            file=sys.stderr,
        )
        return 3

    rendered = json.dumps(document, indent=2)

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
