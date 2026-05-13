"""Python skill backend — resolves ``pkg.mod:func`` entries via importlib.

The entrypoint string is split on ``:`` into module + attribute, then
``importlib.import_module`` + ``getattr`` produce the callable. The
function is invoked with ``(input_dict, ctx)`` — sync or async, the
backend ``await``s either way. Output must be a dict matching the
skill's declared output schema (enforced one layer up in
:func:`dispatch_skill`).

Failure modes mapped to :class:`SkillError`:

* `ImportError` / `AttributeError` on resolve → ``backend_error``
* Function call exception → ``backend_error`` (preserves original message)
* Function returns non-dict → ``validation_failed`` (caught upstream by
  the output validator, which only accepts dicts)

This module's import side-effects register the backend with the
shared registry. Importing this module is the only thing needed to
"install" the Python backend.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from typing import TYPE_CHECKING, Any

from movate.core.models import SkillImplementationKind
from movate.core.skill_backend.base import (
    SkillError,
    SkillErrorType,
    SkillExecutionContext,
    register_backend,
)

if TYPE_CHECKING:
    from movate.core.skill_loader import SkillBundle


class PythonSkillBackend:
    """Resolves the Python entrypoint and calls it.

    The backend object is stateless; one instance handles every
    Python-kind skill in the project. We do cache the resolved
    callable per ``entry`` string because importlib has non-trivial
    overhead on every call.
    """

    kind = SkillImplementationKind.PYTHON

    def __init__(self) -> None:
        self._resolved: dict[str, Any] = {}

    async def execute(
        self,
        skill: SkillBundle,
        input: dict[str, Any],
        ctx: SkillExecutionContext,
    ) -> dict[str, Any]:
        entry = skill.spec.implementation.entry
        func = self._resolve(entry)
        result = func(input, ctx)
        # Tolerate both sync and async impls. Many simple skills
        # (calculator, JSON munging) are sync; HTTP-using ones are
        # async. Letting both work removes a footgun for skill authors
        # who'd otherwise be forced to make trivial things async.
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise SkillError(
                type=SkillErrorType.VALIDATION_FAILED,
                message=(
                    f"python skill {skill.spec.name!r} returned a "
                    f"{type(result).__name__}, expected dict"
                ),
            )
        return result

    def _resolve(self, entry: str) -> Any:
        """Lazily resolve ``pkg.mod:func`` → the function object.

        Caches per-entry; importlib + getattr cost is measurable when
        a skill is invoked hundreds of times in a long-running worker.
        Validation of the ``:`` shape happens at SkillSpec parse time
        so by the time we get here it's already well-formed.
        """
        if entry in self._resolved:
            return self._resolved[entry]
        module_name, attr_name = entry.split(":", 1)
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=f"can't import {module_name!r}: {exc}",
            ) from exc
        if not hasattr(module, attr_name):
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=f"module {module_name!r} has no attribute {attr_name!r}",
            )
        func = getattr(module, attr_name)
        if not callable(func):
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=f"{entry!r} is not callable",
            )
        self._resolved[entry] = func
        return func


# Auto-register on import. CLI + executor both import this module from
# their _runtime initialization paths, so by the time any skill is
# dispatched the backend is wired up.
register_backend(PythonSkillBackend())


# Keep the loop visible to type checkers; not used at runtime here but
# referenced by tests that need a fresh event loop helper.
_ = asyncio  # silence "imported but unused" if no test references asyncio
