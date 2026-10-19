"""The dashboard inventory: discovery, caching, deduplication, read-only.

Hypothesis tested here: the task system's own discovery and validation can feed
the dashboard without changing ``task_system`` -- the inventory must agree with
``collect_listing`` (what ``task.py list --scope all`` projects) on which tasks
exist and how they classify.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from dashboard_support import git, link_dir, make_task, tree_digest

from task_system.dashboard import repo_identity, vcs
from task_system.dashboard.inventory import (
    Inventory,
    InventorySnapshot,
    project_id_for,
)
from task_system.discovery import discover
from task_system.listing import collect_listing

ITEMS = [
    {"id": "later-item", "title": "Later item", "state": "available", "priority": "P3"},
    {"id": "first-item", "title": "First item", "state": "available", "priority": "P1"},
    {"id": "mid-flight", "title": "Mid flight", "state": "in-flight"},
    {"id": "needs-me", "title": "Needs me", "state": "blocked-user", "note": "decide the loop length"},
    {"id": "parked", "title": "Parked", "state": "deferred"},
]


def make_world(tmp_path: Path) -> Path:
    devroot = tmp_path / "devroot"
    for name in ("alpha", "beta", "empty"):
        (devroot / name).mkdir(parents=True)
    make_task(devroot / "alpha", "dev/tasks", "open-one", title="Open one", items=ITEMS)
    make_task(devroot / "alpha", "tmp", "scratch", title="Scratch", status="deferred")
    make_task(devroot / "alpha", "dev/tasks", "done-one", title="Done", status="closed")
    make_task(devroot / "beta", "dev/tasks", "blocked-one", title="Blocked one", status="blocked",
              blocked_by=["dev/tasks/open-one"])
    return devroot


def snapshot_of(devroot: Path, **kw) -> tuple[Inventory, InventorySnapshot]:
    inventory = Inventory(parents=(devroot,), min_interval=0.0, **kw)
    return inventory, inventory.snapshot()


def tasks_by_id(snap: InventorySnapshot) -> dict[tuple[str, str], object]:
    return {(p.name, t.id): t for p in snap.projects for t in p.tasks}


def test_enumerates_projects_tasks_and_items(tmp_path):
    devroot = make_world(tmp_path)
    _inv, snap = snapshot_of(devroot)
    assert [p.name for p in snap.projects] == ["alpha", "beta"]  # `empty` has no tasks
    tasks = tasks_by_id(snap)
    assert set(tasks) == {
        ("alpha", "dev/tasks/open-one"), ("alpha", "dev/tasks/done-one"), ("beta", "dev/tasks/blocked-one"),
    }  # alpha's tmp/scratch is not listed: the dashboard does not support tmp/ tasks
    core = tasks[("alpha", "dev/tasks/open-one")].core
    assert core.classification == "active"
    assert core.title == "Open one"
    # Items are in plan.md priority order (P1 < P3; unprioritised last, block order kept).
    assert [i.id for i in core.items] == ["first-item", "later-item", "mid-flight", "needs-me", "parked"]
    assert {i.id: i.ui_state for i in core.items} == {
        "first-item": "available", "later-item": "available", "mid-flight": "in-flight",
        "needs-me": "blocked", "parked": "deferred",
    }
    assert tasks[("beta", "dev/tasks/blocked-one")].core.classification == "blocked"
    assert tasks[("beta", "dev/tasks/blocked-one")].core.blocked_by == ("dev/tasks/open-one",)


def test_project_identity_is_the_repository_and_the_checkout_keeps_its_own_hash(tmp_path):
    devroot = make_world(tmp_path)
    _inv, snap = snapshot_of(devroot)
    alpha = next(p for p in snap.projects if p.name == "alpha")
    assert alpha.id == repo_identity.repo_id_for("alpha") and len(alpha.id) == 64
    assert alpha.id != next(p for p in snap.projects if p.name == "beta").id
    assert alpha.roots == (str(devroot / "alpha"),)
    task = tasks_by_id(snap)[("alpha", "dev/tasks/open-one")]
    assert task.checkout_id == project_id_for(devroot / "alpha") != alpha.id
    assert task.project_root == str(devroot / "alpha") and task.project_id == alpha.id
    assert snap.find_task(task.checkout_id, task.id) is task
    assert snap.find_task(alpha.id, task.id) is None  # routes key on the checkout, not the repository


def test_tmp_tasks_are_not_in_the_dashboard_at_all(tmp_path):
    devroot = make_world(tmp_path)
    make_task(devroot / "beta", "tmp", "only-tmp", title="Only tmp")
    (devroot / "tmponly" / "tmp").mkdir(parents=True)
    make_task(devroot / "tmponly", "tmp", "lonely", title="Lonely")
    inv, snap = snapshot_of(devroot)
    assert [t for p in snap.projects for t in p.tasks if t.location == "tmp" or t.id.startswith("tmp/")] == []
    assert "tmponly" not in [p.name for p in snap.projects]  # a project with only tmp/ tasks has nothing to show
    assert ("alpha", "tmp/scratch") not in tasks_by_id(snap)
    # The task system itself still sees them: only the dashboard's view drops tmp.
    assert any(r.id == "tmp/scratch" for r in discover("all", devroot, devroot=devroot))


def test_skips_parked_archives_and_noncanonical_folders(tmp_path):
    devroot = make_world(tmp_path)
    make_task(devroot / "alpha", "dev/tasks/archived-tasks", "gone", title="Parked")
    make_task(devroot / "alpha", "dev/tasks/group", "nested", title="Nested too deep")
    (devroot / "alpha" / "dev" / "tasks" / "no-yaml").mkdir()
    _inv, snap = snapshot_of(devroot)
    ids = {t.id for p in snap.projects for t in p.tasks}
    assert not any("archived-tasks" in i or "nested" in i or "no-yaml" in i for i in ids)


def test_unchanged_poll_reads_nothing_and_a_change_reads_one_folder(tmp_path):
    devroot = make_world(tmp_path)
    inv, first = snapshot_of(devroot)
    loads = inv.loads
    assert loads == 3
    again = inv.snapshot()
    assert inv.loads == loads and again.revision == first.revision
    # Edit ONE task's plan.md (size changes): exactly that folder is re-read.
    plan = devroot / "alpha" / "dev" / "tasks" / "open-one" / "plan.md"
    plan.write_text(plan.read_text(encoding="utf-8") + "\nmore\n", encoding="utf-8")
    inv.snapshot()
    assert inv.loads == loads + 1
    # An explicit refresh re-validates everything (git state is not in a fingerprint).
    inv.snapshot(force=True)
    assert inv.loads == loads + 1 + 3


def test_revision_tracks_what_is_shown(tmp_path):
    devroot = make_world(tmp_path)
    inv, first = snapshot_of(devroot)
    plan = devroot / "alpha" / "dev" / "tasks" / "open-one" / "plan.md"
    plan.write_text(plan.read_text(encoding="utf-8").replace("id: parked\n    title: Parked\n    state: deferred",
                                                             "id: parked\n    title: Parked\n    state: available"),
                    encoding="utf-8")
    assert inv.snapshot().revision != first.revision


def test_min_interval_throttles_the_walk(tmp_path):
    devroot = make_world(tmp_path)
    now = [100.0]
    inv = Inventory(parents=(devroot,), min_interval=5.0, clock=lambda: now[0])
    first = inv.snapshot()
    (devroot / "alpha" / "dev" / "tasks" / "open-one" / "log.md").write_text("changed\n", encoding="utf-8")
    assert inv.snapshot() is first  # inside the interval: the cached snapshot
    now[0] += 6
    assert inv.snapshot() is not first
    assert inv.snapshot(force=True) is not first


def test_refreshing_never_writes_a_task_folder(tmp_path):
    """RED CHECK input: a refresh, forced or not, leaves every byte unchanged."""
    devroot = make_world(tmp_path)
    before = tree_digest(devroot)
    inv = Inventory(parents=(devroot,), min_interval=0.0)
    for force in (False, False, True, False, True):
        inv.snapshot(force=force)
    assert tree_digest(devroot) == before


def test_folder_reached_through_two_projects_is_owned_once(tmp_path):
    """A linked dev/tasks is walked once and assigned to the shortest project name."""
    devroot = tmp_path / "devroot"
    shared = tmp_path / "shared-tasks"
    make_task(shared.parent, "shared-tasks", "shared-task", title="Shared")
    for name in ("plugins-kit", "plugins-kit3"):
        (devroot / name / "dev").mkdir(parents=True)
        link_dir(devroot / name / "dev" / "tasks", shared)
    _inv, snap = snapshot_of(devroot)
    owners = [(p.name, t.id) for p in snap.projects for t in p.tasks]
    assert owners == [("plugins-kit", "dev/tasks/shared-task")]


def test_inventory_agrees_with_the_task_system_listing(tmp_path):
    """The hypothesis: same task set, same classification as `list --scope all`."""
    devroot = make_world(tmp_path)
    for name in ("plugins-kit", "plugins-kit3"):
        (devroot / name / "dev").mkdir(parents=True)
    shared = tmp_path / "shared-tasks"
    make_task(shared.parent, "shared-tasks", "shared-task", title="Shared")
    for name in ("plugins-kit", "plugins-kit3"):
        link_dir(devroot / name / "dev" / "tasks", shared)
    _inv, snap = snapshot_of(devroot)
    mine = {(p.name, t.id): t.core.classification for p in snap.projects for t in p.tasks}
    listing = collect_listing("all", devroot, devroot=devroot)
    # The dashboard's one deliberate difference from `list --scope all`: no tmp/ tasks.
    theirs = {(v.project_name, v.id): v.status for v in listing.views
              if v.status != "archived" and not v.id.startswith("tmp/")}
    records = discover("all", devroot, devroot=devroot)
    assert mine == theirs
    assert {(r.project_name, r.id) for r in records if not r.id.startswith("tmp/")} == set(mine)


def test_findings_are_carried_per_task(tmp_path):
    devroot = make_world(tmp_path)
    # A pre-contract folder: no task_items block in plan.md -> validate warns.
    folder = make_task(devroot / "alpha", "dev/tasks", "old-style")
    (folder / "plan.md").write_text("# Plan\n\nno block here\n", encoding="utf-8")
    _inv, snap = snapshot_of(devroot)
    core = tasks_by_id(snap)[("alpha", "dev/tasks/old-style")].core
    assert core.has_items_block is False
    assert any("task_items" in w for w in core.warnings)


def test_missing_project_directories_is_reported_not_raised(tmp_path):
    config = tmp_path / "task.local.yaml"
    config.write_text("project_directories: []\n", encoding="utf-8")
    old = os.environ.pop("DEVROOT", None)
    try:
        inv = Inventory(config_path=config, min_interval=0.0)
        snap = inv.snapshot()
    finally:
        if old is not None:
            os.environ["DEVROOT"] = old
    assert snap.projects == ()
    assert [e["code"] for e in snap.errors] == ["discovery"]
    assert "project directories" in snap.errors[0]["message"]


def test_configured_project_directories_are_read_from_the_config(tmp_path):
    devroot = make_world(tmp_path)
    config = tmp_path / "task.local.yaml"
    config.write_text(f"project_directories:\n  - '{devroot.as_posix()}'\n", encoding="utf-8")
    snap = Inventory(config_path=config, min_interval=0.0).snapshot()
    assert [p.name for p in snap.projects] == ["alpha", "beta"]


def test_a_removed_task_folder_leaves_the_inventory(tmp_path):
    import shutil

    devroot = make_world(tmp_path)
    inv, _first = snapshot_of(devroot)
    assert ("alpha", "dev/tasks/done-one") in tasks_by_id(inv.snapshot())
    shutil.rmtree(devroot / "alpha" / "dev" / "tasks" / "done-one")
    snap = inv.snapshot()
    assert ("alpha", "dev/tasks/done-one") not in tasks_by_id(snap)


# ---------------------------------------------------------------------------
# repository identity: the project is the git repository, not the checkout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,name", [
    ("git@github.com:kitaekatt/plugins-kit.git", "plugins-kit"),
    ("git@github.com:kitaekatt/plugins-kit", "plugins-kit"),
    ("https://github.com/kitaekatt/plugins-kit.git", "plugins-kit"),
    ("https://github.com/kitaekatt/plugins-kit", "plugins-kit"),
    ("https://github.com/kitaekatt/plugins-kit.git/", "plugins-kit"),
    ("ssh://git@host.example:2222/team/Afterplay.git", "Afterplay"),
    ("git@host.example:Afterplay.git", "Afterplay"),
    ("/srv/git/plugins-kit.git", "plugins-kit"),
    ("C:\\repos\\plugins-kit.git", "plugins-kit"),
    ("  https://github.com/kitaekatt/plugins-kit.GIT\n", "plugins-kit"),
])
def test_repo_name_is_the_remote_url_basename_without_dot_git(url, name):
    assert repo_identity.repo_name_from_remote(url) == name


@pytest.mark.parametrize("url", ["", "   ", "/", ".git"])
def test_a_remote_that_names_nothing_yields_no_repo_name(url):
    assert repo_identity.repo_name_from_remote(url) is None


def checkout(devroot: Path, name: str, origin: str | None) -> Path:
    root = devroot / name
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    if origin:
        git(root, "remote", "add", "origin", origin)
    return root


def test_repo_name_for_reads_origin_and_falls_back_to_the_directory_name(tmp_path):
    with_origin = checkout(tmp_path, "plugins-kit3", "git@github.com:kitaekatt/plugins-kit.git")
    no_remote = checkout(tmp_path, "scratchpad", None)
    other_remote = checkout(tmp_path, "mirror", None)
    git(other_remote, "remote", "add", "upstream", "https://example.invalid/not-origin.git")
    (tmp_path / "plain").mkdir()
    assert repo_identity.repo_name_for(with_origin) == "plugins-kit"
    assert repo_identity.repo_name_for(no_remote) == "scratchpad"
    assert repo_identity.repo_name_for(other_remote) == "mirror"  # only `origin` counts
    assert repo_identity.repo_name_for(tmp_path / "plain") == "plain"  # not a repository at all


def test_checkouts_of_one_repository_are_one_project_with_each_tasks_working_root(tmp_path):
    devroot = tmp_path / "devroot"
    origin = "git@github.com:kitaekatt/plugins-kit.git"
    roots = {name: checkout(devroot, name, origin) for name in ("plugins-kit", "plugins-kit2", "plugins-kit3")}
    make_task(roots["plugins-kit"], "dev/tasks", "one", title="One")
    make_task(roots["plugins-kit2"], "dev/tasks", "two", title="Two")
    make_task(roots["plugins-kit3"], "dev/tasks", "three", title="Three")
    afterplay = checkout(devroot, "Afterplay", "https://example.invalid/Afterplay.git")
    afterplay2 = checkout(devroot, "Afterplay2", "https://example.invalid/Afterplay.git")
    make_task(afterplay, "dev/tasks", "a", title="A")
    make_task(afterplay2, "dev/tasks", "b", title="B")
    loner = checkout(devroot, "loner", None)
    make_task(loner, "dev/tasks", "solo", title="Solo")
    _inv, snap = snapshot_of(devroot)
    assert [p.name for p in snap.projects] == ["Afterplay", "loner", "plugins-kit"]
    kit = next(p for p in snap.projects if p.name == "plugins-kit")
    assert kit.roots == tuple(sorted(str(r) for r in roots.values()))
    assert {t.id: t.project_root for t in kit.tasks} == {
        "dev/tasks/one": str(roots["plugins-kit"]), "dev/tasks/two": str(roots["plugins-kit2"]),
        "dev/tasks/three": str(roots["plugins-kit3"]),
    }
    assert len({t.checkout_id for t in kit.tasks}) == 3 and {t.project_id for t in kit.tasks} == {kit.id}
    assert {t.id for p in snap.projects if p.name == "Afterplay" for t in p.tasks} == {"dev/tasks/a", "dev/tasks/b"}
    # A checkout with no remote is identified by its own directory name.
    assert next(p for p in snap.projects if p.name == "loner").roots == (str(loner),)


def test_same_task_path_in_two_checkouts_of_one_repo_stays_two_addressable_tasks(tmp_path):
    devroot = tmp_path / "devroot"
    origin = "https://example.invalid/Afterplay.git"
    one, two = checkout(devroot, "Afterplay", origin), checkout(devroot, "Afterplay2", origin)
    make_task(one, "dev/tasks", "same", title="Same")
    make_task(two, "dev/tasks", "same", title="Same")
    _inv, snap = snapshot_of(devroot)
    assert len(snap.projects) == 1
    tasks = snap.projects[0].tasks
    assert [t.id for t in tasks] == ["dev/tasks/same"] * 2 and tasks[0].checkout_id != tasks[1].checkout_id
    for task in tasks:
        assert snap.find_task(task.checkout_id, "dev/tasks/same") is task


def test_a_linked_dev_tasks_shared_by_checkouts_of_one_repo_is_one_task_in_one_working_root(tmp_path):
    devroot = tmp_path / "devroot"
    shared = tmp_path / "shared-tasks"
    make_task(shared.parent, "shared-tasks", "shared-task", title="Shared")
    origin = "git@github.com:kitaekatt/plugins-kit.git"
    for name in ("plugins-kit", "plugins-kit3"):
        root = checkout(devroot, name, origin)
        (root / "dev").mkdir()
        link_dir(root / "dev" / "tasks", shared)
    _inv, snap = snapshot_of(devroot)
    assert [(p.name, t.id, t.project_root) for p in snap.projects for t in p.tasks] == [
        ("plugins-kit", "dev/tasks/shared-task", str(devroot / "plugins-kit"))]


def test_a_remote_that_changes_is_picked_up_by_a_forced_refresh_only(tmp_path):
    devroot = tmp_path / "devroot"
    root = checkout(devroot, "work", "https://example.invalid/first.git")
    make_task(root, "dev/tasks", "t", title="T")
    inv, snap = snapshot_of(devroot)
    assert [p.name for p in snap.projects] == ["first"]
    git(root, "remote", "set-url", "origin", "https://example.invalid/second.git")
    assert [p.name for p in inv.snapshot().projects] == ["first"]  # read once per checkout
    assert [p.name for p in inv.snapshot(force=True).projects] == ["second"]


def test_a_remote_git_cannot_read_falls_back_to_the_directory_name_and_says_so(tmp_path, monkeypatch):
    devroot = tmp_path / "devroot"
    root = checkout(devroot, "work", "https://example.invalid/first.git")
    make_task(root, "dev/tasks", "t", title="T")

    def refuse(_root, name="origin"):
        raise vcs.GitError("git config remote.origin.url failed: dubious ownership")

    monkeypatch.setattr(vcs, "remote_url", refuse)
    inv, snap = snapshot_of(devroot)
    assert [p.name for p in snap.projects] == ["work"]
    assert [e["code"] for e in snap.errors] == ["repo-identity"] and "dubious ownership" in snap.errors[0]["message"]
    assert [e["code"] for e in inv.snapshot().errors] == ["repo-identity"]  # reported on every snapshot until it clears
