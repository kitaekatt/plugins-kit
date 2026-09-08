"""Tests for git-kit scripts/bootstrap.py (gh auth + org membership checks)."""

import json
import subprocess
from unittest.mock import patch

import git_kit_bootstrap as gb


class FakeCtx:
    """Duck-typed stand-in for the bootstrap engine's script context."""

    def __init__(self, project_dir=None):
        self.project_dir = project_dir
        self.logs: list[str] = []
        self.oks: list[str] = []
        self.failures: list[tuple] = []

    def log(self, msg):
        self.logs.append(msg)

    def log_ok(self, msg):
        self.oks.append(msg)

    def add_failure(self, kind, **kwargs):
        self.failures.append((kind, kwargs))


# ---------------------------------------------------------------------------
# _check_auth -- gh auth status string-scraping
# ---------------------------------------------------------------------------


class TestCheckAuth:
    def test_modern_account_line(self):
        """Current gh emits 'Logged in to github.com account <user> (keyring)'."""
        out = (
            "github.com\n"
            "  Logged in to github.com account christina (keyring)\n"
            "  - Active account: true\n"
        )
        fake = subprocess.CompletedProcess(["gh"], 0, stdout="", stderr=out)
        with patch.object(subprocess, "run", return_value=fake):
            ok, user = gb._check_auth("gh")
        assert ok is True
        assert user == "christina"

    def test_legacy_logged_in_as_line(self):
        """Older gh emits 'Logged in to github.com as <user> (oauth_token)'."""
        out = "  Logged in to github.com as octocat (oauth_token)\n"
        fake = subprocess.CompletedProcess(["gh"], 0, stdout=out, stderr="")
        with patch.object(subprocess, "run", return_value=fake):
            ok, user = gb._check_auth("gh")
        assert ok is True
        assert user == "octocat"

    def test_authenticated_but_unparseable_output(self):
        """Auth success with an unrecognized output format still reports ok."""
        fake = subprocess.CompletedProcess(["gh"], 0, stdout="all good\n", stderr="")
        with patch.object(subprocess, "run", return_value=fake):
            ok, user = gb._check_auth("gh")
        assert ok is True
        assert user is None

    def test_nonzero_returncode_means_not_authed(self):
        fake = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="not logged in")
        with patch.object(subprocess, "run", return_value=fake):
            assert gb._check_auth("gh") == (False, None)

    def test_subprocess_error_means_probe_error(self):
        """A timeout (or other exec-level failure) is a probe error, not a genuine
        negative -- it must not collapse into the same falsy answer as 'not logged
        in'."""
        with patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 10)
        ):
            assert gb._check_auth("gh") == (None, None)


# ---------------------------------------------------------------------------
# _check_org_membership
# ---------------------------------------------------------------------------


class TestCheckOrgMembership:
    def test_nonzero_exit_is_a_probe_error_not_a_negative(self, monkeypatch):
        """A failed API call says nothing about membership.

        Genuine non-membership is observed only on a COMPLETED call: gh exits
        zero, returns the org list, and the required org is absent from it. A
        non-zero exit means the call itself did not complete -- a missing
        read:org scope, a rate limit, an outage -- so reporting it as a
        definitive negative sends an authenticated member to ask an
        administrator for access they already hold.
        """
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout="",
                stderr="HTTP 403: Resource not accessible by personal access token",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        status, orgs = gb._check_org_membership("gh", "WidgetCo")
        assert status is None
        assert orgs == []

    def test_member(self):
        fake = subprocess.CompletedProcess(["gh"], 0, stdout="acme\nWidgetCo\n", stderr="")
        with patch.object(subprocess, "run", return_value=fake):
            member, orgs = gb._check_org_membership("gh", "WidgetCo")
        assert member is True
        assert orgs == ["acme", "WidgetCo"]

    def test_membership_is_case_insensitive(self):
        fake = subprocess.CompletedProcess(["gh"], 0, stdout="widgetco\n", stderr="")
        with patch.object(subprocess, "run", return_value=fake):
            member, _ = gb._check_org_membership("gh", "WidgetCo")
        assert member is True

    def test_not_a_member(self):
        fake = subprocess.CompletedProcess(["gh"], 0, stdout="acme\n", stderr="")
        with patch.object(subprocess, "run", return_value=fake):
            member, orgs = gb._check_org_membership("gh", "WidgetCo")
        assert member is False
        assert orgs == ["acme"]

    def test_api_failure_is_a_probe_error(self):
        """An HTTP failure is a probe error, not a membership verdict."""
        fake = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="HTTP 403")
        with patch.object(subprocess, "run", return_value=fake):
            assert gb._check_org_membership("gh", "WidgetCo") == (None, [])

    def test_absent_from_a_returned_list_is_the_genuine_negative(self):
        """The one shape that means non-membership: a completed call whose
        org list does not contain the required org."""
        fake = subprocess.CompletedProcess(["gh"], 0, stdout="OtherOrg\n", stderr="")
        with patch.object(subprocess, "run", return_value=fake):
            assert gb._check_org_membership("gh", "WidgetCo") == (False, ["OtherOrg"])

    def test_subprocess_error_returns_probe_error(self):
        """An OSError (or other exec-level failure) is a probe error, not a genuine
        negative -- it must not collapse into the same falsy answer as 'not a
        member'."""
        with patch.object(subprocess, "run", side_effect=OSError("boom")):
            assert gb._check_org_membership("gh", "WidgetCo") == (None, [])


# ---------------------------------------------------------------------------
# _project_org_config
# ---------------------------------------------------------------------------


class TestProjectOrgConfig:
    def _write_config(self, tmp_path, payload):
        cfg_dir = tmp_path / ".claude"
        cfg_dir.mkdir()
        (cfg_dir / "bootstrap.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_no_project_dir(self):
        assert gb._project_org_config(None) == (None, None)

    def test_missing_file(self, tmp_path):
        assert gb._project_org_config(tmp_path) == (None, None)

    def test_org_and_remediation(self, tmp_path):
        self._write_config(
            tmp_path,
            {
                "git_kit": {
                    "required_organization": "WidgetCo",
                    "access_remediation": "ask @admin for access",
                }
            },
        )
        assert gb._project_org_config(tmp_path) == ("WidgetCo", "ask @admin for access")

    def test_whitespace_values_treated_as_absent(self, tmp_path):
        self._write_config(
            tmp_path,
            {"git_kit": {"required_organization": "  ", "access_remediation": ""}},
        )
        assert gb._project_org_config(tmp_path) == (None, None)

    def test_invalid_json_treated_as_absent(self, tmp_path):
        cfg_dir = tmp_path / ".claude"
        cfg_dir.mkdir()
        (cfg_dir / "bootstrap.json").write_text("{not json", encoding="utf-8")
        assert gb._project_org_config(tmp_path) == (None, None)

    def test_no_git_kit_section(self, tmp_path):
        self._write_config(tmp_path, {"tools": []})
        assert gb._project_org_config(tmp_path) == (None, None)


# ---------------------------------------------------------------------------
# bootstrap -- orchestration
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_gh_missing_logs_and_skips(self):
        ctx = FakeCtx()
        with patch.object(gb, "_resolve_gh", return_value=None):
            gb.bootstrap(ctx)
        assert ctx.failures == []
        assert any("not found" in m for m in ctx.logs)

    def test_unauthenticated_registers_auth_failure(self):
        ctx = FakeCtx()
        with patch.object(gb, "_resolve_gh", return_value="gh"), patch.object(
            gb, "_check_auth", return_value=(False, None)
        ):
            gb.bootstrap(ctx)
        assert len(ctx.failures) == 1
        kind, kwargs = ctx.failures[0]
        assert kind == "config"
        assert kwargs["field"] == "github_auth"
        assert gb.GH_AUTH_LOGIN_CMD in kwargs["agent_msg"]

    def test_authenticated_without_org_requirement_is_ok(self):
        ctx = FakeCtx(project_dir=None)
        with patch.object(gb, "_resolve_gh", return_value="gh"), patch.object(
            gb, "_check_auth", return_value=(True, "christina")
        ):
            gb.bootstrap(ctx)
        assert ctx.failures == []
        assert any("christina" in m for m in ctx.oks)

    def test_org_member_is_ok(self, tmp_path):
        cfg_dir = tmp_path / ".claude"
        cfg_dir.mkdir()
        (cfg_dir / "bootstrap.json").write_text(
            json.dumps({"git_kit": {"required_organization": "WidgetCo"}}),
            encoding="utf-8",
        )
        ctx = FakeCtx(project_dir=tmp_path)
        with patch.object(gb, "_resolve_gh", return_value="gh"), patch.object(
            gb, "_check_auth", return_value=(True, "christina")
        ), patch.object(gb, "_check_org_membership", return_value=(True, ["WidgetCo"])):
            gb.bootstrap(ctx)
        assert ctx.failures == []
        assert any("WidgetCo" in m for m in ctx.oks)

    def test_non_member_registers_org_failure_with_remediation(self, tmp_path):
        cfg_dir = tmp_path / ".claude"
        cfg_dir.mkdir()
        (cfg_dir / "bootstrap.json").write_text(
            json.dumps(
                {
                    "git_kit": {
                        "required_organization": "WidgetCo",
                        "access_remediation": "ask @admin for access",
                    }
                }
            ),
            encoding="utf-8",
        )
        ctx = FakeCtx(project_dir=tmp_path)
        with patch.object(gb, "_resolve_gh", return_value="gh"), patch.object(
            gb, "_check_auth", return_value=(True, "christina")
        ), patch.object(gb, "_check_org_membership", return_value=(False, ["acme"])):
            gb.bootstrap(ctx)
        assert len(ctx.failures) == 1
        kind, kwargs = ctx.failures[0]
        assert kwargs["field"] == "github_org"
        assert "ask @admin for access" in kwargs["user_msg"]
        assert "ask @admin for access" in kwargs["agent_msg"]
        assert "acme" in kwargs["agent_msg"]

    def test_auth_probe_error_skips_without_registering_failure(self):
        """A probe_error (network hiccup, timeout, outage) must not be diagnosed as
        'Not logged in to GitHub' -- that tells an authenticated user to re-login
        for a problem that has nothing to do with their auth state."""
        ctx = FakeCtx()
        with patch.object(gb, "_resolve_gh", return_value="gh"), patch.object(
            gb, "_check_auth", return_value=(None, None)
        ):
            gb.bootstrap(ctx)
        assert ctx.failures == []
        assert ctx.logs == [
            "github auth: probe failed (gh auth status did not complete), skipping check"
        ]

    def test_org_probe_error_skips_without_registering_failure(self, tmp_path):
        """A probe_error checking org membership must not be diagnosed as 'not a
        member' -- that tells a member with a network hiccup to go ask an admin
        for access they already have."""
        cfg_dir = tmp_path / ".claude"
        cfg_dir.mkdir()
        (cfg_dir / "bootstrap.json").write_text(
            json.dumps({"git_kit": {"required_organization": "WidgetCo"}}),
            encoding="utf-8",
        )
        ctx = FakeCtx(project_dir=tmp_path)
        with patch.object(gb, "_resolve_gh", return_value="gh"), patch.object(
            gb, "_check_auth", return_value=(True, "christina")
        ), patch.object(gb, "_check_org_membership", return_value=(None, [])):
            gb.bootstrap(ctx)
        assert ctx.failures == []
        assert ctx.logs[-1] == (
            "github org: probe failed checking membership in WidgetCo, skipping check"
        )

    def test_auth_failure_takes_priority_over_org_check(self):
        """Org membership is only checked when authenticated."""
        ctx = FakeCtx(project_dir="/nonexistent")
        with patch.object(gb, "_resolve_gh", return_value="gh"), patch.object(
            gb, "_check_auth", return_value=(False, None)
        ), patch.object(gb, "_check_org_membership") as org_mock:
            gb.bootstrap(ctx)
        assert org_mock.call_count == 0
        assert ctx.failures[0][1]["field"] == "github_auth"
