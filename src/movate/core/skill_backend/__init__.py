"""Skill execution backends — one per ``SkillImplementationKind``.

The :class:`SkillBackend` Protocol is the single interface the executor
uses to dispatch a skill call, regardless of how the skill is
implemented (Python function, HTTP endpoint, MCP server). Backends are
matched to skills by ``SkillSpec.implementation.kind`` at registry
build time.

v0.6 ships the Python backend only. HTTP + MCP land in follow-up PRs
without changes to the Protocol or to the executor's tool-use loop.

See ``docs/adr/002-skills-and-contexts.md`` for the design.
"""

from movate.core.skill_backend.base import (
    SkillBackend,
    SkillError,
    SkillErrorType,
    SkillExecutionContext,
    dispatch_skill,
)

__all__ = [
    "SkillBackend",
    "SkillError",
    "SkillErrorType",
    "SkillExecutionContext",
    "dispatch_skill",
]
