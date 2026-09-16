"""Whole-identity rotation publishes under the authoring recovery lifecycle.

Rotation replaces the root of trust, so it is held to the same evidence
standard as an entry add: one exact-ref push, a complete porcelain receipt or
a fresh positive proof, and an identity cache written only after publication
is established. These tests pin the properties that separate that
lifecycle from a bare add/commit/push:

1. exactly ONE push is attempted, and a definite rejection restores;
2. a push whose report is lost after the remote accepted it leaves recovery
   pending rather than claiming nothing was published;
3. a remote that moves between sync and push is a rejection, never an
   automatic rebase that would publish a recipient the concurrent blob is not
   encrypted to;
4. the cache is not replaced before the publication that justifies it;
5. every owned step fails back to the published epoch, and a manifest naming
   a blob outside the reserved slot is refused before a key is generated.
"""

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from sk_publish import fixture_commit_and_push

from secrets_kit import DecryptError, SecretsError, agefile, authoring
from secrets_kit import repo as repository
from test_dest_guard import _armored, _templates, adding  # noqa: F401
from test_init import _seeding_template, seeding  # noqa: F401
from test_sync_view import _author_snapshot, _dummy_crypto, _git, _seed_author

OLD_RECIPIENT = 'age1testrecipient'


def _reject_pushes(remote: Path) -> None:
    """Make the bare remote refuse every push, as a protected remote would."""
    hook = remote / 'hooks/pre-receive'
    hook.write_text('#!/bin/sh\necho dummy rotation rejection >&2\nexit 1\n')
    hook.chmod(0o700)


def _remote_of(clone: Path) -> Path:
    return Path(_git(clone, 'remote', 'get-url', 'origin'))


def _phases(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record every recovery phase this operation actually marks."""
    observed = []
    real_mark = authoring.AuthoringOperation.mark

    def mark(self, phase, **updates):
        observed.append(phase)
        return real_mark(self, phase, **updates)

    monkeypatch.setattr(authoring.AuthoringOperation, 'mark', mark)
    return observed


def _observe_pushes(monkeypatch: pytest.MonkeyPatch, clone: Path, *, before_first=None) -> list:
    """Count the clone's own push invocations, whichever helper issues them."""
    real_run = subprocess.run
    pushes = []

    def run(command, *args, **kwargs):
        if command[:2] == ['git', 'push'] and Path(kwargs.get('cwd') or '.') == clone:
            pushes.append(list(command[1:]))
            if before_first is not None and len(pushes) == 1:
                before_first()
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', run)
    return pushes


def _concurrent_entry(adding: SimpleNamespace) -> str:
    """Another machine publishes an entry encrypted to the OLD recipient."""
    other = adding.data_dir.parent / 'concurrent author'
    repository.clone(str(_remote_of(adding.clone)), other)
    _git(other, 'config', 'user.name', 'dummy')
    _git(other, 'config', 'user.email', 'dummy@example.invalid')
    (other / 'blobs').mkdir(exist_ok=True)
    (other / 'blobs/concurrent.age').write_bytes(
        _armored(OLD_RECIPIENT.encode(), b'dummy concurrent value\n')
    )
    raw = json.loads((other / 'manifest.json').read_text())
    raw['entries']['concurrent'] = {
        'blob': 'blobs/concurrent.age',
        'dest': '${PLAIN}/concurrent.txt',
        'mode': '0600',
    }
    (other / 'manifest.json').write_text(json.dumps(raw, indent=2) + '\n', encoding='utf-8')
    fixture_commit_and_push(
        other, 'dummy concurrent entry', ['manifest.json', 'blobs/concurrent.age']
    )
    return _git(other, 'rev-parse', 'HEAD')


def test_actual_rotation_publication_rejection_preserves_the_identity_cache(
    seeding: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused rotation must not leave a cached key the fleet cannot read.

    The cache names the identity this machine decrypts with. Replacing it
    before publication is proved leaves it naming a recipient that appears in
    no published manifest while the published manifest still names the old
    one -- and convergence decrypts against the local clone, which rotation
    already re-encrypted, so the machine reports healthy while the fleet is
    unrotated.
    """
    assert seeding.cli.main(['init']) == 0
    seeded_cache = seeding.identity.read_bytes()
    seeded_head = _git(seeding.remote, 'rev-parse', 'HEAD')

    # The fixture's keygen is fixed, which would re-derive the seeded manifest
    # byte for byte and leave git with nothing to commit. Rotation has to
    # produce a genuinely different recipient for its publication to exist.
    monkeypatch.setattr(
        agefile, 'keygen', lambda: ('AGE-SECRET-KEY-ROTATED', 'age1rotatedrecipient')
    )
    _reject_pushes(seeding.remote)

    assert seeding.cli.main(['rotate-identity']) != 0
    assert _git(seeding.remote, 'rev-parse', 'HEAD') == seeded_head
    assert seeding.identity.read_bytes() == seeded_cache


def test_actual_rotation_rejection_pushes_once_and_restores_owned_state(
    adding: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One exact-ref push, a definite rejection, then owned restoration.

    The legacy path pushed, fetched, rebased and pushed AGAIN. The second push
    is what makes a rejection indistinguishable from a race resolved behind
    the author's back, so the count is the property, not the exit code.
    """
    _seed_author(adding)
    _dummy_crypto(adding, monkeypatch)
    before = _author_snapshot(adding)
    _reject_pushes(_remote_of(adding.clone))
    phases = _phases(monkeypatch)
    pushes = _observe_pushes(monkeypatch, adding.clone)

    code = adding.cli.main(['rotate-identity'])

    assert code == 1 and len(pushes) == 1
    assert 'definitely_rejected' in phases
    assert phases.index('restored') > phases.index('definitely_rejected')
    assert _author_snapshot(adding) == before
    assert not (adding.data_dir / 'authoring-recovery').exists()


def test_actual_rotation_lost_push_report_leaves_recovery_pending(
    adding: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """An accepted push whose report is lost is UNCERTAIN, never 'nothing was published'.

    The remote applied the commit. A remedy that says nothing was published
    invites a retry against blobs the cached identity can no longer open. The
    pending record then reads back through its own loader, and a fresh proof
    of that publication still refuses to rewrite the cache on the machine's
    behalf -- only unlock replaces a cached identity.
    """
    _seed_author(adding)
    _dummy_crypto(adding, monkeypatch)
    cache = (adding.data_dir / 'identity.txt').read_bytes()
    remote = _remote_of(adding.clone)
    published = _git(remote, 'rev-parse', 'HEAD')
    real_run = subprocess.run
    pushes = []

    def run(command, *args, **kwargs):
        own = command[0] == 'git' and Path(kwargs.get('cwd') or '.') == adding.clone
        if own and command[1] == 'fetch' and '--no-write-fetch-head' in command:
            return subprocess.CompletedProcess(command, 128, b'', b'dummy proof transport failure')
        result = real_run(command, *args, **kwargs)
        if own and command[1] == 'push':
            pushes.append(result.returncode)
            raise subprocess.TimeoutExpired(
                command, kwargs['timeout'], output=result.stdout, stderr=result.stderr
            )
        return result

    with monkeypatch.context() as controlled:
        controlled.setattr(subprocess, 'run', run)
        code = adding.cli.main(['rotate-identity'])
        diagnostic = capsys.readouterr().err

    assert code == 1 and pushes == [0]
    assert _git(remote, 'rev-parse', 'HEAD') != published
    assert 'Nothing was published' not in diagnostic
    marker = adding.data_dir / 'authoring-recovery' / 'marker.json'
    assert json.loads(marker.read_bytes())['phase'] == 'uncertain'
    assert (adding.data_dir / 'identity.txt').read_bytes() == cache

    inspection = authoring._inspect_recovery(adding.data_dir)
    assert inspection['operation'] == 'rotate' and inspection['phase'] == 'uncertain'
    assert inspection['publication_attempted'] and not inspection['cache_compatible']
    with pytest.raises(authoring.AuthoringRecoveryError):
        authoring._reconcile_recovery(adding.data_dir)
    assert (adding.data_dir / 'identity.txt').read_bytes() == cache

    # Separately authorized reconciliation -- what `unlock` would leave behind.
    (adding.data_dir / 'identity.txt').write_bytes(b'dummy new identity\n')
    assert authoring._reconcile_recovery(adding.data_dir)['outcome'] == 'confirmed'
    assert not (adding.data_dir / 'authoring-recovery').exists()


def test_actual_rotation_refuses_a_remote_that_moved_after_synchronization(
    adding: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent blob must not be stranded under a rebased recipient.

    An automatic rebase merges cleanly here -- the recipient sits at the top of
    the manifest and the concurrent entry sits below it -- so the published
    manifest would name the new recipient while the concurrent blob stayed
    encrypted to the old one. Rotation's own message tells users to expect one
    decrypt failure, which is exactly what that data loss looks like.
    """
    _seed_author(adding)
    _dummy_crypto(adding, monkeypatch)
    before = _author_snapshot(adding)
    remote = _remote_of(adding.clone)
    phases = _phases(monkeypatch)
    pushes = _observe_pushes(
        monkeypatch, adding.clone, before_first=lambda: _concurrent_entry(adding)
    )

    code = adding.cli.main(['rotate-identity'])
    after = _author_snapshot(adding)

    assert code == 1 and len(pushes) == 1
    assert 'definitely_rejected' in phases and 'restored' in phases
    assert {key: after[key] for key in ('bytes', 'blobNames', 'head', 'index')} == {
        key: before[key] for key in ('bytes', 'blobNames', 'head', 'index')
    }
    assert not (adding.data_dir / 'authoring-recovery').exists()
    published = adding.data_dir.parent / 'published view'
    repository.clone(str(remote), published)
    assert json.loads((published / 'manifest.json').read_text())['recipient'] == OLD_RECIPIENT
    assert (published / 'blobs/concurrent.age').read_bytes() == _armored(
        OLD_RECIPIENT.encode(), b'dummy concurrent value\n'
    )


ROTATION_FAULTS = ['decrypt', 'encrypt', 'wrap-nonzero', 'wrap-error', 'repeated-recipient', 'commit-refusal']


def _rotation_fault(adding: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, fault: str) -> list:
    """Fail one owned rotation step, the way the real producer would fail."""
    observed = []
    error = SecretsError('dummy controlled rotation failure')
    if fault == 'decrypt':
        def decrypting(identity, blob):
            observed.append('decrypt');raise DecryptError('dummy undecryptable blob')
        monkeypatch.setattr(agefile, 'decrypt_with_identity', decrypting)
    elif fault == 'encrypt':
        def encrypting(recipient, plaintext, target):
            observed.append('encrypt');raise error
        monkeypatch.setattr(agefile, 'encrypt_to_recipient', encrypting)
    elif fault.startswith('wrap'):
        def wrapping(identity, target):
            observed.append('wrap');Path(target).write_bytes(b'dummy partial encrypted wrapper')
            if fault == 'wrap-nonzero':return 9
            raise error
        monkeypatch.setattr(agefile, 'wrap_identity', wrapping)
    elif fault == 'repeated-recipient':
        # The published identity verbatim, so the prepared epoch re-derives the
        # published tree byte for byte -- a keypair that is not fresh.
        def keygen():
            observed.append('keygen');return 'dummy wrapped identity\n', OLD_RECIPIENT
        monkeypatch.setattr(agefile, 'keygen', keygen)
    else:
        real_git = repository._git
        def git(args, *, cwd, timeout):
            if cwd == adding.clone and args[0] == 'commit':
                observed.append('commit');return 128, 'dummy known local write refusal'
            return real_git(args, cwd=cwd, timeout=timeout)
        monkeypatch.setattr(repository, '_git', git)
    return observed


@pytest.mark.parametrize('fault', ROTATION_FAULTS)
def test_actual_rotation_failure_restores_owned_preimages(
    adding: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    """Every owned rotation step fails back to the published epoch.

    A partially written proposed artifact is receipted rather than abandoned,
    so the restoration path is the same one a clean refusal takes.
    """
    _seed_author(adding)
    _dummy_crypto(adding, monkeypatch)
    before = _author_snapshot(adding)
    pushes = _observe_pushes(monkeypatch, adding.clone)
    observed = _rotation_fault(adding, monkeypatch, fault)

    code = adding.cli.main(['rotate-identity'])

    assert code == 1 and observed and pushes == []
    assert _author_snapshot(adding) == before
    assert not (adding.data_dir / 'authoring-recovery').exists()


def test_actual_rotation_refuses_a_published_blob_outside_the_reserved_slot(
    adding: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Rotation re-encrypts whatever the manifest names, so it requires the slot.

    The refusal has to survive synchronization: the layout can arrive from the
    remote between admission and preparation, and it must still be refused
    before a key is generated rather than at the repository-side guard, after
    the passphrase prompt and the checkout rewrite.
    """
    _seed_author(adding)
    other = adding.data_dir.parent / 'nested author'
    repository.clone(str(_remote_of(adding.clone)), other)
    _git(other, 'config', 'user.name', 'dummy')
    _git(other, 'config', 'user.email', 'dummy@example.invalid')
    (other / 'blobs/nested').mkdir(parents=True, exist_ok=True)
    (other / 'blobs/nested/deep.age').write_bytes(_armored(OLD_RECIPIENT.encode(), b'dummy nested value\n'))
    raw = json.loads((other / 'manifest.json').read_text())
    raw['entries']['nested'] = {'blob': 'blobs/nested/deep.age', 'dest': '${PLAIN}/nested.txt', 'mode': '0600'}
    (other / 'manifest.json').write_text(json.dumps(raw, indent=2) + '\n', encoding='utf-8')
    fixture_commit_and_push(other, 'dummy nested layout', ['manifest.json', 'blobs/nested/deep.age'])
    before = _author_snapshot(adding)
    calls = _dummy_crypto(adding, monkeypatch)

    code = adding.cli.main(['rotate-identity'])
    diagnostic = capsys.readouterr().err

    assert code == 1 and list(calls) == []
    assert "blobs/*.age" in diagnostic and "'nested'" in diagnostic
    assert {key: _author_snapshot(adding)[key] for key in ('bytes', 'blobNames', 'head', 'index')} == {
        key: before[key] for key in ('bytes', 'blobNames', 'head', 'index')
    }
    assert not (adding.data_dir / 'authoring-recovery').exists()
