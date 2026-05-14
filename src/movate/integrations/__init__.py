"""Third-party integrations the MDK runtime can call out to.

Each submodule wraps one external system (GitHub, Slack, Microsoft
Teams, etc.) behind a small, dependency-injectable client. The
convention:

* Submodules import their heavyweight third-party deps **lazily** at
  call time so the base ``movate`` install doesn't pull them in. An
  operator who never enables the integration shouldn't pay the disk-
  space + cold-start cost.
* Each client takes its configuration through a frozen dataclass + an
  injectable transport (typically ``httpx.AsyncClient``-compatible)
  so tests can swap in fakes without touching the network.
* Behavior is gated by an environment flag (``MDK_GITHUB_ENABLED``,
  ``MDK_SLACK_ENABLED``, ...). Disabled state should return a
  structured "not configured" error rather than crashing.

The current entry is :mod:`movate.integrations.github` — version-
controlling agent bundles in a per-tenant GitHub repo per ADR 007.
"""

__all__: list[str] = []
