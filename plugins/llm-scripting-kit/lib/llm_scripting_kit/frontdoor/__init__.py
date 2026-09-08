"""OpenAI-compatible fill-first front door for configured transport entries.

The optional web dependencies are imported only when :func:`create_app` or
the command-line entry point is used. Importing ``llm_scripting_kit`` itself
therefore remains independent of FastAPI, uvicorn, and httpx.
"""

from .server import create_app, main

__all__ = ["create_app", "main"]
