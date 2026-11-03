"""The vendored relay-protocol/1 schemas match katsoft-libs, their canonical source.

plugins/llm-kit/channel/schemas holds the schemas the channel server speaks
(the fake relay in test_relay_channel.py validates against them). The canonical
copies live in katsoft-libs under llm_kit/relay/schemas. This is a consistency
check: it catches a copy edited by hand and a source that moved on, not a
change made to both. Skipped when no katsoft-libs checkout is present; point
KATSOFT_LIBS_DIR at one, otherwise the sibling directory of this repository is
used.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDORED = REPO_ROOT / "plugins" / "llm-kit" / "channel" / "schemas"
SOURCE_SUBPATH = Path("packages") / "katsoft-llm-kit" / "src" / "llm_kit" / "relay" / "schemas"

#: Every schema the channel exchanges: the envelopes, the shared defs and the
#: six channel-tier verbs. The set is stated here so that a file added to the
#: vendored directory without being named (or one dropped) is a failure.
EXPECTED = sorted(
    ["common.json", "envelope.request.json", "envelope.response.json"]
    + ["%s.%s.json" % (verb, side)
       for verb in ("register", "heartbeat", "deregister", "poll", "ack", "reply")
       for side in ("request", "response")]
)


def _source_dir() -> Path:
    configured = os.environ.get("KATSOFT_LIBS_DIR")
    root = Path(configured) if configured else REPO_ROOT.parent / "katsoft-libs"
    return root / SOURCE_SUBPATH


def test_vendored_set_is_exactly_the_expected_schemas():
    assert sorted(p.name for p in VENDORED.glob("*.json")) == EXPECTED


@pytest.mark.parametrize("name", EXPECTED)
def test_vendored_schema_equals_katsoft_libs_source(name):
    source = _source_dir() / name
    if not source.parent.is_dir():
        pytest.skip("no katsoft-libs checkout at %s (set KATSOFT_LIBS_DIR)" % source.parent)
    assert source.is_file(), "%s is vendored but katsoft-libs has no such schema" % name
    assert (VENDORED / name).read_bytes() == source.read_bytes(), (
        "%s differs from katsoft-libs; copy the source over it" % name)
