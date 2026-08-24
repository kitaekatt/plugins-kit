"""Convergence behavior: what gets written, what stays silent, what fails."""

import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from secrets_kit import cli_command
from secrets_kit.converge import (
    FAILURE_CONFIG,
    FAILURE_LOCKED,
    converge,
    paths_for,
)
from secrets_kit.state import State


def _run(fleet, **kwargs):
    return converge(fleet.config_path, fleet.data_dir, **kwargs)


def test_absent_config_is_a_silent_no_op(tmp_path):
    """A third party who declared nothing must get silence, not an error."""
    result = converge(tmp_path / "nope.json", tmp_path / "data")
    assert result.skipped_reason == "not configured"
    assert result.failures == []


def test_host_not_listed_is_a_no_op(fleet, monkeypatch):
    """Subsetting by omission: an unlisted machine holds nothing, quietly."""
    monkeypatch.setattr("secrets_kit.manifest.resolve_host", lambda: ["stranger"])
    result = _run(fleet)
    assert result.skipped_reason == "no profiles for this host"
    assert result.failures == []


def test_locked_machine_raises_exactly_one_ask(fleet, monkeypatch):
    """No identity -> one ASK, and nothing else. Per-entry noise is unactionable."""
    from secrets_kit import converge as converge_mod

    monkeypatch.setattr(converge_mod, "age_available", lambda: True)
    result = _run(fleet)
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.key == FAILURE_LOCKED
    assert failure.ask_reason == "info"
    assert cli_command("unlock --new-terminal") in failure.agent_msg
    assert not (fleet.dest_root / "ha-token.txt").exists()


def test_locked_agent_message_forbids_pasting_the_passphrase(fleet, monkeypatch):
    """The prepared statement must never invite a transcript-visible passphrase."""
    from secrets_kit import converge as converge_mod

    monkeypatch.setattr(converge_mod, "age_available", lambda: True)
    failure = _run(fleet).failures[0]
    assert "paste" in failure.agent_msg.lower()
    assert "not an API key" in failure.agent_msg


def test_missing_age_binary_is_not_an_independent_locked_failure(fleet, monkeypatch):
    """age absent -> no locked ASK; bootstrap's own tool-check already owns it.

    Established empirically: when age was installed on a real machine, the
    "secrets: locked" report cleared on its own with no further user action --
    it was never an independent decision, so it must not be reported as one.
    """
    from secrets_kit import converge as converge_mod

    monkeypatch.setattr(converge_mod, "age_available", lambda: False)
    result = _run(fleet)
    assert result.failures == []
    assert result.skipped_reason == "age not installed; bootstrap will install it"
    assert not (fleet.dest_root / "ha-token.txt").exists()


def test_locked_failure_returns_once_age_is_present(fleet, monkeypatch):
    """age present + identity missing -> the ordinary locked ASK, as before."""
    from secrets_kit import converge as converge_mod

    monkeypatch.setattr(converge_mod, "age_available", lambda: True)
    result = _run(fleet)
    assert len(result.failures) == 1
    assert result.failures[0].key == FAILURE_LOCKED
    assert result.skipped_reason == "locked (awaiting one-time unlock)"


def test_unlocked_machine_materializes_its_profile(fleet):
    fleet.unlock()
    result = _run(fleet)
    assert result.failures == []
    assert result.written == 1
    assert (fleet.dest_root / "ha-token.txt").read_bytes() == b"token-value\n"


def test_only_this_machines_profiles_are_materialized(fleet):
    """testbox has home-admin but not rolfing -- the rolfing secret must not land."""
    fleet.unlock()
    _run(fleet)
    assert (fleet.dest_root / "ha-token.txt").exists()
    assert not (fleet.dest_root / "rolfing.txt").exists()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes")
def test_materialized_secret_is_owner_only(fleet):
    fleet.unlock()
    _run(fleet)
    mode = stat.S_IMODE(os.stat(fleet.dest_root / "ha-token.txt").st_mode)
    assert mode == 0o600


def test_second_pass_is_free(fleet):
    """Steady state decrypts nothing -- the whole point of hashing dest."""
    fleet.unlock()
    _run(fleet)

    from secrets_kit import converge as converge_mod

    calls = []
    real = converge_mod.decrypt_with_identity

    def counting(identity, blob):
        calls.append(blob)
        return real(identity, blob)

    converge_mod.decrypt_with_identity = counting
    try:
        result = _run(fleet)
    finally:
        converge_mod.decrypt_with_identity = real

    assert calls == []
    assert result.ok == 1
    assert result.written == 0


def test_deleted_destination_self_heals(fleet):
    fleet.unlock()
    _run(fleet)
    (fleet.dest_root / "ha-token.txt").unlink()

    result = _run(fleet)
    assert result.written == 1
    assert (fleet.dest_root / "ha-token.txt").read_bytes() == b"token-value\n"


def test_tampered_destination_is_rewritten(fleet):
    """A truncated or edited secret is drift, and drift converges."""
    fleet.unlock()
    _run(fleet)
    (fleet.dest_root / "ha-token.txt").write_bytes(b"garbage")

    _run(fleet)
    assert (fleet.dest_root / "ha-token.txt").read_bytes() == b"token-value\n"


def test_rotated_blob_is_repulled(fleet):
    from secrets_kit import agefile

    fleet.unlock()
    _run(fleet)

    agefile.encrypt_to_recipient(
        fleet.recipient, b"rotated\n", fleet.blobs / "ha-token.txt.age"
    )
    result = _run(fleet)
    assert result.written == 1
    assert (fleet.dest_root / "ha-token.txt").read_bytes() == b"rotated\n"


def test_removing_an_entry_upstream_deletes_the_local_copy(fleet):
    """Otherwise 'remove a secret' is a no-op everywhere it already landed."""
    fleet.unlock()
    _run(fleet)
    dest = fleet.dest_root / "ha-token.txt"
    assert dest.exists()

    manifest = json.loads(fleet.manifest_path.read_text())
    manifest["entries"].pop("ha-token")
    manifest["profiles"]["home-admin"] = []
    fleet.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run(fleet)
    assert result.removed == 1
    assert not dest.exists()


def test_shrinking_a_profile_also_deletes(fleet):
    """The entry still exists upstream; this machine simply stops being entitled."""
    fleet.unlock()
    _run(fleet)

    config = json.loads(fleet.config_path.read_text())
    config["machines"]["testbox"]["profiles"] = []
    fleet.config_path.write_text(json.dumps(config), encoding="utf-8")

    result = _run(fleet)
    assert result.removed == 1
    assert not (fleet.dest_root / "ha-token.txt").exists()


def test_identity_rotation_upstream_asks_for_a_re_unlock(fleet):
    """The remedy is 'unlock again', which is categorically not 'fix the manifest'."""
    from secrets_kit import agefile

    fleet.unlock()
    _run(fleet)

    # New fleet identity: re-encrypt to a recipient this machine cannot read.
    manifest = json.loads(fleet.manifest_path.read_text())
    manifest["recipient"] = "age1rotated"
    fleet.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    agefile.encrypt_to_recipient(
        "age1rotated", b"token-value\n", fleet.blobs / "ha-token.txt.age"
    )

    result = _run(fleet)
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.key == FAILURE_LOCKED
    assert failure.ask_reason == "info"
    assert "rotat" in failure.agent_msg.lower()


def test_unknown_profile_fails_loudly(fleet):
    """A typo that silently provisions nothing is the worst possible outcome."""
    fleet.unlock()
    config = json.loads(fleet.config_path.read_text())
    config["machines"]["testbox"]["profiles"] = ["home-admni"]
    fleet.config_path.write_text(json.dumps(config), encoding="utf-8")

    result = _run(fleet)
    assert len(result.failures) == 1
    assert result.failures[0].key == FAILURE_CONFIG
    assert "home-admni" in result.failures[0].agent_msg


def test_unresolvable_var_fails_rather_than_writing_a_literal_path(fleet):
    fleet.unlock()
    manifest = json.loads(fleet.manifest_path.read_text())
    manifest["entries"]["ha-token"]["dest"] = "${NOPE}/x.txt"
    fleet.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run(fleet)
    assert result.failures
    assert not (fleet.tmp / "${NOPE}").exists()


def test_crlf_assertion_catches_a_bad_seed(fleet):
    """newline: lf asserts the SEED, so a broken ssh key is caught at materialization."""
    from secrets_kit import agefile

    fleet.unlock()
    manifest = json.loads(fleet.manifest_path.read_text())
    manifest["entries"]["ha-token"]["newline"] = "lf"
    fleet.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    agefile.encrypt_to_recipient(
        fleet.recipient, b"line1\r\nline2\r\n", fleet.blobs / "ha-token.txt.age"
    )

    result = _run(fleet)
    assert len(result.failures) == 1
    assert "CRLF" in result.failures[0].agent_msg
    assert not (fleet.dest_root / "ha-token.txt").exists()


def test_missing_dest_parent_is_a_failure_not_a_created_tree(fleet):
    """Never invent a directory for a secret -- a wrong path must be visible."""
    fleet.unlock()
    manifest = json.loads(fleet.manifest_path.read_text())
    manifest["entries"]["ha-token"]["dest"] = "${BANK}/not-cloned-yet/ha-token.txt"
    fleet.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run(fleet)
    assert len(result.failures) == 1
    assert not (fleet.tmp / "bank" / "not-cloned-yet").exists()


def test_machine_not_in_env_registry_asks_rather_than_guessing(fleet):
    fleet.unlock()
    result = _run(fleet, known_machines=["otherbox", "5090W"])
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.key == FAILURE_CONFIG
    assert failure.ask_reason == "info"
    assert not (fleet.dest_root / "ha-token.txt").exists()


def test_env_registry_agreement_permits_the_pass(fleet):
    fleet.unlock()
    result = _run(fleet, known_machines=["testbox", "otherbox"])
    assert result.failures == []
    assert result.written == 1


def test_a_write_error_fails_one_entry_without_aborting_the_pass(fleet, monkeypatch):
    """An OSError must be reported, not propagated.

    Regression guard: an exception escaping converge() reaches the bootstrap
    engine and fails the entire session's pass over one unwritable file --
    every other plugin's provisioning included.
    """
    fleet.unlock()
    from secrets_kit import converge as converge_mod

    def boom(dest, data, mode):
        raise OSError("disk full")

    monkeypatch.setattr(converge_mod, "_atomic_write", boom)
    result = _run(fleet)

    assert len(result.failures) == 1
    assert "disk full" in result.failures[0].agent_msg
    # And nothing partial is left at the destination.
    assert [p.name for p in fleet.dest_root.iterdir()] == []


def test_state_file_is_not_world_readable(fleet):
    """It maps where every credential lives, even though it holds no values."""
    fleet.unlock()
    _run(fleet)
    state_path = paths_for(fleet.data_dir)["state"]
    assert state_path.is_file()
    if not sys.platform.startswith("win"):
        assert stat.S_IMODE(os.stat(state_path).st_mode) == 0o600


def test_corrupt_state_recovers_instead_of_blocking(fleet):
    """state.json is a cache; losing it costs a re-decrypt, never a blocked machine."""
    fleet.unlock()
    _run(fleet)
    paths_for(fleet.data_dir)["state"].write_text("{ not json", encoding="utf-8")

    result = _run(fleet)
    assert result.failures == []
    assert (fleet.dest_root / "ha-token.txt").read_bytes() == b"token-value\n"


def test_state_records_dest_so_orphans_can_be_swept(fleet):
    fleet.unlock()
    _run(fleet)
    state = State.load(paths_for(fleet.data_dir)["state"])
    assert state.get("ha-token")["dest"].endswith("ha-token.txt")


def test_cloned_but_unseeded_repo_says_run_init(fleet):
    """The state a machine is in right after the repo is created.

    Must name the actual next step rather than reading as a broken manifest --
    otherwise the reader goes off diagnosing a file that was never supposed to
    exist yet.
    """
    fleet.unlock()
    fleet.manifest_path.unlink()

    result = _run(fleet)
    assert result.skipped_reason == "repo not seeded yet"
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.ask_reason == "info"
    assert cli_command("init --new-terminal") in failure.agent_msg
    assert "not a broken" in failure.agent_msg.lower()


def test_cli_command_resolves_the_shim_that_actually_exists():
    """The rendered invocation must point at a real, runnable file.

    A path that merely looks plausible is the same defect as the bare name it
    replaced: the user spends their one interactive step on a command that
    fails.
    """
    rendered = cli_command()
    shim = Path(os.path.expanduser(rendered))
    assert shim.is_file(), rendered
    assert shim.name == "secrets-kit"


@pytest.mark.parametrize("verb", ["init", "unlock", "rotate-identity"])
def test_passphrase_verbs_are_never_offered_without_new_terminal(verb):
    """A bare interactive verb in a message is an instruction that hangs.

    age prompts on a tty neither the agent nor the `!` prefix has, so any
    message naming one of these verbs has to name --new-terminal with it.
    """
    from secrets_kit import converge as converge_mod

    source = Path(converge_mod.__file__).read_text(encoding="utf-8")
    for match in re.finditer(rf"cli_command\(['\"]({verb})([^'\"]*)['\"]\)", source):
        assert "--new-terminal" in match.group(2), match.group(0)


@pytest.mark.parametrize(
    "verb", ["init", "unlock", "add", "status", "remove", "rotate-identity"]
)
def test_no_message_emits_a_bare_command_name(verb):
    """Nothing may hand out `secrets-kit <verb>` unqualified.

    The shim is not on PATH, so a bare name is `command not found` -- and for a
    prepared statement the user is told to type verbatim, that wastes the one
    step they were asked to take. Every emitted command goes through
    cli_command().
    """
    from secrets_kit import converge as converge_mod
    from secrets_kit import manifest as manifest_mod

    bare = re.compile(rf"(?<![\w/\\.-])secrets-kit {verb}(?![\w-])")
    for module in (converge_mod, manifest_mod):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert not bare.search(source), f"{module.__name__} emits a bare command"


def test_unseeded_check_precedes_the_identity_check(fleet):
    """An unseeded repo on a LOCKED machine must still say 'seed', not 'unlock'.

    Ordering matters: telling someone to unlock a repo that has no identity to
    unlock is a dead end.
    """
    fleet.manifest_path.unlink()
    result = _run(fleet)
    assert result.skipped_reason == "repo not seeded yet"
    assert "init" in result.failures[0].agent_msg
