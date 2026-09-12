#!/usr/bin/env python3
"""Terminal presentation and locking for the user/project bootstrap pass."""

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bootstrap_lib.layered_bootstrap import run_layered_bootstrap
from bootstrap_lib.platform_detect import detect_os, UnsupportedPlatformError
from bootstrap_lib.proc_lock import engine_lock
from bootstrap_lib.records import PassRecorder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run user/project bootstrap manifests only")
    parser.add_argument("--plugin-root", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--console", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    with engine_lock(str(args.data_dir)) as acquired:
        if not acquired:
            print("bootstrap run: another pass is already running; retry after it finishes.",
                  file=sys.stderr)
            return 2
        try:
            current_os = detect_os()
        except UnsupportedPlatformError as error:
            print(f"bootstrap run: {error}", file=sys.stderr)
            return 1
        recorder = PassRecorder(str(args.data_dir), mode="console")
        try:
            print(f"Running user/project bootstrap for {args.project_dir}.", flush=True)
            home = Path(os.environ.get("HOME") or Path.home())
            for directory in (home / ".claude", args.project_dir / ".claude"):
                for filename in ("bootstrap.json", "bootstrap.local.json"):
                    path = directory / filename
                    status = "present" if path.is_file() else "absent"
                    print(f"  {path}: {status}", flush=True)
                    recorder.record("scope", str(path), status=status,
                                    project_dir=str(args.project_dir))
            result = run_layered_bootstrap(
                args.project_dir, args.plugin_root, args.data_dir, current_os, recorder,
            )
            for failure in result.failures:
                recorder.record("failure", failure.get("message", "failed"),
                                sev="fail", failure=failure)
            verdict = (f"User/project bootstrap: {len(result.failures)} failure(s)."
                       if result.failures else "User/project bootstrap completed.")
            # The CLI streams the check records; the child prints the verdict.
            print(verdict, flush=True)
            recorder.record_emit("console", {"systemMessage": verdict})
            return 1 if result.failures else 0
        except Exception as error:
            print(f"bootstrap run: {type(error).__name__}: {error}", file=sys.stderr)
            recorder.record("failure", str(error), sev="fail")
            return 1
        finally:
            recorder.flush()


if __name__ == "__main__":
    sys.exit(main())
