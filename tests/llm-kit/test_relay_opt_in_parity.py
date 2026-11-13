"""The plugin's relay opt-in reader agrees with katsoft-llm-kit's ``llm_kit.relay.config.relay_enabled``.

The /direct hooks and the channel server run under the bootstrap standalone interpreter, which has no
llm_kit package and no PyYAML, so hooks/direct_relay.relay_opt_in reads relay.yaml itself. The owner's
requirement is that nothing in the plugin reaches a relay the user did not turn on in their own settings
file, so this file pins the reader against the library (the root dev environment installs the released
katsoft-llm-kit the plugin requires):

* AGREE: on every case here both return the same boolean or both raise.
* STRICTER: where the reader cannot read a line exactly it raises and the library reads the file. The
  reader may refuse a file the library accepts, never the reverse; it never returns True there.
* NESTED: the reader does not validate nested blocks. Where only a nested block is malformed it reads
  ``enabled`` alone, and the library refuses to start a relay from that file, so no relay listens.

Shown red when written: in direct_relay drop ``"yes"`` from ``_YAML_TRUE``:
test_the_reader_agrees_with_the_library[yes] goes red.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Optional

import pytest

from llm_kit.relay import config as C

REPO = Path(__file__).resolve().parents[2]
DIRECT_RELAY = REPO / "plugins" / "llm-kit" / "hooks" / "direct_relay.py"


def _load_reader():
    spec = importlib.util.spec_from_file_location("direct_relay_parity_under_test", DIRECT_RELAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = _load_reader()

LISTENER = "relay:\n  host: 127.0.0.1\n  port: 4710\n"

AGREE: dict[str, Optional[str]] = {
    "no file": None,
    "empty file": "",
    "comment only": "# relay settings\n",
    "listener only": LISTENER,
    "true": "enabled: true\n",
    "True": "enabled: True\n",
    "TRUE": "enabled: TRUE\n",
    "yes": "enabled: yes\n",
    "on": "enabled: on\n",
    "false": "enabled: false\n",
    "no": "enabled: no\n",
    "off": "enabled: Off\n",
    "comment after": "enabled: true  # turned on 2026-11-12\n",
    "space before colon": "enabled : true\n",
    "crlf": "enabled: true\r\n" + LISTENER.replace("\n", "\r\n"),
    "after a listener": LISTENER + "enabled: true\n",
    "hub block on": "enabled: true\nhub:\n  enabled: true\n",
    "hub on relay off": "enabled: false\nhub:\n  enabled: true\n",
    "hub flow mapping": "hub: {enabled: true}\nenabled: true\n",
    "ledgers": "ledgers:\n  - /tmp/jobs.sqlite3\nenabled: true\n",
    "endpoints only": "llm_endpoints:\n  x:\n    base_url: http://127.0.0.1:8000/v1\n    model: m\n",
    "raise quoted": 'enabled: "true"\n',
    "raise single quoted": "enabled: 'yes'\n",
    "raise word": "enabled: maybe\n",
    "raise empty": "enabled:\n",
    "raise empty with comment": "enabled: # later\n",
    "raise tilde": "enabled: ~\n",
    "raise null": "enabled: null\n",
    "raise int": "enabled: 1\n",
    "raise list": "enabled: [true]\n",
    "raise mapping": "enabled: {}\n",
    "raise hash glued": "enabled: true#x\n",
    "raise unknown key": "enable: true\n",
    "raise unknown key beside": "enabled: true\nport: 4710\n",
    "raise sequence document": "- enabled: true\n",
    "raise scalar document": "enabled\n",
}

STRICTER: dict[str, str] = {
    "document marker": "---\nenabled: true\n",
    "flow document": "{enabled: true}\n",
    "duplicate key": "enabled: true\nenabled: false\n",
    "value on the next line": "enabled:\n  true\n",
    "explicit tag": "enabled: !!bool true\n",
}

NESTED: dict[str, str] = {
    "relay not a mapping": "enabled: true\nrelay: 5\n",
    "relay unknown key": "enabled: true\nrelay:\n  bogus: 1\n",
    "hub flag not boolean": "enabled: true\nhub:\n  enabled: maybe\n",
}


def _write(tmp_path: Path, text: Optional[str]) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "cfg"
    path = home / "llm-kit" / "relay.yaml"
    path.parent.mkdir(parents=True)
    if text is not None:
        path.write_bytes(text.encode("utf-8"))
    return path, {"XDG_CONFIG_HOME": str(home)}


def _outcome(fn, error):
    try:
        return fn()
    except error:
        return "raise"


@pytest.mark.parametrize("case", sorted(AGREE))
def test_the_reader_agrees_with_the_library(tmp_path, case):
    path, env = _write(tmp_path, AGREE[case])
    library = _outcome(lambda: C.relay_enabled(path), C.RelayConfigError)
    reader = _outcome(lambda: R.relay_opt_in(env), R.HookError)
    assert reader == library
    if case.startswith("raise "):
        assert library == "raise"


@pytest.mark.parametrize("case", sorted(STRICTER))
def test_where_the_reader_cannot_read_a_line_it_refuses_and_never_reads_on(tmp_path, case):
    path, env = _write(tmp_path, STRICTER[case])
    assert isinstance(C.relay_enabled(path), bool)  # the library reads it: a real deviation, not a shared error
    with pytest.raises(R.HookError):
        R.relay_opt_in(env)


@pytest.mark.parametrize("case", sorted(NESTED))
def test_a_malformed_nested_block_leaves_no_relay_running(tmp_path, case):
    path, env = _write(tmp_path, NESTED[case])
    assert R.relay_opt_in(env) is True
    with pytest.raises(C.RelayConfigError):
        C.require_enabled("relay", path)


@pytest.mark.parametrize("xdg", [None, "relative/cfg", "ABS"])
def test_the_reader_and_the_library_name_the_same_file(tmp_path, xdg):
    env = {} if xdg is None else {"XDG_CONFIG_HOME": str(tmp_path) if xdg == "ABS" else xdg}
    assert R.config_file(env) == C.config_file(env)
    assert R.opt_in_hint(env) == f"put `enabled: true` in {C.config_file(env)}"


def test_the_library_refusal_names_the_key_the_reader_reports(tmp_path):
    path, _env = _write(tmp_path, None)
    with pytest.raises(C.RelayNotEnabled, match=r"put `enabled: true` in "):
        C.require_enabled("relay", path)
    assert R.OPT_IN_KEY == "enabled: true"


def test_the_standalone_reader_imports_neither_yaml_nor_llm_kit():
    imports = [line.strip() for line in DIRECT_RELAY.read_text(encoding="utf-8").splitlines()
               if line.startswith(("import ", "from "))]
    assert imports and not [line for line in imports if "yaml" in line or "llm_kit" in line]


def test_plugin_config_keys_equal_the_library_key_set():
    """The only plugin-side copy of the top-level relay.yaml key set is direct_relay.CONFIG_KEYS.

    The channel server and relaunch-kit's `--relay` reach it through direct_relay, so one equality
    covers every consumer. Shown red: with "resume" missing from CONFIG_KEYS this fails.
    """
    assert R.CONFIG_KEYS == C._CONFIG_KEYS
