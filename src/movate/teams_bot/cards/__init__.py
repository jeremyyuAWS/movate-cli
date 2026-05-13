"""Adaptive Card builders for the Teams bot.

Each builder is a **pure function** from a domain object (e.g.
:class:`RunView`) to the Adaptive Card JSON spec (``dict[str, Any]``).
No I/O, no SDK dependency, no side effects — easy to unit-test and
trivial to swap when Adaptive Cards schema versions bump.

The handler wraps each card in an :class:`Attachment` and adds it to
the :class:`ReplyActivity`. Teams renders the card inline in the
channel.

Card builders that ship today:

* :func:`build_run_result_card` (3.1.b) — successful agent run:
  response body, cost, latency, agent metadata. Trace link rendered
  only when a Langfuse public host is configured.
* :func:`build_error_card` (3.1.b) — failure: error category +
  one-line hint, no stack trace. Stack traces live in the runtime's
  logs / Langfuse; Teams gets just enough to know what went wrong.
* :func:`build_agent_upload_card` (3.1.d) — operator dragged an
  agent file in: lists detected name/runtime/model/skills so the
  user can verify "this is the right thing" before running.
* :func:`build_dataset_upload_card` (3.1.d) — dataset upload: row
  count + one-row preview.

Deferred to follow-up slices:

* ``build_confirmation_card`` — "are you sure? this will cost ~\\$X"
  gate before expensive runs.
* ``build_eval_scorecard`` (3.2) — per-case scorecard that updates
  in-place as cases complete, surfaces 4-dim eval rollup from #59.
"""

from movate.teams_bot.cards.error import build_error_card
from movate.teams_bot.cards.run_result import build_run_result_card
from movate.teams_bot.cards.upload import (
    build_agent_upload_card,
    build_dataset_upload_card,
)

__all__ = [
    "build_agent_upload_card",
    "build_dataset_upload_card",
    "build_error_card",
    "build_run_result_card",
]
