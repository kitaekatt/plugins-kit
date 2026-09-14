"""Actual destination slots, static leaf links, compatibility and consumers."""

import builtins
import errno
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from secrets_kit import converge as convergence
from secrets_kit import repo as repository
from secrets_kit import SecretsError, perms
from secrets_kit.state import State
from sk_testlib import copy_git_tree
from test_init import _load_cli
from test_secrets_bootstrap import FakeCtx

pytestmark = pytest.mark.skipif(shutil.which('git') is None, reason='real Git required')


@pytest.fixture(autouse=True)
def actual_import_roots():
    root = Path(__file__).resolve().parents[2]
    for module in [convergence, repository, perms, sys.modules[State.__module__]]:
        assert Path(module.__file__).resolve().parent == root / 'plugins/secrets-kit/lib/secrets_kit'


def _build_repo(path):
    for args in [('init', '--quiet'), ('config', 'user.email', 'dummy@example.invalid'), ('config', 'user.name', 'Dummy')]:
        subprocess.run(['git', *args], cwd=path, check=True, capture_output=True)


def _consumer_tree(fleet, git_template, *, ignored=False):
    root = fleet.dest_root.parent
    copy_git_tree(git_template('leaf-consumer', _build_repo), root)
    if ignored:
        (root / '.gitignore').write_text('/secrets/\n', encoding='utf-8')
    return root


def _manifest(fleet, **changes):
    data = json.loads(fleet.manifest_path.read_text())
    data['entries']['ha-token'].update(changes)
    fleet.manifest_path.write_text(json.dumps(data), encoding='utf-8')


def _two_profiles(fleet):
    data = json.loads(fleet.config_path.read_text())
    data['machines']['testbox']['profiles'] = ['home-admin', 'rolfing']
    fleet.config_path.write_text(json.dumps(data), encoding='utf-8')


def _seed(fleet, *, two=False):
    if two:
        _two_profiles(fleet)
    fleet.unlock()
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert result.failures == [] and result.written == (2 if two else 1)
    return fleet.dest_root / 'ha-token.txt', convergence.paths_for(fleet.data_dir)['state']


def _link(slot, referent, *, directory=False):
    try:
        slot.symlink_to(referent, target_is_directory=directory)
    except OSError as error:
        pytest.skip(f'controlled symlink creation unavailable: {error}')


def _outside(fleet, *, data=b'outside dummy bytes\n', mode=0o644, directory=False):
    path = fleet.tmp / 'outside referent'
    if directory:
        path.mkdir()
        (path / 'sentinel').write_bytes(data)
    else:
        path.write_bytes(data)
    path.chmod(0o755 if directory and mode == 0o644 else mode)
    return path


def _observe(monkeypatch, target, *, fault=None):
    record = {'lstat': [], 'opens': [], 'chmod': [], 'decrypt': [], 'replace': [], 'queries': [], 'allocations': []}
    real_lstat, real_open, real_chmod = os.lstat, builtins.open, os.chmod
    real_decrypt, real_replace, real_query = convergence.decrypt_with_identity, os.replace, repository._query
    real_allocate = perms.tempfile.mkstemp

    def lstat(path, *args, **kwargs):
        direct = sys._getframe(1).f_globals.get('__name__') == 'secrets_kit.converge'
        if direct and Path(path) == target:
            record['lstat'].append(str(path))
            if fault:
                number = {'permission': errno.EACCES, 'eio': errno.EIO, 'notdir': errno.ENOTDIR}[fault]
                raise OSError(number, 'dummy destination lstat refusal', str(path))
        return real_lstat(path, *args, **kwargs)

    def opening(path, *args, **kwargs):
        if isinstance(path, (str, Path)) and Path(path) == target and (args[0] if args else kwargs.get('mode', 'r')) == 'rb':
            record['opens'].append(str(path))
        return real_open(path, *args, **kwargs)

    def chmod(path, *args, **kwargs):
        if Path(path) == target:
            record['chmod'].append(str(path))
        return real_chmod(path, *args, **kwargs)

    def decrypt(identity, blob):
        if Path(blob).name == 'ha-token.txt.age':
            record['decrypt'].append(str(blob))
        return real_decrypt(identity, blob)

    def replace(source, destination, *args, **kwargs):
        if Path(destination) == target:
            record['replace'].append((str(source), str(destination)))
        return real_replace(source, destination, *args, **kwargs)

    def allocate(*args, **kwargs):
        outcome = real_allocate(*args, **kwargs)
        if kwargs.get('prefix') == target.name + '.':
            record['allocations'].append(str(Path(outcome[1]).parent))
        return outcome

    def query(args, cwd):
        record['queries'].append((args, str(cwd)))
        return real_query(args, cwd)

    for owner, name, replacement in [(os, 'lstat', lstat), (builtins, 'open', opening), (os, 'chmod', chmod), (convergence, 'decrypt_with_identity', decrypt), (os, 'replace', replace), (repository, '_query', query), (perms.tempfile, 'mkstemp', allocate)]:
        monkeypatch.setattr(owner, name, replacement)
    return record


@pytest.mark.parametrize('dangling', [False, True])
@pytest.mark.parametrize('matching', [False, True])
def test_actual_exposed_slot_refuses_outside_leaf_before_read_or_publication(fleet, git_template, monkeypatch, dangling, matching):
    target = fleet.dest_root / 'ha-token.txt'
    state_path = convergence.paths_for(fleet.data_dir)['state']
    old_row = None
    if matching:
        target, state_path = _seed(fleet)
        old_row = State.load(state_path).rows['ha-token']
        target.unlink()
    else:
        fleet.unlock()
    referent = _outside(fleet, data=b'token-value\n' if matching else b'outside dummy bytes\n')
    before_mode = stat.S_IMODE(referent.stat().st_mode)
    before_bytes = referent.read_bytes()
    if dangling:
        referent.unlink()
    _link(target, referent)
    original_link = os.readlink(target)
    root = _consumer_tree(fleet, git_template)
    _two_profiles(fleet)
    # A distinct genuinely permitted survivor, not another exposed slot.
    survivor = fleet.tmp / 'survivor'
    survivor.mkdir()
    raw = json.loads(fleet.manifest_path.read_text())
    raw['entries']['rolfing']['dest'] = str(survivor / 'rolfing.txt')
    fleet.manifest_path.write_text(json.dumps(raw), encoding='utf-8')
    exposure = repository.dest_exposure(target)
    with monkeypatch.context() as observing:
        record = _observe(observing, target)
        result = convergence.converge(fleet.config_path, fleet.data_dir)
    measured = {'failureCount': len(result.failures), 'written': result.written, 'leafIsLink': target.is_symlink(), 'leafBytes': None if target.is_symlink() else target.read_bytes().decode(), 'targetOpens': len(record['opens']), 'targetDecrypts': len(record['decrypt']), 'targetReplacements': len(record['replace'])}
    print('LEAF_EXPOSURE_TRACE ' + json.dumps(measured, sort_keys=True))
    assert exposure.status == repository.DEST_EXPOSED and exposure.dest == root.resolve() / 'secrets/ha-token.txt'
    assert exposure.gitignore_line == '/secrets/ha-token.txt'
    assert len(result.failures) == 1 and result.failures[0].key == convergence.FAILURE_DEST
    assert result.failures[0].ask_reason == 'info'
    assert 'did not write' in result.failures[0].user_msg.lower()
    assert target.is_symlink() and os.readlink(target) == original_link
    assert record['opens'] == record['chmod'] == record['decrypt'] == record['replace'] == []
    assert len(record['lstat']) == 1 and result.written == 1
    assert State.load(state_path).rows.get('ha-token') == old_row
    assert (survivor / 'rolfing.txt').read_bytes() == b'rolfing-value\n'
    if dangling:
        assert not referent.exists()
    else:
        assert referent.read_bytes() == before_bytes and stat.S_IMODE(referent.stat().st_mode) == before_mode


@pytest.mark.parametrize('policy', ['outside', 'ignored', 'waived'])
@pytest.mark.parametrize('mode_case', ['correct', 'widened', 'cached_changed', 'cached_missing'])
def test_actual_matching_leaf_replaces_slot_without_referent_hash_or_chmod(fleet, git_template, monkeypatch, policy, mode_case):
    target, state_path = _seed(fleet)
    target.unlink()
    referent = _outside(fleet, data=b'token-value\n', mode=0o600 if mode_case == 'correct' else 0o644)
    before_mode = stat.S_IMODE(referent.stat().st_mode)
    if mode_case.startswith('cached'):
        state = State.load(state_path)
        if mode_case == 'cached_changed':
            state.rows['ha-token']['mode'] = '0644'
        else:
            state.rows['ha-token'].pop('mode')
        state.save()
    if policy != 'outside':
        _consumer_tree(fleet, git_template, ignored=policy == 'ignored')
    if policy == 'waived':
        _manifest(fleet, allow_tracked_dest=True)
    _link(target, referent)
    with monkeypatch.context() as observing:
        record = _observe(observing, target)
        result = convergence.converge(fleet.config_path, fleet.data_dir)
    print('LEAF_MATCH_TRACE ' + json.dumps({'ok': result.ok, 'written': result.written, 'leafIsLink': target.is_symlink(), 'targetOpens': len(record['opens']), 'targetChmods': len(record['chmod']), 'targetDecrypts': len(record['decrypt']), 'targetReplacements': len(record['replace']), 'referentMode': stat.S_IMODE(referent.stat().st_mode)}, sort_keys=True))
    assert result.failures == [] and result.ok == 0 and result.written == 1
    assert not target.is_symlink() and target.read_bytes() == b'token-value\n'
    assert record['opens'] == record['chmod'] == [] and len(record['lstat']) == 1
    assert len(record['decrypt']) == len(record['replace']) == 1
    assert Path(record['replace'][0][0]).parent == target.parent
    assert referent.read_bytes() == b'token-value\n' and stat.S_IMODE(referent.stat().st_mode) == before_mode
    assert State.load(state_path).rows['ha-token']['dest'] == str(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    if policy == 'waived':
        assert record['queries'] == []
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert second.failures == [] and (second.ok, second.written) == (1, 0)


@pytest.mark.parametrize('policy', ['outside', 'ignored', 'waived', 'outside_to_exposed'])
@pytest.mark.parametrize('referent_kind', ['file', 'dangling', 'directory'])
def test_actual_cold_permitted_leaf_publication_preserves_referent(fleet, git_template, monkeypatch, policy, referent_kind):
    target = fleet.dest_root / 'ha-token.txt'
    fleet.unlock()
    referent = _outside(fleet, directory=referent_kind == 'directory')
    original_mode = stat.S_IMODE(referent.stat().st_mode)
    if referent_kind == 'dangling':
        referent.unlink()
    if policy in ['ignored', 'waived']:
        _consumer_tree(fleet, git_template, ignored=policy == 'ignored')
    if policy == 'waived':
        _manifest(fleet, allow_tracked_dest=True)
    if policy == 'outside_to_exposed':
        consumer = fleet.tmp / 'referent consumer'
        consumer.mkdir()
        copy_git_tree(git_template('leaf-consumer', _build_repo), consumer)
        moved = consumer / 'outside referent'
        referent.rename(moved) if referent.exists() else None
        referent = moved
    _link(target, referent, directory=referent_kind == 'directory')
    with monkeypatch.context() as observing:
        record = _observe(observing, target)
        result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert result.failures == [] and result.written == 1 and not target.is_symlink()
    assert target.read_bytes() == b'token-value\n' and len(record['replace']) == 1
    assert record['chmod'] == []
    state_path = convergence.paths_for(fleet.data_dir)['state']
    assert State.load(state_path).rows['ha-token']['dest'] == str(target)
    if referent_kind == 'dangling':
        assert not referent.exists()
    else:
        assert stat.S_IMODE(referent.stat().st_mode) == original_mode
        assert (referent / 'sentinel' if referent_kind == 'directory' else referent).read_bytes() == b'outside dummy bytes\n'
    if policy == 'waived':
        assert record['queries'] == []


@pytest.mark.parametrize('fault', ['permission', 'eio', 'notdir'])
def test_actual_lstat_fault_contains_named_failure_retains_row_and_retries(fleet, monkeypatch, fault):
    target, state_path = _seed(fleet, two=True)
    old = State.load(state_path).rows['ha-token']
    with monkeypatch.context() as selective:
        record = _observe(selective, target, fault=fault)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
        first_row = State.load(state_path).rows['ha-token']
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    print('LEAF_FAULT_TRACE ' + json.dumps({'firstFailures': len(first.failures), 'firstOK': first.ok, 'firstAttempts': len(record['lstat']), 'secondFailures': len(second.failures), 'secondOK': second.ok, 'rowPreserved': first_row == old}, sort_keys=True))
    assert len(first.failures) == 1 and first.failures[0].key == 'secrets_entry' and first.failures[0].ask_reason is None
    message = first.failures[0].agent_msg
    assert 'ha-token' in message and str(target) in message and 'lstat' in message and 'dummy destination lstat refusal' in message
    assert {'permission': 'PermissionError', 'eio': 'OSError', 'notdir': 'NotADirectoryError'}[fault] in message
    assert first_row == old and (first.ok, first.written) == (1, 0)
    assert len(record['lstat']) == 1 and record['opens'] == record['chmod'] == record['decrypt'] == record['replace'] == []
    assert second.failures == [] and (second.ok, second.written) == (2, 0)
    assert target.read_bytes() == b'token-value\n' and (fleet.dest_root / 'rolfing.txt').read_bytes() == b'rolfing-value\n'


def test_actual_missing_ordinary_leaf_keeps_materialization(fleet, monkeypatch):
    target, state_path = _seed(fleet)
    target.unlink()
    with monkeypatch.context() as observing:
        record = _observe(observing, target)
        result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert result.failures == [] and result.written == 1 and target.read_bytes() == b'token-value\n'
    assert len(record['lstat']) <= 1 and len(record['decrypt']) == len(record['replace']) == 1
    assert State.load(state_path).rows['ha-token']['dest'] == str(target)


@pytest.mark.parametrize('leaf', ['regular', 'link'])
@pytest.mark.parametrize('spelling', ['absolute', 'relative', 'tilde', 'alias_parent_dot'])
def test_actual_parent_alias_spelling_preserves_slot_and_real_git_remediation(fleet, git_template, monkeypatch, leaf, spelling):
    root = _consumer_tree(fleet, git_template)
    alias_holder = fleet.tmp / 'alias holder'
    alias_holder.mkdir()
    alias = alias_holder / 'parent alias'
    _link(alias, root, directory=True)
    if spelling == 'absolute':
        target = alias / 'secrets/ha-token.txt'
    elif spelling == 'relative':
        monkeypatch.chdir(fleet.tmp)
        target = Path('alias holder/parent alias/secrets/ha-token.txt')
    elif spelling == 'tilde':
        home = Path(os.environ['HOME'])
        _link(home / 'parent alias', root, directory=True)
        target = Path('~/parent alias/secrets/ha-token.txt')
    else:
        # Resolving the alias before '..' must choose the physical root's parent.
        target = alias / '../bank/secrets/ha-token.txt'
    actual = Path(os.path.expanduser(str(target)))
    referent = _outside(fleet)
    if leaf == 'link':
        _link(actual, referent)
    exposure = repository.dest_exposure(target)
    assert exposure.status == repository.DEST_EXPOSED
    assert exposure.dest == root.resolve() / 'secrets/ha-token.txt'
    assert exposure.gitignore_line == '/secrets/ha-token.txt'
    assert repository.gitignore_line_for(target, alias) == exposure.gitignore_line
    (root / '.gitignore').write_text(exposure.gitignore_line + '\n', encoding='utf-8')
    assert repository.dest_exposure(target).status == repository.DEST_IGNORED
    # Current declaration expansion expands '~' before producing the Path.
    _manifest(fleet, dest=str(target))
    fleet.unlock()
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert result.failures == [] and result.written == 1
    assert not actual.is_symlink() and actual.read_bytes() == b'token-value\n'
    assert alias.is_symlink() and referent.read_bytes() == b'outside dummy bytes\n'
    row = State.load(convergence.paths_for(fleet.data_dir)['state']).rows['ha-token']
    assert row['dest'] == str(actual)


@pytest.mark.parametrize('spelling', ['directory', 'root', 'dot', 'parent_dot', 'missing_parent'])
def test_public_nonfile_and_missing_parent_normalization_controls(fleet, git_template, monkeypatch, spelling):
    root = _consumer_tree(fleet, git_template)
    monkeypatch.chdir(root / 'secrets')
    targets = {'directory': root / 'secrets', 'root': Path(root.anchor), 'dot': Path('.'), 'parent_dot': Path('..'), 'missing_parent': root / 'missing/token.txt'}
    target = targets[spelling]
    result = repository.dest_exposure(target)
    assert result.dest == target.resolve()
    if spelling in ['directory', 'dot', 'missing_parent']:
        assert result.status == repository.DEST_EXPOSED
    if spelling == 'missing_parent':
        assert result.gitignore_line == '/missing/token.txt'
    # No materialization of roots or directory-traversal targets.


def test_actual_ordinary_directory_keeps_contained_replace_failure(fleet):
    target = fleet.dest_root / 'ha-token.txt'
    target.mkdir()
    (target / 'sentinel').write_bytes(b'unrelated dummy bytes')
    fleet.unlock()
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(result.failures) == 1 and result.failures[0].key == 'secrets_entry'
    assert result.written == 0 and (target / 'sentinel').read_bytes() == b'unrelated dummy bytes'
    assert 'ha-token' not in State.load(convergence.paths_for(fleet.data_dir)['state']).rows


@pytest.mark.parametrize('link_before_cleanup', [False, True])
def test_actual_recorded_slot_cleanup_preserves_referent(fleet, monkeypatch, link_before_cleanup):
    target, state_path = _seed(fleet)
    target.unlink()
    referent = _outside(fleet, data=b'token-value\n')
    _link(target, referent)
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    if link_before_cleanup:
        target.unlink()
        _link(target, referent)
    raw = json.loads(fleet.config_path.read_text())
    raw['machines']['testbox']['profiles'] = []
    fleet.config_path.write_text(json.dumps(raw), encoding='utf-8')
    result = convergence.converge(fleet.config_path, fleet.data_dir)
    assert first.failures == []
    if link_before_cleanup:
        assert len(result.failures) == 1 and result.removed == 0
        assert target.is_symlink() and State.load(state_path).get('ha-token')['dest'] == str(target)
        assert 'substituted' in result.failures[0].agent_msg
    else:
        assert result.failures == [] and result.removed == 1
        assert not target.exists() and not target.is_symlink() and State.load(state_path).rows == {}
    assert referent.read_bytes() == b'token-value\n'


def _consumer(fleet, monkeypatch, capsys, kind):
    if kind == 'status':
        cli = _load_cli()
        assert Path(cli.__file__).resolve() == Path(__file__).resolve().parents[2] / 'plugins/secrets-kit/scripts/secrets_kit_cli.py'
        monkeypatch.setattr(cli, 'CONFIG_PATH', fleet.config_path)
        monkeypatch.setattr(cli, 'DATA_DIR', fleet.data_dir)
        def call():
            code = cli.main(['status'])
            return {'code': code, 'output': capsys.readouterr().out}
    else:
        path = Path(__file__).resolve().parents[2] / 'plugins/secrets-kit/custom_bootstrap.py'
        spec = importlib.util.spec_from_file_location('secrets_leaf_bootstrap', path)
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
@pytest.mark.parametrize('operation', ['exposed', 'matching', 'lstat', 'unavailable', 'anomaly'])
def test_actual_consumers_use_leaf_policy_failure_and_replacement(fleet, git_template, monkeypatch, capsys, kind, operation):
    target, state_path = _seed(fleet, two=operation == 'lstat')
    old_row = State.load(state_path).rows['ha-token']
    if operation != 'lstat':
        target.unlink()
        referent = _outside(fleet, data=b'token-value\n')
        _link(target, referent)
    if operation in ['exposed', 'unavailable', 'anomaly']:
        _consumer_tree(fleet, git_template)
    if operation in ['unavailable', 'anomaly']:
        real_query = repository._query
        def degraded(args, cwd):
            if args == ['rev-parse', '--is-inside-work-tree']:
                return (127, 'dummy Git unavailable') if operation == 'unavailable' else (0, 'dummy unreadable Git answer')
            return real_query(args, cwd)
        monkeypatch.setattr(repository, '_query', degraded)
    capsys.readouterr()
    call = _consumer(fleet, monkeypatch, capsys, kind)
    with monkeypatch.context() as selective:
        record = _observe(selective, target, fault='permission' if operation == 'lstat' else None)
        result = call()
        first_row = State.load(state_path).rows.get('ha-token')
    retry = call() if operation == 'lstat' else None
    print('LEAF_CONSUMER_TRACE ' + json.dumps({'kind': kind, 'operation': operation, 'leafIsLink': target.is_symlink(), 'replacements': len(record['replace']), 'decrypts': len(record['decrypt']), 'rowRetained': first_row == old_row}, sort_keys=True))
    failed = operation in ['exposed', 'lstat']
    if kind == 'status':
        assert result['code'] == (1 if failed else 0)
        assert ('1 failed' if failed else '1 written') in result['output']
        if failed:
            assert (convergence.FAILURE_DEST if operation == 'exposed' else 'secrets_entry') in result['output']
        if operation == 'lstat':
            assert retry['code'] == 0 and '2 ok' in retry['output']
        if operation in ['unavailable', 'anomaly']:
            assert 'dummy' in result['output']
    else:
        ctx = result['ctx']
        assert len(ctx.failures) == (1 if failed else 0)
        if failed:
            assert ctx.oks == [] and len(ctx.logs) == 1
            key, message = ctx.failures[0]
            assert key == (convergence.FAILURE_DEST if operation == 'exposed' else 'secrets_entry')
            assert ('ask_reason' in message) == (operation == 'exposed')
            assert str(target) in message['agent_msg']
        else:
            assert len(ctx.oks) == 1 and '1 written' in ctx.oks[0]
        if operation == 'lstat':
            assert retry['ctx'].failures == [] and '2 ok' in retry['ctx'].oks[0]
        if operation in ['unavailable', 'anomaly']:
            assert len(ctx.logs) == 1 and 'dummy' in ctx.logs[0]
    if failed:
        assert first_row == old_row and record['opens'] == record['chmod'] == record['decrypt'] == record['replace'] == []
    else:
        assert not target.is_symlink() and target.read_bytes() == b'token-value\n' and len(record['replace']) == 1
        assert referent.read_bytes() == b'token-value\n'


@pytest.mark.parametrize('leaf', ['regular', 'link'])
def test_actual_alias_parent_dot_uses_physical_sibling_and_leaves_lexical_decoy(fleet, git_template, monkeypatch, leaf):
    root = _consumer_tree(fleet, git_template, ignored=True)
    holder = fleet.tmp / 'alias holder'
    holder.mkdir()
    alias = holder / 'parent alias'
    _link(alias, root, directory=True)
    target = alias / '../bank/secrets/ha-token.txt'
    decoy = holder / 'bank/secrets'
    decoy.mkdir(parents=True)
    sentinel = decoy / 'sentinel'
    sentinel.write_bytes(b'lexical decoy unchanged')
    referent = _outside(fleet)
    before_mode = stat.S_IMODE(referent.stat().st_mode)
    if leaf == 'link':
        _link(target, referent)
    _manifest(fleet, dest=str(target))
    fleet.unlock()
    with monkeypatch.context() as observing:
        record = _observe(observing, target)
        result = convergence.converge(fleet.config_path, fleet.data_dir)
    physical_parent = root.resolve() / 'secrets'
    print('LEAF_ALLOCATION_TRACE ' + json.dumps({'failureCount': len(result.failures), 'allocationParents': record['allocations'], 'publicationDestinationPreserved': bool(record['replace']) and record['replace'][0][1] == str(target)}, sort_keys=True))
    assert result.failures == [] and result.written == 1
    assert record['allocations'] == [str(physical_parent)]
    assert len(record['replace']) == 1 and record['replace'][0][1] == str(target)
    assert not target.is_symlink() and target.read_bytes() == b'token-value\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sorted(p.name for p in physical_parent.iterdir()) == ['ha-token.txt']
    assert sorted(p.name for p in decoy.iterdir()) == ['sentinel'] and sentinel.read_bytes() == b'lexical decoy unchanged'
    assert alias.is_symlink() and referent.read_bytes() == b'outside dummy bytes\n'
    assert stat.S_IMODE(referent.stat().st_mode) == before_mode
    state_path = convergence.paths_for(fleet.data_dir)['state']
    assert State.load(state_path).rows['ha-token']['dest'] == str(target)
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert second.failures == [] and (second.ok, second.written) == (1, 0)
    raw = json.loads(fleet.config_path.read_text())
    raw['machines']['testbox']['profiles'] = []
    fleet.config_path.write_text(json.dumps(raw), encoding='utf-8')
    removed = convergence.converge(fleet.config_path, fleet.data_dir)
    assert removed.failures == [] and removed.removed == 1 and not target.exists()
    assert State.load(state_path).rows == {} and referent.read_bytes() == b'outside dummy bytes\n'


def _parent_resolution_fault(monkeypatch, parent, error):
    real = Path.resolve
    calls = []
    def resolving(path, *args, **kwargs):
        owner = sys._getframe(1).f_globals.get('__name__') == 'secrets_kit.perms'
        if owner and path == parent:
            calls.append(str(path))
            raise error
        return real(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolving)
    return calls


@pytest.mark.parametrize('kind', ['oserror', 'runtime'])
def test_real_owner_parent_resolution_failure_precedes_allocation_and_producer(fleet, monkeypatch, kind):
    target = fleet.dest_root / 'ha-token.txt'
    target.write_bytes(b'prior dummy slot')
    before = sorted(p.name for p in target.parent.iterdir())
    error = OSError(errno.EIO, 'dummy parent resolve refusal') if kind == 'oserror' else RuntimeError('dummy parent resolve loop')
    produced = []
    with monkeypatch.context() as selective:
        attempts = _parent_resolution_fault(selective, target.parent, error)
        record = _observe(selective, target)
        def produce(stream):
            produced.append(True)
            stream.write(b'new dummy bytes')
            return True
        caught = None
        try:
            perms._private_output(target, 0o600, produce)
        except SecretsError as failure:
            caught = failure
    assert isinstance(caught, SecretsError) and caught.__cause__ is error
    assert str(target.parent) in str(caught) and type(error).__name__ in str(caught)
    assert len(attempts) == 1 and record['allocations'] == record['replace'] == [] and produced == []
    assert target.read_bytes() == b'prior dummy slot' and sorted(p.name for p in target.parent.iterdir()) == before


@pytest.mark.parametrize('kind', ['oserror', 'runtime'])
def test_actual_parent_resolution_entry_fault_keeps_row_survivor_and_retry(fleet, monkeypatch, kind):
    target, state_path = _seed(fleet, two=True)
    old = State.load(state_path).rows['ha-token']
    target.write_bytes(b'tampered dummy slot')
    error = OSError(errno.EIO, 'dummy parent resolve refusal') if kind == 'oserror' else RuntimeError('dummy parent resolve loop')
    with monkeypatch.context() as selective:
        attempts = _parent_resolution_fault(selective, target.parent, error)
        record = _observe(selective, target)
        first = convergence.converge(fleet.config_path, fleet.data_dir)
        first_row = State.load(state_path).rows['ha-token']
        first_bytes = target.read_bytes()
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert len(first.failures) == 1 and first.failures[0].key == 'secrets_entry' and first.failures[0].ask_reason is None
    message = first.failures[0].agent_msg
    assert str(target.parent) in message and type(error).__name__ in message and 'resolve output parent' in message
    assert len(attempts) == 1 and first_row == old and first_bytes == b'tampered dummy slot'
    assert (first.ok, first.written) == (1, 0) and record['allocations'] == record['replace'] == []
    # Decryption precedes this existing writer boundary; no zero-decrypt promise.
    assert len(record['decrypt']) == 1
    assert second.failures == [] and (second.ok, second.written) == (1, 1)
    assert target.read_bytes() == b'token-value\n' and (fleet.dest_root / 'rolfing.txt').read_bytes() == b'rolfing-value\n'


def test_real_owner_unrelated_producer_runtime_error_is_not_translated(fleet):
    target = fleet.dest_root / 'ha-token.txt'
    target.write_bytes(b'prior dummy slot')
    error = RuntimeError('dummy producer programming error')
    def produce(stream):
        stream.write(b'partial dummy output')
        raise error
    with pytest.raises(RuntimeError) as caught:
        perms._private_output(target, 0o600, produce)
    assert caught.value is error and target.read_bytes() == b'prior dummy slot'
    assert sorted(p.name for p in target.parent.iterdir()) == ['ha-token.txt']
