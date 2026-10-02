"""Tests pinning the SHIPPED review-profile defaults.

Resolution behaviour across layers lives in test_review_profiles.py.
"""

from __future__ import annotations

from pathlib import Path

from bootstrap_lib.code_review import review_profiles as rp


def test_every_shipped_entry_states_effort() -> None:
    data = rp.load_layer(rp.DEFAULTS_PATH)
    assert data
    assert rp.completeness_findings(data, rp.DEFAULTS_PATH) == []


def test_shipped_defaults_resolve_cleanly(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config, _provenance = rp.resolve_config(tmp_path / "project", home=home)
    resolved = rp.apply_model_priority(config)
    assert resolved["profiles"]
