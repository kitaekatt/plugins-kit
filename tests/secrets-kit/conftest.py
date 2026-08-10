"""Fixtures for the secrets-kit suite.

The suite never invokes real ``age``: the crypto is a subprocess boundary we
own a thin wrapper around, and testing through it would only test that age
works. What matters here is the CONVERGENCE logic -- what gets written, when
nothing gets written, what fails and how it is classified -- so age is
replaced with a reversible stand-in and the rest is exercised for real
against a temp filesystem.
"""

import json
import os
import sys
from pathlib import Path

import pytest

_LIB = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "secrets-kit"
    / "lib"
)
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))


@pytest.fixture(scope="session")
def git_template(tmp_path_factory):
    """Build an expensive git tree once per process; return its path.

    Usage: ``template = git_template("name", build_fn)`` where ``build_fn``
    receives an empty directory. Callers must treat the result as READ-ONLY and
    hand tests a `sk_testlib.copy_git_tree` copy of it -- see that module for
    why this exists and why it is safe under pytest-xdist.
    """
    root = tmp_path_factory.mktemp("git-templates")
    built = {}

    def get(key, build):
        if key not in built:
            target = root / key
            target.mkdir(parents=True)
            build(target)
            built[key] = target
        return built[key]

    return get


# A blob is just the plaintext with a marker prefix, so "decryption" is
# reversible and a wrong-identity case can be simulated by changing the
# marker. Enough structure to catch a real bug, no more.
_MARKER = b"AGE-FAKE:"


@pytest.fixture
def fake_age(monkeypatch):
    """Replace the age wrapper with a reversible stand-in."""
    from secrets_kit import DecryptError, agefile
    from secrets_kit import converge as converge_mod

    state = {"recipient": "age1testrecipient", "identity": "AGE-SECRET-KEY-TEST"}

    def encrypt_to_recipient(recipient, plaintext, out_path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(_MARKER + recipient.encode() + b"\n" + plaintext)

    def decrypt_with_identity(identity_path, blob_path):
        if not Path(identity_path).is_file():
            raise DecryptError(f"no unlocked identity at {identity_path}")
        data = Path(blob_path).read_bytes()
        if not data.startswith(_MARKER):
            raise DecryptError(f"cannot decrypt {Path(blob_path).name}")
        body = data[len(_MARKER):]
        recipient, _, plaintext = body.partition(b"\n")
        if recipient.decode() != state["recipient"]:
            raise DecryptError(f"cannot decrypt {Path(blob_path).name}")
        return plaintext

    monkeypatch.setattr(agefile, "encrypt_to_recipient", encrypt_to_recipient)
    monkeypatch.setattr(agefile, "decrypt_with_identity", decrypt_with_identity)
    monkeypatch.setattr(converge_mod, "decrypt_with_identity", decrypt_with_identity)
    return state


@pytest.fixture
def no_network(monkeypatch):
    """Make the repo module inert -- the clone is whatever the test put there."""
    from secrets_kit import repo as repo_mod
    from secrets_kit import converge as converge_mod

    monkeypatch.setattr(converge_mod.repo_mod, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(
        converge_mod.repo_mod, "is_clone", lambda path: Path(path).is_dir()
    )
    return repo_mod


@pytest.fixture
def fleet(tmp_path, fake_age, no_network, monkeypatch):
    """A seeded secrets repo + a configured machine, ready to converge.

    Returns an object exposing the paths a test wants to poke at.
    """
    clone = tmp_path / "data" / "repo"
    blobs = clone / "blobs"
    blobs.mkdir(parents=True)

    dest_root = tmp_path / "bank" / "secrets"
    dest_root.mkdir(parents=True)

    from secrets_kit import agefile

    agefile.encrypt_to_recipient(
        fake_age["recipient"], b"token-value\n", blobs / "ha-token.txt.age"
    )
    agefile.encrypt_to_recipient(
        fake_age["recipient"], b"rolfing-value\n", blobs / "rolfing.txt.age"
    )

    manifest = {
        "version": 1,
        "recipient": fake_age["recipient"],
        "profiles": {"home-admin": ["ha-token"], "rolfing": ["rolfing"]},
        "entries": {
            "ha-token": {
                "blob": "blobs/ha-token.txt.age",
                "dest": "${BANK}/secrets/ha-token.txt",
                "mode": "0600",
            },
            "rolfing": {
                "blob": "blobs/rolfing.txt.age",
                "dest": "${BANK}/secrets/rolfing.txt",
                "mode": "0600",
            },
        },
    }
    (clone / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    config = {
        "repo": "git@example.com:acct/fleet-secrets.git",
        "vars": {"BANK": str(tmp_path / "bank")},
        "machines": {
            "testbox": {"profiles": ["home-admin"]},
            "otherbox": {"profiles": ["home-admin", "rolfing"]},
        },
    }
    config_path = tmp_path / "secrets.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setattr(
        "secrets_kit.manifest.resolve_host", lambda: ["testbox"]
    )

    class Fleet:
        pass

    f = Fleet()
    f.tmp = tmp_path
    f.clone = clone
    f.blobs = blobs
    f.data_dir = tmp_path / "data"
    f.config_path = config_path
    f.manifest_path = clone / "manifest.json"
    f.dest_root = dest_root
    f.recipient = fake_age["recipient"]

    def unlock():
        ident = f.data_dir / "identity.txt"
        ident.parent.mkdir(parents=True, exist_ok=True)
        ident.write_text("AGE-SECRET-KEY-TEST", encoding="utf-8")
        return ident

    f.unlock = unlock
    return f
