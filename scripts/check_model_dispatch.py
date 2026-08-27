#!/usr/bin/env python3
"""Validate orchestration.yaml's model identifiers BY REAL DISPATCH.

Why this exists
---------------
The orchestrate skill's rendered policy names model identifiers that agents
then hand to a CLI. Two incidents established that those identifiers cannot be
validated by inspection:

  * the bare codenames (`sol`, `luna`) are NOT dispatchable via `-m`; only the
    fully qualified `gpt-5.6-*` forms are. A typo shipped.
  * the Codex effort dial is `-c model_reasoning_effort=<...>`, a CONFIG KEY
    that `codex exec --help` does not list. Its spelling is confirmable only
    with `--strict-config`, which rejects an unknown key at launch.

The offline suite (tests/awesome-kit/test_orchestration_guidance.py,
TestNoBareCodenames) asserts only that a codename carries a `gpt-5.6-` prefix.
That catches the exact typo that shipped; it cannot catch the CLASS of defect,
because a renamed or retired model still has the right SHAPE. Only a live call
distinguishes "well-formed" from "dispatchable", and a live call cannot live in
an offline, network-free, cost-free pytest run. Hence: a repo-root script, run
on demand.

What it checks
--------------
Everything is derived from the shipped policy, so a routing row added tomorrow
is probed tomorrow with no edit here:

  * every `routing[].models[]` name with the `agent:` prefix, via
    `claude -p --model <id>` with a one-word prompt. The prefix is the reserved
    Agent-tool namespace, so the external model registry is not consulted;
  * every other `routing[].models[]` name, by delegating discovery to the
    orchestrate guidance resolver. Its harness entry supplies both the
    harness (`codex` or `opencode`) and the model id that harness receives --
    the routing name itself is never passed to a CLI;
  * every `-m <id>` hardcoded in a `backends[].command`, via that backend's own
    launch form. A backend can name a model in its command even when routing
    does not name it, and an unchecked hardcoded id is exactly the defect class
    above;
  * every `-c <key>=` config key named anywhere in orchestration.yaml or in
    references/codex-dispatch.md, via `codex exec --strict-config` with an
    EMPTY prompt -- an unknown key is rejected while loading config, before any
    model call, so key validation costs zero tokens.

An unresolved routing name is itself a failed check: it is listed with the
resolver's reason and never silently omitted. Cost: one trivial dispatch per
resolved model occurrence, plus one zero-token launch per config key.

Usage
-----
    uv run python scripts/check_model_dispatch.py            # everything
    uv run python scripts/check_model_dispatch.py --backend codex
    uv run python scripts/check_model_dispatch.py --backend opencode
    uv run python scripts/check_model_dispatch.py --list     # no dispatch

Exit 0 = every selected identifier dispatched. Exit 1 = a dispatch ran and
rejected at least one identifier. Exit 2 = one or more identifiers could not
be validated (unresolved name, missing CLI, or no identifiers selected), which
is deliberately NOT reported as a pass.

Where this does NOT run
-----------------------
Nowhere automatically. It is not in the pytest suite (network, cost), not in
the pre-commit chain (a commit-time network call is walked around with
`--no-verify` the first time it is slow), and not in publish.py's preflight (a
publish must not depend on a live vendor API or spend usage). So a stale model
id reaches consumers if nobody runs this. That is a real gap and is stated
rather than papered over: the check is a tool, not a gate.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATE = REPO_ROOT / "plugins" / "awesome-kit" / "skills" / "orchestrate"
POLICY_PATH = ORCHESTRATE / "defaults" / "orchestration.yaml"
DISPATCH_DOC = ORCHESTRATE / "references" / "codex-dispatch.md"

# The probe prompt. Deliberately the cheapest thing that still forces a real
# round trip to the named model.
PROMPT = "Reply with exactly: ok"

# The `agent:` namespace is fixed by the Agent tool. Every other routing name
# must resolve through llm-scripting-kit to one of these CLI harnesses.
AGENT_MODEL_PREFIX = "agent:"
AGENT_MODEL_NAMES = frozenset(("fable", "opus", "sonnet", "haiku"))
HARNESS_NAMES = frozenset(("codex", "opencode"))

# Config keys whose probe value is not derivable from the policy.
CONFIG_PROBE_VALUES = {
    "sandbox_workspace_write.network_access": "true",
}
DEFAULT_CONFIG_PROBE_VALUE = "low"

MODEL_TIMEOUT = 300
CONFIG_TIMEOUT = 60


class Probe:
    """One thing to validate, with the source location that names it."""

    def __init__(
        self,
        kind: str,
        value: str,
        where: str,
        extra: str = "",
        *,
        effort: str | None = None,
        is_routing: bool = False,
    ) -> None:
        self.kind = kind  # "claude-model" | "codex-model" | "opencode-model" | ...
        self.value = value
        self.where = where  # file + key path, for the failure message
        self.extra = extra  # human note, e.g. entry id or resolution reason
        self.effort = effort
        self.is_routing = is_routing

    @property
    def backend(self) -> str | None:
        """Return the dispatch harness, or None for an unresolved name."""
        if self.kind == "unresolved-model":
            return None
        return self.kind.split("-", 1)[0]

    def __str__(self) -> str:
        tail = f" ({self.extra})" if self.extra else ""
        return f"{self.kind} {self.value}{tail}\n    from {self.where}"


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:  # pragma: no cover - defensive
        return str(path)


def _load_harness_models(project_root: Path) -> tuple[Mapping[str, Any], list[str]]:
    """Use the guidance resolver, including its shared-lib feature detection.

    This import stays lazy because repo-script tests must load this module
    without re-executing into a plugin venv. The imported function is the
    orchestrate renderer's resolver, not a second implementation of its
    llm-scripting-kit compatibility checks.
    """
    scripts_dir = ORCHESTRATE / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from orchestration_guidance import (  # noqa: PLC0415
        discover_model_definitions,
    )

    return discover_model_definitions(project_root)


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    """Read the normalized resolver record from a mapping or dataclass."""
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _resolution_reason(
    name: str,
    model_entries: Mapping[str, Any],
    model_notes: Sequence[str],
) -> str:
    """Explain why one routing name has no dispatchable harness entry."""
    matching = [
        note
        for note in model_notes
        if f"`{name}`" in note or f"'{name}'" in note
    ]
    if matching:
        return "; ".join(matching)
    if not model_entries and model_notes:
        return "; ".join(model_notes)
    return f"no harness model entry named `{name}` resolves"


def _routing_probe(
    raw_model: Any,
    where: str,
    model_entries: Mapping[str, Any],
    model_notes: Sequence[str],
) -> Probe:
    """Classify one routing name and preserve every resolution failure."""
    shown = str(raw_model)
    if not isinstance(raw_model, str) or not raw_model:
        return Probe(
            "unresolved-model",
            shown,
            where,
            "routing model name is not a non-empty string",
            is_routing=True,
        )

    if raw_model.startswith(AGENT_MODEL_PREFIX):
        model = raw_model[len(AGENT_MODEL_PREFIX):]
        if model not in AGENT_MODEL_NAMES:
            return Probe(
                "unresolved-model",
                raw_model,
                where,
                f"unknown Agent-tool model `{model}`",
                is_routing=True,
            )
        return Probe(
            "claude-model",
            model,
            where,
            f"entry={raw_model}",
            is_routing=True,
        )

    if ":" in raw_model:
        return Probe(
            "unresolved-model",
            raw_model,
            where,
            "only `agent:` is a reserved namespace; other names must be unprefixed",
            is_routing=True,
        )

    entry = model_entries.get(raw_model)
    if entry is None:
        return Probe(
            "unresolved-model",
            raw_model,
            where,
            _resolution_reason(raw_model, model_entries, model_notes),
            is_routing=True,
        )

    harness = _record_value(entry, "harness")
    model = _record_value(entry, "model")
    if not isinstance(harness, str) or not harness:
        reason = f"harness entry `{raw_model}` has no harness"
        return Probe("unresolved-model", raw_model, where, reason, is_routing=True)
    if harness not in HARNESS_NAMES:
        reason = (
            f"harness entry `{raw_model}` names unsupported harness `{harness}`; "
            "known harnesses: codex, opencode"
        )
        return Probe("unresolved-model", raw_model, where, reason, is_routing=True)
    if not isinstance(model, str) or not model:
        reason = f"harness entry `{raw_model}` has no model id"
        return Probe("unresolved-model", raw_model, where, reason, is_routing=True)

    effort = _record_value(entry, "effort")
    effort_text = effort if isinstance(effort, str) and effort else None
    details = [f"entry={raw_model}"]
    if effort_text:
        details.append(f"effort={effort_text}")
    return Probe(
        f"{harness}-model",
        model,
        where,
        ", ".join(details),
        effort=effort_text,
        is_routing=True,
    )


def collect_probes(
    policy: dict[str, Any],
    policy_path: Path = POLICY_PATH,
    *,
    model_entries: Mapping[str, Any] | None = None,
    model_notes: Sequence[str] = (),
) -> list[Probe]:
    probes: list[Probe] = []
    policy_name = _rel(policy_path)

    routing_rows = policy.get("routing") or []
    if model_entries is None:
        needs_harness_models = False
        if isinstance(routing_rows, list):
            needs_harness_models = any(
                isinstance(row, dict)
                and isinstance(row.get("models"), list)
                and any(
                    isinstance(model, str)
                    and not model.startswith(AGENT_MODEL_PREFIX)
                    and ":" not in model
                    for model in row["models"]
                )
                for row in routing_rows
            )
        if needs_harness_models:
            model_entries, model_notes = _load_harness_models(REPO_ROOT)
        else:
            model_entries = {}

    routing_probe_count = 0
    if not isinstance(routing_rows, list):
        probes.append(
            Probe(
                "unresolved-model",
                "<routing>",
                f"{policy_name} routing",
                "routing must be a list of rows",
                is_routing=True,
            )
        )
        routing_probe_count = 1
    else:
        for row_number, row in enumerate(routing_rows, 1):
            if not isinstance(row, dict):
                probes.append(
                    Probe(
                        "unresolved-model",
                        f"<routing row {row_number}>",
                        f"{policy_name} routing[{row_number - 1}]",
                        "routing row must be a mapping with a models list",
                        is_routing=True,
                    )
                )
                routing_probe_count += 1
                continue
            models = row.get("models")
            if not isinstance(models, list):
                probes.append(
                    Probe(
                        "unresolved-model",
                        f"<routing row {row_number}>",
                        f"{policy_name} routing[{row_number - 1}].models",
                        "routing row models must be a list",
                        is_routing=True,
                    )
                )
                routing_probe_count += 1
                continue
            for model_index, raw_model in enumerate(models):
                routing_probe_count += 1
                where = (
                    f"{policy_name} routing[row={row_number}]"
                    f".models[{model_index}]"
                )
                probes.append(
                    _routing_probe(
                        raw_model,
                        where,
                        model_entries,
                        model_notes,
                    )
                )
    if routing_probe_count == 0:
        probes.append(
            Probe(
                "unresolved-model",
                "<routing>",
                f"{policy_name} routing",
                "routing contains no model names to validate",
                is_routing=True,
            )
        )

    # A model id pinned inside a backend's one-line `command:`. A hardcoded id
    # is the exact thing this script exists for, even when routing does not name
    # it. Reading it out of the command string rather than a dedicated field
    # keeps ONE copy of that id in the policy; a second field to scan would be a
    # second thing to keep in step.
    for backend in policy.get("backends") or []:
        command = backend.get("command")
        if not command:
            continue
        backend_id = str(backend.get("id"))
        for model in re.findall(r"(?:^|\s)-m\s+(\S+)", str(command)):
            probes.append(
                Probe(
                    f"{backend_id}-model",
                    model,
                    f"{policy_name} backends[id={backend_id}].command",
                )
            )

    for key, where in sorted(collect_config_keys(policy_path).items()):
        probes.append(Probe("codex-config-key", key, where))
    return probes


def collect_config_keys(policy_path: Path = POLICY_PATH) -> dict[str, str]:
    """Every `-c <key>=` config key named in the policy or the flag catalog.

    Scanning the raw text rather than a parsed field is deliberate: these keys
    are quoted inside prose (`capabilities.effort`, the flag catalog's entry),
    which is exactly why their spelling has never been machine-checked.
    """
    pattern = re.compile(r"-c\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*=")
    found: dict[str, str] = {}
    for path in (policy_path, DISPATCH_DOC):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            key = match.group(1)
            line = text.count("\n", 0, match.start()) + 1
            found.setdefault(key, f"{_rel(path)}:{line}")
    return found


def _resolve(cmd: Sequence[str]) -> list[str]:
    """Resolve argv[0] to a real path, and wrap Windows batch shims.

    Both CLIs ship as `.CMD` shims on Windows (npm/scoop). CreateProcess
    cannot execute a `.cmd` directly, so a bare `subprocess.run(["codex", ...])`
    dies with WinError 2 -- indistinguishable, to a careless reader, from "the
    model id is wrong". Route those through `cmd /c`.
    """
    argv = list(cmd)
    resolved = shutil.which(argv[0])
    if resolved:
        argv[0] = resolved
        if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
            return ["cmd", "/c", *argv]
    return argv


def _run(cmd: Sequence[str], stdin: str, timeout: int, env_overrides: dict | None = None):
    env = os.environ.copy()
    # A nested `claude -p` refuses to launch while CLAUDECODE is set.
    env.pop("CLAUDECODE", None)
    if env_overrides:
        env.update(env_overrides)
    try:
        return subprocess.run(
            _resolve(cmd),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired:
        return None


def probe_claude_model(model: str) -> tuple[bool, str]:
    cmd = ["claude", "-p", "--no-session-persistence", "--model", model, PROMPT]
    result = _run(cmd, stdin="", timeout=MODEL_TIMEOUT)
    if result is None:
        return False, f"timed out after {MODEL_TIMEOUT}s"
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return True, output.splitlines()[-1][:120] if output else "(empty reply)"
    return False, output[:400] or f"exit {result.returncode}, no output"


def _codex_base() -> list[str]:
    return [
        "codex",
        "exec",
        "-s",
        "read-only",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--strict-config",
    ]


def probe_codex_model(model: str, effort: str | None) -> tuple[bool, str]:
    cmd = _codex_base()
    if effort:
        cmd += ["-c", f"model_reasoning_effort={effort}"]
    with tempfile.TemporaryDirectory() as tmp:
        reply_path = Path(tmp) / "last-message.txt"
        cmd += ["-o", str(reply_path), "-m", model, "-"]
        result = _run(cmd, stdin=PROMPT, timeout=MODEL_TIMEOUT)
        if result is None:
            return False, f"timed out after {MODEL_TIMEOUT}s"
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            reply = ""
            if reply_path.exists():
                reply = reply_path.read_text(encoding="utf-8", errors="replace").strip()
            return True, f"replied: {reply[:120]}" if reply else "(empty reply)"
    return False, _last_meaningful(output) or f"exit {result.returncode}"


def probe_opencode_model(model: str, effort: str | None) -> tuple[bool, str]:
    """Dispatch one trivial turn through an OpenCode harness entry."""
    cmd = [
        "opencode",
        "run",
        "--dir",
        str(REPO_ROOT),
        "-m",
        model,
    ]
    if effort:
        cmd += ["--variant", effort]
    # --auto is required for a non-interactive run. The probe prompt asks for
    # no tools, but the flag makes the command fail closed against a prompt.
    cmd.append("--auto")
    result = _run(cmd, stdin=PROMPT, timeout=MODEL_TIMEOUT)
    if result is None:
        return False, f"timed out after {MODEL_TIMEOUT}s"
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return True, output.splitlines()[-1][:120] if output else "(empty reply)"
    return False, _last_meaningful(output) or f"exit {result.returncode}"


def probe_codex_config_key(key: str) -> tuple[bool, str]:
    """Validate a `-c` key with ZERO token cost.

    An unknown key is rejected while config.toml is being loaded, which happens
    before the prompt is read. So an EMPTY prompt separates the two outcomes: a
    known key gets as far as "No prompt provided via stdin", an unknown one
    never does.
    """
    value = CONFIG_PROBE_VALUES.get(key, DEFAULT_CONFIG_PROBE_VALUE)
    cmd = _codex_base() + ["-c", f"{key}={value}", "-"]
    result = _run(cmd, stdin="", timeout=CONFIG_TIMEOUT)
    if result is None:
        return False, f"timed out after {CONFIG_TIMEOUT}s"
    output = (result.stdout + result.stderr).strip()
    if "unknown configuration field" in output:
        return False, _last_meaningful(output)
    if "No prompt provided" in output:
        return True, "accepted by --strict-config (no model call)"
    if result.returncode == 0:
        return True, "accepted by --strict-config"
    return False, _last_meaningful(output) or f"exit {result.returncode}"


def _last_meaningful(output: str) -> str:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line and not line.startswith("-----"):
            return line[:400]
    return ""


def backend_available(name: str) -> bool:
    return shutil.which(name) is not None


def _filter_probes(probes: Sequence[Probe], backend: str) -> list[Probe]:
    """Filter by dispatch harness while retaining unknown routing names."""
    if backend == "all":
        return list(probes)
    return [
        probe
        for probe in probes
        if probe.backend == backend or probe.kind == "unresolved-model"
    ]


def _print_unresolved(probes: Sequence[Probe]) -> None:
    if not probes:
        return
    print("COULD NOT VALIDATE:")
    print()
    for probe in probes:
        print(f"  routing name `{probe.value}` could not be resolved")
        print(f"    declared at: {probe.where}")
        print(f"    reason: {probe.extra}")
        print()


def _print_unrun(probes: Sequence[Probe], binaries: Mapping[str, str]) -> None:
    if not probes:
        return
    print("IDENTIFIERS NOT RUN:")
    print()
    for probe in probes:
        backend = probe.backend or "unknown"
        binary = binaries.get(backend, backend)
        print(f"  {probe.kind} `{probe.value}` could not be validated")
        print(f"    declared at: {probe.where}")
        print(f"    reason: `{binary}` is not on PATH")
        print()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_model_dispatch.py",
        description=(
            "Validate every model identifier and -c config key in the "
            "orchestrate policy by really dispatching to it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Costs one trivial model call per model id; config keys cost "
            "nothing. Exit 1 = a dispatch rejected an identifier; exit 2 = "
            "an identifier could not be validated."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=["all", "claude", "codex", "opencode"],
        default="all",
        help=(
            "restrict probes to a dispatch harness (agent: -> claude; "
            "routing entries may name codex or opencode; default: all)"
        ),
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=POLICY_PATH,
        help=(
            "policy file to read (default: the shipped defaults). Point it at "
            "a mutated copy to confirm this check still fails loudly."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list what would be probed and exit; dispatches nothing",
    )
    args = parser.parse_args(argv)

    policy_path = args.policy.resolve()
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    probes = collect_probes(policy, policy_path)
    probes = _filter_probes(probes, args.backend)
    unresolved = [p for p in probes if p.kind == "unresolved-model"]
    dispatchable = [p for p in probes if p.kind != "unresolved-model"]

    print(f"Source of truth: {_rel(policy_path)}")
    print(
        f"{len(probes)} identifier(s) selected: "
        f"{len(dispatchable)} dispatchable, {len(unresolved)} unresolved.\n"
    )

    if args.list:
        for probe in probes:
            print(f"  {probe}")
        print()
        if unresolved:
            print(
                f"LIST INCOMPLETE: {len(unresolved)} routing identifier(s) "
                "could not be resolved; no dispatch performed."
            )
            return 2
        if not dispatchable:
            print("NO VALIDATION: no identifiers selected; no dispatch performed.")
            return 2
        print(
            f"LIST: {len(dispatchable)} identifier(s) would be dispatched; "
            "no dispatch performed."
        )
        return 0

    if not probes:
        print(
            f"NO VALIDATION: no identifiers selected for backend `{args.backend}`."
        )
        return 2

    needed = {p.backend for p in dispatchable}
    needed.discard(None)
    blocked: list[str] = []
    binaries = {
        "claude": "claude",
        "codex": "codex",
        "opencode": "opencode",
    }
    available = {
        backend: backend_available(binary)
        for backend, binary in binaries.items()
        if backend in needed
    }
    for backend, binary in binaries.items():
        if backend in needed and not available[backend]:
            blocked.append(
                f"{backend}: `{binary}` is not on PATH, so its identifiers "
                f"were NOT validated (this is not a pass)"
            )

    failures: list[tuple[Probe, str]] = []
    unrun: list[Probe] = []
    for probe in dispatchable:
        backend = probe.backend
        if not available.get(backend, False):
            unrun.append(probe)
            continue
        print(f"  probing {probe.kind} `{probe.value}` ... ", end="", flush=True)
        if probe.kind == "claude-model":
            ok, detail = probe_claude_model(probe.value)
        elif probe.kind == "codex-model":
            ok, detail = probe_codex_model(probe.value, probe.effort)
        elif probe.kind == "opencode-model":
            ok, detail = probe_opencode_model(probe.value, probe.effort)
        else:
            ok, detail = probe_codex_config_key(probe.value)
        print("OK" if ok else "FAILED")
        if ok:
            print(f"      {detail}")
        else:
            failures.append((probe, detail))

    print()
    _print_unresolved(unresolved)
    if failures:
        print("NOT DISPATCHABLE:\n")
        for probe, detail in failures:
            noun = "config key" if probe.kind.endswith("config-key") else "model id"
            print(f"  {noun} `{probe.value}` in {policy_path.name} is not dispatchable")
            print(f"    declared at: {probe.where}")
            if probe.extra:
                print(f"    probed with: {probe.extra}")
            print(f"    backend said: {detail}")
            print()

    if blocked:
        for line in blocked:
            print(f"COULD NOT CHECK -- {line}")
        print()
    _print_unrun(unrun, binaries)

    if failures:
        print(f"FAIL: {len(failures)} identifier(s) not dispatchable.")
        return 1
    if unresolved or unrun or not dispatchable:
        cannot_validate = len(unresolved) + len(unrun)
        validated = len(dispatchable) - len(unrun)
        if validated:
            print(
                f"INCOMPLETE: validated {validated} identifier(s); "
                f"could not validate {cannot_validate} identifier(s)."
            )
        elif not dispatchable and unresolved:
            print(
                f"NO VALIDATION: validated 0 identifier(s); no routing model "
                f"name resolved; could not validate {cannot_validate} "
                "identifier(s)."
            )
        else:
            print(
                f"NO VALIDATION: validated 0 identifier(s); could not validate "
                f"{cannot_validate} identifier(s)."
            )
        return 2
    print(f"PASS: validated {len(dispatchable)} identifier(s); all dispatched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
