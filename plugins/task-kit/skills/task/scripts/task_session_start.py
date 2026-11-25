"""task_session_start.py -- the SessionStart hook entry (matchers ``resume``, ``clear`` and ``compact``).

``hooks/sessionstart/session-start.sh`` pipes the hook input (JSON) to this script after its fast path
saw a session file in the project that the event can affect. The input's ``source`` selects the work. The
registrations are read from the git-tracked ``.sessions/<host>.yaml`` files of the project the session runs in:
``$CLAUDE_PROJECT_DIR`` when set, else the input's ``cwd``. A session that registered tasks in a different
project than that one is not refreshed or released (its registration is then corrected at its next
``/task work`` there).

* ``resume`` -- the session id is kept and the process is new: ``sessions.refresh`` gives every record of this
  machine carrying that id the new process stamp and the transcript path.
* ``clear`` -- the session id changes and the process stays, but the cleared conversation no longer works the
  tasks: ``sessions.release_after_clear`` removes every record of the calling process (compaction, which keeps
  the session id, keeps the registration) and the old session id's statusline pointers are removed with them.
* ``compact`` -- the session id and the process stay, so the registration is kept as it is and nothing is
  written. The compacted conversation, though, may no longer know that it holds it: :func:`compact_context`
  prints SessionStart hook JSON to stdout whose ``additionalContext`` names each open task folder
  (:func:`held_folders`) this machine's records hold for the session id, and the shell script passes it to
  Claude Code (the ``compact`` entry is synchronous). A session that holds nothing prints nothing.
* anything else (``fork``, ``startup``) does nothing: a fork has a fresh id and registers through ``/task work``.

Changed session files are then published, one publication per repository, through ``session_publish``.

The hook never fails the session: :func:`main` returns 0 on every path. On ``resume`` and ``clear`` it runs
asynchronously, so nothing it prints reaches anyone, and on ``compact`` stdout carries only the context; a
failure (an error, a note, a publication that did not complete) is therefore appended to ``<data dir>/sessions/session-start-hook.log``
(stderr when the data directory cannot be resolved). That file is a diagnostic of failures only, never a record of registrations or of successful runs. The library is
imported inside :func:`run`, after the venv re-exec in :func:`main`, as ``task.py`` orders it.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

LOG_NAME = "session-start-hook.log"
SOURCE_RESUME = "resume"
SOURCE_CLEAR = "clear"
SOURCE_COMPACT = "compact"
PROJECT_DIR_ENV = "CLAUDE_PROJECT_DIR"
_COMPLETE = ("published", "nothing-pending", "disabled")  # session_publish statuses that need no diagnostic


class HookInputError(ValueError):
    """The hook input is not the JSON object this hook needs."""


def parse_input(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HookInputError(f"the hook input is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HookInputError("the hook input is not a JSON object")
    return data


def log_line(data_dir: Path | None, text: str) -> None:
    """Append one timestamped line to the hook log; to stderr when there is no data directory."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp} session-start: {text}"
    if data_dir is not None:
        try:
            path = Path(data_dir) / "sessions" / LOG_NAME
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            return
        except OSError as exc:
            line += f" (the log {data_dir} is unwritable: {exc})"
    print(line, file=sys.stderr)


def publish_changed(changed, machine, data_dir: Path | None, log: Callable[[str], None]) -> None:
    """Publish the repositories whose session files changed: once per repository, durable roots only.

    Only an outcome that needs a diagnostic is logged (a refusal, or a publication that did not complete)."""
    from task_system import session_publish

    seen: set[Path] = set()
    for file in changed:
        root = Path(file).parent.parent  # <task root>/.sessions/<host>.yaml
        flight = session_publish.preflight(root, machine=machine)
        if not flight.ok:
            log(f"not published for {root}: {flight.reason}")
            continue
        if flight.kind != session_publish.KIND_DURABLE or flight.repo_root is None:
            continue  # a tmp root is machine-local: nothing to publish
        if flight.repo_root in seen:
            continue
        seen.add(flight.repo_root)
        result = session_publish.publish(flight.repo_root, machine, data_dir=data_dir)
        if result.status not in _COMPLETE:
            log(f"{flight.repo_root}: {result.status}: {result.message}")


def held_folders(project_root: Path, machine, session_id: str) -> list[str]:
    """The task folders this machine's records (its own file) hold for ``session_id`` on open tasks; never writes."""
    from task_system import sessions

    return [str(record.root / record.task) for view in sessions.read_project(project_root, machine)
            for record in view.records
            if record.owned and record.session_id == session_id and not record.dropped_on_next_write]


def compact_context(folders: list[str]) -> str:
    """The SessionStart hook JSON that tells a compacted conversation which task folders it still works."""
    text = (f"task-kit: this session is still registered as working {', '.join(folders)} (the registration "
            "carried over the compaction; do not register again). Re-read each task folder's CLAUDE.md and "
            "plan.md before you continue its work.")
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})


def run(raw: str, env: Mapping[str, str] | None = None, *, data_dir: Path | None = None, machine=None,
        identity=None, publish: Callable[..., None] | None = None, log: Callable[[str], None] | None = None,
        project_root: Path | None = None, emit: Callable[[str], None] | None = None) -> str:
    """Do the work for one hook input and return a one-line outcome. Raises on any failure (see :func:`main`).

    ``data_dir``, ``machine``, ``identity``, ``publish``, ``log``, ``project_root`` and ``emit`` (where the
    compact context goes: stdout) default to the real ones (the project is ``$CLAUDE_PROJECT_DIR``, else the
    input's ``cwd``); tests inject them.
    """
    from task_system import datadir, harness, machine as machine_module, sessions, statusline_pointer

    env = os.environ if env is None else env
    data = parse_input(raw)
    source = data.get("source")
    if source not in (SOURCE_RESUME, SOURCE_CLEAR, SOURCE_COMPACT):
        return f"ignored source {source!r}"
    if harness.in_dashboard_run(env):
        return "ignored: this is a dashboard run"
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise HookInputError("the hook input carries no session_id")
    if project_root is None:
        project = env.get(PROJECT_DIR_ENV) or data.get("cwd")
        if not isinstance(project, str) or not project:
            raise HookInputError(f"the hook input carries no cwd and {PROJECT_DIR_ENV} is not set")
        project_root = Path(project)
    if machine is None:
        machine = machine_module.current()
    if source == SOURCE_COMPACT:
        folders = held_folders(project_root, machine, session_id)
        if folders:
            (emit or print)(compact_context(folders))
        return f"compact {session_id[:8]}: holds {len(folders)}"
    if data_dir is None:
        data_dir = datadir.resolve_data_dir()
    if log is None:
        def log(text: str) -> None:
            log_line(data_dir, text)
    if identity is None:
        identity = harness.resolve_identity({**env, harness.SESSION_ID_ENV: session_id})
    transcript = data.get("transcript_path")
    recorded = sessions.to_home_relative(transcript) if isinstance(transcript, str) and transcript else None
    if source == SOURCE_RESUME:
        result = sessions.refresh(identity, machine, recorded, project_root=project_root, data_dir=data_dir)
        outcome = f"resume {session_id[:8]}: refreshed {len(result.refreshed)}, dropped {len(result.dropped)}"
    else:
        result = sessions.release_after_clear(identity, machine, project_root=project_root, data_dir=data_dir)
        for gone in result.released:
            statusline_pointer.remove(data_dir, gone.session_id, gone.root / gone.task)
        outcome = (f"clear {session_id[:8]}: released {len(result.released)} from "
                   f"{', '.join(i[:8] for i in result.previous_ids) or 'no previous session'}")
    for note in result.notes:
        log(f"note: {note}")
    if result.changed_files:
        (publish or publish_changed)(result.changed_files, machine, data_dir, log)
    return outcome


def main() -> int:
    """Re-exec under the task-kit venv, run, and exit 0 whatever happens (the hook never fails the session)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    data_dir = None
    try:
        from bootstrap_guard import reexec_under_plugin_venv

        reexec_under_plugin_venv("task-kit")
        from task_system import datadir

        data_dir = datadir.resolve_data_dir()
        run(sys.stdin.read(), data_dir=data_dir)  # the outcome is not logged: the log holds failures only
    except SystemExit as exc:
        if exc.code not in (0, None):
            log_line(data_dir, f"stopped with exit code {exc.code}")
    except BaseException:  # noqa: BLE001 -- the hook must not fail the session; the traceback is the log
        log_line(data_dir, "failed:\n" + traceback.format_exc().rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
