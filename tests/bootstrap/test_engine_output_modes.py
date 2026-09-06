"""Output-mode parity: the stdout (SessionStart) branch of emit_failure_response
and emit_success_response must render the SAME systemMessage body and the SAME
additionalContext as the background (output_file) branch -- only hookEventName
and the transport (print vs write-to-file) differ.

remediation-reference.md ("Claude's additionalContext is unaffected on both
paths: it always carries the complete log alongside the numbered remediation
steps") and engine-internals.md ("Non-background output ... is identical
except hookEventName") both assert this; before this test existed, nothing
pinned the stdout branch at all.
"""

import json

import bootstrap_lib.engine as engine


def _emit_failure_both_modes(failures, tmp_path, current_os="ubuntu",
                             log_content="log", label="mkt:bootstrap@test"):
    out = tmp_path / "pending.json"
    engine.emit_failure_response(
        failures, current_os=current_os, log_content=log_content,
        label=label, output_file=str(out))
    bg_payload = json.loads(out.read_text())
    return bg_payload


class TestFailureResponseModeParity:
    def test_stdout_systemmessage_matches_background_body(self, tmp_path, capsys):
        # Non-collapsed (no elevation aggregate) mixed case: this is the shape
        # where the stdout branch used to drop user_msg and the "Setup issues
        # found. Fix in order:" header entirely.
        failures = [
            {"type": "venv", "remediation_cmd": "python -m venv .venv"},
            {"type": "config", "name": "api-key", "agent_msg": "set OPENAI_API_KEY",
             "message": "OPENAI_API_KEY is not set"},
        ]
        bg = _emit_failure_both_modes(failures, tmp_path)

        engine.emit_failure_response(
            failures, current_os="ubuntu", log_content="log",
            label="mkt:bootstrap@test")
        stdout_payload = json.loads(capsys.readouterr().out)

        assert stdout_payload["systemMessage"] == bg["systemMessage"]

    def test_stdout_additionalcontext_carries_the_full_log(self, tmp_path, capsys):
        failures = [
            {"type": "config", "name": "api-key", "agent_msg": "set OPENAI_API_KEY",
             "message": "OPENAI_API_KEY is not set"},
        ]
        bg = _emit_failure_both_modes(failures, tmp_path, log_content="LOGBODY")

        engine.emit_failure_response(
            failures, current_os="ubuntu", log_content="LOGBODY",
            label="mkt:bootstrap@test")
        stdout_payload = json.loads(capsys.readouterr().out)

        bg_ac = bg["hookSpecificOutput"]["additionalContext"]
        stdout_ac = stdout_payload["hookSpecificOutput"]["additionalContext"]
        assert "LOGBODY" in stdout_ac
        assert stdout_ac == bg_ac
        assert stdout_ac == bg_ac  # both carry the full log verbatim


class TestSuccessResponseModeParity:
    def test_stdout_and_background_render_the_same_body(self, tmp_path, capsys):
        log_content = "some bootstrap log"
        out = tmp_path / "pending.json"
        engine.emit_success_response(log_content, label="mkt:bootstrap@1.0",
                                     output_file=str(out))
        bg = json.loads(out.read_text())

        engine.emit_success_response(log_content, label="mkt:bootstrap@1.0")
        stdout_payload = json.loads(capsys.readouterr().out)

        assert stdout_payload["systemMessage"] == bg["systemMessage"]
        assert (stdout_payload["hookSpecificOutput"]["additionalContext"]
                == bg["hookSpecificOutput"]["additionalContext"])
