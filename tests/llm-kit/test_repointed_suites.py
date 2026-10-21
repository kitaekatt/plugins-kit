"""The old-versus-copy differential for the three legacy suites.

The legacy suites run in the main suite (``tests/llm-scripting-kit``,
``tests/content-pipeline-kit``, ``tests/job-kit``), so this module does not run
them again. It runs each suite's re-pointed twin from the ignored mirror
``tmp/<suite>`` in chunks (so xdist spreads them over its workers) and compares
the mirror with the legacy suite's collected node ids:

* the mirrored files collect the same node ids as their legacy originals;
* every mirror outcome is a pass, or the difference the suite's ledger declares;
* a mirror skip the ledger does not declare must also be a skip in the legacy
  suite (that one legacy test is run to find out);
* the legacy suite collects the totals and per-file counts the ledger records.

The legacy outcomes are not compared test by test: the ledger records no
baseline failure, so the main suite's own green run is the legacy side. The full
legacy-versus-mirror comparison stays available on demand:
``uv run --extra dev python tests/llm-kit/repoint.py compare <suite>``.

The mirror process puts the generated ``tmp/sitecustomize.py`` on every
interpreter it starts, so a legacy library on any import root fails the run
instead of being compared.
"""

from __future__ import annotations

import pytest

import repoint

CHUNKS = [(suite, index, files)
          for suite in repoint.SUITES
          for index, files in enumerate(repoint.mirror_chunks(repoint.load_ledger(suite)))]


@pytest.mark.parametrize("suite", repoint.SUITES)
def test_legacy_collection_matches_the_ledger(suite):
    ledger = repoint.load_ledger(suite)
    legacy = repoint.collect_legacy_ids(suite)
    assert len(legacy) == repoint.SUITE_TOTALS[suite], (
        f"legacy {suite} collected {len(legacy)} tests, ledger totals "
        f"{repoint.SUITE_TOTALS[suite]}")
    per_file: dict[str, int] = {}
    for node in legacy:
        file_name = node.split("::", 1)[0].split("/", 1)[1]
        per_file[file_name] = per_file.get(file_name, 0) + 1
    recorded = {r["path"].rsplit("/", 1)[1]: r["collection_count"] for r in ledger["rows"]}
    assert per_file == recorded, "per-file collection counts drifted from the ledger"


@pytest.mark.parametrize("suite,index,files", CHUNKS, ids=[f"{s}-{i}" for s, i, _ in CHUNKS])
def test_repointed_chunk_matches_the_legacy_suite(suite, index, files, tmp_path):
    problems, mirror = repoint.compare_mirror_chunk(suite, files, tmp_path)
    assert not problems, (
        f"{suite} chunk {index} ({len(files)} files): {len(problems)} difference(s) between "
        f"the legacy node ids and the re-pointed run ({mirror.summary}):\n"
        + "\n".join(problems[:60]))


def test_the_chunks_cover_every_mirrored_file():
    for suite in repoint.SUITES:
        ledger = repoint.load_ledger(suite)
        chunked = [f for s, _, files in CHUNKS if s == suite for f in files]
        assert sorted(chunked) == sorted(repoint.rewrite_files(ledger))


def test_the_mirror_only_comparison_reports_each_kind_of_difference(tmp_path, monkeypatch):
    out = repoint.Outcome
    ledger = {"rows": [{"path": "tests/s/test_a.py", "disposition": "rewrite", "collection_count": 4,
                        "baseline_failures": [],
                        "declared_differences": {"s/test_a.py::test_declared": {"mirror_status": "skipped"}}}]}
    legacy = ["s/test_a.py::test_ok", "s/test_a.py::test_broken", "s/test_a.py::test_declared",
              "s/test_a.py::test_gone", "s/test_a.py::test_quiet"]
    mirror = {"s/test_a.py::test_ok": out("passed"),
              "s/test_a.py::test_broken": out("failed", "AssertionError", "boom"),
              "s/test_a.py::test_declared": out("skipped"),
              "s/test_a.py::test_quiet": out("skipped"),
              "s/test_a.py::test_new": out("passed")}
    # the legacy file runs test_quiet instead of skipping it
    monkeypatch.setattr(repoint, "run_legacy_files",
                        lambda suite, files, workdir: {"s/test_a.py::test_quiet": out("passed")})
    problems = repoint.compare_mirror_only("s", ["test_a.py"], legacy, mirror, ledger, tmp_path)
    assert sorted(p.split("\n")[0] for p in problems) == sorted([
        "collected by legacy, absent from the mirror: s/test_a.py::test_gone",
        "collected only by the mirror: s/test_a.py::test_new",
        "mirror failed, legacy passes: s/test_a.py::test_broken",
        "skip differs: s/test_a.py::test_quiet: mirror skipped, legacy passed"])
