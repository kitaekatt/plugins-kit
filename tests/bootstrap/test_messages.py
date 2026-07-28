"""Collated-message formatting rules (bootstrap_lib/messages.py).

The rules under test are stated in engine-internals.md ("Collated message
text"): number collated items, keep each within ITEM_MAX, and get there by
AUTHORING a short label (or dropping a trailing clause at a separator) -- never
by cutting text off. An item that still overflows is a missing label at its call
site, which is a bug to fix there rather than paper over here.
"""

import pytest

from bootstrap_lib.messages import ITEM_MAX, derive_short, item_label, numbered
from bootstrap_lib.records import Entry


class TestDeriveShort:
    def test_short_text_is_returned_unchanged(self):
        assert derive_short("install uv") == "install uv"

    def test_drops_the_explanation_at_a_separator(self):
        """Cutting at a separator the author wrote yields a whole clause that
        still names the subject -- unlike cutting at a character count."""
        assert derive_short(
            "uv: FAILED - install attempted but uv not found in PATH"
        ) == "uv: FAILED"

    def test_returns_none_when_no_whole_form_fits(self):
        """A signal to author a display= label at the call site, not licence to
        invent a shorter phrase."""
        assert derive_short("z" * 200) is None

    def test_never_emits_a_cut_off_marker(self):
        for text in ("z" * 200, "a" * 41 + " - tail"):
            assert "..." not in (derive_short(text) or "")


class TestNumbered:
    def test_multiple_items_are_numbered(self):
        assert numbered(["install uv", "create venv"]) == (
            "(1) install uv; (2) create venv"
        )

    def test_single_item_is_not_numbered(self):
        """"(1) x" alone disambiguates nothing and just adds ceremony."""
        assert numbered(["install uv"]) == "install uv"

    def test_empty_is_empty(self):
        assert numbered([]) == ""

    def test_blank_items_are_dropped_before_numbering(self):
        """Ordinals must count what the user can actually see."""
        assert numbered(["a", "  ", "b"]) == "(1) a; (2) b"

    def test_items_carrying_their_own_punctuation_stay_separable(self):
        """The whole point: without ordinals these four clauses read as one."""
        out = numbered(["synced a, b", "linked c; d"])
        assert out == "(1) synced a, b; (2) linked c; d"

    def test_accepts_a_generator(self):
        assert numbered(x for x in ["a", "b"]) == "(1) a; (2) b"

    def test_separator_is_overridable(self):
        assert numbered(["a", "b"], sep=" | ") == "(1) a | (2) b"

    def test_an_authored_short_label_is_used_when_the_item_overflows(self):
        entry = Entry("uv: install command failed - `winget install --id x -e`",
                      short="uv: install failed")
        assert numbered([entry, "ok"]) == "(1) uv: install failed; (2) ok"

    def test_falls_back_to_a_whole_derived_clause(self):
        long = "uv: FAILED - install attempted but uv not found in PATH"
        assert numbered([long, "ok"]) == "(1) uv: FAILED; (2) ok"

    def test_never_cuts_mid_word(self):
        """No "..." anywhere: the limit is met by authoring a label, and an
        item that still overflows is a bug to fix at its call site."""
        assert "..." not in numbered(["z" * 300, "y" * 300])

    def test_limit_can_be_disabled_for_an_unconstrained_surface(self):
        long = "q" * 400
        assert numbered([long], limit=None) == long


class TestItemLabel:
    def test_first_candidate_wins_when_it_fits(self):
        assert item_label("CUDA Toolkit", "some-name") == "CUDA Toolkit"

    def test_over_long_candidate_is_skipped_not_truncated(self):
        prose = "Parsec headless host: PER-COMPUTER install " + "x" * 300
        assert item_label(prose, "parsec-host") == "parsec-host"

    def test_a_fitting_candidate_is_preferred_over_a_longer_one(self):
        """Readability, not safety: a whole slug identifies the item, where the
        first 37 characters of a sentence spend the budget on its least
        distinguishing part."""
        assert item_label("y" * 200, "short") == "short"

    def test_empty_candidates_are_ignored(self):
        assert item_label(None, "", "   ", "name") == "name"

    def test_last_candidate_is_returned_whole_when_nothing_fits(self):
        """Never cut mid-word: a label that stops partway through an identifier
        is unrecognisable. An over-long item is a missing authored label."""
        long = "z" * (ITEM_MAX + 50)
        assert item_label(long) == long

    def test_no_candidates_at_all(self):
        assert item_label(None, "") == ""

    @pytest.mark.parametrize("limit", [10, ITEM_MAX, 200])
    def test_result_fits_when_any_candidate_does(self, limit):
        out = item_label("x" * 500, "ok", limit=limit)
        assert len(out) <= limit
