"""Matching-content permission checks through actual public consumers."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from secrets_kit import converge as convergence, perms
from secrets_kit.state import State
from test_init import _load_cli
from test_secrets_bootstrap import FakeCtx


POSIX_ONLY = pytest.mark.skipif(perms.IS_WINDOWS, reason='POSIX mode observation')


def _seed_matching(fleet, *, mode=0o600, two=False):
    raw = json.loads(fleet.manifest_path.read_text())
    raw['entries']['ha-token']['mode'] = mode
    if two:
        raw['profiles']['home-admin'].append('rolfing')
    fleet.manifest_path.write_text(json.dumps(raw), encoding='utf-8')
    fleet.unlock()
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert result.failures == [] and result.written == (2 if two else 1)
    target = fleet.dest_root / 'ha-token.txt'
    state_path = convergence.paths_for(fleet.data_dir)['state']
    state = State.load(state_path)
    row = state.rows['ha-token']
    row['written_at'] = 1
    row['unused'] = {'nested': ['preserve metadata', False]}
    state.save()
    return target, state_path, dict(row)


def _observe_drift(monkeypatch, target, *, fault=None):
    counts = {'stat': 0, 'chmod': 0, 'decrypt': 0}
    real_stat = Path.stat
    real_chmod = os.chmod
    real_decrypt = convergence.decrypt_with_identity

    def inspecting(path, *args, **kwargs):
        if path == target and sys._getframe(1).f_globals.get('__name__') == 'secrets_kit.perms':
            counts['stat'] += 1
            if fault == 'stat':
                raise PermissionError('dummy destination stat refusal')
        return real_stat(path, *args, **kwargs)

    def changing(path, mode, *args, **kwargs):
        if Path(path) == target:
            counts['chmod'] += 1
            if fault == 'chmod':
                raise PermissionError('dummy destination chmod refusal')
        return real_chmod(path, mode, *args, **kwargs)

    def decrypting(*args, **kwargs):
        counts['decrypt'] += 1
        return real_decrypt(*args, **kwargs)

    monkeypatch.setattr(Path, 'stat', inspecting)
    monkeypatch.setattr(os, 'chmod', changing)
    monkeypatch.setattr(convergence, 'decrypt_with_identity', decrypting)
    return counts


@POSIX_ONLY
@pytest.mark.parametrize('requested,widened', [(0o600, 0o644), (0o1600, 0o600)])
def test_actual_matching_content_repairs_observed_mode_then_is_idempotent(fleet, monkeypatch, requested, widened):
    target, state_path, old = _seed_matching(fleet, mode=requested)
    before = target.read_bytes()
    target.chmod(widened)
    counts = _observe_drift(monkeypatch, target)
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    actual_mode = stat.S_IMODE(os.stat(target).st_mode)
    row = State.load(state_path).rows['ha-token']
    assert actual_mode == stat.S_IMODE(requested) and result.written == 1 and result.ok == 0
    assert result.failures == [] and counts == {'stat': 1, 'chmod': 1, 'decrypt': 0}
    assert target.read_bytes() == before
    assert row['blob_sha256'] == old['blob_sha256'] and row['dest_sha256'] == old['dest_sha256']
    assert row['mode'] == format(requested, '04o') and row['dest'] == str(target) and row['written_at'] != 1
    counts.update(stat=0, chmod=0, decrypt=0)
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert second.ok == 1 and second.written == 0 and second.failures == []
    assert counts == {'stat': 1, 'chmod': 0, 'decrypt': 0}
    assert State.load(state_path).rows['ha-token'] == row


@POSIX_ONLY
@pytest.mark.parametrize('mode', [0o600, 0o1600, 0o100600])
@pytest.mark.parametrize('legacy', [False, True])
def test_actual_undamaged_hit_preserves_row_and_does_not_chmod_or_decrypt(fleet, monkeypatch, mode, legacy):
    target, state_path, old = _seed_matching(fleet, mode=mode)
    if legacy:
        state = State.load(state_path)
        state.rows['ha-token'].pop('dest')
        state.save()
        old.pop('dest')
    counts = _observe_drift(monkeypatch, target)
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert result.ok == 1 and result.written == 0 and result.failures == []
    assert counts['stat'] <= 1 and counts['chmod'] == counts['decrypt'] == 0
    assert State.load(state_path).rows['ha-token'] == old


@POSIX_ONLY
@pytest.mark.parametrize('cached_mode', ['different-declaration', None])
def test_actual_cached_mode_change_retains_one_tighten_without_new_stat(fleet, monkeypatch, cached_mode):
    target, state_path, _ = _seed_matching(fleet)
    if cached_mode is None:
        state = State.load(state_path)
        state.rows['ha-token']['mode'] = None
        state.save()
        requested = 0o600
    else:
        raw = json.loads(fleet.manifest_path.read_text())
        raw['entries']['ha-token']['mode'] = '0644'
        fleet.manifest_path.write_text(json.dumps(raw), encoding='utf-8')
        requested = 0o644
    counts = _observe_drift(monkeypatch, target)
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert result.failures == [] and result.written == 1 and result.ok == 0
    assert counts == {'stat': 0, 'chmod': 1, 'decrypt': 0}
    assert stat.S_IMODE(os.stat(target).st_mode) == requested
    assert State.load(state_path).rows['ha-token']['mode'] == format(requested, '04o')


@POSIX_ONLY
@pytest.mark.parametrize('fault', ['stat', 'chmod'])
def test_actual_permission_fault_retains_row_and_other_entry_then_retries(fleet, monkeypatch, fault):
    target, state_path, old = _seed_matching(fleet, two=True)
    target.chmod(0o644)
    before = target.read_bytes()
    with monkeypatch.context() as selective:
        counts = _observe_drift(selective, target, fault=fault)
        result = convergence.converge(fleet.config_path, fleet.data_dir)
        assert len(result.failures) == 1 and result.failures[0].key == 'secrets_entry'
        failure = result.failures[0]
        assert 'ha-token' in failure.user_msg and str(target) in failure.agent_msg and fault in failure.agent_msg
        assert failure.ask_reason is None and result.ok == 1 and result.written == 0
        assert counts == {'stat': 1, 'chmod': 1 if fault == 'chmod' else 0, 'decrypt': 0}
        assert target.read_bytes() == before and stat.S_IMODE(os.stat(target).st_mode) == 0o644
        assert State.load(state_path).rows['ha-token'] == old
        assert (fleet.dest_root / 'rolfing.txt').read_bytes() == b'rolfing-value\n'
    repaired = convergence.converge(fleet.config_path, fleet.data_dir)
    assert repaired.failures == [] and repaired.written == 1 and repaired.ok == 1
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600 and target.read_bytes() == before


@POSIX_ONLY
def test_real_git_exposure_refuses_matching_content_before_permission_operation(fleet, monkeypatch):
    subprocess.run(['git', 'init', '--quiet', str(fleet.dest_root)], check=True)
    ignore = fleet.dest_root / '.gitignore'
    ignore.write_text('ha-token.txt\n', encoding='utf-8')
    target, state_path, old = _seed_matching(fleet)
    ignore.write_text('', encoding='utf-8')
    target.chmod(0o644)
    counts = _observe_drift(monkeypatch, target)
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(result.failures) == 1 and result.failures[0].key == convergence.FAILURE_DEST
    assert counts == {'stat': 0, 'chmod': 0, 'decrypt': 0}
    assert target.read_bytes() == b'token-value\n' and stat.S_IMODE(os.stat(target).st_mode) == 0o644
    assert State.load(state_path).rows['ha-token'] == old


def _windows_acl_boundary(monkeypatch, target, *, fault=None):
    calls = []
    real_run = subprocess.run
    monkeypatch.setattr(perms, 'IS_WINDOWS', True)
    monkeypatch.setenv('USERNAME', 'dummy-owner')
    monkeypatch.setenv('USERDOMAIN', 'DUMMY')

    def running(argv, *args, **kwargs):
        if argv[0] != 'icacls':
            return real_run(argv, *args, **kwargs)
        calls.append((Path(argv[1]), list(argv), kwargs['timeout']))
        assert kwargs['timeout'] == 20
        if Path(argv[1]) == target:
            if fault == 'spawn':
                raise OSError('dummy target ACL spawn fault')
            if fault == 'timeout':
                raise subprocess.TimeoutExpired(argv, 20)
            if fault == 'nonzero':
                return subprocess.CompletedProcess(argv, 1, stdout=b'dummy target ACL refusal')
        return subprocess.CompletedProcess(argv, 0, stdout=b'dummy ACL operation')

    monkeypatch.setattr(perms.subprocess, 'run', running)
    return calls


@pytest.mark.parametrize('mode', [0o600, 0o644])
@pytest.mark.parametrize('legacy', [False, True])
def test_actual_windows_hit_reapplies_private_acl_once_without_false_record(fleet, monkeypatch, mode, legacy):
    target, state_path, old = _seed_matching(fleet, mode=mode)
    if legacy:
        state = State.load(state_path)
        state.rows['ha-token'].pop('dest')
        state.save()
        old.pop('dest')
    calls = _windows_acl_boundary(monkeypatch, target)
    monkeypatch.setattr(convergence, 'decrypt_with_identity', lambda *args: pytest.fail('matching hit must not decrypt'))
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    target_calls = [call for call in calls if call[0] == target]
    assert len(target_calls) == (1 if mode == 0o600 else 0)
    if target_calls:
        assert target_calls[0][1][2:] == ['/inheritance:r', '/grant:r', 'DUMMY\\dummy-owner:F']
    assert len(calls) == (3 if mode == 0o600 else 2)
    assert result.ok == 1 and result.written == 0 and result.failures == []
    assert State.load(state_path).rows['ha-token'] == old


@pytest.mark.parametrize('fault', ['nonzero', 'spawn', 'timeout'])
def test_actual_windows_private_hit_contains_real_acl_boundary_failure(fleet, monkeypatch, fault):
    target, state_path, old = _seed_matching(fleet, two=True)
    calls = _windows_acl_boundary(monkeypatch, target, fault=fault)
    monkeypatch.setattr(convergence, 'decrypt_with_identity', lambda *args: pytest.fail('matching hits must not decrypt'))
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len([call for call in calls if call[0] == target]) == 1
    assert len(result.failures) == 1 and result.failures[0].key == 'secrets_entry'
    failure = result.failures[0]
    assert failure.ask_reason is None and 'ha-token' in failure.user_msg and str(target) in failure.agent_msg and 'icacls' in failure.agent_msg
    assert result.ok == 1 and result.written == 0
    assert State.load(state_path).rows['ha-token'] == old and target.read_bytes() == b'token-value\n'
    assert len(calls) == 4


@pytest.mark.parametrize('kind', ['content-write', 'cached-mode-change'])
def test_actual_windows_entry_paths_do_not_duplicate_acl_operation(fleet, monkeypatch, kind):
    if kind == 'cached-mode-change':
        target, state_path, _ = _seed_matching(fleet)
        state = State.load(state_path)
        state.rows['ha-token']['mode'] = None
        state.save()
    else:
        fleet.unlock()
        target = fleet.dest_root / 'ha-token.txt'
    calls = _windows_acl_boundary(monkeypatch, target)
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert result.failures == [] and result.written == 1 and result.ok == 0
    selected = [call for call in calls if call[0].parent == target.parent and (call[0] == target or call[0].name.startswith(target.name + '.'))]
    assert len(selected) == 1 and len(calls) == 3


def _adapter(fleet, monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'plugins/secrets-kit/custom_bootstrap.py'
    spec = importlib.util.spec_from_file_location('secrets_drift_bootstrap', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'CONFIG_PATH', fleet.config_path)
    monkeypatch.setattr(module, 'ENV_PATH', fleet.tmp / 'absent-env.json')
    return module


@POSIX_ONLY
@pytest.mark.parametrize('consumer', ['status', 'bootstrap'])
@pytest.mark.parametrize('operation', ['repair', 'no-drift', 'stat-fault', 'chmod-fault'])
def test_actual_consumers_report_genuine_drift_results(fleet, monkeypatch, capsys, consumer, operation):
    target, state_path, old = _seed_matching(fleet)
    if operation != 'no-drift':
        target.chmod(0o644)
    fault = operation.split('-')[0] if operation.endswith('-fault') else None
    counts = _observe_drift(monkeypatch, target, fault=fault)
    if consumer == 'status':
        cli = _load_cli()
        monkeypatch.setattr(cli, 'CONFIG_PATH', fleet.config_path)
        monkeypatch.setattr(cli, 'DATA_DIR', fleet.data_dir)
        code = cli.main(['status'])
        output = capsys.readouterr().out
        assert code == (1 if fault else 0)
        if fault:
            assert 'secrets_entry' in output and 'ha-token' in output and '1 failed' in output
        else:
            assert ('1 written' if operation == 'repair' else '1 ok') in output
    else:
        ctx = FakeCtx(fleet.data_dir)
        _adapter(fleet, monkeypatch).bootstrap(ctx)
        if fault:
            assert len(ctx.failures) == 1 and ctx.failures[0][0] == 'secrets_entry'
            message = ctx.failures[0][1]
            assert 'ask_reason' not in message and str(target) in message['agent_msg'] and fault in message['agent_msg']
            assert ctx.oks == [] and len(ctx.logs) == 1 and '1 failed' in ctx.logs[0]
        else:
            assert ctx.failures == [] and ctx.logs == [] and len(ctx.oks) == 1
            assert ('1 written' if operation == 'repair' else '1 ok') in ctx.oks[0]
    assert counts['decrypt'] == 0
    assert counts['stat'] <= 1 if operation == 'no-drift' else counts['stat'] == 1
    assert target.read_bytes() == b'token-value\n'
    if fault:
        assert State.load(state_path).rows['ha-token'] == old
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o644
    elif operation == 'repair':
        assert State.load(state_path).rows['ha-token']['written_at'] != old['written_at']
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    else:
        assert State.load(state_path).rows['ha-token'] == old
