"""Fenced-YAML-block extraction from markdown documents.

A document may contain multiple fenced yaml blocks; each block may carry
multiple top-level keys; each recognized typed-unit root key is one unit. The
walker returns one entry per recognized unit across the whole document.
"""

import re

from .schema_registry import SCHEMAS_BY_ROOT, SKILL_TYPE_ROOTS

try:
    import yaml as _pyyaml
    HAVE_YAML = True
except ImportError:
    _pyyaml = None
    HAVE_YAML = False


_YAML_BLOCK_RE = re.compile(r"^```ya?ml\s*\n(.*?)^```", re.MULTILINE | re.DOTALL)

# Public alias: consumers that need block SPANS (match.start/end of group 1),
# not just block text, may finditer this directly. Same compiled object the
# iter_yaml_blocks generator uses, so span semantics can never diverge from
# the canonical block recognition.
YAML_BLOCK_RE = _YAML_BLOCK_RE


CONTRACT_ROOT_KEYS = SKILL_TYPE_ROOTS + ("claude_md",)


def iter_yaml_blocks(body_text: str):
    """Yield each fenced YAML block's text as a string.

    Pure iteration -- no parsing, no recognition. Use collect_yaml_units when
    you need parsed + registered units.
    """
    for m in _YAML_BLOCK_RE.finditer(body_text):
        yield m.group(1)


def safe_load_block(block_text: str) -> dict | None:
    """Parse one fenced block's text as YAML; return the dict or None.

    None means: pyyaml unavailable, the YAML failed to parse, or the payload
    is not a mapping. Callers that need to distinguish "no parser" check
    HAVE_YAML themselves.
    """
    if not HAVE_YAML:
        return None
    try:
        data = _pyyaml.safe_load(block_text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def collect_yaml_units(
    body_text: str,
) -> tuple[list[tuple[str, dict]], str | None, tuple[str, str] | None]:
    """Walk all fenced yaml blocks; collect (unit_root, block_data) for every
    recognized root key across all blocks.

    Returns:
        (units, detected_root_no_parser, parse_error) where:
        - units is a list of (unit_root, block_data) tuples. Empty if no blocks
          parsed or no recognized units found.
        - detected_root_no_parser is set only when pyyaml is missing AND a
          contract root key was detected by regex; the audit knows a contract
          is staged but cannot validate.
        - parse_error is (root_key, message) when a fenced yaml block textually
          contains a recognized contract root key but pyyaml raised while
          parsing it (a real YAML syntax error in the block, e.g. an unquoted
          special character inside a flow sequence). Distinguishes "a block is
          present but broken" from "no block at all" -- previously a parse
          exception was swallowed silently (`except Exception: continue`) and
          surfaced as the misleading "no fenced yaml block found", which was
          once mistaken for a CRLF/line-ending intolerance in the fence regex.
          The fence regex and pyyaml both already tolerate CRLF fine; a swallowed
          parse error was the actual mechanism.
    """
    units: list[tuple[str, dict]] = []
    detected_root_no_parser: str | None = None
    parse_error: tuple[str, str] | None = None

    if HAVE_YAML:
        for text in iter_yaml_blocks(body_text):
            try:
                data = _pyyaml.safe_load(text)
            except Exception as exc:
                if parse_error is None:
                    for key in CONTRACT_ROOT_KEYS:
                        if re.search(rf"^{key}\s*:", text, re.MULTILINE):
                            parse_error = (key, str(exc))
                            break
                continue
            if isinstance(data, dict):
                for key in data.keys():
                    if key in SCHEMAS_BY_ROOT:
                        units.append((key, data))
        return units, None, parse_error

    # pyyaml missing -- detect a contract root key by regex inside any yaml fence
    for text in iter_yaml_blocks(body_text):
        for key in CONTRACT_ROOT_KEYS:
            if re.search(rf"^{key}\s*:", text, re.MULTILINE):
                detected_root_no_parser = key
                break
        if detected_root_no_parser:
            break
    return [], detected_root_no_parser, None


def extract_skill_type_unit(body_text: str) -> tuple[dict | None, str, str | None]:
    """Return (parsed_dict, err, root) for the skill-type contract unit if present.

    Used by audit when the skill-type contract unit needs to be located distinct
    from portable units. Returns the first matching skill-type (or claude_md)
    unit's full block_data.
    """
    units, detected_root_no_parser, parse_error = collect_yaml_units(body_text)
    if units:
        for root, data in units:
            if root in CONTRACT_ROOT_KEYS:
                return data, "", root
        return None, "no-contract-yaml-block", None
    if parse_error is not None:
        root, message = parse_error
        return None, f"yaml-parse-error: {message}", root
    if detected_root_no_parser:
        return None, "no-yaml-parser", detected_root_no_parser
    if HAVE_YAML:
        return None, "no-contract-yaml-block", None
    return None, "no-yaml-parser-no-block", None
