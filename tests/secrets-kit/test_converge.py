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
    """Corrupt cache alone permits selected materialization; lost orphan paths remain lost."""
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


# Declaration acceptance precedes every materialization/removal effect.
def _set_declaration_field(data, path, value):
    row = data
    for key in path[:-1]:
        row = row[key]
    row[path[-1]] = value


def _owned_declaration_preimages(fleet):
    destination = fleet.dest_root / "ha-token.txt"
    destination.write_bytes(b"existing dummy destination")
    orphan = fleet.tmp / "owned-orphan.txt"
    orphan.write_bytes(b"existing dummy orphan")
    state_path = fleet.data_dir / "state.json"
    state = State(state_path, {})
    state.record("owned-orphan", blob_sha="dummy", dest_sha="dummy", mode=0o600, dest=str(orphan))
    state.save()
    return {path: path.read_bytes() for path in [destination, orphan, state_path]}


_CONFIG_SHAPES = [
    *((('repo',), value, 'repo') for value in [False, ['local'], {'url': 'local'}]),
    *((('vars',), value, 'vars') for value in [False, 0, '', [], [['BANK', 'x']]]),
    *((('machines',), value, 'machines') for value in [False, 0, '', [], [['testbox', {}]]]),
    *((('vars', 'BANK'), value, 'vars.BANK') for value in [None, 1, False, [], {}]),
    *((('machines', 'testbox'), value, 'machines.testbox') for value in [False, 0, '', [], 'bad']),
    *((('machines', 'testbox', 'profiles'), value, 'profiles') for value in [False, 0, '', {}, [1], [[]], [{}]]),
    *((('machines', 'testbox', 'vars'), value, 'vars') for value in [False, 0, '', [], [['BANK', 'x']]]),
    *((('machines', 'testbox', 'vars', 'BANK'), value, 'vars.BANK') for value in [None, 1, False, [], {}]),
]


@pytest.mark.parametrize('path,value,field', _CONFIG_SHAPES)
def test_malformed_current_config_is_classified_before_local_preparation(fleet, monkeypatch, path, value, field):
    from secrets_kit import converge as subject
    config = json.loads(fleet.config_path.read_text())
    config['machines']['testbox']['vars'] = {'BANK': str(fleet.tmp / 'bank')}
    _set_declaration_field(config, path, value)
    fleet.config_path.write_text(json.dumps(config), encoding='utf-8')
    before = _owned_declaration_preimages(fleet)
    effects = []
    def forbidden(*args, **kwargs):
        effects.append('local preparation')
        raise AssertionError('malformed config reached local preparation')
    monkeypatch.setattr(subject, 'tighten_dir', forbidden)
    monkeypatch.setattr(subject.repo_mod, 'clone', forbidden)
    monkeypatch.setattr(subject.repo_mod, 'refresh', forbidden)

    result = _run(fleet)

    assert [failure.key for failure in result.failures] == [FAILURE_CONFIG]
    failure = result.failures[0]
    assert failure.ask_reason is None
    assert 'secrets.json' in failure.user_msg
    assert field in failure.user_msg
    assert field in failure.agent_msg
    assert result.written == result.removed == 0
    assert {path: path.read_bytes() for path in before} == before
    assert effects == []


@pytest.mark.parametrize('raw,diagnosis', [(b'\xff', 'UTF-8'), (b'{ bad', 'JSON'), (b'null', 'object'), (b'[]', 'object')])
@pytest.mark.parametrize('declaration', ['config', 'manifest'])
def test_encoded_or_top_level_declaration_failure_is_public_and_preserves_owned_files(fleet, raw, diagnosis, declaration):
    before = _owned_declaration_preimages(fleet)
    path = fleet.config_path if declaration == 'config' else fleet.manifest_path
    path.write_bytes(raw)

    result = _run(fleet)

    assert [failure.key for failure in result.failures] == [FAILURE_CONFIG]
    assert path.name in result.failures[0].user_msg
    assert diagnosis in result.failures[0].user_msg
    assert result.failures[0].ask_reason is None
    assert {path: path.read_bytes() for path in before} == before


_MANIFEST_SHAPES = [
    *((('recipient',), value, 'recipient') for value in [True, ['key'], {'key': 'x'}]),
    *((('profiles',), value, 'profiles') for value in [False, 0, '', [], [['home-admin', ['ha-token']]]]),
    *((('entries',), value, 'entries') for value in [False, 0, '', [], [['ha-token', {}]]]),
    *((('entries', 'ha-token'), value, 'ha-token') for value in [None, False, 0, '', []]),
    *((('entries', 'ha-token', 'blob'), value, 'blob') for value in [True, ['blob'], {'blob': 'x'}]),
    *((('profiles', 'home-admin'), value, 'home-admin') for value in [None, False, {}, [1], [[]], [{}]]),
    *((('entries', 'ha-token', 'dest'), value, 'dest') for value in [True, 1, ['path']]),
    *((('entries', 'ha-token', 'dest', branch), value, 'dest.' + branch) for branch in ['default', 'windows'] for value in [False, 0, [], {}]),
    (('entries', 'z-later-invalid'), {'blob': ['bad'], 'dest': '~/unused'}, 'z-later-invalid'),
]


@pytest.mark.parametrize('path,value,field', _MANIFEST_SHAPES)
def test_whole_manifest_shape_is_classified_before_crypto_or_ownership_effects(fleet, monkeypatch, path, value, field):
    from secrets_kit import converge as subject
    fleet.unlock()
    manifest = json.loads(fleet.manifest_path.read_text())
    if len(path) == 4 and path[2] == 'dest':
        spec = manifest['entries']['ha-token']['dest']
        manifest['entries']['ha-token']['dest'] = {'default': spec, 'windows': spec}
    _set_declaration_field(manifest, path, value)
    fleet.manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    before = _owned_declaration_preimages(fleet)
    crypto = []
    real = subject.decrypt_with_identity
    def record(*args, **kwargs):
        crypto.append('decrypt')
        return real(*args, **kwargs)
    monkeypatch.setattr(subject, 'decrypt_with_identity', record)

    result = _run(fleet)

    assert [failure.key for failure in result.failures] == [FAILURE_CONFIG], (
        f'accepted malformed {field}: written={result.written}, removed={result.removed}, crypto={crypto}'
    )
    failure = result.failures[0]
    assert failure.ask_reason is None
    assert 'manifest.json' in failure.user_msg
    assert field in failure.user_msg
    assert field in failure.agent_msg
    assert result.written == result.removed == 0
    assert crypto == []
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize('row', [None, {}, {'profiles': None}, {'profiles': []}])
def test_listed_empty_or_null_machine_keeps_zero_selection_and_owned_orphan_removal(fleet, row):
    fleet.unlock()
    _run(fleet)
    config = json.loads(fleet.config_path.read_text())
    config['machines']['testbox'] = row
    fleet.config_path.write_text(json.dumps(config), encoding='utf-8')
    result = _run(fleet)
    assert result.failures == []
    assert result.skipped_reason is None
    assert result.removed == 1
    assert not (fleet.dest_root / 'ha-token.txt').exists()


@pytest.mark.parametrize('listed', [False, True])
def test_unused_malformed_machine_row_is_not_eagerly_consumed(fleet, monkeypatch, listed):
    fleet.unlock()
    config = json.loads(fleet.config_path.read_text())
    config['machines']['unused'] = ['malformed unused row']
    fleet.config_path.write_text(json.dumps(config), encoding='utf-8')
    if not listed:
        monkeypatch.setattr('secrets_kit.manifest.resolve_host', lambda: ['stranger'])
        fleet.manifest_path.write_bytes(b'not read on an unlisted host')
    result = _run(fleet)
    assert result.failures == []
    if listed:
        assert (fleet.dest_root / 'ha-token.txt').read_bytes() == b'token-value\n'
    else:
        assert result.skipped_reason == 'no profiles for this host'
        assert not (fleet.data_dir / 'state.json').exists()


def test_unselected_destination_expansion_stays_deferred(fleet):
    fleet.unlock()
    manifest = json.loads(fleet.manifest_path.read_text())
    manifest['entries']['rolfing']['dest'] = '${UNUSED_UNRESOLVABLE_VARIABLE}/x'
    fleet.manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    result = _run(fleet)
    assert result.failures == []
    assert result.written == 1


def test_unrelated_runtime_error_is_not_classified_as_a_declaration(fleet, monkeypatch):
    from secrets_kit import converge as subject
    def broken(path):
        raise RuntimeError('unrelated programming defect')
    monkeypatch.setattr(subject.Config, 'load', broken)
    with pytest.raises(RuntimeError, match='unrelated programming defect'):
        _run(fleet)


@pytest.mark.parametrize('declaration', ['config', 'manifest'])
def test_actual_status_main_prints_the_specific_declaration_failure(fleet, monkeypatch, capsys, declaration):
    import importlib.util
    monkeypatch.setattr(sys, 'path', sys.path.copy())
    cli_path = Path(__file__).resolve().parents[2] / 'plugins/secrets-kit/scripts/secrets_kit_cli.py'
    spec = importlib.util.spec_from_file_location('shape_status_cli', cli_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, 'CONFIG_PATH', fleet.config_path)
    monkeypatch.setattr(cli, 'DATA_DIR', fleet.data_dir)
    path = fleet.config_path if declaration == 'config' else fleet.manifest_path
    path.write_bytes(b'\xff')
    before = _owned_declaration_preimages(fleet)
    assert cli.main(['status']) == 1
    output = capsys.readouterr().out
    assert FAILURE_CONFIG in output
    assert path.name in output and 'UTF-8' in output
    assert {path: path.read_bytes() for path in before} == before


def test_actual_remove_main_refuses_malformed_global_config_before_authoring(fleet, monkeypatch, capsys):
    import importlib.util
    from secrets_kit import guard, repo as repo_mod
    monkeypatch.setattr(sys, 'path', sys.path.copy())
    cli_path = Path(__file__).resolve().parents[2] / 'plugins/secrets-kit/scripts/secrets_kit_cli.py'
    spec = importlib.util.spec_from_file_location('shape_remove_cli', cli_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, 'CONFIG_PATH', fleet.config_path)
    monkeypatch.setattr(cli, 'DATA_DIR', fleet.data_dir)
    config = json.loads(fleet.config_path.read_text())
    config['repo'] = ['bad repo type']
    fleet.config_path.write_text(json.dumps(config), encoding='utf-8')
    before = _owned_declaration_preimages(fleet)
    effects = []
    def forbidden(*args, **kwargs):
        effects.append('authoring')
        raise AssertionError('malformed global config reached authoring')
    monkeypatch.setattr(repo_mod, 'clone', forbidden)
    monkeypatch.setattr(repo_mod, 'sync', forbidden)
    monkeypatch.setattr(guard, 'require_guard', forbidden)
    assert cli.main(['remove', 'dummy']) == 1
    output = capsys.readouterr().err
    assert 'secrets.json' in output and 'repo' in output and 'string' in output
    assert effects == []
    assert {path: path.read_bytes() for path in before} == before


# Cache fixtures come from actual materialization, including ownership records.
def _seed_cache_state(fleet, *, both=False):
    if both:
        config = json.loads(fleet.config_path.read_text())
        config['machines']['testbox']['profiles'] = ['home-admin', 'rolfing']
        fleet.config_path.write_text(json.dumps(config), encoding='utf-8')
    fleet.unlock()
    result = _run(fleet)
    assert result.failures == []
    path = paths_for(fleet.data_dir)['state']
    return path, json.loads(path.read_text())


def _cache_selection(fleet, profiles):
    config = json.loads(fleet.config_path.read_text())
    config['machines']['testbox']['profiles'] = profiles
    fleet.config_path.write_text(json.dumps(config), encoding='utf-8')


def _observe_cache_work(monkeypatch):
    from secrets_kit import converge as subject
    counts = {'decrypt': 0, 'tighten': 0}
    for name, key in [('decrypt_with_identity', 'decrypt'), ('tighten', 'tighten')]:
        real = getattr(subject, name)
        def record(*args, _real=real, _key=key, **kwargs):
            counts[_key] += 1
            return _real(*args, **kwargs)
        monkeypatch.setattr(subject, name, record)
    return counts


class TestCacheRecoveryPublic:
    @pytest.mark.parametrize('damage', ['invalid-utf8', 'invalid-json', 'missing'])
    def test_unreadable_cache_takes_normal_selected_materialization(self, fleet, monkeypatch, damage):
        path, _ = _seed_cache_state(fleet)
        if damage == 'missing':
            path.unlink()
        else:
            path.write_bytes(b'\xff' if damage == 'invalid-utf8' else b'{ bad')
        counts = _observe_cache_work(monkeypatch)
        result = _run(fleet)
        assert result.failures == [] and result.notes == []
        assert result.written == 1
        assert counts['decrypt'] == 1
        assert (fleet.dest_root / 'ha-token.txt').read_bytes() == b'token-value\n'
        row = State.load(path).get('ha-token')
        assert row['dest'] == str(fleet.dest_root / 'ha-token.txt')
        assert isinstance(row['blob_sha256'], str) and row['blob_sha256']
        assert isinstance(row['dest_sha256'], str) and row['dest_sha256']

    @pytest.mark.parametrize('data', [None, False, 0, 'bad', [], {}, {'entries': None}, {'entries': False}, {'entries': 0}, {'entries': ''}, {'entries': []}, {'entries': {}}])
    def test_empty_or_wrong_cache_envelope_is_an_existing_recovery_control(self, fleet, monkeypatch, data):
        path, _ = _seed_cache_state(fleet)
        path.write_text(json.dumps(data), encoding='utf-8')
        counts = _observe_cache_work(monkeypatch)
        result = _run(fleet)
        assert result.failures == [] and result.notes == []
        assert result.written == 1 and counts['decrypt'] == 1
        assert (fleet.dest_root / 'ha-token.txt').read_bytes() == b'token-value\n'
        assert State.load(path).get('ha-token')['dest'].endswith('ha-token.txt')

    @pytest.mark.parametrize('bad_row', [None, False, 0, '', []])
    def test_mixed_nonobject_rows_preserve_the_usable_selected_fastpath(self, fleet, monkeypatch, bad_row):
        path, data = _seed_cache_state(fleet)
        data['entries']['bad-orphan'] = bad_row
        path.write_text(json.dumps(data), encoding='utf-8')
        counts = _observe_cache_work(monkeypatch)
        result = _run(fleet)
        assert result.failures == [] and result.notes == []
        assert result.ok == 1 and result.written == 0 and counts['decrypt'] == 0
        assert set(State.load(path).rows) == {'ha-token'}
        assert (fleet.dest_root / 'ha-token.txt').read_bytes() == b'token-value\n'

    @pytest.mark.parametrize('value', [True, 7, ['bad'], {'path': 'bad'}, 'nul-prefix', None, False, 0, '', [], {}, 'absent'])
    def test_bad_orphan_dest_never_invents_an_unlink_but_good_ownership_removes(self, fleet, value):
        path, data = _seed_cache_state(fleet, both=True)
        _cache_selection(fleet, ['home-admin'])
        protected = fleet.tmp / 'protected-prefix.txt'
        protected.write_bytes(b'protected unrelated dummy bytes')
        row = dict(data['entries']['rolfing'])
        if value == 'absent':
            row.pop('dest')
        else:
            row['dest'] = str(protected) + '\x00tail' if value == 'nul-prefix' else value
        data['entries']['bad-orphan'] = row
        path.write_text(json.dumps(data), encoding='utf-8')
        result = _run(fleet)
        assert result.failures == [] and result.notes == []
        assert result.removed == 1
        assert not (fleet.dest_root / 'rolfing.txt').exists()
        assert protected.read_bytes() == b'protected unrelated dummy bytes'
        assert (fleet.dest_root / 'ha-token.txt').read_bytes() == b'token-value\n'
        assert set(State.load(path).rows) == {'ha-token'}

    @pytest.mark.parametrize('damage', ['blob_sha256', 'dest_sha256', 'mode', 'dest-only'])
    def test_usable_orphan_destination_survives_bad_or_missing_comparison_fields(self, fleet, damage):
        path, data = _seed_cache_state(fleet)
        _cache_selection(fleet, [])
        row = data['entries']['ha-token']
        if damage == 'dest-only':
            data['entries']['ha-token'] = {'dest': row['dest']}
        else:
            row[damage] = {'bad': True}
        path.write_text(json.dumps(data), encoding='utf-8')
        loaded = State.load(path).get('ha-token')
        assert loaded['dest'] == str(fleet.dest_root / 'ha-token.txt')
        result = _run(fleet)
        assert result.failures == [] and result.notes == []
        assert result.removed == 1
        assert not (fleet.dest_root / 'ha-token.txt').exists()

    @pytest.mark.parametrize('field', ['blob_sha256', 'dest_sha256'])
    @pytest.mark.parametrize('value', [None, 0, [], 'NOT-A-HEX-DIGEST'])
    def test_bad_selected_comparison_field_takes_existing_content_miss(self, fleet, monkeypatch, field, value):
        path, data = _seed_cache_state(fleet)
        data['entries']['ha-token'][field] = value
        path.write_text(json.dumps(data), encoding='utf-8')
        counts = _observe_cache_work(monkeypatch)
        result = _run(fleet)
        assert result.failures == [] and result.notes == []
        assert result.written == 1 and counts['decrypt'] == 1
        assert (fleet.dest_root / 'ha-token.txt').read_bytes() == b'token-value\n'
        assert State.load(path).get('ha-token')['dest'] == str(fleet.dest_root / 'ha-token.txt')

    @pytest.mark.parametrize('mode', [0, True, None, '', [], {}])
    def test_bad_cached_mode_with_matching_hashes_keeps_no_decrypt_repair(self, fleet, monkeypatch, mode):
        path, data = _seed_cache_state(fleet)
        data['entries']['ha-token']['mode'] = mode
        path.write_text(json.dumps(data), encoding='utf-8')
        counts = _observe_cache_work(monkeypatch)
        result = _run(fleet)
        assert result.failures == [] and result.notes == []
        assert result.written == 1
        assert counts == {'decrypt': 0, 'tighten': 1}
        row = State.load(path).get('ha-token')
        assert row['mode'] == '0600' and row['dest'] == str(fleet.dest_root / 'ha-token.txt')

    @pytest.mark.parametrize('dest', ['missing', True, 'nul'])
    def test_legacy_or_bad_dest_retains_hash_fastpath_without_backfilling_old_ownership(self, fleet, monkeypatch, dest):
        path, data = _seed_cache_state(fleet)
        row = data['entries']['ha-token']
        if dest == 'missing':
            row.pop('dest')
        else:
            row['dest'] = True if dest is True else str(fleet.dest_root / 'ha-token.txt') + '\x00tail'
        path.write_text(json.dumps(data), encoding='utf-8')
        counts = _observe_cache_work(monkeypatch)
        result = _run(fleet)
        assert result.failures == [] and result.notes == []
        assert result.ok == 1 and result.written == 0 and counts['decrypt'] == 0
        _cache_selection(fleet, [])
        result = _run(fleet)
        assert result.failures == [] and result.notes == []
        assert result.removed == 0
        assert (fleet.dest_root / 'ha-token.txt').read_bytes() == b'token-value\n'
        assert State.load(path).rows == {}

    @pytest.mark.parametrize('ledger', ['usable', 'missing', 'invalid-json', 'invalid-utf8', 'missing-dest'])
    def test_lost_unselected_ownership_leaves_old_file_without_discovery(self, fleet, ledger):
        path, data = _seed_cache_state(fleet)
        _cache_selection(fleet, [])
        if ledger == 'missing':
            path.unlink()
        elif ledger in ['invalid-json', 'invalid-utf8']:
            path.write_bytes(b'{ bad' if ledger == 'invalid-json' else b'\xff')
        elif ledger == 'missing-dest':
            data['entries']['ha-token'].pop('dest')
            path.write_text(json.dumps(data), encoding='utf-8')
        result = _run(fleet)
        assert result.failures == [] and result.notes == []
        assert result.removed == (1 if ledger == 'usable' else 0)
        assert (fleet.dest_root / 'ha-token.txt').exists() == (ledger != 'usable')

    def test_unused_metadata_does_not_invalidate_hits_or_deletion_ownership(self, fleet, monkeypatch):
        path, data = _seed_cache_state(fleet)
        row = data['entries']['ha-token']
        row.update(written_at={'not': 'a timestamp'}, unused={'nested': [False, None]})
        path.write_text(json.dumps(data), encoding='utf-8')
        counts = _observe_cache_work(monkeypatch)
        result = _run(fleet)
        assert result.failures == [] and result.notes == []
        assert result.ok == 1 and counts['decrypt'] == 0
        assert State.load(path).get('ha-token') == row
        _cache_selection(fleet, [])
        result = _run(fleet)
        assert result.removed == 1 and result.failures == []

    def test_loader_preserves_exact_independent_fields_without_path_or_digest_grammar(self, fleet):
        path, data = _seed_cache_state(fleet)
        row = data['entries']['ha-token']
        row.update(dest=' relative \u79d8 path ', blob_sha256='UPPER-NONHEX', mode='NOT-OCTAL', dest_sha256=7, written_at=[])
        path.write_text(json.dumps(data), encoding='utf-8')
        loaded = State.load(path).get('ha-token')
        assert loaded['dest'] == row['dest']
        assert loaded['blob_sha256'] == 'UPPER-NONHEX'
        assert loaded['mode'] == 'NOT-OCTAL'
        assert loaded['written_at'] == []
        assert 'dest_sha256' not in loaded


@pytest.mark.parametrize('spelling', ['$A', '${A}'])
@pytest.mark.parametrize('preexisting', [False, True])
def test_stalled_destination_refuses_before_wrong_dummy_write(fleet, monkeypatch, spelling, preexisting):
    from secrets_kit import converge as subject, repo as repo_mod
    from secrets_kit.converge import FAILURE_ENTRY
    parent = fleet.tmp / 'stalled destination'
    parent.mkdir()
    placeholder = parent / spelling
    before = b'controlled placeholder bytes' if preexisting else None
    if preexisting:
        placeholder.write_bytes(before)
    assert not repo_mod.dest_exposure(placeholder).exposed
    manifest = json.loads(fleet.manifest_path.read_text())
    manifest['entries']['ha-token']['dest'] = str(placeholder)
    fleet.manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    config = json.loads(fleet.config_path.read_text())
    config['vars']['A'] = spelling
    fleet.config_path.write_text(json.dumps(config), encoding='utf-8')
    fleet.unlock()
    calls = []
    for name in ['decrypt_with_identity', '_atomic_write']:
        real = getattr(subject, name)
        def observe(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)
        monkeypatch.setattr(subject, name, observe)
    result = _run(fleet)
    observed = {'written': result.written, 'calls': calls,
                'placeholder_bytes': placeholder.read_bytes() if placeholder.exists() else None}
    assert observed == {'written': 0, 'calls': [], 'placeholder_bytes': before}
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.key == FAILURE_ENTRY and failure.ask_reason is None
    assert 'ha-token' in failure.agent_msg and 'stall' in failure.agent_msg.lower()
    assert State.load(paths_for(fleet.data_dir)['state']).get('ha-token') == {}


def test_stalled_entry_does_not_block_another_selected_entry(fleet):
    from secrets_kit.converge import FAILURE_ENTRY
    parent = fleet.tmp / 'stalled alongside healthy'
    parent.mkdir()
    placeholder = parent / '$A'
    manifest = json.loads(fleet.manifest_path.read_text())
    manifest['entries']['ha-token']['dest'] = str(placeholder)
    fleet.manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    config = json.loads(fleet.config_path.read_text())
    config['vars']['A'] = '$A'
    config['machines']['testbox']['profiles'] = ['home-admin', 'rolfing']
    fleet.config_path.write_text(json.dumps(config), encoding='utf-8')
    fleet.unlock()
    result = _run(fleet)
    assert result.written == 1 and not placeholder.exists()
    assert (fleet.dest_root / 'rolfing.txt').read_bytes() == b'rolfing-value\n'
    assert len(result.failures) == 1 and result.failures[0].key == FAILURE_ENTRY
    assert result.failures[0].ask_reason is None and 'ha-token' in result.failures[0].agent_msg
    assert set(State.load(paths_for(fleet.data_dir)['state']).rows) == {'rolfing'}


def test_public_convergence_keeps_valid_nested_variable_destinations(fleet):
    config = json.loads(fleet.config_path.read_text())
    config['vars']['BANK'] = '$PARENT/bank'
    config['vars']['PARENT'] = str(fleet.tmp)
    fleet.config_path.write_text(json.dumps(config), encoding='utf-8')
    fleet.unlock()
    result = _run(fleet)
    assert result.failures == [] and result.written == 1
    assert (fleet.dest_root / 'ha-token.txt').read_bytes() == b'token-value\n'
    assert State.load(paths_for(fleet.data_dir)['state']).get('ha-token')['dest'] == str(fleet.dest_root / 'ha-token.txt')
