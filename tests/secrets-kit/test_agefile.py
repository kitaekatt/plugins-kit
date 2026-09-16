"""Tests for secrets_kit.agefile -- the process boundary around the age CLI.

Pins I16: decrypt_with_identity used to wrap its whole body -- including the
PATH resolution -- in one ``except SecretsError`` that re-typed EVERY failure
as ``DecryptError``. A missing binary, a spawn failure (OSError), or a caller
deadline (subprocess.TimeoutExpired) are dependency/operation faults with
nothing to do with the identity; only a completed nonzero age exit means the
identity cannot open the blob. Re-typing the first three drove an "unlock
again" passphrase-prompt remedy for faults a passphrase can never fix.

No stderr-string heuristic is used anywhere here or in the source: the three
dependency-fault categories are distinguished by which exception the process
boundary already raises (``_resolve`` before the spawn, ``OSError``,
``subprocess.TimeoutExpired``), never by matching age's stderr text.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "plugins" / "secrets-kit" / "lib")
)

from secrets_kit import DecryptError, SecretsError  # noqa: E402
from secrets_kit import agefile  # noqa: E402


@pytest.fixture
def identity_and_blob(tmp_path):
    identity = tmp_path / "identity.txt"
    identity.write_text("AGE-SECRET-KEY-TEST")
    blob = tmp_path / "secret.age"
    blob.write_bytes(b"ciphertext")
    return identity, blob


# ---------------------------------------------------------------------------
# Category 1: missing binary (_resolve, before the spawn)
# ---------------------------------------------------------------------------


def test_decrypt_missing_binary_is_not_decrypt_error(identity_and_blob, monkeypatch):
    identity, blob = identity_and_blob
    monkeypatch.setattr(agefile.shutil, "which", lambda binary: None)

    with pytest.raises(SecretsError) as excinfo:
        agefile.decrypt_with_identity(identity, blob)

    assert not isinstance(excinfo.value, DecryptError)
    assert "not found on PATH" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Category 2: spawn failure (OSError)
# ---------------------------------------------------------------------------


def test_decrypt_spawn_failure_is_not_decrypt_error(identity_and_blob, monkeypatch):
    identity, blob = identity_and_blob
    monkeypatch.setattr(agefile.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(
        agefile.subprocess,
        "run",
        Mock(side_effect=OSError("Exec format error")),
    )

    with pytest.raises(SecretsError) as excinfo:
        agefile.decrypt_with_identity(identity, blob)

    assert not isinstance(excinfo.value, DecryptError)
    assert "could not run" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Category 3: caller deadline (subprocess.TimeoutExpired)
# ---------------------------------------------------------------------------


def test_decrypt_timeout_is_not_decrypt_error(identity_and_blob, monkeypatch):
    identity, blob = identity_and_blob
    monkeypatch.setattr(agefile.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(
        agefile.subprocess,
        "run",
        Mock(side_effect=subprocess.TimeoutExpired(cmd="age", timeout=30)),
    )

    with pytest.raises(SecretsError) as excinfo:
        agefile.decrypt_with_identity(identity, blob)

    assert not isinstance(excinfo.value, DecryptError)
    assert "timed out" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Category 4: a completed nonzero decryption IS a DecryptError (unchanged)
# ---------------------------------------------------------------------------


def test_decrypt_nonzero_exit_is_still_decrypt_error(identity_and_blob, monkeypatch):
    identity, blob = identity_and_blob
    monkeypatch.setattr(agefile.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    completed = Mock(returncode=1, stdout=b"", stderr=b"no identity matched any recipient")
    monkeypatch.setattr(agefile.subprocess, "run", Mock(return_value=completed))

    with pytest.raises(DecryptError) as excinfo:
        agefile.decrypt_with_identity(identity, blob)

    assert "cannot decrypt" in str(excinfo.value)
    assert blob.name in str(excinfo.value)


def test_decrypt_missing_identity_file_is_still_decrypt_error(tmp_path):
    identity = tmp_path / "no-such-identity.txt"
    blob = tmp_path / "secret.age"
    blob.write_bytes(b"ciphertext")

    with pytest.raises(DecryptError):
        agefile.decrypt_with_identity(identity, blob)


# ---------------------------------------------------------------------------
# Premise 3: keygen and encrypt_to_recipient share _run and keep their
# existing behavior -- a failure there is plain SecretsError, never
# DecryptError (they have no identity to mismatch).
# ---------------------------------------------------------------------------


def test_keygen_nonzero_exit_is_plain_secrets_error_not_decrypt_error(monkeypatch):
    monkeypatch.setattr(agefile.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    completed = Mock(returncode=1, stdout=b"", stderr=b"boom")
    monkeypatch.setattr(agefile.subprocess, "run", Mock(return_value=completed))

    with pytest.raises(SecretsError) as excinfo:
        agefile.keygen()

    assert not isinstance(excinfo.value, DecryptError)


def test_keygen_missing_binary_is_plain_secrets_error(monkeypatch):
    monkeypatch.setattr(agefile.shutil, "which", lambda binary: None)

    with pytest.raises(SecretsError) as excinfo:
        agefile.keygen()

    assert not isinstance(excinfo.value, DecryptError)


def test_encrypt_to_recipient_nonzero_exit_is_plain_secrets_error(tmp_path, monkeypatch):
    monkeypatch.setattr(agefile.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    completed = Mock(returncode=1, stdout=b"", stderr=b"boom")
    monkeypatch.setattr(agefile.subprocess, "run", Mock(return_value=completed))

    with pytest.raises(SecretsError) as excinfo:
        agefile.encrypt_to_recipient(
            "age1testrecipient", b"plaintext", tmp_path / "out.age"
        )

    assert not isinstance(excinfo.value, DecryptError)


# ---------------------------------------------------------------------------
# D03 / S02: the unused alternate interactive runner is gone, and the
# operations that keep the terminal (wrap_identity, unwrap_identity) are
# untouched, deliberately unbounded tty operations -- no timeout added.
# ---------------------------------------------------------------------------


def test_run_interactive_helper_is_removed():
    assert not hasattr(agefile, "run_interactive")


def test_wrap_identity_does_not_use_run_or_timeout(tmp_path, monkeypatch):
    """wrap_identity stays a direct, unbounded subprocess.Popen call."""
    monkeypatch.setattr(agefile.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    popen = Mock()
    popen.return_value.communicate.return_value = None
    popen.return_value.returncode = 0
    monkeypatch.setattr(agefile.subprocess, "Popen", popen)

    code = agefile.wrap_identity("identity text", tmp_path / "wrapped.age")

    assert code == 0
    called_kwargs = popen.call_args.kwargs
    assert "timeout" not in called_kwargs
