"""Linear single-agent executor.

Pipeline (v0.1):

    validate input
        → render prompt
        → invoke provider (with retries and fallback chain)
        → validate output
        → record metrics + persist

Workflow orchestration is Phase 3 (`movate.core.workflow`).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from jsonschema import ValidationError as JsonSchemaError

from movate.core.config import ModelPolicy, RuntimePolicy
from movate.core.failures import (
    DEFAULT_RETRY,
    BudgetExceededError,
    MovateError,
    PolicyViolationError,
    SchemaError,
    TenantBudgetExceededError,
)
from movate.core.loader import AgentBundle
from movate.core.models import (
    ErrorInfo,
    FailureRecord,
    JobStatus,
    Metrics,
    ModelConfig,
    ModelFallback,
    RunRecord,
    RunRequest,
    RunResponse,
    TokenUsage,
)
from movate.core.retry import RetryExhaustedError, run_with_retries
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    Message,
)
from movate.providers.pricing import PricingTable
from movate.providers.registry import ProviderRegistry, UnregisteredRuntimeError
from movate.storage.base import StorageProvider
from movate.tools import ToolError, get_tool
from movate.tracing.base import SpanCtx, Tracer

log = logging.getLogger(__name__)

_COST_DRIFT_THRESHOLD = 0.05  # 5%


class Executor:
    def __init__(
        self,
        *,
        provider: BaseLLMProvider | None = None,
        registry: ProviderRegistry | None = None,
        pricing: PricingTable,
        storage: StorageProvider,
        tracer: Tracer,
        tenant_id: str = "local",
        policy: ModelPolicy | None = None,
        runtime_policy: RuntimePolicy | None = None,
    ) -> None:
        """One of ``provider`` (legacy single-runtime) OR ``registry``
        (multi-runtime, v0.6+) must be set. Passing ``provider`` is
        equivalent to ``registry=ProviderRegistry(default_litellm=provider)``
        and is preserved so the existing 100+ test sites keep working
        unchanged. New code passes ``registry=`` so it can wire up
        native-SDK adapters alongside LiteLLM."""
        if provider is None and registry is None:
            raise ValueError("Executor needs either provider= or registry=")
        if provider is not None and registry is not None:
            raise ValueError("pass either provider= OR registry=, not both")
        if registry is not None:
            self._registry = registry
        else:
            # mypy: provider is not None here (one of the two must be set).
            assert provider is not None
            self._registry = ProviderRegistry(default_litellm=provider)
        self._pricing = pricing
        self._storage = storage
        self._tracer = tracer
        self._tenant_id = tenant_id
        # Permissive default — an executor built without a policy enforces
        # nothing, preserving v0.1-style behavior for callers that haven't
        # opted in yet (tests, downstream embedders).
        self._policy = policy or ModelPolicy()
        # RuntimePolicy gates which AgentRuntime values are permitted —
        # belt-and-braces against an agent.yaml that skipped `movate
        # validate` (e.g. loaded over HTTP by a worker). Permissive default.
        self._runtime_policy = runtime_policy or RuntimePolicy()

    async def execute(
        self,
        bundle: AgentBundle,
        request: RunRequest,
        *,
        job_id: str | None = None,
        model_override: ModelConfig | None = None,
        workflow_run_id: str | None = None,
        node_id: str | None = None,
        on_token: Callable[[str], None] | None = None,
        history: list[Message] | None = None,
    ) -> RunResponse:
        """Execute one agent against one input.

        ``model_override`` swaps provider/params for a single run (used by
        ``movate bench`` in v0.2). Override disables the configured fallback
        chain so each comparison row tests exactly one model.

        ``workflow_run_id`` + ``node_id`` are stamped onto the persisted
        :class:`RunRecord` when the executor is invoked from a
        :class:`movate.core.workflow.WorkflowRunner` — keeps the runner from
        having to re-save the same run with a workflow link patched on.

        ``on_token`` opts into streaming. When set, the executor calls
        ``provider.stream()`` and invokes the callback with each text
        delta as it arrives — useful for ``movate run --stream`` to
        render tokens live in the terminal. The accumulated text is
        still schema-validated, persisted, and returned the same way
        as a non-streaming run; ``on_token`` only adds an observation
        callback. Streaming inherits retries + fallback identically
        to one-shot calls (a stream that exhausts retries falls
        through to the next provider in the chain).

        ``history`` is an optional list of prior conversation messages
        (user/assistant pairs from previous turns) — prepended to the
        provider call so multi-turn agents see context. The CURRENT
        request's input still goes through prompt rendering + input
        schema validation the same as a one-shot run; history is purely
        conversational context the model uses for continuity.
        ``movate chat`` is the primary caller; one-shot ``movate run``
        invocations leave it ``None``."""
        job_id = job_id or str(uuid4())
        run_id = str(uuid4())
        spec = bundle.spec
        effective_model = model_override or spec.model

        span = self._tracer.start_span(
            "agent.execute",
            {
                "agent": spec.name,
                "agent_version": spec.version,
                "provider": effective_model.provider,
                "tenant_id": self._tenant_id,
                "job_id": job_id,
                "run_id": run_id,
                "model_override": model_override is not None,
            },
        )

        started = time.monotonic()
        try:
            # Runtime POLICY check first — if the project bans this
            # runtime (e.g. movate.yaml: runtime.allowed: [litellm]),
            # surface as PolicyViolationError so the failure trail
            # matches model-policy violations.
            runtime_violation = self._runtime_policy.check_agent(spec)
            if runtime_violation is not None:
                raise PolicyViolationError(runtime_violation)

            # Runtime AVAILABILITY check — if the agent declared a
            # runtime we don't have an adapter for (e.g. opted into a
            # native runtime whose optional extra isn't installed),
            # fail fast with a SchemaError before doing any side
            # effects or budget checks.
            try:
                provider_for_run = self._registry.get(spec.runtime)
            except UnregisteredRuntimeError as exc:
                # SchemaError is the closest fit in our taxonomy — the
                # YAML declares a runtime that doesn't exist in this
                # build. Retries won't help.
                raise SchemaError(str(exc)) from exc

            # Tenant-budget check — if the tenant's monthly cap is
            # breached, no run should fire (not even a doomed one).
            # Cheap PK lookup + a single SUM aggregate; the index on
            # (tenant_id, created_at) is the perf path.
            await self._check_tenant_budget()

            # Policy check happens BEFORE schema validation and prompt
            # rendering — a denied model shouldn't get to bill latency
            # or trigger any side effects. ``check_model`` is also
            # cheaper than schema validation so a misconfigured agent
            # fails fast.
            #
            # We check the effective model + every fallback the executor
            # might try. ``bench`` uses ``model_override`` which disables
            # the fallback chain, so we only check the override in that
            # case (mirrors the chain construction below).
            if not self._policy.is_permissive():
                self._enforce_policy(spec, effective_model, model_override is not None)

            try:
                bundle.input_validator.validate(request.input)
            except JsonSchemaError as exc:
                raise SchemaError(f"input failed schema: {exc.message}") from exc

            # Render the current turn as role-tagged messages.
            # ``render_messages`` returns ``[{system}, {user}]`` when the
            # prompt template defines a system/user block split (a chat
            # template optimization for memory), or ``[{user}]`` for
            # every existing template (back-compat).
            current_turn = [
                Message(role=m["role"], content=m["content"])  # type: ignore[arg-type]
                for m in bundle.render_messages(request.input)
            ]
            self._tracer.log_event(span, {"prompt_hash": bundle.prompt_hash})

            chain: list[tuple[str, dict[str, Any]]] = [
                (effective_model.provider, dict(effective_model.params))
            ]
            if model_override is None:
                for fb in spec.model.fallback:
                    merged = dict(spec.model.params)
                    merged.update(fb.params)
                    chain.append((fb.provider, merged))

            completion: CompletionResponse | None = None
            chosen_provider = ""
            last_error: MovateError | None = None

            for provider_str, params in chain:
                # Build the final conversation messages list:
                #   [system?, *history, user]
                # The system message (if any) goes FIRST so the model
                # treats it as standing instruction. Conversation history
                # (chat memory) goes between system and the current user
                # turn so prior turns share the same system context
                # without re-tokenizing the instructions every time.
                system_msgs = [m for m in current_turn if m.role == "system"]
                user_msgs = [m for m in current_turn if m.role == "user"]
                conversation: list[Message] = [
                    *system_msgs,
                    *(history or []),
                    *user_msgs,
                ]
                # Build the OpenAI / LiteLLM tool list from the agent's
                # declared tools. Empty list → None so we don't pass
                # ``tools=[]`` to the provider (LiteLLM warns / some
                # providers reject the empty form).
                openai_tools: list[dict[str, Any]] | None = None
                if spec.tools:
                    try:
                        openai_tools = [get_tool(name).to_openai_tool() for name in spec.tools]
                    except ToolError as exc:
                        # Unknown tool name in agent.yaml — fail loudly at
                        # first call. The validate command will eventually
                        # catch this earlier (TODO: validate-time check).
                        raise SchemaError(
                            f"agent {spec.name!r} references unknown tool: {exc}"
                        ) from exc

                req = CompletionRequest(
                    provider=provider_str,
                    messages=conversation,
                    params=params,
                    tools=openai_tools,
                )

                async def _invoke(req: CompletionRequest = req) -> CompletionResponse:
                    if on_token is None:
                        return await provider_for_run.complete(req)
                    # Stream path. Accumulate chunks into a single
                    # CompletionResponse so everything below this
                    # (schema validation, cost calc, persistence) sees
                    # the same shape regardless of streaming.
                    return await self._invoke_streaming(provider_for_run, req, on_token)

                try:
                    completion = await run_with_retries(_invoke)
                    # Tool-call loop. If the model emitted tool_calls,
                    # invoke each registered tool, append the results
                    # to the conversation, and re-call the provider
                    # until the model emits a plain-text response (no
                    # more tool_calls) or we hit the iteration cap.
                    # Token usage accumulates across iterations so the
                    # cost calc below sees the full provider spend.
                    if openai_tools and completion.tool_calls:
                        completion = await self._run_tool_loop(
                            provider_for_run=provider_for_run,
                            initial_completion=completion,
                            conversation=conversation,
                            params=params,
                            provider_str=provider_str,
                            openai_tools=openai_tools,
                            span=span,
                        )
                    chosen_provider = provider_str
                    break
                except RetryExhaustedError as exc:
                    last_error = exc.last_error
                    rule = _retry_rule_for(exc.last_error)
                    if rule and rule.fallback_on_exhaust:
                        self._tracer.log_event(
                            span,
                            {
                                "fallback_triggered": True,
                                "from": provider_str,
                                "reason": exc.last_error.failure_type.value,
                            },
                        )
                        continue
                    raise

            if completion is None:
                assert last_error is not None
                raise last_error

            # Pricing-key dance: each adapter knows the canonical key for
            # its provider strings (LiteLLM passes the agent's
            # ``model.provider`` through unchanged; native_anthropic /
            # native_openai prepend the family prefix; langchain returns
            # None because the model is opaque). When None or the lookup
            # misses we record cost=0 with an event — better than
            # crashing on a runtime where pricing isn't applicable.
            pricing_key = provider_for_run.pricing_key(chosen_provider)
            if pricing_key is None:
                cost = 0.0
                self._tracer.log_event(
                    span,
                    {"cost_skipped": True, "reason": "runtime has no pricing key"},
                )
            else:
                try:
                    cost = self._pricing.cost_for(provider=pricing_key, tokens=completion.tokens)
                except KeyError:
                    cost = 0.0
                    self._tracer.log_event(
                        span,
                        {"cost_skipped": True, "reason": f"no pricing for {pricing_key!r}"},
                    )
            self._check_cost_drift(span, completion, cost)

            # The effective ceiling is the MIN of the agent's declared
            # budget and the project policy's ceiling. Project policy
            # never relaxes — it can only tighten. If a project sets no
            # ceiling, the agent's own budget wins.
            effective_ceiling = self._policy.effective_max_cost(spec.budget.max_cost_usd_per_run)
            if cost > effective_ceiling:
                raise BudgetExceededError(
                    f"run cost ${cost:.4f} exceeds ceiling ${effective_ceiling:.4f} "
                    f"(agent budget ${spec.budget.max_cost_usd_per_run:.4f}, "
                    f"policy {self._policy.max_cost_per_run_usd})"
                )

            output = _parse_json_output(completion.text)
            try:
                bundle.output_validator.validate(output)
            except JsonSchemaError as exc:
                raise SchemaError(f"model output failed schema: {exc.message}") from exc

            metrics = Metrics(
                latency_ms=int((time.monotonic() - started) * 1000),
                tokens=completion.tokens,
                cost_usd=cost,
                provider=chosen_provider,
                pricing_version=self._pricing.version,
            )

            response = RunResponse(
                status="success",
                run_id=run_id,
                data=output,
                human_readable=_extract_human_readable(output),
                trace_id=span.trace_id,
                metrics=metrics,
            )

            await self._record_run(
                bundle=bundle,
                run_id=run_id,
                job_id=job_id,
                request=request,
                response=response,
                chosen_provider=chosen_provider,
                workflow_run_id=workflow_run_id,
                node_id=node_id,
            )
            self._tracer.end_span(span, status="ok")
            return response

        except MovateError as exc:
            return await self._handle_failure(
                span=span,
                bundle=bundle,
                run_id=run_id,
                job_id=job_id,
                started=started,
                err=exc,
            )
        except RetryExhaustedError as exc:
            return await self._handle_failure(
                span=span,
                bundle=bundle,
                run_id=run_id,
                job_id=job_id,
                started=started,
                err=exc.last_error,
            )

    async def _run_tool_loop(
        self,
        *,
        provider_for_run: BaseLLMProvider,
        initial_completion: CompletionResponse,
        conversation: list[Message],
        params: dict[str, Any],
        provider_str: str,
        openai_tools: list[dict[str, Any]],
        span: SpanCtx,
    ) -> CompletionResponse:
        """Iterate the model ↔ tool exchange until the model emits a
        plain-text response (no ``tool_calls``) or we hit the cap.

        Each iteration:

        1. Invokes every tool the model requested. Sync tools run
           in-thread; async tools are awaited. Tool exceptions surface
           as :class:`ToolError` and fail the run (no inline retry).
        2. Appends the assistant message (with the tool_calls metadata)
           and a ``role: tool`` message per result to the conversation.
        3. Re-calls the provider with the updated conversation +
           ``tools=`` still passed so the model can chain follow-up calls.
        4. Accumulates token usage across iterations into a single
           ``CompletionResponse`` so the caller's cost calc sees the
           full spend.

        Iteration cap is 10 — catches runaway loops where the model
        keeps requesting tools forever. Configurable later via
        ``agent.yaml: tools_loop: max_iters: N`` if a real use case
        demands it.
        """
        max_iterations = 10  # hard cap for v1.0

        completion = initial_completion
        # Accumulators — final returned CompletionResponse has the
        # SUMMED token usage and the FINAL text response.
        total_input = completion.tokens.input
        total_output = completion.tokens.output
        total_cached = completion.tokens.cached_input
        # Carry through the latest raw payload for cost-drift checks.
        raw = dict(completion.raw)

        # Working conversation — starts with the original messages,
        # grows by one assistant + N tool messages per iteration.
        msgs = list(conversation)

        iterations = 0
        while completion.tool_calls and iterations < max_iterations:
            iterations += 1
            self._tracer.log_event(
                span,
                {
                    "tool_loop_iteration": iterations,
                    "tool_calls": [{"name": tc.name, "id": tc.id} for tc in completion.tool_calls],
                },
            )

            # 1. Echo the assistant's tool-call request back into the
            #    conversation (LiteLLM / OpenAI requires this so the
            #    `role: tool` messages have a corresponding request to
            #    pair with).
            msgs.append(
                Message(
                    role="assistant",
                    content="",
                    tool_calls=list(completion.tool_calls),
                )
            )

            # 2. Invoke each tool and append the result.
            for tc in completion.tool_calls:
                try:
                    tool = get_tool(tc.name)
                except ToolError as exc:
                    raise SchemaError(f"model requested unknown tool {tc.name!r}: {exc}") from exc
                result = await _invoke_tool(tool.callable, tc.arguments)
                msgs.append(
                    Message(
                        role="tool",
                        content=_serialize_tool_result(result),
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                )

            # 3. Re-call the provider with the updated conversation.
            next_req = CompletionRequest(
                provider=provider_str,
                messages=msgs,
                params=params,
                tools=openai_tools,
            )

            async def _invoke_iter(req: CompletionRequest = next_req) -> CompletionResponse:
                return await provider_for_run.complete(req)

            completion = await run_with_retries(_invoke_iter)
            total_input += completion.tokens.input
            total_output += completion.tokens.output
            total_cached += completion.tokens.cached_input
            raw = dict(completion.raw)  # last iteration's raw payload wins

        if completion.tool_calls and iterations >= max_iterations:
            raise SchemaError(
                f"tool-call loop exceeded {max_iterations} iterations; "
                f"the model keeps requesting tools. Check your prompt — "
                f"agents that loop indefinitely usually have a contradictory "
                f"instruction or missing termination clause."
            )

        return CompletionResponse(
            text=completion.text,
            tokens=TokenUsage(
                input=total_input,
                output=total_output,
                cached_input=total_cached,
            ),
            raw=raw,
            tool_calls=None,  # cleared — we consumed them
        )

    async def _invoke_streaming(
        self,
        provider: BaseLLMProvider,
        req: CompletionRequest,
        on_token: Callable[[str], None],
    ) -> CompletionResponse:
        """Drive ``provider.stream()`` and accumulate into a single
        :class:`CompletionResponse`.

        Token totals come from the LAST chunk in the stream (providers
        return them via ``stream_options={'include_usage': True}``).
        If a stream ends without ever delivering usage stats — older
        providers, mis-configured proxies — we fall through with
        zeros. Cost accounting then reads zero, which is wrong but
        survivable; the cost-drift check downstream will flag it.

        Takes ``provider`` explicitly (rather than via ``self``) so
        the executor can dispatch per-agent across multiple
        registered providers — see :class:`ProviderRegistry`."""
        text_parts: list[str] = []
        final_tokens: TokenUsage | None = None
        raw: dict[str, Any] = {}
        async for chunk in provider.stream(req):
            if chunk.text:
                text_parts.append(chunk.text)
                on_token(chunk.text)
            if chunk.tokens is not None:
                final_tokens = chunk.tokens
            if chunk.raw:
                # Last write wins — adapters that forward provider
                # metadata typically only stamp it on the final chunk.
                raw.update(chunk.raw)
        return CompletionResponse(
            text="".join(text_parts),
            tokens=final_tokens or TokenUsage(),
            raw=raw,
        )

    async def _check_tenant_budget(self) -> None:
        """Abort the run if the tenant has hit its monthly cap.

        Reads :meth:`StorageProvider.get_tenant_budget` (PK lookup —
        cheap). If no row exists for this tenant or
        ``monthly_usd_limit`` is ``None``, returns immediately (the
        default-unlimited case, backwards compatible with every
        pre-budget deployment).

        Race window: under high concurrency two requests can both
        observe "under budget" simultaneously and both succeed,
        pushing combined cost over the cap. The overrun is bounded
        by the in-flight call count — not catastrophic, but
        operators should set the cap slightly below the hard cost
        ceiling they actually want to enforce.
        """
        budget = await self._storage.get_tenant_budget(self._tenant_id)
        if budget is None or budget.monthly_usd_limit is None:
            return
        current = await self._storage.sum_tenant_cost_current_month(self._tenant_id)
        if current >= budget.monthly_usd_limit:
            raise TenantBudgetExceededError(
                f"tenant {self._tenant_id!r} has spent ${current:.2f} of "
                f"${budget.monthly_usd_limit:.2f} this month; runs are paused. "
                f"Operator can raise the budget with "
                f"`movate tenants set-budget {self._tenant_id} --monthly-usd <new>` "
                f"or wait for next-month rollover."
            )

    def _enforce_policy(
        self,
        spec: Any,
        effective_model: ModelConfig,
        is_override: bool,
    ) -> None:
        """Raise ``PolicyViolationError`` if the run would violate policy.

        Called at the top of ``execute()`` before any provider hits.
        Checks the model the executor is about to invoke plus every
        fallback it might try; for ``bench`` (model_override=True) the
        fallback chain is disabled, so we only check the override.
        """
        violations: list[str] = []
        if err := self._policy.check_model(effective_model.provider):
            violations.append(f"primary model: {err}")
        if not is_override:
            for fb in spec.model.fallback:
                if err := self._policy.check_model(fb.provider):
                    violations.append(f"fallback {fb.provider!r}: {err}")
        # Budget ceiling is enforced separately at the cost-check step
        # (we don't know cost yet at executor entry). But if the agent
        # declared a static budget larger than the policy ceiling, the
        # operator should know NOW, not after spending money — so we
        # flag it here too.
        if (
            self._policy.max_cost_per_run_usd is not None
            and spec.budget.max_cost_usd_per_run > self._policy.max_cost_per_run_usd
        ):
            violations.append(
                f"budget.max_cost_usd_per_run={spec.budget.max_cost_usd_per_run} "
                f"exceeds policy ceiling {self._policy.max_cost_per_run_usd}"
            )
        if violations:
            joined = "; ".join(violations)
            raise PolicyViolationError(
                f"agent {spec.name!r} violates model policy: {joined}. See movate.yaml: policy."
            )

    def _check_cost_drift(
        self, span: SpanCtx, completion: CompletionResponse, our_cost: float
    ) -> None:
        litellm_cost = completion.raw.get("litellm_cost_usd")
        if not isinstance(litellm_cost, (int, float)):
            return
        if our_cost <= 0 and litellm_cost <= 0:
            return
        denom = max(abs(our_cost), abs(float(litellm_cost)))
        if denom == 0:
            return
        drift = abs(our_cost - float(litellm_cost)) / denom
        if drift > _COST_DRIFT_THRESHOLD:
            log.warning(
                "cost drift > %.0f%%: pricing-table=$%.6f litellm=$%.6f",
                _COST_DRIFT_THRESHOLD * 100,
                our_cost,
                litellm_cost,
            )
            self._tracer.log_event(
                span,
                {
                    "cost_drift": drift,
                    "cost_pricing_table_usd": our_cost,
                    "cost_litellm_usd": float(litellm_cost),
                },
            )

    async def _record_run(
        self,
        *,
        bundle: AgentBundle,
        run_id: str,
        job_id: str,
        request: RunRequest,
        response: RunResponse,
        chosen_provider: str,
        workflow_run_id: str | None = None,
        node_id: str | None = None,
    ) -> None:
        record = RunRecord(
            run_id=run_id,
            job_id=job_id,
            tenant_id=self._tenant_id,
            agent=bundle.spec.name,
            agent_version=bundle.spec.version,
            prompt_hash=bundle.prompt_hash,
            provider=chosen_provider,
            # provider_version stamps which adapter class produced this
            # run — look up via the registry so multi-runtime executors
            # record the right version per agent.
            provider_version=self._registry.get(bundle.spec.runtime).version,
            pricing_version=self._pricing.version,
            status=JobStatus.SUCCESS,
            input=request.input,
            output=response.data,
            metrics=response.metrics,
            workflow_run_id=workflow_run_id,
            node_id=node_id,
        )
        await self._storage.save_run(record)

    async def _handle_failure(
        self,
        *,
        span: SpanCtx,
        bundle: AgentBundle,
        run_id: str,
        job_id: str,
        started: float,
        err: MovateError,
    ) -> RunResponse:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        status = "safety_blocked" if err.failure_type.value == "content_filter" else "error"
        info = ErrorInfo(type=err.failure_type.value, message=str(err), retryable=err.retryable)
        self._tracer.log_event(span, {"error": info.model_dump()})
        self._tracer.end_span(span, status="error")

        await self._storage.save_failure(
            FailureRecord(
                failure_id=str(uuid4()),
                run_id=run_id,
                tenant_id=self._tenant_id,
                agent=bundle.spec.name,
                failure_type=err.failure_type.value,
                message=str(err),
                retryable=err.retryable,
            )
        )
        # job_id reserved for the workflow + server phases.
        _ = job_id

        return RunResponse(
            status=status,  # type: ignore[arg-type]
            run_id=run_id,
            data={},
            human_readable=f"**Error**: {err}",
            trace_id=span.trace_id,
            metrics=Metrics(latency_ms=elapsed_ms, tokens=TokenUsage()),
            error=info,
        )


async def _invoke_tool(callable_: Callable[..., Any], args: dict[str, Any]) -> Any:
    """Invoke a registered tool with parsed arguments.

    Supports both sync and async callables — async ones are awaited;
    sync ones are run in the default executor (so a slow sync tool
    doesn't block the event loop). Exceptions from the tool itself
    bubble up to the caller — the tool-loop converts them to
    :class:`SchemaError` so the run records a typed failure.
    """
    if inspect.iscoroutinefunction(callable_):
        return await callable_(**args)
    return await asyncio.get_running_loop().run_in_executor(None, lambda: callable_(**args))


def _serialize_tool_result(result: Any) -> str:
    """Convert a tool's return value to the string content the model
    sees in its conversation. Strings pass through; everything else
    gets JSON-encoded so the model sees structured data the same way
    it did the input arguments."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except (TypeError, ValueError):
        return str(result)


def _retry_rule_for(err: MovateError) -> Any:
    return DEFAULT_RETRY.get(err.failure_type)


def _parse_json_output(text: str) -> dict[str, Any]:
    """Extract a JSON object from model output, tolerating markdown fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"model output is not valid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise SchemaError(f"model output must be a JSON object, got {type(result).__name__}")
    return result


def _extract_human_readable(output: dict[str, Any]) -> str:
    for key in ("human_readable", "message", "summary"):
        val = output.get(key)
        if isinstance(val, str):
            return val
    return ""


# Forward-ref bookkeeping
_ = ModelFallback  # keep import for typing in agent specs that reference it
