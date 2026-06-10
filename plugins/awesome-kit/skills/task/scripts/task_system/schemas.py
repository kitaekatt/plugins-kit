"""Embedded-YAML schemas for the task-system typed units (spec sections 2.2, 2.4).

Both schemas are expressed in the skills_kit_lib.schema_engine rule vocabulary
(required / type / forbid_regex / items / keys) and are validated by calling
``skills_kit_lib.schema_engine.validate(yaml_data, schema)`` directly. They are
deliberately NOT registered in skills-kit's schema_registry -- the schema dicts
live here in awesome-kit because they change with the task system (CCP).

Layering: the engine checks structure / types / regex only. Vocabulary checks
that depend on the Task Type -- ``status`` within the type's state_vocabulary,
``priority`` against the type's priority pattern, a known ``_schema_version``,
a registered ``type`` name -- are post-walker checks in validate.py, the same
layering skills-kit itself uses (custom rules live outside the engine).

Schemas are floors, not ceilings: unknown keys pass (a type may add
load-bearing fields beyond this set).
"""

# task.yaml structured record, default "hand-off" type (spec 2.2).
TASK_SCHEMA: dict = {
    "root": "task",
    "keys": {
        "_schema_version": {"required": True, "type": "string"},
        "type": {"required": True, "type": "string"},
        "title": {
            "required": True,
            "type": "string",
            # The engine has no non-empty-string rule; an empty/whitespace-only
            # title is rejected via forbid_regex (re.search matches the empty
            # string at position 0 only when the whole string is blank).
            "forbid_regex": r"^\s*$",
            "msg": "title must be non-empty",
        },
        "status": {"required": True, "type": "string"},
        "priority": {"required": False, "type": "string"},
        "description": {"required": False, "type": "string"},
        # Entries are reference paths (list[path]); per-entry string-ness and
        # resolvability are checked by validate.py's dangling-entry walk (the
        # engine's `items` rule only supports dict-shaped list members).
        "depends_on": {"required": False, "type": "list"},
        "blocked_by": {"required": False, "type": "list"},
        "agent_hint": {"required": False, "type": "string"},
        "skills_to_invoke": {"required": False, "type": "list"},
    },
}

# task_list embedded reference list (spec 2.4): { refs: list[ref] }.
# refs may be empty (no min_len); each ref is { path: required, host: optional }.
TASK_LIST_SCHEMA: dict = {
    "root": "task_list",
    "keys": {
        "refs": {
            "required": True,
            "type": "list",
            "items": {
                "keys": {
                    "path": {"required": True, "type": "string"},
                    "host": {"required": False, "type": "string"},
                },
            },
        },
    },
}
