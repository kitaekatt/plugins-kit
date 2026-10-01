"""p4 edit path of settings_writable, driven by a fake p4 (subprocess.run stand-in).

A real p4 executable on PATH is not portable to Windows (CreateProcess does not
resolve .cmd for a list argv), so the fake replaces subprocess.run inside
settings_writable and emulates the few p4 subcommands used.
"""

import os
import stat
import subprocess
from types import SimpleNamespace

from bootstrap_lib import settings_writable

MARKER = settings_writable._CL_MARKER


class FakeP4:
    def __init__(self, target, changes_out="", edit_fail_cl=None, edit_always_fail=None):
        self.target = target
        self.changes_out = changes_out      # when the listing is client-scoped
        self.unscoped_out = ""              # when it is not
        self.edit_fail_cl = edit_fail_cl
        self.edit_always_fail = edit_always_fail
        self.calls = []

    def __call__(self, argv, **kw):
        args = argv[1:]
        self.calls.append((args, kw.get("cwd")))
        ok = lambda out="": SimpleNamespace(returncode=0, stdout=out, stderr="")
        bad = lambda err: SimpleNamespace(returncode=1, stdout="", stderr=err)
        if args[0] == "info":
            return ok("Client name: c1\nUser name: u1\n")
        if args[0] == "changes":
            return ok(self.changes_out if "-c" in args else self.unscoped_out)
        if args[0] == "change":
            return ok("Change 77 created.")
        if args[0] == "edit":
            if self.edit_always_fail:
                return bad(self.edit_always_fail)
            if "-c" in args and args[args.index("-c") + 1] == self.edit_fail_cl:
                return bad(f"Change {self.edit_fail_cl} belongs to client other.")
            os.chmod(self.target, stat.S_IWRITE | stat.S_IREAD)
            return ok("opened for edit")
        raise AssertionError(f"unexpected p4 call: {args}")


def _target(tmp_path):
    d = tmp_path / ".claude"
    d.mkdir()
    f = d / "settings.json"
    f.write_text("{}")
    os.chmod(f, stat.S_IREAD)
    return f


def _patch(monkeypatch, fake):
    monkeypatch.setattr(settings_writable.subprocess, "run", fake)


def test_edit_runs_in_settings_dir_and_ignores_other_clients_changelist(tmp_path, monkeypatch):
    target = _target(tmp_path)
    fake = FakeP4(str(target), changes_out="", edit_fail_cl="99")
    # An unscoped listing would surface another workspace's marker CL 99.
    fake.unscoped_out = f"Change 99 on 2026/01/01 by u2@other *pending*\n\n\t{MARKER}\n"
    _patch(monkeypatch, fake)

    ok, detail = settings_writable._p4_edit(str(target))

    assert ok, detail
    assert all(cwd == str(target.parent) for _, cwd in fake.calls)
    changes = [a for a, _ in fake.calls if a[0] == "changes"]
    assert changes and all("-c" in a and "c1" in a for a in changes)
    assert not any("99" in a for a, _ in fake.calls if a[0] == "edit")
    assert any(a[:3] == ["edit", "-c", "77"] for a, _ in fake.calls)


def test_rejected_changelist_falls_back_to_plain_edit(tmp_path, monkeypatch):
    target = _target(tmp_path)
    fake = FakeP4(
        str(target),
        changes_out=f"Change 55 on 2026/01/01 by u1@c1 *pending*\n\n\t{MARKER}\n",
        edit_fail_cl="55",
    )
    _patch(monkeypatch, fake)

    ok, detail = settings_writable._p4_edit(str(target))

    assert ok, detail
    edits = [a for a, _ in fake.calls if a[0] == "edit"]
    assert edits[0][:3] == ["edit", "-c", "55"] and edits[-1][0] == "edit" and "-c" not in edits[-1]


def test_failure_message_surfaces_p4_stderr(tmp_path, monkeypatch):
    target = _target(tmp_path)
    fake = FakeP4(str(target), edit_always_fail="Client 'c1' unknown - use 'client' command")
    _patch(monkeypatch, fake)
    monkeypatch.setattr(settings_writable, "_p4_tracked", lambda p: True)

    result = settings_writable.ensure_writable(str(target))

    assert not result.ok
    assert "Client 'c1' unknown" in result.detail
