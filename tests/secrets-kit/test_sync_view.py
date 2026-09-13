"""Actual fetched Git views, public authoring effects and stale-read controls."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from secrets_kit import SecretsError, agefile
from secrets_kit import converge as convergence
from secrets_kit import repo as repository
from test_dest_guard import _armored, _templates, adding
from test_repo import _commit, _fleet_git_template, fleet_git


@pytest.fixture(autouse=True)
def actual_subject_origins():
    root = Path(__file__).resolve().parents[2]
    for name, module in list(sys.modules.items()):
        if name == 'secrets_kit' or name.startswith('secrets_kit.'):
            assert Path(module.__file__).resolve().parent == root / 'plugins/secrets-kit/lib/secrets_kit'
    assert Path(repository.sync.__code__.co_filename).resolve() == root / 'plugins/secrets-kit/lib/secrets_kit/repo.py'
    assert Path(sys.modules['sk_testlib'].__file__).resolve() == root / 'tests/secrets-kit/sk_testlib.py'


def _git(path, *args):
    return subprocess.run(['git', *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


UPSTREAM = ['rev-parse', '--verify', '--quiet', '@{u}']
COUNTS = ['rev-list', '--left-right', '--count', 'HEAD...@{u}']


def _comparison_fault(monkeypatch, clone, *, query=COUNTS, code=124, output='controlled local query failure'):
    actual = repository._git
    observations = []
    assert Path(actual.__code__.co_filename).resolve() == Path(repository.__file__).resolve()
    def querying(args, *, cwd, timeout):
        if cwd == clone and args == query:
            observations.append({'kind': 'injectedComparison', 'argv': args, 'status': code})
            return code, output
        result = actual(args, cwd=cwd, timeout=timeout)
        if cwd == clone and args[0] == 'fetch':
            observations.append({'kind': 'actualFetch', 'argv': args, 'status': result[0]})
        return result
    monkeypatch.setattr(repository, '_git', querying)
    return observations


@pytest.mark.parametrize('query,code,output', [
    (UPSTREAM, 1, ''), (UPSTREAM, 124, 'deadline'), (UPSTREAM, 127, 'execution failure'),
    (UPSTREAM, 0, ''), (UPSTREAM, 0, 'not-an-object-id'), (UPSTREAM, 0, '0' * 40 + '\n' + '1' * 40),
    (COUNTS, 1, 'error'), (COUNTS, 124, 'deadline'), (COUNTS, 127, 'execution failure'),
    (COUNTS, 0, ''), (COUNTS, 0, '0'), (COUNTS, 0, '0 0 0'),
    (COUNTS, 0, 'x 0'), (COUNTS, 0, '-1 0'), (COUNTS, 0, '0 -1'),
    (COUNTS, 0, '0.0 0'), (COUNTS, 0, 'warning\n0 0'),
])
def test_actual_fetch_unknown_established_view_refuses(fleet_git, monkeypatch, query, code, output):
    _commit(fleet_git.other, 'identity.age', 'dummy newly published identity')
    _git(fleet_git.other, 'push', '--quiet')
    published = _git(fleet_git.other, 'rev-parse', 'HEAD')
    old = _git(fleet_git.author, 'rev-parse', 'HEAD')
    observed = _comparison_fault(monkeypatch, fleet_git.author, query=query, code=code, output=output)
    error = None
    try:
        repository.sync(fleet_git.author)
    except SecretsError as caught:
        error = caught
    fetched = _git(fleet_git.author, 'rev-parse', 'refs/remotes/origin/main')
    head = _git(fleet_git.author, 'rev-parse', 'HEAD')
    print('SYNC_QUERY_TRACE ' + json.dumps({'query': query, 'injectedStatus': code, 'refused': error is not None, 'fetchedPublishedCommit': fetched == published, 'checkoutStayedBehind': head == old, 'observations': observed}))
    assert fetched == published and head == old and old != published
    assert any(row['kind'] == 'actualFetch' and row['status'] == 0 for row in observed)
    assert error is not None and ('author' in str(error).lower() or 'fresh' in str(error).lower())
    assert 'controlled local query failure' not in str(error)


@pytest.mark.parametrize('output', ['+0\t00', '00 +0', '-0 0'])
def test_existing_int_convertible_count_spelling_is_not_restricted(fleet_git, monkeypatch, output):
    _comparison_fault(monkeypatch, fleet_git.author, code=0, output=output)
    repository.sync(fleet_git.author)


def test_actual_ahead_only_checkout_remains_accepted(fleet_git):
    _commit(fleet_git.author, 'dummy-local.txt')
    old = _git(fleet_git.author, 'rev-parse', 'HEAD')
    repository.sync(fleet_git.author)
    assert _git(fleet_git.author, 'rev-parse', 'HEAD') == old


def _seed_author(adding):
    assert adding.cli.main(['add', 'ha-token', '--file', adding.source, '--dest', '${PLAIN}/ha-token.txt', '--profile', 'base']) == 0
    (adding.clone / 'identity.age').write_bytes(_armored(b'age1dummywrap', b'dummy wrapped identity\n'))
    repository.commit_and_push(adding.clone, 'dummy wrapped identity', ['identity.age'])
    identity = adding.data_dir / 'identity.txt'
    identity.write_bytes(b'dummy cached old identity\n');identity.chmod(0o600)
    raw = json.loads(adding.config_path.read_text());raw['machines']['testbox']['profiles'] = ['base']
    adding.config_path.write_text(json.dumps(raw))
    source_hook = Path(__file__).resolve().parents[2] / 'plugins/secrets-kit/hooks/fleet-secrets-pre-commit'
    assert (adding.clone / '.git/hooks/pre-commit').read_bytes() == source_hook.read_bytes()


def _publish_other_metadata(adding):
    remote = _git(adding.clone, 'remote', 'get-url', 'origin')
    other = adding.data_dir.parent / 'other author'
    repository.clone(remote, other)
    _git(other, 'config', 'user.name', 'dummy');_git(other, 'config', 'user.email', 'dummy@example.invalid')
    raw = json.loads((other / 'manifest.json').read_text())
    raw['entries']['ha-token']['mode'] = '0644'
    raw['entries']['ha-token']['doc'] = 'other-inventory.md'
    (other / 'manifest.json').write_text(json.dumps(raw))
    repository.commit_and_push(other, 'dummy other entry metadata change', ['manifest.json'])
    return _git(other, 'rev-parse', 'HEAD')


def _author_snapshot(adding):
    files = [adding.manifest_path, adding.clone / 'identity.age', adding.data_dir / 'identity.txt', *sorted((adding.clone / 'blobs').glob('*'))]
    return {'bytes': {str(path): path.read_bytes() if path.exists() else None for path in files}, 'blobNames': sorted(path.name for path in (adding.clone / 'blobs').glob('*')), 'head': _git(adding.clone, 'rev-parse', 'HEAD'), 'index': _git(adding.clone, 'status', '--porcelain'), 'remoteHead': _git(Path(_git(adding.clone, 'remote', 'get-url', 'origin')), 'rev-parse', 'HEAD')}


def _dummy_crypto(adding, monkeypatch):
    """Dummy crypto and a delegating observer for direct CLI plaintext reads."""
    calls = []
    encrypt = agefile.encrypt_to_recipient
    reading = Path.read_bytes
    def source_reading(path):
        if sys._getframe(1).f_globals.get('__name__') == adding.cli.__name__:
            calls.append('source-read')
        return reading(path)
    def keygen():
        calls.append('keygen');return 'dummy new identity\n', 'age1dummynewrecipient'
    def wrapping(identity, target):
        calls.append('wrap');Path(target).write_bytes(_armored(b'age1dummywrap', identity.encode()));return 0
    def encrypting(recipient, plaintext, target):
        calls.append('encrypt');return encrypt(recipient, plaintext, target)
    def decrypting(identity, blob):
        calls.append('decrypt')
        payload = Path(blob).read_bytes()
        return payload.split(b'\n', 2)[2].removesuffix(b'-----END AGE ENCRYPTED FILE-----\n')
    monkeypatch.setattr(agefile, 'keygen', keygen)
    monkeypatch.setattr(agefile, 'wrap_identity', wrapping)
    monkeypatch.setattr(agefile, 'encrypt_to_recipient', encrypting)
    monkeypatch.setattr(agefile, 'decrypt_with_identity', decrypting)
    monkeypatch.setattr(convergence, 'decrypt_with_identity', decrypting)
    monkeypatch.setattr(Path, 'read_bytes', source_reading)
    return calls


def _fresh_consumer(adding):
    data = adding.data_dir.parent / 'fresh consumer'
    repository.clone(_git(adding.clone, 'remote', 'get-url', 'origin'), data / 'repo')
    (data / 'identity.txt').write_bytes(b'dummy consumer identity\n');(data / 'identity.txt').chmod(0o600)
    result = convergence.converge(adding.config_path, data)
    return {'written': result.written, 'failures': len(result.failures), 'cloneHead': _git(data / 'repo', 'rev-parse', 'HEAD')}


@pytest.mark.parametrize('verb', ['init', 'init-noforce', 'add', 'update', 'remove', 'rotate-identity'])
def test_actual_public_author_refuses_stale_view_before_value_effects(adding, monkeypatch, verb):
    _seed_author(adding)
    published = _publish_other_metadata(adding)
    Path(adding.source).write_bytes(b'replacement controlled dummy\n')
    new_source = adding.data_dir.parent / 'new-entry-source.txt'
    new_source.write_bytes(b'new controlled dummy\n')
    before = _author_snapshot(adding)
    observations = _comparison_fault(monkeypatch, adding.clone)
    calls = _dummy_crypto(adding, monkeypatch)
    arguments = {'init': ['init', '--force'], 'init-noforce': ['init'], 'add': ['add', 'new-entry', '--file', str(new_source), '--dest', '${PLAIN}/new-entry.txt'], 'update': ['add', 'ha-token', '--file', adding.source, '--update'], 'remove': ['remove', 'ha-token'], 'rotate-identity': ['rotate-identity']}[verb]
    assert Path(adding.cli.__file__).resolve() == Path(__file__).resolve().parents[2] / 'plugins/secrets-kit/scripts/secrets_kit_cli.py'
    code = adding.cli.main(arguments)
    after = _author_snapshot(adding)
    author_calls = list(calls)
    fetched = _git(adding.clone, 'rev-parse', 'refs/remotes/origin/main')
    consumer = _fresh_consumer(adding)
    print('SYNC_AUTHOR_CONSUMER_TRACE ' + json.dumps({'verb': verb, 'code': code, 'authorBoundaryCalls': author_calls, 'authorSnapshotUnchanged': before == after, 'fetchedPublishedCommit': fetched == published, 'consumer': consumer, 'observations': observations}))
    assert fetched == published and any(row['kind'] == 'actualFetch' and row['status'] == 0 for row in observations)
    assert code == 1 and after == before and author_calls == []
    assert consumer == {'written': 1, 'failures': 0, 'cloneHead': published}


def test_init_comparison_failure_does_not_invent_failed_seed_history(adding, monkeypatch, capsys):
    _seed_author(adding)
    _comparison_fault(monkeypatch, adding.clone)
    assert adding.cli.main(['init']) == 1
    diagnostic = capsys.readouterr().err
    assert 'unlock' in diagnostic
    assert 'seed attempt' not in diagnostic and 'discarding them loses' not in diagnostic


@pytest.mark.parametrize('offline', [False, True])
def test_actual_unlock_keeps_best_effort_after_sync_refusal(adding, monkeypatch, capsys, offline):
    _seed_author(adding)
    if offline:
        unavailable = str(adding.data_dir / 'unavailable.git')
        _git(adding.clone, 'remote', 'set-url', 'origin', unavailable)
        raw = json.loads(adding.config_path.read_text());raw['repo'] = unavailable
        adding.config_path.write_text(json.dumps(raw))
    else:
        _comparison_fault(monkeypatch, adding.clone)
    calls = []
    def unwrapping(wrapped, target):
        calls.append(Path(wrapped).read_bytes());Path(target).write_bytes(b'dummy unlocked identity\n');return 0
    monkeypatch.setattr(agefile, 'unwrap_identity', unwrapping)
    code = adding.cli.main(['unlock'])
    output = capsys.readouterr().out
    assert code == 0 and len(calls) == 1 and 'continuing on the existing checkout' in output
    assert (adding.data_dir / 'identity.txt').read_bytes() == b'dummy unlocked identity\n'


@pytest.mark.parametrize('failure', ['fetch', 'merge'])
def test_actual_status_refresh_keeps_stale_materialization(adding, monkeypatch, capsys, failure):
    _seed_author(adding)
    _dummy_crypto(adding, monkeypatch)
    if failure == 'fetch':
        unavailable = str(adding.data_dir / 'unavailable.git')
        _git(adding.clone, 'remote', 'set-url', 'origin', unavailable)
        raw = json.loads(adding.config_path.read_text());raw['repo'] = unavailable
        adding.config_path.write_text(json.dumps(raw))
    else:
        _git(adding.clone, 'branch', '--unset-upstream')
    code = adding.cli.main(['status', '--refresh'])
    output = capsys.readouterr().out
    assert code == 0 and (adding.plain / 'ha-token.txt').read_bytes() == Path(adding.source).read_bytes()
    assert 'continuing on the existing' in output


def _birth_environment(adding, monkeypatch, kind, transport, existing):
    root = adding.data_dir.parent / 'birth fixtures'
    root.mkdir()
    remote = root / 'remote.git'
    _git(root, 'init', '--quiet', '--bare', '--initial-branch=main', str(remote))
    for name in ['GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME']:
        monkeypatch.setenv(name, 'isolated dummy author')
    for name in ['GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_EMAIL']:
        monkeypatch.setenv(name, 'dummy@example.invalid')
    if kind != 'empty':
        producer = root / 'producer'
        repository.clone(str(remote), producer)
        _commit(producer, 'dummy.txt', 'isolated dummy initial object\n')
        target = 'refs/heads/main' if kind in ('head', 'bad-head') else ('refs/tags/dummy' if kind == 'tag' else 'refs/notes/dummy')
        source = _git(producer, 'rev-parse', 'HEAD^{tree}') if kind == 'notes-tree' else (_git(producer, 'hash-object', '-w', 'dummy.txt') if kind == 'notes-blob' else 'HEAD')
        _git(producer, 'push', '--quiet', 'origin', source + ':' + target)
        if kind == 'bad-head':
            _git(remote, 'symbolic-ref', 'HEAD', 'refs/heads/missing')
    data = root / 'author data'
    monkeypatch.setattr(adding.cli, 'DATA_DIR', data)
    raw = json.loads(adding.config_path.read_text());raw['repo'] = remote.as_uri() if transport == 'file-url' else str(remote)
    adding.config_path.write_text(json.dumps(raw))
    if existing:
        repository.clone(raw['repo'], data / 'repo')
    calls = _dummy_crypto(adding, monkeypatch)
    return root, remote, data, calls


def _network_observer(monkeypatch):
    actual = repository._git
    rows = []
    def querying(args, *, cwd, timeout):
        result = actual(args, cwd=cwd, timeout=timeout)
        rows.append({'argv': args, 'status': result[0]})
        return result
    monkeypatch.setattr(repository, '_git', querying)
    return rows


def _birth_consumer(adding, root, remote):
    data = root / 'consumer data'
    repository.clone(json.loads(adding.config_path.read_text())['repo'], data / 'repo')
    (data / 'identity.txt').write_bytes(b'dummy consumer identity\n');(data / 'identity.txt').chmod(0o600)
    result = convergence.converge(adding.config_path, data)
    return {'written': result.written, 'failures': len(result.failures), 'manifestPresent': (data / 'repo/manifest.json').is_file()}


@pytest.mark.parametrize('kind', ['empty', 'head', 'tag', 'notes', 'notes-blob', 'notes-tree', 'bad-head'])
@pytest.mark.parametrize('transport', ['local-path', 'file-url'])
@pytest.mark.parametrize('existing', [False, True])
def test_actual_birth_coverage_and_first_publication(adding, monkeypatch, kind, transport, existing):
    root, remote, data, calls = _birth_environment(adding, monkeypatch, kind, transport, existing)
    observed = _network_observer(monkeypatch)
    code = adding.cli.main(['init'])
    author_calls = list(calls)
    author_observed = list(observed)
    refs = _git(data / 'repo', 'for-each-ref', '--format=%(refname)')
    mappings = _git(data / 'repo', 'config', '--local', '--get-all', 'remote.origin.fetch')
    identity_present = (data / 'identity.txt').is_file()
    consumer = _birth_consumer(adding, root, remote)
    print('SYNC_BIRTH_TRACE ' + json.dumps({'kind': kind, 'transport': transport, 'existing': existing, 'code': code, 'authorBoundaryCalls': author_calls, 'identityCached': identity_present, 'refs': refs, 'remainingMappings': mappings, 'authorCommands': author_observed, 'consumer': consumer}))
    assert 'refs/secrets-kit/remote-proof/' not in refs
    assert mappings == '+refs/heads/*:refs/remotes/origin/*'
    network = [row for row in author_observed if row['argv'][0] in ('clone', 'fetch')]
    if not existing:
        assert [row['argv'][0] for row in network] == ['clone']
        if 'remote.origin.fetch=+refs/*:refs/secrets-kit/remote-proof/*' in network[0]['argv']:
            assert '--no-local' in network[0]['argv'] and '--template=' in network[0]['argv']
    if kind in ('empty', 'head'):
        assert code == 0 and author_calls == ['keygen', 'wrap'] and identity_present
        assert _git(remote, 'rev-parse', '--verify', 'HEAD:identity.age')
        assert consumer == {'written': 0, 'failures': 0, 'manifestPresent': True}
    else:
        assert code == 1 and author_calls == [] and not identity_present
        assert not (data / 'repo/manifest.json').exists() and not (data / 'repo/identity.age').exists()


@pytest.mark.parametrize('existing', [False, True])
def test_actual_unborn_reentry_after_wrap_failure(adding, monkeypatch, existing):
    root, remote, data, calls = _birth_environment(adding, monkeypatch, 'empty', 'file-url', existing)
    wrapping = agefile.wrap_identity
    def failing(identity, target):
        wrapping(identity, target);return 9
    monkeypatch.setattr(agefile, 'wrap_identity', failing)
    first = adding.cli.main(['init'])
    first_refs = _git(data / 'repo', 'for-each-ref', '--format=%(refname)')
    monkeypatch.setattr(agefile, 'wrap_identity', wrapping)
    second = adding.cli.main(['init'])
    consumer = _birth_consumer(adding, root, remote)
    assert first == 1 and first_refs == '' and second == 0
    assert (data / 'identity.txt').is_file() and consumer['failures'] == 0


@pytest.mark.parametrize('verb', ['add', 'remove', 'rotate-identity'])
def test_new_clone_all_author_entries_validate_before_guard(adding, monkeypatch, verb, capsys):
    root, remote, data, calls = _birth_environment(adding, monkeypatch, 'notes', 'file-url', False)
    observed = _network_observer(monkeypatch)
    arguments = {'add': ['add', 'new-entry', '--file', adding.source, '--dest', '${PLAIN}/new-entry.txt'], 'remove': ['remove', 'new-entry'], 'rotate-identity': ['rotate-identity']}[verb]
    code = adding.cli.main(arguments)
    author_commands = list(observed)
    author_calls = list(calls)
    diagnostic = capsys.readouterr().err
    consumer = _birth_consumer(adding, root, remote)
    assert code == 1 and author_calls == [] and 'author' in diagnostic.lower()
    assert not (data / 'repo/.git/hooks/pre-commit').exists()
    assert [row['argv'][0] for row in author_commands if row['argv'][0] in ('clone', 'fetch')] == ['clone']
    assert not (data / 'identity.txt').exists() and not consumer['manifestPresent']


@pytest.mark.parametrize('detached', [False, True])
def test_actual_established_without_upstream_refuses(fleet_git, detached):
    if detached:
        _git(fleet_git.author, 'update-ref', '--no-deref', 'HEAD', _git(fleet_git.author, 'rev-parse', 'HEAD'))
    else:
        _git(fleet_git.author, 'branch', '--unset-upstream')
    with pytest.raises(SecretsError):
        repository.sync(fleet_git.author)


@pytest.mark.parametrize('fault', ['head', 'symbolic', 'branch-format', 'pre-inventory', 'partial-fetch', 'ref-delete', 'ref-changed'])
def test_existing_unborn_faults_preserve_custody(adding, monkeypatch, fault, capsys):
    root, remote, data, calls = _birth_environment(adding, monkeypatch, 'notes', 'file-url', True)
    actual = repository._git
    rows = []
    fired = []
    def querying(args, *, cwd, timeout):
        rows.append(args)
        target = {'head': ['rev-parse', '--verify', '--quiet', 'HEAD^{commit}'], 'symbolic': ['symbolic-ref', '--quiet', 'HEAD'], 'branch-format': ['check-ref-format', 'refs/heads/main'], 'pre-inventory': ['for-each-ref', '--format=%(refname)%09%(objectname)']}.get(fault)
        if args == target and cwd == data / 'repo':
            fired.append(fault);return 124, 'controlled query failure'
        if args[0] == 'update-ref' and fault in ('ref-delete', 'ref-changed'):
            fired.append(fault)
            if fault == 'ref-delete':
                return 124, 'controlled deletion failure'
            tree = _git(cwd, 'rev-parse', args[-1] + '^{tree}')
            _git(cwd, 'update-ref', args[-2], tree)
        result = actual(args, cwd=cwd, timeout=timeout)
        if args[0] == 'fetch' and fault == 'partial-fetch':
            assert result[0] == 0
            fired.append(fault);return 124, 'controlled failure after real ref acquisition'
        return result
    monkeypatch.setattr(repository, '_git', querying)
    code = adding.cli.main(['init'])
    author_calls = list(calls)
    refs = _git(data / 'repo', 'for-each-ref', '--format=%(refname)')
    diagnostic = capsys.readouterr().err
    monkeypatch.setattr(repository, '_git', actual)
    consumer = _birth_consumer(adding, root, remote)
    print('SYNC_EXISTING_FAULT_TRACE ' + json.dumps({'fault': fault, 'fired': fired, 'code': code, 'authorBoundaryCalls': author_calls, 'refs': refs, 'consumer': consumer}))
    assert code == 1 and author_calls == [] and fired and not (data / 'identity.txt').exists()
    if fault in ('ref-delete', 'ref-changed'):
        assert 'refs/secrets-kit/remote-proof/notes/dummy' in refs and 'cleanup incomplete' in diagnostic.lower()
        deletion = next(args for args in rows if args[0] == 'update-ref')
        assert deletion[:3] == ['update-ref', '--no-deref', '-d'] and len(deletion) == 5
        if fault == 'ref-changed':
            assert _git(data / 'repo', 'rev-parse', deletion[-2]) != deletion[-1]
    else:
        assert 'refs/secrets-kit/remote-proof/' not in refs


@pytest.mark.parametrize('fault', ['partial-clone', 'effective-config', 'duplicate-local-config', 'config-remove', 'config-readback', 'inventory'])
def test_new_clone_cleanup_failure_and_exact_ownership(adding, monkeypatch, fault, capsys):
    root, remote, data, calls = _birth_environment(adding, monkeypatch, 'head', 'file-url', False)
    actual = repository._git
    rows = []
    fired = []
    removed = []
    mapping = '+refs/*:refs/secrets-kit/remote-proof/*'
    def querying(args, *, cwd, timeout):
        rows.append(args)
        if args == ['config', '--get-all', 'remote.origin.fetch'] and fault == 'effective-config':
            fired.append(fault);return 124, 'controlled effective config failure'
        if args[:3] == ['config', '--local', '--unset-all']:
            if fault == 'config-remove':
                fired.append(fault);return 127, 'controlled config removal failure'
            removed.append(True)
        if args == ['config', '--local', '--get-all', 'remote.origin.fetch'] and removed and fault == 'config-readback':
            fired.append(fault);return 0, '+refs/heads/*:refs/remotes/origin/*\ncontrolled unexpected value'
        if args == ['for-each-ref', '--format=%(refname)%09%(objectname)'] and fault == 'inventory':
            fired.append(fault);return 0, 'refs/secrets-kit/remote-proof/heads/main\tbad-object-id'
        result = actual(args, cwd=cwd, timeout=timeout)
        if args[0] == 'clone':
            assert result[0] == 0
            if fault == 'partial-clone':
                fired.append(fault);return 124, 'controlled failure after actual clone completion'
            if fault == 'duplicate-local-config':
                fired.append(fault);_git(data / 'repo', 'config', '--local', '--add', 'remote.origin.fetch', mapping)
        return result
    monkeypatch.setattr(repository, '_git', querying)
    code = adding.cli.main(['init'])
    author_calls = list(calls)
    author_rows = list(rows)
    diagnostic = capsys.readouterr().err
    refs = _git(data / 'repo', 'for-each-ref', '--format=%(refname)')
    values = _git(data / 'repo', 'config', '--local', '--get-all', 'remote.origin.fetch').splitlines()
    monkeypatch.setattr(repository, '_git', actual)
    consumer = _birth_consumer(adding, root, remote)
    print('SYNC_NEW_CLONE_FAULT_TRACE ' + json.dumps({'fault': fault, 'code': code, 'authorBoundaryCalls': author_calls, 'mappingCount': values.count(mapping), 'refs': refs, 'consumer': consumer}))
    assert code == 1 and author_calls == [] and fired and not (data / 'identity.txt').exists()
    assert 'refs/heads/main' in refs and 'refs/remotes/origin/main' in refs
    assert not (data / 'repo/manifest.json').exists() and not (data / 'repo/identity.age').exists()
    if fault in ('duplicate-local-config', 'config-remove'):
        assert values.count(mapping) == (2 if fault == 'duplicate-local-config' else 1)
        assert 'cleanup incomplete' in diagnostic.lower()
    else:
        assert values == ['+refs/heads/*:refs/remotes/origin/*']
    if fault == 'inventory':
        assert 'refs/secrets-kit/remote-proof/' in refs
        assert not any(args[0] == 'update-ref' for args in author_rows)
    else:
        assert 'refs/secrets-kit/remote-proof/' not in refs


@pytest.mark.parametrize('fault', ['comparison', 'guard', 'fetch', 'divergence'])
def test_actual_init_error_advice_names_cached_evidence_without_disposable_history(adding, monkeypatch, fault, capsys):
    _seed_author(adding)
    if fault == 'comparison':
        _comparison_fault(monkeypatch, adding.clone)
    elif fault == 'guard':
        def refusing(clone):
            raise SecretsError('controlled local guard refusal')
        monkeypatch.setattr(adding.cli.guard, 'require_guard', refusing)
    elif fault == 'fetch':
        unavailable = str(adding.data_dir / 'unavailable.git')
        _git(adding.clone, 'remote', 'set-url', 'origin', unavailable)
        raw = json.loads(adding.config_path.read_text());raw['repo'] = unavailable
        adding.config_path.write_text(json.dumps(raw))
    else:
        _publish_other_metadata(adding)
        _commit(adding.clone, 'blobs/dummy-local.age', _armored(b'age1testrecipient', b'dummy local encrypted object\n').decode())
    code = adding.cli.main(['init'])
    diagnostic = capsys.readouterr().err
    assert code == 1 and 'cached remote-tracking view' in diagnostic and 'unlock' in diagnostic
    assert 'seed attempt' not in diagnostic and 'safe to throw away' not in diagnostic and 'always a FAILED' not in diagnostic


@pytest.mark.parametrize('separator', [0x85, 0x2028])
def test_initial_headed_clone_preserves_valid_unicode_ref_characters(adding, monkeypatch, separator):
    root, remote, data, calls = _birth_environment(adding, monkeypatch, 'head', 'file-url', False)
    producer = root / 'producer'
    name = 'refs/notes/dummy' + chr(separator) + 'suffix'
    _git(producer, 'check-ref-format', name)
    _git(producer, 'push', '--quiet', 'origin', 'HEAD:' + name)
    code = adding.cli.main(['init'])
    author_calls = list(calls)
    consumer = _birth_consumer(adding, root, remote)
    assert code == 0 and author_calls == ['keygen', 'wrap']
    assert consumer == {'written': 0, 'failures': 0, 'manifestPresent': True}
    assert 'refs/secrets-kit/remote-proof/' not in _git(data / 'repo', 'for-each-ref', '--format=%(refname)')


def test_actual_empty_remote_with_unicode_whitespace_branch_is_valid(adding, monkeypatch):
    root, remote, data, calls = _birth_environment(adding, monkeypatch, 'empty', 'file-url', False)
    branch = 'refs/heads/main.' + chr(0xa0)
    _git(remote, 'check-ref-format', branch)
    _git(remote, 'symbolic-ref', 'HEAD', branch)
    code = adding.cli.main(['init'])
    author_calls = list(calls)
    consumer = _birth_consumer(adding, root, remote)
    assert code == 0 and author_calls == ['keygen', 'wrap']
    assert consumer == {'written': 0, 'failures': 0, 'manifestPresent': True}
