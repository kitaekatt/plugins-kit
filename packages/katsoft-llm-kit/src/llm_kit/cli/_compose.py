"""Composition-root helpers: the one place the CLI names concrete construction.

The CLI is the composition root. It resolves credentials through
``llm_kit.registry`` and injects them into ``llm_kit.adapters.account`` (see
:mod:`llm_kit.cli.account`), and it injects one
:class:`~llm_kit.dispatch.pipeline_backends.ConfiguredBackendFactory` into the
jobs runner (:mod:`llm_kit.cli.jobs`). The pipeline group builds no backend
itself: its ``create-run --models`` resolves a declaration through the pipeline
runtime and its commands run against a store.
"""
from __future__ import annotations

from typing import Any, Optional

from llm_kit.dispatch import factory as _factory
from llm_kit.dispatch.pipeline_backends import ConfiguredBackendFactory


def create_backend(endpoint: Optional[str] = None, **kwargs: Any) -> Any:
    """``dispatch.factory.create_backend``, looked up at call time.

    Every scripting verb reaches the endpoint-to-backend selection through this
    name, so one seam (``llm_kit.dispatch.factory.create_backend``) redirects
    them all.
    """
    return _factory.create_backend(endpoint, **kwargs)


def configured_factory() -> ConfiguredBackendFactory:
    """The backend factory the CLI injects into the jobs runner."""
    return ConfiguredBackendFactory()
