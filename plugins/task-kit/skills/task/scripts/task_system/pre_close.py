"""Optional repository command gate for task close and archive.

The repository's .claude/bootstrap.json may set task_kit.pre_close to an argv
list or an object with argv and required_paths. Whole arguments {task},
{task_id}, and {repo} name the canonical ref, folder slug, and absolute project
root. No shell or cross-plugin import runs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


class PreCloseError(ValueError):
    """The configured command refused closure or could not establish safety."""


def check_pre_close(project_root: Path, canonical: str, task_id: str) -> None:
    """Run the configured checker before mutation; absent capability is disclosed."""
    root = project_root.resolve()
    config_path = root / ".claude/bootstrap.json"
    if not config_path.exists():
        return
    try:
        config: Any = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreCloseError(f"pre-close: cannot read {config_path}: {exc}") from exc
    if not isinstance(config, dict) or not isinstance(config.get("task_kit", {}), dict):
        raise PreCloseError(f"pre-close: {config_path}: task_kit must be an object")
    section = config.get("task_kit", {})
    if "pre_close" not in section:
        return
    command = section["pre_close"]
    required_paths: Any = []
    if isinstance(command, dict):
        unknown = set(command) - {"argv", "required_paths"}
        if unknown:
            raise PreCloseError(f"pre-close: {config_path}: unknown task_kit.pre_close keys: {sorted(unknown)}")
        required_paths = command.get("required_paths", [])
        command = command.get("argv")
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in command):
        raise PreCloseError(f"pre-close: {config_path}: task_kit.pre_close must be a nonempty argv string list")
    if not isinstance(required_paths, list) or not all(
            isinstance(path, str) and path and "\0" not in path and Path(path).is_absolute() for path in required_paths):
        raise PreCloseError(f"pre-close: {config_path}: task_kit.pre_close.required_paths must be a list of absolute literal paths")
    for name in required_paths:
        try:
            Path(name).stat()
        except FileNotFoundError:
            print(f"task-kit pre-close: required path is absent: {name}; check omitted", file=sys.stderr)
            return
        except OSError as exc:
            raise PreCloseError(f"pre-close: cannot inspect required path {name}: {exc}") from exc
    substitutions = {"{task}": canonical, "{task_id}": task_id, "{repo}": str(root)}
    argv = [substitutions.get(arg, arg) for arg in command]
    try:
        result = subprocess.run(argv, cwd=root, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", timeout=120, check=False)
    except FileNotFoundError:
        print(f"task-kit pre-close: configured executable is absent: {argv[0]}; check omitted", file=sys.stderr)
        return
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError) as exc:
        raise PreCloseError(f"pre-close: checker failed for {canonical}: {exc}") from exc
    if result.returncode:
        raise PreCloseError(f"pre-close: checker exited {result.returncode} for {canonical}: {result.stderr.strip() or result.stdout.strip()}")
    try:
        response: Any = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PreCloseError(f"pre-close: checker did not return JSON for {canonical}: {exc}") from exc
    if (not isinstance(response, dict) or not isinstance(response.get("allowed"), bool)
            or not isinstance(response.get("parked"), list)
            or not all(isinstance(entry, dict) for entry in response["parked"])):
        raise PreCloseError(f"pre-close: checker must return {{allowed: boolean, parked: [objects]}} for {canonical}")
    if not response["allowed"] or response["parked"]:
        raise PreCloseError(f"pre-close: {canonical} is blocked: {json.dumps(response)}")
