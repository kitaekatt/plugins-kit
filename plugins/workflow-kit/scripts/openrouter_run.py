#!/usr/bin/env python
"""workflow-kit openrouter node-strategy runner.

Make ONE non-Claude chat-completion call for a workflow node and write the
reply text to --out. The node's ``--model`` is a model DECLARATION: one or more
llm-scripting-kit TRANSPORT entry ids (e.g. ``or-qwen``), as a comma list. It
is dispatched through llm-scripting-kit's one declaration API,
``llm_scripting_kit.declaration.run``: the first usable entry runs, a classified
halt moves to the next usable entry, and an id this node cannot dispatch (a
harness entry, or one that resolves to nothing) is skipped silently. When no
usable entry is left, the typed floor (``NoUsableRoutingTarget``) is reported
with every declared id and its disposition.

With no ``--model`` the node runs the configured default declaration
(llm-scripting-kit's ``default_endpoint``); ``--cheap`` selects each entry's
``defaultCheap`` model. A model alias (``qwen``) or raw slug
(``qwen/qwen3-32b``) is not an entry id: it resolves to no entry and reaches
the floor like any other unresolved id.

Run this with WORKFLOW-KIT's OWN venv python, which bootstrap provisions with:
  - `llm_scripting_kit` and `bootstrap_lib` -- shared libraries linked onto this
    venv via the bootstrap shared-libs .pth because workflow-kit declares both
    in `shared_lib_imports`. llm-scripting-kit is REQUIRED (an `install: auto`
    plugin edge in bootstrap.json); an absent copy and a too-old one are
    diagnosed apart.
  - the `openai` SDK -- a declared workflow-kit dependency, used lazily inside
    llm-scripting-kit's OpenRouterBackend.

    <workflow-kit-venv-python> scripts/openrouter_run.py \\
        [--model <entry>[,<entry>...]] [--cheap] --prompt-file <req.txt> --out <OUT> \\
        [--system <s>] [--status <STATUS>]

Contract: writes the reply to --out and exits 0 on success. Exit 2 is a
resolution failure (the floor, an invalid declaration, a missing or too-old
shared library); exit 1 is a call that ran and failed. The optional --status
JSON names the entry that ran or, on failure, the error kind, the halt kind
when one was classified, and the floor's dispositions. The seam classifies
transport, HTTP and halt failures; this script reports them.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

#: The llm-scripting-kit release that shipped create_transport_backend, the
#: newest symbol this runner uses (with default_declaration and the
#: declaration module's run/NoUsableRoutingTarget).
_LLM_SCRIPTING_KIT_MIN = "0.46.0"


def _write_status(path, obj):
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def _split(value):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="workflow-kit: one non-Claude model call via llm-scripting-kit's declaration API."
    )
    ap.add_argument(
        "--model",
        default=None,
        help="A model declaration: one or more llm-scripting-kit transport entry ids, "
        "comma-separated (e.g. or-qwen,or-gpt-mini). If omitted, the configured default "
        "declaration is used.",
    )
    ap.add_argument(
        "--cheap",
        action="store_true",
        help="Select each entry's configured 'defaultCheap' model.",
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--prompt", help="user prompt text")
    g.add_argument("--prompt-file", help="path to a file holding the user prompt")
    ap.add_argument("--system", help="optional system message")
    ap.add_argument("--temperature", type=float)
    ap.add_argument("--max-tokens", type=int)
    ap.add_argument("--out", required=True, help="write the reply text here ($OUT)")
    ap.add_argument("--status", help="optional path for a small JSON status object ($STATUS)")
    args = ap.parse_args(argv)

    # The package and the newest symbol are probed separately: a .pth links no
    # version, so this venv can resolve an llm-scripting-kit predating the
    # declaration API. An absent package and a too-old one are different
    # repairs (enable the plugin vs. update it), so they get different messages.
    try:
        import llm_scripting_kit  # noqa: F401
    except ImportError:
        print(
            "llm_scripting_kit not importable. Enable the llm-scripting-kit plugin and run this "
            "with workflow-kit's venv python (bootstrap links llm_scripting_kit onto it via "
            "the shared-libs .pth).",
            file=sys.stderr,
        )
        return 2
    try:
        from llm_scripting_kit import default_declaration
        from llm_scripting_kit.completion import BackendOptions, create_transport_backend
        from llm_scripting_kit.declaration import (
            DeclarationSupportError,
            NoUsableRoutingTarget,
            RUN_COMPLETED,
            RunRequest,
            run,
        )
    except ImportError:
        print(
            "the linked llm_scripting_kit predates the model-declaration API "
            "(create_transport_backend). This requires llm-scripting-kit >= "
            f"{_LLM_SCRIPTING_KIT_MIN}. Run `claude plugin update llm-scripting-kit@plugins-kit` "
            "and restart so bootstrap re-links the newer shared lib onto workflow-kit's venv.",
            file=sys.stderr,
        )
        return 2

    project_root = os.getcwd()
    names = _split(args.model)
    try:
        if not names:
            names = default_declaration(project_root=project_root)
    except (DeclarationSupportError, ValueError) as exc:  # DeclarationError is a ValueError
        print(str(exc), file=sys.stderr)
        _write_status(args.status, {"ok": False, "error": type(exc).__name__})
        return 2

    prompt = args.prompt if args.prompt is not None else Path(args.prompt_file).read_text(encoding="utf-8")
    backend_kwargs = {}
    if args.temperature is not None:
        backend_kwargs["temperature"] = args.temperature
    if args.max_tokens is not None:
        backend_kwargs["max_tokens"] = args.max_tokens
    request = RunRequest(system=args.system or "", prompt=prompt, options=BackendOptions(**backend_kwargs))

    def factory(name, project_root=None):
        return create_transport_backend(name, cheap=args.cheap, project_root=project_root)

    try:
        result = run(
            names, request, project_root=project_root, backend_factory=factory,
            max_attempts=len(names),
        )
    except NoUsableRoutingTarget as exc:
        print(str(exc), file=sys.stderr)
        _write_status(args.status, {
            "ok": False,
            "error": "NoUsableRoutingTarget",
            "dispositions": exc.to_json()["dispositions"],
        })
        return 2
    except (DeclarationSupportError, ValueError) as exc:  # structurally invalid declaration
        print(str(exc), file=sys.stderr)
        _write_status(args.status, {"ok": False, "error": type(exc).__name__})
        return 2

    if result.status != RUN_COMPLETED:
        print(f"openrouter node failed on {result.entry}: {result.detail}", file=sys.stderr)
        status = {
            "ok": False,
            "error": result.status,
            "entry": result.entry,
            "detail": result.detail,
            "attempts": [attempt.to_json() for attempt in result.attempts],
        }
        halt = result.attempts[-1].halt if result.attempts else None
        if halt:
            status["halt"] = halt
        _write_status(args.status, status)
        return 1

    text = result.response.text
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    _write_status(args.status, {
        "ok": True,
        "entry": result.entry,
        "model": result.response.model,
        "bytes": len(text.encode("utf-8")),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
