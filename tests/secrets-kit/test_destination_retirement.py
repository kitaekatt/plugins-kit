"""Actual ownership moves, durable retirements, handovers and consumers."""

import errno
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from secrets_kit import DecryptError
from secrets_kit import converge as convergence
from secrets_kit import perms
from secrets_kit.state import State, sha256_bytes
from test_destination_ownership import _manifest, _observe, _profiles, _rename, _seed
from test_destination_leaf import _consumer_tree
from test_orphan_retry import _consumer


@pytest.fixture(autouse=True)
def actual_subject_origins():
    root = Path(__file__).resolve().parents[2]
    for module in [convergence, perms, sys.modules[State.__module__]]:
        assert Path(module.__file__).resolve().parent == root / 'plugins/secrets-kit/lib/secrets_kit'


def _pending(path):
    field = json.loads(path.read_text()).get('pending_retirements', {'version': 1, 'items': []})
    return field.get('items', []) if isinstance(field, dict) else []


def _move(fleet, *, variable=False, equal=False):
    new_root = fleet.tmp / 'new bank' / 'secrets'
    new_root.mkdir(parents=True)
    new = new_root / 'ha-token.txt'
    if variable:
        raw = json.loads(fleet.config_path.read_text())
        raw['vars']['BANK'] = str(new_root.parent)
        fleet.config_path.write_text(json.dumps(raw), encoding='utf-8')
    else:
        _manifest(fleet, lambda raw: raw['entries']['ha-token'].update(dest=str(new)))
    if equal:
        new.write_bytes(b'token-value\n')
        new.chmod(0o644)
    return new


@pytest.mark.parametrize('variable', [False, True])
@pytest.mark.parametrize('equal', [False, True])
def test_actual_move_publishes_new_ownership_retires_old_and_removes_final_owner(fleet, monkeypatch, variable, equal):
    old, state_path = _seed(fleet)
    new = _move(fleet, variable=variable, equal=equal)
    record = _observe(monkeypatch)
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    first_row = State.load(state_path).get('ha-token')
    first_old_exists, first_new_bytes = old.exists(), new.read_bytes()
    first_decrypts, first_writes = len(record['decrypt']), list(record['write'])
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    later_decrypts = len(record['decrypt']) - first_decrypts
    _profiles(fleet, [])
    final = convergence.converge(fleet.config_path, fleet.data_dir)
    assert first.failures == second.failures == final.failures == []
    assert first.written == first.removed == 1 and not first_old_exists
    assert first_new_bytes == b'token-value\n' and first_row['dest'] == str(new)
    assert first_decrypts == 1 and first_writes == [str(new)]
    assert second.ok == 1 and second.written == second.removed == later_decrypts == 0
    assert final.removed == 1 and not old.exists() and not new.exists()
    assert State.load(state_path).rows == {} and _pending(state_path) == []


@pytest.mark.parametrize('changed', [False, True])
def test_actual_rename_handover_never_unlinks_desired_slot_and_final_owner_retires(fleet, monkeypatch, changed):
    target, state_path = _seed(fleet)
    _rename(fleet, target, blob='blobs/rolfing.txt.age' if changed else 'blobs/ha-token.txt.age')
    record = _observe(monkeypatch)
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    first_bytes, first_rows = target.read_bytes(), State.load(state_path).rows
    first_unlinks = record['unlink'].count(str(target))
    _profiles(fleet, [])
    final = convergence.converge(fleet.config_path, fleet.data_dir)
    assert first.failures == final.failures == [] and first.written == 1 and first.removed == 0
    assert first_bytes == (b'rolfing-value\n' if changed else b'token-value\n')
    assert first_rows.keys() == {'replacement'} and first_unlinks == 0
    assert final.removed == 1 and not target.exists() and State.load(state_path).rows == {}


@pytest.mark.parametrize('fault', ['decrypt', 'protect', 'replace'])
def test_actual_failed_move_preserves_prior_copy_and_later_retry_completes(fleet, monkeypatch, fault):
    old, state_path = _seed(fleet)
    old_row = State.load(state_path).get('ha-token')
    new = _move(fleet)
    with monkeypatch.context() as selective:
        if fault == 'decrypt':
            selective.setattr(convergence, 'decrypt_with_identity', lambda *args: (_ for _ in ()).throw(DecryptError('dummy move decrypt refusal')))
        elif fault == 'protect':
            real = perms.tighten
            def protecting(path, mode):
                if Path(path).parent == new.parent:
                    raise PermissionError('dummy temporary protection refusal')
                return real(path, mode)
            selective.setattr(perms, 'tighten', protecting)
        else:
            real = os.replace
            def replacing(source, destination):
                if Path(destination) == new:
                    raise OSError(errno.EIO, 'dummy final replace refusal')
                return real(source, destination)
            selective.setattr(os, 'replace', replacing)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
        first_row = State.load(state_path).get('ha-token')
        first_old_bytes, first_new_exists = old.read_bytes(), new.exists()
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    second_old_exists, second_new_bytes = old.exists(), new.read_bytes()
    _profiles(fleet, [])
    final = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == 1 and first.removed == 0 and first_row == old_row
    assert first_old_bytes == b'token-value\n' and not first_new_exists
    assert second.failures == [] and second.written == second.removed == 1
    assert not second_old_exists and second_new_bytes == b'token-value\n'
    assert final.failures == [] and final.removed == 1 and not new.exists()


@pytest.mark.parametrize('fault', ['permission', 'eio'])
def test_actual_failed_old_unlink_is_checkpointed_reloaded_and_retried(fleet, monkeypatch, fault):
    old, state_path = _seed(fleet)
    new = _move(fleet)
    real = Path.unlink
    attempts = []
    def unlinking(path, *args, **kwargs):
        if path == old:
            attempts.append(str(path))
            raise OSError(errno.EACCES if fault == 'permission' else errno.EIO, 'dummy old unlink refusal')
        return real(path, *args, **kwargs)
    with monkeypatch.context() as selective:
        selective.setattr(Path, 'unlink', unlinking)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
        first_pending = _pending(state_path)
        first_row = State.load(state_path).get('ha-token')
        first_old_exists, first_new_bytes = old.exists(), new.read_bytes()
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    second_pending, second_old_exists = _pending(state_path), old.exists()
    third = convergence.converge(fleet.config_path, fleet.data_dir)
    _profiles(fleet, [])
    final = convergence.converge(fleet.config_path, fleet.data_dir)
    print('RETIREMENT_RETRY_TRACE ' + json.dumps({'firstFailureCount': len(first.failures), 'pendingAfterFirst': len(first_pending), 'oldAfterFirst': first_old_exists, 'oldAfterRetry': second_old_exists, 'finalOldExists': old.exists()}))
    assert len(first.failures) == 1 and first.written == 1 and first.removed == 0
    assert len(attempts) == 1 and len(first_pending) == 1 and first_old_exists
    assert first_pending[0]['dest'] == str(old) and first_pending[0]['dest_sha256'] == sha256_bytes(b'token-value\n')
    assert first_row['dest'] == str(new) and first_new_bytes == b'token-value\n'
    assert second.failures == [] and second.ok == second.removed == 1 and not second_old_exists
    assert second_pending == [] and third.failures == [] and third.removed == 0
    assert final.failures == [] and final.removed == 1 and not new.exists() and not old.exists()


@pytest.mark.parametrize('damage', ['changed', 'link'])
def test_actual_changed_old_slot_is_retained_after_successful_move(fleet, damage):
    old, state_path = _seed(fleet)
    if damage == 'changed':
        old.write_bytes(b'changed local old copy')
    else:
        old.unlink()
        referent = fleet.tmp / 'outside equal referent'
        referent.write_bytes(b'token-value\n')
        try:
            old.symlink_to(referent)
        except OSError as error:
            pytest.skip(f'controlled symlink unavailable: {error}')
    new = _move(fleet)
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    first_pending = _pending(state_path)
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == len(second.failures) == 1
    assert first.written == 1 and first.removed == second.removed == 0 and second.ok == 1
    assert len(first_pending) == 1 and _pending(state_path) == first_pending
    assert new.read_bytes() == b'token-value\n'
    assert first.failures[0].ask_reason is None and str(old) in first.failures[0].agent_msg
    if damage == 'changed':
        assert old.read_bytes() == b'changed local old copy'
    else:
        assert old.is_symlink() and referent.read_bytes() == b'token-value\n'


@pytest.mark.parametrize('stage', ['checkpoint', 'final'])
def test_actual_state_save_failure_prevents_unsafe_cleanup_or_retries_absence(fleet, monkeypatch, stage):
    old, state_path = _seed(fleet)
    new = _move(fleet)
    real = State.save
    calls = []
    def saving(state):
        calls.append(True)
        if len(calls) == (1 if stage == 'checkpoint' else 2):
            raise OSError(errno.EIO, 'dummy ownership state save refusal')
        return real(state)
    with monkeypatch.context() as selective:
        selective.setattr(State, 'save', saving)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
        first_old_exists = old.exists()
        first_disk = json.loads(state_path.read_text())
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == 1 and first.failures[0].key == 'secrets_config' and first.failures[0].ask_reason is None
    assert 'state' in first.failures[0].agent_msg and str(state_path) in first.failures[0].agent_msg
    assert first.written == 1 and first_old_exists == (stage == 'checkpoint')
    if stage == 'checkpoint':
        assert first_disk['entries']['ha-token']['dest'] == str(old)
    else:
        assert len(first_disk['pending_retirements']['items']) == 1 and first.removed == 1
    assert second.failures == [] and not old.exists() and new.read_bytes() == b'token-value\n'
    assert _pending(state_path) == [] and State.load(state_path).get('ha-token')['dest'] == str(new)


@pytest.mark.parametrize('version', [True, False, 2, '1', None, [], {}])
def test_actual_unknown_state_version_preserves_ledger_and_all_destinations(fleet, version):
    old, state_path = _seed(fleet)
    new = _move(fleet)
    raw = json.loads(state_path.read_text());raw['version'] = version
    state_path.write_text(json.dumps(raw), encoding='utf-8')
    before = state_path.read_bytes()
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(result.failures) == 1 and result.failures[0].key == 'secrets_config' and result.failures[0].ask_reason is None
    assert state_path.read_bytes() == before and old.read_bytes() == b'token-value\n' and not new.exists()
    assert result.written == result.removed == 0


@pytest.mark.parametrize('field', [None, [], {'version': True, 'items': []}, {'version': 2, 'items': []}, {'version': 1, 'items': {}}, {'items': []}])
def test_actual_opaque_pending_format_refuses_move_and_survives_independent_save(fleet, field):
    old, state_path = _seed(fleet)
    new = _move(fleet)
    _profiles(fleet, ['home-admin', 'rolfing'])
    raw = json.loads(state_path.read_text());raw['pending_retirements'] = field
    state_path.write_text(json.dumps(raw), encoding='utf-8')
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    after = json.loads(state_path.read_text())
    assert len(result.failures) == 2 and result.written == 1 and result.removed == 0
    assert after['pending_retirements'] == field and after['entries']['ha-token']['dest'] == str(old)
    assert old.read_bytes() == b'token-value\n' and not new.exists()
    assert (fleet.dest_root / 'rolfing.txt').read_bytes() == b'rolfing-value\n'


@pytest.mark.parametrize('kind', ['status', 'bootstrap'])
def test_actual_consumers_publish_move_but_report_changed_old_copy(fleet, monkeypatch, capsys, kind):
    old, state_path = _seed(fleet)
    old.write_bytes(b'changed old consumer copy')
    new = _move(fleet)
    capsys.readouterr();call = _consumer(fleet, monkeypatch, kind, capsys)
    first, second = call(), call()
    assert new.read_bytes() == b'token-value\n' and old.read_bytes() == b'changed old consumer copy'
    assert len(_pending(state_path)) == 1
    if kind == 'status':
        assert first['code'] == second['code'] == 1
        assert str(old) in first['output'] and '1 written' in first['output'] and '1 failed' in first['output']
    else:
        assert len(first['ctx'].failures) == len(second['ctx'].failures) == 1
        message = first['ctx'].failures[0][1]
        assert str(old) in message['user_msg'] and 'ask_reason' not in message
        assert first['ctx'].oks == [] and '1 written' in first['ctx'].logs[0]


def test_actual_ordinary_matching_state_keeps_metadata_and_one_save(fleet, monkeypatch):
    target, state_path = _seed(fleet)
    state = State.load(state_path);state.rows['ha-token'].update(written_at=1, unknown={'nested': [False]});state.save()
    old = State.load(state_path).get('ha-token')
    saves = [];real = State.save
    def saving(state):
        saves.append(True);return real(state)
    monkeypatch.setattr(State, 'save', saving)
    monkeypatch.setattr(convergence, 'decrypt_with_identity', lambda *args: pytest.fail('ordinary same-slot hit must not decrypt'))
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert result.failures == [] and result.ok == 1 and result.written == result.removed == 0
    assert len(saves) == 1 and State.load(state_path).get('ha-token') == old and target.read_bytes() == b'token-value\n'


def _fail_old_unlink(monkeypatch, old):
    real = Path.unlink
    def unlinking(path, *args, **kwargs):
        if path == old:
            raise PermissionError('dummy pending old unlink refusal')
        return real(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlinking)


def test_actual_fresh_process_restart_retries_only_persisted_retirement(fleet, monkeypatch):
    old, state_path = _seed(fleet)
    new = _move(fleet)
    with monkeypatch.context() as selective:
        _fail_old_unlink(selective, old)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
        first_pending = _pending(state_path)
    root = Path(__file__).resolve().parents[2]
    script = '''import json,sys
from pathlib import Path
root,config,data=map(Path,sys.argv[1:]);sys.path.insert(0,str(root/'plugins/secrets-kit/lib'))
from secrets_kit import converge as c
from secrets_kit import manifest as m
from secrets_kit.state import State
assert Path(c.__file__).resolve().parent==root/'plugins/secrets-kit/lib/secrets_kit'
m.resolve_host=lambda:['testbox'];c.repo_mod.refresh=lambda *a,**k:None
c.repo_mod.is_clone=lambda p:Path(p).is_dir()
c.repo_mod.require_repo_binding=lambda clone_dir,declared_repo:None
def decrypt(identity,blob):
    payload=Path(blob).read_bytes();assert payload.startswith(b'AGE-FAKE:')
    return payload.partition(b'\\n')[2]
c.decrypt_with_identity=decrypt
result=c.converge(config,data)
print(json.dumps({'ok':result.ok,'written':result.written,'removed':result.removed,'failed':result.failed,'rows':sorted(State.load(data/'state.json').rows)}))
'''
    env = os.environ.copy();env['PYTHONDONTWRITEBYTECODE'] = '1';env.pop('PYTHONPATH', None)
    completed = subprocess.run([sys.executable, '-c', script, str(root), str(fleet.config_path), str(fleet.data_dir)], env=env, check=True, capture_output=True, text=True)
    restarted = json.loads(completed.stdout)
    _profiles(fleet, [])
    final = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == 1 and len(first_pending) == 1
    assert restarted == {'ok': 1, 'written': 0, 'removed': 1, 'failed': 0, 'rows': ['ha-token']}
    assert not old.exists() and final.failures == [] and final.removed == 1 and not new.exists()
    assert State.load(state_path).rows == {} and _pending(state_path) == []


def test_actual_previous_pending_retirement_runs_despite_failed_later_move(fleet, monkeypatch):
    old, state_path = _seed(fleet)
    new = _move(fleet)
    with monkeypatch.context() as selective:
        _fail_old_unlink(selective, old)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
    third = fleet.dest_root / 'third.txt'
    _manifest(fleet, lambda raw: raw['entries']['ha-token'].update(dest=str(third)))
    with monkeypatch.context() as selective:
        selective.setattr(convergence, 'decrypt_with_identity', lambda *args: (_ for _ in ()).throw(DecryptError('dummy later move refusal')))
        second = convergence.converge(fleet.config_path, fleet.data_dir)
        second_old_exists, second_row = old.exists(), State.load(state_path).get('ha-token')
        second_pending = _pending(state_path)
    retry = convergence.converge(fleet.config_path, fleet.data_dir)
    _profiles(fleet, [])
    final = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == len(second.failures) == 1
    assert second.removed == 1 and not second_old_exists and second_pending == []
    assert second_row['dest'] == str(new)
    assert retry.failures == [] and retry.written == retry.removed == 1 and not new.exists()
    assert final.failures == [] and final.removed == 1 and not third.exists()


def test_actual_reused_old_name_preserves_independent_retirement_obligation(fleet, monkeypatch):
    old, state_path = _seed(fleet)
    new = _move(fleet)
    with monkeypatch.context() as selective:
        _fail_old_unlink(selective, old)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
    third = fleet.dest_root / 'reused-name.txt'
    def edit(raw):
        previous = raw['entries'].pop('ha-token')
        raw['entries']['replacement'] = dict(previous)
        raw['entries']['ha-token'] = dict(previous, dest=str(third), blob='blobs/rolfing.txt.age')
        raw['profiles']['home-admin'] = ['ha-token', 'replacement']
    _manifest(fleet, edit)
    with monkeypatch.context() as selective:
        _fail_old_unlink(selective, old)
        second = convergence.converge(fleet.config_path, fleet.data_dir)
        second_rows = State.load(state_path).rows
        second_pending = _pending(state_path)
    retry = convergence.converge(fleet.config_path, fleet.data_dir)
    _profiles(fleet, [])
    final = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == len(second.failures) == 1
    assert second_rows['ha-token']['dest'] == str(third) and second_rows['replacement']['dest'] == str(new)
    assert any(item.get('dest') == str(old) for item in second_pending)
    assert retry.failures == [] and retry.removed == 1 and not old.exists()
    assert final.failures == [] and final.removed == 2 and not new.exists() and not third.exists()
    assert _pending(state_path) == [] and State.load(state_path).rows == {}


def test_actual_pending_desired_handover_updates_evidence_without_cancelling_retry(fleet, monkeypatch):
    old, state_path = _seed(fleet)
    new = _move(fleet)
    with monkeypatch.context() as selective:
        _fail_old_unlink(selective, old)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
    def edit(raw):
        raw['entries']['replacement'] = dict(raw['entries']['ha-token'], dest=str(old), blob='blobs/rolfing.txt.age')
        raw['profiles']['home-admin'].append('replacement')
    _manifest(fleet, edit)
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    second_pending = _pending(state_path)
    second_bytes = old.read_bytes()
    _profiles(fleet, [])
    final = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == 1 and second.failures == [] and second.removed == 0
    assert second_bytes == b'rolfing-value\n'
    assert len(second_pending) == 1 and second_pending[0]['dest_sha256'] == sha256_bytes(second_bytes)
    assert final.failures == [] and final.removed == 2 and not old.exists() and not new.exists()
    assert _pending(state_path) == [] and State.load(state_path).rows == {}


@pytest.mark.parametrize('damage', ['missing-hash', 'missing-dest', 'missing-owner', 'nonobject', 'conflicting'])
def test_actual_malformed_pending_claim_retains_evidence_and_unrelated_file(fleet, damage):
    target, state_path = _seed(fleet)
    unrelated = fleet.tmp / 'prior pending copy'
    unrelated.write_bytes(b'owned old bytes')
    item = {'owner': 'old-owner', 'dest': str(unrelated), 'dest_sha256': sha256_bytes(b'owned old bytes'), 'unknown': {'preserve': [False]}}
    claims = [dict(item)]
    if damage == 'missing-hash':
        claims[0].pop('dest_sha256')
    elif damage == 'missing-dest':
        claims[0].pop('dest')
    elif damage == 'missing-owner':
        claims[0].pop('owner')
    elif damage == 'nonobject':
        claims = [True]
    else:
        claims.append(dict(item, dest_sha256=sha256_bytes(b'conflicting bytes')))
    raw = json.loads(state_path.read_text());raw['pending_retirements'] = {'version': 1, 'items': claims}
    state_path.write_text(json.dumps(raw), encoding='utf-8')
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == len(second.failures) == 1 and first.removed == second.removed == 0
    assert _pending(state_path) == claims and unrelated.read_bytes() == b'owned old bytes'
    assert target.read_bytes() == b'token-value\n' and first.ok == second.ok == 1


def test_actual_failed_mover_prior_slot_can_transfer_to_successful_selected_owner(fleet, monkeypatch):
    old, state_path = _seed(fleet)
    new = _move(fleet)
    def edit(raw):
        raw['entries']['replacement'] = dict(raw['entries']['ha-token'], dest=str(old), blob='blobs/rolfing.txt.age')
        raw['profiles']['home-admin'].append('replacement')
    _manifest(fleet, edit)
    real = convergence.decrypt_with_identity
    def decrypting(identity, blob):
        if Path(blob).name == 'ha-token.txt.age':
            raise DecryptError('dummy failed mover decrypt refusal')
        return real(identity, blob)
    with monkeypatch.context() as selective:
        selective.setattr(convergence, 'decrypt_with_identity', decrypting)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
        first_rows = State.load(state_path).rows
        first_bytes = old.read_bytes()
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    _profiles(fleet, [])
    final = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == 1 and first.written == 1 and first.removed == 0
    assert first_rows.keys() == {'replacement'} and first_bytes == b'rolfing-value\n'
    assert second.failures == [] and second.written == 1 and second.ok == 1 and second.removed == 0
    assert final.failures == [] and final.removed == 2 and not old.exists() and not new.exists()


def test_actual_supported_pending_unknown_metadata_survives_failed_unlink(fleet, monkeypatch):
    target, state_path = _seed(fleet)
    old = fleet.tmp / 'pending metadata copy'
    old.write_bytes(b'prior owned bytes')
    item = {'owner': 'prior-owner', 'dest': str(old), 'dest_sha256': sha256_bytes(b'prior owned bytes'), 'unknown': {'nested': [False, None]}}
    field = {'version': 1, 'items': [item], 'unknown': {'preserve': True}}
    raw = json.loads(state_path.read_text());raw['pending_retirements'] = field
    raw.pop('version')
    state_path.write_text(json.dumps(raw), encoding='utf-8')
    with monkeypatch.context() as selective:
        _fail_old_unlink(selective, old)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
        retained = json.loads(state_path.read_text())['pending_retirements']
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == 1 and first.ok == 1 and retained == field
    assert second.failures == [] and second.removed == 1 and not old.exists()
    assert json.loads(state_path.read_text())['pending_retirements'] == {'version': 1, 'items': [], 'unknown': {'preserve': True}}
    assert target.read_bytes() == b'token-value\n'


@pytest.mark.parametrize('repair', ['none', 'declared', 'drift'])
def test_actual_legacy_cache_only_hit_never_invents_retirement_ownership(fleet, monkeypatch, repair):
    if repair == 'drift' and perms.IS_WINDOWS:
        pytest.skip('POSIX drift result; native Windows effective ACL is separately verified')
    target, state_path = _seed(fleet)
    state = State.load(state_path);state.rows['ha-token'].pop('dest');state.save()
    if repair == 'declared':
        _manifest(fleet, lambda raw: raw['entries']['ha-token'].update(mode='0400'))
    elif repair == 'drift':
        target.chmod(0o644)
    monkeypatch.setattr(convergence, 'decrypt_with_identity', lambda *args: pytest.fail('legacy matching hashes preserve no-decrypt behavior'))
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    first_row = State.load(state_path).get('ha-token')
    _profiles(fleet, [])
    final = convergence.converge(fleet.config_path, fleet.data_dir)
    assert first.failures == [] and first.written == int(repair != 'none') and first.ok == int(repair == 'none')
    assert 'dest' not in first_row and len(final.failures) == 1 and final.removed == 0
    assert target.read_bytes() == b'token-value\n' and 'dest' not in State.load(state_path).get('ha-token')


def test_actual_git_withheld_handover_reserves_old_slot_then_retires_final_owner(fleet, git_template):
    root = _consumer_tree(fleet, git_template, ignored=True)
    target, state_path = _seed(fleet)
    prior = State.load(state_path).get('ha-token')
    _rename(fleet, target, blob='blobs/rolfing.txt.age')
    (root / '.gitignore').write_text('# ignore removed\n', encoding='utf-8')
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    first_exists, first_row = target.exists(), State.load(state_path).get('ha-token')
    (root / '.gitignore').write_text('/secrets/\n', encoding='utf-8')
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    second_bytes, second_rows = target.read_bytes(), State.load(state_path).rows
    _profiles(fleet, [])
    final = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == 1 and first.failures[0].key == 'secrets_dest'
    assert first.failures[0].ask_reason == 'info' and first.removed == first.written == 0
    assert first_exists and first_row == prior
    assert second.failures == [] and second.written == 1 and second.removed == 0
    assert second_bytes == b'rolfing-value\n' and second_rows.keys() == {'replacement'}
    assert final.failures == [] and final.removed == 1 and not target.exists()
