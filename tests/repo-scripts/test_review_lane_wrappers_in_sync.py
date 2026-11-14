"""git-kit and p4-kit carry byte-identical review-lane wrapper scripts.

run_review_lane.py and parse_review_lane.py are vendored into both kits so
each can run its lane without depending on the other. Drift would mean the
two kits dispatch review lanes differently for the same input.
"""

import filecmp
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WRAPPER_NAMES = ("run_review_lane.py", "parse_review_lane.py")
_PAIRS = [
    (
        _REPO_ROOT / "plugins" / "git-kit" / "scripts" / name,
        _REPO_ROOT / "plugins" / "p4-kit" / "scripts" / name,
    )
    for name in _WRAPPER_NAMES
]


class TestReviewLaneWrappersInSync:
    def test_all_wrapper_copies_exist(self):
        # Guards the pair list itself: a missing path would make the
        # equality check below meaningless.
        missing = [str(p) for pair in _PAIRS for p in pair if not p.is_file()]
        assert not missing, f"review-lane wrapper copies missing: {missing}"

    def test_wrapper_copies_are_byte_identical(self):
        diffs = []
        for git_copy, p4_copy in _PAIRS:
            if not filecmp.cmp(git_copy, p4_copy, shallow=False):
                diffs.append(
                    f"diverged: {p4_copy.relative_to(_REPO_ROOT)}\n"
                    f"  fix: cp {git_copy} {p4_copy}"
                )
        assert not diffs, (
            "review-lane wrappers must stay byte-identical across git-kit and "
            "p4-kit (edit git-kit, then copy):\n" + "\n".join(diffs)
        )
