"""The clauder `--relay` option: start claude with the llm-kit relay channel loaded.

`clauder --relay [claude flags]` adds three flags to the claude command line and
two variables to its environment, so the session receives messages pushed by the
llm-kit relay channel server (plugins/llm-kit/channel) and can answer them
without a permission dialog:

    --mcp-config <llm-kit>/channel/mcp.json
    --dangerously-load-development-channels server:relay
    --allowedTools mcp__relay__reply
    BOOTSTRAP_PYTHON, LLM_KIT_CHANNEL_SCRIPT   (the two names mcp.json expands)

Cross-plugin edge to llm-kit (plugins/CLAUDE.md, "Optional use of another
plugin"): OPTIONAL, branch REFUSE. clauder restarts sessions without llm-kit,
so llm-kit is not in plugin.json `dependencies`. A session that was asked to
carry the relay but does not would be read as one that does, so with no usable
llm-kit the launch is refused with a diagnosis, and nothing is substituted. The
absent and the too-old states name different `claude plugin` commands.

Off by default (owner requirement: no open relay on a machine by default). The
relay must be turned on in the user's own settings file, llm-kit's relay.yaml
(top-level `enabled: true`). `relay_launch` asks the channel script it resolved
(`opt_in_refusal` in relay_channel.py, which reads that file through llm-kit's
hooks/direct_relay.py) and refuses `--relay`, naming the file and the key, when
the relay is not on. A relay_channel.py without `opt_in_refusal` predates the
opt-in and is refused with the update command; nothing is substituted. The
channel server makes the same check when it starts.

Where llm-kit's files are found (first match decides; a match that fails is an
error, never a reason to look further):

1. LLM_KIT_CHANNEL_SCRIPT, when the caller sets it: an absolute path to
   relay_channel.py. mcp.json is read beside it.
2. Under claudx (CLAUDE_PLUGIN_TEST): the llm-kit directory listed in
   CLAUDE_PLUGIN_TEST_DIRS, the working copy the session loads. The plugin
   cache holds the INSTALLED release there, so it is never consulted.
3. Otherwise: the highest version of llm-kit in the plugin cache,
   ~/.claude/plugins/cache/plugins-kit/llm-kit/<version>/.

Stdlib only; written for Python 3.8 and later (the channel script needs 3.9, see
`resolve_python`).
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

SERVER_NAME = "relay"  # the server name in llm-kit's channel/mcp.json
CHANNEL_SCRIPT_ENV = "LLM_KIT_CHANNEL_SCRIPT"  # names that mcp.json expands
PYTHON_ENV = "BOOTSTRAP_PYTHON"
OWNER = "llm-kit"
MARKETPLACE = "plugins-kit"
CHANNEL_DIR = "channel"
SCRIPT_NAME = "relay_channel.py"
MCP_NAME = "mcp.json"
MIN_PYTHON = (3, 9)  # relay_channel.py is written for Python 3.9 and later

_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")
_INSTALL = "claude plugin install %s@%s" % (OWNER, MARKETPLACE)
_UPDATE = "claude plugin update %s@%s" % (OWNER, MARKETPLACE)


class RelayError(Exception):
    """--relay was asked for and cannot be carried out (the message says why)."""


@dataclass(frozen=True)
class Channel:
    script: str  # absolute path of relay_channel.py
    mcp_config: str  # absolute path of mcp.json


@dataclass(frozen=True)
class RelayLaunch:
    args: Tuple[str, ...]  # claude flags to add
    env: Dict[str, str]  # variables to add to claude's environment


def _home(env: Mapping[str, str]) -> str:
    key = "USERPROFILE" if os.name == "nt" else "HOME"
    return env.get(key) or os.path.expanduser("~")


def _version_key(name: str) -> Optional[Tuple[int, ...]]:
    if not _VERSION_RE.match(name):
        return None
    return tuple(int(part) for part in name.split("."))


def _in_plugin_dir(root: str, how: str) -> Channel:
    script = os.path.join(root, CHANNEL_DIR, SCRIPT_NAME)
    mcp_config = os.path.join(root, CHANNEL_DIR, MCP_NAME)
    missing = [p for p in (script, mcp_config) if not os.path.isfile(p)]
    if missing:
        raise RelayError(
            "--relay needs llm-kit's channel server, but %s has no %s (%s). This llm-kit predates the "
            "relay channel. Update it with `%s` and restart, or pass an llm-kit that has it."
            % (root, os.path.relpath(missing[0], root), how, _UPDATE)
        )
    return Channel(os.path.normpath(script), os.path.normpath(mcp_config))


def resolve_channel(env: Mapping[str, str]) -> Channel:
    """The channel files of the llm-kit this launch uses; raises RelayError."""
    pinned = env.get(CHANNEL_SCRIPT_ENV)
    if pinned:
        if not os.path.isabs(pinned) or not os.path.isfile(pinned):
            raise RelayError(
                "%s must be the absolute path of an existing relay_channel.py, got %r"
                % (CHANNEL_SCRIPT_ENV, pinned)
            )
        mcp_config = os.path.join(os.path.dirname(pinned), MCP_NAME)
        if not os.path.isfile(mcp_config):
            raise RelayError(
                "%s names %s, but there is no %s beside it" % (CHANNEL_SCRIPT_ENV, pinned, MCP_NAME)
            )
        return Channel(os.path.normpath(pinned), os.path.normpath(mcp_config))

    if env.get("CLAUDE_PLUGIN_TEST"):
        dirs = [d for d in env.get("CLAUDE_PLUGIN_TEST_DIRS", "").split(os.pathsep) if d]
        found: List[str] = [
            d for d in dirs if os.path.normcase(os.path.basename(d.rstrip("/\\"))) == os.path.normcase(OWNER)
        ]
        if not found:
            raise RelayError(
                "--relay needs llm-kit's channel server, and this claudx session did not load llm-kit "
                "(loaded: %s). The cached llm-kit is the installed release, so it is not used under "
                "claudx. Start claudx with llm-kit enabled (claudx --all), or set %s."
                % (", ".join(os.path.basename(d.rstrip("/\\")) for d in dirs) or "nothing", CHANNEL_SCRIPT_ENV)
            )
        return _in_plugin_dir(found[0], "the working copy loaded by claudx")

    cache = os.path.join(_home(env), ".claude", "plugins", "cache", MARKETPLACE, OWNER)
    versions = []
    if os.path.isdir(cache):
        for name in os.listdir(cache):
            key = _version_key(name)
            if key is not None and os.path.isdir(os.path.join(cache, name)):
                versions.append((key, name))
    if not versions:
        raise RelayError(
            "--relay needs the llm-kit plugin, and none is installed (no version under %s). "
            "Install it with `%s`, then try again." % (cache, _INSTALL)
        )
    newest = max(versions)[1]
    return _in_plugin_dir(os.path.join(cache, newest), "llm-kit %s, the newest installed" % newest)


def resolve_python(env: Mapping[str, str]) -> str:
    """The interpreter mcp.json's ${BOOTSTRAP_PYTHON} runs the channel script with.

    The caller's BOOTSTRAP_PYTHON when set (it must exist); otherwise the
    interpreter running this launcher, which the clauder shim chose. Paths are
    normalised, so a Windows BOOTSTRAP_PYTHON inherited with forward slashes
    (C:/Users/x/python.exe, measured on 5090W) reaches claude and mcp.json with
    one separator style.
    """
    pinned = env.get(PYTHON_ENV)
    if pinned:
        if not os.path.isabs(pinned) or not os.path.isfile(pinned):
            raise RelayError(
                "%s must be the absolute path of an existing Python interpreter, got %r. "
                "Unset it to use the interpreter running clauder." % (PYTHON_ENV, pinned)
            )
        return os.path.normpath(pinned)
    if sys.version_info < MIN_PYTHON:
        raise RelayError(
            "the relay channel needs Python %d.%d or later and clauder is running under %d.%d. "
            "Set %s to a newer interpreter." % (MIN_PYTHON + sys.version_info[:2] + (PYTHON_ENV,))
        )
    return sys.executable


def require_opt_in(channel: Channel, env: Mapping[str, str]) -> None:
    """Refuse --relay unless the user's relay.yaml turns the relay on; asks the channel script itself.

    Raises RelayError when the relay is off, when its settings cannot be read,
    and when the channel script predates the opt-in. A channel script that
    fails to load raises its own error.
    """
    name = "relaunch_kit_relay_channel"
    spec = importlib.util.spec_from_file_location(name, channel.script)
    if spec is None or spec.loader is None:
        raise RelayError("--relay cannot load llm-kit's channel server %s" % channel.script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    check = getattr(module, "opt_in_refusal", None)
    if check is None:
        raise RelayError(
            "--relay needs an llm-kit whose relay is off until the user turns it on, and %s predates that. "
            "Update it with `%s` and restart." % (channel.script, _UPDATE)
        )
    refusal = check(env)
    if refusal is not None:
        raise RelayError("--relay refused: %s." % refusal)


def relay_launch(env: Mapping[str, str]) -> RelayLaunch:
    """The flags and variables `clauder --relay` adds; raises RelayError."""
    channel = resolve_channel(env)
    require_opt_in(channel, env)
    interpreter = resolve_python(env)
    return RelayLaunch(
        args=(
            "--mcp-config", channel.mcp_config,
            "--dangerously-load-development-channels", "server:" + SERVER_NAME,
            "--allowedTools", "mcp__%s__reply" % SERVER_NAME,
        ),
        env={PYTHON_ENV: interpreter, CHANNEL_SCRIPT_ENV: channel.script},
    )
