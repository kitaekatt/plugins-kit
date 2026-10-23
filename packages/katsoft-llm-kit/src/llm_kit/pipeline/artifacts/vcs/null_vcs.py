"""The no-op VcsBackend: CI, tests, and non-VCS consumers.

Every method is a no-op that satisfies the ``VcsBackend`` protocol shape
without touching any real version-control state. This is what lets
``deliver`` be exercised end-to-end in tests without a real git or p4
repository present.
"""


class NullVcs:
    def open_for_edit(self, path) -> None:
        pass

    def add(self, path) -> None:
        pass

    def make_changeset(self, description: str):
        return None

    def move_into(self, changeset, paths: list) -> None:
        pass

    def finalize_description(self, changeset, description: str) -> None:
        pass

    def revert(self, path) -> None:
        pass

    def delete_if_empty(self, changeset) -> None:
        pass
