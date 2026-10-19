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
