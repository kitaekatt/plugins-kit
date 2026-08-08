"""Tests for content_pipeline.store.intermediary.

These cases pin the two-stage cheap-hash / full-rebuild behavior: cheap-path
short-circuit on a matching stored hash, full-path
rebuild on drift / missing / empty-hash, always-write-and-re-stamp on the
full path, and the no-source no-op. No game/domain concepts appear: an
"entity" has raw-source-derived inputs, a synthesized intermediary slice, and
a stored hash.
"""

from content_pipeline.store.intermediary import (
    IntermediarySpec,
    ensure_intermediary,
)


class _Backend:
    """A tiny in-memory intermediary store with instrumented callables."""

    def __init__(self, *, current_hash, existing=None, rebuild_value=("built", "x")):
        self.current_hash = current_hash
        self.existing = existing  # (content, hash) tuple or None
        self.rebuild_value = rebuild_value  # (content, hash-to-write) or None
        self.rebuild_calls = 0
        self.writes = []

    def spec(self):
        return IntermediarySpec(
            inputs_hash=lambda: self.current_hash,
            load_existing=lambda: self.existing,
            stored_hash=lambda e: e[1],
            rebuild=self._rebuild,
            write=self._write,
            content_equal=lambda a, b: a[0] == b[0],
        )

    def _rebuild(self):
        self.rebuild_calls += 1
        if self.rebuild_value is None:
            return None
        return (self.rebuild_value[0], self.current_hash)

    def _write(self, intermediary, h):
        self.existing = intermediary
        self.writes.append((intermediary, h))


# -- cheap path ---------------------------------------------------------------

def test_cheap_path_short_circuits_on_matching_hash():
    be = _Backend(current_hash="H", existing=("content", "H"))
    result = ensure_intermediary(be.spec())
    assert result.changed is False
    assert result.rebuilt is False
    assert be.rebuild_calls == 0  # never touched the expensive synthesis
    assert be.writes == []
    assert result.intermediary == ("content", "H")


# -- full path: missing -------------------------------------------------------

def test_full_path_when_missing():
    be = _Backend(current_hash="H", existing=None, rebuild_value=("fresh",))
    result = ensure_intermediary(be.spec())
    assert result.rebuilt is True
    assert result.changed is True
    assert be.rebuild_calls == 1
    assert be.writes and be.writes[0][1] == "H"  # current hash stamped
    assert result.intermediary == ("fresh", "H")


# -- full path: hash drift ----------------------------------------------------

def test_full_path_on_hash_drift():
    be = _Backend(current_hash="NEW", existing=("old", "OLD"), rebuild_value=("rebuilt",))
    result = ensure_intermediary(be.spec())
    assert result.rebuilt is True
    assert result.changed is True
    assert result.content_changed is True  # 'old' vs 'rebuilt'
    assert be.writes[0][1] == "NEW"


def test_full_path_on_empty_stored_hash():
    # A stored intermediary with an empty recorded hash cannot be trusted;
    # the full path runs (legacy artifact written before the hash existed).
    be = _Backend(current_hash="H", existing=("old", ""), rebuild_value=("rebuilt",))
    result = ensure_intermediary(be.spec())
    assert result.rebuilt is True
    assert result.changed is True


# -- full path: hash drift but identical content (re-stamp) -------------------

def test_full_path_rewrites_hash_even_when_content_identical():
    # Pathological: hash drifted but synthesized content is byte-identical.
    # Still writes (re-stamps) so the cheap path reclaims the entity next run;
    # content_changed reports False to distinguish a re-stamp from a rewrite.
    be = _Backend(current_hash="NEW", existing=("same", "OLD"), rebuild_value=("same",))
    result = ensure_intermediary(be.spec())
    assert result.changed is True
    assert result.content_changed is False
    assert be.writes[0][1] == "NEW"


# -- no source -> no-op -------------------------------------------------------

def test_no_source_is_a_noop():
    be = _Backend(current_hash="H", existing=None, rebuild_value=None)
    result = ensure_intermediary(be.spec())
    assert result.rebuilt is True
    assert result.changed is False
    assert result.intermediary is None
    assert be.writes == []
