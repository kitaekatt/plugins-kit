"""Launcher target for ``bin/task-kit`` and ``bin/task-kit.cmd``.

Two copies of this file exist, and the same code serves both:

- In the plugin (the working copy, or the plugin cache), the task system is
  beside it: ``../skills/task/scripts/task_system``. It puts that scripts
  directory on ``sys.path`` and dispatches the command.
- In task-kit's data directory ``bin/`` (the copy bootstrap syncs there and
  puts on PATH, so ``task-kit`` works in any terminal) no task system is
  beside it. The bundle is thin on purpose: it locates the newest installed
  task-kit version in the plugin cache at run time and runs THAT copy's
  entrypoint, so a plugin update needs no PATH or bundle change.

Commands:

- ``task-kit export ...`` runs ``task_system.export_cli``, the task export
  databench reads. Every outcome, including a failure to start it, writes one
  JSON document to stdout (``task-export-protocol.md``).
- ``task-kit answers ...`` runs ``task_system.answers_cli``, the task answers engine (refresh, status, prune),
  under the same one-document rules.
- ``task-kit`` with no command runs ``databench --view tasks`` when databench
  is on PATH, and otherwise exits 1 saying databench provides the task view.
- ``task-kit stop [--force]`` stops a dashboard server an earlier version
  started (``task_system.legacy_stop``). It stays for one task-kit minor.

Stdlib only until the task system is imported.
"""
from __future__ import annotations

import json
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN = "task-kit"
ENTRYPOINT_RELATIVE = ("bin", "task_kit_entrypoint.py")
SCRIPTS_RELATIVE = ("skills", "task", "scripts")
TASK_SYSTEM_RELATIVE = SCRIPTS_RELATIVE + ("task_system",)
_VERSION = re.compile(r"^\d+(?:\.\d+)*$")

DATABENCH = "databench"
DATABENCH_VIEW = ("--view", "tasks")
DATABENCH_MISSING_EXIT = 1
USAGE_EXIT = 2
DATABENCH_MISSING_MESSAGE = (
    "task-kit: databench provides the task view, and `databench` is not on PATH. Install databench "
    "(https://github.com/kitaekatt/databench) so that `databench` runs in a terminal, then run `task-kit` again."
)
RETIRED_MESSAGE = (
    "task-kit: the task-kit dashboard is retired; databench provides the task view (run `task-kit` with no "
    "arguments). Remaining commands: `task-kit export ...`, `task-kit answers ...` and `task-kit stop "
    "[--force]` (stops a dashboard server an earlier version started)."
)


def has_task_system(root: Path) -> bool:
    """True when ``root`` is a task-kit plugin root that carries the task system."""
    return root.joinpath(*TASK_SYSTEM_RELATIVE).is_dir()


def version_key(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in name.split("."))


def installed_plugin_root(bundle_root: Path) -> Path:
    """The newest task-kit version in the plugin cache, for a data-dir bundle.

    ``bundle_root`` is ``<plugins>/data/<marketplace>/task-kit``; the cache is
    its sibling tree ``<plugins>/cache/<marketplace>/task-kit/<version>``.
    """
    marketplace = bundle_root.parent.name
    data_root = bundle_root.parent.parent
    cache = data_root.parent / "cache" / marketplace / PLUGIN
    candidates = []
    if cache.is_dir():
        for entry in cache.iterdir():
            if (
                entry.is_dir()
                and _VERSION.match(entry.name)
                and has_task_system(entry)
                and entry.joinpath(*ENTRYPOINT_RELATIVE).is_file()
            ):
                candidates.append((version_key(entry.name), entry))
    if not candidates:
        raise SystemExit(
            f"task-kit: no installed task-kit was found in {cache}. "
            f"Install or update the plugin ({marketplace}:{PLUGIN}) and start a new "
            "Claude Code session so bootstrap can provision it."
        )
    return max(candidates, key=lambda item: item[0])[1]


EXPORT_COMMAND = "export"
ANSWERS_COMMAND = "answers"
STOP_COMMAND = "stop"
EXPORT_PROTOCOL = 1  # the highest export protocol; a start-up failure document names it
EXPORT_INTERNAL_EXIT = 1


def export_start_failure(root: Path, message: str, command: str = EXPORT_COMMAND) -> int:
    """Answer ``task-kit export`` (or ``answers``) with one ``internal`` error document when it cannot start.

    Stdlib only: this runs when the plugin venv or the task system cannot be
    loaded, which is exactly when nothing else could write the document.
    """
    try:
        version = json.loads(root.joinpath(".claude-plugin", "plugin.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        version = "unknown"
    document = {"protocol": EXPORT_PROTOCOL, "task_kit_version": version,
                "error": {"code": "internal", "message": message}}
    print(f"task-kit {command}: internal: {message}", file=sys.stderr)
    sys.stdout.write(json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return EXPORT_INTERNAL_EXIT


def _run_document_command(root: Path, command: str, module: str, argv: list[str]) -> int:
    """Run ``task_system.<module>``'s ``main`` under the plugin venv; a failure to start is one error document."""
    import importlib  # noqa: PLC0415

    from bootstrap_guard import reexec_under_plugin_venv  # noqa: PLC0415

    try:
        reexec_under_plugin_venv(PLUGIN)
    except SystemExit as exc:  # the guard refuses (a redirected venv is missing); its reason is on stderr
        return export_start_failure(
            root, f"task-kit's plugin interpreter is not available (exit {exc.code}); see stderr", command
        )
    try:
        target = importlib.import_module(f"task_system.{module}")
    except ImportError as exc:
        return export_start_failure(
            root, f"the task system cannot be loaded ({type(exc).__name__}: {exc}); task-kit's venv is not provisioned",
            command,
        )
    return target.main(argv)


def run_export(root: Path, argv: list[str]) -> int:
    """``task-kit export ...``: the export CLI."""
    return _run_document_command(root, EXPORT_COMMAND, "export_cli", argv)


def run_answers(root: Path, argv: list[str]) -> int:
    """``task-kit answers ...``: the answers engine CLI."""
    return _run_document_command(root, ANSWERS_COMMAND, "answers_cli", argv)


def run_databench() -> int:
    """Bare ``task-kit``: ``databench --view tasks``, or exit 1 saying databench provides the task view."""
    located = shutil.which(DATABENCH)
    if located is None:
        print(DATABENCH_MISSING_MESSAGE, file=sys.stderr)
        return DATABENCH_MISSING_EXIT
    return subprocess.call([located, *DATABENCH_VIEW])


def run_stop(argv: list[str]) -> int:
    """``task-kit stop``: stop a dashboard server an earlier version started."""
    from bootstrap_guard import reexec_under_plugin_venv, require_bootstrap  # noqa: PLC0415

    reexec_under_plugin_venv(PLUGIN)
    try:
        from task_system import legacy_stop  # noqa: PLC0415
    except ImportError as exc:
        print(f"[task-kit] underlying error: {type(exc).__name__}: {exc}", file=sys.stderr)
        require_bootstrap(PLUGIN, feature="stop", missing="psutil/pyyaml/skills_kit_lib", force=True)
        return 3
    return legacy_stop.main(argv)


def main() -> int:
    here = Path(__file__).resolve()
    root = here.parent.parent
    if not has_task_system(root):
        located = installed_plugin_root(root).joinpath(*ENTRYPOINT_RELATIVE)
        runpy.run_path(str(located), run_name="__main__")
        return 0
    scripts = root.joinpath(*SCRIPTS_RELATIVE)
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    argv = sys.argv[1:]
    if argv[:1] == [EXPORT_COMMAND]:
        return run_export(root, argv[1:])
    if argv[:1] == [ANSWERS_COMMAND]:
        return run_answers(root, argv[1:])
    if not argv:
        return run_databench()
    if argv[0] == STOP_COMMAND:
        return run_stop(argv[1:])
    print(RETIRED_MESSAGE, file=sys.stderr)
    return USAGE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
