"""Real parser omission, pre-encryption metadata validation and consumers."""

import json
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from secrets_kit import agefile, perms
from secrets_kit import converge as convergence
from secrets_kit import repo as repository
from secrets_kit.manifest import Manifest
from secrets_kit.state import State
from test_dest_guard import _templates, adding


@pytest.fixture(autouse=True)
def actual_subject_origins():
    root = Path(__file__).resolve().parents[2]
    for name, module in list(sys.modules.items()):
        if name != 'secrets_kit' and not name.startswith('secrets_kit.'):
            continue
        assert Path(module.__file__).resolve().parent == root / 'plugins/secrets-kit/lib/secrets_kit'


def _git(path, *args):
    return subprocess.run(['git', *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def _argv(adding, *extra, name='ha-token'):
    assert Path(adding.cli.__file__).resolve() == Path(__file__).resolve().parents[2] / 'plugins/secrets-kit/scripts/secrets_kit_cli.py'
    return ['add', name, '--file', adding.source, *extra]


def _seed(adding, *, mode='0644', newline='lf'):
    extra = ['--dest', '${PLAIN}/ha-token.txt', '--mode', '0600', '--profile', 'base', '--doc', 'inventory.md']
    if newline:
        extra += ['--newline', newline]
    assert adding.cli.main(_argv(adding, *extra)) == 0
    raw = json.loads(adding.manifest_path.read_text())
    raw['entries']['ha-token']['mode'] = mode
    adding.manifest_path.write_text(json.dumps(raw), encoding='utf-8')
    repository.commit_and_push(adding.clone, 'dummy existing metadata', ['manifest.json'])
    return adding.clone / adding.entry().blob


def _snapshot(adding, blob):
    remote = Path(_git(adding.clone, 'remote', 'get-url', 'origin'))
    return {'manifest': adding.manifest_path.read_bytes(), 'blob': blob.read_bytes() if blob.exists() else None,
            'head': _git(adding.clone, 'rev-parse', 'HEAD'), 'index': _git(adding.clone, 'status', '--porcelain'),
            'remoteHead': _git(remote, 'rev-parse', 'HEAD')}


def _observe(adding, monkeypatch):
    calls = {'encrypt': [], 'commit': [], 'sourceRead': []}
    encrypt, commit, read = agefile.encrypt_to_recipient, repository.commit_and_push, Path.read_bytes
    def encrypting(recipient, plaintext, destination):
        calls['encrypt'].append(str(destination));return encrypt(recipient, plaintext, destination)
    def committing(*args, **kwargs):
        calls['commit'].append(args[1]);return commit(*args, **kwargs)
    def reading(path):
        if path == Path(adding.source) and sys._getframe(1).f_globals.get('__name__') == adding.cli.__name__:
            calls['sourceRead'].append(str(path))
        return read(path)
    monkeypatch.setattr(agefile, 'encrypt_to_recipient', encrypting)
    monkeypatch.setattr(repository, 'commit_and_push', committing)
    monkeypatch.setattr(Path, 'read_bytes', reading)
    return calls


@pytest.mark.parametrize('explicit', [False, True])
def test_actual_parser_distinguishes_omitted_metadata_from_explicit_flags(adding, explicit):
    extra = ['--mode', '0600', '--newline', 'lf'] if explicit else []
    args = adding.cli.build_parser().parse_args(_argv(adding, '--update', *extra))
    assert args.mode == ('0600' if explicit else None)
    assert args.newline == ('lf' if explicit else None)


def test_actual_parser_retains_unsupported_newline_refusal(adding):
    with pytest.raises(SystemExit) as error:
        adding.cli.build_parser().parse_args(_argv(adding, '--update', '--newline', 'crlf'))
    assert error.value.code == 2


@pytest.mark.parametrize('mode', ['0644', '0640', '0000', 0o644, True, False])
def test_actual_update_omitting_mode_preserves_existing_accepted_semantics(adding, monkeypatch, mode):
    blob = _seed(adding, mode=mode)
    before_blob = blob.read_bytes()
    Path(adding.source).write_bytes(b'updated dummy value\n')
    calls = _observe(adding, monkeypatch)
    code = adding.cli.main(_argv(adding, '--update'))
    entry = adding.entry()
    stored = json.loads(adding.manifest_path.read_text())['entries']['ha-token']
    expected = int(mode, 8) if isinstance(mode, str) else mode
    print('UPDATE_MODE_TRACE ' + json.dumps({'inputMode': mode, 'code': code, 'storedMode': stored['mode'], 'newline': stored.get('newline'), 'encryptionCalls': len(calls['encrypt'])}))
    assert code == 0 and entry.mode == expected and stored['mode'] == format(expected, '04o')
    assert entry.newline == 'lf' and blob.read_bytes() != before_blob
    assert len(calls['encrypt']) == len(calls['commit']) == len(calls['sourceRead']) == 1
    assert entry.doc == 'inventory.md' and Manifest.load(adding.manifest_path).profiles['base'] == ['ha-token']


def test_actual_inherited_lf_rejects_crlf_before_authoring_mutation(adding, monkeypatch, capsys):
    blob = _seed(adding)
    Path(adding.source).write_bytes(b'replacement dummy CRLF\r\n')
    before = _snapshot(adding, blob)
    calls = _observe(adding, monkeypatch)
    code = adding.cli.main(_argv(adding, '--update'))
    after = _snapshot(adding, blob)
    error = capsys.readouterr().err
    assert code == 1 and after == before
    assert calls['encrypt'] == calls['commit'] == [] and len(calls['sourceRead']) == 1
    assert 'CRLF' in error and 'lf' in error.lower() and 'inherit' in error.lower()


@pytest.mark.parametrize('updating', [False, True])
@pytest.mark.parametrize('mode', ['', '0899', 'rw-'])
def test_actual_invalid_explicit_mode_rejects_before_ciphertext_or_manifest_write(adding, monkeypatch, updating, mode):
    blob = _seed(adding) if updating else adding.clone / 'blobs/source.txt.age'
    before = _snapshot(adding, blob)
    calls = _observe(adding, monkeypatch)
    extra = ['--update'] if updating else ['--dest', '${PLAIN}/ha-token.txt']
    code = adding.cli.main(_argv(adding, *extra, '--mode', mode))
    after = _snapshot(adding, blob)
    assert code == 1 and before == after
    assert calls['encrypt'] == calls['commit'] == [] and len(calls['sourceRead']) == 1


@pytest.mark.parametrize('mode', ['0600', '0640', '0000'])
def test_actual_explicit_valid_mode_overrides_inheritance(adding, mode):
    _seed(adding)
    assert adding.cli.main(_argv(adding, '--update', '--mode', mode, '--newline', 'lf')) == 0
    assert adding.entry().mode == int(mode, 8) and adding.entry().newline == 'lf'


@pytest.mark.parametrize('crlf', [False, True])
def test_actual_new_entry_omission_keeps_existing_defaults(adding, crlf):
    Path(adding.source).write_bytes(b'dummy new value\r\n' if crlf else b'dummy new value\n')
    assert adding.cli.main(_argv(adding, '--dest', '${PLAIN}/ha-token.txt')) == 0
    assert adding.entry().mode == 0o600 and adding.entry().newline is None


@pytest.mark.parametrize('crlf', [False, True])
def test_actual_explicit_lf_on_update_accepts_lf_and_rejects_crlf(adding, monkeypatch, crlf):
    blob = _seed(adding, newline=None)
    Path(adding.source).write_bytes(b'dummy changed\r\n' if crlf else b'dummy changed\n')
    before = _snapshot(adding, blob)
    calls = _observe(adding, monkeypatch)
    code = adding.cli.main(_argv(adding, '--update', '--newline', 'lf'))
    after = _snapshot(adding, blob)
    assert code == int(crlf)
    if crlf:
        assert before == after and calls['encrypt'] == calls['commit'] == []
    else:
        assert adding.entry().newline == 'lf' and len(calls['encrypt']) == len(calls['commit']) == 1


def _consume_fresh_clone(adding, monkeypatch, *, plaintext=None):
    data = adding.data_dir.parent / 'fresh consumer data'
    remote = _git(adding.clone, 'remote', 'get-url', 'origin')
    repository.clone(remote, data / 'repo')
    (data / 'identity.txt').write_bytes(b'dummy unlocked identity')
    (data / 'identity.txt').chmod(0o600)
    raw = json.loads(adding.config_path.read_text());raw['machines']['testbox']['profiles'] = ['base']
    adding.config_path.write_text(json.dumps(raw), encoding='utf-8')
    def decrypting(identity, blob):
        if plaintext is not None:
            return plaintext
        payload = Path(blob).read_bytes()
        prefix = b'-----BEGIN AGE ENCRYPTED FILE-----\nage1testrecipient\n'
        suffix = b'-----END AGE ENCRYPTED FILE-----\n'
        assert payload.startswith(prefix) and payload.endswith(suffix)
        return payload[len(prefix):-len(suffix)]
    monkeypatch.setattr(convergence, 'decrypt_with_identity', decrypting)
    result = convergence.converge(adding.config_path, data)
    return result, adding.plain / 'ha-token.txt', data / 'state.json'


def test_actual_published_update_preserves_mode_and_lf_in_fresh_convergence(adding, monkeypatch):
    _seed(adding)
    Path(adding.source).write_bytes(b'updated materialized dummy\n')
    code = adding.cli.main(_argv(adding, '--update', '--profile', 'extra'))
    authored = json.loads(adding.manifest_path.read_text())
    result, target, state_path = _consume_fresh_clone(adding, monkeypatch)
    observed_mode = stat.S_IMODE(target.stat().st_mode)
    observed_bytes = target.read_bytes()
    print('UPDATE_CONSUMER_TRACE ' + json.dumps({'code': code, 'storedMode': authored['entries']['ha-token']['mode'], 'storedNewline': authored['entries']['ha-token'].get('newline'), 'consumerWritten': result.written, 'observedMode': observed_mode}))
    assert code == 0 and result.failures == [] and result.written == 1
    assert authored['entries']['ha-token']['mode'] == '0644' and authored['entries']['ha-token']['newline'] == 'lf'
    assert observed_bytes == b'updated materialized dummy\n'
    if not perms.IS_WINDOWS:
        assert observed_mode == 0o644
    assert State.load(state_path).get('ha-token')['dest'] == str(target)
    assert authored['profiles'] == {'base': ['ha-token'], 'extra': ['ha-token']}


def test_actual_consumer_enforces_inherited_lf_after_published_update(adding, monkeypatch):
    _seed(adding)
    assert adding.cli.main(_argv(adding, '--update')) == 0
    result, target, state_path = _consume_fresh_clone(adding, monkeypatch, plaintext=b'controlled CRLF decrypt\r\n')
    assert len(result.failures) == 1 and result.failures[0].ask_reason is None
    assert 'CRLF' in result.failures[0].agent_msg and result.written == 0 and not target.exists()
    assert State.load(state_path).rows == {}
