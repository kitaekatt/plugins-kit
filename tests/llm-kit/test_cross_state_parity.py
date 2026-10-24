"""Cross-state parity: persisted state written by one side is read by the other.

The design's "Persisted-state compatibility" section promises that legacy
job-kit, content-pipeline-kit and llm-scripting-kit, and the llm-kit that
replaces them, can hand the same persisted files back and forth. These tests
prove the promise across versions, in both directions (forward: legacy writes,
llm-kit reads; rollback: llm-kit writes, legacy reads), not only that each side
reads its own files.

How the sides are driven
------------------------

Every stretch of a scenario runs in its own interpreter
(``fixtures/cross_state/cross_state_worker.py``). The legacy worker's
``sys.path`` holds only the three legacy ``lib`` directories (and bootstrap); the
final worker's holds only ``packages/katsoft-llm-kit/src`` (and bootstrap). HOME and
USERPROFILE name a scratch directory before the interpreter starts, so every
module that computes a path from HOME at import (the verdict cache, the
credential file) resolves under it. Each worker reports the HOME it resolved
and any module of the other side it imported; ``run_worker`` fails on either.
Nothing here makes a network call or starts a model: job backends are fakes.

What is proved
--------------

* Job ledger (schema 12 for legacy job-kit, 13 for llm-kit): a ledger created
  and partly run by legacy is resumed by llm-kit (which upgrades it to schema
  13) to the same terminal state, with the earlier event rows untouched and the
  rest equal to a run done entirely on one side. The reverse is the deliberate
  difference (user ruling 2026-10-19, llm-kit may change its frozen formats for
  ``jobs cancel``): legacy job-kit refuses a schema-13 ledger, loudly and before
  it writes anything, so a rollback after llm-kit has opened a ledger is pinned
  as that refusal (``TestJobLedgerRollbackRefusal``). Handover is
  one process at a time, per the registered one-process-per-ledger rule; the
  tests never run two writers on one job ledger and assert no new lock or
  ownership artifact exists.
* Pipeline execution store (schema 10): one dispatcher lease per run with a
  legacy and an llm-kit process as live contenders, takeover by release or
  expiry with a higher fence, stale unit and dispatcher fences refused,
  distinct runs and distinct units concurrent, and a partly run store handed
  across in both directions with identical events, status and terminal state.
* Spend ledger (schema 1): written by one side and read, settled, halted and
  resumed by the other.
* Verdict cache: ``usage-verdicts.json`` at its legacy path, read and written
  by both sides, including the documented lost-update cost: a stale writer
  restores AVAILABLE over a recorded exhaustion and a pinned read returns it.
  The design keeps last-writer-wins; the tests assert the cost exists and do
  not assert it is prevented.
* Credentials ``.env`` and user config: written by legacy, read by llm-kit at
  the same legacy paths.
* Interrupts: a request and resolution created by one side are answered or
  replayed by the other, with byte-identical resolution documents, checked
  against documents committed under ``fixtures/cross_state/``.

Deliberate differences, each pinned by name in ``TestDeliberateDifferences``:
the console command spelling in ``CLI_COMMAND`` names the llm-kit venv.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import pytest

from final_wording import openrouter_style_to_legacy, to_final

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
_FIXTURES = _HERE / "fixtures" / "cross_state"
_WORKER = _FIXTURES / "cross_state_worker.py"
_CONTRACT = _FIXTURES / "interrupting_contract.py"

LEGACY = "legacy"
FINAL = "final"
SIDES = (LEGACY, FINAL)

#: Every (first, second) pair of sides, same-side pairs included as references.
PAIRS = list(itertools.product(SIDES, repeat=2))
#: Handover of a three-stage scenario: who starts it, who answers, who finishes.
TRIPLES = [
    (LEGACY, LEGACY, LEGACY),
    (FINAL, FINAL, FINAL),
    (LEGACY, FINAL, FINAL),
    (FINAL, LEGACY, LEGACY),
    (LEGACY, FINAL, LEGACY),
    (FINAL, LEGACY, FINAL),
]
MIXED_TRIPLES = [t for t in TRIPLES if len(set(t)) > 1]


def _rolls_back(stages: Sequence[str]) -> bool:
    """Whether a legacy stage follows an llm-kit stage (llm-kit has then written schema 13)."""
    seen_final = False
    for side in stages:
        if side == FINAL:
            seen_final = True
        elif seen_final:
            return True
    return False


#: Job-ledger handovers that stay readable: legacy first, llm-kit after.
JOB_FORWARD_TRIPLES = [t for t in MIXED_TRIPLES if not _rolls_back(t)]
#: Job-ledger handovers a schema-13 ledger refuses at the legacy stage.
JOB_ROLLBACK_TRIPLES = [t for t in MIXED_TRIPLES if _rolls_back(t)]
JOB_PAIRS = [p for p in PAIRS if not _rolls_back(p)]
JOB_ROLLBACK_PAIRS = [p for p in PAIRS if _rolls_back(p)]


def _label(sides: Sequence[str]) -> str:
    return "-".join("L" if s == LEGACY else "F" for s in sides)


# --------------------------------------------------------------------------
# Driving a side in its own process
# --------------------------------------------------------------------------

_SCRUBBED_ENV = (
    "PYTHONPATH",
    "CLAUDE_BOOTSTRAP_DATA_ROOT",
    "CLAUDE_PLUGIN_TEST",
    "CLAUDE_CODE_SESSION_ID",
    "LLM_SCRIPTING_KIT_USAGE_SESSION",
    "OPENROUTER_API_KEY",
    "CROSS_STATE_KEY",
    "CONTENT_PIPELINE_SPEND_LEDGER",
    "JOB_KIT_INTERRUPT_REQUEST",
    "JOB_KIT_INTERRUPT_RESOLUTION",
    "JOB_KIT_INTERRUPT_ID",
)


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME and USERPROFILE point at a temp directory, in this process and every worker."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert Path.home().resolve() == home.resolve()
    for name in _SCRUBBED_ENV:
        monkeypatch.delenv(name, raising=False)
    return home


def op(name: str, label: Optional[str] = None, **args: Any) -> dict:
    return {"op": name, "label": label or name, "args": args}


def _worker_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["_BOOTSTRAP_GUARD_SKIP_REEXEC"] = "1"
    return env


class Running:
    """One started worker; ``result()`` waits for it and returns its outcomes by label."""

    def __init__(self, side: str, home: Path, workdir: Path, ops: Sequence[dict]) -> None:
        self.side = side
        self.home = home
        self.ops = list(ops)
        workdir.mkdir(parents=True, exist_ok=True)
        stem = f"{side}-{time.monotonic_ns()}"
        self._request = workdir / f"{stem}.request.json"
        self._result = workdir / f"{stem}.result.json"
        self._request.write_text(
            json.dumps({"side": side, "home": str(home), "ops": self.ops}), encoding="utf-8"
        )
        self.process = subprocess.Popen(
            [sys.executable, str(_WORKER), str(self._request), str(self._result)],
            env=_worker_env(home),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

    def result(self, timeout: float = 180.0) -> dict:
        try:
            stdout, stderr = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            stdout, stderr = self.process.communicate()
            raise AssertionError(f"{self.side} worker timed out\n{stdout}\n{stderr}")
        assert self.process.returncode == 0, (
            f"{self.side} worker exited {self.process.returncode}\n{stdout}\n{stderr}"
        )
        out = json.loads(self._result.read_text(encoding="utf-8"))
        assert out["leaks"] == [], f"{self.side} worker imported the other side: {out['leaks']}"
        assert Path(out["home"]).resolve() == self.home.resolve()
        by_label: dict[str, dict] = {}
        for item in out["results"]:
            assert item["label"] not in by_label, f"duplicate label {item['label']!r}"
            by_label[item["label"]] = item
        return by_label


def run_worker(side: str, home: Path, workdir: Path, ops: Sequence[dict]) -> dict:
    """Run ``ops`` on ``side`` to completion before returning (one process at a time)."""
    return Running(side, home, workdir, ops).result()


def ok(item: dict) -> Any:
    assert "ok" in item, f"{item['label']} raised: {item['raised']['mro'][0]}: {item['raised']['message']}\n{item['raised'].get('traceback', '')}"
    return item["ok"]


def raised(item: dict) -> dict:
    assert "raised" in item, f"{item['label']} did not raise: {item['ok']!r}"
    shape = dict(item["raised"])
    shape.pop("traceback", None)
    return shape


def outcome(item: dict) -> dict:
    """A step's result, or the shape of what it raised, comparable across sides."""
    return {"ok": item["ok"]} if "ok" in item else {"raised": raised(item)}


def contend(
    home: Path, workdir: Path, specs: Sequence[tuple[str, Sequence[dict]]]
) -> list[dict]:
    """Start the workers, hold them at a barrier, release them together.

    Each worker imports its side, announces readiness, then waits for the
    shared release file before running its operations, so the operations of
    both sides start inside the same few milliseconds.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    go = workdir / "go"
    running = []
    for index, (side, ops) in enumerate(specs):
        ready = workdir / f"ready-{index}"
        barrier = [op("touch", "ready", path=str(ready)), op("wait_file", "go", path=str(go))]
        running.append(Running(side, home, workdir, barrier + list(ops)))
    deadline = time.monotonic() + 90
    for index in range(len(specs)):
        while not (workdir / f"ready-{index}").exists():
            assert time.monotonic() < deadline, "a contender never became ready"
            dead = [r for r in running if r.process.poll() not in (None, 0)]
            assert not dead, f"a contender died: {dead[0].process.communicate()}"
            time.sleep(0.005)
    go.write_text("1", encoding="ascii")
    return [r.result() for r in running]


# --------------------------------------------------------------------------
# Scrubbing what legitimately differs between two runs
# --------------------------------------------------------------------------

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
_HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
_EPOCH = re.compile(r"\b1[0-9]{9}(?:\.[0-9]+)?\b")
_TIMING_KEYS = ("wall_ms", "duration_ms", "elapsed_ms")
_TIMING = re.compile(r'("(?:' + "|".join(_TIMING_KEYS) + r')"\s*:\s*)[0-9.]+')


class Scrub:
    def __init__(self, *roots: Path) -> None:
        names = set()
        for root in roots:
            resolved = root.resolve()
            names |= {
                str(resolved),
                resolved.as_posix(),
                str(root),
                root.as_posix(),
                str(resolved).replace("\\", "\\\\"),
            }
        self.roots = sorted(names, key=len, reverse=True)

    def text(self, value: str) -> str:
        for root in self.roots:
            value = value.replace(root, "<ROOT>")
        value = value.replace("\\", "/")
        value = _ISO.sub("<ISO>", value)
        value = _EPOCH.sub("<EPOCH>", value)
        value = _TIMING.sub(lambda m: m.group(1) + '"<MS>"', value)
        return _HEX32.sub("<HEX32>", value)

    def value(self, value: Any, *, column: str = "") -> Any:
        if isinstance(value, str):
            # A stored JSON column is compared as TEXT (only the volatile tokens
            # replaced), so a byte the encoder adds or drops is a difference.
            return self.text(value)
        if isinstance(value, float):
            return "<EPOCH>" if value > 1e9 else value
        if isinstance(value, Mapping):
            if isinstance(value, dict):
                value = openrouter_style_to_legacy(value)  # declared difference (final_wording)
            return {
                str(k): ("<MS>" if str(k) in _TIMING_KEYS else self.value(v, column=str(k)))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        return value


def first_difference(a: Any, b: Any, path: str = "$") -> Optional[str]:
    if type(a) is not type(b):
        return f"{path}: {a!r} != {b!r}"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b), key=str):
            if key not in a or key not in b:
                return f"{path}.{key}: present on one side only ({a.get(key)!r} vs {b.get(key)!r})"
            found = first_difference(a[key], b[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for index, (x, y) in enumerate(zip(a, b)):
            found = first_difference(x, y, f"{path}[{index}]")
            if found:
                return found
        return None
    if isinstance(a, str) and isinstance(b, str):
        # named deliberate wording differences, including the shipped sol/luna model ids
        return None if to_final(a) == to_final(b) else f"{path}: {a!r} != {b!r}"
    return None if a == b else f"{path}: {a!r} != {b!r}"


def assert_same(actual: Any, expected: Any, what: str = "") -> None:
    diff = first_difference(actual, expected)
    assert diff is None, f"{what}: {diff}"


# --------------------------------------------------------------------------
# Job ledger (schema 12)
# --------------------------------------------------------------------------

_PASS = [sys.executable, "-c", "pass"]
_ANSWER = {"approved": True}
_NOW = 1_900_000_000.0


def _job(job_id: str, work: Path, command: list) -> dict:
    return {
        "id": job_id,
        "prompt": {"user": job_id},
        "models": ["ep"],
        "directory": str(work),
        "contract": {"command": command, "directory": str(work)},
    }


def _write_jobs(base: Path) -> tuple[Path, Path]:
    """A three-job file: an accepted job, one that asks for approval, an accepted job."""
    work = base / "work"
    work.mkdir(parents=True)
    log = base / "continuations.log"
    document = {
        "max_parallel": 1,
        "workspace_root": str(base / "ws"),
        "jobs": [
            _job("a", work, _PASS),
            _job("release", work, [sys.executable, str(_CONTRACT), str(log)]),
            _job("c", work, _PASS),
        ],
    }
    path = base / "jobs.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path, log


def _norm_ledger(tables: Mapping[str, list], scrub: Scrub) -> dict:
    return {
        table: [{k: scrub.value(v, column=k) for k, v in row.items()} for row in rows]
        for table, rows in tables.items()
    }


def _job_args(jobs_file: Path, ledger: Path) -> dict:
    return {"jobs_file": str(jobs_file), "store_path": str(ledger), "run_id": "r"}


def job_handover(base: Path, stages: Sequence[str], *, create_first: bool = False) -> dict:
    """Start a run on one side, answer its interrupt on a second, finish it on a third.

    With ``create_first`` the first side only creates the run (every job
    pending) and the second side executes it up to the wait, so the ledger a
    side resumes was created by another and never run by its creator.
    """
    base.mkdir(parents=True)
    home = base / "home"
    home.mkdir()
    jobs_file, log = _write_jobs(base)
    ledger = base / "ledger" / "runs.sqlite3"
    work = base / "workers"
    args = _job_args(jobs_file, ledger)
    first, second, third = stages
    if create_first:
        created = run_worker(first, home, work, [op("jobs_create", **args), op("dump", db=str(ledger))])
        started = run_worker(
            second,
            home,
            work,
            [op("jobs_resume", run_id="r", store_path=str(ledger), clock_epoch_s=0)],
        )
        state_after_first = ok(created["jobs_create"])
        before = ok(created["dump"])
        waiting = ok(started["jobs_resume"])
        before = ok(run_worker(second, home, work, [op("dump", db=str(ledger))])["dump"])
    else:
        started = run_worker(
            first,
            home,
            work,
            [op("jobs_run", clock_epoch_s=0, **args), op("dump", db=str(ledger))],
        )
        state_after_first = None
        waiting = ok(started["jobs_run"])
        before = ok(started["dump"])
    resolved = run_worker(
        second,
        home,
        work,
        [
            op(
                "jobs_resolve",
                run_id="r",
                store_path=str(ledger),
                interrupt_id=1,
                decision="answer",
                input=_ANSWER,
                now=_NOW,
            )
        ],
    )
    resolution = ok(resolved["jobs_resolve"])
    finished = run_worker(
        third,
        home,
        work,
        [
            op("jobs_resume", run_id="r", store_path=str(ledger), clock_epoch_s=3600),
            op("jobs_state", run_id="r", store_path=str(ledger)),
            op("dump", db=str(ledger)),
            op("jobs_events", run_id="r", store_path=str(ledger)),
            op("files", root=str(ledger.parent)),
        ],
    )
    scrub = Scrub(base)
    after = ok(finished["dump"])
    return {
        "created": state_after_first,
        "waiting": waiting,
        "resolution": scrub.value(resolution["resolution"]),
        "document": resolution["document"],
        "log": log.read_text(encoding="utf-8"),
        "finished": ok(finished["jobs_resume"]),
        "state": ok(finished["jobs_state"]),
        # The schema_version table is left out of the compared ledger and the
        # schema dump is a private key: legacy ends at 12, llm-kit at 13
        # (``TestJobLedgerRollbackRefusal`` and ``test_the_schema_versions_*``).
        "ledger": {
            table: rows
            for table, rows in _norm_ledger(after["tables"], scrub).items()
            if table != "schema_version"
        },
        "_schema": after["schema"],
        "events": [scrub.text(line) for line in ok(finished["jobs_events"])["lines"]],
        "files": ok(finished["files"])["files"],
        "_before_rows": before["tables"],
        "_after_rows": after["tables"],
    }


def _public(result: Mapping[str, Any]) -> dict:
    return {k: v for k, v in result.items() if not k.startswith("_")}


@pytest.fixture(scope="module")
def job_reference(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """The interrupt scenario run entirely on each side, once per module."""
    base = tmp_path_factory.mktemp("job-reference")
    return {
        side: job_handover(base / side, (side, side, side)) for side in SIDES
    }


@pytest.fixture(scope="module")
def job_pending_reference(tmp_path_factory: pytest.TempPathFactory) -> dict:
    base = tmp_path_factory.mktemp("job-pending-reference")
    return {
        side: job_handover(base / side, (side, side, side), create_first=True) for side in SIDES
    }


class TestJobLedgerHandover:
    def test_the_reference_scenario_reaches_the_states_it_claims(self, job_reference):
        for side in SIDES:
            ref = job_reference[side]
            assert ref["waiting"]["exit_code"] == 4
            assert ref["waiting"]["states"] == {"a": "accepted", "release": "waiting", "c": "accepted"}
            assert ref["finished"]["exit_code"] == 0
            assert ref["state"]["states"] == {"a": "accepted", "release": "accepted", "c": "accepted"}
            # Deliberate difference: legacy job-kit stays at schema 12, llm-kit is at 13.
            assert ref["_schema"]["schema_version"] == [12 if side == LEGACY else 13]
            assert ref["document"] and ref["log"] == "--resolution--\n" + ref["document"] + "\n"
            kinds = [e for e in ref["events"] if "continuation" in e or "interrupt" in e]
            assert kinds, "the scenario recorded no interrupt or continuation event"

    def test_both_sides_agree_when_each_runs_alone(self, job_reference):
        assert_same(_public(job_reference[FINAL]), _public(job_reference[LEGACY]), "final vs legacy")

    @pytest.mark.parametrize("stages", JOB_FORWARD_TRIPLES, ids=_label)
    def test_a_ledger_handed_across_reaches_the_same_terminal_state_and_event_bytes(
        self, stages, tmp_path, job_reference
    ):
        """Forward handover: legacy starts the run, llm-kit (which upgrades the ledger) finishes it.

        One process at a time owns the ledger: each stage ends before the next
        starts, which is the registered handover rule. The handovers that put a
        legacy stage after an llm-kit stage are refused (``TestJobLedgerRollbackRefusal``).
        """
        crossed = job_handover(tmp_path / "crossed", stages)
        assert_same(_public(crossed), _public(job_reference[LEGACY]), "handover vs one-side run")
        assert crossed["_schema"]["schema_version"] == [13]
        # The facts the first stage recorded are untouched, byte for byte (raw
        # rows, timestamps included), by whichever side finishes the run.
        before = crossed["_before_rows"]["events"]
        after = crossed["_after_rows"]["events"]
        assert len(after) > len(before) > 0
        assert after[: len(before)] == before
        assert crossed["finished"]["exit_code"] == 0
        # The document one side wrote and the other's continuation read is the committed one.
        assert crossed["document"] == _expected("job_resolution_document.json")

    @pytest.mark.parametrize("stages", [(LEGACY, FINAL, FINAL)], ids=_label)
    def test_a_ledger_created_by_legacy_and_never_run_is_run_by_llm_kit(
        self, stages, tmp_path, job_pending_reference
    ):
        crossed = job_handover(tmp_path / "crossed", stages, create_first=True)
        assert crossed["created"]["states"] == {"a": "pending", "release": "pending", "c": "pending"}
        assert_same(_public(crossed), _public(job_pending_reference[LEGACY]), "pending handover")
        assert crossed["finished"]["exit_code"] == 0

    def test_the_pending_reference_agrees_across_sides(self, job_pending_reference):
        assert_same(
            _public(job_pending_reference[FINAL]), _public(job_pending_reference[LEGACY]), "pending"
        )

    @pytest.mark.parametrize("stages", JOB_FORWARD_TRIPLES, ids=_label)
    def test_handover_adds_no_lock_or_ownership_artifact(self, stages, tmp_path, job_reference):
        """No new lock, refusal or ownership protocol: the ledger directory is what it always was."""
        crossed = job_handover(tmp_path / "crossed", stages)
        assert crossed["files"] == job_reference[LEGACY]["files"]
        assert set(crossed["files"]) <= {"runs.sqlite3", "runs.sqlite3-wal", "runs.sqlite3-shm"}
        # Same tables, indexes and triggers as an llm-kit ledger (the rowid order of
        # sqlite_master differs for an upgraded ledger, so compare the sets).
        objects = lambda dump: {(row[0], row[1]) for row in dump["sqlite_master"]}  # noqa: E731
        assert objects(crossed["_schema"]) == objects(job_reference[FINAL]["_schema"])
        assert crossed["_schema"]["journal_mode"] == job_reference[FINAL]["_schema"]["journal_mode"]

    def test_neither_job_store_takes_a_process_lifetime_os_lock(self):
        """The one-process-per-ledger rule is operational: no code enforces or detects it."""
        forbidden = re.compile(r"\b(fcntl|msvcrt|flock|lockf|LockFileEx|portalocker|filelock)\b")
        scanned = [
            _REPO / "plugins" / "job-kit" / "lib" / "job_kit" / "store.py",
            _REPO / "plugins" / "job-kit" / "lib" / "job_kit" / "run.py",
        ]
        # The supervisor modules lock a supervised run's own lease and owner files (jobs/supervisor.py
        # _lock), never the job ledger. This test pins the ledger store (store, run, ledger readers), so
        # the supervisor modules are out of its scope.
        scanned += sorted(
            path
            for path in (_REPO / "packages" / "katsoft-llm-kit" / "src" / "llm_kit" / "jobs").glob("*.py")
            if "supervisor" not in path.name
        )
        assert len(scanned) >= 4
        for path in scanned:
            hit = forbidden.search(path.read_text(encoding="utf-8"))
            assert hit is None, f"{path} mentions an OS lock primitive: {hit.group(0) if hit else ''}"


# --------------------------------------------------------------------------
# Rollback to legacy job-kit is refused (the schema-13 deliberate difference)
# --------------------------------------------------------------------------


def _is_refusal(item: dict) -> bool:
    shape = raised(item)
    return shape["mro"][0] == "StoreError" and "newer than this" in shape["message"] and "max 12" in shape["message"]


class TestJobLedgerRollbackRefusal:
    """Legacy job-kit stays at schema 12 and refuses a ledger llm-kit has opened.

    The refusal is the future-schema check job-kit already had: loud, typed, and
    before anything is written. The legacy stage is where the handover stops; an
    llm-kit stage after it still finishes the run.
    """

    @pytest.mark.parametrize("stages", JOB_ROLLBACK_TRIPLES, ids=_label)
    def test_a_legacy_stage_after_an_llm_kit_stage_is_refused_and_writes_nothing(
        self, stages, tmp_path
    ):
        base = tmp_path / "scenario"
        base.mkdir()
        home = base / "home"
        home.mkdir()
        jobs_file, _log = _write_jobs(base)
        ledger = base / "ledger" / "runs.sqlite3"
        work = base / "workers"
        args = _job_args(jobs_file, ledger)
        first, second, third = stages
        ok(run_worker(first, home, work, [op("jobs_run", clock_epoch_s=0, **args)])["jobs_run"])
        resolve = op(
            "jobs_resolve",
            run_id="r",
            store_path=str(ledger),
            interrupt_id=1,
            decision="answer",
            input=_ANSWER,
            now=_NOW,
        )
        resume = op("jobs_resume", run_id="r", store_path=str(ledger), clock_epoch_s=3600)
        for side, step, label in ((second, resolve, "jobs_resolve"), (third, resume, "jobs_resume")):
            refused = side == LEGACY
            before = ledger.read_bytes()
            item = run_worker(side, home, work, [step])[label]
            if refused:
                assert _is_refusal(item), item
                assert ledger.read_bytes() == before
            else:
                ok(item)

    def test_llm_kit_still_finishes_a_run_after_legacy_was_refused(self, tmp_path):
        base = tmp_path / "scenario"
        base.mkdir()
        home = base / "home"
        home.mkdir()
        jobs_file, _log = _write_jobs(base)
        ledger = base / "ledger" / "runs.sqlite3"
        work = base / "workers"
        ok(run_worker(FINAL, home, work, [op("jobs_run", clock_epoch_s=0, **_job_args(jobs_file, ledger))])["jobs_run"])
        legacy = run_worker(
            LEGACY,
            home,
            work,
            [op("jobs_state", run_id="r", store_path=str(ledger))],
        )
        assert _is_refusal(legacy["jobs_state"])
        state = ok(run_worker(FINAL, home, work, [op("jobs_state", run_id="r", store_path=str(ledger))])["jobs_state"])
        assert state["states"] == {"a": "accepted", "release": "waiting", "c": "accepted"}

    @pytest.mark.parametrize("pair", JOB_ROLLBACK_PAIRS, ids=_label)
    def test_a_resolution_made_by_llm_kit_cannot_be_replayed_by_legacy(self, pair, tmp_path):
        answerer, replayer = pair
        base = tmp_path / "scenario"
        base.mkdir()
        home = base / "home"
        home.mkdir()
        jobs_file, _log = _write_jobs(base)
        ledger = base / "ledger" / "runs.sqlite3"
        work = base / "workers"
        ok(run_worker(answerer, home, work, [op("jobs_run", clock_epoch_s=0, **_job_args(jobs_file, ledger))])["jobs_run"])
        resolve = dict(run_id="r", store_path=str(ledger), interrupt_id=1, now=_NOW + 5)
        ok(run_worker(answerer, home, work, [op("jobs_resolve", decision="answer", input=_ANSWER, **resolve)])["jobs_resolve"])
        replay = run_worker(replayer, home, work, [op("jobs_resolve", "replay", decision="answer", input=_ANSWER, **resolve)])
        assert _is_refusal(replay["replay"])


# --------------------------------------------------------------------------
# Interrupts (job ledger)
# --------------------------------------------------------------------------


def _expected(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="ascii")


class TestJobInterrupts:
    def test_the_resolution_document_is_the_committed_bytes_on_both_sides(self, job_reference):
        expected = _expected("job_resolution_document.json")
        for side in SIDES:
            assert job_reference[side]["document"] == expected, side

    @pytest.mark.parametrize("pair", JOB_PAIRS, ids=_label)
    def test_a_resolution_made_by_one_side_is_replayed_by_the_other(self, pair, tmp_path):
        """Same answer replays (``replayed`` set, same document); a different one conflicts."""
        answerer, replayer = pair
        base = tmp_path / "scenario"
        base.mkdir()
        home = base / "home"
        home.mkdir()
        jobs_file, log = _write_jobs(base)
        ledger = base / "ledger" / "runs.sqlite3"
        work = base / "workers"
        args = _job_args(jobs_file, ledger)
        run_worker(answerer, home, work, [op("jobs_run", clock_epoch_s=0, **args)])
        resolve = dict(run_id="r", store_path=str(ledger), interrupt_id=1, now=_NOW + 5)
        answered = ok(
            run_worker(
                answerer,
                home,
                work,
                [op("jobs_resolve", decision="answer", input=_ANSWER, **resolve)],
            )["jobs_resolve"]
        )
        stage = run_worker(
            replayer,
            home,
            work,
            [
                op("jobs_resolve", "replay", decision="answer", input=_ANSWER, **resolve),
                op("jobs_resolve", "conflict", decision="reject", reason="no", **resolve),
                op("jobs_document", run_id="r", store_path=str(ledger), interrupt_id=1),
            ],
        )
        replay = ok(stage["replay"])
        scrub = Scrub(base)
        assert replay["resolution"]["replayed"] is True
        assert answered["resolution"]["replayed"] is False
        assert replay["document"] == answered["document"] == ok(stage["jobs_document"])["document"]
        stored = scrub.value(answered["resolution"])
        stored["replayed"] = True
        assert scrub.value(replay["resolution"]) == stored
        conflict = raised(stage["conflict"])
        assert conflict["mro"][0] == "ResolutionConflictError"
        # After the job moved on (finished by the replayer) the replay still answers.
        run_worker(
            replayer, home, work, [op("jobs_resume", run_id="r", store_path=str(ledger), clock_epoch_s=60)]
        )
        # A legacy answerer cannot reopen the ledger once llm-kit has upgraded it to
        # schema 13 (``TestJobLedgerRollbackRefusal``), so llm-kit replays late.
        late_side = replayer if (answerer, replayer) == (LEGACY, FINAL) else answerer
        late = ok(
            run_worker(
                late_side,
                home,
                work,
                [op("jobs_resolve", decision="answer", input=_ANSWER, **resolve)],
            )["jobs_resolve"]
        )
        assert late["resolution"]["replayed"] is True
        assert late["document"] == answered["document"]
        # The continuation received exactly those bytes.
        assert log.read_text(encoding="utf-8") == "--resolution--\n" + answered["document"] + "\n"

    def test_the_conflict_is_the_same_refusal_on_both_sides(self, tmp_path):
        shapes = []
        for side in SIDES:
            base = tmp_path / side
            base.mkdir()
            home = base / "home"
            home.mkdir()
            jobs_file, _log = _write_jobs(base)
            ledger = base / "ledger" / "runs.sqlite3"
            work = base / "workers"
            run_worker(side, home, work, [op("jobs_run", clock_epoch_s=0, **_job_args(jobs_file, ledger))])
            resolve = dict(run_id="r", store_path=str(ledger), interrupt_id=1, now=_NOW)
            stage = run_worker(
                side,
                home,
                work,
                [
                    op("jobs_resolve", "answer", decision="answer", input=_ANSWER, **resolve),
                    op("jobs_resolve", "conflict", decision="reject", reason="no", **resolve),
                    op("jobs_resolve", "bad", decision="answer", input={"approved": False}, **{**resolve, "interrupt_id": 99}),
                ],
            )
            scrub = Scrub(base)
            shapes.append(scrub.value({"conflict": outcome(stage["conflict"]), "unknown": outcome(stage["bad"])}))
        assert_same(shapes[1], shapes[0], "conflict and unknown-interrupt refusals")
        assert shapes[0]["conflict"]["raised"]["mro"][0] == "ResolutionConflictError"
        assert shapes[0]["unknown"]["raised"]["mro"][0] == "UnknownInterruptError"


# --------------------------------------------------------------------------
# Pipeline execution store (schema 10)
# --------------------------------------------------------------------------

_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
_REQUEST = {
    "$interrupt_request": {
        "kind": "approve",
        "request_schema": _SCHEMA,
        "payload": {"q": "ship it?"},
        "expires_in_s": 100000,
    }
}


def pc(db: Path, method: str, *args: Any, label: Optional[str] = None, **kwargs: Any) -> dict:
    return op(
        "pipe_call",
        label or method,
        db=str(db),
        method=method,
        args=list(args),
        kwargs=kwargs,
    )


def seed(db: Path, run_id: str = "run-1", units: Sequence[str] = ("u0", "u1")) -> dict:
    return op("pipe_seed", f"seed-{run_id}", db=str(db), run_id=run_id, units=list(units))




def lease_story(base: Path, holder: str, taker: str) -> dict:
    """A dispatcher lease handed between two sides by refusal, expiry and release."""
    home = base / "home"
    home.mkdir(parents=True)
    db = base / "lease.db"
    work = base / "w"
    steps: dict[str, dict] = {}

    def acquire(who: str, label: str, at: float) -> dict:
        return pc(db, "acquire_dispatcher_lease", "run-1", who, label=label, lease_seconds=100.0, at=at)

    steps.update(
        run_worker(
            holder,
            home,
            work,
            [seed(db, units=("u0",)), acquire("d-hold", "hold", 100.0), acquire("d-hold", "hold-again", 101.0)],
        )
    )
    steps.update(run_worker(taker, home, work, [acquire("d-take", "refused", 150.0)]))
    steps.update(
        run_worker(
            holder,
            home,
            work,
            [pc(db, "renew_dispatcher_lease", "run-1", "d-hold", 2, label="renew", lease_seconds=100.0, at=160.0)],
        )
    )
    steps.update(run_worker(taker, home, work, [acquire("d-take", "takeover", 300.0)]))
    steps.update(
        run_worker(
            holder,
            home,
            work,
            [
                pc(db, "renew_dispatcher_lease", "run-1", "d-hold", 2, label="stale-renew", lease_seconds=100.0, at=301.0),
                pc(db, "release_dispatcher_lease", "run-1", "d-hold", 2, label="stale-release", at=301.0),
                acquire("d-hold", "reacquire-refused", 302.0),
            ],
        )
    )
    steps.update(
        run_worker(
            taker, home, work, [pc(db, "release_dispatcher_lease", "run-1", "d-take", 3, label="release", at=310.0)]
        )
    )
    steps.update(
        run_worker(
            holder,
            home,
            work,
            [acquire("d-hold", "after-release", 311.0), pc(db, "get_run", "run-1", label="run"), op("dump", db=str(db))],
        )
    )
    story = {k: outcome(v) for k, v in steps.items() if not k.startswith("seed")}
    story["dump"] = story["dump"]["ok"]
    return story


@pytest.fixture(scope="module")
def lease_reference(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return {side: lease_story(tmp_path_factory.mktemp(f"lease-{side}"), side, side) for side in SIDES}


class TestDispatcherLease:
    def test_the_reference_story_exercises_every_rule(self, lease_reference):
        for side in SIDES:
            story = lease_reference[side]
            assert story["hold"] == {"ok": 1}
            assert story["hold-again"] == {"ok": 2}  # the holder may re-acquire; the fence still rises
            assert story["refused"] == {"ok": None}  # a different live holder is refused
            assert story["renew"] == {"ok": 260.0}
            assert story["takeover"] == {"ok": 3}  # expiry permits takeover with a higher fence
            for label in ("stale-renew", "stale-release"):
                assert story[label]["raised"]["mro"][0] == "StaleDispatcherLeaseError", label
            assert story["reacquire-refused"] == {"ok": None}
            assert story["release"] == {"ok": None}
            assert story["after-release"] == {"ok": 4}  # release permits takeover; the fence never resets
            run = story["run"]["ok"]
            assert (run["dispatcher_id"], run["dispatcher_fence"]) == ("d-hold", 4)
            assert story["dump"]["schema"]["schema_version"] == [10]

    @pytest.mark.parametrize("pair", PAIRS, ids=_label)
    def test_takeover_and_refusal_do_not_depend_on_which_side_holds_the_lease(
        self, pair, tmp_path, lease_reference
    ):
        holder, taker = pair
        story = lease_story(tmp_path / "story", holder, taker)
        assert_same(story, lease_reference[LEGACY], f"holder={holder} taker={taker}")

    def test_the_sides_agree_when_each_plays_both_parts(self, lease_reference):
        assert_same(lease_reference[FINAL], lease_reference[LEGACY], "final vs legacy")

    @pytest.mark.parametrize("round_number", range(6))
    def test_two_live_contenders_exactly_one_wins(self, round_number, tmp_path):
        """A legacy and an llm-kit process race for one run's lease; one gets fence 1.

        The loser is refused, not queued: it holds no lease and the fence did not
        move. Releasing the winner's lease lets the loser take over with fence 2.
        """
        home = tmp_path / "home"
        db = tmp_path / "contend.db"
        seeder = SIDES[round_number % 2]
        run_worker(seeder, home, tmp_path / "seed", [seed(db, units=("u0",))])

        def acquire(who: str) -> dict:
            return pc(db, "acquire_dispatcher_lease", "run-1", f"d-{who}", label="acquire", lease_seconds=300.0)

        results = contend(home, tmp_path / "race", [(LEGACY, [acquire(LEGACY)]), (FINAL, [acquire(FINAL)])])
        got = {LEGACY: ok(results[0]["acquire"]), FINAL: ok(results[1]["acquire"])}
        winners = [side for side, fence in got.items() if fence is not None]
        assert len(winners) == 1, f"expected exactly one dispatcher, got {got}"
        winner = winners[0]
        loser = LEGACY if winner == FINAL else FINAL
        assert got[winner] == 1 and got[loser] is None
        probe = run_worker(
            loser,
            home,
            tmp_path / "probe",
            [
                pc(db, "get_run", "run-1", label="run"),
                pc(db, "acquire_dispatcher_lease", "run-1", f"d-{loser}", label="again", lease_seconds=300.0),
            ],
        )
        run = ok(probe["run"])
        assert (run["dispatcher_id"], run["dispatcher_fence"]) == (f"d-{winner}", 1)
        assert ok(probe["again"]) is None
        run_worker(
            winner,
            home,
            tmp_path / "release",
            [pc(db, "release_dispatcher_lease", "run-1", f"d-{winner}", 1, label="release")],
        )
        taken = run_worker(
            loser,
            home,
            tmp_path / "take",
            [pc(db, "acquire_dispatcher_lease", "run-1", f"d-{loser}", label="take", lease_seconds=300.0)],
        )
        assert ok(taken["take"]) == 2

    def test_distinct_runs_hold_their_own_leases_concurrently(self, tmp_path):
        home = tmp_path / "home"
        db = tmp_path / "runs.db"
        run_worker(LEGACY, home, tmp_path / "seed", [seed(db, "run-a"), seed(db, "run-b")])

        def acquire(run: str, who: str) -> dict:
            return pc(db, "acquire_dispatcher_lease", run, f"d-{who}", label="acquire", lease_seconds=300.0)

        results = contend(
            home, tmp_path / "race", [(LEGACY, [acquire("run-a", LEGACY)]), (FINAL, [acquire("run-b", FINAL)])]
        )
        assert ok(results[0]["acquire"]) == 1 and ok(results[1]["acquire"]) == 1
        crossed = {
            LEGACY: ok(run_worker(LEGACY, home, tmp_path / "x1", [acquire("run-b", LEGACY)])["acquire"]),
            FINAL: ok(run_worker(FINAL, home, tmp_path / "x2", [acquire("run-a", FINAL)])["acquire"]),
        }
        assert crossed == {LEGACY: None, FINAL: None}, "each run still has exactly its own dispatcher"


def fence_story(base: Path, first: str, second: str) -> dict:
    """Two workers on one unit: the first goes stale when the second reclaims it."""
    home = base / "home"
    home.mkdir(parents=True)
    db = base / "fence.db"
    work = base / "w"
    steps: dict[str, dict] = {}
    steps.update(
        run_worker(
            first,
            home,
            work,
            [
                seed(db, units=("u0", "u1", "u2")),
                pc(db, "claim_unit", "run-1", "u0", "w-first", label="claim-u0", lease_seconds=10.0, at=100.0),
                pc(db, "claim_unit", "run-1", "u1", "w-first", label="claim-u1", lease_seconds=10.0, at=100.0),
                pc(db, "claim_unit", "run-1", "u2", "w-first", label="claim-u2", lease_seconds=10.0, at=100.0),
            ],
        )
    )
    steps.update(
        run_worker(
            second,
            home,
            work,
            [
                pc(db, "claim_unit", "run-1", "u0", "w-second", label="reclaim-u0", lease_seconds=100.0, at=200.0),
                pc(db, "claim_unit", "run-1", "u1", "w-second", label="reclaim-u1", lease_seconds=100.0, at=200.0),
                pc(db, "claim_unit", "run-1", "u2", "w-second", label="reclaim-u2", lease_seconds=100.0, at=200.0),
            ],
        )
    )
    steps.update(
        run_worker(
            first,
            home,
            work,
            [
                pc(db, "accept_unit", "run-1", "u0", 1, label="stale-accept", text="late", at=201.0),
                pc(db, "fail_unit", "run-1", "u1", 1, label="stale-fail", error="late", at=201.0),
                pc(db, "renew_lease", "run-1", "u0", 1, label="stale-renew", lease_seconds=10.0, at=201.0),
                pc(db, "request_interrupt", "run-1", "u2", 1, _REQUEST, label="stale-interrupt", at=201.0),
                pc(db, "claim_unit", "run-1", "u0", "w-first", label="claim-live", lease_seconds=10.0, at=202.0),
                pc(db, "get_unit", "run-1", "u0", label="unit-after-stale"),
            ],
        )
    )
    steps.update(
        run_worker(
            second,
            home,
            work,
            [
                pc(db, "accept_unit", "run-1", "u0", 2, label="accept", text="on time", at=203.0),
                pc(db, "fail_unit", "run-1", "u1", 2, label="fail", error="boom", terminal=True, at=203.0),
            ],
        )
    )
    steps.update(
        run_worker(
            first,
            home,
            work,
            [
                pc(db, "accept_unit", "run-1", "u0", 1, label="stale-accept-terminal", text="later", at=204.0),
                pc(db, "list_attempts", "run-1", "u0", label="attempts"),
                op("dump", db=str(db)),
            ],
        )
    )
    story = {k: outcome(v) for k, v in steps.items() if not k.startswith("seed")}
    story["dump"] = story["dump"]["ok"]
    return story


@pytest.fixture(scope="module")
def fence_reference(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return {side: fence_story(tmp_path_factory.mktemp(f"fence-{side}"), side, side) for side in SIDES}


class TestUnitFences:
    def test_the_reference_story_refuses_every_stale_mutation(self, fence_reference):
        for side in SIDES:
            story = fence_reference[side]
            assert story["claim-u0"]["ok"]["fencing_token"] == 1
            assert story["reclaim-u0"]["ok"]["fencing_token"] == 2
            for label in ("stale-accept", "stale-fail", "stale-renew", "stale-interrupt", "stale-accept-terminal"):
                assert story[label]["raised"]["mro"][0] == "StaleFenceError", label
            assert story["claim-live"]["raised"]["mro"][0] == "AlreadyClaimedError"
            # A refused late submission changed nothing but left its trace.
            unit = story["unit-after-stale"]["ok"]
            assert unit["state"]["value"] == "claimed" and unit["claimed_by"] == "w-second"
            assert story["accept"] == {"ok": None}
            kinds = [a["kind"]["value"] for a in story["attempts"]["ok"]]
            assert kinds == ["claim", "expire", "claim", "superseded", "accept", "superseded"]
            assert story["dump"]["schema"]["schema_version"] == [10]

    @pytest.mark.parametrize("pair", PAIRS, ids=_label)
    def test_a_stale_fence_is_refused_whichever_side_issued_or_holds_it(self, pair, tmp_path, fence_reference):
        first, second = pair
        story = fence_story(tmp_path / "story", first, second)
        assert_same(story, fence_reference[LEGACY], f"first={first} second={second}")

    def test_the_sides_agree_when_each_plays_both_parts(self, fence_reference):
        assert_same(fence_reference[FINAL], fence_reference[LEGACY], "final vs legacy")

    @pytest.mark.parametrize("round_number", range(4))
    def test_valid_claims_proceed_concurrently_and_one_unit_has_one_claimant(self, round_number, tmp_path):
        home = tmp_path / "home"
        db = tmp_path / "claims.db"
        run_worker(SIDES[round_number % 2], home, tmp_path / "seed", [seed(db, units=("u0", "u1", "u2"))])

        def claim(unit: str, who: str) -> dict:
            return pc(db, "claim_unit", "run-1", unit, f"w-{who}", label="claim", lease_seconds=300.0)

        distinct = contend(
            home, tmp_path / "distinct", [(LEGACY, [claim("u0", LEGACY)]), (FINAL, [claim("u1", FINAL)])]
        )
        assert ok(distinct[0]["claim"])["fencing_token"] == 1
        assert ok(distinct[1]["claim"])["fencing_token"] == 1
        contested = contend(
            home, tmp_path / "contested", [(LEGACY, [claim("u2", LEGACY)]), (FINAL, [claim("u2", FINAL)])]
        )
        outcomes = [outcome(r["claim"]) for r in contested]
        winners = [o for o in outcomes if "ok" in o]
        losers = [o for o in outcomes if "raised" in o]
        assert len(winners) == 1 and len(losers) == 1, outcomes
        assert winners[0]["ok"]["fencing_token"] == 1
        assert losers[0]["raised"]["mro"][0] == "AlreadyClaimedError"
        # Each holder's own fence is valid: both sides can submit what they claimed.
        accepts = contend(
            home,
            tmp_path / "accept",
            [
                (LEGACY, [pc(db, "accept_unit", "run-1", "u0", 1, label="accept", at=time.time())]),
                (FINAL, [pc(db, "accept_unit", "run-1", "u1", 1, label="accept", at=time.time())]),
            ],
        )
        assert ok(accepts[0]["accept"]) is None and ok(accepts[1]["accept"]) is None


def pipe_handover(base: Path, stages: Sequence[str]) -> dict:
    """Start a pipeline run on one side, answer its wait on a second, finish it on a third.

    Every time is explicit, so the whole store (not a scrubbed copy of it) is
    compared byte for byte.
    """
    home = base / "home"
    home.mkdir(parents=True)
    db = base / "run.db"
    work = base / "w"
    first, second, third = stages
    started = run_worker(
        first,
        home,
        work,
        [
            seed(db, units=("u0", "u1", "u2", "u3")),
            pc(db, "acquire_dispatcher_lease", "run-1", "d1", label="lease", lease_seconds=1000.0, at=100.0),
            pc(db, "claim_unit", "run-1", "u0", "w1", label="claim-u0", lease_seconds=500.0, at=101.0),
            pc(db, "accept_unit", "run-1", "u0", 1, label="accept-u0", text="t0", at=102.0),
            pc(db, "claim_unit", "run-1", "u1", "w1", label="claim-u1", lease_seconds=500.0, at=103.0),
            pc(db, "claim_unit", "run-1", "u2", "w1", label="claim-u2", lease_seconds=500.0, at=104.0),
            pc(db, "request_interrupt", "run-1", "u2", 1, _REQUEST, label="request", at=105.0),
            pc(db, "record_dispatch", "run-1", "u3", "w9", label="dispatch", at=106.0),
            op("dump", db=str(db)),
        ],
    )
    resolved = run_worker(
        second,
        home,
        work,
        [
            pc(db, "renew_dispatcher_lease", "run-1", "d1", 1, label="renew", lease_seconds=1000.0, at=200.0),
            pc(db, "accept_unit", "run-1", "u1", 1, label="accept-u1", text="t1", at=201.0),
            pc(db, "resolve_interrupt", "run-1", "1", label="resolve", decision="answer", input={"ok": True}, now=202.0),
            op("pipe_document", "document", db=str(db), run_id="run-1", interrupt_id=1),
            pc(db, "claim_unit", "run-1", "u2", "w2", label="claim-u2-again", lease_seconds=100.0, at=203.0),
            pc(db, "accept_unit", "run-1", "u2", 2, label="accept-u2", text="t2", at=204.0),
            pc(db, "settle_dispatch", "run-1", "u3", label="settle", outcome="done", at=205.0),
            pc(db, "claim_unit", "run-1", "u3", "w3", label="claim-u3", lease_seconds=100.0, at=206.0),
            pc(db, "accept_unit", "run-1", "u3", 1, label="accept-u3", text="t3", at=207.0),
        ],
    )
    finished = run_worker(
        third,
        home,
        work,
        [
            pc(db, "resolve_interrupt", "run-1", "1", label="replay", decision="answer", input={"ok": True}, now=300.0),
            pc(db, "resolve_interrupt", "run-1", "1", label="conflict", decision="reject", reason="no", now=301.0),
            op("pipe_document", "document-again", db=str(db), run_id="run-1", interrupt_id=1),
            pc(db, "release_dispatcher_lease", "run-1", "d1", 1, label="release", at=302.0),
            op("pipe_events", db=str(db), run_id="run-1"),
            op("pipe_status", db=str(db), run_id="run-1", now=400.0),
            pc(db, "list_units", "run-1", label="units"),
            op("dump", db=str(db)),
        ],
    )
    story = {
        k: outcome(v)
        for k, v in {**started, **resolved, **finished}.items()
        if not k.startswith("seed") and k != "dump"
    }
    after = ok(finished["dump"])
    return {
        "story": story,
        "ledger": after["tables"],
        "schema": after["schema"],
        "_before": ok(started["dump"])["tables"],
    }


@pytest.fixture(scope="module")
def pipe_reference(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return {side: pipe_handover(tmp_path_factory.mktemp(f"pipe-{side}"), (side, side, side)) for side in SIDES}


class TestPipelineHandover:
    def test_the_reference_scenario_reaches_the_states_it_claims(self, pipe_reference):
        for side in SIDES:
            ref = pipe_reference[side]
            story = ref["story"]
            assert story["request"]["ok"]["id"] == "1"
            assert [u["state"]["value"] for u in story["units"]["ok"]] == ["accepted"] * 4
            assert story["replay"]["ok"]["replayed"] is True
            assert story["conflict"]["raised"]["mro"][0] == "ResolutionConflictError"
            assert story["document"] == story["document-again"]
            assert story["pipe_events"]["ok"]["events"], "the run projected no events"
            assert ref["schema"]["schema_version"] == [10]
            # Mid-run the store held in-flight work: that is what is handed over.
            states = {row["unit_id"]: row["state"] for row in ref["_before"]["units"]}
            assert states == {"u0": "accepted", "u1": "claimed", "u2": "waiting", "u3": "pending"}

    def test_both_sides_agree_when_each_runs_alone(self, pipe_reference):
        assert_same(pipe_reference[FINAL], pipe_reference[LEGACY], "final vs legacy")

    @pytest.mark.parametrize("stages", MIXED_TRIPLES, ids=_label)
    def test_a_store_handed_across_ends_with_identical_events_status_and_bytes(
        self, stages, tmp_path, pipe_reference
    ):
        """Forward (legacy first) and rollback (llm-kit first): nonterminal work is resumed.

        The in-flight claim is submitted with its old fence by the other side;
        the wait is answered by one side and replayed by another; the whole
        store ends byte-identical to a run done on one side.
        """
        crossed = pipe_handover(tmp_path / "crossed", stages)
        assert_same(crossed, pipe_reference[LEGACY], "handover vs one-side run")
        before = crossed["_before"]
        for table in ("attempts", "interrupts"):
            kept = crossed["ledger"][table][: len(before[table])]
            assert kept == before[table], f"{table}: rows written before the handover changed"
        story = crossed["story"]
        assert story["document"]["ok"]["document"] == _expected("pipeline_resolution_document.json")
        assert story["pipe_events"]["ok"]["events"] == json.loads(_expected("pipeline_events.json"))

    def test_resolution_document_is_the_committed_bytes_on_both_sides(self, pipe_reference):
        expected = _expected("pipeline_resolution_document.json")
        for side in SIDES:
            assert pipe_reference[side]["story"]["document"]["ok"]["document"] == expected, side

    def test_projected_events_are_the_committed_bytes_on_both_sides(self, pipe_reference):
        expected = json.loads(_expected("pipeline_events.json"))
        for side in SIDES:
            assert pipe_reference[side]["story"]["pipe_events"]["ok"]["events"] == expected, side


# --------------------------------------------------------------------------
# Spend ledger (schema 1)
# --------------------------------------------------------------------------


def spend_story(base: Path, pattern: Sequence[str]) -> dict:
    """One spend ledger passed between sides: each step runs on the next side in ``pattern``.

    A reservation made by one side is settled, released or refused by another,
    so the receipt a side presents is judged against rows another side wrote.
    """
    base.mkdir(parents=True)
    home = base / "home"
    home.mkdir()
    ledger = base / "spend.sqlite3"
    work = base / "w"
    path = str(ledger)
    steps: list[tuple[str, str, Callable[[dict], dict]]] = [
        ("create", "spend_create", lambda r: dict(path=path, cap_usd=1.0, run_id="spend-run", meta={"k": "v"})),
        ("reserve-a", "spend_reserve", lambda r: dict(path=path, amount_usd=0.40, scope="s", identifier="a", model="m")),
        ("reserve-b", "spend_reserve", lambda r: dict(path=path, amount_usd=0.30, scope="s", identifier="b", model="m")),
        ("settle-a", "spend_settle", lambda r: dict(path=path, reservation=r["reserve-a"]["ok"], cost_usd=0.35)),
        ("over-cap", "spend_reserve", lambda r: dict(path=path, amount_usd=0.50, scope="s", identifier="c", model="m")),
        ("unknown-b", "spend_settle", lambda r: dict(path=path, reservation=r["reserve-b"]["ok"], cost_usd=None)),
        ("stale-settle", "spend_settle", lambda r: dict(path=path, reservation=r["reserve-a"]["ok"], cost_usd=0.35)),
        ("status-1", "spend_status", lambda r: dict(path=path)),
        ("halt", "spend_halt", lambda r: dict(path=path, reason="manual", detail="operator stop")),
        ("halted-reserve", "spend_reserve", lambda r: dict(path=path, amount_usd=0.01, scope="s", identifier="d", model="m")),
        ("resume", "spend_resume", lambda r: dict(path=path)),
        ("reserve-e", "spend_reserve", lambda r: dict(path=path, amount_usd=0.10, scope="s", identifier="e", model="m")),
        ("overbilled", "spend_settle", lambda r: dict(path=path, reservation=r["reserve-e"]["ok"], cost_usd=0.20)),
        ("refused-resume", "spend_resume", lambda r: dict(path=path)),
        ("forced-resume", "spend_resume", lambda r: dict(path=path, force=True)),
        ("status-2", "spend_status", lambda r: dict(path=path)),
        ("reopen", "spend_open", lambda r: dict(path=path)),
    ]
    results: dict[str, dict] = {}
    for index, (label, name, build) in enumerate(steps):
        side = pattern[index % len(pattern)]
        item = run_worker(side, home, work, [op(name, label, **build(results))])[label]
        results[label] = outcome(item)
    dump = ok(run_worker(pattern[0], home, work, [op("dump", db=path)])["dump"])
    scrub = Scrub(base)
    return {
        "steps": scrub.value(results),
        "tables": _norm_ledger(dump["tables"], scrub),
        "schema": dump["schema"],
    }


@pytest.fixture(scope="module")
def spend_reference(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return {side: spend_story(tmp_path_factory.mktemp(f"spend-{side}") / "s", (side,)) for side in SIDES}


class TestSpendLedger:
    def test_the_reference_story_exercises_the_ledger_it_claims(self, spend_reference):
        for side in SIDES:
            steps = spend_reference[side]["steps"]
            assert steps["create"]["ok"]["schema_version"] == 1
            assert steps["reserve-a"]["ok"]["amount_nano"] == 400_000_000
            assert steps["over-cap"]["raised"]["mro"][0] == "SpendCapExceeded"
            assert steps["stale-settle"]["raised"]["mro"][0] == "StaleReservationError"
            assert steps["halted-reserve"]["raised"]["mro"][0] == "SpendLedgerHalted"
            assert steps["overbilled"]["raised"]["mro"][0] == "SpendCapExceeded"
            status = steps["status-2"]["ok"]
            assert status["halted"] is False and status["calls_settled"] >= 1
            assert spend_reference[side]["schema"]["schema_version"] == [1]

    def test_both_sides_agree_when_each_keeps_its_own_ledger(self, spend_reference):
        assert_same(spend_reference[FINAL], spend_reference[LEGACY], "final vs legacy")

    @pytest.mark.parametrize("pattern", [(LEGACY, FINAL), (FINAL, LEGACY)], ids=_label)
    def test_a_ledger_alternating_between_sides_matches_a_ledger_on_one(
        self, pattern, tmp_path, spend_reference
    ):
        story = spend_story(tmp_path / "s", pattern)
        assert_same(story, spend_reference[LEGACY], f"alternating {_label(pattern)}")


# --------------------------------------------------------------------------
# Verdict cache (usage-verdicts.json)
# --------------------------------------------------------------------------

_HALT_DETAIL = "observed quota/credit halt at dispatch"
_POOL = "seven_day"


def _cache_path(home: Path) -> Path:
    return home / ".claude" / "plugins" / "data" / "plugins-kit" / "llm-scripting-kit" / "usage-verdicts.json"


def _snapshot_path(home: Path) -> Path:
    return home / ".claude" / "plugins" / "data" / "plugins-kit" / "claude-ui-kit" / "rate-limits.json"


def _seed_snapshot(home: Path, now: int) -> None:
    """The statusline snapshot both sides read: 10% of the weekly pool used, 3 of 7 days left."""
    path = _snapshot_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"rate_limits": {_POOL: {"used_percentage": 10, "resets_at": now + 3 * 86400}}}
    path.write_text(json.dumps(document), encoding="utf-8")


def _halted_text(session: str, entry: str, now: int) -> str:
    """The exact text the legacy writer leaves after one observed halt."""
    budget = {
        "status": "out-of-quota",
        "pool": _POOL,
        "detail": _HALT_DETAIL,
        "remaining": 0.0,
        "window_remaining": None,
        "resets_at": now + 5 * 3600,
    }
    return json.dumps({"session_key": session, "verdicts": {entry: {"spec": {"pool": _POOL}, "budget": budget}}})


def _read_cache(home: Path) -> dict:
    return json.loads(_cache_path(home).read_text(encoding="utf-8"))


def _assert_legacy_shape(document: Any) -> None:
    """Parseable, and exactly the shape the legacy writer produces."""
    assert isinstance(document, dict) and set(document) == {"session_key", "verdicts"}
    assert isinstance(document["session_key"], str) and document["session_key"]
    for entry, stored in document["verdicts"].items():
        assert set(stored) == {"spec", "budget"}, entry
        assert set(stored["budget"]) == {
            "status", "pool", "detail", "remaining", "window_remaining", "resets_at",
        }, entry


def _pin(side: str, home: Path, work: Path, *, entry: str, session: str, now: int, mode: str = "plain") -> dict:
    return outcome(
        run_worker(
            side, home, work, [op("verdict_pin", "pin", entry=entry, session=session, now=now, mode=mode)]
        )["pin"]
    )


def stale_writer_flow(
    home: Path,
    work: Path,
    *,
    exhausted_by: str,
    stale_by: str,
    session: str,
    now: int,
    exhausted_entry: str = "paced",
    stale_entry: str = "paced",
) -> dict:
    """One writer records exhaustion while another holds a stale snapshot, then writes it.

    The stale writer reads the cache (nothing pinned yet), computes AVAILABLE
    from the snapshot, and pauses before storing. The other writer records the
    halt and stores. The stale writer is released and stores its older view.
    """
    work.mkdir(parents=True, exist_ok=True)
    loaded, go, halted = work / "loaded", work / "go", work / "halted"
    stale = Running(
        stale_by,
        home,
        work / "stale",
        [
            op(
                "verdict_pin",
                "stale",
                entry=stale_entry,
                session=session,
                now=now,
                mode="pause",
                loaded_flag=str(loaded),
                go_flag=str(go),
            )
        ],
    )
    exhausting = Running(
        exhausted_by,
        home,
        work / "halt",
        [
            op("wait_file", "wait", path=str(loaded)),
            op("verdict_halt", "halt", entry=exhausted_entry, session=session, now=now),
            op("touch", "halted", path=str(halted)),
        ],
    )
    deadline = time.monotonic() + 90
    while not halted.exists():
        assert time.monotonic() < deadline, "the exhaustion was never recorded"
        for running in (stale, exhausting):
            assert running.process.poll() in (None, 0), running.process.communicate()
        time.sleep(0.005)
    mid = _read_cache(home)
    go.write_text("1", encoding="ascii")
    stale_item = stale.result()["stale"]
    ok(exhausting.result()["halt"])
    return {"mid": mid, "after": _read_cache(home), "stale": ok(stale_item)}


class TestVerdictCache:
    def test_both_sides_resolve_the_legacy_cache_and_snapshot_paths(self, _scratch_home, tmp_path):
        for side in SIDES:
            info = ok(run_worker(side, _scratch_home, tmp_path / side, [op("verdict_info")])["verdict_info"])
            assert Path(info["cache"]) == _cache_path(_scratch_home), side
            assert Path(info["claude_snapshot"]) == _snapshot_path(_scratch_home), side

    @pytest.mark.parametrize("pair", PAIRS, ids=_label)
    def test_an_observed_halt_written_by_one_side_is_pinned_for_the_other_in_a_session(
        self, pair, _scratch_home, tmp_path
    ):
        writer, reader = pair
        home, now = _scratch_home, int(time.time())
        ok(
            run_worker(
                writer, home, tmp_path / "w", [op("verdict_halt", "halt", entry="paced", session="S1", now=now)]
            )["halt"]
        )
        # Byte for byte what the legacy writer leaves, whichever side wrote it.
        assert _cache_path(home).read_text(encoding="utf-8") == _halted_text("S1", "paced", now)
        read = _pin(reader, home, tmp_path / "r", entry="paced", session="S1", now=now, mode="forbid_evaluate")
        verdict = read["ok"]
        assert verdict["budget"]["status"] == "out-of-quota" and verdict["usable"] is False
        assert verdict["budget"]["resets_at"] == now + 5 * 3600
        # The reader changed nothing: a pinned read is not a write.
        assert _cache_path(home).read_text(encoding="utf-8") == _halted_text("S1", "paced", now)

    @pytest.mark.parametrize("pair", PAIRS, ids=_label)
    def test_an_available_verdict_pinned_by_one_side_is_read_by_the_other(self, pair, _scratch_home, tmp_path):
        writer, reader = pair
        home, now = _scratch_home, int(time.time())
        _seed_snapshot(home, now)
        written = _pin(writer, home, tmp_path / "w", entry="paced", session="S1", now=now)["ok"]
        assert written["budget"]["status"] == "available" and written["usable"] is True
        document = _read_cache(home)
        _assert_legacy_shape(document)
        assert document["session_key"] == "S1" and list(document["verdicts"]) == ["paced"]
        read = _pin(reader, home, tmp_path / "r", entry="paced", session="S1", now=now, mode="forbid_evaluate")
        assert_same(read["ok"], written, "pinned read vs the verdict that was written")

    def test_the_cache_bytes_do_not_depend_on_the_writing_side(self, tmp_path):
        texts = {}
        for side in SIDES:
            home = tmp_path / side / "home"
            home.mkdir(parents=True)
            now = 1_900_000_000
            _seed_snapshot(home, now)
            run_worker(
                side,
                home,
                tmp_path / side / "w",
                [
                    op("verdict_pin", "pin", entry="paced", session="S1", now=now),
                    op("verdict_halt", "halt", entry="other", session="S1", now=now),
                ],
            )
            texts[side] = _cache_path(home).read_text(encoding="utf-8")
        assert texts[FINAL] == texts[LEGACY]
        _assert_legacy_shape(json.loads(texts[LEGACY]))

    @pytest.mark.parametrize("pair", PAIRS, ids=_label)
    def test_writers_from_different_sessions_leave_one_parseable_legacy_shaped_file(
        self, pair, _scratch_home, tmp_path
    ):
        """The file holds one session's dictionary: the last writer's. Nothing is merged."""
        first, second = pair
        home, now = _scratch_home, int(time.time())
        _seed_snapshot(home, now)
        run_worker(first, home, tmp_path / "a", [op("verdict_halt", "halt", entry="paced", session="S1", now=now)])
        assert _read_cache(home)["session_key"] == "S1"
        _pin(second, home, tmp_path / "b", entry="other", session="S2", now=now)
        document = _read_cache(home)
        _assert_legacy_shape(document)
        assert document["session_key"] == "S2"
        assert "other" in document["verdicts"]

    @pytest.mark.parametrize("pair", PAIRS, ids=_label)
    def test_a_stale_writer_restores_available_over_a_recorded_exhaustion(self, pair, _scratch_home, tmp_path):
        """The documented cost, asserted as a cost: last writer wins, and a pinned read believes it.

        One writer records OUT_OF_QUOTA (the file shows it). A writer that read
        the cache before that stores AVAILABLE. A pinned read returns AVAILABLE
        without recomputing, so selection will pick the exhausted endpoint
        again. The design keeps this behaviour (it already exists between
        legacy processes sharing a session key); nothing here asks for it to be
        prevented.
        """
        exhausted_by, stale_by = pair
        home, now = _scratch_home, int(time.time())
        _seed_snapshot(home, now)
        flow = stale_writer_flow(
            home, tmp_path / "flow", exhausted_by=exhausted_by, stale_by=stale_by, session="S1", now=now
        )
        # 1. The exhaustion was durably recorded before the stale writer stored.
        assert flow["mid"]["verdicts"]["paced"]["budget"]["status"] == "out-of-quota"
        # 2. The stale snapshot then replaced it: the file now says AVAILABLE.
        _assert_legacy_shape(flow["after"])
        assert flow["after"]["verdicts"]["paced"]["budget"]["status"] == "available"
        assert flow["stale"]["budget"]["status"] == "available"
        # 3. A pinned read, from either side, returns AVAILABLE without recomputation.
        for reader in SIDES:
            read = _pin(reader, home, tmp_path / f"read-{reader}", entry="paced", session="S1", now=now, mode="forbid_evaluate")
            assert read["ok"]["budget"]["status"] == "available", reader
            assert read["ok"]["usable"] is True, reader
        # 4. So selection retries the endpoint whose exhaustion was recorded.
        for reader in SIDES:
            ranking = ok(
                run_worker(reader, home, tmp_path / f"select-{reader}", [op("verdict_select", "select", session="S1")])["select"]
            )
            assert ranking["dispositions"]["paced"] == "usable", reader
            assert ranking["usability"]["paced"] == "available", reader

    @pytest.mark.parametrize("side", SIDES)
    def test_without_the_stale_writer_selection_skips_the_exhausted_endpoint(self, side, _scratch_home, tmp_path):
        """The control for the test above: the recorded exhaustion does steer selection."""
        home, now = _scratch_home, int(time.time())
        _seed_snapshot(home, now)
        run_worker(side, home, tmp_path / "halt", [op("verdict_halt", "halt", entry="paced", session="S1", now=now)])
        for reader in SIDES:
            ranking = ok(
                run_worker(reader, home, tmp_path / f"select-{reader}", [op("verdict_select", "select", session="S1")])["select"]
            )
            assert ranking["dispositions"]["paced"] == "out-of-quota", reader
            assert ranking["default"] == "backup", reader

    @pytest.mark.parametrize("pair", PAIRS, ids=_label)
    def test_writers_of_different_endpoints_in_one_session_leave_a_parseable_file(
        self, pair, _scratch_home, tmp_path
    ):
        """Both writers store a whole file. The last one's endpoint is there; nothing says the other's is."""
        exhausted_by, stale_by = pair
        home, now = _scratch_home, int(time.time())
        _seed_snapshot(home, now)
        flow = stale_writer_flow(
            home,
            tmp_path / "flow",
            exhausted_by=exhausted_by,
            stale_by=stale_by,
            session="S1",
            now=now,
            exhausted_entry="first-endpoint",
            stale_entry="second-endpoint",
        )
        _assert_legacy_shape(flow["after"])
        assert flow["after"]["session_key"] == "S1"
        assert flow["after"]["verdicts"]["second-endpoint"]["budget"]["status"] == "available"

    def test_unsynchronized_writers_from_both_sides_never_tear_the_file(self, _scratch_home, tmp_path):
        home, now = _scratch_home, int(time.time())
        specs = [
            (LEGACY, [op("verdict_hammer", "hammer", entry="hammer-legacy", session="S-legacy", rounds=80, now=now)]),
            (FINAL, [op("verdict_hammer", "hammer", entry="hammer-final", session="S-final", rounds=80, now=now)]),
        ]
        results = contend(home, tmp_path / "race", specs)
        assert [ok(r["hammer"])["rounds"] for r in results] == [80, 80]
        document = _read_cache(home)
        _assert_legacy_shape(document)
        assert document["session_key"] in ("S-legacy", "S-final")
        prefix = "hammer-legacy" if document["session_key"] == "S-legacy" else "hammer-final"
        assert document["verdicts"] and all(entry.startswith(prefix) for entry in document["verdicts"])
        assert all(v["budget"]["status"] == "out-of-quota" for v in document["verdicts"].values())


# --------------------------------------------------------------------------
# Credentials .env and user config
# --------------------------------------------------------------------------

_USER_DATA = Path(".claude") / "plugins" / "data" / "plugins-kit" / "llm-scripting-kit"
_CONFIG_FLEET = """\
default_endpoint: local-box
"""
_CONFIG_USER = """\
endpoints:
  local-box:
    base_url: http://127.0.0.1:9999/v1
    key_env: CROSS_STATE_KEY
    models:
      fast: {slug: vendor/fast}
    default: fast
"""
_CONFIG_PROJECT = """\
endpoints:
  local-box:
    base_url: http://project.invalid/v1
"""
#: Values chosen to need the writer's quoting rules: spaces, a quote, a backslash.
_ENV_VALUES = {"CROSS_STATE_KEY": "plain-secret-123", "OPENROUTER_API_KEY": ' sp "q" \\b '}
#: What the legacy writer puts on disk for those values (the quoted one is escaped).
_ENV_TEXT = 'CROSS_STATE_KEY=plain-secret-123\nOPENROUTER_API_KEY=" sp \\"q\\" \\\\b "\n'


class TestCredentialsAndConfig:
    def test_both_sides_name_the_same_legacy_locations(self, _scratch_home, tmp_path):
        project = tmp_path / "project"
        paths = {
            side: ok(
                run_worker(side, _scratch_home, tmp_path / side, [op("cred_paths", project_root=str(project))])["cred_paths"]
            )
            for side in SIDES
        }
        expected = {
            "user_env_file": str(_scratch_home / _USER_DATA / ".env"),
            "project_env_file": str(project / ".local-data" / "plugins-kit" / "llm-scripting-kit" / ".env"),
            "legacy_project_env_file": str(project / ".local-data" / "llm-scripting-kit" / ".env"),
        }
        for side in SIDES:
            for key, value in expected.items():
                assert Path(paths[side][key]) == Path(value), (side, key)
        for key in ("user_env_file", "project_env_file", "legacy_project_env_file", "project_env_files", "api_key_env", "base_url"):
            assert paths[FINAL][key] == paths[LEGACY][key], key

    @pytest.mark.parametrize("pair", PAIRS, ids=_label)
    def test_an_env_file_written_by_one_side_is_read_by_the_other(self, pair, _scratch_home, tmp_path):
        writer, reader = pair
        written = ok(
            run_worker(writer, _scratch_home, tmp_path / "w", [op("cred_write", values=_ENV_VALUES)])["cred_write"]
        )
        assert Path(written["path"]) == _scratch_home / _USER_DATA / ".env"
        # Byte for byte what the legacy writer writes (a text-mode file: the platform's newline).
        assert written["bytes"].replace("\r\n", "\n") == _ENV_TEXT
        read = ok(run_worker(reader, _scratch_home, tmp_path / "r", [op("cred_read")])["cred_read"])
        assert read["values"] == _ENV_VALUES
        key = ok(
            run_worker(
                reader, _scratch_home, tmp_path / "k", [op("cred_get_key", project_root=str(tmp_path / "project"))]
            )["cred_get_key"]
        )
        assert key["key"] == _ENV_VALUES["OPENROUTER_API_KEY"] and key["source"] == "user"
        assert Path(key["source_path"]) == _scratch_home / _USER_DATA / ".env"

    def test_the_env_file_bytes_do_not_depend_on_the_writing_side(self, _scratch_home, tmp_path):
        raw = {}
        for side in SIDES:
            target = tmp_path / side / ".env"
            ok(run_worker(side, _scratch_home, tmp_path / f"w-{side}", [op("cred_write", path=str(target), values=_ENV_VALUES)])["cred_write"])
            raw[side] = target.read_bytes()
        assert raw[FINAL] == raw[LEGACY] and raw[LEGACY]

    def test_key_precedence_is_the_same_on_both_sides(self, _scratch_home, tmp_path):
        """Environment, then project, then the superseded project path, then the user file."""
        project = tmp_path / "project"
        modern = project / ".local-data" / "plugins-kit" / "llm-scripting-kit" / ".env"
        superseded = project / ".local-data" / "llm-scripting-kit" / ".env"
        run_worker(
            LEGACY,
            _scratch_home,
            tmp_path / "w",
            [
                op("cred_write", values={"OPENROUTER_API_KEY": "from-user"}),
                op("cred_write", "modern", path=str(modern), values={"OPENROUTER_API_KEY": "from-project"}),
                op("cred_write", "old", path=str(superseded), values={"OPENROUTER_API_KEY": "from-superseded"}),
            ],
        )

        def lookup(side: str, env: dict, root: Optional[Path]) -> dict:
            args = {"env": env}
            if root is not None:
                args["project_root"] = str(root)
            item = run_worker(side, _scratch_home, tmp_path / f"{side}-{len(env)}", [op("cred_get_key", **args)])
            return ok(item["cred_get_key"])

        for side in SIDES:
            assert lookup(side, {"OPENROUTER_API_KEY": "from-env"}, project)["source"] == "env", side
            assert lookup(side, {}, project)["key"] == "from-project", side
            modern.unlink()
            try:
                result = lookup(side, {}, project)
                assert (result["key"], result["legacy_location"]) == ("from-superseded", True), side
                superseded.unlink()
                try:
                    assert lookup(side, {}, project)["key"] == "from-user", side
                finally:
                    superseded.parent.mkdir(parents=True, exist_ok=True)
                    superseded.write_text("OPENROUTER_API_KEY=from-superseded\n", encoding="utf-8")
            finally:
                modern.parent.mkdir(parents=True, exist_ok=True)
                modern.write_text("OPENROUTER_API_KEY=from-project\n", encoding="utf-8")

    def test_user_config_laid_down_for_legacy_is_resolved_identically_by_llm_kit(self, _scratch_home, tmp_path):
        project = tmp_path / "project"
        layers = {
            _scratch_home / ".claude" / "config" / "llm-scripting-kit.yaml": _CONFIG_FLEET,
            _scratch_home / _USER_DATA / "config.yaml": _CONFIG_USER,
            project / ".local-data" / "plugins-kit" / "llm-scripting-kit" / "config.yaml": _CONFIG_PROJECT,
        }
        for path, text in layers.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        run_worker(LEGACY, _scratch_home, tmp_path / "seed", [op("cred_write", values={"CROSS_STATE_KEY": "box-key"})])
        results = {}
        for side in SIDES:
            stage = run_worker(
                side,
                _scratch_home,
                tmp_path / side,
                [
                    op("config_load", "config", project_root=str(project)),
                    op("cred_get_key", "key", project_root=str(project), endpoint="local-box"),
                ],
            )
            results[side] = Scrub(tmp_path, _scratch_home).value({"config": ok(stage["config"]), "key": ok(stage["key"])})
        assert_same(results[FINAL], results[LEGACY], "config and key resolved by llm-kit vs legacy")
        resolved = results[LEGACY]["config"]["endpoint"]
        assert resolved["base_url"] == "http://project.invalid/v1"  # the project layer wins
        assert resolved["key_env"] == "CROSS_STATE_KEY"  # the user layer supplied it
        assert results[LEGACY]["key"]["key"] == "box-key" and results[LEGACY]["key"]["source"] == "user"

    def test_llm_kit_seeds_no_user_layer_of_its_own(self, _scratch_home, tmp_path):
        """Reading credentials and config through llm-kit creates nothing named for llm-kit."""
        project = tmp_path / "project"
        run_worker(LEGACY, _scratch_home, tmp_path / "seed", [op("cred_write", values=_ENV_VALUES)])
        before = sorted(p.relative_to(_scratch_home).as_posix() for p in _scratch_home.rglob("*"))
        run_worker(
            FINAL,
            _scratch_home,
            tmp_path / "final",
            [
                op("cred_read"),
                op("cred_get_key", project_root=str(project)),
                op("config_load", project_root=str(project)),
            ],
        )
        after = sorted(p.relative_to(_scratch_home).as_posix() for p in _scratch_home.rglob("*"))
        assert after == before
        assert not [p for p in after if "llm-kit" in p]


class TestDeliberateDifferences:
    def test_the_cli_command_is_the_one_constant_that_names_the_llm_kit_venv(self, _scratch_home, tmp_path):
        commands = {
            side: ok(
                run_worker(side, _scratch_home, tmp_path / side, [op("cred_paths", project_root=str(tmp_path))])["cred_paths"]
            )["cli_command"]
            for side in SIDES
        }
        assert "/llm-scripting-kit/.venv/" in commands[LEGACY]
        assert "/llm-kit/.venv/" in commands[FINAL]
        assert commands[FINAL].replace("llm-kit", "llm-scripting-kit") == commands[LEGACY]

    def test_every_patched_name_the_scenarios_rely_on_exists_on_both_sides(self, tmp_path):
        """A stale patch target is a failure, never a silently unpatched test."""
        patched = {
            side: ok(run_worker(side, tmp_path / "h", tmp_path / side, [op("patch_targets")])["patch_targets"])["patched"]
            for side in SIDES
        }
        assert patched[FINAL] == patched[LEGACY]
        assert len(patched[LEGACY]) == 5
