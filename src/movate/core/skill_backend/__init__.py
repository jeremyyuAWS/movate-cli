"""Skill execution backends — one per ``SkillImplementationKind``.

The :class:`SkillBackend` Protocol is the single interface the executor
uses to dispatch a skill call, regardless of how the skill is
implemented (Python function, HTTP endpoint, MCP server). Backends are
matched to skills by ``SkillSpec.implementation.kind`` at registry
build time.

v0.6 ships Python + HTTP backends. MCP lands in a follow-up PR without
changes to the Protocol or the executor's tool-use loop.

See ``docs/adr/002-skills-and-contexts.md`` for the design.
"""

from movate.core.skill_backend.base import (
    SkillBackend,
    SkillError,
    SkillErrorType,
    SkillExecutionContext,
    dispatch_skill,
)

# Backend submodules (``python``, ``http``) are NOT re-exported here:
# the stdlib has a top-level ``http`` module so a bare
# ``from movate.core.skill_backend import http`` confuses mypy under
# strict mode. Callers import the submodules by their full dotted
# path instead — see :mod:`movate.cli.skills_cmd` for the pattern.

__all__ = [
    "SkillBackend",
    "SkillError",
    "SkillErrorType",
    "SkillExecutionContext",
    "dispatch_skill",
]
