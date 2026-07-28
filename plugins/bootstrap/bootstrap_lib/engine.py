#!/usr/bin/env python3
"""Bootstrap engine — processes bootstrap manifests and emits hook responses.

Usage:
    python3 -m bootstrap_lib.engine --plugin-root /path/to/bootstrap --data-dir /path/to/data

    Or via console script entry point:
    bootstrap-engine --plugin-root /path/to/bootstrap --data-dir /path/to/data

Exit behavior:
    Emits hook JSON to stdout with systemMessage showing new log entries.
    On failure, additionalContext includes remediation instructions for the agent.
    Silent exit (no stdout) when there are no new log entries to display.
"""

import argparse
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone

# Single shared atomic-write implementation (mkstemp + os.replace next to the
# destination). engine.py used to carry its own fixed-name `path + ".tmp"`
# copy, which collided across concurrent sessions — see atomic_write.py.
from .atomic_write import write_atomic as _write_atomic

# User-facing message text: numbering for collated lines, fits-or-skip label
# selection instead of truncation. See messages.py / engine-internals.md.
from .messages import item_label as _item_label, numbered as _numbered
from .records import reprefix as _reprefix


def main():
    """Entry point: acquire the single-instance lock, then run the engine
    pass with crash containment.

    The shell hook stamps the per-project cooldown BEFORE launching the
    engine and (in background mode) only surfaces what the engine writes to
    bootstrap_display.pending. Without containment, an unhandled exception
    means: traceback lost to engine_output.log, no pending file (silent
    failure for the user), and a stamped cooldown that throttles every
    re-run for the rest of the window. Contain it: write a crash .pending so
    the next prompt surfaces the failure, clear the cooldown so the next
    SessionStart retries, then re-raise behavior via exit code 1 with the
    traceback on stderr (still captured by engine_output.log).

    Locking (proc_lock.engine_lock) wraps the whole pass: rapid session
    start/exit/restart can fire several independent launchers (session-
    bootstrap.sh, the harvest, the SessionStart-missed rescue) within the
    same few seconds, each of which stands down a RE-launch of itself but not
    a genuinely concurrent OTHER process. When the lock can't be acquired,
    another engine instance is already running a pass (for this project or a
    different one -- the lock is engine-wide, not per-project, because a
    concurrent pass from ANY project races the same shared-lib sync this bug
    was filed against) -- stand down without running _main(), but roll back
    this launch's already-consumed per-project cooldown stamp (see
    _stand_down_lock_contended) so the project that lost the race gets a
    genuine retry instead of silently waiting out the cooldown window. The
    elevation flow's own child-engine relaunch (_spawn_recheck_pass)
    sidesteps this entirely by releasing the lock early via
    proc_lock.release_lock() before spawning its child.

    Version-aware arbitration: an engine that CARRIES the update (its own
    version > the global engine_ran_version stamp) does not stand down on the
    first contended attempt. session-bootstrap.sh's _provision step runs
    before the lock, so a harvest-launched NEW engine can reach the lock
    seconds after a resident OLD engine already took it -- yielding there
    hands the pass to the older binary. Such an engine retries for
    _LOCK_RETRY_SECONDS instead; engines that are not newer keep the
    immediate stand-down.
    """
    data_dir, project_dir, plugin_root, console, background = _peek_lock_args()
    if not data_dir:
        # --data-dir is a required arg; _main()'s own parser will reject a
        # genuinely missing one with the standard argparse error.
        _run_with_containment()
        return

    from .proc_lock import engine_lock
    with engine_lock(data_dir) as acquired:
        if acquired:
            _run_with_containment()
            return

    if _carries_update(data_dir, plugin_root):
        with _retry_engine_lock(data_dir) as acquired:
            if acquired:
                _run_with_containment()
                return

    _stand_down_lock_contended(data_dir, project_dir, plugin_root,
                               console=console, background=background)


# Bounds for the version-aware retry in main(). Module constants, not inlined
# literals, so tests can shrink them without sleeping for real.
_LOCK_RETRY_SECONDS = 10.0
_LOCK_RETRY_INTERVAL = 0.5


def _retry_engine_lock(data_dir):
    """Re-attempt proc_lock's NON-BLOCKING acquisition on an interval until
    _LOCK_RETRY_SECONDS elapses, yielding True on the attempt that wins.

    Deliberately a loop over ``engine_lock`` rather than a blocking mode
    inside proc_lock: proc_lock's contract (one non-blocking attempt, caller
    decides how to stand down) is what every other launcher depends on, and
    the release-on-exit ownership check stays proc_lock's.
    """
    import time
    from contextlib import contextmanager

    from .proc_lock import engine_lock

    @contextmanager
    def _loop():
        deadline = time.monotonic() + _LOCK_RETRY_SECONDS
        while True:
            with engine_lock(data_dir) as acquired:
                if acquired:
                    yield True
                    return
            if time.monotonic() >= deadline:
                break
            time.sleep(_LOCK_RETRY_INTERVAL)
        yield False

    return _loop()


def _plugin_root_version(plugin_root) -> str:
    """This process's own bootstrap version, read from its plugin root's
    plugin.json. Empty string when unavailable -- callers treat that as
    "not newer" so an unreadable manifest can never escalate behavior."""
    if not plugin_root:
        return ""
    path = os.path.join(plugin_root, ".claude-plugin", "plugin.json")
    try:
        with open(path, "r") as f:
            return json.load(f).get("version", "") or ""
    except (OSError, ValueError):
        return ""


def _carries_update(data_dir, plugin_root) -> bool:
    """True when THIS engine is strictly newer than the last engine to
    complete a pass -- i.e. it is the one carrying an update forward."""
    own = _plugin_root_version(plugin_root)
    if not own:
        return False
    try:
        from .stamps import global_stamp
        ran = global_stamp(data_dir, "engine_ran_version").read()
    except Exception:
        return False
    return _parse_semver(own) > _parse_semver(ran or "0")


def _peek_lock_args() -> tuple:
    """Lenient pre-parse of the few args needed before the full argparse runs:
    --data-dir/--project-dir/--plugin-root so the lock can be acquired (and, on
    stand-down, this launch's guards rolled back and its own version reported),
    plus --console/--background so a stand-down can REPORT itself on whichever
    channel the caller is listening to."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--project-dir", default=None)
    parser.add_argument("--plugin-root", default=None)
    parser.add_argument("--console", action="store_true")
    parser.add_argument("--background", action="store_true")
    args, _ = parser.parse_known_args()
    return (args.data_dir or "", args.project_dir or "", args.plugin_root or "",
            args.console, args.background)


def _stand_down_lock_contended(data_dir, project_dir, plugin_root="",
                               console=False, background=False):
    """Another engine instance already holds the lock -- this pass never ran.
    Roll back the ONE guard that is unambiguously OURS: the per-project
    cooldown stamp session-bootstrap.sh writes BEFORE launching the engine.
    Left alone, the project this launch was for would get no bootstrap pass
    until that cooldown naturally expires.

    Also RE-ARMS the harvest when this process carries an update it never got
    to apply (own version > engine_ran_version). The earlier reasoning for
    leaving harvest_launched_version alone -- "ANY completed pass stamps
    engine_ran_version, so the marker self-clears" -- is false when the
    completing pass is an OLDER engine: it stamps a version still behind the
    installed one, so should_harvest stays true while the per-installed-
    version marker keeps the harvest permanently disarmed. That is a wedge no
    later session recovers from. Clearing the marker is safe now that the
    stamp is MONOTONIC (an older engine can no longer regress it): if a NEWER
    engine holds the lock, it completes and stamps >= our own version, making
    should_harvest false -- so a cleared marker cannot produce a duplicate
    spawn storm.

    Still deliberately does NOT touch the import-retry / registry-relaunch
    markers: those belong to whichever launch trigger fired, which may not be
    THIS launch, and neither is version-keyed, so neither can wedge the way
    the harvest marker did.

    Logged so a stand-down is visible in bootstrap.log rather than silently
    invisible (the "every remediation-like action logs its outcome" rule this
    repo's CLAUDE.md holds tooling to). Best-effort; never raises -- a failure
    here just costs one retry cycle, never a crash.
    """
    try:
        _clear_project_cooldown(data_dir, project_dir)
    except Exception:
        pass
    own_version = _plugin_root_version(plugin_root)
    rearmed = False
    try:
        if _carries_update(data_dir, plugin_root):
            from .stamps import global_stamp
            global_stamp(data_dir, "harvest_launched_version").clear()
            rearmed = True
    except Exception:
        pass
    holder = _lock_holder_pid(data_dir)
    who = f"engine {own_version}" if own_version else "engine"
    where = f" (pid {holder})" if holder else ""
    headline = f"stand-down: {who} yielded to running engine pass{where}"
    entry = (
        f"{headline}; this project's cooldown was cleared for a retry on the "
        "next opportunity"
    )
    if rearmed:
        entry += "; harvest re-armed (this engine carries an update)"
    try:
        from .log import write_log_block
        write_log_block(data_dir, "bootstrap lock", [entry])
    except Exception:
        pass

    # Also report on whatever channel the CALLER is listening to. The log
    # alone is not enough: a stand-down exits 0 having done nothing, which
    # from outside is indistinguishable from a clean pass. An agent driving
    # `--console --fix-all` sees empty output plus exit 0, reads it as
    # success, and either reports a fix that never ran or starts debugging
    # the wrong layer entirely (observed live, 0.66.2 -- three no-op fix-all
    # invocations in a row were each diagnosed as a different bug). The
    # caller only needs the headline: "stand-down" already means no work
    # happened. The retry/cooldown detail stays in the log entry above.
    try:
        if console:
            print(f"--- bootstrap lock: {headline} ---")
        else:
            emit_success_response(
                f"--- bootstrap lock: {headline} ---",
                label="bootstrap",
                output_file=(os.path.join(data_dir, "bootstrap_display.pending")
                             if background else None),
            )
    except Exception:
        pass


def _lock_holder_pid(data_dir):
    """PID recorded in the single-instance lock file, or None. Best-effort
    diagnostics only -- the holder may exit between this read and the log."""
    try:
        from .proc_lock import LOCK_FILENAME, _read_lock_pid
        return _read_lock_pid(os.path.join(data_dir, LOCK_FILENAME))
    except Exception:
        return None


def _run_with_containment():
    try:
        _main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        try:
            if _is_transient_import_crash(exc):
                # Partial-download race: stay SILENT and retry, never report.
                _defer_transient_retry(tb)
            else:
                _emit_engine_crash(tb)
        except Exception:
            pass  # crash reporting must never mask the original traceback
        sys.stderr.write(tb)
        sys.exit(1)


# First-party packages the engine (and its shared libs) import. A partial plugin
# cache download can land some of a package's modules before others, so the
# SessionStart hook may import one of these and hit a submodule that has not been
# written YET -- a ModuleNotFoundError that self-heals once the download finishes.
_FIRST_PARTY_LIBS = ("bootstrap_lib", "skills_kit_lib", "openrouter_kit")


def _is_transient_import_crash(exc) -> bool:
    """True for a ModuleNotFoundError/ImportError on a first-party package whose
    submodule has not landed yet -- the signature of a SessionStart hook racing a
    mid-flight plugin cache download. Such a crash self-heals on the next pass, so
    it is handled silently (see _defer_transient_retry) rather than surfaced.

    Scoped to first-party packages on purpose: a genuinely missing THIRD-party
    dependency (a real config gap) is NOT transient and keeps the loud crash path.
    """
    if not isinstance(exc, ImportError):
        return False
    name = getattr(exc, "name", None) or ""
    return any(name == lib or name.startswith(lib + ".") for lib in _FIRST_PARTY_LIBS)


def _defer_transient_retry(tb):
    """Handle a transient first-party import crash (partial-download race).

    Stay SILENT -- write NO user-facing message; a ModuleNotFoundError traceback
    only confuses, and it self-heals. Instead: mark a retry pending and clear the
    cooldown so the next SessionStart re-runs. The UserPromptSubmit harvest
    (bootstrap_lib/harvest.py) relaunches within the SAME session off the pending
    marker, so the user rarely has to do anything. The traceback still reaches
    engine_output.log via main()'s stderr write, so the evidence is not lost. A
    completed pass clears the markers (engine._main). No-op in --console mode and
    when --data-dir is unavailable (nowhere to write), matching _emit_engine_crash.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--project-dir", default=None)
    parser.add_argument("--console", action="store_true")
    args, _ = parser.parse_known_args()
    if args.console or not args.data_dir:
        return
    # Deliberately silent to the user -- but not to the record. This path
    # swallows a real traceback on purpose (it self-heals), which meant a
    # recurring, non-self-healing failure wearing this signature would look
    # like nothing had happened at all.
    try:
        from .records import PassRecorder
        r = PassRecorder(args.data_dir, mode="hook", autoflush=False)
        r.record("crash", tb, sev="quiet", transient=True)
        r.flush()
    except Exception:
        pass
    from .stamps import global_stamp
    global_stamp(args.data_dir, "import_retry_pending").write("1")
    # Void the in-flight guard: THIS attempt crashed, so the harvest may relaunch
    # once more. (Set by harvest on launch, cleared here on crash -> at most one
    # retry pass in flight at a time, retrying until the download completes.)
    global_stamp(args.data_dir, "import_retry_launched").clear()
    _clear_project_cooldown(args.data_dir, args.project_dir)


def _emit_engine_crash(tb):
    """Write a crash .pending + clear the cooldown after an engine crash.

    Re-parses argv leniently (the crash may have happened before/around
    normal arg parsing). No-op in --console mode (console writes no files
    and the user sees the traceback directly) and when --data-dir is
    unavailable (nowhere to write).
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--project-dir", default=None)
    parser.add_argument("--console", action="store_true")
    args, _ = parser.parse_known_args()
    if not args.data_dir:
        return

    # Record the traceback before the console early-return. A crashed pass is
    # the one whose evidence matters most, and until now its only home was
    # engine_output.log -- which the next launch (often seconds later)
    # truncated. Console crashes were recorded nowhere at all.
    try:
        from .records import PassRecorder
        crash_recorder = PassRecorder(
            args.data_dir, mode="console" if args.console else "hook",
            autoflush=False)
        crash_recorder.record("crash", tb, sev="fail")
        crash_recorder.flush()
    except Exception:
        pass

    if args.console:
        return

    first_line = tb.strip().splitlines()[-1] if tb.strip() else "unknown error"
    response = {
        "continue": True,
        "suppressOutput": False,
        "systemMessage": (
            f"bootstrap -> engine crashed: {first_line}. "
            "Bootstrap did not complete; it will retry on the next session start."
        ),
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "bootstrap -> the bootstrap engine crashed with an unhandled "
                "exception before completing its pass. Tell the user bootstrap "
                "failed, and offer to investigate. Traceback:\n" + tb
            ),
        },
    }
    pending = os.path.join(args.data_dir, "bootstrap_display.pending")
    _write_atomic(pending, json.dumps(response))
    # Roll back the shell hook's optimistic cooldown stamp so the next
    # SessionStart re-runs instead of silently throttling on a crashed pass.
    _clear_project_cooldown(args.data_dir, args.project_dir)


def _append_detail(entries, text, detail=None, display=None):
    """Append a log entry, carrying structured detail into the record when the
    list is a RecordingList (and degrading to a plain append when it is not).

    The detail never reaches the log line or either message surface -- it exists
    so the record can hold what the line had to leave out.
    """
    if display is None and detail is None:
        entries.append(text)
        return
    rich = getattr(entries, "append_rich", None)
    if rich is not None:
        rich(text, display=display, detail=detail)
        return
    # A PLAIN list -- which is the common case, not the exception: the manifest
    # phases build a local list and the caller extends a RecordingList with it
    # afterwards. Attaching both to the entry itself is what carries them
    # across that hand-off; appending the bare text here silently dropped the
    # detail on every manifest-phase path.
    from .records import Entry
    entries.append(Entry(text, short=display, detail=detail))


def _record_failures(recorder, failures):
    """Record each failure dict verbatim. Tolerates a missing recorder."""
    if recorder is None:
        return
    for f in failures or ():
        recorder.record("failure", f.get("message") or f.get("type") or "failure",
                        sev="fail", plugin=f.get("plugin"), failure=f)


def _record_emit(recorder, channel, response):
    """Record a rendered hook payload. Tolerates a missing recorder.

    This is what makes shortening the user-facing surface free: the exact text
    each audience received is on disk, so a collated line may be as terse as the
    UX wants without the message becoming the only copy.
    """
    if recorder is not None:
        recorder.record_emit(channel, response)


def _record_entries(recorder, sev, entries, section=None, plugin=None):
    """Record entries produced by a helper that returns plain lists.

    Most entries reach the record through a RecordingList, which is why no call
    site had to change. A few helpers build and return their own lists; those
    would otherwise be the one silent gap, so their callers record explicitly.
    """
    if not recorder or not entries:
        return
    for entry in entries:
        recorder.record_entry(sev, entry, section=section, plugin=plugin)


class _NullRecorder:
    """Stand-in when the recorder cannot be built. Absorbs every call.

    The record is valuable but never load-bearing: bootstrap's job is to
    provision the machine, and no observability failure may cost a pass.
    """

    enabled = False

    def record(self, *a, **kw):
        pass

    record_entry = record_emit = flush = record


def _new_recorder(data_dir, start_time, args):
    """Build the pass recorder, or a no-op stand-in. Never raises."""
    try:
        from .records import PassRecorder
        mode = "console" if getattr(args, "console", False) else (
            "background" if getattr(args, "background", False) else "hook")
        return PassRecorder(data_dir, start_time=start_time, mode=mode)
    except Exception:
        return _NullRecorder()


def _main():
    start_time = datetime.now(timezone.utc)

    parser = argparse.ArgumentParser(description="Bootstrap engine")
    parser.add_argument("--plugin-root", required=True, help="Path to bootstrap plugin root")
    parser.add_argument("--data-dir", required=True, help="Path to bootstrap data directory")
    parser.add_argument("--hook-start-epoch", type=int, default=0, help="(unused, kept for backward compat)")
    parser.add_argument("--project-dir", default=None, help="Project root directory (for layered bootstrap.json)")
    parser.add_argument("--verbose", action="store_true", help="Write ok/cached entries to the log file (never shown in hook output)")
    parser.add_argument("--console", action="store_true", help="Plain text output, no JSON/log writes")
    parser.add_argument("--background", action="store_true",
        help="Write display output to bootstrap_display.json instead of stdout")
    parser.add_argument("--fix-all", dest="fix_all", action="store_true",
        help="Interactive remediation run triggered by the user typing "
             "'fix-all'. This is user consent for elevation: on Windows the "
             "engine launches the generated elevation script itself (UAC "
             "prompt), waits for it, then re-runs the checks. NEVER passed by "
             "the SessionStart hook.")
    args = parser.parse_args()

    # --console implies --verbose
    if args.console:
        args.verbose = True

    plugin_root = args.plugin_root
    data_dir = args.data_dir

    from .config import load_config
    from .log import write_log_block
    from .path_repair import repair_path
    from .tool_check import check_tool
    from .path_check import check_path_entry
    from .platform_detect import detect_os, UnsupportedPlatformError
    from .plugin_resolve import list_enabled_plugins
    from .venv_check import check_venv
    from .git_dep_check import check_git_dep

    # Repair PATH before any subprocess fan-out. On Windows, a bloated
    # launching-shell PATH can trip cmd.exe's variable-size limit during
    # venv activation and leave this Python with a stripped PATH that
    # fails tool_check / git_dep_check / etc.
    path_repair_result = repair_path()

    # Step 1: Load/migrate config
    defaults_dir = os.path.join(plugin_root, "defaults")
    config = load_config(data_dir, defaults_dir)

    # Fail fast on an unsupported platform (non-Ubuntu Linux) BEFORE any tool
    # or manifest processing: silently running Ubuntu/apt commands on a foreign
    # distro is exactly what the ratified change forbids. This is a genuine
    # fail-fast (failure policy), not a per-item fix-all failure -- the platform
    # is unsupported as a fact, so there is nothing to auto-fix.
    try:
        current_os = detect_os()
    except UnsupportedPlatformError as e:
        _emit_unsupported_platform(str(e), data_dir, args)
        return
    # Re-arm the apt backend's once-per-pass `apt-get update` guard so the first
    # direct apt install of THIS pass refreshes package lists (see apt.py).
    from .apt import reset_apt_pass_state
    reset_apt_pass_state()
    log_success = config.get("log_success_checks", False) or args.verbose

    # The pass record. Complete by construction and independent of every
    # display filter below: `log_success` and the collated-line width decide
    # what is SHOWN, never what is KEPT. Console mode records too -- it writes
    # no log and no stamps, but an append-only record touches neither, and a
    # console pass against a wedged machine is exactly the state worth having
    # evidence of. See records.py.
    from .records import entry_list as _entry_list
    recorder = _new_recorder(data_dir, start_time, args)
    recorder.record("meta", "engine pass started",
                    detail={"os": current_os, "project_dir": args.project_dir,
                            "plugin_root": plugin_root,
                            "log_success": log_success,
                            "fix_all": bool(getattr(args, "fix_all", False))})

    all_failures = []
    # Bootstrap's own entries (self-bootstrap + user) — written to bootstrap's log.
    # RecordingLists: every append lands in the pass record, including the many
    # sites that hold one of these lists and append to it directly rather than
    # going through a ctx method.
    bootstrap_action_entries = _entry_list(recorder, "action")
    bootstrap_ok_entries = _entry_list(recorder, "ok")
    # Log-only entries (always logged, never displayed) — see _ManifestContext.quiet
    bootstrap_quiet_entries = _entry_list(recorder, "quiet")
    # Display sections: list of (header, action_entries, ok_entries)
    display_sections = []
    # Pass-level shared-lib publish/link collector: both emission sites (each
    # plugin's manifest phase and the Step 4c sweep) record successes here and
    # Step 4c renders ONE aggregated display line for the whole pass.
    shared_lib_links = _SharedLibLinkLog()

    if path_repair_result.changed:
        details = []
        if path_repair_result.deduped:
            details.append(f"deduped {path_repair_result.deduped}")
        if path_repair_result.restored:
            details.append(f"restored {path_repair_result.restored} from registry")
        # Logged as ok (verbose-only): PATH bloat returns next session, so this
        # is a transient cleanup, not a persistent remediation worth surfacing.
        bootstrap_ok_entries.append(
            f"PATH repaired: {path_repair_result.before_entries} -> "
            f"{path_repair_result.after_entries} entries "
            f"({', '.join(details)})"
        )

    # Detect plugins directory (where installed_plugins.json lives)
    # Dev layout: ~/Dev/<marketplace>/plugins/bootstrap → one up
    # Cache layout: ~/.claude/plugins/cache/<marketplace>/bootstrap/<ver> → walk up to ~/.claude/plugins/
    plugins_dir = _find_plugins_dir(plugin_root)
    # Marketplace name: go 2 levels up from plugin_root and take basename.
    # Dev: plugins-kit/plugins/bootstrap → up 2 → plugins-kit
    # Cache: cache/plugins-kit/bootstrap/0.5.0 → up 2 → plugins-kit
    marketplace_name = os.path.basename(os.path.normpath(os.path.join(plugin_root, "..", "..")))
    plugin_json_path = os.path.join(plugin_root, ".claude-plugin", "plugin.json")
    boot_plugin_name = "bootstrap"
    version = ""
    try:
        with open(plugin_json_path, "r") as f:
            pj = json.load(f)
            boot_plugin_name = pj.get("name", "bootstrap")
            version = pj.get("version", "")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    version_suffix = f"@{version}" if version else ""
    bootstrap_label = f"{marketplace_name}:{boot_plugin_name}{version_suffix}" if marketplace_name else f"{boot_plugin_name}{version_suffix}"

    # Step 2b: Version change detection. The last_version stamp is the engine's
    # GLOBAL stamp (in data_dir) — routed through the stamps module's single
    # atomic-write + safe-read convention. See bootstrap_lib/stamps.py.
    from .stamps import global_stamp
    action_entries = []
    ok_entries = []
    if version:
        last_version_stamp = global_stamp(data_dir, "last_version")
        last_version = last_version_stamp.read()
        if last_version:
            # Compared by semver, not string equality: only a genuine UPGRADE
            # is an action. The reverse direction (an older binary running
            # after a newer one) is normal -- a dev tree, or a resident older
            # session -- and reporting it as "updated: 0.62.0 -> 0.61.0" reads
            # as a downgrade that never happened.
            if _parse_semver(version) > _parse_semver(last_version):
                action_entries.append(f"updated: {last_version} -> {version}")
            elif _parse_semver(version) < _parse_semver(last_version):
                ok_entries.append(
                    f"engine {version} ran (a newer {last_version} ran "
                    "previously -- dev tree or older resident session)"
                )
        else:
            action_entries.append(f"installed: {version}")
        # --console returns before the engine_ran_version stamp, so a console
        # run that advanced last_version would leave the two stamps
        # inconsistent and manufacture a phantom transition on the next real
        # pass. Console debug runs write no state; this is that contract.
        if not args.console:
            last_version_stamp.write(version)
    bootstrap_action_entries.extend(action_entries)
    bootstrap_ok_entries.extend(ok_entries)

    # Step 3: Self-setup (tools, PATH, venv from config.self_setup) — runs every session
    self_setup = config.get("self_setup", {})
    action_entries = []
    ok_entries = []
    failures = _process_self_setup(
        self_setup, current_os, data_dir, plugin_root,
        action_entries, ok_entries, plugin_name=boot_plugin_name,
    )
    bootstrap_action_entries.extend(action_entries)
    bootstrap_ok_entries.extend(ok_entries)

    if failures:
        all_failures.extend(failures)

    # Step 3b: Activate bootstrap venv site-packages so PyYAML is available
    _activate_bootstrap_venv(data_dir)

    # Step 3b2: Repair malformed registry records -- "chimera" (a user-scope
    # record carrying projectPath) and "orphan project" (a project-scope record
    # naming no project), both duplicated alongside well-formed records.
    # Once per pass and BEFORE any plugins phase (layered Step 3c and the
    # per-plugin Step 4 both run _phase_plugins), so version checks and
    # updates read a clean registry. Silent no-op on a healthy registry.
    from .registry_repair import (
        apply_repair,
        describe_repair,
        describe_unrepairable,
        find_unrepairable,
        load_registry,
    )
    _registry_path = os.path.join(plugins_dir, "installed_plugins.json")
    _repair_dropped = apply_repair(_registry_path)
    if _repair_dropped:
        bootstrap_action_entries.append(describe_repair(_repair_dropped))
    else:
        bootstrap_ok_entries.append("registry: no malformed records")

    # Refs the healthy-survivor guard refused to touch. Read post-repair so the
    # report reflects the registry as it now stands. Visible (action entry): a
    # skipped ref is a machine still carrying a defect we chose not to fix.
    _repair_skipped = find_unrepairable(load_registry(_registry_path))
    if _repair_skipped:
        bootstrap_action_entries.append(describe_unrepairable(_repair_skipped))

    # Snapshot installed plugin refs BEFORE any layered-manifest / per-plugin /
    # script install runs this pass. Claude Code loaded plugins at session start,
    # before this hook -- so any plugin that ENTERS the registry during this pass
    # is not active in the running session yet (needs /reload-plugins or a
    # restart). The after-snapshot diff (post Step 4b) drives the Step 4d reload
    # nag. We diff the registry directly rather than reusing Step 4b's new_plugins
    # because a layered `plugins:` install lands in the registry BEFORE Step 4's
    # scan and is absorbed by Step 4 (never appearing in new_plugins) -- the gap
    # the cache-kit end-to-end test surfaced.
    _registry_for_diff = os.path.join(plugins_dir, "installed_plugins.json")
    installed_refs_before = set(_read_installed_plugins(_registry_for_diff))

    # Step 3c: Process layered bootstrap manifests (user + project level)
    # Deprecation: warn if legacy user-bootstrap.json exists
    legacy_path = os.path.join(data_dir, "user-bootstrap.json")
    if os.path.isfile(legacy_path):
        bootstrap_action_entries.append(
            "DEPRECATED: user-bootstrap.json found in data dir. "
            "Migrate to ~/.claude/bootstrap.json (still processed this session)."
        )

    layered_manifest, layered_parse_errors = _load_layered_manifests(args.project_dir, data_dir)
    for pe in layered_parse_errors:
        bootstrap_action_entries.append(
            f"layered manifest {pe['path']}: PARSE FAILED - {pe['error']}"
        )
        all_failures.append({
            "type": "manifest_parse",
            "path": pe["path"],
            "message": pe["error"],
            "agent_msg": (
                f"The bootstrap manifest at {pe['path']} failed to parse "
                f"({pe['error']}). Open the file, fix the JSON syntax, and "
                "ask the user to type 'fix-all' to re-run bootstrap. Common "
                "causes: missing/extra commas, unquoted keys, trailing commas."
            ),
            "plugin": "bootstrap",
            "persist_across_sessions": True,
        })
    if layered_manifest:
        action_entries = []
        ok_entries = []
        quiet_entries = []
        failures = _process_manifest(
            layered_manifest, current_os, data_dir, plugin_root,
            action_entries, ok_entries, plugin_name="config",
            project_dir=args.project_dir,
            quiet_entries=quiet_entries, shared_lib_links=shared_lib_links,
        )
        # _reprefix, not an f-string: an f-string produces a plain str and
        # drops the entry's authored short label and detail (see records.Entry).
        prefixed_action = [_reprefix(e, "config: ") for e in action_entries]
        prefixed_ok = [_reprefix(e, "config: ") for e in ok_entries]
        bootstrap_action_entries.extend(prefixed_action)
        bootstrap_ok_entries.extend(prefixed_ok)
        # Log-only (displayed in aggregate by Step 4c). bootstrap_action_entries
        # feeds both the log and bootstrap's display section, so quiet entries
        # ride in the log-only list instead -- see _ManifestContext.quiet.
        bootstrap_quiet_entries.extend(_reprefix(e, "config: ") for e in quiet_entries)
        if failures:
            all_failures.extend(failures)

    # Step 3d: Process project_venv from layered manifest (needs --project-dir)
    project_venv_def = layered_manifest.get("project_venv") if layered_manifest else None
    if project_venv_def and args.project_dir:
        pv_action, pv_ok, pv_failures = _process_project_venv(
            project_venv_def, args.project_dir)
        bootstrap_action_entries.extend(_reprefix(e, "config: ") for e in pv_action)
        bootstrap_ok_entries.extend(_reprefix(e, "config: ") for e in pv_ok)
        all_failures.extend(pv_failures)

    # Step 3e: Process the layered env.json manifest (identity-bearing
    # personalization; bootstrap-env-refactor spec 4.4). Placement is
    # load-bearing: immediately AFTER the layered bootstrap.json manifest
    # (env_vars -> tools -> fonts -> path -> project_venv have all run, so
    # every variable and binary a personalization entry references already
    # exists) and BEFORE plugin manifests (Step 4). Gated by the
    # env_state.json stamp -- see _process_env_pass. env.json failures never
    # affect the bootstrap.json phases above: software still provisions;
    # personalization refuses to guess.
    env_action_entries = []
    env_ok_entries = []
    env_failures = _process_env_pass(
        args.project_dir, current_os, data_dir, plugin_root,
        env_action_entries, env_ok_entries, engine_version=version,
    )
    bootstrap_action_entries.extend(_reprefix(e, "env: ") for e in env_action_entries)
    bootstrap_ok_entries.extend(_reprefix(e, "env: ") for e in env_ok_entries)
    if env_failures:
        all_failures.extend(env_failures)

    # Add bootstrap's own section to display
    display_sections.append((bootstrap_label, list(bootstrap_action_entries), list(bootstrap_ok_entries)))

    # Step 4: Process enabled plugins (auto-discovered via bootstrap.json presence)
    registry_path = os.path.join(plugins_dir, "installed_plugins.json")

    # In dev layout the registry lists all repo plugins, not just enabled ones.
    # Build an enabled_refs filter from settings.json + production registry so only
    # actively-enabled plugins are bootstrapped. Production layout is unaffected
    # (its registry is already authoritative).
    home = os.environ.get("HOME") or os.path.expanduser("~")
    prod_registry = os.path.normpath(os.path.join(home, ".claude", "plugins", "installed_plugins.json"))
    is_dev_layout = os.path.normpath(registry_path) != prod_registry
    enabled_refs = _load_enabled_refs(args.project_dir) if is_dev_layout else None

    # Registry-v2 fallback filter: newer Claude Code keeps installed_plugins.json
    # at {"plugins": {}} for marketplace installs, so cache-derived fallback
    # entries need their own enablement source (settings enabledPlugins). In the
    # dev layout enabled_refs already carries it; in production load it here.
    fallback_refs = enabled_refs if enabled_refs is not None else _load_enabled_refs(args.project_dir)

    enabled_plugins, cache_changed = list_enabled_plugins(
        config, registry_path, plugins_dir, enabled_refs,
        fallback_enabled_refs=fallback_refs,
    )
    if cache_changed:
        from .config import save_config
        save_config(data_dir, config)

    # Sort: bootstrap plugin first, then same-marketplace plugins, then others
    def _plugin_sort_key(pi):
        if pi.name == boot_plugin_name and pi.marketplace == marketplace_name:
            return (0, pi.name)
        if pi.marketplace == marketplace_name:
            return (1, pi.name)
        return (2, pi.name)

    enabled_plugins.sort(key=_plugin_sort_key)
    deferred_plugin_logs = []
    processed_plugin_refs = set()

    for plugin_info in enabled_plugins:
        ref = f"{plugin_info.marketplace}:{plugin_info.name}" if plugin_info.marketplace else plugin_info.name
        processed_plugin_refs.add(ref)
        _bootstrap_single_plugin(
            plugin_info, current_os, data_dir, all_failures,
            log_success, display_sections, deferred_plugin_logs, args,
            engine_version=version, shared_lib_links=shared_lib_links,
            recorder=recorder,
        )

    # Step 4b: Re-scan for plugins installed during Steps 3c/4
    # (e.g. a layered bootstrap.json declared a plugin to install via `claude plugin install`)
    phase2_plugins, phase2_cache_changed = list_enabled_plugins(
        config, registry_path, plugins_dir, enabled_refs,
        fallback_enabled_refs=fallback_refs,
    )
    if phase2_cache_changed:
        from .config import save_config
        save_config(data_dir, config)

    new_plugins = [
        pi for pi in phase2_plugins
        if (f"{pi.marketplace}:{pi.name}" if pi.marketplace else pi.name)
           not in processed_plugin_refs
    ]
    new_plugins.sort(key=_plugin_sort_key)
    for plugin_info in new_plugins:
        _bootstrap_single_plugin(
            plugin_info, current_os, data_dir, all_failures,
            log_success, display_sections, deferred_plugin_logs, args,
            engine_version=version, shared_lib_links=shared_lib_links,
            recorder=recorder,
        )

    # Step 4b2: Self-register bootstrap-dependent plugins for auto-update.
    # _phase_plugins only manages DECLARED plugins[] entries; everything else
    # fell back to Claude Code's own autoUpdate, which reads the marketplace
    # clone BEFORE this pass refreshes it -- one restart behind every publish.
    # Any processed plugin that ships a bootstrap.json but has no plugins[]
    # entry anywhere (merged layers or another plugin's manifest) is appended
    # to ~/.claude/bootstrap.local.json with install: "manual" (update-only:
    # an uninstalled plugin stays uninstalled; uninstalling is the only
    # opt-out, and a deleted entry is re-added next pass). Entries take effect
    # from the NEXT pass -- the plugin was just provisioned this one, so
    # nothing is stale in the gap. Rationale + write-target choice:
    # bootstrap_lib/self_register.py module docstring.
    from .self_register import declared_plugin_ids, ensure_self_registration
    sr_declared_refs, sr_declared_names = declared_plugin_ids(layered_manifest)
    sr_candidates = []
    for pi in enabled_plugins + new_plugins:
        pi_manifest_path = os.path.join(pi.install_path, "bootstrap.json")
        if not pi.marketplace or not os.path.isfile(pi_manifest_path):
            continue
        sr_candidates.append(f"{pi.marketplace}:{pi.name}")
        try:
            with open(pi_manifest_path, "r") as f:
                p_refs, p_names = declared_plugin_ids(json.load(f))
            sr_declared_refs |= p_refs
            sr_declared_names |= p_names
        except (json.JSONDecodeError, OSError):
            pass  # parse failure already surfaced by the per-plugin pass
    sr_actions, sr_oks = ensure_self_registration(
        os.path.join(home, ".claude", "bootstrap.local.json"),
        sr_candidates, sr_declared_refs, sr_declared_names,
    )
    # Built by ensure_self_registration as plain lists, so they carry no
    # RecordingList; record them explicitly rather than let the one code path
    # that bypasses the list-level hook go unrecorded.
    _record_entries(recorder, "action", sr_actions, section="self-register")
    _record_entries(recorder, "ok", sr_oks, section="self-register")
    if sr_actions or sr_oks:
        sr_label = f"{bootstrap_label} self-register"
        display_sections.append((sr_label, sr_actions, sr_oks))
        sr_log = sr_actions + (sr_oks if log_success else [])
        if sr_log and not args.console:
            # Deferred to Step 6 (like plugin logs, the Step 4c sweep, and the
            # Step 4d notice): writing now would leak the block back through
            # Step 5's shell_content read, so every self-register line would be
            # emitted twice -- once from its display section, once verbatim from
            # the log.
            deferred_plugin_logs.append((data_dir, sr_label, sr_log))

    # Step 4c: Shared-lib convergence sweep. Every owner has now published (Steps
    # 4 + 4b), so re-link all consumers in one go -- a consumer processed before
    # its owner no longer waits for the next session. Silent in steady state
    # (already-linked consumers report "cached" -> verbose-only); only a genuinely
    # converged or failed link surfaces. See _shared_lib_convergence_sweep.
    sweep_actions, sweep_quiets, sweep_oks, sweep_failures = _shared_lib_convergence_sweep(
        enabled_plugins + new_plugins, data_dir, shared_lib_links,
    )
    if sweep_failures:
        all_failures.extend(sweep_failures)
    # ONE aggregated display line for every shared-lib success in the pass, from
    # BOTH emission sites (per-plugin manifest phase + this sweep), deduped and
    # grouped by lib -- the per-plugin lines are log-only (see _SharedLibLinkLog).
    # Failures are untouched: they stay in sweep_actions, per-plugin and loud.
    link_summary = shared_lib_links.summary()
    if link_summary:
        sweep_actions = sweep_actions + [link_summary]
    _record_entries(recorder, "action", sweep_actions, section="shared-libs")
    _record_entries(recorder, "quiet", sweep_quiets, section="shared-libs")
    _record_entries(recorder, "ok", sweep_oks, section="shared-libs")
    if sweep_actions or sweep_quiets or sweep_oks:
        sweep_label = f"{bootstrap_label} shared-libs"
        display_sections.append((sweep_label, sweep_actions, sweep_oks))
        sweep_log = sweep_actions + sweep_quiets + (sweep_oks if log_success else [])
        if sweep_log and not args.console:
            # Deferred to Step 6 (like plugin logs and the Step 4d notice):
            # writing now would leak the block back through Step 5's
            # shell_content read, so the block would ALSO be emitted verbatim
            # into the display -- duplicating the aggregate line and dragging
            # the log-only quiet entries (with their .pth paths) onto it.
            deferred_plugin_logs.append((data_dir, sweep_label, sweep_log))

    # Step 4d: Reload/restart advisory. Any plugin that ENTERED the registry during
    # this pass -- a layered `plugins:` install (Step 3c), a per-plugin install, or
    # a script install (Step 4b) -- is not yet loaded by Claude Code (it loaded
    # plugins at session start, before this hook installed them). We detect them by
    # diffing the registry (before Step 3c vs now), NOT Step 4b's new_plugins, which
    # misses layered installs absorbed by Step 4. A plugin merely updated at session
    # start was already loaded by the restart that updated it, so it is not noticed.
    # These are informational display lines, NOT action-required items: whether and
    # when to restart is the user's call, so the notice rides in the normal display
    # output with no relay directive telling Claude to surface it.
    # Toggle off via config "notify_reload_needed".
    if config.get("notify_reload_needed", True):
        newly_installed = _resolve_newly_installed(
            installed_refs_before, _read_installed_plugins(_registry_for_diff),
        )
        notices = [a for a in (
            _reload_advice(newly_installed),
            # Bootstrap self-staleness: a newer bootstrap is cached but this session
            # loaded the old one. /reload-plugins won't re-fire its SessionStart pass.
            _bootstrap_stale_advice(version, boot_plugin_name, marketplace_name, prod_registry,
                                    data_dir=data_dir),
        ) if a]
        for advice in notices:
            advice_label = f"{bootstrap_label} notice"
            display_sections.append((advice_label, [advice], []))
            if not args.console:
                # Deferred to Step 6 (like plugin logs): writing now would leak
                # the block back through Step 5's shell_content read and the
                # notice would appear twice in the emitted display.
                deferred_plugin_logs.append((data_dir, advice_label, [advice]))

    # Step 5: Read shell log entries BEFORE writing any engine entries to the log.
    # Plugin log writes are deferred to step 6 to avoid the bootstrap plugin's
    # ok_entries leaking back through shell_content (its data_dir == engine data_dir).
    if not args.console:
        shell_content = _read_new_log_entries(data_dir, start_time=start_time)
    else:
        shell_content = ""  # Console mode: shell already printed its entries

    # Step 6: Write log entries (bootstrap + plugins) — after reading shell entries
    # Skip in console mode — no file writes.
    #
    # ok_entries remain gated on log_success. That gate still does real work
    # here -- the log IS read back (_read_new_log_entries), so an ok entry
    # written now would reappear in the next pass's display -- but it is no
    # longer a RETENTION decision: every ok entry lands in the pass record
    # regardless (records.py). Turning it on or off changes what is easy to
    # read in bootstrap.log, never what is kept.
    bootstrap_log_entries = bootstrap_action_entries + bootstrap_quiet_entries + (bootstrap_ok_entries if log_success else [])
    if bootstrap_log_entries and not args.console:
        write_log_block(data_dir, bootstrap_label, bootstrap_log_entries, start_time=start_time)
    for plugin_data_dir, plugin_label, plugin_log_entries in deferred_plugin_logs:
        if plugin_log_entries and not args.console:
            write_log_block(plugin_data_dir, plugin_label, plugin_log_entries, start_time=start_time)

    # Step 7: Build display from sections — actions only, never ok entries.
    # ok entries are written to the log file (gated by log_success) for debugging
    # via `tail bootstrap.log`, but never surface in the user-facing hook output.
    display_lines = []
    for header, actions, _oks in display_sections:
        if not actions:
            continue
        # Width-limited like every other collated surface. An entry that would
        # overflow renders its AUTHORED short label (Entry.short, set via
        # _append_detail(display=...)) or a whole clause derived at a separator
        # -- never a mid-word cut. The full diagnostic stays in bootstrap.log
        # and the pass record.
        display_lines.append(f"--- {header}: {_numbered(actions)} ---")

    # Step 7b: Elevation queue -> ONE per-OS remediation script. Harvest every
    # `elevation` descriptor deferred during this pass (apt packages, elevated
    # commands, a missing-brew installer signal), regenerate queue.json when the
    # queue is non-empty, and DELETE a stale queue when it is empty (the ops
    # succeeded, so the item clears). When a queue was written, append ONE
    # aggregated fix-all item naming what it covers; the per-item
    # needs_elevation failures are suppressed by the message layer, which the
    # aggregate speaks for. See bootstrap_lib/fix_queue.py and
    # analysis-dividing-line.md section 4.3.
    #
    # On a --fix-all run (the user TYPED 'fix-all' -- explicit consent for
    # elevation) the step additionally LAUNCHES the fix runner, waits for it,
    # and on success spawns a re-check pass so the deferred items clear in the
    # same fix-all cycle. SessionStart/background passes never launch. Plain
    # --console debug runs (no --fix-all) skip the step entirely, preserving
    # their "no file writes" contract.
    if not args.console or args.fix_all:
        if _elevation_step(all_failures, current_os, data_dir, args, plugin_root,
                           bootstrap_label):
            # The runner completed; a re-check pass was spawned and has emitted
            # its own results -- this pass is done. Record first: the re-check
            # records the POST-fix state, so without this the pre-fix failures
            # that motivated the run would exist nowhere.
            _record_failures(recorder, all_failures)
            return

    # Record every failure dict VERBATIM, before any of it is rendered, and
    # before the console branch returns. A failure carries far more than its
    # display line -- agent_msg, user_msg, install_state, the elevation
    # descriptor, the remediation command -- and every rendered surface keeps
    # only a projection. Items the elevation aggregate speaks for are suppressed
    # from BOTH message surfaces entirely (_visible_failures), so without this
    # they would exist nowhere on disk.
    _record_failures(recorder, all_failures)

    if args.console:
        # Console mode: plain text to stdout, no JSON
        for line in display_lines:
            print(line)
        if all_failures:
            print(f"\n{bootstrap_label} -> {len(all_failures)} failure(s):")
            for f in all_failures:
                print(f"  - [{f['type']}] {f.get('name') or ''}")
                # The message is where the actionable detail lives -- for the
                # elevation_script item on a fix-all run it carries the launch
                # OUTCOME (UAC declined / runner exit code / timed out), which
                # this summary used to drop entirely: a failed fix-all printed
                # nothing but the item's own name, leaving the conversation
                # blind to WHY (observed live, 0.49.0).
                detail = f.get("message") or f.get("user_msg") or ""
                for line in detail.splitlines():
                    print(f"      {line}")
        # Console writes no log and no stamps -- that is its contract, and it is
        # why a console pass used to leave no trace of what it changed. An
        # append-only record breaks neither rule, and a console pass against a
        # wedged machine is exactly the state worth having evidence of.
        recorder.record("emit", "\n".join(display_lines), channel="console")
        return

    # Build final display: shell entries + section entries
    parts = []
    if shell_content:
        # The shell hook's own log block, read back from bootstrap.log. Recorded
        # here so the record covers the whole pass, not just its Python half.
        recorder.record("shell", shell_content)
        parts.append(shell_content)
    parts.extend(display_lines)
    display_content = "\n".join(parts)

    # Update the log display marker
    _update_display_marker(data_dir)

    # Export BOOTSTRAP_BIN_<TOOL> env vars to $CLAUDE_ENV_FILE so plugin
    # scripts can invoke recorded tools directly by absolute path. No-op
    # when CLAUDE_ENV_FILE isn't set (e.g. console mode, tests). See
    # docs/planning/bootstrap/tool-resolution-redesign.md.
    from . import tool_paths as _tool_paths
    _tool_paths.export_tool_env_vars(data_dir)

    # Step 8: Emit results
    output_file = os.path.join(data_dir, "bootstrap_display.pending") if args.background else None
    persistent_alert_path = os.path.join(data_dir, "bootstrap_alert.json")
    has_persistent = any(f.get("persist_across_sessions") for f in all_failures)
    persistent_output_file = persistent_alert_path if (args.background and has_persistent) else None

    if all_failures:
        emit_failure_response(
            all_failures, current_os, display_content,
            label=bootstrap_label, output_file=output_file,
            persistent_output_file=persistent_output_file,
            recorder=recorder,
        )
        # Clear this project's cooldown stamp so the next SessionStart re-runs
        # bootstrap instead of silently throttling. The shell hook stamps the
        # cooldown optimistically before invoking the engine; on failure we
        # roll that back so out-of-band fixes (user runs winget themselves,
        # restarts their IDE, edits config) are picked up on the next session
        # rather than waiting out the throttle window.
        _clear_project_cooldown(data_dir, args.project_dir)
    else:
        if display_content:
            emit_success_response(
                display_content, label=bootstrap_label, output_file=output_file,
                recorder=recorder,
            )
        # else: nothing to show — silent exit (no file written in background mode)

        # Re-stamp the cooldown after a clean pass. Bootstrap itself may have
        # rewritten installed_plugins.json during the pass (plugin installs,
        # ensure_registry_scope); the shell's registry-mtime bypass compares
        # those files against the stamp written BEFORE the engine ran, so
        # bootstrap-authored writes would re-arm a full pass on EVERY session.
        # Refreshing the stamp keeps it newer than our own writes while leaving
        # the bypass armed for genuine Claude-Code-authored registry changes
        # (which land after this pass finishes).
        _restamp_project_cooldown(data_dir, args.project_dir)

    # Clean up stale persistent alert file when no persistent failures remain.
    # This is what makes the alert disappear once the user fixes the underlying
    # issue and the engine confirms the fix on a subsequent run.
    if not has_persistent:
        try:
            os.remove(persistent_alert_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # Stamp the engine version that just COMPLETED a pass (global stamp). This is
    # the single-session update protocol's loop guard: the UserPromptSubmit
    # harvest (bootstrap_lib/harvest.py) only launches a newer engine when the
    # installed bootstrap version > engine_ran_version, and the harvested engine
    # writes its OWN version here on completion — so it can never re-trigger
    # itself. Reached on both clean and check-failure passes (the engine still
    # EXECUTED); only a crash (handled in main()) skips it, and console mode
    # returns earlier so manual --console debug runs never stamp. See
    # references/plugin-reload-lifecycle.md "Single-session update protocol".
    #
    # MONOTONIC by semver: an OLDER engine completing a pass must never regress
    # the stamp. Under rapid restarts a resident 0.61.0 engine can win the lock
    # while the harvest-launched 0.62.0 stands down; an unconditional write
    # there re-opened the update as un-run forever. An unreadable/garbage stored
    # value parses as (0,0,0); ties still rewrite (idempotent).
    if version:
        ran_stamp = global_stamp(data_dir, "engine_ran_version")
        if _parse_semver(version) >= _parse_semver(ran_stamp.read() or "0"):
            ran_stamp.write(version)

    # A pass COMPLETED, so any transient partial-download import race has resolved.
    # Clear the retry markers so the UserPromptSubmit harvest stops relaunching
    # (set by _defer_transient_retry / harvest.run_harvest on a crash+relaunch).
    global_stamp(data_dir, "import_retry_pending").clear()
    global_stamp(data_dir, "import_retry_launched").clear()

    # Absorb the installed/enabled plugin-set snapshot this pass just provisioned
    # (bootstrap_lib/plugins_snapshot.py). The UserPromptSubmit mid-session
    # install relaunch (harvest.run_registry_relaunch) compares live state
    # against this stamp, so writing it at COMPLETION (a) seeds the mechanism on
    # the first pass after it ships and (b) keeps bootstrap-authored registry
    # writes during the pass from self-triggering a relaunch -- the same
    # rationale as _restamp_project_cooldown above. Best-effort by design.
    try:
        from .plugins_snapshot import stamp_plugins_state
        stamp_plugins_state(data_dir)
    except Exception:
        pass


def _elevation_step(all_failures, current_os, data_dir, args, plugin_root,
                    label="bootstrap"):
    """Step 7b: deferred-op queue -> queue.json (+ interactive launch on fix-all).

    Harvests the pass's elevation descriptors, writes/clears queue.json and its
    run-it-yourself shim, and appends the aggregated elevation_script failure
    when a queue exists.

    On a --fix-all run (interactive, user-consented) with a non-empty queue,
    the engine LAUNCHES the fix runner itself and waits (bounded) for it:

    - Windows: `Start-Process -Verb RunAs -Wait` on the engine's interpreter --
      the UAC prompt is a direct consequence of the user's typed 'fix-all'. On
      success a re-check pass is spawned (the engine re-runs WITHOUT --fix-all,
      so it can never loop the prompt) and this function returns True: the
      caller must return without emitting, the re-check pass owns the output.
      On decline/failure/timeout the aggregated item falls back to the shim
      instruction, prefixed with the launch outcome.
    - Unix: no launch is attempted (launch_fix_runner returns None) -- the
      fix-all run has no TTY for a sudo or secret prompt, so the shim
      instruction stands.

    SessionStart/background passes never pass --fix-all, so their behavior is
    exactly the pre-launch behavior: write the queue, surface the item.

    Returns True when a re-check pass was spawned (caller stops), else False.
    """
    from .fix_queue import (
        queue_from_failures, write_or_clear_queue, fix_queue_failure,
        launch_fix_runner, has_actionable,
    )
    tasks = queue_from_failures(all_failures, current_os)
    try:
        path = write_or_clear_queue(tasks, data_dir, current_os)
    except RuntimeError as exc:
        # render_queue raises when bash can't be resolved at write time and the
        # queue holds command/brew tasks (shell strings the runner needs bash
        # for). A background SessionStart hook must DEGRADE, never crash: an
        # uncaught exception here kills the whole pass's output, so every OTHER
        # failure's remediation is lost with it. So: don't append the aggregate
        # (its absence lets _visible_failures surface the per-task needs_elevation
        # failures raw, with their own agent_msg/manual commands -- designed-for
        # degradation), and surface the bash-missing explanation itself via a
        # generic failure the emit_failure_response fallback branch renders (its
        # text already carries the actionable "Install Git for Windows..." fix).
        all_failures.append({
            "type": "fix_queue_write",
            "plugin": "bootstrap",
            "message": str(exc),
            "agent_msg": str(exc),
            "user_msg": str(exc),
            "persist_across_sessions": True,
        })
        return False
    if not path:
        return False

    if not has_actionable(tasks):
        # Everything queued is opportunistic housekeeping (e.g. the dead-PATH
        # prune): worth fixing, not worth an admin nag of its own. The queue
        # and shim stay on disk, so the work rides along the next time a real
        # deferral needs the runner (or the user runs the shim by hand) -- but
        # nothing is surfaced: no aggregate, no fix-all launch, and the covered
        # failures are dropped so they cannot surface as raw per-item nags.
        all_failures[:] = [f for f in all_failures if not _opportunistic(f)]
        return False

    launch_detail = None
    if getattr(args, "fix_all", False):
        result = launch_fix_runner(path, current_os, tasks=tasks)
        if result is not None:
            if result.succeeded:
                note = (f"{label} -> fix runner completed successfully "
                        f"({result.detail}); running re-check pass")
                if args.console:
                    print(note)
                else:
                    from .log import write_log_block
                    write_log_block(data_dir, f"{label} elevation", [note])
                _spawn_recheck_pass(args, plugin_root)
                return True
            launch_detail = result.detail

    item = fix_queue_failure(tasks, current_os, data_dir,
                             launch_detail=launch_detail)
    if current_os == "windows" and not getattr(args, "fix_all", False):
        # Name the consented invocation for Claude: on 'fix-all' it re-runs the
        # engine with --fix-all, and the engine launches the runner itself (so
        # the UAC prompt is a consequence of the user's request, not a surprise).
        hook = os.path.join(plugin_root, "hooks", "sessionstart",
                            "session-bootstrap.sh")
        item["fix_all_cmd"] = f'bash "{hook}" --console --fix-all'
        item["agent_msg"] += (
            f" The fix-all invocation is: `{item['fix_all_cmd']}`."
        )
    all_failures.append(item)
    return False


def _spawn_recheck_pass(args, plugin_root):
    """Re-run the engine (same mode, WITHOUT --fix-all) after a successful
    elevated launch, so the elevated items re-check and clear in the same
    fix-all cycle.

    Dropping --fix-all is the loop guard: even if the re-check still finds a
    non-empty elevation queue (script "succeeded" but a check still fails),
    the child writes the script and surfaces the manual item -- it never
    launches or prompts again. Console output is inherited (Claude sees the
    re-check results); background mode writes a fresh
    bootstrap_display.pending. The child also owns the end-of-pass
    bookkeeping (cooldown clear/restamp, engine_ran_version stamp).

    Releases proc_lock's single-instance lock BEFORE spawning: the child is a
    full second bootstrap_engine.py process with the SAME --data-dir, and
    this (parent) process is still inside its own engine_lock() while it
    waits on subprocess.run -- without releasing first, the child would see
    our still-alive PID as the lock holder and stand down without doing its
    re-check. Safe because this pass has no more work after the child exits
    (the caller returns immediately).
    """
    from .proc_lock import release_lock
    release_lock(args.data_dir)

    import subprocess
    cmd = [
        sys.executable,
        os.path.join(plugin_root, "engine", "bootstrap_engine.py"),
        "--plugin-root", plugin_root,
        "--data-dir", args.data_dir,
    ]
    if args.project_dir:
        cmd += ["--project-dir", args.project_dir]
    if args.verbose:
        cmd += ["--verbose"]
    if args.console:
        cmd += ["--console"]
    if args.background:
        cmd += ["--background"]
    subprocess.run(cmd)


def _plugin_data_dir(data_dir, plugin_info):
    """Per-plugin data dir keyed by the plugin's OWN marketplace, not the engine's.

    ``data_dir`` is the engine's own data dir (``<root>/data/<engine-mkt>/bootstrap``),
    so its grandparent is the shared data root. Earlier code keyed by
    ``<root>/data/<engine-mkt>/<plugin-name>`` -- the ENGINE's marketplace plus the
    bare plugin name. That collides when two marketplaces ship same-named plugins
    (e.g. a fork alongside upstream both having ``bootstrap`` / ``p4-kit``): each
    engine iterates ALL installed plugins and writes the foreign-marketplace plugin
    into its own tree, so the per-plugin data dir AND the derived ``_shared_libs``
    sync target last-writer-win between the two copies. Keying by
    ``plugin_info.marketplace`` (already distinct per marketplace) namespaces them
    apart. Falls back to the engine's marketplace for plugins with no recorded
    marketplace (e.g. ``--plugin-dir`` installs). No-op for single-marketplace
    setups, where a plugin's own marketplace equals the engine's.
    """
    data_root = os.path.dirname(os.path.dirname(data_dir))
    engine_mkt = os.path.basename(os.path.dirname(data_dir))
    mkt = getattr(plugin_info, "marketplace", "") or engine_mkt
    return os.path.join(data_root, mkt, plugin_info.name)


def _plugin_log_label(plugin_info, plugin_data_dir, data_dir, engine_version=""):
    """``<name>@<version>`` for a plugin's own log section.

    Disambiguated for bootstrap ITSELF (the one plugin whose data dir IS the
    engine data dir): its per-plugin label carries the REGISTRY version while
    bootstrap's other log sections carry the RUNNING binary's version, so one
    pass could emit two "bootstrap@X" headers with different X and no way to
    tell which was which. When the two differ, name both.
    """
    if not plugin_info.version:
        return plugin_info.name
    label = f"{plugin_info.name}@{plugin_info.version}"
    is_self = (
        os.path.normcase(os.path.normpath(plugin_data_dir))
        == os.path.normcase(os.path.normpath(data_dir))
    )
    if is_self and engine_version and engine_version != plugin_info.version:
        return f"{label} (engine {engine_version})"
    return label


def _bootstrap_single_plugin(
    plugin_info, current_os, data_dir, all_failures,
    log_success, display_sections, deferred_plugin_logs, args,
    engine_version="", shared_lib_links=None, recorder=None,
):
    """Process a single plugin's bootstrap.json manifest.

    Extracted from the Step 4 loop body to allow reuse in Step 4b (Phase 2 re-scan).
    Mutates the shared containers in place (same pattern as the original inline code).

    `shared_lib_links` is the pass-level _SharedLibLinkLog; shared-lib publish/link
    successes are recorded there (one aggregated display line in Step 4c) instead
    of producing a per-plugin display entry each.

    `engine_version` is the running bootstrap plugin's version; a manifest can
    declare ``requires_bootstrap`` to be skipped (with an "update bootstrap" note)
    when this engine is too old to process it.
    """
    plugin_manifest_path = os.path.join(plugin_info.install_path, "bootstrap.json")
    if not os.path.isfile(plugin_manifest_path):
        return

    # Per-plugin data dir and cache -- keyed by the plugin's OWN marketplace so a
    # fork installed alongside upstream doesn't collide on same-named plugins.
    plugin_data_dir = _plugin_data_dir(data_dir, plugin_info)
    os.makedirs(plugin_data_dir, exist_ok=True)

    # One malformed plugin manifest must not kill the pass for every other
    # plugin — emit a manifest_parse failure (same pattern as the layered
    # manifests in _load_layered_manifests) and move on.
    try:
        with open(plugin_manifest_path, "r") as f:
            plugin_manifest = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        kind = "JSON parse error" if isinstance(e, json.JSONDecodeError) else "read error"
        error = f"{kind}: {e}"
        all_failures.append({
            "type": "manifest_parse",
            "path": plugin_manifest_path,
            "message": error,
            "agent_msg": (
                f"The bootstrap manifest at {plugin_manifest_path} failed to "
                f"parse ({error}). Open the file, fix the JSON syntax, and "
                "ask the user to type 'fix-all' to re-run bootstrap. Common "
                "causes: missing/extra commas, unquoted keys, trailing commas."
            ),
            "plugin": plugin_info.name,
            "persist_across_sessions": True,
        })
        entry = f"bootstrap.json: PARSE FAILED - {error}"
        plugin_label = f"{plugin_info.name}@{plugin_info.version}" if plugin_info.version else plugin_info.name
        deferred_plugin_logs.append((plugin_data_dir, plugin_label, [entry]))
        plugin_display_header = f"{plugin_info.marketplace}:{plugin_info.name}@{plugin_info.version}" if plugin_info.marketplace else plugin_label
        display_sections.append((plugin_display_header, [entry], []))
        return

    # Forward-compat guard: a manifest can declare the minimum bootstrap-engine
    # version it needs (e.g. it uses a `scoop:` fulfillment older engines can't
    # process). If THIS engine is too old, skip the manifest entirely and tell the
    # user to update bootstrap -- rather than misprocessing fields we don't grok.
    required_bootstrap = plugin_manifest.get("requires_bootstrap")
    if required_bootstrap and engine_version and not _version_satisfies(engine_version, required_bootstrap):
        msg = (f"skipped: requires bootstrap >= {required_bootstrap}, but bootstrap "
               f"{engine_version} is running — update the bootstrap plugin")
        plugin_label = f"{plugin_info.name}@{plugin_info.version}" if plugin_info.version else plugin_info.name
        deferred_plugin_logs.append((plugin_data_dir, plugin_label, [msg]))
        plugin_display_header = (f"{plugin_info.marketplace}:{plugin_info.name}@{plugin_info.version}"
                                 if plugin_info.marketplace else plugin_label)
        display_sections.append((plugin_display_header, [msg], []))
        all_failures.append({
            "type": "bootstrap_outdated",
            "name": plugin_info.name,
            "plugin": plugin_info.name,
            "message": msg,
            "agent_msg": (
                f"Plugin {plugin_info.name} requires the plugins-kit:bootstrap plugin to be "
                f">= {required_bootstrap}, but {engine_version} is installed, so its setup was "
                f"skipped. Update bootstrap (restart Claude / the IDE so the new SessionStart "
                f"engine loads, or run `/plugin update`), then re-run."
            ),
            "persist_across_sessions": True,
        })
        return

    # Per-plugin entry lists (written to plugin's own log, and -- via the
    # RecordingList -- to the pass record, tagged with this plugin).
    from .records import entry_list as _entry_list
    plugin_action_entries = _entry_list(recorder, "action", plugin=plugin_info.name)
    plugin_ok_entries = _entry_list(recorder, "ok", plugin=plugin_info.name)

    # Version change detection. Skipped for bootstrap itself: its plugin_data_dir
    # IS the engine data_dir, and Step 2b already wrote last_version there with
    # the RUNNING engine's version. When a dev tree runs against a cached
    # registry the two versions differ, and writing the registry version here
    # produced a flip-flopping "updated: X -> Y" entry every pass (B14).
    if plugin_info.version and (
        os.path.normcase(os.path.normpath(plugin_data_dir))
        != os.path.normcase(os.path.normpath(data_dir))
    ):
        # Per-plugin last_version stamp — same stamps-module convention as the
        # engine's own (Step 2b), just scoped to this plugin's data dir.
        from .stamps import plugin_stamp
        last_version_stamp = plugin_stamp(plugin_data_dir, "last_version")
        last_version = last_version_stamp.read()
        if last_version and last_version != plugin_info.version:
            plugin_action_entries.append(f"updated: {last_version} -> {plugin_info.version}")
        elif not last_version:
            plugin_action_entries.append(f"installed: {plugin_info.version}")
        last_version_stamp.write(plugin_info.version)

    # Project config phase (per-CWD discovery, before config phase)
    # project_detected: True when project found or no project_config section (non-gated plugin)
    project_detected = True
    project_config_section = plugin_manifest.get("project_config")
    if project_config_section:
        project_config_failures = []
        project_detected = _process_project_config(
            project_config_section, plugin_data_dir, plugin_info.install_path,
            plugin_action_entries, ok_entries=plugin_ok_entries, plugin_name=plugin_info.name,
            failures=project_config_failures,
        )
        if project_config_failures:
            all_failures.extend(project_config_failures)

    # Config phase
    config_section = plugin_manifest.get("config")
    if config_section:
        config_failures = _process_config(
            config_section, plugin_data_dir, plugin_info.install_path,
            plugin_action_entries, ok_entries=plugin_ok_entries, plugin_name=plugin_info.name,
            project_detected=project_detected,
        )
        if config_failures:
            all_failures.extend(config_failures)

    action_entries = []
    ok_entries = []
    # Recorded directly: unlike action/ok below, quiet entries are never
    # extended into a RecordingList -- they go straight to the plugin log.
    quiet_entries = _entry_list(recorder, "quiet", plugin=plugin_info.name)
    failures = _process_manifest(
        plugin_manifest, current_os, plugin_data_dir, plugin_info.install_path,
        action_entries, ok_entries, plugin_name=plugin_info.name,
        project_dir=getattr(args, 'project_dir', None),
        project_detected=project_detected,
        quiet_entries=quiet_entries, shared_lib_links=shared_lib_links,
    )
    plugin_action_entries.extend(action_entries)
    plugin_ok_entries.extend(ok_entries)

    if failures:
        all_failures.extend(failures)

    # Collect plugin log info (deferred — written after reading shell entries)
    plugin_label = _plugin_log_label(plugin_info, plugin_data_dir, data_dir, engine_version)
    # quiet_entries are logged unconditionally (they ARE remediations) but stay
    # out of the display section below -- Step 4c speaks for them in aggregate.
    plugin_log_entries = plugin_action_entries + quiet_entries + (plugin_ok_entries if log_success else [])
    deferred_plugin_logs.append((plugin_data_dir, plugin_label, plugin_log_entries))

    # Add plugin section to display
    plugin_display_header = f"{plugin_info.marketplace}:{plugin_info.name}@{plugin_info.version}" if plugin_info.marketplace else plugin_label
    display_sections.append((plugin_display_header, list(plugin_action_entries), list(plugin_ok_entries)))


class _SharedLibLinkLog:
    """Pass-level collector for shared-lib publish/link SUCCESSES.

    Shared-lib links fire for every plugin that imports a shared lib, from two
    emission sites (each plugin's manifest phase and the Step 4c convergence
    sweep). Reported per-plugin, that is one long display line per plugin, each
    repeating the lib name and a .pth path -- verbose and unimportant. The
    events are collected here instead and rendered as ONE line for the pass:

        shared-libs: linked bootstrap_lib (bootstrap, git-kit), p4kit_vcs (p4-kit)

    Paths stay in the per-plugin log entries (debugging substrate); they never
    reach the display. FAILURES never come here -- they stay per-plugin, loud,
    and keep populating the fix-all failure list.
    """

    def __init__(self):
        self._linked = {}   # lib name -> [plugin short name, ...], first-seen order
        self._synced = []   # lib names published by their owner this pass
        self._seen = set()  # (lib, plugin) pairs, so both emission sites dedupe

    def record(self, status, lib, plugin):
        """Record one successful ``published`` (owner sync) or ``linked`` event."""
        if status == "published":
            if lib not in self._synced:
                self._synced.append(lib)
            return
        key = (lib, plugin)
        if key in self._seen:
            return
        self._seen.add(key)
        self._linked.setdefault(lib, []).append(plugin)

    def summary(self):
        """The one aggregated display entry, or "" when nothing happened."""
        parts = []
        if self._synced:
            parts.append("synced " + ", ".join(self._synced))
        if self._linked:
            parts.append("linked " + ", ".join(
                f"{lib} ({', '.join(plugins)})" for lib, plugins in self._linked.items()
            ))
        return "; ".join(parts)


def _shared_lib_convergence_sweep(plugins, data_dir, link_log=None):
    """Re-link every consumer's ``shared_lib_imports`` after all owners published.

    Consumer links (writing ``<lib>.pth`` into a plugin's own venv) happen inline
    during that plugin's manifest processing. If a consumer is processed BEFORE
    the owner publishes the lib (plugins run in sort order, so this is purely an
    ordering accident), the inline link soft-skips with "not yet published; will
    retry next session" -- a gratuitous extra session/restart.

    By the time the full plugin loop (Step 4 + the 4b re-scan) has finished, every
    owner has published, so one idempotent re-link sweep converges the pass: a
    consumer-before-owner link that skipped inline now succeeds in the SAME
    session. ``link_shared_lib`` returns "cached" when the .pth is already correct,
    so consumers that linked fine inline are cheap no-ops here (no duplicate
    action entries -- "cached"/"skipped" go to ok_entries, which are verbose-only).

    Successful links are recorded on ``link_log`` (a _SharedLibLinkLog, shared
    with the per-plugin manifest phase so a lib+plugin pair reported by both
    sites appears once) and their per-plugin entry goes to ``quiets`` -- logged
    with its .pth path, but displayed only via the aggregated summary line.

    Returns ``(actions, quiets, oks, failures)`` for the caller to log + display.
    """
    from .shared_lib import link_shared_lib
    from .venv_check import _find_python

    actions, quiets, oks, failures = [], [], [], []
    seen = set()
    for plugin_info in plugins:
        manifest_path = os.path.join(plugin_info.install_path, "bootstrap.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r") as f:
                imports = json.load(f).get("shared_lib_imports", [])
        except (OSError, ValueError):
            continue
        if not imports:
            continue
        # Key by the plugin's own marketplace (see _plugin_data_dir): the consumer
        # must link against the _shared_libs its owner synced into the SAME
        # marketplace tree, so shared_root is per-plugin, not per-engine.
        plugin_data_dir = _plugin_data_dir(data_dir, plugin_info)
        shared_root = os.path.join(os.path.dirname(plugin_data_dir), "_shared_libs")
        venv_python = _find_python(os.path.join(plugin_data_dir, ".venv"))
        for lib_name in imports:
            # Include marketplace in the dedup key: same-named plugins from two
            # marketplaces link into their own trees and must not skip each other.
            key = (getattr(plugin_info, "marketplace", ""), plugin_info.name, lib_name)
            if key in seen:
                continue
            seen.add(key)
            result = link_shared_lib(lib_name, venv_python, shared_root)
            entry = f"{plugin_info.name}: shared-lib {result.name}: {result.message}"
            if result.status == "linked":
                quiets.append(entry)
                if link_log is not None:
                    link_log.record(result.status, result.name, plugin_info.name)
            elif result.status == "failed":
                actions.append(f"{plugin_info.name}: shared-lib {result.name}: FAILED - {result.message}")
                failures.append({
                    "type": "shared_lib",
                    "name": result.name,
                    "message": result.message,
                    "plugin": plugin_info.name,
                })
            else:  # cached / skipped -> verbose-only
                oks.append(entry)
    return actions, quiets, oks, failures


def _plugin_ships_sessionstart_hook(install_path):
    """True if the plugin registers a ``SessionStart`` hook (in ``hooks/hooks.json``
    or via a ``hooks`` key in ``.claude-plugin/plugin.json``).

    SessionStart is the one hook kind ``/reload-plugins`` cannot make live in the
    running session: it reloads the registration but does not re-FIRE SessionStart
    (that only runs on a fresh session). Other hook kinds (UserPromptSubmit,
    PreToolUse, ...) go live after ``/reload-plugins`` on their next event, and a
    hook's script content is read fresh from disk every run -- so only a
    SessionStart hook forces a restart. See references/plugin-reload-lifecycle.md.
    """
    candidates = (
        os.path.join(install_path, "hooks", "hooks.json"),
        os.path.join(install_path, ".claude-plugin", "plugin.json"),
    )
    for path in candidates:
        try:
            with open(path) as f:
                hooks = json.load(f).get("hooks") or {}
        except (OSError, ValueError):
            continue
        if isinstance(hooks, dict) and hooks.get("SessionStart"):
            return True
    return False


def _read_installed_plugins(registry_path):
    """``{ref: installPath}`` for every installed plugin in the registry file
    (keys like ``cache-kit@plugins-kit``). Empty dict on any read/parse error (a
    missing registry just yields no nag rather than crashing the pass).

    Reads ``installPath`` straight from the registry -- NOT from the processed
    plugin lists -- so a plugin with no ``bootstrap.json`` (e.g. cache-kit, which
    ``list_enabled_plugins`` never returns) is still resolvable for the reload nag.
    Registry entries may be a dict or a list of per-scope dicts; take the first
    ``installPath`` found.
    """
    out = {}
    try:
        with open(registry_path) as f:
            plugins = json.load(f).get("plugins", {})
    except (OSError, ValueError):
        return out
    for ref, entry in plugins.items():
        ip = None
        if isinstance(entry, dict):
            ip = entry.get("installPath")
        elif isinstance(entry, list):
            for e in entry:
                if isinstance(e, dict) and e.get("installPath"):
                    ip = e["installPath"]
                    break
        out[ref] = ip
    return out


def _resolve_newly_installed(before_refs, after_map):
    """Plugins that ENTERED the registry this pass (keys in ``after_map`` not in
    ``before_refs``) -> a name-sorted list of lightweight ``PluginInfo`` built from
    the registry (name / marketplace / installPath), for the Step 4d reload nag.

    install_path comes from the registry so even no-``bootstrap.json`` plugins
    resolve; it is what ``_plugin_ships_sessionstart_hook`` inspects.
    """
    from .plugin_resolve import PluginInfo
    result = []
    for ref in sorted(set(after_map) - set(before_refs)):
        ip = after_map.get(ref)
        if not ip:
            continue
        name, _, marketplace = ref.partition("@")
        result.append(PluginInfo(name=name, install_path=ip, version="", marketplace=marketplace))
    return result


def _reload_advice(newly_installed):
    """User-facing reload/restart notice (informational, not action-required) for
    plugins that ENTERED the registry during this pass, or None when there is
    nothing to advise.

    Claude Code loads plugins at session start -- before this SessionStart hook ran
    and installed these -- so they are not active yet. This is the one case
    bootstrap can PROVE the running session is missing plugin code. A plugin merely
    *updated* at session start was already loaded by the restart that updated it
    (and Parts 1+2 provision its deps in that same pass), so it is deliberately NOT
    nagged here -- that would be noise on every publish.

    Restart vs reload is the MEASURED rule (references/plugin-reload-lifecycle.md),
    not the "hooks always need a restart" folklore: ``/reload-plugins`` reloads
    registrations and skills/commands in-session, so it suffices unless the new
    plugin registers a ``SessionStart`` hook -- only a fresh session re-fires that.
    """
    if not newly_installed:
        return None
    names = ", ".join(sorted(pi.name for pi in newly_installed))
    needs_restart = any(_plugin_ships_sessionstart_hook(pi.install_path) for pi in newly_installed)
    if needs_restart:
        return (
            f"bootstrap installed new plugin(s): {names}. "
            f"They will load next time you restart Claude (or your IDE)."
        )
    return (
        f"bootstrap installed new plugin(s): {names}. "
        f"Run /reload-plugins to start using them."
    )


def _bootstrap_stale_advice(running_version, plugin_name, marketplace_name, registry_path,
                            data_dir=""):
    """Restart notice (informational, not action-required) when the registry
    records a NEWER bootstrap than the one running this session, else None.

    Suppressed once provisioning has CONVERGED: when the global
    engine_ran_version stamp is already >= the registry version, the new
    engine has completed a pass (via the harvest or an earlier restart) and a
    restart would only reload plugin CODE. Without this, every subsequent
    old-binary session in the same window re-fired the same nag against an
    update that had already landed.

    autoUpdate caches the new bootstrap and rewrites ``installed_plugins.json`` at
    session start, but the session already loaded the OLD hook -- and
    ``/reload-plugins`` won't re-fire bootstrap's ``SessionStart`` pass, so only a
    fresh session actually runs the new bootstrap. Claude Code's generic update
    notice says "/reload-plugins", which is insufficient here; this nag tells the
    user the truth. The comparison direction (registry > running) self-guards the
    common dev case (a dev tree running AHEAD of the cache never nags). See
    references/plugin-reload-lifecycle.md.
    """
    if not running_version or not plugin_name or not marketplace_name:
        return None
    cli_ref = f"{plugin_name}@{marketplace_name}"
    try:
        with open(registry_path) as f:
            installs = json.load(f).get("plugins", {}).get(cli_ref, [])
    except (OSError, ValueError):
        return None
    from .plugin_resolve import pick_registry_record
    rec = pick_registry_record(installs)
    registry_version = rec.get("version", "") if rec is not None else ""
    if not registry_version:
        return None
    from .marketplace_lifecycle import _version_greater
    if not _version_greater(registry_version, running_version):
        return None
    if data_dir:
        from .stamps import global_stamp
        ran_version = global_stamp(data_dir, "engine_ran_version").read()
        if ran_version and _parse_semver(ran_version) >= _parse_semver(registry_version):
            return None
    return (
        f"bootstrap was updated to {registry_version}; "
        f"it will load next time you restart Claude (or your IDE)."
    )


def _load_enabled_refs(project_dir=None):
    """Build the set of enabled plugin refs from Claude Code settings + production registry.

    Reads settings files in precedence order (later overrides earlier):
      1. ~/.claude/settings.json         (user scope)
      2. ~/.claude/settings.local.json   (user local overrides)
      3. <project_dir>/.claude/settings.json        (project scope)
      4. <project_dir>/.claude/settings.local.json  (project local overrides)

    A plugin is enabled if its enabledPlugins entry has a final value of True.
    Also includes all plugins found in the production installed_plugins.json registry.

    Scope is handled naturally: user-scoped plugins appear in user settings (always
    included); project-scoped plugins appear in project settings (included only when
    that project is the active --project-dir).

    Returns:
        Set of normalized refs (plugin@marketplace), or None if no sources exist
        (falls back to no filter to preserve the original behavior).
    """
    from .plugin_resolve import parse_plugin_ref

    def _normalize(ref):
        marketplace, name = parse_plugin_ref(ref)
        return f"{name}@{marketplace}" if marketplace else name

    # Collect settings files in ascending precedence
    home = os.environ.get("HOME") or os.path.expanduser("~")
    claude_home = os.path.join(home, ".claude")
    settings_paths = [
        os.path.join(claude_home, "settings.json"),
        os.path.join(claude_home, "settings.local.json"),
    ]
    if project_dir:
        project_claude = os.path.join(project_dir, ".claude")
        settings_paths.append(os.path.join(project_claude, "settings.json"))
        settings_paths.append(os.path.join(project_claude, "settings.local.json"))

    merged_enabled = {}
    any_settings_found = False
    for path in settings_paths:
        try:
            with open(path, "r") as f:
                data = json.load(f)
            ep = data.get("enabledPlugins", {})
            if isinstance(ep, dict):
                merged_enabled.update(ep)
                any_settings_found = True
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    refs = {_normalize(ref) for ref, val in merged_enabled.items() if val}

    # Also include all plugins in the production registry as a secondary source.
    # Use the same home resolution as above so test isolation via HOME env var works.
    prod_registry_path = os.path.join(home, ".claude", "plugins", "installed_plugins.json")
    try:
        with open(prod_registry_path, "r") as f:
            registry = json.load(f)
        for ref in registry.get("plugins", {}):
            refs.add(_normalize(ref))
        any_settings_found = True
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    # If no sources found at all, return None to preserve original (no-filter) behavior
    return refs if any_settings_found else None


def _load_layered_manifests(project_dir, data_dir=None):
    """Load and merge bootstrap manifests from user and project layers.

    Priority (highest wins):
        4. <project>/.claude/bootstrap.local.json
        3. <project>/.claude/bootstrap.json
        2. ~/.claude/bootstrap.local.json
        1. ~/.claude/bootstrap.json
        0. <data_dir>/user-bootstrap.json  (legacy, lowest priority)

    Returns (merged_manifest, parse_errors) where parse_errors is a list of
    {"path": <path>, "error": <message>} dicts for any layer that failed to load.
    Layers that fail to parse are skipped (the merge continues with the rest).
    """
    from .manifest_merge import merge_manifests

    # Collect candidate paths in priority order (lowest first)
    candidates = []

    # Legacy user-bootstrap.json (lowest priority — deprecated)
    if data_dir:
        legacy = os.path.join(data_dir, "user-bootstrap.json")
        candidates.append(legacy)

    # User-level (HOME is preferred, USERPROFILE is the Windows fallback)
    home = os.environ.get("HOME") or os.path.expanduser("~")
    claude_home = os.path.join(home, ".claude")
    candidates.append(os.path.join(claude_home, "bootstrap.json"))
    candidates.append(os.path.join(claude_home, "bootstrap.local.json"))

    # Project-level
    if project_dir:
        project_claude = os.path.join(project_dir, ".claude")
        candidates.append(os.path.join(project_claude, "bootstrap.json"))
        candidates.append(os.path.join(project_claude, "bootstrap.local.json"))

    merged = {}
    parse_errors = []
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r") as f:
                layer = json.load(f)
        except json.JSONDecodeError as e:
            parse_errors.append({"path": path, "error": f"JSON parse error: {e}"})
            continue
        except OSError as e:
            parse_errors.append({"path": path, "error": f"read error: {e}"})
            continue
        merged = merge_manifests(merged, layer)

    return merged, parse_errors


def _activate_bootstrap_venv(data_dir):
    """Add bootstrap venv site-packages to sys.path so PyYAML is importable."""
    import glob as globmod
    venv_path = os.path.join(data_dir, ".venv")
    # Look for site-packages in both Unix and Windows layouts
    patterns = [
        os.path.join(venv_path, "lib", "python*", "site-packages"),
        os.path.join(venv_path, "Lib", "site-packages"),
    ]
    for pattern in patterns:
        matches = globmod.glob(pattern)
        for sp in matches:
            if sp not in sys.path:
                sys.path.insert(0, sp)


def _join_items(items):
    """Format items as 'name [detail], name [detail]' or 'name, name'.

    items: list of (name, detail) tuples. Empty detail -> bare name.
    """
    parts = []
    for name, detail in items:
        if detail:
            parts.append(f"{name} [{detail}]")
        else:
            parts.append(name)
    return ", ".join(parts)


def _link_tool_dir_to_path(result, prefix, action_entries):
    """When a tool resolved on disk but its dir isn't on PATH, own the chain.

    Per dependency-philosophy.md principle 4 (find-or-download, never tell the
    user to "restart your IDE to pick up PATH"), the engine persists the tool's
    directory to PATH itself: shell RC files + Windows User PATH (registry), and
    the live process PATH so subsequent phases this run can find it. Idempotent.

    A tool that is present-on-disk but absent-from-PATH is, for any consumer that
    invokes it by bare name, effectively not installed. This is the missing
    linkage between `tools[]` and `path_entries[]`: a resolved tool pulls its own
    directory onto PATH instead of relying on a separate, hand-authored
    path_entries entry that may or may not exist.

    No-op when the tool resolved with no concrete path (e.g. via a `check`
    command) or is already on PATH.
    """
    if result.on_path or not result.path:
        return
    tool_dir = os.path.dirname(result.path)
    if not tool_dir:
        return
    from .path_check import add_path_to_shell_config, normalize_path_for_compare
    _ok, msg = add_path_to_shell_config(tool_dir)
    current_path = os.environ.get("PATH", "")
    norm = [normalize_path_for_compare(d) for d in current_path.split(os.pathsep)]
    if normalize_path_for_compare(tool_dir) not in norm:
        os.environ["PATH"] = tool_dir + os.pathsep + current_path
    action_entries.append(
        f"{prefix}{result.subject}: on disk but not on PATH — added {tool_dir} ({msg})"
    )


class _StrategyOutcome:
    """Result contract shared by every install-strategy function.

    terminal=True  -> the dispatcher stops and returns ``failure`` (None on
                      success, or a failure dict).
    terminal=False -> the dispatcher falls through to the next strategy.
    """

    __slots__ = ("terminal", "failure")

    def __init__(self, terminal, failure=None):
        self.terminal = terminal
        self.failure = failure


class _ToolEntryCtx:
    """Shared state threaded through the install-strategy dispatch table.

    Bundles the tool entry's parsed fields with the mutable accumulators
    (action_entries / ok_entries / tools_installed) so each strategy takes a
    single argument. ``result`` is the initial check_tool() outcome, populated
    by the resolve strategy and reused by the install-command strategy (which
    reports on ``result.subject`` / ``result.install_cmd`` / ``result.message``).
    """

    __slots__ = ("tool_def", "name", "install", "install_cmds", "skip", "scoop_pkg",
                 "brew_spec", "apt_pkg", "elevated", "tool_install_path", "check_cmd",
                 "download_def", "requires", "machine_resolver", "current_os",
                 "prefix", "action_entries", "ok_entries", "tools_installed",
                 "plugin_name", "result")

    def __init__(self, tool_def, current_os, prefix, action_entries,
                 ok_entries, tools_installed, plugin_name,
                 machine_resolver=None):
        self.tool_def = tool_def
        self.name = tool_def["name"]
        # tool_def is already canonicalized by _normalize_tool_entry: every
        # install value is an object ({"command"..}, {"scoop"..}, ...) and scoop
        # lives under install.<os> (never download). Derive the two shapes the
        # strategies consume:
        #   install_cmds -- os -> bare command string (what check_tool/run_install
        #                   want; "manual" sentinel flows through unchanged).
        #   scoop_pkg    -- the scoop package for this host, or None.
        self.install = tool_def.get("install", {})
        self.install_cmds = {k: v["command"] for k, v in self.install.items()
                             if isinstance(v, dict) and "command" in v}
        os_spec = self.install.get(current_os)
        #   skip -- "not applicable on this OS" (canonical {"skip": true}, from
        #   the "skip" string sentinel). Consumed by the skip strategy, which
        #   short-circuits the entry before resolve.
        self.skip = bool(os_spec.get("skip")) if isinstance(os_spec, dict) else False
        self.scoop_pkg = os_spec.get("scoop") if isinstance(os_spec, dict) else None
        #   brew_spec -- the canonical brew fulfillment for this host, or None.
        #   {"brew": "name"} shorthand -> {"formula": "name"}; the object forms
        #   {"brew": {"cask": ...}} / {"brew": {"formula": ..., "tap": ...}} pass
        #   through. Only the current-OS install value is consulted (brew is
        #   macOS-only), mirroring scoop_pkg.
        self.brew_spec = self._parse_brew(os_spec)
        #   apt_pkg -- the apt package name for this host, or None. Canonical
        #   apt form is a bare string ({"apt": "net-tools"}); apt is Ubuntu-only,
        #   so only the current-OS install value is consulted (mirrors scoop_pkg).
        self.apt_pkg = os_spec.get("apt") if isinstance(os_spec, dict) else None
        #   elevated -- does THIS host's install need privileges? Read with a
        #   False default: an author-written object may omit the field (audit
        #   note N2), and a bare-string install normalizes to
        #   {"command": s, "elevated": False}. Consumed by the install-command
        #   strategy and, on Windows, by the scoop strategy (a manifest with
        #   admin-gated pre_install, e.g. extras/tailscale, declares
        #   {"scoop": ..., "elevated": true}); brew/apt carry their own
        #   elevation model.
        self.elevated = bool(os_spec.get("elevated", False)) if isinstance(os_spec, dict) else False
        self.tool_install_path = tool_def.get("installPath")
        self.check_cmd = tool_def.get("check")
        self.download_def = _resolve_download_def(tool_def.get("download", {}), current_os)
        #   requires -- optional machine-property targeting, e.g.
        #   {"requires": {"dev": true}}: declare a tool once fleet-wide and
        #   have it fall out on machines whose env.json `machines` entry does
        #   not satisfy the mapping (a machine PROPERTY, never a hostname
        #   list). Consumed by the requires strategy, which short-circuits
        #   the entry before resolve -- exactly like the skip sentinel.
        #   machine_resolver -- the phase-shared lazy identity lookup
        #   (env_manifest.MachineRequiresResolver); None when the caller did
        #   not wire one (self-setup), in which case the requires strategy
        #   builds its own over the user layers.
        self.requires = tool_def.get("requires")
        self.machine_resolver = machine_resolver
        self.current_os = current_os
        self.prefix = prefix
        self.action_entries = action_entries
        self.ok_entries = ok_entries
        self.tools_installed = tools_installed
        self.plugin_name = plugin_name
        self.result = None

    @staticmethod
    def _parse_brew(os_spec):
        """Canonicalize the brew fulfillment on this host's install value.

        Accepts the shorthand string ({"brew": "direnv"} -> {"formula":
        "direnv"}) and the object forms ({"brew": {"cask": ...}} /
        {"brew": {"formula": ..., "tap": ...}}). Returns a dict with
        formula|cask (+ optional tap), or None when no brew fulfillment is
        declared for this host.
        """
        if not isinstance(os_spec, dict):
            return None
        brew = os_spec.get("brew")
        if brew is None:
            return None
        if isinstance(brew, str):
            return {"formula": brew}
        if isinstance(brew, dict):
            return dict(brew)
        return None


def _tool_check(ctx):
    """check_tool() with the entry's parsed args — the initial resolve and
    every post-install re-check funnel through here (identical arguments)."""
    from .tool_check import check_tool
    return check_tool(ctx.name, ctx.install_cmds, ctx.current_os,
                      install_path=ctx.tool_install_path, check_cmd=ctx.check_cmd)


def _privileges_available(current_os):
    """Module-level indirection over fix_queue.privileges_available so the
    install-command strategy's defer-vs-run decision is monkeypatchable in tests
    without touching the probes themselves."""
    from .fix_queue import privileges_available
    return privileges_available(current_os)


def _strategy_skip(ctx):
    """Precedence 0: the "skip" sentinel -- this tool is not applicable on this
    OS (design-os-not-applicable.md ruling). The entry short-circuits BEFORE
    resolve: no check subprocess, no install, no PATH work, no failure dict --
    just one verbose-only ok line. Per-OS: only the current OS's install value
    is consulted, so other OSes' fulfillments are untouched. Omitting an OS key
    is NOT skip -- omission keeps its "must already resolve, else a
    no_install_cmd FAILED item" semantics."""
    if not ctx.skip:
        return _StrategyOutcome(False)
    ctx.ok_entries.append(
        f"{ctx.prefix}{ctx.name}: skipped - not applicable on {ctx.current_os}"
    )
    return _StrategyOutcome(True, None)


def _strategy_requires(ctx):
    """Precedence 0b: machine-property targeting via `requires`. A tools[]
    entry may declare {"requires": {"dev": true, ...}}: it applies iff EVERY
    (attribute, expected) pair is satisfied by the current machine's entry in
    the env.json `machines` registry (env_manifest.requires_satisfied) -- so
    a fleet-wide manifest can exclude a tool from a machine by PROPERTY,
    never by hostname list. An unsatisfied entry short-circuits exactly like
    the skip sentinel: before resolve, no check subprocess, no install, no
    failure dict -- one verbose-only ok line. No `requires` key -> falls
    through untouched (the overwhelmingly common case).

    Ordering is load-bearing twice over:
    - AFTER _strategy_skip: skip is decided from the entry alone (no I/O),
      so an entry the current OS already opted out of never triggers an
      env.json read.
    - Identity resolves LAZILY and independently of the env pass: the tools
      phases (Steps 3/3c) run before _process_env_pass (Step 3e), and the
      env_state.json stamp gates that PASS, not identity -- so this strategy
      does its own lookup via the memoized MachineRequiresResolver. A
      manifest with no `requires` anywhere never reads env.json at all,
      keeping fresh/standalone machines (and projects without env.json)
      byte-for-byte unchanged.

    A `requires` on a machine that cannot be identified is a hard failure
    (Environment Awareness doctrine: unknown machines are a hard error, no
    fallbacks, no guessing) -- installing a targeted tool on an unvetted
    machine is exactly what the field exists to prevent."""
    requires = ctx.requires
    if not requires:
        return _StrategyOutcome(False)
    from .env_manifest import MachineRequiresResolver, requires_satisfied
    if not isinstance(requires, dict):
        # Shape validation before any identity work: a scalar/list here is a
        # manifest bug, and guessing at it would silently mis-target installs.
        msg = (f"'requires' must be an object mapping machine attribute -> "
               f"expected value, got {type(requires).__name__} {requires!r}")
        ctx.action_entries.append(f"{ctx.prefix}{ctx.name}: INVALID 'requires' - {msg}")
        return _StrategyOutcome(True, {
            "type": "tool", "name": ctx.name, "message": msg,
            "install_state": "requires_invalid", "install_cmd": None,
            "agent_msg": (
                f"Tool '{ctx.name}' declares an invalid 'requires' field: "
                f"{msg}. Fix the bootstrap.json entry (e.g. "
                f"\"requires\": {{\"dev\": true}})."
            ),
            "plugin": ctx.plugin_name,
            "persist_across_sessions": True,
        })
    resolver = ctx.machine_resolver or MachineRequiresResolver(None)
    machine_key, machine, err = resolver.resolve()
    if err:
        req = json.dumps(requires, sort_keys=True)
        msg = f"cannot evaluate requires {req}: {err}"
        ctx.action_entries.append(f"{ctx.prefix}{ctx.name}: FAILED - {msg}")
        return _StrategyOutcome(True, {
            "type": "tool", "name": ctx.name, "message": msg,
            "install_state": "requires_unresolved", "install_cmd": None,
            "agent_msg": (
                f"Tool '{ctx.name}' declares machine requirements {req}, but "
                f"the current machine could not be resolved: {err}. Add this "
                f"machine (with the attributes that describe it) to the "
                f"'machines' registry in ~/.claude/env.json, then re-run "
                f"bootstrap. No fallbacks: a targeted tool never installs on "
                f"an unidentified machine."
            ),
            "plugin": ctx.plugin_name,
            "persist_across_sessions": True,
        })
    if requires_satisfied(requires, machine):
        return _StrategyOutcome(False)
    req = json.dumps(requires, sort_keys=True)
    ctx.ok_entries.append(
        f"{ctx.prefix}{ctx.name}: skipped - machine '{machine_key}' does not "
        f"satisfy requires {req}"
    )
    return _StrategyOutcome(True, None)


def _strategy_resolve(ctx):
    """Precedence 1: already resolvable via installPath candidates / `check`
    cmd / which. On success record the path, link its dir onto PATH (owning
    the chain; no user "restart" instruction — philosophy P4), and finish."""
    from . import tool_paths
    result = _tool_check(ctx)
    ctx.result = result
    if result.passed:
        if result.path:
            # data_dir=None -> the canonical bootstrap data dir; tool paths are
            # recorded centrally regardless of which plugin's pass found them.
            tool_paths.record(None, result.subject, result.path)
        _link_tool_dir_to_path(result, ctx.prefix, ctx.action_entries)
        ctx.ok_entries.append(f"{ctx.prefix}{result.subject}: ok - {result.message}")
        return _StrategyOutcome(True, None)
    return _StrategyOutcome(False)


def _strategy_scoop(ctx):
    """Precedence 2: Scoop fulfillment (Windows userspace package manager). A
    `scoop` value under install.<os> means "install via Scoop" rather than a
    url/sha download. Normalization (_normalize_tool_entry) moves the legacy
    `download.<os-arch>.scoop` spelling into this canonical install location, so
    the strategy reads ctx.scoop_pkg regardless of which spelling the manifest
    used. Scoop is provisioned LAZILY -- the first such tool installs it. See
    bootstrap_lib/scoop.py. Terminal whenever it applies. (An older engine that
    predates this branch falls through to the install command, degrading to the
    legacy path.)

    Elevation-aware: a scoop fulfillment declaring ``elevated: true`` (an
    admin-gated scoop manifest, e.g. extras/tailscale) is NEVER attempted
    without privileges -- it defers into the elevation queue exactly like an
    elevated opaque command, after ensure_scoop (scoop itself always installs
    unelevated)."""
    from . import tool_paths
    pkg = ctx.scoop_pkg
    if not pkg:
        return _StrategyOutcome(False)
    from .scoop import ensure_scoop, scoop_install
    from .path_repair import repair_path
    es = ensure_scoop()
    if not es.ok:
        ctx.action_entries.append(f"{ctx.prefix}{ctx.name}: scoop unavailable - {es.message}")
        return _StrategyOutcome(True, {"type": "tool", "name": ctx.name, "message": es.message,
                "install_state": "scoop_failed", "install_cmd": None,
                "plugin": ctx.plugin_name})
    if es.message != "already installed":
        ctx.action_entries.append(f"{ctx.prefix}scoop: {es.message}")
    if ctx.elevated and not _privileges_available(ctx.current_os):
        # {"scoop": ..., "elevated": true} + missing privileges: DEFER, never
        # attempt (a background hook must not trigger UAC, and an admin-gated
        # scoop manifest fails unelevated anyway -- pre_install `is_admin`
        # gates error out while leaving a broken ~/scoop/apps install behind).
        # Queued for the remediation .bat via the standard {method: "command"}
        # descriptor; the command wraps scoop in powershell because the .bat
        # runs the queue through bash under elevated cmd (see
        # scoop.elevated_install_command). Mirrors _strategy_install_command's
        # deferral. ensure_scoop ran ABOVE on purpose: scoop itself installs
        # unelevated (and must not be installed as admin), so the deferred
        # elevated install finds a working scoop.
        from .scoop import elevated_install_command
        manual_cmd = elevated_install_command(pkg)
        ctx.action_entries.append(
            f"{ctx.prefix}{ctx.name}: needs elevation - scoop package {pkg} "
            f"requires admin rights; deferred to the remediation script"
        )
        return _StrategyOutcome(True, {
            "type": "tool", "name": ctx.name,
            "message": f"scoop package {pkg} requires elevation to install",
            "install_state": "needs_elevation",
            # No runnable-by-us command: only the user can elevate. install_cmd
            # None keeps the item off the fix-all path (manual-attention only).
            "install_cmd": None,
            # cost: a package install fetches over the network, so it sorts
            # behind the local config fixes. Declared, not derived: these
            # descriptors carry no timeout for fix_queue.cost_of to read.
            "elevation": {"method": "command", "command": manual_cmd,
                          "os": ctx.current_os, "id": f"tool:{ctx.name}",
                          "label": f"Install {ctx.name}", "cost": "slow"},
            "agent_msg": (
                f"Installing {ctx.name} (scoop package {pkg}) needs "
                f"administrator rights, which a background hook must not "
                f"request; bootstrap deferred it into the fix queue."
            ),
            "plugin": ctx.plugin_name,
            "persist_across_sessions": True,
        })
    si = scoop_install(pkg, tool_name=ctx.name)
    # Scoop adds ~/scoop/shims to the user PATH on install; reflect that into
    # this already-running process so the re-check can resolve the binary.
    repair_path()
    recheck = _tool_check(ctx)
    if recheck.passed:
        if recheck.path:
            tool_paths.record(None, recheck.subject, recheck.path)
        ctx.tools_installed.append((ctx.name, f"installed `{pkg}` via scoop"))
        return _StrategyOutcome(True, None)
    if si.ok and si.path:
        # Resolvable on disk but not yet by bare name; record the shim path.
        tool_paths.record(None, ctx.name, si.path)
        ctx.tools_installed.append((ctx.name, f"installed `{pkg}` via scoop ({si.path})"))
        return _StrategyOutcome(True, None)
    ctx.action_entries.append(f"{ctx.prefix}{ctx.name}: scoop install failed - {si.message}")
    return _StrategyOutcome(True, {"type": "tool", "name": ctx.name, "message": si.message,
            "install_state": "scoop_failed", "install_cmd": None,
            "plugin": ctx.plugin_name})


def _strategy_brew(ctx):
    """Precedence 2b: brew fulfillment (macOS package manager). A `brew` value
    under install.<os> means "install via Homebrew" rather than a url/sha
    download or an opaque command. Terminal whenever it applies.

    Homebrew is NEVER auto-installed (ensure_brew is detect-only): a missing
    brew is a descriptive per-item failure, mirroring how scoop surfaces an
    unavailable manager. Only applies on macOS, where the canonical brew object
    is present at install.macos (ctx.brew_spec); on other hosts brew_spec is
    None and this falls through. Mirrors _strategy_scoop's shape."""
    from . import tool_paths
    spec = ctx.brew_spec
    if not spec:
        return _StrategyOutcome(False)
    label = spec.get("cask") or spec.get("formula") or ctx.name
    from .brew import ensure_brew, brew_install
    from .path_repair import repair_path
    eb = ensure_brew()
    if not eb.ok:
        ctx.action_entries.append(f"{ctx.prefix}{ctx.name}: brew unavailable - {eb.message}")
        # brew is missing while a brew-backed entry is pending: signal the
        # elevation queue to lead the macOS remediation script with the Homebrew
        # installer (strategy section 6). The official installer is interactive
        # and may sudo, so the engine never runs it -- one user-run step, then
        # brew entries install unattended next session. (This branch is
        # macOS-only: brew_spec is None off macOS.) The brew_failed item itself
        # re-checks and clears next session once brew exists.
        return _StrategyOutcome(True, {"type": "tool", "name": ctx.name, "message": eb.message,
                "install_state": "brew_failed", "install_cmd": None,
                "elevation": {"method": "brew_installer", "os": "macos"},
                "plugin": ctx.plugin_name})
    bi = brew_install(formula=spec.get("formula"), cask=spec.get("cask"), tap=spec.get("tap"))
    # brew links formulae into its prefix bin (already on PATH); reflect any
    # PATH change into this running process so the re-check can resolve it.
    repair_path()
    recheck = _tool_check(ctx)
    if recheck.passed:
        if recheck.path:
            tool_paths.record(None, recheck.subject, recheck.path)
        ctx.tools_installed.append((ctx.name, f"installed `{label}` via brew"))
        return _StrategyOutcome(True, None)
    if bi.ok and spec.get("cask"):
        # CASK ONLY: brew reported success but the tool doesn't resolve by our
        # check -- a GUI cask may have no CLI binary and no `check` command, so
        # there is nothing on PATH for the re-check to see. Trust brew's success
        # for casks alone. A FORMULA whose re-check fails (keg-only, broken
        # PATH) falls through to the brew_failed failure dict below -- the
        # re-check stays authoritative for anything that should resolve
        # (strategy section 8; mirrors scoop, which only trusts an actual shim).
        ctx.tools_installed.append((ctx.name, f"installed `{label}` via brew"))
        return _StrategyOutcome(True, None)
    ctx.action_entries.append(f"{ctx.prefix}{ctx.name}: brew install failed - {bi.message}")
    return _StrategyOutcome(True, {"type": "tool", "name": ctx.name, "message": bi.message,
            "install_state": "brew_failed", "install_cmd": None,
            "plugin": ctx.plugin_name})


def _strategy_apt(ctx):
    """Precedence 2c: apt fulfillment (Ubuntu system package manager). An `apt`
    value under install.<os> means "install via apt-get" rather than a url/sha
    download or an opaque command. Terminal whenever it applies. Mirrors
    _strategy_scoop / _strategy_brew, and like them runs BEFORE url download
    (manager-over-download); apt/scoop/brew are mutually exclusive by OS (each
    reads only its own host's install value), so on Ubuntu ctx.apt_pkg is set and
    scoop_pkg/brew_spec are None.

    Elevation-aware (elevation policy, section 5): apt always needs root. When
    passwordless sudo is unavailable and we are not root, apt_install NEVER
    attempts the operation and reports needs_elevation; this strategy converts
    that into a persistent `needs_elevation` manual-attention failure carrying an
    `elevation` descriptor so a later step can accumulate every deferred op into
    ONE remediation script. Accumulation is NOT done here.

    Unlike brew's cask leniency, there is NO trust-despite-failed-recheck for
    apt: apt packages install real binaries/services, so the post-install
    re-check stays authoritative -- a package apt claims to have installed but
    that still does not resolve is an apt_failed failure."""
    from . import tool_paths
    pkg = ctx.apt_pkg
    if not pkg:
        return _StrategyOutcome(False)
    from .apt import apt_install
    from .path_repair import repair_path
    ai = apt_install(pkg)
    if ai.needs_elevation:
        manual_cmd = f"sudo apt-get install -y {pkg}"
        ctx.action_entries.append(
            f"{ctx.prefix}{ctx.name}: needs elevation - passwordless sudo "
            f"unavailable; run: {manual_cmd}"
        )
        return _StrategyOutcome(True, {
            "type": "tool", "name": ctx.name,
            "message": ai.message,
            "install_state": "needs_elevation",
            # No runnable-by-us command: only the user can elevate. install_cmd
            # None keeps the item off the fix-all path (manual-attention only).
            "install_cmd": None,
            # Structured elevation descriptor for the (later) elevation queue:
            # method identifies the backend, package is what `apt-get install`
            # needs, os disambiguates when the queue emits per-OS scripts.
            "elevation": {"method": "apt", "package": pkg, "os": ctx.current_os},
            "agent_msg": (
                f"Installing {ctx.name} needs root, but passwordless sudo is not "
                f"available on this machine (sudo -n failed) and bootstrap is not "
                f"running as root. Run `{manual_cmd}`, then type 'fix-all' (or "
                f"re-run bootstrap) to confirm it resolved."
            ),
            "plugin": ctx.plugin_name,
            "persist_across_sessions": True,
        })
    # apt installs into system dirs already on PATH; repair_path for parity with
    # the other managers so any late PATH change is visible to the re-check.
    repair_path()
    recheck = _tool_check(ctx)
    if recheck.passed:
        if recheck.path:
            tool_paths.record(None, recheck.subject, recheck.path)
        _link_tool_dir_to_path(recheck, ctx.prefix, ctx.action_entries)
        ctx.tools_installed.append((ctx.name, f"installed `{pkg}` via apt"))
        return _StrategyOutcome(True, None)
    # Re-check failed: either the install errored, or the backend reported the
    # package present (apt install, or the dpkg already-installed guard) but the
    # tool still does not resolve (wrong check/binary name -- a manifest bug).
    # ai.message names the actual reporter, so the wording stays accurate for both.
    msg = ai.message if not ai.ok else (
        f"{ai.message}, but it still does not resolve "
        f"(check the entry's `check`/binary name)"
    )
    ctx.action_entries.append(f"{ctx.prefix}{ctx.name}: apt install failed - {msg}")
    return _StrategyOutcome(True, {"type": "tool", "name": ctx.name, "message": msg,
            "install_state": "apt_failed", "install_cmd": None,
            "plugin": ctx.plugin_name})


def _strategy_url_download(ctx):
    """Precedence 3: prefer downloading our own copy to ~/.local/bin over
    shelling out to a system package manager. See tool-resolution-redesign.md.
    On failure this logs and FALLS THROUGH to the install command (legacy
    fall-through preserved)."""
    from . import tool_paths
    download_def = ctx.download_def
    if not (download_def and download_def.get("url") and download_def.get("sha256")):
        return _StrategyOutcome(False)
    from .downloader import download_and_install
    dl = download_and_install(
        ctx.name,
        download_def["url"],
        download_def["sha256"],
        binary_name=download_def.get("binary_name"),
        archive_path=download_def.get("archive_path"),
        archive_type=download_def.get("archive_type"),
    )
    if dl.ok:
        tool_paths.record(None, ctx.name, dl.path)
        ctx.tools_installed.append((ctx.name, f"downloaded to {dl.path}"))
        return _StrategyOutcome(True, None)
    ctx.action_entries.append(f"{ctx.prefix}{ctx.name}: download failed - {dl.message}")
    # Fall through to legacy install fallback.
    return _StrategyOutcome(False)


def _strategy_install_command(ctx):
    """Precedence 4 (final fallback): run the OS install command and re-check
    regardless of exit code, or handle the "manual" sentinel.

    The "manual" sentinel (dependency-philosophy.md) means there is no
    unattended installer for this OS: bootstrap verifies the tool resolves on
    PATH but never tries to install it. Treat it as a manual-attention item --
    do NOT execute "manual" as a shell command (it just fails with
    "command not found", surfacing a bogus install_failed every session).

    Always terminal: returns None on a successful re-check or the failure dict
    otherwise."""
    from . import tool_paths
    result = ctx.result
    install_state = "no_install_cmd"
    install_output = ""
    if result.install_cmd == "manual":
        install_state = "manual_install"
    elif result.install_cmd and ctx.elevated and not _privileges_available(ctx.current_os):
        # Elevated command + missing privileges: DEFER, never attempt (a
        # background hook must not sudo / trigger UAC). The op is queued for the
        # per-OS remediation script via this descriptor, and surfaced as a
        # persistent needs_elevation item -- mirroring _strategy_apt. When
        # privileges ARE available (or the command is not elevated -- N2/N3) this
        # branch is skipped and the command runs directly, unchanged.
        manual_cmd = result.install_cmd
        ctx.action_entries.append(
            f"{ctx.prefix}{result.subject}: needs elevation - run: {manual_cmd}"
        )
        return _StrategyOutcome(True, {
            "type": "tool", "name": result.subject,
            "message": f"{result.subject} install requires elevation: {manual_cmd}",
            "install_state": "needs_elevation",
            "install_cmd": None,
            # cost: an OS install command downloads -- see the scoop strategy.
            "elevation": {"method": "command", "command": manual_cmd,
                          "os": ctx.current_os, "id": f"tool:{result.subject}",
                          "label": f"Install {result.subject}", "cost": "slow"},
            "agent_msg": (
                f"Installing {result.subject} needs elevated privileges, which a "
                f"background hook must not request; bootstrap deferred it into "
                f"the fix queue."
            ),
            "plugin": ctx.plugin_name,
            "persist_across_sessions": True,
        })
    elif result.install_cmd:
        from .tool_check import run_install
        from .path_repair import repair_path
        ok, install_output = run_install(result.install_cmd)
        # Re-check regardless of the installer's exit code: a non-zero exit can
        # mean "already installed / no upgrade available" (winget 43), which is
        # success from our standpoint. repair_path() first so a registry PATH
        # update from the installer is visible to this already-running process.
        repair_path()
        recheck = _tool_check(ctx)
        if recheck.passed:
            if recheck.path:
                tool_paths.record(None, recheck.subject, recheck.path)
            _link_tool_dir_to_path(recheck, ctx.prefix, ctx.action_entries)
            verb = "via" if ok else "already present after"
            ctx.tools_installed.append((result.subject, f"{verb} `{result.install_cmd}`"))
            return _StrategyOutcome(True, None)
        # Re-check failed: distinguish "installer ran but we still can't find it"
        # from "installer itself errored".
        install_state = "installed_but_path_stale" if ok else "install_failed"

    # The installer's own output explains BOTH of the next two states, and it
    # was captured and thrown away -- while the fix-all directive we emit tells
    # Claude to "re-run and capture output", i.e. to regenerate the diagnosis we
    # already had. Attach it to the record instead; the displayed line is
    # unchanged.
    install_detail = ({"install_cmd": result.install_cmd,
                       "install_output": install_output}
                      if install_output else None)
    if install_state == "installed_but_path_stale":
        _append_detail(
            ctx.action_entries,
            f"{ctx.prefix}{result.subject}: install succeeded but binary not findable afterward "
            f"(add an installPath hint, or a download recipe to fetch our own copy)",
            detail=install_detail,
        )
    elif install_state == "install_failed":
        _append_detail(
            ctx.action_entries,
            f"{ctx.prefix}{result.subject}: install command failed - `{result.install_cmd}`",
            detail=install_detail,
        )
    elif install_state == "manual_install":
        _append_detail(
            ctx.action_entries,
            f"{ctx.prefix}{result.subject}: not installed — manual install required "
            f"(no unattended installer for this OS); install it and ensure it's on PATH",
            display=f"{result.subject}: manual install needed",
        )
    else:
        ctx.action_entries.append(f"{ctx.prefix}{result.subject}: FAILED - {result.message}")

    return _StrategyOutcome(True, {
        "type": "tool",
        "name": result.subject,
        "message": result.message,
        "install_state": install_state,
        # manual_install carries no runnable command — null it so the item is
        # classified manual-attention (not fix-all eligible) downstream.
        "install_cmd": None if install_state == "manual_install" else result.install_cmd,
        "plugin": ctx.plugin_name,
    })


# Ordered install-strategy dispatch table. Precedence is significant: skip
# (not-applicable-on-this-OS sentinel, beats everything incl. resolve and a
# same-OS download) -> resolve/link -> scoop (Windows) -> brew (macOS) -> apt
# (Ubuntu) -> url download (download.url+sha256) -> install command (re-check
# regardless of exit code, + manual sentinel). scoop, brew, and apt are mutually
# exclusive by OS (each reads only its own host's install value) and all run
# BEFORE url download, matching the per-OS method ladder ("scoop > download",
# "brew > download", "apt > download"); the pre-existing strategies keep their
# relative order (resolve -> scoop -> url -> install), with brew then apt
# inserted next to scoop. Each function takes the shared _ToolEntryCtx and
# returns a _StrategyOutcome; the dispatcher returns the first terminal
# outcome's ``failure``. install_command is always terminal, so the loop always
# resolves.
_INSTALL_STRATEGIES = (
    _strategy_skip,
    _strategy_requires,
    _strategy_resolve,
    _strategy_scoop,
    _strategy_brew,
    _strategy_apt,
    _strategy_url_download,
    _strategy_install_command,
)


def _normalize_tool_entry(tool_def, current_os):
    """Canonicalize legacy tool-entry spellings IN MEMORY (never on disk).

    The single manifest-normalization choke point: every tool entry -- from the
    layered user/project manifest, a per-plugin manifest, or engine self-setup --
    flows through _process_tool_entry, which calls this first, so downstream
    strategies (_ToolEntryCtx and the _INSTALL_STRATEGIES table) consume ONE
    canonical form. Legacy spellings stay backwards-READABLE; the input dict is
    NOT mutated (manifests may be shared / re-processed), and nothing is written
    back to the JSON files on disk.

    Normalizations, per analysis-dividing-line.md section 4 (4.2/4.5):

      1. install.<os> string -> {"command": <s>, "elevated": false} (the opaque
         command object). The "manual" sentinel is preserved as
         {"command": "manual", "elevated": false}; downstream still keys on the
         command string == "manual", so its manual-attention semantics are intact.
         The "skip" sentinel (not applicable on this OS) canonicalizes to
         {"skip": true} instead -- the skip strategy short-circuits the entry
         for that OS (design-os-not-applicable.md ruling). `elevated` is carried
         but not yet acted on (the elevation queue is a later step); any other
         bare string is exactly {"command": s, "elevated": false}.

      2. download.<os[-arch]>.scoop (the legacy, deprecated-but-read spelling) ->
         install.<os>.scoop (the canonical structured location). Only the entry
         _resolve_download_def would pick for THIS host is promoted, so per-arch
         behavior is unchanged. Scoop takes precedence over any command already
         at install.<os> -- this matches the dispatch order (the scoop strategy
         runs before the install-command strategy), which is load-bearing for
         entries that declare both (e.g. p4-kit: install.windows "manual" +
         download.scoop -> scoop wins).

    Returns a shallow-cloned tool_def with canonical `install` / `download`.
    """
    if not isinstance(tool_def, dict):
        return tool_def

    # 1. string install values -> opaque command objects (all OS keys, so the
    #    canonical shape is uniform regardless of which host runs). The "skip"
    #    sentinel ("not applicable on this OS") is special-cased BEFORE the
    #    generic string->command rule: it canonicalizes to {"skip": true},
    #    never to {"command": "skip"} (design-os-not-applicable.md ruling).
    install = {}
    for os_key, val in tool_def.get("install", {}).items():
        if val == "skip":
            install[os_key] = {"skip": True}
        elif isinstance(val, str):
            install[os_key] = {"command": val, "elevated": False}
        else:
            install[os_key] = val

    new_tool = dict(tool_def)

    # 2. legacy download.scoop -> canonical install.<os>.scoop (host-resolved).
    download = tool_def.get("download", {})
    resolved_dl = _resolve_download_def(download, current_os)
    if isinstance(resolved_dl, dict) and resolved_dl.get("scoop"):
        # scoop precedence over any command spelled at install.<os>.
        install[current_os] = {"scoop": resolved_dl["scoop"]}
        # Strip scoop out of the download block so canonical form owns it and no
        # downstream code reads scoop from `download` again.
        new_download = {}
        for k, v in download.items():
            if isinstance(v, dict) and "scoop" in v:
                v = {kk: vv for kk, vv in v.items() if kk != "scoop"}
            new_download[k] = v
        new_tool["download"] = new_download

    new_tool["install"] = install
    return new_tool


def _process_tool_entry(tool_def, current_os, data_dir, prefix, action_entries,
                        ok_entries, tools_installed, plugin_name,
                        machine_resolver=None):
    """Resolve one tool entry: check -> link-to-PATH -> download -> install.

    Shared by _process_self_setup and _process_manifest. Mutates
    action_entries / ok_entries / tools_installed in place. Returns a failure
    dict, or None on success.

    Dispatches the ordered _INSTALL_STRATEGIES table; each strategy shares the
    (_ToolEntryCtx) -> _StrategyOutcome contract. Precedence and behavior are
    identical to the former inline branches:
      - resolve: check_tool() via installPath candidates / `check` cmd / which;
        if resolved, record the path and link its dir onto PATH (philosophy P4).
      - scoop / brew / url download / install command run in order on a miss. After
        ANY install attempt the tool is re-checked regardless of the
        installer's exit code — installers exit non-zero for "already installed
        / no upgrade" (winget 43), so the re-check, not the exit code, decides
        presence. A failed url download falls through to the install command.

    ``data_dir`` is accepted for call-site symmetry; tool paths are recorded
    against the canonical bootstrap data dir (record(None, ...)).

    ``machine_resolver`` is the phase-shared lazy identity lookup for the
    `requires` strategy (env_manifest.MachineRequiresResolver); callers that
    never see `requires` entries may omit it (the strategy then builds its
    own over the user env.json layers).
    """
    tool_def = _normalize_tool_entry(tool_def, current_os)
    ctx = _ToolEntryCtx(tool_def, current_os, prefix, action_entries,
                        ok_entries, tools_installed, plugin_name,
                        machine_resolver=machine_resolver)
    for strategy in _INSTALL_STRATEGIES:
        outcome = strategy(ctx)
        if outcome.terminal:
            return outcome.failure
    return None


def _process_path_entries(path_entries, prefix, action_entries, ok_entries):
    """Check/remediate PATH entries (consolidate adds into one line).

    Shared by _process_self_setup and the manifest path_entries phase
    (previously two near-identical copies). Also prepends each entry to the
    current process PATH so subsequent phases this run can find tools there.
    Comparison uses normcase+normpath — on Windows, case-differing spellings
    of an already-present entry must not re-prepend every phase (B16).
    """
    from .path_check import add_path_to_shell_config, check_path_entry, normalize_path_for_compare

    paths_added = []
    for path_entry in path_entries:
        expanded = os.path.expanduser(path_entry)
        result = check_path_entry(path_entry)
        if result.passed:
            ok_entries.append(f"{prefix}PATH {result.subject}: ok - {result.message}")
        else:
            # Attempt persistent remediation: add to shell RC files
            _ok, msg = add_path_to_shell_config(path_entry)
            paths_added.append((result.subject, msg))
        current_path = os.environ.get("PATH", "")
        norm = [normalize_path_for_compare(d) for d in current_path.split(os.pathsep)]
        if normalize_path_for_compare(expanded) not in norm:
            os.environ["PATH"] = expanded + os.pathsep + current_path

    if paths_added:
        action_entries.append(f"{prefix}PATH added: {_join_items(paths_added)}")


def _process_venv_def(venv_def, data_dir, plugin_root, prefix, label, action_entries,
                      ok_entries, failures, plugin_name, failure_type="venv",
                      failure_plugin=None, always_sync=False, extras=(),
                      export_env_var=True):
    """Run the shared ensure_venv flow and route its outcome to the entry lists.

    One wrapper for all three venv call sites (self-setup, manifest, project
    venv); `label` is the log-entry noun ("venv" / "project_venv").
    """
    from .venv_check import ensure_venv, export_venv_env_var

    result, venv_entries = ensure_venv(
        plugin_root, os.path.join(data_dir, ".venv"),
        extras=extras,
        check_imports=venv_def.get("check_imports", []),
        always_sync=always_sync,
    )
    action_entries.extend(f"{prefix}{label}: {e}" for e in venv_entries)
    if result.passed:
        ok_entries.append(f"{prefix}{label}: ok - {result.message}")
        if export_env_var:
            exported = export_venv_env_var(plugin_name, data_dir)
            if exported:
                ok_entries.append(f"{prefix}{label}: exported {exported} to CLAUDE_ENV_FILE")
    else:
        action_entries.append(f"{prefix}{label}: FAILED - {result.message}")
        failures.append({
            "type": failure_type,
            "message": result.message,
            "remediation_cmd": result.remediation_cmd,
            "plugin": failure_plugin or plugin_name,
        })
    return result


def _process_dead_path_entries(data_dir, prefix, ok_entries):
    """Detect dead Windows User PATH entries; defer their removal to fix-all.

    DETECT ONLY. Deleting PATH entries is destructive with no undo, so it never
    happens as a side effect of a background SessionStart hook -- it goes in the
    fix queue, where the user consents to it (and can read the exact entries in
    queue.json first). This is the one queued operation that needs no privilege;
    it is queued for consent, not for elevation.

    OPPORTUNISTIC: pruning is housekeeping, not a blocker, so the descriptor is
    flagged `opportunistic` -- it rides the fix queue whenever something that
    genuinely needs the runner is also queued, but a queue containing only this
    task surfaces no nag at all (see _elevation_step). The finding still logs
    every session and the queue/shim stay on disk for a user who wants to run
    the prune by hand.

    Silent when clean (an ok entry), a persistent failure when not: the item
    must survive a declined fix-all, so it re-offers (when something actionable
    is alongside it) next session instead of being detected once and forgotten.
    The scan itself is cached on a PATH hash -- see bootstrap_lib.path_prune
    for why caching the RESULT rather than "already reported" is what makes
    that work.

    Returns a failure dict, or None when there is nothing to prune.
    """
    from .path_prune import scan, stamp_path

    dead = scan(data_dir)
    if dead is None:
        # No verdict: nothing to read (not Windows, or the registry is
        # off-limits). Say nothing rather than report a check that never ran as
        # a check that passed.
        return None
    if not dead:
        ok_entries.append(f"{prefix}User PATH: no dead entries")
        return None

    count = len(dead)
    noun = "entry" if count == 1 else "entries"
    ok_entries.append(
        f"{prefix}User PATH: {count} dead {noun} (queued opportunistically; "
        f"prunes when a real fix needs the runner)"
    )
    return {
        "type": "path_prune",
        "name": "dead-path-entries",
        "message": (
            f"Windows User PATH has {count} dead {noun} (directories that no "
            f"longer exist)"
        ),
        "elevation": {
            "method": "path_prune",
            "os": "windows",
            "id": "path_prune",
            "label": f"Remove {count} dead PATH {noun}",
            "entries": dead,
            "backup": os.path.join(os.path.dirname(stamp_path(data_dir)),
                                   "path_backup.txt"),
            "opportunistic": True,
        },
        "agent_msg": (
            f"The Windows User PATH has {count} dead {noun} pointing at "
            f"directories that no longer exist. Bootstrap deferred the removal "
            f"into the fix queue rather than doing it unasked -- deleting PATH "
            f"entries is destructive. The exact entries are listed in the "
            f"queue file."
        ),
        "plugin": "bootstrap",
        "persist_across_sessions": True,
    }


def _process_self_setup(self_setup, current_os, data_dir, plugin_root, action_entries, ok_entries, plugin_name="bootstrap"):
    """Process engine self-setup: tools, path_entries, venv.

    Only these 3 phases — the minimum needed to make the engine runnable.
    Always runs `uv sync` to keep the venv current (~100ms no-op when up to date).
    Returns list of failures.
    """
    failures = []
    p = "[bootstrap-setup] "

    # Check tools (consolidate installs into one line; failures stay per-line)
    tools_installed = []
    for tool_def in self_setup.get("tools", []):
        failure = _process_tool_entry(
            tool_def, current_os, data_dir, p,
            action_entries, ok_entries, tools_installed, plugin_name="bootstrap",
        )
        if failure:
            failures.append(failure)

    if tools_installed:
        action_entries.append(f"{p}tools installed: {_join_items(tools_installed)}")

    # Check path entries (consolidate adds into one line)
    _process_path_entries(self_setup.get("path_entries", []), p, action_entries, ok_entries)

    # Dead User PATH entries (Windows). AFTER the adds above: those can add an
    # entry whose directory does not exist yet, and scanning first would report
    # it as dead in the same breath that bootstrap created it.
    prune_failure = _process_dead_path_entries(data_dir, p, ok_entries)
    if prune_failure:
        failures.append(prune_failure)

    # Check for Python stubs shadowing the standalone python (Windows-only check)
    stub_def = self_setup.get("python_stub_check")
    if stub_def:
        from .python_stub_check import check_python_stub, write_fix_script
        good_python_dir = stub_def.get("good_python_dir", "~/.local/share/python-standalone/python")
        stub_markers = stub_def.get("stub_markers", ["WindowsApps"])
        script_output_dir = stub_def.get("script_output_dir", "~/Desktop")

        stub_result = check_python_stub(good_python_dir, stub_markers)
        if stub_result.passed:
            ok_entries.append(f"{p}python stub: ok - {stub_result.message}")
        else:
            ok_write, write_msg, script_path = write_fix_script(good_python_dir, script_output_dir)
            if ok_write:
                # User-visible action entry (also written into the bootstrap log)
                action_entries.append(
                    f"{p}python stub: detected {stub_result.bad_python}; "
                    f"wrote fix script to {script_path}"
                )
                # Focused user-facing and Claude-facing messages.
                user_msg = (
                    "Claude needs your help! Run the fix_python_path script that is on "
                    "your desktop as administrator to make python accessible to Claude."
                )
                agent_msg = (
                    "A Microsoft Store Python stub is shadowing the standalone Python "
                    "that plugins-kit installed, blocking Claude's access to a working "
                    f"python.exe. Detected stub at: {stub_result.bad_python}. A fix script "
                    f"has been written to the user's desktop at {script_path}. The user "
                    "must double-click it (it self-elevates via UAC) or right-click and "
                    "choose 'Run as administrator'. The script prepends the standalone "
                    "Python directory to the System PATH and then deletes itself. After "
                    "the user runs it successfully, they need to start a new Claude Code "
                    "session for the new System PATH to take effect. If the user asks for "
                    "help, walk them through these steps. Do NOT attempt to run the script "
                    "yourself — it requires interactive UAC consent."
                )
                failures.append({
                    "type": "python_stub",
                    "name": "python_stub",
                    "user_msg": user_msg,
                    "agent_msg": agent_msg,
                    "message": user_msg,  # legacy field for general consumers
                    "bad_python": stub_result.bad_python,
                    "script_path": script_path,
                    "plugin": "bootstrap",
                    "persist_across_sessions": True,
                })
            else:
                action_entries.append(
                    f"{p}python stub: detected {stub_result.bad_python}, "
                    f"could not write fix script: {write_msg}"
                )
                user_msg = (
                    "Claude needs your help! A bad python is shadowing the standalone "
                    f"python plugins-kit installed, and the fix script could not be "
                    f"written automatically. Manually prepend {stub_result.good_python_dir} "
                    "to your System PATH."
                )
                agent_msg = (
                    f"A Microsoft Store Python stub at {stub_result.bad_python} is shadowing "
                    f"the standalone Python, and the fix script could not be written: "
                    f"{write_msg}. The user must manually prepend "
                    f"{stub_result.good_python_dir} to their System PATH (Windows Settings -> "
                    "Edit the system environment variables -> Environment Variables -> System "
                    "variables -> Path -> New -> move to top), then start a new Claude Code "
                    "session."
                )
                failures.append({
                    "type": "python_stub",
                    "name": "python_stub",
                    "user_msg": user_msg,
                    "agent_msg": agent_msg,
                    "message": user_msg,
                    "bad_python": stub_result.bad_python,
                    "script_path": None,
                    "plugin": "bootstrap",
                    "persist_across_sessions": True,
                })

    # Check venv — always run uv sync to keep deps current (~100ms no-op when up to date)
    venv_def = self_setup.get("venv")
    if venv_def:
        _process_venv_def(
            venv_def, data_dir, plugin_root, p, "venv",
            action_entries, ok_entries, failures,
            plugin_name=plugin_name, failure_plugin="bootstrap", always_sync=True,
        )

    return failures


def _process_project_venv(venv_def, project_dir):
    """Process project_venv: ensure the project's own .venv is ready.

    Unlike the plugin venv (which lives in data_dir), this targets the
    project's own pyproject.toml. By default that is the project root
    (<project_dir>/pyproject.toml -> <project_dir>/.venv); an optional
    'subdir' names a project-relative subdirectory that becomes BOTH the
    uv-sync project target and the .venv parent
    (<project_dir>/<subdir>/pyproject.toml -> <project_dir>/<subdir>/.venv)
    -- for layouts like env-config's python/ package dir. A subdir that is
    absolute or resolves outside project_dir is a descriptive failure (fail
    fast; no fallback to the root).

    Args:
        venv_def: Dict with optional 'subdir' (str), 'extras' (list), and
            'check_imports' (list).
        project_dir: Absolute path to the project root.

    Returns:
        (action_entries, ok_entries, failures) tuple.
    """
    action_entries = []
    ok_entries = []
    failures = []

    target_dir = project_dir
    subdir = venv_def.get("subdir")
    if subdir:
        root = os.path.abspath(project_dir)
        resolved = os.path.abspath(os.path.join(root, subdir))
        if os.path.isabs(subdir) or not (
            resolved == root or resolved.startswith(root + os.sep)
        ):
            msg = (
                f"subdir {subdir!r} must be a relative path inside the project "
                f"(it resolves to {resolved}, outside {root})"
            )
            action_entries.append(f"project_venv: FAILED - {msg}")
            failures.append({
                "type": "project_venv",
                "message": msg,
                "remediation_cmd": None,
                "plugin": "config",
            })
            return action_entries, ok_entries, failures
        target_dir = resolved

    # target_dir serves as both data_dir (.venv location) and plugin_root
    # (pyproject.toml location). No env-var export: the project venv belongs
    # to the project, not a plugin.
    _process_venv_def(
        venv_def, target_dir, target_dir, "", "project_venv",
        action_entries, ok_entries, failures,
        plugin_name="config", failure_type="project_venv", failure_plugin="config",
        extras=venv_def.get("extras", []), export_env_var=False,
    )

    return action_entries, ok_entries, failures


def _process_config(config_section, plugin_data_dir, plugin_root, action_entries, ok_entries=None, plugin_name="", project_detected=True):
    """Process the config section of a plugin manifest.

    Runs outside the cache gate — config can change between sessions.
    Returns list of failures (missing config fields).

    When project_detected is False, still copies defaults and runs autodetect,
    but skips required_fields validation (no project = no config failures).
    """
    from .config_check import config_init, config_validate, run_autodetect, load_yaml_config, save_yaml_config

    config_file = config_section["file"]
    defaults_source = config_section.get("defaults_source")

    # 1. Config init: copy defaults if config doesn't exist
    if defaults_source:
        config_path = config_init(plugin_data_dir, plugin_root, defaults_source, config_file)
    else:
        config_path = os.path.join(plugin_data_dir, config_file)

    if not os.path.isfile(config_path):
        return []

    # 2. Load config
    config = load_yaml_config(config_path)

    required_fields = config_section.get("required_fields", {})

    # 3. Autodetect (optional): always run when declared
    autodetect_spec = config_section.get("autodetect")
    if autodetect_spec:
        try:
            changed, ad_actions, ad_ok = run_autodetect(plugin_root, autodetect_spec, config, config_path)
            action_entries.extend(ad_actions)
            if ok_entries is not None:
                ok_entries.extend(ad_ok)
            else:
                action_entries.extend(ad_ok)
            if changed:
                save_yaml_config(config_path, config)
                if not ad_actions:
                    action_entries.append("config autodetect updated values")
        except Exception as e:
            # Autodetect errors are non-fatal, but never silent: every check
            # logs its outcome (B8).
            action_entries.append(f"config autodetect FAILED - {e}")

    # 4. Validate required fields (apply defaults, collect missing)
    # Skip validation when no project detected — required fields are project-scoped
    if not project_detected:
        if ok_entries is not None:
            ok_entries.append("config: skipped required_fields (no project detected)")
        else:
            action_entries.append("config: skipped required_fields (no project detected)")
        return []

    config, missing = config_validate(config, required_fields, config_path)

    # Write back if defaults were applied
    if any(f.get("default") is not None for f in required_fields.values()):
        # Re-check if any defaults were actually applied (config may have changed)
        current_on_disk = load_yaml_config(config_path)
        if config != current_on_disk:
            save_yaml_config(config_path, config)

    if not missing:
        if ok_entries is not None:
            ok_entries.append("config ok")
        else:
            action_entries.append("config ok")
        return []

    # 5. Fix-all: aggregate missing fields into failure directives
    failures = []
    for m in missing:
        failures.append({
            "type": "config",
            "field": m["field"],
            "user_msg": m["user_msg"],
            "agent_msg": m["agent_msg"],
            "plugin": plugin_name,
        })

    return failures


def _normalize_project_required_fields(required_fields):
    """Normalize required_fields to dict form.

    Accepts either:
    - list of field names (strings) — legacy flat form, each becomes {}
    - dict keyed by field name, values are {user_msg, agent_msg, default?} — dict form

    Returns a dict mapping field name -> field spec (dict).
    """
    if isinstance(required_fields, dict):
        return {
            name: (spec if isinstance(spec, dict) else {})
            for name, spec in required_fields.items()
        }
    # Treat as iterable of names (list/tuple)
    return {name: {} for name in required_fields}


def _legacy_remove(path):
    """Delete a file, clearing the read-only bit if the OS rejects the first try.

    Windows surfaces P4-tracked files as read-only on disk, so a plain os.remove
    raises PermissionError. Cleanup is intentional in the migration flow, so
    relax the mode and retry once. Any second failure propagates.
    """
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)


def _legacy_replace(src, dst):
    """Move src to dst, clearing read-only on either side if Windows balks.

    os.replace fails if the destination is read-only (Windows) or if the source
    can't be unlinked. Make both writable on PermissionError, then retry once.
    """
    try:
        os.replace(src, dst)
    except PermissionError:
        for p in (src, dst):
            if os.path.isfile(p):
                os.chmod(p, stat.S_IWRITE)
        os.replace(src, dst)


def _rmdir_if_empty(path):
    """Best-effort: remove a directory only if it is now empty. Never raises.

    Used after a legacy-file migration to leave nothing behind -- if the old
    file was the sole occupant of its directory, drop the empty directory too.
    A non-empty dir (siblings still present), a missing dir, or a permission
    error all simply leave the directory in place.
    """
    try:
        os.rmdir(path)
    except OSError:
        pass


def _process_project_config(project_config_section, plugin_data_dir, plugin_root, action_entries, ok_entries=None, plugin_name="", failures=None):
    """Process the project_config section of a plugin manifest.

    Discovers or reads per-project config (in CWD), syncs values to the data-dir config.

    ``required_fields`` accepts either a flat list of field names (legacy) or a dict
    keyed by field name with values ``{user_msg, agent_msg, default?}``. In dict form:
    - A declared ``default`` is applied when the field is missing from both the file
      and autodetect output (defaults never override already-populated values).
    - Fields that remain missing after autodetect + defaults are emitted as fix-all
      failure entries on the optional ``failures`` list.

    Returns True if a project was detected (config exists or autodetect succeeded),
    False if no project was found (autodetect returned None / no file / no autodetect).
    """
    from .config_check import load_yaml_config, save_yaml_config, run_project_autodetect

    config_file = project_config_section["file"]
    required_fields_spec = _normalize_project_required_fields(
        project_config_section.get("required_fields", [])
    )
    required_field_names = list(required_fields_spec.keys())
    autodetect_spec = project_config_section.get("autodetect")
    legacy_file = project_config_section.get("legacy_file")

    project_config_path = os.path.join(os.getcwd(), config_file)

    # Legacy migration: when the manifest declares a legacy_file, reconcile the
    # old and new paths so downstream logic can run against the new path as if
    # it had always been there. Four cases:
    #   1. only legacy exists           -> move legacy to new path
    #   2. both exist, legacy <= new    -> delete legacy (new is fresher/equal)
    #   3. both exist, legacy >  new    -> move legacy to new (overwrite stale)
    #   4. only new exists, or neither  -> no-op (downstream handles creation)
    # Cases 2/3 cover sessions that ran before legacy_file was honored: the
    # engine had already created a new file from defaults/autodetect, leaving
    # the legacy file orphaned alongside it. mtime decides which copy wins.
    # Wrapped in try/except so a hostile filesystem state (file locked, ACL
    # blocked, etc.) downgrades to a warning instead of killing the whole
    # bootstrap run.
    if legacy_file:
        legacy_path = os.path.join(os.getcwd(), legacy_file)
        try:
            legacy_exists = os.path.isfile(legacy_path)
            new_exists = os.path.isfile(project_config_path)
            migrated = False
            if legacy_exists and not new_exists:
                os.makedirs(os.path.dirname(project_config_path), exist_ok=True)
                _legacy_replace(legacy_path, project_config_path)
                migrated = True
                action_entries.append(
                    f"project config: migrated {legacy_path} -> {project_config_path}"
                )
            elif legacy_exists and new_exists:
                if os.path.getmtime(legacy_path) <= os.path.getmtime(project_config_path):
                    _legacy_remove(legacy_path)
                    migrated = True
                    action_entries.append(
                        f"project config: removed stale legacy {legacy_path} (new path {project_config_path} is fresher)"
                    )
                else:
                    _legacy_replace(legacy_path, project_config_path)
                    migrated = True
                    action_entries.append(
                        f"project config: migrated {legacy_path} -> {project_config_path} (overwrote stale new path)"
                    )
            # Clean up after the migration: if the legacy file was the only thing
            # in its directory, drop the now-empty directory too.
            if migrated:
                _rmdir_if_empty(os.path.dirname(legacy_path))
        except OSError as e:
            # Most common cause on Windows: the legacy file is read-only because
            # source control (e.g. Perforce) hasn't checked it out for delete.
            # Surface as a warning and let the user resolve manually rather than
            # dying mid-bootstrap.
            action_entries.append(
                f"project config: WARNING failed to reconcile {legacy_path} -> {project_config_path}: {e}"
            )

    file_changed = False  # Track whether project_data was modified from disk state

    if os.path.isfile(project_config_path):
        # File exists — load it and check required fields
        project_data = load_yaml_config(project_config_path)
        missing_fields = [f for f in required_field_names if not project_data.get(f)]

        if missing_fields and autodetect_spec:
            # Some fields missing — try autodetect to fill gaps
            detected = run_project_autodetect(plugin_root, autodetect_spec, errors=action_entries)
            if detected:
                for field in missing_fields:
                    if detected.get(field):
                        project_data[field] = detected[field]
                        file_changed = True
                if file_changed:
                    save_yaml_config(project_config_path, project_data)
                    action_entries.append(f"project config: updated {project_config_path}")
                else:
                    if ok_entries is not None:
                        ok_entries.append(f"project config: ok - {project_config_path}")
            else:
                # Autodetect returned None — no active project in CWD
                # Stale config file exists but no project is present
                if ok_entries is not None:
                    ok_entries.append("project config: no project detected (stale config)")
                return False
        else:
            if ok_entries is not None:
                ok_entries.append(f"project config: ok - {project_config_path}")
    else:
        # File doesn't exist — try autodetect
        if autodetect_spec:
            detected = run_project_autodetect(plugin_root, autodetect_spec, errors=action_entries)
            if detected:
                os.makedirs(os.path.dirname(project_config_path), exist_ok=True)
                project_data = dict(detected)
                # Apply defaults for any declared field still missing from detected
                defaults_applied = _apply_project_defaults(project_data, required_fields_spec)
                save_yaml_config(project_config_path, project_data)
                if defaults_applied:
                    action_entries.append(
                        f"project config: created {project_config_path} (with defaults: {', '.join(defaults_applied)})"
                    )
                else:
                    action_entries.append(f"project config: created {project_config_path}")
                file_changed = True
            else:
                if ok_entries is not None:
                    ok_entries.append("project config: no project detected")
                return False  # Nothing detected — downstream phases should skip project-scoped work
        else:
            if ok_entries is not None:
                ok_entries.append("project config: no project detected")
            return False  # No file, no autodetect — nothing to do

    # Apply declared defaults for any required field still missing after autodetect.
    # Defaults never override already-populated values.
    defaults_applied_now = _apply_project_defaults(project_data, required_fields_spec)
    if defaults_applied_now:
        save_yaml_config(project_config_path, project_data)
        action_entries.append(
            f"project config: applied defaults [{', '.join(defaults_applied_now)}] to {project_config_path}"
        )
        file_changed = True

    # Collect fix-all failures for any field that is still missing and has no default.
    # Only applies in dict form (string-list form defines no user/agent messages).
    if failures is not None:
        for field_name, field_spec in required_fields_spec.items():
            if project_data.get(field_name):
                continue
            if field_spec.get("default") is not None:
                continue  # default already applied above
            if not field_spec:
                # String-list form carries no messages — skip fix-all emission
                continue
            agent_msg = field_spec.get(
                "agent_msg", f"Set {field_name} in {project_config_path}"
            ).replace("{config_path}", project_config_path)
            failures.append({
                "type": "project_config",
                "field": field_name,
                "user_msg": field_spec.get("user_msg", field_name),
                "agent_msg": agent_msg,
                "plugin": plugin_name,
            })

    # Sync discovered values to data-dir config
    data_config_path = os.path.join(plugin_data_dir, "config.yaml")
    if os.path.isfile(data_config_path):
        data_config = load_yaml_config(data_config_path)
    else:
        data_config = {}

    changed = False
    for field in required_field_names:
        val = project_data.get(field, "")
        if val and val != data_config.get(field, ""):
            data_config[field] = val
            changed = True

    if changed:
        save_yaml_config(data_config_path, data_config)

    return True


def _apply_project_defaults(project_data, required_fields_spec):
    """Apply declared defaults for any required field not already set in project_data.

    Mutates ``project_data`` in place. Returns the list of field names that received
    a default (empty if none).
    """
    applied = []
    for field_name, field_spec in required_fields_spec.items():
        if project_data.get(field_name):
            continue
        default = field_spec.get("default")
        if default is None:
            continue
        project_data[field_name] = default
        applied.append(field_name)
    return applied


class _ManifestContext:
    """Shared state for the manifest phase handlers (B12).

    Wraps the two entry lists with routing helpers that make the
    "every check must log its outcome" contract structural:

    - ``ok(msg)``     -> ok_entries (verbose-only)
    - ``action(msg)`` -> action_entries (always shown)
    - ``quiet(msg)``  -> quiet_entries: ALWAYS logged (like an action, never
      gated on log_success) but never displayed. For remediations whose
      per-plugin line is noise because the pass reports them in aggregate --
      currently only the shared-lib publish/link events, which Step 4c renders
      as one line for the whole pass (see _SharedLibLinkLog).
    - ``fail(msg, **failure)`` -> action entry AND fix-all failure dict in a
      single call, so a registered failure can never be invisible to the user.

    ``config`` / ``variables`` load lazily on first use; phases before
    ini_settings don't need them.
    """

    _UNSET = object()

    def __init__(self, manifest, current_os, data_dir, plugin_root,
                 action_entries, ok_entries, plugin_name, project_dir,
                 project_detected, quiet_entries=None, shared_lib_links=None):
        self.manifest = manifest
        self.current_os = current_os
        self.data_dir = data_dir
        self.plugin_root = plugin_root
        self.action_entries = action_entries
        self.ok_entries = ok_entries
        self.quiet_entries = [] if quiet_entries is None else quiet_entries
        self.shared_lib_links = shared_lib_links
        self.plugin_name = plugin_name
        self.project_dir = project_dir
        self.project_detected = project_detected
        self.failures = []
        self.prefix = ""
        self._config = self._UNSET
        self._variables = None

    @property
    def config(self):
        if self._config is self._UNSET:
            self._config = _load_plugin_config(self.data_dir, self.action_entries)
        return self._config

    @property
    def variables(self):
        if self._variables is None:
            from .var_resolve import build_variables
            self._variables = build_variables(self.plugin_root, self.data_dir, self.config)
        return self._variables

    def ok(self, message):
        self.ok_entries.append(f"{self.prefix}{message}")

    def action(self, message, display=None, detail=None):
        """Append an action entry.

        `message` is the COMPLETE diagnostic -- write it at whatever length it
        needs, since the log and the pass record keep it whole. `display` is an
        optional short label for the collated display line, needed only when the
        message has no natural short form (no " - " clause to drop); `detail`
        carries structured context that belongs in the record but on no message
        surface. See messages.py and records.py.
        """
        _append_detail(self.action_entries, f"{self.prefix}{message}",
                       display=display, detail=detail)

    def quiet(self, message):
        """Log-only remediation entry: written to the log unconditionally, never
        displayed. Use ONLY when the pass surfaces the same event in aggregate."""
        self.quiet_entries.append(f"{self.prefix}{message}")

    def fail(self, entry, display=None, detail=None, **failure):
        """Append `entry` as an action line AND register `failure` for fix-all.

        (`entry` deliberately doesn't collide with the failure-dict `message`
        kwarg most callers pass.) `display`/`detail` behave as in `action`.
        """
        self.action(entry, display=display, detail=detail)
        failure.setdefault("plugin", self.plugin_name)
        self.failures.append(failure)


def _entry_label(name):
    """Failure-dict label for a possibly-missing entry name.

    Invalid manifest entries may lack a usable name; a stable "(unnamed)"
    placeholder beats str(None)'s misleading "None".
    """
    if name is None or (isinstance(name, str) and not name):
        return "(unnamed)"
    return str(name)


def _phase_env_vars(ctx):
    """env_vars: persist + live-export environment variables.

    Runs FIRST in the phase table (order is load-bearing): install commands
    in any later phase of the same pass may reference these variables (e.g.
    an install command invoking ``$DEVROOT/...``). Each entry is
    ``{"name", "value"}``; ``~`` in the value expands to the user's home at
    apply time so committed manifests stay identity-free. The variable is
    exported into the live process + $CLAUDE_ENV_FILE every pass, and
    persisted (shell rc in-place update on Unix, User registry on Windows)
    when not already in the wanted state. The post-set re-check is
    authoritative.

    PATH is never an env_vars concern (spec directive 3): a PATH entry (any
    case) is rejected as a hard failure before any export or write -- PATH
    edits belong exclusively to ``path_entries`` + tool->PATH linkage, and
    an env_vars PATH entry would clobber the composed value they manage.
    """
    from .env_var_check import check_env_var, export_env_var, set_env_var

    vars_set = []
    for var_def in ctx.manifest.get("env_vars", []):
        name = var_def.get("name")
        value = var_def.get("value")
        if not isinstance(name, str) or not name or not isinstance(value, str):
            # Name the entry by its KEYS, never by its repr. An env_vars entry
            # is frequently an API key, and `{var_def!r}` dumps the value
            # alongside it -- into the log, the message surfaces, and now a
            # durable record. The keys identify the offending entry just as
            # well for an authoring error, which is all this failure is.
            ctx.fail(
                f"env_var: INVALID entry {sorted(var_def)!r} - needs string 'name' and 'value'",
                type="env_var",
                name=_entry_label(name),
                message=f"invalid env_vars entry {sorted(var_def)!r}: needs string 'name' and 'value'",
            )
            continue
        if name.upper() == "PATH":
            ctx.fail(
                f"env_var {name}: REFUSED - PATH is managed by bootstrap path_entries, never env_vars",
                type="env_var",
                name=name,
                message=(
                    f"{name}: PATH is never an env_vars concern. PATH edits "
                    f"belong exclusively to bootstrap 'path_entries' (and "
                    f"the automatic tool->PATH linkage); remove this "
                    f"env_vars entry."
                ),
            )
            continue
        expanded = os.path.expanduser(value)
        exported = export_env_var(name, expanded)
        if exported:
            ctx.ok(f"env_var {name}: exported to CLAUDE_ENV_FILE")

        result = check_env_var(name, expanded, ctx.current_os)
        if result.passed:
            ctx.ok(f"env_var {name}: ok - {result.message}")
            continue
        set_ok, msg = set_env_var(name, expanded, ctx.current_os)
        recheck = check_env_var(name, expanded, ctx.current_os)
        if set_ok and recheck.passed:
            vars_set.append((name, msg))
        else:
            detail = msg if not set_ok else f"set reported '{msg}' but re-check failed: {recheck.message}"
            ctx.fail(
                f"env_var {name}: FAILED - {detail}",
                type="env_var",
                name=name,
                message=f"{name}: {detail}",
            )

    if vars_set:
        ctx.action(f"env vars set: {_join_items(vars_set)}")


def _phase_tools(ctx):
    """tools: resolve -> link-to-PATH -> download -> install.

    Consolidates successful installs into one line; failures stay per-line.
    """
    from .env_manifest import MachineRequiresResolver

    # One phase-shared identity lookup for `requires` entries. Construction
    # does no I/O; env.json is read (once, memoized) only if some entry in
    # this manifest actually declares `requires` -- a manifest without one
    # never resolves identity at all.
    machine_resolver = MachineRequiresResolver(ctx.project_dir)
    tools_installed = []
    for tool_def in ctx.manifest.get("tools", []):
        failure = _process_tool_entry(
            tool_def, ctx.current_os, ctx.data_dir, ctx.prefix,
            ctx.action_entries, ctx.ok_entries, tools_installed,
            plugin_name=ctx.plugin_name, machine_resolver=machine_resolver,
        )
        if failure:
            ctx.failures.append(failure)

    if tools_installed:
        ctx.action(f"tools installed: {_join_items(tools_installed)}")


def _phase_fonts(ctx):
    """fonts: download + per-user install when missing.

    Fonts are OS-agnostic, so the `download` block is normally flat
    ({url, sha256}); a per-OS nesting is still honored for the rare case it's
    needed. Install is unprivileged on every platform, so it runs silently
    here; a missing font is cosmetic (glyphs fall back to ASCII/emoji), so a
    failed download logs an action line and retries next session rather than
    blocking.
    """
    from .font_check import check_font, install_font

    fonts_installed = []
    for font_def in ctx.manifest.get("fonts", []):
        # `fonts` is a layered-mergeable section (user/project bootstrap.json),
        # so a hand-authored entry could omit `name`. Skip it with a logged
        # action rather than letting a KeyError abort the whole bootstrap run.
        name = font_def.get("name") if isinstance(font_def, dict) else None
        if not name:
            ctx.action("font: skipped malformed entry (missing 'name')")
            continue
        match = font_def.get("match") or name
        res = check_font(match)
        if res.passed:
            ctx.ok(f"font {name}: ok - {res.message}")
            continue

        dl_def = font_def.get("download", {})
        if isinstance(dl_def, dict) and "url" not in dl_def:
            dl_def = _resolve_download_def(dl_def, ctx.current_os) or {}
        if not (isinstance(dl_def, dict) and dl_def.get("url") and dl_def.get("sha256")):
            ctx.action(
                f"font {name}: not installed and no download declared for {ctx.current_os}",
                display=f"font {name}: no download declared",
            )
            continue

        inst = install_font(dl_def["url"], dl_def["sha256"], archive_type=dl_def.get("archive_type"))
        if inst.ok:
            recheck = check_font(match)
            detail = f"{len(inst.files)} files" + ("" if recheck.passed else " (not yet detected)")
            fonts_installed.append((name, detail))
        else:
            ctx.action(f"font {name}: install failed - {inst.message}")

    if fonts_installed:
        ctx.action(f"fonts installed: {_join_items(fonts_installed)}")


def _phase_path_entries(ctx):
    """path_entries: persistent PATH remediation (shared with self-setup)."""
    _process_path_entries(
        ctx.manifest.get("path_entries", []), ctx.prefix,
        ctx.action_entries, ctx.ok_entries,
    )


def _phase_venv(ctx):
    """venv: the shared check -> uv sync -> re-check flow (ensure_venv)."""
    _process_venv_def(
        ctx.manifest["venv"], ctx.data_dir, ctx.plugin_root, ctx.prefix, "venv",
        ctx.action_entries, ctx.ok_entries, ctx.failures,
        plugin_name=ctx.plugin_name,
    )


def _phase_git_deps(ctx):
    """git_deps: clone-once + pinned-commit re-checkout.

    A clone that exists on the right branch passes and is never pulled in
    steady state ("clone once"); only pinned commits are re-checked-out.
    Successes consolidate by verb (cloned/pulled/checked-out); failures stay
    per-line.
    """
    import subprocess as _sp2

    from .git_dep_check import check_git_dep, clone_git_dep, pull_git_dep

    git_cloned = []
    git_pulled = []
    git_checked_out = []
    for dep_def in ctx.manifest.get("git_deps", []):
        result = check_git_dep(
            ctx.data_dir,
            dep_def["url"],
            dep_def["branch"],
            dep_def.get("sparse_paths"),
            dep_def.get("commit"),
        )
        if result.passed:
            ctx.ok(f"git {result.subject}: ok - {result.message}")
            continue

        target_path = result.target_path
        pinned_commit = dep_def.get("commit")
        if not os.path.isdir(target_path):
            ok, msg = clone_git_dep(dep_def["url"], dep_def["branch"], target_path, dep_def.get("sparse_paths"), pinned_commit)
            verb = "cloned"
            detail = dep_def["url"]
        elif pinned_commit:
            try:
                _sp2.run(["git", "-C", target_path, "fetch"], capture_output=True, timeout=60)
                r = _sp2.run(["git", "-C", target_path, "checkout", pinned_commit], capture_output=True, text=True, timeout=30)
                ok = r.returncode == 0
                msg = f"checked out {pinned_commit[:7]}" if ok else (r.stderr.strip() or "checkout failed")
            except (_sp2.SubprocessError, OSError) as e:
                ok, msg = False, str(e)
            verb = "checked out"
            detail = pinned_commit[:7]
        else:
            ok, msg = pull_git_dep(target_path)
            verb = "pulled"
            detail = ""

        if ok:
            if verb == "cloned":
                git_cloned.append((result.subject, detail))
            elif verb == "pulled":
                git_pulled.append((result.subject, detail))
            else:
                git_checked_out.append((result.subject, detail))
        else:
            ctx.fail(
                f"git {result.subject}: FAILED - {msg}",
                type="git_dep",
                name=result.subject,
                message=msg,
                remediation_cmd=result.remediation_cmd,
            )

    if git_cloned:
        ctx.action(f"git cloned: {_join_items(git_cloned)}")
    if git_pulled:
        ctx.action(f"git pulled: {_join_items(git_pulled)}")
    if git_checked_out:
        ctx.action(f"git checked out: {_join_items(git_checked_out)}")


def _phase_sync_to_data(ctx):
    """sync_to_data: copy plugin source dirs to the stable data dir.

    Rule: successful outcomes -> ok_entries (verbose-only); failures/actions ->
    action_entries (always shown).
    """
    import shutil

    for sync_def in ctx.manifest.get("sync_to_data", []):
        src_rel = sync_def["src"]
        dst_rel = sync_def["dst"]
        src = os.path.join(ctx.plugin_root, src_rel)
        dst = os.path.join(ctx.data_dir, dst_rel)
        if not os.path.isdir(src):
            ctx.fail(
                f"sync {src_rel} -> {dst_rel}: FAILED - source not found",
                type="sync_to_data",
                src=src_rel,
                dst=dst_rel,
                message=f"source directory not found: {src}",
            )
            continue
        os.makedirs(dst, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        _ensure_shell_scripts_executable(dst)
        ctx.ok(f"sync {src_rel} -> {dst_rel}: ok")


def _ensure_shell_scripts_executable(root):
    """Grant exec on synced *.sh files wherever read is granted.

    Marketplace clones can lose the exec bit (mode committed as 100644,
    core.fileMode=false, Windows checkouts), and copytree preserves the
    stripped mode — leaving e.g. a synced statusline.sh that the harness
    cannot execute (EACCES, silent blank statusline).
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".sh"):
                continue
            path = os.path.join(dirpath, name)
            mode = os.stat(path).st_mode
            os.chmod(path, mode | ((mode & 0o444) >> 2))


# Marketplaces for which a `pin` was declared by a manifest processed earlier
# in THIS engine run. The layered user manifest (the recommended pin home) is
# processed in Step 3c, BEFORE plugin bootstrap.json files in Step 4 — without
# this guard, a plugin manifest's unpinned entry for the same marketplace
# (e.g. bootstrap's own plugins-kit entry) would unpin/update it right after
# the layered pin was applied. One engine process == one run, so no reset is
# needed outside tests.
_pinned_marketplaces_this_run = set()


def _phase_marketplaces(ctx):
    """marketplaces: register + pin/unpin + (alwaysUpdate) refresh.

    Runs before json_entries — marketplaces must be cloned before we merge
    fields like autoUpdate into known_marketplaces.json.

    Pin semantics: a `pin` (git committish) snapshots the entire marketplace
    clone — it freezes FUTURE drift but never downgrades plugins already past
    the snapshot. `pin` takes precedence over `alwaysUpdate`. A pin-only entry
    (no `source`) works when the marketplace is already registered — the
    common case for a pin declared in a layered user manifest. When `pin` is
    absent but the marker file records one, the pin is released (branch
    restored + marketplace updated).
    """
    from .marketplace_lifecycle import (
        check_marketplace_exists, check_marketplace_current,
        add_marketplace, remove_marketplace, update_marketplace,
        apply_marketplace_pin, release_marketplace_pin, load_pin_markers,
    )

    for mkt_def in ctx.manifest.get("marketplaces", []):
        mkt_name = mkt_def.get("name", "")
        source_url = mkt_def.get("source", "")
        pin = mkt_def.get("pin", "")
        if not mkt_name:
            continue

        # remove / enabled:false -- deregister the marketplace (and its plugins)
        # via `claude plugin marketplace remove`. Takes precedence over every
        # other field (pin/source/alwaysUpdate are meaningless for a marketplace
        # we are tearing down). Idempotent: an already-absent marketplace is a
        # quiet ok, so the directive can sit in a checked-in layer forever
        # without erroring once the removal has happened.
        if mkt_def.get("remove") or mkt_def.get("enabled") is False:
            if not check_marketplace_exists(mkt_name).passed:
                ctx.ok(f"marketplace {mkt_name}: already removed")
                continue
            rm_result = remove_marketplace(mkt_name)
            if rm_result.passed:
                ctx.action(f"marketplace {mkt_name}: removed")
            else:
                ctx.fail(
                    f"marketplace {mkt_name}: remove failed - {rm_result.message}",
                    type="marketplace", name=mkt_name, message=rm_result.message,
                )
            continue

        if pin:
            # Record pin intent even if applying fails below — a later
            # manifest's unpinned entry must never unpin a declared pin.
            _pinned_marketplaces_this_run.add(mkt_name)

            mkt_result = check_marketplace_exists(mkt_name)
            if not mkt_result.passed:
                if not source_url:
                    ctx.fail(
                        f"marketplace {mkt_name}: pin '{pin}' declared but the marketplace is not registered and no source is declared to add it",
                        display=f"marketplace {mkt_name}: pin unresolvable",
                        type="marketplace", name=mkt_name,
                        message="pin declared for an unregistered marketplace with no source",
                    )
                    continue
                add_result = add_marketplace(source_url, mkt_name)
                if add_result.passed:
                    ctx.action(f"marketplace {mkt_name}: added ({source_url})")
                else:
                    ctx.fail(
                        f"marketplace {mkt_name}: add failed - {add_result.message}",
                        type="marketplace", name=mkt_name, message=add_result.message,
                    )
                    continue

            if mkt_def.get("alwaysUpdate"):
                # pin takes precedence: skip the stale-check/update path.
                ctx.action(
                    f"marketplace {mkt_name}: alwaysUpdate ignored while pinned",
                    display=f"marketplace {mkt_name}: pinned",
                )

            pin_result = apply_marketplace_pin(mkt_name, pin)
            if pin_result.passed:
                if pin_result.status == "pinned":
                    ctx.action(f"marketplace {mkt_name}: pinned at {pin_result.sha[:8]}")
                else:
                    ctx.ok(f"marketplace {mkt_name}: pinned at {pin_result.sha[:8]}")
            else:
                ctx.fail(
                    f"marketplace {mkt_name}: pin failed - {pin_result.message}",
                    type="marketplace", name=mkt_name, message=pin_result.message,
                )
            continue

        if mkt_name in _pinned_marketplaces_this_run:
            # A pin for this marketplace was declared by an earlier manifest
            # this run; this unpinned entry must not unpin it or update past it.
            ctx.ok(f"marketplace {mkt_name}: pinned earlier this run (skipping update checks)")
            continue

        if mkt_name in load_pin_markers():
            # Pin was removed from the manifest but the marker remains: release
            # it (restore default branch + recorded autoUpdate), then run the
            # normal update path so the clone catches up with its remote.
            rel = release_marketplace_pin(mkt_name)
            if rel.passed:
                upd = update_marketplace(mkt_name)
                if upd.passed:
                    ctx.action(f"marketplace {mkt_name}: unpinned, {rel.message} + updated")
                else:
                    ctx.fail(
                        f"marketplace {mkt_name}: unpinned, {rel.message}; update failed - {upd.message}",
                        display=f"marketplace {mkt_name}: update failed",
                        type="marketplace", name=mkt_name, message=upd.message,
                    )
            else:
                ctx.fail(
                    f"marketplace {mkt_name}: unpin failed - {rel.message}",
                    type="marketplace", name=mkt_name, message=rel.message,
                )
            continue

        if not source_url:
            continue

        mkt_result = check_marketplace_exists(mkt_name)
        if mkt_result.passed:
            # Check if alwaysUpdate is set — if so, check for updates
            if mkt_def.get("alwaysUpdate"):
                current_result = check_marketplace_current(mkt_name)
                if current_result.passed:
                    ctx.ok(f"marketplace {mkt_name}: up to date")
                else:
                    upd_result = update_marketplace(mkt_name)
                    if upd_result.passed:
                        # Marketplace refresh is the *mechanism* by which plugin
                        # updates happen; the plugin updates themselves are the
                        # user-visible outcome. Demote to verbose-only.
                        ctx.ok(f"marketplace {mkt_name}: updated (alwaysUpdate)")
                    else:
                        ctx.fail(
                            f"marketplace {mkt_name}: update failed - {upd_result.message}",
                            type="marketplace", name=mkt_name, message=upd_result.message,
                        )
            else:
                ctx.ok(f"marketplace {mkt_name}: ok")
        else:
            # Auto-add marketplace via CLI
            add_result = add_marketplace(source_url, mkt_name)
            if add_result.passed:
                ctx.action(f"marketplace {mkt_name}: added ({source_url})")
            else:
                ctx.fail(
                    f"marketplace {mkt_name}: add failed - {add_result.message}",
                    type="marketplace", name=mkt_name, message=add_result.message,
                )


def _phase_plugins(ctx):
    """plugins: install / scope / enable / update declared plugin refs.

    Successful actions accumulate per (marketplace, verb) and are emitted as
    consolidated lines after the loop: "<mkt>: updated <name> [old -> new], ...".
    Failures and one-off warnings stay per-line to preserve detail.
    """
    from .marketplace_lifecycle import (
        check_plugin_installed, install_plugin,
        enable_plugin_in_claude, disable_plugin_in_claude,
        check_plugin_enabled, check_plugin_enabled_at_scope,
        enable_plugin_at_scope,
        check_plugin_version, check_plugin_min_version,
        update_plugin, ensure_registry_scope,
        pinned_marketplace_sha,
    )

    plugins_installed = {}      # mkt -> [(name, detail)]
    plugins_re_installed = {}   # mkt -> [(name, detail)]
    plugins_updated = {}        # mkt -> [(name, detail)]
    plugins_enabled = {}        # mkt -> [(name, detail)]
    plugins_disabled = {}       # mkt -> [(name, detail)]

    def _bucket(d, plugin_ref, detail):
        mkt, name = (plugin_ref.split(":", 1) if ":" in plugin_ref else ("", plugin_ref))
        d.setdefault(mkt, []).append((name, detail))

    for plugin_def in ctx.manifest.get("plugins", []):
        plugin_ref = plugin_def.get("ref", "")
        enabled = plugin_def.get("enabled", True)
        desired_scope = plugin_def.get("scope", "user")
        min_version = plugin_def.get("min_version", "")
        install_mode = plugin_def.get("install", "auto")
        if not plugin_ref:
            continue
        if install_mode not in ("auto", "manual"):
            ctx.action(f"plugin {plugin_ref}: unknown install mode '{install_mode}' (expected 'auto' or 'manual'); treating as 'auto'")
            install_mode = "auto"

        # Compute CLI ref for logging (marketplace:plugin -> plugin@marketplace)
        cli_ref = f"{plugin_ref.split(':', 1)[1]}@{plugin_ref.split(':', 1)[0]}" if ":" in plugin_ref else plugin_ref

        # Check if plugin is installed (global registry, handles both ref formats)
        install_result = check_plugin_installed(plugin_ref)
        if not install_result.passed:
            if install_mode == "manual":
                # User is expected to install via `claude plugin install ...`;
                # we only manage updates once they do. Don't surface a failure.
                ctx.ok(f"plugin {plugin_ref}: not installed (install: manual; run `claude plugin install {cli_ref}` to enable)")
                continue
            # Auto-install via CLI
            inst = install_plugin(plugin_ref, scope=desired_scope, project_dir=ctx.project_dir)
            if inst.passed:
                _bucket(plugins_installed, plugin_ref, f"at {desired_scope} scope")
            else:
                ctx.fail(
                    f"plugin {plugin_ref}: install failed - {inst.message}",
                    type="plugin", ref=plugin_ref, message=inst.message,
                )
                continue

        # Ensure plugin is enabled at desired scope (reads settings file directly,
        # not installed_plugins.json which can have stale scope metadata). Skip
        # for install: manual -- the user owns scope and enable state; we just
        # manage version updates.
        if install_result.passed and install_mode != "manual":
            scope_check = check_plugin_enabled_at_scope(plugin_ref, desired_scope, ctx.project_dir)
            if not scope_check.passed:
                # Keep the scope-mismatch note as its own line so the user sees
                # *why* the enable happened; consolidate the action.
                ctx.action(f"plugin {plugin_ref}: {scope_check.message}")
                # The plugin IS installed -- only this scope's enabledPlugins
                # entry is missing -- so write that entry directly instead of
                # re-running `claude plugin install --scope`. The CLI is the
                # wrong tool twice over here: it short-circuits with "already
                # installed" (writing nothing, so the check fails again next
                # session, forever, while reporting success), and when it does
                # write it reserialises the whole settings file, reordering
                # keys in what is frequently a shared, source-controlled file.
                enabled = enable_plugin_at_scope(plugin_ref, desired_scope, ctx.project_dir)
                if enabled.passed:
                    _bucket(plugins_enabled, plugin_ref, f"at {desired_scope} scope")
                else:
                    ctx.fail(
                        f"plugin {plugin_ref}: could not enable at {desired_scope} scope - {enabled.message}",
                        display=f"plugin {plugin_ref}: enable failed",
                        type="plugin", ref=plugin_ref,
                        message=f"enable at {desired_scope} scope failed: {enabled.message}",
                    )
                    continue

            # Sync installed_plugins.json scope to match desired scope.
            # CLI commands (update, uninstall) read scope from this file and
            # fail if it's stale. Fix the data before running those commands.
            # An add is a remediation, a refusal is a defect left unrepaired,
            # and an unreadable registry is a failed check -- all three are
            # always-visible action entries, never verbose-only ok entries.
            _scope_sync = ensure_registry_scope(plugin_ref, desired_scope)
            if _scope_sync.added or _scope_sync.refused or not _scope_sync.passed:
                ctx.action(f"plugin {plugin_ref}: {_scope_sync.message}")

        if install_mode == "manual":
            # Manual-install plugins: only manage version updates. Skip
            # enable/disable side effects so the user's choices are respected.
            if install_result.passed:
                ver_result = check_plugin_version(plugin_ref)
                if not ver_result.up_to_date:
                    upd_result = update_plugin(plugin_ref, scope=desired_scope, project_dir=ctx.project_dir)
                    if upd_result.passed:
                        _bucket(plugins_updated, plugin_ref, f"{ver_result.installed_version} -> {ver_result.latest_version}, manual")
                    else:
                        ctx.action(f"plugin {plugin_ref}: update failed ({ver_result.message}) - {upd_result.message}")
                else:
                    ctx.ok(f"plugin {plugin_ref}: up to date (install: manual)")
        elif enabled:
            mkt_for_ref = plugin_ref.split(":", 1)[0] if ":" in plugin_ref else ""

            # Check if version is up to date (only for already-installed plugins)
            if install_result.passed:
                ver_result = check_plugin_version(plugin_ref)
                if not ver_result.up_to_date:
                    upd_result = update_plugin(plugin_ref, scope=desired_scope, project_dir=ctx.project_dir)
                    if upd_result.passed:
                        _bucket(plugins_updated, plugin_ref, f"{ver_result.installed_version} -> {ver_result.latest_version}")
                    else:
                        ctx.action(f"plugin {plugin_ref}: update failed ({ver_result.message}) - {upd_result.message}")
                else:
                    # up_to_date with a known, *different* marketplace version
                    # means installed is AHEAD (the check never downgrades).
                    # When the marketplace is pinned, surface that as a notice
                    # — expected after pinning to an older snapshot. getattr:
                    # some tests stub check_plugin_version with up_to_date only.
                    _installed = getattr(ver_result, "installed_version", "")
                    _latest = getattr(ver_result, "latest_version", "")
                    if _installed and _latest and _installed != _latest:
                        pin_sha = pinned_marketplace_sha(mkt_for_ref) if mkt_for_ref else ""
                        if pin_sha:
                            ctx.ok(
                                f"plugin {plugin_ref}: installed {_installed} is ahead of the "
                                f"pinned marketplace latest {_latest} (pinned at {pin_sha}); not downgrading"
                            )

            # Check min_version constraint (auto-update, then fail if still unsatisfied)
            if install_result.passed and min_version:
                min_result = check_plugin_min_version(plugin_ref, min_version)
                if not min_result.up_to_date:
                    # While the marketplace is pinned, its marketplace.json caps
                    # available versions at the snapshot — an unsatisfied
                    # min_version cannot be fixed by updating.
                    pin_sha = pinned_marketplace_sha(mkt_for_ref) if mkt_for_ref else ""
                    pin_note = (
                        f"; marketplace {mkt_for_ref} is pinned at {pin_sha}, so the constraint "
                        "cannot be satisfied while pinned - drop the pin to update"
                    ) if pin_sha else ""
                    upd_result = update_plugin(plugin_ref, scope=desired_scope, project_dir=ctx.project_dir)
                    if upd_result.passed:
                        recheck = check_plugin_min_version(plugin_ref, min_version)
                        if recheck.up_to_date:
                            _bucket(plugins_updated, plugin_ref, f"{min_result.installed_version} -> {recheck.installed_version}, satisfies >= {min_version}")
                        else:
                            ctx.fail(
                                f"plugin {plugin_ref}: installed {recheck.installed_version} < required {min_version}, update failed to satisfy constraint{pin_note}",
                                display=f"plugin {plugin_ref}: min_version unmet",
                                type="plugin", ref=plugin_ref,
                                message=f"min_version {min_version} not satisfied (installed {recheck.installed_version}){pin_note}",
                            )
                    else:
                        ctx.fail(
                            f"plugin {plugin_ref}: installed {min_result.installed_version} < required {min_version}, update failed - {upd_result.message}{pin_note}",
                            display=f"plugin {plugin_ref}: min_version unmet",
                            type="plugin", ref=plugin_ref,
                            message=f"min_version {min_version} not satisfied: {upd_result.message}{pin_note}",
                        )

            # Check enabled state at desired scope
            enabled_result = check_plugin_enabled_at_scope(plugin_ref, desired_scope, ctx.project_dir)
            if enabled_result.passed:
                ctx.ok(f"plugin {plugin_ref}: ok")
            else:
                en_result = enable_plugin_in_claude(plugin_ref)
                if en_result.passed:
                    _bucket(plugins_enabled, plugin_ref, f"at {desired_scope} scope")
                else:
                    ctx.fail(
                        f"plugin {plugin_ref}: enable failed - {en_result.message}",
                        type="plugin", ref=plugin_ref, message=en_result.message,
                    )
        else:
            # Only disable if currently enabled (check before acting)
            enabled_result = check_plugin_enabled(plugin_ref)
            if not enabled_result.passed:
                ctx.ok(f"plugin {plugin_ref}: already disabled")
            else:
                dis_result = disable_plugin_in_claude(plugin_ref)
                if dis_result.passed:
                    _bucket(plugins_disabled, plugin_ref, "")
                else:
                    ctx.fail(
                        f"plugin {plugin_ref}: disable failed - {dis_result.message}",
                        type="plugin", ref=plugin_ref, message=dis_result.message,
                    )

    # Flush plugin-action accumulators as consolidated per-marketplace lines.
    def _emit_plugin_verb(verb, buckets):
        for mkt, items in buckets.items():
            if not items:
                continue
            if mkt:
                ctx.action(f"{mkt}: {verb} {_join_items(items)}")
            else:
                ctx.action(f"{verb}: {_join_items(items)}")
    _emit_plugin_verb("installed", plugins_installed)
    _emit_plugin_verb("re-installed", plugins_re_installed)
    _emit_plugin_verb("updated", plugins_updated)
    _emit_plugin_verb("enabled", plugins_enabled)
    _emit_plugin_verb("disabled", plugins_disabled)


def _phase_ini_settings(ctx):
    """ini_settings: project-scoped — skipped when no project detected."""
    from .ini_check import check_ini_setting, write_ini_setting
    from .var_resolve import resolve_vars

    if not ctx.project_detected:
        ctx.ok("ini_settings: skipped (no project detected)")
        return

    for ini_def in ctx.manifest.get("ini_settings", []):
        ini_file = resolve_vars(ini_def["file"], ctx.variables)
        if ini_file is None:
            ctx.ok(f"ini {ini_def['file']}: skipped (unresolved vars)")
            continue

        section = ini_def["section"]
        # Ensure section has brackets for check/write
        section_header = section if section.startswith("[") else f"[{section}]"

        for key, expected in ini_def.get("settings", {}).items():
            result = check_ini_setting(ini_file, section_header, key, expected)
            if result.passed:
                ctx.ok(f"ini {key}: ok")
            else:
                try:
                    write_ini_setting(ini_file, section_header, key, expected)
                    ctx.action(f"ini {key}: set to {expected}")
                except OSError as e:
                    ctx.fail(
                        f"ini {key}: FAILED - {e}",
                        type="ini", file=ini_file, key=key, message=str(e),
                    )


def _phase_json_entries(ctx):
    """json_entries: merge reference entries into target JSON files.

    Runs after marketplaces — so known_marketplaces.json has valid entries.
    """
    from .json_check import check_json_entries, merge_json_entries
    from .var_resolve import resolve_vars

    for json_def in ctx.manifest.get("json_entries", []):
        ref_path = resolve_vars(json_def.get("reference", ""), ctx.variables)
        target_path = resolve_vars(json_def.get("target", ""), ctx.variables)
        if ref_path is None or target_path is None:
            ctx.ok("json: skipped (unresolved vars)")
            continue

        # Resolve reference relative to plugin root if not absolute
        if not os.path.isabs(ref_path):
            ref_path = os.path.join(ctx.plugin_root, ref_path)
        # Expand ~ in target path
        target_path = os.path.expanduser(target_path)

        merge_fields = json_def.get("merge_fields", [])
        preserve_fields = json_def.get("preserve_fields", [])

        result = check_json_entries(ref_path, target_path, merge_fields, preserve_fields)
        if result.passed:
            ctx.ok(f"json {os.path.basename(target_path)}: ok")
        else:
            result = merge_json_entries(ref_path, target_path, merge_fields, preserve_fields)
            if result.passed:
                ctx.action(f"json {os.path.basename(target_path)}: merged")
            else:
                ctx.fail(
                    f"json {os.path.basename(target_path)}: FAILED - {result.message}",
                    type="json", target=target_path, message=result.message,
                )


def _phase_pypi_packages(ctx):
    """pypi_packages: download + extract (consolidate installs; failures per-line)."""
    from .pypi_check import check_pypi_package, download_and_extract
    from .var_resolve import resolve_vars

    pypi_installed = []
    for pypi_def in ctx.manifest.get("pypi_packages", []):
        extract_to = resolve_vars(pypi_def["extract_to"], ctx.variables)
        if extract_to is None:
            ctx.ok(f"pypi {pypi_def['package']}: skipped (unresolved vars)")
            continue

        result = check_pypi_package(pypi_def["package"], extract_to)
        if result.passed:
            ctx.ok(f"pypi {result.package}: ok")
        else:
            extract_pattern = pypi_def.get("extract_pattern")
            result = download_and_extract(pypi_def["package"], extract_to, extract_pattern)
            if result.passed:
                pypi_installed.append((result.package, result.message))
            else:
                ctx.fail(
                    f"pypi {result.package}: FAILED - {result.message}",
                    type="pypi", package=pypi_def["package"], message=result.message,
                )

    if pypi_installed:
        ctx.action(f"pypi: {_join_items(pypi_installed)}")


def _phase_shared_libs(ctx):
    """shared_libs / shared_lib_imports: owner publish + consumer link.

    Rule: cached/skipped -> ok_entries (verbose-only); published/linked ->
    quiet_entries (logged with the .pth path, but NOT displayed per-plugin --
    Step 4c renders one aggregated line for the whole pass, see
    _SharedLibLinkLog); failed -> action_entries + failures. Runs after the venv
    handler so a consumer's own .venv already exists as the .pth target.
    """
    from .shared_lib import sync_shared_lib, link_shared_lib, find_standalone_python
    from .venv_check import _find_python

    shared_root = os.path.join(os.path.dirname(ctx.data_dir), "_shared_libs")

    def _log_shared(result):
        if result.status in ("cached", "skipped"):
            ctx.ok(f"shared-lib {result.name}: {result.message}")
        elif result.status in ("published", "linked"):
            ctx.quiet(f"shared-lib {result.name}: {result.message}")
            if ctx.shared_lib_links is not None:
                ctx.shared_lib_links.record(result.status, result.name, ctx.plugin_name)
        else:  # failed
            ctx.fail(
                f"shared-lib {result.name}: FAILED - {result.message}",
                type="shared_lib", name=result.name, message=result.message,
            )

    # Owner phase: publish source, then broadcast to the standalone Python.
    for lib_def in ctx.manifest.get("shared_libs", []):
        lib_name = lib_def.get("name", "")
        lib_src = lib_def.get("src", ".")
        if not lib_name:
            continue
        sync_result = sync_shared_lib(lib_name, lib_src, ctx.plugin_root, shared_root)
        _log_shared(sync_result)
        if sync_result.status != "failed":
            _log_shared(link_shared_lib(lib_name, find_standalone_python(), shared_root))

    # Consumer phase: link into this plugin's own venv.
    shared_lib_imports = ctx.manifest.get("shared_lib_imports", [])
    if shared_lib_imports:
        venv_python = _find_python(os.path.join(ctx.data_dir, ".venv"))
        for lib_name in shared_lib_imports:
            _log_shared(link_shared_lib(lib_name, venv_python, shared_root))


def _phase_script(ctx):
    """script: run the plugin's custom bootstrap module in-process."""
    script_failures = _run_script_phase(
        ctx.manifest["script"], ctx.plugin_root, ctx.data_dir, ctx.config,
        ctx.action_entries, ctx.ok_entries,
        prefix=ctx.prefix, plugin_name=ctx.plugin_name, project_dir=ctx.project_dir,
    )
    ctx.failures.extend(script_failures)


# The manifest phase table (B12): each phase runs when any of its manifest
# keys is present (truthy). ORDER IS LOAD-BEARING:
#   - env_vars first: the variables are live-exported into the engine
#     process, so install commands in every later phase of the same pass
#     can reference them (e.g. $DEVROOT/...).
#   - tools before venv: uv may be installed by the tools phase.
#   - venv before shared_libs: a consumer link targets this plugin's .venv.
#   - marketplaces before json_entries: the marketplace must be cloned before
#     fields like autoUpdate are merged into known_marketplaces.json.
#   - plugins before ini/json: installs rewrite the plugin registry first.
#   - script last: it may build on everything the manifest set up.
_MANIFEST_PHASES = (
    (("env_vars",), _phase_env_vars),
    (("tools",), _phase_tools),
    (("fonts",), _phase_fonts),
    (("path_entries",), _phase_path_entries),
    (("venv",), _phase_venv),
    (("git_deps",), _phase_git_deps),
    (("sync_to_data",), _phase_sync_to_data),
    (("marketplaces",), _phase_marketplaces),
    (("plugins",), _phase_plugins),
    (("ini_settings",), _phase_ini_settings),
    (("json_entries",), _phase_json_entries),
    (("pypi_packages",), _phase_pypi_packages),
    (("shared_libs", "shared_lib_imports"), _phase_shared_libs),
    (("script",), _phase_script),
)


def _process_manifest(manifest, current_os, data_dir, plugin_root, action_entries, ok_entries, plugin_name="bootstrap", project_dir=None, project_detected=True, quiet_entries=None, shared_lib_links=None):
    """Process a single plugin's bootstrap manifest. Returns list of failures.

    Dispatches to one handler per manifest key via _MANIFEST_PHASES. Entries
    are split into three lists:
    - action_entries: actions performed, failures, conditions not met (always displayed)
    - ok_entries: checks that passed (never displayed; written to log file when log_success is true)
    - quiet_entries: remediations reported in aggregate elsewhere (always logged,
      never displayed). Optional; when omitted the entries are dropped.

    `shared_lib_links` is the pass-level _SharedLibLinkLog that collects
    shared-lib publish/link successes for Step 4c's single aggregated line.

    When project_detected is False, project-scoped primitives (ini_settings) are skipped.
    """
    ctx = _ManifestContext(
        manifest, current_os, data_dir, plugin_root,
        action_entries, ok_entries, plugin_name, project_dir, project_detected,
        quiet_entries=quiet_entries, shared_lib_links=shared_lib_links,
    )
    for keys, handler in _MANIFEST_PHASES:
        if any(manifest.get(k) for k in keys):
            handler(ctx)
    return ctx.failures


class _EnvManifestContext(_ManifestContext):
    """_ManifestContext plus machine identity, for the env.json phase.

    env.json entries may be keyed by hostname as well as OS (spec 4.2):
    ``machine_key`` is the machines-registry key the current hostname
    resolved to (exact match, else the domain-stripped short form),
    ``machine`` that key's registry entry, ``machines`` the whole registry.
    ``machine_key``/``machine`` are set by _validate_env_machines once the
    registry validates; feature handlers (the _ENV_PHASES table) only run
    after that. ``entry_applies`` applies the generic per-entry
    ``os``/``hosts`` filters (intersection semantics).
    """

    def __init__(self, manifest, current_os, data_dir, plugin_root,
                 action_entries, ok_entries, project_dir,
                 hostname, machines):
        super().__init__(
            manifest, current_os, data_dir, plugin_root,
            action_entries, ok_entries, "env", project_dir, True,
        )
        self.hostname = hostname
        self.machines = machines
        self.machine_key = None
        self.machine = None

    def entry_applies(self, entry):
        from .env_manifest import entry_applies
        return entry_applies(entry, self.current_os, self.machine_key)


def _env_section_entries(ctx, section, failure_type):
    """The section's entry list, or None after one descriptive failure.

    Every env.json feature section is an array of entry objects; anything
    else is a manifest error surfaced per-section (not a crash, not a
    guess). ``failure_type`` is the section's per-entry failure type
    (``env_symlink``, ``env_check``, ...) -- section-shape errors carry the
    same type as the entries they gate, one name per section.
    """
    value = ctx.manifest.get(section)
    if isinstance(value, list):
        return value
    ctx.fail(
        f"{section}: INVALID section - expected an array of entries, got {type(value).__name__}",
        type=failure_type,
        name=section,
        message=f"env.json '{section}' must be an array of entries, got {type(value).__name__}",
        persist_across_sessions=True,
    )
    return None


def _env_phase_symlinks(ctx):
    """symlinks: ensure target is a symlink pointing at source (spec 4.3).

    env-config ConfigLinkManager semantics translated to the engine's
    check -> fix -> authoritative re-check idiom: a real file at target is
    preserved as a timestamped backup when the entry sets backup: true; a
    directory at target is never replaced; a missing source is a
    descriptive failure (personalization refuses to guess).
    """
    from .env_features import check_symlink, expand_env_path, fix_symlink

    entries = _env_section_entries(ctx, "symlinks", "env_symlink")
    if entries is None:
        return
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        source = entry.get("source") if isinstance(entry, dict) else None
        target = entry.get("target") if isinstance(entry, dict) else None
        if not (isinstance(name, str) and name and isinstance(source, str)
                and source and isinstance(target, str) and target):
            ctx.fail(
                f"symlink: INVALID entry {entry!r} - needs string 'name', 'source' and 'target'",
                type="env_symlink",
                name=_entry_label(name),
                message=f"invalid symlinks entry {entry!r}: needs string 'name', 'source' and 'target'",
                persist_across_sessions=True,
            )
            continue
        if not ctx.entry_applies(entry):
            ctx.ok(f"symlink {name}: skipped (os/hosts filter)")
            continue
        try:
            src = expand_env_path(source)
            tgt = expand_env_path(target)
        except ValueError as e:
            ctx.fail(
                f"symlink {name}: FAILED - {e}",
                type="env_symlink", name=name, message=f"{name}: {e}",
                persist_across_sessions=True,
            )
            continue

        result = check_symlink(src, tgt)
        if result.passed:
            ctx.ok(f"symlink {name}: ok - {result.message}")
            continue
        fix = fix_symlink(src, tgt, backup=bool(entry.get("backup", False)))
        if fix.needs_elevation:
            # WinError 1314: unelevated symlink creation on Windows needs
            # Developer Mode or admin rights. Defer into the elevation queue
            # via the standard {method: "command"} descriptor (the same
            # route as an elevated env_check fix): the remediation .bat runs
            # the queue through Git Bash elevated, where
            # MSYS=winsymlinks:nativestrict makes `ln -s` create a REAL
            # Windows symlink (default MSYS ln copies instead). -sfn replaces
            # a stale/dangling link left by an earlier attempt. Any backup of
            # a pre-existing regular file already happened inside fix_symlink
            # before os.symlink raised.
            manual_cmd = f"MSYS=winsymlinks:nativestrict ln -sfn '{src}' '{tgt}'"
            ctx.fail(
                f"symlink {name}: needs elevation - deferred; creating "
                f"{tgt} -> {src} requires Developer Mode or admin rights "
                f"on Windows",
                type="env_symlink", name=name,
                message=(
                    f"{name}: creating symlink {tgt} -> {src} requires "
                    f"elevation (WinError 1314)"
                ),
                elevation={
                    "method": "command", "command": manual_cmd,
                    "os": ctx.current_os, "id": f"symlink:{name}",
                    # The label stands alone in the runner's plan and in the
                    # session message's item list, so it names the entry rather
                    # than restating the WinError. `description` is taken only
                    # when it is short enough to collate; the name-derived
                    # fallback is guaranteed to be (see messages.item_label).
                    "label": _item_label(
                        entry.get("label"), entry.get("description"),
                        f"Link {name}",
                    ),
                },
                agent_msg=(
                    f"The symlink '{name}' ({tgt} -> {src}) needs elevation on "
                    f"Windows (WinError 1314); bootstrap deferred it into the "
                    f"fix queue. Enabling Windows Developer Mode (Settings > "
                    f"System > For developers) would let bootstrap create it "
                    f"unelevated instead."
                ),
                persist_across_sessions=True,
            )
            continue
        recheck = check_symlink(src, tgt)
        if fix.ok and recheck.passed:
            ctx.action(f"symlink {name}: {fix.message}")
        else:
            detail = fix.message if not fix.ok else (
                f"fix reported '{fix.message}' but re-check failed: {recheck.message}"
            )
            ctx.fail(
                f"symlink {name}: FAILED - {detail}",
                type="env_symlink", name=name, message=f"{name}: {detail}",
                persist_across_sessions=True,
            )


def _env_phase_shell_rc(ctx):
    """shell_rc: rc-file assertions, two modes (spec 3.1 feature 2).

    ensure (`content`): the SHELL_NAME-rendered block is present in every
    existing rc file among ~/.bashrc and ~/.zshrc; a fresh machine gets the
    platform-default rc created. forbid (`forbid`): the regex must not
    match any rc line; the fix comments matching lines out. Exactly one
    mode per entry. Authoring rule (spec directive 3): shell_rc never
    carries PATH lines -- PATH belongs exclusively to bootstrap.
    """
    import re as _re
    from .env_features import (
        check_shell_ensure, check_shell_forbid,
        fix_shell_ensure, fix_shell_forbid,
    )

    entries = _env_section_entries(ctx, "shell_rc", "env_shell_rc")
    if entries is None:
        return
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        content = entry.get("content") if isinstance(entry, dict) else None
        forbid = entry.get("forbid") if isinstance(entry, dict) else None
        has_content = isinstance(content, str) and bool(content.strip())
        has_forbid = isinstance(forbid, str) and bool(forbid)
        if not (isinstance(name, str) and name) or has_content == has_forbid:
            ctx.fail(
                f"shell_rc: INVALID entry {entry!r} - needs string 'name' and exactly one of 'content'/'forbid'",
                type="env_shell_rc",
                name=_entry_label(name),
                message=f"invalid shell_rc entry {entry!r}: needs string 'name' and exactly one of 'content' (ensure) or 'forbid' (pattern)",
                persist_across_sessions=True,
            )
            continue
        if not ctx.entry_applies(entry):
            ctx.ok(f"shell_rc {name}: skipped (os/hosts filter)")
            continue

        if has_forbid:
            try:
                _re.compile(forbid)
            except _re.error as e:
                ctx.fail(
                    f"shell_rc {name}: INVALID forbid pattern - {e}",
                    type="env_shell_rc", name=name,
                    message=f"{name}: invalid forbid regex {forbid!r}: {e}",
                    persist_across_sessions=True,
                )
                continue
            result = check_shell_forbid(name, forbid)
            if result.passed:
                ctx.ok(f"shell_rc {name}: ok - {result.message}")
                continue
            fix_ok, msg = fix_shell_forbid(forbid)
            recheck = check_shell_forbid(name, forbid)
        else:
            result = check_shell_ensure(name, content)
            if result.passed:
                ctx.ok(f"shell_rc {name}: ok - {result.message}")
                continue
            fix_ok, msg = fix_shell_ensure(content, ctx.current_os)
            recheck = check_shell_ensure(name, content)

        if fix_ok and recheck.passed:
            ctx.action(f"shell_rc {name}: {msg}")
        else:
            detail = msg if not fix_ok else (
                f"fix reported '{msg}' but re-check failed: {recheck.message}"
            )
            ctx.fail(
                f"shell_rc {name}: FAILED - {detail}",
                type="env_shell_rc", name=name, message=f"{name}: {detail}",
                persist_across_sessions=True,
            )


def _env_phase_macos_defaults(ctx):
    """macos_defaults: `defaults read`/`write` assertions (spec 3.1 feature 3).

    macOS-only: on any other OS the whole section no-ops with a verbose
    skip line (entries may also carry os filters, but the mechanism itself
    only exists on macOS). After any successful fix the standard
    preference-cache flush runs once for the pass.
    """
    if ctx.current_os != "macos":
        ctx.ok("macos_defaults: skipped (not macOS)")
        return
    from .env_features import (
        check_macos_default, defaults_expected_string,
        fix_macos_default, flush_macos_defaults_cache,
    )

    entries = _env_section_entries(ctx, "macos_defaults", "env_macos_default")
    if entries is None:
        return
    fixed_any = False
    for entry in entries:
        domain = entry.get("domain") if isinstance(entry, dict) else None
        key = entry.get("key") if isinstance(entry, dict) else None
        value = entry.get("value") if isinstance(entry, dict) else None
        label = (f"{domain}.{key}"
                 if isinstance(domain, str) and isinstance(key, str)
                 else "(unnamed)")
        if not (isinstance(domain, str) and domain and isinstance(key, str)
                and key) or defaults_expected_string(value) is None:
            ctx.fail(
                f"macos_default: INVALID entry {entry!r} - needs string 'domain'/'key' and bool/int/string 'value'",
                type="env_macos_default",
                name=label,
                message=f"invalid macos_defaults entry {entry!r}: needs string 'domain'/'key' and bool/int/string 'value'",
                persist_across_sessions=True,
            )
            continue
        if not ctx.entry_applies(entry):
            ctx.ok(f"macos_default {label}: skipped (os/hosts filter)")
            continue

        result = check_macos_default(domain, key, value)
        if result.passed:
            ctx.ok(f"macos_default {label}: ok - {result.message}")
            continue
        fix_ok, msg = fix_macos_default(domain, key, value)
        recheck = check_macos_default(domain, key, value)
        if fix_ok and recheck.passed:
            fixed_any = True
            ctx.action(f"macos_default {label}: {msg}")
        else:
            detail = msg if not fix_ok else (
                f"fix reported '{msg}' but re-check failed: {recheck.message}"
            )
            ctx.fail(
                f"macos_default {label}: FAILED - {detail}",
                type="env_macos_default", name=label,
                message=f"{label}: {detail}",
                persist_across_sessions=True,
            )
    if fixed_any:
        flush_macos_defaults_cache()
        ctx.ok("macos_defaults: preference cache flushed")


def _env_phase_macos_hotkeys(ctx):
    """macos_hotkeys: symbolic-hotkey remaps (spec 3.1 feature 4). macOS-only.

    Check via one side-effect-free plist export compare; fix via ONE
    export -> mutate -> import round-trip for the whole failing batch plus
    the cache flush / process restarts (env-config
    apply_macos_keyboard_shortcuts), then a fresh export re-checks each
    fixed entry (the re-check is authoritative). An id absent from the
    plist is a descriptive failure -- the fix only mutates existing hotkey
    slots, it never fabricates one.
    """
    if ctx.current_os != "macos":
        ctx.ok("macos_hotkeys: skipped (not macOS)")
        return
    from .env_features import (
        apply_symbolic_hotkeys, hotkey_state, read_symbolic_hotkeys,
    )

    entries = _env_section_entries(ctx, "macos_hotkeys", "env_macos_hotkey")
    if entries is None:
        return
    applicable = []
    for entry in entries:
        hid = entry.get("id") if isinstance(entry, dict) else None
        params = entry.get("parameters") if isinstance(entry, dict) else None
        valid = (
            isinstance(hid, int) and not isinstance(hid, bool)
            and isinstance(params, list) and params
            and all(isinstance(p, int) and not isinstance(p, bool) for p in params)
        )
        if not valid:
            ctx.fail(
                f"macos_hotkey: INVALID entry {entry!r} - needs int 'id' and int-list 'parameters'",
                type="env_macos_hotkey",
                name=_entry_label(hid),
                message=f"invalid macos_hotkeys entry {entry!r}: needs int 'id' and a non-empty int list 'parameters'",
                persist_across_sessions=True,
            )
            continue
        if not ctx.entry_applies(entry):
            ctx.ok(f"macos_hotkey {hid}: skipped (os/hosts filter)")
            continue
        applicable.append(entry)
    if not applicable:
        return

    data, err = read_symbolic_hotkeys()
    if data is None:
        ctx.fail(
            f"macos_hotkeys: FAILED - {err}",
            type="env_macos_hotkey", name="macos_hotkeys", message=err,
            persist_across_sessions=True,
        )
        return

    def _label(entry):
        return entry.get("description") or f"id {entry['id']}"

    failing = []
    for entry in applicable:
        status, detail = hotkey_state(
            data, entry["id"], entry["parameters"], entry.get("enabled", True))
        if status == "ok":
            ctx.ok(f"macos_hotkey {entry['id']}: ok - {_label(entry)}")
        elif status == "missing":
            ctx.fail(
                f"macos_hotkey {entry['id']}: FAILED - {detail}",
                type="env_macos_hotkey", name=str(entry["id"]),
                message=f"{_label(entry)}: {detail} (the fix only remaps existing hotkeys)",
                persist_across_sessions=True,
            )
        else:
            failing.append(entry)
    if not failing:
        return

    fix_ok, msg = apply_symbolic_hotkeys(data, failing)
    redata, rerr = read_symbolic_hotkeys()
    for entry in failing:
        if redata is None:
            re_ok, detail = False, rerr
        else:
            status, detail = hotkey_state(
                redata, entry["id"], entry["parameters"],
                entry.get("enabled", True))
            re_ok = status == "ok"
        if fix_ok and re_ok:
            ctx.action(f"macos_hotkey {entry['id']}: applied - {_label(entry)}")
        else:
            detail2 = msg if not fix_ok else (
                f"fix reported '{msg}' but re-check failed: {detail}"
            )
            ctx.fail(
                f"macos_hotkey {entry['id']}: FAILED - {detail2}",
                type="env_macos_hotkey", name=str(entry["id"]),
                message=f"{_label(entry)}: {detail2}",
                persist_across_sessions=True,
            )


def _env_phase_login_items(ctx):
    """login_items: macOS login-item autostart via System Events (spec 3.1).

    macOS-only. The declared app must exist on disk: a missing app is a
    persistent failure (NOT env-config's warning-skip -- under the env
    gate a silent skip would stamp the pass clean and never converge once
    the app appears; a failure keeps the phase re-running until green,
    which is the gate's convergence loop working as designed).
    """
    if ctx.current_os != "macos":
        ctx.ok("login_items: skipped (not macOS)")
        return
    from .env_features import (
        add_login_item, check_login_item, expand_env_path,
    )

    entries = _env_section_entries(ctx, "login_items", "env_login_item")
    if entries is None:
        return
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        path = entry.get("path") if isinstance(entry, dict) else None
        if not (isinstance(name, str) and name and isinstance(path, str) and path):
            ctx.fail(
                f"login_item: INVALID entry {entry!r} - needs string 'name' and 'path'",
                type="env_login_item",
                name=_entry_label(name),
                message=f"invalid login_items entry {entry!r}: needs string 'name' and 'path'",
                persist_across_sessions=True,
            )
            continue
        if not ctx.entry_applies(entry):
            ctx.ok(f"login_item {name}: skipped (os/hosts filter)")
            continue
        try:
            app_path = expand_env_path(path)
        except ValueError as e:
            ctx.fail(
                f"login_item {name}: FAILED - {e}",
                type="env_login_item", name=name, message=f"{name}: {e}",
                persist_across_sessions=True,
            )
            continue
        if not os.path.exists(app_path):
            ctx.fail(
                f"login_item {name}: FAILED - app not found at {app_path}",
                type="env_login_item", name=name,
                message=(
                    f"{name}: app not found at {app_path}. Install the app "
                    f"(bootstrap tools run earlier in the same pass); the "
                    f"env phase re-runs until this converges."
                ),
                persist_across_sessions=True,
            )
            continue

        result = check_login_item(name)
        if result.passed:
            ctx.ok(f"login_item {name}: ok - {result.message}")
            continue
        fix_ok, msg = add_login_item(app_path, bool(entry.get("hidden", False)))
        recheck = check_login_item(name)
        if fix_ok and recheck.passed:
            ctx.action(f"login_item {name}: {msg}")
        else:
            detail = msg if not fix_ok else (
                f"fix reported '{msg}' but re-check failed: {recheck.message}"
            )
            ctx.fail(
                f"login_item {name}: FAILED - {detail}",
                type="env_login_item", name=name, message=f"{name}: {detail}",
                persist_across_sessions=True,
            )


def _env_phase_env_checks(ctx):
    """env_checks: the generic check/fix contract (spec section 5).

    One mechanism covers every non-declarative item: each entry names a
    `check` command (unprivileged, side-effect free -- the gate is an
    optimization, never a semantic guarantee) and an optional `fix`, both
    opaque shell strings run through the engine's bash shim with a
    per-entry `timeout` (default 600s per command). No engine-side path
    joining -- the deliberate resolution contrast with the plugin-rooted
    `script` phase; contract scripts are invoked via ~-anchored commands.

    Dispatch per applicable entry:

    1. check: exit 0 = configured (verbose ok line, done).
    2. failing + no fix = a persistent manual-attention item (name +
       description + last output line).
    3. `elevated: true` without privileges: NEVER attempted -- deferred
       into the elevation queue via the standard `{method: "command"}`
       descriptor, so the fix lands in the per-OS remediation script the
       pass already writes; the failed stamp keeps the gate open so the
       next session's re-check picks up out-of-band completion.
    4. otherwise run fix (exit code advisory), then RE-RUN check: the
       re-check is authoritative, with NO trust exceptions. Still failing
       = a persistent failure whose message is the fix's last non-empty
       stdout/stderr line (descriptive errors are the script's job).

    A check that cannot complete at all (timeout / no shell) is its own
    persistent failure and the fix is NOT attempted: state is unknown, and
    a check that hangs or cannot run is a contract-script defect to
    surface, not a "not configured" to converge on.
    """
    from .env_features import ENV_CHECK_DEFAULT_TIMEOUT, run_env_command
    from .path_repair import repair_path

    entries = _env_section_entries(ctx, "env_checks", "env_check")
    if entries is None:
        return
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        check = entry.get("check") if isinstance(entry, dict) else None
        fix = entry.get("fix") if isinstance(entry, dict) else None
        if not (isinstance(name, str) and name and isinstance(check, str)
                and check and (fix is None or (isinstance(fix, str) and fix))):
            ctx.fail(
                f"env_check: INVALID entry {entry!r} - needs string 'name' and 'check' (plus optional string 'fix')",
                type="env_check",
                name=_entry_label(name),
                message=f"invalid env_checks entry {entry!r}: needs string 'name' and 'check', plus an optional string 'fix'",
                persist_across_sessions=True,
            )
            continue
        timeout = entry.get("timeout", ENV_CHECK_DEFAULT_TIMEOUT)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            ctx.fail(
                f"env_check {name}: INVALID timeout {timeout!r} - must be a positive integer (seconds)",
                type="env_check", name=name,
                message=f"{name}: invalid timeout {timeout!r}: must be a positive integer number of seconds",
                persist_across_sessions=True,
            )
            continue
        cost = entry.get("cost")
        if cost is not None and cost not in ("quick", "slow"):
            ctx.fail(
                f"env_check {name}: INVALID cost {cost!r} - must be 'quick' or 'slow'",
                type="env_check", name=name,
                message=f"{name}: invalid cost {cost!r}: must be 'quick' or 'slow'",
                persist_across_sessions=True,
            )
            continue
        # Optional agent-facing protocol text. When present, a runtime-state
        # failure of this check surfaces it (combined with the failure detail)
        # as the failure's agent_msg, so the instructions ride in the hook's
        # message TO the agent rather than only the user-facing log. Used to
        # tell Claude how to investigate/offer a fix for a specific check (e.g.
        # repo-sync). Manifest-validation failures above deliberately do NOT
        # carry it -- those are authoring bugs, not runtime state.
        instructions = entry.get("agent_instructions")
        if instructions is not None and not (isinstance(instructions, str) and instructions):
            ctx.fail(
                f"env_check {name}: INVALID agent_instructions - must be a non-empty string",
                display=f"env_check {name}: invalid entry",
                type="env_check", name=name,
                message=f"{name}: invalid agent_instructions: must be a non-empty string",
                persist_across_sessions=True,
            )
            continue

        def _with_instructions(detail):
            """Combine a failure detail line with this entry's agent_instructions
            (None when the entry declares none, so callers pass agent_msg=None and
            emit_failure_response's fallback keeps using `message` unchanged)."""
            return f"{detail}\n{instructions}" if instructions else None

        if not ctx.entry_applies(entry):
            ctx.ok(f"env_check {name}: skipped (os/hosts filter)")
            continue

        rc, detail = run_env_command(check, timeout)
        if rc == 0:
            ctx.ok(f"env_check {name}: ok")
            continue
        if rc is None:
            ctx.fail(
                f"env_check {name}: CHECK could not run - {detail}",
                type="env_check", name=name,
                message=(
                    f"{name}: check could not run ({detail}). Checks must "
                    f"be cheap and side-effect free; fix the check command "
                    f"or raise the entry's 'timeout'."
                ),
                agent_msg=_with_instructions(
                    f"{name}: check could not run ({detail})."
                ),
                persist_across_sessions=True,
            )
            continue

        if fix is None:
            # Check-only entry: manual-attention item (spec step 2). The
            # description is the entry's user-facing instruction; detail is
            # the check's last output line.
            description = entry.get("description")
            parts = [p for p in (
                description if isinstance(description, str) else None,
                detail,
            ) if p]
            ctx.fail(
                f"env_check {name}: needs manual attention - {'; '.join(parts)}",
                type="env_check", name=name,
                message=f"{name}: {'; '.join(parts)}",
                agent_msg=_with_instructions(f"{name}: {'; '.join(parts)}"),
                persist_across_sessions=True,
            )
            continue

        if bool(entry.get("elevated", False)) and not _privileges_available(ctx.current_os):
            ctx.fail(
                f"env_check {name}: needs elevation - deferred; run: {fix}",
                type="env_check", name=name,
                message=f"{name}: fix requires elevation: {fix}",
                elevation={
                    "method": "command", "command": fix,
                    "os": ctx.current_os, "id": f"env_check:{name}",
                    # `description` is the entry's own human phrasing, but it
                    # doubles as prose documentation and is often far too long
                    # to collate; the name is the honest fallback (better a
                    # terse slug than a sentence cut off mid-clause). The full
                    # description still reaches the user via `message` below.
                    "label": _item_label(
                        entry.get("label"), entry.get("description"), name,
                    ),
                    # The entry already declares how long its fix may take; the
                    # engine bounds its fix-all wait by the queue's declarations
                    # rather than one blanket number.
                    "timeout": timeout,
                    # Optional, and usually absent: fix_queue.cost_of falls back
                    # to reading the timeout above, which already separates a
                    # 3600s toolkit download from a default-timeout config fix.
                    # Declare it only when that inference is wrong.
                    **({"cost": cost} if cost else {}),
                    # Author-declared piggyback-only housekeeping: the fix rides
                    # the queue but never generates an admin nag on its own.
                    **({"opportunistic": True}
                       if entry.get("opportunistic") else {}),
                },
                agent_msg=(
                    f"The env check '{name}' is not configured and its fix "
                    f"needs elevated privileges, which a background "
                    f"SessionStart hook must not request; bootstrap deferred "
                    f"it into the fix queue."
                ),
                persist_across_sessions=True,
            )
            continue

        _fix_rc, fix_detail = run_env_command(fix, timeout)
        # A fix may install a tool and update the REGISTRY PATH, which this
        # already-running process cannot see: its os.environ predates the fix,
        # and run_env_command hands that same environ to the check's shell. The
        # re-check would then hunt for a binary that is installed but invisible
        # and report a spurious FAILED for a fix that worked (cuda-toolkit --
        # `command -v nvcc` after `winget install Nvidia.CUDA` -- is the live
        # example). Merge the registry PATH back in first, exactly as
        # _strategy_install_command does before ITS re-check.
        repair_path()
        # The fix's exit code is advisory; the re-check is authoritative
        # (task rule: env_checks has NO trust exceptions).
        re_rc, _re_detail = run_env_command(check, timeout)
        if re_rc == 0:
            ctx.action(f"env_check {name}: fixed - {fix_detail}")
        else:
            ctx.fail(
                f"env_check {name}: FAILED - {fix_detail}",
                type="env_check", name=name,
                message=f"{name}: {fix_detail}",
                agent_msg=_with_instructions(f"{name}: {fix_detail}"),
                persist_across_sessions=True,
            )


# The env.json phase table (spec 4.4). Section order is load-bearing:
# `machines` validation runs first (in _process_env_pass, before dispatch),
# then symlinks -> shell_rc -> macos_defaults -> macos_hotkeys ->
# login_items -> env_checks (array order within each; a contract script's
# internal ordering is its own business), mirroring _MANIFEST_PHASES.
# Until a section has a handler here it is reported as an ignored unknown
# key (forward compatibility, spec 4.5).
_ENV_PHASES = (
    (("symlinks",), _env_phase_symlinks),
    (("shell_rc",), _env_phase_shell_rc),
    (("macos_defaults",), _env_phase_macos_defaults),
    (("macos_hotkeys",), _env_phase_macos_hotkeys),
    (("login_items",), _env_phase_login_items),
    (("env_checks",), _env_phase_env_checks),
)


def _validate_env_machines(ctx, merged, hostname, current_os):
    """The machines-registry gatekeeping (spec 4.2).

    Returns True when entry processing may proceed. Every violation is a
    hard error: one descriptive persistent failure item, no fallbacks --
    personalization refuses to guess. On success, sets ``ctx.machine_key``
    and ``ctx.machine``.
    """
    from .env_manifest import resolve_machine, validate_entry_filters

    machines = merged.get("machines")
    if not isinstance(machines, dict) or not machines:
        ctx.fail(
            "machines registry MISSING - required in any env.json that declares entries",
            type="env_manifest",
            name="machines",
            message=(
                "env.json has no 'machines' registry. Declare every known "
                "machine in ~/.claude/env.json, e.g. "
                '{"machines": {"<hostname>": {"os": "macos|ubuntu|windows"}}}.'
            ),
            agent_msg=(
                "The merged env.json declares entries but no 'machines' "
                "registry, which is required (env.json entries are keyed by "
                "machine identity). Add a 'machines' object to "
                "~/.claude/env.json mapping each hostname to at least "
                '{"os": "macos|ubuntu|windows"}, then ask the user to type '
                "'fix-all' to re-run bootstrap."
            ),
            persist_across_sessions=True,
        )
        return False

    known = ", ".join(sorted(machines))

    # Entry-filter validation (list shape + hosts typo protection) is
    # registry-level -- run it before host resolution so a filter error
    # surfaces even on a machine that is itself unregistered on a later pass.
    filter_errors = validate_entry_filters(merged, machines)
    for err in filter_errors:
        ctx.fail(
            f"entry filter INVALID - {err}",
            type="env_manifest",
            name="entry_filter",
            message=err,
            agent_msg=(
                f"env.json entry-filter validation failed: {err} Fix the "
                "manifest, then ask the user to type 'fix-all' to re-run "
                "bootstrap."
            ),
            persist_across_sessions=True,
        )
    if filter_errors:
        return False

    machine_key = resolve_machine(machines, hostname)
    if machine_key is None:
        ctx.fail(
            f"UNKNOWN MACHINE '{hostname}' - not in the env.json machines registry",
            type="env_manifest",
            name="machines",
            message=(
                f"Unknown machine '{hostname}'. Known machines: {known}. "
                f"Add it to ~/.claude/env.json under 'machines'."
            ),
            agent_msg=(
                f"This machine's hostname '{hostname}' is not declared in "
                f"the env.json machines registry (known machines: {known}). "
                f"env.json personalization refuses to run on an unknown "
                f"machine -- no fallbacks. Add the hostname to "
                f"~/.claude/env.json under 'machines' (value at minimum "
                f'{{"os": "macos|ubuntu|windows"}}), then ask the user to '
                f"type 'fix-all' to re-run bootstrap. bootstrap.json "
                f"provisioning is unaffected."
            ),
            persist_across_sessions=True,
        )
        return False

    declared_os = machines[machine_key].get("os")
    if declared_os != current_os:
        detail = (
            f"declares os '{declared_os}'" if declared_os
            else "declares no 'os'"
        )
        ctx.fail(
            f"OS MISMATCH for machine '{machine_key}' - {detail}, "
            f"but this host detected as '{current_os}'",
            type="env_manifest",
            name="machines",
            message=(
                f"Machine '{machine_key}' {detail} in the env.json machines "
                f"registry, but this host detected as '{current_os}'. "
                f"Likely a hostname collision (e.g. dual-boot installs "
                f"sharing a hostname) or a registry typo -- fix the registry "
                f"before any personalization runs."
            ),
            agent_msg=(
                f"env.json machine '{machine_key}' {detail}, but "
                f"detect_os() reports '{current_os}'. This usually means a "
                f"hostname collision across dual-boot installs or a wrong "
                f"'os' value in ~/.claude/env.json. Fix the machines "
                f"registry, then ask the user to type 'fix-all' to re-run "
                f"bootstrap."
            ),
            persist_across_sessions=True,
        )
        return False

    ctx.machine_key = machine_key
    ctx.machine = machines[machine_key]
    ctx.ok(
        f"machine '{machine_key}' identified (hostname {hostname}, os {current_os})"
    )
    return True


def _process_env_pass(project_dir, current_os, data_dir, plugin_root,
                      action_entries, ok_entries, engine_version="",
                      hostname=None):
    """Step 3e: process the layered env.json manifest, gated by env_state.json.

    Returns the list of failure dicts (empty when green, skipped, or when no
    env.json exists anywhere). The gate (spec 4.4): the phase runs only when
    there is no stamp (first run or explicit reset via
    scripts/env-reset-cooldown.sh), the merged-manifest hash changed, the
    last result was not clean, or the engine version changed; otherwise it
    logs one verbose line and is skipped entirely. A parse error in any
    layer forces the pass to run and stamps it failed, so it re-runs every
    session until fixed.
    """
    from .env_manifest import (
        canonical_manifest_hash, current_hostname, env_gate_reason,
        load_layered_env_manifests, read_env_state, write_env_state,
    )

    merged, parse_errors = load_layered_env_manifests(project_dir)
    if not merged and not parse_errors:
        return []  # env.json not in use anywhere -- nothing to gate or stamp

    manifest_hash = canonical_manifest_hash(merged)
    if parse_errors:
        reason = "manifest parse error"
    else:
        reason = env_gate_reason(
            read_env_state(data_dir), manifest_hash, engine_version)
    if reason is None:
        ok_entries.append("up to date (merged manifest unchanged, last pass clean)")
        return []
    ok_entries.append(f"running ({reason})")

    if hostname is None:
        hostname = current_hostname()
    ctx = _EnvManifestContext(
        merged, current_os, data_dir, plugin_root,
        action_entries, ok_entries, project_dir,
        hostname, merged.get("machines") or {},
    )

    for pe in parse_errors:
        ctx.fail(
            f"manifest {pe['path']}: PARSE FAILED - {pe['error']}",
            type="manifest_parse",
            path=pe["path"],
            message=pe["error"],
            agent_msg=(
                f"The env manifest at {pe['path']} failed to parse "
                f"({pe['error']}). Open the file, fix the JSON syntax, and "
                "ask the user to type 'fix-all' to re-run bootstrap. Common "
                "causes: missing/extra commas, unquoted keys, trailing commas."
            ),
            persist_across_sessions=True,
        )

    if merged and _validate_env_machines(ctx, merged, hostname, current_os):
        handled = {"machines"}
        for keys, handler in _ENV_PHASES:
            handled.update(keys)
            if any(merged.get(k) for k in keys):
                handler(ctx)
        # Forward compatibility (spec 4.5): unknown keys are ignored with a
        # verbose log line, never an error -- an engine too old to know a
        # section skips it; the engine-version gate re-runs the phase once
        # an upgrade teaches the engine the section.
        for section in sorted(merged):
            if section not in handled:
                ctx.ok(f"section '{section}' ignored (not supported by this engine)")

    result = "failed" if ctx.failures else "clean"
    write_env_state(data_dir, manifest_hash, engine_version, result)
    return ctx.failures


def _load_plugin_config(data_dir, action_entries=None):
    """Load plugin config.yaml from data_dir if it exists. Returns dict or empty.

    A load failure is appended to ``action_entries`` when provided — the
    "every check must log its outcome" contract (B8). (Parse errors inside
    load_yaml_config still degrade to an empty dict by design; this guards
    the unexpected: import failures, permission errors, ...)
    """
    config_path = os.path.join(data_dir, "config.yaml")
    try:
        from .config_check import load_yaml_config
        if os.path.isfile(config_path):
            return load_yaml_config(config_path)
    except Exception as e:
        if action_entries is not None:
            action_entries.append(f"config load FAILED - {config_path}: {e}")
    return {}


def _find_plugins_dir(plugin_root):
    """Find the directory containing installed_plugins.json by walking up from plugin_root.

    Works for all layouts:
    - Dev: ~/Dev/<marketplace>/plugins/bootstrap → finds at ../installed_plugins.json
    - Cache: ~/.claude/plugins/cache/<mkt>/bootstrap/<ver> → finds at ~/.claude/plugins/installed_plugins.json
    - Plugin-dir override: plugin_root is the dev tree but the registry lives at
      ~/.claude/plugins/ (potentially on a different drive). The walk-up can't
      reach it, so we fall back to the canonical prod location.

    Falls back to os.path.dirname(plugin_root) only as a last resort.
    """
    d = os.path.dirname(plugin_root)
    for _ in range(10):  # safety limit
        candidate = os.path.join(d, "installed_plugins.json")
        if os.path.isfile(candidate):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    # Walk-up didn't find it. Try the canonical prod location -- handles the
    # plugin-dir-override case where plugin_root is the dev tree on a different
    # drive than ~/.claude/plugins/.
    home = os.environ.get("HOME") or os.path.expanduser("~")
    prod_dir = os.path.join(home, ".claude", "plugins")
    if os.path.isfile(os.path.join(prod_dir, "installed_plugins.json")):
        return prod_dir
    # Final fallback: immediate parent (original behavior)
    return os.path.dirname(plugin_root)



def _run_script_phase(script_def, plugin_root, data_dir, config, action_entries, ok_entries=None, prefix="", plugin_name="", project_dir=None):
    """Run a custom bootstrap script. Returns list of failures."""
    import importlib.util

    if ok_entries is None:
        ok_entries = []
    log_entries = action_entries  # Failures and unconditional messages.
    script_path = os.path.join(plugin_root, script_def["path"])
    entry_point = script_def.get("entry_point", "bootstrap")

    if not os.path.isfile(script_path):
        log_entries.append(f"{prefix}script: skipped ({script_def['path']} not found)")
        return []

    # Build context object for the script
    ctx = _ScriptContext(config, data_dir, plugin_root, log_entries, ok_entries, prefix, plugin_name, project_dir)

    try:
        spec = importlib.util.spec_from_file_location("_bootstrap_script", script_path)
        if spec is None or spec.loader is None:
            log_entries.append(f"{prefix}script: FAILED - could not load {script_def['path']}")
            return []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        func = getattr(module, entry_point, None)
        if func is None:
            log_entries.append(f"{prefix}script: FAILED - {entry_point}() not found in {script_def['path']}")
            return []

        func(ctx)
        return ctx.failures
    except Exception as e:
        log_entries.append(f"{prefix}script: FAILED - {e}")
        return []


class _ScriptContext:
    """Context object passed to custom bootstrap scripts."""

    def __init__(self, config, data_dir, plugin_root, log_entries, ok_entries, prefix, plugin_name, project_dir=None):
        self.config = dict(config) if config else {}
        self.config_path = os.path.join(data_dir, "config.yaml")
        self.data_dir = data_dir
        self.plugin_root = plugin_root
        # Canonical project root the engine was invoked against (Claude Code's
        # launch CWD). May be None for non-project sessions. Scripts should use
        # this instead of re-deriving from Path.cwd() — never walk up looking
        # for .claude/ since Claude Code itself does not.
        self.project_dir = project_dir
        self.failures = []
        self._log_entries = log_entries
        self._ok_entries = ok_entries
        self._prefix = prefix
        self._plugin_name = plugin_name

    def save_config(self) -> None:
        """Write config back to disk."""
        from .config_check import save_yaml_config
        save_yaml_config(self.config_path, self.config)

    def add_failure(self, failure_type: str, **kwargs) -> None:
        """Register a failure for fix-all aggregation."""
        failure = {"type": failure_type, "plugin": self._plugin_name}
        failure.update(kwargs)
        self.failures.append(failure)

    def log(self, message: str) -> None:
        """Add an action log entry. Always shown to the user."""
        self._log_entries.append(f"{self._prefix}{message}")

    def log_ok(self, message: str) -> None:
        """Add an ok log entry. Hidden from the user; shown only in verbose mode."""
        self._ok_entries.append(f"{self._prefix}{message}")


def _read_new_log_entries(data_dir, start_time=None):
    """Read log entries since the last time we displayed them.

    Reads EVERY block, not just the shell's. That is deliberate and was briefly
    got wrong: blocks written by OTHER processes are the whole point of reading
    the log back at all. The shell hook's pre-Python block is one; so is the
    `<label> elevation` block a fix-all pass writes for its spawned re-check
    pass to surface, and the harvest/lock blocks a standing-down engine writes.
    Scoping this to `Shell` headers silently swallowed the confirmation that an
    elevated fix had run.

    Retention is decoupled from presentation by the pass record (records.py),
    NOT by narrowing this reader: bootstrap.log stays curated (ok entries gated
    on log_success, so they never reappear here) while
    `bootstrap_events.jsonl` keeps everything unconditionally.

    Uses a 'last_displayed_at' file to track the timestamp of the last display.
    Does NOT update the marker — call _update_display_marker() after all entries are written.

    A `start_time` floor (default: now - 120s) bounds the lookback window even
    when the marker is missing or stale. This prevents the engine from dumping
    the entire historical log to the user when the marker is reset (e.g. on
    fresh installs, version downgrades, or after a corrupt-marker recovery).
    Without this bound, the user would see months of stale entries — including
    historical failures already resolved — every time the marker disappears.
    """
    from .log import LOG_FILENAME
    log_file = os.path.join(data_dir, LOG_FILENAME)
    marker_file = os.path.join(data_dir, "last_displayed_at")

    try:
        with open(log_file, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return ""

    # Read last-displayed timestamp
    last_displayed = ""
    try:
        with open(marker_file, "r") as f:
            last_displayed = f.read().strip()
    except FileNotFoundError:
        pass

    # Compute the floor: 120 seconds before start_time covers shell startup
    # plus clock skew without including any pre-session content.
    if start_time is None:
        start_time = datetime.now(timezone.utc)
    floor_dt = start_time - timedelta(seconds=120)
    floor = floor_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Effective marker is the LATER of last_displayed and the floor. This means
    # entries older than `start_time - 120s` are never re-displayed, even if the
    # marker is missing or stale.
    effective_marker = max(last_displayed, floor)

    # Filter to blocks strictly after the effective marker.
    # Timestamps are only on header lines (--- label timestamp ---).
    # Untimestamped headers (or content before any header) start excluded —
    # they only get included if a subsequent timestamped header lets them in.
    # Known limitation (B20): timestamps are second-resolution and the
    # comparison is strict, so a block a concurrent session writes within the
    # SAME second as the marker is never displayed. Accepted: the window is
    # one second, the entries still land in bootstrap.log, and an inclusive
    # comparison would re-display every already-shown block instead.
    new_lines = []
    include_block = False
    for line in lines:
        ts = _extract_timestamp(line)
        if ts:
            # This is a header line — decide whether to include this block
            include_block = ts > effective_marker
        if include_block:
            new_lines.append(line)

    if not new_lines:
        return ""

    return "".join(new_lines).rstrip("\n")


def _resolve_download_def(download_block, current_os):
    """Pick the right download entry for this host.

    Lookup order:
      1. "<os>-<arch>" — e.g. "macos-arm64", "windows-amd64". Allows shipping
         distinct binaries per architecture.
      2. "<os>" — for tools whose binary doesn't vary by arch on this OS.

    Returns the matching entry dict, or None if neither key is present.
    """
    if not isinstance(download_block, dict) or not download_block:
        return None
    from .platform_detect import detect_arch
    arch_key = f"{current_os}-{detect_arch()}"
    if arch_key in download_block:
        return download_block[arch_key]
    return download_block.get(current_os)


def _parse_semver(v):
    """Parse a dotted version into a comparable (major, minor, patch) tuple.

    Tolerant: strips a leading ">=", ignores pre-release/build suffixes, and
    treats non-numeric components as 0. "1.2.3" -> (1,2,3); ">=0.21" -> (0,21,0).
    """
    s = str(v).strip()
    if s.startswith(">="):
        s = s[2:].strip()
    out = []
    for part in s.split(".")[:3]:
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def _version_satisfies(current, required):
    """True if `current` >= the minimum `required` version (both dotted semver).

    `required` may be bare ("0.21.0") or ">=0.21.0" -- both mean "at least".
    """
    return _parse_semver(current) >= _parse_semver(required)


def _clear_project_cooldown(data_dir, project_dir):
    """Delete this project's cooldown stamp so the next SessionStart re-runs.

    Routed through the stamps module (project scope), which mirrors the path
    construction in hooks/sessionstart/session-bootstrap.sh:
    <data_dir>/cooldowns/last_run_epoch.<sha1(project_dir)>, with the same
    "_global_" fallback when no project_dir is available. bash and Python share
    the path convention, not a function — see stamps.py's bash/Python boundary
    note. Silent on any I/O error (Stamp.clear swallows it) -- a stale stamp at
    worst delays the next re-run by the throttle window; it never blocks
    remediation.
    """
    from .stamps import project_stamp
    project_stamp(data_dir, "last_run_epoch", project_dir).clear()


def _restamp_project_cooldown(data_dir, project_dir):
    """Refresh this project's cooldown stamp at the end of a clean pass.

    Same project-scope stamp as _clear_project_cooldown. Only refreshes an
    EXISTING stamp — creating it is the shell hook's job (so console runs,
    tests, and direct engine invocations never plant cooldowns), and the
    failure path clears it instead. Content matches the shell's format: the
    current epoch as text (the shell reads it for the age check; the
    registry-bypass compares mtimes — and Stamp.write is the explicit touch that
    advances the mtime). Silent on I/O errors — at worst the registry-mtime
    bypass re-arms one extra pass.
    """
    import time
    from .stamps import project_stamp
    stamp = project_stamp(data_dir, "last_run_epoch", project_dir)
    if not stamp.exists():
        return
    try:
        stamp.write(str(int(time.time())))
    except OSError:
        pass


def _update_display_marker(data_dir):
    """Update the display marker to the latest timestamp in the log file."""
    from .log import LOG_FILENAME
    log_file = os.path.join(data_dir, LOG_FILENAME)
    marker_file = os.path.join(data_dir, "last_displayed_at")

    try:
        with open(log_file, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

    latest_ts = ""
    for line in reversed(lines):
        ts = _extract_timestamp(line)
        if ts:
            latest_ts = ts
            break
    if latest_ts:
        os.makedirs(data_dir, exist_ok=True)
        with open(marker_file, "w") as f:
            f.write(latest_ts)


def _extract_timestamp(line):
    """Extract ISO timestamp from a log header line.

    Format: --- label YYYY-MM-DDTHH:MM:SSZ ---
    Returns the timestamp string or empty string.
    Rejects footer lines (--- label done in X.Xs ---).
    """
    line = line.strip()
    if line.startswith("---") and line.endswith("---"):
        parts = line.split()
        if len(parts) >= 3:
            candidate = parts[-2]
            # Must look like an ISO timestamp (starts with digit, contains T)
            if candidate and candidate[0].isdigit() and "T" in candidate:
                return candidate
    return ""


def emit_success_response(log_content, label="bootstrap", output_file=None,
                          recorder=None):
    """Emit hook JSON showing bootstrap log to user and agent.

    Reload/restart notices ride inside ``log_content`` as ordinary display
    lines. Deliberately NO relay directive in additionalContext: whether and
    when to restart after a plugin update is the user's call, and an
    "ACTION REQUIRED -- surface this now" preamble made the session's Claude
    treat a routine update notice as urgent. See plugin-reload-lifecycle.md.
    """
    if output_file:
        # Background mode: consumed by UserPromptSubmit hook.
        # `systemMessage` is user-facing, `additionalContext` is Claude-facing.
        body = f"{label} -> bootstrap complete:\n{log_content}"
        response = {
            "continue": True,
            "suppressOutput": False,
            "systemMessage": body,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": body,
            },
        }
        _write_atomic(output_file, json.dumps(response))
        _record_emit(recorder, "pending", response)
    else:
        # SessionStart hook: supports hookSpecificOutput with hookEventName
        response = {
            "continue": True,
            "suppressOutput": False,
            "systemMessage": f"{label}:\n{log_content}",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"{label} -> bootstrap complete:\n{log_content}",
            },
        }
        print(json.dumps(response))
        _record_emit(recorder, "stdout", response)


# Failure types fix-all can deterministically remediate without user input.
# Anything else (config items asking for API keys, python_stub admin
# elevation, parse errors in user-edited files, generic custom failures)
# is manual-only — Claude can guide but can't run a one-shot command.
#
# marketplace/plugin/git_dep are deliberately ABSENT: they cross the network
# and only surface as failures after a failed in-line attempt, so they route to
# ASK (see _CREDENTIAL_NETWORK_TYPES / _ask_reason). json/ini stay here because
# their in-user-scope case is genuinely auto-fixable.
#
# Routing authority is _ask_reason, NOT this set: an out-of-scope json/ini
# failure is still a member here (so _is_auto_fixable reports True) yet _ask_reason
# returns "info" and the ASK partition wins -- the item is surfaced, never
# auto-run. This set governs fix-all *eligibility* for the AUTO path only; it is
# never consulted to override an ASK verdict.
_AUTO_FIXABLE_TYPES = frozenset({
    "path", "venv", "ini", "pypi", "json", "sync_to_data",
})


def _opportunistic(failure):
    """True when the failure's deferred task is piggyback-only housekeeping.

    Such a failure is dropped outright when the whole queue is opportunistic
    (see _elevation_step) -- surfacing it raw would recreate the exact nag the
    flag exists to remove.
    """
    desc = failure.get("elevation")
    return isinstance(desc, dict) and bool(desc.get("opportunistic"))


def _spoken_for(failure):
    """True when the aggregated elevation_script item covers this failure.

    Every failure carrying an `elevation` descriptor contributed a task to the
    fix queue, so the aggregate's offer resolves it.
    """
    return isinstance(failure.get("elevation"), dict)


def _visible_failures(failures):
    """Drop items the elevation aggregate speaks for, keeping the aggregate.

    No-op when no aggregate is present: without one, nothing else would report
    those failures and they would vanish silently.
    """
    if not any(f.get("type") == "elevation_script" for f in failures):
        return failures
    return [f for f in failures if not _spoken_for(f)]


def _is_elevation_only(failures):
    """True when every failure is the elevation aggregate or covered by it.

    The predicate for the focused message: an elevation_script item never
    arrives alone (the per-task failures it summarizes persist alongside it by
    design), so "all failures are elevation_script" would never fire.
    """
    has_aggregate = any(f.get("type") == "elevation_script" for f in failures)
    if not has_aggregate:
        return False
    return all(f.get("type") == "elevation_script" or _spoken_for(f)
               for f in failures)


def _is_auto_fixable(failure):
    t = failure.get("type")
    if t == "elevation_script":
        # fix-all launches the fix runner itself -- but only where that launch
        # can actually happen. `fix_all_cmd` is set exactly when it can (Windows,
        # and not already inside a fix-all run), so it doubles as the eligibility
        # signal: on Unix there is no TTY to prompt on, and re-offering fix-all
        # during a fix-all run that just failed would loop the prompt.
        return bool(failure.get("fix_all_cmd"))
    if t == "tool":
        # Tools are fix-all eligible only when we know how to install them
        # AND the install hasn't already run successfully. If install_state
        # is "installed_but_path_stale", rerunning the install just produces
        # "already installed" — fix-all can't help; it's a bootstrap bug.
        # installed_but_path_stale: reinstall just says "already installed".
        # manual_install: there's no unattended installer — only the user can act.
        # needs_elevation: only the user can run sudo — a background hook must not.
        if failure.get("install_state") in (
            "installed_but_path_stale", "manual_install", "needs_elevation",
        ):
            return False
        return bool(failure.get("install_cmd"))
    return t in _AUTO_FIXABLE_TYPES


# The two-outcome contract (fleet-management policy). Every surfaced issue is
# exactly ONE of:
#   AUTO -- fix it now, no prompt. This is the DEFAULT, because bootstrap
#           manages a fleet: it will happily INSTALL SOFTWARE and edit manifests
#           unattended, as long as the fix needs none of the three things below.
#   ASK  -- get the user's go-ahead via the AskUserQuestion tool FIRST, because
#           the fix requires one of exactly three things only the user can give:
#             "elevation" -- admin / root / UAC / sudo a background hook cannot
#                            obtain;
#             "action"    -- a physical or out-of-band act only the user can
#                            perform (press a device button, restart the IDE,
#                            install a GUI app with no unattended installer);
#             "info"      -- a value only the user holds (an API key/secret,
#                            which machine in the fleet this is).
# There is no third "guide the user through it / work through it manually"
# outcome. See skills/bootstrap/references/remediation-reference.md
# ("Two outcomes: auto-fix or ask") for the authored criteria and rationale.
_ASK_REASONS = ("elevation", "action", "info")


# Types whose fix crosses the network and can fail on authentication. Each of
# these only becomes a *failure* AFTER the engine already attempted the
# operation in-line (add_marketplace / install_plugin / clone_git_dep) and it
# failed -- so the item reaching remediation is a failed network/auth op, not a
# fresh one. Handing Claude an AUTO "fix now, run the command" for it is doomed
# twice over: retrying what the engine just failed accomplishes nothing, and the
# common failure cause is a credential (SSH key, token) a background hook cannot
# supply. Route to the user instead. (This never touches the happy path -- a
# marketplace/plugin/git op that succeeds in-line produces no failure.)
_CREDENTIAL_NETWORK_TYPES = frozenset({"marketplace", "plugin", "git_dep"})


def _user_scope_root():
    """The one tree bootstrap may write to unattended: ~/.claude."""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.normpath(os.path.join(home, ".claude"))


def _path_in_user_scope(p):
    """True when p resolves inside ~/.claude. Empty/None -> True (nothing to
    guard: the failure names no write target)."""
    if not p:
        return True
    root = _user_scope_root()
    ap = os.path.normpath(os.path.expanduser(str(p)))
    return ap == root or ap.startswith(root + os.sep)


def _write_target(failure):
    """The filesystem path an AUTO fix for this failure would write, if it names
    one. json carries `target`, ini carries `file`, sync carries `dst`."""
    return failure.get("target") or failure.get("file") or failure.get("dst")


def _ask_reason(failure):
    """Why this failure must ASK the user first: 'elevation' | 'action' | 'info',
    or None when it is AUTO-fixable (the fleet default).

    An explicit `ask_reason` on the failure wins -- that is how a check
    (`env_check`, a plugin `custom_bootstrap` via `ctx.add_failure`, ...) declares
    it needs the user. Otherwise the reason is derived from signals the engine
    already records, so the common cases need no per-site annotation.
    """
    explicit = failure.get("ask_reason")
    if explicit in _ASK_REASONS:
        return explicit
    t = failure.get("type")
    state = failure.get("install_state")
    # Elevation: admin/UAC/sudo the hook cannot obtain.
    if state == "needs_elevation" or t in ("python_stub", "elevation_script"):
        return "elevation"
    # Action: something only the user can physically do.
    if state == "manual_install" or t == "bootstrap_outdated":
        return "action"
    # Info: a value only the user can supply, or a diagnostic only they can run.
    if state == "installed_but_path_stale" or t in ("config", "project_config"):
        return "info"
    # Network + credential classes: a failed marketplace/plugin/git op needs the
    # user, not a doomed AUTO retry (see _CREDENTIAL_NETWORK_TYPES).
    if t in _CREDENTIAL_NETWORK_TYPES:
        return "info"
    # Scope guard: an AUTO fix may only write inside ~/.claude. A json/ini
    # remediation the manifest points at a shared or VCS-tracked file must ask
    # first -- editing a shared file unattended is the failure we will not
    # repeat. In-user-scope targets stay AUTO.
    if t in ("json", "ini") and not _path_in_user_scope(_write_target(failure)):
        return "info"
    # Safety net: AUTO means "fix it now" and hands Claude a run-this directive,
    # so an item bootstrap CANNOT actually auto-fix (no runnable command/edit and
    # not fix-all-eligible) must not be routed there -- it would be told to "run
    # the command shown" with nothing to run. Surface it to the user instead
    # (ASK/info). Phase 2 either gives such an item a real remediation (-> AUTO)
    # or a precise ask reason; until then, asking is the safe default.
    if not _auto_fixable_now(failure):
        return "info"
    return None


def _auto_fixable_now(failure):
    """True when Claude can carry the fix out with NO user input: a fix-all-
    eligible type, or an explicit runnable command on the failure. Distinct from
    `_is_auto_fixable` (fix-all-runnable specifically) -- an AUTO item may also be
    a plain command Claude runs itself or a manifest edit it makes."""
    if _is_auto_fixable(failure):
        return True
    return bool(failure.get("install_cmd") or failure.get("remediation_cmd")
                or failure.get("remediation"))


def _needs_user(failure):
    """True when the failure is ASK (needs the user before we touch the machine),
    False when it is AUTO (fix it now)."""
    return _ask_reason(failure) is not None


def _format_indexes(idxs):
    """Render a sorted index list as '#1, #2, #4' for footer copy."""
    return ", ".join(f"#{i}" for i in idxs)


_ASK_REASON_BLURB = {
    "elevation": "needs admin access",
    "action": "needs you to do something",
    "info": "needs a detail from you",
}


def _short_label(f, limit=None):
    """A friendly one-line label for a failure's summary line.

    Prefer the plugin/check-authored `user_msg` (that is where a friendly,
    plain-language phrasing like 'hue-kit wants to pair with your Hue bridge'
    lives), then `message`, then name/type.

    `limit` selects the audience. The systemMessage summary puts ONE item per
    LINE, so it passes no limit and gets the full phrasing -- that is where the
    user actually reads what is wrong. A COLLATED caller (the AskUserQuestion
    directive, which flattens every item onto one line) passes
    messages.ITEM_MAX and gets the first candidate that FITS, whole; the long
    ones are skipped rather than cut, so no line ever ends mid-clause. See
    engine-internals.md, "Collated message text".
    """
    def first_line(value):
        return str(value).splitlines()[0] if value else ""

    candidates = (first_line(f.get("user_msg")), first_line(f.get("message")),
                  f.get("name"), f.get("type"), "issue")
    if limit is None:
        return next((str(c) for c in candidates if c and str(c).strip()), "issue")
    return _item_label(*candidates, limit=limit)


def _auto_label_lines(auto):
    """User-facing labels for the AUTO (fix-now) items: list[str].

    Tools get the self-evident "Install <name>"; everything else uses its short
    label. No index refs -- the user never sees the numbered additionalContext.
    """
    lines = []
    for f in auto:
        if f.get("type") == "tool":
            lines.append(f"Install {f.get('name', 'tool')}")
        else:
            lines.append(_short_label(f))
    return lines


def _ask_label_lines(ask, limit=None):
    """User-facing labels for the ASK items: list[(label, reason)].

    `limit` is passed straight through to _short_label -- None for the
    one-item-per-line systemMessage, messages.ITEM_MAX for the collated
    directive.

    The elevation_script aggregate expands into one line per queued task (its
    own `labels` field -- do not recompute), each an elevation reason; every
    other ASK item contributes its short label + its own ask reason.
    """
    lines = []
    for f in ask:
        if f.get("type") == "elevation_script":
            for lbl in f.get("labels") or []:
                lines.append((lbl, "elevation"))
        else:
            lines.append((_short_label(f, limit=limit), _ask_reason(f) or "info"))
    return lines


def _auto_user_msg(auto):
    """systemMessage block for AUTO items: named items + a fixing-now line."""
    body = "\n".join(_auto_label_lines(auto))
    lead = "Bootstrap is fixing these automatically -- no action needed from you."
    return f"{body}\n{lead}" if body else lead


def _ask_user_msg(ask):
    """systemMessage block for ASK items: named items (with why) + a will-ask line."""
    lines = _ask_label_lines(ask)
    body = "\n".join(f"{lbl} ({_ASK_REASON_BLURB.get(reason, 'needs your input')})"
                     for lbl, reason in lines)
    lead = ("Claude will ask before changing anything here -- nothing happens "
            "unless you say so.")
    return f"{body}\n{lead}" if body else lead


def _ask_agent_directive(failures, ask_idxs):
    """The additionalContext directive for ASK items: mandate AskUserQuestion.

    Generalizes the compliant elevation-aggregate template (fix_queue.py): state
    the problem once, require a single AskUserQuestion with exactly two options
    ("Do nothing" leading, then "Fix"), act only on "Fix", never re-prompt.
    """
    from .messages import ITEM_MAX
    labels = _ask_label_lines([failures[i - 1] for i in ask_idxs],
                              limit=ITEM_MAX)
    listed = _numbered(lbl for lbl, _ in labels)
    reasons = sorted({reason for _, reason in labels})
    return (
        f"ASK THE USER before touching items {_format_indexes(ask_idxs)} -- they "
        f"need {', '.join(reasons)} (elevation = admin/UAC/sudo a background hook "
        f"cannot obtain; action = something only the user can do, e.g. press a "
        f"device button, restart the IDE, install a GUI app; info = a value only "
        f"the user has, e.g. an API key or which machine this is). Do NOT run "
        f"their fixes yet. A queued 'fix-all' line is easy to miss, so ASK with "
        f"the AskUserQuestion tool -- do not merely mention it. Question: "
        f'"Bootstrap needs your OK to fix: {listed}. Go ahead?" Exactly two '
        f'options, in this order: 1. "Do nothing" (the default -- bootstrap '
        f're-checks next session, nothing is lost); 2. "Fix" (carry out the '
        f'per-item steps above). Act ONLY if the user picks "Fix" (or says so); '
        f'on "Do nothing", say nothing further and do not re-prompt.'
    )


def _auto_agent_directive(auto_idxs):
    """The additionalContext directive for AUTO items: fix now, do not ask."""
    return (
        f"FIX NOW, automatically, without asking the user -- items "
        f"{_format_indexes(auto_idxs)}. This is fleet policy: installing "
        f"non-elevated software and editing manifests unattended is expected. "
        f"Run the command shown for each item, or make the described edit "
        f"yourself. Do NOT wait for the user to say 'fix-all'."
    )


def _emit_unsupported_platform(message, data_dir, args):
    """Surface an unsupported-platform hard error and stop the pass.

    Non-Ubuntu Linux fails fast (detect_os raised UnsupportedPlatformError):
    the platform is unsupported as a fact, so this is NOT a per-item fix-all
    failure -- there is nothing to auto-fix. It surfaces through the same
    channels as normal engine output (a bootstrap_display.pending file in
    background mode, stdout JSON on SessionStart, plain text in console mode)
    so the user sees WHY bootstrap did not run. The pass returns without
    touching tools, manifests, or the cooldown stamp -- the shell hook already
    stamped the per-project cooldown BEFORE launching the engine, so leaving it
    in place means this message re-surfaces at most once per cooldown window
    (not on every session).
    """
    label = "bootstrap"
    system_message = f"{label} -> {message}"
    additional_context = (
        f"{label} -> bootstrap did not run: {message} This is not fixable in "
        "place; tell the user this platform is unsupported by bootstrap."
    )
    if args.console:
        print(system_message)
        return
    if args.background:
        response = {
            "continue": True,
            "suppressOutput": False,
            "systemMessage": system_message,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": additional_context,
            },
        }
        pending = os.path.join(data_dir, "bootstrap_display.pending")
        _write_atomic(pending, json.dumps(response))
    else:
        response = {
            "continue": True,
            "suppressOutput": False,
            "systemMessage": system_message,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": additional_context,
            },
        }
        print(json.dumps(response))


def _emit_focused(failure, label, output_file, persistent_output_file,
                  recorder=None):
    """Emit ONE failure's own messages as the whole response.

    Used when every failure shares a single remediation, so the numbered list
    and fix-all footer would only bury it.

    Two-outcome contract still holds here: a focused ASK failure must direct
    Claude through AskUserQuestion. The elevation_script aggregate already builds
    that directive itself (fix_queue.py), so it is passed through untouched; any
    other ASK-type focused failure (e.g. python_stub -- UAC elevation) is wrapped
    so its agent-facing text mandates the AskUserQuestion prompt instead of a
    bare 'walk them through it'.
    """
    user_msg = failure.get("user_msg", failure.get("message", ""))
    agent_msg = failure.get("agent_msg", failure.get("message", ""))
    if _needs_user(failure) and failure.get("type") != "elevation_script":
        directive = _ask_agent_directive([failure], [1])
        agent_msg = f"{directive}\n\nAfter the user picks \"Fix\", the steps are:\n{agent_msg}"
    response = {
        "continue": True,
        "suppressOutput": False,
        "systemMessage": f"{label}: {user_msg}",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit" if output_file else "SessionStart",
            "additionalContext": f"{label} -> {agent_msg}",
        },
    }
    if output_file:
        _write_atomic(output_file, json.dumps(response))
        if persistent_output_file:
            _write_atomic(persistent_output_file, json.dumps(response))
    else:
        print(json.dumps(response))
    _record_emit(recorder, "focused", response)


def emit_failure_response(failures, current_os, log_content, label="bootstrap", output_file=None, persistent_output_file=None, recorder=None):
    """Emit hook JSON with fix-all directives to stdout or file.

    If persistent_output_file is provided AND any failure is marked
    `persist_across_sessions`, the same JSON is also written to that path so
    subsequent sessions can re-prime bootstrap_display.pending from it.
    """
    agent_lines = [f"{label} -> Setup issues found. Fix in order:\n"]

    # Items the aggregated elevation_script item already speaks for are not
    # listed again: each carries an `elevation` descriptor that IS a task in the
    # queue the aggregate offers to run, and re-stating the elevation rationale
    # once per item is what made this output unreadable. Suppression is
    # conditional on the aggregate actually existing -- if the queue write
    # failed there is nothing speaking for them, so they must surface raw.
    failures = _visible_failures(failures)

    for i, f in enumerate(failures, 1):
        plugin_tag = f" [{f['plugin']}]" if f.get("plugin", "bootstrap") != "bootstrap" else ""
        if f["type"] == "tool":
            state = f.get("install_state", "no_install_cmd")
            if state == "installed_but_path_stale":
                # Don't prescribe a reinstall — winget will say "already
                # installed" and the user loops. Tell Claude what actually
                # happened so it can decide whether to ask the user to
                # verify with `where.exe` or escalate as a bootstrap bug.
                agent_lines.append(
                    f"{i}. {f['name']}{plugin_tag}: install ran successfully but bootstrap still "
                    f"can't find the binary. Don't re-run the install command — it will say "
                    f"\"already installed.\" Ask the user to run `where.exe {f['name']}` "
                    f"(Windows) or `which {f['name']}` (Unix) and report where the binary "
                    f"actually lives; that location should be added to ~/.local/bin or bootstrap's "
                    f"download fallback."
                )
            elif state == "install_failed":
                agent_lines.append(
                    f"{i}. Install of {f['name']} failed{plugin_tag}. "
                    f"Re-run and capture output: `{f['install_cmd']}`"
                )
            elif state == "manual_install":
                agent_lines.append(
                    f"{i}. Install {f['name']}{plugin_tag} manually — there is no unattended "
                    f"installer for this OS. Install the vendor package and ensure "
                    f"`{f['name']}` is on PATH. If this machine doesn't use it, disable the "
                    f"plugin or override the tool in a layered bootstrap.json."
                )
            elif state == "scoop_failed":
                agent_lines.append(
                    f"{i}. Installing {f['name']}{plugin_tag} via Scoop failed: "
                    f"{f.get('message', 'see log')}. Check network access and that Scoop "
                    f"could be provisioned (~/scoop), then re-run."
                )
            elif state == "brew_failed":
                agent_lines.append(
                    f"{i}. Installing {f['name']}{plugin_tag} via Homebrew failed: "
                    f"{f.get('message', 'see log')}. Ensure Homebrew is installed "
                    f"(https://brew.sh) and the formula/cask name is correct, then re-run."
                )
            elif state == "apt_failed":
                agent_lines.append(
                    f"{i}. Installing {f['name']}{plugin_tag} via apt failed: "
                    f"{f.get('message', 'see log')}. Check the package name and apt "
                    f"sources, then re-run."
                )
            elif state == "needs_elevation":
                # Only the user can elevate; a background hook must not sudo/UAC.
                # Surface the exact manual command (carried in agent_msg).
                agent_lines.append(
                    f"{i}. {f.get('agent_msg') or (f['name'] + ' needs elevation to install')}"
                    f"{plugin_tag}"
                )
            else:
                agent_lines.append(f"{i}. Install {f['name']}{plugin_tag}: `{f['install_cmd'] or 'see documentation'}`")
        elif f["type"] == "path":
            agent_lines.append(f"{i}. Add {f['path']} to PATH{plugin_tag}")
        elif f["type"] == "venv":
            agent_lines.append(f"{i}. Setup venv{plugin_tag}: `{f['remediation_cmd']}`")
        elif f["type"] == "git_dep":
            agent_lines.append(f"{i}. Clone {f['name']}{plugin_tag}: `{f['remediation_cmd']}`")
        elif f["type"] == "config":
            agent_lines.append(f"{i}. {f['agent_msg']}{plugin_tag}")
        elif f["type"] == "project_config":
            agent_lines.append(f"{i}. {f['agent_msg']}{plugin_tag}")
        elif f["type"] == "ini":
            agent_lines.append(f"{i}. Fix INI setting {f['key']} in {f['file']}{plugin_tag}: {f['message']}")
        elif f["type"] == "pypi":
            agent_lines.append(f"{i}. Download {f['package']} from PyPI{plugin_tag}: {f['message']}")
        elif f["type"] == "script":
            agent_lines.append(f"{i}. Script issue{plugin_tag}: {f.get('message', 'see log')}")
        elif f["type"] == "json":
            agent_lines.append(f"{i}. Merge JSON entries into {f['target']}{plugin_tag}: {f['message']}")
        elif f["type"] == "marketplace":
            agent_lines.append(f"{i}. Add marketplace {f['name']}{plugin_tag}: {f['message']}")
        elif f["type"] == "plugin":
            agent_lines.append(f"{i}. Install plugin {f['ref']}{plugin_tag}: {f['message']}")
        elif f["type"] == "sync_to_data":
            agent_lines.append(f"{i}. Sync {f['src']} -> {f['dst']}{plugin_tag}: {f['message']}")
        elif f["type"] == "python_stub":
            agent_lines.append(f"{i}. python stub fix needed{plugin_tag}: {f.get('agent_msg', f.get('message', 'see log'))}")
        elif f["type"] == "elevation_script":
            # Aggregated remediation: names what the ONE fix queue covers.
            # Fix-all eligible where the engine can actually launch the runner
            # (Windows, outside an existing fix-all run) -- see _is_auto_fixable.
            agent_lines.append(f"{i}. {f.get('agent_msg', f.get('message', 'run the elevation remediation script'))}{plugin_tag}")
        elif f["type"] == "manifest_parse":
            agent_lines.append(f"{i}. {f.get('agent_msg', f.get('message', 'manifest parse error'))}{plugin_tag}")
        else:
            # Generic fallback for custom failure types (e.g. emitted by plugin
            # custom_bootstrap scripts via ctx.add_failure). If the failure
            # provides agent_msg / user_msg / message, surface it directly so
            # the fix-all directive reaches Claude instead of being silently
            # dropped. Without this, any unrecognized type produced no line at
            # all and Claude had no remediation guidance.
            generic = f.get("agent_msg") or f.get("user_msg") or f.get("message") or f"{f['type']}: see log"
            agent_lines.append(f"{i}. {generic}{plugin_tag}")

    # Two outcomes only (the fleet-management contract -- see _ask_reason): each
    # item is either AUTO (fix now, no prompt -- the default, and it covers
    # installing non-elevated software) or ASK (get the user's go-ahead via the
    # AskUserQuestion tool first, because the fix needs elevation, a user action,
    # or info only the user has). There is no third "manual attention" outcome.
    auto = [f for f in failures if not _needs_user(f)]
    ask = [f for f in failures if _needs_user(f)]
    auto_idxs = [i for i, f in enumerate(failures, 1) if not _needs_user(f)]
    ask_idxs = [i for i, f in enumerate(failures, 1) if _needs_user(f)]

    trailer_parts = []
    if auto_idxs:
        trailer_parts.append(_auto_agent_directive(auto_idxs))
    if ask_idxs:
        trailer_parts.append(_ask_agent_directive(failures, ask_idxs))
    agent_lines.append("\n" + "\n\n".join(trailer_parts))
    agent_msg = "\n".join(agent_lines)

    # User-facing (systemMessage) summary: the AUTO half says what is being fixed
    # for them; the ASK half names what Claude will ask about first.
    user_parts = []
    if auto:
        user_parts.append(_auto_user_msg(auto))
    if ask:
        user_parts.append(_ask_user_msg(ask))
    user_msg = "\n\n".join(user_parts)

    # Focused special-cases: when every failure shares ONE remediation, the
    # numbered list + fix-all boilerplate above is noise. Emit that item's own
    # structured messages instead and drop log_content entirely.
    #
    # python_stub: Python itself is broken, so there is nothing to enumerate.
    # elevation-only: one queue, one offer -- the aggregate already names what
    # it covers, and the per-task items are suppressed above.
    focus = None
    python_stub_failures = [f for f in failures if f["type"] == "python_stub"]
    if python_stub_failures and len(python_stub_failures) == len(failures):
        focus = python_stub_failures[0]
    elif _is_elevation_only(failures):
        focus = next(f for f in failures if f["type"] == "elevation_script")

    if focus is not None:
        _emit_focused(focus, label, output_file, persistent_output_file,
                      recorder=recorder)
        return

    # General path: mixed failures.
    if output_file:
        # Background mode: consumed by UserPromptSubmit hook.
        # `additionalContext` gives Claude the full log + fix directives,
        # `systemMessage` is user-facing only. `user_msg` was selected above
        # based on the auto/manual partition.
        response = {
            "continue": True,
            "suppressOutput": False,
            "systemMessage": f"{label} -> Setup issues found. Fix in order:\n{log_content}\n\n{user_msg}",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": f"{label} -> bootstrap complete:\n{log_content}\n\n{agent_msg}",
            },
        }
        _write_atomic(output_file, json.dumps(response))
        if persistent_output_file:
            _write_atomic(persistent_output_file, json.dumps(response))
    else:
        # SessionStart hook: supports hookSpecificOutput with hookEventName
        response = {
            "continue": True,
            "suppressOutput": False,
            "systemMessage": f"{label}:\n{log_content}",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": agent_msg,
            },
        }
        print(json.dumps(response))
    _record_emit(recorder, "pending" if output_file else "stdout", response)


if __name__ == "__main__":
    main()
