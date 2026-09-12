"""session_env: the pass's $CLAUDE_ENV_FILE block stays bounded and well formed.

The bug these cover: Claude Code splices this block into the front of every
`bash -c` string, so the block is charged against the command-line length limit
on every command. The engine never clears a sessionstart-hook file between
passes, so appending per variable grew it without bound until the assembled
command line was cut mid-string -- surfacing as a quoting error on an unrelated
command.

Values go through ``shlex.quote``, which leaves a metacharacter-free value
unquoted. That is deliberate and these tests assert it: the block is charged
against a length limit, so two bytes per line are not spent on quotes that
``shlex`` has already proven unnecessary.
"""

import shlex

import pytest

from bootstrap_lib import session_env


@pytest.fixture(autouse=True)
def _clean_buffer():
    session_env.reset()
    yield
    session_env.reset()


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / "sessionstart-hook-0.sh"
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(path))
    return path


def _names(path):
    return [
        line[len("export "):].split("=", 1)[0]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("export ")
    ]


def test_record_noops_without_env_file(monkeypatch):
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    assert session_env.record("FOO", "bar") is None
    assert session_env.flush() == 0


def test_record_returns_name_and_flush_writes_once(env_file):
    assert session_env.record("FOO", "bar") == "FOO"
    assert session_env.flush() == 1
    assert env_file.read_text(encoding="utf-8") == "export FOO=bar\n"


def test_flush_without_records_leaves_the_file_alone(env_file):
    env_file.write_text("export EARLIER=kept\n", encoding="utf-8")
    assert session_env.flush() == 0
    assert env_file.read_text(encoding="utf-8") == "export EARLIER=kept\n"


def test_same_name_twice_in_one_pass_writes_one_line(env_file):
    session_env.record("FOO", "first")
    session_env.record("FOO", "second")
    session_env.flush()
    assert env_file.read_text(encoding="utf-8") == "export FOO=second\n"


def test_repeated_passes_do_not_grow_the_block(env_file):
    """The regression. Three identical passes must leave one line per name."""
    for _ in range(3):
        for name, value in (("A", "1"), ("B", "2"), ("C", "3")):
            session_env.record(name, value)
        session_env.flush()

    assert _names(env_file) == ["A", "B", "C"]
    assert env_file.read_text(encoding="utf-8") == (
        "export A=1\nexport B=2\nexport C=3\n"
    )


def test_a_later_pass_updates_a_value_in_place(env_file):
    session_env.record("TOOL", "/old/path")
    session_env.flush()
    session_env.record("TOOL", "/new/path")
    session_env.flush()
    assert env_file.read_text(encoding="utf-8") == "export TOOL=/new/path\n"


def test_a_partial_pass_keeps_what_an_earlier_pass_exported(env_file):
    session_env.record("FROM_FULL_PASS", "kept")
    session_env.flush()
    session_env.record("FROM_PARTIAL_PASS", "added")
    session_env.flush()
    assert _names(env_file) == ["FROM_FULL_PASS", "FROM_PARTIAL_PASS"]


def test_unparseable_lines_are_dropped_on_rewrite(env_file):
    env_file.write_text(
        "export GOOD=keep\n"
        "export BROKEN='unterminated\n"
        "not an export line at all\n",
        encoding="utf-8",
    )
    session_env.record("NEW", "value")
    session_env.flush()
    text = env_file.read_text(encoding="utf-8")
    assert "not an export line" not in text
    assert "unterminated" not in text
    assert _names(env_file) == ["GOOD", "NEW"]


def test_values_are_quoted_so_one_cannot_end_its_own_string(env_file):
    value = "it's \"quoted\"; rm -rf / $(id)"
    session_env.record("NASTY", value)
    session_env.flush()
    text = env_file.read_text(encoding="utf-8")
    assert text == "export NASTY=" + shlex.quote(value) + "\n"
    # Counting quotes proves nothing -- shlex.quote emits the '"'"' idiom, so a
    # correct line here holds an ODD number of them. Parsing is the real test.
    assert shlex.split(text.strip()) == ["export", "NASTY=" + value]
    assert "$(id)" in text  # inert inside single quotes, never expanded


def test_written_utf8_with_lf_endings(env_file):
    session_env.record("PATHY", "C:/x/y")
    session_env.flush()
    raw = env_file.read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode("utf-8") == "export PATHY=C:/x/y\n"


def test_unwritable_file_is_not_fatal(env_file, monkeypatch):
    session_env.record("FOO", "bar")

    def _boom(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr("builtins.open", _boom)
    assert session_env.flush() == 0
