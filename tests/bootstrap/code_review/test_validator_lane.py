"""Tests for the codex validator lane (bootstrap_lib.code_review.validator_lane).

The lane hands one candidate issue to a codex harness entry through llm-kit's
protocol-1 completion CLI and maps the strict-schema verdict onto the Agent
validator's one-line `CONFIRMED:` / `REJECTED:` output. The CLI is replaced by a
recording fake runner here; the live codex runs are recorded in the change that
introduced the lane.

Run: uv run --extra dev pytest tests/bootstrap/code_review/test_validator_lane.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bootstrap_lib.code_review import lane_prompts
from bootstrap_lib.code_review import validator_lane as vl

REPO_ROOT = Path(__file__).resolve().parents[3]
WRAPPERS = [
    REPO_ROOT / "plugins" / kit / "scripts" / "run_validator_lane.py"
    for kit in ("git-kit", "p4-kit")
]

CODEX_RESOLVE = {
    "backend": "codex-cli",
    "kind": "harness",
    "endpoint": "sol",
    "model": "gpt-6.1-sol",
}


class FakeRunner:
    """Answer `models resolve` and `completion complete` like the llm-kit CLI."""

    def __init__(
        self,
        *,
        resolve: dict[str, Any] | None = None,
        resolve_rc: int = 0,
        complete_rc: int = 0,
        envelope: dict[str, Any] | str | None = None,
    ) -> None:
        self.resolve = CODEX_RESOLVE if resolve is None else resolve
        self.resolve_rc = resolve_rc
        self.complete_rc = complete_rc
        self.envelope = envelope
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv: list[str], stdin: str | None, timeout: float) -> tuple[int, str, str]:
        self.calls.append((list(argv), stdin))
        if argv[:2] == ["models", "resolve"]:
            return self.resolve_rc, json.dumps(self.resolve), "resolve stderr"
        assert argv == ["completion", "complete", "--request-file", "-"], argv
        body = self.envelope if isinstance(self.envelope, str) else json.dumps(self.envelope)
        return self.complete_rc, body, "complete stderr"

    def request(self) -> dict[str, Any]:
        (stdin,) = [s for argv, s in self.calls if argv[:2] == ["completion", "complete"]]
        assert stdin is not None
        return json.loads(stdin)

    def completed(self) -> bool:
        return any(argv[:2] == ["completion", "complete"] for argv, _ in self.calls)


def _envelope(structured: Any, *, controls=("sandbox-mode",), backend: str = "codex-cli") -> dict:
    return {
        "protocol": 1,
        "endpoint": "sol",
        "kind": "harness",
        "backend": backend,
        "response": {
            "text": json.dumps(structured),
            "model": "gpt-6.1-sol",
            "status": "completed",
            "structured": structured,
            "execution_controls_applied": list(controls),
        },
    }


@pytest.fixture
def review(tmp_path: Path) -> dict[str, Path]:
    """A prepared bundle with two chunks and a governing CLAUDE.md."""
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    claude_md = project / "CLAUDE.md"
    claude_md.write_text("Use pathlib.Path for file paths.\n", encoding="utf-8")
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "chunk-0.diff").write_text("+CHUNK ZERO DIFF\n", encoding="utf-8")
    (bundle_dir / "chunk-1.diff").write_text("+CHUNK ONE DIFF\n", encoding="utf-8")
    bundle = {
        "bundle_dir": str(bundle_dir),
        "project_root": str(project),
        "description": "the change description",
        "diff_chunks": [
            {"path": "chunk-0.diff", "files": ["src/a.py"]},
            {"path": "chunk-1.diff", "files": ["src/b.py"]},
        ],
        "changed_files": [
            {"path": "src/a.py", "claude_mds": [str(claude_md)]},
            {"path": "src/b.py", "claude_mds": []},
        ],
    }
    bundle_path = bundle_dir / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    return {"bundle": bundle_path, "project": project, "tmp": tmp_path}


def _issue_file(tmp: Path, **overrides: Any) -> Path:
    issue = {
        "file": "src/a.py",
        "lines": "4",
        "reason": "bug",
        "description": "the loop never terminates",
    }
    issue.update(overrides)
    path = tmp / "issue.json"
    path.write_text(json.dumps(issue), encoding="utf-8")
    return path


def _run(review: dict[str, Path], runner: FakeRunner, capsys, *, chunk: int = 0, **issue: Any):
    argv = [
        "--issue", str(_issue_file(review["tmp"], **issue)),
        "--bundle", str(review["bundle"]),
        "--chunk-index", str(chunk),
        "--model", "sol",
        "--effort", "medium",
    ]
    code = vl.main(argv, runner=runner)
    out, err = capsys.readouterr()
    return code, out, err


class TestRequest:
    def test_request_is_read_only_without_network_under_a_strict_verdict_contract(
        self, review, capsys
    ) -> None:
        runner = FakeRunner(envelope=_envelope({"verdict": "CONFIRMED", "reason": "real"}))
        code, _out, _err = _run(review, runner, capsys)
        assert code == vl.EXIT_VERDICT
        request = runner.request()
        assert request["protocol"] == 1
        assert request["endpoint"] == "sol"
        options = request["options"]
        assert options["effort"] == "medium"
        assert options["cwd"] == str(review["project"])
        assert options["extras"] == {"sandbox": "read-only", "network": False}
        contract = options["output_contract"]
        assert contract["policy"] == "native-required"
        schema = contract["schema"]
        assert schema["additionalProperties"] is False
        assert sorted(schema["required"]) == sorted(schema["properties"]) == ["reason", "verdict"]
        assert schema["properties"]["verdict"]["enum"] == ["CONFIRMED", "REJECTED"]

    def test_the_request_parses_under_llm_kits_protocol_one(self, review, capsys) -> None:
        protocol = pytest.importorskip("llm_kit.completion.request_protocol")
        runner = FakeRunner(envelope=_envelope({"verdict": "REJECTED", "reason": "no"}))
        _run(review, runner, capsys)
        parsed = protocol.parse_request(runner.request())
        assert parsed.options.output_contract.strict_violations == ()
        assert dict(parsed.options.extras) == {"sandbox": "read-only", "network": False}

    def test_prompt_carries_only_the_originating_chunk(self, review, capsys) -> None:
        runner = FakeRunner(envelope=_envelope({"verdict": "CONFIRMED", "reason": "real"}))
        _run(review, runner, capsys, chunk=1, file="src/b.py")
        prompt = runner.request()["prompt"]
        assert "+CHUNK ONE DIFF" in prompt
        assert "+CHUNK ZERO DIFF" not in prompt
        assert "the change description" in prompt

    def test_claude_md_issue_carries_its_governing_text(self, review, capsys) -> None:
        runner = FakeRunner(envelope=_envelope({"verdict": "CONFIRMED", "reason": "real"}))
        _run(
            review, runner, capsys, reason="claude_md",
            citation="Use pathlib.Path for file paths.",
        )
        assert "Use pathlib.Path for file paths." in runner.request()["prompt"].split(
            "Governing CLAUDE.md", 1
        )[1]

    def test_bug_issue_carries_no_claude_md_text(self, review, capsys) -> None:
        runner = FakeRunner(envelope=_envelope({"verdict": "CONFIRMED", "reason": "real"}))
        _run(review, runner, capsys)
        assert "Governing CLAUDE.md" not in runner.request()["prompt"]

    def test_system_prompt_states_independence_and_the_shared_guardrails(
        self, review, capsys
    ) -> None:
        runner = FakeRunner(envelope=_envelope({"verdict": "CONFIRMED", "reason": "real"}))
        _run(review, runner, capsys)
        system = runner.request()["system"]
        assert system == vl.VALIDATOR_SYSTEM
        assert lane_prompts.GUARDRAILS in system
        for restriction in vl.VALIDATOR_RESTRICTIONS:
            assert restriction in system


class TestVerdictMapping:
    @pytest.mark.parametrize("verdict", ["CONFIRMED", "REJECTED"])
    def test_verdict_maps_onto_the_agent_one_line_form(self, review, capsys, verdict) -> None:
        runner = FakeRunner(
            envelope=_envelope({"verdict": verdict, "reason": "the loop\n has no exit"})
        )
        code, out, _err = _run(review, runner, capsys)
        assert code == vl.EXIT_VERDICT
        assert out == f"{verdict}: the loop has no exit\n"

    @pytest.mark.parametrize(
        "structured",
        [
            None,
            {"verdict": "MAYBE", "reason": "x"},
            {"verdict": "CONFIRMED", "reason": "   "},
            {"verdict": "CONFIRMED"},
            ["CONFIRMED"],
        ],
    )
    def test_malformed_verdict_is_unvalidated_not_rejected(
        self, review, capsys, structured
    ) -> None:
        runner = FakeRunner(envelope=_envelope(structured))
        code, out, err = _run(review, runner, capsys)
        assert code == vl.EXIT_MALFORMED
        assert out == ""
        assert "unvalidated" in err

    def test_contract_violation_is_unvalidated(self, review, capsys) -> None:
        envelope = _envelope(None)
        envelope["response"]["error"] = {
            "code": "output-contract-violation", "message": "schema mismatch",
        }
        runner = FakeRunner(complete_rc=1, envelope=envelope)
        code, out, _err = _run(review, runner, capsys)
        assert code == vl.EXIT_MALFORMED
        assert out == ""

    def test_unparseable_stdout_is_unvalidated(self, review, capsys) -> None:
        runner = FakeRunner(envelope="not json")
        code, out, _err = _run(review, runner, capsys)
        assert code == vl.EXIT_MALFORMED
        assert out == ""


class TestFailures:
    def test_a_non_codex_entry_is_refused_before_any_call(self, review, capsys) -> None:
        runner = FakeRunner(
            resolve={"backend": "claude-cli", "kind": "harness", "endpoint": "opus"},
        )
        code, out, err = _run(review, runner, capsys)
        assert code == vl.EXIT_REFUSED
        assert out == ""
        assert "claude-cli" in err
        assert not runner.completed()

    def test_a_failed_resolve_is_a_call_failure_before_any_call(self, review, capsys) -> None:
        runner = FakeRunner(resolve_rc=1)
        code, out, _err = _run(review, runner, capsys)
        assert code == vl.EXIT_CALL_FAILED
        assert out == ""
        assert not runner.completed()

    def test_a_failed_call_is_reselectable(self, review, capsys) -> None:
        envelope = _envelope(None)
        envelope["response"]["error"] = {"code": "execution", "message": "boom"}
        runner = FakeRunner(complete_rc=1, envelope=envelope)
        code, out, err = _run(review, runner, capsys)
        assert code == vl.EXIT_CALL_FAILED
        assert out == ""
        assert "boom" in err

    def test_a_halt_is_reported_as_a_halt(self, review, capsys) -> None:
        envelope = _envelope(None)
        envelope["response"]["error"] = {"code": "usage-limit", "message": "quota"}
        envelope["halt"] = {"kind": "usage-limit"}
        runner = FakeRunner(complete_rc=3, envelope=envelope)
        code, out, _err = _run(review, runner, capsys)
        assert code == vl.EXIT_HALT
        assert out == ""

    def test_a_protocol_refusal_is_a_refusal(self, review, capsys) -> None:
        runner = FakeRunner(complete_rc=4, envelope="")
        code, out, _err = _run(review, runner, capsys)
        assert code == vl.EXIT_REFUSED
        assert out == ""

    def test_a_verdict_from_an_unsandboxed_run_is_refused(self, review, capsys) -> None:
        runner = FakeRunner(
            envelope=_envelope({"verdict": "CONFIRMED", "reason": "x"}, controls=())
        )
        code, out, err = _run(review, runner, capsys)
        assert code == vl.EXIT_REFUSED
        assert out == ""
        assert "sandbox" in err

    def test_a_verdict_from_a_networked_run_is_refused(self, review, capsys) -> None:
        runner = FakeRunner(
            envelope=_envelope(
                {"verdict": "CONFIRMED", "reason": "x"},
                controls=("sandbox-mode", "network-enable"),
            )
        )
        code, out, _err = _run(review, runner, capsys)
        assert code == vl.EXIT_REFUSED
        assert out == ""

    def test_a_malformed_issue_is_refused_before_any_call(self, review, capsys) -> None:
        runner = FakeRunner()
        code, out, _err = _run(review, runner, capsys, reason="style")
        assert code == vl.EXIT_REFUSED
        assert out == ""
        assert runner.calls == []

    def test_an_out_of_range_chunk_is_refused_before_any_call(self, review, capsys) -> None:
        runner = FakeRunner()
        code, out, _err = _run(review, runner, capsys, chunk=5)
        assert code == vl.EXIT_REFUSED
        assert runner.calls == []


class TestWrappers:
    def test_both_kits_ship_byte_identical_wrappers(self) -> None:
        first, second = [path.read_bytes() for path in WRAPPERS]
        assert first == second

    def test_wrapper_dispatches_to_the_shared_module(self) -> None:
        body = WRAPPERS[0].read_text(encoding="utf-8")
        assert "from bootstrap_lib.code_review.validator_lane import main" in body
        assert "reexec_under_plugin_venv(_PLUGIN_NAME)" in body
