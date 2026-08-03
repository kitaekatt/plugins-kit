"""Tests for p4kit_vcs.p4_vcs (the Perforce VcsBackend).

Drives P4Vcs against a scripted fake p4 runner -- NO real p4 calls -- asserting
the exact operation mapping distilled from firstpass_ops.cl_creation:

- make_changeset builds a minimal ``p4 change -i`` spec with tab-prefixed
  description lines and NO ``Files:`` section, and parses the new CL number.
- open_for_edit / add / revert / move_into issue the exact p4 verbs on the
  exact paths, and reject p4 wildcards (``...`` / ``*``).
- finalize_description dump-edit-restores, replacing ONLY the Description block
  and preserving the auto-populated ``Files:`` section.
- delete_if_empty deletes an empty CL and leaves a non-empty one alone.
- deliver_changeset (content_pipeline's backend-agnostic choreography) drives a
  fake-runner P4Vcs end to end (duck-type conformance).

content_pipeline is importable in this repo's test env (sibling plugin under
plugins/), so the integration test imports deliver_changeset from it; the p4_vcs
module itself imports nothing from content_pipeline.
"""

import os
import sys

import pytest

_P4KIT_LIB = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                 "plugins", "p4-kit", "lib")
)
if _P4KIT_LIB not in sys.path:
    sys.path.insert(0, _P4KIT_LIB)

_CP_LIB = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                 "plugins", "content-pipeline-kit", "lib")
)
if _CP_LIB not in sys.path:
    sys.path.insert(0, _CP_LIB)

from p4kit_vcs.p4_vcs import (  # noqa: E402
    P4Changeset,
    P4ChangesetContents,
    P4Vcs,
    P4VcsError,
)


class FakeP4:
    """A scripted p4 runner: records every call, returns canned output.

    Signature matches ``P4Runner``: ``(args, input, cwd) -> (rc, out, err)``.
    ``change_o_spec`` is what ``p4 change -o <cl>`` returns; ``created_cl`` is
    the CL number ``p4 change -i`` (create form) reports.
    """

    def __init__(self, created_cl="4242", change_o_spec="",
                 opened_ztag="", opened_c_ztag=""):
        self.calls = []  # list of (args, input, cwd)
        self.created_cl = created_cl
        self.change_o_spec = change_o_spec
        self.opened_ztag = opened_ztag  # `p4 -ztag opened <path>` output
        self.opened_c_ztag = opened_c_ztag  # `p4 -ztag opened -c <cl>` output

    def __call__(self, args, input=None, cwd=None):
        self.calls.append((list(args), input, cwd))
        if args[:2] == ["change", "-i"]:
            return 0, f"Change {self.created_cl} created.\n", ""
        if args[:2] == ["change", "-o"]:
            return 0, self.change_o_spec, ""
        if args[:1] == ["reopen"]:
            # ["reopen", "-c", <cl>, <path>] -- a real move reports "reopened".
            cl, path = args[2], args[3]
            return 0, f"{path}#1 - reopened; change {cl}\n", ""
        if args[:3] == ["-ztag", "opened", "-c"]:
            return 0, self.opened_c_ztag, ""
        if args[:2] == ["-ztag", "opened"]:
            return 0, self.opened_ztag, ""
        return 0, "", ""

    def inputs_for(self, prefix):
        """All stdin bodies passed to calls whose args start with ``prefix``."""
        return [inp for a, inp, _c in self.calls if a[: len(prefix)] == prefix]

    @property
    def arg_vectors(self):
        return [a for a, _i, _c in self.calls]


# -- make_changeset: spec format ---------------------------------------------

def test_make_changeset_spec_is_byte_exact_no_files_section():
    fake = FakeP4(created_cl="777")
    vcs = P4Vcs(client="tc", user="tu", runner=fake)
    cs = vcs.make_changeset("line one\nline two")

    assert isinstance(cs, P4Changeset)
    assert cs.cl == "777"
    # Exactly one `p4 change -i` create call; spec is byte-exact.
    (spec,) = fake.inputs_for(["change", "-i"])
    assert spec == (
        "Change: new\n"
        "Client: tc\n"
        "User: tu\n"
        "Status: pending\n"
        "Description:\n"
        "\tline one\n"
        "\tline two\n"
    )
    # The create spec must NOT carry a Files: section (default-CL sweep footgun).
    assert "Files:" not in spec


def test_make_changeset_tab_prefixes_blank_paragraph_line():
    fake = FakeP4()
    vcs = P4Vcs(client="c", user="u", runner=fake)
    vcs.make_changeset("title\n\nbody paragraph")
    (spec,) = fake.inputs_for(["change", "-i"])
    # Blank paragraph separator becomes a lone tab; every body line tab-prefixed.
    assert "Description:\n\ttitle\n\t\n\tbody paragraph\n" in spec


def test_make_changeset_reads_client_user_from_env(monkeypatch):
    monkeypatch.setenv("P4CLIENT", "envclient")
    monkeypatch.setenv("P4USER", "envuser")
    fake = FakeP4()
    vcs = P4Vcs(runner=fake)  # client/user unset -> env
    vcs.make_changeset("d")
    (spec,) = fake.inputs_for(["change", "-i"])
    assert "Client: envclient\n" in spec
    assert "User: envuser\n" in spec


def test_make_changeset_raises_when_cl_number_unparseable():
    class NoNumber(FakeP4):
        def __call__(self, args, input=None, cwd=None):
            self.calls.append((list(args), input, cwd))
            if args[:2] == ["change", "-i"]:
                return 0, "something unexpected\n", ""
            return 0, "", ""

    vcs = P4Vcs(client="c", user="u", runner=NoNumber())
    with pytest.raises(P4VcsError):
        vcs.make_changeset("d")


# -- simple verbs ------------------------------------------------------------

def test_open_for_edit_issues_p4_edit():
    fake = FakeP4()
    P4Vcs(runner=fake).open_for_edit("foo/bar.txt")
    assert ["edit", "foo/bar.txt"] in fake.arg_vectors


def test_add_issues_p4_add():
    fake = FakeP4()
    P4Vcs(runner=fake).add("foo/new.txt")
    assert ["add", "foo/new.txt"] in fake.arg_vectors


def test_revert_issues_p4_revert_exact_path():
    fake = FakeP4()
    P4Vcs(runner=fake).revert("foo/bar.txt")
    assert ["revert", "foo/bar.txt"] in fake.arg_vectors


def test_cwd_is_threaded_into_runner():
    fake = FakeP4()
    P4Vcs(cwd="/work/ws", runner=fake).open_for_edit("a.txt")
    (_args, _inp, cwd) = fake.calls[0]
    assert cwd == "/work/ws"


# -- move_into: exact paths, dedupe, never wildcard --------------------------

def test_move_into_reopens_exact_paths_and_records_deduped():
    fake = FakeP4()
    vcs = P4Vcs(runner=fake)
    cs = P4Changeset(cl="99")
    vcs.move_into(cs, ["a.txt", "b.txt"])
    vcs.move_into(cs, ["a.txt"])  # duplicate is deduped on the changeset

    assert ["reopen", "-c", "99", "a.txt"] in fake.arg_vectors
    assert ["reopen", "-c", "99", "b.txt"] in fake.arg_vectors
    assert cs.paths == ["a.txt", "b.txt"]


def test_move_into_requires_a_cl():
    vcs = P4Vcs(runner=FakeP4())
    with pytest.raises(P4VcsError):
        vcs.move_into(P4Changeset(cl=None), ["a.txt"])


@pytest.mark.parametrize("bad", ["depot/...", "foo/*.txt", "a/.../b"])
def test_wildcards_are_rejected_everywhere(bad):
    vcs = P4Vcs(runner=FakeP4())
    with pytest.raises(P4VcsError):
        vcs.open_for_edit(bad)
    with pytest.raises(P4VcsError):
        vcs.add(bad)
    with pytest.raises(P4VcsError):
        vcs.revert(bad)
    with pytest.raises(P4VcsError):
        vcs.move_into(P4Changeset(cl="1"), [bad])


# -- move_into: reopen output verification (no-op / wrong-CL detection) -------

def test_move_into_accepts_reopened_output_shape():
    # The default FakeP4 emits "<path>#1 - reopened; change <cl>" -- a real move.
    fake = FakeP4()
    vcs = P4Vcs(runner=fake)
    cs = P4Changeset(cl="99")
    vcs.move_into(cs, ["a.txt"])
    assert cs.paths == ["a.txt"]
    assert ["reopen", "-c", "99", "a.txt"] in fake.arg_vectors


def test_move_into_accepts_already_in_target_cl_noop():
    # Idempotent re-move: file already in the TARGET CL. p4 reports "currently
    # opened for edit; change <this-cl>" -- accepted, path recorded.
    def already_here(args, input=None, cwd=None):
        if args[:1] == ["reopen"]:
            path = args[3]
            return 0, f"{path}#1 - currently opened for edit; change 99\n", ""
        return 0, "", ""

    vcs = P4Vcs(runner=already_here)
    cs = P4Changeset(cl="99")
    vcs.move_into(cs, ["a.txt"])
    assert cs.paths == ["a.txt"]


def test_move_into_accepts_nothing_changed_noop():
    # Idempotent re-move, the other shape p4 emits when the file is ALREADY in
    # the target CL: "<depotFile>#<rev> - nothing changed." with exit 0. The
    # desired end state already holds, so it is a success, not a failure.
    def nothing_changed(args, input=None, cwd=None):
        if args[:1] == ["reopen"]:
            path = args[3]
            return 0, f"//depot/proj/{path}#83 - nothing changed.\n", ""
        return 0, "", ""

    vcs = P4Vcs(runner=nothing_changed)
    cs = P4Changeset(cl="99")
    vcs.move_into(cs, ["a.txt"])
    assert cs.paths == ["a.txt"]


def test_move_into_raises_on_silent_noop():
    # p4 reopen exits 0 but did nothing: the file was never open for edit.
    def noop(args, input=None, cwd=None):
        if args[:1] == ["reopen"]:
            path = args[3]
            return 0, f"{path} - file(s) not opened on this client.\n", ""
        return 0, "", ""

    vcs = P4Vcs(runner=noop)
    cs = P4Changeset(cl="99")
    with pytest.raises(P4VcsError) as exc:
        vcs.move_into(cs, ["a.txt"])
    assert "not opened on this client" in str(exc.value)
    assert cs.paths == []  # nothing recorded -- the move did not happen


def test_move_into_raises_on_wrong_cl():
    # p4 reopen exits 0 but the file is opened in a DIFFERENT CL, not the target
    # (its "currently opened for edit; change <n>" names another CL).
    def wrong_cl(args, input=None, cwd=None):
        if args[:1] == ["reopen"]:
            path = args[3]
            return 0, f"{path}#1 - currently opened for edit; change 12345\n", ""
        return 0, "", ""

    vcs = P4Vcs(runner=wrong_cl)
    cs = P4Changeset(cl="99")  # target is 99, file landed in 12345
    with pytest.raises(P4VcsError) as exc:
        vcs.move_into(cs, ["a.txt"])
    assert "into CL 99" in str(exc.value)
    assert cs.paths == []


def test_move_into_raises_on_nonzero_exit():
    def failing(args, input=None, cwd=None):
        if args[:1] == ["reopen"]:
            return 1, "", "some transport error"
        return 0, "", ""

    vcs = P4Vcs(runner=failing)
    cs = P4Changeset(cl="99")
    with pytest.raises(P4VcsError) as exc:
        vcs.move_into(cs, ["a.txt"])
    assert "some transport error" in str(exc.value)
    assert cs.paths == []


# -- finalize_description: dump-edit-restore, preserve Files: ----------------

_CHANGE_O = (
    "# A Perforce Change Specification.\n"
    "#\n"
    "#  Change:      The change number.\n"
    "\n"
    "Change: 4242\n"
    "\n"
    "Client: myclient\n"
    "\n"
    "User: myuser\n"
    "\n"
    "Status: pending\n"
    "\n"
    "Description:\n"
    "\tpending: content-pipeline delivery\n"
    "\n"
    "Files:\n"
    "\t//depot/foo.txt\t# edit\n"
    "\t//depot/bar.txt\t# edit\n"
)


def test_finalize_replaces_description_and_preserves_files():
    fake = FakeP4(change_o_spec=_CHANGE_O)
    vcs = P4Vcs(runner=fake)
    cs = P4Changeset(cl="4242", paths=["//depot/foo.txt", "//depot/bar.txt"])

    ret = vcs.finalize_description(cs, "Final title\n\nA body paragraph.")
    assert ret == "4242"
    assert cs.description == "Final title\n\nA body paragraph."

    # It dumped then restored.
    assert ["change", "-o", "4242"] in fake.arg_vectors
    restored = fake.inputs_for(["change", "-i"])[-1]

    # New description landed, tab-prefixed; old placeholder gone.
    assert "\tFinal title\n" in restored
    assert "\tA body paragraph.\n" in restored
    assert "pending: content-pipeline delivery" not in restored
    # Files: section preserved verbatim -- the whole point of dump-edit-restore.
    assert "Files:\n" in restored
    assert "\t//depot/foo.txt\t# edit\n" in restored
    assert "\t//depot/bar.txt\t# edit\n" in restored
    # Header comments and other fields survive too.
    assert "Change: 4242\n" in restored
    assert "Client: myclient\n" in restored


def test_finalize_noop_when_no_cl():
    fake = FakeP4()
    vcs = P4Vcs(runner=fake)
    cs = P4Changeset(cl=None)
    assert vcs.finalize_description(cs, "d") is None
    assert cs.description == "d"
    # No p4 change -o / -i attempted.
    assert fake.calls == []


# -- delete_if_empty ---------------------------------------------------------

def test_delete_if_empty_deletes_empty_cl():
    fake = FakeP4()
    vcs = P4Vcs(runner=fake)
    vcs.delete_if_empty(P4Changeset(cl="55", paths=[]))
    assert ["change", "-d", "55"] in fake.arg_vectors


def test_delete_if_empty_leaves_nonempty_cl():
    fake = FakeP4()
    vcs = P4Vcs(runner=fake)
    vcs.delete_if_empty(P4Changeset(cl="55", paths=["a.txt"]))
    assert ["change", "-d", "55"] not in fake.arg_vectors


# -- error propagation -------------------------------------------------------

def test_nonzero_exit_raises_p4vcserror():
    def failing(args, input=None, cwd=None):
        return 1, "", "some p4 error"

    vcs = P4Vcs(runner=failing)
    with pytest.raises(P4VcsError) as exc:
        vcs.open_for_edit("a.txt")
    assert "some p4 error" in str(exc.value)


# -- P4-specific extensions: owning_changeset / describe_changeset -----------

def test_owning_changeset_returns_numbered_cl():
    ztag = (
        "... depotFile //depot/a.txt\n"
        "... clientFile /ws/a.txt\n"
        "... rev 3\n"
        "... action edit\n"
        "... change 12345\n"
        "... type text\n"
    )
    fake = FakeP4(opened_ztag=ztag)
    assert P4Vcs(runner=fake).owning_changeset("a.txt") == "12345"
    assert ["-ztag", "opened", "a.txt"] in fake.arg_vectors


def test_owning_changeset_returns_default():
    ztag = (
        "... depotFile //depot/a.txt\n"
        "... clientFile /ws/a.txt\n"
        "... change default\n"
    )
    fake = FakeP4(opened_ztag=ztag)
    assert P4Vcs(runner=fake).owning_changeset("a.txt") == "default"


def test_owning_changeset_none_when_not_open():
    # p4 opened exits non-zero when the file is not open at all.
    def not_open(args, input=None, cwd=None):
        if args[:2] == ["-ztag", "opened"]:
            return 1, "", "a.txt - file(s) not opened on this client.\n"
        return 0, "", ""

    assert P4Vcs(runner=not_open).owning_changeset("a.txt") is None


def test_owning_changeset_none_when_no_row():
    fake = FakeP4(opened_ztag="")  # zero exit but no change field
    assert P4Vcs(runner=fake).owning_changeset("a.txt") is None


def test_owning_changeset_rejects_wildcard():
    with pytest.raises(P4VcsError):
        P4Vcs(runner=FakeP4()).owning_changeset("foo/...")


def test_describe_changeset_returns_description_and_paths():
    change_o = (
        "Change: 4242\n"
        "\n"
        "Client: c\n"
        "\n"
        "User: u\n"
        "\n"
        "Status: pending\n"
        "\n"
        "Description:\n"
        "\tFinal title\n"
        "\t\n"
        "\tA body paragraph.\n"
        "\n"
        "Files:\n"
        "\t//depot/one.txt\t# edit\n"
    )
    opened_c = (
        "... depotFile //depot/one.txt\n"
        "... clientFile /ws/one.txt\n"
        "... change 4242\n"
        "\n"
        "... depotFile //depot/two.txt\n"
        "... clientFile /ws/two.txt\n"
        "... change 4242\n"
    )
    fake = FakeP4(change_o_spec=change_o, opened_c_ztag=opened_c)
    contents = P4Vcs(runner=fake).describe_changeset("4242")

    assert isinstance(contents, P4ChangesetContents)
    assert contents.cl == "4242"
    assert contents.description == "Final title\n\nA body paragraph."
    assert contents.paths == ["/ws/one.txt", "/ws/two.txt"]
    assert ["change", "-o", "4242"] in fake.arg_vectors
    assert ["-ztag", "opened", "-c", "4242"] in fake.arg_vectors


def test_describe_changeset_supports_contents_assertion():
    # The whole point: compare what the CL claims (description) against what it
    # actually contains (paths) -- the description-vs-contents drift check.
    change_o = (
        "Change: 7\n\nStatus: pending\n\nDescription:\n"
        "\tdeliver: one, two\n\nFiles:\n\t//depot/one.txt\t# edit\n"
    )
    opened_c = (
        "... clientFile /ws/one.txt\n... change 7\n"
    )  # only ONE file actually open, though the description names two
    fake = FakeP4(change_o_spec=change_o, opened_c_ztag=opened_c)
    contents = P4Vcs(runner=fake).describe_changeset("7")
    assert contents.description == "deliver: one, two"
    assert contents.paths == ["/ws/one.txt"]  # drift is visible to the caller


# -- end-to-end choreography via content_pipeline.deliver_changeset ----------

def test_deliver_changeset_over_p4_backend_duck_types():
    from content_pipeline.deliver.inplace import deliver_changeset

    # change -o for the finalize step: a spec whose Files: lists the two paths.
    change_o = (
        "Change: 4242\n\nClient: c\n\nUser: u\n\nStatus: pending\n\n"
        "Description:\n\tpending: content-pipeline delivery\n\n"
        "Files:\n\t//depot/one.txt\t# edit\n\t//depot/two.txt\t# edit\n"
    )
    fake = FakeP4(created_cl="4242", change_o_spec=change_o)
    vcs = P4Vcs(client="c", user="u", runner=fake)

    written = []
    items = [
        {"id": "one", "path": "//depot/one.txt"},
        {"id": "two", "path": "//depot/two.txt"},
    ]

    result = deliver_changeset(
        items,
        vcs=vcs,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=lambda it: written.append(it["path"]),
        describe=lambda moved: "deliver: " + ", ".join(i for i, _p in moved),
    )

    assert written == ["//depot/one.txt", "//depot/two.txt"]
    assert [i for i, _p in result.moved] == ["one", "two"]
    assert result.changeset.cl == "4242"
    assert result.changeset.paths == ["//depot/one.txt", "//depot/two.txt"]
    assert result.description == "deliver: one, two"

    kinds = [a[:2] for a in fake.arg_vectors]
    # placeholder CL created up front, per-item edit+reopen, finalize dumped+restored.
    assert kinds[0] == ["change", "-i"]  # make_changeset
    assert ["edit"] == fake.arg_vectors[1][:1] or ["edit", "//depot/one.txt"] in fake.arg_vectors
    assert ["reopen", "-c"] in kinds
    assert ["change", "-o"] in kinds  # finalize dump-edit-restore
    # Non-empty CL: never deleted.
    assert ["change", "-d"] not in kinds
    # Finalize preserved the Files: section.
    restored = fake.inputs_for(["change", "-i"])[-1]
    assert "//depot/one.txt" in restored and "//depot/two.txt" in restored
    assert "deliver: one, two" in restored


def test_deliver_changeset_empty_batch_deletes_cl():
    from content_pipeline.deliver.inplace import deliver_changeset

    fake = FakeP4(created_cl="4242")
    vcs = P4Vcs(client="c", user="u", runner=fake)

    result = deliver_changeset(
        [],
        vcs=vcs,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=lambda it: None,
        describe=lambda moved: "unused",
    )
    assert result.moved == []
    assert result.description == ""
    # Empty CL is deleted.
    assert ["change", "-d", "4242"] in fake.arg_vectors
