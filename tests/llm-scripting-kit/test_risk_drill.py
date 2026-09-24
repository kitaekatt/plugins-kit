"""Biggest-risk drill, process layer (R30, R31).

Source: docs/planning/quota-resilient-dispatch/declaration-format-design.md,
section "Biggest risk", and requirements.md R30/R31. Report:
docs/planning/quota-resilient-dispatch/drill-report.md.

THE FIXTURE IS SIMULATED (owner ruling, 2026-09-23). No real codex quota
window is used. A fake ``codex`` executable stands in for the CLI: on a
"quota" run it appends a rollout in the real ``usage_limit_exceeded`` shape
(the shape ``test_completion_codex_backend.py::_write_exhausted_rollout``
reproduces) under a scratch sessions directory, prints the usage-limit text
on stderr and exits 1, exactly as the real CLI leaves its evidence.

What runs for real, end to end: ``llm_scripting_kit.run()`` and
``describe()``, the real ``CodexCliBackend`` driving a real subprocess
through the default runner, its rollout re-read (``read_codex_pool``), the
real pinned-verdict cache (``pinned_evaluate`` / ``record_observed_halt``,
redirected to a scratch file), and the real git workspace snapshot. Only the
second entry's backend and the reachability probe are injected.

The three drill assertions:
  1. A multi-entry row whose codex entry is out of quota re-selects a usable
     entry, and the codex entry then reads "out of quota" with its reset.
  2. A broken unit on a usable entry that exits 0 with a WRONG result is a
     task failure and is NOT re-routed.
  3. A unit that halts after writing is re-run only after its workspace is
     reset to its launch state.

Assertion 1 also has an IN-SESSION form (drill finding F1): an agent that
drives codex itself sees the halt, not ``run()``, so the pinned AVAILABLE
verdict stays until the agent runs ``llm-scripting-kit record-halt <entry>``
as the rendered Re-select line tells it to.

Each assertion has a paired ``TestDrillGoesRed`` case that breaks the
behaviour it depends on (monkeypatch only; no shipped code is edited) and
shows the drill check fails.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

from llm_scripting_kit import declaration as decl
from llm_scripting_kit import usage_budget
from llm_scripting_kit.completion.codex_backend import CodexCliBackend
from llm_scripting_kit.completion.halt import HALT_QUOTA
from llm_scripting_kit.completion.types import BackendOptions
from llm_scripting_kit.model_endpoints import HARNESS_KIND, EndpointEntry
from llm_scripting_kit.models import EndpointResolveError
from llm_scripting_kit.reachability import STATUS_REACHABLE, Reachability

SEVEN_DAY = usage_budget.ConserveSpec(pool="seven_day")
EXPECTED = "ANSWER: 4"
WRONG = "ANSWER: 5"

# A real-shaped codex usage-limit message; the reset clause is one year out so
# the parsed reset time is always in the future when the drill runs.
_RESET_YEAR = time.gmtime().tm_year + 1
USAGE_LIMIT_TEXT = (
    "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
    f"to purchase more credits or try again at Jan 20th, {_RESET_YEAR} 3:34 PM."
)

FAKE_CODEX = r"""#!/bin/sh
# Simulated codex CLI for the risk drill. Modes (DRILL_CODEX_MODE):
#   quota  -- optionally write into the cwd, append an exhausted rollout,
#             print the usage-limit error, exit 1
#   wrong  -- write a WRONG answer to the -o file, exit 0
#   right  -- write the expected answer to the -o file, exit 0
#   broken -- print an ordinary error, exit 1 (no quota evidence)
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
cat > /dev/null
case "$DRILL_CODEX_MODE" in
  quota)
    if [ "$DRILL_CODEX_WRITE" = "1" ]; then
      printf 'partial edit by codex\n' > kept.txt
      printf 'half-finished\n' > codex-scratch.txt
    fi
    d="$DRILL_CODEX_SESSIONS/2026/09/23"
    mkdir -p "$d"
    printf '%s\n' "$DRILL_ROLLOUT_LINE1" "$DRILL_ROLLOUT_LINE2" > "$d/rollout-drill-exhausted.jsonl"
    echo "ERROR: $DRILL_USAGE_TEXT" >&2
    exit 1 ;;
  wrong)  printf '%s' "$DRILL_WRONG" > "$out"; exit 0 ;;
  right)  printf '%s' "$DRILL_EXPECTED" > "$out"; exit 0 ;;
  broken) echo "ERROR: unit crashed: KeyError 'x'" >&2; exit 1 ;;
esac
echo "fake codex: unknown mode" >&2
exit 2
"""


def _exhausted_rollout_lines() -> tuple[str, str]:
    rate_limits = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": None,
                "secondary": None,
                "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
            },
        },
    }
    error = {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "error": {"message": USAGE_LIMIT_TEXT, "codex_error_info": "usage_limit_exceeded"},
        },
    }
    return json.dumps(rate_limits), json.dumps(error)


def _healthy_rollout(sessions: Path) -> None:
    """A pre-existing healthy reading, so codex starts out AVAILABLE and first."""
    day = sessions / "2026" / "09" / "22"
    day.mkdir(parents=True)
    reading = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": {
                    "used_percent": 5.0,
                    "resets_at": int(time.time()) + 4 * 3600,
                    "window_minutes": 300,
                },
            },
        },
    }
    path = day / "rollout-drill-healthy.jsonl"
    path.write_text(json.dumps(reading) + "\n")
    past = time.time() - 3600
    os.utime(path, (past, past))


class _ScriptedClaude:
    """The usable second entry. Records what the workspace looked like."""

    name = "claude-cli"

    def __init__(self, answer: str = EXPECTED, workspace: Optional[Path] = None) -> None:
        self.answer = answer
        self.workspace = workspace
        self.calls = 0
        self.seen: dict[str, Any] = {}

    def complete(self, system, user, *, model, options=None):
        self.calls += 1
        if self.workspace is not None:
            self.seen = {
                "kept": (self.workspace / "kept.txt").read_text(),
                "codex_scratch_exists": (self.workspace / "codex-scratch.txt").exists(),
                "launch_dirty": (self.workspace / "launch-dirty.txt").read_text(),
            }
        return self.answer

    def classify_halt(self, exc):
        return None


@dataclass
class _Selection:
    endpoint: str
    kind: str
    backend: Any
    model: str
    effort: Optional[str] = None


def _entries() -> dict[str, EndpointEntry]:
    return {
        "codex": EndpointEntry(
            id="codex", base_url=None, model="gpt-5.4-codex", kind=HARNESS_KIND,
            harness="codex", conserve_usage=SEVEN_DAY,
        ),
        "opus": EndpointEntry(
            id="opus", base_url=None, model="claude-opus", kind=HARNESS_KIND,
            harness="claude",
        ),
    }


def _sibling_entries() -> dict[str, EndpointEntry]:
    """F4: a second entry on the same codex account and pool, and one on another pool."""
    entries = _entries()
    entries["codex-mini"] = EndpointEntry(
        id="codex-mini", base_url=None, model="gpt-5.4-codex-mini", kind=HARNESS_KIND,
        harness="codex", conserve_usage=SEVEN_DAY,
    )
    entries["codex-5h"] = EndpointEntry(
        id="codex-5h", base_url=None, model="gpt-5.4-codex-5h", kind=HARNESS_KIND,
        harness="codex", conserve_usage=usage_budget.ConserveSpec(pool="primary"),
    )
    return entries


def _reach() -> Reachability:
    return Reachability(status=STATUS_REACHABLE, checked="cli-version", detail="drill")


@dataclass
class Drill:
    root: Path
    sessions: Path
    verdicts: Path
    codex: CodexCliBackend

    def factory(self, claude: _ScriptedClaude):
        backends = {
            "codex": self.codex, "codex-mini": self.codex, "codex-5h": self.codex,
            "opus": claude,
        }

        def make(name, **_kw):
            if name not in backends:
                raise EndpointResolveError(f"unknown endpoint '{name}'")
            model = _sibling_entries()[name].model
            return _Selection(name, HARNESS_KIND, backends[name], model)

        return make

    def dispatch(self, names, claude, *, workspace=None, max_attempts=3, entries=None):
        attempts: list[decl.Attempt] = []
        cwd = workspace if workspace is not None else self.root
        result = decl.run(
            names,
            decl.RunRequest(
                system="You are a drill unit.", prompt="What is 2 + 2?",
                options=BackendOptions(cwd=cwd, timeout_s=30),
                workspace=workspace,
            ),
            entries=entries if entries is not None else _entries(),
            backend_factory=self.factory(claude),
            reachability_cache={name: _reach() for name in names},
            max_attempts=max_attempts, on_attempt=attempts.append,
        )
        return result, attempts

    def menu(self, names=("codex", "opus"), entries=None) -> decl.Ranking:
        """What a later dispatch in the same session is shown."""
        return decl.describe(
            list(names), caller="session",
            entries=entries if entries is not None else _entries(),
            backend_factory=self.factory(_ScriptedClaude()),
            reachability_cache={name: _reach() for name in names},
        )


@pytest.fixture
def drill(tmp_path, monkeypatch) -> Drill:
    root = tmp_path / "drill"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "codex"
    fake.write_text(FAKE_CODEX)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    sessions = root / "codex-sessions"
    sessions.mkdir()
    _healthy_rollout(sessions)
    verdicts = root / "usage-verdicts.json"

    # Every HOME-relative quota path goes to scratch; the real verdict cache
    # and the real ~/.codex/sessions are never read or written.
    monkeypatch.setattr(usage_budget, "VERDICT_CACHE", verdicts)
    monkeypatch.setattr(usage_budget, "CODEX_SESSIONS_DIR", sessions)
    monkeypatch.setenv("LLM_SCRIPTING_KIT_USAGE_SESSION", "risk-drill-session")
    line1, line2 = _exhausted_rollout_lines()
    monkeypatch.setenv("DRILL_CODEX_SESSIONS", str(sessions))
    monkeypatch.setenv("DRILL_ROLLOUT_LINE1", line1)
    monkeypatch.setenv("DRILL_ROLLOUT_LINE2", line2)
    monkeypatch.setenv("DRILL_USAGE_TEXT", USAGE_LIMIT_TEXT)
    monkeypatch.setenv("DRILL_EXPECTED", EXPECTED)
    monkeypatch.setenv("DRILL_WRONG", WRONG)
    monkeypatch.delenv("DRILL_CODEX_WRITE", raising=False)

    codex = CodexCliBackend(argv_prefix=(str(fake),), sessions_dir=sessions)
    return Drill(root=root, sessions=sessions, verdicts=verdicts, codex=codex)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def workspace(tmp_path) -> Path:
    repo = tmp_path / "ws"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "drill@example.invalid")
    _git(repo, "config", "user.name", "drill")
    (repo / "kept.txt").write_text("original\n")
    _git(repo, "add", "kept.txt")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "launch-dirty.txt").write_text("user work at launch\n")
    return repo


# ---------------------------------------------------------------------------
# The drill checks. Each returns its evidence and raises AssertionError on a
# failed drill assertion, so the red cases below can reuse them unchanged.
# ---------------------------------------------------------------------------


def check_assertion_1(drill: Drill, monkeypatch) -> dict[str, Any]:
    before = drill.menu()
    assert before.default is not None and before.default.id == "codex", before.render()

    monkeypatch.setenv("DRILL_CODEX_MODE", "quota")
    claude = _ScriptedClaude()
    result, attempts = drill.dispatch(["codex", "opus"], claude)

    assert [(a.entry, a.halt, a.outcome) for a in attempts] == [
        ("codex", HALT_QUOTA, "halted"),
        ("opus", None, "completed"),
    ], [a.to_json() for a in attempts]
    assert result.status == decl.RUN_COMPLETED and result.entry == "opus", result
    assert result.response == EXPECTED

    after = drill.menu()
    codex_state = next(e for e in after.rendered_entries if e.id == "codex")
    assert codex_state.usability == usage_budget.STATUS_OUT_OF_QUOTA, after.render()
    assert codex_state.status_text.startswith("out of quota until "), after.render()
    assert str(_RESET_YEAR) in codex_state.status_text, after.render()
    assert after.default is not None and after.default.id == "opus", after.render()
    disposition = next(d for d in after.dispositions if d.id == "codex")
    return {
        "menu_before": before.render(),
        "attempts": [a.to_json() for a in attempts],
        "menu_after": after.render(),
        "floor_line": disposition.describe_line(),
        "announcement": f"route: drill-unit -> {result.entry}; "
                        f"{attempts[0].entry} failed: {attempts[0].halt}",
    }


def check_assertion_2(drill: Drill, monkeypatch) -> dict[str, Any]:
    monkeypatch.setenv("DRILL_CODEX_MODE", "wrong")
    claude = _ScriptedClaude()
    result, attempts = drill.dispatch(["codex", "opus"], claude)

    assert len(attempts) == 1 and attempts[0].entry == "codex", [a.to_json() for a in attempts]
    assert result.status == decl.RUN_COMPLETED and result.entry == "codex", result
    assert claude.calls == 0, "a wrong exit-0 result was re-routed to another entry"
    # The caller's own validation is what reports the task failure.
    task_failed = result.response.text != EXPECTED
    assert task_failed, result.response.text

    # The same unit crashing (non-zero exit, no quota evidence) is also a
    # task failure for an unattended caller: a failed attempt, not a re-route.
    monkeypatch.setenv("DRILL_CODEX_MODE", "broken")
    crashed, crash_attempts = drill.dispatch(["codex", "opus"], claude)
    assert crashed.status == decl.RUN_FAILED and crashed.entry == "codex", crashed
    assert [a.outcome for a in crash_attempts] == ["failed"]
    assert claude.calls == 0, "an unclassified task error was re-routed"

    codex_state = next(e for e in drill.menu().rendered_entries if e.id == "codex")
    assert codex_state.usable, "a task failure changed codex's quota verdict"
    return {
        "attempts": [a.to_json() for a in attempts],
        "response": result.response.text,
        "crash_detail": crashed.detail,
    }


def check_assertion_3(drill: Drill, monkeypatch, workspace: Path) -> dict[str, Any]:
    monkeypatch.setenv("DRILL_CODEX_MODE", "quota")
    monkeypatch.setenv("DRILL_CODEX_WRITE", "1")
    claude = _ScriptedClaude(workspace=workspace)
    result, attempts = drill.dispatch(["codex", "opus"], claude, workspace=workspace)

    assert result.status == decl.RUN_COMPLETED and result.entry == "opus", result
    assert attempts[0].halt == HALT_QUOTA
    assert "workspace reset to its launch state" in attempts[0].workspace_action, attempts[0]
    assert claude.seen == {
        "kept": "original\n",
        "codex_scratch_exists": False,
        "launch_dirty": "user work at launch\n",
    }, f"re-run saw another model's partial edits: {claude.seen}"
    return {"attempts": [a.to_json() for a in attempts], "seen_by_rerun": claude.seen}


def check_assertion_3_unresettable(drill: Drill, monkeypatch, tmp_path: Path) -> dict[str, Any]:
    """A written workspace that cannot be reset is never re-run elsewhere."""
    monkeypatch.setenv("DRILL_CODEX_MODE", "quota")
    monkeypatch.setenv("DRILL_CODEX_WRITE", "1")
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / "kept.txt").write_text("original\n")
    claude = _ScriptedClaude()
    result, attempts = drill.dispatch(["codex", "opus"], claude, workspace=plain)
    assert result.status == decl.RUN_FAILED, result
    assert claude.calls == 0, "a unit was re-run on a workspace that was not reset"
    assert "could not be reset" in result.detail
    return {"detail": result.detail}


def check_assertion_1_in_session(drill: Drill, monkeypatch) -> dict[str, Any]:
    """F1: a halt the AGENT observed is written back by the record-halt verb."""
    from llm_scripting_kit import cli

    before = drill.menu()
    assert before.default is not None and before.default.id == "codex", before.render()

    # The agent drives codex itself (a session caller); run() never sees it.
    env = dict(os.environ, DRILL_CODEX_MODE="quota")
    halted = subprocess.run(
        [drill.codex.argv_prefix[0], "exec", "-"], input="", env=env,
        capture_output=True, text=True, cwd=drill.root,
    )
    assert halted.returncode == 1 and "usage limit" in halted.stderr
    stale = drill.menu()
    assert stale.default is not None and stale.default.id == "codex", (
        "the pinned AVAILABLE verdict no longer outlives an unrecorded halt; "
        "re-check whether record-halt is still needed"
    )

    # What the Re-select line tells the agent to run.
    assert "llm-scripting-kit record-halt <entry>" in decl.RULE_TRIGGER_SESSION
    monkeypatch.setattr(cli, "discover_model_entries", lambda **_kw: _entries())
    assert cli.main(["record-halt", "codex"]) == cli.EXIT_OK

    after = drill.menu()
    codex_state = next(e for e in after.rendered_entries if e.id == "codex")
    assert codex_state.usability == usage_budget.STATUS_OUT_OF_QUOTA, after.render()
    assert str(_RESET_YEAR) in codex_state.status_text, after.render()
    assert after.default is not None and after.default.id == "opus", after.render()
    return {"menu_before": before.render(), "menu_stale": stale.render(),
            "menu_after": after.render()}


SIBLINGS = ("codex", "codex-mini", "codex-5h", "opus")


def check_sibling_in_session(drill: Drill, monkeypatch) -> dict[str, Any]:
    """F4: record-halt on one entry spends every entry sharing its pool."""
    from llm_scripting_kit import cli

    entries = _sibling_entries()
    before = drill.menu(SIBLINGS, entries)
    mini = next(e for e in before.rendered_entries if e.id == "codex-mini")
    assert mini.usable and "codex" in mini.shares_quota_with, before.render()

    monkeypatch.setattr(cli, "discover_model_entries", lambda **_kw: entries)
    assert cli.main(["record-halt", "codex"]) == cli.EXIT_OK

    after = drill.menu(SIBLINGS, entries)
    states = {e.id: e for e in after.rendered_entries}
    for name in ("codex", "codex-mini"):
        assert states[name].usability == usage_budget.STATUS_OUT_OF_QUOTA, after.render()
    # A different pool on the same account is a different quota.
    assert states["codex-5h"].usable, after.render()
    assert after.default is not None and after.default.id not in ("codex", "codex-mini"), after.render()
    return {"menu_before": before.render(), "menu_after": after.render()}


def check_sibling_run(drill: Drill, monkeypatch) -> dict[str, Any]:
    """F4, process layer: run() does not spend a dispatch on a spent sibling."""
    entries = _sibling_entries()
    names = ["codex", "codex-mini", "opus"]
    before = drill.menu(names, entries)
    first = before.default.id if before.default is not None else None
    assert first in ("codex", "codex-mini"), before.render()
    sibling = "codex-mini" if first == "codex" else "codex"

    monkeypatch.setenv("DRILL_CODEX_MODE", "quota")
    claude = _ScriptedClaude()
    result, attempts = drill.dispatch(names, claude, entries=entries)
    assert [(a.entry, a.outcome) for a in attempts] == [
        (first, "halted"), ("opus", "completed"),
    ], [a.to_json() for a in attempts]
    assert result.entry == "opus", result
    spent = next(e for e in drill.menu(names, entries).rendered_entries if e.id == sibling)
    assert spent.usability == usage_budget.STATUS_OUT_OF_QUOTA
    return {"attempts": [a.to_json() for a in attempts]}


# ---------------------------------------------------------------------------
# The drill
# ---------------------------------------------------------------------------


class TestRiskDrill:
    def test_assertion_1_out_of_quota_codex_reselects_a_usable_entry(self, drill, monkeypatch):
        evidence = check_assertion_1(drill, monkeypatch)
        print(json.dumps(evidence, indent=2))

    def test_assertion_1_in_session_halt_is_recorded_by_record_halt(self, drill, monkeypatch, capsys):
        evidence = check_assertion_1_in_session(drill, monkeypatch)
        print(json.dumps(evidence, indent=2))

    def test_assertion_1_in_session_halt_spends_the_shared_pool(self, drill, monkeypatch):
        evidence = check_sibling_in_session(drill, monkeypatch)
        print(json.dumps(evidence, indent=2))

    def test_assertion_1_run_halt_spends_the_shared_pool(self, drill, monkeypatch):
        evidence = check_sibling_run(drill, monkeypatch)
        print(json.dumps(evidence, indent=2))

    def test_assertion_2_wrong_exit_zero_result_is_a_task_failure(self, drill, monkeypatch):
        evidence = check_assertion_2(drill, monkeypatch)
        print(json.dumps(evidence, indent=2))

    def test_assertion_3_halted_writer_reruns_only_after_reset(self, drill, monkeypatch, workspace):
        evidence = check_assertion_3(drill, monkeypatch, workspace)
        print(json.dumps(evidence, indent=2))

    def test_assertion_3_unresettable_workspace_stops(self, drill, monkeypatch, tmp_path):
        evidence = check_assertion_3_unresettable(drill, monkeypatch, tmp_path)
        print(json.dumps(evidence, indent=2))


class TestDrillGoesRed:
    """Each drill check fails when the behaviour it guards is broken."""

    def test_1_red_when_the_rollout_re_read_is_lost(self, drill, monkeypatch):
        # Step 0 regressed: a quota exit reads as an ordinary task error.
        monkeypatch.setattr(CodexCliBackend, "_quota_probe", lambda self: None)
        with pytest.raises(AssertionError):
            check_assertion_1(drill, monkeypatch)

    def test_1_red_when_the_verdict_is_not_written_back(self, drill, monkeypatch):
        # Re-selection happens, but a later menu still offers codex as default.
        monkeypatch.setattr(decl, "record_observed_halt", lambda *a, **k: None)
        with pytest.raises(AssertionError):
            check_assertion_1(drill, monkeypatch)

    def test_1_in_session_red_when_record_halt_writes_nothing(self, drill, monkeypatch):
        monkeypatch.setattr(usage_budget, "record_observed_halt", lambda *a, **k: None)
        with pytest.raises(AssertionError):
            check_assertion_1_in_session(drill, monkeypatch)

    @staticmethod
    def _write_one_entry_only(monkeypatch):
        real = usage_budget.record_observed_halt

        def single(entry_id, spec, **kw):
            kw.pop("entries", None)
            return real(entry_id, spec, **kw)

        monkeypatch.setattr(usage_budget, "record_observed_halt", single)
        monkeypatch.setattr(decl, "record_observed_halt", single)

    def test_1_sibling_in_session_red_when_one_entry_is_written(self, drill, monkeypatch):
        self._write_one_entry_only(monkeypatch)
        with pytest.raises(AssertionError, match="out of quota|OUT_OF_QUOTA|out-of-quota"):
            check_sibling_in_session(drill, monkeypatch)

    def test_1_sibling_run_red_when_one_entry_is_written(self, drill, monkeypatch):
        self._write_one_entry_only(monkeypatch)
        with pytest.raises(AssertionError, match=r"At index 1 diff: \('codex(-mini)?', 'halted'\)"):
            check_sibling_run(drill, monkeypatch)

    def test_2_red_when_a_completed_run_is_re_routed(self, drill, monkeypatch):
        # A backend that validates and re-routes a wrong answer as a halt.
        real = CodexCliBackend.complete

        def rerouting(self, system, user, *, model, options=None):
            response = real(self, system, user, model=model, options=options)
            if response.text != EXPECTED:
                from llm_scripting_kit.completion.codex_backend import CodexRunError

                raise CodexRunError("wrong answer", halt_kind=HALT_QUOTA)
            return response

        monkeypatch.setattr(CodexCliBackend, "complete", rerouting)
        monkeypatch.setattr(decl, "record_observed_halt", lambda *a, **k: None)
        with pytest.raises(AssertionError):
            check_assertion_2(drill, monkeypatch)

    def test_3_red_when_the_workspace_is_not_reset(self, drill, monkeypatch, workspace):
        monkeypatch.setattr(decl._WorkspaceSnapshot, "restore", lambda self: "workspace reset to its launch state (skipped)")
        with pytest.raises(AssertionError):
            check_assertion_3(drill, monkeypatch, workspace)
