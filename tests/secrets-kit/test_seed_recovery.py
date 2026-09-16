"""Seed failures preserve actual owned preimages and publication evidence."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from sk_publish import fixture_commit_and_push

from secrets_kit import SecretsError, agefile, guard
from secrets_kit import converge as convergence
from secrets_kit import repo as repository
from secrets_kit.manifest import Manifest
from test_dest_guard import _armored, _templates, adding
from test_init import _load_cli, _seeding_template, seeding
from test_repo_binding import _strict_crypto
from test_sync_view import _git, _seed_author


@pytest.fixture(params=[False, True], ids=['seed', 'force-seed'])
def seed_subject(request, monkeypatch):
    subject = request.getfixturevalue('adding' if request.param else 'seeding')
    return _configure_subject(subject, request.param, monkeypatch)


@pytest.fixture
def pending_subject(adding, monkeypatch):
    return _configure_subject(adding, True, monkeypatch)


def _configure_subject(subject, force, monkeypatch):
    subject.force = force
    subject.config_path = subject.cli.CONFIG_PATH
    if subject.force:
        _seed_author(subject)
    else:
        subject.plain = subject.data_dir.parent / 'plain seed'
        subject.plain.mkdir()
        subject.source = str(subject.data_dir.parent / 'dummy proof source.txt')
        Path(subject.source).write_bytes(b'dummy seed materialization proof\n')
        raw = json.loads(subject.config_path.read_text())
        raw['vars'] = {'PLAIN': str(subject.plain)}
        subject.config_path.write_text(json.dumps(raw))
    def dummy_encrypt(recipient, payload, output):
        output = Path(output);output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(_armored(recipient.encode(), payload))
    def no_real_age(binary):
        raise AssertionError('seed fixtures must not resolve a real age binary')
    monkeypatch.setattr(agefile, 'encrypt_to_recipient', dummy_encrypt)
    monkeypatch.setattr(agefile, '_resolve', no_real_age)
    subject.calls = _strict_crypto(subject, monkeypatch)
    original_unwrap = agefile.unwrap_identity
    def encoded_wrap(identity, output):
        subject.calls.append('wrap')
        Path(output).write_bytes(_armored(b'age1dummywrap', b'b64:' + base64.b64encode(identity.encode())))
        return 0
    def encoded_unwrap(wrapped, output):
        payload = Path(wrapped).read_bytes().split(b'\n', 2)[2].removesuffix(b'-----END AGE ENCRYPTED FILE-----\n')
        if not payload.startswith(b'b64:'):return original_unwrap(wrapped, output)
        decoded = base64.b64decode(payload[4:], validate=True)
        if decoded != b'dummy new identity\n':return 9
        subject.calls.append('unwrap');Path(output).write_bytes(decoded);return 0
    monkeypatch.setattr(agefile, 'wrap_identity', encoded_wrap)
    monkeypatch.setattr(agefile, 'unwrap_identity', encoded_unwrap)

    if subject.force:
        second = subject.data_dir.parent / 'second dummy source.txt'
        second.write_bytes(b'dummy second old value\n')
        assert subject.cli.main(['add', 'second-old', '--file', str(second), '--dest', '${PLAIN}/second.txt', '--profile', 'base']) == 0
        assert convergence.converge(subject.config_path, subject.data_dir).written == 2
    guard.require_guard(subject.clone)
    with subject.cli.operation_lock(subject.data_dir):
        pass
    root = Path(__file__).resolve().parents[2]
    assert Path(subject.cli.__file__).resolve() == root / 'plugins/secrets-kit/scripts/secrets_kit_cli.py'
    records = []
    for name, module in list(sys.modules.items()):
        if name == 'secrets_kit' or name.startswith('secrets_kit.'):
            path = Path(module.__file__).resolve()
            assert path.parent == root / 'plugins/secrets-kit/lib/secrets_kit'
            records.append({'module': name, 'path': path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    for name in ['sk_testlib', 'test_init', 'test_dest_guard', 'test_sync_view', 'test_repo_binding']:
        assert Path(sys.modules[name].__file__).resolve() == root / 'tests/secrets-kit' / (name + '.py')
    for function in [subject.cli.cmd_init, subject.cli.main, repository.sync]:
        assert Path(function.__code__.co_filename).resolve() in [root / 'plugins/secrets-kit/scripts/secrets_kit_cli.py', root / 'plugins/secrets-kit/lib/secrets_kit/repo.py']
    assert not hasattr(repository, 'commit_and_push')
    for name in ['_commit_owned', '_publish_owned', '_prove_publication']:
        assert Path(getattr(repository, name).__code__.co_filename).resolve() == root / 'plugins/secrets-kit/lib/secrets_kit/repo.py'
    closure = []
    for path in sorted((root / 'plugins/secrets-kit').rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            closure.append({'file': path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    print('SEED_RECOVERY_SUBJECT_ORIGINS ' + json.dumps(records))
    print('SEED_RECOVERY_COMPLETE_CLOSURE ' + json.dumps(closure))
    return subject


def _read_only_git(path, *args):
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    result = subprocess.run(['git', *args], cwd=path, env=environment, capture_output=True)
    return {'code': result.returncode, 'out': result.stdout, 'err': result.stderr}


def _snapshot(subject):
    paths = {}
    for label, root in [('data', subject.data_dir), ('plain', subject.plain)]:
        for path in sorted(root.rglob('*')):
            relative = path.relative_to(root)
            if '.git' in relative.parts or relative.parts[0] == 'authoring-recovery':
                continue
            if path.is_file():
                paths[label + '/' + relative.as_posix()] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    index = subject.clone / '.git/index'
    leaf = subject.data_dir / 'operation.lock'
    return {'files': paths, 'index': (index.read_bytes(), stat.S_IMODE(index.stat().st_mode)) if index.exists() else None,
            'head': _read_only_git(subject.clone, 'rev-parse', '--verify', 'HEAD'),
            'symbolic': _read_only_git(subject.clone, 'symbolic-ref', 'HEAD'),
            'status': _read_only_git(subject.clone, 'status', '--porcelain=v1', '-z', '--untracked-files=all'),
            'remoteHead': _read_only_git(subject.remote if hasattr(subject, 'remote') else Path(_git(subject.clone, 'remote', 'get-url', 'origin')), 'rev-parse', '--verify', 'HEAD'),
            'guardIdentity': (leaf.stat().st_dev, leaf.stat().st_ino, leaf.read_bytes())}


def _invoke(subject, *, direct=False):
    try:
        if direct:
            code = subject.cli.cmd_init(argparse.Namespace(command='init', force=subject.force, new_terminal=False))
        else:
            code = subject.cli.main(['init'] + (['--force'] if subject.force else []))
        return {'code': code, 'exception': None}
    except BaseException as error:
        return {'code': None, 'exception': error}


def _consumer(subject, name, *, add_proof=False):
    root = subject.data_dir.parent / ('fresh seed ' + name)
    data = root / 'data';plain = root / 'plain';plain.mkdir(parents=True)
    raw = json.loads(subject.config_path.read_text())
    repository.clone(raw['repo'], data / 'repo')
    manifest = data / 'repo/manifest.json';wrapped = data / 'repo/identity.age'
    if not manifest.is_file() or not wrapped.is_file():
        return {'epochPresent': False, 'values': {}, 'failures': None}
    unwrap = agefile.unwrap_identity(wrapped, data / 'identity.txt')
    if unwrap != 0:
        return {'epochPresent': True, 'unwrap': unwrap, 'values': {}, 'failures': None}
    (data / 'identity.txt').chmod(0o600)
    raw['vars']['PLAIN'] = str(plain)
    raw['machines']['testbox']['profiles'] = sorted(json.loads(manifest.read_text())['profiles'])
    config = root / 'secrets.json';config.write_text(json.dumps(raw))
    added = None
    if add_proof:
        cli = _load_cli();cli.CONFIG_PATH = config;cli.DATA_DIR = data
        _git(data / 'repo', 'config', 'user.name', 'dummy');_git(data / 'repo', 'config', 'user.email', 'dummy@example.invalid')
        source = root / 'dummy proof.txt';source.write_bytes(b'dummy seed materialization proof\n')
        added = cli.main(['add', 'seed-proof', '--file', str(source), '--dest', '${PLAIN}/seed-proof.txt', '--profile', 'base'])
        raw['machines']['testbox']['profiles'] = ['base'];config.write_text(json.dumps(raw))
    result = convergence.converge(config, data)
    values = {p.name: p.read_bytes() for p in plain.glob('*') if p.is_file()}
    return {'epochPresent': True, 'unwrap': unwrap, 'recipient': json.loads(manifest.read_text())['recipient'],
            'values': values, 'failures': len(result.failures), 'addedProof': added}


FAULTS = ['keygen', 'keygen-unexpected', 'wrap-nonzero', 'wrap-error', 'wrap-cancel', 'manifest-serialize', 'partial-add', 'commit-refusal', 'receive-rejection']


def _fault(subject, monkeypatch, fault):
    error = RuntimeError('dummy unexpected keygen') if fault == 'keygen-unexpected' else KeyboardInterrupt('dummy wrap cancellation') if fault == 'wrap-cancel' else SecretsError('dummy controlled seed failure')
    observations = []
    if fault.startswith('keygen'):
        def keygen():
            observations.append('keygen');raise error
        monkeypatch.setattr(agefile, 'keygen', keygen)
    elif fault.startswith('wrap'):
        def wrap(identity, output):
            observations.append('partial-wrap');Path(output).write_bytes(b'dummy partial encrypted wrapper')
            if fault == 'wrap-nonzero':return 9
            raise error
        monkeypatch.setattr(agefile, 'wrap_identity', wrap)
    elif fault == 'manifest-serialize':
        real_dump = Manifest.dump
        def dump(manifest):
            if manifest.recipient == 'age1dummynewrecipient':
                observations.append('manifest-serialize');raise error
            return real_dump(manifest)
        monkeypatch.setattr(Manifest, 'dump', dump)
    elif fault == 'receive-rejection':
        remote = subject.remote if hasattr(subject, 'remote') else Path(_git(subject.clone, 'remote', 'get-url', 'origin'))
        hook = remote / 'hooks/pre-receive'
        hook.write_text('#!/bin/sh\necho dummy-known-receive-refusal >&2\nexit 1\n');hook.chmod(0o700)
        observations.append('real-receive-rejection')
    else:
        real_git = repository._git
        def git(args, *, cwd, timeout):
            if cwd == subject.clone and args[0] == ('add' if fault == 'partial-add' else 'commit'):
                observations.append(args[0])
                if fault == 'partial-add':
                    actual = real_git(['add', '--', args[-1]], cwd=cwd, timeout=timeout)
                    assert actual[0] == 0
                return 128, 'dummy known local write refusal'
            return real_git(args, cwd=cwd, timeout=timeout)
        monkeypatch.setattr(repository, '_git', git)
    return error, observations


@pytest.mark.parametrize('fault', FAULTS)
def test_actual_seed_failure_restores_owned_preimages(seed_subject, monkeypatch, capsys, fault):
    subject = seed_subject
    before = _snapshot(subject)
    with monkeypatch.context() as controlled:
        expected, observations = _fault(subject, controlled, fault)
        outcome = _invoke(subject, direct=fault in ['wrap-error', 'wrap-cancel'])
        diagnostic = capsys.readouterr()
    after = _snapshot(subject)
    if fault == 'receive-rejection':
        remote = subject.remote if hasattr(subject, 'remote') else Path(_git(subject.clone, 'remote', 'get-url', 'origin'))
        (remote / 'hooks/pre-receive').unlink()
    old_peer = _consumer(subject, 'before retry')
    retry = _invoke(subject)
    peer = _consumer(subject, 'after retry', add_proof=retry['code'] == 0)
    print('SEED_FAILURE_TRACE ' + json.dumps({'fault': fault, 'force': subject.force, 'code': outcome['code'], 'exception': type(outcome['exception']).__name__ if outcome['exception'] else None, 'snapshotHeld': before == after, 'faultReached': observations, 'retryCode': retry['code'], 'peerValues': {k: v.decode() for k, v in peer['values'].items()}}))
    assert observations and before == after
    if fault in ['keygen-unexpected', 'wrap-cancel', 'wrap-error']:
        assert outcome['exception'] is expected
    else:
        assert outcome['code'] == 1
    if subject.force:
        assert old_peer['failures'] == 0 and len(old_peer['values']) == 2
    assert retry['code'] == 0 and peer['failures'] == 0 and peer['addedProof'] == 0
    assert peer['values']['seed-proof.txt'] == b'dummy seed materialization proof\n'


def test_actual_successful_push_with_lost_report_uses_fresh_proof(seed_subject, monkeypatch, capsys):
    subject = seed_subject;before = _snapshot(subject)
    real_run = subprocess.run;pushes = []
    with monkeypatch.context() as controlled:
        def run(command, *args, **kwargs):
            result = real_run(command, *args, **kwargs)
            if command[:2] == ['git', 'push'] and Path(kwargs.get('cwd') or '.') == subject.clone:
                pushes.append({'actualCode': result.returncode, 'argv': command[1:]})
                if result.returncode == 0:
                    raise subprocess.TimeoutExpired(command, kwargs['timeout'], output=result.stdout, stderr=result.stderr)
            return result
        controlled.setattr(subprocess, 'run', run)
        outcome = _invoke(subject)
        diagnostic = capsys.readouterr()
    after = _snapshot(subject)
    peer = _consumer(subject, 'published despite lost report', add_proof=True)
    print('SEED_LOST_PUSH_TRACE ' + json.dumps({'force': subject.force, 'code': outcome['code'], 'pushes': pushes, 'remoteChanged': before['remoteHead'] != after['remoteHead'], 'peerValues': {k: v.decode() for k, v in peer['values'].items()}}))
    assert pushes and all(p['actualCode'] == 0 for p in pushes) and len(pushes) == 1
    assert outcome['code'] == 0 and before['remoteHead'] != after['remoteHead']
    assert (subject.data_dir / 'identity.txt').read_bytes() == b'dummy new identity\n'
    assert peer['failures'] == 0 and peer['values']['seed-proof.txt'] == b'dummy seed materialization proof\n'
    assert 'Nothing was published' not in diagnostic.err


DIRTY_CASES = ['staged-change', 'unstaged-change', 'staged-delete', 'staged-untracking', 'untracked', 'index-lock', 'merge-head', 'rebase-state', 'foreign-active-hook']


@pytest.mark.parametrize('dirty', DIRTY_CASES)
def test_actual_seed_refuses_foreign_work_before_sync_or_crypto(seed_subject, monkeypatch, capsys, dirty):
    subject = seed_subject;readme = subject.clone / 'README.md'
    if not readme.exists():
        readme.write_bytes(b'dummy foreign baseline\n')
        fixture_commit_and_push(subject.clone, 'dummy tracked baseline', ['README.md'])
    baseline = _git(subject.clone, 'rev-parse', 'HEAD');original = readme.read_bytes()
    controlled = None
    if dirty in ['staged-change', 'unstaged-change']:
        readme.write_bytes(b'dummy unrelated changed text\n')
        if dirty == 'staged-change':_git(subject.clone, 'add', '--', 'README.md')
    elif dirty == 'staged-delete':_git(subject.clone, 'rm', '--quiet', '--', 'README.md')
    elif dirty == 'staged-untracking':_git(subject.clone, 'rm', '--cached', '--quiet', '--', 'README.md')
    else:
        controlled = subject.clone / {'untracked': 'unowned.age', 'index-lock': '.git/index.lock', 'merge-head': '.git/MERGE_HEAD', 'rebase-state': '.git/rebase-merge', 'foreign-active-hook': '.git/hooks/prepare-commit-msg'}[dirty]
        if dirty == 'rebase-state':controlled.mkdir()
        else:
            controlled.write_bytes((baseline + '\n').encode() if dirty == 'merge-head' else b'#!/bin/sh\nexit 0\n' if dirty == 'foreign-active-hook' else b'dummy unrelated fixture bytes\n')
            if dirty == 'foreign-active-hook':controlled.chmod(0o700)
    before = _snapshot(subject);subject.calls.clear();real_git = repository._git;queries = []
    with monkeypatch.context() as observing:
        def git(args, *, cwd, timeout):
            if cwd == subject.clone:queries.append(list(args))
            return real_git(args, cwd=cwd, timeout=timeout)
        observing.setattr(repository, '_git', git)
        outcome = _invoke(subject);captured = capsys.readouterr();calls = list(subject.calls)
    after = _snapshot(subject)
    if controlled is not None:
        held = controlled.is_dir() if dirty == 'rebase-state' else controlled.read_bytes() == ((baseline + '\n').encode() if dirty == 'merge-head' else b'#!/bin/sh\nexit 0\n' if dirty == 'foreign-active-hook' else b'dummy unrelated fixture bytes\n')
        if controlled.is_dir():controlled.rmdir()
        else:controlled.unlink()
    else:
        held = True
        _git(subject.clone, 'reset', '--quiet', baseline, '--', 'README.md');readme.write_bytes(original)
    old_peer = _consumer(subject, 'dirty before retry')
    retry = _invoke(subject)
    peer = _consumer(subject, 'dirty after retry', add_proof=retry['code'] == 0)
    print('SEED_FOREIGN_TRACE ' + json.dumps({'dirty': dirty, 'force': subject.force, 'code': outcome['code'], 'held': before == after, 'calls': calls, 'queries': queries, 'retry': retry['code']}))
    assert outcome['code'] == 1 and before == after and held and calls == []
    assert not any(q[0] in ['fetch', 'merge', 'add', 'commit', 'push'] for q in queries)
    assert retry['code'] == 0 and peer['failures'] == 0 and peer['values']['seed-proof.txt'] == b'dummy seed materialization proof\n'


PENDING_CALLERS = ['init', 'init-noforce', 'add', 'update', 'remove', 'rotate-identity', 'unlock', 'status', 'status-norefresh', 'converge', 'bootstrap', 'direct-init', 'direct-add', 'direct-update', 'direct-remove', 'direct-rotate', 'direct-unlock', 'direct-status']


def _pending_call(subject, caller):
    from test_repo_binding import _operation
    if caller == 'converge':
        result = convergence.converge(subject.config_path, subject.data_dir)
        return (1 if result.failures else 0), result
    if caller.startswith('direct-'):
        arguments = {'direct-init': ['init', '--force'], 'direct-add': ['add', 'pending-new', '--file', subject.source, '--dest', '${PLAIN}/new.txt'], 'direct-update': ['add', 'ha-token', '--file', subject.source, '--update'], 'direct-remove': ['remove', 'ha-token'], 'direct-rotate': ['rotate-identity'], 'direct-unlock': ['unlock'], 'direct-status': ['status']}
        args = subject.cli.build_parser().parse_args(arguments[caller])
        try:return args.func(args), None
        except SecretsError as error:return 1, error
    return _operation(subject, caller)


@pytest.mark.parametrize('caller', PENDING_CALLERS)
def test_actual_all_callers_refuse_unowned_recovery_before_repository_use(pending_subject, monkeypatch, capsys, caller):
    subject = pending_subject;recovery = subject.data_dir / 'authoring-recovery';recovery.mkdir(mode=0o700)
    marker = recovery / 'marker.json';marker.write_bytes(b'dummy-private-marker-token');marker.chmod(0o600)
    identity = (marker.stat().st_dev, marker.stat().st_ino);before = _snapshot(subject);subject.calls.clear()
    real_clone = repository.is_clone;real_git = repository._git;queries = []
    with monkeypatch.context() as observing:
        def is_clone(path):queries.append('clone-detection');return real_clone(path)
        def git(args, *, cwd, timeout):
            if cwd == subject.clone:queries.append('git:' + args[0])
            return real_git(args, cwd=cwd, timeout=timeout)
        observing.setattr(repository, 'is_clone', is_clone);observing.setattr(repository, '_git', git)
        code, result = _pending_call(subject, caller);captured = capsys.readouterr();calls = list(subject.calls)
    after = _snapshot(subject)
    diagnostic = captured.out + captured.err + str(result)
    if hasattr(result, "failures"):
        diagnostic += "\n".join(failure.user_msg for failure in result.failures)
    held = marker.read_bytes() == b'dummy-private-marker-token' and (marker.stat().st_dev, marker.stat().st_ino) == identity
    print('SEED_PENDING_TRACE ' + json.dumps({'caller': caller, 'code': code, 'queries': queries, 'calls': calls, 'snapshotHeld': before == after, 'markerHeld': held}))
    assert code == 1 and queries == [] and calls == [] and before == after and held
    assert 'recovery' in diagnostic.lower() and 'dummy-private-marker-token' not in diagnostic
    if caller == 'converge':
        assert result.ok == result.written == result.removed == 0
        assert len(result.failures) == 1 and result.failures[0].key == 'secrets_authoring_recovery' and result.failures[0].ask_reason is None


@pytest.mark.parametrize('inactive', ['absent', 'unlisted', 'registry'])
def test_inactive_recovery_configuration_remains_inert(tmp_path, monkeypatch, inactive):
    root = tmp_path / 'inactive seed';data = root / 'data';recovery = data / 'authoring-recovery';recovery.mkdir(parents=True, mode=0o700)
    marker = recovery / 'marker.json';marker.write_bytes(b'dummy unowned recovery');marker.chmod(0o600)
    config = root / 'secrets.json'
    raw = {'repo': 'unused-dummy-origin', 'machines': {'another' if inactive == 'unlisted' else 'testbox': {'profiles': []}}}
    if inactive != 'absent':config.write_text(json.dumps(raw))
    monkeypatch.setattr('secrets_kit.manifest.resolve_host', lambda: ['testbox'])
    result = convergence.converge(config, data, known_machines=['another'] if inactive == 'registry' else None)
    assert result.ok == result.written == result.removed == 0 and not (data / 'operation.lock').exists()
    assert marker.read_bytes() == b'dummy unowned recovery'


def _lose_push_report(subject, controlled, *, proof_failure=False, descendant=False):
    real_run = subprocess.run
    observations = {'pushes': [], 'proofs': []}
    def run(command, *args, **kwargs):
        own = command[0] == 'git' and Path(kwargs.get('cwd') or '.') == subject.clone
        if own and command[1] == 'fetch' and '--no-write-fetch-head' in command:
            observations['proofs'].append(command[1:])
            if proof_failure:
                return subprocess.CompletedProcess(command, 128, b'', b'dummy-sensitive-transport-token')
        result = real_run(command, *args, **kwargs)
        if own and command[1] == 'push':
            observations['pushes'].append(result.returncode)
            assert result.returncode == 0
            if descendant:
                observations['descendant'] = _consumer(subject, 'concurrent descendant ' + str(len(observations['pushes'])), add_proof=True)
            raise subprocess.TimeoutExpired(command, kwargs['timeout'], output=result.stdout, stderr=result.stderr)
        return result
    controlled.setattr(subprocess, 'run', run)
    return observations


def test_actual_published_unknown_retains_cipher_and_refuses_every_caller(pending_subject, monkeypatch, capsys):
    from secrets_kit import authoring
    subject = pending_subject
    cache = (subject.data_dir / 'identity.txt').read_bytes()
    with monkeypatch.context() as controlled:
        observations = _lose_push_report(subject, controlled, proof_failure=True)
        outcome = _invoke(subject)
        output = capsys.readouterr()
    recovery = subject.data_dir / 'authoring-recovery'
    record = json.loads((recovery / 'marker.json').read_bytes())
    protected = _snapshot(subject)
    subject.calls.clear()
    real_run = subprocess.run;protected_commands = []
    with monkeypatch.context() as observing:
        def run(command, *args, **kwargs):
            if command[0] == 'git' and Path(kwargs.get('cwd') or '.') == subject.clone:
                protected_commands.append(command)
            return real_run(command, *args, **kwargs)
        observing.setattr(subprocess, 'run', run)
        refusals = [_pending_call(subject, caller)[0] for caller in PENDING_CALLERS]
    held = protected == _snapshot(subject)
    protected_calls = list(subject.calls)
    peer = _consumer(subject, 'unknown but published', add_proof=True)
    inspection = authoring._inspect_recovery(subject.data_dir)
    with pytest.raises(authoring.AuthoringRecoveryError):
        authoring._reconcile_recovery(subject.data_dir)
    old_cache_held = (subject.data_dir / 'identity.txt').read_bytes() == cache
    # Separately authorized reconciliation is modeled only in this owned TMP tree.
    (subject.data_dir / 'identity.txt').write_bytes(b'dummy new identity\n')
    reconciled = authoring._reconcile_recovery(subject.data_dir)
    print('SEED_UNKNOWN_TRACE ' + json.dumps({'observations': observations, 'refusals': refusals, 'held': held,
          'inspection': inspection, 'reconciled': reconciled, 'peerValues': {k:v.decode() for k,v in peer['values'].items()}}))
    assert outcome['code'] == 1 and observations['pushes'] == [0] and len(observations['proofs']) == 1
    assert record['phase'] == 'uncertain' and held and old_cache_held
    assert refusals == [1] * len(PENDING_CALLERS) and protected_commands == [] and protected_calls == []
    assert inspection['phase'] == 'uncertain' and not inspection['cache_compatible']
    assert 'dummy-sensitive-transport-token' not in output.err and 'Nothing was published' not in output.err
    assert peer['failures'] == 0 and peer['values']['seed-proof.txt'] == b'dummy seed materialization proof\n'
    assert reconciled['outcome'] == 'confirmed' and not recovery.exists()


def test_actual_lost_report_accepts_fresh_descendant(seed_subject, monkeypatch):
    subject = seed_subject
    with monkeypatch.context() as controlled:
        observations = _lose_push_report(subject, controlled, descendant=True)
        outcome = _invoke(subject)
    print('SEED_DESCENDANT_TRACE ' + repr(outcome) + repr(observations))
    assert outcome['code'] == 0 and observations['pushes'] == [0] and len(observations['proofs']) == 1
    assert observations['descendant']['failures'] == 0
    assert observations['descendant']['values']['seed-proof.txt'] == b'dummy seed materialization proof\n'
    assert (subject.data_dir / 'identity.txt').read_bytes() == b'dummy new identity\n'


def test_actual_confirmed_publication_cache_failure_retains_recovery(pending_subject, monkeypatch, capsys):
    from secrets_kit import authoring
    subject = pending_subject;cache = (subject.data_dir / 'identity.txt').read_bytes()
    real_output = authoring._private_output
    primary = OSError('dummy cache finalization failure')
    with monkeypatch.context() as controlled:
        def output(dest, mode, produce, **kwargs):
            if dest == subject.data_dir / 'identity.txt':raise primary
            return real_output(dest, mode, produce, **kwargs)
        controlled.setattr(authoring, '_private_output', output)
        outcome = _invoke(subject);diagnostic = capsys.readouterr()
    recovery = subject.data_dir / 'authoring-recovery'
    record = json.loads((recovery / 'marker.json').read_bytes())
    payloads = [path.read_bytes() for path in recovery.iterdir() if path.is_file()]
    peer = _consumer(subject, 'cache failed after publication', add_proof=True)
    with pytest.raises(authoring.AuthoringRecoveryError):authoring._reconcile_recovery(subject.data_dir)
    assert outcome['exception'] is primary and isinstance(primary.authoring_recovery_error, authoring.AuthoringRecoveryError)
    assert record['phase'] == 'finalizing' and (subject.data_dir / 'identity.txt').read_bytes() == cache
    assert b'dummy new identity\n' not in b''.join(payloads)
    assert 'Nothing was published' not in diagnostic.err and peer['failures'] == 0
    assert peer['values']['seed-proof.txt'] == b'dummy seed materialization proof\n'


@pytest.mark.parametrize('fault', ['marker-replacement', 'cipher-protection', 'applying-replacement', 'publishing-marker'])
def test_actual_recovery_write_boundaries_preserve_primary_and_safe_state(pending_subject, monkeypatch, fault):
    from secrets_kit import authoring
    subject = pending_subject;before = _snapshot(subject)
    primary = OSError('dummy recovery boundary failure');observed = []
    real_replace = os.replace;real_tighten = authoring.tighten
    with monkeypatch.context() as controlled:
        def replace(source, dest):
            dest = Path(dest)
            hit = fault == 'marker-replacement' and dest.name == 'marker.json'
            hit |= fault == 'applying-replacement' and dest == subject.clone / 'identity.age'
            if fault == 'publishing-marker' and dest.name == 'marker.json':
                hit |= json.loads(Path(source).read_bytes())['phase'] == 'publishing'
            if hit and not observed:observed.append(fault);raise primary
            return real_replace(source, dest)
        def tighten(path, mode):
            if fault == 'cipher-protection' and Path(path).name == 'proposed-identity':
                observed.append(fault);raise primary
            return real_tighten(path, mode)
        controlled.setattr(os, 'replace', replace);controlled.setattr(authoring, 'tighten', tighten)
        outcome = _invoke(subject)
    after = _snapshot(subject)
    peer = _consumer(subject, 'recovery boundary ' + fault)
    recovery = subject.data_dir / 'authoring-recovery'
    assert observed == [fault] and outcome['exception'] is primary
    assert peer['failures'] == 0 and len(peer['values']) == 2
    if fault in ['marker-replacement', 'publishing-marker']:
        assert recovery.exists() and hasattr(primary, 'authoring_recovery_error')
        assert convergence.converge(subject.config_path, subject.data_dir).failures[0].key == 'secrets_authoring_recovery'
    else:
        assert before == after and not recovery.exists()


def test_actual_body_restoration_and_release_failures_preserve_primary(pending_subject, monkeypatch):
    from secrets_kit import authoring, operation_lock as ownership
    subject = pending_subject;old_wrapper = (subject.clone / 'identity.age').read_bytes()
    primary = RuntimeError('dummy commit interrupted');real_git = repository._git;real_replace = os.replace
    with monkeypatch.context() as controlled:
        def git(args, *, cwd, timeout):
            if cwd == subject.clone and args[0] == 'commit':raise primary
            return real_git(args, cwd=cwd, timeout=timeout)
        def replace(source, dest):
            if Path(dest) == subject.clone / 'identity.age' and Path(source).read_bytes() == old_wrapper:
                raise OSError('dummy restoration refused')
            return real_replace(source, dest)
        def unlock(fd):raise OSError('dummy release refused')
        controlled.setattr(repository, '_git', git);controlled.setattr(os, 'replace', replace)
        controlled.setattr(ownership, '_unlock', unlock)
        outcome = _invoke(subject, direct=True)
    peer = _consumer(subject, 'combined failures')
    assert outcome['exception'] is primary and isinstance(primary.authoring_recovery_error, authoring.AuthoringRecoveryError)
    assert isinstance(primary.operation_lock_release_error, ownership.OperationLockError)
    assert (subject.data_dir / 'authoring-recovery/marker.json').is_file()
    assert peer['failures'] == 0 and len(peer['values']) == 2


def test_actual_owned_fast_forward_is_restored_on_failed_seed(pending_subject, monkeypatch):
    subject = pending_subject
    incoming = _consumer(subject, 'incoming owned fast-forward', add_proof=True)
    before = _snapshot(subject)
    with monkeypatch.context() as controlled:
        expected, observed = _fault(subject, controlled, 'keygen-unexpected')
        outcome = _invoke(subject)
    after = _snapshot(subject)
    retry = _invoke(subject)
    peer = _consumer(subject, 'owned fast-forward retry', add_proof=True)
    assert incoming['failures'] == 0 and outcome['exception'] is expected and observed == ['keygen']
    assert before == after and retry['code'] == 0 and peer['failures'] == 0
    assert peer['values']['seed-proof.txt'] == b'dummy seed materialization proof\n'


def test_actual_completed_commit_with_lost_report_is_verified(seed_subject, monkeypatch):
    subject = seed_subject;real_git = repository._git;commits = []
    with monkeypatch.context() as controlled:
        def git(args, *, cwd, timeout):
            result = real_git(args, cwd=cwd, timeout=timeout)
            if cwd == subject.clone and args[0] == 'commit':
                commits.append(result[0]);return 124, 'dummy lost commit report'
            return result
        controlled.setattr(repository, '_git', git)
        outcome = _invoke(subject)
    peer = _consumer(subject, 'completed commit lost report', add_proof=True)
    assert outcome['code'] == 0 and commits == [0] and peer['failures'] == 0
    assert peer['values']['seed-proof.txt'] == b'dummy seed materialization proof\n'


@pytest.mark.parametrize('spelling', ['local', 'file-url'])
@pytest.mark.parametrize('failure', [False, True], ids=['publish', 'commit-refusal'])
def test_actual_unborn_symbolic_branch_and_index_absence(seeding, monkeypatch, spelling, failure):
    subject = seeding;root = subject.data_dir.parent / 'unborn epoch';root.mkdir()
    remote = root / 'remote.git'
    _git(root, 'init', '--quiet', '--bare', '--initial-branch=fleet-seed', str(remote))
    declared = remote.as_uri() if spelling == 'file-url' else str(remote)
    subject.remote = remote;subject.data_dir = root / 'data';subject.clone = subject.data_dir / 'repo'
    repository.clone(declared, subject.clone)
    _git(subject.clone, 'config', 'user.name', 'dummy');_git(subject.clone, 'config', 'user.email', 'dummy@example.invalid')
    raw = json.loads(subject.cli.CONFIG_PATH.read_text());raw['repo'] = declared
    subject.cli.CONFIG_PATH.write_text(json.dumps(raw));subject.cli.DATA_DIR = subject.data_dir
    subject = _configure_subject(subject, False, monkeypatch)
    before = _snapshot(subject)
    with monkeypatch.context() as controlled:
        if failure:expected, observed = _fault(subject, controlled, 'commit-refusal')
        outcome = _invoke(subject)
    after = _snapshot(subject)
    retry = _invoke(subject) if failure else outcome
    peer = _consumer(subject, 'unborn real materialization', add_proof=retry['code'] == 0)
    assert before['index'] is None and before['symbolic']['out'] == b'refs/heads/fleet-seed\n'
    if failure:
        assert observed == ['commit'] and outcome['code'] == 1 and before == after
    assert retry['code'] == 0 and _read_only_git(remote, 'symbolic-ref', 'HEAD')['out'] == b'refs/heads/fleet-seed\n'
    assert peer['failures'] == 0 and peer['values']['seed-proof.txt'] == b'dummy seed materialization proof\n'


def test_actual_unowned_fast_forward_is_refused_before_merge_or_keygen(pending_subject, monkeypatch):
    subject = pending_subject
    root = subject.data_dir.parent / 'unowned incoming';repository.clone(_git(subject.clone, 'remote', 'get-url', 'origin'), root)
    _git(root, 'config', 'user.name', 'dummy');_git(root, 'config', 'user.email', 'dummy@example.invalid')
    (root / 'README.md').write_bytes(b'dummy unrelated remote change\n')
    _git(root, 'add', '--', 'README.md');_git(root, 'commit', '--quiet', '-m', 'dummy unrelated change');_git(root, 'push', '--quiet')
    before = _snapshot(subject);subject.calls.clear();real_git = repository._git;queries = []
    with monkeypatch.context() as observing:
        def git(args, *, cwd, timeout):
            if cwd == subject.clone:queries.append(args[0])
            return real_git(args, cwd=cwd, timeout=timeout)
        observing.setattr(repository, '_git', git);outcome = _invoke(subject)
    assert outcome['code'] == 1 and before == _snapshot(subject) and subject.calls == []
    assert 'fetch' in queries and 'merge' not in queries and not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('fault', ['fetch-failure', 'fetch-no-update', 'ancestry-query-failure', 'ancestry-malformed-output', 'non-ancestry'])
def test_fresh_proof_never_uses_stale_or_negative_evidence(pending_subject, monkeypatch, fault):
    from secrets_kit import authoring
    subject = pending_subject;cache = (subject.data_dir / 'identity.txt').read_bytes()
    real_run = subprocess.run;pushes = [];proofs = []
    with monkeypatch.context() as controlled:
        def run(command, *args, **kwargs):
            own = command[0] == 'git' and Path(kwargs.get('cwd') or '.') == subject.clone
            if own and command[1] == 'push':
                pushes.append(command)
                if fault != 'non-ancestry':
                    result = real_run(command, *args, **kwargs);assert result.returncode == 0
                    if fault in ['ancestry-query-failure', 'ancestry-malformed-output']:
                        peer = _consumer(subject, 'proof descendant before failed ancestry', add_proof=True)
                        assert peer['failures'] == 0
                raise subprocess.TimeoutExpired(command, kwargs['timeout'])
            if own and command[1] == 'fetch' and '--no-write-fetch-head' in command:
                proofs.append(command)
                if fault in ['fetch-failure', 'fetch-no-update']:
                    return subprocess.CompletedProcess(command, 128 if fault == 'fetch-failure' else 0, b'', b'')
            if own and command[1] == 'merge-base' and fault == 'ancestry-query-failure':
                return subprocess.CompletedProcess(command, 128, b'', b'dummy query fault')
            if own and command[1] == 'merge-base' and fault == 'ancestry-malformed-output':
                return subprocess.CompletedProcess(command, 0, b'dummy malformed ancestry record', b'')
            return real_run(command, *args, **kwargs)
        controlled.setattr(subprocess, 'run', run)
        outcome = _invoke(subject)
    marker = subject.data_dir / 'authoring-recovery/marker.json'
    assert outcome['code'] == 1 and len(pushes) == len(proofs) == 1 and marker.is_file()
    assert json.loads(marker.read_bytes())['phase'] == 'uncertain'
    assert (subject.data_dir / 'identity.txt').read_bytes() == cache
    assert convergence.converge(subject.config_path, subject.data_dir).failures[0].key == 'secrets_authoring_recovery'
    assert _read_only_git(subject.clone, 'for-each-ref', '--format=%(refname)', 'refs/secrets-kit/publication/')['out'] == b''


def test_actual_exited_publishing_process_releases_owner_and_leaves_refusal(pending_subject, tmp_path):
    subject = pending_subject;before = _snapshot(subject)
    root = Path(__file__).resolve().parents[2]
    script = tmp_path / 'dummy seed exit.py'
    script.write_text('''import base64, importlib.util, os, pathlib, subprocess, sys
root, config, data = map(pathlib.Path, sys.argv[1:])
sys.path.insert(0, str(root / "plugins/secrets-kit/lib"))
spec = importlib.util.spec_from_file_location("secrets_kit_cli", root / "plugins/secrets-kit/scripts/secrets_kit_cli.py")
cli = importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
cli.CONFIG_PATH=config;cli.DATA_DIR=data
cli.agefile.keygen=lambda: ("dummy new identity\\n", "age1dummynewrecipient")
def wrap(identity, output):
    pathlib.Path(output).write_bytes(b"-----BEGIN AGE ENCRYPTED FILE-----\\nage1dummywrap\\nb64:" + base64.b64encode(identity.encode()) + b"-----END AGE ENCRYPTED FILE-----\\n")
    return 0
cli.agefile.wrap_identity=wrap
def no_age(binary):raise AssertionError("no real age allowed")
cli.agefile._resolve=no_age
real=subprocess.run
def run(command, *args, **kwargs):
    if command[:2] == ["git", "push"]:os._exit(73)
    return real(command, *args, **kwargs)
subprocess.run=run
cli.main(["init", "--force"])
''')
    child = subprocess.run([sys.executable, str(script), str(root), str(subject.config_path), str(subject.data_dir)], capture_output=True, timeout=30)
    marker = subject.data_dir / 'authoring-recovery/marker.json'
    after = _snapshot(subject)
    refusals = [_pending_call(subject, caller)[0] for caller in PENDING_CALLERS]
    assert child.returncode == 73 and json.loads(marker.read_bytes())['phase'] == 'publishing'
    assert before['guardIdentity'] == after['guardIdentity'] and before['remoteHead'] == after['remoteHead']
    assert refusals == [1] * len(PENDING_CALLERS) and (subject.data_dir / 'identity.txt').read_bytes() == b'dummy cached old identity\n'


@pytest.mark.parametrize('change', ['missing-cipher', 'changed-cipher', 'linked-marker', 'replaced-marker', 'foreign-file', 'directory-mode', 'foreign-hook'])
def test_private_reconciliation_refuses_incomplete_or_substituted_evidence(pending_subject, monkeypatch, change):
    from secrets_kit import authoring
    subject = pending_subject
    with monkeypatch.context() as controlled:
        _lose_push_report(subject, controlled, proof_failure=True)
        assert _invoke(subject)['code'] == 1
    recovery = subject.data_dir / 'authoring-recovery';marker = recovery / 'marker.json'
    if change == 'missing-cipher':(recovery / 'proposed-identity').unlink()
    elif change == 'changed-cipher':(recovery / 'proposed-identity').write_bytes(b'dummy foreign ciphertext')
    elif change == 'foreign-file':(recovery / 'unowned.txt').write_bytes(b'dummy foreign material')
    elif change == 'directory-mode':recovery.chmod(0o755)
    elif change == 'foreign-hook':
        hook = subject.clone / '.git/hooks/prepare-commit-msg';hook.write_bytes(b'#!/bin/sh\nexit 0\n');hook.chmod(0o700)
    else:
        original = recovery.parent / 'foreign marker.json';original.write_bytes(marker.read_bytes());original.chmod(0o600)
        marker.unlink()
        if change == 'linked-marker':marker.symlink_to(original)
        else:marker.write_bytes(original.read_bytes());marker.chmod(0o600)
    before = _snapshot(subject);cache = (subject.data_dir / 'identity.txt').read_bytes()
    with pytest.raises(authoring.AuthoringRecoveryError):authoring._inspect_recovery(subject.data_dir)
    with pytest.raises(authoring.AuthoringRecoveryError):authoring._reconcile_recovery(subject.data_dir)
    assert recovery.exists() and before == _snapshot(subject) and (subject.data_dir / 'identity.txt').read_bytes() == cache


@pytest.mark.parametrize('fault', ['fsync-preimage', 'cleanup-artifact', 'proof-cleanup'])
def test_actual_durable_and_cleanup_faults_leave_recovery_refused(pending_subject, monkeypatch, fault):
    from secrets_kit import authoring
    subject = pending_subject;before = _snapshot(subject);primary = OSError('dummy durable cleanup fault');observed = []
    real_fsync = os.fsync;real_unlink = Path.unlink;real_run = subprocess.run
    with monkeypatch.context() as controlled:
        if fault == 'proof-cleanup':_lose_push_report(subject, controlled)
        prior_run = subprocess.run
        def fsync(fd):
            info = os.fstat(fd)
            recovery = subject.data_dir / 'authoring-recovery'
            if fault == 'fsync-preimage' and recovery.exists():
                for path in recovery.iterdir():
                    if path.name.startswith('preimage-') and path.is_file() and path.stat().st_ino == info.st_ino:
                        observed.append(fault);raise primary
            return real_fsync(fd)
        def unlink(path, *args, **kwargs):
            if fault == 'cleanup-artifact' and path == subject.data_dir / 'authoring-recovery/proposed-identity':
                observed.append(fault);raise primary
            return real_unlink(path, *args, **kwargs)
        def run(command, *args, **kwargs):
            if fault == 'proof-cleanup' and command[:2] == ['git', 'update-ref'] and '-d' in command and any(str(part).startswith('refs/secrets-kit/publication/') for part in command):
                observed.append(fault);return subprocess.CompletedProcess(command, 128, b'', b'dummy cleanup failure')
            return prior_run(command, *args, **kwargs)
        controlled.setattr(os, 'fsync', fsync);controlled.setattr(Path, 'unlink', unlink);controlled.setattr(subprocess, 'run', run)
        outcome = _invoke(subject)
    marker = subject.data_dir / 'authoring-recovery/marker.json'
    if fault == 'fsync-preimage':
        assert outcome['exception'] is primary and before == _snapshot(subject)
        assert (subject.data_dir / 'authoring-recovery').exists()
    else:
        assert marker.is_file() and json.loads(marker.read_bytes())['phase'] in ['finalizing', 'cleaning']
        assert 'Nothing was published' not in str(outcome)
    assert observed and convergence.converge(subject.config_path, subject.data_dir).failures[0].key == 'secrets_authoring_recovery'


@pytest.mark.parametrize('fault', ['cleanup-artifact', 'proof-cleanup'])
def test_actual_published_cleanup_can_resume_only_with_compatible_cache(pending_subject, monkeypatch, fault):
    from secrets_kit import authoring
    subject = pending_subject;real_unlink = Path.unlink;observations = []
    with monkeypatch.context() as controlled:
        if fault == 'proof-cleanup':_lose_push_report(subject, controlled)
        prior_run = subprocess.run
        def unlink(path, *args, **kwargs):
            if fault == 'cleanup-artifact' and path == subject.data_dir / 'authoring-recovery/proposed-identity':
                observations.append(fault);raise OSError('dummy interrupted cleanup')
            return real_unlink(path, *args, **kwargs)
        def run(command, *args, **kwargs):
            if fault == 'proof-cleanup' and command[:2] == ['git', 'update-ref'] and '-d' in command and any(str(part).startswith('refs/secrets-kit/publication/') for part in command):
                observations.append(fault);return subprocess.CompletedProcess(command, 128, b'', b'')
            return prior_run(command, *args, **kwargs)
        controlled.setattr(Path, 'unlink', unlink);controlled.setattr(subprocess, 'run', run)
        outcome = _invoke(subject)
    peer = _consumer(subject, 'cleanup resumable publication', add_proof=True)
    cache = subject.data_dir / 'identity.txt'
    if fault == 'proof-cleanup':
        with pytest.raises(authoring.AuthoringRecoveryError):authoring._reconcile_recovery(subject.data_dir)
        assert cache.read_bytes() == b'dummy cached old identity\n'
        cache.write_bytes(b'dummy new identity\n')
    result = authoring._reconcile_recovery(subject.data_dir)
    assert observations and outcome['code'] != 0 and result == {'outcome': 'confirmed', 'recovery': 'cleared'}
    assert cache.read_bytes() == b'dummy new identity\n' and not (subject.data_dir / 'authoring-recovery').exists()
    assert peer['failures'] == 0 and peer['values']['seed-proof.txt'] == b'dummy seed materialization proof\n'


def test_actual_cleanup_marker_failure_after_owned_unlink_can_resume(pending_subject, monkeypatch):
    from secrets_kit import authoring
    subject = pending_subject;real_replace = os.replace;observed = []
    with monkeypatch.context() as controlled:
        def replace(source, dest):
            if Path(dest).name == 'marker.json':
                proposed = json.loads(Path(source).read_bytes())
                if proposed['phase'] == 'cleaning' and proposed['cleanup_next'] is None and 'entry-index' not in proposed['cleanup_remaining'] and not observed:
                    observed.append('actual-owned-unlink-before-failed-marker-advance')
                    assert not (subject.data_dir / 'authoring-recovery/entry-index').exists()
                    raise OSError('dummy marker update lost after owned unlink')
            return real_replace(source, dest)
        controlled.setattr(os, 'replace', replace)
        outcome = _invoke(subject)
    marker = subject.data_dir / 'authoring-recovery/marker.json'
    before_resume = json.loads(marker.read_bytes())
    result = authoring._reconcile_recovery(subject.data_dir)
    peer = _consumer(subject, 'cleanup marker resumed', add_proof=True)
    assert observed and outcome['code'] != 0 and before_resume['cleanup_next'] == 'entry-index'
    assert result == {'outcome': 'confirmed', 'recovery': 'cleared'} and not marker.exists()
    assert peer['failures'] == 0 and peer['values']['seed-proof.txt'] == b'dummy seed materialization proof\n'


def test_publication_binding_failure_is_unsubmitted_and_restores_entry(seed_subject, monkeypatch):
    from test_repo_binding import BIND_QUERY
    subject = seed_subject;before = _snapshot(subject);real_run = subprocess.run;refusals = [];pushes = []
    with monkeypatch.context() as controlled:
        def run(command, *args, **kwargs):
            marker = subject.data_dir / 'authoring-recovery/marker.json'
            if command == BIND_QUERY and marker.exists() and json.loads(marker.read_bytes())['phase'] == 'publishing':
                refusals.append('binding query unavailable before actual push')
                return subprocess.CompletedProcess(command, 124, b'', b'')
            if command[:2] == ['git', 'push'] and Path(kwargs.get('cwd') or '.') == subject.clone:pushes.append(command)
            return real_run(command, *args, **kwargs)
        controlled.setattr(subprocess, 'run', run);outcome = _invoke(subject)
    after = _snapshot(subject);old_peer = _consumer(subject, 'unsubmitted binding failure')
    retry = _invoke(subject);peer = _consumer(subject, 'binding retry', add_proof=retry['code'] == 0)
    assert refusals and pushes == [] and outcome['code'] == 1 and before == after
    if subject.force:assert old_peer['failures'] == 0 and len(old_peer['values']) == 2
    assert retry['code'] == 0 and peer['failures'] == 0 and peer['values']['seed-proof.txt'] == b'dummy seed materialization proof\n'


def test_seed_refuses_ignored_blob_parent_alias_before_fetch(seeding, monkeypatch):
    subject = _configure_subject(seeding, False, monkeypatch)
    ignore = subject.clone / '.gitignore';ignore.write_bytes(b'*\n!.gitignore\n!README.md\n!identity.age\n!manifest.json\n')
    _git(subject.clone, 'add', '--', '.gitignore');_git(subject.clone, 'commit', '--quiet', '-m', 'dummy intersecting ignore');_git(subject.clone, 'push', '--quiet')
    foreign = subject.data_dir.parent / 'unowned cipher folder';foreign.mkdir();sentinel = foreign / 'unowned.age';sentinel.write_bytes(b'dummy unowned protected bytes')
    (subject.clone / 'blobs').symlink_to(foreign, target_is_directory=True)
    before = _snapshot(subject);subject.calls.clear();real_run = subprocess.run;mutations = []
    with monkeypatch.context() as observing:
        def run(command, *args, **kwargs):
            if command[0] == 'git' and Path(kwargs.get('cwd') or '.') == subject.clone and command[1] in ['fetch', 'merge', 'add', 'commit', 'push']:
                mutations.append(command[1])
            return real_run(command, *args, **kwargs)
        observing.setattr(subprocess, 'run', run);outcome = _invoke(subject)
    assert outcome['code'] == 1 and mutations == [] and subject.calls == [] and before == _snapshot(subject)
    assert sentinel.read_bytes() == b'dummy unowned protected bytes' and (subject.clone / 'blobs').is_symlink()


def test_recovery_preimage_integrity_is_checked_before_branch_cas(pending_subject, monkeypatch):
    subject = pending_subject;before = _snapshot(subject);real_git = repository._git;branches = []
    with monkeypatch.context() as controlled:
        def git(args, *, cwd, timeout):
            if cwd == subject.clone and args[0] == 'commit':
                actual = real_git(args, cwd=cwd, timeout=timeout);assert actual[0] == 0
                journal = subject.data_dir / 'authoring-recovery'
                record = json.loads((journal / 'marker.json').read_bytes())
                (journal / record['preimages']['identity.age']['stored']).write_bytes(b'dummy corrupted recovery preimage')
                raise RuntimeError('dummy completed commit and corrupted preimage')
            return real_git(args, cwd=cwd, timeout=timeout)
        real_run = subprocess.run
        def run(command, *args, **kwargs):
            if command[:2] == ['git', 'update-ref'] and any(str(part).startswith('refs/heads/') for part in command):branches.append(command)
            return real_run(command, *args, **kwargs)
        controlled.setattr(repository, '_git', git);controlled.setattr(subprocess, 'run', run);outcome = _invoke(subject)
    after = _snapshot(subject)
    peer = _consumer(subject, 'preimage integrity failure')
    assert isinstance(outcome['exception'], RuntimeError) and hasattr(outcome['exception'], 'authoring_recovery_error')
    assert branches == [] and before['remoteHead'] == after['remoteHead'] and before['head'] != after['head']
    assert (subject.data_dir / 'authoring-recovery/marker.json').is_file() and peer['failures'] == 0 and len(peer['values']) == 2


def test_foreign_staged_content_at_owned_path_is_not_discarded(pending_subject, monkeypatch):
    subject = pending_subject;real_git = repository._git;staged = []
    with monkeypatch.context() as controlled:
        def git(args, *, cwd, timeout):
            if cwd == subject.clone and args[0] == 'add':
                actual = real_git(args, cwd=cwd, timeout=timeout);assert actual[0] == 0
                object_result = subprocess.run(['git', 'hash-object', '-w', '--stdin'], cwd=subject.clone, input=b'dummy foreign staged content', capture_output=True, check=True)
                oid = object_result.stdout.decode().strip()
                _git(subject.clone, 'update-index', '--cacheinfo', '100644,' + oid + ',identity.age')
                staged.append(((subject.clone / '.git/index').read_bytes(), (subject.clone / 'identity.age').read_bytes()))
                return 128, 'dummy partial stage report'
            return real_git(args, cwd=cwd, timeout=timeout)
        controlled.setattr(repository, '_git', git);outcome = _invoke(subject)
    actual = ((subject.clone / '.git/index').read_bytes(), (subject.clone / 'identity.age').read_bytes())
    peer = _consumer(subject, 'foreign staged content refusal')
    assert staged and outcome['code'] == 1 and actual == staged[0]
    assert (subject.data_dir / 'authoring-recovery/marker.json').exists() and peer['failures'] == 0 and len(peer['values']) == 2


def test_recovery_binding_does_not_copy_repository_userinfo(pending_subject, monkeypatch):
    from secrets_kit import authoring
    subject = pending_subject
    raw = json.loads(subject.config_path.read_text());local_repo = raw['repo']
    declared = 'https://dummy-user:dummy-password@example.invalid/fleet.git'
    # The clone-local rewrite keeps every transport inside this owned TMP tree.
    _git(subject.clone, 'config', 'remote.origin.url', declared)
    _git(subject.clone, 'config', 'url.' + local_repo + '.insteadOf', declared)
    raw['repo'] = declared;subject.config_path.write_text(json.dumps(raw))
    with monkeypatch.context() as controlled:
        observations = _lose_push_report(subject, controlled, proof_failure=True)
        outcome = _invoke(subject)
    recovery = subject.data_dir / 'authoring-recovery'
    marker = (recovery / 'marker.json').read_bytes()
    record = json.loads(marker)
    inspection = authoring._inspect_recovery(subject.data_dir)
    # Fresh peers use the same local repository directly, without the URL rewrite.
    raw['repo'] = local_repo;subject.config_path.write_text(json.dumps(raw))
    peer = _consumer(subject, 'userinfo binding', add_proof=True)
    raw['repo'] = declared;subject.config_path.write_text(json.dumps(raw))
    (subject.data_dir / 'identity.txt').write_bytes(b'dummy new identity\n')
    reconciled = authoring._reconcile_recovery(subject.data_dir)
    assert outcome['code'] == 1 and observations['pushes'] == [0]
    assert b'dummy-user' not in marker and b'dummy-password' not in marker and declared.encode() not in marker
    assert record['declared_repo_digest'] == hashlib.sha256(declared.encode()).hexdigest()
    assert declared not in json.dumps(inspection) and reconciled['outcome'] == 'confirmed'
    assert peer['failures'] == 0 and peer['values']['seed-proof.txt'] == b'dummy seed materialization proof\n'
    assert not recovery.exists()
