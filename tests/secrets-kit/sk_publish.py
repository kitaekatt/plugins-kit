"""Publish helpers for fixtures that used to call `secrets_kit.repo.commit_and_push`.

`repo.commit_and_push` had no production caller once the authoring lifecycle
moved onto the owned publication path (`repo._commit_owned` +
`repo._publish_owned`, driven by `authoring.py`'s `_publish_outputs`). Deleting
the dead function still leaves two distinct test needs it used to serve, kept
separate here because they exercise different code and prove different
things:

- `fixture_commit_and_push` -- an ordinary add/commit/push, standing in for a
  DIFFERENT machine publishing state the test under focus then has to react
  to (a peer's manifest edit, a rejected remote, a pre-existing baseline
  commit). This is fixture setup, not secrets-kit's own hardened path, so it
  is a plain re-implementation of the retired function's own behavior
  (stage, commit, and on a push rejection fetch + rebase + retry once).
- `owned_commit_and_push` -- reproduces what `authoring.py` itself does to
  publish: computes the same expected tree via a private index seeded from
  the synced head, then calls `repo._commit_owned` and `repo._publish_owned`
  directly. Tests asserting a SECURITY property of secrets-kit's real
  publication path (e.g. that a hostile inherited Git environment variable
  cannot divert it) call this so they exercise the exact functions production
  uses, not a stand-in.
"""

from pathlib import Path
from typing import List, Optional

from secrets_kit import SecretsError
from secrets_kit import repo as repo_mod


def fixture_commit_and_push(clone_dir: Path, message: str, paths: List[str]) -> None:
    """Stage, commit, and push `paths` as an ordinary producer would.

    Not secrets-kit's own hardened publication path -- see
    `owned_commit_and_push` for that. For fixture setup only: simulating
    another machine's write, not exercising anything this plugin defends.
    """
    code, output = repo_mod._git(
        ["add", "--"] + paths, cwd=clone_dir, timeout=repo_mod.LOCAL_WRITE_TIMEOUT
    )
    if code != 0:
        raise SecretsError(f"fixture git add failed: {output}")

    code, output = repo_mod._git(
        ["commit", "-m", message], cwd=clone_dir, timeout=repo_mod.LOCAL_WRITE_TIMEOUT
    )
    if code != 0 and "nothing to commit" not in output:
        raise SecretsError(f"fixture git commit failed: {output}")

    code, output = repo_mod._git(["push", "--quiet"], cwd=clone_dir, timeout=repo_mod.CLONE_TIMEOUT)
    if code == 0:
        return

    # A different fixture producer having pushed meanwhile is the ordinary
    # case this stands in for; two authoring machines never actually race.
    repo_mod._git(["fetch", "--quiet", "--prune"], cwd=clone_dir, timeout=repo_mod.FETCH_TIMEOUT)
    code, rebase_output = repo_mod._git(
        ["rebase", "--quiet", "@{u}"], cwd=clone_dir, timeout=repo_mod.LOCAL_WRITE_TIMEOUT
    )
    if code == 0:
        code, output = repo_mod._git(["push", "--quiet"], cwd=clone_dir, timeout=repo_mod.CLONE_TIMEOUT)
        if code == 0:
            return
    else:
        repo_mod._git(["rebase", "--abort"], cwd=clone_dir, timeout=repo_mod.LOCAL_WRITE_TIMEOUT)
        output = f"{output}\nrebase onto the remote also failed: {rebase_output}"

    raise SecretsError(f"fixture git push failed: {output}")


def owned_commit_and_push(clone_dir: Path, message: str, paths: List[str]) -> None:
    """Publish `paths` through the same two calls `authoring.py` makes.

    Computes `expected_tree` the way `authoring._prepare_tree` does (a
    private index seeded from the synced head, then one hash-object +
    update-index per path), then calls `repo._commit_owned` and
    `repo._publish_owned` with the clone's actual branch and recorded origin.
    For tests asserting a property of secrets-kit's real publication path.
    """
    synced_head = repo_mod.head_sha(clone_dir)
    tree = _prepare_tree(clone_dir, synced_head, paths)
    commit = repo_mod._commit_owned(
        clone_dir, message, paths, expected_parent=synced_head, expected_tree=tree
    )
    branch = _current_branch(clone_dir)
    declared_repo = repo_mod._recorded_origin(clone_dir)
    evidence = repo_mod._publish_owned(
        clone_dir, commit_oid=commit, target_ref=branch, declared_repo=declared_repo
    )
    if evidence.outcome != "confirmed":
        raise SecretsError(
            f"fixture owned publish did not confirm: {evidence.outcome} ({evidence.reason})"
        )


def _current_branch(clone_dir: Path) -> str:
    code, output = repo_mod._git(
        ["symbolic-ref", "--quiet", "HEAD"], cwd=clone_dir, timeout=repo_mod.QUERY_TIMEOUT
    )
    if code != 0 or not output.startswith("refs/heads/"):
        raise SecretsError("fixture owned publish requires a direct branch HEAD")
    return output


def _prepare_tree(clone_dir: Path, synced_head: Optional[str], paths: List[str]) -> str:
    private_index = clone_dir / ".git" / "sk-fixture-index"
    base_args = ["read-tree", synced_head] if synced_head else ["read-tree", "--empty"]
    repo_mod._owned_query(clone_dir, base_args, index=private_index)
    for name in paths:
        full = clone_dir / name
        if not full.exists():
            repo_mod._owned_query(
                clone_dir, ["update-index", "--force-remove", "--", name], index=private_index
            )
            continue
        result = repo_mod._owned_git(clone_dir, ["hash-object", "-w", "--", name])
        oid = result.stdout.decode("ascii").rstrip("\n")
        if result.returncode != 0 or not repo_mod._object_id(oid):
            raise SecretsError("fixture owned publish could not hash a staged path")
        repo_mod._owned_query(
            clone_dir,
            ["update-index", "--add", "--cacheinfo", f"100644,{oid},{name}"],
            index=private_index,
        )
    tree = repo_mod._owned_query(clone_dir, ["write-tree"], index=private_index).decode(
        "ascii"
    ).rstrip("\n")
    try:
        private_index.unlink()
    except OSError:
        pass
    return tree
