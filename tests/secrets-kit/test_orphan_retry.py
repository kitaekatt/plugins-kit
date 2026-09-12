"""Orphan cleanup failure, persisted ownership and actual consumer retry."""

import errno
import importlib.util
import json
from pathlib import Path

import pytest

from secrets_kit import converge as convergence
from secrets_kit.state import State
from test_init import _load_cli
from test_secrets_bootstrap import FakeCtx


def _seed_retirement(fleet):
    config = json.loads(fleet.config_path.read_text())
    config['machines']['testbox']['profiles'] = ['home-admin', 'rolfing']
    fleet.config_path.write_text(json.dumps(config), encoding='utf-8')
    fleet.unlock()
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    assert first.failures == [] and first.written == 2
    state_path = convergence.paths_for(fleet.data_dir)['state']
    state = State.load(state_path)
    state.rows['rolfing'].update(written_at=1, unused={'nested': ['preserve this row', False]})
    state.save()
    return fleet.dest_root / 'rolfing.txt', state_path


def _unselect(fleet, eligibility):
    if eligibility == 'manifest':
        raw = json.loads(fleet.manifest_path.read_text())
        raw['entries'].pop('rolfing')
        raw['profiles']['rolfing'] = []
        fleet.manifest_path.write_text(json.dumps(raw), encoding='utf-8')
    else:
        config = json.loads(fleet.config_path.read_text())
        config['machines']['testbox']['profiles'] = [] if eligibility == 'all' else ['home-admin']
        fleet.config_path.write_text(json.dumps(config), encoding='utf-8')


def _unlink_boundary(monkeypatch, target, *, fault=None):
    calls = []
    real = Path.unlink

    def unlinking(path, *args, **kwargs):
        if path == target:
            calls.append(path)
            if fault == 'permission':
                raise PermissionError('dummy orphan unlink refusal')
            if fault == 'eio':
                raise OSError(errno.EIO, 'dummy orphan EIO refusal')
        return real(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', unlinking)
    return calls


def _assert_failure(failure, target, fault='permission'):
    assert failure.key == 'secrets_entry' and failure.ask_reason is None
    assert 'rolfing' in failure.user_msg and str(target) in failure.user_msg
    assert 'rolfing' in failure.agent_msg and str(target) in failure.agent_msg
    assert ('dummy orphan unlink refusal' if fault == 'permission' else 'dummy orphan EIO refusal') in failure.agent_msg
    assert 'retain' in failure.agent_msg.lower() and 'later' in failure.agent_msg.lower()
    assert 'unlock' not in failure.agent_msg.lower() and 'passphrase' not in failure.agent_msg.lower()


def _three_pass_trace(fleet, monkeypatch, target, state_path, *, fault):
    """Execute every phase before assertions, including the old-code later loss."""
    with monkeypatch.context() as selective:
        first_calls = _unlink_boundary(selective, target, fault=fault)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
        first_row = State.load(state_path).rows.get('rolfing')
        first_bytes = target.read_bytes()
    with monkeypatch.context() as observing:
        later_calls = _unlink_boundary(observing, target)
        second = convergence.converge(fleet.config_path, fleet.data_dir)
        second_attempts = len(later_calls)
        second_row = State.load(state_path).rows.get('rolfing')
        second_exists = target.exists()
        later_calls.clear()
        third = convergence.converge(fleet.config_path, fleet.data_dir)
        third_attempts = len(later_calls)
        third_row = State.load(state_path).rows.get('rolfing')
        third_exists = target.exists()
    print('I08_TRACE ' + json.dumps({'firstFailureCount': len(first.failures), 'firstRowRetained': first_row is not None, 'firstAttempts': len(first_calls), 'secondAttempts': second_attempts, 'secondRemoved': second.removed, 'secondExists': second_exists, 'secondRowPresent': second_row is not None, 'thirdAttempts': third_attempts, 'thirdExists': third_exists, 'thirdRowPresent': third_row is not None}, sort_keys=True))
    return {'first': first, 'firstAttempts': len(first_calls), 'firstRow': first_row, 'firstBytes': first_bytes,
            'second': second, 'secondAttempts': second_attempts, 'secondRow': second_row, 'secondExists': second_exists,
            'third': third, 'thirdAttempts': third_attempts, 'thirdRow': third_row, 'thirdExists': third_exists}


@pytest.mark.parametrize('eligibility', ['profile', 'manifest'])
@pytest.mark.parametrize('fault', ['permission', 'eio'])
def test_actual_failed_retirement_survives_reload_and_independent_retry(fleet, monkeypatch, eligibility, fault):
    target, state_path = _seed_retirement(fleet)
    old = State.load(state_path).rows['rolfing']
    before = target.read_bytes()
    _unselect(fleet, eligibility)
    monkeypatch.setattr(convergence, 'decrypt_with_identity', lambda *args: pytest.fail('selected survivor is a matching hit'))
    trace = _three_pass_trace(fleet, monkeypatch, target, state_path, fault=fault)
    assert trace['firstAttempts'] == 1 and len(trace['first'].failures) == 1
    _assert_failure(trace['first'].failures[0], target, fault)
    assert trace['firstRow'] == old and trace['firstBytes'] == before
    assert (trace['first'].ok, trace['first'].written, trace['first'].removed) == (1, 0, 0)
    assert trace['secondAttempts'] == 1 and trace['secondRow'] is None and not trace['secondExists']
    assert trace['second'].failures == [] and (trace['second'].ok, trace['second'].written, trace['second'].removed) == (1, 0, 1)
    assert trace['thirdAttempts'] == 0 and trace['thirdRow'] is None and not trace['thirdExists']
    assert trace['third'].failures == [] and (trace['third'].ok, trace['third'].written, trace['third'].removed) == (1, 0, 0)
    assert (fleet.dest_root / 'ha-token.txt').read_bytes() == b'token-value\n'


@pytest.mark.parametrize('eligibility', ['profile', 'manifest'])
@pytest.mark.parametrize('absent', [False, True])
def test_actual_success_or_confirmed_absence_forgets_only_owned_row(fleet, monkeypatch, eligibility, absent):
    target, state_path = _seed_retirement(fleet)
    _unselect(fleet, eligibility)
    if absent:
        target.unlink()
    calls = _unlink_boundary(monkeypatch, target)
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(calls) == 1 and first.failures == [] and first.removed == (0 if absent else 1)
    assert State.load(state_path).rows.keys() == {'ha-token'} and not target.exists()
    calls.clear()
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert calls == [] and second.failures == [] and second.removed == 0
    assert (fleet.dest_root / 'ha-token.txt').read_bytes() == b'token-value\n'


def test_actual_mixed_cleanup_continues_then_retries_only_failed_orphan(fleet, monkeypatch):
    target, state_path = _seed_retirement(fleet)
    old = State.load(state_path).rows['rolfing']
    _unselect(fleet, 'all')
    trace = _three_pass_trace(fleet, monkeypatch, target, state_path, fault='permission')
    assert len(trace['first'].failures) == 1 and trace['first'].removed == 1 and trace['firstRow'] == old
    _assert_failure(trace['first'].failures[0], target)
    assert not (fleet.dest_root / 'ha-token.txt').exists()
    assert trace['second'].failures == [] and trace['second'].removed == 1 and not trace['secondExists']
    assert trace['secondAttempts'] == 1 and trace['thirdAttempts'] == 0 and trace['third'].removed == 0
    assert State.load(state_path).rows == {}


@pytest.mark.parametrize('field', ['blob_sha256', 'dest_sha256', 'mode'])
def test_actual_failed_orphan_keeps_complete_sanitized_row_with_bad_comparison(fleet, monkeypatch, field):
    target, state_path = _seed_retirement(fleet)
    state = State.load(state_path)
    state.rows['rolfing'][field] = {'invalid': 'comparison metadata'}
    state.save()
    old = State.load(state_path).rows['rolfing']
    assert field not in old and old['dest'] == str(target)
    _unselect(fleet, 'manifest')
    trace = _three_pass_trace(fleet, monkeypatch, target, state_path, fault='permission')
    assert len(trace['first'].failures) == 1 and trace['firstRow'] == old
    assert trace['secondAttempts'] == 1 and trace['second'].removed == 1 and trace['secondRow'] is None
    assert trace['thirdAttempts'] == 0 and trace['third'].failures == [] and not trace['thirdExists']


@pytest.mark.parametrize('damage', ['missing', None, True, 'nul'])
def test_actual_unusable_destination_forgets_without_guessed_unlink(fleet, monkeypatch, damage):
    target, state_path = _seed_retirement(fleet)
    state = State.load(state_path)
    if damage == 'missing':
        state.rows['rolfing'].pop('dest')
    else:
        state.rows['rolfing']['dest'] = str(target) + '\x00tail' if damage == 'nul' else damage
    state.save()
    _unselect(fleet, 'profile')
    sentinel = fleet.tmp / 'unrelated sentinel'
    sentinel.write_bytes(b'unrelated bytes')
    calls = _unlink_boundary(monkeypatch, target, fault='permission')
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert calls == [] and result.failures == [] and result.removed == 0
    assert State.load(state_path).rows.keys() == {'ha-token'}
    assert target.read_bytes() == b'rolfing-value\n' and sentinel.read_bytes() == b'unrelated bytes'


@pytest.mark.parametrize('damage', ['missing', 'json', 'utf8'])
def test_actual_lost_ledger_does_not_discover_unselected_file(fleet, monkeypatch, damage):
    target, state_path = _seed_retirement(fleet)
    if damage == 'missing':
        state_path.unlink()
    else:
        state_path.write_bytes(b'{ bad' if damage == 'json' else b'\xff')
    _unselect(fleet, 'profile')
    sentinel = fleet.tmp / 'unrelated sentinel'
    sentinel.write_bytes(b'unrelated bytes')
    calls = _unlink_boundary(monkeypatch, target, fault='permission')
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert calls == [] and result.failures == [] and result.removed == 0 and result.written == 1
    assert State.load(state_path).rows.keys() == {'ha-token'}
    assert target.read_bytes() == b'rolfing-value\n' and sentinel.read_bytes() == b'unrelated bytes'


def _consumer(fleet, monkeypatch, kind, capsys):
    if kind == 'status':
        cli = _load_cli()
        monkeypatch.setattr(cli, 'CONFIG_PATH', fleet.config_path)
        monkeypatch.setattr(cli, 'DATA_DIR', fleet.data_dir)

        def call():
            code = cli.main(['status'])
            return {'code': code, 'output': capsys.readouterr().out}
    else:
        path = Path(__file__).resolve().parents[2] / 'plugins/secrets-kit/custom_bootstrap.py'
        spec = importlib.util.spec_from_file_location('secrets_orphan_bootstrap', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, 'CONFIG_PATH', fleet.config_path)
        monkeypatch.setattr(module, 'ENV_PATH', fleet.tmp / 'absent-env.json')

        def call():
            ctx = FakeCtx(fleet.data_dir)
            module.bootstrap(ctx)
            return {'ctx': ctx}
    return call


@pytest.mark.parametrize('kind', ['status', 'bootstrap'])
@pytest.mark.parametrize('eligibility', ['profile', 'manifest'])
def test_actual_consumer_failure_and_clean_retry_uses_persisted_row(fleet, monkeypatch, capsys, kind, eligibility):
    target, state_path = _seed_retirement(fleet)
    old = State.load(state_path).rows['rolfing']
    _unselect(fleet, eligibility)
    capsys.readouterr()
    call = _consumer(fleet, monkeypatch, kind, capsys)
    with monkeypatch.context() as selective:
        failed_calls = _unlink_boundary(selective, target, fault='permission')
        first = call()
        first_row = State.load(state_path).rows.get('rolfing')
    with monkeypatch.context() as observing:
        later_calls = _unlink_boundary(observing, target)
        second = call()
        second_attempts = len(later_calls)
        later_calls.clear()
        third = call()
        third_attempts = len(later_calls)
    print('I08_CONSUMER_TRACE ' + json.dumps({'kind': kind, 'firstRowRetained': first_row is not None, 'firstAttempts': len(failed_calls), 'secondAttempts': second_attempts, 'thirdAttempts': third_attempts, 'finalExists': target.exists(), 'finalRowPresent': 'rolfing' in State.load(state_path).rows}, sort_keys=True))
    assert len(failed_calls) == 1 and first_row == old
    assert second_attempts == 1 and third_attempts == 0
    assert State.load(state_path).rows.keys() == {'ha-token'} and not target.exists()
    if kind == 'status':
        assert first['code'] == 1 and 'secrets_entry' in first['output']
        assert 'rolfing' in first['output'] and str(target) in first['output'] and '1 failed' in first['output']
        assert second['code'] == third['code'] == 0
        assert '1 removed' in second['output'] and '0 failed' in second['output'] and 'removed' not in third['output']
    else:
        failed = first['ctx']
        assert len(failed.failures) == 1 and failed.failures[0][0] == 'secrets_entry'
        message = failed.failures[0][1]
        assert 'ask_reason' not in message and 'rolfing' in message['user_msg'] and str(target) in message['agent_msg']
        assert 'dummy orphan unlink refusal' in message['agent_msg'] and failed.oks == []
        assert len(failed.logs) == 1 and '1 failed' in failed.logs[0]
        assert second['ctx'].failures == third['ctx'].failures == []
        assert second['ctx'].logs == third['ctx'].logs == []
        assert len(second['ctx'].oks) == len(third['ctx'].oks) == 1
        assert '1 removed' in second['ctx'].oks[0] and 'removed' not in third['ctx'].oks[0]
