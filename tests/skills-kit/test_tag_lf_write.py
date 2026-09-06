"""I3: tag.py must write LF regardless of host. write_text with no newline=
translates "\\n" to os.linesep on open(mode="w"), which rewrites the whole
file to CRLF on Windows even when the source was LF. On this (POSIX) host
os.linesep is already "\\n", so a CRLF-in-bytes assertion alone would pass
even without the fix -- the load-bearing assertion is that the write call
itself passes newline="\\n" explicitly (gen_standards_doc.py's precedent),
verified by intercepting Path.write_text.
"""

from pathlib import Path
from unittest.mock import patch

from skills_kit_lib.tag import tag


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "SKILL.md"
    p.write_bytes(text.encode("utf-8"))
    return p


def test_tag_write_text_passes_explicit_lf_newline(tmp_path):
    p = _write(tmp_path, "---\nname: x\ndescription: d\n---\n# X\n")
    real_write_text = Path.write_text
    calls = []

    def _spy(self, data, *args, **kwargs):
        calls.append((args, kwargs))
        return real_write_text(self, data, *args, **kwargs)

    with patch.object(Path, "write_text", _spy):
        result = tag(p, "technique-skill", force=False, check_only=False)

    assert result["ok"], result
    assert calls, "tag() did not call Path.write_text"
    _, kwargs = calls[-1]
    assert kwargs.get("newline") == "\n", (
        f"write_text must pass newline='\\n' explicitly; got kwargs={kwargs}"
    )


def test_tag_writes_lf_bytes_no_crlf(tmp_path):
    p = _write(tmp_path, "---\nname: x\ndescription: d\n---\n# X\n")
    result = tag(p, "technique-skill", force=False, check_only=False)
    assert result["ok"], result
    raw = p.read_bytes()
    assert b"\r\n" not in raw, raw
    assert b"skill-type: technique-skill\n" in raw
