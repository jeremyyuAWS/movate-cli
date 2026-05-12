"""Named reducers for parallel workflow state merging.

When parallel branches of a workflow both write to the same state key,
LangGraph requires a *reducer* — a function that combines multiple
incoming values into one. Operators declare reducers per-key in the
workflow's ``state_schema`` via the ``x-movate-reducer`` JSON Schema
annotation::

    state_schema:
      type: object
      properties:
        history:
          type: array
          items: {type: string}
          x-movate-reducer: append      # operator.add (concat lists)
        seen_urls:
          type: array
          items: {type: string}
          x-movate-reducer: union       # dedup, preserve order
        score:
          type: number
          x-movate-reducer: max
        decisions:
          type: object
          x-movate-reducer: merge       # shallow dict merge

Keys without an annotation use the **last-write-wins** default (same as
``StateGraph(dict)`` behaviour without a reducer). That's fine for keys
only one branch writes; required only for keys that fan-out branches
contend over.

Why named reducers instead of arbitrary Python callables:

* Operators author workflow.yaml. Letting them ship a Python callable
  reference would mean YAML can import arbitrary code at workflow load
  — a security regression. Named reducers are an enum the compiler
  validates.
* Six well-chosen reducers cover the use cases real workflows hit. New
  ones can be added with a single registry entry plus a test.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Reducer implementations
# ---------------------------------------------------------------------------


def _append(left: Any, right: Any) -> Any:
    """Concatenate two lists. ``None`` is treated as the empty list so
    branches that didn't write the key don't suppress the others."""
    if left is None:
        return right
    if right is None:
        return left
    return list(left) + list(right)


def _union(left: Any, right: Any) -> Any:
    """List-of-strings (or other hashables) deduplicated with first-seen
    order preserved. Useful for ``seen_urls`` / ``visited_ids`` style
    accumulators where parallel branches may discover overlapping sets."""
    if left is None:
        return right
    if right is None:
        return left
    seen: set[Any] = set()
    out: list[Any] = []
    for item in list(left) + list(right):
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _max(left: Any, right: Any) -> Any:
    """Return the larger of two values. ``None`` skips."""
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _min(left: Any, right: Any) -> Any:
    """Return the smaller of two values. ``None`` skips."""
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _last(_left: Any, right: Any) -> Any:
    """Last-write-wins. Useful when you want to declare the reducer
    explicitly (for documentation) even though it's also the default
    for un-annotated keys."""
    return right


def _merge(left: Any, right: Any) -> Any:
    """Shallow dict merge. Right wins on conflicting keys."""
    if left is None:
        return right or {}
    if right is None:
        return left or {}
    return {**left, **right}


# ---------------------------------------------------------------------------
# Registry — single source of truth
# ---------------------------------------------------------------------------


REDUCERS: dict[str, Callable[[Any, Any], Any]] = {
    "append": _append,
    "union": _union,
    "max": _max,
    "min": _min,
    "last": _last,
    "merge": _merge,
}
"""Mapping from the YAML-facing reducer name to its implementation.

When extending: add the name + function; add a test row to
``test_workflow_parallel.py``. Keep the reducer pure — no I/O, no
state outside the two arguments. LangGraph may call it multiple times
per node, across re-runs and replays."""


class ReducerError(Exception):
    """Raised when ``state_schema`` references an unknown reducer name.
    Bubbles to ``WorkflowCompileError`` so workflow load surfaces the
    typo immediately, not at first parallel-merge."""


# ---------------------------------------------------------------------------
# Extraction — walks the state_schema for ``x-movate-reducer`` annotations
# ---------------------------------------------------------------------------


def extract_reducers(state_schema: dict[str, Any]) -> dict[str, Callable[[Any, Any], Any]]:
    """Return ``{property_name: reducer_callable}`` for every top-level
    state-schema property that declares ``x-movate-reducer``.

    Only inspects the top level. Nested objects with per-field reducers
    are deferred — workflows that need deeper structure should flatten
    or use one of the dict reducers (``merge``) at the top level.

    Raises :class:`ReducerError` if any annotation references an unknown
    reducer name.
    """
    if not isinstance(state_schema, dict):
        return {}
    properties = state_schema.get("properties")
    if not isinstance(properties, dict):
        return {}

    found: dict[str, Callable[[Any, Any], Any]] = {}
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            continue
        reducer_name = prop_schema.get("x-movate-reducer")
        if reducer_name is None:
            continue
        if not isinstance(reducer_name, str):
            raise ReducerError(
                f"property {prop_name!r}: x-movate-reducer must be a string, "
                f"got {type(reducer_name).__name__}"
            )
        if reducer_name not in REDUCERS:
            raise ReducerError(
                f"property {prop_name!r}: unknown reducer {reducer_name!r}. "
                f"Known reducers: {', '.join(sorted(REDUCERS))}"
            )
        found[prop_name] = REDUCERS[reducer_name]
    return found


# Expose `operator.add` semantics for use by callers who want to inspect
# the registry without importing _append directly.
__all__ = ["REDUCERS", "ReducerError", "extract_reducers"]


# Sanity-check at import time that `_append` matches operator.add for
# common cases — guards against an accidental refactor that breaks
# LangGraph's expectations.
assert _append([1, 2], [3, 4]) == operator.add([1, 2], [3, 4])
