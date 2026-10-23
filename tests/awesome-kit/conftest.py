"""Shared sys.path setup for the awesome-kit test suite.

Mirrors the tests/skills-kit / tests/workflow-kit pattern: put the plugin code
directories on sys.path so tests import plugin modules directly when run under
the repo-root pytest. plugins/skills-kit is also on pyproject pythonpath; it
is added here too so this suite stays self-contained.
"""

import os
import sys
from pathlib import Path

# Scripts that re-exec into the plugin venv (orchestration_guidance.py)
# call os.execv at import time, which abandons the pytest process itself and
# yields a false green. This env var makes the re-exec a no-op, matching how the
# real script behaves once the guard has already fired. See plugins/CLAUDE.md.
os.environ.setdefault("_BOOTSTRAP_GUARD_SKIP_REEXEC", "1")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYS_PATHS = (
    _REPO_ROOT / "plugins" / "skills-kit",  # skills_kit_lib
    # orchestration_guidance + its vendored bootstrap_guard
    _REPO_ROOT / "plugins" / "awesome-kit" / "skills" / "orchestrate" / "scripts",
)
for _p in _SYS_PATHS:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Orchestrate's routing render consults llm-kit's live subscription
# quota, so without this every routing assertion turns on the DEVELOPER's
# current balance -- a test that passes or fails by how much Fable was spent
# this week. Off by default here; the quota tests re-enable it deliberately.
os.environ.setdefault("ORCHESTRATE_QUOTA_ROUTING", "0")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_consent_and_auth(monkeypatch, tmp_path):
    """Keep every render independent of the developer's consent file, logins and environment.

    Orchestrate applies llm-kit's consent scope `orchestrate`, classifies codex
    entries by the codex CLI's authentication, and classifies native Claude
    entries from the session environment. Without this, a render would turn on
    the developer's own ~/.config/llm-kit/consent.json, on a live `codex doctor`
    and on whatever billing variables the developer's shell exports. The consent
    file points at an absent path (the library's default policy: local and
    subscription on, metered off), the session environment is empty and the codex
    probe reports subscription auth; tests that need another state use the
    `consent_file`, `harness_auth` and `session_env` fixtures.
    """
    from types import SimpleNamespace

    import orchestration_guidance as og

    consent_path = tmp_path / "consent" / "consent.json"
    monkeypatch.setenv("LLM_KIT_CONSENT_FILE", str(consent_path))
    states = {"codex": "subscription"}
    env: dict = {}

    def probe(_api, harnesses, _cwd):
        return {
            name: SimpleNamespace(state=states[name], reason=f"stubbed {states[name]} auth")
            for name in harnesses
        }

    monkeypatch.setattr(og, "probe_session_auth", probe)
    monkeypatch.setattr(og, "session_environment", lambda: env)
    return SimpleNamespace(consent_path=consent_path, states=states, env=env)


@pytest.fixture
def consent_file(_isolated_consent_and_auth):
    """Write the consent file the render reads: `consent_file({"metered": True, "scopes": {...}})`."""
    import json

    path = _isolated_consent_and_auth.consent_path

    def write(content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 2, **content}), encoding="utf-8")
        return path

    write.path = path
    return write


@pytest.fixture
def harness_auth(_isolated_consent_and_auth):
    """Set the codex probe's stubbed authentication state: `harness_auth("codex", "metered")`."""
    states = _isolated_consent_and_auth.states

    def set_state(harness, state):
        assert harness == "codex", "native Claude entries are classified from the session environment"
        states[harness] = state

    return set_state


@pytest.fixture
def session_env(_isolated_consent_and_auth):
    """The session environment the renderer classifies native Claude entries from."""
    return _isolated_consent_and_auth.env
