"""`clauder --relay`: option stripping, llm-kit lookup, argv and environment, relaunch.

The lexer parts run on lib/claude_args.py alone; the launch parts run
relaunch.main against the fake claude (fake_claude.py), whose recorded calls are
the dry output: nothing here starts the real claude.
"""

import json
import os
import subprocess
import sys

import pytest

import claude_args
import relaunch
import relay_option
from claude_args import HIDDEN_FLAGS, FLAGS, RELAY_FLAG, launch_argv, parse
from conftest import BIN, PLUGIN, posix_only
from relay_option import RelayError

SID = "0b7e6c2a-1111-4222-8333-444455556666"
REPO = PLUGIN.parents[1]


def run(fake, args, *, env_extra=None, **kwargs):
    kwargs.setdefault("interactive", True)
    env = fake.env()
    env.update(env_extra or {})
    return relaunch.main(list(args), env=env, **kwargs)


def relay_args(root):
    """The flags relay_launch adds for the llm-kit at `root` (a fake cached llm-kit)."""
    return [
        "--mcp-config", str(root / "channel" / "mcp.json"),
        "--dangerously-load-development-channels", "server:relay",
        "--allowedTools", "mcp__relay__reply",
    ]


# --- the option in the lexer ---------------------------------------------------


@pytest.mark.parametrize("argv,positions,carried", [
    (["--relay"], (0,), []),
    (["--relay", "--model", "m", "hello"], (0,), ["--model", "m"]),
    (["--model", "m", "--relay", "hello"], (2,), ["--model", "m"]),
    (["hello", "--relay"], (1,), []),
    (["--add-dir", "a", "b", "--relay", "--effort", "high"], (3,), ["--add-dir", "a", "b", "--effort", "high"]),
    (["-cr", "ID", "--relay"], (2,), []),  # the cluster is one raw token; the index counts raw tokens
    (["-cn", "my name", "--relay", "x"], (2,), ["-n", "my name"]),
    (["--relay", "--relay"], (0, 1), []),
])
def test_relay_is_removed_where_it_stands_and_its_index_is_reported(argv, positions, carried):
    parsed = parse(argv)
    assert parsed.pass_through is None
    assert parsed.launcher_positions == positions
    assert list(parsed.carried) == carried
    assert all(argv[i] == RELAY_FLAG for i in positions)


def test_a_value_that_reads_like_the_relay_flag_is_a_value_not_the_option():
    parsed = parse(["--system-prompt", "--relay", "--model", "m"])
    assert parsed.launcher_positions == ()
    assert list(parsed.carried) == ["--system-prompt", "--relay", "--model", "m"]
    after_terminator = parse(["--", "--relay"])
    assert after_terminator.launcher_positions == () and after_terminator.operands == ("--relay",)


def test_relay_seen_before_a_mode_flag_is_reported_with_the_pass_through():
    parsed = parse(["--relay", "-p", "hi"])
    assert parsed.pass_through == "-p" and parsed.launcher_positions == (0,)


def test_relay_takes_no_value_and_is_not_a_prefix_match():
    with pytest.raises(claude_args.ArgsError, match="unknown option '--relay'"):
        parse(["--relay=x"])
    with pytest.raises(claude_args.ArgsError, match="unknown option '--relays'"):
        parse(["--relays"])


def test_hidden_table_holds_what_the_help_comparison_does_not_cover():
    visible = {n for f in FLAGS for n in f.names}
    hidden = {n for f in HIDDEN_FLAGS for n in f.names}
    assert hidden == {"--relay", "--dangerously-load-development-channels"}
    assert not (visible & hidden)  # a flag in both would be compared with the help and fail it
    # the lexer reads the hidden channel flag as claude does: variadic
    parsed = parse(["--dangerously-load-development-channels", "server:a", "server:b", "--model", "m"])
    assert list(parsed.carried) == ["--dangerously-load-development-channels", "server:a", "server:b", "--model", "m"]


@pytest.mark.parametrize("argv,expected", [
    (["--relay", "--model", "m"], ["--model", "m", "X", "Y"]),
    (["--model", "m", "--relay", "hello"], ["--model", "m", "hello", "X", "Y"]),
    (["--relay", "--", "--odd prompt"], ["X", "Y", "--", "--odd prompt"]),
    (["--relay", "--model", "--", "p"], ["--model", "--", "p", "X", "Y"]),  # `--` as a value is not the terminator
])
def test_launch_argv_adds_the_flags_before_the_terminator_else_at_the_end(argv, expected):
    assert launch_argv(argv, parse(argv), ("X", "Y")) == expected


def test_added_flags_cannot_swallow_a_prompt_or_be_taken_as_one():
    argv = ["hello", "--relay"]
    out = launch_argv(argv, parse(argv), ("--allowedTools", "mcp__relay__reply"))
    parsed = parse(out)
    assert parsed.operands == ("hello",)
    assert list(parsed.carried) == ["--allowedTools", "mcp__relay__reply"]


# --- finding llm-kit -----------------------------------------------------------


def make_llm_kit(root, *, channel=True):
    root.mkdir(parents=True)
    if channel:
        (root / "channel").mkdir()
        (root / "channel" / "relay_channel.py").write_text("# channel\n", encoding="utf-8")
        (root / "channel" / "mcp.json").write_text("{}\n", encoding="utf-8")
    return root


def cache_env(home, **extra):
    env = {"HOME": str(home), "USERPROFILE": str(home)}
    env.update(extra)
    return env


def cache_dir(home):
    return home / ".claude" / "plugins" / "cache" / "plugins-kit" / "llm-kit"


def test_the_newest_cached_version_is_used_by_number_not_by_text(tmp_path):
    for version in ("0.9.0", "0.10.0", "0.2.5", "not-a-version"):
        make_llm_kit(cache_dir(tmp_path) / version)
    channel = relay_option.resolve_channel(cache_env(tmp_path))
    assert channel.script == str(cache_dir(tmp_path) / "0.10.0" / "channel" / "relay_channel.py")
    assert channel.mcp_config == str(cache_dir(tmp_path) / "0.10.0" / "channel" / "mcp.json")


def test_no_installed_llm_kit_is_refused_with_the_install_command(tmp_path):
    with pytest.raises(RelayError) as absent:
        relay_option.resolve_channel(cache_env(tmp_path))
    assert "none is installed" in str(absent.value)
    assert "claude plugin install llm-kit@plugins-kit" in str(absent.value)
    make_llm_kit(cache_dir(tmp_path) / "not-a-version")  # a directory that is no version is not an install
    with pytest.raises(RelayError, match="none is installed"):
        relay_option.resolve_channel(cache_env(tmp_path))


def test_an_llm_kit_without_the_channel_is_refused_with_the_update_command_and_a_different_message(tmp_path):
    make_llm_kit(cache_dir(tmp_path) / "0.16.1", channel=False)
    with pytest.raises(RelayError) as old:
        relay_option.resolve_channel(cache_env(tmp_path))
    text = str(old.value)
    assert "predates the relay channel" in text and "claude plugin update llm-kit@plugins-kit" in text
    assert "none is installed" not in text and "claude plugin install" not in text


def test_an_older_version_that_has_the_channel_does_not_stand_in_for_the_newest(tmp_path):
    make_llm_kit(cache_dir(tmp_path) / "0.17.0")
    make_llm_kit(cache_dir(tmp_path) / "0.18.0", channel=False)
    with pytest.raises(RelayError, match="predates the relay channel"):
        relay_option.resolve_channel(cache_env(tmp_path))


def test_under_claudx_the_loaded_working_copy_is_used_and_the_cache_is_not(tmp_path):
    make_llm_kit(cache_dir(tmp_path) / "0.17.0")  # the installed release: must not be chosen
    work = make_llm_kit(tmp_path / "work" / "plugins" / "llm-kit")
    other = make_llm_kit(tmp_path / "work" / "plugins" / "relaunch-kit")
    env = cache_env(
        tmp_path, CLAUDE_PLUGIN_TEST="1", CLAUDE_PLUGIN_TEST_MARKETPLACE="plugins-kit",
        CLAUDE_PLUGIN_TEST_DIRS=os.pathsep.join([str(other), str(work)]),
    )
    assert relay_option.resolve_channel(env).script == str(work / "channel" / "relay_channel.py")


def test_under_claudx_without_llm_kit_loaded_the_cache_is_not_a_substitute(tmp_path):
    make_llm_kit(cache_dir(tmp_path) / "0.17.0")
    only = make_llm_kit(tmp_path / "work" / "plugins" / "relaunch-kit")
    env = cache_env(tmp_path, CLAUDE_PLUGIN_TEST="1", CLAUDE_PLUGIN_TEST_DIRS=str(only))
    with pytest.raises(RelayError) as refused:
        relay_option.resolve_channel(env)
    assert "did not load llm-kit" in str(refused.value) and "relaunch-kit" in str(refused.value)
    assert "claudx --all" in str(refused.value)
    with pytest.raises(RelayError, match="did not load llm-kit"):  # no TEST_DIRS at all
        relay_option.resolve_channel(cache_env(tmp_path, CLAUDE_PLUGIN_TEST="1"))


def test_under_claudx_a_loaded_llm_kit_without_the_channel_is_refused(tmp_path):
    work = make_llm_kit(tmp_path / "work" / "plugins" / "llm-kit", channel=False)
    env = cache_env(tmp_path, CLAUDE_PLUGIN_TEST="1", CLAUDE_PLUGIN_TEST_DIRS=str(work))
    with pytest.raises(RelayError, match="predates the relay channel"):
        relay_option.resolve_channel(env)


def test_a_pinned_channel_script_wins_and_is_checked(tmp_path):
    make_llm_kit(cache_dir(tmp_path) / "0.17.0")
    pinned = make_llm_kit(tmp_path / "elsewhere")
    script = str(pinned / "channel" / "relay_channel.py")
    channel = relay_option.resolve_channel(cache_env(tmp_path, LLM_KIT_CHANNEL_SCRIPT=script))
    assert channel.script == script and channel.mcp_config == str(pinned / "channel" / "mcp.json")
    with pytest.raises(RelayError, match="absolute path of an existing relay_channel.py"):
        relay_option.resolve_channel(cache_env(tmp_path, LLM_KIT_CHANNEL_SCRIPT="relay_channel.py"))
    with pytest.raises(RelayError, match="absolute path of an existing relay_channel.py"):
        relay_option.resolve_channel(cache_env(tmp_path, LLM_KIT_CHANNEL_SCRIPT=str(tmp_path / "nope.py")))
    (pinned / "channel" / "mcp.json").unlink()
    with pytest.raises(RelayError, match="no mcp.json beside it"):
        relay_option.resolve_channel(cache_env(tmp_path, LLM_KIT_CHANNEL_SCRIPT=script))


# --- the interpreter ------------------------------------------------------------


def test_the_launching_interpreter_is_the_default_and_a_set_one_must_exist(tmp_path):
    assert relay_option.resolve_python({}) == sys.executable
    assert relay_option.resolve_python({"BOOTSTRAP_PYTHON": sys.executable}) == sys.executable
    for bad in (str(tmp_path / "no-python"), "python3"):
        with pytest.raises(RelayError, match="BOOTSTRAP_PYTHON must be the absolute path"):
            relay_option.resolve_python({"BOOTSTRAP_PYTHON": bad})


def test_an_interpreter_older_than_the_channel_script_is_refused(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 8, 18, "final", 0))
    with pytest.raises(RelayError, match=r"needs Python 3\.9 or later and clauder is running under 3\.8"):
        relay_option.resolve_python({})


# --- the pin to llm-kit's mcp.json -----------------------------------------------


def test_names_match_the_mcp_json_llm_kit_ships():
    """Pins the producer: the server name and the two variables come from channel/mcp.json."""
    config = json.loads((REPO / "plugins" / "llm-kit" / "channel" / "mcp.json").read_text(encoding="utf-8"))
    assert list(config["mcpServers"]) == [relay_option.SERVER_NAME]
    server = config["mcpServers"][relay_option.SERVER_NAME]
    assert server["command"] == "${%s}" % relay_option.PYTHON_ENV
    assert server["args"] == ["${%s}" % relay_option.CHANNEL_SCRIPT_ENV]
    assert (REPO / "plugins" / "llm-kit" / "channel" / relay_option.SCRIPT_NAME).is_file()


def test_the_working_copy_llm_kit_resolves_under_claudx():
    work = REPO / "plugins" / "llm-kit"
    channel = relay_option.resolve_channel({"CLAUDE_PLUGIN_TEST": "1", "CLAUDE_PLUGIN_TEST_DIRS": str(work)})
    assert channel.mcp_config == str(work / "channel" / "mcp.json")


# --- the launch -------------------------------------------------------------------


def test_relay_adds_the_flags_and_the_two_variables_and_strips_the_option(fake_claude):
    root = fake_claude.install_llm_kit()
    fake_claude.script([{"exit": 4}])
    assert run(fake_claude, ["--relay", "--model", "m", "hello"]) == 4
    (call,) = fake_claude.calls()
    assert call["argv"] == ["--model", "m", "hello"] + relay_args(root)
    assert call["relay_env"] == {
        "BOOTSTRAP_PYTHON": sys.executable,
        "LLM_KIT_CHANNEL_SCRIPT": str(root / "channel" / "relay_channel.py"),
    }
    assert RELAY_FLAG not in call["argv"]


def test_relay_keeps_a_bootstrap_python_the_caller_set(fake_claude):
    fake_claude.install_llm_kit()
    fake_claude.script([{}])
    run(fake_claude, ["--relay"], env_extra={"BOOTSTRAP_PYTHON": sys.executable})
    assert fake_claude.calls()[0]["relay_env"]["BOOTSTRAP_PYTHON"] == sys.executable


def test_relay_does_not_swallow_a_user_variadic_flag_or_prompt(fake_claude):
    root = fake_claude.install_llm_kit()
    fake_claude.script([{}])
    run(fake_claude, ["--allowedTools", "Bash", "--relay", "do it"])
    assert fake_claude.calls()[0]["argv"] == ["--allowedTools", "Bash", "do it"] + relay_args(root)


def test_the_relaunch_carries_the_flags_and_the_variables(fake_claude):
    root = fake_claude.install_llm_kit()
    fake_claude.script([{"request": {}}, {"request": {"action": "prompt", "prompt": "/task work x", "reason": "hand-off"}}, {}])
    assert run(fake_claude, ["--relay", "--model", "m", "-c", "first"]) == 0
    first, resumed, handed = fake_claude.calls()
    assert first["argv"] == ["--model", "m", "-c", "first"] + relay_args(root)
    assert resumed["argv"] == ["--model", "m"] + relay_args(root) + ["--resume", SID]
    assert handed["argv"] == ["--model", "m"] + relay_args(root) + ["--", "/task work x"]
    assert first["relay_env"] == resumed["relay_env"] == handed["relay_env"]
    assert resumed["relay_env"]["LLM_KIT_CHANNEL_SCRIPT"] == str(root / "channel" / "relay_channel.py")


def test_without_relay_nothing_is_added(fake_claude):
    fake_claude.install_llm_kit()
    fake_claude.script([{"request": {}}, {}])
    assert run(fake_claude, ["--model", "m", "hello"]) == 0
    first, second = fake_claude.calls()
    assert first["argv"] == ["--model", "m", "hello"]
    assert second["argv"] == ["--model", "m", "--resume", SID]
    assert first["relay_env"] == second["relay_env"] == {"BOOTSTRAP_PYTHON": None, "LLM_KIT_CHANNEL_SCRIPT": None}


def test_a_value_that_reads_like_the_option_reaches_claude_untouched(fake_claude):
    fake_claude.script([{}])
    assert run(fake_claude, ["--system-prompt", "--relay"]) == 0
    assert fake_claude.calls()[0]["argv"] == ["--system-prompt", "--relay"]


def test_missing_llm_kit_fails_loudly_and_launches_nothing(fake_claude, capsys):
    fake_claude.script([{}])
    assert run(fake_claude, ["--relay", "hello"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("clauder: ") and "none is installed" in err
    assert "claude plugin install llm-kit@plugins-kit" in err
    assert fake_claude.calls() == [] and not fake_claude.rendezvous().exists()


def test_an_llm_kit_without_the_channel_launches_nothing(fake_claude, capsys):
    fake_claude.install_llm_kit("0.16.1", channel=False)
    fake_claude.script([{}])
    assert run(fake_claude, ["--relay"]) == 2
    assert "predates the relay channel" in capsys.readouterr().err
    assert fake_claude.calls() == []


# Off by default. Shown red when written: in relay_option.relay_launch delete the
# ``require_opt_in(channel, env)`` call: the three tests below go red.

def test_relay_is_refused_while_the_relay_is_off_and_launches_nothing(fake_claude, capsys):
    fake_claude.install_llm_kit(opt_in="the llm-kit relay is not enabled on this machine; to turn it on, put X")
    fake_claude.script([{}])
    assert run(fake_claude, ["--relay"]) == 2
    err = capsys.readouterr().err
    assert err == "clauder: --relay refused: the llm-kit relay is not enabled on this machine; to turn it on, put X.\n"
    assert fake_claude.calls() == [] and not fake_claude.rendezvous().exists()


def test_an_llm_kit_whose_channel_predates_the_opt_in_launches_nothing(fake_claude, capsys):
    fake_claude.install_llm_kit("0.20.0", opt_in=False)
    fake_claude.script([{}])
    assert run(fake_claude, ["--relay"]) == 2
    err = capsys.readouterr().err
    assert "predates that" in err and "claude plugin update llm-kit@plugins-kit" in err
    assert fake_claude.calls() == []


def test_the_real_channel_refuses_until_relay_yaml_turns_the_relay_on(tmp_path):
    script = REPO / "plugins" / "llm-kit" / "channel" / relay_option.SCRIPT_NAME
    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "LLM_KIT_CHANNEL_SCRIPT": str(script)}
    path = tmp_path / "cfg" / "llm-kit" / "relay.yaml"
    with pytest.raises(RelayError, match=r"--relay refused: the llm-kit relay is not enabled on this machine"):
        relay_option.relay_launch(env)
    path.parent.mkdir(parents=True)
    path.write_text("enabled: false\n", encoding="utf-8")
    with pytest.raises(RelayError) as off:
        relay_option.relay_launch(env)
    assert "put `enabled: true` in %s" % path in str(off.value)
    path.write_text("enabled: true\n", encoding="utf-8")
    assert relay_option.relay_launch(env).env[relay_option.CHANNEL_SCRIPT_ENV] == str(script)


@pytest.mark.parametrize("args", [["--relay", "-p", "hi"], ["--relay", "--bg"], ["--relay", "mcp", "list"]])
def test_relay_is_refused_for_a_run_that_is_not_an_interactive_session(fake_claude, capsys, args):
    fake_claude.install_llm_kit()
    fake_claude.script([{}])
    assert run(fake_claude, args) == 2
    assert "--relay loads a channel into an interactive session" in capsys.readouterr().err
    assert fake_claude.calls() == []


def test_relay_without_a_terminal_is_refused_not_forwarded(fake_claude, capsys):
    fake_claude.install_llm_kit()
    fake_claude.script([{}])
    assert run(fake_claude, ["--relay"], interactive=False, exec_fn=lambda *a: pytest.fail("exec")) == 2
    assert "stdin or stdout is not a terminal" in capsys.readouterr().err
    assert fake_claude.calls() == []


@posix_only
def test_clauder_relay_through_the_real_shim_in_a_terminal(fake_claude):
    from test_launcher import _chain_env, run_in_pty

    root = fake_claude.install_llm_kit()
    fake_claude.script([{"request": {}}, {"exit": 6}])
    status, output = run_in_pty([str(BIN / "clauder"), "--relay", "--model", "m"], _chain_env(fake_claude))
    assert status == 6, output
    first, second = fake_claude.calls()
    assert first["argv"] == ["--model", "m"] + relay_args(root)
    assert second["argv"] == ["--model", "m"] + relay_args(root) + ["--resume", SID]
    for call in (first, second):
        assert call["relay_env"]["LLM_KIT_CHANNEL_SCRIPT"] == str(root / "channel" / "relay_channel.py")
        assert os.path.isfile(call["relay_env"]["BOOTSTRAP_PYTHON"])

