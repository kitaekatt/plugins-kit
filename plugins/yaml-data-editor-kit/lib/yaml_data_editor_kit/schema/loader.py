"""Load a profile: ``type`` / ``view`` / ``source`` documents into a Profile.

The loader is fail-closed. An unrecognised key is a ``ProfileError`` rather
than a silently ignored one, because a typo in a declaration that is quietly
dropped removes a check the profile author believes is running.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import yaml

from .errors import ProfileError
from .model import (
    Adapter,
    Constraint,
    CONSTRAINT_KINDS,
    Extensible,
    FieldSpec,
    FIELD_KINDS,
    LEGAL_KEY_KINDS,
    OpenSpec,
    Ordered,
    Profile,
    SOURCE_LAYOUTS,
    SourceSpec,
    STORED_INT,
    STORED_STRING,
    TypeSpec,
    Variants,
    VIEW_FORMS,
    ViewEntry,
    ViewSpec,
)

_FIELD_KEYS = {
    "type", "required", "unit", "meaning", "sentinel", "derived", "provenance",
    "of", "key", "value", "fields", "to",
    "values", "values_from", "stored",
    "shape_from", "partial_of", "routes",
    "length", "min_length", "max_length", "min_chars", "max_chars", "min", "max",
    "ordered",
}
_TYPE_KEYS = {
    "dialect", "id", "title", "identified_by", "fields",
    "variants", "extensible", "open", "constraints", "adapter",
}
_VIEW_KEYS = {"dialect", "id", "of", "form", "fields", "covers"}
_VIEW_ENTRY_KEYS = {"field", "computed", "label", "format", "link", "group", "when", "from"}
_SOURCE_KEYS = {
    "dialect", "of", "layout", "path", "key", "generated_by",
    "record_keys", "record_keys_from", "metadata_keys",
}
_CONSTRAINT_KEYS = {"kind", "why", "from", "to", "both_ways", "ids", "files", "scope"}


def load_profile(paths: Iterable[Path] | Path) -> Profile:
    """Load every dialect document under ``paths`` into one profile.

    A path may be a directory (every ``*.yaml`` / ``*.yml`` beneath it is read)
    or a single file. A file may hold several documents.
    """
    if isinstance(paths, Path):
        paths = [paths]
    profile = Profile()
    for document in _each_document(paths):
        _load_document(profile, document)
    resolve(profile)
    return profile


def _each_document(paths: Iterable[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            for suffix in ("*.yaml", "*.yml"):
                found.extend(sorted(path.rglob(suffix)))
        else:
            found.append(path)
    return found


def _load_document(profile: Profile, document: Path) -> None:
    try:
        raw_documents = list(yaml.safe_load_all(document.read_text(encoding="utf-8")))
    except yaml.YAMLError as exc:
        raise ProfileError("not valid YAML: {0}".format(exc), document) from exc
    for raw in raw_documents:
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ProfileError("a dialect document must be a mapping", document)
        dialect = raw.get("dialect")
        if dialect == "type/1":
            spec = _parse_type(raw, document)
            _refuse_duplicate(profile.types, spec.id, "type", document)
            profile.types[spec.id] = spec
        elif dialect == "view/1":
            view = _parse_view(raw, document)
            _refuse_duplicate(profile.views, view.id, "view", document)
            profile.views[view.id] = view
        elif dialect == "source/1":
            profile.sources.append(_parse_source(raw, document))
        else:
            raise ProfileError(
                "unknown or missing 'dialect:' ({0!r}); expected type/1, view/1 or "
                "source/1".format(dialect),
                document,
            )


def _refuse_duplicate(registry: dict, key: str, kind: str, document: Path) -> None:
    if key in registry:
        raise ProfileError("{0} '{1}' is declared twice".format(kind, key), document)


def _reject_unknown(raw: dict, allowed: set[str], where: str, document: Path) -> None:
    unknown = sorted({str(k) for k in raw} - allowed)
    if unknown:
        raise ProfileError(
            "{0}: unknown key(s) {1}".format(where, ", ".join(unknown)), document
        )


def _require(raw: dict, key: str, where: str, document: Path) -> Any:
    if key not in raw:
        raise ProfileError("{0}: '{1}:' is required".format(where, key), document)
    return raw[key]


# --------------------------------------------------------------------------
# type documents
# --------------------------------------------------------------------------


def _parse_type(raw: dict, document: Path) -> TypeSpec:
    _reject_unknown(raw, _TYPE_KEYS, "type document", document)
    type_id = _require(raw, "id", "type document", document)
    where = "type '{0}'".format(type_id)

    fields = _parse_fields(raw.get("fields") or {}, where, document)
    spec = TypeSpec(
        id=type_id,
        title=raw.get("title"),
        identified_by=raw.get("identified_by"),
        fields=fields,
        document=document,
    )

    if "extensible" in raw:
        spec.extensible = _parse_extensible(raw["extensible"], where, fields, document)
    if "variants" in raw:
        spec.variants = _parse_variants(raw["variants"], where, fields, document)
    if "open" in raw:
        spec.open = _parse_open(raw["open"], where, document)
    if "constraints" in raw:
        spec.constraints = _parse_constraints(raw["constraints"], where, document)
    if "adapter" in raw:
        spec.adapter = _parse_adapter(raw["adapter"], where, document)

    _check_shape_from_placement(spec, where, document)
    if _uses_shape_from(spec) and spec.adapter is None:
        raise ProfileError(
            "{0}: a type declaring 'shape_from:' must also declare an 'adapter:' "
            "saying how to read the foreign declarations".format(where),
            document,
        )
    return spec


def _check_shape_from_placement(spec: TypeSpec, where: str, document: Path) -> None:
    """``shape_from:`` is legal only as a map's ``value:``.

    The dialect lifts data into shape in exactly one place -- a map value
    taking the shape of the record its own key resolves to -- and says so: it
    is not a general macro. Anywhere else there is no key to resolve, so the
    construct has no meaning and refusing it at load time beats reporting it
    once per record.
    """

    def walk(field: FieldSpec, name: str, is_map_value: bool) -> None:
        if field.shape_from is not None and not is_map_value:
            raise ProfileError(
                "{0} field '{1}': 'shape_from:' is only legal as a map's 'value:', "
                "where the key names the record supplying the shape".format(where, name),
                document,
            )
        if field.of is not None:
            walk(field.of, name + ".of", False)
        if field.key is not None:
            walk(field.key, name + ".key", False)
        if field.value is not None:
            walk(field.value, name + ".value", field.kind == "map")
        for child_name, child in field.fields.items():
            walk(child, "{0}.{1}".format(name, child_name), False)

    fields = dict(spec.fields)
    if spec.variants is not None:
        for added in spec.variants.when.values():
            fields.update(added)
    if spec.open is not None:
        fields["open"] = spec.open.type
    for name, field in fields.items():
        walk(field, name, False)


def _uses_shape_from(spec: TypeSpec) -> bool:
    def walk(field: FieldSpec) -> bool:
        if field.shape_from is not None:
            return True
        for child in (field.of, field.key, field.value):
            if child is not None and walk(child):
                return True
        return any(walk(f) for f in field.fields.values())

    fields = list(spec.fields.values())
    if spec.variants is not None:
        for added in spec.variants.when.values():
            fields.extend(added.values())
    return any(walk(f) for f in fields)


def _parse_fields(raw: Any, where: str, document: Path) -> dict[str, FieldSpec]:
    if not isinstance(raw, dict):
        raise ProfileError("{0}: 'fields:' must be a mapping".format(where), document)
    return {
        name: _parse_field(name, decl, "{0} field '{1}'".format(where, name), document)
        for name, decl in raw.items()
    }


def _parse_field(name: str, raw: Any, where: str, document: Path) -> FieldSpec:
    if not isinstance(raw, dict):
        raise ProfileError("{0}: a field declaration must be a mapping".format(where), document)
    _reject_unknown(raw, _FIELD_KEYS, where, document)

    shaped = [k for k in ("type", "shape_from", "partial_of") if k in raw]
    if len(shaped) != 1:
        raise ProfileError(
            "{0}: declare exactly one of 'type:', 'shape_from:' or 'partial_of:' "
            "(found {1})".format(where, ", ".join(shaped) or "none"),
            document,
        )

    spec = FieldSpec(name=name)
    spec.required = bool(raw.get("required", True))
    spec.unit = raw.get("unit")
    spec.meaning = raw.get("meaning")
    spec.derived = raw.get("derived")
    spec.provenance = raw.get("provenance")
    if "sentinel" in raw:
        spec.sentinel = _parse_sentinel(raw["sentinel"], where, document)

    spec.length = raw.get("length")
    spec.min_length = raw.get("min_length")
    spec.max_length = raw.get("max_length")
    spec.min_chars = raw.get("min_chars")
    spec.max_chars = raw.get("max_chars")
    spec.minimum = raw.get("min")
    spec.maximum = raw.get("max")
    if "ordered" in raw:
        spec.ordered = _parse_ordered(raw["ordered"], where, document)

    if "shape_from" in raw:
        spec.shape_from = _as_path(raw["shape_from"], "shape_from", where, document)
        return spec
    if "partial_of" in raw:
        spec.partial_of = str(raw["partial_of"])
        spec.routes = _parse_routes(raw.get("routes") or {}, where, document)
        return spec
    if "routes" in raw:
        raise ProfileError(
            "{0}: 'routes:' is only meaningful on a 'partial_of:' field".format(where),
            document,
        )

    kind = raw["type"]
    if kind not in FIELD_KINDS:
        raise ProfileError(
            "{0}: unknown type '{1}'; expected one of {2}".format(
                where, kind, ", ".join(FIELD_KINDS)
            ),
            document,
        )
    spec.kind = kind

    if kind == "list":
        spec.of = _parse_field(
            "of", _require(raw, "of", where, document), where + " 'of:'", document
        )
    elif kind == "map":
        spec.key = _parse_field(
            "key", _require(raw, "key", where, document), where + " 'key:'", document
        )
        if spec.key.kind not in LEGAL_KEY_KINDS:
            raise ProfileError(
                "{0}: a map key must be an id, a ref or an enum -- a map keyed by "
                "undeclared values is a record whose fields nobody wrote "
                "down".format(where),
                document,
            )
        spec.value = _parse_field(
            "value", _require(raw, "value", where, document), where + " 'value:'", document
        )
    elif kind == "ref":
        spec.to = str(_require(raw, "to", where, document))
    elif kind == "record":
        spec.fields = _parse_fields(_require(raw, "fields", where, document), where, document)
    elif kind == "enum":
        _parse_enum(spec, raw, where, document)

    return spec


def _parse_enum(spec: FieldSpec, raw: dict, where: str, document: Path) -> None:
    stored = raw.get("stored", STORED_STRING)
    if stored not in (STORED_INT, STORED_STRING):
        raise ProfileError(
            "{0}: 'stored:' must be '{1}' or '{2}'".format(where, STORED_INT, STORED_STRING),
            document,
        )
    spec.stored = stored

    has_values = "values" in raw
    has_from = "values_from" in raw
    if has_values == has_from:
        raise ProfileError(
            "{0}: an enum declares exactly one of 'values:' or 'values_from:'".format(where),
            document,
        )
    if has_from:
        spec.values_from = _as_path(raw["values_from"], "values_from", where, document)
        return

    values = raw["values"]
    if isinstance(values, dict):
        spec.value_labels = dict(values)
        for stored_value in values:
            _check_stored_form(stored_value, stored, where, document)
    elif isinstance(values, list):
        spec.values = list(values)
    else:
        raise ProfileError(
            "{0}: 'values:' must be a list of members or a mapping of stored value "
            "to label".format(where),
            document,
        )


def _check_stored_form(value: Any, stored: str, where: str, document: Path) -> None:
    if stored == STORED_INT and not (isinstance(value, int) and not isinstance(value, bool)):
        raise ProfileError(
            "{0}: 'stored: int' but member {1!r} is not an integer".format(where, value),
            document,
        )
    if stored == STORED_STRING and not isinstance(value, str):
        raise ProfileError(
            "{0}: 'stored: string' but member {1!r} is not a string".format(where, value),
            document,
        )


def _parse_sentinel(raw: Any, where: str, document: Path) -> dict[Any, str]:
    if isinstance(raw, dict):
        return {k: str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        return {v: "" for v in raw}
    return {raw: ""}


def _parse_ordered(raw: Any, where: str, document: Path) -> Ordered:
    if not isinstance(raw, dict) or "significance" not in raw:
        raise ProfileError(
            "{0}: 'ordered:' requires 'significance:' -- an ordered list whose order "
            "has no stated meaning tells a later reader nothing".format(where),
            document,
        )
    return Ordered(significance=str(raw["significance"]))


def _parse_routes(raw: Any, where: str, document: Path) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ProfileError("{0}: 'routes:' must be a mapping".format(where), document)
    routes: dict[str, str] = {}
    for key, target in raw.items():
        routes[str(key)] = _as_path(target, "routes." + str(key), where, document)
    return routes


def _as_path(raw: Any, key: str, where: str, document: Path) -> str:
    text = str(raw)
    if text.count(".") != 1 or text.startswith(".") or text.endswith("."):
        raise ProfileError(
            "{0}: '{1}: {2}' must be a '<type>.<field>' path".format(where, key, text),
            document,
        )
    return text


def _parse_extensible(
    raw: Any, where: str, fields: dict[str, FieldSpec], document: Path
) -> Extensible:
    if not isinstance(raw, dict):
        raise ProfileError("{0}: 'extensible:' must be a mapping".format(where), document)
    _reject_unknown(raw, {"via", "abstract_flag"}, where + " 'extensible:'", document)
    via = str(_require(raw, "via", where + " 'extensible:'", document))
    abstract_flag = raw.get("abstract_flag")
    abstract_flag = None if abstract_flag is None else str(abstract_flag)
    for declared in (via, abstract_flag):
        if declared is not None and declared in fields:
            raise ProfileError(
                "{0}: 'extensible:' already declares '{1}'; do not restate it under "
                "'fields:'".format(where, declared),
                document,
            )
    return Extensible(via=via, abstract_flag=abstract_flag)


def _parse_variants(
    raw: Any, where: str, fields: dict[str, FieldSpec], document: Path
) -> Variants:
    if not isinstance(raw, dict):
        raise ProfileError("{0}: 'variants:' must be a mapping".format(where), document)
    # YAML 1.1 -- which PyYAML implements -- resolves a bare `on:` to the
    # boolean True, so `variants: { on: category }` arrives with a True key.
    # The dialect spells the discriminator key `on:`, so accept both spellings
    # rather than making every profile author quote it.
    raw = {("on" if key is True else key): value for key, value in raw.items()}
    _reject_unknown(raw, {"on", "when"}, where + " 'variants:'", document)
    on = str(_require(raw, "on", where + " 'variants:'", document))
    discriminator = fields.get(on)
    if discriminator is None:
        raise ProfileError(
            "{0}: variants 'on: {1}' does not name a field of this type".format(where, on),
            document,
        )
    if discriminator.kind != "enum":
        raise ProfileError(
            "{0}: variants 'on: {1}' must name an enum field, not a {2}".format(
                where, on, discriminator.kind or "shape"
            ),
            document,
        )
    when_raw = _require(raw, "when", where + " 'variants:'", document)
    if not isinstance(when_raw, dict):
        raise ProfileError("{0}: variants 'when:' must be a mapping".format(where), document)
    when: dict[Any, dict[str, FieldSpec]] = {}
    for value, added in when_raw.items():
        when[value] = _parse_fields(
            added or {}, "{0} variant '{1}'".format(where, value), document
        )
    return Variants(on=on, when=when)


def _parse_open(raw: Any, where: str, document: Path) -> OpenSpec:
    if not isinstance(raw, dict):
        raise ProfileError("{0}: 'open:' must be a mapping".format(where), document)
    _reject_unknown(raw, {"prefix", "type"}, where + " 'open:'", document)
    prefix = str(_require(raw, "prefix", where + " 'open:'", document))
    field = _parse_field(
        "open", _require(raw, "type", where + " 'open:'", document), where + " 'open:'", document
    )
    return OpenSpec(prefix=prefix, type=field)


def _parse_constraints(raw: Any, where: str, document: Path) -> list[Constraint]:
    if not isinstance(raw, list):
        raise ProfileError("{0}: 'constraints:' must be a list".format(where), document)
    out: list[Constraint] = []
    for index, entry in enumerate(raw):
        spot = "{0} constraint #{1}".format(where, index)
        if not isinstance(entry, dict):
            raise ProfileError("{0}: must be a mapping".format(spot), document)
        _reject_unknown(entry, _CONSTRAINT_KEYS, spot, document)
        kind = _require(entry, "kind", spot, document)
        if kind not in CONSTRAINT_KINDS:
            raise ProfileError(
                "{0}: unknown constraint kind '{1}'; expected one of {2}".format(
                    spot, kind, ", ".join(CONSTRAINT_KINDS)
                ),
                document,
            )
        why = entry.get("why")
        if not why:
            raise ProfileError(
                "{0}: 'why:' is required -- a constraint whose reason nobody wrote down "
                "is one nobody can safely remove later".format(spot),
                document,
            )
        constraint = Constraint(kind=kind, why=str(why))
        if kind == "covers":
            constraint = Constraint(
                kind=kind,
                why=str(why),
                from_path=_as_path(_require(entry, "from", spot, document), "from", spot, document),
                to_path=_as_path(_require(entry, "to", spot, document), "to", spot, document),
                both_ways=bool(entry.get("both_ways", False)),
            )
        elif kind == "matches_files":
            constraint = Constraint(
                kind=kind,
                why=str(why),
                ids=_as_path(_require(entry, "ids", spot, document), "ids", spot, document),
                files=str(_require(entry, "files", spot, document)),
            )
        elif kind == "unique":
            constraint = Constraint(
                kind=kind,
                why=str(why),
                ids=_as_path(_require(entry, "ids", spot, document), "ids", spot, document),
                scope=entry.get("scope"),
            )
        out.append(constraint)
    return out


def _parse_adapter(raw: Any, where: str, document: Path) -> Adapter:
    if not isinstance(raw, dict):
        raise ProfileError("{0}: 'adapter:' must be a mapping".format(where), document)
    _reject_unknown(raw, {"type_key", "types", "cardinality_key"}, where + " 'adapter:'", document)
    types = _require(raw, "types", where + " 'adapter:'", document)
    if not isinstance(types, dict):
        raise ProfileError("{0}: adapter 'types:' must be a mapping".format(where), document)
    for foreign, native in types.items():
        if native not in FIELD_KINDS:
            raise ProfileError(
                "{0}: adapter maps '{1}' to '{2}', which is not a dialect type".format(
                    where, foreign, native
                ),
                document,
            )
    cardinality_key = raw.get("cardinality_key")
    return Adapter(
        type_key=str(_require(raw, "type_key", where + " 'adapter:'", document)),
        types={str(k): str(v) for k, v in types.items()},
        cardinality_key=None if cardinality_key is None else str(cardinality_key),
    )


# --------------------------------------------------------------------------
# view and source documents
# --------------------------------------------------------------------------


def _parse_view(raw: dict, document: Path) -> ViewSpec:
    _reject_unknown(raw, _VIEW_KEYS, "view document", document)
    view_id = _require(raw, "id", "view document", document)
    where = "view '{0}'".format(view_id)
    form = _require(raw, "form", where, document)
    if form not in VIEW_FORMS:
        raise ProfileError(
            "{0}: unknown form '{1}'; expected one of {2}".format(
                where, form, ", ".join(VIEW_FORMS)
            ),
            document,
        )
    entries_raw = raw.get("fields") or []
    if not isinstance(entries_raw, list):
        raise ProfileError("{0}: 'fields:' must be an ordered list".format(where), document)
    entries: list[ViewEntry] = []
    for index, entry in enumerate(entries_raw):
        spot = "{0} entry #{1}".format(where, index)
        if not isinstance(entry, dict):
            raise ProfileError("{0}: must be a mapping".format(spot), document)
        _reject_unknown(entry, _VIEW_ENTRY_KEYS, spot, document)
        named = [k for k in ("field", "computed") if k in entry]
        if len(named) != 1:
            raise ProfileError(
                "{0}: declare exactly one of 'field:' or 'computed:'".format(spot), document
            )
        if "field" in entry and "from" in entry:
            raise ProfileError(
                "{0}: 'from:' belongs to a 'computed:' entry".format(spot), document
            )
        entries.append(
            ViewEntry(
                field=entry.get("field"),
                computed=entry.get("computed"),
                label=entry.get("label"),
                format=entry.get("format"),
                link=bool(entry.get("link", False)),
                group=entry.get("group"),
                when=entry.get("when"),
                from_expr=entry.get("from"),
            )
        )
    return ViewSpec(
        id=view_id,
        of=str(_require(raw, "of", where, document)),
        form=form,
        entries=entries,
        covers=raw.get("covers"),
        document=document,
    )


def _parse_source(raw: dict, document: Path) -> SourceSpec:
    _reject_unknown(raw, _SOURCE_KEYS, "source document", document)
    of = str(_require(raw, "of", "source document", document))
    where = "source for '{0}'".format(of)
    layout = _require(raw, "layout", where, document)
    if layout not in SOURCE_LAYOUTS:
        raise ProfileError(
            "{0}: unknown layout '{1}'; expected one of {2}".format(
                where, layout, ", ".join(SOURCE_LAYOUTS)
            ),
            document,
        )
    spec = SourceSpec(
        of=of,
        layout=layout,
        path=str(_require(raw, "path", where, document)),
        key=raw.get("key"),
        generated_by=raw.get("generated_by"),
        record_keys=raw.get("record_keys"),
        record_keys_from=raw.get("record_keys_from"),
        metadata_keys=raw.get("metadata_keys"),
        document=document,
    )
    if layout != "rows" and spec.key is not None:
        raise ProfileError(
            "{0}: 'key:' names the containing key of a 'rows' layout only".format(where),
            document,
        )
    if layout == "keyed_map" and not (
        spec.record_keys or spec.record_keys_from or spec.metadata_keys
    ):
        raise ProfileError(
            "{0}: a keyed_map requires 'record_keys:', 'record_keys_from:' or "
            "'metadata_keys:' to separate records from the document's own metadata "
            "keys".format(where),
            document,
        )
    if spec.record_keys_from is not None:
        spec.record_keys_from = _as_path(spec.record_keys_from, "record_keys_from", where, document)
    return spec


# --------------------------------------------------------------------------
# cross-document resolution
# --------------------------------------------------------------------------


def resolve(profile: Profile) -> None:
    """Check every cross-document reference a profile makes.

    Runs after all documents are loaded, because a ``ref`` may name a type
    declared in a file read later.
    """
    for type_spec in profile.types.values():
        _resolve_type(profile, type_spec)
    for view in profile.views.values():
        _resolve_view(profile, view)
    for source in profile.sources:
        _resolve_source(profile, source)


def _resolve_type(profile: Profile, spec: TypeSpec) -> None:
    where = "type '{0}'".format(spec.id)
    fields = list(spec.every_possible_field().items())
    if spec.open is not None:
        fields.append(("open", spec.open.type))
    for name, field in fields:
        _resolve_field(profile, field, "{0} field '{1}'".format(where, name), spec)
    for index, constraint in enumerate(spec.constraints):
        spot = "{0} constraint #{1}".format(where, index)
        for path in (constraint.from_path, constraint.to_path, constraint.ids):
            if path is not None:
                _resolve_value_path(profile, path, spot, spec.document)


def _resolve_field(profile: Profile, field: FieldSpec, where: str, owner: TypeSpec) -> None:
    document = owner.document
    if field.to is not None and field.to not in profile.types:
        raise ProfileError(
            "{0}: 'ref' to unknown type '{1}'".format(where, field.to), document
        )
    if field.values_from is not None:
        _resolve_value_path(profile, field.values_from, where, document)
    if field.shape_from is not None:
        type_id, field_name = field.shape_from.split(".")
        target = profile.types.get(type_id)
        if target is None:
            raise ProfileError(
                "{0}: 'shape_from' names unknown type '{1}'".format(where, type_id), document
            )
        if field_name not in target.every_possible_field():
            raise ProfileError(
                "{0}: 'shape_from' names '{1}', which is not a field of type "
                "'{2}'".format(where, field_name, type_id),
                document,
            )
    if field.partial_of is not None:
        target = profile.types.get(field.partial_of)
        if target is None:
            raise ProfileError(
                "{0}: 'partial_of' names unknown type '{1}'".format(where, field.partial_of),
                document,
            )
        for key, route in field.routes.items():
            _resolve_value_path(profile, route, "{0} route '{1}'".format(where, key), document)
    for child in (field.of, field.key, field.value):
        if child is not None:
            _resolve_field(profile, child, where, owner)
    for name, child in field.fields.items():
        _resolve_field(profile, child, "{0}.{1}".format(where, name), owner)


def _resolve_value_path(
    profile: Profile, path: str, where: str, document: Path | None
) -> None:
    type_id, field_name = path.split(".")
    target = profile.types.get(type_id)
    if target is None:
        raise ProfileError(
            "{0}: path '{1}' names unknown type '{2}'".format(where, path, type_id), document
        )
    if field_name == target.identified_by:
        return
    if field_name not in target.every_possible_field():
        raise ProfileError(
            "{0}: path '{1}' names neither type '{2}'s identity nor one of its "
            "fields".format(where, path, type_id),
            document,
        )


def _resolve_view(profile: Profile, view: ViewSpec) -> None:
    where = "view '{0}'".format(view.id)
    target = profile.types.get(view.of)
    if target is None:
        raise ProfileError("{0}: 'of:' names unknown type '{1}'".format(where, view.of), document=view.document)
    available = target.every_possible_field()
    for entry in view.entries:
        if entry.field is not None and entry.field not in available:
            raise ProfileError(
                "{0}: names field '{1}', which type '{2}' does not declare".format(
                    where, entry.field, view.of
                ),
                view.document,
            )
        if entry.when is not None:
            if target.variants is None or entry.when not in target.variants.when:
                raise ProfileError(
                    "{0}: entry '{1}' has 'when: {2!r}', which is not a variant value of "
                    "type '{3}'".format(where, entry.name, entry.when, view.of),
                    view.document,
                )
    if view.covers is not None and view.covers not in profile.views:
        raise ProfileError(
            "{0}: 'covers:' names unknown view '{1}'".format(where, view.covers), view.document
        )


def _resolve_source(profile: Profile, source: SourceSpec) -> None:
    where = "source for '{0}'".format(source.of)
    target = profile.types.get(source.of)
    if target is None:
        raise ProfileError(
            "{0}: 'of:' names unknown type '{1}'".format(where, source.of), source.document
        )
    if source.layout != "single" and not target.identified_by:
        raise ProfileError(
            "{0}: layout '{1}' needs records to have an identity, but type '{2}' declares "
            "no 'identified_by:'".format(where, source.layout, source.of),
            source.document,
        )
    if source.record_keys_from is not None:
        _resolve_value_path(profile, source.record_keys_from, where, source.document)
