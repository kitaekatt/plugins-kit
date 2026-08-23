"""Read a foreign schema-as-data declaration through a type's ``adapter:``.

``shape_from:`` lets a corpus keep its own schemas AS DATA and have the dialect
validate against them. Those declarations are written in the corpus's own type
language, so the type using ``shape_from`` declares an ``adapter:`` saying how
to read them. This module is that reading, and nothing else: it turns one
foreign declaration mapping into dialect ``FieldSpec`` objects.
"""

from __future__ import annotations

from typing import Any

from .model import Adapter, FieldSpec


def adapt_shape(
    adapter: Adapter, declarations: Any
) -> tuple[dict[str, FieldSpec], list[str]]:
    """Turn a foreign ``fields`` mapping into dialect field declarations.

    Returns the adapted fields and a list of problems, each phrased so a
    caller can attach it to the file and record it came from.
    """
    problems: list[str] = []
    if not isinstance(declarations, dict):
        return {}, ["the record named by 'shape_from:' does not hold a mapping of declarations"]

    fields: dict[str, FieldSpec] = {}
    for name, declaration in declarations.items():
        field_name = str(name)
        if not isinstance(declaration, dict):
            problems.append(
                "declaration of '{0}' is not a mapping, so the adapter cannot read "
                "it".format(field_name)
            )
            continue
        foreign = declaration.get(adapter.type_key)
        if foreign is None:
            problems.append(
                "declaration of '{0}' has no '{1}:' key, which the adapter names as the "
                "type key".format(field_name, adapter.type_key)
            )
            continue
        native = adapter.types.get(str(foreign))
        if native is None:
            problems.append(
                "declaration of '{0}' has type '{1}', which the adapter does not "
                "map".format(field_name, foreign)
            )
            continue

        element = FieldSpec(name=field_name, kind=native)
        if adapter.cardinality_key is not None and adapter.cardinality_key in declaration:
            count = declaration[adapter.cardinality_key]
            if not isinstance(count, int) or isinstance(count, bool):
                problems.append(
                    "declaration of '{0}' has '{1}: {2!r}', which is not a whole "
                    "number".format(field_name, adapter.cardinality_key, count)
                )
                continue
            fields[field_name] = FieldSpec(
                name=field_name, kind="list", of=element, length=count
            )
        else:
            fields[field_name] = element
    return fields, problems
