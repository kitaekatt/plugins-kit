"""Shared glob matching for review identifiers."""

import fnmatch


def _matches_one_glob(norm: str, base: str, gnorm: str) -> bool:
    """Match one posix-normalized pattern against a normalized identifier.

    A ``**/`` prefix means "at ANY depth, including the root". For a
    single-segment tail, use a basename compare so a root file also matches.
    Multi-segment tails are tried rooted as well. Other patterns use ordinary
    fnmatch against the whole identifier.
    """
    if gnorm.startswith("**/"):
        tail = gnorm[3:]
        if "/" in tail:
            if fnmatch.fnmatch(norm, tail):
                return True
        elif fnmatch.fnmatch(base, tail):
            return True
    return fnmatch.fnmatch(norm, gnorm)


def matches_claim(identifier: str, claim_globs: list[str]) -> bool:
    """True if ``identifier`` is claimed by ``claim_globs``.

    A ``!`` pattern is an absolute exclusion evaluated before positives.
    An exclusions-only list claims nothing.
    """
    if not claim_globs:
        return False
    norm = identifier.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    positives: list[str] = []
    for glob in claim_globs:
        normalized = glob.replace("\\", "/")
        if normalized.startswith("!"):
            if _matches_one_glob(norm, base, normalized[1:]):
                return False
        else:
            positives.append(normalized)
    return any(_matches_one_glob(norm, base, glob) for glob in positives)
