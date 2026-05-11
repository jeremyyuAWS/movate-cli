# Determinism, replay, and failure recovery

**Audience:** anyone designing a new node type, retry policy, or replay
feature. **Status:** v1.0 semantics frozen; v1.1+ items called out
explicitly.

This doc answers the practical question operators keep asking:

> A run failed at node N. What actually happens? What state survives?
> Can I resume? Will the next run produce the same output?

The short version: movate distinguishes between **transient** and
**persistent** failures, preserves enough state at the point of failure
to make a human decision, and defers automatic resume-from-checkpoint to
v1.1 when LangGraph's checkpointer ships. Until then, "resume" is "rerun
the whole workflow with the same inputs after fixing the failing node."

---

## TL;DR — the failure matrix

| Failure | Retries? | What's persisted | Operator action | Status |
|---|---|---|---|---|
| Transient (rate-limit / network) | Yes — backoff + jitter | Last error in `failures` table | None — eventually succeeds | Shipped |
| Auth / content-filter / schema | No — terminal | Run row + failure row | Fix config, rerun | Shipped |
| Per-run budget exceeded | No — terminal | Run row marked `error` | Bump budget OR fix agent | Shipped |
| Tenant monthly budget exceeded | No — terminal | Failure row, no provider call | Raise cap OR wait for month rollover | Shipped |
| Model-policy violation | No — terminal | Failure row, no provider call | Change model OR fix policy | Shipped |
| Workflow node failure | Per-node retry policy applies | Per-node `RunRecord`s up to + including failure; workflow row marked `error` with `error_node_id` | Rerun (no resume in v0.3) | Shipped (v0.3) |
| Worker job exhausts retries | After `max_attempts` | Job row → `DEAD_LETTER` | Operator inspects + requeues | Shipped |
| Single-agent replay (debug) | n/a | Original `RunRecord` is read-only | `movate run --replay <run-id>` | Shipped |
| Workflow resume-from-checkpoint | n/a | Will be persisted via LangGraph checkpointer | `POST /workflows/{id}/resume` | **v1.1+** |

---

## Failure classification

Every error in movate is one of two species, classified at construction
time by [`movate.core.failures`](../src/movate/core/failures.py):

* **Retryable** — the same inputs at a later time might succeed. Rate
  limits, transient network blips, provider 5xx. Caller backs off and
  tries again. Worker re-queues with exponential backoff + jitter.

* **Terminal** — same inputs will always fail. Auth errors, schema
  validation, content-filter rejection, budget exhaustion, policy
  denial. No retry. The failure surfaces immediately and a human
  decides next steps.

The classification lives on the exception type, not on the call site —
adding a new failure mode means picking the right type, not threading a
`retryable: bool` flag through every layer.

| Exception type | retryable | typical cause |
|---|---|---|
| `RateLimitError` | Yes (retry_after honored) | Provider 429 |
| `TransientError` | Yes (backoff) | Provider 5xx, network glitch |
| `AuthError` | No | Bad API key |
| `ValidationError` | No | Input or output JSON schema mismatch |
| `ContentFilterError` | No | Provider safety reject |
| `BudgetExceededError` | No | Per-run `max_cost_usd_per_run` exceeded |
| `TenantBudgetExceededError` | No | Per-tenant monthly cap exceeded |
| `PolicyViolationError` | No | `movate.yaml` `policy:` block denied the call |
| `JobTimeoutError` | No (worker DLQs) | Job exceeded `total_ms` |

---

## Retry policies — agent-level vs job-level

Two distinct retry layers; both default-on, both configurable. They
solve different problems and must not be confused.

### Agent-level (`movate.core.retry`)

Inside `Executor.execute()`. Wraps the provider call. Applies when a
single attempt fails with a retryable exception. Retries the same agent
+ same input with the same model (or the fallback chain, if exhausted).
Bounded by:

* `timeouts.call_ms` per attempt
* `timeouts.total_ms` across all attempts + fallbacks
* `budget.max_cost_usd_per_run` cumulative cost — counts retries

Output: a single `RunRecord` with `metrics.attempts > 1` if retries
happened.

### Job-level (`movate.core.job_retry`)

In the worker. Wraps the entire agent (or workflow) execution. Applies
when an agent or workflow returns terminal-error with a retryable cause
— including infrastructure failures the agent layer can't see (worker
crashed mid-execute, postgres connection lost).

Bounded by `JobRetryPolicy`:

* `max_attempts` (default 3)
* `base_delay_s` × `factor ^ attempt` (exponential)
* `±25%` jitter to de-pile thundering herds
* `max_delay_s` ceiling

After `max_attempts`, the job lands in `JobStatus.DEAD_LETTER` and
notifications fire. No automatic retry beyond that — an operator must
inspect + requeue.

**The two layers compose.** A network blip retries inside the agent
(agent-level). A worker crash retries the whole job (job-level). Don't
add a third layer between them.

---

## State preservation — what survives a failure

The two questions that matter:

1. **Can I see what went wrong?** Yes. Every failure writes a
   `FailureRecord` row with the typed exception, the traceback, the
   prompt hash, the input that triggered it, and the failing model.
   `movate logs <run-id> --tail` (v0.4) renders the timeline.

2. **Can I reconstruct the state at the point of failure?** Yes, with
   caveats by failure type:

| Failure | Pre-failure state preserved? | Where |
|---|---|---|
| Single-agent retryable | Yes — retries are in-process | RunRecord + retry-history events |
| Single-agent terminal | Yes — input, prompt, partial response if any | RunRecord + FailureRecord |
| Workflow node terminal | Yes — every successful node's RunRecord; failing node's input | WorkflowRunRecord + per-node RunRecord chain |
| Worker job retry | Yes — input pinned at submit time; outputs not yet persisted | JobRecord (`attempt_count`, `next_retry_at`) |
| Worker dead-letter | Yes — final attempt's RunRecord + every FailureRecord | JobRecord (`DEAD_LETTER`) + RunRecord + FailureRecord chain |

What's **not** preserved today:

* Cached LLM responses for replay. The executor doesn't have a
  content-addressed prompt-hash → response cache. Adding one is
  on the v1.1 list for deterministic replay.
* Mid-execution tool-call state. There are no tool nodes in v0.3, so
  the question doesn't arise yet.
* Partial token streams. Streaming preview is stderr-only; the final
  schema-validated response is what gets persisted.

---

## Workflow node failure — explicit answers

The exact questions from the architecture review:

> **If node 7 fails: do you replay from node 6?**

**v0.3: No automatic resume.** The workflow run is marked `error` with
`error_node_id = "n7"`. RunRecords for nodes 1–6 are kept (they
succeeded). Re-running the workflow with the same input starts over
from node 1.

**v1.1+: Yes, with the LangGraph checkpointer.** The
[LangGraph seam prototype](./langgraph-seam.md) validates this — each
node's post-state is checkpointed; resume calls `graph.invoke(None,
config={"thread_id": ...})` which continues from the last successful
checkpoint. v1.1 will surface this as `POST /workflows/{id}/resume`.

> **Rehydrate state?**

**v0.3: Manually, by reading the per-node RunRecords.** The
WorkflowRunRecord stores the initial state; per-node RunRecords store
each node's output. Reconstructing the pre-failure state is a join.
There's no "resume from saved state" runtime path.

**v1.1+: Yes, automatic.** Checkpointer-backed state is rehydrated on
resume. Tenant isolation: the checkpoint key must include `tenant_id` so
tenant A can't resume tenant B's HITL workflow (called out as an open
question in `docs/langgraph-seam.md`).

> **Rerun tools?**

**v0.3: Not applicable — no tools.** Tool registry lands in v1.1.

**v1.1+: Per-tool declaration.** Tool YAML will require an explicit
flag:

```yaml
tool:
  name: create_ticket
  side_effects: true       # state-mutating; resume MUST NOT replay
  retryable: false
```

The checkpointer consults `side_effects: true` to know NOT to replay
the call on resume — the side effect already happened, replaying would
double it. Idempotent tools (`side_effects: false`) replay safely.
Compiler will reject a workflow that resumes through a
side-effect-true tool without an explicit override.

> **Replay cached LLM output?**

**v0.3: No cache.** Every execution calls the provider. Cost-tracked
per run; expensive in real-LLM mode but the dev-loop relies on `--mock`
for hermetic reruns.

**v1.1+ (proposed): Yes, opt-in via `--replay-cached`.** Content-address
on `(prompt_hash, model.provider, model.params)` → `response.data`.
Cache hit returns the recorded response without a provider call;
cache miss falls through to the live model. Useful for two cases:

1. Debugging downstream nodes when the upstream LLM call is
   deterministic-enough.
2. CI evals that want to test prompt logic without burning API budget.

Not a substitute for `--mock`; mock is faster and entirely hermetic.

> **Invalidate downstream nodes?**

**v0.3: They never ran.** Workflow execution is topological; if node 7
fails, nodes 8–N didn't execute, so there's nothing to invalidate.

**v1.1+ with conditional / parallel topologies:** Failure invalidates
the *branch* containing the failure, not the whole workflow. Sibling
branches that completed successfully stay valid. The
checkpoint reflects partial-success state.

---

## Replay semantics — what's the same, what's different

`movate run --replay <run-id>` (shipped in v0.4) re-executes a recorded
single-agent run against the *current* agent bundle on disk. Output:
diff between original and current.

What's pinned:

* The original input (read from RunRecord).

What's read fresh from disk:

* Prompt template (Jinja file).
* Model config (provider, params, fallback chain).
* Input + output JSON schemas.
* Policy block.

What's recomputed:

* Provider call (uses the agent's current model + prompt).
* Cost (uses current pricing table).
* Latency (real wall-clock).

Replay is a **debug tool**, not a determinism contract. The output is a
diff — `output_changed`, `status_changed`, `changed_keys`, cost +
latency deltas. Output changes are not failures; only a current-run
error trips exit 1.

**Workflow replay is deferred.** Single-agent replay covers ~80% of
debug cases. Workflow replay needs to decide whether to replay every
node or surgical replay of one node — that decision is paired with the
LangGraph checkpointer in v1.1.

---

## Determinism guarantees — what we promise vs what we don't

**We promise:**

* Same agent.yaml + same input + same prompt + same `--mock` →
  byte-identical output. This is the foundation of the eval gate.
* Same agent.yaml + same prompt-hash + same model + same params + same
  input → output that *should* match, modulo provider-side
  non-determinism (temperature, sampling). LLM-as-judge runs use
  `runs: 3+` + mean aggregation to absorb this.
* Cost numbers within ±5% of LiteLLM's reported cost. Drift > 5% logs
  loud (`cost_drift` event in tracing).

**We don't promise:**

* Bit-identical output across two real-LLM calls with the same input.
  Even `temperature: 0.0` doesn't guarantee this — model build dates,
  routing, and tokenization can drift. Use `subset_match` /
  `llm_judge` scoring for tolerance.
* Same execution timing. Provider routing, network, and fallback hops
  vary.
* Resume-from-checkpoint correctness through a side-effecting tool.
  v1.1 will refuse to compile a workflow that crosses a
  `side_effects: true` boundary on resume unless the operator opts in.

---

## Operator playbook — common scenarios

### "My workflow died at node 3. How do I keep going?"

1. `movate trace replay <workflow-run-id>` — see what each node received
   and returned up to the failure.
2. Inspect the failing node's `FailureRecord`. Was it transient
   (retryable=true)? Then it should have already retried — bump
   `JobRetryPolicy.max_attempts` if you want more attempts.
3. Persistent failure → fix the agent or the input shape, rerun the
   entire workflow from scratch.
4. Want partial resume? **You're asking for v1.1.** Track the
   feature request against `docs/langgraph-seam.md` §4 (HITL +
   checkpointing).

### "An agent retried itself 3 times then died with the same error."

`AuthError`, `ValidationError`, and `ContentFilterError` are terminal —
they shouldn't retry. If you're seeing retries on those, file a bug
with the FailureRecord type. If the error is genuinely transient
(rate-limit), bump `JobRetryPolicy.max_attempts` OR
`JobRetryPolicy.max_delay_s`.

### "Same input now produces a different output than last week."

Real-LLM models drift across build dates. Expected. Three options:

1. Pin the model id with a date suffix (`anthropic/claude-haiku-4-5-20251001`),
   not a floating tag.
2. Bump `--regression-tolerance` on the eval gate to absorb noise (we
   default to 0.05 for real-LLM, 0.0 for mock).
3. Use `llm_judge` + `runs: 3+` + `gate_mode: mean` so a single noisy
   call doesn't trip the gate.

### "Worker keeps dead-lettering the same job."

`JobRetryPolicy.max_attempts` has been exhausted. Three causes, in
descending likelihood:

1. **Input bug.** The job's input fails validation in a way that's
   reproducible. Fix the input.
2. **Agent bug.** The prompt + schema combination produces invalid
   JSON every time. Use `movate run <agent> <input>` locally to
   reproduce.
3. **Provider outage.** Live API is down. The job will succeed when
   the provider comes back; meantime `movate jobs requeue <job-id>`
   resets to QUEUED.

---

## What v1.1 will add

Tracked separately in BACKLOG.md + `docs/langgraph-seam.md`:

* Workflow checkpointer (memory / sqlite / postgres) with per-tenant
  isolation.
* `POST /workflows/{id}/resume` HTTP API for HITL.
* Cached-LLM-response replay mode (`movate run --replay-cached`).
* `tool: side_effects: true|false` declaration on the (forthcoming) tool
  registry.
* Conditional + parallel topology failure semantics — branch-level
  invalidation rather than whole-workflow rollback.
* Workflow replay CLI (`movate run --replay <workflow-run-id>`).

The shape of these is sketched in
[`docs/langgraph-seam.md`](./langgraph-seam.md) — that doc records what
the LangGraph spike learned about the IR additions each one needs.

---

## Pointers

* Failure types: [`src/movate/core/failures.py`](../src/movate/core/failures.py)
* Agent-level retry: [`src/movate/core/retry.py`](../src/movate/core/retry.py)
* Job-level retry: [`src/movate/core/job_retry.py`](../src/movate/core/job_retry.py)
* Workflow runner: [`src/movate/core/workflow/runner.py`](../src/movate/core/workflow/runner.py)
* Replay engine: [`src/movate/core/run_replay.py`](../src/movate/core/run_replay.py)
* CI eval gate: [`docs/ci-eval-gate.md`](./ci-eval-gate.md)
* LangGraph seam: [`docs/langgraph-seam.md`](./langgraph-seam.md)
