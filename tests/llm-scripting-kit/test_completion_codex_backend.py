"""Tests for llm_scripting_kit.completion.codex_backend.

Hermetic: the subprocess runner is stubbed via ``runner=`` and the launcher via
``argv_prefix=``, so no ``codex`` binary need exist. The stub writes the ``-o``
file itself, because that file -- not stdout -- is where codex's answer lands
and the round trip is the thing worth pinning.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from llm_scripting_kit.completion import halt
from llm_scripting_kit.completion.claude_runner import (
    AgentTimeoutError,
    run_claude_streaming,
    run_cli_streaming,
)
from llm_scripting_kit.completion.codex_backend import (
    PROMPT_SEPARATOR,
    CodexCliBackend,
    CodexRunError,
    compose_prompt,
)
from llm_scripting_kit.completion.types import BackendOptions, LLMBackend


ARGV_PREFIX = ("codex",)


class _StubRunner:
    """Records calls; writes the ``-o`` file the way a real codex run would.

    Runner seam signature mirrors run_cli_streaming:
    ``(cmd, request, cwd, *, log_prefix, timeout_s, label, hard_stop_markers)``.
    """

    def __init__(self, output_text="codex-answer", result=("", "", 0), raises=None):
        self.output_text = output_text
        self.result = result
        self.raises = raises
        self.calls = []

    def __call__(self, cmd, request, cwd, **kwargs):
        self.calls.append(
            {"cmd": list(cmd), "request": request, "cwd": cwd, **kwargs}
        )
        if self.raises is not None:
            raise self.raises
        if self.output_text is not None:
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_text(self.output_text, encoding="utf-8")
        return self.result


def _backend(runner, **kw) -> CodexCliBackend:
    return CodexCliBackend(runner=runner, argv_prefix=ARGV_PREFIX, **kw)


def _root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


# ---------------------------------------------------------------------------
# argv
# ---------------------------------------------------------------------------


class TestArgv:
    def test_exact_default_argv(self, tmp_path: Path):
        """The COMPLETE default argv, not a spot check.

        Everything but the two temp paths is fixed, so the whole list is
        asserted -- a silently added or reordered flag is exactly the class of
        change this pins.
        """
        runner = _StubRunner()
        root = _root(tmp_path)
        _backend(runner).complete(
            "SYS", "USER", model="gpt-5.4-codex",
            options=BackendOptions(cwd=root),
        )
        cmd = runner.calls[0]["cmd"]
        out_path = cmd[cmd.index("-o") + 1]

        expected = ["codex", "exec", "-s", "workspace-write"]
        if os.name == "nt":
            expected += ["-c", 'windows.sandbox="unelevated"']
        expected += [
            "-c", "sandbox_workspace_write.network_access=true",
            "-m", "gpt-5.4-codex",
            "-C", str(root),
            "-o", out_path,
            "--skip-git-repo-check", "--color", "never", "-",
        ]
        assert cmd == expected

    def test_effort_and_model_forwarded(self, tmp_path: Path):
        runner = _StubRunner()
        _backend(runner).complete(
            "s", "u", model="gpt-5.4-codex",
            options=BackendOptions(cwd=_root(tmp_path), effort="high"),
        )
        cmd = runner.calls[0]["cmd"]
        assert cmd[cmd.index("-m") + 1] == "gpt-5.4-codex"
        assert "model_reasoning_effort=high" in cmd

    def test_extras_forwarded(self, tmp_path: Path):
        runner = _StubRunner()
        root = _root(tmp_path)
        scratch = (tmp_path / "scratch").resolve()
        extra_dir = (tmp_path / "extra").resolve()
        schema = (tmp_path / "schema.json").resolve()
        _backend(runner).complete(
            "s", "u", model="gpt-5.4-codex",
            options=BackendOptions(
                cwd=root,
                extras={
                    "scratch_dir": scratch,
                    "add_dirs": [extra_dir],
                    "sandbox": "read-only",
                    "network": False,
                    "output_schema": schema,
                    "ignored_by_this_backend": "x",
                },
            ),
        )
        cmd = runner.calls[0]["cmd"]
        assert cmd[cmd.index("-s") + 1] == "read-only"
        # network=False -> the network_access config pair is not emitted.
        assert "sandbox_workspace_write.network_access=true" not in cmd
        add_dirs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--add-dir"]
        assert add_dirs == [str(extra_dir), str(scratch)]
        assert cmd[cmd.index("--output-schema") + 1] == str(schema)
        assert "ignored_by_this_backend" not in cmd

    def test_relative_cwd_is_rejected(self):
        """A relative -C must raise, never be silently normalized.

        VERIFIED upstream: a relative ``-C`` combined with ``--add-dir``
        silently voids the ENTIRE writable-root set -- every write fails while
        codex still exits 0. The builder raises; this asserts the backend does
        not paper over a caller's relative path on the way there.
        """
        runner = _StubRunner()
        with pytest.raises(ValueError, match="must be an absolute path"):
            _backend(runner).complete(
                "s", "u", model="gpt-5.4-codex",
                options=BackendOptions(cwd=Path("relative/dir")),
            )
        assert runner.calls == []

    def test_absent_cwd_defaults_to_resolved_process_cwd(self):
        runner = _StubRunner()
        _backend(runner).complete("s", "u", model="gpt-5.4-codex")
        cmd = runner.calls[0]["cmd"]
        assert cmd[cmd.index("-C") + 1] == str(Path.cwd().resolve())


# ---------------------------------------------------------------------------
# prompt composition / response
# ---------------------------------------------------------------------------


class TestPromptAndResponse:
    def test_system_and_user_composed_onto_stdin(self, tmp_path: Path):
        runner = _StubRunner()
        _backend(runner).complete(
            "SYS", "USER", model="gpt-5.4-codex",
            options=BackendOptions(cwd=_root(tmp_path)),
        )
        call = runner.calls[0]
        assert call["request"] == f"SYS{PROMPT_SEPARATOR}USER"
        assert call["request"] == "SYS\n\n---\n\nUSER"
        # The prompt rides stdin, never argv.
        assert "SYS" not in call["cmd"] and "USER" not in call["cmd"]

    @pytest.mark.parametrize(
        "system,user,expected",
        [("S", "U", "S\n\n---\n\nU"), ("", "U", "U"), ("S", "", "S")],
    )
    def test_compose_prompt_edges(self, system, user, expected):
        assert compose_prompt(system, user) == expected

    def test_output_file_round_trip_and_cleanup(self, tmp_path: Path):
        runner = _StubRunner(output_text="the answer")
        resp = _backend(runner).complete(
            "s", "u", model="gpt-5.4-codex",
            options=BackendOptions(cwd=_root(tmp_path)),
        )
        assert resp.text == "the answer"
        assert resp.model == "gpt-5.4-codex"
        # No usage envelope on the default path -- honest zeros, not estimates.
        assert (resp.input_tokens, resp.output_tokens, resp.cache_hit_tokens) == (
            0, 0, 0,
        )
        assert resp.attempts == 1
        assert resp.from_cache is False
        assert resp.wall_ms >= 0

        cmd = runner.calls[0]["cmd"]
        assert not Path(cmd[cmd.index("-o") + 1]).exists()

    def test_missing_output_file_raises(self, tmp_path: Path):
        """The DELETING stub is the point.

        ``tempfile.mkstemp`` has already created the file, so a stub that
        merely declines to write it leaves an EMPTY file and lands in the
        empty-file branch instead -- the missing-file branch could then be
        deleted outright with this test still green.
        """

        class _DeletingRunner(_StubRunner):
            def __call__(self, cmd, request, cwd, **kwargs):
                Path(cmd[cmd.index("-o") + 1]).unlink()
                return super().__call__(cmd, request, cwd, **kwargs)

        runner = _DeletingRunner(output_text=None, result=("chatter", "denied", 0))
        with pytest.raises(CodexRunError, match="wrote no output file") as excinfo:
            _backend(runner).complete(
                "s", "u", model="gpt-5.4-codex",
                options=BackendOptions(cwd=_root(tmp_path)),
            )
        assert excinfo.value.stderr == "denied"

    def test_empty_output_file_raises(self, tmp_path: Path):
        runner = _StubRunner(output_text="   \n", result=("chatter", "denied", 0))
        with pytest.raises(CodexRunError, match="is empty") as excinfo:
            _backend(runner).complete(
                "s", "u", model="gpt-5.4-codex",
                options=BackendOptions(cwd=_root(tmp_path)),
            )
        assert excinfo.value.stderr == "denied"

    def test_transcript_never_reaches_the_exception_message(self, tmp_path: Path):
        """Model-authored text in the message would forge a halt.

        ``halt.classify_codex_exception`` substring-matches ``str(exc)``, and
        codex writes its transcript to BOTH channels -- so a run whose output
        merely discusses a rate limit must not classify as one.
        """
        chatter = "the docs mention a rate limit and an unauthorized user"
        runner = _StubRunner(output_text="   \n", result=(chatter, chatter, 0))
        with pytest.raises(CodexRunError) as excinfo:
            _backend(runner).complete(
                "s", "u", model="gpt-5.4-codex",
                options=BackendOptions(cwd=_root(tmp_path)),
            )
        exc = excinfo.value
        assert chatter not in str(exc)
        assert exc.stdout == chatter and exc.stderr == chatter
        assert halt.classify_codex_exception(exc) is None

    def test_output_file_cleaned_up_on_failure(self, tmp_path: Path):
        runner = _StubRunner(output_text=None, result=("", "boom", 1))
        with pytest.raises(RuntimeError, match="exit 1"):
            _backend(runner).complete(
                "s", "u", model="gpt-5.4-codex",
                options=BackendOptions(cwd=_root(tmp_path)),
            )
        cmd = runner.calls[0]["cmd"]
        assert not Path(cmd[cmd.index("-o") + 1]).exists()

    def test_ignores_completion_knobs_codex_does_not_expose(self, tmp_path: Path):
        """temperature / max_tokens are accepted and ignored (documented)."""
        runner = _StubRunner()
        resp = _backend(runner).complete(
            "s", "u", model="gpt-5.4-codex",
            options=BackendOptions(
                cwd=_root(tmp_path), temperature=0.9, max_tokens=7,
                cache_salt=3, allowed_tools="Read",
            ),
        )
        assert resp.text == "codex-answer"
        cmd = runner.calls[0]["cmd"]
        for flag in ("--temperature", "--max-tokens", "--allowedTools"):
            assert flag not in cmd
        assert "0.9" not in cmd and "7" not in cmd


# ---------------------------------------------------------------------------
# protocol / timeout / halt
# ---------------------------------------------------------------------------


class TestProtocolAndHalt:
    def test_name_and_protocol(self):
        backend = CodexCliBackend(argv_prefix=ARGV_PREFIX)
        assert backend.name == "codex-cli"
        assert isinstance(backend, LLMBackend)

    def test_timeout_defaults_and_option(self, tmp_path: Path):
        runner = _StubRunner()
        root = _root(tmp_path)
        _backend(runner).complete(
            "s", "u", model="gpt-5.4-codex", options=BackendOptions(cwd=root),
        )
        assert runner.calls[0]["timeout_s"] == 900.0
        assert runner.calls[0]["label"] == "codex exec"

        runner2 = _StubRunner()
        _backend(runner2).complete(
            "s", "u", model="gpt-5.4-codex",
            options=BackendOptions(cwd=root, timeout_s=30, log_prefix="[cx]"),
        )
        assert runner2.calls[0]["timeout_s"] == 30
        assert runner2.calls[0]["log_prefix"] == "[cx]"

    def test_timeout_propagates_typed_and_classifies(self, tmp_path: Path):
        exc = AgentTimeoutError(
            "codex exec exceeded 900s timeout", cmd=["codex", "exec"],
            elapsed_s=901, stdout="partial", stderr="quiet",
        )
        runner = _StubRunner(raises=exc)
        backend = _backend(runner)
        with pytest.raises(AgentTimeoutError) as excinfo:
            backend.complete(
                "s", "u", model="gpt-5.4-codex",
                options=BackendOptions(cwd=_root(tmp_path)),
            )
        assert excinfo.value is exc
        assert backend.classify_halt(exc) == halt.HALT_RATE_LIMIT
        # The temp file is removed even on the raising path.
        cmd = runner.calls[0]["cmd"]
        assert not Path(cmd[cmd.index("-o") + 1]).exists()

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("codex exec failed: you have hit your usage limit", halt.HALT_RATE_LIMIT),
            ("rate limit reached for this account", halt.HALT_RATE_LIMIT),
            ("429 too many requests", halt.HALT_RATE_LIMIT),
            ("not logged in; run codex login", halt.HALT_AUTH),
            ("unauthorized", halt.HALT_AUTH),
            ("something ordinary went wrong", None),
        ],
    )
    def test_classify_halt(self, message, expected):
        backend = CodexCliBackend(argv_prefix=ARGV_PREFIX)
        assert backend.classify_halt(RuntimeError(message)) == expected


# ---------------------------------------------------------------------------
# runner alias (item 1: the rename must not break content-pipeline-kit)
# ---------------------------------------------------------------------------


def test_run_claude_streaming_alias_is_the_renamed_runner():
    """content-pipeline-kit imports the OLD name from this shared lib.

    A shared lib reaches every consumer at once with no version pin, so the
    rename to ``run_cli_streaming`` has to leave the old name bound to the very
    same object rather than to a wrapper.
    """
    assert run_claude_streaming is run_cli_streaming
