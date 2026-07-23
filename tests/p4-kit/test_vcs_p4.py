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

from p4kit_vcs.p4_vcs import P4Changeset, P4Vcs, P4VcsError  # noqa: E402


class FakeP4:
    """A scripted p4 runner: records every call, returns canned output.

    Signature matches ``P4Runner``: ``(args, input, cwd) -> (rc, out, err)``.
    ``change_o_spec`` is what ``p4 change -o <cl>`` returns; ``created_cl`` is
    the CL number ``p4 change -i`` (create form) reports.
    """

    def __init__(self, created_cl="4242", change_o_spec=""):
        self.calls = []  # list of (args, input, cwd)
        self.created_cl = created_cl
        self.change_o_spec = change_o_spec

    def __call__(self, args, input=None, cwd=None):
        self.calls.append((list(args), input, cwd))
        if args[:2] == ["change", "-i"]:
            return 0, f"Change {self.created_cl} created.\n", ""
        if args[:2] == ["change", "-o"]:
            return 0, self.change_o_spec, ""
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
