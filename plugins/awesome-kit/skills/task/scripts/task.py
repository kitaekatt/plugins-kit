"""task.py -- the task-system CLI entry point (spec section 7).

One entry point with verb subcommands. The CLI exposes 13 verbs:
``init``, ``list``, ``show``, ``status``, ``validate``,
``work``, ``update``, ``items``, ``close``, ``reopen``,
``archive``, ``delete``, and ``move``.

Conventions (spec 7.1): exit 0 on success, non-zero on failure/block;
findings print to stderr. ``validate`` exits 0 iff there are no errors AND no
warnings; the classification prints to stdout. Advisory ``note:`` lines
(the document-size approaching-budget / dominant-section / session-diary
signals) also print to stderr but are NOT findings -- they never affect the
exit code. ``init`` prints the created
folder path to stdout on success; on failure the reason/findings go to
stderr (and no partial folder is left behind).

Read-op conventions (Step 3):
- ``list`` prints one stable, parseable line per task --
  ``id  status  priority  title`` (two-space separated; absent fields ``-``;
  remote tasks as ``<path> @<host>  remote  -  -``, status not locally
  resolvable). Discovery notes go to stderr. Exit 0 even when empty.
- ``show <ref>`` prints the selected task.yaml fields; non-zero with a
  reason on stderr when the ref is unresolvable or the folder is not
  readable locally (archived / orphaned / remote).
- ``status <ref>`` is the spec's one INFERENCE verb (spec 7.1): a background
  agent summarizes the task. The script side implemented here is the
  SUBSTRATE ONLY -- classification + findings + the raw material
  (task.yaml fields, document paths, the parsed task_items). The
  summarization itself is dispatched by the skill layer (Step 6), not by
  this script.
- ``items <ref>`` enumerates the task's open items (the plan.md
  ``task_items`` unit; design/task-items-design.md section 8): one parseable
  line per item -- ``id  state  priority  title`` (two-space separated,
  absent priority ``-``), sorted by priority then block order;
  ``--state``/``--priority`` filter.
  Findings about the block go to stderr as notes (validate is the gate that
  reports them as findings); exit 0 even when empty. Non-zero with a reason
  only when the ref is unresolvable or the folder is not readable locally
  (archived / orphaned / remote) -- matching ``show``.

State-op conventions (Step 4):
- ``work <ref>`` exits non-zero when validate blocks (ANY error or warning),
  the ref is remote, or auto-init fails -- findings to stderr. On pass it
  prints to stdout a single
  initialization block: a ``== task init ... ==`` header, one
  ``Skill(skill: "<name>")`` line per merged skill
  (``state_ops.BASELINE_SKILLS`` then the task's own ``skills_to_invoke``,
  deduped), an ``agent_hint: <name>`` dispatch hint line when present, and
  a closing ``== then: dispatch ... ==`` directive (the skill layer acts on
  these; the script only emits them).
- ``update <ref>`` applies the explicit ref. Prints the
  re-validation classification to stdout and findings to stderr; exits 0 iff
  there are no findings -- but the field edits persist regardless (update is
  a write op; validate reports). List-valued flags (``--depends-on``,
  ``--blocked-by``, ``--skill-to-invoke``, ``--durable-output``) are
  repeatable and REPLACE the stored list (no append/remove micro-ops in v1).
  ``--durable-output`` declares a document that OUTLIVES the task, by its
  path in the owning repo; archive verifies each declaration still resolves
  outside the folder it is about to park or delete.
- ``close <ref>`` prints ``closed: <id>``; ``reopen <ref>`` prints the
  re-validation classification (exit 0 iff no findings, like ``update``).

Location-op conventions (Step 5):
- ``archive <ref>`` prints ``archived: <id>`` plus the closure-policy
  disposition. tmp -> folder moved to tmp/archived-tasks/<stub>. Non-tmp:
  version control is the record; in a git repo the final state is committed,
  the folder deleted, and the removal committed (never removing the folder
  before its final state is committed); outside a git repo NO git command
  runs -- the final state is recorded, the folder kept, and submission via
  the workspace's VCS (e.g. p4) plus the finishing delete are left to the
  agent/user. Non-zero with the refusal reason on stderr when the task is
  not active (closed -> reopen first), the folder is missing, the tmp
  parking spot is occupied, or a git command fails.
- ``delete <ref>`` prints ``deleted: <id>``; accepts an active OR archived
  task, refuses a dev/tasks folder git can see is dirty (delete never
  auto-commits; outside a git repo the agent owns VCS state), then the
  folder is removed even when tmp.
- ``move <ref> <dest>`` (dest: ``tmp`` or ``dev/tasks``; the stub is
  preserved) prints ``moved: <old> -> <new>`` and the rewritten-document
  count. Non-zero when the source folder is absent or the destination
  already exists (nothing changed).

Usage:
    task.py validate <ref> [--root PATH]
    task.py init <stub|desc> [--dest tmp|dev/tasks] [--type hand-off] [--root PATH]
    task.py list [--scope user|project|skill|file] [--target X]
                 [--status S] [--priority P] [--root PATH]
    task.py show <ref> [--root PATH]
    task.py items <ref> [--state S] [--priority P] [--root PATH]
    task.py status <ref> [--root PATH]
    task.py work <ref> [--root PATH]
    task.py update <ref> [--status S] [--priority P] [--description D]
                   [--depends-on PATH ...] [--blocked-by PATH ...]
                   [--agent-hint H] [--skill-to-invoke NAME ...]
                   [--durable-output PATH ...] [--root PATH]
    task.py close <ref> [--root PATH]
    task.py reopen <ref> [--root PATH]
    task.py archive <ref> [--root PATH]
    task.py delete <ref> [--root PATH]
    task.py move <ref> <dest> [--root PATH]
"""

import argparse
import sys
from pathlib import Path

# Plugins define their own bootstrap-provisioned venv and must run under it
# preferentially: skills_kit_lib is linked onto awesome-kit's venv by the
# bootstrap shared-libs .pth ("shared_lib_imports": ["skills_kit_lib"]), so a
# bare `python` / `uv run` invocation would miss it. Re-exec under the
# provisioned venv BEFORE the task_system import below (which imports
# skills_kit_lib) -- a no-op when already there. The guard is the vendored,
# stdlib-only bootstrap_guard next to this script; importing it can never
# itself trip the missing-shared-lib failure. See plugins/CLAUDE.md.
from bootstrap_guard import reexec_under_plugin_venv  # noqa: E402

reexec_under_plugin_venv("awesome-kit")

try:
    from task_system import location_ops  # noqa: E402
    from task_system import resolve  # noqa: E402
    from task_system import state_ops  # noqa: E402
    from task_system.discovery import (  # noqa: E402
        DiscoveryError,
        discover,
        read_task_block,
    )
    from task_system.init import InitError, init_task  # noqa: E402
    from task_system.state_ops import StateOpError  # noqa: E402
    from task_system.task_items import read_task_items, sort_items  # noqa: E402
    from task_system.types import DEFAULT_TYPE_NAME, get_type  # noqa: E402
    from task_system.validate import validate_ref  # noqa: E402
except ImportError:
    # Safety net for the installed-but-not-yet-provisioned window: the failed
    # import is itself proof the provisioned venv (shared libs + pyyaml) is
    # not available here.
    from bootstrap_guard import require_bootstrap

    require_bootstrap(
        "awesome-kit",
        feature="task system",
        missing="skills_kit_lib/pyyaml",
        force=True,
    )


def _cmd_validate(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    result = validate_ref(args.ref, root)
    print(result.classification)
    _print_findings(result)
    return 0 if result.clean else 1


def _print_findings(result) -> None:
    """Findings (errors + warnings) then advisory notes, all to stderr.
    Notes are not findings: they never affect the exit code."""
    for msg in result.errors:
        print(f"error: {msg}", file=sys.stderr)
    for msg in result.warnings:
        print(f"warning: {msg}", file=sys.stderr)
    for msg in result.notes:
        print(f"note: {msg}", file=sys.stderr)


def _cmd_init(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        folder = init_task(
            args.stub_or_desc, root, dest=args.dest, task_type=args.task_type
        )
    except InitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(folder)
    return 0


def _render_value(value: object) -> str:
    """Render one task.yaml field for line output: absent/empty -> ``-``;
    lists comma-joined; multi-line strings continuation-indented."""
    if value is None:
        return "-"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "-"
    text = str(value)
    if "\n" in text:
        lines = text.rstrip("\n").split("\n")
        return "\n  ".join(lines)
    return text


def _format_list_line(rec) -> str:
    ident = rec.id
    if rec.classification == "remote" and rec.host:
        ident = f"{rec.id} @{rec.host}"
    return "  ".join(
        [ident, rec.classification, rec.priority or "-", rec.title or "-"]
    )


def _cmd_list(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    notes: list[str] = []
    try:
        records = discover(
            args.scope,
            root,
            target=args.target,
            status=args.status,
            priority=args.priority,
            notes=notes,
        )
    except DiscoveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    for rec in records:
        print(_format_list_line(rec))
    return 0


_SHOW_FIELDS = (
    "type",
    "title",
    "status",
    "priority",
    "description",
    "depends_on",
    "blocked_by",
    "agent_hint",
    "skills_to_invoke",
    "durable_outputs",
)


def _cmd_show(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        resolved = resolve.resolve_ref(args.ref, root)
    except resolve.RefResolutionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    folder = resolved.folder(root)
    if not folder.is_dir():
        print(
            f"error: {resolved.canonical}: no task folder readable locally "
            "(archived, orphaned, or remote)",
            file=sys.stderr,
        )
        return 1
    block = read_task_block(folder)
    if block is None:
        print(
            f"error: {resolved.canonical}/task.yaml is missing, unparseable, "
            "or mis-shaped -- run validate",
            file=sys.stderr,
        )
        return 1
    print(f"id: {resolved.canonical}")
    for field in _SHOW_FIELDS:
        print(f"{field}: {_render_value(block.get(field))}")
    return 0


def _items_type(folder: Path):
    """The registered type governing a folder's task_items vocabulary; falls
    back to the default type for a missing/unknown ``type`` so items still
    render (the type finding itself is validate's job)."""
    block = read_task_block(folder) or {}
    type_name = block.get("type")
    ttype = get_type(type_name) if isinstance(type_name, str) else None
    if ttype is None:
        ttype = get_type(DEFAULT_TYPE_NAME)
    assert ttype is not None
    return ttype


def _format_item_line(item) -> str:
    return "  ".join([item.id, item.state, item.priority or "-", item.title])


def _cmd_items(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        resolved = resolve.resolve_ref(args.ref, root)
    except resolve.RefResolutionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    canonical = resolved.canonical
    folder = resolved.folder(root)
    if not folder.is_dir():
        print(
            f"error: {canonical}: no task folder readable locally "
            "(archived, orphaned, or remote)",
            file=sys.stderr,
        )
        return 1
    result = read_task_items(folder, _items_type(folder))
    for msg in result.errors:
        print(f"note: {msg}", file=sys.stderr)
    if not result.block_found:
        print(
            f"note: no task_items block in {canonical}/plan.md -- "
            "pre-contract folder; run the one-time conversion "
            "(handoff-template.md, 'Converting a pre-contract folder')",
            file=sys.stderr,
        )
    items = sort_items(result.items)
    if args.state is not None:
        items = [it for it in items if it.state == args.state]
    if args.priority is not None:
        items = [it for it in items if it.priority == args.priority]
    for item in items:
        print(_format_item_line(item))
    return 0


def _print_state_op_error(exc: StateOpError) -> None:
    print(f"error: {exc}", file=sys.stderr)
    for msg in exc.errors:
        print(f"error: {msg}", file=sys.stderr)
    for msg in exc.warnings:
        print(f"warning: {msg}", file=sys.stderr)


def _emit_work(result) -> None:
    """The spec 7.1 work output, framed as ONE initialization block: every
    skill the task needs (baseline + declared, merged by state_ops) as
    Skill(...) lines, the dispatch hint, and the closing directive. The
    skill layer acts on these; the script only emits them.

    The framing lines are load-bearing, not decoration. A single emitted
    list makes the rule "invoke every Skill(...) line printed" -- no
    cross-referencing prose for a requirement the script did not print --
    and the closing directive is what turns a loaded orchestrate skill into
    an actual dispatch (the observed miss was invoking the task's own
    skills and then implementing inline anyway)."""
    print("== task init -- invoke each of these now, one Skill call each ==")
    for skill in result.skills_to_invoke:
        print(f'Skill(skill: "{skill}")')
    if result.agent_hint is not None:
        print(f"agent_hint: {result.agent_hint}")
    print(
        "== then: dispatch the work per orchestrate -- "
        "do not implement inline in the main context =="
    )


def _cmd_work(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        result = state_ops.work(args.ref, root)
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    _emit_work(result)
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        result = state_ops.update(
            args.ref,
            root,
            status=args.status,
            priority=args.priority,
            description=args.description,
            depends_on=args.depends_on,
            blocked_by=args.blocked_by,
            agent_hint=args.agent_hint,
            skills_to_invoke=args.skills_to_invoke,
            durable_outputs=args.durable_outputs,
        )
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    validation = result.validation
    print(validation.classification)
    _print_findings(validation)
    # The write persisted either way; the exit code reports the findings
    # (consistent with the validate verb).
    return 0 if validation.clean else 1


def _cmd_close(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        canonical = state_ops.close(args.ref, root)
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    print(f"closed: {canonical}")
    return 0


def _cmd_reopen(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        result = state_ops.reopen(args.ref, root)
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    validation = result.validation
    print(validation.classification)
    _print_findings(validation)
    return 0 if validation.clean else 1


def _cmd_archive(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        result = location_ops.archive_task(args.ref, root)
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    if result.folder_removed:
        disposition = (
            "final state committed; folder deleted; version control is the "
            "record"
        )
    elif result.vcs_pending:
        disposition = (
            "final state recorded; folder kept -- not a git workspace, so "
            "submit it with your version control (e.g. p4 submit), then run "
            "delete"
        )
    else:
        disposition = f"moved to {result.archived_to}, status: archived"
    print(f"archived: {result.canonical} ({disposition})")
    if result.durable_note is not None:
        print(f"note: {result.durable_note}", file=sys.stderr)
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        canonical = location_ops.delete_task(args.ref, root)
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    print(f"deleted: {canonical}")
    return 0


def _cmd_move(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        result = location_ops.move_task(
            args.ref, args.dest, root
        )
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    print(f"moved: {result.old_canonical} -> {result.new_canonical}")
    print(f"rewrote {len(result.rewritten_docs)} document(s)")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    # INFERENCE verb, script-side substrate only (see module docstring): the
    # summarization over this material is dispatched by the skill layer
    # (Step 6). This prints classification + findings + the raw material.
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    result = validate_ref(args.ref, root)
    print(f"classification: {result.classification}")
    print(f"id: {result.canonical or '-'}")
    if result.errors or result.warnings:
        print("findings:")
        for msg in result.errors:
            print(f"  error: {msg}")
        for msg in result.warnings:
            print(f"  warning: {msg}")
    else:
        print("findings: none")
    if result.notes:
        print("notes:")
        for msg in result.notes:
            print(f"  note: {msg}")
    if result.canonical is not None and result.classification != "remote":
        folder = root / result.canonical
        if folder.is_dir():
            block = read_task_block(folder)
            if block is not None:
                print("task.yaml:")
                for key, value in block.items():
                    print(f"  {key}: {_render_value(value)}")
            print("documents:")
            for fname in ("CLAUDE.md", "plan.md", "log.md"):
                fpath = folder / fname
                suffix = "" if fpath.is_file() else "  (missing)"
                print(f"  {fname}: {fpath}{suffix}")
            # The parsed task_items menu (design section 8): part of the
            # substrate so the summarizer leads with it without re-parsing.
            # Its findings already surface above via validate.
            items_result = read_task_items(folder, _items_type(folder))
            if not items_result.block_found:
                print("items: no task_items block in plan.md (pre-contract)")
            elif not items_result.items:
                print("items: none (empty task_items block)")
            else:
                print("items:")
                for item in sort_items(items_result.items):
                    print(f"  {_format_item_line(item)}")
    print(
        "note: status is an inference verb -- summarization of this material "
        "is dispatched by the skill layer (Step 6); this output is the "
        "script-side substrate only."
    )
    return 0 if result.canonical is not None else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="task", description="Task-system CLI (see SKILL design spec)."
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_validate = sub.add_parser(
        "validate", help="Check a task against its type schema; emit findings."
    )
    p_validate.add_argument(
        "ref", help="Task path (tmp/<stub> or dev/tasks/<stub>) or bare stub."
    )
    p_validate.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root the ref is relative to (default: cwd).",
    )

    p_init = sub.add_parser(
        "init", help="Create the folder + scaffolding for a new task."
    )
    p_init.add_argument(
        "stub_or_desc",
        help="Folder stub (kebab/safe chars) or a freeform description.",
    )
    p_init.add_argument(
        "--dest",
        choices=["tmp", "dev/tasks"],
        default="tmp",
        help="Location for the task folder (default: tmp).",
    )
    p_init.add_argument(
        "--type",
        dest="task_type",
        default="hand-off",
        help="Registered task type (default: hand-off).",
    )
    p_init.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root the folder is created under (default: cwd).",
    )

    p_list = sub.add_parser(
        "list", help="Enumerate tasks in a scope (folder crawl + reference scan)."
    )
    p_list.add_argument(
        "--scope",
        choices=["user", "project", "skill", "file"],
        default="project",
        help="Discovery scope (default: project).",
    )
    p_list.add_argument(
        "--target",
        default=None,
        help="Scope target: skill name-or-path (scope skill) or document "
        "path (scope file).",
    )
    p_list.add_argument(
        "--status", default=None, help="Only tasks with this status/classification."
    )
    p_list.add_argument(
        "--priority", default=None, help="Only tasks with this priority."
    )
    p_list.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root (default: cwd).",
    )

    p_show = sub.add_parser(
        "show", help="Print one task's selected task.yaml fields (no inference)."
    )
    p_show.add_argument(
        "ref", help="Task path (tmp/<stub> or dev/tasks/<stub>) or bare stub."
    )
    p_show.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root the ref is relative to (default: cwd).",
    )

    p_items = sub.add_parser(
        "items",
        help="Enumerate the task's open items (the plan.md task_items unit): "
        "one line per item -- id  state  priority  title -- sorted by "
        "priority then block order.",
    )
    p_items.add_argument(
        "ref",
        help="Task path (tmp/<stub> or dev/tasks/<stub>) or bare stub.",
    )
    p_items.add_argument(
        "--state",
        default=None,
        help="Only items in this state (available | in-flight | "
        "blocked-user | deferred).",
    )
    p_items.add_argument(
        "--priority", default=None, help="Only items with this priority."
    )
    p_items.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root the ref is relative to (default: cwd).",
    )
    p_status = sub.add_parser(
        "status",
        help="Print the script-side substrate for the status summary: "
        "classification, findings, and raw material (task.yaml fields, "
        "document paths). status is an INFERENCE verb -- the summarization "
        "itself is dispatched by the skill layer (Step 6), not this script.",
    )
    p_status.add_argument(
        "ref", help="Task path (tmp/<stub> or dev/tasks/<stub>) or bare stub."
    )
    p_status.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root the ref is relative to (default: cwd).",
    )

    def add_state_op_parser(name: str, help_text: str):
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "ref",
            help="Task path (tmp/<stub> or dev/tasks/<stub>) or bare stub.",
        )
        p.add_argument(
            "--root",
            type=Path,
            default=None,
            help="Project root the ref is relative to (default: cwd).",
        )
        return p

    add_state_op_parser(
        "work",
        "Prepare the task for work (auto-init when the folder is absent; "
        "gated by validate -- errors AND warnings block).",
    )
    p_update = add_state_op_parser(
        "update",
        "Upsert + refresh: init when absent, apply task.yaml field edits, "
        "append the dated log.md entry, re-validate.",
    )
    p_update.add_argument(
        "--status", default=None, help="Set task.status (type vocabulary)."
    )
    p_update.add_argument(
        "--priority", default=None, help="Set task.priority (e.g. P1)."
    )
    p_update.add_argument(
        "--description", default=None, help="Set task.description."
    )
    p_update.add_argument(
        "--depends-on",
        dest="depends_on",
        action="append",
        default=None,
        metavar="PATH",
        help="Set a depends_on entry (repeatable; REPLACES the stored list).",
    )
    p_update.add_argument(
        "--blocked-by",
        dest="blocked_by",
        action="append",
        default=None,
        metavar="PATH",
        help="Set a blocked_by entry (repeatable; REPLACES the stored list).",
    )
    p_update.add_argument(
        "--agent-hint", dest="agent_hint", default=None, help="Set task.agent_hint."
    )
    p_update.add_argument(
        "--skill-to-invoke",
        dest="skills_to_invoke",
        action="append",
        default=None,
        metavar="NAME",
        help="Set a skills_to_invoke entry (repeatable; REPLACES the stored "
        "list).",
    )
    p_update.add_argument(
        "--durable-output",
        dest="durable_outputs",
        action="append",
        default=None,
        metavar="PATH",
        help="Declare a document this task produced that OUTLIVES it: a "
        "repo-relative path in the owning repo, outside the task folder "
        "(repeatable; REPLACES the stored list). archive verifies each one "
        "still has a home.",
    )
    add_state_op_parser(
        "close",
        "Mark an active task closed; keep the folder.",
    )
    add_state_op_parser(
        "reopen",
        "Reverse a terminal state back to active (the folder must still "
        "exist); re-validate.",
    )
    add_state_op_parser(
        "archive",
        "Archive an active task per the closure policy: tmp -> status "
        "archived, folder moved to tmp/archived-tasks/<stub>; non-tmp -> "
        "version control is the record (git repo: commit final state, "
        "delete folder, commit removal; other/no VCS: record final state, "
        "keep folder for the agent to submit + delete).",
    )
    add_state_op_parser(
        "delete",
        "Remove the folder even when tmp (unconditional removal). Accepts "
        "an active or archived task; refuses a dev/tasks folder git can "
        "see is dirty (never auto-commits).",
    )
    p_move = add_state_op_parser(
        "move",
        "Relocate the task folder to the other location root and rewrite "
        "every task_list reference to the new path.",
    )
    p_move.add_argument(
        "dest",
        choices=["tmp", "dev/tasks"],
        help="Destination location root (the stub is preserved).",
    )

    args = parser.parse_args(argv)
    if args.verb == "validate":
        return _cmd_validate(args)
    if args.verb == "init":
        return _cmd_init(args)
    if args.verb == "list":
        return _cmd_list(args)
    if args.verb == "show":
        return _cmd_show(args)
    if args.verb == "items":
        return _cmd_items(args)
    if args.verb == "status":
        return _cmd_status(args)
    if args.verb == "work":
        return _cmd_work(args)
    if args.verb == "update":
        return _cmd_update(args)
    if args.verb == "close":
        return _cmd_close(args)
    if args.verb == "reopen":
        return _cmd_reopen(args)
    if args.verb == "archive":
        return _cmd_archive(args)
    if args.verb == "delete":
        return _cmd_delete(args)
    if args.verb == "move":
        return _cmd_move(args)
    parser.error(f"unknown verb: {args.verb}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
