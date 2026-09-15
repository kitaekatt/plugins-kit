"""Private authoring recovery under the existing physical operation owner.

Only encrypted proposed identities are retained. An uncertain publication
blocks ordinary callers until private reconciliation establishes a safe
outcome. File fsync and supported directory fsync do not certify power-loss
recovery or native filesystem access controls.
"""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any, Optional

from . import SecretsError, cli_command
from . import agefile, guard, repo
from .manifest import Manifest
from .perms import _private_output, tighten, tighten_dir

RECOVERY_DIRECTORY = "authoring-recovery"
_SEED_PATHS = ("identity.age", "manifest.json", ".gitignore")
_ROTATION_PATHS = ("identity.age", "manifest.json")
_REMEDY = (
    "Ordinary secrets operations are stopped. Preserve the recovery directory, "
    "clone and identity cache. Inspect and reconcile this operation privately "
    "under the existing operation owner; do not delete recovery material or "
    "retry publication to infer its outcome."
)


class AuthoringRecoveryError(SecretsError):
    """Recovery evidence is pending, incomplete or no longer owned."""


def _recovery_error(reason: str) -> AuthoringRecoveryError:
    return AuthoringRecoveryError(f"secrets authoring recovery required: {reason}", _REMEDY)


def require_no_recovery(data_dir: Path) -> None:
    """Any recovery entry blocks work; do not parse private bytes for callers."""
    try:
        (data_dir / RECOVERY_DIRECTORY).lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise _recovery_error("recovery presence could not be established") from None
    raise _recovery_error("pending or unowned recovery material")


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _ordinary(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not valid or getattr(info, "st_file_attributes", 0) & 0x400:
        raise _recovery_error("a required recovery or authoring slot is not ordinary")
    return info


def _private(path: Path, *, directory: bool = False) -> os.stat_result:
    info = _ordinary(path, directory=directory)
    if os.name == "nt":
        from .operation_lock import _windows_private
        try:_windows_private(path, directory=directory)
        except SecretsError:raise _recovery_error("private recovery access controls could not be established") from None
    elif info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise _recovery_error("recovery ownership or permissions changed")
    return info


def _file_state(path: Path) -> Optional[dict]:
    try:
        info = _ordinary(path)
    except FileNotFoundError:
        return None
    return {"digest": _digest(path.read_bytes()), "mode": stat.S_IMODE(info.st_mode)}


def _directory_sync(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    def produce(stream: Any) -> bool:
        stream.write(payload)
        return True
    if not _private_output(path, mode, produce):
        raise _recovery_error("protected output was not published")
    _directory_sync(path.parent)


def _head(clone: Path) -> Optional[str]:
    if repo._committed_head(clone):
        return repo._owned_oid(clone, "HEAD")
    repo._require_unborn_branch(clone)
    return None


def _branch(clone: Path) -> str:
    value = repo._owned_query(clone, ["symbolic-ref", "--quiet", "HEAD"]).decode("utf-8", "strict").rstrip("\n")
    if not value.startswith("refs/heads/"):
        raise SecretsError("seed requires a symbolic branch")
    repo._owned_query(clone, ["check-ref-format", value])
    repo._require_direct_ref(clone, value)
    return value


def _config(clone: Path, key: str) -> list[str]:
    result = repo._owned_git(clone, ["config", "--null", "--get-all", key])
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return []
    if result.returncode != 0 or result.stderr or not result.stdout.endswith(b"\0"):
        raise SecretsError("seed repository configuration could not be established")
    return [record.decode("utf-8", "strict") for record in result.stdout[:-1].split(b"\0")]


def _admit(clone: Path, declared_repo: str) -> str:
    """Refuse foreign state before fetching, changing checkout or reading keys."""
    _ordinary(clone, directory=True)
    if os.path.lexists(clone / "blobs"):
        _ordinary(clone / "blobs", directory=True)
    git_dir = clone / ".git"
    _ordinary(git_dir, directory=True)
    if Path(repo._owned_query(clone, ["rev-parse", "--show-toplevel"]).decode().rstrip("\n")).resolve() != clone.resolve():
        raise SecretsError("seed repository layout is unsupported")
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        if os.path.lexists(git_dir / name):
            raise SecretsError("seed refused: an unfinished Git operation exists")
    if any(git_dir.rglob("*.lock")):
        raise SecretsError("seed refused: a conflicting Git lock exists")
    guard._owned_hook_target(clone)
    if _config(clone, "core.worktree") or _config(clone, "core.sparseCheckout") not in ([], ["false"]):
        raise SecretsError("seed repository overrides or sparse layout are unsupported")
    if _config(clone, "core.sharedRepository") not in ([], ["false"], ["0"]):
        raise SecretsError("seed shared repository permissions are unsupported")
    physical_git = Path(repo._owned_query(clone, ["rev-parse", "--absolute-git-dir"]).decode().rstrip("\n"))
    common_git = Path(repo._owned_query(clone, ["rev-parse", "--git-common-dir"]).decode().rstrip("\n"))
    if not common_git.is_absolute():common_git = clone / common_git
    if physical_git.resolve() != git_dir.resolve() or common_git.resolve() != git_dir.resolve():
        raise SecretsError("seed shared Git metadata layout is unsupported")
    if os.path.lexists(git_dir / "index"):
        _ordinary(git_dir / "index")
    if any(git_dir.glob("sharedindex.*")):
        raise SecretsError("seed split-index layout is unsupported")
    if repo._owned_query(clone, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]):
        raise SecretsError("seed refused: index or checkout contains unrelated changes")
    hooks = git_dir / "hooks"
    if hooks.exists():
        _ordinary(hooks, directory=True)
        for path in hooks.iterdir():
            if path.name.endswith(".sample"):
                continue
            info = _ordinary(path)
            if info.st_mode & 0o111 or os.name == "nt":
                if path.name != "pre-commit" or not guard._is_guarded_at(path):
                    raise SecretsError("seed refused: a foreign active Git hook exists")
    for name in _SEED_PATHS:
        if _file_state(clone / name) is not None:
            tracked = repo._owned_git(clone, ["ls-files", "--error-unmatch", "--", name])
            if tracked.returncode != 0:
                raise SecretsError("seed refused: ignored or untracked material occupies an authoring slot")
    branch = _branch(clone)
    if _config(clone, "remote.origin.pushurl"):
        raise SecretsError("seed requires one exact fetch and publication destination")
    if _config(clone, "remote.origin.mirror") not in ([], ["false"]):
        raise SecretsError("seed refuses mirror publication")
    remote = _config(clone, f"branch.{branch[11:]}.remote")
    merge = _config(clone, f"branch.{branch[11:]}.merge")
    if remote not in ([], ["origin"]) or merge not in ([], [branch]):
        raise SecretsError("seed requires one unambiguous branch publication target")
    if _head(clone) is not None and (remote != ["origin"] or merge != [branch]):
        raise SecretsError("seed established branch has no exact origin upstream")
    return branch


def _eligible(name: str) -> bool:
    return name in _SEED_PATHS or name.startswith("blobs/") and name.endswith(".age") and len(Path(name).parts) == 2


def _proposed_names(record: dict) -> set:
    """The proposed-output artifacts this OPERATION is allowed to retain."""
    if record["operation"] == "seed":
        return {"proposed-identity", "proposed-ignore"}
    if record["operation"] == "rotate":
        return {"proposed-identity"} | {"proposed-blob-" + str(index)
                                        for index in range(len(record["rotation_blobs"]))}
    return set() if record["operation"] == "remove" else {"proposed-blob"}


_PUBLICATION_REASONS = {
    "fresh proof unavailable", "publication proof ref is not exclusively available",
    "fresh branch reachability", "fresh branch does not prove reachability",
    "publication proof cleanup incomplete", "complete exact-ref push receipt", "complete exact-ref rejection",
    "binding refused before push invocation",
}


@dataclass
class AuthoringOperation:
    data_dir: Path
    clone: Path
    declared_repo: str
    record: dict
    directory_identity: tuple[int, int]
    marker_identity: Optional[tuple[int, int]] = None
    broken: bool = False

    @property
    def directory(self) -> Path:
        return self.data_dir / RECOVERY_DIRECTORY

    def _custody(self) -> None:
        info = _private(self.directory, directory=True)
        if (info.st_dev, info.st_ino) != self.directory_identity:
            raise _recovery_error("recovery directory identity changed")
        for path, key in [(self.clone, "clone_identity"), (self.clone / ".git", "git_identity")]:
            physical = _ordinary(path, directory=True)
            if [physical.st_dev, physical.st_ino] != self.record[key]:
                raise _recovery_error("physical clone binding changed")
        if self.marker_identity is not None:
            marker = _private(self.directory / "marker.json")
            if (marker.st_dev, marker.st_ino) != self.marker_identity:
                raise _recovery_error("recovery marker identity changed")
        if self.broken:
            raise _recovery_error("a recovery record update failed")

    def mark(self, phase: str, **updates: Any) -> None:
        self._custody()
        proposed = dict(self.record, phase=phase, **updates)
        if self._exact_receipts:
            names = {path.name for path in self.directory.iterdir()} - {"marker.json"}
            if names != set(self.record["artifacts"]):
                raise _recovery_error("recovery directory contains unreceipted material")
            for name, expected in self.record["artifacts"].items():
                if _file_state(self.directory / name) != expected:
                    raise _recovery_error("recovery artifact changed outside its owned producer")
        else:
            proposed["artifacts"] = {path.name: _file_state(path) for path in self.directory.iterdir()
                                     if path.name != "marker.json"}
        self._persist(proposed)

    def _remember_artifact(self, name: str) -> None:
        """Receipt a known completed allocation or write, without adopting others."""
        self._custody()
        _private(self.directory / name)
        artifacts = dict(self.record["artifacts"])
        artifacts[name] = _file_state(self.directory / name)
        self._persist(dict(self.record, artifacts=artifacts))

    @property
    def _exact_receipts(self) -> bool:
        """Version 1 re-derives its receipts; later records receipt exactly."""
        return self.record["version"] != 1

    def _published(self) -> bool:
        return self.record["phase"] in ("confirmed", "finalizing") or (
            self.record["phase"] == "cleaning" and self.record.get("cleanup_outcome") == "confirmed")

    def _entry_cache_compatible(self) -> bool:
        return _file_state(self.data_dir / "identity.txt") == self.record.get("entry_cache")

    def _cache_digest_compatible(self) -> bool:
        cache = self.data_dir / "identity.txt"
        return _file_state(cache) is not None and _digest(cache.read_bytes()) == self.record.get("cache_digest")

    def _cache_compatible(self) -> bool:
        """Report the cache against the field this record proposes or retains."""
        if "cache_digest" in self.record:
            return self._cache_digest_compatible()
        return "entry_cache" in self.record and self._entry_cache_compatible()

    def _require_cache(self) -> None:
        """Require the cache this PHASE expects, over fields and not versions.

        A retained cache must hold while publication is still being
        established. A record that proposes a replacement identity must hold
        that replacement once its publication is established; one that
        proposes none keeps the retained expectation throughout. An
        established publication whose cache is still the retained one is never
        rewritten automatically -- it is reported for private reconciliation.
        """
        if self._published() and "cache_digest" in self.record:
            if not self._cache_digest_compatible():
                raise _recovery_error(
                    "publication confirmed; cache not finalized"
                    if "entry_cache" in self.record and self._entry_cache_compatible()
                    else "publication confirmed; existing cache is not compatible")
            return
        if "entry_cache" in self.record and not self._entry_cache_compatible():
            raise _recovery_error("identity cache changed during authoring")

    def refuse(self) -> int:
        """Finalize a return-only validation refusal without inventing a primary."""
        try:
            self.restore()
        except BaseException as error:
            print(_recovery_error("validation refused; owned restoration incomplete"), file=sys.stderr)
            if not isinstance(error, Exception):
                raise
        return 1

    def _persist(self, proposed: dict) -> None:
        """Advance a protected marker after the corresponding durable effects."""
        self._custody()
        try:
            proposed["directory_identity"] = list(self.directory_identity)
            def produce(stream: Any) -> bool:
                receiving = os.fstat(stream.fileno())
                proposed["marker_identity"] = [receiving.st_dev, receiving.st_ino]
                stream.write(json.dumps(proposed, sort_keys=True).encode())
                return True
            if not _private_output(self.directory / "marker.json", 0o600, produce):
                raise _recovery_error("recovery marker was not published")
            _directory_sync(self.directory)
            info = _ordinary(self.directory / "marker.json")
            self.marker_identity = (info.st_dev, info.st_ino)
            self.record = proposed
        except BaseException:
            self.broken = True
            raise

    def capture(self, name: str) -> None:
        if name in self.record["preimages"]:
            return
        state = _file_state(self.clone / name)
        if state is not None:
            stored = "preimage-" + str(len(self.record["preimages"]))
            _write(self.directory / stored, (self.clone / name).read_bytes())
            state = dict(state, stored=stored)
        self.record["preimages"][name] = state
        if state is not None and self._exact_receipts:
            self._remember_artifact(state["stored"])

    def prepare(self) -> tuple[str, str, int]:
        """Produce the complete encrypted epoch before replacing repo files."""
        identity, recipient = agefile.keygen()
        wrapped = self.directory / "proposed-identity"
        fd = os.open(wrapped, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        tighten(wrapped, 0o600)
        before = _ordinary(wrapped)
        code = agefile.wrap_identity(identity, wrapped)
        after = _ordinary(wrapped)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise _recovery_error("proposed encrypted identity slot changed")
        if code != 0:
            return identity, recipient, code
        if not wrapped.read_bytes():
            raise SecretsError("age produced no encrypted identity")
        with wrapped.open("rb") as stream:
            os.fsync(stream.fileno())
        _directory_sync(self.directory)
        manifest = Manifest(self.clone / "manifest.json", {"version": 1, "recipient": recipient, "profiles": {}, "entries": {}})
        _write(self.directory / "proposed-manifest", manifest.dump().encode("utf-8"))
        outputs = {"identity.age": "proposed-identity", "manifest.json": "proposed-manifest"}
        if not (self.clone / ".gitignore").exists():
            _write(self.directory / "proposed-ignore", guard.GITIGNORE.encode("utf-8"))
            outputs[".gitignore"] = "proposed-ignore"
        self.record["outputs"] = {name: dict(_file_state(self.directory / stored), stored=stored)
                                   for name, stored in outputs.items()}
        tree = self._prepare_tree()
        cache_bytes = identity.replace("\n", os.linesep).encode("utf-8")
        self.mark("prepared", expected_tree=tree, cache_digest=_digest(cache_bytes))
        return identity, recipient, 0

    def _receiving_slot(self, stored: str) -> Path:
        """Allocate an owned empty private artifact and record its identity."""
        path = self.directory / stored
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        tighten(path, 0o600)
        receiving = _private(path)
        self.record["receiving_identities"][stored] = [receiving.st_dev, receiving.st_ino]
        _directory_sync(self.directory)
        self._remember_artifact(stored)
        return path

    def _durable_artifact(self, stored: str, *, require_content: bool = True) -> dict:
        """Verify an owned producer wrote its own slot, then make it durable."""
        path = self.directory / stored
        actual = _private(path)
        if [actual.st_dev, actual.st_ino] != self.record["receiving_identities"][stored]:
            raise _recovery_error("a proposed encrypted slot changed under its producer")
        if require_content and not actual.st_size:
            raise SecretsError("age produced no encrypted output")
        with path.open("rb") as stream:os.fsync(stream.fileno())
        _directory_sync(self.directory)
        self._remember_artifact(stored)
        return dict(_file_state(path), stored=stored)

    def _salvage_artifact(self, primary: BaseException, stored: str) -> None:
        """Receipt a failed producer's own slot, or diagnose the custody gap."""
        try:
            self._durable_artifact(stored, require_content=False)
        except BaseException:
            primary.authoring_recovery_error = _recovery_error(
                "failed encryption artifact custody or durability is incomplete")
            if hasattr(primary, "add_note"):primary.add_note(str(primary.authoring_recovery_error))

    def prepare_rotation(self, identity: str, recipient: str, manifest_bytes: bytes) -> int:
        """Produce the complete replacement epoch before replacing repo files.

        Only bytes that will be committed verbatim reach the recovery
        directory. Each entry is decrypted and re-encrypted one at a time
        through age's own pipes, so no entry plaintext -- and nothing derived
        from one -- is ever written beside the proposed ciphertext.
        """
        self._custody()
        self._require_cache()
        outputs = {}
        for index, name in enumerate(self.record["rotation_blobs"]):
            stored = "proposed-blob-" + str(index)
            path = self._receiving_slot(stored)
            try:
                agefile.encrypt_to_recipient(recipient, agefile.decrypt_with_identity(
                    self.data_dir / "identity.txt", self.clone / name), path)
            except BaseException as primary:
                self._salvage_artifact(primary, stored)
                raise
            outputs[name] = self._durable_artifact(stored)
        wrapped = self._receiving_slot("proposed-identity")
        try:
            code = agefile.wrap_identity(identity, wrapped)
        except BaseException as primary:
            self._salvage_artifact(primary, "proposed-identity")
            raise
        if code != 0:
            self._durable_artifact("proposed-identity", require_content=False)
            return code
        outputs["identity.age"] = self._durable_artifact("proposed-identity")
        _write(self.directory / "proposed-manifest", manifest_bytes)
        self._remember_artifact("proposed-manifest")
        outputs["manifest.json"] = dict(_file_state(self.directory / "proposed-manifest"), stored="proposed-manifest")
        authored = ["identity.age", "manifest.json"] + list(self.record["rotation_blobs"])
        self.record["outputs"] = {name: outputs[name] for name in authored}
        tree = self._prepare_tree()
        if tree == repo._owned_oid(self.clone, self.record["synced_head"] + "^{tree}"):
            raise SecretsError(
                "rotation refused: the replacement epoch is identical to the published one",
                "A fresh identity always changes the manifest recipient, so this "
                "means the generated keypair is not fresh. Nothing was published.")
        cache_bytes = identity.replace("\n", os.linesep).encode("utf-8")
        self.mark("prepared", expected_tree=tree, authored_paths=authored,
                  cache_digest=_digest(cache_bytes))
        return 0

    def _prepare_tree(self) -> str:
        private_index = self.directory / "prepared-index"
        algorithm = repo._owned_query(self.clone, ["rev-parse", "--show-object-format"]).decode("ascii").rstrip("\n")
        if algorithm not in ("sha1", "sha256"):
            raise SecretsError("seed Git object format is unsupported")
        header = b"DIRC\x00\x00\x00\x02\x00\x00\x00\x00"
        _write(private_index, header + hashlib.new(algorithm, header).digest())
        if self._exact_receipts:
            self._remember_artifact("prepared-index")
        repo._owned_query(self.clone, ["read-tree", self.record["synced_head"]] if self.record["synced_head"] else ["read-tree", "--empty"], index=private_index)
        tighten(private_index, 0o600)
        for name, state in self.record["outputs"].items():
            if state is None:
                repo._owned_query(self.clone, ["update-index", "--force-remove", "--", name], index=private_index)
                continue
            stored = state["stored"]
            result = repo._owned_git(self.clone, ["hash-object", "-w", "--stdin"], payload=(self.directory / stored).read_bytes())
            oid = result.stdout.decode("ascii").rstrip("\n")
            if result.returncode != 0 or not repo._object_id(oid):
                raise SecretsError("prepared encrypted Git object could not be established")
            repo._owned_query(self.clone, ["update-index", "--add", "--cacheinfo", f"100644,{oid},{name}"], index=private_index)
        tree = repo._owned_query(self.clone, ["write-tree"], index=private_index).decode("ascii").rstrip("\n")
        with private_index.open("rb") as stream:os.fsync(stream.fileno())
        _directory_sync(self.directory)
        if self._exact_receipts:
            self._remember_artifact("prepared-index")
        return tree

    def prepare_entry(self, manifest_bytes: bytes, recipient: str, plaintext: Optional[bytes]) -> None:
        """Prepare an exact manifest and encrypted write or explicit deletion."""
        self._custody()
        self._require_cache()
        name = self.record["selected_blob"]
        if not name:
            raise SecretsError("entry authoring has no supported selected blob")
        _write(self.directory / "proposed-manifest", manifest_bytes)
        self._remember_artifact("proposed-manifest")
        manifest_state = dict(_file_state(self.directory / "proposed-manifest"), stored="proposed-manifest")
        if self.record["operation"] == "remove":
            if self.record["synced_files"].get(name) is None or repo._owned_git(self.clone, ["ls-files", "--error-unmatch", "--", name]).returncode != 0:
                raise SecretsError("remove requires its selected blob in the synchronized checkout and index")
            output = None
        else:
            if plaintext is None:
                raise SecretsError("entry encryption requires plaintext bytes")
            path = self.directory / "proposed-blob"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            tighten(path, 0o600)
            receiving = _private(path)
            self.record["blob_receiving_identity"] = [receiving.st_dev, receiving.st_ino]
            _directory_sync(self.directory)
            self._remember_artifact("proposed-blob")
            try:
                agefile.encrypt_to_recipient(recipient, plaintext, path)
            except BaseException as primary:
                try:
                    actual = _private(path)
                    if (actual.st_dev, actual.st_ino) != (receiving.st_dev, receiving.st_ino):
                        raise _recovery_error("failed encryption changed its owned receiving slot")
                    with path.open("rb") as stream:os.fsync(stream.fileno())
                    _directory_sync(self.directory)
                    self._remember_artifact("proposed-blob")
                except BaseException as recovery:
                    primary.authoring_recovery_error = _recovery_error("failed encryption artifact custody or durability is incomplete")
                    if hasattr(primary, "add_note"):primary.add_note(str(primary.authoring_recovery_error))
                raise
            actual = _private(path)
            if (actual.st_dev, actual.st_ino) != (receiving.st_dev, receiving.st_ino):
                raise _recovery_error("proposed encrypted blob slot changed")
            if not path.read_bytes():
                raise SecretsError("age produced no encrypted entry")
            with path.open("rb") as stream:os.fsync(stream.fileno())
            _directory_sync(self.directory)
            self._remember_artifact("proposed-blob")
            output = dict(_file_state(path), stored="proposed-blob")
        self.record["outputs"] = {name: output, "manifest.json": manifest_state}
        tree = self._prepare_tree()
        self.mark("prepared", expected_tree=tree, authored_paths=[name, "manifest.json"])

    def apply_entry(self, message: str) -> None:
        self._custody()
        self._require_cache()
        if self.record["expected_tree"] == repo._owned_oid(self.clone, self.record["synced_head"] + "^{tree}"):
            self._verify_synchronized()
            self.mark("unchanged")
            self.cleanup()
            return
        self._publish_outputs(message)
        self._require_cache()
        self.cleanup()

    def _verify_synchronized(self) -> None:
        if _branch(self.clone) != self.record["branch"] or _head(self.clone) != self.record["synced_head"]:
            raise _recovery_error("synchronized branch changed before authoring")
        for name, expected in self.record["synced_files"].items():
            if _file_state(self.clone / name) != expected:
                raise _recovery_error("authoring footprint changed after preparation")
        if self._exact_receipts:
            if _file_state(self.clone / ".git/index") != self.record["synced_index"]:
                raise _recovery_error("synchronized index changed before authoring")
            _admit(self.clone, self.declared_repo)

    def apply_and_publish(self, identity: str) -> None:
        self._publish_outputs("seed: fleet identity + empty manifest")
        # An unborn branch's first explicit push needs its later consumer upstream.
        short = self.record["branch"][11:]
        repo._owned_query(self.clone, ["config", f"branch.{short}.remote", "origin"])
        repo._owned_query(self.clone, ["config", f"branch.{short}.merge", self.record["branch"]])
        self._finalize_cache(identity)

    def apply_rotation(self, identity: str) -> None:
        """Publish the replacement epoch before the cache that reads it."""
        self._publish_outputs("rotate: fleet identity")
        self._finalize_cache(identity)

    def _finalize_cache(self, identity: str) -> None:
        def produce(stream: Any) -> bool:
            stream.write(identity)
            return True
        if not _private_output(self.data_dir / "identity.txt", 0o600, produce, text=True):
            raise _recovery_error("publication confirmed; identity cache finalization incomplete")
        _directory_sync(self.data_dir)
        if _digest((self.data_dir / "identity.txt").read_bytes()) != self.record["cache_digest"]:
            raise _recovery_error("publication confirmed; identity cache differs")
        self.cleanup()

    def _publish_outputs(self, message: str) -> None:
        self._custody()
        self._require_cache()
        self._verify_synchronized()
        self.mark("applying")
        if self.record["operation"] in ("add", "update") and not (self.clone / "blobs").exists():
            (self.clone / "blobs").mkdir(mode=0o700)
            parent = _ordinary(self.clone / "blobs", directory=True)
            if self.record["entry_blob_parent"] is None:
                self.mark("applying", created_blob_parent=[parent.st_dev, parent.st_ino])
            _directory_sync(self.clone)
        for name, state in self.record["outputs"].items():
            if state is None:
                (self.clone / name).unlink()
                _directory_sync((self.clone / name).parent)
                continue
            prepared = self.directory / state["stored"]
            if _file_state(prepared) != {key: state[key] for key in ("digest", "mode")}:
                raise _recovery_error("prepared encrypted output changed")
            _write(self.clone / name, prepared.read_bytes(), 0o644)
        commit = repo._commit_owned(self.clone, message, self.record.get("authored_paths", list(self.record["outputs"])),
                                    expected_parent=self.record["synced_head"], expected_tree=self.record["expected_tree"])
        proof = repo._publication_ref(commit)
        existing = repo._owned_git(self.clone, ["rev-parse", "--verify", "--quiet", proof])
        repo._require_direct_ref(self.clone, proof)
        if existing.returncode != 1 or existing.stdout or existing.stderr:
            raise _recovery_error("publication proof namespace contains unowned material")
        self.mark("publishing", commit_oid=commit, proof_ref=proof)
        try:
            publication_origin = repo._recorded_origin(self.clone)
            if _digest(publication_origin.encode("utf-8")) != self.record["declared_repo_digest"]:
                raise repo.RepoBindingError("repository binding changed before publication")
            # Re-read the directly recorded origin at the publication boundary.
            # The admission query proves the starting binding, but a changed
            # local origin must also be rejected immediately before the only
            # push invocation.
            evidence = repo._publish_owned(
                self.clone,
                commit_oid=commit,
                target_ref=self.record["branch"],
                declared_repo=publication_origin,
                binding_checked=True,
            )
        except repo.RepoBindingError:
            # This helper's binding check precedes its only push invocation.
            self.mark("unsubmitted", publication_reason="binding refused before push invocation")
            raise
        self.mark(evidence.outcome, publication_reason=evidence.reason)
        if evidence.outcome == "definitely_rejected":
            raise SecretsError("publication was definitely rejected")
        if evidence.outcome != "confirmed":
            raise _recovery_error("publication outcome is uncertain")
        self.mark("finalizing")
        if evidence.reason == "publication proof cleanup incomplete":
            raise _recovery_error("publication confirmed; proof cleanup incomplete")

    def restore(self) -> None:
        """Restore enumerated owned effects using branch CAS and exact index bytes."""
        self._custody()
        self._require_cache()
        if self.record["phase"] == "capturing":
            raise _recovery_error("durable entry capture is incomplete")
        if self.record["phase"] in ("publishing", "uncertain", "confirmed", "finalizing", "cleaning", "unchanged"):
            raise _recovery_error("publication has not been proved absent")
        payloads = {}
        for name, preimage in self.record["preimages"].items():
            if preimage is not None:
                path = self.directory / preimage["stored"]
                _private(path)
                payload = path.read_bytes()
                if _digest(payload) != preimage["digest"]:
                    raise _recovery_error("preimage integrity failed before restoration")
                payloads[name] = payload
        index_payload = None
        if self.record["index"] is not None:
            _private(self.directory / "entry-index")
            index_payload = (self.directory / "entry-index").read_bytes()
            if _digest(index_payload) != self.record["index"]["digest"]:
                raise _recovery_error("index preimage integrity failed before restoration")
        if _branch(self.clone) != self.record["branch"]:
            raise _recovery_error("symbolic branch changed during authoring")
        current_head = _head(self.clone)
        allowed_heads = {self.record["entry_head"], self.record.get("synced_head"), self.record.get("commit_oid")}
        if current_head not in allowed_heads:
            # A commit may have completed before its report was lost. Exact parent/tree
            # establishes custody only for this operation's proposed commit.
            if not current_head or not self.record.get("expected_tree"):
                raise _recovery_error("branch tip is not owned by this operation")
            tree = repo._owned_oid(self.clone, "HEAD^{tree}")
            parents = repo._owned_query(self.clone, ["rev-list", "--parents", "-n", "1", current_head]).decode().split()
            if tree != self.record["expected_tree"] or parents != [current_head] + ([self.record.get("synced_head")] if self.record.get("synced_head") else []):
                raise _recovery_error("branch tip is not the prepared commit")
        for name, preimage in self.record["preimages"].items():
            actual = _file_state(self.clone / name)
            possibilities = [preimage]
            if name in self.record.get("synced_files", {}):possibilities.append(self.record["synced_files"][name])
            possibilities = [None if state is None else {key: state[key] for key in ("digest", "mode")} for state in possibilities]
            # Applied encrypted files have repository mode, not journal mode.
            if name in self.record.get("outputs", {}):
                output = self.record["outputs"][name]
                possibilities.append(None if output is None else {"digest": output["digest"], "mode": 0o644})
            if actual not in possibilities:
                raise _recovery_error("an authoring path contains foreign edits")
        staged = repo._owned_query(self.clone, ["diff", "--cached", "--name-only", "-z", current_head] if current_head else ["diff", "--cached", "--name-only", "-z"])
        if any(name.decode() not in self.record["preimages"] for name in staged.split(b"\0") if name):
            raise _recovery_error("index contains changes outside the owned footprint")
        rows = repo._owned_query(self.clone, ["ls-files", "--stage", "-z", "--", *self.record["preimages"]])
        entries = {}
        for row in rows.split(b"\0"):
            if not row:continue
            header, raw_name = row.split(b"\t", 1)
            fields = header.split()
            name = raw_name.decode("utf-8", "strict")
            if len(fields) != 3 or fields[0] not in (b"100644", b"100755") or fields[2] != b"0" or name in entries:
                raise _recovery_error("owned index entries are unsupported or unresolved")
            payload = repo._owned_query(self.clone, ["cat-file", "blob", fields[1].decode("ascii")])
            entries[name] = {"digest": _digest(payload), "mode": 0o755 if fields[0] == b"100755" else 0o644}
        for name, preimage in self.record["preimages"].items():
            known = [preimage]
            if name in self.record.get("synced_files", {}):known.append(self.record["synced_files"][name])
            if name in self.record.get("outputs", {}):known.append(self.record["outputs"][name])
            allowed = [None if state is None else {"digest": state["digest"], "mode": 0o755 if state["mode"] & 0o111 else 0o644} for state in known]
            if entries.get(name) not in allowed:
                raise _recovery_error("index contains foreign staged content at an owned path")
        parent_identity = self.record.get("created_blob_parent")
        if parent_identity and (self.clone / "blobs").exists():
            parent = _ordinary(self.clone / "blobs", directory=True)
            if [parent.st_dev, parent.st_ino] != parent_identity:
                raise _recovery_error("created encrypted-blob parent changed")
            names = {Path(name).name for name in self.record["preimages"] if name.startswith("blobs/")}
            if any(path.name not in names for path in (self.clone / "blobs").iterdir()):
                raise _recovery_error("created encrypted-blob parent contains foreign material")
        if current_head != self.record["entry_head"]:
            args = ["update-ref", "--no-deref", self.record["branch"], self.record["entry_head"], current_head] if self.record["entry_head"] else ["update-ref", "--no-deref", "-d", self.record["branch"], current_head]
            repo._owned_query(self.clone, args)
        for name, state in self.record["preimages"].items():
            path = self.clone / name
            if state is None:
                if path.exists():path.unlink()
            else:
                payload = payloads[name]
                if _file_state(path) != {key: state[key] for key in ("digest", "mode")}:
                    _write(path, payload, state["mode"])
        index = self.clone / ".git/index"
        state = self.record["index"]
        if state is None:
            if index.exists():_ordinary(index);index.unlink()
        else:
            payload = index_payload
            _write(index, payload, state["mode"])
        if _head(self.clone) != self.record["entry_head"] or _file_state(index) != state:
            raise _recovery_error("restoration could not be verified")
        for name, preimage in self.record["preimages"].items():
            expected = None if preimage is None else {key: preimage[key] for key in ("digest", "mode")}
            if _file_state(self.clone / name) != expected:
                raise _recovery_error("restored authoring file differs from its preimage")
        if parent_identity and (self.clone / "blobs").exists():
            parent = _ordinary(self.clone / "blobs", directory=True)
            if [parent.st_dev, parent.st_ino] != parent_identity:
                raise _recovery_error("created encrypted-blob parent changed before restoration cleanup")
            (self.clone / "blobs").rmdir()
            _directory_sync(self.clone)
        self.mark("restored")
        self.cleanup()

    def cleanup(self) -> None:
        self._custody()
        self._require_cache()
        allowed = {"marker.json", "entry-index", "prepared-index", "proposed-manifest"}
        allowed.update(_proposed_names(self.record))
        allowed.update(state["stored"] for state in self.record["preimages"].values() if state)
        paths = list(self.directory.iterdir())
        if any(path.name not in allowed for path in paths):
            raise _recovery_error("recovery directory contains unowned material")
        for path in paths:
            _private(path)
            if path.name != "marker.json" and _file_state(path) != self.record["artifacts"].get(path.name):
                raise _recovery_error("cleanup artifact integrity changed")
        if self.record["phase"] != "cleaning":
            outcome = "unchanged" if self.record["phase"] == "unchanged" else "confirmed" if self.record["phase"] in ("confirmed", "finalizing") else "restored"
            self.mark("cleaning", cleanup_outcome=outcome, cleanup_remaining=sorted(path.name for path in paths if path.name != "marker.json"), cleanup_next=None)
        for name in list(self.record["cleanup_remaining"]):
            path = self.directory / name
            planned_missing = self.record.get("cleanup_next") == name and not os.path.lexists(path)
            if not os.path.lexists(path) and not planned_missing:
                raise _recovery_error("cleanup artifact disappeared without an owned deletion")
            self._persist(dict(self.record, cleanup_next=name))
            if os.path.lexists(path):
                _private(path)
                if _file_state(path) != self.record["artifacts"].get(name):
                    raise _recovery_error("cleanup artifact no longer matches its owned receipt")
                path.unlink()
                _directory_sync(self.directory)
            remaining = [item for item in self.record["cleanup_remaining"] if item != name]
            artifacts = dict(self.record["artifacts"])
            artifacts.pop(name, None)
            self._persist(dict(self.record, cleanup_next=None, cleanup_remaining=remaining, artifacts=artifacts))
        self._custody()
        marker = self.directory / "marker.json"
        marker.unlink()
        self.marker_identity = None
        try:
            self.directory.rmdir()
        except OSError:
            # Directory removal can fail after the last unlink. Restore the same
            # owned record without adopting unexpected directory contents.
            self._persist(dict(self.record))
            raise
        _directory_sync(self.data_dir)

    def failure(self, primary: BaseException) -> None:
        try:
            self.restore()
        except BaseException as recovery:
            diagnostic = recovery if isinstance(recovery, AuthoringRecoveryError) else _recovery_error("owned restoration or finalization incomplete")
            primary.authoring_recovery_error = diagnostic
            if hasattr(primary, "add_note"):primary.add_note(str(diagnostic))


def _canonical_blob(blob: str) -> bool:
    """The one reserved direct blobs/<leaf>.age slot, spelled exactly."""
    return blob.startswith("blobs/") and _eligible(blob) and "\\" not in blob and blob == Path(blob).as_posix()


def _selected_entry_blob(manifest: Manifest, name: str, source_name: Optional[str]) -> Optional[str]:
    """Select a supported literal slot from an entry or new-source basename."""
    blob = manifest.entries[name].blob if name in manifest.entries else ("blobs/" + source_name + ".age" if source_name is not None else None)
    if blob is not None and not _canonical_blob(blob):
        raise SecretsError("entry authoring requires a canonical direct blobs/*.age slot")
    return blob


def _rotation_blobs(manifest: Manifest) -> list[str]:
    """Every encrypted entry slot a rotation re-encrypts, refusing others.

    Rotation rewrites whatever the manifest names, so it requires the same
    canonical slot that entry authoring enforces on the way in. A slot the
    recovery record cannot express is one an interrupted rotation could not
    restore, and the repository-side guard would refuse its commit anyway --
    after the passphrase prompt and after the checkout had been rewritten.
    """
    selected = set()
    for name, entry in manifest.entries.items():
        if not _canonical_blob(entry.blob):
            raise SecretsError(
                f"rotation requires a canonical direct blobs/*.age slot; entry '{name}' names another layout",
                f"Re-add that entry with `{cli_command('add')} {name} --file <plaintext> --update` so its "
                "ciphertext occupies the reserved slot, then rotate. Nothing was changed.")
        selected.add(entry.blob)
    return sorted(selected)


def _capture_rotation_blobs(operation: AuthoringOperation, manifest: Manifest) -> list[str]:
    selected = _rotation_blobs(manifest)
    for name in selected:
        state = _file_state(operation.clone / name)
        if state is not None and repo._owned_git(operation.clone, ["ls-files", "--error-unmatch", "--", name]).returncode != 0:
            raise SecretsError("rotation refused: unowned material occupies a selected blob")
        operation.capture(name)
    return selected


def _capture_entry_selection(operation: AuthoringOperation, manifest: Manifest, name: str,
                             source_name: Optional[str]) -> Optional[str]:
    selected = _selected_entry_blob(manifest, name, source_name)
    if selected is not None:
        state = _file_state(operation.clone / selected)
        if state is not None and repo._owned_git(operation.clone, ["ls-files", "--error-unmatch", "--", selected]).returncode != 0:
            raise SecretsError("entry authoring refused: unowned material occupies the selected blob")
        operation.capture(selected)
    return selected


def _prepare_seed(data_dir: Path, clone_dir: Path, declared_repo: str, *, force: bool) -> AuthoringOperation:
    return _prepare_operation(data_dir, clone_dir, declared_repo, force=force)


def _prepare_entry(data_dir: Path, clone_dir: Path, declared_repo: str, *, name: str,
                   source_name: Optional[str]) -> AuthoringOperation:
    return _prepare_operation(data_dir, clone_dir, declared_repo, force=False,
                              entry_name=name, source_name=source_name)


def _prepare_rotation(data_dir: Path, clone_dir: Path, declared_repo: str) -> AuthoringOperation:
    return _prepare_operation(data_dir, clone_dir, declared_repo, force=False, rotate=True)


def _prepare_operation(data_dir: Path, clone_dir: Path, declared_repo: str, *, force: bool,
                       entry_name: Optional[str] = None, source_name: Optional[str] = None,
                       rotate: bool = False) -> AuthoringOperation:
    """Caller holds the physical owner; snapshot entry state before sync."""
    created = not repo.is_clone(clone_dir)
    if created:
        repo._clone_for_authoring(declared_repo, clone_dir)
    else:
        _ordinary(clone_dir, directory=True)
        repo.require_repo_binding(clone_dir, declared_repo)
    branch = _admit(clone_dir, declared_repo)
    entry = _head(clone_dir)
    index = _file_state(clone_dir / ".git/index")
    if entry_name is not None:
        manifest = Manifest.load(clone_dir / "manifest.json")
        selected = _selected_entry_blob(manifest, entry_name, source_name)
        if selected is not None and _file_state(clone_dir / selected) is not None and repo._owned_git(clone_dir, ["ls-files", "--error-unmatch", "--", selected]).returncode != 0:
            raise SecretsError("entry authoring refused: unowned material occupies the selected blob")
        parent = _ordinary(clone_dir / "blobs", directory=True) if (clone_dir / "blobs").exists() else None
    if rotate:
        manifest = Manifest.load(clone_dir / "manifest.json")
        _rotation_blobs(manifest)
    if entry_name is not None or rotate:
        entry_cache = _file_state(data_dir / "identity.txt")
    directory = data_dir / RECOVERY_DIRECTORY
    directory.mkdir(mode=0o700)
    try:
        tighten_dir(directory)
        info = _ordinary(directory, directory=True)
        record = {"version": 1, "operation": "seed", "phase": "admitted", "force": force,
                  "clone_identity": list((_ordinary(clone_dir, directory=True).st_dev, clone_dir.stat().st_ino)),
                  "git_identity": list((clone_dir.joinpath(".git").stat().st_dev, clone_dir.joinpath(".git").stat().st_ino)),
                  "branch": branch, "declared_repo_digest": _digest(declared_repo.encode("utf-8")), "entry_head": entry,
                  "synced_head": entry, "index": index, "preimages": {}, "outputs": {}, "artifacts": {}}
        if entry_name is not None:
            record.update(version=2, operation="remove" if source_name is None else "update" if entry_name in manifest.entries else "add",
                          entry_cache=entry_cache, selected_blob=None,
                          entry_blob_parent=[parent.st_dev, parent.st_ino] if parent else None, created_blob_parent=None)
            record.pop("force")
        elif rotate:
            record.update(version=3, operation="rotate", entry_cache=entry_cache,
                          rotation_blobs=[], receiving_identities={})
            record.pop("force")
        operation = AuthoringOperation(data_dir, clone_dir, declared_repo, record, (info.st_dev, info.st_ino))
        operation.mark("capturing")
        if index:
            _write(directory / "entry-index", (clone_dir / ".git/index").read_bytes())
            if operation._exact_receipts:operation._remember_artifact("entry-index")
        for name in ("manifest.json",) if entry_name is not None else _ROTATION_PATHS if rotate else _SEED_PATHS:
            operation.capture(name)
        if entry_name is not None:
            operation.record["selected_blob"] = _capture_entry_selection(operation, manifest, entry_name, source_name)
        elif rotate:
            operation.record["rotation_blobs"] = _capture_rotation_blobs(operation, manifest)
        operation.mark("admitted")
        if not created and entry is None:
            repo._fetch_unborn(clone_dir)
        elif not created:
            code, _ = repo._git(["fetch", "--quiet", "--prune"], cwd=clone_dir, timeout=repo.FETCH_TIMEOUT)
            if code != 0:raise SecretsError(f"seed fetch failed (status {code})")
            counts = repo._ahead_behind(clone_dir)
            if counts is None:raise SecretsError("seed refused: repository comparison is unknown")
            if counts[0]:
                state = "diverged" if counts[1] else "ahead of the remote"
                raise SecretsError(f"seed refused: local history is {state}; no local commit is treated as disposable")
            if counts[1]:
                target = repo._owned_oid(clone_dir, "@{u}")
                if entry_name is not None or rotate:
                    payload = repo._owned_query(clone_dir, ["show", target + ":manifest.json"])
                    incoming_manifest = Manifest(clone_dir / "manifest.json", json.loads(payload))
                if rotate:
                    operation.record["rotation_blobs"] = _capture_rotation_blobs(operation, incoming_manifest)
                elif entry_name is not None:
                    operation.record["selected_blob"] = _capture_entry_selection(operation, incoming_manifest, entry_name, source_name)
                    operation.record["operation"] = "remove" if source_name is None else "update" if entry_name in incoming_manifest.entries else "add"
                names = repo._owned_query(clone_dir, ["diff", "--name-only", "-z", entry, target]).split(b"\0")
                for raw in names:
                    if not raw:continue
                    name = raw.decode("utf-8", "strict")
                    if not _eligible(name):raise SecretsError("seed fast-forward would change unowned paths")
                    if _file_state(clone_dir / name) is not None and repo._owned_git(clone_dir, ["ls-files", "--error-unmatch", "--", name]).returncode != 0:
                        raise SecretsError("seed fast-forward would overwrite unowned ignored material")
                    operation.capture(name)
                incoming = {}
                for name in operation.record["preimages"]:
                    row = repo._owned_query(clone_dir, ["ls-tree", "-z", target, "--", name])
                    if not row:incoming[name] = None;continue
                    fields = row.split(b"\t", 1)[0].split()
                    if len(fields) != 3 or fields[0] not in (b"100644", b"100755") or fields[1] != b"blob":
                        raise SecretsError("seed fast-forward requires unsupported file slots")
                    payload = repo._owned_query(clone_dir, ["cat-file", "blob", fields[2].decode("ascii")])
                    incoming[name] = {"digest": _digest(payload), "mode": 0o644 if fields[0] == b"100644" else 0o755}
                operation.mark("syncing", synced_head=target, synced_files=incoming)
                code, _ = repo._git(["merge", "--ff-only", "--quiet", target], cwd=clone_dir, timeout=repo.FETCH_TIMEOUT)
                if code != 0:raise SecretsError(f"seed fast-forward failed (status {code})")
                if entry_name is not None and operation.record["entry_blob_parent"] is None and (clone_dir / "blobs").exists():
                    parent = _ordinary(clone_dir / "blobs", directory=True)
                    operation.record["created_blob_parent"] = [parent.st_dev, parent.st_ino]
        operation.record["synced_head"] = _head(clone_dir)
        _admit(clone_dir, declared_repo)
        guard.require_guard(clone_dir)
        _admit(clone_dir, declared_repo)
        if entry_name is not None or rotate:
            manifest = Manifest.load(clone_dir / "manifest.json")
            if rotate:
                if _rotation_blobs(manifest) != operation.record["rotation_blobs"]:
                    raise _recovery_error("synchronized rotation footprint changed after preimage capture")
                for name in operation.record["rotation_blobs"]:
                    if _file_state(clone_dir / name) is None:
                        raise SecretsError(f"rotation refused: the synchronized checkout has no {name}")
            elif _selected_entry_blob(manifest, entry_name, source_name) != operation.record["selected_blob"]:
                raise _recovery_error("synchronized entry selection changed after preimage capture")
            operation.record["synced_index"] = _file_state(clone_dir / ".git/index")
            operation._require_cache()
        operation.mark("synchronized", synced_files={name: _file_state(clone_dir / name) for name in operation.record["preimages"]})
        return operation
    except BaseException as primary:
        if "operation" in locals():operation.failure(primary)
        elif directory.exists():
            primary.authoring_recovery_error = _recovery_error("recovery directory setup incomplete")
            if hasattr(primary, "add_note"):primary.add_note(str(primary.authoring_recovery_error))
        raise


def _valid_file_receipt(state: Any, *, stored: bool = False) -> bool:
    if state is None:
        return not stored
    return (isinstance(state, dict) and set(state) == ({"digest", "mode", "stored"} if stored else {"digest", "mode"})
            and isinstance(state["digest"], str) and len(state["digest"]) == 64
            and all(c in "0123456789abcdef" for c in state["digest"])
            and type(state["mode"]) is int and 0 <= state["mode"] <= 0o7777
            and (not stored or isinstance(state["stored"], str)))


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _valid_physical_identity(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and all(type(n) is int and n >= 0 for n in value)


def _load_entry_operation(data_dir: Path, info: os.stat_result, marker: os.stat_result,
                          record: dict) -> AuthoringOperation:
    """Accept only the finite version-2 entry record, never weaken seed records."""
    required = {"version", "operation", "phase", "clone_identity", "git_identity", "branch", "declared_repo_digest",
                "entry_head", "synced_head", "index", "preimages", "outputs", "artifacts", "directory_identity",
                "marker_identity", "entry_cache", "selected_blob", "entry_blob_parent", "created_blob_parent"}
    optional = {"synced_files", "synced_index", "expected_tree", "authored_paths", "blob_receiving_identity",
                "commit_oid", "proof_ref", "publication_reason", "cleanup_outcome", "cleanup_remaining", "cleanup_next"}
    if not required.issubset(record) or set(record) - required - optional:
        raise ValueError
    if type(record["version"]) is not int or record["version"] != 2 or record["operation"] not in {"add", "update", "remove"}:
        raise ValueError
    if record["phase"] not in {"admitted", "syncing", "synchronized", "prepared", "applying", "publishing", "unsubmitted", "uncertain", "definitely_rejected", "confirmed", "finalizing", "restored", "unchanged", "cleaning"}:
        raise ValueError
    for key in ("clone_identity", "git_identity", "directory_identity", "marker_identity"):
        if not _valid_physical_identity(record[key]):raise ValueError
    if record["directory_identity"] != [info.st_dev, info.st_ino] or record["marker_identity"] != [marker.st_dev, marker.st_ino]:
        raise ValueError
    for key in ("entry_blob_parent", "created_blob_parent"):
        if record[key] is not None and not _valid_physical_identity(record[key]):raise ValueError
    if record["created_blob_parent"] is not None and record["entry_blob_parent"] is not None:raise ValueError
    for key in ("entry_head", "synced_head"):
        if not repo._object_id(record[key]):raise ValueError
    for key in ("index", "entry_cache"):
        if not _valid_file_receipt(record[key]):raise ValueError
    if "synced_index" in record and not _valid_file_receipt(record["synced_index"]):raise ValueError
    if "blob_receiving_identity" in record and not _valid_physical_identity(record["blob_receiving_identity"]):raise ValueError
    if "commit_oid" in record:
        if not repo._object_id(record["commit_oid"]) or record.get("proof_ref") != repo._publication_ref(record["commit_oid"]) or not repo._object_id(record.get("expected_tree")):raise ValueError
    elif "proof_ref" in record:raise ValueError
    if not _valid_digest(record["declared_repo_digest"]):raise ValueError
    selected = record["selected_blob"]
    if selected is not None and (not isinstance(selected, str) or not selected.startswith("blobs/") or not _eligible(selected) or "\\" in selected or selected != Path(selected).as_posix()):raise ValueError
    clone = data_dir / "repo"
    for path, key in [(clone, "clone_identity"), (clone / ".git", "git_identity")]:
        physical = _ordinary(path, directory=True)
        if record[key] != [physical.st_dev, physical.st_ino]:raise ValueError
    declared_repo = repo._recorded_origin(clone)
    if _digest(declared_repo.encode("utf-8")) != record["declared_repo_digest"]:raise ValueError
    if not isinstance(record["preimages"], dict) or "manifest.json" not in record["preimages"]:raise ValueError
    stored_names = set()
    for name, state in record["preimages"].items():
        if not isinstance(name, str) or not _eligible(name):raise ValueError
        if state is None:continue
        if not _valid_file_receipt(state, stored=True):raise ValueError
        stored = state["stored"]
        if not stored.startswith("preimage-") or not stored[9:].isdigit() or stored in stored_names:raise ValueError
        stored_names.add(stored)
    if selected is not None and selected not in record["preimages"]:raise ValueError
    if not isinstance(record["outputs"], dict) or not isinstance(record["artifacts"], dict):raise ValueError
    if record["outputs"]:
        if selected is None or set(record["outputs"]) != {selected, "manifest.json"}:raise ValueError
        for name, state in record["outputs"].items():
            if name == selected and record["operation"] == "remove":
                if state is not None:raise ValueError
            elif not _valid_file_receipt(state, stored=True) or state["stored"] != ("proposed-manifest" if name == "manifest.json" else "proposed-blob"):
                raise ValueError
    advanced = record["phase"] in {"prepared", "applying", "publishing", "unsubmitted", "uncertain", "definitely_rejected", "confirmed", "finalizing", "unchanged"}
    if advanced or record["phase"] == "cleaning" and record["cleanup_outcome"] in {"confirmed", "unchanged"}:
        if not record["outputs"] or record.get("authored_paths") != [selected, "manifest.json"] or not repo._object_id(record.get("expected_tree")):raise ValueError
    if "synced_files" in record:
        if not isinstance(record["synced_files"], dict) or set(record["synced_files"]) != set(record["preimages"]):raise ValueError
        if not all(_valid_file_receipt(state) for state in record["synced_files"].values()):raise ValueError
    synchronized = advanced or record["phase"] == "synchronized" or record["phase"] == "cleaning" and record["cleanup_outcome"] in {"confirmed", "unchanged"}
    if synchronized:
        if "synced_files" not in record or "synced_index" not in record or not _valid_file_receipt(record["synced_index"]):raise ValueError
    if "publication_reason" in record and record["publication_reason"] not in _PUBLICATION_REASONS:raise ValueError
    allowed = {"entry-index", "prepared-index", "proposed-manifest"} | stored_names
    if record["operation"] != "remove":allowed.add("proposed-blob")
    if set(record["artifacts"]) - allowed or any(not _valid_file_receipt(state) or state is None for state in record["artifacts"].values()):raise ValueError
    actual_names = {path.name for path in (data_dir / RECOVERY_DIRECTORY).iterdir()} - {"marker.json"}
    expected_names = set(record["artifacts"])
    if record["phase"] == "cleaning":
        remaining = record["cleanup_remaining"]
        if record["cleanup_outcome"] not in {"confirmed", "restored", "unchanged"} or not isinstance(remaining, list) or len(remaining) != len(set(remaining)) or set(remaining) != expected_names:raise ValueError
        next_name = record["cleanup_next"]
        if next_name is not None:
            if next_name not in expected_names:raise ValueError
            if next_name not in actual_names:expected_names.remove(next_name)
    if actual_names != expected_names:raise ValueError
    for name in actual_names:
        path = data_dir / RECOVERY_DIRECTORY / name
        _private(path)
        if _file_state(path) != record["artifacts"][name]:raise ValueError
    if "proposed-blob" in actual_names:
        physical = _private(data_dir / RECOVERY_DIRECTORY / "proposed-blob")
        if record.get("blob_receiving_identity") != [physical.st_dev, physical.st_ino]:raise ValueError
    if record["operation"] == "remove" and "blob_receiving_identity" in record:raise ValueError
    if _branch(clone) != record["branch"]:raise ValueError
    published = record["phase"] in {"publishing", "uncertain", "confirmed", "finalizing"} or record["phase"] == "cleaning" and record["cleanup_outcome"] == "confirmed"
    if published:
        commit = record["commit_oid"]
        if not repo._object_id(commit) or record["proof_ref"] != repo._publication_ref(commit) or _head(clone) != commit or repo._owned_oid(clone, "HEAD^{tree}") != record["expected_tree"]:raise ValueError
        _admit(clone, declared_repo)
        for name, state in record["outputs"].items():
            if _file_state(clone / name) != (None if state is None else {"digest": state["digest"], "mode": 0o644}):raise ValueError
    if record["phase"] == "unchanged" or record["phase"] == "cleaning" and record["cleanup_outcome"] == "unchanged":
        if _head(clone) != record["synced_head"] or _file_state(clone / ".git/index") != record["synced_index"]:raise ValueError
        _admit(clone, declared_repo)
        if any(_file_state(clone / name) != state for name, state in record["synced_files"].items()):raise ValueError
    repo.require_repo_binding(clone, declared_repo)
    return AuthoringOperation(data_dir, clone, declared_repo, record, (info.st_dev, info.st_ino), (marker.st_dev, marker.st_ino))


def _load_rotation_operation(data_dir: Path, info: os.stat_result, marker: os.stat_result,
                             record: dict) -> AuthoringOperation:
    """Accept only the finite version-3 rotation record, never weaken others."""
    required = {"version", "operation", "phase", "clone_identity", "git_identity", "branch", "declared_repo_digest",
                "entry_head", "synced_head", "index", "preimages", "outputs", "artifacts", "directory_identity",
                "marker_identity", "entry_cache", "rotation_blobs", "receiving_identities"}
    optional = {"synced_files", "synced_index", "expected_tree", "authored_paths", "cache_digest",
                "commit_oid", "proof_ref", "publication_reason", "cleanup_outcome", "cleanup_remaining", "cleanup_next"}
    if not required.issubset(record) or set(record) - required - optional:
        raise ValueError
    if type(record["version"]) is not int or record["version"] != 3 or record["operation"] != "rotate":
        raise ValueError
    if record["phase"] not in {"admitted", "syncing", "synchronized", "prepared", "applying", "publishing", "unsubmitted", "uncertain", "definitely_rejected", "confirmed", "finalizing", "restored", "cleaning"}:
        raise ValueError
    for key in ("clone_identity", "git_identity", "directory_identity", "marker_identity"):
        if not _valid_physical_identity(record[key]):raise ValueError
    if record["directory_identity"] != [info.st_dev, info.st_ino] or record["marker_identity"] != [marker.st_dev, marker.st_ino]:
        raise ValueError
    for key in ("entry_head", "synced_head"):
        if not repo._object_id(record[key]):raise ValueError
    for key in ("index", "entry_cache"):
        if not _valid_file_receipt(record[key]):raise ValueError
    if "synced_index" in record and not _valid_file_receipt(record["synced_index"]):raise ValueError
    if "cache_digest" in record and not _valid_digest(record["cache_digest"]):raise ValueError
    if "commit_oid" in record:
        if not repo._object_id(record["commit_oid"]) or record.get("proof_ref") != repo._publication_ref(record["commit_oid"]) or not repo._object_id(record.get("expected_tree")):raise ValueError
    elif "proof_ref" in record:raise ValueError
    if not _valid_digest(record["declared_repo_digest"]):raise ValueError
    blobs = record["rotation_blobs"]
    if not isinstance(blobs, list) or sorted(set(blobs)) != blobs:raise ValueError
    for name in blobs:
        if not isinstance(name, str) or not _canonical_blob(name):raise ValueError
    clone = data_dir / "repo"
    for path, key in [(clone, "clone_identity"), (clone / ".git", "git_identity")]:
        physical = _ordinary(path, directory=True)
        if record[key] != [physical.st_dev, physical.st_ino]:raise ValueError
    declared_repo = repo._recorded_origin(clone)
    if _digest(declared_repo.encode("utf-8")) != record["declared_repo_digest"]:raise ValueError
    if not isinstance(record["preimages"], dict) or not {"manifest.json", "identity.age"}.issubset(record["preimages"]):raise ValueError
    stored_names = set()
    for name, state in record["preimages"].items():
        if not isinstance(name, str) or not _eligible(name):raise ValueError
        if state is None:continue
        if not _valid_file_receipt(state, stored=True):raise ValueError
        stored = state["stored"]
        if not stored.startswith("preimage-") or not stored[9:].isdigit() or stored in stored_names:raise ValueError
        stored_names.add(stored)
    if any(name not in record["preimages"] for name in blobs):raise ValueError
    for key in ("outputs", "artifacts", "receiving_identities"):
        if not isinstance(record[key], dict):raise ValueError
    proposed = {"identity.age": "proposed-identity", "manifest.json": "proposed-manifest"}
    proposed.update({name: "proposed-blob-" + str(index) for index, name in enumerate(blobs)})
    if record["outputs"]:
        if set(record["outputs"]) != set(proposed):raise ValueError
        for name, state in record["outputs"].items():
            if not _valid_file_receipt(state, stored=True) or state["stored"] != proposed[name]:raise ValueError
    advanced = record["phase"] in {"prepared", "applying", "publishing", "unsubmitted", "uncertain", "definitely_rejected", "confirmed", "finalizing"}
    confirmed_cleaning = record["phase"] == "cleaning" and record["cleanup_outcome"] == "confirmed"
    if advanced or confirmed_cleaning:
        if not record["outputs"] or record.get("authored_paths") != ["identity.age", "manifest.json"] + blobs:raise ValueError
        if not repo._object_id(record.get("expected_tree")) or "cache_digest" not in record:raise ValueError
    if "synced_files" in record:
        if not isinstance(record["synced_files"], dict) or set(record["synced_files"]) != set(record["preimages"]):raise ValueError
        if not all(_valid_file_receipt(state) for state in record["synced_files"].values()):raise ValueError
    if advanced or confirmed_cleaning or record["phase"] == "synchronized":
        if "synced_files" not in record or "synced_index" not in record or not _valid_file_receipt(record["synced_index"]):raise ValueError
    if "publication_reason" in record and record["publication_reason"] not in _PUBLICATION_REASONS:raise ValueError
    allowed = {"entry-index", "prepared-index", "proposed-manifest"} | stored_names | _proposed_names(record)
    if set(record["artifacts"]) - allowed or any(not _valid_file_receipt(state) or state is None for state in record["artifacts"].values()):raise ValueError
    actual_names = {path.name for path in (data_dir / RECOVERY_DIRECTORY).iterdir()} - {"marker.json"}
    expected_names = set(record["artifacts"])
    if record["phase"] == "cleaning":
        remaining = record["cleanup_remaining"]
        if record["cleanup_outcome"] not in {"confirmed", "restored"} or not isinstance(remaining, list) or len(remaining) != len(set(remaining)) or set(remaining) != expected_names:raise ValueError
        next_name = record["cleanup_next"]
        if next_name is not None:
            if next_name not in expected_names:raise ValueError
            if next_name not in actual_names:expected_names.remove(next_name)
    if actual_names != expected_names:raise ValueError
    for name in actual_names:
        path = data_dir / RECOVERY_DIRECTORY / name
        _private(path)
        if _file_state(path) != record["artifacts"][name]:raise ValueError
    for stored, identity in record["receiving_identities"].items():
        if stored not in _proposed_names(record) or not _valid_physical_identity(identity):raise ValueError
        if stored in actual_names:
            physical = _private(data_dir / RECOVERY_DIRECTORY / stored)
            if identity != [physical.st_dev, physical.st_ino]:raise ValueError
    if _branch(clone) != record["branch"]:raise ValueError
    if record["phase"] in {"publishing", "uncertain", "confirmed", "finalizing"} or confirmed_cleaning:
        commit = record["commit_oid"]
        if not repo._object_id(commit) or record["proof_ref"] != repo._publication_ref(commit) or _head(clone) != commit or repo._owned_oid(clone, "HEAD^{tree}") != record["expected_tree"]:raise ValueError
        _admit(clone, declared_repo)
        for name, state in record["outputs"].items():
            if _file_state(clone / name) != {"digest": state["digest"], "mode": 0o644}:raise ValueError
    repo.require_repo_binding(clone, declared_repo)
    return AuthoringOperation(data_dir, clone, declared_repo, record, (info.st_dev, info.st_ino), (marker.st_dev, marker.st_ino))


def _load_operation(data_dir: Path) -> AuthoringOperation:
    directory = data_dir / RECOVERY_DIRECTORY
    info = _private(directory, directory=True)
    marker = _private(directory / "marker.json")
    try:
        record = json.loads((directory / "marker.json").read_bytes())
        if isinstance(record, dict) and record.get("version") == 2:
            return _load_entry_operation(data_dir, info, marker, record)
        if isinstance(record, dict) and record.get("version") == 3:
            return _load_rotation_operation(data_dir, info, marker, record)
        required = {"version", "operation", "phase", "clone_identity", "git_identity", "branch", "declared_repo_digest", "entry_head", "synced_head", "index", "preimages", "outputs", "artifacts", "directory_identity", "marker_identity"}
        if not isinstance(record, dict) or not required.issubset(record) or record["version"] != 1 or record["operation"] != "seed":
            raise ValueError
        if not isinstance(record["declared_repo_digest"], str):raise ValueError
        for key in ("entry_head", "synced_head"):
            if record[key] is not None and not repo._object_id(record[key]):raise ValueError
        if record["phase"] not in {"admitted", "syncing", "synchronized", "prepared", "applying", "publishing", "unsubmitted", "uncertain", "definitely_rejected", "confirmed", "finalizing", "restored", "cleaning"}:
            raise ValueError
        if record["directory_identity"] != [info.st_dev, info.st_ino] or record["marker_identity"] != [marker.st_dev, marker.st_ino]:raise ValueError
        clone = data_dir / "repo"
        for path, key in [(clone, "clone_identity"), (clone / ".git", "git_identity")]:
            physical = _ordinary(path, directory=True)
            if [physical.st_dev, physical.st_ino] != record[key]:raise ValueError
        declared_repo = repo._recorded_origin(clone)
        if _digest(declared_repo.encode("utf-8")) != record["declared_repo_digest"]:raise ValueError
        for name, state in record["preimages"].items():
            if not _eligible(name):raise ValueError
            if state and (not state["stored"].startswith("preimage-") or not state["stored"][9:].isdigit()):raise ValueError
        if any(name not in _SEED_PATHS for name in record["outputs"]):raise ValueError
        expected_outputs = {"identity.age": "proposed-identity", "manifest.json": "proposed-manifest", ".gitignore": "proposed-ignore"}
        for name, state in record["outputs"].items():
            if state["stored"] != expected_outputs[name]:raise ValueError
        actual_names = {path.name for path in directory.iterdir()} - {"marker.json"}
        expected_names = set(record["artifacts"])
        if record["phase"] == "cleaning":
            if record["cleanup_outcome"] not in ("confirmed", "restored") or set(record["cleanup_remaining"]) != expected_names or len(record["cleanup_remaining"]) != len(expected_names):raise ValueError
            next_name = record.get("cleanup_next")
            if next_name is not None:
                if next_name not in expected_names:raise ValueError
                if next_name not in actual_names:expected_names.remove(next_name)
        if actual_names != expected_names:raise ValueError
        for name, expected in record["artifacts"].items():
            if "/" in name or "\\" in name or name in (".", ".."):raise ValueError
            if name not in actual_names:continue
            _private(directory / name)
            if _file_state(directory / name) != expected:raise ValueError
        published = record["phase"] in {"publishing", "uncertain", "confirmed", "finalizing"} or record["phase"] == "cleaning" and record["cleanup_outcome"] == "confirmed"
        if published:
            if not repo._object_id(record["commit_oid"]) or "identity.age" not in record["outputs"]:raise ValueError
            if record["proof_ref"] != repo._publication_ref(record["commit_oid"]):raise ValueError
            if _head(clone) != record["commit_oid"]:raise ValueError
            if repo._owned_oid(clone, "HEAD^{tree}") != record["expected_tree"]:raise ValueError
            _admit(clone, declared_repo)
            for name, output in record["outputs"].items():
                if _file_state(clone / name) != {"digest": output["digest"], "mode": 0o644}:raise ValueError
        if _branch(clone) != record["branch"]:raise ValueError
    except AuthoringRecoveryError:
        raise
    except (ValueError, KeyError, TypeError, OSError, SecretsError, subprocess.TimeoutExpired):
        raise _recovery_error("recovery record is malformed or binding changed") from None
    try:repo.require_repo_binding(clone, declared_repo)
    except SecretsError:raise _recovery_error("recovery repository binding changed or is unavailable") from None
    return AuthoringOperation(data_dir, clone, declared_repo, record,
                         (info.st_dev, info.st_ino), (marker.st_dev, marker.st_ino))


def _inspect_recovery(data_dir: Path) -> dict:
    """Return finite private state without keys, paths, URLs or raw diagnostics."""
    from .operation_lock import _recovery_operation_lock
    with _recovery_operation_lock(data_dir) as canonical:
        operation = _load_operation(canonical)
        record = operation.record
        return {"phase": record["phase"], "operation": record["operation"], "binding": "matched",
                "path_integrity": "matched", "attempted_commit": record.get("commit_oid"),
                "publication_evidence": record.get("publication_reason", "not observed"),
                "publication_attempted": record["phase"] in ("publishing", "uncertain", "confirmed", "finalizing") or record.get("cleanup_outcome") == "confirmed",
                "cache_compatible": operation._cache_compatible()}


def _reconcile_recovery(data_dir: Path) -> dict:
    """Never prompt or overwrite the cache; unknown publication stays pending."""
    from .operation_lock import _recovery_operation_lock
    with _recovery_operation_lock(data_dir) as canonical:
        operation = _load_operation(canonical)
        record = operation.record
        operation._require_cache()
        if record["phase"] == "unchanged" or record["phase"] == "cleaning" and record["cleanup_outcome"] == "unchanged":
            operation._verify_synchronized()
            operation.cleanup()
            return {"outcome": "unchanged", "recovery": "cleared"}
        if record["phase"] == "cleaning" and record["cleanup_outcome"] == "restored":
            if _head(operation.clone) != record["entry_head"] or _file_state(operation.clone / ".git/index") != record["index"]:
                raise _recovery_error("restored baseline changed before cleanup")
            for name, preimage in record["preimages"].items():
                expected = None if preimage is None else {key: preimage[key] for key in ("digest", "mode")}
                if _file_state(operation.clone / name) != expected:raise _recovery_error("restored authoring path changed")
            operation.cleanup()
            return {"outcome": "unsubmitted or definitely rejected", "recovery": "cleared"}
        if record.get("proof_ref"):
            proof = record["proof_ref"]
            repo._require_direct_ref(operation.clone, proof)
            existing = repo._owned_git(operation.clone, ["rev-parse", "--verify", "--quiet", proof])
            if existing.returncode == 0:
                observed = existing.stdout.decode("ascii").rstrip("\n")
                if not repo._object_id(observed):raise _recovery_error("owned proof ref is malformed")
                repo._owned_query(operation.clone, ["update-ref", "--no-deref", "-d", proof, observed])
            elif existing.returncode != 1 or existing.stdout or existing.stderr:
                raise _recovery_error("owned proof cleanup unavailable")
        if record["phase"] in ("publishing", "uncertain"):
            evidence = repo._prove_publication(operation.clone, commit_oid=record["commit_oid"], target_ref=record["branch"], declared_repo=operation.declared_repo)
            if evidence.outcome != "confirmed":raise _recovery_error("fresh proof leaves publication uncertain")
            operation.mark("confirmed", publication_reason=evidence.reason)
        if operation.record["phase"] in ("confirmed", "finalizing", "cleaning"):
            # A confirmed publication whose cache was never finalized is
            # reported, never rewritten here: only unlock may replace it.
            operation._require_cache()
            operation.cleanup()
            return {"outcome": "confirmed", "recovery": "cleared"}
        operation.restore()
        return {"outcome": "unsubmitted or definitely rejected", "recovery": "cleared"}
