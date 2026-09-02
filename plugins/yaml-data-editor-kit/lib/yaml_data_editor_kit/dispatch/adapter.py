"""Derive the worker-facing adapter from a durable dispatch plan."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml

from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.pipeline.workunit import WorkUnit

from .state import DispatchPlan
from .units import (
    SYSTEM_PROMPT,
    prompt_for_payload,
    system_prompt_for_unit,
    validation_spec_for_unit,
)


def prompt_for(payload: Mapping[str, Any]) -> str:
    """Build the canonical user prompt for a saved unit payload."""
    return prompt_for_payload(payload)


def adapter_for(plan: DispatchPlan) -> RunAdapter:
    """Create an adapter whose unit identity and prompts come from ``plan``."""
    def unit_for(unit_id: str) -> WorkUnit:
        saved = plan.unit_for(unit_id)
        payload = saved.get("payload", saved)
        return WorkUnit(id=unit_id, payload=payload)

    return RunAdapter(
        unit_for=unit_for,
        system_for=system_prompt_for_unit,
        user_for=lambda unit: prompt_for(unit.payload),
        validation_spec_for=validation_spec_for_unit,
        adapter_version=plan.adapter_version,
    )


__all__ = ["SYSTEM_PROMPT", "adapter_for", "prompt_for"]
