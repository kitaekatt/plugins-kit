"""Legacy pool boundaries and shared lock contracts.

The former task-named take, blanket ignored-file deletion, and flat rerun tests
are superseded by test_worktree_lifecycle.py's real-remote safety contracts.
Run: uv run --extra dev pytest tests/git-kit/test_worktree_pool.py -q.
"""

from pathlib import Path
import pytest
from test_worktrees import wt, manifest  # noqa: F401
from test_worktree_lifecycle import lifecycle  # noqa: F401
import worktree_pool as pool_mod


@pytest.mark.parametrize("pool,match", [
    ([], "must be an object"),
    ({"size": 0}, "size"),
    ({"size": True}, "size"),
    ({"setup": []}, "setup"),
    ({"setup": [["ok"], []]}, "setup"),
    ({"rebuild_when_changed": "lock"}, "rebuild_when_changed"),
    ({"bogus": 1}, "Unknown"),
    ({"after_take": {"remove": ["../x"]}}, "after_take.remove"),
    ({"after_take": {"remove": ["/abs"]}}, "after_take.remove"),
    ({"after_take": {"steps": [[]]}}, "after_take.steps"),
    ({"after_take": {"other": 1}}, "after_take"),
])
def test_pool_settings_are_validated(wt, tmp_path, pool, match):  # noqa: F811
    manifest(Path.home(), {"git_kit": {"worktrees": {"enabled": True, "pool": pool}}})
    with pytest.raises(ValueError, match=match):
        wt.resolve_settings(tmp_path)


def test_pool_defaults(wt, tmp_path):  # noqa: F811
    manifest(Path.home(), {"git_kit": {"worktrees": {"enabled": True, "pool": {}}}})
    pool = wt.resolve_settings(tmp_path).pool
    assert (pool.size, pool.setup, pool.rebuild_when_changed) == (2, [["bootstrap", "run"]], [])
    assert (list(pool.after_take_remove), list(pool.after_take)) == ([], [])
    manifest(Path.home(), {"git_kit": {"worktrees": {"enabled": True}}})
    assert wt.resolve_settings(tmp_path).pool is None



@pytest.mark.parametrize("entry", ["take", "claim", "build", "source"])
def test_raw_pool_mutators_require_claim_protocol(lifecycle, entry):
    obj = lifecycle
    with pytest.raises(ValueError, match="configure|claim|migration"):
        if entry == "take":
            pool_mod.take(obj.repo, None, "task", Path("unused"))
        elif entry == "claim":
            pool_mod.claim(obj.namespace, obj.namespace / "raw")
        elif entry == "source":
            pool_mod.pool_source(obj.namespace / "ready", obj.repo, obj.url)
        else:
            pool_mod.build_one(obj.namespace, obj.repo, [])
    assert not obj.namespace.exists()


def test_pool_aliases_share_lifecycle_state(lifecycle):
    obj = lifecycle
    result = pool_mod.refill(obj.repo, None)
    assert len(result["built"]) == 1
    assert pool_mod.status(obj.repo, None)["namespace"] == str(obj.namespace)
    with pool_mod._file_lock(obj.state / "maintenance.lock", blocking=False) as first:
        assert first
        with pool_mod._file_lock(obj.state / "maintenance.lock", blocking=False) as second:
            assert not second


def test_remote_namespace_collision_preserves_records(lifecycle):
    obj = lifecycle
    obj.initialize()
    from worktree_lifecycle import atomic_json
    atomic_json(obj.state / "namespace.json", {"version": 1, "remote": "another/remote.git"})
    with pytest.raises(ValueError, match="collision"):
        obj.maintain()
    assert not (obj.namespace / "ready").exists()
