"""Static guards for the presence-kit how-it-works page."""
import hashlib
import re
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "presence-kit"
PAGE = PLUGIN / "docs" / "how-it-works.html"
PRESENCE_MD = PLUGIN / "references" / "presence.md"
SKILLS = ("presence-kit:afk", "presence-kit:autonomous", "presence-kit:recap")


def _page_bytes() -> bytes:
    return PAGE.read_bytes()


def test_page_hash_matches_presence_md():
    comments = re.findall(
        rb"<!--\s*presence\.md sha256:\s*([0-9a-f]{64})\s*-->", _page_bytes()
    )
    assert len(comments) == 1, "page needs exactly one presence.md sha256 comment"
    actual = hashlib.sha256(PRESENCE_MD.read_bytes()).hexdigest()
    assert comments[0].decode("ascii") == actual, (
        "references/presence.md changed; update the page content and its hash comment"
    )


def test_page_is_ascii():
    _page_bytes().decode("ascii")


def test_page_names_each_skill_in_full_form():
    text = _page_bytes().decode("ascii")
    for skill in SKILLS:
        assert f"/{skill}" in text, f"page does not name /{skill}"


def test_page_is_self_contained():
    text = _page_bytes().decode("ascii")
    assert not re.search(r"(?:src|href)\s*=\s*[\"']https?:", text)
    assert "@import" not in text
    assert "url(http" not in text


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_page_states_the_departure_write_the_correction_and_the_return():
    """The hash only forces a re-read; these pin what the prompt hook (K3) changed on the page."""
    text = _flat(_page_bytes().decode("ascii"))
    # the hook writes afk itself and tells Claude before it answers
    assert "A hook sets you afk right then and tells Claude before it answers" in text
    assert "saying it again while you are afk keeps you afk" in text
    assert "Claude recognizing that you are leaving, without the word, is the backup" in text
    # a wrong match is corrected by Claude
    assert "can set afk by mistake" in text
    assert "undoes the change with <code>state set present</code>" in text
    assert "tells you once in one line" in text
    # the return rule, including the prompt that set afk
    assert "except the message that set or kept you afk" in text
    # nothing still calls the departure-word hook future work
    for stale in ("not part of this release", "comes later", "no effect in this release"):
        assert stale not in text, stale


def test_page_and_presence_md_agree_on_the_hook_behavior():
    md = _flat(PRESENCE_MD.read_text(encoding="utf-8"))
    for needle in (
        'a line that begins "set the user afk"',
        '"kept the user afk"',
        "`state set present`",
        "tell the user once in one line",
        "is not a return",
    ):
        assert needle in md, needle
    assert "arrives in a later one" not in md and "not part of this release" not in md
