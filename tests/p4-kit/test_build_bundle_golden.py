"""Golden characterization of build_bundle across its phases.

Each scenario drives build_bundle with faked P4 collaborators and records
everything observable: the collaborator call log (order, arguments, count),
the arguments build_bundle hands to assemble_bundle (files, path effects,
snapshot seed), the returned bundle, stderr, and any refusal. The recording
is compared to tests/p4-kit/golden/build_bundle.json, which was captured from
build_bundle before it was split into phase functions.

Regenerate ONLY for an intended behaviour change:
    P4_KIT_UPDATE_GOLDEN=1 uv run --extra dev pytest tests/p4-kit/test_build_bundle_golden.py
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import prepare_review as pr

GOLDEN = Path(__file__).parent / "golden" / "build_bundle.json"

GUIDE = "//depot/guide.md"
NEW_MD = "//depot/new.md"
APY = "//depot/a.py"
OLD = "//depot/old.py"

LOCAL_FILES = {
    GUIDE: ("guide.md", "intro\nnew\n"),
    NEW_MD: ("new.md", "hello\n"),
    APY: ("a.py", "x = 2\n"),
    OLD: ("old.py", None),
}


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().upper()


def _describe(*, pending: bool, client: str = "client", section: str = "Shelved files") -> str:
    status = " *pending*" if pending else ""
    return (
        f"Change 77 by author@{client} on 2026/01/01 12:00:00{status}\n\n"
        "\tRefactor the guide\n\n"
        f"{section} ...\n\n"
        f"... {GUIDE}#3 edit\n"
        f"... {NEW_MD}#1 add\n"
        f"... {APY}#5 edit\n"
        f"... {OLD}#2 delete\n\n"
        "Differences ...\n\n"
        f"==== {GUIDE}#3 (text) ====\n\n"
        "@@ -1,2 +1,2 @@\n intro\n-old\n+new\n"
        f"==== {NEW_MD}#1 (text) ====\n\n"
        "@@ -0,0 +1 @@\n+hello\n"
        f"==== {APY}#5 (text) ====\n\n"
        "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        f"==== {OLD}#2 (text) ====\n\n"
        "@@ -1 +0,0 @@\n-gone\n"
    )


STABLE_DIGESTS = {GUIDE: _md5("intro\nnew\n"), NEW_MD: _md5("hello\n"), APY: _md5("x = 2\n"), OLD: ""}
ACTIONS = {GUIDE: "edit", NEW_MD: "add", APY: "edit", OLD: "delete"}
STABLE = {"digests": STABLE_DIGESTS, "actions": ACTIONS}
EMPTY = {"digests": {}, "actions": {}}
CHANGED = {"digests": dict(STABLE_DIGESTS, **{APY: "FFFF"}), "actions": ACTIONS}
SCAN_FAILED = {"digests": {}, "actions": {}, "scan_ok": False, "scan_reason": "fstat refused"}
OPENED = dict(ACTIONS)

PENDING = ("describe", _describe(pending=True), True)
SUBMITTED = ("describe", _describe(pending=False, section="Affected files"), False)
FOREIGN = ("describe", _describe(pending=True, client="author-ws"), True)
UNSHELVED = ("raise_unshelved",)

BASE: dict[str, Any] = {
    "describes": [PENDING],
    "fingerprints": [STABLE],
    "opened": (OPENED, []),
    "auto_shelve": STABLE,
    "pre_images": {GUIDE: "intro\nold\n", NEW_MD: "", APY: "x = 1\n", OLD: "gone\n"},
    "content": {GUIDE: "intro\nnew\n", NEW_MD: "hello\n"},
    "workspace": True,
    "client": "client",
    "unreconciled": ([], []),
    "default_open": ([], []),
    "unresolved": ([], []),
    "kwargs": {},
}


def _scenario(**overrides: Any) -> dict[str, Any]:
    return {**BASE, **overrides}


SCENARIOS: dict[str, dict[str, Any]] = {
    "submitted_local": _scenario(describes=[SUBMITTED], fingerprints=[EMPTY]),
    "submitted_with_claim_refused": _scenario(
        describes=[SUBMITTED], fingerprints=[EMPTY], kwargs={"claim_globs": ["**/*.md"]}
    ),
    "pending_seam_b_clean": _scenario(),
    "pending_seam_b_with_claims": _scenario(kwargs={"claim_globs": ["**/guide.md"]}),
    "pending_fully_claimed": _scenario(kwargs={"claim_globs": ["**/*.md", "**/*.py"]}),
    "pending_review_machine_emitted": _scenario(kwargs={"review_machine_emitted": True}),
    "pending_markdown_content_mismatch_retries_then_strips": _scenario(
        content={GUIDE: "intro\nsomething else\n", NEW_MD: "hello\n"}
    ),
    "pending_markdown_digest_mismatch_and_drift": _scenario(
        fingerprints=[{"digests": dict(STABLE_DIGESTS, **{GUIDE: "ABCD"}), "actions": ACTIONS}]
    ),
    "pending_markdown_fetch_fails": _scenario(content={GUIDE: None, NEW_MD: None}),
    "pending_markdown_preimage_missing": _scenario(
        pre_images={GUIDE: None, NEW_MD: "", APY: None, OLD: None}
    ),
    "pending_markdown_preimage_unrelated": _scenario(
        pre_images={GUIDE: "nothing alike\n", NEW_MD: "", APY: "x = 1\n", OLD: "gone\n"}
    ),
    "pending_fingerprint_moves_once": _scenario(fingerprints=[STABLE, CHANGED, STABLE, STABLE]),
    "pending_fingerprint_keeps_moving": _scenario(
        fingerprints=[STABLE, CHANGED, STABLE, CHANGED]
    ),
    "pending_empty_fingerprint_retries_at_top": _scenario(fingerprints=[EMPTY, STABLE]),
    "pending_shelf_scan_failed": _scenario(fingerprints=[SCAN_FAILED]),
    "pending_opened_scan_incomplete": _scenario(
        opened=({}, [{"scan": "shelf_opened", "reason": "opened refused"}])
    ),
    "pending_divergence_refused": _scenario(opened=(dict(OPENED, **{"//depot/extra.py": "edit"}), [])),
    "pending_no_workspace_root": _scenario(workspace=False),
    "pending_hygiene_findings": _scenario(
        unreconciled=(
            [
                {"depot": APY, "local": "<WS>/a.py", "action": "edit"},
                {"depot": "//depot/stray.py", "local": "<WS>/stray.py", "action": "add"},
            ],
            [],
        ),
        default_open=([{"depot": "//depot/d.py", "action": "edit"}], []),
        unresolved=([{"depot": GUIDE}], [{"scan": "unresolved", "reason": "partial"}]),
    ),
    "auto_shelve_then_stable": _scenario(
        describes=[UNSHELVED, PENDING], fingerprints=[EMPTY, EMPTY, STABLE]
    ),
    "auto_shelve_then_retry_keeps_ownership": _scenario(
        describes=[UNSHELVED, PENDING], fingerprints=[EMPTY, EMPTY, CHANGED, STABLE, STABLE]
    ),
    "pre_shelve_race_found_foreign_shelf": _scenario(
        describes=[UNSHELVED, PENDING], fingerprints=[EMPTY, STABLE]
    ),
    "pre_shelve_scan_failed_refused": _scenario(
        describes=[UNSHELVED], fingerprints=[EMPTY, SCAN_FAILED]
    ),
    "foreign_client": _scenario(describes=[FOREIGN]),
    "foreign_client_review_machine_emitted": _scenario(
        describes=[FOREIGN], kwargs={"review_machine_emitted": True}
    ),
    "foreign_client_with_claim_refused": _scenario(
        describes=[FOREIGN], kwargs={"claim_globs": ["**/*.md"]}
    ),
    "pending_markdown_post_image_over_limit": _scenario(max_source_bytes=8),
    "workspace_view": _scenario(
        describes=[], fingerprints=[], workspace_view=True
    ),
}


class _Recorder:
    def __init__(self, scenario: dict[str, Any], tmp: Path) -> None:
        self.scenario = scenario
        self.tmp = tmp
        self.ws = tmp / "ws"
        self.calls: list[list[Any]] = []
        self.assembled: list[dict[str, Any]] = []
        self._describes = list(scenario["describes"])
        self._fingerprints = list(scenario["fingerprints"])

    def log(self, name: str, *args: Any) -> None:
        self.calls.append([name, *args])

    # -- collaborators -------------------------------------------------
    def fetch_describe(self, cl):
        self.log("fetch_describe", cl)
        item = self._describes.pop(0) if len(self._describes) > 1 else self._describes[0]
        if item[0] == "raise_unshelved":
            raise pr.PendingUnshelvedError(f"pending CL {cl} has no shelved content to review")
        return item[1], item[2]

    def fetch_shelf_fingerprint(self, cl):
        self.log("fetch_shelf_fingerprint", cl)
        item = self._fingerprints.pop(0) if len(self._fingerprints) > 1 else self._fingerprints[0]
        return pr.ShelfScanResult(**item)

    def auto_shelve_cl(self, cl):
        self.log("auto_shelve_cl", cl)
        return pr.ShelfScanResult(**self.scenario["auto_shelve"])

    def fetch_opened_files(self, cl):
        self.log("fetch_opened_files", cl)
        opened, incomplete = self.scenario["opened"]
        return dict(opened), list(incomplete)

    def get_workspace_root(self):
        self.log("get_workspace_root")
        if not self.scenario["workspace"]:
            return None, self.scenario["client"]
        return self.ws, self.scenario["client"]

    def resolve_local_paths(self, depots):
        self.log("resolve_local_paths", list(depots))
        return {
            d: str(self.ws / LOCAL_FILES[d][0]) if d in LOCAL_FILES else None for d in depots
        }

    def materialize_preimage(self, depot, action, bundle_dir, base_revision):
        self.log("materialize_preimage", depot, action, base_revision)
        text = self.scenario["pre_images"].get(depot)
        if text is None:
            return None
        dest = Path(bundle_dir) / "pre" / LOCAL_FILES[depot][0]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        return str(dest)

    def fetch_file_content(self, depot, revision, cl, is_shelved=False, is_delete=False):
        self.log("fetch_file_content", depot, revision, cl, is_shelved, is_delete)
        return self.scenario["content"].get(depot)

    def find_unreconciled(self, dirs):
        self.log("find_unreconciled", list(dirs))
        found, incomplete = self.scenario["unreconciled"]
        return [
            {k: v.replace("<WS>", str(self.ws)) if isinstance(v, str) else v for k, v in e.items()}
            for e in found
        ], list(incomplete)

    def find_default_open(self, dirs):
        self.log("find_default_open", list(dirs))
        found, incomplete = self.scenario["default_open"]
        return list(found), list(incomplete)

    def find_unresolved(self, cl):
        self.log("find_unresolved", cl)
        found, incomplete = self.scenario["unresolved"]
        return list(found), list(incomplete)

    def run_p4(self, args):
        self.log("run_p4", list(args))
        return 1, "", "no such file(s)"

    def assemble_bundle(self, real):
        def wrapper(**kwargs):
            reader = kwargs.get("snapshot_reader")
            self.assembled.append(
                {
                    "files": kwargs["files"],
                    "claim_globs": kwargs["claim_globs"],
                    "review_generated": kwargs["review_generated"],
                    "snapshot_seed": kwargs["snapshot_seed"],
                    "path_effects": [_effect(e) for e in kwargs["path_effects"]],
                    "snapshot_reader": type(reader).__name__ if reader is not None else None,
                    "mechanical_contract": kwargs["mechanical_contract"],
                    "workspace_root": str(kwargs["workspace_root"]),
                }
            )
            return real(**kwargs)

        return wrapper


def _effect(effect) -> dict[str, Any]:
    out = dataclasses.asdict(effect)
    if out["post_image"] is not None:
        out["post_image"] = out["post_image"].decode("utf-8")
    return out


def _workspace_view() -> pr.WorkspaceView:
    return pr.WorkspaceView(
        describe=_describe(pending=True, section="Affected files"),
        digests=dict(STABLE_DIGESTS),
        actions=dict(ACTIONS),
    )


def run_scenario(name: str, tmp: Path) -> dict[str, Any]:
    scenario = SCENARIOS[name]
    rec = _Recorder(scenario, tmp)
    rec.ws.mkdir(parents=True)
    (rec.ws / "CLAUDE.md").write_text("workspace rule\n", encoding="utf-8")
    for _depot, (rel, text) in LOCAL_FILES.items():
        if text is not None:
            (rec.ws / rel).write_text(text, encoding="utf-8")
    kwargs = dict(scenario["kwargs"])
    if scenario.get("workspace_view"):
        kwargs["workspace_view"] = _workspace_view()
    stderr = io.StringIO()
    result: dict[str, Any] = {}
    patches = [
        patch.object(pr, n, side_effect=getattr(rec, n))
        for n in (
            "fetch_describe", "fetch_shelf_fingerprint", "auto_shelve_cl", "fetch_opened_files",
            "get_workspace_root", "resolve_local_paths", "materialize_preimage",
            "fetch_file_content", "find_unreconciled", "find_default_open", "find_unresolved",
            "run_p4",
        )
    ]
    if "max_source_bytes" in scenario:
        patches.append(patch.object(pr, "MAX_SOURCE_BYTES", scenario["max_source_bytes"]))
    patches.append(patch.object(pr, "assemble_bundle", side_effect=rec.assemble_bundle(pr.assemble_bundle)))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(contextlib.redirect_stderr(stderr))
        try:
            result["bundle"] = pr.build_bundle(
                "77", tmp / "bundle", ledger_path=tmp / "ledger.jsonl", **kwargs
            )
        except ValueError as exc:
            result["error"] = [type(exc).__name__, str(exc)]
    result["calls"] = rec.calls
    result["assembled"] = rec.assembled
    result["stderr"] = stderr.getvalue()
    text = json.dumps(result, sort_keys=True, indent=1, default=str)
    for prefix in sorted({str(tmp.resolve()), str(tmp)}, key=len, reverse=True):
        text = text.replace(prefix, "<TMP>")
    return json.loads(text)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_build_bundle_matches_golden(name, tmp_path):
    actual = run_scenario(name, tmp_path)
    if os.environ.get("P4_KIT_UPDATE_GOLDEN") == "1":
        golden = json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.is_file() else {}
        golden[name] = actual
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(golden, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        return
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert name in golden, f"no golden recording for {name}"
    assert actual == golden[name]


def test_every_golden_scenario_still_exists():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert sorted(golden) == sorted(SCENARIOS)


def test_golden_recordings_are_substantive():
    """Guard against a golden that would pass if build_bundle returned nothing."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for name, rec in golden.items():
        assert rec["calls"], name
        assert ("bundle" in rec) != ("error" in rec), name
        if "bundle" in rec:
            assert rec["bundle"]["changed_files"] or rec["bundle"].get("claimed_files"), name
            assert rec["assembled"], name
