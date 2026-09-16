"""Entry authoring faults preserve owned epochs before an actual clean retry."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from secrets_kit import agefile, authoring
from secrets_kit.manifest import Manifest
from test_dest_guard import _templates, adding
from test_init import _seeding_template, seeding
from test_seed_recovery import _consumer, _snapshot, pending_subject


@pytest.fixture
def entry_subject(pending_subject: SimpleNamespace) -> SimpleNamespace:
    subject = pending_subject
    source = subject.data_dir.parent / 'replacement dummy source.txt'
    source.write_bytes(b'dummy replacement entry value\n')
    subject.entry_source = source
    return subject


def _arguments(subject: SimpleNamespace, operation: str) -> list[str]:
    if operation == 'remove':
        return ['remove', 'ha-token']
    common = ['add', 'third-entry' if operation == 'add' else 'ha-token',
              '--file', str(subject.entry_source), '--doc', 'dummy replacement docs']
    return common + (['--dest', '${PLAIN}/third.txt', '--profile', 'base']
                     if operation == 'add' else ['--update'])


def _invoke_entry(subject: SimpleNamespace, operation: str, *, direct: bool = False) -> dict[str, Any]:
    arguments = _arguments(subject, operation)
    try:
        if direct:
            args = subject.cli.build_parser().parse_args(arguments)
            code = args.func(args)
        else:
            code = subject.cli.main(arguments)
        return {'code': code, 'exception': None}
    except BaseException as error:
        return {'code': None, 'exception': error}


def _selected_blob(subject: SimpleNamespace, operation: str) -> Path:
    manifest = Manifest.load(subject.clone / 'manifest.json')
    relative = ('blobs/' + subject.entry_source.name + '.age' if operation == 'add'
                else manifest.entries['ha-token'].blob)
    return subject.clone / relative


def _entry_fault(subject: SimpleNamespace, controlled: pytest.MonkeyPatch,
                 operation: str, fault: str) -> tuple[BaseException, list[dict[str, Any]]]:
    primary = OSError('dummy remove unlink refusal') if fault == 'unlink' else RuntimeError('dummy entry preparation failure')
    observations: list[dict[str, Any]] = []
    target = _selected_blob(subject, operation)
    real_encrypt = agefile.encrypt_to_recipient

    def encrypt(recipient: str, plaintext: bytes, output: Path) -> None:
        observations.append({'step': 'encrypt', 'outputIsCheckoutTarget': output == target})
        real_encrypt(recipient, plaintext, output)
        if fault == 'partial-encrypt':
            output.write_bytes(b'dummy incomplete encrypted output')
            raise primary

    controlled.setattr(agefile, 'encrypt_to_recipient', encrypt)
    if fault == 'serialize':
        real_dump = Manifest.dump

        def dump(manifest: Manifest) -> str:
            changed = ('third-entry' in manifest.entries if operation == 'add'
                       else manifest.entries['ha-token'].doc == 'dummy replacement docs')
            if changed:
                observations.append({'step': 'rewritten-manifest-serialize'})
                raise primary
            return real_dump(manifest)

        controlled.setattr(Manifest, 'dump', dump)
    elif fault == 'unlink':
        real_unlink = Path.unlink

        def unlink(path: Path, *args: Any, **kwargs: Any) -> None:
            if path == target:
                observations.append({'step': 'selected-blob-unlink'})
                raise primary
            real_unlink(path, *args, **kwargs)

        controlled.setattr(Path, 'unlink', unlink)
    return primary, observations


@pytest.mark.parametrize('direct', [False, True], ids=['main', 'direct'])
@pytest.mark.parametrize('operation,fault', [('add', 'serialize'), ('update', 'serialize'),
                                          ('add', 'partial-encrypt'), ('update', 'partial-encrypt'),
                                          ('remove', 'unlink')])
def test_actual_entry_preparation_or_deletion_fault_restores_before_retry(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], operation: str, fault: str, direct: bool,
) -> None:
    subject = entry_subject
    initial_peer = _consumer(subject, 'initial entry epoch')
    before = _snapshot(subject)
    with monkeypatch.context() as controlled:
        primary, observations = _entry_fault(subject, controlled, operation, fault)
        outcome = _invoke_entry(subject, operation, direct=direct)
        output = capsys.readouterr()
    after = _snapshot(subject)
    print('ENTRY_FAULT_BEFORE_RETRY ' + json.dumps({
        'operation': operation, 'fault': fault, 'direct': direct,
        'code': outcome['code'], 'primaryIdentityHeld': outcome['exception'] is primary,
        'snapshotHeld': before == after, 'remoteHeld': before['remoteHead'] == after['remoteHead'],
        'observations': observations,
    }))
    old_peer = _consumer(subject, 'entry fault before retry')
    retry = _invoke_entry(subject, operation)
    peer = _consumer(subject, 'entry fault after retry')
    print('ENTRY_FAULT_AFTER_RETRY ' + json.dumps({
        'operation': operation, 'fault': fault, 'retryCode': retry['code'],
        'peerValues': {k: v.decode() for k, v in peer['values'].items()},
        'oldPeerValues': {k: v.decode() for k, v in old_peer['values'].items()},
    }))
    assert observations and before == after
    assert outcome['exception'] is primary
    assert old_peer['failures'] == 0 and old_peer['values'] == initial_peer['values']
    assert retry['code'] == 0 and peer['failures'] == 0
    if operation == 'remove':
        assert 'ha-token.txt' not in peer['values'] and len(peer['values']) == 1
        from secrets_kit import converge as convergence
        convergence_result = convergence.converge(subject.config_path, subject.data_dir)
        assert convergence_result.removed == 1 and not convergence_result.failures
        assert not (subject.plain / 'ha-token.txt').exists()
    else:
        expected = 'third.txt' if operation == 'add' else 'ha-token.txt'
        assert peer['values'][expected] == b'dummy replacement entry value\n'
    assert not (subject.data_dir / 'authoring-recovery').exists()
    assert 'Nothing was published' not in output.err


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
@pytest.mark.parametrize('dirty', ['staged-change', 'unstaged-change', 'staged-delete', 'staged-untracking', 'untracked', 'index-lock', 'merge-head', 'rebase-state', 'foreign-active-hook'])
def test_entry_foreign_work_refuses_before_sync_or_crypto(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str, dirty: str,
) -> None:
    from secrets_kit import repo
    from test_sync_view import _git
    subject = entry_subject;path = subject.clone / 'manifest.json';baseline = path.read_bytes()
    controlled = None
    if dirty in ['staged-change', 'unstaged-change']:
        path.write_bytes(baseline + b'\n')
        if dirty == 'staged-change':_git(subject.clone, 'add', '--', 'manifest.json')
    elif dirty == 'staged-delete':_git(subject.clone, 'rm', '--quiet', '--', 'manifest.json')
    elif dirty == 'staged-untracking':_git(subject.clone, 'rm', '--cached', '--quiet', '--', 'manifest.json')
    else:
        controlled = subject.clone / {'untracked': 'unowned.age', 'index-lock': '.git/index.lock', 'merge-head': '.git/MERGE_HEAD', 'rebase-state': '.git/rebase-merge', 'foreign-active-hook': '.git/hooks/prepare-commit-msg'}[dirty]
        if dirty == 'rebase-state':controlled.mkdir()
        else:
            controlled.write_bytes((_git(subject.clone, 'rev-parse', 'HEAD') + '\n').encode() if dirty == 'merge-head' else b'#!/bin/sh\nexit 0\n' if dirty == 'foreign-active-hook' else b'dummy foreign work')
            if dirty == 'foreign-active-hook':controlled.chmod(0o700)
    before = _snapshot(subject);subject.calls.clear();real_git = repo._git;queries = []
    with monkeypatch.context() as observing:
        def git(args: list[str], *, cwd: Path, timeout: int) -> Any:
            if cwd == subject.clone:queries.append(args[0])
            return real_git(args, cwd=cwd, timeout=timeout)
        observing.setattr(repo, '_git', git)
        outcome = _invoke_entry(subject, operation)
    after = _snapshot(subject);calls = list(subject.calls)
    print('ENTRY_ADMISSION_BEFORE_RETRY ' + json.dumps({'operation': operation, 'dirty': dirty, 'code': outcome['code'], 'snapshotHeld': before == after, 'queries': queries, 'calls': calls}))
    if controlled is not None:
        if controlled.is_dir():controlled.rmdir()
        else:controlled.unlink()
    else:
        _git(subject.clone, 'reset', '--quiet', 'HEAD', '--', 'manifest.json');path.write_bytes(baseline)
    old_peer = _consumer(subject, 'foreign work before retry')
    retry = _invoke_entry(subject, operation);peer = _consumer(subject, 'foreign work after retry')
    assert outcome['code'] == 1 and before == after and calls == []
    assert not any(q in ['fetch', 'merge', 'add', 'commit', 'push', 'reset', 'stash', 'clean'] for q in queries)
    assert old_peer['failures'] == peer['failures'] == 0 and retry['code'] == 0
    assert not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
@pytest.mark.parametrize('corruption', ['version', 'operation', 'phase', 'seed-output', 'null-manifest', 'duplicate-preimage', 'unsupported-path', 'reason', 'tree', 'foreign-artifact', 'modified-preimage'])
def test_entry_malformed_private_record_refuses_without_clearing_evidence(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str, corruption: str,
) -> None:
    from secrets_kit import authoring
    subject = entry_subject;_pending_entry(subject, monkeypatch, operation)
    owner = authoring._load_operation(subject.data_dir)
    original = json.loads(json.dumps(owner.record));record = json.loads(json.dumps(original))
    recovery = subject.data_dir / 'authoring-recovery';extra = None;changed = None
    if corruption == 'version':record['version'] = 1
    elif corruption == 'operation':record['operation'] = 'seed'
    elif corruption == 'phase':record['phase'] = 'unknown-private-phase'
    elif corruption == 'seed-output':record['outputs']['identity.age'] = record['outputs']['manifest.json']
    elif corruption == 'null-manifest':record['outputs']['manifest.json'] = None
    elif corruption == 'duplicate-preimage':
        duplicate = 'blobs/duplicate-private-receipt.age'
        record['preimages'][duplicate] = dict(next(s for s in record['preimages'].values() if s))
        record['synced_files'][duplicate] = None
    elif corruption == 'unsupported-path':record['selected_blob'] = 'blobs/../dummy.age'
    elif corruption == 'reason':record['publication_reason'] = 'dummy arbitrary sensitive marker text'
    elif corruption == 'tree':record['expected_tree'] = record['entry_head']
    elif corruption == 'foreign-artifact':
        extra = recovery / 'foreign-dummy-file';extra.write_bytes(b'dummy foreign bytes');extra.chmod(0o600)
    else:
        changed = recovery / next(s['stored'] for s in record['preimages'].values() if s)
        previous = changed.read_bytes();changed.write_bytes(b'dummy substituted preimage')
    if corruption not in ['foreign-artifact', 'modified-preimage']:owner._persist(record)
    before = _snapshot(subject);artifacts = {p.name: p.read_bytes() for p in recovery.iterdir()}
    with pytest.raises(authoring.AuthoringRecoveryError):authoring._inspect_recovery(subject.data_dir)
    with pytest.raises(authoring.AuthoringRecoveryError):authoring._reconcile_recovery(subject.data_dir)
    assert before == _snapshot(subject) and artifacts == {p.name: p.read_bytes() for p in recovery.iterdir()}
    if extra:extra.unlink()
    if changed:changed.write_bytes(previous)
    owner._persist(original)
    assert authoring._reconcile_recovery(subject.data_dir)['outcome'] == 'confirmed'
    assert not recovery.exists()



def _pending_entry(subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str) -> dict[str, Any]:
    from test_seed_recovery import _lose_push_report
    with monkeypatch.context() as controlled:
        observations = _lose_push_report(subject, controlled, proof_failure=True)
        outcome = _invoke_entry(subject, operation)
    assert outcome['code'] == 1 and observations['pushes'] == [0]
    return json.loads((subject.data_dir / 'authoring-recovery/marker.json').read_bytes())


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
@pytest.mark.parametrize('change', ['bytes', 'mode', 'appearance', 'disappearance'])
def test_entry_private_reconciliation_requires_exact_entry_cache(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str, change: str,
) -> None:
    from secrets_kit import authoring
    subject = entry_subject
    cache = subject.data_dir / 'identity.txt'
    original = cache.read_bytes(), cache.stat().st_mode & 0o777
    if change == 'appearance':cache.unlink()
    _pending_entry(subject, monkeypatch, operation)
    if change in ['bytes', 'appearance']:cache.write_bytes(b'dummy incompatible cache\n');cache.chmod(0o600)
    elif change == 'mode':cache.chmod(0o400)
    else:cache.unlink()
    before = _snapshot(subject)
    artifacts = {p.name: p.read_bytes() for p in (subject.data_dir / 'authoring-recovery').iterdir()}
    inspection = authoring._inspect_recovery(subject.data_dir)
    with pytest.raises(authoring.AuthoringRecoveryError):authoring._reconcile_recovery(subject.data_dir)
    assert not inspection['cache_compatible'] and before == _snapshot(subject)
    assert artifacts == {p.name: p.read_bytes() for p in (subject.data_dir / 'authoring-recovery').iterdir()}
    if change == 'appearance':cache.unlink()
    else:
        if cache.exists():cache.chmod(original[1])
        cache.write_bytes(original[0]);cache.chmod(original[1])
    assert authoring._reconcile_recovery(subject.data_dir)['outcome'] == 'confirmed'
    assert not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('operation', ['update', 'remove'])
def test_entry_remap_to_unchanged_blob_is_captured_before_fast_forward(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    from secrets_kit import repo
    from test_sync_view import _git
    subject = entry_subject
    incoming = subject.data_dir.parent / 'incoming remap'
    repo.clone(_git(subject.clone, 'remote', 'get-url', 'origin'), incoming)
    _git(incoming, 'config', 'user.name', 'dummy');_git(incoming, 'config', 'user.email', 'dummy@example.invalid')
    raw = json.loads((incoming / 'manifest.json').read_bytes())
    old_a = raw['entries']['ha-token']['blob']
    unchanged_b = raw['entries'].pop('second-old')['blob']
    raw['entries']['ha-token']['blob'] = unchanged_b
    for names in raw['profiles'].values():
        if 'second-old' in names:names.remove('second-old')
    (incoming / 'manifest.json').write_text(json.dumps(raw))
    added = incoming / 'blobs/incoming-unreferenced.age';added.write_bytes(b'dummy admitted incoming ciphertext')
    _git(incoming, 'add', '--', 'manifest.json', 'blobs/incoming-unreferenced.age')
    _git(incoming, 'commit', '--quiet', '-m', 'dummy remote remap');_git(incoming, 'push', '--quiet')
    assert _git(incoming, 'diff', '--name-only', 'HEAD^', 'HEAD').splitlines() == ['blobs/incoming-unreferenced.age', 'manifest.json']
    before = _snapshot(subject)
    real_git = repo._git;stages = []
    with monkeypatch.context() as controlled:
        def git(args: list[str], *, cwd: Path, timeout: int) -> Any:
            if cwd == subject.clone and args[0] == 'add':stages.append(args[2:])
            if cwd == subject.clone and args[0] == 'commit':return 128, 'dummy remap commit refusal'
            return real_git(args, cwd=cwd, timeout=timeout)
        controlled.setattr(repo, '_git', git)
        outcome = _invoke_entry(subject, operation)
    after = _snapshot(subject)
    print('ENTRY_REMAP_BEFORE_RETRY ' + json.dumps({'operation': operation, 'snapshotHeld': before == after, 'stages': stages, 'oldA': old_a, 'unchangedB': unchanged_b}))
    old_peer = _consumer(subject, 'remap before retry')
    retry = _invoke_entry(subject, operation)
    peer = _consumer(subject, 'remap after retry')
    assert outcome['code'] == 1 and before == after and stages == [[unchanged_b, 'manifest.json']]
    assert old_peer['failures'] == peer['failures'] == 0 and retry['code'] == 0
    assert (subject.clone / old_a).exists() and (subject.clone / 'blobs/incoming-unreferenced.age').exists()
    if operation == 'update':
        assert Manifest.load(subject.clone / 'manifest.json').entries['ha-token'].blob == unchanged_b
        assert peer['values']['ha-token.txt'] == b'dummy replacement entry value\n'
    else:
        assert 'ha-token' not in Manifest.load(subject.clone / 'manifest.json').entries
        assert not (subject.clone / unchanged_b).exists() and peer['values'] == {}


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
@pytest.mark.parametrize('fault', ['partial-add', 'commit-refusal', 'commit-lost-report'])
def test_entry_staging_and_commit_faults_have_exact_tree_and_retry(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str, fault: str,
) -> None:
    from secrets_kit import repo
    from test_sync_view import _git
    subject = entry_subject;before = _snapshot(subject);initial_peer = _consumer(subject, 'commit initial')
    selected = _selected_blob(subject, operation).relative_to(subject.clone).as_posix()
    real_git = repo._git;observed = []
    with monkeypatch.context() as controlled:
        def git(args: list[str], *, cwd: Path, timeout: int) -> Any:
            if cwd == subject.clone and args[0] in ['add', 'commit']:observed.append(args)
            if cwd == subject.clone and args[0] == 'add' and fault == 'partial-add':
                assert real_git(['add', '--', args[-1]], cwd=cwd, timeout=timeout)[0] == 0
                return 128, 'dummy partial stage refusal'
            if cwd == subject.clone and args[0] == 'commit' and fault == 'commit-refusal':return 128, 'dummy commit refusal'
            result = real_git(args, cwd=cwd, timeout=timeout)
            if cwd == subject.clone and args[0] == 'commit' and fault == 'commit-lost-report':
                assert result[0] == 0
                return 124, 'dummy lost completed commit report'
            return result
        controlled.setattr(repo, '_git', git)
        outcome = _invoke_entry(subject, operation)
    after = _snapshot(subject)
    print('ENTRY_COMMIT_BEFORE_RETRY ' + json.dumps({'operation': operation, 'fault': fault, 'code': outcome['code'], 'snapshotHeld': before == after, 'observed': observed}))
    old_peer = _consumer(subject, 'commit before retry')
    retry = _invoke_entry(subject, operation) if fault != 'commit-lost-report' else outcome
    peer = _consumer(subject, 'commit after retry')
    assert observed[0] == ['add', '--', selected, 'manifest.json']
    if fault != 'commit-lost-report':
        assert outcome['code'] == 1 and before == after and old_peer['values'] == initial_peer['values']
    else:assert outcome['code'] == 0 and before['remoteHead'] != after['remoteHead']
    assert retry['code'] == 0 and peer['failures'] == 0
    expected_message = ('add: third-entry' if operation == 'add' else 'rotate: ha-token' if operation == 'update' else 'remove: ha-token')
    assert _git(subject.clone, 'log', '-1', '--format=%s') == expected_message
    assert _git(subject.clone, 'rev-parse', 'HEAD^') == before['head']['out'].decode().strip()
    assert not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
@pytest.mark.parametrize('fault', ['artifact-unlink', 'marker-remove', 'directory-remove'])
def test_entry_published_cleanup_fault_is_privately_resumable_without_push(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str, fault: str,
) -> None:
    from secrets_kit import authoring
    subject = entry_subject;primary = OSError('dummy published entry cleanup failure');observed = []
    real_unlink = Path.unlink;real_rmdir = Path.rmdir
    recovery = subject.data_dir / 'authoring-recovery'
    with monkeypatch.context() as controlled:
        def unlink(path: Path, *args: Any, **kwargs: Any) -> None:
            hit = path.parent == recovery and path.name == ('entry-index' if fault == 'artifact-unlink' else 'marker.json' if fault == 'marker-remove' else '')
            if hit and not observed:observed.append(fault);raise primary
            real_unlink(path, *args, **kwargs)
        def rmdir(path: Path) -> None:
            if path == recovery and fault == 'directory-remove' and not observed:observed.append(fault);raise primary
            real_rmdir(path)
        controlled.setattr(Path, 'unlink', unlink);controlled.setattr(Path, 'rmdir', rmdir)
        outcome = _invoke_entry(subject, operation)
    assert observed == [fault] and outcome['exception'] is primary and recovery.exists()
    peer = _consumer(subject, 'published cleanup fault ' + fault)
    assert peer['failures'] == 0 and authoring._inspect_recovery(subject.data_dir)['cache_compatible']
    assert authoring._reconcile_recovery(subject.data_dir)['outcome'] == 'confirmed'
    assert not recovery.exists()


def test_identical_entry_update_interrupted_cleanup_resumes_as_unchanged(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from secrets_kit import authoring
    subject = entry_subject;before = _snapshot(subject);real_unlink = Path.unlink;primary = OSError('dummy unchanged cleanup fault')
    with monkeypatch.context() as controlled:
        def unlink(path: Path, *args: Any, **kwargs: Any) -> None:
            if path == subject.data_dir / 'authoring-recovery/entry-index':raise primary
            real_unlink(path, *args, **kwargs)
        controlled.setattr(Path, 'unlink', unlink)
        with pytest.raises(OSError) as caught:subject.cli.main(['add', 'ha-token', '--file', subject.source, '--update'])
    inspection = authoring._inspect_recovery(subject.data_dir)
    assert caught.value is primary and before == _snapshot(subject) and inspection['publication_attempted'] is False
    assert authoring._reconcile_recovery(subject.data_dir)['outcome'] == 'unchanged'
    assert not (subject.data_dir / 'authoring-recovery').exists()

@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
@pytest.mark.parametrize('cache_present', [False, True], ids=['locked', 'cached'])
def test_actual_entry_receive_rejection_has_one_push_and_exact_restoration(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    operation: str, cache_present: bool,
) -> None:
    import subprocess
    from test_sync_view import _git
    subject = entry_subject
    cache = subject.data_dir / 'identity.txt'
    if not cache_present:
        cache.unlink()
    initial_peer = _consumer(subject, 'entry receive initial')
    before = _snapshot(subject)
    remote = subject.remote if hasattr(subject, 'remote') else Path(_git(subject.clone, 'remote', 'get-url', 'origin'))
    hook = remote / 'hooks/pre-receive'
    hook.write_text('#!/bin/sh\necho dummy entry receive rejection >&2\nexit 1\n')
    hook.chmod(0o700)
    real_run = subprocess.run
    pushes = []
    with monkeypatch.context() as observing:
        def run(command: list[str], *args: Any, **kwargs: Any) -> Any:
            result = real_run(command, *args, **kwargs)
            if command[:2] == ['git', 'push'] and Path(kwargs.get('cwd') or '.') == subject.clone:
                pushes.append({'actualCode': result.returncode, 'argv': command[1:]})
            return result
        observing.setattr(subprocess, 'run', run)
        outcome = _invoke_entry(subject, operation)
    after = _snapshot(subject)
    print('ENTRY_RECEIVE_BEFORE_RETRY ' + json.dumps({'operation': operation, 'cachePresent': cache_present,
          'code': outcome['code'], 'snapshotHeld': before == after, 'pushes': pushes}))
    hook.unlink()
    old_peer = _consumer(subject, 'entry receive before retry')
    retry = _invoke_entry(subject, operation)
    peer = _consumer(subject, 'entry receive after retry')
    assert outcome['code'] == 1 and before == after and len(pushes) == 1 and pushes[0]['actualCode'] != 0
    assert all(not flag.startswith('--force') for p in pushes for flag in p['argv'])
    assert old_peer['failures'] == 0 and old_peer['values'] == initial_peer['values']
    assert retry['code'] == 0 and peer['failures'] == 0
    assert cache.exists() == cache_present and not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
@pytest.mark.parametrize('proof_failure', [False, True], ids=['confirmed', 'uncertain'])
@pytest.mark.parametrize('cache_present', [False, True], ids=['locked', 'cached'])
def test_actual_entry_lost_successful_report_and_private_reconciliation(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
    operation: str, proof_failure: bool, cache_present: bool,
) -> None:
    import subprocess
    from secrets_kit import authoring
    from test_seed_recovery import PENDING_CALLERS, _lose_push_report, _pending_call
    subject = entry_subject
    cache = subject.data_dir / 'identity.txt'
    if not cache_present:
        cache.unlink()
    old_cache = (cache.read_bytes(), cache.stat().st_mode & 0o777) if cache.exists() else None
    before = _snapshot(subject)
    with monkeypatch.context() as controlled:
        observations = _lose_push_report(subject, controlled, proof_failure=proof_failure)
        outcome = _invoke_entry(subject, operation)
    after = _snapshot(subject)
    recovery = subject.data_dir / 'authoring-recovery'
    marker = recovery / 'marker.json'
    record = json.loads(marker.read_bytes()) if marker.exists() else None
    print('ENTRY_LOST_REPORT_BEFORE_RECONCILE ' + json.dumps({'operation': operation,
          'cachePresent': cache_present, 'proofFailure': proof_failure, 'code': outcome['code'],
          'observations': observations, 'remoteChanged': before['remoteHead'] != after['remoteHead'],
          'markerPresent': marker.exists(), 'phase': record['phase'] if record else None}))
    peer = _consumer(subject, 'entry lost successful report peer')
    if proof_failure and marker.exists():
        protected = _snapshot(subject)
        real_run = subprocess.run
        commands = []
        with monkeypatch.context() as observing:
            def run(command: list[str], *args: Any, **kwargs: Any) -> Any:
                if command[0] == 'git' and Path(kwargs.get('cwd') or '.') == subject.clone:
                    commands.append(command)
                return real_run(command, *args, **kwargs)
            observing.setattr(subprocess, 'run', run)
            refusals = [_pending_call(subject, caller)[0] for caller in PENDING_CALLERS]
        assert all(code == 1 for code in refusals) and not commands and protected == _snapshot(subject)
        inspection = authoring._inspect_recovery(subject.data_dir)
        assert inspection['operation'] == operation and inspection['cache_compatible']
        reconciled = authoring._reconcile_recovery(subject.data_dir)
        assert reconciled['outcome'] == 'confirmed' and reconciled['recovery'] == 'cleared'
    assert len(observations['pushes']) == 1 and observations['pushes'] == [0] and len(observations['proofs']) == 1
    assert outcome['code'] == (1 if proof_failure else 0) and before['remoteHead'] != after['remoteHead']
    assert peer['failures'] == 0
    assert ((cache.read_bytes(), cache.stat().st_mode & 0o777) if cache.exists() else None) == old_cache
    assert not recovery.exists()


@pytest.mark.parametrize('cache_present', [False, True], ids=['locked', 'cached'])
def test_actual_identical_entry_update_has_no_replacement_commit_or_push(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, cache_present: bool,
) -> None:
    import subprocess
    subject = entry_subject
    cache = subject.data_dir / 'identity.txt'
    if not cache_present:
        cache.unlink()
    before = _snapshot(subject)
    real_run = subprocess.run
    commands = []
    with monkeypatch.context() as observing:
        def run(command: list[str], *args: Any, **kwargs: Any) -> Any:
            if command[:2] in [['git', 'push'], ['git', 'commit']] and Path(kwargs.get('cwd') or '.') == subject.clone:
                commands.append(command)
            return real_run(command, *args, **kwargs)
        observing.setattr(subprocess, 'run', run)
        outcome = subject.cli.main(['add', 'ha-token', '--file', subject.source, '--update'])
    after = _snapshot(subject)
    peer = _consumer(subject, 'entry identical update peer')
    print('ENTRY_UNCHANGED_TRACE ' + json.dumps({'code': outcome, 'commands': commands, 'snapshotHeld': before == after}))
    assert outcome == 0 and not commands and before == after and peer['failures'] == 0
    assert not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('inherited', ['GIT_GLOB_PATHSPECS', 'GIT_NOGLOB_PATHSPECS', 'GIT_ICASE_PATHSPECS'])
def test_entry_literal_blob_filename_is_preserved_under_inherited_pathspec_options(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, inherited: str,
) -> None:
    subject = entry_subject
    source = subject.data_dir.parent / 'dummy [literal]* source.txt';source.write_bytes(b'dummy replacement entry value\n')
    subject.entry_source = source
    with monkeypatch.context() as controlled:
        controlled.setenv(inherited, '1');outcome = _invoke_entry(subject, 'add')
    peer = _consumer(subject, 'literal filename peer')
    assert outcome['code'] == 0 and peer['failures'] == 0
    assert Manifest.load(subject.clone / 'manifest.json').entries['third-entry'].blob == 'blobs/' + source.name + '.age'
    assert peer['values']['third.txt'] == b'dummy replacement entry value\n'


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
@pytest.mark.parametrize('slot', ['directory', 'symlink', 'hardlink'])
def test_entry_unsupported_cache_slot_refuses_reconciliation_without_effects(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str, slot: str,
) -> None:
    import os
    from secrets_kit import authoring
    subject = entry_subject;cache = subject.data_dir / 'identity.txt';original = cache.read_bytes(), cache.stat().st_mode & 0o777
    _pending_entry(subject, monkeypatch, operation);cache.unlink();target = subject.data_dir.parent / 'dummy separate cache slot'
    if slot == 'directory':cache.mkdir()
    else:
        target.write_bytes(original[0]);target.chmod(original[1])
        if slot == 'symlink':cache.symlink_to(target)
        else:os.link(target, cache)
    recovery = subject.data_dir / 'authoring-recovery';artifacts = {p.name: p.read_bytes() for p in recovery.iterdir()}
    with pytest.raises(authoring.AuthoringRecoveryError):authoring._reconcile_recovery(subject.data_dir)
    assert artifacts == {p.name: p.read_bytes() for p in recovery.iterdir()}
    if slot == 'directory':cache.rmdir()
    else:cache.unlink()
    cache.write_bytes(original[0]);cache.chmod(original[1])
    assert authoring._reconcile_recovery(subject.data_dir)['outcome'] == 'confirmed'


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
def test_entry_unsubmitted_record_cannot_address_foreign_proof_ref(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    from secrets_kit import authoring
    subject = entry_subject;_pending_entry(subject, monkeypatch, operation);owner = authoring._load_operation(subject.data_dir)
    original = dict(owner.record);bad = dict(original, phase='unsubmitted', proof_ref='refs/heads/dummy-foreign-proof')
    owner._persist(bad);before = _snapshot(subject)
    recovery = subject.data_dir / 'authoring-recovery';artifacts = {p.name: p.read_bytes() for p in recovery.iterdir()}
    with pytest.raises(authoring.AuthoringRecoveryError):authoring._reconcile_recovery(subject.data_dir)
    assert before == _snapshot(subject) and artifacts == {p.name: p.read_bytes() for p in recovery.iterdir()}
    owner._persist(original);assert authoring._reconcile_recovery(subject.data_dir)['outcome'] == 'confirmed'



def test_entry_failed_add_restores_owned_blob_parent_absence(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from secrets_kit import repo
    subject = entry_subject
    assert subject.cli.main(['remove', 'ha-token']) == subject.cli.main(['remove', 'second-old']) == 0
    parent = subject.clone / 'blobs';parent.rmdir();before = _snapshot(subject);real_git = repo._git
    with monkeypatch.context() as controlled:
        def git(args: list[str], *, cwd: Path, timeout: int) -> Any:
            if cwd == subject.clone and args[0] == 'commit':return 128, 'dummy new parent commit refusal'
            return real_git(args, cwd=cwd, timeout=timeout)
        controlled.setattr(repo, '_git', git);outcome = _invoke_entry(subject, 'add')
    after = _snapshot(subject);parent_absent_before_retry = not parent.exists()
    print('ENTRY_PARENT_BEFORE_RETRY ' + json.dumps({'code': outcome['code'], 'snapshotHeld': before == after, 'parentAbsent': parent_absent_before_retry}))
    old_peer = _consumer(subject, 'absent parent before retry');retry = _invoke_entry(subject, 'add');peer = _consumer(subject, 'absent parent after retry')
    assert outcome['code'] == 1 and before == after and parent_absent_before_retry and parent.is_dir()
    assert old_peer['failures'] == peer['failures'] == 0 and old_peer['values'] == {} and retry['code'] == 0
    assert peer['values'] == {'third.txt': b'dummy replacement entry value\n'}
    assert not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
def test_entry_fast_forward_restoration_cas_failure_retains_then_resumes(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    from secrets_kit import repo
    subject = entry_subject;incoming = _consumer(subject, 'incoming before CAS fault', add_proof=True)
    before = _snapshot(subject);real_git = repo._git;real_query = repo._owned_query;observed = []
    with monkeypatch.context() as controlled:
        def git(args: list[str], *, cwd: Path, timeout: int) -> Any:
            if cwd == subject.clone and args[0] == 'commit':return 128, 'dummy commit refusal before CAS fault'
            return real_git(args, cwd=cwd, timeout=timeout)
        def query(clone: Path, args: list[str], **kwargs: Any) -> bytes:
            if clone == subject.clone and args[0] == 'update-ref' and '-d' not in args:
                observed.append(args);raise OSError('dummy owned restoration CAS unavailable')
            return real_query(clone, args, **kwargs)
        controlled.setattr(repo, '_git', git);controlled.setattr(repo, '_owned_query', query)
        outcome = _invoke_entry(subject, operation)
    assert incoming['failures'] == 0 and outcome['code'] == 1 and observed
    assert (subject.data_dir / 'authoring-recovery').exists()
    assert authoring._reconcile_recovery(subject.data_dir)['recovery'] == 'cleared'
    assert before == _snapshot(subject)
    retry = _invoke_entry(subject, operation);peer = _consumer(subject, 'CAS fault actual retry')
    assert retry['code'] == 0 and peer['failures'] == 0



@pytest.mark.parametrize('selected', ['blobs/nested/dummy.age', 'blobs/../dummy.age', '/dummy-absolute.age', './blobs/dummy.age', 'blobs//dummy.age', 'blobs/./dummy.age'])
@pytest.mark.parametrize('operation', ['update', 'remove'])
def test_entry_unsupported_selected_path_refuses_before_journal_or_crypto(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, selected: str, operation: str,
) -> None:
    from sk_publish import fixture_commit_and_push
    subject = entry_subject;manifest = subject.clone / 'manifest.json';raw = json.loads(manifest.read_bytes())
    raw['entries']['ha-token']['blob'] = selected;manifest.write_text(json.dumps(raw))
    fixture_commit_and_push(subject.clone, 'dummy unsupported selected baseline', ['manifest.json'])
    before = _snapshot(subject);subject.calls.clear()
    outcome = _invoke_entry(subject, operation)
    assert outcome['code'] == 1 and before == _snapshot(subject) and subject.calls == []
    assert not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
def test_entry_ignored_incoming_target_refuses_before_merge_and_preserves_occupant(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    from secrets_kit import repo
    from test_sync_view import _git
    subject = entry_subject;incoming = subject.data_dir.parent / 'incoming occupied target'
    repo.clone(_git(subject.clone, 'remote', 'get-url', 'origin'), incoming)
    _git(incoming, 'config', 'user.name', 'dummy');_git(incoming, 'config', 'user.email', 'dummy@example.invalid')
    raw = json.loads((incoming / 'manifest.json').read_bytes())
    name = 'third-entry' if operation == 'add' else 'ha-token'
    slot = 'blobs/' + subject.entry_source.name + '.age' if operation == 'add' else 'blobs/incoming-occupied.age'
    raw['entries'][name] = dict(raw['entries']['ha-token'], blob=slot, dest='${PLAIN}/third.txt' if operation == 'add' else '${PLAIN}/ha-token.txt')
    (incoming / slot).write_bytes((incoming / raw['entries']['second-old']['blob']).read_bytes())
    (incoming / 'manifest.json').write_text(json.dumps(raw))
    _git(incoming, 'add', '--', 'manifest.json', slot);_git(incoming, 'commit', '--quiet', '-m', 'dummy incoming occupied slot');_git(incoming, 'push', '--quiet')
    occupant = subject.clone / slot;occupant.write_bytes(b'dummy ignored unowned occupant')
    _git(subject.clone, 'config', '--add', 'core.excludesFile', str(subject.data_dir.parent / 'dummy excludes'))
    excludes = subject.data_dir.parent / 'dummy excludes';excludes.write_text('/' + slot + '\n')
    before = _snapshot(subject);subject.calls.clear();real_git = repo._git;queries = []
    with monkeypatch.context() as observing:
        def git(args: list[str], *, cwd: Path, timeout: int) -> Any:
            if cwd == subject.clone:queries.append(args[0])
            return real_git(args, cwd=cwd, timeout=timeout)
        observing.setattr(repo, '_git', git);outcome = _invoke_entry(subject, operation)
    assert outcome['code'] == 1 and before == _snapshot(subject) and subject.calls == []
    assert 'merge' not in queries and occupant.read_bytes() == b'dummy ignored unowned occupant'
    assert not (subject.data_dir / 'authoring-recovery').exists()



@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
def test_entry_lost_report_accepts_fresh_descendant_without_republication(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    from test_seed_recovery import _lose_push_report
    subject = entry_subject
    with monkeypatch.context() as controlled:
        observations = _lose_push_report(subject, controlled, descendant=True)
        outcome = _invoke_entry(subject, operation)
    peer = _consumer(subject, 'entry descendant accepted peer')
    print('ENTRY_DESCENDANT_PROOF_TRACE ' + json.dumps({'operation': operation, 'code': outcome['code'], 'pushes': observations['pushes'], 'proofs': observations['proofs']}))
    assert outcome['code'] == 0 and observations['pushes'] == [0] and len(observations['proofs']) == 1
    assert observations['descendant']['failures'] == peer['failures'] == 0
    assert not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
def test_entry_origin_change_is_rejected_before_push(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    """The final origin read must still match the admitted repository digest."""
    from secrets_kit import authoring, repo

    subject = entry_subject
    changed = False
    published: list[str] = []
    real_origin = repo._recorded_origin
    real_commit = repo._commit_owned

    def origin(clone: Path) -> str:
        value = real_origin(clone)
        if changed:
            return str(subject.data_dir.parent / 'wrong-publication-target.git')
        return value

    def commit(*args: Any, **kwargs: Any) -> str:
        nonlocal changed
        result = real_commit(*args, **kwargs)
        changed = True
        return result

    def publish(*args: Any, **kwargs: Any) -> Any:
        published.append(kwargs['declared_repo'])
        raise AssertionError('publication must not run after the origin changes')

    with monkeypatch.context() as controlled:
        controlled.setattr(repo, '_recorded_origin', origin)
        controlled.setattr(repo, '_commit_owned', commit)
        controlled.setattr(repo, '_publish_owned', publish)
        before = _snapshot(subject)
        outcome = _invoke_entry(subject, operation)
    after = _snapshot(subject)
    print('ENTRY_ORIGIN_CHANGE_TRACE ' + json.dumps({
        'operation': operation, 'code': outcome['code'],
        'exception': type(outcome['exception']).__name__ if outcome['exception'] else None,
        'published': published, 'snapshotHeld': before == after,
    }))
    assert outcome['code'] == 1 and outcome['exception'] is None
    assert published == [] and before == after
    assert not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
@pytest.mark.parametrize('fault', ['fetch-no-update', 'ancestry-query-failure', 'ancestry-malformed-output', 'non-ancestry'])
def test_entry_negative_or_stale_publication_proof_retains_evidence(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str, fault: str,
) -> None:
    import subprocess
    from secrets_kit import authoring
    subject = entry_subject;before = _snapshot(subject);real_run = subprocess.run;pushes = [];proofs = []
    with monkeypatch.context() as controlled:
        def run(command: list[str], *args: Any, **kwargs: Any) -> Any:
            own = command[0] == 'git' and Path(kwargs.get('cwd') or '.') == subject.clone
            if own and command[1] == 'push':
                pushes.append(command)
                if fault != 'non-ancestry':
                    result = real_run(command, *args, **kwargs);assert result.returncode == 0
                    if fault.startswith('ancestry'):
                        descendant = _consumer(subject, 'entry malformed ancestry descendant', add_proof=True)
                        assert descendant['failures'] == 0
                raise subprocess.TimeoutExpired(command, kwargs['timeout'])
            if own and command[1] == 'fetch' and '--no-write-fetch-head' in command:
                proofs.append(command)
                if fault == 'fetch-no-update':return subprocess.CompletedProcess(command, 0, b'', b'')
            if own and command[1] == 'merge-base' and fault.startswith('ancestry'):
                return subprocess.CompletedProcess(command, 128 if fault == 'ancestry-query-failure' else 0, b'dummy malformed ancestry record' if fault.endswith('output') else b'', b'')
            return real_run(command, *args, **kwargs)
        controlled.setattr(subprocess, 'run', run)
        outcome = _invoke_entry(subject, operation)
    after = _snapshot(subject);record = json.loads((subject.data_dir / 'authoring-recovery/marker.json').read_bytes())
    print('ENTRY_NEGATIVE_PROOF_TRACE ' + json.dumps({'operation': operation, 'fault': fault, 'code': outcome['code'], 'pushes': len(pushes), 'proofs': len(proofs), 'phase': record['phase'], 'remoteChanged': before['remoteHead'] != after['remoteHead']}))
    peer = _consumer(subject, 'negative proof actual remote')
    assert outcome['code'] == 1 and len(pushes) == len(proofs) == 1 and record['phase'] == 'uncertain'
    assert peer['failures'] == 0
    if fault == 'non-ancestry':
        protected = _snapshot(subject)
        with pytest.raises(authoring.AuthoringRecoveryError):authoring._reconcile_recovery(subject.data_dir)
        assert protected == _snapshot(subject)
    else:
        assert authoring._reconcile_recovery(subject.data_dir)['outcome'] == 'confirmed'
        assert not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
def test_entry_positive_proof_cleanup_failure_is_resumable_without_push(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    import subprocess
    from secrets_kit import authoring
    subject = entry_subject;real_run = subprocess.run;pushes = [];failed = []
    with monkeypatch.context() as controlled:
        def run(command: list[str], *args: Any, **kwargs: Any) -> Any:
            own = command[0] == 'git' and Path(kwargs.get('cwd') or '.') == subject.clone
            if own and command[1] == 'update-ref' and '-d' in command and any(x.startswith('refs/secrets-kit/publication/') for x in command) and not failed:
                failed.append(command);return subprocess.CompletedProcess(command, 128, b'', b'dummy owned proof cleanup refusal')
            result = real_run(command, *args, **kwargs)
            if own and command[1] == 'push':
                pushes.append(result.returncode);assert result.returncode == 0
                raise subprocess.TimeoutExpired(command, kwargs['timeout'])
            return result
        controlled.setattr(subprocess, 'run', run)
        outcome = _invoke_entry(subject, operation)
    record = json.loads((subject.data_dir / 'authoring-recovery/marker.json').read_bytes())
    peer = _consumer(subject, 'entry positive proof cleanup failure')
    assert pushes == [0] and len(failed) == 1 and outcome['code'] == 1 and record['phase'] == 'finalizing'
    assert peer['failures'] == 0 and authoring._reconcile_recovery(subject.data_dir)['outcome'] == 'confirmed'
    assert not (subject.data_dir / 'authoring-recovery').exists()



@pytest.mark.parametrize('refusal', ['missing-source', 'duplicate-entry', 'missing-dest', 'missing-entry'])
@pytest.mark.parametrize('direct', [False, True], ids=['main', 'direct'])
def test_entry_return_refusal_restores_owned_fast_forward_before_return(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, refusal: str, direct: bool,
) -> None:
    subject = entry_subject
    incoming = _consumer(subject, 'incoming before return refusal', add_proof=True)
    before = _snapshot(subject);subject.calls.clear()
    if refusal == 'missing-source':arguments = ['add', 'missing-source', '--file', str(subject.data_dir.parent / 'absent dummy source.txt'), '--dest', '${PLAIN}/missing.txt']
    elif refusal == 'duplicate-entry':arguments = ['add', 'ha-token', '--file', subject.source]
    elif refusal == 'missing-dest':arguments = ['add', 'missing-dest', '--file', subject.source]
    else:arguments = ['remove', 'not-an-entry']
    if direct:
        args = subject.cli.build_parser().parse_args(arguments);outcome = args.func(args)
    else:outcome = subject.cli.main(arguments)
    after = _snapshot(subject);calls = list(subject.calls)
    print('ENTRY_RETURN_REFUSAL_TRACE ' + json.dumps({'refusal': refusal, 'direct': direct, 'code': outcome, 'snapshotHeld': before == after, 'calls': calls}))
    old_peer = _consumer(subject, 'return refusal fresh peer')
    retry = _invoke_entry(subject, 'add');peer = _consumer(subject, 'return refusal clean retry')
    assert incoming['failures'] == 0 and outcome == 1 and before == after and calls == []
    assert old_peer['failures'] == peer['failures'] == 0 and retry['code'] == 0
    assert not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('change', ['appearance', 'disappearance', 'recipient-change'])
def test_entry_retry_uses_synchronized_authoritative_manifest(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    from secrets_kit import repo
    from test_sync_view import _git
    subject = entry_subject;incoming = subject.data_dir.parent / ('incoming ' + change)
    repo.clone(_git(subject.clone, 'remote', 'get-url', 'origin'), incoming)
    _git(incoming, 'config', 'user.name', 'dummy');_git(incoming, 'config', 'user.email', 'dummy@example.invalid')
    raw = json.loads((incoming / 'manifest.json').read_bytes());blob = raw['entries']['ha-token']['blob']
    if change == 'appearance':
        raw['entries']['third-entry'] = dict(raw['entries']['ha-token'], blob='blobs/incoming-third.age', dest='${PLAIN}/third.txt')
        raw['profiles']['base'].append('third-entry')
        (incoming / 'blobs/incoming-third.age').write_bytes((incoming / blob).read_bytes())
    elif change == 'disappearance':
        raw['entries'].pop('ha-token')
        for names in raw['profiles'].values():
            if 'ha-token' in names:names.remove('ha-token')
        (incoming / blob).unlink()
    else:
        raw['recipient'] = 'age1dummynewrecipient'
        for entry in raw['entries'].values():
            payload = agefile.decrypt_with_identity(subject.data_dir / 'identity.txt', incoming / entry['blob'])
            agefile.encrypt_to_recipient(raw['recipient'], payload, incoming / entry['blob'])
        assert agefile.wrap_identity('dummy new identity\n', incoming / 'identity.age') == 0
    (incoming / 'manifest.json').write_text(json.dumps(raw))
    _git(incoming, 'add', '--', 'manifest.json', 'blobs', 'identity.age');_git(incoming, 'commit', '--quiet', '-m', 'dummy authoritative incoming');_git(incoming, 'push', '--quiet')
    before = _snapshot(subject);real_encrypt = agefile.encrypt_to_recipient;recipients = []
    with monkeypatch.context() as observing:
        def encrypt(recipient: str, plaintext: bytes, output: Path) -> None:
            recipients.append(recipient);real_encrypt(recipient, plaintext, output)
        observing.setattr(agefile, 'encrypt_to_recipient', encrypt)
        outcome = _invoke_entry(subject, 'add' if change == 'appearance' else 'remove' if change == 'disappearance' else 'update')
    after = _snapshot(subject)
    print('ENTRY_AUTHORITATIVE_TRACE ' + json.dumps({'change': change, 'code': outcome['code'], 'snapshotHeld': before == after, 'recipients': recipients}))
    if change in ['appearance', 'disappearance']:
        old_peer = _consumer(subject, 'authoritative refusal before retry')
        retry = _invoke_entry(subject, 'update' if change == 'appearance' else 'add')
        peer = _consumer(subject, 'authoritative refusal after retry')
        assert outcome['code'] == 1 and before == after and recipients == []
        assert old_peer['failures'] == peer['failures'] == 0 and retry['code'] == 0
    else:
        assert outcome['code'] == 0 and recipients == ['age1dummynewrecipient']
        assert Manifest.load(subject.clone / 'manifest.json').recipient == 'age1dummynewrecipient'
        peer = _consumer(subject, 'coherent incoming recipient peer')
        assert peer['failures'] == 0 and peer['values']['ha-token.txt'] == b'dummy replacement entry value\n'
    assert not (subject.data_dir / 'authoring-recovery').exists()



@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
@pytest.mark.parametrize('boundary', ['blob-before', 'blob-after', 'manifest-before', 'manifest-after', 'directory-sync'])
def test_entry_apply_boundaries_restore_exact_epoch_before_retry(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str, boundary: str,
) -> None:
    from secrets_kit import authoring
    subject = entry_subject;selected = _selected_blob(subject, operation);before = _snapshot(subject)
    initial_peer = _consumer(subject, 'apply boundary initial');primary = OSError('dummy entry apply fault');observed = []
    real_write = authoring._write;real_unlink = Path.unlink;real_sync = authoring._directory_sync
    target = subject.clone / 'manifest.json' if boundary.startswith('manifest') else selected
    with monkeypatch.context() as controlled:
        def write(path: Path, payload: bytes, mode: int = 0o600) -> None:
            hit = path == target and boundary != 'directory-sync' and not observed
            if hit and boundary.endswith('before'):observed.append(boundary);raise primary
            real_write(path, payload, mode)
            if hit:observed.append(boundary);raise primary
        def unlink(path: Path, *args: Any, **kwargs: Any) -> None:
            hit = path == selected and operation == 'remove' and boundary.startswith('blob') and not observed
            if hit and boundary.endswith('before'):observed.append(boundary);raise primary
            real_unlink(path, *args, **kwargs)
            if hit:observed.append(boundary);raise primary
        def sync(path: Path) -> None:
            real_sync(path)
            if path == subject.clone / 'blobs' and boundary == 'directory-sync' and not observed:
                observed.append(boundary);raise primary
        controlled.setattr(authoring, '_write', write);controlled.setattr(Path, 'unlink', unlink);controlled.setattr(authoring, '_directory_sync', sync)
        outcome = _invoke_entry(subject, operation)
    after = _snapshot(subject)
    print('ENTRY_APPLY_BEFORE_RETRY ' + json.dumps({'operation': operation, 'boundary': boundary, 'snapshotHeld': before == after, 'primaryIdentityHeld': outcome['exception'] is primary, 'observed': observed}))
    old_peer = _consumer(subject, 'apply boundary before retry')
    retry = _invoke_entry(subject, operation);peer = _consumer(subject, 'apply boundary after retry')
    assert observed == [boundary] and outcome['exception'] is primary and before == after
    assert old_peer['values'] == initial_peer['values'] and old_peer['failures'] == peer['failures'] == 0 and retry['code'] == 0
    assert not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('operation', ['add', 'update'])
@pytest.mark.parametrize('fault', ['before-output', 'completed-cancellation'])
@pytest.mark.parametrize('direct', [False, True], ids=['main', 'direct'])
def test_entry_encryption_exception_identity_and_owned_output_cleanup(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str, fault: str, direct: bool,
) -> None:
    subject = entry_subject;before = _snapshot(subject);observed = []
    primary = RuntimeError('dummy encrypt before bytes') if fault == 'before-output' else KeyboardInterrupt('dummy completed encrypt cancellation')
    real_encrypt = agefile.encrypt_to_recipient
    with monkeypatch.context() as controlled:
        def encrypt(recipient: str, plaintext: bytes, output: Path) -> None:
            observed.append({'isCheckoutTarget': output == _selected_blob(subject, operation), 'protectedBeforeBytes': output.exists() and output.stat().st_mode & 0o777 == 0o600})
            if fault == 'completed-cancellation':real_encrypt(recipient, plaintext, output)
            raise primary
        controlled.setattr(agefile, 'encrypt_to_recipient', encrypt)
        outcome = _invoke_entry(subject, operation, direct=direct)
    after = _snapshot(subject)
    print('ENTRY_ENCRYPT_BEFORE_RETRY ' + json.dumps({'operation': operation, 'fault': fault, 'direct': direct, 'snapshotHeld': before == after, 'observed': observed, 'primaryIdentityHeld': outcome['exception'] is primary}))
    old_peer = _consumer(subject, 'encrypt before retry');retry = _invoke_entry(subject, operation);peer = _consumer(subject, 'encrypt after retry')
    assert observed == [{'isCheckoutTarget': False, 'protectedBeforeBytes': True}]
    assert outcome['exception'] is primary and before == after and retry['code'] == 0
    assert old_peer['failures'] == peer['failures'] == 0 and not (subject.data_dir / 'authoring-recovery').exists()


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
@pytest.mark.parametrize('direct', [False, True], ids=['main', 'direct'])
def test_entry_body_recovery_and_release_faults_attach_to_same_primary(
    entry_subject: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, operation: str, direct: bool,
) -> None:
    from secrets_kit import authoring, operation_lock as ownership, repo
    subject = entry_subject;primary = RuntimeError('dummy entry commit interrupted')
    original_index = (subject.clone / '.git/index').read_bytes();real_git = repo._git;real_write = authoring._write
    with monkeypatch.context() as controlled:
        def git(args: list[str], *, cwd: Path, timeout: int) -> Any:
            if cwd == subject.clone and args[0] == 'commit':raise primary
            return real_git(args, cwd=cwd, timeout=timeout)
        def write(path: Path, payload: bytes, mode: int = 0o600) -> None:
            if path == subject.clone / '.git/index' and payload == original_index:raise OSError('dummy entry restoration refusal')
            real_write(path, payload, mode)
        def unlock(fd: int) -> None:raise OSError('dummy entry release refusal')
        controlled.setattr(repo, '_git', git);controlled.setattr(authoring, '_write', write);controlled.setattr(ownership, '_unlock', unlock)
        outcome = _invoke_entry(subject, operation, direct=direct)
    assert outcome['exception'] is primary
    assert isinstance(primary.authoring_recovery_error, authoring.AuthoringRecoveryError)
    assert isinstance(primary.operation_lock_release_error, ownership.OperationLockError)
    peer = _consumer(subject, 'entry combined fault peer')
    assert peer['failures'] == 0 and len(peer['values']) == 2
    assert authoring._reconcile_recovery(subject.data_dir)['recovery'] == 'cleared'
