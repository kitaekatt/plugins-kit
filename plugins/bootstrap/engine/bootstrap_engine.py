#!/usr/bin/env python3
"""Bootstrap engine — thin wrapper that delegates to bootstrap_lib.engine.

This file exists for backward compatibility with callers that invoke
the engine directly via `python3 engine/bootstrap_engine.py`.
"""

import sys
import os
import json
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _argument_value(name):
    prefix = name + "="
    for index, argument in enumerate(sys.argv[1:]):
        if argument == name and index + 2 <= len(sys.argv) - 1:
            return sys.argv[index + 2]
        if argument.startswith(prefix):
            return argument[len(prefix):]
    return ""


def _write_pending(path, content):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bootstrap-pending.", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass


try:
    from bootstrap_lib.engine import main
except ImportError as exc:
    data_dir = _argument_value("--data-dir")
    note = "bootstrap wrapper import failed: " + str(exc)
    if data_dir:
        try:
            os.makedirs(data_dir, exist_ok=True)
            with open(os.path.join(data_dir, "bootstrap.log"), "a") as stream:
                stream.write(note + "\n")
            response = {
                "continue": True,
                "suppressOutput": False,
                "systemMessage": note,
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": note,
                },
            }
            _write_pending(
                os.path.join(data_dir, "bootstrap_display.pending"),
                json.dumps(response),
            )
        except OSError:
            pass
    sys.stderr.write(note + "\n")
    raise SystemExit(1)

if __name__ == "__main__":
    main()
