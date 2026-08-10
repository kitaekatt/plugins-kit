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
Everything is derived from the shipped policy, so a rung added tomorrow is
probed tomorrow with no edit here:

  * every `ladders[].rungs[].model` on the Claude ladder, via
    `claude -p --model <id>` with a one-word prompt;
  * every `ladders[].rungs[].model` on the Codex ladder, via
    `codex exec -m <id>` with a one-word prompt, carrying that rung's
    `effort:` value as `-c model_reasoning_effort=<effort>`;
  * every `-c <key>=` config key named anywhere in orchestration.yaml or in
    references/codex-dispatch.md, via `codex exec --strict-config` with an
    EMPTY prompt -- an unknown key is rejected while loading config, before any
    model call, so key validation costs zero tokens.

Cost: one trivial dispatch per model id (5 on the shipped policy), plus one
zero-token launch per config key.

Usage
-----
    uv run python scripts/check_model_dispatch.py            # everything
    uv run python scripts/check_model_dispatch.py --backend codex
    uv run python scripts/check_model_dispatch.py --list     # no dispatch

Exit 0 = every identifier dispatched. Exit 1 = at least one did not, named
with its file and its key path. Exit 2 = the check could not run at all (a
backend CLI is missing, a login is absent), which is deliberately NOT reported
as a pass.

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
from typing import Any, Sequence

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATE = REPO_ROOT / "plugins" / "awesome-kit" / "skills" / "orchestrate"
POLICY_PATH = ORCHESTRATE / "defaults" / "orchestration.yaml"
DISPATCH_DOC = ORCHESTRATE / "references" / "codex-dispatch.md"

# The probe prompt. Deliberately the cheapest thing that still forces a real
# round trip to the named model.
PROMPT = "Reply with exactly: ok"

# Ladder id -> the backend that dispatches it. Ladder ids come from the policy
# and match the `backends[].id` records there.
CLAUDE_LADDER = "agent"
CODEX_LADDER = "codex"

# Config keys whose probe value is not derivable from the policy.
CONFIG_PROBE_VALUES = {
    "sandbox_workspace_write.network_access": "true",
}
DEFAULT_CONFIG_PROBE_VALUE = "low"

MODEL_TIMEOUT = 300
CONFIG_TIMEOUT = 60


class Probe:
    """One thing to validate, with the source location that names it."""

    def __init__(self, kind: str, value: str, where: str, extra: str = "") -> None:
        self.kind = kind  # "claude-model" | "codex-model" | "codex-config-key"
        self.value = value
        self.where = where  # file + key path, for the failure message
        self.extra = extra  # human note, e.g. the effort carried alongside

    def __str__(self) -> str:
        tail = f" ({self.extra})" if self.extra else ""
        return f"{self.kind} {self.value}{tail}\n    from {self.where}"


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:  # pragma: no cover - defensive
        return str(path)


def collect_probes(policy: dict[str, Any], policy_path: Path = POLICY_PATH) -> list[Probe]:
    probes: list[Probe] = []
    policy_name = _rel(policy_path)

    for ladder in policy.get("ladders") or []:
        ladder_id = ladder.get("id")
        for rung in ladder.get("rungs") or []:
            model = rung.get("model")
            if not model:
                continue
            where = (
                f"{policy_name} ladders[id={ladder_id}]"
                f".rungs[id={rung.get('id')}].model"
            )
            if ladder_id == CLAUDE_LADDER:
                probes.append(Probe("claude-model", str(model), where))
            elif ladder_id == CODEX_LADDER:
                effort = rung.get("effort")
                extra = f"effort={effort}" if effort else ""
                probes.append(Probe("codex-model", str(model), where, extra))
            else:
                print(
                    f"WARNING: ladder `{ladder_id}` has no known backend; "
                    f"its rungs are NOT dispatch-checked ({where})",
                    file=sys.stderr,
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
            "nothing. Exit 1 = an identifier is not dispatchable; exit 2 = "
            "the check could not run (missing CLI / not logged in)."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=["all", "claude", "codex"],
        default="all",
        help="restrict the probes to one backend (default: all)",
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
    if args.backend == "claude":
        probes = [p for p in probes if p.kind.startswith("claude")]
    elif args.backend == "codex":
        probes = [p for p in probes if p.kind.startswith("codex")]

    print(f"Source of truth: {_rel(policy_path)}")
    print(f"{len(probes)} identifier(s) to validate by real dispatch.\n")

    if args.list:
        for probe in probes:
            print(f"  {probe}")
        return 0

    needed = {p.kind.split("-")[0] for p in probes}
    blocked: list[str] = []
    for backend, binary in (("claude", "claude"), ("codex", "codex")):
        if backend in needed and not backend_available(binary):
            blocked.append(
                f"{backend}: `{binary}` is not on PATH, so its identifiers "
                f"were NOT validated (this is not a pass)"
            )

    failures: list[tuple[Probe, str]] = []
    unrun: list[Probe] = []
    for probe in probes:
        backend = probe.kind.split("-")[0]
        if not backend_available(backend):
            unrun.append(probe)
            continue
        print(f"  probing {probe.kind} `{probe.value}` ... ", end="", flush=True)
        if probe.kind == "claude-model":
            ok, detail = probe_claude_model(probe.value)
        elif probe.kind == "codex-model":
            effort = probe.extra.split("=", 1)[1] if probe.extra else None
            ok, detail = probe_codex_model(probe.value, effort)
        else:
            ok, detail = probe_codex_config_key(probe.value)
        print("OK" if ok else "FAILED")
        if ok:
            print(f"      {detail}")
        else:
            failures.append((probe, detail))

    print()
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

    if failures:
        print(f"FAIL: {len(failures)} identifier(s) not dispatchable.")
        return 1
    if unrun or blocked:
        checked = len(probes) - len(unrun)
        print(f"INCOMPLETE: {checked}/{len(probes)} checked; the rest could not run.")
        return 2
    print(f"PASS: all {len(probes)} identifier(s) dispatched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
