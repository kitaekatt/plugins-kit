"""Contract tests for the commands registered in unreal-kit/hooks/hooks.json."""

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "unreal-kit"
_HOOKS = _PLUGIN_DIR / "hooks" / "hooks.json"


def _registered_commands() -> list[str]:
    payload = json.loads(_HOOKS.read_text(encoding="utf-8"))
    return [
        hook["command"]
        for entries in payload["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    ]


def _write_sentinel(root: Path, relative_script: str, record: Path) -> Path:
    script = root / relative_script
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$#\" > \"$SENTINEL_RECORD\"\n"
        "if [ \"$#\" -gt 0 ]; then printf '%s\\n' \"$1\" >> \"$SENTINEL_RECORD\"; fi\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.mark.parametrize("command", _registered_commands())
def test_registered_command_preserves_plugin_root_as_one_argument(tmp_path, command):
    """Literal root substitution must survive a plugin install path with spaces."""
    plugin_root = tmp_path / "plugin root with spaces"
    record = tmp_path / "record"
    relative = command.split("${CLAUDE_PLUGIN_ROOT}", 1)[1].split()[0].strip('"').lstrip("/")
    _write_sentinel(plugin_root, relative, record)
    expanded = command.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
    argv = shlex.split(expanded)

    subprocess.run(argv, check=True, capture_output=True, text=True, env={**os.environ, "SENTINEL_RECORD": str(record)})
    lines = record.read_text(encoding="utf-8").splitlines()
    assert lines == ["0"]


@pytest.mark.parametrize("command", _registered_commands())
def test_registered_command_expands_environment_root_as_one_argument(tmp_path, command):
    """The documented shell expansion must also preserve a spaced root."""
    plugin_root = tmp_path / "plugin root with spaces"
    record = tmp_path / "record"
    relative = command.split("${CLAUDE_PLUGIN_ROOT}", 1)[1].split()[0].strip('"').lstrip("/")
    _write_sentinel(plugin_root, relative, record)

    subprocess.run(
        ["/bin/sh", "-c", command],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(plugin_root), "SENTINEL_RECORD": str(record)},
    )
    assert record.read_text(encoding="utf-8").splitlines() == ["0"]
