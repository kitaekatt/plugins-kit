"""Whole secrets operations contend across actual independent processes."""

import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from secrets_kit import agefile
from secrets_kit import converge as convergence
from secrets_kit import repo as repository
from test_dest_guard import _templates, adding
from test_repo_binding import _adapter, _snapshot, _strict_crypto
from test_sync_view import _git, _seed_author, actual_subject_origins


WORKER = r'''
import hashlib, importlib.util, json, os, subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace

request = json.loads(Path(sys.argv[1]).read_text())
root = Path(request['root']).resolve()
sys.path.insert(0, str(root / 'plugins/secrets-kit/lib'))
from secrets_kit import SecretsError, agefile, guard, repo
from secrets_kit import manifest
from secrets_kit import converge as convergence
source = root / 'plugins/secrets-kit/scripts/secrets_kit_cli.py'
spec = importlib.util.spec_from_file_location('dummy_operation_cli', source)
cli = importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
cli.CONFIG_PATH = Path(request['config']);cli.DATA_DIR = Path(request['data'])
manifest.resolve_host = lambda: ['testbox']
events = []
child = None
real_git = repo._git
assert Path(real_git.__code__.co_filename).resolve() == root / 'plugins/secrets-kit/lib/secrets_kit/repo.py'
def git(args, *, cwd, timeout):
    events.append('git:' + args[0]);return real_git(args, cwd=cwd, timeout=timeout)
repo._git = git
real_is_clone = repo.is_clone
def is_clone(path):
    events.append('clone-detection');return real_is_clone(path)
repo.is_clone = is_clone
real_guard = guard.require_guard
def require_guard(path):
    events.append('hook-guard');return real_guard(path)
guard.require_guard = require_guard
real_run = subprocess.run
def run(args, *positional, **keywords):
    if args == ['git', 'config', '--local', '--no-includes', '--null', '--get-all', 'remote.origin.url']:
        events.append('binding-query')
    return real_run(args, *positional, **keywords)
subprocess.run = run
def armored(recipient, payload):
    return b'-----BEGIN AGE ENCRYPTED FILE-----\n' + recipient.encode() + b'\n' + payload + b'-----END AGE ENCRYPTED FILE-----\n'
def encrypt(recipient, payload, output):
    events.append('encrypt');Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_bytes(armored(recipient, payload))
def decrypt(identity, blob):
    events.append('decrypt')
    header, recipient, payload = Path(blob).read_bytes().split(b'\n', 2)
    key = {b'dummy cached old identity\n': b'age1testrecipient', b'dummy new identity\n': b'age1dummynewrecipient'}.get(Path(identity).read_bytes())
    if header != b'-----BEGIN AGE ENCRYPTED FILE-----' or key != recipient:
        raise SecretsError('dummy key does not match dummy recipient')
    return payload.removesuffix(b'-----END AGE ENCRYPTED FILE-----\n')
def keygen():
    events.append('keygen');return 'dummy new identity\n', 'age1dummynewrecipient'
def wrap(identity, output):
    events.append('wrap');Path(output).write_bytes(armored('age1dummywrap', identity.encode()));return 0
def unwrap(wrapped, output):
    events.append('unwrap')
    payload = Path(wrapped).read_bytes().split(b'\n', 2)[2].removesuffix(b'-----END AGE ENCRYPTED FILE-----\n')
    key = {b'dummy wrapped identity\n': b'dummy cached old identity\n', b'dummy new identity\n': b'dummy new identity\n'}.get(payload)
    if key is None:return 9
    Path(output).write_bytes(key);return 0
agefile.encrypt_to_recipient = encrypt;agefile.decrypt_with_identity = decrypt
agefile.keygen = keygen;agefile.wrap_identity = wrap;agefile.unwrap_identity = unwrap
convergence.decrypt_with_identity = decrypt
real_load = manifest.Manifest.load
paused = False
def load(path):
    global paused, child
    value = real_load(path);events.append('manifest-read')
    if request.get('pause') and not paused:
        paused = True
        if request.get('exec_child'):
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'], close_fds=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        Path(request['ready']).write_text('actual manifest read')
        limit = time.monotonic() + 15
        while not Path(request['release']).exists():
            if time.monotonic() > limit:raise RuntimeError('dummy parent release timed out')
            time.sleep(0.01)
        if request.get('after_read') == 'expected':raise SecretsError('dummy expected operation failure')
        if request.get('after_read') == 'unexpected':raise RuntimeError('dummy unexpected operation failure')
        if request.get('after_read') == 'exit':os._exit(17)
    return value
manifest.Manifest.load = load
code = 0;exception = None;failures = []
try:
    if request['operation'] == 'bootstrap':
        adapter_source = root / 'plugins/secrets-kit/custom_bootstrap.py'
        adapter_spec = importlib.util.spec_from_file_location('dummy_operation_adapter', adapter_source)
        adapter = importlib.util.module_from_spec(adapter_spec);adapter_spec.loader.exec_module(adapter)
        assert Path(adapter.__file__).resolve() == adapter_source
        adapter.CONFIG_PATH = cli.CONFIG_PATH;adapter._known_machines = lambda: ['testbox']
        ctx = SimpleNamespace(data_dir=cli.DATA_DIR, log=lambda text: events.append('log:' + text), log_ok=lambda text: events.append('ok:' + text), add_failure=lambda key, **kw: failures.append({'key': key, **kw}))
        adapter.bootstrap(ctx);code = 1 if failures else 0
    elif request['operation'] == 'converge':
        result = convergence.converge(cli.CONFIG_PATH, cli.DATA_DIR)
        failures = [{'key': f.key, 'ask_reason': f.ask_reason, 'user_msg': f.user_msg} for f in result.failures]
        code = 1 if failures else 0
    elif request.get('direct'):
        args = cli.build_parser().parse_args(request['argv']);code = args.func(args)
    else:code = cli.main(request['argv'])
except SecretsError as error:
    exception = type(error).__name__;code = cli._fail(str(error))
except Exception as error:
    exception = type(error).__name__;code = 2
origins = []
for name, module in list(sys.modules.items()):
    if name == 'secrets_kit' or name.startswith('secrets_kit.'):
        file = Path(module.__file__).resolve()
        assert file.parent == root / 'plugins/secrets-kit/lib/secrets_kit'
        origins.append({'module': name, 'file': file.relative_to(root).as_posix(), 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()})
assert Path(cli.main.__code__.co_filename).resolve() == source
Path(request['output']).write_text(json.dumps({'code': code, 'exception': exception, 'events': events, 'failures': failures, 'origins': origins, 'exec_child': child.pid if child else None}))
sys.exit(code)
'''


def _request(adding, tag, *, operation='add', data=None, pause=False, direct=False, after_read=None, exec_child=False):
    root = Path(__file__).resolve().parents[2]
    receipts = adding.data_dir.parent / 'process receipts'
    receipts.mkdir(exist_ok=True)
    source = receipts / (tag + '-source.txt')
    source.write_bytes(('dummy ' + tag + ' value\n').encode())
    argv = {
        'add': ['add', tag, '--file', str(source), '--dest', '${PLAIN}/' + tag + '.txt', '--profile', 'base'],
        'update': ['add', 'ha-token', '--file', str(source), '--update'],
        'remove': ['remove', 'ha-token'],
        'init': ['init', '--force'],
        'rotate-identity': ['rotate-identity'],
        'unlock': ['unlock'],
        'status': ['status'],
        'status-refresh': ['status', '--refresh'],
        'bootstrap': [], 'converge': [],
    }[operation]
    request = {'root': str(root), 'config': str(adding.config_path), 'data': str(data or adding.data_dir), 'operation': operation, 'argv': argv, 'direct': direct, 'pause': pause, 'ready': str(receipts / (tag + '-ready')), 'release': str(receipts / (tag + '-release')), 'output': str(receipts / (tag + '-output.json')), 'after_read': after_read, 'exec_child': exec_child}
    path = receipts / (tag + '-request.json')
    path.write_text(json.dumps(request))
    worker = receipts / (tag + '-worker.py')
    worker.write_text(WORKER)
    return request, path, worker


def _start(request, path, worker):
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return subprocess.Popen([sys.executable, str(worker), str(path)], cwd=worker.parent, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_ready(process, request):
    limit = time.monotonic() + 12
    while not Path(request['ready']).exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail('owner failed before actual manifest barrier: ' + stdout + stderr)
        if time.monotonic() > limit:
            process.kill();process.communicate()
            pytest.fail('owner never reached actual manifest barrier')
        time.sleep(0.01)


def _finish(process, request, *, timeout=12):
    stdout, stderr = process.communicate(timeout=timeout)
    output = Path(request['output'])
    receipt = json.loads(output.read_text()) if output.exists() else {'code': process.returncode, 'events': [], 'exception': None}
    receipt.update(stdout=stdout, stderr=stderr, process_code=process.returncode)
    print('OPERATION_PROCESS_RESULT ' + json.dumps(receipt))
    return receipt


def _consumer(adding, remote, name):
    root = adding.data_dir.parent / ('fresh operation consumer ' + name)
    repository.clone(str(remote), root / 'data/repo')
    (root / 'plain').mkdir()
    assert agefile.unwrap_identity(root / 'data/repo/identity.age', root / 'data/identity.txt') == 0
    (root / 'data/identity.txt').chmod(0o600)
    raw = json.loads(adding.config_path.read_text())
    raw['repo'] = str(remote);raw['vars']['PLAIN'] = str(root / 'plain')
    config = root / 'secrets.json';config.write_text(json.dumps(raw))
    result = convergence.converge(config, root / 'data')
    return {'failures': [f.user_msg for f in result.failures], 'written': result.written, 'values': {p.name: p.read_bytes().decode() for p in (root / 'plain').iterdir() if p.is_file()}}


@pytest.fixture(autouse=True)
def operation_subject_origins():
    root = Path(__file__).resolve().parents[2]
    for name in ['test_repo_binding', 'test_sync_view', 'test_dest_guard', 'sk_testlib']:
        file = Path(sys.modules[name].__file__).resolve()
        assert file == root / 'tests/secrets-kit' / (name + '.py')
    assert Path(convergence.converge.__code__.co_filename).resolve() == root / 'plugins/secrets-kit/lib/secrets_kit/converge.py'


@pytest.mark.parametrize('alias', ['same', 'dot', 'physical'])
def test_actual_process_contender_refuses_then_retry_preserves_both_values(adding, monkeypatch, alias):
    _seed_author(adding);_strict_crypto(adding, monkeypatch)
    remote = Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))
    data = adding.data_dir
    if alias == 'dot':
        (data / 'nested').mkdir();data = data / 'nested/..'
    if alias == 'physical':
        data = data.parent / 'physical data alias';data.symlink_to(adding.data_dir, target_is_directory=True)
    a = _request(adding, 'alpha', pause=True)
    owner = _start(*a);_wait_ready(owner, a[0])
    before = _snapshot(adding, [remote])
    b = _request(adding, 'beta', data=data)
    contender = _start(*b)
    try:
        second = _finish(contender, b[0], timeout=5)
        while_held = _snapshot(adding, [remote])
        owner_still_paused = owner.poll() is None and not Path(a[0]['release']).exists()
    finally:
        Path(a[0]['release']).touch()
        first = _finish(owner, a[0])
        if contender.poll() is None:contender.kill();contender.communicate()
    after_owner = json.loads(_git(remote, 'show', 'HEAD:manifest.json'))
    print('OPERATION_HELD_TRACE ' + json.dumps({'contenderCode': second['code'], 'ownerCode': first['code'], 'ownerStillPausedAtContenderExit': owner_still_paused, 'remoteEntriesAfterOwnerBeforeRetry': sorted(after_owner['entries']), 'protectedSnapshotHeld': before == while_held}))
    retry = _request(adding, 'beta', data=data)
    retried = _finish(_start(*retry), retry[0])
    committed = json.loads(_git(remote, 'show', 'HEAD:manifest.json'))
    consumer = _consumer(adding, remote, alias)
    assert second['code'] == 1 and 'operation' in second['stderr'].lower() and 'retry' in second['stderr'].lower()
    assert second['events'] == [] and before == while_held and owner_still_paused
    assert first['code'] == retried['code'] == 0 and set(committed['entries']) == {'ha-token', 'alpha', 'beta'}
    assert consumer['failures'] == [] and consumer['values']['alpha.txt'] == 'dummy alpha value\n' and consumer['values']['beta.txt'] == 'dummy beta value\n'


CASES = [(op, False) for op in ['init', 'add', 'update', 'remove', 'rotate-identity', 'unlock', 'status', 'status-refresh', 'bootstrap', 'converge']]
CASES += [(op, True) for op in ['init', 'add', 'update', 'remove', 'rotate-identity', 'unlock', 'status']]


@pytest.mark.parametrize('operation,direct', CASES)
def test_all_actual_caller_paths_refuse_before_protected_work(adding, monkeypatch, operation, direct):
    _seed_author(adding);_strict_crypto(adding, monkeypatch)
    remote = Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))
    a = _request(adding, 'owner', pause=True)
    owner = _start(*a);_wait_ready(owner, a[0])
    before = _snapshot(adding, [remote])
    b = _request(adding, 'contender', operation=operation, direct=direct)
    contender = _start(*b)
    try:
        second = _finish(contender, b[0], timeout=5)
        held = _snapshot(adding, [remote])
    finally:
        Path(a[0]['release']).touch()
        first = _finish(owner, a[0])
        if contender.poll() is None:contender.kill();contender.communicate()
    peer = _consumer(adding, remote, operation + ('-direct' if direct else ''))
    messages = second['stderr'] + second['stdout'] + json.dumps(second.get('failures', []))
    assert second['code'] == 1 and 'operation' in messages.lower() and 'lock' in messages.lower()
    assert not any(event in ['clone-detection', 'binding-query', 'hook-guard', 'manifest-read', 'encrypt', 'decrypt', 'unwrap', 'wrap', 'keygen'] or event.startswith(('ok:', 'git:')) for event in second['events'])
    assert before == held and first['code'] == 0 and peer['failures'] == [] and peer['values']['owner.txt'] == 'dummy owner value\n'
    assert all(not f.get('ask_reason') for f in second.get('failures', []))


@pytest.mark.parametrize('after_read,expected', [('expected', 1), ('unexpected', 2), ('exit', 17)])
def test_real_process_release_after_failures_and_exit(adding, monkeypatch, after_read, expected):
    _seed_author(adding);_strict_crypto(adding, monkeypatch)
    a = _request(adding, 'failed-owner', pause=True, after_read=after_read)
    owner = _start(*a);_wait_ready(owner, a[0])
    b = _request(adding, 'retry')
    second = _finish(_start(*b), b[0])
    Path(a[0]['release']).touch();first = _finish(owner, a[0])
    retry = _finish(_start(*b), b[0])
    remote = Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))
    peer = _consumer(adding, remote, after_read)
    assert second['code'] == 1 and first['process_code'] == expected and retry['code'] == 0
    assert peer['failures'] == [] and peer['values']['retry.txt'] == 'dummy retry value\n'
    if after_read == 'unexpected':assert first['exception'] == 'RuntimeError'


def test_exec_child_does_not_keep_released_operation_owned(adding, monkeypatch):
    _seed_author(adding);_strict_crypto(adding, monkeypatch)
    a = _request(adding, 'exec-owner', pause=True, exec_child=True)
    owner = _start(*a);_wait_ready(owner, a[0])
    b = _request(adding, 'exec-retry')
    second = _finish(_start(*b), b[0])
    Path(a[0]['release']).touch();first = _finish(owner, a[0])
    child = first['exec_child']
    try:
        os.kill(child, 0)
        retry = _finish(_start(*b), b[0])
        child_still_alive = True
    finally:os.kill(child, signal.SIGTERM)
    assert second['code'] == 1 and first['code'] == retry['code'] == 0 and child_still_alive


@pytest.mark.parametrize('inactive', ['absent', 'unlisted', 'registry'])
def test_inactive_or_registry_rejected_converge_does_not_prepare_data(adding, monkeypatch, inactive):
    if inactive == 'absent':adding.config_path.unlink()
    if inactive == 'unlisted':
        raw = json.loads(adding.config_path.read_text());raw['machines'] = {'other': {'profiles': ['base']}}
        adding.config_path.write_text(json.dumps(raw))
    target = adding.data_dir.parent / 'must remain absent'
    result = convergence.converge(adding.config_path, target, known_machines=['other'] if inactive == 'registry' else None)
    assert not target.exists() and (result.skipped_reason if inactive != 'registry' else result.failed == 1)


@pytest.mark.parametrize('verb', ['init', 'unlock', 'rotate-identity'])
def test_terminal_parent_handoff_precedes_any_data_work(adding, monkeypatch, verb):
    target = adding.data_dir.parent / 'terminal parent must remain absent'
    monkeypatch.setattr(adding.cli, 'DATA_DIR', target)
    monkeypatch.setattr(adding.cli, 'relaunch_self', lambda *args: 'dummy controlled child transport')
    monkeypatch.setattr(adding.cli, '_require_config', lambda: pytest.fail('parent must not load config'))
    assert adding.cli.main([verb, '--new-terminal']) == 0 and not target.exists()


def _lock_module():
    spec = importlib.util.find_spec('secrets_kit.operation_lock')
    assert spec is not None, 'new package-local guard contract is absent'
    module = __import__('secrets_kit.operation_lock', fromlist=['operation_lock'])
    assert Path(module.__file__).resolve() == Path(__file__).resolve().parents[2] / 'plugins/secrets-kit/lib/secrets_kit/operation_lock.py'
    return module


@pytest.mark.parametrize('hazard', ['symlink', 'dangling', 'hardlink', 'nonempty', 'loose', 'directory'])
def test_actual_guard_leaf_fault_refuses_and_preserves_protected_data(adding, monkeypatch, hazard):
    _seed_author(adding)
    calls = _strict_crypto(adding, monkeypatch);calls.clear()
    path = adding.data_dir / 'operation.lock'
    if path.exists():path.unlink()
    target = adding.data_dir.parent / 'dummy guard target'
    target.write_bytes(b'dummy unrelated guard bytes')
    if hazard == 'symlink':path.symlink_to(target)
    if hazard == 'dangling':path.symlink_to(target.parent / 'absent guard target')
    if hazard == 'hardlink':os.link(target, path)
    if hazard == 'nonempty':path.write_bytes(b'dummy prior nonempty guard');path.chmod(0o600)
    if hazard == 'loose':path.touch();path.chmod(0o644)
    if hazard == 'directory':path.mkdir()
    remote = Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))
    before = _snapshot(adding, [remote]);identity = path.lstat()
    code = adding.cli.main(['remove', 'ha-token'])
    after = _snapshot(adding, [remote]);final = path.lstat()
    assert code == 1 and calls == [] and before == after and (identity.st_dev, identity.st_ino, identity.st_mode) == (final.st_dev, final.st_ino, final.st_mode)
    assert target.read_bytes() == b'dummy unrelated guard bytes'


@pytest.mark.parametrize('hazard', ['writable-data', 'dangling-data', 'loop-data'])
def test_actual_data_identity_fault_refuses_before_repo_work(adding, monkeypatch, hazard):
    _seed_author(adding);calls = _strict_crypto(adding, monkeypatch);calls.clear()
    data = adding.data_dir
    absent = data.parent / 'absent physical target'
    if hazard == 'writable-data':data.chmod(0o777)
    if hazard == 'dangling-data':
        data = data.parent / 'dangling data alias';data.symlink_to(absent, target_is_directory=True)
    if hazard == 'loop-data':
        data = data.parent / 'loop data alias';data.symlink_to(data, target_is_directory=True)
    monkeypatch.setattr(adding.cli, 'DATA_DIR', data)
    remote = Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))
    before = _snapshot(adding, [remote]);identity = data.lstat()
    code = adding.cli.main(['remove', 'ha-token'])
    after = _snapshot(adding, [remote]);final = data.lstat()
    assert code == 1 and calls == [] and before == after and not absent.exists()
    assert (identity.st_dev, identity.st_ino, identity.st_mode) == (final.st_dev, final.st_ino, final.st_mode)


@pytest.mark.parametrize('number,busy', [(errno.EACCES, True), (errno.EAGAIN, True), (errno.ENOLCK, False), (errno.ENOSYS, False), (errno.EOPNOTSUPP, False), (errno.EIO, False), (errno.EINTR, False), (errno.EBADF, False)])
def test_scoped_kernel_fault_category_through_actual_command(adding, monkeypatch, capsys, number, busy):
    _seed_author(adding);_lock_module()
    import fcntl
    calls = _strict_crypto(adding, monkeypatch);calls.clear()
    observed = []
    actual = fcntl.flock
    def fault(fd, operation):
        observed.append(operation)
        if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:raise OSError(number, 'dummy kernel fault token')
        return actual(fd, operation)
    monkeypatch.setattr(fcntl, 'flock', fault)
    remote = Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))
    before = _snapshot(adding, [remote]);code = adding.cli.main(['remove', 'ha-token'])
    message = capsys.readouterr().err;after = _snapshot(adding, [remote])
    assert code == 1 and calls == [] and before == after and observed == [fcntl.LOCK_EX | fcntl.LOCK_NB]
    assert ('active secrets operation' in message) is busy and 'dummy kernel fault token' not in message


@pytest.mark.parametrize('body', ['normal', 'expected', 'unexpected'])
def test_release_fault_retains_real_outcome_and_primary_exception(adding, monkeypatch, capsys, body):
    _seed_author(adding);_strict_crypto(adding, monkeypatch)
    module = _lock_module()
    source = adding.data_dir.parent / 'release-source.txt';source.write_bytes(b'dummy release value\n')
    args = ['add', 'release-change', '--file', str(source), '--dest', '${PLAIN}/release-change.txt', '--profile', 'base']
    remote = Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))
    old = _git(remote, 'rev-parse', 'HEAD')
    observed = []
    primary = RuntimeError('dummy primary unexpected error') if body == 'unexpected' else repository.SecretsError('dummy primary expected error')
    def release(fd):raise OSError(5, 'dummy release raw token')
    def fail_encrypt(*args):observed.append('encrypt');raise primary
    with monkeypatch.context() as scoped:
        scoped.setattr(module, '_unlock', release)
        if body != 'normal':scoped.setattr(agefile, 'encrypt_to_recipient', fail_encrypt)
        if body == 'unexpected':
            with pytest.raises(RuntimeError) as error:adding.cli.main(args)
            assert error.value is primary
            code = 2
        else:code = adding.cli.main(args)
        message = capsys.readouterr().err
    new = _git(remote, 'rev-parse', 'HEAD')
    manifest = json.loads(_git(remote, 'show', 'HEAD:manifest.json'))
    reacquired = convergence.converge(adding.config_path, adding.data_dir)
    peer = _consumer(adding, remote, 'release-' + body)
    assert reacquired.failed == 0 and peer['failures'] == [] and 'dummy release raw token' not in message
    if body == 'normal':
        assert code == 1 and new != old and 'release-change' in manifest['entries'] and peer['values']['release-change.txt'] == 'dummy release value\n'
        assert 'may already have completed' in message
    else:
        assert code == (2 if body == 'unexpected' else 1) and new == old and observed == ['encrypt']
        assert isinstance(primary.operation_lock_release_error, module.OperationLockError)
        if body == 'expected':assert 'primary expected' in message and 'release failed' in message


def test_converge_release_fault_keeps_factual_materialization_counts(adding, monkeypatch):
    _seed_author(adding);_strict_crypto(adding, monkeypatch)
    module = _lock_module()
    with monkeypatch.context() as scoped:
        def release(fd):raise OSError(5, 'dummy release fault')
        scoped.setattr(module, '_unlock', release)
        result = convergence.converge(adding.config_path, adding.data_dir)
    retry = convergence.converge(adding.config_path, adding.data_dir)
    assert result.written == 1 and result.ok == 0 and result.failed == 1 and result.failures[0].key == 'secrets_operation_lock'
    assert not result.failures[0].ask_reason and (adding.plain / 'ha-token.txt').read_bytes() == Path(adding.source).read_bytes()
    assert retry.failed == retry.written == 0 and retry.ok == 1


def test_guard_inode_is_stable_across_nested_refusal_and_reacquisition(tmp_path):
    module = _lock_module();data = tmp_path / 'new data/child data'
    with module.operation_lock(data) as canonical:
        path = canonical / 'operation.lock';before = path.stat()
        with pytest.raises(module.OperationBusyError):
            with module.operation_lock(data):pytest.fail('nested acquisition must refuse')
        held = path.stat()
    with module.operation_lock(data):after = path.stat()
    assert (before.st_dev, before.st_ino) == (held.st_dev, held.st_ino) == (after.st_dev, after.st_ino)
    assert path.read_bytes() == b'' and before.st_mode & 0o777 == 0o600
    assert data.stat().st_mode & 0o777 == data.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('verb', ['init', 'unlock', 'rotate-identity'])
def test_controlled_terminal_transport_runs_real_guarded_child(adding, monkeypatch, verb):
    _seed_author(adding);_strict_crypto(adding, monkeypatch)
    remote = Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))
    a = _request(adding, 'terminal-owner', pause=True)
    owner = _start(*a);_wait_ready(owner, a[0])
    before = _snapshot(adding, [remote])
    children = []
    def transport(command, extra):
        assert command == verb and extra == (['--force'] if verb == 'init' else [])
        child = _request(adding, 'terminal-child', operation=command)
        children.append(_finish(_start(*child), child[0]))
        return 'dummy subprocess transport'
    monkeypatch.setattr(adding.cli, 'relaunch_self', transport)
    monkeypatch.setattr(adding.cli, '_require_config', lambda: pytest.fail('terminal parent must not load config or acquire'))
    argv = [verb, '--new-terminal'] + (['--force'] if verb == 'init' else [])
    try:
        parent_code = adding.cli.main(argv)
        after = _snapshot(adding, [remote])
    finally:
        Path(a[0]['release']).touch();first = _finish(owner, a[0])
    peer = _consumer(adding, remote, 'terminal-' + verb)
    assert parent_code == 0 and len(children) == 1 and children[0]['code'] == 1 and children[0]['events'] == []
    assert before == after and first['code'] == 0 and peer['failures'] == [] and peer['values']['terminal-owner.txt'] == 'dummy terminal-owner value\n'


@pytest.mark.parametrize('fault,number,winerror,busy', [
    ('locking', errno.EACCES, 33, True),
    ('locking', errno.EACCES, 5, False),
    ('locking', errno.EACCES, None, True),
    ('locking', errno.EAGAIN, None, True),
    ('locking', errno.EDEADLK, None, True),
    ('locking', errno.ENOSYS, 50, False),
    ('locking', errno.EINTR, None, False),
    ('seek', errno.EACCES, 5, False),
])
def test_windows_kernel_categories_are_scoped_to_acquisition(adding, monkeypatch, capsys, fault, number, winerror, busy):
    _seed_author(adding);module = _lock_module()
    calls = _strict_crypto(adding, monkeypatch);calls.clear()
    observed = []
    error = OSError(number, 'dummy native raw token')
    if winerror is not None:error.winerror = winerror
    def locking(fd, mode, size):
        observed.append((mode, size, os.lseek(fd, 0, os.SEEK_CUR)))
        if mode == 2:raise error
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking))
    monkeypatch.setattr(module, 'IS_WINDOWS', True)
    monkeypatch.setattr(module, '_windows_private', lambda path, *, directory: None)
    if fault == 'seek':
        actual_seek = os.lseek
        guard = (adding.data_dir / 'operation.lock').stat()
        def seek(fd, offset, whence):
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) == (guard.st_dev, guard.st_ino):raise error
            return actual_seek(fd, offset, whence)
        monkeypatch.setattr(os, 'lseek', seek)
    remote = Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))
    before = _snapshot(adding, [remote]);code = adding.cli.main(['remove', 'ha-token'])
    message = capsys.readouterr().err;after = _snapshot(adding, [remote])
    assert code == 1 and calls == [] and before == after and 'dummy native raw token' not in message
    assert ('active secrets operation' in message) is busy
    assert observed == ([] if fault == 'seek' else [(2, 1, 0)])


def test_windows_empty_guard_protocol_never_initializes_or_inherits_bytes(tmp_path, monkeypatch):
    module = _lock_module();observed = []
    def locking(fd, mode, size):
        observed.append({'mode': mode, 'size': size, 'offset': os.lseek(fd, 0, os.SEEK_CUR), 'bytes': os.fstat(fd).st_size, 'inheritable': os.get_inheritable(fd)})
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking))
    monkeypatch.setattr(module, 'IS_WINDOWS', True)
    monkeypatch.setattr(module, '_windows_private', lambda path, *, directory: None)
    with module.operation_lock(tmp_path / 'dummy Windows data') as data:
        assert (data / 'operation.lock').read_bytes() == b''
    assert observed == [{'mode': mode, 'size': 1, 'offset': 0, 'bytes': 0, 'inheritable': False} for mode in [2, 0]]


def test_home_expansion_inability_is_visible_setup_refusal(adding, monkeypatch):
    _seed_author(adding);_lock_module()
    calls = _strict_crypto(adding, monkeypatch);calls.clear()
    actual = Path.expanduser
    def unavailable(path):
        if path == adding.data_dir:raise RuntimeError('dummy home unavailable raw token')
        return actual(path)
    monkeypatch.setattr(Path, 'expanduser', unavailable)
    remote = Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))
    before = _snapshot(adding, [remote]);code = adding.cli.main(['remove', 'ha-token'])
    after = _snapshot(adding, [remote])
    assert code == 1 and calls == [] and before == after


@pytest.mark.parametrize('when', ['before-open', 'after-acquire'])
def test_observed_guard_substitution_refuses_without_repair(adding, monkeypatch, when):
    _seed_author(adding);module = _lock_module()
    calls = _strict_crypto(adding, monkeypatch);calls.clear()
    path = adding.data_dir / 'operation.lock'
    saved = adding.data_dir.parent / 'dummy substituted guard'
    original = path.stat()
    replacement = []
    def substitute():
        os.replace(path, saved)
        path.touch();path.chmod(0o600)
        replacement.append(path.stat().st_ino)
    if when == 'before-open':
        actual = os.open
        def open_guard(file, flags, *args, **kwargs):
            if Path(file) == path and not flags & os.O_CREAT:substitute()
            return actual(file, flags, *args, **kwargs)
        monkeypatch.setattr(os, 'open', open_guard)
    else:
        actual = module._acquire
        def acquire(fd, data):
            actual(fd, data);substitute()
        monkeypatch.setattr(module, '_acquire', acquire)
    remote = Path(_git(adding.clone, 'config', '--local', '--get', 'remote.origin.url'))
    before = _snapshot(adding, [remote]);code = adding.cli.main(['remove', 'ha-token'])
    after = _snapshot(adding, [remote])
    assert code == 1 and calls == [] and before == after and len(replacement) == 1
    assert path.stat().st_ino == replacement[0] and saved.stat().st_ino == original.st_ino
    assert path.read_bytes() == saved.read_bytes() == b''
