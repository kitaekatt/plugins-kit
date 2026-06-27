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


def main():
    """Entry point: run the engine pass with crash containment.

    The shell hook stamps the per-project cooldown BEFORE launching the
    engine and (in background mode) only surfaces what the engine writes to
    bootstrap_display.pending. Without containment, an unhandled exception
    means: traceback lost to engine_output.log, no pending file (silent
    failure for the user), and a stamped cooldown that throttles every
    re-run for the rest of the window. Contain it: write a crash .pending so
    the next prompt surfaces the failure, clear the cooldown so the next
    SessionStart retries, then re-raise behavior via exit code 1 with the
    traceback on stderr (still captured by engine_output.log).
    """
    try:
        _main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception:
        import traceback
        tb = traceback.format_exc()
        try:
            _emit_engine_crash(tb)
        except Exception:
            pass  # crash reporting must never mask the original traceback
        sys.stderr.write(tb)
        sys.exit(1)


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
    if args.console or not args.data_dir:
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
    from .platform_detect import detect_os
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

    current_os = detect_os()
    log_success = config.get("log_success_checks", False) or args.verbose
    all_failures = []
    # Bootstrap's own entries (self-bootstrap + user) — written to bootstrap's log
    bootstrap_action_entries = []
    bootstrap_ok_entries = []
    # Display sections: list of (header, action_entries, ok_entries)
    display_sections = []

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

    # Step 2b: Version change detection
    action_entries = []
    ok_entries = []
    if version:
        last_version_file = os.path.join(data_dir, "last_version")
        try:
            with open(last_version_file, "r") as f:
                last_version = f.read().strip()
        except FileNotFoundError:
            last_version = ""
        if last_version and last_version != version:
            action_entries.append(f"updated: {last_version} -> {version}")
        elif not last_version:
            action_entries.append(f"installed: {version}")
        os.makedirs(data_dir, exist_ok=True)
        with open(last_version_file, "w") as f:
            f.write(version)
    bootstrap_action_entries.extend(action_entries)

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
        failures = _process_manifest(
            layered_manifest, current_os, data_dir, plugin_root,
            action_entries, ok_entries, plugin_name="config",
            project_dir=args.project_dir,
        )
        prefixed_action = [f"config: {e}" for e in action_entries]
        prefixed_ok = [f"config: {e}" for e in ok_entries]
        bootstrap_action_entries.extend(prefixed_action)
        bootstrap_ok_entries.extend(prefixed_ok)
        if failures:
            all_failures.extend(failures)

    # Step 3d: Process project_venv from layered manifest (needs --project-dir)
    project_venv_def = layered_manifest.get("project_venv") if layered_manifest else None
    if project_venv_def and args.project_dir:
        pv_action, pv_ok, pv_failures = _process_project_venv(
            project_venv_def, args.project_dir)
        bootstrap_action_entries.extend(f"config: {e}" for e in pv_action)
        bootstrap_ok_entries.extend(f"config: {e}" for e in pv_ok)
        all_failures.extend(pv_failures)

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

    enabled_plugins, cache_changed = list_enabled_plugins(config, registry_path, plugins_dir, enabled_refs)
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
            engine_version=version,
        )

    # Step 4b: Re-scan for plugins installed during Steps 3c/4
    # (e.g. a layered bootstrap.json declared a plugin to install via `claude plugin install`)
    phase2_plugins, phase2_cache_changed = list_enabled_plugins(config, registry_path, plugins_dir, enabled_refs)
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
            engine_version=version,
        )

    # Step 4c: Shared-lib convergence sweep. Every owner has now published (Steps
    # 4 + 4b), so re-link all consumers in one go -- a consumer processed before
    # its owner no longer waits for the next session. Silent in steady state
    # (already-linked consumers report "cached" -> verbose-only); only a genuinely
    # converged or failed link surfaces. See _shared_lib_convergence_sweep.
    sweep_actions, sweep_oks, sweep_failures = _shared_lib_convergence_sweep(
        enabled_plugins + new_plugins, data_dir,
    )
    if sweep_failures:
        all_failures.extend(sweep_failures)
    if sweep_actions or sweep_oks:
        sweep_label = f"{bootstrap_label} shared-libs"
        display_sections.append((sweep_label, sweep_actions, sweep_oks))
        sweep_log = sweep_actions + (sweep_oks if log_success else [])
        if sweep_log and not args.console:
            write_log_block(data_dir, sweep_label, sweep_log, start_time=start_time)

    # Step 4d: Reload/restart advisory. Any plugin that ENTERED the registry during
    # this pass -- a layered `plugins:` install (Step 3c), a per-plugin install, or
    # a script install (Step 4b) -- is not yet loaded by Claude Code (it loaded
    # plugins at session start, before this hook installed them). We detect them by
    # diffing the registry (before Step 3c vs now), NOT Step 4b's new_plugins, which
    # misses layered installs absorbed by Step 4. A plugin merely updated at session
    # start was already loaded by the restart that updated it, so it is not nagged.
    # Toggle off via config "notify_reload_needed".
    action_required = []
    if config.get("notify_reload_needed", True):
        newly_installed = _resolve_newly_installed(
            installed_refs_before, _read_installed_plugins(_registry_for_diff),
        )
        action_required = [a for a in (
            _reload_advice(newly_installed),
            # Bootstrap self-staleness: a newer bootstrap is cached but this session
            # loaded the old one. /reload-plugins won't re-fire its SessionStart pass.
            _bootstrap_stale_advice(version, boot_plugin_name, marketplace_name, prod_registry),
        ) if a]
        # action_required is ALSO passed to emit_success_response so the Claude-facing
        # additionalContext leads with a relay directive (systemMessage isn't reliably
        # shown, so we instruct the session's Claude to tell the user). Still added to
        # the display + log here so console mode and bootstrap.log get it too.
        for advice in action_required:
            advice_label = f"{bootstrap_label} action required"
            display_sections.append((advice_label, [advice], []))
            if not args.console:
                write_log_block(data_dir, advice_label, [advice], start_time=start_time)

    # Step 5: Read shell log entries BEFORE writing any engine entries to the log.
    # Plugin log writes are deferred to step 6 to avoid the bootstrap plugin's
    # ok_entries leaking back through shell_content (its data_dir == engine data_dir).
    if not args.console:
        shell_content = _read_new_log_entries(data_dir, start_time=start_time)
    else:
        shell_content = ""  # Console mode: shell already printed its entries

    # Step 6: Write log entries (bootstrap + plugins) — after reading shell entries
    # Skip in console mode — no file writes.
    # Only include ok_entries when log_success is true — otherwise they leak back
    # through shell_content on the next run (the log reader can't distinguish
    # ok vs action entries, so they bypass the log_success display filter).
    bootstrap_log_entries = bootstrap_action_entries + (bootstrap_ok_entries if log_success else [])
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
        display_lines.append(f"--- {header}: {'; '.join(actions)} ---")

    if args.console:
        # Console mode: plain text to stdout, no JSON
        for line in display_lines:
            print(line)
        if all_failures:
            print(f"\n{bootstrap_label} -> {len(all_failures)} failure(s):")
            for f in all_failures:
                print(f"  - [{f['type']}] {f.get('name', f.get('message', ''))}")
        return

    # Build final display: shell entries + section entries
    parts = []
    if shell_content:
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
        )
        # Clear this project's cooldown stamp so the next SessionStart re-runs
        # bootstrap instead of silently throttling. The shell hook stamps the
        # cooldown optimistically before invoking the engine; on failure we
        # roll that back so out-of-band fixes (user runs winget themselves,
        # restarts their IDE, edits config) are picked up on the next session
        # rather than waiting out the throttle window.
        _clear_project_cooldown(data_dir, args.project_dir)
    else:
        if display_content or action_required:
            emit_success_response(
                display_content, label=bootstrap_label, output_file=output_file,
                action_required=action_required,
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


def _bootstrap_single_plugin(
    plugin_info, current_os, data_dir, all_failures,
    log_success, display_sections, deferred_plugin_logs, args,
    engine_version="",
):
    """Process a single plugin's bootstrap.json manifest.

    Extracted from the Step 4 loop body to allow reuse in Step 4b (Phase 2 re-scan).
    Mutates the shared containers in place (same pattern as the original inline code).

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

    # Per-plugin entry lists (written to plugin's own log)
    plugin_action_entries = []
    plugin_ok_entries = []

    # Version change detection. Skipped for bootstrap itself: its plugin_data_dir
    # IS the engine data_dir, and Step 2b already wrote last_version there with
    # the RUNNING engine's version. When a dev tree runs against a cached
    # registry the two versions differ, and writing the registry version here
    # produced a flip-flopping "updated: X -> Y" entry every pass (B14).
    if plugin_info.version and (
        os.path.normcase(os.path.normpath(plugin_data_dir))
        != os.path.normcase(os.path.normpath(data_dir))
    ):
        last_version_file = os.path.join(plugin_data_dir, "last_version")
        try:
            with open(last_version_file, "r") as f:
                last_version = f.read().strip()
        except FileNotFoundError:
            last_version = ""
        if last_version and last_version != plugin_info.version:
            plugin_action_entries.append(f"updated: {last_version} -> {plugin_info.version}")
        elif not last_version:
            plugin_action_entries.append(f"installed: {plugin_info.version}")
        with open(last_version_file, "w") as f:
            f.write(plugin_info.version)

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
    failures = _process_manifest(
        plugin_manifest, current_os, plugin_data_dir, plugin_info.install_path,
        action_entries, ok_entries, plugin_name=plugin_info.name,
        project_dir=getattr(args, 'project_dir', None),
        project_detected=project_detected,
    )
    plugin_action_entries.extend(action_entries)
    plugin_ok_entries.extend(ok_entries)

    if failures:
        all_failures.extend(failures)

    # Collect plugin log info (deferred — written after reading shell entries)
    plugin_label = f"{plugin_info.name}@{plugin_info.version}" if plugin_info.version else plugin_info.name
    plugin_log_entries = plugin_action_entries + (plugin_ok_entries if log_success else [])
    deferred_plugin_logs.append((plugin_data_dir, plugin_label, plugin_log_entries))

    # Add plugin section to display
    plugin_display_header = f"{plugin_info.marketplace}:{plugin_info.name}@{plugin_info.version}" if plugin_info.marketplace else plugin_label
    display_sections.append((plugin_display_header, list(plugin_action_entries), list(plugin_ok_entries)))


def _shared_lib_convergence_sweep(plugins, data_dir):
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

    Returns ``(actions, oks, failures)`` for the caller to log + display.
    """
    from .shared_lib import link_shared_lib
    from .venv_check import _find_python

    actions, oks, failures = [], [], []
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
                actions.append(entry)
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
    return actions, oks, failures


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
    """User-facing reload/restart nag for plugins that ENTERED the registry during
    this pass, or None when there is nothing to advise.

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
            f"Restart Claude (or your IDE) to start using them."
        )
    return (
        f"bootstrap installed new plugin(s): {names}. "
        f"Run /reload-plugins to start using them."
    )


def _bootstrap_stale_advice(running_version, plugin_name, marketplace_name, registry_path):
    """Restart nag when the registry records a NEWER bootstrap than the one running
    this session, else None.

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
    registry_version = ""
    if isinstance(installs, list) and installs and isinstance(installs[0], dict):
        registry_version = installs[0].get("version", "")
    elif isinstance(installs, dict):
        registry_version = installs.get("version", "")
    if not registry_version:
        return None
    from .marketplace_lifecycle import _version_greater
    if not _version_greater(registry_version, running_version):
        return None
    return (
        f"bootstrap was updated to {registry_version}. "
        f"Restart Claude (or your IDE) to load it."
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


def _process_tool_entry(tool_def, current_os, data_dir, prefix, action_entries,
                        ok_entries, tools_installed, plugin_name):
    """Resolve one tool entry: check -> link-to-PATH -> download -> install.

    Shared by _process_self_setup and _process_manifest (previously two
    near-identical copies). Mutates action_entries / ok_entries / tools_installed
    in place. Returns a failure dict, or None on success.

    Resolution policy:
      - check_tool() resolves via installPath candidates / `check` cmd / which.
      - If resolved but not on PATH, _link_tool_dir_to_path() persists its dir
        (owning the chain; no user "restart" instruction — philosophy P4).
      - On miss: prefer a `download` recipe (our own copy under ~/.local/bin),
        else run the install command. After ANY install attempt we re-check
        regardless of the installer's exit code — installers exit non-zero for
        "already installed / no upgrade" (winget 43), so the re-check, not the
        exit code, decides "is it there now."
    """
    from .tool_check import check_tool
    from . import tool_paths

    name = tool_def["name"]
    install_cmds = tool_def.get("install", {})
    tool_install_path = tool_def.get("installPath")
    check_cmd = tool_def.get("check")
    download_def = _resolve_download_def(tool_def.get("download", {}), current_os)

    result = check_tool(name, install_cmds, current_os,
                        install_path=tool_install_path, check_cmd=check_cmd)

    if result.passed:
        if result.path:
            # data_dir=None -> the canonical bootstrap data dir; tool paths are
            # recorded centrally regardless of which plugin's pass found them.
            tool_paths.record(None, result.subject, result.path)
        _link_tool_dir_to_path(result, prefix, action_entries)
        ok_entries.append(f"{prefix}{result.subject}: ok - {result.message}")
        return None

    # Phase-2 path A: Scoop fulfillment (Windows userspace package manager). A
    # `scoop` key in the resolved download entry means "install via Scoop" rather
    # than a url/sha download. Scoop is provisioned LAZILY -- the first such tool
    # installs it. See bootstrap_lib/scoop.py. (An older engine that predates this
    # branch falls through to the install command, degrading to the legacy path.)
    if download_def and download_def.get("scoop"):
        from .scoop import ensure_scoop, scoop_install
        from .path_repair import repair_path
        pkg = download_def["scoop"]
        es = ensure_scoop()
        if not es.ok:
            action_entries.append(f"{prefix}{name}: scoop unavailable - {es.message}")
            return {"type": "tool", "name": name, "message": es.message,
                    "install_state": "scoop_failed", "install_cmd": None,
                    "plugin": plugin_name}
        if es.message != "already installed":
            action_entries.append(f"{prefix}scoop: {es.message}")
        si = scoop_install(pkg, tool_name=name)
        # Scoop adds ~/scoop/shims to the user PATH on install; reflect that into
        # this already-running process so the re-check can resolve the binary.
        repair_path()
        recheck = check_tool(name, install_cmds, current_os,
                             install_path=tool_install_path, check_cmd=check_cmd)
        if recheck.passed:
            if recheck.path:
                tool_paths.record(None, recheck.subject, recheck.path)
            tools_installed.append((name, f"installed `{pkg}` via scoop"))
            return None
        if si.ok and si.path:
            # Resolvable on disk but not yet by bare name; record the shim path.
            tool_paths.record(None, name, si.path)
            tools_installed.append((name, f"installed `{pkg}` via scoop ({si.path})"))
            return None
        action_entries.append(f"{prefix}{name}: scoop install failed - {si.message}")
        return {"type": "tool", "name": name, "message": si.message,
                "install_state": "scoop_failed", "install_cmd": None,
                "plugin": plugin_name}

    # Phase-2 path B: prefer downloading our own copy to ~/.local/bin over shelling
    # out to a system package manager. See tool-resolution-redesign.md.
    if download_def and download_def.get("url") and download_def.get("sha256"):
        from .downloader import download_and_install
        dl = download_and_install(
            name,
            download_def["url"],
            download_def["sha256"],
            binary_name=download_def.get("binary_name"),
            archive_path=download_def.get("archive_path"),
            archive_type=download_def.get("archive_type"),
        )
        if dl.ok:
            tool_paths.record(None, name, dl.path)
            tools_installed.append((name, f"downloaded to {dl.path}"))
            return None
        action_entries.append(f"{prefix}{name}: download failed - {dl.message}")
        # Fall through to legacy install fallback below.

    # Tool not found — attempt remediation if an install command is available.
    # The "manual" sentinel (dependency-philosophy.md) means there is no
    # unattended installer for this OS: bootstrap verifies the tool resolves on
    # PATH but never tries to install it. Treat it as a manual-attention item —
    # do NOT execute "manual" as a shell command (it just fails with
    # "command not found", surfacing a bogus install_failed every session).
    install_state = "no_install_cmd"
    if result.install_cmd == "manual":
        install_state = "manual_install"
    elif result.install_cmd:
        from .tool_check import run_install
        from .path_repair import repair_path
        ok, _output = run_install(result.install_cmd)
        # Re-check regardless of the installer's exit code: a non-zero exit can
        # mean "already installed / no upgrade available" (winget 43), which is
        # success from our standpoint. repair_path() first so a registry PATH
        # update from the installer is visible to this already-running process.
        repair_path()
        recheck = check_tool(name, install_cmds, current_os,
                             install_path=tool_install_path, check_cmd=check_cmd)
        if recheck.passed:
            if recheck.path:
                tool_paths.record(None, recheck.subject, recheck.path)
            _link_tool_dir_to_path(recheck, prefix, action_entries)
            verb = "via" if ok else "already present after"
            tools_installed.append((result.subject, f"{verb} `{result.install_cmd}`"))
            return None
        # Re-check failed: distinguish "installer ran but we still can't find it"
        # from "installer itself errored".
        install_state = "installed_but_path_stale" if ok else "install_failed"

    if install_state == "installed_but_path_stale":
        action_entries.append(
            f"{prefix}{result.subject}: install succeeded but binary not findable afterward "
            f"(add an installPath hint, or a download recipe to fetch our own copy)"
        )
    elif install_state == "install_failed":
        action_entries.append(f"{prefix}{result.subject}: install command failed - `{result.install_cmd}`")
    elif install_state == "manual_install":
        action_entries.append(
            f"{prefix}{result.subject}: not installed — manual install required "
            f"(no unattended installer for this OS); install it and ensure it's on PATH"
        )
    else:
        action_entries.append(f"{prefix}{result.subject}: FAILED - {result.message}")

    return {
        "type": "tool",
        "name": result.subject,
        "message": result.message,
        "install_state": install_state,
        # manual_install carries no runnable command — null it so the item is
        # classified manual-attention (not fix-all eligible) downstream.
        "install_cmd": None if install_state == "manual_install" else result.install_cmd,
        "plugin": plugin_name,
    }


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

    Unlike the plugin venv (which lives in data_dir), this targets
    <project_dir>/.venv using the project's own pyproject.toml.

    Args:
        venv_def: Dict with optional 'extras' (list) and 'check_imports' (list).
        project_dir: Absolute path to the project root.

    Returns:
        (action_entries, ok_entries, failures) tuple.
    """
    action_entries = []
    ok_entries = []
    failures = []

    # project_dir serves as both data_dir (.venv location) and plugin_root
    # (pyproject.toml location). No env-var export: the project venv belongs
    # to the project, not a plugin.
    _process_venv_def(
        venv_def, project_dir, project_dir, "", "project_venv",
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
    - ``fail(msg, **failure)`` -> action entry AND fix-all failure dict in a
      single call, so a registered failure can never be invisible to the user.

    ``config`` / ``variables`` load lazily on first use; phases before
    ini_settings don't need them.
    """

    _UNSET = object()

    def __init__(self, manifest, current_os, data_dir, plugin_root,
                 action_entries, ok_entries, plugin_name, project_dir,
                 project_detected):
        self.manifest = manifest
        self.current_os = current_os
        self.data_dir = data_dir
        self.plugin_root = plugin_root
        self.action_entries = action_entries
        self.ok_entries = ok_entries
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

    def action(self, message):
        self.action_entries.append(f"{self.prefix}{message}")

    def fail(self, entry, **failure):
        """Append `entry` as an action line AND register `failure` for fix-all.

        (`entry` deliberately doesn't collide with the failure-dict `message`
        kwarg most callers pass.)
        """
        self.action(entry)
        failure.setdefault("plugin", self.plugin_name)
        self.failures.append(failure)


def _phase_tools(ctx):
    """tools: resolve -> link-to-PATH -> download -> install.

    Consolidates successful installs into one line; failures stay per-line.
    """
    tools_installed = []
    for tool_def in ctx.manifest.get("tools", []):
        failure = _process_tool_entry(
            tool_def, ctx.current_os, ctx.data_dir, ctx.prefix,
            ctx.action_entries, ctx.ok_entries, tools_installed,
            plugin_name=ctx.plugin_name,
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
            ctx.action(f"font {name}: not installed and no download declared for {ctx.current_os}")
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
                ctx.action(f"marketplace {mkt_name}: alwaysUpdate ignored while pinned")

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
            inst = install_plugin(plugin_ref, scope=desired_scope)
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
                # *why* the re-install happened; consolidate the action.
                ctx.action(f"plugin {plugin_ref}: {scope_check.message}")
                reinst = install_plugin(plugin_ref, scope=desired_scope)
                if reinst.passed:
                    _bucket(plugins_re_installed, plugin_ref, f"at {desired_scope} scope")
                else:
                    ctx.fail(
                        f"plugin {plugin_ref}: scope install failed - {reinst.message}",
                        type="plugin", ref=plugin_ref,
                        message=f"scope install failed: {reinst.message}",
                    )
                    continue

            # Sync installed_plugins.json scope to match desired scope.
            # CLI commands (update, uninstall) read scope from this file and
            # fail if it's stale. Fix the data before running those commands.
            ensure_registry_scope(plugin_ref, desired_scope)

        if install_mode == "manual":
            # Manual-install plugins: only manage version updates. Skip
            # enable/disable side effects so the user's choices are respected.
            if install_result.passed:
                ver_result = check_plugin_version(plugin_ref)
                if not ver_result.up_to_date:
                    upd_result = update_plugin(plugin_ref, scope=desired_scope)
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
                    upd_result = update_plugin(plugin_ref, scope=desired_scope)
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
                    upd_result = update_plugin(plugin_ref, scope=desired_scope)
                    if upd_result.passed:
                        recheck = check_plugin_min_version(plugin_ref, min_version)
                        if recheck.up_to_date:
                            _bucket(plugins_updated, plugin_ref, f"{min_result.installed_version} -> {recheck.installed_version}, satisfies >= {min_version}")
                        else:
                            ctx.fail(
                                f"plugin {plugin_ref}: installed {recheck.installed_version} < required {min_version}, update failed to satisfy constraint{pin_note}",
                                type="plugin", ref=plugin_ref,
                                message=f"min_version {min_version} not satisfied (installed {recheck.installed_version}){pin_note}",
                            )
                    else:
                        ctx.fail(
                            f"plugin {plugin_ref}: installed {min_result.installed_version} < required {min_version}, update failed - {upd_result.message}{pin_note}",
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
    action_entries; failed -> action_entries + failures. Runs after the venv
    handler so a consumer's own .venv already exists as the .pth target.
    """
    from .shared_lib import sync_shared_lib, link_shared_lib, find_standalone_python
    from .venv_check import _find_python

    shared_root = os.path.join(os.path.dirname(ctx.data_dir), "_shared_libs")

    def _log_shared(result):
        if result.status in ("cached", "skipped"):
            ctx.ok(f"shared-lib {result.name}: {result.message}")
        elif result.status in ("published", "linked"):
            ctx.action(f"shared-lib {result.name}: {result.message}")
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
#   - tools before venv: uv may be installed by the tools phase.
#   - venv before shared_libs: a consumer link targets this plugin's .venv.
#   - marketplaces before json_entries: the marketplace must be cloned before
#     fields like autoUpdate are merged into known_marketplaces.json.
#   - plugins before ini/json: installs rewrite the plugin registry first.
#   - script last: it may build on everything the manifest set up.
_MANIFEST_PHASES = (
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


def _process_manifest(manifest, current_os, data_dir, plugin_root, action_entries, ok_entries, plugin_name="bootstrap", project_dir=None, project_detected=True):
    """Process a single plugin's bootstrap manifest. Returns list of failures.

    Dispatches to one handler per manifest key via _MANIFEST_PHASES. Entries
    are split into two lists:
    - action_entries: actions performed, failures, conditions not met (always displayed)
    - ok_entries: checks that passed (never displayed; written to log file when log_success is true)

    When project_detected is False, project-scoped primitives (ini_settings) are skipped.
    """
    ctx = _ManifestContext(
        manifest, current_os, data_dir, plugin_root,
        action_entries, ok_entries, plugin_name, project_dir, project_detected,
    )
    for keys, handler in _MANIFEST_PHASES:
        if any(manifest.get(k) for k in keys):
            handler(ctx)
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

    Mirrors the path construction in hooks/sessionstart/session-bootstrap.sh:
    <data_dir>/cooldowns/last_run_epoch.<sha1(project_dir)>, with the same
    "_global_" fallback when no project_dir is available. Silent on any I/O
    error -- a stale stamp at worst delays the next re-run by the throttle
    window; it never blocks remediation.
    """
    import hashlib
    key = "_global_"
    if project_dir:
        try:
            key = hashlib.sha1(project_dir.encode("utf-8")).hexdigest()
        except Exception:
            pass
    stamp = os.path.join(data_dir, "cooldowns", f"last_run_epoch.{key}")
    try:
        os.remove(stamp)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _restamp_project_cooldown(data_dir, project_dir):
    """Refresh this project's cooldown stamp at the end of a clean pass.

    Same path construction as _clear_project_cooldown. Only refreshes an
    EXISTING stamp — creating it is the shell hook's job (so console runs,
    tests, and direct engine invocations never plant cooldowns), and the
    failure path clears it instead. Content matches the shell's format: the
    current epoch as text (the shell reads it for the age check; the
    registry-bypass compares mtimes). Silent on I/O errors — at worst the
    registry-mtime bypass re-arms one extra pass.
    """
    import hashlib
    import time
    key = "_global_"
    if project_dir:
        try:
            key = hashlib.sha1(project_dir.encode("utf-8")).hexdigest()
        except Exception:
            pass
    stamp = os.path.join(data_dir, "cooldowns", f"last_run_epoch.{key}")
    if not os.path.isfile(stamp):
        return
    try:
        with open(stamp, "w") as f:
            f.write(str(int(time.time())))
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


def emit_success_response(log_content, label="bootstrap", output_file=None, action_required=None):
    """Emit hook JSON showing bootstrap log to user and agent.

    ``action_required`` is a list of user-facing instructions (reload/restart
    nags). They're already in ``log_content``, but the user does NOT reliably see
    systemMessage, so we ALSO lead the Claude-facing additionalContext with an
    explicit directive telling the session's Claude to relay them to the user --
    the one channel confirmed to always reach Claude. See plugin-reload-lifecycle.md.
    """
    action_required = action_required or []
    if output_file:
        # Background mode: consumed by UserPromptSubmit hook.
        # `systemMessage` is user-facing, `additionalContext` is Claude-facing.
        body = f"{label} -> bootstrap complete:\n{log_content}"
        add_ctx = body
        if action_required:
            bullets = "\n".join(f"- {a}" for a in action_required)
            add_ctx = (
                "ACTION REQUIRED — bootstrap needs the user to do something, and they "
                "will NOT see it unless you tell them. Surface this to the user now, in "
                f"your reply:\n{bullets}\n\n{body}"
            )
        response = {
            "continue": True,
            "suppressOutput": False,
            "systemMessage": body,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": add_ctx,
            },
        }
        _write_atomic(output_file, json.dumps(response))
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


# Failure types fix-all can deterministically remediate without user input.
# Anything else (config items asking for API keys, python_stub admin
# elevation, parse errors in user-edited files, generic custom failures)
# is manual-only — Claude can guide but can't run a one-shot command.
_AUTO_FIXABLE_TYPES = frozenset({
    "path", "venv", "git_dep", "ini", "pypi",
    "json", "marketplace", "plugin", "sync_to_data",
})


def _is_auto_fixable(failure):
    t = failure.get("type")
    if t == "tool":
        # Tools are fix-all eligible only when we know how to install them
        # AND the install hasn't already run successfully. If install_state
        # is "installed_but_path_stale", rerunning the install just produces
        # "already installed" — fix-all can't help; it's a bootstrap bug.
        # installed_but_path_stale: reinstall just says "already installed".
        # manual_install: there's no unattended installer — only the user can act.
        if failure.get("install_state") in ("installed_but_path_stale", "manual_install"):
            return False
        return bool(failure.get("install_cmd"))
    return t in _AUTO_FIXABLE_TYPES


def _format_indexes(idxs):
    """Render a sorted index list as '#1, #2, #4' for footer copy."""
    return ", ".join(f"#{i}" for i in idxs)


def emit_failure_response(failures, current_os, log_content, label="bootstrap", output_file=None, persistent_output_file=None):
    """Emit hook JSON with fix-all directives to stdout or file.

    If persistent_output_file is provided AND any failure is marked
    `persist_across_sessions`, the same JSON is also written to that path so
    subsequent sessions can re-prime bootstrap_display.pending from it.
    """
    agent_lines = [f"{label} -> Setup issues found. Fix in order:\n"]

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

    # Classify each item as fix-all eligible vs manual-only so the footer
    # matches reality. Three cases: all auto, mixed, all manual.
    auto_idxs = [i for i, f in enumerate(failures, 1) if _is_auto_fixable(f)]
    manual_idxs = [i for i, f in enumerate(failures, 1) if not _is_auto_fixable(f)]

    if auto_idxs and not manual_idxs:
        agent_trailer = "\nAll items above are fix-all eligible. Run 'fix-all' to resolve them, or type 'fixed' after manual fixes."
        user_msg = "Tell Claude 'fix-all' to auto-fix, or 'fixed' after manual fixes."
    elif auto_idxs and manual_idxs:
        agent_trailer = (
            f"\nRun 'fix-all' to auto-resolve items {_format_indexes(auto_idxs)}. "
            f"Items {_format_indexes(manual_idxs)} need manual attention — guide the user "
            f"through the steps above. Type 'fixed' once everything is resolved."
        )
        user_msg = (
            f"Tell Claude 'fix-all' to auto-fix items {_format_indexes(auto_idxs)}; "
            f"items {_format_indexes(manual_idxs)} need manual attention. "
            f"Type 'fixed' once done."
        )
    else:
        agent_trailer = "\nNone of these are fix-all eligible — guide the user through the steps above. Type 'fixed' once resolved."
        user_msg = "These issues need manual attention — work through them with Claude. Type 'fixed' once resolved."

    agent_lines.append(agent_trailer)
    agent_msg = "\n".join(agent_lines)

    # Special-case: if all failures are python_stub, emit a focused, user-friendly
    # response (no log_content noise, no "fix-all" boilerplate — the user can't
    # tell Claude to fix this since it requires admin elevation on their machine).
    python_stub_failures = [f for f in failures if f["type"] == "python_stub"]
    only_python_stub = bool(python_stub_failures) and len(python_stub_failures) == len(failures)

    if only_python_stub:
        # Use the first python_stub failure's structured messages.
        ps = python_stub_failures[0]
        focused_user_msg = ps.get("user_msg", ps.get("message", ""))
        focused_agent_msg = ps.get("agent_msg", ps.get("message", ""))
        if output_file:
            response = {
                "continue": True,
                "suppressOutput": False,
                "systemMessage": f"{label}: {focused_user_msg}",
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": f"{label} -> {focused_agent_msg}",
                },
            }
            _write_atomic(output_file, json.dumps(response))
            if persistent_output_file:
                _write_atomic(persistent_output_file, json.dumps(response))
        else:
            response = {
                "continue": True,
                "suppressOutput": False,
                "systemMessage": f"{label}: {focused_user_msg}",
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": f"{label} -> {focused_agent_msg}",
                },
            }
            print(json.dumps(response))
        return

    # General path: mixed or non-python_stub failures.
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


if __name__ == "__main__":
    main()
