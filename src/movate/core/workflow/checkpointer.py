"""Workflow checkpoint backends for the LangGraph runtime.

Three concerns this module owns:

1. **Tenant isolation.** The load-bearing security boundary. Every
   checkpoint operation must be scoped to a tenant so that tenant A's
   workflow thread_ids are invisible to tenant B's auth context — even
   if tenant B somehow guesses or replays tenant A's thread_id. We
   enforce this by namespacing the LangGraph thread_id with the
   tenant_id at the boundary (see :class:`TenantNamespacedCheckpointer`).
   A mismatched tenant returns "not found", not 403 — 403 would leak
   the existence of the workflow.

2. **Backend selection.** Three kinds are documented (memory / sqlite /
   postgres) matching our storage providers, but v1.0 of this module
   ships **memory only**. SQLite + Postgres ride on top of the
   ``langgraph-checkpoint-sqlite`` / ``-postgres`` companion packages
   and require async connection-pool lifecycle management; both are
   queued for a follow-up PR (see BACKLOG.md "Tier 2 follow-up:
   determinism implementation" §2). Until then the factory raises
   :class:`NotImplementedError` with an operator-facing pointer.

3. **Lifecycle.** The factory returns a value that's safe to pass to
   ``StateGraph.compile(checkpointer=...)`` directly. For memory that's
   the saver instance. For sqlite/postgres that'll be an async context
   manager — the runner will need to open / close it around
   ``ainvoke``. v1.0 only deals with the memory case so the surface
   stays simple.

Why this layout rather than a thin wrapper over our existing
:class:`StorageProvider`: LangGraph's checkpoint protocol is non-trivial
(channel versioning, pending writes for parallel, serializer plumbing).
The companion packages handle that correctly today; wrapping their
output is dramatically less code than re-implementing the protocol from
scratch. The tradeoff is one more optional dep; the upside is we
inherit their bug fixes and stay current with LangGraph itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from enum import StrEnum
from typing import TYPE_CHECKING, Any

# Import the LangGraph base if available so the wrapper can inherit it.
# When langgraph isn't installed we keep an `object` stub so the module
# itself stays importable; instantiating the wrapper raises immediately
# because :func:`make_checkpointer` (the only documented constructor)
# also import-guards.
try:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    _HAS_LANGGRAPH = True
except ImportError:  # pragma: no cover — covered by the missing-dep test path
    _HAS_LANGGRAPH = False

    class BaseCheckpointSaver:  # type: ignore[no-redef]
        """Stub used only when ``langgraph`` isn't installed. Real users
        go through :func:`make_checkpointer`, which raises before this
        stub is ever subclass-instantiated."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
            pass


if TYPE_CHECKING:
    # Only used in type annotations; deferred so the module loads cleanly
    # without ``langgraph`` installed.
    from langgraph.checkpoint.base import (
        Checkpoint,
        CheckpointMetadata,
        CheckpointTuple,
    )


# ---------------------------------------------------------------------------
# Public enum — what the workflow.yaml field accepts
# ---------------------------------------------------------------------------


class CheckpointerKind(StrEnum):
    """Backend selector for :attr:`WorkflowSpec.checkpointer`.

    * ``memory`` — in-process; checkpoints are lost on restart. Default
      for workflows that don't require resume across restarts (no HUMAN
      nodes, no expectation of fault-tolerant continuation). Fast,
      hermetic, zero infra.
    * ``sqlite`` — single-file persistence at ``~/.movate/checkpoints.db``
      (override via ``MOVATE_CHECKPOINT_DB``). Suitable for single-node
      deployments and local dev. **Deferred to a follow-up PR.**
    * ``postgres`` — multi-node-safe persistence, shares the runtime
      Postgres DSN. The production backend for HITL workflows.
      **Deferred to a follow-up PR.**
    """

    MEMORY = "memory"
    SQLITE = "sqlite"
    POSTGRES = "postgres"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CheckpointerError(Exception):
    """Raised for construction-time failures (unknown kind, missing dep,
    invalid config). Runtime checkpoint failures bubble up as the
    underlying LangGraph saver's exceptions."""


# ---------------------------------------------------------------------------
# Tenant namespacing — wraps any BaseCheckpointSaver
# ---------------------------------------------------------------------------


_NAMESPACE_SEP = "::"


class TenantNamespacedCheckpointer(BaseCheckpointSaver):  # type: ignore[type-arg]
    """Adapter that prefixes every ``thread_id`` with a tenant tag.

    Inherits from :class:`BaseCheckpointSaver` so LangGraph internals can
    look up the helper methods (``get_next_version``, ``config_specs``,
    etc.) without us hand-forwarding each one. Operations that take a
    config are overridden to namespace it; everything else falls through
    to ``super()`` (which delegates to the inner saver via ``self.serde``).

    When ``langgraph`` isn't installed the base class is a no-op stub
    and instantiation raises immediately — :func:`make_checkpointer`
    is the only documented constructor and it import-guards.

    Threat model:

    * Tenant A's workflow_run_id = ``wf-abc-123`` becomes
      thread_id = ``acme::wf-abc-123`` in the underlying saver.
    * Tenant B's identical ``wf-abc-123`` becomes ``globex::wf-abc-123``.
    * Either tenant looking up ``wf-abc-123`` directly under the other's
      auth gets ``None`` — same response as a thread that never existed.
      No information leak.

    The namespace separator is ``::``; the tenant tag must not contain
    it (validated at construction).
    """

    def __init__(self, inner: Any, tenant_id: str) -> None:
        if not _HAS_LANGGRAPH:
            raise CheckpointerError(
                "TenantNamespacedCheckpointer requires the langgraph package. "
                "Install with: uv pip install 'movate-cli[langgraph]'"
            )
        if not tenant_id:
            raise CheckpointerError("tenant_id is required")
        if _NAMESPACE_SEP in tenant_id:
            raise CheckpointerError(
                f"tenant_id {tenant_id!r} contains the namespace separator "
                f"{_NAMESPACE_SEP!r}; reject at the API boundary instead"
            )
        # Inherit serde from the inner saver so the persistence format
        # is whatever LangGraph picked, not something we re-invented.
        super().__init__(serde=inner.serde)
        self._inner = inner
        self._tenant = tenant_id

    # ----- pass-throughs not affected by namespacing ------------------------

    def get_next_version(self, current: Any, channel: Any = None) -> Any:
        return self._inner.get_next_version(current, channel)

    # ----- namespacing primitive --------------------------------------------

    def _namespace(self, config: Any) -> Any:
        """Return a config copy with thread_id prefixed by tenant tag.

        Idempotent — if the thread_id is already namespaced for this
        tenant we leave it alone (cheap guard against accidental
        double-wrap if the same config is passed back into us).
        """
        cfg = dict(config)
        configurable = dict(cfg.get("configurable", {}))
        thread_id = configurable.get("thread_id")
        if thread_id is not None and isinstance(thread_id, str):
            prefix = f"{self._tenant}{_NAMESPACE_SEP}"
            if not thread_id.startswith(prefix):
                configurable["thread_id"] = f"{prefix}{thread_id}"
        cfg["configurable"] = configurable
        return cfg

    # ----- sync protocol ----------------------------------------------------

    def put(
        self,
        config: Any,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, Any],
    ) -> Any:
        return self._inner.put(self._namespace(config), checkpoint, metadata, new_versions)

    def get_tuple(self, config: Any) -> Any:  # CheckpointTuple | None
        return self._inner.get_tuple(self._namespace(config))

    def list(
        self,
        config: Any | None,
        *,
        filter: Any = None,
        before: Any = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        # If config is None we'd be iterating the entire saver across all
        # tenants; refuse — cross-tenant enumeration is the exact pattern
        # the namespace is designed to prevent.
        if config is None:
            raise CheckpointerError(
                "list() without a config argument is rejected by the tenant-"
                "namespaced wrapper; supply a config with thread_id to scope "
                "the iteration."
            )
        yield from self._inner.list(
            self._namespace(config),
            filter=filter,
            before=self._namespace(before) if before else None,
            limit=limit,
        )

    def put_writes(
        self,
        config: Any,
        writes: Any,
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._inner.put_writes(self._namespace(config), writes, task_id, task_path)

    # ----- async protocol ---------------------------------------------------

    async def aput(
        self,
        config: Any,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, Any],
    ) -> Any:
        return await self._inner.aput(self._namespace(config), checkpoint, metadata, new_versions)

    async def aget_tuple(self, config: Any) -> Any:  # CheckpointTuple | None
        return await self._inner.aget_tuple(self._namespace(config))

    async def alist(
        self,
        config: Any | None,
        *,
        filter: Any = None,
        before: Any = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            raise CheckpointerError(
                "alist() without a config argument is rejected by the tenant-"
                "namespaced wrapper; supply a config with thread_id to scope "
                "the iteration."
            )
        async for x in self._inner.alist(
            self._namespace(config),
            filter=filter,
            before=self._namespace(before) if before else None,
            limit=limit,
        ):
            yield x

    async def aput_writes(
        self,
        config: Any,
        writes: Any,
        task_id: str,
        task_path: str = "",
    ) -> None:
        await self._inner.aput_writes(self._namespace(config), writes, task_id, task_path)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_checkpointer(
    kind: CheckpointerKind | str,
    *,
    tenant_id: str,
) -> TenantNamespacedCheckpointer:
    """Construct a tenant-namespaced checkpointer of the requested kind.

    Returns a :class:`TenantNamespacedCheckpointer` ready to be passed
    to ``StateGraph.compile(checkpointer=...)``. The wrapper handles
    the tenant isolation; the inner saver handles the actual
    persistence.

    Raises :class:`CheckpointerError` when:

    * The kind is unrecognised.
    * The underlying LangGraph backend can't be imported (e.g. running
      without ``movate-cli[langgraph]``).
    * The kind is currently deferred (sqlite / postgres in v1.0).
    """
    if isinstance(kind, str):
        try:
            kind = CheckpointerKind(kind)
        except ValueError as exc:
            raise CheckpointerError(
                f"unknown checkpointer kind {kind!r}; valid: "
                f"{', '.join(k.value for k in CheckpointerKind)}"
            ) from exc

    if kind is CheckpointerKind.MEMORY:
        try:
            from langgraph.checkpoint.memory import (  # noqa: PLC0415 — optional dep
                MemorySaver,
            )
        except ImportError as exc:
            raise CheckpointerError(
                "checkpointer 'memory' requires the langgraph package. "
                "Install with: uv pip install 'movate-cli[langgraph]'"
            ) from exc
        return TenantNamespacedCheckpointer(MemorySaver(), tenant_id=tenant_id)

    if kind is CheckpointerKind.SQLITE:
        raise CheckpointerError(
            "checkpointer 'sqlite' is queued for a follow-up PR (see BACKLOG.md "
            "Tier 2 §2). Use 'memory' for now, or 'postgres' once that lands."
        )

    if kind is CheckpointerKind.POSTGRES:
        raise CheckpointerError(
            "checkpointer 'postgres' is queued for a follow-up PR (see BACKLOG.md "
            "Tier 2 §2). Use 'memory' until then."
        )

    # Unreachable — StrEnum constraint plus exhaustive cases above.
    raise CheckpointerError(f"unhandled checkpointer kind: {kind!r}")  # pragma: no cover


__all__ = [
    "CheckpointerError",
    "CheckpointerKind",
    "TenantNamespacedCheckpointer",
    "make_checkpointer",
]
