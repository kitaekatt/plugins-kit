"""The /direct hooks' Windows access-control policy (hooks/winacl.py, direct_relay.py on nt).

The hooks run under the bootstrap standalone interpreter, which carries no llm_kit package, so
hooks/winacl.py is a byte copy of katsoft-libs ``llm_kit/relay/winacl.py`` (the same pattern as the vendored
channel schemas, see test_relay_channel_schema_sync.py) and direct_relay.load_winacl supplies the one llm_kit
name the copy imports. On POSIX the hooks keep the mode and owner checks; on nt they ask winacl instead (an
owner-plus-SYSTEM protected DACL, judged on the open handle). These tests fake nt; the Win32 calls themselves
are exercised by the library's native tests on a Windows host.

Edits that turn each test red (each shown red when this file was written):

* byte copy -- change one character in hooks/winacl.py (or delete the file): test_winacl_vendored_bytes_equal_source.
* token read on nt -- in direct_relay.read_token delete the ``_read_validated_nt`` branch (back to the mode check):
  test_hook_token_read_nt_policy goes red.
* enablement record on nt -- in direct_relay.enablement and direct_relay.enable delete the ``_on_nt()`` branches (or
  delete the ``refuse_writable_ancestors`` call in _write_private_nt):
  test_enablement_record_nt_policy goes red.
* loading the copy -- drop the ``llm_kit.relay.config`` stand-in in direct_relay.load_winacl:
  test_load_winacl_runs_without_llm_kit goes red.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / "plugins" / "llm-kit" / "hooks"
VENDORED = HOOKS / "winacl.py"
SOURCE_SUBPATH = Path("packages") / "katsoft-llm-kit" / "src" / "llm_kit" / "relay" / "winacl.py"

SESSION = "b91e0c44-2f8a-4d3b-8c6e-71a5d0e3f2a9"
TOKEN = "tok-" + "a" * 40


def _load_hook_helpers():
    spec = importlib.util.spec_from_file_location("direct_relay_winacl_under_test", HOOKS / "direct_relay.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def R():
    return _load_hook_helpers()


def _source_file() -> Path:
    configured = os.environ.get("KATSOFT_LIBS_DIR")
    root = Path(configured) if configured else REPO.parent / "katsoft-libs"
    return root / SOURCE_SUBPATH


def test_winacl_vendored_bytes_equal_source():
    source = _source_file()
    if not source.parent.is_dir():
        pytest.skip("no katsoft-libs checkout at %s (set KATSOFT_LIBS_DIR)" % source.parent)
    assert VENDORED.is_file(), "hooks/winacl.py is missing; copy %s over it" % source
    assert VENDORED.read_bytes() == source.read_bytes(), "hooks/winacl.py differs from katsoft-libs; copy the source over it"


class FakeWinacl:
    """Records the calls the hooks make and applies a policy: ``bad`` paths fail validation."""

    def __init__(self, R: Any, bad: tuple[str, ...] = ()) -> None:
        self.R = R
        self.bad = bad
        self.calls: list[tuple[str, str]] = []

    def _judge(self, what: str, path: Any) -> None:
        self.calls.append((what, os.path.basename(str(path))))
        if any(str(path).endswith(name) for name in self.bad):
            raise self.R.HookError("grants access to S-1-1-0; only the current user and SYSTEM may have it")

    def refuse_writable_ancestors(self, path: Any) -> None:
        self._judge("refuse_writable_ancestors", path)

    def validate_fd(self, fd: int, path: Any) -> None:
        self._judge("validate_fd", path)

    def validate(self, path: Any) -> None:
        self._judge("validate", path)

    def provision(self, path: Any, directory: bool = False) -> None:
        self._judge("provision_dir" if directory else "provision", path)

    def provision_fd(self, fd: int, path: Any) -> None:
        self._judge("provision_fd", path)

    def create_private_dir(self, path: Any) -> None:
        self._judge("create_private_dir", path)
        os.mkdir(path)

    def create_private_file(self, path: Any) -> int:
        self._judge("create_private_file", path)
        return os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o666)


def _fake_nt(monkeypatch: pytest.MonkeyPatch, R: Any, fake: FakeWinacl) -> None:
    monkeypatch.setattr(R, "_on_nt", lambda: True)
    monkeypatch.setattr(R, "load_winacl", lambda: fake)


def _env(tmp_path: Path) -> dict[str, str]:
    return {"XDG_STATE_HOME": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path / "data"), "HOME": str(tmp_path)}


def _token_file(env: dict[str, str], mode: int = 0o666) -> Path:
    path = Path(env["XDG_STATE_HOME"]) / "llm-kit" / "relay" / "relay.token"
    path.parent.mkdir(parents=True)
    path.write_text(TOKEN + "\n")
    path.chmod(mode)
    return path


@pytest.mark.skipif(os.name == "nt", reason="the fake-nt run needs POSIX modes to prove they are not consulted")
def test_hook_token_read_nt_policy(tmp_path, monkeypatch, R):
    """On nt the token's mode bits mean nothing; its ACL decides. A 0666 file the ACL accepts is read, and a file
    the ACL refuses is never read, whatever its mode says."""
    env = _env(tmp_path)
    path = _token_file(env, mode=0o666)

    fake = FakeWinacl(R)
    _fake_nt(monkeypatch, R, fake)
    assert R.read_token(env) == TOKEN
    assert ("validate_fd", "relay.token") in fake.calls

    refused = FakeWinacl(R, bad=("relay.token",))
    _fake_nt(monkeypatch, R, refused)
    with pytest.raises(R.HookError, match="relay token .*relay.token.*only the current user and SYSTEM"):
        R.read_token(env)

    path.chmod(0o600)  # a tight mode does not rescue a refused ACL either
    with pytest.raises(R.HookError, match="only the current user and SYSTEM"):
        R.read_token(env)

    path.unlink()  # a missing token stays "no relay has run", not a policy failure
    with pytest.raises(R.RelayUnavailable):
        R.read_token(env)


@pytest.mark.skipif(os.name == "nt", reason="the fake-nt run needs POSIX modes to prove they are not consulted")
def test_enablement_record_nt_policy(tmp_path, monkeypatch, R):
    """On nt the enablement record is created private (directory and file) before a byte is written, validated
    after the replace, and read only when its ACL passes."""
    env = _env(tmp_path)
    fake = FakeWinacl(R)
    _fake_nt(monkeypatch, R, fake)

    path = R.enable(env, SESSION, "/direct", now=1.0)
    kinds = [what for what, _ in fake.calls]
    assert kinds[:6] == ["refuse_writable_ancestors", "create_private_dir", "provision_dir", "validate",
                         "create_private_file", "provision_fd"]
    assert kinds.index("validate_fd") < len(kinds) - 1 and kinds[-1] == "validate", kinds  # the temp file is judged before the final path
    assert fake.calls[-1] == ("validate", path.name)
    assert json.loads(path.read_text(encoding="ascii"))["session_id"] == SESSION
    assert not [p for p in path.parent.iterdir() if p.name != path.name], "a temp file was left behind"

    fake.calls.clear()
    path.chmod(0o666)  # POSIX mode bits are not consulted on nt
    record = R.enablement(env, SESSION)
    assert record is not None and record["session_id"] == SESSION
    assert ("validate_fd", path.name) in fake.calls

    refused = FakeWinacl(R, bad=(path.name,))
    _fake_nt(monkeypatch, R, refused)
    with pytest.raises(R.HookError, match="enablement record .*only the current user and SYSTEM"):
        R.enablement(env, SESSION)

    gone = FakeWinacl(R)
    _fake_nt(monkeypatch, R, gone)
    path.unlink()
    assert R.enablement(env, SESSION) is None  # no record: not enabled, not an error

    shared = FakeWinacl(R, bad=("enabled",))  # a writable ancestor refuses the directory before anything is created
    _fake_nt(monkeypatch, R, shared)
    with pytest.raises(R.HookError, match="only the current user and SYSTEM"):
        R.enable(env, SESSION, "/direct", now=2.0)
    assert [what for what, _ in shared.calls] == ["refuse_writable_ancestors"]
    assert not path.parent.exists() or not list(path.parent.iterdir())

    broken = FakeWinacl(R, bad=(".tmp",))  # a temp file the ACL refuses is never written to
    _fake_nt(monkeypatch, R, broken)
    with pytest.raises(R.HookError, match="only the current user and SYSTEM"):
        R.enable(env, SESSION, "/direct", now=2.0)
    assert not path.exists()
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".")], "a refused temp file was left behind"


def test_load_winacl_runs_without_llm_kit(R):
    """The standalone interpreter has no llm_kit. The copy loads against a stand-in for its one llm_kit import,
    leaves sys.modules as it found it, and its policy raises the hooks' own HookError."""
    saved = {name: sys.modules.get(name) for name in ("llm_kit", "llm_kit.relay", "llm_kit.relay.config")}
    winacl = R.load_winacl()
    assert {name: sys.modules.get(name) for name in saved} == saved
    assert issubclass(winacl.RelayConfigError, R.HookError)
    assert R.load_winacl() is winacl

    user = "S-1-5-21-1-2-3-1001"
    everyone = winacl.Descriptor(owner=user, protected=True, dacl_present=True,
                                 aces=[winacl.Ace(0, 0, 0x1F01FF, user), winacl.Ace(0, 0, 0x1200A9, "S-1-1-0")])
    with pytest.raises(R.HookError, match="grants access to S-1-1-0"):
        winacl.check_descriptor(everyone, user, "relay.token")
    private = winacl.Descriptor(owner=user, protected=True, dacl_present=True,
                                aces=[winacl.Ace(0, 0, 0x1F01FF, user), winacl.Ace(0, 0, 0x1F01FF, "S-1-5-18")])
    winacl.check_descriptor(private, user, "relay.token")
    if os.name != "nt":
        with pytest.raises(R.HookError, match="only available on Windows"):
            winacl.validate(Path("relay.token"))
