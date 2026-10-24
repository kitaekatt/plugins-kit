"""Private supervisor/child entry point; no manager or task policy.

The child records itself and waits for the owner's durable go record BEFORE
executing the caller's argv. This closes the spawn-to-identity crash window.
"""

from __future__ import annotations

from pathlib import Path
import sys

# Running this exact file keeps the launching interpreter and package source
# together, including in a source-tree test. No PATH or site installation changes.
if __name__ == "__main__":
    # Replace the script directory: jobs/select.py must not shadow the native
    # stdlib select module when cold imports reach socket/selectors on Windows.
    sys.path[0] = str(Path(__file__).resolve().parents[2])

import argparse
import os
import subprocess
import time
from typing import Any

from llm_kit.jobs import _supervisor_process as processes
from llm_kit.jobs.supervisor import (
    JobProcessSpec, ProcessHandle, SupervisorError, _load, _lock, _record, _settle,
    _spawn_options, _state_reads_until, _tree, _verify_lease, _write,
)


HANDSHAKE_S = 10.0
POLL_S = 0.02


def _child(handle: ProcessHandle, spec: JobProcessSpec, *, finalizer: bool = False) -> int:
    runner = _record(handle, "finalizer-owner.json" if finalizer else "runner.json")
    if runner is None:
        raise SupervisorError("Child has no supervisor identity", handle=handle)
    owner = processes.ProcessIdentity.from_dict(runner["process"])
    me = processes.identity(os.getpid())
    if me is None:
        raise SupervisorError("Cannot establish child identity", handle=handle)
    _write(handle.run_dir / ("finalizer.json" if finalizer else "child.json"),
           {"nonce": handle.nonce, "process": me.to_dict()})
    deadline = time.monotonic() + HANDSHAKE_S
    with _state_reads_until(deadline):
        while True:
            go = _record(handle, "finalizer-go.json" if finalizer else "go.json")
            if go is not None and (not finalizer or go.get("process") == me.to_dict()):
                break
            if processes.live(owner) is None or (not finalizer and _record(handle, "cancel.json") is not None):
                return 75
            if time.monotonic() >= deadline:
                return 76
            time.sleep(POLL_S)
    if processes.live(owner) is None or (not finalizer and _record(handle, "cancel.json") is not None):
        return 75
    _verify_lease(handle, spec)
    env = {**os.environ, **spec.env}
    command = spec.cancel_argv if finalizer else spec.argv
    os.chdir(spec.cwd)
    if os.name == "nt":
        # Windows exec creates a successor rather than replacing this process.
        # Retain the recorded ancestor until that successor has exited.
        # CREATE_NO_WINDOW does not preserve these redirected handles unless
        # they are supplied explicitly to the successor's startup information.
        return subprocess.call(command, env=env, stdin=subprocess.DEVNULL,
                               stdout=sys.stdout, stderr=sys.stderr,
                               **_spawn_options(detached=False))
    os.execvpe(command[0], command, env)
    raise AssertionError("execvpe returned")


def _run(handle: ProcessHandle, spec: JobProcessSpec) -> int:
    with _lock(handle.run_dir / "owner.lock", blocking=False):
        _verify_lease(handle, spec)
        # Keep the Windows kernel handle alive until the owner exits. No handle
        # inheritance is requested, so an owner crash closes the last handle.
        windows_job = processes.assign_windows_job()
        me = processes.identity(os.getpid())
        if me is None:
            raise SupervisorError("Cannot establish supervisor identity", handle=handle)
        _write(handle.run_dir / "runner.json", {"nonce": handle.nonce, "process": me.to_dict()})
        if _record(handle, "cancel.json") is not None:
            _settle(handle, spec, state="cancelled", reason="Cancelled before dispatch")
            return 0
        started = time.monotonic()
        with (handle.run_dir / "stdout.log").open("ab") as out, (handle.run_dir / "stderr.log").open("ab") as err:
            worker = Path(__file__).resolve()
            child = subprocess.Popen([sys.executable, str(worker), "child", str(handle.run_dir), handle.nonce],
                                     cwd=spec.cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                     **_spawn_options(detached=True))
            handshake_deadline = time.monotonic() + HANDSHAKE_S
            child_ident = None
            with _state_reads_until(handshake_deadline):
                while time.monotonic() < handshake_deadline:
                    record = _record(handle, "child.json")
                    if record is not None:
                        candidate = processes.ProcessIdentity.from_dict(record["process"])
                        # Windows venv Python can have a launcher in front of the
                        # interpreter. The recorded child must be it or its descendant.
                        launched = processes.identity(child.pid)
                        related = launched is not None and (
                            candidate == launched or candidate in processes.descendants(launched))
                        if related and processes.live(candidate) is not None:
                            child_ident = candidate
                            break
                    if child.poll() is not None:
                        break
                    time.sleep(POLL_S)
            if child_ident is None:
                # The launched shim has not been authorized to execute argv.
                launched = processes.identity(child.pid)
                if launched is not None:
                    _write(handle.run_dir / "tree.json", {"nonce": handle.nonce, "members": [launched.to_dict()]})
                _settle(handle, spec, state="refused", reason="Child did not acknowledge the dispatch handshake")
                child.wait(timeout=spec.grace_s)
                return 2
            seen: dict[tuple[int, float], processes.ProcessIdentity] = {
                (child_ident.pid, child_ident.create_time): child_ident}
            _write(handle.run_dir / "tree.json", {"nonce": handle.nonce, "members": [child_ident.to_dict()]})
            if _record(handle, "cancel.json") is None:
                _write(handle.run_dir / "go.json", {"nonce": handle.nonce})
            state, detail = "finished", "Command exited; inspect its result and acceptance separately"
            while child.poll() is None:
                found = processes.reachable(child_ident, list(seen.values()))
                fresh = {(p.pid, p.create_time): p for p in found}
                if any(key not in seen for key in fresh):
                    seen.update(fresh)
                    _write(handle.run_dir / "tree.json", {"nonce": handle.nonce,
                                                          "members": [p.to_dict() for p in seen.values()]})
                with _state_reads_until(started + spec.timeout_s):
                    request = _record(handle, "cancel.json")
                if request is not None:
                    state, detail = "cancelled", request["reason"]
                    break
                if time.monotonic() - started >= spec.timeout_s:
                    state, detail = "timeout", "Run-wide deadline exceeded"
                    break
                time.sleep(POLL_S)
            _settle(handle, spec, state=state, reason=detail, returncode=child.poll())
            if child.poll() is None:
                child.wait(timeout=spec.grace_s)
        del windows_job
        return 0


def _recover(handle: ProcessHandle, spec: JobProcessSpec) -> int:
    with _lock(handle.run_dir / "owner.lock", blocking=False):
        _verify_lease(handle, spec)
        windows_job = processes.assign_windows_job()
        me = processes.identity(os.getpid())
        if me is None:
            raise SupervisorError("Cannot establish recovery supervisor identity", handle=handle)
        _write(handle.run_dir / "runner.json", {"nonce": handle.nonce, "process": me.to_dict()})
        _settle(handle, spec, state="cancelled", reason="Supervisor lost; prior work may be partial")
        del windows_job
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "child", "finalize", "recover"))
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("nonce")
    args = parser.parse_args(argv)
    handle, spec = _load(ProcessHandle(args.run_dir, args.nonce))
    if args.mode in {"child", "finalize"}:
        return _child(handle, spec, finalizer=args.mode == "finalize")
    try:
        return _recover(handle, spec) if args.mode == "recover" else _run(handle, spec)
    except BlockingIOError:
        # A second copy may inspect but must never settle another owner's run.
        print("Supervisor owner lock is already held", file=sys.stderr)
        return 2
    except Exception as exc:
        # Preserve the lease and all identities for adoption. An exception is
        # NOT evidence that the command stopped; do not record a clean refusal.
        previous = _record(handle, "result.json")
        if previous is None or previous.get("state") == "indeterminate":
            _write(handle.run_dir / "result.json", {"nonce": handle.nonce, "state": "indeterminate",
                                                   "returncode": None, "detail": f"Supervisor failed: {type(exc).__name__}: {exc}",
                                                   "survivors": []})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
