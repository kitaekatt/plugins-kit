"""Tests for bootstrap_lib.code_review.kit_scripts.

The helpers git-kit's and p4-kit's prepare_review.py shared as copies
before they moved here. Each kit's own
tests keep exercising them end to end through its scripts; these pin the
shared functions directly.
"""

from __future__ import annotations

import pytest

from bootstrap_lib.code_review import kit_scripts


# ------------------------------------------------------------- env_timeout_s


def test_env_timeout_reads_the_named_variable(monkeypatch):
    monkeypatch.setenv("KIT_SCRIPTS_TEST_TIMEOUT_S", "5")
    assert kit_scripts.env_timeout_s("KIT_SCRIPTS_TEST_TIMEOUT_S", 60.0) == 5.0


def test_env_timeout_accepts_a_fractional_value(monkeypatch):
    monkeypatch.setenv("KIT_SCRIPTS_TEST_TIMEOUT_S", "2.5")
    assert kit_scripts.env_timeout_s("KIT_SCRIPTS_TEST_TIMEOUT_S", 60.0) == 2.5


@pytest.mark.parametrize("raw", [None, "", "not-a-number"])
def test_env_timeout_falls_back_to_the_default(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("KIT_SCRIPTS_TEST_TIMEOUT_S", raising=False)
    else:
        monkeypatch.setenv("KIT_SCRIPTS_TEST_TIMEOUT_S", raw)
    assert kit_scripts.env_timeout_s("KIT_SCRIPTS_TEST_TIMEOUT_S", 42.0) == 42.0


# --------------------------------------------------------- parse_review_args


def test_parse_review_args_plain_positional():
    assert kit_scripts.parse_review_args(["main..HEAD"]) == (["main..HEAD"], [], False)


def test_parse_review_args_claims_both_spellings():
    assert kit_scripts.parse_review_args(
        ["123", "--claim", "**/*.md", "--claim=**/CLAUDE.md"]
    ) == (["123"], ["**/*.md", "**/CLAUDE.md"], False)


def test_parse_review_args_review_machine_emitted_flag():
    assert kit_scripts.parse_review_args(["--staged", "--review-machine-emitted"]) == (
        ["--staged"],
        [],
        True,
    )


def test_parse_review_args_claim_without_value_raises():
    with pytest.raises(ValueError, match="--claim requires a glob argument"):
        kit_scripts.parse_review_args(["123", "--claim"])
