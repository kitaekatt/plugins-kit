"""Tests for git-kit's scripts/run_review_lane.py.

The completion seam is FAKED here rather than imported. llm_scripting_kit is not
on this suite's pythonpath (it is linked into plugin venvs by the bootstrap
shared-libs .pth, not installed), and a test that needed a live endpoint would
not be a unit test anyway. Faking it exercises exactly what this script owns --
the refusals, the budget pre-flight, the repair attempt, and the envelope --
without asserting anything about a transport it does not implement.

The script is byte-identical in p4-kit; the drift guard for that lives in
tests/bootstrap/test_run_review_lane_drift.py, so it is not re-tested here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

# conftest.py sets _BOOTSTRAP_GUARD_VENV_REEXEC before this import so the
# script's module-level reexec_under_plugin_venv() does not os.execv the pytest
# process into git-kit's provisioned venv.
assert os.environ.get("_BOOTSTRAP_GUARD_VENV_REEXEC") == "1"

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "git-kit" / "scripts" / "run_review_lane.py"
)
_spec = importlib.util.spec_from_file_location("git_kit_run_review_lane", _SCRIPT)
lr = importlib.util.module_from_spec(_spec)
sys.modules["git_kit_run_review_lane"] = lr
_spec.loader.exec_module(lr)


LANE = "reviewer_b_diff_only_bugs"
ONE_ISSUE = json.dumps(
    [{"file": "a.py", "lines": "4", "reason": "bug", "description": "boom"}]
)


class FakeHaltError(Exception):
    """Stand-in for llm_scripting_kit.completion.HaltError."""


class FakeEndpointResolveError(Exception):
    """Stand-in for llm_scripting_kit.models.EndpointResolveError."""


@dataclass
class FakeResponse:
    text: str
    model: str = "served-model"
    finish_reason: Optional[str] = "stop"
    input_tokens: int = 11
    output_tokens: int = 22


class FakeBackend:
    """Records every call and replays a scripted list of outcomes."""

    def __init__(self, outcomes: list[Any], name: str = "fake-transport") -> None:
        self.name = name
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def complete(self, system: str, user: str, *, model: str, options=None):
        self.calls.append(
            {"system": system, "user": user, "model": model, "options": options}
        )
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass
class FakeSelection:
    endpoint: str
    kind: str
    backend: Any
    model: str
    effort: Optional[str] = None


@dataclass
class FakeEntry:
    context_window: Optional[int] = None


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch):
    """Install a fake llm_scripting_kit into sys.modules.

    Returns a small control object the tests mutate to choose what
    ``create_backend`` returns and what the registry advertises.
    """

    class Control:
        selection: Optional[FakeSelection] = None
        resolve_error: Optional[Exception] = None
        entries: dict[str, FakeEntry] = {}

    control = Control()

    @dataclass
    class BackendOptions:
        max_tokens: int = 4096
        temperature: float = 0.3
        timeout_s: Optional[float] = None
        cache_salt: int = 0
        user_cache_prefix: str = ""
        effort: Optional[str] = None
        allowed_tools: Optional[str] = None
        disallowed_tools: Optional[str] = None
        system_prompt_mode: str = "replace"
        cwd: Optional[Path] = None
        log_prefix: str = "[llm]"

    def create_backend(endpoint, *, project_root=None, **_kwargs):
        if control.resolve_error is not None:
            raise control.resolve_error
        return control.selection

    root = types.ModuleType("llm_scripting_kit")
    completion = types.ModuleType("llm_scripting_kit.completion")
    completion.BackendOptions = BackendOptions
    completion.HaltError = FakeHaltError
    completion.create_backend = create_backend
    models = types.ModuleType("llm_scripting_kit.models")
    models.EndpointResolveError = FakeEndpointResolveError
    models.discover_model_entries = lambda **_kwargs: control.entries
    root.completion = completion
    root.models = models

    monkeypatch.setitem(sys.modules, "llm_scripting_kit", root)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion", completion)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.models", models)
    return control


def _transport(outcomes: list[Any], endpoint: str = "my-endpoint") -> FakeSelection:
    return FakeSelection(
        endpoint=endpoint, kind="transport", backend=FakeBackend(outcomes), model="m"
    )


class TestDispatchRefusals:
    """Every refusal below is a fact about the lane, not a user preference."""

    @pytest.mark.parametrize("alias", ["sonnet", "opus", "haiku", "fable"])
    def test_an_agent_alias_is_refused(self, alias: str) -> None:
        with pytest.raises(lr.LaneConfigError, match="Agent-tool alias"):
            lr.check_lane_dispatchable(LANE, alias)

    def test_an_unknown_lane_is_refused(self) -> None:
        with pytest.raises(lr.LaneConfigError, match="not a review lane"):
            lr.check_lane_dispatchable("reviewer_z", "my-endpoint")

    def test_an_ineligible_lane_is_refused_by_name(self) -> None:
        """The validator especially: it is the control, not a candidate."""
        with pytest.raises(lr.LaneConfigError, match="not eligible"):
            lr.check_lane_dispatchable("validator", "my-endpoint")

    def test_the_eligible_lane_passes(self) -> None:
        lr.check_lane_dispatchable(LANE, "my-endpoint")


class TestRunLane:
    def test_returns_issues_and_an_audit_envelope(self, seam) -> None:
        seam.selection = _transport([FakeResponse(ONE_ISSUE)])
        result = lr.run_lane(lane=LANE, model="my-endpoint", diff_text="d", files=["a.py"])
        assert result["issues"] == [
            {"file": "a.py", "lines": "4", "reason": "bug", "description": "boom"}
        ]
        assert result["endpoint"] == "my-endpoint"
        assert result["backend"] == "fake-transport"
        assert result["served_model"] == "served-model"
        assert result["configured_model"] == "my-endpoint"
        assert result["attempts"] == 1
        assert result["prompt_version"]

    def test_sends_the_canonical_prompt_and_inlines_the_diff(self, seam) -> None:
        """A completion has no file access, so a chunk PATH would be useless."""
        from bootstrap_lib.code_review import lane_prompts

        seam.selection = _transport([FakeResponse("[]")])
        lr.run_lane(lane=LANE, model="my-endpoint", diff_text="UNIQUE-DIFF-TEXT")
        call = seam.selection.backend.calls[0]
        assert call["system"] == lane_prompts.REVIEWER_B_SYSTEM
        assert "UNIQUE-DIFF-TEXT" in call["user"]

    def test_temperature_is_pinned_to_zero(self, seam) -> None:
        """Two runs over one diff that disagree are not two opinions."""
        seam.selection = _transport([FakeResponse("[]")])
        lr.run_lane(lane=LANE, model="my-endpoint", diff_text="d")
        assert seam.selection.backend.calls[0]["options"].temperature == 0.0

    def test_an_unknown_endpoint_is_a_config_error(self, seam) -> None:
        seam.resolve_error = FakeEndpointResolveError("no such endpoint")
        with pytest.raises(lr.LaneConfigError, match="neither an Agent-tool alias"):
            lr.run_lane(lane=LANE, model="typo-endpoint", diff_text="d")

    def test_a_halt_is_a_lane_failure(self, seam) -> None:
        seam.selection = _transport([FakeHaltError("out of credit")])
        with pytest.raises(lr.LaneRunError, match="halted"):
            lr.run_lane(lane=LANE, model="my-endpoint", diff_text="d")

    def test_a_transport_error_is_a_lane_failure(self, seam) -> None:
        seam.selection = _transport([ConnectionError("refused")])
        with pytest.raises(lr.LaneRunError, match="ConnectionError"):
            lr.run_lane(lane=LANE, model="my-endpoint", diff_text="d")


class TestAgentLoopRefusal:
    def test_a_repo_reading_lane_is_refused_on_a_transport(self, monkeypatch) -> None:
        """Refused rather than degraded: a completion cannot fetch the files."""
        monkeypatch.setattr(
            lr, "ENDPOINT_ELIGIBLE_LANES", frozenset({"reviewer_c_introduced_code"})
        )
        selection = FakeSelection(
            endpoint="e", kind="transport", backend=FakeBackend([]), model="m"
        )
        with pytest.raises(lr.LaneConfigError, match="needs an agent loop"):
            lr._check_selection("reviewer_c_introduced_code", selection)

    def test_a_harness_selection_is_accepted(self) -> None:
        selection = FakeSelection(
            endpoint="e", kind="harness", backend=FakeBackend([]), model="m"
        )
        lr._check_selection("reviewer_c_introduced_code", selection)

    def test_an_agent_loop_lane_is_actually_granted_read(self) -> None:
        """Passing the harness check must GRANT the capability it checked for.

        claude-cli renders allowed_tools=None as `--allowedTools ""` -- an
        allow-nothing list, not an absent flag -- so without this the lane would
        be admitted as needing an agent loop and then handed a tool-less
        completion holding a prompt that tells it to read files.
        """
        assert lr._allowed_tools_for("reviewer_c_introduced_code") == "Read"

    def test_a_diff_only_lane_stays_a_pure_completion(self) -> None:
        assert lr._allowed_tools_for(LANE) is None

    def test_the_repair_attempt_keeps_the_tool_grant(self, seam) -> None:
        """Rebuilding BackendOptions must not silently drop a field."""
        seam.selection = _transport([FakeResponse("nope"), FakeResponse(ONE_ISSUE)])
        lr.run_lane(lane=LANE, model="my-endpoint", diff_text="d")
        first, second = seam.selection.backend.calls
        assert second["options"].allowed_tools == first["options"].allowed_tools
        assert second["options"].effort == first["options"].effort


class TestContextBudget:
    def test_an_oversized_chunk_fails_before_dispatch(self, seam) -> None:
        """Truncation is invisible, so it is refused rather than risked."""
        seam.selection = _transport([FakeResponse("[]")])
        seam.entries = {"my-endpoint": FakeEntry(context_window=1000)}
        with pytest.raises(lr.LaneRunError, match="does not fit endpoint"):
            lr.run_lane(
                lane=LANE,
                model="my-endpoint",
                diff_text="x" * 100_000,
                max_output_tokens=256,
            )
        assert seam.selection.backend.calls == []

    def test_a_fitting_chunk_dispatches(self, seam) -> None:
        seam.selection = _transport([FakeResponse("[]")])
        seam.entries = {"my-endpoint": FakeEntry(context_window=100_000)}
        lr.run_lane(lane=LANE, model="my-endpoint", diff_text="x" * 300)
        assert len(seam.selection.backend.calls) == 1

    def test_an_unknown_window_skips_the_check(self, seam) -> None:
        """An unstated limit must not be invented and used to refuse work."""
        seam.selection = _transport([FakeResponse("[]")])
        seam.entries = {"my-endpoint": FakeEntry(context_window=None)}
        lr.run_lane(lane=LANE, model="my-endpoint", diff_text="x" * 500_000)
        assert len(seam.selection.backend.calls) == 1


class TestOutputRepair:
    def test_one_repair_attempt_can_recover(self, seam) -> None:
        seam.selection = _transport([FakeResponse("Sure! Here you go."), FakeResponse(ONE_ISSUE)])
        result = lr.run_lane(lane=LANE, model="my-endpoint", diff_text="d")
        assert result["attempts"] == 2
        assert len(result["issues"]) == 1
        assert "did not parse" in seam.selection.backend.calls[1]["user"]

    def test_a_second_failure_fails_the_lane(self, seam) -> None:
        """Bounded at one: a third ask buys nothing and is unbounded spend."""
        seam.selection = _transport([FakeResponse("nope"), FakeResponse("still nope")])
        with pytest.raises(lr.LaneRunError, match="after 2 attempt"):
            lr.run_lane(lane=LANE, model="my-endpoint", diff_text="d")
        assert len(seam.selection.backend.calls) == 2

    def test_a_length_stop_names_the_output_budget(self, seam) -> None:
        truncated = FakeResponse("[{", finish_reason="length")
        seam.selection = _transport([truncated, truncated])
        with pytest.raises(lr.LaneRunError, match="max-output-tokens"):
            lr.run_lane(lane=LANE, model="my-endpoint", diff_text="d")

    def test_a_repair_uses_a_fresh_cache_salt(self, seam) -> None:
        """Otherwise a cached bad response is replayed as the repair."""
        seam.selection = _transport([FakeResponse("nope"), FakeResponse(ONE_ISSUE)])
        lr.run_lane(lane=LANE, model="my-endpoint", diff_text="d")
        assert seam.selection.backend.calls[1]["options"].cache_salt == 1


class TestNoSilentFallback:
    def test_the_module_offers_no_agent_fallback(self) -> None:
        """A silent fallback would misreport what reviewed the change.

        Asserted structurally rather than behaviorally: the guarantee is that
        no such path can be added without this test being deleted on purpose.
        """
        source = _SCRIPT.read_text(encoding="utf-8")
        assert "fallback_to_agent" not in source
        assert "no fallback" in source.lower()


class TestCli:
    def test_success_prints_the_envelope(self, seam, tmp_path, capsys) -> None:
        chunk = tmp_path / "c.diff"
        chunk.write_text("diff --git a/a.py b/a.py", encoding="utf-8")
        seam.selection = _transport([FakeResponse(ONE_ISSUE)])
        code = lr.main(
            ["--lane", LANE, "--model", "my-endpoint", "--chunk", str(chunk), "--file", "a.py"]
        )
        assert code == lr.EXIT_OK
        assert json.loads(capsys.readouterr().out)["issues"][0]["file"] == "a.py"

    def test_a_config_error_exits_two(self, seam, tmp_path, capsys) -> None:
        chunk = tmp_path / "c.diff"
        chunk.write_text("d", encoding="utf-8")
        code = lr.main(
            ["--lane", "validator", "--model", "my-endpoint", "--chunk", str(chunk)]
        )
        assert code == lr.EXIT_USAGE
        assert "not eligible" in capsys.readouterr().err

    def test_a_lane_failure_exits_one(self, seam, tmp_path, capsys) -> None:
        chunk = tmp_path / "c.diff"
        chunk.write_text("d", encoding="utf-8")
        seam.selection = _transport([FakeHaltError("rate limited")])
        code = lr.main([
            "--lane", LANE, "--model", "my-endpoint", "--chunk", str(chunk)
        ])
        assert code == lr.EXIT_LANE_FAILED
        assert "halted" in capsys.readouterr().err

    def test_a_missing_chunk_exits_two(self, seam, tmp_path, capsys) -> None:
        code = lr.main([
            "--lane", LANE, "--model", "my-endpoint",
            "--chunk", str(tmp_path / "nope.diff"),
        ])
        assert code == lr.EXIT_USAGE
        assert "cannot read chunk" in capsys.readouterr().err
