"""Declined-findings ledger for the code-review pipeline (shared back-half).

Reviews re-run against the same change (the same CL, the same git range) re-
surface findings the author already looked at and declined -- both generic
code-review issues and md-domain audit subject-lens findings. This module is the
VCS-neutral memory that lets a re-run render those previously-declined findings
COLLAPSED instead of re-litigating them.

Home / ownership. The HOST code-review kits (git-kit, p4-kit) own change
identity, so they drive the ledger: each front-half computes a `change_id`
(the CL number / the range spec) and a `baseline` token (see below), loads the
ledger for that change, and emits `ledger_hits` in its bundle. After the review's
decision pass the skill records newly-declined findings back via
`prepare_review.py --ledger-record <json>`. Everything format-neutral -- key
normalization, baseline invalidation, storage -- lives here so both kits behave
identically, exactly like the rest of the pipeline (chunking, CLAUDE.md walk).

Key. Deliberately aligned with skills-kit's own attribution matching: a finding
is keyed by criterion/reason + taxonomy + a NORMALIZED anchor, never by line
numbers or exact wording (both churn on trivial edits). Concretely:
    md-domain audit finding: (file, criterion, taxonomy, normalized-message-anchor)
    code-review issue: (file, reason, normalized-description-anchor)
The normalized anchor is the lowercased first N significant (alphanumeric)
tokens of the message/description -- see `normalize_anchor`.

Normalization LIMITS (documented on purpose). The anchor is a lossy fingerprint,
so two DISTINCT findings that share a file + criterion/reason and whose messages
open with the same N tokens collapse to one key (false merge); conversely a
finding reworded in its FIRST N tokens gets a new key and re-surfaces (false
miss). N is a deliberate tradeoff -- large enough that boilerplate openers do not
collude, small enough that a trailing-clause edit does not defeat the match. The
ledger is advisory memory, not a correctness gate, so both failure modes degrade
to "asked once more" / "not re-asked once", never to a wrong review verdict.

Baseline (staleness). Each entry records the change's `baseline` token at the
time it was declined. On a later run the front-half recomputes the current
baseline; an entry whose stored baseline differs is STALE -- the change's content
moved underneath it, so the finding re-surfaces rather than staying collapsed.
    git: the range base SHA (`git rev-parse <base>`).
    p4:  a hash over the CL's shelf fingerprint + per-file (rev, action) map.
When the baseline changes the entry is invalidated; `record_declined` prunes
stale entries so the file does not grow without bound.

SERIOUS never collapses. Mirrors skills-kit's reducer rule that a SERIOUS
md-domain audit finding always survives: such findings are NEVER written to the
ledger (so they can never produce a hit and are always re-asked). The skill's
rendering also treats a SERIOUS finding as non-collapsible belt-and-braces.

Storage. A single JSON file in the plugin's version-independent data dir, a
sibling of the per-change `reviews/<id>/` bundle dirs (e.g.
~/.claude/plugins/data/plugins-kit/p4-kit/reviews/ledger.json). Never in the
user's repo working tree. Shape:
    {"version": 1, "changes": {"<change_id>": {"entries": [<entry>, ...]}}}
Each entry: {key, kind, file, verdict:"declined", baseline, timestamp, label,
and (md_audit only) severity}.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional

LEDGER_VERSION = 1

# Significant tokens kept from a message/description to form the anchor. 8 is
# long enough that shared boilerplate openers ("the value should be ...") do not
# collide across genuinely different findings, short enough that a trailing edit
# to the sentence does not defeat the match.
ANCHOR_TOKENS = 8

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _low(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def normalize_file(path: Optional[str]) -> str:
    """Fold a file path to a stable matching form (posix slashes, lowercased).

    Lowercasing is intentional: on Windows the same file reaches the ledger with
    different drive-letter / component casing depending on the VCS command that
    produced it, and case-insensitive filesystems make two spellings the same
    file. On case-sensitive systems this is a mild over-normalization (two files
    differing only by case share a key) -- acceptable for advisory memory.
    """
    return (path or "").replace("\\", "/").lower()


def normalize_anchor(text: Optional[str], n: int = ANCHOR_TOKENS) -> str:
    """Lowercased first `n` significant tokens of `text`.

    Punctuation, casing, and word-spacing are discarded so cosmetic edits to a
    finding's wording do not change its key. Pure-DIGIT tokens are dropped too:
    an embedded line number ("null deref at line 42") is exactly the volatile
    detail the key must NOT depend on, so it is excluded rather than consuming an
    anchor slot. See the module docstring for the limits of this fingerprint.
    """
    tokens = [t for t in _TOKEN_RE.findall((text or "").lower()) if not t.isdigit()]
    return " ".join(tokens[:n])


def _digest(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()[:16]


def key_for(finding: dict) -> str:
    """Stable ledger key for a declined finding.

    `finding` carries `kind` ("md_audit" | "code_review"), `file`, and the
    kind-specific fields the key is built from. Raises ValueError on an unknown
    kind so a malformed record payload fails loudly rather than silently keying
    everything the same.
    """
    kind = finding.get("kind")
    file = normalize_file(finding.get("file"))
    if kind == "md_audit":
        anchor = normalize_anchor(finding.get("message"))
        return _digest(
            "md_audit", file, _low(finding.get("criterion")),
            _low(finding.get("taxonomy")), anchor,
        )
    if kind == "code_review":
        anchor = normalize_anchor(finding.get("description"))
        return _digest("code_review", file, _low(finding.get("reason")), anchor)
    raise ValueError(f"unknown ledger finding kind: {kind!r}")


def label_for(finding: dict) -> str:
    """Short human label used when rendering a collapsed entry.

    Prefers an explicit `label`; otherwise a trimmed one-liner from the finding's
    message/description. Kept short -- it is shown inline in the
    'previously declined (N): <labels>' note.
    """
    explicit = (finding.get("label") or "").strip()
    if explicit:
        return explicit[:80]
    text = (finding.get("message") or finding.get("description") or "").strip()
    text = " ".join(text.split())
    return text[:80] if text else key_for(finding)


def is_serious(finding: dict) -> bool:
    """True for a SERIOUS md-domain audit finding (never laddered into memory)."""
    return finding.get("kind") == "md_audit" and _low(finding.get("severity")) == "serious"


def baseline_token(payload) -> str:
    """Deterministic baseline token from an arbitrary JSON-able payload.

    Used by the p4 front-half to fold the CL's shelf fingerprint + per-file
    (rev, action) map into one string; git passes its resolved base SHA straight
    through as the baseline and does not need this.
    """
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def _entry_for(finding: dict, baseline: str) -> dict:
    entry = {
        "key": key_for(finding),
        "kind": finding.get("kind"),
        "file": normalize_file(finding.get("file")),
        "verdict": "declined",
        "baseline": baseline,
        "timestamp": time.time(),
        "label": label_for(finding),
    }
    if finding.get("kind") == "md_audit":
        entry["severity"] = _low(finding.get("severity"))
    return entry


def default_ledger() -> dict:
    return {"version": LEDGER_VERSION, "changes": {}}


def load_ledger(ledger_file: Path) -> dict:
    """Read the ledger JSON, tolerating absence / corruption with a fresh doc."""
    try:
        raw = ledger_file.read_text(encoding="utf-8")
    except OSError:
        return default_ledger()
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return default_ledger()
    if not isinstance(doc, dict) or not isinstance(doc.get("changes"), dict):
        return default_ledger()
    doc.setdefault("version", LEDGER_VERSION)
    return doc


def save_ledger(ledger_file: Path, ledger: dict) -> None:
    """Atomically write the ledger JSON (temp file + replace)."""
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger_file.with_suffix(ledger_file.suffix + ".tmp")
    tmp.write_text(
        json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    tmp.replace(ledger_file)


def ledger_hits(ledger_file: Path, change_id: str, current_baseline: str) -> list[dict]:
    """Return the entries for `change_id` whose baseline still matches.

    An entry whose stored baseline differs from `current_baseline` is stale (the
    change moved) and is omitted, so a re-surfaced finding is decided again.
    Read-only -- pruning of stale entries happens in `record_declined`.
    """
    ledger = load_ledger(ledger_file)
    change = ledger.get("changes", {}).get(str(change_id))
    if not change:
        return []
    return [
        e for e in change.get("entries", [])
        if e.get("baseline") == current_baseline
    ]


def record_declined(
    ledger_file: Path,
    change_id: str,
    current_baseline: str,
    findings: list[dict],
) -> int:
    """Persist newly-declined findings for `change_id`; return the count stored.

    - SERIOUS md-domain audit findings are dropped (never collapsed on a later run).
    - Stale entries for this change (baseline != current) are pruned so the file
      does not accumulate dead keys across an evolving change.
    - New entries are deduplicated against surviving ones by key.
    """
    ledger = load_ledger(ledger_file)
    changes = ledger.setdefault("changes", {})
    bucket = changes.setdefault(str(change_id), {})
    existing = [
        e for e in bucket.get("entries", [])
        if e.get("baseline") == current_baseline
    ]
    by_key = {e["key"]: e for e in existing if e.get("key")}

    stored = 0
    for finding in findings:
        if is_serious(finding):
            continue
        entry = _entry_for(finding, current_baseline)
        if entry["key"] in by_key:
            continue
        by_key[entry["key"]] = entry
        stored += 1

    bucket["entries"] = list(by_key.values())
    if not bucket["entries"]:
        # Nothing survives for this change -- drop the empty bucket.
        changes.pop(str(change_id), None)
    save_ledger(ledger_file, ledger)
    return stored


def record_from_file(ledger_file: Path, json_path: Path) -> int:
    """`--ledger-record` back-end: read a declined-findings JSON and record it.

    Payload shape:
        {"change_id": "<id>", "baseline": "<token>",
         "declined": [ {kind, file, ...}, ... ]}
    Deterministic -- the caller (skill) writes the JSON, this never asks the
    model to hand-edit the ledger. Returns the number of entries stored.
    """
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    change_id = payload.get("change_id")
    baseline = payload.get("baseline")
    declined = payload.get("declined") or []
    if not change_id or baseline is None:
        raise ValueError(
            "--ledger-record payload needs 'change_id' and 'baseline'"
        )
    return record_declined(ledger_file, change_id, baseline, declined)
