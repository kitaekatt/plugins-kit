"""Apply user/project bootstrap declarations without the plugin lifecycle.

The shared manifest handlers own provisioning semantics. This entry point
selects only the four current user/project layers, with no registry discovery,
legacy layer, env.json pass, self-setup, or automatic project operations.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import engine, interpreter_env
from .records import PassRecorder, RecordingList


@dataclass
class LayeredBootstrapResult:
    actions: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    #: The resolved bootstrap profile for this run, or None before it has
    #: been computed (only when a manifest parse error short-circuits before
    #: profile resolution can run -- resolve_layers is called even then, so
    #: in practice this is only None if the caller never assigns it).
    profile_status: str | None = None


def run_layered_bootstrap(
    project_dir: Path,
    plugin_root: Path,
    data_dir: Path,
    current_os: str,
    recorder: PassRecorder | None = None,
) -> LayeredBootstrapResult:
    """Apply only declared requirements; the caller owns locking and output."""
    result = LayeredBootstrapResult(
        actions=RecordingList(recorder, "action", section="config"),
        checks=RecordingList(recorder, "ok", section="config"),
        details=RecordingList(recorder, "quiet", section="config"),
    )
    # No data_dir: the loader's deprecated user-bootstrap.json candidate is
    # deliberately excluded from a terminal run.
    manifest, errors, profile_state = engine._load_layered_manifests_ex(str(project_dir))
    for error in errors:
        result.failures.append({"type": "manifest_parse", **error,
                                "message": error["error"]})
        result.actions.append(f"{error['path']}: PARSE FAILED - {error['error']}")
    # A broken override must not allow lower-priority requirements to run.
    # Leaves profile_status at its default (None): a parse error keeps this
    # early return exactly as it was before profiles existed, rather than
    # reporting "unselected" over a manifest that never fully loaded.
    if errors:
        return result

    result.profile_status = profile_state.status
    # `bootstrap run` never prompts (there is no session to ask in): it only
    # surfaces what resolve_layers already decided. state.errors is a bad
    # `profiles` declaration (not a JSON parse error, handled above) and is a
    # failure here too, same as the SessionStart lifecycle.
    for perr in profile_state.errors:
        result.failures.append({
            "type": "profile_invalid",
            "message": perr,
            "agent_msg": f"A declared bootstrap profile is invalid: {perr}.",
            "plugin": "bootstrap",
        })
        result.actions.append(f"profile: {perr}")
    for pwarn in profile_state.warnings:
        result.actions.append(f"profile: {pwarn}")
    if profile_state.status == "selected":
        result.checks.append(
            "profile: applied '%s' (chain: %s)" % (
                profile_state.selected, " -> ".join(profile_state.chain)))
    elif profile_state.status in ("unselected", "unknown"):
        result.actions.append(
            "profile: none selected -- run 'bootstrap profile'")

    # Reuse provisioned YAML/config dependencies; this only adds existing
    # site-packages paths and performs no runtime installation or repair.
    engine._activate_bootstrap_venv(str(data_dir))
    # This path never enters engine._main, so it resets the interpreter names
    # itself, before any manifest command runs (see interpreter_env).
    engine_python = interpreter_env.begin_pass()
    result.checks.append(f"python: {interpreter_env.ENGINE_VAR}={engine_python}")
    # The project default by the normative rule, WITHOUT the record: the
    # record key is the hook's hash of Claude's cwd, which this terminal cwd
    # cannot reproduce. No record write, no persistence, no shell hook here.
    # A project whose PROJECT layer declares "project_python": false gets no
    # project name; the key in a user layer is ignored (engine
    # _project_python_opt_out).
    project_python_source = None
    if project_dir:
        user_layers, project_layers = engine._interpreter_env_layers(
            str(project_dir), profile_state)
        opted_out = engine._project_python_opt_out(
            user_layers, project_layers, result.details)
        pp_value, project_python_source = interpreter_env.export_project_default(
            str(project_dir),
            subdir=engine._project_venv_subdir(str(project_dir), manifest),
            opted_out=opted_out,
            record_dir=None,
        )
        result.checks.append(
            engine._project_python_entry(pp_value, project_python_source))
    if manifest:
        result.failures.extend(engine._process_manifest(
            manifest, current_os, str(data_dir), str(plugin_root),
            result.actions, result.checks, plugin_name="config",
            project_dir=str(project_dir), quiet_entries=result.details,
        ))
    for key, handler in (("project_venv", engine._process_project_venv),
                         ("project_npm", engine._process_project_npm)):
        if manifest.get(key):
            actions, checks, failures = handler(
                manifest[key], str(project_dir), quiet_entries=result.details,
            )
            result.actions.extend(actions)
            result.checks.extend(checks)
            result.failures.extend(failures)
            if key == "project_venv" and not failures:
                if project_python_source in engine._PROJECT_PYTHON_OUTRANKS_VENV:
                    result.checks.append(
                        engine._kept_over_venv_entry(project_python_source))
                    continue
                project_python = engine._export_project_python(
                    manifest[key], str(project_dir))
                if project_python:
                    result.checks.append(
                        f"project_venv: exported "
                        f"{interpreter_env.PROJECT_VAR}={project_python} (process)")
    # SessionStart's default-on link operation is not an implicit CLI task.
    if "agent_skills_link" in manifest:
        actions, checks, failures = engine._run_agent_skills_link_check(
            str(project_dir), manifest["agent_skills_link"],
        )
        result.actions.extend(actions)
        result.checks.extend(checks)
        result.failures.extend(failures)
    return result
