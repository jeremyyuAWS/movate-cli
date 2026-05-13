"""Thin wrapper over :class:`MovateClient` for the Teams bot's needs.

The wrapper hides three concerns from the handler:

1. **Submit + wait + fetch.** The handler wants "give me a run result
   for ``agent=foo, input=bar``" — the wrapper splits this into the
   three runtime calls (``submit_job`` → ``wait_for_terminal`` →
   ``get_run``) and returns a single :class:`RunOutcome` value.
2. **Error categorisation.** Failures from the runtime arrive as
   structured :class:`ErrorInfo` (terminal) or :class:`MovateClientError`
   (network / auth). The wrapper normalises them into a discriminated
   :class:`RunOutcome` shape with ``success`` / ``terminal_failure`` /
   ``client_failure`` variants — the card builder picks the right
   template by inspecting the variant.
3. **Bounded waits.** Teams' channel pipeline expects a reply within
   ~15 seconds; longer agent runs would time the channel out. The
   wrapper enforces ``MOVATE_TEAMS_RUN_TIMEOUT_S`` (default 25s) and
   converts a timeout into a structured "job still running" outcome
   that the card can render gracefully ("Job submitted; check
   ``mdk jobs show <id>`` in a minute").

Slice 3.1.b uses a **single fleet API key** from the env var
``MOVATE_TEAMS_FLEET_API_KEY``. Per-user keys + identity binding
land in 3.1.c (issue #67); the wrapper's API is shaped to accept a
key per call so the migration is one-line.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from movate.core.client import MovateClient, MovateClientError
from movate.core.models import JobKind, JobStatus
from movate.runtime.schemas import JobView, RunView

# How long to wait for a job to reach terminal before giving up. Teams
# itself times out the outer HTTP request to /api/messages around 15s;
# this slightly higher value buys headroom for the round-trip + serialisation.
# Configurable via env so an operator pinning a slow agent's runtime can
# bump it without code changes.
_DEFAULT_RUN_TIMEOUT_ENV = "MOVATE_TEAMS_RUN_TIMEOUT_S"
_DEFAULT_RUN_TIMEOUT_S = 25.0

# Poll cadence while waiting. Faster polls = lower median latency but
# higher load on the runtime; 0.5s is a fair default for human-interactive
# Teams chat where the request is already racing the channel timeout.
_DEFAULT_POLL_INTERVAL_S = 0.5


@dataclass
class RunOutcome:
    """Discriminated result of a single run attempt.

    ``kind`` selects the variant:

    * ``"success"`` — agent ran cleanly; ``run`` carries the RunView
      (with ``output``, ``metrics``, etc.).
    * ``"terminal_failure"`` — runtime accepted the job and the job
      reached a terminal status with an error. ``run`` may carry a
      :class:`RunView` (partial run with ``error`` field set) OR be
      ``None`` if the job died before a run was created. ``job`` is
      always set in this variant.
    * ``"client_failure"`` — the bot's HTTP call to the runtime failed
      (auth, agent-not-found, network). ``message`` carries the
      operator-readable summary; ``run`` / ``job`` are ``None``.
    * ``"timeout"`` — wait_for_terminal exceeded the budget; the job
      is still progressing on the runtime. ``job_id`` carries the id
      so the card can suggest ``mdk jobs show <id>``.

    The card builders dispatch on ``kind`` to pick run_result vs
    error template.
    """

    kind: Literal["success", "terminal_failure", "client_failure", "timeout"]
    run: RunView | None = None
    job: JobView | None = None
    job_id: str = ""
    """For ``timeout``: the id the operator can poll later. Empty for
    other variants."""

    message: str = ""
    """One-line error summary for ``client_failure``. Empty for success."""

    category: str = ""
    """Short code for terminal_failure / client_failure (``schema_error``,
    ``rate_limit``, ``not_found``, etc.). Empty for success."""

    hint: str = ""
    """Optional follow-up suggestion ("Check MOVATE_TEAMS_FLEET_API_KEY"
    / "Run `mdk jobs show <id>`"). Empty when no hint applies."""


async def execute_run(
    *,
    client: MovateClient,
    agent: str,
    input_payload: dict[str, Any],
    timeout_s: float | None = None,
) -> RunOutcome:
    """Submit a single agent run and wait for terminal.

    The function is intentionally tied to ONE :class:`MovateClient`
    instance — the caller (build_app / handler) owns lifecycle. We
    don't create or close the client here so the connection pool
    stays warm across requests.

    Three failure modes:

    1. Submission fails (auth, agent unknown, malformed input) →
       ``RunOutcome(kind="client_failure", message=..., category=...)``
    2. Job reaches terminal-with-error within budget →
       ``RunOutcome(kind="terminal_failure", run=..., job=...)``
    3. Timeout before terminal →
       ``RunOutcome(kind="timeout", job_id=...)``
    """
    effective_timeout = timeout_s if timeout_s is not None else _resolve_timeout()

    # --- Stage 1: submit ----------------------------------------------------
    try:
        accepted = await client.submit_job(
            kind=JobKind.AGENT,
            target=agent,
            input=input_payload,
        )
    except MovateClientError as exc:
        # The runtime structured-error path gives us code + message; non-2xx
        # responses without a movate envelope get a generic message. We
        # surface both via the same client_failure variant so the card
        # builder doesn't have to special-case. ``MovateClientError`` may
        # carry a ``code`` attribute (set in client.py from the
        # runtime's structured error envelope); fall back to a generic
        # category when absent.
        code = getattr(exc, "code", None) or "client_error"
        return RunOutcome(
            kind="client_failure",
            message=str(exc) or "submission rejected by runtime",
            category=code,
            hint=_hint_for(code, agent=agent),
        )

    # --- Stage 2: poll until terminal --------------------------------------
    try:
        job = await client.wait_for_terminal(
            accepted.job_id,
            poll_interval_seconds=_DEFAULT_POLL_INTERVAL_S,
            max_wait_seconds=effective_timeout,
        )
    except TimeoutError:
        # Job is still progressing on the runtime — operator can come
        # back with ``mdk jobs show <id>``. Not a failure of the bot;
        # the card explains how to recover.
        return RunOutcome(
            kind="timeout",
            job_id=accepted.job_id,
            message=(
                f"job still running after {effective_timeout:.0f}s; "
                f"the runtime is still working on it"
            ),
            hint=(f"check progress with `mdk jobs show {accepted.job_id}` or wait and re-ask"),
        )
    except MovateClientError as exc:
        # Polling itself failed (transient network, runtime restart
        # mid-poll). Surface as client_failure so the card shows the
        # actionable failure rather than crashing.
        return RunOutcome(
            kind="client_failure",
            message=f"failed polling job {accepted.job_id}: {exc}",
            category="poll_failed",
        )

    # --- Stage 3: classify the terminal status -----------------------------
    if job.status == JobStatus.SUCCESS:
        # Fetch the actual run for the output payload. /jobs/{id} only
        # carries pointer state; /runs/{id} has output + metrics. Two
        # round-trips by design — runs are big, jobs are small.
        if job.result_run_id is None:
            # Should never happen on SUCCESS but defensive.
            return RunOutcome(
                kind="client_failure",
                message="runtime reported SUCCESS but produced no run_id",
                category="malformed_response",
            )
        try:
            run = await client.get_run(job.result_run_id)
        except MovateClientError as exc:
            return RunOutcome(
                kind="client_failure",
                message=f"job {accepted.job_id} succeeded but couldn't fetch run: {exc}",
                category="fetch_failed",
            )
        return RunOutcome(kind="success", run=run, job=job)

    # ERROR or SAFETY_BLOCKED — the agent ran but did not succeed.
    # If we have a result_run_id, fetch the run for its error field.
    # Otherwise the job's own error field is the only signal.
    run_view: RunView | None = None
    if job.result_run_id is not None:
        try:
            run_view = await client.get_run(job.result_run_id)
        except MovateClientError:
            # Non-fatal: we can still show the job-level error below.
            run_view = None

    error_code = ""
    error_msg = "job failed without a structured error"
    if run_view is not None and run_view.error is not None:
        error_code = run_view.error.type
        error_msg = run_view.error.message
    elif job.error is not None:
        error_code = job.error.type
        error_msg = job.error.message

    return RunOutcome(
        kind="terminal_failure",
        run=run_view,
        job=job,
        category=error_code,
        message=error_msg,
        hint=_hint_for(error_code, agent=agent),
    )


def _resolve_timeout() -> float:
    """Read ``MOVATE_TEAMS_RUN_TIMEOUT_S`` from the env, falling back
    to the default. Invalid values fall through to the default rather
    than crashing the request — log noise, not user-visible failure."""
    raw = os.environ.get(_DEFAULT_RUN_TIMEOUT_ENV)
    if not raw:
        return _DEFAULT_RUN_TIMEOUT_S
    try:
        return max(1.0, float(raw))
    except ValueError:
        return _DEFAULT_RUN_TIMEOUT_S


def _hint_for(code: str, *, agent: str) -> str:
    """Look up a one-line suggestion for a given error code.

    Hints are deliberately operator-facing (assume the recipient
    can SSH / check env / read logs) — Teams renders them subtly
    underneath the main error. Not all codes get a hint; an empty
    string means "no hint, just show the message."
    """
    if not code:
        return ""
    table = {
        "auth_error": (
            "Set MOVATE_TEAMS_FLEET_API_KEY to a valid Movate API key, "
            "then restart `mdk teams-bot serve`."
        ),
        "not_found": (
            f"Is '{agent}' registered on the runtime? "
            "Try `mdk jobs list-agents` against the same target."
        ),
        "schema_error": (
            "The input didn't match the agent's input schema. "
            "Try `mdk show <agent>` to see the expected shape."
        ),
        "rate_limit": ("Provider rate limit — wait a few seconds and re-ask."),
        "context_length": (
            "The input was too large for this model. Trim it or switch "
            "to a larger-context model in agent.yaml."
        ),
        "policy_violation": (
            "An mdk policy blocked this run. Check `mdk policy export` for the active rules."
        ),
        "budget_exceeded": (
            "The run hit its `budget.max_cost_usd_per_run` cap. "
            "Raise it in agent.yaml or simplify the prompt."
        ),
    }
    return table.get(code, "")
