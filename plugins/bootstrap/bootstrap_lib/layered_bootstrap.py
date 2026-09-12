"""Apply user/project bootstrap declarations without the plugin lifecycle.

The shared manifest handlers own provisioning semantics. This entry point
selects only the four current user/project layers, with no registry discovery,
legacy layer, env.json pass, self-setup, or automatic project operations.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import engine
from .records import PassRecorder, RecordingList


@dataclass
class LayeredBootstrapResult:
    actions: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)


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
    manifest, errors = engine._load_layered_manifests(str(project_dir))
    for error in errors:
        result.failures.append({"type": "manifest_parse", **error,
                                "message": error["error"]})
        result.actions.append(f"{error['path']}: PARSE FAILED - {error['error']}")
    # A broken override must not allow lower-priority requirements to run.
    if errors:
        return result

    # Reuse provisioned YAML/config dependencies; this only adds existing
    # site-packages paths and performs no runtime installation or repair.
    engine._activate_bootstrap_venv(str(data_dir))
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
    # SessionStart's default-on link operation is not an implicit CLI task.
    if "agent_skills_link" in manifest:
        actions, checks, failures = engine._run_agent_skills_link_check(
            str(project_dir), manifest["agent_skills_link"],
        )
        result.actions.extend(actions)
        result.checks.extend(checks)
        result.failures.extend(failures)
    return result
