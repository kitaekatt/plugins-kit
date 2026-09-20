"""_cmd_pair: the 101 (link button not pressed) retry loop, and the minted
key file's mode and secrecy.

`requests` is a stub module installed by conftest (satisfying the module-
level `import requests` in hue_kit_cli.py without the real package); each
test here replaces its `post` attribute with a fake that never touches a
socket. `time.sleep` is patched to a no-op so the retry loop does not
actually wait.
"""

from __future__ import annotations

import os
import stat
import sys
from argparse import Namespace

import pytest


def _fake_response(payload):
    class _Resp:
        def json(self):
            return payload
    return _Resp()


class TestRetryOnLinkButtonNotPressed:
    """While the bridge answers error type 101 ('link button not pressed'),
    _cmd_pair must keep polling rather than giving up -- and must stop
    polling and succeed the moment a success response arrives."""

    def test_retries_past_101_then_succeeds(self, hue_cli, tmp_path,
                                            monkeypatch, capfd):
        calls = {"n": 0}

        def fake_post(url, json=None, verify=None, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                return _fake_response(
                    [{"error": {"type": 101,
                                "description": "link button not pressed"}}])
            return _fake_response([{"success": {"username": "SECRETKEY123"}}])

        monkeypatch.setattr(sys.modules["requests"], "post", fake_post, raising=False)
        monkeypatch.setattr("time.sleep", lambda s: None)
        monkeypatch.setattr(hue_cli, "PAIRED_KEY_FILE", tmp_path / "app-key.txt")

        rc = hue_cli._cmd_pair(
            Namespace(force=False, no_wait=True))

        assert rc == 0
        assert calls["n"] == 3
        assert (tmp_path / "app-key.txt").read_text() == "SECRETKEY123\n"

    def test_gives_up_after_the_deadline_with_101_forever(
            self, hue_cli, tmp_path, monkeypatch):
        """A bridge that never gets its button pressed must eventually raise
        rather than loop forever -- simulated by advancing monotonic time
        past the 30s deadline on the second poll."""
        times = iter([0.0, 0.0, 31.0])

        def fake_post(url, json=None, verify=None, timeout=None):
            return _fake_response(
                [{"error": {"type": 101,
                            "description": "link button not pressed"}}])

        monkeypatch.setattr(sys.modules["requests"], "post", fake_post, raising=False)
        monkeypatch.setattr("time.sleep", lambda s: None)
        monkeypatch.setattr("time.monotonic", lambda: next(times))
        monkeypatch.setattr(hue_cli, "PAIRED_KEY_FILE", tmp_path / "app-key.txt")

        with pytest.raises(SystemExit):
            hue_cli._cmd_pair(
                Namespace(force=False, no_wait=True))

        assert not (tmp_path / "app-key.txt").exists()


class TestKeyFileSecrecy:
    """The minted key is written to PAIRED_KEY_FILE with owner-only (0600)
    permissions, and must never be printed to stdout or stderr -- only the
    file path and the '(0600)' marker belong in the terminal output."""

    def test_key_file_is_owner_only_and_key_never_printed(
            self, hue_cli, tmp_path, monkeypatch, capfd):
        secret = "TOP-SECRET-KEY-VALUE"

        def fake_post(url, json=None, verify=None, timeout=None):
            return _fake_response([{"success": {"username": secret}}])

        monkeypatch.setattr(sys.modules["requests"], "post", fake_post, raising=False)
        key_file = tmp_path / "app-key.txt"
        monkeypatch.setattr(hue_cli, "PAIRED_KEY_FILE", key_file)

        rc = hue_cli._cmd_pair(
            Namespace(force=False, no_wait=True))

        captured = capfd.readouterr()
        assert rc == 0
        assert secret not in captured.out
        assert secret not in captured.err
        assert key_file.read_text() == secret + "\n"
        mode = stat.S_IMODE(os.stat(key_file).st_mode)
        assert mode == 0o600


class TestOtherPairingErrorsRaise:
    """A pairing error that is not the 101 retry code must raise
    immediately -- it is not a 'keep polling' condition."""

    def test_non_101_error_raises_without_retrying(self, hue_cli, tmp_path,
                                                    monkeypatch):
        calls = {"n": 0}

        def fake_post(url, json=None, verify=None, timeout=None):
            calls["n"] += 1
            return _fake_response(
                [{"error": {"type": 1, "description": "unauthorized"}}])

        monkeypatch.setattr(sys.modules["requests"], "post", fake_post, raising=False)
        monkeypatch.setattr("time.sleep", lambda s: None)
        monkeypatch.setattr(hue_cli, "PAIRED_KEY_FILE", tmp_path / "app-key.txt")

        with pytest.raises(SystemExit):
            hue_cli._cmd_pair(
                Namespace(force=False, no_wait=True))

        assert calls["n"] == 1
