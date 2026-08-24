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
    PathKeyStep,
    PathWalk,
    Profile,
    SCALAR_KINDS,
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
    """An ANCHORED path: ``<type>.<segment>[.<segment>]*``.

    Depth is unbounded -- the walk in ``resolve()`` is what bounds a path to
    the nesting a profile actually declared. All this checks is shape: at
    least one segment past the anchor, and no empty segment (which also
    catches a leading or trailing dot).
    """
    text = str(raw)
    segments = text.split(".")
    if len(segments) < 2 or any(not segment for segment in segments):
        raise ProfileError(
            "{0}: '{1}: {2}' must be a '<type>.<segment>[.<segment>]*' path".format(
                where, key, text
            ),
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
    _check_value_set_cycles(profile)


def _has_identity_less_rows(profile: Profile, type_id: str) -> bool:
    """Whether a type has rows that the corpus must load without identities."""
    target = profile.types[type_id]
    return not target.identified_by and any(
        source.layout == "rows" for source in profile.sources_for(type_id)
    )


def _resolve_type(profile: Profile, spec: TypeSpec) -> None:
    where = "type '{0}'".format(spec.id)
    if spec.extensible is not None and _has_identity_less_rows(profile, spec.id):
        raise ProfileError(
            "{0}: identity-less type '{1}' cannot declare 'extensible:'".format(
                where, spec.id
            ),
            spec.document,
        )
    fields = list(spec.every_possible_field().items())
    if spec.open is not None:
        fields.append(("open", spec.open.type))
    for name, field in fields:
        _resolve_field(profile, field, "{0} field '{1}'".format(where, name), spec)
    for index, constraint in enumerate(spec.constraints):
        spot = "{0} constraint #{1}".format(where, index)
        for path in (constraint.from_path, constraint.to_path, constraint.ids):
            if path is not None:
                landed = _resolve_value_path(profile, path, spot, spec.document)
                _check_terminates_at_set(landed.field, path, spot, spec.document)


def _resolve_field(profile: Profile, field: FieldSpec, where: str, owner: TypeSpec) -> None:
    document = owner.document
    if field.to is not None:
        if field.to not in profile.types:
            raise ProfileError(
                "{0}: 'ref' to unknown type '{1}'".format(where, field.to), document
            )
        if _has_identity_less_rows(profile, field.to):
            raise ProfileError(
                "{0}: identity-less type '{1}' cannot be a 'ref' target".format(
                    where, field.to
                ),
                document,
            )
    if field.values_from is not None:
        walked = _resolve_value_path(
            profile,
            field.values_from,
            where,
            document,
            value_set_owner=field,
        )
        _refuse_identity_less_id_set(
            profile, walked, "values_from", where, document
        )
    if field.shape_from is not None:
        _resolve_value_path(profile, field.shape_from, where, document)
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


def _refuse_identity_less_id_set(
    profile: Profile,
    walked: PathWalk,
    declaration: str,
    where: str,
    document: Path | None,
) -> None:
    """Reject id sets supplied to ``values_from:`` or ``record_keys_from:``."""
    if not _has_identity_less_rows(profile, walked.type_id):
        return
    raise ProfileError(
        "{0}: identity-less type '{1}' cannot supply an id set to '{2}:'".format(
            where, walked.type_id, declaration
        ),
        document,
    )


def resolve_field_path(profile: Profile, path: str) -> PathWalk:
    """Return the recorded walk for an already-validated anchored path.

    The result distinguishes a declared identity ``FieldSpec`` from a
    synthetic identity. A missing walk is an internal error, never a signal to
    skip validation.
    """
    for walked in profile.path_walks:
        if walked.anchored_path == path:
            return walked
    raise RuntimeError("validated path '{0}' has no recorded walk".format(path))


def _resolve_value_path(
    profile: Profile,
    path: str,
    where: str,
    document: Path | None,
    *,
    value_set_owner: FieldSpec | None = None,
) -> PathWalk:
    """Resolve an ANCHORED path against its type's declarations.

    The walk is recorded on the profile so corpus-time map-key checking is an
    obligation of every declared path, independent of which validator feature
    later consumes its value.
    """
    segments = path.split(".")
    type_id, rest = segments[0], segments[1:]
    target = profile.types.get(type_id)
    if target is None:
        raise ProfileError(
            "{0}: path '{1}' names unknown type '{2}'".format(where, path, type_id), document
        )
    return _walk_declared_path(
        profile,
        target,
        type_id,
        rest,
        path,
        where,
        document,
        value_set_owner=value_set_owner,
    )


def _walk_declared_path(
    profile: Profile,
    target: TypeSpec,
    type_id: str,
    segments: list[str],
    path: str,
    where: str,
    document: Path | None,
    *,
    value_set_owner: FieldSpec | None = None,
) -> PathWalk:
    """Walk ``segments`` (everything after the anchor) over ``target``'s
    declarations and record every map-key obligation the walk crosses.

    There are exactly three legal steps: into a ``record``'s ``fields:``, into
    a ``map`` as a KEY (landing on its ``value:``), and -- for the FIRST
    segment only -- a field a ``variants:`` adds. Refused: continuing through
    a ``ref``, a ``list``, or a ``shape_from`` value -- each would reach into
    another type, which is a join and out of scope.
    """
    if not segments:
        raise ProfileError(
            "{0}: path '{1}' has no segment after the type".format(where, path), document
        )
    anchored_path = ".".join([type_id, *segments])
    if segments == [target.identified_by] and target.identified_by is not None:
        identity_field = target.every_possible_field().get(target.identified_by)
        walked = PathWalk(
            type_id=type_id,
            segments=tuple(segments),
            path=path,
            anchored_path=anchored_path,
            field=identity_field,
            synthetic_identity=identity_field is None,
            key_steps=(),
            where=where,
            document=document,
            value_set_owner=value_set_owner,
        )
        profile.path_walks.append(walked)
        return walked

    first = segments[0]
    field = target.every_possible_field().get(first)
    if field is None:
        raise ProfileError(
            "{0}: path '{1}' names '{2}', which type '{3}' does not declare".format(
                where, path, first, type_id
            ),
            document,
        )
    consumed = "{0}.{1}".format(type_id, first) if path == anchored_path else first
    anchored_consumed = "{0}.{1}".format(type_id, first)
    key_steps: list[PathKeyStep] = []
    for segment in segments[1:]:
        field, key_step = _step_into_field(
            field,
            segment,
            consumed,
            anchored_consumed,
            path,
            where,
            document,
        )
        if key_step is not None:
            key_steps.append(key_step)
        consumed = "{0}.{1}".format(consumed, segment)
        anchored_consumed = "{0}.{1}".format(anchored_consumed, segment)
    walked = PathWalk(
        type_id=type_id,
        segments=tuple(segments),
        path=path,
        anchored_path=anchored_path,
        field=field,
        synthetic_identity=False,
        key_steps=tuple(key_steps),
        where=where,
        document=document,
        value_set_owner=value_set_owner,
    )
    profile.path_walks.append(walked)
    return walked


def _step_into_field(
    field: FieldSpec,
    segment: str,
    consumed: str,
    anchored_consumed: str,
    path: str,
    where: str,
    document: Path | None,
) -> tuple[FieldSpec, PathKeyStep | None]:
    if field.kind == "ref":
        raise ProfileError(
            "{0}: path '{1}' cannot continue with segment '{2}' past '{3}', a ref -- "
            "reaching through a ref reaches into another type, which is a join".format(
                where, path, segment, consumed
            ),
            document,
        )
    if field.kind == "list":
        raise ProfileError(
            "{0}: path '{1}' cannot continue with segment '{2}' past '{3}', a list -- "
            "a path may end at a list but not continue through one".format(
                where, path, segment, consumed
            ),
            document,
        )
    if field.shape_from is not None:
        raise ProfileError(
            "{0}: path '{1}' cannot continue with segment '{2}' past '{3}', whose "
            "shape comes from 'shape_from:' -- that shape is read from data at "
            "validation time, so no segment past it can be resolved when the profile "
            "loads".format(
                where, path, segment, consumed
            ),
            document,
        )
    if field.kind == "record":
        next_field = field.fields.get(segment)
        if next_field is None:
            raise ProfileError(
                "{0}: path '{1}' names '{2}', which is not a field of the record at "
                "'{3}'".format(where, path, segment, consumed),
                document,
            )
        return next_field, None
    if field.kind == "map":
        _check_key_membership(field.key, segment, consumed, path, where, document)
        if field.value is None:
            raise ProfileError(
                "{0}: path '{1}' steps into the map at '{2}', which declares no "
                "'value:'".format(where, path, consumed),
                document,
            )
        return field.value, PathKeyStep(
            segment=segment,
            map_path=consumed,
            anchored_map_path=anchored_consumed,
            key_spec=field.key,
        )
    raise ProfileError(
        "{0}: path '{1}' cannot continue with segment '{2}' past '{3}', a '{4}' -- "
        "only a record's fields or a map's keyed value can be stepped into".format(
            where, path, segment, consumed, field.kind or "shape"
        ),
        document,
    )


def _check_key_membership(
    key_spec: FieldSpec | None,
    segment: str,
    consumed: str,
    path: str,
    where: str,
    document: Path | None,
) -> None:
    """Check a key step against the map's declared legal set.

    Only a STATICALLY declared set (an inline 'values:' / mapping form) is
    checkable at profile-load time -- the loader has no corpus, so a
    'values_from:' enum or a 'ref' key's legal set (another type's actual
    identities) cannot be resolved here. Those, like a bare 'id' key, resolve
    the shape unchecked; membership against corpus data is the validator's
    job, once a corpus exists.
    """
    if key_spec is None or key_spec.kind != "enum" or key_spec.values_from is not None:
        return
    legal = [str(v) for v in key_spec.enum_members]
    if str(segment) in legal:
        return
    if not legal:
        raise ProfileError(
            "{0}: path '{1}' has key '{2}' at map '{3}', but the declared set is "
            "empty and admits no map key".format(where, path, segment, consumed),
            document,
        )
    raise ProfileError(
        "{0}: path '{1}' has key '{2}' at map '{3}', which is not a member of the "
        "declared set ({4})".format(where, path, segment, consumed, ", ".join(legal)),
        document,
    )


def _check_value_set_cycles(profile: Profile) -> None:
    """Reject enum value sets whose path key checks depend on themselves."""
    walks_by_owner: dict[int, PathWalk] = {}
    for walked in profile.path_walks:
        if walked.value_set_owner is not None:
            walks_by_owner.setdefault(id(walked.value_set_owner), walked)

    dependencies: dict[int, list[tuple[int, PathKeyStep]]] = {}
    for owner_id, walked in walks_by_owner.items():
        edges: list[tuple[int, PathKeyStep]] = []
        for step in walked.key_steps:
            key_spec = step.key_spec
            if (
                key_spec is not None
                and key_spec.kind == "enum"
                and key_spec.values_from is not None
            ):
                dependency_id = id(key_spec)
                if dependency_id not in walks_by_owner:
                    raise RuntimeError(
                        "enum key 'values_from:' has no recorded path walk"
                    )
                edges.append((dependency_id, step))
        dependencies[owner_id] = edges

    state: dict[int, int] = {}
    for root_id in walks_by_owner:
        if state.get(root_id, 0) != 0:
            continue
        state[root_id] = 1
        path_nodes = [root_id]
        frames: list[tuple[int, int]] = [(root_id, 0)]
        while frames:
            owner_id, edge_index = frames[-1]
            edges = dependencies.get(owner_id, [])
            if edge_index >= len(edges):
                state[owner_id] = 2
                frames.pop()
                path_nodes.pop()
                continue

            dependency_id, step = edges[edge_index]
            frames[-1] = (owner_id, edge_index + 1)
            dependency_state = state.get(dependency_id, 0)
            if dependency_state == 0:
                state[dependency_id] = 1
                path_nodes.append(dependency_id)
                frames.append((dependency_id, 0))
                continue
            if dependency_state == 2:
                continue

            cycle_start = path_nodes.index(dependency_id)
            cycle_nodes = path_nodes[cycle_start:] + [dependency_id]
            cycle = " -> ".join(walks_by_owner[node].path for node in cycle_nodes)
            types = ", ".join(
                sorted({walks_by_owner[node].type_id for node in cycle_nodes})
            )
            walked = walks_by_owner[owner_id]
            raise ProfileError(
                "{0}: cyclic 'values_from:' declarations ({1}); resolving path '{2}' "
                "steps through map '{3}', whose enum key depends on the cycle; types "
                "involved: {4}".format(
                    walked.where,
                    cycle,
                    walked.path,
                    step.anchored_map_path,
                    types,
                ),
                walked.document,
            )


def _check_terminates_at_set(
    field: FieldSpec | None, path: str, where: str, document: Path | None
) -> None:
    """A constraint's 'ids:' / 'from:' / 'to:' must land on a SET: an id
    field, a list of scalars, or a list of refs. Landing on a single scalar is
    an error -- a constraint over one value is not a constraint.
    """
    if field is None:
        return  # the type's own identity: a set of one id, trivially legal.
    if field.kind == "id":
        return
    if field.kind == "list" and field.of is not None and (
        field.of.kind in SCALAR_KINDS or field.of.kind == "ref"
    ):
        return
    raise ProfileError(
        "{0}: path '{1}' must end at a set -- an id field, a list of scalars, or a "
        "list of refs; a path ending at a single scalar is not a constraint".format(
            where, path
        ),
        document,
    )


def _resolve_view(profile: Profile, view: ViewSpec) -> None:
    where = "view '{0}'".format(view.id)
    target = profile.types.get(view.of)
    if target is None:
        raise ProfileError("{0}: 'of:' names unknown type '{1}'".format(where, view.of), document=view.document)
    for entry in view.entries:
        if entry.when is not None:
            if target.variants is None or entry.when not in target.variants.when:
                raise ProfileError(
                    "{0}: entry '{1}' has 'when: {2!r}', which is not a variant value of "
                    "type '{3}'".format(where, entry.name, entry.when, view.of),
                    view.document,
                )
        if entry.field is not None:
            segments = entry.field.split(".")
            if any(not segment for segment in segments):
                raise ProfileError(
                    "{0}: entry '{1}' has an empty segment in 'field: {2}'".format(
                        where, entry.name, entry.field
                    ),
                    view.document,
                )
            _walk_declared_path(
                profile,
                target,
                view.of,
                segments,
                entry.field,
                "{0} entry '{1}'".format(where, entry.name),
                view.document,
            )
            is_identity_path = segments == [target.identified_by]
            if not is_identity_path and segments[0] not in target.declared_fields():
                _check_variant_scoping(target, segments[0], entry, where, view.document)
    if view.covers is not None and view.covers not in profile.views:
        raise ProfileError(
            "{0}: 'covers:' names unknown view '{1}'".format(where, view.covers), view.document
        )


def _check_variant_scoping(
    target: TypeSpec, first_segment: str, entry: ViewEntry, where: str, document: Path | None
) -> None:
    """A view entry whose FIRST path segment names a field only ``variants:``
    adds needs a matching ``when:`` -- otherwise it is an error, because where
    the path is written is what decides whether that field is in scope.
    """
    adding_variants: list[Any] = []
    if target.variants is not None:
        for value, added in target.variants.when.items():
            if first_segment in added:
                adding_variants.append(value)
    if entry.when is None or entry.when not in adding_variants:
        variants = ", ".join(repr(value) for value in adding_variants)
        raise ProfileError(
            "{0}: entry '{1}' names '{2}', which is added by variants {{{3}}} -- an "
            "entry naming it needs a 'when:' matching one of those variants".format(
                where, entry.name, first_segment, variants
            ),
            document,
        )


def _resolve_source(profile: Profile, source: SourceSpec) -> None:
    where = "source for '{0}'".format(source.of)
    target = profile.types.get(source.of)
    if target is None:
        raise ProfileError(
            "{0}: 'of:' names unknown type '{1}'".format(where, source.of), source.document
        )
    if source.layout == "file_per_record" and not target.identified_by:
        raise ProfileError(
            "{0}: layout '{1}' needs records to have an identity, but type '{2}' declares "
            "no 'identified_by:'".format(where, source.layout, source.of),
            source.document,
        )
    if source.record_keys_from is not None:
        walked = _resolve_value_path(
            profile, source.record_keys_from, where, source.document
        )
        _refuse_identity_less_id_set(
            profile, walked, "record_keys_from", where, source.document
        )
