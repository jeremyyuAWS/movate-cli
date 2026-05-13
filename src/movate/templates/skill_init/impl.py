"""Implementation for the __SKILL_NAME__ skill.

The function signature is ``(input: dict, ctx: SkillExecutionContext) -> dict``.

* ``input`` is the dict the LLM produced, already validated against
  the skill's input schema.
* ``ctx`` carries ``trace_id``, ``tenant_id``, ``run_id``, and
  ``call_ms_budget`` — useful for propagating tracing or doing time-
  bounded work.
* The return value must be a dict matching the skill's output schema;
  the executor validates it for you and surfaces a clear error to the
  model if the shape is wrong.

Async is fine too — the backend awaits whatever you return. Use
``async def`` if the skill does I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from movate.core.skill_backend import SkillExecutionContext


def run(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    """Replace this body with the skill's real logic.

    The starter just echoes the input back as a string — enough to
    smoke-test the wiring end-to-end via ``mdk skills run``.
    """
    query = input["query"]
    return {"result": f"echo: {query}"}
