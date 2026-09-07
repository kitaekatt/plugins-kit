"""Endpoint reachability checks -- distinct from configuration.

``endpoints`` lists what is CONFIGURED (adapter, base_url, model, name): pure
static data, no network, no subprocess, always instant. This module answers a
different question -- is a configured endpoint ACTUALLY USABLE right now --
and it NEVER issues a completion to answer it. Zero LLM calls, always:

- **transport** entries (the ``openrouter`` adapter, including a self-hosted
  OpenAI-compatible server): a ``GET {base_url}/models`` metadata request
  (:func:`llm_scripting_kit.account.probe_endpoint`). This proves the server
  is up and answering HTTP -- it does NOT prove a completion would succeed. A
  model can be unloaded or a worker wedged behind a perfectly healthy
  ``/models`` response, which is why a passing verdict is named
  ``STATUS_REACHABLE`` rather than ``available`` or ``healthy`` -- those would
  claim more than a metadata probe can support.
- **harness** entries (``claude-cli``, ``codex-cli``, ``opencode-cli``): the
  underlying CLI resolves on PATH and answers ``--version`` within the
  timeout. This establishes the harness is INVOCABLE -- weaker still than the
  transport check, since it says nothing about whether the harness's own
  model access works. A real completion would spawn an agent and cost real
  time and, for a subscription CLI, real quota, so it is never attempted here.

THREE outcomes, not two. A boolean ``reachable`` cannot distinguish "I checked
and it is down" from "I could not check" -- and collapsing the second into the
first is a false negative a caller cannot see: gating on ``reachable is
False`` would skip a perfectly usable endpoint whose check merely failed to
run (an optional dependency missing, an unexpected exception in the check
machinery itself). So the verdict is :data:`STATUS_REACHABLE`,
:data:`STATUS_UNREACHABLE`, or :data:`STATUS_UNKNOWN` -- a plain string, the
same convention :class:`~.completion.types.LLMResponse.status` already uses in
this package, rather than a bool that invites exactly that misreading. This is
the same honesty rule that produced the ``reachable`` name in the first place
(a metadata probe cannot claim ``available``), applied one level down: a
FAILED check cannot claim ``unreachable`` either.

Two call sites share this module rather than each owning a check: the
``endpoints --verify`` flag (batch, best-effort, concurrent) and the ``probe``
verb (one endpoint, exit code is the answer). Same code, two surfaces.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .account import probe_endpoint
from .harness_adapters import resolve_opencode_cli

DEFAULT_VERIFY_TIMEOUT_S = 5.0
"""Short on purpose. A caller reaching for ``--verify`` or ``probe`` is asking
precisely because it does not want to block on a dead target -- queueing work
against an endpoint that will not answer, rather than losing the work item
waiting to find that out. A live ``/models`` endpoint answers in well under a
second and a present CLI's ``--version`` returns near-instantly, so 5s is
generous headroom for a slow LAN hop while still failing a genuinely dead
target fast."""

STATUS_REACHABLE = "reachable"
"""The check ran and the target answered."""

STATUS_UNREACHABLE = "unreachable"
"""The check ran and the target did NOT answer (dead host, missing CLI,
nonzero exit, timeout, ...) -- a real, checked verdict."""

STATUS_UNKNOWN = "unknown"
"""The check could NOT be run to a verdict at all -- e.g. an optional
dependency (``bootstrap_lib``) was unavailable, or the check machinery raised
unexpectedly. This is NEVER reported as :data:`STATUS_UNREACHABLE`: "I could
not check" and "I checked and it is down" are different facts, and a caller
gating on a boolean-shaped reading must not be able to conflate them."""

_KNOWN_HARNESSES = ("claude", "codex", "opencode")


@dataclass(frozen=True)
class Reachability:
    """The outcome of one reachability check. Constructors never raise.

    ``status`` is one of :data:`STATUS_REACHABLE`, :data:`STATUS_UNREACHABLE`,
    or :data:`STATUS_UNKNOWN` -- see the module docstring for why a third
    state exists and why it is not folded into a bool. ``checked`` names the
    METHOD actually exercised (``"models-endpoint"`` or ``"cli-version"``),
    which is a separate axis from ``status`` -- a check can be attempted by a
    known method and still land on any of the three verdicts (a
    ``"cli-version"`` check that could not even resolve ``bootstrap_lib``
    still names ``"cli-version"`` as the method it was TRYING to use).
    ``detail`` is a short human-readable reason: the version banner or ``ok``
    on success, a specific failure otherwise. ``detail`` itself never carries
    a key, a token, or an Authorization header. This is narrower than "this
    module never reads a credential": a transport check on a keyed endpoint
    delegates to :func:`llm_scripting_kit.account.probe_endpoint`, which does
    resolve a key via ``get_api_key`` and sends it as a Bearer token to
    perform the check -- it is simply never echoed back into this result.
    """

    status: str
    checked: str
    detail: str

    def to_json(self) -> Dict[str, Any]:
        return {"status": self.status, "checked": self.checked, "detail": self.detail}


def _resolve_cli(name: str) -> Optional[List[str]]:
    """Resolve ``name`` to a launchable argv prefix, honoring Windows shims.

    Stdlib-only local twin of ``bootstrap_lib.codex.resolve_cli`` -- kept
    dependency-free deliberately so it works as the PATH-only FALLBACK for
    codex when ``bootstrap_lib`` itself is not importable, and so claude gets
    the identical Windows-safe resolution opencode already has via
    :func:`.harness_adapters.resolve_opencode_cli`.
    """
    resolved = shutil.which(name)
    if resolved is None:
        return None
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", resolved]
    return [resolved]


def check_transport(
    name: str, *, timeout: float = DEFAULT_VERIFY_TIMEOUT_S, project_root: Optional[str] = None
) -> Reachability:
    """Metadata-only liveness check for a transport (openrouter-adapter) endpoint.

    Delegates to :func:`llm_scripting_kit.account.probe_endpoint`, which never
    raises and never sends more than a ``GET {base_url}/models`` request. When
    the endpoint NAME itself resolves (``EndpointProbe.resolved``), a real
    request was attempted and the verdict is :data:`STATUS_REACHABLE` or
    :data:`STATUS_UNREACHABLE`. A RESOLVE failure -- an unknown endpoint or an
    unreadable/dangling model-endpoints registry -- means no request was ever
    attempted, so it is :data:`STATUS_UNKNOWN` ("I could not check"), never
    :data:`STATUS_UNREACHABLE` ("I checked and it is down").
    """
    result = probe_endpoint(name, timeout=timeout, project_root=project_root)
    if not result.resolved:
        return Reachability(status=STATUS_UNKNOWN, checked="models-endpoint", detail=result.detail)
    status = STATUS_REACHABLE if result.ok else STATUS_UNREACHABLE
    return Reachability(status=status, checked="models-endpoint", detail=result.detail)


def check_harness(harness: Optional[str], *, timeout: float = DEFAULT_VERIFY_TIMEOUT_S) -> Reachability:
    """CLI-presence check for a harness entry. Never spawns a model run.

    Resolution plus a bounded ``--version`` invocation -- enough to say the
    harness is invocable, nothing more. A caller must not read
    :data:`STATUS_REACHABLE` here as "a completion through this harness would
    succeed".
    """
    key = (harness or "").strip().lower()
    if key not in _KNOWN_HARNESSES:
        shown = harness if harness else "<none>"
        # Not "unreachable": nothing was actually checked, since there is no
        # known method for a harness name this module has never heard of.
        return Reachability(
            status=STATUS_UNKNOWN, checked="cli-version",
            detail=f"no reachability check registered for harness {shown!r} (known: {', '.join(_KNOWN_HARNESSES)})",
        )
    if key == "codex":
        return _check_codex(timeout=timeout)
    if key == "opencode":
        prefix = resolve_opencode_cli()
        if prefix is None:
            return Reachability(status=STATUS_UNREACHABLE, checked="cli-version", detail="`opencode` not found on PATH")
        return _run_version_probe(list(prefix), label="opencode", timeout=timeout)
    # claude
    resolved = _resolve_cli("claude")
    if resolved is None:
        return Reachability(status=STATUS_UNREACHABLE, checked="cli-version", detail="`claude` not found on PATH")
    return _run_version_probe(resolved, label="claude", timeout=timeout)


def _check_codex(*, timeout: float) -> Reachability:
    """Prefer ``bootstrap_lib.detect_codex``; fall back to a bare PATH probe.

    ``bootstrap_lib`` is this plugin's OPTIONAL shared-lib dependency (see
    this plugin's CLAUDE.md: "consumers must declare bootstrap_lib
    themselves"). Its absence says nothing about whether codex ITSELF is
    installed and working -- codex-cli can be present and fully functional on
    a machine whose llm-scripting-kit venv simply never linked bootstrap_lib.
    Treating that ImportError as "codex unreachable" was a FALSE NEGATIVE
    (observed live: codex-cli 0.150.1 installed and in active use, reported
    unreachable purely because this module's own optional import failed).

    So on ImportError this falls back to exactly the PATH + ``--version``
    check claude and opencode already use -- a real, checked verdict rather
    than a manufactured one. ``detect_codex`` stays the PREFERRED path when
    importable: it is cached, it parses a structured version, and it is the
    same detector bootstrap's own tooling relies on elsewhere.
    """
    try:
        from bootstrap_lib.codex import detect_codex  # noqa: PLC0415
    except ImportError:
        resolved = _resolve_cli("codex")
        if resolved is None:
            return Reachability(status=STATUS_UNREACHABLE, checked="cli-version", detail="`codex` not found on PATH")
        return _run_version_probe(resolved, label="codex", timeout=timeout)
    detection = detect_codex(timeout=timeout)
    status = STATUS_REACHABLE if detection.available else STATUS_UNREACHABLE
    return Reachability(status=status, checked="cli-version", detail=detection.reason)


def _run_version_probe(argv: List[str], *, label: str, timeout: float) -> Reachability:
    shown = f"{label} --version"
    try:
        proc = subprocess.run(
            [*argv, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return Reachability(status=STATUS_UNREACHABLE, checked="cli-version", detail=f"`{shown}` timed out after {timeout:g}s")
    except OSError as exc:
        return Reachability(
            status=STATUS_UNREACHABLE, checked="cli-version", detail=f"`{shown}` did not run ({type(exc).__name__})"
        )
    if proc.returncode != 0:
        return Reachability(status=STATUS_UNREACHABLE, checked="cli-version", detail=f"`{shown}` exited {proc.returncode}")
    lines = [
        line.strip() for line in proc.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()
    ]
    return Reachability(status=STATUS_REACHABLE, checked="cli-version", detail=lines[0] if lines else "detected")


def check_entry(
    entry_json: Dict[str, Any],
    name: str,
    *,
    timeout: float = DEFAULT_VERIFY_TIMEOUT_S,
    project_root: Optional[str] = None,
) -> Reachability:
    """Dispatch by ``entry_json["kind"]`` -- the same shape the ``endpoints`` verb emits.

    Wrapped in a catch-all: every check function below is already written to
    return a verdict rather than raise, but an unexpected exception anywhere
    in that path must still surface as :data:`STATUS_UNKNOWN`, never crash the
    caller and never read as :data:`STATUS_UNREACHABLE` -- the same rule
    ``_check_codex`` applies to its one known failure mode, generalized so a
    future, unanticipated one cannot regress it silently.
    """
    try:
        if entry_json.get("kind") == "harness":
            return check_harness(entry_json.get("harness"), timeout=timeout)
        return check_transport(name, timeout=timeout, project_root=project_root)
    except Exception as exc:  # noqa: BLE001 -- see docstring: unknown, never a crash, never "down"
        return Reachability(
            status=STATUS_UNKNOWN, checked="unknown",
            detail=f"reachability check raised unexpectedly: {type(exc).__name__}: {exc}",
        )


def check_many(
    entries: Dict[str, Dict[str, Any]],
    *,
    timeout: float = DEFAULT_VERIFY_TIMEOUT_S,
    project_root: Optional[str] = None,
    max_workers: int = 8,
) -> Dict[str, Reachability]:
    """Verify every entry concurrently so a full-list ``--verify`` is not the
    sum of every individual timeout. Not over-built: a plain thread pool, no
    retry, no backoff -- each check is already bounded and non-raising.
    """
    if not entries:
        return {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(entries))) as pool:
        futures = {
            name: pool.submit(check_entry, entry, name, timeout=timeout, project_root=project_root)
            for name, entry in entries.items()
        }
        return {name: future.result() for name, future in futures.items()}


__all__ = [
    "DEFAULT_VERIFY_TIMEOUT_S",
    "STATUS_REACHABLE",
    "STATUS_UNREACHABLE",
    "STATUS_UNKNOWN",
    "Reachability",
    "check_transport",
    "check_harness",
    "check_entry",
    "check_many",
]
