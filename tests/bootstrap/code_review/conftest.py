"""Keep code-review tests independent of the user's configured checks."""

from pathlib import Path

import pytest

from bootstrap_lib.code_review import mechanical_config


@pytest.fixture(autouse=True)
def mechanical_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "review-home"
    monkeypatch.setattr(
        mechanical_config, "_home_path",
        lambda override: home if override is None else Path(override).expanduser(),
    )
    return home


@pytest.fixture
def personal_conventions(mechanical_config_home: Path) -> Path:
    """Opt in explicitly where tests need personal-convention findings."""
    path = mechanical_config_home / ".claude/config/mechanical_builtin_checks.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("checks: [non_ascii, abs_path]\n", encoding="utf-8")
    return path
