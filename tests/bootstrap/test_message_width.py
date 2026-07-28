"""Enforcement: every collated message item can be rendered within ITEM_MAX.

The rule (engine-internals.md, "Collated message text") is that a collated item
is at most 40 characters and is NEVER cut off to get there -- it is shortened by
authoring a `display=` label, or by dropping a trailing clause at a separator
the author already wrote.

Those two escapes cannot cover every string automatically, so this test is the
thing that keeps the rule true: it walks the engine's own `ctx.action(...)` /
`ctx.fail(...)` calls and fails when one produces an item with no whole short
form and no authored label. The fix is always at the call site (add
`display="..."`), never here and never by truncating.
"""

import ast
import pathlib

import pytest

from bootstrap_lib.messages import ITEM_MAX, derive_short

BOOTSTRAP_LIB = pathlib.Path(__file__).resolve().parents[2] / "plugins" / "bootstrap" / "bootstrap_lib"

#: Stand-in for an f-string's runtime values. Optimistic (assumes each
#: interpolated value is short), which is the right bias: this test is here to
#: catch statically over-long TEXT, not to model every possible tool name.
PLACEHOLDER = "<X>"


def _render(node):
    """Approximate a str/f-string node's runtime text, or None if not static."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(part.value) if isinstance(part, ast.Constant) else PLACEHOLDER
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def collated_items():
    """(file, line, text) for every statically-renderable collated item that
    does NOT carry an authored display label."""
    found = []
    for path in sorted(BOOTSTRAP_LIB.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in ("action", "fail", "append_rich"):
                continue
            if not node.args:
                continue
            if any(kw.arg == "display" for kw in node.keywords):
                continue  # authored label: exempt by construction
            text = _render(node.args[0])
            if text is not None:
                found.append((path.name, node.args[0].lineno, text.strip()))
    return found


def test_the_scan_finds_call_sites():
    """Guard against the audit silently passing because it matched nothing."""
    assert len(collated_items()) > 50


def test_every_collated_item_has_a_whole_short_form():
    offenders = [
        (name, line, text)
        for name, line, text in collated_items()
        if len(text) > ITEM_MAX and derive_short(text, ITEM_MAX) is None
    ]
    assert not offenders, (
        "These entries cannot be rendered within "
        f"{ITEM_MAX} characters without cutting them off. Add a short "
        "`display=\"...\"` label at each call site -- do NOT shorten the entry "
        "text itself (the log and the pass record want it whole), and do NOT "
        "truncate:\n"
        + "\n".join(f"  {n}:{ln} ({len(t)} chars) {t}" for n, ln, t in offenders)
    )


@pytest.mark.parametrize("text,expected", [
    ("uv: FAILED - install attempted but uv not found", "uv: FAILED"),
    ("marketplace x: added (https://example.com/very/long/url)", "marketplace x: added"),
])
def test_separator_derivation_covers_the_common_entry_shape(text, expected):
    """Why only 9 sites needed an authored label: this repo's entries almost
    all read "<subject>: <verdict> - <explanation>", and dropping the
    explanation leaves a whole clause that still names the subject."""
    assert derive_short(text, ITEM_MAX) == expected
