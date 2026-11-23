"""Embedded-YAML schemas for the task-system typed units (spec sections 2.2, 2.4).

Both schemas are expressed in the skills_kit_lib.schema_engine rule vocabulary
(required / type / forbid_regex / items / keys) and are validated by calling
``skills_kit_lib.schema_engine.validate(yaml_data, schema)`` directly. They are
deliberately NOT registered in skills-kit's schema_registry -- the schema dicts
live here in task-kit because they change with the task system (CCP).

Layering: the engine checks structure / types / regex only. Vocabulary checks
that depend on the Task Type -- ``status`` within the type's state_vocabulary,
``priority`` against the type's priority pattern, a known ``_schema_version``,
a registered ``type`` name -- are post-walker checks in validate.py, the same
layering skills-kit itself uses (custom rules live outside the engine).

Schemas are floors, not ceilings: unknown keys pass (a type may add
load-bearing fields beyond this set).
"""

# Legal ``task.merge.policy`` values (a post-walker vocabulary check in
# validate.py, like ``status``). The field only DECLARES the policy; acting on
# it is the consumer's job, and what ``on_unit_done`` means for a whole task
# is not defined by task-kit.
MERGE_POLICIES: tuple[str, ...] = ("on_accept", "on_unit_done", "never")

# ``merge.target`` / ``merge.remote`` end up as git arguments: refuse blank,
# whitespace-bearing and option-looking (leading "-") values at the boundary.
_GIT_NAME_FORBID = r"^\s*$|\s|^-"
_GIT_NAME_MSG = "must be a non-blank name with no whitespace and no leading '-'"

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
        # Review metadata. Summary provenance is optional so older task
        # records remain readable and can be upgraded by ``review``.
        "summary": {"required": False, "type": "string"},
        "summary_fingerprint": {"required": False, "type": "string"},
        "summary_updated": {"required": False, "type": "string"},
        # Entries are reference paths (list[path]); per-entry string-ness and
        # resolvability are checked by validate.py's dangling-entry walk (the
        # engine's `items` rule only supports dict-shaped list members).
        "depends_on": {"required": False, "type": "list"},
        "blocked_by": {"required": False, "type": "list"},
        "agent_hint": {"required": False, "type": "string"},
        "skills_to_invoke": {"required": False, "type": "list"},
        # Documents this task produced that outlive it (spec 2.7): repo-
        # relative paths in the OWNING repo, NOT in the task folder. Absent
        # is legal and means "nothing durable was declared" -- every task
        # predating the field reads that way, and archive degrades to a note
        # rather than refusing. Per-entry checks are location_ops' archive
        # verification (existence + outside-the-folder), never validate's:
        # findings gate `work`, and a script cannot tell an architecture
        # spec from a throwaway analysis.
        "durable_outputs": {"required": False, "type": "list"},
        # Hide the task from menus (list, review, the dashboard export, answers
        # refresh and status) until a calendar date, ``YYYY-MM-DD`` in local
        # time: hidden while today is before it, visible on and after it.
        # Optional; absent or null is visible, and status is unaffected. An
        # unquoted ``2026-11-30`` loads from YAML as a date and a quoted one as
        # a string, so the engine checks no type; the date check is a
        # post-walker one in validate.py (hidden.parse_hidden_until).
        "hidden_until": {"required": False},
        # Merge target for the task's work, declared once per task. Optional:
        # absent means the task declares no merge policy, and consumers must
        # not infer one. ``remote`` is optional inside the block; a consumer
        # reading a block without it uses ``origin``.
        "merge": {
            "required": False,
            "type": "dict",
            "keys": {
                "policy": {"required": True, "type": "string"},
                "target": {
                    "required": True,
                    "type": "string",
                    "forbid_regex": _GIT_NAME_FORBID,
                    "msg": _GIT_NAME_MSG,
                },
                "remote": {
                    "required": False,
                    "type": "string",
                    "forbid_regex": _GIT_NAME_FORBID,
                    "msg": _GIT_NAME_MSG,
                },
            },
        },
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

# task_items embedded item enumeration (design/task-items-design.md section 4):
# { items: list[item] }, embedded in plan.md, one block per task. items may be
# empty (a fresh task has nothing enumerated yet). Post-walker checks in
# task_items.py (same layering as task.yaml's): state within the type's
# item_state_vocabulary, priority against the type's priority pattern,
# kebab-case ids, id uniqueness, block singularity + plan.md placement.
TASK_ITEMS_SCHEMA: dict = {
    "root": "task_items",
    "keys": {
        "items": {
            "required": True,
            "type": "list",
            "items": {
                "keys": {
                    "id": {"required": True, "type": "string"},
                    "title": {
                        "required": True,
                        "type": "string",
                        "forbid_regex": r"^\s*$",
                        "msg": "title must be non-empty",
                    },
                    "state": {"required": True, "type": "string"},
                    "priority": {"required": False, "type": "string"},
                    "note": {"required": False, "type": "string"},
                },
            },
        },
    },
}
