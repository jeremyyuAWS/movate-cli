"""Materialise a LangGraph-compatible state class from a JSON Schema.

LangGraph's parallel-write semantics require *typed* state — when two
branches both write the same key, LangGraph consults the field's type
annotation for a reducer. A bare ``StateGraph(dict)`` has no
annotations and raises ``InvalidUpdateError`` on parallel writes.

This module bridges the gap: given a workflow's JSON Schema +
:func:`movate.core.workflow.reducers.extract_reducers` output, it
synthesizes a :class:`typing.TypedDict` whose fields are annotated
with the registered reducer callable where one is declared. LangGraph
discovers those via ``typing.get_type_hints(..., include_extras=True)``
and wires them into its channel-merge step.

Only used when the workflow has parallel edges — pure sequential and
conditional workflows continue to use ``StateGraph(dict)`` so we don't
change behaviour for graphs that don't need reducers.

The synthesized class is a closure per workflow (anonymous, deduped
by id). Tests can introspect it via ``typing.get_type_hints``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, TypedDict


def build_typed_state_class(
    state_schema: dict[str, Any],
    reducers: dict[str, Callable[[Any, Any], Any]],
) -> type:
    """Return a TypedDict class with reducer-annotated fields.

    Every top-level property in ``state_schema`` becomes a field:

    * If the property has a reducer (per the ``reducers`` mapping), the
      field is ``Annotated[Any, <reducer>]``.
    * Otherwise the field is ``Any`` (no reducer — LangGraph's default
      last-write-wins replace-on-update applies).

    ``total=False`` so workflows can supply partial initial state
    without satisfying every declared property — matches what
    operators expect from JSON Schema's permissive defaults.

    Field types are uniformly ``Any`` rather than narrowed to the JSON
    Schema's declared type because:

    * LangGraph only inspects annotations for the **reducer** wrapper,
      not the base type.
    * JSON Schema types don't map 1:1 to Python types (``array`` →
      ``list``, ``object`` → ``dict``, ``integer`` vs ``number``).
      Doing the conversion would add code for no LangGraph benefit.
    * The JSON Schema validator (``Draft202012Validator``) still gates
      ``initial_state`` at runtime — typing-narrowed fields would be
      duplicating that check.
    """
    properties = state_schema.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}

    fields: dict[str, Any] = {}
    for prop_name in properties:
        if prop_name in reducers:
            fields[prop_name] = Annotated[Any, reducers[prop_name]]
        else:
            fields[prop_name] = Any

    # Functional TypedDict form — the supported way to build a TypedDict
    # at runtime. The class name is conventional; tests look up fields
    # via typing.get_type_hints rather than relying on a stable __name__.
    return TypedDict("WorkflowState", fields, total=False)  # type: ignore[operator,no-any-return]


__all__ = ["build_typed_state_class"]
