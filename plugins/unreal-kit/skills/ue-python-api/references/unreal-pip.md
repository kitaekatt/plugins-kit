# unreal-pip - UE Package Manager

**Repo:** https://github.com/hannesdelbeke/unreal-pip
**Location:** `<data_dir>/github/unreal-pip` (cloned by bootstrap)

Package manager for installing Python packages into UE's embedded Python environment.
UE's embedded Python has no pip by default. unreal-pip bridges that gap.

## How it works

1. Finds UE's Python interpreter via `unreal.get_interpreter_executable_path()`
2. Determines the correct site-packages directory in the engine installation
3. Runs `pip install --target <site-packages>` using UE's own interpreter
4. Checks `pkg_resources.working_set` to skip already-installed packages

## Key functions

```python
import unreal_pip

# Install packages (skips already-installed)
unreal_pip.install(['pyyaml', 'numpy'])

# Uninstall packages
unreal_pip.uninstall(['numpy'])

# Low-level pip command
unreal_pip._pip_cmd(command='list')
```

## Where packages go

Packages install to:
```
<EngineDir>/Binaries/ThirdParty/Python3/Win64/Lib/site-packages/
```
This is UE's engine-level site-packages, so installed packages persist across projects
and editor restarts.

## Bootstrap pattern

Scripts don't call unreal_pip directly. Instead, they use the bootstrap helper:

```python
import os
import sys

data_root = os.environ.get(
    "CLAUDE_BOOTSTRAP_DATA_ROOT",
    os.path.expanduser("~/.claude/plugins/data"),
)
plugin_data = os.path.join(data_root, "plugins-kit", "unreal-kit")
sys.path.insert(0, os.path.join(plugin_data, "lib"))
sys.path.insert(0, os.path.join(plugin_data, "github", "unreal-pip"))
from bootstrap import ensure_dependencies
ensure_dependencies()
```

This reads `lib/requirements.yaml` from the data directory, checks what is
installed, and uses unreal_pip to install anything missing. The bootstrap
parser does not need pyyaml itself; it uses a small line parser while resolving
that dependency.

For the full bootstrapping architecture, see `references/script-bootstrap.md`.

## Platform note

The current implementation uses `subprocess.STARTUPINFO` which is Windows-specific.
This matches our current setup (Windows dev machine with UE Editor).
