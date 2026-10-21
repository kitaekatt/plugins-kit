"""Shared harness for the llm-kit CLI tests (and its own self-checks).

Two things live here:

* a fake environment every CLI test uses: HOME and USERPROFILE point at a temp
  directory (and are verified to resolve there), every variable that steers
  credentials or the registry is scrubbed, the bootstrap re-exec is skipped, and
  persisted-state constants that are bound at import time are re-pointed at the
  temp directory on every module that holds a copy;
* a pair of "sides" for differential parity. The legacy side imports the frozen
  plugins' CLIs (llm-scripting-kit, job-kit, content-pipeline-kit) from their own
  lib directories with a ``sys.path`` insert scoped to a module fixture; the final
  side is ``llm_kit.cli``. A scenario is a function of a side, so the same call
  vector runs on both and the outcomes are compared.

Nothing here makes a network call or starts a real model: backends are fakes,
reachability checks are replaced, and no process outside the test is started.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import re
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

import pytest

from final_wording import claude_contract_to_final

REPO = Path(__file__).resolve().parents[2]
LEGACY_LIBS = (
    REPO / "plugins" / "job-kit" / "lib",
    REPO / "plugins" / "llm-scripting-kit" / "lib",
    REPO / "plugins" / "content-pipeline-kit" / "lib",
)
LEGACY_ROOTS = ("job_kit", "llm_scripting_kit", "content_pipeline", "bootstrap_guard")

#: Variables that steer credentials, the registry, routing or pacing. Every CLI
#: test starts with all of them unset.
SCRUBBED_ENV = (
    "MODEL_ENDPOINTS_REGISTRY",
    "OPENROUTER_API_KEY",
    "CLAUDE_BOOTSTRAP_DATA_ROOT",
    "CLAUDE_PLUGIN_TEST",
    "CLAUDE_PLUGIN_TEST_MARKETPLACE",
    "LLM_SCRIPTING_KIT_USAGE_SESSION",
    "LLM_SCRIPTING_KIT_MODELS",
    "LOCAL_KEY",
    "FRONTDOOR_HOST",
    "FRONTDOOR_PORT",
    "_BOOTSTRAP_GUARD_VENV_REEXEC",
)


@dataclass(frozen=True)
class Outcome:
    """What one CLI invocation did, in comparable form."""

    code: Any
    out: str
    err: str


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME, verified to be the one every home lookup resolves to."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    for name in SCRUBBED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("_BOOTSTRAP_GUARD_SKIP_REEXEC", "1")
    assert Path.home() == h
    assert os.path.expanduser("~") == str(h)
    return h


def run(main: Callable[..., Any], argv: Sequence[str], capsys: pytest.CaptureFixture[str],
        *, stdin: Optional[str] = None, stdin_bytes: Optional[bytes] = None,
        monkeypatch: Optional[pytest.MonkeyPatch] = None) -> Outcome:
    """Call ``main(argv)`` and capture its exit code, stdout and stderr.

    A ``SystemExit`` (argparse, ``--version``) is recorded as the code, not raised.
    """
    capsys.readouterr()
    patch = contextlib.nullcontext() if monkeypatch is None else contextlib.ExitStack()
    with patch:
        if monkeypatch is not None:
            if stdin_bytes is not None:
                monkeypatch.setattr(
                    sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_bytes), encoding="utf-8")
                )
            elif stdin is not None:
                monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        try:
            code: Any = main(list(argv))
        except SystemExit as exc:
            code = exc.code
    captured = capsys.readouterr()
    return Outcome(code, captured.out, captured.err)


_TIME_KEYS = frozenset({
    "started_at", "ended_at", "created_at", "updated_at", "finished_at", "wall_ms", "ts", "timestamp",
    "lease_expires_at", "claimed_at", "heartbeat_at", "completed_at", "accepted_at", "failed_at",
    "read_at", "reserved_at", "settled_at", "at",
})


def scrub_json(value: Any) -> Any:
    """``value`` with every wall-clock field replaced by a marker.

    A claude-cli ``structured_output`` record is put in the final form on both
    sides (llm-kit delivers a contract natively; the legacy record by prompt).
    """
    if isinstance(value, dict):
        return claude_contract_to_final({
            k: ("<t>" if (k in _TIME_KEYS or k.endswith("_at")) else scrub_json(v))
            for k, v in value.items()
        })
    if isinstance(value, list):
        return [scrub_json(v) for v in value]
    return value


def normalize(text: str, replacements: Mapping[str, str]) -> str:
    """Stable form of CLI text: temp paths and the executable token replaced."""
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def normalize_json_text(text: str, replacements: Mapping[str, str]) -> Any:
    """Parse JSON-lines/JSON output after path normalization and time scrubbing."""
    text = normalize(text, replacements)
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        return scrub_json(json.loads(stripped))
    except json.JSONDecodeError:
        return [scrub_json(json.loads(line)) for line in stripped.splitlines()]


# --------------------------------------------------------------------------
# The two sides
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def legacy_mods() -> Iterator[types.SimpleNamespace]:
    """The frozen legacy CLIs, imported from their own lib directories.

    The ``sys.path`` insert and the ``sys.modules`` entries are removed at module
    teardown, so the legacy import roots never leak into other test modules.
    """
    before = set(sys.modules)
    for entry in map(str, LEGACY_LIBS):
        sys.path.insert(0, entry)
    try:
        ns = types.SimpleNamespace(
            cli=importlib.import_module("llm_scripting_kit.cli"),
            jobs_cli=importlib.import_module("job_kit.cli"),
            jobs_model=importlib.import_module("job_kit.model"),
            jobs_store=importlib.import_module("job_kit.store"),
            jobs_run=importlib.import_module("job_kit.run"),
            api_key=importlib.import_module("llm_scripting_kit.api_key"),
            factory=importlib.import_module("llm_scripting_kit.completion.factory"),
            types=importlib.import_module("llm_scripting_kit.completion.types"),
            reach=importlib.import_module("llm_scripting_kit.reachability"),
            account=importlib.import_module("llm_scripting_kit.account"),
            swapper=importlib.import_module("llm_scripting_kit.swapper"),
            swapper_acceptance=importlib.import_module("llm_scripting_kit.swapper_acceptance"),
            frontdoor_acceptance=importlib.import_module("llm_scripting_kit.frontdoor.acceptance"),
            frontdoor=importlib.import_module("llm_scripting_kit.frontdoor"),
            cpk_run=importlib.import_module("content_pipeline.cli.run"),
            cpk_scaffold=importlib.import_module("content_pipeline.cli.scaffold"),
            cpk_store=importlib.import_module("content_pipeline.execution.store"),
            usage=importlib.import_module("llm_scripting_kit.usage_budget"),
            models=importlib.import_module("llm_scripting_kit.models"),
            declaration=importlib.import_module("llm_scripting_kit.declaration"),
            client=importlib.import_module("llm_scripting_kit.client"),
            seats=importlib.import_module("llm_scripting_kit.seats"),
            process=importlib.import_module("llm_scripting_kit._swapper_process"),
        )
        yield ns
    finally:
        for entry in map(str, LEGACY_LIBS):
            with contextlib.suppress(ValueError):
                sys.path.remove(entry)
        for name in list(sys.modules):
            if name not in before and name.split(".")[0] in LEGACY_ROOTS:
                del sys.modules[name]


@pytest.fixture(scope="module")
def final_mods() -> types.SimpleNamespace:
    """The final CLI package and the final modules the scenarios patch."""
    return types.SimpleNamespace(
        cli=importlib.import_module("llm_kit.cli"),
        common=importlib.import_module("llm_kit.cli._common"),
        account=importlib.import_module("llm_kit.cli.account"),
        completion=importlib.import_module("llm_kit.cli.completion"),
        models_cli=importlib.import_module("llm_kit.cli.models"),
        local=importlib.import_module("llm_kit.cli.local"),
        jobs_cli=importlib.import_module("llm_kit.cli.jobs"),
        pipeline=importlib.import_module("llm_kit.cli.pipeline"),
        run=importlib.import_module("llm_kit.cli.run"),
        compose=importlib.import_module("llm_kit.cli._compose"),
        jobs_model=importlib.import_module("llm_kit.jobs.model"),
        jobs_store=importlib.import_module("llm_kit.jobs.store"),
        jobs_run=importlib.import_module("llm_kit.jobs.run"),
        api_key=importlib.import_module("llm_kit.registry.api_key"),
        factory=importlib.import_module("llm_kit.dispatch.factory"),
        types=importlib.import_module("llm_kit.completion.types"),
        reach=importlib.import_module("llm_kit.dispatch.reachability"),
        account_adapter=importlib.import_module("llm_kit.adapters.account"),
        swapper=importlib.import_module("llm_kit.adapters.swapper"),
        swapper_acceptance=importlib.import_module("llm_kit.adapters.swapper_acceptance"),
        frontdoor_acceptance=importlib.import_module("llm_kit.frontdoor.acceptance"),
        frontdoor=importlib.import_module("llm_kit.frontdoor"),
        scaffold=importlib.import_module("llm_kit.scaffold"),
        exec_store=importlib.import_module("llm_kit.pipeline.execution.store"),
        usage=importlib.import_module("llm_kit.dispatch.usage_budget"),
        models=importlib.import_module("llm_kit.registry.models"),
        declaration=importlib.import_module("llm_kit.dispatch.declaration"),
        client=importlib.import_module("llm_kit.adapters.client"),
        seats=importlib.import_module("llm_kit.dispatch.seats"),
        process=importlib.import_module("llm_kit.adapters._swapper_process"),
    )


class FakeBackend:
    """A completion backend that records its call and returns or raises on cue."""

    name = "fake"

    def __init__(self, response: Any = None, error: Optional[BaseException] = None,
                 halt: Optional[str] = None) -> None:
        self.response = response
        self.error = error
        self.halt = halt
        self.call: Optional[tuple] = None

    def complete(self, system: str, user: str, *, model: str, options: Any = None) -> Any:
        self.call = (system, user, model, options)
        if self.error is not None:
            raise self.error
        return self.response

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        return self.halt
