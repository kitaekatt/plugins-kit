"""Recorded repository spelling must agree before using a retained clone."""

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from secrets_kit import agefile
from secrets_kit import SecretsError
from secrets_kit import converge as convergence
from secrets_kit import repo as repository
from test_dest_guard import _armored, _templates, adding
from test_sync_view import _dummy_crypto, _git, _seed_author, actual_subject_origins


OPERATIONS = ['init', 'init-noforce', 'add', 'update', 'remove', 'rotate-identity', 'unlock', 'status', 'status-norefresh', 'bootstrap']
BIND_QUERY = ['git', 'config', '--local', '--no-includes', '--null', '--get-all', 'remote.origin.url']


@pytest.fixture(autouse=True)
def binding_subject_origins():
    root = Path(__file__).resolve().parents[2]
    records = []
    for name, module in list(sys.modules.items()):
        if name == 'secrets_kit' or name.startswith('secrets_kit.'):
            file = Path(module.__file__).resolve()
            assert file.parent == root / 'plugins/secrets-kit/lib/secrets_kit'
            records.append({'module': name, 'file': file.relative_to(root).as_posix(), 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()})
    for name in ['sk_testlib', 'test_repo', 'test_dest_guard', 'test_sync_view']:
        file = Path(sys.modules[name].__file__).resolve()
        assert file == root / 'tests/secrets-kit' / (name + '.py')
        records.append({'module': name, 'file': file.relative_to(root).as_posix(), 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()})
    assert Path(convergence.converge.__code__.co_filename).resolve() == root / 'plugins/secrets-kit/lib/secrets_kit/converge.py'
    print('BINDING_SUBJECT_ORIGINS ' + json.dumps(records))


def _strict_crypto(adding, monkeypatch):
    calls = _dummy_crypto(adding, monkeypatch)
    identities = {
        b'dummy cached old identity\n': b'age1testrecipient',
        b'dummy new fleet consumer identity\n': b'age1dummynewfleet',
        b'dummy new identity\n': b'age1dummynewrecipient',
    }
    def decrypt(identity, blob):
        calls.append('decrypt')
        header, recipient, payload = Path(blob).read_bytes().split(b'\n', 2)
        if header != b'-----BEGIN AGE ENCRYPTED FILE-----' or identities.get(Path(identity).read_bytes()) != recipient:
            raise SecretsError('dummy identity does not match dummy ciphertext recipient')
        return payload.removesuffix(b'-----END AGE ENCRYPTED FILE-----\n')
    def unwrap(wrapped, target):
        calls.append('unwrap')
        payload = Path(wrapped).read_bytes().split(b'\n', 2)[2].removesuffix(b'-----END AGE ENCRYPTED FILE-----\n')
        identity = {b'dummy wrapped identity\n': b'dummy cached old identity\n',
                    b'dummy new fleet wrapped identity\n': b'dummy new fleet consumer identity\n',
                    b'dummy new identity\n': b'dummy new identity\n'}.get(payload)
        if identity is None:
            return 9
        Path(target).write_bytes(identity)
        return 0
    monkeypatch.setattr(agefile, 'decrypt_with_identity', decrypt)
    monkeypatch.setattr(convergence, 'decrypt_with_identity', decrypt)
    monkeypatch.setattr(agefile, 'unwrap_identity', unwrap)
    return calls


def _adapter(adding):
    source = Path(__file__).resolve().parents[2] / 'plugins/secrets-kit/custom_bootstrap.py'
    spec = importlib.util.spec_from_file_location('dummy_secrets_binding_adapter', source)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    assert Path(adapter.__file__).resolve() == source
    assert Path(adapter.bootstrap.__code__.co_filename).resolve() == source
    adapter.CONFIG_PATH = adding.config_path
    adapter._known_machines = lambda: ['testbox']
    return adapter


def _operation(adding, operation):
    if operation == 'bootstrap':
        failures = []
        logs = []
        context = SimpleNamespace(data_dir=adding.data_dir, log=logs.append, log_ok=logs.append,
                                  add_failure=lambda key, **kwargs: failures.append({'key': key, **kwargs}))
        _adapter(adding).bootstrap(context)
        return (1 if failures else 0), {'failures': failures, 'logs': logs}
    source = adding.data_dir.parent / 'new-entry-source.txt'
    arguments = {
        'init': ['init', '--force'],
        'init-noforce': ['init'],
        'add': ['add', 'new-entry', '--file', str(source), '--dest', '${PLAIN}/new-entry.txt'],
        'update': ['add', 'ha-token', '--file', adding.source, '--update'],
        'remove': ['remove', 'ha-token'],
        'rotate-identity': ['rotate-identity'],
        'unlock': ['unlock'],
        'status': ['status', '--refresh'],
        'status-norefresh': ['status'],
    }
    return adding.cli.main(arguments[operation]), {}


def _boundaries(adding, monkeypatch, calls):
    calls.clear()
    assert Path(adding.cli.__file__).resolve() == Path(__file__).resolve().parents[2] / 'plugins/secrets-kit/scripts/secrets_kit_cli.py'
    actual = repository._git
    queries = []
    def querying(args, *, cwd, timeout):
        result = actual(args, cwd=cwd, timeout=timeout)
        queries.append({'argv': args, 'cwd': str(cwd) if cwd else None, 'timeout': timeout, 'status': result[0]})
        return result
    monkeypatch.setattr(repository, '_git', querying)
    guarded = adding.cli.guard.require_guard
    def guarding(clone):
        calls.append('guard')
        return guarded(clone)
    monkeypatch.setattr(adding.cli.guard, 'require_guard', guarding)
    return calls, queries


def _two_repositories(adding, monkeypatch):
    _seed_author(adding)
    orphan = adding.data_dir.parent / 'orphan-source.txt'
    orphan.write_bytes(b'dummy owned orphan\n')
    assert adding.cli.main(['add', 'old-orphan', '--file', str(orphan), '--dest', '${PLAIN}/old-orphan.txt', '--profile', 'base']) == 0
    calls = _strict_crypto(adding, monkeypatch)
    assert convergence.converge(adding.config_path, adding.data_dir).written == 2
    assert adding.cli.main(['remove', 'old-orphan']) == 0
    old = Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))
    root = adding.data_dir.parent / 'new fleet'
    root.mkdir()
    new = root / 'remote.git'
    _git(root, 'clone', '--quiet', '--bare', str(old), str(new))
    producer = root / 'producer'
    repository.clone(str(new), producer)
    _git(producer, 'config', 'user.name', 'dummy');_git(producer, 'config', 'user.email', 'dummy@example.invalid')
    raw = json.loads((producer / 'manifest.json').read_text())
    raw['recipient'] = 'age1dummynewfleet'
    raw['entries']['ha-token']['doc'] = 'dummy-new-fleet.md'
    agefile.encrypt_to_recipient(raw['recipient'], b'dummy new fleet payload\n', producer / raw['entries']['ha-token']['blob'])
    (producer / 'identity.age').write_bytes(_armored(b'age1dummywrap', b'dummy new fleet wrapped identity\n'))
    (producer / 'manifest.json').write_text(json.dumps(raw))
    repository.commit_and_push(producer, 'dummy distinct new fleet', ['manifest.json', 'identity.age', raw['entries']['ha-token']['blob']])
    (adding.plain / 'ha-token.txt').write_bytes(b'dummy user-edited destination\n')
    (adding.data_dir.parent / 'new-entry-source.txt').write_bytes(b'dummy new entry\n')
    calls.clear()
    return old, new, calls


def _snapshot(adding, remotes):
    roots = [adding.data_dir, adding.plain]
    files = {}
    for root in roots:
        for file in sorted(root.rglob('*')):
            if file.is_file() and '.git' not in file.relative_to(root).parts:
                files[str(file)] = file.read_bytes()
    def observed(*args):
        proc = subprocess.run(['git', *args], cwd=adding.clone, capture_output=True, text=True)
        return {'status': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr}
    return {'files': files, 'head': observed('rev-parse', 'HEAD'),
            'index': observed('status', '--porcelain'),
            'indexBytes': (adding.clone / '.git/index').read_bytes(),
            'hookBytes': (adding.clone / '.git/hooks/pre-commit').read_bytes(),
            'originConfig': (adding.clone / '.git/config').read_bytes(),
            'remoteHeads': [_git(remote, 'rev-parse', 'HEAD') for remote in remotes],
            'remoteTrees': [_git(remote, 'ls-tree', '-r', 'HEAD') for remote in remotes]}


def _new_consumer(adding, remote, name='new'):
    root = adding.data_dir.parent / ('fresh ' + name + ' fleet consumer')
    repository.clone(str(remote), root / 'data/repo')
    (root / 'plain').mkdir()
    assert agefile.unwrap_identity(root / 'data/repo/identity.age', root / 'data/identity.txt') == 0
    (root / 'data/identity.txt').chmod(0o600)
    raw = json.loads(adding.config_path.read_text())
    raw['repo'] = str(remote)
    raw['vars']['PLAIN'] = str(root / 'plain')
    config = root / 'secrets.json'
    config.write_text(json.dumps(raw))
    result = convergence.converge(config, root / 'data')
    print('BINDING_PEER_RESULT ' + json.dumps({'peer': name, 'failures': [{'key': failure.key, 'message': failure.agent_msg} for failure in result.failures]}))
    destination = root / 'plain/ha-token.txt'
    return {'written': result.written, 'failures': len(result.failures),
            'payload': destination.read_bytes().decode() if destination.is_file() else None,
            'recipient': json.loads((root / 'data/repo/manifest.json').read_text())['recipient']}


@pytest.mark.parametrize('operation', OPERATIONS)
def test_actual_changed_declaration_refuses_old_clone_before_use(adding, monkeypatch, capsys, operation):
    old, new, calls = _two_repositories(adding, monkeypatch)
    raw = json.loads(adding.config_path.read_text());raw['repo'] = str(new)
    adding.config_path.write_text(json.dumps(raw))
    before = _snapshot(adding, [old, new])
    calls, queries = _boundaries(adding, monkeypatch, calls)
    code, adapter = _operation(adding, operation)
    captured = capsys.readouterr()
    after = _snapshot(adding, [old, new])
    author_calls = list(calls)
    author_queries = list(queries)
    later_unlock, unused = _operation(adding, 'unlock')
    later_converge = convergence.converge(adding.config_path, adding.data_dir)
    after_later = _snapshot(adding, [old, new])
    later_calls = list(calls)
    old_consumer = _new_consumer(adding, old, 'old')
    consumer = _new_consumer(adding, new)
    diagnostic = captured.out + captured.err + json.dumps(adapter)
    print('REPO_BINDING_TRACE ' + json.dumps({'operation': operation, 'code': code,
          'snapshotUnchanged': before == after, 'authorBoundaryCalls': author_calls,
          'authorQueries': author_queries, 'laterUnlock': later_unlock,
          'laterConvergeFailures': len(later_converge.failures), 'laterBoundaryCalls': later_calls,
          'oldConsumer': old_consumer, 'consumer': consumer}))
    assert code == 1 and before == after == after_later and author_calls == later_calls == []
    assert later_unlock == 1 and len(later_converge.failures) == 1
    assert not any(row['argv'][0] in ('clone', 'fetch', 'merge', 'cat-file', 'add', 'commit', 'push') for row in author_queries)
    assert 'repo' in diagnostic.lower() and ('match' in diagnostic.lower() or 'origin' in diagnostic.lower())
    assert consumer == {'written': 1, 'failures': 0, 'payload': 'dummy new fleet payload\n', 'recipient': 'age1dummynewfleet'}
    assert old_consumer == {'written': 1, 'failures': 0, 'payload': Path(adding.source).read_bytes().decode(), 'recipient': 'age1testrecipient'}


@pytest.mark.parametrize('operation', OPERATIONS)
def test_actual_same_origin_remains_usable(adding, monkeypatch, operation):
    old, new, calls = _two_repositories(adding, monkeypatch)
    calls, queries = _boundaries(adding, monkeypatch, calls)
    code, adapter = _operation(adding, operation)
    assert code == (1 if operation == 'init-noforce' else 0)
    assert _git(adding.clone, 'config', '--local', '--get', 'remote.origin.url') == str(old)


@pytest.mark.parametrize('case', ['file-alias', 'missing', 'duplicate-equal', 'duplicate-unequal', 'malformed-config'])
def test_actual_unusable_origin_refuses_unlock_before_crypto(adding, monkeypatch, capsys, case):
    old, new, calls = _two_repositories(adding, monkeypatch)
    if case == 'file-alias':
        raw = json.loads(adding.config_path.read_text());raw['repo'] = old.as_uri()
        adding.config_path.write_text(json.dumps(raw))
    elif case == 'missing':
        _git(adding.clone, 'config', '--local', '--unset-all', 'remote.origin.url')
    elif case.startswith('duplicate'):
        _git(adding.clone, 'config', '--local', '--add', 'remote.origin.url', str(old if case == 'duplicate-equal' else new))
    else:
        with (adding.clone / '.git/config').open('a') as stream:
            stream.write('\n[dummy malformed\n')
    before = _snapshot(adding, [old, new])
    calls, queries = _boundaries(adding, monkeypatch, calls)
    code, unused = _operation(adding, 'unlock')
    diagnostic = capsys.readouterr().err
    after = _snapshot(adding, [old, new])
    author_calls = list(calls)
    author_queries = list(queries)
    old_consumer = _new_consumer(adding, old, 'old')
    new_consumer = _new_consumer(adding, new)
    print('BINDING_ORIGIN_FAILURE ' + json.dumps({'case': case, 'code': code, 'snapshotUnchanged': before == after, 'authorCalls': author_calls, 'queries': author_queries, 'oldConsumer': old_consumer, 'newConsumer': new_consumer}))
    assert code == 1 and before == after and author_calls == [] and 'binding' in diagnostic.lower()
    assert not any(row['argv'][0] in ('fetch', 'merge', 'cat-file') for row in author_queries)
    assert old_consumer['failures'] == new_consumer['failures'] == 0


@pytest.mark.parametrize('case', ['cooldown-active', 'cooldown-expired', 'forced', 'missing-manifest'])
def test_binding_precedes_every_convergence_refresh_path(adding, monkeypatch, case):
    old, new, calls = _two_repositories(adding, monkeypatch)
    stamp = adding.data_dir / 'fetch-stamp'
    from secrets_kit.converge import paths_for
    stamp = paths_for(adding.data_dir)['fetch_stamp']
    if case == 'cooldown-expired':
        stamp.touch();os.utime(stamp, (1, 1))
    elif case == 'cooldown-active':
        stamp.touch()
    if case == 'missing-manifest':
        adding.manifest_path.unlink()
    raw = json.loads(adding.config_path.read_text());raw['repo'] = str(new)
    adding.config_path.write_text(json.dumps(raw))
    before = _snapshot(adding, [old, new])
    calls, queries = _boundaries(adding, monkeypatch, calls)
    result = convergence.converge(adding.config_path, adding.data_dir, force_refresh=case == 'forced')
    after = _snapshot(adding, [old, new])
    assert before == after and calls == [] and result.ok == result.written == result.removed == 0
    assert len(result.failures) == 1 and result.failures[0].key == convergence.FAILURE_CONFIG
    assert 'binding' in result.failures[0].agent_msg.lower() and result.failures[0].ask_reason is None
    assert not any(row['argv'][0] in ('fetch', 'merge', 'cat-file') for row in queries)


@pytest.mark.parametrize('case', ['absent', 'unlisted', 'registry-rejected'])
def test_inactive_or_rejected_host_does_not_prepare_binding(adding, monkeypatch, case):
    _seed_author(adding)
    raw = json.loads(adding.config_path.read_text())
    if case == 'absent':
        adding.config_path.unlink()
    elif case == 'unlisted':
        raw['machines'] = {};adding.config_path.write_text(json.dumps(raw))
    def no_preparation(*args, **kwargs):
        pytest.fail('inactive/rejected pass performed local preparation')
    monkeypatch.setattr(repository, '_git', no_preparation)
    monkeypatch.setattr(convergence, 'paths_for', no_preparation)
    result = convergence.converge(adding.config_path, adding.data_dir, known_machines=['dummy-other'] if case == 'registry-rejected' else None)
    assert (len(result.failures) == 1) if case == 'registry-rejected' else (not result.failures and result.skipped_reason)


def test_same_origin_offline_unlock_still_uses_cached_wrapped_identity(adding, monkeypatch, capsys):
    old, new, calls = _two_repositories(adding, monkeypatch)
    held = old.with_name('offline-held.git')
    old.rename(held)
    calls, queries = _boundaries(adding, monkeypatch, calls)
    code, unused = _operation(adding, 'unlock')
    diagnostic = capsys.readouterr().out
    assert code == 0 and calls == ['unwrap'] and 'continuing on the existing checkout' in diagnostic
    assert (adding.data_dir / 'identity.txt').read_bytes() == b'dummy cached old identity\n'


def test_recorded_origin_matches_even_when_transport_spelling_is_rewritten(adding, monkeypatch):
    old, new, calls = _two_repositories(adding, monkeypatch)
    _git(adding.clone, 'config', '--local', 'url.' + old.as_uri() + '.insteadOf', str(old))
    assert _git(adding.clone, 'remote', 'get-url', 'origin') == old.as_uri()
    calls, queries = _boundaries(adding, monkeypatch, calls)
    code, unused = _operation(adding, 'remove')
    assert code == 0 and _git(adding.clone, 'config', '--local', '--get', 'remote.origin.url') == str(old)


def _origin_reply(monkeypatch, adding, *, code=0, stdout=b'', stderr=b'', exception=None):
    actual = repository.subprocess.run
    observed = []
    def querying(argv, **kwargs):
        if argv == BIND_QUERY and kwargs.get('cwd') == str(adding.clone):
            assert kwargs['timeout'] == 10 and kwargs['stdout'] == kwargs['stderr'] == subprocess.PIPE
            assert kwargs['env']['GIT_TERMINAL_PROMPT'] == '0'
            observed.append({'argv': argv, 'timeout': kwargs['timeout']})
            if exception is not None:
                raise exception
            return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)
        return actual(argv, **kwargs)
    monkeypatch.setattr(repository.subprocess, 'run', querying)
    return observed


@pytest.mark.parametrize('code,stdout,stderr,exception', [
    (1, b'', b'', None), (1, b'dummy\0', b'', None),
    (128, b'dummy\0', b'dummy private token', None), (124, b'', b'', None), (-9, b'', b'', None),
    (0, b'', b'', None), (0, b'\0', b'', None), (0, b'dummy', b'', None),
    (0, b'\xff\0', b'', None), (0, b'dummy\0dummy\0', b'', None),
    (0, b'dummy\0other\0', b'', None), (0, b'dummy\0trailing', b'', None),
    (0, b'dummy\0\0', b'', None), (0, b'dummy\0', b'dummy private token', None),
    (0, b'dummy\0', b'\n', None),
    (0, b'', b'', FileNotFoundError(2, 'dummy private token')),
    (0, b'', b'', PermissionError(13, 'dummy private token')),
    (0, b'', b'', subprocess.TimeoutExpired(BIND_QUERY, 10, output=b'dummy private token', stderr=b'dummy private token')),
])
def test_complete_origin_query_failure_refuses_without_reply_disclosure(adding, monkeypatch, capsys, code, stdout, stderr, exception):
    _seed_author(adding)
    calls = _strict_crypto(adding, monkeypatch)
    calls.clear()
    before = _snapshot(adding, [Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))])
    declared = json.loads(adding.config_path.read_text())['repo'].encode()
    stdout = stdout.replace(b'dummy', declared)
    observed = _origin_reply(monkeypatch, adding, code=code, stdout=stdout, stderr=stderr, exception=exception)
    result, unused = _operation(adding, 'remove')
    diagnostic = capsys.readouterr().err
    after = _snapshot(adding, [Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))])
    assert result == 1 and calls == [] and before == after and len(observed) == 1
    assert 'binding' in diagnostic.lower() and 'establish' in diagnostic.lower() and 'dummy private token' not in diagnostic


@pytest.mark.parametrize('value', [' dummy path ', '\tdummy\t', 'dummy\nurl\n', 'dummy\r\nurl\r', 'dummy' + chr(0xa0), 'dummy' + chr(0x2028) + 'url', 'ssh://dummy:dummy-token@example.invalid/path'])
def test_single_complete_origin_value_preserves_legitimate_data(adding, monkeypatch, value):
    _seed_author(adding)
    raw = json.loads(adding.config_path.read_text());raw['repo'] = value
    adding.config_path.write_text(json.dumps(raw))
    observed = _origin_reply(monkeypatch, adding, stdout=value.encode() + b'\0')
    code, unused = _operation(adding, 'remove')
    assert code == 0 and len(observed) == 1


def test_unrepresentable_declaration_is_comparison_inability(adding, monkeypatch, capsys):
    _seed_author(adding)
    raw = json.loads(adding.config_path.read_text());raw['repo'] = 'dummy' + chr(0xd800)
    adding.config_path.write_text(json.dumps(raw))
    _origin_reply(monkeypatch, adding, stdout=b'dummy\0')
    code, unused = _operation(adding, 'remove')
    diagnostic = capsys.readouterr().err
    assert code == 1 and 'establish' in diagnostic.lower() and 'binding' in diagnostic.lower()


def test_binding_mismatch_omits_both_credential_shaped_urls(adding, monkeypatch, capsys):
    _seed_author(adding)
    raw = json.loads(adding.config_path.read_text());raw['repo'] = 'https://dummy:declared-token@example.invalid/repo'
    adding.config_path.write_text(json.dumps(raw))
    _origin_reply(monkeypatch, adding, stdout=b'https://dummy:recorded-token@example.invalid/repo\0')
    code, unused = _operation(adding, 'remove')
    captured = capsys.readouterr()
    diagnostic = captured.out + captured.err
    assert code == 1 and 'declared-token' not in diagnostic and 'recorded-token' not in diagnostic


def test_empty_profile_selection_stays_active_and_retires_owned_orphan(adding, monkeypatch):
    old, new, calls = _two_repositories(adding, monkeypatch)
    (adding.plain / 'ha-token.txt').write_bytes(Path(adding.source).read_bytes())
    raw = json.loads(adding.config_path.read_text());raw['machines']['testbox']['profiles'] = []
    adding.config_path.write_text(json.dumps(raw))
    result = convergence.converge(adding.config_path, adding.data_dir)
    assert not result.failures and result.removed == 2 and result.written == 0
    assert not (adding.plain / 'old-orphan.txt').exists()
    assert not (adding.plain / 'ha-token.txt').exists()


def test_initial_authoring_clone_adds_no_binding_query(adding, monkeypatch):
    from test_sync_view import _birth_consumer, _birth_environment
    root, remote, data, calls = _birth_environment(adding, monkeypatch, 'empty', 'file-url', False)
    actual = repository.subprocess.run
    queries = []
    def querying(argv, **kwargs):
        if argv == BIND_QUERY:
            queries.append(argv)
        return actual(argv, **kwargs)
    monkeypatch.setattr(repository.subprocess, 'run', querying)
    code = adding.cli.main(['init'])
    author_queries = list(queries)
    consumer = _birth_consumer(adding, root, remote)
    assert code == 0 and author_queries == [] and consumer['failures'] == 0


def test_binding_query_preserves_shared_git_environment_guards(adding, monkeypatch):
    _seed_author(adding)
    declaration = json.loads(adding.config_path.read_text())['repo']
    actual = repository.subprocess.run
    observed = []
    def querying(argv, **kwargs):
        if argv == BIND_QUERY:
            env = kwargs['env']
            assert kwargs['cwd'] == str(adding.clone) and kwargs['timeout'] == 10
            assert kwargs['stdout'] == kwargs['stderr'] == subprocess.PIPE
            assert all(name not in env for name in repository._RELOCATING_ENV + repository._INJECTING_ENV)
            assert all(name not in env for name in ['GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0'])
            assert env['GIT_CONFIG_GLOBAL'] == env['GIT_CONFIG_SYSTEM'] == os.devnull
            assert env['GIT_TERMINAL_PROMPT'] == '0' and env['GIT_SSH_COMMAND'] == 'dummy retained SSH command'
            observed.append(True)
            return subprocess.CompletedProcess(argv, 0, stdout=declaration.encode() + b'\0', stderr=b'dummy controlled warning')
        return actual(argv, **kwargs)
    with monkeypatch.context() as context:
        for name in repository._RELOCATING_ENV + repository._INJECTING_ENV:
            context.setenv(name, 'dummy environment injection')
        context.setenv('GIT_CONFIG_COUNT', '1')
        context.setenv('GIT_CONFIG_KEY_0', 'remote.origin.url')
        context.setenv('GIT_CONFIG_VALUE_0', 'dummy injected origin')
        context.setenv('GIT_CONFIG_GLOBAL', os.devnull);context.setenv('GIT_CONFIG_SYSTEM', os.devnull)
        context.setenv('GIT_TERMINAL_PROMPT', '1');context.setenv('GIT_SSH_COMMAND', 'dummy retained SSH command')
        context.setattr(repository.subprocess, 'run', querying)
        code, unused = _operation(adding, 'remove')
    assert code == 1 and observed == [True]


def test_unchanged_hash_convergence_preserves_steady_state(adding, monkeypatch):
    old, new, calls = _two_repositories(adding, monkeypatch)
    first = convergence.converge(adding.config_path, adding.data_dir)
    calls.clear()
    actual = repository.subprocess.run
    queries = []
    def querying(argv, **kwargs):
        if argv == BIND_QUERY:
            queries.append(argv)
        return actual(argv, **kwargs)
    monkeypatch.setattr(repository.subprocess, 'run', querying)
    second = convergence.converge(adding.config_path, adding.data_dir)
    assert first.written == first.removed == 1 and not first.failures
    assert second.ok == 1 and second.written == second.removed == 0 and not second.failures and calls == []
    if queries:
        assert queries == [BIND_QUERY]


@pytest.mark.parametrize('suffix', [' ', '\t', '\n', '\r\n', chr(0xa0), chr(0x2028), ' dummy trailing data '])
def test_actual_git_origin_record_preserves_whitespace_data(adding, monkeypatch, suffix):
    _seed_author(adding)
    old = _git(adding.clone, 'config', '--local', '--get', 'remote.origin.url')
    value = old + suffix
    _git(adding.clone, 'config', '--local', 'remote.origin.url', value)
    raw = json.loads(adding.config_path.read_text());raw['repo'] = value
    adding.config_path.write_text(json.dumps(raw))
    calls = _strict_crypto(adding, monkeypatch)
    calls.clear()
    code, unused = _operation(adding, 'unlock')
    assert code == 0 and calls == ['unwrap']
    assert (adding.data_dir / 'identity.txt').read_bytes() == b'dummy cached old identity\n'
