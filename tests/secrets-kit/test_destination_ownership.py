"""Public destination planning and conservative orphan ownership."""

import builtins
import errno
import json
import os
from pathlib import Path
import sys

import pytest

from secrets_kit import DecryptError, SecretsError
from secrets_kit import converge as convergence
from secrets_kit import repo as repository
from secrets_kit.state import State, sha256_bytes
from test_orphan_retry import _consumer


@pytest.fixture(autouse=True)
def actual_subject_origins():
    root = Path(__file__).resolve().parents[2]
    for module in [convergence, repository, sys.modules[State.__module__]]:
        assert Path(module.__file__).resolve().parent == root / 'plugins/secrets-kit/lib/secrets_kit'


def _manifest(fleet, edit):
    raw = json.loads(fleet.manifest_path.read_text())
    edit(raw)
    fleet.manifest_path.write_text(json.dumps(raw), encoding='utf-8')


def _profiles(fleet, profiles):
    raw = json.loads(fleet.config_path.read_text())
    raw['machines']['testbox']['profiles'] = profiles
    fleet.config_path.write_text(json.dumps(raw), encoding='utf-8')


def _seed(fleet):
    fleet.unlock()
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    assert first.failures == [] and first.written == 1
    path = convergence.paths_for(fleet.data_dir)['state']
    return fleet.dest_root / 'ha-token.txt', path


def _observe(monkeypatch):
    record = {'decrypt': [], 'write': [], 'unlink': []}
    decrypt, write, unlink = convergence.decrypt_with_identity, convergence._atomic_write, Path.unlink
    def decrypting(identity, blob):
        record['decrypt'].append(str(blob))
        return decrypt(identity, blob)
    def writing(dest, plaintext, mode):
        record['write'].append(str(dest))
        return write(dest, plaintext, mode)
    def unlinking(path, *args, **kwargs):
        record['unlink'].append(str(path))
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(convergence, 'decrypt_with_identity', decrypting)
    monkeypatch.setattr(convergence, '_atomic_write', writing)
    monkeypatch.setattr(Path, 'unlink', unlinking)
    return record


def _link(path, target):
    try:
        path.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as error:
        pytest.skip(f'controlled link creation unavailable: {error}')


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('spelling', ['same', 'relative', 'tilde', 'parent-link', 'parent-dot'])
def test_actual_collisions_refuse_both_execution_orders_and_preserve_survivor(fleet, monkeypatch, reverse, spelling):
    fleet.unlock()
    target = fleet.dest_root / 'collision.txt'
    alias = fleet.tmp / 'bank alias'
    _link(alias, fleet.dest_root.parent)
    alternate = target if spelling == 'same' else alias / 'secrets' / target.name
    if spelling == 'relative':
        monkeypatch.chdir(fleet.tmp)
        alternate = Path('bank/secrets/collision.txt')
    elif spelling == 'tilde':
        monkeypatch.setenv('HOME', str(fleet.tmp))
        alternate = Path('~/bank/secrets/collision.txt')
    if spelling == 'parent-dot':
        holder = fleet.tmp / 'nested' / 'aliases'
        holder.mkdir(parents=True)
        alias = holder / 'secrets alias'
        _link(alias, fleet.dest_root)
        alternate = alias / '..' / 'secrets' / target.name
        decoy = holder / 'secrets'
        decoy.mkdir()
        (decoy / target.name).write_bytes(b'lexical decoy')
    names = ['a-conflict', 'z-conflict']
    blobs = ['blobs/ha-token.txt.age', 'blobs/rolfing.txt.age']
    if reverse:
        blobs.reverse()
    def edit(raw):
        for name, blob, dest in zip(names, blobs, [target, alternate]):
            raw['entries'][name] = {'blob': blob, 'dest': str(dest), 'mode': '0600', 'allow_tracked_dest': True}
        raw['profiles']['conflict'] = names
    _manifest(fleet, edit)
    _profiles(fleet, ['home-admin', 'conflict'])
    record = _observe(monkeypatch)
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    print('OWNERSHIP_COLLISION_TRACE ' + json.dumps({'reverse': reverse, 'spelling': spelling, 'failureCount': len(result.failures), 'writes': len(record['write']), 'targetExists': target.exists()}))
    assert len(result.failures) == 2 and result.written == 1
    for failure in result.failures:
        assert failure.key == 'secrets_entry' and failure.ask_reason is None
        assert all(name in failure.agent_msg for name in names) and str(target) in failure.agent_msg
        assert 'collision' in failure.agent_msg.lower()
    assert record['write'] == [str(fleet.dest_root / 'ha-token.txt')]
    assert len(record['decrypt']) == 1 and not target.exists()
    assert (fleet.dest_root / 'ha-token.txt').read_bytes() == b'token-value\n'
    if spelling == 'parent-dot':
        assert (decoy / target.name).read_bytes() == b'lexical decoy'


def _rename(fleet, target, *, blob='blobs/ha-token.txt.age'):
    def edit(raw):
        old = raw['entries'].pop('ha-token')
        raw['entries']['replacement'] = dict(old, dest=str(target), blob=blob)
        raw['profiles']['home-admin'] = ['replacement']
    _manifest(fleet, edit)


@pytest.mark.parametrize('fault', ['blob', 'decrypt', 'write', 'inspection'])
def test_actual_failed_new_owner_reserves_prior_slot_and_keeps_restart_evidence(fleet, monkeypatch, fault):
    target, state_path = _seed(fleet)
    old = State.load(state_path).get('ha-token')
    _rename(fleet, target, blob='blobs/rolfing.txt.age')
    with monkeypatch.context() as selective:
        record = _observe(selective)
        if fault == 'blob':
            (fleet.blobs / 'rolfing.txt.age').unlink()
        elif fault == 'decrypt':
            selective.setattr(convergence, 'decrypt_with_identity', lambda *args: (_ for _ in ()).throw(DecryptError('dummy decrypt refusal')))
        elif fault == 'write':
            selective.setattr(convergence, '_atomic_write', lambda *args: (_ for _ in ()).throw(PermissionError('dummy write refusal')))
        else:
            real = os.lstat
            def inspecting(path, *args, **kwargs):
                if Path(path) == target and sys._getframe(1).f_globals.get('__name__') == convergence.__name__:
                    raise OSError(errno.EIO, 'dummy inspection refusal')
                return real(path, *args, **kwargs)
            selective.setattr(os, 'lstat', inspecting)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
        first_row = State.load(state_path).get('ha-token')
        first_exists = target.exists()
        first_bytes = target.read_bytes() if first_exists else None
    print('OWNERSHIP_RESERVED_TRACE ' + json.dumps({'fault': fault, 'oldRowRetained': first_row == old, 'targetExists': first_exists, 'targetUnlinks': record['unlink'].count(str(target))}))
    assert len(first.failures) == 1 and first.removed == 0
    assert str(target) not in record['unlink'] and first_row == old
    assert first_bytes == b'token-value\n'


def test_actual_cached_selected_slot_is_reserved_from_unselected_name(fleet):
    target, state_path = _seed(fleet)
    state = State.load(state_path)
    state.rows['previous-owner'] = dict(state.get('ha-token'))
    state.save()
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    first_exists = target.exists()
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert first.failures == second.failures == [] and first.removed == second.removed == 0
    assert first_exists and target.read_bytes() == b'token-value\n'
    assert (first.ok, first.written) == (1, 0) and (second.ok, second.written) == (1, 0)
    assert State.load(state_path).get('previous-owner')['dest'] == str(target)


@pytest.mark.parametrize('damage', ['changed', 'equal-link', 'dangling-link', 'directory', 'missing-hash', 'bad-hash'])
def test_actual_unselected_invalid_ownership_preserves_bytes_and_evidence(fleet, damage):
    target, state_path = _seed(fleet)
    if damage == 'changed':
        target.write_bytes(b'changed local content')
    elif damage in ['equal-link', 'dangling-link']:
        target.unlink()
        referent = fleet.tmp / 'referent'
        if damage == 'equal-link':
            referent.write_bytes(b'token-value\n')
        _link(target, referent)
    elif damage == 'directory':
        target.unlink()
        target.mkdir()
        (target / 'sentinel').write_bytes(b'unrelated directory')
    else:
        state = State.load(state_path)
        if damage == 'missing-hash':
            state.rows['ha-token'].pop('dest_sha256')
        else:
            state.rows['ha-token']['dest_sha256'] = {'invalid': True}
        state.save()
    old = State.load(state_path).get('ha-token')
    _profiles(fleet, [])
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == len(second.failures) == 1
    assert first.removed == second.removed == 0 and State.load(state_path).get('ha-token') == old
    for failure in first.failures + second.failures:
        assert failure.key == 'secrets_entry' and failure.ask_reason is None
        assert 'ha-token' in failure.user_msg and str(target) in failure.agent_msg
        assert 'retain' in failure.agent_msg.lower() and 'passphrase' not in failure.agent_msg.lower()
    if damage in ['equal-link', 'dangling-link']:
        assert target.is_symlink()
        if damage == 'equal-link':
            assert referent.read_bytes() == b'token-value\n'
    elif damage == 'directory':
        assert (target / 'sentinel').read_bytes() == b'unrelated directory'
    else:
        assert target.read_bytes() == (b'changed local content' if damage == 'changed' else b'token-value\n')


@pytest.mark.parametrize('fault', ['permission', 'eio'])
def test_actual_retirement_read_error_is_not_confirmed_absence(fleet, monkeypatch, fault):
    target, state_path = _seed(fleet)
    old = State.load(state_path).get('ha-token')
    _profiles(fleet, [])
    real = builtins.open
    def opening(path, *args, **kwargs):
        if isinstance(path, (str, Path)) and Path(path) == target:
            number = errno.EACCES if fault == 'permission' else errno.EIO
            raise OSError(number, 'dummy retirement read refusal')
        return real(path, *args, **kwargs)
    with monkeypatch.context() as selective:
        selective.setattr(builtins, 'open', opening)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
        first_row = State.load(state_path).get('ha-token')
        first_exists = target.exists()
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == 1 and first.removed == 0 and first_row == old and first_exists
    assert second.failures == [] and second.removed == 1 and not target.exists()
    assert State.load(state_path).rows == {}


@pytest.mark.parametrize('conflicting', [False, True])
def test_actual_duplicate_orphan_claims_share_one_attempt_or_refuse_ambiguity(fleet, monkeypatch, conflicting):
    target, state_path = _seed(fleet)
    state = State.load(state_path)
    state.rows['second-owner'] = dict(state.get('ha-token'))
    if conflicting:
        state.rows['second-owner']['dest_sha256'] = sha256_bytes(b'conflicting recorded bytes')
    state.save()
    _profiles(fleet, [])
    record = _observe(monkeypatch)
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert record['unlink'].count(str(target)) == (0 if conflicting else 1)
    assert result.removed == (0 if conflicting else 1)
    if conflicting:
        assert len(result.failures) == 1 and 'ambiguous' in result.failures[0].agent_msg.lower()
        assert target.read_bytes() == b'token-value\n' and len(State.load(state_path).rows) == 2
    else:
        assert result.failures == [] and not target.exists() and State.load(state_path).rows == {}


@pytest.mark.parametrize('fault', ['variable', 'resolve-os', 'resolve-loop'])
def test_actual_unknown_selected_slot_disables_cleanup_but_keeps_valid_survivor(fleet, monkeypatch, fault):
    target, state_path = _seed(fleet)
    _profiles(fleet, ['rolfing'])
    survivor = fleet.dest_root / 'survivor.txt'
    def edit(raw):
        raw['entries']['survivor'] = dict(raw['entries']['rolfing'], dest=str(survivor))
        raw['profiles']['rolfing'].append('survivor')
        raw['entries']['rolfing']['dest'] = '${UNRESOLVED_OWNERSHIP_VARIABLE}/token' if fault == 'variable' else str(fleet.tmp / 'fault parent' / 'token')
    _manifest(fleet, edit)
    monkeypatch.delenv('UNRESOLVED_OWNERSHIP_VARIABLE', raising=False)
    if fault != 'variable':
        real = Path.resolve
        def resolving(path, *args, **kwargs):
            if path == fleet.tmp / 'fault parent':
                if fault == 'resolve-loop':
                    raise RuntimeError('dummy ownership loop')
                raise OSError(errno.EIO, 'dummy ownership resolve refusal')
            return real(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'resolve', resolving)
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(result.failures) == 1 and result.failures[0].ask_reason is None
    assert result.written == 1 and result.removed == 0
    assert target.read_bytes() == b'token-value\n' and State.load(state_path).get('ha-token')['dest'] == str(target)
    assert survivor.read_bytes() == b'rolfing-value\n'


@pytest.mark.parametrize('kind', ['status', 'bootstrap'])
@pytest.mark.parametrize('operation', ['changed-orphan', 'collision'])
def test_actual_consumers_report_ownership_refusal_and_preserve_files(fleet, monkeypatch, capsys, kind, operation):
    target, state_path = _seed(fleet)
    if operation == 'changed-orphan':
        target.write_bytes(b'changed local content')
        _profiles(fleet, [])
    else:
        _manifest(fleet, lambda raw: (raw['entries']['rolfing'].update(dest=str(target)), raw['profiles']['home-admin'].append('rolfing')))
    capsys.readouterr()
    call = _consumer(fleet, monkeypatch, kind, capsys)
    first, second = call(), call()
    assert target.read_bytes() == (b'changed local content' if operation == 'changed-orphan' else b'token-value\n')
    assert State.load(state_path).get('ha-token')['dest'] == str(target)
    if kind == 'status':
        assert first['code'] == second['code'] == 1
        assert 'secrets_entry' in first['output'] and str(target) in first['output']
    else:
        assert len(first['ctx'].failures) == (1 if operation == 'changed-orphan' else 2)
        assert all('ask_reason' not in message for _, message in first['ctx'].failures)
        assert str(target) in first['ctx'].failures[0][1]['agent_msg']


@pytest.mark.parametrize('absent', [False, True])
def test_actual_regular_owned_retirement_and_missing_leaf_remain_compatible(fleet, absent):
    target, state_path = _seed(fleet)
    if absent:
        target.unlink()
    _profiles(fleet, [])
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert first.failures == second.failures == [] and first.removed == (0 if absent else 1)
    assert second.removed == 0 and not target.exists() and State.load(state_path).rows == {}


def test_actual_leaf_link_and_referent_are_distinct_selected_slots(fleet, monkeypatch):
    fleet.unlock()
    target = fleet.dest_root / 'ha-token.txt'
    referent = fleet.dest_root / 'rolfing.txt'
    referent.write_bytes(b'prior referent')
    _link(target, referent)
    _profiles(fleet, ['home-admin', 'rolfing'])
    _manifest(fleet, lambda raw: [entry.update(allow_tracked_dest=True) for entry in raw['entries'].values()])
    real_query = repository._query
    queries = []
    def querying(*args, **kwargs):
        queries.append(args)
        return real_query(*args, **kwargs)
    monkeypatch.setattr(repository, '_query', querying)
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    monkeypatch.setattr(convergence, 'decrypt_with_identity', lambda *args: pytest.fail('matching same-slot entries must not decrypt'))
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert first.failures == second.failures == [] and first.written == 2 and second.ok == 2
    assert not target.is_symlink() and target.read_bytes() == b'token-value\n'
    assert referent.read_bytes() == b'rolfing-value\n' and queries == []


@pytest.mark.parametrize('mode_change', [False, True])
def test_actual_collision_precedes_cached_permission_repair(fleet, monkeypatch, mode_change):
    target, state_path = _seed(fleet)
    old = State.load(state_path).get('ha-token')
    target.chmod(0o644)
    def edit(raw):
        raw['entries']['rolfing']['dest'] = str(target)
        raw['profiles']['home-admin'].append('rolfing')
        if mode_change:
            raw['entries']['ha-token']['mode'] = '0400'
    _manifest(fleet, edit)
    record = _observe(monkeypatch)
    calls = []
    real = convergence.tighten
    def tightening(path, mode):
        calls.append(str(path))
        return real(path, mode)
    monkeypatch.setattr(convergence, 'tighten', tightening)
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(result.failures) == 2 and result.ok == result.written == result.removed == 0
    assert record['decrypt'] == record['write'] == calls == []
    assert target.stat().st_mode & 0o777 == 0o644 and target.read_bytes() == b'token-value\n'
    assert State.load(state_path).get('ha-token') == old
