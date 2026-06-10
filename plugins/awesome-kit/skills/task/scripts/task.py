"""task.py -- the task-system CLI entry point (spec section 7).

One entry point with verb subcommands. Steps 1-5 ship ``validate``, ``init``,
the read ops ``list`` / ``show`` / ``current`` / ``status``, the state ops
``work`` / ``switch`` / ``update`` / ``close`` / ``reopen``, and the
destructive + location ops ``archive`` / ``delete`` / ``move``.

Conventions (spec 7.1): exit 0 on success, non-zero on failure/block;
findings print to stderr. ``validate`` exits 0 iff there are no errors AND no
warnings; the classification prints to stdout. ``init`` prints the created
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
- ``current`` reads the global pointer. The pointer stores the ABSOLUTE
  folder path (spec 2.6), so it is self-resolving -- ``--root`` is accepted
  for interface stability but not needed; the project-relative id printed is
  derived from the stored path. Stale content (missing folder, or a line
  that is not a derivable absolute task path) is cleared and "none" is
  reported (exit 0, spec 2.6).
- ``status <ref>`` is the spec's one INFERENCE verb (spec 7.1): a background
  agent summarizes the task. The script side implemented here is the
  SUBSTRATE ONLY -- classification + findings + the raw material
  (task.yaml fields, document paths). The summarization itself is
  dispatched by the skill layer (Step 6), not by this script.

State-op conventions (Step 4):
- ``work <ref>`` exits non-zero when validate blocks (ANY error or warning),
  the ref is remote, or auto-init fails -- findings to stderr, pointer
  unwritten. On pass it writes the pointer and prints to stdout one
  ``Skill(skill: "<name>")`` line per ``skills_to_invoke`` entry plus an
  ``agent_hint: <name>`` dispatch hint line when present (the skill layer
  acts on these; the script only emits them).
- ``switch <ref>`` first runs ``update`` on the current task when one is
  set and extant (a ``note:`` line on stderr reports it), then behaves
  exactly like ``work <ref>``.
- ``update [<ref>]`` defaults the ref to the current task. Prints the
  re-validation classification to stdout and findings to stderr; exits 0 iff
  there are no findings -- but the field edits persist regardless (update is
  a write op; validate reports). List-valued flags (``--depends-on``,
  ``--blocked-by``, ``--skill-to-invoke``) are repeatable and REPLACE the
  stored list (no append/remove micro-ops in v1).
- ``close <ref>`` prints ``closed: <id>``; ``reopen <ref>`` prints the
  re-validation classification (exit 0 iff no findings, like ``update``).

Location-op conventions (Step 5):
- ``archive <ref>`` prints ``archived: <id>`` plus the closure-policy
  disposition (tmp folder kept vs folder deleted). Non-zero with the refusal
  reason on stderr when the task is not active (closed -> reopen first), the
  folder is missing, or a non-tmp folder is uncommitted / not in a git repo
  (commit first; git is the record -- no auto-commit).
- ``delete <ref>`` prints ``deleted: <id>``; same preconditions and
  uncommitted guard as archive (the documented Step 5 reading), then the
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
    task.py current [--root PATH] [--pointer PATH]
    task.py status <ref> [--root PATH]
    task.py work <ref> [--root PATH] [--pointer PATH]
    task.py switch <ref> [--root PATH] [--pointer PATH]
    task.py update [<ref>] [--status S] [--priority P] [--description D]
                   [--depends-on PATH ...] [--blocked-by PATH ...]
                   [--agent-hint H] [--skill-to-invoke NAME ...]
                   [--root PATH] [--pointer PATH]
    task.py close <ref> [--root PATH] [--pointer PATH]
    task.py reopen <ref> [--root PATH] [--pointer PATH]
    task.py archive <ref> [--root PATH] [--pointer PATH]
    task.py delete <ref> [--root PATH] [--pointer PATH]
    task.py move <ref> <dest> [--root PATH] [--pointer PATH]
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
    from task_system import pointer as pointer_mod  # noqa: E402
    from task_system import resolve  # noqa: E402
    from task_system import state_ops  # noqa: E402
    from task_system.discovery import (  # noqa: E402
        DiscoveryError,
        discover,
        read_task_block,
    )
    from task_system.init import InitError, init_task  # noqa: E402
    from task_system.state_ops import StateOpError  # noqa: E402
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
    for msg in result.errors:
        print(f"error: {msg}", file=sys.stderr)
    for msg in result.warnings:
        print(f"warning: {msg}", file=sys.stderr)
    return 0 if result.clean else 1


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


def _pointer_path(args: argparse.Namespace) -> Path:
    return (
        args.pointer if args.pointer is not None else pointer_mod.DEFAULT_POINTER_PATH
    )


def _cmd_current(args: argparse.Namespace) -> int:
    # The pointer stores the ABSOLUTE folder path (spec 2.6), so it is
    # self-resolving: the project root and the project-relative id both
    # derive from the stored path. --root stays accepted but unused.
    pointer_path = _pointer_path(args)
    current = pointer_mod.read_current(pointer_path)
    if current is None:
        print("none")
        return 0
    folder = Path(current)
    derived = state_ops.derive_root_and_canonical(folder)
    if derived is None or not folder.is_dir():
        # Stale pointer (spec 2.6): missing folder, or content that is not a
        # derivable absolute task path -- clear and report none, no error.
        pointer_mod.clear_current(pointer_path)
        print("none")
        return 0
    derived_root, canonical = derived
    result = validate_ref(canonical, derived_root)
    block = read_task_block(folder) or {}
    title = block.get("title")
    print(
        "  ".join(
            [
                canonical,
                result.classification,
                title if isinstance(title, str) and title else "-",
            ]
        )
    )
    return 0


def _print_state_op_error(exc: StateOpError) -> None:
    print(f"error: {exc}", file=sys.stderr)
    for msg in exc.errors:
        print(f"error: {msg}", file=sys.stderr)
    for msg in exc.warnings:
        print(f"warning: {msg}", file=sys.stderr)


def _emit_work(result) -> None:
    """The spec 7.1 work output: Skill(...) lines + the dispatch hint. The
    skill layer acts on these; the script only emits them."""
    for skill in result.skills_to_invoke:
        print(f'Skill(skill: "{skill}")')
    if result.agent_hint is not None:
        print(f"agent_hint: {result.agent_hint}")


def _cmd_work(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        result = state_ops.work(args.ref, root, _pointer_path(args))
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    _emit_work(result)
    return 0


def _cmd_switch(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        result = state_ops.switch(args.ref, root, _pointer_path(args))
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    if result.previous is not None:
        print(
            f"note: updated previous current {result.previous.canonical} "
            f"({result.previous.validation.classification})",
            file=sys.stderr,
        )
    _emit_work(result.work)
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        result = state_ops.update(
            args.ref,
            root,
            _pointer_path(args),
            status=args.status,
            priority=args.priority,
            description=args.description,
            depends_on=args.depends_on,
            blocked_by=args.blocked_by,
            agent_hint=args.agent_hint,
            skills_to_invoke=args.skills_to_invoke,
        )
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    validation = result.validation
    print(validation.classification)
    for msg in validation.errors:
        print(f"error: {msg}", file=sys.stderr)
    for msg in validation.warnings:
        print(f"warning: {msg}", file=sys.stderr)
    # The write persisted either way; the exit code reports the findings
    # (consistent with the validate verb).
    return 0 if validation.clean else 1


def _cmd_close(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        canonical = state_ops.close(args.ref, root, _pointer_path(args))
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    print(f"closed: {canonical}")
    return 0


def _cmd_reopen(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        result = state_ops.reopen(args.ref, root, _pointer_path(args))
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    validation = result.validation
    print(validation.classification)
    for msg in validation.errors:
        print(f"error: {msg}", file=sys.stderr)
    for msg in validation.warnings:
        print(f"warning: {msg}", file=sys.stderr)
    return 0 if validation.clean else 1


def _cmd_archive(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        result = location_ops.archive_task(args.ref, root, _pointer_path(args))
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    disposition = (
        "folder deleted; git is the record"
        if result.folder_removed
        else "tmp folder kept, status: archived"
    )
    print(f"archived: {result.canonical} ({disposition})")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        canonical = location_ops.delete_task(args.ref, root, _pointer_path(args))
    except StateOpError as exc:
        _print_state_op_error(exc)
        return 1
    print(f"deleted: {canonical}")
    return 0


def _cmd_move(args: argparse.Namespace) -> int:
    root = (args.root if args.root is not None else Path.cwd()).resolve()
    try:
        result = location_ops.move_task(
            args.ref, args.dest, root, _pointer_path(args)
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

    p_current = sub.add_parser(
        "current", help="Report the single global current task (read the pointer)."
    )
    p_current.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Accepted for interface stability; the pointer stores an "
        "absolute path and is self-resolving (spec 2.6).",
    )
    p_current.add_argument(
        "--pointer",
        type=Path,
        default=None,
        help="Pointer file location (default: the user-global pointer).",
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

    def add_state_op_parser(name: str, help_text: str, *, ref_optional: bool = False):
        p = sub.add_parser(name, help=help_text)
        if ref_optional:
            p.add_argument(
                "ref",
                nargs="?",
                default=None,
                help="Task path or bare stub (default: the current task).",
            )
        else:
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
        p.add_argument(
            "--pointer",
            type=Path,
            default=None,
            help="Pointer file location (default: the user-global pointer).",
        )
        return p

    add_state_op_parser(
        "work",
        "Set the task as the single global current task (auto-init when the "
        "folder is absent; gated by validate -- errors AND warnings block).",
    )
    add_state_op_parser(
        "switch",
        "Update the current task (it becomes a plain active task), then "
        "work the given ref.",
    )
    p_update = add_state_op_parser(
        "update",
        "Upsert + refresh: init when absent, apply task.yaml field edits, "
        "append the dated log.md entry, re-validate.",
        ref_optional=True,
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
    add_state_op_parser(
        "close",
        "Mark an active task closed; keep the folder; clear the pointer if "
        "it names this task.",
    )
    add_state_op_parser(
        "reopen",
        "Reverse a terminal state back to active (the folder must still "
        "exist); re-validate.",
    )
    add_state_op_parser(
        "archive",
        "Archive an active task per the closure policy: tmp -> status "
        "archived, folder kept; non-tmp -> folder deleted (git is the "
        "record; refuses when uncommitted).",
    )
    add_state_op_parser(
        "delete",
        "Archive semantics, then remove the folder even when tmp "
        "(unconditional removal).",
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
    if args.verb == "current":
        return _cmd_current(args)
    if args.verb == "status":
        return _cmd_status(args)
    if args.verb == "work":
        return _cmd_work(args)
    if args.verb == "switch":
        return _cmd_switch(args)
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
