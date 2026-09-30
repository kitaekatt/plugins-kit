#!/usr/bin/env python
"""CLI: validate and compile a .workflow.yaml into a native Workflow tool script.

Usage:
    compile_workflow.py <file.workflow.yaml>                 # compile, print JS to stdout
    compile_workflow.py <file.workflow.yaml> -o out.js       # compile, write to out.js
    compile_workflow.py <file.workflow.yaml> --print         # compile, print to stdout
    compile_workflow.py <file.workflow.yaml> --validate-only  # validate only, no output

Exit codes: 0 on success, 1 on any workflow error (message goes to stderr).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a bare script (skill invokes it by path): make the package importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workflow_kit_lib import WorkflowError, compile_doc, load_workflow  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compile a .workflow.yaml to a Workflow tool script.")
    parser.add_argument("workflow", help="path to the .workflow.yaml file")
    parser.add_argument("-o", "--out", help="write the compiled JS to this path")
    parser.add_argument("--print", dest="to_stdout", action="store_true",
                        help="print the compiled JS to stdout (default when no -o)")
    parser.add_argument("--validate-only", action="store_true",
                        help="validate the workflow and exit without compiling")
    args = parser.parse_args(argv)

    try:
        doc = load_workflow(args.workflow)
        if args.validate_only:
            print(f"OK: {doc.name} ({len(doc.steps)} step(s))")
            return 0
        js = compile_doc(doc)
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(js, encoding="utf-8", newline="\n")
        print(str(out))
    else:
        # Bytes, not text mode: Windows text-mode stdout turns "\n" into "\r\n".
        sys.stdout.flush()
        sys.stdout.buffer.write(js.encode("utf-8"))
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
