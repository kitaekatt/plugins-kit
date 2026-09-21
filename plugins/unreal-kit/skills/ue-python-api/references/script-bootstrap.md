# Script Bootstrap (UE-Side Dependencies)

How the ue-python-api skill manages dependencies inside Unreal Engine's embedded Python, where the session bootstrap's venv isn't available.

## The Problem

Scripts running inside UE Editor need packages like `pyyaml`, but UE's embedded Python doesn't share the host's venv. Meanwhile, the host-side runner needs `upyrc` and `pyyaml` to function. These are two separate Python environments with different dependency management.

## Two Dependency Sets

| Set | Manifest | Runtime | Manager | Install target |
|-----|----------|---------|---------|----------------|
| UE-side | `requirements.yaml` | UE's embedded Python | `bootstrap.py` + `unreal_pip.py` | Engine site-packages |
| Host-side | `pyproject.toml` | Bootstrap-selected plugin interpreter | Session bootstrap | Plugin data venv |

## UE-Side Bootstrap (`lib/bootstrap.py`)

Scripts call `ensure_dependencies()` at the top:

```python
import sys, os
sys.path.insert(0, os.path.expanduser('~/.claude/plugins/data/plugins-kit/unreal-kit/lib'))
sys.path.insert(0, os.path.expanduser('~/.claude/plugins/data/plugins-kit/unreal-kit/github/unreal-pip'))
from bootstrap import ensure_dependencies
ensure_dependencies()

import yaml  # now available
```

The function:
1. Reads `requirements.yaml` using a **hand-rolled YAML parser** (not pyyaml — since pyyaml is itself a dependency being installed)
2. Checks installed packages via `importlib.metadata.distributions()`
3. Installs missing packages via `unreal_pip.install()`, which shells out to pip using UE's embedded Python interpreter
4. Targets UE's own site-packages: `Engine/Binaries/ThirdParty/Python3/Win64/Lib/site-packages`
5. Invalidates import caches so new packages are immediately importable

## Host-Side Wrappers

The `.cmd` entry points handle host-side dependencies:

For a host-side invocation from a POSIX shell, use the bootstrap-selected
interpreter expression. It fails with a version diagnosis when the bootstrap
variables are unavailable:

```
"${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}}" "${CLAUDE_PLUGIN_ROOT}/skills/ue-python-api/scripts/ue_runner.py" <script>.py
```

- **`ue-runner.cmd`**: Starts with the deterministic standalone interpreter, accepts a validated `BOOTSTRAP_PYTHON` fallback, and then checks the bootstrap-provisioned plugin venv (`~/.claude/plugins/data/plugins-kit/unreal-kit/.venv/Scripts/python.exe`). The runner then re-execs into that venv via `bootstrap_guard.reexec_under_plugin_venv`, where upyrc and pyyaml are available. If no bootstrap interpreter variable is present, the command prints the required version diagnosis.

## Stdlib-Only Constraint

Three modules include hand-rolled minimal YAML parsers to avoid needing pyyaml before it's installed:

| Module | Where it runs | Why it can't use pyyaml |
|--------|--------------|------------------------|
| `lib/bootstrap.py` | UE Editor | pyyaml is the dependency being installed |
| `lib/ue_runner_config.py` | Host Python | May run before venv exists |

This duplication is intentional — each module must function independently during the bootstrapping phase when no external packages are guaranteed to exist.

## Config Resolution

The runner loads configuration through a layered system:

```
CLI args  >  project config  >  skill config  >  hardcoded defaults
              (~/.claude/.local-data/skills/     (ue_runner_config.yaml)
               ue-python-api/project.yaml)
```

The bootstrap engine's `project_config` primitive writes the project config during session start. The config includes `engine_dir` and `uproject` paths needed by both the remote executor and the commandlet fallback.

## Interaction Flow

The session bootstrap ensures system tools and the host-side venv are ready. The script bootstrap handles UE-side dependencies that can only be resolved inside the editor.

```
Session Start
    |
    v
Session Bootstrap (bash hook)
    Verifies: git, uv, PATH entries
    Creates: host-side venv (upyrc, pyyaml)
    Clones: git dependencies
    |
    v
Claude Code session active
    |
    v
User runs a UE Python script (via ue-runner)
    |
    v
ue_runner.py loads config (fallback YAML parser if needed)
    |
    v
Script sent to UE Editor (remote or commandlet)
    |
    v
Script Bootstrap (inside UE Python)
    ensure_dependencies() installs to Engine site-packages
    |
    v
Script executes with all dependencies available
```
