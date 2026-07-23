"""The git VcsBackend implementation -- the shipped, implied default.

Implements the ``VcsBackend`` protocol against a local git working tree.
Git is the implied default VCS for content-pipeline-kit (see the plugin
proposal's VCS-seam decision); a Perforce implementation of the same
protocol ships in p4-kit rather than here, so this plugin never depends on
p4 tooling.
"""


class GitVcs:
    def open_for_edit(self, path) -> None:
        raise NotImplementedError

    def add(self, path) -> None:
        raise NotImplementedError

    def make_changeset(self, description: str):
        raise NotImplementedError

    def move_into(self, changeset, paths: list) -> None:
        raise NotImplementedError

    def finalize_description(self, changeset, description: str) -> None:
        raise NotImplementedError

    def revert(self, path) -> None:
        raise NotImplementedError

    def delete_if_empty(self, changeset) -> None:
        raise NotImplementedError
