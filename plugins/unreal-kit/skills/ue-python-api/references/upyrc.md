# upyrc — Remote Execution

**Repo:** https://github.com/cgtoolbox/UnrealRemoteControlWrapper
**PyPI:** https://pypi.org/project/upyrc/
**Location:** Host-side dependency (system Python, not UE's Python)
**Status:** Integrated into `ue_runner.py`

Python wrapper around UE's built-in Python Remote Execution protocol (UDP multicast).
Sends Python code to a running UE Editor for execution — no copy-paste needed.

**Key insight:** This uses UE's **Python Remote Execution** (part of Python Editor Script Plugin),
NOT the Web Remote Control plugin. No additional plugins needed — just enable
"Remote Execution" in Editor Preferences -> Plugins -> Python.

## How it works

1. Creates a UDP multicast connection (default `239.0.0.1:6766`)
2. Sends Python code to the editor's embedded Python interpreter
3. Editor executes the code and sends results back via the same channel

## Key API

```python
from upyrc import upyre

# Config from .uproject (reads Python plugin settings)
config = upyre.RemoteExecutionConfig.from_uproject_path(r"path/to/project.uproject")

# Or manual config
config = upyre.RemoteExecutionConfig(
    multicast_group=("239.0.0.1", 6766),
    multicast_bind_address="0.0.0.0",
)

# Execute
with upyre.PythonRemoteConnection(config) as conn:
    result = conn.execute_python_command(
        "unreal.log('hello')",
        exec_type=upyre.ExecTypes.EXECUTE_FILE,
        raise_exc=True,
    )
```

## Integration with ue_runner.py

The terminal runner (`scripts/ue_runner.py`) uses upyrc as its primary execution path.
If the editor isn't responding, it falls back to the headless commandlet.

Host-side install: the bootstrap plugin provisions upyrc (with pyyaml) into the
unreal-kit plugin venv at session start -- no manual pip install. The runner
re-execs into that venv, so any invoking interpreter works.
