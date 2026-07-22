"""Tests for sha256 verification of the standalone Python tarball download.

session-bootstrap.sh downloads a pinned indygreg python-build-standalone
CPython when no usable Python exists. Because PY_VERSION/RELEASE_TAG are pinned
in the script, the correct sha256 per platform triple is known ahead of time
and pinned alongside them. A verification failure must DISCARD the tarball and
abort the install (no extraction), not proceed.

These are static-analysis tests (same style as test_session_bootstrap_paths.py):
they assert the pinned hashes are exactly the known-good values, that the
checksum is verified BEFORE extraction, and that a mismatch aborts rather than
falling through to `tar`.
"""

import re
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent
    / "plugins"
    / "bootstrap"
    / "hooks"
    / "sessionstart"
    / "session-bootstrap.sh"
)

# Known-good sha256 for cpython-3.12.9+20250317-<triple>-install_only_stripped.tar.gz,
# taken from the release's per-archive .sha256 sidecar files. If PY_VERSION or
# RELEASE_TAG in the script changes, these must be refreshed too.
EXPECTED_HASHES = {
    "x86_64-pc-windows-msvc": "ee338839315bdd8af5fc935f9595eca20ebebdd250726c5816b2d0cf94d1e661",
    "x86_64-unknown-linux-gnu": "a36bc60c38fe146e908e2e71fc21266c8558b24a9407226b1d887212839437ef",
    "aarch64-unknown-linux-gnu": "0354f70e7d3e2d0c36308edc1815c563d9bae1a3221830f7e222f6bb0a7e1a3a",
    "x86_64-apple-darwin": "1a414bf392a7afe08c742502a82edd41893a1144ccbceb184dc5ee6ee9c069c0",
    "aarch64-apple-darwin": "0a4647b7df3c8eca11071d6cea68a14a4b102bd6fc6afae314e9852510654b7d",
}


class TestPinnedHashes:
    """Every supported triple has the correct pinned sha256 in the script."""

    def test_pinned_version_still_matches_hashes(self) -> None:
        content = SCRIPT_PATH.read_text()
        assert 'PY_VERSION="3.12.9"' in content, (
            "PY_VERSION changed; the pinned sha256 values in the script AND in "
            "this test's EXPECTED_HASHES must be refreshed from the release."
        )
        assert 'RELEASE_TAG="20250317"' in content, (
            "RELEASE_TAG changed; refresh the pinned sha256 values."
        )

    def test_each_triple_pins_correct_hash(self) -> None:
        content = SCRIPT_PATH.read_text()
        for triple, expected in EXPECTED_HASHES.items():
            m = re.search(
                re.escape(triple) + r'\)\s*EXPECTED_SHA256="([0-9a-fA-F]{64})"',
                content,
            )
            assert m, f"No pinned EXPECTED_SHA256 found for triple {triple}"
            assert m.group(1).lower() == expected, (
                f"Pinned sha256 for {triple} is {m.group(1)}, expected {expected}"
            )


class TestVerificationAborts:
    """A checksum mismatch must abort before extraction, not proceed."""

    def test_checksum_verified_before_extraction(self) -> None:
        content = SCRIPT_PATH.read_text()
        verify_pos = content.find("EXPECTED_SHA256")
        # The tar extraction that installs the runtime.
        extract_pos = content.find('tar xzf "$_dl_tmp" -C "$STANDALONE_DIR"')
        assert verify_pos != -1, "verification block not found"
        assert extract_pos != -1, "extraction call not found"
        assert verify_pos < extract_pos, (
            "sha256 verification must appear BEFORE tar extraction so a bad "
            "tarball is never extracted."
        )

    def test_mismatch_branch_discards_and_exits(self) -> None:
        content = SCRIPT_PATH.read_text()
        # Isolate the mismatch branch: from the comparison to the else that
        # logs success.
        m = re.search(
            r'\[ "\$_actual_sha256" != "\$EXPECTED_SHA256" \];\s*then(.*?)\belse\b',
            content,
            re.DOTALL,
        )
        assert m, "checksum-mismatch branch not found"
        branch = m.group(1)
        assert 'rm -f "$_dl_tmp"' in branch, (
            "mismatch branch must discard the downloaded tarball"
        )
        assert "checksum mismatch" in branch, (
            "mismatch branch must log the failure"
        )
        assert "exit 0" in branch, (
            "mismatch branch must abort (exit) rather than fall through to tar"
        )
