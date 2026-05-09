"""Tracing layer: pluggable Tracer interface, env-driven selection.

Selection precedence (lazy — Langfuse only imports when wanted, and never
breaks the run if the package or keys are unavailable):

1. ``MOVATE_TRACER=stdout`` → :class:`StdoutTracer` (testing/CI override).
2. ``MOVATE_TRACER=langfuse`` OR ``LANGFUSE_SECRET_KEY`` set in env →
   :class:`LangfuseTracer`. Falls through to stdout with a stderr warning
   if the package isn't installed or the client can't init.
3. Default → :class:`StdoutTracer` writing JSON spans to stderr.

OTel lands in a follow-up; the same dispatch will grow an `otel` branch.
"""

import os
import sys

from movate.tracing.base import SpanCtx, Tracer
from movate.tracing.stdout import StdoutTracer

__all__ = ["SpanCtx", "StdoutTracer", "Tracer", "build_tracer"]


def build_tracer() -> Tracer:
    """Auto-select a Tracer based on env vars."""
    explicit = os.environ.get("MOVATE_TRACER", "").strip().lower()

    if explicit == "stdout":
        return StdoutTracer(stream=sys.stderr)

    want_langfuse = explicit == "langfuse" or (
        explicit == "" and os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    )
    if want_langfuse:
        # Lazy import: keep the optional dep optional. Tracer module has no
        # third-party deps of its own, but the env-driven check inside it
        # imports langfuse which is the actual optional.
        try:
            from movate.tracing.langfuse import (  # noqa: PLC0415 - lazy by design
                LangfuseTracer,
                LangfuseUnavailableError,
            )

            try:
                return LangfuseTracer()
            except LangfuseUnavailableError as exc:
                sys.stderr.write(f"[movate] Langfuse unavailable, falling back to stdout: {exc}\n")
        except ImportError as exc:  # pragma: no cover - tracer module has no deps
            sys.stderr.write(
                f"[movate] Langfuse tracer module failed to import, falling back to stdout: {exc}\n"
            )

    return StdoutTracer(stream=sys.stderr)
