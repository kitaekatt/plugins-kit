"""Task discovery -- the spec section 8 algorithm (the ``list`` substrate).

Two crawl modes, both script-driven (spec 8):

1. **By folder (primary):** every ``task.yaml`` under the scope's roots
   (``dev/tasks/`` + ``tmp/``) -- the authoritative enumeration of live tasks.
2. **By reference (scoped association):** every ``refs[].path`` (+ optional
   ``host``) from ``task_list:`` typed-unit blocks embedded in the scope's
   document set. ``project``/``user`` scope that document set to markdown
   under the task roots; reaching a ref embedded elsewhere means naming its
   document (``skill``/``file`` scope). See the scope->documents reading.

Candidates are canonicalized (resolve.py) and deduped by canonical path; each
survivor is classified via validate.py (classification logic is never
duplicated here) and projected to a TaskRecord (id, classification, title,
priority, last update, host).

Readings chosen in Step 3 (flagged in the implementation report):

- **Scope -> documents (``project``/``user``).** The document set is the
  ``*.md`` under the scope's task roots (``tmp/`` + ``dev/tasks/``), NOT the
  whole tree, and the parked ``<root>/archived-tasks/`` subtree is excluded
  under EITHER root for parity with the folder crawl (both roots park: tmp
  always, dev/tasks when git is configured to ignore the folder). Rationale:
  an embedded ``task_list`` block is
  indistinguishable from an example of one, so a whole-tree crawl reports the
  format's own documentation as live tasks (this is what it did -- the spec's
  2.4 sample block resolved as three phantom tasks in plugins-kit, the one
  project whose tree contains the skill's source). The association feature is
  unchanged, just no longer implicit: name the document via ``skill``/``file``
  scope to pick up refs outside the task roots.
- **Scope -> effective root.** ``user`` discovers against the user root
  (default ``~/.claude``; parameter-injectable for tests): both its task
  roots (``<user_root>/dev/tasks`` + ``<user_root>/tmp``) and its document
  set live there, and refs found in user documents canonicalize against it.
  All other scopes use the project root.
- **``skill <name-or-path>``.** A target that is an existing path (absolute
  or project-root-relative) to a skill directory or to its SKILL.md is used
  directly; otherwise the target is a skill NAME, located by searching the
  project for ``skills/<name>/SKILL.md`` (multiple matches -> error listing
  candidates; none -> error). Docs = SKILL.md + ``references/*.md``.
- **Union semantics.** Per spec 8 step 2 the candidate set is the UNION of
  folder crawl and reference scan in EVERY scope -- ``skill``/``file``
  narrow the DOCUMENT set only ("roots as project"), so folder-found tasks
  appear in every local-rooted scope.
- **Non-canonical task.yaml.** A ``task.yaml`` found at a non-canonical
  depth under a root (e.g. nested inside a task folder) is not a known task
  location: skipped with a note, never silently.
- **Host retention on dedupe.** The first non-None ``host`` seen for a
  canonical path is retained and threaded to validate as ``ref_host`` (a
  folder-crawl candidate carries no host). Remote refs are opaque: tmp +
  non-matching host is never read locally, even when a same-named local
  folder exists (validate's remote short-circuit governs).
- **Archived omitted by default.** ``discover`` retains the working-set
  projection: a task that classifies as ``archived`` is dropped unless the
  caller asks for ``status="archived"``. The CLI applies its own open/closed
  sectioning on top of this projection; remote, orphaned, and invalid records
  remain available to direct discovery and explicit status queries.
- **Notes, not crashes.** Skipped material (unreadable documents,
  unparseable/malformed ``task_list`` blocks, unresolvable ref paths,
  non-canonical folder hits) is collected into the optional ``notes``
  accumulator; discovery never raises for bad content (only for a bad
  scope/target -- DiscoveryError).

Reuse note: ``skills_kit_lib.document_walker.iter_yaml_blocks`` supplies the
fenced-YAML-block extraction. Its ``collect_yaml_units`` cannot serve here:
it filters by skills-kit's ``SCHEMAS_BY_ROOT`` registry, and ``task_list``
is deliberately NOT registered there (the schema lives in awesome-kit, CCP
-- see schemas.py). Block parsing + ``task_list`` recognition + schema
validation (via ``skills_kit_lib.schema_engine``) happen here.
"""

from __future__ import annotations

import os
import datetime
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from skills_kit_lib import schema_engine
from skills_kit_lib.document_walker import iter_yaml_blocks

from . import resolve
from .schemas import TASK_LIST_SCHEMA
from .validate import validate_ref

SCOPES = ("all", "project", "user", "skill", "file")
LOCAL_SCOPES = ("project", "user", "skill", "file")
DEFAULT_USER_ROOT = Path.home() / ".claude"
TASK_CONFIG_PATH = DEFAULT_USER_ROOT / "task.local.yaml"
_ENV_REFERENCE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|%([A-Za-z_][A-Za-z0-9_]*)%")

_TASK_LIST_KEY_RE = re.compile(r"^task_list\s*:", re.MULTILINE)
# A log entry is `- YYYY-MM-DD HH:MM: detail`; the time is optional (older
# entries carry the date only) and may include seconds, which are dropped.
_LOG_ENTRY_RE = re.compile(
    r"^\s*-\s+(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2})(?::\d{2})?)?:\s*(.*?)\s*$",
    re.MULTILINE,
)
# The time a date-only log entry is read as.
DATE_ONLY_TIME = "12:00"
OPEN_CLASSIFICATIONS = frozenset(("active", "blocked"))


class DiscoveryError(ValueError):
    """A discovery scope or target could not be resolved."""


@dataclass(frozen=True)
class ProjectRoot:
    """A project directory eligible for configured multi-project discovery."""

    name: str
    root: Path


@dataclass(frozen=True)
class TaskUpdate:
    """One dated entry from a task folder's append-only update log."""

    date: str
    detail: str
    time: str | None = None

    @property
    def timestamp(self) -> str:
        """Sortable ``YYYY-MM-DD HH:MM``; a date-only entry reads as noon."""
        return f"{self.date} {self.time or DATE_ONLY_TIME}"


@dataclass(frozen=True)
class TaskRecord:
    """One discovered task, projected for ``list`` (spec 8 step 5)."""

    id: str  # canonical project-relative path (the task id, spec 5)
    classification: str  # validate's outcome (stored status or computed)
    title: str | None = None
    priority: str | None = None
    last_update: str | None = None
    host: str | None = None  # retained ref host tag, when any ref carried one
    project_name: str = ""
    project_root: Path | None = None


def read_task_block(folder: Path) -> dict | None:
    """The parsed ``task:`` block of a folder's task.yaml, or None when it is
    absent/unreadable/unparseable/mis-shaped.

    Projection helper only -- findings about a bad record are validate.py's
    job; this never raises.
    """
    try:
        data = yaml.safe_load((folder / "task.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("task")
    return block if isinstance(block, dict) else None


def read_last_update(folder: Path) -> str | None:
    """Return the latest ISO date from dated ``log.md`` entries.

    The log is the task system's durable update history. A scaffold has no
    dated entry yet, so the projection uses ``None`` until the first update or
    archive entry is recorded.
    """
    updates = read_task_updates(folder)
    return max((entry.date for entry in updates), default=None)


def read_task_updates(folder: Path) -> tuple[TaskUpdate, ...]:
    """Read dated activity entries from the task's append-only log.

    Summary-maintenance entries are deliberately excluded: generating a
    summary must not make an otherwise idle task appear recently active.
    """
    try:
        text = (folder / "log.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    entries = [
        TaskUpdate(date=date, detail=detail, time=time or None)
        for date, time, detail in _LOG_ENTRY_RE.findall(text)
        if not (detail.startswith("summary:") or detail.startswith("update: summary:"))
    ]
    return tuple(
        sorted(reversed(entries), key=lambda entry: entry.timestamp, reverse=True)
    )


def log_timestamp() -> str:
    """The local ``YYYY-MM-DD HH:MM`` stamp a new log.md entry carries."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# --- scope resolution --------------------------------------------------------


def _resolve_skill_docs(target: str, project_root: Path) -> list[Path]:
    """The document set for ``skill <name-or-path>`` (module docstring)."""
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = project_root / target
    skill_dir: Path | None = None
    if candidate.is_dir() and (candidate / "SKILL.md").is_file():
        skill_dir = candidate
    elif candidate.is_file() and candidate.name == "SKILL.md":
        skill_dir = candidate.parent
    else:
        matches = sorted(project_root.rglob(f"skills/{target}/SKILL.md"))
        if len(matches) > 1:
            raise DiscoveryError(
                f"ambiguous skill name {target!r}: candidates: "
                + ", ".join(str(m.parent) for m in matches)
            )
        if matches:
            skill_dir = matches[0].parent
    if skill_dir is None:
        raise DiscoveryError(
            f"skill {target!r} not found: no SKILL.md at that path and no "
            f"skills/{target}/SKILL.md under {project_root}"
        )
    docs = [skill_dir / "SKILL.md"]
    docs.extend(sorted((skill_dir / "references").glob("*.md")))
    return docs


def _scope_docs(
    scope: str, effective_root: Path, project_root: Path, target: str | None
) -> list[Path]:
    """Resolve scope -> document set (spec 8 step 1)."""
    if scope in ("project", "user"):
        # Only *.md under the task roots (tmp/, dev/tasks/) -- task folders'
        # own docs, which may legitimately embed a task_list. A whole-tree
        # crawl is deliberately NOT done: it cannot distinguish a document
        # USING the task_list format from one DOCUMENTING it, so an example
        # block in a design doc resolves as live tasks. Refs embedded outside
        # the task roots stay reachable, but only by naming their document --
        # ``--scope skill <name>`` / ``--scope file <path>``.
        docs: list[Path] = []
        for root_tag in resolve.KNOWN_ROOTS:
            base = effective_root / root_tag
            if not base.is_dir():
                continue
            for doc in base.rglob("*.md"):
                rel = doc.relative_to(effective_root).parts
                if resolve.is_parked_parts(rel):
                    continue  # parked archived tasks: not listed (2a parity)
                docs.append(doc)
        return sorted(docs)
    assert target is not None  # guarded in discover()
    if scope == "skill":
        return _resolve_skill_docs(target, project_root)
    # scope == "file"
    doc = Path(target)
    if not doc.is_absolute():
        doc = project_root / target
    if not doc.is_file():
        raise DiscoveryError(f"file scope target is not a readable file: {target!r}")
    return [doc]


# --- candidate collection ----------------------------------------------------


def _add_candidate(
    candidates: dict[str, str | None], canonical: str, host: str | None
) -> None:
    """Dedupe by canonical path; retain the first non-None host tag seen."""
    if canonical not in candidates:
        candidates[canonical] = host
    elif candidates[canonical] is None and host is not None:
        candidates[canonical] = host


def _crawl_folders(
    effective_root: Path, candidates: dict[str, str | None], notes: list[str]
) -> None:
    """Folder crawl: every task.yaml under the known roots (spec 8 step 2a)."""
    for root_tag in resolve.KNOWN_ROOTS:
        base = effective_root / root_tag
        if not base.is_dir():
            continue
        for task_yaml in sorted(base.rglob("task.yaml")):
            rel_parts = task_yaml.parent.relative_to(effective_root).parts
            if resolve.is_parked_parts(rel_parts):
                continue  # parked archived tasks: deliberate, not listed
            rel = task_yaml.parent.relative_to(effective_root).as_posix()
            try:
                resolved = resolve.resolve_path(rel, effective_root)
            except resolve.RefResolutionError as exc:
                notes.append(
                    f"skipped task.yaml at non-canonical location {rel!r}: {exc}"
                )
                continue
            _add_candidate(candidates, resolved.canonical, None)


def _scan_doc(
    doc: Path,
    effective_root: Path,
    candidates: dict[str, str | None],
    notes: list[str],
) -> None:
    """Reference scan of one document (spec 8 step 2b)."""
    try:
        text = doc.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        notes.append(f"skipped unreadable document {doc}: {exc}")
        return
    for block_text in iter_yaml_blocks(text):
        try:
            data = yaml.safe_load(block_text)
        except yaml.YAMLError:
            if _TASK_LIST_KEY_RE.search(block_text):
                notes.append(
                    f"{doc}: skipped unparseable YAML block containing task_list"
                )
            continue
        if not isinstance(data, dict) or "task_list" not in data:
            continue
        fails, _ = schema_engine.validate(data, TASK_LIST_SCHEMA)
        if fails:
            detail = "; ".join(f"{path}: {msg}" for path, msg in fails[:3])
            notes.append(f"{doc}: skipped malformed task_list block ({detail})")
            continue
        for ref in data["task_list"]["refs"]:
            path = ref["path"]  # schema-guaranteed present string
            host = ref.get("host")
            try:
                resolved = resolve.resolve_path(path, effective_root)
            except resolve.RefResolutionError as exc:
                notes.append(f"{doc}: skipped unresolvable ref {path!r}: {exc}")
                continue
            _add_candidate(
                candidates, resolved.canonical, host if isinstance(host, str) else None
            )


# --- the section-8 algorithm -------------------------------------------------


def _discover_local(
    scope: str,
    project_root: Path,
    *,
    project_name: str | None = None,
    target: str | None = None,
    user_root: Path | None = None,
    local_host: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    notes: list[str] | None = None,
) -> list[TaskRecord]:
    """Enumerate the tasks in a scope (spec 8). Returns records sorted by id.

    ``status``/``priority`` filter the projection (spec 8 step 5); with no
    ``status`` every classification except ``archived`` is emitted. Explicit
    ``status`` values list any classification, including ``archived``.
    ``local_host`` overrides host detection (injectable for tests);
    ``notes``, when given, accumulates skip-with-note findings.
    """
    if notes is None:
        notes = []
    if scope not in LOCAL_SCOPES:
        raise DiscoveryError(
            f"unknown scope {scope!r} (expected one of: " + ", ".join(SCOPES) + ")"
        )
    if scope in ("skill", "file") and target is None:
        raise DiscoveryError(f"scope {scope!r} requires a target")

    effective_root = (
        (user_root if user_root is not None else DEFAULT_USER_ROOT)
        if scope == "user"
        else project_root
    )

    # Step 1: scope -> roots + document set. Step 2: collect candidates.
    docs = _scope_docs(scope, effective_root, project_root, target)
    candidates: dict[str, str | None] = {}
    _crawl_folders(effective_root, candidates, notes)
    for doc in docs:
        _scan_doc(doc, effective_root, candidates, notes)

    # Steps 3-5: dedupe happened in the candidate map; classify via validate,
    # project, filter.
    records: list[TaskRecord] = []
    for canonical, host in sorted(candidates.items()):
        result = validate_ref(
            canonical, effective_root, ref_host=host, local_host=local_host
        )
        title: str | None = None
        prio: str | None = None
        last_update: str | None = None
        if result.classification != "remote":  # remote is opaque: never read
            folder = effective_root / canonical
            if folder.is_dir():
                block = read_task_block(folder)
                if block is not None:
                    raw_title = block.get("title")
                    raw_prio = block.get("priority")
                    title = raw_title if isinstance(raw_title, str) else None
                    prio = raw_prio if isinstance(raw_prio, str) else None
                last_update = read_last_update(folder)
        record = TaskRecord(
            id=canonical,
            classification=result.classification,
            title=title,
            priority=prio,
            last_update=last_update,
            host=host,
            project_name=project_name or effective_root.name,
            project_root=effective_root,
        )
        if status is None:
            if record.classification == "archived":
                continue  # working set by default; --status archived opts in
        elif record.classification != status:
            continue
        if priority is not None and record.priority != priority:
            continue
        records.append(record)
    return records


def configured_project_directories(config_path: Path | None = None) -> tuple[Path, ...]:
    """Read user-configured parents for multi-project discovery."""
    path = config_path if config_path is not None else TASK_CONFIG_PATH
    if not path.exists():
        return ()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DiscoveryError(f"cannot read task config {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("project_directories"), list):
        raise DiscoveryError(f"{path}: project_directories must be a list of paths")

    def expand_reference(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        value = os.environ.get(name)
        if not value:
            raise DiscoveryError(f"{path}: environment variable {name} is not set")
        return value

    directories: list[Path] = []
    for raw in data["project_directories"]:
        if not isinstance(raw, str) or not raw.strip():
            raise DiscoveryError(f"{path}: project_directories entries must be nonempty strings")
        candidate = Path(_ENV_REFERENCE_RE.sub(expand_reference, raw)).expanduser()
        if not candidate.is_absolute() or not candidate.is_dir():
            raise DiscoveryError(f"{path}: project directory is not an existing absolute directory: {candidate}")
        directories.append(candidate.resolve())
    return tuple(dict.fromkeys(directories))


def discover_project_roots(parents: tuple[Path, ...]) -> tuple[ProjectRoot, ...]:
    """Return direct child directories of configured parents in stable order."""
    entries: dict[Path, Path] = {}
    for parent in parents:
        try:
            for entry in parent.iterdir():
                if entry.is_dir() and not entry.name.startswith("."):
                    entries.setdefault(entry.resolve(), entry)
        except OSError as exc:
            raise DiscoveryError(f"cannot read project directory {parent}: {exc}") from exc
    return tuple(
        ProjectRoot(name=entry.name, root=entry)
        for entry in sorted(entries.values(), key=lambda path: (path.name.casefold(), path.name, str(path)))
    )


def _record_identity(record: TaskRecord) -> tuple[str, ...]:
    """Identify a physical task folder, preserving absent references separately."""
    root = record.project_root
    if root is None:
        return ("record", record.id, record.host or "")
    folder = root / record.id
    if folder.is_dir():
        try:
            return ("folder", os.path.normcase(os.path.normpath(str(folder.resolve()))))
        except OSError:
            pass
    return ("reference", os.path.normcase(os.path.normpath(str(root.resolve()))), record.id, record.host or "")


def _project_preference(record: TaskRecord) -> tuple[int, str, str, str]:
    """Prefer the shortest project name when a physical task is shared."""
    return (
        len(record.project_name),
        record.project_name.casefold(),
        record.project_name,
        str(record.project_root or ""),
    )


def discover(
    scope: str,
    project_root: Path,
    *,
    target: str | None = None,
    user_root: Path | None = None,
    local_host: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    notes: list[str] | None = None,
    devroot: Path | None = None,
) -> list[TaskRecord]:
    """Enumerate one project or projects under configured parent directories."""
    if scope != "all":
        return _discover_local(
            scope,
            project_root,
            target=target,
            user_root=user_root,
            local_host=local_host,
            status=status,
            priority=priority,
            notes=notes,
            project_name=(
                (user_root if user_root is not None else DEFAULT_USER_ROOT).name
                if scope == "user"
                else project_root.name
            ),
        )
    if target is not None:
        raise DiscoveryError("scope 'all' does not accept a target")

    collected: list[TaskRecord] = []
    output_notes = notes if notes is not None else []
    parents = (devroot,) if devroot is not None else configured_project_directories()
    if not parents:
        raise DiscoveryError(f"scope 'all' requires project_directories in {TASK_CONFIG_PATH}")
    for project in discover_project_roots(parents):
        project_notes: list[str] = []
        collected.extend(
            _discover_local(
                "project",
                project.root,
                local_host=local_host,
                status=status,
                priority=priority,
                notes=project_notes,
                project_name=project.name,
            )
        )
        output_notes.extend(
            f"{project.name}: {message}" for message in project_notes
        )

    selected: dict[tuple[str, ...], TaskRecord] = {}
    for record in collected:
        identity = _record_identity(record)
        current = selected.get(identity)
        if current is None or _project_preference(record) < _project_preference(current):
            selected[identity] = record
    return sorted(
        selected.values(),
        key=lambda record: (
            record.project_name.casefold(),
            record.project_name,
            record.id,
        ),
    )
