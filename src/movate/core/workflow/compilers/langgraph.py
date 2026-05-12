"""Compile :class:`WorkflowGraph` onto a LangGraph ``StateGraph``.

v1.0 of the compiler: **linear AGENT workflows only.** Mirrors the
contract of :class:`movate.core.workflow.runner.WorkflowRunner` — same
``WorkflowResult`` shape, same per-node ``RunRecord`` persistence, same
tenant_id propagation, same first-failure-stops semantics — but the
topology walk runs through LangGraph's ``CompiledStateGraph.ainvoke``
instead of our hand-rolled loop.

Why ship this now if it does nothing the homegrown runner doesn't:

* **It's the seam.** Conditional edges (v1.1), parallel fan-out (v1.1),
  HITL pauses (v1.1), and the checkpointer ecosystem all plug in here
  by removing the linear-only validator and emitting the additional
  LangGraph constructs. Without the seam in production code, those
  features remain ahead of their integration path.
* **It validates the seam against a real workload.** The
  ``langgraph_prototype.py`` spike used mock node callables. This
  compiler wraps the actual ``Executor.execute`` — proving that retry,
  fallback, cost tracking, schema validation, and storage persistence
  compose with LangGraph's node-fn lifecycle without surprises.

Error semantics (v1.0):

* Linear walk; first node that returns a non-success
  :class:`movate.core.models.RunResponse` stops the workflow. State
  AT THE POINT OF FAILURE is preserved (not merged with the failing
  node's partial output) — same as the homegrown runner.
* Schema-validation errors on initial_state raise
  :class:`movate.core.workflow.runner.WorkflowRunError` before the
  graph is compiled.
* Agent-load errors at a node raise ``WorkflowRunError`` before that
  node's runner fn is invoked.

What's deferred (v1.1+):

* Conditional edges → ``add_conditional_edges`` mapping. See
  ``docs/langgraph-seam.md`` §B.
* Parallel fan-out → state-schema reducer annotations.
  See ``docs/langgraph-seam.md`` §A.
* HITL → ``interrupt_before`` + checkpointer.
  See ``docs/langgraph-seam.md`` §D-§E.
* Branch-level failure invalidation (sibling branches survive failure).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaError

from movate.core.executor import Executor
from movate.core.loader import AgentBundle, AgentLoadError, load_agent
from movate.core.models import (
    RunRecord,
    RunRequest,
    WorkflowRunRecord,
    WorkflowStatus,
)
from movate.core.workflow.checkpointer import (
    CheckpointerError,
    async_checkpointer,
)
from movate.core.workflow.compilers._typed_state import build_typed_state_class
from movate.core.workflow.ir import EdgeKind, NodeType, WorkflowGraph
from movate.core.workflow.reducers import extract_reducers
from movate.storage.base import StorageProvider

if TYPE_CHECKING:
    # Forward-declared so the type annotation resolves without a runtime
    # circular import (runner.py imports this module via its dispatch).
    from movate.core.workflow.runner import WorkflowResult


class LangGraphCompileError(Exception):
    """Raised when the IR can't be compiled to a LangGraph StateGraph.

    Distinct from :class:`WorkflowRunError` (runtime / per-node failures)
    and :class:`WorkflowCompileError` (IR validation failures). Callers
    typically catch all three and map to exit-code 2.
    """


# ---------------------------------------------------------------------------
# Capability check
# ---------------------------------------------------------------------------


def can_compile(graph: WorkflowGraph) -> tuple[bool, str | None]:
    """Return ``(supported, reason)`` for the v1.1.x compiler.

    Currently supported: AGENT + HUMAN nodes; SEQUENTIAL / CONDITIONAL /
    PARALLEL_FAN_OUT / PARALLEL_FAN_IN edges. Rejected: TOOL / FUNCTION /
    SUB_WORKFLOW node types (queued for v1.1.x). Structural rules
    (conditional default-last, parallel minimum-2-branches, HUMAN-needs-
    checkpointer) are enforced by ``validate_dag``.
    """
    for nid, node in graph.nodes.items():
        if node.type not in (NodeType.AGENT, NodeType.HUMAN):
            return (
                False,
                f"node {nid!r} has type {node.type.value!r}; langgraph compiler "
                "currently handles AGENT and HUMAN nodes only. TOOL / FUNCTION / "
                "SUB_WORKFLOW support lands in v1.1.x.",
            )
    return (True, None)


def import_langgraph() -> tuple[Any, Any, Any]:
    """Lazy LangGraph import.

    Done in a helper rather than at module top so ``movate validate`` and
    other commands that touch this module (via the compilers package
    import chain) don't pay the LangGraph startup cost. Raises
    :class:`LangGraphCompileError` with an install hint when LangGraph
    isn't on the system Python — operators see a friendly pointer
    instead of a raw ImportError.
    """
    try:
        from langgraph.graph import END, START, StateGraph  # noqa: PLC0415 — optional dep
    except ImportError as exc:
        raise LangGraphCompileError(
            "workflow.yaml declares 'runtime: langgraph' but the langgraph "
            "package isn't installed. Install with: "
            "uv pip install 'movate-cli[langgraph]'"
        ) from exc
    return StateGraph, START, END


# ---------------------------------------------------------------------------
# Runner entry point — async; matches WorkflowRunner.run's surface
# ---------------------------------------------------------------------------


async def run_via_langgraph(  # noqa: PLR0912 — single orchestrator; splitting fragments it
    graph: WorkflowGraph,
    initial_state: dict[str, Any],
    *,
    executor: Executor,
    storage: StorageProvider,
    tenant_id: str,
    workflow_run_id: str | None = None,
    resume_payload: dict[str, Any] | None = None,
    resume_as_node: str | None = None,
) -> WorkflowResult:
    """Execute ``graph`` under the LangGraph runtime.

    Drop-in replacement for :meth:`WorkflowRunner.run` when
    ``graph.runtime == "langgraph"``. Returns the same ``WorkflowResult``
    shape so downstream code (CLI render, storage queries, replay)
    doesn't branch on runtime.

    Two invocation modes:

    * **Fresh run** (``resume_payload=None``, the default) — calls
      ``compiled.ainvoke(initial_state)``. Used by the runner.

    * **Resume** (``resume_payload`` is a dict + ``workflow_run_id``
      points at a paused checkpoint) — calls
      ``compiled.aupdate_state(config, resume_payload)`` then
      ``compiled.ainvoke(None, config)``. Used by
      :func:`movate.core.workflow.resume.resume_workflow`. Skips
      ``initial_state`` schema validation since the checkpointed state
      already passed it on the original run.
    """
    # Local imports for the WorkflowResult + WorkflowRunError types and
    # the _summarize_run helper. runner.py imports THIS module at dispatch
    # time, so importing runner at module-level here would cycle.
    from movate.core.workflow.runner import (  # noqa: PLC0415 — circular import
        WorkflowResult,
        WorkflowRunError,
        _summarize_run,
    )

    supported, reason = can_compile(graph)
    if not supported:
        raise LangGraphCompileError(reason or "unsupported graph shape")

    StateGraph, START, END = import_langgraph()  # noqa: N806 — LangGraph public names

    wf_id = workflow_run_id or str(uuid4())
    started = time.monotonic()

    # Validate initial state up front — same as the homegrown runner.
    # Skipped on resume because the checkpointed state already passed
    # validation on the original invocation; the resume_payload is a
    # MERGE not a fresh state, and is validated against the HUMAN
    # node's resume_payload_schema by the caller (resume.py) instead.
    if resume_payload is None:
        try:
            Draft202012Validator(graph.state_schema).validate(initial_state)
        except JsonSchemaError as exc:
            raise WorkflowRunError(
                f"initial_state failed workflow state_schema: {exc.message}"
            ) from exc

    # Pre-load every AGENT bundle so failures surface before we build
    # the graph. Mirrors the homegrown runner's per-node load step.
    # HUMAN nodes have no bundle to load — they're pure pause-points;
    # LangGraph's interrupt_before handles them at the graph layer.
    bundles: dict[str, AgentBundle] = {}
    human_node_ids: list[str] = []
    for nid, node in graph.nodes.items():
        if node.type is NodeType.HUMAN:
            human_node_ids.append(nid)
            continue
        try:
            bundles[nid] = load_agent(node.ref)
        except AgentLoadError as exc:
            raise WorkflowRunError(
                f"node {nid!r}: agent at {node.ref} failed to load: {exc}"
            ) from exc

    # Shared closure state — node fns append RunRecords here and mark
    # workflow-level errors. Reading these after `ainvoke` reconstructs
    # the WorkflowResult.
    captured_runs: list[RunRecord] = []
    error_state: dict[str, Any] = {}  # {"node_id": ..., "error": ErrorInfo, "state_before": dict}

    # Detect whether this workflow needs LangGraph's typed-state path
    # (parallel branches with reducers) or the simpler dict path (pure
    # sequential / conditional). The two paths differ in:
    #   * State class: TypedDict vs dict
    #   * Node-fn return shape: delta-only vs full-state
    # Gating on actual parallel edges keeps existing linear / conditional
    # workflows on the current behaviour so we don't regress them.
    has_parallel = any(
        e.kind in (EdgeKind.PARALLEL_FAN_OUT, EdgeKind.PARALLEL_FAN_IN) for e in graph.edges
    )
    reducers = extract_reducers(graph.state_schema)
    use_typed_state = has_parallel or bool(reducers)
    state_class: Any = (
        build_typed_state_class(graph.state_schema, reducers) if use_typed_state else dict
    )

    def _make_human_node_fn(
        node_id: str,
    ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        """Build a no-op runner for a HUMAN node.

        HUMAN nodes don't do work themselves — they exist to mark a
        pause point. LangGraph's ``interrupt_before`` mechanism halts
        execution BEFORE this fn is invoked on first walk; on resume
        the fn IS invoked (now with the operator-supplied payload
        already merged into state via update_state) and just
        passes state through. The downstream graph then continues.
        """

        async def human_node_fn(state: dict[str, Any]) -> dict[str, Any]:
            # Already-errored short-circuit; same logic as AGENT path.
            if error_state:
                return {} if use_typed_state else state
            # Passthrough: the resume payload was merged via
            # graph.update_state BEFORE ainvoke(None) was called,
            # so by the time we get here `state` already reflects
            # whatever the human supplied. Nothing to do.
            return {} if use_typed_state else dict(state)

        return human_node_fn

    def _make_node_fn(
        node_id: str,
    ) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        bundle = bundles[node_id]

        async def node_fn(state: dict[str, Any]) -> dict[str, Any]:
            # If a previous node errored, this one is a no-op pass-through.
            # LangGraph still walks downstream nodes in topological order;
            # we short-circuit by ignoring them, then the post-invoke
            # logic builds the WorkflowResult from `error_state`.
            if error_state:
                # Typed-state path: return an empty delta (no-op merge).
                # Dict-state path: return the unchanged state so keys
                # are preserved through replace-on-update.
                return {} if use_typed_state else state

            # Project state onto the agent's input schema (same rule as
            # the homegrown runner).
            agent_input = _project_state(state, bundle)

            response = await executor.execute(
                bundle,
                RunRequest(agent=bundle.spec.name, input=agent_input),
                workflow_run_id=wf_id,
                node_id=node_id,
            )

            # Per-node RunRecord summary for the WorkflowResult.runs view.
            summary = _summarize_run(
                response,
                tenant_id=tenant_id,
                bundle=bundle,
                wf_id=wf_id,
                node_id=node_id,
            )
            captured_runs.append(summary)

            if response.status != "success":
                # Same as homegrown: persist an ERROR-status RunRecord
                # for queries that join on workflow_run_id+node_id.
                # Record FIRST failure only — later parallel branches
                # may also fail concurrently, but the first one to hit
                # this lock wins reporting precedence.
                await storage.save_run(summary)
                if "node_id" not in error_state:
                    error_state["node_id"] = node_id
                    error_state["error"] = response.error
                return {} if use_typed_state else state

            if use_typed_state:
                # Typed-state path: return ONLY the agent's output. LangGraph
                # merges via per-key reducers (operator.add for `append`,
                # etc.) or replace-on-update for un-annotated keys. This is
                # the only correct shape for parallel branches — a full-state
                # return would double-count upstream values under any
                # accumulating reducer.
                return response.data
            # Dict-state path: return full merged state because
            # `StateGraph(dict)` replaces wholesale on update.
            new_state = dict(state)
            new_state.update(response.data)
            return new_state

        return node_fn

    state_graph = StateGraph(state_class)
    for nid, node in graph.nodes.items():
        if node.type is NodeType.HUMAN:
            state_graph.add_node(nid, _make_human_node_fn(nid))
        else:
            state_graph.add_node(nid, _make_node_fn(nid))

    state_graph.add_edge(START, graph.entrypoint)

    # Group outbound edges by source. ``validate_dag`` guarantees per-source
    # kinds are uniform (all SEQUENTIAL, all CONDITIONAL, or all
    # PARALLEL_FAN_OUT — never mixed within a single source).
    #
    # Mapping to LangGraph constructs:
    #   * SEQUENTIAL          → state_graph.add_edge(src, dst)
    #   * CONDITIONAL fan-out → state_graph.add_conditional_edges(src, router, mapping)
    #   * PARALLEL_FAN_OUT    → multiple state_graph.add_edge(src, dst_i)
    #                            (LangGraph runs the targets concurrently)
    #   * PARALLEL_FAN_IN     → state_graph.add_edge(src_i, dst) — fan-in is
    #                            per-target; LangGraph waits on all upstream
    #                            edges before firing the target.
    by_source: dict[str, list[Any]] = {}
    for edge in graph.edges:
        by_source.setdefault(edge.from_id, []).append(edge)

    for src, outbound in by_source.items():
        if outbound and outbound[0].kind is EdgeKind.CONDITIONAL:
            _wire_conditional_fan_out(state_graph, src, outbound)
        else:
            # Sequential, parallel_fan_out, and parallel_fan_in all
            # compile to plain add_edge calls. LangGraph's default
            # execution model concurrently runs siblings sharing a
            # source (fan-out) and waits on siblings sharing a target
            # (fan-in).
            for e in outbound:
                state_graph.add_edge(e.from_id, e.to_id)

    for sink in graph.sinks():
        state_graph.add_edge(sink, END)

    # Construct the checkpointer (if configured) and pass into compile().
    # Tenant isolation is the load-bearing concern — every checkpoint
    # operation runs through TenantNamespacedCheckpointer which prefixes
    # the thread_id with `tenant_id::` so tenant A's threads are
    # invisible to tenant B regardless of guessed / shared workflow_run_ids.
    #
    # SQLite + Postgres backends need async connection-pool lifecycle
    # — opened on entering the context, closed on exit. Memory is
    # lifecycle-free but wrapped in the same CM so the call sites
    # don't branch on backend.
    # Tracks pause state — set when LangGraph stops at a HUMAN node.
    # Read after `ainvoke` returns to distinguish "ran to completion"
    # from "paused waiting for resume."
    paused_at: str | None = None

    if graph.checkpointer is not None:
        try:
            async with async_checkpointer(graph.checkpointer, tenant_id=tenant_id) as checkpointer:
                # ``interrupt_before`` tells LangGraph to halt the walk
                # BEFORE invoking the listed node fns. Empty list ⇒ no
                # interrupts; the existing happy path continues unchanged.
                compile_kwargs: dict[str, Any] = {"checkpointer": checkpointer}
                if human_node_ids:
                    compile_kwargs["interrupt_before"] = human_node_ids
                compiled = state_graph.compile(**compile_kwargs)
                # LangGraph requires a thread_id when a checkpointer is
                # attached. We use the workflow_run_id so each invocation
                # maps 1:1 to a checkpoint thread — matches how operators
                # think about "this workflow run."
                invoke_config: dict[str, Any] = {"configurable": {"thread_id": wf_id}}
                if resume_payload is None:
                    # Fresh-run path: invoke with the initial state.
                    final_state = await compiled.ainvoke(dict(initial_state), config=invoke_config)
                else:
                    # Resume path: merge the payload into the
                    # checkpointed state, then invoke with None to
                    # continue from the checkpoint. LangGraph's
                    # update_state writes the merged values to the
                    # current checkpoint; the subsequent ainvoke(None)
                    # re-enters at the post-merge point and proceeds
                    # through the rest of the graph (including any
                    # downstream HUMAN nodes, which may pause again).
                    #
                    # ``as_node`` is critical: without it, LangGraph
                    # treats the update as supplementing the existing
                    # interrupt and re-pauses at the SAME node on the
                    # next ainvoke. Setting ``as_node`` to the paused
                    # HUMAN node tells LangGraph "this update IS the
                    # node's output; advance past it." The resume.py
                    # wrapper threads pause_at from the checkpointed
                    # WorkflowRunRecord into this kwarg.
                    update_kwargs: dict[str, Any] = {}
                    if resume_as_node is not None:
                        update_kwargs["as_node"] = resume_as_node
                    await compiled.aupdate_state(invoke_config, resume_payload, **update_kwargs)
                    final_state = await compiled.ainvoke(None, config=invoke_config)
                # Detect pause: after invoke returns, ask the checkpointer
                # which node was about to run next. If it's a HUMAN node,
                # the workflow is paused there.
                if human_node_ids:
                    paused_at = await _detect_pause(compiled, invoke_config, human_node_ids)
        except CheckpointerError as exc:
            # Re-raise as LangGraphCompileError so the runner's caller
            # gets a single error type to handle for "compile failed",
            # regardless of which sub-step failed.
            raise LangGraphCompileError(str(exc)) from exc
    else:
        compiled = state_graph.compile()
        final_state = await compiled.ainvoke(dict(initial_state))

    finished = time.monotonic()

    if error_state:
        # Workflow halted mid-walk. We use LangGraph's post-merge state
        # (not the failing node's pre-merge `state_before` snapshot) for
        # the WorkflowRunRecord because:
        #
        #   * In SEQUENTIAL workflows the two values are equivalent — the
        #     failing node received state X and returned state X unchanged
        #     (our error short-circuit), so LangGraph's final state is X.
        #
        #   * In PARALLEL workflows the post-merge state includes sibling
        #     branches' completed contributions (LangGraph reduces every
        #     completed branch's writes before reporting back). The
        #     pre-merge snapshot from a single failing branch would
        #     DROP those sibling outputs — operators looking at
        #     `WorkflowResult.final_state` expect to see what succeeded,
        #     not what the failing branch saw.
        wf_record = WorkflowRunRecord(
            workflow_run_id=wf_id,
            tenant_id=tenant_id,
            workflow=graph.name,
            workflow_version=graph.version,
            status=WorkflowStatus.ERROR,
            initial_state=initial_state,
            final_state=final_state,
            error_node_id=error_state["node_id"],
            error=error_state["error"],
        )
        await storage.save_workflow_run(wf_record)
        return WorkflowResult(
            workflow_run_id=wf_id,
            status=WorkflowStatus.ERROR,
            initial_state=initial_state,
            final_state=final_state,
            runs=captured_runs,
            error_node_id=error_state["node_id"],
            error=error_state["error"],
            started_at=started,
            finished_at=finished,
        )

    # Pause path. LangGraph stopped before a HUMAN node — workflow
    # didn't complete; awaiting an external resume.
    if paused_at is not None:
        paused_node = graph.nodes[paused_at]
        wf_record = WorkflowRunRecord(
            workflow_run_id=wf_id,
            tenant_id=tenant_id,
            workflow=graph.name,
            workflow_version=graph.version,
            status=WorkflowStatus.PAUSED,
            initial_state=initial_state,
            final_state=final_state,
            pause_at=paused_at,
        )
        await storage.save_workflow_run(wf_record)
        return WorkflowResult(
            workflow_run_id=wf_id,
            status=WorkflowStatus.PAUSED,
            initial_state=initial_state,
            final_state=final_state,
            runs=captured_runs,
            pause_at=paused_at,
            resume_payload_schema=paused_node.resume_payload_schema,
            started_at=started,
            finished_at=finished,
        )

    # Happy path.
    wf_record = WorkflowRunRecord(
        workflow_run_id=wf_id,
        tenant_id=tenant_id,
        workflow=graph.name,
        workflow_version=graph.version,
        status=WorkflowStatus.SUCCESS,
        initial_state=initial_state,
        final_state=final_state,
    )
    await storage.save_workflow_run(wf_record)
    return WorkflowResult(
        workflow_run_id=wf_id,
        status=WorkflowStatus.SUCCESS,
        initial_state=initial_state,
        final_state=final_state,
        runs=captured_runs,
        started_at=started,
        finished_at=finished,
    )


def _wire_conditional_fan_out(
    state_graph: Any,
    src: str,
    outbound: list[Any],
) -> None:
    """Emit a LangGraph ``add_conditional_edges`` call for the conditional
    edges leaving ``src``.

    Pre-conditions enforced by :func:`validate_conditional`:

    * All edges in ``outbound`` are ``EdgeKind.CONDITIONAL``.
    * Exactly one has ``condition is None`` (the explicit default).
    * The default is the LAST element in ``outbound``.
    * Every non-default has a parseable ``condition``.

    We pre-parse each condition at compile time so the runtime router fn
    only does a quick truthiness check per branch — no parsing on the
    hot path.
    """
    from movate.core.workflow.condition_dsl import parse_condition  # noqa: PLC0415

    # Pre-compile (parse) every non-default branch's expression. The
    # default branch is the last entry; capture its target separately.
    branches: list[tuple[Any, str]] = []  # [(CompiledCondition, target_node_id)]
    default_target: str | None = None
    for e in outbound:
        if e.when_is_default():
            default_target = e.to_id
        else:
            assert e.condition is not None  # validator guaranteed
            branches.append((parse_condition(e.condition), e.to_id))
    assert default_target is not None  # validator guaranteed

    def router(state: dict[str, Any]) -> str:
        # Walk the conditional branches in YAML order; first truthy wins.
        # Mirrors the human reading of the YAML — operators see the same
        # priority order on paper as the runtime applies.
        for cond, target in branches:
            if cond.evaluate(state):
                return target
        # default_target is set above (validator guarantees `when: null`
        # default exists for every conditional fan-out); the asserts after
        # the loop narrow the type for mypy.
        assert default_target is not None
        return default_target

    # `path_map` lets us return target node ids directly (rather than
    # arbitrary keys that LangGraph then maps to nodes). Identity map.
    targets = {e.to_id for e in outbound}
    path_map = {t: t for t in targets}
    state_graph.add_conditional_edges(src, router, path_map)


async def _detect_pause(
    compiled: Any,
    config: dict[str, Any],
    human_node_ids: list[str],
) -> str | None:
    """Return the HUMAN node id the workflow paused at, or None if it
    ran to completion.

    LangGraph exposes the post-invoke snapshot via ``aget_state(config)``;
    the ``next`` field on that snapshot is the tuple of node names
    queued to run next. If that tuple is non-empty AND its first member
    is one of our HUMAN nodes, the workflow paused before invoking it.
    Empty ``next`` means the graph hit END — no pause, just completion.
    """
    try:
        snapshot = await compiled.aget_state(config)
    except Exception:  # pragma: no cover — defensive; LangGraph rarely raises here
        return None

    next_nodes = getattr(snapshot, "next", ()) or ()
    if not next_nodes:
        return None

    # If the next-to-run node is a HUMAN node, we're paused there.
    for nid in next_nodes:
        if nid in human_node_ids:
            return str(nid)
    return None


def _project_state(state: dict[str, Any], bundle: AgentBundle) -> dict[str, Any]:
    """Same rule as :func:`movate.core.workflow.runner._project_state` —
    duplicated here (not imported) so this module stays a true alternative
    compiler: a future variant might project differently (e.g. typed-state
    extraction) without touching the homegrown runner."""
    props = bundle.input_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return dict(state)
    return {k: state[k] for k in props if k in state}


# Type-only re-export so callers can `from ...compilers.langgraph import LangGraphCompileError`
# without importing this whole module's heavy machinery.
__all__ = ["LangGraphCompileError", "can_compile", "import_langgraph", "run_via_langgraph"]
