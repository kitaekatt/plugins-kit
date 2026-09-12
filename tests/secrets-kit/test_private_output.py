"""Private output lifecycle through actual writers and local CLI consumers."""

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from secrets_kit import SecretsError, agefile, perms
from secrets_kit import converge as convergence
from secrets_kit.state import State
from test_init import _load_cli, _seeding_template, seeding  # noqa: F401


@pytest.fixture
def private_root(tmp_path):
    root = tmp_path / 'private-output'
    root.mkdir()
    return root


def _observe_private_bytes(monkeypatch, targets, *, windows=False):
    """Observe real descriptor writes; delegate every write and protection."""
    targets = [Path(path) for path in targets]
    descriptors = {}
    protected = set()
    events = []
    real_open = os.open
    real_fdopen = os.fdopen
    real_tighten = perms.tighten
    real_run = subprocess.run

    def identify(path):
        path = Path(path)
        return next((target for target in targets if path.parent == target.parent
                     and (path.name == target.name or path.name.startswith(target.name + '.'))), None)

    def opening(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if identify(path) is not None:
            descriptors[fd] = Path(path)
        else:
            descriptors.pop(fd, None)
        return fd

    def tighten(path, mode):
        if identify(path) is not None:
            info = Path(path).stat()
            events.append(('protect', Path(path), info.st_size, mode))
            protected.add((info.st_dev, info.st_ino))
        return real_tighten(path, mode)

    class ObservedStream:
        def __init__(self, stream, path):
            self.stream = stream
            self.path = path

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, value):
            info = os.fstat(self.stream.fileno())
            events.append(('write', self.path, (info.st_dev, info.st_ino) in protected,
                           stat.S_IMODE(info.st_mode), self.stream.encoding if hasattr(self.stream, 'encoding') else None))
            return self.stream.write(value)

    def fdopening(fd, *args, **kwargs):
        stream = real_fdopen(fd, *args, **kwargs)
        return ObservedStream(stream, descriptors[fd]) if fd in descriptors else stream

    def running(argv, *args, **kwargs):
        if argv[0] != 'icacls':
            return real_run(argv, *args, **kwargs)
        assert kwargs['timeout'] == 20
        events.append(('acl', Path(argv[1]), Path(argv[1]).stat().st_size, list(argv)))
        return subprocess.CompletedProcess(argv, 0, stdout=b'processed dummy object')

    monkeypatch.setattr(os, 'open', opening)
    monkeypatch.setattr(os, 'fdopen', fdopening)
    monkeypatch.setattr(perms, 'tighten', tighten)
    monkeypatch.setattr(convergence, 'tighten', tighten)
    monkeypatch.setattr(perms, 'IS_WINDOWS', windows)
    if windows:
        monkeypatch.setenv('USERNAME', 'dummy-owner')
        monkeypatch.setenv('USERDOMAIN', 'DUMMY')
        monkeypatch.setattr(subprocess, 'run', running)
    return events


def _assert_protected_before_bytes(events, target, *, text=False, windows=False):
    target = Path(target)
    writes = [event for event in events if event[0] == 'write' and event[1].parent == target.parent
              and (event[1].name == target.name or event[1].name.startswith(target.name + '.'))]
    protections = [event for event in events if event[0] == 'protect' and event[1].parent == target.parent
                   and event[1].name.startswith(target.name + '.')]
    assert writes and all(event[2] for event in writes), events
    assert len(protections) == 1 and protections[0][2:] == (0, 0o600), events
    assert all(event[1] != target for event in writes), events
    if text:
        assert all(event[4] == 'utf-8' for event in writes)
    if not windows:
        assert all(event[3] == 0o600 for event in writes)
    else:
        acls = [event for event in events if event[0] == 'acl' and event[1] == protections[0][1]]
        assert len(acls) == 1 and acls[0][2] == 0, events


@pytest.mark.parametrize('windows', [False, True])
def test_real_state_save_protects_before_serialized_bytes(private_root, monkeypatch, windows):
    path = private_root / 'state.json'
    path.write_text('{"version":1,"entries":{}}\n', encoding='utf-8')
    state = State.load(path)
    state.record('dummy', blob_sha='blob', dest_sha='dest', mode=0o600, dest=str(private_root / 'dummy'))
    events = _observe_private_bytes(monkeypatch, [path], windows=windows)
    state.save()
    _assert_protected_before_bytes(events, path, text=True, windows=windows)
    assert json.loads(path.read_text())['entries'] == state.rows


@pytest.mark.parametrize('windows', [False, True])
def test_actual_converge_protects_destination_and_state_then_skips_crypto(fleet, monkeypatch, windows):
    fleet.unlock()
    target = fleet.dest_root / 'ha-token.txt'
    state_path = convergence.paths_for(fleet.data_dir)['state']
    events = _observe_private_bytes(monkeypatch, [target, state_path], windows=windows)
    first = convergence.converge(fleet.config_path, fleet.data_dir)
    _assert_protected_before_bytes(events, target, windows=windows)
    _assert_protected_before_bytes(events, state_path, text=True, windows=windows)
    assert first.failures == [] and first.written == 1
    assert target.read_bytes() == b'token-value\n'
    state = State.load(state_path)
    assert state.rows['ha-token']['dest'] == str(target)
    events.clear()
    monkeypatch.setattr(convergence, 'decrypt_with_identity', lambda *args: pytest.fail('unchanged content must not decrypt'))
    second = convergence.converge(fleet.config_path, fleet.data_dir)
    assert second.failures == [] and second.written == 0 and second.ok == 1
    assert not [event for event in events if event[0] == 'write' and event[1].parent == target.parent]
    _assert_protected_before_bytes(events, state_path, text=True, windows=windows)


@pytest.mark.parametrize('operation', ['init', 'rotate-identity'])
@pytest.mark.parametrize('windows', [False, True])
def test_real_authoring_cache_protects_before_text_and_retains_order(seeding, monkeypatch, operation, windows):
    if operation == 'rotate-identity':
        assert seeding.cli.main(['init']) == 0
        seeding.identity.write_text('old dummy identity', encoding='utf-8')
    events = _observe_private_bytes(monkeypatch, [seeding.identity], windows=windows)
    real_publish = seeding.cli.repo_mod.commit_and_push
    ordering = []

    def publish(*args, **kwargs):
        ordering.append(('publish', seeding.identity.read_bytes() if seeding.identity.exists() else None))
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(seeding.cli.repo_mod, 'commit_and_push', publish)
    assert seeding.cli.main([operation]) == 0
    _assert_protected_before_bytes(events, seeding.identity, text=True, windows=windows)
    assert seeding.identity.read_text() == 'AGE-SECRET-KEY-NEW'
    assert ordering == [('publish', None if operation == 'init' else b'AGE-SECRET-KEY-NEW')]


def _dummy_unwrap(monkeypatch, code=0, *, spawn_error=False, output=b'dummy unwrapped identity\n'):
    calls = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(agefile, '_resolve', lambda name: '/dummy-age')

    class Child:
        returncode = code

        def __init__(self, argv, **kwargs):
            calls.append(('spawn', argv, set(kwargs)))
            assert argv[:2] == ['/dummy-age', '-d']
            assert set(kwargs) == {'stdout'}
            if spawn_error:
                raise OSError('dummy spawn fault')
            self.output = kwargs['stdout']

        def communicate(self, *args, **kwargs):
            assert not args and not kwargs
            calls.append(('communicate',))
            self.output.write(output)

    def spawning(argv, *args, **kwargs):
        if argv[0] != '/dummy-age':
            return real_popen(argv, *args, **kwargs)
        return Child(argv, **kwargs)

    monkeypatch.setattr(agefile.subprocess, 'Popen', spawning)
    return calls


@pytest.mark.parametrize('windows', [False, True])
@pytest.mark.parametrize('code', [0, 7])
@pytest.mark.parametrize('existing', ['absent', 'file', 'symlink'])
def test_actual_unwrap_protects_stdout_and_preserves_old_slots(private_root, monkeypatch, windows, code, existing):
    wrapped = private_root / 'identity.age'
    wrapped.write_bytes(b'dummy ciphertext')
    target = private_root / 'identity.txt'
    referent = private_root / 'referent'
    if existing == 'file':
        target.write_bytes(b'old dummy identity')
    if existing == 'symlink':
        referent.write_bytes(b'old referent bytes')
        target.symlink_to(referent)
    events = _observe_private_bytes(monkeypatch, [target], windows=windows)
    calls = _dummy_unwrap(monkeypatch, code)
    assert agefile.unwrap_identity(wrapped, target) == code
    _assert_protected_before_bytes(events, target, windows=windows)
    assert calls[0][0] == 'spawn' and calls[1] == ('communicate',)
    if code == 0:
        assert not target.is_symlink() and target.read_bytes() == b'dummy unwrapped identity\n'
    elif existing == 'absent':
        assert not target.exists()
    else:
        assert target.read_bytes() == (b'old referent bytes' if existing == 'symlink' else b'old dummy identity')
        assert target.is_symlink() == (existing == 'symlink')
    if existing == 'symlink':
        assert referent.read_bytes() == b'old referent bytes'
    assert sorted(path.name for path in private_root.iterdir()) == sorted(['identity.age'] + ([] if code != 0 and existing == 'absent' else ['identity.txt']) + (['referent'] if existing == 'symlink' else []))


@pytest.mark.parametrize('existing', [False, True])
def test_actual_unlock_failed_child_keeps_prior_cache(seeding, monkeypatch, existing, capsys):
    assert seeding.cli.main(['init']) == 0
    if existing:
        seeding.identity.write_bytes(b'old cached dummy key')
    else:
        seeding.identity.unlink()
    _dummy_unwrap(monkeypatch, 9)
    assert seeding.cli.main(['unlock']) == 1
    assert 'cache was not replaced' in capsys.readouterr().err
    assert seeding.identity.read_bytes() == b'old cached dummy key' if existing else not seeding.identity.exists()


@pytest.mark.parametrize('writer', ['state', 'converge'])
@pytest.mark.parametrize('kind', ['file', 'symlink'])
def test_actual_writers_leave_predictable_temp_preimages_untouched(private_root, writer, kind):
    target = private_root / ('state.json' if writer == 'state' else 'secret.txt')
    target.write_bytes(b'old final bytes')
    old_temp = target.with_name(target.name + f'.tmp-{os.getpid()}')
    referent = private_root / 'referent'
    if kind == 'symlink':
        referent.write_bytes(b'unrelated referent bytes')
        old_temp.symlink_to(referent)
    else:
        old_temp.write_bytes(b'unrelated temp bytes')
        old_temp.chmod(0o666)
    prior_mode = old_temp.stat().st_mode
    if writer == 'state':
        State(target, {}).save()
    else:
        convergence._atomic_write(target, b'new dummy bytes', 0o600)
    assert old_temp.stat().st_mode == prior_mode
    assert old_temp.is_symlink() == (kind == 'symlink') and old_temp.read_bytes() == (b'unrelated referent bytes' if kind == 'symlink' else b'unrelated temp bytes')
    if kind == 'symlink':
        assert referent.read_bytes() == b'unrelated referent bytes'


@pytest.mark.parametrize('writer', ['state', 'converge', 'unwrap'])
@pytest.mark.parametrize('fault', ['protection', 'replace'])
def test_actual_private_writer_fault_preserves_old_final(private_root, monkeypatch, writer, fault):
    target = private_root / ('state.json' if writer == 'state' else 'identity.txt')
    target.write_bytes(b'old final bytes')
    calls = []
    if writer == 'unwrap':
        wrapped = private_root / 'identity.age'
        wrapped.write_bytes(b'dummy wrapped')
        calls = _dummy_unwrap(monkeypatch)
    if fault == 'protection':
        def refuse(path, mode):
            raise SecretsError(f'dummy protection fault on {path}')
        monkeypatch.setattr(perms, 'tighten', refuse)
        monkeypatch.setattr(convergence, 'tighten', refuse)
    else:
        def refuse(source, destination):
            assert Path(destination) == target
            raise OSError('dummy replace fault')
        monkeypatch.setattr(os, 'replace', refuse)
    with pytest.raises((SecretsError, OSError), match='dummy .* fault'):
        if writer == 'state':
            State(target, {}).save()
        elif writer == 'converge':
            convergence._atomic_write(target, b'new dummy bytes', 0o600)
        else:
            agefile.unwrap_identity(wrapped, target)
    assert target.read_bytes() == b'old final bytes'
    assert not [path for path in private_root.iterdir() if path.name.startswith(target.name + '.')]
    if writer == 'unwrap' and fault == 'protection':
        assert calls == []


def test_actual_wrap_retains_tty_input_and_integer_status(private_root, monkeypatch):
    output = private_root / 'identity.age'
    monkeypatch.setattr(agefile, '_resolve', lambda name: '/dummy-age')
    seen = []

    class Child:
        returncode = 6

        def __init__(self, argv, **kwargs):
            assert argv == ['/dummy-age', '-p', '-a']
            assert set(kwargs) == {'stdin', 'stdout'} and kwargs['stdin'] == subprocess.PIPE
            self.output = kwargs['stdout']

        def communicate(self, value):
            seen.append(value)
            self.output.write(b'dummy ciphertext')

    monkeypatch.setattr(agefile.subprocess, 'Popen', Child)
    assert agefile.wrap_identity('dummy identity\n', output) == 6
    assert seen == [b'dummy identity\n'] and output.read_bytes() == b'dummy ciphertext'


@pytest.mark.parametrize('collision', ['file', 'symlink'])
def test_shared_owner_exclusively_skips_allocator_collision(private_root, monkeypatch, collision):
    target = private_root / 'private.txt'
    candidate = private_root / 'private.txt.collision'
    referent = private_root / 'referent'
    if collision == 'symlink':
        referent.write_bytes(b'unrelated referent')
        candidate.symlink_to(referent)
    else:
        candidate.write_bytes(b'unrelated candidate')
    names = iter(['collision', 'fresh'])
    monkeypatch.setattr(tempfile, '_get_candidate_names', lambda: names)

    def produce(stream):
        stream.write(b'dummy plaintext')
        return True

    assert perms._private_output(target, 0o600, produce) is True
    assert target.read_bytes() == b'dummy plaintext'
    assert candidate.is_symlink() == (collision == 'symlink')
    assert candidate.read_bytes() == (b'unrelated referent' if collision == 'symlink' else b'unrelated candidate')
    if collision == 'symlink':
        assert referent.read_bytes() == b'unrelated referent'
    assert not (private_root / 'private.txt.fresh').exists()


@pytest.mark.parametrize('mode', [0o600, 0o644, 0o700, 0o1600, True])
def test_shared_owner_requested_mode_precedes_first_byte_despite_umask(private_root, mode):
    target = private_root / 'private.txt'
    seen = []

    def produce(stream):
        seen.append(stat.S_IMODE(os.fstat(stream.fileno()).st_mode))
        stream.write(b'dummy plaintext')
        return True

    previous = os.umask(0o077)
    try:
        assert perms._private_output(target, mode, produce) is True
    finally:
        os.umask(previous)
    if not perms.IS_WINDOWS:
        assert seen == [stat.S_IMODE(mode)]
        assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(mode)


@pytest.mark.parametrize('success', [True, False, None, 1])
def test_shared_owner_requires_explicit_boolean_publication(private_root, success):
    target = private_root / 'private.txt'
    target.write_bytes(b'old bytes')

    def produce(stream):
        stream.write(b'dummy partial')
        return success

    if type(success) is bool:
        assert perms._private_output(target, 0o600, produce) is success
    else:
        with pytest.raises(TypeError, match='boolean'):
            perms._private_output(target, 0o600, produce)
    assert target.read_bytes() == (b'dummy partial' if success is True else b'old bytes')
    assert list(private_root.iterdir()) == [target]


@pytest.mark.parametrize('fault', ['producer', 'flush', 'fsync', 'close', 'replace', 'fdopen', 'protection'])
def test_shared_owner_faults_close_descriptor_and_preserve_final(private_root, monkeypatch, fault):
    target = private_root / 'private.txt'
    target.write_bytes(b'old bytes')
    allocated = []
    real_mkstemp = tempfile.mkstemp
    real_fdopen = os.fdopen

    def allocating(*args, **kwargs):
        result = real_mkstemp(*args, **kwargs)
        allocated.append(result)
        return result

    class FaultStream:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def flush(self):
            if fault == 'flush':
                raise OSError('dummy flush fault')
            self.stream.flush()

        def close(self):
            self.stream.close()
            if fault == 'close':
                raise OSError('dummy close fault')

    def opening(fd, *args, **kwargs):
        if fault == 'fdopen':
            raise OSError('dummy fdopen fault')
        return FaultStream(real_fdopen(fd, *args, **kwargs))

    def refuse(*args, **kwargs):
        raise OSError(f'dummy {fault} fault')

    def produce(stream):
        stream.write(b'dummy partial')
        if fault == 'producer':
            raise RuntimeError('dummy producer fault')
        return True

    monkeypatch.setattr(tempfile, 'mkstemp', allocating)
    monkeypatch.setattr(os, 'fdopen', opening)
    if fault == 'fsync':
        monkeypatch.setattr(os, 'fsync', refuse)
    elif fault == 'replace':
        monkeypatch.setattr(os, 'replace', refuse)
    elif fault == 'protection':
        monkeypatch.setattr(perms, 'tighten', refuse)
    with pytest.raises((OSError, RuntimeError), match=f'dummy {fault} fault'):
        perms._private_output(target, 0o600, produce)
    assert len(allocated) == 1
    with pytest.raises(OSError):
        os.fstat(allocated[0][0])
    assert target.read_bytes() == b'old bytes' and list(private_root.iterdir()) == [target]


@pytest.mark.parametrize('primary', ['exception', 'abort'])
def test_owned_cleanup_failure_is_visible_and_retains_primary_context(private_root, monkeypatch, primary):
    target = private_root / 'private.txt'
    target.write_bytes(b'old bytes')
    real_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path.parent == target.parent and path.name.startswith(target.name + '.'):
            raise PermissionError('dummy cleanup refusal')
        return real_unlink(path, *args, **kwargs)

    def produce(stream):
        stream.write(b'dummy partial')
        if primary == 'exception':
            raise RuntimeError('dummy primary failure')
        return False

    monkeypatch.setattr(Path, 'unlink', unlink)
    with pytest.raises(SecretsError, match='temporary output') as caught:
        perms._private_output(target, 0o600, produce)
    if primary == 'exception':
        assert isinstance(caught.value.__cause__, RuntimeError)
        assert 'dummy primary failure' in str(caught.value.__cause__)
    leftovers = [path for path in private_root.iterdir() if path != target]
    assert len(leftovers) == 1 and leftovers[0].read_bytes() == b'dummy partial'
    assert target.read_bytes() == b'old bytes'
    if not perms.IS_WINDOWS:
        assert stat.S_IMODE(leftovers[0].stat().st_mode) == 0o600
    real_unlink(leftovers[0])


@pytest.mark.parametrize('which', ['destination', 'state'])
@pytest.mark.parametrize('fault', ['protection', 'replace'])
def test_actual_converge_selective_writer_fault_preserves_prior_record(fleet, monkeypatch, which, fault):
    fleet.unlock()
    assert convergence.converge(fleet.config_path, fleet.data_dir).written == 1
    target = fleet.dest_root / 'ha-token.txt'
    state_path = convergence.paths_for(fleet.data_dir)['state']
    previous_state = state_path.read_bytes()
    previous_target = target.read_bytes()
    agefile.encrypt_to_recipient(fleet.recipient, b'changed dummy\n', fleet.blobs / 'ha-token.txt.age')
    failing = target if which == 'destination' else state_path
    real_tighten = perms.tighten
    real_replace = os.replace

    def tightening(path, mode):
        if fault == 'protection' and Path(path).parent == failing.parent and Path(path).name.startswith(failing.name + '.'):
            raise SecretsError(f'dummy protection fault on {path}')
        return real_tighten(path, mode)

    def replacing(source, destination):
        if fault == 'replace' and Path(destination) == failing:
            raise OSError(f'dummy replace fault on {destination}')
        return real_replace(source, destination)

    monkeypatch.setattr(perms, 'tighten', tightening)
    monkeypatch.setattr(convergence, 'tighten', tightening)
    monkeypatch.setattr(os, 'replace', replacing)
    if which == 'state':
        with pytest.raises((SecretsError, OSError), match='dummy .* fault'):
            convergence.converge(fleet.config_path, fleet.data_dir)
        assert state_path.read_bytes() == previous_state
        assert target.read_bytes() == b'changed dummy\n'
    else:
        result = convergence.converge(fleet.config_path, fleet.data_dir)
        assert len(result.failures) == 1 and result.failures[0].key == 'secrets_entry'
        assert result.failures[0].ask_reason is None and result.written == 0
        assert target.read_bytes() == previous_target
        assert State.load(state_path).rows == json.loads(previous_state)['entries']
    assert not [path for path in failing.parent.iterdir() if path.name.startswith(failing.name + '.')]


@pytest.mark.parametrize('operation', ['init', 'rotate-identity'])
@pytest.mark.parametrize('fault', ['protection', 'replace'])
def test_actual_authoring_cache_fault_preserves_old_cache_and_prior_effect_order(seeding, monkeypatch, operation, fault):
    if operation == 'rotate-identity':
        assert seeding.cli.main(['init']) == 0
    seeding.identity.write_bytes(b'old dummy cache')
    publications = []
    real_publish = seeding.cli.repo_mod.commit_and_push
    real_tighten = perms.tighten
    real_replace = os.replace

    def publish(*args, **kwargs):
        publications.append(True)
        return real_publish(*args, **kwargs)

    def tightening(path, mode):
        if fault == 'protection' and Path(path).parent == seeding.identity.parent and Path(path).name.startswith(seeding.identity.name + '.'):
            raise SecretsError(f'dummy cache protection fault at {path}')
        return real_tighten(path, mode)

    def replacing(source, destination):
        if fault == 'replace' and Path(destination) == seeding.identity:
            raise OSError('dummy cache replace fault')
        return real_replace(source, destination)

    monkeypatch.setattr(seeding.cli.repo_mod, 'commit_and_push', publish)
    monkeypatch.setattr(perms, 'tighten', tightening)
    if hasattr(seeding.cli, 'tighten'):
        monkeypatch.setattr(seeding.cli, 'tighten', tightening)
    monkeypatch.setattr(os, 'replace', replacing)
    if fault == 'protection':
        assert seeding.cli.main([operation]) == 1
    else:
        with pytest.raises(OSError, match='dummy cache replace fault'):
            seeding.cli.main([operation])
    assert seeding.identity.read_bytes() == b'old dummy cache'
    assert len(publications) == (1 if operation == 'init' else 0)
    assert not [path for path in seeding.identity.parent.iterdir() if path.name.startswith(seeding.identity.name + '.')]
    # Earlier encrypted checkout changes / seed publication are not rolled back.
    assert (seeding.clone / 'identity.age').is_file()


@pytest.mark.parametrize('existing', ['absent', 'file', 'symlink'])
def test_actual_unwrap_spawn_fault_preserves_slot_and_is_not_passphrase_status(private_root, monkeypatch, existing):
    target = private_root / 'identity.txt'
    wrapped = private_root / 'identity.age'
    wrapped.write_bytes(b'dummy wrapped')
    referent = private_root / 'referent'
    if existing == 'file':
        target.write_bytes(b'old dummy cache')
    elif existing == 'symlink':
        referent.write_bytes(b'old referent')
        target.symlink_to(referent)
    calls = _dummy_unwrap(monkeypatch, spawn_error=True)
    with pytest.raises(SecretsError, match='could not run age: dummy spawn fault'):
        agefile.unwrap_identity(wrapped, target)
    assert len(calls) == 1
    if existing == 'absent':
        assert not target.exists()
    else:
        assert target.read_bytes() == (b'old referent' if existing == 'symlink' else b'old dummy cache')
        assert target.is_symlink() == (existing == 'symlink')
    if existing == 'symlink':
        assert referent.read_bytes() == b'old referent'
    assert not [path for path in private_root.iterdir() if path.name.startswith(target.name + '.')]


def test_actual_unwrap_zero_empty_output_remains_success(private_root, monkeypatch):
    target = private_root / 'identity.txt'
    target.write_bytes(b'old dummy cache')
    wrapped = private_root / 'identity.age'
    wrapped.write_bytes(b'dummy wrapped')
    _dummy_unwrap(monkeypatch, output=b'')
    assert agefile.unwrap_identity(wrapped, target) == 0
    assert target.read_bytes() == b''


def test_actual_unlock_success_uses_protected_child_stdout(seeding, monkeypatch):
    assert seeding.cli.main(['init']) == 0
    events = _observe_private_bytes(monkeypatch, [seeding.identity])
    _dummy_unwrap(monkeypatch)
    assert seeding.cli.main(['unlock']) == 0
    _assert_protected_before_bytes(events, seeding.identity)
    assert seeding.identity.read_bytes() == b'dummy unwrapped identity\n'


@pytest.mark.parametrize('fault', ['protection', 'replace'])
def test_actual_unlock_private_file_fault_reports_cache_operation(seeding, monkeypatch, fault, capsys):
    assert seeding.cli.main(['init']) == 0
    seeding.identity.write_bytes(b'old dummy cache')
    calls = _dummy_unwrap(monkeypatch)
    real_tighten = perms.tighten
    real_replace = os.replace

    def tightening(path, mode):
        if fault == 'protection':
            raise SecretsError(f'dummy private protection fault at {path}')
        return real_tighten(path, mode)

    def replacing(source, destination):
        if Path(destination) == seeding.identity:
            raise OSError('dummy private replace fault')
        return real_replace(source, destination)

    monkeypatch.setattr(perms, 'tighten', tightening)
    if hasattr(seeding.cli, 'tighten'):
        monkeypatch.setattr(seeding.cli, 'tighten', tightening)
    monkeypatch.setattr(os, 'replace', replacing)
    assert seeding.cli.main(['unlock']) == 1
    diagnostic = capsys.readouterr().err
    assert 'private' in diagnostic and 'incorrect passphrase' not in diagnostic
    assert seeding.identity.read_bytes() == b'old dummy cache'
    assert len(calls) == (0 if fault == 'protection' else 2)


@pytest.mark.parametrize('mode', [0o600, 0o644])
def test_shared_owner_windows_real_acl_operation_precedes_content(private_root, monkeypatch, mode):
    target = private_root / 'private.txt'
    calls = []
    monkeypatch.setattr(perms, 'IS_WINDOWS', True)
    monkeypatch.setenv('USERNAME', 'dummy-owner')
    monkeypatch.setenv('USERDOMAIN', 'DUMMY')

    def running(argv, **kwargs):
        assert argv[0] == 'icacls' and argv[2:] == ['/inheritance:r', '/grant:r', 'DUMMY\\dummy-owner:F']
        assert kwargs['timeout'] == 20 and Path(argv[1]).stat().st_size == 0
        calls.append('acl')
        return subprocess.CompletedProcess(argv, 0, stdout=b'dummy result')

    def produce(stream):
        calls.append('first byte')
        stream.write(b'dummy output')
        return True

    monkeypatch.setattr(perms.subprocess, 'run', running)
    assert perms._private_output(target, mode, produce) is True
    assert calls == (['acl', 'first byte'] if mode == 0o600 else ['first byte'])


@pytest.mark.parametrize('fault', ['nonzero', 'spawn', 'timeout'])
def test_shared_owner_windows_actual_acl_failure_stops_producer(private_root, monkeypatch, fault):
    target = private_root / 'private.txt'
    target.write_bytes(b'old dummy bytes')
    calls = []
    monkeypatch.setattr(perms, 'IS_WINDOWS', True)
    monkeypatch.setenv('USERNAME', 'dummy-owner')

    def running(argv, **kwargs):
        calls.append((argv, kwargs['timeout']))
        assert Path(argv[1]).stat().st_size == 0
        if fault == 'timeout':
            raise subprocess.TimeoutExpired(argv, 20)
        if fault == 'spawn':
            raise OSError('dummy icacls spawn fault')
        return subprocess.CompletedProcess(argv, 1, stdout=b'dummy ACL refusal')

    def produce(stream):
        pytest.fail('protection must finish successfully before producer')

    monkeypatch.setattr(perms.subprocess, 'run', running)
    with pytest.raises(SecretsError, match='icacls'):
        perms._private_output(target, 0o600, produce)
    assert len(calls) == 1 and calls[0][1] == 20
    assert target.read_bytes() == b'old dummy bytes' and list(private_root.iterdir()) == [target]


def test_shared_owner_fsyncs_then_closes_before_replace(private_root, monkeypatch):
    target = private_root / 'private.txt'
    fd_seen = []
    events = []
    real_fsync = os.fsync
    real_replace = os.replace

    def produce(stream):
        fd_seen.append(stream.fileno())
        stream.write(b'dummy output')
        events.append('produce')
        return True

    def syncing(fd):
        assert fd == fd_seen[0] and os.fstat(fd).st_size == len(b'dummy output')
        events.append('fsync')
        return real_fsync(fd)

    def replacing(source, destination):
        with pytest.raises(OSError):
            os.fstat(fd_seen[0])
        assert Path(source).read_bytes() == b'dummy output'
        events.append('replace after close')
        return real_replace(source, destination)

    monkeypatch.setattr(os, 'fsync', syncing)
    monkeypatch.setattr(os, 'replace', replacing)
    assert perms._private_output(target, 0o600, produce) is True
    assert events == ['produce', 'fsync', 'replace after close']


def test_shared_owner_already_absent_owned_temp_cleanup_is_clean(private_root, monkeypatch):
    target = private_root / 'private.txt'
    target.write_bytes(b'old bytes')
    real_unlink = Path.unlink
    calls = []

    def unlinking(path, *args, **kwargs):
        assert path.parent == target.parent and path.name.startswith(target.name + '.')
        calls.append(path)
        real_unlink(path, *args, **kwargs)
        raise FileNotFoundError('owned temp already absent at cleanup boundary')

    def produce(stream):
        stream.write(b'dummy partial')
        return False

    monkeypatch.setattr(Path, 'unlink', unlinking)
    assert perms._private_output(target, 0o600, produce) is False
    assert len(calls) == 1
    assert target.read_bytes() == b'old bytes' and list(private_root.iterdir()) == [target]
