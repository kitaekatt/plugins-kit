"""Schema-DSL engine.

Walks a parsed YAML payload against a schema dict and emits per-path
pass/fail verdicts. Schemas are Python dicts using a small rule vocabulary
(no external schema language). The validator is intentionally minimal --
schemas are floors, not ceilings: required keys / type / list-length /
regex constraints are checked, unknown keys are permitted unless they
appear on the schema's forbidden_keys list (mixed-type drift signal).

Rule vocabulary:

- {"required": True/False}                 -- key required vs optional
- {"type": "string"|"list"|"dict"|"int"|"bool"}
- {"min_len": N} / {"max_len": N}          -- list-length bounds
- {"forbid_regex": "<pat>", "msg": "..."}  -- string must not match
- {"items": <subschema>}                   -- each list item matches subschema
- {"keys": {<key>: <rule>, ...}}           -- dict has these sub-keys
- {"value_schema": <subschema>}            -- every value in a dict (arbitrary keys) matches subschema
- {"forbidden_keys": [<key>, ...]}         -- on root schema; presence is drift
- {"root_type": "list"|"dict"}             -- on root schema; list-rooted units

Each rule may also carry "note" with explanatory text shown in audit output.
"""

import re as _re


def _typecheck(value, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "list":
        return isinstance(value, list)
    if expected_type == "dict":
        return isinstance(value, dict)
    if expected_type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "bool":
        return isinstance(value, bool)
    return True


def _check_keys(container: dict, keys_rule: dict, base_path: str, fails: list, ok_paths: list):
    """Walk a {<key>: <rule>} map against a dict container: required keys must
    be present; present keys recurse into _validate_value."""
    for sub_key, sub_rule in keys_rule.items():
        sub_path = f"{base_path}.{sub_key}"
        present = sub_key in container
        if sub_rule.get("required") and not present:
            fails.append((sub_path, "required key missing"))
        elif present:
            _validate_value(container[sub_key], sub_rule, sub_path, fails, ok_paths)


def _check_value_schema(container: dict, value_schema: dict, base_path: str,
                        fails: list, ok_paths: list):
    """value_schema: dict of arbitrary keys, each value matches subschema.
    Used by ACTIONS_SCHEMA where keys are user-defined action names."""
    for k, v in container.items():
        sub_path = f"{base_path}.{k}"
        if not isinstance(v, dict):
            fails.append((sub_path, f"expected dict value, got {type(v).__name__}"))
            continue
        _check_keys(v, value_schema.get("keys", {}), sub_path, fails, ok_paths)


def _validate_value(value, rule: dict, path: str, fails: list, ok_paths: list):
    """Walk a single value against a rule. Append failure descriptors to fails;
    record paths checked without failure in ok_paths.
    """
    fails_before = len(fails)

    expected_type = rule.get("type")
    if expected_type and not _typecheck(value, expected_type):
        fails.append((path, f"expected {expected_type}, got {type(value).__name__}"))
        return

    min_len = rule.get("min_len")
    if min_len is not None and isinstance(value, list) and len(value) < min_len:
        fails.append((path, f"list length {len(value)} < required {min_len}"))

    max_len = rule.get("max_len")
    if max_len is not None and isinstance(value, list) and len(value) > max_len:
        fails.append((path, f"list length {len(value)} > permitted {max_len}"))

    forbid = rule.get("forbid_regex")
    if forbid and isinstance(value, str):
        m = _re.search(forbid, value, _re.IGNORECASE)
        if m:
            msg = rule.get("msg", "matched forbidden pattern")
            fails.append((path, f"{msg} (matched '{m.group(0)}')"))

    items_rule = rule.get("items")
    if items_rule and isinstance(value, list):
        for i, item in enumerate(value):
            sub_path = f"{path}[{i}]"
            if not isinstance(item, dict):
                fails.append((sub_path, f"expected dict in list item, got {type(item).__name__}"))
                continue
            _check_keys(item, items_rule.get("keys", {}), sub_path, fails, ok_paths)

    keys_rule = rule.get("keys")
    if keys_rule and isinstance(value, dict):
        _check_keys(value, keys_rule, path, fails, ok_paths)

    value_schema = rule.get("value_schema")
    if value_schema and isinstance(value, dict):
        _check_value_schema(value, value_schema, path, fails, ok_paths)

    if len(fails) == fails_before:
        ok_paths.append(path)


def validate(yaml_data: dict, schema: dict) -> tuple[list, list]:
    """Validate yaml_data against a per-type schema.

    Returns (fails, checked) where:
    - fails: list of (path, message) tuples for each failure
    - checked: list of paths that were checked without failure (informational)
    """
    fails: list = []
    checked: list = []

    root = schema["root"]
    block = yaml_data.get(root)
    if block is None:
        fails.append((root, "root key missing"))
        return fails, checked

    root_type = schema.get("root_type", "dict")
    if root_type == "list":
        if not isinstance(block, list):
            fails.append((root, f"root must be a list, got {type(block).__name__}"))
            return fails, checked
        min_len = schema.get("min_len")
        if min_len is not None and len(block) < min_len:
            fails.append((root, f"list length {len(block)} < required {min_len}"))
        items_rule = schema.get("items")
        if items_rule:
            for i, item in enumerate(block):
                _validate_value(item, items_rule, f"{root}[{i}]", fails, checked)
        return fails, checked

    if not isinstance(block, dict):
        fails.append((root, f"root must be a dict, got {type(block).__name__}"))
        return fails, checked

    # Delegate the dict-rooted walk: keys + (root-level) value_schema are the
    # same shapes _validate_value already handles.
    _validate_value(
        block,
        {"keys": schema.get("keys", {}), "value_schema": schema.get("value_schema")},
        root,
        fails,
        checked,
    )

    forbidden = schema.get("forbidden_keys", [])
    for f_key in forbidden:
        if f_key in block:
            fails.append((f"{root}.{f_key}", "forbidden key (mixed-type drift signal)"))

    return fails, checked
