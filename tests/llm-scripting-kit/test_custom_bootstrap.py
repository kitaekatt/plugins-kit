"""Tests for plugins/llm-scripting-kit/custom_bootstrap.py.

Exercises the four bootstrap states. None of them escalates: an OpenRouter key
is a precondition only some sessions need, so every unmet case is recorded as a
DEFERRED REQUIREMENT for the point-of-need skill to act on, and ctx.failures
stays empty throughout.
- key missing entirely  -> add_deferred_requirement with set-key instructions
- key present + valid   -> log success, write content-hash cache
- key present + 401     -> add_deferred_requirement with rotation hint
- key present + 402     -> add_deferred_requirement with credit hint

And the autodetect path: legacy loc-ops .env present -> migrated. Plus the
0.5.0 user-data-dir rename (openrouter-kit -> llm-scripting-kit).
"""

from unittest.mock import patch

import pytest

import custom_bootstrap as cb
from llm_scripting_kit import constants
from llm_scripting_kit.account import AccountCheckError, AccountStatus
from llm_scripting_kit.env_file import read_env_file, write_env_file


class FakeContext:
    """Minimal stand-in for the bootstrap engine's _ScriptContext."""

    def __init__(self, data_dir, project_dir):
        self.data_dir = str(data_dir)
        self.project_dir = str(project_dir)
        self.failures = []
        self.deferred = []
        self.actions = []
        self.oks = []

    def add_failure(self, failure_type, **kwargs):
        self.failures.append({"type": failure_type, **kwargs})

    def add_deferred_requirement(self, name, **kwargs):
        self.deferred.append({"name": name, **kwargs})

    def log(self, msg):
        self.actions.append(msg)

    def log_ok(self, msg):
        self.oks.append(msg)


@pytest.fixture
def env_setup(monkeypatch, tmp_path):
    """Redirect USER_ENV_FILE to tmp_path and clear the env var."""
    user_env = tmp_path / "user_data" / ".env"
    monkeypatch.setattr(constants, "USER_ENV_FILE", user_env)
    monkeypatch.setattr("llm_scripting_kit.api_key.USER_ENV_FILE", user_env)
    monkeypatch.setattr(cb, "USER_ENV_FILE", user_env)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    data_dir = tmp_path / "data"
    project_dir = tmp_path / "project"
    data_dir.mkdir()
    project_dir.mkdir()
    return {"user_env": user_env, "data_dir": data_dir, "project_dir": project_dir, "tmp": tmp_path}


def _ok_status(label="test"):
    return AccountStatus(
        ok=True, label=label, usage=0.0, limit=None, is_free_tier=False,
        rate_limit=None, failure_reason=None, raw=None,
    )


def _fail_status(reason):
    return AccountStatus(
        ok=False, label=None, usage=None, limit=None, is_free_tier=None,
        rate_limit=None, failure_reason=reason, raw=None,
    )


class TestBootstrapMissingKey:
    def test_no_key_anywhere_defers_without_escalating(self, env_setup):
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])
        cb.bootstrap(ctx)
        # The load-bearing assertion: a missing key produces NO fix-all entry,
        # so a session that never calls the API is never prompted about it.
        assert ctx.failures == []
        assert len(ctx.deferred) == 1
        d = ctx.deferred[0]
        assert d["name"] == "openrouter_credential"
        assert d["satisfied_by"] == "llm-scripting-kit set-key"
        # The brief user_msg says there is nothing to do yet; the detailed
        # remediation (set-key + where to get a key) lives in the agent_msg,
        # for the point-of-need skill to present verbatim.
        assert "fix-all" not in d["user_msg"]
        assert "llm-scripting-kit set-key" in d["agent_msg"]
        assert "openrouter.ai/keys" in d["agent_msg"]

    def test_missing_key_is_not_announced_to_the_user(self, env_setup):
        """The expected state on a machine that never calls the API is quiet."""
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])
        cb.bootstrap(ctx)
        assert ctx.actions == []
        assert any("deferred" in m for m in ctx.oks)

    def test_old_engine_without_the_api_stays_silent(self, env_setup):
        """A bootstrap predating add_deferred_requirement must not fall back to
        add_failure -- that would resurrect the nag this change removes."""
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])
        ctx.add_deferred_requirement = None  # engine lacks the API
        cb.bootstrap(ctx)
        assert ctx.failures == []


def _write_cache_file(data_dir, key, epoch=None):
    """Write the validation cache: hash line + optional epoch line (legacy = hash only)."""
    import hashlib

    cache_file = data_dir / "last_validated.sha256"
    content = hashlib.sha256(key.encode("utf-8")).hexdigest() + "\n"
    if epoch is not None:
        content += f"{int(epoch)}\n"
    cache_file.write_text(content)
    return cache_file


class TestBootstrapValidationOk:
    def test_valid_key_logs_success_and_writes_cache(self, env_setup):
        write_env_file(env_setup["user_env"], {"OPENROUTER_API_KEY": "sk-or-v1-good"})
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account", return_value=_ok_status(label="my-key")):
            cb.bootstrap(ctx)

        assert ctx.failures == []
        assert any("my-key" in m for m in ctx.actions)
        cache_file = env_setup["data_dir"] / "last_validated.sha256"
        assert cache_file.is_file()
        # Hash (64-char hex) on line 1, validation epoch on line 2
        lines = cache_file.read_text().split()
        assert len(lines) == 2
        assert len(lines[0]) == 64
        assert lines[1].isdigit()

    def test_fresh_cached_key_skips_network_call(self, env_setup):
        import time
        write_env_file(env_setup["user_env"], {"OPENROUTER_API_KEY": "sk-or-v1-cached"})
        _write_cache_file(env_setup["data_dir"], "sk-or-v1-cached", epoch=time.time())
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account") as mock_check:
            cb.bootstrap(ctx)

        mock_check.assert_not_called()
        assert ctx.failures == []
        assert any("cached" in m for m in ctx.oks)


class TestValidationCacheExpiry:
    """W8: a hash-matching cache only skips /auth/key while fresh (~7 days)."""

    def test_stale_cache_revalidates_and_refreshes_timestamp(self, env_setup):
        import time
        write_env_file(env_setup["user_env"], {"OPENROUTER_API_KEY": "sk-or-v1-old"})
        stale_epoch = time.time() - (cb._REVALIDATE_AFTER_SECONDS + 60)
        cache_file = _write_cache_file(env_setup["data_dir"], "sk-or-v1-old", epoch=stale_epoch)
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account", return_value=_ok_status()) as mock_check:
            cb.bootstrap(ctx)

        mock_check.assert_called_once()
        assert ctx.failures == []
        # cache rewritten with a fresh timestamp
        new_epoch = int(cache_file.read_text().split()[1])
        assert new_epoch > stale_epoch + cb._REVALIDATE_AFTER_SECONDS - 120

    def test_stale_cache_redetects_revocation(self, env_setup):
        import time
        write_env_file(env_setup["user_env"], {"OPENROUTER_API_KEY": "sk-or-v1-revoked"})
        stale_epoch = time.time() - (cb._REVALIDATE_AFTER_SECONDS + 60)
        _write_cache_file(env_setup["data_dir"], "sk-or-v1-revoked", epoch=stale_epoch)
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account", return_value=_fail_status("auth")) as mock_check:
            cb.bootstrap(ctx)

        mock_check.assert_called_once()
        assert ctx.failures == []
        assert len(ctx.deferred) == 1

    def test_legacy_hash_only_cache_revalidates(self, env_setup):
        # Pre-W8 cache files carry no timestamp; treat them as stale, not fresh.
        write_env_file(env_setup["user_env"], {"OPENROUTER_API_KEY": "sk-or-v1-legacyfmt"})
        _write_cache_file(env_setup["data_dir"], "sk-or-v1-legacyfmt", epoch=None)
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account", return_value=_ok_status()) as mock_check:
            cb.bootstrap(ctx)

        mock_check.assert_called_once()
        assert ctx.failures == []


class TestBootstrapValidationFailures:
    def test_401_defers_rotation_hint(self, env_setup):
        write_env_file(env_setup["user_env"], {"OPENROUTER_API_KEY": "sk-or-v1-bad"})
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account", return_value=_fail_status("auth")):
            cb.bootstrap(ctx)

        # A key that exists but is rejected is deferred too: it is still only
        # the sessions that call the API that need to hear about it.
        assert ctx.failures == []
        assert len(ctx.deferred) == 1
        msg = ctx.deferred[0]["user_msg"]
        assert "401" in msg or "rejected" in msg.lower()

    def test_402_defers_credit_hint(self, env_setup):
        write_env_file(env_setup["user_env"], {"OPENROUTER_API_KEY": "sk-or-v1-broke"})
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account", return_value=_fail_status("no_credit")):
            cb.bootstrap(ctx)

        assert ctx.failures == []
        assert len(ctx.deferred) == 1
        assert "credit" in ctx.deferred[0]["user_msg"].lower()

    def test_network_error_does_not_block_bootstrap(self, env_setup):
        write_env_file(env_setup["user_env"], {"OPENROUTER_API_KEY": "sk-or-v1-ok"})
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account", side_effect=AccountCheckError("offline")):
            cb.bootstrap(ctx)

        assert ctx.failures == []
        assert any("validation skipped" in m for m in ctx.actions)
        # Cache must NOT be written when validation didn't happen
        assert not (env_setup["data_dir"] / "last_validated.sha256").is_file()


class TestDataDirMigration:
    """0.5.0 hard cutover: the user data dir moved openrouter-kit -> llm-scripting-kit."""

    def _legacy_dir(self, env_setup):
        return env_setup["user_env"].parent.parent / "openrouter-kit"

    def test_old_dir_moved_when_new_absent(self, env_setup):
        legacy = self._legacy_dir(env_setup)
        write_env_file(legacy / ".env", {"OPENROUTER_API_KEY": "sk-or-v1-moved"})
        (legacy / "config.yaml").write_text("default: qwen\n", encoding="utf-8")
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account", return_value=_ok_status()):
            cb.bootstrap(ctx)

        assert not legacy.exists()
        new_dir = env_setup["user_env"].parent
        assert read_env_file(new_dir / ".env")["OPENROUTER_API_KEY"] == "sk-or-v1-moved"
        assert (new_dir / "config.yaml").is_file()
        assert any("migrated user data dir" in m for m in ctx.actions)
        assert ctx.failures == []

    def test_noop_and_quiet_when_no_legacy_dir(self, env_setup):
        write_env_file(env_setup["user_env"], {"OPENROUTER_API_KEY": "sk-or-v1-only"})
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account", return_value=_ok_status()):
            cb.bootstrap(ctx)

        assert not any("migrated user data dir" in m for m in ctx.actions)

    def test_new_dir_wins_when_both_exist(self, env_setup):
        legacy = self._legacy_dir(env_setup)
        write_env_file(legacy / ".env", {"OPENROUTER_API_KEY": "sk-or-v1-stale"})
        write_env_file(env_setup["user_env"], {"OPENROUTER_API_KEY": "sk-or-v1-current"})
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account", return_value=_ok_status()):
            cb.bootstrap(ctx)

        assert legacy.is_dir()
        assert read_env_file(env_setup["user_env"])["OPENROUTER_API_KEY"] == "sk-or-v1-current"
        assert not any("migrated user data dir" in m for m in ctx.actions)

    def test_migration_is_idempotent(self, env_setup):
        legacy = self._legacy_dir(env_setup)
        write_env_file(legacy / ".env", {"OPENROUTER_API_KEY": "sk-or-v1-once"})

        for _ in range(2):
            ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])
            with patch.object(cb, "check_account", return_value=_ok_status()):
                cb.bootstrap(ctx)

        # Second pass finds nothing to move and says nothing.
        assert not any("migrated user data dir" in m for m in ctx.actions)
        assert read_env_file(env_setup["user_env"])["OPENROUTER_API_KEY"] == "sk-or-v1-once"


class TestLegacyMigration:
    def test_loc_ops_legacy_env_migrated(self, env_setup):
        # Put a key in loc-ops's old location
        legacy = env_setup["project_dir"] / ".local-data" / "loc" / ".env"
        write_env_file(legacy, {"OPENROUTER_API_KEY": "sk-or-v1-legacy", "OTHER": "x"})
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account", return_value=_ok_status()):
            cb.bootstrap(ctx)

        # Migrated to canonical .env
        assert env_setup["user_env"].is_file()
        assert read_env_file(env_setup["user_env"])["OPENROUTER_API_KEY"] == "sk-or-v1-legacy"
        # Migration logged
        assert any("migrated" in m for m in ctx.actions)
        # No fix-all entries
        assert ctx.failures == []

    def test_migration_preserves_a_preexisting_unrelated_key_in_the_user_env(self, env_setup):
        # A second, unrelated endpoint's key already lives in the user .env
        # (documented to coexist -- see README "Keys for multiple endpoints
        # coexist in the same .env"). write_env_file REPLACES the file, so a
        # migration that writes only the migrated key would silently delete
        # this one. Seed it BEFORE the migration runs.
        write_env_file(env_setup["user_env"], {"SOME_OTHER_KEY": "keep-me-byte-for-byte"})

        legacy = env_setup["project_dir"] / ".local-data" / "loc" / ".env"
        write_env_file(legacy, {"OPENROUTER_API_KEY": "sk-or-v1-legacy"})
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account", return_value=_ok_status()):
            cb.bootstrap(ctx)

        result = read_env_file(env_setup["user_env"])
        assert result["OPENROUTER_API_KEY"] == "sk-or-v1-legacy"
        # Survives byte-for-byte -- not merely present, but unaltered.
        assert result["SOME_OTHER_KEY"] == "keep-me-byte-for-byte"

    def test_legacy_does_not_overwrite_canonical(self, env_setup):
        # Both files exist; canonical should win and migration is a no-op.
        write_env_file(env_setup["user_env"], {"OPENROUTER_API_KEY": "sk-or-v1-canon"})
        legacy = env_setup["project_dir"] / ".local-data" / "loc" / ".env"
        write_env_file(legacy, {"OPENROUTER_API_KEY": "sk-or-v1-legacy"})
        ctx = FakeContext(env_setup["data_dir"], env_setup["project_dir"])

        with patch.object(cb, "check_account", return_value=_ok_status()):
            cb.bootstrap(ctx)

        # Canonical untouched
        assert read_env_file(env_setup["user_env"])["OPENROUTER_API_KEY"] == "sk-or-v1-canon"
        # No migration log entry
        assert not any("migrated" in m for m in ctx.actions)
