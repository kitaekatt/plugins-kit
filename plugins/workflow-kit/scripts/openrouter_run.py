#!/usr/bin/env python
"""workflow-kit openrouter node-strategy runner.

Make ONE OpenRouter chat-completion call through llm-scripting-kit's completion
seam (`llm_scripting_kit.completion.OpenRouterBackend`) and write the reply
text to --out.

Run this with WORKFLOW-KIT's OWN venv python, which bootstrap provisions with:
  - `llm_scripting_kit` -- owned by the llm-scripting-kit plugin, published as a shared
    library and linked onto this venv via the bootstrap shared-libs .pth because
    workflow-kit declares `shared_lib_imports: ["llm_scripting_kit"]`. No path
    discovery -- just import.
  - the `openai` SDK -- a declared workflow-kit dependency (pyproject.toml +
    venv.check_imports), installed into this venv by bootstrap. The seam's
    OpenRouterBackend uses it internally (lazily, via make_openai_client); this
    script never touches the SDK or the client directly.

Run it with workflow-kit's venv python (which has both):

    <workflow-kit-venv-python> scripts/openrouter_run.py \
        [--model <alias|slug>] [--cheap] --prompt-file <req.txt> --out <OUT> \
        [--system <s>] [--status <STATUS>]

Contract: writes the reply to --out; exits 0 on success, non-zero on failure
(the workflow-kit-agent reports the exit code). Optionally writes a small JSON
status object to --status for routing. The seam classifies the transport, HTTP,
and halt-taxonomy failures (auth / rate limit / insufficient credit); this
script does not reimplement any of that classification, it only reports it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def build_messages(prompt, system=None):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


def _write_status(path, obj):
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="workflow-kit: one OpenRouter call via llm-scripting-kit's completion seam."
    )
    ap.add_argument(
        "--model",
        default=None,
        help="Model: a registry alias (e.g. qwen) or a raw OpenRouter slug "
        "(e.g. qwen/qwen3-32b). If omitted, llm-scripting-kit's configured 'default' "
        "(or 'defaultCheap' with --cheap) is used.",
    )
    ap.add_argument(
        "--cheap",
        action="store_true",
        help="When no --model is given, select the configured 'defaultCheap' model.",
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

    # llm_scripting_kit is linked onto workflow-kit's venv by the bootstrap shared-libs
    # .pth (workflow-kit declares shared_lib_imports). No path discovery -- just
    # import. It owns model resolution (alias / slug / default / defaultCheap) and
    # the completion seam (transport, response normalization, halt classification);
    # workflow-kit does not reimplement any of it.
    try:
        from llm_scripting_kit import ModelResolveError, resolve_model
        from llm_scripting_kit.completion import BackendOptions, OpenRouterBackend
    except ImportError:
        print(
            "llm_scripting_kit not importable. Enable the llm-scripting-kit plugin and run this "
            "with workflow-kit's venv python (bootstrap links llm_scripting_kit onto it via "
            "the shared-libs .pth).",
            file=sys.stderr,
        )
        return 2
    try:
        args.model = resolve_model(args.model, cheap=args.cheap, project_root=os.getcwd())
    except ModelResolveError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    prompt = args.prompt if args.prompt is not None else Path(args.prompt_file).read_text(encoding="utf-8")

    backend_kwargs = {}
    if args.temperature is not None:
        backend_kwargs["temperature"] = args.temperature
    if args.max_tokens is not None:
        backend_kwargs["max_tokens"] = args.max_tokens
    options = BackendOptions(**backend_kwargs)

    backend = OpenRouterBackend(project_root=Path(os.getcwd()))

    try:
        resp = backend.complete(args.system or "", prompt, model=args.model, options=options)
    except ImportError as e:
        print("openai SDK not available: " + str(e), file=sys.stderr)
        print(
            "Install it into the runner's Python, e.g. `uv pip install --python <python> openai`.",
            file=sys.stderr,
        )
        return 2
    except RuntimeError as e:  # no API key resolved, or an empty completion (seam-classified)
        print(str(e), file=sys.stderr)
        _write_status(args.status, {"ok": False, "error": type(e).__name__})
        return 1
    except Exception as e:  # noqa: BLE001 -- any other transport/API error is a node failure
        halt_kind = backend.classify_halt(e)
        print("OpenRouter call failed: " + str(e), file=sys.stderr)
        status = {"ok": False, "error": type(e).__name__}
        if halt_kind:
            status["halt"] = halt_kind
        _write_status(args.status, status)
        return 1

    text = resp.text

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    _write_status(args.status, {"ok": True, "model": resp.model, "bytes": len(text.encode("utf-8"))})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
