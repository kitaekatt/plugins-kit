"""The tracked-tree guard: never materialize plaintext into someone's repo.

The secrets repo's pre-commit hook protects the blobs. Nothing protected a
CONSUMER repo a ``--dest`` pointed into -- the convergence pass rewrote
plaintext there every session and a routine ``git add -A`` staged it. These
tests pin the three places that now stop it: the predicate, the add-time
refusal, and the convergence re-check.

Every plaintext here is a throwaway string. A test for this guard that used a
real credential would, when it failed, produce exactly the outcome the guard
exists to prevent.
"""

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sk_testlib import copy_git_tree

from secrets_kit import repo as repo_mod
from secrets_kit.converge import FAILURE_DEST, converge
from secrets_kit.manifest import Manifest
from secrets_kit.repo import (
    DEST_EXPOSED,
    DEST_IGNORED,
    DEST_NOT_IN_REPO,
    DEST_UNDETERMINED,
    dest_exposure,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")

_PLAINTEXT = b"throwaway-not-a-real-secret\n"

_CLI_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "secrets-kit"
    / "scripts"
    / "secrets_kit_cli.py"
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def _build_plain_repo(path: Path) -> None:
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")


# Populated once per process by the autouse `_templates` fixture below, so the
# ~30 `_init_repo` call sites stay unchanged. Read-only after that: every
# caller gets an independent `copy_git_tree` copy.
_TEMPLATES = {}


@pytest.fixture(scope="session", autouse=True)
def _templates(git_template):
    _TEMPLATES["plain"] = git_template("dest-guard-plain", _build_plain_repo)
    _TEMPLATES["adding"] = git_template("dest-guard-adding", _build_adding_tree)


def _init_repo(path: Path) -> Path:
    """A committable git repo -- a copy of the template, not a fresh init."""
    path.mkdir(parents=True, exist_ok=True)
    copy_git_tree(_TEMPLATES["plain"], path)
    return path


_QUERY_VERBS = ("rev-parse", "check-ignore")


def _no_git(monkeypatch):
    """Make the exposure queries look like git is not installed.

    Only the queries: the surrounding verbs (fetch, commit, push) are how the
    test drives the code under test, and a blanket stub would fail them first
    and never reach the guard.
    """
    real = repo_mod._git

    def fake(args, **kwargs):
        if args and args[0] in _QUERY_VERBS:
            return (127, "could not run git: [Errno 2]")
        return real(args, **kwargs)

    monkeypatch.setattr(repo_mod, "_git", fake)


# --------------------------------------------------------------------------
# The predicate
# --------------------------------------------------------------------------

class TestDestExposure:
    def test_a_path_in_no_repo_is_not_in_repo(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        result = dest_exposure(plain / "token.txt")
        assert result.status == DEST_NOT_IN_REPO
        assert result.exposed is False
        assert result.undetermined is False

    def test_a_gitignored_path_is_safe(self, tmp_path):
        repo = _init_repo(tmp_path / "consumer")
        (repo / "config").mkdir()
        (repo / ".gitignore").write_text("/config/token.txt\n", encoding="utf-8")
        assert dest_exposure(repo / "config" / "token.txt").status == DEST_IGNORED

    def test_a_non_ignored_path_is_exposed(self, tmp_path):
        repo = _init_repo(tmp_path / "consumer")
        (repo / "config").mkdir()
        result = dest_exposure(repo / "config" / "token.txt")
        assert result.status == DEST_EXPOSED
        assert result.exposed is True

    def test_a_tracked_file_is_exposed(self, tmp_path):
        """The worst case: the path is already in the index."""
        repo = _init_repo(tmp_path / "consumer")
        (repo / "token.txt").write_text("placeholder\n", encoding="utf-8")
        _git(repo, "add", "--", "token.txt")
        assert dest_exposure(repo / "token.txt").exposed is True

    def test_a_missing_parent_inside_a_repo_is_still_classified(self, tmp_path):
        """A consumer repo cloned but not yet built out must not read as safe.

        The dest file does not exist and neither does its directory; the
        predicate walks up to the repo root rather than giving up.
        """
        repo = _init_repo(tmp_path / "consumer")
        assert dest_exposure(repo / "build" / "gen" / "token.txt").exposed is True

    def test_git_failure_is_not_reported_as_not_in_a_repo(self, tmp_path, monkeypatch):
        """The load-bearing distinction: 'no repo' and 'could not ask' differ.

        Collapsing them would make a machine without git look permanently safe,
        which is how a guard fails open.
        """
        repo = _init_repo(tmp_path / "consumer")
        assert dest_exposure(repo / "token.txt").exposed is True

        _no_git(monkeypatch)
        result = dest_exposure(repo / "token.txt")
        assert result.status == DEST_UNDETERMINED
        assert result.undetermined is True
        assert result.exposed is False
        assert result.status != DEST_NOT_IN_REPO

    def test_a_timeout_is_also_undetermined(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path / "consumer")
        monkeypatch.setattr(repo_mod, "_git", lambda *a, **k: (124, "git ... timed out"))
        assert dest_exposure(repo / "token.txt").status == DEST_UNDETERMINED


def _stub_is_inside_work_tree(monkeypatch, code, output):
    """Answer only `rev-parse --is-inside-work-tree`; let real git do the rest.

    A blanket stub would also answer `check-ignore`, whose verdict comes from
    the exit code -- so exit 0 would read as "ignored" and mask what is being
    tested here.
    """
    real = repo_mod._git

    def fake(args, **kwargs):
        if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return (code, output)
        return real(args, **kwargs)

    monkeypatch.setattr(repo_mod, "_git", fake)


class TestOutputParsing:
    """`_git` merges stderr into stdout, so every answer can arrive with noise
    glued to it. None of those comparisons may resolve to a permissive state."""

    def test_a_warning_before_true_does_not_read_as_not_in_a_repo(
        self, tmp_path, monkeypatch
    ):
        """The fail-open case: a config warning must not read as 'no repo here'.

        Exit 0 with `warning: ...\\ntrue` is a real repository. Comparing the
        whole blob to "true" sent it to DEST_NOT_IN_REPO -- the permissive
        state -- for a dest that was in fact inside a working tree.

        Line-scanning does better than merely refusing to guess: the answer is
        still on its own line, so the true verdict is RECOVERED. Undetermined
        is reserved for output with no answer in it at all (below).
        """
        repo = _init_repo(tmp_path / "consumer")
        _stub_is_inside_work_tree(
            monkeypatch, 0, "warning: unable to access '/etc/gitconfig'\ntrue"
        )
        result = dest_exposure(repo / "token.txt")
        assert result.status != DEST_NOT_IN_REPO
        assert result.status == DEST_EXPOSED

    def test_unparseable_exit_zero_output_is_undetermined(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path / "consumer")
        _stub_is_inside_work_tree(monkeypatch, 0, "who knows")
        assert dest_exposure(repo / "token.txt").status == DEST_UNDETERMINED

    def test_empty_exit_zero_output_is_undetermined(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path / "consumer")
        _stub_is_inside_work_tree(monkeypatch, 0, "")
        assert dest_exposure(repo / "token.txt").status == DEST_UNDETERMINED

    def test_a_clean_false_is_still_not_in_a_repo(self, tmp_path, monkeypatch):
        """The bare-repo GIT_DIR case must keep working after the hardening."""
        repo = _init_repo(tmp_path / "consumer")
        _stub_is_inside_work_tree(monkeypatch, 0, "false")
        assert dest_exposure(repo / "token.txt").status == DEST_NOT_IN_REPO

    def test_a_warning_before_false_still_parses_as_not_in_a_repo(
        self, tmp_path, monkeypatch
    ):
        """Line-scanning recovers the answer; only an UNREADABLE one is undetermined."""
        repo = _init_repo(tmp_path / "consumer")
        _stub_is_inside_work_tree(monkeypatch, 0, "warning: noise\nfalse")
        assert dest_exposure(repo / "token.txt").status == DEST_NOT_IN_REPO

    def test_a_warning_glued_to_the_token_is_undetermined(self, tmp_path, monkeypatch):
        """No newline between the noise and the answer: nothing to parse."""
        repo = _init_repo(tmp_path / "consumer")
        _stub_is_inside_work_tree(monkeypatch, 0, "warning: x true")
        assert dest_exposure(repo / "token.txt").status == DEST_UNDETERMINED

    def test_a_warning_before_the_toplevel_does_not_yield_a_wrong_fix_line(
        self, tmp_path, monkeypatch
    ):
        """A bogus toplevel would name the WRONG file in remediation text.

        The verdict must stand; the fix line must be omitted rather than
        invented.
        """
        repo = _init_repo(tmp_path / "consumer")
        (repo / "config").mkdir()
        real = repo_mod._git

        def noisy(args, **k):
            code, output = real(args, **k)
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return (code, "warning: core.autocrlf\n" + output)
            return (code, output)

        monkeypatch.setattr(repo_mod, "_git", noisy)
        result = dest_exposure(repo / "config" / "token.txt")
        # Last non-empty line is the real answer, so this one still resolves.
        assert result.exposed is True
        assert result.gitignore_line == "/config/token.txt"

    def test_a_toplevel_that_is_not_an_ancestor_is_rejected(
        self, tmp_path, monkeypatch
    ):
        """Verification, not trust: a garbled path must not become a fix line."""
        repo = _init_repo(tmp_path / "consumer")
        (repo / "config").mkdir()
        real = repo_mod._git

        def liar(args, **k):
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return (0, str(tmp_path / "somewhere-else"))
            return real(args, **k)

        monkeypatch.setattr(repo_mod, "_git", liar)
        result = dest_exposure(repo / "config" / "token.txt")
        assert result.exposed is True
        # THIS is the assertion that distinguishes the fix. `gitignore_line is
        # None` alone would also pass against the old code, which computed a
        # relpath that escaped the tree and returned None for that reason --
        # do not "simplify" this test by dropping the toplevel check.
        assert result.toplevel is None
        assert result.gitignore_line is None

    def test_a_stale_git_dir_does_not_make_an_exposed_dest_look_safe(
        self, tmp_path, monkeypatch
    ):
        """git resolves a repo from the environment BEFORE the cwd.

        An inherited GIT_DIR pointing somewhere else makes `rev-parse` exit 128
        with "fatal: not a git repository", which the not-a-repo classifier
        would read as the permissive verdict for a dest that is very much
        inside a working tree.
        """
        repo = _init_repo(tmp_path / "consumer")
        (repo / "config").mkdir()
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere.git"))

        result = dest_exposure(repo / "config" / "token.txt")

        assert result.status != DEST_NOT_IN_REPO
        assert result.exposed is True
        assert result.gitignore_line == "/config/token.txt"

    def test_a_stale_git_work_tree_does_not_redirect_the_answer(
        self, tmp_path, monkeypatch
    ):
        repo = _init_repo(tmp_path / "consumer")
        (repo / "config").mkdir()
        (repo / ".gitignore").write_text("/config/token.txt\n", encoding="utf-8")
        monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))

        assert dest_exposure(repo / "config" / "token.txt").status == DEST_IGNORED


class TestUndeterminedCause:
    """Both causes fail open, but they are not the same event and must not read
    the same. Notes reach ctx.log, which is always shown."""

    def test_git_missing_is_reported_as_systemic(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path / "consumer")
        _stub_is_inside_work_tree(monkeypatch, 127, "could not run git: [Errno 2]")

        result = dest_exposure(repo / "token.txt")

        assert result.status == DEST_UNDETERMINED
        assert result.cause == repo_mod.DEST_UNDETERMINED_UNAVAILABLE
        assert result.anomalous is False

    def test_a_timeout_is_systemic_too(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path / "consumer")
        _stub_is_inside_work_tree(monkeypatch, 124, "git ... timed out")
        assert dest_exposure(repo / "token.txt").anomalous is False

    def test_unreadable_output_is_an_anomaly(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path / "consumer")
        _stub_is_inside_work_tree(monkeypatch, 0, "who knows")

        result = dest_exposure(repo / "token.txt")

        assert result.anomalous is True
        assert result.cause == repo_mod.DEST_UNDETERMINED_ANOMALY
        # The raw output is the only diagnostic that will ever exist for this.
        assert "who knows" in result.detail

    def test_an_unexpected_failure_is_an_anomaly(self, tmp_path, monkeypatch):
        """Exit 128 for a reason that is not 'no repo here' is alarming."""
        repo = _init_repo(tmp_path / "consumer")
        _stub_is_inside_work_tree(monkeypatch, 128, "fatal: detected dubious ownership")
        assert dest_exposure(repo / "token.txt").anomalous is True


class TestGitignoreLine:
    def test_is_repo_root_relative_and_anchored(self, tmp_path):
        repo = _init_repo(tmp_path / "consumer")
        (repo / "config").mkdir()
        result = dest_exposure(repo / "config" / "ha-token.txt")
        assert result.gitignore_line == "/config/ha-token.txt"

    def test_is_relative_to_the_repo_root_not_the_dest_directory(self, tmp_path):
        """A nested dest must not produce a line that ignores the wrong file."""
        repo = _init_repo(tmp_path / "consumer")
        (repo / "a" / "b").mkdir(parents=True)
        assert (
            dest_exposure(repo / "a" / "b" / "t.txt").gitignore_line == "/a/b/t.txt"
        )

    def test_actually_makes_the_dest_ignored(self, tmp_path):
        """The remediation has to WORK, not merely look plausible."""
        repo = _init_repo(tmp_path / "consumer")
        (repo / "config").mkdir()
        dest = repo / "config" / "ha-token.txt"
        line = dest_exposure(dest).gitignore_line

        (repo / ".gitignore").write_text(line + "\n", encoding="utf-8")
        assert dest_exposure(dest).status == DEST_IGNORED

    def test_uses_forward_slashes_on_every_platform(self, tmp_path):
        repo = _init_repo(tmp_path / "consumer")
        (repo / "a").mkdir()
        assert "\\" not in dest_exposure(repo / "a" / "t.txt").gitignore_line


# --------------------------------------------------------------------------
# add-time refusal
# --------------------------------------------------------------------------

def _armored(recipient: bytes, plaintext: bytes) -> bytes:
    """Blob text the secrets repo's own pre-commit guard will accept."""
    return (
        b"-----BEGIN AGE ENCRYPTED FILE-----\n"
        + recipient
        + b"\n"
        + plaintext
        + b"-----END AGE ENCRYPTED FILE-----\n"
    )


def _build_adding_tree(root: Path) -> None:
    """The expensive half of `adding`: a bare remote, a seeded+pushed clone,
    and a consumer working tree. Ten git processes, built once per process.

    Nothing machine- or test-specific is baked in; the only absolute paths are
    the clone's origin URL, which `copy_git_tree` repoints.
    """
    remote = root / "remote.git"
    _git(root, "init", "--quiet", "--bare", "--initial-branch=main", str(remote))

    data_dir = root / "data"
    clone = data_dir / "repo"
    data_dir.mkdir()
    _git(root, "clone", "--quiet", str(remote), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    (clone / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "recipient": "age1testrecipient",
                "profiles": {},
                "entries": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _git(clone, "add", "--", "manifest.json")
    _git(clone, "commit", "--quiet", "-m", "seed")
    _git(clone, "push", "--quiet", "origin", "main")

    consumer = _init_repo(root / "consumer")
    (consumer / "config").mkdir()
    # A second in-tree directory, so a --dest CHANGE can be exercised against a
    # path convergence could actually write to.
    (consumer / "other").mkdir()
    (root / "plain").mkdir()
    (root / "source.txt").write_bytes(_PLAINTEXT)


@pytest.fixture
def adding(tmp_path, monkeypatch):
    """A seeded secrets repo with a real remote, plus dest trees to aim at.

    Neither `fleet` nor the `repo` fixture in test_guard.py fits: this needs a
    pushable secrets repo AND a real git working tree standing in for the
    consumer. `no_network` deliberately stubs git out, so it cannot be reused.

    The tree itself is a private copy of a per-process template (see
    `sk_testlib`); the copy is a real git repo and the test owns it outright.
    """
    tree = copy_git_tree(_TEMPLATES["adding"], tmp_path / "adding")
    remote = tree / "remote.git"
    data_dir = tree / "data"
    clone = data_dir / "repo"
    consumer = tree / "consumer"
    plain = tree / "plain"
    source = tree / "source.txt"

    config_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps(
            {
                "repo": str(remote),
                "vars": {"CONSUMER": str(consumer), "PLAIN": str(plain)},
                "machines": {"testbox": {"profiles": []}},
            }
        ),
        encoding="utf-8",
    )

    spec = importlib.util.spec_from_file_location("secrets_kit_cli", _CLI_PATH)
    cli = importlib.util.module_from_spec(spec)
    sys.modules["secrets_kit_cli"] = cli
    spec.loader.exec_module(cli)

    monkeypatch.setattr(cli, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cli, "DATA_DIR", data_dir)
    monkeypatch.setattr("secrets_kit.manifest.resolve_host", lambda: ["testbox"])

    from secrets_kit import agefile

    def encrypt_to_recipient(recipient, plaintext, out_path):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(_armored(recipient.encode(), plaintext))

    monkeypatch.setattr(agefile, "encrypt_to_recipient", encrypt_to_recipient)

    class Adding:
        pass

    a = Adding()
    a.cli = cli
    a.clone = clone
    a.consumer = consumer
    a.plain = plain
    a.source = str(source)
    a.data_dir = data_dir
    a.config_path = config_path
    a.manifest_path = clone / "manifest.json"

    def args(**overrides):
        base = dict(
            command="add",
            name="ha-token",
            file=str(source),
            dest=None,
            mode="0600",
            newline=None,
            doc=None,
            profile=None,
            update=False,
            allow_tracked_dest=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    a.args = args

    def entry(name="ha-token"):
        return Manifest.load(a.manifest_path).entries[name]

    a.entry = entry
    return a


class TestAddRefusal:
    def test_refuses_a_non_ignored_dest_inside_a_working_tree(self, adding, capsys):
        code = adding.cli.cmd_add(adding.args(dest="${CONSUMER}/config/ha-token.txt"))
        assert code == 1

        err = capsys.readouterr().err
        assert (adding.consumer / "config" / "ha-token.txt").as_posix() in err
        assert "/config/ha-token.txt" in err
        assert "--allow-tracked-dest" in err

        # Nothing was recorded and nothing was encrypted: refusing after the
        # blob landed would leave ciphertext for a value we declined.
        assert adding.manifest_path.read_text().count("ha-token") == 0
        assert not (adding.clone / "blobs").exists()

    def test_accepts_a_gitignored_dest(self, adding):
        (adding.consumer / ".gitignore").write_text(
            "/config/ha-token.txt\n", encoding="utf-8"
        )
        assert adding.cli.cmd_add(
            adding.args(dest="${CONSUMER}/config/ha-token.txt")
        ) == 0
        assert adding.entry().allow_tracked_dest is False

    def test_accepts_a_dest_outside_any_repo(self, adding):
        assert adding.cli.cmd_add(adding.args(dest="${PLAIN}/ha-token.txt")) == 0
        assert "ha-token" in adding.manifest_path.read_text()

    def test_the_override_is_persisted_into_the_manifest(self, adding):
        """An add-time-only flag would leave convergence refusing forever."""
        assert adding.cli.cmd_add(
            adding.args(
                dest="${CONSUMER}/config/ha-token.txt", allow_tracked_dest=True
            )
        ) == 0
        assert adding.entry().allow_tracked_dest is True
        assert json.loads(adding.manifest_path.read_text())["entries"]["ha-token"][
            "allow_tracked_dest"
        ] is True

    def test_an_update_keeps_a_previously_granted_override(self, adding):
        adding.cli.cmd_add(
            adding.args(
                dest="${CONSUMER}/config/ha-token.txt", allow_tracked_dest=True
            )
        )
        assert adding.cli.cmd_add(adding.args(update=True)) == 0
        assert adding.entry().allow_tracked_dest is True

    def test_an_unrelated_update_does_not_clear_the_stored_override(self, adding):
        """Touching neither dest nor flag must leave the consent intact."""
        adding.cli.cmd_add(
            adding.args(
                dest="${CONSUMER}/config/ha-token.txt", allow_tracked_dest=True
            )
        )
        assert adding.cli.cmd_add(adding.args(update=True, doc="see inventory")) == 0
        assert adding.entry().allow_tracked_dest is True
        assert adding.entry().doc == "see inventory"

    def test_re_supplying_the_identical_dest_keeps_the_override(self, adding):
        """The destination did not change, so the consent still applies to it."""
        adding.cli.cmd_add(
            adding.args(
                dest="${CONSUMER}/config/ha-token.txt", allow_tracked_dest=True
            )
        )
        assert adding.cli.cmd_add(
            adding.args(update=True, dest="${CONSUMER}/config/ha-token.txt")
        ) == 0
        assert adding.entry().allow_tracked_dest is True


_ARMOR_END = b"-----END AGE ENCRYPTED FILE-----\n"


def _prepare_convergence(adding, monkeypatch):
    """Make the `adding` fixture's repo convergeable: unlocked, with a profile.

    The fixture is built for authoring, so it has no identity and no profile
    assignment. Both are needed to assert what the PASS does with what `add`
    recorded -- which is the only way to test the two layers together.
    """
    from secrets_kit import converge as converge_mod

    def decrypt(identity_path, blob_path):
        body = Path(blob_path).read_bytes().split(b"\n", 2)[2]
        return body[: -len(_ARMOR_END)]

    monkeypatch.setattr(converge_mod, "decrypt_with_identity", decrypt)

    (adding.data_dir / "identity.txt").write_text("stub", encoding="utf-8")

    manifest = json.loads(adding.manifest_path.read_text())
    manifest["profiles"] = {"home": ["ha-token"]}
    adding.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    config = json.loads(adding.config_path.read_text())
    config["machines"]["testbox"]["profiles"] = ["home"]
    adding.config_path.write_text(json.dumps(config), encoding="utf-8")


class TestConsentDoesNotTransfer:
    """Consent is per-destination. Inheriting it across a --dest change would
    let a rotation relocate a credential into a different unignored tree with
    no check at add time AND none at convergence."""

    def _granted(self, adding):
        adding.cli.cmd_add(
            adding.args(
                dest="${CONSUMER}/config/ha-token.txt", allow_tracked_dest=True
            )
        )
        assert adding.entry().allow_tracked_dest is True

    def test_an_update_to_a_new_exposed_dest_is_refused(self, adding, capsys):
        self._granted(adding)
        capsys.readouterr()

        code = adding.cli.cmd_add(
            adding.args(update=True, dest="${CONSUMER}/other/ha-token.txt")
        )
        assert code == 1

        err = capsys.readouterr().err
        assert "/other/ha-token.txt" in err
        assert "does not transfer" in err
        assert "per-destination" in err

        # The entry is untouched: still the old dest, still consented.
        assert adding.entry().dest_spec == "${CONSUMER}/config/ha-token.txt"
        assert adding.entry().allow_tracked_dest is True

    def test_the_new_dest_is_accepted_when_consent_is_granted_again(self, adding):
        self._granted(adding)
        assert adding.cli.cmd_add(
            adding.args(
                update=True,
                dest="${CONSUMER}/other/ha-token.txt",
                allow_tracked_dest=True,
            )
        ) == 0
        assert adding.entry().dest_spec == "${CONSUMER}/other/ha-token.txt"
        assert adding.entry().allow_tracked_dest is True

    def test_moving_to_a_safe_dest_drops_the_override_rather_than_carrying_it(
        self, adding
    ):
        """Consent for the old path must not linger on a path that never needed it."""
        self._granted(adding)
        assert adding.cli.cmd_add(
            adding.args(update=True, dest="${PLAIN}/ha-token.txt")
        ) == 0
        assert adding.entry().allow_tracked_dest is False
        assert "allow_tracked_dest" not in json.loads(
            adding.manifest_path.read_text()
        )["entries"]["ha-token"]

    def test_convergence_never_writes_to_the_transferred_dest(
        self, adding, monkeypatch
    ):
        """End to end: the refusal is what stops convergence honouring it.

        The persisted flag is the only thing the pass reads, so an add that
        wrote it for an unchecked destination would defeat BOTH layers at once
        -- no check at add time and none at convergence. Driven through a real
        `converge` rather than asserting on the manifest, because "the pass
        would have honoured it" is the claim under test.
        """
        self._granted(adding)
        adding.cli.cmd_add(
            adding.args(update=True, dest="${CONSUMER}/other/ha-token.txt")
        )

        _prepare_convergence(adding, monkeypatch)
        result = converge(adding.config_path, adding.data_dir)

        transferred = adding.consumer / "other" / "ha-token.txt"
        assert not transferred.exists()
        assert result.failures == []
        # The consented original still converges: only the transfer was stopped.
        assert (adding.consumer / "config" / "ha-token.txt").read_bytes() == _PLAINTEXT

    def test_convergence_honours_a_properly_re_granted_dest(self, adding, monkeypatch):
        """The positive control: re-consenting really does move the secret."""
        self._granted(adding)
        adding.cli.cmd_add(
            adding.args(
                update=True,
                dest="${CONSUMER}/other/ha-token.txt",
                allow_tracked_dest=True,
            )
        )

        _prepare_convergence(adding, monkeypatch)
        converge(adding.config_path, adding.data_dir)

        assert (adding.consumer / "other" / "ha-token.txt").read_bytes() == _PLAINTEXT

class TestAddWhenTheCheckCannotRun:
    """Cannot-determine is not unsafe: authoring proceeds with a note, and the
    convergence pass re-checks on the machine where the path matters."""

    def test_proceeds_with_a_note_when_git_is_unavailable(
        self, adding, monkeypatch, capsys
    ):
        """A machine without git must still be able to author secrets."""
        _no_git(monkeypatch)
        assert adding.cli.cmd_add(adding.args(dest="${PLAIN}/ha-token.txt")) == 0

        err = capsys.readouterr().err
        assert "git is unavailable" in err
        assert "ANOMALY" not in err

    def test_an_unreadable_git_answer_is_flagged_as_an_anomaly(
        self, adding, monkeypatch, capsys
    ):
        _stub_is_inside_work_tree(monkeypatch, 0, "unexpected git output")
        assert adding.cli.cmd_add(adding.args(dest="${PLAIN}/ha-token.txt")) == 0

        err = capsys.readouterr().err
        assert "ANOMALY" in err
        assert "unexpected git output" in err

    def test_a_dest_that_resolves_only_elsewhere_is_still_addable(
        self, adding, capsys
    ):
        """A fleet dest may name a variable only the TARGET machine declares.

        Blocking the add would make the guard decide what the fleet may
        contain from whichever machine the author happens to be sitting at.
        The check is skipped with a note and re-runs at convergence, where the
        path actually matters.
        """
        code = adding.cli.cmd_add(adding.args(dest="${ONLY_ON_THE_MAC}/x.txt"))
        assert code == 0

        err = capsys.readouterr().err
        assert "does not resolve on this machine" in err
        assert "re-checked at convergence" in err
        assert adding.entry().dest_spec == "${ONLY_ON_THE_MAC}/x.txt"

    def test_a_per_os_dest_spec_unresolvable_here_is_still_addable(self, adding):
        """The dest-object form takes the same route as a plain string."""
        adding.cli.cmd_add(adding.args(dest="${PLAIN}/x.txt"))

        data = json.loads(adding.manifest_path.read_text())
        data["entries"]["ha-token"]["dest"] = {
            "default": "${ONLY_ON_THE_MAC}/x.txt",
            "windows": "${ONLY_ON_THIS_BOX}/x.txt",
        }
        adding.manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        assert adding.cli.cmd_add(adding.args(update=True)) == 0

    def test_an_update_reusing_an_unresolvable_stored_spec_is_not_blocked(
        self, adding, capsys
    ):
        adding.cli.cmd_add(adding.args(dest="${ONLY_ON_THE_MAC}/x.txt"))
        capsys.readouterr()

        assert adding.cli.cmd_add(adding.args(update=True)) == 0
        assert "does not resolve on this machine" in capsys.readouterr().err


# --------------------------------------------------------------------------
# convergence re-check
# --------------------------------------------------------------------------

def _make_dest_tree_a_repo(fleet, ignored: bool):
    """Turn the fleet's destination directory into a real consumer repo."""
    repo = _init_repo(fleet.tmp / "bank")
    if ignored:
        (repo / ".gitignore").write_text("/secrets/\n", encoding="utf-8")
    return repo


def _rewrite_manifest(fleet, **entry_overrides):
    data = json.loads(fleet.manifest_path.read_text())
    data["entries"]["ha-token"].update(entry_overrides)
    fleet.manifest_path.write_text(json.dumps(data), encoding="utf-8")


class TestConvergeRecheck:
    def test_withholds_the_write_for_an_exposed_dest(self, fleet):
        _make_dest_tree_a_repo(fleet, ignored=False)
        fleet.unlock()

        result = converge(fleet.config_path, fleet.data_dir)

        assert result.written == 0
        assert not (fleet.dest_root / "ha-token.txt").exists()
        assert [f.key for f in result.failures] == [FAILURE_DEST]

        failure = result.failures[0]
        assert failure.ask_reason == "info"
        assert "/secrets/ha-token.txt" in failure.user_msg
        # Paths render posix, like every other command this package prints.
        assert (fleet.dest_root / "ha-token.txt").as_posix() in failure.user_msg
        assert "\\" not in failure.user_msg
        assert "--allow-tracked-dest" in failure.agent_msg

    def test_the_about_to_write_message_does_not_claim_the_file_is_there(self, fleet):
        """Nothing is on disk yet, so the message must say 'did NOT write'."""
        _make_dest_tree_a_repo(fleet, ignored=False)
        fleet.unlock()

        failure = converge(fleet.config_path, fleet.data_dir).failures[0]

        assert "did NOT write" in failure.user_msg
        assert "already materialized" not in failure.user_msg
        assert "WITHHELD" in failure.agent_msg
        assert "rm --cached" not in failure.agent_msg

    def test_other_entries_still_converge(self, fleet):
        """One withheld entry must not take the rest of the pass down."""
        _make_dest_tree_a_repo(fleet, ignored=False)
        (fleet.tmp / "bank" / ".gitignore").write_text(
            "/secrets/rolfing.txt\n", encoding="utf-8"
        )
        fleet.unlock()

        import json as _json

        config = _json.loads(fleet.config_path.read_text())
        config["machines"]["testbox"]["profiles"] = ["home-admin", "rolfing"]
        fleet.config_path.write_text(_json.dumps(config), encoding="utf-8")

        result = converge(fleet.config_path, fleet.data_dir)

        assert result.written == 1
        assert (fleet.dest_root / "rolfing.txt").exists()
        assert not (fleet.dest_root / "ha-token.txt").exists()
        assert [f.key for f in result.failures] == [FAILURE_DEST]

    def test_writes_normally_when_the_dest_is_ignored(self, fleet):
        _make_dest_tree_a_repo(fleet, ignored=True)
        fleet.unlock()

        result = converge(fleet.config_path, fleet.data_dir)

        assert result.failures == []
        assert (fleet.dest_root / "ha-token.txt").read_bytes() == b"token-value\n"

    def test_writes_when_the_entry_carries_the_override(self, fleet):
        _make_dest_tree_a_repo(fleet, ignored=False)
        _rewrite_manifest(fleet, allow_tracked_dest=True)
        fleet.unlock()

        result = converge(fleet.config_path, fleet.data_dir)

        assert result.failures == []
        assert (fleet.dest_root / "ha-token.txt").read_bytes() == b"token-value\n"

    def test_degrades_to_a_note_and_writes_when_git_is_unavailable(
        self, fleet, monkeypatch
    ):
        """A machine that only pulls secrets may have no git installed."""
        _make_dest_tree_a_repo(fleet, ignored=False)
        fleet.unlock()
        _no_git(monkeypatch)

        result = converge(fleet.config_path, fleet.data_dir)

        assert result.failures == []
        assert (fleet.dest_root / "ha-token.txt").exists()
        note = "\n".join(result.notes)
        assert "git is unavailable" in note
        # Systemic and expected -- it must not be dressed up as an incident.
        assert "ANOMALY" not in note

    def test_an_unreadable_git_answer_is_reported_as_an_anomaly(
        self, fleet, monkeypatch
    ):
        """Still fail-open, but this one must not look routine.

        Notes reach ctx.log, the always-shown channel, so naming it is enough
        to make it visible without inventing a severity tier.
        """
        _make_dest_tree_a_repo(fleet, ignored=False)
        fleet.unlock()
        _stub_is_inside_work_tree(monkeypatch, 0, "unexpected git output")

        result = converge(fleet.config_path, fleet.data_dir)

        assert result.failures == []
        assert (fleet.dest_root / "ha-token.txt").exists()
        note = "\n".join(result.notes)
        assert "ANOMALY" in note
        assert "ha-token" in note
        assert "unexpected git output" in note

    def test_no_repo_root_yields_prose_not_a_broken_command(self, fleet, monkeypatch):
        """A fake `git -C the enclosing repository ...` is worse than no command."""
        _make_dest_tree_a_repo(fleet, ignored=False)
        fleet.unlock()
        real = repo_mod._git

        def liar(args, **kwargs):
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return (0, str(fleet.tmp / "somewhere-else"))
            return real(args, **kwargs)

        monkeypatch.setattr(repo_mod, "_git", liar)

        failure = converge(fleet.config_path, fleet.data_dir).failures[0]

        for message in (failure.user_msg, failure.agent_msg):
            assert "the enclosing repository" not in message
            assert "git -C" not in message
            assert "root could not be determined" in message
            assert "Find the repository that contains that path" in message

    def test_no_repo_root_on_the_already_written_path_also_avoids_a_command(
        self, fleet, monkeypatch
    ):
        repo = _make_dest_tree_a_repo(fleet, ignored=True)
        fleet.unlock()
        converge(fleet.config_path, fleet.data_dir)
        (repo / ".gitignore").write_text("/unrelated\n", encoding="utf-8")

        real = repo_mod._git

        def liar(args, **kwargs):
            if args[:2] == ["rev-parse", "--show-toplevel"]:
                return (0, str(fleet.tmp / "somewhere-else"))
            return real(args, **kwargs)

        monkeypatch.setattr(repo_mod, "_git", liar)

        failure = converge(fleet.config_path, fleet.data_dir).failures[0]

        assert "git -C" not in failure.user_msg
        assert "git -C" not in failure.agent_msg
        assert "untrack it as well" in failure.user_msg
        assert "already materialized" in failure.user_msg

    def test_a_later_gitignore_change_flips_a_previously_fine_entry(self, fleet):
        """The case no add-time check can cover, and the reason the check sits
        above the unchanged fast path.

        The entry was authored against an ignored destination and converged
        cleanly. Someone then rewrites the consumer's .gitignore. NOTHING about
        the entry changes -- same blob, same file on disk -- so a check that
        only ran when about to write would report `ok` forever while the
        plaintext sat in a tracked tree.
        """
        repo = _make_dest_tree_a_repo(fleet, ignored=True)
        fleet.unlock()
        assert converge(fleet.config_path, fleet.data_dir).written == 1

        (repo / ".gitignore").write_text("/unrelated\n", encoding="utf-8")

        result = converge(fleet.config_path, fleet.data_dir)

        assert [f.key for f in result.failures] == [FAILURE_DEST]
        assert result.ok == 0
        assert result.written == 0

    def test_an_already_exposed_entry_is_neither_rewritten_nor_deleted(self, fleet):
        """Reporting is the remedy; removing the file would be a second unasked act."""
        repo = _make_dest_tree_a_repo(fleet, ignored=True)
        fleet.unlock()
        converge(fleet.config_path, fleet.data_dir)
        (repo / ".gitignore").write_text("/unrelated\n", encoding="utf-8")

        converge(fleet.config_path, fleet.data_dir)

        assert (fleet.dest_root / "ha-token.txt").read_bytes() == b"token-value\n"

    def test_the_already_written_message_names_the_index_removal(self, fleet):
        """'The write was withheld' would be a lie when the file is already there."""
        repo = _make_dest_tree_a_repo(fleet, ignored=True)
        fleet.unlock()
        converge(fleet.config_path, fleet.data_dir)
        (repo / ".gitignore").write_text("/unrelated\n", encoding="utf-8")

        failure = converge(fleet.config_path, fleet.data_dir).failures[0]

        assert "already materialized" in failure.user_msg
        assert "did NOT write" not in failure.user_msg
        assert "rm --cached -- secrets/ha-token.txt" in failure.user_msg
        assert "WITHHELD" not in failure.agent_msg
        assert "rotate the underlying credential" in failure.agent_msg

    def test_a_pending_rotation_onto_a_now_exposed_dest_is_withheld(self, fleet):
        """The other half: the value changed AND the tree became tracked."""
        repo = _make_dest_tree_a_repo(fleet, ignored=True)
        fleet.unlock()
        converge(fleet.config_path, fleet.data_dir)
        (repo / ".gitignore").write_text("/unrelated\n", encoding="utf-8")

        from secrets_kit import agefile

        agefile.encrypt_to_recipient(
            fleet.recipient, b"rotated-value\n", fleet.blobs / "ha-token.txt.age"
        )

        result = converge(fleet.config_path, fleet.data_dir)

        assert result.written == 0
        assert [f.key for f in result.failures] == [FAILURE_DEST]
        # The rotated value is not leaked into the tracked tree.
        assert (fleet.dest_root / "ha-token.txt").read_bytes() == b"token-value\n"

    def test_an_exposed_entry_reports_every_pass_until_it_is_fixed(self, fleet):
        """The engine dedupes on the key and re-reports; going quiet would be the bug."""
        repo = _make_dest_tree_a_repo(fleet, ignored=False)
        fleet.unlock()

        for _ in range(3):
            result = converge(fleet.config_path, fleet.data_dir)
            assert [f.key for f in result.failures] == [FAILURE_DEST]

        (repo / ".gitignore").write_text("/secrets/\n", encoding="utf-8")
        result = converge(fleet.config_path, fleet.data_dir)
        assert result.failures == []
        assert (fleet.dest_root / "ha-token.txt").exists()
