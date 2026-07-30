"""Seeding is the one irreversible verb -- these are its two safety properties.

1. It decides "is this repo already seeded?" from the REMOTE, so a machine
   whose clone predates someone else's seed cannot generate a second fleet
   identity and orphan every blob encrypted to the first.
2. It is all-or-nothing. If the seed cannot be published, nothing is kept --
   in particular no local identity naming a key the fleet has never seen.

Both are regressions from a real incident: a clone that had not fetched since
before the repo was seeded reported "never seeded", init generated a second
identity, and only git's push rejection stopped it reaching the fleet.
"""

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_CLI_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "secrets-kit"
    / "scripts"
    / "secrets_kit_cli.py"
)


def _load_cli():
    spec = importlib.util.spec_from_file_location("secrets_kit_cli", _CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["secrets_kit_cli"] = module
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


@pytest.fixture
def seeding(tmp_path, monkeypatch):
    """A configured machine whose clone is one commit behind a bare remote."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(remote))

    origin = tmp_path / "origin"
    _git(tmp_path, "clone", "--quiet", str(remote), str(origin))
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / "README.md").write_text("readme", encoding="utf-8")
    _git(origin, "add", "--", "README.md")
    _git(origin, "commit", "--quiet", "-m", "init")
    _git(origin, "push", "--quiet", "origin", "main")

    data_dir = tmp_path / "data"
    clone = data_dir / "repo"
    data_dir.mkdir()
    _git(tmp_path, "clone", "--quiet", str(remote), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")

    config_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps(
            {
                "repo": str(remote),
                "machines": {"testbox": {"profiles": []}},
            }
        ),
        encoding="utf-8",
    )

    cli = _load_cli()
    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "DATA_DIR", data_dir)
    monkeypatch.setattr(
        "secrets_kit.manifest.resolve_host", lambda: ["testbox"]
    )

    from secrets_kit import agefile

    monkeypatch.setattr(
        agefile, "keygen", lambda: ("AGE-SECRET-KEY-NEW", "age1newrecipient")
    )

    def wrap_identity(identity_text, out_path):
        # Must look like real age armor: the repo's pre-commit guard refuses to
        # record an identity.age that is not ciphertext, and it is right to.
        Path(out_path).write_text(
            "-----BEGIN AGE ENCRYPTED FILE-----\nZmFrZQ==\n"
            "-----END AGE ENCRYPTED FILE-----\n",
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(agefile, "wrap_identity", wrap_identity)

    class Seeding:
        pass

    s = Seeding()
    s.cli = cli
    s.remote = remote
    s.origin = origin
    s.clone = clone
    s.data_dir = data_dir
    s.identity = data_dir / "identity.txt"
    s.args = argparse.Namespace(command="init", force=False, new_terminal=False)

    def seed_remotely():
        """Another machine seeds the repo; our clone knows nothing about it."""
        (s.origin / "identity.age").write_text(
            "-----BEGIN AGE ENCRYPTED FILE-----\ndGhlaXJz\n"
            "-----END AGE ENCRYPTED FILE-----\n",
            encoding="utf-8",
        )
        (s.origin / "manifest.json").write_text(
            json.dumps({"version": 1, "recipient": "age1theirs",
                        "profiles": {}, "entries": {}}),
            encoding="utf-8",
        )
        _git(s.origin, "add", "--", "identity.age", "manifest.json")
        _git(s.origin, "commit", "--quiet", "-m", "seed")
        _git(s.origin, "push", "--quiet")

    s.seed_remotely = seed_remotely
    return s


def test_seeds_a_fresh_repo(seeding, capsys):
    assert seeding.cli.cmd_init(seeding.args) == 0

    _git(seeding.origin, "pull", "--quiet")
    assert (seeding.origin / "identity.age").is_file()
    assert (seeding.origin / "manifest.json").is_file()
    # The seeding machine is immediately usable -- it does not have to unlock
    # itself against the key it just generated.
    assert seeding.identity.read_text() == "AGE-SECRET-KEY-NEW"


def test_refuses_when_the_remote_is_already_seeded(seeding, capsys):
    """The incident case: our checkout predates someone else's seed."""
    seeding.seed_remotely()
    assert not (seeding.clone / "identity.age").exists()

    assert seeding.cli.cmd_init(seeding.args) == 1

    err = capsys.readouterr().err
    assert "already seeded" in err
    assert "unlock" in err
    # Nothing generated, nothing cached, and the fleet's identity untouched.
    assert not seeding.identity.exists()
    _git(seeding.origin, "pull", "--quiet")
    assert "dGhlaXJz" in (seeding.origin / "identity.age").read_text()


def test_diverged_clone_over_a_seeded_remote_points_at_unlock(seeding, capsys):
    """The incident's aftermath: a local seed that never published.

    Reporting only the git divergence would leave the user resolving branches.
    The actionable fact is that the remote is seeded, so the local commit is
    worthless and what this machine needs is `unlock`.
    """
    seeding.seed_remotely()
    (seeding.clone / "identity.age").write_text("ours", encoding="utf-8")
    _git(seeding.clone, "add", "--", "identity.age")
    _git(seeding.clone, "commit", "--quiet", "--no-verify", "-m", "seed")

    assert seeding.cli.cmd_init(seeding.args) == 1

    err = capsys.readouterr().err
    assert "diverged" in err
    assert "unlock" in err
    assert not seeding.identity.exists()


def test_rolls_back_when_the_seed_cannot_be_published(seeding, capsys, monkeypatch):
    """A seed that did not publish must leave no trace, above all no identity.

    Keeping it would be worse than failing: the next run would see
    identity.age in the checkout, call the repo seeded, and send the user to
    unlock a key no other machine can ever produce.
    """
    from secrets_kit import repo as repo_mod
    from secrets_kit import SecretsError

    before = repo_mod.head_sha(seeding.clone)

    def refuse(*a, **k):
        raise SecretsError("git push failed: rejected")

    monkeypatch.setattr(repo_mod, "commit_and_push", refuse)

    assert seeding.cli.cmd_init(seeding.args) == 1

    assert "discarded" in capsys.readouterr().err
    assert not seeding.identity.exists()
    assert not (seeding.clone / "identity.age").exists()
    assert repo_mod.head_sha(seeding.clone) == before
